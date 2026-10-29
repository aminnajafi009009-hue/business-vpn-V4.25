"""
handlers/menu.py
هندلرهای منوی پایین صفحه (Reply Keyboard) که همیشه در دسترس کاربر/ادمین است.

این روتر باید قبل از همه‌ی روترهای دیگر در bot.py ثبت شود تا دکمه‌های این
منو در هر حالتی (حتی وسط یک مکالمه‌ی FSM مثل ارسال رسید یا تیکت) همیشه کار
کنند و کاربر هیچ‌وقت در یک مرحله گیر نکند.
"""

import ui_editor


from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile

import database as db
import crypto
import bot_info
from subscription import is_config_expired
from utils import show_menu_with_sticker
from states import UserStates, AdminStates
from config import ADMIN_ID, PLANS_INTRO_TEXT, DATABASE_PATH
from keyboards import (
    plans_menu,
    plans_direct_to_vip,
    vip_categories_keyboard,
    my_configs_menu,
    my_configs_list_keyboard,
    wallet_menu,
    profile_menu,
    referral_menu,
    support_menu,
    back_button,
    admin_panel_menu,
    admin_back_button,
    admin_discount_menu,
    admin_userlist_menu,
    purchase_payment_keyboard,
    free_test_confirm_keyboard,
    main_reply_keyboard,
    renew_services_keyboard,
    admin_reply_keyboard,
    admin_referrers_page_keyboard,
    user_guides_menu,
    user_guide_detail_keyboard,
    admin_guides_menu,
    admin_broadcast_audience_keyboard,
)

router = Router(name="menu")

# fix: وقتی ادمین داخل «ویرایش متن و دکمه‌ها» منتظر دریافت متن جدید است، متن ارسالی او
# (مثلاً «🎁 تست رایگان» با ایموجی پریمیوم که متن ساده‌اش با متن فعلی یکی است) نباید توسط
# هندلرهای منوی پایین کاربر بلعیده شود؛ باید به admin.router برسد.
from aiogram.filters import StateFilter
router.message.filter(~StateFilter(AdminStates.waiting_ui_editor_input))


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_sub_admin(str(user_id))


def _has_admin_permission(user_id: int, permission: str) -> bool:
    if user_id == ADMIN_ID:
        return True
    if not db.is_sub_admin(str(user_id)):
        return False
    return db.sub_admin_has_permission(str(user_id), permission)


