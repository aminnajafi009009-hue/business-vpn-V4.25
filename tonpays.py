"""
tonpays.py
کلاینت درگاه TonPays (تون‌پیز) — API استاندارد، مطابق مستندات رسمی (https://doc.tonpays.online).

  • احراز هویت: هدر X-API-Key
  • ساخت فاکتور:    POST /api/v1/invoices/create   {amount(تومان), order_id(≤۲۰ کاراکتر), callback_url, buyer_chat_id}
  • استعلام:         GET  /api/v1/invoices/check/{invoice_id}
  • لغو (فقط pending): POST /api/v1/invoices/{invoice_id}/cancel
  • ثبت کارت عودت مبلغ بیشتر: POST /api/v1/invoices/{invoice_id}/refund {invoice_id, card_number, card_holder}
  • حالت «تلگرام» (فاکتور کاستوم، کلید جدا = custom_tg_key):
        POST /api/custom/v1/invoices/telegram/create  → کارت مقصد (card_number/card_name) داخل ربات
        GET  /api/custom/v1/invoices/check/{id} | POST …/{id}/cancel | …/{id}/receipt | …/{id}/refund
        POST /api/custom/v1/invoices/{id}/change-card   (تعویض کارت؛ کول‌داون ۶۰ ثانیه)
    فاکتورهایی که با کلید کاستوم ساخته شده‌اند با همان کلید و مسیر کاستوم استعلام/لغو/رسید می‌شوند (kind="tg").
  • ارسال رسید مابقی (مبلغ کمتر): POST /api/v1/invoices/{invoice_id}/receipt  (multipart file)
  • وب‌هوک: هدر اختیاری X-TonPays-Signature = "t=<unix>,v1=<hmac_sha256_hex>" روی «timestamp.raw_body»
           (فقط وقتی «سکرت امضا» در پروفایل ساخته شده باشد ارسال می‌شود)
  • وضعیت‌ها: pending | processing | action_required_less | action_required_more | completed | rejected | expired | canceled
    فقط completed با paid=true یعنی پرداخت‌شده. مبلغ نهایی واریزی (final_amount) با مبلغ فاکتور (request_amount)
    فرق دارد (کسر/اعشار یکتا)؛ شارژ/تحویل همیشه به‌اندازه‌ی request_amount (مبلغ فاکتور ما) است.
  • خطاها: {"detail": {"code": "...", "message": "..."}}
  • محدودیت: ساخت/استعلام ۶۰ در دقیقه، ۱۲۰ درخواست از هر آی‌پی در دقیقه.

این ماژول sync است (requests)؛ نسخه‌های async در online_gateways.py با asyncio.to_thread.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time

import requests

import database as db
from payment_manager_v3 import TonPaysConfig

logger = logging.getLogger(__name__)

SETTING_KEY = "pm_tonpays"
DEFAULT_BASE_URL = "https://tonpays.online"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25
SIGNATURE_TOLERANCE_SECONDS = 600
_USER_AGENT = "VPNShopBot-TonPays/1.0"
_INVOICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
VALID_LINK_MODES = ("web", "telegram")

_CODE_TEXT = {
    "MISSING_API_KEY": "هدر API Key ارسال نشد",
    "INVALID_API_KEY": "API Key نامعتبر است",
    "INACTIVE_API_KEY": "API Key غیرفعال است",
    "WRONG_API_KEY_KIND": "نوع کلید اشتباه است (باید کلید «استاندارد» باشد)",
    "ACCOUNT_NOT_VERIFIED": "حساب فروشگاه در TonPays هنوز تأیید نشده است",
    "ACCOUNT_SUSPENDED": "حساب فروشگاه در TonPays تعلیق شده است",
    "STORE_INACTIVE": "فروشگاه در TonPays غیرفعال است",
    "DUPLICATE_ORDER_ID": "شناسه سفارش تکراری است",
    "AMOUNT_TOO_LOW": "مبلغ کمتر از حداقل مجاز TonPays است",
    "AMOUNT_TOO_HIGH": "مبلغ بیشتر از سقف مجاز TonPays است",
    "INVALID_BUYER_CHAT_ID": "شناسه تلگرام خریدار نامعتبر است",
    "INVALID_CALLBACK_URL": "آدرس وب‌هوک نامعتبر است (باید HTTPS عمومی باشد)",
    "RATE_LIMIT_EXCEEDED": "سقف تعداد درخواست؛ کمی بعد دوباره تلاش کنید",
    "INVOICE_NOT_FOUND": "فاکتور پیدا نشد",
    "ACCESS_DENIED": "دسترسی به این فاکتور ندارید",
    "BUYER_HAS_PENDING_INVOICE": "خریدار فاکتور در انتظار پرداخت دارد",
    "BUYER_HAS_PROCESSING_INVOICE": "خریدار فاکتور در انتظار تأیید دارد",
    "BUYER_HAS_NEED_ACTION_LESS": "خریدار فاکتور «مبلغ کمتر» باز دارد",
    "BUYER_HAS_NEED_ACTION_MORE": "خریدار فاکتور «مبلغ بیشتر» باز دارد",
    "BUYER_INVOICE_BUSY": "ساخت فاکتور برای این خریدار در جریان است",
    "CANCEL_NOT_ALLOWED_PROCESSING": "فاکتور در انتظار تأیید لغو نمی‌شود",
    "CANCEL_NOT_ALLOWED_NEED_ACTION_LESS": "فاکتور مبلغ کمتر لغو نمی‌شود",
    "CANCEL_NOT_ALLOWED_NEED_ACTION_MORE": "فاکتور مبلغ بیشتر لغو نمی‌شود",
    "INVOICE_ALREADY_CANCELED": "فاکتور قبلاً لغو شده",
    "INVOICE_EXPIRED": "فاکتور منقضی شده",
}


# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------
def cfg() -> TonPaysConfig:
    try:
        d = json.loads(db.get_setting(SETTING_KEY, "{}") or "{}")
        if not isinstance(d, dict):
            d = {}
    except Exception:
        logger.exception("خواندن تنظیمات pm_tonpays ناموفق بود")
        d = {}
    return TonPaysConfig.from_dict(d)


def api_key() -> str:
    return (cfg().api_key or "").strip()


def webhook_secret() -> str:
    return (cfg().webhook_secret or "").strip()


def custom_tg_key() -> str:
    return (getattr(cfg(), "custom_tg_key", "") or "").strip()


# نوع فاکتور: "tg" = فاکتور کاستوم تلگرام (کلید و مسیر /api/custom)، "std" = API استاندارد
_STD_PREFIX = "/api/v1/invoices"
_TG_PREFIX = "/api/custom/v1/invoices"


def kind_of(payment: dict | None) -> str:
    """نوع فاکتور یک پرداخت را از gw_data می‌خواند؛ فاکتورهای قدیمی بدون کلید «kind» همان استاندارد هستند."""
    return "tg" if db.gw_data_of(payment).get("kind") == "tg" else "std"


def _prefix_and_key(kind: str) -> tuple[str, str]:
    if kind == "tg":
        return _TG_PREFIX, custom_tg_key()
    return _STD_PREFIX, api_key()


def base_url() -> str:
    return ((cfg().base_url or "").strip() or DEFAULT_BASE_URL).rstrip("/")


def enabled() -> bool:
    c = cfg()
    return bool(c.enabled and (c.api_key or "").strip())


def min_amount() -> int:
    try:
        return max(int(cfg().min_amount or 0), 0)
    except (TypeError, ValueError):
        return 0


def amount_ok(amount: int) -> bool:
    try:
        a = int(amount)
    except (TypeError, ValueError):
        return False
    return a > 0 and a >= min_amount()


def _headers(key: str | None = None) -> dict:
    return {"X-API-Key": (api_key() if key is None else key), "Accept": "application/json", "User-Agent": _USER_AGENT}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _err_obj(body) -> dict:
    """خطا ممکن است در «detail» یا «error» باشد: {"detail":{code,message}} | {"success":false,"error":{code,message}}"""
    if isinstance(body, dict):
        for k in ("detail", "error"):
            d = body.get(k)
            if isinstance(d, dict) and (d.get("code") or d.get("message")):
                return d
    return {}


def error_code(body) -> str:
    d = _err_obj(body)
    return str(d.get("code") or "") if d else ""


def _error_text(status: int | None, body) -> str:
    code = error_code(body)
    msg = ""
    msg = str(_err_obj(body).get("message") or "")[:200]
    base = _CODE_TEXT.get(code) or (f"خطای HTTP {status}" if status else "خطا")
    extra = f" ({code})" if code and code not in _CODE_TEXT else ""
    return base + extra + (f" — {msg}" if msg and not _CODE_TEXT.get(code) else "")


def _request(method: str, path: str, payload: dict | None = None, files=None, key: str | None = None) -> dict:
    """خروجی: {"ok","status","data","error","code"} — هیچ استثنایی نشت نمی‌کند و کلید API در لاگ/خطا نمی‌آید."""
    use_key = api_key() if key is None else key
    if not use_key:
        return {"ok": False, "status": None, "data": None,
                "error": ("کلید تلگرام کاستوم تون‌پیز تنظیم نشده است" if key is not None else "API Key تون‌پیز تنظیم نشده است"),
                "code": ""}
    if not str(use_key).isascii() or any(c.isspace() for c in str(use_key)):
        return {"ok": False, "status": None, "data": None,
                "error": "کلید API نامعتبر است (فقط حروف انگلیسی/عدد، بدون فاصله و متن فارسی) — کلید را دوباره از پنل تون‌پیز کپی و ثبت کنید",
                "code": ""}
    try:
        kw: dict = {"headers": _headers(use_key), "timeout": (CONNECT_TIMEOUT, READ_TIMEOUT), "allow_redirects": False}
        if files is not None:
            kw["files"] = files
        elif payload is not None:
            kw["json"] = payload
        resp = requests.request(method, f"{base_url()}{path}", **kw)
        if method.upper() == "GET" and resp.status_code >= 500:
            # خطای موقت سمت سرور TonPays (۵۰۲/۵۰۳…): فقط برای GET (بی‌خطر) یک‌بار دوباره تلاش می‌شود
            import time as _t
            _t.sleep(1.5)
            resp = requests.request(method, f"{base_url()}{path}", **kw)
    except requests.RequestException as exc:
        logger.warning("TonPays %s %s خطای شبکه: %s", method, path, type(exc).__name__)
        return {"ok": False, "status": None, "data": None, "error": f"خطای شبکه ({type(exc).__name__})", "code": ""}
    try:
        body = resp.json()
    except ValueError:
        body = None
        # ۵xx = خطای موقتِ خود TonPays (نه باگ ربات)؛ ERROR نشود تا هشدار الکی به ادمین نرود
        (logger.warning if resp.status_code >= 500 else logger.error)("TonPays %s %s پاسخ غیر-JSON | status=%s", method, path, resp.status_code)
    if 200 <= resp.status_code < 300 and isinstance(body, dict):
        return {"ok": True, "status": resp.status_code, "data": body, "error": None, "code": ""}
    if 300 <= resp.status_code < 400:
        return {"ok": False, "status": resp.status_code, "data": None,
                "error": "ریدایرکت از سمت TonPays (Base URL را بررسی کنید)", "code": ""}
    return {"ok": False, "status": resp.status_code, "data": body if isinstance(body, dict) else None,
            "error": _error_text(resp.status_code, body), "code": error_code(body)}


# ---------------------------------------------------------------------------
# عملیات
# ---------------------------------------------------------------------------
def create_invoice(amount_toman: int, order_id: str, callback_url: str | None = None,
                   buyer_chat_id: int | None = None) -> dict:
    """ساخت فاکتور. order_id حداکثر ۲۰ کاراکتر. بدون retry کورکورانه (retry هوشمند در online_gateways)."""
    payload: dict = {"amount": int(amount_toman), "order_id": str(order_id)[:20]}
    if callback_url:
        payload["callback_url"] = callback_url
    if buyer_chat_id and cfg().send_buyer:
        try:
            payload["buyer_chat_id"] = int(buyer_chat_id)
        except (TypeError, ValueError):
            pass
    res = _request("POST", "/api/v1/invoices/create", payload)
    if res["ok"]:
        iid = str((res["data"] or {}).get("invoice_id") or "").strip()
        if not _INVOICE_ID_RE.match(iid):
            return {"ok": False, "status": res["status"], "data": res["data"],
                    "error": "پاسخ نامعتبر از TonPays (invoice_id ندارد)", "code": ""}
    return res


def create_tg_invoice(amount_toman: int, order_id: str, callback_url: str | None = None,
                      buyer_chat_id: int | None = None) -> dict:
    """ساخت فاکتور کاستوم تلگرام (حالت «تلگرام»): پاسخ شامل card_number/card_name و final_amount است.
    buyer_chat_id در این مسیر الزامی است."""
    try:
        buyer = int(buyer_chat_id or 0)
    except (TypeError, ValueError):
        buyer = 0
    if buyer <= 0:
        return {"ok": False, "status": None, "data": None,
                "error": "شناسه تلگرام خریدار برای حالت تلگرام الزامی است", "code": ""}
    payload: dict = {"amount": int(amount_toman), "order_id": str(order_id)[:20], "buyer_chat_id": buyer}
    if callback_url:
        payload["callback_url"] = callback_url
    res = _request("POST", f"{_TG_PREFIX}/telegram/create", payload, key=custom_tg_key())
    if res["ok"]:
        iid = str((res["data"] or {}).get("invoice_id") or "").strip()
        if not _INVOICE_ID_RE.match(iid):
            return {"ok": False, "status": res["status"], "data": res["data"],
                    "error": "پاسخ نامعتبر از TonPays (invoice_id ندارد)", "code": ""}
    return res


_KEY_OK_CODES = {"AMOUNT_TOO_LOW", "AMOUNT_TOO_HIGH", "DUPLICATE_ORDER_ID", "INVALID_CALLBACK_URL",
                 "INVALID_BUYER_CHAT_ID", "BUYER_HAS_PENDING_INVOICE", "BUYER_HAS_PROCESSING_INVOICE",
                 "BUYER_HAS_NEED_ACTION_LESS", "BUYER_HAS_NEED_ACTION_MORE", "BUYER_INVOICE_BUSY",
                 "INVOICE_NOT_FOUND"}


def _body_hint(res: dict) -> str:
    d = res.get("data")
    if isinstance(d, dict) and d:
        det = _err_obj(d) or d.get("detail", d)
        return str(det)[:160]
    return ""


def test_connection() -> tuple[bool, str]:
    ok, msg = _test_std_connection()
    if not custom_tg_key():
        return ok, msg
    ok_tg, msg_tg = _test_tg_connection()
    return ok and ok_tg, msg + "\n\n" + msg_tg


def _test_tg_connection() -> tuple[bool, str]:
    """تست کلید «تلگرام کاستوم»: مبلغ ۱ تومان؛ خطای AMOUNT_TOO_LOW یعنی کلید و مسیر درست است."""
    import secrets as _sec
    oid = "CT" + _sec.token_hex(6)
    res = _request("POST", f"{_TG_PREFIX}/telegram/create", {"amount": 1, "order_id": oid, "buyer_chat_id": 1},
                   key=custom_tg_key())
    if res["ok"]:
        iid = str((res.get("data") or {}).get("invoice_id") or "")
        if iid:
            try:
                cancel_invoice(iid, "tg")
            except Exception:
                pass
        return True, "کلید تلگرام کاستوم معتبر است."
    if (res.get("code") or "") in _KEY_OK_CODES:
        return True, "کلید تلگرام کاستوم معتبر است."
    return False, "کلید تلگرام کاستوم: " + (res.get("error") or "خطا") + (f" (HTTP {res.get('status')})" if res.get("status") else "")


def _test_std_connection() -> tuple[bool, str]:
    """تست اتصال: یک «ساخت فاکتور» با مبلغ نامعتبر (۱ تومان) می‌فرستد؛ سرور قبل از ساخت چیزی، کلید و مسیر را
    بررسی می‌کند (AMOUNT_TOO_LOW = کلید و مسیر درست). اگر به‌هر دلیل فاکتور ساخته شد، فوراً لغو می‌شود.
    مسیر قبلی (استعلام یک فاکتور ساختگی) روی ۴۰۴ بدون کد گیر می‌کرد و اطلاعات کافی نمی‌داد."""
    import secrets as _sec
    if not api_key():
        return False, "API Key تنظیم نشده است."
    oid = "CT" + _sec.token_hex(6)
    res = _request("POST", "/api/v1/invoices/create", {"amount": 1, "order_id": oid})
    if res["ok"]:
        iid = str((res.get("data") or {}).get("invoice_id") or "")
        if iid:
            try:
                cancel_invoice(iid)
            except Exception:
                pass
        return True, "ارتباط با تون‌پیز برقرار است و API Key معتبر است."
    code = res.get("code") or ""
    if code in _KEY_OK_CODES:
        return True, "ارتباط با تون‌پیز برقرار است و API Key معتبر است."
    status = res.get("status")
    url = f"{base_url()}/api/v1/invoices/create"
    hint = _body_hint(res)
    if status == 404 and not code:
        return False, (f"خطای HTTP 404 — آدرس API پیدا نشد.\nآدرس تست‌شده: {url}\n"
                       "«Base URL» را دقیقاً https://tonpays.online بگذارید (بدون / و بدون مسیر اضافه)."
                       + (f"\nپاسخ سرور: {hint}" if hint else ""))
    err = res.get("error") or "خطا"
    if status is None:
        return False, f"{err}\n(اینترنت/فایروال سرور و Base URL را بررسی کنید: {url})"
    return False, f"{err}" + (f"\nپاسخ سرور: {hint}" if hint and hint not in err else "") + f"\n(HTTP {status})"


def get_status(invoice_id: str, kind: str = "std") -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    prefix, key = _prefix_and_key(kind)
    return _request("GET", f"{prefix}/check/{iid}", key=key)


def cancel_invoice(invoice_id: str, kind: str = "std") -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    prefix, key = _prefix_and_key(kind)
    return _request("POST", f"{prefix}/{iid}/cancel", key=key)


def change_card(invoice_id: str) -> dict:
    """تعویض کارت مقصد یک فاکتور کاستوم تلگرام. پاسخ: card_number/card_name تازه. کول‌داون سمت تون‌پیز اعمال می‌شود."""
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    return _request("POST", f"{_TG_PREFIX}/{iid}/change-card", key=custom_tg_key())


def register_refund_card(invoice_id: str, card_number: str, card_holder: str, kind: str = "std") -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    prefix, key = _prefix_and_key(kind)
    return _request("POST", f"{prefix}/{iid}/refund",
                    {"invoice_id": iid, "card_number": re.sub(r"\D", "", card_number or ""), "card_holder": (card_holder or "").strip()},
                    key=key)


def send_remainder_receipt(invoice_id: str, filename: str, content: bytes, content_type: str = "image/jpeg",
                           kind: str = "std") -> dict:
    """ارسال رسید: برای فاکتور استاندارد «مابقی»، برای فاکتور کاستوم تلگرام هم رسید فاکتور در انتظار و هم رسید مابقی."""
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    prefix, key = _prefix_and_key(kind)
    return _request("POST", f"{prefix}/{iid}/receipt", files={"file": (filename or "receipt.jpg", content, content_type)}, key=key)


def _first(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return str(v).strip()
    return ""


def extract_card_info(data: dict | None) -> dict:
    """اطلاعات کارت مقصد/کد پیگیری/مهلت را (اگر API در پاسخ فاکتور داده باشد) با چند نام فیلد رایج می‌خواند."""
    d = data if isinstance(data, dict) else {}
    out = {}
    card = re.sub(r"\D", "", _first(d, ("card_number", "destination_card", "pay_card_number", "card", "cardNumber")))
    if len(card) >= 12:
        out["card_number"] = card
    holder = _first(d, ("card_holder", "card_name", "card_owner", "card_holder_name", "holder_name", "holder"))
    if holder:
        out["card_holder"] = holder
    track = _first(d, ("tracking_code", "tracking_id", "payment_code", "invoice_code", "track_id", "public_id"))
    if track:
        out["tracking_code"] = track
    exp = _first(d, ("expires_at", "expire_at", "expiration_time", "expired_at"))
    if exp:
        out["expires_at_raw"] = exp
    return out


def pick_links(data: dict | None) -> dict:
    """invoice_url = ربات تلگرام؛ web_invoice_url = صفحه‌ی وب (با شناسه‌ی خریدار). بسته به link_mode ترجیح اول فرق می‌کند."""
    d = data if isinstance(data, dict) else {}

    def _u(k):
        v = d.get(k)
        return v.strip() if isinstance(v, str) and v.strip().lower().startswith(("https://", "http://", "tg://")) else None

    tg, web = _u("invoice_url"), _u("web_invoice_url")
    if cfg().link_mode == "telegram":
        primary = tg or web
    else:
        primary = web or tg
    secondary = tg if (tg and tg != primary) else (web if (web and web != primary) else None)
    return {"primary": primary, "secondary": secondary}


# ---------------------------------------------------------------------------
# امضا
# ---------------------------------------------------------------------------
def verify_signature(raw_body: bytes, header: str | None, secret: str | None = None, now: float | None = None) -> bool:
    """X-TonPays-Signature: t=<unix>,v1=<hmac_sha256_hex>  روی  f"{t}.{raw_body}"."""
    secret = (secret if secret is not None else webhook_secret()).strip()
    if not secret or not header:
        return False
    parts = {}
    for chunk in str(header).split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    t, v1 = parts.get("t", ""), parts.get("v1", "").lower()
    if not t.isdigit() or not v1:
        return False
    if abs((now if now is not None else time.time()) - int(t)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode("utf-8"), t.encode() + b"." + (raw_body or b""), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


# ---------------------------------------------------------------------------
# ارزیابی
# ---------------------------------------------------------------------------
def _amt(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def evaluate(payment: dict, data: dict) -> tuple[str, str]:
    """ارزیابی پاسخ استعلام/وب‌هوک. خروجی (state, reason):
       paid | pending | action_less | action_more | review | closed | error
    فقط status=completed و paid=true پرداخت‌شده است؛ شناسه‌ی سفارش و مبلغ فاکتور باید با فاکتور ما بخواند."""
    if not isinstance(data, dict):
        return "error", "پاسخ نامعتبر"
    status = str(data.get("status") or "").lower()
    if status in ("pending", "processing"):
        return "pending", status
    if status == "action_required_less":
        return "action_less", status
    if status == "action_required_more":
        # مبلغ بیشتر واریز شده: پرداخت هنوز paid=false است؛ باید کارت عودت مازاد ثبت شود (register_refund_card)،
        # سپس فاکتور completed می‌شود. دیگر به «بررسی دستی ادمین» نمی‌رود.
        return "action_more", status
    if status in ("expired", "canceled", "cancelled"):
        return "closed", status
    if status == "rejected":
        rr = data.get("reject_reasons")
        return "closed", "rejected" + (": " + "، ".join(str(x) for x in rr)[:200] if isinstance(rr, list) and rr else "")
    if status != "completed" or data.get("paid") is not True:
        return "error", f"وضعیت ناشناخته: {status[:40]}"

    stored = str(payment.get("ref_id") or "")
    iid = str(data.get("invoice_id") or "")
    if stored and iid and stored != iid:
        return "review", "invoice_id پاسخ با فاکتور مطابقت ندارد"
    oid = data.get("order_id")
    if oid not in (None, "") and str(oid) != str(payment.get("hash_id")):
        return "review", "order_id پاسخ با فاکتور مطابقت ندارد"
    ra = _amt(data.get("request_amount"))
    if ra is not None and ra != int(payment["price"]):
        return "review", f"مبلغ فاکتور تون‌پیز ({ra:,}) با مبلغ ثبت‌شده ({int(payment['price']):,}) مطابقت ندارد"
    return "paid", ""
