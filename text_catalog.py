"""کاتالوگ متن‌های ربات — سیستم شخصی‌سازی کامل

همه متن‌های قابل ویرایش اینجا تعریف شده‌اند.
ادمین می‌تواند از پنل «✏️ مدیریت متن‌ها» همه را ویرایش کند.
RichText: Premium Emoji و MessageEntity را نگه می‌دارد.
"""

from collections import OrderedDict
import database as db

TEXT_CATEGORIES: OrderedDict = OrderedDict()


def _cat(name: str, items: list):
    """اضافه کردن دسته‌بندی جدید"""
    TEXT_CATEGORIES.setdefault(name, []).extend(items)


# ===========================================================================
# 🚀 شروع و منوی اصلی
# ===========================================================================
_cat('🚀 شروع و منوی اصلی', [
    ('start_welcome', '👋 خوش آمدید! {first_name} عزیز\n\nاز منوی پایین صفحه انتخاب کنید 👇'),
    ('start_blocked', '🚫 دسترسی شما به ربات مسدود شده است. در صورت وجود ابهام با پشتیبانی تماس بگیرید.'),
    ('start_blocked_short', '🚫 دسترسی شما به ربات مسدود شده است.'),
    ('start_join_required', '⚠️ برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید:'),
    ('start_join_not_done', '❌ هنوز در همه کانال‌ها عضو نشدید!'),
    ('start_join_confirmed', 'منوی اصلی در پایین صفحه فعال شد ✅'),
    ('start_back_admin', '👨\u200d💻 بازگشت به منوی اصلی — از منوی پایین صفحه ادامه دهید ✅'),
    ('start_back_user', '👋 بازگشت به منوی اصلی — از منوی پایین صفحه ادامه دهید ✅'),
    ('start_admin_welcome', '👨\u200d💻 به پنل مدیریت خوش آمدید!\n\nهمه\u200cی امکانات مدیریتی از منوی پایین صفحه قابل دسترسی است ✅'),
    ('join_confirm', '✅ عضو شدم'),
    # دکمه‌های منوی اصلی reply keyboard
    ('main_btn_plans', '🛒 خرید اشتراک'),
    ('main_btn_free_test', '🎁 تست رایگان'),
    ('main_btn_services', '📱 سرویس\u200cهای من'),
    ('main_btn_wallet', '💰 کیف پول'),
    ('main_btn_referral', '👥 دعوت دوستان'),
    ('main_btn_profile', '👤 پروفایل من'),
    ('main_btn_support', '👨\u200d💻 پشتیبانی'),
    ('main_btn_guides', '📚 راهنما'),
    ('main_btn_agency', '🤝 درخواست نمایندگی'),
])

# ===========================================================================
# 🔘 دکمه‌های عمومی (مشترک در همه بخش‌ها)
# ===========================================================================
_cat('🔘 دکمه‌های عمومی', [
    ('btn_back', '🔙 بازگشت'),
    ('btn_back_main', '🏠 بازگشت به منوی اصلی'),
    ('btn_cancel', '❌ انصراف'),
    ('btn_confirm', '✅ تأیید'),
    ('btn_yes', '✅ بله'),
    ('btn_no', '❌ خیر'),
    ('btn_close', '❌ بستن'),
    ('common_start_required', 'ابتدا دستور /start را بزنید.'),
    ('processing_request', '⚠️ این درخواست در حال پردازش/ثبت\u200cشده است.'),
    ('invalid_request', '❌ درخواست نامعتبر است.'),
    ('only_number', '❌ فقط عدد ارسال کنید.'),
    ('orders_closed', '🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می\u200cباشد.'),
])

# ===========================================================================
# 💳 روش‌های پرداخت (دکمه‌ها یک‌جا)
# ===========================================================================
_cat('💳 دکمه‌های روش پرداخت', [
    ('pay_wallet', '👛 پرداخت از کیف پول'),
    ('pay_online', '🌐 پرداخت آنلاین (تایید خودکار)'),
    ('pay_card', '💳 پرداخت کارت به کارت'),
    ('pay_crypto', '💱 پرداخت ارزی'),
    ('pay_discount', '🎟 ثبت کد تخفیف'),
    ('pay_back', '🔙 بازگشت'),
    ('pay_change_method', '🔄 انتخاب روش پرداخت دیگر'),
    ('online_pay_btn', '💳 پرداخت (کارت به کارت خودکار)'),
    ('online_check_btn', '✅ پرداخت را انجام دادم / بررسی کن'),
    ('online_cancel_btn', '🔙 انصراف'),
    ('payment_not_active', 'این روش پرداخت در حال حاضر فعال نیست.'),
    ('building_payment', '⏳ در حال ساخت لینک پرداخت...'),
    ('checking_payment', '⏳ در حال بررسی وضعیت پرداخت...'),
    ('payment_owned', '⛔️ این پرداخت متعلق به شما نیست.'),
    ('payment_already_confirmed', '✅ این پرداخت قبلاً تأیید شده است.'),
    ('payment_problem', '❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس\u200cها شروع کنید.'),
    ('online_payment_not_paid', '⏳ هنوز پرداختی برای این فاکتور ثبت نشده. چند لحظه صبر کنید و دوباره بزنید.'),
    ('online_payment_success_order', '✅ پرداخت آنلاین شما تأیید شد و سفارش شما ثبت گردید. سرویس شما به\u200cزودی ارسال می\u200cشود.'),
    ('online_payment_success_wallet', '✅ پرداخت آنلاین شما تأیید شد و کیف پول شما شارژ شد.'),
    ('online_payment_button', '💳 پرداخت آنلاین'),
    ('online_payment_check', '✅ بررسی پرداخت'),
    ('online_payment_cancel', '🔙 انصراف'),
    ('online_payment_create_failed', '❌ ساخت لینک پرداخت آنلاین ناموفق بود. لطفاً روش پرداخت دیگری را انتخاب کنید.'),
    ('invoice_copy_card', '💳 کپی شماره کارت'),
    ('invoice_copy_amount', '📋 کپی مبلغ'),
])

