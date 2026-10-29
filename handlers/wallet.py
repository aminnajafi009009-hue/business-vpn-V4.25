"""
handlers/wallet.py
نمایش کیف پول (موجودی آزاد / موجودی در انتظار)، تاریخچه تراکنش‌ها،
و فرایند شارژ کیف پول (انتخاب مبلغ یا مبلغ دلخواه + ارسال رسید).
"""

import ui_editor


import logging

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

import database as db
import notif_settings
import uniquepay
import uniquepay_cfg
import online_gateways as og
import alerts
import bot_info
from utils import invoice_expiry_note, is_duplicate_action, format_deadline_time, progress_bar, apply_random_amount_variation
from utils import send_user_notification
from utils import show_menu_with_sticker, extract_receipt_media, send_receipt_card_to_admin, receipt_user_label
from states import UserStates
from config import (
    ADMIN_ID,
    ONLINE_PAYMENT_MIN_AMOUNT,
)
from keyboards import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    wallet_menu,
    charge_amount_keyboard,
    charge_payment_method_keyboard,
    charge_crypto_select_keyboard,
    charge_crypto_detail_keyboard,
    online_payment_wallet_keyboard,
    back_button,
    admin_charge_approval_keyboard,
    card_payment_actions_keyboard,
    receipt_submitted_keyboard,
)

logger = logging.getLogger(__name__)

router = Router(name="wallet")


def _wallet_topup_error(amount: int) -> str | None:
    """اعتبارسنجی متمرکز مبلغ شارژ کیف پول (تومان).
    💡 حداقل/حداکثر دیگر مقدار ثابت در .env نیستند؛ از تنظیمات «مدیریت شارژ
    کیف‌پول» در دیتابیس خوانده می‌شوند تا ادمین بتواند بدون ری‌دیپلوی تغییرشان دهد."""
    limits = db.get_wallet_settings()
    if amount < limits["min_topup"]:
        return f"❌ حداقل مبلغ شارژ کیف پول {limits['min_topup']:,} تومان است."
    if amount > limits["max_topup"]:
        return f"❌ حداکثر مبلغ شارژ کیف پول {limits['max_topup']:,} تومان است."
    return None


# --- شارژ با کد تخفیف: کاربر «پرداخت» را کم‌شده می‌پردازد ولی «مبلغ اصلی» شارژ می‌شود ---------
# وضعیت در تنظیمات دیتابیس نگه داشته می‌شود (تا با ری‌استارت ربات از بین نرود) و فقط وقتی اعمال
# می‌شود که مبلغ پرداخت دقیقاً همان مبلغ تخفیف‌خورده باشد.
_WD_TTL_SECONDS = 2 * 3600


def wd_set(tg_id, code: str, credit: int, pay: int) -> None:
    import json as _json, time as _time
    db.set_setting(f"wallet_disc:{tg_id}", _json.dumps({"code": code, "credit": int(credit), "pay": int(pay), "ts": int(_time.time())}))


def wd_clear(tg_id) -> None:
    try:
        if db.get_setting(f"wallet_disc:{tg_id}"):
            db.set_setting(f"wallet_disc:{tg_id}", "")
    except Exception:
        pass


def wd_get(tg_id, pay: int | None = None) -> dict | None:
    import json as _json, time as _time
    try:
        raw = db.get_setting(f"wallet_disc:{tg_id}")
        if not raw:
            return None
        d = _json.loads(raw)
        if _time.time() - int(d.get("ts", 0)) > _WD_TTL_SECONDS:
            return None
        if pay is not None and int(d.get("pay", -1)) != int(pay):
            return None
        return d
    except Exception:
        return None


def wd_receipt_args(tg_id, amount: int) -> dict:
    """آرگومان‌های discount_code/extra برای ساخت رسید/فاکتور شارژ (خالی اگر تخفیفی در کار نیست)."""
    d = wd_get(tg_id, amount)
    if not d:
        return {}
    return {"discount_code": d["code"], "extra": f"wd:{int(d['credit'])}"}


async def _reject_invalid_wallet_topup(target, amount: int) -> bool:
    """اگر مبلغ خارج از بازه باشد پیام خطا می‌دهد و True برمی‌گرداند.
    در شارژ با کد تخفیف، بازه روی «مبلغ اصلی شارژ» سنجیده می‌شود نه مبلغ تخفیف‌خورده."""
    _d = wd_get(target.from_user.id, amount)
    error = _wallet_topup_error(int(_d["credit"]) if _d else amount)
    if not error:
        return False
    if isinstance(target, types.CallbackQuery):
        await target.answer(error, show_alert=True)
    else:
        await target.answer(error)
    return True


def _get_user_row(telegram_id):
    """کاربر را برمی‌گرداند؛ اگر هنوز ساخته نشده، می‌سازد (محافظتی)."""
    user = db.get_user(telegram_id)
    if user is None:
        return None
    return user


