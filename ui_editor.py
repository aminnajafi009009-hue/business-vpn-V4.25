"""ویرایشگر کامل رابط کاربری ربات.

این فایل عمداً فقط رابط کاربری مشتری را مدیریت می‌کند؛ بخش «مدیریت/پنل ادمین»
از ویرایشگر حذف شده تا تغییرات ادمین از این قسمت آسیب نبیند.

امکانات:
- ویرایش متن صفحه‌ها با حفظ MessageEntity و Custom Emoji (Premium Emoji)
- ویرایش نام تمام دکمه‌های ثبت‌شده
- آیکن Custom Emoji برای دکمه‌ها (در Bot APIهای جدید)
- جابه‌جایی دکمه‌ها
- مخفی/نمایش کردن دکمه‌ها
- تنظیم تعداد دکمه در هر ردیف؛ پیش‌فرض همیشه یک دکمه در هر ردیف است
- نگهداری تنظیمات در دیتابیس
"""

import datetime
import json
import os
import logging
import re
import threading
from typing import Any

import database as db
import cache
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# ⚡ PERFORMANCE:
# _ensure() used to execute a full DB transaction containing several
# CREATE TABLE IF NOT EXISTS / ALTER TABLE statements on EVERY outgoing
# user message because bot.py globally routes send/edit calls through
# apply_auto_text(). With Turso this is a network round-trip per message and
# was the main source of the 2-3 second latency.
#
# Keep the editor schema initialized once per process. Writes/migrations still
# use the same transaction; only the repeated read-path initialization is
# removed.
_SCHEMA_READY = False
_SCHEMA_INIT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# رجیستری صفحه‌های مشتری
# ---------------------------------------------------------------------------
# callbackهایی که با _ تمام می‌شوند، الگوی callbackهای داینامیک هستند؛ مثلاً
# buy_ برای buy_plan_a / buy_plan_b.
SCREENS: dict[str, dict[str, Any]] = {
    # admin_panel: حذف شده از Editor چون Reply Keyboard ادمین به ۶ دسته تبدیل شد.
    # (backward compat: screens_keyboard و get_order همچنان کار می‌کنند)
    # "admin_panel": {...},
    "main_reply": {"category": "start", "label": "⌨️ منوی پایین کاربر", "default": "", "buttons": [
        ("plans", "🛒 خرید اشتراک"), ("free_test", "🎁 تست رایگان"), ("services", "📱 سرویس‌های من"), ("renew", "🔁 تمدید سرویس"),
        ("wallet", "💰 کیف پول"), ("referral", "👥 دعوت دوستان"), ("profile", "👤 پروفایل من"),
        ("support", "👨‍💻 پشتیبانی"), ("guides", "راهنما"), ("agency", "🤝 درخواست نمایندگی"), ("agent_manage", "🤝 مدیریت نمایندگی"),
    ]},
    "start": {"category": "start", "label": "🚀 شروع و منوی اصلی", "default": "👋 خوش آمدید", "buttons": []},
    "buy_plans": {"category": "shop", "label": "🛒 متن معرفی خرید اشتراک", "default": "🛒 انتخاب سرویس مناسب", "buttons": [
        ("plans_vip", "سرور VIP (V2Ray)"), ("cbuild_start", "🛠 کانفیگ خودتو بساز"), ("back", "🔙 بازگشت"),
    ]},
    "plans": {"category": "shop", "label": "🛒 خرید اشتراک", "default": "🛒 انتخاب سرویس مناسب", "buttons": [
        ("plans_vip", "سرور VIP (V2Ray)"),
        ("cbuild_start", "🛠 کانفیگ خودتو بساز (ویژه VIP)"), ("payg_buy", "⚡ خرید سرویس PAYG"),
        ("back", "🔙 بازگشت به منوی اصلی"),
    ]},
    "free_test_intro": {"category": "shop", "label": "🎁 توضیح تست رایگان (یک پلن، قبل از دریافت)", "default": (
        "🎁 {plan_name}\n\n"
        "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
        "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
        "⚡️ روی دکمه‌ی سبز زیر بزن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
    ), "buttons": []},
    "free_test_list_intro": {"category": "shop", "label": "🎁 توضیح تست رایگان (چند پلن، انتخاب از لیست)", "default": (
        "🎁 تست رایگان\n\n"
        "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
        "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
        "👇 یکی از تست‌های زیر را انتخاب کن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
    ), "buttons": []},
    "free_test_multi": {"category": "shop", "label": "🎁 تحویل میانبر تست‌ها (چند پلن، یک پیام)", "default": (
        "🎁 تست‌های رایگان شما آماده شد ✅\n\n"
        "{tests}\n\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید."
    ), "buttons": []},
    "free_test": {"category": "shop", "label": "🎁 تست رایگان", "default": "🎁 تست رایگان", "buttons": [
        ("pay_wallet_", "⚡️ همین الان تست رایگان بگیر"), ("plans", "🔙 بازگشت"),
    ]},
    "vip_category_list": {"category": "shop", "label": "⭐ دسته‌بندی VIP", "default": "⭐ دسته‌بندی‌های VIP", "buttons": [
        ("vipcat_", "دسته‌بندی VIP"), ("cbuild_start", "🛠 کانفیگ خودتو بساز"), ("plans", "🔙 بازگشت"),
    ]},
    "vip_plans": {"category": "shop", "label": "پلن‌های VIP", "default": "پلن‌های VIP", "buttons": [
        ("buy_", "📅 پلن"), ("plans_vip", "🔙 بازگشت به دسته‌بندی‌ها"),
    ]},
    "plan_select": {"category": "shop", "label": "📅 انتخاب پلن", "default": "📅 انتخاب پلن", "buttons": [
        ("buy_", "📅 پلن"), ("plans", "🔙 بازگشت"),
    ]},
    "payg_name": {"category": "shop", "label": "⚡ نام سرویس PAYG", "default": "", "buttons": [
        ("payg_name_auto", "🤖 انتخاب خودکار"), ("plans", "🔙 انصراف"),
    ]},
    "custom_build": {"category": "shop", "label": "🛠 کانفیگ خودتو بساز", "default": "🛠 کانفیگ خودتو بساز", "buttons": [
        ("cbuild_pay_wallet", "👛 پرداخت از کیف پول"), ("cbuild_pay_online", "🌐 پرداخت آنلاین"),
        ("cbuild_pay_card", "💳 کارت به کارت"), ("discount_cbuild", "🎟 ثبت کد تخفیف"),
        ("plans", "🔙 انصراف"),
    ]},
    "cbuild_payment_method": {"category": "shop", "label": "💳 روش پرداخت کانفیگ", "default": "💳 روش پرداخت", "buttons": [
        ("cbuild_pay_wallet", "👛 پرداخت از کیف پول"), ("cbuild_pay_online", "🌐 پرداخت آنلاین"),
        ("cbuild_pay_fx", "💳 پرداخت آنلاین (FrenzyEx)"), ("cbuild_pay_tr", "🌪 پرداخت آنلاین (ترونادو)"), ("cbuild_pay_tp", "💎 پرداخت آنلاین (تون‌پیز)"),
        ("cbuild_pay_card", "💳 کارت به کارت"), ("discount_cbuild", "🎟 ثبت کد تخفیف"), ("plans", "🔙 انصراف"),
    ]},
    "cbuild_pay_wallet": {"category": "finance", "label": "👛 پرداخت کیف پول", "default": "👛 پرداخت از کیف پول", "buttons": [
        ("cbuild_change_payment", "🔄 روش پرداخت دیگر"), ("plans", "🔙 بازگشت"),
    ]},
    "cbuild_pay_online": {"category": "finance", "label": "🌐 پرداخت آنلاین", "default": "🌐 پرداخت آنلاین", "buttons": [
        ("uq_copy_card", "📋 کپی شماره کارت (یونیک‌پی)"), ("uq_copy_amount", "📋 کپی مبلغ به ریال (یونیک‌پی)"), ("uq_pay", "💳 پرداخت در صفحه UniquePay"),
        ("gw_copy_amount", "📋 کپی مبلغ (تومان)"), ("gw_copy_trx", "📋 کپی مقدار TRX"), ("gw_copy_wallet", "👛 کپی ولت"),
        ("gw_pay_frenzyex", "💳 پرداخت در FrenzyEx"), ("gw_pay_tronado", "🌪 پرداخت در ترونادو"), ("gw_pay_tonpays", "💰 کارت به کارت(Tonpays)"), ("gw_open_bot", "🤖 باز کردن در چت ربات"),
        ("checkpay_", "✅ پرداخت را انجام دادم / بررسی کن"),
        ("cbuild_change_payment", "🔄 روش پرداخت دیگر"), ("plans", "🔙 بازگشت"),
    ]},
    "cbuild_pay_card": {"category": "finance", "label": "💳 پرداخت کارت به کارت", "default": "💳 پرداخت کارت به کارت", "buttons": [
        ("cbuild_change_payment", "🔄 روش پرداخت دیگر"), ("plans", "🔙 بازگشت"),
    ]},
    "plan_payment_method": {"category": "shop", "label": "💳 روش پرداخت خرید", "default": "💳 روش پرداخت", "buttons": [
        ("pay_wallet_", "👛 پرداخت از کیف پول"), ("pay_online_", "🌐 پرداخت آنلاین"), ("pay_fx_", "💳 پرداخت آنلاین (FrenzyEx)"), ("pay_tr_", "🌪 پرداخت آنلاین (ترونادو)"), ("pay_tp_", "💎 پرداخت آنلاین (تون‌پیز)"),
        ("pay_card_", "💳 پرداخت کارت به کارت"), ("pay_crypto_", "💎 پرداخت ارز دیجیتال"), ("discount_plan_", "🎟 ثبت کد تخفیف"), ("plans", "🔙 بازگشت"),
    ]},
    "plan_pay_wallet": {"category": "finance", "label": "👛 خرید با کیف پول", "default": "👛 پرداخت از کیف پول", "buttons": [
        ("plans", "🔙 بازگشت"),
    ]},
    "plan_pay_online": {"category": "finance", "label": "🌐 خرید آنلاین", "default": "🌐 پرداخت آنلاین", "buttons": [
        ("uq_copy_card", "📋 کپی شماره کارت (یونیک‌پی)"), ("uq_copy_amount", "📋 کپی مبلغ به ریال (یونیک‌پی)"), ("uq_pay", "💳 پرداخت در صفحه UniquePay"),
        ("gw_copy_amount", "📋 کپی مبلغ (تومان)"), ("gw_copy_trx", "📋 کپی مقدار TRX"), ("gw_copy_wallet", "👛 کپی ولت"),
        ("gw_pay_frenzyex", "💳 پرداخت در FrenzyEx"), ("gw_pay_tronado", "🌪 پرداخت در ترونادو"), ("gw_pay_tonpays", "💰 کارت به کارت(Tonpays)"), ("gw_open_bot", "🤖 باز کردن در چت ربات"),
        ("checkpay_", "✅ پرداخت را انجام دادم / بررسی کن"),
        ("plans", "🔙 بازگشت"),
    ]},
    "gateway_tonpays_card": {"category": "finance", "label": "💎 فاکتور تون‌پیز (حالت تلگرام)", "default": "", "buttons": [
        ("tp_copy_card", "📋 کپی شماره کارت"), ("tp_copy_amount", "💰 کپی مبلغ به ریال"), ("tp_check", "❓ بررسی وضعیت پرداخت"),
        ("tp_receipt", "📸 ارسال تصویر رسید"), ("tp_change_card", "🔄 تعویض کارت"),
    ]},
    "plan_pay_card": {"category": "finance", "label": "💳 خرید کارت به کارت", "default": "💳 پرداخت کارت به کارت", "buttons": [
        ("plans", "🔙 بازگشت"),
    ]},
    "discount_code_entry": {"category": "shop", "label": "🎟 ورود کد تخفیف", "default": "🎟 کد تخفیف خود را وارد کنید:", "buttons": [
        ("back", "🔙 بازگشت / انصراف"),
    ]},
    "services": {"category": "services", "label": "📱 سرویس‌های من", "default": "📱 سرویس‌های شما", "buttons": [
        ("my_configs_vip", "سرویس‌های VIP من"), ("service_search", "🔎 جستجوی سرویس"), ("back", "🏠 بازگشت"),
    ]},
    "my_configs_empty": {"category": "services", "label": "📱 سرویس‌های من — خالی", "default": "📱 شما هنوز هیچ سرویسی خریداری نکرده‌اید.", "buttons": [("back", "🏠 بازگشت به منوی اصلی")]},
    "my_configs_has": {"category": "services", "label": "📱 سرویس‌های من", "default": "📱 سرویس‌های شما", "buttons": [
        ("my_configs_vip", "سرویس‌های VIP من"), ("back", "🏠 بازگشت به منوی اصلی"),
    ]},
    "my_configs_list_empty": {"category": "services", "label": "📋 لیست سرویس‌ها — خالی", "default": "📋 سرویسی برای نمایش وجود ندارد.", "buttons": [("back", "🏠 بازگشت به منوی اصلی")]},
    "my_configs_list_has": {"category": "services", "label": "📋 لیست سرویس‌ها", "default": "📋 سرویس‌های شما", "buttons": [
        ("viewconfig_", "سرویس"), ("service_search", "🔎 جستجوی سرویس"), ("back", "🔙 بازگشت"),
    ]},
    "config_detail": {"category": "services", "label": "📱 جزئیات سرویس", "default": "📱 جزئیات سرویس", "buttons": [
        ("renewcfg_", "🔁 تمدید سرویس"), ("viewqr_", "🖼 مشاهده کیوآرکد"),
        ("mirrorconfigs_", "🔗 دریافت کانفیگ‌های تکی"), ("wgfiles_", "📁 دریافت فایل"), ("usersvclink_", "🔄 تغییر لینک ساب"),
        ("open_sublink", "🔗 باز کردن لینک ساب"), ("copy_sublink", "📋 کپی لینک ساب"), ("wallet", "💰 شارژ کیف پول"),
        ("usersvcenable_", "▶️ فعال‌کردن لینک ساب"), ("usersvcdisable_", "⏸ غیرفعال‌کردن لینک ساب"),
        ("delconfig_", "🗑 حذف سرویس"), ("usersvclinkconfirm_", "✅ تغییر لینک ساب"), ("viewconfig_", "🔙 بازگشت (از تأیید تغییر لینک)"), ("back", "🔙 بازگشت"),
    ]},
    "config_delete_confirm": {"category": "services", "label": "🗑 تأیید حذف سرویس", "default": "⚠️ مطمئنی می‌خوای این سرویس رو حذف کنی؟", "buttons": [
        ("delconfirm_", "✅ بله، حذف کن"), ("viewconfig_", "❌ انصراف"),
    ]},
    "renew_payment_method": {"category": "services", "label": "💳 روش پرداخت تمدید", "default": "💳 روش پرداخت تمدید", "buttons": [
        ("cbuild_pay_wallet", "💰 پرداخت از کیف پول"), ("cbuild_pay_online", "🌐 پرداخت آنلاین"),
        ("cbuild_pay_fx", "💳 پرداخت آنلاین (FrenzyEx)"), ("cbuild_pay_tr", "🌪 پرداخت آنلاین (ترونادو)"), ("cbuild_pay_tp", "💎 پرداخت آنلاین (تون‌پیز)"),
        ("cbuild_pay_card", "💳 کارت به کارت"), ("discount_cbuild", "🎟 ثبت کد تخفیف"), ("renew_cancel", "🔙 بازگشت"),
    ]},
    "renew_done": {"category": "services", "label": "✅ نتیجه تمدید", "default": "✅ تمدید سرویس «{service_name}» با موفقیت انجام شد.\n\n📦 حجم اضافه: {added_volume}\n⏳ زمان اضافه: {added_days}\n\n📦 وضعیت فعلی: {current_package}\n🔗 لینک ساب شما تغییر نکرده است.", "buttons": [("back", "🔙 بازگشت")]},
    "renew_menu": {"category": "services", "label": "🔁 تمدید سرویس", "default": "🔁 تمدید سرویس", "buttons": [
        ("renewvol_custom", "➕ مقدار دلخواه (حجم)"), ("renewdays_custom", "➕ زمان دلخواه"),
        ("renewmode_time", "⏳ تمدید زمان"), ("renewmode_volume", "🗜 تمدید حجم"), ("renewmode_both", "🔁 تمدید حجم و زمان"),
        ("renew_cancel", "🔙 بازگشت"),
    ]},
    "wallet": {"category": "finance", "label": "💰 کیف پول", "default": "💰 کیف پول", "buttons": [
        ("charge", "💳 شارژ کیف پول"), ("use_discount", "🎟 ثبت کد تخفیف"), ("transactions", "📋 تراکنش‌های من"), ("back", "🏠 بازگشت"),
    ]},
    "wallet_free": {"category": "finance", "label": "💰 کیف پول آزاد", "default": "💰 کیف پول آزاد", "buttons": [("back", "🔙 بازگشت")]},
    "wallet_locked": {"category": "finance", "label": "🔒 کیف پول مسدود", "default": "🔒 کیف پول مسدود", "buttons": [("back", "🔙 بازگشت")]},
    "wallet_transactions": {"category": "finance", "label": "📋 تراکنش‌ها", "default": "📋 تراکنش‌های کیف پول", "buttons": [("back", "🔙 بازگشت")]},
    "wallet_charge": {"category": "finance", "label": "💳 شارژ کیف پول", "default": "💳 شارژ کیف پول", "buttons": [
        ("charge_", "💳 مبلغ شارژ"), ("charge_custom", "💵 مبلغ دلخواه"), ("wallet", "🔙 بازگشت"),
    ]},
    "walletcharge_method": {"category": "finance", "label": "💳 روش شارژ", "default": "💳 روش شارژ", "buttons": [
        ("chargepay_card_", "💳 کارت به کارت"), ("chargepay_online_", "🌐 پرداخت آنلاین"),
        ("chargepay_fx_", "💳 پرداخت آنلاین (FrenzyEx)"), ("chargepay_tr_", "🌪 پرداخت آنلاین (ترونادو)"), ("chargepay_tp_", "💎 پرداخت آنلاین (تون‌پیز)"),
        ("chargepay_crypto_", "💎 پرداخت ارز دیجیتال"), ("charge", "🔙 بازگشت"),
    ]},
    "walletcharge_pay_card": {"category": "finance", "label": "💳 رسید شارژ", "default": "💳 پرداخت کارت به کارت", "buttons": [
        ("walletcharge_method", "🔄 روش پرداخت دیگر"), ("wallet", "🔙 بازگشت"),
    ]},
    "walletcharge_pay_online": {"category": "finance", "label": "🌐 شارژ آنلاین", "default": "🌐 پرداخت آنلاین", "buttons": [
        ("uq_copy_card", "📋 کپی شماره کارت (یونیک‌پی)"), ("uq_copy_amount", "📋 کپی مبلغ به ریال (یونیک‌پی)"), ("uq_pay", "💳 پرداخت در صفحه UniquePay"),
        ("gw_copy_amount", "📋 کپی مبلغ (تومان)"), ("gw_copy_trx", "📋 کپی مقدار TRX"), ("gw_copy_wallet", "👛 کپی ولت"),
        ("gw_pay_frenzyex", "💳 پرداخت در FrenzyEx"), ("gw_pay_tronado", "🌪 پرداخت در ترونادو"), ("gw_pay_tonpays", "💰 کارت به کارت(Tonpays)"), ("gw_open_bot", "🤖 باز کردن در چت ربات"),
        ("checkpay_", "✅ پرداخت را انجام دادم / بررسی کن"),
        ("wallet", "🔙 بازگشت")]},
    "referral": {"category": "finance", "label": "👥 دعوت دوستان", "vars": ["reward_amount", "invite_link", "invite_code", "invited_count", "successful_invites", "released_amount", "locked_wallet", "condition_text"], "default": (
        "👥 دعوت دوستان و کسب درآمد 💸\n\n"
        "دوستانتو دعوت کن و به‌ازای هر دعوت موفق، {reward_amount} تومان پاداش نقدی بگیر! 🎁\n\n"
        "🔗 لینک اختصاصی شما:\n{invite_link}\n\n"
        "🔑 کد اختصاصی: {invite_code}\n\n"
        "👤 تعداد دعوت: {invited_count}\n"
        "✅ دعوت‌های موفق: {successful_invites}\n"
        "🔓 مبلغ آزاد شده: {released_amount} تومان\n"
        "🔒 مبلغ در انتظار: {locked_wallet} تومان\n\n"
        "{condition_text}"
    ), "buttons": [("back", "🔙 بازگشت")]},
    "profile": {"category": "services", "label": "👤 پروفایل", "vars": ["name", "telegram_id", "wallet", "locked_wallet", "configs_count", "total_purchase", "joined", "invited_count", "successful_invites"], "default": (
        "👤 پروفایل حرفه‌ای شما\n\n"
        "📛 نام: {name}\n"
        "🆔 آیدی: {telegram_id}\n\n"
        "💰 موجودی قابل استفاده: {wallet} تومان\n"
        "🔒 موجودی در انتظار: {locked_wallet} تومان\n\n"
        "📦 تعداد سرویس: {configs_count}\n"
        "🛒 کل خرید: {total_purchase} تومان\n"
        "📅 تاریخ عضویت: {joined}\n\n"
        "👥 تعداد دعوت: {invited_count} | دعوت موفق: {successful_invites}"
    ), "buttons": [
        ("wallet_free", "💰 کیف پول آزاد"), ("wallet_locked", "🔒 کیف پول مسدود"), ("purchase_history", "🛒 تاریخچه خرید"),
        ("transactions", "📋 تاریخچه تراکنش"), ("referral", "🔗 لینک دعوت اختصاصی"), ("back", "🏠 بازگشت به منوی اصلی"),
    ]},
    "purchase_history": {"category": "services", "label": "🧾 تاریخچه خرید", "vars": ["purchase_reports", "total_spent", "purchase_count"], "default": (
        "🛒 تاریخچه خرید شما:\n\n"
        "{purchase_reports}\n\n"
        "💰 مجموع خرید: {total_spent} تومان\n"
        "📊 تعداد خریدها: {purchase_count}"
    ), "buttons": [("back", "🔙 بازگشت")]},
    "support": {"category": "services", "label": "👨‍💻 پشتیبانی", "default": "👨‍💻 پشتیبانی", "buttons": [("ticket", "🎫 ارسال تیکت"), ("my_tickets", "📋 تیکت‌های من"), ("support_channel", "📢 کانال اصلی و پشتیبان"), ("back", "🏠 بازگشت")]},
    "ticket_write": {"category": "services", "label": "🎫 ارسال تیکت", "default": "🎫 متن تیکت خود را ارسال کنید:", "buttons": [("back", "❌ انصراف")]},
    "guides_empty": {"category": "services", "label": "راهنما — خالی", "default": "راهنما و آموزش‌ها", "buttons": [("back", "🔙 بازگشت")]},
    "guides_has": {"category": "services", "label": "راهنما", "default": "راهنما و آموزش‌ها", "buttons": [("guideopen_", "راهنما"), ("user_guides", "فهرست راهنماها"), ("back", "🏠 بازگشت به منوی اصلی")]},
    "join_confirmed": {"category": "start", "label": "✅ عضویت تأیید شد", "default": "منوی اصلی در پایین صفحه فعال شد ✅", "buttons": []},
    "start_join_required": {"category": "start", "label": "🔐 عضویت اجباری", "default": "⚠️ برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید:", "buttons": []},
    "start_welcome": {"category": "start", "label": "👋 خوش‌آمدگویی", "default": "👋 خوش آمدید", "buttons": []},
    "agency_request": {"category": "services", "label": "🤝 درخواست نمایندگی", "default": "🤝 درخواست نمایندگی", "buttons": [("back", "🔙 بازگشت")]},
    "agent_manage": {"category": "services", "label": "🤝 مدیریت نمایندگی", "default": "🤝 مدیریت نمایندگی", "buttons": [("agentstats", "📊 آمار نمایندگی"), ("agentprefix", "🏷 نام دلخواه ساخت کانفیگ"), ("back", "🔙 بازگشت")]},
    "config_delivery": {"category": "services", "label": "📤 تحویل کانفیگ", "default": (
        "✅ سرویس با موفقیت ایجاد شد\n\n"
        "👤 نام کاربری سرویس : {name}\n"
        "🇺🇳 لوکیشن: {location}\n"
        "⏳ مدت زمان: {days}\n"
        "🗜 حجم سرویس: {volume}\n"
        "👤 تعداد کاربر: {users}\n\n"
        "لینک اتصال:\n{sub_link}\n\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید."
    ), "buttons": [("guide", "🧑‍🦯 دریافت روش اتصال"), ("wgfiles_", "📁 دریافت فایل")]},
    "receipt_submitted": {"category": "finance", "label": "🧾 ارسال رسید", "default": "رسید شما ارسال شد.", "buttons": [("support_url", "👨‍💻 ارتباط با پشتیبانی"), ("back", "🏠 بازگشت به صفحه اصلی")]},
    "card_payment_actions": {"category": "finance", "label": "💳 اقدامات پرداخت کارت‌به‌کارت", "default": "", "buttons": [
        ("copy_card", "📋 کپی شماره کارت"), ("copy_amount", "📋 کپی مبلغ به ریال"),
        ("changepay_", "🔄 انتخاب روش پرداخت دیگر"),
    ]},
    "crypto_payment_detail": {"category": "finance", "label": "💱 جزئیات پرداخت ارزی", "default": "", "buttons": [
        ("copy_wallet", "👛 کپی ولت"), ("copy_amount", "📋 کپی مبلغ"), ("other_asset", "🔙 انتخاب ارز دیگر"),
    ]},
    "custom_build_options": {"category": "shop", "label": "🛠 انتخاب گزینه‌ی سرویس سفارشی", "default": "🛠 سرویس خودت رو بساز\n\nیکی از گزینه‌های زیر را انتخاب کن:", "buttons": [("back", "🔙 انصراف")]},
    "payg_options": {"category": "shop", "label": "⚡ انتخاب گزینه‌ی PAYG", "default": "⚡ سرویس PAYG\n\nیکی از گزینه‌های زیر را انتخاب کن:", "buttons": [("back", "🔙 انصراف")]},
    "config_copy_all": {"category": "services", "label": "📋 کپی همه کانفیگ‌ها", "default": "", "buttons": [("copy_all", "📋 کپی همه کانفیگ‌ها")]},
    "notif_service_ended": {"category": "start", "label": "⛔ اعلان پایان سرویس", "default": "", "buttons": [("back", "🛍 خرید اشتراک")]},
    "notif_fup_start": {"category": "start", "label": "⚖️ اعلان شروع مصرف منصفانه", "default": (
        "⚖️ مصرف منصفانه\n\n"
        "سرویس نامحدود شما به اسم {service_name} به حد مصرف آزاد ({free_limit}) رسیده است؛ برای امروز غیرفعال شد و از ساعت ۰۰:۰۰ فردا به‌صورت خودکار دوباره فعال می‌شود.\n\n"
        "از فردا هر روز تا {daily_limit} می‌توانید مصرف کنید؛ با رسیدن به سقف روزانه، سرویس تا نیمه‌شب متوقف و از فردا دوباره فعال می‌شود."
    ), "buttons": [("back", "⚙️ مدیریت سرویس")]},
    "notif_fup_blocked": {"category": "start", "label": "⛔ اعلان توقف روزانه (مصرف منصفانه)", "default": (
        "⛔ سقف مصرف روزانه\n\n"
        "سرویس {service_name} امروز به سقف مصرف منصفانه‌ی خود ({daily_limit}) رسید و تا ساعت ۰۰:۰۰ امشب متوقف است. "
        "فردا به‌صورت خودکار دوباره فعال می‌شود."
    ), "buttons": [("back", "⚙️ مدیریت سرویس")]},
    "notif_payg_empty_suspend": {"category": "start", "label": "⏸ اعلان اتمام موجودی PAYG (توقف سرویس)", "default": (
        "⏸ سرویس PAYG شما به‌دلیل کافی نبودن موجودی کیف پول موقتاً متوقف شد. بعد از شارژ کیف پول دوباره فعال می‌شود."
    ), "buttons": [("wallet", "💵 شارژ کیف پول")]},
    "notif_payg_empty_delete": {"category": "start", "label": "⛔ اعلان اتمام موجودی PAYG (حذف سرویس)", "default": (
        "⛔️ سرویس PAYG شما به‌دلیل کافی نبودن موجودی کیف پول حذف شد."
    ), "buttons": [("wallet", "💵 شارژ کیف پول")]},
    "notif_payg_resumed": {"category": "start", "label": "✅ اعلان فعال‌شدن مجدد PAYG بعد از شارژ", "default": (
        "✅ سرویس PAYG شما بعد از شارژ کیف پول دوباره فعال شد."
    ), "buttons": []},
    "notif_wallet_low": {"category": "start", "label": "🪫 اعلان کم‌شدن موجودی کیف پول", "default": (
        "🪫 موجودی کیف پول شما کم شده است.\n\n💰 موجودی فعلی: {balance} تومان\n\nبرای جلوگیری از وقفه در خرید و تمدید، کیف پول خود را شارژ کنید."
    ), "buttons": [("wallet", "💵 شارژ کیف پول")]},
    "notif_referral_join": {"category": "start", "label": "🎉 رفرال: ورود دعوت‌شده (پیام به دعوت‌کننده)", "default": (
        "🎉 یک عضو جدید از طریق لینک دعوت شما وارد ربات شد و عضویتش تأیید شد!\n\n"
        "👤 نام: {name}\n🆔 آیدی: {user_id}\n\n💰 {reward_text}"
    ), "buttons": []},
    "notif_referral_reward": {"category": "start", "label": "🎁 رفرال: آزاد شدن پاداش (پیام به دعوت‌کننده)", "default": (
        "🎁 پاداش دعوت آزاد شد!\n\n"
        "کاربری که دعوت کرده بودید ({name}) خرید واجد شرطش را انجام داد و {reward} تومان به کیف پول شما اضافه شد."
    ), "buttons": []},
    "notif_referral_left": {"category": "start", "label": "🚨 رفرال: خروج دعوت‌شده از کانال (قفل/جریمه)", "default": (
        "🚨 کاربری که با کد دعوت شما آمده بود از یکی از کانال‌های اجباری ما خارج شد:\n\n{details}"
    ), "buttons": []},
    "notif_referral_rejoined": {"category": "start", "label": "✅ رفرال: بازگشت دعوت‌شده به کانال‌ها", "default": (
        "✅ کاربری که با کد دعوت شما آمده بود دوباره عضو همه‌ی کانال‌های ما شد:\n\n{details}"
    ), "buttons": []},
    "notif_referral_line_lock": {"category": "start", "label": "   ↳ رفرال: خط «پاداش مسدود شد»", "default": (
        "⏳ {amount} تومان پاداش دعوتش موقتاً از کیف پول قابل‌برداشت شما به کیف پول مسدود برگشت؛ "
        "به‌محض این‌که دوباره عضو کانال‌ها شود خودکار آزاد می‌شود."
    ), "buttons": []},
    "notif_referral_line_lockpen": {"category": "start", "label": "   ↳ رفرال: خط «جریمه از پاداش مسدود»", "default": (
        "💸 {penalty} تومان جریمه از همان پاداش مسدودشده کم شد؛ با بازگشت او به کانال‌ها {remaining} تومان برایتان آزاد می‌شود."
    ), "buttons": []},
    "notif_referral_line_wallet": {"category": "start", "label": "   ↳ رفرال: خط «جریمه از کیف پول رفرالی»", "default": (
        "💸 {amount} تومان از کیف پول رفرالی شما کسر شد."
    ), "buttons": []},
    "notif_referral_line_service": {"category": "start", "label": "   ↳ رفرال: خط «جریمه از حجم سرویس»", "default": (
        "📉 {gb} گیگابایت از حجم سرویس رفرالی شما ({service}) کم شد."
    ), "buttons": []},
    "notif_referral_line_service_after_wallet": {"category": "start", "label": "   ↳ رفرال: خط «جریمه از حجم (موجودی رفرالی کافی نبود)»", "default": (
        "⚠️ موجودی کیف پول رفرالی شما برای جریمه کافی نبود؛ به‌جای آن\n📉 {gb} گیگابایت از حجم سرویس رفرالی شما ({service}) کم شد."
    ), "buttons": []},
    "notif_referral_line_release": {"category": "start", "label": "   ↳ رفرال: خط «پاداش دوباره آزاد شد»", "default": (
        "🔓 {amount} تومان پاداش دعوتش دوباره در کیف پول قابل‌برداشت شما آزاد شد.{penalty_note}"
    ), "buttons": []},
    "notif_usage_alert": {"category": "start", "label": "⚠️ اعلان هشدار مصرف/انقضا", "default": "", "buttons": [("back", "📦 مشاهده سرویس")]},
    "order_log": {"category": "start", "label": "🧾 دکمه‌ی پیام لاگ سفارش (کانال)", "default": "", "buttons": [("order_log_buy", "🛒 خرید اشتراک")]},
    "ticket_reply": {"category": "services", "label": "↩️ پاسخ به تیکت (پیام پشتیبانی)", "default": "", "buttons": [("replyticket_", "↩️ پاسخ")]},
    "insufficient_balance": {"category": "finance", "label": "💰 موجودی ناکافی", "default": "❌ موجودی کیف پول کافی نیست.", "buttons": [
        ("wallet", "💵 شارژ کیف پول"), ("back", "🔙 بازگشت"),
    ]},
}

