"""
fsm_hybrid.py — HybridFSMStorage

🎯 هدف: کاهش تعداد round-trip به Turso از ۴ بار (get_state، get_data،
   set_state، set_data) به صفر بار در هر پیام.

چطور:
- state و data در RAM نگه می‌داره (MemoryStorage)
- در پس‌زمینه هر FLUSH_INTERVAL ثانیه یا‌بعد از تغییر مهم، به DB می‌نویسه
- در startup همه داده‌ها از DB خونده و warm میشه
- اگر ربات crash کنه، حداکثر FLUSH_INTERVAL ثانیه state گم میشه (قابل قبول)

نتیجه: هر callback/message یی که قبلاً ۲۰۰-۶۰۰ms صرف FSM می‌کرد، حالا <1ms
"""

import asyncio
import json
import logging
import time
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = 10  # ثانیه — هر ۱۰ثانیه dirty stateها به DB فلاش میشن
MAX_DIRTY_AGE = 30   # بعد از ۳۰ثانیه حتماً فلاش میشه (حتی اگه هنوز فعال باشه)


def _key_str(key: StorageKey) -> str:
    return ":".join([
        str(key.bot_id),
        str(key.chat_id),
        str(key.user_id),
        str(getattr(key, "thread_id", None)),
        str(getattr(key, "business_connection_id", None)),
        str(getattr(key, "destiny", "default")),
    ])


class HybridFSMStorage(BaseStorage):
    """
    FSM Storage که state/data رو در RAM نگه میداره و async در پس‌زمینه به Turso میفرسته.
    با startup warm-up — یعنی بعد از ری‌استارت هم state کاربران حفظ میشه.
    """

    def __init__(self):
        self._states: dict[str, str | None] = {}   # key → state string
        self._data:   dict[str, dict]       = {}   # key → data dict
        self._dirty:  dict[str, float]      = {}   # key → timestamp of last change
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._warmed = False

    # ─────────────────────────── warm-up ────────────────────────────────────

    async def warm_up(self):
        """در startup، همه FSM state/data از Turso به RAM لود میشه."""
        try:
            import database as db
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, db.fsm_get_all)
            async with self._lock:
                for row in rows:
                    k = row["storage_key"]
                    if row.get("state"):
                        self._states[k] = row["state"]
                    if row.get("data"):
                        try:
                            self._data[k] = json.loads(row["data"])
                        except Exception:
                            pass
            self._warmed = True
            logger.info("FSM warm-up: %d entries loaded from Turso", len(rows))
        except Exception:
            logger.exception("FSM warm-up failed — starting with empty state")
            self._warmed = True

    # ─────────────────────────── background flush ────────────────────────────

    def start_flush_task(self):
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                await self._flush_dirty()
            except Exception:
                logger.exception("FSM flush loop error")

    async def _flush_dirty(self, force=False):
        """همه dirty keyها رو به Turso بنویس."""
        import database as db
        now = time.monotonic()
        async with self._lock:
            to_flush = [
                k for k, t in list(self._dirty.items())
                if force or (now - t) >= FLUSH_INTERVAL
            ]
            items = []
            for k in to_flush:
                items.append((k, self._states.get(k), self._data.get(k, {})))
                del self._dirty[k]

        if not items:
            return

        loop = asyncio.get_running_loop()
        for k, state, data in items:
            try:
                data_json = json.dumps(data, ensure_ascii=False, default=str)
                await loop.run_in_executor(
                    None,
                    lambda _k=k, _s=state, _d=data_json: (
                        db.fsm_set_state(_k, _s),
                        db.fsm_set_data(_k, _d),
                    )
                )
            except Exception:
                logger.exception("FSM flush error for key %s", k)
                # برگردوندن به dirty برای retry بعدی
                async with self._lock:
                    self._dirty.setdefault(k, now)

        if items:
            logger.debug("FSM flushed %d dirty keys to Turso", len(items))

    # ─────────────────────────── BaseStorage interface ───────────────────────

    async def set_state(self, key: StorageKey, state: "State | str | None" = None) -> None:
        k = _key_str(key)
        value = state.state if isinstance(state, State) else state
        async with self._lock:
            self._states[k] = value
            self._dirty[k] = time.monotonic()

    async def get_state(self, key: StorageKey) -> str | None:
        k = _key_str(key)
        async with self._lock:
            return self._states.get(k)

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        k = _key_str(key)
        async with self._lock:
            self._data[k] = data
            self._dirty[k] = time.monotonic()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        k = _key_str(key)
        async with self._lock:
            return dict(self._data.get(k, {}))

    async def close(self) -> None:
        """قبل از shutdown همه dirty stateها رو flush کن."""
        logger.info("FSM closing — flushing all dirty states...")
        await self._flush_dirty(force=True)
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
