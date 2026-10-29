"""
alerts.py
بررسی دوره‌ای مصرف و تاریخ انقضای سرویس‌های VIP و اطلاع‌رسانی خودکار به کاربر
وقتی ۸۰٪/۹۰٪ حجم مصرف شده یا ۲ روز به پایان سرویس مانده است.
این هشدارها فقط مخصوص سرویس‌های VIP هستند (طبق درخواست کاربر).
"""
import logging
from datetime import datetime

import crypto
import database as db
import bot_info
import notif_settings
import ui_editor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
from subscription import fetch_subscription_info, usage_bar, days_remaining
from keyboards import back_button
from utils import send_notification_sticker

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800  # هر ۳۰ دقیقه یک بار


async def check_usage_alerts(bot):
    """روی همه‌ی سرویس‌های VIP فعال حلقه می‌زند و در صورت لزوم هشدار می‌فرستد."""
    configs = db.get_active_vip_configs()
    for cfg in configs:
        try:
            await _check_single_config(bot, cfg)
        except Exception:
            logger.exception("خطا در بررسی هشدار مصرف برای سرویس %s", cfg.get("id"))


async def _check_single_config(bot, cfg):
    try:
        sub_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        return
    if not sub_link.lower().startswith(("http://", "https://")):
        return

    usage = await fetch_subscription_info(sub_link)
    if not usage:
        return

    user = db.get_user_by_id(cfg["user_id"])
    if not user:
        return

    total = usage.get("total")
    used = (usage.get("upload") or 0) + (usage.get("download") or 0)

    # daily snapshot for the mini app usage charts (fills even if the app is never opened)
    db.record_usage_snapshot(cfg["id"], used)

    usage_ok = db.user_pref_enabled(user_id=user["id"], key="usage_alerts")
    expiry_ok = db.user_pref_enabled(user_id=user["id"], key="expiry_alerts")

    if total and usage_ok:
        percent = min(100, round(used / total * 100))
        if percent >= 90 and not cfg.get("alert_90_sent"):
            await _send_usage_alert(bot, user, cfg, percent)
            db.set_config_alert_sent(cfg["id"], "alert_90_sent")
            db.set_config_alert_sent(cfg["id"], "alert_80_sent")
        elif percent >= 80 and not cfg.get("alert_80_sent"):
            await _send_usage_alert(bot, user, cfg, percent)
            db.set_config_alert_sent(cfg["id"], "alert_80_sent")

    expire_ts = usage.get("expire")
    remaining = days_remaining(expire_ts) if expire_ts else None
    if remaining is not None:
        if expiry_ok and 0 <= remaining <= 2 and not cfg.get("alert_expiry_sent"):
            await _send_expiry_alert(bot, user, cfg, remaining)
            db.set_config_alert_sent(cfg["id"], "alert_expiry_sent")

    ended_by_time = remaining is not None and remaining < 0
    ended_by_volume = bool(total) and used >= total
    if expiry_ok and (ended_by_time or ended_by_volume) and not cfg.get("alert_ended_sent"):
        await _send_ended_alert(bot, user, cfg, ended_by_volume)
        db.set_config_alert_sent(cfg["id"], "alert_ended_sent")


async def _send_usage_alert(bot, user, cfg, percent):
    if not notif_settings.enabled("usage_volume"):
        return
    bar = usage_bar(percent)
    text = (
        "🔔 هشدار حجم مصرفی سرویس\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"{bar}\n"
        f"✅ شما تا الان {percent}٪ از حجم سرویستون رو مصرف کردید.\n\n"
        "برای جلوگیری از قطعی سرویس، پیشنهاد می‌کنیم همین الان تمدید کنید 🔁"
    )
    sticker_key = "notif_usage_90" if percent >= 90 else "notif_usage_80"
    await _safe_send(bot, user, cfg, text, sticker_key=sticker_key)


async def _send_expiry_alert(bot, user, cfg, remaining):
    if not notif_settings.enabled("usage_time"):
        return
    days_text = "امروز به پایان می‌رسه" if remaining == 0 else f"فقط {remaining} روز دیگه مونده"
    text = (
        "⏰ هشدار پایان سرویس\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"🟨 سرویس شما {days_text}!\n\n"
        "برای جلوگیری از قطعی، همین الان تمدید کنید 🔁"
    )
    await _safe_send(bot, user, cfg, text, sticker_key="notif_expiry")


