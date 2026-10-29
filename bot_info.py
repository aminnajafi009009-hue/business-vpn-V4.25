"""
bot_info.py
«اطلاعات ربات» — مجموعه‌ی تنظیمات هویتی/کسب‌وکاری (نه تنظیمات فنی حساس) که هم
می‌توانند از .env (config.py) خوانده شوند و هم — بدون نیاز به ری‌دیپلوی —
از پنل ادمین (بخش «ℹ️ اطلاعات ربات») در دیتابیس بازنویسی/ویرایش شوند.

هر کلید ابتدا از جدول settings دیتابیس خوانده می‌شود؛ اگر چیزی برایش ذخیره
نشده باشد (هنوز ادمین ویرایشش نکرده)، مقدار پیش‌فرض از config.py (که خودش از
.env می‌آید) برگردانده می‌شود.

این ماژول مخصوص هویت/برندینگ و اطلاعات تماس کسب‌وکار است (نام ربات، متن
خوش‌آمد، شماره کارت، کانال‌های اجباری، لینک پشتیبانی و ...) — نه اطلاعات
محرمانه‌ی اتصال به سرویس‌های ثالث (مثل رمز پنل مرزبان یا گواهی پاسارگاد) که
همچنان فقط از طریق .env تنظیم می‌شوند.
"""

import json
import logging

import config
import database as db

logger = logging.getLogger(__name__)

_PREFIX = "botinfo_"
_DEFAULT_SUPPORT_URL = "https://t.me/businesss_support"
_CHANNELS_KEY = _PREFIX + "required_channels"

# کلید داخلی -> (مقدار پیش‌فرض ثابت یا None برای گرفتن از config.py، برچسب فارسی برای پنل ادمین)
_FIELDS = {
    "support_url": (_DEFAULT_SUPPORT_URL, "👨‍💻 لینک پشتیبانی (آیدی/کانال تلگرام)"),
    "bot_username": (None, "🤖 یوزرنیم ربات (بدون @)"),
    "connection_guide_url": (None, "📘 لینک آموزش اتصال"),
    "order_log_channel_id": (None, "📋 آیدی عددی کانال یا گروه لاگ سفارشات"),
    "config_name_prefix": ("tg", "🏷 پیشوند نام کانفیگ‌های ساخته‌شده (فقط حروف/عدد انگلیسی و _)"),
}

_DEFAULT_WELCOME_TEXT = (
    "سلام {first_name} 👋 به Business VPN خوش اومدی\n\n"
    "اگر از قطعی، افت سرعت یا پینگ بالا خسته شدی، اینجا می‌تونی سرویس مناسب استفاده روزمره بگیری.\n\n"
    "🎁 اولین باره؟\n"
    "روی «تست رایگان» بزن؛ اول کیفیت را ببین، بعد تصمیم بگیر.\n\n"
    "🛒 آماده خریدی؟\n"
    "«خرید اشتراک» را انتخاب کن.\n\n"
    "⭐ برات سواله چطور میتونی به ما اعتماد کنی؟تجربه مشتریان قبل از خرید:\n"
    "@businesss_etemad\n\n"
    "💬 اگر برای انتخاب سرویس سؤال داری:\n"
    "@businesss_support"
)


def _default_for(key: str):
    if key == "card_number":
        return config.CARD_NUMBER
    if key == "card_holder":
        return config.CARD_HOLDER
    if key == "bot_username":
        return config.BOT_USERNAME
    if key == "connection_guide_url":
        return config.CONNECTION_GUIDE_URL
    if key == "order_log_channel_id":
        return str(config.ORDER_LOG_CHANNEL_ID)
    if key == "welcome_text":
        return _DEFAULT_WELCOME_TEXT
    default, _label = _FIELDS.get(key, (None, None))
    return default


_PM_CARD_KEY = "pm_card"
_CARD_FIELD_MAP = {"card_number": "card_number", "card_holder": "owner_name"}