# ===========================================================================
# 🛒 خرید اشتراک
# ===========================================================================
_cat('🛒 خرید اشتراک', [
    ('plans_intro', '🛒 **خرید اشتراک**\n\nلطفاً سرویس مورد نظر خود را از منوی زیر انتخاب کنید 👇'),
    ('vip_intro', '⭐ سرویس‌های VIP\n\nیکی از دسته‌ها را انتخاب کنید 👇'),
    ('vip_category_title', '{category_name}:\n\nیکی از دسته‌ها را انتخاب کنید 👇'),
    ('vip_category_empty', '😔 فعلاً هیچ دسته\u200cای موجود نیست'),
    ('vip_plans_empty', '😔 فعلاً هیچ پلنی در این دسته نیست'),
    ('plan_payment_page', '🛒 {plan_name}\n💰 قیمت: {price:,} تومان{note}\n👛 موجودی کیف پول شما: {wallet:,} تومان\n\nروش پرداخت را انتخاب کنید:'),
    ('plan_not_found', '❌ این پلن یافت نشد.'),
    ('category_not_found', '❌ این دسته یافت نشد.'),
    ('wallet_purchase_success', '✅ پرداخت شما ثبت شد. سفارش شما در صف ارسال سرویس قرار گرفت.'),
    ('wallet_not_enough', '❌ موجودی کافی نیست.'),
    ('wallet_insufficient', '❌ موجودی کیف پول کافی نیست!\n\n💰 قیمت: {price:,} تومان\n👛 موجودی: {wallet:,} تومان\n⚠️ کمبود: {needed:,} تومان'),
    ('insufficient_charge', '💵 شارژ کیف پول'),
    ('plan_payment_service_name', '🔤 نام سرویس: {service_name}'),
    ('config_name_prompt', '🔤 یک نام برای سرویس انتخاب کنید.\n\nفقط حروف انگلیسی، عدد و _ مجاز است (۳ تا ۲۶ کاراکتر).\nپنج رقم تصادفی در انتهای نام اضافه می\u200cشود و همان نام روی پنل ساخته می\u200cشود.'),
    ('config_name_invalid', '❌ نام باید ۳ تا ۲۶ کاراکتر و فقط شامل حروف انگلیسی، عدد و _ باشد؛ پنج رقم تصادفی در انتها اضافه می\u200cشود.\n\nیک اسم دیگر بفرست یا «انتخاب خودکار نام» را بزن:'),
    ('config_name_duplicate', '❌ نام تکراری است. لطفاً یک اسم دیگر انتخاب کنید.'),
    ('config_name_panel_duplicate', '❌ این نام قبلاً در پنل وجود دارد. لطفاً یک اسم دیگر انتخاب کنید.'),
    ('config_name_back_message', '🔙 به انتخاب پلن برگشتید.'),
    ('config_name_auto', '🤖 انتخاب خودکار نام'),
    ('config_name_back', '🔙 بازگشت به مرحله قبل'),
    ('card_receipt_registered', '✅ رسید شما ثبت شد. پس از تأیید، سفارش شما در صف ارسال سرویس قرار می\u200cگیرد.'),
    ('receipt_registered', '✅ رسید ثبت شد. پس از تأیید ادمین، نتیجه به شما اطلاع داده می\u200cشود.'),
    ('receipt_photo_only', '📸 لطفاً عکس رسید پرداخت را ارسال کنید.'),
    ('invoice_expired_wait', '⏰ مهلت ۳۰ دقیقه\u200cای پرداخت این فاکتور به پایان رسیده. لطفاً دوباره از منوی سرویس‌ها سفارش بدید.'),
    ('free_test_page', '🎁 {plan_name}\n💰 قیمت: {price:,} تومان\n👛 موجودی کیف پول شما: {wallet:,} تومان\n\nروش پرداخت را انتخاب کنید:'),
    ('free_test_soon', '🎁 تست رایگان به\u200cزودی فعال می\u200cشود!'),
    ('free_test_used', '⚠️ شما قبلاً از «تست رایگان» استفاده کرده\u200cاید.'),
    ('free_test_registered', '✅ درخواست تست رایگان شما ثبت شد!\n\nسرویس شما به زودی ارسال می\u200cشود.'),
    ('free_test_processing', '⚠️ این درخواست در حال پردازش است.'),
    ('purchase_receipt_error', '❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس\u200cها شروع کنید.'),
])

# ===========================================================================
# 💳 فاکتور و پرداخت کارت
# ===========================================================================
_cat('💳 فاکتور و پرداخت کارت', [
    ('invoice_plan_card',
     '🟩🟩⬜️ مرحله 2 از 3\n\n💳 پرداخت کارت به کارت\n\n'
     '🛒 {plan_name}\n💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     '💳 شماره کارت:\n{card_number}\n\n👤 به نام: {card_holder}\n\n'
     '📸 پس از واریز، عکس رسید پرداخت یا 📝 متن رسید را همینجا ارسال کنید.'),
    ('invoice_wallet_card',
     '🟩⬜️ مرحله 2 از 2\n\n💳 شارژ کیف پول\n\n'
     '💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     '💳 شماره کارت:\n{card_number}\n\n👤 به نام: {card_holder}\n\n'
     '📸 پس از واریز، عکس رسید پرداخت یا 📝 متن رسید را همینجا ارسال کنید.'),
    ('invoice_card_expiry', '⏱ این شماره کارت تا ساعت {deadline} (۳۰ دقیقه) معتبر است.'),
    ('invoice_wallet_expiry', '⏱ این شماره کارت تا ساعت {deadline} (۳۰ دقیقه) معتبر است.'),
    ('invoice_custom_expiry', '⏱ این شماره کارت تا ساعت {deadline} (۳۰ دقیقه) معتبر است.'),
    ('online_plan_invoice',
     '🌐 پرداخت آنلاین (کارت\u200cبه\u200cکارت خودکار)\n\n'
     '🛒 {plan_name}\n💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     'روی دکمه\u200cی «پرداخت» بزنید، مبلغ را واریز کنید، سپس روی «بررسی کن» بزنید.\n'
     '⏱ به\u200cمحض تأیید بانک، سفارش شما به\u200cطور خودکار ثبت می\u200cشود.\n\n'
     '⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است.'),
    ('online_wallet_invoice',
     '🌐 پرداخت آنلاین (کارت\u200cبه\u200cکارت خودکار)\n\n'
     '💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     'روی دکمه\u200cی «پرداخت» بزنید، سپس روی «بررسی کن» بزنید.\n'
     '⏱ به\u200cمحض تأیید بانک، کیف پول شما به\u200cطور خودکار شارژ می\u200cشود.\n\n'
     '⚠️ این فاکتور تا ۳۰ دقیقه دیگر — تا ساعت {deadline} — معتبر است.'),
    ('online_plan_invoice_generic',
     '🌐 پرداخت آنلاین ({gateway})\n\n'
     '🛒 {plan_name}\n💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     'روی دکمه\u200cی «پرداخت» بزنید و پرداخت را تکمیل کنید. سپس روی «بررسی کن» بزنید.\n\n'
     '⚠️ این فاکتور تا ساعت {deadline} (۳۰ دقیقه) معتبر است.'),
    ('online_wallet_invoice_generic',
     '🌐 پرداخت آنلاین ({gateway})\n\n'
     '💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     'روی دکمه\u200cی «پرداخت» بزنید. سپس روی «بررسی کن» بزنید.\n\n'
     '⚠️ این فاکتور تا ساعت {deadline} (۳۰ دقیقه) معتبر است.'),
    ('online_min_amount', '❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست.'),
    ('charge_receipt_expired', '⏰ مهلت ۳۰ دقیقه\u200cای پرداخت این فاکتور به پایان رسیده. لطفاً دوباره از منوی شارژ شروع کنید.'),
    ('charge_receipt_registered', '✅ رسید ثبت شد. پس از تأیید ادمین، کیف پول شما شارژ می\u200cشود.'),
    ('charge_problem', '❌ مشکلی پیش آمد، لطفاً دوباره از منوی شارژ شروع کنید.'),
])

