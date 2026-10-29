"""
subscription.py
دریافت زنده‌ی اطلاعات مصرف (حجم، تاریخ انقضا، نام سرویس) از روی لینک ساب کاربر،
بدون نیاز به هیچ دسترسی به دیتابیس یا API پنل.

توضیح فنی:
اکثر پنل‌های V2Ray/X-UI/Marzban/Hiddify و مشابه، وقتی یک درخواست GET به لینک ساب
زده شود (دقیقاً همان کاری که اپ‌های کلاینت مثل v2rayNG برای نمایش حجم باقی‌مانده
انجام می‌دهند)، یک هدر استاندارد به نام Subscription-Userinfo برمی‌گردانند؛
چیزی شبیه:
    upload=1073741824; download=2147483648; total=53687091200; expire=1751328000
همچنین بسیاری از پنل‌ها هدر Profile-Title را هم برمی‌گردانند که نام سرویس را
به‌صورت base64 دارد.

بعضی پنل‌ها (مثل Hiddify) به‌جای لینک خام ساب، یک لینک «نمایش در مرورگر»
(چیزی شبیه down.hplo.ir/view?...) می‌دهند که یک صفحه‌ی HTML برمی‌گرداند، نه
هدرهای بالا. در این حالت باید لینک ساب واقعی را از داخل همان صفحه پیدا کرد.
این ماژول این حالت را هم به‌صورت best-effort پوشش می‌دهد.

⚠️ توجه: چون این محیط به اینترنت دسترسی ندارد، این بخش قابل تست مستقیم روی
لینک‌های واقعی نبوده؛ اگر باز هم لینک‌های down.hplo.ir جواب ندادند، لطفاً یک
نمونه لینک واقعی (یا خروجی که مرورگر/curl از آن می‌گیرد) بفرست تا دقیق‌تر اصلاح شود.
"""

import base64
import re
from datetime import datetime

from utils import TEHRAN_TZ, now_tehran_naive

import aiohttp

import loopsafe

# هدرهایی که شبیه یک کلاینت واقعی V2Ray/Clash هستند؛ خیلی از پنل‌ها بدون
# User-Agent مناسب، درخواست را رد می‌کنند یا صفحه‌ی HTML عادی برمی‌گردانند.
_CLIENT_HEADERS = {
    "User-Agent": "v2rayNG/1.8.29 (Linux; Android)",
    "Accept": "*/*",
}

_SUB_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

# اسکیم‌های پروتکل‌های کانفیگ تکی که ممکن است داخل بدنه‌ی یک لینک ساب باشند.
_CONFIG_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "hysteria://", "hysteria2://", "hy2://", "tuic://")

# perf: قبلاً هر بار که یک لینک ساب خونده می‌شد (یعنی هر بار که یک کاربر
# "سرویس‌های من" رو باز می‌کرد)، یک aiohttp.ClientSession کاملاً تازه ساخته
# می‌شد. ساخت Session جدید یعنی یک TCP handshake و (برای https) یک TLS
# handshake کاملاً از صفر برای *هر* درخواست — این می‌تونه صدها میلی‌ثانیه به
# هر درخواست اضافه کنه، دقیقاً برخلاف مرزبان/پاسارگارد که یک Session مشترک
# و reuse-شده دارن. الان همون الگو اینجا هم پیاده شده: یک Session مشترک با
# connection pool و DNS cache، که بین درخواست‌های پشت‌سرهم reuse می‌شه.
_shared_session: aiohttp.ClientSession | None = None


async def _get_shared_session() -> aiohttp.ClientSession:
    global _shared_session
    if loopsafe.usable(_shared_session):
        return _shared_session
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, keepalive_timeout=60)
    _shared_session = aiohttp.ClientSession(headers=_CLIENT_HEADERS, connector=connector)
    return _shared_session


async def _get(session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout | None = None):
    # اعتبارسنجی TLS برای حفاظت از لینک محرمانه اشتراک فعال است.
    async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
        headers = dict(resp.headers)
        try:
            body = await resp.text(errors="ignore")
        except Exception:
            body = ""
        return resp.status, headers, body, str(resp.url)


def _looks_like_html(body: str) -> bool:
    head = (body or "").strip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head" in head


def _decode_profile_title(headers: dict) -> str | None:
    raw = headers.get("Profile-Title") or headers.get("profile-title")
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower().startswith("base64:"):
        raw = raw[7:]
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", errors="ignore").strip()
    except Exception:
        return raw or None


