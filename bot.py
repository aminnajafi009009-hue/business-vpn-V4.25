"""
bot.py
فایل اصلی اجرای ربات. تمام Routerهای پوشه‌ی handlers اینجا به Dispatcher
وصل می‌شوند. یک سرور Flask کوچک هم کنارش اجرا می‌شود تا Render سرویس را
"زنده" تشخیص بدهد (لازمه‌ی سرویس‌های نوع Web Service).
"""

import asyncio
import logging
import os
import threading

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.middlewares.base import BaseMiddleware

import database as db
import uniquepay
import alerts
import panels
import fsm_storage
import fsm_hybrid
import db_async
import perf_middleware
import rich_registry
from config import TOKEN, ADMIN_ID
import cache
from handlers import menu, start, wallet, profile, referral, plans, ticket, admin, panel_admin, gateways, admin_notifs, admin_reports, admin_backup, admin_fup
from handlers.plans import finalize_online_payment, finalize_custom_online_payment
from handlers.wallet import finalize_wallet_charge_online_payment
from handlers.gateways import settle_gateway_payment
import online_gateways as og
from alerts import check_usage_alerts, CHECK_INTERVAL_SECONDS
from sentry_setup import init_sentry

# 🆕 ادغام تک-سرویسی: به‌جای اجرای bot.py و webapp_api.py به‌صورت دو Web Service
# جدا روی Render (که با محدودیت زمانی/تعداد دیپلوی رایگان گیت‌هاب مشکل ایجاد
# می‌کرد)، اپ Flask کامل Mini App (تمام endpointهای /api/...) همینجا ایمپورت
# و در همان ترد Flask زیر اجرا می‌شود؛ یعنی با یک‌بار دیپلوی (یک Web Service)،
# هم ربات (long polling، در event loop اصلی) و هم بک‌اند Mini App (در یک ترد
# جدا) هر دو زنده و فعال می‌شوند و هر دو به همان یک آدرس Render متصل‌اند.
from webapp_api import app as flask_app