def _pm_card_value(key: str) -> str:
    """شماره/نام صاحب کارت اکنون فقط از «مدیریت پرداخت ← کارت‌به‌کارت» خوانده می‌شود."""
    try:
        raw = db.get_setting(_PM_CARD_KEY)
        data = json.loads(raw) if raw else {}
        return str(data.get(_CARD_FIELD_MAP[key]) or "").strip()
    except Exception:
        return ""


def migrate_card_to_payment_settings() -> None:
    """مقدار قدیمیِ شماره/نام کارت (اطلاعات ربات یا .env) را یک‌بار به تنظیمات روش‌های پرداخت منتقل می‌کند."""
    try:
        raw = db.get_setting(_PM_CARD_KEY)
        data = json.loads(raw) if raw else {}
        changed = False
        for key, pm_field in _CARD_FIELD_MAP.items():
            if str(data.get(pm_field) or "").strip():
                continue
            legacy = (db.get_setting(_PREFIX + key) or "").strip() or str(
                (config.CARD_NUMBER if key == "card_number" else config.CARD_HOLDER) or "").strip()
            if legacy:
                data[pm_field] = legacy
                changed = True
        if changed:
            data.setdefault("enabled", True)
            db.set_setting(_PM_CARD_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.exception("migrate_card_to_payment_settings failed")


def get(key: str) -> str:
    """مقدار مؤثر فعلی یک فیلد «اطلاعات ربات» را برمی‌گرداند: اول از دیتابیس
    (اگر ادمین قبلاً از پنل ذخیره کرده)، وگرنه پیش‌فرض .env/config.py."""
    if key in _CARD_FIELD_MAP:
        return _pm_card_value(key)
    stored = db.get_setting(_PREFIX + key)
    if stored is not None and stored != "":
        return stored
    default = _default_for(key)
    return default if default is not None else ""


_HIDDEN_KEYS = {"welcome_text"}   # از «اطلاعات ربات» حذف شد؛ متن خوش‌آمد از «ویرایش متن ← خوش‌آمدگویی» تغییر می‌کند


def set(key: str, value: str) -> None:
    if key not in _FIELDS and key not in _HIDDEN_KEYS:
        raise ValueError(f"فیلد نامعتبر برای اطلاعات ربات: {key}")
    db.set_setting(_PREFIX + key, value)


def set_welcome_text_with_entities(value: str, entities=None) -> None:
    """متن خوش‌آمد را ذخیره می‌کند و Premium Emoji / فرمت آن را هم نگه می‌دارد.
    (قبلاً این تابع وجود نداشت و ذخیره‌ی متن خوش‌آمد با AttributeError می‌شکست.)"""
    set("welcome_text", value)
    try:
        import rich_registry
        rich_registry.register(value, entities or [])
    except Exception:
        logger.exception("ثبت entityهای متن خوش‌آمد ناموفق بود")


# ---------------------------------------------------------------------------
# 🎭 ری‌اکشن روی پیام /start مشتری (ایموجی معمولی یا پریمیوم)
# ---------------------------------------------------------------------------
START_REACTION_KEY = _PREFIX + "start_reaction"
# ایموجی‌های مجاز ری‌اکشن تلگرام (انتخاب سریع)
QUICK_REACTIONS = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "🙏", "👌", "💯", "🤣", "⚡", "🏆", "😍", "🫡"]


def get_start_reaction() -> dict | None:
    """{"type": "emoji"|"custom", "value": ...} یا None (بدون ری‌اکشن)."""
    raw = db.get_setting(START_REACTION_KEY)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if isinstance(d, dict) and d.get("type") in ("emoji", "custom") and str(d.get("value") or "").strip():
        return {"type": d["type"], "value": str(d["value"]).strip()}
    return None


def set_start_reaction(kind: str | None, value: str | None = None) -> None:
    if not kind or not value:
        db.set_setting(START_REACTION_KEY, "")
        return
    if kind not in ("emoji", "custom"):
        raise ValueError("نوع ری‌اکشن نامعتبر است")
    db.set_setting(START_REACTION_KEY, json.dumps({"type": kind, "value": str(value).strip()}, ensure_ascii=False))