# ===========================================================================
# 💱 پرداخت ارزی
# ===========================================================================
_cat('💱 پرداخت ارزی', [
    ('crypto_not_configured', '❌ پرداخت ارزی هنوز توسط ادمین تنظیم نشده است.'),
    ('crypto_asset_unavailable', '❌ این ارز در حال حاضر در دسترس نیست.'),
    ('crypto_choose_asset', '🔙 انتخاب ارز'),
    ('crypto_asset_ton', '🟣 TON'),
    ('crypto_asset_trx', '🔴 TRX'),
    ('crypto_asset_usdt', '🟢 USDT (TRC20)'),
    ('crypto_receipt_hint', '📨 ارسال رسید / Hash'),
    ('crypto_receipt_hint_alert', '📸 عکس رسید یا 📝 متن/Hash تراکنش را همینجا ارسال کن.'),
    ('crypto_receipt_invalid', '❌ لطفاً عکس رسید یا متن/Hash تراکنش را ارسال کنید.'),
    ('crypto_payment_intro',
     '💱 پرداخت ارزی\n\n🛒 {plan_name}\n💰 قیمت سرویس: {price:,} تومان\n⏱ اعتبار این فاکتور: ۳۰ دقیقه — تا ساعت {deadline}\n\n'
     'تعرفه لحظه\u200cای پرداخت:\n{available_rows}{stale_note}\n\nارز موردنظر را انتخاب کن:'),
    ('crypto_payment_detail',
     '🟩🟩⬜️ مرحله 2 از 3\n\n💱 پرداخت ارزی\n\n'
     '🛒 {plan_name}\n💰 مبلغ قابل پرداخت: {price:,} تومان\n'
     '💵 مبلغ پرداختی: {amount} {asset}\n\n'
     '🌐 شبکه: {network}\n👛 ولت: برای دریافت آدرس، دکمه «📋 کپی ولت» را بزنید.\n\n'
     '📸 پس از واریز، عکس رسید یا 📝 متن/Hash تراکنش را ارسال کنید.\n'
     '⏱ این فاکتور تا ساعت {deadline} (۳۰ دقیقه) معتبر است.'),
    ('crypto_copy_wallet', '👛 کپی ولت'),
    ('crypto_copy_amount', '📋 کپی مبلغ'),
])

# ===========================================================================
# 💰 کیف پول
# ===========================================================================
_cat('💰 کیف پول', [
    ('wallet_overview',
     '💰 کیف پول شما\n\n'
     '👛 موجودی قابل استفاده: {wallet:,} تومان\n'
     '🔒 موجودی در انتظار: {locked:,} تومان'),
    ('wallet_free_overview', '💰 موجودی قابل استفاده شما\n\n{wallet:,} تومان\n\nاین مبلغ را می\u200cتوانید برای خرید سرویس استفاده کنید.'),
    ('wallet_locked_overview', '🔒 موجودی در انتظار شما\n\n{locked:,} تومان'),
    ('wallet_charge', '💳 شارژ کیف پول'),
    ('wallet_discount', '🎟 ثبت کد تخفیف'),
    ('wallet_transactions', '📋 تراکنش\u200cهای من'),
    ('charge_choose_amount', '💳 مبلغ شارژ را انتخاب کنید:'),
    ('charge_custom', '💵 مبلغ دلخواه'),
    ('charge_custom_prompt', '💵 مبلغ دلخواه را به تومان ارسال کنید:'),
    ('wallet_charge_range', '❌ مبلغ شارژ باید بین {min_amount:,} تا {max_amount:,} تومان باشد.'),
    ('wallet_pay_online', '🌐 پرداخت آنلاین (تایید خودکار)'),
    ('wallet_pay_card', '💳 پرداخت کارت به کارت'),
    ('wallet_check_pay', '✅ پرداخت را انجام دادم / بررسی کن'),
    ('wallet_cancel', '🔙 انصراف'),
    ('transactions_empty', '📋 هنوز تراکنشی ندارید.'),
    ('transactions_title', '📋 تراکنش\u200cهای اخیر:\n\n'),
    # دکمه‌های مبالغ پیش‌فرض (از تنظیمات ادمین تأمین می‌شوند)
    ('charge_amount_50000', '💰 ۵۰,۰۰۰ تومان'),
    ('charge_amount_100000', '💰 ۱۰۰,۰۰۰ تومان'),
    ('charge_amount_200000', '💰 ۲۰۰,۰۰۰ تومان'),
])

# ===========================================================================
# 🎟 کد تخفیف
# ===========================================================================
_cat('🎟 کد تخفیف', [
    ('discount_enter', '🎟 کد تخفیف خود را وارد کنید:'),
    ('discount_cancel', '🔙 انصراف'),
    ('discount_invalid', '❌ کد تخفیف نامعتبر یا تمام شده.'),
    ('discount_forbidden', '❌ شما مجاز به استفاده از این کد تخفیف نیستید.'),
    ('discount_limit', '❌ سهمیه\u200cی استفاده\u200cی شما از این کد تمام شده.'),
    ('discount_fixed_note',
     '💡 این کد یک کد تخفیف با مبلغ ثابت است؛ لطفاً از منوی «🛒 خرید اشتراک» پلن مورد نظرتان را انتخاب '
     'کنید و در صفحه\u200cی پرداخت همان پلن، کد را وارد کنید.'),
    ('discount_success', '✅ کد تخفیف {percent}٪ با موفقیت ثبت شد و در خرید بعدی شما اعمال می\u200cشود.{plans_note}'),
    ('invalid_discount', '❌ کد تخفیف نامعتبر است.'),
])

