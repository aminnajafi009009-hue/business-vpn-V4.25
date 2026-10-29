"""Central permission registry for sub-admins.

Single source of truth for labels, callback routes and UI menu permissions.
Adding a new admin callback should only require adding its route here.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class PermissionSpec:
    key: str
    label: str
    callbacks: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()

REGISTRY = (
    PermissionSpec("stats", "📊 آمار", ("admin_stats", "stat_")),
    PermissionSpec("requests", "📥 صف درخواست‌ها", ("admin_request_queue", "admin_order_queue", "dismissorder_", "dismisscustomorder_", "qordview_", "qcustview_", "clearorders", "clearorders_confirm", "clearorders_do", "marzbansend|")),
    PermissionSpec("receipts", "🧾 تایید رسیدها", ("admin_pending_receipts", "approve_", "reject_", "approvepay|", "rejectpay|", "approvecustom_", "rejectcustom_", "rcptview_", "rcptviewc_", "custom_", "clearreceipts", "clearreceipts_confirm", "clearreceipts_do")),
    PermissionSpec("users", "👥 کاربران و سرویس‌ها", ("admin_userlist", "userpage_", "useropen_", "useropenmsg_", "accounting_", "admin_search", "useractions_", "pm_", "toggleblock_", "walletedit_", "svcs_", "svcdetail_", "svcdelete_", "svcrestore_", "svcpurge", "svcpurge_", "svcpurgeconfirm_", "svcedit_", "svcedit_link_", "svcedit_qr_", "admin_userlist_active", "admin_userlist_all", "admin_config_search", "searchmode_", "searchopen_", "srchpg_", "deleteuser_", "deleteuserconfirm_")),
    PermissionSpec("broadcast", "📢 پیام همگانی", ("admin_broadcast", "broadcast_status", "broadcast_stop", "bcaud_", "fwd_rich_")),
    PermissionSpec("discounts", "🎟 مدیریت تخفیف", (
        "admin_discount", "discdetail_", "discdelete", "discdelete_", "discdeleteconfirm_",
        "discedit_", "discedit_value_", "discedit_uses_", "discedit_minorder_", "discedit_maxuser_",
        "discedit_expiry_", "discedit_users_", "discedit_plans_", "discplan", "discplan_", "dsc|", "ndaud|",
        "discplaned_", "new_discount", "disctype_", "admin_gift_all", "giftall_do_", "discaud_",
        "giftaud_", "giftdo_",
    )),
    # 📣 لینک‌های تبلیغاتی (کمپین) — مجوز مستقل
    PermissionSpec("campaigns", "📣 کمپین تبلیغاتی", (
        "admin_campaigns", "campdetail_", "camptoggle_", "campedit_", "campdelete_",
        "campdeleteconfirm_", "new_campaign",
    )),
    PermissionSpec("agency", "🤝 نمایندگی", ("admin_agency", "new_agent", "deleteagent_", "agentopen_", "editagentpercent_", "agentppct_", "agent_plans_", "agentplan", "editagentcats_", "agentcat_", "agentcatall_", "editagentprefix_", "agopt", "agtq_", "agdm")),
    PermissionSpec("plans", "🗂 مدیریت پلن‌های VIP", ("admin_vip_categories", "newvip", "newvipcat", "vipcat", "admincat_", "vipcatdesc_", "delvip", "delvipcat_", "vipplan", "vipplan_", "newvipplan_", "delvipplan_", "movevip", "movevipcat_", "movevipplan_", "vmap", "admin_buy_subscription", "vip")),
    PermissionSpec("vpn_panel", "🛡️ پنل‌های VPN و نگاشت", (
        "admin_vpn_panels", "vpntype|", "vpndetail|", "vpntest|", "vpnedit", "vpntoggle|", "vpndelete", "vpnmap", "vpnadd",
        "panelchoose|", "panelsend|", "panelcatalog|", "panelmap|", "sendvip_", "sendcustomorder_",
        "panelcustom_", "panelcustompanel|", "panelcustompick|", "panelrevoke_", "panelrenew_", "panelrenewpick|", "paneldisable_", "panelenable_",
        # 🐛 فیکس امنیتی (این بار جا نمونه‌ست از دور قبل که یه‌بار رو یه کپی
        # جدا زده بودمش و merge نشده بود): این پیشوندها مربوط به روش اتصال
        # پنل (یوزر/پسورد یا API)، نگاشت «بدون تمپلیت»، و تأیید نهایی انتخاب
        # تمپلیت هستند که اصلاً تو رجیستری نبودن — یعنی هر ادمین فرعی حتی
        # بدون دسترسی «مدیریت پنل‌های VPN» می‌تونست این‌ها رو تغییر بده.
        "vpnauthadd|", "vpnauthswitch|", "vpndirecttoggle|", "vpndirectconfirm|", "vpncatalogpick|",
        # 🆕 ایجاد سرویس دستی توسط ادمین (برای خودش)
        "admin_create_service", "adminsvcpanel_", "adminsvccatalog_", "autosend_", "adminsvcmode|", "adminsvcgrp", "adminsvcback|", "panelrenewcancel_", "panelrenewkind_", "vpndirectall|", "vpndirectprotocol|", "admin_marzban", "admin_pasargad", "admin_panel_choose", "panelchoose", "marz", "pasargad", "svcrevokesub_", "fup_",
    )),
    PermissionSpec("referrals", "🤝 مدیریت دعوت‌ها", ("admin_referrals", "refpage_", "refdetail_")),
    PermissionSpec("guides", "📚 مدیریت راهنما", ("admin_guides", "guide", "guidenew", "guideadminopen_", "guidemove_", "guideeditname_", "guideeditcontent_", "guidedelete_", "guidedeleteconfirm_")),
    PermissionSpec("stickers", "🎬 استیکرهای منو", ("admin_stickers", "sticker", "stickeropen_", "stickerset_", "stickeroff_", "stickeron_", "stickerreset_")),
    # 🐛 فیکس امنیتی: کال‌بک‌های تنظیمات رفرال (refset_) و شارژ کیف‌پول (wset_)
    # قبلاً اصلاً در این رجیستری ثبت نشده بودند. چون permission_for_callback
    # برای کال‌بک‌های ثبت‌نشده None برمی‌گرداند و AdminPermissionMiddleware در
    # آن حالت هیچ محدودیتی اعمال نمی‌کند، هر ادمین فرعی — even بدون داشتن
    # دسترسی «ℹ️ اطلاعات ربات» — می‌توانست مبلغ پاداش رفرال، حداقل/حداکثر
    # شارژ کیف‌پول و... را تغییر دهد. حالا این دو زیربخش هم زیر همان مجوز
    # «botinfo» قرار گرفته‌اند (دقیقاً هم‌جا با دکمه‌های ورودشان در منوی
    # «اطلاعات ربات»).
    PermissionSpec("botinfo", "ℹ️ اطلاعات ربات", (
        "admin_botinfo", "botinfo", "botinfo_open", "botinfo_channels", "botinfo_channel_add",
        "botinfo_edit_", "botinfo_reaction", "botinfo_react_", "botinfo_channel_del_", "channel",
        "botinfo_referral", "botinfo_wallet", "ntf_", "adv_",
    )),
    PermissionSpec("backup", "💾 بکاپ", ("admin_backup",)),
    PermissionSpec("orders_toggle", "🔴/🟢 روشن و خاموش کردن سفارشات", ("admin_orders_off", "admin_orders_on")),
    PermissionSpec("settings", "⚙️ تست و بساز سرویس خودت", ("admin_free_test_settings", "admin_free_test", "ftpanel", "ftmap", "free_test", "admin_custom_build_settings", "cbset_", "mopt_", "admin_custom_build_toggle", "ft_shortcut_toggle", "ftunit_", "moptcfg|", "admin_renewal_settings", "renewset", "renewsetscope", "renewsetcat", "renewsw", "rt_", "renewcustlist")),
    PermissionSpec("logs", "🦖 لاگ خطاها و Audit", ("errlog", "errlogrefresh", "errlogclear", "errlogclearconfirm", "errlogsentryguide", "errlogdetail_", "admin_audit_logs", "auditpage_")),
    PermissionSpec("health", "🩺 سلامت ربات", ("admin_health", "admin_cache_status", "cache_clear", "cache_clear_do")),
    PermissionSpec("texts", "📝 ویرایش متن و دکمه‌ها", ("admin_text_editor", "ui_cat:", "ui_screen:", "ui_edit_text:", "ui_button:", "ui_layout:", "ui_move:", "textedit_", "textbtn_", "textlayout_", "ui_mode:", "ui_toggle:", "ui_style:", "ui_auto", "ui_edit_auto:", "ui_buttons_page:", "ui_button_page:", "ui_screen_textpage:", "ui_cbtn_", "admin_texts", "textdelete_", "textcat_", "uitc_", "ui_")),
    PermissionSpec("payment_methods", "💳 روش‌های پرداخت", (
        "admin_payment_methods", "paymt_", "crypto_backup_price_", "crypto_backup_toggle_",
    )),
    PermissionSpec("referral_settings", "🎁 تنظیمات رفرال", ("admin_referral_settings", "refset_")),
    PermissionSpec("wallet_settings", "👛 تنظیمات کیف پول", ("admin_wallet_settings", "wset_")),
    PermissionSpec("log_settings", "📜 مدیریت لاگ‌ها (کانال/گروه گزارش)", ("admin_log_settings", "admin_order_log_channel")),
    PermissionSpec("payg", "⚡ تنظیمات PayG (پرداخت به‌ازای مصرف)", ("admin_payg", "payg_catprice_", "payg_planprice_", "payg_empty_", "payg_min_balance", "payg_price_", "payg_toggle")),
    PermissionSpec("miniapp", "🖥 Mini App ادمین", ("admin_miniapp_open", "admin_miniapp_seturl")),
    PermissionSpec("tickets", "🎫 مدیریت تیکت‌ها", (
        "admin_tickets", "admtickets_", "admticketview_", "admticketreply_", "admticketclose_", "admticketreopen_",
        "replyticket_",
    )),
)

PERMISSION_LABELS = {x.key: x.label for x in REGISTRY}

def permission_for_callback(data: str | None) -> str | None:
    d = data or ""
    if d in {"admin_back", "noop"}:
        return None
    for spec in REGISTRY:
        for prefix in spec.callbacks:
            if d == prefix or d.startswith(prefix):
                return spec.key
    return None

def permission_for_message(text: str | None) -> str | None:
    text = text or ""
    aliases = {
        "📊 آمار":"stats", "📥 صف درخواست‌ها":"requests", "👥 لیست کاربران":"users", "🔍 جستجوی حرفه‌ای":"users",
        "📢 پیام همگانی":"broadcast", "🎟 مدیریت تخفیف":"discounts", "🤝 نمایندگی (تخفیف VIP)":"agency",
        "🗂 دسته‌بندی‌های VIP":"plans", "🖥 مدیریت پنل‌های VPN":"vpn_panel", "🤝 مدیریت دعوت‌ها":"referrals",
        "📚 مدیریت راهنما":"guides", "🦖 لاگ خطاها (Sentry)":"logs", "🦖 لاگ خطاها و Audit":"logs", "🦖 لاگ خطاها":"logs", "ℹ️ اطلاعات ربات":"botinfo",
        "🎬 استیکرهای منو":"stickers", "💾 بکاپ":"backup", "🎁 تنظیم تست رایگان":"settings", "🧩 تنظیم بساز سرویس خودت":"settings",
        "📝 ویرایش متن و دکمه‌ها":"texts", "🔴 خاموش کردن سفارشات":"orders_toggle", "🟢 روشن کردن سفارشات":"orders_toggle",
        "🩺 سلامت ربات":"health", "🚀 وضعیت کش":"health", "👮 مدیریت ادمین‌ها":"manage_admins", "🧾 رسیدهای در انتظار":"receipts",
        "🎫 مدیریت تیکت‌ها":"tickets", "🔁 تنظیمات تمدید":"settings", "🔎 جستجوی کانفیگ":"users", "🔍 جستجوی کاربر":"users",
        "📣 لینک‌های تبلیغاتی":"campaigns", "📣 لینک تبلیغاتی":"campaigns", "📣 کمپین تبلیغاتی":"campaigns", "📜 مدیریت لاگ‌ها":"log_settings", "💰 تنظیمات کیف پول":"wallet_settings", "💳 تنظیمات کیف‌پول":"wallet_settings",
        "🎁 تنظیمات رفرال":"referral_settings", "💳 روش‌های پرداخت":"payment_methods",
        "🖥 پنل مدیریت (Mini App)":"miniapp", "📱 پنل مدیریت (Mini App)":"miniapp", "mini app":"miniapp", "Mini App":"miniapp", "مینی اپ":"miniapp", "🖥 Mini App ادمین":"miniapp", "🛠 ایجاد سرویس (ادمین)":"vpn_panel", "📋 گزارش فعالیت ادمین":"logs",
    }
    return aliases.get(text)
