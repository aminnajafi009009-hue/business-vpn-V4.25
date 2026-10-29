"""
uniquepay_sync.py
همان کلاینت uniquepay.py ولی نسخه‌ی sync (با requests) برای استفاده در
webapp_api.py که یک اپ Flask سینک است (بدون event loop آسنکرون).
منطق و پارامترها دقیقاً مطابق مستندات رسمی API یونیک‌پی و آینه‌ی uniquepay.py است.
"""

import logging
import uuid

import requests

import uniquepay_cfg as cfg

logger = logging.getLogger(__name__)

_TIMEOUT = 20

# مثل uniquepay.py (نسخه‌ی آسنکرون ربات) و shahrah.py: بدون User-Agent شبیه
# مرورگر، درخواست‌های requests به‌عنوان ترافیک بات شناسایی و توسط
# Cloudflare بلاک/چلنج می‌شوند و درگاه پرداخت آنلاین در Mini App باز نمی‌شود.
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


def new_hash_id(prefix: str = "miniapp") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def create_invoice(hash_id: str, amount: int, redirect_url: str | None = None) -> dict | None:
    if not cfg.token() or not cfg.amount_ok(amount):
        return None

    payload = cfg.build_payload(hash_id, amount, redirect_url)
    try:
        resp = requests.post(
            f"{cfg.base_url()}{cfg.api_path('create-invoice')}", data=payload, headers=_headers(), timeout=_TIMEOUT
        )
        data = resp.json()
    except requests.RequestException:
        logger.exception("خطا در ارتباط با UniquePay هنگام ساخت اینوویس (miniapp)")
        return None
    except ValueError:
        # پاسخ JSON نبود؛ احتمالاً صفحه‌ی چلنج Cloudflare یا خطای سرور برگشته.
        # برخلاف قبل، بدنه‌ی خام را لاگ می‌کنیم تا علت واقعی مخفی نماند.
        logger.error(
            "UniquePay create-invoice (miniapp) پاسخ غیر-JSON داد | status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        return None

    if not data or not data.get("status"):
        logger.warning("UniquePay create-invoice (miniapp) ناموفق بود: %s", data)
        return None
    return data


def check_invoice(hash_id: str) -> dict | None:
    if not cfg.token():
        return None

    try:
        resp = requests.post(
            f"{cfg.base_url()}{cfg.api_path('check-invoice')}",
            data={"hashId": hash_id},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except requests.RequestException:
        logger.exception("خطا در ارتباط با UniquePay هنگام بررسی اینوویس (miniapp)")
        return None
    except ValueError:
        logger.error(
            "UniquePay check-invoice (miniapp) پاسخ غیر-JSON داد | status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        return None

    if not data or not data.get("status"):
        return None
    return data.get("invoice")


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


def is_invoice_paid(invoice: dict | None) -> bool:
    return bool(invoice and invoice.get("isPaid") and invoice.get("isVerified"))
