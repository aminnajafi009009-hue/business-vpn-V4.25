"""ادمین: «📊 گزارشات پیشرفته» — راه‌اندازی گروه تاپیک‌دار، وضعیت، پاسخ از داخل گروه و بک‌آپ هفتگی.

فایل جدا از admin.py. دکمه‌ها مستقیم aiogram هستند (نه ویرایشگر دکمه)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as KB

import adv_reports
import database as db
import notif_settings
from config import ADMIN_ID

router = Router()
logger = logging.getLogger(__name__)


class AdvStates(StatesGroup):
    waiting_group_id = State()


def _allowed(uid: int) -> bool:
    return uid == ADMIN_ID or (db.is_sub_admin(str(uid)) and db.sub_admin_has_permission(str(uid), "botinfo"))


def _status_text() -> str:
    c = adv_reports.get_cfg()
    lines = ["📊 گزارشات پیشرفته", ""]
    if not c["chat_id"]:
        lines += [
            "هنوز گروهی ثبت نشده است.",
            "با دکمه‌ی «🚀 راه‌اندازی» مراحل ساخت گروه و تاپیک‌ها را دنبال کنید.",
            "",
            "تاپیک‌هایی که ساخته می‌شوند:",
        ]
        lines += [f"• {t} — {d}" for _, t, d in adv_reports.TOPICS]
    else:
        lines.append(f"✅ گروه ثبت‌شده: {c['chat_id']}")
        lines.append(f"وضعیت کلی: {'🟢 روشن' if c['enabled'] else '🔴 خاموش'}")
        lines.append("")
        for k, t, _ in adv_reports.TOPICS:
            ok = bool(c["topics"].get(k))
            on = notif_settings.enabled("adv_" + k)
            lines.append(f"{'🟢' if ok and on else '⚪️' if ok else '❌'} {t}" + ("" if ok else " (تاپیک ساخته نشده)") + ("" if on or not ok else " — خاموش"))
        lines.append("")
        lines.append("روشن/خاموش هر تاپیک از «🔔 تنظیمات اعلان‌ها» انجام می‌شود.")
    return "\n".join(lines)


def _kb() -> KB:
    c = adv_reports.get_cfg()
    rows = [[B(text="🚀 راه‌اندازی / تغییر گروه", callback_data="adv_setup", style="primary")]]
    if c["chat_id"]:
        rows.append([B(text=("🔴 خاموش‌کردن کل گزارشات" if c["enabled"] else "🟢 روشن‌کردن کل گزارشات"), callback_data="adv_toggle", style="danger" if c["enabled"] else "success")])
        rows.append([B(text="🔄 ساخت مجدد تاپیک‌های ناقص", callback_data="adv_recreate"), B(text="💾 بک‌آپ همین حالا", callback_data="adv_backup_now")])
        rows.append([B(text="🗑 حذف تنظیمات گروه", callback_data="adv_reset", style="danger")])
    rows.append([B(text="🔙 بازگشت", callback_data="admin_botinfo", style="danger")])
    return KB(inline_keyboard=rows)


async def _show(cb: types.CallbackQuery):
    try:
        await cb.message.edit_text(_status_text(), reply_markup=_kb())
    except Exception:
        await cb.message.answer(_status_text(), reply_markup=_kb())


@router.callback_query(F.data == "adv_open")
async def adv_open(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    await state.clear()
    await _show(cb)


@router.callback_query(F.data == "adv_setup")
async def adv_setup(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    me = await cb.bot.get_me()
    text = (
        "🚀 راه‌اندازی گزارشات پیشرفته\n\n"
        "۱) در تلگرام یک «گروه جدید» بسازید (فقط خودتان داخلش باشید).\n"
        "۲) در تنظیمات گروه ← Edit ← گزینه‌ی «Topics» را روشن کنید (گروه تبدیل به سوپرگروه تاپیک‌دار می‌شود).\n"
        "۳) برای گرفتن «آیدی عددی گروه»، ربات @RawDataBot (یا @getidsbot) را به گروه اضافه کنید؛ "
        "به‌محض اضافه‌شدن آیدی گروه (عددی که با -100 شروع می‌شود) را می‌فرستد. (یا دستور /id در @MissRose_bot). بعد از گرفتن عدد می‌توانید آن ربات را از گروه حذف کنید.\n"
        f"۴) همین ربات (@{me.username}) را به گروه اضافه و «ادمین با همه‌ی دسترسی‌ها» کنید (مخصوصاً Manage Topics).\n"
        "۵) آیدی عددی گروه را همین‌جا برای من بفرستید 👇\n\n"
        "بعد از آن، تاپیک‌ها خودکار ساخته می‌شوند."
    )
    await state.set_state(AdvStates.waiting_group_id)
    try:
        await cb.message.edit_text(text, reply_markup=KB(inline_keyboard=[[B(text="❌ انصراف", callback_data="adv_open", style="danger")]]))
    except Exception:
        await cb.message.answer(text, reply_markup=KB(inline_keyboard=[[B(text="❌ انصراف", callback_data="adv_open", style="danger")]]))


async def _create_topics(bot, chat_id: int, existing: dict) -> tuple[dict, list[str]]:
    topics = dict(existing)
    errors = []
    for key, title, desc in adv_reports.TOPICS:
        if topics.get(key):
            continue
        try:
            t = await bot.create_forum_topic(chat_id, title[:128])
            topics[key] = t.message_thread_id
            try:
                await bot.send_message(chat_id, f"{title}\n{desc}", message_thread_id=t.message_thread_id)
            except Exception:
                pass
            await asyncio.sleep(0.4)
        except Exception as exc:
            errors.append(f"{title}: {str(exc)[:120]}")
    return topics, errors


@router.message(AdvStates.waiting_group_id)
async def adv_group_id(message: types.Message, state: FSMContext):
    if not _allowed(message.from_user.id):
        return
    raw = (message.text or "").strip().replace("−", "-")
    try:
        chat_id = int(raw)
    except ValueError:
        await message.answer("❌ آیدی عددی نامعتبر است. عددی مثل -1001234567890 بفرستید.")
        return
    bot = message.bot
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        await message.answer("❌ گروه پیدا نشد. مطمئن شوید ربات عضو گروه است و آیدی درست است.")
        return
    if not getattr(chat, "is_forum", False):
        await message.answer("❌ قابلیت «Topics» در این گروه روشن نیست. آن را روشن کنید و دوباره آیدی را بفرستید.")
        return
    me = await bot.get_me()
    try:
        m = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        await message.answer("❌ وضعیت ربات در گروه خوانده نشد؛ ربات را ادمین کنید.")
        return
    if m.status not in ("administrator", "creator"):
        await message.answer("❌ ربات هنوز ادمین گروه نیست. آن را «ادمین با همه‌ی دسترسی‌ها» کنید و دوباره بفرستید.")
        return
    if m.status == "administrator" and not getattr(m, "can_manage_topics", False):
        await message.answer("❌ ربات دسترسی «Manage Topics» ندارد. دسترسی‌های ادمین را کامل کنید و دوباره بفرستید.")
        return
    wait = await message.answer("⏳ در حال ساخت تاپیک‌ها...")
    old = adv_reports.get_cfg()
    keep = old["topics"] if old["chat_id"] == chat_id else {}
    topics, errors = await _create_topics(bot, chat_id, keep)
    adv_reports.save_cfg({"chat_id": chat_id, "topics": topics, "enabled": True})
    await state.clear()
    msg = f"✅ گزارشات پیشرفته راه‌اندازی شد ({len(topics)} از {len(adv_reports.TOPICS)} تاپیک)."
    if errors:
        msg += "\n\n⚠️ خطاها:\n" + "\n".join(errors)
    try:
        await wait.delete()
    except Exception:
        pass
    await message.answer(msg + "\n\n" + _status_text(), reply_markup=_kb())


@router.callback_query(F.data == "adv_toggle")
async def adv_toggle(cb: types.CallbackQuery):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    c = adv_reports.get_cfg()
    c["enabled"] = not c["enabled"]
    adv_reports.save_cfg(c)
    await _show(cb)


@router.callback_query(F.data == "adv_recreate")
async def adv_recreate(cb: types.CallbackQuery):
    await cb.answer("⏳")
    if not _allowed(cb.from_user.id):
        return
    c = adv_reports.get_cfg()
    if not c["chat_id"]:
        return
    topics, errors = await _create_topics(cb.bot, int(c["chat_id"]), c["topics"])
    c["topics"] = topics
    adv_reports.save_cfg(c)
    if errors:
        await cb.message.answer("⚠️ خطاها:\n" + "\n".join(errors))
    await _show(cb)


@router.callback_query(F.data == "adv_reset")
async def adv_reset(cb: types.CallbackQuery):
    await cb.answer()
    if not _allowed(cb.from_user.id):
        return
    adv_reports.save_cfg({"chat_id": None, "topics": {}, "enabled": True})
    await _show(cb)


@router.callback_query(F.data == "adv_backup_now")
async def adv_backup_now(cb: types.CallbackQuery):
    if not _allowed(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer("⏳ در حال ساخت بک‌آپ...")
    ok = await asyncio.to_thread(adv_reports.send_backup, True)
    await cb.message.answer("✅ بک‌آپ در تاپیک بک‌آپ ارسال شد." if ok else "❌ ارسال بک‌آپ ناموفق بود (تاپیک بک‌آپ ساخته شده؟).")


# --------------------------------------------------------------------------- پاسخ از داخل گروه
@router.message(F.reply_to_message, F.chat.type.in_({"group", "supergroup"}))
async def group_reply(message: types.Message):
    cfg = adv_reports.get_cfg()
    if not cfg["chat_id"] or message.chat.id != int(cfg["chat_id"]):
        return
    if not _allowed(message.from_user.id):
        return
    rec = adv_reports.lookup_by_message(message.chat.id, message.reply_to_message.message_id)
    if not rec:
        return
    extra = rec.get("extra") or {}
    kind = extra.get("type")
    text = message.text or message.caption or ""
    if kind not in ("ticket", "collab") or not text.strip():
        return
    tid = extra.get("user_tid")
    try:
        if kind == "ticket":
            ticket = db.get_ticket(int(extra["ticket_id"]))
            if not ticket:
                return
            db.add_ticket_message(ticket["id"], "admin", text)
            from utils import send_notification_sticker as _sns
            try:
                await _sns(message.bot, int(tid), "notif_ticket_reply")
            except Exception:
                pass
            await message.bot.send_message(
                int(tid), f"💬 پاسخ پشتیبانی (تیکت #{ticket['id']}):\n\n{text}",
                reply_markup=KB(inline_keyboard=[[B(text="✍️ پاسخ", callback_data=f"ticketreply_{ticket['id']}", style="success")]]),
            )
        else:
            await message.bot.send_message(int(tid), f"💬 پاسخ پشتیبانی به درخواست همکاری شما:\n\n{text}")
        await message.reply("✅ برای کاربر ارسال شد.")
    except Exception:
        logger.exception("adv group reply failed")
        await message.reply("❌ ارسال ناموفق بود (شاید کاربر ربات را بلاک کرده).")


# --------------------------------------------------------------------------- بک‌آپ هفتگی
async def weekly_backup_loop(bot=None):
    """هر ساعت چک می‌کند؛ اگر ≥۷ روز از آخرین بک‌آپ گذشته باشد می‌فرستد."""
    while True:
        try:
            if adv_reports.ready("backup") and notif_settings.enabled("adv_backup") and adv_reports.backup_due(7):
                await asyncio.to_thread(adv_reports.send_backup, False)
        except Exception:
            logger.exception("weekly backup loop")
        await asyncio.sleep(3600)
