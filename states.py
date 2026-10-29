"""
states.py
تمام Stateهای FSM ربات اینجا تعریف می‌شوند تا در همه‌ی handlerها
قابل import و استفاده باشند.
"""

from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    waiting_custom_charge = State()
    waiting_charge_receipt = State()
    waiting_tonpays_refund = State()           # TonPays: دریافت کارت/نام برای عودت مازاد
    waiting_tonpays_remainder = State()        # TonPays: دریافت رسید مابقی (مبلغ کمتر)
    waiting_ticket_message = State()
    waiting_service_search = State()
    waiting_agency_request_message = State()  # درخواست نمایندگی (دکمه‌ی منوی پایین)
    waiting_agent_prefix = State()
    waiting_config_name = State()  # انتخاب نام سرویس توسط کاربر (بعد از انتخاب پلن)
    waiting_broadcast = State()
    waiting_config = State()
    waiting_discount_code = State()
    waiting_wd_amount = State()   # شارژ کیف پول با کد تخفیف: مبلغ
    waiting_wd_code = State()     # شارژ کیف پول با کد تخفیف: کد
    waiting_discount_plan = State()          # کد تخفیف وارد شده هنگام خرید یک پلن خاص
    waiting_card_purchase_receipt = State()  # رسید پرداخت کارت‌به‌کارت برای خرید سرویس
    waiting_crypto_charge_receipt = State()  # رسید/هش شارژ کیف پول با ارز دیجیتال
    waiting_crypto_purchase_receipt = State()  # رسید/هش پرداخت ارزی برای خرید سرویس
    # تمدید سرویس بر اساس تنظیمات دسته/پلن
    waiting_renew_custom_volume = State()
    waiting_renew_days = State()

    # «بساز سرویس خودت» و «تمدید سرویس» (هر دو از یک مسیر مشترک رد می‌شوند)
    waiting_custom_volume = State()
    waiting_custom_days = State()
    waiting_custom_name = State()
    waiting_custom_card_receipt = State()
    waiting_payg_name = State()
    waiting_discount_cbuild = State()         # کد تخفیف واردشده در مرحله‌ی پرداخت «بساز سرویس خودت»/تمدید