# ---------------------------------------------------------------------------
# منوی کاربر عادی
# ---------------------------------------------------------------------------
@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "plans", "🛒 خرید اشتراک"))
async def menu_plans(message: types.Message, state: FSMContext):
    await state.clear()
    if not db.is_orders_enabled():
        await message.answer(
            ui_editor.get_text("msg_a351fcf2fa", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.")
        )
        return
    if plans_direct_to_vip():
        # سفارشی و PAYG خاموشند → مستقیم پوشه‌ها و پلن‌ها (بدون دکمه‌ی «سرور VIP»)
        await show_menu_with_sticker(
            message.bot, message.chat.id, "buy_plans",
            PLANS_INTRO_TEXT, reply_markup=vip_categories_keyboard(), ui_key=ui_editor.plans_intro_key(),
        )
        return
    # 🧪 تست: استیکر service.webm درست بالای منوی خرید اشتراک
    await show_menu_with_sticker(
        message.bot, message.chat.id, "buy_plans",
        PLANS_INTRO_TEXT, reply_markup=plans_menu(), ui_key=ui_editor.plans_intro_key(),
    )
    
    
def _free_test_options() -> list:
    """گزینه‌های تست رایگان قابل نمایش: اگر ادمین پلن‌های تست تعریف کرده باشد همان‌ها
    (فقط فعال‌ها)، وگرنه پلن پیش‌فرض."""
    from config import FREE_TEST_PLAN_KEY
    entries = db.get_enabled_free_test_panels()
    if entries:
        return [(db.free_test_plan_key(e["num"]), db._free_test_entry_to_plan(e)) for e in entries]
    return [(FREE_TEST_PLAN_KEY, db.get_effective_plan(FREE_TEST_PLAN_KEY))]


async def _free_test_shortcut(bot, chat_id: int, user: dict, options: list) -> bool:
    """تحویل جمعی تست‌ها. اگر هیچ تستی ساخته نشد False برمی‌گرداند تا مسیر عادی (فهرست) نشان داده شود."""
    from handlers.panel_admin import auto_fulfill_vip_via_panel
    from aiogram.types import MessageEntity
    collected: list = []
    skipped_used = 0
    for plan_key, plan in options:
        if not plan or int(plan.get("price") or 0) > 0:
            continue  # میانبر فقط برای تست‌های رایگان
        if db.has_used_free_test(user["id"], plan_key):
            skipped_used += 1
            continue
        if not db.claim_purchase_action(f"tg-ftshort:{user['id']}:{plan_key}:{int(__import__('time').time()) // 30}"):
            continue
        order_id = db.create_order(user["id"], plan_key, plan["name"], "test", 0, payment_method="تست رایگان")
        try:
            await auto_fulfill_vip_via_panel(bot, str(user["telegram_id"]), plan_key, order_id, collector=collected)
        except Exception:
            import logging; logging.getLogger(__name__).exception("free test shortcut failed (%s)", plan_key)
    if not collected:
        if skipped_used and skipped_used == len(options):
            await bot.send_message(chat_id, ui_editor.get_text("msg_b5034a8263", "⚠️ شما قبلاً از «تست رایگان» استفاده کرده‌اید. هر کاربر فقط یک‌بار می‌تواند این پلن را دریافت کند."))
            return True
        return False
    lines = []
    for i, c in enumerate(collected, 1):
        lines.append(f"{i}) {c['name']}\n⏳ {c['days']} | 🗜 {c['volume']}\n{c['link']}")
    fallback = ("🎁 تست‌های رایگان شما آماده شد ✅\n\n" + "\n\n".join(lines) +
                "\n\n🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید.")
    from utils import delivery_time_vars as _dtv
    _tv = _dtv(None)
    text, ents = ui_editor.render_template("free_test_multi", {"tests": "\n\n".join(lines), "count": len(collected),
                                                              "order_time": _tv["delivery_time"], "delivery_time": _tv["delivery_time"]}, fallback=fallback)
    entities = [MessageEntity(type=e["type"], offset=e["offset"], length=e["length"], custom_emoji_id=e.get("custom_emoji_id")) for e in (ents or [])]
    from keyboards import config_delivery_keyboard
    _bi = bot_info
    try:
        from utils import send_rich_template as _srt
        if await _srt(bot, chat_id, "free_test_multi", {"tests": "\n\n".join(lines), "count": len(collected),
                                                       "order_time": _tv["delivery_time"], "delivery_time": _tv["delivery_time"]},
                      reply_markup=config_delivery_keyboard(_bi.get("connection_guide_url"), is_test=True)) is not None:
            return True
        await bot.send_message(chat_id, text, entities=entities or None, parse_mode=None,
                               reply_markup=config_delivery_keyboard(_bi.get("connection_guide_url"), is_test=True),
                               link_preview_options=types.LinkPreviewOptions(is_disabled=True))
    except Exception:
        await bot.send_message(chat_id, text, parse_mode=None)
    return True


async def _show_free_test_menu(bot, chat_id: int, user_tid: int) -> bool:
    options = _free_test_options()
    user = db.get_user(user_tid)
    if user is None:
        return False
    if options and db.get_setting("free_test_shortcut", "0") == "1":
        # ⚡ میانبر: بدون پیام معرفی، همه‌ی تست‌های تعریف‌شده (یک یا چند پلن) یک‌جا در یک پیام (بدون QR) تحویل می‌شود.
        if await _free_test_shortcut(bot, chat_id, user, options):
            return True
    tv = None
    if len(options) == 1:
        plan_key, plan = options[0]
        _ui_key = "free_test_intro"
        tv = {"plan_name": plan["name"]}
        text = (
            f"🎁 {plan['name']}\n\n"
            "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
            "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
            "⚡️ روی دکمه‌ی سبز زیر بزن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
        )
        markup = free_test_confirm_keyboard(plan_key)
    else:
        _ui_key = "free_test_list_intro"
        text = (
            "🎁 تست رایگان\n\n"
            "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
            "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
            "👇 یکی از تست‌های زیر را انتخاب کن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
        )
        from keyboards import free_test_list_keyboard
        markup = free_test_list_keyboard(options)
    await show_menu_with_sticker(bot, chat_id, "free_test", text, reply_markup=markup, ui_key=_ui_key, template_values=tv)
    return True


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "free_test", "🎁 تست رایگان"))
async def menu_free_test(message: types.Message, state: FSMContext):
    if not db.is_orders_enabled():
        await message.answer(ui_editor.get_text("msg_91ed1eceef", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد."))
        return
    if db.get_user(message.from_user.id) is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return
    await state.clear()
    await _show_free_test_menu(message.bot, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "buy_plan_test")
async def menu_free_test_callback(callback: types.CallbackQuery, state: FSMContext):
    if not db.is_orders_enabled():
        await callback.answer(ui_editor.get_alert_text("msg_91ed1eceef", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد."), show_alert=True)
        return
    if db.get_user(callback.from_user.id) is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    await state.clear()
    await _show_free_test_menu(callback.bot, callback.message.chat.id, callback.from_user.id)
    await callback.answer()


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "services", "📱 سرویس‌های من"))
async def menu_my_configs(message: types.Message, state: FSMContext):
    # 🛠 همان فیکس UX سرویس‌های من: دیگر منوی میانیِ تک‌گزینه‌ای نشون نمی‌دیم،
    # مستقیم لیست سرویس‌های VIP کاربر رو نشون می‌دیم.
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    configs = [c for c in db.get_configs(user["id"]) if not is_config_expired(c)]
    if not configs:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "my_configs_list_empty",
            "شما هنوز هیچ سرویس فعالی نداری.\n\nبرای خرید، از «🛒 خرید اشتراک» اقدام کن.",
            reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="my_configs_list_empty"),
        )
    else:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "my_configs_list_has",
            "سرویس‌های شما\n\nبرای مشاهده‌ی لینک سابسکریپشن و مدیریت هرکدام، روی نام آن بزنید 👇",
            reply_markup=my_configs_list_keyboard(configs, "", "back"),
        )


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "renew", "🔁 تمدید سرویس"))
async def menu_renew(message: types.Message, state: FSMContext):
    """دکمه‌ی «تمدید» منوی پایین: لیست سرویس‌های قابل تمدید؛ انتخاب هر سرویس وارد فرآیند تمدید (renewcfg_) می‌شود."""
    await state.clear()
    if ui_editor.get_button_meta("main_reply", "renew").get("hidden"):
        return  # دکمه برای کاربران خاموش است؛ متنی که کاربر دستی تایپ کرده را نادیده بگیر
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return
    if not db.is_renewal_enabled():
        await message.answer(db.renew_block_text())
        return
    configs = [c for c in db.get_configs(user["id"]) if not c.get("deleted") and db.config_allows_renew(c)]
    if not configs:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "my_configs_list_empty",
            "سرویس قابل تمدیدی ندارید.\n\n(سرویس‌های تست رایگان و PAYG قابل تمدید نیستند.)",
            reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="my_configs_list_empty"),
        )
        return
    await show_menu_with_sticker(
        message.bot, message.chat.id, "my_configs_list_has",
        "🔁 تمدید سرویس\n\nسرویسی که می‌خواهید تمدید کنید را انتخاب کنید 👇",
        reply_markup=renew_services_keyboard(configs),
    )


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "wallet", "💰 کیف پول"))
async def menu_wallet(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    rs = db.get_referral_settings()
    if rs["paid_purchase_required"] and rs["min_volume_enabled"]:
        wallet_condition = f"ℹ️ موجودی در انتظار، پس از خرید حجم {rs['min_volume_gb']} گیگ یا بیشتر توسط فردی که با لینک شما عضو شده، به‌صورت خودکار آزاد می‌شود."
    elif rs["paid_purchase_required"]:
        wallet_condition = "ℹ️ موجودی در انتظار، پس از هر خرید پولی توسط فردی که با لینک شما عضو شده، به‌صورت خودکار آزاد می‌شود."
    else:
        wallet_condition = "ℹ️ پاداش دعوت بدون نیاز به خرید فرد دعوت‌شده، بلافاصله پس از عضویت او آزاد می‌شود."

    text = (
        f"💰 کیف پول شما\n\n"
        f"👛 موجودی قابل استفاده: {user['wallet']:,} تومان\n"
        f"🔒 موجودی در انتظار: {user['locked_wallet']:,} تومان\n\n"
        f"{wallet_condition}"
    )
    _ref_off_rx = (None if db.referral_enabled() else (r"ℹ️|پاداش دعوت" if int(user["locked_wallet"] or 0) > 0 else r"موجودی در انتظار|ℹ️|پاداش دعوت"))
    await show_menu_with_sticker(message.bot, message.chat.id, "wallet", text, reply_markup=wallet_menu(), drop_lines_regex=_ref_off_rx)


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "referral", "👥 دعوت دوستان"))
async def menu_referral(message: types.Message, state: FSMContext):
    await state.clear()
    if not db.referral_enabled():
        await message.answer("این بخش در حال حاضر غیرفعال است.")
        return
    from handlers.referral import send_referral
    if not await send_referral(message.bot, message.chat.id, message.from_user.id):
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "profile", "👤 پروفایل من"))
async def menu_profile(message: types.Message, state: FSMContext):
    await state.clear()
    from handlers.profile import send_profile
    if not await send_profile(message.bot, message.chat.id, message.from_user.id):
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))