BUTTON_BUNDLES = {
    "payments": {
        "label": "💳 دکمه‌های پرداخت",
        "screen_keys": {"plan_payment_method", "cbuild_payment_method", "walletcharge_method", "plan_pay_wallet", "plan_pay_online", "gateway_tonpays_card", "plan_pay_card", "cbuild_pay_wallet", "cbuild_pay_online", "cbuild_pay_card", "wallet_charge", "walletcharge_pay_card", "walletcharge_pay_online"},
        "callback_keys": {"cbuild_pay_wallet", "cbuild_pay_online", "cbuild_pay_card", "cbuild_change_payment", "walletcharge_method", "charge_custom"},
        "callback_prefixes": ("pay_wallet_", "pay_online_", "pay_fx_", "pay_tr_", "pay_card_", "pay_crypto_", "discount_plan_", "changepay_", "charge_", "chargepay_card_", "chargepay_online_"),
    },
    "wallet": {
        "label": "💰 دکمه‌های کیف پول",
        "screen_keys": {"wallet", "wallet_free", "wallet_locked", "wallet_transactions", "wallet_charge", "walletcharge_method", "walletcharge_pay_card", "walletcharge_pay_online"},
        "callback_keys": {"wallet", "charge", "transactions", "use_discount"},
        "callback_prefixes": ("charge_", "chargepay_", "wallet", "transactions"),
    },
    "purchase": {
        "label": "🛒 دکمه‌های خرید و پلن‌ها",
        "screen_keys": {"buy_plans", "plans", "free_test", "vip_category_list", "vip_plans", "plan_select", "custom_build", "payg_name", "plan_payment_method", "cbuild_payment_method", "discount_code_entry"},
        "callback_keys": {"plans", "plans_vip", "cbuild_start", "discount_cbuild"},
        "callback_prefixes": ("buy_", "vipcat_", "discount_plan_", "discount_cbuild", "payg_buy"),
    },
    "services": {
        "label": "📱 دکمه‌های سرویس‌ها",
        "screen_keys": {"services", "my_configs_empty", "my_configs_has", "my_configs_list_empty", "my_configs_list_has", "service_list_vip", "config_detail", "config_delete_confirm", "renew_done", "renew_menu"},
        "callback_keys": {"my_configs", "my_configs_vip", "service_search", "renew_cancel"},
        "callback_prefixes": ("viewconfig_", "renewcfg_", "viewqr_", "mirrorconfigs_", "wgfiles_", "usersvclink_", "usersvcenable_", "usersvcdisable_", "delconfig_", "delconfirm_", "renewdays_", "renewvol_"),
    },
    "shared": {
        "label": "🔁 دکمه‌های عمومی و ناوبری",
        "screen_keys": {"start", "main_reply", "support", "guides_has", "guides_empty", "profile", "receipt_submitted", "card_payment_actions", "insufficient_balance"},
        "callback_keys": {"back", "plans", "wallet", "support", "profile", "referral", "user_guides", "ticket", "my_tickets", "agency", "agent_manage", "renew_cancel", "ticket_cancel"},
        "callback_prefixes": ("guideopen_", "ticketreply_", "changepay_"),
    },
}


