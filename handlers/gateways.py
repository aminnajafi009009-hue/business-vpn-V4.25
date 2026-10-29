"""
handlers/gateways.py
مسیرهای کاربر/ادمین برای درگاه‌های پرداخت آنلاین «جدید» (FrenzyEx و Tronado و TonPays):

  • ساخت فاکتور ۳۰ دقیقه‌ای برای: خرید پلن (pay_fx_/pay_tr_/pay_tp_)، بساز-سرویس/تمدید (cbuild_pay_fx/tr)،
    شارژ کیف پول (chargepay_fx_/chargepay_tr_) — دقیقاً هم‌شکل فاکتور یونیک‌پی/کریپتو
    (دکمه‌ی کپی مبلغ، پرداخت، «بررسی کن»، مهلت ساعت‌دار).
  • بررسی وضعیت: دکمه‌ی «بررسی کن» (از handlers/plans.py::check_online_payment صدا زده می‌شود)
    و پولر پس‌زمینه‌ی bot.py (settle_gateway_payment).
  • تحویل نهایی: finalize_gateway_payment → همان finalize_* های موجود (سفارش/سرویس/شارژ) پس
    لاگ سفارش کانال/ادمین، JSON پنل، تحویل خودکار و... دقیقاً مثل بقیه‌ی روش‌ها اجرا می‌شود.
  • پرداخت دیرهنگام (بعد از انقضای ۳۰ دقیقه): همان سفارش/شارژ اصلی خودکار تحویل داده می‌شود (کاربر منتظر نمی‌ماند).
  • بررسی دستی ادمین (review): دکمه‌های gwok_/gwno_.
"""
import json
import logging

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

import alerts
import database as db
import online_gateways as og
import ui_editor
from config import ADMIN_ID
from keyboards import (
    custom_build_payment_keyboard,
    gateway_payment_keyboard,
    purchase_payment_keyboard,
    charge_payment_method_keyboard,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
)
import text_catalog
from states import UserStates
from utils import is_duplicate_action, progress_bar, show_menu_with_sticker, format_deadline_time
from handlers.plans import (
    _compute_final_price,
    _chosen_name,
    plan_type,
    finalize_online_payment,
    finalize_custom_online_payment,
)
from handlers.wallet import finalize_wallet_charge_online_payment, _reject_invalid_wallet_topup, wd_receipt_args

logger = logging.getLogger(__name__)

router = Router(name="gateways")


