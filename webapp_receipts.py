"""اجرای «دقیقاً همان هندلرهای ربات» برای تأیید/رد رسیدها از داخل مینی‌اپ ادمین.

هندلرهای approve_/reject_/approvepay|/rejectpay|/approvecustom_/rejectcustom_ در handlers/admin.py
با یک CallbackQuery ساختگی (بدون پیام تلگرامی) اجرا می‌شوند؛ پس ثبت خرید، سفارش، دعوت/رفرال،
مصرف کد تخفیف، اعلان کاربر، لاگ فعالیت ادمین و ... یکسان با ربات انجام می‌شود."""
from __future__ import annotations

import asyncio
import loopsafe
import logging
from types import SimpleNamespace

from aiogram import Bot

import database as db
from config import TOKEN

log = logging.getLogger(__name__)


class _FakeMessage:
    photo = None
    document = None
    caption = None
    text = ""

    def __init__(self, bot, chat_id):
        self.bot, self.chat = bot, SimpleNamespace(id=int(chat_id))
        self.message_id = 0

    async def edit_text(self, *a, **k): return None
    async def edit_caption(self, *a, **k): return None
    async def edit_reply_markup(self, *a, **k): return None
    async def delete(self, *a, **k): return None

    async def answer(self, text=None, **kw):
        kw.pop("_skip_auto_text", None)
        try:
            return await self.bot.send_message(self.chat.id, text, **kw)
        except Exception:
            log.debug("fake message answer failed", exc_info=True)


class _FakeCallback:
    def __init__(self, bot, actor_id, actor_name, data):
        self.bot = bot
        self.data = data
        self.id = f"mini-{actor_id}-{abs(hash(data))}"
        self.from_user = SimpleNamespace(id=int(actor_id), full_name=actor_name, first_name=actor_name, username=None)
        self.message = _FakeMessage(bot, actor_id)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text=None, show_alert=False, **kw):
        if text:
            self.answers.append((str(text), bool(show_alert)))
        return True


def run_handler(handler_name: str, data: str, actor_id: int, actor_name: str):
    """خروجی: (ok, message). ok=False اگر هندلر هشدار/خطا برگرداند."""
    import handlers.admin as adm
    fn = getattr(adm, handler_name)

    async def _go():
        from tg_temp_bot import TempBot
        bot = TempBot(token=TOKEN)
        try:
            cb = _FakeCallback(bot, actor_id, actor_name, data)
            await fn(cb)
            return cb.answers
        finally:
            await bot.session.close()

    answers = loopsafe.run(_go())
    msg = answers[-1][0] if answers else ""
    alerts = [t for t, al in answers if al]
    return (not alerts), (alerts[-1] if alerts else msg)


def callback_for(kind: str, action: str, rec: dict):
    """(handler_name, callback_data) برای هر نوع رسید."""
    tid, rid = str(rec["telegram_id"]), rec.get("id")
    if kind == "charge":
        if action == "approve":
            return "approve_charge", f"approve_{tid}_{int(rec['amount'])}_{rid}"
        return "reject_charge", f"reject_{tid}_{rid}"
    if kind in ("plan_card", "plan_crypto"):
        if action == "approve":
            return "approve_purchase", f"approvepay|{tid}|{rec.get('plan_key') or rec.get('extra')}|{int(rec['amount'])}|{rid}"
        return "reject_purchase", f"rejectpay|{tid}|{rid}"
    raise ValueError(kind)
