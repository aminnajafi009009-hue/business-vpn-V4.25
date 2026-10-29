"""
threexui_panel.py
کلاینت آسنکرون (aiohttp) برای پنل‌های 3x-ui (MHSanaei/3x-ui) — نسخه‌ی چندنمونه‌ای.

⚠️ نکته‌ی مهم درباره‌ی نسخه‌های 3x-ui (طبق مستندات و ایشوهای رسمی گیت‌هاب پروژه،
github.com/MHSanaei/3x-ui):
سه‌ایکس-یو‌آی دو نسل API دارد که با هم سازگار نیستند:
  • نسخه‌های قدیمی‌تر (پنل Vue، هنوز پرکاربردترین نسخه‌ی نصب‌شده روی سرورها):
    endpoint های `/panel/api/inbounds/*` (addClient/updateClient/delClient).
  • نسخه‌های خیلی جدید v3.x (پنل React): این endpoint ها را حذف کرده‌اند و
    به‌جایش `/panel/api/clients/*` را دارند.
این فایل نسل قدیمی (legacy) را پیاده کرده — چون مستندترین و پرکاربردترین حالت
است و اکثر نصب‌های فعلی را پوشش می‌دهد. اگر نمونه‌ای از پنل شما دقیقاً از
جدیدترین نسخه‌ی React است و این حالت جواب نداد، پیام خطای واضحی نشان داده
می‌شود (نه سکوت)، چون /panel/api/inbounds/addClient روی آن نسخه‌ها اصلاً وجود
ندارد.

برخلاف مرزبان/پاسارگارد، 3x-ui اصلاً مفهوم «تمپلیت» یا «گروه» ندارد: هر
inbound (پروتکل + پورت مشخص روی سرور) مستقیماً همان چیزی است که پلن به آن
نگاشت می‌شود — یعنی حالت «بدون تمپلیت» همیشگی و تنها حالت است.

هر «کاربر» در 3x-ui یک آبجکت جدا نیست؛ داخل فیلد settings (یک رشته‌ی
JSON-encode شده) یک inbound مشخص زندگی می‌کند. برای همین service_id این پنل
به‌شکل ترکیبی "<inbound_id>:<client_uuid>" ذخیره می‌شود تا برای تمدید/حذف/
فعال‌وغیرفعال‌کردن مجبور به جست‌وجوی email در همه‌ی inbound ها نباشیم.
"""

import asyncio
import json
import logging
import time
import uuid as uuid_lib

import aiohttp

import loopsafe

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

_login_cache: dict[int, dict] = {}      # panel_id -> {"ok": bool, "expires_at": float, "csrf": str | None}
_inbounds_cache: dict[int, dict] = {}   # panel_id -> {"items": ..., "expires_at": ...}
_sessions: dict[tuple, aiohttp.ClientSession] = {}
_session_lock = asyncio.Lock()

_MAX_PANEL_RETRIES = 2
_PANEL_RETRY_DELAY = 1.5


def _pid(panel: dict) -> int:
    return int(panel["id"])


async def _get_session(panel: dict) -> aiohttp.ClientSession:
    """یک aiohttp.ClientSession جدا به‌ازای هر نمونه پنل، با cookie jar داخلی
    خودش — برای 3x-ui همین یعنی کوکی سشن لاگین به‌صورت خودکار بین
    درخواست‌های بعدی همان نمونه حفظ می‌شود، بدون نیاز به مدیریت دستی کوکی."""
    pid = (_pid(panel), id(asyncio.get_running_loop()))  # یک Session برای هر (پنل، event loop)
    sess = _sessions.get(pid)
    if loopsafe.usable(sess):
        return sess
    if True:
        sess = _sessions.get(pid)
        if not loopsafe.usable(sess):
            connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, keepalive_timeout=75)
            sess = aiohttp.ClientSession(timeout=_TIMEOUT, connector=connector, cookie_jar=aiohttp.CookieJar())
            _sessions[pid] = sess
        return sess