async def _send_ended_alert(bot, user, cfg, ended_by_volume: bool):
    if not notif_settings.enabled("service_ended"):
        return
    reason = "حجم سرویستون" if ended_by_volume else "زمان سرویستون"
    text = (
        "⛔️ سرویس شما به پایان رسید\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"🔴 {reason} شما به پایان رسیده و سرویس منقضی شده است.\n\n"
        "برای ادامه‌ی استفاده، همین الان از بخش خرید اشتراک، سرویس خودتون رو تمدید کن 🔁"
    )
    try:
        await send_notification_sticker(bot, int(user["telegram_id"]), "notif_ended")
    except Exception:
        pass
    try:
        await bot.send_message(
            int(user["telegram_id"]), text,
            reply_markup=back_button("plans", "🛍 خرید اشتراک", screen="notif_service_ended"),
        )
    except Exception:
        logger.exception("ارسال هشدار پایان سرویس ناموفق بود")


async def _safe_send(bot, user, cfg, text, sticker_key: str | None = None):
    try:
        if sticker_key:
            await send_notification_sticker(bot, int(user["telegram_id"]), sticker_key)
        await bot.send_message(
            int(user["telegram_id"]), text,
            reply_markup=back_button(f"viewconfig_{cfg['id']}", "📦 مشاهده سرویس", screen="notif_usage_alert"),
        )
    except Exception:
        logger.exception("ارسال هشدار مصرف به کاربر %s ناموفق بود", user.get("telegram_id"))


# ---------------------------------------------------------------------------
# 🛎 لاگ همه‌ی سفارش‌های نهایی‌شده (خرید/تمدید/تست رایگان/سرویس سفارشی) در
# کانال «اعتماد»، با قالب ثابت.
# ---------------------------------------------------------------------------
def _mask_telegram_id(telegram_id) -> str:
    """آیدی عددی را برای حفظ حریم خصوصی، در پیام کانال اعتماد به‌شکل ماسک‌شده
    نمایش می‌دهد؛ مثلاً 6512345515 → 65*****515 (۲ رقم اول + ۳ رقم آخر باقی می‌مانند)."""
    s = str(telegram_id or "-")
    if len(s) <= 5:
        return s
    return s[:2] + "*" * (len(s) - 5) + s[-3:]


def _fix_unlimited_typo(value) -> str:
    """رفع تایپوی «نامدود» (بجای «نامحدود») در متن‌های لاگ سفارش."""
    s = str(value or "")
    return s.replace("نامدود", "نامحدود") if "نامدود" in s else s


def _renewal_log_details(added_volume: float = 0, added_days: int = 0) -> str:
    parts = []
    if added_volume:
        parts.append(f"+{float(added_volume):g} گیگ")
    if added_days:
        parts.append(f"+{int(added_days)} روز")
    return (" | ".join(parts) + "\n") if parts else ""


def _entities_to_types(raw_entities):
    out = []
    for e in raw_entities or []:
        try:
            out.append(MessageEntity(
                type=e["type"], offset=e["offset"], length=e["length"],
                custom_emoji_id=e.get("custom_emoji_id"), url=e.get("url"),
                language=e.get("language"),
            ))
        except Exception:
            continue
    return out


def _gregorian_to_jalali(gy: int, gm: int, gd: int):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_now_text() -> str:
    """زمان فعلی تهران به‌صورت شمسی، مثل 1405-07-07 03:36"""
    from utils import now_tehran
    now = now_tehran()
    jy, jm, jd = _gregorian_to_jalali(now.year, now.month, now.day)
    return f"{jy:04d}-{jm:02d}-{jd:02d} {now.strftime('%H:%M:%S')}"


def _order_log_template_key(order_label: str) -> str:
    label = str(order_label or "")
    if "تست" in label:
        return "order_log_test"
    if "تمدید" in label:
        return "order_log_renewal"
    if "payg" in label.lower():
        return "order_log_payg"
    if "سفارشی" in label:
        return "order_log_custom"
    return "order_log_purchase"


