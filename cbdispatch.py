"""اجرای مستقیم یک callback (برای دکمه‌های دلخواه ادمین) بدون اینکه کاربر دکمه‌ی اضافه ببیند.

یک CallbackQuery مصنوعی ساخته و از خود Dispatcher عبور می‌دهیم؛ پیام «⏳» ربات به‌عنوان پیام دکمه
استفاده می‌شود تا هندلرهایی که edit_text می‌کنند همان پیام را به صفحه‌ی مقصد تبدیل کنند.
اگر هندلر پیام را ویرایش نکرد (مثلاً پیام جدید فرستاد)، پیام «⏳» پاک می‌شود."""
import logging
import re
import uuid

from aiogram import types

logger = logging.getLogger(__name__)

DP = None  # توسط bot.py مقداردهی می‌شود
_EDITED: set[tuple[int, int]] = set()
SYN_PREFIX = "syn_"
_SAFE = re.compile(r"^[\w\-|:.,@]{1,64}$")


def is_synthetic(callback_id) -> bool:
    return str(callback_id or "").startswith(SYN_PREFIX)


def note_edit(method) -> None:
    """از Bot.__call__ صدا زده می‌شود؛ ویرایش پیام‌ها را ثبت می‌کند."""
    try:
        if not type(method).__name__.startswith("Edit"):
            return
        cid, mid = getattr(method, "chat_id", None), getattr(method, "message_id", None)
        if cid is not None and mid is not None:
            _EDITED.add((int(cid), int(mid)))
            if len(_EDITED) > 5000:
                _EDITED.clear()
    except Exception:
        pass


async def open_callback(bot, user: types.User, chat_id: int, data: str) -> bool:
    """callback_data را مثل کلیک روی دکمه اجرا می‌کند. True = اجرا شد."""
    data = str(data or "").strip()
    if DP is None or not data or not _SAFE.match(data):
        return False
    try:
        placeholder = await bot.send_message(chat_id, "⏳", _skip_auto_text=True)
    except Exception:
        logger.exception("ارسال پیام موقت برای دکمه‌ی دلخواه ناموفق بود")
        return False
    key = (int(chat_id), int(placeholder.message_id))
    try:
        cq = types.CallbackQuery(
            id=f"{SYN_PREFIX}{uuid.uuid4().hex}", from_user=user, chat_instance="synthetic",
            message=placeholder, data=data,
        )
        await DP.feed_update(bot, types.Update(update_id=0, callback_query=cq))
    except Exception:
        logger.exception("اجرای مستقیم دکمه‌ی دلخواه ناموفق بود: %s", data)
    finally:
        if key not in _EDITED:
            try:
                await bot.delete_message(chat_id, placeholder.message_id)
            except Exception:
                pass
        _EDITED.discard(key)
    return True
