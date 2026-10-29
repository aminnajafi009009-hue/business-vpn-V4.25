"""
handlers/start.py
دستور /start، بررسی عضویت اجباری در کانال‌ها، و پردازش لینک دعوت اختصاصی
(/start BVPNXXXXX).

نکته: منطق بررسی عضویت کانال‌ها (check_membership) دست‌نخورده باقی مانده.
"""

import asyncio
import logging

from aiogram import Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatMemberStatus

import database as db
import bot_info
import notif_settings
import panels
from utils import show_menu_with_sticker
from keyboards import (
    join_channels_keyboard,
    main_reply_keyboard,
    admin_reply_keyboard,
)
from config import ADMIN_ID


def _persian_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

router = Router(name="start")
logger = logging.getLogger(__name__)


async def _delete_saved_start_menu(bot, chat_id: int, state: FSMContext):
    """پاک‌کردن پیام قبلی مسیر /start تا خوش‌آمدگویی تکراری باقی نماند."""
    data = await state.get_data()
    ids = []
    for key in ("start_menu_message_id", "start_join_message_id"):
        value = data.get(key)
        if value:
            try:
                mid = int(value)
                if mid not in ids:
                    ids.append(mid)
            except (TypeError, ValueError):
                pass
    for message_id in ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    if ids:
        await state.update_data(start_menu_message_id=None, start_join_message_id=None)


async def check_membership(bot, user_id: int) -> list:
    not_joined = []
    for ch in bot_info.get_required_channels():
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                not_joined.append(ch)
        except Exception as e:
            logger.error(f"check_membership failed for channel {ch['id']}: {e}")
            not_joined.append(ch)
    return not_joined


# ---------------------------------------------------------------------------
# 🚪 لفت‌دادن/برگشت به کانال‌های اجباری
# تلگرام برای هر تغییر وضعیت عضویت یک کاربر در چتی که ربات ادمینشه، یک
# آپدیت chat_member می‌فرستد. اینجا فقط کانال‌هایی که در «اطلاعات ربات ←
# کانال‌های اجباری» ثبت شده‌اند رصد می‌شوند (نه هر چت دیگری که ربات توش ادمینه).
# ---------------------------------------------------------------------------
_JOINED_STATUSES = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
_LEFT_STATUSES = (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)


@router.chat_member()
async def on_channel_membership_change(update: types.ChatMemberUpdated):
    channel_ids = {int(ch["id"]) for ch in bot_info.get_required_channels()}
    if update.chat.id not in channel_ids:
        return  # کانالی که تحت نظر نیست (چت دیگری که ربات توش ادمینه)

    target_user = update.new_chat_member.user
    if target_user.is_bot:
        return  # از جمله خود ربات

    user = db.get_user(target_user.id)
    if user is None:
        return  # کسی که اصلاً با ربات تعامل نکرده لازم نیست دنبالش کنیم

    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status

    if old_status in _JOINED_STATUSES and new_status in _LEFT_STATUSES:
        await _on_user_left_channel(update.bot, target_user.id)
    elif old_status in _LEFT_STATUSES and new_status in _JOINED_STATUSES:
        await _on_user_rejoined_channel(update.bot, target_user.id)