def start_reaction_label() -> str:
    r = get_start_reaction()
    if not r:
        return "بدون ری‌اکشن"
    return r["value"] if r["type"] == "emoji" else f"ایموجی پریمیوم ({r['value']})"


def labels() -> dict:
    return {k: v[1] for k, v in _FIELDS.items()}


def all_values() -> dict:
    return {k: get(k) for k in _FIELDS}


def get_welcome_text(first_name: str) -> str:
    template = get("welcome_text") or _DEFAULT_WELCOME_TEXT
    try:
        return template.format(first_name=first_name)
    except Exception:
        # اگر ادمین متنی بدون { } جای‌گذاری شده وارد کرده باشد، همان متن خام برگردانده می‌شود.
        return template


def get_support_url() -> str:
    """لینک پشتیبانی را به‌صورت یک URL معتبر برای دکمه‌ی شیشه‌ای تلگرام برمی‌گرداند.
    اگر ادمین فقط یوزرنیم (مثلاً "@mysupport" یا "mysupport") وارد کرده باشد،
    بدون http/https ذخیره نمی‌شود چون تلگرام برای چنین urlهایی خطای
    BUTTON_URL_INVALID برمی‌گرداند."""
    raw = (get("support_url") or "").strip()
    if not raw:
        return _DEFAULT_SUPPORT_URL
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.startswith("t.me/") or raw.startswith("telegram.me/") or raw.startswith("www."):
        return "https://" + raw
    return "https://t.me/" + raw


# ---------------------------------------------------------------------------
# کانال‌های عضویت اجباری — به‌صورت یک آرایه‌ی JSON در همان جدول settings
# ذخیره می‌شود؛ اگر ادمین چیزی تنظیم نکرده باشد، از REQUIRED_CHANNELS در
# config.py (که خودش می‌تواند از .env بیاید) استفاده می‌شود.
# ---------------------------------------------------------------------------
def get_required_channels() -> list:
    stored = db.get_setting(_CHANNELS_KEY)
    if stored:
        try:
            parsed = json.loads(stored)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            logger.exception("خطا در خواندن required_channels ذخیره‌شده در دیتابیس")
    return config.REQUIRED_CHANNELS


def set_required_channels(channels: list) -> None:
    db.set_setting(_CHANNELS_KEY, json.dumps(channels, ensure_ascii=False))


def add_required_channel(channel_id, name: str, url: str) -> None:
    # کانال‌های پیش‌فرض داخل config.py با id عددی (int) ذخیره می‌شوند، در حالی که
    # فرم افزودن از پنل ادمین channel_id را می‌تواند به‌صورت str بفرستد؛ مقایسه‌ی
    # مستقیم بین int و str همیشه False است، پس اینجا همیشه با str() مقایسه می‌کنیم.
    target = str(channel_id)
    channels = get_required_channels()
    channels = [c for c in channels if str(c.get("id")) != target]
    channels.append({"id": channel_id, "name": name, "url": url})
    set_required_channels(channels)


def remove_required_channel(channel_id) -> None:
    # دکمه‌ی حذف توی پنل ادمین، callback_data را به‌صورت رشته‌متن (str) می‌فرستد، درحالی
    # که کانال‌های پیش‌فرض داخل config.py با id عددی (int) ذخیره شده‌اند؛ مقایسه‌ی int != str
    # همیشه True است، پس اینجا با str() مقایسه می‌کنیم تا فارغ از نوع درست حذف شود.
    target = str(channel_id)
    channels = [c for c in get_required_channels() if str(c.get("id")) != target]
    set_required_channels(channels)


# ---------------------------------------------------------------------------
# 🔁 تنظیمات تمدید سرویس — هم‌ساختار ربات
# ---------------------------------------------------------------------------
def _renewal_category_key(category_id: int, field: str) -> str:
    return f"renewal_category_{int(category_id)}_{field}"