def _parse_userinfo(headers: dict) -> dict | None:
    header = headers.get("Subscription-Userinfo") or headers.get("subscription-userinfo")
    if not header:
        return None
    info = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            try:
                info[key.strip()] = int(value.strip())
            except ValueError:
                pass
    return info if info else None


def _extract_name_from_body(body: str) -> str | None:
    """اگر بدنه یک لینک ساب خام باشد (base64 از چند کانفیگ)، تلاش می‌کند
    از remark (بعد از #) اولین کانفیگ، یک اسم دربیاورد."""
    if not body:
        return None
    candidate = body.strip()
    decoded = None
    try:
        decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        decoded = None

    text = decoded if decoded and ("://" in decoded) else (candidate if "://" in candidate else None)
    if not text:
        return None

    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if "#" in first_line:
        from urllib.parse import unquote
        remark = first_line.split("#", 1)[1].strip()
        remark = unquote(remark)
        if remark:
            return remark
    return None


def _find_embedded_sub_link(html: str) -> str | None:
    """در صفحات «نمایش در مرورگر» (مثل Hiddify) دنبال لینک ساب واقعی درون HTML/JS می‌گردد."""
    if not html:
        return None
    candidates = _SUB_URL_PATTERN.findall(html)
    # اولویت با لینک‌هایی که به نظر لینک ساب واقعی می‌رسند (نه فایل‌های استاتیک/آیکون)
    for url in candidates:
        low = url.lower()
        if any(bad in low for bad in [".png", ".jpg", ".css", ".js", ".ico", ".svg", ".woff"]):
            continue
        if any(good in low for good in ["/sub", "/api/", "sub/", "subscribe"]):
            return url.rstrip("\"'<>),.;")
    return None


async def fetch_subscription_info(sub_url: str) -> dict | None:
    """نسخه‌ی سازگار قبلی: فقط upload/download/total/expire را برمی‌گرداند."""
    meta = await extract_meta(sub_url)
    return meta.get("userinfo") if meta else None


async def extract_meta(sub_url: str, _depth: int = 0, _retry: int = 0) -> dict | None:
    """
    اطلاعات کامل یک لینک ساب را برمی‌گرداند:
        {
            "userinfo": {"upload":.., "download":.., "total":.., "expire":..} یا None,
            "name": "..." یا None,
            "final_url": "...",  # لینک نهایی بعد از دنبال کردن ریدایرکت‌ها
        }
    اگر لینک اصلاً قابل‌دسترسی نبود، None برمی‌گرداند.
    """
    if not sub_url or not sub_url.strip().lower().startswith(("http://", "https://")):
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        session = await _get_shared_session()
        status, headers, body, final_url = await _get(session, sub_url.strip(), timeout=timeout)

        userinfo = _parse_userinfo(headers)
        name = _decode_profile_title(headers)

        if userinfo or name:
            if not name:
                name = _extract_name_from_body(body)
            return {"userinfo": userinfo, "name": name, "final_url": final_url}

        # اگر هدرهای موردنظر نیامدند و صفحه شبیه HTML بود (مثلاً صفحه‌ی
        # «نمایش در مرورگر» مثل down.hplo.ir/view)، دنبال لینک ساب واقعی
        # درون همان صفحه می‌گردیم و یک‌بار دیگر تلاش می‌کنیم.
        if _looks_like_html(body) and _depth == 0:
            embedded = _find_embedded_sub_link(body)
            if embedded and embedded != sub_url:
                return await extract_meta(embedded, _depth=1)

        # حالت آخر: شاید بدنه خودش یک ساب خام base64 باشد بدون هدر مفید.
        name = _extract_name_from_body(body)
        if name:
            return {"userinfo": None, "name": name, "final_url": final_url}

        return None
    except Exception:
        # بعضی پنل‌ها (مخصوصاً لینک‌های میرور/نمایش‌در‌مرورگر) موقع اولین
        # درخواست کند یا موقتاً ناپایدار هستند؛ درست مثل extract_configs،
        # قبل از اعلام شکست نهایی یک‌بار دیگر تلاش می‌کنیم. این دقیقاً همان
        # نامتقارنی‌ای بود که باعث می‌شد یک لینک در مینی‌اپ (که extract_configs
        # را با retry صدا می‌زد) جواب بدهد ولی در ربات (که فقط extract_meta
        # بدون retry را صدا می‌زد) شکست بخورد.
        if _retry == 0:
            return await extract_meta(sub_url, _depth=_depth, _retry=1)
        return None