async def _on_user_left_channel(bot, telegram_id: int):
    channels = bot_info.get_required_channels()
    text = (
        "⚠️ شما از یکی از کانال‌های اجباری ربات خارج شدید.\n\n"
        "برای اینکه بتونید همچنان از خدمات ربات استفاده کنید، لطفاً دوباره عضو کانال(های) زیر بشید:"
    )
    try:
        notif_settings.gate("left_channel")
        await bot.send_message(telegram_id, text, reply_markup=join_channels_keyboard(channels))
    except notif_settings.Skip:
        pass
    except Exception:
        logger.warning("ارسال پیام لفت‌دادن کانال به کاربر %s ناموفق بود (احتمالاً ربات را بلاک کرده).", telegram_id)

    # همه‌ی پیامدهای این لفت (قفل پاداش + جریمه) در «یک» پیام به معرف و یک گزارش به تاپیک دعوت‌ها می‌رود.
    settings = db.get_referral_settings()
    invited_user = db.get_user(telegram_id)
    referrer = db.get_user_by_id(invited_user["referrer_id"]) if invited_user and invited_user.get("referrer_id") else None
    if not referrer or not db.referral_enabled():
        return
    lines: list[str] = []
    lock_ref = None

    # ۱) قفل موقت پاداش (relock) — فقط اگر پاداش آزادشده هنوز در کیف پول رفرالی معرف بود و واقعاً قفل شد.
    if settings.get("channel_leave_lock_enabled"):
        lock_ref = db.relock_referral_reward_for_invited(telegram_id)
        if lock_ref:
            lines.append(_line("notif_referral_line_lock", amount=f"{int(lock_ref['locked_amount']):,}",
                               default="⏳ {amount} تومان پاداش دعوتش موقتاً از کیف پول قابل‌برداشت شما به کیف پول مسدود برگشت؛ "
                                       "به‌محض این‌که دوباره عضو کانال‌ها شود خودکار آزاد می‌شود."))

    # ۲) سیستم جریمه — مستقل از قفل بالا.
    penalty_lines, penalty_detail = await _apply_channel_leave_penalty(bot, telegram_id, settings, lock_ref)
    lines.extend(penalty_lines)

    await _send_referral_notice(bot, referrer, invited_user, "left", lines, penalty_detail)


def _line(key: str, default: str, **vars_) -> str:
    """یک خط پیام رفرال از «ویرایش متن» (در نبود ویرایش، متن پیش‌فرض)؛ متغیرها {name} جایگزین می‌شوند."""
    import re as _re
    try:
        import ui_editor
        text = ui_editor.get_text(key, default) or default
    except Exception:
        text = default
    return _re.sub(r"\{([A-Za-z_]+)\}", lambda m: str(vars_.get(m.group(1), m.group(0))), text)


async def _apply_channel_leave_penalty(bot, telegram_id: int, settings: dict, lock_ref: dict | None = None) -> tuple[list[str], str]:
    """جریمه را اعمال می‌کند و (خطوط توضیح برای معرف، توضیح فنی برای گزارش ادمین) برمی‌گرداند.
    ترتیب: ۰) اگر همین لفت پاداشی را مسدود کرده، مبلغ جریمه از «همان پاداش مسدودشده» کم می‌شود (و با بازگشت دعوت‌شده
    فقط باقی‌مانده آزاد می‌شود) ۱) کیف پول رفرالی (اگر کافی بود) ۲) سرویس رفرالی (اگر کیف پول رفرالی کافی نبود).
    پول عادی هرگز کم نمی‌شود."""
    if not settings.get("channel_leave_penalty_enabled"):
        return [], "جریمه خاموش است"
    amount = int(settings.get("penalty_wallet_amount") or 0)
    if lock_ref and amount > 0:
        r = db.penalize_locked_reward(lock_ref, amount)
        if r["deducted"] > 0:
            return [_line("notif_referral_line_lockpen", penalty=f"{r['deducted']:,}", remaining=f"{r['remaining']:,}",
                          default="💸 {penalty} تومان جریمه از همان پاداش مسدودشده کم شد؛ با بازگشت او به کانال‌ها {remaining} تومان برایتان آزاد می‌شود.")], \
                   f"کسر {r['deducted']:,} تومان از پاداش مسدودشده (باقی‌مانده برای آزادسازی: {r['remaining']:,})"
    result = db.apply_referral_wallet_penalty(telegram_id)
    if not result:
        return [], "جریمه‌ای اعمال نشد"
    amount = int(result.get("requested") or 0)
    gb_set = int(result.get("gb_setting") or 0)
    lines: list[str] = []
    mode = result.get("mode")
    if mode == "wallet":
        lines.append(_line("notif_referral_line_wallet", amount=f"{int(result['deducted']):,}", default="💸 {amount} تومان از کیف پول رفرالی شما کسر شد."))
        return lines, f"کسر {result['deducted']:,} تومان از کیف پول رفرالی"
    if mode == "service":
        cfg = db.get_config_by_id(result.get("service_config_id")) if result.get("service_config_id") else None
        panel = db.get_vpn_panel(cfg.get("panel_id")) if cfg and cfg.get("panel_id") else None
        gb_penalty = result.get("service_gb_penalty") or 0
        if cfg and panel and cfg.get("service_id"):
            ok, msg = await panels.reduce_service_quota(panel, cfg["service_id"], gb_penalty)
            if ok:
                if amount > 0:
                    lines.append(_line("notif_referral_line_service_after_wallet", gb=gb_penalty, service=cfg.get("service_id"),
                                       default="⚠️ موجودی کیف پول رفرالی شما برای جریمه کافی نبود؛ به‌جای آن\n📉 {gb} گیگابایت از حجم سرویس رفرالی شما ({service}) کم شد."))
                else:
                    lines.append(_line("notif_referral_line_service", gb=gb_penalty, service=cfg.get("service_id"),
                                       default="📉 {gb} گیگابایت از حجم سرویس رفرالی شما ({service}) کم شد."))
                return lines, f"کسر {gb_penalty} گیگ از سرویس رفرالی {cfg.get('service_id')}"
            logger.warning("اعمال جریمه‌ی حجمی رفرال روی سرویس %s ناموفق بود: %s", cfg.get("service_id"), msg)
            return [], f"کسر حجم از سرویس {cfg.get('service_id')} ناموفق بود: {msg}"
        logger.warning("سرویس رفرالی هدف جریمه پیدا شد ولی اطلاعات پنل/سرویس ناقص است.")
        return [], "سرویس رفرالی بدون اطلاعات پنل/سرویس کامل"
    # هیچ‌کدام ممکن نبود: چیزی از معرف کم نشده، پس پیام اضافه به او نمی‌رود (علت فقط در گزارش ادمین می‌آید)
    return lines, (f"موجودی رفرالی {int(result.get('referral_available') or 0):,} < جریمه {amount:,} و سرویس رفرالی فعال پیدا نشد"
                   + ("" if gb_set > 0 else " (جریمه‌ی حجمی روی ۰ است)"))