CUSTOM_RENEWAL_ID = -1  # «دسته»ی مجازی: پیش‌فرض تمدید همه‌ی گزینه‌های سفارشی (بساز سرویس خودت)
CUSTOM_OPTION_BASE = -1000  # «دسته»ی مجازی هر گزینه‌ی سفارشی: id = -(1000 + شماره‌ی گزینه)

def custom_option_id(num) -> int:
    return CUSTOM_OPTION_BASE - int(num or 0)

def custom_option_from_id(cid) -> int:
    return CUSTOM_OPTION_BASE - int(cid)

def get_renewal_settings(category_id: int | None) -> dict:
    try:
        cid = int(category_id or 0)
    except Exception:
        cid = 0
    if cid == CUSTOM_RENEWAL_ID:
        return get_renewal_custom_settings(0, _only_global=True)
    if cid <= CUSTOM_OPTION_BASE:
        return get_renewal_custom_settings(custom_option_from_id(cid))
    defaults = {"mode":"day","price_day":0,"price_gb":5500,"min_day":1,"max_day":0,"min_gb":1,"max_gb":0,"day_options":"30,60,90","gb_options":"10,20,50"}
    # 🆕 «پیش‌فرض همه‌ی سرویس‌ها» (category 0): برای سرویس‌های بدون دسته/پلن (سفارشی، تست، ساخت دستی، قدیمی)
    # و برای فیلدهایی که یک دسته خودش ست نکرده. قبلاً این سرویس‌ها همیشه «فقط زمان» با مقدارهای ثابت می‌گرفتند.
    if cid > 0:
        defaults = get_renewal_settings(0)
    out = dict(defaults)
    for field in out:
        raw = db.get_setting(_renewal_category_key(cid, field))
        if raw in (None, ""):
            continue
        if field == "mode":
            out[field] = raw if raw in ("day","gb","both") else defaults[field]
        elif field in ("day_options","gb_options"):
            out[field] = str(raw)
        else:
            try:
                value = max(0, int(float(raw)))
                if field.startswith("min_"):
                    value = max(1, value)
                out[field] = value
            except Exception:
                pass
    if out["max_day"] and out["max_day"] < out["min_day"]:
        out["max_day"] = out["min_day"]
    if out["max_gb"] and out["max_gb"] < out["min_gb"]:
        out["max_gb"] = out["min_gb"]
    return out

def set_renewal_setting(category_id: int, field: str, value) -> None:
    allowed={"mode","price_day","price_gb","min_day","max_day","min_gb","max_gb","day_options","gb_options"}
    if field not in allowed:
        raise ValueError("فیلد نامعتبر تنظیمات تمدید")
    if field == "mode":
        if value not in ("day","gb","both"):
            raise ValueError("حالت تمدید نامعتبر است")
        raw=value
    elif field in ("day_options","gb_options"):
        raw=str(value)
    else:
        raw=str(max(0,int(float(value))))
    db.set_setting(_renewal_category_key(int(category_id),field),raw)

