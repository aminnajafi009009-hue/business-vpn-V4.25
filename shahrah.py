"""
shahrah.py
کلاینت آسنکرون (aiohttp) برای وب‌سرویس reseller پنل «شاهراه» (shahrah.top) — نسخه‌ی چندنمونه‌ای.
هیچ‌جای دیگر پروژه نباید مستقیماً به این API درخواست بزند؛ همه باید از همین
فایل استفاده کنند تا در صورت تقییر مستندات فقط همین‌جا عوض شود.

🆕 چندپنلی: برخلاف نسخه‌ی قبلی (یک پنل ثابت از .env)، همه‌ی توابع این فایل یک
دیکشنری `panel` (یک ردیف از جدول vpn_panels شامل حداقل id/base_url/api_key) می‌گیرند تا
بتوان همزمان چند نمونه‌ی مستقل از پنل شاهراه (هرکدام با کلید جدا) روی ربات فعال داشت.

توکن (api_key) طبق توصیه‌ی خود مستندات شاهراه همیشه در هدر X-API-KEY فرستاده می‌شود
(نه در query string) و فقط از ردیف همان نمونه‌ی پنل خوانده می‌شود.

همه‌ی توابع این فایل یک تاپل (ok: bool, data: dict|None, message: str) برمی‌گردانند:
- ok=True  → data شامل بدنه‌ی کامل پاسخ (dict) است.
- ok=False → data ممکن است None یا بدنه‌ی خام خطا باشد؛ message پیام قابل‌نمایش به ادمین است.
"""

import logging

import aiohttp

import loopsafe

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

# perf: قبلاً هر درخواست به شاهراه یک aiohttp.ClientSession کاملاً تازه می‌ساخت
# (بدون reuse اتصال/TLS)؛ چون هدرها (از جمله X-API-KEY) همیشه per-request پاس
# داده می‌شوند نه در سطح Session، یک Session مشترک بین همه‌ی پنل‌های شاهراه
# کاملاً امن است و سرعت درخواست‌ها را به‌خصوص روی پنل‌های پشت Cloudflare
# (که TLS handshake کندتری دارند) محسوس بهتر می‌کند.
_shared_session: aiohttp.ClientSession | None = None


async def _get_shared_session() -> aiohttp.ClientSession:
    global _shared_session
    if loopsafe.usable(_shared_session):
        return _shared_session
    connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300, keepalive_timeout=60)
    _shared_session = aiohttp.ClientSession(timeout=_TIMEOUT, connector=connector)
    return _shared_session

