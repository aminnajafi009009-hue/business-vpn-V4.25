"""
utils.py
توابع کمکی کوچک و مشترک بین handlerها.
"""

import os
import re
import aiohttp
import time
import random
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramBadRequest

from keyboards import main_reply_keyboard

logger = logging.getLogger(__name__)

# سرور ربات (Render) با ساعت UTC کار می‌کند و همه‌ی رشده‌های زمانی ذخیره‌شده در
# دیتابیس (created_at/expires_at و ...) بر همین اساس هستند؛ برای اینکه چیزی که
# به کاربر نمایش داده می‌شود (نه چیزی که در فاکتورها/شمارش‌معکوس مقایسه می‌شود)
# همیشه بر طبق ساعت تهران باشد، این دو تابع کمکی فقط برای «نمایش» استفاده می‌شوند.
# 🐛 فیکس: قبلاً اینجا از pytz استفاده می‌شد که در requirements.txt نبود و در محیط دیپلوی (Render)
# باعتت ModuleNotFoundError: No module named 'pytz' می‌شد؛ به جای آن از zoneinfo (کتابخانه‌ی استاندارد پایتون 3.9+) استفاده شد تا وابستگی جدیدی لازم نباشد.
TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def now_tehran() -> datetime:
    """اکنون را بر اساس ساعت تهران برمی‌گرداند (فقط برای نمایش به کاربر/ادمین؛
    نه برای مقایسه با زمان‌های ذخیره‌شده در دیتابیس که بر مبنای ساعت UTC سرور هستند)."""
    return datetime.now(timezone.utc).astimezone(TEHRAN_TZ)


def now_tehran_naive() -> datetime:
    """معادل now_tehran اما به‌صورت naive (بدون tzinfo)؛ برای مقایسه/تفریق با
    تاریخ‌های داخلیو‌شده‌ی بدون تایم‌زون (مثلاً تاریخ انقضای سرویس configs.expiry) لازم است."""
    return now_tehran().replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 📅 تبدیل میلادی → شمسی (جلالی) بدون هیچ وابستگی/پکیج بیرونی جدید (مثل
# jdatetime)، چون اضافه‌کردن یک پکیج جدید به requirements.txt می‌تواند دیپلوی
# روی Render را (اگر فراموش شود) بشکند. الگوریتم استاندارد تبدیل گرگوری به
# جلالی (بر پایه‌ی الگوریتم معروف؛ صحیح برای بازه‌ی سال‌های ۱۹۰۰ تا ۲۱۰۰ میلادی
# که برای این ربات کاملاً کافی است).
# ---------------------------------------------------------------------------
_JALALI_DAYS_IN_MONTH = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]


def gregorian_to_jalali(g_year: int, g_month: int, g_day: int) -> tuple[int, int, int]:
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gy = g_year - 1600
    gm = g_month - 1
    gd = g_day - 1

    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for i in range(gm):
        g_day_no += g_days_in_month[i]
    if gm > 1 and ((g_year % 4 == 0 and g_year % 100 != 0) or (g_year % 400 == 0)):
        g_day_no += 1
    g_day_no += gd

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365

    for i in range(11):
        if j_day_no < _JALALI_DAYS_IN_MONTH[i]:
            jm = i + 1
            jd = j_day_no + 1
            break
        j_day_no -= _JALALI_DAYS_IN_MONTH[i]
    else:
        jm = 12
        jd = j_day_no + 1

    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """🆕 معکوسِ gregorian_to_jalali بالا (برای «مشاهده‌ی آمار در تاریخ
    مشخص» — ادمین تاریخ را به شمسی وارد می‌کند و باید به بازه‌ی میلادی
    برای کوئری روی created_at تبدیل شود). همان الگوریتم استاندارد، بدون
    نیاز به هیچ پکیج بیرونی."""
    jy -= 979
    jm -= 1
    jd -= 1

    j_day_no = 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4
    for i in range(jm):
        j_day_no += _JALALI_DAYS_IN_MONTH[i]
    j_day_no += jd

    g_day_no = j_day_no + 79

    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no %= 146097

    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False

    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461

    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no %= 365

    g_days_in_month = [31, 29 if (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)) else 28,
                        31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 12 and g_day_no >= g_days_in_month[gm]:
        g_day_no -= g_days_in_month[gm]
        gm += 1
    return gy, gm + 1, g_day_no + 1


def stat_period_bounds_utc(period: str, custom_dt: "datetime | None" = None) -> "tuple[str | None, str | None]":
    """🆕 بند ۲۳ — بازه‌ی [شروع, پایان) یک دوره‌ی آماری را بر حسب زمان سرور
    (که طبق مستندات همین فایل معادل UTC فرض شده — نگاه کن به now_tehran)
    برمی‌گرداند، ولی مرزهای «روز»/«ماه» بر اساس تقویم *تهران* محاسبه می‌شوند
    (نه UTC خام)، چون «امروز» برای ادمین یعنی امروزِ تهران، نه امروزِ UTC که
    ممکن است چند ساعت جلوتر/عقب‌تر باشد.

    خروجی None یعنی بدون محدودیت (برای period == "all").
    """
    fmt = "%Y-%m-%d %H:%M:%S"

    def to_utc_str(dt_tehran_naive: datetime) -> str:
        aware = dt_tehran_naive.replace(tzinfo=TEHRAN_TZ)
        return aware.astimezone(timezone.utc).replace(tzinfo=None).strftime(fmt)

    now_t = now_tehran().replace(tzinfo=None)
    today_start = now_t.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "all":
        return None, None
    if period == "hour":
        return to_utc_str(now_t - timedelta(hours=1)), to_utc_str(now_t)
    if period == "today":
        return to_utc_str(today_start), to_utc_str(now_t)
    if period == "yesterday":
        y_start = today_start - timedelta(days=1)
        return to_utc_str(y_start), to_utc_str(today_start)
    if period == "this_month":
        jy, jm, _ = gregorian_to_jalali(today_start.year, today_start.month, today_start.day)
        gy, gm, gd = jalali_to_gregorian(jy, jm, 1)
        month_start = datetime(gy, gm, gd)
        return to_utc_str(month_start), to_utc_str(now_t)
    if period == "last_month":
        jy, jm, _ = gregorian_to_jalali(today_start.year, today_start.month, today_start.day)
        jm -= 1
        if jm == 0:
            jm, jy = 12, jy - 1
        gy, gm, gd = jalali_to_gregorian(jy, jm, 1)
        this_gy, this_gm, this_gd = jalali_to_gregorian(*((jy, jm + 1, 1) if jm < 12 else (jy + 1, 1, 1)))
        return to_utc_str(datetime(gy, gm, gd)), to_utc_str(datetime(this_gy, this_gm, this_gd))
    if period == "custom" and custom_dt is not None:
        day_start = custom_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return to_utc_str(day_start), to_utc_str(day_start + timedelta(days=1))
    return None, None


def parse_jalali_date(text: str) -> "datetime | None":
    """رشته‌ی تاریخ شمسی به‌فرم ۱۴۰۴/۰۶/۱۱ یا ۱۴۰۴-۰۶-۱۱ را به datetime
    میلادی (ساعت ۰۰:۰۰) تبدیل می‌کند؛ اگر فرمت نامعتبر بود None برمی‌گرداند."""
    import re
    m = re.match(r"^\s*(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s*$", (text or "").strip())
    if not m:
        return None
    jy, jm, jd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= jm <= 12 and 1 <= jd <= 31):
        return None
    try:
        gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
        return datetime(gy, gm, gd)
    except Exception:
        return None