async def _ensure_login(panel: dict):
    """طبق auth_method انتخاب‌شده در پنل ادمین:
      • api_key → هدر Authorization: Bearer <api_key> روی هر درخواست (فقط
        روی 3x-ui نسخه‌ی v3.0.2 به بعد کار می‌کند؛ از Settings → Security →
        API Token در خود پنل ساخته می‌شود).
      • userpass (پیش‌فرض) → لاگین کوکی‌محور کلاسیک با POST /login که روی
        همه‌ی نسخه‌ها کار می‌کند."""
    if panel.get("auth_method") == "api_key":
        token = (panel.get("api_key") or "").strip()
        if not token:
            return False, "اتصال این پنل 3x-ui روی «API» تنظیم شده ولی هیچ API Token ای ثبت نشده."
        return True, None

    base_url = (panel.get("base_url") or "").rstrip("/")
    username = panel.get("username")
    password = panel.get("password")
    if not (base_url and username and password):
        return False, "اطلاعات اتصال این پنل 3x-ui (آدرس/یوزرنیم/پسورد) کامل تنظیم نشده."

    pid = _pid(panel)
    cache = _login_cache.setdefault(pid, {"ok": False, "expires_at": 0.0, "csrf": None})
    now = time.monotonic()
    if cache["ok"] and now < cache["expires_at"]:
        return True, None

    for attempt in range(_MAX_PANEL_RETRIES + 1):
        try:
            session = await _get_session(panel)
            async with session.post(f"{base_url}/login", data={"username": username, "password": password}) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                ok = resp.status == 200 and (data is None or data.get("success", True))
                if not ok:
                    detail = (data or {}).get("msg") if isinstance(data, dict) else None
                    return False, f"ورود به پنل 3x-ui «{panel.get('name', '')}» ناموفق بود ({resp.status}): {detail or 'یوزرنیم/پسورد را بررسی کن'}"
                cache["ok"] = True
                cache["expires_at"] = now + 20 * 60  # مهلت سشن پیش‌فرض پنل معمولاً ۶۰ دقیقه است؛ محافظه‌کارانه ۲۰ دقیقه کش می‌کنیم
                # 🆕 پنل‌های جدیدتر (React) برای درخواست‌های تغییردهنده (POST) به یک
                # X-CSRF-Token هم نیاز دارند؛ پنل‌های قدیمی‌تر (Vue) این endpoint را
                # اصلاً ندارند، پس اگر 404/خطا داد بی‌سروصدا نادیده می‌گیریم (یعنی این
                # پنل به CSRF نیاز ندارد) به‌جای این‌که کل لاگین را خراب کنیم.
                try:
                    async with session.get(f"{base_url}/panel/api/csrf-token") as csrf_resp:
                        if csrf_resp.status == 200:
                            csrf_data = await csrf_resp.json(content_type=None)
                            cache["csrf"] = (csrf_data or {}).get("obj") if isinstance(csrf_data, dict) else None
                except Exception:
                    cache["csrf"] = None
                return True, None
        except Exception:
            if attempt < _MAX_PANEL_RETRIES:
                await asyncio.sleep(_PANEL_RETRY_DELAY)
                continue
            logger.exception("خطا در اتصال به پنل 3x-ui هنگام لاگین")
            return False, "خطا در برقراری ارتباط با پنل 3x-ui (شبکه/سرور در دسترس نیست)."