async def log_order_to_channel(
    bot,
    *,
    order_label: str,
    user: dict,
    username: str | None,
    service_id: str | None,
    service_name: str | None,
    package_text: str,
    amount_text: str,
    expiry_text: str,
    renewal_details: str | None = None,
    payment_method: str | None = None,
    panel_name: str | None = None,
):
    """لاگ سفارش در کانال؛ متن از «ویرایش متن‌ها» (order_log_purchase / _renewal / _test / _custom / _payg)
    خوانده می‌شود و Premium Emoji و متغیرها پشتیبانی می‌شوند."""
    from utils import now_tehran, to_jalali_str
    import text_catalog

    if not notif_settings.enabled("admin_order_log"):
        return
    package_text = _fix_unlimited_typo(package_text)
    display_expiry = _fix_unlimited_typo(expiry_text)
    # تاریخ میلادی → شمسی (مقادیر غیرتاریخی مثل «نامحدود» دست‌نخورده می‌مانند)
    if display_expiry and display_expiry not in ("-", "نامحدود"):
        try:
            display_expiry = to_jalali_str(datetime.strptime(display_expiry[:10], "%Y-%m-%d"), with_time=False)
        except Exception:
            pass

    template_key = _order_log_template_key(order_label)
    real_service_name = service_id or service_name or "-"
    values = {
        "order_label": order_label,
        "customer_name": user.get("name", "-"),
        "customer": user.get("name", "-"),
        "telegram_id": _mask_telegram_id(user.get("telegram_id")),
        "service_id": service_id or "-",
        "service_name": real_service_name,
        "package_name": package_text or "-",
        "package": package_text or "-",
        "amount": amount_text or "-",
        "expiry": display_expiry or "-",
        "time": f"{jalali_now_text()} (به وقت تهران)",
        "logged_at": f"{jalali_now_text()} (به وقت تهران)",
        "payment_method": payment_method or "-",
        "renewal_details": renewal_details or "",
        "username": username or "-",
        "panel_name": panel_name or "-",
        "panel": panel_name or "-",
    }
    # تایپوی «نامدود» روی «مقدارها» اصلاح می‌شود (نه روی متن رندرشده)، تا offset ایموجی‌های پریمیوم
    # و فرمت (ریچ‌تکست) به‌هم نخورد و entityها دور ریخته نشوند.
    values = {k: (_fix_unlimited_typo(v) if isinstance(v, str) else v) for k, v in values.items()}
    rendered = text_catalog.text(template_key, **values)
    text = str(rendered)
    entities = _entities_to_types(getattr(rendered, "entities", None))
    # ریچ‌مسیج ذخیره‌شده برای این قالب (جدول/تیتر/…)؛ اگر نبود، متن + entity معمولی
    rich_payload = None
    try:
        rich_payload = ui_editor.rich_payload_for(template_key, {k: v for k, v in values.items()})
    except Exception:
        logger.exception("rich payload lookup failed for %s", template_key)

    reply_markup = None
    try:
        # از سیستم «ویرایش دکمه‌ها» عبور می‌کند: متن/ایموجی/رنگ/مخفی بودن/ترتیب و دکمه‌های دلخواه صفحه‌ی «order_log».
        import keyboards as _kbm
        bot_username = (bot_info.get("bot_username") or "").strip().lstrip("@")
        _rows = []
        if bot_username:
            _rows.append([_kbm.InlineKeyboardButton(
                text=ui_editor.get_button("order_log", "order_log_buy", "🛒 خرید اشتراک"),
                url="https://t.me/" + bot_username,
                style="success",
                ui_screen="order_log",
                ui_button_key="order_log_buy",
            )])
        _mk = _kbm.InlineKeyboardMarkup(ui_screen="order_log", inline_keyboard=_rows)
        # داخل کانال/گروه فقط دکمه‌های لینکی کار می‌کنند (callback و Web App در کانال معنا ندارند و پیام را رد می‌کنند).
        # دکمه‌ی دلخواهِ نوع «callback» در کانال کار نمی‌کند؛ به لینک عمیق ربات (?start=cb_<id>) تبدیل می‌شود
        # تا با کلیک، کاربر به ربات برود و همان دکمه/عملکرد برایش باز شود.
        _cb_ids = {}
        if bot_username:
            try:
                for _cb in ui_editor.get_custom_buttons("order_log"):
                    if _cb.get("action_type") == "callback":
                        _cb_ids.setdefault(str(_cb.get("action_value") or ""), int(_cb["id"]))
            except Exception:
                logger.exception("خواندن دکمه‌های دلخواه لاگ سفارش ناموفق بود")
        _clean = []
        for _row in (_mk.inline_keyboard or []):
            _r = []
            for b in _row:
                if getattr(b, "url", None):
                    _r.append(b)
                    continue
                _cd = getattr(b, "callback_data", None)
                if _cd and str(_cd) in _cb_ids:
                    _r.append(_kbm.InlineKeyboardButton(
                        text=b.text, url=f"https://t.me/{bot_username}?start=cb_{_cb_ids[str(_cd)]}",
                        style=getattr(b, "style", None) or "primary",
                    ))
            if _r:
                _clean.append(_r)
        if _clean:
            _mk.inline_keyboard = _clean
            reply_markup = _mk
    except Exception:
        logger.exception("ساخت کیبورد لاگ سفارش ناموفق بود")
        reply_markup = None

    channel = bot_info.get("order_log_channel_id")
    if not channel or str(channel) == "0":
        logger.warning("کانال لاگ سفارش تنظیم نشده است")
        return
    if rich_payload:
        try:
            import rich_message
            await rich_message.send(bot, int(channel), rich_payload, reply_markup=reply_markup)
            return
        except Exception:
            logger.exception("ارسال ریچ‌مسیج لاگ سفارش ناموفق بود؛ تلاش با متن + entity")
    try:
        await bot.send_message(
            int(channel), text, reply_markup=reply_markup,
            entities=entities or None, parse_mode=None, _skip_auto_text=True,
        )
    except Exception:
        logger.exception("ارسال لاگ سفارش به کانال ناموفق بود؛ تلاش با متن ساده")
        try:
            await bot.send_message(int(channel), text, reply_markup=reply_markup,
                                   parse_mode=None, _skip_auto_text=True)
        except Exception:
            logger.exception("ارسال لاگ سفارش (متن ساده) هم ناموفق بود")