def get_renewal_custom_settings(option_num, _only_global: bool = False) -> dict:
    """تنظیمات تمدید سرویس سفارشی. پایه: قیمت/محدوده‌ی «گزینه»ی ساخت سرویس (price_per_gb / price_per_30_days /
    min,max gb/days) و حالت «حجم و زمان»؛ هر فیلدی که ادمین در «تنظیمات تمدید ← سرویس سفارشی» صریحاً تعیین کرده
    باشد روی آن اولویت دارد."""
    out = dict(get_renewal_settings(0))
    out["mode"] = "both"
    try:
        cb = db.get_effective_custom_build_settings(int(option_num or 0))
        out.update({
            "price_gb": int(cb.get("price_per_gb") or 0),
            "price_day": float(cb.get("price_per_30_days") or 0) / 30.0,
            "min_gb": max(1, int(cb.get("min_gb") or 1)), "max_gb": int(cb.get("max_gb") or 0),
            "min_day": max(1, int(cb.get("min_days") or 1)), "max_day": int(cb.get("max_days") or 0),
        })
    except Exception:
        pass
    _scopes = [CUSTOM_RENEWAL_ID] + ([] if _only_global else [custom_option_id(option_num)])
    for _sc in _scopes:   # اول پیش‌فرض همه‌ی سفارشی‌ها، بعد تنظیمات اختصاصی همین گزینه
      for field in ("mode", "price_day", "price_gb", "min_day", "max_day", "min_gb", "max_gb", "day_options", "gb_options"):
        raw = db.get_setting(_renewal_category_key(_sc, field))
        if raw in (None, ""):
            continue
        if field == "mode":
            if raw in ("day", "gb", "both"): out[field] = raw
        elif field in ("day_options", "gb_options"):
            out[field] = str(raw)
        else:
            try:
                v = max(0, int(float(raw)))
                out[field] = max(1, v) if field.startswith("min_") else v
            except Exception:
                pass
    if out["max_day"] and out["max_day"] < out["min_day"]: out["max_day"] = out["min_day"]
    if out["max_gb"] and out["max_gb"] < out["min_gb"]: out["max_gb"] = out["min_gb"]
    return out


def get_renewal_plan_settings(plan_key: str | None, category_id: int | None = None) -> dict:
    base=get_renewal_settings(category_id)
    key=str(plan_key or "").strip()
    if not key:
        return base
    out=dict(base); prefix=f"renewal_plan_{key}_"
    for field in ("mode","price_day","price_gb","min_day","max_day","min_gb","max_gb","day_options","gb_options"):
        raw=db.get_setting(prefix+field)
        if raw in (None,""):
            continue
        if field=="mode":
            if raw in ("day","gb","both"): out[field]=raw
        elif field in ("day_options","gb_options"):
            out[field]=str(raw)
        else:
            try:
                value=max(0,int(float(raw)))
                if field.startswith("min_"): value=max(1,value)
                out[field]=value
            except Exception: pass
    if out["max_day"] and out["max_day"] < out["min_day"]: out["max_day"]=out["min_day"]
    if out["max_gb"] and out["max_gb"] < out["min_gb"]: out["max_gb"]=out["min_gb"]
    return out

def set_renewal_plan_setting(plan_key: str, field: str, value) -> None:
    allowed={"mode","price_day","price_gb","min_day","max_day","min_gb","max_gb","day_options","gb_options"}
    if not plan_key or field not in allowed: raise ValueError("تنظیم تمدید پلن نامعتبر است")
    if field=="mode":
        if value not in ("day","gb","both"): raise ValueError("حالت تمدید نامعتبر است")
        raw=value
    elif field in ("day_options","gb_options"): raw=str(value)
    else: raw=str(max(0,int(float(value))))
    db.set_setting(f"renewal_plan_{str(plan_key).strip()}_{field}",raw)

def clear_renewal_plan_overrides_for_category(category_id:int, field:str)->None:
    for plan in db.get_vip_plans(int(category_id)):
        db.set_setting(f"renewal_plan_{plan['plan_key']}_{field}","")


# ---------------------------------------------------------------------------
# 🏷 حالت نام‌گذاری سرویس‌ها
#   default → یوزرنیم ساخته‌شده روی پنل = «پیشوند ربات + _ + ۸ رقم» (معماری پیش‌فرض این ربات)
#   user    → کاربر بعد از انتخاب پلن یک نام انتخاب می‌کند (نام + _ + ۵ رقم) و دقیقاً همان روی پنل ساخته می‌شود
# نماینده‌ها هرگز وارد حالت user نمی‌شوند: فقط پیشوند پیش‌فرض یا پیشوند اختصاصی خودشان.
# ---------------------------------------------------------------------------
_CONFIG_NAME_MODE_KEY = _PREFIX + "config_name_mode"
CONFIG_NAME_MODES = ("default", "user")


def get_config_name_mode() -> str:
    try:
        value = (db.get_setting(_CONFIG_NAME_MODE_KEY) or "").strip().lower()
    except Exception:
        value = ""
    return value if value in CONFIG_NAME_MODES else "default"