@router.message(lambda message: message.text in (ui_editor.get_button("main_reply", "guides", "راهنما"), "📚 راهنما"))
async def menu_user_guides(message: types.Message, state: FSMContext):
    await state.clear()
    guides = db.get_guides()
    if not guides:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "guides_empty",
            "📚 راهنما و آموزش‌ها\n\nهنوز هیچ راهنمایی ثبت نشده.",
            reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="guides_empty"),
        )
        return
    await show_menu_with_sticker(
        message.bot, message.chat.id, "guides_has",
        "📚 راهنما و آموزش‌ها\n\nیکی از موارد زیر را برای مشاهده انتخاب کنید 👇",
        reply_markup=user_guides_menu(guides),
    )


@router.callback_query(F.data == "user_guides")
async def menu_user_guides_callback(callback: types.CallbackQuery):
    guides = db.get_guides()
    if not guides:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "guides_empty", 
            "📚 راهنما و آموزش‌ها\n\nهنوز هیچ راهنمایی ثبت نشده.", reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="guides_empty")
        )
        await callback.answer()
        return
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "guides_has", 
        "📚 راهنما و آموزش‌ها\n\nیکی از موارد زیر را برای مشاهده انتخاب کنید 👇",
        reply_markup=user_guides_menu(guides),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("guideopen_"))
