"""
handlers/plans.py
نمایش دسته‌بندی سرویس‌های VIP، اعمال کد تخفیف، خرید سرویس
(با دو روش پرداخت: کیف پول و کارت‌به‌کارت)، و نمایش سرویس‌های خریداری‌شده کاربر.

نکته مهم: بعد از یک خرید موفق (چه با کیف پول چه با کارت‌به‌کارت)، اگر حجم آن
پلن حداقل REFERRAL_MIN_VOLUME_GB گیگ باشد، db.complete_referral فراخوانی
می‌شود تا اگر معرفی داشته، مبلغ قفل‌شده‌ی معرفش آزاد شود (تست رایگان و
پلن‌های زیر این حجم پاداش را آزاد نمی‌کنند).
"""

import ui_editor


import json
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

import database as db
import crypto
import uniquepay
import uniquepay_cfg
import online_gateways as og
import alerts
import bot_info
from subscription import fetch_subscription_info, extract_configs, extract_meta, format_bytes, format_expire, usage_bar, days_remaining, is_config_expired, format_service_package
from utils import WALLET_PAID_TEXT, send_user_notification, parse_int_in_range, is_duplicate_action, format_deadline_time, progress_bar, now_tehran_naive, show_menu_with_sticker, invoice_expiry_note, apply_random_amount_variation, to_jalali_str, extract_receipt_media, send_receipt_card_to_admin, receipt_user_label
from states import UserStates
from handlers.panel_admin import auto_fulfill_vip_via_panel, auto_fulfill_custom_via_panel, _deliver_panel_link
import panels
from uniquepay_cfg import enabled as _uq_enabled
from config import (
    ADMIN_ID,
    PLANS_INTRO_TEXT,
    FREE_TEST_PLAN_KEY,
)
from keyboards import (
    plans_menu,
    plans_direct_to_vip,
    vip_categories_keyboard,
    vip_category_plans_keyboard,
    purchase_payment_keyboard,
    free_test_confirm_keyboard,
    free_claim_keyboard,
    insufficient_balance_keyboard,
    back_button,
    admin_purchase_notify_keyboard,
    admin_purchase_card_approval_keyboard,
    admin_custom_order_notify_keyboard,
    admin_custom_order_card_approval_keyboard,
    my_configs_menu,
    my_configs_list_keyboard,
    service_search_results_keyboard,
    config_detail_keyboard,
    confirm_delete_config_keyboard,
    custom_build_payment_keyboard,
    custom_build_cancel_keyboard,
    payg_name_keyboard,
    renew_mode_keyboard,
    renew_payment_keyboard,
    online_payment_keyboard,
    receipt_submitted_keyboard,
    card_payment_actions_keyboard,
    confirm_change_sublink_keyboard,
    config_name_reply_keyboard,
)

logger = logging.getLogger(__name__)

ORDERS_CLOSED_TEXT = (
    "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد."
)

plan_type = db.plan_type  # نسخه‌ی DB-aware (دسته‌بندی‌های VIP را هم می‌شناسد)

# fix: این الگو قبلاً هیچ‌جا تعریف نشده بود ولی در custom_name_input استفاده
# می‌شد؛ در نتیجه وارد کردن نام برای «کانفیگ خودتو بساز» همیشه با NameError
# کرش می‌کرد. فقط حروف/عدد انگلیسی (بدون فاصله و کاراکتر اضافه) مجاز است.
_LATIN_NAME_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")

# ---------------------------------------------------------------------------
# دکمه‌های اینلاین این فایل از ویرایشگر دکمه‌ها عبور می‌کنند: callback → (صفحه، کلید دکمه)
# ---------------------------------------------------------------------------
_UI_BTN_MAP = (
    ("renewvol_custom", "renew_menu", "renewvol_custom"), ("renewdays_custom", "renew_menu", "renewdays_custom"),
    ("renew_cancel", "renew_menu", "renew_cancel"),
    ("copyallcfg_", "config_copy_all", "copy_all"),
)


def _UB(**kw):
    from keyboards import InlineKeyboardButton as _B
    if "ui_screen" not in kw:
        cb = str(kw.get("callback_data") or "")
        for pre, sc, key in _UI_BTN_MAP:
            if cb == pre or (pre.endswith("_") and cb.startswith(pre)):
                kw["ui_screen"], kw["ui_button_key"] = sc, key
                break
        else:
            if kw.get("copy_text") is not None:
                kw["ui_screen"], kw["ui_button_key"] = "config_copy_all", "copy_all"
    return _B(**kw)


def _UM(**kw):
    from keyboards import InlineKeyboardMarkup as _M
    return _M(**kw)


router = Router(name="plans")


def _calc_custom_price(volume_gb: int, days: int, telegram_id=None, option_num: int | None = None) -> tuple[int, bool]:
    """قیمت «بساز سرویس خودت» را حساب می‌کند و اگر کاربر نماینده باشد، تخفیف
    نمایندگی‌اش (مثل پلن‌های VIP) روی همین قیمت هم اعمال می‌شود.
    خروجی دوم True است اگر تخفیف نمایندگی اعمال شده باشد."""
    _cb = db.get_effective_custom_build_settings(option_num)
    price = volume_gb * _cb["price_per_gb"] + (days / 30) * _cb["price_per_30_days"]
    discount_applied = False
    if telegram_id is not None:
        _pct = db.agent_percent_for_option(telegram_id, "custom_build", option_num)
        if db.get_agent(telegram_id) and _pct > 0:
            price = price * (1 - _pct / 100)
            discount_applied = True
    return int(round(price)), discount_applied


@router.callback_query(F.data == "noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "plans")
async def show_services(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not db.is_orders_enabled():
        await callback.answer(ORDERS_CLOSED_TEXT, show_alert=True)
        return
    if plans_direct_to_vip():
        # سفارشی و PAYG خاموشند → مستقیم پوشه‌ها و پلن‌ها
        await _send_vip_categories(callback.bot, callback.message.chat.id)
        await callback.answer()
        return
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "buy_plans", PLANS_INTRO_TEXT, reply_markup=plans_menu(), ui_key=ui_editor.plans_intro_key())
    await callback.answer()


def _vip_root_description():
    """توضیحات صفحه‌ی اصلی دسته‌بندی‌های VIP (ثبت‌شده توسط ادمین روی «پلن‌های بدون پوشه»)."""
    try:
        root = db.get_vip_root_bucket(create=False)
        if not root:
            return "", None, None
        text, ents = db.get_vip_category_description(root)
        if not text:
            return "", None, None
        import rich_registry
        return text, ents, __import__("rich_message").load("cat", root["id"])
    except Exception:
        return "", None, None


async def _send_vip_root_menu(bot, chat_id: int, sticker_key: str, base_text: str, markup, ui_key=None):
    """منوی اصلی VIP؛ اگر ادمین توضیحات تعریف کرده باشد، زیر عنوان نمایش داده می‌شود."""
    desc, ents, rich = _vip_root_description()
    if not desc:
        await show_menu_with_sticker(bot, chat_id, sticker_key, base_text, reply_markup=markup, ui_key=ui_key)
        return
    # وقتی ادمین توضیحات برای صفحه‌ی اصلی VIP گذاشته، متن پیش‌فرض (عنوان) حذف می‌شود و فقط توضیحات ادمین نمایش داده می‌شود.
    entities = list(ents) if ents else None
    if entities:
        import rich_registry
        entities = rich_registry.shift_entities(entities, 0)
    await show_menu_with_sticker(
        bot, chat_id, sticker_key, desc, reply_markup=markup,
        text_entities=entities, skip_ui_text=True, rich_payload=rich,
    )


async def _send_vip_categories(bot, chat_id: int):
    if plans_direct_to_vip():
        # سفارشی و PAYG خاموشند: پوشه‌ها/پلن‌ها زیر همان متن و استیکر «خرید اشتراک» نمایش داده می‌شوند
        await _send_vip_root_menu(bot, chat_id, "buy_plans", PLANS_INTRO_TEXT, vip_categories_keyboard(), ui_key=ui_editor.plans_intro_key())
        return
    from keyboards import vip_shortcut_on
    if vip_shortcut_on():
        # ⚡ میانبر VIP: پوشه‌ها/پلن‌ها کنار سفارشی و PAYG زیر همان متن «خرید اشتراک»
        await _send_vip_root_menu(bot, chat_id, "buy_plans", PLANS_INTRO_TEXT, plans_menu(), ui_key=ui_editor.plans_intro_key())
        return
    # 🧪 تست: استیکر plan.webm درست بالای منوی دسته‌های VIP
    await _send_vip_root_menu(
        bot, chat_id, "plan_select",
        "سرویس‌های VIP (V2Ray)\n\nیکی از دسته‌ها را انتخاب کنید 👇", vip_categories_keyboard(),
    )


@router.callback_query(F.data == "plans_vip")
async def show_vip_plans(callback: types.CallbackQuery, state: FSMContext):
    await _send_vip_categories(callback.bot, callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data.startswith("vipcat_"))
async def show_vip_category_plans(callback: types.CallbackQuery, state: FSMContext):
    category_key = callback.data.replace("vipcat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer(ui_editor.get_alert_text("msg_1a7a2ff066", "❌ این دسته یافت نشد."), show_alert=True)
        return
    data = await state.get_data()
    discount = data.get("discount_percent", 0)
    await _render_category_plans(callback.bot, callback.message.chat.id, cat, category_key, discount)
    await callback.answer()


async def _render_category_plans(bot, chat_id: int, cat: dict, category_key: str, discount):
    text = f"{cat['name']}:"
    desc_entities = None
    desc_text, desc_ents = db.get_vip_category_description(cat)
    if desc_text:
        # 📝 توضیحات پوشه: بالای منوی داخل پوشه، با همان ایموجی پریمیوم/ریچ‌تکستی که ادمین فرستاده.
        prefix = f"{text}\n\n"
        text = prefix + desc_text
        if desc_ents:
            import rich_registry
            desc_entities = rich_registry.shift_entities(desc_ents, rich_registry.u16len(prefix))
    await show_menu_with_sticker(bot, chat_id, "vip_category_list",
        text, reply_markup=vip_category_plans_keyboard(category_key, discount),
        text_entities=desc_entities, skip_ui_text=True,
        rich_payload=__import__("rich_message").load("cat", cat["id"]),
    )


def _compute_final_price(plan_key: str, plan: dict, telegram_id, data: dict) -> tuple[int, str, str | None]:
    """قیمت نهایی یک پلن را با درنظرگرفتن کد تخفیف کاربر (اگر برای این پلن معتبر باشد)
    و تخفیف خودکار نمایندگی (فقط روی VIP) محاسبه می‌کند و بهترین (کمترین) قیمت را برمی‌گرداند.
    خروجی سوم، کد تخفیفی است که واقعاً «برنده» شده (باید مصرفش ثبت شود) یا None اگر
    تخفیف نمایندگی برنده شده باشد یا هیچ تخفیفی اعمال نشده باشد."""
    price = plan["price"]

    code_price = price
    code = data.get("discount_code")
    valid_code = False
    if code:
        discount = db.get_discount(code)
        user = db.get_user(telegram_id)
        if (
            discount
            and discount["uses"] > 0
            and not db.discount_is_expired(discount)
            and db.discount_applies_to_plan(discount, plan_key)
            and db.discount_allowed_for_user(discount, telegram_id)
            and db.discount_allowed_for_first_purchase(discount, user["id"] if user else 0)
        ):
            over_cap = (
                discount.get("max_uses_per_user")
                and user is not None
                and db.user_discount_uses(discount["id"], user["id"]) >= discount["max_uses_per_user"]
            )
            under_min = discount.get("min_order_amount") and price < discount["min_order_amount"]
            if not over_cap and not under_min:
                code_price = db.compute_discount(discount, price)
                valid_code = True

    agent_price = price
    if plan_type(plan_key) == "vip":
        _pct = db.agent_percent_for_plan(telegram_id, plan_key, plan.get("category_id"))
        if _pct:
            agent_price = int(round(price * (1 - _pct / 100)))

    final_price = min(code_price, agent_price)
    note = ""
    winning_code = None
    if final_price < price:
        if valid_code and code_price <= agent_price:
            note = " (کد تخفیف اعمال شد)"
            winning_code = code
        else:
            note = " (تخفیف نمایندگی اعمال شد)"
    return final_price, note, winning_code


# ---------------------------------------------------------------------------
# کد تخفیف عمومی (از طریق کیف پول وارد می‌شود و روی خرید بعدی اعمال می‌شود)
# ---------------------------------------------------------------------------
@router.message(UserStates.waiting_discount_code)
async def check_discount(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    discount = db.get_discount(code)

    if discount is None or discount["uses"] <= 0 or db.discount_is_expired(discount):
        await message.answer(
            ui_editor.get_text("msg_3a34d9a47c", "❌ کد تخفیف نامعتبر یا تمام شده."),
            reply_markup=back_button("plans", "🔙 بازگشت", screen="discount_code_entry"),
        )
        await state.clear()
        return

    if not db.discount_allowed_for_user(discount, message.from_user.id):
        await message.answer(
            ui_editor.get_text("msg_9462130b1f", "❌ شما مجاز به استفاده از این کد تخفیف نیستید."),
            reply_markup=back_button("plans", "🔙 بازگشت", screen="discount_code_entry"),
        )
        await state.clear()
        return

    _fp_user = db.get_user(message.from_user.id)
    if not db.discount_allowed_for_first_purchase(discount, _fp_user["id"] if _fp_user else 0):
        await message.answer(
            "❌ این کد مخصوص اولین خرید است و چون قبلاً از فروشگاه خرید کرده‌اید، برایتان قابل استفاده نیست.",
            reply_markup=back_button("plans", "🔙 بازگشت", screen="discount_code_entry"),
        )
        await state.clear()
        return

    if discount.get("max_uses_per_user"):
        user = db.get_user(message.from_user.id)
        if user and db.user_discount_uses(discount["id"], user["id"]) >= discount["max_uses_per_user"]:
            await message.answer(
                ui_editor.get_text("msg_4c8185f39a", "❌ سهمیه‌ی استفاده‌ی شما از این کد تمام شده."),
                reply_markup=back_button("plans", "🔙 بازگشت", screen="discount_code_entry"),
            )
            await state.clear()
            return

    if discount.get("discount_type") == "amount":
        await message.answer(
            ui_editor.get_text("msg_47e7ab291b", "💡 این کد یک کد تخفیف با مبلغ ثابت است؛ لطفاً از منوی «🛒 خرید اشتراک» پلن مورد نظرتان را انتخاب "
            "کنید و در صفحه‌ی پرداخت همان پلن، کد را وارد کنید."),
            reply_markup=plans_menu(),
        )
        await state.clear()
        return

    await state.update_data(discount_code=code, discount_percent=discount["percent"])
    plans_note = "" if not db.discount_is_expired(discount) and not db.discount_plans(discount) else \
        " (فقط روی پلن‌های خاص قابل استفاده است)"
    await message.answer(
        f"✅ کد تخفیف {discount['percent']}٪ با موفقیت ثبت شد و در خرید بعدی شما (در صورت تطابق پلن) اعمال می‌شود.{plans_note}",
        reply_markup=plans_menu(),
    )
    await state.set_state(None)


# ---------------------------------------------------------------------------
# کد تخفیف اختصاصیِ یک پلن (در مرحله‌ی پرداخت)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("discount_plan_"))
async def discount_for_plan(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("discount_plan_", "")
    if db.get_effective_plan(plan_key) is None:
        await callback.answer(ui_editor.get_alert_text("msg_80727cdf55", "❌ این پلن یافت نشد."), show_alert=True)
        return
    await state.update_data(discount_target_plan=plan_key)
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "discount_code_entry", "🎟 کد تخفیف خود را وارد کنید:")
    await state.set_state(UserStates.waiting_discount_plan)
    await callback.answer()


@router.message(UserStates.waiting_discount_plan)
async def check_discount_for_plan(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    discount = db.get_discount(code)
    data = await state.get_data()
    plan_key = data.get("discount_target_plan")
    plan = db.get_effective_plan(plan_key)

    if plan is None:
        await message.answer(ui_editor.get_text("msg_792953088f", "❌ مشکلی پیش آمد، دوباره از منوی سرویس‌ها شروع کنید."), reply_markup=back_button("plans", "🔙 بازگشت", screen="discount_code_entry"))
        await state.clear()
        return

    if discount is None or discount["uses"] <= 0 or db.discount_is_expired(discount):
        await message.answer(
            ui_editor.get_text("msg_3a34d9a47c", "❌ کد تخفیف نامعتبر یا تمام شده."),
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=True),
        )
        await state.set_state(None)
        return

    if not db.discount_applies_to_plan(discount, plan_key):
        await message.answer(
            ui_editor.get_text("msg_8af7a32c0b", "❌ این کد تخفیف روی این پلن قابل استفاده نیست."),
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=True),
        )
        await state.set_state(None)
        return

    if not db.discount_allowed_for_user(discount, message.from_user.id):
        await message.answer(
            ui_editor.get_text("msg_9462130b1f", "❌ شما مجاز به استفاده از این کد تخفیف نیستید."),
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=True),
        )
        await state.set_state(None)
        return

    _fp_user = db.get_user(message.from_user.id)
    if not db.discount_allowed_for_first_purchase(discount, _fp_user["id"] if _fp_user else 0):
        await message.answer(
            "❌ این کد مخصوص اولین خرید است و چون قبلاً از فروشگاه خرید کرده‌اید، برایتان قابل استفاده نیست.",
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=True),
        )
        await state.set_state(None)
        return

    if discount.get("max_uses_per_user"):
        user = db.get_user(message.from_user.id)
        if user and db.user_discount_uses(discount["id"], user["id"]) >= discount["max_uses_per_user"]:
            await message.answer(
                ui_editor.get_text("msg_4c8185f39a", "❌ سهمیه‌ی استفاده‌ی شما از این کد تمام شده."),
                reply_markup=purchase_payment_keyboard(plan_key, show_discount=True),
            )
            await state.set_state(None)
            return

    if discount.get("min_order_amount") and plan["price"] < discount["min_order_amount"]:
        await message.answer(
            f"❌ این کد فقط برای خریدهای بالای {discount['min_order_amount']:,} تومان قابل استفاده است.",
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=True),
        )
        await state.set_state(None)
        return

    await state.update_data(discount_code=code, discount_percent=discount["percent"])
    final_price = db.compute_discount(discount, plan["price"])
    value_text = f"{discount['percent']}٪" if discount.get("discount_type") != "amount" else f"{discount['amount']:,} تومانی"
    text = (
        f"✅ کد تخفیف {value_text} اعمال شد!\n\n"
        f"🛒 {plan['name']}\n💰 قیمت نهایی: {final_price:,} تومان\n\n"
        + ("🎁 این سفارش رایگان شد! برای دریافت سرویس روی دکمه‌ی زیر بزنید." if int(final_price) <= 0 else "روش پرداخت را انتخاب کنید:")
    )
    await message.answer(text, reply_markup=(free_claim_keyboard(plan_key) if int(final_price) <= 0 else purchase_payment_keyboard(plan_key, show_discount=False)))
    await state.set_state(None)


