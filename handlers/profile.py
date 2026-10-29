"""
handlers/profile.py
پروفایل حرفه‌ای کاربر (👤 کاربران) و تاریخچه خرید.
زیرمنوهای کیف پول آزاد/مسدود، تاریخچه تراکنش، و لینک دعوت
در فایل‌های wallet.py و referral.py پیاده شده‌اند (روی همون callback_dataها).
"""

import ui_editor


from aiogram import Router, F, types

import database as db
from utils import show_menu_with_sticker
from keyboards import profile_menu, back_button

router = Router(name="profile")


async def send_profile(bot, chat_id: int, tg_id: int) -> bool:
    """پروفایل (قالب قابل‌ویرایش با ایموجی پرمیوم) — مشترک بین دکمه‌ی شیشه‌ای و منوی پایین."""
    user = db.get_user(tg_id)
    if user is None:
        return False
    configs_count = len(db.get_configs(user["id"]))
    template_values = {
        "name": user["name"], "telegram_id": user["telegram_id"],
        "wallet": f"{user['wallet']:,}", "locked_wallet": f"{user['locked_wallet']:,}",
        "locked": f"{user['locked_wallet']:,}",
        "configs_count": str(configs_count), "total_purchase": f"{user['total_purchase']:,}",
        "joined": str(user["joined"]), "invited_count": str(user["invited_count"]),
        "successful_invites": str(user["successful_invites"]),
    }
    await show_menu_with_sticker(
        bot, chat_id, "profile", ui_editor.SCREENS["profile"]["default"],
        reply_markup=profile_menu(), template_values=template_values, ui_key="profile",
        drop_lines_regex=(None if db.referral_enabled() else r"موجودی در انتظار|تعداد دعوت|دعوت موفق|دعوت‌های موفق"),
    )
    return True


@router.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    if not await send_profile(callback.bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "purchase_history")
async def purchase_history(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    txs = db.get_transactions(user["id"], limit=50)
    purchases = [tx for tx in txs if tx["type"] == "purchase"]

    reports = "".join(
        f"📦 {tx['description']} | {tx['amount']:,} تومان | {tx['created_at']}\n"
        for tx in purchases[:15]
    ).rstrip()
    if not reports:
        reports = "هنوز خریدی انجام نداده‌اید."

    template_values = {
        "purchase_reports": reports,
        "total_spent": f"{sum(int(tx.get('amount') or 0) for tx in purchases):,}",
        "purchase_count": str(len(purchases)),
    }

    # دقیقاً همان معماری متن غنیِ ویرایشگر/پیام همگانی: متن و Entityهای ذخیره‌شده
    # از ui_editor خوانده می‌شوند و فقط placeholderهای زیر با داده‌ی همان کاربر
    # جایگزین می‌شوند.
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id,
        "purchase_history",
        ui_editor.SCREENS["purchase_history"].get("default", ""),
        reply_markup=back_button("profile", "🔙 بازگشت", screen="purchase_history"),
        template_values=template_values,
        ui_key="purchase_history",
    )
    await callback.answer()
