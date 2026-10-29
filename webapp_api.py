"""
webapp_api.py
بک‌اند Flask برای Mini App تلگرام. کنار ربات (bot.py) و به‌صورت جدا اجرا می‌شود
و از همان database.py / crypto.py / config.py استفاده می‌کند تا دقیقاً همان
منطق و اعداد ربات تکرار شود (بدون هیچ رقم/قیمت جدید).

=============================================================================
نکات امنیتی مهم (لطفاً قبل از تغییر این فایل بخوانید):
=============================================================================
۱) هرگز به user_id ارسالی از سمت کلاینت (فرانت‌اند) اعتماد نکنید. تنها منبع
   قابل‌اعتماد برای «این درخواست از طرف کدام کاربر تلگرام است»، اعتبارسنجی
   initData با HMAC (تابع validate_init_data) است. هر endpoint حساس باید
   از دکوریتور @require_telegram_auth استفاده کند و از g.tg_user بخواند،
   نه از JSON بدنه‌ی درخواست.
۲) قیمت‌ها همیشه سمت سرور (از روی PLANS / فرمول ساخت سفارشی) محاسبه می‌شوند؛
   هیچ‌وقت قیمتی که کلاینت فرستاده مستقیم مصرف نمی‌شود.
۳) فایل‌های کاربر فقط از مسیرهای مجاز سرویس مدیریت می‌شوند؛
   یا مستقیم به تلگرام پراکسی می‌شوند (رسید) یا از طریق getFile تلگرام
   استریم می‌شوند (کیوآرکد/فایل‌ها) تا هم توکن ربات لو نرود و هم فضای
   دیسک اشغال نشود.
۴) CORS فقط برای دامنه(های) مشخص‌شده در MINIAPP_ORIGIN باز می‌شود، نه '*'.
۵) روی endpointهای مالی (خرید، شارژ کیف پول) یک محدودیت نرخ ساده‌ی
   حافظه‌ای وجود دارد تا اسپم سفارش/رسید برای ادمین ارسال نشود. این
   محدودیت per-process است؛ اگر با چند worker/گونیکورن دیپلوی می‌کنید،
   برای محیط پروداکشن واقعی به Redis مهاجرت کنید.
=============================================================================
"""

import asyncio
import loopsafe
import hashlib
import hmac
import io
import json
import logging
import re
import threading
import time
from functools import wraps
from urllib.parse import parse_qsl

import requests
from flask import Flask, g, jsonify, redirect, request, send_file, send_from_directory

from sentry_setup import init_sentry
try:
    from sentry_sdk.integrations.flask import FlaskIntegration
    init_sentry(extra_integrations=[FlaskIntegration()])
except ImportError:
    init_sentry()

import crypto
import database as db
import bot_info
import notif_settings
from utils import now_tehran_naive, apply_random_amount_variation
import uniquepay_sync
import online_gateways as og
import fsm_storage
import panels
from aiogram import Bot
from handlers.panel_admin import auto_fulfill_vip_via_panel, auto_fulfill_custom_via_panel
from datetime import datetime

from subscription import fetch_subscription_info, extract_configs, days_remaining, is_config_expired
from uniquepay_cfg import enabled as _uq_enabled
import uniquepay_cfg
from config import (
    ADMIN_ID,
    PLANS_INTRO_TEXT,
    REFERRAL_LOCK_AMOUNT,
    REFERRAL_MIN_VOLUME_GB,
    TOKEN,
    FREE_TEST_PLAN_KEY,
    ONLINE_PAYMENT_MIN_AMOUNT,
)

# اجازه‌ی دامنه‌هایی که مجازند به این API درخواست بزنند (comma-separated در .env)
import os

# 🐛 فیکس: مقایسه‌ی Origin قبلاً دقیق (exact string match) بود، یعنی اگر کسی
# در MINIAPP_ORIGIN آدرس را با یک "/" اضافه در انتها ست می‌کرد (مثلاً چون از
# روی آدرس Menu Button بات‌فادر که خودش یک "/" انتهایی دارد کپی شده)، هیچ‌وقت
# با هدر Origin واقعی مرورگر (که هرگز "/" انتهایی ندارد) match نمی‌شد و CORS
# همیشه رد می‌شد؛ این باعث خطای "Failed to fetch" در فرانت‌اند می‌شد بدون هیچ
# پیام خطای واضحی. حالا "/" انتهایی هنگام مقایسه نادیده گرفته می‌شود.
MINIAPP_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("MINIAPP_ORIGIN", "").split(",") if o.strip()]

plan_type = db.plan_type  # نسخه‌ی DB-aware (دسته‌بندی‌های VIP را هم می‌شناسد)

# 🐛 فیکس: اگر این پروسه هیچ‌وقت fsm_storage.storage را مقداردهی نکند، مسیر
# نادر «سرویس در شاهراه ساخته شد ولی لینک ساب خودکار پیدا نشد» (که در
# auto_fulfill_vip_via_panel/_deliver_panel_link از ادمین می‌خواهد لینک را
# دستی پیست کند) اینجا کار نمی‌کند. چون DBStorage مستقیماً روی همان دیتابیس
# اصلی (نه RAM) ذخیره می‌کند، ساختن یک نمونه‌ی جدا از آن در این پروسه هم امن
# است و با پروسه‌ی اصلی ربات (bot.py) هماهنگ می‌ماند.
if fsm_storage.storage is None:
    fsm_storage.storage = fsm_storage.DBStorage()


def _run_async_with_temp_bot(coro_factory):
    """پُل sync→async برای صدا زدن توابع آسنکرون مخصوص ربات aiogram (مثل
    auto_fulfill_vip_via_panel/auto_fulfill_custom_via_panel در
    handlers/panel_admin.py) از داخل این پروسه‌ی Flask سینک. برای هر
    فراخوانی یک نمونه‌ی موقت Bot ساخته می‌شود و در پایان session‌اش بسته
    می‌شود تا نشتی اتصال ایجاد نشود. coro_factory تابعی است که با گرفتن
    نمونه‌ی Bot، یک coroutine برمی‌گرداند. اگر شاهراه غیرفعال باشد یا نگاشتی
    نباشد (یا هر خطای دیگری رخ دهد)، False برمی‌گردد تا مسیر همیشگی اطلاع
    دستی به ادمین دنبال شود و هیچ سفارشی گم نشود."""
    async def _go():
        from tg_temp_bot import TempBot
        tmp_bot = TempBot(token=TOKEN)
        try:
            return await coro_factory(tmp_bot)
        finally:
            await tmp_bot.session.close()

    try:
        return loopsafe.run(_go())
    except Exception:
        logging.getLogger(__name__).exception("خطا در ارسال خودکار از شاهراه (Mini App)")
        return False

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
_LATIN_NAME_RE = re.compile(r"^[A-Za-z0-9]+$")

app = Flask(__name__)
try:
    import adv_reports as _adv_reports
    _adv_reports.install_error_handler()
except Exception:
    pass
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # حداکثر ۱۰ مگابایت آپلود


# =============================================================================
# اعتبارسنجی Telegram WebApp initData (رسمی، طبق مستندات تلگرام)
# =============================================================================
# حداکثر عمر initData؛ تلگرام همان initData اولیه را تا بسته‌شدن مینی‌اپ نگه می‌دارد و اگر کاربر
# مینی‌اپ را چند روز باز بگذارد (یا تلگرام صفحه را کش کند)، با حد ۲۴ ساعته «invalid_auth» می‌گرفت.
INITDATA_MAX_AGE_SECONDS = int(os.environ.get("MINIAPP_INITDATA_MAX_AGE", str(3 * 86400)) or 3 * 86400)


def validate_init_data_ex(init_data: str, bot_token: str, max_age_seconds: int | None = None) -> tuple[dict | None, str]:
    """initData مینی‌اپ را بررسی می‌کند و علت دقیق رد شدن را هم برمی‌گرداند
    (missing / malformed / no_hash / bad_hash / no_auth_date / expired / no_user)."""
    if max_age_seconds is None:
        max_age_seconds = INITDATA_MAX_AGE_SECONDS
    if not init_data:
        return None, "missing"
    try:
        # 🐛 فیکس: keep_blank_values=True — قبلاً فیلدهای خالی (مثلاً start_param=) از data_check_string
        # حذف می‌شدند و هش هیچ‌وقت مطابقت نمی‌کرد (invalid_auth).
        parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None, "malformed"

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None, "no_hash"

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None, "bad_hash"

    auth_date = parsed.get("auth_date")
    if not auth_date:
        return None, "no_auth_date"
    try:
        if time.time() - int(auth_date) > max_age_seconds:
            return None, "expired"
    except ValueError:
        return None, "no_auth_date"

    user_json = parsed.get("user")
    if not user_json:
        return None, "no_user"
    try:
        return json.loads(user_json), "ok"
    except json.JSONDecodeError:
        return None, "no_user"


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int | None = None) -> dict | None:
    return validate_init_data_ex(init_data, bot_token, max_age_seconds)[0]


# =============================================================================
# بررسی عضویت اجباری در کانال‌ها (معادل sync همان check_membership در
# handlers/start.py)
# 🐛 فیکس: قبلاً Mini App اصلاً این شرط را چک نمی‌کرد و کاربری که هیچ‌وقت وارد
# ربات نشده و در کانال‌های اجباری عضو نشده، می‌توانست مستقیماً از این‌جا خرید
# کند و به همه‌ی امکانات دسترسی داشته باشد — یکی از قوانین کسب‌وکاری اصلی
# پروژه به‌طور کامل bypass می‌شد. حالا همان قانون این‌جا هم اعمال می‌شود.
# =============================================================================
def _check_membership_sync(user_id: int) -> list[dict]:
    not_joined = []
    for ch in bot_info.get_required_channels():
        try:
            resp = requests.get(
                f"{TELEGRAM_API}/getChatMember",
                params={"chat_id": ch["id"], "user_id": user_id},
                timeout=8,
            )
            data = resp.json()
            status = data.get("result", {}).get("status") if data.get("ok") else None
            if status not in ("member", "administrator", "creator"):
                not_joined.append(ch)
        except Exception:
            # مثل نسخه‌ی ربات: در صورت خطا (کوتاهی شبکه، ربات ادمین کانال
            # نیست و...) fail-closed عمل می‌کنیم، یعنی «عضو نشده» فرض می‌کنیم.
            not_joined.append(ch)
    return not_joined


def _channel_public_url(c: dict) -> str:
    """لینک قابل‌کلیک کانال: لینک ثبت‌شده (نرمال‌شده: @user / t.me/... بدون https) یا، اگر خالی بود، از @username آیدی."""
    url = str(c.get("url") or "").strip()
    if url.startswith("@"):
        url = "https://t.me/" + url[1:]
    elif url and not url.lower().startswith(("http://", "https://", "tg://")):
        url = "https://" + url.lstrip("/")
    if not url:
        cid = str(c.get("id") or "").strip()
        if cid.startswith("@"):
            url = "https://t.me/" + cid[1:]
        elif cid and not cid.lstrip("-").isdigit():
            url = "https://t.me/" + cid
    return url


def _serialize_channels(channels: list[dict]) -> list[dict]:
    return [{"name": c.get("name") or c.get("id"), "url": _channel_public_url(c)} for c in channels]


def require_telegram_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        init_data = auth_header[4:] if auth_header.startswith("tma ") else request.headers.get("X-Telegram-Init-Data", "")

        tg_user, _reason = validate_init_data_ex(init_data, TOKEN)
        if tg_user is None or "id" not in tg_user:
            # reason فقط برای عیب‌یابی است (missing = مینی‌اپ بدون initData باز شده، bad_hash = توکن ربات
            # با توکن سرور نمی‌خواند، expired = initData قدیمی)
            return jsonify({"error": "invalid_auth", "reason": _reason}), 401

        g.tg_user = tg_user

        if bot_info.get_required_channels():
            not_joined = _check_membership_sync(tg_user["id"])
            if not_joined:
                return jsonify({
                    "error": "channel_join_required",
                    "channels": _serialize_channels(not_joined),
                }), 403

        g.user = db.create_user(tg_user["id"], tg_user.get("first_name") or tg_user.get("username") or "کاربر")
        if g.user.get("is_blocked"):
            return jsonify({"error": "user_blocked"}), 403
        return f(*args, **kwargs)

    return wrapper


def require_admin_api(f):
    @wraps(f)
    @require_telegram_auth
    def wrapper(*args, **kwargs):
        user_id = g.tg_user.get("id")
        if user_id != ADMIN_ID and not db.is_sub_admin(user_id):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)

    return wrapper


def require_reseller_api(f):
    @wraps(f)
    @require_telegram_auth
    def wrapper(*args, **kwargs):
        agent = db.get_agent(g.tg_user.get("id"))
        if not agent or int(agent.get("vip_discount_percent") or 0) <= 0:
            return jsonify({"error": "forbidden"}), 403
        g.agent = agent
        return f(*args, **kwargs)

    return wrapper




# =============================================================================
# Admin Role Check برای Mini App
# =============================================================================
@app.route("/api/role", methods=["GET"])
@require_telegram_auth
def api_get_role():
    """بررسی نقش کاربر (admin / user) برای Mini App.
    فرانت‌اند بر اساس این endpoint تصمیم می‌گیرد کدام UI را نمایش بدهد.
    امنیت: اعتبارسنجی با HMAC توسط @require_telegram_auth انجام شده است.
    """
    user_id = g.tg_user.get("id")
    is_admin_user = (user_id == ADMIN_ID) or db.is_sub_admin(user_id)
    agent = db.get_agent(user_id) if not is_admin_user else None
    is_reseller_user = bool(agent and int(agent.get("vip_discount_percent") or 0) > 0)
    role = "admin" if is_admin_user else ("reseller" if is_reseller_user else "user")
    return jsonify({
        "role": role,
        "is_admin": is_admin_user,
        "is_reseller": is_reseller_user,
        "user_id": user_id,
    })

@app.route("/api/membership/status", methods=["GET"])
def api_membership_status():
    """endpoint سبک (بدون نیاز به ساخت کاربر) که فرانت‌اند مینی‌اپ قبل از هر
    درخواست دیگری صدا می‌زند تا اگر کاربر در کانال‌های اجباری عضو نبود، صفحه‌ی
    «عضویت در کانال» را نشان دهد، نه این‌که با یک خطای عمومی ۴۰۳ مواجه شود."""
    auth_header = request.headers.get("Authorization", "")
    init_data = auth_header[4:] if auth_header.startswith("tma ") else request.headers.get("X-Telegram-Init-Data", "")
    tg_user, _reason = validate_init_data_ex(init_data, TOKEN)
    if tg_user is None or "id" not in tg_user:
        return jsonify({"error": "invalid_auth", "reason": _reason}), 401

    not_joined = _check_membership_sync(tg_user["id"]) if bot_info.get_required_channels() else []
    return jsonify({"joined": not not_joined, "channels": _serialize_channels(not_joined)})