CATEGORIES = {
    # ——— کاربران ———
    "start":          "🚀 شروع",
    "main_menu":      "🏠 منوی اصلی",
    "shop":           "🛍 خرید",
    "renewal":        "🔁 تمدید",
    "services":       "📱 سرویس‌ها",
    "test":           "🎁 تست",
    "custom_service": "🛠 سرویس سفارشی",
    "wallet":         "💰 کیف پول",
    "payment":        "💳 پرداخت",
    "finance":        "🯧 فاکتور",
    "discount":       "🎟 تخفیف",
    "referral":       "👥 رفرال",
    "agency":         "🤝 نمایندگی",
    "active_services":"✅ سرویس‌های فعال",
    "expired_services":"❌ سرویس‌های منقضی",
    "errors":         "⚠️ پیام‌های خطا",
    "success":        "✅ پیام‌های موفقیت",
    "guides":         "📚 راهنما",
    "support":        "📞 پشتیبانی",
    # ——— مدیریت ———
    "admin":               "🛡 پنل مدیریت",
    "admin_users":         "👥 کاربران (ادمین)",
    "admin_stats":         "📊 آمار",
    "admin_tickets":       "🎫 تیکت‌ها",
    "admin_broadcast":     "📢 پیام همگانی",
    "admin_orders":        "📥 سفارش‌ها",
    "admin_payments":      "💳 پرداخت‌ها (ادمین)",
    "admin_receipts":      "🧲 رسیدها",
    "admin_services":      "📱 سرویس‌ها (ادمین)",
    "admin_panels":        "🖥 پنل‌ها",
    "admin_vip":           "⭐ VIP",
    "admin_test":          "🎁 تست (ادمین)",
    "admin_renewal":       "🔁 تمدید (ادمین)",
    "admin_agents":        "🤝 نمایندگان",
    "admin_discounts":     "🎟 تخفیف‌ها (ادمین)",
    "admin_referral":      "👥 رفرال (ادمین)",
    "admin_wallet":        "💰 کیف پول (ادمین)",
    "admin_payg":          "⚡ Pay As You Go",
    "admin_miniapp":       "🖥 Mini App",
    "admin_settings":      "⚙️ تنظیمات",
    # ——— مابقی ———
    "all_messages": "🧩 تمام پیام‌ها و اعلان‌ها",
    "all_buttons":  "🔘 تمام دکمه‌های قابل ویرایش",
}

