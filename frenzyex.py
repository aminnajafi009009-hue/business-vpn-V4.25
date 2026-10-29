"""
frenzyex.py
کلاینت درگاه FrenzyEx (Merchant Payment Gateway Guide v3) — کاملاً مطابق راهنمای رسمی.

  • ساخت درخواست پرداخت:  POST /api/v1/payment-requests         (Bearer MERCHANT_API_KEY)
  • استعلام وضعیت:        GET  /api/v1/payment-requests/{request_id}
  • لغو درخواست پرداخت‌نشده: POST /api/v1/payment-requests/{request_id}/cancel
  • کال‌بک امضاشده:       هدر X-Frenzy-Signature = HMAC-SHA256(raw body, CALLBACK_SECRET) به‌صورت hex
                         هدر X-Frenzy-Event = payment.completed | payment.test

قواعد مهم راهنما که اینجا رعایت شده‌اند:
  - API Key / Callback Secret فقط سمت سرور (هیچ‌جا در لاگ/پیام کاربر چاپ نمی‌شود).
  - لینک پرداخت را خودمان نمی‌سازیم؛ checkout_url / pay_url / bot_pay_url را از پاسخ کپی می‌کنیم.
  - POST ساخت درخواست هرگز کورکورانه retry نمی‌شود (v1 کلید idempotency ندارد).
  - تصمیم نهایی تحویل: امضای معتبر + event=payment.completed + status=paid + تطابق
    request_id/order_ref/مبلغ؛ و فقط یک‌بار (idempotent) — اینجا فقط «ارزیابی» انجام می‌شود و
    تحویل واقعی با claim اتمیک دیتابیس در لایه‌ی بالاتر انجام می‌شود.
  - tx_hash به‌تنهایی هرگز معیار پرداخت نیست (ممکن است custody:<id> باشد).

این ماژول sync است (requests)؛ نسخه‌های async با asyncio.to_thread در online_gateways.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re

import requests

import database as db
from payment_manager_v3 import FrenzyExConfig

logger = logging.getLogger(__name__)

SETTING_KEY = "pm_frenzyex"
DEFAULT_BASE_URL = "https://frenzy.fastsnap.info"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25

_USER_AGENT = "VPNShopBot-FrenzyEx/1.0"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
VALID_PAYMENT_METHODS = ("auto", "standard", "nowpay")
VALID_LINK_MODES = ("checkout", "bot")


# ---------------------------------------------------------------------------
# تنظیمات (از پنل ادمین → دیتابیس)
# ---------------------------------------------------------------------------
def cfg() -> FrenzyExConfig:
    try:
        raw = db.get_setting(SETTING_KEY, "{}") or "{}"
        d = json.loads(raw)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        logger.exception("خواندن تنظیمات pm_frenzyex ناموفق بود")
        d = {}
    return FrenzyExConfig.from_dict(d)


def api_key() -> str:
    return (cfg().api_key or "").strip()


def callback_secret() -> str:
    return (cfg().callback_secret or "").strip()


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


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }


# ---------------------------------------------------------------------------
# تماس HTTP
# ---------------------------------------------------------------------------
# پیام‌های خطای قابل‌فهم برای ادمین (طبق جدول «Error Handling» راهنما)
_HTTP_REASONS = {
    400: "درخواست نامعتبر (مبلغ/ارز/روش پرداخت یا Crypto Collection برای این مرچنت فعال نیست)",
    401: "API Key نامعتبر یا منقضی است (هدر Bearer)",
    403: "حساب مرچنت در FrenzyEx غیرفعال است",
    404: "درخواست پرداخت پیدا نشد (یا متعلق به این مرچنت نیست)",
    409: "تعارض وضعیت (پرداخت‌شده/در حال بررسی/قبلاً با روش دیگری شروع شده)",
    410: "مهلت صفحه‌ی پرداخت مشتری تمام شده است",
    429: "بیش از ۱۰ درخواست در ۶۰ ثانیه (Rate limit)؛ کمی بعد دوباره تلاش کنید",
    503: "سرویس قیمت‌گذاری کریپتو موقتاً در دسترس نیست",
}


def _error_text(status: int, body) -> str:
    base = _HTTP_REASONS.get(status) or f"خطای HTTP {status}"
    detail = ""
    if isinstance(body, dict):
        for k in ("error", "message", "detail", "msg"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                detail = v.strip()[:200]
                break
            if isinstance(v, dict):
                inner = v.get("message") or v.get("detail")
                if isinstance(inner, str) and inner.strip():
                    detail = inner.strip()[:200]
                    break
    return f"{base}" + (f" — {detail}" if detail else "")


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    """خروجی همیشه dict است: {"ok": bool, "status": int|None, "data": dict|None, "error": str|None}.
    هیچ استثنایی به بیرون نشت نمی‌کند و کلید API هرگز در پیام خطا نمی‌آید."""
    if not api_key():
        return {"ok": False, "status": None, "data": None, "error": "API Key فرنزی‌اکس تنظیم نشده است"}
    url = f"{base_url()}{path}"
    try:
        resp = requests.request(
            method, url,
            data=(json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None),
            headers=_headers(),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.warning("FrenzyEx %s %s خطای شبکه: %s", method, path, type(exc).__name__)
        return {"ok": False, "status": None, "data": None, "error": f"خطای شبکه ({type(exc).__name__})"}

    try:
        body = resp.json()
    except ValueError:
        body = None
        logger.error("FrenzyEx %s %s پاسخ غیر-JSON داد | status=%s body=%s", method, path, resp.status_code, resp.text[:300])

    if 200 <= resp.status_code < 300 and isinstance(body, dict):
        return {"ok": True, "status": resp.status_code, "data": body, "error": None}

    if 300 <= resp.status_code < 400:
        return {"ok": False, "status": resp.status_code, "data": None,
                "error": "ریدایرکت از سمت FrenzyEx (Base URL را بررسی کنید)"}

    return {"ok": False, "status": resp.status_code, "data": body if isinstance(body, dict) else None,
            "error": _error_text(resp.status_code, body)}


# ---------------------------------------------------------------------------
# عملیات API
# ---------------------------------------------------------------------------
def create_payment_request(amount_toman: int, order_ref: str, description: str | None = None) -> dict:
    """درخواست پرداخت جدید به تومان (TMN). خروجی دیکشنری _request است؛ در موفقیت data شامل
    request_id و لینک‌هاست. توجه: این تابع عمداً retry ندارد."""
    c = cfg()
    method = c.payment_method if c.payment_method in VALID_PAYMENT_METHODS else "auto"
    payload: dict = {
        "amount": int(amount_toman),
        "amount_ccy": "TMN",
        "payment_method": method,
        "order_ref": str(order_ref),
    }
    desc = (description or c.description or "").strip()
    if desc:
        payload["description"] = desc[:200]
    try:
        rate = int(c.usd_rate_toman or 0)
    except (TypeError, ValueError):
        rate = 0
    if rate > 0:
        payload["usd_rate_toman"] = rate
    result = _request("POST", "/api/v1/payment-requests", payload)
    if result["ok"]:
        rid = str((result["data"] or {}).get("request_id") or "").strip()
        if not _REQUEST_ID_RE.match(rid):
            logger.error("FrenzyEx: پاسخ ساخت درخواست request_id معتبر نداشت: %s", str(result["data"])[:300])
            return {"ok": False, "status": result["status"], "data": result["data"],
                    "error": "پاسخ نامعتبر از FrenzyEx (request_id ندارد)"}
    return result


def get_status(request_id: str) -> dict:
    rid = str(request_id or "").strip()
    if not _REQUEST_ID_RE.match(rid):
        return {"ok": False, "status": None, "data": None, "error": "request_id نامعتبر"}
    return _request("GET", f"/api/v1/payment-requests/{rid}")


def cancel_request(request_id: str) -> dict:
    rid = str(request_id or "").strip()
    if not _REQUEST_ID_RE.match(rid):
        return {"ok": False, "status": None, "data": None, "error": "request_id نامعتبر"}
    return _request("POST", f"/api/v1/payment-requests/{rid}/cancel", {})


def pick_links(data: dict | None) -> dict:
    """لینک‌های پاسخ ساخت درخواست. طبق راهنما: checkout_url ترجیح اول؛ pay_url فالبک؛
    bot_pay_url لینک «بدون WebView». فیلدهای اختیاری ممکن است نباشند (defensive)."""
    d = data if isinstance(data, dict) else {}

    def _u(k):
        v = d.get(k)
        return v.strip() if isinstance(v, str) and v.strip().lower().startswith(("https://", "http://", "tg://")) else None

    checkout = _u("checkout_url")
    pay = _u("pay_url") or _u("tg_url")
    bot = _u("bot_pay_url") or _u("bot_tg_url")
    mode = cfg().link_mode if cfg().link_mode in VALID_LINK_MODES else "checkout"
    if mode == "bot":
        primary = bot or checkout or pay
    else:
        primary = checkout or pay or bot
    # دکمه‌ی دوم: «باز کردن در چت ربات» (اگر با لینک اصلی فرق داشته باشد)
    secondary = bot if bot and bot != primary else None
    return {"primary": primary, "secondary": secondary}


# ---------------------------------------------------------------------------
# امضا و ارزیابی
# ---------------------------------------------------------------------------
def verify_signature(raw_body: bytes, signature_header: str | None, secret: str | None = None) -> bool:
    """HMAC-SHA256 روی «بدنه‌ی خام» (نه JSON دوباره‌سریال‌شده)، مقایسه‌ی constant-time، hex با حروف کوچک."""
    secret = (secret if secret is not None else callback_secret()).strip()
    sig = (signature_header or "").strip().lower()
    if sig.startswith("sha256="):
        sig = sig[7:]
    if not secret or not sig:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _to_amount(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _amount_matches(expected: int, got) -> bool:
    g = _to_amount(got)
    return g is not None and abs(g - float(expected)) < 0.5


def evaluate_callback(payment: dict, data: dict) -> tuple[str, str]:
    """ارزیابی بدنه‌ی کال‌بک (بعد از تأیید امضا). خروجی (verdict, reason):
       "ignore"  → رویداد تستی/غیرپرداخت (فقط 200 بدهید، هیچ تحویلی نه)
       "paid"    → همه‌چیز مطابق است
       "review"  → پرداخت تأیید شده ولی مغایرت (مثلاً مبلغ) دارد → بررسی دستی ادمین"""
    if not isinstance(data, dict):
        return "ignore", "بدنه نامعتبر"
    event = str(data.get("event") or "")
    if event == "payment.test" or data.get("test") is True:
        return "ignore", "callback تستی"
    if event != "payment.completed":
        return "ignore", f"event نامرتبط: {event[:40]}"
    if str(data.get("status") or "") != "paid":
        return "ignore", "status=paid نیست"

    stored_rid = str(payment.get("ref_id") or "")
    ids = {str(data.get(k)) for k in ("request_id", "payment_id", "purchase_id") if data.get(k) not in (None, "")}
    if not ids or (stored_rid and stored_rid not in ids):
        return "review", "شناسه‌ی درخواست کال‌بک با فاکتور مطابقت ندارد"
    order_ref = data.get("order_ref") if data.get("order_ref") not in (None, "") else data.get("order_id")
    if order_ref not in (None, "") and str(order_ref) != str(payment.get("hash_id")):
        return "review", "order_ref کال‌بک با فاکتور مطابقت ندارد"

    ccy = str(data.get("price_currency") or "").upper()
    if ccy and ccy != "TMN":
        return "review", f"ارز فاکتور TMN نیست ({ccy})"
    if not _amount_matches(int(payment["price"]), data.get("price_amount")):
        return "review", f"مبلغ فاکتور ({data.get('price_amount')}) با مبلغ ثبت‌شده ({int(payment['price']):,}) مطابقت ندارد"
    return "paid", ""


def evaluate_status(payment: dict, data: dict) -> tuple[str, str]:
    """ارزیابی پاسخ GET status. خروجی (state, reason) با state از:
       pending | paid | closed (expired/canceled) | review | error"""
    if not isinstance(data, dict):
        return "error", "پاسخ نامعتبر"
    status = str(data.get("status") or "").lower()
    if status == "pending":
        return "pending", ""
    if status in ("expired", "canceled", "cancelled"):
        return "closed", status
    if status != "paid":
        return "error", f"وضعیت ناشناخته: {status[:40]}"

    stored_rid = str(payment.get("ref_id") or "")
    rid = str(data.get("request_id") or "")
    if rid and stored_rid and rid != stored_rid:
        return "review", "request_id پاسخ با فاکتور مطابقت ندارد"
    order_ref = data.get("order_ref")
    if order_ref not in (None, "") and str(order_ref) != str(payment.get("hash_id")):
        return "review", "order_ref پاسخ با فاکتور مطابقت ندارد"
    ccy = str(data.get("amount_ccy") or "").upper()
    if ccy and ccy != "TMN":
        return "review", f"ارز فاکتور TMN نیست ({ccy})"
    amt = data.get("amount")
    if amt is not None and not _amount_matches(int(payment["price"]), amt):
        return "review", f"مبلغ فاکتور ({amt}) با مبلغ ثبت‌شده ({int(payment['price']):,}) مطابقت ندارد"
    return "paid", ""