_JALALI_MONTH_NAMES = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def to_jalali_str(dt: datetime, with_time: bool = True) -> str:
    """یک datetime میلادی را به رشته‌ی تاریخ شمسی (مثل ۱۴۰۴/۰۶/۱۱ ۱۸:۳۰) تبدیل می‌کند."""
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    date_part = f"{jy:04d}/{jm:02d}/{jd:02d}"
    if with_time:
        return f"{date_part} {dt.strftime('%H:%M')}"
    return date_part


def _fa_digits(v) -> str:
    return str(v).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def humanize_duration(seconds: float) -> str:
    """مدت‌زمان به فارسی: «۳ دقیقه و ۱۲ ثانیه» / «۲ ساعت و ۵ دقیقه» / «۱ روز و ۳ ساعت»."""
    try:
        sec = max(0, int(seconds))
    except Exception:
        return "—"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        out = f"{d} روز" + (f" و {h} ساعت" if h else "")
    elif h:
        out = f"{h} ساعت" + (f" و {m} دقیقه" if m else "")
    elif m:
        out = f"{m} دقیقه" + (f" و {s} ثانیه" if s else "")
    else:
        out = f"{s} ثانیه"
    return _fa_digits(out)


def delivery_time_vars(created_at=None) -> dict:
    """متغیرهای زمانی متن تحویل سرویس: زمان ثبت سفارش، زمان تحویل و مدت بین آن‌ها.
    اگر زمان ثبت سفارش معلوم نباشد (ارسال دستی بدون سفارش) مقدارها «—» می‌شوند."""
    now = now_tehran_naive()
    start = None
    try:
        if created_at:
            start = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            # created_at در دیتابیس با datetime.now() (ساعت محلیِ سرور، naive) ذخیره می‌شود؛ سرور لزوماً
            # به وقت تهران نیست، پس اول با ساعت محلی سرور aware می‌شود و بعد به تهران تبدیل می‌شود.
            # (قبلاً naive مستقیم «وقت تهران» فرض می‌شد و زمان ثبت سفارش اشتباه نمایش داده می‌شد.)
            start = start.astimezone(ZoneInfo("Asia/Tehran")).replace(tzinfo=None)
    except Exception:
        start = None
    return {
        # ثانیه هم نمایش داده می‌شود (۱۴۰۵/۰۷/۲۸ ۲۲:۴۲:۱۷)
        "order_time": _fa_digits(to_jalali_str(start, with_time=True) + start.strftime(":%S")) if start else "—",
        "delivery_time": _fa_digits(to_jalali_str(now, with_time=True) + now.strftime(":%S")),
        "elapsed": humanize_duration((now - start).total_seconds()) if start else "—",
    }


def to_jalali_str_long(dt: datetime, with_time: bool = True) -> str:
    """مثل to_jalali_str ولی با نام فارسی ماه، مثل «۱۱ شهریور ۱۴۰۴ - ۱۸:۳۰»."""
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    date_part = f"{jd} {_JALALI_MONTH_NAMES[jm - 1]} {jy}"
    if with_time:
        return f"{date_part} - {dt.strftime('%H:%M')}"
    return date_part


def utc_str_to_tehran(dt_str: str, fmt_in: str = "%Y-%m-%d %H:%M:%S"):
    """یک رشته‌ی زمانی که با ساعت UTC سرور ذخیره شده (مثل expires_at فاکتورها) را
    به یک datetime بر اساس ساعت تهران تبدیل می‌کند. در صورت خطا None برمی‌گرداند."""
    try:
        dt_utc = datetime.strptime(dt_str, fmt_in).replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(TEHRAN_TZ)
    except Exception:
        return None


_DIGIT_MAP = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹" "٠١٢٣٤٥٦٧٨٩",
    "0123456789" "0123456789",
)

# ---------------------------------------------------------------------------
# قفل سبک برای جلوگیری از اجرای دوباره‌ی یک عملیات مالی/سفارش در بازه‌ی کوتاه.
#
# چرا لازم است؟ اگر ربات به هر دلیلی (مثلاً خوابیدن سرویس رایگان Render) چند
# ثانیه/دقیقه بی‌پاسخ بماند، تلگرام همه‌ی تاچ‌هایی که کاربر پشت سر هم زده را
# صف می‌کند و وقتی ربات بیدار شد، همه را یک‌جا تحویل می‌دهد. هرکدام از این
# callbackهای صف‌شده، یک نسخه‌ی «قدیمی» از پیام (قبل از هر ویرایشی) همراه خودش
# دارد؛ پس چک‌کردن متن پیام (مثلاً «آیا قبلاً تأیید شده؟») برای تشخیص تکراری
# بودن کافی نیست، چون همه‌ی نسخه‌های صف‌شده متن قدیمی یکسانی دارند.
# این قفل با کلید مشخص (کاربر+نوع عملیات) و بدون هیچ await قبل از خودش صدا
# زده می‌شود تا واقعاً به‌عنوان یک بخش اتمیک (غیرقابل‌قطع توسط تسک دیگر asyncio)
# عمل کند.
# ---------------------------------------------------------------------------
_recent_actions: dict[str, float] = {}
_ACTION_COOLDOWN_SECONDS = 15.0


def is_duplicate_action(key: str, cooldown: float = _ACTION_COOLDOWN_SECONDS) -> bool:
    """اگر همین کلید در `cooldown` ثانیه‌ی اخیر پردازش شده باشد True برمی‌گرداند.
    باید همیشه به‌عنوان اولین خط سینک هندلر (قبل از هر await) صدا زده شود."""
    now = time.monotonic()
    last = _recent_actions.get(key)
    _recent_actions[key] = now
    if len(_recent_actions) > 5000:
        cutoff = now - cooldown
        for k in [k for k, t in _recent_actions.items() if t < cutoff]:
            _recent_actions.pop(k, None)
    return last is not None and (now - last) < cooldown


def normalize_digits(text: str) -> str:
    """ارقام فارسی/عربی را به انگلیسی تبدیل می‌کند تا int() و isdigit() درست کار کنند."""
    if not text:
        return text
    return text.translate(_DIGIT_MAP)


# 🐛 فیکس: کیبورد فارسی/عربی گوشی‌ها (به‌خصوص iOS) اغلب هنگام تایپ عدد،
# نویسه‌های نامرئی جهت‌ساز/فرمت‌دهنده (RLM, LRM, ALM, ZWNJ, ZWSP, BOM) را
# قبل/بعد/وسط رقم‌ها اضافه می‌کنند. این نویسه‌ها با چشم دیده نمی‌شوند ولی باعث
# می‌شوند isdigit()/int() آن رشته شکست بخورد.
_INVISIBLE_CHARS_MAP = dict.fromkeys(
    ord(ch) for ch in "\u200b\u200c\u200d\u200e\u200f\u061c\ufeff\u202a\u202b\u202c\u202d\u202e"
)


def clean_numeric_id(text: str) -> str:
    """ورودی مثل آیدی عددی/مبلغ را پاک‌سازی می‌کند: ارقام فارسی/عربی را به انگلیسی
    تبدیل و نویسه‌های نامرئی جهت‌ساز/فرمت‌دهنده (RLM, LRM, ALM, ZWNJ, ZWSP, BOM و...) را حذف
    می‌کند تا isdigit()/int() روی مقداری که کاربران از کی‌بورد‌های فارسی/عربی تایپ می‌کنند
    (مثلاً آیدی عددی ادمین، مبلغ شارج، کد تخفیف) درست کار کند.
    """
    if not text:
        return text
    cleaned = text.translate(_INVISIBLE_CHARS_MAP)
    return normalize_digits(cleaned).strip()