# ===========================================================================
# 📦 سرویس‌های من
# ===========================================================================
_cat('📦 سرویس\u200cهای من', [
    # قالب‌های اصلی پیام «سرویس‌های من» (همان متن‌هایی که واقعاً نمایش داده می‌شوند) — اول لیست
    ('my_configs_list_has', 'سرویس‌های شما\n\nبرای مشاهده‌ی لینک سابسکریپشن و مدیریت هرکدام، روی نام آن بزنید 👇'),
    ('my_configs_list_empty', 'شما هنوز هیچ سرویس فعالی ندارید.'),
    ('config_detail',
     '📊 وضعیت سرویس : {status}\n'
     '👤 نام سرویس : {service_name}\n\n'
     '🌍 موقعیت سرویس : {location} 🌿\n'
     '📂 نام محصول : {product}\n\n'
     '🔋 ترافیک : {traffic}\n'
     '☑️ حجم مصرفی : {used}\n'
     '💢 حجم باقی مانده : {remaining}\n'
     '{usage_bar}\n\n'
     '🗓 تاریخ اتمام : {expiry}'),
    ('configs_empty', '📱 شما هنوز هیچ سرویسی خریداری نکرده\u200cاید.\n\nبرای خرید، از «🛒 خرید اشتراک» اقدام کنید.'),
    ('configs_has', '📱 سرویس\u200cهای شما\n\nکدوم دسته رو می\u200cخوای ببینی؟ 👇'),
    ('vip_configs_empty', 'شما هنوز هیچ سرویس VIP\u200cای خریداری نکرده\u200cاید.'),
    ('vip_configs_has', 'سرویس\u200cهای VIP شما 👇'),
    ('service_detail_text',
     '📦 {plan}\n{live_status}\n\n📊 وضعیت مصرف (لحظه\u200cای):\n'
     '💿 حجم کل: {total}\n📲 مصرف\u200cشده: {used}\n📱 باقی\u200cمانده: {remaining}\n\n'
     '{bar} {percent}٪ مصرف شده\n\n'
     '⏰ تاریخ انقضا: {expiry}\n{expiry_status}\n\n'
     '🔗 لینک ساب (Subscription) شما:\n{link}\n\n📆 تاریخ خرید: {purchase_date}'),
    ('config_detail_error', '❌ خطا در نمایش جزئیات سرویس.'),
    ('config_expired', '⛔️ منقضی شده'),
    ('config_days_left', '⌛️ زمان باقی\u200cمانده: {days} روز'),
    ('config_subscription_help', '🔗 لینک ساب (Subscription) شما:'),
    ('service_live_active', '🟢 فعال'),
    ('service_live_expired', '🔴 منقضی'),
    ('service_not_found', '❌ سرویس یافت نشد.'),
    ('service_not_owned', '❌ این سرویس متعلق به شما نیست.'),
    ('service_loading', '⏳ در حال دریافت اطلاعات مصرف...'),
    ('subscription_fetching', '⏳ در حال دریافت کانفیگ\u200cها...'),
    ('subscription_unavailable', '❌ لینک ساب در حال حاضر در دسترس نیست.'),
    ('qr_missing', '❌ کیوآرکدی برای این سرویس ثبت نشده.'),
    ('qr_failed', '❌ ارسال کیوآرکد ناموفق بود.'),
    ('config_back_service', '🔙 بازگشت به سرویس'),
    # دکمه‌های سرویس
    ('config_qr', '🖼 مشاهده کیوآرکد'),
    ('config_sub', '🔗 لینک اشتراک'),
    ('config_refresh', '🔄 بروزرسانی اطلاعات'),
    ('config_mirror', '🔗 دریافت کانفیگ\u200cهای تکی'),
    ('config_enable', '▶️ فعال\u200cسازی سرویس'),
    ('config_disable', '⏸ غیرفعال\u200cسازی سرویس'),
    ('config_revoke', '🔄 ساخت لینک ساب جدید'),
    ('config_delete', '🗑 حذف سرویس'),
    ('confirm_delete_yes', '✅ بله، حذف کن'),
    ('confirm_delete_no', '❌ انصراف'),
    ('service_deleted', '✅ سرویس حذف شد.'),
    ('service_disable_confirm', '⚠️ مطمئنی می\u200cخوای سرویس رو غیرفعال کنی؟'),
    ('service_disable_failed', '❌ غیرفعال\u200cسازی ناموفق بود: {msg}'),
    ('service_disabled', '🚫 سرویس غیرفعال شد.'),
    ('service_enable_failed', '❌ فعال\u200cسازی ناموفق بود: {msg}'),
    ('service_enabled', '✅ سرویس دوباره فعال شد.'),
    ('service_revoke_confirm', '⚠️ مطمئنی می\u200cخوای لینک ساب عوض شه؟\n\nبعد از تعویض، لینک قبلی دیگه کار نمی\u200cکنه.'),
    ('service_revoke_failed', '❌ ساخت لینک ساب جدید ناموفق بود: {msg}'),
    ('service_revoke_done', '✅ لینک ساب جدید ساخته شد.'),
    ('service_search', '🔎 جستجوی سرویس'),
    ('service_search_prompt', '🔎 بخشی از اسم سرویس را وارد کنید:'),
    ('service_search_empty', '🔎 برای «{query}» هیچ سرویسی پیدا نشد.'),
    ('service_search_found', '🔎 {count} سرویس برای «{query}» پیدا شد:\n\n👇 برای مشاهده جزئیات انتخاب کنید.'),
    ('buy_service_button', '🛒 خرید سرویس'),
    ('config_back', '🔙 بازگشت به سرویس\u200cهای VIP من'),
])

# ===========================================================================
# 📤 تحویل سرویس
# ===========================================================================
_cat('📤 تحویل سرویس', [
    ('service_delivery_text',
     '✅ سرویس شما با موفقیت تحویل داده شد\n\n'
     '👤 نام کاربری: {service_label}\n\n'
     '🔗 لینک کانفیگ شما:\n{link}\n\n'
     '📋 لینک را کپی کنید و داخل برنامه\u200cتان جایگذاری کنید.\n\n'
     '💝 ممنون از اعتماد شما\n\n'
     'برای دریافت اپلیکیشن یا نحوه اتصال، از گزینه\u200cهای زیر استفاده کنید 👇'),
    ('service_delivery_test_text',
     '🎁 تست رایگان شما با موفقیت تحویل داده شد\n\n'
     '👤 نام کاربری: {service_label}\n\n'
     '🔗 لینک کانفیگ شما:\n{link}\n\n'
     '📋 لینک را کپی کنید و داخل برنامه\u200cتان جایگذاری کنید.\n\n'
     'برای دریافت اپلیکیشن یا نحوه اتصال، از گزینه\u200cهای زیر استفاده کنید 👇'),
    ('service_delivery_apps_button', '📱 دریافت اپلیکیشن'),
    ('service_delivery_connection_button', '🔧 نحوه اتصال کانفیگ'),
    ('notif_service_delivery', '📦 سرویس شما آماده شد ⬇️'),
])

