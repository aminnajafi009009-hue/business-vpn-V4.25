"""
config.py
تمام تنظیمات ربات از اینجا خوانده می‌شود.
هیچ مقدار حساس (توکن، آیدی ادمین، شماره کارت) نباید مستقیم داخل کد نوشته شود؛
همه از فایل .env خوانده می‌شوند.
"""

import os
from dotenv import load_dotenv

load_dotenv()

def _get_env(key: str, required: bool = True, default=None):
    value = os.environ.get(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"متغیر محیطی الزامی '{key}' در فایل .env تنظیم نشده است.")
    return value

TOKEN = _get_env("TOKEN").strip().strip("\"").strip("\'")
ADMIN_ID = int(_get_env("ADMIN_ID"))

CARD_NUMBER = _get_env("CARD_NUMBER")
CARD_HOLDER = _get_env("CARD_HOLDER")

# ---------------------------------------------------------------------------
# Sentry — لاگ متمرکز خطاهای production. اگر SENTRY_DSN خالی/تنظیم‌نشده
# باشد، sentry_sdk.init اصلاً صدا زده نمی‌شود و تمام capture_exception ها
# بی‌اثر (no-op) می‌مانند؛ یعنی این قابلیت کاملاً اختیاری است و نبودنش هیچ
# خطایی ایجاد نمی‌کند. برای فعال‌سازی: یک پروژه‌ی رایگان روی sentry.io بساز
# و مقدار DSN اش را در Render، در بخش Environment، به همین اسم اضافه کن.
SENTRY_DSN = _get_env("SENTRY_DSN", required=False, default="")
SENTRY_ENVIRONMENT = _get_env("SENTRY_ENVIRONMENT", required=False, default="production")

# ---------------------------------------------------------------------------
# درگاه پرداخت آنلاین یونیک‌پی (UniquePay) — کارت‌به‌کارت با تایید خودکار.
# اگر UNIQUEPAY_BUSINESS_TOKEN خالی باشد، این روش پرداخت به‌طور خودکار از
# لیست روش‌های پرداخت (ربات و مینی‌اپ) مخفی می‌شود.
# ---------------------------------------------------------------------------
UNIQUEPAY_BASE_URL = _get_env("UNIQUEPAY_BASE_URL", required=False, default="https://uniquepay.top")
UNIQUEPAY_BUSINESS_TOKEN = _get_env("UNIQUEPAY_BUSINESS_TOKEN", required=False, default="")
# آدرسی که کاربر پس از پرداخت موفق به آن هدایت می‌شود (باید با دامنه‌ی
# ثبت‌شده برای بیزینس در پنل uniquepay.top مطابقت داشته باشد؛ برای ربات
# می‌توان لینک خود ربات را ثبت کرد).
UNIQUEPAY_REDIRECT_URL = _get_env("UNIQUEPAY_REDIRECT_URL", required=False, default="")
UNIQUEPAY_ENABLED = bool(UNIQUEPAY_BUSINESS_TOKEN)
# حداقل مبلغی که درگاه پرداخت آنلاین (یونیک‌پی) قبول می‌کند. برای مبالغ
# مساوی یا کمتر از این عدد، درگاه آنلاین (نه در خرید پلن، نه در شارژ کیف
# پول) نمایش داده نمی‌شود و کاربر باید از کارت‌به‌کارت یا کیف پول استفاده کند.
ONLINE_PAYMENT_MIN_AMOUNT = 50_000

# محدودیت شارژ کیف پول (تومان)
# حداقل و حداکثر مبلغی که کاربر می‌تواند برای شارژ کیف پول ثبت کند.
MIN_WALLET_TOPUP = int(_get_env("MIN_WALLET_TOPUP", required=False, default="50000"))
MAX_WALLET_TOPUP = int(_get_env("MAX_WALLET_TOPUP", required=False, default="3000000"))