def parse_int_in_range(text: str, min_value: int, max_value: int) -> int | None:
    """متن را به عدد صحیح تبدیل می‌کند و اگر در بازه‌ی مجاز نبود None برمی‌گرداند."""
    if not text:
        return None
    cleaned = normalize_digits(text).strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if not (min_value <= value <= max_value):
        return None
    return value


# ---------------------------------------------------------------------------
# 🎲 جلوگیری از مسدودی کارت بانکی به‌خاطر واریزی‌های زیاد با مبلغ کاملاً یکسان:
# وقتی خیلی از کاربران مختلف دقیقاً یک مبلغ ثابت (مثلاً همیشه ۵۸,۵۰۰ تومان) را
# به یک کارت شخصی واریز می‌کنند، سیستم‌های تشخیص تقلب بانک این را الگوی
# «پرداخت‌یاری/پول‌شویی» تشخیص می‌دهد و کارت را مسدود می‌کند. برای پیشگیری،
# به‌جای مبلغ ثابت هر پلن/شارژ، هر فاکتور کارت‌به‌کارت مبلغی کمی متفاوت (به‌طور
# رندوم ± ۱۰۰ تا ۱۵۰۰ تومان) نشان می‌دهد؛ همین مبلغِ رندوم‌شده (نه مبلغ پایه)
# در فاکتور ذخیره، به کاربر نمایش داده و برای تشخیص/تأیید رسید استفاده می‌شود.
def apply_random_amount_variation(price: int, min_variation: int = 100, max_variation: int = 1500) -> int:
    """مبلغ نهایی یک فاکتور کارت‌به‌کارت را برمی‌گرداند: مبلغ پایه به‌همراه یک
    افزایش یا کاهش تصادفی بین min_variation و max_variation تومان، تا مبلغ
    واریزی کاربران مختلف برای همان پلن/مبلغ شارژ، کاملاً یکسان نباشد."""
    if price is None or price <= 0:
        return price
    try:
        import database as _db
        if not _db.card_option("random_amount"):
            return price  # ادمین رندوم‌سازی را خاموش کرده: مبلغ رند/پایه
    except Exception:
        pass
    variation = random.randint(min_variation, max_variation)
    # اگر کم‌کردن مبلغ آن را منفی/خیلی کوچک می‌کرد، همیشه رو به بالا رندوم می‌کنیم.
    if random.random() < 0.5 and price - variation > 0:
        variation = -variation
    return price + variation


# ---------------------------------------------------------------------------
# ⏰ مدیریت فاکتور/مهلت پرداخت و نمایش پیشرفت مرحله‌ای در پیام‌های ربات


# ---------------------------------------------------------------------------
# ⏰ مدیریت فاکتور/مهلت پرداخت و نمایش پیشرفت مرحله‌ای در پیام‌های ربات

def invoice_expiry_note(kind: str, deadline: str = "") -> str:
    """جمله‌ی مهلت ۳۰ دقیقه‌ای فاکتور دستی (کارت‌به‌کارت/ارز دیجیتال)؛ قابل ویرایش با کلیدهای invoice_*_expiry."""
    import ui_editor
    if kind == "crypto":
        return "⏱ این فاکتور تا ۳۰ دقیقه معتبر است. لطفاً تا این مهلت رسید یا هش تراکنش را ارسال کنید، وگرنه فاکتور به‌طور خودکار منقضی و حذف می‌شود."
    key = {"wallet": "invoice_wallet_expiry", "custom": "invoice_custom_expiry"}.get(kind, "invoice_card_expiry")
    t = ui_editor.get_text(key, "⏱ این شماره کارت تا ساعت {deadline} (۳۰ دقیقه) معتبر است.") or ""
    return t.replace("{deadline}", str(deadline))


def format_deadline_time(expires_at: str) -> str:
    """از رشته YYYY-MM-DD HH:MM:SS (که با ساعت UTC سرور ذخیره شده) ساعت:دقیقه را
    بر اساس ساعت تهران برمی‌گرداند (مثلاً برای پیام «فاکتور تا فلان ساعت معتبر است»)."""
    dt_tehran = utc_str_to_tehran(expires_at)
    if dt_tehran is not None:
        return dt_tehran.strftime("%H:%M")
    try:
        return expires_at.split(" ")[1][:5]
    except Exception:
        return ""


def progress_bar(step: int, total: int) -> str:
    """یک نوار پیشرفت ایموجی‌ای ساده برای نمایش مرحله X از Y در پیام‌های مسیر خرید."""
    step = max(1, min(step, total))
    filled = "🟩" * step + "⬜️" * (total - step)
    label = filled + " مرحله " + str(step) + " از " + str(total)
    return label + chr(10) + chr(10)


# ---------------------------------------------------------------------------
# 🧪 تست: نمایش یک استیکر (ویدیویی) درست بالای یک منو، به‌ازای هر مرحله از
# مسیر خرید. هر بار که این تابع دوباره برای همان چت صدا زده شود، آخرین جفت
# «استیکر + پیام منو»یی که خودش قبلاً فرستاده حذف می‌شود و استیکر/منوی جدید
# جای آن‌ها می‌نشیند؛ یعنی همیشه استیکرِ مرحله‌ی فعلی، درست بالای منوی همان
# مرحله دیده می‌شود.
#
# عمداً این وضعیت (شناسه‌ی پیام استیکر/منوی قبلی) در یک دیکشنری ساده در حافظه
# نگه داشته می‌شود، نه در FSMContext؛ چون خیلی از handlerها همین وسط
# state.clear() صدا می‌زنند (برای پاک کردن state/دیتای قبلی) و اگر این
# اطلاعات را داخل state ذخیره می‌کردیم، با هر state.clear() گم می‌شد و دیگر
# نمی‌توانستیم پیام قبلی را برای حذف پیدا کنیم.
# 🆕 دیگر برای ردیابی پیام قبلی جهت حذف استفاده نمی‌شود (طبق درخواست صریح:
# پیام‌های قبلی دیگر حذف نمی‌شوند)، پس این دیکشنری هم حذف شد تا برای هر چت
# فعال بی‌جهت تو حافظه نگه‌داری نشه.

# ---------------------------------------------------------------------------
# ⚡ کش‌های حافظه‌ای برای رفع کندی: قبلاً هر بار جابه‌جایی بین منوها باعث
# می‌شد (۱) فایل استیکر پیش‌فرض دوباره از روی دیسک به تلگرام آپلود شود و
# (۲) یک کوئری دیتابیس برای بررسی سفارشی‌سازی ادمین اجرا شود (که وقتی
# دیتابیس روی سرویس ابری/Turso است یعنی یک رفت‌وبرگشت شبکه‌ای اضافه). با کش
# کردن نتیجه‌ی این دو در حافظه، این هزینه‌ها از مسیر اصلی هر جابه‌جایی حذف
# می‌شوند. کش سفارشی‌سازی ادمین فقط با صدا زدن invalidate_section_sticker_cache
# (بعد از هر تغییر در پنل ادمین) باطل می‌شود.
_default_sticker_file_id_cache: dict[str, str] = {}
_section_sticker_override_cache: dict[str, dict | None] = {}
_section_sticker_override_cache_loaded: set[str] = set()


def invalidate_section_sticker_cache(section_key: str) -> None:
    """باید بعد از هر تغییر ادمین روی استیکر یک بخش (آپلود/غیرفعال/فعال/ریست)
    صدا زده شود تا کش حافظه‌ای به‌روز شود و تغییر بلافاصله برای کاربران اعمال شود."""
    _section_sticker_override_cache.pop(section_key, None)
    _section_sticker_override_cache_loaded.discard(section_key)


