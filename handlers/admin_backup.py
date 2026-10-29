"""ادمین: صفحه‌ی «💾 بکاپ» — دریافت فوری + بک‌آپ خودکار (هر N روز، ساعت ۰۰:۰۰ تهران).

callbackها با پیشوند admin_backup هستند تا همان دسترسی «backup» را بگیرند. دکمه‌ها مستقیم aiogram‌اند."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as KB

import adv_reports
import database as db
from config import ADMIN_ID

router = Router()
logger = logging.getLogger(__name__)
PRESETS = (1, 2, 3, 7, 14, 30)


class BackupStates(StatesGroup):
    waiting_days = State()


def _allowed(uid: int) -> bool:
    return uid == ADMIN_ID or (db.is_sub_admin(str(uid)) and db.sub_admin_has_permission(str(uid), "backup"))


def settings() -> dict:
    try:
        days = max(1, min(365, int(db.get_setting("autobackup_days", "7") or 7)))
    except ValueError:
        days = 7
    return {"enabled": db.get_setting("autobackup_enabled", "0") == "1", "days": days,
            "last_date": db.get_setting("autobackup_last_date", "") or ""}


def page_text() -> str:
    s = settings()
    topic = "✅ فعال" if adv_reports.ready("backup") else "⚪️ تاپیک بک‌آپ تنظیم نشده (گزارشات پیشرفته)"
    return (
        "💾 بک‌آپ\n\n"
        "📥 «دریافت بک‌آپ» همین لحظه نسخه‌ی دیتابیس را در همین ربات و (اگر تنظیم باشد) تاپیک بک‌آپ گروه می‌فرستد.\n\n"
        f"⏰ بک‌آپ خودکار: {'🟢 روشن' if s['enabled'] else '🔴 خاموش'}\n"
        f"📅 هر {s['days']} روز، ساعت ۰۰:۰۰ (به وقت تهران)\n"
        f"📨 تاپیک گروه: {topic}\n"
        + (f"🕛 آخرین بک‌آپ خودکار: {s['last_date']}" if s["last_date"] else "")
    )


def page_kb() -> KB:
    s = settings()
    rows = [
        [B(text="📥 دریافت بک‌آپ", callback_data="admin_backup_now", style="primary")],
        [B(text=("🟢 بک‌آپ خودکار: روشن" if s["enabled"] else "🔴 بک‌آپ خودکار: خاموش"), callback_data="admin_backup_toggle", style="success" if s["enabled"] else "danger")],
        [B(text=(f"{'✅ ' if s['days'] == d else ''}{d} روز"), callback_data=f"admin_backup_days|{d}", style="success" if s["days"] == d else None) for d in PRESETS[:3]],
        [B(text=(f"{'✅ ' if s['days'] == d else ''}{d} روز"), callback_data=f"admin_backup_days|{d}", style="success" if s["days"] == d else None) for d in PRESETS[3:]],
        [B(text="✏️ تعداد روز دلخواه", callback_data="admin_backup_custom", style="primary")],
        [B(text="🔙 بازگشت", callback_data="admin_cat_system", style="danger")],
    ]
    return KB(inline_keyboard=rows)


async def show_page(target, edit: bool = False):
    try:
        if edit:
            await target.edit_text(page_text(), reply_markup=page_kb())
            return
    except Exception:
        pass
    await target.answer(page_text(), reply_markup=page_kb())


@router.callback_query(F.data == "admin_backup")
async def backup_open(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    await state.clear()
    await show_page(cb.message, edit=True)


@router.callback_query(F.data == "admin_backup_now")
async def backup_now(cb: types.CallbackQuery):
    if not _allowed(cb.from_user.id):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer("⏳ در حال ساخت بک‌آپ...")
    res = await asyncio.to_thread(adv_reports.backup_everywhere, True)
    if not res["admin"] and not res["topic"]:
        await cb.message.answer("❌ خطا در ساخت/ارسال بک‌آپ.")
        return
    note = "✅ بک‌آپ ارسال شد."
    if res["topic"] is True:
        note += "\n📨 در تاپیک بک‌آپ گروه هم ارسال شد."
    elif res["topic"] is False:
        note += "\n⚠️ ارسال به تاپیک گروه ناموفق بود."
    await cb.message.answer(note)


@router.callback_query(F.data == "admin_backup_toggle")
async def backup_toggle(cb: types.CallbackQuery):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    db.set_setting("autobackup_enabled", "0" if settings()["enabled"] else "1")
    await show_page(cb.message, edit=True)


@router.callback_query(F.data.startswith("admin_backup_days|"))
async def backup_days(cb: types.CallbackQuery):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    try:
        d = int(cb.data.split("|")[1])
    except ValueError:
        return
    db.set_setting("autobackup_days", str(max(1, min(365, d))))
    await show_page(cb.message, edit=True)


@router.callback_query(F.data == "admin_backup_custom")
async def backup_custom(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    await state.set_state(BackupStates.waiting_days)
    await cb.message.answer("✏️ هر چند روز یک‌بار بک‌آپ خودکار گرفته شود؟ (عدد ۱ تا ۳۶۵)")


@router.message(BackupStates.waiting_days)
async def backup_custom_value(message: types.Message, state: FSMContext):
    if not _allowed(message.from_user.id):
        return
    try:
        d = int((message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
    except ValueError:
        d = 0
    if not 1 <= d <= 365:
        await message.answer("❌ یک عدد بین ۱ تا ۳۶۵ بفرستید:")
        return
    db.set_setting("autobackup_days", str(d))
    await state.clear()
    await show_page(message)


async def autobackup_loop(bot=None):
    """هر ۳۰ ثانیه چک می‌کند؛ ساعت ۰۰:xx تهران، اگر روشن باشد و فاصله‌ی روزها رسیده باشد، بک‌آپ می‌فرستد."""
    from datetime import date
    from utils import now_tehran_naive
    while True:
        try:
            now = now_tehran_naive()
            s = settings()
            if s["enabled"] and now.hour == 0 and s["last_date"] != now.strftime("%Y-%m-%d"):
                due = True
                if s["last_date"]:
                    try:
                        y, m, d = (int(x) for x in s["last_date"].split("-"))
                        due = (now.date() - date(y, m, d)).days >= s["days"]
                    except Exception:
                        due = True
                if due:
                    db.set_setting("autobackup_last_date", now.strftime("%Y-%m-%d"))
                    await asyncio.to_thread(adv_reports.backup_everywhere, False)
        except Exception:
            logger.exception("autobackup loop")
        await asyncio.sleep(30)
