"""گزارشات پیشرفته: ارسال لاگ‌ها/گزارش‌ها به یک گروه تلگرام تاپیک‌دار (هر موضوع = یک تاپیک).

- هسته‌ی غیر-هندلر: از هر جای کد (ربات، API مینی‌اپ، لایه‌ی دیتابیس) و بدون نیاز به آبجکت bot قابل‌فراخوانی است.
- همه‌ی ارسال‌ها در یک thread پس‌زمینه انجام می‌شوند و هیچ‌وقت exception پرتاب نمی‌کنند.
- هر موضوع با notif_settings (کلید adv_<topic>) قابل خاموش‌کردن است.
- مخصوص ادمین است؛ در ویرایش متن/دکمه نمی‌آید."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from datetime import datetime

import requests

import database as db
import notif_settings

log = logging.getLogger(__name__)

# (key, عنوان تاپیک, شرح)
TOPICS: list[tuple[str, str, str]] = [
    ("payments", "💳 پرداخت‌ها", "رسیدهای دریافتی + وضعیت تأیید/رد/بررسی‌نشده"),
    ("delivery", "📦 تحویل سرویس", "خرید/تمدید/تست/سفارشی/PAYG با مشخصات کامل و دکمه‌ی مدیریت"),
    ("errors", "🚨 خطاها و پنل", "خطاهای ربات، پنل‌ها، سرور و هاست"),
    ("backup", "💾 بک‌آپ", "بک‌آپ خودکار هفتگی دیتابیس"),
    ("autopay", "🌐 پرداخت‌های خودکار", "لاگ پرداخت درگاه‌ها (همه‌ی درگاه‌های فعلی و آینده)"),
    ("collab", "🤝 درخواست‌های همکاری", "با ریپلای روی پیام، پاسخ به کاربر می‌رود"),
    ("tickets", "🎫 تیکت‌ها", "با ریپلای روی پیام، پاسخ به کاربر می‌رود"),
    ("discounts", "🎟 کدهای تخفیف", "چه کسی از چه کدی روی چه بسته‌ای استفاده کرد"),
    ("referrals", "👥 دعوت‌ها", "چه کسی چه کسی را دعوت کرد"),
]
TOPIC_TITLES = {k: t for k, t, _ in TOPICS}
CFG_KEY = "adv_cfg"

_q: "queue.Queue" = queue.Queue(maxsize=2000)
_worker_started = False
_worker_lock = threading.Lock()
_uname_cache: dict[str, tuple[float, str | None]] = {}


# --------------------------------------------------------------------------- config
def get_cfg() -> dict:
    try:
        raw = json.loads(db.get_setting(CFG_KEY, "{}") or "{}")
    except Exception:
        raw = {}
    return {"chat_id": raw.get("chat_id"), "topics": dict(raw.get("topics") or {}), "enabled": bool(raw.get("enabled", True))}


def save_cfg(cfg: dict) -> None:
    db.set_setting(CFG_KEY, json.dumps({"chat_id": cfg.get("chat_id"), "topics": cfg.get("topics") or {}, "enabled": bool(cfg.get("enabled", True))}))


def ready(topic: str | None = None) -> bool:
    c = get_cfg()
    if not (c["chat_id"] and c["enabled"]):
        return False
    return topic is None or bool(c["topics"].get(topic))


def active(topic: str) -> bool:
    return ready(topic) and notif_settings.enabled("adv_" + topic)


# --------------------------------------------------------------------------- telegram
def _token() -> str:
    from config import TOKEN
    return TOKEN


def api(method: str, data: dict | None = None, files: dict | None = None, timeout: int = 20) -> dict | None:
    """فراخوانی خام Bot API؛ خروجی = result یا None."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{_token()}/{method}", data=data or {}, files=files, timeout=timeout)
        js = r.json()
        if js.get("ok"):
            return js.get("result")
        log.warning("adv_reports %s failed: %s", method, str(js.get("description"))[:200])
    except Exception as exc:
        log.warning("adv_reports %s error: %s", method, type(exc).__name__)
    return None