# ---------------------------------------------------------------------------
# انتخاب پلن → نمایش روش‌های پرداخت
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 🏷 نام‌گذاری سرویس توسط کاربر (مرحله‌ی بعد از انتخاب پلن)
#
# فقط وقتی ادمین از «ℹ️ اطلاعات ربات ← 🏷 پیشوند» گزینه‌ی «نامگذاری توسط کاربر» را روشن کرده باشد.
# نماینده‌ها و تست رایگان هرگز این مرحله را نمی‌بینند (نماینده: پیشوند پیش‌فرض/اختصاصی خودش).
# نام نهایی = نام انتخابی (حروف کوچک) + _ + ۵ رقم تصادفی؛ بعد از بررسی یکتا بودن در دیتابیس و
# خودِ پنل روی سفارش ذخیره می‌شود و ساخت سرویس روی پنل دقیقاً با همین یوزرنیم انجام می‌شود.
# ---------------------------------------------------------------------------
_CHOSEN_BASE_RE = re.compile(r"[A-Za-z0-9_]{3,26}")


def _naming_applies(telegram_id, plan_key: str) -> bool:
    try:
        if bot_info.get_config_name_mode() != "user":
            return False
        if db.is_free_test_key(plan_key) or plan_type(plan_key) != "vip":
            return False
        if db.get_agent(telegram_id):
            return False
        return True
    except Exception:
        logger.exception("naming_applies check failed")
        return False


def _chosen_name(data: dict, plan_key: str) -> str | None:
    """نام انتخابیِ همین پلن از FSM (اگر کاربر برای پلن دیگری نام انتخاب کرده بود نادیده گرفته می‌شود)."""
    name = (data or {}).get("purchase_config_name")
    if name and (data or {}).get("purchase_config_name_plan") == plan_key:
        return str(name)
    return None


def _auto_service_name(callback_or_message) -> str:
    """نام پایه‌ی خودکار از یوزرنیم/نام تلگرام کاربر؛ پسوند پنج‌رقمی بعداً اضافه می‌شود."""
    # حالت «با پیشوند پیش‌فرض» (تنظیم ادمین): نام پایه همان پیشوند پیش‌فرض است و پسوند طبق تنظیمات بعداً اضافه می‌شود.
    try:
        if bot_info.get_auto_name_mode() == "prefix":
            _pfx = re.sub(r"[^A-Za-z0-9_]+", "", str(bot_info.get("config_name_prefix") or "")) or "tg"
            return _pfx[:26]
    except Exception:
        logger.debug("auto name prefix mode failed", exc_info=True)
    user = getattr(callback_or_message, "from_user", None)
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    full_name = str(getattr(user, "full_name", "") or "").strip()
    if _CHOSEN_BASE_RE.fullmatch(username or ""):
        return username
    clean_name = re.sub(r"[^A-Za-z0-9_]", "_", full_name).strip("_")
    if _CHOSEN_BASE_RE.fullmatch(clean_name or ""):
        return clean_name
    return "svc"


async def _service_name_available(plan_key: str, name: str, panel: dict | None = None) -> tuple[bool, str]:
    if not re.fullmatch(r"[a-z0-9_]{3,32}", name or ""):
        return False, str(ui_editor.get_text("config_name_invalid", "❌ نام نامعتبر است."))
    if db.service_name_taken(name):
        return False, str(ui_editor.get_text("config_name_duplicate", "❌ نام تکراری است. لطفاً یک اسم دیگر انتخاب کنید."))
    try:
        if panel is None:
            mapping = db.get_panel_map_for_plan_key(plan_key)
            if mapping and mapping.get("enabled"):
                panel = db.get_vpn_panel(mapping.get("panel_id"))
        if panel is not None:
            if panel and panel.get("enabled"):
                ok, snap, _msg = await panels.get_service_snapshot(panel, name)
                if ok and snap:
                    return False, str(ui_editor.get_text("config_name_panel_duplicate", "❌ این نام قبلاً در پنل وجود دارد. لطفاً یک اسم دیگر انتخاب کنید."))
    except Exception:
        # خطای شبکه/پنل جلوی خرید را نمی‌گیرد؛ ساخت روی پنل در صورت تکراری‌بودن خودش خطا می‌دهد.
        logger.debug("panel duplicate check failed", exc_info=True)
    return True, ""


async def _finalize_service_name(plan_key: str, base_name: str, panel: dict | None = None, allow_short: bool = False) -> str:
    """نام نهایی: base_12345 (حروف کوچک)، با بررسی هم‌زمان دیتابیس و پنل."""
    import secrets
    base = (base_name or "").strip().lstrip("@")
    if not (_CHOSEN_BASE_RE.fullmatch(base) or (allow_short and re.fullmatch(r"[A-Za-z0-9_]{1,26}", base))):
        raise ValueError("invalid base name")
    base = base.lower()
    for _ in range(100):
        _sfx = bot_info.random_suffix()
        candidate = f"{base[:31 - len(_sfx)].rstrip('_') or base[:1]}_{_sfx}"
        ok, _err = await _service_name_available(plan_key, candidate, panel)
        if ok:
            return candidate
    raise RuntimeError("نتوانستم یک نام یکتا برای سرویس پیدا کنم.")


async def _show_plan_payment_methods(callback: types.CallbackQuery, state: FSMContext, plan_key: str):
    if await _plan_payment_page(callback.bot, callback.message.chat.id, callback.from_user.id, state, plan_key, callback=callback):
        await callback.answer()


async def _plan_payment_page(bot, chat_id: int, tg_user_id: int, state: FSMContext, plan_key: str, callback: types.CallbackQuery | None = None) -> bool:
    """صفحه‌ی انتخاب روش پرداخت یک پلن؛ هم از callback و هم بعد از مرحله‌ی «انتخاب نام» (message) استفاده می‌شود.
    خروجی True یعنی صفحه نمایش داده شد (callback.answer را caller می‌زند)."""
    from types import SimpleNamespace as _NS
    if callback is None:
        async def _answer(text=None, show_alert=False, **_kw):
            if text and show_alert:
                await bot.send_message(chat_id, str(text))
        callback = _NS(bot=bot, message=_NS(chat=_NS(id=chat_id)), from_user=_NS(id=tg_user_id), answer=_answer)
    return await _plan_payment_page_impl(callback, state, plan_key)


async def _plan_payment_page_impl(callback, state: FSMContext, plan_key: str) -> bool:
    if not db.is_orders_enabled():
        await callback.answer(ORDERS_CLOSED_TEXT, show_alert=True)
        return False

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        await callback.answer(ui_editor.get_alert_text("msg_80727cdf55", "❌ این پلن یافت نشد."), show_alert=True)
        return False

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return False

    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        await callback.answer(
            ui_editor.get_text("msg_b5034a8263", "⚠️ شما قبلاً از «تست رایگان» استفاده کرده‌اید. هر کاربر فقط یک‌بار می‌تواند این پلن را دریافت کند."),
            show_alert=True,
        )
        return False

    if db.is_free_test_key(plan_key):
        # fix: متن این صفحه با تکنیک‌های روان‌شناسی فروش (اثبات بدون ریسک، حذف اصطکاک
        # تصمیم‌گیری، تأکید بر فوریت/سادگی) بازنویسی شد.
        text = (
            f"🎁 {plan['name']}\n\n"
            "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
            "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
            "⚡️ روی دکمه‌ی سبز زیر بزن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
        )
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "plan_payment_method", text,
            reply_markup=free_test_confirm_keyboard(plan_key), ui_key="free_test_intro",
            template_values={"plan_name": plan["name"]},
        )
        return True

    data = await state.get_data()
    final_price, note, _winning_code = _compute_final_price(plan_key, plan, callback.from_user.id, data)

    text = progress_bar(1, 3) + f"🛍 {plan['name']}\n💰 قیمت: {final_price:,} تومان"
    if note:
        text += note
    text += f"\n👛 موجودی کیف پول شما: {user['wallet']:,} تومان"
    _chosen = _chosen_name(data, plan_key)
    if _chosen:
        text += "\n" + str(ui_editor.get_text("plan_payment_service_name", "🔤 نام سرویس: {service_name}")).replace("{service_name}", _chosen)
    _is_free = int(final_price) <= 0
    text += ("\n\n🎁 این سفارش رایگان است؛ برای دریافت سرویس روی دکمه‌ی زیر بزنید." if _is_free else "\n\nروش پرداخت را انتخاب کنید:")

    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_payment_method", text,
                                 reply_markup=(free_claim_keyboard(plan_key) if _is_free else purchase_payment_keyboard(plan_key, show_discount=not note)))
    return True


@router.callback_query(F.data.startswith("buy_"))
async def buy_plan(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("buy_", "")
    # هر خرید جدید بدون نامِ مانده از خرید قبلی شروع می‌شود
    await state.update_data(purchase_config_name=None, purchase_config_name_plan=None)
    if _naming_applies(callback.from_user.id, plan_key) and db.is_orders_enabled() and db.get_effective_plan(plan_key) is not None \
            and db.get_user(callback.from_user.id) is not None:
        await state.update_data(purchase_naming_plan=plan_key)
        await state.set_state(UserStates.waiting_config_name)
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "config_name",
            ui_editor.get_text("config_name_prompt", "🔤 یک نام برای سرویس انتخاب کنید."),
            reply_markup=config_name_reply_keyboard(), ui_key="config_name_prompt",
        )
        await callback.answer()
        return
    await _show_plan_payment_methods(callback, state, plan_key)


async def _config_name_chosen(bot, chat_id: int, tg_user_id: int, state: FSMContext, plan_key: str, final_name: str):
    await state.update_data(purchase_config_name=final_name, purchase_config_name_plan=plan_key)
    await state.set_state(None)
    await _plan_payment_page(bot, chat_id, tg_user_id, state, plan_key)


async def _config_name_back(bot, chat_id: int, state: FSMContext, plan_key: str | None):
    await state.update_data(purchase_config_name=None, purchase_config_name_plan=None, purchase_naming_plan=None)
    await state.set_state(None)
    plan = db.get_effective_plan(plan_key) if plan_key else None
    cat = db.get_vip_category(plan.get("category_id")) if plan and plan.get("category_id") else None
    if cat:
        data = await state.get_data()
        await _render_category_plans(bot, chat_id, cat, cat["key"], data.get("discount_percent", 0))
    else:
        await _send_vip_categories(bot, chat_id)


@router.callback_query(F.data == "config_name_auto")
async def purchase_config_name_auto(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("purchase_naming_plan")
    if not plan_key:
        await callback.answer(ui_editor.get_alert_text("msg_80727cdf55", "❌ این پلن یافت نشد."), show_alert=True)
        return
    try:
        final_name = await _finalize_service_name(plan_key, _auto_service_name(callback), allow_short=True)
    except Exception as exc:
        await callback.answer(f"❌ ساخت نام یکتا ناموفق بود: {exc}", show_alert=True)
        return
    await callback.answer()
    await _config_name_chosen(callback.bot, callback.message.chat.id, callback.from_user.id, state, plan_key, final_name)


@router.callback_query(F.data == "config_name_back")
async def purchase_config_name_back(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer()
    await _config_name_back(callback.bot, callback.message.chat.id, state, data.get("purchase_naming_plan"))


@router.message(UserStates.waiting_config_name)
async def purchase_config_name_input(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    plan_key = data.get("purchase_naming_plan")
    if not plan_key:
        await state.set_state(None)
        await message.answer(ui_editor.get_text("msg_80727cdf55", "❌ این پلن یافت نشد."))
        return
    # اگر کاربر به‌جای تایپ نام، روی دکمه‌های منوی پایین صفحه زد، از مرحله‌ی نام‌گذاری خارج می‌شود
    # (منوی اصلی کار خودش را بکند و کاربر در این state گیر نکند).
    if raw in (
        db.get_text_override("config_name_back", "🔙 بازگشت به مرحله قبل"),
    ):
        await _config_name_back(message.bot, message.chat.id, state, plan_key)
        return
    _is_auto = raw == db.get_text_override("config_name_auto", "🤖 انتخاب خودکار نام")
    if _is_auto:
        raw = _auto_service_name(message)
    try:
        final_name = await _finalize_service_name(plan_key, raw, allow_short=_is_auto)
    except ValueError:
        await message.answer(
            str(ui_editor.get_text("config_name_invalid", "❌ نام نامعتبر است.")),
            reply_markup=config_name_reply_keyboard(),
        )
        return
    except Exception as exc:
        await message.answer(f"❌ نتوانستم نام یکتای سرویس را بسازم: {exc}", reply_markup=config_name_reply_keyboard())
        return
    await _config_name_chosen(message.bot, message.chat.id, message.from_user.id, state, plan_key, final_name)


# ✅ دکمه‌ی «انتخاب روش پرداخت دیگر» روی صفحه‌ی پرداخت کارت‌به‌کارت: فاکتور فعلی را
# منقضی می‌کند و دوباره صفحه‌ی انتخاب روش پرداخت را نمایش می‌دهد.
@router.callback_query(F.data.startswith("changepay_plan_"))
async def change_payment_method_plan(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("changepay_plan_", "")
    data = await state.get_data()
    invoice_id = data.get("card_invoice_id")
    if invoice_id:
        db.delete_invoice(invoice_id)
    await state.update_data(
        card_purchase_plan=None, card_purchase_price=None,
        card_purchase_discount_code=None, card_invoice_id=None, card_purchase_service_name=None,
    )
    await state.set_state(None)
    await _show_plan_payment_methods(callback, state, plan_key)


# ---------------------------------------------------------------------------
# پرداخت از کیف پول
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("pay_wallet_"))
async def pay_with_wallet(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("pay_wallet_", "")
    # 🎁 تست رایگان: اگر کاربر همین پلن تست را قبلاً گرفته، قبل از هر قفل/کول‌داون همان
    # پیام «قبلاً استفاده کرده‌اید» نشان داده می‌شود (نه «در حال پردازش»).
    if db.is_free_test_key(plan_key):
        _u = db.get_user(callback.from_user.id)
        if _u and db.has_used_free_test(_u["id"], plan_key):
            await callback.answer(
                ui_editor.get_text("msg_b5034a8263", "⚠️ شما قبلاً از «تست رایگان» استفاده کرده‌اید. هر کاربر فقط یک‌بار می‌تواند این پلن را دریافت کند."),
                show_alert=True,
            )
            return
    if is_duplicate_action(f"walletbuy_{callback.from_user.id}_{plan_key}") or not db.claim_purchase_action(f"tg-wallet:{callback.from_user.id}:{callback.message.message_id}:{plan_key}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        await callback.answer(ui_editor.get_alert_text("msg_80727cdf55", "❌ این پلن یافت نشد."), show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        await callback.answer(
            ui_editor.get_text("msg_b5034a8263", "⚠️ شما قبلاً از «تست رایگان» استفاده کرده‌اید. هر کاربر فقط یک‌بار می‌تواند این پلن را دریافت کند."),
            show_alert=True,
        )
        return

    data = await state.get_data()
    final_price, _note, winning_code = _compute_final_price(plan_key, plan, callback.from_user.id, data)

    if user["wallet"] < final_price:
        needed = final_price - user["wallet"]
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_pay_wallet", 
            f"❌ موجودی کیف پول کافی نیست!\n\n"
            f"💰 قیمت: {final_price:,} تومان\n"
            f"👛 موجودی: {user['wallet']:,} تومان\n"
            f"⚠️ کمبود: {needed:,} تومان",
            reply_markup=insufficient_balance_keyboard(),
        )
        await callback.answer()
        return

    success, referral_funded_purchase = db.deduct_wallet_for_purchase(user["id"], final_price, f"خرید {plan['name']}")
    if not success:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_pay_wallet", 
            "❌ موجودی کافی نیست. ممکن است موجودی شما تغییر کرده باشد.",
            reply_markup=insufficient_balance_keyboard(),
        )
        await callback.answer()
        return

    if winning_code:
        db.use_discount(winning_code, user["id"])

    if db.referral_purchase_qualifies(plan.get("volume_gb", 0), paid_purchase=True, is_free_test=db.is_free_test_key(plan_key)):
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    order_id = db.create_order(user["id"], plan_key, plan["name"], plan_type(plan_key), final_price, payment_method="کیف پول",
                               service_name=_chosen_name(data, plan_key))

    # ✅ ترتیب: اول استیکر (اگر تنظیم شده) + پیام «پرداخت موفق»، بعد تحویل/پیام‌های بعدی.
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_pay_wallet",
        WALLET_PAID_TEXT, reply_markup=_UM(inline_keyboard=[]),
    )

    # VIP و «تست رایگان»: اگر شاهراه فعال و برای این پلن (یا برای تست رایگان،
    # نگاشت سراسری‌اش) بسته‌ای نگاشت شده باشد، همین‌جا و بدون نیاز به هیچ
    handled = False
    if plan_type(plan_key) in ("vip", "test"):
        handled = await auto_fulfill_vip_via_panel(callback.bot, str(callback.from_user.id), plan_key, order_id)
        if handled:
            # فقط خریدی که تمام مبلغش از موجودی رفرالی تأمین شده، سرویس رفرالی محسوب می‌شود.
            if referral_funded_purchase:
                db.mark_latest_config_referral_funded(user["id"], "vip")

    if not handled:
        await callback.bot.send_message(
            ADMIN_ID,
            f"🛒 خرید جدید (کیف پول)!\n\n"
            f"👤 {callback.from_user.full_name}\n"
            f"🆔 {callback.from_user.id}\n"
            f"📦 {plan['name']}\n"
            f"💰 {final_price:,} تومان",
            reply_markup=admin_purchase_notify_keyboard(str(callback.from_user.id), plan_key, order_id),
        )
    await state.update_data(discount_percent=0, discount_code="", purchase_config_name=None, purchase_config_name_plan=None)
    await callback.answer()


# ---------------------------------------------------------------------------
# پرداخت آنلاین (درگاه یونیک‌پی — کارت‌به‌کارت با تایید خودکار)
# ---------------------------------------------------------------------------
ONLINE_PAID_TEXT = "✅ پرداخت شما تأیید شد و سفارش ثبت گردید. سرویس شما به‌زودی ارسال می‌شود."


async def _notify_online_paid(bot, payment: dict, sticker_key: str, text: str = ONLINE_PAID_TEXT):
    """اعلان پرداخت موفق آنلاین: اول استیکر (اگر تنظیم شده)، بعد پیام؛ قبل از تحویل سرویس."""
    try:
        await show_menu_with_sticker(
            bot, int(payment["telegram_id"]), sticker_key, text,
            reply_markup=_UM(inline_keyboard=[]), ui_key="payment_success",
        )
    except Exception:
        logger.exception("ارسال اعلان پرداخت آنلاین موفق ناموفق بود")


async def finalize_online_payment(bot, payment: dict) -> int | None:
    """اینوویس پرداخت‌شده‌ی یونیک‌پی را به یک سفارش واقعی تبدیل می‌کند و به
    ادمین اطلاع می‌دهد تا کانفیگ را ارسال کند.

    🐛 فیکس ریس‌کاندیشن: قبلاً idempotency فقط با یک if ساده روی payment
    ورودی چک می‌شد که چون هم پولر پس‌زمینه‌ی ربات و هم دکمه‌ی «بررسی پرداخت»
    (و در دیپلوی مینی‌اپ، endpoint جدای webapp_api.py) می‌توانند هم‌زمان این
    را صدا بزنند، امکان ساخت سفارش/سرویس تکراری برای یک پرداخت وجود داشت.
    حالا با db.claim_online_payment_for_finalize یک قفل اتمیک روی ردیف
    گرفته می‌شود؛ اگر فراخوانی دیگری برنده شده باشد، اینجا فقط None برمی‌گردد
    (کاری تکراری انجام نمی‌شود). اگر وسط کار خطا بیفتد، وضعیت به pending
    برمی‌گردد تا پولر بعدی دوباره تلاش کند."""
    if payment["status"] == "paid" and payment.get("order_id"):
        return payment["order_id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        # یعنی یک فراخوانی هم‌زمان دیگر (یا پولر، یا دکمه‌ی کاربر، یا مینی‌اپ)
        # همین الان دارد/داشت همین پرداخت را پردازش می‌کند؛ برای جلوگیری از
        # سفارش تکراری اینجا هیچ کاری نمی‌کنیم.
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid" and fresh.get("order_id"):
            return fresh["order_id"]
        return None

    try:
        try:
            _chosen_svc = (json.loads(payment.get("extra") or "{}") or {}).get("service_name")
        except Exception:
            _chosen_svc = None
        order_id = db.create_order(
            payment["user_id"], payment["plan_key"], payment["plan_name"],
            payment["order_type"], payment["price"], payment_method=og.method_label(payment),
            service_name=_chosen_svc,
        )
        db.mark_online_payment_paid(payment["id"], order_id)

        if payment.get("discount_code"):
            try:
                db.use_discount(payment["discount_code"], payment["user_id"])
            except Exception:
                logger.exception("خطا در مصرف کد تخفیف پس از پرداخت آنلاین")

        plan = db.get_effective_plan(payment["plan_key"]) if payment["plan_key"] else None
        if plan and db.referral_purchase_qualifies(plan.get("volume_gb", 0), paid_purchase=True, is_free_test=db.is_free_test_key(payment.get("plan_key"))):
            try:
                db.complete_referral(payment["user_id"])
            except ValueError:
                pass
    except Exception:
        # اگر وسط ساخت سفارش خطا بیفتد، claim را آزاد می‌کنیم تا دفعه‌ی بعد
        # (پولر یا کلیک مجدد کاربر) بتواند دوباره تلاش کند، نه اینکه پرداخت
        # برای همیشه در حالت processing گیر کند.
        db.set_online_payment_status(payment["id"], "pending")
        raise

    await _notify_online_paid(bot, payment, "plan_pay_online")
    handled = False
    if payment["plan_key"] and plan_type(payment["plan_key"]) in ("vip", "test"):
        handled = await auto_fulfill_vip_via_panel(bot, payment["telegram_id"], payment["plan_key"], order_id)

    if not handled:
        await bot.send_message(
            ADMIN_ID,
            f"🛒 خرید جدید ({og.admin_label(payment)})!\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"📦 {payment['plan_name']}\n"
            f"💰 {payment['price']:,} تومان",
            reply_markup=admin_purchase_notify_keyboard(payment["telegram_id"], payment["plan_key"], order_id),
        )
    return order_id


async def finalize_custom_online_payment(bot, payment: dict) -> int | None:
    """معادل finalize_online_payment، برای سفارش‌های «بساز سرویس خودت» که با
    یونیک‌پی پرداخت شده‌اند (حجم/مدت/نام در فیلد extra به‌صورت JSON ذخیره شده).
    🐛 همان فیکس ریس‌کاندیشن finalize_online_payment اینجا هم اعمال شده."""
    if payment["status"] == "paid" and payment.get("order_id"):
        return payment["order_id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid" and fresh.get("order_id"):
            return fresh["order_id"]
        return None

    extra = json.loads(payment.get("extra") or "{}")
    volume = extra.get("volume")
    days = extra.get("days")
    custom_name = extra.get("custom_name")
    order_type = extra.get("order_type", "new")
    target_config_id = extra.get("target_config_id")

    try:
        order_id = db.create_custom_order(
            payment["user_id"], volume, days, custom_name, payment["price"], order_type, target_config_id, renew_mode=extra.get("renew_mode"),
            payment_method=og.method_label(payment), option_num=extra.get("option_num") or 0,
        )
        db.set_custom_order_status(order_id, "paid")
        db.mark_online_payment_paid(payment["id"], order_id)

        if db.referral_purchase_qualifies(volume, paid_purchase=True, is_free_test=False):
            try:
                db.complete_referral(payment["user_id"])
            except ValueError:
                pass
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise

    user = db.get_user_by_id(payment["user_id"])
    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت)"

    await _notify_online_paid(bot, payment, "cbuild_pay_online")
    handled = False
    if user:
        handled = await auto_fulfill_custom_via_panel(bot, user, order_id, volume, days, custom_name)

    if not handled:
        await bot.send_message(
            ADMIN_ID,
            f"🛠 {label} ({og.admin_label(payment)})!\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"📦 حجم: {volume} گیگ\n⏳ مدت: {days} روز\n"
            + (f"🔤 نام: {custom_name}\n" if custom_name else "")
            + f"💰 {payment['price']:,} تومان\n🔢 شماره سفارش: {order_id}",
            reply_markup=admin_custom_order_notify_keyboard(order_id),
        )
    return order_id


@router.callback_query(F.data.startswith("pay_online_"))
async def pay_with_online(callback: types.CallbackQuery, state: FSMContext):
    if not _uq_enabled():
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return

    plan_key = callback.data.replace("pay_online_", "")
    if is_duplicate_action(f"onlinebuy_{callback.from_user.id}_{plan_key}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        await callback.answer(ui_editor.get_alert_text("msg_80727cdf55", "❌ این پلن یافت نشد."), show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        await callback.answer(
            ui_editor.get_text("msg_b5034a8263", "⚠️ شما قبلاً از «تست رایگان» استفاده کرده‌اید. هر کاربر فقط یک‌بار می‌تواند این پلن را دریافت کند."),
            show_alert=True,
        )
        return

    data = await state.get_data()
    final_price, _note, winning_code = _compute_final_price(plan_key, plan, callback.from_user.id, data)

    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))

    hash_id = uniquepay.new_hash_id("plan")
    invoice = await uniquepay.create_invoice(hash_id, final_price)
    if invoice is None:
        await alerts.report_uniquepay_create_failure(callback.bot, ADMIN_ID)
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_pay_online", 
            "❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست. لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید.",
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=False),
        )
        return

    payment_link = uniquepay_cfg.pick_link(invoice)
    if not payment_link:
        await alerts.report_uniquepay_create_failure(callback.bot, ADMIN_ID)
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_pay_online", 
            "❌ برای مبالِ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست. لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید.",
            reply_markup=purchase_payment_keyboard(plan_key, show_discount=False),
        )
        return

    alerts.report_uniquepay_create_success()

    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(callback.from_user.id),
        hash_id=hash_id,
        plan_name=plan["name"],
        price=final_price,
        order_type=plan_type(plan_key),
        plan_key=plan_key,
        discount_code=winning_code,
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
        extra=(json.dumps({"service_name": _chosen_name(data, plan_key)}, ensure_ascii=False) if _chosen_name(data, plan_key) else None),
    )

    white_label = uniquepay.white_label_data(invoice)
    payable_text = (
        f"💰 مبلغ دقیق واریز: {int(white_label['payableAmount']):,} تومان"
        if white_label and str(white_label['payableAmount']).isdigit()
        else f"💰 مبلغ سفارش: {final_price:,} تومان"
    )
    card_text = f"\n💳 شماره کارت مقصد: {white_label['cardNumber']}\n" if white_label else ""
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "plan_pay_online", 
        progress_bar(2, 3) +
        f"🌐 پرداخت آنلاین (کارت‌به‌کارت خودکار)\n\n"
        f"🛒 {plan['name']}\n"
        + payable_text + card_text + "\n"
        f"روی دکمه‌ی پرداخت بزنید یا از اطلاعات وایت‌لیبل بالا استفاده کنید؛ سپس همینجا روی «بررسی کن» بزنید.\n"
        f"⏱ به‌محض تأیید نهایی UniquePay، سفارش شما به‌طور خودکار ثبت می‌شود.\n\n"
        f"⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است. اگر تا این مهلت پرداخت تایید نشود، به‌طور خودکار منقضی و حذف خواهد شد.",
        reply_markup=online_payment_keyboard(payment_link, payment_id, white_label),
    )