# ---------------------------------------------------------------------------
# ساخت و نمایش فاکتور (مشترک بین سه مسیر)
# ---------------------------------------------------------------------------
async def _create_invoice_screen(
    callback: types.CallbackQuery,
    *,
    gw: str,
    ref_prefix: str,
    amount: int,
    user: dict,
    payment_kwargs: dict,
    header: str,
    done_text: str,
    sticker_key: str,
    ui_screen: str,
    back_callback: str,
    step: tuple[int, int] | None,
    fail_keyboard,
    description: str,
):
    """فاکتور را روی درگاه می‌سازد، ردیف online_payments را ثبت و صفحه‌ی پرداخت را نشان می‌دهد."""
    title = og.title(gw)
    ref = og.new_ref(ref_prefix, gw)
    res = await og.create_payment(gw, ref, amount, description, buyer_id=callback.from_user.id)
    if not res.ok:
        logger.warning("ساخت فاکتور %s ناموفق: %s", gw, res.error)
        await alerts.report_gateway_create_failure(callback.bot, ADMIN_ID, title, res.error)
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, sticker_key,
            f"❌ ساخت لینک پرداخت {title} ناموفق بود. لطفاً چند لحظه بعد دوباره تلاش کنید یا روش پرداخت دیگری را انتخاب کنید.",
            reply_markup=fail_keyboard,
        )
        return
    alerts.report_gateway_create_success(title)

    try:
        payment_id = db.create_online_payment(
            user_id=user["id"],
            telegram_id=str(callback.from_user.id),
            hash_id=ref,
            price=amount,
            payment_link=res.link,
            ref_id=res.ref_id,
            gateway=gw,
            gw_data=res.gw_data,
            **payment_kwargs,
        )
    except Exception:
        logger.exception("ثبت فاکتور %s در دیتابیس ناموفق بود", gw)
        await og.cancel_payment({"gateway": gw, "ref_id": res.ref_id})
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, sticker_key,
            "❌ مشکلی در ثبت فاکتور پیش آمد. لطفاً دوباره تلاش کنید.",
            reply_markup=fail_keyboard,
        )
        return

    row = db.get_online_payment(payment_id) or {}
    deadline = ""
    try:
        deadline = format_deadline_time(db.online_payment_expires_at(row["created_at"]))
    except Exception:
        pass

    _tp = gw == "tonpays"
    gd = res.gw_data or {}
    if _tp:
        # تون‌پیز مهلت ثابتی در API اعلام نمی‌کند؛ فقط اگر در پاسخ فاکتور زمان انقضا بود نشان می‌دهیم
        deadline = _tonpays_deadline(gd.get("expires_at_raw"))
    if _tp and gd.get("kind") == "tg" and gd.get("card_number"):
        # حالت «تلگرام»: کارت مقصد داخل ربات (هم‌شکل پیام ربات تون‌پیز)
        await _show_tonpays_card_screen(callback, payment_id=payment_id, amount=amount, gd=gd, sticker_key=sticker_key)
        return

    lines = [header, f"💰 مبلغ سفارش: {amount:,} تومان"]
    lines.extend(res.info_lines)
    _progress = progress_bar(*step) if step else ""
    _details = "\n".join(lines)
    if _tp:
        _deadline_note = f"⚠️ این فاکتور تا ساعت {deadline} معتبر است." if deadline else ""
    else:
        _deadline_note = (
            (f"⚠️ این فاکتور تا ساعت {deadline} (۳۰ دقیقه) معتبر است. " if deadline else "⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است. ")
            + "اگر تا این مهلت پرداخت تأیید نشود، به‌طور خودکار منقضی می‌شود "
            + "(اگر بعد از آن پرداخت کرده باشید، سفارش شما به‌صورت خودکار تحویل داده می‌شود)."
        )
    text = (
        f"{_progress}🌐 پرداخت آنلاین ({title})\n\n"
        f"{_details}\n\n"
        "روی دکمه‌ی پرداخت بزنید و پرداخت را کامل کنید؛ سپس همینجا روی «بررسی کن» بزنید.\n"
        f"⏱ به‌محض تأیید نهایی {title}، {done_text}"
        + (f"\n\n{_deadline_note}" if _deadline_note else "")
    )
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, sticker_key, text,
        reply_markup=gateway_payment_keyboard(
            gw, res.link, payment_id, back_callback, ui_screen, link2=res.link2,
            price=None if _tp else amount, gw_data=res.gw_data,
        ),
        ui_key="gateway_invoice",
        template_values={"progress": _progress, "title": title, "details": _details,
                         "done_text": done_text, "deadline_note": _deadline_note},
    )


