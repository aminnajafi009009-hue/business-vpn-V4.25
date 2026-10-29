"""
tronado.py
کلاینت درگاه ترونادو (Tronado، @Tronado_Robot) — API نسخه‌ی v5.

⚠️ مستندات رسمی ترونادو داخل یک مینی‌اپ تلگرام است و قابل دریافت خودکار نیست؛ این کلاینت بر پایه‌ی
قرارداد API که در پیاده‌سازی‌های مرجع عمومی (فروشگاه‌های فعال) استفاده می‌شود نوشته شده:

  • قیمت ترون:      POST {base}/Tron/GetPriceToToman            → {"TronPriceToman": N}
  • ساخت سفارش:     POST {base}/api/v5/GetOrderToken?wageFromBusinessPercentage=N   (هدر x-api-key)
        بدنه: {"PaymentID","WalletAddress","TronAmount","CallbackUrl"}
        موفق: {"Token": "...", "EstimatedTomanAmount": ...}  (یا داخل "Data")
        ناموفق: {"IsSuccessful":false,"Code":-31,"Message":"...","Data":null}
  • وضعیت سفارش:    POST {base}/Order/GetStatusByPaymentID  {"Id": PaymentID}   (هدر x-api-key)
        → {"OrderStatusID": 30|40|200|..., "IsPaid": bool, ...}   (30=پرداخت‌شده، 40=رد رسید، 200=منقضی)
  • لینک پرداخت کاربر: https://t.me/tronado_robot/customerpayment?startapp={Token}
  • کال‌بک پرداخت: بدنه JSON + هدر X-Tronado-Sig = HMAC-SHA512(raw body, IpnSigningKey) hex
        فیلدها: PaymentId/PaymentID, OrderStatusID, IsPaid, TronAmount, ActualTronAmount,
        UserPaidTomanAmount, TomanAmountWithoutWage, UniqueCode
  • کال‌بک اعتراض (Dispute): همان امضا؛ Event=DisputeAccepted

اگر ترونادو چیزی از قرارداد بالا را تغییر دهد، فقط همین فایل باید به‌روز شود.
قواعد ایمنی (مطابق مرجع): تحویل فقط با «امضای معتبر + پرداخت‌شده + تطابق شناسه/مبلغ» و فقط یک‌بار؛
در هر مغایرتی (کمتر واریز کردن و...) تحویل خودکار انجام نمی‌شود و ادمین بررسی دستی می‌کند.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import time
from decimal import Decimal, ROUND_CEILING

import requests

import database as db
from payment_manager_v3 import TronadoConfig

logger = logging.getLogger(__name__)

SETTING_KEY = "pm_tronado"
DEFAULT_BASE_URL = "https://bot.tronado.cloud"
ORDER_TOKEN_PATH = "/api/v5/GetOrderToken"
PRICE_PATH = "/Tron/GetPriceToToman"
STATUS_BY_PAYMENT_ID_PATH = "/Order/GetStatusByPaymentID"
PAYMENT_URL_TEMPLATE = "https://t.me/tronado_robot/customerpayment?startapp={token}"

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
TRX_EPSILON = 0.000001

# OrderStatusID ها (طبق کال‌بک/استعلام)
STATUS_PAID = 30
STATUS_REJECTED = 40
STATUS_EXPIRED = 200

_PAYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_USER_AGENT = "VPNShopBot-Tronado/1.0"


# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------
def cfg() -> TronadoConfig:
    try:
        d = json.loads(db.get_setting(SETTING_KEY, "{}") or "{}")
        if not isinstance(d, dict):
            d = {}
    except Exception:
        logger.exception("خواندن تنظیمات pm_tronado ناموفق بود")
        d = {}
    return TronadoConfig.from_dict(d)


def api_key() -> str:
    return (cfg().api_key or "").strip()


def ipn_signing_key() -> str:
    return (cfg().ipn_signing_key or "").strip()


def wallet_address() -> str:
    return (cfg().wallet_address or "").strip()


def base_url() -> str:
    return ((cfg().base_url or "").strip() or DEFAULT_BASE_URL).rstrip("/")


def wage_percent() -> int:
    try:
        return max(0, min(100, int(cfg().wage_percent or 0)))
    except (TypeError, ValueError):
        return 0


def enabled() -> bool:
    c = cfg()
    return bool(c.enabled and (c.api_key or "").strip() and (c.wallet_address or "").strip())


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


# ---------------------------------------------------------------------------
# اعتبارسنجی آدرس ترون (Base58Check؛ همان قاعده‌ای که GetOrderToken اعمال می‌کند)
# ---------------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_valid_tron_address(address: str) -> bool:
    addr = (address or "").strip()
    if len(addr) != 34 or addr[0] != "T":
        return False
    num = 0
    for ch in addr:
        idx = _B58.find(ch)
        if idx < 0:
            return False
        num = num * 58 + idx
    try:
        raw = num.to_bytes(25, "big")
    except OverflowError:
        return False
    if raw[0] != 0x41:
        return False
    checksum = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return hmac.compare_digest(checksum, raw[21:])


# ---------------------------------------------------------------------------
# تماس HTTP
# ---------------------------------------------------------------------------
def _post(path: str, payload: dict, with_key: bool = True, query: dict | None = None) -> dict:
    """خروجی: {"ok": bool, "status": int|None, "data": dict|None, "error": str|None}. هرگز استثنا نمی‌دهد."""
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _USER_AGENT}
    if with_key:
        key = api_key()
        if not key:
            return {"ok": False, "status": None, "data": None, "error": "API Key ترونادو تنظیم نشده است"}
        headers["x-api-key"] = key
    url = f"{base_url()}{path}"
    try:
        resp = requests.post(
            url, params=query, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.warning("Tronado POST %s خطای شبکه: %s", path, type(exc).__name__)
        return {"ok": False, "status": None, "data": None, "error": f"خطای شبکه ({type(exc).__name__})"}
    try:
        body = resp.json()
    except ValueError:
        logger.error("Tronado POST %s پاسخ غیر-JSON داد | status=%s body=%s", path, resp.status_code, resp.text[:300])
        return {"ok": False, "status": resp.status_code, "data": None,
                "error": f"پاسخ نامعتبر از ترونادو (HTTP {resp.status_code})"}
    if not isinstance(body, dict):
        return {"ok": False, "status": resp.status_code, "data": None, "error": "پاسخ نامعتبر از ترونادو"}
    if resp.status_code >= 400:
        return {"ok": False, "status": resp.status_code, "data": body, "error": _error_message(body, resp.status_code)}
    return {"ok": True, "status": resp.status_code, "data": body, "error": None}


def _error_message(body: dict, status: int | None = None) -> str:
    msg = ""
    for k in ("ErrorMessage", "Message", "message", "error"):
        v = body.get(k) if isinstance(body, dict) else None
        if isinstance(v, str) and v.strip():
            msg = v.strip()[:250]
            break
    code = body.get("Code") if isinstance(body, dict) else None
    out = msg or (f"HTTP {status}" if status else "پاسخ نامعتبر از سرویس ترونادو")
    if code not in (None, ""):
        out += f" (Code {code})"
    return out


# ---------------------------------------------------------------------------
# قیمت ترون
# ---------------------------------------------------------------------------
_price_cache: dict = {"v": None, "t": 0.0}
PRICE_CACHE_SECONDS = 15


def get_tron_price_toman(use_cache: bool = True) -> float | None:
    """قیمت هر ۱ TRX به تومان از خود ترونادو (همان نرخی که سفارش را با آن می‌سنجد)."""
    now = time.time()
    if use_cache and _price_cache["v"] and now - _price_cache["t"] < PRICE_CACHE_SECONDS:
        return _price_cache["v"]
    res = _post(PRICE_PATH, {}, with_key=False)
    if not res["ok"]:
        return None
    try:
        price = float((res["data"] or {}).get("TronPriceToman"))
    except (TypeError, ValueError):
        return None
    if not (price > 0) or math.isinf(price) or math.isnan(price):
        return None
    _price_cache["v"], _price_cache["t"] = price, now
    return price


def toman_to_trx(price_toman: int, tron_price: float) -> float:
    """مبلغ تومان → TRX با ۶ رقم اعشار، رو به بالا (تا مبلغ فاکتور هیچ‌وقت کم‌پوشش نشود)."""
    d = (Decimal(int(price_toman)) / Decimal(str(tron_price))).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    return float(d)


# ---------------------------------------------------------------------------
# ساخت سفارش
# ---------------------------------------------------------------------------
def _normalize_order_response(decoded: dict) -> dict:
    """Token را از سطح اول یا Data بیرون می‌کشد و پیام خطا را (حتی وقتی Data=null) حفظ می‌کند."""
    if "Token" in decoded:
        return decoded
    inner = decoded.get("Data")
    if isinstance(inner, dict):
        out = dict(inner)
        if "ErrorMessage" not in out and decoded.get("Message"):
            out["ErrorMessage"] = decoded.get("Message")
        return out
    out = dict(decoded)
    if "ErrorMessage" not in out and str(decoded.get("Message") or "").strip():
        out["ErrorMessage"] = str(decoded["Message"])
    return out


def create_order(payment_id: str, price_toman: int, callback_url: str) -> dict:
    """سفارش (توکن پرداخت) می‌سازد. خروجی:
       {"ok": True, "token", "tron_amount", "wage", "estimated_toman", "link"}
       یا {"ok": False, "error": "..."}."""
    wallet = wallet_address()
    if not wallet:
        return {"ok": False, "error": "آدرس کیف پول ترونادو تنظیم نشده است"}
    if not is_valid_tron_address(wallet):
        return {"ok": False, "error": "آدرس کیف پول ترونادو معتبر نیست (Base58Check؛ حروف بزرگ/کوچک مهم است)"}
    if not _PAYMENT_ID_RE.match(str(payment_id)):
        return {"ok": False, "error": "PaymentID نامعتبر"}
    if not str(callback_url or "").lower().startswith("https://"):
        return {"ok": False, "error": "آدرس Callback باید HTTPS عمومی باشد (آدرس عمومی ربات را در تنظیمات ترونادو ثبت کنید)"}

    price = get_tron_price_toman()
    if not price:
        return {"ok": False, "error": "دریافت قیمت ترون از ترونادو ناموفق بود"}
    tron_amount = toman_to_trx(int(price_toman), price)
    if tron_amount <= 0:
        return {"ok": False, "error": "مبلغ ترون محاسبه‌شده نامعتبر است"}

    wage = wage_percent()
    res = _post(
        ORDER_TOKEN_PATH,
        {"PaymentID": str(payment_id), "WalletAddress": wallet, "TronAmount": tron_amount, "CallbackUrl": callback_url},
        query={"wageFromBusinessPercentage": wage},
    )
    if res["data"] is None:
        return {"ok": False, "error": res["error"] or "پاسخ نامعتبر از سرویس ترونادو"}

    norm = _normalize_order_response(res["data"])
    token = str(norm.get("Token") or "").strip()
    if not res["ok"] or not token:
        err = str(norm.get("ErrorMessage") or "").strip() or res["error"] or "پاسخ نامعتبر از سرویس ترونادو"
        code = res["data"].get("Code")
        if code not in (None, "") and f"Code {code}" not in err:
            err += f" (Code {code})"
        return {"ok": False, "error": err[:300]}
    if not re.match(r"^[A-Za-z0-9_\-\.=]{4,200}$", token):
        return {"ok": False, "error": "توکن نامعتبر از ترونادو"}

    est = norm.get("EstimatedTomanAmount")
    return {
        "ok": True,
        "token": token,
        "tron_amount": tron_amount,
        "wage": wage,
        "estimated_toman": _parse_toman(est),
        "link": PAYMENT_URL_TEMPLATE.format(token=token),
    }


def _parse_toman(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(math.ceil(float(value))) if value == value else 0
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value.split(".")[0])
        return int(digits) if digits else 0
    return 0


# ---------------------------------------------------------------------------
# وضعیت
# ---------------------------------------------------------------------------
def get_status(payment_id: str) -> dict:
    """استعلام وضعیت با شناسه‌ی پرداخت خودمان. خروجی مثل _post؛ data شامل OrderStatusID و IsPaid."""
    if not _PAYMENT_ID_RE.match(str(payment_id)):
        return {"ok": False, "status": None, "data": None, "error": "PaymentID نامعتبر"}
    return _post(STATUS_BY_PAYMENT_ID_PATH, {"Id": str(payment_id)})


def _is_paid(data: dict) -> bool:
    return status_code_of(data) == STATUS_PAID or data.get("IsPaid") is True


def status_code_of(data: dict) -> int | None:
    v = data.get("OrderStatusID")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# امضا
# ---------------------------------------------------------------------------
def verify_signature(raw_body: bytes, signature_header: str | None, key: str | None = None) -> bool:
    """HMAC-SHA512 روی بدنه‌ی خام، hex؛ مقایسه‌ی constant-time (هدر X-Tronado-Sig)."""
    key = (key if key is not None else ipn_signing_key()).strip()
    sig = (signature_header or "").strip().lower()
    if not key or not sig:
        return False
    expected = hmac.new(key.encode("utf-8"), raw_body or b"", hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# ارزیابی پرداخت (پورت منطق مرجع tronadoV2Evaluate + دوره‌ی انتظار برای کال‌بک)
# ---------------------------------------------------------------------------
def _norm_token(t) -> str:
    return str(t or "").strip().replace("-", "").lower()


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def evaluate(payment: dict, gw: dict, data: dict, source: str) -> tuple[str, str, int | None]:
    """خروجی (verdict, reason, paid_toman):
       verdict:
         "not_paid" → پرداخت‌شده نیست
         "paid"     → مطابق و قابل تحویل
         "review"   → مغایرت واقعی (کمتر پرداخت، شناسه نامطابق...) → ادمین
         "wait"     → (فقط source=status) داده‌ی مبلغ نیست؛ منتظر کال‌بک امضاشده بمان
       source: "callback" | "status"
       اگر ادمین قبلاً دستی تأیید کرده (gw["approved"]) فقط پرداخت‌شدن کافی است."""
    ref = data.get("PaymentId", data.get("PaymentID"))
    if ref is not None and str(ref) != str(payment.get("hash_id")):
        return "review", "شناسه‌ی پرداخت ترونادو با فاکتور مطابقت ندارد", None
    if not _is_paid(data):
        return "not_paid", "", None

    user_paid = _num(data.get("UserPaidTomanAmount"))
    user_paid_i = int(user_paid) if user_paid is not None else None
    if gw.get("approved"):
        return "paid", "", user_paid_i

    stored_tok, cb_tok = _norm_token(gw.get("token")), _norm_token(data.get("UniqueCode"))
    if stored_tok and cb_tok and stored_tok != cb_tok:
        logger.warning("Tronado: UniqueCode با توکن ذخیره‌شده فرق دارد | payment=%s", payment.get("hash_id"))

    sent = _num(gw.get("tron_amount")) or 0.0
    wage = gw.get("wage")
    estimated = int(gw.get("estimated_toman") or 0)
    if wage is None or sent <= 0:
        return "review", "اطلاعات اولیه‌ی فاکتور ترونادو (کارمزد/مقدار ترون) ذخیره نشده است", user_paid_i
    wage = int(wage)

    tron = _num(data.get("TronAmount"))
    if wage <= 0:
        if tron is None:
            return ("wait" if source != "callback" else "review",
                    "مقدار ترون تحویل‌شده در پاسخ ترونادو وجود ندارد", user_paid_i)
        if round(tron, 6) + TRX_EPSILON < round(sent, 6):
            return "review", f"ترون تحویل‌شده ({tron:g}) کمتر از ترون فاکتور ({sent:g}) است", user_paid_i
        return "paid", "", user_paid_i

    # wage > 0: مبلغ پرداختی کاربر فقط در کال‌بک امضاشده هست
    if source != "callback":
        return "wait", "مبلغ تومانی پرداخت‌شده فقط در کال‌بک امضاشده می‌آید", user_paid_i
    if estimated <= 0:
        return "review", "مبلغ تخمینی فاکتور ذخیره نشده است", user_paid_i
    if user_paid_i is None:
        return "review", "مبلغ پرداختی کاربر در کال‌بک ترونادو وجود ندارد", None
    if user_paid_i < estimated:
        return "review", f"مبلغ پرداختی کاربر ({user_paid_i:,}) کمتر از مبلغ تخمینی فاکتور ({estimated:,}) است", user_paid_i
    return "paid", "", user_paid_i