async def _send_referral_notice(bot, referrer: dict, invited: dict | None, event: str, lines: list[str], detail: str = "") -> None:
    """یک پیام یکپارچه به معرف (قالب قابل‌ویرایش) + گزارش به تاپیک «دعوت‌ها» (حتی اگر پیامدی برای معرف نبود)."""
    if lines:
        key = "notif_referral_left" if event == "left" else "notif_referral_rejoined"
        default = ui_editor_default(key)
        try:
            notif_settings.gate("referral_penalty" if event == "left" else "referral_reward")
            await _send_template(bot, int(referrer["telegram_id"]), key, {
                "name": (invited or {}).get("name") or "—", "user_id": (invited or {}).get("telegram_id") or "—",
                "details": "\n".join(lines)}, default)
        except notif_settings.Skip:
            pass
        except Exception:
            logger.warning("ارسال اعلان رفرال به معرف %s ناموفق بود", referrer.get("telegram_id"))
    try:
        import adv_reports
        adv_reports.referral_event(referrer["id"], (invited or {}).get("id"), event,
                                   detail="\n".join(lines) if lines else (detail or "پیامدی برای معرف نداشت"),
                                   technical=detail)
    except Exception:
        logger.debug("گزارش رفرال به گروه اعلان‌ها ارسال نشد", exc_info=True)


def ui_editor_default(key: str) -> str:
    import ui_editor
    return ui_editor.SCREENS[key]["default"]


async def _send_template(bot, chat_id: int, key: str, values: dict, default: str, parse_mode=None) -> None:
    """ارسال پیام از «ویرایش متن» با حفظ Entity/ایموجی پریمیوم + استیکر اعلان دعوت."""
    import ui_editor
    from aiogram.types import MessageEntity
    from utils import send_notification_sticker as _sns
    text, raw = ui_editor.render_template(key, values, fallback=default)
    ents = [MessageEntity(type=e["type"], offset=e["offset"], length=e["length"], custom_emoji_id=e.get("custom_emoji_id")) for e in (raw or [])]
    await _sns(bot, int(chat_id), "notif_referral_success")
    from utils import send_rich_template as _srt
    if await _srt(bot, int(chat_id), key, values) is None:
        await bot.send_message(int(chat_id), text, entities=ents or None)