def _tonpays_deadline(raw) -> str:
    """زمان انقضای ISO/‌«YYYY-MM-DD HH:MM:SS» (UTC) → «HH:MM» تهران؛ ناموفق = رشته‌ی خالی."""
    if not raw:
        return ""
    try:
        from datetime import datetime, timezone
        txt = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_deadline_time(dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        return ""


_TP_CARD_SCREEN = "gateway_tonpays_card"
_TP_CARD_FALLBACK = (
    "💰 مبلغ دقیق واریز: {amount} ریال\n"
    "💳 شماره کارت: {card}\n"
    "👤 صاحب کارت: {holder}\n"
    "🧾 کد پیگیری: {tracking}\n\n"
    "📸 پس از واریز می‌توانید تصویر رسید را ارسال کنید و وضعیت پرداخت را بررسی کنید.\n\n"
    "🌟 پرداخت ها توسط درگاه Ton Pays تایید میشه"
)


def _tp_card_text(values: dict) -> str:
    """متن حالت «تلگرام» از کاتالوگ متن (قابل ویرایش در ویرایشگر)؛ اگر قالب ادمین خراب بود، متن پیش‌فرض."""
    tpl = text_catalog.TEXTS.get(_TP_CARD_SCREEN) or _TP_CARD_FALLBACK
    # مبلغ دقیق واریز ریالی است؛ قالب‌های ذخیره‌شده‌ی قدیمی که «تومان» دارند هم اصلاح می‌شوند.
    tpl = tpl.replace("{amount} تومان", "{amount} ریال")
    try:
        return tpl.format(**values)
    except (KeyError, IndexError, ValueError):
        return _TP_CARD_FALLBACK.format(**values)


async def _show_tonpays_card_screen(callback, *, payment_id, amount, gd, sticker_key):
    """حالت «تلگرام» تون‌پیز: کارت مقصد، مبلغ دقیق، کد پیگیری — هم‌شکل پیام خودِ ربات تون‌پیز.
    متن و برچسب دکمه‌ها از ویرایشگر (ui_editor / text_catalog) خوانده می‌شوند."""
    try:
        exact = int(gd.get("final_amount") or amount)
    except (TypeError, ValueError):
        exact = int(amount)
    rial = exact * 10
    card = str(gd.get("card_number") or "")
    values = {
        "amount": f"{rial:,}",
        "card": card,
        "holder": str(gd.get("card_holder") or "—"),
        "tracking": str(gd.get("tracking_code") or "—"),
    }
    text = _tp_card_text(values)
    kb = InlineKeyboardMarkup(ui_screen=_TP_CARD_SCREEN, inline_keyboard=[
        [InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=card), style="primary",
                              ui_screen=_TP_CARD_SCREEN, ui_button_key="tp_copy_card"),
         InlineKeyboardButton(text=f"{rial:,} ریال", copy_text=CopyTextButton(text=str(rial)), style="primary",
                              ui_screen=_TP_CARD_SCREEN, ui_button_key="tp_copy_amount")],
        [InlineKeyboardButton(text="❓ بررسی وضعیت پرداخت", callback_data=f"checkpay_{payment_id}", style="primary",
                              ui_screen=_TP_CARD_SCREEN, ui_button_key="tp_check")],
        [InlineKeyboardButton(text="📸 ارسال تصویر رسید", callback_data=f"tpless_{payment_id}", style="primary",
                              ui_screen=_TP_CARD_SCREEN, ui_button_key="tp_receipt")],
        [InlineKeyboardButton(text="🔄 تعویض کارت", callback_data=f"tpchg_{payment_id}", style="primary",
                              ui_screen=_TP_CARD_SCREEN, ui_button_key="tp_change_card")],
    ])
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, sticker_key, text, reply_markup=kb,
        ui_key=_TP_CARD_SCREEN, template_values=values,
    )


def _gw_or_alert(short: str):
    gw = og.resolve_short(short)
    return gw if gw and og.enabled(gw) else None


async def _below_min(callback: types.CallbackQuery, gw: str, amount: int) -> bool:
    if og.amount_ok(gw, amount):
        return False
    await callback.answer(
        f"❌ حداقل مبلغ برای پرداخت با {og.title(gw)} {og.min_amount(gw):,} تومان است. "
        "لطفاً روش پرداخت دیگری را انتخاب کنید.",
        show_alert=True,
    )
    return True


# ---------------------------------------------------------------------------
# خرید پلن
# ---------------------------------------------------------------------------
@router.callback_query(F.data.regexp(r"^pay_(fx|tr|tp)_.+"))
async def pay_plan_with_gateway(callback: types.CallbackQuery, state: FSMContext):
    short, plan_key = callback.data[len("pay_"):len("pay_") + 2], callback.data[len("pay_fx_"):]
    gw = _gw_or_alert(short)
    if gw is None:
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return
    if is_duplicate_action(f"gwbuy_{callback.from_user.id}_{plan_key}"):
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
    if await _below_min(callback, gw, final_price):
        return

    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))
    chosen = _chosen_name(data, plan_key)
    await _create_invoice_screen(
        callback, gw=gw, ref_prefix="plan", amount=final_price, user=user,
        payment_kwargs=dict(
            plan_name=plan["name"], order_type=plan_type(plan_key), plan_key=plan_key,
            discount_code=winning_code, kind="plan",
            extra=(json.dumps({"service_name": chosen}, ensure_ascii=False) if chosen else None),
        ),
        header=f"🛒 {plan['name']}",
        done_text="سفارش شما به‌طور خودکار ثبت می‌شود.",
        sticker_key="plan_pay_online", ui_screen="plan_pay_online", back_callback="plans", step=(2, 3),
        fail_keyboard=purchase_payment_keyboard(plan_key, show_discount=False),
        description=f"خرید {plan['name']}",
    )