if MIN_WALLET_TOPUP < 0 or MAX_WALLET_TOPUP < MIN_WALLET_TOPUP:
    raise RuntimeError("محدوده شارژ کیف پول نامعتبر است: MIN_WALLET_TOPUP باید کمتر یا مساوی MAX_WALLET_TOPUP باشد.")

# پرداخت رمزارزی — کیف پول‌ها از .env خوانده می‌شوند و از پنل «اطلاعات ربات» نیز
# قابل Override هستند. اگر هیچ‌کدام تنظیم نباشند، گزینه Crypto در پرداخت اصلاً نمایش داده نمی‌شود.
CRYPTO_USDT_TRON_WALLET = _get_env("CRYPTO_USDT_TRON_WALLET", required=False, default="")
CRYPTO_TON_WALLET = _get_env("CRYPTO_TON_WALLET", required=False, default="")
CRYPTO_TRX_WALLET = _get_env("CRYPTO_TRX_WALLET", required=False, default="")
CRYPTO_PAYMENT_ENABLED = bool(CRYPTO_TON_WALLET or CRYPTO_TRX_WALLET or CRYPTO_USDT_TRON_WALLET)
CRYPTO_USDT_TOMAN_RATE = 0
CRYPTO_USDT_ENABLED = bool(CRYPTO_USDT_TRON_WALLET)

DATABASE_PATH = _get_env("DATABASE_PATH", required=False, default="database.db")

# اتصال به Turso (دیتابیس ابری رایگان و دائمی، سازگار با SQLite).
# اگر این دو مقدار در .env تنظیم شده باشند، ربات به‌جای فایل محلی SQLite
# (که با هر دیپلوی روی Render پاک می‌شود) از Turso استفاده می‌کند و اطلاعات
# کاربران برای همیشه حفظ می‌شود. اگر خالی بمانند، ربات مثل قبل از فایل محلی
# استفاده می‌کند (مناسب اجرای تستی روی سیستم شخصی).
TURSO_DATABASE_URL = _get_env("TURSO_DATABASE_URL", required=False, default="")
TURSO_AUTH_TOKEN = _get_env("TURSO_AUTH_TOKEN", required=False, default="")

REQUIRED_CHANNELS = [
    {"id": -1003957260685, "name": "کانال اصلی", "url": "https://t.me/businesss_vpn"},
    {"id": -1003904350377, "name": "کانال اعتماد", "url": "https://t.me/businesss_etemad"},
]

VIP_PLANS = {
    # 🐛 فیکس: قبلاً "volume_gb" اینجا ست نشده بود، در نتیجه هنگام seed اولیه‌ی
    # دیتابیس مقدار volume_gb این پلن‌ها صفر ذخیره می‌شد و چون آزادسازی پاداش
    # معرف با شرط volume_gb >= REFERRAL_MIN_VOLUME_GB انجام می‌شود، خرید همین
    # پلن‌های پیش‌فرض (که محصول اصلی سایت‌اند) هرگز پاداش دعوت را آزاد نمی‌کرد.
    "plan_1": {"name": "10 گیگ | کاربر و زمان ∞", "price": 75000, "days": 0, "volume_gb": 10},
    "plan_3": {"name": "20 گیگ | کاربر و زمان ∞", "price": 150000, "days": 0, "volume_gb": 20},
    "plan_6": {"name": "30 گیگ | کاربر و زمان ∞", "price": 225000, "days": 0, "volume_gb": 30},
    "plan_7": {"name": "50 گیگ | کاربر و زمان ∞", "price": 300000, "days": 0, "volume_gb": 50},
    "plan_8": {"name": "100 گیگ | کاربر و زمان ∞", "price": 500000, "days": 0, "volume_gb": 100},
}
# ⚠️ توجه: از این به بعد VIP_PLANS فقط برای «بار اول» (seed) دیتابیس استفاده می‌شود.
# منبع اصلی و همیشه‌به‌روز پلن‌های VIP، جدول‌های vip_categories/vip_plans در
# database.py است (چون از پنل ادمین قابل افزودن/ویرایش/حذف است — بخش «دسته‌بندی VIP»).
# برای گرفتن لیست واقعی/فعلی پلن‌های VIP همیشه از db.get_all_vip_plans_flat() یا
# db.get_vip_plans(category_id) استفاده کنید، نه از این دیکشنری.
# 🐛 اگر این پروژه از قبل یک‌بار روی یک دیتابیس اجرا شده و پلن‌های پیش‌فرض با
# volume_gb=0 ذخیره شده‌اند، اجرای مجدد init_db() این seed قدیمی را عوض
# نمی‌کند (فقط «بار اول که دیتابیس کاملاً خالی است» seed می‌شود). برای دیتابیس‌های
# قدیمی، مقدار حجم هر پلن را یک‌بار از پنل ادمین → «دسته‌بندی VIP» ویرایش/ذخیره
# کنید تا مقدار صحیح در دیتابیس هم ثبت شود.


