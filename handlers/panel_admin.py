"""
handlers/panel_admin.py
مدیریت یکپارچه‌شده‌ی هر سه نوع پنل (شاهراه / مرزبان / پاسارگارد).

⚠️ جایگزین handlers/shahrah_admin.py قدیمی (یکپنلی، فقط شاهراه). این ماژول:
- هر سه نوع پنل را هم‌زمان پشتیبانی می‌کند (هر سه در یک لحظه می‌توانند فعال باشند).
- هر نوع پنل می‌تواند چند نمونه (Instance) هم‌زمان داشته باشد (مدیریت در دکمه‌های جداگانه).
- نگاشت پلن/بسته در سطح "کدام نمونه‌ی پنل" انجام می‌شود (تا ادمین بتواند برای هر پلن/بسته تعیین
  کند دقیقاً از کدام نمونه‌ی پنل استفاده شود).

پلتفرم واقعی هر پنل فقط از ماژول panels.py صدا می‌شود (هیچ‌وقت مستقیم به shahrah.py /
marzban_panel.py / pasargad_panel.py وصل نمی‌شود) تا رفتار برای هر سه نوع یکسان بماند.

۰️⃣ قانون ارسال VIP بر اساس روش پرداخت (بدون تفاوت با قبل):
- کیف پول و پرداخت آنلاین: اگر پلن/دسته یک نگاشت فعال به یک نمونه‌ی پنل داشته باشد، سرویس
  بلافاصله و کاملاً خودکار از همون نمونه ساخته و برای مشتری ارسال می‌شود. اگر نگاشتی
  وجود ندارد یا نمونه‌ی مقصد غیرفعال است، دقیقاً متل قبل به ادمین اطلاع داده می‌شود تا خودش دستی
  ارسال کند.
- کارت‌به‌کارت: تفاوتی نکرده — بعد از تایید رسید توسط ادمین، دکمه‌ی «ارسال خودکار از پنل»
  همان مقصدی که برای این پلن نگاشت شده را نشان می‌دهد.

"""

import html
import json
import logging
import notif_settings
import re
import secrets
import string
from datetime import datetime, timedelta
from subscription import format_service_package, days_remaining
from io import BytesIO

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

import database as db
import crypto
import panels
import fsm_storage
import bot_info
import ui_editor
from config import ADMIN_ID
from states import AdminStates
from keyboards import (
    admin_vpn_panel_types_keyboard,
    admin_vpn_panel_list_keyboard,
    admin_vpn_panel_detail_keyboard,
    admin_vpn_panel_delete_confirm_keyboard,
    admin_vpn_panel_edit_menu_keyboard,
    admin_vpn_panel_auth_choice_keyboard,
    admin_create_service_panel_keyboard,
    admin_create_service_catalog_keyboard,
    admin_create_service_mode_keyboard,
    admin_create_service_group_keyboard,
    vpn_panel_back_keyboard,
    admin_vpn_panel_types_cancel_keyboard,
    admin_vpn_panel_map_menu_keyboard,
    vpn_map_category_pick_keyboard,
    vpn_map_vip_category_pick_keyboard,
    vpn_map_vip_plans_keyboard,
    vpn_catalog_pick_keyboard,
    config_delivery_keyboard,
    vpn_map_mode_keyboard,
    vpn_direct_multiselect_keyboard,
    vpn_direct_protocol_keyboard,
)
from utils import is_duplicate_action, now_tehran_naive, parse_int_in_range

_LATIN_NAME_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
from handlers.admin import _is_admin, _log_fulfilled_order, AdminPermissionMiddleware

router = Router(name="panel_admin")
router.message.middleware(AdminPermissionMiddleware())
router.callback_query.middleware(AdminPermissionMiddleware())
logger = logging.getLogger(__name__)

try:
    import qrcode
except ImportError:
    qrcode = None  # اگر نصب نباشد، لینک به‌جای عکس QR به‌صورت متنی فرستاده می‌شود.


def _make_qr_bytes(link: str) -> bytes:
    img = qrcode.make(link)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pretty(data, limit: int = 1500) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        text = str(data)
    if len(text) > limit:
        text = text[:limit] + "\n... (بریده‌شد)"
    return html.escape(text)