# ---------------------------------------------------------------------------
# بساز سرویس خودت / تمدید
# ---------------------------------------------------------------------------
@router.callback_query(F.data.regexp(r"^cbuild_pay_(fx|tr|tp)$"))
async def pay_custom_with_gateway(callback: types.CallbackQuery, state: FSMContext):
    short = callback.data[len("cbuild_pay_"):]
    gw = _gw_or_alert(short)
    if gw is None:
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return
    if is_duplicate_action(f"cbuildgwbuy_{callback.from_user.id}"):
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
    price = int(price)
    if await _below_min(callback, gw, price):
        return

    order_type = data.get("custom_order_type", "new")
    custom_name = data.get("custom_name")
    extra_payload = json.dumps({
        "volume": volume, "days": days, "custom_name": custom_name,
        "order_type": order_type, "target_config_id": data.get("custom_target_config_id"),
        "renew_mode": data.get("renew_mode"), "option_num": data.get("custom_option_num") or 0,
    })
    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))
    await _create_invoice_screen(
        callback, gw=gw, ref_prefix="cbuild", amount=price, user=user,
        payment_kwargs=dict(
            plan_name=custom_name or "سرویس سفارشی (بساز سرویس خودت)", order_type="custom",
            plan_key=None, kind="custom", extra=extra_payload,
        ),
        header=f"🧩 سرویس سفارشی — {volume} گیگ / {days} روز",
        done_text="سفارش شما به‌طور خودکار ثبت می‌شود.",
        sticker_key="cbuild_pay_online", ui_screen="plan_pay_online", back_callback="cbuild_start", step=(2, 3),
        fail_keyboard=custom_build_payment_keyboard(),
        description=f"سرویس سفارشی {volume}GB/{days}d",
    )


# ---------------------------------------------------------------------------
# شارژ کیف پول
# ---------------------------------------------------------------------------
@router.callback_query(F.data.regexp(r"^chargepay_(fx|tr|tp)_\d+$"))
async def pay_wallet_with_gateway(callback: types.CallbackQuery):
    short = callback.data[len("chargepay_"):len("chargepay_") + 2]
    gw = _gw_or_alert(short)
    if gw is None or gw not in db.available_topup_methods():
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return
    try:
        amount = int(callback.data.split("_", 2)[2])
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return
    if await _reject_invalid_wallet_topup(callback, amount):
        return
    if await _below_min(callback, gw, amount):
        return
    if is_duplicate_action(f"gwcharge_{callback.from_user.id}_{short}_{amount}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))
    await _create_invoice_screen(
        callback, gw=gw, ref_prefix="charge", amount=amount, user=user,
        payment_kwargs=dict(plan_name="شارژ کیف پول", order_type="wallet_charge", kind="wallet_charge", **wd_receipt_args(callback.from_user.id, amount)),
        header="👛 شارژ کیف پول",
        done_text="کیف پول شما به‌طور خودکار شارژ می‌شود.",
        sticker_key="walletcharge_pay_online", ui_screen="walletcharge_pay_online", back_callback="wallet", step=(1, 2),
        fail_keyboard=charge_payment_method_keyboard(amount),
        description="شارژ کیف پول",
    )


# ---------------------------------------------------------------------------
# تحویل
# ---------------------------------------------------------------------------
async def finalize_gateway_payment(bot, payment: dict) -> int | None:
    """تحویل پرداخت تأییدشده‌ی درگاه جدید با همان finalize_* موجود (idempotent با claim اتمیک).
    پرداخت دیرهنگام (بعد از ۳۰ دقیقه) هم دقیقاً همان سفارش/شارژ اصلی را خودکار تحویل می‌دهد تا کاربر منتظر نماند."""
    was_late = False
    if payment["status"] == "expired":
        if not db.reopen_expired_gateway_payment(payment["id"]):
            fresh = db.get_online_payment(payment["id"])
            if not fresh or fresh["status"] != "pending":
                return fresh["id"] if fresh and fresh["status"] == "paid" else None
        payment = db.get_online_payment(payment["id"]) or payment
        was_late = True
    kind = payment.get("kind")
    if kind == "custom":
        result = await finalize_custom_online_payment(bot, payment)
    elif kind == "wallet_charge":
        result = await finalize_wallet_charge_online_payment(bot, payment)
    else:
        result = await finalize_online_payment(bot, payment)
    if was_late and result:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⏰ پرداخت بعد از مهلت ۳۰ دقیقه ({og.title(og.gateway_of(payment))}) تأیید و خودکار تحویل داده شد.\n"
                f"🆔 {payment['telegram_id']}\n📦 {og.kind_label(payment)}\n🧾 {payment['hash_id']}",
            )
        except Exception:
            logger.exception("اعلان پرداخت دیرهنگام به ادمین ناموفق بود")
    return result