def set_config_name_mode(mode: str) -> None:
    if mode not in CONFIG_NAME_MODES:
        raise ValueError("حالت نام‌گذاری نامعتبر است.")
    db.set_setting(_CONFIG_NAME_MODE_KEY, mode)


# حالت «انتخاب خودکار نام» (وقتی نامگذاری توسط کاربر فعال است و کاربر دکمه‌ی «انتخاب خودکار» را می‌زند):
#   username = از یوزرنیم/نام تلگرام کاربر (رفتار قبلی)   |   prefix = از پیشوند پیش‌فرضی که ادمین تعیین کرده
_AUTO_NAME_MODE_KEY = _PREFIX + "config_name_auto_mode"
AUTO_NAME_MODES = ("username", "prefix")


def get_auto_name_mode() -> str:
    try:
        v = (db.get_setting(_AUTO_NAME_MODE_KEY) or "").strip().lower()
    except Exception:
        v = ""
    return v if v in AUTO_NAME_MODES else "username"


def set_auto_name_mode(mode: str) -> None:
    if mode not in AUTO_NAME_MODES:
        raise ValueError("حالت انتخاب خودکار نامعتبر است.")
    db.set_setting(_AUTO_NAME_MODE_KEY, mode)


# ---------------------------------------------------------------------------
# 🔢 پسوند تصادفی نام سرویس (همیشه روشن؛ پیش‌فرض ۸ رقم عدد)
# ---------------------------------------------------------------------------
_SUFFIX_LEN_KEY = _PREFIX + "cfg_suffix_len"
_SUFFIX_KIND_KEY = _PREFIX + "cfg_suffix_kind"
SUFFIX_KINDS = ("digits", "letters", "mixed")
SUFFIX_KIND_LABELS = {"digits": "فقط عدد", "letters": "فقط حروف", "mixed": "ترکیب"}
SUFFIX_MIN, SUFFIX_MAX = 3, 12


def get_suffix_len() -> int:
    try:
        n = int((db.get_setting(_SUFFIX_LEN_KEY) or "").strip())
    except (TypeError, ValueError, AttributeError):
        return 8
    return n if SUFFIX_MIN <= n <= SUFFIX_MAX else 8


def set_suffix_len(n: int) -> None:
    n = int(n)
    if not SUFFIX_MIN <= n <= SUFFIX_MAX:
        raise ValueError(f"تعداد باید بین {SUFFIX_MIN} تا {SUFFIX_MAX} باشد.")
    db.set_setting(_SUFFIX_LEN_KEY, str(n))


def get_suffix_kind() -> str:
    try:
        v = (db.get_setting(_SUFFIX_KIND_KEY) or "").strip().lower()
    except Exception:
        v = ""
    return v if v in SUFFIX_KINDS else "digits"


def set_suffix_kind(kind: str) -> None:
    if kind not in SUFFIX_KINDS:
        raise ValueError("نوع پسوند نامعتبر است.")
    db.set_setting(_SUFFIX_KIND_KEY, kind)


def random_suffix(length: int | None = None, kind: str | None = None) -> str:
    """پسوند تصادفی امن طبق تنظیمات ادمین (حروف فقط کوچک انگلیسی؛ سازگار با مرزبان/پاسارگارد)."""
    import secrets as _s
    import string as _st
    n = int(length) if length else get_suffix_len()
    k = kind or get_suffix_kind()
    alphabet = {"digits": _st.digits, "letters": _st.ascii_lowercase}.get(k, _st.ascii_lowercase + _st.digits)
    if k == "mixed" and n >= 2:
        # ترکیب واقعی: حداقل یک حرف و یک عدد
        chars = [_s.choice(_st.ascii_lowercase), _s.choice(_st.digits)] + [_s.choice(alphabet) for _ in range(n - 2)]
        _s.SystemRandom().shuffle(chars)
        return "".join(chars)
    return "".join(_s.choice(alphabet) for _ in range(n))