@router.callback_query(F.data.startswith("checkpay_"))
async def check_online_payment(callback: types.CallbackQuery):
    try:
        payment_id = int(callback.data.replace("checkpay_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    payment = db.get_online_payment(payment_id)
    if payment is None:
        await callback.answer(
            ui_editor.get_text("msg_1a379a9932", "⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید."),
            show_alert=True,
        )
        return

    if str(callback.from_user.id) != payment["telegram_id"]:
        await callback.answer(ui_editor.get_alert_text("msg_69d5a4f257", "⛔️ این پرداخت متعلق به شما نیست."), show_alert=True)
        return

    if payment["status"] == "paid":
        await callback.answer(ui_editor.get_alert_text("msg_127225a077", "✅ این پرداخت قبلاً تأیید شده است."), show_alert=True)
        return

    if og.is_new_gateway(og.gateway_of(payment)):
        # درگاه‌های جدید (FrenzyEx/Tronado): استعلام/تحویل در handlers/gateways.py
        from handlers.gateways import check_for_user
        await check_for_user(callback, payment)
        return

    await callback.answer(ui_editor.get_alert_text("msg_ee96bfd443", "⏳ در حال بررسی وضعیت پرداخت..."))

    invoice = await uniquepay.check_invoice(payment["hash_id"])
    if not invoice or not invoice.get("isPaid") or not invoice.get("isVerified"):
        await callback.answer(
            ui_editor.get_text("msg_44b5b9f601", "⏳ هنوز پرداختی برای این اینوویس ثبت نشده. اگر همین الان پرداخت کردید،"
            " چند لحظه صبر کنید و دوباره بزنید."),
            show_alert=True,
        )
        return

    payment_kind = payment.get("kind")
    if payment_kind == "custom":
        result = await finalize_custom_online_payment(callback.bot, payment)
    elif payment_kind == "wallet_charge":
        from handlers.wallet import finalize_wallet_charge_online_payment
        result = await finalize_wallet_charge_online_payment(callback.bot, payment)
    else:
        result = await finalize_online_payment(callback.bot, payment)

    if result is None:
        # یعنی هم‌زمان یک فراخوانی دیگر (مثلاً پولر پس‌زمینه یا مینی‌اپ) در
        # حال پردازش همین پرداخت است؛ برای جلوگیری از سفارش تکراری اینجا کار
        # دیگری انجام نمی‌دهیم، فقط پیام موفقیت را نشان می‌دهیم.
        pass



# ---------------------------------------------------------------------------
# پرداخت کارت به کارت
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("pay_card_"))
async def pay_with_card(callback: types.CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("pay_card_", "")
    plan = db.get_effective_plan(plan_key)
    if plan is None:
        await callback.answer(ui_editor.get_alert_text("msg_80727cdf55", "❌ این پلن یافت نشد."), show_alert=True)
        return

    if db.is_free_test_key(plan_key):
        user = db.get_user(callback.from_user.id)
        if user and db.has_used_free_test(user["id"], plan_key):
            await callback.answer(
                ui_editor.get_text("msg_b5034a8263", "⚠️ شما قبلاً از «تست رایگان» استفاده کرده‌اید. هر کاربر فقط یک‌بار می‌تواند این پلن را دریافت کند."),
                show_alert=True,
            )
            return

    data = await state.get_data()
    final_price, _note, winning_code = _compute_final_price(plan_key, plan, callback.from_user.id, data)
    # 🎲 برای پیشگیری از مسدودی کارت به‌خاطر واریزی‌های زیاد با مبلغ یکسان،
    # مبلغ نهایی این فاکتور را کمی رندوم (± ۱۰۰ تا ۱۵۰۰ تومان) می‌کنیم.
    final_price = apply_random_amount_variation(final_price)

    invoicing_user = db.get_user(callback.from_user.id)
    invoice = db.create_invoice(
        user_id=invoicing_user["id"] if invoicing_user else None,
        telegram_id=str(callback.from_user.id),
        kind="plan_card",
        label=plan["name"],
        price=final_price,
    )
    deadline_str = format_deadline_time(invoice["expires_at"])

    await state.update_data(
        card_purchase_plan=plan_key, card_purchase_price=final_price, card_purchase_discount_code=winning_code,
        card_invoice_id=invoice["id"], card_purchase_service_name=_chosen_name(data, plan_key),
    )
    await state.set_state(UserStates.waiting_card_purchase_receipt)

    invoice_values = {
        "plan_name": plan["name"],
        "amount": final_price,
        "card_number": bot_info.get("card_number"),
        "card_holder": bot_info.get("card_holder"),
        "deadline": deadline_str,
    }
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "plan_pay_card",
        progress_bar(2, 3) +
        "\n\n💳 پرداخت کارت به کارت\n\n"
        "🛒 {plan_name}\n💰 مبلغ قابل پرداخت: {amount:,} تومان\n\n"
        "💳 شماره کارت:\n{card_number}\n\n👤 به نام: {card_holder}\n\n"
        "📸 پس از واریز، عکس رسید پرداخت یا 📝 متن رسید را همینجا ارسال کنید.\n\n"
        "⏱ این شماره کارت و قیمت تا ساعت {deadline} (۳۰ دقیقه) معتبر است. لطفاً تا این ساعت رسید پرداخت را ارسال کنید، وگرنه این فاکتور به‌طور خودکار منقضی و حذف می‌شود.",
        template_values=invoice_values,
        ui_key="invoice_plan_card",
        append_note=invoice_expiry_note("card", deadline_str),
        reply_markup=card_payment_actions_keyboard(bot_info.get("card_number"), final_price, f"changepay_plan_{plan_key}"),
    )
    await callback.answer()


@router.message(UserStates.waiting_card_purchase_receipt, F.photo | F.document | F.text)
async def receive_purchase_receipt(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    data = await state.get_data()
    plan_key = data.get("card_purchase_plan")
    final_price = data.get("card_purchase_price")
    winning_code = data.get("card_purchase_discount_code")
    invoice_id = data.get("card_invoice_id")
    plan = db.get_effective_plan(plan_key)

    file_id, file_type, receipt_text = extract_receipt_media(message)
    # متن رسید (مثلاً شماره پیگیری/توضیح) هم مجاز است؛ ولی یک متن خیلی کوتاه رسید حساب نمی‌شود.
    if file_id is None and len(receipt_text) < 4:
        await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))
        return

    if plan is None or final_price is None:
        await message.answer(ui_editor.get_text("msg_82c9780032", "❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس‌ها شروع کنید."))
        await state.clear()
        return

    if not invoice_id or db.consume_invoice(invoice_id) is None:
        await message.answer(
            ui_editor.get_text("msg_1a379a9932", "⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید.")
        )
        await state.clear()
        return

    user = db.get_user(uid)
    # 🐛 فیکس: کد تخفیف قبلاً همین‌جا (قبل از تأیید ادمین) مصرف می‌شد؛ یعنی اگر ادمین رسید
    # را رد می‌کرد، سهم کد تخفیف به کاربر برنمی‌گردد. حالا مانند مینی‌اپ، کد تخفیف
    # فقط همراه رسید ذخیره می‌شود و در approve_purchase (handlers/admin.py) مصرف خواهد شد.

    receipt_id = None
    try:
        receipt_id = db.create_pending_receipt(
            "plan_card", uid, user["id"] if user else None, plan["name"], final_price,
            extra=plan_key, plan_key=plan_key, discount_code=winning_code,
            file_id=file_id, file_type=file_type, note=(receipt_text or None),
            service_name=data.get("card_purchase_service_name"),
        )
    except Exception:
        logger.exception("ثبت رسید کارت‌به‌کارت ناموفق بود")

    if invoice_id:
        db.delete_invoice(invoice_id)

    tracking_code = receipt_id if receipt_id is not None else invoice_id
    admin_caption = (
        f"💳 رسید خرید کارت‌به‌کارت\n\n"
        f"👤 {receipt_user_label(message.from_user)}\n"
        f"🆔 {uid}\n"
        f"📦 {plan['name']}\n"
        f"💰 {final_price:,} تومان\n"
        + (f"🔤 نام سرویس: {data.get('card_purchase_service_name')}\n" if data.get("card_purchase_service_name") else "")
        + f"🧾 کد پیگیری: {tracking_code}"
        + (f"\n📝 متن رسید: {receipt_text}" if receipt_text and file_id is None else "")
    )
    if receipt_id is not None:
        try:
            db.set_pending_receipt_media(receipt_id, details=admin_caption)
        except Exception:
            logger.exception("ذخیره‌ی توضیح رسید ناموفق بود")
    delivered = await send_receipt_card_to_admin(
        message.bot, admin_caption,
        reply_markup=admin_purchase_card_approval_keyboard(uid, plan_key, final_price, receipt_id),
        file_id=file_id, file_type=file_type,
    )
    if not delivered:
        logger.error("رسید %s برای ادمین ارسال نشد؛ فقط در صف رسیدها موجود است", tracking_code)
    await message.answer(
        f"✅ رسید پرداخت با موفقیت دریافت شد\n\n"
        f"🧾 کد پیگیری: `{tracking_code}`\n"
        f"📦سرویس: {plan['name']}\n"
        f"💳 مبلغ: {final_price:,} تومان\n"
        f"🟡 وضعیت: در حال بررسی\n\n"
        f"رسید شما ثبت شده و نیازی به ارسال مجدد آن نیست.\n\n"
        f"پس از تأیید پرداخت، لینک سرویس و آموزش اتصال از طریق همین ربات برایت ارسال می‌شود.",
        parse_mode="Markdown",
        reply_markup=receipt_submitted_keyboard(),
    )
    await state.update_data(
        discount_percent=0, discount_code="", card_purchase_plan=None,
        card_purchase_price=None, card_purchase_discount_code=None, card_invoice_id=None,
        card_purchase_service_name=None, purchase_config_name=None, purchase_config_name_plan=None,
    )
    await state.set_state(None)


@router.message(UserStates.waiting_card_purchase_receipt)
async def purchase_receipt_wrong_format(message: types.Message):
    await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))