# برخی سرویس‌ها (به‌خصوص پشت Cloudflare، مثل شاهراه) درخواست‌هایی که با
# User-Agent پیش‌فرض کتابخانه‌ی aiohttp فرستاده می‌شوند را به‌عنوان ترافیک بات مشکوک
# تشخیص داده و بلاک/چلنج می‌کنند. با یک User-Agent شبیه مرورگر واقعی از این مشکل جلوگیری می‌شود.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _headers(panel: dict, has_body: bool) -> dict:
    headers = {
        "Accept": "application/json",
        "X-API-KEY": panel.get("api_key") or "",
        "User-Agent": _USER_AGENT,
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


async def _request(panel: dict, method: str, path: str, *, json_body: dict | None = None,
                    params: dict | None = None) -> tuple[bool, dict | None, str]:
    base_url = (panel.get("base_url") or "").rstrip("/")
    if not (base_url and panel.get("api_key")):
        return False, None, f"اطلاعات اتصال این پنل شاهراه «{panel.get('name', '')}» (آدرس/کلید) تنظیم نشده."

    url = f"{base_url}{path}"
    try:
        session = await _get_shared_session()
        for _hop in range(5):
            async with session.request(
                method, url, headers=_headers(panel, json_body is not None), json=json_body,
                params=params, allow_redirects=False,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                    # پیگیری دستی ریدایرکت با حفظ دقیق متد/بدنه؛ رفتار پیش‌فرض
                    # aiohttp روی ۳۰۱/۳۰۲/۳۰۳ متد POST را خودکار به GET تبدیل می‌کند.
                    new_url = resp.headers["Location"]
                    if new_url.startswith("/"):
                        new_url = f"{resp.url.scheme}://{resp.url.host}{new_url}"
                    logger.info("پیگیری دستی ریدایرکت شاهراه: %s %s -> %s", method, url, new_url)
                    url = new_url
                    params = None
                    continue
                raw_text = None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                    try:
                        raw_text = (await resp.text())[:300]
                    except Exception:
                        raw_text = None
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
                break
        else:
            return False, None, "تعداد ریدایرکت‌های پنل شاهراه بیش از حد مجاز بود."
    except Exception:
        logger.exception("خطا در ارتباط با پنل شاهراه (%s %s)", method, path)
        return False, None, "خطا در برقراری ارتباط با پنل شاهراه (شبکه/سرور در دسترس نیست)."

    if status == 401:
        raw_msg = (data or {}).get("message") if isinstance(data, dict) else None
        return False, data, (
            f"توکن نامعتبر است یا ارسال نشده (401 UNAUTHORIZED): {raw_msg}"
            if raw_msg else
            "توکن نامعتبر است یا ارسال نشده (401 UNAUTHORIZED)."
        )
    if status == 404:
        raw_msg = (data or {}).get("message") if isinstance(data, dict) else None
        return False, data, (
            f"برند، بسته یا سرویس مورد نظر در پنل شاهراه پیدا نشد (404): {raw_msg}"
            if raw_msg else
            f"برند، بسته یا سرویس مورد نظر در پنل شاهراه پیدا نشد (404). آدرس درخواست: {url}"
        )
    if status == 400:
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        return False, data, f"داده‌ی ارسالی نامعتبر است (400): {msg or 'بدون جزئیات'}"
    if status >= 500:
        raw_msg = (data or {}).get("message") if isinstance(data, dict) else None
        if raw_msg:
            extra = f" — پیام پنل: {raw_msg}"
        elif data:
            extra = f" — بدنه‌ی خام: {data}"
        elif raw_text:
            extra = f" — پاسخ فیر-JSON (Content-Type: {content_type}):\n{raw_text}"
        else:
            extra = " — پنل هیچ بدنه‌ای برنگرداند."
        return False, data, f"خطای داخلی پنل شاهراه هنگام پردازش درخواست ({status}){extra}"

    if isinstance(data, dict) and data.get("ok") is False:
        return False, data, data.get("message", "خطای نامشخص از پنل شاهراه.")

    if not isinstance(data, dict):
        return False, data, "پاسخ نامنتظره از پنل شاهراه دریافت شد."

    return True, data, "موفق"


async def get_me(panel: dict) -> tuple[bool, dict | None, str]:
    """اطلاعات برند reseller."""
    return await _request(panel, "GET", "/me")


async def get_traffic(panel: dict) -> tuple[bool, dict | None, str]:
    """ترافیک‌های خریداری‌شده و میزان مصرف برند."""
    return await _request(panel, "GET", "/traffic")


async def get_plans(panel: dict) -> tuple[bool, dict | None, str]:
    """فهرست بسته‌های فعال برند (شامل planSlug هرکدام)."""
    return await _request(panel, "GET", "/plans")


async def get_services(panel: dict, page: int = 1, limit: int = 20, q: str | None = None,
                        status: str | None = None) -> tuple[bool, dict | None, str]:
    params = {"page": page, "limit": limit}
    if q:
        params["q"] = q
    if status:
        params["status"] = status
    return await _request(panel, "GET", "/services", params=params)


async def get_service(panel: dict, slug: str) -> tuple[bool, dict | None, str]:
    """مشاهده‌ی یک سرویس و کانفیگ‌های آن."""
    return await _request(panel, "GET", f"/services/{slug}")


async def create_service(panel: dict, plan_slug: str, username: str) -> tuple[bool, dict | None, str]:
    """ساخت سرویس جدید با یک بسته‌ی مشخص."""
    return await _request(
        panel, "POST", "/services/create", json_body={"planSlug": plan_slug, "username": username}
    )


async def renew_service(panel: dict, slug: str, plan_slug: str) -> tuple[bool, dict | None, str]:
    """تمدید یک سرویس موجود با یک بسته."""
    return await _request(
        panel, "POST", f"/services/{slug}/renew", json_body={"planSlug": plan_slug}
    )


async def disable_service(panel: dict, slug: str) -> tuple[bool, dict | None, str]:
    return await _request(panel, "POST", f"/services/{slug}/disable")


async def enable_service(panel: dict, slug: str) -> tuple[bool, dict | None, str]:
    return await _request(panel, "POST", f"/services/{slug}/enable")


# ---------------------------------------------------------------------------
# استخراج «بهترین حدس» لینک ساب و شناسه‌ی سرویس از پاسخ‌های create/renew.
# مستندات شاهراه ساختار دقیق بدنه‌ی این دو پاسخ را نشان نداده؛ بنابراین این
# تابع به‌صورت تدافعی چند کلید متداول را می‌گردد و در نهایت هر مقدار رشته‌ایی
# که با http شروع شود را هم به‌عنوان آخرین گزینه در نظر می‌گیرد.
# ---------------------------------------------------------------------------
_LINK_KEYS = ("subscriptionUrl", "subUrl", "subLink", "sub_link", "configUrl", "link", "url")
_SLUG_KEYS = ("slug", "serviceSlug", "service_slug")


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def extract_link_and_slug(payload: dict) -> tuple[str | None, str | None]:
    link, slug = None, None
    for node in _walk(payload):
        if link is None:
            for k in _LINK_KEYS:
                v = node.get(k)
                if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")):
                    link = v.strip()
                    break
        if slug is None:
            for k in _SLUG_KEYS:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    slug = v.strip()
                    break
        if link and slug:
            break

    if link is None:
        for node in _walk(payload):
            for v in node.values():
                if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")):
                    link = v.strip()
                    break
            if link:
                break

    return link, slug