class AdminStates(StatesGroup):
    # 📝 ویرایشگر جامع متن و دکمه‌های رابط کاربری
    waiting_ui_editor_input = State()
    waiting_ui_row_layout = State()

    # سازگاری با نام Stateهای قدیمی که هنوز در admin.py استفاده می‌شوند.
    # این Aliasها عمداً به Stateهای مستقل قبلی اشاره می‌کنند تا callback/FSMهای
    # موجود بدون تغییر در رفتار بخش‌های دیگر از کار نیفتند.
    waiting_text_override_value = State()
    waiting_vip_plan_userlimit = State()
    waiting_vip_plan_edit_userlimit = State()
    waiting_custom_amount = State()
    waiting_search_user = State()
    waiting_config_text = State()
    waiting_discount_code_step = State()
    waiting_discount_percent_step = State()
    waiting_discount_uses_step = State()
    waiting_discount_value_step = State()   # مقدار تخفیف (درصد یا مبلغ ثابت)
    waiting_discount_plans_step = State()   # پلن‌های قابل‌اعمال (all یا لیست plan_key با کاما)
    waiting_discount_users_step = State()   # آیدی‌های عددی مجاز به استفاده (خالی/۰ یعنی همه)

    # ✏️ ویرایش یک کد تخفیف موجود (از صفحه‌ی جزئیات کد)
    waiting_discount_edit_value = State()   # ویرایش درصد/مبلغ
    waiting_discount_edit_uses = State()    # ویرایش تعداد استفاده‌ی باقی‌مانده
    waiting_discount_edit_users = State()   # ویرایش آیدی‌های مجاز
    waiting_discount_edit_min_order = State()   # ویرایش حداقل مبلغ سفارش
    waiting_discount_edit_max_per_user = State()  # ویرایش سقف استفاده‌ی هر کاربر
    waiting_discount_edit_expiry = State()  # ویرایش تاریخ انقضا
    waiting_for_log_channel = State()   # دریافت آیدی کانال لاگ سفارشات
    # 💳 مدیریت پرداخت
    waiting_crypto_symbol = State()     # نماد ارز جدید (مثل SOL)
    waiting_crypto_name = State()       # نام ارز (مثل Solana)
    waiting_crypto_network = State()    # شبکه (مثل Solana)
    waiting_crypto_wallet = State()     # آدرس کیف‌پول
    waiting_card_number = State()       # شماره کارت
    waiting_card_owner = State()        # نام صاحب کارت

    # نمایندگی (تخفیف خودکار روی VIP برای یک آیدی عددی خاص)
    waiting_agent_id_step = State()
    waiting_agent_percent_step = State()
    waiting_agent_edit_percent = State()  # تغییر درصد تخفیف یک نماینده‌ی موجود (از داخل صفحه‌ی نماینده)
    waiting_agent_categories = State()
    waiting_agent_prefix = State()
    waiting_agent_plan_percent = State()
    waiting_agent_test_quota = State()
    waiting_agent_option_percent = State()
    waiting_agdm_percent = State()
    waiting_map_option_name = State()      # نام گزینه‌ی جدید نگاشت (PAYG/سرویس سفارشی)
    waiting_map_option_rename = State()

    # ویرایش نام/قیمت پلن‌های VIP از پنل ادمین
    waiting_plan_edit_name = State()
    waiting_plan_edit_price = State()

    # 🗂 دسته‌بندی‌های VIP (بخش ۶) — افزودن دسته‌ی جدید و افزودن/ویرایش پلن داخل هر دسته
    waiting_vip_category_name = State()
    waiting_vip_category_description = State()   # 📝 ویرایش توضیح بالای دکمه‌های پلن‌های این دسته
    waiting_vip_plan_name = State()   # مرحله‌ی ۱ از ۴ افزودن پلن جدید
    waiting_vip_plan_price = State()  # مرحله‌ی ۲ از ۴
    waiting_vip_plan_gb = State()     # مرحله‌ی ۳ از ۴
    waiting_vip_plan_days = State()   # مرحله‌ی ۴ از ۴
    waiting_vip_plan_edit_name = State()
    waiting_vip_plan_edit_price = State()
    waiting_vip_plan_edit_gb = State()
    waiting_vip_plan_edit_days = State()
    waiting_vip_plan_edit_user_limit = State()


    # ارسال کانفیگ VIP با کیوآرکد + لینک ساب (mirroring خودکار)
    waiting_send_qr_photo = State()
    waiting_send_qr_link = State()
    waiting_send_qr_manual = State()  # فقط اگر تشخیص خودکار از روی لینک شکست بخورد


    # مدیریت سرویس‌های کاربران توسط ادمین
    waiting_edit_sublink = State()
    waiting_edit_qr = State()

    # 🔗 اتصال پنل‌های شاهراه/مرزبان/پاسارگارد — فقط برای وقتی که تشخیص خودکار
    # لینک ساب از پاسخ create/renew ممکن نشود و لازم باشد ادمین یک‌بار دستی
    # لینک را وارد کند. (قبلاً فقط مخصوص شاهراه بود؛ حالا برای هر سه نوع پنل.)
    waiting_panel_manual_link = State()

    # 🆕 چندپنلی: افزودن/ویرایش یک نمونه‌ی پنل (شاهراه/مرزبان/پاسارگارد)
    waiting_panel_name = State()          # مرحله‌ی ۱: برچسب دلخواه این نمونه
    waiting_panel_base_url = State()      # مرحله‌ی ۲: آدرس پایه
    waiting_panel_api_key = State()       # فقط شاهراه: کلید API
    waiting_panel_username = State()      # فقط مرزبان/پاسارگارد: نام کاربری ادمین
    waiting_panel_password = State()      # فقط مرزبان/پاسارگارد: رمز عبور ادمین
    waiting_panel_edit_field = State()    # ویرایش یک فیلد خاص از یک نمونه‌ی موجود

    # ✉️ پیام خصوصی ادمین به یک کاربر خاص (از بخش مدیریت کاربر/جستجوی)
    waiting_pm_message = State()

    # ↩️ پاسخ ادمین به یک تیکت پشتیبانی (state جدا از UserStates.waiting_ticket_message
    # تا اگر ادمین خودش هم یک تیکت عادی بزند، با این حالت قاطی نشود)
    waiting_ticket_reply = State()

    # 📚 مدیریت راهنما — افزودن/ویرایش محتوای هر قطعه راهنما (متن/عکس/فیلم)
    waiting_guide_title = State()
    waiting_guide_content = State()
    waiting_guide_edit_title = State()
    waiting_guide_edit_content = State()

    # 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو (تست رایگان/خرید اشتراک/
    # انتخاب پلن/بساز کانفیگ)
    waiting_sticker_upload = State()

    # ℹ️ اطلاعات ربات (ویرایش متن خوش‌آمدگویی، شماره کارت، لینک پشتیبانی و ...)
    waiting_botinfo_value = State()
    waiting_cfg_suffix_len = State()
    waiting_renewal_setting_value = State()
    waiting_botinfo_channel_add = State()
    waiting_botinfo_channel_name = State()
    waiting_botinfo_channel_url = State()
    waiting_start_reaction = State()
    waiting_referral_setting_value = State()
    waiting_wallet_setting_value = State()
    waiting_admin_service_volume = State()
    waiting_admin_service_days = State()
    waiting_admin_service_name = State()
    waiting_panel_renew_volume = State()
    waiting_panel_renew_days = State()
    waiting_campaign_code = State()
    waiting_campaign_name = State()
    waiting_campaign_rename = State()

    # 🎁 تنظیم حجم/مدت (ساعت یا روز)/قیمت پلن تست رایگان
    waiting_free_test_settings = State()

    # 🧩 تنظیم قیمت/محدوده‌ی «بساز سرویس خودت»
    waiting_custom_build_settings = State()
    waiting_stats_custom_date = State()
    waiting_ui_text = State()
    waiting_ui_button_text = State()
    # ➕ افزودن دکمه‌ی سفارشی به یک صفحه (مقصد، سپس متن روی دکمه)
    waiting_ui_cbtn_value = State()
    waiting_ui_cbtn_text = State()
    # 🎁 هدیه همگانی (بند ۴) و ✏️ ویرایش دستی کیف‌پول کاربر (بند ۶)
    waiting_gift_all_amount = State()
    waiting_wallet_edit_amount = State()
    # (duplicate removed by bug fix)

    # 🆕 مدیریت ادمین‌های فرعی / شاهراه دستی
    waiting_sub_admin_id = State()

    # ➕ ساخت دکمه‌ی سفارشی در ویرایشگر متن/دکمه‌ها
    waiting_custom_button_text = State()   # متن نمایشی دکمه
    waiting_custom_button_value = State()  # مقدار عملکرد (callback دستی/URL/اپ‌لینک)
    waiting_shahrah_manual_link = State()

    # 🔎 جستجوی کانفیگ (بند ۱۹)
    waiting_search_config = State()

    # 🤝 انتخاب پلن‌های مجاز برای هر نماینده (بند ۴)
    waiting_agent_plans = State()

    # 💳 تنظیمات Pay-as-you-go (بند ۷)
    waiting_payg_price_per_gb = State()
    waiting_payg_min_balance = State()
    waiting_map_option_cfg = State()
    waiting_payg_empty_message = State()

    # 🎁 دسته‌بندی مخاطب تخفیف در ساخت کد تخفیف (بند ۳)
    waiting_discount_specific_users = State()

    # 📊 آمار سفارشی
    waiting_stats_custom_date = State()

    # 🧩 تست رایگان چندپنلی (بند ۱۴)
    waiting_free_test_panel_volume = State()
    waiting_free_test_panel_days = State()
    waiting_free_test_panel_name = State()
    waiting_free_test_panel_price = State()
    waiting_free_test_default_volume = State()
    waiting_free_test_default_days = State()
    waiting_free_test_default_price = State()

    # 📝 Rich Text Template Manager (بند ۱۳)
    waiting_rt_label   = State()   # مرحله ۱: برچسب قالب
    waiting_rt_content = State()   # مرحله ۲: متن کامل قالب

    # 🖥 Mini App URL
    waiting_miniapp_url = State()
    waiting_uniquepay_business_token = State()
    waiting_uniquepay_redirect_url = State()
    waiting_uniquepay_callback_url = State()
    waiting_uniquepay_min_amount = State()
    waiting_gateway_field = State()   # ویرایش یک فیلد تنظیمات FrenzyEx/Tronado (gw/field در state data)
    waiting_crypto_wallet_edit = State()
    waiting_crypto_backup_price = State()