# ---------------------------------------------------------------------------
# سرویس‌های من
#
# 🛠 فیکس UX: قبلاً زدن روی «📱 سرویس‌های من» یک منوی میانی («کدوم دسته رو
# می‌خوای ببینی؟») نشون می‌داد که فقط یک گزینه‌ی واحد («🚀 سرویس‌های VIP من»)
# داشت — یعنی کاربر همیشه مجبور بود یک کلیک اضافه و بی‌فایده بزنه. الان
# «my_configs» مستقیماً همون چیزی رو نشون می‌ده که قبلاً «my_configs_vip»
# نشون می‌داد؛ «my_configs_vip» هم به‌عنوان alias نگه داشته شده تا دکمه‌های
# بازگشتِ صفحه‌ی جزئیات سرویس (که هنوز به این نام ارجاع می‌دن) خراب نشن.
# ---------------------------------------------------------------------------
async def _show_my_services_list(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    configs = [c for c in db.get_configs(user["id"]) if not is_config_expired(c)]
    if not configs:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "my_configs_list_empty", 
            "شما هنوز هیچ سرویس فعالی ندارید.",
            reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="my_configs_list_empty"),
        )
    else:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "my_configs_list_has", 
            "سرویس‌های شما\n\nبرای مشاهده‌ی لینک سابسکریپشن و مدیریت هرکدام، روی نام آن بزنید 👇",
            reply_markup=my_configs_list_keyboard(configs, "", "back", show_search=True),
        )
    await callback.answer()


@router.callback_query(F.data == "my_configs")
async def my_configs(callback: types.CallbackQuery):
    await _show_my_services_list(callback)


@router.callback_query(F.data == "my_configs_vip")
async def my_configs_vip(callback: types.CallbackQuery):
    await _show_my_services_list(callback)


@router.callback_query(F.data == "service_search")
async def service_search_start(callback: types.CallbackQuery, state: FSMContext):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    await state.set_state(UserStates.waiting_service_search)
    await callback.message.answer("🔎 اسم یا بخشی از اسم سرویسی که دنبالشی رو بفرست:")
    await callback.answer()


