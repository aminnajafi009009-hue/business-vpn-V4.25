"""nav_history.py — «بازگشت» واقعی (یک پله عقب) و بدون پاک‌کردن پیام، برای پنل کاربر و ادمین.

مشکل قبلی
---------
* دکمه‌های «🔙 بازگشت» با callback ثابتِ از پیش‌نوشته (مثل admin_back / plans / back) کار می‌کردند؛ پس معمولاً چند پله
  به عقب می‌پریدند یا اصلاً پنجره را می‌بستند (admin_back پیام را پاک می‌کرد).
* هندلرها پیام را edit/delete می‌کردند و مسیر طی‌شده از بین می‌رفت.

راه‌حل (یک نقطه‌ی مرکزی، بدون دست‌زدن به صدها هندلر)
-------------------------------------------------
1) هر پیامِ «متنی + کیبورد اینلاین» که ربات می‌فرستد/ویرایش می‌کند به‌صورت یک «نما» (متن، entity، کیبورد) ثبت می‌شود.
   وقتی یک دکمه‌ی ناوبری، همان پیام را به صفحه‌ی دیگری ویرایش می‌کند، نمای قبلی در «پشته‌ی» همان پیام می‌رود.
2) دکمه‌ی «🔙 بازگشت» (نه «🏠 منوی اصلی»، نه «انصراف») یک نمای قبلی را از پشته برمی‌دارد و آن را
   **به‌صورت پیام جدید زیر پیام فعلی** می‌فرستد (هیچ‌چیز پاک یا ویرایش نمی‌شود) و حالت انتظار ورودی (FSM) را رها می‌کند.
   هیچ هندلری دوباره اجرا نمی‌شود، پس هیچ عملیاتی (حذف/تأیید/تغییر) تکرار نمی‌شود.
3) اگر پشته خالی بود (مثلاً بعد از ری‌استارت)، همان هندلر قبلیِ دکمه اجرا می‌شود، ولی در «حالت بازگشت»: هر
   edit_text/edit_caption به پیام جدید تبدیل و هر delete نادیده گرفته می‌شود.
دکمه‌ی «🏠 …منوی اصلی» و «انصراف/لغو» هم همیشه هندلر خودشان را اجرا می‌کنند (تا پاک‌سازی/خانه درست انجام شود)، ولی
در همان «حالت بازگشت» (بدون پاک‌کردن/ویرایش پیام).
"""
from __future__ import annotations

import contextvars
import logging
from collections import OrderedDict
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.methods import (
    DeleteMessage, EditMessageCaption, EditMessageReplyMarkup, EditMessageText, SendMessage,
)
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

_MAX_VIEWS = 6000
_MAX_DEPTH = 15


@dataclass
class _Ctx:
    chat: int
    msg: int
    back_mode: bool
    pressed: str = ""
    data: str = ""


_CTX: contextvars.ContextVar = contextvars.ContextVar("nav_ctx", default=None)
_VIEW: "OrderedDict[tuple, dict]" = OrderedDict()
_STACK: "OrderedDict[tuple, list]" = OrderedDict()
_UNDO: dict = {}   # id(method) -> (key, old_view, pushed) برای برگرداندن وضعیت وقتی ویرایش در تلگرام رد شد


# ---------------------------------------------------------------------------
# برچسب دکمه‌ها
# ---------------------------------------------------------------------------
def is_home_label(text: str | None) -> bool:
    t = str(text or "")
    return "🏠" in t or "بازگشت به منوی اصلی" in t or "بازگشت به خانه" in t


def is_cancel_label(text: str | None) -> bool:
    t = str(text or "")
    return "انصراف" in t or "لغو" in t


def is_step_back_label(text: str | None) -> bool:
    """«🔙 بازگشت …» = یک پله عقب (نه خانه، نه انصراف، نه صفحه‌بندی قبلی/بعدی)."""
    t = str(text or "")
    if not t or is_home_label(t) or is_cancel_label(t) or "قبلی" in t or "بعدی" in t:
        return False
    # مقصد صریح: «بازگشت به لیست سرویس‌ها» همیشه به لیست سرویس‌های همان کاربر می‌رود (حتی وقتی از نتایج جستجو آمده‌ایم)
    if "بازگشت به لیست سرویس" in t:
        return False
    return "بازگشت" in t or "🔙" in t or "بستن" in t


def _back_cb(markup) -> str | None:
    """callback_data آخرین دکمه‌ی «بازگشت» یک کیبورد (برای تشخیص اینکه ویرایش، ناوبری است یا فقط تازه‌سازی)."""
    try:
        found = None
        for row in getattr(markup, "inline_keyboard", None) or []:
            for b in row:
                if is_step_back_label(getattr(b, "text", "")) and getattr(b, "callback_data", None):
                    found = b.callback_data
        return found
    except Exception:
        return None


def _sig(markup) -> frozenset:
    try:
        return frozenset(str(b.callback_data) for row in (markup.inline_keyboard or []) for b in row
                         if getattr(b, "callback_data", None))
    except Exception:
        return frozenset()