async def menu_user_guide_open(callback: types.CallbackQuery):
    guide_id = int(callback.data.replace("guideopen_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer(ui_editor.get_alert_text("msg_1318898fb7", "❌ این راهنما دیگر موجود نیست."), show_alert=True)
        return

    from handlers.admin import _guide_detail_text_and_entities
    caption, entities = _guide_detail_text_and_entities(guide)
    entities = entities or None
    kb = user_guide_detail_keyboard()
    import rich_message as _rich
    _gr = _rich.load("guide", guide_id)
    if _gr:
        try:
            await _rich.send(callback.bot, callback.message.chat.id, _gr, reply_markup=kb)
            await callback.answer()
            return
        except Exception:
            import logging; logging.getLogger(__name__).exception("Rich Message راهنما ارسال نشد؛ نمایش متن عادی")
    try:
        if guide["content_type"] == "photo" and guide.get("file_id"):
            await callback.message.answer_photo(guide["file_id"], caption=caption, caption_entities=entities, reply_markup=kb)
        elif guide["content_type"] == "video" and guide.get("file_id"):
            await callback.message.answer_video(guide["file_id"], caption=caption, caption_entities=entities, reply_markup=kb)
        else:
            await callback.message.answer(caption, entities=entities, reply_markup=kb)
    except Exception:
        await callback.message.answer(caption, reply_markup=kb)
    await callback.answer()


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "support", "👨‍💻 پشتیبانی"))
async def menu_ticket(message: types.Message, state: FSMContext):
    await state.clear()
    await show_menu_with_sticker(
        message.bot, message.chat.id, "support",
        "👨‍💻 پشتیبانی\n\nمی‌تونی مستقیم تیکت بزنی یا از کانال اصلی و پشتیبان استفاده کنی 👇",
        reply_markup=support_menu(),
    )


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "agent_manage", "🤝 مدیریت نمایندگی"))
async def menu_agent_manage(message: types.Message, state: FSMContext):
    agent = db.get_agent(message.from_user.id)
    if not agent:
        return
    await state.clear()
    cats = db.get_vip_categories()
    selected = set(db.get_agent_category_ids(message.from_user.id))
    scope = "همه دسته‌ها" if not selected else ", ".join(str(c.get("name")) for c in cats if int(c.get("id")) in selected)
    prefix = agent.get("custom_prefix") or "انتخاب نشده"
    text = (f"🤝 مدیریت نمایندگی\n\n💯 تخفیف: {agent.get('vip_discount_percent',0)}٪\n"
            f"🗂 دسته‌های مشمول: {scope}\n🏷 پیشوند ساخت کانفیگ: {prefix}\n\n"
            "از دکمه‌های زیر می‌توانی تنظیمات نمایندگی خودت را مدیریت کنی:")
    from keyboards import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(ui_screen="agent_manage", inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار نمایندگی", callback_data="agentstats", style="primary", ui_screen="agent_manage", ui_button_key="agentstats")],
        [InlineKeyboardButton(text="🏷 انتخاب نام دلخواه برای ساخت کانفیگ", callback_data="agentprefix", style="success", ui_screen="agent_manage", ui_button_key="agentprefix")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back", style="danger", ui_screen="agent_manage", ui_button_key="back")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "agentstats")
async def menu_agent_stats(callback: types.CallbackQuery):
    agent = db.get_agent(callback.from_user.id)
    if not agent:
        await callback.answer("❌ نماینده نیستید.", show_alert=True); return
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ اطلاعات کاربری پیدا نشد.", show_alert=True); return
    cur = db.get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(price),0) AS total FROM orders WHERE user_id=? AND status IN ('paid','processing','fulfilled')", (user['id'],))
    row = db._fetchone(cur) or {"c":0,"total":0}
    await callback.message.edit_text(
        f"📊 آمار نمایندگی\n\n👤 نماینده: {user.get('name','-')}\n"
        f"💯 تخفیف: {agent.get('vip_discount_percent',0)}٪\n"
        f"🛒 تعداد سفارش‌های ثبت‌شده: {row.get('c',0):,}\n"
        f"💰 مجموع مبلغ سفارش‌ها: {int(row.get('total',0) or 0):,} تومان",
        reply_markup=__import__('keyboards').back_button("back", "🔙 بازگشت", screen="agent_manage")
    )
    await callback.answer()


@router.callback_query(F.data == "agentprefix")
async def menu_agent_prefix_start(callback: types.CallbackQuery, state: FSMContext):
    if not db.get_agent(callback.from_user.id):
        await callback.answer("❌ نماینده نیستید.", show_alert=True); return
    await state.set_state(UserStates.waiting_agent_prefix)
    await callback.message.answer("🏷 یک پیشوند انگلیسی برای نام سرویس‌ها بفرست (۱ تا 12 کاراکتر، فقط a-z و 0-9 و _):")
    await callback.answer()


@router.message(UserStates.waiting_agent_prefix)
async def menu_agent_prefix_save(message: types.Message, state: FSMContext):
    import re
    value=(message.text or '').strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,12}", value):
        await message.answer("❌ پیشوند نامعتبر است. فقط a-z، عدد و _ و حداکثر 12 کاراکتر.")
        return
    db.set_agent_prefix(message.from_user.id, value)
    await state.clear()
    await message.answer(f"✅ پیشوند «{value}» ذخیره شد. از این به بعد نام سرویس‌هایی که برایت ساخته می‌شود با همین پیشوند شروع می‌شود (جایگزین پیشوند ربات).", reply_markup=main_reply_keyboard(message.from_user.id))


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "agency", "🤝 درخواست نمایندگی"))
async def menu_agency_request_start(message: types.Message, state: FSMContext):
    await state.clear()
    await show_menu_with_sticker(
        message.bot, message.chat.id, "agency_request",
        "🤝 درخواست نمایندگی\n\n"
        "درخواست و مشخصات خودتون (اسم، شماره تماس، میزان فعالیت/تعداد مشتری تقریبی و توضیحات) "
        "رو در یک پیام بنویسید و ارسال کنید؛ مستقیم برای پشتیبانی فرستاده می‌شه و به‌زودی بررسی و پاسخ داده می‌شه 👇",
        reply_markup=back_button("back", "🔙 انصراف", screen="ticket_write"),
    )
    await state.set_state(UserStates.waiting_agency_request_message)