def _admin_fsm(bot) -> FSMContext | None:
    """FSMContext مربوط به چت خود ادمین، برای زمانی که می‌خواهیم از یک مسیر
    قیرتعاملی (متل بعد از پرداخت کیف‌پول/آنلاین که در چت مشتری اتفاق می‌افتد)
    همان مکانیزم «لطفاً لینک رو دستی بفرست» را روی چت ادمین صدا بزنیم.
    اگر هنوز storage ثبت نشده باشد (مثلاً در تست) None برمی‌گرداند."""
    if fsm_storage.storage is None:
        return None
    return FSMContext(
        storage=fsm_storage.storage,
        key=StorageKey(bot_id=bot.id, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


# ---------------------------------------------------------------------------
# 🤖 ارسال کاملاً خودکار بعد از پرداخت کیف‌پول/آنلاین (بدون دخالت ادمین)
# فقط VIP و «بساز سرویس خودت» — 
# اگر هیچ نمونه‌ی پنلی برای این پلن/بسته نگاشت نشده یا غیرفعال باشد، False برمی‌گرداند
# تا مسیر همیشگی (اطلاع دستی به ادمین) دنبال شود و هیچ سفارشی گم نشود.
# ---------------------------------------------------------------------------
async def auto_fulfill_vip_via_panel(bot, uid, plan_key: str, order_id: int | None, collector: list | None = None) -> bool:
    mapping = db.get_panel_map_for_plan_key(plan_key)
    if not mapping or not mapping.get("enabled"):
        return False

    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if not plan or not user:
        return False

    panel = db.get_vpn_panel(mapping["panel_id"])
    if not panel or not panel.get("enabled"):
        return False

    # Atomic fulfillment claim: only one worker may create the remote service.
    if order_id:
        current = db.get_order(order_id)
        if current and current.get("status") == "fulfilled":
            return True
        if current and current.get("status") == "processing":
            return True
        if current and current.get("status") not in ("pending", "paid"):
            return False
        if not db.claim_order_for_processing(order_id):
            fresh = db.get_order(order_id)
            return bool(fresh and fresh.get("status") in ("processing", "fulfilled"))

    username = _username_for_order(uid, order_id)
    ok, link, remote_service_id, data, msg = await panels.create_service(
        panel, username, mapping["remote_ref"],
        volume_gb=plan.get("volume_gb"), days=plan.get("days"), device_limit=plan.get("user_limit"),
    )
    if not ok:
        if order_id:
            try: db.set_order_status(order_id, "paid")
            except Exception: logger.exception("failed to release VIP order claim %s", order_id)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ خرید VIP (کیف‌پول/پرداخت آنلاین) قرار بود خودکار از {panels.panel_label(panel)} ارسال شود ولی "
            f"ساخت سرویس در پنل ناموفق بود:\n{msg}\n"
            f"🔑 مرجع ارسال‌شده: {_display_remote_ref(mapping)}\n\n"
            "لطفاً از دکمه‌ی ارسال دستی زیر همین سفارش استفاده کن. اگه خطا NOT_FOUND بود، احتمالاً باید "
            "این نگاشت رو دوباره از مدیریت این پنل تنظیم کنی.",
        )
        return False

    if notif_settings.enabled("admin_panel_log"):
        try:  # خطای ارسال این پیام ادمین نباید تحویل سرویسِ ساخته‌شده را متوقف کند (سفارش در processing می‌ماند)
            await bot.send_message(
                ADMIN_ID, f"📨 پاسخ پنل {panels.panel_label(panel)} (ارسال خودکار بعد از پرداخت):\n<pre>{_pretty(data)}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("ارسال لاگ پاسخ پنل به ادمین ناموفق بود")

    snapshot = {"name": plan.get("name"), "volume_gb": plan.get("volume_gb"), "days": plan.get("days")}
    ctx = {"uid": uid, "plan_key": plan_key, "order_id": order_id, "order_kind": "plan",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}
    if collector is not None:
        ctx["collector"] = collector  # حالت «میانبر تست»: تحویل جمعی در یک پیام (بدون QR)

    if not link:
        ctx.pop("collector", None)  # لینک خودکار پیدا نشد: مسیر دستی (ادمین) عادی ادامه می‌یابد
        admin_state = _admin_fsm(bot)
        if admin_state:
            await admin_state.update_data(panel_pending_ctx=ctx)
            await admin_state.set_state(AdminStates.waiting_panel_manual_link)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ سرویس در {panels.panel_label(panel)} ساخته شد (خودکار، بعد از پرداخت) ولی لینک ساب به‌صورت "
            "خودکار پیدا نشد.\nلطفاً لینک رو از پاسخ بالا کپی و همینجا برام بفرست:",
        )
        return True

    await _deliver_panel_link(bot, ctx, link)
    return True


async def auto_fulfill_custom_via_panel(bot, user: dict, order_id: int, volume, days, custom_name) -> bool:
    """Fulfill a custom order/renewal exactly once on the authoritative panel."""
    current = db.get_custom_order(order_id)
    if current and current.get("status") == "fulfilled":
        return True
    if current and current.get("status") == "processing":
        return True
    if current and current.get("status") not in ("pending", "paid"):
        return False
    if not db.claim_custom_order_for_processing(order_id):
        fresh = db.get_custom_order(order_id)
        return bool(fresh and fresh.get("status") in ("processing", "fulfilled"))

    order = db.get_custom_order(order_id)
    if order and order.get("order_type") == "renew" and order.get("target_config_id"):
        ok, msg = await _fulfill_custom_renew(bot, order, order_id, volume, days)
        try:
            if notif_settings.enabled("admin_delivery_report"):
                await bot.send_message(ADMIN_ID, msg)
        except Exception:
            pass
        return ok

    _opt = int((order or {}).get("option_num") or 0)
    mapping = db.get_option_mapping("custom_build", _opt)
    if not mapping and _opt:
        mapping = db.get_option_mapping("custom_build", 0)
    if not mapping or not mapping.get("enabled"):
        try: db.set_custom_order_status(order_id, "paid")
        except Exception: logger.exception("failed to release custom order claim %s", order_id)
        return False

    panel = db.get_vpn_panel(mapping["panel_id"])
    if not panel or not panel.get("enabled"):
        try: db.set_custom_order_status(order_id, "paid")
        except Exception: logger.exception("failed to release custom order claim %s", order_id)
        return False

    ok, link, remote_service_id, data, msg = await _create_custom_service(panel, user["telegram_id"], order, mapping["remote_ref"])
    if not ok:
        try: db.set_custom_order_status(order_id, "paid")
        except Exception: logger.exception("failed to release custom order claim %s", order_id)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ سفارش «بساز سرویس خودت» (کیف‌پول/پرداخت آنلاین) قرار بود خودکار از {panels.panel_label(panel)} ارسال "
            f"شود ولی ساخت سرویس ناموفق بود:\n{msg}\n"
            f"🔑 مرجع ارسال‌شده: {_display_remote_ref(mapping)}\n\n"
            "لطفاً از دکمه‌ی ارسال دستی این سفارش استفاده کن.",
        )
        return False

    if notif_settings.enabled("admin_panel_log"):
        try:  # خطای ارسال این پیام ادمین نباید تحویل سرویسِ ساخته‌شده را متوقف کند (سفارش در processing می‌ماند)
            await bot.send_message(
                ADMIN_ID, f"📨 پاسخ پنل {panels.panel_label(panel)} (ارسال خودکار بعد از پرداخت):\n<pre>{_pretty(data)}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("ارسال لاگ پاسخ پنل به ادمین ناموفق بود")

    snapshot = {"name": custom_name or "سرویس سفارشی", "volume_gb": volume, "days": days}
    ctx = {"uid": user["telegram_id"], "plan_key": None, "order_id": order_id, "order_kind": "custom",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        admin_state = _admin_fsm(bot)
        if admin_state:
            await admin_state.update_data(panel_pending_ctx=ctx)
            await admin_state.set_state(AdminStates.waiting_panel_manual_link)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ سرویس در {panels.panel_label(panel)} ساخته شد (خودکار، بعد از پرداخت) ولی لینک ساب به‌صورت "
            "خودکار پیدا نشد.\nلطفاً لینک رو از پاسخ بالا کپی و همینجا برام بفرست:",
        )
        return True

    await _deliver_panel_link(bot, ctx, link)
    return True


# ---------------------------------------------------------------------------
# 📂 قدم اول: انتخاب نوع پنل (همه هم‌زمان قابل مدیریت) و لیست نمونه‌ها
# ---------------------------------------------------------------------------

_USERNAME_ALPHABET = string.ascii_lowercase + string.digits  # فقط حروف کوچک+عدد، چون بعضی پنل‌ها حروف بزرگ رو با کوچک یکی می‌بینن
_USERNAME_INVALID_RE = re.compile(r"[^a-z0-9_]+")
_USERNAME_MULTI_UNDERSCORE_RE = re.compile(r"_+")


def _to_base36(num: int) -> str:
    """تبدیل عدد به پایه‌ی ۳۶ (حروف کوچک + عدد) برای کوتاه‌تر شدن کد."""
    num = int(num)
    if num == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    n = abs(num)
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


_CHOSEN_NAME_RE = re.compile(r"[a-z0-9_]{3,32}")


def _username_for_order(telegram_id, order_id=None) -> str:
    """یوزرنیمی که روی پنل ساخته می‌شود.

    حالت «نامگذاری توسط کاربر»: اگر کاربر هنگام خرید نامی انتخاب کرده باشد، همان نام (که روی سفارش
    ذخیره شده) دقیقاً به‌عنوان یوزرنیم پنل استفاده می‌شود. وگرنه (حالت پیش‌فرض، نماینده‌ها، تست رایگان،
    مینی‌اپ و ...) همان معماریِ همیشگیِ «پیشوند + ۸ رقم» (_service_username) اجرا می‌شود."""
    if order_id:
        try:
            chosen = str((db.get_order(int(order_id)) or {}).get("service_name") or "").strip().lower()
            if chosen and _CHOSEN_NAME_RE.fullmatch(chosen):
                return chosen
        except Exception:
            logger.exception("خواندن نام انتخابی سفارش %s ناموفق بود", order_id)
    return _service_username(telegram_id)


def _custom_name_base(order) -> str | None:
    """نام انتخابیِ کاربر در مرحله‌ی «نام سرویس» سفارشی (فقط لاتین/عدد/_ ، ۳ تا ۳۲ کاراکتر) یا None."""
    try:
        chosen = str((order or {}).get("custom_name") or "").strip().lower()
        if chosen and _CHOSEN_NAME_RE.fullmatch(chosen):
            return chosen
    except Exception:
        logger.exception("خواندن نام سفارش سفارشی ناموفق بود")
    return None


def _suffixed_username(base: str) -> str:
    import secrets
    _sfx = bot_info.random_suffix()
    return f"{base[:31 - len(_sfx)].rstrip('_') or 'tg'}_{_sfx}"


def _looks_duplicate(msg) -> bool:
    m = str(msg or "").lower()
    return any(k in m for k in ("exist", "already", "duplicate", "409", "موجود", "تکراری", "قبلا", "قبلاً", "conflict", "unique"))


async def _create_custom_service(panel, telegram_id, order, remote_ref):
    """ساخت سرویس سفارشی با همان نامی که کاربر داده است؛ اگر از قبل روی پنل بود، «_ + ۸ رقم تصادفی» اضافه می‌شود.
    خروجی: همان tuple بخش create_service."""
    base = _custom_name_base(order)
    volume, days = order["volume_gb"], order["days"]
    if not base:
        return await panels.create_service(panel, _service_username(telegram_id), remote_ref, volume_gb=volume, days=days)
    username = base
    if panel.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"):
        try:
            ok_s, snap, _m = await panels.get_service_snapshot(panel, username)
            if ok_s and snap:
                username = _suffixed_username(base)
        except Exception:
            logger.exception("بررسی تکراری‌بودن نام سرویس روی پنل ناموفق بود")
    res = await panels.create_service(panel, username, remote_ref, volume_gb=volume, days=days)
    if not res[0] and username == base and _looks_duplicate(res[4]):
        res = await panels.create_service(panel, _suffixed_username(base), remote_ref, volume_gb=volume, days=days)
    return res


def _service_username(telegram_id) -> str:
    """نام سرویس: پیشوند قابل‌تنظیم ادمین + کد کوتاه یکتا (سازگار با مرزبان/پاسارگارد).

    fix: نسخه‌ی قبلی کد بعد از پیشوند رو با «آیدی عددی کامل + ۸ رقم زمان»
    می‌ساخت که هم خیلی طولانی بود (روی پنل‌هایی مثل مرزبان که طول یوزرنیم
    محدوده، امکان رد شدن یا نامعتبر شدن داشت) و هم چون فقط از عدد استفاده
    می‌کرد فضای اسم‌های ممکن محدود بود. الان شناسه‌ی کاربر با پایه‌ی ۳۶
    (حروف+عدد) کدگذاری می‌شه که خودش کوتاه‌تره، و یک پسوند ۴ کاراکتری
    تصادفیِ امن (حروف+عدد) هم اضافه می‌شه تا برخورد اسمی پیش نیاد.

    ضمناً پیشوند دقیقاً طبق قوانین مرزبان نرمال‌سازی می‌شه (فقط
    a-z/0-9/_، بدون آندرلاین تکراری یا در ابتدا/انتها) و کل نام نهایی
    هیچ‌وقت از ۳۲ کاراکتر بیشتر نمی‌شه، چون این سقف واقعی مرزبان/پاسارگارده.
    """
    raw = str(bot_info.get("config_name_prefix") or "tg").strip().lower()
    try:
        agent = db.get_agent(telegram_id)
        custom_agent_prefix = str((agent or {}).get("custom_prefix") or "").strip().lower()
        if custom_agent_prefix:
            raw = custom_agent_prefix  # پیشوند نماینده جایگزین پیشوند ربات می‌شود
    except Exception:
        pass
    prefix = _USERNAME_INVALID_RE.sub("_", raw)
    prefix = _USERNAME_MULTI_UNDERSCORE_RE.sub("_", prefix).strip("_") or "tg"
    prefix = prefix[:20]

    # بخش دوم نام: پسوند تصادفی طبق تنظیم ادمین (پیش‌فرض ۸ رقم عدد)
    num_code = bot_info.random_suffix()
    username = f"{prefix}_{num_code}"
    username = _USERNAME_MULTI_UNDERSCORE_RE.sub("_", username).strip("_")
    if len(username) > 32:
        username = f"{prefix[:31 - len(num_code)].rstrip('_') or 'tg'}_{num_code}"
    if len(username) < 3:
        username = f"tg_{num_code}"
    return username

@router.callback_query(F.data == "admin_vpn_panels")
async def open_vpn_panel_types(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🖥 مدیریت پنل‌های VPN\n\n"
        "هر سه نوع پنل (شاهراه/مرزبان/پاسارگارد) می‌توانند هم‌زمان فعال باشند و هرکدام می‌تواند چند نمونه داشته باشد.\n"
        "یک نوع رو انتخاب کن:",
        reply_markup=admin_vpn_panel_types_keyboard(),
    )
    await callback.answer()


@router.message(F.text == "🖥 مدیریت پنل‌های VPN")
async def menu_admin_vpn_panels(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🖥 مدیریت پنل‌های VPN\n\n"
        "هر سه نوع پنل (شاهراه/مرزبان/پاسارگارد) می‌توانند هم‌زمان فعال باشند و هرکدام می‌تواند چند نمونه داشته باشد.\n"
        "یک نوع رو انتخاب کن:",
        reply_markup=admin_vpn_panel_types_keyboard(),
    )


@router.callback_query(F.data.startswith("vpntype|"))
async def open_vpn_panel_type_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_type = callback.data.split("|")[1]
    if panel_type not in panels.PANEL_TYPES:
        await callback.answer("❌ نوع پنل نامعتبر.", show_alert=True)
        return
    instances = db.list_vpn_panels(panel_type=panel_type)
    label = panels.PANEL_TYPE_LABELS[panel_type]
    text = f"🖥 نمونه‌های پنل {label}"
    if not instances:
        text += "\n\nهنوز هیچ نمونه‌ای از این نوع اضافه نشده. می‌تونی چند نمونه هم‌زمان از این نوع اضافه کنی."
    await callback.message.edit_text(text, reply_markup=admin_vpn_panel_list_keyboard(panel_type, instances))
    await callback.answer()


@router.callback_query(F.data.startswith("vpndetail|"))
async def open_vpn_panel_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    status = "🟢 فعال" if panel.get("enabled") else "🔴 غیرفعال"
    text = (
        f"🖥 {panels.panel_label(panel)}\n"
        f"وضعیت: {status}\n"
        f"🌐 ادرس: <code>{html.escape(panel.get('base_url') or '')}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_vpn_panel_detail_keyboard(panel))
    await callback.answer()


@router.callback_query(F.data.startswith("vpntest|"))
async def vpn_panel_test(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.answer("⏳ در حال تست اتصال...")
    ok, data, msg = await panels.test_connection(panel)
    if not ok:
        await callback.message.answer(f"❌ اتصال ناموفق: {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await callback.message.answer(
        f"✅ اتصال به {panels.panel_label(panel)} برقرار است.\n<pre>{_pretty(data)}</pre>",
        parse_mode="HTML", reply_markup=vpn_panel_back_keyboard(panel_id),
    )


@router.callback_query(F.data.startswith("vpntoggle|"))
async def vpn_panel_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    db.update_vpn_panel(panel_id, enabled=not panel.get("enabled"))
    panel = db.get_vpn_panel(panel_id)
    status = "🟢 فعال" if panel.get("enabled") else "🔴 غیرفعال"
    text = (
        f"🖥 {panels.panel_label(panel)}\n"
        f"وضعیت: {status}\n"
        f"🌐 ادرس: <code>{html.escape(panel.get('base_url') or '')}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_vpn_panel_detail_keyboard(panel))
    await callback.answer("✅ وضعیت به‌روز شد.")


@router.callback_query(F.data.startswith("vpndelete|"))
async def vpn_panel_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ مطمئنی می‌خوای {panels.panel_label(panel)} حذف شود؟\n"
        "تمام نگاشت‌های پلن/بسته مربوط به این نمونه هم حذف می‌شوند (سرویس‌های قبلی سالم می‌مانند).",
        reply_markup=admin_vpn_panel_delete_confirm_keyboard(panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpndeleteconfirm|"))
async def vpn_panel_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    panel_type = panel["panel_type"]
    db.delete_vpn_panel(panel_id)
    instances = db.list_vpn_panels(panel_type=panel_type)
    await callback.message.edit_text(
        f"🗑 حذف شد. نمونه‌های فعلی پنل {panels.PANEL_TYPE_LABELS[panel_type]}:",
        reply_markup=admin_vpn_panel_list_keyboard(panel_type, instances),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ➕ افزودن نمونه‌ی جدید از یک نوع پنل (FSM)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("vpnadd|"))
async def vpn_panel_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_type = callback.data.split("|")[1]
    if panel_type not in panels.PANEL_TYPES:
        await callback.answer("❌ نوع پنل نامعتبر.", show_alert=True)
        return
    await state.update_data(new_panel_type=panel_type)
    await state.set_state(AdminStates.waiting_panel_name)
    await callback.message.edit_text(
        f"➕ افزودن پنل {panels.PANEL_TYPE_LABELS[panel_type]} جدید\n\n"
        "یک نام دلخواه برای این نمونه بفرست (فقط برای تشخیص خودت در لیست، مثلاً «سرور 1 المان»):",
        reply_markup=admin_vpn_panel_types_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_panel_name)
async def vpn_panel_add_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام خالی معتبر نیست. دوباره بفرست:")
        return
    await state.update_data(new_panel_name=name)
    await state.set_state(AdminStates.waiting_panel_base_url)
    await message.answer("🌐 ادرس پایه (base URL) این پنل رو بفرست (مثلاً https://panel.example.com):")


@router.message(AdminStates.waiting_panel_base_url)
async def vpn_panel_add_base_url(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    base_url = (message.text or "").strip()
    if not base_url.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک ادرس معتبر نیست؛ لطفاً با http یا https بفرست:")
        return
    data = await state.get_data()
    panel_type = data.get("new_panel_type")
    await state.update_data(new_panel_base_url=base_url.rstrip("/"))
    if panel_type == "shahrah":
        await state.update_data(new_panel_auth_method="api_key")
        await state.set_state(AdminStates.waiting_panel_api_key)
        await message.answer("🔑 API Key این نمونه رو بفرست:")
    elif len(panels.PANEL_AUTH_METHODS.get(panel_type, ())) > 1:
        # 🆕 این نوع پنل هم از یوزرنیم/پسورد و هم از API Key پشتیبانی می‌کند؛
        # ادمین انتخاب می‌کند کدام‌یک برای این نمونه استفاده شود.
        await message.answer(
            "🔌 روش اتصال به این پنل را انتخاب کن:",
            reply_markup=admin_vpn_panel_auth_choice_keyboard(panel_type),
        )
    else:
        await state.update_data(new_panel_auth_method="userpass")
        await state.set_state(AdminStates.waiting_panel_username)
        await message.answer("👤 نام کاربری این نمونه رو بفرست:")


@router.callback_query(F.data.startswith("vpnauthadd|"))
async def vpn_panel_add_auth_choice(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_type, auth_method = callback.data.split("|")
    await state.update_data(new_panel_auth_method=auth_method)
    if auth_method == "api_key":
        await state.set_state(AdminStates.waiting_panel_api_key)
        await callback.message.edit_text("🔑 API Key این نمونه رو بفرست:")
    else:
        await state.set_state(AdminStates.waiting_panel_username)
        await callback.message.edit_text("👤 نام کاربری این نمونه رو بفرست:")
    await callback.answer()


@router.message(AdminStates.waiting_panel_api_key)
async def vpn_panel_add_api_key(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    api_key = (message.text or "").strip()
    if not api_key:
        await message.answer("❌ API Key خالی معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    panel_id = db.create_vpn_panel(
        data["new_panel_type"], data["new_panel_name"], data["new_panel_base_url"], api_key=api_key,
        auth_method=data.get("new_panel_auth_method", "api_key"),
    )
    await state.clear()
    panel = db.get_vpn_panel(panel_id)
    await message.answer(
        f"✅ پنل {panels.panel_label(panel)} اضافه شد.",
        reply_markup=admin_vpn_panel_detail_keyboard(panel),
    )


@router.message(AdminStates.waiting_panel_username)
async def vpn_panel_add_username(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    username = (message.text or "").strip()
    if not username:
        await message.answer("❌ نام کاربری خالی معتبر نیست. دوباره بفرست:")
        return
    await state.update_data(new_panel_username=username)
    await state.set_state(AdminStates.waiting_panel_password)
    await message.answer("🔐 رمز عبور این نمونه رو بفرست:")


@router.message(AdminStates.waiting_panel_password)
async def vpn_panel_add_password(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    password = (message.text or "").strip()
    if not password:
        await message.answer("❌ رمز عبور خالی معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    panel_id = db.create_vpn_panel(
        data["new_panel_type"], data["new_panel_name"], data["new_panel_base_url"],
        username=data["new_panel_username"], password=password,
        auth_method=data.get("new_panel_auth_method", "userpass"),
    )
    await state.clear()
    panel = db.get_vpn_panel(panel_id)
    await message.answer(
        f"✅ پنل {panels.panel_label(panel)} اضافه شد.",
        reply_markup=admin_vpn_panel_detail_keyboard(panel),
    )


# ---------------------------------------------------------------------------
# ✏️ ویرایش یک نمونه‌ی موجود
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("vpnedit|"))
async def vpn_panel_edit_menu(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"✏️ ویرایش {panels.panel_label(panel)}\nیک فیلد رو انتخاب کن:",
        reply_markup=admin_vpn_panel_edit_menu_keyboard(panel),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnauthswitch|"))
async def vpn_panel_auth_switch(callback: types.CallbackQuery):
    """جابه‌جایی روش اتصال یک نمونه‌ی موجود پنل (مرزبان/پاسارگارد) بین
    یوزرنیم/پسورد و API Key. مقدار فیلدهای طرف مقابل حذف نمی‌شود (اگر قبلاً
    پر شده بود) تا اگر ادمین دوباره برگردد، لازم نباشد از نو واردشان کند؛
    فقط اگر فیلدهای لازم برای روش جدید هنوز خالی باشد، تذکر داده می‌شود."""
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    current = panel.get("auth_method") or "userpass"
    new_method = "api_key" if current == "userpass" else "userpass"
    db.update_vpn_panel(panel_id, auth_method=new_method)
    panel = db.get_vpn_panel(panel_id)
    missing_warning = ""
    if new_method == "api_key" and not panel.get("api_key"):
        missing_warning = "\n\n⚠️ هنوز API Key ثبت نشده؛ از همین منو مقدارش رو وارد کن."
    elif new_method == "userpass" and not (panel.get("username") and panel.get("password")):
        missing_warning = "\n\n⚠️ هنوز نام کاربری/رمز عبور ��بت نشده؛ از همین منو مقدارشون رو وارد کن."
    await callback.message.edit_text(
        f"✅ روش اتصال {panels.panel_label(panel)} تغییر کرد.{missing_warning}\nیک فیلد رو انتخاب کن:",
        reply_markup=admin_vpn_panel_edit_menu_keyboard(panel),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpneditfield|"))
async def vpn_panel_edit_field_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, field = callback.data.split("|")
    panel_id = int(panel_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    field_labels = {
        "name": "نام", "base_url": "ادرس پایه (base URL)", "api_key": "API Key",
        "username": "نام کاربری", "password": "رمز عبور",
    }
    await state.update_data(edit_panel_id=panel_id, edit_panel_field=field)
    await state.set_state(AdminStates.waiting_panel_edit_field)
    await callback.message.edit_text(
        f"✅ {field_labels.get(field, field)} جدید رو برای {panels.panel_label(panel)} بفرست:",
        reply_markup=vpn_panel_back_keyboard(panel_id),
    )
    await callback.answer()


@router.message(AdminStates.waiting_panel_edit_field)
async def vpn_panel_edit_field_received(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ مقدار خالی معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    panel_id = data.get("edit_panel_id")
    field = data.get("edit_panel_field")
    panel = db.get_vpn_panel(panel_id) if panel_id else None
    if not panel or not field:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا دوباره تلاش کن.")
        await state.clear()
        return
    if field == "base_url":
        value = value.rstrip("/")
    db.update_vpn_panel(panel_id, **{field: value})
    await state.clear()
    panel = db.get_vpn_panel(panel_id)
    await message.answer(
        f"✅ ذخیره شد. {panels.panel_label(panel)} به‌روز شد.",
        reply_markup=admin_vpn_panel_detail_keyboard(panel),
    )


# ---------------------------------------------------------------------------
# 🗂 نگاشت پلن/بسته → این نمونه‌ی مشخص از پنل
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("vpnmap|"))
async def vpn_panel_map_menu(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗂 نگاشت پلن/بسته به {panels.panel_label(panel)}\n\n"
        "کدام بخش رو می‌خوای به این نمونه وصل کنی؟",
        reply_markup=admin_vpn_panel_map_menu_keyboard(panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnmapdelplan|"))
async def vpn_map_delete_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    _, panel_id, plan_id = callback.data.split("|")
    plan = db.get_vip_plan_by_id(int(plan_id))
    if not plan:
        await callback.answer("❌ پلن پیدا نشد.", show_alert=True); return
    db.delete_panel_plan_map("vip_plan", int(plan_id))
    await callback.answer("🗑 نگاشت پلن حذف شد.", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=vpn_map_vip_plans_keyboard(plan["category_id"], db.get_vip_plans(plan["category_id"]), int(panel_id)))

@router.callback_query(F.data.startswith("vpnmapdelcat|"))
async def vpn_map_delete_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id): return
    _, panel_id, scope, scope_id = callback.data.split("|")
    if scope != "vip_category" or not scope_id.isdigit():
        await callback.answer("❌ نگاشت قابل حذف پیدا نشد.", show_alert=True); return
    db.delete_panel_plan_map("vip_category", int(scope_id))
    await callback.answer("🗑 نگاشت پیش‌فرض دسته حذف شد.", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=vpn_map_vip_plans_keyboard(int(scope_id), db.get_vip_plans(int(scope_id)), int(panel_id)))

@router.callback_query(F.data.startswith("vpnmapvip|"))
async def vpn_map_vip(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    cats = db.get_vip_categories()
    if not cats:
        await callback.answer("هنوز هیچ دسته‌بندی VIP‌ای ساخته نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗂 یک دسته‌بندی VIP رو انتخاب کن تا پلن‌های داخلش رو ببینی و برای هرکدوم جداگانه به {panels.panel_label(panel)} وصلشون کنی:",
        reply_markup=vpn_map_vip_category_pick_keyboard(cats, panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnmapvipcat|"))
async def vpn_map_vip_cat_pick(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, cat_id_str = callback.data.split("|")
    panel_id, cat_id = int(panel_id_str), int(cat_id_str)
    plans_list = db.get_vip_plans(cat_id)
    if not plans_list:
        await callback.answer("این دسته‌بندی هنوز هیچ پلنی نداره.", show_alert=True)
        return
    await callback.message.edit_text(
        "یک پلن رو انتخاب کن تا بسته/تمپلیت متناظرش از این نمونه پنل رو مشخص کنی:",
        reply_markup=vpn_map_vip_plans_keyboard(cat_id, plans_list, panel_id),
    )
    await callback.answer()


def _display_remote_ref(mapping: dict) -> str:
    """برای نگاشت‌های «بدون تمپلیت» به‌جای دیکته‌ی خام JSON، همان خلاصه‌ی خوانا
    (remote_name، مثل «بدون تمپلیت (۳ اینباند)») نشان داده می‌شود."""
    ref = mapping.get("remote_ref") or ""
    if isinstance(ref, str) and ref.startswith("direct:"):
        return mapping.get("remote_name") or "بدون تمپلیت"
    return str(ref)


async def _open_catalog_picker(callback: types.CallbackQuery, state: FSMContext, panel: dict, panel_id: int,
                                scope: str, scope_id: int, prompt: str):
    await callback.answer("⏳ در حال دریافت لیست از پنل...")
    choices, msg = await panels.get_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await state.update_data(panel_map_scope=scope, panel_map_scope_id=scope_id,
                             panel_map_choices=choices, panel_map_panel_id=panel_id)
    await callback.message.answer(prompt, reply_markup=vpn_catalog_pick_keyboard(choices, panel_id))


async def _open_direct_picker(callback: types.CallbackQuery, state: FSMContext, panel: dict, panel_id: int,
                               scope: str, scope_id: int, prompt: str):
    await callback.answer("⏳ در حال دریافت اینباند/گروه‌های زنده‌ی پنل...")
    choices, msg = await panels.get_direct_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await state.update_data(panel_map_scope=scope, panel_map_scope_id=scope_id,
                             panel_map_direct_choices=choices, panel_map_direct_selected=[], panel_map_panel_id=panel_id)
    if panel.get("panel_type") == "marzban":
        protocols=sorted({str(c.get("protocol") or "unknown") for c in choices})
        if len(protocols) > 1:
            await callback.message.answer(
                prompt + "\n\nاول پروتکل موردنظر را انتخاب کن:",
                reply_markup=vpn_direct_protocol_keyboard(protocols, panel_id),
            )
            return
    await callback.message.answer(
        prompt + "\n\nچندتا از موارد زیر رو انتخاب کن (حداقل یکی)، بعد «✅ ذخیره» رو بزن:",
        reply_markup=vpn_direct_multiselect_keyboard(choices, panel_id, set()),
    )


def _map_done_markup(data: dict, panel_id: int):
    """بعد از ذخیره‌ی نگاشت: اگر مسیر از یک صفحه‌ی مشخص (پلن/پوشه/PAYG/…) شروع شده بود، به همان برمی‌گردد."""
    ret = (data or {}).get("panel_map_return")
    if ret:
        return types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="🔙 بازگشت", callback_data=ret, style="primary")
        ]])
    return admin_vpn_panel_map_menu_keyboard(panel_id)


async def _start_mapping(callback: types.CallbackQuery, state: FSMContext, panel: dict, panel_id: int,
                          scope: str, scope_id: int, prompt: str, ret: str | None = None):
    """قدم اول نگاشت: اگر این نوع پنل حالت «بدون تمپلیت» را هم پشتیبانی کند
    (مرزبان/پاسارگارد)، اول از ادمین می‌پرسد کدام روش را می‌خواهد؛ شاهراه
    (که تمام API‌اش plan-محور است، نه inbound/group-محور) همیشه مستقیم می‌رود
    سراغ لیست بسته‌های خودش، دقیقاً مثل قبل."""
    await state.update_data(panel_map_return=ret)
    if panel.get("panel_type") not in ("marzban", "pasargad", "pasargad_wg"):
        await _open_catalog_picker(callback, state, panel, panel_id, scope, scope_id, prompt)
        return
    await state.update_data(panel_map_scope=scope, panel_map_scope_id=scope_id,
                             panel_map_panel_id=panel_id, panel_map_prompt=prompt)
    await callback.message.answer(
        "🔌 برای این نگاشت می‌خوای از یک تمپلیت آماده‌ی پنل استفاده کنی، یا مستقیم از اینباند/گروه (بدون نیاز به ساختن تمپلیت در پنل)؟",
        reply_markup=vpn_map_mode_keyboard(panel_id, supports_direct=True),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnmapmode|"))
async def vpn_map_mode_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, mode = callback.data.split("|")
    panel_id = int(panel_id_str)
    panel = db.get_vpn_panel(panel_id)
    data = await state.get_data()
    scope = data.get("panel_map_scope")
    scope_id = data.get("panel_map_scope_id")
    prompt = data.get("panel_map_prompt") or f"یک گزینه از {panels.panel_label(panel)} رو انتخاب کن:"
    if not panel or not scope or scope_id is None:
        await callback.answer("❌ این مرحله منقضی شده؛ دوباره از منوی نگاشت شروع کن.", show_alert=True)
        return
    if mode == "direct":
        await _open_direct_picker(callback, state, panel, panel_id, scope, scope_id, prompt)
    else:
        await _open_catalog_picker(callback, state, panel, panel_id, scope, scope_id, prompt)


@router.callback_query(F.data.startswith("vpndirectprotocol|"))
async def vpn_direct_protocol_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id): return
    _, panel_id_str, protocol = callback.data.split("|",2)
    panel_id=int(panel_id_str); data=await state.get_data()
    choices=data.get("panel_map_direct_choices") or []
    if data.get("panel_map_panel_id") != panel_id or not choices:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منوی نگاشت شروع کن.", show_alert=True); return
    if protocol != "all":
        choices=[c for c in choices if str(c.get("protocol") or "unknown")==protocol]
    await state.update_data(panel_map_direct_filtered_choices=choices, panel_map_direct_selected=[], panel_map_direct_protocol=protocol)
    await callback.message.edit_text("🔌 اینباندهای این پروتکل را انتخاب کن (حداقل یکی):", reply_markup=vpn_direct_multiselect_keyboard(choices, panel_id, set()))
    await callback.answer()


@router.callback_query(F.data.startswith("vpndirecttoggle|"))
async def vpn_direct_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, idx_str = callback.data.split("|")
    panel_id, idx = int(panel_id_str), int(idx_str)
    data = await state.get_data()
    choices = data.get("panel_map_direct_filtered_choices") or data.get("panel_map_direct_choices") or []
    if data.get("panel_map_panel_id") != panel_id or not choices:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منو وارد شو.", show_alert=True)
        return
    selected = set(data.get("panel_map_direct_selected") or [])
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await state.update_data(panel_map_direct_selected=list(selected))
    await callback.message.edit_reply_markup(reply_markup=vpn_direct_multiselect_keyboard(choices, panel_id, selected))
    await callback.answer()


@router.callback_query(F.data.startswith("vpndirectall|"))
async def vpn_direct_toggle_all(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    data = await state.get_data()
    choices = data.get("panel_map_direct_filtered_choices") or data.get("panel_map_direct_choices") or []
    if data.get("panel_map_panel_id") != panel_id or not choices:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منو وارد شو.", show_alert=True)
        return
    current = set(data.get("panel_map_direct_selected") or [])
    all_ids = {c["idx"] for c in choices}
    selected = set() if current >= all_ids else all_ids
    await state.update_data(panel_map_direct_selected=list(selected))
    await callback.message.edit_reply_markup(reply_markup=vpn_direct_multiselect_keyboard(choices, panel_id, selected))
    await callback.answer()


@router.callback_query(F.data.startswith("vpndirectconfirm|"))
async def vpn_direct_confirm(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    data = await state.get_data()
    choices = data.get("panel_map_direct_filtered_choices") or data.get("panel_map_direct_choices") or []
    selected_idx = set(data.get("panel_map_direct_selected") or [])
    scope = data.get("panel_map_scope")
    scope_id = data.get("panel_map_scope_id")
    if data.get("panel_map_panel_id") != panel_id or not scope or scope_id is None:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منو وارد شو.", show_alert=True)
        return
    if not selected_idx:
        await callback.answer("❌ حداقل یک مورد رو انتخاب کن.", show_alert=True)
        return

    chosen = [c for c in choices if c["idx"] in selected_idx]
    panel = db.get_vpn_panel(panel_id)
    if panel.get("panel_type") in ("pasargad", "pasargad_wg"):
        payload = {"group_ids": [c["raw_id"] for c in chosen]}
        summary = f"بدون تمپلیت ({len(chosen)} گروه)"
    else:
        inbounds: dict[str, list] = {}
        for c in chosen:
            inbounds.setdefault(c["protocol"], []).append(c["raw_id"])
        payload = {"inbounds": inbounds}
        summary = f"بدون تمپلیت ({len(chosen)} اینباند)"

    remote_ref = "direct:" + json.dumps(payload, ensure_ascii=False)
    db.set_panel_plan_map(scope, int(scope_id), panel_id, remote_ref, summary)

    cleared_note = ""
    if scope == "vip_category":
        db.clear_panel_plan_overrides_for_category(int(scope_id))
        cleared_note = (
            "\n\n♻️ نگاشت‌های اختصاصی قدیمی پلن‌های این دسته (اگر وجود داشت) پاک شد تا این "
            "پیش‌فرض واقعاً روی همه‌ی پلن‌های این دسته اعمال شود."
        )

    await callback.message.edit_text(
        f"✅ ذخیره شد: این بخش از این پس بدون هیچ تمپلیتی، مستقیم با «{summary}» از {panels.panel_label(panel)} ساخته می‌شود.{cleared_note}",
        reply_markup=_map_done_markup(data, panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnmapvipplan|"))
async def vpn_map_vip_plan_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, cat_id_str, plan_id_str = callback.data.split("|")
    panel_id, plan_id = int(panel_id_str), int(plan_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _start_mapping(
        callback, state, panel, panel_id, "vip_plan", plan_id,
        f"یک بسته/تمپلیت از {panels.panel_label(panel)} رو انتخاب کن تا به این پلن مشخص وصل بشه:",
    )


@router.callback_query(F.data.startswith("vpnmapcustom|"))
async def vpn_map_custom_build(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _start_mapping(
        callback, state, panel, panel_id, "custom_build", 0,
        f"🧩 این بسته/تمپلیت، پیش‌فرض همه‌ی سفارش‌های «بساز سرویس خودت» از {panels.panel_label(panel)} خواهد بود. یک بسته انتخاب کن:",
    )


@router.callback_query(F.data.startswith("vpnmappayg|"))
async def vpn_map_payg(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _start_mapping(
        callback, state, panel, panel_id, "payg", 0,
        f"⚡ این بسته/گروه برای همه‌ی سرویس‌های «PAYG» از {panels.panel_label(panel)} استفاده می‌شود. (اگر تنظیم نکنی، از نگاشت «بساز سرویس خودت» استفاده می‌شود.)",
    )


@router.callback_query(F.data.startswith("ftmappick|"))
async def ft_map_pick_panel(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    num = int(callback.data.split("|")[1])
    panels_list = db.list_vpn_panels(enabled_only=True)
    if not panels_list:
        await callback.answer("❌ هیچ پنل فعالی ثبت نشده.", show_alert=True)
        return
    from keyboards import admin_free_test_map_panels_keyboard
    await callback.message.answer("🔌 این پلن تست از کدام پنل ساخته شود؟", reply_markup=admin_free_test_map_panels_keyboard(num, panels_list))
    await callback.answer()


@router.callback_query(F.data.startswith("ftmap|"))
async def ft_map_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, num_s, panel_id_s = callback.data.split("|")
    num, panel_id = int(num_s), int(panel_id_s)
    panel = db.get_vpn_panel(panel_id)
    entry = db.get_free_test_entry_by_num(num)
    if not panel or not entry:
        await callback.answer("❌ پنل یا پلن تست پیدا نشد.", show_alert=True)
        return
    await _start_mapping(
        callback, state, panel, panel_id, "free_test", num,
        f"🧪 بسته/تمپلیت یا گروه ساخت «{entry.get('name')}» را از {panels.panel_label(panel)} انتخاب کن:",
        ret=f"ftpanel_open_{entry.get('id')}",
    )


@router.callback_query(F.data.startswith("vpnmapfreetest|"))
async def vpn_map_free_test(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _start_mapping(
        callback, state, panel, panel_id, "free_test", 0,
        f"🧪 این بسته/تمپلیت برای همه‌ی سفارش‌های «تست رایگان» از {panels.panel_label(panel)} استفاده خواهد شد. یک بسته کوچک انتخاب کن:",
    )


@router.callback_query(F.data.startswith("vpnmapcat|"))
async def vpn_map_cat_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, scope, cat_id_str = callback.data.split("|")
    panel_id, cat_id = int(panel_id_str), int(cat_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _start_mapping(
        callback, state, panel, panel_id, scope, cat_id,
        f"یک بسته/تمپلیت از {panels.panel_label(panel)} رو انتخاب کن تا به این دسته‌بندی وصل بشه:",
    )


@router.callback_query(F.data.startswith("vpncatalogpick|"))
async def vpn_catalog_pick_set(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, idx_str = callback.data.split("|")
    panel_id, idx = int(panel_id_str), int(idx_str)
    data = await state.get_data()
    choices = data.get("panel_map_choices") or []
    scope = data.get("panel_map_scope")
    scope_id = data.get("panel_map_scope_id")
    stored_panel_id = data.get("panel_map_panel_id")
    chosen = next((c for c in choices if c["idx"] == idx), None)
    if not chosen or not scope or scope_id is None or stored_panel_id != panel_id:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منو وارد شو.", show_alert=True)
        return

    db.set_panel_plan_map(scope, int(scope_id), panel_id, chosen["ref"], chosen["name"])

    # fix: اگر این یک نگاشت پیش‌فرض برای کل دسته بود (scope=vip_category)، نگاشت‌های اختصاصی
    # قدیمی‌تر تک‌تک پلن‌های همان دسته را هم پاک می‌کنیم تا پیش‌فرض جدید واقعاً
    # روی همه‌ی پلن‌های دسته اعمال شود (ورنه فقط برای پلن‌های بدون نگاشت اختصاصی).
    cleared_note = ""
    if scope == "vip_category":
        db.clear_panel_plan_overrides_for_category(int(scope_id))
        cleared_note = (
            "\n\n♻️ نگاشت‌های اختصاصی قدیمی پلن‌های این دسته (اگر وجود داشت) پاک شد تا این "
            "پیش‌فرض واقعاً روی همه‌ی پلن‌های این دسته اعمال شود."
        )

    panel = db.get_vpn_panel(panel_id)
    await callback.message.edit_text(
        f"✅ ذخیره شد: این بخش از این پس به «{chosen['name']}» (ref: {chosen['ref']}) از {panels.panel_label(panel)} وصل می‌شود.{cleared_note}",
        reply_markup=_map_done_markup(data, panel_id),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📤 ارسال خودکار بعد از تایید رسید (کارت‌به‌کارت — بر اساس نگاشت ازقبل تعیین‌شده)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("panelsend|"))
async def panel_send_service(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if is_duplicate_action(f"panelsend_{callback.data}"):
        await callback.answer("⚠️ این عملیات چند لحظه پیش انجام شد.", show_alert=True)
        return

    _, uid, plan_key, order_id_str = callback.data.split("|")
    order_id = int(order_id_str) if order_id_str and order_id_str != "0" else None

    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if not plan or not user:
        await callback.answer("❌ کاربر یا پلن یافت نشد.", show_alert=True)
        return

    mapping = db.get_panel_map_for_plan_key(plan_key)
    if not mapping or not mapping.get("enabled"):
        await callback.answer("❌ برای دسته‌بندی این پلن هنوز هیچ پنل فعالی نگاشت نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(mapping["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل نگاشت‌شده پیدا نشد.", show_alert=True)
        return

    await callback.answer(f"⏳ در حال ساخت سرویس در {panels.panel_label(panel)}...")
    username = _username_for_order(uid, order_id)
    ok, link, remote_service_id, data, msg = await panels.create_service(
        panel, username, mapping["remote_ref"], volume_gb=plan.get("volume_gb"), days=plan.get("days"), device_limit=plan.get("user_limit"),
    )
    if not ok:
        await callback.message.answer(
            f"❌ ساخت سرویس در {panels.panel_label(panel)} ناموفق بود: {msg}\n\n"
            f"🔑 مرجع ارسال‌شده: <code>{html.escape(_display_remote_ref(mapping))}</code>\n"
            "اگه این خطا مربوط به پیدا‌نشدن بسته/تمپلیت باشد، از مدیریت همین نمونه پنل دوباره نگاشت کن.",
            parse_mode="HTML",
        )
        return

    if notif_settings.enabled("admin_panel_log"):
        await callback.message.answer(f"📨 پاسخ {panels.panel_label(panel)}:\n<pre>{_pretty(data)}</pre>", parse_mode="HTML")

    snapshot = {"name": plan.get("name"), "volume_gb": plan.get("volume_gb"), "days": plan.get("days")}
    ctx = {"uid": uid, "plan_key": plan_key, "order_id": order_id, "order_kind": "plan",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        await state.update_data(panel_pending_ctx=ctx)
        await state.set_state(AdminStates.waiting_panel_manual_link)
        await callback.message.answer(
            "⚠️ سرویس ساخته شد ولی نتونستم لینک ساب رو خودکار پیدا کنم.\n"
            "لطفاً لینک ساب رو از پاسخ بالا کپی و همینجا ارسال کن:"
        )
        return

    await _deliver_panel_link(callback.bot, ctx, link)


# ---------------------------------------------------------------------------
# 🧩 ساخت خودکار «بساز سرویس خودت» از یک پنل انتخابی (کارت‌به‌کارت)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("panelcustom_"))
async def panel_custom_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.replace("panelcustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ سفارش یافت نشد.", show_alert=True)
        return
    if order.get("order_type") == "renew" and order.get("target_config_id"):
        # سفارش تمدید («بساز سرویس خودت» با کارت‌به‌کارت): برخلاف سفارش‌های جدید،
        # اینجا نباید کیبورد انتخاب پنل/تمپلیت نشون داده بشه و یک سرویس تازه
        # ساخته بشه؛ چون سرویس مقصد (و پنل/سرویس‌آیدی‌اش) از قبل روی همون کانفیگ
        # قبلی (target_config_id) مشخصه. قبلاً این مسیر همیشه کیبورد انتخاب پنل
        # رو نشون می‌داد و به panel_custom_pick می‌رسید که بدون توجه به
        # order_type همیشه panels.create_service (ساخت سرویس تازه) رو صدا
        # می‌زد؛ همین باعث می‌شد تمدیدهایی که ادمین با دکمه‌ی «ساخت خودکار از
        # یک پنل» (بعد از تایید رسید کارت‌به‌کارت) ارسال می‌کرد، به‌جای تمدید
        # سرویس قبلی، یک سرویس کاملاً جدید و جدا بسازند.
        await callback.answer("⏳ در حال تمدید همان سرویس قبلی از داخل پنل...")
        ok, msg = await _fulfill_custom_renew(callback.bot, order, order_id, order["volume_gb"], order["days"])
        await callback.message.answer(msg)
        if not ok:
            await callback.message.answer(
                "برای تمدید این سفارش می‌تونی به‌جاش از دکمه‌ی «📤 شروع ارسال کانفیگ — دستی» "
                "روی پیام اصلی سفارش استفاده کنی."
            )
        return
    # 🧩 اگر برای همین گزینه‌ی سفارشی (یا حالت کلی «بساز سرویس خودت») در تنظیمات پلن/نگاشت پنل،
    # یک پنل فعال نگاشت شده، همان نگاشت مستقیم استفاده می‌شود؛ دیگر لازم نیست ادمین پنل و
    # بسته/تمپلیت را دستی انتخاب کند (دقیقاً مثل ارسال خودکار بعد از پرداخت کیف‌پول/آنلاین).
    _opt = int(order.get("option_num") or 0)
    _mapping = db.get_option_mapping("custom_build", _opt)
    if not _mapping and _opt:
        _mapping = db.get_option_mapping("custom_build", 0)
    _mpanel = db.get_vpn_panel(_mapping["panel_id"]) if (_mapping and _mapping.get("panel_id")) else None
    if _mapping and _mapping.get("enabled") and _mapping.get("remote_ref") and _mpanel and _mpanel.get("enabled"):
        _u = db.get_user_by_id(order["user_id"])
        if not _u:
            await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
            return
        await callback.answer(f"⏳ در حال ساخت سرویس طبق نگاشت در {panels.panel_label(_mpanel)}...")
        _ok = await auto_fulfill_custom_via_panel(
            callback.bot, _u, order_id, order["volume_gb"], order["days"], order.get("custom_name"),
        )
        if _ok:
            await callback.message.answer(f"✅ سفارش #{order_id} طبق نگاشت پلن از {panels.panel_label(_mpanel)} ساخته و ارسال شد.")
        else:
            await callback.message.answer(
                "❌ ساخت خودکار طبق نگاشت ناموفق بود (جزئیات خطا بالا/برای ادمین اصلی ارسال شد).\n"
                "می‌تونی از «📤 شروع ارسال کانفیگ — دستی» استفاده کنی."
            )
        return
    enabled_panels = db.list_vpn_panels(enabled_only=True)
    if not enabled_panels:
        await callback.answer("❌ هیچ پنل فعالی وجود ندارد.", show_alert=True)
        return
    await state.update_data(panel_custom_order_id=order_id)
    buttons = [
        [types.InlineKeyboardButton(
            text=f"{panels.panel_label(p)}", callback_data=f"panelcustompanel|{order_id}|{p['id']}",
         style="primary")]
        for p in enabled_panels
    ]
    await callback.answer()
    await callback.message.answer(
        f"🧩 سفارش «بساز سرویس خودت» #{order_id} — {order['volume_gb']} گیگ / {order['days']} روز\n"
        "یکی از پنل‌های فعال رو انتخاب کن:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("panelcustompanel|"))
async def panel_custom_pick_panel(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, order_id_str, panel_id_str = callback.data.split("|")
    order_id, panel_id = int(order_id_str), int(panel_id_str)
    order = db.get_custom_order(order_id)
    if order and order.get("order_type") == "renew" and order.get("target_config_id"):
        # محافظتی: اگر این کیبورد قبل از رفع باگ بالا برای ادمین ارسال شده و
        # هنوز روی صفحه‌اش باز مونده، دوباره مسیر ساخت سرویس تازه رو طی نکنه.
        await callback.answer("⏳ در حال تمدید همان سرویس قبلی از داخل پنل...")
        ok, msg = await _fulfill_custom_renew(callback.bot, order, order_id, order["volume_gb"], order["days"])
        await callback.message.answer(msg)
        return
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.answer("⏳ در حال دریافت لیست از پنل...")
    choices, msg = await panels.get_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}")
        return
    await state.update_data(panel_custom_order_id=order_id, panel_custom_panel_id=panel_id,
                             panel_custom_choices=choices)
    buttons = [
        [types.InlineKeyboardButton(text=c["label"], callback_data=f"panelcustompick|{order_id}|{panel_id}|{c['idx']}", style="primary")]
        for c in choices
    ]
    await callback.message.answer(
        f"نزدیک‌ترین بسته/تمپلیت از {panels.panel_label(panel)} رو انتخاب کن:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("panelcustompick|"))
async def panel_custom_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, order_id_str, panel_id_str, idx_str = callback.data.split("|")
    order_id, panel_id, idx = int(order_id_str), int(panel_id_str), int(idx_str)
    data_state = await state.get_data()
    choices = data_state.get("panel_custom_choices") or []
    chosen = next((c for c in choices if c["idx"] == idx), None)
    order = db.get_custom_order(order_id)
    panel = db.get_vpn_panel(panel_id)
    if not chosen or not order or not panel:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره تلاش کن.", show_alert=True)
        return
    if order.get("order_type") == "renew" and order.get("target_config_id"):
        # محافظتی: اگر این کیبورد قبل از رفع باگ بالا برای ادمین ارسال شده و
        # هنوز روی صفحه‌اش باز مونده، دوباره مسیر ساخت سرویس تازه رو طی نکنه.
        await callback.answer("⏳ در حال تمدید همان سرویس قبلی از داخل پنل...")
        ok, msg = await _fulfill_custom_renew(callback.bot, order, order_id, order["volume_gb"], order["days"])
        await callback.message.answer(msg)
        return
    user = db.get_user_by_id(order["user_id"])
    if not user:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    await callback.answer(f"⏳ در حال ساخت سرویس در {panels.panel_label(panel)}...")
    ok, link, remote_service_id, data, msg = await _create_custom_service(panel, user["telegram_id"], order, chosen["ref"])
    if not ok:
        await callback.message.answer(
            f"❌ ساخت سروی�� در {panels.panel_label(panel)} ناموفق بود: {msg}\n"
            f"🔑 مرجع ارسال‌شده: <code>{html.escape(chosen['ref'])}</code>",
            parse_mode="HTML",
        )
        return

    if notif_settings.enabled("admin_panel_log"):
        await callback.message.answer(f"📨 پاسخ {panels.panel_label(panel)}:\n<pre>{_pretty(data)}</pre>", parse_mode="HTML")

    snapshot = {"name": order.get("custom_name") or "سرویس سفارشی",
                "volume_gb": order["volume_gb"], "days": order["days"]}
    ctx = {"uid": user["telegram_id"], "plan_key": None, "order_id": order_id, "order_kind": "custom",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        await state.update_data(panel_pending_ctx=ctx)
        await state.set_state(AdminStates.waiting_panel_manual_link)
        await callback.message.answer(
            "⚠️ سرویس ساخته شد ولی نتونستم لینک ساب رو خودکار پیدا کنم.\n"
            "لطفاً لینک ساب رو از پاسخ بالا کپی و همینجا ارسال کن:"
        )
        return

    await _deliver_panel_link(callback.bot, ctx, link)


@router.message(AdminStates.waiting_panel_manual_link)
async def panel_manual_link_received(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک ساب رو با http یا https ارسال کن:")
        return
    data = await state.get_data()
    ctx = data.get("panel_pending_ctx")
    if not ctx:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا دکمه‌ی ارسال خودکار رو دوباره بزن:")
        await state.clear()
        return
    await _deliver_panel_link(message.bot, ctx, link)
    await state.clear()


def _fmt_gb_bytes(total_bytes) -> str:
    try:
        b = float(total_bytes or 0)
    except (TypeError, ValueError):
        b = 0
    if b <= 0:
        return "نامحدود"
    return f"{round(b / (1024 ** 3), 2):g} گیگ"


def _fmt_days_from_expire(expire) -> str:
    try:
        if not expire or int(expire) <= 0:
            return "نامحدود"
        return f"{max(0, days_remaining(expire))} روز"
    except Exception:
        return "نامشخص"


def _renew_done_values(service_name, volume, days, volume_text, days_text, old_known, old_total, old_expire, new_total, new_expire) -> dict:
    """مقادیر قالب «renew_done»: حجم/زمان اولیه، بعد از تمدید و مقدار اضافه‌شده (+ {details} آماده)."""
    added_v = f"{float(volume or 0):g} گیگ" if float(volume or 0) > 0 else "—"
    added_d = f"{int(days or 0)} روز" if int(days or 0) > 0 else "—"
    old_v = _fmt_gb_bytes(old_total) if old_known else "نامشخص"
    old_d = _fmt_days_from_expire(old_expire) if old_known else "نامشخص"
    new_v = _fmt_gb_bytes(new_total) if new_total is not None else volume_text
    new_d = _fmt_days_from_expire(new_expire) if new_expire is not None else days_text
    lines = []
    if float(volume or 0) > 0:
        lines.append(f"📦 حجم اضافه: {added_v}")
    if int(days or 0) > 0:
        lines.append(f"⏳ زمان اضافه: {added_d}")
    lines.append(f"📦 وضعیت فعلی: {volume_text} | {days_text}")
    return {
        "service_name": service_name,
        "added_volume": added_v, "added_days": added_d,
        "current_package": f"{volume_text} | {days_text}",
        "old_volume": old_v, "new_volume": new_v,
        "old_days": old_d, "new_days": new_d,
        "details": "\n".join(lines),
    }


async def _fulfill_custom_renew(bot, order: dict, order_id: int, volume, days) -> tuple[bool, str]:
    """تمدید واقعی همان سرویس، مطابق معماری ربات؛ لینک ساب و service_id حفظ می‌شوند."""
    target_config_id=order.get("target_config_id")
    cfg=db.get_config_by_id(target_config_id) if target_config_id else None
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        try: db.set_custom_order_status(order_id,"paid")
        except Exception: logger.exception("failed to release renew claim %s",order_id)
        return False,"❌ سرویس مقصد برای تمدید یافت نشد یا از پنل ساخته نشده است."
    panel=db.get_vpn_panel(cfg["panel_id"])
    if not panel or not panel.get("enabled"):
        try: db.set_custom_order_status(order_id,"paid")
        except Exception: logger.exception("failed to release renew claim %s",order_id)
        return False,"❌ پنل مربوط به این سرویس یافت نشد یا غیرفعال است."

    # وضعیت قبل از تمدید (برای متغیرهای «حجم/زمان اولیه» در پیام تمدید موفق)
    _old_total=None; _old_expire=None; _old_known=False
    try:
        _ok0,_snap0,_ = await panels.get_service_snapshot(panel,cfg["service_id"])
        if _ok0 and isinstance(_snap0,dict):
            _old_total=_snap0.get("data_limit") if _snap0.get("data_limit") is not None else _snap0.get("total")
            _old_expire=_snap0.get("expire"); _old_known=True
    except Exception:
        logger.exception("خواندن وضعیت قبل از تمدید ناموفق بود")

    ok,snapshot,msg=await panels.renew_existing_service(panel,cfg["service_id"],volume_gb=volume or 0,days=days or 0)
    if not ok:
        try: db.set_custom_order_status(order_id,"paid")
        except Exception: logger.exception("failed to release renew claim %s",order_id)
        return False,f"❌ تمدید از داخل پنل ناموفق بود: {msg}"

    actual_name=(snapshot or {}).get("username") or cfg.get("service_id") or cfg.get("plan","سرویس")
    actual_expire=(snapshot or {}).get("expire")
    if actual_expire:
        try: expiry_date=datetime.fromtimestamp(int(actual_expire)).strftime("%Y-%m-%d")
        except Exception: expiry_date=cfg.get("expiry")
    else: expiry_date=cfg.get("expiry")
    total_bytes=(snapshot or {}).get("total")
    volume_gb_actual=round(total_bytes/(1024**3),2) if total_bytes else float(volume or 0)
    remaining_days_actual=days_remaining(actual_expire) if actual_expire else int(days or 0)
    volume_text,days_text=format_service_package(volume_gb_actual,remaining_days_actual,None)

    # فقط وضعیت واقعی سرویس را همگام کن؛ config/service_id/config link عوض نمی‌شوند.
    db.update_config(target_config_id,cfg.get("plan"),cfg.get("config"),expiry=expiry_date,service_id=cfg.get("service_id"),panel_id=cfg.get("panel_id"))
    db.set_config_referral_funded(target_config_id,db.custom_order_is_referral_funded(order_id))
    user=db.get_user_by_id(cfg["user_id"])
    if user:
        try:
            text, raw_entities = ui_editor.render_template(
                "renew_done",
                _renew_done_values(actual_name, volume, days, volume_text, days_text, _old_known, _old_total, _old_expire, total_bytes, actual_expire),
                fallback=(
                    f"✅ تمدید سرویس «{actual_name}» با موفقیت انجام شد.\n\n"
                    f"📦 حجم اضافه: {float(volume or 0):g} گیگ\n"
                    f"⏳ زمان اضافه: {int(days or 0)} روز\n\n"
                    f"حجم/زمان فعلی: {volume_text} | {days_text}\n"
                    "🔗 لینک ساب شما تغییر نکرده است."
                ),
            )
            from aiogram.types import MessageEntity
            entities=[MessageEntity(type=e["type"], offset=e["offset"], length=e["length"], custom_emoji_id=e.get("custom_emoji_id")) for e in (raw_entities or [])]
            from utils import send_notification_sticker as _sns
            await _sns(bot, int(user["telegram_id"]), "renew_done")
            from utils import send_rich_template as _srt
            _rv = _renew_done_values(actual_name, volume, days, volume_text, days_text, _old_known, _old_total, _old_expire, total_bytes, actual_expire)
            if await _srt(bot, int(user["telegram_id"]), "renew_done", _rv) is None:
                await bot.send_message(int(user["telegram_id"]), text, entities=entities or None, parse_mode=None, _skip_auto_text=True)
        except Exception: logger.exception("ارسال پیام تمدید واقعی به کاربر ناموفق بود")

    db.set_custom_order_status(order_id,"fulfilled")
    try:
        await _log_fulfilled_order(
            bot, user or {"telegram_id":str(order.get("user_id") or ""),"id":order.get("user_id")},
            custom_order_id=order_id, target_config_id=target_config_id, service_id=cfg.get("service_id"),
            service_name=actual_name,
            package_text=f"{volume_text} | {days_text}",
            expiry_text=expiry_date or "نامحدود",
            panel_id=cfg.get("panel_id"), added_volume=float(volume or 0), added_days=int(days or 0)
        )
    except Exception: logger.exception("ثبت لاگ کانال برای تمدید سفارش %s ناموفق بود",order_id)
    return True,"✅ همان سرویس قبلی با موفقیت تمدید شد؛ لینک ساب تغییر نکرد."

def _delivery_time_values(order_kind, order_id) -> dict:
    """متغیرهای زمان ثبت سفارش/تحویل/مدت بین آن‌ها برای قالب «تحویل کانفیگ»."""
    from utils import delivery_time_vars
    created = None
    try:
        if order_id:
            o = db.get_custom_order(order_id) if order_kind == "custom" else db.get_order(order_id)
            created = (o or {}).get("created_at")
    except Exception:
        created = None
    return delivery_time_vars(created)


async def _deliver_panel_link(bot, ctx: dict, link: str):
    """سرویس ساخته‌شده از هر یک از سه نوع پنل را در دیتابیس ذخیره و برای کاربر ارسال می‌کند
    (دقیقاً همان قالب/تجربه‌ی ارسال دستی، فقط بدون نیاز به آپلود دستی عکس/لینک)."""
    uid = ctx["uid"]
    plan_key = ctx.get("plan_key")
    order_id = ctx.get("order_id")
    order_kind = ctx.get("order_kind")
    panel_id = ctx.get("panel_id")
    panel_type = ctx.get("panel_type")
    service_id = ctx.get("service_id")
    snap = ctx.get("snapshot") or {}
    # اگر کلید پلن در ctx نبود ولی سفارش پلن مشخص است، کلید را از خود سفارش بخوان (برای حجم/تعداد کاربر درست)
    if not plan_key and order_kind == "plan" and order_id:
        try: plan_key = (db.get_order(order_id) or {}).get("plan_key") or None
        except Exception: plan_key = None
    _live_ok = False

    user = db.get_user(uid)
    if user is None:
        await bot.send_message(ADMIN_ID, "❌ کاربر یافت نشد؛ سرویس در پنل ساخته شد ولی ارسال نشد.")
        return

    # نام/حجم/انقضا باید از خود پنل خوانده شود؛ مخصوصاً برای شاهراه نام سرویس ممکن است
    # با نام پلن فروشگاه متفاوت باشد.
    if panel_id and service_id:
        panel_obj = db.get_vpn_panel(panel_id)
        if panel_obj:
            ok_live, live, _ = await panels.get_service_snapshot(panel_obj, service_id)
            if ok_live and isinstance(live, dict):
                _live_ok = True
                snap = {**snap, **live}
    # 🐛 «نام کاربری سرویس» باید دقیقاً همان یوزرنیمی باشد که روی پنل ساخته شده؛ قبلاً نام پلن
    # فروشگاه (snap["name"]) جلوتر بود و یوزرنیم واقعی نمایش داده نمی‌شد.
    _plan_label = snap.get("name")
    name = snap.get("username") or ctx.get("username") or (str(service_id) if service_id and ":" not in str(service_id) else None) or _plan_label or "کاربر"
    volume_gb = snap.get("volume_gb")
    if volume_gb is None and snap.get("total"):
        try: volume_gb = snap["total"] / (1024**3)
        except Exception: volume_gb = None
    days = snap.get("days")
    if days is None and snap.get("expire"):
        try: days = max(0, days_remaining(snap["expire"]))
        except Exception: days = None
    if volume_gb is None and _live_ok and not snap.get("total"):
        volume_gb = 0  # پنل حجم ندارد (نامحدود) → «نامحدود»، نه «طبق بسته‌ی انتخابی»
    volume_text, days_text = format_service_package(volume_gb, days, plan_key)
    from subscription import service_users_text as _sut
    _users_t = _sut(plan_key)
    if _users_t == "نامحدود":  # پلن سقف کاربر نداشت/پیدا نشد: اگر خود پنل HWID محدود دارد همان را نشان بده
        try:
            _hl = int(((snap.get("raw") or {}).get("hwid_limit")) or 0)
            if _hl > 0: _users_t = str(_hl)
        except Exception:
            pass
    expiry_date = None
    if snap.get("expire"):
        try: expiry_date = datetime.fromtimestamp(int(snap["expire"])).strftime("%Y-%m-%d")
        except Exception: expiry_date = None
    if not expiry_date and days: expiry_date = (now_tehran_naive() + timedelta(days=days)).strftime("%Y-%m-%d")

    # متن تحویل کانفیگ از ویرایشگر UI می‌آید و Entityهای ذخیره‌شده (از جمله
    # Premium/Custom Emoji) را عیناً حفظ می‌کند. اگر ادمین هنوز چیزی ذخیره
    # نکرده باشد، همان متن پیش‌فرض قبلی استفاده می‌شود.
    delivery_fallback = (
        "✅ سرویس با موفقیت ایجاد شد\n\n"
        f"👤 نام کاربری سرویس : {name}\n"
        "🇺🇳 لوکیشن: مولتی لوکیشن+تانل\n"
        f"⏳ مدت زمان: {days_text}\n"
        f"🗜 حجم سرویس: {volume_text}\n"
        f"👤 تعداد کاربر: {_users_t}\n\n"
        "لینک اتصال:\n"
        f"{link}\n\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید."
    )
    _is_test_delivery = bool(plan_key and db.is_free_test_key(plan_key))
    _deliv_key = "config_delivery_test" if (_is_test_delivery and ui_editor.has_text_override("config_delivery_test")) else "config_delivery"
    _deliv_values = {
        "name": name, "location": "مولتی لوکیشن+تانل",
        "days": days_text, "volume": volume_text, "users": _users_t,
        "sub_link": link,
        "plan_name": _plan_label or name,
        "expiry_date": expiry_date or "—",
        **_delivery_time_values(order_kind, order_id),
    }
    caption, raw_entities = ui_editor.render_template(_deliv_key, _deliv_values, fallback=delivery_fallback)
    from aiogram.types import MessageEntity
    caption_entities = [MessageEntity(
        type=e["type"], offset=e["offset"], length=e["length"],
        custom_emoji_id=e.get("custom_emoji_id")
    ) for e in (raw_entities or [])]

    encrypted = crypto.encrypt_config(link)
    plan_name = f"{name} | {volume_text} | {days_text}"
    config_type = ctx.get("config_type") or (db.plan_type(plan_key) if plan_key else "vip")

    config_id = db.add_config(
        user["id"], plan_name, encrypted, expiry=expiry_date,
        config_type=config_type, service_id=service_id, source=panel_type, panel_id=panel_id,
        plan_key=(plan_key if order_kind == "plan" and plan_key and not (db.is_free_test_key(plan_key)) else None),
    )
    if order_kind == "custom" and order_id:
        try:
            _co = db.get_custom_order(order_id) or {}
            if str(_co.get("order_type") or "new") != "renew":
                db.set_config_custom_option(config_id, int(_co.get("option_num") or 0))
        except Exception:
            logger.warning("ذخیره‌ی گزینه‌ی سرویس سفارشی انجام نشد cfg=%s", config_id, exc_info=True)
    if config_type == "payg":
        db.mark_config_as_payg(config_id, int(ctx.get("payg_price_per_gb") or 0), int(snap.get("used") or 0), paused=False)

    # 🛠 فیکس ریشه‌ای: اگر سفارش با کیف‌پول کاملاً از referral_wallet تأمین شده،
    # حتی در تحویل دستی/لینک دستی هم سرویس باید referral_funded=1 شود.
    # قبلاً این فلگ فقط در مسیر auto_fulfill ست می‌شد و در مسیر دستی جا می‌افتاد.
    if order_kind == "plan" and order_id and db.order_is_referral_funded(int(order_id)):
        db.set_config_referral_funded(config_id, True)
    elif order_kind == "custom" and order_id and db.custom_order_is_referral_funded(int(order_id)):
        db.set_config_referral_funded(config_id, True)

    sent_photo_file_id = None
    _collector = ctx.get("collector")
    if _collector is not None:
        _collector.append({"name": name, "link": link, "volume": volume_text, "days": days_text, "plan_key": plan_key})
    else:
        try:
            from utils import send_notification_sticker as _sns
            await _sns(bot, int(uid), "notif_service_delivery")
            _wg_cfg = config_id if panels.needs_wg_delivery(panel_obj if panel_obj else {"panel_type": panel_type}) else None
            from utils import send_delivery_rich as _sdr
            _rich = await _sdr(bot, int(uid), _deliv_key, _deliv_values,
                               config_delivery_keyboard(bot_info.get("connection_guide_url"), wg_cfg_id=_wg_cfg),
                               _make_qr_bytes(link) if qrcode else None)
            if _rich is not None:
                sent_photo_file_id = _rich[1]
            elif qrcode:
                photo = types.BufferedInputFile(_make_qr_bytes(link), filename="qr.png")
                sent = await bot.send_photo(
                    int(uid), photo, caption=caption, caption_entities=caption_entities or None, reply_markup=config_delivery_keyboard(bot_info.get("connection_guide_url"), wg_cfg_id=_wg_cfg)
                )
                if sent.photo:
                    sent_photo_file_id = sent.photo[-1].file_id
            else:
                await bot.send_message(
                    int(uid), caption, entities=caption_entities or None, parse_mode=None, reply_markup=config_delivery_keyboard(bot_info.get("connection_guide_url"), wg_cfg_id=_wg_cfg)
                )
            if notif_settings.enabled("admin_delivery_report"):
                await bot.send_message(ADMIN_ID, "✅ سرویس به‌صورت خودکار ساخته و برای کاربر ارسال شد.")
        except Exception as e:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ سرویس ساخته و ذخیره شد ولی ارسال پیام به کاربر ناموفق بود: {e}")
            except Exception:
                logger.exception("اطلاع‌رسانی خطای تحویل به ادمین هم ناموفق بود")

    if sent_photo_file_id:
        db.set_config_qr(config_id, sent_photo_file_id)

    if order_kind == "plan" and order_id:
        db.set_order_status(order_id, "fulfilled")
    elif order_kind == "custom" and order_id:
        db.set_custom_order_status(order_id, "fulfilled")

    if True:  # شامل سرویس‌های ساخته‌شده توسط ادمین (admin_manual) هم می‌شود
        # هر سرویس لاگ جدا و مستقل خودش را دارد؛ خطای لاگ نباید تحویل/لاگ سرویس بعدی را خراب کند.
        try:
          await _log_fulfilled_order(
            bot, user,
            plan_order_id=order_id if order_kind == "plan" else None,
            custom_order_id=order_id if order_kind == "custom" else None,
            # 🐛 فیکس: قبلاً target_config_id پاس داده نمی‌شد، پس منطق «گرفتن نام
            # واقعی از خودِ پنل» در _log_fulfilled_order هیچ‌وقت اجرا نمی‌شد و
            # لاگ همیشه اسم پلن (name) را نشان می‌داد، نه یوزرنیم واقعی‌ای که
            # پنل ساخته (مثل businesssvpnbot_39xpsj6h596).
            target_config_id=config_id, service_id=service_id, service_name=name,
            package_text=("PAYG | بدون محدودیت حجم و زمان (پرداخت به‌ازای مصرف)" if order_kind == "payg" else f"{volume_text} | {days_text}"),
            expiry_text=expiry_date or "نامحدود",
            panel_id=panel_id,
            **(
                {"label_override": "⚡ خرید سرویس PAYG", "payment_method": "کیف پول (PAYG)",
                 "amount_text_override": f"{int(ctx.get('payg_price_per_gb') or 0):,} تومان / هر GB مصرف"}
                if order_kind == "payg" else
                ({"label_override": "🛒 خرید جدید", "payment_method": "ساخت توسط ادمین"} if order_kind not in ("plan", "custom") else {})
            ),
        )
        except Exception:
            logger.exception("لاگ تحویل سرویس (کانال/ادمین) ارسال نشد: %s", service_id)


# ---------------------------------------------------------------------------
# 🔁 مدیریت سرویس‌های ساخته‌شده از هر یک از سه نوع پنل (از صفحه‌ی جزئیات سرویس)
# نمونه‌ی پنل هر سرویس از روی configs.panel_id دقیقاً همونی که سرویس روی اون ساخته شده پیدا می‌شود
# (حتی اگر بعداً نمونه‌های تکراری دیگه‌ای از همون نوع اضافه شوند).
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("panelrevoke_"))
async def panel_revoke_sub(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    cfg_id=int(callback.data.replace("panelrevoke_","")); cfg=db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ سرویس پنلی پیدا نشد.",show_alert=True); return
    panel=db.get_vpn_panel(cfg["panel_id"])
    if not panel: await callback.answer("❌ پنل پیدا نشد.",show_alert=True); return
    if panel.get("panel_type")=="shahrah":
        await callback.answer("ℹ️ شاهراه تغییر لینک مستقل بدون تعویض بسته را از این مسیر ارائه نمی‌کند.",show_alert=True); return
    await callback.answer("⏳ در حال باطل‌کردن لینک قبلی و ساخت لینک جدید از خود پنل...")
    ok, link, new_id, data, msg = await panels.regenerate_sub_link(panel, cfg["service_id"])
    if not ok or not link:
        await callback.message.answer(
            f"❌ تغییر لینک انجام نشد. لینک قبلی همچنان در دیتابیس حفظ شده است.\n\n{msg}"
        )
        return

    # فقط وقتی پنل واقعاً لینک جدید داده، لینک ذخیره‌شده را جایگزین می‌کنیم.
    db.replace_config_link(
        cfg_id, crypto.encrypt_config(link),
        new_id if (new_id and new_id != cfg["service_id"]) else None,
    )
    db.set_config_link_disabled(cfg_id, False)
    await callback.message.answer(
        "✅ لینک سرویس با موفقیت تغییر کرد.\n\n"
        "🔒 لینک قبلی توسط خود پنل Revoke و غیرفعال شد.\n"
        "🔗 لینک جدید صادر و در سرویس ذخیره شد.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="🔙 مشاهده سرویس", callback_data=f"svcdetail_{cfg_id}", style="danger")]]
        ),
    )

def _admin_renewal_settings_for_config(cfg:dict)->dict:
    try: plan_key,cid=db.resolve_config_plan(cfg)
    except Exception: plan_key,cid=None,None
    cid=int(cid or 0)
    return bot_info.get_renewal_plan_settings(plan_key,cid) if plan_key else bot_info.get_renewal_settings(cid)

@router.callback_query(F.data.startswith("panelrenew_"))
async def panel_renew_start(callback:types.CallbackQuery,state:FSMContext):
    """تمدید توسط ادمین: از محدودیت‌های «تنظیمات تمدید» مستثناست؛ اول نوع تمدید (حجم/زمان/هردو) پرسیده می‌شود."""
    if not _is_admin(callback.from_user.id): return
    if not db.is_renewal_enabled():
        await callback.answer(db.renew_block_text(), show_alert=True); return
    cfg_id=int(callback.data.replace("panelrenew_","",1)); cfg=db.get_config_by_id(cfg_id) if cfg_id else None
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از پنل VPN ساخته نشده.",show_alert=True); return
    panel=db.get_vpn_panel(cfg["panel_id"])
    if not panel: await callback.answer("❌ پنل این سرویس پیدا نشد.",show_alert=True); return
    await state.clear()
    kb=types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📦 فقط حجم",callback_data=f"panelrenewkind_{cfg_id}_vol",style="primary")],
        [types.InlineKeyboardButton(text="⏳ فقط زمان",callback_data=f"panelrenewkind_{cfg_id}_day",style="primary")],
        [types.InlineKeyboardButton(text="📦⏳ هر دو (حجم و زمان)",callback_data=f"panelrenewkind_{cfg_id}_both",style="success")],
        [types.InlineKeyboardButton(text="🔙 لغو",callback_data=f"panelrenewcancel_{cfg_id}",style="danger")],
    ])
    await callback.message.answer(
        f"🔁 تمدید سرویس «{cfg.get('service_id')}»\n\nادمین از محدودیت‌های تنظیمات تمدید مستثناست و هر مقدار دلخواهی می‌تواند اضافه کند.\nچه چیزی اضافه شود؟",
        reply_markup=kb,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("panelrenewkind_"))
async def panel_renew_kind(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    try:
        _, cfg_id_s, kind = callback.data.split("_", 2)
        cfg_id=int(cfg_id_s)
    except Exception:
        await callback.answer("❌ درخواست نامعتبر.",show_alert=True); return
    if kind not in ("vol","day","both"):
        await callback.answer("❌ درخواست نامعتبر.",show_alert=True); return
    cfg=db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از پنل VPN ساخته نشده.",show_alert=True); return
    await state.update_data(panel_renew_cfg_id=cfg_id,panel_renew_kind=kind,panel_renew_days=None,panel_renew_volume=None)
    cancel=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 بازگشت / لغو",callback_data=f"panelrenewcancel_{cfg_id}",style="danger")]])
    if kind=="vol":
        await state.set_state(AdminStates.waiting_panel_renew_volume); prompt="📦 حجم اضافه را به گیگ وارد کن (مثلاً 10 یا 2.5).\n۰ = حجم نامحدود"
    else:
        await state.set_state(AdminStates.waiting_panel_renew_days); prompt="⏳ تعداد روز اضافه را وارد کن (مثلاً 30).\n۰ = زمان نامحدود"
    if kind=="both": prompt+="\n«-» = این بخش را رد کن (تغییر نده)"
    await callback.message.answer(prompt,reply_markup=cancel); await callback.answer()

def _parse_admin_renew_value(text,integer=False):
    """«-» → 'skip' ، ۰ → 'unl' (نامحدود) ، عدد مثبت → خود عدد ، نامعتبر → None."""
    raw=str(text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩","01234567890123456789")).replace(",",".").replace("٫",".").strip()
    if raw in ("-","—","–"): return "skip"
    try: v=float(raw)
    except ValueError: return None
    if v<0 or v>1_000_000: return None
    if v==0: return "unl"
    if integer:
        return int(v) if v==int(v) else None
    return v

def _parse_admin_renew_number(text,integer=False):
    raw=str(text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩","01234567890123456789")).replace(",",".").replace("٫",".").strip()
    try:
        v=float(raw)
    except ValueError:
        return None
    if v<=0 or v>1_000_000: return None
    if integer:
        return int(v) if v==int(v) else None
    return v

def _renew_cancel_kb(cfg_id):
    return types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 بازگشت / لغو",callback_data=f"panelrenewcancel_{cfg_id}",style="danger")]])

@router.message(AdminStates.waiting_panel_renew_volume)
async def panel_renew_volume_received(message:types.Message,state:FSMContext):
    if not _is_admin(message.from_user.id): return
    data=await state.get_data(); both=data.get("panel_renew_kind")=="both"
    volume=_parse_admin_renew_value(message.text)
    if volume is None or (volume=="skip" and not both):
        await message.answer("❌ یک عدد وارد کن (۰ = نامحدود"+("، «-» = رد کردن" if both else "")+"):",reply_markup=_renew_cancel_kb(data.get("panel_renew_cfg_id"))); return
    await state.update_data(panel_renew_volume=volume)
    if both and data.get("panel_renew_days") is None:
        await state.set_state(AdminStates.waiting_panel_renew_days); await message.answer("⏳ حالا تعداد روز اضافه را وارد کن (۰ = نامحدود، «-» = رد کردن):",reply_markup=_renew_cancel_kb(data.get("panel_renew_cfg_id"))); return
    await _apply_admin_panel_renew(message,state,volume,data.get("panel_renew_days") if data.get("panel_renew_days") is not None else "skip")

@router.message(AdminStates.waiting_panel_renew_days)
async def panel_renew_days_received(message:types.Message,state:FSMContext):
    if not _is_admin(message.from_user.id): return
    data=await state.get_data(); both=data.get("panel_renew_kind")=="both"
    days=_parse_admin_renew_value(message.text,integer=True)
    if days is None or (days=="skip" and not both):
        await message.answer("❌ یک عدد صحیح وارد کن (۰ = نامحدود"+("، «-» = رد کردن" if both else "")+"):",reply_markup=_renew_cancel_kb(data.get("panel_renew_cfg_id"))); return
    await state.update_data(panel_renew_days=days)
    if both and data.get("panel_renew_volume") is None:
        await state.set_state(AdminStates.waiting_panel_renew_volume); await message.answer("📦 حالا حجم اضافه را به گیگ وارد کن (۰ = نامحدود، «-» = رد کردن):",reply_markup=_renew_cancel_kb(data.get("panel_renew_cfg_id"))); return
    await _apply_admin_panel_renew(message,state,data.get("panel_renew_volume") if data.get("panel_renew_volume") is not None else "skip",days)

async def _apply_admin_panel_renew(message:types.Message,state:FSMContext,volume,days):
    data=await state.get_data(); cfg=db.get_config_by_id(data.get("panel_renew_cfg_id")) if data.get("panel_renew_cfg_id") else None
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await message.answer("❌ سرویس دیگر پیدا نشد."); await state.clear(); return
    panel=db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await message.answer("❌ پنل سرویس پیدا نشد."); await state.clear(); return
    vol_unl=(volume=="unl"); days_unl=(days=="unl")
    vol_add=0 if volume in ("skip","unl") else float(volume)
    days_add=0 if days in ("skip","unl") else int(days)
    if volume=="skip" and days=="skip":
        await message.answer("❌ هر دو بخش رد شد؛ چیزی برای تمدید نیست."); await state.clear(); return
    await message.answer("⏳ در حال تمدید همان سرویس از داخل پنل...")
    ok,snapshot,msg=await panels.renew_existing_service(panel,cfg["service_id"],volume_gb=vol_add,days=days_add,volume_unlimited=vol_unl,days_unlimited=days_unl)
    if not ok: await message.answer(f"❌ تمدید ناموفق بود: {msg}"); await state.clear(); return
    expire=(snapshot or {}).get("expire")
    expiry=cfg.get("expiry")
    if days_unl: expiry=None
    elif expire:
        try: expiry=datetime.fromtimestamp(int(expire)).strftime("%Y-%m-%d")
        except Exception: pass
    # لینک، service_id و نام سرویس حفظ می‌شوند؛ فقط expiry همگام می‌شود.
    db.update_config(cfg["id"],cfg.get("plan"),cfg.get("config"),expiry=expiry,service_id=cfg.get("service_id"),panel_id=cfg.get("panel_id"))
    _lines=[f"✅ سرویس «{cfg.get('service_id')}» تمدید شد.",""]
    if vol_unl: _lines.append("📦 حجم: نامحدود شد")
    elif vol_add: _lines.append(f"📦 حجم اضافه: {vol_add:g} گیگ")
    if days_unl: _lines.append("⏳ زمان: نامحدود شد")
    elif days_add: _lines.append(f"⏳ زمان اضافه: {days_add} روز")
    _lines.append("🔗 لینک ساب تغییر نکرد.")
    await message.answer("\n".join(_lines))
    await state.clear()

@router.callback_query(F.data.startswith("panelrenewcancel_"))
async def panel_renew_cancel(callback:types.CallbackQuery,state:FSMContext):
    await state.clear(); await callback.answer("لغو شد."); await callback.message.edit_text("🔙 تمدید لغو شد.")

@router.callback_query(F.data.startswith("paneldisable_"))
async def panel_disable(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    cfg_id = int(callback.data.replace("paneldisable_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از هیچ پنلی ساخته نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل مربوط پیدا نشد.", show_alert=True)
        return
    ok, msg = await panels.disable_service(panel, cfg["service_id"])
    if ok:
        db.set_config_link_disabled(cfg_id, True)
    await callback.answer(f"✅ در {panels.panel_label(panel)} غیرفعال شد." if ok else f"❌ {msg}", show_alert=True)


@router.callback_query(F.data.startswith("panelenable_"))
async def panel_enable(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    cfg_id = int(callback.data.replace("panelenable_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از هیچ پنلی ساخته نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل مربوط پیدا نشد.", show_alert=True)
        return
    ok, msg = await panels.enable_service(panel, cfg["service_id"])
    if ok:
        db.set_config_link_disabled(cfg_id, False)
        try:
            import fair_usage
            if cfg.get("fup_blocked"):
                fair_usage.admin_release(cfg_id)  # ادمین دستی آزاد کرد؛ سهمیه‌ی امروز از همین لحظه
        except Exception:
            pass
    await callback.answer(f"✅ در {panels.panel_label(panel)} فعال شد." if ok else f"❌ {msg}", show_alert=True)


# ---------------------------------------------------------------------------
# 🛠 ایجاد سرویس دستی توسط ادمین (برای خودش)
# مشابه «بساز سرویس خودت» کاربر عادی، ولی:
#   • هیچ قیمتی محاسبه/کم نمی‌شود (رایگان)
#   • هیچ محدودیت حداقل/حداکثر حجم یا روز اعمال نمی‌شود
#   • ادمین آزاد است از هر پنل فعال (نه فقط پنل مپ‌شده‌ی custom_build) و هر
#     تمپلیت/اینباند آن پنل انتخاب کند
#   • سرویس مستقیم به خود ادمین تحویل داده می‌شود (لینک/کانفیگ)
# ---------------------------------------------------------------------------


@router.message(F.text == "🛠 ایجاد سرویس (ادمین)")
async def admin_create_service_start_reply(message: types.Message, state: FSMContext):
    """ورودی Reply Keyboard برای همان wizard ایجاد سرویس دستی."""
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    panels_list = db.list_vpn_panels(enabled_only=True)
    if not panels_list:
        await message.answer("❌ هیچ پنل فعالی ثبت نشده. اول یک پنل اضافه کن.")
        return
    await message.answer(
        "🛠 ایجاد سرویس دستی (رایگان، مخصوص خودت)\n\nاز کدوم پنل بسازم؟ 👇",
        reply_markup=admin_create_service_panel_keyboard(panels_list),
    )

@router.callback_query(F.data == "admin_create_service")
async def admin_create_service_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    panels_list = db.list_vpn_panels(enabled_only=True)
    if not panels_list:
        await callback.answer("❌ هیچ پنل فعالی ثبت نشده. اول یک پنل اضافه کن.", show_alert=True)
        return
    await callback.message.edit_text(
        "🛠 ایجاد سرویس دستی (رایگان، مخصوص خودت)\n\nاز کدوم پنل بسازم؟ 👇",
        reply_markup=admin_create_service_panel_keyboard(panels_list),
    )
    await callback.answer()


async def _adminsvc_target_label(state: FSMContext) -> str:
    data = await state.get_data()
    tid = data.get("adminsvc_target_uid")
    if not tid:
        return ""
    u = db.get_user(tid)
    return f"👤 کاربر: {(u or {}).get('name') or '-'} ({tid})\n\n"


@router.callback_query(F.data.startswith("autosend_"))
async def admin_autosend_start(callback: types.CallbackQuery, state: FSMContext):
    """⚡ ارسال خودکار کانفیگ برای یک کاربر (ساخت روی پنل و تحویل مستقیم به همان کاربر)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("autosend_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    panels_list = db.list_vpn_panels(enabled_only=True)
    if not panels_list:
        await callback.answer("❌ هیچ پنل فعالی ثبت نشده. اول یک پنل اضافه کن.", show_alert=True)
        return
    await state.clear()
    await state.update_data(adminsvc_target_uid=str(user["telegram_id"]))
    await callback.message.answer(
        f"⚡ ارسال خودکار کانفیگ\n\n👤 کاربر: {user['name']} ({user['telegram_id']})\n\nاز کدوم پنل بسازم؟ 👇",
        reply_markup=admin_create_service_panel_keyboard(panels_list),
    )
    await callback.answer()


async def _adminsvc_open_catalog(callback: types.CallbackQuery, state: FSMContext, panel: dict):
    panel_id = panel["id"]
    await callback.answer("⏳ در حال دریافت لیست از پنل...")
    choices, msg = await panels.get_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await state.update_data(adminsvc_panel_id=panel_id, adminsvc_choices=choices)
    await callback.message.answer(
        f"از {panels.panel_label(panel)}، کدوم بسته/تمپلیت/اینباند؟ 👇",
        reply_markup=admin_create_service_catalog_keyboard(choices, panel_id),
    )


@router.callback_query(F.data.startswith("adminsvcpanel_"))
async def admin_create_service_pick_panel(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.replace("adminsvcpanel_", ""))
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    # مرزبان/پاسارگارد: علاوه بر تمپلیت، ساخت مستقیم از گروه/اینباند هم ممکن است.
    if panel.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"):
        await state.update_data(adminsvc_panel_id=panel_id)
        await callback.message.answer(
            "🔌 سرویس با تمپلیت پنل ساخته بشه یا مستقیم با گروه‌های پنل (بدون تمپلیت)؟",
            reply_markup=admin_create_service_mode_keyboard(panel_id),
        )
        await callback.answer()
        return
    await _adminsvc_open_catalog(callback, state, panel)


@router.callback_query(F.data.startswith("adminsvcmode|"))
async def admin_create_service_pick_mode(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, mode = callback.data.split("|")
    panel_id = int(panel_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    if mode != "group":
        await _adminsvc_open_catalog(callback, state, panel)
        return
    await callback.answer("⏳ در حال دریافت گروه‌ها/اینباندهای پنل...")
    choices, msg = await panels.get_direct_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await state.update_data(adminsvc_panel_id=panel_id, adminsvc_group_choices=choices, adminsvc_group_selected=[])
    await callback.message.answer(
        "🗂 یک یا چند گروه/اینباند را انتخاب کن، بعد «➡️ ادامه» را بزن:",
        reply_markup=admin_create_service_group_keyboard(choices, panel_id, set()),
    )


@router.callback_query(F.data.startswith("adminsvcgrp|"))
async def admin_create_service_group_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, idx_str = callback.data.split("|")
    panel_id, idx = int(panel_id_str), int(idx_str)
    data = await state.get_data()
    choices = data.get("adminsvc_group_choices") or []
    if data.get("adminsvc_panel_id") != panel_id or not choices:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره شروع کن.", show_alert=True)
        return
    selected = set(data.get("adminsvc_group_selected") or [])
    selected.symmetric_difference_update({idx})
    await state.update_data(adminsvc_group_selected=list(selected))
    await callback.message.edit_reply_markup(reply_markup=admin_create_service_group_keyboard(choices, panel_id, selected))
    await callback.answer()


@router.callback_query(F.data.startswith("adminsvcgrpall|"))
async def admin_create_service_group_all(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    data = await state.get_data()
    choices = data.get("adminsvc_group_choices") or []
    if data.get("adminsvc_panel_id") != panel_id or not choices:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره شروع کن.", show_alert=True)
        return
    all_ids = {c["idx"] for c in choices}
    selected = set() if set(data.get("adminsvc_group_selected") or []) >= all_ids else all_ids
    await state.update_data(adminsvc_group_selected=list(selected))
    await callback.message.edit_reply_markup(reply_markup=admin_create_service_group_keyboard(choices, panel_id, selected))
    await callback.answer()


@router.callback_query(F.data.startswith("adminsvcgrpok|"))
async def admin_create_service_group_confirm(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    data = await state.get_data()
    choices = data.get("adminsvc_group_choices") or []
    selected = set(data.get("adminsvc_group_selected") or [])
    panel = db.get_vpn_panel(panel_id)
    if data.get("adminsvc_panel_id") != panel_id or not choices or not panel:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره شروع کن.", show_alert=True)
        return
    if not selected:
        await callback.answer("❌ حداقل یک مورد را انتخاب کن.", show_alert=True)
        return
    chosen = [c for c in choices if c["idx"] in selected]
    if panel.get("panel_type") in ("pasargad", "pasargad_wg"):
        payload = {"group_ids": [c["raw_id"] for c in chosen]}
    else:
        inbounds: dict[str, list] = {}
        for c in chosen:
            inbounds.setdefault(c["protocol"], []).append(c["raw_id"])
        payload = {"inbounds": inbounds}
    await state.update_data(
        adminsvc_remote_ref="direct:" + json.dumps(payload, ensure_ascii=False),
        adminsvc_choice_label=f"گروه‌ها ({len(chosen)})",
    )
    await state.set_state(AdminStates.waiting_admin_service_volume)
    await callback.message.answer(_ADMIN_VOL_PROMPT, reply_markup=_adminsvc_nav_keyboard(None))
    await callback.answer()


@router.callback_query(F.data.startswith("adminsvccatalog_"))
async def admin_create_service_pick_catalog(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, idx_str = callback.data.split("_")
    panel_id, idx = int(panel_id_str), int(idx_str)
    data = await state.get_data()
    choices = data.get("adminsvc_choices") or []
    if data.get("adminsvc_panel_id") != panel_id or not choices:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منو وارد شو.", show_alert=True)
        return
    chosen = next((c for c in choices if c["idx"] == idx), None)
    if not chosen:
        await callback.answer("❌ گزینه نامعتبر است.", show_alert=True)
        return
    await state.update_data(adminsvc_remote_ref=chosen["ref"], adminsvc_choice_label=chosen["label"])
    await state.set_state(AdminStates.waiting_admin_service_volume)
    await callback.message.answer(_ADMIN_VOL_PROMPT, reply_markup=_adminsvc_nav_keyboard(None))
    await callback.answer()


_ADMIN_VOL_PROMPT = "📦 حجم سرویس رو به گیگابایت وارد کن.\n۰ یا «-» = نامحدود"
_ADMIN_DAYS_PROMPT = "⏳ مدت اعتبار رو به روز وارد کن.\n۰ یا «-» = نامحدود"
_ADMIN_NAME_PROMPT = "🔤 یک نام (لاتین، بدون فاصله) برای سرویس بفرست، یا فقط «-» بفرست تا خودکار ساخته بشه:"


def _adminsvc_nav_keyboard(back_to: str | None):
    rows = []
    if back_to:
        rows.append(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"adminsvcback|{back_to}", style="primary"))
    rows.append(types.InlineKeyboardButton(text="❌ انصراف", callback_data="adminsvcback|cancel", style="danger"))
    return types.InlineKeyboardMarkup(inline_keyboard=[rows])


def _parse_admin_amount(text, integer=False):
    """عدد ≥ ۰؛ «-» یعنی نامحدود (۰). خروجی None = نامعتبر."""
    raw = str(text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace("٫", ".").replace(",", ".").strip()
    if raw in ("-", "—", "–"):
        return 0
    try:
        v = float(raw)
    except ValueError:
        return None
    if v < 0 or v > 1_000_000:
        return None
    if integer:
        return int(v) if v == int(v) else None
    return v


@router.callback_query(F.data.startswith("adminsvcback|"))
async def admin_create_service_back(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    to = callback.data.split("|", 1)[1]
    if to == "cancel":
        await state.clear()
        try:
            await callback.message.edit_text("🔙 ساخت سرویس لغو شد.")
        except Exception:
            await callback.message.answer("🔙 ساخت سرویس لغو شد.")
        await callback.answer()
        return
    if to == "volume":
        await state.set_state(AdminStates.waiting_admin_service_volume)
        await callback.message.answer(_ADMIN_VOL_PROMPT, reply_markup=_adminsvc_nav_keyboard(None))
    elif to == "days":
        await state.set_state(AdminStates.waiting_admin_service_days)
        await callback.message.answer(_ADMIN_DAYS_PROMPT, reply_markup=_adminsvc_nav_keyboard("volume"))
    await callback.answer()


@router.message(AdminStates.waiting_admin_service_volume)
async def admin_create_service_volume(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    volume = _parse_admin_amount(message.text)
    if volume is None:
        await message.answer("❌ یک عدد (۰ یا «-» = نامحدود) بفرست:", reply_markup=_adminsvc_nav_keyboard(None))
        return
    await state.update_data(adminsvc_volume=volume)
    await state.set_state(AdminStates.waiting_admin_service_days)
    await message.answer(_ADMIN_DAYS_PROMPT, reply_markup=_adminsvc_nav_keyboard("volume"))


@router.message(AdminStates.waiting_admin_service_days)
async def admin_create_service_days(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    days = _parse_admin_amount(message.text, integer=True)
    if days is None:
        await message.answer("❌ یک عدد صحیح (۰ یا «-» = نامحدود) بفرست:", reply_markup=_adminsvc_nav_keyboard("volume"))
        return
    await state.update_data(adminsvc_days=days)
    await state.set_state(AdminStates.waiting_admin_service_name)
    await message.answer(_ADMIN_NAME_PROMPT, reply_markup=_adminsvc_nav_keyboard("days"))


@router.message(AdminStates.waiting_admin_service_name)
async def admin_create_service_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    custom_name = None if raw == "-" else raw
    if custom_name and not _LATIN_NAME_RE.match(custom_name):
        await message.answer("❌ فقط حروف انگلیسی و عدد، بدون فاصله؛ یا «-» برای نام خودکار:", reply_markup=_adminsvc_nav_keyboard("days"))
        return

    data = await state.get_data()
    panel_id = data.get("adminsvc_panel_id")
    remote_ref = data.get("adminsvc_remote_ref")
    volume = data.get("adminsvc_volume")
    days = data.get("adminsvc_days")
    panel = db.get_vpn_panel(panel_id) if panel_id else None
    await state.clear()
    if not panel or remote_ref is None or volume is None or days is None:
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از «🛠 ایجاد سرویس (ادمین)» شروع کن.")
        return

    target_uid = str(data.get("adminsvc_target_uid") or message.from_user.id)
    is_for_user = target_uid != str(message.from_user.id)
    if db.get_user(target_uid) is None:
        if is_for_user:
            await message.answer("❌ کاربر هدف پیدا نشد.")
            return
        db.create_user(message.from_user.id, message.from_user.full_name)

    username = custom_name or _service_username(target_uid)
    await message.answer("⏳ در حال ساخت سرویس روی پنل...")
    ok, link, remote_service_id, resp_data, msg = await panels.create_service(
        panel, username, remote_ref, volume_gb=volume, days=days,
    )
    if not ok:
        await message.answer(f"❌ ساخت سرویس ناموفق بود:\n{msg}")
        return

    snapshot = {"name": custom_name or username, "volume_gb": volume, "days": days}
    ctx = {
        "uid": int(target_uid), "plan_key": None, "order_id": None, "order_kind": "admin_manual",
        "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
        "snapshot": snapshot,
    }
    if not link:
        await message.answer(
            f"⚠️ سرویس در {panels.panel_label(panel)} ساخته شد ولی لینک ساب خودکار پیدا نشد.\n"
            f"پاسخ پنل:\n<pre>{_pretty(resp_data)}</pre>\nلطفاً لینک رو از بالا کپی و همینجا برام بفرست:",
            parse_mode="HTML",
        )
        await state.update_data(panel_pending_ctx=ctx)
        await state.set_state(AdminStates.waiting_panel_manual_link)
        return

    await _deliver_panel_link(message.bot, ctx, link)


# ===========================================================================
# 🔌 گزینه‌های نگاشت چندگانه (PAYG / «بساز سرویس خودت») — از داخل تنظیمات خودشان
# ===========================================================================
def _mopt_kind_ok(kind: str) -> bool:
    return kind in db.MAP_OPTION_KINDS


def _mopt_list_text(kind: str, options: list[dict]) -> str:
    from keyboards import MAP_KIND_TITLES
    title = MAP_KIND_TITLES.get(kind, kind)
    base = (
        f"🔌 نگاشت پنل‌های {title}\n\n"
        "برای هر گزینه یک نام (که کاربر می‌بیند) و یک نگاشت به پنل (تمپلیت/گروه/اینباند) تعریف کن.\n"
        "اگر فقط یک گزینه‌ی فعال باشد کاربر مستقیم به مرحله‌ی بعد می‌رود؛ با چند گزینه، اول لیست نام‌ها را می‌بیند.\n\n"
    )
    if not options:
        return base + "هنوز گزینه‌ای تعریف نشده. با «➕ افزودن» شروع کن."
    return base + "\n".join(f"{'✅' if o['mapped'] else '⚪️'} {o['name']}" for o in options)


@router.callback_query(F.data.startswith("mopt_list|"))
async def mopt_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    kind = callback.data.split("|")[1]
    if not _mopt_kind_ok(kind):
        await callback.answer("❌", show_alert=True); return
    await state.clear()
    from keyboards import admin_map_options_keyboard
    opts = db.get_map_options(kind)
    await callback.message.edit_text(_mopt_list_text(kind, opts), reply_markup=admin_map_options_keyboard(kind, opts))
    await callback.answer()


@router.callback_query(F.data.startswith("mopt_add|"))
async def mopt_add(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    kind = callback.data.split("|")[1]
    if not _mopt_kind_ok(kind):
        return
    await state.set_state(AdminStates.waiting_map_option_name)
    await state.update_data(mopt_kind=kind)
    await callback.message.edit_text("✏️ نام نمادین این گزینه را بفرست (همان که کاربر در لیست می‌بیند؛ مثلاً «اقتصادی» یا «پرسرعت»):")
    await callback.answer()


@router.message(AdminStates.waiting_map_option_name)
async def mopt_add_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    data = await state.get_data()
    kind = data.get("mopt_kind")
    if not name or not kind or not _mopt_kind_ok(kind):
        await message.answer("❌ نام معتبر بفرست:")
        return
    num = db.add_map_option(kind, name)
    await state.clear()
    await message.answer(f"✅ گزینه‌ی «{name}» ساخته شد. حالا پنل آن را انتخاب کن:")
    await _mopt_show_panel_picker(message, kind, num)


async def _mopt_show_panel_picker(target, kind: str, num: int):
    from keyboards import admin_panel_pick_keyboard
    plist = db.list_vpn_panels(enabled_only=True)
    if kind == "payg":
        plist = [p for p in plist if p.get("panel_type") in ("marzban", "pasargad", "pasargad_wg", "threexui")]
    if not plist:
        await target.answer("❌ هیچ پنل فعال (و سازگار) ثبت نشده.")
        return
    await target.answer(
        "🔌 این گزینه از کدام پنل ساخته شود؟",
        reply_markup=admin_panel_pick_keyboard(plist, f"mopt_map|{kind}|{num}|{{pid}}", f"mopt_open|{kind}|{num}"),
    )


@router.callback_query(F.data.startswith("mopt_open|"))
async def mopt_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, kind, num_s = callback.data.split("|")
    num = int(num_s)
    opt = db.get_map_option(kind, num)
    if not opt:
        await callback.answer("❌ گزینه پیدا نشد.", show_alert=True); return
    from keyboards import admin_map_option_detail_keyboard, MAP_KIND_TITLES
    m = db.get_option_mapping(kind, num) if kind != "payg" or num else db.get_panel_plan_map_with_panel("payg", 0)
    if m:
        map_txt = f"✅ {m.get('panel_name') or m.get('panel_id')} → {_display_remote_ref(m)}"
    else:
        map_txt = "⚪️ هنوز نگاشت نشده"
    if kind == "payg":
        pr = db.get_map_option_cfg("payg", num).get("price_per_gb") or int(db.get_payg_settings().get("price_per_gb") or 0)
        extra = f"\n💰 قیمت هر گیگ: {pr:,} تومان" + ("" if db.get_map_option_cfg("payg", num).get("price_per_gb") else " (قیمت اختصاصی تنظیم نشده)")
    else:
        cb = db.get_effective_custom_build_settings(num)
        own = db.get_map_option_cfg("custom_build", num)
        extra = (f"\n💰 هر گیگ: {cb['price_per_gb']:,} | هر ۳۰ روز: {cb['price_per_30_days']:,} تومان"
                 f"\n📦 حجم: {cb['min_gb']}–{cb['max_gb']} گیگ | ⏳ مدت: {cb['min_days']}–{cb['max_days']} روز"
                 + ("" if own else "\n(قیمت/محدوده‌ی اختصاصی تنظیم نشده؛ از پیش‌فرض عمومی)"))
    await callback.message.edit_text(
        f"🔌 {MAP_KIND_TITLES.get(kind, kind)} — «{opt['name']}»\n\n{map_txt}{extra}",
        reply_markup=admin_map_option_detail_keyboard(kind, num, bool(m)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("moptcfg|"))
async def moptcfg_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, kind, num_s, field = callback.data.split("|")
    if kind != "payg" or field != "price_per_gb":
        await callback.answer("❌", show_alert=True); return
    num = int(num_s)
    opt = db.get_map_option(kind, num)
    if not opt:
        await callback.answer("❌ گزینه پیدا نشد.", show_alert=True); return
    await state.set_state(AdminStates.waiting_map_option_cfg)
    await state.update_data(mcfg_kind=kind, mcfg_num=num, mcfg_field=field)
    cur = db.get_map_option_cfg(kind, num).get(field)
    await callback.message.edit_text(
        f"💰 قیمت هر گیگ پلن PAYG «{opt['name']}»\nفعلی: {(f'{cur:,} تومان' if cur else 'تنظیم نشده')}\n\nقیمت جدید را به تومان بفرست (فقط عدد):",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 انصراف", callback_data=f"mopt_open|{kind}|{num}", style="danger")]]),
    )
    await callback.answer()


@router.message(AdminStates.waiting_map_option_cfg)
async def moptcfg_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace(",", "").replace("،", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("❌ فقط عدد صحیح بیشتر از صفر بفرست:")
        return
    data = await state.get_data()
    kind, num, field = data.get("mcfg_kind"), int(data.get("mcfg_num") or 0), data.get("mcfg_field")
    cfg = db.get_map_option_cfg(kind, num)
    cfg[field] = int(raw)
    db.set_map_option_cfg(kind, num, cfg)
    await state.clear()
    from keyboards import admin_map_options_keyboard
    opts = db.get_map_options(kind)
    await message.answer("✅ قیمت ذخیره شد.\n\n" + _mopt_list_text(kind, opts), reply_markup=admin_map_options_keyboard(kind, opts))


@router.callback_query(F.data.startswith("mopt_pick|"))
async def mopt_pick(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    _, kind, num_s = callback.data.split("|")
    await _mopt_show_panel_picker(callback.message, kind, int(num_s))
    await callback.answer()


@router.callback_query(F.data.startswith("mopt_map|"))
async def mopt_map(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, kind, num_s, pid_s = callback.data.split("|")
    num, pid = int(num_s), int(pid_s)
    panel = db.get_vpn_panel(pid)
    opt = db.get_map_option(kind, num)
    if not panel or not opt:
        await callback.answer("❌ پنل یا گزینه پیدا نشد.", show_alert=True); return
    await _start_mapping(
        callback, state, panel, pid, kind, num,
        f"بسته/تمپلیت یا گروه ساخت «{opt['name']}» را از {panels.panel_label(panel)} انتخاب کن:",
        ret=f"mopt_open|{kind}|{num}",
    )


@router.callback_query(F.data.startswith("mopt_ren|"))
async def mopt_rename_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, kind, num_s = callback.data.split("|")
    await state.set_state(AdminStates.waiting_map_option_rename)
    await state.update_data(mopt_kind=kind, mopt_num=int(num_s))
    await callback.message.edit_text("✏️ نام جدید را بفرست:")
    await callback.answer()


@router.message(AdminStates.waiting_map_option_rename)
async def mopt_rename_done(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام معتبر بفرست:"); return
    db.rename_map_option(data["mopt_kind"], int(data["mopt_num"]), name)
    await state.clear()
    from keyboards import admin_map_options_keyboard
    opts = db.get_map_options(data["mopt_kind"])
    await message.answer("✅ نام تغییر کرد.\n\n" + _mopt_list_text(data["mopt_kind"], opts), reply_markup=admin_map_options_keyboard(data["mopt_kind"], opts))


@router.callback_query(F.data.startswith("mopt_del|"))
async def mopt_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    _, kind, num_s = callback.data.split("|")
    db.remove_map_option(kind, int(num_s))
    from keyboards import admin_map_options_keyboard
    opts = db.get_map_options(kind)
    await callback.message.edit_text("🗑 حذف شد.\n\n" + _mopt_list_text(kind, opts), reply_markup=admin_map_options_keyboard(kind, opts))
    await callback.answer()


# ===========================================================================
# 🗂 نگاشت پلن / کل پوشه‌ی VIP — از داخل خود پلن/پوشه (و بلافاصله بعد از ساخت پلن)
# ===========================================================================
def _vmap_target(short: str, ident: int):
    """(scope, back_callback, title) برای پلن ('p') یا پوشه ('c')."""
    if short == "p":
        plan = db.get_vip_plan_by_id(ident)
        if not plan:
            return None
        return "vip_plan", f"vipplan_{plan['plan_key']}", f"پلن «{plan['name']}»"
    cat = db.get_vip_category(ident)
    if not cat:
        return None
    return "vip_category", f"admincat_{cat['key']}", f"کل پوشه‌ی «{cat['name']}»"


async def send_vip_map_picker(target_message, short: str, ident: int, *, intro: str | None = None, skip_label: str | None = None):
    """لیست پنل‌های فعال برای نگاشت پلن/پوشه؛ با دکمه‌ی رد کردن/بازگشت."""
    from keyboards import admin_panel_pick_keyboard
    tgt = _vmap_target(short, ident)
    if not tgt:
        return
    scope, back_cb, title = tgt
    plist = db.list_vpn_panels(enabled_only=True)
    if not plist:
        await target_message.answer("❌ هیچ پنل فعالی ثبت نشده؛ از «🖥 پنل‌های VPN» اضافه کن.", reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="🔙 بازگشت", callback_data=back_cb, style="danger")]]))
        return
    existing = db.get_panel_plan_map(scope, ident)
    extra = []
    if existing:
        extra.append([types.InlineKeyboardButton(text="🗑 حذف نگاشت فعلی", callback_data=f"vmapdel|{short}|{ident}", style="danger")])
    text = intro or f"🔌 {title} از کدام پنل ساخته شود؟"
    if existing:
        text += f"\n\nنگاشت فعلی: ✅ {existing.get('remote_name') or existing.get('remote_ref')}"
    if short == "c":
        text += "\n\nℹ️ این نگاشت روی همه‌ی پلن‌های داخل پوشه اعمال می‌شود (نگاشت‌های اختصاصی قبلی پلن‌ها پاک می‌شوند)."
    kb = admin_panel_pick_keyboard(plist, f"vmapp|{short}|{ident}|{{pid}}", back_cb, extra)
    if skip_label:
        kb.inline_keyboard[-1][0].text = skip_label
    await target_message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("vmap|"))
async def vmap_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    _, short, ident = callback.data.split("|")
    await send_vip_map_picker(callback.message, short, int(ident))
    await callback.answer()


@router.callback_query(F.data.startswith("vmapp|"))
async def vmap_panel_chosen(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, short, ident_s, pid_s = callback.data.split("|")
    ident, pid = int(ident_s), int(pid_s)
    tgt = _vmap_target(short, ident)
    panel = db.get_vpn_panel(pid)
    if not tgt or not panel:
        await callback.answer("❌ پنل یا مقصد پیدا نشد.", show_alert=True); return
    scope, back_cb, title = tgt
    await _start_mapping(
        callback, state, panel, pid, scope, ident,
        f"بسته/تمپلیت یا گروه ساخت {title} را از {panels.panel_label(panel)} انتخاب کن:",
        ret=back_cb,
    )


@router.callback_query(F.data.startswith("vmapdel|"))
async def vmap_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    _, short, ident_s = callback.data.split("|")
    tgt = _vmap_target(short, int(ident_s))
    if not tgt:
        await callback.answer("❌", show_alert=True); return
    db.delete_panel_plan_map(tgt[0], int(ident_s))
    await callback.answer("🗑 نگاشت حذف شد.", show_alert=True)