init_sentry()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _patch_global_user_text_editing():
    """تمام پیام‌های متنی مشتری را از یک نقطه از Editor عبور می‌دهد.

    این لایه برای پیام‌هایی است که در handlerها به‌صورت مستقیم answer/send_message
    شده‌اند و قبلاً اصلاً ui_key نداشتند؛ بنابراین ویرایشگر واقعاً سرتاسر ربات را
    پوشش می‌دهد، بدون دست‌زدن به پنل مدیریت.
    """
    try:
        import ui_editor
        import cache as _cache
        from aiogram import Bot as _Bot

        # کش 60ثانیه‌ای is_sub_admin — هر send_message یک DB call حذف می‌شه
        def _cached_is_sub_admin(telegram_id: str) -> bool:
            _k = f"isadm_bool:{telegram_id}"
            val, hit = _cache.get(_k)
            if hit:
                return val
            result = db.is_sub_admin(telegram_id)
            _cache.set(_k, result, ttl=60)
            return result
        from aiogram.types import CallbackQuery as _CallbackQuery
        if not getattr(_Bot, "_ui_editor_patched", False):
            import contextvars as _cv
            from aiogram.methods import (
                SendMessage as _SendMessage, EditMessageText as _EditMessageText,
                EditMessageCaption as _EditMessageCaption, SendPhoto as _SendPhoto,
                SendVideo as _SendVideo, SendDocument as _SendDocument,
                SendAnimation as _SendAnimation,
            )
            from aiogram.types import MessageEntity as _MessageEntity
            _skip_auto = _cv.ContextVar("ui_skip_auto_text", default=False)

            # callerهایی مثل show_menu_with_sticker متن را از قبل اعمال کرده‌اند.
            _orig_send_message = _Bot.send_message
            async def _send_message(self, *args, **kwargs):
                if kwargs.pop("_skip_auto_text", False):
                    tok = _skip_auto.set(True)
                    try:
                        return await _orig_send_message(self, *args, **kwargs)
                    finally:
                        _skip_auto.reset(tok)
                return await _orig_send_message(self, *args, **kwargs)
            _Bot.send_message = _send_message

            # همین پرچم برای ارسال مدیا (کپشن) هم: پیام همگانی کپشن/entityهای اصلی را نباید بازنویسی کند.
            def _wrap_media_skip(_name):
                _orig = getattr(_Bot, _name, None)
                if _orig is None:
                    return
                async def _w(self, *args, **kwargs):
                    if kwargs.pop("_skip_auto_text", False):
                        tok = _skip_auto.set(True)
                        try:
                            return await _orig(self, *args, **kwargs)
                        finally:
                            _skip_auto.reset(tok)
                    return await _orig(self, *args, **kwargs)
                setattr(_Bot, _name, _w)
            for _n in ("send_photo", "send_video", "send_animation", "send_document", "send_audio", "send_voice"):
                _wrap_media_skip(_n)

            # همه‌ی ارسال/ویرایش‌ها (از جمله message.answer و callback.message.edit_text
            # که مستقیم متدهای API را می‌سازند و از Bot.send_message رد نمی‌شوند)
            # از Bot.__call__ عبور می‌کنند؛ متن‌های ویرایش‌شده را همین‌جا اعمال می‌کنیم.
            _orig_call = _Bot.__call__
            def _apply_field(method, text_attr, ent_attr):
                text = getattr(method, text_attr, None)
                if not text:
                    return
                new_text, new_entities = ui_editor.apply_auto_text_with_entities(str(text))
                if new_text == text and not new_entities:
                    return
                setattr(method, text_attr, new_text)
                if new_entities and not getattr(method, ent_attr, None):
                    # parse_mode با entities هم‌زمان معنی ندارد
                    if hasattr(method, "parse_mode"):
                        method.parse_mode = None
                    setattr(method, ent_attr, [_MessageEntity(
                        type=e["type"], offset=e["offset"], length=e["length"],
                        custom_emoji_id=e.get("custom_emoji_id")
                    ) for e in new_entities])

            async def _call_inner(self, method, request_timeout=None):
                try:
                    if not _skip_auto.get():
                        chat_id = getattr(method, "chat_id", None)
                        if chat_id is not None and not isinstance(chat_id, str):
                            is_admin = int(chat_id) == int(ADMIN_ID) or _cached_is_sub_admin(str(chat_id))
                            if not is_admin:
                                if isinstance(method, _SendMessage):
                                    _rp = ui_editor.rich_text_match(str(method.text or ""))
                                    if _rp:
                                        try:
                                            import rich_message as _rm
                                            from aiogram.methods import SendRichMessage as _SRM
                                            _rmeth = _SRM(chat_id=method.chat_id, rich_message=_rm.to_input(_rp),
                                                          reply_markup=method.reply_markup)
                                            return await _orig_call(self, _rmeth, request_timeout=request_timeout)
                                        except Exception:
                                            logger.exception("rich text send failed; plain fallback")
                                if isinstance(method, (_SendMessage, _EditMessageText)):
                                    _apply_field(method, "text", "entities")
                                elif isinstance(method, (_EditMessageCaption, _SendPhoto, _SendVideo, _SendDocument, _SendAnimation)):
                                    _apply_field(method, "caption", "caption_entities")
                except Exception:
                    logger.exception("ui editor auto-text failed in Bot.__call__")
                # 💎 لایه‌ی مرکزی Premium Emoji / Rich Text: نام پلن‌ها، پوشه‌ها، راهنماها،
                # متن‌ها و آیکن دکمه‌ها برای همه (کاربر و ادمین) و در همه‌ی پیام‌ها حفظ می‌شود.
                _deco = None
                try:
                    _deco = rich_registry.decorate_method(method, bot=self)
                except Exception:
                    logger.exception("rich registry decoration failed in Bot.__call__")
                try:
                    return await _orig_call(self, method, request_timeout=request_timeout)
                except TelegramBadRequest as exc:
                    # اگر تلگرام یکی از entity/آیکن‌های اضافه‌شده را نپذیرفت، یک‌بار بدون آن‌ها تلاش کن.
                    if _deco and rich_registry.should_retry(exc):
                        logger.warning("rich registry decoration rejected (%s); retrying plain", exc)
                        _deco.undo(method)
                        return await _orig_call(self, method, request_timeout=request_timeout)
                    # دکمه‌ی «رفتن به پیوی کاربر» (tg://user?id=…) برای کاربری که ربات نمی‌شناسد/حریم‌خصوصی محدود دارد رد می‌شود
                    # (BUTTON_USER_INVALID / BUTTON_USER_PRIVACY_RESTRICTED)؛ همان پیام را بدون آن دکمه‌ها دوباره بفرست.
                    if any(k in str(exc) for k in ("BUTTON_USER_INVALID", "BUTTON_USER_PRIVACY_RESTRICTED")):
                        _mk = getattr(method, "reply_markup", None)
                        _rows = getattr(_mk, "inline_keyboard", None)
                        if _rows:
                            _new_rows = [[b for b in r if not str(getattr(b, "url", "") or "").startswith("tg://user")] for r in _rows]
                            _new_rows = [r for r in _new_rows if r]
                            try:
                                method.reply_markup = _mk.model_copy(update={"inline_keyboard": _new_rows})
                            except Exception:
                                method.reply_markup = None
                            return await _orig_call(self, method, request_timeout=request_timeout)
                    raise
            async def _call(self, method, request_timeout=None):
                # 🔙 بازگشت واقعی (یک پله عقب، بدون پاک‌کردن/ویرایش پیام) — nav_history.py
                import nav_history
                import cbdispatch
                cbdispatch.note_edit(method)
                _act, _m2, _view = nav_history.before_call(method)
                if _act == "skip":
                    return True
                if _act == "replace" and _m2 is not None:
                    method = _m2
                try:
                    _res = await _call_inner(self, method, request_timeout)
                except Exception:
                    nav_history.failed_call(method)
                    raise
                nav_history.after_call(method, _view, _res)
                return _res
            _Bot.__call__ = _call
            _Bot._ui_editor_patched = True

        if not getattr(_CallbackQuery, "_ui_editor_patched", False):
            _orig_answer = _CallbackQuery.answer
            async def _answer(self, *args, **kwargs):
                try:
                    uid = getattr(getattr(self, "from_user", None), "id", None)
                    text = kwargs.get("text", args[0] if args else None)
                    is_admin = uid is not None and (int(uid) == int(ADMIN_ID) or _cached_is_sub_admin(str(uid)))
                    if text and not is_admin:
                        new_text, _new_entities = ui_editor.apply_auto_text_with_entities(str(text))
                        if args:
                            args = list(args); args[0] = new_text; args = tuple(args)
                        else:
                            kwargs["text"] = new_text
                except Exception:
                    logger.exception("ui editor auto-text failed for callback answer")
                import cb_state
                _text = kwargs.get("text", args[0] if args else None)
                _alert = bool(kwargs.get("show_alert", args[1] if len(args) > 1 else False))
                import cbdispatch
                if cbdispatch.is_synthetic(self.id):
                    # callback مصنوعی (دکمه‌ی دلخواه): پاسخ به تلگرام معنی ندارد؛ هشدارها را به‌صورت پیام نشان بده.
                    if _text and _alert:
                        try:
                            msg = getattr(self, "message", None)
                            if msg is not None:
                                await msg.answer(str(_text))
                        except Exception:
                            logger.debug("synthetic alert failed", exc_info=True)
                    return True
                if cb_state.is_answered(self.id):
                    # تلگرام فقط یک پاسخ برای هر callback قبول می‌کند. پیام‌های هشدار را
                    # به‌جای گم‌شدن، داخل چت نشان می‌دهیم.
                    if _text and _alert:
                        try:
                            msg = getattr(self, "message", None)
                            if msg is not None:
                                await msg.answer(str(_text))
                        except Exception:
                            logger.debug("callback alert fallback failed", exc_info=True)
                    return True
                cb_state.mark_answered(self.id, bool(_text))
                try:
                    return await _orig_answer(self, *args, **kwargs)
                except Exception:
                    logger.debug("callback answer failed", exc_info=True)
                    return False
            _CallbackQuery.answer = _answer
            _CallbackQuery._ui_editor_patched = True
    except Exception:
        logger.exception("ui editor global text patch initialization failed")