async def hold_for_review(bot, payment: dict, reason: str) -> bool:
    """انتقال یک‌باره به review + اعلان ادمین (با دکمه‌ی تأیید/رد) و کاربر."""
    if not db.hold_online_payment_for_review(payment["id"], reason):
        return False
    fresh = db.get_online_payment(payment["id"]) or payment
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text=t, callback_data=c) for t, c in og.review_buttons(payment["id"])
    ]])
    try:
        await bot.send_message(ADMIN_ID, og.review_admin_text(fresh, reason), reply_markup=kb)
    except Exception:
        logger.exception("اعلان بررسی دستی به ادمین ناموفق بود")
    try:
        await bot.send_message(int(payment["telegram_id"]), og.review_user_text(payment))
    except Exception:
        logger.exception("اعلان بررسی دستی به کاربر ناموفق بود")
    return True


async def settle_gateway_payment(bot, payment: dict) -> og.GwResult:
    """یک پرداخت درگاه جدید را استعلام و (در صورت پرداخت) تحویل می‌دهد. توسط پولر و دکمه‌ی «بررسی کن»."""
    res = await og.check_payment(payment)
    if res.state == "paid":
        await finalize_gateway_payment(bot, payment)
    elif res.state == "review":
        await hold_for_review(bot, payment, res.reason or "مغایرت در پرداخت")
    elif res.state in ("action_less", "action_more"):
        # تون‌پیز: مبلغ کمتر/بیشتر واریز شده؛ یک‌بار (برای هر حالت) راهنما + دکمه‌ی اقدام برای کاربر
        flag = "less_sent" if res.state == "action_less" else "more_sent"
        fresh = db.get_online_payment(payment["id"]) or payment
        if not db.gw_data_of(fresh).get(flag):
            db.merge_gw_data(payment["id"], **{flag: True})
            await _send_tonpays_action(bot, payment, res)
    return res


def _tonpays_action_keyboard(res_state: str, payment_id: int):
    if res_state == "action_less":
        btn = types.InlineKeyboardButton(text="📸 ارسال رسید مابقی", callback_data=f"tpless_{payment_id}", style="primary")
    else:
        btn = types.InlineKeyboardButton(text="💳 ثبت کارت عودت", callback_data=f"tpmore_{payment_id}", style="primary")
    return types.InlineKeyboardMarkup(inline_keyboard=[[btn]])


async def _send_tonpays_action(bot, payment: dict, res: og.GwResult):
    """پیام راهنما + دکمه‌ی اقدام (رسید مابقی / ثبت کارت عودت) برای پرداخت تون‌پیز."""
    try:
        d = res.data or {}
        done = (res.state == "action_less" and d.get("remaining_receipt_received")) or (res.state == "action_more" and d.get("refund_pending"))
        await bot.send_message(
            int(payment["telegram_id"]), res.reason,
            reply_markup=None if done else _tonpays_action_keyboard(res.state, payment["id"]),
        )
    except Exception:
        logger.exception("ارسال راهنمای مبلغ کمتر/بیشتر تون‌پیز به کاربر ناموفق بود")


def _own_tonpays_payment(callback_or_msg_user_id, payment_id: int):
    payment = db.get_online_payment(payment_id)
    if not payment or str(payment.get("telegram_id")) != str(callback_or_msg_user_id) or og.gateway_of(payment) != "tonpays":
        return None
    return payment


