"""
rich_registry.py
================
حفظ Premium Emoji (custom_emoji) و Rich Text (bold / italic / quote / link / ...) در *تمام*
بخش‌هایی که کاربر یا ادمین می‌بیند.

مشکل قبلی
---------
فقط متن‌هایی که از «ویرایشگر متن» آمده بودند و دکمه‌های ویرایشگر، ایموجی پریمیوم داشتند.
نام پلن‌ها، پوشه‌ها/زیرپوشه‌ها، توضیح پوشه، عنوان راهنما، متن خوش‌آمد و … به‌صورت متن ساده
ذخیره می‌شد (entityها دور ریخته می‌شد) و داخل پیام‌ها / فاکتورها / لیست‌ها / دکمه‌ها ایموجی
عادی نمایش داده می‌شد.

راه‌حل (یک نقطه‌ی مرکزی)
-----------------------
1) هر پیامی که ادمین/ساب‌ادمین می‌فرستد و entity دارد، همراه entityهایش در جدول
   ``rich_snippets`` ثبت می‌شود (کل متن + هر خط + تکه‌های بین {متغیر}ها).
2) هنگام ارسال/ویرایش هر پیام (Bot.__call__) متن خروجی با این تکه‌ها تطبیق داده می‌شود و
   entityها دوباره روی همان محل‌ها گذاشته می‌شوند — فرقی نمی‌کند نام داخل f-string، فاکتور،
   لیست، پیام اعلان یا هر قالب دیگری آمده باشد.
3) دکمه‌ها متن entity ندارند؛ تلگرام فقط «آیکن» (icon_custom_emoji_id) می‌پذیرد. برای هر دکمه
   اولین ایموجی پریمیومِ ثبت‌شده در متنش به‌صورت آیکن گذاشته می‌شود و ایموجی عادیِ پشتش از متن
   دکمه برداشته می‌شود تا دوبار نمایش داده نشود.
4) اگر تلگرام entity/آیکنی را نپذیرد، همان درخواست یک‌بار بدون تزئینات تکرار می‌شود تا
   هیچ پیامی گم نشود.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from datetime import datetime

logger = logging.getLogger(__name__)

# entityهایی که نگه‌داری می‌شوند (mention/url/hashtag/command خودکار هستند و نباید ذخیره شوند)
KEEP_TYPES = {
    "bold", "italic", "underline", "strikethrough", "spoiler",
    "code", "pre", "blockquote", "expandable_blockquote",
    "text_link", "custom_emoji",
}
_KEEP_KEYS = ("type", "offset", "length", "url", "language", "custom_emoji_id")
MAX_SNIPPETS = 4000
MAX_SNIPPET_LEN = 4096
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}")

_lock = threading.RLock()
_loaded = False
_table_ready = False
_map: dict[str, list[dict]] = {}
_order: list[str] = []          # طولانی‌ترین تکه اول
# ایموجی عادی -> شناسه‌ی ایموجی پریمیومی که ادمین جایگزینش کرده (جدیدترین انتخاب برنده است).
# اینطوری هر ایموجی عادی که ادمین حتی یک‌بار در جایی پریمیوم کرده، همه‌جای دیگر هم پریمیوم نمایش داده می‌شود.
_char_map: dict[str, str] = {}
_MAX_ENTITIES = 100
# گروه‌های هم‌معنی؛ هر گروه با یک آیکن پریمیوم نمایش داده می‌شود (در صورت نیاز گروه‌های بیشتری اضافه کن)
_ALIAS_GROUPS = [{"👇", "⬇"}]
_ZWJ = "\u200d"


def _emoji_like_str(piece: str) -> bool:
    if not piece or len(piece) > 12:
        return False
    return not any(ch.isalnum() or ch.isspace() or ch in "{}()[]<>.,:;!?-_/\\|" for ch in piece)


def _learn_chars(text: str, ents: list[dict]) -> None:
    pre = _prefix(text)
    for e in ents:
        if e.get("type") != "custom_emoji":
            continue
        a = _u16_to_py(pre, e["offset"])
        b = _u16_to_py(pre, e["offset"] + e["length"])
        if pre[a] != e["offset"] or pre[b] != e["offset"] + e["length"]:
            continue
        fb = text[a:b]
        if _emoji_like_str(fb):
            eid = str(e["custom_emoji_id"])
            _char_map[fb] = eid
            # ایموجی‌های هم‌معنی (مثلاً 👇 و ⬇️): اگر ادمین یکی را پریمیوم کرده، بقیه‌ی هم‌گروه هم
            # (در صورتی که خودشان جداگانه پریمیوم نشده باشند) همان آیکن را می‌گیرند.
            _fb_base = fb.replace("\ufe0f", "")
            for _grp in _ALIAS_GROUPS:
                if _fb_base in _grp:
                    for _m in _grp:
                        _char_map.setdefault(_m, eid)
                        _char_map.setdefault(_m + "\ufe0f", eid)
            # ایموجی‌هایی مثل ⚪️ ⬜️ ✅ با/بدون variation-selector (U+FE0F) نوشته می‌شوند؛
            # اگر فقط یک شکل یاد گرفته شود، شکل دیگر (مثلاً داخل نوار مصرف و وضعیت سرویس که
            # با کد ساخته می‌شود) پریمیوم نمی‌شد. هر دو شکل ثبت می‌شود.
            base = fb.replace("\ufe0f", "")
            if base and base != fb:
                _char_map.setdefault(base, eid)
            elif base and len(base) == 1 and ord(base) < 0x1F000 and ord(base) >= 0x2190:
                _char_map.setdefault(base + "\ufe0f", eid)


# متنِ دکمه‌ای که فقط آیکن پریمیوم دارد (تلگرام متن خالی را رد می‌کند، پس یک جای‌خالی نامرئی می‌گذاریم)
ICON_ONLY_TEXT = "\u2800"
_EMOJI_JOINERS = "\u200d\ufe0f\ufe0e\u20e3"


def is_emoji_only(text) -> bool:
    """True اگر متن فقط از ایموجی/نماد تشکیل شده باشد (بدون حرف/عدد/فاصله‌ی معنادار)."""
    t = str(text or "").strip()
    if not t:
        return False
    for ch in t:
        if ch.isalnum():
            return False
        if ch.isspace() and ch != "\u3164":
            return False
        if not (ord(ch) >= 0x2190 or ch in _EMOJI_JOINERS or ch in "\u00a9\u00ae\u2122"):
            return False
    return True


def strip_first_emoji(text: str) -> str:
    """اولین ایموجی/نماد داخل متن را برمی‌دارد (برای دکمه‌ای که آیکن پریمیوم دارد تا دوبار نمایش داده نشود)."""
    span = _first_emoji_span(text or "")
    if not span:
        return text
    pre = _prefix(text)
    a = _u16_to_py(pre, span[0])
    b = _u16_to_py(pre, span[0] + span[1])
    new = re.sub(r"[ \t\u200c]{2,}", " ", text[:a] + text[b:]).strip()
    return new or text


def auto_map_enabled() -> bool:
    try:
        import database as db
        return str(db.get_setting("rich_auto_emoji", "1") or "1") != "0"
    except Exception:
        return True


# ---------------------------------------------------------------------------
# ابزارهای UTF-16
# ---------------------------------------------------------------------------
def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _prefix(text: str) -> list[int]:
    """pre[i] = تعداد واحدهای UTF-16 قبل از کاراکتر i (طول = len+1)."""
    pre = [0] * (len(text) + 1)
    acc = 0
    for i, ch in enumerate(text):
        acc += 2 if ord(ch) > 0xFFFF else 1
        pre[i + 1] = acc
    return pre


def _u16_to_py(pre: list[int], units: int) -> int:
    """نزدیک‌ترین اندیس پایتونی که ابتدای آن روی units یا بعد از آن است."""
    lo, hi = 0, len(pre) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pre[mid] < units:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _strip_bounds(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return a, b


def _alnum_count(s: str) -> int:
    return sum(1 for ch in s if ch.isalnum())


# ---------------------------------------------------------------------------
# نرمال‌سازی entity
# ---------------------------------------------------------------------------
def normalize(entities) -> list[dict]:
    out = []
    for e in entities or []:
        try:
            if hasattr(e, "model_dump"):
                e = e.model_dump(exclude_none=True)
            elif not isinstance(e, dict):
                e = dict(e)
            if e.get("type") not in KEEP_TYPES:
                continue
            if e["type"] == "custom_emoji" and not e.get("custom_emoji_id"):
                continue
            item = {k: e[k] for k in _KEEP_KEYS if e.get(k) is not None}
            item["offset"] = int(item["offset"])
            item["length"] = int(item["length"])
            if item["length"] <= 0:
                continue
            out.append(item)
        except Exception:
            continue
    return out


def _clip(pre: list[int], ents: list[dict], a: int, b: int) -> list[dict]:
    """entityهای مربوط به text[a:b] را با offset نسبی (UTF-16) برمی‌گرداند."""
    ua, ub = pre[a], pre[b]
    out = []
    for e in ents:
        s, t = e["offset"], e["offset"] + e["length"]
        s2, t2 = max(s, ua), min(t, ub)
        if t2 <= s2:
            continue
        if e["type"] == "custom_emoji" and (s2 != s or t2 != t):
            continue            # ایموجی را نصفه نمی‌بریم
        item = dict(e)
        item["offset"] = s2 - ua
        item["length"] = t2 - s2
        out.append(item)
    return out


def shift_entities(entities, offset: int) -> list[dict]:
    """همه‌ی entityها را به اندازه‌ی offset (واحد UTF-16) جابه‌جا می‌کند (برای وقتی که پیشوندی به متن اضافه شده)."""
    out = []
    for e in normalize(entities):
        item = dict(e)
        item["offset"] = int(item["offset"]) + int(offset)
        out.append(item)
    return out


def to_message_entities(entities) -> list:
    """لیست dict entity را به MessageEntity های aiogram تبدیل می‌کند (ایموجی پریمیوم/فرمت حفظ می‌شود)."""
    from aiogram.types import MessageEntity
    out = []
    for e in normalize(entities):
        try:
            out.append(MessageEntity(**e))
        except Exception:
            continue
    return out


def has_premium_for(symbol: str) -> bool:
    """آیا برای این ایموجی عادی، نسخه‌ی پریمیوم یاد گرفته شده است؟"""
    _load()
    sym = str(symbol or "")
    if not sym:
        return False
    cand = {sym, sym.replace("\ufe0f", ""), sym.replace("\ufe0f", "") + "\ufe0f"}
    return any(c in _char_map for c in cand)


def strip_with_entities(text: str, entities) -> tuple[str, list[dict]]:
    """متن را trim می‌کند و entityها (ایموجی پریمیوم/فرمت) را با همان برش جابه‌جا می‌کند."""
    text = text or ""
    ents = normalize(entities)
    a, b = _strip_bounds(text, 0, len(text))
    pre = _prefix(text)
    return text[a:b], _clip(pre, ents, a, b)


# ---------------------------------------------------------------------------
# ذخیره‌سازی
# ---------------------------------------------------------------------------
def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    import database as db
    with db.transaction() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS rich_snippets ("
            "text TEXT PRIMARY KEY, entities_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    _table_ready = True


def _rebuild_order() -> None:
    global _order
    _order = sorted(_map.keys(), key=len, reverse=True)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            import database as db
            _ensure_table()
            cur = db.get_connection().cursor()
            cur.execute("SELECT text, entities_json FROM rich_snippets ORDER BY updated_at ASC")
            for text, raw in cur.fetchall():
                try:
                    ents = normalize(json.loads(raw))
                except Exception:
                    ents = []
                if text and ents:
                    _map[text] = ents
                    _learn_chars(text, ents)
            _rebuild_order()
            _loaded = True
        except Exception:
            logger.exception("rich_registry load failed")
            _loaded = True


def _candidates(text: str):
    """(start, end, is_whole) تکه‌هایی از متن که ثبت می‌شوند."""
    out: list[tuple[int, int, bool]] = []
    ws, we = _strip_bounds(text, 0, len(text))
    if we <= ws:
        return out
    out.append((ws, we, True))
    pos = 0
    for line in text.split("\n"):
        la, lb = pos, pos + len(line)
        pos = lb + 1
        a, b = _strip_bounds(text, la, lb)
        if b <= a:
            continue
        if (a, b) != (ws, we):
            out.append((a, b, False))
        pieces, last = [], a
        for m in _PLACEHOLDER.finditer(text, a, b):
            pieces.append((last, m.start()))
            last = m.end()
        pieces.append((last, b))
        if len(pieces) > 1:
            for pa, pb in pieces:
                pa, pb = _strip_bounds(text, pa, pb)
                if pb > pa and (pa, pb) != (a, b):
                    out.append((pa, pb, False))
    return out


def register(text: str, entities, *, forget_plain: bool = True) -> int:
    """متن + entityهایش را ثبت می‌کند. اگر متن هیچ entity قابل‌نگه‌داری نداشته باشد و
    forget_plain=True باشد، ثبت قبلیِ دقیقاً همین متن پاک می‌شود (یعنی ادمین نسخه‌ی ساده را
    جایگزین کرده است)."""
    if not text or not isinstance(text, str):
        return 0
    _load()
    ents = normalize(entities)
    pre = _prefix(text)
    save: dict[str, list[dict]] = {}
    cands = _candidates(text)
    whole_seg = None
    for a, b, whole in cands:
        seg = text[a:b]
        if whole:
            whole_seg = seg
        if len(seg) > MAX_SNIPPET_LEN:
            continue
        if not whole and _alnum_count(seg) < 2:
            continue
        clipped = _clip(pre, ents, a, b)
        if clipped:
            save[seg] = clipped
    forget: set[str] = set()
    # نسخه‌ی ساده‌ی دقیقاً همین متن = ادمین ایموجی/فرمت را برداشته است
    if forget_plain and whole_seg is not None and whole_seg not in save and whole_seg in _map:
        forget.add(whole_seg)
    changed = {s: e for s, e in save.items() if _map.get(s) != e}
    if not changed and not forget:
        return 0
    now = datetime.utcnow().isoformat()
    try:
        import database as db
        _ensure_table()
        with db.transaction() as cur:
            for s, e in changed.items():
                cur.execute(
                    "INSERT INTO rich_snippets(text, entities_json, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(text) DO UPDATE SET entities_json=excluded.entities_json, "
                    "updated_at=excluded.updated_at",
                    (s, json.dumps(e, ensure_ascii=False), now),
                )
            for s in forget:
                cur.execute("DELETE FROM rich_snippets WHERE text=?", (s,))
            if len(_map) + len(changed) > MAX_SNIPPETS:
                cur.execute(
                    "DELETE FROM rich_snippets WHERE text IN ("
                    "SELECT text FROM rich_snippets ORDER BY updated_at ASC LIMIT ?)",
                    (max(50, len(_map) + len(changed) - MAX_SNIPPETS),),
                )
    except Exception:
        logger.exception("rich_registry save failed")
    with _lock:
        for s, e in changed.items():
            _map[s] = e
            _learn_chars(s, e)
        for s in forget:
            _map.pop(s, None)
        if len(_map) > MAX_SNIPPETS:
            for s in sorted(_map.keys(), key=len)[: len(_map) - MAX_SNIPPETS]:
                _map.pop(s, None)
        _rebuild_order()
    return len(changed) + len(forget)


def register_from_message(message) -> int:
    """متن/کپشن + entityهای یک aiogram Message را ثبت می‌کند."""
    text = getattr(message, "text", None)
    ents = getattr(message, "entities", None)
    if text is None:
        text = getattr(message, "caption", None)
        ents = getattr(message, "caption_entities", None)
    return register(text or "", ents)


def forget(text: str) -> None:
    """ثبت یک نام/متن را حذف می‌کند (مثلاً بعد از حذف پلن یا پوشه)."""
    if not text:
        return
    _load()
    s = text.strip()
    if s in _map:
        try:
            import database as db
            with db.transaction() as cur:
                cur.execute("DELETE FROM rich_snippets WHERE text=?", (s,))
        except Exception:
            logger.exception("rich_registry forget failed")
        with _lock:
            _map.pop(s, None)
            _rebuild_order()


# ---------------------------------------------------------------------------
# مهاجرت/پرکردنِ داده‌های موجود (هر بار با بالا آمدن ربات؛ idempotent)
# ---------------------------------------------------------------------------
def _first_emoji_span(name: str):
    """(offset, length) بر حسب UTF-16 اولین ایموجی/نماد داخل نام، در هر جای نام (ابتدا، وسط یا انتها)؛ یا None.
    برای نام‌های قدیمی که فقط شناسه‌ی اولین ایموجی‌شان ذخیره شده بود و محلش از دست رفته."""
    if not name:
        return None
    joiners = "\u200d\ufe0f\ufe0e\u20e3"
    i, n = 0, len(name)
    while i < n:
        ch = name[i]
        if (not ch.isalnum() and not ch.isspace() and ch not in joiners
                and (ord(ch) >= 0x2190 or ch in "\u00a9\u00ae\u2122")):
            j = i + 1
            while j < n and (name[j] in joiners or (not name[j].isalnum() and not name[j].isspace()
                                                    and ord(name[j]) >= 0x2190)):
                j += 1
            pre = _prefix(name)
            return pre[i], pre[j] - pre[i]
        i += 1
    return None


def backfill() -> None:
    """رکوردهای قبلی (متن‌های ویرایشگر، راهنماها، نام‌های دارای ایموجی قدیمی) را وارد رجیستری می‌کند."""
    try:
        import database as db
        _load()
        cur = db.get_connection().cursor()
        # 1) متن‌های ویرایشگر UI
        try:
            cur.execute("SELECT text, entities_json FROM ui_text_overrides")
            for text, raw in cur.fetchall():
                try:
                    ents = json.loads(raw) if raw else []
                except Exception:
                    ents = []
                if ents:
                    register(text or "", ents, forget_plain=False)
        except Exception:
            pass
        # 2) متن راهنماها
        try:
            for g in db.get_guides() or []:
                ents = g.get("body_entities") or []
                if ents and g.get("body_text"):
                    register(str(g["body_text"]), ents, forget_plain=False)
        except Exception:
            logger.debug("backfill guides failed", exc_info=True)
        # 3) نام‌هایی که فقط اولین ایموجی‌شان ذخیره شده بود (text:<name>)
        try:
            cur.execute("SELECT key, custom_emoji_id FROM ui_legacy_emoji_meta WHERE key LIKE 'text:%'")
            for key, emoji_id in cur.fetchall():
                name = str(key)[5:]
                if not emoji_id or not name or name in _map:
                    continue
                span = _first_emoji_span(name)
                if span:
                    register(name, [{"type": "custom_emoji", "offset": span[0], "length": span[1],
                                     "custom_emoji_id": str(emoji_id)}], forget_plain=False)
        except Exception:
            pass
    except Exception:
        logger.exception("rich_registry backfill failed")


# ---------------------------------------------------------------------------
# اعمال روی متن خروجی
# ---------------------------------------------------------------------------
def _overlap(a1, b1, a2, b2) -> bool:
    return a1 < b2 and a2 < b1


def _conflicts(new: dict, existing: list[dict]) -> bool:
    s, e = new["offset"], new["offset"] + new["length"]
    for x in existing:
        xs, xe = x["offset"], x["offset"] + x["length"]
        if not _overlap(s, e, xs, xe):
            continue
        if x["type"] == new["type"] or {x["type"], new["type"]} & {"pre", "code"}:
            return True
    return False


def entities_for(text: str, existing=None) -> list[dict]:
    """entityهای ثبت‌شده‌ای که باید روی متن خروجی بنشینند (offset بر حسب UTF-16)."""
    if not text or not isinstance(text, str):
        return []
    _load()
    if not _map:
        return []
    existing_n = []
    for x in existing or []:
        try:
            if hasattr(x, "model_dump"):
                x = x.model_dump(exclude_none=True)
            existing_n.append({"type": x.get("type"), "offset": int(x.get("offset", 0)),
                               "length": int(x.get("length", 0))})
        except Exception:
            continue
    pre = None
    claimed: list[tuple[int, int]] = []
    added: list[dict] = []
    tl = len(text)
    for snip in _order:
        if len(snip) > tl:
            continue
        start = text.find(snip)
        while start != -1:
            if pre is None:
                pre = _prefix(text)
            ua, ub = pre[start], pre[start + len(snip)]
            if not any(_overlap(ua, ub, c0, c1) for c0, c1 in claimed):
                mine = []
                for e in _map.get(snip, ()):
                    item = dict(e)
                    item["offset"] = ua + e["offset"]
                    if item["offset"] + item["length"] > ub:
                        continue
                    if not _conflicts(item, existing_n + added + mine):
                        mine.append(item)
                if mine:
                    added.extend(mine)
                claimed.append((ua, ub))
            start = text.find(snip, start + len(snip))
    if _char_map and auto_map_enabled():
        if pre is None:
            pre = _prefix(text)
        taken = existing_n + added
        total = sum(1 for x in taken)
        for fb in sorted(_char_map, key=len, reverse=True):
            if total >= _MAX_ENTITIES:
                break
            i = text.find(fb)
            while i != -1 and total < _MAX_ENTITIES:
                j = i + len(fb)
                # 👇 + U+FE0F (variation selector) را هم یک ایموجی حساب کن، نه «چسبیده به کاراکتر دیگر»
                if j < tl and text[j] == "\ufe0f" and not fb.endswith("\ufe0f"):
                    j += 1
                before = text[i - 1] if i > 0 else ""
                after = text[j] if j < tl else ""
                glued = before == _ZWJ or bool(after and (
                    after == _ZWJ or after in ("\ufe0f", "\u20e3") or "\U0001f3fb" <= after <= "\U0001f3ff"))
                if not glued:
                    item = {"type": "custom_emoji", "offset": pre[i], "length": pre[j] - pre[i],
                            "custom_emoji_id": _char_map[fb]}
                    if not _conflicts(item, taken):
                        added.append(item)
                        taken.append(item)
                        total += 1
                i = text.find(fb, j)
    added.sort(key=lambda d: (d["offset"], -d["length"]))
    return added


# ---------------------------------------------------------------------------
# دکمه‌ها
# ---------------------------------------------------------------------------
def _first_custom_emoji(ents: list[dict]):
    ce = [e for e in ents if e["type"] == "custom_emoji"]
    if not ce:
        return None
    return min(ce, key=lambda e: e["offset"])


def button_icon(text: str, current_icon: str | None = None):
    """(icon_id, new_text) — اولین ایموجی پریمیوم ثبت‌شده در متن دکمه و حذف ایموجی عادیِ آن.
    اگر current_icon داده شود فقط تکه‌ای که همان آیکن را دارد بررسی می‌شود (برای حذف ایموجی تکراری)."""
    if not text or not isinstance(text, str):
        return None, text
    _load()
    if not _map:
        return None, text
    for snip in _order:
        if len(snip) > len(text):
            continue
        i = text.find(snip)
        if i < 0:
            continue
        ce = _first_custom_emoji(_map.get(snip, ()))
        if not ce:
            continue
        if current_icon and str(ce["custom_emoji_id"]) != str(current_icon):
            continue
        spre = _prefix(snip)
        a = _u16_to_py(spre, ce["offset"])
        b = _u16_to_py(spre, ce["offset"] + ce["length"])
        new = text[: i + a] + text[i + b:]
        new = re.sub(r"[ \t‌]{2,}", " ", new).strip()
        if not new:
            # دکمه فقط یک ایموجی بود: آیکن پریمیوم جای آن را می‌گیرد؛ نگه‌داشتن ایموجی عادی = دو ایموجی
            new = ICON_ONLY_TEXT
        return str(ce["custom_emoji_id"]), new
    if _char_map and auto_map_enabled():
        best = None
        for fb, eid in _char_map.items():
            if current_icon and str(eid) != str(current_icon):
                continue
            i = text.find(fb)
            if i < 0:
                continue
            if i > 0 and text[i - 1] == _ZWJ:
                continue
            if best is None or i < best[0] or (i == best[0] and len(fb) > len(best[1])):
                best = (i, fb, eid)
        if best:
            i, fb, eid = best
            _end = i + len(fb)
            if _end < len(text) and text[_end] == "\ufe0f":
                _end += 1
            new = text[:i] + text[_end:]
            new = re.sub(r"[ \t\u200c]{2,}", " ", new).strip()
            return str(eid), (new or ICON_ONLY_TEXT)
    return None, text


_NO_AUTO_ICON: dict = {}


def mark_no_auto_icon(btn) -> None:
    """دکمه‌ای که ادمین متنش را در ویرایشگر دکمه‌ها عوض کرده: آیکن خودکار (از تکه‌های ثبت‌شده/نگاشت ایموجی) نمی‌گیرد."""
    if len(_NO_AUTO_ICON) > 4000:
        _NO_AUTO_ICON.clear()
    _NO_AUTO_ICON[id(btn)] = btn


def _process_buttons(rows, attr_rows: str):
    changed = False
    new_rows = []
    for row in rows or []:
        new_row = []
        for b in row:
            try:
                if _NO_AUTO_ICON.get(id(b)) is b and not getattr(b, "icon_custom_emoji_id", None):
                    new_row.append(b)
                    continue
                t = getattr(b, "text", None)
                cur_icon = getattr(b, "icon_custom_emoji_id", None)
                if t and "icon_custom_emoji_id" in type(b).model_fields:
                    icon, nt = button_icon(t, cur_icon)
                    if icon and (icon != cur_icon or nt != t):
                        b = b.model_copy(update={"text": nt, "icon_custom_emoji_id": icon})
                        changed = True
            except Exception:
                logger.debug("rich_registry button failed", exc_info=True)
            new_row.append(b)
        new_rows.append(new_row)
    return new_rows, changed


def process_markup(markup):
    """کیبورد (اینلاین/ریپلای) را با آیکن پریمیوم دکمه‌ها برمی‌گرداند؛ اگر تغییری نباشد همان را."""
    if markup is None:
        return markup
    # فقط کیبورد اینلاین؛ متن دکمه‌های ReplyKeyboard همان متنی است که کاربر موقع زدن می‌فرستد و
    # هندلرها با آن تطبیق می‌دهند، پس نباید تغییر کند (آیکن آن‌ها از ویرایشگر دکمه می‌آید).
    rows = getattr(markup, "inline_keyboard", None)
    if rows:
        new_rows, changed = _process_buttons(rows, "inline_keyboard")
        if changed:
            return markup.model_copy(update={"inline_keyboard": new_rows})
    return markup


# ---------------------------------------------------------------------------
# اتصال به درخواست‌های Bot API
# ---------------------------------------------------------------------------
_TEXT_FIELDS = (("text", "entities"), ("caption", "caption_entities"))



# ---------------------------------------------------------------------------
# تبدیل parse_mode=Markdown/HTML به entity (تا پیام‌های Markdown/HTML هم پریمیوم بگیرند)
# ---------------------------------------------------------------------------
def _norm_pm(pm):
    if pm is None:
        return ""
    return str(getattr(pm, "value", pm)).strip().lower()


def md_to_entities(text: str):
    """Markdown قدیمی تلگرام (*bold* _italic_ `code` ```pre``` [t](url)) -> (plain_text, entities).
    اگر ساختار نامعتبر باشد None برمی‌گرداند (پیام بدون تغییر ارسال می‌شود)."""
    out: list[str] = []
    ents: list[dict] = []
    ulen = 0
    i, n = 0, len(text)

    def push(s):
        nonlocal ulen
        out.append(s)
        ulen += u16len(s)

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in "_*`[":
            push(text[i + 1]); i += 2; continue
        if text.startswith("```", i):
            j = text.find("```", i + 3)
            if j < 0:
                return None
            body = text[i + 3:j]
            if "\n" in body.split("\n", 1)[0] or True:
                first, _, rest = body.partition("\n")
                lang = first.strip() if rest and re.fullmatch(r"[A-Za-z0-9_+-]+", first.strip() or "x") else ""
                body = rest if lang else body
            start = ulen
            push(body)
            ents.append({"type": "pre", "offset": start, "length": ulen - start, **({"language": lang} if lang else {})})
            i = j + 3; continue
        if ch in "*_`":
            j = text.find(ch, i + 1)
            if j < 0:
                return None
            body = text[i + 1:j]
            start = ulen
            push(body)
            t = {"*": "bold", "_": "italic", "`": "code"}[ch]
            if ulen > start:
                ents.append({"type": t, "offset": start, "length": ulen - start})
            i = j + 1; continue
        if ch == "[":
            m = re.compile(r"\[([^\]]+)\]\(([^)]+)\)").match(text, i)
            if m:
                start = ulen
                push(m.group(1))
                ents.append({"type": "text_link", "offset": start, "length": ulen - start, "url": m.group(2)})
                i = m.end(); continue
        push(ch); i += 1
    return "".join(out), ents


def html_to_entities(text: str):
    """HTML تلگرام (<b> <i> <u> <s> <code> <pre> <a href> <tg-spoiler> <blockquote> <tg-emoji>) -> (plain, entities)."""
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.buf: list[str] = []
            self.ulen = 0
            self.stack: list[tuple[str, int, dict]] = []
            self.ents: list[dict] = []
            self.bad = False

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            t = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "u": "underline",
                 "ins": "underline", "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough",
                 "code": "code", "pre": "pre", "a": "text_link", "tg-spoiler": "spoiler",
                 "blockquote": "blockquote", "tg-emoji": "custom_emoji"}.get(tag)
            if tag == "span" and "spoiler" in (a.get("class") or ""):
                t = "spoiler"
            if t is None:
                self.bad = True
                return
            extra = {}
            if t == "text_link":
                extra["url"] = a.get("href", "")
            if t == "custom_emoji":
                extra["custom_emoji_id"] = a.get("emoji-id", "")
            if t == "blockquote" and "expandable" in a:
                t = "expandable_blockquote"
            self.stack.append((t, self.ulen, extra))

        def handle_endtag(self, tag):
            if not self.stack:
                self.bad = True
                return
            t, start, extra = self.stack.pop()
            if self.ulen > start:
                self.ents.append({"type": t, "offset": start, "length": self.ulen - start, **extra})

        def handle_data(self, data):
            self.buf.append(data)
            self.ulen += u16len(data)

    p = _P()
    try:
        p.feed(text)
        p.close()
    except Exception:
        return None
    if p.bad or p.stack:
        return None
    ents = [e for e in p.ents if e["type"] != "pre" or True]
    ents.sort(key=lambda d: (d["offset"], -d["length"]))
    return "".join(p.buf), ents


def _convert_by_parse_mode(text: str, pm_name: str):
    if pm_name == "markdown":
        return md_to_entities(text)
    if pm_name == "html":
        return html_to_entities(text)
    return None


class Decoration:
    """نگه‌داری وضعیت قبل از تزئین تا در صورت رد شدن توسط تلگرام برگردانده شود."""

    def __init__(self):
        self.saved: dict = {}

    def __bool__(self):
        return bool(self.saved)

    def undo(self, method):
        for k, v in self.saved.items():
            try:
                setattr(method, k, v)
            except Exception:
                pass


def decorate_method(method, bot=None) -> Decoration:
    from aiogram.types import MessageEntity
    dec = Decoration()
    fields = type(method).model_fields
    pm = getattr(method, "parse_mode", None)
    pm_name = _norm_pm(pm)
    if pm is None:
        try:
            pm_name = _norm_pm(bot.default.parse_mode) if bot is not None else ""
        except Exception:
            pm_name = ""
    # MarkdownV2 پشتیبانی نمی‌شود (دست‌نخورده می‌ماند)؛ Markdown و HTML به entity تبدیل می‌شوند.
    convertible = pm_name in ("", "markdown", "html")
    if convertible:
        for tf, ef in _TEXT_FIELDS:
            if tf not in fields or ef not in fields:
                continue
            text = getattr(method, tf, None)
            if not text or not isinstance(text, str):
                continue
            existing = list(getattr(method, ef, None) or [])
            use_text = text
            if pm_name in ("markdown", "html"):
                if existing:
                    continue
                conv = _convert_by_parse_mode(text, pm_name)
                if conv is None:
                    continue
                use_text, conv_ents = conv
                existing = [MessageEntity(**d) for d in conv_ents]
            extra = entities_for(use_text, existing)
            if not extra:
                continue
            dec.saved[tf] = text
            dec.saved[ef] = getattr(method, ef, None)
            if "parse_mode" in fields and "parse_mode" not in dec.saved:
                dec.saved["parse_mode"] = getattr(method, "parse_mode", None)
            merged = list(existing) + [MessageEntity(**d) for d in extra]
            merged.sort(key=lambda e: (e.offset, -e.length))
            setattr(method, tf, use_text)
            setattr(method, ef, merged)
            if "parse_mode" in fields:
                method.parse_mode = None
    if "reply_markup" in fields:
        rm = getattr(method, "reply_markup", None)
        if rm is not None:
            new_rm = process_markup(rm)
            if new_rm is not rm:
                dec.saved["reply_markup"] = rm
                method.reply_markup = new_rm
    return dec


_RETRY_HINTS = ("entit", "emoji", "document_invalid", "button", "icon", "parse")


def should_retry(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in _RETRY_HINTS)