async def send_admin_order_report(
    bot,
    *,
    order_label: str,
    payment_method: str | None,
    user: dict,
    service_name: str | None,
    package_text: str | None,
    amount_text: str | None,
    panel_name: str | None = None,
    expiry_text: str | None = None,
):
    """گزارش نهایی خرید/تست/تمدید برای ادمین اصلی (با دکمه‌ی مدیریت کاربر)."""
    from config import ADMIN_ID
    try:
        import adv_reports
        adv_reports.delivery_report(order_label=order_label, payment_method=payment_method, user=user,
                                    service_name=service_name, package_text=_fix_unlimited_typo(package_text or "-"),
                                    amount_text=amount_text, panel_name=panel_name, expiry_text=expiry_text)
    except Exception:
        logger.exception("adv delivery report failed")
    if not notif_settings.enabled("admin_order_report"):
        return

    label = str(order_label or "")
    if "تست" in label:
        kind = "تست"
    elif "تمدید" in label:
        kind = "تمدید"
    elif "سفارشی" in label:
        kind = "سفارشی"
    else:
        kind = "خرید"

    if panel_name:
        if kind == "تمدید":
            action_line = f"به صورت خودکار از پنل {panel_name} تمدید شد ✅"
        else:
            action_line = f"به صورت خودکار از پنل {panel_name} ساخته و ارسال شد ✅"
    else:
        action_line = "تمدید شد ✅" if kind == "تمدید" else "ارسال شد ✅"

    telegram_id = str(user.get("telegram_id") or "-")
    username = await fetch_username(bot, telegram_id) if telegram_id != "-" else None
    name = str(user.get("name") or "-")
    who = f"{name}-@{username}" if username else name

    text = (
        f"🛒 {kind}-{payment_method or '-'}\n"
        f"{action_line}\n\n"
        f"👤 {who}\n"
        f"🆔ایدی عددی:{telegram_id}\n"
        f"📌 {service_name or '-'}\n\n"
        f"💰 {amount_text or '-'}\n"
        f"📦 {_fix_unlimited_typo(package_text or '-')}\n"
        f"⏰ {jalali_now_text()} (به وقت تهران)"
    )
    keyboard = None
    if telegram_id != "-":
        from keyboards import InlineKeyboardButton as UIInlineKeyboardButton
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            UIInlineKeyboardButton(text="👤 مدیریت کاربر", callback_data=f"useropenmsg_{telegram_id}", style="primary")
        ]])
    try:
        await bot.send_message(int(ADMIN_ID), text, reply_markup=keyboard, parse_mode=None, _skip_auto_text=True)
    except Exception:
        logger.exception("ارسال گزارش سفارش برای ادمین ناموفق بود")


async def fetch_username(bot, telegram_id) -> str | None:
    try:
        chat = await bot.get_chat(int(telegram_id))
        return chat.username
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 💳 مانیتورینگ سلامت درگاه پرداخت آنلاین (یونیک‌پی)
# پولر هر ۲۰ ثانیه وضعیت اینوویس‌های در انتظار را چک می‌کند؛ اگر خودِ درگاه
# قطعی/کند باشد، این چک‌ها پشت‌سرهم fail می‌شوند ولی قبلاً هیچ‌جا به ادمین
# اطلاع داده نمی‌شد (فقط لاگ Render که با هر ری‌استارت پاک می‌شود). این بخش
# نرخ fail را در یک بازه‌ی زمانی می‌سنجد و در صورت عبور از آستانه، یک‌بار به
# ادمین پیام می‌دهد (با cooldown تا اسپم نشود).
# ---------------------------------------------------------------------------
import time as _time