@router.callback_query(F.data.regexp(r"^tpmore_\d+$"))
async def tonpays_refund_start(callback: types.CallbackQuery, state: FSMContext):
    payment = _own_tonpays_payment(callback.from_user.id, int(callback.data.split("_", 1)[1]))
    if not payment or payment["status"] not in ("pending", "expired"):
        await callback.answer("❌ این پرداخت دیگر قابل اقدام نیست.", show_alert=True)
        return
    await state.update_data(tp_refund_payment=payment["id"])
    await state.set_state(UserStates.waiting_tonpays_refund)
    await callback.answer()
    await callback.message.answer(
        "💳 ثبت کارت عودت مازاد\n\n"
        "شماره کارت ۱۶ رقمی و نام صاحب کارت خودتان را در یک پیام و در دو خط بفرستید. مثال:\n"
        "6037991234567890\nعلی رضایی\n\n"
        "⚠️ کارت باید متعلق به خودتان باشد؛ مازاد به همین کارت واریز می‌شود.\n"
        "برای انصراف /start را بزنید."
    )


@router.message(UserStates.waiting_tonpays_refund, F.text)
async def tonpays_refund_receive(message: types.Message, state: FSMContext):
    import re as _re
    data = await state.get_data()
    payment = _own_tonpays_payment(message.from_user.id, int(data.get("tp_refund_payment") or 0))
    if not payment:
        await state.clear()
        await message.answer("❌ این درخواست منقضی شده؛ از «بررسی کن» دوباره شروع کنید.")
        return
    raw = (message.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = _re.sub(r"\D", "", raw.split("\n", 1)[0]) if "\n" in raw else ""
    if len(digits) != 16:
        m = _re.search(r"(?:\d[\s-]?){16}", raw)
        digits = _re.sub(r"\D", "", m.group(0)) if m else ""
    holder = _re.sub(r"[\d\-]", " ", raw)
    holder = _re.sub(r"\s+", " ", holder).strip()
    if len(digits) != 16 or len(holder) < 3:
        await message.answer("❌ فرمت درست نیست. خط اول شماره کارت ۱۶ رقمی و خط دوم نام صاحب کارت باشد. دوباره بفرستید:")
        return
    import asyncio, tonpays
    res = await asyncio.to_thread(tonpays.register_refund_card, payment.get("ref_id") or "", digits, holder, tonpays.kind_of(payment))
    if not res["ok"]:
        await message.answer(f"❌ ثبت کارت ناموفق بود: {res.get('error') or 'خطا'}\nدوباره تلاش کنید یا با پشتیبانی تماس بگیرید.")
        return
    await state.clear()
    db.merge_gw_data(payment["id"], refund_card_registered=True)
    await message.answer("✅ کارت عودت ثبت شد؛ مازاد به همین کارت برمی‌گردد. در حال تأیید پرداخت...")
    fresh = db.get_online_payment(payment["id"]) or payment
    out = await settle_gateway_payment(message.bot, fresh)
    if out.state not in ("paid",):
        await message.answer("⏳ بعد از تأیید تون‌پیز، سفارش شما خودکار تحویل داده می‌شود. می‌توانید «بررسی کن» را هم بزنید.")


@router.callback_query(F.data.regexp(r"^tpless_\d+$"))
async def tonpays_remainder_start(callback: types.CallbackQuery, state: FSMContext):
    payment = _own_tonpays_payment(callback.from_user.id, int(callback.data.split("_", 1)[1]))
    if not payment or payment["status"] not in ("pending", "expired"):
        await callback.answer("❌ این پرداخت دیگر قابل اقدام نیست.", show_alert=True)
        return
    await state.update_data(tp_remainder_payment=payment["id"])
    await state.set_state(UserStates.waiting_tonpays_remainder)
    await callback.answer()
    await callback.message.answer(
        "📸 عکس رسید واریز را (JPEG/PNG/WebP، حداکثر ۱۰ مگابایت) همین‌جا بفرستید.\n"
        "اگر مبلغ کمتری واریز کرده‌اید، رسید «مابقی مبلغ» را بفرستید. برای انصراف /start را بزنید."
    )


@router.message(UserStates.waiting_tonpays_remainder, F.photo | F.document)
async def tonpays_remainder_receive(message: types.Message, state: FSMContext):
    import asyncio, tonpays
    data = await state.get_data()
    payment = _own_tonpays_payment(message.from_user.id, int(data.get("tp_remainder_payment") or 0))
    if not payment:
        await state.clear()
        await message.answer("❌ این درخواست منقضی شده؛ از «بررسی کن» دوباره شروع کنید.")
        return
    if message.photo:
        file_id, ctype, fname = message.photo[-1].file_id, "image/jpeg", "receipt.jpg"
        size = message.photo[-1].file_size or 0
    else:
        doc = message.document
        ctype = (doc.mime_type or "").lower()
        if ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            await message.answer("❌ فقط تصویر JPEG/PNG/WebP/GIF قابل قبول است. دوباره بفرستید:")
            return
        file_id, fname, size = doc.file_id, doc.file_name or "receipt", doc.file_size or 0
    if size > 10 * 1024 * 1024:
        await message.answer("❌ حجم تصویر بیشتر از ۱۰ مگابایت است. تصویر کوچک‌تری بفرستید:")
        return
    try:
        buf = await message.bot.download(file_id)
        content = buf.read() if buf else b""
    except Exception:
        logger.exception("دانلود رسید مابقی تون‌پیز ناموفق بود")
        content = b""
    if not content:
        await message.answer("❌ دریافت تصویر ناموفق بود؛ دوباره بفرستید:")
        return
    res = await asyncio.to_thread(tonpays.send_remainder_receipt, payment.get("ref_id") or "", fname, content, ctype,
                                  tonpays.kind_of(payment))
    if not res["ok"]:
        await message.answer(f"❌ ارسال رسید ناموفق بود: {res.get('error') or 'خطا'}\nدوباره تلاش کنید یا با پشتیبانی تماس بگیرید.")
        return
    await state.clear()
    db.merge_gw_data(payment["id"], remainder_receipt_sent=True)
    if payment.get("status") == "pending":
        await message.answer("✅ رسید واریز ثبت شد و در انتظار تأیید تون‌پیز است. بعد از تأیید، سفارش خودکار تحویل داده می‌شود (می‌توانید «بررسی کن» را هم بزنید).")
    else:
        await message.answer("✅ رسید مابقی ثبت شد و در انتظار تأیید تون‌پیز است. بعد از تأیید، سفارش خودکار تحویل داده می‌شود (می‌توانید «بررسی کن» را هم بزنید).")


@router.callback_query(F.data.regexp(r"^tpchg_\d+$"))
async def tonpays_change_card(callback: types.CallbackQuery):
    """تعویض کارت مقصد (فقط فاکتور کاستوم تلگرام در انتظار پرداخت). کول‌داون و سقف تعویض را خود تون‌پیز اعمال می‌کند."""
    import asyncio, tonpays
    payment = _own_tonpays_payment(callback.from_user.id, int(callback.data.split("_", 1)[1]))
    if not payment or payment["status"] != "pending" or tonpays.kind_of(payment) != "tg":
        await callback.answer("❌ تعویض کارت برای این فاکتور ممکن نیست.", show_alert=True)
        return
    res = await asyncio.to_thread(tonpays.change_card, payment.get("ref_id") or "")
    if not res["ok"]:
        await callback.answer(f"⏳ {res.get('error') or 'تعویض کارت انجام نشد'}", show_alert=True)
        return
    new = tonpays.extract_card_info(res.get("data") or {})
    if not new.get("card_number"):
        await callback.answer("⚠️ کارت جدیدی دریافت نشد؛ دوباره بزنید.", show_alert=True)
        return
    db.merge_gw_data(payment["id"], card_number=new["card_number"], card_holder=new.get("card_holder"))
    fresh = db.get_online_payment(payment["id"]) or payment
    await callback.answer("🔄 کارت جدید ثبت شد.")
    await _show_tonpays_card_screen(callback, payment_id=payment["id"], amount=int(payment["price"]),
                                    gd=db.gw_data_of(fresh), sticker_key=None)
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.message(UserStates.waiting_tonpays_remainder)
async def tonpays_remainder_wrong(message: types.Message):
    await message.answer("📸 لطفاً عکس رسید را به‌صورت تصویر بفرستید (برای انصراف /start).")


async def check_for_user(callback: types.CallbackQuery, payment: dict):
    """دکمه‌ی «پرداخت را انجام دادم / بررسی کن» برای درگاه‌های جدید."""
    status = payment["status"]
    if status == "review":
        await callback.answer("⏳ پرداخت شما در حال بررسی توسط مدیریت است.", show_alert=True)
        return
    if status == "rejected":
        await callback.answer("❌ این پرداخت توسط مدیریت رد شده است. با پشتیبانی در ارتباط باشید.", show_alert=True)
        return
    await callback.answer(ui_editor.get_alert_text("msg_ee96bfd443", "⏳ در حال بررسی وضعیت پرداخت..."))
    res = await settle_gateway_payment(callback.bot, payment)
    if res.state == "paid":
        return
    if res.state == "review":
        await callback.answer("⏳ پرداخت شما ثبت شد و برای تأیید نهایی به مدیریت ارجاع شد.", show_alert=True)
    elif res.state == "closed":
        await callback.answer("⏰ این فاکتور در درگاه منقضی/لغو شده است. اگر پرداخت کرده‌اید با پشتیبانی تماس بگیرید، وگرنه سفارش را دوباره ثبت کنید.", show_alert=True)
    elif res.state == "error":
        await callback.answer("⚠️ فعلاً امکان استعلام وضعیت نیست. چند لحظه بعد دوباره بزنید.", show_alert=True)
    elif res.state in ("action_less", "action_more"):
        await callback.answer("⚠️ برای تکمیل پرداخت اقدام لازم است؛ راهنما در پیام بعدی ارسال شد.", show_alert=False)
        await _send_tonpays_action(callback.bot, payment, res)
    elif status == "expired":
        await callback.answer("⏰ مهلت این فاکتور تمام شده است. اگر پرداخت کرده‌اید، پس از تأیید درگاه سفارش شما خودکار تحویل داده می‌شود.", show_alert=True)
    else:
        await callback.answer(
            "⏳ هنوز پرداختی برای این فاکتور تأیید نشده. اگر همین الان پرداخت کردید، چند لحظه صبر کنید و دوباره بزنید.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# تأیید/رد دستی ادمین
# ---------------------------------------------------------------------------
def _can_review(user_id: int) -> bool:
    try:
        return user_id == ADMIN_ID or any(
            db.sub_admin_has_permission(str(user_id), perm) for perm in ("receipts", "requests", "payment_methods")
        )
    except Exception:
        return user_id == ADMIN_ID


async def _finish_review_message(callback: types.CallbackQuery, suffix: str):
    try:
        base = callback.message.text or callback.message.caption or ""
        await callback.message.edit_text(base + "\n\n" + suffix, reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^gwok_\d+$"))
async def gw_review_approve(callback: types.CallbackQuery):
    if not _can_review(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    pid = int(callback.data.split("_", 1)[1])
    if not db.approve_review_gateway_payment(pid):
        await callback.answer("این مورد قبلاً بررسی شده یا دیگر در وضعیت بررسی نیست.", show_alert=True)
        return
    payment = db.get_online_payment(pid)
    await callback.answer("✅ تأیید شد؛ در حال تحویل...")
    try:
        await finalize_gateway_payment(callback.bot, payment)
        await _finish_review_message(callback, f"✅ توسط {callback.from_user.full_name} تأیید و تحویل شد.")
    except Exception:
        logger.exception("تحویل پس از تأیید دستی ناموفق بود (payment=%s)", pid)
        await _finish_review_message(callback, "⚠️ تأیید ثبت شد ولی تحویل با خطا مواجه شد؛ پولر دوباره تلاش می‌کند.")


@router.callback_query(F.data.regexp(r"^gwno_\d+$"))
async def gw_review_reject(callback: types.CallbackQuery):
    if not _can_review(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    pid = int(callback.data.split("_", 1)[1])
    payment = db.get_online_payment(pid)
    if not db.reject_review_gateway_payment(pid):
        await callback.answer("این مورد قبلاً بررسی شده یا دیگر در وضعیت بررسی نیست.", show_alert=True)
        return
    await callback.answer("❌ رد شد.")
    await _finish_review_message(callback, f"❌ توسط {callback.from_user.full_name} رد شد (هیچ شارژ/سفارشی انجام نشد).")
    try:
        if payment:
            await callback.bot.send_message(
                int(payment["telegram_id"]),
                f"❌ پرداخت شما (فاکتور {payment['id']}) پس از بررسی تأیید نشد. در صورت نیاز با پشتیبانی در ارتباط باشید.",
            )
    except Exception:
        logger.exception("اعلان رد دستی به کاربر ناموفق بود")