@router.callback_query(F.data == "wallet")
async def wallet_overview(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    text = (
        f"💰 کیف پول شما\n\n"
        f"💰 موجودی قابل استفاده: {user['wallet']:,} تومان\n"
        f"🔒 موجودی در انتظار: {user['locked_wallet']:,} تومان\n\n"
        + (f"ℹ️ موجودی در انتظار، پس از خرید حجم {db.get_referral_settings()['min_volume_gb']} گیگ یا بیشتر توسط فردی که با لینک شما عضو شده، به‌صورت خودکار آزاد می‌شود."
         if db.get_referral_settings()["paid_purchase_required"] and db.get_referral_settings()["min_volume_enabled"]
         else ("ℹ️ پاداش دعوت پس از هر خرید پولی توسط فرد دعوت‌شده به‌صورت خودکار آزاد می‌شود."
               if db.get_referral_settings()["paid_purchase_required"]
               else "ℹ️ پاداش دعوت بدون نیاز به خرید، بلافاصله پس از عضویت فرد دعوت‌شده آزاد می‌شود."))
    )
    _ref_off_rx = (None if db.referral_enabled() else (r"ℹ️|پاداش دعوت" if int(user["locked_wallet"] or 0) > 0 else r"موجودی در انتظار|ℹ️|پاداش دعوت"))
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet", text, reply_markup=wallet_menu(), drop_lines_regex=_ref_off_rx)
    await callback.answer()


@router.callback_query(F.data == "wallet_free")
async def wallet_free(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    text = f"💰 موجودی قابل استفاده شما\n\n{user['wallet']:,} تومان\n\nاین مبلغ را می‌توانید برای خرید سرویس استفاده کنید."
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_free", text, reply_markup=back_button("profile", "🔙 بازگشت", screen="wallet_free"))
    await callback.answer()


@router.callback_query(F.data == "wallet_locked")
async def wallet_locked(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    if not db.referral_enabled():
        await callback.answer("این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    text = (
        f"🔒 موجودی در انتظار شما\n\n{user['locked_wallet']:,} تومان\n\n"
        + (f"این مبلغ از دعوت دوستان به‌دست آمده و پس از خرید حجم {db.get_referral_settings()['min_volume_gb']} گیگ یا بیشتر توسط آن‌ها، به‌صورت خودکار به موجودی قابل‌استفاده شما اضافه می‌شود."
         if db.get_referral_settings()["paid_purchase_required"] and db.get_referral_settings()["min_volume_enabled"]
         else ("این مبلغ پس از هر خرید پولی فرد دعوت‌شده آزاد می‌شود."
               if db.get_referral_settings()["paid_purchase_required"]
               else "این مبلغ بدون نیاز به خرید فرد دعوت‌شده، پس از عضویت او آزاد می‌شود."))
    )
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_locked", text, reply_markup=back_button("profile", "🔙 بازگشت", screen="wallet_locked"))
    await callback.answer()


@router.callback_query(F.data == "transactions")
async def transactions(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    txs = db.get_transactions(user["id"], limit=10)
    if not txs:
        text = "📋 هنوز تراکنشی ندارید."
    else:
        icon_map = {
            "charge": "✅",
            "purchase": "🛒",
            "referral_locked": "🔒",
            "referral_release": "🔓",
        }
        # نوع‌هایی که واقعاً واریز به حساب هستند (سبز/+)؛ بقیه خروج از حساب یا
        # در انتظار محسوب می‌شوند (قرمز/بدون علامت). این دقیقاً همان منطقی است
        # که Mini App (webapp_api._signed_tx_amount) استفاده می‌کند تا نمایش
        # تراکنش‌ها بین ربات و Mini App یکسان باشد.
        positive_types = ("charge", "referral_release")
        text = "📋 تراکنش‌های اخیر:\n\n"
        for tx in txs:
            icon = icon_map.get(tx["type"], "•")
            sign = "+" if tx["type"] in positive_types else "-"
            text += f"{icon} {tx['description']} | {sign}{tx['amount']:,} تومان | {tx['created_at']}\n"

    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_transactions", text, reply_markup=back_button("wallet", "🔙 بازگشت", screen="wallet_transactions"))
    await callback.answer()


# ---------------------------------------------------------------------------
# فرایند شارژ کیف پول
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "charge")
async def charge(callback: types.CallbackQuery):
    wd_clear(callback.from_user.id)
    limits = db.get_wallet_settings()
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_charge", 
        f"💳 مبلغ شارژ را انتخاب کنید:\n\n📌 حداقل شارژ: {limits['min_topup']:,} تومان\n📌 حداکثر شارژ: {limits['max_topup']:,} تومان",
        reply_markup=charge_amount_keyboard(limits["quick_amounts"])
    )
    await callback.answer()


async def _offer_charge_payment_method(target, amount: int, state: FSMContext):
    """پس از مشخص‌شدن مبلغ شارژ، روش‌های پرداخت فعال برای «شارژ کیف پول» را نشان می‌دهد
    (کارت‌به‌کارت، آنلاین، ارز دیجیتال؛ هر کدام با سوییچ اختصاصی شارژ در تنظیمات روش‌های پرداخت).
    اگر فقط یک روش در دسترس باشد مستقیم همان روش شروع می‌شود."""
    if await _reject_invalid_wallet_topup(target, amount):
        await state.clear()
        return

    is_cb = isinstance(target, types.CallbackQuery)
    bot_ = target.bot
    chat_id = target.message.chat.id if is_cb else target.chat.id
    tg_id = target.from_user.id

    methods = list(db.available_topup_methods())
    if "online" in methods and amount <= ONLINE_PAYMENT_MIN_AMOUNT:
        methods.remove("online")
    # درگاه‌های جدید فقط برای مبلغ‌های بالاتر از حداقل خودشان پیشنهاد می‌شوند
    methods = [m for m in methods if not (og.is_new_gateway(m) and not og.amount_ok(m, amount))]

    if not methods:
        await show_menu_with_sticker(
            bot_, chat_id, "walletcharge_method",
            "❌ در حال حاضر هیچ روش پرداختی برای شارژ کیف پول فعال نیست. لطفاً بعداً دوباره تلاش کنید.",
            reply_markup=charge_payment_method_keyboard(amount, []),
        )
        await state.clear()
        return

    if len(methods) == 1 and methods[0] == "card":
        # فقط کارت‌به‌کارت: مثل قبل مستقیم؛ فاکتور (با مهلت ۳۰ دقیقه) همین‌جا ساخته می‌شود.
        await _start_wallet_card_payment(bot_, chat_id, tg_id, amount, state)
        return

    _wd = wd_get(tg_id, amount)
    text = (
        f"💳 مبلغ شارژ: {int(_wd['credit']):,} تومان\n🎟 مبلغ قابل پرداخت (با کد {_wd['code']}): {amount:,} تومان\n\n💰 روش پرداخت را انتخاب کنید:"
        if _wd else f"💳 مبلغ: {amount:,} تومان\n\n💰 روش پرداخت را انتخاب کنید:"
    )
    await show_menu_with_sticker(
        bot_, chat_id, "walletcharge_method", text,
        reply_markup=charge_payment_method_keyboard(amount, methods),
    )


# ---------------------------------------------------------------------------
# شارژ کیف پول با کد تخفیف: ۱) مبلغ شارژ ← ۲) کد تخفیف ← ۳) پرداخت مبلغ تخفیف‌خورده
# (همان مبلغ اولیه به کیف پول اضافه می‌شود)
# ---------------------------------------------------------------------------
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789", ",٬ ")


def _wd_amount_keyboard(amounts):
    rows, row = [], []
    for a in (amounts or []):
        row.append(InlineKeyboardButton(text=f"{int(a):,} تومان", callback_data=f"wdamt_{int(a)}", style="primary"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data="wallet", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "use_discount")
async def use_discount(callback: types.CallbackQuery, state: FSMContext):
    wd_clear(callback.from_user.id)
    await state.clear()
    limits = db.get_wallet_settings()
    await state.set_state(UserStates.waiting_wd_amount)
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "discount_code_entry",
        "🎟 شارژ کیف پول با کد تخفیف\n\n"
        "💵 چه مبلغی می‌خواهید کیف پولتان را شارژ کنید؟ یکی از مبلغ‌ها را انتخاب کنید یا مبلغ دلخواه را (به تومان) بفرستید.\n"
        "در مرحله‌ی بعد کد تخفیف را وارد می‌کنید؛ تخفیف از مبلغ پرداختی کم می‌شود ولی همین مبلغ کامل به کیف پولتان اضافه می‌شود.\n\n"
        f"📌 حداقل شارژ: {limits['min_topup']:,} تومان\n📌 حداکثر شارژ: {limits['max_topup']:,} تومان",
        reply_markup=_wd_amount_keyboard(limits["quick_amounts"]),
    )
    await callback.answer()


async def _wd_ask_code(target, state: FSMContext, amount: int):
    if _wallet_topup_error(amount):
        await (target.answer(_wallet_topup_error(amount), show_alert=True) if isinstance(target, types.CallbackQuery) else target.answer(_wallet_topup_error(amount)))
        return
    await state.update_data(wd_amount=int(amount))
    await state.set_state(UserStates.waiting_wd_code)
    bot_ = target.bot
    chat_id = target.message.chat.id if isinstance(target, types.CallbackQuery) else target.chat.id
    await show_menu_with_sticker(
        bot_, chat_id, "discount_code_entry",
        f"💳 مبلغ شارژ: {int(amount):,} تومان\n\n🎟 حالا کد تخفیف خود را وارد کنید:",
        reply_markup=back_button("wallet", "🔙 انصراف", screen="discount_code_entry"),
    )


@router.callback_query(F.data.startswith("wdamt_"), UserStates.waiting_wd_amount)
async def wd_amount_pick(callback: types.CallbackQuery, state: FSMContext):
    try:
        amount = int(callback.data.replace("wdamt_", ""))
    except ValueError:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    await _wd_ask_code(callback, state, amount)
    try:
        await callback.answer()
    except Exception:
        pass


@router.message(UserStates.waiting_wd_amount)
async def wd_amount_text(message: types.Message, state: FSMContext):
    txt = (message.text or "").translate(_FA_DIGITS)
    if not txt.isdigit():
        await message.answer("❌ فقط عدد (به تومان) ارسال کنید.")
        return
    await _wd_ask_code(message, state, int(txt))


@router.message(UserStates.waiting_wd_code)
async def wd_code_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = int(data.get("wd_amount") or 0)
    back = back_button("wallet", "🔙 بازگشت", screen="discount_code_entry")
    if not amount or _wallet_topup_error(amount):
        await message.answer("❌ مشکلی پیش آمد، لطفاً دوباره از منوی کیف پول شروع کنید.", reply_markup=back)
        await state.clear()
        return
    code = (message.text or "").strip().upper()
    discount = db.get_discount(code) if code else None
    user = db.get_user(message.from_user.id)
    err = None
    if discount is None or discount["uses"] <= 0 or db.discount_is_expired(discount):
        err = ui_editor.get_text("msg_3a34d9a47c", "❌ کد تخفیف نامعتبر یا تمام شده.")
    elif not db.discount_allowed_for_user(discount, message.from_user.id):
        err = ui_editor.get_text("msg_9462130b1f", "❌ شما مجاز به استفاده از این کد تخفیف نیستید.")
    elif not db.discount_allowed_for_first_purchase(discount, user["id"] if user else 0):
        err = "❌ این کد مخصوص اولین خرید است و چون قبلاً از فروشگاه خرید کرده‌اید، برایتان قابل استفاده نیست."
    elif discount.get("max_uses_per_user") and user and db.user_discount_uses(discount["id"], user["id"]) >= discount["max_uses_per_user"]:
        err = ui_editor.get_text("msg_4c8185f39a", "❌ سهمیه‌ی استفاده‌ی شما از این کد تمام شده.")
    elif not db.discount_applies_to_wallet(discount):
        err = "❌ این کد تخفیف روی شارژ کیف پول قابل استفاده نیست."
    if err:
        await message.answer(err, reply_markup=back)
        await state.clear()
        return
    pay = int(db.compute_discount(discount, amount))
    if pay >= amount:
        await message.answer("❌ این کد روی این مبلغ تخفیفی ایجاد نمی‌کند.", reply_markup=back)
        await state.clear()
        return
    if pay <= 0:
        await message.answer("❌ تخفیف این کد از مبلغ شارژ بیشتر است؛ لطفاً مبلغ بالاتری انتخاب کنید.", reply_markup=back)
        await state.clear()
        return
    wd_set(message.from_user.id, discount["code"], amount, pay)
    await state.clear()
    await message.answer(
        f"✅ کد تخفیف ثبت شد\n\n💳 مبلغ شارژ: {amount:,} تومان\n🎟 تخفیف: {amount - pay:,} تومان\n"
        f"💰 مبلغی که می‌پردازید: {pay:,} تومان\n\nبعد از پرداخت، {amount:,} تومان به کیف پولتان اضافه می‌شود."
    )
    await _offer_charge_payment_method(message, pay, state)


@router.callback_query(F.data.startswith("charge_"))
async def charge_amount(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.replace("charge_", "")

    if action == "custom":
        limits = db.get_wallet_settings()
        await state.set_state(UserStates.waiting_custom_charge)
        # متن قابل ویرایش (کلید charge_custom_prompt) + خطوط حداقل/حداکثر؛ ایموجی پریمیوم حفظ می‌شود
        _cp_default = "💵 مبلغ دلخواه را به تومان ارسال کنید:"
        _cp_text = ui_editor.get_text("charge_custom_prompt", _cp_default) or _cp_default
        _cp_ents = ui_editor.get_entities("charge_custom_prompt") or []
        _cp_text = _cp_text.rstrip()
        _cp_ents = [e for e in _cp_ents if e.get("offset", 0) + e.get("length", 0) <= len(_cp_text.encode("utf-16-le")) // 2]
        _cp_text += f"\n\n📌 حداقل: {limits['min_topup']:,} تومان\n📌 حداکثر: {limits['max_topup']:,} تومان"
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "wallet_charge",
            _cp_text, skip_ui_text=True, text_entities=_cp_ents or None,
        )
    else:
        amount = int(action)
        await _offer_charge_payment_method(callback, amount, state)
    await callback.answer()


@router.message(UserStates.waiting_custom_charge)
async def custom_charge_amount(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer(ui_editor.get_text("msg_39be0509db", "❌ فقط عدد ارسال کنید."))
        return

    amount = int(message.text)
    await _offer_charge_payment_method(message, amount, state)


async def _start_wallet_card_payment(bot_, chat_id: int, tg_user_id: int, amount: int, state: FSMContext):
    """صفحه‌ی پرداخت کارت‌به‌کارت شارژ کیف پول: فاکتور ۳۰ دقیقه‌ای می‌سازد و متن درخواست رسید
    (شامل مهلت ۳۰ دقیقه، مثل بقیه‌ی فاکتورها) را نشان می‌دهد."""
    try:
        _prev = (await state.get_data()).get("wallet_card_invoice_id")
        if _prev:
            db.delete_invoice(_prev)  # فاکتور قبلیِ همین کاربر نباید بعداً پیام «منقضی شد» بفرستد
    except Exception:
        pass
    invoicing_user = db.get_user(tg_user_id)
    invoice = db.create_invoice(
        user_id=invoicing_user["id"] if invoicing_user else None,
        telegram_id=str(tg_user_id),
        kind="wallet_card",
        label="شارژ کیف پول",
        price=amount,
    )
    deadline_str = format_deadline_time(invoice["expires_at"])
    await state.update_data(amount=amount, wallet_card_invoice_id=invoice["id"])
    await state.set_state(UserStates.waiting_charge_receipt)
    await show_menu_with_sticker(bot_, chat_id, "walletcharge_pay_card",
        progress_bar(1, 2) +
        f"💳 مبلغ: {amount:,} تومان\n\n"
        f"💳 شماره کارت:\n{bot_info.get('card_number')}\n\n"
        f"👤 {bot_info.get('card_holder')}\n\n"
        f"📸 عکس رسید را ارسال کنید.\n\n"
        f"⏱ این شماره کارت و مبلغ تا ساعت {deadline_str} (۳۰ دقیقه) معتبر است. لطفاً تا این ساعت رسید پرداخت را ارسال کنید، وگرنه این فاکتور به‌طور خودکار منقضی و حذف می‌شود.\n\n"
        f"*⚠️ دقیقاً همین مبلغ ({amount:,} تومان) را واریز کنید تا پرداختتان شناسایی و بلافاصله تایید شود.*",
        parse_mode="Markdown",
        append_note=invoice_expiry_note("wallet", deadline_str),
        reply_markup=card_payment_actions_keyboard(bot_info.get('card_number'), amount, f"changepay_wallet_{amount}"),
    )


def _pm_manager():
    from handlers.admin import _pm
    return _pm()


@router.callback_query(F.data.startswith("chargepay_crypto_"))
async def charge_pay_crypto_start(callback: types.CallbackQuery, state: FSMContext):
    """شارژ کیف پول با ارز دیجیتال: انتخاب ارز."""
    try:
        amount = int(callback.data.replace("chargepay_crypto_", ""))
    except ValueError:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    if await _reject_invalid_wallet_topup(callback, amount):
        return
    if "crypto" not in db.available_topup_methods():
        await callback.answer("❌ این روش برای شارژ کیف پول فعال نیست.", show_alert=True)
        return
    coins = [c for c in _pm_manager().enabled_cryptos if str(c.wallet or "").strip()]
    if not coins:
        await callback.answer("در حال حاضر هیچ ارز فعالی برای پرداخت تنظیم نشده است.", show_alert=True)
        return
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "walletcharge_pay_crypto",
        f"💱 شارژ کیف پول با ارز دیجیتال\n\n💰 مبلغ: {amount:,} تومان\n\nارز مورد نظر را انتخاب کنید:",
        reply_markup=charge_crypto_select_keyboard(coins, amount),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("chargecrypto|"))
async def charge_pay_crypto_asset(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("|")
    try:
        amount = int(parts[1]); symbol = parts[2]
    except (IndexError, ValueError):
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    if await _reject_invalid_wallet_topup(callback, amount):
        return
    if "crypto" not in db.available_topup_methods():
        await callback.answer("❌ این روش برای شارژ کیف پول فعال نیست.", show_alert=True)
        return
    pm = _pm_manager()
    coin = pm.get_crypto(symbol)
    if not coin or not coin.enabled or not pm.crypto_master_enabled:
        await callback.answer("❌ این ارز در حال حاضر در دسترس نیست.", show_alert=True)
        return
    wallet_addr = str(coin.wallet or "").strip()
    if not wallet_addr:
        await callback.answer("❌ آدرس کیف‌پول این ارز هنوز تنظیم نشده است.", show_alert=True)
        return
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا /start بزنید.", show_alert=True)
        return
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
    if not rate or float(rate) <= 0:
        await callback.answer("❌ دریافت قیمت لحظه‌ای این ارز ممکن نشد. چند دقیقه دیگر تلاش کنید.", show_alert=True)
        return
    amount_asset = round(amount * (1 + float(coin.margin_percent or 0) / 100.0) / float(rate), 6)
    if amount_asset <= 0:
        await callback.answer("❌ محاسبه مبلغ ارزی ناموفق بود.", show_alert=True)
        return
    try:
        _prev = (await state.get_data()).get("wallet_crypto_invoice_id")
        if _prev:
            db.delete_invoice(_prev)
    except Exception:
        pass
    invoice = db.create_invoice(
        user_id=user["id"], telegram_id=str(callback.from_user.id), kind="wallet_crypto",
        label="شارژ کیف پول", price=amount,
        payload={"asset": coin.symbol, "amount": amount_asset, "network": coin.network},
    )
    await state.update_data(
        amount=amount, wallet_crypto_invoice_id=invoice["id"], wallet_crypto_symbol=coin.symbol,
        wallet_crypto_network=coin.network, wallet_crypto_amount=str(amount_asset),
    )
    await state.set_state(UserStates.waiting_crypto_charge_receipt)
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "walletcharge_pay_crypto",
        f"💱 شارژ کیف پول با ارز دیجیتال\n\n"
        f"💰 مبلغ شارژ: {amount:,} تومان\n"
        f"💵 مبلغ قابل پرداخت: {amount_asset} {coin.symbol}\n"
        f"🌐 شبکه: {coin.network or '-'}\n"
        f"👛 آدرس ولت: {wallet_addr}\n\n"
        "پس از واریز، عکس رسید یا هش تراکنش را همینجا ارسال کنید.\n"
        "⏱ این فاکتور تا ۳۰ دقیقه معتبر است.",
        append_note=invoice_expiry_note("crypto"),
        reply_markup=charge_crypto_detail_keyboard(wallet_addr, str(amount_asset), amount),
    )
    await callback.answer()


async def _submit_crypto_charge_receipt(message: types.Message, state: FSMContext, hash_text: str | None = None):
    uid = str(message.from_user.id)
    data = await state.get_data()
    amount = data.get("amount")
    symbol = data.get("wallet_crypto_symbol")
    amount_asset = data.get("wallet_crypto_amount")
    network = data.get("wallet_crypto_network")
    invoice_id = data.get("wallet_crypto_invoice_id")
    _wd = wd_get(message.from_user.id, int(amount)) if amount is not None else None
    if amount is None or not symbol or not amount_asset or _wallet_topup_error(int(_wd["credit"]) if _wd else int(amount)):
        await message.answer("❌ مشکلی پیش آمد، لطفاً دوباره از منوی شارژ شروع کنید.")
        await state.clear()
        return
    if not invoice_id or db.consume_invoice(invoice_id) is None:
        await message.answer("⏰ مهلت ۳۰ دقیقه‌ای این فاکتور به پایان رسیده است. لطفاً دوباره از منوی شارژ شروع کنید.")
        await state.clear()
        return
    file_id, file_type, _t = extract_receipt_media(message)
    user = db.get_user(uid)
    receipt_id = None
    try:
        receipt_id = db.create_pending_receipt(
            "charge", uid, user["id"] if user else None, "شارژ کیف پول (ارز دیجیتال)", int(amount),
            file_id=file_id, file_type=file_type, note=(hash_text or None),
            **wd_receipt_args(message.from_user.id, int(amount)),
        )
    except Exception:
        logger.exception("ثبت رسید شارژ ارزی ناموفق بود")
    db.delete_invoice(invoice_id)
    tracking_code = receipt_id if receipt_id is not None else invoice_id
    admin_caption = (
        f"💱 رسید شارژ کیف پول (ارز دیجیتال)\n\n"
        f"👤 {receipt_user_label(message.from_user)}\n🆔 {uid}\n"
        f"💰 {int(amount):,} تومان\n💵 {amount_asset} {symbol}\n🌐 شبکه: {network or '-'}\n"
        + (f"🎟 کد تخفیف {_wd['code']}: مبلغ شارژ نهایی {int(_wd['credit']):,} تومان (تخفیف {int(_wd['credit']) - int(amount):,} تومان)\n" if _wd else "")
        + f"🧾 کد پیگیری: {tracking_code}"
        + (f"\n📝 Hash / Receipt: {hash_text}" if hash_text else "")
    )
    if receipt_id is not None:
        try:
            db.set_pending_receipt_media(receipt_id, details=admin_caption)
        except Exception:
            logger.exception("ذخیره‌ی توضیح رسید شارژ ارزی ناموفق بود")
    await send_receipt_card_to_admin(
        message.bot, admin_caption,
        reply_markup=admin_charge_approval_keyboard(uid, int(amount), receipt_id),
        file_id=file_id, file_type=file_type,
    )
    await message.answer(
        f"✅ رسید پرداخت ارزی دریافت شد\n\n🧾 کد پیگیری: {tracking_code}\n"
        f"💳 مبلغ: {int(amount):,} تومان\n"
        + (f"🎁 مبلغ شارژ شدنی: {int(_wd['credit']):,} تومان (با کد تخفیف {_wd['code']})\n" if _wd else "")
        + f"💵 {amount_asset} {symbol}\n🟡 وضعیت: در حال بررسی\n\n"
        "پس از تأیید، مبلغ به کیف پولتان اضافه می‌شود و به شما اطلاع داده می‌شود.",
        reply_markup=receipt_submitted_keyboard(),
    )
    wd_clear(message.from_user.id)
    await state.clear()


@router.message(UserStates.waiting_crypto_charge_receipt, F.photo | F.document)
async def charge_crypto_receipt_photo(message: types.Message, state: FSMContext):
    await _submit_crypto_charge_receipt(message, state)


@router.message(UserStates.waiting_crypto_charge_receipt, F.text)
async def charge_crypto_receipt_text(message: types.Message, state: FSMContext):
    h = (message.text or "").strip()
    if len(h) < 8:
        await message.answer("❌ هش تراکنش معتبر به نظر نمی‌رسد. هش کامل یا عکس رسید را ارسال کنید.")
        return
    await _submit_crypto_charge_receipt(message, state, hash_text=h)


@router.message(UserStates.waiting_crypto_charge_receipt)
async def charge_crypto_receipt_invalid(message: types.Message):
    await message.answer("❌ لطفاً عکس رسید یا هش تراکنش را ارسال کنید.")


@router.callback_query(F.data.startswith("chargepay_card_"))
async def charge_pay_with_card(callback: types.CallbackQuery, state: FSMContext):
    try:
        amount = int(callback.data.replace("chargepay_card_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    if await _reject_invalid_wallet_topup(callback, amount):
        return

    if "card" not in db.available_topup_methods():
        await callback.answer("❌ این روش برای شارژ کیف پول فعال نیست.", show_alert=True)
        return

    await _start_wallet_card_payment(callback.bot, callback.message.chat.id, callback.from_user.id, amount, state)
    await callback.answer()


# ✅ دکمه‌ی «انتخاب روش پرداخت دیگر» روی صفحه‌ی پرداخت کارت‌به‌کارت شارژ کیف پول:
# فاکتور فعلی را منقضی می‌کند و دوباره مرحله‌ی انتخاب روش پرداخت را نمایش می‌دهد.
@router.callback_query(F.data.startswith("changepay_wallet_"))
async def change_payment_method_wallet(callback: types.CallbackQuery, state: FSMContext):
    try:
        amount = int(callback.data.replace("changepay_wallet_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    if await _reject_invalid_wallet_topup(callback, amount):
        return

    data = await state.get_data()
    invoice_id = data.get("wallet_card_invoice_id")
    if invoice_id:
        db.delete_invoice(invoice_id)
    await state.update_data(wallet_card_invoice_id=None)
    await state.set_state(None)
    await _offer_charge_payment_method(callback, amount, state)
    await callback.answer()


@router.callback_query(F.data.startswith("chargepay_online_"))
async def charge_pay_online(callback: types.CallbackQuery):
    if "online" not in db.available_topup_methods():
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return

    try:
        amount = int(callback.data.replace("chargepay_online_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    if await _reject_invalid_wallet_topup(callback, amount):
        return

    if amount <= ONLINE_PAYMENT_MIN_AMOUNT:
        await callback.answer(
            ui_editor.get_text("msg_b46b807d8b", "❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست."
            " لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید."),
            show_alert=True,
        )
        return

    if is_duplicate_action(f"onlinecharge_{callback.from_user.id}_{amount}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))

    hash_id = uniquepay.new_hash_id("charge")
    invoice = await uniquepay.create_invoice(hash_id, amount)
    if invoice is None or not uniquepay_cfg.pick_link(invoice):
        await alerts.report_uniquepay_create_failure(callback.bot, ADMIN_ID)
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "walletcharge_pay_online", 
            "❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست."
            " لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید.",
            reply_markup=charge_payment_method_keyboard(amount),
        )
        return

    alerts.report_uniquepay_create_success()

    payment_link = uniquepay_cfg.pick_link(invoice)
    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(callback.from_user.id),
        hash_id=hash_id,
        plan_name="شارژ کیف پول",
        price=amount,
        order_type="wallet_charge",
        kind="wallet_charge",
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
        **wd_receipt_args(callback.from_user.id, amount),
    )

    white_label = uniquepay.white_label_data(invoice)
    payable_text = (
        f"💰 مبلغ دقیق واریز: {int(white_label['payableAmount']):,} تومان"
        if white_label and str(white_label['payableAmount']).isdigit()
        else f"💰 مبلغ سفارش: {amount:,} تومان"
    )
    card_text = f"\n💳 شماره کارت مقصد: {white_label['cardNumber']}\n" if white_label else ""
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "walletcharge_pay_online", 
        progress_bar(1, 2) +
        f"🌐 پرداخت آنلاین (کارت‌به‌کارت خودکار)\n\n"
        + payable_text + card_text + "\n"
        f"روی دکمه‌ی پرداخت بزنید یا از اطلاعات وایت‌لیبل بالا استفاده کنید؛ سپس همینجا روی «بررسی کن» بزنید.\n"
        f"⏱ به‌محض تأیید نهایی UniquePay، کیف پول شما به‌طور خودکار شارژ می‌شود.\n\n"
        f"⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است. اگر تا این مهلت پرداخت تایید نشود، به‌طور خودکار منقضی و حذف خواهد شد.",
        reply_markup=online_payment_wallet_keyboard(payment_link, payment_id, white_label),
    )


async def finalize_wallet_charge_online_payment(bot, payment: dict) -> int | None:
    """اینوویس پرداخت‌شده‌ی یونیک‌پی برای «شارژ کیف پول» را نهایی می‌کند.
    معادل finalize_online_payment در handlers/plans.py، با این تفاوت که به‌جای
    ساخت سفارش/سرویس، مستقیماً مبلغ به کیف پول کاربر اضافه می‌شود. از همان
    قفل اتمیک claim_online_payment_for_finalize استفاده می‌شود تا اگر پولر
    پس‌زمینه‌ی ربات (bot.py) و دکمه‌ی «بررسی کن» هم‌زمان صدا زده شوند، کیف
    پول فقط یک‌بار شارژ شود."""
    amount = int(payment["price"])
    _wd_credit = db.parse_wallet_discount_extra(payment.get("extra")) if payment.get("discount_code") else None
    if _wallet_topup_error(max(amount, _wd_credit or 0)):
        logger.error(
            "Rejected out-of-range wallet online payment during finalization: user=%s amount=%s",
            payment.get("telegram_id"), amount,
        )
        return None

    if payment["status"] == "paid":
        return payment["id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid":
            return fresh["id"]
        return None

    try:
        db.add_to_wallet(payment["user_id"], payment["price"], "شارژ کیف پول (پرداخت آنلاین)")
        db.credit_charge_bonus(payment["user_id"], int(payment["price"]))
        _credited = db.apply_wallet_discount_credit(payment["user_id"], int(payment["price"]), payment.get("discount_code"), payment.get("extra"))
        db.mark_online_payment_paid(payment["id"], None)
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise
    try:
        wd_clear(payment["telegram_id"])
    except Exception:
        pass

    try:
        from handlers.admin import user_text as _ut
        if db.user_pref_enabled(user_id=payment["user_id"], key="wallet_alerts") and notif_settings.enabled("wallet_charged"):
            await send_user_notification(
                bot, int(payment["telegram_id"]), "notif_wallet_charge",
                _ut("notif_wallet_online_charged", amount=int(_credited)) + (f"\n🎟 با کد تخفیف {payment['discount_code']}: {int(_credited) - int(payment['price']):,} تومان تخفیف گرفتید." if int(_credited) > int(payment["price"]) else ""),
            )
    except Exception:
        logger.exception("اعلان شارژ آنلاین به کاربر ناموفق بود")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💳 شارژ کیف پول ({og.admin_label(payment)})!\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"💰 {payment['price']:,} تومان"
            + (f"\n🎟 کد {payment['discount_code']}: شارژ نهایی {int(_credited):,} تومان" if int(_credited) > int(payment["price"]) else ""),
        )
    except Exception:
        logger.exception("ارسال پیام اطلاع‌رسانی شارژ آنلاین به ادمین ناموفق بود")

    return payment["id"]


@router.message(UserStates.waiting_charge_receipt, F.photo | F.document)
async def receive_receipt(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    data = await state.get_data()
    amount = data.get("amount")
    wallet_card_invoice_id = data.get("wallet_card_invoice_id")

    file_id, file_type, _txt = extract_receipt_media(message)
    if file_id is None:
        await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))
        return

    if amount is None:
        await message.answer(progress_bar(2, 2) + "❌ مشکلی پیش آمد، لطفاً دوباره از منوی شارژ شروع کنید.")
        await state.clear()
        return

    _wd = wd_get(message.from_user.id, int(amount))
    error = _wallet_topup_error(int(_wd["credit"]) if _wd else int(amount))
    if error:
        await message.answer(error)
        await state.clear()
        return

    if not wallet_card_invoice_id or db.consume_invoice(wallet_card_invoice_id) is None:
        await message.answer(
            ui_editor.get_text("msg_d77397f5d3", "⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی شارژ شروع کنید.")
        )
        await state.clear()
        return

    user = db.get_user(uid)
    receipt_id = None
    try:
        receipt_id = db.create_pending_receipt(
            "charge", uid, user["id"] if user else None, "شارژ کیف پول", amount,
            file_id=file_id, file_type=file_type,
            **wd_receipt_args(message.from_user.id, int(amount)),
        )
    except Exception:
        logger.exception("ثبت رسید شارژ ناموفق بود")

    if wallet_card_invoice_id:
        db.delete_invoice(wallet_card_invoice_id)

    tracking_code = receipt_id if receipt_id is not None else wallet_card_invoice_id
    admin_caption = (
        f"📩 رسید شارژ کیف پول (کارت‌به‌کارت)\n\n"
        f"👤 {receipt_user_label(message.from_user)}\n"
        f"🆔 {uid}\n"
        f"💰 {amount:,} تومان\n"
        + (f"🎟 کد تخفیف {_wd['code']}: مبلغ شارژ نهایی {int(_wd['credit']):,} تومان (تخفیف {int(_wd['credit']) - int(amount):,} تومان)\n" if _wd else "")
        + f"🧾 کد پیگیری: {tracking_code}"
    )
    if receipt_id is not None:
        try:
            db.set_pending_receipt_media(receipt_id, details=admin_caption)
        except Exception:
            logger.exception("ذخیره‌ی توضیح رسید شارژ ناموفق بود")
    delivered = await send_receipt_card_to_admin(
        message.bot, admin_caption,
        reply_markup=admin_charge_approval_keyboard(uid, amount, receipt_id),
        file_id=file_id, file_type=file_type,
    )
    if not delivered:
        logger.error("رسید شارژ %s برای ادمین ارسال نشد؛ فقط در صف رسیدها موجود است", tracking_code)

    await message.answer(
        f"✅ رسید پرداخت با موفقیت دریافت شد\n\n"
        f"🧾 کد پیگیری: `{tracking_code}`\n"
        f"📦سرویس: شارژ کیف پول\n"
        f"💳 مبلغ پرداختی: {amount:,} تومان\n"
        + (f"🎁 مبلغ شارژ شدنی: {int(_wd['credit']):,} تومان (با کد تخفیف {_wd['code']})\n" if _wd else "")
        + f"🟡 وضعیت: در حال بررسی\n\n"
        f"رسید شما ثبت شده و نیازی به ارسال مجدد آن نیست.\n\n"
        f"پس از تأیید پرداخت، مبلغ به کیف پولتان اضافه می‌شود و از همین ربات به شما اطلاع داده می‌شود.",
        parse_mode="Markdown",
        reply_markup=receipt_submitted_keyboard(),
    )
    wd_clear(message.from_user.id)
    await state.clear()


@router.message(UserStates.waiting_charge_receipt)
async def receipt_wrong_format(message: types.Message):
    # اگر کاربر به‌جای عکس، متن فرستاد
    await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))