async def _on_user_rejoined_channel(bot, telegram_id: int):
    still_missing = await check_membership(bot, telegram_id)
    if still_missing:
        return  # هنوز همه‌ی کانال‌های اجباری را کامل نکرده؛ صبر می‌کنیم تا بقیه را هم جوین بده

    settings = db.get_referral_settings()
    if not settings.get("channel_leave_lock_enabled"):
        return
    ref = db.rerelease_referral_reward_for_invited(telegram_id)
    if not ref:
        return  # قبلاً چیزی قفل نشده بود (مثلاً پاداش خرج شده بود)؛ پس چیزی هم آزاد نمی‌شود و اعلانی لازم نیست
    referrer = db.get_user_by_id(ref["referrer_id"])
    if not referrer:
        return
    pen = int(ref.get("penalty_total") or 0)
    note = f"\n(💸 {pen:,} تومان بابت جریمه‌ی خروج از کانال از این پاداش کسر شده بود.)" if pen > 0 else ""
    await _send_referral_notice(
        bot, referrer, db.get_user(telegram_id), "rejoined",
        [_line("notif_referral_line_release", amount=f"{int(ref['released']):,}", penalty=f"{pen:,}", penalty_note=note,
               default="🔓 {amount} تومان پاداش دعوتش دوباره در کیف پول قابل‌برداشت شما آزاد شد.{penalty_note}")],
    )


def _ensure_user(telegram_id, full_name: str, referrer_code: str | None = None, campaign_code: str | None = None):
    """کاربر را اگر وجود نداشت می‌سازد؛ کد دعوت معتبر یا کد کمپین تبلیغاتی
    را هم پاس می‌دهد (این دو مستقل از هم‌اند، ولی چون هر دو از همان یک پارامتر
    /start می‌آیند، معمولاً فقط یکی‌شان در هر لحظه پر است).
    این تابع فقط باید بعد از تأیید عضویت کاربر در کانال‌های اجباری صدا زده شود،
    چون همین‌جا رکورد دعوت ساخته و ۴۰,۰۰۰ تومان در کیف پول مسدود معرف قفل می‌شود."""
    return db.create_user(telegram_id, full_name, referrer_invite_code=referrer_code, campaign_code=campaign_code)


def _parse_start_param(raw: str | None) -> tuple[str | None, str | None]:
    """پارامتر /start را به (referrer_code, campaign_code) تفکیک می‌کند.
    لینک‌های تبلیغاتی همیشه با پیشوند c_ ساخته می‌شوند (مثلاً /start c_INSTA1)
    تا هرگز با کد دعوت معمولی (که همیشه با BVPN شروع می‌شود) قاطی نشوند."""
    if not raw:
        return None, None
    raw = raw.strip()
    if raw.lower().startswith("c_"):
        return None, raw[2:]
    return raw, None


async def _notify_referrer_of_new_join(bot, user: dict):
    """
    وقتی عضویت یک کاربر تازه (که از لینک دعوت وارد شده) در کانال‌ها تأیید می‌شود،
    یک پیام حاوی آیدی و نام او برای معرفش ارسال می‌شود تا بداند چه کسی از طریق
    لینک او وارد ربات شده است.
    """
    if not user or not user.get("referrer_id") or not db.referral_enabled():
        return

    referrer = db.get_user_by_id(user["referrer_id"])
    if referrer is None:
        return

    try:
        settings = db.get_referral_settings()
        if settings["paid_purchase_required"]:
            if settings["min_volume_enabled"]:
                reward_text = (
                    f"پس از اینکه این کاربر یک خرید "
                    f"{settings['min_volume_gb']} گیگ یا بیشتر انجام دهد، "
                    f"{settings['reward_amount']:,} تومان به‌صورت خودکار "
                    f"به کیف پول شما آزاد می‌شود."
                )
            else:
                reward_text = (
                    "پس از اینکه این کاربر هر خرید پولی انجام دهد، "
                    f"{settings['reward_amount']:,} تومان به‌صورت خودکار "
                    "به کیف پول شما آزاد می‌شود."
                )
        else:
            reward_text = (
                f"{settings['reward_amount']:,} تومان به‌صورت فوری و بدون نیاز "
                "به خرید به کیف پول شما آزاد می‌شود."
            )

        notif_settings.gate("referral_reward")
        await _send_template(bot, int(referrer["telegram_id"]), "notif_referral_join",
                             {"name": user["name"], "user_id": user["telegram_id"], "reward_text": reward_text},
                             ui_editor_default("notif_referral_join"))
    except notif_settings.Skip:
        pass
    except Exception as e:
        logger.error(f"failed to notify referrer {referrer['telegram_id']}: {e}")