# ===========================================================================
# 🔁 تمدید سرویس
# ===========================================================================
_cat('🔁 تمدید سرویس', [
    ('renew_menu_title', '🔁 تمدید سرویس'),
    ('renew_choose_service', '🔁 سرویس موردنظر برای تمدید را انتخاب کنید 👇'),
    ('renew_volume_prompt', '📦 مقدار حجمی که می\u200cخواهید اضافه شود را انتخاب کنید:'),
    ('renew_days_prompt', '⏳ مقدار زمان اضافه را انتخاب کنید:'),
    ('renew_volume_custom', '➕ مقدار دلخواه'),
    ('renew_custom_volume_prompt', '📦 مقدار حجم اضافه را به گیگ وارد کنید (بیشتر از ۵۰):'),
    ('renew_days_custom', '➕ زمان دلخواه'),
    ('renew_custom_days_prompt', '⏳ تعداد روز اضافه را وارد کنید:'),
    ('renew_summary',
     '🧾 تمدید سرویس «{service_name}»\n\n'
     '📦 حجم اضافه: {volume}\n⏳ زمان اضافه: {days}\n'
     '💰 مبلغ: {price:,} تومان\n\nروش پرداخت را انتخاب کنید:'),
    ('renew_card_invoice',
     '🟩🟩⬜️ مرحله 2 از 3\n\n💳 پرداخت کارت به کارت\n\n'
     '🔁 تمدید سرویس: {service_name}\n📦 حجم اضافه: {volume}\n⏳ زمان اضافه: {days}\n'
     '💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n'
     '💳 شماره کارت:\n{card_number}\n\n👤 به نام: {card_holder}\n\n'
     '📸 پس از واریز، عکس رسید پرداخت یا 📝 متن رسید را همینجا ارسال کنید.'),
    ('renew_card_registered', '✅ رسید تمدید ثبت شد. پس از تأیید ادمین، تغییرات روی سرویس اعمال می\u200cشود.'),
    ('renew_approved', '✅ تمدید سرویس شما تأیید شد.'),
    ('renew_done', 'تمدید سرویس «{service_name}» با موفقیت انجام شد.\n\n{details}\n\n✨ تمدید با موفقیت روی سرویس شما اعمال شد.'),
    ('renew_cancelled', '🔙 عملیات تمدید لغو شد.'),
    ('renew_volume_10', '۱۰ گیگ'),
    ('renew_volume_20', '۲۰ گیگ'),
    ('renew_volume_30', '۳۰ گیگ'),
    ('renew_volume_40', '۴۰ گیگ'),
    ('renew_volume_50', '۵۰ گیگ'),
    ('renew_days_30', '۳۰ روز'),
    ('renew_days_60', '۶۰ روز'),
    ('renew_days_90', '۹۰ روز'),
    ('renew_pay_card', '💳 پرداخت کارت به کارت'),
    ('renew_pay_crypto', '💱 پرداخت ارزی'),
    ('renew_pay_back', '🔙 بازگشت'),
    ('renew_cancel', '❌ لغو تمدید'),
])

# ===========================================================================
# 🛠 سرویس سفارشی (بساز سرویس خودت)
# ===========================================================================
_cat('🛠 سرویس سفارشی', [
    ('custom_build_title', '🛠 سرویس خودت رو بساز'),
    ('plans_vip_button', '⭐ سرور VIP (V2Ray)'),
    ('plans_custom_button', '🛠 سرویس خودت رو بساز'),
    ('custom_build_volume_prompt', '📦 حجم موردنظر را به گیگابایت ارسال کنید:'),
    ('custom_build_days_prompt', '⏳ مدت سرویس را به روز ارسال کنید:'),
    ('custom_build_name_prompt', '🔤 یک نام انگلیسی برای سرویس ارسال کنید:'),
    ('custom_build_summary', '🧾 خلاصه سفارش'),
    ('custom_pay_wallet', '👛 پرداخت از کیف پول'),
    ('custom_pay_online', '🌐 پرداخت آنلاین (تایید خودکار)'),
    ('custom_pay_card', '💳 پرداخت کارت به کارت'),
    ('custom_cancel', '🔙 انصراف'),
    ('custom_payment_approved', '✅ پرداخت شما تأیید شد!\nسرویس شما به\u200cزودی ساخته و ارسال می\u200cشود.'),
    ('admin_custom_approve', '✅ تأیید پرداخت'),
    ('admin_custom_reject', '❌ رد رسید'),
    ('admin_custom_send_manual', '📤 شروع ارسال کانفیگ — دستی'),
    ('online_custom_invoice',
     '🌐 پرداخت آنلاین ({gateway})\n\n'
     '🧩 سرویس سفارشی — {volume} گیگ / {days} روز\n'
     '💰 مبلغ قابل پرداخت: {price:,} تومان\n\n'
     'روی دکمه\u200cی «پرداخت» بزنید. سپس روی «بررسی پرداخت» بزنید.\n\n'
     '⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است.'),
])

# ===========================================================================
# 👤 پروفایل
# ===========================================================================
_cat('👤 پروفایل', [
    ('profile_overview',
     '🧑\u200d💻 پروفایل شما\n\n'
     '📛 نام: {name}\n🪪 آیدی: {telegram_id}\n\n'
     '💰 موجودی قابل استفاده: {wallet:,} تومان\n'
     '🔒 موجودی در انتظار: {locked:,} تومان\n\n'
     '📦 تعداد سرویس: {configs_count}\n'
     '🛒 کل خرید: {total_purchase:,} تومان\n'
     '🗓 تاریخ عضویت: {joined}\n\n'
     '👥 تعداد دعوت: {invited_count} | دعوت موفق: {successful_invites}'),
    ('profile_free_wallet', '💰 کیف پول آزاد'),
    ('profile_locked_wallet', '🔒 کیف پول مسدود'),
    ('profile_history', '🛒 تاریخچه خرید'),
    ('profile_transactions', '📋 تاریخچه تراکنش'),
    ('profile_referral', '🔗 لینک دعوت اختصاصی'),
    ('profile_back', '🏠 بازگشت به منوی اصلی'),
    ('purchase_history_empty', '🛒 شما هنوز خریدی انجام نداده\u200cاید.'),
    ('purchase_history_title', '🛒 تاریخچه خرید شما:\n\n'),
])

# ===========================================================================
# 👥 دعوت دوستان و رفرال
# ===========================================================================
_cat('👥 دعوت دوستان', [
    ('referral_overview',
     '👥 دعوت دوستان و کسب درآمد 💸\n\n'
     'دوستانتو دعوت کن و به\u200cازای هر دعوت موفق، {reward:,} تومان پاداش نقدی بگیر! 🎁\n\n'
     '🔗 لینک اختصاصی شما:\n{invite_link}\n\n'
     '🔑 کد اختصاصی: {invite_code}\n\n'
     '👤 تعداد دعوت: {invited_count}\n'
     '✅ دعوت\u200cهای موفق: {successful_invites}\n'
     '🔓 مبلغ آزاد شده: {released:,} تومان\n'
     '🔒 مبلغ در انتظار: {locked:,} تومان\n\n'
     'ℹ️ به\u200cازای هر دوستی که با لینک شما عضو شود و یک خرید حجم {min_gb} گیگ یا بیشتر انجام دهد، '
     '{reward:,} تومان به\u200cطور خودکار به کیف پول شما آزاد می\u200cشود.'),
    ('referral_back', '🏠 بازگشت به منوی اصلی'),
])