def _cbs(markup) -> list:
    try:
        return [str(b.callback_data) for row in (markup.inline_keyboard or []) for b in row
                if getattr(b, "callback_data", None)]
    except Exception:
        return []


def _head(cb: str) -> str:
    import re as _re
    return _re.split(r"[_|:]", cb, 1)[0]


def _is_paging(old: dict, new: dict, pressed: str) -> bool:
    """صفحه‌بندی/سورت لیست (نه ناوبری): دکمه‌ی فشرده‌شده callbackی با شماره‌ی انتهایی دارد و هم نمای قبلی و هم نمای جدید
    چند callback هم‌خانواده (هم‌پیشوند) دارند؛ این‌طور «بازگشت» به صفحه‌ی قبلیِ لیست نمی‌رود و از لیست خارج می‌شود.
    فقط وقتی فعال می‌شود که برچسب دکمه‌ی فشرده‌شده نامشخص/غیرآیتمی باشد."""
    try:
        data = _CTX.get().data if _CTX.get() is not None else ""
        import re as _re
        if not data or not _re.search(r"\d+$", data):
            return False
        # لیست کاربران: جابه‌جایی بین سورت‌ها/صفحات (ul_<sort>_<page>) پشته‌ی بازگشت را پر نمی‌کند
        if _re.match(r"^ul_[a-z]+_\d+$", data) and any(_re.match(r"^ul_[a-z]+_\d+$", c) for c in _cbs(old["markup"])):
            return True
        if (pressed or "").strip() and not any(w in pressed for w in ("صفحه", "قبلی", "بعدی", "➡", "⬅", "◀", "▶")):
            return False
        h = _head(data)
        same_old = [c for c in _cbs(old["markup"]) if c != data and _head(c) == h]
        same_new = [c for c in _cbs(new["markup"]) if _head(c) == h]
        return bool(same_old) and bool(same_new)
    except Exception:
        return False


def _is_navigation(old: dict, new: dict, pressed: str) -> bool:
    """آیا این ویرایش «رفتن به صفحه‌ی دیگر» است (و نمای قبلی باید در پشته برود)؟ تازه‌سازی همان صفحه (سوییچ، صفحه‌بندی،
    بعد از ثبت مقدار) پشته را پر نمی‌کند."""
    if any(w in (pressed or "") for w in ("قبلی", "بعدی", "➡️", "⬅️", "صفحه", "◀", "▶", "⏮", "⏭")):
        return False
    if _is_paging(old, new, pressed):
        return False
    a, b = _sig(old["markup"]), _sig(new["markup"])
    if _back_cb(old["markup"]) != _back_cb(new["markup"]):
        return True
    if not a and not b:
        return False
    return len(a & b) / max(len(a | b), 1) < 0.7


def _remember(store: OrderedDict, key, value) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > _MAX_VIEWS:
        store.popitem(last=False)