# نگاشت یک کلید کوتاه و معنادار (که در کد handlerها استفاده می‌شود) به نام
# فایل واقعی استیکر روی دیسک (پوشه‌ی stickers/ کنار همین پروژه).
STICKERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stickers")
STICKER_FILES = {
    "free_test": "test.webm",       # دکمه‌ی «🎁 تست رایگان»
    "buy_plans": "service.webm",    # دکمه‌ی «🛒 خرید اشتراک»
    "plan_select": "plan.webm",     # انتخاب پلن VIP
    "custom_build": "make.webm",    # دکمه‌ی «🚀 کانفیگ خودتو بساز»
}

# عنوان فارسی قابل‌نمایش هر بخش، برای استفاده در پنل مدیریت استیکرها (handlers/admin.py).
STICKER_SECTION_LABELS = {
    # ===== 👤 کاربر =====
    "start_welcome":          "👋 شروع با /start",
    "join_confirmed":         "✅ تایید عضویت در کانال‌ها",
    "free_test":              "🎁 تست رایگان",
    "buy_plans":              "🛒 خرید اشتراک",
    "plan_select":            "🚀 انتخاب پلن VIP",
    "custom_build":           "🛠 بساز کانفیگ خودت",
    "my_configs_empty":       "📱 سرویس‌های من (خالی)",
    "my_configs_has":         "📱 سرویس‌های من (دارای سرویس)",
    "wallet":                 "💰 کیف پول",
    "wallet_charge":          "💵 شارژ کیف پول",
    "referral":               "👥 دعوت دوستان",
    "profile":                "👤 پروفایل من",
    "guides_empty":           "📚 راهنما (خالی)",
    "guides_has":             "📚 راهنما (دارای محتوا)",
    "support":                "👨‍💻 پشتیبانی",
    "agency_request":         "🤝 درخواست نمایندگی",
    "vip_category_list":      "🚀 لیست پلان‌های VIP",
    "discount_code_entry":    "🎟 ورود کد تخفیف",
    "my_configs_list_empty":  "📋 لیست سرویس‌ها (خالی)",
    "my_configs_list_has":    "📋 لیست سرویس‌ها",
    "config_detail":          "📦 جزئیات یک سرویس",
    "config_delete_confirm":  "🗑 تأیید حذف سرویس",
    "renew_menu":             "🔁 تمدید سرویس",
    "renew_done":             "✅ تمدید موفق",
    "config_delivery":        "📦 تحویل کانفیگ",
    "order_log":              "📋 لاگ سفارش",
    "agent_manage":           "🤝 مدیریت نمایندگی",
    "ticket_write":           "✍️ نوشتن پیام تیکت",
    "plan_payment_method":    "💳 انتخاب روش پرداخت (خرید پلن)",
    "plan_pay_wallet":        "👛 پرداخت با کیف پول",
    "plan_pay_card":          "💳 پرداخت کارت‌به‌کارت",
    "plan_pay_online":        "🌐 پرداخت آنلاین",
    "cbuild_payment_method":  "💳 انتخاب روش پرداخت (بساز کانفیگ)",
    "walletcharge_method":    "💳 انتخاب روش شارژ کیف پول",
    "receipt_submitted":      "🧧 ارسال رسید پرداخت",
    "payment_success":        "✅ پرداخت موفق",
    "payment_failed":         "❌ پرداخت ناموفق",
    "insufficient_balance":   "❌ موجودی ناکافی",
    "notif_expiry":           "⏰ هشدار پایان سرویس",
    "notif_usage_80":         "🔔 هشدار مصرف 80% حجم",
    "notif_gift":             "🎁 هدیه دریافتی",
    "notif_referral_success": "🎉 دعوت موفق",
    "notif_payg_empty":       "⚠️ اتمام موجودی PAYG",
    "notif_wallet_low":       "🪫 کم‌شدن موجودی کیف پول",
    "notif_usage_90":         "🚨 هشدار مصرف 90% حجم",
    "notif_ended":            "⛔️ اتمام حجم/زمان سرویس",
    "notif_wallet_charge":    "💰 شارژ کیف پول (اعلان به کاربر)",
    "notif_purchase_approved": "✅ تأیید پرداخت خرید (کارت/ارز)",
    "notif_receipt_rejected": "❌ رد رسید پرداخت",
    "notif_service_delivery": "📦 ارسال کانفیگ (دستی و خودکار)",
    "notif_personal_message": "💌 پیام شخصی ادمین",
    "notif_broadcast":        "📢 پیام همگانی",
    "notif_ticket_reply":     "🎫 پاسخ تیکت",
    "cbuild_pay_wallet":      "👛 پرداخت با کیف پول (بساز کانفیگ)",
    "cbuild_pay_card":        "💳 پرداخت کارت‌به‌کارت (بساز کانفیگ)",
    "cbuild_pay_online":      "🌐 پرداخت آنلاین (بساز کانفیگ)",
    "walletcharge_pay_card":  "💳 شارژ کیف پول (کارت‌به‌کارت)",
    "walletcharge_pay_online": "🌐 شارژ کیف پول (آنلاین)",
    "walletcharge_pay_crypto": "💎 شارژ کیف پول (ارز دیجیتال)",
    "notif_order_approved":   "✅ سفارش تأیید شد",
    "notif_order_rejected":   "❌ سفارش رد شد",
    "service_active":         "🟢 سرویس فعال",
    "service_expired":        "🔴 سرویس منقضی",
    "service_near_expiry":    "⏰ سرویس در آستانه انقضا",
    "error_general":          "🙅 خطای عمومی",
    "error_payment":          "❌ خطای پرداخت",
    "error_service_full":     "⚠️ ظرفیت پر شده",
    "success_purchase":       "✅ خرید موفق",
    "success_renewal":        "✅ تمدید موفق",
    "success_test":           "✅ تست فعال شد",
    "admin_broadcast_sent":   "📢 ارسال پیام همگانی (ادمین)",
    "admin_new_order":        "📋 سفارش جدید (ادمین)",
    "admin_new_ticket":       "🎫 تیکت جدید (ادمین)",
    "admin_gift_sent":        "🎁 هدیه ارسال شد (ادمین)",
}



async def _delete_messages_in_background(bot, chat_id: int, message_ids: list[int]) -> None:
    """(دیگر صدا زده نمی‌شود — طبق درخواست صریح، پیام/منو/استیکر مرحله‌ی قبل
    دیگر حذف نمی‌شود، همون‌طور بمونه مشکلی نداره. تابع را فقط برای سازگاری
    نگه داشتیم، چون حذف‌کردن AsyncIO Task اضافه‌ای بود که خودش سهم کوچکی از
    فشار روی connection pool مشترک aiohttp داشت.)"""
    for msg_id in message_ids:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


def _get_section_sticker_override(sticker_key: str) -> dict | None:
    """نسخه‌ی کش‌شده‌ی db.get_section_sticker؛ قبلاً این کوئری روی هر جابه‌جایی
    منو (حتی وقتی ادمین چیزی سفارشی نکرده بود) اجرا می‌شد و به‌خصوص وقتی دیتابیس
    روی سرویس ابری (Turso) است، کندی محسوسی اضافه می‌کرد."""
    if sticker_key in _section_sticker_override_cache_loaded:
        return _section_sticker_override_cache.get(sticker_key)
    override = None
    try:
        import database as db  # lazy import: از وابستگی حلقوی بین ماژول‌ها جلوگیری می‌شود
        override = db.get_section_sticker(sticker_key)
    except Exception:
        logger.exception("خطا در خواندن تنظیمات استیکر بخش '%s' از دیتابیس", sticker_key)
    _section_sticker_override_cache[sticker_key] = override
    _section_sticker_override_cache_loaded.add(sticker_key)
    return override


