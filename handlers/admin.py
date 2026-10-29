"""
handlers/admin.py
پنل کامل ادمین: آمار، لیست کاربران، جستجوی حرفه‌ای، شارژ کیف پول، ارسال
کانفیگ، تأیید/رد پرداخت کارت‌به‌کارت خرید سرویس، مدیریت تخفیف (ساخت گام‌به‌گام)،
مدیریت دعوت‌ها، پیام همگانی، بکاپ.

تمام handlerهای این فایل فقط برای ADMIN_ID فعال هستند.
"""

from datetime import datetime, timedelta
import asyncio
import html
import json
import logging
import os
import re

from aiogram import Router, F, types, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile

import database as db
import cache
import crypto
from payment_manager_v3 import PaymentManager as _PM
import alerts
from subscription import extract_meta, days_remaining, format_bytes, usage_bar, fetch_subscription_info, format_expire, extract_configs
from utils import parse_int_in_range, is_duplicate_action, now_tehran_naive, STICKER_SECTION_LABELS, STICKER_FILES, STICKERS_DIR, invalidate_section_sticker_cache, send_notification_sticker, clean_numeric_id, telegram_utf16_length, to_jalali_str, now_tehran, stat_period_bounds_utc, parse_jalali_date
from states import AdminStates, UserStates
from permissions import permission_for_callback as _registry_permission_for_callback, permission_for_message as _registry_permission_for_message
import bot_info
import panels as panel_api
_pm_instance: "_PM | None" = None
import threading as _threading
_pm_lock = _threading.Lock()

def _pm() -> "_PM":
    """Lazy singleton برای PaymentManager — thread-safe."""
    global _pm_instance
    if _pm_instance is not None:
        return _pm_instance
    with _pm_lock:
        if _pm_instance is None:
            _pm_instance = _PM(db.get_setting, db.set_setting)
            _pm_instance.load()
    return _pm_instance

from config import (
    ADMIN_ID,
    DATABASE_PATH,
    AGENCY_VIP_DISCOUNT_PERCENT,
    FREE_TEST_PLAN_KEY,
)
from keyboards import (
    admin_custom_build_keyboard,
    InlineKeyboardButton,
    admin_panel_menu,
    admin_reply_keyboard,
    admin_back_button,
    admin_discount_menu,
    admin_user_actions_keyboard,
    admin_purchase_notify_keyboard,
    admin_custom_order_notify_keyboard,
    admin_broadcast_audience_keyboard,
    admin_gift_all_confirm_keyboard,
    admin_health_keyboard,
    admin_cache_keyboard,
    admin_custom_build_keyboard,
    admin_audit_logs_keyboard,
    admin_stats_keyboard,
    admin_userlist_menu,
    config_delivery_keyboard,
    main_reply_keyboard,
    admin_services_list_keyboard,
    admin_service_detail_keyboard,
    admin_order_queue_keyboard,
    admin_clear_orders_confirm_keyboard,
    admin_request_queue_menu,
    admin_pending_receipts_keyboard,
    admin_clear_receipts_confirm_keyboard,
    admin_purge_confirm_keyboard,
    admin_userlist_page_keyboard,
    admin_accounting_keyboard,
    discount_type_keyboard,
    discount_plans_select_keyboard,
    discount_scope_keyboard,
    admin_renewal_custom_options_menu,
    discount_scope_summary,
    discount_new_audience_keyboard,
    discount_plans_edit_keyboard,
    discount_detail_keyboard,
    discount_audience_keyboard,
    discount_delete_confirm_keyboard,
    admin_agency_menu,
    admin_agent_row_keyboard,
    admin_agent_actions_keyboard,
    admin_vip_categories_keyboard,
    vip_categories_keyboard,
    admin_vip_category_detail_keyboard,
    admin_vip_plan_detail_keyboard,
    admin_pm_cancel_keyboard,
    admin_referrers_page_keyboard,
    admin_referred_detail_keyboard,
    admin_guides_menu,
    admin_guide_detail_keyboard,
    admin_guide_delete_confirm_keyboard,
    admin_guide_cancel_keyboard,
    admin_error_logs_keyboard,
    admin_error_log_detail_keyboard,
    admin_error_logs_clear_confirm_keyboard,
    admin_stickers_menu,
    admin_sticker_detail_keyboard,
    admin_sticker_cancel_keyboard,
    admin_botinfo_menu,
    admin_cfgname_menu,
    admin_cfgname_root_menu,
    admin_order_log_channel_keyboard,
    admin_cfgsuffix_menu,
    admin_botinfo_field_keyboard,
    admin_botinfo_channels_menu,
    admin_botinfo_wallet_menu,
    admin_botinfo_referral_menu,
    admin_campaigns_menu,
    campaign_detail_keyboard,
    campaign_delete_confirm_keyboard,
    admin_manage_admins_keyboard,
    admin_permissions_keyboard,
    admin_renewal_categories_menu, admin_renewal_category_menu, admin_renewal_plan_menu, admin_renewal_mode_menu, admin_renewal_scope_menu,
    admin_config_search_keyboard, admin_unified_search_results_keyboard, admin_agent_plan_select_keyboard,
    admin_discount_audience_keyboard, admin_payg_menu, admin_panel_folders_keyboard,
    # FIX-19/7/4/3/16 – new keyboards
    admin_config_search_keyboard,
    admin_agent_plan_select_keyboard,
    admin_discount_audience_keyboard,
    admin_payg_menu,
    admin_panel_folders_keyboard,
    admin_vip_move_select_keyboard, admin_vip_move_dest_keyboard,
    admin_referral_settings_menu, admin_wallet_settings_menu, admin_payment_methods_menu, admin_log_settings_menu,
    admin_crypto_list_menu, admin_card_info_menu, admin_wallet_info_menu,
)

from keyboards import (
    admin_gift_audience_keyboard,
    admin_search_mode_keyboard,
    admin_search_results_keyboard,
    admin_gift_all_confirm_keyboard,
    admin_payg_category_pricing_keyboard,
    admin_payg_plan_pricing_keyboard,
    admin_payg_pricing_mode_keyboard,
    admin_free_test_panels_keyboard,
    admin_free_test_panel_detail_keyboard,
    admin_crypto_backup_keyboard,
)

plan_type = db.plan_type  # نسخه‌ی DB-aware (دسته‌بندی‌های VIP را هم می‌شناسد)

import ui_editor as _ui_editor
import text_catalog as _text_catalog

# Pre-warm ui_editor schema cache at import time (avoids first-call freeze)
try:
    import ui_editor as _ui_editor_warmup
    _ui_editor_warmup._ensure()
except Exception:
    pass

TEXTS = {k: v.get("default", "") for k, v in _ui_editor.SCREENS.items()}
TEXT_CATEGORIES = {}
for _key, _screen in _ui_editor.SCREENS.items():
    TEXT_CATEGORIES.setdefault(_screen.get("category", "other"), []).append(_key)
CATEGORY_BY_KEY = {k: _ui_editor.SCREENS[k].get("category", "other") for k in _ui_editor.SCREENS}

def user_text(key: str, default: str = "", **values):
    value = _ui_editor.get_text(key, default)
    if not value:
        # کلیدهای اعلان (notif_*) در متن‌های پیش‌فرض text_catalog هستند، نه SCREENS؛
        # قبلاً مقدار خالی برمی‌گشت و ارسال پیام بی‌صدا شکست می‌خورد.
        try:
            import text_catalog as _tc
            value = _tc.TEXTS.get(key, "") or ""
        except Exception:
            value = ""
    try:
        return value.format(**values)
    except (KeyError, ValueError, IndexError):
        return value

def refresh_user_text(key: str | None = None):
    return None

TELEGRAM_TEXT_LIMIT = 4096

def truncate_for_telegram(value: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    value = str(value or "")
    if telegram_utf16_length(value) <= limit:
        return value
    out = []
    total = 0
    for ch in value:
        n = len(ch.encode("utf-16-le")) // 2
        if total + n > limit:
            break
        out.append(ch); total += n
    return "".join(out)

def is_message_too_long_error(exc: Exception) -> bool:
    return "message is too long" in str(exc).lower() or "MESSAGE_TOO_LONG" in str(exc).upper()

def serialize_message_entities(entities):
    result = []
    for entity in entities or []:
        try:
            result.append(entity.model_dump(exclude_none=True))
        except Exception:
            try:
                result.append(dict(entity))
            except Exception:
                pass
    return result

def message_entities_from_dicts(items):
    result = []
    for item in items or []:
        try:
            result.append(types.MessageEntity(**item) if isinstance(item, dict) else item)
        except Exception:
            pass
    return result

async def enrich_configs_with_subscription_names(configs):
    for cfg in configs or []:
        cfg["_display_name"] = cfg.get("_display_name") or cfg.get("plan") or ""
        try:
            raw = crypto.decrypt_config(cfg.get("config", ""))
            if raw and raw.lower().startswith(("http://", "https://")):
                meta = await extract_meta(raw)
                name = (meta or {}).get("name")
                if name:
                    cfg["_display_name"] = name
        except Exception:
            pass
    return configs

def _admin_user_delete_keyboard(uid):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"deleteuserconfirm_{uid}", style="danger")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data=f"useropen_{uid}", style="primary")],
    ])

def _available_panels():
    rows = db.list_vpn_panels(enabled_only=True)
    return {str(p["id"]): panel_api.panel_label(p) for p in rows}

def _active_panel_id():
    value = db.get_setting("active_vpn_panel", "")
    return str(value) if value else None

def _set_active_panel(panel_id):
    try:
        pid = int(panel_id)
    except Exception:
        return False
    if db.get_vpn_panel(pid) is None:
        return False
    db.set_setting("active_vpn_panel", str(pid))
    return True

def _panel_choose_keyboard(available, active):
    buttons = []
    for pid, label in available.items():
        prefix = "✅ " if str(pid) == str(active) else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"panelchoose_{pid}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def _pasargad_menu(_text=""):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 تست اتصال", callback_data="pasargadtest", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])

router = Router(name="admin")
logger = logging.getLogger(__name__)


def _permission_for_message_text(text: str | None) -> str | None:
    registered = _registry_permission_for_message(text)
    if registered:
        return registered
    mapping = {
        "📊 آمار": "stats",
        "📥 صف درخواست‌ها": "requests",
        "👥 لیست کاربران": "users",
        "🔍 جستجوی کاربر": "users",
        "📢 پیام همگانی": "broadcast",
        "🎟 مدیریت تخفیف": "discounts",
        "🤝 نمایندگی (تخفیف VIP)": "agency",
        "🗂 دسته‌بندی‌های VIP": "plans",
        "🛒 خرید اشتراک برای خودم": "plans",
        "📦 نگاشت پلن‌ها به پنل فعال": "vpn_panel",
        "🛡️ اتصال پنل پاسارگارد": "vpn_panel",
        "🔀 انتخاب پنل VPN فعال": "vpn_panel",
        "🤝 مدیریت دعوت‌ها": "referrals",
        "📚 مدیریت راهنما": "guides",
        "🦖 لاگ خطاها": "logs",
        "ℹ️ اطلاعات ربات": "botinfo",
        "🎬 استیکرهای منو": "stickers",
        "💾 بکاپ": "backup",
        "🎁 تنظیم تست رایگان": "settings",
        "🔁 تنظیمات ��مدی��": "settings",
        "📝 مدیریت متن‌های کاربر": "texts",
        "✏️ ویرایش متن و دکمه‌ها": "texts",
        "🔴 خاموش کردن سفارشات": "orders_toggle",
        "🟢 روشن کردن سفارشات": "orders_toggle",
        "🎁 تنظیمات رفرال": "referral_settings",
        "💳 تنظیمات کیف‌پول": "wallet_settings",
        "💳 روش‌های پرداخت": "payment_methods",
        "📜 مدیریت ��اگ‌ها": "log_settings",
    }
    return mapping.get(text or "")


def _permission_for_callback(data: str | None) -> str | None:
    d = data or ""
    registered = _registry_permission_for_callback(d)
    if registered:
        return registered
    if d in {"admin_back"}:
        return None
    if d.startswith(("approve_", "reject_", "approvepay|", "rejectpay|", "approvecustom_", "rejectcustom_", "clearreceipts", "rcptview_", "rcptviewc_")) or d == "admin_pending_receipts":
        return "receipts"
    groups = [
        (("admin_stats", "stat_"), "stats"),
        (("admin_request_queue", "admin_order_queue", "dismissorder_", "dismisscustomorder_", "qordview_", "qcustview_", "clearorders", "marzbansend|"), "requests"),
        (("admin_userlist", "userpage_", "useropen_", "useropenmsg_", "accounting_", "admin_search", "useractions_", "pm_", "toggleblock_", "deleteuser_", "deleteuserconfirm_", "svcs_", "svcdetail_", "svcdelete_", "svcrestore_", "svcpurge", "svcedit_"), "users"),
        (("admin_broadcast",), "broadcast"),
        (("admin_discount", "discdetail_", "discdelete", "discedit_", "discaud_", "discplan", "dsc|", "ndaud|", "new_discount", "disctype_"), "discounts"),
        (("admin_agency", "new_agent", "deleteagent_", "agentopen_", "agentppct_", "agent_plans_", "agentplan", "autosend_", "editagentpercent_", "editagentcats_", "agentcat_", "agentcatall_", "editagentprefix_", "agopt", "agtq_", "agdm"), "agency"),
        (("admin_vip_categories", "admin_buy_subscription", "newvip", "vip"), "plans"),
        (("admin_marzban", "admin_pasargad", "admin_panel_choose", "panelchoose", "marz", "pasargad", "svcrevokesub_", "fup_"), "vpn_panel"),
        (("admin_botinfo", "botinfo", "channel", "ntf_", "adv_"), "botinfo"),
        (("admin_stickers", "sticker"), "stickers"),
        (("admin_referrals", "refpage_", "refdetail_"), "referrals"),
        (("admin_guides", "guide"), "guides"),
        (("admin_texts", "admin_text_editor", "textedit_", "textdelete_", "ui_"), "texts"),
        (("errlog",), "logs"),
        (("admin_backup",), "backup"),
        (("admin_orders_off", "admin_orders_on"), "orders_toggle"),
        (("admin_referral_settings", "refset_"), "referral_settings"),
        (("admin_wallet_settings", "wset_"), "wallet_settings"),
        (("admin_payment_methods", "paymt_crypto_master", "paymt_card", "paymt_wallet", "paymt_uniquepay", "paymt_frenzyex", "paymt_tronado", "paymt_tonpays", "paymt_crypto", "paymt_add_coin", "paymt_del_coin", "paymt_toggle", "paymt_topup"), "payment_methods"),
        (("admin_log_settings", "admin_order_log_channel"), "log_settings"),
        (("free_test", "admin_free_test_settings", "admin_renewal_settings", "renewset", "renewsetscope", "renewsetcat", "renewsw"), "settings"),
    ]
    for prefixes, perm in groups:
        if any(d == x or d.startswith(x) for x in prefixes):
            return perm
    return None


class AdminPermissionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or user.id == ADMIN_ID:
            return await handler(event, data)
        if not db.is_sub_admin(str(user.id)):
            return await handler(event, data)
        # مدیریت خود ادمین‌های فرعی فقط برای ادمین اصلی است.
        cb_data = getattr(event, "data", None)
        if cb_data and (cb_data == "admin_manage_admins" or cb_data.startswith("subadm")):
            await event.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
            return
        perm = _permission_for_callback(cb_data) if cb_data is not None else _permission_for_message_text(getattr(event, "text", None))
        # اگر دکمه‌ای داخل یک بخش مجاز ساخته شده باشد، همان مجوز بخش مادر کافی است.
        if user.id != ADMIN_ID and perm == "receipts" and db.sub_admin_has_permission(str(user.id), "requests"):
            return await handler(event, data)
        if perm and not db.sub_admin_has_permission(str(user.id), perm):
            if cb_data is not None:
                await event.answer("⛔ شما به این قابلیت دسترسی ندارید.", show_alert=True)
            else:
                await event.answer("⛔ شما به این قابلیت دسترسی ندارید.")
            return
        return await handler(event, data)


router.message.middleware(AdminPermissionMiddleware())
router.callback_query.middleware(AdminPermissionMiddleware())


def _is_main_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_sub_admin(str(user_id))

def _admin_perm(user_id: int, permission: str) -> bool:
    return user_id == ADMIN_ID or db.sub_admin_has_permission(str(user_id), permission)

def _current_admin_permissions(user_id: int) -> set[str] | None:
    if user_id == ADMIN_ID:
        return None
    adm = db.get_sub_admin(str(user_id)) or {}
    return set(adm.get("permissions") or [])

def _admin_panel_kb_for(user_id: int):
    return admin_panel_menu(db.is_orders_enabled(), permissions=_current_admin_permissions(user_id), is_main_admin=(user_id == ADMIN_ID))

def _admin_reply_kb_for(user_id: int, orders_enabled: bool | None = None):
    return admin_reply_keyboard(orders_enabled, permissions=_current_admin_permissions(user_id), is_main_admin=(user_id == ADMIN_ID))


async def _notify_main_admin_action(bot, actor, action: str, target: str = "", details: str = ""):
    name = getattr(actor, "full_name", None) or getattr(actor, "first_name", None) or str(getattr(actor, "id", ""))
    actor_id = str(getattr(actor, "id", ""))
    try:
        db.log_admin_action(actor_id, name, action, target, details)
    except Exception:
        logger.exception("admin activity log failed")
    if actor_id != str(ADMIN_ID) and notif_settings.enabled("admin_subadmin_report"):
        try:
            await bot.send_message(ADMIN_ID, f"📝 گزارش عملکرد ادمین فرعی\n\n👤 {name}\n🆔 {actor_id}\n✅ عملیات: {action}\n🎯 مورد: {target or '-'}\nℹ️ جزئیات: {details or '-'}")
        except Exception:
            logger.exception("notify main admin failed")

async def _deny_no_perm(obj, text="⛔ شما به این بخش دسترسی ندارید."):
    if hasattr(obj, "answer"):
        try:
            await obj.answer(text, show_alert=True)
        except TypeError:
            await obj.answer(text)


async def _reply_with_user_actions(target, text: str, uid, is_blocked: bool, *, edit: bool = False):
    """پیام همراه با کیبورد اقدامات کاربر (admin_user_actions_keyboard) را می‌فرستد یا ویرایش می‌کند.
    اگر تلگرام به‌خاطر تنظیمات حریم‌خصوصی محدودتر همان کاربر خاص، دکمه‌ی «رفتن به پیوی کاربر»
    (لینک tg://user) را رد کند (خطای BUTTON_USER_PRIVACY_RESTRICTED)، به‌جای کرش کردن کل پیام،
    همان پیام را بدون این دکمه‌ی خاص دوباره می‌فرستد؛ برای بقیه‌ی کاربران دکمه همچنان نمایش داده می‌شود."""
    try:
        kb = admin_user_actions_keyboard(uid, is_blocked)
        if edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "BUTTON_USER_PRIVACY_RESTRICTED" in str(e) or "BUTTON_USER_INVALID" in str(e):
            kb = admin_user_actions_keyboard(uid, is_blocked, show_pm_link=False)
            if edit:
                await target.edit_text(text, reply_markup=kb)
            else:
                await target.answer(text, reply_markup=kb)
        else:
            raise


_RECEIPTS_QUEUE_MARKER = "🧾 رسیدهای در انتظار تایید"


async def _finish_receipt_message(message: types.Message, note: str, queue_refresh=None):
    """پیام رسید (چه پیام متنی معمولی از ربات کلاسیک، چه پیام عکس+کپشن از
    Mini App که دکمه‌ها مستقیم روی خودِ عکس هستند) را با یک خط نتیجه
    (تأیید/رد) نهایی می‌کند و دکمه‌ها را حذف می‌کند.
    توجه: روی پیام‌های عکس‌دار، edit_text خطا می‌دهد (تلگرام برای عکس‌ها
    caption دارد نه text)؛ برای همین باید edit_caption صدا زده شود.

    queue_refresh: یک تابع async بدون آرگومان. اگر همین دکمه‌ی تأیید/رد از
    داخل پیام «صف درخواست‌ها → رسیدهای در انتظار تایید» زده شده باشد (نه از
    پیام تک‌رسیدیِ اصلی)، به‌جای خالی‌کردن دکمه‌های کل لیست، همان لیست
    رفرش می‌شود تا آیتم‌های دیگرِ هنوز-در-انتظار از بین نروند."""
    has_media = bool(message.photo or getattr(message, "document", None))
    text_or_caption = message.caption if has_media else message.text
    if queue_refresh is not None and text_or_caption and text_or_caption.startswith(_RECEIPTS_QUEUE_MARKER):
        await queue_refresh()
        return
    empty_kb = types.InlineKeyboardMarkup(inline_keyboard=[])
    if has_media:
        await message.edit_caption(caption=(message.caption or "") + note, reply_markup=empty_kb)
    else:
        await message.edit_text((message.text or "") + note, reply_markup=empty_kb)




def _format_volume_gb_label(volume_gb) -> str:
    if volume_gb is None:
        return "نامشخص"
    try:
        v = float(volume_gb)
    except (TypeError, ValueError):
        return str(volume_gb)
    if v <= 0:
        return "نامحدود"
    if v < 1:
        mb = round(v * 1024)
        return f"{mb} مگابایت"
    return f"{int(v) if v.is_integer() else v:g} گیگ"



def _english_digits(value) -> str:
    text = str(value if value is not None else "")
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def _delivery_service_label(plan_name, volume_gb, days, user_limit, plan_key=None) -> str:
    """نام نمایشی تحویل سرویس بر اساس اطلاعات واقعی پلن خریداری‌شده."""
    if db.is_free_test_key(plan_key):
        return "تست رایگان"

    volume_text = _format_volume_gb_label(volume_gb) if volume_gb is not None else "نامشخص"
    days_text = f"زمان {days} روزه" if days else "زمان نامحدود"
    user_limit_text = "نامحدود کاربر" if not user_limit else f"{user_limit} کاربر"

    return _english_digits(f"{volume_text} | {days_text} | {user_limit_text}")

def _gb_from_bytes(num_bytes) -> float | None:
    if not num_bytes:
        return None
    try:
        return round(int(num_bytes) / (1024 ** 3), 1)
    except (TypeError, ValueError):
        return None


@router.message(Command("admin"))
async def admin_entry(message: types.Message):
    if not _is_admin(message.from_user.id):
        return  # کاربر عادی هیچ پاسخی نمی‌گیرد (نه حتی پیام خطا) - امنیتی
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(message.from_user.id))


_ADMIN_LAST_CAT: dict = {}


@router.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    """بازگشت ادمین: دیگر پیام را پاک نمی‌کند؛ منوی بخشی که ادمین از آن آمده را در «پیام جدید» زیر پیام فعلی می‌فرستد.
    (وقتی تاریخچه‌ی همان پیام موجود باشد، nav_history پیش از رسیدن به اینجا یک پله‌ی دقیق عقب می‌رود.)"""
    await callback.answer()
    await state.clear()
    uid = callback.from_user.id
    if not _is_admin(uid):
        return
    cat = _ADMIN_LAST_CAT.get(uid)
    if cat in _CAT_MAP:
        kb_fn, label = _CAT_MAP[cat]
        try:
            kb = kb_fn()
            if uid != ADMIN_ID:
                kb = _filter_kb_for_subadmin(kb, uid)
            await callback.message.answer(f"📂 {label}\n\nیکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=kb)
            return
        except Exception:
            logger.exception("admin_back: ساخت منوی دسته ناموفق بود")
    await callback.message.answer("🛠 پنل مدیریت\n\nیکی از بخش‌ها را انتخاب کنید:", reply_markup=_admin_panel_kb_for(uid))


@router.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    # رفع باگ: اگر متن/دکمه دقیقاً همان چیزی باشد که الان روی پیام هست (مثلاً دوبار زدن روی دکمه)، تلگرام خطای "message is not modified"
    # می‌دهد که قبلاً گرفته نمی‌شد و به هندلر سراسری خطا می‌رفت و به کاربر "خطایی پیش آمد" نشان داده می‌شد.
    try:
        await callback.message.edit_text("👮 مدیریت ادمین‌های فرعی\n\nادمین را انتخاب کنید یا ادمین جدید اضافه کنید:", reply_markup=admin_manage_admins_keyboard(db.get_all_sub_admins()))
    except TelegramBadRequest:
        pass
    await callback.answer()

# 🐛 فیکس: دکمه‌ی «مدیریت ادمین‌ها» قبلاً فقط داخل پنل اینلاین بود، طبق درخواست کاربر الان به منوی پایین صفحه (reply keyboard) هم منتقل شد.
@router.message(F.text == "👮 مدیریت ادمین‌ها")
async def admin_manage_admins_from_menu(message: types.Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("👮 مدیریت ادمین‌های فرعی\n\nادمین را انتخاب کنید یا ادمین جدید اضافه کنید:", reply_markup=admin_manage_admins_keyboard(db.get_all_sub_admins()))


@router.callback_query(F.data == "subadm_add")
async def subadm_add(callback: types.CallbackQuery, state: FSMContext):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    await state.set_state(AdminStates.waiting_sub_admin_id)
    try:
        await callback.message.edit_text(
            "آیدی عددی تلگرام ادمین فرعی را بفرستید. اگر خواستید نام هم ثبت شود این‌طور بفرستید:\n"
            "نام | آیدی\n"
            "مثال: علی پشتیبان | 123456789",
            reply_markup=admin_back_button(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.message(AdminStates.waiting_sub_admin_id)
async def subadm_add_id(message: types.Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id): return
    raw = clean_numeric_id(message.text or "")
    if "|" in raw:
        name, tid = [x.strip() for x in raw.split("|", 1)]
        tid = clean_numeric_id(tid)
    else:
        tid, name = raw, raw
    if not tid.isdigit():
        await message.answer("❌ فرمت درست نیست. فقط آیدی عددی یا «نام | آیدی» بفرستید.", reply_markup=admin_back_button()); return
    try:
        db.add_sub_admin(tid, name=name or tid, permissions=[])
    except Exception:
        logger.exception("خطا در ثبت ادمین فرعی")
        await message.answer("⚠️ ثبت آیدی با خطا مواجه شد. لطفاً دوباره تلاش کنید.", reply_markup=admin_back_button())
        return
    await _notify_main_admin_action(message.bot, message.from_user, "افزودن ادمین فرعی", tid, name or tid)
    await state.clear()
    await message.answer("✅ ادمین فرعی اضافه شد. حالا قابلیت‌هایش را با تیک انتخاب کنید:", reply_markup=admin_permissions_keyboard(tid, []))

@router.callback_query(F.data.startswith("subadm_") & ~F.data.startswith("subadmperm_") & ~F.data.startswith("subadmdel_"))
async def subadm_open(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    tid = callback.data.split("_",1)[1]
    adm = db.get_sub_admin(tid)
    if not adm:
        await callback.answer("❌ ادمین یافت نشد.", show_alert=True); return
    try:
        await callback.message.edit_text(f"👤 ادمین فرعی: {tid}\n\nقابلیت‌ها را تیک بزنید/بردارید:", reply_markup=admin_permissions_keyboard(tid, adm.get("permissions") or []))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("subadmperm_"))
async def subadm_toggle_perm(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    rest = callback.data.removeprefix("subadmperm_")
    # رفع باگ: برخی کلیدهای قابلیت خودشان زیرخط دارند (مثل "vpn_panel"، "orders_toggle")، با rsplit("_", 1) قبلی
    # آیدی و نام قابلیت اشتباه جدا می‌شدند (مثلاً آیدی "...vpn" و قابلیت "panel") و ادمین پیدا نمی‌شد. الان با ':' (که در هیچ‌کدام وجود ندارد) جدا می‌شوند.
    if ":" in rest:
        tid, perm = rest.split(":", 1)
    else:
        # سازگاری با دکمه‌های قدیمی (اگر callback زیرخطدار قبلی هنوز جایی فعال باشد)
        tid, perm = rest.rsplit("_", 1)
    adm = db.get_sub_admin(tid)
    if not adm: await callback.answer("❌ یافت نشد.", show_alert=True); return
    perms = set(adm.get("permissions") or [])
    if perm in perms: perms.remove(perm)
    else: perms.add(perm)
    db.update_sub_admin_permissions(tid, sorted(perms))
    await _notify_main_admin_action(callback.bot, callback.from_user, "تغییر دسترسی ادمین فرعی", tid, perm)
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_permissions_keyboard(tid, sorted(perms)))
    except TelegramBadRequest:
        pass
    # فیکس: قبلاً فقط کیبورد تأیید خود ادمین اصلی به‌روز می‌شد، ولی منوی پایین صفحه‌ی خود ادمین فرعی
    # (که از آخرین /start او ساخته شده بود) همچنان قدیمی می‌ماند تا دوباره /start بزند. حالا
    # همینجا منوی پایین صفحه‌اش را با دسترسی‌های تازه دوباره برایش می‌فرستیم تا فوراً به‌روز شود.
    try:
        await callback.bot.send_message(
            int(tid),
            "🔄 دسترسی‌های شما توسط ادمین اصلی به‌روزرسانی شد. منوی پایین صفحه شما براساس دسترسی‌های جدید به‌روز شد.",
            reply_markup=admin_reply_keyboard(permissions=set(perms), is_main_admin=False),
        )
    except Exception:
        logger.exception("اطلاع ادمین فرعی از تغییر دسترسی ناموفق بود")
    await callback.answer("✅ بروزرسانی شد")

@router.callback_query(F.data.startswith("subadmdel_"))
async def subadm_delete(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    tid = callback.data.removeprefix("subadmdel_")
    db.delete_sub_admin(tid)
    await _notify_main_admin_action(callback.bot, callback.from_user, "حذف ادمین فرعی", tid, "")
    try:
        await callback.message.edit_text("✅ ادمین حذف شد.", reply_markup=admin_manage_admins_keyboard(db.get_all_sub_admins()))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.message(F.text == "📥 صف درخواست‌ها")
async def menu_admin_request_queue(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    order_count = len(db.get_pending_orders(limit=200))
    receipt_count = len(db.get_pending_receipts(limit=200))
    await message.answer(
        "📥 صف درخواست‌ها\n\nچه چیزی رو می‌خوای بررسی کنی؟ 👇",
        reply_markup=admin_request_queue_menu(order_count, receipt_count),
    )


@router.message(F.text == "🤝 نمایندگی (تخفیف VIP)")
async def menu_admin_agency(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    agents = db.get_all_agents()
    text = (
        "🤝 هنوز هیچ نماینده‌ای ثبت نشده.\n\nبرای افزودن، دکمه‌ی زیر را بزنید 👇"
        if not agents else
        "🤝 نمایندگان فعلی (تخفیف خودکار روی VIP)\n\nروی هرکدام بزنید تا مثل بخش «کاربران» مدیریتش کنید 👇"
    )
    await message.answer(text, reply_markup=admin_agency_menu(agents))


@router.message(F.text == "🗂 دسته‌بندی‌های VIP")
async def menu_admin_vip_categories(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🗂 دسته‌بندی‌های VIP\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور VIP» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر د��ته، روی آن بزنید 👇",
        reply_markup=admin_vip_categories_keyboard(),
    )




@router.message(F.text == "🔴 خاموش کردن سفارشات")
async def menu_admin_orders_off(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    db.set_orders_enabled(False)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await message.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_closed", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.") + "\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🔴 بخش سفارشات خاموش شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=_admin_reply_kb_for(message.from_user.id, False))


@router.message(F.text == "🟢 روشن کردن سفارشات")
async def menu_admin_orders_on(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    db.set_orders_enabled(True)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await message.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_opened", "🟢 ربات مجدداً فعال شد!") + "\n\nبا زدن /start می‌توانید دوباره سفارش ثبت کنید.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🟢 بخش سفارشات روشن شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=_admin_reply_kb_for(message.from_user.id, True))


def _error_logs_text(logs: list, total: int) -> str:
    if not logs:
        return "🦖 لاگ خطاها\n\n✅ تا این لحظه هیچ خطای ثبت نشده."
    return f"🦖 لاگ خطاها — {total} خطای ثبت‌شده\n\nروی هرکدام بزنید تا جزئیاتش رو ببینید 👇"


async def _open_error_logs(target, edit: bool = False):
    logs = db.get_error_logs(limit=15)
    total = db.count_error_logs()
    text = _error_logs_text(logs, total)
    kb = admin_error_logs_keyboard(logs)
    if edit:
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


# 🐛 فیکس: برچسب دکمه در منو به «🦖 لاگ خطاها و Audit» تغییر کرده بود ولی
# این فیلتر همچنان فقط متن قدیمی را می‌گرفت، پس دکمه بی‌جواب می‌ماند.
@router.message(F.text.in_({"🦖 لاگ خطاها", "🦖 لاگ خطاها و Audit"}))
async def menu_admin_error_logs(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await _open_error_logs(message, edit=False)


@router.callback_query(F.data == "errlogrefresh")
async def admin_error_logs_refresh(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _open_error_logs(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("errlogdetail_"))
async def admin_error_log_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    log_id = int(callback.data.split("_", 1)[1])
    log = db.get_error_log(log_id)
    if log is None:
        await callback.answer("❌ یافت نشد.", show_alert=True)
        return
    # 🆕 فیکس: قبلاً پیام خطا جداگانه از یک طول ثابت (message تا ۲۰۰۰ کاراکتر + traceback تا ۳۵۰۰ کاراکتر) ساخته می‌شد؛ اگر خطایی با پیام (message) طولانی ثبت می‌شد (مثلاً همین خطای «MESSAGE_TOO_LONG»)، مجموع این دو می‌توانست از سقف تلگرام (۴۰۹۶) رد شود و خود صفحه‌ی «لاگ خطاها» هم با همان خطا مواجه می‌شد و ادمین اصلاً نمی‌توانست جزئیات را ببیند. حالا طول قابل‌نمایش traceback پویا بر اساس طول واقعی بقیه محاسبه می‌شود تا مجموع همیشه زیر سقف تلگرام بماند، و بازهم یک try/except محافظتی اضافه شده تا اگر بازهم محاسبه جایی کم بیافتاد، پیام با یک نسخه‌ی کاملاً مختصر‌شده بازهم فرستاده شود تا این بخش از پنل ادمین هرگز با ارور متوقف نشود.
    error_type_display = html.escape(str(log["error_type"]))
    occurred_at_display = html.escape(str(log.get("occurred_at") or ""))
    message_display = html.escape(str(log.get("message") or "")[:300])
    header = (
        f"⚠️ {error_type_display}\n"
        f"🕐 {occurred_at_display}\n\n"
        f"📝 {message_display}\n\n"
    )
    wrapper_len = len("<pre></pre>")
    max_tb_len = max(TELEGRAM_TEXT_LIMIT - len(header) - wrapper_len - 20, 200)
    tb = html.escape(str(log.get("traceback") or "")[:max_tb_len])
    text = f"{header}<pre>{tb}</pre>"
    try:
        await callback.message.edit_text(text, reply_markup=admin_error_log_detail_keyboard())
    except TelegramBadRequest as e:
        if is_message_too_long_error(e):
            fallback_text = truncate_for_telegram(
                f"⚠️ {error_type_display}\n🕐 {occurred_at_display}\n\n📝 {message_display}"
            )
            await callback.message.edit_text(fallback_text, reply_markup=admin_error_log_detail_keyboard())
        else:
            raise
    await callback.answer()


@router.callback_query(F.data == "errlogclear")
async def admin_error_logs_clear_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑 مطمئنید می‌خواهید همه‌ی لاگ ها پاک شوند؟", reply_markup=admin_error_logs_clear_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "errlogclearconfirm")
async def admin_error_logs_clear_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    db.clear_error_logs()
    await _open_error_logs(callback, edit=True)
    await callback.answer("✅ پاک شد.")


# ---------------------------------------------------------------------------
# 📊 آمار
# ---------------------------------------------------------------------------
_STAT_PERIOD_LABELS = {
    "all": "📊 آمار کل", "hour": "🕐 یک ساعت اخیر", "today": "☁️ امروز", "yesterday": "☀️ دیروز",
    "this_month": "☀️ ماه فعلی", "last_month": "☁️ ماه قبل",
}


def _render_stats_text(period: str, custom_dt=None) -> str:
    start, end = stat_period_bounds_utc(period, custom_dt)
    s = db.get_dashboard_stats(start, end)
    title = _STAT_PERIOD_LABELS.get(period)
    if not title:
        title = f"🗓 آمار روز {to_jalali_str(custom_dt, with_time=False)}"
    lines = [
        f"📊 آمار ربات — {title}",
        "━━━━━━━━━━━━━━━",
        "",
        "👥 کاربران",
        f"• کل کاربران: {s['total_users']:,} نفر",
        f"• کاربر جدید در این بازه: {s['new_users']:,} نفر",
        f"• مشتریان (تاکنون خریدار): {s['total_buyers_ever']:,} نفر",
        f"• مسدودشده: {s['blocked_users']:,} نفر",
        "",
        "🛒 فروش",
        f"• سرویس آماده (VIP): {s['plan_new_count']:,} عدد | {s['plan_new_amount']:,} تومان",
        f"• سرویس سفارشی (بساز سرویس خودت): {s['custom_new_count']:,} عدد | {s['custom_new_amount']:,} تومان",
        f"• جمع فروش جدید: {s['sales_count']:,} عدد | {s['sales_amount']:,} تومان",
        f"• تمدید: {s['renew_count']:,} عدد | {s['renew_amount']:,} تومان ({s['renew_pct']}٪ از درآمد)",
        f"• تست رایگان: {s['test_count']:,} عدد",
        f"💵 درآمد کل (فروش + تمدید): {s['total_revenue']:,} تومان",
    ]
    methods = s.get("by_method") or {}
    if methods:
        lines += ["", "💳 درآمد به تفکیک روش پرداخت"]
        for name, v in sorted(methods.items(), key=lambda kv: -kv[1]["amount"]):
            _pm = {"card": "💳 کارت به کارت", "wallet": "👛 کیف پول", "crypto": "🪙 رمزارز",
                   "gateway": "🏦 درگاه", "zarinpal": "🏦 درگاه"}.get(str(name), str(name))
            lines.append(f"• {_pm}: {v['count']:,} عدد | {v['amount']:,} تومان")
    lines += [
        "",
        "💰 کیف پول",
        f"• موجودی کل کاربران: {s['total_wallet']:,} تومان",
        f"• موجودی مسدود (پاداش دعوت): {s['total_locked']:,} تومان",
        f"• شارژ در این بازه: {s['wallet_charged']:,} تومان",
        "",
        "📦 سرویس‌ها و زیرساخت",
        f"• سرویس‌های فعال: {s['active_services']:,} از {s['total_services']:,}",
        f"• پنل‌ها: {s['enabled_panels']:,} فعال از {s['total_panels']:,}",
        f"• نمایندگان: {s['total_agents']:,} نفر",
        "",
        "📥 در انتظار اقدام (لحظه‌ای)",
        f"• رسیدهای در انتظار تأیید: {s['pending_receipts']:,}",
        f"• سفارش‌های در انتظار ارسال: {s['pending_orders']:,}",
        f"• تیکت‌های باز: {s['open_tickets']:,}",
        "",
        "📈 تحلیل",
        f"• نرخ تبدیل به مشتری: {s['conversion_rate']}٪",
        f"• میانگین درآمد هر خریدار (در بازه): {s['avg_per_customer']:,} تومان",
        f"• درآمد پیش‌بینی‌شده‌ی ماهانه: {s['predicted_monthly']:,} تومان",
    ]
    return "\n".join(lines)


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext | None = None):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.message.edit_text(_render_stats_text("all"), reply_markup=admin_stats_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("stat_"))
async def admin_stats_period(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    period = callback.data.replace("stat_", "")
    if period == "custom":
        await state.set_state(AdminStates.waiting_stats_custom_date)
        await callback.message.answer(
            "🗓 تاریخ مورد نظر را به شمسی بفرست (مثلاً 1404/06/11):"
        )
        await callback.answer()
        return
    try:
        await callback.message.edit_text(_render_stats_text(period), reply_markup=admin_stats_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer("🔄 بروزرسانی شد.")


@router.message(AdminStates.waiting_stats_custom_date)
async def admin_stats_custom_date(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    dt = parse_jalali_date(message.text or "")
    if dt is None:
        await message.answer("❌ فرمت تاریخ نامعتبر است. مثال درست: 1404/06/11 — دوباره بفرست:")
        return
    await state.clear()
    await message.answer(_render_stats_text("custom", dt), reply_markup=admin_stats_keyboard())


# ---------------------------------------------------------------------------
# 👥 لیست کاربران
# ---------------------------------------------------------------------------
_USERLIST_TITLES = {
    "tx": "💰 بیشترین تراکنش", "svc": "📦 بیشترین سرویس", "last": "🕒 آخرین تراکنش",
    "new": "🆕 جدیدترین عضو", "wal": "👛 موجودی کیف پول", "nobuy": "👤 بدون خرید",
}


def _userlist_view(sort: str, page: int):
    per_page = 10
    if sort not in db.USER_LIST_SORTS:
        sort = "new"
    total = db.count_users_sorted(sort)
    last_page = max(0, (total - 1) // per_page) if total else 0
    page = max(0, min(int(page), last_page))
    users = db.get_users_sorted_page(sort, page, per_page)
    title = f"👥 لیست کاربران — {_USERLIST_TITLES[sort]}"
    if not users:
        text = f"{title}\n\nهنوز کاربری در این لیست نیست."
    else:
        start = page * per_page + 1
        text = f"{title} — {total:,} نفر\nنمایش {start} تا {start + len(users) - 1}:\n\nبرای مدیریت هرکدام روی نامش بزن 👇"
    return text, admin_userlist_page_keyboard(users, page, (page + 1) * per_page < total, sort)


@router.callback_query(F.data == "admin_userlist")
async def admin_user_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    text, kb = _userlist_view("new", 0)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ul_(tx|svc|last|new|wal|nobuy)_\d+$"))
async def admin_userlist_sorted(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, sort, page = callback.data.split("_")
    text, kb = _userlist_view(sort, int(page))
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # همان صفحه دوباره زده شد
    await callback.answer()


# سازگاری با دکمه‌های قدیمی (پیام‌های قبلی در چت)
@router.callback_query(F.data.in_({"admin_userlist_active", "admin_userlist_all"}))
async def admin_userlist_legacy(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    text, kb = _userlist_view("tx" if callback.data.endswith("active") else "new", 0)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("userpage_"))
async def admin_userlist_page_nav(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split("_")
    kind = parts[1] if len(parts) > 2 else "all"
    text, kb = _userlist_view("tx" if kind == "active" else "nobuy" if kind == "non" else "new", int(parts[-1]))
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("useropen_"))
async def admin_user_open(callback: types.CallbackQuery):
    """با زدن روی هرکدام از کاربران در لیست، مستقیم وارد صفحه‌ی مدیریت همان کاربر می‌شویم
    (همان صفحه‌ای که از طریق «🔍 جستجوی حرفه‌ای» هم بازش می‌شد)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ن��ارید.", show_alert=True)
        return

    uid = callback.data.replace("useropen_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        callback.message, text, user["telegram_id"], db.is_user_blocked(user["telegram_id"]), edit=True
    )
    await callback.answer()


@router.callback_query(F.data.startswith("useropenmsg_"))
async def admin_user_open_new_message(callback: types.CallbackQuery):
    """مثل «مدیریت کاربر» ولی صفحه را در «پیام جدید» می‌فرستد؛ برای دکمه‌ی زیر پیام‌های لاگ/گزارش
    تا خودِ پیام لاگ پاک/ویرایش نشود."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("useropenmsg_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        callback.message, text, user["telegram_id"], db.is_user_blocked(user["telegram_id"]), edit=False
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📒 حسابداری کاربر — تراکنش‌ها (کیف پول، خرید، شارژ، منشأ پول) با صفحه‌بندی
# ---------------------------------------------------------------------------
def _tx_type_label(tx_type: str) -> str:
    return {
        "charge": "💳 شارژ (تأیید کارت‌به‌کارت/دستی توسط ادمین)",
        "purchase": "🛒 خرید سرویس (کسر از کیف پول)",
        "referral_locked": "🔒 پاداش دعوت (در انتظار آزادسازی)",
        "referral_release": "🔓 آزادسازی پاداش دعوت",
        "referral_pending": "🔒 پاداش دعوت (در انتظار)",
    }.get(tx_type, f"📄 {tx_type}")


@router.callback_query(F.data.startswith("accounting_"))
async def admin_user_accounting(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    _, uid, page_str = callback.data.split("_")
    page = int(page_str)
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    per_page = 10
    txs = db.get_transactions_page(user["id"], page, per_page)
    has_next = len(txs) == per_page

    text = (
        f"📒 حسابداری کاربر {user['name']} (🆔 {user['telegram_id']})\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود (پاداش دعوت در انتظار): {user['locked_wallet']:,} تومان\n"
        f"🛒 مجموع خرید: {user['total_purchase']:,} تومان\n\n"
        f"📋 تراکنش‌ها (صفحه {page + 1}):\n\n"
    )
    if not txs:
        text += "— تراکنشی در این صفحه نیست —"
    else:
        for tx in txs:
            sign = "+" if tx["amount"] >= 0 and tx["type"] in ("charge", "referral_release") else "-"
            text += (
                f"{_tx_type_label(tx['type'])}\n"
                f"{sign}{abs(tx['amount']):,} تومان | {tx['status']}\n"
                f"📝 {tx.get('description') or '-'}\n"
                f"🕐 {tx['created_at']}\n\n"
            )

    await callback.message.edit_text(text, reply_markup=admin_accounting_keyboard(uid, page, has_next))
    await callback.answer()


# ---------------------------------------------------------------------------
# 🔍 جستجوی حرفه‌ای
# ---------------------------------------------------------------------------
_SEARCH_PROMPTS = {
    "user": "🔍 جستجوی کاربر\n\nبخشی از نام، آیدی عددی یا کد دعوت را بفرستید؛ همه‌ی کاربران مرتبط لیست می‌شوند 👇",
    "config": "🔎 جستجوی کانفیگ\n\nبخشی از شناسه/نام سرویس (مثلاً چند رقم از آن) یا نام صاحبش را بفرستید؛ همه‌ی سرویس‌های مرتبط لیست می‌شوند 👇",
}
_SEARCH_PAGE_SIZE = 8


def _unified_search(query: str, mode: str = "user") -> list:
    """جستجوی جزئی همزمان روی کاربران (نام/آیدی/کد دعوت) و سرویس‌ها (شناسه/نام/صاحب)."""
    q = clean_numeric_id(query or "").strip() if (query or "").strip().replace(" ", "").isdigit() else (query or "").strip()
    if not q:
        return []
    items = []
    seen_users = set()
    if mode != "config":
        for u in db.search_users_multi(q, limit=200):
            seen_users.add(u["id"])
            items.append(("u", u))
        return items
    tokens = [t.casefold() for t in q.split() if t.strip()] or [q.casefold()]
    try:
        cfgs = db.get_all_configs_for_admin_search(include_deleted=True)
    except Exception:
        cfgs = []
    for cfg in cfgs:
        hay = " ".join(str(x or "") for x in (
            cfg.get("service_id"), cfg.get("plan"), cfg.get("product_name"),
            cfg.get("owner_name"), cfg.get("owner_tid"),
        )).casefold()
        if all(t in hay for t in tokens):
            items.append(("c", cfg))
    return items


async def _render_search_results(target, state: FSMContext, page: int):
    data = await state.get_data()
    query = data.get("search_query") or ""
    mode = data.get("search_mode") or "user"
    items = _unified_search(query, mode)
    if not items:
        text = f"🔎 برای «{query}» هیچ موردی پیدا نشد.\n\nعبارت دیگری بفرستید:"
        await state.set_state(AdminStates.waiting_search_config if mode == "config" else AdminStates.waiting_search_user)
        kb = admin_config_search_keyboard()
    else:
        kind_txt = "🚀 سرویس" if mode == "config" else "👥 کاربر"
        text = (f"🔎 نتیجه‌ی جستجوی «{query}»\n\n{kind_txt}: {len(items)} مورد\n"
                "برای مدیریت روی هر مورد بزنید 👇\n(برای جستجوی دیگر، عبارت جدید را همین‌جا بفرستید)")
        kb = admin_unified_search_results_keyboard(items, page, _SEARCH_PAGE_SIZE, mode)
    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _open_search_prompt(target, state: FSMContext, mode: str = "user"):
    await state.clear()
    await state.update_data(search_mode=mode)
    await state.set_state(AdminStates.waiting_search_config if mode == "config" else AdminStates.waiting_search_user)
    kb = admin_config_search_keyboard()
    prompt = _SEARCH_PROMPTS[mode]
    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(prompt, reply_markup=kb)
        except Exception:
            await target.message.answer(prompt, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(prompt, reply_markup=kb)


@router.callback_query(F.data == "admin_search")
async def admin_search_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _open_search_prompt(callback, state)


@router.callback_query(F.data == "admin_config_search")
async def admin_config_search_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    await _open_search_prompt(callback, state, "config")


@router.callback_query(F.data.startswith("srchpg_"))
async def admin_search_page(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        page = int(callback.data.replace("srchpg_", ""))
    except ValueError:
        page = 0
    data = await state.get_data()
    if not data.get("search_query"):
        await callback.answer("⚠️ نتیجه منقضی شده؛ دوباره جستجو کنید.", show_alert=True)
        return
    await _render_search_results(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("searchmode_"))
async def admin_search_pick_mode(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    mode = callback.data.replace("searchmode_", "")
    prompts = {
        "id":     "🔢 آیدی عددی تلگرام کاربر را وارد کنید:",
        "name":   "👤 بخشی از نام کاربر را وارد کنید (حداقل ۲ حرف):",
        "invite": "🎟 کد دعوت کاربر را وارد کنید:",
        "all":    "🔎 عبارتی وارد کنید (نام، آیدی یا کد دعوت):",
    }
    await state.update_data(search_mode=mode)
    await state.set_state(AdminStates.waiting_search_user)
    await callback.message.edit_text(prompts.get(mode, prompts["all"]))
    await callback.answer()


@router.callback_query(F.data.startswith("searchopen_"))
async def admin_search_open_user(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("searchopen_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        callback.message, text, user["telegram_id"],
        db.is_user_blocked(user["telegram_id"]), edit=True
    )
    await state.clear()
    await callback.answer()


async def _search_text_received(message: types.Message, state: FSMContext, mode: str):
    if not _is_admin(message.from_user.id):
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ لطفاً متن جستجو را بفرستید.", reply_markup=admin_config_search_keyboard())
        return
    await state.update_data(search_query=query, search_mode=mode)
    await state.set_state(AdminStates.waiting_search_config if mode == "config" else AdminStates.waiting_search_user)
    await _render_search_results(message, state, 0)


@router.message(AdminStates.waiting_search_user)
async def admin_search_result(message: types.Message, state: FSMContext):
    await _search_text_received(message, state, "user")


@router.message(AdminStates.waiting_search_config)
async def admin_config_search_result(message: types.Message, state: FSMContext):
    await _search_text_received(message, state, "config")


# ---------------------------------------------------------------------------
# 💳 شارژ کیف پول (تأیید/رد رسید + شارژ دستی)
# دسترسی از طریق «🔍 جستجوی حرفه‌ای» ← دکمه «💰 شارژ دستی» (برای بهینه شدن فضای منو)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approve_"))
async def approve_charge(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"approvecharge_{callback.data}") or not db.claim_admin_action(f"approvecharge_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    # فرمت: approve_{uid}_{amount}[_{receipt_id}] (رسیدهای قدیمی/Mini App ممکن است بدون receipt_id باشند)
    parts = callback.data.split("_")
    try:
        uid = parts[1]
        amount = int(parts[2])
        receipt_id = int(parts[3]) if len(parts) > 3 and parts[3] else None
    except (IndexError, ValueError):
        await callback.answer("❌ اطلاعات رسید نامعتبر است.", show_alert=True)
        return

    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    if receipt_id is not None:
        _rc = db.get_pending_receipt_by_id(receipt_id)
        if _rc is not None and _rc.get("status") != "pending":
            await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
            return

    db.add_to_wallet(user["id"], amount, "شارژ کیف پول (تأیید رسید)")
    _gift = db.credit_charge_bonus(user["id"], amount)
    _credited = amount
    try:
        if receipt_id is not None and _rc is not None and _rc.get("discount_code"):
            _credited = db.apply_wallet_discount_credit(user["id"], amount, _rc.get("discount_code"), _rc.get("extra"))
    except Exception:
        logger.exception("اعمال تخفیف شارژ کیف پول هنگام تأیید رسید ناموفق بود")
    try:
        if receipt_id is not None:
            db.resolve_pending_receipt_by_id(receipt_id, outcome="approved")
        else:
            db.resolve_pending_receipt("charge", uid, amount, outcome="approved")
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید و شارژ شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        if db.user_pref_enabled(telegram_id=uid, key="wallet_alerts"):
            notif_settings.gate("receipt_approved")
            await send_notification_sticker(callback.bot, int(uid), "notif_wallet_charge")
            await callback.bot.send_message(int(uid), user_text("notif_wallet_charge_approved", amount=_credited) + (f"\n🎟 با کد تخفیف، {_credited - amount:,} تومان تخفیف گرفتید (پرداختی: {amount:,} تومان)." if _credited > amount else "") + (f"\n🎁 هدیه‌ی شارژ: {_gift:,} تومان" if _gift else ""))
    except Exception:
        pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "تأیید رسید شارژ کیف پول", uid, f"مبلغ {amount:,} تومان" + (f" (شارژ نهایی {_credited:,} با تخفیف)" if _credited > amount else ""))
    await callback.answer("✅ شارژ شد.")


@router.callback_query(F.data.startswith("reject_"))
async def reject_charge(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"rejectcharge_{callback.data}") or not db.claim_admin_action(f"rejectcharge_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    # فرمت: reject_{uid}[_{receipt_id}]
    parts = callback.data.split("_")
    try:
        uid = parts[1]
        receipt_id = int(parts[2]) if len(parts) > 2 and parts[2] else None
    except (IndexError, ValueError):
        await callback.answer("❌ اطلاعات رسید نامعتبر است.", show_alert=True)
        return
    try:
        if receipt_id is not None:
            db.resolve_pending_receipt_by_id(receipt_id, outcome="rejected")
        else:
            db.resolve_pending_receipt("charge", uid, outcome="rejected")
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        notif_settings.gate("receipt_rejected")
        await send_notification_sticker(callback.bot, int(uid), "notif_receipt_rejected")
        await callback.bot.send_message(int(uid), user_text("notif_receipt_rejected_short"))
    except Exception:
        pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "رد رسید", uid if 'uid' in locals() else str(order_id) if 'order_id' in locals() else "", "")
    await callback.answer("❌ رد شد.")


@router.callback_query(F.data.startswith("custom_"))
async def custom_charge_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    # فرمت: custom_{uid}[_{receipt_id}]
    _parts = callback.data.split("_")
    uid = _parts[1] if len(_parts) > 1 else ""
    _rid = int(_parts[2]) if len(_parts) > 2 and _parts[2].isdigit() else None
    await state.update_data(charge_target=uid, charge_receipt_id=_rid)
    await state.set_state(AdminStates.waiting_custom_amount)
    await callback.message.answer(f"💵 مبلغ شارژ برای کاربر {uid} را به تومان ارسال کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_custom_amount)
async def custom_charge_apply(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید.")
        return

    data = await state.get_data()
    uid = data.get("charge_target")
    amount = int(clean_numeric_id(message.text))

    user = db.get_user(uid)
    if user is None:
        await message.answer("❌ کاربر یافت نشد.")
        await state.clear()
        return

    db.add_to_wallet(user["id"], amount, "شارژ دستی توسط ادمین")
    try:
        _rid = data.get("charge_receipt_id")
        if _rid is not None:
            db.resolve_pending_receipt_by_id(int(_rid), outcome="approved")
        else:
            db.resolve_pending_receipt("charge", uid, outcome="approved")
    except Exception:
        pass
    await message.answer(f"✅ {amount:,} تومان به کیف پول کاربر {uid} اضافه شد.")
    try:
        if db.user_pref_enabled(telegram_id=uid, key="wallet_alerts"):
            notif_settings.gate("wallet_charged")
            await send_notification_sticker(message.bot, int(uid), "notif_wallet_charge")
            await message.bot.send_message(int(uid), user_text("notif_wallet_charged", amount=amount))
    except Exception:
        pass
    await state.clear()


# ---------------------------------------------------------------------------
# 💳 تأیید/رد رسید خرید کارت‌به‌کارت (سرویس)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approvepay|"))
async def approve_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    parts = callback.data.split("|")
    if len(parts) == 5:
        _, uid, plan_key, price_str, receipt_id_str = parts
        receipt_id = int(receipt_id_str)
    elif len(parts) == 4:
        _, uid, plan_key, price_str = parts
        receipt_id = None
    else:
        await callback.answer("❌ اطلاعات رسید نامعتبر است.", show_alert=True)
        return
    price = int(price_str)
    if plan_key == "@" and receipt_id is not None:
        _rc = db.get_pending_receipt_by_id(receipt_id)
        plan_key = (_rc or {}).get("plan_key") or (_rc or {}).get("extra") or plan_key
    if receipt_id is not None:
        _rc0 = db.get_pending_receipt_by_id(receipt_id)
        if _rc0 is not None and _rc0.get("status") != "pending":
            await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
            return
    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if user is None or plan is None:
        await callback.answer("❌ کاربر یا پلن یافت نشد.", show_alert=True)
        return

    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        await callback.answer(
            "⚠️ این کاربر قبلاً از «تست رایگان» استفاده کرده؛ هر کاربر فقط یک‌بار می‌تواند این پلن را بگیرد.",
            show_alert=True,
        )
        return

    # رسید را فقط بعد از اعتبارسنجی کامل claim می‌کنیم؛ در نسخه‌ی قبلی اگر
    # یکی از بررسی‌های بعدی خطا می‌داد، receipt برای همیشه «پردازش‌شده» علامت
    # می‌خورد و دفعه‌ی بعد دکمه‌ی تأیید بی‌دلیل می‌گفت قبلاً پردازش شده.
    if is_duplicate_action(f"approvepay_{callback.data}") or not db.claim_admin_action(f"approvepay_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شد��.", show_alert=True)
        return

    # 🐛 فیکس: کد تخفیف کارت‌به‌کارت حالا فقط همین‌جا (تأیید ادمین) مصرف
    # می‌شود، نه هنگام ارسال رسید؛ اگر ادمین رد کند سهم کد تخفیف مصرف نمی‌شود.
    pending = None
    try:
        if receipt_id is not None:
            pending = db.get_pending_receipt_by_id(receipt_id)
        if pending is None:
            pending = db.find_pending_receipt("plan_card", uid, price) or db.find_pending_receipt("plan_crypto", uid, price)
            if pending and pending.get("id") is not None:
                receipt_id = int(pending["id"])
    except Exception:
        pending = None

    db.record_purchase(user["id"], price, f"خرید {plan['name']} (کارت به کارت)")

    if pending and pending.get("discount_code"):
        try:
            db.use_discount(pending["discount_code"], user["id"])
        except Exception:
            logging.getLogger(__name__).exception("خطا در مصرف کد تخفیف کارت‌به‌کارت")

    _pm = "پرداخت ارزی" if (pending and str(pending.get("kind")) == "plan_crypto") else "کارت به کارت"
    # «زمان ثبت سفارش» در پیام تحویل = لحظه‌ی ارسال رسید توسط کاربر (نه لحظه‌ی تأیید ادمین)
    _receipt_ts = (pending or {}).get("created_at") if pending else None
    order_id = db.create_order(user["id"], plan_key, plan["name"], plan_type(plan_key), price, payment_method=_pm, created_at=_receipt_ts,
                               service_name=((pending or {}).get("service_name") if pending else None))

    # تأیید کارت‌به‌کارت = پرداخت واقعی و موفق؛ بنابراین فقط بعد از ساخت
    # سفارشِ همین خرید، پاداش دعو�� ر�� آزاد می‌کنیم.
    if price > 0 and db.referral_purchase_qualifies(plan.get("volume_gb", 0), paid_purchase=True, is_free_test=db.is_free_test_key(plan_key)):
        try:
            db.complete_referral(user["id"], qualifying_order_id=order_id)
        except ValueError:
            pass
    try:
        db.resolve_pending_receipt_by_id(receipt_id, outcome="approved")
    except Exception:
        pass

    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید شد و خرید ثبت شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        _discount_note = f"\n🎟 کد تخفیف {pending['discount_code']} برای این خرید مصرف شد." if pending and pending.get("discount_code") else ""
        _confirm_text = user_text("notif_purchase_approved", plan_name=plan["name"], discount_note=_discount_note)
        # fix: به کاربر بگوییم کد تخفیف همین تأیید مصرف شده تا گیج نشود چرا دیگر قابل‌استفاده نیست.
        if pending and pending.get("discount_code"):
            _confirm_text += f"\n🎟 کد تخفیف {pending['discount_code']} برای این خرید مصرف شد."
        notif_settings.gate("receipt_approved")
        await send_notification_sticker(callback.bot, int(uid), "notif_purchase_approved")
        await callback.bot.send_message(int(uid), _confirm_text)
    except Exception:
        pass
    await callback.message.answer(
        "📤 برای ارسال کانفیگ این خرید:", reply_markup=admin_purchase_notify_keyboard(uid, plan_key, order_id)
    )
    await _notify_main_admin_action(callback.bot, callback.from_user, "تأیید خرید/سفارش", uid if 'uid' in locals() else str(order_id) if 'order_id' in locals() else "", "ثبت شد")
    await callback.answer("✅ تأیید شد.")


@router.callback_query(F.data.startswith("rejectpay|"))
async def reject_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    parts = callback.data.split("|")
    if len(parts) == 3:
        _, uid, receipt_id_str = parts
        receipt_id = int(receipt_id_str)
    elif len(parts) == 2:
        _, uid = parts
        receipt_id = None
    else:
        await callback.answer("❌ اطلاعات رسید نامعتبر است.", show_alert=True)
        return
    if is_duplicate_action(f"rejectpay_{callback.data}") or not db.claim_admin_action(f"rejectpay_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return
    try:
        if receipt_id is not None:
            db.resolve_pending_receipt_by_id(receipt_id, outcome="rejected")
        else:
            db.resolve_pending_receipt("plan_card", uid, outcome="rejected")
            db.resolve_pending_receipt("plan_crypto", uid, outcome="rejected")
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        notif_settings.gate("receipt_rejected")
        await send_notification_sticker(callback.bot, int(uid), "notif_receipt_rejected")
        await callback.bot.send_message(int(uid), user_text("notif_receipt_rejected"))
    except Exception:
        pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "رد رسید", uid if 'uid' in locals() else str(order_id) if 'order_id' in locals() else "", "")
    await callback.answer("❌ رد شد.")


# ---------------------------------------------------------------------------
# 🛠 تأیید/رد رسید کارت‌به‌کارت برای «بساز سرویس خودت» / «تمدید سرویس»
#
# 🐛 فیکس بحرانی: این سه هندلر (approvecustom_ / rejectcustom_ /
# dismisscustomorder_) در نسخه‌ی V2.19 از فایل حذف شده بودند — فقط همین هدر
# کامنت با چند خط خالی زیرش باقی مانده بود. نتیجه این بود که دکمه‌های
# «✅ تأیید پرداخت» و «❌ رد رسید» روی رسیدهای «بساز سرویس خودت» و همچنین
# دکمه‌ی حذف از صف، کاملاً بی‌اثر بودند (aiogram هیچ هندلری پیدا نمی‌کرد و
# کلیک بی‌جواب می‌ماند) — دقیقاً همان «تایید و ارسال کانفیگ به مشکل خورده».
# اینجا دقیقاً با همان الگوی approvepay|/rejectpay| بالا بازسازی شده‌اند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approvecustom_"))
async def approve_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("approvecustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ این سفارش یافت نشد.", show_alert=True)
        return

    user = db.get_user_by_id(order["user_id"])
    if user is None:
        await callback.answer("❌ کاربر این سفارش یافت نشد.", show_alert=True)
        return

    if is_duplicate_action(f"approvecustom_{callback.data}") or not db.claim_admin_action(f"approvecustom_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    db.set_custom_order_status(order_id, "paid")
    if order["price"] > 0:
        db.record_purchase(user["id"], order["price"], "خرید سرویس سفارشی (کارت به کارت)")
        if db.referral_purchase_qualifies(order.get("volume_gb", 0), paid_purchase=True, is_free_test=False):
            try:
                db.complete_referral(user["id"])
            except ValueError:
                pass
    try:
        db.resolve_pending_receipt("custom", user["telegram_id"], outcome="approved")
    except Exception:
        pass

    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید شد و سفارش ثبت شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        notif_settings.gate("receipt_approved")
        await send_notification_sticker(callback.bot, int(user["telegram_id"]), "notif_purchase_approved")
        await callback.bot.send_message(
            int(user["telegram_id"]),
            user_text("notif_purchase_approved", plan_name="سرویس سفارشی", discount_note=""),
        )
    except Exception:
        pass
    await callback.message.answer(
        "📤 برای ارسال کانفیگ این سفارش:", reply_markup=admin_custom_order_notify_keyboard(order_id)
    )
    await _notify_main_admin_action(callback.bot, callback.from_user, "تأیید سفارش سفارشی", str(order_id), "ثبت شد")
    await callback.answer("✅ تأیید شد.")


@router.callback_query(F.data.startswith("rejectcustom_"))
async def reject_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("rejectcustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ این سفارش یافت نشد.", show_alert=True)
        return

    if is_duplicate_action(f"rejectcustom_{callback.data}") or not db.claim_admin_action(f"rejectcustom_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    db.set_custom_order_status(order_id, "rejected")
    user = db.get_user_by_id(order["user_id"])
    if user:
        try:
            db.resolve_pending_receipt("custom", user["telegram_id"], outcome="rejected")
        except Exception:
            pass

    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    if user:
        try:
            notif_settings.gate("receipt_rejected")
            await send_notification_sticker(callback.bot, int(user["telegram_id"]), "notif_receipt_rejected")
            await callback.bot.send_message(int(user["telegram_id"]), user_text("notif_receipt_rejected"))
        except Exception:
            pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "رد رسید سفارش سفارشی", str(order_id), "")
    await callback.answer("❌ رد شد.")


@router.callback_query(F.data.startswith("dismisscustomorder_"))
async def dismiss_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("dismisscustomorder_", ""))
    db.set_custom_order_status(order_id, "dismissed")
    await _render_order_queue(callback)
    await callback.answer("🗑 از صف پاک شد.")


def _format_test_package_for_log(order: dict | None, package_text: str) -> str:
    """فرمت بسته‌ی تست رایگان برای لاگ کانال.

    تست‌های زیر ۱ گیگ با مگابایت و تست‌های زیر ۱ روز با ساعت نمایش داده
    می‌شوند تا مقادیری مثل 0.0488281 گیگ و 0.0416667 روز در لاگ دیده نشوند.
    """
    if not order or order.get("order_type") != "test":
        return package_text

    try:
        plan_key = order.get("plan_key")
        plan = db.get_effective_plan(plan_key) if plan_key else None
    except Exception:
        plan = None

    if not plan:
        return package_text

    # حجم: کمتر از ۱ گیگ → مگابایت؛ در غیر این صورت → گیگ
    try:
        volume_gb = float(plan.get("volume_gb", 0) or 0)
    except (TypeError, ValueError):
        volume_gb = 0

    if volume_gb <= 0:
        volume_label = "نامحدود"
    elif volume_gb < 1:
        volume_label = f"{round(volume_gb * 1024):g} مگابایت"
    else:
        volume_label = f"{volume_gb:g} گیگ"

    # مدت: کمتر از ۱ روز → ساعت؛ ۱ روز و بیشتر → روز
    try:
        days = float(plan.get("days", 0) or 0)
    except (TypeError, ValueError):
        days = 0

    if days <= 0:
        duration_label = "نامحدود"
    elif days < 1:
        hours = days * 24
        if abs(hours - round(hours)) < 1e-9:
            duration_label = f"{round(hours):g} ساعت"
        else:
            duration_label = f"{hours:g} ساعت"
    else:
        duration_label = f"{int(days) if days.is_integer() else days:g} روز"

    return f"{volume_label} | {duration_label}"


async def _log_fulfilled_order(
    bot, user: dict, *, plan_order_id=None, custom_order_id=None,
    target_config_id=None, service_id=None, service_name: str = "-",
    package_text: str = "-", expiry_text: str = "-",
    payment_method: str | None = None, panel_id=None,
    added_volume: float = 0, added_days: int = 0, label_override: str | None = None,
    amount_text_override: str | None = None,
):
    """لاگ استاندارد سفارش (کانال اعتماد) + گزارش نهایی برای ادمین را می‌سازد و ارسال می‌کند."""
    label = "🛒 خرید جدید"
    amount_text = "-"
    order = None

    if plan_order_id:
        order = db.get_order(plan_order_id)
        if order:
            amount_text = f"{order['price']:,} تومان" if order["price"] else "رایگان"
            # تشخیص نوع لاگ فقط از خودِ نوع سفارش انجام می‌شود. وجود
            # target_config_id به‌تنهایی به معنی تمدید نیست؛ هنگام تحویل یک
            # خرید عادی هم target_config_id داریم و قبلاً همین باعث می‌شد
            # «🛒 خرید جدید» اشتباهاً «🔁 تمدید سرویس» ثبت شود.
            order_type = str(order.get("order_type") or "").strip().lower()
            if order_type in ("test", "free_test"):
                label = "🎁 تست رایگان"
                package_text = _format_test_package_for_log(order, package_text)
            elif order_type == "renew":
                label = "🔁 تمدید سرویس"
            else:
                label = "🛒 خرید جدید"
    elif custom_order_id:
        order = db.get_custom_order(custom_order_id)
        if order:
            amount_text = f"{order['price']:,} تومان" if order["price"] else "رایگان"
            label = "🔁 تمدید سرویس" if order.get("order_type") == "renew" else "🛠 سرویس سفارشی جدید"
    elif target_config_id:
        label = "🔁 تمدید سرویس"

    if label_override:
        label = label_override
    if amount_text_override:
        amount_text = amount_text_override
    if not payment_method:
        payment_method = (order or {}).get("payment_method") or ""
    if "تست" in label:
        payment_method = "رایگان"
    payment_method = payment_method or "-"

    # 🔁 در لاگ تمدید، «نام بسته» باید همان بسته‌ای باشد که کاربر در ربات خریده (نام پلن VIP که ادمین گذاشته)،
    # نه اطلاعاتِ حجم/زمانِ خوانده‌شده از پنل.
    if "تمدید" in label:
        try:
            _bought = None
            if plan_order_id and order and str(order.get("plan_name") or "").strip():
                _bought = str(order["plan_name"]).strip()
            if not _bought and target_config_id:
                _tc = db.get_config_by_id(target_config_id) or {}
                _pk = (_tc.get("plan_key") or "").strip()
                if _pk:
                    _bought = str((db.get_vip_plan(_pk) or {}).get("name") or "").strip() or None
                if not _bought:
                    _bought = str(_tc.get("product_name") or "").strip() or None
            if _bought:
                package_text = _bought
        except Exception:
            logging.getLogger(__name__).exception("خواندن نام بسته‌ی خریداری‌شده برای لاگ تمدید ناموفق بود")

    renewal_details = alerts._renewal_log_details(added_volume, added_days) if "تمدید" in label else ""
    if "تمدید" in label and not renewal_details and order and order.get("volume_gb") is not None:
        renewal_details = alerts._renewal_log_details(order.get("volume_gb") or 0, order.get("days") or 0)

    username = await alerts.fetch_username(bot, user["telegram_id"])
    # اسم پنل (همان نامی که ادمین برای پنل گذاشته) → متغیر {panel_name} در متن لاگ
    _log_panel_name = None
    try:
        _pid = panel_id
        if not _pid and target_config_id:
            _pid = (db.get_config_by_id(target_config_id) or {}).get("panel_id")
        if _pid:
            _log_panel_name = (db.get_vpn_panel(_pid) or {}).get("name") or None
    except Exception:
        logging.getLogger(__name__).exception("خواندن اسم پنل برای لاگ سفارش ناموفق بود")
    await alerts.log_order_to_channel(
        bot,
        panel_name=_log_panel_name,
        order_label=label,
        user=user,
        username=username,
        service_id=service_id,
        service_name=service_name,
        package_text=package_text,
        amount_text=amount_text,
        expiry_text=expiry_text,
        renewal_details=renewal_details,
        payment_method=payment_method,
    )

    # گزارش ادمین (خطای آن نباید لاگ کانال یا تحویل سرویس را خراب کند)
    try:
        panel_name = None
        if panel_id:
            panel_obj = db.get_vpn_panel(panel_id)
            panel_name = (panel_obj or {}).get("name") or None
        await alerts.send_admin_order_report(
            bot,
            order_label=label,
            payment_method=payment_method,
            user=user,
            service_name=service_id or service_name,
            package_text=package_text,
            amount_text=amount_text,
            panel_name=panel_name,
        )
    except Exception:
        logging.getLogger(__name__).exception("گزارش ادمین سفارش ارسال نشد")


# ---------------------------------------------------------------------------
# 🛒 خرید اشتراک برای خود ادمین
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_buy_subscription")
async def admin_buy_subscription(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if not db.is_orders_enabled():
        await callback.answer("🔴 سفارشات در حال حاضر خاموش است.", show_alert=True)
        return

    await state.clear()
    if db.get_user(callback.from_user.id) is None:
        db.create_user(callback.from_user.id, callback.from_user.full_name or "Admin")

    await callback.message.answer(
        "🛒 خرید اشتراک برای خودم\n\n🚀 دسته‌بندی سرویس مورد نظر را انتخاب کنید 👇",
        reply_markup=vip_categories_keyboard(),
    )
    await callback.answer()


@router.message(F.text == "🛒 خرید اشتراک برای خودم")
async def admin_buy_subscription_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if not db.is_orders_enabled():
        await message.answer("🔴 سفارشات در حال حاضر خاموش است.")
        return
    await state.clear()
    if db.get_user(message.from_user.id) is None:
        db.create_user(message.from_user.id, message.from_user.full_name or "Admin")
    await message.answer(
        "🛒 خرید اشتراک برای خودم\n\n🚀 دسته‌بندی سرویس مورد نظر را انتخاب کنید 👇",
        reply_markup=vip_categories_keyboard(),
    )


# ---------------------------------------------------------------------------
# 📤 ارسال کانفیگ دستی — فقط لینک ساب (کیوآرکد خودکار از روی همان لینک ساخته می‌شود؛
# اگر ساخت QR شکست بخورد، سرویس بدون QR ارسال می‌شود). نام/حجم/مدت به‌صورت خودکار
# از روی خود لینک تشخیص داده می‌شود؛ اگر تشخیص خودکار جواب نداد، به‌صورت
# دستی از ادمین پرسیده می‌شود
# ---------------------------------------------------------------------------
async def _start_send_flow(callback: types.CallbackQuery, state: FSMContext, uid: str,
                            target_config_id: int | None, order_id: int | None,
                            plan_order_id: int | None = None, hint: str = ""):
    await state.update_data(
        send_target_uid=uid,
        send_target_config_id=target_config_id,
        send_order_id=order_id,
        send_plan_order_id=plan_order_id,
        qr_file_id=None,
    )
    await state.set_state(AdminStates.waiting_send_qr_link)
    await callback.message.answer(
        f"📤 ارسال کانفیگ برای کاربر {uid}{hint}\n\n🔗 لینک ساب (Subscription) این سرویس رو ارسال کن:\n"
        "(کیوآرکد خودکار از روی همین لینک ساخته می‌شود)"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sendvip_"))
async def send_config_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("sendvip_", "")
    uid, _, plan_order_id = raw.partition("|")
    await _start_send_flow(
        callback, state, uid, target_config_id=None, order_id=None,
        plan_order_id=int(plan_order_id) if plan_order_id else None,
    )


# 🐛 فیکس بحرانی: هندلر «📤 شروع ارسال کانفیگ» برای سفارش‌های «بساز سرویس
# خودت» هم حذف شده بود (دقیقاً کنار همان sendvip_ بالا). یعنی بعد از تأیید
# رسید، دکمه‌ی ارسال کانفیگ هیچ کاری نمی‌کرد. اینجا با همان _start_send_flow
# بازسازی شده، فقط با order_id سفارش سفارشی تا حجم/مدت از خود سفارش خوانده شود.
@router.callback_query(F.data.startswith("sendcustomorder_"))
async def send_custom_order_config_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("sendcustomorder_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ این سفارش یافت نشد.", show_alert=True)
        return
    user = db.get_user_by_id(order["user_id"])
    if user is None:
        await callback.answer("❌ کاربر این سفارش یافت نشد.", show_alert=True)
        return
    await _start_send_flow(
        callback, state, str(user["telegram_id"]),
        target_config_id=order.get("target_config_id"), order_id=order_id, plan_order_id=None,
    )


@router.message(AdminStates.waiting_send_qr_photo)
async def send_config_legacy_photo_state(message: types.Message, state: FSMContext):
    """مرحله‌ی «عکس کیوآرکد» حذف شده؛ اگر ادمینی با نسخه‌ی قبلی وسط این مرحله مانده باشد،
    به مرحله‌ی لینک منتقل می‌شود (و اگر همین پیام لینک بود، همان‌جا پردازش می‌شود)."""
    await state.set_state(AdminStates.waiting_send_qr_link)
    if (message.text or "").strip().lower().startswith(("http://", "https://")):
        await send_config_link_received(message, state)
        return
    await message.answer("🔗 دیگر نیازی به عکس کیوآرکد نیست؛ فقط لینک ساب (Subscription) این سرویس رو ارسال کن:")


@router.message(AdminStates.waiting_send_qr_link)
async def send_config_link_received(message: types.Message, state: FSMContext):
    sub_link = (message.text or "").strip()
    if not sub_link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک ساب رو با http یا https ارسال کن:")
        return

    data = await state.get_data()
    order_id = data.get("send_order_id")
    order = db.get_custom_order(order_id) if order_id else None

    await message.answer("⏳ در حال تشخیص خودکار اطلاعات از روی لینک...")
    meta = await extract_meta(sub_link)
    userinfo = (meta or {}).get("userinfo") or {}
    fetched_name = (meta or {}).get("name")

    # 🔗 لینک را به کاربر واقعی همان پنل (مرزبان/پاسارگارد) وصل می‌کنیم تا یوزرنیم، حجم و زمان
    # دقیقاً از پنل بیاید و سرویس بعداً (مصرف، اتصال، دستگاه‌ها، تغییر لینک) از پنل میرور شود.
    resolved = None
    try:
        resolved = await panel_api.resolve_manual_link(sub_link, name_hint=fetched_name)
    except Exception:
        logger.warning("تشخیص کاربر پنل از روی لینک ناموفق بود", exc_info=True)
    link_data = {"send_panel_id": None, "send_service_id": None, "send_source": None}
    panel_name = None
    snap = None
    if resolved:
        snap = resolved.get("snapshot") or {}
        panel_name = resolved["username"]
        link_data = {"send_panel_id": resolved["panel"]["id"], "send_service_id": resolved["service_id"],
                     "send_source": resolved["panel_type"]}
        await message.answer(f"✅ سرویس در پنل «{resolved['panel'].get('name') or ''}» پیدا شد: {panel_name}")

    if order:
        # اطلاعات حجم/مدت از خود سفارش (که کاربر برایش پول پرداخت کرده) قابل‌اعتمادتر است
        volume_gb = order["volume_gb"]
        days = order["days"]
        name = panel_name or fetched_name or order.get("custom_name") or "کاربر"
        await state.update_data(send_volume_gb=volume_gb, send_days=days, send_name=name, send_sub_link=sub_link, **link_data)
        await _finalize_send(message, state)
        return

    if snap:
        from webapp_user_ext import to_epoch as _to_epoch  # expire در پاسارگارد ISO و در مرزبان epoch است
        _exp = _to_epoch(snap.get("expire"))
        volume_gb = _gb_from_bytes(snap.get("total")) if snap.get("total") else None
        days = days_remaining(_exp) if _exp else None
        if volume_gb is None and not snap.get("total"):
            volume_gb = _gb_from_bytes(userinfo.get("total"))
        if days is None and not _exp:
            days = days_remaining(userinfo.get("expire"))
    else:
        volume_gb = _gb_from_bytes(userinfo.get("total"))
        days = days_remaining(userinfo.get("expire"))

    # نام/حجم/مدت دیگر از ادمین پرسیده نمی‌شود: هرچه از پنل/لینک استخراج شد استفاده می‌شود
    # و مقدارِ قابل‌تشخیص‌نبودن به‌صورت «—» (خط تیره) برای کاربر ارسال می‌شود؛ هیچ‌وقت شکست نمی‌خورد.
    await state.update_data(send_sub_link=sub_link, send_volume_gb=volume_gb, send_days=days,
                            send_name=panel_name or fetched_name, **link_data)
    await _finalize_send(message, state)


async def _send_delivery_message(bot, uid: int, caption: str, caption_entities: list, qr_bytes: bytes | None, markup):
    """تحویل سرویس به کاربر: اگر QR ساخته شده باشد عکس QR + کپشن، وگرنه (یا اگر ارسال عکس خطا داد)
    فقط پیام متنی. ایموجی پریمیوم/ریچ‌تکست کپشن در هر دو حالت حفظ می‌شود؛ اگر تلگرام entityها را
    رد کند یک‌بار بدون entity تکرار می‌شود تا تحویل سرویس هرگز گم نشود.
    خروجی: (پیام ارسال‌شده | None، file_id عکس QR | None)"""
    from aiogram.types import BufferedInputFile, LinkPreviewOptions
    if qr_bytes:
        try:
            sent = await bot.send_photo(
                uid, BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=caption, caption_entities=caption_entities or None, parse_mode=None, reply_markup=markup,
            )
            fid = sent.photo[-1].file_id if getattr(sent, "photo", None) else None
            return sent, fid
        except Exception:
            logger.warning("ارسال عکس QR تحویل سرویس ناموفق بود؛ ارسال بدون QR", exc_info=True)
    no_preview = LinkPreviewOptions(is_disabled=True)
    for ents in ((caption_entities or None), None):
        try:
            sent = await bot.send_message(
                uid, caption, entities=ents, parse_mode=None, reply_markup=markup, link_preview_options=no_preview,
            )
            return sent, None
        except TelegramBadRequest:
            if ents is None:
                raise
            logger.warning("تلگرام entityهای متن تحویل را نپذیرفت؛ ارسال بدون entity", exc_info=True)
    return None, None


async def _finalize_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("send_target_uid")
    sub_link = data.get("send_sub_link")
    target_config_id = data.get("send_target_config_id")
    order_id = data.get("send_order_id")
    plan_order_id = data.get("send_plan_order_id")
    name = data.get("send_name") or "—"

    # 🐛 فیکس: قبلاً متن تحویلی همیشه ثابت "نامحدود" نشان می‌داد و سقف کاربر (HWID Limit) واقعی پلن را نادیده می‌گرفت. حالا اگر این ارسال از روی یک سفارش پلن VIP باشد سقف کاربر همان پلن خوانده و نمایش داده می‌شود (0 = نامحدود).
    user_limit = None
    if plan_order_id:
        plan_order = db.get_order(plan_order_id)
        if plan_order and plan_order.get("plan_key"):
            order_plan = db.get_effective_plan(plan_order["plan_key"])
            if order_plan:
                user_limit = order_plan.get("user_limit")
    volume_gb = data.get("send_volume_gb")
    days = data.get("send_days")

    user = db.get_user(uid)
    if user is None or sub_link is None:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا (🔗 لینک ساب) دوباره امتحان کن.")
        await state.clear()
        return

    volume_text = _format_volume_gb_label(volume_gb) if volume_gb is not None else "—"
    days_text = f"{days} روز" if days is not None else "—"
    plan_name_for_delivery = None
    if plan_order_id:
        plan_order = db.get_order(plan_order_id)
        if plan_order and plan_order.get("plan_key"):
            plan_obj = db.get_effective_plan(plan_order["plan_key"])
            if plan_obj:
                plan_name_for_delivery = plan_obj.get("name")
    delivery_label = (
        _delivery_service_label(
            plan_name_for_delivery,
            volume_gb,
            days,
            user_limit,
            plan_order.get("plan_key") if plan_order_id and plan_order else None,
        )
        if plan_name_for_delivery or plan_order_id
        else _delivery_service_label(name, volume_gb, days, None)
    )
    is_test_delivery = bool(plan_order_id and plan_order and db.is_free_test_key(plan_order.get("plan_key")))
    delivery_text_key = "service_delivery_test_text" if is_test_delivery else "service_delivery_text"
    # ✅ یکسان با ارسال خودکار (_deliver_panel_link): همان قالب «config_delivery» و همان Entityها
    # (ایموجی پرمیوم) برای هر دو مسیر دستی و خودکار استفاده می‌شود.
    from subscription import format_service_package as _fsp
    _plan_key_for_text = plan_order.get("plan_key") if plan_order_id and plan_order else None
    _vol_t, _days_t = _fsp(volume_gb if volume_gb else None, days if days else None, _plan_key_for_text)
    if not order_id:
        # مسیر دستی بدون سفارش: مقدار نامشخص = «—» (نه «نامحدود»)
        if volume_gb is None:
            _vol_t = "—"
        if days is None:
            _days_t = "—"
    _users_t = "نامحدود" if not user_limit else str(user_limit)
    _fallback = (
        "✅ سرویس با موفقیت ایجاد شد\n\n"
        f"👤 نام کاربری سرویس : {name}\n"
        "🇺🇳 لوکیشن: مولتی لوکیشن+تانل\n"
        f"⏳ مدت زمان: {_days_t}\n"
        f"🗜 حجم سرویس: {_vol_t}\n"
        f"👤 تعداد کاربر:{_users_t}\n\n"
        "لینک اتصال:\n"
        f"{sub_link}\n\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید."
    )
    _order_created_at = None
    try:
        if plan_order_id and plan_order:
            _order_created_at = plan_order.get("created_at")
        elif order_id:
            _co = db.get_custom_order(order_id)
            _order_created_at = (_co or {}).get("created_at")
    except Exception:
        _order_created_at = None
    _deliv_key = "config_delivery_test" if (is_test_delivery and _ui_editor.has_text_override("config_delivery_test")) else "config_delivery"
    _deliv_values = {"name": name, "location": "مولتی لوکیشن+تانل", "days": _days_t, "volume": _vol_t,
         "users": _users_t, "sub_link": sub_link,
         "plan_name": plan_name_for_delivery or name, "expiry_date": (
             (now_tehran_naive() + timedelta(days=days)).strftime("%Y-%m-%d") if days is not None else "—"),
         **__import__("utils").delivery_time_vars(_order_created_at)}
    caption, _raw_entities = _ui_editor.render_template(_deliv_key, _deliv_values, fallback=_fallback)
    caption_entities = [
        types.MessageEntity(type=e["type"], offset=e["offset"], length=e["length"], custom_emoji_id=e.get("custom_emoji_id"))
        for e in (_raw_entities or [])
    ]

    expiry_date = None
    if days is not None:
        expiry_date = (now_tehran_naive() + timedelta(days=days)).strftime("%Y-%m-%d")

    encrypted = crypto.encrypt_config(sub_link)
    plan_name = f"{name} | {volume_text} | {days_text}"  # مقدارهای نامشخص «—» هستند

    # بدون عکس QR از ادمین: QR از روی خود لینک ساخته می‌شود؛ اگر ساخت QR شکست بخورد (None)، بدون QR ارسال می‌شود.
    from utils import make_qr_png_bytes
    qr_bytes = make_qr_png_bytes(sub_link)

    _pid = data.get("send_panel_id")
    _sid = data.get("send_service_id")
    _src = data.get("send_source")
    if target_config_id:
        db.update_config(target_config_id, plan_name, encrypted, expiry=expiry_date)
        config_id = target_config_id
    else:
        config_id = db.add_config(user["id"], plan_name, encrypted, expiry=expiry_date, config_type=("test" if is_test_delivery else "vip"),
                                  plan_key=((plan_order or {}).get("plan_key") if plan_order_id and not is_test_delivery else None))
    if order_id and not plan_order_id and not target_config_id:
        try:
            _co2 = db.get_custom_order(order_id) or {}
            if str(_co2.get("order_type") or "new") != "renew":
                db.set_config_custom_option(config_id, int(_co2.get("option_num") or 0))
        except Exception:
            logger.warning("ذخیره‌ی گزینه‌ی سرویس سفارشی انجام نشد cfg=%s", config_id, exc_info=True)
    if _pid and _sid and _src:
        try:
            db.link_config_to_panel(config_id, _pid, _sid, _src, service_name=name)
        except Exception:
            logger.warning("اتصال سرویس دستی به پنل ذخیره نشد cfg=%s", config_id, exc_info=True)

    if order_id:
        db.set_custom_order_status(order_id, "fulfilled")
    if plan_order_id:
        db.set_order_status(plan_order_id, "fulfilled")

    _wg_cfg = None
    try:
        _pp = db.get_vpn_panel(_pid) if _pid else None
        if _pp and panel_api.needs_wg_delivery(_pp):
            _wg_cfg = config_id
    except Exception:
        _wg_cfg = None
    try:
        await send_notification_sticker(message.bot, int(uid), "notif_service_delivery")
        _deliv_kb = config_delivery_keyboard(bot_info.get("connection_guide_url"), is_test=is_test_delivery, wg_cfg_id=_wg_cfg)
        from utils import send_delivery_rich as _sdr
        _rich = await _sdr(message.bot, int(uid), _deliv_key, _deliv_values, _deliv_kb, qr_bytes)
        if _rich is not None:
            sent_msg, sent_qr_file_id = _rich
        else:
            sent_msg, sent_qr_file_id = await _send_delivery_message(
                message.bot, int(uid), caption, caption_entities, qr_bytes, _deliv_kb,
            )
        # QR ذخیره‌شده‌ی قبلی (در تمدید با لینک جدید) دیگر معتبر نیست؛ فقط QR تازه نگه داشته می‌شود.
        try:
            db.set_config_qr(config_id, sent_qr_file_id)
        except Exception:
            logger.debug("ذخیره QR سرویس ناموفق بود", exc_info=True)
        # منوی پایینی دائمی کاربر نباید بعد از تحویل سرویس مخفی شود.
        db.set_keyboard_hidden(int(uid), False)
        await message.bot.send_message(int(uid), "⬇️ منوی اصلی شما همچنان در دسترس است.", reply_markup=main_reply_keyboard(int(uid)))
        await message.answer("✅ کانفیگ برای کاربر ارسال شد." + ("" if sent_qr_file_id else "\nℹ️ (بدون کیوآرکد ارسال شد)"))
        await _notify_main_admin_action(message.bot, message.from_user, "ارسال کانفیگ", uid, plan_name)
    except Exception as e:
        await message.answer(f"⚠️ سرویس ذخیره شد ولی ارسال پیام به کاربر ناموفق بود: {e}")

    await _log_fulfilled_order(
        message.bot, user, plan_order_id=plan_order_id, custom_order_id=order_id,
        target_config_id=target_config_id, service_name=name,
        package_text=f"{volume_text} | {days_text}", expiry_text=expiry_date or "نامحدود",
    )

    await state.clear()


# ---------------------------------------------------------------------------
# 🎮 ارسال کانفیگ گیمینگ (WireGuard) — بدون کیوآرکد/mirroring/تمدید:
# ادمین ابتدا شناسه سرویس و لینک ساب را وارد می‌کند، سپس هر تعداد فایل
# .conf که بخواهد آپلود می‌کند (هرکدام می‌تواند کپشن/لوکیشن جدا داشته باشد)
# و در پایان با زدن دکمه «✅ پایان ارسال فایل‌ها» همه‌ی فایل‌ها یک‌جا برای
# کاربر ارسال می‌شوند - دقیقاً مطابق فرمت فایلی که در کانال نمونه دیده می‌شود.
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 📝 مدیریت جامع متن‌های کاربر و اعلان‌ها
# همه‌ی متن‌های قابل ویرایش در text_catalog.py تعریف شده‌اند.
TEXT_KEYS = TEXTS


def _chunk2(items):
    rows = []
    for i in range(0, len(items), 2):
        rows.append(items[i:i + 2])
    return rows


def _text_manager_keyboard(category: str | None = None):
    """مدیریت متن‌ها؛ در هر ردیف تا ۲ دکمه (🐛 فیکس: قبلاً ۳ دکمه در هر ردیف بود که روی صفحه‌کلید شلوغ و فشرده بود)."""
    buttons = []
    if category is None:
        categories = list(TEXT_CATEGORIES.keys())
        for i in range(0, len(categories), 2):
            row = []
            for cat in categories[i:i + 2]:
                count = len(TEXT_CATEGORIES[cat])
                row.append(InlineKeyboardButton(
                    text=f"📝 {cat.split(' ', 1)[-1]} ({count})",
                    callback_data=f"textcat_{categories.index(cat)}",
                    style="primary",
                ))
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
        return types.InlineKeyboardMarkup(inline_keyboard=buttons)

    items = TEXT_CATEGORIES.get(category, [])
    delivery_labels = {
        "service_delivery_text": "✏️ تغییر متن تحویل سرویس (بسته‌ها)",
        "service_delivery_apps_button": "📱 دکمه لینک برنامه‌ها (بسته‌ها)",
        "service_delivery_connection_button": "��� دکمه اتصال کانفینگ (بسته‌ها)",
        "service_delivery_test_text": "✏️ تغییر متن تحویل سرویس (تست)",
        "service_delivery_test_apps_button": "📱 دکمه لینک برنامه‌ها (تست)",
        "service_delivery_test_connection_button": "🔧 دکمه اتصال کانفینگ (تست)",
    }
    for i in range(0, len(items), 2):
        row = []
        for key, default in items[i:i + 2]:
            value = user_text(key, default).replace("\n", " ")[:22]
            label = delivery_labels.get(key, f"✏️ {key[:14]} | {value}")
            if key in delivery_labels:
                label = f"✏️ {label}"
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"textedit_{key}",
                style="primary",
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به دسته‌ها", callback_data="admin_texts", style="primary")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_texts")
async def admin_texts(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "✏️ ویرایش متن و دکمه‌ها\n\nابتدا نوع موردی که می‌خواهید ویرایش کنید را انتخاب کنید:",
        reply_markup=_ui_editor.editor_mode_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ✏️ ویرایشگر جامع متن و دکمه‌ها
# ui_editor.py از قبل منوها و ذخیره‌سازی را دارد؛ این handlerها اتصال آن‌ها
# به Router ادمین را کامل می‌کنند. این بخش مستقل است و به منطق خرید/تحویل دست
# نمی‌زند.
# ---------------------------------------------------------------------------

def _ui_editor_entities(message: types.Message) -> list[dict]:
    # متن ساده → entities ؛ پست مدیادار (عکس/ویدیو با کپشن، مثلاً فوروارد از کانال) → caption_entities
    ents = getattr(message, "entities", None)
    if not ents and getattr(message, "text", None) is None:
        ents = getattr(message, "caption_entities", None)
    return serialize_message_entities(ents)


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def message_text_and_entities(message: types.Message) -> tuple[str, list[dict]]:
    """متن (یا کپشن) پیام را همراه با entityهایش برمی‌گرداند و whitespace ابتدا/انتها را طوری
    حذف می‌کند که offset ایموجی‌های پریمیوم (custom_emoji) جابه‌جا نشود.
    🐛 فیکس: قبلاً فقط text.strip() زده می‌شد و entityها با offset قدیمی ذخیره می‌شدند؛ اگر پیام با
    خط خالی/فاصله شروع می‌شد (یا کپشن بود) ایموجی پریمیوم ذخیره نمی‌شد و به‌صورت ایموجی عادی می‌رفت."""
    text = message.text if getattr(message, "text", None) is not None else (getattr(message, "caption", None) or "")
    entities = _ui_editor_entities(message)
    lead = len(text) - len(text.lstrip())
    stripped = text.strip()
    shift = _utf16_len(text[:lead])
    total = _utf16_len(stripped)
    fixed = []
    for e in entities:
        try:
            off = int(e.get("offset", 0)) - shift
            end = off + int(e.get("length", 0))
        except (TypeError, ValueError):
            continue
        off, end = max(0, off), min(total, end)
        if end <= off:
            continue
        item = dict(e)
        item["offset"], item["length"] = off, end - off
        fixed.append(item)
    return stripped, fixed


async def _edit_with_entities(callback: types.CallbackQuery, *parts, reply_markup=None):
    """پیام ویرایشگر را با entityهای واقعی (ایموجی پریمیوم، بولد، …) ویرایش می‌کند تا پیش‌نمایش
    دقیقاً مثل چیزی باشد که کاربر می‌بیند. parts: رشته یا (متن, entities).
    اگر تلگرام entity را نپذیرفت، یک‌بار با متن ساده تکرار می‌شود."""
    text, ents = _ui_editor.compose_with_entities(*parts)
    ent_models = message_entities_from_dicts(ents)
    try:
        await callback.message.edit_text(text, entities=ent_models or None, parse_mode=None, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            raise
        logger.warning("ویرایش پیش‌نمایش با entity رد شد؛ تلاش با متن ساده: %s", exc)
        await callback.message.edit_text(text, parse_mode=None, reply_markup=reply_markup)


@router.callback_query(F.data == "admin_text_editor")
async def ui_editor_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "✏️ ویرایش متن و دکمه‌ها\n\nنوع ویرایش را انتخاب کنید:",
        reply_markup=_ui_editor.editor_mode_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_mode:"))
async def ui_editor_mode(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    mode = callback.data.split(":", 1)[1]
    if mode == "text":
        markup = _ui_editor.text_categories_keyboard()
        title = "✏️ ویرایش متن‌ها — دسته‌بندی را انتخاب کنید"
    elif mode == "buttons":
        markup = _ui_editor.button_bundles_keyboard()
        title = "🔘 ویرایش دکمه‌ها — بسته‌ی دکمه‌ها را انتخاب کنید"
    else:
        await callback.answer("❌ حالت ویرایش نامعتبر است.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(title, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cat:"))
async def ui_editor_category(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, mode, category = parts
    if category not in _ui_editor.CATEGORIES:
        await callback.answer("❌ بخش پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"{_ui_editor.category_label(category)}\n\nمورد موردنظر را انتخاب کنید:",
        reply_markup=_ui_editor.screens_keyboard(category, mode),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_auto_groups:"))
async def ui_editor_auto_groups(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except Exception:
        page = 0
    await callback.message.edit_text("🧩 پیام‌های واقعی ربات\n\nفایل/بخش موردنظر را انتخاب کنید:", reply_markup=_ui_editor.text_categories_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("ui_auto_group:"))
async def ui_editor_auto_group(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    source_file, page_raw = parts[1], parts[2]
    try:
        page = max(0, int(page_raw))
    except Exception:
        page = 0
    await callback.message.edit_text(
        f"🧩 پیام‌های واقعی — {source_file}\n\nمورد موردنظر را انتخاب کنید:",
        reply_markup=_ui_editor.text_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_auto:"))
async def ui_editor_auto_preview(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    current = _ui_editor.get_text(key, "")
    await _edit_with_entities(
        callback, "🧩 پیش‌نمایش متن واقعی\n\n", (current, _ui_editor.get_entities(key)),
        reply_markup=_ui_editor.text_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_screen:"))
async def ui_editor_screen(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, mode, key = parts
    if key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    await state.clear()
    sc = _ui_editor.SCREENS[key]
    if mode == "text":
        text, _ents = _ui_editor.preview_text_entities(key)
        await _edit_with_entities(callback, (text, _ents), reply_markup=_ui_editor.text_screen_keyboard(key))
        await callback.answer()
        return
    elif mode == "buttons":
        text = f"🔘 دکمه‌های «{sc.get('label', key)}»\n\nدکمه موردنظر را انتخاب کنید یا وضعیت/رنگ آن را تغییر دهید:"
        markup = _ui_editor.button_screen_keyboard(key)
    else:
        await callback.answer("❌ حالت نامعتبر است.", show_alert=True)
        return
    await callback.message.edit_text(text[:3900], reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("ui_screen_textpage:"))
async def ui_editor_text_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    key, page = parts[1], max(0, int(parts[2]))
    _ptext, _pents = _ui_editor.preview_text_entities(key, page)
    await _edit_with_entities(callback, (_ptext, _pents), reply_markup=_ui_editor.text_screen_keyboard(key, page))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_button_page:"))
async def ui_editor_button_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    key, page = parts[1], max(0, int(parts[2]))
    sc = _ui_editor.SCREENS[key]
    await callback.message.edit_text(
        f"🔘 دکمه‌های «{sc.get('label', key)}»\n\nدکمه موردنظر را انتخاب کنید یا وضعیت/رنگ آن را تغییر دهید:",
        reply_markup=_ui_editor.button_screen_keyboard(key, page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_edit_text:"))
async def ui_editor_edit_text(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in _ui_editor.SCREENS:
        await callback.answer("❌ متن پیدا نشد.", show_alert=True)
        return
    current = _ui_editor.get_text(key, _ui_editor.SCREENS[key].get("default", ""))
    variable_hint = ""
    _vars = _ui_editor.SCREENS[key].get("vars")
    if _vars:
        variable_hint = "\n\n🔧 متغیرهای قابل استفاده:\n" + " | ".join("{" + v + "}" for v in _vars)
    await state.update_data(ui_edit_kind="text", ui_text_key=key)
    await state.set_state(AdminStates.waiting_ui_editor_input)
    await _edit_with_entities(
        callback,
        f"✏️ ویرایش متن\n\n«{_ui_editor.SCREENS[key].get('label', key)}»\n\nمتن فعلی:\n",
        (current, _ui_editor.get_entities(key)),
        f"{variable_hint}\n\nمتن جدید را ارسال کنید.\n\n💎 اگر Premium/Custom Emoji بفرستید، entity آن هم ذخیره می‌شود.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_edit_auto:"))
async def ui_editor_edit_auto(callback: types.CallbackQuery, state: FSMContext):
    """Backward compat: قدیمی AST scanner callbacks را به uitc_edit هدایت می‌کند."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    current = _ui_editor.get_text(key, "")
    await state.update_data(ui_edit_kind="text", ui_text_key=key)
    await state.set_state(AdminStates.waiting_ui_editor_input)
    _cur_ents = _ui_editor.get_entities(key)
    _cur_text, _cur_ents = _ui_editor.compose_with_entities((current, _cur_ents), limit=1200)
    await _edit_with_entities(
        callback,
        f"✏️ ویرایش متن\n\nکلید: {key}\n\n",
        (_cur_text, _cur_ents),
        "\n\n—\nمتن جدید را ارسال کنید.\n💎 برای Premium Emoji همان ایموجی را در ابتدا بفرستید.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()




# ===========================================================================
# کاتالوگ متن‌ها — text_catalog handlers
# ===========================================================================

@router.callback_query(F.data == "uitc_cats")
async def uitc_open_categories(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    await state.clear()
    await callback.message.edit_text(
        "✏️ ویرایش متن‌ها — دسته‌بندی را انتخاب کنید:",
        reply_markup=_ui_editor.text_categories_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("uitc_cat:"))
async def uitc_open_category(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    category = callback.data.split(":", 1)[1]
    await state.clear()
    try:
        entries = _ui_editor.text_catalog_entries(category)
    except Exception:
        entries = []
    if not entries:
        await callback.answer("❌ دسته خالی یا پیدا نشد.", show_alert=True); return
    await callback.message.edit_text(
        f"""✏️ {category}

متن موردنظر را انتخاب کنید ({len(entries)} متن):""",
        reply_markup=_ui_editor.text_category_entries_keyboard(category, 0)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("uitc_pg:"))
async def uitc_page(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True); return
    _, category, page_raw = parts
    try:
        page = max(0, int(page_raw))
    except Exception:
        page = 0
    entries = _ui_editor.text_catalog_entries(category)
    await callback.message.edit_text(
        f"""✏️ {category}

متن موردنظر را انتخاب کنید ({len(entries)} متن):""",
        reply_markup=_ui_editor.text_category_entries_keyboard(category, page)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("uitc_key:"))
async def uitc_show_entry(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key = callback.data.split(":", 1)[1]
    # پیدا کردن دسته و default
    category = ""
    default_text = ""
    try:
        category, default_text = _ui_editor.text_catalog_find(key)
    except Exception:
        pass
    current = _ui_editor.get_text(key, default_text)
    entities = _ui_editor.get_entities(key)
    has_emoji = any(e.get("type") == "custom_emoji" for e in entities)
    emoji_note = " 💎 «دارای Premium Emoji»" if has_emoji else ""
    is_custom = bool(current and current != default_text)
    status = "✅ سفارشی‌سازی‌شده" if is_custom else "صفحه‌پیش‌فرض"
    if current:
        preview, _prev_ents = _ui_editor.compose_with_entities((current, entities), limit=1200)
    else:
        preview, _prev_ents = "— خالی —", []
    await state.update_data(uitc_key=key, uitc_category=category, uitc_default=default_text)
    await _edit_with_entities(
        callback,
        f"✏️ کلید: {key}\n{emoji_note}\nوضعیت: {status}\n\nمتن فعلی:\n",
        (preview, _prev_ents),
        f"\n\n{_ui_editor.variables_help(key, default_text)}",
        reply_markup=_ui_editor.text_entry_keyboard(key, category)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("uitc_edit:"))
async def uitc_start_edit(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key = callback.data.split(":", 1)[1]
    # پیدا کردن default
    default_text = ""
    category = ""
    try:
        category, default_text = _ui_editor.text_catalog_find(key)
    except Exception:
        pass
    current = _ui_editor.get_text(key, default_text)
    await state.update_data(ui_edit_kind="text", ui_text_key=key, uitc_category=category, uitc_default=default_text)
    await state.set_state(AdminStates.waiting_ui_editor_input)
    _cur_text, _cur_ents = _ui_editor.compose_with_entities((current, _ui_editor.get_entities(key)), limit=1000)
    await _edit_with_entities(
        callback,
        f"✏️ ویرایش متن\n\nکلید: {key}\n\nمتن فعلی:\n",
        (_cur_text, _cur_ents),
        f"""\n\n—
متن جدید را بفرستید.
💎 برای Premium Emoji همان ایموجی را در ابتدای پیام بفرستید.
🧾 برای جدول/تیتر (Rich Message): پیام ریچ را فوروارد کنید یا خط اول را «html:» بنویسید.

{_ui_editor.variables_help(key, default_text)}""",
        reply_markup=admin_back_button()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("uitc_reset:"))
async def uitc_reset_entry(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key = callback.data.split(":", 1)[1]
    try:
        # حذف override
        conn = db.get_connection()
        conn.execute("DELETE FROM ui_text_overrides WHERE key=?", (key,))
        conn.commit()
        import cache as _cache
        _cache.invalidate(f"uitext:{key}")
        _cache.invalidate(f"uientities:{key}")
        _cache.invalidate(f"uihas:{key}")
        try:
            __import__("rich_message").clear_text(str(key))
        except Exception:
            pass
        print(f"[UIEditor] Reset text override for: {key}")
    except Exception as e:
        print(f"[UIEditor] Error resetting {key}: {e}")
    # پیدا کردن default
    default_text = ""
    category = ""
    try:
        category, default_text = _ui_editor.text_catalog_find(key)
    except Exception:
        pass
    await callback.answer("✅ متن به پیش‌فرض بازگشت.")
    await callback.message.edit_text(
        f"""✅ بازگشت به پیش‌فرض

کلید: {key}

متن پیش‌فرض:
{default_text[:1000]}""",
        reply_markup=_ui_editor.text_entry_keyboard(key, category)
    )

@router.callback_query(F.data.startswith("ui_button:"))
async def ui_editor_edit_button(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, screen_key, callback_key = parts
    sc = _ui_editor.SCREENS.get(screen_key)
    if not sc or callback_key not in {cb for cb, _ in sc.get("buttons", [])}:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    default = _ui_editor._button_default(screen_key, callback_key)
    current = _ui_editor.get_button(screen_key, callback_key, default)
    meta = _ui_editor.get_button_meta(screen_key, callback_key)
    emoji_note = "\n💎 برای Premium Emoji، همان ایموجی را در ابتدای پیام بفرستید." if meta.get("custom_emoji_id") else ""
    await state.update_data(ui_edit_kind="button", ui_screen_key=screen_key, ui_callback_key=callback_key)
    await state.set_state(AdminStates.waiting_ui_editor_input)
    # آیکن پریمیومِ دکمه در متن ذخیره نمی‌شود؛ برای پیش‌نمایش، همان آیکن را ابتدای «متن فعلی» نشان بده.
    _icon_parts = []
    if meta.get("custom_emoji_id"):
        _icon_parts.append(("💎", [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": str(meta["custom_emoji_id"])}]))
        _icon_parts.append(" ")
    await _edit_with_entities(
        callback,
        f"🔘 ویرایش دکمه\n\nصفحه: {sc.get('label', screen_key)}\nمتن فعلی: ",
        *_icon_parts,
        f"{current}{emoji_note}\n\nمتن جدید دکمه را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_toggle:"))
async def ui_editor_toggle_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    _ui_editor.toggle_button(parts[1], parts[2])
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(parts[1]))
    await callback.answer("✅ وضعیت دکمه تغییر کرد.")


@router.callback_query(F.data.startswith("ui_style:"))
async def ui_editor_style_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    screen_key, callback_key = parts[1], parts[2]
    style = _ui_editor.next_button_style(_ui_editor.effective_style(screen_key, callback_key))
    _ui_editor.set_button_style(screen_key, callback_key, style)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(screen_key))
    await callback.answer("🎨 رنگ دکمه تغییر کرد.")


@router.callback_query(F.data.startswith("ui_reset:"))
async def ui_editor_reset_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    _ui_editor.reset_button(parts[1], parts[2])
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(parts[1]))
    await callback.answer("♻️ تنظیمات دکمه به پیش‌فرض برگشت.")

@router.callback_query(F.data.startswith("ui_move:"))
async def ui_editor_move_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 3)
    if len(parts) != 4 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    screen_key, callback_key, direction = parts[1], parts[2], parts[3]
    _ui_editor.move_button(screen_key, callback_key, direction)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(screen_key))
    await callback.answer("↕️ جای دکمه تغییر کرد.")


# ---------------------------------------------------------------------------
# ➕ دکمه‌های سفارشی ادمین (افزودن/حذف/جابه‌جایی دکمه‌ی دلخواه در هر صفحه)
#
# 🐛 فیکس بحرانی: کل این خانواده‌ی هندلرها (ui_cbtn_*) و همچنین
# ui_buttons_page: از فایل حذف شده بودند، در حالی که ui_editor.py همچنان این
# دکمه‌ها را می‌ساخت و permissions.py هم مجوزشان را تعریف کرده بود. نتیجه:
# «➕ دکمه‌های سفارشی این صفحه» و صفحه‌بندی فهرست دکمه‌ها کاملاً بی‌جواب
# بودند — بخش بزرگی از همان «ویرایش متن و دکمه‌ها کلاً باگه».
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("ui_buttons_page:"))
async def ui_editor_buttons_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except Exception:
        page = 0
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.all_buttons_keyboard(page))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_btnbundle:"))
async def ui_editor_button_bundle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    bundle_key = callback.data.split(":", 1)[1]
    if bundle_key != "all" and bundle_key not in _ui_editor.BUTTON_BUNDLES:
        await callback.answer("❌ بسته پیدا نشد.", show_alert=True)
        return
    label = "همه‌ی دکمه‌ها" if bundle_key == "all" else _ui_editor.BUTTON_BUNDLES[bundle_key]["label"]
    await callback.message.edit_text(
        f"🔘 {label}\n\nروی نام هر دکمه بزن تا متنش را تغییر بدهی. با چشم وضعیت نمایش، ب�� رنگ استایل، و با ↕️ وارد مدیریت همان صفحه می‌شوی.",
        reply_markup=_ui_editor.button_bundle_keyboard(bundle_key, 0),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_bundle_page:"))
async def ui_editor_button_bundle_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    bundle_key = parts[1]
    try:
        page = max(0, int(parts[2]))
    except Exception:
        page = 0
    if bundle_key != "all" and bundle_key not in _ui_editor.BUTTON_BUNDLES:
        await callback.answer("❌ بسته پیدا نشد.", show_alert=True)
        return
    label = "همه‌ی دکمه‌ها" if bundle_key == "all" else _ui_editor.BUTTON_BUNDLES[bundle_key]["label"]
    await callback.message.edit_text(
        f"🔘 {label}\n\nروی نام هر دکمه بزن تا متنش را تغییر بدهی. با چشم وضعیت نمایش، با رنگ استایل، و با ↕️ وارد مدیریت همان صفحه می‌شوی.",
        reply_markup=_ui_editor.button_bundle_keyboard(bundle_key, page),
    )
    await callback.answer()
@router.callback_query(F.data.startswith("ui_cbtn_list:"))
async def ui_cbtn_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    screen_key = callback.data.split(":", 1)[1]
    if screen_key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"➕ دکمه‌های سفارشی «{_ui_editor.SCREENS[screen_key]['label']}»\n\n"
        "این دکمه‌ها علاوه بر دکمه‌های اصلی ربات، پایین همین صفحه نشان داده می‌شوند.",
        reply_markup=_ui_editor.custom_buttons_keyboard(screen_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_move:"))
async def ui_cbtn_move(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    button_id, direction = int(parts[1]), parts[2]
    btn = _ui_editor.get_custom_button(button_id)
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    _ui_editor.move_custom_button(button_id, direction)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.custom_buttons_keyboard(btn["screen_key"]))
    await callback.answer("↕️ جابه‌جا شد.")


@router.callback_query(F.data.startswith("ui_cbtn_del:"))
async def ui_cbtn_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    button_id = int(callback.data.split(":", 1)[1])
    btn = _ui_editor.get_custom_button(button_id)
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    screen_key = btn["screen_key"]
    _ui_editor.delete_custom_button(button_id)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.custom_buttons_keyboard(screen_key))
    await callback.answer("🗑 دکمه حذف شد.")


@router.callback_query(F.data.startswith("ui_cbtn_open:"))
async def ui_cbtn_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    button_id = int(callback.data.split(":", 1)[1])
    btn = _ui_editor.get_custom_button(button_id)
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    kind = {"callback": "🔗 صفحه‌ی داخل ربات", "url": "🌐 لینک وب", "webapp": "📲 اپ‌لینک"}.get(btn["action_type"], btn["action_type"])
    await callback.answer(
        f"«{btn['text']}»\nنوع: {kind}\nمقصد: {btn['action_value']}\n\nبرای حذف از 🗑 کنارش استفاده کن.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("ui_cbtn_vis:"))
async def ui_cbtn_visibility(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    btn = _ui_editor.get_custom_button(int(callback.data.split(":", 1)[1]))
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    _ui_editor.toggle_button(btn["screen_key"], _ui_editor.custom_button_key(btn["id"]))
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.custom_buttons_keyboard(btn["screen_key"]))
    await callback.answer("✅ وضعیت دکمه تغییر کرد.")


@router.callback_query(F.data.startswith("ui_cbtn_col:"))
async def ui_cbtn_color(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    btn = _ui_editor.get_custom_button(int(callback.data.split(":", 1)[1]))
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    ck = _ui_editor.custom_button_key(btn["id"])
    _ui_editor.set_button_style(btn["screen_key"], ck, _ui_editor.next_button_style(_ui_editor.effective_style(btn["screen_key"], ck)))
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.custom_buttons_keyboard(btn["screen_key"]))
    await callback.answer("🎨 رنگ دکمه تغییر کرد.")


@router.callback_query(F.data.startswith("ui_cbtn_add:"))
async def ui_cbtn_add(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    screen_key = callback.data.split(":", 1)[1]
    if screen_key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    await state.update_data(cbtn_screen=screen_key)
    await callback.message.edit_text(
        "🎨 اول رنگ دکمه‌ی جدید را انتخاب کن:",
        reply_markup=_ui_editor.custom_button_style_keyboard(screen_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_style:"))
async def ui_cbtn_style(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    screen_key, style = parts[1], parts[2]
    await state.update_data(cbtn_screen=screen_key, cbtn_style=style)
    await callback.message.edit_text(
        "🔗 این دکمه کاربر را کجا ببرد؟",
        reply_markup=_ui_editor.custom_button_action_type_keyboard(screen_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_type:"))
async def ui_cbtn_type(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("�� دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    screen_key, action_type = parts[1], parts[2]
    await state.update_data(cbtn_screen=screen_key, cbtn_action_type=action_type)

    if action_type == "callback_quick":
        await callback.message.edit_text(
            "🔗 دکمه به کدام صفحه‌ی ربات وصل شود؟",
            reply_markup=_ui_editor.quick_destination_keyboard(screen_key),
        )
        await callback.answer()
        return

    prompts = {
        "callback_manual": _ui_editor.CALLBACK_HELP_TEXT,
        "url": "🌐 آدرس کامل لینک را بفرست (با https:// شروع شود):",
        "webapp": "📲 آدرس کامل Web App را بفرست (با https:// شروع شود):",
    }
    await state.set_state(AdminStates.waiting_ui_cbtn_value)
    await callback.message.answer(prompts.get(action_type, "مقدار مقصد را بفرست:"))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_dest:"))
async def ui_cbtn_dest(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    screen_key, dest = parts[1], parts[2]
    await state.update_data(cbtn_screen=screen_key, cbtn_action_type="callback", cbtn_action_value=dest)
    await state.set_state(AdminStates.waiting_ui_cbtn_text)
    await callback.message.answer("✏️ حالا متن روی دکمه را بفرست (می‌تونی ایموجی پرمیوم هم بذاری):")
    await callback.answer()


@router.message(AdminStates.waiting_ui_cbtn_value)
async def ui_cbtn_value_input(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action_type = data.get("cbtn_action_type")
    value = (message.text or "").strip()
    if action_type in ("url", "webapp") and not value.startswith("https://"):
        await message.answer("❌ آدرس باید با https:// شروع شود. دوباره بفرست:")
        return
    if not value:
        await message.answer("❌ مقدار خالی است. دوباره بفرست:")
        return
    stored_type = "callback" if action_type == "callback_manual" else action_type
    await state.update_data(cbtn_action_type=stored_type, cbtn_action_value=value)
    await state.set_state(AdminStates.waiting_ui_cbtn_text)
    await message.answer("✏️ حالا متن روی دکمه را بفرست (می‌تونی ایموجی پرمیوم هم بذاری):")


@router.message(AdminStates.waiting_ui_cbtn_text)
async def ui_cbtn_text_input(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    screen_key = data.get("cbtn_screen")
    text = (message.text or "").strip()
    if not screen_key or screen_key not in _ui_editor.SCREENS:
        await state.clear()
        await message.answer("❌ این مسیر منقضی ��ده؛ دوباره از ویرایشگر شروع کن.")
        return
    if not text:
        await message.answer("❌ متن دکمه خالی است. دوباره بفرست:")
        return
    custom_emoji_id = None
    for entity in _ui_editor_entities(message):
        if entity.get("type") == "custom_emoji" and entity.get("custom_emoji_id"):
            custom_emoji_id = str(entity["custom_emoji_id"])
            break
    _ui_editor.add_custom_button(
        screen_key, text,
        data.get("cbtn_style", "primary"),
        data.get("cbtn_action_type", "callback"),
        data.get("cbtn_action_value", "back"),
        custom_emoji_id=custom_emoji_id,
    )
    await state.clear()
    await message.answer(
        f"✅ دکمه‌ی «{text}» به صفحه‌ی «{_ui_editor.SCREENS[screen_key]['label']}» اضافه شد.",
        reply_markup=_ui_editor.custom_buttons_keyboard(screen_key),
    )


@router.callback_query(F.data.startswith("ui_rowlayout:"))
async def ui_editor_rowlayout_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key=callback.data.split(":",1)[1]
    if key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True); return
    await state.update_data(ui_rowlayout_key=key)
    await state.set_state(AdminStates.waiting_ui_row_layout)
    await callback.message.answer("📐 الگوی تعداد دکمه‌ها در هر ردیف را با کاما بفرست. مثال: 1,3,2\nیعنی ردیف اول ۱ دکمه، ردیف دوم ۳ دکمه و ردیف سوم ۲ دکمه.")
    await callback.answer()

@router.message(AdminStates.waiting_ui_row_layout)
async def ui_editor_rowlayout_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    raw=(message.text or '').replace('��',',').strip()
    try:
        widths=[int(x.strip()) for x in raw.split(',') if x.strip()]
        if not widths or any(x<1 or x>8 for x in widths): raise ValueError
    except Exception:
        await message.answer("❌ فرمت نامعتبر است. مثال درست: 1,3,2"); return
    data=await state.get_data(); key=data.get('ui_rowlayout_key')
    if key not in _ui_editor.SCREENS:
        await state.clear(); await message.answer("❌ صفحه پیدا نشد."); return
    _ui_editor.set_row_layout(key,widths); await state.clear()
    await message.answer(f"✅ الگوی ردیف‌ها ذخیره شد: {', '.join(map(str,widths))}", reply_markup=_ui_editor.button_screen_keyboard(key))


@router.callback_query(F.data.startswith("ui_layout:"))
async def ui_editor_layout(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    screen_key, action, value = parts[1], parts[2], parts[3]
    if action == "set":
        cols = max(1, min(4, int(value)))
    elif action == "cycle":
        cols = max(1, min(4, int(value)))
    else:
        await callback.answer("❌ حالت چیدمان نامعتبر است.", show_alert=True)
        return
    _ui_editor.set_layout(screen_key, "vertical" if cols == 1 else "inline", cols)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(screen_key))
    await callback.answer("📐 چیدمان ذخیره شد.")


@router.message(AdminStates.waiting_ui_editor_input)
async def ui_editor_save_input(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    kind = data.get("ui_edit_kind")
    # 🧾 ریچ‌مسیج (جدول/تیتر/لیست) برای متن‌ها: پیام ریچ فوروارد کن یا خط اول «html:» / «md:»
    if data.get("ui_edit_kind") == "text":
        import rich_message as _rich
        _rp = _rich.extract(message)
        if _rp:
            _rkey = data.get("ui_text_key")
            try:
                await _rich.send(message.bot, message.chat.id, _rich.substitute(_rp, None))  # پیش‌نمایش/اعتبارسنجی
            except Exception as exc:
                await message.answer(f"❌ این Rich Message پذیرفته نشد و ذخیره نشد:\n{exc}\n\nدوباره بفرست:", parse_mode=None)
                return
            _rich.save_text(str(_rkey), _rp)
            await state.clear()
            _mk = _ui_editor.text_screen_keyboard(_rkey) if _rkey in _ui_editor.SCREENS else _ui_editor.text_categories_keyboard()
            await message.answer("✅ متن (Rich Message) ذخیره شد. متغیرها مثل {name} داخل جدول/متن جایگزین می‌شوند.", reply_markup=_mk)
            return
    value, _entities = message_text_and_entities(message)
    if not value:
        await message.answer("❌ مقدار نمی‌تواند خالی باشد. دوباره ارسال کنید:")
        return
    if len(value) > 3800:
        await message.answer("❌ متن خیلی طولانی است. لطفاً متن کوتاه‌تری ارسال کنید:")
        return
    try:
        if kind == "text":
            key = data.get("ui_text_key")
            is_catalog_key = False
            try:
                is_catalog_key = bool(_ui_editor.text_catalog_find(str(key))[1])
            except Exception:
                is_catalog_key = False
            _ui_editor._discover_auto_messages()
            is_auto_key = str(key).startswith("auto_") and str(key) in _ui_editor._AUTO_REGISTRY
            if key not in _ui_editor.SCREENS and not is_catalog_key and not is_auto_key:
                raise ValueError("invalid text key")
            _ui_editor.set_text(key, value, _entities)
            try:
                __import__("rich_message").clear_text(str(key))  # ذخیره‌ی متن ساده، ریچ قبلی را بی‌اثر می‌کند
            except Exception:
                pass
            await state.clear()
            if str(key).startswith("auto_"):
                item = None
                source = (item or {}).get("source", "")
                markup = _ui_editor.text_categories_keyboard() if source else _ui_editor.editor_mode_keyboard()
                await message.answer("✅ متن ذخیره شد و Entityهای آن هم حفظ شدند.", reply_markup=markup)
            else:
                await message.answer("✅ متن ذخیره شد و Entityهای آن هم حفظ شدند.", reply_markup=_ui_editor.text_screen_keyboard(key))
            return
        if kind == "button":
            screen_key = data.get("ui_screen_key")
            callback_key = data.get("ui_callback_key")
            if screen_key not in _ui_editor.SCREENS:
                raise ValueError("invalid screen")
            defaults = dict(_ui_editor.SCREENS[screen_key].get("buttons") or [])
            if callback_key not in defaults:
                raise ValueError("invalid button")
            custom_emoji_id = None
            for entity in _entities:
                if entity.get("type") == "custom_emoji" and entity.get("custom_emoji_id"):
                    custom_emoji_id = str(entity["custom_emoji_id"])
                    # ایموجی پریمیوم روی دکمه به‌صورت آیکن (icon_custom_emoji_id) نمایش داده می‌شود؛ پس
                    # ایموجی عادیِ پشتش را از متن دکمه برمی‌داریم تا دو بار نمایش داده نشود.
                    try:
                        _b = value.encode("utf-16-le")
                        _start, _end = int(entity["offset"]) * 2, (int(entity["offset"]) + int(entity["length"])) * 2
                        _without = (_b[:_start] + _b[_end:]).decode("utf-16-le").strip()
                        if _without:
                            value = _without
                    except Exception:
                        pass
                    break
            _ui_editor.set_button(screen_key, callback_key, value, custom_emoji_id=custom_emoji_id)
            await state.clear()
            await message.answer("✅ متن دکمه ذخیره شد." + (" 💎 Premium Emoji هم ذخیره شد." if custom_emoji_id else ""), reply_markup=_ui_editor.button_screen_keyboard(screen_key))
            return
        raise ValueError("unknown editor state")
    except Exception:
        logger.exception("UI editor save failed")
        await state.clear()
        await message.answer("❌ ذخیره انجام نشد. دوباره از منوی ویرایش متن و دکمه‌ها وارد شوید.")


@router.callback_query(F.data.startswith("textcat_"))
async def admin_text_category(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    try:
        index = int(callback.data.replace("textcat_", "", 1))
        category = list(TEXT_CATEGORIES.keys())[index]
    except Exception:
        await callback.answer("❌ بخش متن پیدا نشد.", show_alert=True); return
    await state.clear()
    note = "\n\n🔒 متن انقضای فاکتور کارت‌به‌کارت سیستمی است و از اینجا قابل تغییر نیست." if "فاکتور کارت‌به‌کارت" in category else ""
    await callback.message.edit_text(
        f"📝 {category}\n\nمتن موردنظر را برای ویرایش انتخاب کنید:{note}",
        reply_markup=_text_manager_keyboard(category),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("textedit_"))
async def admin_text_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key = callback.data.replace("textedit_", "", 1)
    if key not in TEXT_KEYS:
        await callback.answer("❌ متن پیدا نشد.", show_alert=True); return
    await state.update_data(text_override_key=key)
    await state.set_state(AdminStates.waiting_text_override_value)
    current = user_text(key, TEXT_KEYS[key])
    placeholder_note = "\n\nمتغیرهای قابل استفاده: " + ", ".join("{" + x + "}" for x in _template_vars(TEXT_KEYS[key])) if _template_vars(TEXT_KEYS[key]) else ""
    lock_note = "\n\n🔒 توجه: جمله‌ی زمان انقضای فاکتور در کد سیستمی تولید می‌شود و جزو این متن نیست." if key.startswith("invoice_") else ""
    await callback.message.edit_text(
        f"✏️ ویرایش متن: {key}\n\nمتن فعلی:\n{current}{placeholder_note}{lock_note}\n\nمتن جدید را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


def _template_vars(template: str) -> list[str]:
    import string
    vars_found = []
    for _, field, _, _ in string.Formatter().parse(template):
        if field and field not in vars_found:
            vars_found.append(field.split("!", 1)[0].split(":", 1)[0])
    return vars_found


@router.message(AdminStates.waiting_text_override_value)
async def admin_text_edit_save(message: types.Message, state: FSMContext):
    data = await state.get_data(); key = data.get("text_override_key")
    if key not in TEXT_KEYS:
        await state.clear(); await message.answer("❌ عملیات منقضی شد."); return
    value, entities = message_text_and_entities(message)
    if not value:
        await message.answer("❌ متن نمی‌تواند خالی باشد:"); return
    # 🆕 فیکس: اگر این متن از سقف مجاز تلگرام برای متن پیام (۴۰۹۶ کاراکتر) بلندتر ذخیره شود، بعداً هر‌بار که این متن (مثلاً برای فاکتور، پیام راهنما و غیره) برای کاربر فرستاده شود، تلگرام خطای «MESSAGE_TOO_LONG» برمی‌گرداند. اینجا قبل از ذخیره‌شدن گرفته می‌شود (علاوه بر محافظتی که در show_menu_with_sticker اضافه شد).
    if len(value) > 3800:
        await message.answer(
            f"❌ این متن خیلی طولانی است ({len(value)} کاراکتر) و ممکن است تلگرام آن را رد کند (سقف تلگرام: ۴۰۹۶ کاراکتر). لطفاً متن کوتاه‌تری بفرست:"
        )
        return
    # جلوگیری از خراب‌شدن فاکتور با حذف متغیرهای سیستمی
    required = set(_template_vars(TEXT_KEYS[key]))
    if required:
        supplied = set(_template_vars(value))
        missing = required - supplied
        if missing:
            await message.answer("❌ این متغیرهای ضروری حذف شده‌اند: " + ", ".join("{" + x + "}" for x in sorted(missing)))
            return
    custom_emoji_id = None
    for entity in entities:
        if entity.get("type") == "custom_emoji" and entity.get("custom_emoji_id"):
            custom_emoji_id = str(entity["custom_emoji_id"])
            break
    db.set_text_override(key, value, entities=entities)
    db.set_button_custom_emoji_id(key, custom_emoji_id)
    refresh_user_text(key)
    await state.clear()
    category = CATEGORY_BY_KEY.get(key)
    await message.answer("✅ متن ذخیره شد.", reply_markup=_text_manager_keyboard(category))


# 📥 صف سفارشات — لیست خریدهای تأییدشده‌ای که هنوز کانفیگ‌��ان ارسال نشده،
# چه خرید پلن معمولی (VIP/گیمینگ) و چه سفارش سفارشی/تمدید.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_orders_off")
async def admin_orders_off(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    db.set_orders_enabled(False)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await callback.message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await callback.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_closed", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.") + "\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🔴 بخش سفارشات خاموش شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_orders_on")
async def admin_orders_on(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    db.set_orders_enabled(True)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await callback.message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await callback.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_opened", "🟢 ربات مجدداً فعال شد!") + "\n\nبا زدن /start می‌توانید دوباره سفارش ثبت کنید.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🟢 بخش سفارشات روشن شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ن��موفق.")
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(callback.from_user.id))
    await callback.answer()


async def _edit_or_resend(callback: types.CallbackQuery, text: str, reply_markup=None):
    """پیام فعلی را ویرایش می‌کند؛ اگر پیام عکس‌دار بود (edit_text ممکن نیست) آن را حذف و پیام تازه می‌فرستد."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(text, reply_markup=reply_markup)


async def _render_order_queue(callback: types.CallbackQuery):
    pending = db.get_pending_orders(limit=25)
    for o in pending:
        u = db.get_user_by_id(o["user_id"])
        o["telegram_id"] = u["telegram_id"] if u else ""
    custom_paid = db.get_paid_custom_orders(limit=25)

    total = len(pending) + len(custom_paid)
    if not total:
        text = "📦 سفارش‌های در انتظار\n\n✅ در حال حاضر هیچ سفارش در انتظار ارسالی وجود ندارد."
    else:
        text = f"📦 سفارش‌های در انتظار — {total} مورد در انتظار ارسال\n\nروی هرکدوم بزن تا جزئیات سفارش را ببینی و ارسال دستی یا ارسال از پنل را انتخاب کنی 👇"

    await _edit_or_resend(callback, text, admin_order_queue_keyboard(pending, custom_paid))


@router.callback_query(F.data == "admin_request_queue")
async def admin_request_queue(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_count = len(db.get_pending_orders(limit=200)) + len(db.get_paid_custom_orders(limit=200))
    receipt_count = len(db.get_pending_receipts(limit=200)) + len(db.get_pending_custom_order_receipts(limit=200))
    await _edit_or_resend(
        callback,
        "📥 صف درخواست‌ها\n\nچه چیزی رو می‌خوای بررسی کنی؟ 👇",
        admin_request_queue_menu(order_count, receipt_count),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_order_queue")
async def admin_order_queue(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_order_queue(callback)
    await callback.answer()


# ---------------------------------------------------------------------------
# 🧾 رسیدهای در انتظار تایید — همه‌ی رسیدهای شارژ کیف پول و خرید کارت‌به‌کارت
# (پلن ثابت + بساز سرویس خودت) که هنوز ادمین تایید/رد نکرده، در یک لیست.
# تایید/رد از همینجا دقیقاً همان مسیر همیشگی (پیام فوروارد‌شده در چت ادمین)
# را صدا می‌زند، فقط یک راه میان‌بر برای دیدن همه‌چیز یک‌جاست.
# ---------------------------------------------------------------------------
async def _render_pending_receipts(callback: types.CallbackQuery):
    receipts = db.get_pending_receipts(limit=25)
    custom_receipts = db.get_pending_custom_order_receipts(limit=25)

    total = len(receipts) + len(custom_receipts)
    if total == 0:
        text = "🧾 رسیدهای در انتظار تایید\n\n✅ در حال حاضر هیچ رسید در انتظار تاییدی وجود ندارد."
    else:
        text = f"🧾 رسیدهای در انتظار تایید — {total} مورد\n\nروی هر رسید بزن تا خودِ رسید (عکس/متن) را ببینی و آنجا تأیید یا رد کنی 👇"

    await _edit_or_resend(callback, text, admin_pending_receipts_keyboard(receipts, custom_receipts))


@router.callback_query(F.data == "admin_pending_receipts")
async def admin_pending_receipts(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_pending_receipts(callback)
    await callback.answer()


@router.callback_query(F.data == "clearreceipts_confirm")
async def clear_receipts_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ مطمئنی می‌خوای همه‌ی رسیدهای این لیست رو بررسی‌شده علامت بزنی؟\n"
        "(توجه: این کار فقط لیست رو خالی می‌کنه؛ اگر هنوز به کاربری تایید/رد اعلام نکردی، "
        "پیام اصلی رسیدش همچنان توی چتت هست و باید از همونجا اقدام کنی.)",
        reply_markup=admin_clear_receipts_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clearreceipts_do")
async def clear_receipts_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    db.dismiss_all_pending_receipts()
    await _render_pending_receipts(callback)
    await callback.answer("🧹 لیست خالی شد.")


@router.callback_query(F.data.startswith("dismissorder_"))
async def dismiss_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("dismissorder_", ""))
    db.set_order_status(order_id, "dismissed")
    await _render_order_queue(callback)
    await callback.answer("🗑 از صف پاک شد.")




@router.callback_query(F.data == "clearorders_confirm")
async def clear_orders_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ مطمئنی می‌خوای همه‌ی سفارش‌های این صف رو پاک کنی؟\n"
        "(این کار فقط سفارش‌ها رو از صف حذف می‌کنه؛ اگه کانفیگ کسی رو نفرستادی، دیگه اینجا یادآوریش نمی‌مونه.)",
        reply_markup=admin_clear_orders_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clearorders_do")
async def clear_orders_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    pending = db.get_pending_orders(limit=1000)
    for o in pending:
        db.set_order_status(o["id"], "dismissed")
    custom_paid = db.get_paid_custom_orders(limit=1000)
    for co in custom_paid:
        db.set_custom_order_status(co["id"], "dismissed")
    await _render_order_queue(callback)
    await callback.answer(f"🧹 {len(pending) + len(custom_paid)} سفارش پاک شد.")


# ---------------------------------------------------------------------------
# 🧾 مشاهده‌ی یک رسید از «صف درخواست‌ها» — خودِ رسید (عکس/متن) + دکمه‌ی تأیید/رد
# ---------------------------------------------------------------------------
_RECEIPT_KIND_LABELS = {"charge": "شارژ کیف پول", "plan_card": "خرید کارت‌به‌کارت", "plan_crypto": "پرداخت ارزی"}


def _receipt_view_caption(r: dict) -> str:
    if r.get("details"):
        return str(r["details"])
    u = db.get_user(r["telegram_id"])
    name = (u or {}).get("name") or "-"
    text = (
        f"🧾 رسید {_RECEIPT_KIND_LABELS.get(r['kind'], r['kind'])}\n\n"
        f"👤 {name}\n🆔 {r['telegram_id']}\n"
        f"📦 {r['label']}\n💰 {int(r['amount']):,} تومان\n"
        f"🧾 کد پیگیری: {r['id']}\n🕒 {r.get('created_at') or '-'}"
    )
    if r.get("note"):
        text += f"\n📝 {r['note']}"
    return text


@router.callback_query(F.data.startswith("rcptview_"))
async def admin_view_receipt(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        rid = int(callback.data.replace("rcptview_", ""))
    except ValueError:
        await callback.answer("❌ نامعتبر.", show_alert=True)
        return
    r = db.get_pending_receipt_by_id(rid)
    if r is None or r.get("status") != "pending":
        await callback.answer("⚠️ این رسید قبلاً بررسی شده یا پیدا نشد.", show_alert=True)
        await _render_pending_receipts(callback)
        return
    uid = str(r["telegram_id"])
    if r["kind"] == "charge":
        kb = admin_charge_approval_keyboard(uid, int(r["amount"]), rid)
    else:
        plan_key = r.get("plan_key") or r.get("extra") or ""
        kb = admin_purchase_card_approval_keyboard(uid, plan_key, int(r["amount"]), rid)
    kb = with_back_row(kb, "🔙 بازگشت به لیست رسیدها", "admin_pending_receipts")
    caption = _receipt_view_caption(r)
    await callback.answer()
    chat_id = callback.message.chat.id
    try:
        if r.get("file_id") and r.get("file_type") == "document":
            await callback.bot.send_document(chat_id, r["file_id"], caption=caption[:1000], reply_markup=kb)
        elif r.get("file_id"):
            await callback.bot.send_photo(chat_id, r["file_id"], caption=caption[:1000], reply_markup=kb)
        else:
            extra = "" if r.get("note") else "\n\n⚠️ تصویری برای این رسید ذخیره نشده (رسید قدیمی)؛ پیام اصلی آن را در چت خود پیدا کنید."
            await callback.bot.send_message(chat_id, caption[:3900] + extra, reply_markup=kb)
    except Exception:
        logger.exception("نمایش رسید %s ناموفق بود", rid)
        await callback.bot.send_message(chat_id, caption[:3900], reply_markup=kb)


@router.callback_query(F.data.startswith("rcptviewc_"))
async def admin_view_custom_receipt(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        oid = int(callback.data.replace("rcptviewc_", ""))
    except ValueError:
        await callback.answer("❌ نامعتبر.", show_alert=True)
        return
    order = db.get_custom_order(oid)
    if order is None or order.get("status") != "pending":
        await callback.answer("⚠️ این رسید قبلاً بررسی شده یا پیدا نشد.", show_alert=True)
        await _render_pending_receipts(callback)
        return
    u = db.get_user_by_id(order["user_id"]) or {}
    label = "تمدید سرویس" if order.get("order_type") == "renew" else "سرویس سفارشی جدید"
    caption = (
        f"💳 رسید {label}\n\n"
        f"👤 {u.get('name') or '-'}\n🆔 {u.get('telegram_id') or '-'}\n"
        f"📦 حجم: {order['volume_gb']} گیگ\n⏳ مدت: {order['days']} روز\n"
        + (f"🔤 نام: {order['custom_name']}\n" if order.get("custom_name") else "")
        + f"💰 {int(order['price']):,} تومان\n🔢 شماره سفارش: {oid}"
    )
    kb = with_back_row(admin_custom_order_card_approval_keyboard(oid), "🔙 بازگشت به لیست رسیدها", "admin_pending_receipts")
    await callback.answer()
    chat_id = callback.message.chat.id
    try:
        if order.get("receipt_file_id"):
            await callback.bot.send_photo(chat_id, order["receipt_file_id"], caption=caption[:1000], reply_markup=kb)
            return
    except Exception:
        logger.exception("نمایش عکس رسید سفارش سفارشی %s ناموفق بود", oid)
    extra = "\n\n⚠️ تصویری برای این رسید ذخیره نشده؛ پیام اصلی آن را در چت خود پیدا کنید."
    await callback.bot.send_message(chat_id, caption + extra, reply_markup=kb)


# ---------------------------------------------------------------------------
# 📦 جزئیات یک سفارش در «صف سفارش‌های در انتظار» + ارسال دستی / ارسال از پنل
# ---------------------------------------------------------------------------
def _order_type_label(order_type: str) -> str:
    return {"vip": "خرید سرویس VIP", "test": "تست رایگان", "renew": "تمدید", "payg": "Pay-as-you-go"}.get(str(order_type or ""), str(order_type or "-"))


@router.callback_query(F.data.startswith("qordview_"))
async def admin_view_queue_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        oid = int(callback.data.replace("qordview_", ""))
    except ValueError:
        await callback.answer("❌ نامعتبر.", show_alert=True)
        return
    order = db.get_order(oid)
    if order is None or (order.get("status") or "pending") != "pending":
        await callback.answer("⚠️ این سفارش دیگر در صف نیست.", show_alert=True)
        await _render_order_queue(callback)
        return
    u = db.get_user_by_id(order["user_id"]) or {}
    tid = str(u.get("telegram_id") or "")
    try:
        _chat = await callback.bot.get_chat(int(tid))
        uname = getattr(_chat, "username", None)
    except Exception:
        uname = None
    who = f"{u.get('name') or '-'}" + (f" (@{uname})" if uname else "")
    plan_key = order.get("plan_key")
    plan = db.get_effective_plan(plan_key) if plan_key else None
    pkg = ""
    if plan:
        pkg = f"\n📊 حجم/مدت: {_format_volume_gb_label(plan.get('volume_gb'))} / {plan.get('days') or 'نامحدود'} روز"
    text = (
        f"📦 سفارش #{oid} — منتظر ارسال سرویس\n\n"
        f"👤 {who}\n🆔 ایدی عددی: {tid}\n"
        f"🏷 نوع: {_order_type_label(order.get('order_type'))}\n"
        f"📌 پلن: {order.get('plan_name') or '-'}{pkg}\n"
        f"💰 مبلغ: {int(order.get('price') or 0):,} تومان\n"
        f"💳 روش پرداخت: {order.get('payment_method') or '-'}\n"
        f"🕒 ثبت: {order.get('created_at') or '-'}\n\n"
        "یکی از روش‌های تحویل را انتخاب کن 👇"
    )
    kb = admin_purchase_notify_keyboard(tid, plan_key, oid)
    rows = [list(r) for r in kb.inline_keyboard]
    if plan_key and not any("panelsend|" in (b.callback_data or "") for r in rows for b in r):
        try:
            if db.list_vpn_panels(enabled_only=True):
                # پنلی برای این پلن نگاشت نشده؛ دکمه نمایش داده می‌شود تا ادمین علت را ببیند.
                rows.append([InlineKeyboardButton(text="📤 ارسال از پنل", callback_data=f"panelsend|{tid}|{plan_key}|{oid}", style="primary")])
        except Exception:
            pass
    rows.append([InlineKeyboardButton(text="👤 مدیریت کاربر", callback_data=f"useropen_{tid}", style="primary")])
    rows.append([
        InlineKeyboardButton(text="🗑 حذف از صف", callback_data=f"dismissorder_{oid}", style="danger"),
        InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_order_queue", style="primary"),
    ])
    await callback.answer()
    await _edit_or_resend(callback, text, types.InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("qcustview_"))
async def admin_view_queue_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        oid = int(callback.data.replace("qcustview_", ""))
    except ValueError:
        await callback.answer("❌ نامعتبر.", show_alert=True)
        return
    order = db.get_custom_order(oid)
    if order is None or order.get("status") != "paid":
        await callback.answer("⚠️ این سفارش دیگر در صف نیست.", show_alert=True)
        await _render_order_queue(callback)
        return
    u = db.get_user_by_id(order["user_id"]) or {}
    tid = str(u.get("telegram_id") or "")
    is_renew = order.get("order_type") == "renew"
    text = (
        f"🛠 سفارش {'تمدید' if is_renew else 'سرویس سفارشی'} #{oid} — منتظر ارسال\n\n"
        f"👤 {u.get('name') or '-'}\n🆔 ایدی عددی: {tid}\n"
        f"📦 حجم: {order['volume_gb']} گیگ\n⏳ مدت: {order['days']} روز\n"
        + (f"🔤 نام: {order['custom_name']}\n" if order.get("custom_name") else "")
        + f"💰 مبلغ: {int(order['price']):,} تومان\n"
        f"💳 روش پرداخت: {order.get('payment_method') or '-'}\n"
        f"🕒 ثبت: {order.get('created_at') or '-'}\n\n"
        "یکی از روش‌های تحویل را انتخاب کن 👇"
    )
    rows = [list(r) for r in admin_custom_order_notify_keyboard(oid).inline_keyboard]
    rows.append([InlineKeyboardButton(text="👤 مدیریت کاربر", callback_data=f"useropen_{tid}", style="primary")])
    rows.append([
        InlineKeyboardButton(text="🗑 حذف از صف", callback_data=f"dismisscustomorder_{oid}", style="danger"),
        InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_order_queue", style="primary"),
    ])
    await callback.answer()
    await _edit_or_resend(callback, text, types.InlineKeyboardMarkup(inline_keyboard=rows))


# ---------------------------------------------------------------------------
# 📦 مشاهده و مدیریت سرویس‌های یک کاربر (لیست/حذف نرم/ادیت لینک ساب و کیوآرکد/
# مدیریت فایل‌های گیمینگ) — همه از طریق «🔍 جستجوی حرفه‌ای»
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("useractions_"))
async def admin_user_actions_back(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("useractions_", "")
    await _reply_with_user_actions(
        callback.message, f"👤 مدیریت کاربر {uid}", uid, db.is_user_blocked(uid), edit=True
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ✉️ پیام خصوصی ادمین به یک کاربر خاص (متن/عکس/فیلم/فوروارد)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("pm_"))
async def admin_pm_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("pm_", "")
    await state.set_state(AdminStates.waiting_pm_message)
    await state.update_data(pm_target_uid=uid)
    await callback.message.edit_text(
        f"✉️ پیامی که می‌خواهید به کاربر {uid} ارسال شود را بفرستید (متن، عکس، فیلم یا فوروارد هم پذیرفه است):",
        reply_markup=admin_pm_cancel_keyboard(uid),
    )
    await callback.answer()


@router.message(AdminStates.waiting_pm_message)
async def admin_pm_send(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("pm_target_uid")
    if not uid:
        await state.clear()
        return
    try:
        await send_notification_sticker(message.bot, int(uid), "notif_personal_message")
        await message.bot.copy_message(int(uid), message.chat.id, message.message_id)
        await _reply_with_user_actions(message, "✅ پیام خصوصی برای کاربر ارسال شد.", uid, db.is_user_blocked(uid), edit=False)
    except Exception:
        await _reply_with_user_actions(message, "❌ ارسال پیام ناموفق بود (ممکن است کاربر ربات را مسدود کرده باشد).", uid, db.is_user_blocked(uid), edit=False)
    await state.clear()


# ---------------------------------------------------------------------------
# 🚫 مسدود/رفع مسدودیت کاربر — کاربر‌های مسدود نمی‌توانند از ربات استفاده کنند
# (بررسی در middleware/start.py)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ✏️ ویرایش بی‌صدای موجودی کیف‌پول کاربر (بند ۶)
# تفاوتش با «💰 شارژ دستی»: اینجا هیچ پیام/نوتیفیکیشنی برای کاربر ارسال
# نمی‌شود و ادمین می‌تواند موجودی را به هر مقداری (کمتر یا بیشتر) تنظیم کند،
# نه فقط اضافه کند. تراکنش داخلی برای ردگیری حسابداری همچنان ثبت می‌شود.
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("walletedit_"))
async def admin_wallet_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("walletedit_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    await state.update_data(wallet_edit_uid=uid)
    await state.set_state(AdminStates.waiting_wallet_edit_amount)
    await callback.message.answer(
        f"✏️ ویرایش موجودی ک����ف‌پول «{user['name']}»\n\n"
        f"موجودی فعلی: {user['wallet']:,} تومان\n\n"
        "مقدار جدید را به تومان بفرست (موجودی دقیقاً روی همین عدد تنظیم می‌شود).\n"
        "برای کم/زیاد کردن نسبی می‌تونی از + یا - استفاده کنی، مثلاً: +50000 یا -20000\n\n"
        "⚠️ هیچ پیامی برای کاربر ارسال نخواهد شد."
    )
    await callback.answer()


@router.message(AdminStates.waiting_wallet_edit_amount)
async def admin_wallet_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("wallet_edit_uid")
    user = db.get_user(uid) if uid else None
    if user is None:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از لیست کاربران شروع کن.")
        return

    raw = (message.text or "").replace(",", "").replace("٬", "").strip()
    try:
        if raw.startswith(("+", "-")):
            delta = int(raw)
            new_balance = user["wallet"] + delta
        else:
            new_balance = int(raw)
        if new_balance < 0:
            await message.answer("❌ موجودی نمی‌تواند منفی شود. عدد دیگری بفرست:")
            return
    except Exception:
        await message.answer("❌ فقط عدد بفرست (یا +عدد / -عدد برای تغییر نسبی):")
        return

    old_balance = user["wallet"]
    diff = new_balance - old_balance
    db.set_user_wallet(user["id"], new_balance, f"ویرایش دستی توسط ادمین ({diff:+,} تومان)")
    await state.clear()
    await message.answer(
        f"✅ موجودی «{user['name']}» از {old_balance:,} به {new_balance:,} تومان تغییر کرد.\n"
        "🤫 هیچ پیامی برای کاربر ارسال نشد.",
        reply_markup=admin_user_actions_keyboard(uid, is_blocked=bool(user.get("is_blocked"))),
    )
    await _notify_main_admin_action(message.bot, message.from_user, "ویرایش بی‌صدای کیف‌پول", str(uid), f"{old_balance:,} → {new_balance:,}")


@router.callback_query(F.data.startswith("toggleblock_"))
async def admin_toggle_block(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("toggleblock_", "")
    currently_blocked = db.is_user_blocked(uid)
    db.set_user_blocked(uid, not currently_blocked)

    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"💛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛍 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}\n\n"
        + ("🚫 وضعیت: مسدود" if not currently_blocked else "✅ وضعیت: رفع مسدودیت شد")
    )
    await _reply_with_user_actions(callback.message, text, user["telegram_id"], not currently_blocked, edit=True)
    await callback.answer("🚫 کاربر مسدود شد." if not currently_blocked else "✅ مسدودیت برداشته شد.")


@router.callback_query(F.data.startswith("deleteuser_"))
async def admin_delete_user_ask(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "users"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("deleteuser_", "", 1)
    if uid == str(ADMIN_ID) or db.is_sub_admin(uid):
        await callback.answer("⛔ حساب ادمین قابل حذف نیست.", show_alert=True)
        return
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚠️ حذف کامل کاربر\n\n"
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        "این عملیات فقط اطلاعات کاربر را از دیتابیس ربات حذف می‌کند.\n"
        "هیچ درخواستی به پنل Marzban یا PasarGuard ارسال نمی‌شود.\n\n"
        "❗️این عملیات قابل بازگشت نیست.",
        reply_markup=_admin_user_delete_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("deleteuserconfirm_"))
async def admin_delete_user_confirm(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "users"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("deleteuserconfirm_", "", 1)
    if uid == str(ADMIN_ID) or db.is_sub_admin(uid):
        await callback.answer("⛔ حساب ادمین قابل حذف نیست.", show_alert=True)
        return
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر قبلاً حذف شده یا یافت نشد.", show_alert=True)
        return

    try:
        deleted = db.delete_user_from_bot(uid)
    except Exception:
        logger.exception("خطا در حذف کامل کاربر از دیتابیس ربات: %s", uid)
        await callback.answer("❌ حذف کاربر انجام نشد؛ اطلاعات ربات تغییر نکرد.", show_alert=True)
        return

    if not deleted:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        f"✅ کاربر «{user['name']}» با شناسه {uid} فقط از دیتابیس ربات حذف شد.\n\n"
        "🔒 هیچ تغییری در Marzban یا PasarGuard انجام نشد.",
        reply_markup=admin_back_button(),
    )
    await callback.answer("🗑 کاربر از ربات حذف شد.", show_alert=True)
    await _notify_main_admin_action(
        callback.bot,
        callback.from_user,
        "حذف کامل کاربر از ربات",
        uid,
        f"name={user['name']}",
    )


@router.callback_query(F.data.startswith("svcs_"))
async def admin_view_user_services(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("svcs_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    configs = db.get_configs(user["id"], include_deleted=True)
    await enrich_configs_with_subscription_names(configs)
    if not configs:
        await callback.message.edit_text(
            f"📦 کاربر {uid} هنوز هیچ سرویسی نداره.",
            reply_markup=admin_back_button(),
        )
    else:
        await callback.message.edit_text(
            f"📦 سرویس‌های کاربر {uid}\n\n❌ یعنی توسط خودِ کاربر حذف شده (ولی برای شما همچنان قابل‌مشاهده‌ست).\n\nروی هرکدوم بزن برای جزئیات و مدیریت 👇",
            reply_markup=admin_services_list_keyboard(configs, uid),
        )
    await callback.answer()


def _remaining_days_from_date_str(date_str) -> int | None:
    """تعداد روز باقی‌مانده تا انقضا را از یک رشته تاریخ به فرمت YYYY-MM-DD محاسبه می‌کند."""
    if not date_str:
        return None
    try:
        exp_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        delta = exp_date - now_tehran_naive()
        return delta.days
    except Exception:
        return None


async def _service_detail_text(cfg: dict) -> str:
    """
    توجه: قبلاً این متن با parse_mode="Markdown" (نسخه‌ی قدیمی مارک‌داون
    تلگرام) فرستاده می‌شد و cfg['plan'] بدون هیچ escape‌ای مستقیم داخل متن
    قرار می‌گرفت. نسخه‌ی قدیمی Markdown تلگرام امکان escape کردن کاراکترهای
    خاص رو نداره؛ پس اگر نام پلن یک زیرخط (_) تک و جفت‌نشده داشت (مثل
    "Businesss_vpn - 1090174")، پارسر اون رو شروع ایتالیک در نظر می‌گرفت و
    چون بسته نمی‌شد، کل درخواست ویرایش پیام با خطای "can't parse entities"
    رد می‌شد و صفحه‌ی جزئیات سرویس اصلاً نمایش داده نمی‌شد.
    راه‌حل: استفاده از HTML به‌جای Markdown، چون HTML یک تابع escape رسمی و
    قابل‌اعتماد داره (html.escape) و این مشکل اصلاً پیش نمیاد.
    """
    icon = "🚀" if cfg.get("type", "vip") == "vip" else "🎮"
    status = "❌ حذف‌شده (توسط کاربر یا ادمین)" if cfg.get("deleted") else "✅ فعال"
    try:
        sub_preview = crypto.decrypt_config(cfg["config"])
    except Exception:
        sub_preview = "⚠️ خطا در رمزگشایی"

    plan_safe = html.escape(str(cfg.get("_display_name") or cfg.get("plan", "")))
    created_safe = html.escape(str(cfg.get("created_at", "")))
    sub_preview_safe = html.escape(sub_preview)

    text = (
        f"{icon} {plan_safe}\n\n"
        f"📌 وضعیت: {status}\n"
        f"📆 تاریخ ایجاد: {created_safe}\n"
    )
    if cfg.get("service_id"):
        text += f"🆔 شناسه سرویس: {html.escape(str(cfg['service_id']))}\n"

    usage = None
    sub_url = sub_preview if sub_preview.lower().startswith(("http://", "https://")) else None
    if cfg.get("type", "vip") == "vip" and sub_url:
        try:
            usage = await fetch_subscription_info(sub_url)
        except Exception:
            usage = None

    if usage:
        total = usage.get("total")
        used = (usage.get("upload") or 0) + (usage.get("download") or 0)
        remaining_bytes = (total - used) if total else None

        text += "\n📊 وضعیت مصرف (لحظه‌ای):\n"
        if total:
            text += f"   • حجم کل: {html.escape(format_bytes(total))}\n"
        text += f"   • مصرف‌شده: {html.escape(format_bytes(used))}\n"
        if remaining_bytes is not None:
            text += f"   • باقی‌مانده: {html.escape(format_bytes(remaining_bytes))}\n"
        if total:
            percent = min(100, round(used / total * 100))
            text += f"\n{usage_bar(percent)} {percent}٪ مصرف شده\n"
        text += f"\n⏰ تاریخ انقضا: {html.escape(format_expire(usage.get('expire')))}\n"
        remaining_days = days_remaining(usage.get("expire"))
        if remaining_days is not None:
            text += "⛔️ منقضی شده\n" if remaining_days <= 0 else f"⌛️ زمان باقی‌مانده: {remaining_days} روز\n"
    elif cfg.get("expiry"):
        text += f"⏰ انقضا: {html.escape(str(cfg['expiry']))}\n"
        remaining_days = _remaining_days_from_date_str(cfg.get("expiry"))
        if remaining_days is not None:
            if remaining_days <= 0:
                text += "⌛️ زمان باقی‌مانده: ⛔️ منقضی شده\n"
            else:
                text += f"⌛️ زمان باقی‌مانده: {remaining_days} روز\n"

    text += f"\n🔗 لینک ساب فعلی:\n<code>{sub_preview_safe}</code>\n"
    if cfg.get("type", "vip") == "vip":
        text += f"\n��� کیوآرکد: {'ثبت شده ✅' if cfg.get('qr_file_id') else 'ثبت نشده ❌'}"
    else:
        files = db.get_gaming_files(cfg["id"])
        text += f"\n📁 تعداد فایل کانفیگ: {len(files)}"
    return text


async def _render_service_detail(callback: types.CallbackQuery, cfg_id: int):
    cfg = db.get_config_by_id(cfg_id)
    if cfg is None:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return
    owner = db.get_user_by_id(cfg["user_id"])
    uid = owner["telegram_id"] if owner else ""

    await enrich_configs_with_subscription_names([cfg])
    text = await _service_detail_text(cfg)
    _sub_url = None
    try:
        _dec = str(crypto.decrypt_config(cfg["config"]) or "").strip()
        if _dec.lower().startswith(("http://", "https://")):
            _sub_url = _dec
    except Exception:
        _sub_url = None
    kb = admin_service_detail_keyboard(cfg, uid, sub_link_url=_sub_url)
    # -----------------------------------------------------------------
    # علت اصلی خطای "خطایی پیش آمد..." روی این صفحه: نام پلن (cfg['plan'])
    # می‌تونه هرچیزی باشه (مثلاً چیزی شبیه "Businesss_vpn - 1090174") و اگر
    # داخلش یک زیرخط (_) تکی و جفت‌نشده باشه، پارسر Markdown قدیمی تلگرام
    # اون رو شروع ایتالیک در نظر می‌گیره و چون بسته نمی‌شه، کل پیام با خطای
    # "can't parse entities" رد می‌شه. قبلاً هیچ try/except اینجا نبود، پس
    # این خطا مستقیم می‌رفت به هندلر سراسری و کاربر فقط "خطایی پیش آمد" رو
    # می‌بیند بدون اینکه جزئیات سرویس اصلاً نمایش داده بشه. حالا اگر مارک‌داون
    # شکست بخوره، همون متن رو بدون فرمت (parse_mode=None) دوباره می‌فرستیم.
    # -----------------------------------------------------------------
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            pass
        else:
            logger.exception("خطا در نمایش جزئیات سرویس ادمین برای cfg_id=%s", cfg_id)
            try:
                await callback.message.edit_text(text, parse_mode=None, reply_markup=kb)
            except Exception:
                logger.exception("خطا در fallback بدون فرمت برای جزئیات سرویس cfg_id=%s", cfg_id)
                await callback.answer("❌ خطا در نمایش جزئیات سرویس. دوباره تلاش کنید.", show_alert=True)
                return
    await callback.answer()


@router.callback_query(F.data.startswith("svcdetail_"))
async def admin_service_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcdetail_", ""))
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcdelete_"))
async def admin_service_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcdelete_", ""))
    db.set_config_deleted(cfg_id, True)
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcrestore_"))
async def admin_service_restore(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcrestore_", ""))
    db.set_config_deleted(cfg_id, False)
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcpurge_"))
async def admin_service_purge_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcpurge_", ""))
    await callback.message.edit_text(
        "⚠️ این کار غیرقابل بازگشته و کل اطلاعات این سرویس (شامل فایل‌های گیمینگ) برای همیشه پاک می‌شه.\n\nمطمئنی؟",
        reply_markup=admin_purge_confirm_keyboard(cfg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svcpurgeconfirm_"))
async def admin_service_purge_apply(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcpurgeconfirm_", ""))
    cfg = db.get_config_by_id(cfg_id)
    owner = db.get_user_by_id(cfg["user_id"]) if cfg else None
    uid = owner["telegram_id"] if owner else ""
    db.delete_config_permanently(cfg_id)
    await callback.message.edit_text("✅ سرویس برای همیشه حذف شد.", reply_markup=admin_back_button())
    await callback.answer()


@router.callback_query(F.data.startswith("svcedit_link_"))
async def admin_service_edit_link_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcedit_link_", ""))
    await state.update_data(edit_config_id=cfg_id)
    await state.set_state(AdminStates.waiting_edit_sublink)
    await callback.message.answer("🔗 لینک ساب جدید این سرویس رو ارسال کن:")
    await callback.answer()


@router.message(AdminStates.waiting_edit_sublink)
async def admin_service_edit_link_apply(message: types.Message, state: FSMContext):
    new_link = (message.text or "").strip()
    if not new_link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک رو با http یا https ارسال کن:")
        return

    data = await state.get_data()
    cfg_id = data.get("edit_config_id")
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await message.answer("❌ سرویس یافت نشد.")
        await state.clear()
        return

    db.update_config_link(cfg_id, crypto.encrypt_config(new_link))
    await message.answer("✅ لینک ساب سرویس بروزرسانی شد.", reply_markup=admin_back_button())
    await state.clear()


@router.callback_query(F.data.startswith("svcedit_qr_"))
async def admin_service_edit_qr_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcedit_qr_", ""))
    await state.update_data(edit_config_id=cfg_id)
    await state.set_state(AdminStates.waiting_edit_qr)
    await callback.message.answer("🖼 عکس کیوآرکد جدید این سرویس رو ارسال کن:")
    await callback.answer()


@router.message(AdminStates.waiting_edit_qr, F.photo)
async def admin_service_edit_qr_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cfg_id = data.get("edit_config_id")
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await message.answer("❌ سرویس یافت نشد.")
        await state.clear()
        return

    db.set_config_qr(cfg_id, message.photo[-1].file_id)
    await message.answer("✅ عکس کیوآرکد سرویس بروزرسانی شد.", reply_markup=admin_back_button())
    await state.clear()


@router.message(AdminStates.waiting_edit_qr)
async def admin_service_edit_qr_wrong_format(message: types.Message):
    await message.answer("📸 لطفاً عکس کیوآرکد رو ارسال کن (نه متن).")














# ---------------------------------------------------------------------------
# 🎟 مدیریت تخفیف (ساخت کد تخفیف به‌صورت گام‌به‌گام)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_discount")
async def admin_discount_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    discounts = db.get_all_discounts()
    if not discounts:
        text = "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    else:
        text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇"

    await callback.message.edit_text(text, reply_markup=admin_discount_menu(discounts))
    await callback.answer()


def _discount_detail_text(d: dict) -> str:
    if d.get("discount_type") == "amount":
        value_text = f"💵 {d['amount']:,} تومان"
    else:
        value_text = f"�� {d['percent']}٪"

    plans_raw = d.get("applicable_plans")
    if not plans_raw:
        plans_text = "همه‌ی پلن‌ها"
    else:
        plans_text = discount_scope_summary(json.loads(plans_raw)) or "همه‌ی پلن‌ها"

    users_raw = d.get("allowed_user_ids")
    if not users_raw:
        users_text = "—"
    else:
        ids = json.loads(users_raw)
        users_text = "، ".join(f"`{i}`" for i in ids)

    audience = d.get("audience") or "all"
    from keyboards import _DISCOUNT_AUDIENCE_LABELS as _DAL
    audience_text = f"🎯 فقط افراد خاص: {users_text}" if audience == "specific" else _DAL.get(audience, audience)

    extra = ""
    if d.get("min_order_amount"):
        extra += f"\n💰 حداقل مبلغ سفارش: {d['min_order_amount']:,} تومان"
    if d.get("max_uses_per_user"):
        extra += f"\n🔂 سقف استفاده برای هر کاربر: {d['max_uses_per_user']}"
    if d.get("expires_at"):
        extra += f"\n⏰ انقضا: {d['expires_at']}"

    return (
        f"🎟 کد تخفیف: `{d['code']}`\n"
        f"{value_text}\n"
        f"🔁 تعداد استفاده‌ی باقی‌مانده: {d['uses']}\n"
        f"🎯 پلن‌های مجاز: {plans_text}\n"
        f"👥 مخاطب: {audience_text}"
        f"{extra}"
    )


@router.callback_query(F.data.startswith("discdetail_"))
async def admin_discount_detail(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    discount_id = int(callback.data.replace("discdetail_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all")
    )
    await callback.answer()


# 🆕 «تخفیف اولین خرید» (بند ۱۴): درصد تخفیفی که فقط به کسی می‌رسد که هیچ
# خرید قبلی در فروشگاه ندارد — مشوقی برای مشتری تازه، نه برای کسی که همین
# الان هم دارد می‌خرد. زیرساخت دیتابیسی‌اش از قبل بود، فقط همین دکمه‌ی
# روشن/خاموش‌کردنش هندلر نداشت و بی‌جواب می‌ماند.
@router.callback_query(F.data.startswith("discedit_firstpurchase_"))
async def admin_discount_toggle_first_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_firstpurchase_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    new_value = 0 if bool(d.get("first_purchase_only")) else 1
    db.update_discount(discount_id, first_purchase_only=new_value)
    d = db.get_discount_by_id(discount_id)
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown",
        reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"),
    )
    await callback.answer("✅ فقط برای اولین خرید فعال شد." if new_value else "✅ برای همه‌ی خریدها فعال شد.")


@router.callback_query(F.data.startswith("discdelete_"))
async def admin_discount_delete_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discdelete_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"❗️ آیا از حذف کد `{d['code']}` مطمئن هستید؟",
        parse_mode="Markdown",
        reply_markup=discount_delete_confirm_keyboard(discount_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discdeleteconfirm_"))
async def admin_discount_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discdeleteconfirm_", ""))
    db.delete_discount_by_id(discount_id)
    await callback.answer("✅ کد تخفیف حذف شد.", show_alert=True)
    discounts = db.get_all_discounts()
    text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇" if discounts else \
        "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    await callback.message.edit_text(text, reply_markup=admin_discount_menu(discounts))


# --- ویرایش مقدار تخفیف (درصد/مبلغ) ---
@router.callback_query(F.data.startswith("discedit_value_"))
async def admin_discount_edit_value_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_value_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    label = "درصد جدید را وارد کنید (بین ۱ تا ۱۰۰)" if d.get("discount_type") != "amount" else \
        "مبلغ ثابت جدید را به تومان وارد کنید"
    await callback.message.edit_text(f"✏️ {label}:", reply_markup=admin_back_button())
    await state.set_state(AdminStates.waiting_discount_edit_value)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_value)
async def admin_discount_edit_value_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    d = db.get_discount_by_id(discount_id) if discount_id else None
    if d is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(clean_numeric_id(message.text))
    if d.get("discount_type") == "amount":
        if value <= 0:
            await message.answer("❌ مبلغ باید بزرگ‌تر از صفر باشد:")
            return
        db.update_discount(discount_id, amount=value)
    else:
        if not (1 <= value <= 100):
            await message.answer("❌ درصد باید بین ۱ تا ۱۰۰ باشد:")
            return
        db.update_discount(discount_id, percent=value)

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ مقدار تخفیف بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش تعداد استفاده‌ی باقی‌مانده ---
@router.callback_query(F.data.startswith("discedit_uses_"))
async def admin_discount_edit_uses_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_uses_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ تعداد دفعات مجاز باقی‌مانده‌ی این کد را وارد کنید (مثلاً 50):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_uses)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_uses)
async def admin_discount_edit_uses_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not clean_numeric_id(message.text).isdigit() or int(clean_numeric_id(message.text)) < 0:
        await message.answer("❌ لطفاً یک عدد صحیح غیرمنفی وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, uses=int(clean_numeric_id(message.text)))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ تعداد استفاده بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش حداقل مبلغ سفارش ---
@router.callback_query(F.data.startswith("discedit_minorder_"))
async def admin_discount_edit_minorder_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_minorder_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ حداقل مبلغ سفارش (به تومان) برای استفاده از این کد را وارد کنید.\n"
        "برای برداشت�� محدودیت، عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_min_order)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_min_order)
async def admin_discount_edit_minorder_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, min_order_amount=int(clean_numeric_id(message.text)))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ حداقل مبلغ سفارش بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش سقف استفاده‌ی هر کاربر ---
@router.callback_query(F.data.startswith("discedit_maxuser_"))
async def admin_discount_edit_maxuser_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_maxuser_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت ��شد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ سقف تعداد دفعا�� استفاده‌ی هر کاربر از این کد را وارد کنید.\n"
        "برای بی‌محدودیت‌کردن، عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_max_per_user)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_max_per_user)
async def admin_discount_edit_maxuser_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, max_uses_per_user=int(clean_numeric_id(message.text)))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ سقف استفاده‌ی هر کاربر بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش تاریخ انقضا ---
@router.callback_query(F.data.startswith("discedit_expiry_"))
async def admin_discount_edit_expiry_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_expiry_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ تاریخ انقضا را به‌فرمت `YYYY-MM-DD` (مثلاً 2026-12-31) وارد کنید.\n"
        "برای برداشتن انقضا (کد همیشه معتبر باشد)، عدد 0 را ارسال کنید.",
        parse_mode="Markdown",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_expiry)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_expiry)
async def admin_discount_edit_expiry_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    raw = (message.text or "").strip()
    if raw == "0":
        db.update_discount(discount_id, expires_at=None)
    else:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            await message.answer("❌ فرمت نامعتبر است؛ به‌صورت YYYY-MM-DD وارد کنید (یا 0 برای حذف انقضا):")
            return
        db.update_discount(discount_id, expires_at=parsed.strftime("%Y-%m-%d 23:59:59"))

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ تاریخ انقضا بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش کاربران مجاز ---
@router.callback_query(F.data.startswith("discedit_audience_"))
async def admin_discount_audience_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_audience_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        "👥 این کد تخفیف برای چه کسانی قابل استفاده باشد؟",
        reply_markup=discount_audience_keyboard(discount_id, d.get("audience") or "all"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discaud_"))
async def admin_discount_audience_set(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # callback_data به‌شکل discaud_<value>_<discount_id> است؛ value خودش
    # ممکن است underscore داشته باشد (non_buyers)، پس از سمت راست جدا می‌شود.
    body = callback.data[len("discaud_"):]
    value, _, discount_id_str = body.rpartition("_")
    if not value or not discount_id_str.isdigit():
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    discount_id = int(discount_id_str)
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    db.update_discount(discount_id, audience=value)
    d = db.get_discount_by_id(discount_id)
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown",
        reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"),
    )
    await callback.answer("✅ مخاطب تغییر کرد.")


@router.callback_query(F.data.startswith("discedit_users_"))
async def admin_discount_edit_users_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_users_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "👤 آیدی‌های عددی تلگرام مجاز به استفاده از این کد را وارد کنید "
        "(هرکدام با کاما، فاصله یا خط جدید جدا شود).\n\n"
        "برای برداشتن محدودیت (باز کردن کد برای همه‌ی کاربران) عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_users)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_users)
async def admin_discount_edit_users_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    raw = (message.text or "").strip()
    ids = [p for p in re.split(r"[\s,،]+", raw) if p]

    if not ids or ids == ["0"]:
        db.update_discount(discount_id, allowed_user_ids=None, audience="all")
        summary = "بدون محدودیت (همه‌ی کاربران)"
    else:
        if not all(p.lstrip("-").isdigit() for p in ids):
            await message.answer("❌ فقط آیدی‌های عددی معتبر وارد کنید (یا 0 برای باز کردن برای همه):")
            return
        ids = sorted(set(ids))
        # 🆕 وارد کردن آیدی‌های خاص یعنی این کد صریحاً روی دسته‌ی «افراد
        # مشخص» تنظیم می‌شود (اگر قبلاً روی «خریداران»/«بدون‌خرید» بود، این
        # انتخاب صریح‌تر جایگزینش می‌شود).
        db.update_discount(discount_id, allowed_user_ids=ids, audience="specific")
        summary = "، ".join(f"`{i}`" for i in ids)

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer(f"✅ کاربران مجاز بروزرسانی شد: {summary}", parse_mode="Markdown", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش پلن‌های مجاز ---
@router.callback_query(F.data.startswith("discedit_plans_"))
async def admin_discount_edit_plans_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_plans_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    selected = db._discount_plans(d) or []
    await state.update_data(edit_discount_id=discount_id, edit_discount_plans=selected)
    await callback.message.edit_text(
        f"🎯 این کد روی چه چیزهایی اعمال شود؟\n\nانتخاب فعلی: {discount_scope_summary(selected)}",
        reply_markup=discount_scope_keyboard("e", discount_id, selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discplaned_"))
async def admin_discount_edit_plans_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # discplaned_<id>_<key|all|done>
    rest = callback.data.replace("discplaned_", "")
    discount_id_str, _, key = rest.partition("_")
    discount_id = int(discount_id_str)
    data = await state.get_data()
    selected = data.get("edit_discount_plans", [])

    if key == "all":
        selected = []
    elif key == "done":
        db.update_discount(discount_id, applicable_plans=selected or None)
        await state.clear()
        d = db.get_discount_by_id(discount_id)
        await callback.message.edit_text(
            _discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all")
        )
        await callback.answer("✅ پلن‌های مجاز ذخیره شد.")
        return
    else:
        selected = [p for p in selected if p != key] if key in selected else selected + [key]

    await state.update_data(edit_discount_plans=selected)
    await callback.message.edit_reply_markup(reply_markup=discount_plans_edit_keyboard(discount_id, selected))
    await callback.answer()


@router.callback_query(F.data == "new_discount")
async def new_discount_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🎟 ساخت کد تخفیف جدید — مرحله ۱ از ۶\n\n"
        "✏️ کد تخفیف مورد نظر را بدون فاصله وارد کنید (مثلاً SUMMER20):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_code_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_code_step)
async def new_discount_code_input(message: types.Message, state: FSMContext):
    code = message.text.strip().upper() if message.text else ""
    if not code or " " in code:
        await message.answer("❌ کد نامعتبر است؛ بدون فاصله دوباره وارد کنید:")
        return
    await state.update_data(new_discount_code=code)
    await message.answer(
        "🎟 مرحله ۲ از ۶\n\nنوع تخفیف را انتخاب کنید:", reply_markup=discount_type_keyboard()
    )


@router.callback_query(F.data.startswith("disctype_"), AdminStates.waiting_discount_code_step)
async def new_discount_type_chosen(callback: types.CallbackQuery, state: FSMContext):
    disc_type = callback.data.replace("disctype_", "")
    await state.update_data(new_discount_type=disc_type, new_discount_plans=[])
    label = "درصد تخفیف را وارد کنید (عددی بین ۱ تا ۱۰۰، مثلاً 20)" if disc_type == "percent" else \
        "مبلغ ثابت تخفیف را به تومان وارد کنید (مثلاً 20000)"
    await callback.message.edit_text(f"🎟 مرحله ۳ از ۶\n\n💯 {label}:")
    await state.set_state(AdminStates.waiting_discount_value_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_value_step)
async def new_discount_value_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    disc_type = data.get("new_discount_type", "percent")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(clean_numeric_id(message.text))
    if disc_type == "percent" and not (1 <= value <= 100):
        await message.answer("❌ درصد باید بین ۱ تا ۱۰۰ باشد:")
        return
    if disc_type == "amount" and value <= 0:
        await message.answer("❌ مبلغ باید بزرگ‌تر از صفر باشد:")
        return

    await state.update_data(new_discount_value=value)
    await message.answer(
        "🎟 مرحله ۴ از ۶\n\n"
        "🎯 این کد روی چه چیزهایی اعمال شود؟\n"
        "دسته‌بندی، پلن، تمدید یا سرویس سفارشی را انتخاب کنید؛ اگر چیزی انتخاب نکنید یا «همه» را بزنید هیچ محدودیتی نخواهد داشت.\n\n"
        "انتخاب فعلی: ♾ همه (بدون محدودیت)",
        reply_markup=discount_scope_keyboard("n", 0, []),
    )


@router.callback_query(F.data.startswith("discplan_"), AdminStates.waiting_discount_value_step)
async def new_discount_plan_toggle(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.replace("discplan_", "")
    data = await state.get_data()
    selected = data.get("new_discount_plans", [])

    if key == "all":
        selected = []
    elif key == "done":
        await state.update_data(new_discount_plans=selected)
        await callback.message.edit_text(
            "🎟 مرحله ۵ از ۶\n\n"
            "👤 این کد فقط برای چه کاربرانی مجاز باشد؟\n\n"
            "آیدی‌های عددی تلگرام را وارد کنید (هرکدام با کاما، فاصله یا خط جدید جدا شود).\n"
            "برای باز بودن کد برای همه عدد 0 بفرستید؛ مخاطب دسته‌ای (خریدار، منقضی و…) را بعد از ساخت از صفحه‌ی کد انتخاب کنید.",
            reply_markup=admin_back_button(),
        )
        await state.set_state(AdminStates.waiting_discount_users_step)
        await callback.answer()
        return
    else:
        selected = [p for p in selected if p != key] if key in selected else selected + [key]

    await state.update_data(new_discount_plans=selected)
    await callback.message.edit_reply_markup(reply_markup=discount_plans_select_keyboard(selected))
    await callback.answer()


_STEP5_TEXT = ("🎟 مرحله ۵ از ۶\n\n👤 این کد برای چه کاربرانی مجاز باشد؟\n"
               "یکی از دسته‌های زیر را انتخاب کنید (همان لیست «ویرایش کاربران مجاز» صفحه‌ی کد) یا «فقط افراد خاص» را بزنید و آیدی عددی بدهید.")


@router.callback_query(F.data.startswith("dsc|"))
async def discount_scope_cb(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        _, mode, did_s, act, arg = callback.data.split("|", 4)
        did = int(did_s)
    except Exception:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    key = "new_discount_plans" if mode == "n" else "edit_discount_plans"
    data = await state.get_data()
    if mode == "e" and data.get("edit_discount_id") != did:
        d0 = db.get_discount_by_id(did)
        if d0 is None:
            await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
            return
        data["edit_discount_plans"] = db._discount_plans(d0) or []
        await state.update_data(edit_discount_id=did, edit_discount_plans=data["edit_discount_plans"])
    selected = list(data.get(key) or [])

    if act == "done":
        if mode == "n":
            await state.update_data(new_discount_plans=selected)
            await callback.message.edit_text(_STEP5_TEXT, reply_markup=discount_new_audience_keyboard())
            await callback.answer()
            return
        db.update_discount(did, applicable_plans=selected or None)
        await state.clear()
        d = db.get_discount_by_id(did)
        await callback.message.edit_text(
            _discount_detail_text(d), parse_mode="Markdown",
            reply_markup=discount_detail_keyboard(did, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"),
        )
        await callback.answer("✅ محدوده‌ی کد ذخیره شد.")
        return
    if act == "all":
        selected = []
    elif act == "t":
        selected = [x for x in selected if x != arg] if arg in selected else selected + [arg]
    view = {"cats": "cats", "plans": "plans"}.get(act, "main")
    if act == "t":  # بعد از toggle در همان صفحه‌ی فعلی می‌مانیم
        view = "cats" if arg.startswith("cat:") else ("main" if arg.startswith("__") else "plans")
    await state.update_data(**{key: selected})
    head = {"cats": "📁 دسته‌بندی‌هایی که کد روی آن‌ها اعمال شود را انتخاب کنید (شامل زیرپوشه‌ها):",
            "plans": "🧩 پلن‌هایی که کد روی آن‌ها اعمال شود را انتخاب کنید:"}.get(view, "🎯 این کد روی چه چیزهایی اعمال شود؟")
    await callback.message.edit_text(
        f"{head}\n\nانتخاب فعلی: {discount_scope_summary(selected)}",
        reply_markup=discount_scope_keyboard(mode, did, selected, view),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ndaud|"))
async def new_discount_audience_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    value = callback.data.split("|", 1)[1]
    if value == "specific":
        await state.update_data(new_discount_audience="specific")
        await callback.message.edit_text(
            "🎯 آیدی‌های عددی تلگرام کاربران مجاز را وارد کنید (هرکدام با کاما، فاصله یا خط جدید جدا شود):",
            reply_markup=admin_back_button(),
        )
        await state.set_state(AdminStates.waiting_discount_users_step)
        await callback.answer()
        return
    await state.update_data(new_discount_audience=value, new_discount_users=None)
    await callback.message.edit_text("🎟 مرحله ۶ از ۶\n\n🔁 تعداد دفعات مجاز استفاده از این کد را وارد کنید (مثلاً 50):")
    await state.set_state(AdminStates.waiting_discount_uses_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_users_step)
async def new_discount_users_input(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    ids = [p for p in re.split(r"[\s,،]+", raw) if p]

    if not ids or ids == ["0"]:
        await state.update_data(new_discount_users=None, new_discount_audience="all")
    elif all(p.lstrip("-").isdigit() for p in ids):
        await state.update_data(new_discount_users=sorted(set(ids)), new_discount_audience="specific")
    else:
        await message.answer("❌ فقط آیدی‌های عددی معتبر وارد کنید (یا 0 برای باز کردن برای همه):")
        return

    await message.answer(
        "🎟 مرحله ۶ از ۶\n\n🔁 تعداد دفعات مجاز استفاده از این کد را وارد کنید (مثلاً 50):"
    )
    await state.set_state(AdminStates.waiting_discount_uses_step)


@router.message(AdminStates.waiting_discount_uses_step)
async def new_discount_uses_input(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit() or int(clean_numeric_id(message.text)) <= 0:
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید:")
        return

    data = await state.get_data()
    code = data.get("new_discount_code")
    disc_type = data.get("new_discount_type", "percent")
    value = data.get("new_discount_value", 0)
    plans = data.get("new_discount_plans") or None
    allowed_users = data.get("new_discount_users") or None
    audience = data.get("new_discount_audience") or ("specific" if allowed_users else "all")
    uses = int(clean_numeric_id(message.text))

    try:
        db.create_discount(
            code,
            percent=value if disc_type == "percent" else 0,
            uses=uses,
            discount_type=disc_type,
            amount=value if disc_type == "amount" else 0,
            applicable_plans=plans,
            allowed_user_ids=allowed_users,
        )
        if allowed_users or audience != "all":
            try:
                _new = [d for d in db.get_all_discounts() if d["code"] == code.upper()]
                if _new:
                    db.update_discount(_new[0]["id"], audience=audience)
            except Exception:
                logger.exception("set discount audience failed")
        value_text = f"{value}٪" if disc_type == "percent" else f"{value:,} تومان"
        all_plans = db.get_all_plans()
        plans_text = discount_scope_summary(plans or [])
        from keyboards import _DISCOUNT_AUDIENCE_LABELS as _DAL2
        users_text = ("، ".join(f"`{i}`" for i in allowed_users) if allowed_users else _DAL2.get(audience, audience))
        await message.answer(
            f"✅ کد تخفیف جدید با موفقیت ساخته شد! 🎉\n\n"
            f"🎟 کد: `{code}`\n💯 مقدار: {value_text}\n🎯 پلن‌ها: {plans_text}\n"
            f"👤 کاربران مجاز: {users_text}\n🔁 تعداد استفاده: {uses}",
            parse_mode="Markdown",
            reply_markup=admin_discount_menu(db.get_all_discounts()),
        )
    except Exception:
        await message.answer("❌ این کد قبلاً ثبت شده.", reply_markup=admin_discount_menu(db.get_all_discounts()))
    await state.clear()


# ---------------------------------------------------------------------------
# 🤝 مدیریت دعوت‌ها
# ---------------------------------------------------------------------------
REFERRERS_PER_PAGE = 10


def _referrers_page_text(page: int, total: int) -> str:
    if total == 0:
        return "🤝 هنوز هیچ دعوتی ثبت نشده."
    return (
        f"🤝 مدیریت دعوت‌شده‌ها — مرتب‌شده بر اساس بیشترین دعوت\n\n"
        f"👥 تعداد کل دعوت‌کننده‌ها: {total}\n\n"
        f"روی هر کدام بزنید تا لیست دعوت‌شده‌هایش و وضعیت کیف پولش رو ببینید 👇"
    )


@router.callback_query(F.data == "admin_referrals")
async def admin_referrals(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_referrers_page(callback, 0)
    await callback.answer()


async def _render_referrers_page(callback: types.CallbackQuery, page: int):
    total = db.count_referrers()
    users = db.get_referrers_page(page, REFERRERS_PER_PAGE)
    has_next = total > (page + 1) * REFERRERS_PER_PAGE
    await callback.message.edit_text(
        _referrers_page_text(page, total),
        reply_markup=admin_referrers_page_keyboard(users, page, has_next),
    )


@router.callback_query(F.data.startswith("refpage_"))
async def admin_referrers_page_nav(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    page = int(callback.data.replace("refpage_", ""))
    await _render_referrers_page(callback, page)
    await callback.answer()


@router.callback_query(F.data.startswith("refdetail_"))
async def admin_referrer_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, uid, page_str = callback.data.split("_")
    page = int(page_str)

    referrer = db.get_user(uid)
    if referrer is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    invited = db.get_referred_users(referrer["id"])
    text = (
        f"🤝 دعوت‌شده‌های {referrer['name']} (🆔 {referrer['telegram_id']})\n\n"
        f"💛 کیف پول آزاد دعوت‌کننده: {referrer['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود دعوت‌کننده (در انتظار): {referrer['locked_wallet']:,} تومان\n"
        f"👥 تعداد دعوت: {referrer['invited_count']} | ✅ موفق: {referrer['successful_invites']}\n\n"
        f"📋 لیست افراد دعوت‌شده:\n"
    )
    if not invited:
        text += "— هنوز هیچ کاربری ثبت نشده."
    else:
        for i, u in enumerate(invited, 1):
            reward = u.get("referral_reward") or 0
            status = u.get("referral_status") or "-"
            text += (
                f"{i}. {u['name']} | 🆔 {u['telegram_id']} | "
                f"🎁 پاداش: {reward:,} تومان | وضعیت: {status}\n"
            )

    await callback.message.edit_text(text, reply_markup=admin_referred_detail_keyboard(uid, page))
    await callback.answer()


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — با ثبت آیدی عددی یک فرد، همه‌ی خریدهای VIP بعدی او به‌صورت
# خودکار با درصد تعیین‌شده تخفیف می‌خورد (بدون نیاز به وارد کردن کد تخفیف).
# ---------------------------------------------------------------------------
def _user_detail_text(user: dict) -> str:
    """همان متن استاندا��د صفحه‌ی «مدیریت کاربر» (بخش کاربران)؛ برای اینکه صفحه‌ی
    نماینده هم دقیقاً همین محیط را نشان دهد، این تابع مشترک استفاده می‌شود."""
    stats = db.get_referral_stats(user["id"])
    return (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )


@router.callback_query(F.data == "admin_agency")
async def admin_agency_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    agents = db.get_all_agents()
    text = (
        "🤝 هنوز هیچ نماینده‌ای ثبت نشده.\n\nبرای افزودن، دکمه‌ی زیر را بزنید 👇"
        if not agents else
        "🤝 نمایندگان فعلی (تخفیف خودکار روی VIP)\n\nروی هرکدام بزنید تا مثل بخش «کاربران» مدیریتش کنید 👇"
    )

    await callback.message.edit_text(text, reply_markup=admin_agency_menu(agents))
    await callback.answer()


@router.callback_query(F.data == "new_agent")
async def new_agent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🤝 افزودن نماینده\n\n"
        "🆔 آیدی عددی (Telegram ID) فرد را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_agent_id_step)
    await callback.answer()


@router.message(AdminStates.waiting_agent_id_step)
async def new_agent_id_input(message: types.Message, state: FSMContext):
    tid = clean_numeric_id(message.text or "").strip()
    if not tid.isdigit():
        await message.answer("❌ آیدی عددی نامعتبر است؛ فقط عدد ارسال کنید:")
        return
    existing = db.get_agent(tid)
    # ثبت فقط با آیدی عددی؛ درصد تخفیف از «مدیریت تخفیف نمایندگی» تنظیم می‌شود (نماینده‌ی موجود دست‌نخورده می‌ماند)
    if existing is None:
        db.add_agent(tid, 0)
        try:
            from aiogram.types import InlineKeyboardMarkup
            welcome_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏷 انتخاب نام دلخواه برای ساخت کانفیگ", callback_data="agentprefix", style="success")
            ]])
            await message.bot.send_message(
                int(tid),
                "🎉 تبریک! شما به‌عنوان نماینده ربات ثبت شدید.\n\nمی‌توانید نام دلخواه خود را برای ساخت کانفیگ انتخاب کنید.",
                reply_markup=welcome_kb,
            )
            await message.bot.send_message(int(tid), "⬇️ مدیریت نمایندگی از منوی پایین در دسترس است.", reply_markup=main_reply_keyboard(int(tid)))
        except Exception:
            logger.exception("ارسال پیام خوش‌آمد نماینده %s ناموفق بود", tid)
    await state.clear()
    await message.answer(
        f"✅ نماینده ثبت شد!\n\n🆔 {tid}\n\nبرای تعیین تخفیف، روی نماینده بزنید و «🎯 مدیریت تخفیف نمایندگی» را انتخاب کنید."
        if existing is None else f"ℹ️ این آیدی از قبل نماینده است: 🆔 {tid}",
        reply_markup=admin_agency_menu(db.get_all_agents()),
    )


@router.callback_query(F.data.startswith("editagentcats_"))
async def admin_agent_categories(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    tid=callback.data.replace("editagentcats_", "")
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    cats=db.get_vip_categories(); selected=set(db.get_agent_category_ids(tid))
    rows=[]
    for c in cats:
        cid=int(c["id"]); mark="✅" if (not selected or cid in selected) else "⬜️"
        rows.append([InlineKeyboardButton(text=f"{mark} {c['name']}", callback_data=f"agentcat_{tid}_{cid}", style="primary")])
    rows.append([InlineKeyboardButton(text="🌐 همه دسته‌ها", callback_data=f"agentcatall_{tid}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agentopen_{tid}", style="danger")])
    await callback.message.edit_text("🗂 دسته‌های مشمول تخفیف نمایندگی\n\nبا زدن روی هر دسته، تخفیف نماینده فقط روی همان دسته اعمال می‌شود. اگر همه را فعال کنی، تخفیف روی تمام دسته‌هاست.", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("agentcatall_"))
async def admin_agent_categories_all(callback: types.CallbackQuery):
    tid=callback.data.replace("agentcatall_", "")
    if not _is_admin(callback.from_user.id) or not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    db.set_agent_categories(tid, [])
    await callback.answer("✅ تخفیف روی همه دسته‌ها فعال شد.")
    await admin_agent_categories(callback)

@router.callback_query(F.data.startswith("agentcat_"))
async def admin_agent_category_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    parts=callback.data.split("_")
    if len(parts)!=3 or not db.get_agent(parts[1]):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    tid,cid=parts[1],int(parts[2]); current=set(db.get_agent_category_ids(tid))
    # اگر حالت «همه» فعال بود، اولین انتخاب را به همه دسته‌ها تبدیل می‌کنیم و سپس
    # همان دسته را حذف می‌کنیم تا رفتار toggle قابل‌پیش‌بینی باشد.
    if not current:
        current={int(c["id"]) for c in db.get_vip_categories()}
    if cid in current: current.remove(cid)
    else: current.add(cid)
    db.set_agent_categories(tid, sorted(current))
    await admin_agent_categories(callback)
    await callback.answer("✅ ذخیره شد.")

@router.callback_query(F.data.startswith("editagentprefix_"))
async def admin_agent_prefix_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    tid=callback.data.replace("editagentprefix_", "")
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    await state.update_data(agent_prefix_target=tid)
    await state.set_state(AdminStates.waiting_agent_prefix)
    await callback.message.answer("🏷 پیشوند دلخواه نماینده را ب��رست (۱ تا 12 کاراکتر، فقط a-z، عدد و _):")
    await callback.answer()

@router.message(AdminStates.waiting_agent_prefix)
async def admin_agent_prefix_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    value=(message.text or '').strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,12}", value):
        await message.answer("❌ پیشوند نامعتبر است. فقط a-z، عدد و _ و حداکثر 12 کاراکتر."); return
    data=await state.get_data(); tid=data.get("agent_prefix_target")
    if not tid or not db.get_agent(tid):
        await state.clear(); await message.answer("❌ نماینده پیدا نشد."); return
    db.set_agent_prefix(tid,value); await state.clear()
    await message.answer(f"✅ پیشوند نماینده ذخیره شد: {value}", reply_markup=admin_agent_actions_keyboard(tid))


# 🛠 تخفیف نماینده به‌ازای هر «گزینه‌ی نگاشت» (سفارشی / PAYG) + 🎁 سهمیه‌ی تست ماهانه
@router.callback_query(F.data.startswith("agopt_"))
async def admin_agent_options(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    tid = callback.data[len("agopt_"):]
    agent = db.get_agent(tid)
    if not agent:
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    rows = []
    for kind, title in (("custom_build", "🛠 سفارشی"), ("payg", "⚡ PAYG")):
        for o in db.get_map_options(kind):
            ov = db.get_agent_option_percent_override(tid, kind, o["num"])
            eff = db.agent_percent_for_option(tid, kind, o["num"])
            label = f"{title} | {o.get('name') or o['num']} | {eff}٪" + ("" if ov is not None else " (پیش‌فرض)")
            rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"agoptp|{tid}|{kind}|{o['num']}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agentopen_{tid}", style="danger")])
    txt = ("🛠 تخفیف نماینده روی گزینه‌های «سرویس سفارشی» و «PAYG»\n\n"
           "برای هر گزینه یک درصد جدا بگذارید. پیش‌فرض: سفارشی = درصد کلی نماینده، PAYG = بدون تخفیف.")
    if len(rows) == 1:
        txt += "\n\n⚠️ هنوز گزینه‌ای (نگاشت پنل) برای سفارشی/PAYG تعریف نشده است."
    await callback.message.edit_text(txt, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("agoptp|"))
async def admin_agent_option_percent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    try:
        _, tid, kind, num = callback.data.split("|")
    except ValueError:
        await callback.answer(); return
    await state.update_data(ago_tid=tid, ago_kind=kind, ago_num=int(num))
    await state.set_state(AdminStates.waiting_agent_option_percent)
    await callback.message.answer("💯 درصد تخفیف این گزینه برای نماینده را بفرستید (۰ تا ۱۰۰).\nبرای برگشت به پیش‌فرض «-» بفرستید.", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_agent_option_percent)
async def admin_agent_option_percent_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    d = await state.get_data()
    txt = (message.text or "").strip()
    raw = "-" if txt == "-" else clean_numeric_id(txt).strip()
    if raw == "-":
        pct = None
    elif raw.isdigit() and 0 <= int(raw) <= 100:
        pct = int(raw)
    else:
        await message.answer("❌ عددی بین 0 تا 100 بفرستید (یا «-»):"); return
    db.set_agent_option_percent(d["ago_tid"], d["ago_kind"], d["ago_num"], pct)
    await state.clear()
    await message.answer("✅ ذخیره شد.", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 گزینه‌ها", callback_data=f"agopt_{d['ago_tid']}", style="primary")]]))


@router.callback_query(F.data.startswith("agtq_"))
async def admin_agent_test_quota_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    tid = callback.data[len("agtq_"):]
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    await state.update_data(agtq_tid=tid)
    await state.set_state(AdminStates.waiting_agent_test_quota)
    cur = db.agent_test_quota(tid)
    await callback.message.answer(
        f"🎁 سهمیه‌ی تست ماهانه‌ی نماینده {tid}\nفعلی: {cur if cur else 'بدون سهمیه (هر پلن تست یک‌بار)'}\n\n"
        "تعداد تستی که نماینده در هر ۳۰ روز می‌تواند بگیرد را بفرستید (از هر پلن تست دلخواه). برای حذف سهمیه ۰ بفرستید.",
        reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_agent_test_quota)
async def admin_agent_test_quota_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    raw = clean_numeric_id((message.text or "").strip()).strip()
    if not raw.isdigit() or int(raw) > 100000:
        await message.answer("❌ یک عدد صحیح (۰ یا بیشتر) بفرستید:"); return
    d = await state.get_data(); tid = d.get("agtq_tid")
    db.set_agent_test_quota(tid, int(raw)); await state.clear()
    await message.answer(f"✅ سهمیه‌ی تست ماهانه: {int(raw) or 'غیرفعال'}", reply_markup=admin_agent_actions_keyboard(tid))


# =============================================================================
# 🎯 مدیریت تخفیف نمایندگی — تنظیم درصد برای همه / دسته‌بندی‌ها (و تک‌تک پلن‌ها) / سفارشی / PAYG
# callbackها: agdm|tid  agdmall|tid  agdmcats|tid  agdmcat|tid|catid  agdmcatall|tid|catid
#             agdmplan|tid|plan_key  agdmopts|tid|kind  agdmoptall|tid|kind  agdmopt|tid|kind|num
# =============================================================================
_AGDM_KINDS = {"custom_build": "🛠 سرویس سفارشی", "payg": "⚡ PAYG"}


def _agdm_all_plans():
    out = []
    for c in db.get_vip_categories():
        for pl in db.get_vip_plans(c["id"]):
            out.append((c, pl))
    return out


def _agdm_unrestrict(tid: str, category_id=None, plan_key=None, everything=False):
    """محدودیت‌های قدیمیِ «دسته‌های مشمول/پلن‌های مجاز» جلوی اعمال درصد تنظیم‌شده را نگیرند."""
    try:
        if everything:
            db.set_agent_categories(tid, [])
            db.set_agent_plan_keys(tid, [])
            return
        cids = db.get_agent_category_ids(tid)
        if category_id is not None and cids and int(category_id) not in cids:
            db.set_agent_categories(tid, cids + [int(category_id)])
        keys = db.get_agent_plan_keys(tid)
        if plan_key and keys and plan_key not in keys:
            db.set_agent_plan_keys(tid, keys + [plan_key])
    except Exception:
        logger.exception("agdm: برداشتن محدودیت قدیمی نماینده ناموفق بود")


def _agdm_apply(tid: str, scope: str, ref: dict, pct: int) -> None:
    """scope: all | cat | plan | opts (همه‌ی گزینه‌های یک نوع) | opt"""
    if scope == "all":
        db.add_agent(tid, pct, (db.get_agent(tid) or {}).get("note"))
        _agdm_unrestrict(tid, everything=True)
        for c, pl in _agdm_all_plans():
            db.set_agent_plan_percent(tid, pl["plan_key"], pct)
        for kind in _AGDM_KINDS:
            for o in db.get_map_options(kind):
                db.set_agent_option_percent(tid, kind, o["num"], pct)
    elif scope == "cat":
        cid = int(ref["cat"])
        _agdm_unrestrict(tid, category_id=cid)
        for pl in db.get_vip_plans(cid):
            _agdm_unrestrict(tid, plan_key=pl["plan_key"])
            db.set_agent_plan_percent(tid, pl["plan_key"], pct)
    elif scope == "plan":
        pl = db.get_vip_plan(ref["plan"]) or {}
        _agdm_unrestrict(tid, category_id=pl.get("category_id"), plan_key=ref["plan"])
        db.set_agent_plan_percent(tid, ref["plan"], pct)
    elif scope == "opts":
        for o in db.get_map_options(ref["kind"]):
            db.set_agent_option_percent(tid, ref["kind"], o["num"], pct)
    elif scope == "opt":
        db.set_agent_option_percent(tid, ref["kind"], int(ref["num"]), pct)


def _agdm_main_view(tid: str):
    agent = db.get_agent(tid) or {}
    text = (
        f"🎯 مدیریت تخفیف نمایندگی\n🆔 {tid}\n\n"
        f"💯 درصد پایه‌ی نماینده: {int(agent.get('vip_discount_percent') or 0)}٪\n\n"
        "با «تخفیف همه» یک درصد برای تمام پلن‌ها، دسته‌بندی‌ها، سرویس سفارشی و PAYG ثبت می‌شود؛ "
        "بعد می‌توانید هر بخش را جداگانه تغییر دهید."
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 تخفیف همه (پلن‌ها، دسته‌ها، سفارشی و PAYG)", callback_data=f"agdmall|{tid}", style="success")],
        [InlineKeyboardButton(text="🗂 دسته‌بندی‌ها و پلن‌ها", callback_data=f"agdmcats|{tid}", style="primary")],
        [InlineKeyboardButton(text="🛠 سرویس سفارشی", callback_data=f"agdmopts|{tid}|custom_build", style="primary")],
        [InlineKeyboardButton(text="⚡ PAYG", callback_data=f"agdmopts|{tid}|payg", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agentopen_{tid}", style="danger")],
    ])
    return text, kb


def _agdm_cats_view(tid: str):
    rows = []
    for c in db.get_vip_categories():
        plans = db.get_vip_plans(c["id"])
        if not plans:
            continue
        pcts = {db.agent_percent_for_plan(tid, pl["plan_key"], c["id"]) for pl in plans}
        shown = f"{pcts.copy().pop()}٪" if len(pcts) == 1 else "متفاوت"
        rows.append([InlineKeyboardButton(text=f"🗂 {c['name']} | {shown}"[:60], callback_data=f"agdmcat|{tid}|{c['id']}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agdm|{tid}", style="danger")])
    text = "🗂 دسته‌بندی‌ها\n\nیک دسته را انتخاب کنید تا پلن‌هایش را ببینید و درصد را تنظیم کنید."
    if len(rows) == 1:
        text += "\n\n⚠️ هنوز دسته‌بندی دارای پلن وجود ندارد."
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


def _agdm_cat_view(tid: str, cid: int):
    cat = next((c for c in db.get_vip_categories() if int(c["id"]) == int(cid)), None)
    plans = db.get_vip_plans(int(cid)) if cat else []
    rows = [[InlineKeyboardButton(text="💯 تخفیف همه‌ی پلن‌های این دسته", callback_data=f"agdmcatall|{tid}|{cid}", style="success")]]
    for pl in plans:
        pct = db.agent_percent_for_plan(tid, pl["plan_key"], cid)
        rows.append([InlineKeyboardButton(text=f"{pl['name']} | {pct}٪"[:60], callback_data=f"agdmplan|{tid}|{pl['plan_key']}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agdmcats|{tid}", style="danger")])
    text = f"🗂 {cat['name'] if cat else 'دسته'}\n\nروی هر پلن بزنید تا درصد همان پلن را تنظیم کنید."
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


def _agdm_opts_view(tid: str, kind: str):
    title = _AGDM_KINDS.get(kind, kind)
    rows = [[InlineKeyboardButton(text=f"💯 تخفیف همه‌ی گزینه‌های {title}", callback_data=f"agdmoptall|{tid}|{kind}", style="success")]]
    opts = db.get_map_options(kind)
    for o in opts:
        pct = db.agent_percent_for_option(tid, kind, o["num"])
        rows.append([InlineKeyboardButton(text=f"{o['name']} | {pct}٪"[:60], callback_data=f"agdmopt|{tid}|{kind}|{o['num']}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agdm|{tid}", style="danger")])
    text = f"{title}\n\nروی هر گزینه بزنید تا درصد همان گزینه را تنظیم کنید."
    if kind == "payg":
        text += "\nدر PAYG درصد روی نرخ هر گیگ اعمال می‌شود (مثلاً ۱۰۰۰ تومان با ۳۰٪ → ۷۰۰ تومان به‌ازای هر گیگ)."
    if not opts:
        text += "\n\n⚠️ هنوز گزینه‌ای (نگاشت پنل) تعریف نشده است."
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


def _agdm_guard(callback) -> bool:
    return _is_admin(callback.from_user.id)


@router.callback_query(F.data.startswith("agdm|"))
async def agdm_main(callback: types.CallbackQuery):
    if not _agdm_guard(callback):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    tid = callback.data.split("|")[1]
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    text, kb = _agdm_main_view(tid)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("agdmcats|"))
async def agdm_cats(callback: types.CallbackQuery):
    if not _agdm_guard(callback):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    tid = callback.data.split("|")[1]
    text, kb = _agdm_cats_view(tid)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("agdmcat|"))
async def agdm_cat(callback: types.CallbackQuery):
    if not _agdm_guard(callback):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    _, tid, cid = callback.data.split("|")
    text, kb = _agdm_cat_view(tid, int(cid))
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("agdmopts|"))
async def agdm_opts(callback: types.CallbackQuery):
    if not _agdm_guard(callback):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    _, tid, kind = callback.data.split("|")
    text, kb = _agdm_opts_view(tid, kind)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


_AGDM_PROMPTS = ("agdmall|", "agdmcatall|", "agdmplan|", "agdmoptall|", "agdmopt|")


@router.callback_query(F.data.startswith(_AGDM_PROMPTS))
async def agdm_prompt(callback: types.CallbackQuery, state: FSMContext):
    if not _agdm_guard(callback):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    parts = callback.data.split("|")
    head, tid = parts[0], parts[1]
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    if head == "agdmall":
        scope, ref, back, what = "all", {}, f"agdm|{tid}", "همه‌ی پلن‌ها، دسته‌بندی‌ها، سرویس سفارشی و PAYG"
    elif head == "agdmcatall":
        scope, ref, back, what = "cat", {"cat": parts[2]}, f"agdmcat|{tid}|{parts[2]}", "همه‌ی پلن‌های این دسته"
    elif head == "agdmplan":
        pl = db.get_vip_plan(parts[2]) or {}
        scope, ref, back, what = "plan", {"plan": parts[2]}, f"agdmcat|{tid}|{pl.get('category_id') or 0}", f"پلن «{pl.get('name') or parts[2]}»"
    elif head == "agdmoptall":
        scope, ref, back, what = "opts", {"kind": parts[2]}, f"agdmopts|{tid}|{parts[2]}", f"همه‌ی گزینه‌های {_AGDM_KINDS.get(parts[2], parts[2])}"
    else:
        name = next((o["name"] for o in db.get_map_options(parts[2]) if str(o["num"]) == parts[3]), parts[3])
        scope, ref, back, what = "opt", {"kind": parts[2], "num": parts[3]}, f"agdmopts|{tid}|{parts[2]}", f"گزینه‌ی «{name}»"
    await state.update_data(agdm_tid=tid, agdm_scope=scope, agdm_ref=ref, agdm_back=back)
    await state.set_state(AdminStates.waiting_agdm_percent)
    await callback.message.answer(
        f"💯 درصد تخفیف نماینده {tid} برای {what} را بفرستید (عددی بین ۰ تا ۱۰۰؛ ۰ = بدون تخفیف).",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 انصراف", callback_data=back, style="danger")]]),
    )
    await callback.answer()


@router.message(AdminStates.waiting_agdm_percent)
async def agdm_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = clean_numeric_id((message.text or "").strip()).strip()
    if not raw.isdigit() or not (0 <= int(raw) <= 100):
        await message.answer("❌ عددی بین ۰ تا ۱۰۰ بفرستید:")
        return
    d = await state.get_data()
    tid, scope, ref, back = d.get("agdm_tid"), d.get("agdm_scope"), d.get("agdm_ref") or {}, d.get("agdm_back") or ""
    if not tid or not db.get_agent(tid):
        await state.clear()
        await message.answer("❌ نماینده یافت نشد.", reply_markup=admin_agency_menu(db.get_all_agents()))
        return
    _agdm_apply(tid, scope, ref, int(raw))
    await state.clear()
    # بازگشت به همان صفحه‌ای که از آن آمده بودیم (با مقادیر تازه)
    if scope == "all":
        text, kb = _agdm_main_view(tid)
    elif scope in ("cat", "plan"):
        cid = ref.get("cat") or (db.get_vip_plan(ref.get("plan")) or {}).get("category_id") or 0
        text, kb = _agdm_cat_view(tid, int(cid))
    else:
        text, kb = _agdm_opts_view(tid, ref.get("kind"))
    await message.answer(f"✅ ذخیره شد: {int(raw)}٪\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("deleteagent_"))
async def delete_agent(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    tid = callback.data.replace("deleteagent_", "")
    db.remove_agent(tid)
    await callback.answer("✅ نماینده حذف شد.")
    await admin_agency_list(callback)


# --- 👤 باز کردن صفحه‌ی یک نماینده — دقیقاً همان صفحه‌ی «مدیریت کاربر»
# (بخش کاربران)، به‌علاوه‌ی دکمه‌ی «💯 تغییر درصد تخفیف نمایندگی» ---
@router.callback_query(F.data.startswith("agentopen_"))
async def admin_agent_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    tid = callback.data.replace("agentopen_", "")
    agent = db.get_agent(tid)
    if agent is None:
        await callback.answer("❌ این نماینده یافت نشد.", show_alert=True)
        return

    user = db.get_user(tid)
    if user is None:
        await callback.message.edit_text(
            f"🤝 نماینده 🆔 {tid} | 💯 {agent['vip_discount_percent']}٪\n\n"
            "⚠️ این آیدی هنوز ربات را /start نزده؛ اطلاعات کاربری‌ای برایش ثبت نشده.",
            reply_markup=admin_agent_actions_keyboard(tid),
        )
        await callback.answer()
        return

    text = _user_detail_text(user) + f"\n\n🤝 درصد تخفیف نمایندگی (VIP): {agent['vip_discount_percent']}٪"
    await callback.message.edit_text(text, reply_markup=admin_agent_actions_keyboard(tid))
    await callback.answer()


@router.callback_query(F.data.startswith("editagentpercent_"))
async def admin_agent_edit_percent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    tid = callback.data.replace("editagentpercent_", "")
    if db.get_agent(tid) is None:
        await callback.answer("❌ این نماینده یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_agent_id=tid)
    await state.set_state(AdminStates.waiting_agent_edit_percent)
    await callback.message.edit_text("💯 درصد تخفیف جدید (بین ۱ تا ۱۰۰) را ارسال کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_agent_edit_percent)
async def admin_agent_edit_percent_apply(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit() or not (1 <= int(clean_numeric_id(message.text)) <= 100):
        await message.answer("❌ لطفاً یک عدد بین ۱ تا ۱۰۰ وارد کنید:")
        return
    data = await state.get_data()
    tid = data.get("edit_agent_id")
    agent = db.get_agent(tid) if tid else None
    if agent is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.", reply_markup=admin_agency_menu(db.get_all_agents()))
        await state.clear()
        return

    percent = int(clean_numeric_id(message.text))
    db.add_agent(tid, percent, agent.get("note"))
    await message.answer(
        f"✅ درصد تخفیف نماینده به‌روزر��انی شد:\n🆔 {tid}\n💯 {percent}٪",
        reply_markup=admin_agent_actions_keyboard(tid),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Premium/Custom Emoji for dynamic VIP category/plan names
# ---------------------------------------------------------------------------
def _save_dynamic_premium_emoji(text: str, message: types.Message):
    """Persist the custom emoji attached to a dynamic category/plan name."""
    try:
        for e in serialize_message_entities(getattr(message, "entities", None)):
            if e.get("type") == "custom_emoji" and e.get("custom_emoji_id"):
                db.set_button_custom_emoji_id("text:" + str(text), str(e["custom_emoji_id"]))
                return
        db.set_button_custom_emoji_id("text:" + str(text), None)
    except Exception:
        logger.exception("ذخیره Premium Emoji نام داینامیک ناموفق بود")

# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP — می‌توان هر تعداد دسته و داخل هرکدام هر تعداد پلن اضافه
# کرد؛ همه‌شان خودکار در «🛒 خرید اشتراک → 🚀 سرور VIP» برای کاربر ظاهر می‌شوند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "vipsc_toggle")
async def admin_vip_shortcut_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    on = db.get_setting("vip_shortcut", "0") == "1"
    db.set_setting("vip_shortcut", "0" if on else "1")
    await callback.answer(
        "⚡ میانبر خاموش شد." if on else
        "⚡ میانبر روشن شد: پوشه‌ها و پلن‌ها مستقیم کنار «ساخت سفارشی» و PAYG زیر متن «خرید اشتراک» نمایش داده می‌شوند.",
        show_alert=not on)
    await admin_vip_categories_list(callback)


@router.callback_query(F.data == "admin_vip_categories")
async def admin_vip_categories_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗂 پوشه‌ها و پلن‌های «🛒 خرید اشتراک»\n\n"
        "این صفحه دقیقاً همان چیزی است که کاربر با زدن «خرید اشتراک» می‌بیند: پوشه‌ها و پلن‌های بدون پوشه کنار هم.\n"
        "هر تعداد پوشه و زیرپوشه بسازید و پلن‌ها را بین آن‌ها جابجا کنید.\n"
        "برای ورود به هر پوشه روی آن بزنید 👇",
        reply_markup=admin_vip_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "newvipcat")
async def admin_new_vip_category_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.update_data(new_vip_cat_parent=None)
    await state.set_state(AdminStates.waiting_vip_category_name)
    await callback.message.edit_text("🗂 نام پوشه‌ی جدید را ارسال کنید (مثلاً «💎 حجم بالای ویژه»):")
    await callback.answer()


@router.message(AdminStates.waiting_vip_category_name)
async def admin_new_vip_category_apply(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    data = await state.get_data()
    parent_key = data.get("new_vip_cat_parent")
    parent = db.get_vip_category(parent_key) if parent_key else None
    cat = db.create_vip_category(name, parent["id"] if parent else None)
    _save_dynamic_premium_emoji(name, message)
    await message.answer(
        f"✅ پوشه‌ی «{name}» ساخته شد!\n\nحالا می‌توانید از داخل همین پوشه، زیرپوشه یا پلن اضافه کنید 👇",
        reply_markup=admin_vip_category_detail_keyboard(cat["key"]),
    )
    await state.clear()


@router.callback_query(F.data.startswith("vipcatdesc_"))
async def admin_vip_category_edit_description(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("vipcatdesc_", "")
    if category_key == "root":
        _rb = db.get_vip_root_bucket(create=True)
        category_key = _rb["key"] if _rb else category_key
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(vip_category_key=category_key)
    await state.set_state(AdminStates.waiting_vip_category_description)
    cur_text, cur_ents = db.get_vip_category_description(cat)
    import rich_message as _rich
    _cur_rich = _rich.load("cat", cat["id"])
    await callback.message.answer(
        "🧾 برای «Rich Message» (جدول، تیتر، لیست و...): همان پیام ریچ را برای من فوروارد/ارسال کن، "
        "یا خط اول را «html:» (یا «md:») بنویس و بعدش کد HTML/Markdown ریچ را بگذار.\n\n"
        "📝 توضیحات این پوشه را بفرست.\n\n"
        "این متن «بالای منوی داخل پوشه» (بالای زیرپوشه‌ها و پلن‌ها) به کاربر نمایش داده می‌شود.\n"
        "ایموجی پریمیوم و ریچ‌تکست (بولد، ایتالیک، زیرخط، نقل‌قول، اسپویلر، کد، لینک) همان‌طور که بفرستی حفظ می‌شود.\n\n"
        "برای حذف توضیحات، فقط یک خط تیره «-» بفرست.",
        parse_mode=None,
    )
    if _cur_rich:
        await callback.message.answer("توضیحات فعلی (Rich Message) 👇", parse_mode=None)
        try:
            await _rich.send(callback.bot, callback.message.chat.id, _cur_rich)
        except Exception:
            await callback.message.answer("⚠️ نمایش Rich Message فعلی ممکن نشد.", parse_mode=None)
    elif cur_text:
        import rich_registry
        await callback.message.answer("توضیحات فعلی 👇", parse_mode=None)
        try:
            await callback.message.answer(
                cur_text, entities=rich_registry.to_message_entities(cur_ents) or None, parse_mode=None,
            )
        except Exception:
            await callback.message.answer(cur_text, parse_mode=None)
    else:
        await callback.message.answer("توضیحات فعلی: (خالی)", parse_mode=None)
    await callback.answer()


@router.message(AdminStates.waiting_vip_category_description)
async def admin_vip_category_save_description(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    category_key = data.get("vip_category_key")
    cat = db.get_vip_category(category_key) if category_key else None
    if cat is None:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از منوی دسته‌بندی‌ها شروع کن.")
        return
    import rich_message as _rich
    _payload = _rich.extract(message)
    if _payload:
        try:
            _rich.save("cat", cat["id"], _payload)
            await _rich.send(message.bot, message.chat.id, _payload)  # پیش‌نمایش؛ اگر تلگرام نپذیرد خطا می‌دهد
        except Exception as exc:
            _rich.clear("cat", cat["id"])
            await message.answer(f"❌ این Rich Message پذیرفته نشد و ذخیره نشد:\n{exc}\n\nدوباره بفرست یا از «html:» استفاده کن:")
            return
        db.update_vip_category_description(cat["id"], None, None)
        await state.clear()
        await message.answer("✅ توضیحات پوشه (Rich Message) ذخیره شد و بالای منوی داخل پوشه نمایش داده می‌شود.",
                             reply_markup=admin_vip_category_detail_keyboard(category_key))
        return
    if not message.text:
        await message.answer("❌ فقط متن یا Rich Message بفرست (می‌تونه ایموجی پریمیوم و ریچ‌تکست داشته باشه)؛ یا «-» برای حذف:")
        return
    import rich_registry
    # بعد از trim، offset entityها هم با همان برش جابه‌جا می‌شود تا ایموجی/فرمت سرجایش بماند.
    text, ents = rich_registry.strip_with_entities(message.text, message.entities)
    if not text or text == "-":
        db.update_vip_category_description(cat["id"], None, None)
        _rich.clear("cat", cat["id"])
        await state.clear()
        await message.answer("✅ توضیحات پوشه حذف شد.", reply_markup=admin_vip_category_detail_keyboard(category_key))
        return
    if len(text) > 3000:
        await message.answer(f"❌ توضیحات خیلی طولانی است ({len(text)} کاراکتر). حداکثر ۳۰۰۰ کاراکتر؛ کوتاه‌ترش کن و دوباره بفرست:")
        return
    db.update_vip_category_description(cat["id"], text, ents)
    _rich.clear("cat", cat["id"])
    await state.clear()
    n_prem = sum(1 for e in ents if e.get("type") == "custom_emoji")
    n_rich = len(ents) - n_prem
    await message.answer(
        "✅ توضیحات پوشه ذخیره شد و بالای منوی داخل پوشه نمایش داده می‌شود."
        + (f"\n💎 ایموجی پریمیوم: {n_prem}" if n_prem else "")
        + (f"\n✍️ قالب‌بندی (ریچ‌تکست): {n_rich} مورد" if n_rich else ""),
        reply_markup=admin_vip_category_detail_keyboard(category_key),
        parse_mode=None,
    )


@router.callback_query(F.data.startswith("vipplanlimit_"))
async def admin_vip_plan_edit_user_limit(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipplanlimit_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    await state.update_data(vip_plan_key=plan_key)
    await state.set_state(AdminStates.waiting_vip_plan_edit_user_limit)
    await callback.message.answer(
        f"👥 سقف دستگاه همزمان (HWID) این پلن را به‌صورت عدد بفرست.\n"
        f"عدد ۰ یعنی نامحدود.\n\nمقدار فعلی: {plan.get('user_limit') or 0}"
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_plan_edit_user_limit)
async def admin_vip_plan_save_user_limit(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    plan_key = data.get("vip_plan_key")
    plan = db.get_vip_plan(plan_key) if plan_key else None
    if plan is None:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از منوی پلن‌ها شروع کن.")
        return
    try:
        limit = int((message.text or "").strip())
        if limit < 0:
            raise ValueError
    except Exception:
        await message.answer("❌ فقط یک عدد صحیح ۰ یا بیشتر بفرست (۰ = نامحدود):")
        return
    db.update_vip_plan(plan_key, user_limit=limit)
    await state.clear()
    await message.answer(
        f"✅ سقف دستگاه این پلن روی {'نامحدود' if limit == 0 else limit} تنظیم شد.",
        reply_markup=admin_vip_plan_detail_keyboard(plan_key),
    )


@router.callback_query(F.data.startswith("admincat_"))
async def admin_vip_category_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("admincat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    if db.is_vip_root_bucket(cat):
        await admin_vip_categories_list(callback)
        return
    plans = db.get_vip_plans(cat["id"])
    subs = db.get_child_vip_categories(cat["id"])
    path = " › ".join(c["name"] for c in db.get_vip_category_path(cat["id"]))
    _desc_text, _ = db.get_vip_category_description(cat)
    _desc_text = _desc_text or (__import__("rich_message").load("cat", cat["id"]) and "rich")
    text = (
        f"🗂 {path}\n\n📁 زیرپوشه‌ها: {len(subs)}\n📦 تعداد پلن: {len(plans)}\n"
        f"📝 توضیحات پوشه: {'✅ ثبت شده' if _desc_text else 'ندارد'}\n\n"
        "برای ورود به هر زیرپوشه یا مدیریت هر پلن روی آن بزنید 👇"
    )
    await callback.message.edit_text(text, reply_markup=admin_vip_category_detail_keyboard(category_key))
    await callback.answer()


@router.callback_query(F.data.startswith("delvipcat_"))
async def admin_delete_vip_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("delvipcat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    parent = db.get_vip_category(int(cat["parent_id"])) if cat.get("parent_id") is not None else None
    ok = db.delete_vip_category(cat["id"])
    if not ok:
        if db.get_child_vip_categories(cat["id"]):
            await callback.answer("❌ این پوشه زیرپوشه دارد؛ اول آن‌ها را حذف یا منتقل کنید.", show_alert=True)
        else:
            await callback.answer("❌ این پوشه پلن دارد؛ اول پلن‌هایش را حذف یا به پوشه‌ی دیگر منتقل کنید.", show_alert=True)
        return
    await callback.answer("✅ پوشه حذف شد.")
    if parent and not db.is_vip_root_bucket(parent):
        await admin_vip_category_detail(callback.model_copy(update={"data": f"admincat_{parent['key']}"}))
    else:
        await admin_vip_categories_list(callback)


# --- افزودن پلن جدید به یک دسته (۴ مرحله: نام / قیمت / حجم گیگ / مدت روز) ---
@router.callback_query(F.data.startswith("newvipplan_"))
async def admin_new_vip_plan_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("newvipplan_", "")
    if category_key == "root":
        # پلن بدون پوشه: مستقیم زیر «خرید اشتراک» قرار می‌گیرد.
        category_key = db.get_vip_root_bucket()["key"]
    if db.get_vip_category(category_key) is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(new_vip_plan_category=category_key)
    await state.set_state(AdminStates.waiting_vip_plan_name)
    await callback.message.edit_text(
        "📦 افزودن پلن جدید — مرحله ۱ از ۵\n\n✏️ نام پلن را ارسال کنید (مثلاً «۲۰۰ گیگ | کاربر و زمان ∞»):"
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_plan_name)
async def admin_new_vip_plan_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    _save_dynamic_premium_emoji(name, message)
    await state.update_data(new_vip_plan_name=name)
    await state.set_state(AdminStates.waiting_vip_plan_price)
    await message.answer("📦 مرحله ۲ از ۵\n\n💰 قیمت را به تومان (فقط عدد) ارسال کنید:")


@router.message(AdminStates.waiting_vip_plan_price)
async def admin_new_vip_plan_price(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    await state.update_data(new_vip_plan_price=int(clean_numeric_id(message.text)))
    await state.set_state(AdminStates.waiting_vip_plan_gb)
    await message.answer(
        "📦 مرحله ۳ از ۵\n\n🗜 حجم را به گیگابایت ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):"
    )


@router.message(AdminStates.waiting_vip_plan_gb)
async def admin_new_vip_plan_gb(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    await state.update_data(new_vip_plan_gb=int(clean_numeric_id(message.text)))
    await state.set_state(AdminStates.waiting_vip_plan_days)
    await message.answer("📦 مرحله ۴ از ۵\n\n⏳ مدت را به روز ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):")


@router.message(AdminStates.waiting_vip_plan_days)
async def admin_new_vip_plan_days(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    await state.update_data(new_vip_plan_days=int(clean_numeric_id(message.text)))
    await state.set_state(AdminStates.waiting_vip_plan_userlimit)
    await message.answer(
        "📦 مرحله ۵ از ۵\n\n"
        "👥 سقف کاربر همزمان (HWID Limit) را به عدد ارسال کنید (۰ تا ۱۰، ۰ = نامحدود):"
    )


@router.message(AdminStates.waiting_vip_plan_userlimit)
async def admin_new_vip_plan_userlimit(message: types.Message, state: FSMContext):
    user_limit = parse_int_in_range((message.text or "").strip(), 0, 10)
    if user_limit is None:
        await message.answer("❌ عددی بین ۰ تا ۱۰ ارسال کنید (۰ = نامحدود):")
        return
    data = await state.get_data()
    category_key = data.get("new_vip_plan_category")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.")
        await state.clear()
        return

    days = data.get("new_vip_plan_days", 0)
    name = data.get("new_vip_plan_name")
    price = data.get("new_vip_plan_price")
    volume_gb = data.get("new_vip_plan_gb", 0)

    plan_key = db.add_vip_plan(cat["id"], name, price, days=days, volume_gb=volume_gb, user_limit=user_limit)
    # Emoji was captured from the name message; store it under the stable dynamic-name key.
    # (Calling again is harmless and keeps the flow resilient to FSM serialization.)
    await message.answer(
        f"✅ پلن جد��د اضافه شد! 🎉\n\n📦 {name}\n💰 {price:,} تومان\n🗜 "
        f"{volume_gb if volume_gb else 'نامحدود'} گیگ\n⏳ {days if days else 'نامحدود'} روز\n👥 {'نامحدود' if user_limit == 0 else f'{user_limit} کاربر'} همزمان",
        reply_markup=admin_vip_category_detail_keyboard(category_key),
    )
    await state.clear()
    # 🔌 بلافاصله بعد از ساخت پلن: لیست پنل‌ها برای نگاشت (قابل رد کردن)
    try:
        from handlers.panel_admin import send_vip_map_picker
        _new_plan = db.get_vip_plan(plan_key)
        if _new_plan:
            await send_vip_map_picker(
                message, "p", _new_plan["id"],
                intro=f"🔌 می‌خوای همین الان «{name}» را به یک پنل نگاشت کنی؟ (می‌توانی رد کنی و بعداً از داخل پلن یا کل پوشه انجام بدهی)",
                skip_label="⏭ رد کردن",
            )
    except Exception:
        logger.exception("نمایش لیست پنل برای نگاشت پلن جدید ناموفق بود")


# --- مشاهده/ویرایش/حذف یک پلن مشخص ---
@router.callback_query(F.data.startswith("vipplan_"))
async def admin_vip_plan_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_vip_category(plan["category_id"])
    text = (
        f"📦 {plan['name']}\n\n"
        f"💰 قیمت: {plan['price']:,} تومان\n"
        f"🗜 حجم: {plan['volume_gb'] if plan['volume_gb'] else 'نامحدود'} گیگ\n"
        f"⏳ مدت: {plan['days'] if plan['days'] else 'نامحدود'} روز\n"
        f"👥 سقف کاربر: {'نامحدود' if plan.get('user_limit', 0) == 0 else str(plan['user_limit']) + ' کاربر'}\n"
        f"🗂 محل: {(' › '.join(c['name'] for c in db.get_vip_category_path(cat['id'])) if not db.is_vip_root_bucket(cat) else 'بدون پوشه (مستقیم زیر خرید اشتراک)') if cat else '-'}"
    )
    await callback.message.edit_text(
        text, reply_markup=admin_vip_plan_detail_keyboard(plan_key, cat["key"] if cat else "")
    )
    await callback.answer()


def _vip_plan_edit_starter(field_state, prompt: str, prefix: str):
    async def handler(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        plan_key = callback.data.replace(prefix, "")
        if db.get_vip_plan(plan_key) is None:
            await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
            return
        await state.update_data(edit_vip_plan_key=plan_key)
        await state.set_state(field_state)
        await callback.message.edit_text(prompt)
        await callback.answer()
    return handler


router.callback_query(F.data.startswith("vipplanname_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_name, "✏️ نام جدید پلن را ارسال کنید:", "vipplanname_")
)
router.callback_query(F.data.startswith("vipplanprice_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_price, "💰 قیمت جدید را به تومان (فقط عدد) ارسال کنید:", "vipplanprice_")
)
router.callback_query(F.data.startswith("vipplangb_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_gb, "🗜 حجم جدید را به گیگ (فقط عدد، 0 = نامحدود) ارسال کنید:", "vipplangb_")
)
router.callback_query(F.data.startswith("vipplandays_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_days, "⏳ مدت جدید را به روز (فقط عدد، 0 = نامحدود) ارسال کنید:", "vipplandays_")
)
router.callback_query(F.data.startswith("vipplanuserlimit_"))(
    _vip_plan_edit_starter(
        AdminStates.waiting_vip_plan_edit_userlimit,
        "👥 سقف کاربر همزمان جدید را ارسال کنید (۰ تا ۱۰، ۰ = نامحدود):",
        "vipplanuserlimit_"
    )
)


async def _after_vip_plan_edit(message: types.Message, state: FSMContext, plan_key: str, success_text: str):
    plan = db.get_vip_plan(plan_key)
    cat = db.get_vip_category(plan["category_id"]) if plan else None
    await message.answer(
        success_text,
        reply_markup=admin_vip_plan_detail_keyboard(plan_key, cat["key"] if cat else ""),
    )
    await state.clear()


@router.message(AdminStates.waiting_vip_plan_edit_name)
async def admin_vip_plan_edit_name_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    new_name = (message.text or "").strip()
    if not plan_key or not new_name:
        await message.answer("❌ متن نامعتبر است؛ دوباره ارسال کنید:")
        return
    old_plan = db.get_vip_plan(plan_key)
    old_name = (old_plan or {}).get("name")
    db.update_vip_plan(plan_key, name=new_name)
    if old_name and old_name != new_name:
        db.set_button_custom_emoji_id("text:" + str(old_name), None)
    _save_dynamic_premium_emoji(new_name, message)
    await _after_vip_plan_edit(message, state, plan_key, f"✅ نام پلن به‌روزرسانی شد:\n📦 {new_name}")


@router.message(AdminStates.waiting_vip_plan_edit_price)
async def admin_vip_plan_edit_price_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    price = int(clean_numeric_id(message.text))
    db.update_vip_plan(plan_key, price=price)
    await _after_vip_plan_edit(message, state, plan_key, f"✅ قیمت پلن به‌روزرسانی شد:\n💰 {price:,} تومان")


@router.message(AdminStates.waiting_vip_plan_edit_gb)
async def admin_vip_plan_edit_gb_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    volume_gb = int(clean_numeric_id(message.text))
    db.update_vip_plan(plan_key, volume_gb=volume_gb)
    await _after_vip_plan_edit(
        message, state, plan_key, f"✅ حجم پلن به‌روزرسانی شد:\n🗜 {volume_gb if volume_gb else 'نامحدود'} گیگ"
    )


@router.message(AdminStates.waiting_vip_plan_edit_days)
async def admin_vip_plan_edit_days_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    days = int(clean_numeric_id(message.text))
    db.update_vip_plan(plan_key, days=days)
    await _after_vip_plan_edit(
        message, state, plan_key, f"✅ مدت پلن به‌روزرسانی شد:\n⏳ {days if days else 'نامحدود'} روز"
    )


@router.message(AdminStates.waiting_vip_plan_edit_userlimit)
async def admin_vip_plan_edit_userlimit_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    user_limit = parse_int_in_range((message.text or "").strip(), 0, 10)
    if user_limit is None:
        await message.answer("❌ عددی بین ۰ تا ۱۰ ارسال کنید (۰ = نامحدود):")
        return
    db.update_vip_plan(plan_key, user_limit=user_limit)
    await _after_vip_plan_edit(
        message, state, plan_key,
        f"✅ سقف کاربر همزمان پلن به‌روزرسانی شد:\n👥 {'نامحدود' if user_limit == 0 else f'{user_limit} کاربر'}"
    )


@router.callback_query(F.data.startswith("delvipplan_"))
async def admin_delete_vip_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("delvipplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_vip_category(plan["category_id"])
    db.delete_vip_plan(plan_key)
    await callback.answer("✅ پلن حذف شد.")
    if cat:
        await admin_vip_category_detail(callback.model_copy(update={"data": f"admincat_{cat['key']}"}))
    else:
        await admin_vip_categories_list(callback)


# --- ↕️ تغییر ترتیب دسته‌بندی‌ها/پلن‌های VIP ---
@router.callback_query(F.data.startswith("movevipcat_"))
async def admin_move_vip_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movevipcat_", "")
    category_key, _, direction = raw.rpartition("_")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    db.move_vip_category(cat["id"], direction)
    await callback.answer()
    parent = db.get_vip_category(int(cat["parent_id"])) if cat.get("parent_id") is not None else None
    if parent and not db.is_vip_root_bucket(parent):
        await admin_vip_category_detail(callback.model_copy(update={"data": f"admincat_{parent['key']}"}))
    else:
        await admin_vip_categories_list(callback)


@router.callback_query(F.data.startswith("movevipplan_"))
async def admin_move_vip_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movevipplan_", "")
    plan_key, _, direction = raw.rpartition("_")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    db.move_vip_plan(plan["id"], direction)
    await callback.answer()
    cat = db.get_vip_category(plan["category_id"])
    if cat:
        await admin_vip_category_detail(callback.model_copy(update={"data": f"admincat_{cat['key']}"}))


# 📢 پیام همگانی
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # 🆕 بند ۱۸: اول مخاطب انتخاب می‌شود، بعد متن پیام گرفته می‌شود.
    await callback.message.edit_text(
        "📢 پیام همگانی\n\nاین پیام برای چه کسانی ارسال شود؟",
        reply_markup=admin_broadcast_audience_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bcaud_"))
async def admin_broadcast_pick_audience(callback: types.CallbackQuery, state: FSMContext):
    if not _admin_perm(callback.from_user.id, "broadcast"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    audience = callback.data.replace("bcaud_", "")
    from database import AUDIENCE_LABELS
    if audience not in AUDIENCE_LABELS:
        await callback.answer("❌ گزینه نامعتبر.", show_alert=True)
        return
    label = AUDIENCE_LABELS[audience]
    count = len(db.get_broadcastable_users(audience))
    await state.update_data(broadcast_audience=audience)
    await state.set_state(UserStates.waiting_broadcast)
    await callback.message.edit_text(
        f"📢 مخاطب: {label} ({count:,} نفر)\n\n"
        "حالا پیامی که می‌خواهید ارسال شود را بفرستید.\n"
        "هر نوع پیامی پشتیبانی می‌شود (متن، عکس، فیلم، پیام فوروارد‌شده از کانال) و "
        "دقیقاً با همان قالب اصلی خودش — شامل ایموجی پرمیوم و دکمه‌های زیرش — ارسال می‌شود.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


_BC_STATS = {"copy_fail": 0, "last": "", "emoji": 0, "kind": "", "src": 0, "after": 0, "strip": 0}
_BC_ORIGIN: dict = {}


def _count_custom_emoji(m) -> int:
    ents = list(getattr(m, "entities", None) or []) + list(getattr(m, "caption_entities", None) or [])
    return sum(1 for e in ents if getattr(e, "type", "") == "custom_emoji")


async def _send_broadcast_rich(bot, target_chat_id: int, source: types.Message):
    """پیام همگانی را با مسیر nativeِ Telegram کپی می‌کند.

    مهم: برای حفظ ۱۰۰٪ فرمت پیام، مخصوصاً Custom/Premium Emoji و دکمه‌های
    Inline، اول از copyMessage خود Telegram استفاده می‌کنیم. بازسازی دستی
    متن + entities برای پیام‌های فورواردشده از کانال می‌تواند در بعضی موارد
    باعث از دست رفتن Custom Emoji شود. اگر copyMessage به هر دلیل برای یک
    نوع پیام قابل استفاده نباشد، مسیر مستقیم قبلی به‌عنوان fallback اجرا می‌شود.
    """
    # copyMessage خود Telegram entityهای اصلی (custom_emoji) را عیناً حفظ می‌کند.
    # ترتیب تلاش: ۱) با دکمه‌های اصلی پیام  ۲) بدون reply_markup (برای پیام‌های
    # فورواردشده از کانال که دکمه‌ی نامعتبر/مخصوص ربات دیگر دارند و باعث رد شدن
    # کل کپی می‌شد و پیام با ایموجی معمولی ارسال می‌گشت)  ۳) fallback دستی.
    _first_exc = None
    # اگر پیام از یک کانال فوروارد شده و ربات به همان کانال دسترسی دارد، از خودِ پستِ اصلی کپی می‌گیریم؛
    # این تنها راهی است که ایموجی پرمیومِ اصلی با قطعیت حفظ می‌شود (فورواردِ ادمین بدون پرمیوم ممکن است entityها را از دست بدهد).
    _fo = getattr(source, "forward_origin", None)
    _okey = None
    if _fo is not None and getattr(_fo, "type", "") == "channel" and getattr(_fo, "chat", None) is not None:
        _okey = (_fo.chat.id, _fo.message_id)
        if _BC_ORIGIN.get(_okey) is not False:
            try:
                copied = await bot.copy_message(chat_id=int(target_chat_id), from_chat_id=_fo.chat.id, message_id=_fo.message_id)
                _BC_ORIGIN[_okey] = True
                if source.reply_markup:
                    try:
                        await bot.edit_message_reply_markup(chat_id=int(target_chat_id), message_id=copied.message_id, reply_markup=source.reply_markup)
                    except (TelegramForbiddenError, TelegramRetryAfter):
                        raise
                    except Exception:
                        logger.warning("broadcast: دکمه روی کپی پست اصلی نشست", exc_info=True)
                return copied
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception:
                _BC_ORIGIN[_okey] = False  # ربات به کانال دسترسی ندارد؛ برای بقیه‌ی گیرنده‌ها دیگر امتحان نمی‌شود
    try:
        # ۱) همیشه اول «بدون reply_markup» کپی می‌کنیم؛ این دقیقاً همان مسیری است که ایموجی پرمیوم را کامل
        #    نگه می‌دارد. (قبلاً وقتی زیر پیام فورواردی دکمه بود، کپی همراه دکمه انجام می‌شد و ایموجی‌ها عادی می‌شدند.)
        copied = await bot.copy_message(chat_id=int(target_chat_id), from_chat_id=source.chat.id, message_id=source.message_id)
    except (TelegramForbiddenError, TelegramRetryAfter):
        raise
    except Exception as exc:
        _first_exc = exc
        copied = None
        # یک‌بار دیگر امتحان (خطای گذرای شبکه/تلگرام) تا بی‌دلیل به ارسال دستی نیفتد
        try:
            await asyncio.sleep(0.7)
            copied = await bot.copy_message(chat_id=int(target_chat_id), from_chat_id=source.chat.id, message_id=source.message_id)
            _first_exc = None
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception as exc2:
            _first_exc = exc2
            copied = None
    if copied is not None:
        # ۲) دکمه‌های زیر پیام را (اگر بود) جدا روی همان پیامِ کپی‌شده می‌گذاریم؛ شکست این مرحله پیام را خراب نمی‌کند.
        if source.reply_markup:
            try:
                _r = await bot.edit_message_reply_markup(
                    chat_id=int(target_chat_id), message_id=copied.message_id, reply_markup=source.reply_markup,
                )
                # تشخیص: اگر بعد از گذاشتن دکمه ایموجی پرمیوم پیام کم شده، همان گیرنده را با «کپی + دکمه در یک درخواست» دوباره بفرست
                _src_n = _count_custom_emoji(source)
                if _src_n and isinstance(_r, types.Message):
                    _after = _count_custom_emoji(_r)
                    _BC_STATS["src"] = _src_n
                    _BC_STATS["after"] = _after
                    if _after < _src_n:
                        _BC_STATS["strip"] += 1
                        try:
                            await bot.delete_message(int(target_chat_id), copied.message_id)
                        except Exception:
                            pass
                        return await bot.copy_message(
                            chat_id=int(target_chat_id), from_chat_id=source.chat.id, message_id=source.message_id,
                            reply_markup=source.reply_markup,
                        )
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception:
                logger.warning("broadcast: افزودن دکمه به پیام کپی‌شده ناموفق بود", exc_info=True)
        return copied
    _BC_STATS["copy_fail"] += 1
    _BC_STATS["last"] = str(_first_exc)[:160]
    logger.warning("broadcast copy_message failed (%s); falling back to manual send", _first_exc)
    # اگر پیام فورواردی بود و دستی نمی‌توان ایموجی پرمیوم را حفظ کرد، فوروارد واقعی
    # (بدون هدر نیست ولی ایموجی حفظ می‌شود) آخرین راه است.
    markup = None if _first_exc is not None and source.reply_markup is not None else source.reply_markup
    if source.text is not None:
        return await bot.send_message(
            int(target_chat_id), source.text, entities=source.entities or None,
            parse_mode=None, reply_markup=markup, _skip_auto_text=True,
        )
    if source.photo:
        return await bot.send_photo(
            int(target_chat_id), source.photo[-1].file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
            _skip_auto_text=True,
        )
    if source.video:
        return await bot.send_video(
            int(target_chat_id), source.video.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
            _skip_auto_text=True,
        )
    if source.animation:
        return await bot.send_animation(
            int(target_chat_id), source.animation.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
            _skip_auto_text=True,
        )
    if source.document:
        return await bot.send_document(
            int(target_chat_id), source.document.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
            _skip_auto_text=True,
        )
    if source.audio:
        return await bot.send_audio(
            int(target_chat_id), source.audio.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
            _skip_auto_text=True,
        )
    if source.voice:
        return await bot.send_voice(
            int(target_chat_id), source.voice.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
            _skip_auto_text=True,
        )
    if source.sticker:
        return await bot.send_sticker(int(target_chat_id), source.sticker.file_id, reply_markup=markup)
    return await bot.copy_message(
        chat_id=int(target_chat_id),
        from_chat_id=source.chat.id,
        message_id=source.message_id,
        reply_markup=markup,
    )


@router.message(UserStates.waiting_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    # 🐛 فیکس: قبلاً این هندلر فقط برای ADMIN_ID (ادمین اصلی) فیلتر شده بود، در حالی که
    # دکمه‌ی «📢 پیام ��مگانی» (چه از منوی پایین صفحه، چه از پنل ادمین) به ادمین‌های فرعی
    # دارای مجوز broadcast هم اجازه‌ی ورود به همین state را می‌داد. در نتیجه وقتی ادمین
    # فرعی متن پیام همگانی را ارسال می‌کرد، هیچ هندلری آن پیام را نمی‌گرفت (فیلتر رد می‌شد)
    # و کاربر بدون هیچ پاسخی می‌ماند. حالا مجوز را ��وباره همینجا (به‌جای فیلتر ADMIN_ID) چک می‌کنیم.
    if not _admin_perm(message.from_user.id, "broadcast"):
        return

    data = await state.get_data()
    audience = data.get("broadcast_audience", "all")

    # 📨 ریچ‌مسیج (بند ۵): copy_message پیام را عیناً همان‌طور که هست کپی
    # می‌کند — متن، فرمت‌بندی، ایموجی پرمیوم و حتی دکمه‌های شیشه‌ای زیرش —
    # بدون هدر «فورواردشده از». این دقیقاً همان رفتاری است که قبلاً داشتید و
    # اینجا دست‌نخورده حفظ شده؛ فقط انتخاب مخاطب به آن اضافه شده است.
    # همچنین get_broadcastable_users کسانی که ربات را بلاک کرده‌اند از قبل حذف
    # می‌کند تا وقت ارسال روی مقصدهای قطعاً شکست‌خورده تلف نشود.
    users = db.get_broadcastable_users(audience, respect_prefs=True)
    from database import AUDIENCE_LABELS as _AL
    audience_label = _AL.get(audience, audience)

    status_msg = await message.answer(f"📢 در حال ارسال به {len(users):,} کاربر ({audience_label})...")
    _BC_STATS["copy_fail"] = 0
    _BC_STATS["last"] = ""
    _BC_ORIGIN.clear()
    _BC_STATS.update(src=0, after=0, strip=0)
    _ents = list(message.entities or []) + list(message.caption_entities or [])
    _n_emoji = sum(1 for e in _ents if getattr(e, "type", "") == "custom_emoji")

    # 🚀 بهینه‌سازی سرعت: به‌جای ارسال یکی‌یکی و توقف ثابت بین هر پیام (که برای یک گروه بزرگ کاربر خیلی کند می‌شود)، حالا تا سقف مجاز تلگرام (حدوداً ۲۵-۳۰ پیام در ثانیه) به‌صورت هم‌زمان ارسال می‌شوند (دهها برابر سریع‌تر از حالت قبلی که یکی‌یکی با تاخیر ۵۰ میلی‌ثانیه ارسال می‌شد).
    semaphore = asyncio.Semaphore(25)
    lock = asyncio.Lock()
    counters = {"sent": 0, "failed": 0, "done": 0}

    async def _send_one(u):
        async with semaphore:
            try:
                await send_notification_sticker(message.bot, int(u["telegram_id"]), "notif_broadcast")
                await _send_broadcast_rich(message.bot, int(u["telegram_id"]), message)
                ok = True
            except TelegramForbiddenError:
                # کاربر ربات را بلاک کرده؛ علامت بزن تا دفعه‌ی بعد اصلاً امتحان نشود.
                try:
                    db.mark_bot_blocked(u["telegram_id"])
                except Exception:
                    pass
                ok = False
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await _send_broadcast_rich(message.bot, int(u["telegram_id"]), message)
                    ok = True
                except Exception:
                    ok = False
            except Exception:
                ok = False
        async with lock:
            counters["done"] += 1
            if ok:
                counters["sent"] += 1
            else:
                counters["failed"] += 1
            if counters["done"] % 200 == 0:
                try:
                    await status_msg.edit_text(
                        f"📢 در حال ارسال... ({counters['done']}/{len(users)}) ✅ {counters['sent']} | ❌ {counters['failed']}"
                    )
                except Exception:
                    pass

    await asyncio.gather(*(_send_one(u) for u in users))
    sent, failed = counters["sent"], counters["failed"]

    _note = ""
    if _BC_STATS["copy_fail"]:
        _note = (f"\n\n⚠️ کپی مستقیم تلگرام برای {_BC_STATS['copy_fail']} گیرنده خطا داد و ارسال دستی انجام شد "
                 f"(ممکن است ایموجی پرمیوم عادی شده باشد):\n{_BC_STATS['last']}")
    if _BC_STATS["strip"]:
        _note += (f"\n\n🔎 تشخیص: بعد از گذاشتن دکمه، ایموجی پرمیوم پیام از {_BC_STATS['src']} به {_BC_STATS['after']} رسید؛ "
                  f"برای {_BC_STATS['strip']} گیرنده پیام با «کپی + دکمه در یک درخواست» دوباره ارسال شد.")
    elif _BC_STATS["src"]:
        _note += f"\n\n🔎 تشخیص: ایموجی پرمیوم پیام {_BC_STATS['src']} و بعد از ارسال {_BC_STATS['after']} (تلگرام سالم نگه داشت)."
    await status_msg.edit_text(f"✅ ارسال شد به {sent:,} نفر ({audience_label}). ناموفق: {failed:,} نفر." + _note)
    await state.clear()


# ---------------------------------------------------------------------------
# 🎁 هدیه همگانی (بند ۴) — افزودن یک مبلغ به کیف‌پول همه‌ی کاربران با یک کلیک
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_gift_all")
async def admin_gift_all_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🎁 هدیه هدف‌مند\n\nاین هدیه برای چه گروهی از کاربران باشد؟",
        reply_markup=admin_gift_audience_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("giftaud_"))
async def admin_gift_pick_audience(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    audience = callback.data.replace("giftaud_", "")
    from database import AUDIENCE_LABELS
    if audience not in AUDIENCE_LABELS:
        await callback.answer("❌ گزینه نامعتبر.", show_alert=True)
        return
    label = AUDIENCE_LABELS[audience]
    count = len(db.get_broadcastable_users(audience))
    await state.update_data(gift_audience=audience)
    await state.set_state(AdminStates.waiting_gift_all_amount)
    await callback.message.edit_text(
        f"🎁 مخاطب هدیه: {label} ({count:,} نفر)\n\nحالا مبلغ هدیه را فقط به تومان بفرست.\nمثال: 50000",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_gift_all_amount)
async def admin_gift_all_amount(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        amount = int((message.text or "").replace(",", "").replace("٬", "").strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ فقط یک عدد مثبت بفرست:")
        return
    data = await state.get_data()
    audience = data.get("gift_audience", "all")
    await state.update_data(gift_amount=amount)
    from database import AUDIENCE_LABELS
    label = AUDIENCE_LABELS.get(audience, audience)
    count = len(db.get_broadcastable_users(audience))
    await message.answer(
        f"🎁 تأیید هدیه\n\n"
        f"💰 مبلغ: {amount:,} تومان\n"
        f"👥 مخاطب: {label} ({count:,} نفر)\n\n"
        "آیا مطمئن هستید؟",
        reply_markup=admin_gift_all_confirm_keyboard(amount, audience),
    )
@router.callback_query(F.data.startswith("giftall_do_"))
async def admin_gift_all_confirm_legacy(callback: types.CallbackQuery):
    """Legacy handler for old giftall_do_<amount> callback (backwards compat) —
    همان مسیر هدیه‌ی هدف‌مند (واریز + اعلان کاربر + استیکر) را اجرا می‌کند."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    amount = int(callback.data.replace("giftall_do_", ""))
    await admin_gift_targeted_confirm(callback.model_copy(update={"data": f"giftdo_{amount}_all"}))


@router.callback_query(F.data.startswith("giftdo_"))
async def admin_gift_targeted_confirm(callback: types.CallbackQuery):
    """تأیید هدیه هدف‌مند — callback: giftdo_<amount>_<audience>."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    parts = callback.data.replace("giftdo_", "").split("_", 1)
    if len(parts) != 2:
        await callback.answer("❌ داده نامعتبر", show_alert=True)
        return
    amount = int(parts[0])
    audience = parts[1]
    from database import AUDIENCE_LABELS
    label = AUDIENCE_LABELS.get(audience, audience)
    await callback.message.edit_text(f"⏳ در حال واریز {amount:,} تومان به {label}...")
    try:
        targets = db.get_broadcastable_users(audience, respect_prefs=True)
        count = db.gift_all_users(amount, description="🎁 هدیه ادمین", audience=audience)
    except Exception:
        logger.exception("هدیه هدف‌مند ناموفق")
        await callback.message.edit_text("❌ واریز هدیه ناموفق بود؛ هیچ مبلغی واریز نشد.")
        await callback.answer()
        return
    await callback.message.edit_text(
        f"✅ {amount:,} تومان به {count:,} کاربر ({label}) واریز شد.\n\n📨 در حال اطلاع‌رسانی به کاربران..."
    )
    await callback.answer()
    sent = 0
    for u in (targets if notif_settings.enabled("gift_received") else []):
        try:
            await send_notification_sticker(callback.bot, int(u["telegram_id"]), "notif_gift")
            await callback.bot.send_message(
                int(u["telegram_id"]),
                user_text("notif_gift_received", amount=amount),
                parse_mode=None,
            )
            sent += 1
        except TelegramForbiddenError:
            try:
                db.mark_bot_blocked(u["telegram_id"])
            except Exception:
                pass
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
        except Exception:
            pass
        await asyncio.sleep(0.04)
    try:
        await callback.message.edit_text(
            f"✅ {amount:,} تومان به {count:,} کاربر ({label}) واریز شد.\n📨 اطلاع‌رسانی برای {sent:,} نفر ارسال شد."
        )
    except Exception:
        pass


# dummy old confirm block replaced above
async def _admin_gift_all_confirm_block(callback, amount):
    await callback.message.edit_text("⏳ در حال واریز هدیه به کیف پول کاربران...")
    try:
        count = db.gift_all_users(amount)
    except Exception:
        logger.exception("هدیه همگانی ناموفق بود")
        await callback.message.edit_text("❌ واریز هدیه ناموفق بود؛ هیچ مبلغی واریز نشد.")
        await callback.answer()
        return
    await callback.message.edit_text(f"✅ {amount:,} تومان به کیف پول {count:,} کاربر اضافه شد.\n\n📨 در حال ارسال اطلاع‌رسانی به کاربران...")

    # 🔔 اطلاع‌رسانی شخصی به هر کاربری که هدیه دریافت کرده است.
    # هدیه در DB ب��ای تمام کاربران غیرمسدود ثبت می‌شود؛ برای ارسال پیام فقط
    # کاربرانی را می‌گیریم که ربات را بلاک نکرده‌اند، چون برای بقیه ارسال ممکن نیست.
    gift_users = db.get_broadcastable_users("all", respect_prefs=True)
    if not notif_settings.enabled("gift_received"):
        gift_users = []
    notify_semaphore = asyncio.Semaphore(25)
    notify_lock = asyncio.Lock()
    notify_counters = {"sent": 0, "failed": 0, "done": 0}

    async def _notify_gift_user(user):
        async with notify_semaphore:
            try:
                await send_notification_sticker(callback.bot, int(user["telegram_id"]), "notif_gift")
                await callback.bot.send_message(
                    int(user["telegram_id"]),
                    user_text("notif_gift_received", amount=amount),
                    parse_mode=None,
                )
                ok = True
            except TelegramForbiddenError:
                try:
                    db.mark_bot_blocked(user["telegram_id"])
                except Exception:
                    pass
                ok = False
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await callback.bot.send_message(
                        int(user["telegram_id"]),
                        f"💰 کیف پول شما به مبلغ {amount:,} تومان از سوی ادمین شارژ شد.",
                        parse_mode=None,
                    )
                    ok = True
                except Exception:
                    ok = False
            except Exception:
                ok = False

        async with notify_lock:
            notify_counters["done"] += 1
            if ok:
                notify_counters["sent"] += 1
            else:
                notify_counters["failed"] += 1

    await asyncio.gather(*(_notify_gift_user(u) for u in gift_users))
    await callback.message.edit_text(
        f"✅ {amount:,} تومان به کیف پول {count:,} کاربر اضافه شد.\n"
        f"📨 اطلاع‌رسانی ارسال شد: {notify_counters['sent']:,} نفر | "
        f"❌ ناموفق: {notify_counters['failed']:,} نفر"
    )
    await _notify_main_admin_action(callback.bot, callback.from_user, "هدیه همگانی", str(amount), f"{count} کاربر")
    await callback.answer("🎁 انجام شد.")


# ---------------------------------------------------------------------------
# 📚 مدیریت راهنما و اموزش‌ها — افزودن/ویرایش/حذف/تغییر ترتیب (متن/عکس/فیلم)
# ---------------------------------------------------------------------------
def _first_emoji_utf16_span(title: str) -> int:
    """طول UTF-16 اولین ایموجی (یک گرافم کامل با FE0F/ZWJ/پوست/keycap) ابتدای متن؛ ۰ اگر متن با ایموجی شروع نشود."""
    import unicodedata
    if not title:
        return 0
    first = title[0]
    cp = ord(first)
    nxt = title[1] if len(title) > 1 else ""
    keycap = first in "0123456789#*" and nxt in ("\ufe0f", "\u20e3")
    if not keycap and unicodedata.category(first) not in {"So", "Sk"} and not (0x1F000 <= cp <= 0x1FAFF):
        return 0
    n = 1
    while n < len(title):
        c = ord(title[n])
        if c in (0xFE0E, 0xFE0F, 0x20E3) or 0x1F3FB <= c <= 0x1F3FF or 0xE0020 <= c <= 0xE007F:
            n += 1
        elif c == 0x200D and n + 1 < len(title):
            n += 2
        else:
            break
    return len(title[:n].encode("utf-16-le")) // 2


def _guide_detail_text_and_entities(guide: dict):
    title = str(guide.get("title") or "")
    body = str(guide.get("body_text") or "")
    prefix = title
    text = prefix + (f"\n\n{body}" if body else "")
    entities = []

    try:
        emoji_id = db.get_button_custom_emoji_id("text:" + title)
        if emoji_id:
            # فقط «اولین ایموجی» عنوان (دقیقاً یک گرافم) پرمیوم می‌شود؛ قبلاً چند نماد پشت‌سرهم یک entity
            # می‌گرفتند و تلگرام ENTITY_TEXT_INVALID می‌داد (و صفحه‌ی راهنما در پنل باز نمی‌شد).
            span = _first_emoji_utf16_span(title)
            if span:
                entities.append(types.MessageEntity(type="custom_emoji", offset=0, length=span, custom_emoji_id=str(emoji_id)))
    except Exception:
        logger.exception("بازسازی Premium Emoji عنوان راهنما ناموفق بود")

    if body:
        shift = telegram_utf16_length(prefix + "\n\n")
        for entity in message_entities_from_dicts(guide.get("body_entities") or []):
            try:
                entities.append(entity.model_copy(update={"offset": int(entity.offset) + shift}))
            except Exception:
                pass
    return text, entities


async def _send_guide_detail(target: types.Message, guide_id: int):
    guide = db.get_guide(guide_id)
    if guide is None:
        await target.answer("❌ این راهنما دیگر موجود نیست.")
        return
    guides = db.get_guides()
    idx = next((i for i, g in enumerate(guides) if g["id"] == guide_id), 0)
    caption, entities = _guide_detail_text_and_entities(guide)
    kb = admin_guide_detail_keyboard(guide_id, idx, len(guides))
    import rich_message as _rich
    _gr = _rich.load("guide", guide_id)
    if _gr:
        try:
            await _rich.send(target.bot, target.chat.id, _gr, reply_markup=kb)
            return
        except Exception:
            logger.exception("نمایش Rich Message راهنما ناموفق بود")
    try:
        if guide["content_type"] == "photo" and guide.get("file_id"):
            await target.answer_photo(guide["file_id"], caption=caption, caption_entities=entities or None, reply_markup=kb)
        elif guide["content_type"] == "video" and guide.get("file_id"):
            await target.answer_video(guide["file_id"], caption=caption, caption_entities=entities or None, reply_markup=kb)
        else:
            await target.answer(caption, entities=entities or None, parse_mode=None, reply_markup=kb)
    except Exception:
        # اگر تلگرام entityها را نپذیرفت، دست‌کم صفحه‌ی مدیریت بدون entity باز شود (متن ساده).
        logger.exception("نمایش راهنما با entity ناموفق بود؛ ارسال بدون entity")
        await target.answer(caption, entities=None, parse_mode=None, reply_markup=kb)

@router.callback_query(F.data == "admin_guides")
async def admin_guides_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    guides = db.get_guides()
    text = (
        f"📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}\n\n"
        "از اینجا می‌تونید راهنما/آموزش جدید اضافه کنید یا موردهای موجود را ویرایش کنید:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_guides_menu(guides))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_guides_menu(guides))
    await callback.answer()


@router.callback_query(F.data == "guidenew")
async def admin_guide_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندار��د.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_title)
    await callback.message.answer(
        "📚 عنوان راهنما/آموزش جدید را بفرستید:",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_title)
async def admin_guide_new_title(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ عنوان نمی‌تواند خالی باشد. دوباره بفرستید:")
        return
    # Premium/Custom Emoji انتخاب‌شده برای عنوان راهنما را به نام داینامیک متصل می‌کنیم
    # تا همان Emoji در دکمه‌ی راهنما نمایش داده شود و Emoji پیش‌فرضی وجود نداشته باشد.
    _save_dynamic_premium_emoji(title, message)
    _n_ce = sum(1 for e in (message.entities or []) if getattr(e, "type", "") == "custom_emoji")
    await message.answer(f"ℹ️ ایموجی پریمیوم در عنوان: {_n_ce}" + ("" if _n_ce else " (تلگرام ایموجی پریمیومی نفرستاد؛ از پک پریمیوم انتخاب کنید)"))
    await state.update_data(guide_new_title=title)
    await state.set_state(AdminStates.waiting_guide_content)
    await message.answer(
        "📝 حالا محتوای این راهنما را بفرستید (متن، عکس با کپشن، یا فیلم با کپشن):",
        reply_markup=admin_guide_cancel_keyboard(),
    )


@router.message(AdminStates.waiting_guide_content)
async def admin_guide_new_content(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    title = data.get("guide_new_title")
    if not title:
        await state.clear()
        return

    import rich_message as _rich
    _payload = _rich.extract(message)
    if _payload:
        try:
            _rich.to_input(_payload)
            await _rich.send(message.bot, message.chat.id, _payload)  # پیش‌نمایش/اعتبارسنجی تلگرام
        except Exception as exc:
            await message.answer(f"❌ این Rich Message پذیرفته نشد:\n{exc}\n\nدوباره بفرستید:")
            return
        guide = db.create_guide(title=title, content_type="text", body_text="🧾 Rich Message", file_id=None, body_entities=[])
        _rich.save("guide", guide["id"], _payload)
        await state.clear()
        await message.answer(f"✅ راهنمای «{title}» (Rich Message) اضافه شد.")
        await _send_guide_detail(message, guide["id"])
        return

    if message.photo:
        content_type, file_id, body_text = "photo", message.photo[-1].file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    elif message.video:
        content_type, file_id, body_text = "video", message.video.file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    else:
        content_type, file_id, body_text = "text", None, (message.text or "")
        body_entities = serialize_message_entities(getattr(message, "entities", None))

    if not body_text and not file_id:
        await message.answer("❌ محتوا نمی‌تواند خالی باشد. دوباره بفرستید:")
        return

    guide = db.create_guide(
        title=title, content_type=content_type, body_text=body_text or None,
        file_id=file_id, body_entities=body_entities,
    )
    await state.clear()
    await message.answer(f"✅ راهنمای «{title}» اضافه شد.")
    await _send_guide_detail(message, guide["id"])


@router.callback_query(F.data.startswith("guideadminopen_"))
async def admin_guide_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideadminopen_", ""))
    await _send_guide_detail(callback.message, guide_id)
    await callback.answer()


@router.callback_query(F.data.startswith("guidemove_"))
async def admin_guide_move(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("guidemove_", "")
    guide_id_str, _, direction = raw.rpartition("_")
    guide_id = int(guide_id_str)
    db.move_guide(guide_id, direction)
    await _send_guide_detail(callback.message, guide_id)
    await callback.answer("↕️ ترتیب به‌روزرسانی شد.")


@router.callback_query(F.data.startswith("guideeditname_"))
async def admin_guide_edit_name_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideeditname_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_edit_title)
    await state.update_data(guide_edit_id=guide_id)
    await callback.message.answer(
        f"✏️ عنوان جدید برای «{guide['title']}» را بفرستید:",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_edit_title)
async def admin_guide_edit_name_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    guide_id = data.get("guide_edit_id")
    if not guide_id:
        await state.clear()
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ عنوان نمی‌تواند خالی باشد. دوباره بفرستید:")
        return
    old_guide = db.get_guide(guide_id)
    old_title = (old_guide or {}).get("title")
    if old_title and old_title != title:
        db.set_button_custom_emoji_id("text:" + str(old_title), None)
    _save_dynamic_premium_emoji(title, message)
    db.update_guide(guide_id, title=title)
    await state.clear()
    _n_ce = sum(1 for e in (message.entities or []) if getattr(e, "type", "") == "custom_emoji")
    await message.answer("✅ عنوان به‌روزرسانی شد. ایموجی پریمیوم شناخته‌شده: " + (str(_n_ce) if _n_ce else "۰ (تلگرام ایموجی پریمیومی نفرستاد؛ از پک پریمیوم انتخاب کنید)"))
    await _send_guide_detail(message, guide_id)


@router.callback_query(F.data.startswith("guideeditcontent_"))
async def admin_guide_edit_content_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideeditcontent_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_edit_content)
    await state.update_data(guide_edit_id=guide_id)
    await callback.message.answer(
        f"📝 محتوای جدید برای «{guide['title']}» را بفرستید (متن، عکس با کپشن، یا فیلم با کپشن):",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_edit_content)
async def admin_guide_edit_content_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    guide_id = data.get("guide_edit_id")
    if not guide_id:
        await state.clear()
        return

    import rich_message as _rich
    _payload = _rich.extract(message)
    if _payload:
        try:
            _rich.to_input(_payload)
            await _rich.send(message.bot, message.chat.id, _payload)  # پیش‌نمایش/اعتبارسنجی تلگرام
        except Exception as exc:
            await message.answer(f"❌ این Rich Message پذیرفته نشد:\n{exc}\n\nدوباره بفرستید:")
            return
        db.update_guide(guide_id, content_type="text", body_text="🧾 Rich Message", file_id=None, body_entities=[])
        _rich.save("guide", guide_id, _payload)
        await state.clear()
        await message.answer("✅ محتوا (Rich Message) به‌روزرسانی شد.")
        await _send_guide_detail(message, guide_id)
        return

    if message.photo:
        content_type, file_id, body_text = "photo", message.photo[-1].file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    elif message.video:
        content_type, file_id, body_text = "video", message.video.file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    else:
        content_type, file_id, body_text = "text", None, (message.text or "")
        body_entities = serialize_message_entities(getattr(message, "entities", None))

    if not body_text and not file_id:
        await message.answer("❌ محتوا نمی‌تواند خالی باشد. دوباره بفرستید:")
        return

    _rich.clear("guide", guide_id)
    db.update_guide(
        guide_id, content_type=content_type, body_text=body_text, file_id=file_id,
        body_entities=body_entities,
    )
    await state.clear()
    await message.answer("✅ محتوا به‌روزرسانی شد.")
    await _send_guide_detail(message, guide_id)


@router.callback_query(F.data.startswith("guidedelete_"))
async def admin_guide_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guidedelete_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await callback.message.answer(
        f"❗️ آیا از حذف «{guide['title']}» مطمئن هستید؟",
        reply_markup=admin_guide_delete_confirm_keyboard(guide_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("guidedeleteconfirm_"))
async def admin_guide_delete_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guidedeleteconfirm_", ""))
    guide = db.get_guide(guide_id)
    title = guide["title"] if guide else ""
    db.delete_guide(guide_id)
    __import__("rich_message").clear("guide", guide_id)
    guides = db.get_guides()
    await callback.message.answer(
        f"✅ راهنمای «{title}» حذف شد.\n\n📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}",
        reply_markup=admin_guides_menu(guides),
    )
    await callback.answer("🗑 حذف شد.")


# ---------------------------------------------------------------------------
# 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو
# ---------------------------------------------------------------------------
_STICKER_SECTION_ORDER = list(STICKER_SECTION_LABELS.keys())


def _sticker_status(section_key: str):
    """وضعیت فعلی یک بخش را برمی‌گرداند: (is_enabled, has_custom, file_id_or_None)."""
    row = db.get_section_sticker(section_key)
    if row is None:
        return True, False, None  # پیش‌فرض: فعال، بدون سفارشی‌سازی
    return bool(row["is_enabled"]), True, row.get("file_id")


async def _send_sticker_section_detail(target: types.Message, section_key: str):
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    is_enabled, has_custom, file_id = _sticker_status(section_key)

    if is_enabled:
        status_line = "✅ فعال — سفارشی (آپلود‌شده توسط ادمین)" if has_custom else "➖ فعال — استیکر پیش‌فرض پروژه"
    else:
        status_line = "🛑 غیرفعال — هیچ استیکری نشان داده نمی‌شود"

    text = f"🎬 {label}\n\nوضعیت: {status_line}"
    kb = admin_sticker_detail_keyboard(section_key, has_custom=has_custom, is_enabled=is_enabled)

    try:
        if is_enabled:
            if file_id:
                await target.answer_sticker(file_id)
            else:
                filename = STICKER_FILES.get(section_key)
                if filename:
                    await target.answer_sticker(FSInputFile(os.path.join(STICKERS_DIR, filename)))
    except Exception:
        logger.exception("خطا در پیش‌نمایش استیکر بخش '%s'", section_key)

    await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin_stickers")
async def admin_stickers_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    sections = []
    for key in _STICKER_SECTION_ORDER:
        is_enabled, has_custom, _fid = _sticker_status(key)
        if not is_enabled:
            emoji = "🛑"
        elif has_custom:
            emoji = "✅"
        else:
            emoji = "➖"
        sections.append({"key": key, "label": STICKER_SECTION_LABELS.get(key, key), "status_emoji": emoji})
    text = (
        "🎬 مدیریت استیکرهای منو\n\n"
        "✅ = سفارشی‌شده، ➖ = پیش‌فرض پروژه، 🛑 = غیرفعال\n\n"
        "یکی از بخش‌ها رو انتخاب کن:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_stickers_menu(sections))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_stickers_menu(sections))
    await callback.answer()


@router.message(F.text == "🎬 استیکرهای منو")
async def admin_stickers_list_msg(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    sections = []
    for key in _STICKER_SECTION_ORDER:
        is_enabled, has_custom, _fid = _sticker_status(key)
        if not is_enabled:
            emoji = "🛑"
        elif has_custom:
            emoji = "✅"
        else:
            emoji = "➖"
        sections.append({"key": key, "label": STICKER_SECTION_LABELS.get(key, key), "status_emoji": emoji})
    text = (
        "🎬 مدیریت استیکرهای منو\n\n"
        "✅ = سفارشی‌شده، ➖ = پیش‌فرض پروژه، 🛑 = غیرفعال\n\n"
        "یکی از بخش‌ها رو انتخاب کن:"
    )
    await message.answer(text, reply_markup=admin_stickers_menu(sections))


@router.callback_query(F.data.startswith("stickeropen_"))
async def admin_sticker_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    section_key = callback.data.replace("stickeropen_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer()


@router.callback_query(F.data.startswith("stickerset_"))
async def admin_sticker_set_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickerset_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_sticker_upload)
    await state.update_data(sticker_section_key=section_key)
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    await callback.message.answer(
        f"📤 استیکر/فایل موردنظرت رو برای «{label}» بفرست.\n\n"
        "فقط یک استیکر متحرک (ویدیویی) معتبر تلگرام قابل قبوله؛ هر ویدیوی معمولی رو تلگرام به‌عنوان استیکر قبول نمی‌کنه.",
        reply_markup=admin_sticker_cancel_keyboard(section_key),
    )
    await callback.answer()


@router.message(AdminStates.waiting_sticker_upload)
async def admin_sticker_upload_receive(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    section_key = data.get("sticker_section_key")
    if not section_key:
        await state.clear()
        return

    file_id = None
    if message.sticker:
        file_id = message.sticker.file_id
    elif message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id

    if not file_id:
        await message.answer("❌ فقط استیکر یا فایل/ویدیوی قابل قبوله. دوباره بفرست یا انصراف بده:")
        return

    db.set_section_sticker(section_key, file_id)
    invalidate_section_sticker_cache(section_key)
    await state.clear()
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    await message.answer(f"✅ استیکر «{label}» ذخیره شد.")
    await _send_sticker_section_detail(message, section_key)


@router.callback_query(F.data.startswith("stickeroff_"))
async def admin_sticker_off(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickeroff_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.set_section_sticker_enabled(section_key, False)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("🛑 غیرفعال شد.")


@router.callback_query(F.data.startswith("stickeron_"))
async def admin_sticker_on(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickeron_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.set_section_sticker_enabled(section_key, True)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("✅ فعال شد.")


@router.callback_query(F.data.startswith("stickerreset_"))
async def admin_sticker_reset(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickerreset_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.reset_section_sticker(section_key)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("♻️ به حالت پیش‌فرض برگشت.")


# ---------------------------------------------------------------------------
# 💾 بکاپ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_backup")
async def admin_backup(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    try:
        if db.USE_TURSO:
            export_path = "/tmp/backup_export.json"
            db.export_backup_json(export_path)
            backup_file = FSInputFile(export_path, filename="backup.json")
            caption = "💾 بکاپ دیتابیس (Turso — JSON)"
        else:
            backup_file = FSInputFile(DATABASE_PATH)
            caption = "💾 بکاپ دیتابیس"
        await callback.message.answer_document(backup_file, caption=caption)
    except Exception:
        await callback.message.answer("❌ خطا در ساخت بکاپ.")
    await callback.answer()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 🔁 تنظیمات تمدید سرویس — کپی ساختار ربات
# ---------------------------------------------------------------------------
def _admin_renewal_settings(category_id, plan_key=None):
    try: cid=int(category_id or 0)
    except Exception: cid=0
    if plan_key:
        return bot_info.get_renewal_plan_settings(plan_key,cid)
    return bot_info.get_renewal_settings(cid)

def _renewal_scope_parts(target):
    if str(target).startswith("cat_"):
        try: return "cat",int(str(target)[4:]),None
        except Exception: return None,None,None
    if str(target).startswith("plan_"):
        key=str(target)[5:]; plan=db.get_vip_plan(key)
        if plan and plan.get("category_id"): return "plan",int(plan["category_id"]),key
    return None,None,None

@router.callback_query(F.data == "admin_renewal_settings")
async def admin_renewal_settings_open(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    await state.clear(); await callback.message.edit_text("🔁 تنظیمات تمدید سرویس\n\nبرای هر دسته‌بندی می‌توانی نوع تمدید، قیمت، حداقل/حداکثر و دکمه‌های انتخاب را جداگانه تنظیم کنی.",reply_markup=admin_renewal_categories_menu()); await callback.answer()

@router.callback_query(F.data == "renewsw_toggle")
async def admin_renewal_switch_toggle(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    db.set_renewal_enabled(not db.is_renewal_enabled())
    try: await callback.message.edit_reply_markup(reply_markup=admin_renewal_categories_menu())
    except Exception: pass
    await callback.answer("🟢 تمدید روشن شد." if db.is_renewal_enabled() else "🔴 تمدید در همه‌ی بخش‌ها خاموش شد.")

@router.message(F.text == "🔁 تنظیمات تمدید")
async def admin_renewal_settings_open_msg(message:types.Message,state:FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear(); await message.answer("🔁 تنظیمات تمدید سرویس\n\nیک دسته‌بندی را انتخاب کن:",reply_markup=admin_renewal_categories_menu())

@router.callback_query(F.data.startswith("renewsetcat_"))
async def admin_renewal_category(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    try: cid=int(callback.data.replace("renewsetcat_","",1))
    except Exception: await callback.answer("❌ دسته نامعتبر.",show_alert=True); return
    cat=db.get_vip_category(cid)
    if not cat: await callback.answer("❌ دسته پیدا نشد.",show_alert=True); return
    await state.clear(); await callback.message.edit_text(f"🔁 تنظیمات تمدید\n\nدسته: {cat['name']}\n\nمحدوده تنظیمات را انتخاب کن:",reply_markup=admin_renewal_scope_menu(cid)); await callback.answer()

@router.callback_query(F.data == "renewcustlist")
async def admin_renewal_custom_list(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    await state.clear()
    await callback.message.edit_text("🛠 تنظیمات تمدید سرویس سفارشی\n\nیکی از گزینه‌های سرویس سفارشی را انتخاب کن؛ قیمت و محدوده‌ی تمدید هر گزینه جداست (مقدار ثبت‌نشده از قیمت خود گزینه یا «پیش‌فرض همه‌ی سفارشی‌ها» گرفته می‌شود):",reply_markup=admin_renewal_custom_options_menu())
    await callback.answer()

@router.callback_query(F.data.startswith("renewsetscope_all_"))
async def admin_renewal_scope_all(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    cid=int(callback.data.replace("renewsetscope_all_","",1)); await state.clear(); await callback.message.edit_text("🔁 تنظیمات تمدید\n\nمحدوده: سرویس‌های سفارشی (بساز سرویس خودت)\nمقادیر ثبت‌نشده از قیمت/محدوده‌ی خود گزینه‌ی سفارشی گرفته می‌شود." if cid==bot_info.CUSTOM_RENEWAL_ID else ("🔁 تنظیمات تمدید\n\nمحدوده: گزینه‌ی سفارشی «"+str(next((o.get("name") for o in (db.get_map_options("custom_build") or []) if int(o["num"])==bot_info.custom_option_from_id(cid)),cid))+"»" if cid<=bot_info.CUSTOM_OPTION_BASE else "🔁 تنظیمات تمدید\n\nمحدوده: همه پلن‌های این دسته"),reply_markup=admin_renewal_category_menu(cid)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetscope_plan_"))
async def admin_renewal_scope_plan(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    raw=callback.data.replace("renewsetscope_plan_","",1)
    try: cid_s,key=raw.split("_",1); cid=int(cid_s)
    except Exception: await callback.answer("❌ پلن نامعتبر.",show_alert=True); return
    plan=db.get_vip_plan(key)
    if not plan: await callback.answer("❌ پلن پیدا نشد.",show_alert=True); return
    await state.clear(); await callback.message.edit_text(f"🔁 تنظیمات تمدید\n\nدسته: {(db.get_vip_category(cid) or {}).get('name',cid)}\nپلن: {plan.get('name',key)}",reply_markup=admin_renewal_plan_menu(cid,key)); await callback.answer()

@router.callback_query(F.data.regexp(r"^renewsetscope_\d+$"))
async def admin_renewal_scope_back(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    cid=int(callback.data.replace("renewsetscope_","",1)); await state.clear(); await callback.message.edit_text("🔁 تنظیمات تمدید\n\nمحدوده تنظیمات را انتخاب کن:",reply_markup=admin_renewal_scope_menu(cid)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetmode_"))
async def admin_renewal_mode(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    target=callback.data.replace("renewsetmode_","",1); scope,cid,key=_renewal_scope_parts(target)
    if not scope: await callback.answer("❌ مقصد نامعتبر.",show_alert=True); return
    await callback.message.edit_text("نوع تمدید را انتخاب کن:",reply_markup=admin_renewal_mode_menu(cid,key)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetmodeval_"))
async def admin_renewal_mode_save(callback:types.CallbackQuery,state:FSMContext):
    raw=callback.data.replace("renewsetmodeval_","",1)
    try: mode,target=raw.split("_",1)
    except Exception: await callback.answer("❌ مقدار نامعتبر.",show_alert=True); return
    scope,cid,key=_renewal_scope_parts(target)
    if mode not in ("day","gb","both") or not scope: await callback.answer("❌ مقدار نامعتبر.",show_alert=True); return
    if scope=="cat":
        bot_info.set_renewal_setting(cid,"mode",mode); bot_info.clear_renewal_plan_overrides_for_category(cid,"mode")
        kb=admin_renewal_category_menu(cid)
    else:
        bot_info.set_renewal_plan_setting(key,"mode",mode); kb=admin_renewal_plan_menu(cid,key)
    await callback.message.edit_text("✅ نوع تمدید ذخیره شد.",reply_markup=kb); await callback.answer()

@router.callback_query(F.data.startswith("renewset_"))
async def admin_renewal_numeric_start(callback:types.CallbackQuery,state:FSMContext):
    raw=callback.data.replace("renewset_","",1)
    if raw.startswith("options_"):
        parts=raw.split("_",2); unit=parts[1]; target=parts[2]
        scope,cid,key=_renewal_scope_parts(target)
        if unit not in ("day","gb") or not scope: await callback.answer("❌ گزینه نامعتبر.",show_alert=True); return
        await state.update_data(renewal_target=target,renewal_field=f"{unit}_options",renewal_unit=unit)
        await state.set_state(AdminStates.waiting_renewal_setting_value)
        st=_admin_renewal_settings(cid,key); await callback.message.answer(f"دکمه‌های {('روز' if unit=='day' else 'گیگ')} را با کاما جدا کن.\nمثال: {('30,60,90' if unit=='day' else '10,20,50')}\nمقدار فعلی: {st.get(f'{unit}_options')}"); await callback.answer(); return
    parts=raw.split("_",2)
    if len(parts)!=3: await callback.answer("❌ گزینه نامعتبر.",show_alert=True); return
    field,unit,target=parts; scope,cid,key=_renewal_scope_parts(target)
    if field not in ("price","min","max") or unit not in ("day","gb") or not scope: await callback.answer("❌ گزینه نامعتبر.",show_alert=True); return
    st=_admin_renewal_settings(cid,key); cur=st.get(f"{field}_{unit}")
    await state.update_data(renewal_target=target,renewal_field=f"{field}_{unit}",renewal_unit=unit)
    await state.set_state(AdminStates.waiting_renewal_setting_value)
    label={"price":"قیمت","min":"حداقل","max":"حداکثر"}[field]; suffix="تومان" if field=="price" else ("روز" if unit=="day" else "گیگ")
    shown="نامحدود" if field=="max" and not int(cur or 0) else cur
    await callback.message.answer(f"{label} {suffix} را وارد کن.\nمقدار فعلی: {shown}"); await callback.answer()

@router.message(AdminStates.waiting_renewal_setting_value)
async def admin_renewal_setting_save(message:types.Message,state:FSMContext):
    if not _is_admin(message.from_user.id): return
    data=await state.get_data(); target=data.get("renewal_target"); field=data.get("renewal_field")
    scope,cid,key=_renewal_scope_parts(target or "")
    if not scope or not field: await state.clear(); await message.answer("❌ خطای داخلی تنظیمات تمدید."); return
    value=(message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹","0123456789"))
    try:
        if field.endswith("_options"):
            nums=[]
            for x in value.replace("،",",").split(","):
                x=x.strip()
                if x: nums.append(int(x))
            nums=list(dict.fromkeys(nums))
            unit=field.split("_")[0]; st=_admin_renewal_settings(cid,key); mn=int(st.get(f"min_{unit}") or 1); mx=int(st.get(f"max_{unit}") or 0)
            if not nums or any(n<mn or (mx and n>mx) for n in nums): raise ValueError
            raw=",".join(map(str,nums))
        else:
            number=int(value.replace(",","").replace("٬","").strip())
            if number<0 or (field.startswith("min") and number<1): raise ValueError
            raw=number
        if scope=="cat":
            bot_info.set_renewal_setting(cid,field,raw); bot_info.clear_renewal_plan_overrides_for_category(cid,field); kb=admin_renewal_category_menu(cid)
        else:
            bot_info.set_renewal_plan_setting(key,field,raw); kb=admin_renewal_plan_menu(cid,key)
        await state.clear(); await message.answer("✅ تنظیم تمدید ذخیره شد.",reply_markup=kb)
    except Exception:
        await message.answer("❌ مقدار نامعتبر است. حداقل/حداکثر و گزینه‌ها باید با تنظیمات فعلی سازگار باشند.")

# ℹ️ اطلاعات ربات (قالب فروشی)
# ---------------------------------------------------------------------------
def _botinfo_status_text():
    values = bot_info.all_values()
    labels = bot_info.labels()
    lines = ["ℹ️ اطلاعات ربات\n", "برای ویرایش روی هر کدام بزنید:\n"]
    for key, label in labels.items():
        val = values.get(key) or "—"
        lines.append(f"• {label}: {val}")
    channels = bot_info.get_required_channels()
    lines.append(f"\n• کانال‌های اجباری: {len(channels)} عدد")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_botinfo")
async def admin_botinfo_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text(_botinfo_status_text(), reply_markup=admin_botinfo_menu())
    except TelegramBadRequest:
        await callback.message.answer(_botinfo_status_text(), reply_markup=admin_botinfo_menu())
    await callback.answer()


@router.message(F.text == "ℹ️ اطلاعات ربات")
async def admin_botinfo_open_msg(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(_botinfo_status_text(), reply_markup=admin_botinfo_menu())



@router.callback_query(F.data == "botinfo_open")
async def admin_botinfo_open_alias(callback: types.CallbackQuery, state: FSMContext):
    """بازگشت استاندارد از زیرمنوهای اطلاعات ربات."""
    await admin_botinfo_open(callback, state)

def _cfgname_root_text() -> str:
    return (
        "🏷 نامگذاری سرویس\n\n"
        "نام کاربری سرویس روی پنل = پیشوند + _ + پسوند تصادفی\n"
        f"مثال: {(bot_info.get('config_name_prefix') or 'tg').strip() or 'tg'}_{bot_info.random_suffix()}\n\n"
        "🏷 پیشوند: نامگذاری پیش‌فرض یا نامگذاری توسط کاربر (و تنظیم «انتخاب خودکار»)\n"
        f"🔢 پسوند: {bot_info.get_suffix_len()} کاراکتر، {bot_info.SUFFIX_KIND_LABELS[bot_info.get_suffix_kind()]}"
    )


def _cfgname_menu_text() -> str:
    mode = bot_info.get_config_name_mode()
    prefix = (bot_info.get("config_name_prefix") or "tg").strip() or "tg"
    if mode == "user":
        active = "👤 نامگذاری توسط کاربر"
        example = "کاربر بعد از انتخاب پلن نام دلخواهش را می‌فرستد؛ مثال: ali_48213 (همان یوزرنیم روی پنل ساخته می‌شود)"
        if bot_info.get_auto_name_mode() == "prefix":
            auto = f"با پیشوند پیش‌فرض ({prefix}_{bot_info.random_suffix()})"
        else:
            auto = "ساخت از یوزرنیم/نام تلگرام کاربر (مثلاً ali_48213)"
        extra = f"\n🤖 دکمه‌ی «انتخاب خودکار» برای کاربر: {auto}"
    else:
        active = f"🏷 نامگذاری پیش‌فرض ({prefix})"
        example = f"یوزرنیم روی پنل به‌صورت {prefix}_{bot_info.random_suffix()} ساخته می‌شود"
        extra = ""
    return (
        "🏷 پیشوند سرویس‌ها\n\n"
        f"✅ حالت فعال: {active}\n"
        f"ℹ️ {example}{extra}\n\n"
        "با زدن روی هر گزینه همان فعال و دیگری غیرفعال می‌شود.\n"
        "🤝 نماینده‌ها همیشه فقط پیشوند پیش‌فرض یا پیشوند اختصاصی خودشان را دارند."
    )


def _cfgsuffix_menu_text() -> str:
    return (
        "🔢 پسوند سرویس‌ها\n\n"
        f"تعداد کاراکتر: {bot_info.get_suffix_len()}\n"
        f"نوع: {bot_info.SUFFIX_KIND_LABELS[bot_info.get_suffix_kind()]}\n"
        f"نمونه: {bot_info.random_suffix()}\n\n"
        "پسوند همیشه فعال است (پیش‌فرض ۸ رقم عدد)."
    )


async def _edit_or_answer(callback: types.CallbackQuery, text: str, markup):
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


async def _show_cfgname_menu(callback: types.CallbackQuery):
    markup = admin_cfgname_menu(
        bot_info.get_config_name_mode(), (bot_info.get("config_name_prefix") or "tg").strip() or "tg"
    )
    await _edit_or_answer(callback, _cfgname_menu_text(), markup)


async def _show_cfgsuffix_menu(callback: types.CallbackQuery):
    await _edit_or_answer(callback, _cfgsuffix_menu_text(), admin_cfgsuffix_menu())


@router.callback_query(F.data == "botinfo_cfgname")
async def admin_cfgname_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await _edit_or_answer(callback, _cfgname_root_text(), admin_cfgname_root_menu())
    await callback.answer()


@router.callback_query(F.data == "botinfo_pfx")
async def admin_cfgname_prefix_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await _show_cfgname_menu(callback)
    await callback.answer()


@router.callback_query(F.data == "botinfo_sfx")
async def admin_cfgname_suffix_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await _show_cfgsuffix_menu(callback)
    await callback.answer()


@router.callback_query(F.data.in_({"botinfo_autoname_username", "botinfo_autoname_prefix"}))
async def admin_cfgname_auto_set(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    mode = "prefix" if callback.data.endswith("_prefix") else "username"
    bot_info.set_auto_name_mode(mode)
    await _show_cfgname_menu(callback)
    await callback.answer("✅ انتخاب خودکار: " + ("با پیشوند پیش‌فرض" if mode == "prefix" else "از یوزرنیم کاربر"))


@router.callback_query(F.data.in_({"botinfo_cfgname_default", "botinfo_cfgname_user"}))
async def admin_cfgname_set(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    mode = "user" if callback.data.endswith("_user") else "default"
    bot_info.set_config_name_mode(mode)
    await _show_cfgname_menu(callback)
    if mode == "user":
        await callback.answer("✅ نامگذاری توسط کاربر فعال شد.")
    else:
        prefix = (bot_info.get("config_name_prefix") or "tg").strip() or "tg"
        await callback.answer(f"✅ نامگذاری پیش‌فرض فعال شد ({prefix}). برای تغییر آن «✏️ تغییر پیشوند پیش‌فرض» را بزنید.")


@router.callback_query(F.data.startswith("botinfo_sfx_kind_"))
async def admin_cfg_suffix_kind(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    kind = callback.data.replace("botinfo_sfx_kind_", "")
    if kind not in bot_info.SUFFIX_KINDS:
        await callback.answer("❌ نامعتبر.", show_alert=True)
        return
    bot_info.set_suffix_kind(kind)
    await _show_cfgsuffix_menu(callback)
    await callback.answer(f"✅ پسوند: {bot_info.SUFFIX_KIND_LABELS[kind]}")


@router.callback_query(F.data == "botinfo_sfx_len")
async def admin_cfg_suffix_len_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_cfg_suffix_len)
    await callback.answer()
    await callback.message.answer(
        f"🔢 تعداد کاراکتر پسوند بعد از نام کانفیگ را بفرستید (عدد {bot_info.SUFFIX_MIN} تا {bot_info.SUFFIX_MAX}، مثلاً 6 یا 8).\n"
        f"مقدار فعلی: {bot_info.get_suffix_len()}\n\nبرای انصراف /start را بزنید."
    )


@router.message(AdminStates.waiting_cfg_suffix_len, F.text)
async def admin_cfg_suffix_len_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not raw.isdigit() or not (bot_info.SUFFIX_MIN <= int(raw) <= bot_info.SUFFIX_MAX):
        await message.answer(f"❌ یک عدد بین {bot_info.SUFFIX_MIN} تا {bot_info.SUFFIX_MAX} بفرستید:")
        return
    bot_info.set_suffix_len(int(raw))
    await state.clear()
    await message.answer(
        f"✅ تعداد کاراکتر پسوند روی {int(raw)} تنظیم شد.\n\n" + _cfgsuffix_menu_text(),
        reply_markup=admin_cfgsuffix_menu(),
    )


def _referral_back(callback: types.CallbackQuery) -> str:
    """مقصد دکمه‌ی بازگشت را از همان صفحه‌ای که ادمین از آن آمده می‌خواند."""
    try:
        last = callback.message.reply_markup.inline_keyboard[-1][-1].callback_data
        if last in ("botinfo_open", "admin_back"):
            return last
    except Exception:
        pass
    return "admin_back"


@router.callback_query(F.data == "admin_referral_settings")
async def admin_referral_settings_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    settings = db.get_referral_settings()
    await callback.message.edit_text(_referral_settings_text(settings), reply_markup=admin_referral_settings_menu(settings))
    await callback.answer()


def _referral_settings_text(settings: dict) -> str:
    return (
        "🎁 تنظیمات رفرال\n\n"
        f"🔘 سیستم رفرال: {'روشن' if settings.get('enabled', True) else 'خاموش (دعوت دوستان و پاداش/جریمه غیرفعال است)'}\n"
        f"💰 مبلغ پاداش: {settings['reward_amount']:,} تومان\n"
        f"📦 حداقل حجم: {settings['min_volume_gb']} گیگ\n"
        f"📏 شرط حداقل حجم: {'روشن' if settings['min_volume_enabled'] else 'خاموش'}\n"
        f"💳 شرط خرید پولی: {'روشن' if settings['paid_purchase_required'] else 'خاموش'}\n"
        f"🔒 قفل پاداش با خروج از کانال: {'روشن' if settings.get('channel_leave_lock_enabled', True) else 'خاموش'}\n"
        f"🚨 سیستم جریمه: {'روشن' if settings.get('channel_leave_penalty_enabled', False) else 'خاموش'}\n"
        f"💸 جریمه کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان\n"
        f"📉 جریمه سرویس: {settings.get('penalty_service_gb', 0)} گیگ"
    )


@router.callback_query(F.data == "botinfo_referral")
async def admin_botinfo_referral_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    settings = db.get_referral_settings()
    text = (
        "🎁 مدیریت رفرال\n\n"
        f"🔘 سیستم رفرال: {'روشن' if settings.get('enabled', True) else 'خاموش (دعوت دوستان و پاداش/جریمه غیرفعال است)'}\n"
        f"💰 مبلغ پاداش: {settings['reward_amount']:,} تومان\n"
        f"📦 حداقل حجم: {settings['min_volume_gb']} گیگ\n"
        f"📏 شرط حداقل حجم: {'روشن' if settings['min_volume_enabled'] else 'خاموش'}\n"
        f"💳 شرط خرید پولی: {'روشن' if settings['paid_purchase_required'] else 'خاموش'}\n"
        f"🔒 قفل پاداش با خروج از کانال: {'روشن' if settings.get('channel_leave_lock_enabled', True) else 'خ��موش'}\n"
        f"🚨 سیستم جریمه: {'روشن' if settings.get('channel_leave_penalty_enabled', False) else 'خاموش'}\n"
        f"💸 جریمه کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان\n"
        f"📉 جریمه سرویس: {settings.get('penalty_service_gb', 0)} گیگ"
    )
    await callback.message.edit_text(text, reply_markup=admin_botinfo_referral_menu(settings))
    await callback.answer()


@router.callback_query(F.data == "botinfo_wallet")
async def admin_botinfo_wallet_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    settings = db.get_wallet_settings()
    text = (
        "💳 مدیریت شارژ کیف‌پول\n\n"
        f"📉 حداقل شارژ: {settings['min_topup']:,} تومان\n"
        f"📈 حداکثر شارژ: {settings['max_topup']:,} تومان\n"
        f"🔘 مبالغ پیشنهادی: {' / '.join(f'{a:,}' for a in settings['quick_amounts'])}"
    )
    await callback.message.edit_text(text, reply_markup=admin_botinfo_wallet_menu(settings))
    await callback.answer()


@router.callback_query(F.data.startswith("refset_toggle_"))
async def admin_referral_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("refset_toggle_", "")
    settings = db.get_referral_settings()
    mapping = {
        "enabled": "enabled",
        "min_volume": "min_volume_enabled",
        "paid_purchase": "paid_purchase_required",
        "channel_leave_lock": "channel_leave_lock_enabled",
        "channel_leave_penalty": "channel_leave_penalty_enabled",
    }
    setting_key = mapping.get(key)
    if not setting_key:
        await callback.answer("❌ تنظیم نامعتبر است.", show_alert=True)
        return
    back = _referral_back(callback)
    db.set_referral_setting(setting_key, not bool(settings[setting_key]))
    settings = db.get_referral_settings()
    await callback.message.edit_text(_referral_settings_text(settings), reply_markup=admin_referral_settings_menu(settings, back))
    await callback.answer("✅ تنظیم ذخیره شد.")


@router.callback_query(F.data.startswith("refset_edit_"))
async def admin_referral_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("refset_edit_", "")
    prompts = {
        "min_volume": ("min_volume_gb", "📦 حداقل حجم لازم برای آزاد شدن پاداش را به گیگ وارد کنید (عدد مثبت):"),
        "reward": ("reward_amount", "💰 مبلغ پاداش هر رفرال را به تومان وارد کنید:"),
        "penalty_wallet": ("penalty_wallet_amount", "💸 مبلغ جریمه را به تومان وارد کنید (فقط از کیف‌پول رفرالی کسر می‌شود؛ اگر کافی نبود از حجم سرویس رفرالی) (۰ برای بدون جریمه):"),
        "penalty_gb": ("penalty_service_gb", "📉 مقدار جریمه از سرویس را به گیگ وارد کنید (۰ برای بدون جریمه):"),
    }
    item = prompts.get(key)
    if not item:
        await callback.answer("❌ تنظیم نامعتبر است.", show_alert=True)
        return
    setting_key, prompt = item
    settings = db.get_referral_settings()
    await state.update_data(referral_setting_key=setting_key, referral_back=_referral_back(callback))
    await state.set_state(AdminStates.waiting_referral_setting_value)
    await callback.message.edit_text(f"{prompt}\n\nمقدار فعلی: {settings.get(setting_key, 0)}", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_referral_setting_value)
async def admin_referral_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("referral_setting_key")
    raw = clean_numeric_id(message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(raw)
    if key in {"min_volume_gb", "reward_amount"} and value <= 0:
        await message.answer("❌ مقدار باید بزرگ‌تر از صفر باشد:")
        return
    try:
        db.set_referral_setting(key, value)
    except Exception as e:
        await message.answer(f"❌ ذخیره تنظیمات ناموفق بود: {e}")
        return
    back = data.get("referral_back") or "admin_back"
    await state.clear()
    settings = db.get_referral_settings()
    await message.answer("✅ تنظیم رفرال ذخیره شد.\n\n" + _referral_settings_text(settings), reply_markup=admin_referral_settings_menu(settings, back))


@router.callback_query(F.data.startswith("wset_edit_"))
async def admin_wallet_setting_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("wset_edit_", "")
    prompts = {
        "min_topup": "📉 حداقل مبلغ شارژ را به تومان وارد کنید:",
        "max_topup": "📈 حداکثر مبلغ شارژ را به تومان وارد کنید:",
        "quick_amounts": "🔘 مبالغ پیشنهادی را با کاما/فاصله جدا کنید؛ مثال: 50000,100000,200000",
        "low_balance": "🪫 حد هشدار موجودی کم را به تومان وارد کنید (۰ = غیرفعال). وقتی موجودی کاربر از این مبلغ به پایین‌تر برسد، اعلان «کم‌شدن موجودی» (با دکمه‌ی شارژ کیف پول) برایش ارسال می‌شود. متن اعلان از «ویرایش متن‌ها» و روشن/خاموش آن از «تنظیمات اعلان‌ها» قابل تغییر است.",
        "gift_percent": "🎁 درصد هدیه‌ی شارژ را وارد کنید (۰ تا ۱۰۰؛ ۰ = بدون هدیه). مثال: اگر ۱۰ باشد با شارژ ۱۰۰٬۰۰۰ تومان، ۱۰٬۰۰۰ تومان هدیه می‌گیرد.",
    }
    if key not in prompts:
        await callback.answer("❌ تنظیم نامعتبر است.", show_alert=True)
        return
    settings = db.get_wallet_settings()
    current = settings[key]
    if isinstance(current, list):
        current = ", ".join(str(x) for x in current)
    await state.update_data(wallet_setting_key=key)
    await state.set_state(AdminStates.waiting_wallet_setting_value)
    await callback.message.edit_text(f"{prompts[key]}\n\nمقدار فعلی: {current}", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_wallet_setting_value)
async def admin_wallet_setting_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("wallet_setting_key")
    raw = (message.text or "").strip()
    try:
        if key == "quick_amounts":
            parts = [x for x in re.split(r"[\s,،]+", clean_numeric_id(raw)) if x]
            if not parts or not all(x.isdigit() for x in parts):
                raise ValueError("فقط اعداد معتبر وارد کنید")
            values = [int(x) for x in parts if int(x) > 0]
            db.set_wallet_setting("quick_amounts", values)
        elif key == "gift_percent":
            pct = float(clean_numeric_id(raw).replace("٫", ".").replace("/", ".") or "nan")
            if not 0 <= pct <= 100:
                raise ValueError("عدد باید بین ۰ و ۱۰۰ باشد")
            _pm().update_wallet(gift_percent=pct)
        elif key == "low_balance":
            value = int(clean_numeric_id(raw) or "nan")
            if value < 0:
                raise ValueError("مبلغ نمی‌تواند منفی باشد")
            db.set_wallet_setting("low_balance", value)
        elif key in {"min_topup", "max_topup"}:
            value = int(clean_numeric_id(raw))
            if value <= 0:
                raise ValueError("مبلغ باید بزرگ‌تر از صفر باشد")
            current = db.get_wallet_settings()
            if key == "min_topup" and value > int(current["max_topup"]):
                raise ValueError("حداقل مبلغ نمی‌تواند از حداکثر بیشتر باشد")
            if key == "max_topup" and value < int(current["min_topup"]):
                raise ValueError("حداکثر مبلغ نمی‌تواند از حداقل کمتر باشد")
            db.set_wallet_setting(key, value)
        else:
            raise ValueError("تنظیم نامعتبر است")
    except Exception as e:
        await message.answer(f"❌ {e}\n\nدوباره وارد کنید:")
        return
    await state.clear()
    settings = db.get_wallet_settings()
    await message.answer(
        "✅ تنظیمات کیف‌پول ذخیره شد.",
        reply_markup=admin_wallet_info_menu({"enabled": _pm().wallet.enabled, **settings}),
    )


# ---------------------------------------------------------------------------
# 📣 لینک‌های تبلیغاتی / کمپین‌ها
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_campaigns")
async def admin_campaigns_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    campaigns = db.list_campaigns()
    text = "📣 لینک‌های تبلیغاتی\n\n" + ("هنوز کمپینی ساخته نشده.\n\nیک کمپین جدید بسازید:" if not campaigns else "کمپین‌ها را برای مشاهده آمار و مدیریت انتخاب کنید:")
    await callback.message.edit_text(text, reply_markup=admin_campaigns_menu(campaigns))
    await callback.answer()


@router.callback_query(F.data == "new_campaign")
async def admin_campaign_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_campaign_code)
    await callback.message.edit_text("📣 ساخت کمپین جدید\n\nمرحله ۱ از ۲\n\nکد کوتاه کمپین را وارد کنید (فقط حروف انگلیسی، عدد و _):", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_campaign_code)
async def admin_campaign_code_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    code = db.normalize_campaign_code(message.text or "")
    if not code:
        await message.answer("❌ کد کمپین نامعتبر است. فقط حروف انگلیسی، عدد و _ وارد کنید:")
        return
    if db.get_campaign_by_code(code):
        await message.answer("❌ این کد قبلاً استفاده شده است. یک کد دیگر وارد کنید:")
        return
    await state.update_data(campaign_code=code)
    await state.set_state(AdminStates.waiting_campaign_name)
    await message.answer(f"📣 مرحله ۲ از ۲\n\nنام نمایشی کمپین «{code}» را وارد کنید:", reply_markup=admin_back_button())


@router.message(AdminStates.waiting_campaign_name)
async def admin_campaign_name_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام کمپین نمی‌تواند خالی باشد:")
        return
    data = await state.get_data()
    code = data.get("campaign_code")
    try:
        campaign_id = db.create_campaign(code, name)
    except Exception:
        await message.answer("❌ ساخت کمپین ناموفق بود؛ احتمالاً کد تکراری است.")
        return
    await state.clear()
    campaign = db.get_campaign_by_id(campaign_id)
    await _send_campaign_detail(message, campaign)


async def _send_campaign_detail(target, campaign: dict | None):
    if not campaign:
        if isinstance(target, types.CallbackQuery):
            await target.answer("❌ کمپین یافت نشد.", show_alert=True)
        else:
            await target.answer("❌ کمپین یافت نشد.")
        return
    stats = db.get_campaign_stats(campaign["id"])
    username = bot_info.get("bot_username") or "your_bot"
    link = f"https://t.me/{username}?start=c_{campaign['code']}"
    text = (
        f"📣 کمپین: {campaign['name']}\n"
        f"🔑 کد: {campaign['code']}\n"
        f"🔗 لینک: {link}\n\n"
        f"🟢 وضعیت: {'فعال' if campaign.get('is_active', 1) else 'غیرفعال'}\n"
        f"👥 ورودی: {stats['members']}\n"
        f"🛒 تبدیل به خرید: {stats['converted']}\n"
        f"💰 مجموع خرید: {stats['total_spent']:,} تومان"
    )
    markup = campaign_detail_keyboard(campaign)
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("campdetail_"))
async def admin_campaign_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campdetail_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    await _send_campaign_detail(callback, db.get_campaign_by_id(cid))


@router.callback_query(F.data.startswith("camptoggle_"))
async def admin_campaign_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("camptoggle_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    db.set_campaign_active(cid, not bool(campaign.get("is_active", 1)))
    await _send_campaign_detail(callback, db.get_campaign_by_id(cid))


@router.callback_query(F.data.startswith("campedit_"))
async def admin_campaign_rename_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campedit_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    await state.update_data(campaign_rename_id=cid)
    await state.set_state(AdminStates.waiting_campaign_rename)
    await callback.message.edit_text(f"✏️ نام جدید برای کمپین «{campaign['name']}» را ارسال کنید:", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_campaign_rename)
async def admin_campaign_rename_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    cid = data.get("campaign_rename_id")
    name = (message.text or "").strip()
    if not cid or not name:
        await message.answer("❌ نام نامعتبر است. دوباره ارسال کنید:")
        return
    db.rename_campaign(int(cid), name)
    await state.clear()
    await _send_campaign_detail(message, db.get_campaign_by_id(int(cid)))


@router.callback_query(F.data.startswith("campdelete_"))
async def admin_campaign_delete_start(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campdelete_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(f"⚠️ کمپین «{campaign['name']}» حذف شود؟\n\nکاربرانی که با این کمپین وارد شده‌اند حذف نمی‌شوند.", reply_markup=campaign_delete_confirm_keyboard(cid))
    await callback.answer()


@router.callback_query(F.data.startswith("campdeleteconfirm_"))
async def admin_campaign_delete_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campdeleteconfirm_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    name = campaign["name"]
    db.delete_campaign(cid)
    await callback.message.edit_text(f"✅ کمپین «{name}» حذف شد.", reply_markup=admin_campaigns_menu(db.list_campaigns()))
    await callback.answer("🗑 حذف شد.")

def _start_reaction_screen():
    cur = bot_info.start_reaction_label()
    text = (
        "🎭 ری‌اکشن روی پیام /start مشتری\n\n"
        f"ری‌اکشن فعلی: {cur}\n\n"
        "یکی از ایموجی‌های پایین را بزنید، یا «✏️ ارسال ایموجی دلخواه» را بزنید و یک ایموجی معمولی یا "
        "ایموجی پریمیوم (Premium Emoji) بفرستید.\n"
        "ℹ️ فقط ایموجی‌هایی که تلگرام برای ری‌اکشن مجاز می‌داند روی پیام می‌نشینند؛ اگر ایموجی‌ای پذیرفته نشود، ری‌اکشن بی‌صدا نادیده گرفته می‌شود."
    )
    quick = bot_info.QUICK_REACTIONS
    rows = []
    for i in range(0, len(quick), 4):
        rows.append([types.InlineKeyboardButton(text=("✅ " if cur == e else "") + e, callback_data=f"botinfo_react_set|{e}") for e in quick[i:i + 4]])
    rows.append([types.InlineKeyboardButton(text="✏️ ارسال ایموجی دلخواه / پریمیوم", callback_data="botinfo_react_custom")])
    rows.append([types.InlineKeyboardButton(text="❌ بدون ری‌اکشن", callback_data="botinfo_react_clear")])
    rows.append([types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open")])
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "botinfo_reaction")
async def admin_start_reaction_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    text, kb = _start_reaction_screen()
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("botinfo_react_set|"))
async def admin_start_reaction_quick(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    emoji = callback.data.split("|", 1)[1]
    if emoji not in bot_info.QUICK_REACTIONS:
        await callback.answer("❌ نامعتبر.", show_alert=True)
        return
    bot_info.set_start_reaction("emoji", emoji)
    text, kb = _start_reaction_screen()
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer("✅ ذخیره شد.")


@router.callback_query(F.data == "botinfo_react_clear")
async def admin_start_reaction_clear(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    bot_info.set_start_reaction(None)
    text, kb = _start_reaction_screen()
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer("✅ ری‌اکشن حذف شد.")


@router.callback_query(F.data == "botinfo_react_custom")
async def admin_start_reaction_custom(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_start_reaction)
    await callback.message.answer("✏️ یک پیام با فقط «یک ایموجی» بفرستید (معمولی یا پریمیوم). برای انصراف /cancel بزنید.")
    await callback.answer()


@router.message(AdminStates.waiting_start_reaction)
async def admin_start_reaction_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    custom_id = next((str(e.custom_emoji_id) for e in (message.entities or []) if e.type == "custom_emoji" and getattr(e, "custom_emoji_id", None)), None)
    if custom_id:
        bot_info.set_start_reaction("custom", custom_id)
    else:
        txt = (message.text or "").strip()
        if not txt or len(txt) > 8 or any(ch.isalnum() and ch.isascii() for ch in txt):
            await message.answer("❌ فقط یک ایموجی بفرستید.")
            return
        bot_info.set_start_reaction("emoji", txt)
    await state.clear()
    text, kb = _start_reaction_screen()
    await message.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("botinfo_edit_"))
async def admin_botinfo_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("botinfo_edit_", "")
    labels = bot_info.labels()
    if key not in labels:
        await callback.answer("❌ یافت نشد.", show_alert=True)
        return
    await state.update_data(botinfo_key=key)
    await state.set_state(AdminStates.waiting_botinfo_value)
    current = bot_info.get(key) or "—"
    await callback.message.answer(
        f"✏️ مقدار جدید برای «{labels[key]}» را بفرستید.\nمقدار فعلی: {current}",
        reply_markup=admin_botinfo_field_keyboard(key),
    )
    await callback.answer()


@router.message(AdminStates.waiting_botinfo_value)
async def admin_botinfo_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("botinfo_key")
    labels = bot_info.labels()
    if not key or key not in labels:
        await state.clear()
        await message.answer("❌ خطای داخلی. دوباره تلاش کنید.")
        return
    value = (message.text or "").strip()
    if key == "config_name_prefix":
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "", value)
        if not cleaned:
            await message.answer(
                "❌ پیشوند باید فقط از حروف/عدد انگلیسی و زیرخط (_) تشکیل شده باشد؛ دوباره وارد کن:"
            )
            return
        value = cleaned
    # متن را تا سقف واقعی Telegram Bot API می‌پذیریم. مسیر /start هنگام ارسال
    # متن ثابت پایین را هم حساب می‌کند و اگر مجموع از سقف عبور کند، امن کوتاه می‌شود.
    from utils import telegram_utf16_length
    if telegram_utf16_length(value) > TELEGRAM_TEXT_LIMIT:
        await message.answer(
            f"❌ متن از سقف تلگرام بیشتر است. سقف واقعی: {TELEGRAM_TEXT_LIMIT} واحد UTF-16. لطفاً متن کوتاه‌تری بفرست:"
        )
        return
    if key == "welcome_text":
        # Custom/Premium Emoji به‌صورت MessageEntity می‌آید؛ ذخیره‌ی صرفِ message.text
        # شناسه‌ی emoji را از بین می‌برد.
        _wvalue, _went = message_text_and_entities(message)
        bot_info.set_welcome_text_with_entities(_wvalue or value, _went)
    else:
        bot_info.set(key, value)
    await state.clear()
    if key == "config_name_prefix":
        await message.answer(
            f"✅ پیشوند پیش‌فرض روی «{value}» تنظیم شد.\n\n" + _cfgname_menu_text(),
            reply_markup=admin_cfgname_menu(bot_info.get_config_name_mode(), value),
        )
        return
    await message.answer(f"✅ «{labels[key]}» به‌روز شد.", reply_markup=admin_botinfo_menu())


@router.callback_query(F.data == "botinfo_channels")
async def admin_botinfo_channels_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    channels = bot_info.get_required_channels()
    text = "📢 کانال‌های عضویت اجباری\n\nروی هرکدام بزنید تا حذف شود." if channels else "📢 هیچ کانال اجباریثبت نشده."
    try:
        await callback.message.edit_text(text, reply_markup=admin_botinfo_channels_menu(channels))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_botinfo_channels_menu(channels))
    await callback.answer()


@router.callback_query(F.data == "botinfo_channel_add")
async def admin_botinfo_channel_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_botinfo_channel_add)
    await callback.message.answer(
        "➕ افزودن کانال اجباری\n\n"
        "مرحله ۱ از ۳\n"
        "آیدی یا یوزرنیم کانال را وارد کنید:\n"
        "مثال: -1001234567890 یا @mychannel",
        reply_markup=admin_back_button(),
    )


@router.message(AdminStates.waiting_botinfo_channel_add)
async def admin_botinfo_channel_add_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw_chat_id = clean_numeric_id((message.text or "").strip())
    if not raw_chat_id:
        await message.answer("❌ آیدی/یوزرنیم کانال خالی است. دوباره وارد کنید:")
        return
    await state.update_data(botinfo_channel_id=raw_chat_id)
    await state.set_state(AdminStates.waiting_botinfo_channel_name)
    await message.answer("مرحله ۲ از ۳\nنام نمایشی کانال را وارد کنید:")


@router.message(AdminStates.waiting_botinfo_channel_name)
async def admin_botinfo_channel_add_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمایشی نمی‌تواند خالی باشد. دوباره وارد کنید:")
        return
    await state.update_data(botinfo_channel_name=name)
    await state.set_state(AdminStates.waiting_botinfo_channel_url)
    await message.answer("مرحله ۳ از ۳\nلینک دعوت کانال را وارد کنید:\nمثال: https://t.me/mychannel")


@router.message(AdminStates.waiting_botinfo_channel_url)
async def admin_botinfo_channel_add_url(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    raw_chat_id = data.get("botinfo_channel_id", "")
    name = data.get("botinfo_channel_name", "")
    url = (message.text or "").strip()
    if not url:
        await message.answer("❌ لینک دعوت خالی است. دوباره وارد کنید:")
        return
    chat_id_input = raw_chat_id if raw_chat_id.startswith("@") or raw_chat_id.startswith("-") or raw_chat_id.lstrip("-").isdigit() else f"@{raw_chat_id.lstrip('@')}"
    try:
        chat = await message.bot.get_chat(chat_id_input)
    except Exception as e:
        await message.answer(
            "❌ ربات نتوانست این کانال را پیدا کند. ممکن است:\n"
            "• فرمت آیدی اشتباه باشد (برای کانال باید با «-100» شروع شود)\n"
            "• ربات هنوز به این کانال اضافه/عضو نشده باشد\n\n"
            f"خطای دقیق: {e}"
        )
        return
    try:
        bot_member = await message.bot.get_chat_member(chat.id, message.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.answer("⚠️ ربات باید در این کانال ادمین باشد تا عضویت کاربران را بررسی کند.")
            return
    except Exception as e:
        await message.answer(f"❌ بررسی وضعیت ربات در کانال ناموفق بود: {e}")
        return
    bot_info.add_required_channel(chat.id, name, url)
    await state.clear()
    channels = bot_info.get_required_channels()
    await message.answer(
        f"✅ کانال اضافه شد.\nآیدی واقعی کانال: {chat.id}",
        reply_markup=admin_botinfo_channels_menu(channels),
    )


@router.callback_query(F.data.startswith("botinfo_channel_del_"))
async def admin_botinfo_channel_del(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    chat_id = callback.data.replace("botinfo_channel_del_", "", 1)
    bot_info.remove_required_channel(chat_id)
    channels = bot_info.get_required_channels()
    text = "📢 کانال‌های عضویت اجباری" if channels else "📢 هیچ کانال اجباریثبت نشده."
    try:
        await callback.message.edit_text(text, reply_markup=admin_botinfo_channels_menu(channels))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_botinfo_channels_menu(channels))
    await callback.answer("✅ حذف شد.")


@router.message(F.text.in_({"📝 مدیریت متن‌های کاربر", "✏️ ویرایش متن و دکمه‌ها"}))
async def admin_texts_from_menu(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "✏️ ویرایش متن و دکمه‌ها\n\nابتدا نوع موردی که می‌خواهید ویرایش کنید را انتخاب کنید:",
        reply_markup=_ui_editor.editor_mode_keyboard(),
    )


@router.callback_query(F.data == "errlogsentryguide")
async def admin_error_logs_sentry_guide(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    text = (
        "🔗 راهنمای فعال‌سازی Sentry\n\n"
        "برای فعال‌سازی، مقدار DSN سرویس Sentry را در متغیر محیطی SENTRY_DSN قرار دهید و ربات را ری‌استارت کنید.\n\n"
        "پس از بالا آمدن ربات، خطاهای جدید در بخش «🦖 لاگ خطاها» ثبت و در صورت فعال بودن Sentry ارسال می‌شوند."
    )
    await callback.message.edit_text(text, reply_markup=admin_back_button())
    await callback.answer()


@router.callback_query(F.data == "errlog")
async def admin_error_logs_open_callback(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _open_error_logs(callback, edit=True)
    await callback.answer()


def _free_test_shortcut_button():
    from aiogram.types import InlineKeyboardButton
    on = db.get_setting("free_test_shortcut", "0") == "1"
    # تلگرام فقط primary/success/danger را برای style می‌پذیرد؛ «secondary» کل پیام را رد می‌کرد.
    return InlineKeyboardButton(
        text=("⚡ میانبر: روشن ✅" if on else "⚡ میانبر: خاموش ⛔"),
        callback_data="ft_shortcut_toggle", style=("success" if on else "danger"))


@router.callback_query(F.data == "ft_shortcut_toggle")
async def admin_free_test_shortcut_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    on = db.get_setting("free_test_shortcut", "0") == "1"
    db.set_setting("free_test_shortcut", "0" if on else "1")
    await callback.answer(
        "⚡ میانبر خاموش شد." if on else
        "⚡ میانبر روشن شد: پیام معرفی و دکمه‌ی «تست رایگان» نمایش داده نمی‌شود و تست(ها) بدون QR مستقیم در یک پیام ارسال می‌شود.",
        show_alert=not on)
    await admin_free_test_settings_open_callback(callback, state)


@router.callback_query(F.data == "admin_free_test_settings")
async def admin_free_test_settings_open_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()  # FAST
    except Exception:
        pass  # (وقتی از دکمه‌ی میانبر صدا زده می‌شود قبلاً پاسخ داده شده)
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    panels = db.get_free_test_panels()
    from keyboards import admin_free_test_panels_keyboard
    from aiogram.types import InlineKeyboardButton
    plan = db.get_effective_free_test_plan()
    price = plan.get("price", 0)
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    text = (
        "🎁 مدیریت تست رایگان\n\n"
        f"🗂 پنل‌ها: {len(panels)} عدد\n\n"
        "روی هر پنل بزنید تا مدیریت کنید."
    )
    kb = admin_free_test_panels_keyboard(panels)
    kb.inline_keyboard.insert(0, [_free_test_shortcut_button()])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.exception("نمایش تنظیمات تست رایگان ناموفق بود")
            try:
                await callback.message.answer(text, reply_markup=kb)
            except Exception:
                logger.exception("ارسال تنظیمات تست رایگان (fallback) ناموفق بود")


# ---------------------------------------------------------------------------
# 🎁 تنظیم حجم/روز پلن «تست رایگان» از پنل ادمین
# ---------------------------------------------------------------------------
# 🎁 تنظیم حجم/روز پلن «تست رایگان» از پنل ادمین
# ---------------------------------------------------------------------------
FREE_TEST_MIN_VOLUME_MB = 50
FREE_TEST_MAX_VOLUME_MB = 1024
FREE_TEST_MIN_DAYS = 1
FREE_TEST_MAX_DAYS = 7
FREE_TEST_MIN_PRICE = 0
FREE_TEST_MAX_PRICE = 2000


def _free_test_settings_text() -> str:
    plan = db.get_effective_free_test_plan()
    price = plan.get("price", 0)
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    return (
        "🎁 تنظیم پلن «تست رایگان»\n\n"
        f"مقدار فعلی: {plan['name']} — قیمت: {price_label}\n\n"
        "برای تغییر، حجم (مگابایت)، تعداد روز و قیمت (تومان) را با | جدا و ارسال کنید.\n"
        f"محدوده‌ی مجاز: حجم بین {FREE_TEST_MIN_VOLUME_MB} تا {FREE_TEST_MAX_VOLUME_MB} مگابایت، "
        f"روز بین {FREE_TEST_MIN_DAYS} تا {FREE_TEST_MAX_DAYS} روز، "
        f"قیمت بین {FREE_TEST_MIN_PRICE} (رایگان) تا {FREE_TEST_MAX_PRICE} تومان.\n"
        "مثال: 500 | 3 | 1500\n\n"
        "برای انصراف /cancel بزنید."
    )


@router.message(F.text == "🎁 تنظیم تست رایگان")
async def admin_free_test_settings_open(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        panels = db.get_free_test_panels()
        from keyboards import admin_free_test_panels_keyboard
        from aiogram.types import InlineKeyboardButton
        plan = db.get_effective_free_test_plan()
        price = plan.get("price", 0)
        price_label = "رایگان" if price == 0 else f"{price:,} تومان"
        text = (
            "🎁 مدیریت تست رایگان\n\n"
            f"🗂 پنل‌ها: {len(panels)} عدد\n\n"
            "روی هر پنل بزنید تا مدیریت کنید."
        )
        panels_kb = admin_free_test_panels_keyboard(panels)
        panels_kb.inline_keyboard.insert(0, [_free_test_shortcut_button()])
        await message.answer(text, reply_markup=panels_kb)
    except Exception:
        await state.set_state(AdminStates.waiting_free_test_settings)
        await message.answer(_free_test_settings_text())


@router.message(AdminStates.waiting_free_test_default_volume)
async def admin_free_test_default_volume(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (FREE_TEST_MIN_VOLUME_MB <= int(raw) <= FREE_TEST_MAX_VOLUME_MB):
        await message.answer(f"❌ حجم باید بین {FREE_TEST_MIN_VOLUME_MB} تا {FREE_TEST_MAX_VOLUME_MB} مگابایت باشد.")
        return
    await state.update_data(ft_default_volume=int(raw))
    await state.set_state(AdminStates.waiting_free_test_default_days)
    await message.answer("مرحله ۲ از ۳\nتعداد روز را وارد کنید:")


@router.message(AdminStates.waiting_free_test_default_days)
async def admin_free_test_default_days(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (FREE_TEST_MIN_DAYS <= int(raw) <= FREE_TEST_MAX_DAYS):
        await message.answer(f"❌ روز باید بین {FREE_TEST_MIN_DAYS} تا {FREE_TEST_MAX_DAYS} باشد.")
        return
    await state.update_data(ft_default_days=int(raw))
    await state.set_state(AdminStates.waiting_free_test_default_price)
    await message.answer("مرحله ۳ از ۳\nقیمت را به تومان وارد کنید (برای رایگان: 0):")


@router.message(AdminStates.waiting_free_test_default_price)
async def admin_free_test_default_price(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace(",", "").strip()
    if not raw.isdigit() or not (FREE_TEST_MIN_PRICE <= int(raw) <= FREE_TEST_MAX_PRICE):
        await message.answer(f"❌ قیمت باید بین {FREE_TEST_MIN_PRICE} تا {FREE_TEST_MAX_PRICE} تومان باشد.")
        return
    data = await state.get_data()
    volume_mb = int(data.get("ft_default_volume", FREE_TEST_MIN_VOLUME_MB))
    days = int(data.get("ft_default_days", FREE_TEST_MIN_DAYS))
    price = int(raw)
    try:
        db.set_free_test_override(volume_mb, days, "day", price)
    except TypeError:
        db.set_free_test_override(volume_mb, days, price)
    await state.clear()
    plan = db.get_effective_free_test_plan()
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    await message.answer(f"✅ پلن پیشفرض تست رایگان ذخیره شد: {plan['name']} — قیمت: {price_label}")


def _payg_text(s: dict) -> str:
    status = "✅ فعال" if s["enabled"] else "❌ غیرفعال"
    lines = ["⚡ تنظیمات Pay-as-you-go", "", f"وضعیت: {status}",
             f"حداقل موجودی برای ساخت سرویس: {s['min_balance']:,} تومان",
             f"عملکرد اتمام: {'🗑 حذف' if s['empty_action'] == 'delete' else '⏸️ توقف'}", "", "📋 پلن‌های PAYG (نام — قیمت هر GB):"]
    opts = db.get_map_options("payg")
    if not opts:
        lines.append("هنوز پلنی تعریف نشده. از «مدیریت پلن‌های PAYG» اضافه کن.")
    for o in opts:
        pr = db.get_map_option_cfg("payg", o["num"]).get("price_per_gb") or int(s.get("price_per_gb") or 0)
        lines.append(f"{'✅' if o['mapped'] else '⚪️'} {o['name']} — {pr:,} تومان" + ("" if o["mapped"] else " (نگاشت نشده)"))
    return "\n".join(lines)


@router.callback_query(F.data == "admin_payg")
async def admin_payg_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    s = db.get_payg_settings()
    text = _payg_text(s)
    await callback.message.edit_text(text, reply_markup=admin_payg_menu(s))
    await callback.answer()


@router.callback_query(F.data == "payg_toggle")
async def payg_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    s = db.get_payg_settings()
    db.set_payg_settings(enabled=not s["enabled"])
    s2 = db.get_payg_settings()
    status = "✅ فعال" if s2["enabled"] else "❌ غیرفعال"
    await callback.answer(f"PAYG {status}")
    text = _payg_text(s2)
    await callback.message.edit_text(text, reply_markup=admin_payg_menu(s2))


@router.callback_query(F.data == "payg_price_per_gb")
async def payg_set_price_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id): return
    await state.update_data(payg_cat_id=None, payg_plan_key=None)
    await state.set_state(AdminStates.waiting_payg_price_per_gb)
    await callback.message.edit_text("💰 قیمت هر گیگابایت (تومان) را وارد کنید:")
    await callback.answer()


@router.callback_query(F.data == "payg_min_balance")
async def payg_min_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_payg_min_balance)
    await callback.message.edit_text("💳 حداقل موجودی کیف پول (تومان) را وارد کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_payg_min_balance)
async def payg_min_balance_done(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    v = parse_int_in_range(message.text, 0, 100_000_000)
    if v is None:
        await message.answer("❌ عدد معتبر وارد کنید.")
        return
    db.set_payg_settings(min_balance=v)
    await state.clear()
    s = db.get_payg_settings()
    await message.answer(f"✅ حداقل موجودی: {v:,} تومان", reply_markup=admin_payg_menu(s))


@router.callback_query(F.data == "payg_empty_action_toggle")
async def payg_empty_action_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    s = db.get_payg_settings()
    new_action = "delete" if s["empty_action"] == "suspend" else "suspend"
    db.set_payg_settings(empty_action=new_action)
    lbl = "🗑 حذف" if new_action == "delete" else "⏸️ تعلیق"
    await callback.answer(f"عملکرد: {lbl}")
    s2 = db.get_payg_settings()
    text = _payg_text(s2)
    await callback.message.edit_text(text, reply_markup=admin_payg_menu(s2))


@router.callback_query(F.data == "payg_empty_msg")
async def payg_empty_msg_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_payg_empty_message)
    await callback.message.edit_text(
        "📝 پیام اتمام کیف را وارد کنید:\n"
        "(برای کاربر هنگام خالی شدن کیف ارسال می‌شود)"
    )
    await callback.answer()


@router.message(AdminStates.waiting_payg_empty_message)
async def payg_empty_msg_done(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    txt = (message.text or "").strip()
    db.set_payg_settings(empty_message=txt)
    await state.clear()
    s = db.get_payg_settings()
    await message.answer("✅ پیام ذخیره شد.", reply_markup=admin_payg_menu(s))


# =============================================================================
# FIX 4 – Agent plan-level selection
# =============================================================================

@router.callback_query(F.data.startswith("agent_plans_"))
async def agent_plans_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    agent_tid = callback.data.replace("agent_plans_", "")
    cats = db.get_vip_categories()
    all_plans: list = []
    for cat in cats:
        all_plans.extend(db.get_vip_plans(cat["id"]))
    selected = db.get_agent_plan_keys(agent_tid)
    await callback.message.edit_text(
        f"🎯 پلن‌های مجاز نماینده {agent_tid}\n"
        "✅ = مجاز | ◻️ = مسدود | لیست خالی = همه پلن‌ها",
        reply_markup=admin_agent_plan_select_keyboard(agent_tid, all_plans, selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("agentplan_"))
async def agent_plan_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    rest = callback.data[len("agentplan_"):]
    agent_tid, _, plan_key = rest.partition("_")
    current = list(db.get_agent_plan_keys(agent_tid))
    if plan_key in current:
        current.remove(plan_key)
    else:
        current.append(plan_key)
    db.set_agent_plan_keys(agent_tid, current)
    cats = db.get_vip_categories()
    all_plans: list = []
    for cat in cats:
        all_plans.extend(db.get_vip_plans(cat["id"]))
    selected = db.get_agent_plan_keys(agent_tid)
    await callback.message.edit_reply_markup(
        reply_markup=admin_agent_plan_select_keyboard(agent_tid, all_plans, selected)
    )
    await callback.answer("✅")


@router.callback_query(F.data.startswith("agentppct_"))
async def agent_plan_percent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id): return
    rest = callback.data[len("agentppct_"):]
    agent_tid, _, plan_key = rest.partition("_")
    agent = db.get_agent(agent_tid)
    if not agent:
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    await state.update_data(agent_pp_tid=agent_tid, agent_pp_key=plan_key)
    await state.set_state(AdminStates.waiting_agent_plan_percent)
    cur = db.get_agent_plan_percent_override(agent_tid, plan_key)
    await callback.message.answer(
        f"💯 درصد تخفیف این پلن برای نماینده {agent_tid}\n"
        f"فعلی: {cur if cur is not None else str(agent['vip_discount_percent']) + ' (پیش‌فرض نماینده)'}٪\n\n"
        "عددی بین 0 تا 100 بفرستید.\nبرای برگشت به درصد پیش‌فرض نماینده، «-» بفرستید.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_agent_plan_percent)
async def agent_plan_percent_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    data = await state.get_data()
    tid, key = data.get("agent_pp_tid"), data.get("agent_pp_key")
    txt = (message.text or "").strip()
    raw = "-" if txt == "-" else clean_numeric_id(txt).strip()
    if raw == "-":
        pct = None
    elif raw.isdigit() and 0 <= int(raw) <= 100:
        pct = int(raw)
    else:
        await message.answer("❌ عددی بین 0 تا 100 بفرستید (یا «-» برای پیش‌فرض):"); return
    db.set_agent_plan_percent(tid, key, pct)
    await state.clear()
    all_plans: list = []
    for cat in db.get_vip_categories():
        all_plans.extend(db.get_vip_plans(cat["id"]))
    await message.answer(
        "✅ ذخیره شد.",
        reply_markup=admin_agent_plan_select_keyboard(tid, all_plans, db.get_agent_plan_keys(tid)),
    )


@router.callback_query(F.data.startswith("agentplanall_"))
async def agent_plan_all(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    agent_tid = callback.data.replace("agentplanall_", "")
    db.set_agent_plan_keys(agent_tid, [])  # empty = all plans
    cats = db.get_vip_categories()
    all_plans: list = []
    for cat in cats:
        all_plans.extend(db.get_vip_plans(cat["id"]))
    await callback.message.edit_reply_markup(
        reply_markup=admin_agent_plan_select_keyboard(agent_tid, all_plans, [])
    )
    await callback.answer("♾️ همه پلن‌ها انتخاب شدند")


@router.callback_query(F.data.startswith("agentplansave_"))
async def agent_plan_save(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    agent_tid = callback.data.replace("agentplansave_", "")
    keys = db.get_agent_plan_keys(agent_tid)
    count = len(keys) if keys else "همه"
    await callback.answer(f"✅ {count} پلن برای نماینده ذخیره شد", show_alert=True)


# =============================================================================
# FIX 21 – Mini app URL setting from admin panel
# =============================================================================

@router.callback_query(F.data == "admin_miniapp_seturl")
async def admin_miniapp_seturl_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id): return
    current = db.get_setting("miniapp_webapp_url", "")
    await state.set_state(AdminStates.waiting_miniapp_url)
    await callback.message.edit_text(
        f"📱 آدرس Mini App را وارد کنید:\n"
        f"(فعلی: {current or 'تنظیم نشده'})"
    )
    await callback.answer()



# ---------------------------------------------------------------------------
# Callback chain hardening: legacy/admin utility buttons that previously had
# keyboard entries but no callback endpoint.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_health")
async def admin_health_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    try:
        cache_stats = cache.stats()
        db_ok = bool(db.get_setting("_healthcheck", "ok") is not None)
        text = (
            "🩺 سلامت ربات\n\n"
            f"🗄 دیتابیس: {'✅' if db_ok else '❌'}\n"
            f"⚡ کش: {cache_stats['entries']} مورد | Hit Rate: {cache_stats['hit_rate']}%\n"
            f"📦 Hit: {cache_stats['hits']} | Miss: {cache_stats['misses']}"
        )
    except Exception as exc:
        text = f"🩺 سلامت ربات\n\n❌ بررسی کامل نشد:\n{exc}"
    await callback.message.edit_text(text, reply_markup=admin_health_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_cache_status")
async def admin_cache_status(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    st = cache.stats()
    text = (
        "🚀 وضعیت کش\n\n"
        f"📦 ورودی‌ها: {st['entries']}\n"
        f"✅ Hit: {st['hits']}\n"
        f"❌ Miss: {st['misses']}\n"
        f"📈 نرخ موفقیت: {st['hit_rate']}%"
    )
    await callback.message.edit_text(text, reply_markup=admin_cache_keyboard())
    await callback.answer()

@router.callback_query(F.data == "cache_clear_do")
async def admin_cache_clear_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    cache.clear_all()
    await callback.answer("✅ کش کامل پاک شد.")
    await admin_cache_status(callback)

@router.callback_query(F.data == "admin_custom_build_settings")
async def admin_custom_build_settings_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    enabled = db.get_setting("custom_build_enabled", "1") == "1"
    await callback.message.edit_text("🧩 تنظیمات سرویس سفارشی\n\nوضعیت ساخت سرویس سفارشی را مدیریت کنید.",
                                     reply_markup=admin_custom_build_keyboard(enabled))
    await callback.answer()

@router.callback_query(F.data == "admin_audit_logs")
async def admin_audit_logs_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    await _render_admin_audit_page(callback, 0)
    await callback.answer()

@router.callback_query(F.data.startswith("auditpage_"))
async def admin_audit_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    try: page = max(0, int(callback.data.split("_", 1)[1]))
    except Exception: page = 0
    await _render_admin_audit_page(callback, page)
    await callback.answer()

async def _render_admin_audit_page(target, page: int):
    logs = db.get_recent_audit_logs(limit=100) or []
    page_size = 10
    start = page * page_size
    chunk = logs[start:start + page_size]
    if not chunk:
        text = "📋 گزارش فعالیت ادمین\n\nموردی برای نمایش وجود ندارد."
    else:
        rows = ["📋 گزارش فعالیت ادمین\n"]
        for item in chunk:
            rows.append(
                f"🕐 {item.get('created_at') or item.get('timestamp') or '-'}\n"
                f"👤 {item.get('telegram_id') or item.get('admin_id') or '-'}\n"
                f"🔧 {item.get('action') or '-'}\n"
                f"📝 {str(item.get('details') or item.get('description') or '')[:180]}\n"
            )
        text = "\n".join(rows)
    markup = admin_audit_logs_keyboard(page, start + page_size < len(logs))
    if hasattr(target, "message"):
        await target.message.edit_text(text[:3900], reply_markup=markup)
    else:
        await target.answer(text[:3900], reply_markup=markup)

def _order_log_channel() -> str:
    """کانال/گروه لاگ سفارشات مؤثر؛ «0» یعنی حذف‌شده/غیرفعال. (همان مقداری که alerts.py می‌خواند)"""
    v = str(bot_info.get("order_log_channel_id") or "").strip()
    return "" if v in ("", "0") else v


def _order_log_text() -> str:
    cur = _order_log_channel()
    return (
        f"📊 کانال یا گروه لاگ سفارشات\n\nفعلی: {cur or 'تنظیم نشده'}\n\n"
        "آیدی عددی کانال/گروه (مثل -1001234567890) یا @username را ارسال کنید.\n"
        "ربات باید در آن ادمین باشد (در گروه، عضو با اجازه‌ی ارسال پیام).\n"
        "برای حذف، دکمه‌ی زیر را بزنید."
    )


@router.callback_query(F.data == "admin_order_log_channel")
async def admin_order_log_channel_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    await state.set_state(AdminStates.waiting_for_log_channel)
    await callback.message.edit_text(_order_log_text(), reply_markup=admin_order_log_channel_keyboard(bool(_order_log_channel())))
    await callback.answer()


@router.callback_query(F.data == "admin_order_log_channel_del")
async def admin_order_log_channel_delete(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    bot_info.set("order_log_channel_id", "0")
    db.set_setting("log_order_channel_id", "")
    await state.clear()
    await callback.message.edit_text(
        "✅ کانال/گروه لاگ سفارشات حذف شد و ارسال لاگ سفارش غیرفعال است.",
        reply_markup=admin_order_log_channel_keyboard(False),
    )
    await callback.answer("حذف شد")


@router.message(AdminStates.waiting_for_log_channel)
async def admin_order_log_channel_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    value = (message.text or "").strip()
    if value in ("-", "0", ""):
        value = "0"
    elif not value.startswith("@"):
        try: int(value)
        except Exception:
            await message.answer("❌ آیدی کانال/گروه نامعتبر است. مثال: -1001234567890 یا @channel")
            return
    bot_info.set("order_log_channel_id", value)
    db.set_setting("log_order_channel_id", "" if value == "0" else value)
    await state.clear()
    await message.answer(f"✅ کانال/گروه لاگ سفارشات ذخیره شد: {value if value != '0' else 'غیرفعال'}",
                         reply_markup=admin_log_settings_menu())

# Free-test panel management callbacks that existed in keyboards but were not
# registered in the router.
@router.callback_query(F.data == "ftpanel_list")
async def free_test_panels_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    panels_list = db.get_free_test_panels()
    await callback.message.edit_text(
        "🎁 پنل‌های تست رایگان\n\nیک پنل را برای مدیریت انتخاب کنید:",
        reply_markup=admin_free_test_panels_keyboard(panels_list),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("ftpanel_open_"))
async def free_test_panel_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    pid = callback.data.split("_", 2)[2]
    panel = next((p for p in db.get_free_test_panels() if str(p.get("id")) == str(pid)), None)
    if not panel:
        await callback.answer("❌ پنل پیدا نشد.", show_alert=True); return
    num = int(panel.get("num") or 0)
    mp = db.get_panel_plan_map_with_panel("free_test", num) if num else None
    map_line = f"🔌 نگاشت پنل: {mp.get('panel_name')} ← {mp.get('remote_name') or mp.get('remote_ref')}" if mp else "🔌 نگاشت پنل: ❌ تنظیم نشده"
    vol_mb = round(float(panel.get('volume_gb', 0) or 0) * 1024)
    await callback.message.edit_text(
        f"🎁 {panel.get('name', pid)}\n\n"
        f"وضعیت: {'🟢 فعال' if panel.get('enabled', True) else '🔴 غیرفعال'}\n"
        f"حجم: {vol_mb} مگابایت\nمدت: {_ft_duration_label(panel)}\n"
        f"قیمت: {int(panel.get('price', 0) or 0):,} تومان\n{map_line}",
        reply_markup=admin_free_test_panel_detail_keyboard(pid, panel.get("enabled", True), num),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("ftpanel_toggle_"))
async def free_test_panel_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    pid = callback.data.split("_", 2)[2]
    enabled = db.toggle_free_test_panel(pid)
    if enabled is None:
        await callback.answer("❌ پنل پیدا نشد.", show_alert=True); return
    await callback.answer("✅ وضعیت تغییر کرد.")
    await free_test_panel_open(callback)

@router.callback_query(F.data.startswith("ftpanel_del_"))
async def free_test_panel_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    pid = callback.data.split("_", 2)[2]
    ok = db.remove_free_test_panel(pid)
    await callback.answer("🗑 حذف شد." if ok else "❌ پنل پیدا نشد.", show_alert=not ok)
    await free_test_panels_list(callback)

# END new_handlers_v2


# ────────────────────────────────────────────────────────────────────────────
# FIX 15: Toggle کانفیگ‌ساز سفارشی (بساز سرویس خودت)
# ────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_custom_build_toggle")
async def admin_custom_build_toggle(callback: types.CallbackQuery):
    current = db.get_setting("custom_build_enabled", "1")
    new_val = "0" if current == "1" else "1"
    db.set_setting("custom_build_enabled", new_val)
    status = "✅ فعال" if new_val == "1" else "❌ غیرفعال"
    await callback.answer(f"بساز سرویس خودت: {status}", show_alert=True)
    await callback.message.edit_reply_markup(
        reply_markup=admin_custom_build_keyboard(enabled=(new_val == "1")))


# 🧩 ویرایش قیمت‌ها و محدوده‌های «بساز سرویس خودت» (قبلاً هندلر نداشت)
_CBSET_FIELDS = [
    ("price_per_gb", "💰 قیمت هر گیگ (تومان)"),
    ("price_per_30_days", "💰 قیمت هر ۳۰ روز (تومان)"),
    ("min_gb", "📦 حداقل حجم (گیگ)"),
    ("max_gb", "📦 حداکثر حجم (گیگ)"),
    ("min_days", "⏳ حداقل مدت (روز)"),
    ("max_days", "⏳ حداکثر مدت (روز)"),
]


def _cbset_target_label(num: str) -> str:
    if num == "g":
        return "🌐 پیش‌فرض عمومی"
    o = db.get_map_option("custom_build", int(num))
    return f"🔌 {o['name']}" if o else f"گزینه {num}"


def _cbset_cur(num: str) -> dict:
    return db.get_effective_custom_build_settings(None if num == "g" else int(num))


def _cbset_picker_kb():
    opts = db.get_map_options("custom_build")
    rows = [[types.InlineKeyboardButton(text="🌐 پیش‌فرض عمومی (برای گزینه‌هایی که قیمت اختصاصی ندارند)", callback_data="cbset_o|g")]]
    for o in opts:
        rows.append([types.InlineKeyboardButton(text=f"{'✅' if o['mapped'] else '⚪️'} {o['name']}", callback_data=f"cbset_o|{o['num']}")])
    rows.append([types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_custom_build_settings")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _cbset_text_and_kb(num: str = "g"):
    cur = _cbset_cur(num)
    own = {} if num == "g" else db.get_map_option_cfg("custom_build", int(num))
    lines = [f"✏️ قیمت و محدوده — {_cbset_target_label(num)}", ""]
    for key, label in _CBSET_FIELDS:
        mark = "" if num == "g" else (" ✏️" if key in own else " (از عمومی)")
        lines.append(f"{label}: {int(cur.get(key) or 0):,}{mark}")
    lines.append("\nروی هر مورد بزنید تا مقدار جدید را وارد کنید:")
    rows = [[types.InlineKeyboardButton(text=label, callback_data=f"cbset_f|{num}|{key}")] for key, label in _CBSET_FIELDS]
    if num != "g" and own:
        rows.append([types.InlineKeyboardButton(text="♻️ برگشت به مقادیر عمومی", callback_data=f"cbset_clr|{num}")])
    rows.append([types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="cbset_edit")])
    return "\n".join(lines), types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "cbset_edit")
async def admin_cbset_edit(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    await state.clear()
    await callback.message.edit_text(
        "📏 قیمت و محدوده‌های «سرویس سفارشی»\n\nمحدوده‌ی کدام پلن (گزینه‌ی نگاشت‌شده) را می‌خواهی تنظیم کنی؟\n"
        "هر پلن می‌تواند قیمت و محدوده‌ی خودش را داشته باشد؛ مواردی که تنظیم نکنی از «پیش‌فرض عمومی» می‌آیند.",
        reply_markup=_cbset_picker_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("cbset_o|"))
async def admin_cbset_open_option(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    num = callback.data.split("|", 1)[1]
    await state.clear()
    text, kb = _cbset_text_and_kb(num)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cbset_clr|"))
async def admin_cbset_clear_option(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    num = callback.data.split("|", 1)[1]
    if num != "g":
        db.clear_map_option_cfg("custom_build", int(num))
    text, kb = _cbset_text_and_kb(num)
    await callback.message.edit_text("♻️ به مقادیر عمومی برگشت.\n\n" + text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cbset_f|"))
async def admin_cbset_pick_field(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    _, num, key = callback.data.split("|", 2)
    labels = dict(_CBSET_FIELDS)
    if key not in labels:
        await callback.answer("❌ مورد نامعتبر.", show_alert=True); return
    await state.clear()
    await state.set_state(AdminStates.waiting_custom_build_settings)
    await state.update_data(cbset_field=key, cbset_num=num)
    cur = _cbset_cur(num)
    await callback.message.edit_text(
        f"{_cbset_target_label(num)}\n{labels[key]}\nمقدار فعلی: {int(cur.get(key) or 0):,}\n\nمقدار جدید را (فقط عدد) بفرستید:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 انصراف", callback_data=f"cbset_o|{num}")]]),
    )
    await callback.answer()


@router.message(AdminStates.waiting_custom_build_settings)
async def admin_cbset_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("cbset_field")
    num = str(data.get("cbset_num") or "g")
    if key not in dict(_CBSET_FIELDS):
        await state.clear()
        await message.answer("❌ نشست منقضی شد. دوباره از «تنظیم قیمت و محدوده‌ها» وارد شوید.")
        return
    raw = (message.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace(",", "").replace("،", "").strip()
    if not raw.isdigit():
        await message.answer("❌ فقط عدد صحیح و مثبت بفرستید:")
        return
    val = int(raw)
    cur = _cbset_cur(num)
    new = {k: int(cur.get(k) or 0) for k, _ in _CBSET_FIELDS}
    new[key] = val
    if key.startswith("price") and val <= 0:
        await message.answer("❌ قیمت باید بیشتر از صفر باشد:")
        return
    if new["min_gb"] < 1 or new["min_days"] < 1 or new["max_gb"] < new["min_gb"] or new["max_days"] < new["min_days"]:
        await message.answer("❌ محدوده‌ها نامعتبر است (حداقل باید ≥ ۱ و حداکثر ≥ حداقل باشد). دوباره بفرستید:")
        return
    if num == "g":
        db.set_custom_build_override(**new)
    else:
        own = db.get_map_option_cfg("custom_build", int(num))
        own[key] = val
        db.set_map_option_cfg("custom_build", int(num), own)
    await state.clear()
    text, kb = _cbset_text_and_kb(num)
    await message.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb)


# ────────────────────────────────────────────────────────────────��───────────
# FIX 13: Forward پیام با custom emoji به کانال لاگ
# ────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("fwd_rich_"))
async def forward_rich_message(callback: types.CallbackQuery):
    """Forward a rich text message (with custom emoji) to log channel."""
    msg_id = callback.data.split("fwd_rich_")[1]
    log_channel = _order_log_channel()
    if not log_channel:
        await callback.answer("⚠️ ابتدا کانال لاگ را تنظیم کنید.", show_alert=True)
        return
    try:
        await callback.bot.forward_message(
            chat_id=int(log_channel),
            from_chat_id=callback.message.chat.id,
            message_id=int(msg_id),
        )
        await callback.answer("✅ ارسال شد.", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ خطا: {e}", show_alert=True)

# ---------------------------------------------------------------------------
# FIX 11: VIP subfolder — helper functions + callbacks (ported from Cherry)
# ---------------------------------------------------------------------------

def _vip_token_to_cat(token: str):
    """'root' → None (سطح اصلی)؛ در غیر این صورت رکورد پوشه (یا None اگر پیدا نشد)."""
    if not token or token == "root":
        return None
    cat = db.get_vip_category(token)
    if cat is not None and db.is_vip_root_bucket(cat):
        return None
    return cat


def _vip_source_token(cat) -> str:
    if cat is None or db.is_vip_root_bucket(cat):
        return "root"
    return cat["key"]


async def _render_vip_move_select(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    src = _vip_token_to_cat(data.get("vip_mv_src") or "root")
    holder = src if src else db.get_vip_root_bucket(create=False)
    plans = db.get_vip_plans(holder["id"]) if holder else []
    selected = set(data.get("vip_mv_items") or [])
    where = " › ".join(c["name"] for c in db.get_vip_category_path(src["id"])) if src else "خرید اشتراک (بدون پوشه)"
    await callback.message.edit_text(
        f"📤 انتقال پلن‌ها\n\n📍 از: {where}\n\nپلن‌هایی را که می‌خواهید منتقل کنید انتخاب کنید (☑️)، بعد «ادامه» را بزنید 👇",
        reply_markup=admin_vip_move_select_keyboard(data.get("vip_mv_src") or "root", plans, selected),
    )


async def _render_vip_move_dest(callback: types.CallbackQuery, state: FSMContext, browse_token: str):
    data = await state.get_data()
    mode = data.get("vip_mv_mode")
    browse = _vip_token_to_cat(browse_token)
    exclude: set = set()
    moving = None
    if mode == "folder":
        moving = _vip_token_to_cat((data.get("vip_mv_items") or [""])[0])
        if moving:
            exclude = db.get_vip_descendant_ids(moving["id"])
    where = " › ".join(c["name"] for c in db.get_vip_category_path(browse["id"])) if browse else "خرید اشتراک (سطح اصلی)"
    title = (
        f"📂 انتقال پوشه‌ی «{moving['name'] if moving else '?'}»"
        if mode == "folder" else
        f"📤 انتقال {len(data.get('vip_mv_items') or [])} پلن"
    )
    await callback.message.edit_text(
        f"{title}\n\n📍 مقصد فعلی: {where}\n\nداخل پوشه‌ها بروید و هرجا که خواستید «✅ انتقال به همین‌جا» را بزنید 👇",
        reply_markup=admin_vip_move_dest_keyboard(browse, exclude_ids=exclude),
    )


async def _open_vip_source_view(callback: types.CallbackQuery, source_token: str):
    cat = _vip_token_to_cat(source_token)
    if cat is None:
        await admin_vip_categories_list(callback)
    else:
        await admin_vip_category_detail(callback.model_copy(update={"data": f"admincat_{cat['key']}"}))


@router.callback_query(F.data.startswith("newvipsub_"))
async def admin_new_vip_subcategory_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parent_key = callback.data.replace("newvipsub_", "")
    parent = db.get_vip_category(parent_key)
    if parent is None or db.is_vip_root_bucket(parent):
        await callback.answer("❌ این پوشه یافت نشد.", show_alert=True)
        return
    await state.update_data(new_vip_cat_parent=parent_key)
    await state.set_state(AdminStates.waiting_vip_category_name)
    await callback.message.edit_text(f"🗂 نام زیرپوشه‌ی جدید داخل «{parent['name']}» را ارسال کنید:")
    await callback.answer()


@router.callback_query(F.data.startswith("vipmvsel_"))
async def admin_vip_move_select_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    token = callback.data.replace("vipmvsel_", "")
    src = _vip_token_to_cat(token)
    holder = src if src else db.get_vip_root_bucket(create=False)
    if token != "root" and src is None:
        await callback.answer("❌ این پوشه یافت نشد.", show_alert=True)
        return
    if not holder or not db.get_vip_plans(holder["id"]):
        await callback.answer("این بخش پلنی برای انتقال ندارد.", show_alert=True)
        return
    await state.update_data(vip_mv_mode="plans", vip_mv_items=[], vip_mv_src=_vip_source_token(src))
    await _render_vip_move_select(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("vipmvtog_"))
async def admin_vip_move_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("vip_mv_mode") != "plans":
        await callback.answer("⚠️ این صفحه منقضی شده؛ دوباره از اول شروع کنید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipmvtog_", "")
    items = list(data.get("vip_mv_items") or [])
    if plan_key in items:
        items.remove(plan_key)
    else:
        items.append(plan_key)
    await state.update_data(vip_mv_items=items)
    await _render_vip_move_select(callback, state)
    await callback.answer()


@router.callback_query(F.data == "vipmvall")
async def admin_vip_move_toggle_all(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("vip_mv_mode") != "plans":
        await callback.answer("⚠️ این صفحه منقضی شده؛ دوباره از اول شروع کنید.", show_alert=True)
        return
    src = _vip_token_to_cat(data.get("vip_mv_src") or "root")
    holder = src if src else db.get_vip_root_bucket(create=False)
    keys = [p["plan_key"] for p in (db.get_vip_plans(holder["id"]) if holder else [])]
    current = set(data.get("vip_mv_items") or [])
    await state.update_data(vip_mv_items=[] if keys and current >= set(keys) else keys)
    await _render_vip_move_select(callback, state)
    await callback.answer()


@router.callback_query(F.data == "vipmvgo")
async def admin_vip_move_go(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("vip_mv_mode") != "plans":
        await callback.answer("⚠️ این صفحه منقضی شده؛ دوباره از اول شروع کنید.", show_alert=True)
        return
    if not data.get("vip_mv_items"):
        await callback.answer("اول حداقل یک پلن را انتخاب کنید.", show_alert=True)
        return
    await _render_vip_move_dest(callback, state, "root")
    await callback.answer()


@router.callback_query(F.data.startswith("vipmvplan_"))
async def admin_vip_move_single_plan(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipmvplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    src = db.get_vip_category(plan["category_id"])
    await state.update_data(vip_mv_mode="plans", vip_mv_items=[plan_key], vip_mv_src=_vip_source_token(src))
    await _render_vip_move_dest(callback, state, "root")
    await callback.answer()


@router.callback_query(F.data.startswith("vipmvcat_"))
async def admin_vip_move_folder_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cat = _vip_token_to_cat(callback.data.replace("vipmvcat_", ""))
    if cat is None:
        await callback.answer("❌ این پوشه یافت نشد.", show_alert=True)
        return
    parent = db.get_vip_category(int(cat["parent_id"])) if cat.get("parent_id") is not None else None
    await state.update_data(vip_mv_mode="folder", vip_mv_items=[cat["key"]], vip_mv_src=_vip_source_token(parent))
    await _render_vip_move_dest(callback, state, "root")
    await callback.answer()


@router.callback_query(F.data.startswith("vipmvdst_"))
async def admin_vip_move_browse(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("vip_mv_mode") not in ("plans", "folder") or not data.get("vip_mv_items"):
        await callback.answer("⚠️ این صفحه منقضی شده؛ دوباره از ����ل شروع کنید.", show_alert=True)
        return
    token = callback.data.replace("vipmvdst_", "")
    if token != "root" and _vip_token_to_cat(token) is None:
        await callback.answer("❌ این پوشه یافت نشد.", show_alert=True)
        return
    await _render_vip_move_dest(callback, state, token)
    await callback.answer()


def _vip_carry_over_plan_settings(plan: dict, source_category_id: int) -> None:
    """پلن با انتقال به پوشه‌ی دیگر نباید رفتارش عوض شود: اگر نگاشت پنل یا تنظیمات تمدید
    فقط روی «پوشه‌ی مبدأ» تعریف شده بود، همان را روی خود پلن ثبت می‌کنیم تا بعد از انتقال هم
    تحویل خودکار از همان پنل و همان قیمت/حالت تمدید کار کند. (تنظیمات اختصاصیِ خود پلن دست‌نخورده می‌ماند.)"""
    try:
        if not db.get_panel_plan_map("vip_plan", plan["id"]):
            src_map = db.get_panel_plan_map("vip_category", int(source_category_id))
            if src_map:
                db.set_panel_plan_map(
                    "vip_plan", plan["id"], src_map["panel_id"], src_map["remote_ref"], src_map.get("remote_name")
                )
    except Exception:
        logger.exception("حفظ نگاشت پنل هنگام انتقال پلن %s ناموفق بود", plan.get("plan_key"))
    try:
        for field in ("mode", "price_day", "price_gb", "min_day", "max_day", "min_gb", "max_gb", "day_options", "gb_options"):
            plan_raw = db.get_setting(f"renewal_plan_{plan['plan_key']}_{field}")
            if plan_raw not in (None, ""):
                continue
            cat_raw = db.get_setting(bot_info._PREFIX + bot_info._renewal_category_key(int(source_category_id), field))
            if cat_raw not in (None, ""):
                db.set_setting(f"renewal_plan_{plan['plan_key']}_{field}", str(cat_raw))
    except Exception:
        logger.exception("حفظ تنظیمات تمدید هنگام انتقال پلن %s ناموفق بود", plan.get("plan_key"))


@router.callback_query(F.data.startswith("vipmvhere_"))
async def admin_vip_move_apply(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    mode = data.get("vip_mv_mode")
    items = list(data.get("vip_mv_items") or [])
    if mode not in ("plans", "folder") or not items:
        await callback.answer("⚠️ این صفحه منقضی شده؛ دوباره از اول شروع کنید.", show_alert=True)
        return
    token = callback.data.replace("vipmvhere_", "")
    dest = _vip_token_to_cat(token)
    if token != "root" and dest is None:
        await callback.answer("❌ پوشه‌ی مقصد یافت نشد.", show_alert=True)
        return

    if mode == "folder":
        moving = _vip_token_to_cat(items[0])
        if moving is None:
            await callback.answer("❌ پوشه پیدا نشد.", show_alert=True)
            await state.clear()
            return
        ok, msg = db.move_vip_category_to(moving["id"], dest["id"] if dest else None)
        if not ok:
            await callback.answer(f"❌ {msg}", show_alert=True)
            return
        await state.clear()
        await callback.answer("✅ پوشه منتقل شد.")
        await _open_vip_source_view(callback, token)
        return

    # انتقال پلن‌ها
    dest_cat = dest if dest else db.get_vip_root_bucket()
    plans = [db.get_vip_plan(k) for k in items]
    plans = [p for p in plans if p]
    to_move = [p for p in plans if int(p["category_id"]) != int(dest_cat["id"])]
    if not to_move:
        await callback.answer("پلن‌های انتخاب‌شده همین الان در همین پوشه هستند.", show_alert=True)
        return
    sources = {p["plan_key"]: int(p["category_id"]) for p in to_move}
    moved = db.move_vip_plans_to_category([p["plan_key"] for p in to_move], dest_cat["id"])
    for p in to_move:
        _vip_carry_over_plan_settings(p, sources[p["plan_key"]])
    await state.clear()
    await callback.answer(f"✅ {moved} پلن منتقل شد.")
    await _open_vip_source_view(callback, token)


@router.callback_query(F.data == "vipmvcancel")
async def admin_vip_move_cancel(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    src_token = data.get("vip_mv_src") or "root"
    await state.clear()
    await callback.answer("لغو شد.")
    await _open_vip_source_view(callback, src_token)


def _ft_duration_label(entry: dict) -> str:
    if str(entry.get("duration_unit") or "days") == "hours":
        v = entry.get("duration_value") or 0
        return f"{int(v) if float(v) == int(v) else v} ساعت"
    d = entry.get("days", 0)
    return f"{int(d) if float(d) == int(d) else d} روز"


@router.callback_query(F.data == "admin_free_test_add_panel")
async def admin_free_test_add_panel(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_free_test_panel_volume)
    await callback.message.edit_text("🎁 افزودن پنل تست رایگان\n\nمرحله ۱ از ۴\nحجم پنل را به مگابایت وارد کنید. مثال: 500")


@router.message(AdminStates.waiting_free_test_panel_volume)
async def admin_free_test_panel_volume_recv(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (50 <= int(raw) <= 10240):
        await message.answer("❌ حجم باید بین 50 تا 10240 مگابایت باشد. دوباره ارسال کنید:")
        return
    await state.update_data(fp_volume=int(raw))
    await state.set_state(AdminStates.waiting_free_test_panel_days)
    from aiogram.types import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
    await message.answer(
        "مرحله ۲ از ۴\nواحد مدت اعتبار را انتخاب کنید:",
        reply_markup=_IKM(inline_keyboard=[[
            _IKB(text="روزانه", callback_data="ftunit_days", style="primary"),
            _IKB(text="ساعتی", callback_data="ftunit_hours", style="primary"),
        ]]),
    )


@router.callback_query(F.data.in_({"ftunit_days", "ftunit_hours"}))
async def admin_free_test_panel_unit_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    unit = "hours" if callback.data == "ftunit_hours" else "days"
    await state.update_data(fp_unit=unit)
    await state.set_state(AdminStates.waiting_free_test_panel_days)
    await callback.message.edit_text(
        "مرحله ۲ از ۴\n" + ("تعداد ساعت را وارد کنید (1-720):" if unit == "hours" else "تعداد روز را وارد کنید (1-30):")
    )
    await callback.answer()


@router.message(AdminStates.waiting_free_test_panel_days)
async def admin_free_test_panel_days_recv(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    _unit = (await state.get_data()).get("fp_unit")
    if _unit is None:
        await message.answer("ابتدا واحد مدت (روزانه/ساعتی) را با دکمه‌های بالا انتخاب کنید.")
        return
    _max = 720 if _unit == "hours" else 30
    if not raw.isdigit() or not (1 <= int(raw) <= _max):
        await message.answer(f"❌ مقدار باید بین 1 تا {_max} باشد. دوباره ارسال کنید:")
        return
    await state.update_data(fp_days=int(raw))
    await state.set_state(AdminStates.waiting_free_test_panel_name)
    await message.answer("مرحله ۳ از ۴\nنام نمایشی این پنل را وارد کنید (مثلاً تست 500 مگ):")


@router.message(AdminStates.waiting_free_test_panel_name)
async def admin_free_test_panel_name_recv(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد. دوباره وارد کنید:")
        return
    await state.update_data(fp_name=name)
    await state.set_state(AdminStates.waiting_free_test_panel_price)
    await message.answer("مرحله ۴ از ۴\nقیمت این پنل را به تومان وارد کنید (برای رایگان: 0):")


@router.message(AdminStates.waiting_free_test_panel_price)
async def admin_free_test_panel_price_recv(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").replace(",", "").strip()
    if not raw.isdigit() or int(raw) < 0:
        await message.answer("❌ قیمت باید یک عدد صحیح صفر یا بیشتر باشد. دوباره ارسال کنید:")
        return
    data = await state.get_data()
    volume = int(data.get("fp_volume", 500))
    days = int(data.get("fp_days", 3))
    unit = "hours" if data.get("fp_unit") == "hours" else "days"
    name = data.get("fp_name", f"تست {volume} مگ")
    price = int(raw)
    panel_id = db.add_free_test_panel({
        "name": name,
        "volume_gb": round(volume / 1024, 3),
        "days": (days / 24) if unit == "hours" else days,
        "duration_unit": unit,
        "duration_value": days,
        "price": price,
        "enabled": True,
    })
    await state.clear()
    entry = next((p for p in db.get_free_test_panels() if p.get("id") == panel_id), {})
    num = int(entry.get("num") or 0)
    from keyboards import admin_free_test_map_panels_keyboard
    panels_list = db.list_vpn_panels(enabled_only=True)
    tail = (
        "\n\n🔌 مرحله آخر: این پلن از کدام پنل فعال ساخته شود؟ پنل را انتخاب کن تا تمپلیت یا گروه را برگزینی 👇"
        if panels_list else "\n\n⚠️ هیچ پنل فعالی ثبت نشده؛ بعد از افزودن پنل، از صفحه‌ی همین پلن «نگاشت پنل» را بزن."
    )
    await message.answer(
        f"✅ پلن تست رایگان «{name}» اضافه شد.\n\n"
        f"🆔 شناسه: {panel_id}\n"
        f"💾 حجم: {volume} مگابایت\n"
        f"⏰ مدت: {days} {'ساعت' if unit == 'hours' else 'روز'}\n"
        f"💰 قیمت: {price:,} تومان" + tail,
        reply_markup=admin_free_test_map_panels_keyboard(num, panels_list) if panels_list else None,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 💱 CRYPTO BACKUP PRICE HANDLERS — بند ۲۳
# ═══════════════════════════════════════════════════════════════════════════════
# 💱 CRYPTO BACKUP PRICE HANDLERS — بند ۲۳
# ═══════════════════════════════��═════════════��════��════════════════════════════

@router.callback_query(F.data.startswith("crypto_backup_price_"))
async def admin_crypto_backup_price_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    symbol = callback.data.replace("crypto_backup_price_", "")
    await state.update_data(crypto_backup_symbol=symbol)
    await state.set_state(AdminStates.waiting_crypto_wallet)
    current = db.get_crypto_backup_price(symbol)
    enabled = db.get_crypto_backup_enabled(symbol)
    price_txt = f"{current:,.0f} تومان" if current else "تنظیم نشده"
    await callback.message.edit_text(
        f"💱 قیمت Backup ارز {symbol}\n\n"
        f"قیمت فعلی: {price_txt}\n\n"
        "قیمت ج��ید را به تومان وارد کن��د:",
        reply_markup=admin_crypto_backup_keyboard(symbol, current, enabled),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("crypto_backup_toggle_"))
async def admin_crypto_backup_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    symbol = callback.data.replace("crypto_backup_toggle_", "")
    current = db.get_crypto_backup_enabled(symbol)
    db.set_crypto_backup_enabled(symbol, not current)
    new_st = not current
    await callback.answer(f"Fallback {symbol}: {'✅ فعال' if new_st else '❌ غیرفعال'}")
    current_price = db.get_crypto_backup_price(symbol)
    await callback.message.edit_reply_markup(
        reply_markup=admin_crypto_backup_keyboard(symbol, current_price, new_st)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ⚡ PAYG CATEGORY/PLAN PRICE HANDLERS — بند ۷
# ═══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "payg_price_mode")
async def admin_payg_price_mode_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await callback.message.edit_text(
        "⚡ روش قیمت‌گذاری Pay As You Go\n\n"
        "اولویت: پلن > دسته‌بندی > عمومی",
        reply_markup=admin_payg_pricing_mode_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "payg_price_by_category")
async def admin_payg_price_by_category_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    cats = db.get_vip_categories() if hasattr(db, 'get_vip_categories') else []
    await callback.message.edit_text(
        "⚡ قیمت PAYG بر اساس دسته‌بندی:",
        reply_markup=admin_payg_category_pricing_keyboard(cats),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("payg_catprice_"))
async def admin_payg_catprice_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    try:
        cat_id = int(callback.data.replace("payg_catprice_", ""))
    except ValueError:
        await callback.answer("❌ ID نامعتبر", show_alert=True)
        return
    cats = db.get_vip_categories() if hasattr(db, 'get_vip_categories') else []
    cat = next((c for c in cats if (c.get('id') or c.get('category_id')) == cat_id), {})
    await state.update_data(payg_cat_id=cat_id, payg_plan_key=None)
    await state.set_state(AdminStates.waiting_payg_price_per_gb)
    current = db.get_payg_category_price(cat_id)
    cur_txt = f"{current:,} ت/GB" if current else "عمومی"
    await callback.message.edit_text(
        f"⚡ PAYG دسته: {cat.get('name', cat_id)}\nفعلی: {cur_txt}\n\nقیمت جدید وارد کن (0 = عمومی):",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.callback_query(F.data == "payg_price_by_plan")
async def admin_payg_price_by_plan_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    plans = []
    if hasattr(db, 'get_all_vip_plans_flat'):
        raw = db.get_all_vip_plans_flat()
        plans = list(raw.values()) if isinstance(raw, dict) else raw
    await callback.message.edit_text(
        "⚡ قیمت PAYG بر اساس پلن:",
        reply_markup=admin_payg_plan_pricing_keyboard(plans),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("payg_planprice_"))
async def admin_payg_planprice_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    plan_key = callback.data.replace("payg_planprice_", "")
    plan = {}
    if hasattr(db, 'get_vip_plan'):
        plan = db.get_vip_plan(plan_key) or {}
    await state.update_data(payg_cat_id=None, payg_plan_key=plan_key)
    await state.set_state(AdminStates.waiting_payg_price_per_gb)
    current = db.get_payg_plan_price(plan_key)
    cur_txt = f"{current:,} ت/GB" if current else "از دسته‌بندی"
    await callback.message.edit_text(
        f"⚡ PAYG پلن: {plan.get('name', plan_key)}\nفعلی: {cur_txt}\n\nقیمت جدید وارد کن (0 = دسته‌بندی):",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_payg_price_per_gb)
async def admin_payg_price_set(message: types.Message, state: FSMContext):
    """Handler مشترک برای تنظیم قیمت PAYG عمومی یا بر اساس دسته/پلن."""
    if not _is_admin(message.from_user.id):
        return
    try:
        price = int((message.text or "").replace(",", "").replace("،", "").strip())
        if price < 0:
            raise ValueError
    except Exception:
        await message.answer("❌ فقط عدد صحیح وارد کنید:")
        return
    data = await state.get_data()
    cat_id = data.get('payg_cat_id')
    plan_key = data.get('payg_plan_key')
    if cat_id is not None:
        db.set_payg_category_price(cat_id, price)
        await message.answer(f"✅ قیمت PAYG دسته به {price:,} ت/GB تنظیم شد.")
    elif plan_key:
        db.set_payg_plan_price(plan_key, price)
        await message.answer(f"✅ قیمت PAYG پلن به {price:,} ت/GB تنظیم شد.")
    else:
        db.set_payg_settings(price_per_gb=price)
        s = db.get_payg_settings()
        await message.answer(f"✅ قیمت PAYG: {price:,} تومان/GB", reply_markup=admin_payg_menu(s))
    await state.clear()


# ===========================================================================
# بند ۱۳ — Rich Text Template Manager
# ===========================================================================
from keyboards import (
    with_back_row,
    admin_custom_order_card_approval_keyboard,
    admin_charge_approval_keyboard,
    admin_purchase_card_approval_keyboard,
    admin_crypto_coin_keyboard,
    receipt_submitted_keyboard,
    admin_rich_text_list_keyboard,
    admin_rich_text_detail_keyboard,
    admin_rich_text_confirm_del_keyboard,
)


@router.callback_query(F.data == "rt_list")
async def admin_rt_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await state.clear()
    templates = db.get_rich_text_templates()
    count = len(templates)
    await callback.message.edit_text(
        f"📝 قالب‌های Rich Text\n\n"
        f"تعداد: {count} قالب\n\n"
        "روی هر قالب بزنید تا ویرایش یا حذف کنید 👇",
        reply_markup=admin_rich_text_list_keyboard(templates),
    )
    await callback.answer()


@router.callback_query(F.data == "rt_new")
async def admin_rt_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_rt_label)
    await callback.message.edit_text(
        "📝 قالب جدید — مرحله ۱ از ۲\n\n"
        "برچسب شناسایى برای این قالب بفرستید:\n\n"
        "(مثال: «پیام خوشآمدگویی»)",
    )
    await callback.answer()


@router.message(AdminStates.waiting_rt_label)
async def admin_rt_new_label(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("❌ برچسب نمی‌تواند خالی باشد.")
        return
    await state.update_data(rt_label=label)
    await state.set_state(AdminStates.waiting_rt_content)
    await message.answer(
        f"📝 قالب جدید — مرحله ۲ از ۲\n\n"
        f"برچسب: {label}\n\n"
        "متن کامل قالب را اینجا بفرستید.\n"
        "می‌توانید از Markdown یا HTML استفاده کنید.\n"
        "ایموجی‌های Premium خودبهخود ذخیره خواهند شد."
    )


@router.message(AdminStates.waiting_rt_content)
async def admin_rt_new_content(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    content = (message.text or message.caption or "").strip()
    if not content:
        await message.answer("❌ متن نمی‌تواند خالی باشد.")
        return
    data = await state.get_data()
    label = data.get('rt_label', 'قالب جدید')
    # ذخیره Custom Emoji اگر وجود داشت
    _save_dynamic_premium_emoji(label, message)
    template = db.create_rich_text_template(label, content)
    await state.clear()
    await message.answer(
        f"✅ قالب «{label}» با شناسهٔ {template['id']} ذخیره شد!",
        reply_markup=admin_rich_text_detail_keyboard(template['id']),
    )


@router.callback_query(F.data.startswith("rt_open_"))
async def admin_rt_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await state.clear()
    try:
        tid = int(callback.data.replace("rt_open_", ""))
    except ValueError:
        await callback.answer("❌ ID نامعتبر", show_alert=True)
        return
    t = db.get_rich_text_template(tid)
    if t is None:
        await callback.answer("❌ قالب یافت نشد.", show_alert=True)
        return
    preview = (t['content'] or '')[:200]
    await callback.message.edit_text(
        f"📄 {t['label']}\n\n"
        f"🆔 ID: {t['id']}\n"
        f"📅 ساخته: {t.get('created_at', '')[:10]}\n\n"
        f"📝 پیشنمایش:\n{preview}{'...' if len(t['content']) > 200 else ''}",
        reply_markup=admin_rich_text_detail_keyboard(tid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt_edit_"))
async def admin_rt_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    try:
        tid = int(callback.data.replace("rt_edit_", ""))
    except ValueError:
        await callback.answer("❌", show_alert=True)
        return
    t = db.get_rich_text_template(tid)
    if t is None:
        await callback.answer("❌ قالب یافت نشد.", show_alert=True)
        return
    await state.update_data(rt_edit_id=tid)
    await state.set_state(AdminStates.waiting_rt_content)
    await callback.message.edit_text(
        f"✏️ ویرایش متن قالب «{t['label']}»\n\n"
        "متن جدید را بفرستید:"
    )
    await callback.answer()


@router.message(AdminStates.waiting_rt_content)
async def admin_rt_save_content(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    edit_id = data.get('rt_edit_id')
    content = (message.text or message.caption or "").strip()
    if not content:
        await message.answer("❌ متن نمی‌تواند خالی باشد.")
        return
    if edit_id:
        db.update_rich_text_template(edit_id, content=content)
        await state.clear()
        await message.answer("✅ متن قالب به‌روز شد.", reply_markup=admin_rich_text_detail_keyboard(edit_id))
    else:
        # ادامه ایجاد قالب جدید
        label = data.get('rt_label', 'قالب جدید')
        _save_dynamic_premium_emoji(label, message)
        template = db.create_rich_text_template(label, content)
        await state.clear()
        await message.answer(
            f"✅ قالب «{label}» با شناسهٔ {template['id']} ذخیره شد!",
            reply_markup=admin_rich_text_detail_keyboard(template['id']),
        )


@router.callback_query(F.data.startswith("rt_relabel_"))
async def admin_rt_relabel_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    try:
        tid = int(callback.data.replace("rt_relabel_", ""))
    except ValueError:
        await callback.answer("❌", show_alert=True)
        return
    t = db.get_rich_text_template(tid)
    if t is None:
        await callback.answer("❌ قالب یافت نشد.", show_alert=True)
        return
    await state.update_data(rt_edit_id=tid)
    await state.set_state(AdminStates.waiting_rt_label)
    await callback.message.edit_text(
        f"🏷 ویرایش برچسب قالب «{t['label']}»\n\n"
        "برچسب جدید را بفرستید:"
    )
    await callback.answer()


@router.message(AdminStates.waiting_rt_label)
async def admin_rt_save_label(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("❌ برچسب نمی‌تواند خالی باشد.")
        return
    data = await state.get_data()
    edit_id = data.get('rt_edit_id')
    if edit_id:
        db.update_rich_text_template(edit_id, label=label)
        await state.clear()
        await message.answer("✅ برچسب قالب به‌روز شد.", reply_markup=admin_rich_text_detail_keyboard(edit_id))
    else:
        # مرحله اول ایجاد قالب
        await state.update_data(rt_label=label)
        await state.set_state(AdminStates.waiting_rt_content)
        await message.answer(
            f"📝 قالب جدید — مرحله ۲ از ۲\n\n"
            f"برچسب: {label}\n\n"
            "متن کامل قالب را بفرستید:"
        )


@router.callback_query(F.data.startswith("rt_del_"))
async def admin_rt_delete_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    try:
        tid = int(callback.data.replace("rt_del_", ""))
    except ValueError:
        await callback.answer("❌", show_alert=True)
        return
    t = db.get_rich_text_template(tid)
    if t is None:
        await callback.answer("❌ قالب یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗑 حذف قالب «{t['label']}»\n\n"
        "آیا مطمئن هستید که می‌خواهید این قالب را حذف کنید?",
        reply_markup=admin_rich_text_confirm_del_keyboard(tid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt_delconfirm_"))
async def admin_rt_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    try:
        tid = int(callback.data.replace("rt_delconfirm_", ""))
    except ValueError:
        await callback.answer("❌", show_alert=True)
        return
    ok = db.delete_rich_text_template(tid)
    if ok:
        await callback.answer("✅ قالب حذف شد.")
        templates = db.get_rich_text_templates()
        await callback.message.edit_text(
            f"📝 قالب‌های Rich Text\n\n"
            f"تعداد: {len(templates)} قالب",
            reply_markup=admin_rich_text_list_keyboard(templates),
        )
    else:
        await callback.answer("❌ عملیات ناموفق بود.", show_alert=True)
# این فایل به admin.py اضافه می‌شود


# ===========================================================================
# MEGA PATCH — Universal Reply Keyboard Router
# این handler‌ها دکمه‌های reply keyboard را به همان محتوای callback متصل می‌کنند.
# ===========================================================================

@router.message(F.text == "📊 آمار")
async def admin_stats_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    await message.answer(_render_stats_text("all"), reply_markup=admin_stats_keyboard())


@router.message(F.text == "👥 لیست کاربران")
async def admin_userlist_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    text, kb = _userlist_view("new", 0)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "🔍 جستجوی حرفه‌ای")
async def admin_search_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await _open_search_prompt(message, state, "user")


@router.message(F.text == "🔎 جستجوی کانفیگ")
async def admin_config_search_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await _open_search_prompt(message, state, "config")


@router.message(F.text == "📢 پیام همگانی")
async def admin_broadcast_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "📢 پیام همگانی\n\nنوع مخاطب را انتخاب کنید:",
        reply_markup=admin_broadcast_audience_keyboard(),
    )


@router.message(F.text == "🎟 مدیریت تخفیف")
async def admin_discount_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_discount_keyboard
    try:
        codes = db.get_all_discount_codes() if hasattr(db, 'get_all_discount_codes') else []
        kb = admin_discount_keyboard(codes)
    except Exception:
        kb = admin_back_button()
    await message.answer("🎟 مدیریت تخفیف", reply_markup=kb)


@router.message(F.text == "📣 لینک‌های تبلیغاتی")
async def admin_campaigns_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_campaigns_keyboard
    try:
        camps = db.get_campaigns() if hasattr(db, 'get_campaigns') else []
        kb = admin_campaigns_keyboard(camps)
    except Exception:
        kb = admin_back_button()
    await message.answer("📣 لینک‌های تبلیغاتی", reply_markup=kb)


@router.message(F.text == "🛠 ایجاد سرویس (ادمین)")
async def admin_create_service_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_create_service_keyboard
    try:
        kb = admin_create_service_keyboard()
    except Exception:
        kb = admin_back_button()
    await message.answer("🛠 ایجاد سرویس (ادمین)", reply_markup=kb)


@router.message(F.text == "📚 مدیریت راهنما")
async def admin_guides_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_guides_keyboard
    try:
        guides = db.get_guides() if hasattr(db, 'get_guides') else []
        kb = admin_guides_keyboard(guides)
    except Exception:
        kb = admin_back_button()
    await message.answer("📚 مدیریت راهنما", reply_markup=kb)


@router.message(F.text.in_({"🦖 لاگ خطاها و Audit", "🦖 لاگ خطاها"}))
async def admin_errlog_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_error_logs_keyboard
    try:
        logs = db.get_error_logs(limit=50) if hasattr(db, 'get_error_logs') else []
        kb = admin_error_logs_keyboard(logs)
    except Exception:
        kb = admin_back_button()
    await message.answer("🦖 لاگ خطاها و Audit", reply_markup=kb)


@router.message(F.text == "📜 مدیریت لاگ‌ها")
async def admin_log_settings_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    log_channel = _order_log_channel()
    text = (
        "📋 مدیریت لاگ‌ها\n\n"
        f"📡 کانال/گروه لاگ سفارشات: {log_channel or 'تنظیم نشده'}\n\n"
        "برای دریافت لاگ خودکار سفارشات، ربات را به کانال اضافه کنید."
    )
    from keyboards import admin_log_settings_menu
    await message.answer(text, reply_markup=admin_log_settings_menu())


@router.message(F.text == "💳 روش‌های پرداخت")
async def admin_payment_methods_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_payment_methods_menu
    text = (
        "💳 مدیریت روش‌های پرداخت\n\n"
        "در این بخش می‌توانید درگاه‌های پرداخت فعال را مدیریت کنید."
    )
    await message.answer(text, reply_markup=admin_payment_methods_menu())


@router.message(F.text.in_({"💳 تنظیمات کیف‌پول", "💰 تنظیمات کیف پول"}))
async def admin_wallet_settings_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_wallet_settings_menu
    settings = db.get_wallet_settings() if hasattr(db, 'get_wallet_settings') else {}
    text = (
        "💰 تنظیمات کیف‌پول\n\n"
        f"حداقل شارژ: {settings.get('min_topup', 0):,} تومان\n"
        f"حداکثر شارژ: {settings.get('max_topup', 0):,} تومان"
    )
    await message.answer(text, reply_markup=admin_wallet_settings_menu(settings))


@router.message(StateFilter(None), (F.text.in_({"🖥 پنل مدیریت (Mini App)", "📱 پنل مدیریت (Mini App)", "mini app", "Mini App", "مینی اپ", "🖥 Mini App ادمین"})) | (F.text.lower().in_({"mini app", "miniapp", "mini-app"})))
async def admin_miniapp_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    _bot_username = (db.get_setting("bot_username", "") or getattr(__import__("config"), "BOT_USERNAME", "") or "").strip()
    miniapp_url = (db.get_setting("miniapp_webapp_url", "") or "").strip()
    from keyboards import is_valid_webapp_url as _valid_webapp
    webapp_url = miniapp_url if _valid_webapp(miniapp_url) else ""
    if webapp_url and not webapp_url.rstrip("/").endswith("/admin-panel"):
        webapp_url = webapp_url.rstrip("/") + "/admin-panel"
    rows = []
    if webapp_url:
        rows.append([InlineKeyboardButton(text="🖥 باز کردن پنل ادمین", web_app=WebAppInfo(url=webapp_url), style="primary")])
    rows.append([InlineKeyboardButton(text="🔗 تنظیم آدرس Mini App", callback_data="admin_miniapp_seturl", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        f"🖥 پنل مدیریت (Mini App)\n\n"
        f"آدرس فعلی: {webapp_url or 'تنظیم نشده'}\n\n"
        "برای باز کردن پنل مدیریت گرافیکی روی دکمه زیر کلیک کنید:",
        reply_markup=kb
    )


@router.message(F.text == "💾 بکاپ")
async def admin_backup_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_backup_keyboard
    try:
        kb = admin_backup_keyboard()
    except Exception:
        kb = admin_back_button()
    await message.answer("💾 بکاپ", reply_markup=kb)


@router.message(F.text == "🤝 مدیریت دعوت‌ها")
async def admin_referrals_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_referrals_keyboard
    try:
        kb = admin_referrals_keyboard()
    except Exception:
        kb = admin_back_button()
    await message.answer("🤝 مدیریت دعوت‌ها", reply_markup=kb)


@router.message(F.text == "🎫 مدیریت تیکت‌ها")
async def admin_tickets_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_tickets_keyboard
    try:
        tickets = db.get_pending_tickets() if hasattr(db, 'get_pending_tickets') else []
        kb = admin_tickets_keyboard(tickets)
    except Exception:
        kb = admin_back_button()
    await message.answer("🎫 مدیریت تیکت‌ها", reply_markup=kb)


@router.message(F.text == "🖥 مدیریت پنل‌های VPN")
async def admin_vpn_panels_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_panels_keyboard
    try:
        panels = db.list_vpn_panels() if hasattr(db, 'list_vpn_panels') else []
        kb = admin_panels_keyboard(panels)
    except Exception:
        kb = admin_back_button()
    await message.answer("🖥 مدیریت پنل‌های VPN", reply_markup=kb)


@router.message(F.text == "🎁 تنظیمات رفرال")
async def admin_referral_settings_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear()
    from keyboards import admin_referral_settings_menu
    try:
        settings = db.get_referral_settings() if hasattr(db, 'get_referral_settings') else {}
        kb = admin_referral_settings_menu(settings)
    except Exception:
        kb = admin_back_button()
    await message.answer(_referral_settings_text(settings), reply_markup=kb)


# --- Mini App URL Save Handler ---
@router.message(AdminStates.waiting_miniapp_url)
async def admin_miniapp_url_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    url = (message.text or "").strip().rstrip("/")
    if url and "." in url and not url.lower().startswith(("http://", "https://")) and " " not in url:
        url = "https://" + url
    if not url.startswith("https://"):
        await message.answer("❌ آدرس باید با https:// شروع شود. دوباره وارد کنید:")
        return
    from keyboards import is_valid_webapp_url as _valid_webapp
    if not _valid_webapp(url):
        await message.answer(
            "❌ این آدرس دامنه‌ی معتبر ندارد (مثلاً https://example.com).\n"
            "تلگرام دکمه‌ی Web App با آدرس بدون دامنه را رد می‌کند و باعث خطا در «سرویس‌های من» می‌شود.\n"
            "آدرس کامل Mini App را دوباره وارد کنید:"
        )
        return
    # ادمین ممکن است خودِ /admin-panel را بفرستد. مسیر را هنگام ذخیره حذف می‌کنیم
    # تا یک مقدار canonical داشته باشیم و handler بازکردن Mini App خودش مسیر را بسازد.
    for suffix in ("/admin-panel", "/admin", "/reseller"):
        if url.endswith(suffix):
            url = url[:-len(suffix)].rstrip("/")
            break
    db.set_setting("miniapp_webapp_url", url)
    saved = (db.get_setting("miniapp_webapp_url", "") or "").strip().rstrip("/")
    if saved != url:
        await state.clear()
        await message.answer("❌ ذخیره آدرس Mini App انجام نشد؛ مقدار ثبت‌شده با مقدار ورودی یکسان نیست. لطفاً دوباره تلاش کنید.")
        return
    await state.clear()
    await message.answer(
        f"✅ آدرس Mini App ذخیره شد:\n{saved}/admin-panel",
        reply_markup=_admin_reply_kb_for(message.from_user.id)
    )


# --- Crypto payment route for user purchase ---
def _resolve_crypto_plan_key(value: str) -> str | None:
    """Resolve the compact plan token used by crypto buttons back to the real plan key."""
    value = str(value or "").strip()
    if not value:
        return None
    if db.get_effective_plan(value) is not None:
        return value
    import hashlib
    try:
        for plan_key in db.get_all_plans().keys():
            if hashlib.md5(str(plan_key).encode("utf-8")).hexdigest()[:10] == value:
                return str(plan_key)
    except Exception:
        logger.exception("failed to resolve compact crypto plan key")
    return None


@router.callback_query(F.data.startswith("pay_crypto_asset|"))
async def user_pay_crypto_asset(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    parts = callback.data.split("|", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, compact_plan_key, symbol = parts
    plan_key = _resolve_crypto_plan_key(compact_plan_key)
    if not plan_key:
        await callback.answer("❌ پلن این پرداخت دیگر در دسترس نیست. دوباره پلن را انتخاب کنید.", show_alert=True)
        return
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا /start بزنید.", show_alert=True)
        return
    plan = db.get_effective_plan(plan_key)
    if plan is None:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return
    try:
        from handlers.plans import _compute_final_price
        from keyboards import crypto_payment_detail_keyboard
        data = await state.get_data()
        final_price, _note, winning_code = _compute_final_price(plan_key, plan, callback.from_user.id, data)
        coin = _pm().get_crypto(symbol)
        if not coin or not coin.enabled or not _pm().crypto_master_enabled:
            await callback.answer("❌ این ارز در حال حاضر در دسترس نیست.", show_alert=True)
            return
        wallet_addr = str(coin.wallet or '').strip()
        if not wallet_addr:
            await callback.answer("❌ آدرس کیف‌پول این ارز هنوز تنظیم نشده است.", show_alert=True)
            return
        # قیمت لحظه‌ای از API؛ قیمت پشتیبان فقط وقتی API در دسترس نیست.
        rate, rate_source = None, ""
        try:
            import crypto_rates
            rate, rate_source = await crypto_rates.get_rate_toman(symbol)
        except Exception:
            logger.exception("live crypto rate failed for %s", symbol)
        if not rate or float(rate) <= 0:
            try:
                backup_on = db.get_crypto_backup_enabled(symbol)
            except Exception:
                backup_on = True
            if backup_on:
                rate = coin.backup_price or db.get_crypto_backup_price(symbol)
                rate_source = "backup"
        if not rate or float(rate) <= 0:
            await callback.answer("❌ دریافت قیمت لحظه‌ای این ارز ممکن نشد. چند دقیقه دیگر تلاش کن یا به پشتیبانی خبر بده.", show_alert=True)
            return
        logger.info("crypto invoice rate %s=%s via %s", symbol, rate, rate_source)
        gross_price = final_price * (1 + float(coin.margin_percent or 0) / 100.0)
        amount_asset = round(gross_price / float(rate), 6)
        if amount_asset <= 0:
            await callback.answer("❌ محاسبه مبلغ ارزی ناموفق بود.", show_alert=True)
            return
        invoice = db.create_invoice(
            user_id=user["id"],
            telegram_id=str(callback.from_user.id),
            kind="plan_crypto",
            label=plan["name"],
            price=final_price,
            payload={"asset": coin.symbol, "amount": amount_asset, "network": coin.network},
        )
        _sd = await state.get_data()
        await state.update_data(
            crypto_purchase_plan=plan_key,
            crypto_purchase_price=final_price,
            crypto_purchase_symbol=coin.symbol,
            crypto_purchase_network=coin.network,
            crypto_purchase_amount=str(amount_asset),
            crypto_purchase_wallet=wallet_addr,
            crypto_purchase_discount_code=winning_code,
            crypto_invoice_id=invoice["id"],
            crypto_purchase_service_name=(
                _sd.get("purchase_config_name") if _sd.get("purchase_config_name_plan") == plan_key else None
            ),
        )
        await state.set_state(UserStates.waiting_crypto_purchase_receipt)
        await callback.message.edit_text(
            f"💱 پرداخت ارزی\n\n"
            f"📦 پلن: {plan['name']}\n"
            f"💰 مبلغ سرویس: {final_price:,} تومان\n"
            f"💵 مبلغ قابل پرداخت: {amount_asset} {coin.symbol}\n"
            f"🌐 شبکه: {coin.network or '-'}\n"
            f"👛 آدرس ولت: {wallet_addr}\n\n"
            "پس از واریز، عکس رسید یا هش تراکنش را همینجا ارسال کنید.\n"
            "⏱ این فاکتور تا ۳۰ دقیقه معتبر است.",
            reply_markup=crypto_payment_detail_keyboard(wallet_addr, str(amount_asset), plan_key),
        )
    except Exception:
        logger.exception("pay_crypto asset selection failed")
        await callback.answer("❌ آماده‌سازی پرداخت ارزی ناموفق بود.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("pay_crypto_") & ~F.data.startswith("pay_crypto_asset|"))
async def user_pay_crypto_start(callback: types.CallbackQuery, state: FSMContext):
    """شروع پرداخت ارز دیجیتال برای خرید پلن."""
    plan_key = callback.data.replace("pay_crypto_", "")
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا /start بزنید.", show_alert=True)
        return
    plan = db.get_effective_plan(plan_key)
    if plan is None:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return
    try:
        from handlers.plans import _compute_final_price
        from keyboards import crypto_select_keyboard
        data = await state.get_data()
        final_price, _note, _winning_code = _compute_final_price(plan_key, plan, callback.from_user.id, data)
        enabled_cryptos = list(_pm().enabled_cryptos or [])
        if not enabled_cryptos:
            await callback.answer("در حال حاضر هیچ ارز فعالی برای پرداخت تنظیم نشده است.", show_alert=True)
            return
        kb = crypto_select_keyboard(enabled_cryptos, plan_key, final_price)
        await callback.message.edit_text(
            f"💱 پرداخت ارزی\n\n"
            f"📦 پلن: {plan.get('name', plan_key)}\n"
            f"💰 مبلغ سفارش: {final_price:,} تومان\n\n"
            "ارز مورد نظر را انتخاب کنید:",
            reply_markup=kb,
        )
    except Exception:
        logger.exception("pay_crypto start failed")
        await callback.answer("❌ باز کردن بخش پرداخت ارزی ناموفق بود.", show_alert=True)
        return
    await callback.answer()




async def _submit_crypto_purchase_receipt(message: types.Message, state: FSMContext, hash_text: str | None = None) -> None:
    uid = str(message.from_user.id)
    user = db.get_user(uid)
    data = await state.get_data()
    plan_key = data.get("crypto_purchase_plan")
    final_price = data.get("crypto_purchase_price")
    symbol = data.get("crypto_purchase_symbol")
    amount_asset = data.get("crypto_purchase_amount")
    network = data.get("crypto_purchase_network")
    winning_code = data.get("crypto_purchase_discount_code")
    invoice_id = data.get("crypto_invoice_id")
    plan = db.get_effective_plan(plan_key) if plan_key else None

    if user is None or plan is None or final_price is None or not symbol or not amount_asset:
        await message.answer("❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس‌ها شروع کنید.")
        await state.clear()
        return

    if not invoice_id or db.consume_invoice(invoice_id) is None:
        await message.answer("⏰ مهلت این فاکتور پرداخت ارزی به پایان رسیده است. لطفاً دوباره از منوی سرویس‌ها شروع کنید.")
        await state.clear()
        return

    from utils import extract_receipt_media, send_receipt_card_to_admin, receipt_user_label
    file_id, file_type, _msg_text = extract_receipt_media(message)
    receipt_id = None
    try:
        receipt_id = db.create_pending_receipt(
            "plan_crypto", uid, user["id"], plan["name"], int(final_price),
            extra=plan_key, plan_key=plan_key, discount_code=winning_code,
            file_id=file_id, file_type=file_type, note=(hash_text or None),
            service_name=data.get("crypto_purchase_service_name"),
        )
    except Exception:
        logger.exception("ثبت رسید ارزی ناموفق بود")
        receipt_id = None

    if invoice_id:
        db.delete_invoice(invoice_id)

    tracking_code = receipt_id if receipt_id is not None else invoice_id
    admin_caption = (
        f"💱 رسید پرداخت ارزی ({'تصویر' if file_id else 'هش/متن'})\n\n"
        f"👤 {receipt_user_label(message.from_user)}\n"
        f"🆔 {uid}\n"
        f"📦 {plan['name']}\n"
        f"💰 {int(final_price):,} تومان\n"
        f"💵 {amount_asset} {symbol}\n"
        f"🌐 شبکه: {network or '-'}\n"
        + (f"🔤 نام سرویس: {data.get('crypto_purchase_service_name')}\n" if data.get("crypto_purchase_service_name") else "")
        + f"🧾 کد پیگیری: {tracking_code}"
        + (f"\n📝 Hash / Receipt: {hash_text}" if hash_text else "")
    )
    if receipt_id is not None:
        try:
            db.set_pending_receipt_media(receipt_id, details=admin_caption)
        except Exception:
            logger.exception("ذخیره‌ی توضیح رسید ارزی ناموفق بود")
    delivered = await send_receipt_card_to_admin(
        message.bot, admin_caption,
        reply_markup=admin_purchase_card_approval_keyboard(uid, plan_key, int(final_price), receipt_id),
        file_id=file_id, file_type=file_type,
    )
    if not delivered:
        logger.error("رسید ارزی %s برای ادمین ارسال نشد؛ فقط در صف رسیدها موجود است", tracking_code)

    from keyboards import receipt_submitted_keyboard
    await message.answer(
        f"✅ رسید پرداخت ارزی با موفقیت ثبت شد\n\n"
        f"🧾 کد پیگیری: `{tracking_code}`\n"
        f"📦 سرویس: {plan['name']}\n"
        f"💳 مبلغ ریالی: {int(final_price):,} تومان\n"
        f"💵 مبلغ ارزی: {amount_asset} {symbol}\n"
        f"🟡 وضعیت: در حال بررسی",
        parse_mode="Markdown",
        reply_markup=receipt_submitted_keyboard(),
    )
    await state.clear()


@router.message(UserStates.waiting_crypto_purchase_receipt, F.photo | F.document)
async def receive_crypto_purchase_receipt_photo(message: types.Message, state: FSMContext):
    await _submit_crypto_purchase_receipt(message, state)


@router.message(UserStates.waiting_crypto_purchase_receipt, F.text)
async def receive_crypto_purchase_receipt_text(message: types.Message, state: FSMContext):
    hash_text = (message.text or '').strip()
    if len(hash_text) < 8:
        await message.answer("❌ هش تراکنش معتبر به نظر نمی‌رسد. لطفاً هش کامل تراکنش یا عکس رسید را ارسال کنید.")
        return
    await _submit_crypto_purchase_receipt(message, state, hash_text=hash_text)


@router.message(UserStates.waiting_crypto_purchase_receipt)
async def receive_crypto_purchase_receipt_invalid(message: types.Message):
    await message.answer("❌ لطفاً عکس رسید یا هش تراکنش را ارسال کنید.")


# --- Free test single plan settings (backward compat) ---
@router.callback_query(F.data == "admin_free_test_single")
async def admin_free_test_settings_single(callback: types.CallbackQuery, state: FSMContext):
    """تنظیم پلن پیش‌فرض تست رایگان (فرمت قدیمی)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    await state.set_state(AdminStates.waiting_free_test_settings)
    await callback.message.edit_text(_free_test_settings_text())
    await callback.answer()


# END MEGA PATCH — Reply Keyboard Handlers

# =============================================================================
# PATCH BLOCK — payment/admin miniapp/log callbacks restored after refactor
# =============================================================================

@router.callback_query(F.data == "admin_payment_methods")
async def admin_payment_methods_open(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    text = (
        "💳 مدیریت روش‌های پرداخت\n\n"
        "روش موردنظر را انتخاب و وضعیت هر درگاه را مدیریت کنید."
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_payment_methods_menu())
    except Exception:
        pass


@router.callback_query(F.data == "admin_wallet_settings")
async def admin_wallet_settings_open(callback: types.CallbackQuery):
    """Backward-compatible alias: wallet settings now live inside Payment Methods."""
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    await paymt_wallet_open(callback)


@router.callback_query(F.data == "admin_log_settings")
async def admin_log_settings_open(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    log_channel = _order_log_channel()
    text = (
        "📋 مدیریت لاگ‌ها\n\n"
        f"📡 کانال/گروه لاگ سفارشات: {log_channel or 'تنظیم نشده'}\n\n"
        "برای دریافت لاگ خودکار سفارشات، ربات را به کانال اضافه کنید و آی‌دی کانال را وارد نمایید."
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_log_settings_menu())
    except Exception:
        pass


@router.callback_query(F.data == "admin_miniapp_open")
async def admin_miniapp_open_handler(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ این بخش فقط برای ادمین مجاز است.", show_alert=True)
        return
    await callback.answer()  # FAST
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    _bot_username = (bot_info.get("bot_username") or db.get_setting("bot_username", "") or getattr(__import__("config"), "BOT_USERNAME", "")) or ""
    import os
    miniapp_url = (db.get_setting("miniapp_webapp_url", "") or os.environ.get("MINIAPP_WEBAPP_URL", "")).strip()
    if miniapp_url:
        webapp_url = miniapp_url.rstrip("/")
        if not webapp_url.endswith("/admin-panel"):
            webapp_url += "/admin-panel"
    elif _bot_username:
        webapp_url = "https://t.me/" + _bot_username + "/app?startapp=role_admin"
    else:
        webapp_url = ""
    rows = []
    if webapp_url and webapp_url.startswith("https://t.me/"):
        # لینک t.me نمی‌تواند داخل web_app قرار بگیرد (تلگرام کل پیام را رد می‌کند)؛
        # فقط به‌صورت دکمه‌ی لینک. برای دکمه‌ی وب‌اپ واقعی باید آدرس Mini App تنظیم شود.
        rows.append([InlineKeyboardButton(text="🖥 باز کردن پنل ادمین", url=webapp_url, style="primary")])
    elif webapp_url.startswith("https://") and __import__("keyboards").is_valid_webapp_url(webapp_url):
        rows.append([InlineKeyboardButton(text="🖥 باز کردن پنل ادمین", web_app=WebAppInfo(url=webapp_url), style="primary")])
        rows.append([InlineKeyboardButton(text="🌐 لینک مستقیم پنل", url=webapp_url, style="primary")])
    rows.append([InlineKeyboardButton(text="🔗 تنظیم آدرس Mini App", callback_data="admin_miniapp_seturl", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بستن", callback_data="admin_back", style="danger")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(
            "🖥 پنل مدیریت (Mini App)\n\nبرای باز کردن پنل مدیریت گرافیکی روی یکی از دکمه‌های زیر کلیک کنید:"
            + ("" if webapp_url.startswith("https://") else "\n\n⚠️ آدرس Mini App (https) تنظیم نشده است؛ از دکمه‌ی «تنظیم آدرس Mini App» آن را وارد کنید."),
            reply_markup=kb,
        )
    except Exception:
        logger.exception("admin_miniapp_open edit failed")


@router.callback_query(F.data.startswith("paymt_topup"))
async def paymt_topup_toggle(callback: types.CallbackQuery):
    """فعال/غیرفعال‌کردن هر روش پرداخت فقط برای «شارژ کیف پول» (مستقل از خرید)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    if "|" not in callback.data:
        await callback.answer("با دکمه‌های پایین، هر روش را برای شارژ کیف پول روشن/خاموش کنید.", show_alert=True)
        return
    method = callback.data.split("|", 1)[1]
    try:
        new_state = db.toggle_topup_switch(method)
    except ValueError:
        await callback.answer("❌ روش نامعتبر", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_payment_methods_menu())
    except Exception:
        pass
    await callback.answer("✅ برای شارژ کیف پول فعال شد." if new_state else "⛔ برای شارژ کیف پول غیرفعال شد.")


@router.callback_query(F.data == "paymt_card")
async def paymt_card_open(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); card = pm.card
    st = "✅ فعال" if card.enabled else "❌ غیرفعال"
    card_num = getattr(card, 'card_number', None) or getattr(card, 'number', '') or 'تنظیم نشده'
    card_owner = getattr(card, 'owner_name', None) or getattr(card, 'owner', '') or 'تنظیم نشده'
    text = (
        "💳 کارت‌به‌کارت دستی\n\n"
        f"وضعیت: {st}\n"
        f"شماره کارت: {card_num}\n"
        f"نام صاحب: {card_owner}\n\n"
        "🔒 قفل فوروارد: پیام‌هایی که شماره کارت دارند قابل فوروارد/ذخیره نمی‌شوند.\n"
        "🎲 مبلغ رندوم: هر فاکتور ± ۱۰۰ تا ۱۵۰۰ تومان تغییر می‌کند؛ با خاموش‌کردن، مبلغ رند (همان قیمت) نمایش داده می‌شود."
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_card_info_menu(card))
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^paymt_card_opt\|(show_copy_card|show_copy_amount|forward_lock|random_amount)$"))
async def paymt_card_option_toggle(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    key = callback.data.split("|", 1)[1]
    pm = _pm()
    pm.update_card(**{key: not bool(getattr(pm.card, key, db.card_option(key)))})
    await paymt_card_open(callback)


@router.callback_query(F.data == "paymt_card_toggle")
async def paymt_card_toggle(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); pm.update_card(enabled=not pm.card.enabled)
    await paymt_card_open(callback)


@router.callback_query(F.data == "paymt_card_number")
async def paymt_card_number_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_card_number)
    try:
        await callback.message.edit_text("💳 شماره کارت جدید را وارد کنید (16 رقم):")
    except Exception:
        pass


@router.message(AdminStates.waiting_card_number)
async def paymt_card_number_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    num = (message.text or "").replace("-", "").replace(" ", "").strip()
    _pm().update_card(card_number=num)
    await state.clear()
    await message.answer(f"✅ شماره کارت ذخیره شد: {num}", reply_markup=admin_card_info_menu(_pm().card))


@router.callback_query(F.data == "paymt_card_owner")
async def paymt_card_owner_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_card_owner)
    try:
        await callback.message.edit_text("👤 نام صاحب کارت را وارد کنید:")
    except Exception:
        pass


@router.message(AdminStates.waiting_card_owner)
async def paymt_card_owner_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    _pm().update_card(owner_name=(message.text or "").strip())
    await state.clear()
    await message.answer("✅ نام صاحب کارت ذخیره شد.", reply_markup=admin_card_info_menu(_pm().card))


@router.callback_query(F.data == "paymt_wallet")
async def paymt_wallet_open(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm()
    settings = db.get_wallet_settings()
    payload = {"enabled": bool(pm.wallet.enabled), **settings}
    st = "✅ فعال" if payload["enabled"] else "❌ غیرفعال"
    text = (
        "💰 پرداخت از کیف‌پول کاربران\n\n"
        f"وضعیت: {st}\n"
        f"حداقل شارژ: {payload['min_topup']:,} تومان\n"
        f"حداکثر شارژ: {payload['max_topup']:,} تومان\n"
        f"مبالغ پیشنهادی: {' / '.join(f'{int(x):,}' for x in payload['quick_amounts'])}"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_wallet_info_menu(payload))
    except Exception:
        pass


@router.callback_query(F.data == "paymt_wallet_toggle")
async def paymt_wallet_toggle(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); pm.update_wallet(enabled=not pm.wallet.enabled)
    await paymt_wallet_open(callback)


@router.callback_query(F.data == "paymt_uniquepay")
async def paymt_uniquepay_open(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); u = pm.uniquepay
    st = "✅ فعال" if u.enabled else "❌ غیرفعال"
    token = (u.business_token or "").strip()
    token_display = (token[:18] + "...") if len(token) > 18 else (token or "تنظیم نشده")
    redirect = u.redirect_url or "تنظیم نشده"
    cb_url = u.callback_url or "تنظیم نشده"
    mode = "🤖 داخل تلگرام" if u.use_telegram_link else "🌐 وب‌سایت درگاه"
    wl_text = "روشن (کارت مقصد + مبلغ دقیق)" if u.show_card_white_label else "خاموش"
    text = (
        "🌐 پرداخت آنلاین (UniquePay)\n\n"
        f"وضعیت: {st}\n"
        f"Business Token: {token_display}\n"
        f"Redirect URL: {redirect}\n"
        f"Callback URL: {cb_url}\n"
        f"حداقل مبلغ: {u.min_amount:,} تومان\n"
        f"محل پرداخت: {mode}\n"
        f"نمایش اطلاعات کارت (وایت‌لیبل): {wl_text}"
    )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("❌ غیرفعال کردن" if u.enabled else "✅ فعال کردن"), callback_data="paymt_uniquepay_toggle", style="primary")],
        [InlineKeyboardButton(text="🔐 Business Token", callback_data="paymt_uniquepay_token", style="primary"),
         InlineKeyboardButton(text="🔁 Redirect URL", callback_data="paymt_uniquepay_redirect", style="primary")],
        [InlineKeyboardButton(text="📡 Callback URL", callback_data="paymt_uniquepay_callback", style="primary"),
         InlineKeyboardButton(text="💰 حداقل مبلغ", callback_data="paymt_uniquepay_min_amount", style="primary")],
        [InlineKeyboardButton(text=("📍 محل پرداخت: 🤖 داخل تلگرام" if u.use_telegram_link else "📍 محل پرداخت: 🌐 وب‌سایت درگاه"), callback_data="paymt_uniquepay_mode", style="success")],
        [InlineKeyboardButton(text=("💳 نمایش کارت و مبلغ دقیق: ✅ روشن" if u.show_card_white_label else "💳 نمایش کارت و مبلغ دقیق: ❌ خاموش"), callback_data="paymt_uniquepay_wl", style="success" if u.show_card_white_label else "danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "paymt_uniquepay_toggle")
async def paymt_uniquepay_toggle(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); pm.update_uniquepay(enabled=not pm.uniquepay.enabled)
    await paymt_uniquepay_open(callback)


@router.callback_query(F.data == "paymt_uniquepay_token")
async def paymt_uniquepay_token_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_uniquepay_business_token)
    await callback.message.edit_text("🔐 Business Token یونیک‌پی را وارد کنید:")


@router.message(AdminStates.waiting_uniquepay_business_token)
async def paymt_uniquepay_token_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    _pm().update_uniquepay(business_token=(message.text or '').strip())
    await state.clear()
    await message.answer("✅ Business Token ذخیره شد.")


@router.callback_query(F.data == "paymt_uniquepay_redirect")
async def paymt_uniquepay_redirect_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_uniquepay_redirect_url)
    await callback.message.edit_text("🔁 Redirect URL را وارد کنید (یا - برای حذف):")


@router.message(AdminStates.waiting_uniquepay_redirect_url)
async def paymt_uniquepay_redirect_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or '').strip()
    _pm().update_uniquepay(redirect_url='' if raw == '-' else raw)
    await state.clear()
    await message.answer("✅ Redirect URL ذخیره شد.")


@router.callback_query(F.data == "paymt_uniquepay_callback")
async def paymt_uniquepay_callback_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_uniquepay_callback_url)
    await callback.message.edit_text("📡 Callback URL را وارد کنید (یا - برای حذف):")


@router.message(AdminStates.waiting_uniquepay_callback_url)
async def paymt_uniquepay_callback_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or '').strip()
    _pm().update_uniquepay(callback_url='' if raw == '-' else raw)
    await state.clear()
    await message.answer("✅ Callback URL ذخیره شد.")


@router.callback_query(F.data == "paymt_uniquepay_min_amount")
async def paymt_uniquepay_min_amount_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_uniquepay_min_amount)
    await callback.message.edit_text("💰 حداقل مبلغ را به تومان وارد کنید:")


@router.message(AdminStates.waiting_uniquepay_min_amount)
async def paymt_uniquepay_min_amount_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or '').replace(',', '').strip()
    if not raw.isdigit() or int(raw) < 50000:
        await message.answer("❌ حداقل مبلغ باید عددی و حداقل 50000 تومان باشد.")
        return
    _pm().update_uniquepay(min_amount=int(raw))
    await state.clear()
    await message.answer("✅ حداقل مبلغ ذخیره شد.")


@router.callback_query(F.data == "paymt_uniquepay_mode")
async def paymt_uniquepay_mode_toggle(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); u = pm.uniquepay
    pm.update_uniquepay(use_telegram_link=not u.use_telegram_link, use_ddbot_compat=False)
    await paymt_uniquepay_open(callback)


@router.callback_query(F.data == "paymt_uniquepay_wl")
async def paymt_uniquepay_wl_toggle(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); u = pm.uniquepay
    pm.update_uniquepay(show_card_white_label=not u.show_card_white_label)
    await paymt_uniquepay_open(callback)



# ---------------------------------------------------------------------------
# 🌐 درگاه‌های جدید: FrenzyEx و Tronado (تنظیمات کامل مثل بقیه‌ی روش‌های پرداخت)
# ---------------------------------------------------------------------------
import online_gateways as _og
import notif_settings
import frenzyex as _frenzyex
import tronado as _tronado
import tonpays as _tonpays

# field → (برچسب, نوع)
_GW_FIELDS = {
    "frenzyex": {
        "api_key": ("🔑 API Key (frx_live_...)", "secret"),
        "callback_secret": ("🔏 Callback Secret", "secret"),
        "base_url": ("🌐 Base URL", "url"),
        "usd_rate_toman": ("💵 نرخ USD/USDT به تومان (۰ = ارسال نشود)", "int0"),
        "min_amount": ("💰 حداقل مبلغ (تومان)", "int"),
        "description": ("📝 توضیح فاکتور", "text"),
        "public_url": ("📡 آدرس عمومی HTTPS ربات (برای Callback)", "url_opt"),
    },
    "tronado": {
        "api_key": ("🔑 API Key (x-api-key)", "secret"),
        "ipn_signing_key": ("🔏 کلید امضای IPN", "secret"),
        "wallet_address": ("👛 آدرس کیف پول ترون (TRC20)", "address"),
        "wage_percent": ("🧮 سهم کارمزد روی فروشنده (۰ تا ۱۰۰ درصد)", "wage"),
        "min_amount": ("💰 حداقل مبلغ (تومان)", "int"),
        "base_url": ("🌐 Base URL", "url"),
        "public_url": ("📡 آدرس عمومی HTTPS ربات (برای Callback)", "url_opt"),
    },
    "tonpays": {
        "api_key": ("🔑 API Key (X-API-Key، کلید استاندارد)", "secret"),
        "custom_tg_key": ("🔑 کلید «تلگرام کاستوم» (فقط برای حالت تلگرام)", "secret"),
        "webhook_secret": ("🔏 سکرت امضای وب‌هوک (اختیاری)", "secret"),
        "base_url": ("🌐 Base URL", "url"),
        "min_amount": ("💰 حداقل مبلغ (تومان)", "int"),
        "public_url": ("📡 آدرس عمومی HTTPS ربات (برای وب‌هوک)", "url_opt"),
    },
}


def _gw_mask(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return "تنظیم نشده"
    return "••••" + v[-4:] if len(v) >= 8 else "••••"


def _gw_screen(gw: str):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    pm = _pm()
    if gw == "frenzyex":
        c = pm.frenzyex
        cb = _og.callback_url("frenzyex")
        pm_label = {"auto": "خودکار (auto)", "standard": "فقط کارت‌به‌کارت (standard)", "nowpay": "فقط Crypto Collection (nowpay)"}.get(c.payment_method, c.payment_method)
        text = (
            "💳 درگاه FrenzyEx\n\n"
            f"وضعیت: {'✅ فعال' if c.enabled else '❌ غیرفعال'}\n"
            f"API Key: {_gw_mask(c.api_key)}\n"
            f"Callback Secret: {_gw_mask(c.callback_secret)}\n"
            f"Base URL: {c.base_url}\n"
            f"روش پرداخت مشتری: {pm_label}\n"
            f"لینک اصلی: {'checkout_url' if c.link_mode == 'checkout' else 'bot_pay_url (داخل چت ربات)'}\n"
            f"نرخ USD به تومان: {int(c.usd_rate_toman or 0):,}\n"
            f"حداقل مبلغ: {int(c.min_amount):,} تومان\n"
            f"توضیح فاکتور: {c.description or '-'}\n\n"
            f"📥 آدرس Callback که باید به FrenzyEx بدهید:\n{cb or '❌ آدرس عمومی ربات مشخص نیست؛ «آدرس عمومی» را ثبت کنید'}\n\n"
            "⚠️ بدون Callback Secret، کال‌بک‌ها رد می‌شوند (ولی استعلام خودکار وضعیت همچنان پرداخت‌ها را تأیید می‌کند).\n"
            "🔒 هر پرداخت با امضای معتبر + مبلغ مطابق، دقیقاً یک‌بار تحویل می‌شود."
        )
        kb = [
            [InlineKeyboardButton(text=("❌ غیرفعال کردن" if c.enabled else "✅ فعال کردن"), callback_data="paymt_frenzyex_toggle", style="primary")],
            [InlineKeyboardButton(text="🔑 API Key", callback_data="paymt_frenzyex_edit|api_key", style="primary"),
             InlineKeyboardButton(text="🔏 Callback Secret", callback_data="paymt_frenzyex_edit|callback_secret", style="primary")],
            [InlineKeyboardButton(text="🌐 Base URL", callback_data="paymt_frenzyex_edit|base_url", style="primary"),
             InlineKeyboardButton(text="📡 آدرس عمومی", callback_data="paymt_frenzyex_edit|public_url", style="primary")],
            [InlineKeyboardButton(text="💰 حداقل مبلغ", callback_data="paymt_frenzyex_edit|min_amount", style="primary"),
             InlineKeyboardButton(text="💵 نرخ USD", callback_data="paymt_frenzyex_edit|usd_rate_toman", style="primary")],
            [InlineKeyboardButton(text="📝 توضیح فاکتور", callback_data="paymt_frenzyex_edit|description", style="primary")],
            [InlineKeyboardButton(text=f"🔀 روش پرداخت: {c.payment_method}", callback_data="paymt_frenzyex_method", style="success"),
             InlineKeyboardButton(text=f"🔗 لینک: {'checkout' if c.link_mode == 'checkout' else 'bot'}", callback_data="paymt_frenzyex_link", style="success")],
            [InlineKeyboardButton(text="🧪 تست اتصال", callback_data="paymt_frenzyex_test", style="primary")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
        ]
    elif gw == "tonpays":
        c = pm.tonpays
        cb = _og.callback_url("tonpays")
        text = (
            "💎 درگاه تون‌پیز (TonPays)\n\n"
            f"وضعیت: {'✅ فعال' if c.enabled else '❌ غیرفعال'}\n"
            f"API Key: {_gw_mask(c.api_key)}\n"
            f"کلید تلگرام کاستوم: {_gw_mask(c.custom_tg_key)}\n"
            f"سکرت امضای وب‌هوک: {_gw_mask(c.webhook_secret)}\n"
            f"Base URL: {c.base_url}\n"
            f"لینک اصلی: {'صفحه‌ی وب (web_invoice_url)' if c.link_mode == 'web' else 'ربات تلگرام (invoice_url)'}\n"
            f"ارسال شناسه‌ی تلگرام خریدار: {'✅ بله' if c.send_buyer else '❌ خیر'}\n"
            f"حداقل مبلغ: {int(c.min_amount):,} تومان\n\n"
            f"📥 آدرس وب‌هوک (به‌صورت خودکار با هر فاکتور ارسال می‌شود):\n{cb or '❌ آدرس عمومی ربات مشخص نیست؛ «آدرس عمومی» را ثبت کنید'}\n\n"
            "ℹ️ سکرت امضا را از پروفایل ← کلید API ← ساخت سکرت امضا بگیرید. بدون سکرت، وب‌هوک‌ها با استعلام مستقیم از API تأیید می‌شوند.\n"
            "🔒 فقط وضعیت completed با paid=true تحویل می‌شود؛ مبلغ کمتر/بیشتر به کاربر/بررسی دستی ادمین می‌رود.\n"
            "📚 مستندات: https://doc.tonpays.online"
        )
        kb = [
            [InlineKeyboardButton(text=("❌ غیرفعال کردن" if c.enabled else "✅ فعال کردن"), callback_data="paymt_tonpays_toggle", style="primary")],
            [InlineKeyboardButton(text="🔑 API Key", callback_data="paymt_tonpays_edit|api_key", style="primary"),
             InlineKeyboardButton(text="🔑 کلید تلگرام کاستوم", callback_data="paymt_tonpays_edit|custom_tg_key", style="primary")],
            [InlineKeyboardButton(text="🔏 سکرت وب‌هوک", callback_data="paymt_tonpays_edit|webhook_secret", style="primary")],
            [InlineKeyboardButton(text="🌐 Base URL", callback_data="paymt_tonpays_edit|base_url", style="primary"),
             InlineKeyboardButton(text="📡 آدرس عمومی", callback_data="paymt_tonpays_edit|public_url", style="primary")],
            [InlineKeyboardButton(text="💰 حداقل مبلغ", callback_data="paymt_tonpays_edit|min_amount", style="primary")],
            [InlineKeyboardButton(text=f"🔗 لینک: {'وب' if c.link_mode == 'web' else 'تلگرام'}", callback_data="paymt_tonpays_link", style="success"),
             InlineKeyboardButton(text=f"{'✅' if c.send_buyer else '❌'} شناسه‌ی خریدار", callback_data="paymt_tonpays_buyer", style="success")],
            [InlineKeyboardButton(text="🧪 تست اتصال", callback_data="paymt_tonpays_test", style="primary")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
        ]
    else:
        c = pm.tronado
        ipn, disp = _og.callback_url("tronado"), _og.dispute_callback_url()
        addr_ok = _tronado.is_valid_tron_address(c.wallet_address) if c.wallet_address else False
        text = (
            "🌪 درگاه ترونادو (Tronado)\n\n"
            f"وضعیت: {'✅ فعال' if c.enabled else '❌ غیرفعال'}\n"
            f"API Key: {_gw_mask(c.api_key)}\n"
            f"کلید امضای IPN: {_gw_mask(c.ipn_signing_key)}\n"
            f"کیف پول ترون: {c.wallet_address or 'تنظیم نشده'}"
            + ("" if (not c.wallet_address or addr_ok) else "  ❌ نامعتبر (حروف بزرگ/کوچک مهم است)") + "\n"
            f"سهم کارمزد روی فروشنده: {int(c.wage_percent)}٪\n"
            f"حداقل مبلغ: {int(c.min_amount):,} تومان\n"
            f"Base URL: {c.base_url}\n\n"
            f"📥 آدرس IPN پرداخت:\n{ipn or '❌ آدرس عمومی ربات مشخص نیست؛ «آدرس عمومی» را ثبت کنید'}\n"
            f"⚖️ آدرس Callback اعتراض (اختیاری):\n{disp or '-'}\n\n"
            "⚠️ دامنه‌ی Callback باید پیش از استفاده توسط پشتیبانی ترونادو (@TronadoSupp) ثبت و تأیید شود؛ "
            "بدون آن کال‌بک ارسال نمی‌شود (استعلام خودکار وضعیت همچنان کار می‌کند، ولی با wage بالاتر از ۰ نیاز به کال‌بک دارد).\n"
            "ℹ️ طبق مستندات ترونادو هر کاربر روزانه ۱ تراکنش و سقف ۵۰۰ هزار تومان (ماهانه ۱ میلیون) دارد؛ تغییرش با پشتیبانی ترونادو است.\n"
            "👛 کیف پول ترونادو را با کیف پول بخش «ارز دیجیتال» یکی نکنید (احتمال شارژ دوباره از یک واریز)."
        )
        kb = [
            [InlineKeyboardButton(text=("❌ غیرفعال کردن" if c.enabled else "✅ فعال کردن"), callback_data="paymt_tronado_toggle", style="primary")],
            [InlineKeyboardButton(text="🔑 API Key", callback_data="paymt_tronado_edit|api_key", style="primary"),
             InlineKeyboardButton(text="🔏 کلید امضای IPN", callback_data="paymt_tronado_edit|ipn_signing_key", style="primary")],
            [InlineKeyboardButton(text="👛 آدرس کیف پول", callback_data="paymt_tronado_edit|wallet_address", style="primary"),
             InlineKeyboardButton(text="🧮 سهم کارمزد", callback_data="paymt_tronado_edit|wage_percent", style="primary")],
            [InlineKeyboardButton(text="💰 حداقل مبلغ", callback_data="paymt_tronado_edit|min_amount", style="primary"),
             InlineKeyboardButton(text="📡 آدرس عمومی", callback_data="paymt_tronado_edit|public_url", style="primary")],
            [InlineKeyboardButton(text="🌐 Base URL", callback_data="paymt_tronado_edit|base_url", style="primary")],
            [InlineKeyboardButton(text="🧪 تست اتصال", callback_data="paymt_tronado_test", style="primary")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
        ]
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def _gw_show(callback: types.CallbackQuery, gw: str):
    text, kb = _gw_screen(gw)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "paymt_frenzyex")
async def paymt_frenzyex_open(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await _gw_show(callback, "frenzyex")


@router.callback_query(F.data == "paymt_tronado")
async def paymt_tronado_open(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await _gw_show(callback, "tronado")


@router.callback_query(F.data == "paymt_tonpays")
async def paymt_tonpays_open(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    await _gw_show(callback, "tonpays")


@router.callback_query(F.data.in_({"paymt_tonpays_link", "paymt_tonpays_buyer"}))
async def paymt_tonpays_switches(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm()
    if callback.data.endswith("_link"):
        new_mode = "telegram" if pm.tonpays.link_mode == "web" else "web"
        pm.update_tonpays(link_mode=new_mode)
        if new_mode == "telegram" and not pm.tonpays.custom_tg_key:
            await callback.message.answer("⚠️ حالت تلگرام به «کلید تلگرام کاستوم» نیاز دارد؛ تا ثبت نشود فاکتور به‌صورت لینک وب ساخته می‌شود.")
    else:
        pm.update_tonpays(send_buyer=not pm.tonpays.send_buyer)
    await _gw_show(callback, "tonpays")


@router.callback_query(F.data.in_({"paymt_frenzyex_toggle", "paymt_tronado_toggle", "paymt_tonpays_toggle"}))
async def paymt_gateway_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    gw = "frenzyex" if "frenzyex" in callback.data else ("tonpays" if "tonpays" in callback.data else "tronado")
    pm = _pm()
    cur = {"frenzyex": pm.frenzyex, "tronado": pm.tronado, "tonpays": pm.tonpays}[gw]
    if not cur.enabled:
        missing = []
        if not (cur.api_key or "").strip():
            missing.append("API Key")
        if gw == "tronado":
            if not (cur.wallet_address or "").strip():
                missing.append("آدرس کیف پول")
            elif not _tronado.is_valid_tron_address(cur.wallet_address):
                missing.append("آدرس کیف پول معتبر")
            if not (cur.ipn_signing_key or "").strip():
                missing.append("کلید امضای IPN")
        if gw == "frenzyex" and not (cur.callback_secret or "").strip():
            missing.append("Callback Secret")
        hard = [m for m in missing if m in ("API Key", "آدرس کیف پول", "آدرس کیف پول معتبر")]
        if hard:
            await callback.answer("❌ ابتدا این موارد را تنظیم کنید: " + "، ".join(hard), show_alert=True)
            return
        if missing:
            await callback.answer("⚠️ فعال شد ولی «" + "، ".join(missing) + "» هنوز تنظیم نشده (کال‌بک رد می‌شود).", show_alert=True)
        else:
            await callback.answer()
    else:
        await callback.answer()
    if gw == "frenzyex":
        pm.update_frenzyex(enabled=not pm.frenzyex.enabled)
    elif gw == "tonpays":
        pm.update_tonpays(enabled=not pm.tonpays.enabled)
    else:
        pm.update_tronado(enabled=not pm.tronado.enabled)
    await _gw_show(callback, gw)


@router.callback_query(F.data == "paymt_frenzyex_method")
async def paymt_frenzyex_method(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm()
    order = ["auto", "standard", "nowpay"]
    cur = pm.frenzyex.payment_method if pm.frenzyex.payment_method in order else "auto"
    pm.update_frenzyex(payment_method=order[(order.index(cur) + 1) % 3])
    await _gw_show(callback, "frenzyex")


@router.callback_query(F.data == "paymt_frenzyex_link")
async def paymt_frenzyex_link(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm()
    pm.update_frenzyex(link_mode=("bot" if pm.frenzyex.link_mode == "checkout" else "checkout"))
    await _gw_show(callback, "frenzyex")


@router.callback_query(F.data.in_({"paymt_frenzyex_test", "paymt_tronado_test", "paymt_tonpays_test"}))
async def paymt_gateway_test(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("⏳ در حال تست...")
    import asyncio as _aio
    if "tonpays" in callback.data:
        _ok, _m = await _aio.to_thread(_tonpays.test_connection)
        msg = ("✅ " if _ok else "❌ ") + _m
    elif "frenzyex" in callback.data:
        def _t():
            import requests as _rq
            try:
                r = _rq.get(_frenzyex.base_url() + "/api/v1/health", timeout=(8, 15), allow_redirects=False)
                ok = r.status_code == 200 and (r.json() or {}).get("ok") is True
                return ("✅ سرور FrenzyEx در دسترس است (/api/v1/health)." if ok else f"❌ پاسخ نامعتبر (HTTP {r.status_code})")
            except Exception as exc:
                return f"❌ خطای ارتباط: {type(exc).__name__}"
        msg = await _aio.to_thread(_t)
        msg += "\nℹ️ اعتبار API Key فقط هنگام ساخت اولین فاکتور واقعی تأیید می‌شود."
    else:
        price = await _aio.to_thread(_tronado.get_tron_price_toman, False)
        msg = (f"✅ ارتباط با ترونادو برقرار است. قیمت هر TRX: {price:,.0f} تومان" if price else "❌ دریافت قیمت ترون از ترونادو ناموفق بود (Base URL/اینترنت سرور را بررسی کنید).")
        c = _pm().tronado
        if c.wallet_address and not _tronado.is_valid_tron_address(c.wallet_address):
            msg += "\n❌ آدرس کیف پول معتبر نیست."
        msg += "\nℹ️ اعتبار API Key هنگام ساخت اولین فاکتور واقعی تأیید می‌شود."
    try:
        await callback.message.answer(msg)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^paymt_(frenzyex|tronado|tonpays)_edit\|[a-z_]+$"))
async def paymt_gateway_edit_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    head, field = callback.data.split("|", 1)
    gw = "frenzyex" if "frenzyex" in head else ("tonpays" if "tonpays" in head else "tronado")
    spec = _GW_FIELDS[gw].get(field)
    if spec is None:
        return
    await state.set_state(AdminStates.waiting_gateway_field)
    await state.update_data(gw_edit=gw, gw_field=field)
    hint = {
        "secret": "\n(پیام شما بعد از ذخیره حذف می‌شود.)",
        "url_opt": "\nبرای حذف مقدار، «-» بفرستید.",
        "int0": "\nعدد صحیح (۰ = غیرفعال).",
        "int": "\nعدد صحیح به تومان.",
        "wage": "\nعدد ۰ تا ۱۰۰.",
        "address": "\nآدرس ۳۴ کاراکتری که با T شروع می‌شود (حروف بزرگ/کوچک مهم است).",
    }.get(spec[1], "")
    await callback.message.edit_text(f"{spec[0]} را ارسال کنید:{hint}")


@router.message(AdminStates.waiting_gateway_field)
async def paymt_gateway_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    gw, field = data.get("gw_edit"), data.get("gw_field")
    spec = (_GW_FIELDS.get(gw) or {}).get(field)
    if spec is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    kind = spec[1]
    value = None
    err = None
    if kind in ("secret", "text", "url", "url_opt", "address") and not raw:
        err = "مقدار خالی است."
    elif kind == "secret" and (not raw.isascii() or any(c.isspace() for c in raw)):
        err = "کلید فقط باید شامل حروف انگلیسی/عدد/علامت و بدون فاصله و متن فارسی باشد؛ دقیقاً از پنل درگاه کپی کنید."
    elif kind in ("int", "int0", "wage"):
        digits = raw.replace(",", "")
        digits = digits.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        if not digits.isdigit():
            err = "فقط عدد صحیح ارسال کنید."
        else:
            n = int(digits)
            if kind == "wage" and not 0 <= n <= 100:
                err = "عدد باید بین ۰ و ۱۰۰ باشد."
            elif kind == "int" and n < 1000:
                err = "حداقل مبلغ باید حداقل ۱۰۰۰ تومان باشد."
            else:
                value = n
    elif kind in ("url", "url_opt"):
        if kind == "url_opt" and raw == "-":
            value = ""
        else:
            v = raw if raw.lower().startswith(("http://", "https://")) else "https://" + raw
            if " " in v or "." not in v.split("//", 1)[-1]:
                err = "آدرس نامعتبر است."
            else:
                value = v.rstrip("/")
    elif kind == "address":
        if not _tronado.is_valid_tron_address(raw):
            err = "آدرس ترون نامعتبر است (باید با T شروع شود، ۳۴ کاراکتر و Base58Check معتبر؛ حروف بزرگ/کوچک را تغییر ندهید)."
        else:
            crypto_wallets = [str(c.wallet or "").strip().lower() for c in _pm().cryptos if c.symbol.upper() == "TRX"]
            if raw.lower() in crypto_wallets:
                err = "این آدرس با کیف پول TRX بخش «ارز دیجیتال» یکی است؛ برای جلوگیری از شارژ دوباره، آدرس جداگانه‌ای بدهید."
            else:
                value = raw
    else:
        value = raw
    if err:
        await message.answer(f"❌ {err}\nدوباره ارسال کنید یا /cancel بزنید.")
        return
    if gw == "frenzyex":
        _pm().update_frenzyex(**{field: value})
    elif gw == "tonpays":
        _pm().update_tonpays(**{field: value})
    else:
        _pm().update_tronado(**{field: value})
    await state.clear()
    if kind == "secret":
        try:
            await message.delete()
        except Exception:
            pass
    await message.answer("✅ ذخیره شد.")
    text, kb = _gw_screen(gw)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "paymt_crypto")
async def paymt_crypto_open(callback: types.CallbackQuery):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); cryptos = pm.cryptos
    active_cnt = sum(1 for c in cryptos if c.enabled)
    text = (
        f"💎 مدیریت ارزهای دیجیتال\n\n"
        f"وضعیت کل بخش: {'✅ فعال' if pm.crypto_master_enabled else '❌ غیرفعال'}\n"
        f"تعداد کل: {len(cryptos)} ارز | فعال: {active_cnt}\n\n"
        "برای تنظیمات هر ارز روی آن کلیک کنید."
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_crypto_list_menu(cryptos, pm.crypto_master_enabled))
    except Exception:
        pass


@router.callback_query(F.data == "paymt_crypto_master")
async def paymt_crypto_master_toggle(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm()
    pm.set_crypto_master_enabled(not pm.crypto_master_enabled)
    await paymt_crypto_open(callback)


@router.callback_query(F.data.startswith("paymt_toggle:"))
async def paymt_crypto_toggle(callback: types.CallbackQuery):
    symbol = callback.data.split(":", 1)[1]
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); c = pm.get_crypto(symbol)
    if c:
        c.enabled = not c.enabled
        pm._save_cryptos()
    await paymt_crypto_open(callback)


@router.callback_query(F.data.startswith("paymt_del_coin:"))
async def paymt_del_coin(callback: types.CallbackQuery):
    """حذف واقعی ارز؛ callback قبلی فقط ساخته می‌شد و Handler نداشت."""
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    symbol = callback.data.split(":", 1)[1].strip().upper()
    if not symbol:
        return
    pm = _pm()
    removed = pm.remove_crypto(symbol)
    if not removed:
        await callback.answer("❌ این ارز پیدا نشد یا قبلاً حذف شده است.", show_alert=True)
        return
    # Backup/fallback settings را هم پاک کن تا بعداً ارز حذف‌شده دوباره قیمت نگیرد.
    try:
        db.set_setting(f"crypto_backup_price_{symbol}", "")
        db.set_setting(f"crypto_backup_enabled_{symbol}", "0")
    except Exception:
        logger.exception("failed to clear crypto backup settings for %s", symbol)
    await callback.answer(f"✅ {symbol} حذف شد.")
    await paymt_crypto_open(callback)

@router.callback_query(F.data == "paymt_add_coin")
async def paymt_add_coin_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_crypto_symbol)
    try:
        await callback.message.edit_text(
            "💎 افزودن ارز دیجیتال جدید\n\n"
            "مرحله ۱/۴: نماد ارز را وارد کنید (مثال: SOL, MATIC, BNB, ETH)\n\n"
            "💡 قیمت از API خودکار دریافت می‌شود. Backup Price قابل تنظیم است."
        )
    except Exception:
        pass


@router.message(AdminStates.waiting_crypto_symbol)
async def paymt_add_coin_symbol(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    sym = (message.text or '').strip().upper()
    await state.update_data(new_coin_symbol=sym)
    await state.set_state(AdminStates.waiting_crypto_name)
    await message.answer("مرحله ۲/۴: نام ارز را وارد کنید (مثال: Solana):")


@router.message(AdminStates.waiting_crypto_name)
async def paymt_add_coin_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    edit_symbol = data.get("crypto_edit_symbol")
    name = (message.text or '').strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد.")
        return
    entities = _ui_editor_entities(message)
    custom_emoji_id = next((str(e.get("custom_emoji_id")) for e in entities
                            if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")), None)
    if edit_symbol:
        _pm().update_crypto(edit_symbol, name=name, custom_emoji_id=custom_emoji_id)
        await state.clear()
        c = _pm().get_crypto(edit_symbol)
        await message.answer(
            f"✅ نام {edit_symbol} به «{name}» تغییر کرد.",
            reply_markup=admin_crypto_coin_keyboard(c) if c else admin_crypto_list_menu(_pm().cryptos),
        )
        return
    await state.update_data(new_coin_name=name, new_coin_custom_emoji_id=custom_emoji_id)
    await state.set_state(AdminStates.waiting_crypto_network)
    await message.answer("مرحله ۳/۴: نام شبکه را وارد کنید (مثال: SOL):")


@router.message(AdminStates.waiting_crypto_network)
async def paymt_add_coin_network(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.update_data(new_coin_network=(message.text or '').strip())
    await state.set_state(AdminStates.waiting_crypto_wallet)
    await message.answer("مرحله ۴/۴: آدرس Wallet را وارد کنید (یا - برای خالی):")


@router.message(AdminStates.waiting_crypto_wallet)
async def paymt_add_coin_wallet(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    sym = data.get("new_coin_symbol", "")
    name = data.get("new_coin_name", sym)
    network = data.get("new_coin_network", sym)
    wallet_addr = (message.text or "").strip()
    if wallet_addr == '-':
        wallet_addr = ''
    await state.clear()
    pm = _pm()
    pm.add_crypto(symbol=sym, name=name, network=network, emoji="💎", wallet=wallet_addr, margin_percent=3.0, custom_emoji_id=data.get("new_coin_custom_emoji_id"))
    await message.answer(
        f"✅ ارز {sym} ({name}) اضافه شد.\n"
        f"شبکه: {network}\n"
        f"Wallet: {wallet_addr or 'تنظیم نشده'}",
        reply_markup=admin_crypto_list_menu(pm.cryptos),
    )


@router.callback_query(F.data.startswith("paymt_coin:"))
async def paymt_coin_open(callback: types.CallbackQuery):
    symbol = callback.data.split(":", 1)[1]
    await callback.answer()  # FAST
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); c = pm.get_crypto(symbol)
    if not c:
        return
    st = "✅ فعال" if c.enabled else "❌ غیرفعال"
    backup_text = f"{c.backup_price:,.0f} تومان" if c.backup_price else "تنظیم نشده"
    wallet_display = c.wallet if c.wallet else "تنظیم نشده"
    from keyboards import admin_crypto_coin_keyboard
    text = (
        f"{c.emoji} تنظیمات ارز {c.symbol} ({c.name})\n"
        f"شبکه: {c.network}\n"
        f"وضعیت: {st}\n"
        f"Wallet: {wallet_display}\n"
        f"Backup Price: {backup_text}\n"
        f"Margin: {c.margin_percent}%"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_crypto_coin_keyboard(c))
    except Exception:
        pass


@router.callback_query(F.data.startswith("paymt_coin_toggle:"))
async def paymt_coin_toggle(callback: types.CallbackQuery):
    symbol = callback.data.split(":", 1)[1]
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); c = pm.get_crypto(symbol)
    if c:
        c.enabled = not c.enabled
        pm._save_cryptos()
    await paymt_coin_open(callback.model_copy(update={"data": f"paymt_coin:{symbol}"}))


@router.callback_query(F.data.startswith("paymt_coin_name:"))
async def paymt_coin_name_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    symbol = callback.data.split(":", 1)[1].strip().upper()
    c = _pm().get_crypto(symbol)
    if not c:
        await callback.answer("❌ ارز پیدا نشد.", show_alert=True)
        return
    await state.update_data(crypto_edit_symbol=symbol)
    await state.set_state(AdminStates.waiting_crypto_name)
    await callback.message.edit_text(
        f"✏️ نام ارز {symbol} فعلی: {c.name}\n\nنام جدید را بفرستید. می‌توانید Premium Emoji هم همراه نام ارسال کنید.",
        reply_markup=admin_back_button(),
    )

@router.callback_query(F.data.startswith("paymt_coin_wallet:"))
async def paymt_coin_wallet_start(callback: types.CallbackQuery, state: FSMContext):
    symbol = callback.data.split(":", 1)[1]
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); c = pm.get_crypto(symbol)
    current = c.wallet if c else ''
    await state.update_data(edit_coin_symbol=symbol)
    await state.set_state(AdminStates.waiting_crypto_wallet_edit)
    await callback.message.edit_text(
        f"💼 ویرایش Wallet برای {symbol}\n"
        f"آدرس فعلی: {current or 'تنظیم نشده'}\n\n"
        "آدرس جدید را وارد کنید:"
    )


@router.message(AdminStates.waiting_crypto_wallet_edit)
async def paymt_coin_wallet_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    symbol = data.get("edit_coin_symbol", "")
    wallet_addr = (message.text or "").strip()
    pm = _pm(); c = pm.get_crypto(symbol)
    if c:
        c.wallet = wallet_addr
        pm._save_cryptos()
    await state.clear()
    from keyboards import admin_crypto_coin_keyboard
    await message.answer(f"✅ Wallet {symbol} ذخیره شد.", reply_markup=admin_crypto_coin_keyboard(c) if c else None)


@router.callback_query(F.data.startswith("paymt_coin_backup:"))
async def paymt_coin_backup_start(callback: types.CallbackQuery, state: FSMContext):
    symbol = callback.data.split(":", 1)[1]
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    pm = _pm(); c = pm.get_crypto(symbol)
    current_text = f"{c.backup_price:,.0f} تومان" if (c and c.backup_price) else "تنظیم نشده"
    await state.update_data(edit_coin_symbol=symbol)
    await state.set_state(AdminStates.waiting_crypto_backup_price)
    await callback.message.edit_text(
        f"💰 Backup Price برای {symbol}\n"
        f"قیمت فعلی: {current_text}\n\n"
        "قیمت Backup را به تومان وارد کنید:"
    )


@router.message(AdminStates.waiting_crypto_backup_price)
async def paymt_coin_backup_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    symbol = data.get("edit_coin_symbol", "")
    try:
        price = float((message.text or "0").replace(",", ""))
    except ValueError:
        await message.answer("❌ فقط عدد معتبر وارد کنید.")
        return
    pm = _pm(); c = pm.get_crypto(symbol)
    if c:
        c.backup_price = price
        pm._save_cryptos()
    await state.clear()
    from keyboards import admin_crypto_coin_keyboard
    await message.answer(f"✅ Backup Price {symbol} ذخیره شد.", reply_markup=admin_crypto_coin_keyboard(c) if c else None)

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY DISPATCH — Reply Keyboard → Inline Category Menu
# ─────────────────────────────────────────────────────────────────────────────

from keyboards import (
    admin_cat_users_keyboard,
    admin_cat_finance_keyboard,
    admin_cat_service_keyboard,
    admin_cat_comm_keyboard,
    admin_cat_appearance_keyboard,
    admin_cat_system_keyboard,
)

_CAT_MAP = {
    "👥 کاربران و سفارشات": (admin_cat_users_keyboard,   "👥 کاربران و سفارشات"),
    "💰 فروش و مالی":       (admin_cat_finance_keyboard,  "💰 فروش و مالی"),
    "🛠 سرویس و پنل":       (admin_cat_service_keyboard,  "🛠 سرویس و پنل"),
    "📢 ارتباط و تبلیغات":  (admin_cat_comm_keyboard,     "📢 ارتباط و تبلیغات"),
    "🎨 ظاهر و محتوا":      (admin_cat_appearance_keyboard,"🎨 ظاهر و محتوا"),
    "⚙️ سیستم و آمار":      (admin_cat_system_keyboard,   "⚙️ سیستم و آمار"),
}


def _filter_kb_for_subadmin(kb, user_id: int):
    """دکمه‌هایی که ادمین فرعی به آن‌ها دسترسی ندارد از منوی دسته حذف می‌شوند (به‌جای «دسترسی ندارید» بعد از کلیک)."""
    perms = _current_admin_permissions(user_id) or set()
    rows = []
    for row in kb.inline_keyboard:
        kept = []
        for b in row:
            need = _permission_for_callback(getattr(b, "callback_data", None)) if getattr(b, "callback_data", None) else None
            if need is None or need in perms or (need == "receipts" and "requests" in perms):
                kept.append(b)
        if kept:
            rows.append(kept)
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.in_(list(_CAT_MAP.keys())))
async def admin_category_dispatch(message: types.Message, state: FSMContext):
    """Route 6 reply-keyboard category buttons to their inline sub-menus."""
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    key = (message.text or "").strip()
    kb_fn, label = _CAT_MAP.get(key, (None, key))
    if kb_fn is None:
        return
    _ADMIN_LAST_CAT[message.from_user.id] = key
    try:
        kb = kb_fn()
    except Exception as e:
        logging.exception("admin category keyboard build failed: %s", e)
        await message.answer("❌ ساخت منوی این بخش با خطا روبه‌رو شد. لاگ را بررسی کن.")
        return
    if message.from_user.id != ADMIN_ID:
        kb = _filter_kb_for_subadmin(kb, message.from_user.id)
        if not kb.inline_keyboard:
            await message.answer("⛔ به هیچ‌کدام از گزینه‌های این بخش دسترسی ندارید.")
            return
    await message.answer(f"📂 {label}\n\nیکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=kb)


# ---------------------------------------------------------------------------
# 🔎 /emojidiag — گزارش اینکه کدام ایموجی‌های هر صفحه «پریمیوم ذخیره‌شده» است و کدام عادی مانده.
# فقط برای ادمین. برای پیدا کردن دقیق ایموجی‌هایی که هنوز عادی نمایش داده می‌شوند.
# ---------------------------------------------------------------------------
def _diag_plain_emojis(text: str, entities: list) -> tuple[list[str], int]:
    covered = set()
    n_custom = 0
    for e in entities or []:
        if e.get("type") == "custom_emoji":
            n_custom += 1
            for u in range(int(e["offset"]), int(e["offset"]) + int(e["length"])):
                covered.add(u)
    plain, pos = [], 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        is_emoji = (ord(ch) >= 0x1F000 or 0x2190 <= ord(ch) <= 0x2BFF) and not ch.isalnum()
        if is_emoji and pos not in covered:
            plain.append(ch)
        pos += width
    return plain, n_custom


def _diag_final_plain(text: str) -> str:
    """شبیه‌سازی خروجی نهایی: rich_registry روی متن اعمال می‌شود و ایموجی‌های بدون پریمیوم برمی‌گردد."""
    import rich_registry
    ents = rich_registry.entities_for(text)
    plain, _n = _diag_plain_emojis(text, ents)
    return " ".join(plain) if plain else "هیچ"


# ایموجی‌هایی که «کد» خودش داخل متن می‌گذارد (نه ادمین در قالب ویرایشگر): وضعیت سرویس، نوار مصرف و …
# اینها تنها وقتی پریمیوم نمایش داده می‌شوند که ادمین یک‌بار نسخه‌ی پریمیومشان را برای ربات بفرستد.
_DYNAMIC_EMOJI_SYMBOLS = [
    ("⚪️", "وضعیت سرویس: نامشخص"),
    ("✅", "وضعیت سرویس: فعال"),
    ("❌", "وضعیت سرویس: منقضی‌شده"),
    ("🟩", "نوار مصرف: بخش پر (سبز)"),
    ("🟨", "نوار مصرف: بخش پر (≥۸۰٪)"),
    ("🟥", "نوار مصرف: بخش پر (≥۹۰٪)"),
    ("⬜️", "نوار مصرف: بخش خالی"),
    ("🌿", "جزئیات سرویس: کنار موقعیت"),
]


@router.message(Command("emojisetup"))
async def emoji_setup(message: types.Message):
    """🎛 فهرست ایموجی‌های «پویا» (وضعیت سرویس، نوار مصرف، …) و اینکه کدام‌شان هنوز پریمیوم ندارد.
    ادمین کافی است همان خط آماده را با ایموجی‌های پریمیوم دلخواه‌اش به ربات بفرستد؛ همه‌ی پیام‌های
    ادمین خودکار یاد گرفته می‌شود و از همان لحظه در تمام پیام‌های ربات اعمال می‌شود."""
    if not _is_admin(message.from_user.id):
        return
    import rich_registry
    rich_registry._load()
    lines = ["🎛 ایموجی‌های پویای ربات (وضعیت سرویس، نوار مصرف، …)\n"]
    missing = []
    for sym, label in _DYNAMIC_EMOJI_SYMBOLS:
        ok = rich_registry.has_premium_for(sym)
        lines.append(f"{'✅' if ok else '⚠️'} {sym}  {label} — {'پریمیوم دارد' if ok else 'هنوز عادی است'}")
        if not ok:
            missing.append(sym)
    if missing:
        lines.append(
            "\nبرای پریمیوم‌شدن موارد ⚠️، پیام بعدی را کپی کن، به‌جای هر ایموجی، ایموجی پریمیوم دلخواهت را بگذار "
            "و برای ربات بفرست (همه‌ی ایموجی‌ها را در یک پیام بفرست؛ ربات خودش یاد می‌گیرد):"
        )
        lines.append("\n" + " ".join(missing))
    else:
        lines.append("\n🎉 همه‌ی ایموجی‌های پویا پریمیوم دارند. برای عوض‌کردن هرکدام، نسخه‌ی پریمیوم جدیدش را برای ربات بفرست.")
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("emojidiag"))
async def emoji_diag(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    import rich_registry
    rich_registry._load()
    lines = ["🔎 گزارش ایموجی پریمیوم\n",
             f"رجیستری: {len(rich_registry._map)} تکه، {len(rich_registry._char_map)} ایموجی یادگرفته‌شده\n",
             "۱) آخرین متن‌های ذخیره‌شده در ویرایشگر:"]
    try:
        cur = db.get_connection().cursor()
        cur.execute("SELECT key, text, entities_json FROM ui_text_overrides ORDER BY updated_at DESC LIMIT 15")
        for key, text, ej in cur.fetchall():
            try:
                ents = json.loads(ej or "[]")
            except Exception:
                ents = []
            plain, n_custom = _diag_plain_emojis(text or "", ents)
            lines.append(f"• {key}: {n_custom} پریمیوم | عادی: {' '.join(plain) if plain else 'هیچ'}")
    except Exception as exc:
        lines.append(f"خطا: {exc}")

    lines.append("\n۲) خروجی نهایی که کاربر می‌بیند (ایموجی‌هایی که عادی می‌مانند):")
    try:
        prof = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "0", _ui_editor.get_text("profile", _ui_editor.SCREENS["profile"]["default"]))
        lines.append(f"• پروفایل: {_diag_final_plain(prof)}")
        sample = ("📊 وضعیت سرویس : ⚪️ نامشخص\n👤 نام سرویس : x\n\n🌍 موقعیت سرویس : y 🌿\n📂 نام محصول : z\n\n"
                  "🔋 ترافیک : 1\n☑️ حجم مصرفی : 0\n💢 حجم باقی مانده : 1\n🟩🟩⬜️⬜️ ۰٪ مصرف شده\n\n🗓 تاریخ اتمام : 1")
        cd = _ui_editor.get_text("config_detail", sample) if _ui_editor.has_text_override("config_detail") else sample
        cd = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "0", cd)
        lines.append(f"• جزئیات سرویس: {_diag_final_plain(cd)}")
        lines.append(f"• فهرست راهنما: {_diag_final_plain('📚 راهنما و آموزش‌ها 👇')}")
    except Exception as exc:
        lines.append(f"خطا: {exc}")

    lines.append("\n۳) عنوان راهنماها:")
    for g in db.get_guides():
        title = str(g.get("title") or "")
        eid = db.get_button_custom_emoji_id("text:" + title)
        icon, _t = rich_registry.button_icon(title)
        ents = g.get("body_entities") or []
        n_body = sum(1 for e in ents if e.get("type") == "custom_emoji")
        lines.append(f"• {title} — id ذخیره‌شده: {'دارد' if eid else 'ندارد'} | آیکن نهایی دکمه: {'دارد' if (eid or icon) else 'ندارد'} | پریمیوم متن: {n_body}")
    await message.answer("\n".join(lines)[:4000], parse_mode=None)