# =============================================================================
# 🐛 فیکس: محدودیت نرخ قبلاً فقط در حافظه‌ی همین پروسه ذخیره می‌شد (با ری‌استارت
# صفر می‌شد و بین Workerها/پروسه‌های متعدد مشترک نیست). حالا حذف شده و به
# db.consume_api_rate_limit جایگزین شده که دیتابیسی و مشترک بین تمام پروسه/Workerهاست.
# =============================================================================


def rate_limit(max_calls: int, period_seconds: int):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = f"{f.__name__}:{g.tg_user['id']}"
            if not db.consume_api_rate_limit(key, max_calls, period_seconds):
                return jsonify({"error": "rate_limited"}), 429
            return f(*args, **kwargs)

        return wrapper

    return decorator


# =============================================================================
# CORS (فقط برای دامنه‌های مجاز، نه '*')
# 🐛 فیکس: نسخه‌ی قبلی اگر متغیر محیطی MINIAPP_ORIGIN تنظیم نمی‌شد (که هیچ
# خطای startup هم نمی‌داد)، به‌طور خاموش هر Origin ای را مجاز می‌کرد
# («or not MINIAPP_ORIGINS» یعنی وقتی لیست خالی است، شرط همیشه True می‌شود)؛
# دقیقاً برخلاف چیزی که در کامنت بالای فایل ادعا شده («نه '*'»). حالا اگر
# تنظیم نشده باشد، پیش‌فرض امن «رد کردن» است، نه «قبول همه».
# =============================================================================
if not MINIAPP_ORIGINS:
    logging.getLogger(__name__).warning(
        "MINIAPP_ORIGIN تنظیم نشده است؛ درخواست‌های cross-origin به این API رد "
        "خواهند شد (CORS بسته است). آدرس دامنه‌ی Mini App خودتان را در .env با "
        "MINIAPP_ORIGIN=https://your-miniapp-domain ست کنید."
    )


@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin")
    allowed = list(MINIAPP_ORIGINS)
    try:
        # آدرسی که ادمین از داخل ربات ثبت کرده هم مجاز است؛ نیازی به ست‌کردن دستی env نیست.
        from urllib.parse import urlparse as _urlparse
        _saved = (db.get_setting("miniapp_webapp_url", "") or "").strip()
        if _saved:
            _p = _urlparse(_saved)
            if _p.scheme and _p.netloc:
                allowed.append(f"{_p.scheme}://{_p.netloc}")
    except Exception:
        pass
    if origin and origin.rstrip("/") in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, X-Telegram-Init-Data, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return "", 204


# =============================================================================
# کمک‌کننده‌های تماس مستقیم با Telegram Bot API (بدون وابستگی به aiogram،
# چون این فایل sync است و جدا از حلقه‌ی asyncio ربات اجرا می‌شود)
# =============================================================================
def tg_send_sticker_for(chat_id, sticker_key):
    """اگر ادمین برای sticker_key استیکر فعال تنظیم کرده باشد همان را می‌فرستد (بی‌صدا در خطا)."""
    try:
        row = db.get_section_sticker(sticker_key)
        if not row or not row.get("is_enabled") or not row.get("file_id"):
            return False
        r = requests.post(f"{TELEGRAM_API}/sendSticker", json={"chat_id": chat_id, "sticker": row["file_id"]}, timeout=15)
        return bool(r.ok)
    except Exception:
        return False


def tg_notify_user(chat_id, sticker_key, text, reply_markup=None):
    """اعلان کاربر: اول استیکر (اگر هست)، بعد پیام."""
    if sticker_key:
        tg_send_sticker_for(chat_id, sticker_key)
    if text:
        return tg_send_message(chat_id, text, reply_markup=reply_markup)
    return False


def _is_staff_chat(chat_id) -> bool:
    try:
        if int(chat_id) == int(ADMIN_ID):
            return True
        return bool(db.is_sub_admin(str(chat_id)))
    except Exception:
        return False


def _tg_editor_decorate(chat_id, text, parse_mode):
    """پیام‌های مینی‌اپ (اعلان شارژ/پرداخت و …) مستقیم با HTTP فرستاده می‌شوند و از Bot.__call__ رد نمی‌شوند؛
    پس ایموجی پریمیوم / ریچ‌تکستِ «ویرایش متن‌ها» را همین‌جا اعمال می‌کنیم.
    خروجی: (text, entities|None, rich_payload|None)"""
    if parse_mode or not text:
        return text, None, None
    try:
        if _is_staff_chat(chat_id):
            return text, None, None
        import ui_editor
        rich = ui_editor.rich_text_match(str(text))
        if rich:
            return text, None, rich
        new_text, ents = ui_editor.apply_auto_text_with_entities(str(text))
        if ents:
            return new_text, [
                {k: v for k, v in e.items() if k in ("type", "offset", "length", "url", "language", "custom_emoji_id") and v is not None}
                for e in ents
            ], None
        return new_text, None, None
    except Exception:
        logging.getLogger(__name__).debug("tg editor decorate failed", exc_info=True)
        return text, None, None


def _tg_send_rich(chat_id, rich_payload, reply_markup=None) -> bool:
    try:
        import rich_message
        inp = rich_message.to_input(rich_payload).model_dump(mode="json", exclude_none=True)
        body = {"chat_id": chat_id, "rich_message": inp}
        if reply_markup:
            body["reply_markup"] = reply_markup
        r = requests.post(f"{TELEGRAM_API}/sendRichMessage", json=body, timeout=15)
        return bool(r.ok and r.json().get("ok"))
    except Exception:
        logging.getLogger(__name__).debug("tg rich send failed", exc_info=True)
        return False


def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    text, _entities, _rich = _tg_editor_decorate(chat_id, text, parse_mode)
    if _rich and _tg_send_rich(chat_id, _rich, reply_markup=reply_markup):
        return True
    payload = {"chat_id": chat_id, "text": text}
    if _entities:
        payload["entities"] = _entities
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        if _entities:
            _r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
            if _r.ok and _r.json().get("ok"):
                return True
            # تلگرام entity را نپذیرفت؛ یک‌بار بدون آن‌ها (پیام هرگز گم نشود)
            payload.pop("entities", None)
        response = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram sendMessage failed")
        return True
    except requests.RequestException as e:
        logging.getLogger(__name__).warning("tg_send_message failed: %s", e)
    except (ValueError, RuntimeError) as e:
        logging.getLogger(__name__).warning("tg_send_message rejected: %s", e)
    return False


def tg_send_photo_bytes(chat_id, file_bytes, filename, caption=None, reply_markup=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"photo": (filename, file_bytes)}
    try:
        response = requests.post(f"{TELEGRAM_API}/sendPhoto", data=data, files=files, timeout=30)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description") or "Telegram sendPhoto failed")
        # file_id عکس را برمی‌گردانیم تا رسید در «صف درخواست‌ها» هم قابل‌مشاهده باشد (truthy = موفق)
        try:
            return (result.get("result", {}).get("photo") or [{}])[-1].get("file_id") or True
        except Exception:
            return True
    except requests.RequestException as e:
        logging.getLogger(__name__).warning("tg_send_photo_bytes failed: %s", e)
    except (ValueError, RuntimeError) as e:
        logging.getLogger(__name__).warning("tg_send_photo_bytes rejected: %s", e)
    return False


def _valid_receipt_photo(photo) -> bool:
    if photo is None:
        return False
    mime = (photo.mimetype or "").lower()
    return mime in {"image/jpeg", "image/png", "image/webp"}


def tg_get_file_bytes(file_id: str):
    """فایل را از تلگرام می‌گیرد و بایت خامش را برمی‌گرداند (بدون افشای توکن به کلاینت)."""
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        r2 = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=30)
        r2.raise_for_status()
        return r2.content
    except (requests.RequestException, KeyError):
        return None


def _kb_json(markup):
    """InlineKeyboardMarkup (aiogram) → dict برای Bot API خام؛ تا کیبوردهای ادمین دقیقاً مثل ربات باشند."""
    return markup.model_dump(exclude_none=True)


