"""
sentry_setup.py
راه‌اندازی اختیاری Sentry برای لاگ متمرکز خطاهای production (هم برای ربات،
هم برای webapp_api). اگر SENTRY_DSN در .env / Environment تنظیم نشده باشد،
init() هیچ کاری نمی‌کند و بقیه‌ی کد (capture_exception در جاهای دیگر) هم
بی‌خطر و بی‌اثر باقی می‌ماند — یعنی نبودِ Sentry هیچ‌وقت باعث کرش نمی‌شود.

چرا لازم است: بدون این، خطاهای پیش‌بینی‌نشده فقط در لاگ Render ثبت می‌شوند
که با هر ری‌استارت/دیپلوی پاک می‌شود. با Sentry (پلن رایگانش برای این حجم
ترافیک کافی است)، خطاها نگه داشته می‌شوند و حتی می‌شود روی آن‌ها ایمیل/آلارم
تنظیم کرد.
"""
import logging

from config import SENTRY_DSN, SENTRY_ENVIRONMENT

logger = logging.getLogger(__name__)


def init_sentry(extra_integrations=None):
    if not SENTRY_DSN:
        logger.info("SENTRY_DSN تنظیم نشده؛ لاگ متمرکز Sentry غیرفعال است.")
        return

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("پکیج sentry-sdk نصب نیست؛ SENTRY_DSN نادیده گرفته شد.")
        return

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        integrations=extra_integrations or [],
        traces_sample_rate=0.05,  # ردیابی پرفورمنس رو کم نگه می‌داریم؛ فقط خطاها مهم‌اند
        send_default_pii=False,   # اطلاعات شخصی کاربر (مثل آیدی تلگرام) به‌طور خودکار فرستاده نشود
    )
    logger.info("Sentry با موفقیت راه‌اندازی شد.")