def _welcome_text(first_name: str) -> str:
    return bot_info.get_welcome_text(first_name)


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_sub_admin(str(user_id))


async def _react_to_start(message: types.Message):
    """ری‌اکشن تنظیم‌شده‌ی ادمین (ایموجی معمولی/پریمیوم) را روی پیام /start مشتری می‌گذارد؛ هر خطایی بی‌صدا نادیده گرفته می‌شود."""
    try:
        r = bot_info.get_start_reaction()
        if not r:
            return
        from aiogram.types import ReactionTypeEmoji, ReactionTypeCustomEmoji
        rt = ReactionTypeEmoji(emoji=r["value"]) if r["type"] == "emoji" else ReactionTypeCustomEmoji(custom_emoji_id=r["value"])
        await message.bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=[rt])
    except Exception as exc:
        logger.info("ری‌اکشن /start ثبت نشد: %s", type(exc).__name__)


@router.message(Command("start"))
async def start(message: types.Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    if not _is_admin(user_id):
        asyncio.create_task(_react_to_start(message))
    # 🛡 لینک «مدیریت کاربر/سرویس» از گزارشات پیشرفته (فقط ادمین): /start adm_<tid>[_<cfgid>]
    _arg = (command.args or "").strip()
    if _arg.startswith("adm_") and (user_id == ADMIN_ID or db.is_sub_admin(str(user_id))):
        _parts = _arg.split("_")
        _tid = _parts[1] if len(_parts) > 1 else ""
        _rows = []
        if _tid:
            _rows.append([types.InlineKeyboardButton(text="👤 مدیریت کاربر", callback_data=f"useropen_{_tid}", style="primary")])
        if len(_parts) > 2 and _parts[2].isdigit():
            _rows.append([types.InlineKeyboardButton(text="📦 مدیریت سرویس", callback_data=f"svcdetail_{_parts[2]}", style="success")])
        if _rows:
            await message.answer(f"🛡 مدیریت سریع (کاربر {_tid})", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=_rows))
            return
    # 🔗 لینک عمیق دکمه‌ی دلخواهِ لاگ کانال: /start cb_<id>
    _cb_btn = None
    if _arg.startswith("cb_") and _arg[3:].isdigit():
        try:
            import ui_editor as _uie
            _c = _uie.get_custom_button(int(_arg[3:]))
            if _c and _c.get("action_type") == "callback" and _c.get("action_value"):
                _cb_btn = _c
        except Exception:
            logger.exception("خواندن دکمه‌ی دلخواه لینک عمیق ناموفق بود")
    not_joined = await check_membership(message.bot, user_id)

    referrer_code, campaign_code = _parse_start_param(None if _cb_btn else command.args)
    # کد دعوت/کمپین را تا زمان تأیید عضویت کاربر در کانال‌ها نگه می‌داریم تا رسماً
    # ثبت نشود و پاداش معرف زودتر از موعد قفل نشود.
    if referrer_code:
        await state.update_data(pending_referrer_code=referrer_code)
    if campaign_code:
        await state.update_data(pending_campaign_code=campaign_code)

    if not_joined:
        if _cb_btn is not None:
            await state.update_data(pending_cb=str(_cb_btn["action_value"]))
        await _delete_saved_start_menu(message.bot, message.chat.id, state)
        # 🐛 فیکس: قبلاً اینجا فقط یک پیام متنی بدون استیکر فرستاده می‌شد، برای
        # همین وقتی که کاربر برای اولین بار /start می‌زد و هنوز عضو کانال‌ها نشده، استیکر
        # «شروع با /start» اصلاً دیده نمی‌شد (فقط بعد از تأیید عضویت در check_join). حالا
        # همین استیکر درست بالای لیست کانال‌های اجباری هم نشان داده می‌شود.
        # show_main_keyboard=False عمداً پاس داده شده چون عضویت کاربر هنوز تأیید نشده و نباید منوی
        # دائمی پایین صفحه زودتر از موعد فعال شود.
        # ⚠️ عضویت اجباری نباید از کلید «start_welcome» استفاده کند؛ چون
        # show_menu_with_sticker از sticker_key به‌عنوان ui_key هم استفاده می‌کند
        # و در نتیجه متن خوش‌آمدگوییِ سفارشی‌شده روی پیام عضویت اجباری اعمال می‌شد.
        # این صفحه باید کلید مستقل خودش را داشته باشد تا متنش جداگانه قابل ویرایش باشد.
        join_msg = await show_menu_with_sticker(
            message.bot, message.chat.id, "start_join_required",
            "⚠️ برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید:",
            reply_markup=join_channels_keyboard(not_joined),
            show_main_keyboard=False,
            ui_key="start_join_required",
        )
        if join_msg and getattr(join_msg, "message_id", None):
            await state.update_data(start_join_message_id=join_msg.message_id)
        return

    if db.is_user_blocked(user_id):
        await message.answer("🚫 دسترسی شما به ربات مسدود شده است. در صورت وجود ابهام با پشتیبانی در ارتباط باشید.")
        return

    data = await state.get_data()
    referrer_code = referrer_code or data.get("pending_referrer_code")
    campaign_code = campaign_code or data.get("pending_campaign_code")
    existed_before = db.get_user(user_id) is not None
    user = _ensure_user(user_id, message.from_user.full_name, referrer_code, campaign_code)
    await state.update_data(pending_referrer_code=None, pending_campaign_code=None)

    if not existed_before:
        await _notify_referrer_of_new_join(message.bot, user)

    if _is_admin(user_id):
        await message.answer(
            "👨‍💻 به پنل مدیریت خوش آمدید!\n\nهمه‌ی امکانات مدیریتی از منوی پایین صفحه قابل دسترسی است ✅",
            reply_markup=admin_reply_keyboard(permissions=(None if user_id == ADMIN_ID else set((db.get_sub_admin(str(user_id)) or {}).get("permissions") or [])), is_main_admin=(user_id == ADMIN_ID)),
        )
        return

    if _cb_btn is not None:
        # دکمه‌ی دلخواه کانال: مستقیم همان بخش باز می‌شود. کاربر جدید پیام خوش‌آمد را هم می‌بیند.
        import cbdispatch
        _opened = False
        try:
            _opened = await cbdispatch.open_callback(message.bot, message.from_user, message.chat.id, str(_cb_btn["action_value"]))
        except Exception:
            logger.exception("باز کردن مستقیم دکمه‌ی لینک عمیق ناموفق بود")
        if _opened and existed_before:
            return

    await _delete_saved_start_menu(message.bot, message.chat.id, state)
    welcome_msg = await show_menu_with_sticker(
        message.bot, message.chat.id, "start_welcome",
        _welcome_text(message.from_user.first_name), reply_markup=main_reply_keyboard(message.from_user.id),
        template_values={"first_name": message.from_user.first_name},
        ui_key="start_welcome",
    )
    if welcome_msg and getattr(welcome_msg, "message_id", None):
        await state.update_data(start_menu_message_id=welcome_msg.message_id)