@router.message(UserStates.waiting_service_search)
async def service_search_input(message: types.Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    await state.clear()
    if user is None:
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ یه متن برای جستجو بفرست.")
        return
    results = db.search_user_configs(user["id"], query)
    if not results:
        await message.answer(
            f"❌ هیچ سرویسی با «{query}» پیدا نشد.",
            reply_markup=service_search_results_keyboard([], query),
        )
        return
    await message.answer(
        f"🔎 {len(results)} نتیجه برای «{query}»:\n\nبرای مدیریت هرکدام روی نامش بزن 👇",
        reply_markup=service_search_results_keyboard(results, query),
    )



def _persian_digits(value) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _parse_any_datetime(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", s):
            return _parse_any_datetime(float(s))
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _tehran_display(value) -> str:
    dt = _parse_any_datetime(value)
    if not dt:
        return str(value) if value not in (None, "") else "نامشخص"
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo("Asia/Tehran"))
    except Exception:
        pass
    return _persian_digits(to_jalali_str(dt, with_time=True))


def _connection_vars(live) -> dict:
    """متغیرهای اتصال برای «جزئیات سرویس»: آخرین اتصال، اپ/دستگاه، آنلاین‌بودن، تعداد دستگاه‌ها…
    هر مقدار فقط وقتی «نامشخص» است که پنل واقعاً آن را نداده باشد."""
    out = {"last_connection": "نامشخص", "last_app": "نامشخص", "last_device": "نامشخص", "online_status": "نامشخص",
           "last_sub_fetch": "نامشخص", "devices_count": "نامشخص", "devices_limit": "نامشخص",
           "days_left": "نامشخص"}
    try:
        if not live:
            return out
        from webapp_user_ext import build_connection
        conn = build_connection(live)
        if conn.get("online_at"):
            out["last_connection"] = _tehran_display(conn["online_at"])
            out["online_status"] = "🟢 آنلاین" if conn.get("is_online") else "⚪ آفلاین"
        app = (conn.get("app") or {}).get("name")
        if app:
            out["last_app"] = str(app)
        devs = conn.get("devices") or []
        if devs:
            d0 = devs[0]
            out["last_device"] = " ".join(x for x in (str(d0.get("name") or ""), str(d0.get("platform") or "")) if x).strip() or "نامشخص"
        elif (conn.get("app") or {}).get("device"):
            out["last_device"] = str(conn["app"]["device"])
        elif conn.get("user_agent"):
            out["last_device"] = str(conn["user_agent"])[:40]
        if conn.get("last_sub_fetch_at"):
            out["last_sub_fetch"] = _tehran_display(conn["last_sub_fetch_at"])
        # تعداد دستگاه: اگر پنل لیست دستگاه‌ها را داد (حتی خالی) عدد واقعی است، وگرنه نامشخص
        if devs or conn.get("devices_known"):
            out["devices_count"] = _persian_digits(len(devs))
        if conn.get("devices_limit") is not None:
            out["devices_limit"] = _persian_digits(conn["devices_limit"]) if conn["devices_limit"] else "نامحدود"
        from webapp_user_ext import to_epoch as _to_epoch
        exp = _to_epoch(live.get("expire"))
        if exp:
            import time as _t
            out["days_left"] = _persian_digits(max(0, int((exp - _t.time()) // 86400))) + " روز"
    except Exception:
        logger.debug("connection vars failed", exc_info=True)
    return out


def _product_name_from_config(cfg: dict) -> str:
    """نام محصول را به فرمت فروشگاه برمی‌گرداند: «۳۰ گیگ | ۱ ماهه کاربر ∞».
    برای رکوردهای قدیمی که product_name فقط «۵ گیگ» بوده، از plan ذخیره‌شده
    حجم/مدت را استخراج می‌کنیم تا خروجی قدیمی هم یکدست شود."""
    explicit = (cfg.get("product_name") or "").strip()
    if explicit and "|" in explicit and ("ماه" in explicit or "روز" in explicit):
        return explicit
    raw = str(cfg.get("plan") or "").strip()
    parts = [x.strip() for x in raw.split("|")]
    volume = None
    days = None
    if len(parts) >= 2:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*گیگ", parts[1])
        if m:
            volume = float(m.group(1))
    if len(parts) >= 3:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*روز", parts[2])
        if m:
            days = float(m.group(1))
    if volume is None:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*گیگ", explicit)
        if m:
            volume = float(m.group(1))
    if volume is not None:
        volume_text = str(int(volume)) if float(volume).is_integer() else str(volume).rstrip("0").rstrip(".")
        if days is not None and days > 0:
            if float(days) % 30 == 0:
                months = days / 30
                duration = f"{int(months) if float(months).is_integer() else months:g} ماهه"
            else:
                duration = f"{int(days) if float(days).is_integer() else days:g} روزه"
        else:
            duration = "زمان ∞"
        try:
            _pl = db.get_vip_plan(cfg.get("plan_key")) if cfg.get("plan_key") else None
            _ul = int((_pl or {}).get("user_limit") or 0)
        except Exception:
            _ul = 0
        return f"{volume_text} گیگ | {duration} " + (f"{_ul} کاربر" if _ul > 0 else "کاربر ∞")
    return explicit or (parts[1] if len(parts) > 1 else raw) or "نامشخص"

_DISABLED_WORDS = {"disabled", "disable", "inactive", "deactivated", "suspended", "paused", "blocked", "banned", "stopped", "off", "غیرفعال"}
_EXPIRED_WORDS = {"expired", "limited", "exhausted", "finished", "ended", "depleted", "منقضی"}
_ACTIVE_WORDS = {"active", "enabled", "enable", "on", "ok", "online", "running", "فعال"}


def _derive_status(live: dict | None) -> str | None:
    """وضعیت واقعی سرویس را از پاسخ پنل درمی‌آورد: رشته‌ی status/state یا پرچم‌های بولی (enabled/isActive/disabled…)."""
    if not live:
        return None
    st = str(live.get("status") or "").strip().lower()
    if st:
        return st
    raw = live.get("raw") if isinstance(live.get("raw"), dict) else {}
    nodes = [raw] + [raw.get(k) for k in ("service", "user", "data") if isinstance(raw.get(k), dict)]
    for node in nodes:
        for k in ("state", "serviceStatus", "service_status"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
        for k in ("enabled", "isEnabled", "is_enabled", "active", "isActive", "is_active"):
            if isinstance(node.get(k), bool):
                return "active" if node[k] else "disabled"
        for k in ("disabled", "isDisabled", "is_disabled", "suspended", "isSuspended", "paused", "blocked"):
            if isinstance(node.get(k), bool):
                return "disabled" if node[k] else "active"
    return None


def _status_is_disabled(live: dict | None):
    """True/False اگر وضعیت فعال/غیرفعال از پاسخ پنل قابل تشخیص باشد؛ وگرنه None."""
    st = _derive_status(live)
    if not st:
        return None
    if st in _DISABLED_WORDS:
        return True
    if st in _ACTIVE_WORDS or st in _EXPIRED_WORDS:
        return False
    return None


def _service_status_text(usage: dict | None) -> str:
    if not usage:
        return "⚪️ نامشخص"
    total, used = usage.get("total"), usage.get("used")
    try:
        if total and used is not None and float(used) >= float(total):
            return "❌ منقضی شده (اتمام حجم)"
    except Exception:
        pass
    exp = usage.get("expire")
    try:
        if exp and float(exp) <= datetime.now(tz=timezone.utc).timestamp():
            return "❌ منقضی شده (اتمام زمان)"
    except Exception:
        pass
    status = str(usage.get("status") or "").lower()
    if status in _DISABLED_WORDS:
        return "⛔ غیرفعال شده"
    if status in _EXPIRED_WORDS:
        return "❌ منقضی شده"
    return "✅ فعال"


def _service_usage_bar(percent: int) -> str:
    percent = min(100, max(0, int(percent)))
    filled = round(percent / 10)
    return "🟩" * filled + "⬜️" * (10 - filled) + f" {_persian_digits(percent)}٪ مصرف شده"


_MANUAL_LINK_MISS: dict[int, float] = {}


async def _auto_link_manual_config(cfg: dict) -> bool:
    """سرویس‌های «ارسال دستی» (source=manual) پنل ندارند؛ لینک ساب را به کاربر واقعی پنل وصل می‌کنیم
    تا نام، مصرف، اتصال، دستگاه‌ها و تغییر لینک از خود پنل میرور شود. نتیجه در دیتابیس ذخیره می‌شود."""
    import time as _t
    try:
        if cfg.get("source") != "manual" or cfg.get("panel_id") or cfg.get("service_id"):
            return False
        cid = int(cfg["id"])
        if _t.time() - _MANUAL_LINK_MISS.get(cid, 0) < 600:
            return False
        link = crypto.decrypt_config(cfg["config"])
        res = await asyncio.wait_for(panels.resolve_manual_link(link), timeout=15.0)
        if not res:
            _MANUAL_LINK_MISS[cid] = _t.time()
            return False
        db.link_config_to_panel(cid, res["panel"]["id"], res["service_id"], res["panel_type"], service_name=res["username"])
        cfg.update({"panel_id": res["panel"]["id"], "service_id": res["service_id"],
                    "source": res["panel_type"], "service_name": res["username"]})
        return True
    except Exception:
        logger.debug("auto-link manual config failed cfg=%s", cfg.get("id"), exc_info=True)
        try:
            _MANUAL_LINK_MISS[int(cfg["id"])] = __import__("time").time()
        except Exception:
            pass
        return False


async def _live_service_info(cfg: dict):
    """منبع اطلاعات سرویس: شاهراه از API خودش و مرزبان/پاسارگارد مستقیماً از پنل.
    فقط برای شاهراه، استخراج کانفیگ از لینک ساب مجاز است."""
    panel = db.get_vpn_panel(cfg.get("panel_id")) if cfg.get("panel_id") else None
    if not panel or not cfg.get("service_id") or cfg.get("source") not in panels.PANEL_TYPES:
        return None, None, None
    ok, snap, msg = await panels.get_service_snapshot(panel, cfg["service_id"])
    if not ok or not snap:
        return panel, None, msg
    return panel, snap, None


def _service_expiry_text(usage, cfg: dict, panel_managed_hint: bool = False) -> str:
    """تاریخ اتمام سرویس: اگر زمان مشخصی باشد شمسی همراه با ساعت (به وقت تهران)؛
    اگر نامحدود باشد «نامحدود». اولویت با داده‌ی زنده‌ی پنل (یا هدر لینک ساب)، سپس تاریخ ذخیره‌شده."""
    if usage is not None:
        exp = usage.get("expire")
        try:
            numeric = float(exp) if exp not in (None, "") else 0.0
        except (TypeError, ValueError):
            numeric = 1.0  # رشته‌ی تاریخ
        has_exp = not (exp in (None, "") or numeric <= 0)
        if has_exp:
            if _parse_any_datetime(exp):
                return _tehran_display(exp)
            return str(exp)
        # هدر لینک ساب تاریخی نداشت ولی برای سرویس تاریخ ثبت شده است → از همان استفاده کن
        if not (usage.get("_from_sub") and cfg.get("expiry")):
            return "نامحدود"
    stored = cfg.get("expiry")
    if stored:
        try:
            dt = datetime.strptime(str(stored)[:10], "%Y-%m-%d")
            return _persian_digits(to_jalali_str(dt, with_time=False))
        except Exception:
            return str(stored)
    return "نامشخص" if panel_managed_hint else "نامحدود"


# پنل‌هایی که در panels.py واقعاً فعال/غیرفعال‌سازی و تغییر لینک دارند
_TOGGLE_PANEL_TYPES = {"shahrah", "marzban", "pasargad", "pasargad_wg", "threexui"}
_REGEN_PANEL_TYPES = {"marzban", "pasargad", "pasargad_wg", "threexui"}


async def _verify_panel_toggle(panel, service_id, want_disabled: bool):
    """بعد از فعال/غیرفعال‌سازی، وضعیت را دوباره از خود پنل می‌خواند.
    (True, None) اگر اعمال شده یا وضعیت قابل‌تشخیص نیست؛ (False, پیام) اگر پنل هنوز وضعیت قبلی را دارد."""
    try:
        ok, snap, _m = await panels.get_service_snapshot(panel, service_id)
        if not ok or not snap:
            return True, None
        now_disabled = _status_is_disabled(snap)
        if now_disabled is None or now_disabled == want_disabled:
            return True, None
        return False, "پنل دستور را پذیرفت ولی وضعیت سرویس در پنل تغییر نکرد."
    except Exception:
        logger.debug("verify toggle failed", exc_info=True)
        return True, None


@router.callback_query(F.data.startswith("viewconfig_"))
async def view_config(callback: types.CallbackQuery, cfg_id: int | None = None, toast: str | None = None):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    if cfg_id is None:
        try:
            cfg_id = int(callback.data.replace("viewconfig_", ""))
        except ValueError:
            await callback.answer(ui_editor.get_alert_text("msg_d3f440f81a", "❌ سرویس یافت نشد."), show_alert=True)
            return

    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return

    # پاسخ callback را قبل از تماس شبکه‌ای بده تا Telegram فوراً کلیک را تأیید کند
    # و کاربر حس نکند ربات هنگ کرده است. در نسخهٔ قبلی این await بعد از دریافت پنل
    # بود و کندی خود پنل مستقیماً به تأخیر رابط ربات منتقل می‌شد.
    try:
        await callback.answer(toast or "⏳ در حال دریافت اطلاعات سرویس...")
    except Exception:
        pass

    await _auto_link_manual_config(cfg)
    try:
        panel, live, live_error = await asyncio.wait_for(_live_service_info(cfg), timeout=8.0)
    except asyncio.TimeoutError:
        panel = db.get_vpn_panel(cfg.get("panel_id")) if cfg.get("panel_id") else None
        live, live_error = None, "timeout"
    except Exception:
        logger.exception("خطا/timeout در دریافت اطلاعات زنده سرویس cfg_id=%s", cfg_id)
        panel, live, live_error = db.get_vpn_panel(cfg.get("panel_id")) if cfg.get("panel_id") else None, None, "error"

    # لینک ذخیره‌شده همان لینکی است که هنگام تحویل به کاربر ثبت شده. تغییر حجم
    # رفرالی نباید باعث جایگزین‌شدن لینک دیگری شود؛ حتی اگر GET پنل URL متفاوتی برگرداند.
    sub_url = None
    try:
        stored_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        stored_link = None
    if stored_link and stored_link.lower().startswith(("http://", "https://")):
        sub_url = stored_link
    elif live:
        raw_link = live.get("link")
        if isinstance(raw_link, str) and raw_link.lower().startswith(("http://", "https://")):
            sub_url = raw_link
    decrypted = sub_url if sub_url else None

    try:
        usage = None
        if live and panel:
            usage = {
                "total": live.get("total"),
                "upload": live.get("upload") or 0,
                "download": live.get("download") or 0,
                "used": live.get("used"),
                "expire": live.get("expire"),
                "status": _derive_status(live),
            }
        elif decrypted:
            # سرویسی که پنل زنده ندارد (ارسال دستی/لینک خارجی): همه‌ی اطلاعات از هدرهای خود لینک ساب
            # (Subscription-Userinfo: upload/download/total/expire) میرور می‌شود، نه فقط برای شاهراه.
            try:
                usage = await fetch_subscription_info(decrypted)
            except Exception:
                logger.exception("خطای دریافت مصرف از لینک ساب برای cfg_id=%s", cfg_id)
            if usage is not None:
                usage["_from_sub"] = True
                # expire=0 / total=0 در هدر یعنی نامحدود
                if not usage.get("expire"):
                    usage["expire"] = None
                if not usage.get("total"):
                    usage["total"] = None

        if usage:
            total = usage.get("total")
            def _num(v):
                try:
                    return int(float(v)) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None
            total = _num(total)
            used = _num(usage.get("used"))
            if used is None:
                used = (_num(usage.get("upload")) or 0) + (_num(usage.get("download")) or 0)
            usage["used"] = used
        else:
            total = used = None

        remaining = None
        if total is not None and used is not None:
            try:
                remaining = max(0, int(total) - int(used))
            except Exception:
                pass

        percent = min(100, round((used or 0) / total * 100)) if total else 0
        traffic_text = format_bytes(total) if total else "نامحدود"
        used_text = format_bytes(used or 0)
        remaining_text = format_bytes(remaining) if remaining is not None else "نامشخص"
        usage_bar_text = _service_usage_bar(percent) if total else "⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️ ۰٪ مصرف شده"

        expiry_text = _service_expiry_text(usage, cfg, panel_managed_hint=bool(cfg.get("panel_id")))

        last_connection = _connection_vars(live).get("last_connection", "نامشخص")
        service_name = live.get("username") if live else None
        if sub_url:
            try:
                # اگر پنل username را برنگرداند، Profile-Title خود لینک ساب را
                # به‌عنوان مرجع نام سرویس می‌گیریم؛ این باعث می‌شود نام نمایش‌داده‌شده
                # دقیقاً با همان لینک تحویل‌شده mirror باشد.
                if not service_name:
                    meta = await extract_meta(sub_url)
                    service_name = (meta or {}).get("name")
            except Exception:
                pass
        service_name = service_name or cfg.get("service_id") or "نامشخص"
        panel_name = (panel.get("name") if panel else None) or "نامشخص"
        location_text = f"مولتی لوکیشن | {panel_name}"
        product_name = _product_name_from_config(cfg)

        fallback_detail = (
            "📊 وضعیت سرویس : {status}\n"
            "👤 نام سرویس : {service_name}\n\n"
            "🌍 موقعیت سرویس : {location} 🌿\n"
            "📂 نام محصول : {product}\n\n"
            "🔋 ترافیک : {traffic}\n"
            "☑️ حجم مصرفی : {used}\n"
            "💢 حجم باقی مانده : {remaining}\n"
            "{usage_bar}\n\n"
            "🗓 تاریخ اتمام : {expiry}"
        )
        text, text_entities = ui_editor.render_template(
            "config_detail",
            {
                "status": _service_status_text(usage),
                "service_name": service_name,
                "location": location_text,
                "product": product_name,
                "traffic": traffic_text,
                "used": used_text,
                "remaining": remaining_text,
                "usage_bar": usage_bar_text,
                "expiry": expiry_text,
                "last_connection": last_connection,
                **_connection_vars(live),
            },
            fallback=fallback_detail,
        )

        _ptype = (panel or {}).get("panel_type") or cfg.get("source")
        panel_managed = bool(cfg.get("source") in panels.PANEL_TYPES and cfg.get("service_id") and cfg.get("panel_id"))
        # فقط پنل‌هایی که فعال/غیرفعال‌سازی واقعی دارند دکمه‌ی مدیریت می‌گیرند (بقیه «پشتیبانی نشده» می‌دهند).
        can_manage_link = bool(panel_managed and _ptype in _TOGGLE_PANEL_TYPES)
        can_change_link = bool(panel_managed and _ptype in _REGEN_PANEL_TYPES)
        panel_direct_configs = bool(panel_managed and panel and panel.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"))
        # وضعیت فعال/غیرفعال را با پنل همگام کن (اگر از خود پنل یا جای دیگری تغییر کرده باشد)
        _sub_disabled = bool(cfg.get("sub_link_disabled"))
        if can_manage_link and live:
            _pd = _status_is_disabled(live)
            if _pd is not None and _pd != _sub_disabled:
                _sub_disabled = _pd
                try:
                    db.set_config_link_disabled(cfg_id, _pd)
                except Exception:
                    logger.exception("sync sub_link_disabled failed cfg_id=%s", cfg_id)
        kb = config_detail_keyboard(
            cfg_id, sub_link_url=sub_url, has_qr=bool(cfg.get("qr_file_id")),
            sub_link_disabled=_sub_disabled, can_manage_link=can_manage_link, can_change_link=can_change_link,
            panel_direct_configs=panel_direct_configs, allow_renew=db.config_allows_renew(cfg),
            is_wireguard=bool(panel and panels.needs_wg_delivery(panel)),
        )
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "config_detail",
            text, parse_mode=None, reply_markup=kb,
            ui_key="config_detail",
            template_values={
                "status": _service_status_text(usage),
                "service_name": service_name,
                "location": location_text,
                "product": product_name,
                "traffic": traffic_text,
                "used": used_text,
                "remaining": remaining_text,
                "usage_bar": usage_bar_text,
                "expiry": expiry_text,
                "last_connection": last_connection,
                **_connection_vars(live),
            },
        )
    except Exception:
        logger.exception("خطای کلی در نمایش جزئیات سرویس برای cfg_id=%s", cfg_id)
        try:
            await callback.message.answer(
                ui_editor.get_text("msg_5974ab0abf", "❌ خطا در نمایش جزئیات سرویس. لطفاً دوباره تلاش کنید."),
                reply_markup=back_button("my_configs_vip", "🔙 بازگشت", screen="config_detail"),
            )
        except Exception:
            logger.exception("خطا در fallback جزئیات سرویس")

@router.callback_query(F.data.startswith("usersvclink_"))
async def user_change_sublink_warn(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    cfg_id = int(callback.data.replace("usersvclink_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not user or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return
    await callback.message.answer(
        ui_editor.get_text("msg_d500a5a42e", "⚠️ تغییر لینک ساب\n\n"
        "اگر فکر می‌کنید کسی به سرویس شما دسترسی دارد، با تغییر لینک ساب می‌توانید دسترسی دیگران را قطع کنید.\n\n"
        "توجه: بعد از تغییر، لینک ساب قبلی شما دیگر کار نخواهد کرد و باید از لینک جدید استفاده کنید."),
        reply_markup=confirm_change_sublink_keyboard(cfg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("usersvclinkconfirm_"))
async def user_change_sublink_confirm(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    cfg_id = int(callback.data.replace("usersvclinkconfirm_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not user or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return
    if not cfg.get("panel_id") or not cfg.get("service_id") or cfg.get("source") not in panels.PANEL_TYPES:
        await callback.answer(ui_editor.get_alert_text("msg_1ce27b2ff2", "❌ تغییر خودکار لینک برای این سرویس فعال نیست؛ با پشتیبانی تماس بگیر."), show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer(ui_editor.get_alert_text("msg_1fd738597f", "❌ نمونه پنل پیدا نشد."), show_alert=True)
        return
    await callback.answer(ui_editor.get_alert_text("msg_ca813dec9c", "⏳ در حال ساخت لینک جدید..."))
    try:
        ok, new_link, new_service_id, raw_data, msg = await panels.regenerate_sub_link(panel, cfg["service_id"])
    except Exception as exc:
        logger.exception("regenerate_sub_link crashed cfg_id=%s", cfg_id)
        ok, new_link, new_service_id, raw_data, msg = False, None, None, None, str(exc)
    if not ok or not new_link:
        reason = (msg or "").strip()
        await callback.message.answer(
            ui_editor.get_text("msg_821c2bcf5d", "⚠️ ساخت خودکار لینک جدید ممکن نشد. لطفاً برای تغییر لینک ساب با پشتیبانی تماس بگیرید.")
            + (f"\n\nعلت: {reason}" if reason else ""),
            reply_markup=back_button(f"viewconfig_{cfg_id}", "🔙 بازگشت", screen="config_detail"),
        )
        return
    # فقط لینک عوض می‌شود: created_at/تاریخ‌ها دست نمی‌خورند و QR قدیمی (مربوط به لینک باطل‌شده) پاک می‌شود.
    db.replace_config_link(
        cfg_id, crypto.encrypt_config(new_link),
        new_service_id if (new_service_id and new_service_id != cfg.get("service_id")) else None,
    )
    db.set_config_link_disabled(cfg_id, False)
    _same = (msg == "SAME_LINK_CREDS_ROTATED")
    await callback.message.answer(
        ("✅ کلیدهای اتصال سرویس شما در پنل عوض شد و دسترسی کانفیگ‌های قبلی (هر فرد دیگری) قطع شد.\n\n"
         "ℹ️ آدرس لینک ساب در این پنل ثابت است و تغییر نمی‌کند؛ فقط کافی است در برنامه‌تان یک‌بار «آپدیت اشتراک» بزنید.")
        if _same else
        ui_editor.get_text("msg_62b8e2ca17", "✅ لینک ساب شما با موفقیت تغییر کرد و دسترسی هر فرد دیگری قطع شد."),
        reply_markup=back_button(f"viewconfig_{cfg_id}", "🔙 بازگشت به سرویس", screen="config_detail"),
    )
    # لینک جدید + QR جدید را همان لحظه نشان بده تا کاربر مطمئن شود واقعاً عوض شده است
    try:
        from aiogram.types import BufferedInputFile
        from utils import make_qr_png_bytes
        qr_bytes = make_qr_png_bytes(new_link)
        if qr_bytes:
            sent = await callback.message.answer_photo(
                BufferedInputFile(qr_bytes, filename="qr.png"), caption=f"🔗 لینک ساب جدید:\n{new_link}",
            )
            if getattr(sent, "photo", None):
                db.set_config_qr(cfg_id, sent.photo[-1].file_id)
        else:
            await callback.message.answer(f"🔗 لینک ساب جدید:\n{new_link}")
    except Exception:
        logger.warning("ارسال لینک/QR جدید بعد از تغییر لینک ناموفق بود cfg_id=%s", cfg_id, exc_info=True)


@router.callback_query(F.data.startswith("usersvcdisable_"))
async def user_disable_sublink(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    cfg_id = int(callback.data.replace("usersvcdisable_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not user or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return
    if not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer(ui_editor.get_alert_text("msg_1464432778", "❌ این قابلیت برای این سرویس فعال نیست."), show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer(ui_editor.get_alert_text("msg_1fd738597f", "❌ نمونه پنل پیدا نشد."), show_alert=True)
        return
    ok, msg = await panels.disable_service(panel, cfg["service_id"])
    if ok:
        ok, msg = await _verify_panel_toggle(panel, cfg["service_id"], True)
    if not ok:
        await callback.answer(f"❌ غیرفعال‌سازی روی پنل انجام نشد.\n{msg}"[:190], show_alert=True)
        return
    db.set_config_link_disabled(cfg_id, True)
    # هر callback فقط یک‌بار قابل answer شدن است؛ قبلاً اینجا یک‌بار
    # answer(show_alert=True) صدا زده می‌شد و بعد view_config دوباره callback.answer را صدا
    # می‌زد که تلگرام برای callback قبلاً‌پاسخداده‌شده خطا می‌دهد (و در نتیجه
    # صفحه/دکمه‌ها به‌روز نمی‌شد). حالا فقط یک‌بار (داخل خود view_config) answer می‌شود
    # و پیام موفقیت هم همون جا نشون داده می‌شود، همراه با رفرش کامل صفحه با وضعیت جدید.
    await view_config(callback, cfg_id=cfg_id, toast="✅ لینک ساب غیرفعال شد.")


@router.callback_query(F.data.startswith("usersvcenable_"))
async def user_enable_sublink(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    cfg_id = int(callback.data.replace("usersvcenable_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not user or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return
    if not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer(ui_editor.get_alert_text("msg_1464432778", "❌ این قابلیت برای این سرویس فعال نیست."), show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer(ui_editor.get_alert_text("msg_1fd738597f", "❌ نمونه پنل پیدا نشد."), show_alert=True)
        return
    import fair_usage
    _deny = fair_usage.enable_denied(cfg)
    if _deny:
        await callback.answer(_deny, show_alert=True)
        return
    ok, msg = await panels.enable_service(panel, cfg["service_id"])
    if ok:
        ok, msg = await _verify_panel_toggle(panel, cfg["service_id"], False)
    if not ok:
        await callback.answer(f"❌ فعال‌سازی روی پنل انجام نشد.\n{msg}"[:190], show_alert=True)
        return
    db.set_config_link_disabled(cfg_id, False)
    await view_config(callback, cfg_id=cfg_id, toast="✅ لینک ساب فعال شد.")


# حداکثر واقعی تلگرام برای متن یک پیام ۴۰۹۶ کاراکتر است؛ برای امنیت بیشتر
# (کدهای یونیکد چندبایتی و فاصله‌ی احتیاطی) عدد کمتری در نظر گرفته می‌شود.
_TELEGRAM_MSG_SAFE_LIMIT = 3500


# کانفیگ‌های تکی آخرین ارسال هر کاربر؛ برای دکمه‌ی «کپی همه‌ی کانفیگ‌ها».
# (کوتاه‌مدت و درون‌حافظه‌ای؛ اگر پاک شده باشد، دکمه دوباره از لینک ساب می‌خواند.)
_LAST_SENT_CONFIGS: dict = {}
_LAST_SENT_CONFIGS_MAX = 500


def _remember_sent_configs(tg_id, cfg_id, configs: list[str]):
    if len(_LAST_SENT_CONFIGS) >= _LAST_SENT_CONFIGS_MAX:
        for k in list(_LAST_SENT_CONFIGS)[: _LAST_SENT_CONFIGS_MAX // 2]:
            _LAST_SENT_CONFIGS.pop(k, None)
    _LAST_SENT_CONFIGS[(int(tg_id), int(cfg_id))] = list(configs)


def _copy_all_configs_keyboard(cfg_id, configs: list[str]):
    """دکمه‌ی «کپی همه‌ی کانفیگ‌ها» زیر آخرین پیام. اگر مجموع کانفیگ‌ها ≤۲۵۶
    کاراکتر باشد مستقیم copy_text است؛ وگرنه callback که همه را در یک بلوک
    قابل‌کپی می‌فرستد (تلگرام برای copy_text سقف ۲۵۶ کاراکتر دارد)."""
    joined = "\n".join(configs)
    if 0 < len(joined) <= 256:
        btn = _UB(text="📋 کپی همه کانفیگ‌ها", copy_text=types.CopyTextButton(text=joined), style="primary")
    else:
        btn = _UB(text="📋 کپی همه کانفیگ‌ها", callback_data=f"copyallcfg_{cfg_id}", style="primary")
    return _UM(inline_keyboard=[[btn]])


async def _send_configs_safely(callback: types.CallbackQuery, configs: list[str], plan_name: str, cfg_id=None):
    """کانفیگ‌های تکی استخراج‌شده را در چند پیام (هرکدام زیر سقف امن تلگرام)
    برای کاربر ارسال می‌کند. زیر آخرین پیام دکمه‌ی «کپی همه‌ی کانفیگ‌ها» قرار می‌گیرد.
    ۱) هر کانفیگ تکی که به‌تنهایی طولانی‌تر از سقف امن باشد در پیام جداگانه می‌رود.
    ۲) اگر ارسال با مارک‌داون خطا بدهد، بدون فرمت دوباره ارسال می‌شود.
    """
    header = f"📥 {len(configs)} کانفیگ از سرویس {plan_name} پیدا شد:\n\n"

    pieces: list[tuple[str, str]] = []  # (markdown, plain)
    chunk_md, chunk_plain = header, header
    for conf in configs:
        conf_md_line = f"`{conf}`\n\n"
        if len(conf_md_line) > _TELEGRAM_MSG_SAFE_LIMIT:
            if chunk_md != header:
                pieces.append((chunk_md, chunk_plain))
                chunk_md, chunk_plain = header, header
            pieces.append((f"`{conf}`", conf))
            continue
        if len(chunk_md) + len(conf_md_line) > _TELEGRAM_MSG_SAFE_LIMIT:
            pieces.append((chunk_md, chunk_plain))
            chunk_md, chunk_plain = header, header
        chunk_md += conf_md_line
        chunk_plain += f"{conf}\n\n"
    if chunk_md != header:
        pieces.append((chunk_md, chunk_plain))

    markup = _copy_all_configs_keyboard(cfg_id, configs) if (cfg_id is not None and configs) else None
    if cfg_id is not None:
        _remember_sent_configs(callback.from_user.id, cfg_id, configs)

    async def _send(text: str, parse_mode: str | None, reply_markup):
        try:
            await callback.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except Exception:
            logger.exception("خطا در ارسال پیام کانفیگ (parse_mode=%s)", parse_mode)
            return False

    for i, (md, plain) in enumerate(pieces):
        rm = markup if i == len(pieces) - 1 else None
        if await _send(md, "Markdown", rm):
            continue
        if not await _send(plain, None, rm):
            await callback.message.answer(
                ui_editor.get_text("msg_a6c974ffae", "❌ ارسال یکی از کانفیگ‌ها با خطا مواجه شد. لطفاً با پشتیبانی تماس بگیرید.")
            )


def _owned_or_admin_config(tg_id: int, cfg_id: int):
    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg.get("deleted") and not _is_admin_id(tg_id):
        return None
    if _is_admin_id(tg_id):
        return cfg
    user = db.get_user(tg_id)
    if user is None or cfg["user_id"] != user["id"]:
        return None
    return cfg


def _is_admin_id(tg_id: int) -> bool:
    try:
        return int(tg_id) == int(ADMIN_ID) or bool(db.is_admin(tg_id))
    except Exception:
        return int(tg_id) == int(ADMIN_ID)


async def _send_copyable_block(message: types.Message, header: str, lines: list[str]):
    """لیست خطوط را داخل بلوک‌های <pre> می‌فرستد (تپ روی بلوک، کل آن را کپی می‌کند)."""
    import html as _html
    block, size = [], 0
    async def flush():
        if block:
            body = "\n".join(block)
            try:
                await message.answer(f"{_html.escape(header)}\n<pre>{_html.escape(body)}</pre>", parse_mode="HTML")
            except Exception:
                await message.answer(f"{header}\n{body}", parse_mode=None)
    for ln in lines:
        if size + len(ln) + 1 > _TELEGRAM_MSG_SAFE_LIMIT and block:
            await flush(); block, size = [], 0
        block.append(ln); size += len(ln) + 1
    await flush()


@router.callback_query(F.data.startswith("copysub_"))
async def copy_sub_link(callback: types.CallbackQuery):
    try:
        cfg_id = int(callback.data.replace("copysub_", ""))
    except ValueError:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return
    cfg = _owned_or_admin_config(callback.from_user.id, cfg_id)
    if cfg is None:
        await callback.answer("❌ این سرویس متعلق به شما نیست یا یافت نشد.", show_alert=True)
        return
    try:
        link = str(crypto.decrypt_config(cfg["config"]) or "").strip()
    except Exception:
        link = ""
    if not link.lower().startswith(("http://", "https://")):
        await callback.answer("❌ لینک ساب معتبری برای این سرویس ثبت نشده.", show_alert=True)
        return
    await callback.answer()
    await _send_copyable_block(callback.message, "🔗 لینک ساب (روی آن بزنید تا کپی شود):", [link])


@router.callback_query(F.data.startswith("copyallcfg_"))
async def copy_all_configs(callback: types.CallbackQuery):
    try:
        cfg_id = int(callback.data.replace("copyallcfg_", ""))
    except ValueError:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return
    cfg = _owned_or_admin_config(callback.from_user.id, cfg_id)
    if cfg is None:
        await callback.answer("❌ این سرویس متعلق به شما نیست یا یافت نشد.", show_alert=True)
        return
    configs = _LAST_SENT_CONFIGS.get((int(callback.from_user.id), cfg_id))
    if not configs:
        # کش پاک شده (مثلاً ری‌استارت)؛ دوباره از لینک ساب استخراج می‌کنیم.
        try:
            link = str(crypto.decrypt_config(cfg["config"]) or "").strip()
            if link.lower().startswith(("http://", "https://")):
                configs = await extract_configs(link)
        except Exception:
            logger.exception("استخراج مجدد کانفیگ‌ها برای کپی همه ناموفق بود cfg_id=%s", cfg_id)
            configs = None
    if not configs:
        await callback.answer("❌ کانفیگی برای کپی پیدا نشد؛ دوباره «دریافت کانفیگ‌های تکی» را بزنید.", show_alert=True)
        return
    await callback.answer()
    import html as _html
    body = "\n".join(configs)
    header = f"📋 همه‌ی {len(configs)} کانفیگ — با یک بار لمس بلوک زیر، همه‌ی آن‌ها یک‌جا کپی می‌شود:"
    if len(header) + len(body) + 20 <= 4000:
        # یک پیام، یک بلوک: یک لمس = کپی کل کانفیگ‌ها در کلیپ‌بورد
        try:
            await callback.message.answer(f"{_html.escape(header)}\n<pre>{_html.escape(body)}</pre>", parse_mode="HTML")
        except Exception:
            await callback.message.answer(f"{header}\n{body}", parse_mode=None)
    else:
        # بیش از سقف طول یک پیام تلگرام: به‌جای چند بلوک جدا، همه را در یک فایل متنی می‌فرستیم.
        await callback.message.answer_document(
            types.BufferedInputFile(body.encode("utf-8"), filename="configs.txt"),
            caption=f"📋 همه‌ی {len(configs)} کانفیگ در یک فایل (برای طولانی‌بودن بیش از سقف یک پیام تلگرام).",
        )


@router.callback_query(F.data.startswith("wgfiles_"))
async def wireguard_files(callback: types.CallbackQuery):
    """ارسال فایل(های) کانفیگ وایرگارد سرویس."""
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا دستور /start را بزنید.", show_alert=True)
        return
    try:
        cfg_id = int(callback.data.replace("wgfiles_", ""))
    except ValueError:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return
    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer("❌ این سرویس متعلق به شما نیست یا یافت نشد.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg.get("panel_id")) if cfg.get("panel_id") else None
    if not panel or not panels.needs_wg_delivery(panel):
        await callback.answer("❌ این سرویس فایل وایرگارد ندارد.", show_alert=True)
        return
    await callback.answer("⏳ در حال دریافت فایل‌ها...")
    sub_url = None
    try:
        dec = crypto.decrypt_config(cfg["config"])
        if dec and str(dec).lower().startswith(("http://", "https://")):
            sub_url = str(dec).strip()
    except Exception:
        pass
    try:
        ok, files, msg = await panels.get_wireguard_files(panel, cfg.get("service_id"), sub_url)
    except Exception:
        logger.exception("wgfiles failed cfg=%s", cfg_id)
        ok, files, msg = False, [], "خطای داخلی"
    if not ok or not files:
        await callback.message.answer("❌ " + str(msg or "فایل وایرگارد در دسترس نیست. کمی بعد دوباره تلاش کنید."))
        return
    for name, data in files[:20]:
        try:
            await callback.message.answer_document(types.BufferedInputFile(data, filename=name))
        except Exception:
            logger.warning("ارسال فایل وایرگارد ناموفق cfg=%s", cfg_id, exc_info=True)


@router.callback_query(F.data.startswith("mirrorconfigs_"))
async def mirror_configs(callback: types.CallbackQuery):
    """ارسال کانفیگ‌های تکی سرویس.

    برای مرزبان/پاسارگارد، پاسخ GET /api/user ممکن است فقط اطلاعات کاربر و
    subscription_url را برگرداند و خود URLهای vless/vmess را داخل payload
    نداشته باشد. در نسخه‌ی قبلی همین‌جا با پیام «کانفیگ تکی برنگرداند» متوقف
    می‌شد و در نتیجه دکمه برای بعضی سرویس‌ها هیچ کانفیگی ارسال نمی‌کرد.

    حالا ترتیب fallback این است:
      1) استخراج مستقیم URLهای کانفیگ از پاسخ زنده‌ی پنل؛
      2) اگر نبود، استفاده از لینک ساب زنده‌ی پنل؛
      3) در نهایت استفاده از لینک ساب ذخیره‌شده در DB.
    """
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    try:
        cfg_id = int(callback.data.replace("mirrorconfigs_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_d3f440f81a", "❌ سرویس یافت نشد."), show_alert=True)
        return

    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return

    await callback.answer(ui_editor.get_alert_text("msg_d56d34875b", "⏳ در حال دریافت کانفیگ‌ها..."))

    panel = db.get_vpn_panel(cfg.get("panel_id")) if cfg.get("panel_id") else None
    live_sub_url = None

    # اگر سرویس روی پنل است، اول پاسخ زنده‌ی خود پنل را امتحان می‌کنیم.
    if panel and cfg.get("service_id"):
        try:
            ok, snapshot, msg = await panels.get_service_snapshot(panel, cfg.get("service_id"))
        except Exception:
            logger.exception("خطای غیرمنتظره در دریافت snapshot برای cfg_id=%s", cfg_id)
            ok, snapshot, msg = False, None, "خطای داخلی"

        if ok and snapshot:
            # 1) بعضی پنل‌ها URLهای تکی را مستقیماً داخل payload برمی‌گردانند.
            configs = panels.extract_panel_configs(snapshot)
            if configs:
                await _send_configs_safely(callback, configs, cfg.get("plan") or "سرویس", cfg_id=cfg_id)
                return

            # 2) اگر فقط subscription_url برگشته باشد، آن را برای استخراج کانفیگ‌ها
            # استفاده می‌کنیم. مسیر نسبی را به URL کامل پنل تبدیل می‌کنیم.
            live_sub_url = snapshot.get("link")
            if isinstance(live_sub_url, str) and live_sub_url.startswith("/"):
                base_url = str(panel.get("base_url") or "").rstrip("/")
                if base_url:
                    live_sub_url = base_url + live_sub_url

        elif panel.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"):
            # خطای پنل نباید مانع fallback به لینک ذخیره‌شده شود؛ ممکن است
            # لینک ساب قبلاً در DB سالم و قابل استفاده باشد.
            logger.warning("snapshot پنل برای cfg_id=%s ناموفق بود: %s", cfg_id, msg)

    # 3) لینک زنده‌ی پنل، و بعد لینک ذخیره‌شده‌ی DB.
    candidate_links = []
    if live_sub_url:
        candidate_links.append(live_sub_url)

    try:
        decrypted = crypto.decrypt_config(cfg["config"])
    except Exception:
        decrypted = None
    if decrypted and str(decrypted).lower().startswith(("http://", "https://")):
        candidate_links.append(decrypted)

    # حذف تکراری‌ها بدون تغییر ترتیب اولویت.
    unique_links = []
    seen = set()
    for link in candidate_links:
        link = str(link).strip()
        if link and link not in seen:
            seen.add(link)
            unique_links.append(link)

    if not unique_links:
        await callback.message.answer(ui_editor.get_text(
            "msg_44390536bd",
            "❌ لینک ساب معتبری برای این سرویس ثبت نشده."
        ))
        return

    last_result = None
    for sub_url in unique_links:
        try:
            configs = await extract_configs(sub_url)
        except Exception:
            logger.exception("خطای غیرمنتظره در extract_configs برای cfg_id=%s", cfg_id)
            configs = None
        last_result = configs
        if configs:
            await _send_configs_safely(callback, configs, cfg.get("plan") or "سرویس", cfg_id=cfg_id)
            return

    if last_result is None:
        await callback.message.answer(ui_editor.get_text(
            "msg_9410c4aeac",
            "❌ لینک ساب در حال حاضر در دسترس نیست. کمی بعد دوباره امتحان کنید."
        ))
    else:
        await callback.message.answer(ui_editor.get_text(
            "msg_a58136abf9",
            "⚠️ لینک ساب باز شد ولی هیچ کانفیگ تکی‌ای داخلش پیدا نشد."
        ))


@router.callback_query(F.data.startswith("viewqr_"))
async def view_config_qr(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    try:
        cfg_id = int(callback.data.replace("viewqr_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_d3f440f81a", "❌ سرویس یافت نشد."), show_alert=True)
        return

    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return
    if not cfg.get("qr_file_id"):
        await callback.answer(ui_editor.get_alert_text("msg_e6ee2b21a1", "❌ کیوآرکدی برای این سرویس ثبت نشده."), show_alert=True)
        return

    await callback.answer()
    try:
        await callback.bot.send_photo(callback.from_user.id, cfg["qr_file_id"], caption=f"🖼 کیوآرکد {cfg['plan']}")
    except Exception:
        await callback.answer(ui_editor.get_alert_text("msg_f0b8973a1a", "❌ ارسال کیوآرکد ناموفق بود."), show_alert=True)


@router.callback_query(F.data.startswith("delconfig_"))
async def delete_config_confirm(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    try:
        cfg_id = int(callback.data.replace("delconfig_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_d3f440f81a", "❌ سرویس یافت نشد."), show_alert=True)
        return

    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"] or cfg.get("deleted"):
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return

    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "config_delete_confirm", 
        f"⚠️ مطمئنی می‌خوای «{cfg['plan']}» رو حذف کنی؟\n\n"
        f"این سرویس از لیست «سرویس‌های من» شما پاک می‌شه (ولی اطلاعاتش نزد پشتیبانی می‌مونه).",
        reply_markup=confirm_delete_config_keyboard(cfg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delconfirm_"))
async def delete_config_apply(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    try:
        cfg_id = int(callback.data.replace("delconfirm_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_d3f440f81a", "❌ سرویس یافت نشد."), show_alert=True)
        return

    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"]:
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return

    if cfg.get("panel_id") and cfg.get("service_id") and cfg.get("source") in panels.PANEL_TYPES:
        panel = db.get_vpn_panel(cfg["panel_id"])
        if panel:
            ok, msg = await panels.delete_service(panel, cfg["service_id"])
            if not ok:
                await callback.answer(ui_editor.get_alert_text("msg_panel_delete_error", f"❌ حذف از پنل ناموفق بود: {msg}", {"msg": msg}), show_alert=True)
                return
    db.set_config_deleted(cfg_id, True)
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, None,
        "✅ سرویس حذف شد.",
        reply_markup=back_button("my_configs_vip", "🔙 بازگشت", screen="config_detail"),
        ui_key="config_deleted",
    )
    await callback.answer()


async def _cbuild_begin(callback: types.CallbackQuery, state: FSMContext, option_num: int):
    await state.clear()
    await state.update_data(custom_order_type="new", custom_target_config_id=None, custom_option_num=int(option_num or 0))
    await state.set_state(UserStates.waiting_custom_volume)
    _cb = db.get_effective_custom_build_settings(int(option_num or 0))
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, None,
        progress_bar(1, 3) +
        "🛠 سرویس خودت رو بساز\n\n"
        f"💡 نحوه محاسبه قیمت: هر گیگابایت حجم {_cb['price_per_gb']:,} تومان + "
        f"هر ۳۰ روز {_cb['price_per_30_days']:,} تومان (متناسب با تعداد روزها محاسبه می‌شه).\n\n"
        f"📦 حجم سرویس مورد نظرت رو به گیگابایت وارد کن (بین {_cb['min_gb']} تا {_cb['max_gb']}):",
        reply_markup=custom_build_cancel_keyboard(),
    )


@router.callback_query(F.data == "cbuild_start")
async def cbuild_start(callback: types.CallbackQuery, state: FSMContext):
    if not db.is_orders_enabled():
        await callback.answer(ORDERS_CLOSED_TEXT, show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    # 🔌 چند نگاشت/گزینه: اگر بیش از یکی (با پنل فعال) تعریف شده باشد، اول لیست نام‌ها نشان داده می‌شود؛
    # اگر فقط یکی بود (یا هیچ، مثل قبل) مستقیم می‌رود مرحله‌ی بعد.
    opts = db.usable_map_options("custom_build")
    if len(opts) > 1:
        await state.clear()
        rows = [[_UB(text=f"🛠 {o['name']}", callback_data=f"cbopt_{o['num']}", style="primary")] for o in opts]
        rows.append([_UB(text="🔙 انصراف", callback_data="plans", style="danger", ui_screen="custom_build_options", ui_button_key="back")])
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, None,
            "🛠 سرویس خودت رو بساز\n\nیکی از گزینه‌های زیر را انتخاب کن:",
            reply_markup=_UM(inline_keyboard=rows),
        )
        await callback.answer()
        return
    await _cbuild_begin(callback, state, opts[0]["num"] if opts else 0)
    await callback.answer()


@router.callback_query(F.data.startswith("cbopt_"))
async def cbuild_pick_option(callback: types.CallbackQuery, state: FSMContext):
    try:
        num = int(callback.data.replace("cbopt_", ""))
    except ValueError:
        await callback.answer("❌ گزینه نامعتبر است.", show_alert=True)
        return
    if not any(o["num"] == num for o in db.usable_map_options("custom_build")):
        await callback.answer("❌ این گزینه دیگر در دسترس نیست.", show_alert=True)
        return
    await _cbuild_begin(callback, state, num)
    await callback.answer()


# ---------------------------------------------------------------------------
# 🔁 تمدید سرویس — ساختار دسته/پلنِ ربات، بدون افزودن دکمه به منوی اصلی
# ---------------------------------------------------------------------------
def _renewal_settings(category_id, plan_key=None, custom_option=None):
    try:
        cid=int(category_id or 0)
    except Exception:
        cid=0
    defaults={"mode":"day","price_day":0,"price_gb":5500,"min_day":1,"max_day":0,"min_gb":1,"max_gb":0,"day_options":"30,60,90","gb_options":"10,20,50"}
    try:
        if custom_option is not None and not plan_key:
            defaults = bot_info.get_renewal_custom_settings(custom_option)
        else:
            defaults = bot_info.get_renewal_plan_settings(plan_key,cid) if plan_key else bot_info.get_renewal_settings(cid)
    except Exception:
        pass
    if defaults.get("mode") not in ("day","gb","both"):
        defaults["mode"]="day"
    return defaults

def _renewal_category_id(cfg):
    try:
        return db.resolve_config_plan(cfg)[1]
    except Exception:
        return None

def _renew_choice_values(minimum, maximum, raw):
    try: minimum=max(1,int(minimum or 1))
    except Exception: minimum=1
    try: maximum=int(maximum or 0)
    except Exception: maximum=0
    out=[]
    for x in str(raw or "").replace("،",",").split(","):
        try: v=int(float(x.strip()))
        except Exception: continue
        if v<minimum or (maximum and v>maximum) or v in out: continue
        out.append(v)
    return out

@router.callback_query(F.data.startswith("renewcfg_"))
async def renew_choose_service(callback: types.CallbackQuery, state: FSMContext):
    if not db.is_orders_enabled():
        await callback.answer(ORDERS_CLOSED_TEXT, show_alert=True); return
    user=db.get_user(callback.from_user.id)
    try: cfg_id=int(callback.data.replace("renewcfg_","",1))
    except Exception: cfg_id=0
    cfg=db.get_config_by_id(cfg_id) if user else None
    if not user or not cfg or cfg.get("deleted") or int(cfg.get("user_id") or 0)!=int(user.get("id") or 0):
        await callback.answer("❌ این سرویس متعلق به شما نیست یا یافت نشد.",show_alert=True); return
    if not db.config_allows_renew(cfg):
        await callback.answer(db.renew_block_text(cfg),show_alert=True); return
    category_id=_renewal_category_id(cfg)
    _rpk,_rcid=db.resolve_config_plan(cfg); category_id=_rcid or category_id
    _rco=db.config_custom_option(cfg) if not _rpk else None
    settings=_renewal_settings(category_id,_rpk,_rco)
    service_name=str(cfg.get("service_id") or cfg.get("_display_name") or cfg.get("plan") or "سرویس").strip()
    mode=settings.get("mode","day")
    await state.clear()
    await state.update_data(custom_order_type="renew",custom_target_config_id=cfg_id,renew_cfg_id=cfg_id,renew_category_id=category_id,renew_plan_key=_rpk,renew_custom_option=_rco,renew_mode=mode,renew_service_name=service_name,renew_telegram_id=callback.from_user.id,custom_volume=0,custom_days=0,custom_name=None)
    if mode=="gb":
        vals=_renew_choice_values(settings.get("min_gb"),settings.get("max_gb"),settings.get("gb_options"))
        rows=[]
        for i in range(0,len(vals),2): rows.append([_UB(text=f"{v} گیگ",callback_data=f"renewvol_{v}", style="primary") for v in vals[i:i+2]])
        rows.append([_UB(text="➕ مقدار دلخواه",callback_data="renewvol_custom", style="primary")])
        rows.append([_UB(text="❌ لغو تمدید",callback_data="renew_cancel", style="danger")])
        prompt="📦 مقدار حجمی که می‌خواهید به سرویس اضافه شود را انتخاب کنید:"
    else:
        vals=_renew_choice_values(settings.get("min_day"),settings.get("max_day"),settings.get("day_options"))
        rows=[]
        for i in range(0,len(vals),2): rows.append([_UB(text=f"{v} روز",callback_data=f"renewdays_{v}", style="primary") for v in vals[i:i+2]])
        rows.append([_UB(text="➕ زمان دلخواه",callback_data="renewdays_custom", style="primary")])
        rows.append([_UB(text="❌ لغو تمدید",callback_data="renew_cancel", style="danger")])
        prompt="⏳ مقدار زمان اضافه را انتخاب کنید:"
    await callback.message.edit_text(f"🔁 تمدید سرویس «{service_name}»\n\n{prompt}",reply_markup=_UM(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("renewdays_"))
async def renew_days_choice(callback: types.CallbackQuery,state:FSMContext):
    data=await state.get_data()
    if data.get("custom_order_type")!="renew": await callback.answer("❌ مسیر تمدید منقضی شده.",show_alert=True); return
    value=callback.data.replace("renewdays_","")
    if value=="custom":
        await state.set_state(UserStates.waiting_renew_days)
        await callback.message.edit_text("⏳ تعداد روز تمدید را وارد کنید:",reply_markup=_UM(inline_keyboard=[[_UB(text="🔙 لغو",callback_data="renew_cancel", style="danger")]])); await callback.answer(); return
    try: days=int(value)
    except Exception: await callback.answer("❌ مقدار نامعتبر.",show_alert=True); return
    settings=_renewal_settings(data.get("renew_category_id"),data.get("renew_plan_key"),data.get("renew_custom_option"))
    if days<int(settings.get("min_day") or 1) or (settings.get("max_day") and days>int(settings["max_day"])):
        await callback.answer("❌ تعداد روز خارج از محدوده تنظیم‌شده است.",show_alert=True); return
    await state.update_data(custom_days=days)
    if data.get("renew_mode")=="both":
        vals=_renew_choice_values(settings.get("min_gb"),settings.get("max_gb"),settings.get("gb_options"))
        rows=[]
        for i in range(0,len(vals),2): rows.append([_UB(text=f"{v} گیگ",callback_data=f"renewvol_{v}", style="primary") for v in vals[i:i+2]])
        rows.append([_UB(text="➕ مقدار دلخواه",callback_data="renewvol_custom", style="primary")])
        rows.append([_UB(text="❌ لغو تمدید",callback_data="renew_cancel", style="danger")])
        await callback.message.edit_text("📦 حالا حجم تمدید را انتخاب کنید:",reply_markup=_UM(inline_keyboard=rows)); await callback.answer(); return
    await state.update_data(custom_volume=0)
    await state.set_state(None)
    await _show_custom_summary(callback.bot,callback.message.chat.id,callback.from_user.id,state)
    await callback.answer()

@router.message(UserStates.waiting_renew_days)
async def renew_custom_days(message:types.Message,state:FSMContext):
    try: days=int((message.text or "").strip())
    except Exception: days=0
    data=await state.get_data(); settings=_renewal_settings(data.get("renew_category_id"),data.get("renew_plan_key"),data.get("renew_custom_option"))
    if days<int(settings.get("min_day") or 1) or (settings.get("max_day") and days>int(settings["max_day"])):
        await message.answer(f"❌ تعداد روز باید بین {settings.get('min_day',1)} و {settings.get('max_day') or 'نامحدود'} باشد:"); return
    await state.update_data(custom_days=days)
    if data.get("renew_mode")=="both":
        vals=_renew_choice_values(settings.get("min_gb"),settings.get("max_gb"),settings.get("gb_options"))
        rows=[]
        for i in range(0,len(vals),2): rows.append([_UB(text=f"{v} گیگ",callback_data=f"renewvol_{v}", style="primary") for v in vals[i:i+2]])
        rows.append([_UB(text="➕ مقدار دلخواه",callback_data="renewvol_custom", style="primary")])
        rows.append([_UB(text="❌ لغو تمدید",callback_data="renew_cancel", style="danger")])
        await state.set_state(None); await message.answer("📦 حالا حجم تمدید را انتخاب کنید:",reply_markup=_UM(inline_keyboard=rows)); return
    await state.update_data(custom_volume=0); await state.set_state(None); await _show_custom_summary(message.bot,message.chat.id,message.from_user.id,state)

@router.callback_query(F.data.startswith("renewvol_"))
async def renew_volume_choice(callback:types.CallbackQuery,state:FSMContext):
    data=await state.get_data()
    if data.get("custom_order_type")!="renew": await callback.answer("❌ مسیر تمدید منقضی شده.",show_alert=True); return
    value=callback.data.replace("renewvol_","")
    if value=="custom":
        await state.set_state(UserStates.waiting_renew_custom_volume)
        await callback.message.edit_text("📦 مقدار حجم اضافه را به گیگ وارد کنید:",reply_markup=_UM(inline_keyboard=[[_UB(text="🔙 لغو",callback_data="renew_cancel", style="danger")]])); await callback.answer(); return
    try: volume=float(value)
    except Exception: await callback.answer("❌ مقدار نامعتبر.",show_alert=True); return
    settings=_renewal_settings(data.get("renew_category_id"),data.get("renew_plan_key"),data.get("renew_custom_option"))
    if volume<float(settings.get("min_gb") or 1) or (settings.get("max_gb") and volume>float(settings["max_gb"])):
        await callback.answer("❌ حجم خارج از محدوده تنظیم‌شده است.",show_alert=True); return
    await state.update_data(custom_volume=volume)
    if data.get("renew_mode")=="both" and not data.get("custom_days"):
        await callback.answer("❌ ابتدا مدت تمدید را انتخاب کنید.",show_alert=True); return
    await state.set_state(None); await _show_custom_summary(callback.bot,callback.message.chat.id,callback.from_user.id,state); await callback.answer()

@router.message(UserStates.waiting_renew_custom_volume)
async def renew_custom_volume(message:types.Message,state:FSMContext):
    try: volume=float((message.text or "").strip().replace(",","."))
    except Exception: volume=0
    data=await state.get_data(); settings=_renewal_settings(data.get("renew_category_id"),data.get("renew_plan_key"),data.get("renew_custom_option"))
    if volume<float(settings.get("min_gb") or 1) or (settings.get("max_gb") and volume>float(settings["max_gb"])):
        await message.answer(f"❌ حجم باید بین {settings.get('min_gb',1)} و {settings.get('max_gb') or 'نامحدود'} گیگ باشد:"); return
    await state.update_data(custom_volume=volume)
    await state.set_state(None); await _show_custom_summary(message.bot,message.chat.id,message.from_user.id,state)

@router.callback_query(F.data == "renew_cancel")
async def renew_cancel(callback:types.CallbackQuery,state:FSMContext):
    await state.clear(); await callback.message.edit_text("🔙 عملیات تمدید لغو شد."); await callback.answer()

@router.callback_query(F.data.startswith("renew_"))
async def renew_start(callback: types.CallbackQuery, state: FSMContext):
    # سازگاری با callbackهای قدیمی renew_<config_id>. مسیر اصلی فعلی تمدید
    # از renewcfg_<config_id> و تنظیمات دسته/پلن استفاده می‌کند.

    if not db.is_orders_enabled():
        await callback.answer(ORDERS_CLOSED_TEXT, show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    try:
        cfg_id = int(callback.data.replace("renew_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_d3f440f81a", "❌ سرویس یافت نشد."), show_alert=True)
        return

    cfg = db.get_config_by_id(cfg_id)
    if cfg is None or cfg["user_id"] != user["id"]:
        await callback.answer(ui_editor.get_alert_text("msg_c0d5b8f1e3", "❌ این سرویس متعلق به شما نیست یا یافت نشد."), show_alert=True)
        return

    await state.clear()
    await state.update_data(custom_order_type="renew", custom_target_config_id=cfg_id)
    _cb = db.get_effective_custom_build_settings()
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "custom_build",
        f"🔁 تمدید سرویس «{cfg['plan']}»\n\n"
        f"💡 زمان: هر ۳۰ روز {_cb['price_per_30_days']:,} تومان | حجم: هر گیگ {_cb['price_per_gb']:,} تومان\n\n"
        "نوع تمدید را انتخاب کن:",
        reply_markup=renew_mode_keyboard(),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("renewmode_"))
async def renew_mode_start(callback: types.CallbackQuery, state: FSMContext):
    # fix: قبلاً `_cb` اینجا هیچ‌جا تعریف نمی‌شد ولی چند خط پایین‌تر برای
    # min_days/min_gb استفاده می‌شد؛ در نتیجه هر سه حالت تمدید (زمان/حجم/هردو)
    # با NameError کرش می‌کردن و مسیر «تمدید سرویس» عملاً کار نمی‌کرد.
    _cb = db.get_effective_custom_build_settings()
    mode = callback.data.replace("renewmode_", "")
    if mode not in ("time", "volume", "both"):
        await callback.answer(ui_editor.get_alert_text("msg_0ea6b221c0", "❌ حالت تمدید نامعتبر است."), show_alert=True); return
    data = await state.get_data()
    if data.get("custom_order_type") != "renew" or not data.get("custom_target_config_id"):
        await callback.answer(ui_editor.get_alert_text("msg_5d323fcef2", "❌ این مسیر منقضی شده؛ دوباره تمدید را بزن."), show_alert=True); return
    await state.update_data(renew_mode=mode, custom_volume=0 if mode == "time" else None, custom_days=0 if mode == "volume" else None)
    if mode == "time":
        await state.set_state(UserStates.waiting_custom_days)
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "custom_build",
            f"⏳ چند روز به اعتبار سرویس اضافه شود؟\nحداقل {_cb['min_days']} روز:", reply_markup=custom_build_cancel_keyboard())
    else:
        await state.set_state(UserStates.waiting_custom_volume)
        text = f"🗜 چند گیگابایت به حجم سرویس اضافه شود؟\nحداقل {_cb['min_gb']} گیگ:" if mode == "volume" else "🗜 چند گیگابایت حجم اضافه شود؟"
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "custom_build", text, reply_markup=custom_build_cancel_keyboard())
    await callback.answer()


@router.message(UserStates.waiting_custom_volume)
async def custom_volume_input(message: types.Message, state: FSMContext):
    data0 = await state.get_data()
    _cb = db.get_effective_custom_build_settings(int(data0.get("custom_option_num") or 0))
    min_gb = _cb['min_gb']
    volume = parse_int_in_range(message.text, min_gb, _cb['max_gb'])
    if volume is None:
        await message.answer(f"❌ لطفاً فقط یک عدد بین {min_gb} تا {_cb['max_gb']} وارد کن:")
        return

    await state.update_data(custom_volume=volume)
    if data0.get("custom_order_type") == "renew" and data0.get("renew_mode") == "volume":
        await state.update_data(custom_days=0)
        await state.set_state(None)
        await _show_custom_summary(message.bot, message.chat.id, message.from_user.id, state)
        return
    await state.set_state(UserStates.waiting_custom_days)
    data = await state.get_data()
    text = f"⏳ مدت اعتبار سرویس رو به روز وارد کن (بین {_cb['min_days']} تا {_cb['max_days']}):"
    if data.get("custom_order_type") == "new":
        # 🧪 تست: ادامه‌ی استیکر make.webm در مرحله‌ی بعدی مسیر «بساز سرویس خودت»
        await show_menu_with_sticker(message.bot, message.chat.id, "custom_build", text)
    else:
        await show_menu_with_sticker(message.bot, message.chat.id, "custom_build", text)


@router.message(UserStates.waiting_custom_days)
async def custom_days_input(message: types.Message, state: FSMContext):
    data0 = await state.get_data()
    _cb = db.get_effective_custom_build_settings(int(data0.get("custom_option_num") or 0))
    min_days = _cb['min_days']
    days = parse_int_in_range(message.text, min_days, _cb['max_days'])
    if days is None:
        await message.answer(
            f"❌ لطفاً فقط یک عدد بین {min_days} تا {_cb['max_days']} وارد کن:"
        )
        return

    await state.update_data(custom_days=days)
    data = await state.get_data()

    if data.get("custom_order_type") == "renew":
        await state.set_state(None)
        await _show_custom_summary(message.bot, message.chat.id, message.from_user.id, state)
        return

    await state.set_state(UserStates.waiting_custom_name)
    # 🧪 تست: ادامه‌ی استیکر make.webm در مرحله‌ی وارد کردن نام سرویس
    await show_menu_with_sticker(
        message.bot, message.chat.id, "custom_build",
        "🔤 یک نام (به لاتین، بدون فاصله و کاراکتر اضافه) برای سرویست وارد کن؛ فقط حروف انگلیسی و عدد:\nمثال: aminvpn1",
    )


@router.message(UserStates.waiting_custom_name)
async def custom_name_input(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or not _LATIN_NAME_RE.match(name):
        await message.answer(ui_editor.get_text("msg_0f396b0947", "❌ فقط حروف انگلیسی و عدد، بدون فاصله و بدون کاراکتر اضافه؛ دوباره وارد کن:"))
        return

    await state.update_data(custom_name=name)
    await state.set_state(None)
    await _show_custom_summary(message.bot, message.chat.id, message.from_user.id, state)


async def _show_custom_summary(bot, chat_id: int, user_id, state: FSMContext):
    data = await state.get_data()
    volume = data["custom_volume"]
    days = data["custom_days"]
    is_renew = data.get("custom_order_type") == "renew"
    if is_renew:
        settings = _renewal_settings(data.get("renew_category_id"), data.get("renew_plan_key"), data.get("renew_custom_option"))
        price = int(round(float(volume or 0) * int(settings.get("price_gb") or 0) + int(days or 0) * float(settings.get("price_day") or 0)))
        agent_discount_applied = False
    else:
        price, agent_discount_applied = _calc_custom_price(volume, days, user_id, option_num=int(data.get("custom_option_num") or 0))
    await state.update_data(custom_price=price)

    title = "🔁 خلاصه‌ی تمدید سرویس" if is_renew else "🛠 خلاصه‌ی سرویس سفارشی"

    text = f"{title}\n\n"
    if is_renew and data.get("renew_mode") == "time":
        text += f"⏳ افزایش زمان: {days} روز\n"
    elif is_renew and data.get("renew_mode") == "volume":
        text += f"🗜 افزایش حجم: {volume} گیگابایت\n"
    else:
        text += f"📦 حجم: {volume} گیگابایت\n⏳ مدت: {days} روز\n"
    if not is_renew:
        text += f"🔤 نام سرویس: {data['custom_name']}\n"
    text += f"\n💰 قیمت نهایی: {price:,} تومان"
    if agent_discount_applied:
        text += " (تخفیف نمایندگی اعمال شد)"
    text += "\n\nروش پرداخت را انتخاب کنید:"

    # 🧪 تست: از اینجا به بعد (مرحله‌ی انتخاب روش پرداخت و بعدش) دیگر هیچ استیکری
    # نشان داده نمی‌شود؛ این فراخوانی همچنین آخرین استیکر/منوی make.webm را پاک می‌کند.
    await show_menu_with_sticker(bot, chat_id, "cbuild_payment_method", text, reply_markup=(renew_payment_keyboard() if is_renew else custom_build_payment_keyboard()))


# ✅ دکمه‌ی «انتخاب روش پرداخت دیگر» روی صفحه‌ی پرداخت کارت‌به‌کارت سرویس سفارشی:
# فاکتور فعلی را منقضی می‌کند و دوباره صفحه‌ی خلاصه و انتخاب روش پرداخت را نمایش می‌دهد.
@router.callback_query(F.data == "cbuild_change_payment")
async def cbuild_change_payment(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    invoice_id = data.get("custom_card_invoice_id")
    if invoice_id:
        db.delete_invoice(invoice_id)
    await state.update_data(custom_card_invoice_id=None)
    await state.set_state(None)
    await _show_custom_summary(callback.bot, callback.message.chat.id, callback.from_user.id, state)
    await callback.answer()


@router.callback_query(F.data == "discount_cbuild")
async def discount_for_cbuild(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("custom_price") is None:
        await callback.answer(ui_editor.get_alert_text("msg_c0306266d7", "❌ اطلاعات منقضی شده؛ دوباره از اول شروع کن."), show_alert=True)
        return
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "discount_code_entry", "🎟 کد تخفیف خود را وارد کنید:")
    await state.set_state(UserStates.waiting_discount_cbuild)
    await callback.answer()


@router.message(UserStates.waiting_discount_cbuild)
async def check_discount_for_cbuild(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    discount = db.get_discount(code)
    data = await state.get_data()
    base_price = data.get("custom_price")
    _kb = renew_payment_keyboard if data.get("custom_order_type") == "renew" else custom_build_payment_keyboard

    if base_price is None:
        await message.answer(ui_editor.get_text("msg_792953088f", "❌ مشکلی پیش آمد، دوباره از منوی سرویس‌ها شروع کنید."), reply_markup=back_button("plans", "🔙 بازگشت", screen="discount_code_entry"))
        await state.set_state(None)
        return

    if discount is None or discount["uses"] <= 0 or db.discount_is_expired(discount):
        await message.answer(ui_editor.get_text("msg_3a34d9a47c", "❌ کد تخفیف نامعتبر یا تمام شده."), reply_markup=_kb())
        await state.set_state(None)
        return

    if not db.discount_allowed_for_user(discount, message.from_user.id):
        await message.answer(ui_editor.get_text("msg_9462130b1f", "❌ شما مجاز به استفاده از این کد تخفیف نیستید."), reply_markup=_kb())
        await state.set_state(None)
        return

    _scope = "renew" if data.get("custom_order_type") == "renew" else "custom"
    if not db.discount_applies_to_scope(discount, _scope):
        await message.answer(
            "❌ این کد روی " + ("تمدید سرویس" if _scope == "renew" else "سرویس سفارشی") + " قابل استفاده نیست.",
            reply_markup=_kb(),
        )
        await state.set_state(None)
        return

    _fp_user = db.get_user(message.from_user.id)
    if not db.discount_allowed_for_first_purchase(discount, _fp_user["id"] if _fp_user else 0):
        await message.answer(
            "❌ این کد مخصوص اولین خرید است و چون قبلاً از فروشگاه خرید کرده‌اید، برایتان قابل استفاده نیست.",
            reply_markup=_kb(),
        )
        await state.set_state(None)
        return

    if discount.get("max_uses_per_user"):
        user = db.get_user(message.from_user.id)
        if user and db.user_discount_uses(discount["id"], user["id"]) >= discount["max_uses_per_user"]:
            await message.answer(ui_editor.get_text("msg_4c8185f39a", "❌ سهمیه‌ی استفاده‌ی شما از این کد تمام شده."), reply_markup=_kb())
            await state.set_state(None)
            return

    if discount.get("min_order_amount") and base_price < discount["min_order_amount"]:
        await message.answer(
            f"❌ این کد فقط برای خریدهای بالای {discount['min_order_amount']:,} تومان قابل استفاده است.",
            reply_markup=_kb(),
        )
        await state.set_state(None)
        return

    final_price = db.compute_discount(discount, base_price)
    await state.update_data(custom_price=final_price, custom_discount_code=code)
    user = db.get_user(message.from_user.id)
    if user:
        db.use_discount(code, user["id"])
    await state.set_state(None)
    await message.answer(
        f"✅ کد تخفیف اعمال شد. قیمت جدید: {final_price:,} تومان",
        reply_markup=_kb(show_discount=False),
    )


@router.callback_query(F.data == "cbuild_pay_wallet")
async def cbuild_pay_wallet(callback: types.CallbackQuery, state: FSMContext):
    if is_duplicate_action(f"cbuildwalletbuy_{callback.from_user.id}") or not db.claim_purchase_action(f"tg-cbuild-wallet:{callback.from_user.id}:{callback.message.message_id}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    data = await state.get_data()
    volume, days, price = data.get("custom_volume"), data.get("custom_days"), data.get("custom_price")
    if volume is None or days is None or price is None:
        await callback.answer(ui_editor.get_alert_text("msg_82c9780032", "❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس‌ها شروع کنید."), show_alert=True)
        return

    if user["wallet"] < price:
        needed = price - user["wallet"]
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "cbuild_pay_wallet", 
            f"❌ موجودی کیف پول کافی نیست!\n\n💰 قیمت: {price:,} تومان\n"
            f"👛 موجودی: {user['wallet']:,} تومان\n⚠️ کمبود: {needed:,} تومان",
            reply_markup=insufficient_balance_keyboard(),
        )
        await callback.answer()
        return

    order_type = data.get("custom_order_type", "new")
    target_config_id = data.get("custom_target_config_id")
    custom_name = data.get("custom_name")

    success, referral_funded_purchase = db.deduct_wallet_for_purchase(user["id"], price, "خرید سرویس سفارشی" if order_type == "new" else "تمدید سرویس")
    if not success:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "cbuild_pay_wallet", 
            "❌ موجودی کافی نیست. ممکن است موجودی شما تغییر کرده باشد.",
            reply_markup=insufficient_balance_keyboard(),
        )
        await callback.answer()
        return

    order_id = db.create_custom_order(user["id"], volume, days, custom_name, price, order_type, target_config_id, renew_mode=data.get("renew_mode"), payment_method="کیف پول", option_num=data.get("custom_option_num") or 0)
    db.set_custom_order_status(order_id, "paid")

    if db.referral_purchase_qualifies(volume, paid_purchase=True, is_free_test=False):
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت)"

    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "cbuild_pay_wallet", WALLET_PAID_TEXT)

    # همون منطق VIP: با کیف‌پول/آنلاین، اگه شاهراه یک نگاشت پیش‌فرض برای این
    # بخش داشته باشه، بدون نیاز به انتخاب ادمین خودکار ساخته و ارسال می‌شه.
    handled = await auto_fulfill_custom_via_panel(callback.bot, user, order_id, volume, days, custom_name)
    if handled:
        if order_type == "renew" and target_config_id:
            # تمدید، منشأ مالی نسخه‌ی جدید سرویس را تعیین می‌کند؛
            # اگر این تمدید با کیف‌پول عادی/ترکیبی انجام شده باشد، سرویس دیگر
            # نباید هدف جریمه‌ی حجم رفرال بماند. اگر کل مبلغ از referral_wallet
            # آمده، همان سرویس دوباره رفرالی می‌شود.
            db.set_config_referral_funded(int(target_config_id), bool(referral_funded_purchase))
        elif referral_funded_purchase:
            db.mark_latest_config_referral_funded(user["id"], "vip")

    if not handled:
        await callback.bot.send_message(
            ADMIN_ID,
            f"🛠 {label}!\n\n"
            f"👤 {callback.from_user.full_name}\n🆔 {callback.from_user.id}\n"
            f"📦 حجم: {volume} گیگ\n⏳ مدت: {days} روز\n"
            + (f"🔤 نام: {custom_name}\n" if custom_name else "")
            + f"💰 {price:,} تومان (پرداخت‌شده از کیف پول)\n🔢 شماره سفارش: {order_id}",
            reply_markup=admin_custom_order_notify_keyboard(order_id),
        )
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "cbuild_pay_online")
async def cbuild_pay_online(callback: types.CallbackQuery, state: FSMContext):
    if not _uq_enabled():
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return
    if is_duplicate_action(f"cbuildonlinebuy_{callback.from_user.id}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    data = await state.get_data()
    volume, days, price = data.get("custom_volume"), data.get("custom_days"), data.get("custom_price")
    if volume is None or days is None or price is None:
        await callback.answer(ui_editor.get_alert_text("msg_82c9780032", "❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس‌ها شروع کنید."), show_alert=True)
        return

    order_type = data.get("custom_order_type", "new")
    target_config_id = data.get("custom_target_config_id")
    custom_name = data.get("custom_name")

    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))

    hash_id = uniquepay.new_hash_id("cbuild")
    invoice = await uniquepay.create_invoice(hash_id, price)
    if invoice is None or not uniquepay_cfg.pick_link(invoice):
        await alerts.report_uniquepay_create_failure(callback.bot, ADMIN_ID)
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "cbuild_pay_online", 
            "❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست. لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید.",
            reply_markup=custom_build_payment_keyboard(),
        )
        return

    payment_link = uniquepay_cfg.pick_link(invoice)
    alerts.report_uniquepay_create_success()

    extra_payload = json.dumps({
        "volume": volume, "days": days, "custom_name": custom_name,
        "order_type": order_type, "target_config_id": target_config_id, "renew_mode": data.get("renew_mode"),
        "option_num": data.get("custom_option_num") or 0,
    })
    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(callback.from_user.id),
        hash_id=hash_id,
        plan_name=custom_name or "سرویس سفارشی (بساز سرویس خودت)",
        price=price,
        order_type="custom",
        plan_key=None,
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
        kind="custom",
        extra=extra_payload,
    )

    white_label = uniquepay.white_label_data(invoice)
    payable_text = (
        f"💰 مبلغ دقیق واریز: {int(white_label['payableAmount']):,} تومان"
        if white_label and str(white_label['payableAmount']).isdigit()
        else f"💰 مبلغ سفارش: {price:,} تومان"
    )
    card_text = f"\n💳 شماره کارت مقصد: {white_label['cardNumber']}\n" if white_label else ""
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "cbuild_pay_online", 
        progress_bar(2, 3) +
        f"🌐 پرداخت آنلاین (کارت‌به‌کارت خودکار)\n\n"
        f"🧩 سرویس سفارشی — {volume} گیگ / {days} روز\n"
        + payable_text + card_text + "\n"
        f"روی دکمه‌ی پرداخت بزنید یا از اطلاعات وایت‌لیبل بالا استفاده کنید؛ سپس همینجا روی «بررسی کن» بزنید.\n"
        f"⏱ به‌محض تأیید نهایی UniquePay، سفارش شما به‌طور خودکار ثبت می‌شود.\n\n"
        f"⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است. اگر تا این مهلت پرداخت تایید نشود، به‌طور خودکار منقضی و حذف خواهد شد.",
        reply_markup=online_payment_keyboard(payment_link, payment_id, white_label, "cbuild_start"),
    )


@router.callback_query(F.data == "cbuild_pay_card")
async def cbuild_pay_card(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    price = data.get("custom_price")
    if price is None:
        await callback.answer(ui_editor.get_alert_text("msg_82c9780032", "❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس‌ها شروع کنید."), show_alert=True)
        return

    # 🎲 برای پیشگیری از مسدودی کارت به‌خاطر واریزی‌های زیاد با مبلغ یکسان، فقط برای همین
    # فاکتور کارت‌به‌کارت یک مبلغ کمی رندوم‌شده (± ۱۰۰ تا ۱۵۰۰ تومان) می‌سازیم؛ قیمت پایه
    # (custom_price) در state دست‌نخورده باقی می‌ماند تا روش پرداخت دیگری (کیف پول/آنلاین) انتخاب شود.
    invoice_price = apply_random_amount_variation(price)

    invoicing_user = db.get_user(callback.from_user.id)
    invoice = db.create_invoice(
        user_id=invoicing_user["id"] if invoicing_user else None,
        telegram_id=str(callback.from_user.id),
        kind="custom_card",
        label="سرویس سفارشی",
        price=invoice_price,
    )
    deadline_str = format_deadline_time(invoice["expires_at"])
    await state.update_data(custom_card_invoice_id=invoice["id"], custom_card_invoice_price=invoice_price)
    await state.set_state(UserStates.waiting_custom_card_receipt)
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "cbuild_pay_card", 
        progress_bar(2, 3) +
        f"💳 پرداخت کارت به کارت\n\n"
        f"💰 مبلغ قابل پرداخت: {invoice_price:,} تومان\n\n"
        f"💳 شماره کارت:\n{bot_info.get('card_number')}\n\n"
        f"👤 به نام: {bot_info.get('card_holder')}\n\n"
        f"📸 پس از واریز، عکس رسید پرداخت را همینجا ارسال کنید.\n\n"
        f"⏱ این شماره کارت و قیمت تا ساعت {deadline_str} (۳۰ دقیقه) معتبر است. لطفاً تا این ساعت رسید پرداخت را ارسال کنید، وگرنه این فاکتور به‌طور خودکار منقضی و حذف می‌شود.\n\n"
        f"*⚠️ دقیقاً همین مبلغ ({invoice_price:,} تومان) رو واریز کنید تا پرداخت شما شناسایی و بلافاصله تایید بشه.*",
        parse_mode="Markdown",
        append_note=invoice_expiry_note("custom", deadline_str),
        reply_markup=card_payment_actions_keyboard(bot_info.get('card_number'), invoice_price, "cbuild_change_payment"),
    )
    await callback.answer()


@router.message(UserStates.waiting_custom_card_receipt, F.photo | F.document)
async def custom_receive_receipt(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    user = db.get_user(uid)
    data = await state.get_data()
    volume, days, price = data.get("custom_volume"), data.get("custom_days"), data.get("custom_price")
    order_type = data.get("custom_order_type", "new")
    target_config_id = data.get("custom_target_config_id")
    custom_card_invoice_id = data.get("custom_card_invoice_id")
    custom_name = data.get("custom_name")
    # 🎲 مبلغی که واقعاً روی فاکتور نمایش داده شده (رندوم‌شده) همان چیزی است که
    # باید به‌عنوان مبلغ نهایی سفارش/رسید ثبت شود، نه قیمت پایه‌ی custom_price.
    invoice_price = data.get("custom_card_invoice_price", price)

    file_id, file_type, _txt = extract_receipt_media(message)
    if file_id is None:
        await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))
        return

    if user is None or volume is None or days is None or price is None:
        await message.answer(ui_editor.get_text("msg_82c9780032", "❌ مشکلی پیش آمد، لطفاً دوباره از منوی سرویس‌ها شروع کنید."))
        await state.clear()
        return

    if not custom_card_invoice_id or db.consume_invoice(custom_card_invoice_id) is None:
        await message.answer(
            ui_editor.get_text("msg_1a379a9932", "⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید.")
        )
        await state.clear()
        return

    order_id = db.create_custom_order(user["id"], volume, days, custom_name, invoice_price, order_type, target_config_id, renew_mode=data.get("renew_mode"), payment_method="کارت به کارت", option_num=data.get("custom_option_num") or 0)
    if custom_card_invoice_id:
        db.delete_invoice(custom_card_invoice_id)


    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت)"
    try:
        db.set_custom_order_receipt(order_id, file_id=file_id)
    except Exception:
        logger.exception("ذخیره‌ی رسید سفارش سفارشی ناموفق بود")
    admin_caption = (
        f"💳 رسید {label}\n\n"
        f"👤 {receipt_user_label(message.from_user)}\n🆔 {uid}\n"
        f"📦 حجم: {volume} گیگ\n⏳ مدت: {days} روز\n"
        + (f"🔤 نام: {custom_name}\n" if custom_name else "")
        + f"💰 {invoice_price:,} تومان\n🔢 شماره سفارش: {order_id}"
    )
    delivered = await send_receipt_card_to_admin(
        message.bot, admin_caption,
        reply_markup=admin_custom_order_card_approval_keyboard(order_id),
        file_id=file_id, file_type=file_type,
    )
    if not delivered:
        logger.error("رسید سفارش سفارشی %s برای ادمین ارسال نشد", order_id)
    tracking_code = order_id
    await message.answer(
        f"✅ رسید پرداخت با موفقیت دریافت شد\n\n"
        f"🧾 کد پیگیری: `{tracking_code}`\n"
        f"📦سرویس: {label}\n"
        f"💳 مبلغ: {invoice_price:,} تومان\n"
        f"🟡 وضعیت: در حال بررسی\n\n"
        f"رسید شما ثبت شده و نیازی به ارسال مجدد آن نیست.\n\n"
        f"پس از تأیید پرداخت، لینک سرویس و آموزش اتصال از طریق همین ربات برایت ارسال می‌شود.",
        parse_mode="Markdown",
        reply_markup=receipt_submitted_keyboard(),
    )
    await state.clear()


@router.message(UserStates.waiting_custom_card_receipt)
async def custom_receipt_wrong_format(message: types.Message):
    await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))


async def _start_payg_purchase(callback: types.CallbackQuery, state: FSMContext, plan_key: str | None, option_num: int | None = None):
    # پاسخ فوری به Callback تا Telegram Spinner معطل DB/پنل نشود.
    try:
        await callback.answer()
    except Exception:
        pass
    if not db.is_orders_enabled():
        await callback.answer(ORDERS_CLOSED_TEXT, show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    settings = db.get_payg_settings()
    if not settings.get("enabled"):
        await callback.answer("❌ سرویس PAYG در حال حاضر غیرفعال است.", show_alert=True)
        return

    _payg_types = ("marzban", "pasargad", "pasargad_wg", "threexui")
    if option_num is None:
        opts = db.usable_map_options("payg", _payg_types)
        if len(opts) > 1:
            await state.update_data(payg_pending_plan=plan_key)
            rows = [[_UB(
                text=f"{o['name']} — {db.payg_price_for_user(callback.from_user.id, o['num'], db.resolve_payg_price_per_gb(plan_key, (db.get_effective_plan(plan_key) or {}).get('category_id') if plan_key else None, option_num=o['num'])):,} تومان/GB",
                callback_data=f"paygopt_{o['num']}", style="primary")] for o in opts]
            rows.append([_UB(text="🔙 انصراف", callback_data="plans", style="danger", ui_screen="payg_options", ui_button_key="back")])
            await show_menu_with_sticker(
                callback.bot, callback.message.chat.id, "plan_pay_wallet",
                "⚡ سرویس PAYG\n\nیکی از گزینه‌های زیر را انتخاب کن:",
                reply_markup=_UM(inline_keyboard=rows),
            )
            return
        option_num = opts[0]["num"] if opts else 0
    mapping = db.get_option_mapping("payg", option_num)
    if not mapping:
        await callback.answer("❌ پنل پیش‌فرض PAYG هنوز برای ساخت سرویس تنظیم نشده است.", show_alert=True)
        return
    panel = db.get_vpn_panel(mapping["panel_id"]) if mapping.get("panel_id") else None
    if not panel or not panel.get("enabled"):
        await callback.answer("❌ پنل PAYG در حال حاضر در دسترس نیست.", show_alert=True)
        return
    if panel.get("panel_type") not in ("marzban", "pasargad", "pasargad_wg", "threexui"):
        await callback.answer("❌ PAYG فقط روی پنل‌های مرزبان، پاسارگارد یا 3X-UI قابل راه‌اندازی است.", show_alert=True)
        return

    min_balance = int(settings.get("min_balance") or 0)
    if int(user.get("wallet") or 0) < min_balance:
        needed = max(0, min_balance - int(user.get("wallet") or 0))
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "plan_pay_wallet",
            f"⚡ سرویس PAYG\n\n"
            f"برای ساخت سرویس PAYG باید حداقل {min_balance:,} تومان در کیف پولت داشته باشی.\n"
            f"👛 موجودی فعلی: {int(user.get('wallet') or 0):,} تومان\n"
            f"💳 کسری تا حداقل: {needed:,} تومان",
            reply_markup=insufficient_balance_keyboard(),
        )
        return

    plan = db.get_effective_plan(plan_key) if plan_key else None
    category_id = plan.get("category_id") if plan else None
    price_per_gb = db.resolve_payg_price_per_gb(plan_key, category_id, option_num=int(option_num or 0))
    price_per_gb = db.payg_price_for_user(callback.from_user.id, int(option_num or 0), price_per_gb)
    await state.clear()
    await state.update_data(payg_plan_key=plan_key, payg_price_per_gb=price_per_gb, payg_option_num=int(option_num or 0))
    await state.set_state(UserStates.waiting_payg_name)
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "custom_build",
        progress_bar(1, 2) +
        "⚡ ساخت سرویس PAYG\n\n"
        "این سرویس با حجم و زمان نامحدود ساخته می‌شود و فقط تا وقتی فعال می‌ماند که کیف پولت موجودی کافی داشته باشد.\n\n"
        f"💰 نرخ فعلی: {price_per_gb:,} تومان برای هر GB مصرف\n"
        f"👛 حداقل موجودی لازم برای فعال‌ماندن: {min_balance:,} تومان\n\n"
        "حالا یک نام کاربری لاتین برای سرویس بفرست (فقط حروف انگلیسی و عدد، بدون فاصله):\n"
        "مثال: aminpayg1",
        reply_markup=payg_name_keyboard(),
    )


@router.callback_query(F.data.startswith("paygopt_"))
async def payg_pick_option(callback: types.CallbackQuery, state: FSMContext):
    try:
        num = int(callback.data.replace("paygopt_", ""))
    except ValueError:
        await callback.answer("❌ گزینه نامعتبر است.", show_alert=True)
        return
    data = await state.get_data()
    await _start_payg_purchase(callback, state, data.get("payg_pending_plan"), option_num=num)


@router.callback_query(F.data == "payg_buy")
async def payg_buy_start(callback: types.CallbackQuery, state: FSMContext):
    await _start_payg_purchase(callback, state, None)


@router.callback_query(F.data.startswith("payg_buy_"))
async def payg_buy_from_plan(callback: types.CallbackQuery, state: FSMContext):
    await _start_payg_purchase(callback, state, callback.data.replace("payg_buy_", "", 1))


@router.message(UserStates.waiting_payg_name)
async def payg_name_input(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or not _LATIN_NAME_RE.match(name):
        await message.answer(ui_editor.get_text("msg_0f396b0947", "❌ فقط حروف انگلیسی و عدد، بدون فاصله و بدون کاراکتر اضافه؛ دوباره وارد کن:"))
        return
    await _payg_create_with_name(message.bot, message.chat.id, message.from_user.id, state, name)


@router.callback_query(F.data == "payg_name_auto", UserStates.waiting_payg_name)
async def payg_name_auto(callback: types.CallbackQuery, state: FSMContext):
    """دکمه‌ی «انتخاب خودکار» مرحله‌ی نام PAYG: همان منطق نامگذاری خودکار (یوزرنیم کاربر یا پیشوند پیش‌فرض + پسوند)."""
    data = await state.get_data()
    mapping = db.get_option_mapping("payg", data.get("payg_option_num") or 0)
    panel = db.get_vpn_panel(mapping["panel_id"]) if mapping and mapping.get("panel_id") else None
    try:
        name = await _finalize_service_name(data.get("payg_plan_key") or "payg", _auto_service_name(callback), panel=panel, allow_short=True)
    except Exception as exc:
        await callback.answer(f"❌ ساخت نام یکتا ناموفق بود: {exc}", show_alert=True)
        return
    await callback.answer()
    await _payg_create_with_name(callback.bot, callback.message.chat.id, callback.from_user.id, state, name)


async def _payg_create_with_name(bot, chat_id: int, uid: int, state: FSMContext, name: str):
    user = db.get_user(uid)
    if user is None:
        await state.clear()
        await bot.send_message(chat_id, ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    settings = db.get_payg_settings()
    min_balance = int(settings.get("min_balance") or 0)
    if int(user.get("wallet") or 0) < min_balance:
        await state.clear()
        await bot.send_message(chat_id, 
            f"❌ موجودی کیف پولت برای فعال‌سازی PAYG کافی نیست. حداقل لازم: {min_balance:,} تومان",
            reply_markup=insufficient_balance_keyboard(),
        )
        return

    data = await state.get_data()
    plan_key = data.get("payg_plan_key")
    price_per_gb = int(data.get("payg_price_per_gb") or 0)
    mapping = db.get_option_mapping("payg", data.get("payg_option_num") or 0)
    panel = db.get_vpn_panel(mapping["panel_id"]) if mapping and mapping.get("panel_id") else None
    if not mapping or not panel or panel.get("panel_type") not in ("marzban", "pasargad", "pasargad_wg", "threexui"):
        await state.clear()
        await bot.send_message(chat_id, "❌ پنل PAYG برای ساخت سرویس آماده نیست. از ادمین بخواه نگاشت این بخش را بررسی کند.")
        return

    await bot.send_message(chat_id, "⏳ در حال ساخت سرویس PAYG...")
    ok, link, remote_service_id, raw_data, err = await panels.create_service(
        panel, name, mapping["remote_ref"], volume_gb=0, days=0,
    )
    if not ok or not link:
        await state.clear()
        await bot.send_message(chat_id, f"❌ ساخت سرویس PAYG ناموفق بود:\n{err or 'بدون جزئیات'}")
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ ساخت PAYG برای {uid} ناموفق بود:\n{err or raw_data}")
        except Exception:
            pass
        return

    ctx = {
        "uid": str(uid),
        "plan_key": plan_key,
        "order_id": None,
        "order_kind": "payg",
        "panel_id": panel["id"],
        "panel_type": panel["panel_type"],
        "service_id": remote_service_id,
        "snapshot": {"name": name, "volume_gb": 0, "days": 0},
        "config_type": "payg",
        "payg_price_per_gb": price_per_gb,
    }
    await _deliver_panel_link(bot, ctx, link)
    await bot.send_message(chat_id, 
        f"⚡ سرویس PAYG با موفقیت ساخته شد.\n"
        f"💰 نرخ این سرویس: {price_per_gb:,} تومان برای هر GB مصرف\n"
        "👛 تا وقتی موجودی کیف پولت تمام نشود (صفر نشود) سرویس فعال می‌ماند و مصرف‌ات از کیف پول کم می‌شود.",
    )
    # لاگ ادمین/کانال همین حالا توسط _deliver_panel_link با قالب «خرید» ارسال شده (بدون پیام تکراری)
    await state.clear()