# ---------------------------------------------------------------------------
# 💎 ترمیم خودکار ایموجی پرمیوم (custom_emoji entities)
# دقیقاً عیناً از معماری پروژه‌ی premium_bot_v6 پورت شده. مشکلی که حل می‌کند:
# وقتی متن یک صفحه توسط ادمین یا از طریق render_template دوباره ساخته می‌شود،
# آفست (offset) ذخیره‌شده‌ی هر custom_emoji ممکن است دیگر با متن جدید هم‌خوان
# نباشد (چون طول قسمت‌های قبل از آن عوض شده) — نتیجه یا ایموجی اشتباه نمایش
# داده می‌شود یا کلاً تلگرام کل پیام را با خطای ENTITY_TEXT_INVALID رد می‌کند.
# راه‌حل: برای هر custom_emoji، شکل «آلترناتیو متنی» آن (همان اموجی معمولی که
# custom_emoji روی آن سوار شده) از تلگرام گرفته و کش می‌شود؛ بعد در متن فعلی
# دنبال نزدیک‌ترین رخداد همان اموجی معمولی می‌گردیم و offset واقعی را از روی
# همان محاسبه می‌کنیم — نه از روی offset ذخیره‌شده‌ی قدیمی.
# ---------------------------------------------------------------------------
def telegram_utf16_length(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def _entity_span_is_valid(text: str, ent) -> bool:
    """اعتبارسنجی یک entity در برابر متن فعلی (بر حسب واحد UTF-16 که تلگرام
    استفاده می‌کند)؛ نباید هیچ مرزی وسط یک surrogate pair بیفتد."""
    try:
        off = int(ent.offset); length = int(ent.length)
    except Exception:
        return False
    if off < 0 or length <= 0:
        return False
    total = telegram_utf16_length(text)
    end = off + length
    if end > total:
        return False

    def boundary_ok(units):
        used = 0
        for ch in text:
            nxt = used + (2 if ord(ch) > 0xFFFF else 1)
            if units == used or units == nxt:
                return True
            if used < units < nxt:
                return False
            used = nxt
        return units == used

    return boundary_ok(off) and boundary_ok(end)


def _sanitize_entities_for_text(text: str, entities) -> list:
    """فقط entity هایی که span شان روی متن فعلی معتبر است نگه داشته می‌شود."""
    from aiogram.types import MessageEntity
    out = []
    for raw in entities or []:
        try:
            ent = MessageEntity(**raw) if isinstance(raw, dict) else raw
            if _entity_span_is_valid(text, ent):
                out.append(ent)
        except Exception:
            continue
    return out


async def _repair_custom_emoji_entities(bot, text: str, entities):
    """آفست ایموجی‌های پرمیوم ذخیره‌شده را بعد از رندر مجدد متن ترمیم می‌کند."""
    valid = _sanitize_entities_for_text(text, entities)
    custom = []
    result = []
    for ent in valid:
        typ = ent.type.value if hasattr(ent.type, "value") else str(ent.type)
        if typ == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            custom.append(ent)
        else:
            result.append(ent)
    if not custom:
        return result

    cache = getattr(bot, "_premium_emoji_alt_cache", None)
    if cache is None:
        cache = {}
        try: setattr(bot, "_premium_emoji_alt_cache", cache)
        except Exception: pass

    ids = [str(e.custom_emoji_id) for e in custom if str(e.custom_emoji_id) not in cache]
    if ids:
        try:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids)
            for sticker in stickers or []:
                sid = getattr(sticker, "custom_emoji_id", None)
                alt = getattr(sticker, "emoji", None)
                if sid and alt:
                    cache[str(sid)] = str(alt)
        except Exception:
            pass

    def u16(s):
        return len((s or "").encode("utf-16-le")) // 2

    def py_from_u16(s, units):
        used = 0
        for i, ch in enumerate(s):
            if used >= units:
                return i
            used += 2 if ord(ch) > 0xFFFF else 1
            if used >= units:
                return i + 1
        return len(s)

    def _slice_u16(s, off, length):
        b = (s or "").encode("utf-16-le")
        try:
            return b[off * 2:(off + length) * 2].decode("utf-16-le")
        except Exception:
            return ""

    def _emoji_like(piece: str) -> bool:
        piece = (piece or "").strip()
        if not piece:
            return False
        return all((not ch.isalnum()) and ch not in "{}()[]<>.,:;!?-_/\\|" and (ord(ch) >= 0x2190 or ch in "\u200d\u20e3\ufe0f\u00a9\u00ae\u2122") for ch in piece)

    for ent in custom:
        sid = str(ent.custom_emoji_id)
        # 🛡 اگر خود offset (که render_template دقیق جابه‌جا کرده) هم‌اکنون روی یک
        # ایموجی/نماد قرار دارد، دست‌نخورده می‌ماند. قبلاً alt استیکر در کل متن
        # جستجو می‌شد و اگر با نویسه‌ی جایگزین (placeholder) فرق داشت، چند ایموجی
        # روی یک نقطه‌ی دیگر می‌افتادند و تلگرام همه را نمایش نمی‌داد.
        if _entity_span_is_valid(text, ent) and _emoji_like(_slice_u16(text, int(ent.offset), int(ent.length))):
            result.append(ent)
            continue
        alt = cache.get(sid)
        if alt:
            hits = []
            start = 0
            while True:
                pos = text.find(alt, start)
                if pos < 0:
                    break
                hits.append(pos)
                start = pos + 1
            if hits:
                old_py = py_from_u16(text, int(ent.offset))
                pos = min(hits, key=lambda x: abs(x - old_py))
                result.append(ent.model_copy(update={"offset": u16(text[:pos]), "length": u16(alt)}))
                continue
        if _entity_span_is_valid(text, ent):
            result.append(ent)
    return result


def drop_matching_lines(text: str, entities, pattern: str):
    """خط‌هایی از متن که با regex تطبیق دارند را حذف می‌کند و offset/length همه‌ی entityها را (UTF-16) درست می‌کند."""
    import re as _re
    if not text:
        return text, entities
    rx = _re.compile(pattern)
    u16 = lambda s: len(s.encode("utf-16-le")) // 2
    lines = text.split("\n")
    out, removed, pos = [], [], 0
    for i, line in enumerate(lines):
        span = u16(line) + (1 if i < len(lines) - 1 else 0)
        if rx.search(line):
            removed.append((pos, pos + span))
        else:
            out.append(line)
        pos += span
    if not removed:
        return text, entities
    new_text = "\n".join(out)
    new_ents = []
    for e in (entities or []):
        s, t = int(e.offset), int(e.offset) + int(e.length)
        shift_s = sum(min(b, s) - a for a, b in removed if a < s)
        cut = sum(max(0, min(b, t) - max(a, s)) for a, b in removed)
        ns, nl = s - shift_s, (t - s) - cut
        if nl > 0:
            try:
                e = e.model_copy(update={"offset": ns, "length": nl})
            except Exception:
                e.offset, e.length = ns, nl
            new_ents.append(e)
    return new_text, (new_ents or None) if entities else entities