_patch_global_user_text_editing()


class RichRegistryMiddleware(BaseMiddleware):
    """هر پیام ادمین/ساب‌ادمین که ایموجی پریمیوم یا فرمت (bold/quote/…) دارد را در رجیستری
    ثبت می‌کند؛ تا نام پلن/پوشه/راهنما/توضیحات و … هرجا نمایش داده شد، ایموجی پریمیوم بماند."""

    async def __call__(self, handler, event, data):
        try:
            user = getattr(event, "from_user", None)
            if user is not None:
                is_adm = int(user.id) == int(ADMIN_ID)
                if not is_adm:
                    _k = f"isadm_bool:{user.id}"
                    is_adm, hit = cache.get(_k)
                    if not hit:
                        is_adm = db.is_sub_admin(str(user.id))
                        cache.set(_k, is_adm, ttl=60)
                if is_adm:
                    rich_registry.register_from_message(event)
        except Exception:
            logger.exception("rich registry capture failed")
        return await handler(event, data)


class BlockedUserMiddleware(BaseMiddleware):
    """اعمال مسدودی روی تمام پیام‌ها و callbackها، نه فقط /start."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id != ADMIN_ID:
            cache_key = f"blocked_user:{user.id}"
            blocked, hit = cache.get(cache_key)
            if not hit:
                blocked = db.is_user_blocked(user.id)
                cache.set(cache_key, bool(blocked), ttl=5)
            if blocked:
                text = "🚫 دسترسی شما به ربات مسدود شده است. با پشتیبانی در ارتباط باشید."
                if getattr(event, "answer", None):
                    try:
                        if event.__class__.__name__ == "CallbackQuery":
                            await event.answer(text, show_alert=True)
                        else:
                            await event.answer(text)
                    except Exception:
                        logger.warning("اعلام مسدودی کاربر ناموفق بود", exc_info=True)
                return None
        return await handler(event, data)


# ---------------------------------------------------------------------------
# هندلر سراسری خطا — تا حالا اگر یک خطای پیش‌بینی‌نشده (باگ) وسط پردازش یک
# callback (مثلاً دکمه‌ی پرداخت) رخ می‌داد، چون هیچ‌جا callback.answer() صدا
# زده نمی‌شد، دکمه برای کاربر برای همیشه در حالت لودینگ/فریز می‌ماند و نه
# پیامی به کاربر می‌رسید و نه به ادمین — و ردیابی علتش هم سخت بود چون فقط در
# لاگ‌ها گم می‌شد. این هندلر تضمین می‌کند که:
# ۱) خطا با جزئیات کامل (traceback) در لاگ ثبت شود تا بعداً قابل پیگیری باشد.
# ۲) کاربر همیشه یک پیام/آلارم دریافت کند و دکمه از حالت لودینگ خارج شود،
#    به‌جای این‌که تا ابد فریز بماند.
# ---------------------------------------------------------------------------
async def global_error_handler(event: ErrorEvent):
    # 🐛 فیکس: خطای کاملاً بی‌ضرر و متداول تلگرام “محتوا تفاوتی ندارد” (message is not modified) را باید به‌طور جدایی مدیریت کرد: وقتی یک دکمه دوبار زده می‌شود یا handler دوباره همان متن/دکمه را edit می‌کند، تلگرام این خطا را برمی‌گرداند ولی هیچ مشکلی برای کاربر رخ نداده؛ قبلاً هم به لاگ و هم Sentry به‌عنوان خطای جدی ثبت می‌شد و به کاربر هم پیام خطای گمراه‌کننده نمایش داده می‌شد.
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower():
        logger.info("نادیده‌گرفتن خطای بی‌ضرر message-is-not-modified (کلیک دوباره/محتوای یکسان)")
        try:
            update = event.update
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
        except Exception:
            pass
        return True

    logger.exception(
        "خطای پیش‌بینی‌نشده هنگام پردازش آپدیت: %s", event.exception, exc_info=event.exception
    )
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(event.exception)
    except Exception:
        pass
    try:
        import traceback as _tb
        db.log_error(
            error_type=type(event.exception).__name__,
            message=str(event.exception),
            traceback_text="".join(_tb.format_exception(type(event.exception), event.exception, event.exception.__traceback__)),
            context="global_error_handler",
        )
    except Exception:
        pass
    update = event.update
    warning_text = "⚠️ خطایی پیش آمد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer(warning_text, show_alert=True)
            except Exception:
                # اگر این callback قبلاً یک‌بار answer شده باشد (مثلاً با پیام
                # "در حال دریافت..." قبل از شروع کار اصلی)، تلگرام دیگر اجازه‌ی
                # answer دوباره را نمی‌دهد و این except صدا زده می‌شود. قبلاً
                # همین‌جا خطا فقط لاگ می‌شد و کاربر هیچ پیامی نمی‌دید (دکمه فقط
                # در حالت لودینگ می‌ماند). حالا به‌جای answer، مستقیماً یک پیام
                # در همون چت می‌فرستیم تا کاربر همیشه یک نتیجه ببیند.
                logger.warning("امکان answer دوباره‌ی callback نبود؛ ارسال پیام مستقیم به چت.")
                if update.callback_query.message:
                    await update.callback_query.message.answer(warning_text)
        elif update.message:
            await update.message.answer(warning_text)
    except Exception:
        logger.exception("خطا حتی در تلاش برای اطلاع‌رسانی خطای اصلی به کاربر")
    return True


# ---------------------------------------------------------------------------
# Flask - همان اپ webapp_api (بک‌اند کامل Mini App) + یک مسیر health-check
# ساده برای اینکه Render سرویس را "زنده" تشخیص بدهد (لازمه‌ی Web Serviceها).
# ---------------------------------------------------------------------------
@flask_app.route("/")
def health_check():
    return "Bot + Mini App API هر دو در حال اجرا هستند ✅"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # threaded=True: تا درخواست‌های هم‌زمان Mini App (مثلاً چند کاربر) یکدیگر را بلاک نکنند
    # و همزمان با پاسخگویی ربات (در event loop اصلی asyncio) پردازش شوند.
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


# ---------------------------------------------------------------------------
# aiogram - ربات اصلی
# ---------------------------------------------------------------------------
async def run_bot():
    db.init_db()
    try:
        import bot_info as _bi; _bi.migrate_card_to_payment_settings()
    except Exception:
        pass
    # 🐛 HybridFSMStorage باید قبل از warm_up ساخته شود؛ در غیر این صورت
    # به‌خاطر assignment داخل همین تابع، پایتون آن را local تشخیص می‌دهد.
    _hybrid_storage = fsm_hybrid.HybridFSMStorage()
    # 🔥 FSM warm-up + Cache warm-up (async)
    await _hybrid_storage.warm_up()
    _hybrid_storage.start_flush_task()
    await db_async.warm_cache()
    recovered = db.recover_stuck_online_payments()
    if recovered:
        logger.warning("%d پرداخت processing قدیمی برای پردازش مجدد بازیابی شد.", recovered)
    logger.info("Database initialized.")
    rich_registry.backfill()

    bot = Bot(token=TOKEN)
    # 🐛 فیکس: قبلاً MemoryStorage (فقط RAM) بود، با هر ری‌استارت state گم می‌شد.
    dp = Dispatcher(storage=_hybrid_storage)
    import cbdispatch
    cbdispatch.DP = dp
    dp.errors.register(global_error_handler)
    blocked_middleware = BlockedUserMiddleware()
    dp.message.outer_middleware(blocked_middleware)
    dp.message.outer_middleware(RichRegistryMiddleware())
    dp.callback_query.outer_middleware(blocked_middleware)
    import nav_history
    nav_history.install(dp)

    # 🚀 Performance middlewares
    _cb_answer_mw = perf_middleware.CallbackAnswerMiddleware()
    _user_cache_mw = perf_middleware.UserCacheMiddleware()
    _perf_log_mw = perf_middleware.PerformanceLogMiddleware()

    dp.message.outer_middleware(_user_cache_mw)
    dp.callback_query.outer_middleware(_cb_answer_mw)
    dp.callback_query.outer_middleware(_user_cache_mw)
    dp.message.outer_middleware(_perf_log_mw)
    dp.callback_query.outer_middleware(_perf_log_mw)

    fsm_storage.storage = dp.storage

    # ترتیب ثبت Routerها مهم است: handler خاص‌تر باید زودتر بیاید.
    # menu (منوی پایین صفحه) باید همیشه اول باشد تا دکمه‌های ثابت پایین صفحه
    # در هر شرایطی (حتی وسط یک FSM دیگر) همیشه در دسترس و فعال باشند.
    # admin باید بعد از آن باشد چون فیلتر سخت‌گیرانه‌تری (ADMIN_ID) دارد
    # و برخی callback_dataهای مشترک (مثل state یکسان) را زودتر می‌گیرد.
    dp.include_router(admin_reports.router)
    dp.include_router(admin_backup.router)
    dp.include_router(admin_fup.router)
    dp.include_router(menu.router)
    dp.include_router(admin.router)
    dp.include_router(panel_admin.router)
    dp.include_router(admin_notifs.router)
    dp.include_router(start.router)
    dp.include_router(wallet.router)
    dp.include_router(profile.router)
    dp.include_router(referral.router)
    dp.include_router(gateways.router)
    dp.include_router(plans.router)
    dp.include_router(ticket.router)

    asyncio.create_task(usage_alert_loop(bot))
    asyncio.create_task(admin_backup.autobackup_loop(bot))
    import fair_usage
    asyncio.create_task(fair_usage.fup_loop(bot))
    try:
        import adv_reports
        adv_reports.install_error_handler()
    except Exception:
        pass
    asyncio.create_task(invoice_expiry_loop(bot))
    asyncio.create_task(self_ping_loop())
    # poller همیشه اجرا می‌شود (توکن ممکن است بعداً از پنل ادمین تنظیم شود)
    asyncio.create_task(online_payment_poller(bot))
    asyncio.create_task(payg_maintenance_loop(bot))
    asyncio.create_task(payg_resume_loop(bot))
    asyncio.create_task(wallet_low_loop(bot))

    logger.info("Bot starting polling...")
    await dp.start_polling(bot)


async def usage_alert_loop(bot: Bot):
    """هر ۳۰ دقیقه سرویس‌های VIP را برای هشدار ۸۰٪/۹۰٪ مصرف و ۲ روز به انقضا بررسی می‌کند.
    همچنین کانفیگ‌های منقضی‌شده را به‌صورت خودکار آرشیو می‌کند (deleted=1) تا
    از پنل کاربر حذف شوند."""
    while True:
        try:
            await check_usage_alerts(bot)
        except Exception:
            logger.exception("خطا در بررسی دوره‌ای هشدارهای مصرف/انقضا")
        try:
            archived = db.archive_expired_configs()
            if archived > 0:
                logger.info("کانفیگ‌های منقضی‌شده آرشیو شدند: %d مورد", archived)
        except Exception:
            logger.exception("خطا در آرشیو خودکار کانفیگ‌های منقضی‌شده")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


PAYG_CHECK_INTERVAL_SECONDS = int(os.environ.get("PAYG_CHECK_INTERVAL_SECONDS", "300") or 300)
PAYG_RESUME_POLL_SECONDS = 2


_PAYG_RESUMING: set = set()


async def _payg_resume_cfg(bot: Bot, cfg: dict, user: dict, panel: dict, used: int | None = None) -> bool:
    """فعال‌سازی دوباره‌ی سرویس PAYG متوقف‌شده (فقط اگر موجودی > 0). بین حلقه‌ی اصلی و صف شارژ هم‌زمان دوبار اجرا نمی‌شود."""
    cid = cfg['id']
    if cid in _PAYG_RESUMING:
        return False
    _PAYG_RESUMING.add(cid)
    try:
        if int(user.get('wallet') or 0) <= 0:
            return False
        ok_enable, enable_msg = await panels.enable_service(panel, cfg['service_id'])
        if not ok_enable:
            logger.warning('PAYG enable failed for cfg=%s: %s', cid, enable_msg)
            return False
        if used is None:
            ok, snap, _m = await panels.get_service_snapshot(panel, cfg['service_id'])
            used = int((snap or {}).get('used') or 0) if ok and isinstance(snap, dict) else int(cfg.get('payg_last_used_bytes') or 0)
        db.set_payg_paused(cid, False)
        db.update_payg_usage_snapshot(cid, used)
        await _notify_payg_resumed(bot, user)
        return True
    finally:
        _PAYG_RESUMING.discard(cid)


async def payg_resume_loop(bot: Bot):
    """بعد از هر شارژ کیف پول (تریگر دیتابیس → صف) ظرف ~۲ ثانیه سرویس‌های PAYG متوقف‌شده‌ی همان کاربر فعال می‌شوند."""
    await asyncio.sleep(10)
    while True:
        try:
            if db.get_payg_settings().get('enabled'):
                for uid in db.pop_payg_resume_users():
                    user = db.get_user_by_id(uid)
                    if not user or int(user.get('wallet') or 0) <= 0:
                        continue
                    for cfg in db.get_paused_payg_configs_for_user(uid):
                        panel = db.get_vpn_panel(cfg['panel_id']) if cfg.get('panel_id') else None
                        if panel and panel.get('enabled'):
                            try:
                                await _payg_resume_cfg(bot, cfg, user, panel)
                            except Exception:
                                logger.exception('خطا در فعال‌سازی فوری PAYG cfg=%s', cfg.get('id'))
        except Exception:
            logger.exception('خطا در حلقه‌ی فعال‌سازی فوری PAYG')
        await asyncio.sleep(PAYG_RESUME_POLL_SECONDS)


async def _notify_wallet_low(bot: Bot, user: dict):
    try:
        import ui_editor
        from utils import send_notification_sticker as _sns, send_rich_template as _srt
        from aiogram.types import MessageEntity
        from keyboards import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
        _key = 'notif_wallet_low'
        _vals = {'balance': f"{int(user.get('wallet') or 0):,}"}
        tg = int(user['telegram_id'])
        await _sns(bot, tg, _key)
        _kb = _IKM(ui_screen=_key, inline_keyboard=[[_IKB(text='💵 شارژ کیف پول', callback_data='wallet', style='primary')]])
        if await _srt(bot, tg, _key, _vals, reply_markup=_kb) is not None:
            return
        _t, _raw = ui_editor.render_template(_key, _vals, fallback=ui_editor.SCREENS[_key]['default'])
        _ents = [MessageEntity(type=e['type'], offset=e['offset'], length=e['length'], custom_emoji_id=e.get('custom_emoji_id')) for e in (_raw or [])]
        await bot.send_message(tg, _t, entities=_ents or None, reply_markup=_kb)
    except Exception:
        logger.exception('ارسال اعلان کم‌شدن موجودی کیف پول ناموفق بود')


async def wallet_low_loop(bot: Bot):
    """هشدار کم‌شدن موجودی: وقتی موجودی کاربر از «حد تعیین‌شده‌ی ادمین» (تنظیمات کیف پول) به پایین‌تر برود، یک‌بار اعلان می‌رود."""
    await asyncio.sleep(12)
    while True:
        try:
            rows = db.pop_wallet_low_candidates()
            if rows:
                import notif_settings
                thr = int(db.get_wallet_settings().get('low_balance') or 0)
                if thr > 0 and notif_settings.enabled('wallet_low'):
                    seen = set()
                    for uid, old in rows:
                        if uid in seen:
                            continue
                        seen.add(uid)
                        user = db.get_user_by_id(uid)
                        if not user or user.get('is_blocked'):
                            continue
                        _now = db.get_wallet_fresh(uid)
                        if old >= thr > _now:
                            await _notify_wallet_low(bot, {**user, 'wallet': _now})
        except Exception:
            logger.exception('خطا در حلقه‌ی هشدار کم‌شدن موجودی کیف پول')
        await asyncio.sleep(PAYG_RESUME_POLL_SECONDS)


async def payg_maintenance_loop(bot: Bot):
    """بررسی مستقل و سریع PAYG (قبلاً داخل حلقه‌ی ۳۰ دقیقه‌ای هشدارها بود و قطع/وصل/کسر پول تا نیم ساعت دیر می‌شد)."""
    await asyncio.sleep(15)
    while True:
        try:
            await run_payg_maintenance(bot)
        except Exception:
            logger.exception("خطا در بررسی دوره‌ای سرویس‌های PAYG")
        await asyncio.sleep(max(15, PAYG_CHECK_INTERVAL_SECONDS))


PAYG_BILLING_UNIT_BYTES = 1024 ** 3


def _payg_charge_amount(delta_bytes: int, price_per_gb: int) -> int:
    delta_bytes = max(0, int(delta_bytes or 0))
    price_per_gb = max(0, int(price_per_gb or 0))
    if delta_bytes <= 0 or price_per_gb <= 0:
        return 0
    return (delta_bytes * price_per_gb + PAYG_BILLING_UNIT_BYTES - 1) // PAYG_BILLING_UNIT_BYTES


async def _notify_payg_wallet_empty(bot: Bot, user: dict, cfg: dict, action: str, custom_message: str):
    import notif_settings
    if not notif_settings.enabled('payg_empty'):
        return
    text = (custom_message or '').strip()
    _key = 'notif_payg_empty_delete' if action == 'delete' else 'notif_payg_empty_suspend'
    try:
        import ui_editor
        from utils import send_notification_sticker as _sns, send_rich_template as _srt
        from aiogram.types import MessageEntity
        await _sns(bot, int(user['telegram_id']), 'notif_payg_empty')
        from keyboards import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
        _kb = _IKM(ui_screen=_key, inline_keyboard=[[_IKB(text='💵 شارژ کیف پول', callback_data='wallet', style='primary')]])
        if text:
            # پیام سفارشی ادمین (از تنظیمات PAYG) اولویت دارد
            await bot.send_message(int(user['telegram_id']), text, reply_markup=_kb)
            return
        if await _srt(bot, int(user['telegram_id']), _key, {}, reply_markup=_kb) is not None:
            return
        _t, _raw = ui_editor.render_template(_key, {}, fallback=ui_editor.SCREENS[_key]['default'])
        _ents = [MessageEntity(type=e['type'], offset=e['offset'], length=e['length'], custom_emoji_id=e.get('custom_emoji_id')) for e in (_raw or [])]
        await bot.send_message(int(user['telegram_id']), _t, entities=_ents or None, reply_markup=_kb)
    except Exception:
        logger.exception('ارسال اعلان اتمام موجودی PAYG ناموفق بود')


async def _notify_payg_resumed(bot: Bot, user: dict):
    import notif_settings
    if not notif_settings.enabled('payg_empty'):
        return
    try:
        from utils import send_notification_sticker as _sns
        await _sns(bot, int(user['telegram_id']), 'notif_payg_empty')
        from utils import send_rich_template as _srt
        if await _srt(bot, int(user['telegram_id']), 'notif_payg_resumed', {}) is None:
            import ui_editor
            from aiogram.types import MessageEntity
            _t, _raw = ui_editor.render_template('notif_payg_resumed', {}, fallback=ui_editor.SCREENS['notif_payg_resumed']['default'])
            _ents = [MessageEntity(type=e['type'], offset=e['offset'], length=e['length'], custom_emoji_id=e.get('custom_emoji_id')) for e in (_raw or [])]
            await bot.send_message(int(user['telegram_id']), _t, entities=_ents or None)
    except Exception:
        logger.exception('ارسال اعلان فعال‌سازی مجدد PAYG ناموفق بود')


async def _enforce_payg_balance_policy(bot: Bot, cfg: dict, user: dict, panel: dict, settings: dict) -> None:
    action = settings.get('empty_action') or 'suspend'
    if action == 'delete':
        ok, msg = await panels.delete_service(panel, cfg['service_id'])
        if ok:
            db.set_config_deleted(cfg['id'], True)
            await _notify_payg_wallet_empty(bot, user, cfg, 'delete', '')
        else:
            logger.warning('PAYG delete failed for cfg=%s: %s', cfg.get('id'), msg)
        return

    if cfg.get('payg_paused'):
        return
    ok, msg = await panels.disable_service(panel, cfg['service_id'])
    if ok:
        db.set_payg_paused(cfg['id'], True)
        await _notify_payg_wallet_empty(bot, user, cfg, 'suspend', '')
    else:
        logger.warning('PAYG suspend failed for cfg=%s: %s', cfg.get('id'), msg)


async def run_payg_maintenance(bot: Bot):
    settings = db.get_payg_settings()
    if not settings.get('enabled'):
        return
    min_balance = int(settings.get('min_balance') or 0)
    configs = db.get_active_payg_configs(limit=500)
    # هر کاربر سریال (کسر کیف پول امن)، کاربرهای مختلف هم‌زمان (سقف ۶ درخواست هم‌زمان به پنل‌ها)
    by_user: dict = {}
    for _c in configs:
        by_user.setdefault(_c['user_id'], []).append(_c)
    _sem = asyncio.Semaphore(6)

    async def _run_user(_cfgs):
        async with _sem:
            await _payg_process_configs(bot, _cfgs, settings, min_balance)

    await asyncio.gather(*[_run_user(v) for v in by_user.values()], return_exceptions=True)


async def _payg_process_configs(bot: Bot, configs: list, settings: dict, min_balance: int):
    for cfg in configs:
        try:
            user = db.get_user_by_id(cfg['user_id'])
            if cfg.get('payg_paused') and user and int(user.get('wallet') or 0) <= 0:
                continue  # هنوز شارژ نشده؛ نیازی به تماس با پنل نیست
            panel = db.get_vpn_panel(cfg['panel_id']) if cfg.get('panel_id') else None
            if not user or not panel or not panel.get('enabled') or not cfg.get('service_id'):
                continue
            ok, snap, msg = await panels.get_service_snapshot(panel, cfg['service_id'])
            if not ok or not isinstance(snap, dict):
                logger.warning('PAYG snapshot failed for cfg=%s: %s', cfg.get('id'), msg)
                continue
            db.sync_config_from_panel(cfg['id'], snap)
            used = int(snap.get('used') or 0)
            last_used = int(cfg.get('payg_last_used_bytes') or 0)
            if used < last_used:
                db.update_payg_usage_snapshot(cfg['id'], used)
                last_used = used

            if cfg.get('payg_paused'):
                # حداقل موجودی فقط برای «ساخت» سرویس لازم است؛ ادامه/از‌سرگیری تا وقتی موجودی صفر نشده مجاز است.
                if int(user.get('wallet') or 0) > 0:
                    await _payg_resume_cfg(bot, cfg, user, panel, used)
                continue

            delta = max(0, used - last_used)
            charge_amount = _payg_charge_amount(delta, int(cfg.get('payg_price_per_gb') or settings.get('price_per_gb') or 0))
            if charge_amount > 0:
                if db.deduct_from_wallet(user['id'], charge_amount, f"مصرف PAYG {cfg.get('service_id') or cfg.get('plan') or cfg.get('id')}"):
                    db.update_payg_usage_snapshot(cfg['id'], used)
                    user = db.get_user_by_id(user['id']) or user
                else:
                    await _enforce_payg_balance_policy(bot, cfg, user, panel, settings)
                    continue

            if int((user.get('wallet') or 0)) <= 0:
                await _enforce_payg_balance_policy(bot, cfg, user, panel, settings)
        except Exception:
            logger.exception('خطا در پردازش PAYG config=%s', cfg.get('id'))


ONLINE_PAYMENT_POLL_SECONDS = 20


async def online_payment_poller(bot: Bot):
    """هر ۲۰ ثانیه اینوویس‌های در انتظار درگاه یونیک‌پی را چک می‌کند و به‌محض
    پرداخت‌شدن، بدون نیاز به این‌که کاربر دکمه‌ی «بررسی کن» را بزند، سفارش را
    خودکار ثبت کرده و به کاربر و ادمین اطلاع می‌دهد. این همان «تایید خودکار»
    درخواست‌شده برای درگاه پرداخت آنلاین است.
    علاوه‌براین، نرخ خطای هر چرخه را می‌سنجد و اگر درگاه قطعی/کند شده باشد
    (بیشتر چک‌ها fail شوند)، یک‌بار (با cooldown) به ادمین هشدار می‌دهد."""
    while True:
        checked = 0
        failed = 0
        try:
            pending = db.get_gateway_work_payments(limit=50)
            for payment in pending:
                if og.is_new_gateway(og.gateway_of(payment)):
                    # درگاه‌های جدید (FrenzyEx/Tronado): استعلام + تحویل/بررسی دستی/پرداخت دیرهنگام
                    try:
                        await settle_gateway_payment(bot, payment)
                    except Exception:
                        logger.exception("خطا در بررسی خودکار پرداخت %s (%s)", payment.get("hash_id"), payment.get("gateway"))
                    continue
                checked += 1
                try:
                    invoice = await uniquepay.check_invoice(payment["hash_id"])
                    if invoice and invoice.get("isPaid") and invoice.get("isVerified"):
                        payment_kind = payment.get("kind")
                        if payment_kind == "custom":
                            result = await finalize_custom_online_payment(bot, payment)
                        elif payment_kind == "wallet_charge":
                            result = await finalize_wallet_charge_online_payment(bot, payment)
                        else:
                            result = await finalize_online_payment(bot, payment)
                        if result is None:
                            # یک فراخوانی هم‌زمان دیگر (مثلاً مینی‌اپ) همین الان
                            # در حال پردازش این پرداخت است؛ برای جلوگیری از پیام
                            # تکراری به کاربر، این چرخه کاری انجام نمی‌دهد.
                            continue
                except Exception:
                    failed += 1
                    logger.exception("خطا در بررسی خودکار اینوویس %s", payment.get("hash_id"))
            if checked:
                await alerts.report_uniquepay_check_cycle(bot, ADMIN_ID, checked, failed)
        except Exception:
            logger.exception("خطا در حلقه‌ی پولر پرداخت آنلاین")
        await asyncio.sleep(ONLINE_PAYMENT_POLL_SECONDS)


INVOICE_EXPIRY_POLL_SECONDS = 60


async def invoice_expiry_loop(bot: Bot):
    """هر ۶۰ ثانیه فاکتورهای کارت‌به‌کارت (پلن/بساز-سرویس/کیف‌پول) و پرداخت‌های آنلاین پرداخت‌نشده که مهلت ۳۰ دقیقه‌ایشان تمام شده را حذف می‌کند
    و به کاربر پیامی می‌فرستد که برای دوباره سفارش ثبت کند."""
    while True:
        try:
            expired_invoices = db.expire_due_invoices()
            for inv in expired_invoices:
                try:
                    await bot.send_message(
                        int(inv["telegram_id"]),
                        f"⏰ مهلت ۳۰ دقیقه‌ای پرداخت فاکتور تان برای «{inv['label']}» به پایان رسید و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید.\n\n🧾 کد پیگیری: `{inv['id']}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("ارسال پیام انقضای فاکتور به کاربر ناموفق بود")
        except Exception:
            logger.exception("خطا در حلقه‌ی انقضای فاکتورها")
        try:
            expired_online = db.expire_due_online_payments()
            for pay in expired_online:
                try:
                    new_gw = og.is_new_gateway(og.gateway_of(pay))
                    if new_gw:
                        await og.cancel_payment(pay)  # best-effort (FrenzyEx)
                    await bot.send_message(
                        int(pay["telegram_id"]),
                        f"⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید.\n\n🧾 کد پیگیری: `{pay['id']}`"
                        + ("\n\nℹ️ اگر همزمان پرداخت کرده‌اید، پس از تأیید درگاه سفارش شما خودکار تحویل داده می‌شود." if new_gw else ""),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("ارسال پیام انقضای پرداخت آنلاین به کاربر ناموفق بود")
        except Exception:
            logger.exception("خطا در حلقه‌ی انقضای پرداخت‌های آنلاین")
        await asyncio.sleep(INVOICE_EXPIRY_POLL_SECONDS)


SELF_PING_INTERVAL_SECONDS = 120  # ۲ دقیقه؛ زیر آستانهٔ خواب ۱۵ دقیقه‌ای Render رایگان


async def self_ping_loop():
    """هر ۲ دقیقه یک درخواست GET به آدرس خودش می‌زند تا در پلن رایگان Render
    (که بعد از ۱۵ دقیقه بی‌فعالیتی سرویس را می‌خواباند) ربات/مینی‌اپ همیشه بیدار بماند.
    آدرس را یا از SELF_PING_URL (اگر دستی ست شده باشد) یا از RENDER_EXTERNAL_URL
    (که خود Render به‌صورت خودکار در Web Serviceها ست می‌کند) می‌خواند.
    اگر هیچکدام تنظیم نشده باشد (مثلاً اجرای محلی/غیرRender)، حلقه بی‌ضرر غیرفعال می‌ماند."""
    ping_url = os.environ.get("SELF_PING_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not ping_url:
        logger.warning("SELF_PING_URL/RENDER_EXTERNAL_URL تنظیم نشده؛ حلقهٔ self-ping غیرفعال ماند.")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
            try:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    logger.info("self-ping انجام شد (وضعیت %s).", resp.status)
            except Exception:
                logger.exception("self-ping ناموفق بود؛ در چرخهٔ بعدی دوباره تلاش می‌شود.")


def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask keep-alive server started.")

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