def _view_of(method) -> dict | None:
    markup = getattr(method, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return None
    text = getattr(method, "text", None)
    ents = getattr(method, "entities", None)
    pm = getattr(method, "parse_mode", None)
    if isinstance(method, EditMessageCaption):
        text, ents, pm = getattr(method, "caption", None), getattr(method, "caption_entities", None), getattr(method, "parse_mode", None)
    if not text:
        return None
    return {"text": text, "entities": list(ents) if ents else None, "parse_mode": pm,
            "markup": markup, "lpo": getattr(method, "link_preview_options", None)}


def _is_int_chat(chat) -> bool:
    return isinstance(chat, int) and not isinstance(chat, bool)


# ---------------------------------------------------------------------------
# قلاب‌های Bot.__call__
# ---------------------------------------------------------------------------
def before_call(method):
    """(action, new_method, view): action ∈ ok | skip | replace."""
    try:
        ctx = _CTX.get()
        if isinstance(method, DeleteMessage) and ctx is not None and ctx.back_mode:
            return "skip", None, None
        if isinstance(method, EditMessageReplyMarkup) and ctx is not None and ctx.back_mode \
                and _is_int_chat(method.chat_id) and method.message_id == ctx.msg and method.chat_id == ctx.chat:
            return "skip", None, None
        if isinstance(method, (EditMessageText, EditMessageCaption)) and _is_int_chat(method.chat_id) \
                and method.message_id is not None:
            if ctx is not None and ctx.back_mode and method.chat_id == ctx.chat:
                is_cap = isinstance(method, EditMessageCaption)
                text = method.caption if is_cap else method.text
                if text:
                    new = SendMessage(
                        chat_id=method.chat_id, text=text,
                        entities=(method.caption_entities if is_cap else method.entities),
                        parse_mode=method.parse_mode,
                        link_preview_options=None if is_cap else method.link_preview_options,
                        reply_markup=method.reply_markup,
                    )
                    return "replace", new, _view_of(new)
            view = _view_of(method)
            key = (method.chat_id, method.message_id)
            old = _VIEW.get(key)
            if view is not None:
                pushed = False
                if ctx is not None and old is not None and ctx.chat == method.chat_id and ctx.msg == method.message_id \
                        and _is_navigation(old, view, ctx.pressed):
                    st = _STACK.setdefault(key, [])
                    st.append(old)
                    pushed = True
                    if len(st) > _MAX_DEPTH:
                        del st[0]
                _remember(_VIEW, key, view)
                if len(_UNDO) > 500:
                    _UNDO.clear()
                _UNDO[id(method)] = (key, old, pushed)
            return "ok", None, None
        if isinstance(method, SendMessage) and _is_int_chat(method.chat_id):
            return "ok", None, _view_of(method)
    except Exception:
        logger.debug("nav before_call failed", exc_info=True)
    return "ok", None, None


def failed_call(method) -> None:
    """ویرایش/ارسال در تلگرام رد شد (مثلاً BUTTON_USER_INVALID): نمای ثبت‌شده‌ی بی‌اعتبار را برنگردان‌ایم."""
    try:
        info = _UNDO.pop(id(method), None)
        if not info:
            return
        key, old, pushed = info
        if pushed:
            st = _STACK.get(key)
            if st:
                st.pop()
        if old is not None:
            _VIEW[key] = old
        else:
            _VIEW.pop(key, None)
    except Exception:
        logger.debug("nav failed_call failed", exc_info=True)


def after_call(method, view, result) -> None:
    _UNDO.pop(id(method), None)
    try:
        if not isinstance(method, SendMessage) or view is None:
            return
        mid = getattr(result, "message_id", None)
        chat = getattr(getattr(result, "chat", None), "id", None) or method.chat_id
        if mid is None or not _is_int_chat(chat):
            return
        key = (chat, mid)
        _remember(_VIEW, key, view)
        stack: list = []
        ctx = _CTX.get()
        if ctx is not None and ctx.chat == chat:
            # پیام جدید از دلِ یک دکمه: پشته‌ی پیام قبلی + نمای فعلی آن
            stack = list(_STACK.get((ctx.chat, ctx.msg), []))
            cur = _VIEW.get((ctx.chat, ctx.msg))
            if not ctx.back_mode and cur is not None and _back_cb(view["markup"]) is not None \
                    and _is_navigation(cur, view, ctx.pressed):
                stack.append(cur)
            stack = stack[-_MAX_DEPTH:]
        _remember(_STACK, key, stack)
    except Exception:
        logger.debug("nav after_call failed", exc_info=True)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
def _pressed_text(event) -> str | None:
    msg = getattr(event, "message", None)
    markup = getattr(msg, "reply_markup", None)
    data = getattr(event, "data", None)
    if not markup or data is None:
        return None
    for row in getattr(markup, "inline_keyboard", None) or []:
        for b in row:
            if getattr(b, "callback_data", None) == data:
                return getattr(b, "text", None)
    return None


class NavMiddleware(BaseMiddleware):
    """outer-middleware روی callback_query: تشخیص دکمه‌ی بازگشت و اجرای «پیام جدید زیر پیام قبلی»."""

    async def __call__(self, handler, event, data):
        msg = getattr(event, "message", None)
        chat = getattr(getattr(msg, "chat", None), "id", None)
        mid = getattr(msg, "message_id", None)
        if chat is None or mid is None:
            return await handler(event, data)
        text = _pressed_text(event)
        key = (chat, mid)
        if is_step_back_label(text):
            st = _STACK.get(key)
            if st:
                view = st.pop()
                try:
                    stack_copy = list(st)
                    token = _CTX.set(_Ctx(chat, mid, True, text or "", str(getattr(event, 'data', '') or '')))
                    try:
                        new = await event.bot.send_message(
                            chat_id=chat, text=view["text"], entities=view["entities"], parse_mode=view["parse_mode"],
                            link_preview_options=view["lpo"], reply_markup=view["markup"],
                        )
                    finally:
                        _CTX.reset(token)
                    _remember(_STACK, (chat, new.message_id), stack_copy)
                    try:
                        fsm = data.get("state")
                        if fsm is not None:
                            await fsm.set_state(None)
                    except Exception:
                        logger.debug("nav: state reset failed", exc_info=True)
                    try:
                        await event.answer()
                    except Exception:
                        pass
                    return None
                except Exception:
                    logger.exception("nav: ارسال نمای قبلی ناموفق بود؛ هندلر معمولی اجرا می‌شود")
                    st.append(view)
        back_mode = bool(text) and (is_step_back_label(text) or is_home_label(text) or is_cancel_label(text))
        token = _CTX.set(_Ctx(chat, mid, back_mode, text or "", str(getattr(event, 'data', '') or '')))
        try:
            return await handler(event, data)
        finally:
            _CTX.reset(token)


def install(dp) -> None:
    dp.callback_query.outer_middleware(NavMiddleware())
