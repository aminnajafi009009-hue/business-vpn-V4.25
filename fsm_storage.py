"""
fsm_storage.py
۱) `storage`: یک reference بسیار ساده و سراسری به همان storage ای که در
   bot.py برای Dispatcher ساخته می‌شود.

چرا لازم است؟ وقتی خرید VIP با کیف‌پول یا پرداخت آنلاین انجام می‌شود، این
اتفاق در چت خود مشتری می‌افتد، نه چت ادمین. اگر بخواهیم بعد از ارسال خودکار
از پنل شاهراه (که معمولاً موفق است) در آن حالت نادری که لینک ساب به‌صورت
خودکار از پاسخ پنل پیدا نشود، از ادمین بخواهیم لینک را دستی پیست کند، باید
دقیقاً همان مکانیزم FSM (AdminStates.waiting_shahrah_manual_link) را این‌بار
روی چت خود ادمین صدا بزنیم، نه چت مشتری. برای ساختن FSMContext برای چت ادمین
از هر نقطه‌ای از کد (نه فقط از داخل یک handler که خودش state دارد)، به همین
reference مشترک نیاز داریم.

bot.py بلافاصله بعد از ساختن Dispatcher مقدار storage را اینجا قرار می‌دهد.

۲) `DBStorage`: 🐛 فیکس یک باگ — قبلاً Dispatcher با MemoryStorage (فقط RAM)
   ساخته می‌شد؛ یعنی با هر ری‌استارت پروسه‌ی ربات (دیپلوی مجدد، کرش، خواب
   رفتن سرویس رایگان و…)، همه‌ی وضعیت‌های چندمرحله‌ای (مثلاً «ادمین در حال
   ساخت پلن VIP جدید است» یا «کاربر منتظر آپلود رسید است») گم می‌شد و کاربر/
   ادمین بدون هیچ پیامی وسط کار گیر می‌کرد. DBStorage همان state/data را در
   جدول fsm_storage همان دیتابیس اصلی پروژه (SQLite/Turso) نگه می‌دارد؛ پس
   با ری‌استارت ربات از بین نمی‌رود، دقیقاً مثل بقیه‌ی داده‌های پروژه.
"""

import json
import logging
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)

storage = None


def _serialize_key(key: StorageKey) -> str:
    parts = [
        str(key.bot_id),
        str(key.chat_id),
        str(key.user_id),
        str(getattr(key, "thread_id", None)),
        str(getattr(key, "business_connection_id", None)),
        str(getattr(key, "destiny", "default")),
    ]
    return ":".join(parts)


class DBStorage(BaseStorage):
    """پیاده‌سازی سبک BaseStorage آیوگرم روی همان دیتابیس اصلی پروژه (به‌جای
    نگه‌داشتن state/data فقط در RAM)."""

    async def set_state(self, key: StorageKey, state: "State | str | None" = None) -> None:
        import database as db

        value = state.state if isinstance(state, State) else state
        try:
            db.fsm_set_state(_serialize_key(key), value)
        except Exception:
            logger.exception("خطا در ذخیره‌ی FSM state روی دیتابیس")

    async def get_state(self, key: StorageKey) -> str | None:
        import database as db

        try:
            return db.fsm_get_state(_serialize_key(key))
        except Exception:
            logger.exception("خطا در خواندن FSM state از دیتابیس")
            return None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        import database as db

        try:
            db.fsm_set_data(_serialize_key(key), json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            logger.exception("خطا در ذخیره‌ی FSM data روی دیتابیس")

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        import database as db

        try:
            raw = db.fsm_get_data(_serialize_key(key))
        except Exception:
            logger.exception("خطا در خواندن FSM data از دیتابیس")
            return {}
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    async def close(self) -> None:
        # اتصال دیتابیس در جای دیگری (database.py) مدیریت و بسته می‌شود.
        pass