def inline_kb(*rows):
    """rows: هر سطر لیستی از (text, callback_data)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]}


def _calc_custom_price(volume_gb: int, days: int, telegram_id=None, option_num: int | None = None) -> tuple[int, bool]:
    """مثل نسخه‌ی ربات کلاسیک: اگر کاربر نماینده باشد، تخفیف نمایندگی‌اش
    روی قیمت «بساز سرویس خودت» هم اعمال می‌شود. قیمت/محدوده از «گزینه‌ی نگاشت» انتخابی."""
    _cb = db.get_effective_custom_build_settings(option_num)
    price = volume_gb * _cb["price_per_gb"] + (days / 30) * _cb["price_per_30_days"]
    discount_applied = False
    if telegram_id is not None:
        _pct = db.agent_percent_for_option(telegram_id, "custom_build", option_num)
        if db.get_agent(telegram_id) and _pct > 0:
            price = price * (1 - _pct / 100)
            discount_applied = True
    return int(round(price)), discount_applied


def _serialize_plan(key, plan):
    return {
        "key": key,
        "name": plan["name"],
        "price": plan["price"],
        "days": plan.get("days", 0),
        "volume_gb": plan.get("volume_gb", 0),
        "type": plan_type(key),
        "duration_unit": plan.get("duration_unit", "days"),
        "duration_value": plan.get("duration_value", plan.get("days", 0)),
    }


# =============================================================================
# پروفایل / کاربر
# =============================================================================
@app.route("/api/me", methods=["GET"])
@require_telegram_auth
def api_me():
    u = g.user
    configs_count = len(db.get_configs(u["id"]))
    agent = db.get_agent(g.tg_user["id"])
    is_admin_user = (g.tg_user.get("id") == ADMIN_ID) or db.is_sub_admin(g.tg_user.get("id"))
    is_reseller_user = bool(agent and int(agent.get("vip_discount_percent") or 0) > 0)
    return jsonify({
        "name": u["name"],
        "username": g.tg_user.get("username"),
        "telegram_id": u["telegram_id"],
        "wallet": u["wallet"],
        "locked_wallet": u["locked_wallet"],
        "total_purchase": u["total_purchase"],
        "joined": u["joined"],
        "invited_count": u["invited_count"],
        "successful_invites": u["successful_invites"],
        "services_count": configs_count,
        "orders_enabled": db.is_orders_enabled(),
        "agent_discount_percent": agent["vip_discount_percent"] if agent else 0,
        "is_agent": bool(agent),
        "referral_enabled": db.referral_enabled(),
        "config_name_mode": bot_info.get_config_name_mode(),
        "is_admin": is_admin_user,
        "is_reseller": is_reseller_user,
    })


# =============================================================================
# پلن‌ها — VIP حالا دسته‌بندی‌شده (همان دسته‌بندی‌های ساخته‌شده در پنل ادمین)،
# =============================================================================
@app.route("/api/plans", methods=["GET"])
@require_telegram_auth
def api_plans():
    agent = db.get_agent(g.tg_user["id"])
    is_admin_user = (g.tg_user.get("id") == ADMIN_ID) or db.is_sub_admin(g.tg_user.get("id"))
    is_reseller_user = bool(agent and int(agent.get("vip_discount_percent") or 0) > 0)
    vip_categories = []
    for cat in db.get_vip_categories():
        plans = db.get_vip_plans(cat["id"])
        is_root = db.is_vip_root_bucket(cat)
        # پوشه‌ای که فقط زیرپوشه دارد و پلن مستقیم ندارد، در مینی‌اپ دسته‌ی خالی ساخته نکند.
        if not plans and (is_root or db.get_child_vip_categories(cat["id"])):
            continue
        # نام کامل مسیر برای زیرپوشه‌ها («پوشه › زیرپوشه»)
        if is_root:
            display_name = "سایر پلن‌ها"
        else:
            display_name = " › ".join(c["name"] for c in db.get_vip_category_path(cat["id"])) or cat["name"]
        vip_categories.append({
            "key": cat["key"],
            "name": display_name,
            "plans": [_serialize_plan(p["plan_key"], p) for p in plans],
        })

    return jsonify({
        "vip_categories": vip_categories,
        "orders_enabled": db.is_orders_enabled(),
        "agent_discount_percent": agent["vip_discount_percent"] if agent else 0,
        "is_agent": bool(agent),
        "referral_enabled": db.referral_enabled(),
        "config_name_mode": bot_info.get_config_name_mode(),
        "is_admin": is_admin_user,
        "is_reseller": is_reseller_user,
        "online_payment_enabled": _uq_enabled(),
        "online_gateways": [{"key": k, "title": og.title(k), "min_amount": og.min_amount(k)} for k in og.enabled_gateways()],
        "intro_text": PLANS_INTRO_TEXT,
    })


# =============================================================================
# Admin Mini App APIs
# =============================================================================
@app.route("/admin/stats", methods=["GET"])
@require_admin_api
def api_admin_stats():
    conn = db.get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    total_users = int(cur.fetchone()[0])

    today = now_tehran_naive().date().isoformat()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE substr(joined, 1, 10) = ?", (today,))
    new_users_today = int(cur.fetchone()[0])

    try:
        active_users_today = int(db.count_active_users(1) or 0)
    except Exception:
        active_users_today = 0

    cur.execute("SELECT COUNT(*) AS c FROM orders")
    total_orders = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) AS c FROM orders WHERE substr(created_at, 1, 10) = ?", (today,))
    orders_today = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) AS c FROM orders WHERE status = 'pending'")
    pending_orders = int(cur.fetchone()[0])

    cur.execute("SELECT COUNT(*) AS c FROM configs WHERE deleted = 0")
    active_services = int(cur.fetchone()[0])

    cur.execute("SELECT COALESCE(SUM(price), 0) AS s FROM orders WHERE status IN ('completed', 'active', 'paid', 'processing')")
    revenue_total = int(cur.fetchone()[0] or 0)
    cur.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM orders WHERE status IN ('completed', 'active', 'paid', 'processing') AND substr(created_at, 1, 10) = ?",
        (today,),
    )
    revenue_today = int(cur.fetchone()[0] or 0)

    try:
        open_tickets = int(db.count_tickets("open") or 0)
    except Exception:
        open_tickets = 0

    return jsonify({
        "total_users": total_users,
        "active_users_today": active_users_today,
        "new_users_today": new_users_today,
        "total_orders": total_orders,
        "orders_today": orders_today,
        "active_services": active_services,
        "pending_orders": pending_orders,
        "revenue_today": revenue_today,
        "revenue_total": revenue_total,
        "open_tickets": open_tickets,
    })


@app.route("/admin/orders/recent", methods=["GET"])
@require_admin_api
def api_admin_recent_orders():
    cur = db.get_connection().cursor()
    cur.execute(
        """
        SELECT o.id, o.user_id, u.name AS user_name, o.plan_name, o.price, o.status, o.created_at
        FROM orders o
        LEFT JOIN users u ON u.id = o.user_id
        ORDER BY o.id DESC
        LIMIT 20
        """
    )
    rows = cur.fetchall() or []
    return jsonify([
        {
            "id": int(row[0]),
            "user_id": int(row[1]) if row[1] is not None else None,
            "user_name": row[2],
            "plan_name": row[3],
            "amount": int(row[4] or 0),
            "status": row[5] or "pending",
            "created_at": row[6],
        }
        for row in rows
    ])


@app.route("/admin/user/<query>", methods=["GET"])
@require_admin_api
def api_admin_user_search(query: str):
    q = (query or "").strip()
    user = None
    if q.isdigit():
        user = db.get_user_by_id(int(q)) or db.get_user(q)
    if user is None and q:
        matches = db.search_users_multi(q, limit=1)
        user = matches[0] if matches else None
    if user is None:
        return jsonify({"error": "کاربر یافت نشد"}), 404
    return jsonify({
        "id": int(user["id"]),
        "telegram_id": user["telegram_id"],
        "name": user.get("name"),
        "wallet": int(user.get("wallet") or 0),
        "locked_wallet": int(user.get("locked_wallet") or 0),
        "total_purchase": int(user.get("total_purchase") or 0),
        "is_blocked": bool(user.get("is_blocked")),
        "is_vip": bool((user.get("total_purchase") or 0) > 0),
        "invite_code": user.get("invite_code"),
    })


@app.route("/admin/user/toggle-block", methods=["POST"])
@require_admin_api
def api_admin_user_toggle_block():
    body = request.get_json(silent=True) or {}
    user_id = int(body.get("user_id") or 0)
    user = db.get_user_by_id(user_id)
    if user is None:
        return jsonify({"error": "not_found"}), 404
    new_blocked = not bool(user.get("is_blocked"))
    db.set_user_blocked(user["telegram_id"], new_blocked)
    return jsonify({"success": True, "is_blocked": new_blocked})


@app.route("/admin/user/add-wallet", methods=["POST"])
@require_admin_api
def api_admin_user_add_wallet():
    body = request.get_json(silent=True) or {}
    user_id = int(body.get("user_id") or 0)
    amount = int(body.get("amount") or 0)
    user = db.get_user_by_id(user_id)
    if user is None:
        return jsonify({"error": "not_found"}), 404
    if amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    db.add_to_wallet(user_id, amount, "شارژ کیف‌پول از پنل مینی‌اپ ادمین", tx_type="admin_adjust")
    fresh = db.get_user_by_id(user_id)
    try:
        if db.user_pref_enabled(user_id=user.get("id"), key="wallet_alerts"):
            tg_notify_user(user.get("telegram_id"), "notif_wallet_charge", f"✅ کیف پول شما {amount:,} تومان شارژ شد.")
    except Exception:
        pass
    return jsonify({"success": True, "wallet": int((fresh or user).get("wallet") or 0)})


@app.route("/admin/broadcast", methods=["POST"])
@require_admin_api
@rate_limit(max_calls=3, period_seconds=60)
def api_admin_broadcast():
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message_required"}), 400
    users = db.get_broadcastable_users("all", respect_prefs=True)
    sent = 0
    failed = 0
    for user in users:
        if tg_send_message(user.get("telegram_id"), message):
            sent += 1
        else:
            failed += 1
    return jsonify({"success": True, "sent": sent, "failed": failed})


# =============================================================================
# Reseller Mini App APIs
# =============================================================================
@app.route("/reseller/stats", methods=["GET"])
@require_reseller_api
def api_reseller_stats():
    agent = g.agent
    stats = db.get_referral_stats(g.user["id"])
    bot_username = bot_info.get("bot_username") or db.get_setting("bot_username", "") or ""
    referral_link = f"https://t.me/{bot_username}?start={stats['invite_code']}" if bot_username else stats["invite_code"]
    return jsonify({
        "balance": int(g.user.get("wallet") or 0),
        "locked_balance": int(g.user.get("locked_wallet") or 0),
        "total_earned": int((g.user.get("wallet") or 0) + (g.user.get("locked_wallet") or 0)),
        "customers_count": int(stats.get("successful_invites") or 0),
        "orders_count": int(stats.get("successful_invites") or 0),
        "orders_today": 0,
        "commission_percent": int(agent.get("vip_discount_percent") or 0),
        "referral_link": referral_link,
        "recent_sales": [],
    })


# =============================================================================
# 🎁 تست رایگان — دقیقاً همان چیزی که در ربات با دکمه‌ی «🎁 تست رایگان» دیده
# می‌شود؛ یک پلن کوچک با قیمت نمادین، با همان مسیر خرید عادی (کیف پول/کارت).
# =============================================================================
@app.route("/api/free-trial", methods=["GET"])
@require_telegram_auth
def api_free_trial():
    plan = db.get_effective_plan(FREE_TEST_PLAN_KEY)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404
    plans = []
    entries = db.get_enabled_free_test_panels()
    for e in entries:
        p = db._free_test_entry_to_plan(e)
        plans.append({"num": int(e.get("num") or 0), "name": p["name"], "volume_gb": p.get("volume_gb", 0), "days": p.get("days", 0),
                      "duration_unit": p.get("duration_unit", "days"), "duration_value": p.get("duration_value"),
                      "price": p.get("price", 0), "plan_key": db.free_test_plan_key(e["num"]),
                      "used": bool(db.has_used_free_test(g.user["id"], db.free_test_plan_key(e["num"])))})
    if not plans:
        plans.append({"num": 0, "name": plan["name"], "volume_gb": plan.get("volume_gb", 0), "days": plan.get("days", 0),
                      "duration_unit": plan.get("duration_unit", "days"), "duration_value": plan.get("duration_value"),
                      "price": plan.get("price", 0), "plan_key": FREE_TEST_PLAN_KEY,
                      "used": bool(db.has_used_free_test(g.user["id"], FREE_TEST_PLAN_KEY))})
    return jsonify({
        "plan": _serialize_plan(FREE_TEST_PLAN_KEY, plan),
        "plans": plans,
        "already_used": all(x.get("used") for x in plans),
        "wallet": g.user["wallet"],
        "orders_enabled": db.is_orders_enabled(),
    })


# =============================================================================
# محاسبه‌ی قیمت نهایی: بهترین حالت بین کد تخفیف کاربر و تخفیف خودکار نمایندگی
# (فقط روی VIP) — دقیقاً پورت‌شده از handlers/plans.py تا هیچ مغایرتی با ربات
# کلاسیک نداشته باشد.
# =============================================================================
def _compute_final_price(plan_key: str, plan: dict, tg_user_id, user_db_id: int, discount_code: str):
    price = plan["price"]

    code_price = price
    valid_code = None
    if discount_code:
        discount = db.get_discount(discount_code)
        if (
            discount
            and discount["uses"] > 0
            and not db.discount_is_expired(discount)
            and db.discount_applies_to_plan(discount, plan_key)
            and db.discount_allowed_for_user(discount, tg_user_id)
            and db.discount_allowed_for_first_purchase(discount, user_db_id)
        ):
            over_cap = (
                discount.get("max_uses_per_user")
                and db.user_discount_uses(discount["id"], user_db_id) >= discount["max_uses_per_user"]
            )
            under_min = discount.get("min_order_amount") and price < discount["min_order_amount"]
            if not over_cap and not under_min:
                code_price = db.compute_discount(discount, price)
                valid_code = discount

    agent_price = price
    if plan_type(plan_key) == "vip":
        _pct = db.agent_percent_for_plan(tg_user_id, plan_key, plan.get("category_id"))
        if _pct:
            agent_price = int(round(price * (1 - _pct / 100)))

    final_price = min(code_price, agent_price)
    winning_code = discount_code if (valid_code is not None and code_price <= agent_price) else None
    return final_price, winning_code


# =============================================================================
# کد تخفیف
# =============================================================================
@app.route("/api/discount/validate", methods=["POST"])
@require_telegram_auth
def api_discount_validate():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    plan_key = body.get("plan_key")
    if not code:
        return jsonify({"error": "code_required"}), 400

    discount = db.get_discount(code)
    if discount is None or discount["uses"] <= 0 or db.discount_is_expired(discount):
        return jsonify({"valid": False}), 200

    if plan_key and not db.discount_applies_to_plan(discount, plan_key):
        return jsonify({"valid": False, "reason": "wrong_plan"}), 200

    if not db.discount_allowed_for_user(discount, g.tg_user["id"]):
        return jsonify({"valid": False, "reason": "not_allowed"}), 200

    if not db.discount_allowed_for_first_purchase(discount, g.user["id"]):
        return jsonify({"valid": False, "reason": "first_purchase_only"}), 200

    if discount.get("max_uses_per_user") and db.user_discount_uses(discount["id"], g.user["id"]) >= discount["max_uses_per_user"]:
        return jsonify({"valid": False, "reason": "user_limit_reached"}), 200

    if plan_key and discount.get("min_order_amount"):
        plan = db.get_effective_plan(plan_key)
        if plan and plan["price"] < discount["min_order_amount"]:
            return jsonify({"valid": False, "reason": "min_order_amount", "min_order_amount": discount["min_order_amount"]}), 200

    return jsonify({
        "valid": True,
        "percent": discount["percent"],
        "discount_type": discount.get("discount_type", "percent"),
        "amount": discount.get("amount", 0),
        "min_order_amount": discount.get("min_order_amount", 0),
        "expires_at": discount.get("expires_at"),
    })


# =============================================================================
# خرید پلن ثابت (کیف پول)
# =============================================================================
# =============================================================================
# نام‌گذاری سرویس — معادل handlers/plans.py (حالت «نامگذاری توسط کاربر»)
# =============================================================================
_NAME_BASE_RE = re.compile(r"[A-Za-z0-9_]{3,26}")


def _naming_applies_web(user: dict, plan_key: str) -> bool:
    try:
        if bot_info.get_config_name_mode() != "user":
            return False
        if db.is_free_test_key(plan_key) or plan_type(plan_key) != "vip":
            return False
        if db.get_agent(user["telegram_id"]):
            return False
        return True
    except Exception:
        logging.getLogger(__name__).exception("naming_applies check failed")
        return False


def _service_name_available_web(plan_key: str, name: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[a-z0-9_]{3,32}", name or ""):
        return False, "invalid_name"
    if db.service_name_taken(name):
        return False, "name_taken"
    try:
        mapping = db.get_panel_map_for_plan_key(plan_key)
        if mapping and mapping.get("enabled"):
            panel = db.get_vpn_panel(mapping.get("panel_id"))
            if panel and panel.get("enabled"):
                ok, snap, _m = loopsafe.run(panels.get_service_snapshot(panel, name))
                if ok and snap:
                    return False, "name_taken_panel"
    except Exception:
        logging.getLogger(__name__).debug("panel duplicate check failed", exc_info=True)
    return True, ""


def _finalize_service_name_web(plan_key: str, base: str) -> str:
    import secrets
    base = (base or "").strip().lstrip("@")
    if not _NAME_BASE_RE.fullmatch(base):
        raise ValueError("invalid_name")
    base = base.lower()
    for _ in range(100):
        _sfx = bot_info.random_suffix()
        cand = f"{base[:31 - len(_sfx)].rstrip('_') or base[:1]}_{_sfx}"
        ok, _e = _service_name_available_web(plan_key, cand)
        if ok:
            return cand
    raise RuntimeError("name_unavailable")


def _resolve_chosen_name(user: dict, plan_key: str, raw):
    """(final_name|None, error_response|None). در حالت «user» نام الزامی است."""
    if not _naming_applies_web(user, plan_key):
        return None, None
    raw = str(raw or "").strip()
    if not raw:
        return None, (jsonify({"error": "name_required"}), 400)
    # اگر کلاینت نام نهایی (با پسوند) را از /api/orders/service-name گرفته باشد، همان را اعتبارسنجی می‌کنیم.
    low = raw.lower()
    if re.fullmatch(r"[a-z0-9_]{3,26}_\d{5}", low):
        ok, err = _service_name_available_web(plan_key, low)
        if ok:
            return low, None
        return None, (jsonify({"error": err}), 409)
    try:
        return _finalize_service_name_web(plan_key, raw), None
    except ValueError:
        return None, (jsonify({"error": "invalid_name"}), 400)
    except Exception:
        return None, (jsonify({"error": "name_unavailable"}), 409)


@app.route("/api/orders/service-name", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=20, period_seconds=60)
def api_order_service_name():
    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    if db.get_effective_plan(plan_key) is None:
        return jsonify({"error": "plan_not_found"}), 404
    if not _naming_applies_web(g.user, plan_key):
        return jsonify({"required": False, "name": None})
    name, err = _resolve_chosen_name(g.user, plan_key, body.get("name"))
    if err:
        return err
    return jsonify({"required": True, "name": name})


@app.route("/api/orders/wallet", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_order_wallet():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404

    user = g.user

    # 🐛 فیکس: مینی‌اپ قبلاً محدودیت «هر کاربر فقط یک‌بار تست رایگان» را چک
    # نمی‌کرد (برخلاف ربات کلاسیک در handlers/plans.py)، پس کاربر می‌توانست از
    # مینی‌اپ به‌تعداد نامحدود «تست رایگان» بگیرد.
    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        return jsonify({"error": "free_test_already_used"}), 409

    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)

    chosen_name, _nerr = _resolve_chosen_name(user, plan_key, body.get("service_name"))
    if _nerr:
        return _nerr

    if not db.claim_purchase_action(f"mini-wallet:{user['id']}:{plan_key}:{discount_code}", ttl_seconds=5):
        return jsonify({"error": "duplicate_request"}), 409

    if user["wallet"] < final_price:
        return jsonify({
            "error": "insufficient_balance",
            "price": final_price,
            "wallet": user["wallet"],
            "needed": final_price - user["wallet"],
        }), 402

    _ok, referral_funded_purchase = db.deduct_wallet_for_purchase(user["id"], final_price, f"خرید {plan['name']}")
    if not _ok:
        return jsonify({"error": "insufficient_balance"}), 402

    if winning_code:
        db.use_discount(winning_code, user["id"])

    if db.referral_purchase_qualifies(plan.get("volume_gb", 0), paid_purchase=True, is_free_test=db.is_free_test_key(plan_key)):
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    order_id = db.create_order(user["id"], plan_key, plan["name"], plan_type(plan_key), final_price, payment_method="کیف پول", service_name=chosen_name)

    # 🐛 فیکس: خرید کیف‌پولی از مینی‌اپ همیشه فقط به ادمین اطلاع می‌داد و منتظر
    # ارسال دستی می‌ماند؛ برخلاف ربات کلاسیک (handlers/plans.py::pay_with_wallet)
    # که اگر شاهراه فعال و برای این پلن نگاشت شده باشد، سرویس بلافاصله و
    # کاملاً خودکار از پنل شاهراه ساخته و ارسال می‌شود. همان منطق اینجا هم با
    # auto_fulfill_vip_via_panel اجرا می‌شود؛ اگر نگاشتی نباشد یا ناموفق
    # باشد، دقیقاً مثل قبل به ادمین برای ارسال دستی اطلاع داده می‌شود.
    tg_notify_user(user["telegram_id"], "plan_pay_wallet", "✅ پرداخت موفق! سرویس شما به‌زودی ساخته و ارسال می‌شود.")
    handled = False
    if plan_type(plan_key) in ("vip", "test"):
        handled = _run_async_with_temp_bot(
            lambda bot: auto_fulfill_vip_via_panel(bot, str(user["telegram_id"]), plan_key, order_id)
        )
        if handled and referral_funded_purchase:
            db.mark_latest_config_referral_funded(user["id"], "vip")

    if not handled:
        from keyboards import admin_purchase_notify_keyboard
        tg_send_message(
            ADMIN_ID,
            f"🛒 خرید جدید (کیف پول)!\n\n"
            f"👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
            f"📦 {plan['name']}\n💰 {final_price:,} تومان",
            reply_markup=_kb_json(admin_purchase_notify_keyboard(str(user["telegram_id"]), plan_key, order_id)),
        )
    return jsonify({"success": True, "order_id": order_id, "price": final_price})


# =============================================================================
# خرید پلن ثابت (کارت‌به‌کارت) — مرحله‌ی اول: اطلاعات کارت
# =============================================================================
@app.route("/api/orders/card/info", methods=["POST"])
@require_telegram_auth
def api_order_card_info():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()
    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404

    # 🐛 فیکس: مثل کیف‌پول/پرداخت آنلاین، محدودیت «هر کاربر فقط یک‌بار تست
    # رایگان» باید همینجا هم چک شود، وگرنه کاربر می‌توانست با کارت‌به‌کارت هم
    # به‌تعداد نامحدود تست رایگان بگیرد.
    if db.is_free_test_key(plan_key) and db.has_used_free_test(g.user["id"], plan_key):
        return jsonify({"error": "free_test_already_used"}), 409

    final_price, _winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], g.user["id"], discount_code)

    if _naming_applies_web(g.user, plan_key) and not str(body.get("service_name") or "").strip():
        return jsonify({"error": "name_required"}), 400

    # 🎲 مثل ربات: مبلغ فاکتور کارت‌به‌کارت کمی رندوم می‌شود (اگر ادمین خاموش نکرده باشد)
    final_price = apply_random_amount_variation(final_price)
    invoice = db.create_invoice(
        user_id=g.user["id"], telegram_id=str(g.user["telegram_id"]),
        kind="plan_card", label=plan["name"], price=final_price,
    )
    return jsonify({
        "card_number": bot_info.get("card_number"), "card_holder": bot_info.get("card_holder"), "price": final_price,
        "invoice_id": invoice["id"], "expires_at": invoice["expires_at"], **_card_ui_flags(),
    })


# =============================================================================
# پرداخت آنلاین (درگاه یونیک‌پی — کارت‌به‌کارت با تایید خودکار)
# =============================================================================
def _finalize_online_payment(payment: dict) -> int | None:
    """معادل نسخه‌ی ربات (handlers/plans.py::finalize_online_payment).
    🐛 فیکس ریس‌کاندیشن: این تابع در پروسه‌ی جدای webapp_api.py اجرا می‌شود
    در حالی که پولر پس‌زمینه‌ی ربات (bot.py) هم می‌تواند هم‌زمان همین پرداخت
    را finalize کند؛ چون این دو یک پروسه‌ی مشترک و قفل حافظه‌ای مشترک ندارند،
    یک if ساده کافی نبود. حالا با db.claim_online_payment_for_finalize یک
    claim اتمیک روی ردیف دیتابیس گرفته می‌شود تا از بین این دو مسیر، فقط
    یکی سفارش بسازد."""
    if payment["status"] == "paid" and payment.get("order_id"):
        return payment["order_id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid" and fresh.get("order_id"):
            return fresh["order_id"]
        return None

    try:
        try:
            _chosen_svc = (json.loads(payment.get("extra") or "{}") or {}).get("service_name")
        except Exception:
            _chosen_svc = None
        order_id = db.create_order(
            payment["user_id"], payment["plan_key"], payment["plan_name"],
            payment["order_type"], payment["price"], payment_method=og.method_label(payment),
            service_name=_chosen_svc,
        )
        db.mark_online_payment_paid(payment["id"], order_id)

        if payment.get("discount_code"):
            try:
                db.use_discount(payment["discount_code"], payment["user_id"])
            except Exception:
                logging.getLogger(__name__).exception("خطا در مصرف کد تخفیف پس از پرداخت آنلاین (Mini App)")

        plan = db.get_effective_plan(payment["plan_key"]) if payment["plan_key"] else None
        if plan and db.referral_purchase_qualifies(plan.get("volume_gb", 0), paid_purchase=True, is_free_test=db.is_free_test_key(payment.get("plan_key"))):
            try:
                db.complete_referral(payment["user_id"])
            except ValueError:
                pass
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise

    # 🐛 فیکس: مثل کیف‌پول، پرداخت آنلاین (یونیک‌پی) از مینی‌اپ هم قرار بود طبق
    # منطق ربات کلاسیک (handlers/plans.py::finalize_online_payment)، اگر
    # شاهراه فعال و برای این پلن نگاشت شده باشد، سرویس را بلافاصله و کاملاً
    # خودکار از پنل شاهراه بسازد و ارسال کند؛ ولی اینجا این مسیر اصلاً صدا زده
    # نمی‌شد و همیشه فقط ادمین مطلع می‌شد.
    tg_notify_user(payment["telegram_id"], "plan_pay_online", "✅ پرداخت شما تأیید شد و سفارش ثبت گردید. سرویس شما به‌زودی ارسال می‌شود.")
    handled = False
    if payment["plan_key"] and plan_type(payment["plan_key"]) in ("vip", "test"):
        handled = _run_async_with_temp_bot(
            lambda bot: auto_fulfill_vip_via_panel(
                bot, payment["telegram_id"], payment["plan_key"], order_id
            )
        )

    if not handled:
        send_btn = ("🚀 ارسال کانفیگ VIP (دستی)", f"sendvip_{payment['telegram_id']}|{order_id}")

        tg_send_message(
            ADMIN_ID,
            f"🛒 خرید جدید ({og.admin_label(payment)} - Mini App)!\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"📦 {payment['plan_name']}\n💰 {payment['price']:,} تومان",
            reply_markup=inline_kb([send_btn]),
        )
    return order_id



def _finalize_custom_online_payment(payment: dict) -> int | None:
    """معادل handlers/plans.py::finalize_custom_online_payment برای «بساز سرویس خودت / تمدید» با پرداخت آنلاین."""
    if payment["status"] == "paid" and payment.get("order_id"):
        return payment["order_id"]
    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid" and fresh.get("order_id"):
            return fresh["order_id"]
        return None
    try:
        extra = json.loads(payment.get("extra") or "{}")
    except Exception:
        extra = {}
    volume, days = extra.get("volume"), extra.get("days")
    custom_name = extra.get("custom_name")
    order_type = extra.get("order_type", "new")
    target_config_id = extra.get("target_config_id")
    try:
        order_id = db.create_custom_order(
            payment["user_id"], volume, days, custom_name, payment["price"], order_type, target_config_id,
            renew_mode=extra.get("renew_mode"), payment_method=og.method_label(payment), option_num=extra.get("option_num") or 0,
        )
        db.set_custom_order_status(order_id, "paid")
        db.mark_online_payment_paid(payment["id"], order_id)
        if db.referral_purchase_qualifies(volume, paid_purchase=True, is_free_test=False):
            try:
                db.complete_referral(payment["user_id"])
            except ValueError:
                pass
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise
    user = db.get_user_by_id(payment["user_id"])
    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت - Mini App)"
    tg_notify_user(payment["telegram_id"], "cbuild_pay_online", "✅ پرداخت شما تأیید شد. سرویس شما به‌زودی ساخته و ارسال می‌شود.")
    handled = False
    if user:
        handled = _run_async_with_temp_bot(lambda bot: auto_fulfill_custom_via_panel(bot, user, order_id, volume, days, custom_name))
    if not handled:
        from keyboards import admin_custom_order_notify_keyboard
        tg_send_message(
            ADMIN_ID,
            f"🛠 {label} ({og.admin_label(payment)})!\n\n🆔 {payment['telegram_id']}\n"
            f"📦 حجم: {volume} گیگ\n⏳ مدت: {days} روز\n"
            + (f"🔤 نام: {custom_name}\n" if custom_name else "")
            + f"💰 {payment['price']:,} تومان\n🔢 شماره سفارش: {order_id}",
            reply_markup=_kb_json(admin_custom_order_notify_keyboard(order_id)),
        )
    return order_id


def _finalize_any_online_payment(payment: dict) -> int | None:
    kind = payment.get("kind")
    if kind == "wallet_charge":
        return _finalize_wallet_charge_online_payment(payment)
    if kind == "custom":
        return _finalize_custom_online_payment(payment)
    return _finalize_online_payment(payment)


# =============================================================================
# درگاه‌های جدید (FrenzyEx / Tronado): وب‌هوک‌ها + ساخت/استعلام فاکتور Mini App
# =============================================================================
def _gw_notify_admin(text, buttons=None):
    kb = inline_kb(list(buttons)) if buttons else None
    return tg_send_message(ADMIN_ID, text, reply_markup=kb)


def _gw_notify_user(chat_id, text):
    return tg_send_message(chat_id, text)


def _gw_webhook_response(result):
    status, body = result
    return body, status, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/payments/frenzyex/callback", methods=["POST"])
def api_frenzyex_callback():
    raw = request.get_data(cache=False) or b""
    return _gw_webhook_response(og.handle_frenzyex_callback(raw, request.headers, _gw_notify_admin, _gw_notify_user))


@app.route("/api/payments/tonpays/callback", methods=["POST"])
def api_tonpays_callback():
    raw = request.get_data(cache=False) or b""
    return _gw_webhook_response(og.handle_tonpays_callback(raw, request.headers, _gw_notify_admin, _gw_notify_user))


@app.route("/api/payments/tronado/callback", methods=["POST"])
def api_tronado_callback():
    raw = request.get_data(cache=False) or b""
    return _gw_webhook_response(og.handle_tronado_callback(raw, request.headers, _gw_notify_admin, _gw_notify_user))


@app.route("/api/payments/tronado/dispute", methods=["POST"])
def api_tronado_dispute():
    raw = request.get_data(cache=False) or b""
    return _gw_webhook_response(og.handle_tronado_dispute(raw, request.headers, _gw_notify_admin))


def _requested_new_gateway():
    """اگر بدنه‌ی درخواست gateway=frenzyex|tronado|tonpays داشت همان را برمی‌گرداند، وگرنه None (مسیر قدیمی یونیک‌پی)."""
    body = request.get_json(silent=True) or {}
    gw = str(body.get("gateway") or "").strip().lower()
    return gw if gw in og.NEW_GATEWAYS else None


def _gw_create_payload(payment_id, link, price, res):
    row = db.get_online_payment(payment_id)
    gw = db.gw_data_of(row) if row else {}
    return jsonify({
        "payment_id": payment_id, "payment_link": link, "payment_link2": res.link2, "price": price,
        "gateway": row.get("gateway") if row else None,
        "tron_amount": gw.get("tron_amount"),
        "info_lines": res.info_lines,
        "expires_at": db.online_payment_expires_at(row["created_at"]) if row else None,
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
    })


def _card_ui_flags() -> dict:
    """تنظیمات ادمین برای صفحه‌ی کارت‌به‌کارت (دکمه‌های کپی)."""
    return {"show_copy_card": db.card_option("show_copy_card"), "show_copy_amount": db.card_option("show_copy_amount")}


def _create_new_gateway_plan_payment(gw: str):
    if not og.enabled(gw):
        return jsonify({"error": "gateway_disabled"}), 400
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403
    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()
    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404
    user = g.user
    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        return jsonify({"error": "free_test_already_used"}), 409
    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)
    chosen_name, _nerr = _resolve_chosen_name(user, plan_key, body.get("service_name"))
    if _nerr:
        return _nerr
    if not final_price or not og.amount_ok(gw, final_price):
        return jsonify({"error": "amount_too_low", "min_amount": og.min_amount(gw)}), 400
    ref = og.new_ref("miniapp-plan", gw)
    res = og.create_payment_sync(gw, ref, final_price, plan["name"], buyer_id=user["telegram_id"])
    if not res.ok:
        return jsonify({"error": "gateway_error", "detail": res.error}), 502
    try:
        payment_id = db.create_online_payment(
            user_id=user["id"], telegram_id=str(user["telegram_id"]), hash_id=ref,
            plan_name=plan["name"], price=final_price, order_type=plan_type(plan_key),
            plan_key=plan_key, discount_code=winning_code, payment_link=res.link,
            ref_id=res.ref_id, gateway=gw, gw_data=res.gw_data,
            extra=(json.dumps({"service_name": chosen_name}, ensure_ascii=False) if chosen_name else None),
        )
    except Exception:
        logging.getLogger(__name__).exception("ثبت فاکتور %s (Mini App) ناموفق بود", gw)
        og.cancel_payment_sync({"gateway": gw, "ref_id": res.ref_id})
        return jsonify({"error": "gateway_error"}), 500
    return _gw_create_payload(payment_id, res.link, final_price, res)


def _create_new_gateway_wallet_payment(gw: str):
    if not og.enabled(gw) or gw not in db.available_topup_methods():
        return jsonify({"error": "gateway_disabled"}), 400
    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    ws = db.get_wallet_settings()
    if amount < ws["min_topup"]:
        return jsonify({"error": "amount_too_low", "min_amount": ws["min_topup"]}), 400
    if not og.amount_ok(gw, amount):
        return jsonify({"error": "amount_too_low", "min_amount": og.min_amount(gw)}), 400
    if amount > ws["max_topup"]:
        return jsonify({"error": "amount_too_high", "max_amount": ws["max_topup"]}), 400
    user = g.user
    ref = og.new_ref("miniapp-charge", gw)
    res = og.create_payment_sync(gw, ref, amount, "شارژ کیف پول", buyer_id=user["telegram_id"])
    if not res.ok:
        return jsonify({"error": "gateway_error", "detail": res.error}), 502
    try:
        payment_id = db.create_online_payment(
            user_id=user["id"], telegram_id=str(user["telegram_id"]), hash_id=ref,
            plan_name="شارژ کیف پول", price=amount, order_type="wallet_charge", kind="wallet_charge",
            payment_link=res.link, ref_id=res.ref_id, gateway=gw, gw_data=res.gw_data,
        )
    except Exception:
        logging.getLogger(__name__).exception("ثبت فاکتور شارژ %s (Mini App) ناموفق بود", gw)
        og.cancel_payment_sync({"gateway": gw, "ref_id": res.ref_id})
        return jsonify({"error": "gateway_error"}), 500
    return _gw_create_payload(payment_id, res.link, amount, res)


def _new_gateway_status_response(payment: dict):
    """استعلام وضعیت برای فاکتورهای FrenzyEx/Tronado در Mini App (تحویل با همان finalizerهای اتمیک موجود)."""
    st = payment["status"]
    if st == "paid":
        return jsonify({"status": "paid", "order_id": payment.get("order_id")})
    if st == "review":
        return jsonify({"status": "pending", "review": True})
    if st in ("expired", "rejected"):
        # پرداخت دیرهنگام را پولر ربات (واریز به کیف پول) رسیدگی می‌کند؛ مینی‌اپ مثل فاکتور منقضی رفتار می‌کند.
        return jsonify({"error": "invoice_expired"}), 404
    res = og.check_payment_sync(payment)
    if res.state == "paid":
        fresh = db.get_online_payment(payment["id"]) or payment
        result_id = _finalize_any_online_payment(fresh)
        if result_id is None:
            return jsonify({"status": "pending"})
        return jsonify({"status": "paid", "order_id": result_id})
    if res.state == "review":
        og.hold_for_review_sync(payment, res.reason or "مغایرت در پرداخت", _gw_notify_admin, _gw_notify_user)
        return jsonify({"status": "pending", "review": True})
    if res.state == "closed":
        return jsonify({"error": "invoice_expired"}), 404
    if res.state in ("action_less", "action_more"):
        # تون‌پیز: مبلغ کمتر/بیشتر واریز شده؛ مینی‌اپ باید دکمه‌ی «ارسال رسید مابقی»/«ثبت کارت عودت» نشان بدهد.
        d = res.data or {}
        return jsonify({"status": "pending", "note": res.reason,
                        "action": "less" if res.state == "action_less" else "more",
                        "remaining_amount": d.get("remaining_amount"), "remaining_card_number": d.get("remaining_card_number"),
                        "remaining_card_holder": d.get("remaining_card_holder"), "refund_amount": d.get("refund_amount"),
                        "done": bool(d.get("remaining_receipt_received") if res.state == "action_less" else d.get("refund_pending"))})
    if res.state == "pending" and res.reason:
        return jsonify({"status": "pending", "note": res.reason})
    return jsonify({"status": "pending"})


def _own_tonpays_payment_api(payment_id):
    p = db.get_online_payment(int(payment_id)) if str(payment_id).isdigit() else None
    if not p or str(p.get("telegram_id")) != str(g.user["telegram_id"]) or og.gateway_of(p) != "tonpays":
        return None
    return p


@app.route("/api/orders/online/<payment_id>/tonpays/refund", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=6, period_seconds=60)
def api_tonpays_refund(payment_id):
    """ثبت کارت عودت مازاد (TonPays action_required_more)."""
    import re as _re, tonpays
    p = _own_tonpays_payment_api(payment_id)
    if not p or p["status"] not in ("pending", "expired"):
        return jsonify({"error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    card = _re.sub(r"\D", "", str(body.get("card_number") or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
    holder = str(body.get("card_holder") or "").strip()
    if len(card) != 16 or len(holder) < 3:
        return jsonify({"error": "invalid_card"}), 400
    res = tonpays.register_refund_card(p.get("ref_id") or "", card, holder, tonpays.kind_of(p))
    if not res["ok"]:
        return jsonify({"error": "gateway_error", "detail": res.get("error")}), 502
    db.merge_gw_data(p["id"], refund_card_registered=True)
    return jsonify({"ok": True})


@app.route("/api/orders/online/<payment_id>/tonpays/remainder", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=6, period_seconds=60)
def api_tonpays_remainder(payment_id):
    """ارسال رسید مابقی (TonPays action_required_less)؛ multipart با فیلد file."""
    import tonpays
    p = _own_tonpays_payment_api(payment_id)
    if not p or p["status"] not in ("pending", "expired"):
        return jsonify({"error": "not_found"}), 404
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no_file"}), 400
    ctype = (f.mimetype or "").lower()
    if ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return jsonify({"error": "bad_type"}), 400
    content = f.read(10 * 1024 * 1024 + 1)
    if not content or len(content) > 10 * 1024 * 1024:
        return jsonify({"error": "bad_size"}), 400
    res = tonpays.send_remainder_receipt(p.get("ref_id") or "", f.filename or "receipt.jpg", content, ctype, tonpays.kind_of(p))
    if not res["ok"]:
        return jsonify({"error": "gateway_error", "detail": res.get("error")}), 502
    db.merge_gw_data(p["id"], remainder_receipt_sent=True)
    return jsonify({"ok": True})


@app.route("/api/orders/online/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_order_online_create():
    _gw = _requested_new_gateway()
    if _gw:
        return _create_new_gateway_plan_payment(_gw)
    if not _uq_enabled():
        return jsonify({"error": "gateway_disabled"}), 400
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404

    user = g.user

    # 🐛 فیکس: مثل کیف‌پول/کارت‌به‌کارت، محدودیت «هر کاربر فقط یک‌بار تست
    # رایگان» باید همینجا هم چک شود، وگرنه کاربر می‌توانست از درگاه پرداخت
    # آنلاین هم به‌تعداد نامحدود تست رایگان بگیرد.
    if db.is_free_test_key(plan_key) and db.has_used_free_test(user["id"], plan_key):
        return jsonify({"error": "free_test_already_used"}), 409

    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)

    # 🐛 فیکس: تا اینجا برای مبالف کم (≤ ONLINE_PAYMENT_MIN_AMOUNT) هیچ بررسی انجام نمی‌شد و درخواست مستقیماً به درگاه می‌رفت و تا خود درگاه با خطای ‌عمومی gateway_error رد می‌شد (مثل کیف‌پول)؛ باید همین جا بررسی شود تا فراند پیام دقیق amount_too_low را ببیند، نه یک خطای گنگ درگاه.
    if final_price and final_price <= ONLINE_PAYMENT_MIN_AMOUNT:
        return jsonify({"error": "amount_too_low", "min_amount": ONLINE_PAYMENT_MIN_AMOUNT}), 400

    chosen_name, _nerr = _resolve_chosen_name(user, plan_key, body.get("service_name"))
    if _nerr:
        return _nerr

    hash_id = uniquepay_sync.new_hash_id("miniapp-plan")
    invoice = uniquepay_sync.create_invoice(hash_id, final_price)
    payment_link = uniquepay_cfg.pick_link(invoice) if invoice else None
    if not payment_link:
        return jsonify({"error": "gateway_error"}), 502

    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(user["telegram_id"]),
        hash_id=hash_id,
        plan_name=plan["name"],
        price=final_price,
        order_type=plan_type(plan_key),
        plan_key=plan_key,
        discount_code=winning_code,
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
        extra=(json.dumps({"service_name": chosen_name}, ensure_ascii=False) if chosen_name else None),
    )
    return jsonify({
        "payment_id": payment_id, "payment_link": payment_link, "price": final_price,
        "white_label": uniquepay_sync.white_label_data(invoice),
        "expires_at": db.online_payment_expires_at(db.get_online_payment(payment_id)["created_at"]),
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
    })


def _finalize_wallet_charge_online_payment(payment: dict) -> int | None:
    """معادل نسخه‌ی ربات (handlers/wallet.py::finalize_wallet_charge_online_payment).
    به‌جای ساخت سفارش، مستقیماً مبلف به کیف پول کاربر اضافه می‌شود. از همان
    claim اتمیک استفاده می‌شود تا با پولر پس‌زمینه‌ی ربات (bot.py) که هم‌زمان
    همین اینویس‌ها را چک می‌کند، تداخل/شارژ تکراری پیش نیاید."""
    if payment["status"] == "paid":
        return payment["id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid":
            return fresh["id"]
        return None

    try:
        db.add_to_wallet(payment["user_id"], payment["price"], "شارژ کیف پول (پرداخت آنلاین)")
        db.credit_charge_bonus(payment["user_id"], int(payment["price"]))
        _credited = db.apply_wallet_discount_credit(payment["user_id"], int(payment["price"]), payment.get("discount_code"), payment.get("extra"))
        db.mark_online_payment_paid(payment["id"], None)
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise

    if db.user_pref_enabled(user_id=payment.get("user_id"), key="wallet_alerts") and notif_settings.enabled("wallet_charged"):
        tg_notify_user(payment["telegram_id"], "notif_wallet_charge",
                       f"✅ پرداخت آنلاین شما تأیید شد و کیف پول شما به مبلغ {int(_credited):,} تومان شارژ شد.")
    tg_send_message(
        ADMIN_ID,
        f"💳 شارژ کیف پول ({og.admin_label(payment)} - Mini App)!\n\n"
        f"🆔 {payment['telegram_id']}\n💰 {payment['price']:,} تومان",
    )
    return payment["id"]


@app.route("/api/wallet/online/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_wallet_online_create():
    _gw = _requested_new_gateway()
    if _gw:
        return _create_new_gateway_wallet_payment(_gw)
    if not _uq_enabled() or "online" not in db.available_topup_methods():
        return jsonify({"error": "gateway_disabled"}), 400

    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    if amount < db.get_wallet_settings()["min_topup"]:
        return jsonify({"error": "amount_too_low", "min_amount": db.get_wallet_settings()["min_topup"]}), 400
    if amount <= ONLINE_PAYMENT_MIN_AMOUNT:
        return jsonify({"error": "amount_too_low", "min_amount": ONLINE_PAYMENT_MIN_AMOUNT}), 400
    # 🐛 فیکس: قبلاً هیچ سقفی برای مبلف شارژ اونلاین وجود ندارشت.
    if amount > db.get_wallet_settings()["max_topup"]:
        return jsonify({"error": "amount_too_high", "max_amount": db.get_wallet_settings()["max_topup"]}), 400

    user = g.user
    hash_id = uniquepay_sync.new_hash_id("miniapp-charge")
    invoice = uniquepay_sync.create_invoice(hash_id, amount)
    payment_link = uniquepay_cfg.pick_link(invoice) if invoice else None
    if not payment_link:
        return jsonify({"error": "gateway_error"}), 502

    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(user["telegram_id"]),
        hash_id=hash_id,
        plan_name="شارژ کیف پول",
        price=amount,
        order_type="wallet_charge",
        kind="wallet_charge",
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
    )
    return jsonify({
        "payment_id": payment_id, "payment_link": payment_link, "price": amount,
        "white_label": uniquepay_sync.white_label_data(invoice),
        "expires_at": db.online_payment_expires_at(db.get_online_payment(payment_id)["created_at"]),
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
    })


@app.route("/api/orders/online/status", methods=["GET"])
@require_telegram_auth
def api_order_online_status():
    payment_id = request.args.get("id", type=int)
    if not payment_id:
        return jsonify({"error": "invalid_request"}), 400

    payment = db.get_online_payment(payment_id)
    if payment is None:
        return jsonify({"error": "invoice_expired"}), 404
    if str(g.tg_user["id"]) != payment["telegram_id"]:
        return jsonify({"error": "forbidden"}), 403

    if og.is_new_gateway(og.gateway_of(payment)):
        return _new_gateway_status_response(payment)

    if payment["status"] == "paid":
        return jsonify({"status": "paid", "order_id": payment.get("order_id")})

    invoice = uniquepay_sync.check_invoice(payment["hash_id"])
    if invoice and invoice.get("isPaid") and invoice.get("isVerified"):
        result_id = _finalize_any_online_payment(payment)
        if result_id is None:
            # یک فراخوانی هم‌زمان دیگر (مثلاً پولر ربات) همین الان در حال
            # پردازش همین پرداخت است؛ کلاینت چند لحظه‌ی دیگر دوباره همین
            # endpoint را صدا می‌زند و آن‌موقع status=paid برمی‌گردد.
            return jsonify({"status": "pending"})
        return jsonify({"status": "paid", "order_id": result_id})

    return jsonify({"status": "pending"})


# =============================================================================
# خرید پلن ثابت (کارت‌به‌کارت) — مرحله‌ی دوم: آپلود عکس رسید (multipart)
# =============================================================================
@app.route("/api/orders/card/receipt", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_order_card_receipt():
    # 🐛 فیکس: برخلاف مرحلهٔ اطلاعات کارت، این endpoint قبلاً orders_enabled و محدودیت
    # تست رایگان را دوباره بررسی نمی‌کرد و می‌شد با زدن مستقیم این endpoint رسید/Pending
    # برای ادمین ساخت حتی وقتی سفارش‌ها بسته است یا این کاربر قبلاً تست رایگان گرفته.
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    plan_key = request.form.get("plan_key")
    discount_code = (request.form.get("discount_code") or "").strip()
    photo = request.files.get("receipt")
    invoice_id = request.form.get("invoice_id", type=int)

    plan = db.get_effective_plan(plan_key)
    if plan is None or photo is None:
        return jsonify({"error": "invalid_request"}), 400
    if not _valid_receipt_photo(photo):
        return jsonify({"error": "invalid_file_type"}), 400

    if db.is_free_test_key(plan_key) and db.has_used_free_test(g.user["id"], plan_key):
        return jsonify({"error": "free_test_already_used"}), 409

    _inv = db.consume_invoice(invoice_id) if invoice_id else None
    if _inv is None:
        return jsonify({"error": "invoice_expired"}), 409

    user = g.user
    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)
    try:
        if int(_inv.get("price") or 0) > 0:
            final_price = int(_inv["price"])  # همان مبلغی که به کاربر نشان داده شد (با رندوم‌سازی)
    except Exception:
        pass
    chosen_name, _nerr = _resolve_chosen_name(user, plan_key, request.form.get("service_name"))
    if _nerr:
        return _nerr

    caption = (
        f"💳 رسید خرید کارت‌به‌کارت (Mini App)\n\n"
        f"👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
        f"📦 {plan['name']}\n💰 {final_price:,} تومان"
        + (f"\n🔤 نام سرویس: {chosen_name}" if chosen_name else "")
    )
    try:
        _receipt_id = db.create_pending_receipt(
            "plan_card", user["telegram_id"], user["id"], plan["name"], final_price,
            extra=plan_key, plan_key=plan_key, discount_code=winning_code,
            service_name=chosen_name,
        )
    except Exception:
        _receipt_id = None
        logging.getLogger(__name__).exception("خطا در ثبت رسید معلق خرید کارت به کارت (Mini App)")
    _approve_cb = f"approvepay|{user['telegram_id']}|{plan_key}|{final_price}" + (f"|{_receipt_id}" if _receipt_id is not None else "")
    if len(_approve_cb.encode("utf-8")) > 64 and _receipt_id is not None:
        _approve_cb = f"approvepay|{user['telegram_id']}|@|{final_price}|{_receipt_id}"
    approve_kb = inline_kb([
        (f"✅ تأیید پرداخت ({final_price:,} ت)", _approve_cb),
        ("❌ رد رسید", f"rejectpay|{user['telegram_id']}" + (f"|{_receipt_id}" if _receipt_id is not None else "")),
    ])
    if _receipt_id is not None:
        caption += f"\n🧾 کد پیگیری: {_receipt_id}"
    sent = tg_send_photo_bytes(ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=approve_kb)
    if sent and _receipt_id is not None:
        try:
            db.set_pending_receipt_media(_receipt_id, file_id=sent if isinstance(sent, str) else None,
                                         file_type="photo" if isinstance(sent, str) else None, details=caption)
        except Exception:
            logging.getLogger(__name__).exception("ذخیره‌ی file_id رسید Mini App ناموفق بود")
    if not sent:
        if _receipt_id is not None:
            try:
                db.delete_pending_receipt(_receipt_id)
            except Exception:
                logging.getLogger(__name__).exception("خطا در تمیزکاری رسید معلق پس از شکست ارسال")
        return jsonify({
            "error": "telegram_delivery_failed",
            "error_message": "ارسال رسید به ادمین موقتاً با خطا مواجه شد؛ لطفاً چند لحظه دیگر دوباره تلاش کنید.",
        }), 502

    # 🐛 فیکس: کد تخفیف کارت‌به‌کارت حالا با discount_code=winning_code همراه
    # رسید در دیتابیس ذخیره می‌شود و فقط هنگام تأیید ادمین (نه اینجا، نه هنگام
    # رد) مصرف خواهد شد؛ قبلاً این مقدار اصلاً ذخیره نمی‌شد.
    if invoice_id:
        db.delete_invoice(invoice_id)

    return jsonify({"success": True})


# =============================================================================
# کیف پول
# =============================================================================
_TX_POSITIVE_TYPES = ("charge", "referral_release")


def _signed_tx_amount(tx: dict) -> int:
    """در دیتابیس amount همیشه مثبت (مقدار مطلق) ذخیره می‌شود؛ نوع تراکنش
    تعیین می‌کند که واقعاً واریز بوده (شارژ/آزادسازی پاداش → مثبت/سبز) یا
    خروج از حساب بوده (خرید، برداشت، پاداش در انتظار → منفی/قرمز)."""
    amount = abs(tx["amount"])
    return amount if tx["type"] in _TX_POSITIVE_TYPES else -amount


@app.route("/api/wallet", methods=["GET"])
@require_telegram_auth
def api_wallet():
    u = g.user
    txs = db.get_transactions(u["id"], limit=20)
    return jsonify({
        "wallet": u["wallet"],
        "locked_wallet": u["locked_wallet"],
        "card_number": bot_info.get("card_number"),
        "card_holder": bot_info.get("card_holder"),
        "min_purchase_gb": db.get_referral_settings()["min_volume_gb"],
        "online_payment_enabled": _uq_enabled(),
        "gift_percent": db.wallet_gift_percent(),
        "topup_methods": db.available_topup_methods(),
        "online_gateways": [{"key": k, "title": og.title(k), "min_amount": og.min_amount(k)} for k in og.enabled_gateways() if k in db.available_topup_methods()],
        "online_payment_min_amount": ONLINE_PAYMENT_MIN_AMOUNT,
        "referral_rule": _referral_rule_payload()["rule"],
        "referral_enabled": db.referral_enabled(),
        "transactions": [
            __import__("webapp_common").tx_view(tx)
            for tx in txs
        ],
    })


@app.route("/api/wallet/card/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_wallet_card_create():
    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    if amount > db.get_wallet_settings()["max_topup"]:
        return jsonify({"error": "amount_too_high", "max_amount": db.get_wallet_settings()["max_topup"]}), 400
    if amount < db.get_wallet_settings()["min_topup"]:
        return jsonify({"error": "amount_too_low", "min_amount": db.get_wallet_settings()["min_topup"]}), 400
    if "card" not in db.available_topup_methods():
        return jsonify({"error": "method_disabled", "message": "کارت‌به‌کارت برای شارژ کیف پول فعال نیست."}), 403

    user = g.user
    invoice = db.create_invoice(
        user_id=user["id"],
        telegram_id=str(user["telegram_id"]),
        kind="wallet_card_miniapp",
        label="شارج کیف پول (Mini App)",
        price=amount,
    )
    return jsonify({
        "invoice_id": invoice["id"], "expires_at": invoice["expires_at"],
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
        "card_number": bot_info.get("card_number"), "card_holder": bot_info.get("card_holder"), "price": amount,
        **_card_ui_flags(),
    })


@app.route("/api/wallet/topup", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_wallet_topup():
    amount_raw = request.form.get("amount")
    photo = request.files.get("receipt")
    invoice_id = request.form.get("invoice_id", type=int)

    if not amount_raw or not str(amount_raw).isdigit() or photo is None:
        return jsonify({"error": "invalid_request"}), 400
    if not _valid_receipt_photo(photo):
        return jsonify({"error": "invalid_file_type"}), 400
    amount = int(amount_raw)
    if amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    # 🐛 فیکس: مبلف شارژ کارت‌به‌کارت را هم محدود کنیم.
    if amount > db.get_wallet_settings()["max_topup"]:
        return jsonify({"error": "amount_too_high", "max_amount": db.get_wallet_settings()["max_topup"]}), 400

    if not invoice_id or db.consume_invoice(invoice_id) is None:
        return jsonify({"error": "invoice_expired"}), 410

    user = g.user
    caption = f"📩 رسید شارژ (Mini App)\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n💰 {amount:,} تومان"
    try:
        _receipt_id = db.create_pending_receipt("charge", user["telegram_id"], user["id"], "شارژ کیف پول (Mini App)", amount)
    except Exception:
        _receipt_id = None
        logging.getLogger(__name__).exception("خطا در ثبت رسید معلق شارژ کیف پول (Mini App)")
    _sfx = f"_{_receipt_id}" if _receipt_id is not None else ""
    approve_kb = inline_kb([
        (f"✅ تأیید {amount:,}", f"approve_{user['telegram_id']}_{amount}{_sfx}"),
        ("💵 مبلغ دلخواه", f"custom_{user['telegram_id']}{_sfx}"),
        ("❌ رد", f"reject_{user['telegram_id']}{_sfx}"),
    ])
    if _receipt_id is not None:
        caption += f"\n🧾 کد پیگیری: {_receipt_id}"
    sent = tg_send_photo_bytes(ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=approve_kb)
    if sent and _receipt_id is not None:
        try:
            db.set_pending_receipt_media(_receipt_id, file_id=sent if isinstance(sent, str) else None,
                                         file_type="photo" if isinstance(sent, str) else None, details=caption)
        except Exception:
            logging.getLogger(__name__).exception("ذخیره‌ی file_id رسید شارژ Mini App ناموفق بود")
    if not sent:
        if _receipt_id is not None:
            try:
                db.delete_pending_receipt(_receipt_id)
            except Exception:
                logging.getLogger(__name__).exception("خطا در تمیزکاری رسید معلق پس از شکست ارسال")
        return jsonify({
            "error": "telegram_delivery_failed",
            "error_message": "ارسال رسید به ادمین موقتاً با خطا مواجه شد؛ لطفاً چند لحظه دیگر دوباره تلاش کنید.",
        }), 502

    db.delete_invoice(invoice_id)
    return jsonify({"success": True})


# =============================================================================
# سرویس‌های من — فقط VIP
@app.route("/api/services/<int:config_id>/configs", methods=["GET"])
@require_telegram_auth
def api_service_configs(config_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404
    panel, snapshot, err = _live_panel_snapshot(cfg)
    if panel and panel.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"):
        _pc = panels.extract_panel_configs(snapshot) if snapshot is not None else []
        if _pc:
            return jsonify({"configs": _pc, "source": "panel"})
        # پنل خطا داد یا لینک‌ها را برنگرداند → از لینک ساب هم تلاش می‌کنیم (قبلاً «کانفیگی یافت نشد» می‌داد)
        try:
            _sl = crypto.decrypt_config(cfg["config"])
            if _sl and _sl.lower().startswith(("http://", "https://")):
                _sc = loopsafe.run(extract_configs(_sl)) or []
                if _sc:
                    return jsonify({"configs": _sc, "source": "subscription"})
        except Exception:
            logging.getLogger(__name__).exception("subscription config fallback failed")
        if snapshot is None:
            return jsonify({"error": "panel_unreachable", "message": err}), 502
        return jsonify({"configs": [], "source": "panel"})
    try:
        sub_link = crypto.decrypt_config(cfg["config"])
        if not sub_link or not sub_link.lower().startswith(("http://", "https://")):
            return jsonify({"configs": []})
        configs = loopsafe.run(extract_configs(sub_link)) or []
        return jsonify({"configs": configs, "source": "shahrah_subscription"})
    except Exception:
        logging.getLogger(__name__).exception("subscription config extraction failed")
        return jsonify({"error": "unreachable"}), 502


# --- cache + موازی‌سازی تماس‌های زنده با پنل -------------------------------------
# قبلاً هر سرویس به‌صورت پشت‌سرهم از پنل پرسیده می‌شد؛ با N سرویس و پنل کند، صفحات
# «خانه» و «سرویس‌ها» N برابر معطل می‌شدند و عملاً باز نمی‌شدند. حالا: کش کوتاه (۲۵ ثانیه)،
# نتیجه‌ی قدیمی تا ۱۵ دقیقه به‌عنوان fallback هنگام خطای پنل، و درخواست موازی.
import threading as _th
from concurrent.futures import ThreadPoolExecutor as _TPE, wait as _fwait

_SNAP_TTL = 25
_SNAP_STALE = 900
_SNAP_TIMEOUT = 6
_snap_cache: dict = {}
_snap_lock = _th.Lock()
_snap_pool = _TPE(max_workers=8, thread_name_prefix="snap")


def invalidate_live_snapshot(cfg_or_id=None):
    """بعد از هر عملیات تغییردهنده (ریست/تمدید/فعال‌سازی/حذف) کش آن سرویس پاک شود."""
    with _snap_lock:
        if cfg_or_id is None:
            _snap_cache.clear()
            return
        cfg = cfg_or_id if isinstance(cfg_or_id, dict) else db.get_config_by_id(int(cfg_or_id))
        if cfg:
            _snap_cache.pop((cfg.get("panel_id"), str(cfg.get("service_id"))), None)


def _live_panel_snapshot(cfg, fresh: bool = False):
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        return None, None, "service_not_panel_managed"
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        return None, None, "panel_not_found"
    key = (cfg["panel_id"], str(cfg["service_id"]))
    now = time.time()
    with _snap_lock:
        hit = _snap_cache.get(key)
    if hit and not fresh and now - hit[0] < _SNAP_TTL:
        return panel, hit[1], None
    try:
        ok, snapshot, msg = loopsafe.run(panels.get_service_snapshot(panel, cfg["service_id"]))
    except Exception as exc:
        logging.getLogger(__name__).exception("live panel snapshot failed")
        ok, snapshot, msg = False, None, str(exc)
    if ok and snapshot is not None:
        with _snap_lock:
            _snap_cache[key] = (time.time(), snapshot)
        return panel, snapshot, None
    # پنل خطا داد: نتیجه‌ی قدیمی (اگر هست) بهتر از صفحه‌ی خالی است
    if hit and now - hit[0] < _SNAP_STALE:
        return panel, hit[1], None
    return panel, None, msg


def _live_snapshots(cfgs):
    """[(cfg, panel, snapshot, err)] با درخواست‌های موازی و سقف زمانی."""
    futs = {}
    for cfg in cfgs:
        futs[cfg["id"]] = _snap_pool.submit(_live_panel_snapshot, cfg)
    _fwait(list(futs.values()), timeout=_SNAP_TIMEOUT)
    out = []
    for cfg in cfgs:
        f = futs[cfg["id"]]
        if f.done() and not f.cancelled() and f.exception() is None:
            panel, snap, err = f.result()
        else:
            f.cancel()
            panel = db.get_vpn_panel(cfg["panel_id"]) if cfg.get("panel_id") else None
            snap, err = None, "timeout"
        out.append((cfg, panel, snap, err))
    return out


def _own_config_or_none(config_id: int, user_id: int):
    cfg = db.get_config_by_id(config_id)
    if cfg is None or cfg["user_id"] != user_id or cfg.get("deleted"):
        return None
    return cfg


def _expire_to_epoch(v):
    """expire پنل (epoch ثانیه/میلی‌ثانیه یا رشته‌ی ISO) → epoch ثانیه یا None (= نامحدود)."""
    from webapp_user_ext import to_epoch
    return to_epoch(v)


def _service_metrics(cfg, live, has_snapshot):
    """محاسبه‌ی یکسان مصرف/زمان/وضعیت سرویس — دقیقاً مطابق نمایش ربات (view_config):
    مصرف = used یا آپلود+دانلود، حجم ۰/خالی = نامحدود، باقی‌مانده کمتر از صفر نمی‌شود،
    زمان از پنل و اگر پنل در دسترس نبود از تاریخ ذخیره‌شده‌ی سرویس."""
    live = live or {}
    total = live.get("total") if has_snapshot else None
    used = live.get("used") if has_snapshot else None
    if has_snapshot and used is None:
        try:
            used = int(live.get("upload") or 0) + int(live.get("download") or 0)
        except Exception:
            used = 0
    unlimited_volume = bool(has_snapshot and not total)
    if has_snapshot:
        total = int(total or 0)
        used = int(used or 0)
    remaining = None if (total is None or used is None or unlimited_volume) else max(0, total - used)
    percent = None
    if has_snapshot:
        percent = 0 if unlimited_volume else min(100, round(used / total * 100))
    now = int(time.time())
    expire_ts = _expire_to_epoch(live.get("expire")) if has_snapshot else None
    unlimited_time = False
    if has_snapshot and not expire_ts:
        unlimited_time = True
    # PAYG: حجم و زمان محدودیت ندارد (پرداخت به‌ازای مصرف) → همیشه «نامحدود»، حتی اگر پنل مقدار دیگری برگرداند
    # یا در دسترس نباشد.
    _is_payg = str(cfg.get("type") or "").lower() == "payg" or bool(cfg.get("payg"))
    if _is_payg:
        unlimited_volume = True
        unlimited_time = True
        expire_ts = None
        total = 0
        if used is None:
            try: used = int(cfg.get("payg_last_used_bytes") or 0)
            except Exception: used = 0
        remaining = None
        percent = 0
    if not has_snapshot and not _is_payg:
        stored = cfg.get("expiry")
        if stored:
            try:
                dt = datetime.strptime(str(stored)[:10], "%Y-%m-%d")
                expire_ts = int(dt.timestamp()) + 86399
            except Exception:
                expire_ts = None
    remaining_days = None
    if expire_ts:
        left = expire_ts - now
        remaining_days = -1 if left <= 0 else -(-left // 86400)
    status = live.get("status") if has_snapshot else None
    if status in (None, "active", "on_hold"):
        if expire_ts and expire_ts <= now and has_snapshot:
            status = "expired"
        elif has_snapshot and not unlimited_volume and used >= total:
            status = "limited"
    return {
        "total": total, "used": used, "remaining": remaining, "percent": percent,
        "expire_ts": expire_ts, "remaining_days": remaining_days,
        "unlimited_volume": unlimited_volume, "unlimited_time": unlimited_time, "status": status,
    }


@app.route("/api/services", methods=["GET"])
@require_telegram_auth
def api_services():
    out = []
    _cfgs = [c for c in db.get_configs(g.user["id"]) if not c.get("deleted")]
    for cfg, panel, snapshot, err in _live_snapshots(_cfgs):
        # سرویس‌های منقضی هم نمایش داده می‌شوند (برای تمدید/حذف)؛ قبلاً حذف می‌شدند و
        # کاربر با سرویس منقضی یا هنگام خطای پنل «سرویسی یافت نشد» می‌دید.
        _expired = False
        if panel and snapshot is not None and snapshot.get("expire"):
            try:
                _expired = int(snapshot["expire"]) <= int(time.time())
            except Exception:
                pass
        elif not panel:
            try:
                _expired = bool(is_config_expired(cfg))
            except Exception:
                pass
        live = snapshot or {}
        sub_link = None
        if panel and panel.get("panel_type") == "shahrah":
            sub_link = live.get("link")
            if not sub_link:
                try: sub_link = crypto.decrypt_config(cfg["config"])
                except Exception: sub_link = None
        elif not panel:
            try: sub_link = crypto.decrypt_config(cfg["config"])
            except Exception: sub_link = None
        m = _service_metrics(cfg, live, snapshot is not None)
        out.append({
            "id": cfg["id"], "plan": cfg["plan"], "created_at": cfg["created_at"],
            "expiry": cfg["expiry"], "service_id": cfg.get("service_id"),
            "panel_type": panel.get("panel_type") if panel else cfg.get("source"),
            "panel_name": panel.get("name") if panel else None,
            "status": m["status"], "sub_link": sub_link,
            "usage": {"total": m["total"], "used": m["used"], "remaining": m["remaining"], "percent": m["percent"]},
            "remaining_days": m["remaining_days"], "unlimited_time": m["unlimited_time"],
            "unlimited_volume": m["unlimited_volume"], "expire_ts": m["expire_ts"],
            "panel_error": bool(panel and snapshot is None),
        })
        try:
            import webapp_user_ext
            webapp_user_ext.enrich_list_item(out[-1], cfg, panel, snapshot)
        except Exception:
            logging.getLogger(__name__).exception("service enrich failed")
    return jsonify({"vip": out})


@app.route("/api/services/<int:config_id>/usage", methods=["GET"])
@require_telegram_auth
def api_service_usage(config_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404
    panel, snapshot, err = _live_panel_snapshot(cfg)
    if snapshot is not None:
        m = _service_metrics(cfg, snapshot, True)
        return jsonify({"source": "panel", "panel_type": panel.get("panel_type"), "usage": {"total": m["total"], "used": m["used"], "remaining": m["remaining"], "percent": m["percent"], "expire": m["expire_ts"]}, "remaining_days": m["remaining_days"], "unlimited_time": m["unlimited_time"], "status": m["status"]})
    if panel and panel.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"):
        return jsonify({"error": "panel_unreachable", "message": err}), 502
    return jsonify({"usage": None, "remaining_days": None, "source": "panel_unavailable"})


@app.route("/api/services/<int:config_id>/qr", methods=["GET"])
@require_telegram_auth
def api_service_qr(config_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404
    content = tg_get_file_bytes(cfg["qr_file_id"]) if cfg.get("qr_file_id") else None
    if content is not None:
        return send_file(io.BytesIO(content), mimetype="image/jpeg")
    # no stored QR (or Telegram fetch failed): render one from the subscription link
    try:
        link = crypto.decrypt_config(cfg["config"])
        if link and link.lower().startswith(("http://", "https://")):
            import qrcode
            img = qrcode.QRCode(box_size=10, border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
            img.add_data(link)
            img.make(fit=True)
            buf = io.BytesIO()
            img.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png")
    except Exception:
        logging.getLogger(__name__).exception("qr render failed")
    return jsonify({"error": "not_found"}), 404


@app.route("/api/services/<int:config_id>/delete", methods=["POST"])
@require_telegram_auth
def api_service_delete(config_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404
    if cfg.get("panel_id") and cfg.get("service_id") and cfg.get("source") in panels.PANEL_TYPES:
        panel = db.get_vpn_panel(cfg["panel_id"])
        if panel:
            try:
                ok, msg = loopsafe.run(panels.delete_service(panel, cfg["service_id"]))
            except Exception as exc:
                ok, msg = False, str(exc)
            if not ok:
                return jsonify({"error": "panel_delete_failed", "message": msg}), 502
    db.set_config_deleted(config_id, True)
    invalidate_live_snapshot(cfg)
    return jsonify({"success": True})


# =============================================================================
# تمدید سرویس VIP / سرویس سفارشی («کانفیگ خودتو بساز» — فقط VIP)
# =============================================================================
def _renew_settings_for(target_config_id):
    """تنظیمات تمدیدِ ثبت‌شده‌ی ادمین (برای پلن/پوشه‌ی همین سرویس) — همان منبعی که ربات استفاده می‌کند."""
    try:
        cfg = _own_config_or_none(target_config_id, g.user["id"]) if target_config_id else None
    except Exception:
        cfg = None
    if not cfg:
        return None
    try:
        import bot_info as _bi
        pk, cid = db.resolve_config_plan(cfg)
        _co = db.config_custom_option(cfg)
        if _co is not None and not pk:
            st = _bi.get_renewal_custom_settings(_co)  # سرویس سفارشی: قیمت/محدوده‌ی همان گزینه‌ی ساخت
        else:
            st = _bi.get_renewal_plan_settings(pk, cid) if pk else _bi.get_renewal_settings(cid)
    except Exception:
        return None
    def _opts(raw):
        out = []
        for x in str(raw or "").replace("،", ",").split(","):
            try:
                v = int(float(x.strip()))
            except Exception:
                continue
            if v > 0 and v not in out:
                out.append(v)
        return out
    mode = {"day": "time", "gb": "volume", "both": "both"}.get(st.get("mode"), "time")
    return {
        "mode": mode, "min_day": int(st.get("min_day") or 1), "max_day": int(st.get("max_day") or 0),
        "min_gb": int(st.get("min_gb") or 1), "max_gb": int(st.get("max_gb") or 0),
        "price_day": float(st.get("price_day") or 0), "price_gb": int(st.get("price_gb") or 0),
        "day_options": [v for v in _opts(st.get("day_options")) if v >= int(st.get("min_day") or 1) and (not st.get("max_day") or v <= int(st["max_day"]))],
        "gb_options": [v for v in _opts(st.get("gb_options")) if v >= int(st.get("min_gb") or 1) and (not st.get("max_gb") or v <= int(st["max_gb"]))],
    }


def _custom_build_common(body, order_type, target_config_id=None, require_name=True):
    if order_type != "renew" and db.get_setting("custom_build_enabled", "1") == "0":
        return None, (jsonify({"error": "custom_build_disabled", "message": "ℹ️ ساخت سرویس سفارشی فعلاً غیرفعال است."}), 403)
    if order_type == "renew" and target_config_id:
        try:
            _tc = db.get_config_by_id(int(target_config_id))
        except Exception:
            _tc = None
        if _tc and not db.config_allows_renew(_tc):
            return None, (jsonify({"error": "renew_not_allowed", "message": db.renew_block_text()}), 403)
    volume = body.get("volume_gb")
    days = body.get("days")
    name = (body.get("name") or "").strip() or None
    renew_mode = body.get("renew_mode") if order_type == "renew" else None

    option_num = 0
    if order_type == "new":
        option_num, opt_err = _resolve_custom_option(body.get("option_num"))
        if opt_err:
            return None, opt_err
    _cb_limits = db.get_effective_custom_build_settings(option_num)
    _rs = _renew_settings_for(target_config_id) if order_type == "renew" else None
    if _rs:
        # ✅ تنظیمات تمدید ادمین (نوع، حداقل/حداکثر، قیمت هر روز/گیگ) — دقیقاً مثل ربات
        renew_mode = _rs["mode"]
        if renew_mode in ("time", "both"):
            if not isinstance(days, int) or days < _rs["min_day"] or (_rs["max_day"] and days > _rs["max_day"]):
                return None, (jsonify({"error": "invalid_days", "min_days": _rs["min_day"], "max_days": _rs["max_day"]}), 400)
        if renew_mode in ("volume", "both"):
            if not isinstance(volume, int) or volume < _rs["min_gb"] or (_rs["max_gb"] and volume > _rs["max_gb"]):
                return None, (jsonify({"error": "invalid_volume", "min_gb": _rs["min_gb"], "max_gb": _rs["max_gb"]}), 400)
        if renew_mode == "time":
            volume = 0
        elif renew_mode == "volume":
            days = 0
        price = int(round(int(volume or 0) * _rs["price_gb"] + int(days or 0) * _rs["price_day"]))
        return {"volume": volume, "days": days, "name": name, "price": price, "agent_discount_applied": False,
                "renew_mode": renew_mode, "option_num": option_num}, None
    if order_type == "renew":
        if renew_mode not in ("time", "volume", "both"):
            renew_mode = "both"
        # تمدید زمان: فقط روز، حداقل ۳۰ روز
        if renew_mode == "time":
            if not isinstance(days, int) or not (30 <= days <= _cb_limits["max_days"]):
                return None, (jsonify({"error": "invalid_days", "min_days": 30}), 400)
            volume = 0
        # تمدید حجم: فقط حجم، حداقل ۵ گیگ
        elif renew_mode == "volume":
            if not isinstance(volume, int) or not (5 <= volume <= _cb_limits["max_gb"]):
                return None, (jsonify({"error": "invalid_volume", "min_gb": 5}), 400)
            days = 0
        else:
            if not isinstance(volume, int) or not (_cb_limits["min_gb"] <= volume <= _cb_limits["max_gb"]):
                return None, (jsonify({"error": "invalid_volume"}), 400)
            if not isinstance(days, int) or not (_cb_limits["min_days"] <= days <= _cb_limits["max_days"]):
                return None, (jsonify({"error": "invalid_days"}), 400)
    else:
        if not isinstance(volume, int) or not (_cb_limits["min_gb"] <= volume <= _cb_limits["max_gb"]):
            return None, (jsonify({"error": "invalid_volume"}), 400)
        if not isinstance(days, int) or not (_cb_limits["min_days"] <= days <= _cb_limits["max_days"]):
            return None, (jsonify({"error": "invalid_days"}), 400)

    if require_name and order_type == "new":
        if not name or not _LATIN_NAME_RE.match(name):
            return None, (jsonify({"error": "invalid_name"}), 400)

    price, agent_discount_applied = _calc_custom_price(volume, days, g.tg_user["id"], option_num=option_num)
    return {
        "volume": volume, "days": days, "name": name, "price": price,
        "agent_discount_applied": agent_discount_applied, "renew_mode": renew_mode,
        "option_num": option_num,
    }, None


def _resolve_custom_option(raw):
    """Same rule as the bot (_cbuild_begin): one usable option -> it is implied; several -> must pick a valid one."""
    opts = db.usable_map_options("custom_build")
    nums = [int(o["num"]) for o in opts]
    if raw is None or raw == "":
        if len(nums) > 1:
            return 0, (jsonify({"success": False, "error": "invalid_option", "message": "یکی از گزینه‌های سرویس را انتخاب کنید."}), 400)
        return (nums[0] if nums else 0), None
    try:
        num = int(raw)
    except (TypeError, ValueError):
        return 0, (jsonify({"success": False, "error": "invalid_option", "message": "گزینه‌ی انتخاب‌شده نامعتبر است."}), 400)
    if nums and num not in nums:
        return 0, (jsonify({"success": False, "error": "invalid_option", "message": "این گزینه دیگر در دسترس نیست."}), 400)
    if not nums and num != 0:
        return 0, (jsonify({"success": False, "error": "invalid_option", "message": "این گزینه دیگر در دسترس نیست."}), 400)
    return num, None


@app.route("/api/custom-build/options", methods=["GET"])
@require_telegram_auth
def api_custom_build_options():
    if db.get_setting("custom_build_enabled", "1") == "0":
        return jsonify({"options": [], "disabled": True})
    out = []
    for o in db.usable_map_options("custom_build"):
        _cb = db.get_effective_custom_build_settings(int(o["num"]))
        out.append({"num": int(o["num"]), "name": o.get("name") or f"گزینه {o['num']}", **{k: _cb[k] for k in ("price_per_gb", "price_per_30_days", "min_gb", "max_gb", "min_days", "max_days")}})
    return jsonify({"options": out})


@app.route("/api/custom-build/limits", methods=["GET"])
@require_telegram_auth
def api_custom_build_limits():
    try:
        _on = int(request.args.get("option_num") or 0)
    except ValueError:
        _on = 0
    _cb = db.get_effective_custom_build_settings(_on)
    return jsonify({
        "min_gb": _cb["min_gb"],
        "max_gb": _cb["max_gb"],
        "min_days": _cb["min_days"],
        "max_days": _cb["max_days"],
        "price_per_gb": _cb["price_per_gb"],
        "price_per_30_days": _cb["price_per_30_days"],
        "online_payment_enabled": _uq_enabled(),
        "online_gateways": [{"key": k, "title": og.title(k), "min_amount": og.min_amount(k)} for k in og.enabled_gateways()],
    })


@app.route("/api/services/<int:config_id>/renew-settings", methods=["GET"])
@require_telegram_auth
def api_service_renew_settings(config_id):
    st = _renew_settings_for(config_id)
    if st is None:
        return jsonify({"error": "service_not_found"}), 404
    return jsonify(st)


@app.route("/api/custom-build/quote", methods=["POST"])
@require_telegram_auth
def api_custom_build_quote():
    body = request.get_json(silent=True) or {}
    data, err = _custom_build_common(body, body.get("order_type", "new"), body.get("target_config_id"), require_name=False)
    if err:
        return err
    return jsonify({"price": data["price"], "agent_discount_applied": data["agent_discount_applied"]})


@app.route("/api/custom-build/wallet", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_custom_build_wallet():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    order_type = body.get("order_type", "new")
    target_config_id = body.get("target_config_id")
    renew_mode = body.get("renew_mode")

    if order_type == "renew":
        cfg = _own_config_or_none(target_config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "service_not_found"}), 404
        if not db.config_allows_renew(cfg):
            return jsonify({"error": "renew_not_allowed", "message": db.renew_block_text()}), 403

    data, err = _custom_build_common(body, order_type, target_config_id)
    if err:
        return err

    user = g.user
    lock_key = f"mini-custom-wallet:{user['id']}:{order_type}:{target_config_id}:{data['volume']}:{data['days']}:{renew_mode}"
    if not db.claim_purchase_action(lock_key, ttl_seconds=5):
        return jsonify({"error": "duplicate_request"}), 409
    if user["wallet"] < data["price"]:
        return jsonify({
            "error": "insufficient_balance", "price": data["price"],
            "wallet": user["wallet"], "needed": data["price"] - user["wallet"],
        }), 402

    desc = "خرید سرویس سفارشی" if order_type == "new" else "تمدید سرویس"
    _ok, referral_funded_purchase = db.deduct_wallet_for_purchase(user["id"], data["price"], desc)
    if not _ok:
        return jsonify({"error": "insufficient_balance"}), 402

    order_id = db.create_custom_order(
        user["id"], data["volume"], data["days"], data["name"], data["price"], order_type, target_config_id, renew_mode=data.get("renew_mode"),
        payment_method="کیف پول",
        option_num=data.get("option_num") or 0,
    )
    db.set_custom_order_status(order_id, "paid")

    if db.referral_purchase_qualifies(data["volume"], paid_purchase=True, is_free_test=False):
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت - Mini App)"

    # 🐛 فیکس: مثل خرید VIP، «بساز سرویس خودت» از کیف‌پول در مینی‌اپ هم قرار بود
    # طبق منطق ربات کلاسیک (handlers/plans.py، فراخوانی auto_fulfill_custom_via_panel)
    # اگر شاهراه فعال و نگاشت پیش‌فرض custom_build تنظیم شده باشد، به‌صورت
    # کاملاً خودکار ساخته و ارسال شود؛ ولی اینجا این مسیر اصلاً صدا زده نمی‌شود.
    tg_notify_user(user["telegram_id"], "cbuild_pay_wallet", "✅ پرداخت موفق! سرویس شما به‌زودی ساخته و ارسال می‌شود.")
    handled = _run_async_with_temp_bot(
        lambda bot: auto_fulfill_custom_via_panel(
            bot, user, order_id, data["volume"], data["days"], data["name"]
        )
    )

    if handled:
        if order_type == "renew" and target_config_id:
            db.set_config_referral_funded(int(target_config_id), bool(referral_funded_purchase))
        elif referral_funded_purchase:
            db.mark_latest_config_referral_funded(user["id"], "vip")

    if not handled:
        from keyboards import admin_custom_order_notify_keyboard
        tg_send_message(
            ADMIN_ID,
            f"🛠 {label}!\n\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
            f"📦 حجم: {data['volume']} گیگ\n⏳ مدت: {data['days']} روز\n"
            + (f"🔤 نام: {data['name']}\n" if data["name"] else "")
            + f"💰 {data['price']:,} تومان (پرداخت‌شده از کیف پول)\n🔢 شماره سفارش: {order_id}",
            reply_markup=_kb_json(admin_custom_order_notify_keyboard(order_id)),
        )
    return jsonify({"success": True, "order_id": order_id, "price": data["price"]})


@app.route("/api/custom-build/online/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_custom_build_online_create():
    """پرداخت آنلاین «بساز سرویس خودت / تمدید» (UniquePay یا FrenzyEx/Tronado) — معادل cbuild_pay_online / cbuild_pay_fx|tr ربات."""
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403
    gw = _requested_new_gateway()
    if gw:
        if not og.enabled(gw):
            return jsonify({"error": "gateway_disabled"}), 400
    elif not _uq_enabled():
        return jsonify({"error": "gateway_disabled"}), 400
    body = request.get_json(silent=True) or {}
    order_type = body.get("order_type", "new")
    target_config_id = body.get("target_config_id")
    if order_type == "renew" and _own_config_or_none(target_config_id, g.user["id"]) is None:
        return jsonify({"error": "service_not_found"}), 404
    data, err = _custom_build_common(body, order_type, target_config_id)
    if err:
        return err
    user = g.user
    price = int(data["price"])
    if gw:
        if not og.amount_ok(gw, price):
            return jsonify({"error": "amount_too_low", "min_amount": og.min_amount(gw)}), 400
    elif price <= ONLINE_PAYMENT_MIN_AMOUNT:
        return jsonify({"error": "amount_too_low", "min_amount": ONLINE_PAYMENT_MIN_AMOUNT}), 400
    lock_key = f"mini-custom-online:{user['id']}:{order_type}:{target_config_id}:{data['volume']}:{data['days']}"
    if not db.claim_purchase_action(lock_key, ttl_seconds=5):
        return jsonify({"error": "duplicate_request"}), 409
    extra = json.dumps({
        "volume": data["volume"], "days": data["days"], "custom_name": data["name"], "order_type": order_type,
        "target_config_id": target_config_id, "renew_mode": data.get("renew_mode"), "option_num": data.get("option_num") or 0,
    })
    plan_name = data["name"] or "سرویس سفارشی (بساز سرویس خودت)"
    if gw:
        ref = og.new_ref("miniapp-cbuild", gw)
        res = og.create_payment_sync(gw, ref, price, f"سرویس سفارشی {data['volume']}GB/{data['days']}d", buyer_id=user["telegram_id"])
        if not res.ok:
            return jsonify({"error": "gateway_error", "detail": res.error}), 502
        try:
            payment_id = db.create_online_payment(
                user_id=user["id"], telegram_id=str(user["telegram_id"]), hash_id=ref, plan_name=plan_name,
                price=price, order_type="custom", plan_key=None, kind="custom", payment_link=res.link,
                ref_id=res.ref_id, gateway=gw, gw_data=res.gw_data, extra=extra,
            )
        except Exception:
            logging.getLogger(__name__).exception("ثبت فاکتور سفارشی %s (Mini App) ناموفق بود", gw)
            og.cancel_payment_sync({"gateway": gw, "ref_id": res.ref_id})
            return jsonify({"error": "gateway_error"}), 500
        return _gw_create_payload(payment_id, res.link, price, res)
    hash_id = uniquepay_sync.new_hash_id("miniapp-cbuild")
    invoice = uniquepay_sync.create_invoice(hash_id, price)
    payment_link = uniquepay_cfg.pick_link(invoice) if invoice else None
    if not payment_link:
        return jsonify({"error": "gateway_error"}), 502
    payment_id = db.create_online_payment(
        user_id=user["id"], telegram_id=str(user["telegram_id"]), hash_id=hash_id, plan_name=plan_name, price=price,
        order_type="custom", plan_key=None, kind="custom", payment_link=payment_link,
        ref_id=str(invoice.get("refId")), extra=extra,
    )
    return jsonify({
        "payment_id": payment_id, "payment_link": payment_link, "price": price,
        "white_label": uniquepay_sync.white_label_data(invoice),
        "expires_at": db.online_payment_expires_at(db.get_online_payment(payment_id)["created_at"]),
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
    })


@app.route("/api/custom-build/card/info", methods=["POST"])
@require_telegram_auth
def api_custom_build_card_info():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    order_type = body.get("order_type", "new")
    target_config_id = body.get("target_config_id")
    if order_type == "renew" and _own_config_or_none(target_config_id, g.user["id"]) is None:
        return jsonify({"error": "service_not_found"}), 404
    data, err = _custom_build_common(body, order_type, target_config_id, require_name=False)
    if err:
        return err
    inv_price = apply_random_amount_variation(data["price"])
    invoice = db.create_invoice(
        user_id=g.user["id"], telegram_id=str(g.user["telegram_id"]),
        kind="custom_card", label="سرویس سفارشی", price=inv_price,
    )
    return jsonify({
        "card_number": bot_info.get("card_number"), "card_holder": bot_info.get("card_holder"), "price": inv_price,
        "invoice_id": invoice["id"], "expires_at": invoice["expires_at"], **_card_ui_flags(),
    })


@app.route("/api/custom-build/card/receipt", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_custom_build_card_receipt():
    # 🐛 فیکس: مشابه مورد api_order_card_receipt، این endpoint هم قبلاً orders_enabled
    # را دوباره بررسی نمی‌کرد (فقط api_custom_build_card_info این بررسی را دارد).
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    order_type = request.form.get("order_type", "new")
    target_config_id = request.form.get("target_config_id")
    target_config_id = int(target_config_id) if target_config_id and target_config_id.isdigit() else None
    photo = request.files.get("receipt")
    invoice_id = request.form.get("invoice_id", type=int)
    if photo is not None and not _valid_receipt_photo(photo):
        return jsonify({"error": "invalid_file_type"}), 400

    try:
        volume = int(request.form.get("volume_gb", ""))
        days = int(request.form.get("days", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_request"}), 400
    name = (request.form.get("name") or "").strip() or None

    if order_type == "renew":
        cfg = _own_config_or_none(target_config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "service_not_found"}), 404
        if not db.config_allows_renew(cfg):
            return jsonify({"error": "renew_not_allowed", "message": db.renew_block_text()}), 403
    renew_mode = request.form.get("renew_mode") if order_type == "renew" else None
    data, err = _custom_build_common({"volume_gb": volume, "days": days, "name": name, "renew_mode": renew_mode, "option_num": request.form.get("option_num")}, order_type, target_config_id)
    if err:
        return err
    volume, days, name, price = data["volume"], data["days"], data["name"], data["price"]
    if photo is None:
        return jsonify({"error": "receipt_required"}), 400

    _inv = db.consume_invoice(invoice_id) if invoice_id else None
    if _inv is None:
        return jsonify({"error": "invoice_expired"}), 409
    try:
        if int(_inv.get("price") or 0) > 0:
            price = int(_inv["price"])
    except Exception:
        pass

    user = g.user
    order_id = db.create_custom_order(user["id"], volume, days, name, price, order_type, target_config_id, renew_mode=data.get("renew_mode"), payment_method="کارت به کارت", option_num=data.get("option_num") or 0)

    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت - Mini App)"
    caption = (
        f"💳 رسید {label}\n\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
        f"📦 حجم: {volume} گیگ\n⏳ مدت: {days} روز\n"
        + (f"🔤 نام: {name}\n" if name else "")
        + f"💰 {price:,} تومان\n🔢 شماره سفارش: {order_id}"
    )
    approve_kb = inline_kb([
        ("✅ تأیید پرداخت", f"approvecustom_{order_id}"),
        ("❌ رد رسید", f"rejectcustom_{order_id}"),
    ])
    _sent = tg_send_photo_bytes(ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=approve_kb)
    if isinstance(_sent, str):
        try:
            db.set_custom_order_receipt(order_id, file_id=_sent)
        except Exception:
            logging.getLogger(__name__).exception("ذخیره‌ی file_id رسید سفارش سفارشی Mini App ناموفق بود")

    if invoice_id:
        db.delete_invoice(invoice_id)

    return jsonify({"success": True, "order_id": order_id, "price": price})


# =============================================================================
# دعوت دوستان
# =============================================================================
def _referral_rule_payload() -> dict:
    rs = db.get_referral_settings()
    return {
        "rule": ("any" if not rs["paid_purchase_required"] else ("min_volume" if rs["min_volume_enabled"] else "paid")),
        "min_purchase_gb": rs["min_volume_gb"],
        "reward_amount": rs["reward_amount"],
    }


@app.route("/api/referral", methods=["GET"])
@require_telegram_auth
def api_referral():
    u = g.user
    if not db.referral_enabled():
        return jsonify({"error": "referral_disabled"}), 403
    stats = db.get_referral_stats(u["id"])
    _rs = db.get_referral_settings()
    return jsonify({
        "invite_link": f"https://t.me/{bot_info.get('bot_username')}?start={stats['invite_code']}",
        "invite_code": stats["invite_code"],
        "invited_count": stats["invited_count"],
        "successful_invites": stats["successful_invites"],
        "released_amount": stats["released_amount"],
        "locked_wallet": u["locked_wallet"],
        "reward_amount": _rs["reward_amount"],
        "min_purchase_gb": _rs["min_volume_gb"],
        # شرط واقعی آزادسازی پاداش طبق تنظیمات ربات/ادمین
        "rule": ("any" if not _rs["paid_purchase_required"] else ("min_volume" if _rs["min_volume_enabled"] else "paid")),
        "min_volume_enabled": _rs["min_volume_enabled"],
        "paid_purchase_required": _rs["paid_purchase_required"],
        "channel_leave_lock": _rs["channel_leave_lock_enabled"],
        "penalty_enabled": _rs["channel_leave_penalty_enabled"],
        "penalty_wallet_amount": _rs["penalty_wallet_amount"],
        "penalty_service_gb": _rs["penalty_service_gb"],
        "pending_count": stats["pending_count"],
    })


# =============================================================================
# پشتیبانی (تیکت متنی)
# =============================================================================
@app.route("/api/support/ticket", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_support_ticket():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text_required"}), 400
    if len(text) > 2000:
        return jsonify({"error": "text_too_long"}), 400

    user = g.user
    tg_send_message(
        ADMIN_ID,
        f"🎫 تیکت جدید (Mini App)\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n\n💬 {text}",
        reply_markup=inline_kb([("↩️ پاسخ", f"replyticket_{user['telegram_id']}")]),
    )
    return jsonify({"success": True})


@app.route("/api/support/info", methods=["GET"])
@require_telegram_auth
def api_support_info():
    try:
        guides = [{"id": x["id"], "title": x["title"], "body": x.get("body_text") or "", "type": x.get("content_type") or "text"}
                  for x in db.get_guides() if (x.get("content_type") or "text") == "text" and (x.get("body_text") or "").strip()]
    except Exception:
        guides = []
    return jsonify({"channel_url": bot_info.get_support_url(), "guide_url": bot_info.get("connection_guide_url"), "guides": guides})


def _external_miniapp_url(path: str = ""):
    """URL مینی‌اپ که جدا از Backend دیپلوی شده است."""
    base = (os.environ.get("MINIAPP_WEBAPP_URL", "") or "").strip().rstrip("/")
    if not base:
        try:
            base = (db.get_setting("miniapp_webapp_url", "") or "").strip().rstrip("/")
        except Exception:
            base = ""
    if not base:
        return ""
    if path:
        return base + "/" + path.lstrip("/")
    return base


# =============================================================================
# ماژول‌های توسعه‌یافته‌ی API (ادمین / نماینده)؛ هر ماژول register(app, api) دارد.
# =============================================================================
def _register_extensions():
    import importlib, sys
    for name in ("webapp_user_ext", "webapp_payg", "webapp_crypto", "webapp_admin_a", "webapp_admin_b", "webapp_admin_c", "webapp_admin_d", "webapp_admin_e", "webapp_admin_f", "webapp_reseller"):
        try:
            mod = importlib.import_module(name)
            mod.register(app, sys.modules[__name__])
        except ModuleNotFoundError as exc:
            if exc.name != name:
                logging.getLogger(__name__).exception("extension %s failed", name)
        except Exception:
            logging.getLogger(__name__).exception("extension %s failed", name)


_register_extensions()


@app.route("/admin-panel")
@app.route("/admin-panel/")
@app.route("/admin")
@app.route("/reseller")
def redirect_to_external_miniapp():
    """Backend فقط API است؛ Frontend مینی‌اپ جداگانه دیپلوی می‌شود."""
    target = _external_miniapp_url(request.path.rstrip("/") or "")
    if not target:
        return jsonify({"error": "MINIAPP_WEBAPP_URL is not configured"}), 503
    return redirect(target, code=302)


if __name__ == "__main__":

    db.init_db()
    bot_info.migrate_card_to_payment_settings()
    # برای اجرای واقعی پروداکشن به‌جای این، از gunicorn استفاده کنید، مثلا:
    # gunicorn -w 2 -b 0.0.0.0:8000 webapp_api:app
    app.run(host="0.0.0.0", port=int(os.environ.get("WEBAPP_API_PORT", "8000")))
