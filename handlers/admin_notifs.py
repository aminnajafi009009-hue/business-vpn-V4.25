"""ادمین: «🔔 تنظیمات اعلان‌ها» — روشن/خاموش هر اعلان و لاگ.

فایل جدا از admin.py. دکمه‌ها مستقیم از aiogram ساخته می‌شوند (نه wrapper ویرایشگر دکمه) چون
مخصوص ادمین‌اند و نباید در «ویرایش متن/دکمه» بیایند."""
from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
import notif_settings
from config import ADMIN_ID

router = Router()
logger = logging.getLogger(__name__)


def _allowed(user_id: int) -> bool:
    return user_id == ADMIN_ID or (db.is_sub_admin(str(user_id)) and db.sub_admin_has_permission(str(user_id), "botinfo"))


def notif_text() -> str:
    return (
        "🔔 تنظیمات اعلان‌ها و لاگ‌ها\n\n"
        "زیر هر مورد دو دکمه‌ی «✅ روشن» و «❌ خاموش» است. موارد دارای 🛡 فقط برای ادمین ارسال می‌شوند.\n"
        "پیش‌فرض همه روشن است."
    )


def notif_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for it in notif_settings.all_items():
        tag = "🛡 " if it["audience"] == "admin" else ""
        on = it["enabled"]
        # ردیف اول: متن کامل اعلان؛ ردیف دوم: دو دکمه‌ی روشن/خاموش
        rows.append([InlineKeyboardButton(text=f"{tag}{it['label']}", callback_data="ntf_noop")])
        rows.append([
            InlineKeyboardButton(text="✅ روشن", callback_data=f"ntf_on|{it['key']}", style="success" if on else None),
            InlineKeyboardButton(text="❌ خاموش", callback_data=f"ntf_off|{it['key']}", style=None if on else "danger"),
        ])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_botinfo", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "ntf_open")
async def ntf_open(callback: types.CallbackQuery):
    await callback.answer()
    if not _allowed(callback.from_user.id):
        return
    try:
        await callback.message.edit_text(notif_text(), reply_markup=notif_keyboard())
    except Exception:
        pass


@router.callback_query(F.data == "ntf_noop")
async def ntf_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ntf_(on|off)\|[a-z_]+$"))
async def ntf_set(callback: types.CallbackQuery):
    if not _allowed(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    head, key = callback.data.split("|", 1)
    try:
        notif_settings.set_enabled(key, head == "ntf_on")
    except ValueError:
        await callback.answer("❌ مورد نامعتبر", show_alert=True)
        return
    await callback.answer("✅ روشن شد" if head == "ntf_on" else "❌ خاموش شد")
    try:
        await callback.message.edit_reply_markup(reply_markup=notif_keyboard())
    except Exception:
        pass  # تغییری در کیبورد نبوده (مثلاً دوباره روی همان وضعیت زده شده)