def _configs_in_text(text: str) -> list[str]:
    """هر خطی که با یکی از اسکیم‌های کانفیگ شروع شود را برمی‌گرداند (برای استفاده‌ی مشترک در چند جای این فایل)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return [ln for ln in lines if ln.startswith(_CONFIG_SCHEMES)]


def _try_base64_decode(text: str) -> str | None:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        return None


def _configs_from_json_envelope(text: str) -> list[str]:
    """برخی پنل‌ها به‌جای متن خام/base64، یک خروجی JSON برمی‌گردانند
    (مثلاً یک لیست ساده از رشته‌های کانفیگ یا یک دیکشنری با کلیدی مثل configs/links/data/result).
    بدون یک نمونه‌ی واقعی از این قالب، بی‌ضرر بوده و فقط best-effort است."""
    import json as _json
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return []
    try:
        data = _json.loads(stripped)
    except Exception:
        return []

    found: list[str] = []

    def _walk(node):
        if isinstance(node, str):
            s = node.strip()
            if s.startswith(_CONFIG_SCHEMES):
                found.append(s)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            for key in ("configs", "links", "data", "result", "items", "proxies", "subscriptions"):
                if key in node:
                    _walk(node[key])
            # اگر هیچکدام از کلیدهای شناخته‌شده نبود، همه‌ی مقادیر رو بررسی کن (best-effort)
            if not found:
                for v in node.values():
                    _walk(v)

    _walk(data)
    return found


def _parse_configs(body: str) -> list[str]:
    """بدنه‌ی خام لینک ساب را به لیست کانفیگ‌های تکی تبدیل می‌کند؛ چند قالب متفاوت را به این ترتیب پشتیبانی می‌کند (هر کدام فقط وقتی قبلی چیزی پیدا نشده امتحان می‌شود، تا داده‌های معتبر خراب نشود):
    ۱) متن خام (بدون هیچ رمزگشایی) — اگر بدنه از قبل شامل خطوط کانفیگ خام باشد
    ۲) اگر چند خط دارد: هر خط جداگانه خودش base64-شده (برخی پنل‌ها هر سطر را جداگانه رمزنگاری می‌کنند)
    ۳) base64 کل بدنه (متداول‌ترین فرمت v2rayNG/Clash)
    ۴) یک خروجی JSON شامل لیستی از کانفیگ‌ها

    ترتیب بالا عمدی است: وقتی چندین خط داریم (۲)، decode-کردن کل بدنه به-عنوان یک بلاک واحد (۳) می‌تواند به-خاطر از-بین-رفتن padding بین خطوط، خروجی مخدوش/آشغال بدهد، برای همین قبل از تلاش (۳) بررسی می‌شود.
    """
    if not body:
        return []
    # حذف BOM/کاراکترهای نامرئی احتمالی که بعضی پنل‌ها اول پاسخ می‌فرستند
    text = body.strip().lstrip("﻿​‌")

    # تلاش ۱: متن قبلاً مستقیم بدون هیچ رمزگشایی
    found = _configs_in_text(text)
    if found:
        return found

    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # تلاش ۲: فقط وقتی چندین خط داریم، هر خط را جداگانه base64 دیکود کن تا قبل از اینکه دیکود-کل بدنه به اشتباه خطوط را درهم بکوبد (چون padding هر خط مستقل است)
    if len(raw_lines) > 1:
        per_line_found: list[str] = []
        for ln in raw_lines:
            if len(ln) < 8:
                continue
            dec_line = _try_base64_decode(ln)
            if dec_line and dec_line.strip().startswith(_CONFIG_SCHEMES):
                per_line_found.append(dec_line.strip())
        if per_line_found:
            return per_line_found

    # تلاش ۳: کل بدنه را یک بلاک base64 واحد فرض کن (متداول‌ترین فرمت)
    decoded = _try_base64_decode(text)
    if decoded:
        found = _configs_in_text(decoded)
        if found:
            return found

    # تلاش ۴: خروجی JSON
    found = _configs_from_json_envelope(decoded or text)
    if found:
        return found

    return []


async def fetch_raw_preview(sub_url: str, max_len: int = 400) -> str | None:
    """فقط برای تشخیص: وقتی هیچ کانفیگی پیدا نمی‌شود، این تابع یک نمونه‌ی کوچک از بدنه‌ی خام پاسخ
    را برمی‌گرداند تا به ادمین ارسال شود و فرمت واقعی پاسخ مشخص شود."""
    try:
        timeout = aiohttp.ClientTimeout(total=15, connect=8)
        session = await _get_shared_session()
        status, headers, body, final_url = await _get(session, sub_url.strip(), timeout=timeout)
        preview = (body or "")[:max_len]
        return f"status={status} content-type={headers.get('Content-Type') or headers.get('content-type')} preview={preview!r}"
    except Exception as e:
        return f"خطا در fetch_raw_preview: {e}"


def format_service_package(volume_gb, days, plan_key: str | None = None) -> tuple[str, str]:
    """متن نمایشی حجم/مدت سرویس را می‌سازد. برای پلن تست رایگان (FREE_TEST_PLAN_KEY) به‌جای
    گیگابایت/روز خام (که برای مقادیر کوچک اعشاری مثل 0.0416 زشت و گیج‌کننده می‌شود)
    از مگابایت/ساعت خوانا استفاده می‌کند؛ برای بقیه‌ی پلن‌ها همان قالب قبلی حفظ می‌شود.
    """
    from database import is_free_test_key
    if is_free_test_key(plan_key) and volume_gb is not None and days is not None:
        volume_mb = round(volume_gb * 1024)
        if volume_mb < 1024:
            volume_text = f"{volume_mb} مگابایت"
        else:
            gb_value = volume_mb / 1024
            volume_text = f"{gb_value:.0f} گیگابایت" if gb_value == int(gb_value) else f"{gb_value:.2f} گیگابایت"
        hours = days * 24
        if hours < 24:
            hv = int(round(hours)) if float(hours) == int(round(hours)) else round(hours, 1)
            days_text = f"{hv} ساعت"
        else:
            dv = int(days) if float(days) == int(days) else round(days, 2)
            days_text = f"{dv} روز"
        return volume_text, days_text
    if volume_gb is None and plan_key:
        # اگر حجم از پنل خوانده نشد، از خود پلن بخوان (حجم ۰ در پلن = نامحدود)
        try:
            import database as _db
            _pl = (_db.get_effective_plan(plan_key) if hasattr(_db, "get_effective_plan") else None) or _db.get_vip_plan(plan_key)
            if _pl is not None:
                volume_gb = float(_pl.get("volume_gb") or 0)
                if days is None:
                    days = _pl.get("days")
        except Exception:
            pass
    if volume_gb is None:
        volume_text = "طبق بسته‌ی انتخابی"
    elif not volume_gb:
        volume_text = "نامحدود"
    else:
        _g = float(volume_gb)
        volume_text = f"{int(_g) if _g == int(_g) else round(_g, 2)} گیگابایت"
    days_text = f"{int(days) if days and float(days) == int(days) else days} روز" if days else "نامحدود"
    return volume_text, days_text


def service_users_text(plan_key: str | None) -> str:
    """تعداد کاربر همزمان (HWID) طبق پلن انتخابی؛ ۰/نامشخص = نامحدود."""
    try:
        import database as _db
        _pl = None
        if plan_key:
            _pl = (_db.get_effective_plan(plan_key) if hasattr(_db, "get_effective_plan") else None) or _db.get_vip_plan(plan_key)
        n = int((_pl or {}).get("user_limit") or 0)
        return str(n) if n > 0 else "نامحدود"
    except Exception:
        return "نامحدود"


async def extract_configs(sub_url: str, _depth: int = 0, _retry: int = 0) -> list[str] | None:
    """کانفیگ‌های تکی (vmess/vless/trojan/...) را از داخل یک لینک ساب استخراج
    می‌کند، حتی اگر پشت یک صفحه‌ی میرور «نمایش در مرورگر» مثل down.hplo.ir/view
    پنهان شده باشد (همان منطق extract_meta برای پیدا کردن لینک واقعی).

    این تابع دقیقاً همان چیزی است که هم ربات (mirrorconfigs_) و هم مینی‌اپ
    (api/services/<id>/configs) استفاده می‌کنند؛ اگر روی مینی‌اپ جواب می‌دهد ولی
    روی ربات نه، معمولاً به‌خاطر timeout کوتاه یا یک تلاش ناموفق تک‌مرحله‌ای است
    (که اینجا با افزایش timeout و یک retry خودکار جبران شده).

    خروجی:
        None            → لینک اصلاً قابل‌دسترسی نبود (حتی بعد از retry)
        []              → لینک باز شد ولی هیچ کانفیگی داخلش پیدا نشد
        [کانفیگ, ...]   → کانفیگ‌های پیداشده
    """
    if not sub_url or not sub_url.strip().lower().startswith(("http://", "https://")):
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        session = await _get_shared_session()
        try:
            status, headers, body, final_url = await _get(session, sub_url.strip(), timeout=timeout)
        except aiohttp.ClientConnectorCertificateError:
            # برخی پنل‌های خودمیزبان از گواهینامه‌ی TLS معتبر/خودامضا استفاده می‌کنند که
            # باعث می‌شد این دکمه همیشه با خطای SSL شکست بخورد و کاربر هیچ کانفیگی دریافت نکند؛ یک بار
            # دیگر بدون اعتبارسنجی TLS تلاش می‌کنیم تا فقط کانفیگ‌ها استخراج شوند (خود لینک قبلاً محرمانه برای همین کاربر ارسال شده).
            # perf: قبلاً برای همین یک تلاش، یک Session کاملاً جدید ساخته می‌شد؛
            # ssl=False را می‌شود مستقیم روی همان Session مشترک هم پاس داد.
            async with session.get(sub_url.strip(), allow_redirects=True, ssl=False, timeout=timeout) as resp:
                status = resp.status
                try:
                    body = await resp.text(errors="ignore")
                except Exception:
                    body = ""
                final_url = str(resp.url)

        configs = _parse_configs(body)
        if configs:
            return configs

        if _looks_like_html(body) and _depth == 0:
            embedded = _find_embedded_sub_link(body)
            if embedded and embedded != sub_url:
                return await extract_configs(embedded, _depth=1)

        return []
    except Exception:
        # بعضی پنل‌ها موقع اولین درخواست کند/ناپایدار هستند (timeout یا قطعی
        # موقت اتصال)؛ قبل از اعلام شکست نهایی، یک‌بار دیگر تلاش می‌کنیم.
        if _retry == 0:
            return await extract_configs(sub_url, _depth=_depth, _retry=1)
        return None


def format_bytes(num_bytes) -> str:
    """بایت را به شکل خوانا مثل «۱۲.۴ گیگابایت» تبدیل می‌کند."""
    if num_bytes is None:
        return "نامشخص"
    try:
        num_bytes = int(num_bytes)
    except (TypeError, ValueError):
        return "نامشخص"

    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} گیگابایت"
    mb = num_bytes / (1024 ** 2)
    return f"{mb:.0f} مگابایت"


def format_expire(expire_ts) -> str:
    """تایم‌استمپ انقضا را به تاریخ خوانا تبدیل می‌کند."""
    if not expire_ts:
        return "نامحدود"
    try:
        dt = datetime.fromtimestamp(int(expire_ts), tz=TEHRAN_TZ).replace(tzinfo=None)
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return "نامشخص"


def usage_bar(percent, length: int = 10) -> str:
    """نوار پیشرفت مصرف با ایموجی؛ مثل 🟩🟩🟩🟩🟩🟩⬜⬜⬜⬜ ۶۰٪"""
    try:
        percent = max(0, min(100, float(percent)))
    except (TypeError, ValueError):
        percent = 0
    filled = round(length * percent / 100)
    color = "🟥" if percent >= 90 else ("🟨" if percent >= 80 else "🟩")
    return color * filled + "⬜" * (length - filled)


def days_remaining(expire_ts) -> int | None:
    if not expire_ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(expire_ts), tz=TEHRAN_TZ).replace(tzinfo=None)
        delta = dt - now_tehran_naive()
        total_seconds = delta.total_seconds()
        if total_seconds <= 0:
            return 0
        # به‌جای floor ساده‌ی .days (که برای هر باقیمانده‌ی کمتر از ۲۴ ساعت صفر
        # برمی‌گشت و باعث می‌شد سرویس‌های فعال با چند ساعت باقیمانده به‌اشتباه
        # «منقضی‌شده» نمایش داده شوند)، به بالا گرد می‌کنیم تا حداقل ۱ روز نشان بدهد.
        return -(-int(total_seconds) // 86400)
    except Exception:
        return None


def is_config_expired(cfg: dict) -> bool:
    """بررسی اینکه آیا یک کانفیگ منقضی شده است یا خیر.
    اگر expiry تنظیم نشده باشد (None) False برمی‌گرداند (نامحدود فرض می‌شود).
    """
    expiry = cfg.get("expiry")
    if not expiry:
        return False
    try:
        exp_dt = datetime.strptime(str(expiry)[:10], "%Y-%m-%d")
        # 🐛 تاریخ انقضا «روزانه» است؛ سرویس تا پایان همان روز معتبر است. قبلاً با نیمه‌شب مقایسه
        # می‌شد و مثلاً تستِ چندساعته (انقضا = امروز) همان لحظه از «سرویس‌های من» ربات حذف می‌شد.
        return exp_dt.date() < now_tehran_naive().date()
    except Exception:
        return False