@router.callback_query(F.data == "agency")
async def cb_agency_request_start(callback: types.CallbackQuery, state: FSMContext):
    """همان «درخواست نمایندگی» منوی پایین، برای دکمه‌های اینلاین/دلخواه (callback=agency)."""
    await callback.answer()
    await menu_agency_request_start(callback.message, state)


@router.message(UserStates.waiting_agency_request_message)
async def menu_agency_request_send(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer(ui_editor.get_text("msg_b940e468ec", "❌ لطفاً درخواستتون رو به‌صورت متن ارسال کنید:"))
        return

    await message.bot.send_message(
        ADMIN_ID,
        f"🤝 درخواست نمایندگی جدید\n👤 {message.from_user.full_name}\n🆔 {uid}\n\n💬 {text}",
    )
    try:
        import adv_reports
        adv_reports.collab_request(uid, message.from_user.full_name, text)
    except Exception:
        pass
    await message.answer(
        ui_editor.get_text("msg_8331e7233a", "✅ درخواست شما برای پشتیبانی ارسال شد. به‌زودی بررسی و باهاتون تماس گرفته می‌شه."),
        reply_markup=main_reply_keyboard(message.from_user.id),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# منوی ادمین
# ---------------------------------------------------------------------------
@router.message(F.text == "📊 آمار")
async def menu_admin_stats(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "stats"):
        return
    await state.clear()
    # بند ۱۰: آمار reply keyboard = همان مسیر inline keyboard با کیبورد دوره‌های زمانی
    from handlers.admin import _render_stats_text, admin_stats_keyboard
    await message.answer(_render_stats_text("all"), reply_markup=admin_stats_keyboard())


@router.message(F.text == "👥 لیست کاربران")
async def menu_admin_userlist(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "users"):
        return
    await state.clear()
    text = (
        f"👥 مدیریت کاربران\n\n"
        f"👥 کل کاربران ثبت‌نامی: {db.count_users()}\n"
        f"🟢 مشتریانی که خرید داشته‌اند: {db.count_customers()}\n\n"
        f"یکی از گزینه‌های زیر را انتخاب کنید 👇"
    )
    await message.answer(text, reply_markup=admin_userlist_menu())


@router.message(F.text == "📢 پیام همگانی")
async def menu_admin_broadcast(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "broadcast"):
        return
    # بند ۱: دقیقاً مثل inline keyboard — ابتدا صفحه‌ی انتخاب مخاطب نشان می‌دهیم
    await message.answer(
        "📢 پیام همگانی\n\nاین پیام برای چه کسانی ارسال شود؟",
        reply_markup=admin_broadcast_audience_keyboard(),
    )


@router.message(F.text == "🎟 مدیریت تخفیف")
async def menu_admin_discount(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "discounts"):
        return
    await state.clear()
    discounts = db.get_all_discounts()
    if not discounts:
        text = "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    else:
        text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇"
    await message.answer(text, reply_markup=admin_discount_menu(discounts))


REFERRERS_PER_PAGE = 10


@router.message(F.text == "🤝 مدیریت دعوت‌ها")
async def menu_admin_referrals(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "referrals"):
        return
    await state.clear()
    total = db.count_referrers()
    if total == 0:
        text = "🤝 هنوز هیچ دعوتی ثبت نشده."
    else:
        text = (
            f"🤝 مدیریت دعوت‌شده‌ها — مرتب‌شده بر اساس بیشترین دعوت\n\n"
            f"👥 تعداد کل دعوت‌کننده‌ها: {total}\n\n"
            f"روی هر کدام بزنید تا لیست دعوت‌شده‌هایش و وضعیت کیف پولش رو ببینید 👇"
        )
    users = db.get_referrers_page(0, REFERRERS_PER_PAGE)
    has_next = total > REFERRERS_PER_PAGE
    await message.answer(text, reply_markup=admin_referrers_page_keyboard(users, 0, has_next))


@router.message(F.text == "📚 مدیریت راهنما")
async def menu_admin_guides(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "guides"):
        return
    await state.clear()
    guides = db.get_guides()
    await message.answer(
        f"📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}\n\n"
        "از اینجا می‌تونید راهنما/آموزش جدید اضافه کنید یا موردهای موجود را ویرایش کنید:",
        reply_markup=admin_guides_menu(guides),
    )


@router.message(F.text == "🔍 جستجوی حرفه‌ای")
async def menu_admin_professional_search(message: types.Message, state: FSMContext):
    """جستجوی یکپارچه (کاربر + سرویس) — بلافاصله منتظر متن جستجو می‌شود."""
    if not _has_admin_permission(message.from_user.id, "users"):
        return
    from handlers.admin import _open_search_prompt
    await _open_search_prompt(message, state)


@router.message(F.text == "💾 بکاپ")
async def menu_admin_backup(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "backup"):
        return
    await state.clear()
    from handlers.admin_backup import show_page
    await show_page(message)




# ---------------------------------------------------------------------------
# ➕ دکمه‌های دلخواه ادمین روی «منوی اصلی» (Reply Keyboard)
# ---------------------------------------------------------------------------
def _main_menu_custom_button(text: str | None):
    if not text:
        return None
    try:
        for b in ui_editor.get_custom_buttons("main_reply"):
            if b.get("text") == text:
                return b
    except Exception:
        pass
    return None


@router.message(lambda message: _main_menu_custom_button(message.text) is not None)
async def menu_custom_button(message: types.Message, state: FSMContext):
    b = _main_menu_custom_button(message.text)
    if not b:
        return
    await state.clear()
    typ, val = b.get("action_type"), (b.get("action_value") or "")
    if typ == "webapp":
        # در حالت عادی خودِ دکمه‌ی پایین WebApp را باز می‌کند؛ این مسیر فقط پشتیبان است.
        btn = types.InlineKeyboardButton(text=b["text"], web_app=types.WebAppInfo(url=val))
    elif typ == "url":
        btn = types.InlineKeyboardButton(text=b["text"], url=val)
    else:
        # دکمه‌ی callback: مستقیم همان بخش باز می‌شود (بدون پیام/دکمه‌ی اضافه)
        import cbdispatch
        if await cbdispatch.open_callback(message.bot, message.from_user, message.chat.id, val):
            return
        btn = types.InlineKeyboardButton(text=b["text"], callback_data=val[:64])
    await message.answer("👇 برای ادامه روی دکمه بزن:", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[btn]]))
