"""
rich_message.py
===============
پشتیبانی از «Rich Message» تلگرام (Bot API 10.1+: جدول، تیتر، لیست، نقل‌قول، ایموجی پریمیوم و ...).

ادمین می‌تواند محتوای ریچ را به دو روش بدهد:
  1) فوروارد/ارسال خودِ پیام ریچ به ربات  → ساختار بلاک‌ها ذخیره می‌شود.
  2) نوشتن کد: اولین خط   html:   یا   md:   و بعد از آن کد HTML/Markdown ریچ (مثلاً <table>...).

ذخیره‌سازی در جدول settings (بدون نیاز به migration):  rich_cat:<id>  و  rich_guide:<id>
"""
from __future__ import annotations

import json
import logging
import re

import database as db

logger = logging.getLogger(__name__)

_PREFIX = re.compile(r"^\s*(html|md|markdown)\s*:[ \t]*\n?", re.I)


def _key(kind: str, ident) -> str:
    return f"rich_{kind}:{ident}"


def extract(message) -> dict | None:
    """payload ریچ را از پیام ادمین بیرون می‌کشد؛ اگر پیام ریچ نبود None."""
    rm = getattr(message, "rich_message", None)
    if rm is not None and getattr(rm, "blocks", None):
        blocks = [b.model_dump(mode="json", exclude_none=True) for b in rm.blocks]
        payload = {"kind": "blocks", "blocks": blocks}
        if getattr(rm, "is_rtl", None):
            payload["is_rtl"] = True
        return payload
    txt = getattr(message, "text", None) or ""
    m = _PREFIX.match(txt)
    if m:
        body = txt[m.end():].strip()
        if body:
            return {"kind": "html" if m.group(1).lower() == "html" else "markdown", "content": body}
    return None


def to_input(payload: dict, photo=None):
    """photo: یک InputFile (مثل QR) که بالای پیامِ ریچ، داخل خود پیام نمایش داده می‌شود (به‌جای پیام جداگانه)."""
    from aiogram.types import InputRichMessage, InputRichMessageMedia, InputMediaPhoto
    kind = payload.get("kind")
    if kind in ("html", "markdown"):
        content = payload["content"]
        extra = {}
        if photo is not None:
            extra["media"] = [InputRichMessageMedia(id="qrcode", media=InputMediaPhoto(media=photo))]
            if kind == "html":
                content = '<img src="tg://photo?id=qrcode">\n' + content
            else:
                content = "![QR](tg://photo?id=qrcode)\n\n" + content
        return InputRichMessage(**{kind: content}, **extra)
    blocks = list(payload["blocks"])
    if photo is not None:
        blocks = [{"type": "photo", "photo": {"type": "photo", "media": photo}}] + blocks
    data = {"blocks": blocks}
    if payload.get("is_rtl"):
        data["is_rtl"] = True
    return InputRichMessage.model_validate(data)


def save(kind: str, ident, payload: dict) -> None:
    db.set_setting(_key(kind, ident), json.dumps(payload, ensure_ascii=False))


def clear(kind: str, ident) -> None:
    try:
        if db.get_setting(_key(kind, ident), ""):
            db.set_setting(_key(kind, ident), "")
    except Exception:
        logger.debug("rich clear failed", exc_info=True)


def load(kind: str, ident) -> dict | None:
    try:
        raw = db.get_setting(_key(kind, ident), "") or ""
        return json.loads(raw) if raw else None
    except Exception:
        logger.exception("rich load failed (%s:%s)", kind, ident)
        return None


async def send(bot, chat_id, payload: dict, reply_markup=None, photo=None):
    """ارسال پیام ریچ؛ در خطا استثنا می‌دهد (caller تصمیم می‌گیرد fallback کند)."""
    return await bot.send_rich_message(
        chat_id=chat_id, rich_message=to_input(payload, photo=photo), reply_markup=reply_markup,
    )


# ---------------------------------------------------------------------------
# 🧾 ریچ‌مسیج برای «ویرایش متن‌ها» (کلید متن = key در ui_editor) + جایگزینی متغیرها
# ---------------------------------------------------------------------------
_TEXT_CACHE: dict = {}
_TEXT_KEYS: set | None = None


def text_keys() -> set:
    """کلیدهای متنی که ریچ‌مسیج دارند (یک‌بار از DB خوانده و در حافظه نگه داشته می‌شود)."""
    global _TEXT_KEYS
    if _TEXT_KEYS is None:
        keys = set()
        try:
            cur = db.get_connection().cursor()
            cur.execute("SELECT key, value FROM settings WHERE key LIKE 'rich_text:%'")
            for k, v in cur.fetchall():
                if v:
                    keys.add(str(k)[len("rich_text:"):])
        except Exception:
            logger.debug("rich text keys load failed", exc_info=True)
        _TEXT_KEYS = keys
    return _TEXT_KEYS


def load_text(key: str) -> dict | None:
    """payload ریچ ذخیره‌شده برای یک متن ویرایشگر؛ None اگر نباشد (کش حافظه‌ای، فقط یک بار از DB)."""
    if key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    payload = load("text", key)
    _TEXT_CACHE[key] = payload
    return payload


def save_text(key: str, payload: dict) -> None:
    save("text", key, payload)
    _TEXT_CACHE[key] = payload
    text_keys().add(key)


def clear_text(key: str) -> None:
    if _TEXT_CACHE.get(key) is not None or key not in _TEXT_CACHE:
        clear("text", key)
    _TEXT_CACHE[key] = None
    text_keys().discard(key)


def substitute(payload: dict, values: dict | None) -> dict:
    """{متغیر}ها را داخل همه‌ی رشته‌های payload عوض می‌کند (بدون format تا آکولادهای دیگر نشکنند)."""
    if not values:
        return payload
    repl = {"{" + str(k) + "}": str(v) for k, v in values.items() if v is not None}

    def walk(x):
        if isinstance(x, str):
            for k, v in repl.items():
                if k in x:
                    x = x.replace(k, v)
            return x
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            return {a: walk(b) for a, b in x.items()}
        return x

    return walk(payload)