def _ensure() -> None:
    """Ensure UI-editor tables exist, but never hit Turso repeatedly.

    The old implementation opened a transaction and ran all CREATE/ALTER
    statements on every call. Since get_text()/get_button()/get_layout() are
    called while rendering almost every message, that turned the global UI
    editor hook into a database round-trip for every response.
    """
    global _SCHEMA_READY

    if _SCHEMA_READY:
        _migrate_unshare()  # (ارزان: فقط یک پرچم؛ اگر دفعه‌ی اول جدول settings آماده نبود دوباره تلاش می‌کند)
        return

    # Only the first caller performs the migration. Other callers wait once,
    # then return without touching the database.
    with _SCHEMA_INIT_LOCK:
        if _SCHEMA_READY:
            return

        with db.transaction() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS ui_text_overrides (
                key TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                entities_json TEXT,
                updated_at TEXT NOT NULL
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS ui_button_overrides (
                screen_key TEXT NOT NULL,
                callback_key TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY(screen_key,callback_key)
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS ui_button_meta (
                screen_key TEXT NOT NULL,
                callback_key TEXT NOT NULL,
                custom_emoji_id TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                style TEXT,
                PRIMARY KEY(screen_key,callback_key)
            )""")
            try:
                cur.execute("ALTER TABLE ui_button_meta ADD COLUMN style TEXT")
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

            cur.execute("""CREATE TABLE IF NOT EXISTS ui_layouts (
                screen_key TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'vertical',
                columns INTEGER NOT NULL DEFAULT 1,
                order_json TEXT,
                row_widths_json TEXT
            )""")
            try:
                cur.execute("ALTER TABLE ui_layouts ADD COLUMN row_widths_json TEXT")
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
            cur.execute("""CREATE TABLE IF NOT EXISTS ui_custom_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                screen_key TEXT NOT NULL,
                text TEXT NOT NULL,
                style TEXT NOT NULL DEFAULT 'primary',
                action_type TEXT NOT NULL,
                action_value TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")
            try:
                cur.execute("ALTER TABLE ui_custom_buttons ADD COLUMN custom_emoji_id TEXT")
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        _SCHEMA_READY = True
    _migrate_unshare()


def _pattern_matches(pattern: str, callback_data: str) -> bool:
    return callback_data == pattern or (pattern.endswith("_") and callback_data.startswith(pattern))



_AUTO_REGISTRY_READY = False
_AUTO_REGISTRY: dict[str, dict[str, Any]] = {}
_AUTO_REGEX_CACHE: dict[str, re.Pattern] = {}
_AUTO_MATCH_ENTRIES: list[dict] | None = None

def _ast_message_template(node) -> tuple[str, list[str]] | None:
    """Convert a static string/f-string argument into an editable template.
    Dynamic expressions become stable {v1}, {v2} placeholders so the user can
    rewrite the sentence without losing runtime values."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, []
    if not isinstance(node, ast.JoinedStr):
        return None
    parts, names = [], []
    idx = 0
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            idx += 1
            name = f"v{idx}"
            names.append(name)
            parts.append("{" + name + "}")
        else:
            return None
    return "".join(parts), names

_AUTO_ALIASES: list[dict[str, Any]] = []


def _static_core(template: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}", "", template or "")).strip()


def _find_canonical(core: str, nph: int, pool: list) -> tuple | None:
    """بین متن‌های قبلی، متنی را پیدا می‌کند که «همان پیام» است (هسته‌ی ثابتِ تقریباً یکسان و همان تعداد متغیر)."""
    import difflib
    if len(core) < 12:
        return None
    for ck, ctpl, ccore, cn in pool:
        if cn != nph:
            continue
        if ccore == core or difflib.SequenceMatcher(None, ccore, core).ratio() >= 0.93:
            return ck, ctpl
    return None


def _discover_auto_messages() -> None:
    """کشف متن‌های واقعیِ سمت کاربر از روی کد (ui_discovery v2): کلید پایدار، دسته‌بندی درست، متغیرهای نام‌دار.

    متن‌های تکراری (همان پیام در چند هندلر) فقط یک‌بار در لیست ویرایش می‌آیند؛ نسخه‌های دیگر «هم‌نام»
    ثبت می‌شوند و با ویرایش همان یک متن، همه‌ی جاها عوض می‌شوند."""
    global _AUTO_REGISTRY_READY
    if _AUTO_REGISTRY_READY:
        return
    _AUTO_REGISTRY_READY = True
    try:
        from pathlib import Path
        import ui_discovery
        import text_catalog as _tc
        root = Path(__file__).resolve().parent
        reg = ui_discovery.discover(root)
        dead = _dead_catalog_keys()
        pool: list = []
        for items in _tc.TEXT_CATEGORIES.values():
            for k, d in items:
                if k in dead or not _is_text_catalog_content_key(k):
                    continue
                pool.append((k, d, _static_core(d), len(re.findall(r"\{[A-Za-z_]", d))))
        live = {d.strip() for _k, d, _c, _n in pool}
        for key, item in reg.items():
            tpl = item["template"].strip()
            if tpl in live:
                continue  # همین متن قبلاً در کاتالوگ دستی هست؛ تکراری نشان داده نشود
            nph = len(re.findall(r"\{[A-Za-z_]", tpl))
            core = _static_core(tpl)
            hit = _find_canonical(core, nph, pool)
            if hit:
                _AUTO_ALIASES.append({"key": key, "template": tpl, "canonical": hit[0],
                                      "canonical_template": hit[1], "source": item.get("source", ""),
                                      "partial": bool(item.get("partial"))})
                continue
            _AUTO_REGISTRY[key] = item
            pool.append((key, tpl, core, nph))
    except Exception:
        logger.exception("auto message discovery failed")


_DEAD_KEYS: set | None = None


def _dead_catalog_keys() -> set:
    """کلیدهای کاتالوگ که در کد هیچ‌جا استفاده نمی‌شوند (ویرایششان اثری ندارد) — از لیست ویرایش حذف می‌شوند."""
    global _DEAD_KEYS
    if _DEAD_KEYS is not None:
        return _DEAD_KEYS
    dead: set = set()
    try:
        from pathlib import Path
        import text_catalog as _tc
        root = Path(__file__).resolve().parent
        chunks_src = []
        for f in root.rglob("*.py"):
            if f.name == "text_catalog.py" or "tests" in f.parts or "__pycache__" in f.parts:
                continue
            try:
                chunks_src.append(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        allsrc = "\n".join(chunks_src)
        for items in _tc.TEXT_CATEGORIES.values():
            for k, d in items:
                if f'"{k}"' in allsrc or f"'{k}'" in allsrc:
                    continue
                parts = [c.strip() for c in re.split(r"\{[^}]*\}", d) if len(c.strip()) >= 8]
                chunk = max(parts, key=len) if parts else d
                if chunk[:25] in allsrc:
                    continue
                dead.add(k)
    except Exception:
        logger.exception("dead catalog scan failed")
    dead |= {"service_delivery_text", "service_delivery_test_text"}  # فقط در لیبل ادمین آمده‌اند؛ هرگز رندر نمی‌شوند
    _DEAD_KEYS = dead
    return dead


def _live_catalog_templates() -> set:
    try:
        import text_catalog as _tc
        dead = _dead_catalog_keys()
        return {d.strip() for items in _tc.TEXT_CATEGORIES.values() for k, d in items if k not in dead}
    except Exception:
        return set()

def _auto_pattern(template: str) -> re.Pattern:
    cached = _AUTO_REGEX_CACHE.get(template)
    if cached:
        return cached
    import re as _re
    parts = []
    pos = 0
    for m in _re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template):
        parts.append(_re.escape(template[pos:m.start()]))
        parts.append(r"(.*?)")
        pos = m.end()
    parts.append(_re.escape(template[pos:]))
    try:
        pattern = _re.compile("^" + "".join(parts) + "$", _re.S)
    except Exception:
        pattern = _re.compile("^" + _re.escape(template) + "$", _re.S)
    _AUTO_REGEX_CACHE[template] = pattern
    return pattern

def auto_entries_for_screen(key: str) -> list[dict]:
    """Return discoverable messages relevant to a screen plus catalog messages.
    It is intentionally built once and never scans source during each render."""
    _discover_auto_messages()
    screen = SCREENS.get(key) or {}
    cat = screen.get("category")
    # Map UI categories to catalog sections using keywords; unknown screens still
    # receive discovered messages from their source.
    keyword_map = {
        "start": ("شروع", "اعلان"),
        "shop": ("خرید", "پرداخت", "سرویس سفارشی"),
        "finance": ("کیف پول", "فاکتور", "پرداخت", "اعلان"),
        "services": ("سرویس", "تحویل", "تمدید"),
        "discount": ("تخفیف",),
        "referral": ("دعوت",),
        "guides": ("راهنما",),
        "support": ("پشتیبانی",),
        "agency": ("پشتیبانی و نمایندگی",),
        "renewal": ("تمدید",),
        "custom_service": ("سرویس سفارشی",),
    }
    wanted = keyword_map.get(cat, ())
    items = []
    try:
        import text_catalog as _tc
        for section, entries in _tc.TEXT_CATEGORIES.items():
            if wanted and not any(w in section for w in wanted):
                continue
            for k, template in entries:
                if _is_text_catalog_content_key(k):
                    items.append({"key": k, "template": template, "source": section})
    except Exception:
        pass
    # Add source-discovered messages; avoid duplicates with catalog exact templates.
    known = {(x["key"], x["template"]) for x in items}
    for item in _AUTO_REGISTRY.values():
        if (item["key"], item["template"]) not in known:
            items.append(item)
    # Stable order and de-dup by key.
    out, seen = [], set()
    for item in items:
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        out.append(item)
    return out

VAR_DOCS = {
    "name": "نام سرویس/کاربر", "service_name": "نام سرویس", "location": "لوکیشن", "days": "مدت",
    "volume": "حجم", "users": "تعداد کاربر", "sub_link": "لینک اشتراک", "plan_name": "نام پلن",
    "expiry_date": "تاریخ انقضا (میلادی)", "order_time": "زمان ثبت سفارش (شمسی، با ثانیه)",
    "delivery_time": "زمان تحویل سرویس (شمسی، با ثانیه)", "elapsed": "مدت بین ثبت سفارش تا تحویل",
    "status": "وضعیت سرویس", "product": "نام محصول", "traffic": "حجم کل", "used": "حجم مصرفی",
    "remaining": "حجم باقی‌مانده", "usage_bar": "نوار مصرف", "expiry": "تاریخ اتمام",
    "last_connection": "آخرین اتصال", "last_app": "آخرین اپ متصل‌شده", "last_device": "آخرین دستگاه متصل‌شده",
    "online_status": "آنلاین/آفلاین", "last_sub_fetch": "آخرین دریافت لینک اشتراک",
    "devices_count": "تعداد دستگاه‌های ثبت‌شده", "devices_limit": "سقف دستگاه", "days_left": "روزهای باقی‌مانده",
    "tests": "فهرست تست‌ها (نام، مدت، حجم، لینک)", "count": "تعداد تست‌های تحویل‌شده",
    "first_name": "نام کاربر", "price": "قیمت", "wallet": "موجودی کیف پول",
    "plan": "نام پلن", "free_limit": "حد مصرف آزاد", "daily_limit": "سقف مصرف روزانه",
    "telegram_id": "آیدی عددی کاربر", "username": "یوزرنیم تلگرام کاربر", "panel_name": "اسم پنل (همان نامی که برای پنل گذاشته‌اید)", "order_label": "نوع سفارش", "customer_name": "نام مشتری",
    "package_name": "بسته (حجم | زمان)", "amount": "مبلغ", "payment_method": "نحوه پرداخت",
    "renewal_details": "جزئیات تمدید (فقط در لاگ تمدید)", "time": "زمان ثبت لاگ",
    "added_volume": "حجم اضافه‌شده در پی تمدید", "added_days": "زمان اضافه‌شده در پی تمدید",
    "old_volume": "حجم اولیه بسته (قبل از تمدید)", "new_volume": "حجم بعد از تمدید",
    "old_days": "زمان اولیه (قبل از تمدید)", "new_days": "زمان بعد از تمدید",
    "current_package": "وضعیت فعلی (حجم | زمان)", "details": "جزئیات آماده (در تمدید: اضافه‌شده + وضعیت فعلی)",
}
_ORDER_LOG_VARS = ["order_label", "customer_name", "telegram_id", "service_name", "package_name", "amount", "expiry",
                   "time", "payment_method", "renewal_details", "username", "panel_name"]
KEY_EXTRA_VARS = {
    "order_log_purchase": _ORDER_LOG_VARS, "order_log_renewal": _ORDER_LOG_VARS, "order_log_test": _ORDER_LOG_VARS,
    "order_log_custom": _ORDER_LOG_VARS, "order_log_payg": _ORDER_LOG_VARS,
    "renew_done": ["service_name", "old_volume", "new_volume", "added_volume", "old_days", "new_days", "added_days", "current_package", "details"],
    "notif_fup_start": ["service_name", "plan", "free_limit", "daily_limit"],
    "notif_fup_blocked": ["service_name", "plan", "free_limit", "daily_limit"],
    "notif_referral_join": ["name", "user_id", "reward_text"],
    "notif_referral_reward": ["name", "user_id", "reward"],
    "notif_referral_left": ["name", "user_id", "details"],
    "notif_referral_rejoined": ["name", "user_id", "details"],
    "notif_referral_line_lock": ["amount"],
    "notif_referral_line_lockpen": ["penalty", "remaining"],
    "notif_referral_line_wallet": ["amount"],
    "notif_referral_line_service": ["gb", "service"],
    "notif_referral_line_service_after_wallet": ["gb", "service"],
    "notif_referral_line_release": ["amount", "penalty", "penalty_note"],
    "config_delivery": ["name", "location", "days", "volume", "users", "sub_link", "plan_name", "expiry_date",
                        "order_time", "delivery_time", "elapsed"],
    "config_delivery_test": ["name", "location", "days", "volume", "users", "sub_link", "plan_name", "expiry_date",
                             "order_time", "delivery_time", "elapsed"],
    "config_detail": ["status", "service_name", "location", "product", "traffic", "used", "remaining", "usage_bar",
                      "expiry", "last_connection", "last_app", "last_device", "online_status", "last_sub_fetch",
                      "devices_count", "devices_limit", "days_left"],
    "free_test_multi": ["tests", "count", "order_time", "delivery_time"],
    "free_test_intro": ["plan_name"],
    "gateway_invoice": ["progress", "title", "details", "done_text", "deadline_note"],
    "gateway_tonpays_card": ["amount", "card", "holder", "tracking"],
}


def variables_help(key: str, template: str = "") -> str:
    """راهنمای متغیرهای قابل‌استفاده در یک متن؛ ادمین هر کدام را می‌تواند بگذارد یا حذف کند."""
    names = list(KEY_EXTRA_VARS.get(key, []))
    for n in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template or ""):
        if n not in names:
            names.append(n)
    if not names:
        return ""
    lines = [f"{{{n}}} — {VAR_DOCS.get(n, 'مقدار پویا')}" for n in names]
    return "🔣 متغیرها (اختیاری؛ هر کدام را بخواهید بگذارید یا حذف کنید):\n" + "\n".join(lines)


def rich_payload_for(key: str, values: dict | None = None):
    """اگر برای این متن ریچ‌مسیج (جدول/تیتر…) ذخیره شده باشد، payload با متغیرهای جایگزین‌شده برمی‌گرداند."""
    try:
        import rich_message as _rm
        keys = _rm.text_keys()
        if not keys or not key:
            return None
        cands = [key]
        if key in _CATALOG_ALIASES:
            cands.append(_CATALOG_ALIASES[key])
        cands += [k for k, a in _CATALOG_ALIASES.items() if a == key]
        for k in cands:
            if k in keys:
                payload = _rm.load_text(k)
                if payload:
                    if key == "gateway_tonpays_card":
                        import json as _json
                        payload = _json.loads(_json.dumps(payload, ensure_ascii=False).replace("{amount} تومان", "{amount} ریال"))
                    return _rm.substitute(payload, values)
    except Exception:
        logger.exception("rich payload lookup failed (%s)", key)
    return None


def override_text_entries() -> list[dict]:
    """کلیدهایی که ادمین در «ویرایش متن‌ها» ذخیره کرده، با خودِ «متن ویرایش‌شده» به‌عنوان قالب.

    🐛 فیکس: پیام‌هایی که با user_text()/get_text() ساخته می‌شوند از قبل متن ویرایش‌شده‌ی ادمین
    را (بدون entity) می‌فرستند؛ لایه‌ی مرکزی فقط با «متن پیش‌فرض» تطبیق می‌داد، پس متن ویرایش‌شده
    هرگز match نمی‌شد و ایموجی پریمیوم/ریچ‌تکست از پیام (اعلان‌ها، تأیید پرداخت، لاگ‌ها و …) می‌پرید.
    این لیست (کش ۲۰ ثانیه‌ای، با پاک‌سازی در set_text) آن خلأ را پر می‌کند.
    """
    ck = "uioverride_entries"
    val, hit = cache.get(ck)
    if hit and val is not None:
        return val
    out: list[dict] = []
    try:
        _ensure()
        cur = db.get_connection().cursor()
        cur.execute("SELECT key, text, entities_json FROM ui_text_overrides")
        for k, t, ej in cur.fetchall():
            if not t or not str(t).strip():
                continue
            try:
                has_ents = bool(json.loads(ej)) if ej else False
            except Exception:
                has_ents = False
            out.append({"key": str(k), "template": str(t), "source": "override", "has_entities": has_ents})
    except Exception:
        logger.debug("override entries load failed", exc_info=True)
    out.sort(key=lambda x: len(x["template"]), reverse=True)
    cache.set(ck, out, ttl=20)
    return out


def rich_text_match(text: str):
    """برای پیام‌هایی که مستقیم send_message می‌شوند: متن را به متن کاتالوگ/کشف‌شده تطبیق می‌دهد و
    در صورت داشتن ریچ‌مسیج، payload آماده‌ی ارسال را برمی‌گرداند."""
    try:
        import rich_message as _rm
        if not text or not _rm.text_keys():
            return None
        # متنِ ویرایش‌شده‌ی ادمین (override) هم باید match شود، نه فقط متن پیش‌فرض کاتالوگ.
        for item in override_text_entries():
            if item["key"] not in _rm.text_keys():
                continue
            m = _auto_pattern(item["template"]).match(text)
            if not m:
                continue
            names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", item["template"])
            values = {n: g for n, g in zip(names, m.groups())}
            payload = rich_payload_for(item["key"], values)
            if payload:
                return payload
        for item in auto_text_entries():
            if item["key"] not in _rm.text_keys() and not any(
                    _CATALOG_ALIASES.get(item["key"]) == k for k in _rm.text_keys()):
                continue
            m = _auto_pattern(item.get("template", "")).match(text)
            if not m:
                continue
            names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", item["template"])
            values = {n: g for n, g in zip(names, m.groups())}
            return rich_payload_for(item["key"], values)
    except Exception:
        logger.exception("rich text match failed")
    return None


def auto_text_entries() -> list[dict]:
    global _AUTO_MATCH_ENTRIES
    if _AUTO_MATCH_ENTRIES is not None:
        return _AUTO_MATCH_ENTRIES
    _discover_auto_messages()
    out = []
    try:
        import text_catalog as _tc
        for section, entries in _tc.TEXT_CATEGORIES.items():
            for k, template in entries:
                if _is_text_catalog_content_key(k) and k not in _dead_catalog_keys():
                    out.append({"key": k, "template": template, "source": section})
    except Exception:
        pass
    out.extend(_AUTO_REGISTRY.values())
    out.extend(_AUTO_ALIASES)
    seen = set()
    result = []
    for item in out:
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        result.append(item)
    result.sort(key=lambda x: len(x.get("template", "")), reverse=True)
    _AUTO_MATCH_ENTRIES = result
    return result

_PARTIAL_CACHE: dict[str, re.Pattern] = {}


def _partial_pattern(template: str) -> re.Pattern:
    """الگوی جست‌وجوی یک «تکه‌ی متن» داخل پیام کامل (بدون لنگر ^$؛ متغیرِ ابتدا/انتهای تکه تا مرز خط می‌رود)."""
    cached = _PARTIAL_CACHE.get(template)
    if cached:
        return cached
    parts: list[str] = []
    pos = 0
    toks = list(re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template))
    for i, m in enumerate(toks):
        parts.append(re.escape(template[pos:m.start()]))
        at_end = m.end() == len(template)
        parts.append(r"([^\n]*)" if at_end else r"(.*?)")
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    try:
        pat = re.compile("".join(parts), re.S)
    except Exception:
        pat = re.compile(re.escape(template))
    _PARTIAL_CACHE[template] = pat
    return pat


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _apply_partial_overrides(text: str):
    """جایگزینیِ تکه‌های ویرایش‌شده در پیام‌های ترکیبی. entityهای ساخته‌شده با offset درست برمی‌گردند."""
    ents: list[dict] = []
    changed = False
    try:
        for item in auto_text_entries():
            if not item.get("partial"):
                continue
            key = item.get("canonical") or item["key"]
            template = item.get("canonical_template") if item.get("canonical") else item.get("template", "")
            own_t = item.get("template", "")
            if item.get("canonical") and (get_text(item["key"], own_t) != own_t or get_entities(item["key"])):
                key, template = item["key"], own_t
            override = get_text(key, template)
            if override == template and not get_entities(key):
                continue
            m = _partial_pattern(own_t).search(text)
            if not m:
                continue
            names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", own_t)
            tnames = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template)
            groups = m.groups()
            values = {}
            for idx, name in enumerate(tnames):
                if idx < len(groups):
                    values[name] = groups[idx]
            rendered, new_ents = render_template(key, values, fallback=override)
            start16 = _u16(text[:m.start()])
            old16 = _u16(text[m.start():m.end()])
            delta = _u16(rendered) - old16
            for e in ents:
                if e["offset"] >= start16 + old16:
                    e["offset"] += delta
            for e in (new_ents or []):
                e = dict(e)
                e["offset"] = int(e["offset"]) + start16
                ents.append(e)
            text = text[:m.start()] + rendered + text[m.end():]
            changed = True
    except Exception:
        logger.exception("partial text override failed")
    return (text, ents) if changed else (text, [])


def apply_auto_text_with_entities(text: str):
    """Apply the first matching catalog/source override without DB work when no
    override exists. Dynamic source messages retain runtime values through vN."""
    if not text:
        return text, []
    _discover_auto_messages()
    # Catalog first: exact template regex with named placeholders.
    candidates = auto_text_entries()
    for item in candidates:
        template = item.get("template", "")
        if not template:
            continue
        pattern = _auto_pattern(template)
        m = pattern.match(text)
        if not m:
            continue
        key = item["key"]
        names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template)
        groups = m.groups()
        canonical = item.get("canonical")
        if canonical:
            # نسخه‌ی هم‌نامِ یک متن: اگر برای خودش ویرایشی نیست، ویرایشِ متن اصلی را اعمال کن (متغیرها به‌ترتیب)
            if get_text(key, template) == template and not get_entities(key):
                key = canonical
                template = item.get("canonical_template", template)
                names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template)
        override = get_text(key, template)
        if override == template and not get_entities(key):
            continue
        values = {}
        for idx, name in enumerate(names):
            if idx < len(groups):
                values[name] = groups[idx]
        rendered, entities = render_template(key, values, fallback=override)
        return rendered, entities
    # متن‌هایی که تکه‌تکه ساخته می‌شوند (text += "...") — هر تکه‌ی ویرایش‌شده را داخل پیام کامل جایگزین کن.
    _p_text, _p_ents = _apply_partial_overrides(text)
    if _p_text != text:
        return _p_text, _p_ents
    # متن ویرایش‌شده‌ی ادمین (override) که قبلاً با user_text()/get_text() جایگزین شده و دیگر شبیه
    # قالب پیش‌فرض نیست: با خودِ متن ویرایش‌شده تطبیق بده تا entityها (ایموجی پریمیوم/bold/…) برگردند.
    for item in override_text_entries():
        if not item.get("has_entities"):
            continue
        template = item["template"]
        m = _auto_pattern(template).match(text)
        if not m:
            continue
        key = item["key"]
        names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template)
        groups = m.groups()
        values = {name: groups[idx] for idx, name in enumerate(names) if idx < len(groups)}
        rendered, entities = render_template(key, values, fallback=template)
        if entities:
            return rendered, entities
    return text, []



# 🐛 فیکس: یک callback مثل «plans» هم برای دکمه‌ی منوی اصلی «🛒 خرید اشتراک» و هم برای ده‌ها
# دکمه‌ی «🔙 بازگشت/انصراف» (که به همان صفحه برمی‌گردند) استفاده می‌شود. قبلاً همه‌ی آن‌ها یک
# رکورد مشترک داشتند، پس تغییر نام «خرید اشتراک» روی همه‌ی دکمه‌های بازگشت هم اعمال می‌شد.
# حالا دکمه‌ها بر اساس «نقش» هم گروه‌بندی می‌شوند: گروه back (بازگشت/انصراف/لغو) و گروه main
# (بقیه). فقط دکمه‌های هم‌گروه با هم مشترک‌اند.
_BACK_WORDS = ("بازگشت", "انصراف", "لغو", "🔙", "🏠", "⬅️")


def _label_group(label: str | None) -> str:
    text = str(label or "")
    return "back" if any(w in text for w in _BACK_WORDS) else "main"


def _button_group(screen_key: str | None, callback_key: str) -> str:
    """گروه (back/main) دکمه‌ی مشخص‌شده بر اساس برچسب پیش‌فرضش در رجیستری."""
    if screen_key and screen_key in SCREENS:
        for pattern, label in SCREENS[screen_key].get("buttons", []):
            if pattern == callback_key:
                return _label_group(label)
    return "main"


def _callback_occurrences(callback_key: str, group: str | None = None) -> list[tuple[str, str]]:
    """Return all screen/pattern locations for a logical callback (optionally only one group)."""
    result = []
    for screen_key, screen in SCREENS.items():
        for pattern, label in screen.get("buttons", []):
            if pattern == callback_key and (group is None or _label_group(label) == group):
                result.append((screen_key, pattern))
    return result


def _callback_group_names(callback_key: str) -> set[str]:
    return {_label_group(label) for screen in SCREENS.values()
            for pattern, label in screen.get("buttons", []) if pattern == callback_key}


def _group_storage_cb(callback_key: str, group: str) -> str:
    # اگر یک callback هر دو نقش را دارد، گروه back کلید جدا (cb#back) می‌گیرد و گروه main همان
    # کلید قبلی را نگه می‌دارد (بنابراین ویرایش‌های قبلیِ دکمه‌ی اصلی دست‌نخورده می‌ماند).
    if group == "back" and len(_callback_group_names(callback_key)) > 1:
        return f"{callback_key}#back"
    return callback_key


# fix: این دکمه‌ها در چند صفحه با «معنی متفاوت» می‌آیند (مثلاً pay_wallet_ هم «تست رایگان» است
# هم «پرداخت از کیف پول»). اگر مشترک ذخیره شوند، تغییر نام یکی روی بقیه هم اعمال می‌شود.
_PER_SCREEN_ONLY = {"pay_wallet_", "pay_online_", "pay_fx_", "pay_tr_", "pay_card_", "pay_crypto_", "discount_plan_"}


def _is_shared_button(callback_key: str, screen_key: str | None = None, group: str | None = None) -> bool:
    """🐛 فیکس: دیگر هیچ دکمه‌ای بین صفحه‌ها «مشترک» نیست. هر دکمه فقط و دقیقاً در جای خودش تغییر می‌کند
    (قبلاً ویرایش مثلاً «ثبت کد تخفیف» یا «بازگشت» یک‌جا، دکمه‌های بازگشت/قبلی/بعدی/رد در صفحه‌های دیگر را هم عوض می‌کرد)."""
    return False


def _button_storage_key(screen_key: str, callback_key: str) -> tuple[str, str]:
    return screen_key, callback_key


def pattern_for(screen_key: str, callback_data: str, label: str | None = None) -> str | None:
    """الگوی دکمه‌ی ثبت‌شده در رجیستری یک صفحه برای یک callback؛ اگر چند الگو بخواند، هم‌نقش (بازگشت/اصلی) برگزیده می‌شود."""
    matches = [(p, lb) for p, lb in (SCREENS.get(screen_key) or {}).get("buttons", []) if _pattern_matches(p, str(callback_data))]
    if not matches:
        return None
    if len(matches) > 1 and label is not None:
        grp = _label_group(label)
        for p, lb in matches:
            if _label_group(lb) == grp:
                return p
    return matches[0][0]


_UNSHARE_DONE = False


def _migrate_unshare() -> None:
    """یک‌بار: ویرایش‌های قدیمیِ «مشترک» (__global__) را روی تک‌تک جای‌هایشان کپی می‌کند تا ظاهر ربات تغییر نکند،
    ولی از این به بعد هر دکمه جداگانه قابل ویرایش باشد."""
    global _UNSHARE_DONE
    if _UNSHARE_DONE:
        return
    try:
        if db.get_setting("ui_btn_unshare_v1", "") == "1":
            _UNSHARE_DONE = True
            return
        with db.transaction() as cur:
            cur.execute("SELECT callback_key, text FROM ui_button_overrides WHERE screen_key='__global__'")
            glob_text = cur.fetchall()
            cur.execute("SELECT callback_key, custom_emoji_id, hidden, style FROM ui_button_meta WHERE screen_key='__global__'")
            glob_meta = cur.fetchall()

            def _targets(stored_cb):
                if stored_cb.endswith("#back"):
                    base, only = stored_cb[:-5], "back"
                else:
                    base, only = stored_cb, None
                occ = [(sk, pat, _label_group(lb)) for sk, sc in SCREENS.items()
                       for pat, lb in sc.get("buttons", []) if pat == base]
                both = {g for _, _, g in occ} == {"back", "main"}
                out = []
                for sk, pat, g in occ:
                    if only == "back" and g != "back":
                        continue
                    if only is None and both and g == "back":
                        continue  # کلید ساده = نقش «اصلی» وقتی هر دو نقش وجود دارد
                    out.append((sk, pat))
                return out

            for cbk, text in glob_text:
                for sk, pat in _targets(cbk):
                    cur.execute("INSERT OR IGNORE INTO ui_button_overrides(screen_key,callback_key,text) VALUES(?,?,?)", (sk, pat, text))
            for cbk, emoji, hidden, style in glob_meta:
                for sk, pat in _targets(cbk):
                    cur.execute(
                        "INSERT OR IGNORE INTO ui_button_meta(screen_key,callback_key,custom_emoji_id,hidden,style) VALUES(?,?,?,?,?)",
                        (sk, pat, emoji, hidden or 0, style))
            cur.execute("DELETE FROM ui_button_overrides WHERE screen_key='__global__'")
            cur.execute("DELETE FROM ui_button_meta WHERE screen_key='__global__'")
        db.set_setting("ui_btn_unshare_v1", "1")
        _UNSHARE_DONE = True
        cache.invalidate_prefix("uibtn:")
        cache.invalidate_prefix("uibtnmeta:")
    except Exception as exc:
        if "no such table" in str(exc).lower():
            logger.debug("ui button unshare migration deferred (db not ready)")
        else:
            logger.exception("ui button unshare migration failed")


def get_screen(key: str):
    return SCREENS.get(key)


def category_label(key: str):
    return CATEGORIES.get(key, key)


def editor_mode_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ ویرایش متن", callback_data="ui_mode:text", style="primary")],
        [InlineKeyboardButton(text="🔘 ویرایش دکمه‌ها", callback_data="ui_mode:buttons", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def categories_keyboard(mode: str = "text"):
    _ensure()
    items = list(CATEGORIES.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(text=v, callback_data=f"ui_cat:{mode}:{k}", style="primary") for k, v in items[i:i + 2]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_text_editor", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_TEXT_ONLY_EXCLUDED_PREFIXES = (
    "main_btn_",
    "btn_",
    "pay_",
    "wallet_pay_",
    "renew_pay_",
    "custom_pay_",
    "online_payment_",
    "invoice_copy_",
)
_TEXT_ONLY_EXCLUDED_KEYS = {
    "join_confirm",
    "insufficient_charge",
}


# دسته‌هایی که فقط متن دکمه‌اند؛ در «ویرایش متن‌ها» نمایش داده نمی‌شوند (از «ویرایش دکمه‌ها» قابل‌ویرایش‌اند)
_TEXT_ONLY_EXCLUDED_CATEGORIES = ("🔘 دکمه‌های عمومی", "💳 دکمه‌های روش پرداخت")
# کلیدهایی که برچسب دکمه‌اند (نه متن پیام)
_BUTTON_LABEL_KEYS = {
    "config_back_service", "config_qr", "config_sub", "config_refresh", "config_mirror", "config_enable",
    "config_disable", "config_revoke", "config_delete", "config_back", "config_name_auto",
    "confirm_delete_yes", "confirm_delete_no", "service_search", "buy_service_button",
    "service_delivery_apps_button", "service_delivery_connection_button",
    "renew_volume_custom", "renew_days_custom", "renew_volume_30", "renew_days_30", "renew_cancel",
    "plans_custom_button", "custom_cancel", "admin_custom_approve", "admin_custom_reject",
    "admin_custom_send_manual", "admin_ticket_reply_btn", "admin_ticket_close_btn",
    "crypto_choose_asset", "crypto_copy_wallet", "crypto_copy_amount",
    "wallet_charge", "wallet_discount", "wallet_transactions", "charge_custom", "wallet_check_pay",
    "wallet_cancel", "discount_cancel", "agency_cancel", "notif_view_service",
    "profile_free_wallet", "profile_locked_wallet", "profile_history", "profile_transactions",
    "profile_referral", "profile_back", "referral_back", "support_ticket", "support_channels",
    "support_back", "guides_back", "guide_detail_back",
    "admin_crypto_approve", "admin_crypto_reject", "admin_renew_approve", "admin_renew_reject",
}


def _is_text_catalog_content_key(key: str) -> bool:
    if key in _TEXT_ONLY_EXCLUDED_KEYS or key in _BUTTON_LABEL_KEYS:
        return False
    return not any(key.startswith(prefix) for prefix in _TEXT_ONLY_EXCLUDED_PREFIXES)


def text_catalog_entries(category: str):
    """متن‌های واقعی ربات در یک دسته: کاتالوگ دستیِ فعال + متن‌های کشف‌شده‌ی همان دسته."""
    out = []
    if category in _TEXT_ONLY_EXCLUDED_CATEGORIES:
        return []
    try:
        import text_catalog as _tc
        dead = _dead_catalog_keys()
        out = [(k, d) for k, d in _tc.TEXT_CATEGORIES.get(category, [])
               if _is_text_catalog_content_key(k) and k not in dead]
    except Exception:
        out = []
    _discover_auto_messages()
    out += [(it["key"], it["template"]) for it in _AUTO_REGISTRY.values() if it.get("category") == category]
    return out


def text_catalog_categories():
    """همه‌ی دسته‌هایی که متن واقعی دارند (کاتالوگ + کشف‌شده‌ها در دسته‌ی درست خودشان)."""
    try:
        import text_catalog as _tc
        cats = list(_tc.TEXT_CATEGORIES.keys())
    except Exception:
        cats = []
    _discover_auto_messages()
    for it in _AUTO_REGISTRY.values():
        c = it.get("category")
        if c and c not in cats:
            cats.append(c)
    return [cat for cat in cats if text_catalog_entries(cat)]


def text_catalog_find(key: str):
    if str(key).startswith(("auto_", "msg_")):
        _discover_auto_messages()
        item = _AUTO_REGISTRY.get(str(key))
        if item:
            return item["category"], item["template"]
    try:
        import text_catalog as _tc
        for cat in _tc.TEXT_CATEGORIES.keys():
            for k, default in _tc.TEXT_CATEGORIES.get(cat, []):
                if k == key:
                    return cat, default
    except Exception:
        pass
    return "", ""


def text_categories_keyboard():
    """کیبورد دسته‌بندی‌های متن از text_catalog"""
    try:
        import text_catalog as _tc
        cats = text_catalog_categories()
    except Exception:
        cats = []
    rows = []
    for i in range(0, len(cats), 2):
        row = []
        for cat in cats[i:i+2]:
            row.append(InlineKeyboardButton(
                text=cat,
                callback_data=f"uitc_cat:{cat[:40]}",
                style="primary"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_text_editor", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def text_category_entries_keyboard(category: str, page: int = 0, page_size: int = 8):
    """کیبورد فهرست متن‌های یک دسته"""
    try:
        import text_catalog as _tc
        entries = text_catalog_entries(category)
    except Exception:
        entries = []
    total = len(entries)
    start = page * page_size
    page_entries = entries[start:start + page_size]
    rows = []
    for key, default_text in page_entries:
        current = get_text(key, default_text).replace('\n', ' ').strip()
        preview = current[:28] + ('…' if len(current) > 28 else '')
        rows.append([InlineKeyboardButton(
            text=f"✏️ {preview}",
            callback_data=f"uitc_key:{key}",
            style="primary"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"uitc_pg:{category[:30]}:{page-1}", style="primary"))
    if start + page_size < total:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"uitc_pg:{category[:30]}:{page+1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به دسته‌بندی‌ها", callback_data="uitc_cats", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def text_entry_keyboard(key: str, category: str = ""):
    """کیبورد ویرایش یک متن"""
    rows = [
        [InlineKeyboardButton(text="✏️ ویرایش این متن", callback_data=f"uitc_edit:{key}", style="primary")],
    ]
    # بازنشانی به پیش‌فرض
    rows.append([InlineKeyboardButton(text="🔄 بازنشانی به پیش‌فرض", callback_data=f"uitc_reset:{key}", style="danger")])
    if category:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"uitc_cat:{category[:40]}", style="danger")])
    else:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="uitc_cats", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def all_button_entries(page: int = 0, page_size: int = 8):
    """فهرست جامع دکمه‌ها؛ callbackهای مشترک فقط یک‌بار نمایش داده می‌شوند."""
    _ensure()
    items = []
    seen_callbacks = set()
    for screen_key, screen in SCREENS.items():
        for cb, label in screen.get("buttons", []):
            _grp = _label_group(label)
            if _is_shared_button(cb, group=_grp):
                if (cb, _grp) in seen_callbacks:
                    continue
                seen_callbacks.add((cb, _grp))
                occurrences = _callback_occurrences(cb, _grp)
                screen_key = occurrences[0][0] if occurrences else screen_key
            else:
                ident = (screen_key, cb)
                if ident in seen_callbacks:
                    continue
                seen_callbacks.add(ident)
            items.append({
                "screen_key": screen_key,
                "callback_key": cb,
                "label": label,
                "screen_label": screen.get("label", screen_key),
                "shared": _is_shared_button(cb, group=_grp),
            })
    items.sort(key=lambda x: (not x.get("shared", False), x["screen_label"], x["callback_key"]))
    start = max(0, int(page)) * page_size
    return items[start:start + page_size], len(items)


def _button_in_bundle(item: dict, bundle_key: str) -> bool:
    bundle = BUTTON_BUNDLES.get(bundle_key) or {}
    if not bundle:
        return False
    screen_key = item.get("screen_key")
    callback_key = item.get("callback_key")
    if screen_key in set(bundle.get("screen_keys") or set()):
        return True
    if callback_key in set(bundle.get("callback_keys") or set()):
        return True
    for prefix in bundle.get("callback_prefixes") or ():
        if callback_key == prefix or str(callback_key).startswith(prefix):
            return True
    return False


def button_bundle_entries(bundle_key: str):
    items, _total = all_button_entries(0, 10000)
    return [item for item in items if _button_in_bundle(item, bundle_key)]


def button_bundles_keyboard():
    rows = []
    for key, meta in BUTTON_BUNDLES.items():
        count = len(button_bundle_entries(key))
        rows.append([InlineKeyboardButton(text=f"{meta['label']} ({count})", callback_data=f"ui_btnbundle:{key}", style="primary")])
    rows.append([InlineKeyboardButton(text="🧾 همه‌ی دکمه‌ها", callback_data="ui_btnbundle:all", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_text_editor", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button_bundle_keyboard(bundle_key: str, page: int = 0, page_size: int = 8):
    if bundle_key == "all":
        items, total = all_button_entries(page, page_size)
        bundle_label = "همه‌ی دکمه‌ها"
    else:
        all_items = button_bundle_entries(bundle_key)
        total = len(all_items)
        start = max(0, int(page)) * page_size
        items = all_items[start:start + page_size]
        bundle_label = (BUTTON_BUNDLES.get(bundle_key) or {}).get("label", bundle_key)

    rows = []
    for item in items:
        label = get_button(item["screen_key"], item["callback_key"], item["label"])
        meta = get_button_meta(item["screen_key"], item["callback_key"])
        hidden_mark = "🚫" if meta["hidden"] else "👁"
        screen_text = item["screen_label"]
        short = label if len(label) <= 20 else label[:17] + "…"
        rows.append([
            InlineKeyboardButton(text=f"✏️ {short}", callback_data=f"ui_button:{item['screen_key']}:{item['callback_key']}", style="primary"),
            InlineKeyboardButton(text=hidden_mark, callback_data=f"ui_toggle:{item['screen_key']}:{item['callback_key']}", style="primary"),
            InlineKeyboardButton(text=_style_dot(effective_style(item["screen_key"], item["callback_key"])), callback_data=f"ui_style:{item['screen_key']}:{item['callback_key']}", style="primary"),
            InlineKeyboardButton(text="↕️", callback_data=f"ui_screen:buttons:{item['screen_key']}", style="primary"),
        ])
        rows.append([InlineKeyboardButton(text=f"📂 {screen_text}", callback_data=f"ui_screen:buttons:{item['screen_key']}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"ui_bundle_page:{bundle_key}:{page-1}", style="primary"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"ui_bundle_page:{bundle_key}:{page+1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=f"🔙 بازگشت به بسته‌ها ({bundle_label})", callback_data="ui_mode:buttons", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def all_buttons_keyboard(page: int = 0, page_size: int = 8):
    return button_bundle_keyboard("all", page, page_size)


def screens_keyboard(cat: str, mode: str = "text"):
    _ensure()
    if cat == "all_messages" and mode == "text":
        return text_categories_keyboard()
    if cat == "all_buttons" and mode == "buttons":
        return all_buttons_keyboard(0)
    rows = []
    for key, screen in SCREENS.items():
        if screen.get("category") != cat:
            continue
        # فیلتر: اگر نه default text دارد نه button، نشان نده
        has_text = bool((screen.get("default") or "").strip())
        has_buttons = bool(screen.get("buttons"))
        if mode == "text" and not has_text:
            continue
        if mode == "buttons" and not has_buttons:
            continue
        rows.append([InlineKeyboardButton(text=screen["label"], callback_data=f"ui_screen:{mode}:{key}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="ui_mode:" + mode, style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# 🐛 ریشه‌ی «ویرایش کردم ولی عوض نمی‌شود»: ویرایشگر متن‌ها فقط کلیدهای «کاتالوگ» را ذخیره می‌کند
# (مثلاً profile_overview)، ولی صفحه‌ی واقعی ربات از کلید صفحه (profile) می‌خواند؛ پس ویرایش
# هیچ‌وقت به آن صفحه نمی‌رسید و فقط تکه‌هایی که اتفاقی با متن کد یکی بودند پریمیوم می‌شدند.
# اگر برای کلید صفحه چیزی ذخیره نشده باشد، متن/entityهای کلید کاتالوگِ هم‌معنی خوانده می‌شود.
_CATALOG_ALIASES = {
    "profile": "profile_overview",
    "plans": "plans_intro",
    "buy_plans": "plans_intro",
    "guides_has": "guides_intro",
}


def _alias_row(key: str):
    alias = _CATALOG_ALIASES.get(key)
    if not alias:
        return None
    cur = db.get_connection().cursor()
    cur.execute("SELECT text, entities_json FROM ui_text_overrides WHERE key=?", (alias,))
    return cur.fetchone()


def get_text(key: str, fallback: str | None = None) -> str:
    _ensure()
    # perf: پرتکرارترین تابع کل ربات — تقریباً هر متنی که به کاربر نشون داده
    # می‌شه از همینجا رد می‌شه؛ کش کردنش بیشترین تاثیر رو روی سرعت پاسخ‌گویی داره.
    cache_key = f"uitext:{key}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value if cached_value is not None else (fallback if fallback is not None else SCREENS.get(key, {}).get("default", ""))
    cur = db.get_connection().cursor()
    cur.execute("SELECT text FROM ui_text_overrides WHERE key=?", (key,))
    row = cur.fetchone()
    if row:
        cache.set(cache_key, row[0])
        return row[0]
    arow = _alias_row(key)
    if arow and arow[0]:
        cache.set(cache_key, arow[0])
        return arow[0]
    # Overrides saved by older Editor versions used a hash of the template only.
    # Keep them readable after the new source-specific catalog is deployed.

    cache.set(cache_key, None)
    if fallback is not None and fallback != "":
        return fallback
    auto_default = None
    if str(key).startswith(("auto_", "msg_")):
        _discover_auto_messages()
        auto_default = (_AUTO_REGISTRY.get(str(key)) or {}).get("template")
    if auto_default is not None:
        return auto_default
    return fallback if fallback is not None else SCREENS.get(key, {}).get("default", "")


def has_text_override(key: str) -> bool:
    """آیا برای این کلید متنِ ذخیره‌شده‌ی ویرایشگر وجود دارد؟"""
    _ensure()
    ck = f"uihas:{key}"
    val, hit = cache.get(ck)
    if hit:
        return bool(val)
    cur = db.get_connection().cursor()
    cur.execute("SELECT 1 FROM ui_text_overrides WHERE key=?", (key,))
    found = cur.fetchone() is not None or _alias_row(key) is not None
    cache.set(ck, found)
    return found


def plans_intro_key() -> str:
    """متن معرفی «خرید اشتراک» در ویرایشگر دو ورودی دارد: «plans» و «buy_plans».
    قبلاً دکمه‌ی منوی پایین از buy_plans و دکمه‌ی شیشه‌ای از plans می‌خواند؛ پس هرکدام را
    ویرایش می‌کردی فقط یکی از مسیرها عوض می‌شد. حالا هر دو مسیر از کلیدی می‌خوانند که
    ویرایش شده (اولویت با plans)."""
    for k in ("plans", "buy_plans"):
        if has_text_override(k):
            return k
    return "plans"


def get_alert_text(key: str, fallback: str = "") -> str:
    """متن کوتاه Callback Alert/Toast را از همان مخزن ویرایش متن می‌خواند."""
    return get_text(key, fallback)


def get_entities(key: str):
    _ensure()
    cache_key = f"uientities:{key}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = db.get_connection().cursor()
    cur.execute("SELECT entities_json FROM ui_text_overrides WHERE key=?", (key,))
    row = cur.fetchone()
    if not row:
        row = _alias_row(key)
    if not row or not row[-1]:
        cache.set(cache_key, [])
        return []
    try:
        value = json.loads(row[-1])
    except Exception:
        value = []
    cache.set(cache_key, value)
    return value



def _utf16_to_py_index(text: str, offset_units: int) -> int:
    units = 0
    for i, ch in enumerate(text):
        if units >= offset_units:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def _py_to_utf16_offset(text: str, index: int) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text[:index])


def _sanitize_config_detail_template(template: str, entities: list[dict]):
    """دو فیلد بلااستفاده‌ی قدیمی جزئیات سرویس را حذف می‌کند و آفست Custom Emojiها را سالم نگه می‌دارد."""
    if not template:
        return template, entities

    blocked_labels = (
        "🔃 آخرین زمان آپدیت لینک اشتراک",
        "🌍 کلاینت متصل شده",
    )
    removed = []
    pieces = []
    cursor = 0
    pos = 0
    for line in template.splitlines(keepends=True):
        line_start = pos
        line_end = pos + len(line)
        pos = line_end
        if any(label in line for label in blocked_labels):
            removed.append((line_start, line_end))
            continue
        pieces.append(line)
    if not removed:
        return template, entities

    sanitized = "".join(pieces)
    remapped = []
    for entity in entities or []:
        try:
            old_start = _utf16_to_py_index(template, int(entity.get("offset", 0)))
            old_end = _utf16_to_py_index(template, int(entity.get("offset", 0)) + int(entity.get("length", 0)))
        except Exception:
            continue
        if any(old_start < end and old_end > start for start, end in removed):
            continue
        shift = sum(end - start for start, end in removed if end <= old_start)
        item = dict(entity)
        new_start = max(0, old_start - shift)
        new_end = max(new_start, old_end - shift)
        item["offset"] = _py_to_utf16_offset(sanitized, new_start)
        item["length"] = _py_to_utf16_offset(sanitized, new_end) - item["offset"]
        remapped.append(item)
    return sanitized, remapped


def _fmt_value(values: dict, match) -> str:
    """مقدار placeholder را با قالب اختیاری ({amount:,} و …) برمی‌گرداند."""
    name = match.group(1)
    if name not in values:
        return match.group(0)
    value = values[name]
    spec_match = re.match(r"\{[A-Za-z_][A-Za-z0-9_]*:([^}]*)\}", match.group(0))
    if spec_match:
        try:
            return format(value, spec_match.group(1))
        except Exception:
            pass
    return str(value)


def render_template(key: str, values: dict[str, Any], fallback: str | None = None):
    """متن ذخیره‌شده را با placeholderها رندر می‌کند و Custom Emojiها و فرمت (bold/code/quote/…) را حفظ می‌کند.

    🐛 فیکس: قبلاً هر entity که با یک {placeholder} هم‌پوشانی داشت (مثلاً لینک اتصال داخل کد/بولد/نقل‌قول
    یا کل متن داخل blockquote) کاملاً دور ریخته می‌شد و فرمت (ریچ‌تکست) از پیام می‌پرید. حالا مرزهای
    entity روی متن جدید نگاشت می‌شود: entity دور placeholder به اندازه‌ی مقدار جایگزین کش می‌آید.
    فقط Custom Emoji که خودش با یک placeholder هم‌پوشانی دارد حذف می‌شود (offset آن معنی ندارد).
    """
    template = get_text(key, fallback)
    entities = get_entities(key)
    if key == "config_detail":
        template, entities = _sanitize_config_detail_template(template, entities)
    if key == "gateway_tonpays_card" and "{amount} تومان" in template:
        # مبلغ دقیق واریز تون‌پیز ریالی است؛ قالب‌های ذخیره‌شده‌ی قدیمی («تومان») اصلاح می‌شوند.
        _old, _new = "{amount} تومان", "{amount} ریال"
        _ents = [dict(e) if isinstance(e, dict) else e for e in (entities or [])]
        _pos = 0
        while True:
            _i = template.find(_old, _pos)
            if _i < 0:
                break
            _u16 = len(template[:_i + len(_old)].encode("utf-16-le")) // 2
            _delta = len(_new.encode("utf-16-le")) // 2 - len(_old.encode("utf-16-le")) // 2
            for _e in _ents:
                if isinstance(_e, dict):
                    if _e.get("offset", 0) >= _u16:
                        _e["offset"] += _delta
                    elif _e.get("offset", 0) + _e.get("length", 0) >= _u16 and _e.get("offset", 0) < _u16:
                        _e["length"] += _delta
            template = template[:_i] + _new + template[_i + len(_old):]
            _pos = _i + len(_new)
        entities = _ents
    if not values:
        return template, entities

    import re
    pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")
    parts = []
    cursor = 0
    new_len = 0
    # (old_start, old_end, new_start, new_end) برای هر placeholder (اندیس پایتونی)
    spans = []
    for match in pattern.finditer(template):
        before = template[cursor:match.start()]
        parts.append(before)
        new_len += len(before)
        replacement = _fmt_value(values, match)
        spans.append((match.start(), match.end(), new_len, new_len + len(replacement)))
        parts.append(replacement)
        new_len += len(replacement)
        cursor = match.end()
    parts.append(template[cursor:])
    rendered = "".join(parts)

    def _map(pos: int, is_end: bool) -> int:
        delta = 0
        for o_s, o_e, n_s, n_e in spans:
            if pos <= o_s:
                break
            if pos >= o_e:
                delta = n_e - o_e
                continue
            return n_e if is_end else n_s      # وسط یک placeholder
        return pos + delta

    remapped = []
    for entity in entities or []:
        try:
            old_start = _utf16_to_py_index(template, int(entity.get("offset", 0)))
            old_end = _utf16_to_py_index(template, int(entity.get("offset", 0)) + int(entity.get("length", 0)))
        except Exception:
            continue
        if old_end <= old_start:
            continue
        if entity.get("type") == "custom_emoji" and any(old_start < o_e and old_end > o_s for o_s, o_e, _a, _b in spans):
            continue
        new_start = _map(old_start, False)
        new_end = _map(old_end, True)
        if new_end <= new_start:
            continue
        item = dict(entity)
        item["offset"] = _py_to_utf16_offset(rendered, new_start)
        item["length"] = _py_to_utf16_offset(rendered, new_end) - item["offset"]
        if item["length"] <= 0:
            continue
        remapped.append(item)
    return rendered, remapped

def set_text(key: str, text: str, entities=None):
    _ensure()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_text_overrides(key,text,entities_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET text=excluded.text,entities_json=excluded.entities_json,updated_at=excluded.updated_at",
            (key, text, json.dumps(entities or [], ensure_ascii=False), datetime.datetime.now().isoformat()),
        )
    cache.invalidate(f"uitext:{key}")
    cache.invalidate(f"uientities:{key}")
    cache.invalidate(f"uihas:{key}")
    for _k, _a in _CATALOG_ALIASES.items():
        if _a == key:
            cache.invalidate(f"uitext:{_k}")
            cache.invalidate(f"uientities:{_k}")
            cache.invalidate(f"uihas:{_k}")
    cache.invalidate("uioverride_entries")
    # Auto-rendered messages may contain this override.
    cache.invalidate_prefix("uiauto:")


def get_button(key: str, callback_key: str, default: str) -> str:
    _ensure()
    gkey, gcb = _button_storage_key(key, callback_key)
    cache_key = f"uibtn:{gkey}:{gcb}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value if cached_value is not None else default
    cur = db.get_connection().cursor()
    if gkey == "__global__":
        cur.execute("SELECT text FROM ui_button_overrides WHERE screen_key=? AND callback_key=?", (gkey, gcb))
        row = cur.fetchone()
        if not row:
            # Backward compatibility: prefer the first existing per-screen override (same role only).
            for sk, cb in _callback_occurrences(callback_key, _button_group(key, callback_key)):
                cur.execute("SELECT text FROM ui_button_overrides WHERE screen_key=? AND callback_key=?", (sk, cb))
                row = cur.fetchone()
                if row:
                    break
    else:
        cur.execute("SELECT text FROM ui_button_overrides WHERE screen_key=? AND callback_key=?", (gkey, gcb))
        row = cur.fetchone()
    cache.set(cache_key, row[0] if row else None)
    return row[0] if row else default


def set_button(key: str, callback_key: str, text: str, custom_emoji_id: str | None = None):
    _ensure()
    gkey, gcb = _button_storage_key(key, callback_key)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_button_overrides(screen_key,callback_key,text) VALUES(?,?,?) "
            "ON CONFLICT(screen_key,callback_key) DO UPDATE SET text=excluded.text",
            (gkey, gcb, text),
        )
        cur.execute(
            "INSERT INTO ui_button_meta(screen_key,callback_key,custom_emoji_id,hidden) VALUES(?,?,?,COALESCE((SELECT hidden FROM ui_button_meta WHERE screen_key=? AND callback_key=?),0)) "
            "ON CONFLICT(screen_key,callback_key) DO UPDATE SET custom_emoji_id=excluded.custom_emoji_id",
            (gkey, gcb, custom_emoji_id, gkey, gcb),
        )
        # Remove stale per-screen overrides after a shared button is centralized.
        if gkey == "__global__":
            for sk, cb in _callback_occurrences(callback_key, _button_group(key, callback_key)):
                cur.execute("DELETE FROM ui_button_overrides WHERE screen_key=? AND callback_key=?", (sk, cb))
                cur.execute("DELETE FROM ui_button_meta WHERE screen_key=? AND callback_key=?", (sk, cb))
    cache.invalidate_prefix("uibtn:")
    cache.invalidate_prefix("uibtnmeta:")


# دکمه‌هایی که تا وقتی ادمین روشنشان نکرده پنهان‌اند (مثلاً «تمدید» منوی پایین کاربر)
DEFAULT_HIDDEN_BUTTONS = {("main_reply", "renew")}


def get_button_meta(key: str, callback_key: str) -> dict:
    _ensure()
    gkey, gcb = _button_storage_key(key, callback_key)
    cache_key = f"uibtnmeta:{gkey}:{gcb}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = db.get_connection().cursor()
    cur.execute("SELECT custom_emoji_id,hidden,style FROM ui_button_meta WHERE screen_key=? AND callback_key=?", (gkey, gcb))
    row = cur.fetchone()
    if not row and gkey == "__global__":
        for sk, cb in _callback_occurrences(callback_key, _button_group(key, callback_key)):
            cur.execute("SELECT custom_emoji_id,hidden,style FROM ui_button_meta WHERE screen_key=? AND callback_key=?", (sk, cb))
            row = cur.fetchone()
            if row:
                break
    value = {"custom_emoji_id": row[0] if row else None,
             "hidden": bool(row[1]) if row else ((key, callback_key) in DEFAULT_HIDDEN_BUTTONS),
             "style": row[2] if row else None}
    cache.set(cache_key, value)
    return value


_VALID_BUTTON_STYLES = {"primary", "secondary", "success", "danger"}
_STYLE_CYCLE = ["primary", "success", "danger"]
_STYLE_DOTS = {None: "⚪️", "primary": "🔵", "success": "🟢", "danger": "🔴"}


def _style_dot(style: str | None) -> str:
    return _STYLE_DOTS.get(style, "⚪️")


# ---------------------------------------------------------------------------
# 🎨 استایل «واقعی» فعلی دکمه‌ها (پیش‌فرضِ کد + override ادمین)
# ---------------------------------------------------------------------------
_CODE_STYLES: dict | None = None
RUNTIME_STYLES: dict = {}   # (screen, key) -> style کدنویسی‌شده؛ توسط wrapper دکمه‌ها هنگام رندر ثبت می‌شود


def _scan_code_styles() -> dict:
    """با AST همه‌ی فراخوانی‌های InlineKeyboardButton(...) در سورس پروژه را می‌خواند و استایل کدنویسی‌شده‌ی آن‌ها را
    به‌صورت {"keys": {(screen,key): style}, "cbs": [(template_prefix, exact, screen, style)]} برمی‌گرداند."""
    global _CODE_STYLES
    if _CODE_STYLES is not None:
        return _CODE_STYLES
    import ast
    import glob
    base = os.path.dirname(os.path.abspath(__file__))
    files = glob.glob(os.path.join(base, "*.py")) + glob.glob(os.path.join(base, "handlers", "*.py"))
    keys: dict = {}
    cbs: list = []
    kpre: list = []
    for path in files:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name != "InlineKeyboardButton":
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            st = kw.get("style")
            style = st.value if isinstance(st, ast.Constant) and isinstance(st.value, str) else None
            if style not in ("primary", "success", "danger"):
                continue
            scr = kw.get("ui_screen")
            bk = kw.get("ui_button_key")
            scr_v = scr.value if isinstance(scr, ast.Constant) and isinstance(scr.value, str) else None
            bk_v = bk.value if isinstance(bk, ast.Constant) and isinstance(bk.value, str) else None
            if scr_v and bk_v:
                keys.setdefault((scr_v, bk_v), style)
            elif isinstance(bk, ast.JoinedStr) and bk.values and isinstance(bk.values[0], ast.Constant) and bk.values[0].value:
                kpre.append((scr_v or "*", str(bk.values[0].value), style))
            cd = kw.get("callback_data")
            if isinstance(cd, ast.Constant) and isinstance(cd.value, str):
                cbs.append((cd.value, True, scr_v, style))
            elif isinstance(cd, ast.JoinedStr) and cd.values and isinstance(cd.values[0], ast.Constant):
                cbs.append((str(cd.values[0].value), False, scr_v, style))
    _CODE_STYLES = {"keys": keys, "cbs": cbs, "kpre": kpre}
    return _CODE_STYLES


def default_style(key: str, callback_key: str) -> str:
    """رنگ پیش‌فرضی که کد برای این دکمه می‌گذارد (اگر ادمین override نکرده باشد)."""
    rt = RUNTIME_STYLES.get((key, callback_key))
    if rt:
        return rt
    try:
        data = _scan_code_styles()
        st = data["keys"].get((key, callback_key))
        if st:
            return st
        for scr, pre, stl in data.get("kpre", []):
            if scr in (key, "*") and callback_key.startswith(pre):
                return stl
        pat = callback_key
        same_screen, any_screen = [], []
        for tmpl, exact, scr, style in data["cbs"]:
            if exact:
                hit = _pattern_matches(pat, tmpl) if pat.endswith("_") else tmpl == pat
            else:
                hit = bool(tmpl) and (tmpl.startswith(pat) if pat.endswith("_") else tmpl == pat)
                if not hit and pat.endswith("_"):
                    hit = False
            if hit:
                (same_screen if scr == key else any_screen).append(style)
        for pool in (same_screen, any_screen):
            if pool:
                return max(set(pool), key=pool.count)
    except Exception:
        pass
    return "primary"


def effective_style(key: str, callback_key: str) -> str:
    """استایلی که دکمه همین الان واقعاً دارد: override ادمین، وگرنه پیش‌فرض کد."""
    st = get_button_meta(key, callback_key).get("style")
    if st in ("primary", "success", "danger"):
        return st
    if str(callback_key).startswith("cbtn_") and str(callback_key)[5:].isdigit():
        _cb = get_custom_button(int(str(callback_key)[5:]))
        if _cb and _cb.get("style") in ("primary", "success", "danger"):
            return _cb["style"]
    return default_style(key, callback_key)


def set_button_style(key: str, callback_key: str, style: str | None):
    if style is not None and style not in _VALID_BUTTON_STYLES:
        raise ValueError("رنگ دکمه نامعتبر است")
    _ensure()
    gkey, gcb = _button_storage_key(key, callback_key)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_button_meta(screen_key,callback_key,custom_emoji_id,hidden,style) "
            "VALUES(?,?,NULL,COALESCE((SELECT hidden FROM ui_button_meta WHERE screen_key=? AND callback_key=?),0),?) "
            "ON CONFLICT(screen_key,callback_key) DO UPDATE SET style=excluded.style",
            (gkey, gcb, gkey, gcb, style),
        )
    cache.invalidate_prefix("uibtnmeta:")


def toggle_button(key: str, callback_key: str):
    _ensure()
    gkey, gcb = _button_storage_key(key, callback_key)
    old = get_button_meta(key, callback_key)["hidden"]
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_button_meta(screen_key,callback_key,custom_emoji_id,hidden,style) "
            "VALUES(?,?,NULL,?,NULL) ON CONFLICT(screen_key,callback_key) DO UPDATE SET hidden=excluded.hidden",
            (gkey, gcb, 0 if old else 1),
        )
    cache.invalidate_prefix("uibtnmeta:")


def reset_button(key: str, callback_key: str):
    _ensure()
    gkey, gcb = _button_storage_key(key, callback_key)
    with db.transaction() as cur:
        cur.execute("DELETE FROM ui_button_overrides WHERE screen_key=? AND callback_key=?", (gkey, gcb))
        cur.execute("DELETE FROM ui_button_meta WHERE screen_key=? AND callback_key=?", (gkey, gcb))
    cache.invalidate_prefix("uibtn:")
    cache.invalidate_prefix("uibtnmeta:")

def next_button_style(current: str | None) -> str | None:
    """چرخه‌ی یک‌کلیکی رنگ از روی رنگ «فعلی واقعی»: 🔵 → 🟢 → 🔴 → 🔵 ...
    (بازگشت به پیش‌فرض کد با دکمه‌ی ♻️)."""
    try:
        idx = _STYLE_CYCLE.index(current)
    except ValueError:
        idx = -1
    return _STYLE_CYCLE[(idx + 1) % len(_STYLE_CYCLE)]


def get_layout(key: str):
    _ensure()
    cache_key = f"uilayout:{key}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = db.get_connection().cursor()
    cur.execute("SELECT mode,columns,order_json,row_widths_json FROM ui_layouts WHERE screen_key=?", (key,))
    row = cur.fetchone()
    if not row:
        # پیش‌فرض همه منوها یک دکمه در هر ردیف است؛ ادمین می‌تواند
        # از Editor چیدمان ۲/۳/۴تایی را انتخاب و ذخیره کند.
        value = {"mode": "vertical", "columns": 1, "order": None, "row_widths": None}
        cache.set(cache_key, value)
        return value
    try:
        order = json.loads(row[2]) if row[2] else None
    except Exception:
        order = None
    mode = row[0] if row[0] in ("vertical", "inline") else "vertical"
    columns = int(row[1] or 1)
    if mode == "vertical":
        columns = 1
    else:
        columns = min(4, max(1, columns))
    try:
        row_widths = json.loads(row[3]) if row[3] else None
        if not isinstance(row_widths, list):
            row_widths = None
    except Exception:
        row_widths = None
    value = {"mode": mode, "columns": columns, "order": order, "row_widths": row_widths}
    cache.set(cache_key, value)
    return value


def set_layout(key: str, mode: str, columns: int = 1):
    if mode not in ("vertical", "inline"):
        mode = "vertical"
    columns = 1 if mode == "vertical" else min(4, max(1, int(columns)))
    # تعداد دکمه‌ها لازم نیست مضرب columns باشد؛ ردیف آخر می‌تواند ناقص باشد.
    # مثال: 7 دکمه با columns=2 => 2+2+2+1.
    _ensure()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_layouts(screen_key,mode,columns,order_json) VALUES(?,?,?,NULL) "
            "ON CONFLICT(screen_key) DO UPDATE SET mode=excluded.mode,columns=excluded.columns",
            (key, mode, columns),
        )
    cache.invalidate(f"uilayout:{key}")


def set_row_layout(key: str, widths: list[int] | None):
    _ensure()
    clean=[max(1,min(8,int(x))) for x in (widths or []) if int(x)>0]
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_layouts(screen_key,mode,columns,order_json,row_widths_json) VALUES(?,?,?,NULL,?) "
            "ON CONFLICT(screen_key) DO UPDATE SET row_widths_json=excluded.row_widths_json",
            (key, "inline" if clean else "vertical", max(clean or [1]), json.dumps(clean, ensure_ascii=False) if clean else None),
        )
    cache.invalidate(f"uilayout:{key}")


def _base_order(key: str):
    return [cb for cb, _ in SCREENS.get(key, {}).get("buttons", [])]


def custom_button_key(button_id) -> str:
    """کلید ترتیبِ یک دکمه‌ی سفارشی داخل لیست دکمه‌های همان صفحه (بدون «:» تا با callbackهای ui_move قاطی نشود)."""
    return f"cbtn_{int(button_id)}"


def get_order(key: str):
    base = _base_order(key)
    try:
        custom = [] if key == "start" else [custom_button_key(b["id"]) for b in get_custom_buttons(key)]
    except Exception:
        custom = []
    valid = set(base) | set(custom)
    order = get_layout(key).get("order") or base[:]
    # حذف callbackهای قدیمی/نامعتبر و اضافه‌کردن callbackهای جدید (و دکمه‌های سفارشی) به انتهای لیست.
    order = [x for x in order if x in valid]
    order += [x for x in base if x not in order]
    order += [x for x in custom if x not in order]
    return order


def move_button(key: str, callback_key: str, direction: str):
    order = get_order(key)
    if callback_key not in order:
        return
    i = order.index(callback_key)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(order):
        return
    order[i], order[j] = order[j], order[i]
    lay = get_layout(key)
    _ensure()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_layouts(screen_key,mode,columns,order_json) VALUES(?,?,?,?) "
            "ON CONFLICT(screen_key) DO UPDATE SET order_json=excluded.order_json",
            (key, lay["mode"], lay["columns"], json.dumps(order, ensure_ascii=False)),
        )
    cache.invalidate(f"uilayout:{key}")


def _button_default(key: str, callback_key: str) -> str:
    for cb, label in SCREENS.get(key, {}).get("buttons", []):
        if cb == callback_key:
            return label
    return "🔘 دکمه"


def override_button_text(callback_data: str, default: str) -> str:
    """برای سازگاری با تمام محل‌های قدیمی که screen_key را نمی‌فرستند.

    🐛 فیکس: فقط دکمه‌هایی هم‌نقش با `default` در نظر گرفته می‌شوند. یعنی «🔙 بازگشت» که callbackش
    plans است هرگز override دکمه‌ی «🛒 خرید اشتراک» را نمی‌گیرد (و برعکس).
    """
    grp = _label_group(default)
    matches = []
    for key, screen in SCREENS.items():
        for cb, label in screen.get("buttons", []):
            if _pattern_matches(cb, callback_data) and _label_group(label) == grp:
                matches.append((key, cb, label))
    if len(matches) == 1:
        key, cb, label = matches[0]
        return get_button(key, cb, default)
    # چند دکمه با همین callback/نقش: بدون screen_key نمی‌شود فهمید کدام است؛ هرگز حدس نمی‌زنیم
    # (قبلاً «تنها override موجود» روی همه‌ی دکمه‌های مشابه اعمال می‌شد و دکمه‌های دیگر هم عوض می‌شدند).
    return default


def button_meta_for_callback(callback_data: str, default: str | None = None):
    grp = _label_group(default) if default is not None else None
    matches = []
    for key, screen in SCREENS.items():
        for cb, label in screen.get("buttons", []):
            if _pattern_matches(cb, callback_data) and (grp is None or _label_group(label) == grp):
                matches.append((key, cb))
    if not matches:
        return None
    if len(matches) > 1:
        cb = matches[0][1]
        if _is_shared_button(cb, screen_key=matches[0][0]):
            meta = get_button_meta(matches[0][0], cb)
            meta.update({"screen_key": "__global__", "callback_key": cb})
            return meta
        return None
    key, cb = matches[0]
    meta = get_button_meta(key, cb)
    meta.update({"screen_key": key, "callback_key": cb})
    return meta


def _u16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def compose_with_entities(*parts, limit: int = 3900):
    """چند تکه‌ی متن (str یا (متن, entities)) را به‌هم می‌چسباند و offset ایموجی‌های پریمیوم/فرمت هر
    تکه را (بر حسب UTF-16) جابه‌جا می‌کند تا پیش‌نمایش ویرایشگر دقیقاً مثل پیام واقعی دیده شود.
    خروجی: (text, [entity dict]) و متن به `limit` کاراکتر بریده می‌شود (entityهای بریده‌شده حذف/کوتاه می‌شوند)."""
    out = ""
    ents = []
    for part in parts:
        if isinstance(part, str):
            out += part
            continue
        txt, e = part[0] or "", part[1] or []
        shift = _u16_len(out)
        for x in e:
            try:
                d = dict(x)
                d["offset"] = int(d["offset"]) + shift
                d["length"] = int(d["length"])
            except Exception:
                continue
            ents.append(d)
        out += txt
    if len(out) > limit:
        out = out[:limit]
        lim16 = _u16_len(out)
        clipped = []
        for d in ents:
            if d["offset"] >= lim16:
                continue
            if d["offset"] + d["length"] > lim16:
                if d.get("type") == "custom_emoji":
                    continue  # ایموجی نیمه‌بریده معتبر نیست
                d["length"] = lim16 - d["offset"]
            if d["length"] > 0:
                clipped.append(d)
        ents = clipped
    return out, ents


def preview_text_entities(key: str, page: int = 0):
    """مثل preview_text ولی (text, entities) برمی‌گرداند تا ایموجی پریمیوم در پیش‌نمایش حفظ شود."""
    base = get_text(key, SCREENS[key].get("default", ""))
    variants = auto_entries_for_screen(key)
    page_size = 6
    start = max(0, int(page)) * page_size
    visible = variants[start:start + page_size]
    parts = [f"📝 پیش‌نمایش «{SCREENS[key]['label']}»\n\n", (base, get_entities(key))]
    if variants:
        parts.append(f"\n\n🧩 متن‌های واقعی این مسیر — صفحه {page + 1}/{max(1, (len(variants)+page_size-1)//page_size)}")
        for i, item in enumerate(visible, start + 1):
            parts.append(f"\n\n🧩 متن واقعی {i} — {item['source']}\n")
            parts.append((get_text(item["key"], item["template"]), get_entities(item["key"])))
    return compose_with_entities(*parts)


def preview_text(key: str, page: int = 0) -> str:
    base = get_text(key, SCREENS[key].get("default", ""))
    variants = auto_entries_for_screen(key)
    page_size = 6
    start = max(0, int(page)) * page_size
    visible = variants[start:start + page_size]
    chunks = [f"📝 پیش‌نمایش «{SCREENS[key]['label']}»", "", base]
    if variants:
        chunks.extend(["", f"🧩 متن‌های واقعی این مسیر — صفحه {page + 1}/{max(1, (len(variants)+page_size-1)//page_size)}"])
        for i, item in enumerate(visible, start + 1):
            preview = get_text(item["key"], item["template"])
            chunks.extend(["", f"🧩 متن واقعی {i} — {item['source']}", preview])
    result = "\n".join(chunks)
    return result[:3900] if len(result) > 3900 else result


def text_screen_keyboard(key: str, page: int = 0):
    rows = [[InlineKeyboardButton(text="✏️ تغییر متن اصلی", callback_data=f"ui_edit_text:{key}", style="primary")]]
    variants = auto_entries_for_screen(key)
    page_size = 6
    start = max(0, int(page)) * page_size
    visible = variants[start:start + page_size]
    for i, item in enumerate(visible, start + 1):
        current = get_text(item["key"], item["template"]).replace("\n", " ").strip()
        preview = current[:30] + ("…" if len(current) > 30 else "")
        rows.append([InlineKeyboardButton(text=f"🧩 متن {i}: {preview}"[:48], callback_data=f"ui_edit_auto:{item['key']}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"ui_screen_textpage:{key}:{page-1}", style="primary"))
    if (page + 1) * page_size < len(variants):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"ui_screen_textpage:{key}:{page+1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به فهرست متن‌ها", callback_data="ui_mode:text", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button_screen_keyboard(key: str, page: int = 0):
    sc = SCREENS[key]
    lay = get_layout(key)
    order = get_order(key)
    labels = dict(sc.get("buttons") or {})
    page_size = 8
    start = max(0, int(page)) * page_size
    visible_order = order[start:start + page_size]
    rows = []
    for cb in visible_order:
        if cb.startswith("cbtn_"):
            _cb = get_custom_button(int(cb[5:])) if cb[5:].isdigit() else None
            if not _cb:
                continue
            _kind = {"callback": "🔗", "url": "🌐", "webapp": "📲"}.get(_cb["action_type"], "🔘")
            rows.append([InlineKeyboardButton(text=f"➕ {_kind} {_cb['text']} (دکمه‌ی دلخواه)"[:60], callback_data=f"ui_cbtn_open:{_cb['id']}", style="primary",
                                              icon_custom_emoji_id=_cb.get("custom_emoji_id") or None)])
            _csk = _cb["screen_key"]
            _meta = get_button_meta(_csk, cb)
            rows.append([
                InlineKeyboardButton(text="🚫" if _meta["hidden"] else "👁", callback_data=f"ui_toggle:{_csk}:{cb}", style="primary"),
                InlineKeyboardButton(text=_style_dot(effective_style(_csk, cb)), callback_data=f"ui_style:{_csk}:{cb}", style="primary"),
                InlineKeyboardButton(text="⬆️", callback_data=f"ui_move:{key}:{cb}:up", style="primary"),
                InlineKeyboardButton(text="⬇️", callback_data=f"ui_move:{key}:{cb}:down", style="primary"),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"ui_cbtn_del:{_cb['id']}", style="danger"),
            ])
            continue
        if cb not in labels:
            continue
        meta = get_button_meta(key, cb)
        status = "🚫" if meta["hidden"] else "👁"
        # UX: "noop" یک دکمه‌ی تزئینی/جداکننده است (مثلاً خط‌چین بین دو بخش
        # از منو) و هیچ عملکردی هنگام کلیک نداره. قبلاً دقیقاً مثل بقیه‌ی
        # دکمه‌های واقعی نمایش داده می‌شد و ادمین گمان می‌کرد این یه دکمه‌ی
        # خراب/بی‌فایده‌ست؛ الان با برچسب مشخص می‌شه که این فقط یه جداکننده‌ی
        # ظاهریه (هنوز هم می‌شه متن/ایموجیش رو عوض کرد، فقط کلیک روش کاری
        # انجام نمی‌ده).
        if cb == "noop":
            rows.append([
                InlineKeyboardButton(text=f"🏷 (جداکننده‌ی تزئینی) {get_button(key, cb, labels[cb])}"[:60], callback_data=f"ui_button:{key}:{cb}", style="primary"),
                InlineKeyboardButton(text=status, callback_data=f"ui_toggle:{key}:{cb}", style="primary"),
            ])
            continue
        # نام دکمه در ردیف جدا و تمام‌عرض (تا متن کامل خوانده شود)، ابزارها در ردیف زیرش
        rows.append([
            InlineKeyboardButton(text=f"✏️ {get_button(key, cb, labels[cb])}"[:60], callback_data=f"ui_button:{key}:{cb}", style="primary"),
        ])
        rows.append([
            InlineKeyboardButton(text=status, callback_data=f"ui_toggle:{key}:{cb}", style="primary"),
            InlineKeyboardButton(text=_style_dot(effective_style(key, cb)), callback_data=f"ui_style:{key}:{cb}", style="primary"),
            InlineKeyboardButton(text="♻️", callback_data=f"ui_reset:{key}:{cb}", style="danger"),
            InlineKeyboardButton(text="⬆️", callback_data=f"ui_move:{key}:{cb}:up", style="primary"),
            InlineKeyboardButton(text="⬇️", callback_data=f"ui_move:{key}:{cb}:down", style="primary"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"ui_button_page:{key}:{page-1}", style="primary"))
    if (page + 1) * page_size < len(order):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"ui_button_page:{key}:{page+1}", style="primary"))
    if nav:
        rows.append(nav)

    current_cols = min(4, max(1, int(lay.get("columns") or 1)))
    layout_row = []
    for cols in (1, 2, 3, 4):
        mark = "✅ " if cols == current_cols else ""
        layout_row.append(InlineKeyboardButton(
            text=f"{mark}{cols} دکمه/ردیف",
            callback_data=f"ui_layout:{key}:set:{cols}",
         style="primary"))
    rows.append(layout_row)
    rows.append([InlineKeyboardButton(
        text=f"📐 چیدمان فعلی: {current_cols} دکمه در هر ردیف",
        callback_data=f"ui_layout:{key}:cycle:{1 if current_cols >= 4 else current_cols + 1}",
     style="primary")])
    current_rows = ",".join(str(x) for x in (lay.get("row_widths") or [])) or "پیش‌فرض"
    rows.append([InlineKeyboardButton(text=f"🧩 الگوی ردیف‌ها: {current_rows}", callback_data=f"ui_rowlayout:{key}", style="primary")])
    custom_count = len(get_custom_buttons(key))
    rows.append([InlineKeyboardButton(
        text=f"➕ دکمه‌های سفارشی این صفحه ({custom_count})" + (" — منوی اصلی (پایین)" if key == "start" else ""),
        callback_data=f"ui_cbtn_list:{canonical_cbtn_screen(key)}",
     style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به فهرست دکمه‌ها", callback_data="ui_mode:buttons", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# دکمه‌های سفارشی ادمین (افزودن دکمه‌ی دلخواه به هر صفحه)
# ---------------------------------------------------------------------------

BUTTON_STYLES = {
    "primary": "🔵 آبی (پیش‌فرض)",
    "success": "🟢 سبز",
    "danger": "🔴 قرمز",
}

# مقصدهای پرتکرار داخل ربات که ادمین می‌تواند بدون دونستن callback خام،
# دکمه‌ی سفارشی‌اش را مستقیم به آن‌ها وصل کند (مثلاً «راهنما» یا «پشتیبانی»).
QUICK_DESTINATIONS = [
    ("plans", "🛒 خرید اشتراک"),
    ("my_configs", "📱 سرویس‌های من"),
    ("wallet", "💰 کیف پول"),
    ("referral", "👥 دعوت دوستان"),
    ("profile", "👤 پروفایل من"),
    ("support", "👨‍💻 پشتیبانی"),
    ("user_guides", "📚 فهرست راهنماها"),
    ("agency", "🤝 درخواست نمایندگی"),
    ("buy_plan_test", "🎁 تست رایگان"),
    ("cbuild_start", "🛠 بساز سرویس خودت"),
    ("charge", "➕ شارژ کیف پول"),
    ("transactions", "🧾 تراکنش‌های کیف پول"),
    ("purchase_history", "🛍 سابقه خرید"),
    ("ticket", "✉️ ارسال تیکت جدید"),
    ("my_tickets", "🎫 تیکت‌های من"),
    ("back", "🏠 منوی اصلی"),
]

# فهرست کامل callbackهای قابل‌استفاده برای دکمه‌ی دلخواه (متن راهنمای «وارد کردن callback دستی»).
CALLBACK_HELP_TEXT = (
    "✍️ مقدار callback را دقیقاً مثل یکی از موارد زیر بفرست.\n\n"
    "🏠 منو و خرید\n"
    "• back — منوی اصلی\n"
    "• plans — خرید اشتراک (دسته‌ها/پلن‌ها)\n"
    "• plans_vip — پلن‌های VIP\n"
    "• buy_plan_test — تست رایگان\n"
    "• cbuild_start — بساز سرویس خودت (سفارشی)\n"
    "• payg_buy — سرویس پرداخت‌به‌ازای‌مصرف (PAYG)\n"
    "• vipcat_<کلید دسته> — یک دسته‌ی مشخص (مثلاً vipcat_abc)\n"
    "• buy_<کلید پلن> — خرید یک پلن مشخص\n"
    "• cbopt_<شماره> — یک گزینه‌ی سفارشی مشخص (شماره‌ی گزینه)\n"
    "• paygopt_<شماره> — یک گزینه‌ی PAYG مشخص\n"
    "• use_discount — شارژ کیف پول با کد تخفیف\n\n"
    "📱 سرویس‌ها\n"
    "• my_configs — سرویس‌های من\n"
    "• my_configs_vip — سرویس‌های VIP من\n"
    "• service_search — جستجوی سرویس\n\n"
    "💰 کیف پول و پروفایل\n"
    "• wallet — کیف پول\n"
    "• charge — شارژ کیف پول\n"
    "• transactions — تراکنش‌های کیف پول\n"
    "• wallet_free — موجودی آزاد\n"
    "• wallet_locked — موجودی مسدود\n"
    "• profile — پروفایل من\n"
    "• purchase_history — سابقه خرید\n"
    "• referral — دعوت دوستان\n\n"
    "🎫 پشتیبانی و راهنما\n"
    "• support — پشتیبانی\n"
    "• ticket — ارسال تیکت جدید\n"
    "• my_tickets — تیکت‌های من\n"
    "• user_guides — فهرست راهنماها\n"
    "• guideopen_<شناسه> — یک راهنمای مشخص (شناسه‌ی عددی راهنما)\n"
    "• agency — درخواست نمایندگی\n"
    "• check_join — «عضو شدم» (بررسی عضویت اجباری)\n"
    "• noop — بدون عملکرد (فقط ظاهر/جداکننده)\n\n"
    "⚠️ callbackهایی که با admin یا پیشوندهای مدیریتی شروع می‌شوند (مثل admin_..., approve..., panel...) "
    "فقط برای ادمین کار می‌کنند؛ برای دکمه‌ی کاربران استفاده نکن. اگر callback اشتباه باشد، دکمه هنگام فشردن جوابی نمی‌دهد."
)


def canonical_cbtn_screen(screen_key: str) -> str:
    """منوی اصلی واقعی کاربر «منوی پایین» (main_reply) است؛ «start» فقط پیام خوش‌آمد است و
    دکمه‌ی دلخواه روی آن هیچ‌جا نشان داده نمی‌شد. پس هر دو به همان منوی اصلی وصل می‌شوند."""
    return "main_reply" if screen_key == "start" else screen_key


def get_custom_buttons(screen_key: str):
    _ensure()
    screen_key = canonical_cbtn_screen(screen_key)
    cache_key = f"uicbtn:{screen_key}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = db.get_connection().cursor()
    cur.execute(
        "SELECT id,screen_key,text,style,action_type,action_value,position,custom_emoji_id FROM ui_custom_buttons "
        "WHERE screen_key=? ORDER BY position ASC, id ASC",
        (screen_key,),
    )
    rows = cur.fetchall()
    keys = ["id", "screen_key", "text", "style", "action_type", "action_value", "position", "custom_emoji_id"]
    value = [dict(zip(keys, r)) for r in rows]
    cache.set(cache_key, value)
    return value


def get_custom_button(button_id: int):
    _ensure()
    cur = db.get_connection().cursor()
    cur.execute(
        "SELECT id,screen_key,text,style,action_type,action_value,position,custom_emoji_id FROM ui_custom_buttons WHERE id=?",
        (button_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "screen_key", "text", "style", "action_type", "action_value", "position", "custom_emoji_id"]
    return dict(zip(keys, row))


def add_custom_button(screen_key: str, text: str, style: str, action_type: str, action_value: str, custom_emoji_id: str | None = None) -> int:
    _ensure()
    screen_key = canonical_cbtn_screen(screen_key)
    if style not in BUTTON_STYLES:
        style = "primary"
    with db.transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(position),-1)+1 FROM ui_custom_buttons WHERE screen_key=?", (screen_key,))
        next_pos = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ui_custom_buttons(screen_key,text,style,action_type,action_value,position,created_at,custom_emoji_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (screen_key, text, style, action_type, action_value, next_pos, datetime.datetime.now().isoformat(), custom_emoji_id),
        )
        new_id = cur.lastrowid
    cache.invalidate(f"uicbtn:{screen_key}")
    return new_id


def delete_custom_button(button_id: int):
    _ensure()
    btn = get_custom_button(button_id)
    with db.transaction() as cur:
        cur.execute("DELETE FROM ui_custom_buttons WHERE id=?", (button_id,))
    if btn:
        cache.invalidate(f"uicbtn:{btn['screen_key']}")


def _save_order(key: str, order: list):
    lay = get_layout(key)
    _ensure()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO ui_layouts(screen_key,mode,columns,order_json) VALUES(?,?,?,?) "
            "ON CONFLICT(screen_key) DO UPDATE SET order_json=excluded.order_json",
            (key, lay["mode"], lay["columns"], json.dumps(order, ensure_ascii=False)),
        )
    cache.invalidate(f"uilayout:{key}")


def ordered_custom_buttons(screen_key: str):
    """دکمه‌های سفارشی به همان ترتیبی که در لیست کل دکمه‌های صفحه (و روی کیبورد کاربر) هستند."""
    items = get_custom_buttons(screen_key)
    pos = {k: i for i, k in enumerate(get_order(screen_key))}
    return sorted(items, key=lambda b: pos.get(custom_button_key(b["id"]), 10**6))


def move_custom_button(button_id: int, direction: str):
    """جابه‌جایی نسبت به دکمه‌های سفارشی همسایه (در لیست خودِ دکمه‌های سفارشی)؛ ترتیب کلی صفحه هم همین‌جا به‌روز می‌شود."""
    btn = get_custom_button(button_id)
    if not btn:
        return
    key = btn["screen_key"]
    sibs = ordered_custom_buttons(key)
    idx = next((i for i, b in enumerate(sibs) if b["id"] == button_id), None)
    if idx is None:
        return
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(sibs):
        return
    order = get_order(key)
    ka, kb = custom_button_key(sibs[idx]["id"]), custom_button_key(sibs[j]["id"])
    ia, ib = order.index(ka), order.index(kb)
    order[ia], order[ib] = order[ib], order[ia]
    _save_order(key, order)


def custom_buttons_keyboard(screen_key: str):
    items = ordered_custom_buttons(screen_key)
    rows = []
    for b in items:
        kind = {"callback": "🔗", "url": "🌐", "webapp": "📲"}.get(b["action_type"], "🔘")
        _ck = custom_button_key(b["id"])
        rows.append([
            InlineKeyboardButton(text=f"{kind} {b['text']}"[:40], callback_data=f"ui_cbtn_open:{b['id']}", style="primary",
                                  icon_custom_emoji_id=b.get("custom_emoji_id") or None),
            InlineKeyboardButton(text="🚫" if get_button_meta(b["screen_key"], _ck)["hidden"] else "👁", callback_data=f"ui_cbtn_vis:{b['id']}", style="primary"),
            InlineKeyboardButton(text=_style_dot(effective_style(b["screen_key"], _ck)), callback_data=f"ui_cbtn_col:{b['id']}", style="primary"),
            InlineKeyboardButton(text="⬆️", callback_data=f"ui_cbtn_move:{b['id']}:up", style="primary"),
            InlineKeyboardButton(text="⬇️", callback_data=f"ui_cbtn_move:{b['id']}:down", style="primary"),
            InlineKeyboardButton(text="🗑", callback_data=f"ui_cbtn_del:{b['id']}", style="primary"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن دکمه‌ی جدید", callback_data=f"ui_cbtn_add:{screen_key}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به تنظیمات دکمه‌های این صفحه", callback_data=f"ui_screen:buttons:{screen_key}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_button_style_keyboard(screen_key: str):
    rows = [[InlineKeyboardButton(text=v, callback_data=f"ui_cbtn_style:{screen_key}:{k}", style="primary")] for k, v in BUTTON_STYLES.items()]
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"ui_cbtn_list:{screen_key}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_button_action_type_keyboard(screen_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 وصل به یکی از صفحات ربات", callback_data=f"ui_cbtn_type:{screen_key}:callback_quick", style="success")],
        [InlineKeyboardButton(text="✍️ وارد کردن callback دستی (پیشرفته)", callback_data=f"ui_cbtn_type:{screen_key}:callback_manual", style="danger")],
        [InlineKeyboardButton(text="🌐 لینک وب معمولی", callback_data=f"ui_cbtn_type:{screen_key}:url", style="primary")],
        [InlineKeyboardButton(text="📲 اپ‌لینک (Web App داخل تلگرام)", callback_data=f"ui_cbtn_type:{screen_key}:webapp", style="primary")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data=f"ui_cbtn_list:{screen_key}", style="danger")],
    ])


def quick_destination_keyboard(screen_key: str):
    rows = [[InlineKeyboardButton(text=label, callback_data=f"ui_cbtn_dest:{screen_key}:{cb}", style="primary")] for cb, label in QUICK_DESTINATIONS]
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"ui_cbtn_list:{screen_key}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def screen_preview_markup(key: str):
    sc = SCREENS.get(key) or {}
    labels = dict(sc.get("buttons") or [])
    order = get_order(key)
    lay = get_layout(key)
    visible = []
    for cb in order:
        if cb not in labels or get_button_meta(key, cb)["hidden"]:
            continue
        visible.append(InlineKeyboardButton(text=get_button(key, cb, labels[cb]), callback_data=cb[:64], style="primary"))
    cols = lay["columns"] if lay["mode"] == "inline" else 1
    return InlineKeyboardMarkup(inline_keyboard=[visible[i:i+cols] for i in range(0, len(visible), cols)])


CATEGORY_LABELS = {
    # کاربران
    "start":          "🚀 شروع",
    "main_menu":      "🏠 منوی اصلی",
    "shop":           "🛒 خرید",
    "renewal":        "🔁 تمدید",
    "services":       "📱 سرویس‌ها",
    "free_test":      "🎁 تست",
    "custom_service": "🛠 سرویس سفارشی",
    "wallet":         "💰 کیف پول",
    "payment":        "💳 پرداخت",
    "invoice":        "🧧 فاکتور",
    "discount":       "🎟 تخفیف",
    "referral":       "🤝 رفرال",
    "agency":         "🤝 نمایندگی",
    "active_services":"🟢 سرویس‌های فعال",
    "expired_services":"🔴 سرویس‌های منقضی",
    "errors":         "🙅 پیام‌های خطا",
    "success":        "✅ پیام‌های موفقیت",
    "help":           "📚 راهنما",
    "support":        "🎫 پشتیبانی",
    # مدیریت
    "admin":          "🛡 پنل مدیریت",
    "admin_users":    "👥 مدیریت کاربران",
    "admin_stats":    "📊 آمار",
    "admin_tickets":  "🎫 تیکت‌ها",
    "admin_broadcast":"📢 پیام همگانی",
    "admin_orders":   "📋 سفارش‌ها",
    "admin_payments": "💰 پرداخت‌ها",
    "admin_receipts": "🧧 رسیدها",
    "admin_services": "📱 سرویس‌ها",
    "admin_panels":   "🖥 پنل‌ها",
    "admin_vip":      "🚀 VIP",
    "admin_test":     "🎁 تست",
    "admin_renewal":  "🔁 تمدید",
    "admin_agents":   "🤝 نمایندگان",
    "admin_discount": "🎟 تخفیف‌ها",
    "admin_referral": "🤝 رفرال",
    "admin_wallet":   "💰 کیف پول",
    "admin_payg":     "⚡ Pay As You Go",
    "admin_miniapp":  "🏠 Mini App",
    "admin_settings": "⚙️ تنظیمات",
    "finance":        "💰 مالی",
}