async def _request(panel: dict, method: str, path: str, json_body: dict | None = None, _retry_on_fail: bool = True):
    ok, err = await _ensure_login(panel)
    if not ok:
        return False, None, err

    base_url = (panel.get("base_url") or "").rstrip("/")
    headers = {}
    if panel.get("auth_method") == "api_key":
        headers["Authorization"] = f"Bearer {(panel.get('api_key') or '').strip()}"
    else:
        csrf = _login_cache.get(_pid(panel), {}).get("csrf")
        if csrf and method.upper() != "GET":
            headers["X-CSRF-Token"] = csrf

    data, status = None, None
    for attempt in range(_MAX_PANEL_RETRIES + 1):
        try:
            session = await _get_session(panel)
            async with session.request(method, f"{base_url}{path}", json=json_body, headers=headers) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw) if raw else None
                except Exception:
                    data = None
                status = resp.status
            break
        except Exception:
            if attempt < _MAX_PANEL_RETRIES:
                await asyncio.sleep(_PANEL_RETRY_DELAY)
                continue
            logger.exception("خطا در ارتباط با پنل 3x-ui (%s %s)", method, path)
            return False, None, "خطا در برقراری ارتباط با پنل 3x-ui (شبکه/سرور در دسترس نیست)."

    # 🐛 طبق ایشوهای رسمی 3x-ui (github.com/MHSanaei/3x-ui#3052 و #3236)، این پنل
    # گاهی به‌جای JSON، یک پاسخ کاملاً خالی برمی‌گرداند (نه خطا، نه موفقیت) —
    # معمولاً یعنی سشن منقضی شده. یک‌بار به‌صورت خودکار دوباره لاگین می‌کنیم و
    # درخواست را تکرار می‌کنیم؛ اگر بازم خالی بود، پیام روشن می‌دهیم (نه سکوت).
    if status in (401, 403) or (data is None and _retry_on_fail):
        _login_cache.setdefault(_pid(panel), {})["ok"] = False
        if _retry_on_fail:
            return await _request(panel, method, path, json_body=json_body, _retry_on_fail=False)
        return False, data, "پاسخ خالی/نامعتبر از پنل 3x-ui گرفتیم (معمولاً یعنی سشن منقضی شده)؛ دوباره امتحان کن."

    if status is None or status >= 400:
        detail = (data or {}).get("msg") if isinstance(data, dict) else None
        return False, data, f"خطای پنل 3x-ui ({status}): {detail or 'بدون جزئیات'}"

    if isinstance(data, dict) and data.get("success") is False:
        return False, data, f"پنل 3x-ui خطا داد: {data.get('msg') or 'بدون جزئیات'}"

    return True, (data or {}).get("obj") if isinstance(data, dict) else data, "موفق"


async def test_connection(panel: dict):
    ok, data, msg = await _request(panel, "GET", "/panel/api/inbounds/list")
    if not ok:
        return False, None, msg
    count = len(data) if isinstance(data, list) else 0
    return True, {"inbounds_count": count}, f"اتصال موفق ({count} اینباند پیدا شد)"


async def get_inbounds(panel: dict, force_refresh: bool = False):
    """لیست اینباندهای این نمونه پنل (هر پلن مستقیماً به یکی از همین‌ها نگاشت می‌شود)."""
    pid = _pid(panel)
    cache = _inbounds_cache.setdefault(pid, {"items": None, "expires_at": 0.0})
    now = time.monotonic()
    if (not force_refresh) and cache.get("items") is not None and now < cache.get("expires_at", 0):
        return True, cache["items"], "موفق (cache)"
    ok, data, msg = await _request(panel, "GET", "/panel/api/inbounds/list")
    if ok:
        cache["items"] = data if isinstance(data, list) else []
        cache["expires_at"] = now + 900
        return True, cache["items"], msg
    return False, None, msg


async def _get_inbound_raw(panel: dict, inbound_id: int):
    """اینباند را تازه (بدون کش) با تمام کلاینت‌های فعلی‌اش می‌گیرد — لازم برای
    تمدید/حذف/فعال‌وغیرفعال‌کردن، چون باید کل رشته‌ی settings را دوباره
    (با کلاینت اصلاح‌شده) پس بفرستیم؛ طبق ایشوی رسمی #3083، ارسال بدنه‌ی
    ناقص می‌تواند بی‌سروصدا نادیده گرفته شود."""
    ok, data, msg = await _request(panel, "GET", f"/panel/api/inbounds/get/{inbound_id}")
    if not ok or not isinstance(data, dict):
        return None, msg
    return data, None


def _parse_clients(inbound_raw: dict) -> list[dict]:
    settings = inbound_raw.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    return list((settings or {}).get("clients") or [])


def _expire_ms_from_days(days) -> int:
    if not days:
        return 0
    return int(time.time() * 1000) + int(days) * 86400 * 1000


def _bytes_from_gb(volume_gb) -> int:
    if not volume_gb:
        return 0
    return int(float(volume_gb) * 1024 * 1024 * 1024)


