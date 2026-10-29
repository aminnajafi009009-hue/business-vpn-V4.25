"""
uniquepay.py
کلاینت آسنکرون (aiohttp) برای درگاه پرداخت آنلاین یونیک‌پی (uniquepay.top).
این ماژول فقط دو کار انجام می‌دهد که دقیقاً طبق مستندات رسمی API است:

۱) create_invoice → ساخت اینوویس جدید و گرفتن لینک پرداخت (کارت‌به‌کارت خودکار).
۲) check_invoice  → بررسی وضعیت یک اینوویس (پرداخت شده یا نه).

هیچ‌جای دیگر پروژه نباید مستقیماً به uniquepay.top درخواست بزند؛ همه باید از
همین دو تابع استفاده کنند تا در صورت تغییر API فقط همین فایل عوض شود.
"""

import logging
import uuid

import aiohttp

import loopsafe

import uniquepay_cfg as cfg

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

# perf: مثل subscription.py/shahrah.py، یک Session مشترک به‌جای ساختن
# Session تازه در هر تماس (create/check invoice)، به‌خصوص چون check_invoice
# معمولاً چندبار پشت‌سرهم در حین انتظار برای تایید پرداخت صدا زده می‌شود.
_shared_session: aiohttp.ClientSession | None = None


async def _get_shared_session() -> aiohttp.ClientSession:
    global _shared_session
    if loopsafe.usable(_shared_session):
        return _shared_session
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, keepalive_timeout=60)
    _shared_session = aiohttp.ClientSession(timeout=_TIMEOUT, connector=connector)
    return _shared_session

# مثل shahrah.py: از User-Agent شبیه مرورگر استفاده می‌کنیم تا درخواست‌ها به‌عنوان
# ترافیک بات (امضای پیش‌فرض aiohttp) بلاک/چلنج نشوند.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {cfg.token()}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": _USER_AGENT,
    }


def new_hash_id(prefix: str = "order") -> str:
    """یک شناسه‌ی یکتا برای ارسال به‌عنوان hashId اینوویس می‌سازد."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


async def create_invoice(hash_id: str, amount: int, redirect_url: str | None = None) -> dict | None:
    """اینوویس جدید می‌سازد. در صورت موفقیت دیکشنری کامل پاسخ (شامل paymentLink
    و refId) را برمی‌گرداند؛ در غیر این صورت None."""
    if not cfg.token():
        logger.warning("Business Token یونیک‌پی (پنل یا .env) تنظیم نشده؛ ساخت اینوویس نادیده گرفته شد.")
        return None
    if not cfg.amount_ok(amount):
        logger.info("مبلغ %s کمتر از حداقل مجاز درگاه یونیک‌پی است.", amount)
        return None

    payload = cfg.build_payload(hash_id, amount, redirect_url)

    try:
        session = await _get_shared_session()
        async with session.post(
            f"{cfg.base_url()}{cfg.api_path('create-invoice')}", data=payload, headers=_headers()
        ) as resp:
            status_code = resp.status
            try:
                data = await resp.json(content_type=None)
            except Exception:
                # پاسخ JSON نبود (مثلاً صفحهٔ چلنج Cloudflare یا خطای داخلی سرور)؛ بدنهٔ
                # خام را لاگ می‌کنیم تا علت واقعی مخفی نماند.
                raw_text = (await resp.text())[:500]
                logger.error(
                    "UniquePay create-invoice پاسخ غیر-JSON داد | status=%s body=%s",
                    status_code, raw_text,
                )
                return None
    except Exception:
        logger.exception("خطا در ارتباط با UniquePay هنگام ساخت اینوویس")
        return None

    if not data or not data.get("status"):
        logger.warning("UniquePay create-invoice ناموفق بود (status=%s): %s", status_code, data)
        return None
    return data


async def check_invoice(hash_id: str) -> dict | None:
    """وضعیت یک اینوویس را برمی‌گرداند: دیکشنری invoice (شامل isPaid, amount, fee)
    یا None در صورت خطا/عدم وجود."""
    if not cfg.token():
        return None

    payload = {"hashId": hash_id}
    try:
        session = await _get_shared_session()
        async with session.post(
            f"{cfg.base_url()}{cfg.api_path('check-invoice')}", data=payload, headers=_headers()
        ) as resp:
            status_code = resp.status
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raw_text = (await resp.text())[:500]
                logger.error(
                    "UniquePay check-invoice پاسخ غیر-JSON داد | status=%s body=%s",
                    status_code, raw_text,
                )
                return None
    except Exception:
        logger.exception("خطا در ارتباط با UniquePay هنگام بررسی اینوویس")
        return None

    if not data or not data.get("status"):
        logger.warning("UniquePay check-invoice ناموفق بود (status=%s): %s", status_code, data)
        return None
    return data.get("invoice")


async def is_invoice_paid(hash_id: str) -> bool:
    invoice = await check_invoice(hash_id)
    # UniquePay now exposes isVerified separately; payment is final only when
    # both flags are true.
    return bool(invoice and invoice.get("isPaid") and invoice.get("isVerified"))


def white_label_data(invoice: dict | None) -> dict | None:
    """Return the White Label payment data supplied by UniquePay."""
    if not isinstance(invoice, dict):
        return None
    try:
        import uniquepay_cfg as _cfg
        if not _cfg.show_white_label():
            return None
    except Exception:
        pass
    wl = invoice.get("whiteLabel")
    if not isinstance(wl, dict):
        return None
    card = str(wl.get("cardNumber") or "").strip()
    amount = str(wl.get("payableAmount") or invoice.get("payableAmount") or "").strip()
    if not card or not amount:
        return None
    return {"cardNumber": card, "payableAmount": amount}