async def show_menu_with_sticker(
    bot,
    chat_id: int,
    sticker_key: str | None,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    show_main_keyboard: bool = True,
    ui_key: str | None = None,
    template_values: dict | None = None,
    text_entities: list | None = None,
    skip_ui_text: bool = False,
    rich_payload: dict | None = None,
    drop_lines_regex: str | None = None,
    append_note: str | None = None,
):
    """یک پیام منوی تازه می‌فرستد (همیشه پیام جدید، نه ویرایش پیام قبلی) و اگر
    sticker_key داده شده باشد، درست بالای همان منو یک استیکر می‌فرستد.

    🚀 پرفورمنس: اگر استیکر پیش‌فرض (غیرسفارشی) باشد، فقط دفعه‌ی اول از روی دیسک
    آپلود می‌شود و file_id برگشتی‌شده تلگرام در حافظه کش می‌شود؛ دفعات بعدی فقط
    همان file_id را می‌فرستد (بدون خواندن دوباره‌ی فایل از دیسک)؛ همین بزرگ‌ترین عامل کندی قبلی بود.

    ✅ منوی دائمی پایین صفحه (main_reply_keyboard) همراه با هر استیکری که اینجا فرستاده
    می‌شود دوباره تازه می‌شود؛ قبلاً فقط در /start فرستاده می‌شد و با حذف همان پیام در
    جابه‌جایی بعدی از دید کاربر گم می‌شد و کاربر مجبور می‌شد دوباره /start بزند.

    ⚠️ show_main_keyboard=False: فقط برای صفحه‌ی «عضویت اجباری در کانال‌ها» (پیش از
    تأیید عضویت) استفاده شود؛ چون هیچ‌کدام از handlerهای منوی پایین صفحه، عضویت
    کاربر را دوباره چک نمی‌کنند، اگر آنجا هم منوی پایین صفحه فعال شود کاربرِ
    هنوز-عضونشده می‌تواند بدون عضویت واقعی از دکمه‌های پایین صفحه استفاده کند.

    پیام‌های قبلی (استیکر/منوی مرحله‌ی قبل) در پس‌زمینه حذف می‌شوند تا کاربر منتظر
    تمام‌شدن حذف نماند و منوی جدید در سریع‌ترین حالت ممکن ظاهر شود. اگر sticker_key
    مقدار None باشد، فقط پیام منو (بدون استیکر جدید) فرستاده می‌شود؛ برای مرحله‌هایی
    که نباید استیکری در آن‌ها نمایش داده شود (مثلاً مرحله‌ی نهایی انتخاب/انجام پرداخت).
    """
    # 🆕 طبق درخواست صریح: پیام/منو/استیکر مرحله‌ی قبل دیگر حذف نمی‌شود (حتی
    # در پس‌زمینه) — بمونه مشکلی نداره. قبلاً هر جابه‌جایی منو یعنی حداقل دو
    # درخواست delete_message اضافه (هرچند پس‌زمینه/غیربلاک‌کننده) که خودشان
    # سهمی از connection pool مشترک aiohttp بات مصرف می‌کردند و روی حجم
    # بالای کاربر همزمان محسوس بود؛ الان کاملاً حذف شده.

    new_sticker_msg_id = None
    if sticker_key:
        # ابتدا بررسی می‌شود که آیا ادمین از پنل ادمین برای این بخش چیزی سفارشی کرده
        # (استیکر خاص یا غیرفعال‌سازی کامل)؛ اگر چیزی سفارشی نشده باشد، از استیکر
        # پیش‌فرض داخل پروژه استفاده می‌شود (از کش در صورت موجود).
        import database as db  # lazy import (وابستگی حلقوی)
        override = _get_section_sticker_override(sticker_key)

        sticker_source = None  # ("file_id", value) یا ("path", value)
        if override is not None:
            if override.get("is_enabled") and override.get("file_id"):
                sticker_source = ("file_id", override["file_id"])
            # اگر ادمین صریحاً این بخش رو غیرفعال کرده باشد (is_enabled=0)، هیچ استیکری نشون داده نمی‌شه.
        else:
            cached_file_id = _default_sticker_file_id_cache.get(sticker_key)
            if cached_file_id:
                sticker_source = ("file_id", cached_file_id)
            else:
                # file_id را بین restartها هم نگه می‌داریم؛ در غیر این صورت اولین
                # درخواست بعد از هر deploy دوباره فایل Sticker را upload می‌کرد.
                try:
                    persisted_file_id = db.get_setting(f"default_sticker_file_id:{sticker_key}", "")
                except Exception:
                    persisted_file_id = ""
                if persisted_file_id:
                    _default_sticker_file_id_cache[sticker_key] = persisted_file_id
                    sticker_source = ("file_id", persisted_file_id)
                else:
                    filename = STICKER_FILES.get(sticker_key)
                    if filename:
                        sticker_path = os.path.join(STICKERS_DIR, filename)
                        # Deployهای Render ممکن است فایل‌های اختیاری استیکر را همراه کد نداشته باشند.
                        # نبود فایل نباید هیچ منوی کاربر را متوقف یا لاگ ERROR تولید کند.
                        if os.path.isfile(sticker_path):
                            sticker_source = ("path", sticker_path)

        if sticker_source:
            sticker_reply_markup = main_reply_keyboard() if show_main_keyboard else None
            try:
                if sticker_source[0] == "file_id":
                    sticker_msg = await bot.send_sticker(
                        chat_id, sticker=sticker_source[1], reply_markup=sticker_reply_markup,
                    )
                else:
                    sticker_msg = await bot.send_sticker(
                        chat_id, sticker=FSInputFile(sticker_source[1]), reply_markup=sticker_reply_markup,
                    )
                    # فقط برای استیکرهای پیش‌فرض (غیرسفارشی) کش می‌شود تا دفعه‌ی
                    # بعد به‌جای آپلود دوباره‌ی فایل از دیسک، همان file_id تلگرام مستقیم
                    # استفاده شود (بزرگ‌ترین عامل کندی قبلی همین بود).
                    if sticker_msg.sticker and sticker_msg.sticker.file_id:
                        file_id = sticker_msg.sticker.file_id
                        _default_sticker_file_id_cache[sticker_key] = file_id
                        try:
                            db.set_setting(f"default_sticker_file_id:{sticker_key}", file_id)
                        except Exception:
                            logger.debug("could not persist sticker file_id for %s", sticker_key, exc_info=True)
                new_sticker_msg_id = sticker_msg.message_id
            except Exception as exc:
                # خطای موقت شبکه/آپلود نباید منوی اصلی یا جریان سفارش را بخواباند.
                # traceback کامل برای خطای شبکه ارزش عملی ندارد و فقط لاگ را پر می‌کند؛
                # خطاهای غیرشبکه‌ای همچنان با traceback ثبت می‌شوند.
                if isinstance(exc, (OSError, aiohttp.ClientError)):
                    logger.warning("ارسال استیکر '%s' به‌دلیل خطای شبکه انجام نشد؛ ادامه بدون استیکر", sticker_key)
                else:
                    logger.exception("خطا در ارسال استیکر '%s'", sticker_key)

    entities = None
    _orig_menu_text = text  # قبل از اعمال override؛ برای تطبیق قالب ریچ‌مسیج
    try:
        import ui_editor
        import rich_registry
        # اگر caller ui_key نداده ولی sticker_key یک صفحه‌ی ثبت‌شده در Editor است،
        # همان صفحه را به‌طور خودکار به متن واقعی وصل کن. این باعث می‌شود هیچ مسیر
        # مشتری فقط به خاطر فراموش شدن ui_key از شخصی‌سازی جا نماند.
        effective_ui_key = ui_key or (sticker_key if sticker_key in ui_editor.SCREENS else None)
        if skip_ui_text:
            # متن از خود caller می‌آید (مثلاً نام پوشه + توضیحاتِ ثبت‌شده با ایموجی پریمیوم/ریچ‌تکست)؛
            # override کلید صفحه نباید آن را عوض کند.
            effective_ui_key = None
            if text_entities:
                _ents = rich_registry.to_message_entities(text_entities)
                if _ents:
                    entities = await _repair_custom_emoji_entities(bot, text, _ents)
                    if entities:
                        parse_mode = None
        if effective_ui_key:
            if template_values:
                text, raw_entities = ui_editor.render_template(
                    effective_ui_key, template_values, fallback=text
                )
            else:
                overridden = ui_editor.get_text(effective_ui_key, text)
                if overridden:
                    text = overridden
                raw_entities = ui_editor.get_entities(effective_ui_key)
            from aiogram.types import MessageEntity
            if raw_entities:
                entities = [MessageEntity(type=e["type"], offset=e["offset"], length=e["length"], custom_emoji_id=e.get("custom_emoji_id")) for e in raw_entities]
                parse_mode = None
                # 💎 ترمیم خودکار آفست ایموجی‌های پرمیوم قبل از ارسال (نگاه کن به توضیح بالای _repair_custom_emoji_entities)
                entities = await _repair_custom_emoji_entities(bot, text, entities)
        # وقتی ui_key صریح داریم، همین مسیر مرجع نهایی متن و Entityهاست؛
        # اجرای دوباره‌ی auto catalog می‌توانست متن را عوض کند و Custom Emojiها را
        # بدون entity بفرستد. فقط پیام‌های بدون ui_key از catalog سراسری استفاده می‌کنند.
        # 🐛 فیکس: اگر صفحه کلید ویرایشگر دارد ولی ادمین متنش را از «کاتالوگ متن‌ها/پیام‌های
        # کشف‌شده» ویرایش کرده باشد (override روی کلید صفحه نیست)، قبلاً هرگز اعمال نمی‌شد و
        # متن/ایموجی پریمیوم عادی می‌ماند. حالا در این حالت هم تطبیق کاتالوگ امتحان می‌شود.
        if effective_ui_key and not entities:
            _t2, _auto2 = ui_editor.apply_auto_text_with_entities(text)
            if _auto2:
                from aiogram.types import MessageEntity
                text = _t2
                entities = [MessageEntity(
                    type=e["type"], offset=e["offset"], length=e["length"],
                    custom_emoji_id=e.get("custom_emoji_id")
                ) for e in _auto2]
                parse_mode = None
        if not effective_ui_key and not skip_ui_text:
            text, auto_entities = ui_editor.apply_auto_text_with_entities(text)
            if auto_entities:
                from aiogram.types import MessageEntity
                entities = [MessageEntity(
                    type=e["type"], offset=e["offset"], length=e["length"],
                    custom_emoji_id=e.get("custom_emoji_id")
                ) for e in auto_entities]
                parse_mode = None
        if drop_lines_regex:
            text, entities = drop_matching_lines(text, entities, drop_lines_regex)
        if append_note and append_note not in text and not ("۳۰ دقیقه" in text and "معتبر" in text):
            # جمله‌ی مهلت ۳۰ دقیقه‌ای فاکتور در کد ساخته می‌شود و با ویرایش متن اصلی حذف نمی‌شود (آفست entityها دست‌نخورده می‌ماند)
            text = text.rstrip() + "\n\n" + append_note
    except Exception:
        logger.exception("ui editor render failed for %s", ui_key or sticker_key)
    if not rich_payload and effective_ui_key:
        try:
            rich_payload = ui_editor.rich_payload_for(effective_ui_key, template_values)
        except Exception:
            logger.exception("rich text lookup failed for %s", effective_ui_key)
    if not rich_payload and not skip_ui_text:
        # 🧾 متن‌هایی که داخل کد ساخته می‌شوند (مثل «🎁 {name} ... تست رایگان» در منو) کلید صفحه ندارند؛
        # ریچ‌مسیجی که ادمین از «کاتالوگ/متن‌های واقعی» ذخیره کرده فقط با تطبیق قالب متن پیدا می‌شود.
        # قبلاً این تطبیق فقط برای کاربران عادی و در Bot.__call__ انجام می‌شد و این مسیر
        # (_skip_auto_text) آن را دور می‌زد، برای همین ریچ‌مسیج ذخیره‌شده نمایش داده نمی‌شد.
        try:
            import ui_editor
            rich_payload = ui_editor.rich_text_match(_orig_menu_text) or ui_editor.rich_text_match(text)
        except Exception:
            logger.exception("rich text match failed for %s", ui_key or sticker_key)
    if rich_payload:
        # 🧾 Rich Message (جدول/تیتر/...) به‌جای متن ساده؛ اگر تلگرام نپذیرفت، متن عادی فرستاده می‌شود.
        try:
            import rich_message
            return await rich_message.send(bot, chat_id, rich_payload, reply_markup=reply_markup)
        except Exception:
            logger.exception("ارسال Rich Message ناموفق بود؛ نمایش متن عادی")
    # 🔒 قفل فوروارد: پیام‌هایی که شماره کارت دارند (کارت‌به‌کارت دستی/کارت درگاه) قابل فوروارد/ذخیره نباشند.
    _protect = None
    try:
        import database as _db2
        if _db2.card_option("forward_lock") and re.search(r"\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}", str(text or "")):
            _protect = True
    except Exception:
        _protect = None
    try:
        menu_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode, entities=entities, protect_content=_protect, _skip_auto_text=True)
    except TelegramBadRequest:
        # تلگرام پیام/کیبورد را رد کرد (مثلاً icon_custom_emoji_id یا entity نامعتبر). به‌جای خطا برای کاربر،
        # یک‌بار بدون ایموجی/entityهای سفارشی دوباره می‌فرستیم.
        logger.exception("send_message رد شد؛ تلاش مجدد بدون ایموجی/entity سفارشی (%s)", ui_key or sticker_key)
        safe_markup = reply_markup
        try:
            if reply_markup is not None and getattr(reply_markup, "inline_keyboard", None):
                from aiogram.types import InlineKeyboardMarkup as _IKM
                safe_markup = _IKM(inline_keyboard=[
                    [b.model_copy(update={"icon_custom_emoji_id": None}) for b in row]
                    for row in reply_markup.inline_keyboard
                ])
        except Exception:
            safe_markup = reply_markup
        menu_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=safe_markup, parse_mode=None, entities=None, protect_content=_protect, _skip_auto_text=True)
    return menu_msg