def extract_link_and_username(panel: dict, data: dict) -> tuple[str | None, str | None]:
    email = (data or {}).get("email")
    link = (data or {}).get("sub_link")
    return link, email


async def _fetch_sub_link(panel: dict, sub_id: str) -> str | None:
    """به‌جای حدس‌زدن آدرس ساب (چون سرور ساب معمولاً روی یک پورت جدا از خود
    پنل اجرا می‌شود، مثلاً پیش‌فرض 10882 — نه پورت پنل)، از endpoint رسمی
    خودِ پنل (GET /panel/api/inbounds/getSubLinks/:subId) که روی همان
    base_url پنل جواب می‌دهد لینک واقعی را می‌گیریم."""
    ok, data, msg = await _request(panel, "GET", f"/panel/api/inbounds/getSubLinks/{sub_id}")
    if not ok:
        return None
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data.get("subUrl") or data.get("url")
    if isinstance(data, str) and data:
        return data
    return None


async def create_user(panel: dict, inbound_id: int, username: str, volume_gb, days):
    """یک کلاینت جدید به inbound مشخص‌شده اضافه می‌کند. username همان email
    (شناسه‌ی نمایشی، نه ایمیل واقعی) خواهد بود؛ uuid و subId این‌جا در خود
    ربات تولید می‌شوند (نه در پنل) چون طبق ایشوی رسمی #3237، subId هنگام
    ساخت از طریق API همیشه به‌صورت خودکار تولید نمی‌شود."""
    client_uuid = str(uuid_lib.uuid4())
    sub_id = uuid_lib.uuid4().hex[:16]
    client = {
        "id": client_uuid,
        "email": username,
        "limitIp": 0,
        "totalGB": _bytes_from_gb(volume_gb),
        "expiryTime": _expire_ms_from_days(days),
        "enable": True,
        "flow": "",
        "subId": sub_id,
        "tgId": "",
        "comment": "",
        "reset": 0,
    }
    body = {"id": int(inbound_id), "settings": json.dumps({"clients": [client]})}
    ok, data, msg = await _request(panel, "POST", "/panel/api/inbounds/addClient", json_body=body)
    if not ok:
        return False, None, msg
    sub_link = await _fetch_sub_link(panel, sub_id)
    result = {
        "subId": sub_id, "email": username, "uuid": client_uuid,
        "inbound_id": int(inbound_id), "sub_link": sub_link,
    }
    return True, result, msg