# ===========================================================================
# 👨‍💻 پشتیبانی و نمایندگی
# ===========================================================================
_cat('👨\u200d💻 پشتیبانی و نمایندگی', [
    ('support_intro', '👨\u200d💻 پشتیبانی\n\nاگر به مشکلی برخوردید می\u200cتوانید تیکت بزنید 👇'),
    ('support_ticket', '🎫 ارسال تیکت'),
    ('support_channels', '📢 کانال اصلی و پشتیبان'),
    ('support_back', '🏠 بازگشت به منوی اصلی'),
    ('support_error', '❌ خطایی در نمایش منوی پشتیبانی پیش آمد.'),
    ('ticket_write', '✍️ پیام خود را برای پشتیبانی بنویسید:'),
    ('ticket_sent', '✅ پیام شما برای پشتیبانی ارسال شد. به\u200cزودی پاسخ داده می\u200cشود.'),
    ('ticket_reply_sent', '✅ پاسخ ارسال شد.'),
    ('ticket_reply_failed', '❌ ارسال پاسخ ناموفق بود.'),
    ('agency_intro', '🤝 درخواست نمایندگی\n\nمشخصات خود را در یک پیام بنویسید و ارسال کنید 👇'),
    ('agency_cancel', '🔙 انصراف'),
    ('agency_invalid', '❌ لطفاً درخواستتون رو به\u200cصورت متن ارسال کنید:'),
    ('agency_sent', '✅ درخواست شما ارسال شد. به\u200cزودی بررسی می\u200cشود.'),
    ('admin_ticket_reply_prompt', '✏️ پاسخ خود را برای این تیکت ارسال کنید:'),
    ('admin_ticket_reply_btn', '↩️ پاسخ به تیکت'),
    ('admin_ticket_close_btn', '✅ بستن تیکت'),
    ('admin_ticket_reopen_btn', '🔓 باز کردن تیکت'),
    ('ticket_user_reply_prefix', '📩 پاسخ پشتیبانی:'),
])

# ===========================================================================
# 📚 راهنما
# ===========================================================================
_cat('📚 راهنما', [
    ('guides_back', '🏠 بازگشت به منوی اصلی'),
    ('guide_detail_back', '🔙 بازگشت به لیست راهنما'),
    ('guide_missing', '❌ این راهنما دیگر موجود نیست.'),
    ('guides_empty', '📚 راهنما و آموزش\u200cها\n\nهنوز هیچ راهنمایی ثبت نشده.'),
    ('guides_intro', '📚 راهنما و آموزش\u200cها\n\nیکی از موارد زیر را برای مشاهده انتخاب کنید 👇'),
])

# ===========================================================================
# 🔔 اعلان‌های کاربر
# ===========================================================================
_cat('🔔 اعلان\u200cها', [
    ('notif_wallet_charged', '✅ کیف پول شما {amount:,} تومان شارژ شد.'),
    ('notif_gift_received', '🎁 کیف پول شما به مبلغ {amount:,} تومان از سوی ادمین شارژ شد.'),
    ('notif_wallet_online_charged', '✅ پرداخت آنلاین شما تأیید شد و کیف پول شما به مبلغ {amount:,} تومان شارژ شد.'),
    ('notif_wallet_charge_approved', '✅ شارژ {amount:,} تومانی شما تأیید شد.'),
    ('notif_purchase_approved',
     '✅ پرداخت شما تأیید شد!\n\n📦 {plan_name}\nسرویس شما به\u200cزودی ارسال می\u200cشود.{discount_note}'),
    ('notif_receipt_approved', '✅ رسید پرداخت شما تأیید شد.'),
    ('notif_receipt_rejected', '❌ متأسفانه رسید پرداخت شما تأیید نشد. با پشتیبانی تماس بگیرید.'),
    ('notif_receipt_rejected_short', '❌ متأسفانه رسید شما تأیید نشد.'),
    ('notif_free_test_used_admin', '⚠️ این کاربر قبلاً از «تست رایگان» استفاده کرده.'),
    ('notif_usage_80',
     '🔔 هشدار حجم مصرفی سرویس\n\n📦 {plan}\n\n{bar}\n✅ شما تا الان {percent}٪ مصرف کردید.\n\nپیشنهاد می\u200cکنیم همین الان تمدید کنید 🔁'),
    ('notif_usage_90',
     '🔔 هشدار حجم مصرفی سرویس\n\n📦 {plan}\n\n{bar}\n⚠️ شما تا الان {percent}٪ مصرف کردید.\n\nپیشنهاد می\u200cکنیم همین الان تمدید کنید 🔁'),
    ('notif_expiry',
     '❌ سرویس شما به پایان رسید\n\n📦 {plan}\n\n🔴 حجم یا زمان سرویس شما تمام شده.\n\nبرای ادامه، سرویس را تمدید کنید یا سرویس جدید بخرید.'),
    ('notif_renew_service_button', 'تمدید همین سرویس'),
    ('notif_buy_new_service_button', 'خرید سرویس جدید'),
    ('notif_view_service', '📦 مشاهده سرویس'),
    ('notif_left_required', '⚠️ عضویت شما در کانال اجباری لغو شده است.\n\nدوباره عضو شوید و «عضویت را بررسی کن» را بزنید 👇'),
    ('notif_fup_start',
     '⚖️ مصرف منصفانه\n\n'
     'سرویس نامحدود شما به اسم {service_name} به حد مصرف آزاد ({free_limit}) رسیده است؛ برای امروز غیرفعال شد و از ساعت ۰۰:۰۰ فردا به‌صورت خودکار دوباره فعال می‌شود.\n\n'
     'از فردا هر روز تا {daily_limit} می‌توانید مصرف کنید؛ با رسیدن به سقف روزانه، سرویس تا نیمه‌شب متوقف و از فردا دوباره فعال می‌شود.'),
    ('notif_fup_blocked',
     '⛔ سقف مصرف روزانه\n\n'
     'سرویس {service_name} امروز به سقف مصرف منصفانه‌ی خود ({daily_limit}) رسید و تا ساعت ۰۰:۰۰ امشب متوقف است. '
     'فردا به‌صورت خودکار دوباره فعال می‌شود.'),
    ('notif_payg_empty_suspend',
     '⏸ سرویس PAYG شما به‌دلیل کافی نبودن موجودی کیف پول موقتاً متوقف شد. بعد از شارژ کیف پول دوباره فعال می‌شود.'),
    ('notif_payg_empty_delete',
     '⛔️ سرویس PAYG شما به‌دلیل کافی نبودن موجودی کیف پول حذف شد.'),
    ('notif_payg_resumed',
     '✅ سرویس PAYG شما بعد از شارژ کیف پول دوباره فعال شد.'),
    ('notif_wallet_low',
     '🪫 موجودی کیف پول شما کم شده است.\n\n💰 موجودی فعلی: {balance} تومان\n\nبرای جلوگیری از وقفه در خرید و تمدید، کیف پول خود را شارژ کنید.'),
    ('orders_opened', '🟢 ربات مجدداً فعال شد!'),
    ('orders_opened_btn', 'با زدن /start می\u200cتوانید دوباره سفارش ثبت کنید.'),
    ('orders_closed_suffix', 'روشن شدن دوباره\u200cی آن اطلاع\u200cرسانی خواهد شد.'),
])