def api_error(method: str, data: dict | None = None) -> tuple[dict | None, str]:
    try:
        r = requests.post(f"https://api.telegram.org/bot{_token()}/{method}", data=data or {}, timeout=20)
        js = r.json()
        if js.get("ok"):
            return js.get("result"), ""
        return None, str(js.get("description") or "")
    except Exception as exc:
        return None, type(exc).__name__


def _username(tid) -> str | None:
    tid = str(tid)
    hit = _uname_cache.get(tid)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    res = api("getChat", {"chat_id": tid}, timeout=8)
    un = (res or {}).get("username")
    _uname_cache[tid] = (time.time(), un)
    return un


def user_line(tid, name: str | None = None) -> str:
    un = _username(tid)
    nm = name or (db.get_user(tid) or {}).get("name") or "-"
    return f"👤 {nm}{(' — @' + un) if un else ''}\n🆔 {tid}"


def _now_text() -> str:
    try:
        from alerts import jalali_now_text
        return jalali_now_text()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def _thread_params(topic: str) -> dict | None:
    c = get_cfg()
    th = c["topics"].get(topic)
    if not (c["chat_id"] and th):
        return None
    return {"chat_id": c["chat_id"], "message_thread_id": th}


def send(topic: str, text: str, buttons: list | None = None, parse_mode: str | None = None) -> int | None:
    """ارسال هم‌زمان یک پیام متنی به تاپیک؛ خروجی message_id. (فقط از داخل worker/thread صدا بزنید)"""
    p = _thread_params(topic)
    if not p:
        return None
    p["text"] = text[:4000]
    if parse_mode:
        p["parse_mode"] = parse_mode
    if buttons:
        p["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    res = api("sendMessage", p)
    return (res or {}).get("message_id")


def send_media(topic: str, file_id: str, file_type: str | None, caption: str) -> int | None:
    p = _thread_params(topic)
    if not p:
        return None
    p["caption"] = caption[:1000]
    if file_type == "photo":
        p["photo"] = file_id
        res = api("sendPhoto", p)
    else:
        p["document"] = file_id
        res = api("sendDocument", p)
    return (res or {}).get("message_id")


def send_file(topic: str, path: str, filename: str, caption: str = "") -> int | None:
    p = _thread_params(topic)
    if not p:
        return None
    p["caption"] = caption[:1000]
    try:
        with open(path, "rb") as fh:
            res = api("sendDocument", p, files={"document": (filename, fh)}, timeout=120)
        return (res or {}).get("message_id")
    except Exception:
        log.exception("adv_reports send_file failed")
        return None


# --------------------------------------------------------------------------- storage of posted messages
def _ensure_table() -> None:
    with db.transaction() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS adv_msgs (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL,
                   chat_id INTEGER, message_id INTEGER, is_media INTEGER DEFAULT 0,
                   base_text TEXT, extra TEXT, created_at TEXT,
                   UNIQUE(kind, ref))"""
        )


_table_ready = False


def _tbl():
    global _table_ready
    if not _table_ready:
        _ensure_table()
        _table_ready = True


def _remember(kind: str, ref, message_id: int | None, is_media: bool, base_text: str, extra: dict | None = None) -> None:
    if not message_id:
        return
    _tbl()
    c = get_cfg()
    with db.transaction() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO adv_msgs (kind, ref, chat_id, message_id, is_media, base_text, extra, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, str(ref), c["chat_id"], int(message_id), 1 if is_media else 0, base_text, json.dumps(extra or {}, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def _lookup(kind: str, ref) -> dict | None:
    _tbl()
    cur = db.get_connection().cursor()
    cur.execute("SELECT * FROM adv_msgs WHERE kind = ? AND ref = ?", (kind, str(ref)))
    return db._fetchone(cur)


def lookup_by_message(chat_id, message_id) -> dict | None:
    """برای ریپلای ادمین روی پیام تیکت/همکاری."""
    _tbl()
    cur = db.get_connection().cursor()
    cur.execute("SELECT * FROM adv_msgs WHERE chat_id = ? AND message_id = ?", (int(chat_id), int(message_id)))
    d = db._fetchone(cur)
    if d is None:
        return None
    try:
        d["extra"] = json.loads(d.get("extra") or "{}")
    except Exception:
        d["extra"] = {}
    return d


# --------------------------------------------------------------------------- worker
def _run_worker():
    while True:
        fn = _q.get()
        try:
            fn()
        except Exception:
            log.exception("adv_reports job failed")


def submit(fn, delay: float = 0.0) -> None:
    """کار را در thread پس‌زمینه اجرا می‌کند (با تأخیر اختیاری)."""
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_run_worker, name="adv-reports", daemon=True).start()
            _worker_started = True
    if delay > 0:
        threading.Timer(delay, lambda: _q.put_nowait(fn) if not _q.full() else None).start()
    else:
        try:
            _q.put_nowait(fn)
        except queue.Full:
            pass


def _guard(topic: str) -> bool:
    try:
        return active(topic)
    except Exception:
        return False


# --------------------------------------------------------------------------- 1) payments / receipts
_RECEIPT_KIND = {"charge": "شارژ کیف پول", "plan_card": "خرید کارت‌به‌کارت", "plan_crypto": "پرداخت ارزی", "custom": "سرویس سفارشی", "renew": "تمدید"}
_STATUS_LINE = {"pending": "⏳ وضعیت: بررسی نشده", "approved": "✅ وضعیت: تأیید شد", "rejected": "❌ وضعیت: رد شد"}


def _receipt_text(row: dict) -> str:
    body = str(row.get("details") or "").strip()
    if not body:
        parts = [
            f"🧾 رسید {_RECEIPT_KIND.get(row.get('kind'), row.get('kind') or '')}",
            user_line(row.get("telegram_id")),
            f"📦 {row.get('label') or '-'}",
            f"💰 {int(row.get('amount') or 0):,} تومان",
        ]
        if row.get("discount_code"):
            parts.append(f"🎟 کد تخفیف: {row['discount_code']}")
        if row.get("service_name"):
            parts.append(f"🔤 نام سرویس: {row['service_name']}")
        if row.get("note"):
            parts.append(f"📝 {str(row['note'])[:300]}")
        body = "\n".join(parts)
    else:
        body = "🧾 رسید دریافت‌شده\n" + user_line(row.get("telegram_id")) + "\n\n" + body
    return body[:850]


def _post_receipt(rid: int, status: str = "pending") -> None:
    if not _guard("payments"):
        return
    if _lookup("receipt", rid):
        return
    row = db.get_pending_receipt_by_id(rid)
    if not row:
        return
    base = _receipt_text(row)
    text = base + "\n\n" + _STATUS_LINE[status]
    media = row.get("file_id")
    mid = send_media("payments", media, row.get("file_type"), text) if media else send("payments", text)
    _remember("receipt", rid, mid, bool(media), base)


def receipt_created(rid: int) -> None:
    """بعد از ساخت رسید معلق (چند ثانیه صبر می‌کنیم تا عکس/توضیح هم ذخیره شود)."""
    if active("payments"):
        submit(lambda: _post_receipt(int(rid)), delay=4.0)


def receipt_media_set(rid: int) -> None:
    receipt_created(rid)


def _edit_status(kind: str, ref, status: str, note: str = "") -> bool:
    rec = _lookup(kind, ref)
    if not rec:
        return False
    text = (rec.get("base_text") or "") + "\n\n" + _STATUS_LINE[status] + (f" ({note})" if note else "") + f"\n⏰ {_now_text()}"
    if rec.get("is_media"):
        res = api("editMessageCaption", {"chat_id": rec["chat_id"], "message_id": rec["message_id"], "caption": text[:1000]})
    else:
        res = api("editMessageText", {"chat_id": rec["chat_id"], "message_id": rec["message_id"], "text": text[:4000]})
    return res is not None


def receipt_decided(rid: int, outcome: str) -> None:
    """outcome: approved | rejected"""
    if not active("payments") or outcome not in ("approved", "rejected"):
        return

    def job():
        if not _guard("payments"):
            return
        if not _lookup("receipt", rid):
            _post_receipt(int(rid), status=outcome)
            rec = _lookup("receipt", rid)
            if rec:
                _edit_status("receipt", rid, outcome)
            return
        _edit_status("receipt", rid, outcome)
    submit(job, delay=1.0)


def custom_receipt_set(oid: int) -> None:
    if not active("payments"):
        return

    def job():
        if not _guard("payments") or _lookup("corder", oid):
            return
        o = db.get_custom_order(int(oid))
        if not o:
            return
        u = db.get_user_by_id(int(o["user_id"])) or {}
        base = (
            f"🧾 رسید سفارش ({'تمدید' if o.get('order_type') == 'renew' else 'سرویس سفارشی'})\n"
            + user_line(u.get("telegram_id"), u.get("name"))
            + f"\n📦 {o.get('volume_gb')} گیگ | {o.get('days')} روز" + (f"\n🔤 {o.get('custom_name')}" if o.get("custom_name") else "")
            + f"\n💰 {int(o.get('price') or 0):,} تومان\n🔢 سفارش #{oid}"
        )
        fid = o.get("receipt_file_id")
        text = base + "\n\n" + _STATUS_LINE["pending"]
        mid = send_media("payments", fid, "photo", text) if fid else send("payments", text)
        _remember("corder", oid, mid, bool(fid), base)
    submit(job, delay=3.0)


def custom_decided(oid: int, outcome: str) -> None:
    if not active("payments") or outcome not in ("approved", "rejected"):
        return
    submit(lambda: _edit_status("corder", oid, outcome) if _guard("payments") else None, delay=1.0)


# --------------------------------------------------------------------------- 2) delivery
def _find_config(service_name: str | None, telegram_id) -> dict | None:
    try:
        cur = db.get_connection().cursor()
        if service_name:
            cur.execute("SELECT * FROM configs WHERE deleted = 0 AND (lower(COALESCE(service_name,'')) = lower(?) OR lower(COALESCE(service_id,'')) = lower(?)) ORDER BY id DESC LIMIT 1", (service_name, service_name))
            r = db._fetchone(cur)
            if r:
                return r
    except Exception:
        pass
    return None


def delivery_report(*, order_label, payment_method, user: dict, service_name, package_text, amount_text, panel_name=None, expiry_text=None) -> None:
    if not active("delivery"):
        return

    def job():
        if not _guard("delivery"):
            return
        tid = str(user.get("telegram_id") or "")
        label = str(order_label or "")
        kind = "تست" if "تست" in label else "تمدید" if "تمدید" in label else "سفارشی" if "سفارشی" in label else "PAYG" if "PAYG" in label.upper() else "خرید"
        cfg = _find_config(service_name, tid)
        lines = [
            f"📦 {kind} — {payment_method or '-'}",
            ("✅ تحویل خودکار از پنل " + panel_name) if panel_name else "✅ تحویل/تمدید انجام شد",
            "",
            user_line(tid, user.get("name")),
            f"🛍 بسته: {(cfg or {}).get('plan') or package_text or '-'}",
            f"🔑 یوزرنیم سرویس: {service_name or '-'}",
            f"📦 مشخصات: {package_text or '-'}",
            f"💰 {amount_text or '-'}",
        ]
        if expiry_text:
            lines.append(f"⏳ انقضا: {expiry_text}")
        lines.append(f"⏰ {_now_text()}")
        buttons = None
        try:
            import bot_info
            bu = (bot_info.get("bot_username") or "").lstrip("@")
            if bu and tid:
                suffix = f"_{cfg['id']}" if cfg else ""
                buttons = [[{"text": "👤 مدیریت کاربر/سرویس در ربات", "url": f"https://t.me/{bu}?start=adm_{tid}{suffix}"}]]
        except Exception:
            pass
        send("delivery", "\n".join(lines), buttons=buttons)
    submit(job)


# --------------------------------------------------------------------------- 5) autopay
def payment_event(payment_id: int, event: str) -> None:
    """event: created | paid | status:<x>"""
    if not active("autopay"):
        return

    def job():
        if not _guard("autopay"):
            return
        p = db.get_online_payment(int(payment_id))
        if not p:
            return
        try:
            import online_gateways as og
            gw = og.admin_label(p)
        except Exception:
            gw = str(p.get("gateway") or "uniquepay")
        titles = {"created": "🆕 فاکتور ساخته شد", "paid": "✅ پرداخت تأیید شد"}
        title = titles.get(event) or ("ℹ️ تغییر وضعیت: " + event.split(":", 1)[-1])
        kind = "شارژ کیف پول" if p.get("kind") == "wallet_charge" else (p.get("plan_name") or "خرید")
        lines = [
            f"{title} — {gw}",
            user_line(p.get("telegram_id")),
            f"📦 {kind}",
            f"💰 {int(p.get('price') or 0):,} تومان",
            f"🔢 فاکتور #{p['id']} | ref: {p.get('ref_id') or '-'}",
        ]
        if p.get("order_id"):
            lines.append(f"🧾 سفارش #{p['order_id']}")
        lines.append(f"⏰ {_now_text()}")
        send("autopay", "\n".join(lines))
    submit(job, delay=0.5)


# --------------------------------------------------------------------------- 6,7) tickets & collab
def ticket_message(ticket_id: int, text: str, user_telegram_id=None) -> None:
    if not active("tickets"):
        return

    def job():
        if not _guard("tickets"):
            return
        t = db.get_ticket(int(ticket_id)) or {}
        u = db.get_user_by_id(int(t["user_id"])) if t.get("user_id") else None
        tid = user_telegram_id or (u or {}).get("telegram_id")
        body = f"🎫 تیکت #{ticket_id}\n{user_line(tid, (u or {}).get('name'))}\n\n💬 {text[:1500]}\n\n↩️ برای پاسخ، روی همین پیام «ریپلای» بزنید."
        mid = send("tickets", body)
        _remember("ticket_msg", f"{ticket_id}:{mid}", mid, False, body, {"type": "ticket", "ticket_id": int(ticket_id), "user_tid": str(tid)})
    submit(job)


def collab_request(user_telegram_id, name: str, text: str) -> None:
    if not active("collab"):
        return

    def job():
        if not _guard("collab"):
            return
        body = f"🤝 درخواست همکاری/نمایندگی\n{user_line(user_telegram_id, name)}\n\n💬 {text[:1500]}\n\n↩️ برای پاسخ، روی همین پیام «ریپلای» بزنید."
        mid = send("collab", body)
        _remember("collab_msg", f"{user_telegram_id}:{mid}", mid, False, body, {"type": "collab", "user_tid": str(user_telegram_id)})
    submit(job)


# --------------------------------------------------------------------------- 8) discounts
def discount_used(code: str, user_id) -> None:
    if not active("discounts"):
        return

    def job():
        if not _guard("discounts"):
            return
        u = db.get_user_by_id(int(user_id)) if user_id else None
        order = None
        try:
            if u:
                cur = db.get_connection().cursor()
                cur.execute("SELECT plan_name, price, order_type, created_at FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", (int(user_id),))
                order = db._fetchone(cur)
        except Exception:
            order = None
        lines = ["🎟 استفاده از کد تخفیف", f"🏷 کد: {code}", user_line((u or {}).get("telegram_id"), (u or {}).get("name"))]
        if order:
            lines.append(f"📦 بسته: {order.get('plan_name')} ({order.get('order_type')})")
            lines.append(f"💰 مبلغ خرید: {int(order.get('price') or 0):,} تومان")
        lines.append(f"⏰ {_now_text()}")
        send("discounts", "\n".join(lines))
    submit(job, delay=3.0)


# --------------------------------------------------------------------------- 9) referrals
def referral_event(referrer_id, invited_id, event: str = "joined", detail: str | None = None, technical: str | None = None) -> None:
    if not active("referrals"):
        return

    def job():
        if not _guard("referrals"):
            return
        a = db.get_user_by_id(int(referrer_id)) if referrer_id else None
        b = db.get_user_by_id(int(invited_id)) if invited_id else None
        title = {"joined": "👥 دعوت جدید", "left": "🚪 دعوت‌شده از کانال اجباری خارج شد",
                 "rejoined": "🔁 دعوت‌شده دوباره عضو کانال‌ها شد"}.get(event, "🎁 دعوت موفق (پاداش آزاد شد)")
        extra = ""
        if detail:
            extra += f"\n\n📨 اعلان ارسال‌شده به دعوت‌کننده:\n{detail}"
        if technical and technical != detail:
            extra += f"\n\n🔎 جزئیات: {technical}"
        send("referrals", f"{title}\n\n🔹 دعوت‌کننده:\n{user_line((a or {}).get('telegram_id'), (a or {}).get('name'))}\n\n🔸 دعوت‌شده:\n{user_line((b or {}).get('telegram_id'), (b or {}).get('name'))}{extra}\n⏰ {_now_text()}")
    submit(job, delay=1.0)


def referral_reward_notice(referrer_id, invited_id, reward) -> None:
    """پیام مستقیم به معرف وقتی پاداش دعوتش آزاد شد (قالب «ویرایش متن»، خاموش/روشن: تنظیمات اعلان ← دعوت موفق)."""
    def job():
        try:
            import notif_settings, ui_editor, json
            if not notif_settings.enabled("referral_reward"):
                return
            a = db.get_user_by_id(int(referrer_id)) if referrer_id else None
            b = db.get_user_by_id(int(invited_id)) if invited_id else None
            if not a:
                return
            text, raw = ui_editor.render_template("notif_referral_reward", {
                "name": (b or {}).get("name") or "—", "user_id": (b or {}).get("telegram_id") or "—",
                "reward": f"{int(reward or 0):,}"}, fallback=ui_editor.SCREENS["notif_referral_reward"]["default"])
            data = {"chat_id": str(a["telegram_id"]), "text": text}
            if raw:
                data["entities"] = json.dumps([{k: v for k, v in e.items() if k in ("type", "offset", "length", "custom_emoji_id")} for e in raw])
            api("sendMessage", data)
        except Exception:
            log.warning("referral_reward_notice failed", exc_info=True)
    submit(job, delay=2.0)


# --------------------------------------------------------------------------- 3) errors (logging handler)
class _ErrorHandler(logging.Handler):
    _recent: dict[str, float] = {}
    _window: list[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR or record.name.startswith(("urllib3", "requests", "adv_reports")):
                return
            if not ready("errors") or not notif_settings.enabled("adv_errors"):
                return
            msg = record.getMessage()
            tb = ""
            if record.exc_info:
                import traceback
                tb = "".join(traceback.format_exception(*record.exc_info))[-1200:]
            key = hashlib.md5((record.name + msg[:200] + tb[-200:]).encode("utf-8", "ignore")).hexdigest()
            now = time.time()
            if now - self._recent.get(key, 0) < 300:
                return
            self._recent[key] = now
            self._window[:] = [t for t in self._window if now - t < 60]
            if len(self._window) >= 15:
                return
            self._window.append(now)
            text = f"🚨 خطا — {record.name}\n⏰ {_now_text()}\n\n{msg[:900]}" + (f"\n\n{tb}" if tb else "")
            submit(lambda: send("errors", text))
        except Exception:
            pass


_err_handler_installed = False


def install_error_handler() -> None:
    global _err_handler_installed
    if _err_handler_installed:
        return
    logging.getLogger().addHandler(_ErrorHandler(level=logging.ERROR))
    _err_handler_installed = True


# --------------------------------------------------------------------------- 4) backup
def make_backup_file() -> tuple[str, str] | None:
    """(path, filename) برای ارسال."""
    import os
    import shutil
    import sqlite3
    import tempfile
    from config import DATABASE_PATH
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    try:
        if getattr(db, "USE_TURSO", False):
            path = os.path.join(tempfile.gettempdir(), f"backup_{stamp}.json")
            db.export_backup_json(path)
            return path, f"backup_{stamp}.json"
        path = os.path.join(tempfile.gettempdir(), f"backup_{stamp}.db")
        src = sqlite3.connect(DATABASE_PATH)
        try:
            dst = sqlite3.connect(path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return path, f"backup_{stamp}.db"
    except Exception:
        log.exception("adv_reports backup failed")
        try:
            path = os.path.join(tempfile.gettempdir(), f"backup_{stamp}.db")
            shutil.copyfile(DATABASE_PATH, path)
            return path, f"backup_{stamp}.db"
        except Exception:
            return None


def send_backup(manual: bool = False) -> bool:
    """بک‌آپ را به تاپیک بک‌آپ می‌فرستد (sync)."""
    if not (ready("backup") and (manual or notif_settings.enabled("adv_backup"))):
        return False
    res = make_backup_file()
    if not res:
        send("backup", "❌ ساخت بک‌آپ ناموفق بود.")
        return False
    path, name = res
    mid = send_file("backup", path, name, f"💾 بک‌آپ {'دستی' if manual else 'خودکار هفتگی'} دیتابیس\n⏰ {_now_text()}")
    try:
        import os
        os.remove(path)
    except Exception:
        pass
    if mid:
        db.set_setting("adv_last_backup", str(int(time.time())))
    return bool(mid)


def _admin_chat() -> str:
    from config import ADMIN_ID
    return str(ADMIN_ID)


def backup_everywhere(manual: bool = True) -> dict:
    """بک‌آپ همین لحظه را برای ادمین اصلی (در ربات) و تاپیک «بک‌آپ» گروه می‌فرستد (sync؛ از thread صدا بزنید).
    خروجی: {"admin": bool, "topic": bool|None}."""
    res = make_backup_file()
    out = {"admin": False, "topic": None}
    if not res:
        return out
    path, name = res
    caption = f"💾 بک‌آپ {'دستی' if manual else 'خودکار'} دیتابیس\n⏰ {_now_text()}"
    try:
        with open(path, "rb") as fh:
            r = api("sendDocument", {"chat_id": _admin_chat(), "caption": caption}, files={"document": (name, fh)}, timeout=120)
        out["admin"] = r is not None
    except Exception:
        log.exception("backup to admin failed")
    if ready("backup") and notif_settings.enabled("adv_backup"):
        mid = send_file("backup", path, name, caption)
        out["topic"] = bool(mid)
    try:
        import os
        os.remove(path)
    except Exception:
        pass
    if out["admin"] or out["topic"]:
        db.set_setting("adv_last_backup", str(int(time.time())))
    return out


def backup_due(interval_days: float = 7.0) -> bool:
    try:
        last = int(db.get_setting("adv_last_backup", "0") or 0)
    except Exception:
        last = 0
    return time.time() - last >= interval_days * 86400