async def _mutate_client(panel: dict, service_id: str, mutate_fn):
    """الگوی مشترک تمدید/فعال‌وغیرفعال‌کردن/حذف: اینباند را کامل می‌خواند،
    کلاینت هدف را با mutate_fn تغییر می‌دهد (یا حذف می‌کند اگر None برگرداند)،
    و کل settings را دوباره POST می‌کند."""
    try:
        inbound_id_str, client_uuid = service_id.split(":", 1)
        inbound_id = int(inbound_id_str)
    except Exception:
        return False, None, "شناسه‌ی سرویس این پنل نامعتبر است."

    inbound_raw, err = await _get_inbound_raw(panel, inbound_id)
    if inbound_raw is None:
        return False, None, err or "این اینباند روی پنل پیدا نشد."

    clients = _parse_clients(inbound_raw)
    idx = next((i for i, c in enumerate(clients) if c.get("id") == client_uuid), None)
    if idx is None:
        return False, None, "این کلاینت روی پنل پیدا نشد (شاید قبلاً حذف شده)."

    new_client = mutate_fn(dict(clients[idx]))
    body = {"id": inbound_id, "settings": json.dumps({"clients": [new_client]})}
    ok, data, msg = await _request(panel, "POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json_body=body)
    if not ok:
        return False, None, msg
    sub_link = await _fetch_sub_link(panel, new_client.get("subId")) if new_client.get("subId") else None
    return True, {
        "email": new_client.get("email"), "subId": new_client.get("subId"),
        "inbound_id": inbound_id, "sub_link": sub_link,
    }, msg


async def renew_user_custom(panel: dict, service_id: str, volume_gb, days, volume_unlimited=False, days_unlimited=False):
    """تمدید افزایشی: حجم/روز باقی‌مانده به مقدار پلن اضافه می‌شود (نه جایگزین)،
    دقیقاً هم‌رفتار با renew_user_custom مرزبان/پاسارگارد."""
    def mutate(c):
        now_ms = int(time.time() * 1000)
        base_expire = c.get("expiryTime") or 0
        base_expire = base_expire if base_expire > now_ms else now_ms
        c["expiryTime"] = base_expire + int(days or 0) * 86400 * 1000
        c["totalGB"] = int(c.get("totalGB") or 0) + _bytes_from_gb(volume_gb)
        if volume_unlimited:
            c["totalGB"] = 0
        if days_unlimited:
            c["expiryTime"] = 0
        c["enable"] = True
        return c
    return await _mutate_client(panel, service_id, mutate)


async def disable_user(panel: dict, service_id: str):
    def mutate(c):
        c["enable"] = False
        return c
    return await _mutate_client(panel, service_id, mutate)


async def enable_user(panel: dict, service_id: str):
    def mutate(c):
        c["enable"] = True
        return c
    return await _mutate_client(panel, service_id, mutate)


async def reduce_user_quota(panel: dict, service_id: str, gb: float):
    """🆕 جریمه‌ی رفرال: gb گیگابایت مستقیماً از totalGB فعلی کلاینت کم
    می‌کند (نه اضافه). اگر totalGB صفر (نامحدود) باشد، کاری نمی‌کند."""
    inbound_id_str, client_uuid = service_id.split(":", 1)
    inbound_raw, err = await _get_inbound_raw(panel, int(inbound_id_str))
    if inbound_raw is None:
        return False, None, err or "این اینباند روی پنل پیدا نشد."
    clients = _parse_clients(inbound_raw)
    found = next((c for c in clients if c.get("id") == client_uuid), None)
    if not found or int(found.get("totalGB") or 0) <= 0:
        return False, None, "این سرویس محدودیت حجمی ندارد (نامحدود است) یا پیدا نشد؛ جریمه‌ی حجمی اعمال نمی‌شود."

    def mutate(c):
        reduction_bytes = int(float(gb) * 1024 * 1024 * 1024)
        c["totalGB"] = max(0, int(c.get("totalGB") or 0) - reduction_bytes)
        return c
    return await _mutate_client(panel, service_id, mutate)


async def reduce_user_days(panel: dict, service_id: str, days: float):
    inbound_id_str, client_uuid = service_id.split(":", 1)
    inbound_raw, err = await _get_inbound_raw(panel, int(inbound_id_str))
    if inbound_raw is None:
        return False, None, err or "این اینباند روی پنل پیدا نشد."
    found = next((c for c in _parse_clients(inbound_raw) if c.get("id") == client_uuid), None)
    exp = int((found or {}).get("expiryTime") or 0)
    if not found or exp <= 0:
        return False, None, "این سرویس محدودیت زمانی ندارد (نامحدود است) یا پیدا نشد."
    new_exp = exp - int(float(days) * 86400 * 1000)
    if new_exp <= int(time.time() * 1000) + 3600 * 1000:
        return False, None, "زمان باقی‌مانده‌ی سرویس کمتر از مقداری است که می‌خواهید کم کنید."

    def mutate(c):
        c["expiryTime"] = new_exp
        return c
    return await _mutate_client(panel, service_id, mutate)


async def delete_user(panel: dict, service_id: str):
    try:
        inbound_id_str, client_uuid = service_id.split(":", 1)
        inbound_id = int(inbound_id_str)
    except Exception:
        return False, None, "شناسه‌ی سرویس این پنل نامعتبر است."
    ok, data, msg = await _request(panel, "POST", f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}")
    return ok, data, msg


async def get_client_traffic(panel: dict, email: str):
    """مصرف کلاینت با email (نه uuid) — طبق مستندات رسمی همین endpoint را دارد."""
    ok, data, msg = await _request(panel, "GET", f"/panel/api/inbounds/getClientTraffics/{email}")
    if not ok:
        return False, None, msg
    return True, data, msg
