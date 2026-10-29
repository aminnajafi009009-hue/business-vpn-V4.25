"""ادمین: «⚖️ مصرف منصفانه» (سرویس و پنل) — روشن/خاموش، حد مصرف آزاد، سهمیه‌ی روزانه و لیست سرویس‌های محدودشده.

callbackها با پیشوند fup_ (دسترسی vpn_panel). منطق در fair_usage.py."""
from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as KB

import database as db
import fair_usage as fu
from config import ADMIN_ID

router = Router()
logger = logging.getLogger(__name__)


class FupStates(StatesGroup):
    waiting_free = State()
    waiting_daily = State()


def _allowed(uid: int) -> bool:
    return uid == ADMIN_ID or (db.is_sub_admin(str(uid)) and db.sub_admin_has_permission(str(uid), "vpn_panel"))


def page_text() -> str:
    c = fu.get_cfg()
    started = len(db.get_fup_started_configs())
    blocked = len(db.get_fup_blocked_configs())
    return (
        "⚖️ مصرف منصفانه\n\n"
        "برای سرویس‌های «نامحدود» متصل به پنل (به‌جز PAYG): کاربر تا حد مصرف آزاد بدون محدودیت مصرف می‌کند؛ "
        "بعد از آن هر روز فقط سهمیه‌ی روزانه مجاز است و با رسیدن به سقف، سرویس تا ساعت ۰۰:۰۰ متوقف می‌شود "
        "(کاربر نمی‌تواند با دکمه‌ی «فعال‌سازی» دورش بزند). سرویس‌های PAYG و حجم‌دار تحت‌تأثیر نیستند.\n\n"
        f"وضعیت: {'🟢 روشن' if c['enabled'] else '🔴 خاموش'}\n"
        f"🎯 حد مصرف آزاد: {fu.fmt_bytes(c['free_bytes'])}\n"
        f"📅 سهمیه‌ی روزانه بعد از آن: {fu.fmt_bytes(c['daily_bytes'])}\n\n"
        f"👥 سرویس‌های واردشده به مصرف منصفانه: {started}\n"
        f"⛔ هم‌اکنون متوقف: {blocked}\n\n"
        "ℹ️ بررسی هر ۵ دقیقه انجام می‌شود. متن اعلان‌ها از «ویرایش متن» و روشن/خاموش آن‌ها از «تنظیمات اعلان‌ها» قابل تغییر است."
    )


def page_kb() -> KB:
    c = fu.get_cfg()
    return KB(inline_keyboard=[
        [B(text=("🟢 مصرف منصفانه: روشن" if c["enabled"] else "🔴 مصرف منصفانه: خاموش"), callback_data="fup_toggle", style="success" if c["enabled"] else "danger")],
        [B(text="🎯 تنظیم حد مصرف آزاد", callback_data="fup_free", style="primary")],
        [B(text="📅 تنظیم سهمیه‌ی روزانه", callback_data="fup_daily", style="primary")],
        [B(text="📋 سرویس‌های محدودشده", callback_data="fup_list", style="primary")],
        [B(text="🔙 بازگشت", callback_data="admin_cat_service", style="danger")],
    ])


async def _show(target, edit=True):
    try:
        if edit:
            await target.edit_text(page_text(), reply_markup=page_kb())
            return
    except Exception:
        pass
    await target.answer(page_text(), reply_markup=page_kb())


@router.callback_query(F.data == "fup_open")
async def fup_open(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    await state.clear()
    await _show(cb.message)


@router.callback_query(F.data == "fup_toggle")
async def fup_toggle(cb: types.CallbackQuery):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    fu.set_enabled(not fu.get_cfg()["enabled"])
    await _show(cb.message)


@router.callback_query(F.data == "fup_free")
async def fup_free(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    await state.set_state(FupStates.waiting_free)
    await cb.message.answer(
        f"🎯 حد مصرف آزاد فعلی: {fu.fmt_bytes(fu.get_cfg()['free_bytes'])}\n\n"
        "کاربر تا این مقدار (کل مصرف سرویس) بدون هیچ محدودیتی مصرف می‌کند.\n"
        "مقدار جدید را بفرستید؛ مثلاً 100 (گیگابایت) یا 500MB یا 2.5GB:")


@router.callback_query(F.data == "fup_daily")
async def fup_daily(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    await state.set_state(FupStates.waiting_daily)
    await cb.message.answer(
        f"📅 سهمیه‌ی روزانه‌ی فعلی: {fu.fmt_bytes(fu.get_cfg()['daily_bytes'])}\n\n"
        "بعد از رسیدن به حد مصرف آزاد، کاربر هر روز تا این مقدار مجاز است و ساعت ۰۰:۰۰ صفر می‌شود.\n"
        "مقدار را بفرستید؛ مثلاً 5 (گیگابایت) یا 500MB:")


async def _save(message: types.Message, state: FSMContext, field: str, default_unit: str):
    if not _allowed(message.from_user.id):
        return
    val = fu.parse_amount(message.text or "", default_unit)
    if val is None or val < 0 or (field == "free_bytes" and val == 0) or val > 10_000_000 * fu.GB:
        await message.answer("❌ مقدار نامعتبر است. مثل 5 یا 500MB یا 2.5GB بفرستید:")
        return
    c = fu.get_cfg(); c[field] = val; fu.save_cfg(c)
    await state.clear()
    await _show(message, edit=False)


@router.message(FupStates.waiting_free)
async def fup_free_value(message: types.Message, state: FSMContext):
    await _save(message, state, "free_bytes", "gb")


@router.message(FupStates.waiting_daily)
async def fup_daily_value(message: types.Message, state: FSMContext):
    await _save(message, state, "daily_bytes", "gb")


@router.callback_query(F.data == "fup_list")
async def fup_list(cb: types.CallbackQuery):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    blocked = db.get_fup_blocked_configs(limit=30)
    started = [x for x in db.get_fup_started_configs(limit=30) if not x.get("fup_blocked")][:15]
    rows = []
    for tag, items in (("⛔", blocked), ("⚖️", started)):
        for x in items:
            rows.append([B(text=f"{tag} {x.get('service_id') or x.get('plan')}"[:60], callback_data=f"svcdetail_{x['id']}", style="primary")])
    rows.append([B(text="🔙 بازگشت", callback_data="fup_open", style="danger")])
    text = "📋 سرویس‌های محدودشده\n\n⛔ = هم‌اکنون متوقف | ⚖️ = وارد مصرف منصفانه (فعال)\nبرای مدیریت روی سرویس بزنید 👇" if len(rows) > 1 else "📋 سرویسی در مصرف منصفانه نیست."
    try:
        await cb.message.edit_text(text, reply_markup=KB(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=KB(inline_keyboard=rows))