# ===========================================================================
# 🧾 لاگ سفارش‌ها (کانال لاگ ادمین)
# ===========================================================================
_cat('🧾 لاگ سفارش‌ها', [
    ('order_log_purchase',
     '🛒 خرید جدید\n\n💳 نحوه پرداخت: {payment_method}\n'
     '👤 مشتری: {customer_name}\n🆔 آیدی: {telegram_id}\n'
     '📌 شناسه کانفیگ: {service_name}\n📦 بسته: {package_name}\n'
     '💰 مبلغ: {amount}\n📅 انقضا: {expiry}\n⏰ زمان: {time}'),
    ('order_log_renewal',
     '🔁 تمدید سرویس\n\n💳 نحوه پرداخت: {payment_method}\n'
     '👤 مشتری: {customer_name}\n🆔 آیدی: {telegram_id}\n'
     '📌 شناسه کانفیگ: {service_name}\n📦 بسته: {package_name}\n'
     '{renewal_details}💰 مبلغ: {amount}\n⏰ زمان: {time}'),
    ('order_log_test',
     '🎁 تست رایگان\n\n👤 مشتری: {customer_name}\n🆔 آیدی: {telegram_id}\n'
     '📌 شناسه کانفیگ: {service_name}\n📦 بسته: {package_name}\n'
     '💰 مبلغ: رایگان\n📅 انقضا: {expiry}\n⏰ زمان: {time}'),
    ('order_log_custom',
     '🛠 سرویس سفارشی جدید\n\n💳 نحوه پرداخت: {payment_method}\n'
     '👤 مشتری: {customer_name}\n🆔 آیدی: {telegram_id}\n'
     '📌 شناسه کانفیگ: {service_name}\n📦 بسته: {package_name}\n'
     '💰 مبلغ: {amount}\n📅 انقضا: {expiry}\n⏰ زمان: {time}'),
    ('order_log_payg',
     '⚡ خرید سرویس PAYG\n\n💳 نحوه پرداخت: {payment_method}\n'
     '👤 مشتری: {customer_name}\n🆔 آیدی: {telegram_id}\n'
     '📌 شناسه کانفیگ: {service_name}\n📦 بسته: {package_name}\n'
     '💰 مبلغ: {amount}\n📅 انقضا: {expiry}\n⏰ زمان: {time}'),
    ('admin_card_receipt',
     '💳 رسید کارت به کارت\n\n👤 {name}\n🆔 {telegram_id}\n\n'
     '📦 بسته: {plan_name}\n💰 {price:,} تومان'),
    ('admin_wallet_charge_receipt',
     '💳 رسید شارژ کیف پول\n\n👤 {name}\n🆔 {telegram_id}\n\n'
     '💰 مبلغ: {amount:,} تومان'),
    ('admin_custom_build_receipt',
     '🛠 رسید سرویس سفارشی\n\n👤 {name}\n🆔 {telegram_id}\n\n'
     '📦 {volume} گیگ / {days} روز\n💰 {price:,} تومان'),
    ('admin_renew_card_receipt',
     '🔁 رسید تمدید\n\n👤 مشتری: {customer}\n🆔 {telegram_id}\n'
     '📌 سرویس: {service_username}\n📦 بسته: {package_name}\n'
     '{renew_details}💰 مبلغ: {amount:,} تومان'),
    ('admin_crypto_receipt',
     '💱 رسید پرداخت ارزی\n\n👤 {name}\n🆔 {telegram_id}\n\n'
     '📦 بسته: {plan_name}\n💰 {price:,} تومان\n'
     '💵 {amount} {asset}\n📝 Hash: {hash_text}'),
    ('admin_crypto_receipt_photo',
     '💱 رسید پرداخت ارزی (تصویر)\n\n👤 {name}\n🆔 {telegram_id}\n\n'
     '📦 بسته: {plan_name}\n💰 {price:,} تومان\n💵 {amount} {asset}'),
    ('admin_crypto_approve', '✅ تأیید پرداخت ارزی'),
    ('admin_crypto_reject', '❌ رد پرداخت ارزی'),
    ('admin_renew_approve', '✅ تأیید تمدید'),
    ('admin_renew_reject', '❌ رد تمدید'),
    ('admin_crypto_renew_receipt',
     '🔁 رسید تمدید ارزی\n\n👤 {name}\n🆔 {telegram_id}\n'
     '📌 سرویس: {service_name}\n📦 بسته: {plan_name}\n'
     '{renew_details}💰 {price:,} تومان\n💵 {amount} {asset}'),
    ('admin_crypto_photo_only', '📸 رسید تصویری ارسال شده؛ Hash متنی ثبت نشده.'),
    ('fair_use_admin_request',
     '⚖️ درخواست مصرف منصفانه\n\n👤 {customer_name}\n🆔 {telegram_id}\n\n'
     '📦 بسته: {plan}\n📌 سرویس: {service_name}\n'
     '📊 مصرف: {used} / سقف: {fair_use_gb} گیگ\n📅 انقضا: {expiry}'),
])