UNIQUEPAY_ALERT_COOLDOWN_SECONDS = 30 * 60  # حداقل ۳۰ دقیقه بین دو هشدار مشابه
UNIQUEPAY_FAILURE_RATE_THRESHOLD = 0.5      # اگر بیش از ۵۰٪ چک‌های یک چرخه fail شوند
UNIQUEPAY_MIN_SAMPLE = 3                    # حداقل تعداد نمونه برای معنادار بودن نرخ

_uniquepay_state = {
    "last_check_alert_at": 0.0,
    "last_create_alert_at": 0.0,
    "create_fail_streak": 0,
}


async def report_uniquepay_check_cycle(bot, admin_id, checked: int, failed: int):
    """بعد از هر چرخه‌ی کامل پولر (بررسی همه‌ی اینوویس‌های در انتظار) صدا زده
    می‌شود. اگر نرخ خطا از آستانه بیشتر باشد، یک هشدار (با cooldown) می‌فرستد."""
    if not notif_settings.enabled("admin_gateway_report"):
        return
    if checked < UNIQUEPAY_MIN_SAMPLE or failed == 0:
        return
    rate = failed / checked
    if rate < UNIQUEPAY_FAILURE_RATE_THRESHOLD:
        return

    now = _time.time()
    if now - _uniquepay_state["last_check_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_check_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"در آخرین چرخه‌ی بررسی، {failed} از {checked} چک وضعیت اینوویس ({round(rate * 100)}٪) "
        "با خطا مواجه شد.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. تا رفع مشکل، بهتره کاربرها رو به پرداخت "
        "کارت‌به‌کارت یا کیف پول راهنمایی کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


async def report_uniquepay_create_failure(bot, admin_id):
    """هر بار که ساخت اینوویس (create_invoice) برای یک کاربر شکست بخورد صدا
    زده می‌شود. بعد از ۳ شکست پشت‌سرهم (بدون هیچ موفقیت میانی)، یک هشدار
    می‌فرستد؛ با موفقیت بعدی، شمارنده صفر می‌شود."""
    if not notif_settings.enabled("admin_gateway_report"):
        return
    _uniquepay_state["create_fail_streak"] += 1
    if _uniquepay_state["create_fail_streak"] < 3:
        return

    now = _time.time()
    if now - _uniquepay_state["last_create_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_create_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"{_uniquepay_state['create_fail_streak']} کاربر پشت‌سرهم موفق به ساخت لینک پرداخت آنلاین نشدند "
        "و به کارت‌به‌کارت هدایت شدند.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. بد نیست پنل یونیک‌پی رو چک کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


def report_uniquepay_create_success():
    _uniquepay_state["create_fail_streak"] = 0



# ---------------------------------------------------------------------------
# 🌐 هشدار درگاه‌های جدید (FrenzyEx / Tronado)
# ---------------------------------------------------------------------------
_gateway_alert_state: dict = {}


async def report_gateway_create_failure(bot, admin_id, gateway_title: str, reason: str):
    """هر شکست ساخت فاکتور (با دلیل دقیق برای ادمین، مثلاً API Key نامعتبر یا Code -31 ترونادو) هشدار می‌دهد؛
    حداکثر هر ۳۰ دقیقه یک‌بار برای هر درگاه."""
    if not notif_settings.enabled("admin_gateway_report"):
        return
    st = _gateway_alert_state.setdefault(gateway_title, {"last": 0.0, "streak": 0})
    st["streak"] += 1
    now = _time.time()
    if now - st["last"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    st["last"] = now
    text = (
        f"⚠️ هشدار درگاه {gateway_title}\n\n"
        f"ساخت لینک پرداخت برای یک کاربر ناموفق بود ({st['streak']} شکست پشت‌سرهم).\n"
        f"📝 دلیل: {str(reason)[:300]}\n\n"
        "تنظیمات درگاه را در «روش‌های پرداخت» بررسی کنید.\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار درگاه %s به ادمین ناموفق بود", gateway_title)


def report_gateway_create_success(gateway_title: str):
    st = _gateway_alert_state.setdefault(gateway_title, {"last": 0.0, "streak": 0})
    st["streak"] = 0
