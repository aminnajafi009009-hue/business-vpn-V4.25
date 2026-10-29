"""
perf_middleware.py — لایه‌های performance برای aiogram

🎯 هدف: كاهش زمان پاسخ به زیر 100ms

اجزا: 
1. CallbackAnswerMiddleware — فوری قبل از handler، callback.answer() صدا می‌زنه
   → کاربر فوری پاسخ می‌گیرد (دور شدن ساعت شن)

2. UserCacheMiddleware — در outer middleware یک‌بار get_user() صدا می‌زنه
   و نتیجه رو در data["user"] می‌گذارد
   → همه handlerها با data.get("user") به‌جای db.get_user() بخونن

3. PerformanceLogMiddleware — روی requestهای کند (>100ms) لاگ می‌گیرد
"""

import asyncio
import logging
import time
import threading
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import db_async
from config import ADMIN_ID

_USER_CACHE: dict[int, tuple[float, Any]] = {}
_USER_CACHE_TTL = 5.0
_USER_CACHE_LOCK = threading.Lock()

def _cached_user(user_id: int):
    now = time.monotonic()
    with _USER_CACHE_LOCK:
        item = _USER_CACHE.get(int(user_id))
        if item and now - item[0] < _USER_CACHE_TTL:
            return item[1]
    return None

def _store_user(user_id: int, user: Any):
    with _USER_CACHE_LOCK:
        _USER_CACHE[int(user_id)] = (time.monotonic(), user)

logger = logging.getLogger(__name__)


class CallbackAnswerMiddleware(BaseMiddleware):
    """
    فوری callback.answer() صدا می‌زنه تا کاربر فوری پاسخ بگیرد.
    Handlerها می‌تونن با answer_cb_later=True این را اورراید کنن.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            import cb_state

            async def _late_answer():
                # اگر هندلر تا ۱ ثانیه جواب نداد، برای خاموش‌شدن ساعت شنی جواب خالی بده.
                await asyncio.sleep(1.0)
                if not cb_state.is_answered(event.id):
                    try:
                        await event.answer()
                    except Exception:
                        pass

            timer = asyncio.create_task(_late_answer())
            try:
                return await handler(event, data)
            finally:
                timer.cancel()
                if not cb_state.is_answered(event.id):
                    try:
                        await event.answer()
                    except Exception:
                        pass
        return await handler(event, data)


class UserCacheMiddleware(BaseMiddleware):
    """
    یک‌بار get_user رو صدا می‌زنه و نتیجه رو در data["user"] می‌گذارد.
    Handlerها باید data.get("user") بخونن به‌جای db.get_user() صدا کردن.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id if event.from_user else None

        if telegram_id == ADMIN_ID:
            # ادمین اصلی برای callbackهای مدیریتی به user record نیاز ندارد؛
            # این lookup فقط latency اضافه می‌کرد.
            data["user"] = None
        elif telegram_id:
            try:
                user = _cached_user(telegram_id)
                if user is None:
                    user = await db_async.get_user(telegram_id)
                    _store_user(telegram_id, user)
                data["user"] = user
            except Exception:
                logger.warning("خطا در UserCacheMiddleware", exc_info=True)
                data["user"] = None
        else:
            data["user"] = None

        return await handler(event, data)


class PerformanceLogMiddleware(BaseMiddleware):
    """
    requestهای کندتر از 100ms رو لاگ می‌گیرد.
    """
    SLOW_THRESHOLD_MS = 100

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        t0 = time.monotonic()
        result = await handler(event, data)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if elapsed_ms > self.SLOW_THRESHOLD_MS:
            # لاگ کردن تا بدونیم کجا کند است
            desc = ""
            if isinstance(event, CallbackQuery):
                desc = f"callback={event.data!r:.60s}"
            elif isinstance(event, Message):
                desc = f"msg={event.text!r:.60s}" if event.text else "msg=(media)"
            logger.warning(
                "⚠️ SLOW [%.0fms] %s",
                elapsed_ms,
                desc,
            )
        return result