# ===========================================================================
# ✅ متن‌های اصلی که از «صفحه‌ها» هم ساخته می‌شوند (کلید یکسان با صفحه → یک ویرایش، یک اثر)
# ===========================================================================
_cat('📤 تحویل سرویس', [
    ('config_delivery',
     '✅ سرویس با موفقیت ایجاد شد\n\n'
     '👤 نام کاربری سرویس : {name}\n'
     '🇺🇳 لوکیشن: {location}\n'
     '⏳ مدت زمان: {days}\n'
     '🗜 حجم سرویس: {volume}\n'
     '👤 تعداد کاربر: {users}\n\n'
     'لینک اتصال:\n{sub_link}\n\n'
     '🧑\u200d🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید.'),
    ('config_delivery_test',
     '🎁 تست رایگان شما آماده شد ✅\n\n'
     '👤 نام کاربری سرویس : {name}\n'
     '⏳ مدت زمان: {days}\n'
     '🗜 حجم سرویس: {volume}\n\n'
     'لینک اتصال:\n{sub_link}\n\n'
     '🧑\u200d🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید.'),
])
_cat('🌐 درگاه‌های پرداخت آنلاین', [
    ('gateway_invoice',
     '{progress}🌐 پرداخت آنلاین ({title})\n\n'
     '{details}\n\n'
     'روی دکمه‌ی پرداخت بزنید و پرداخت را کامل کنید؛ سپس همینجا روی «بررسی کن» بزنید.\n'
     '⏱ به‌محض تأیید نهایی {title}، {done_text}\n\n'
     '{deadline_note}'),
    ('gateway_tonpays_card',
     '💰 مبلغ دقیق واریز: {amount} ریال\n'
     '💳 شماره کارت: {card}\n'
     '👤 صاحب کارت: {holder}\n'
     '🧾 کد پیگیری: {tracking}\n\n'
     '📸 پس از واریز می‌توانید تصویر رسید را ارسال کنید و وضعیت پرداخت را بررسی کنید.\n\n'
     '🌟 پرداخت ها توسط درگاه Ton Pays تایید میشه'),
])
_cat('🎁 تست رایگان', [
    ('free_test_intro',
     '🎁 {plan_name}\n\n'
     '🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n'
     '۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n'
     '⚡️ روی دکمه‌ی سبز زیر بزن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅'),
    ('free_test_list_intro',
     '🎁 تست رایگان\n\n'
     '🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n'
     '۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n'
     '👇 یکی از تست‌های زیر را انتخاب کن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅'),
    ('free_test_multi',
     '🎁 تست‌های رایگان شما آماده شد ✅\n\n{tests}\n\n'
     '🧑\u200d🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید.'),
])

# ===========================================================================
# TEXTS و سایر متغیرها
# ===========================================================================
TEXTS = {key: default for items in TEXT_CATEGORIES.values() for key, default in items}
CATEGORY_BY_KEY = {key: category for category, items in TEXT_CATEGORIES.items() for key, _ in items}
_CACHE: dict = {}


# ---------------------------------------------------------------------------
# RichText — نگهداری Premium Emoji و MessageEntity
# ---------------------------------------------------------------------------
class RichText(str):
    """رشته‌ای که entityهای واقعی تلگرام را همراه خودش حمل می‌کند.
    Custom/Premium Emoji، Bold، Italic، لینک و سایر entityها بعد از ذخیره
    در پنل ادمین هنگام ارسال دوباره به Telegram تحویل داده می‌شوند.
    """

    def __new__(cls, value: str, entities: list | None = None):
        obj = super().__new__(cls, value)
        obj.entities = [dict(e) for e in (entities or [])]
        return obj

    @staticmethod
    def _units(value: str) -> int:
        return len(str(value).encode('utf-16-le')) // 2

    def __add__(self, other):
        if isinstance(other, RichText):
            return RichText(
                str(self) + str(other),
                self.entities + other.entities_shifted(self._units(str(self)))
            )
        return RichText(str(self) + str(other), self.entities)

    def __radd__(self, other):
        shift = self._units(str(other))
        return RichText(str(other) + str(self), self.entities_shifted(shift))

    def entities_shifted(self, shift: int) -> list:
        result = []
        for e in self.entities:
            x = dict(e)
            x['offset'] = int(x.get('offset', 0)) + shift
            result.append(x)
        return result


def _render_with_entities(template: str, entities: list, values: dict) -> RichText:
    """جایگزینی {placeholder}ها با حفظ offset صحیح entityها"""
    import string
    formatter = string.Formatter()
    parts = []
    src_pos = 0
    out_pos = 0
    mappings = []  # (src_start, src_end, out_start, out_end)

    for literal, field, spec, conv in formatter.parse(template):
        if literal:
            parts.append(literal)
            n = len(literal.encode('utf-16-le')) // 2
            mappings.append((src_pos, src_pos + n, out_pos, out_pos + n))
            src_pos += n
            out_pos += n
        if field is not None:
            token = '{' + field
            if conv:
                token += '!' + conv
            if spec:
                token += ':' + spec
            token += '}'
            src_n = len(token.encode('utf-16-le')) // 2
            try:
                value = formatter.get_field(field, (), values)[0]
                if conv:
                    value = formatter.convert_field(value, conv)
                value = formatter.format_field(value, spec)
            except Exception:
                value = '{' + field + (':' + spec if spec else '') + '}'
            value = str(value)
            parts.append(value)
            out_n = len(value.encode('utf-16-le')) // 2
            mappings.append((src_pos, src_pos + src_n, out_pos, out_pos + out_n))
            src_pos += src_n
            out_pos += out_n

    rendered = ''.join(parts)
    rendered_units = len(rendered.encode('utf-16-le')) // 2

    def map_boundary(pos: int):
        for a, b, c, d in mappings:
            if a <= pos <= b:
                if b == a:
                    return c
                if pos == b:
                    return d
                ratio = (pos - a) / (b - a)
                return int(round(c + ratio * (d - c)))
        return None

    out_entities = []
    for ent in (entities or []):
        try:
            off = int(ent.get('offset', 0))
            length = int(ent.get('length', 0))
            start = map_boundary(off)
            end = map_boundary(off + length)
            if start is None or end is None or end <= start or end > rendered_units:
                continue
            e = dict(ent)
            e['offset'] = start
            e['length'] = end - start
            out_entities.append(e)
        except Exception:
            continue
    return RichText(rendered, out_entities)


def text(key: str, default: str | None = None, **values) -> str:
    """متن نهایی قابل ارسال به تلگرام را برمی‌گرداند.
    
    اگر ادمین override کرده باشد، آن را برمی‌گرداند؛ وگرنه پیش‌فرض.
    اگر entityهای Premium Emoji داشته باشد، RichText برمی‌گرداند.
    """
    # 🐛 فیکس: قبلاً نتیجه در _CACHE دائمی ذخیره می‌شد و refresh() هیچ‌جا صدا زده نمی‌شد؛ پس بعد از
    # «ویرایش متن‌ها» (لاگ سفارش، اعلان‌ها و …) متن/ایموجی پریمیوم جدید تا ری‌استارت اعمال نمی‌شد.
    # حالا مستقیم از کش ui_editor (که با set_text پاک می‌شود) خوانده می‌شود.
    try:
        import ui_editor as _ue
        template = _ue.get_text(key, TEXTS.get(key, default or ''))
        if not template:
            template = TEXTS.get(key, default or '')
        entities = _ue.get_entities(key)
    except Exception:
        template = db.get_text_override(key, TEXTS.get(key, default or ''))
        entities = db.get_text_override_entities(key)
    if values:
        try:
            return _render_with_entities(template, entities, values)
        except Exception:
            fallback = TEXTS.get(key, default or template)
            try:
                return RichText(fallback.format_map(values), [])
            except Exception:
                return RichText(fallback, [])
    return RichText(template, entities) if entities else template


def refresh(key: str):
    """حذف cache برای یک کلید (بعد از ویرایش ادمین)"""
    _CACHE.pop(key, None)


def refresh_all():
    """پاک کردن کل cache"""
    _CACHE.clear()


def all_items():
    return TEXT_CATEGORIES