# پلن «تست» — با زدن دکمه‌ی «🎁 تست رایگان» دقیقاً مثل بقیه‌ی پلن‌ها
# (با همان مراحل کیف‌پول/کارت‌به‌کارت) به کاربر نمایش داده می‌شود.
FREE_TEST_PLAN_KEY = "plan_test"
FREE_TEST_PLAN = {"name": "1 گیگ 7 روزه", "price": 2000, "days": 7, "volume_gb": 1}

# ⚠️ PLANS/plan_type اینجا فقط «snapshot اولیه» هستند (برای seed دیتابیس و fallback).
# چون از این پس دسته‌بندی‌ها و پلن‌های VIP از پنل ادمین اضافه/ویرایش می‌شوند، این دو
# دیگر منبع درستی نیستند و در همه‌جای ربات به‌جایشان از db.get_all_plans() و
# db.plan_type(plan_key) استفاده می‌کنیم (که همیشه لیست واقعی/به‌روز را از دیتابیس می‌خوانند).
PLANS = {**VIP_PLANS, FREE_TEST_PLAN_KEY: FREE_TEST_PLAN}


def plan_type(plan_key: str) -> str:
    """نگه‌داشته‌شده فقط برای سازگاری با کد قدیمی؛ در کد جدید از db.plan_type استفاده کنید."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return "test"
    return "vip"


# ---------------------------------------------------------------------------
# کانال «اعتماد» — همه‌ی سفارش‌های نهایی‌شده (شامل تست رایگان) با یک قالب
# ثابت در این کانال لاگ می‌شوند. پیش‌فرض همان کانال اعتمادی است که در بالا
# در REQUIRED_CHANNELS تعریف شده؛ اگر بخواهید کانال دیگری باشد، در .env
# مقدار ORDER_LOG_CHANNEL_ID را ست کنید.
ORDER_LOG_CHANNEL_ID = int(_get_env("ORDER_LOG_CHANNEL_ID", required=False, default="-1003904350377"))

# ---------------------------------------------------------------------------
# نمایندگی — تخفیف ۵۰٪ روی محصولات VIP برای آیدی‌های عددی‌ای که ادمین به‌عنوان
# «نماینده» ثبت می‌کند (از طریق دیتابیس؛ جدول agents در database.py).
AGENCY_VIP_DISCOUNT_PERCENT = int(_get_env("AGENCY_VIP_DISCOUNT_PERCENT", required=False, default="50"))

# متن معرفی سرویس‌ها که هنگام ورود به «🎁 خرید اشتراک» نمایش داده می‌شود.
# ⚠️ عمداً متن ساده (بدون * و بدون parse_mode=Markdown): با parse_mode صریح،
# ایموجی پریمیوم (rich_registry) و entityهای ویرایشگر روی این پیام اعمال نمی‌شد.
# برای تغییر متن و پریمیوم‌کردن ایموجی‌ها از ربات: ویرایشگر ← متن‌ها ← «🛒 خرید اشتراک».
PLANS_INTRO_TEXT = (
    "🛒 انتخاب سرویس مناسب\n\n"
    "برای اینکه هزینه اضافه ندهی، براساس استفاده‌ات انتخاب کن:\n\n"
    "🔵 VIP (V2Ray)\n"
    "مناسب اینستاگرام، تلگرام، یوتیوب، دانلود و استفاده روزمره و استفاده عمومی."
)

# مبلغی که با ثبت‌نام هر فرد دعوت‌شده، به‌صورت "قفل" به معرف تعلق می‌گیرد
# و فقط بعد از اینکه فرد دعوت‌شده یک خرید واجد شرط (حجم >= REFERRAL_MIN_VOLUME_GB)
# داشته باشد، آزاد می‌شود.
REFERRAL_LOCK_AMOUNT = int(_get_env("REFERRAL_LOCK_AMOUNT", required=False, default="20000"))

# حداقل حجم (به گیگابایت) خریدی که فرد دعوت‌شده باید انجام دهد تا پاداش دعوت
# معرفش آزاد شود. خریدهای زیر این حجم (مثل تست رایگان یا پلن‌های ۵ گیگ) پاداش
# را آزاد نمی‌کنند.
REFERRAL_MIN_VOLUME_GB = int(_get_env("REFERRAL_MIN_VOLUME_GB", required=False, default="10"))
BOT_USERNAME = _get_env("BOT_USERNAME", required=False, default="BusinessSVPNBot")

# اگر UNIQUEPAY_REDIRECT_URL در .env تنظیم نشده باشد، پیش‌فرض لینک خود ربات
# است (باید این آدرس هم در پنل uniquepay.top به‌عنوان دامنه‌ی مجاز ثبت شود).
UNIQUEPAY_REDIRECT_URL = UNIQUEPAY_REDIRECT_URL or ("https://t.me/" + BOT_USERNAME)

# ---------------------------------------------------------------------------
# «بساز سرویس خودت» — محاسبه‌ی قیمت سرویس سفارشی
# ---------------------------------------------------------------------------
CUSTOM_BUILD_PRICE_PER_GB = int(_get_env("CUSTOM_BUILD_PRICE_PER_GB", required=False, default="5000"))
CUSTOM_BUILD_PRICE_PER_30_DAYS = int(_get_env("CUSTOM_BUILD_PRICE_PER_30_DAYS", required=False, default="5000"))
CUSTOM_BUILD_MIN_GB, CUSTOM_BUILD_MAX_GB = 5, 1000
CUSTOM_BUILD_MIN_DAYS, CUSTOM_BUILD_MAX_DAYS = 30, 1000

# لینک آموزش اتصال که زیر پیام ارسال کانفیگ به خریدار نمایش داده می‌شود.
CONNECTION_GUIDE_URL = _get_env(
    "CONNECTION_GUIDE_URL", required=False, default="https://t.me/businesss_vpn/16?single"
)

# ---------------------------------------------------------------------------
# 🔗 پنل شاهراه (Shahrah Reseller VaaS) — ساخت/تمدید/فعال‌سازی خودکار سرویس
# از طریق وب‌سرویس reseller شاهراه.
# ⚠️ SHAHRAH_API_KEY یک مقدار کاملاً محرمانه است: هرگز داخل کد commit نشود،
# فقط در .env (سمت سرور) قرار بگیرد و هیچ‌وقت به مینی‌اپ/کلاینت فرستاده نشود.
# اگر خالی باشد، کل بخش «اتصال پنل شاهراه» در ربات به‌طور خودکار غیرفعال/مخفی
# می‌شود و همه‌چیز دقیقاً مثل قبل (ارسال کاملاً دستی) کار می‌کند.
# ---------------------------------------------------------------------------
SHAHRAH_BASE_URL = _get_env(
    "SHAHRAH_BASE_URL", required=False, default="https://shahrah.top/api/vaas/reseller"
).rstrip("/")
SHAHRAH_API_KEY = _get_env("SHAHRAH_API_KEY", required=False, default="")
SHAHRAH_ENABLED = bool(SHAHRAH_API_KEY)