def make_qr_png_bytes(link: str) -> bytes | None:
    """QR را از روی خود لینک می‌سازد و PNG برمی‌گرداند؛ اگر کتابخانه‌ی qrcode نصب نباشد یا ساخت
    شکست بخورد None برمی‌گرداند تا تحویل سرویس بدون QR انجام شود."""
    if not link or not isinstance(link, str):
        return None
    try:
        import io
        import qrcode
        img = qrcode.make(link)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        return data or None
    except Exception:
        logger.warning("ساخت QR از روی لینک ناموفق بود؛ ارسال بدون QR", exc_info=True)
        return None


async def send_notification_sticker(bot, chat_id: int, sticker_key: str) -> None:
    """برای پیام‌های اطلاع‌رسانیِ تکی (نه مسیر منو) - مثل پیام شخصی ادمین،
    پیام همگانی، هشدار انقضا/مصرف سرویس، شارژ کیف پول توسط ادمین یا ارسال
    سرویس توسط ادمین - اگر ادمین از پنل ادمین (بخش «مدیریت استیکرها») برای
    همین sticker_key چیزی آپلود و فعال کرده باشد، همان استیکر را درست قبل از
    پیام اصلی می‌فرستد.

    برخلاف show_menu_with_sticker:
    - این کلیدها هیچ استیکر پیش‌فرض پروژه‌ای ندارند (در STICKER_FILES نیستند)؛
      یعنی تا وقتی ادمین چیزی آپلود نکند، هیچ استیکری فرستاده نمی‌شود و هیچ
      رفتار فعلی تغییر نمی‌کند.
    - هیچ پیام قبلی حذف نمی‌شود و کیبورد پایین صفحه دوباره فرستاده نمی‌شود،
      چون این پیام‌ها مستقل از مسیر منو و در هر لحظه‌ای ممکن است ارسال شوند.
    - هر خطایی (مثلاً کاربر ربات را بلاک کرده) بی‌صدا نادیده گرفته می‌شود تا
      ارسال پیام اصلی بعد از آن هیچ‌وقت به‌خاطر این استیکر متوقف نشود.
    """
    override = _get_section_sticker_override(sticker_key)
    if not override or not override.get("is_enabled") or not override.get("file_id"):
        return
    try:
        await bot.send_sticker(chat_id, sticker=override["file_id"])
    except Exception:
        logger.exception("خطا در ارسال استیکر اطلاع‌رسانی '%s'", sticker_key)


