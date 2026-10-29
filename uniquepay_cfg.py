"""
uniquepay_cfg.py
تنظیمات مؤثر درگاه یونیک‌پی: اول از تنظیمات پنل ادمین (pm_uniquepay در دیتابیس)،
و اگر خالی بود از .env (config.py). هر دو کلاینت (async و sync) از همین‌جا می‌خوانند
تا «Business Token / Redirect URL / Callback URL / حداقل مبلغ / حالت لینک» در ربات و
مینی‌اپ واقعاً روی درخواست‌ها اثر بگذارند.
"""
import json
import logging

from config import UNIQUEPAY_BASE_URL, UNIQUEPAY_BUSINESS_TOKEN, UNIQUEPAY_REDIRECT_URL

logger = logging.getLogger(__name__)

# مستندات: مبلغ باید «بیشتر از ۵۰٬۰۰۰ تومان» باشد.
HARD_MIN_AMOUNT = 50_000


def _panel() -> dict:
    try:
        import database as db
        raw = db.get_setting("pm_uniquepay", "{}") or "{}"
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        logger.exception("خواندن تنظیمات pm_uniquepay ناموفق بود")
        return {}


def token() -> str:
    return (_panel().get("business_token") or "").strip() or (UNIQUEPAY_BUSINESS_TOKEN or "").strip()


def redirect_url() -> str:
    return (_panel().get("redirect_url") or "").strip() or UNIQUEPAY_REDIRECT_URL


def callback_url() -> str:
    return (_panel().get("callback_url") or "").strip()


def min_amount() -> int:
    try:
        v = int(_panel().get("min_amount") or 0)
    except (TypeError, ValueError):
        v = 0
    return max(v, 0)


def use_telegram_link() -> bool:
    return bool(_panel().get("use_telegram_link", False))


def show_white_label() -> bool:
    """نمایش «شماره کارت مقصد + مبلغ دقیق واریز» (وایت‌لیبل) کنار لینک پرداخت. پیش‌فرض روشن."""
    return bool(_panel().get("show_card_white_label", True))


def base_url() -> str:
    return UNIQUEPAY_BASE_URL


def api_path(name: str) -> str:
    """name: create-invoice | check-invoice. (حالت DDBot/Mirza حذف شد؛ همیشه API رسمی.)"""
    return f"/api/{name}"


def amount_ok(amount: int) -> bool:
    """مبلغ باید از سقف سخت درگاه (۵۰ هزار) بیشتر و از حداقل تنظیم‌شده کمتر نباشد."""
    a = int(amount)
    return a > HARD_MIN_AMOUNT and a >= min_amount()


def build_payload(hash_id: str, amount: int, redirect: str | None = None) -> dict:
    p = {"hashId": hash_id, "amount": str(int(amount)), "redirectUrl": redirect or redirect_url()}
    cb = callback_url()
    if cb:
        p["callbackUrl"] = cb
    return p


def pick_link(invoice: dict | None) -> str | None:
    """لینک پرداخت: اگر «پرداخت داخل تلگرام» روشن باشد telegramPaymentLink، وگرنه paymentLink."""
    if not isinstance(invoice, dict):
        return None
    web = invoice.get("paymentLink")
    tg = invoice.get("telegramPaymentLink")
    if use_telegram_link() and tg:
        return tg
    return web or tg


def enabled() -> bool:
    """درگاه وقتی فعال است که توکن (پنل یا .env) موجود باشد و ادمین آن را خاموش نکرده باشد."""
    if not token():
        return False
    return bool(_panel().get("enabled", True))