@router.callback_query(F.data == "check_join")
async def check_join(callback: types.CallbackQuery, state: FSMContext):
    not_joined = await check_membership(callback.bot, callback.from_user.id)
    if not_joined:
        import ui_editor
        await callback.answer(ui_editor.get_text("join_not_completed", "❌ هنوز در همه کانال‌ها عضو نشدید!"), show_alert=True)
        return

    if db.is_user_blocked(callback.from_user.id):
        import ui_editor
        await callback.message.edit_text(ui_editor.get_text("user_blocked", "🚫 دسترسی شما به ربات مسدود شده است."))
        await callback.answer()
        return

    # فقط همین‌جا (بعد از تأیید واقعی عضویت) کاربر رسماً ثبت و پاداش معرف قفل می‌شود.
    data = await state.get_data()
    referrer_code = data.get("pending_referrer_code")
    campaign_code = data.get("pending_campaign_code")
    existed_before = db.get_user(callback.from_user.id) is not None
    user = _ensure_user(callback.from_user.id, callback.from_user.full_name, referrer_code, campaign_code)
    await state.update_data(pending_referrer_code=None, pending_campaign_code=None)

    if not existed_before:
        await _notify_referrer_of_new_join(callback.bot, user)

    if _is_admin(callback.from_user.id):
        await callback.message.edit_text("👨‍💻 به پنل مدیریت خوش آمدید! همه‌ی امکانات مدیریتی از منوی پایین صفحه قابل دسترسی است ✅")
        await callback.message.answer("منوی مدیریتی فعال شد:", reply_markup=admin_reply_keyboard(permissions=(None if callback.from_user.id == ADMIN_ID else set((db.get_sub_admin(str(callback.from_user.id)) or {}).get("permissions") or [])), is_main_admin=(callback.from_user.id == ADMIN_ID)))
    else:
        # پیام خوش‌آمد قبلی که ممکن است از یک /start قبلی باقی مانده باشد، حذف می‌شود.
        saved = await state.get_data()
        previous_welcome_id = saved.get("start_menu_message_id")
        try:
            previous_welcome_id = int(previous_welcome_id) if previous_welcome_id else None
        except (TypeError, ValueError):
            previous_welcome_id = None
        if previous_welcome_id and previous_welcome_id != callback.message.message_id:
            try:
                await callback.bot.delete_message(callback.message.chat.id, previous_welcome_id)
            except Exception:
                pass

        # خود پیام عضویت اجباری/دکمه «عضو شدم» را حذف می‌کنیم؛ سپس فقط یک بار
        # استیکر + پیام خوش‌آمدگویی نهایی ارسال می‌شود.
        try:
            await callback.message.delete()
        except Exception:
            pass
        await state.update_data(start_menu_message_id=None, start_join_message_id=None)

        welcome_msg = await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "start_welcome",
            _welcome_text(callback.from_user.first_name),
            reply_markup=main_reply_keyboard(callback.from_user.id),
            template_values={"first_name": callback.from_user.first_name},
            ui_key="start_welcome",
        )
        if welcome_msg and getattr(welcome_msg, "message_id", None):
            await state.update_data(start_menu_message_id=welcome_msg.message_id)
        # دکمه‌ی دلخواهِ کانال که قبل از تأیید عضویت زده شده بود، حالا مستقیم باز می‌شود
        _pcb = (await state.get_data()).get("pending_cb")
        if _pcb:
            await state.update_data(pending_cb=None)
            try:
                import cbdispatch
                await cbdispatch.open_callback(callback.bot, callback.from_user, callback.message.chat.id, str(_pcb))
            except Exception:
                logger.exception("باز کردن دکمه‌ی دلخواه بعد از تأیید عضویت ناموفق بود")
    await callback.answer()


@router.callback_query(F.data == "back")
async def go_back(callback: types.CallbackQuery):
    """بازگشت از زیرمنوهای اینلاین؛ دیگر منوی اصلی اینلاین دوباره ارسال نمی‌شود؛
    تمام مسیرها از طریق همین منوی دائمی پایین صفحه در دسترس است.

    🐛 فیکس: قبلاً اینجا فقط متن پیام فعلی ویرایش می‌شد، پس اگر بالای همان منو یک
    استیکر وجود داشت، روی صفحه باقی می‌ماند. حالا از show_menu_with_sticker استفاده
    می‌شود تا همزمان با بستن منو، استیکرش هم حذف شود و منوی دائمی پایین صفحه
    هم دوباره تازه/فعال شود."""
    if _is_admin(callback.from_user.id):
        await callback.message.edit_text("👨‍💻 بازگشت به منوی اصلی — از منوی پایین صفحه ادامه دهید ✅")
    else:
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, None,
            "👋 بازگشت به منوی اصلی — از منوی پایین صفحه ادامه دهید ✅",
            reply_markup=main_reply_keyboard(callback.from_user.id),
        )
    await callback.answer()