async def send_user_notification(bot, chat_id, sticker_key: str | None, text, **kwargs):
    """اعلان کاربر: اگر برای sticker_key استیکر فعال باشد اول استیکر، بعد پیام.
    خطای استیکر هرگز مانع پیام نمی‌شود؛ پیام خالی هم ارسال نمی‌شود.
    خروجی: پیام ارسال‌شده یا None (مثلاً کاربر ربات را بلاک کرده)."""
    if sticker_key:
        try:
            await send_notification_sticker(bot, int(chat_id), sticker_key)
        except Exception:
            logger.exception("استیکر اعلان '%s' ارسال نشد", sticker_key)
    if not text:
        return None
    try:
        return await bot.send_message(int(chat_id), text, **kwargs)
    except Exception:
        logger.exception("ارسال اعلان به کاربر %s ناموفق بود", chat_id)
        return None


WALLET_PAID_TEXT = "✅ پرداخت موفق! سرویس شما به‌زودی ساخته و ارسال می‌شود."


# ---------------------------------------------------------------------------
# 🧾 ارسال رسید پرداخت به ادمین (مشترک برای کارت‌به‌کارت، ارزی، هش و شارژ)
# ---------------------------------------------------------------------------
def extract_receipt_media(message):
    """از پیام کاربر، عکس/فایل رسید یا متن (هش/توضیح) را بیرون می‌کشد.
    خروجی: (file_id, file_type, text) — file_type یکی از 'photo' / 'document' / None."""
    file_id, file_type = None, None
    photo = getattr(message, "photo", None)
    document = getattr(message, "document", None)
    if photo:
        file_id, file_type = photo[-1].file_id, "photo"
    elif document and str(getattr(document, "mime_type", "") or "").lower().startswith("image/"):
        file_id, file_type = document.file_id, "document"
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    return file_id, file_type, text


async def send_receipt_card_to_admin(bot, caption: str, reply_markup=None, file_id: str | None = None,
                                     file_type: str | None = None) -> bool:
    """رسید را برای ادمین می‌فرستد؛ دکمه‌های تأیید/رد «زیر خودِ عکس» (روی کپشن) قرار می‌گیرند.
    اگر ارسال با عکس شکست بخورد، به‌جایش پیام متنی (با همان دکمه‌ها) ارسال می‌شود تا هیچ رسیدی گم نشود.
    خروجی True یعنی حداقل یک پیام همراه با دکمه‌ها به ادمین رسید."""
    from config import ADMIN_ID
    caption = (caption or "")
    cap = caption if len(caption) <= 1000 else caption[:997] + "..."
    try:
        if file_id and file_type == "photo":
            await bot.send_photo(ADMIN_ID, file_id, caption=cap, reply_markup=reply_markup)
            return True
        if file_id and file_type == "document":
            await bot.send_document(ADMIN_ID, file_id, caption=cap, reply_markup=reply_markup)
            return True
        await bot.send_message(ADMIN_ID, caption[:4000], reply_markup=reply_markup)
        return True
    except Exception:
        logger.exception("ارسال کارت رسید به ادمین (با رسانه) ناموفق بود؛ تلاش با پیام متنی")
    try:
        if file_id:
            try:
                if file_type == "document":
                    await bot.send_document(ADMIN_ID, file_id)
                else:
                    await bot.send_photo(ADMIN_ID, file_id)
            except Exception:
                logger.exception("ارسال خودِ عکس رسید به ادمین ناموفق بود")
        await bot.send_message(ADMIN_ID, caption[:4000], reply_markup=reply_markup)
        return True
    except Exception:
        logger.exception("ارسال رسید به ادمین کاملاً ناموفق بود")
        return False


def receipt_user_label(user) -> str:
    """«نام (@یوزرنیم)» برای کپشن رسید ادمین."""
    name = getattr(user, "full_name", "") or "-"
    username = getattr(user, "username", None)
    return f"{name} (@{username})" if username else name


async def send_rich_template(bot, chat_id, key: str, values: dict | None, reply_markup=None, photo=None):
    """اگر برای متن «key» (ویرایش متن) ریچ‌مسیج/جدول ذخیره شده باشد، همان را (با جایگزینی متغیرها) می‌فرستد و پیام را برمی‌گرداند؛
    وگرنه (یا اگر تلگرام نپذیرفت) None تا caller متن عادی را بفرستد. photo: عکس داخل خود ریچ‌مسیج."""
    try:
        import ui_editor
        payload = ui_editor.rich_payload_for(key, dict(values or {}))
        if not payload:
            return None
        import rich_message
        return await rich_message.send(bot, chat_id, payload, reply_markup=reply_markup, photo=photo)
    except Exception:
        logger.exception("ارسال ریچ‌مسیج قالب %s ناموفق بود", key)
        return None


async def send_delivery_rich(bot, chat_id, key: str, values: dict | None, reply_markup, qr_bytes: bytes | None):
    """تحویل سرویس با ریچ‌مسیج (جدول): QR داخل خود ریچ‌مسیج (بالای متن) و دکمه‌ها زیرش؛
    اگر تلگرام این حالت را نپذیرفت، ریچ‌مسیج بدون QR + QR جداگانه.
    خروجی: (پیام، file_id QR | None) یا None وقتی ریچ‌مسیجی برای این قالب نیست."""
    from aiogram.types import BufferedInputFile
    if qr_bytes:
        sent = await send_rich_template(bot, chat_id, key, values, reply_markup=reply_markup,
                                        photo=BufferedInputFile(qr_bytes, filename="qr.png"))
        if sent is not None:
            fid = None
            try:
                for blk in (getattr(getattr(sent, "rich_message", None), "blocks", None) or []):
                    ph = getattr(blk, "photo", None)
                    if ph:
                        fid = (ph[-1] if isinstance(ph, list) else ph).file_id
                        break
            except Exception:
                fid = None
            return sent, fid
    sent = await send_rich_template(bot, chat_id, key, values, reply_markup=reply_markup)
    if sent is None:
        return None
    fid = None
    if qr_bytes:
        try:
            ph = await bot.send_photo(chat_id, BufferedInputFile(qr_bytes, filename="qr.png"))
            fid = ph.photo[-1].file_id if getattr(ph, "photo", None) else None
        except Exception:
            logger.warning("ارسال QR بعد از ریچ‌مسیج تحویل ناموفق بود", exc_info=True)
    return sent, fid
