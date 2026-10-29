"""
cache.py
لایه‌ی کش سبک و حافظه‌ای (in-process) برای کاهش رفت‌وبرگشت به دیتابیس.

چرا لازم بود: وقتی از Turso/libsql (دیتابیس ابری) استفاده می‌شود، هر
`cur.execute(...)` یک درخواست شبکه‌ی واقعی به سرور Turso می‌فرستد. خیلی از
داده‌ها (متن‌های اطلاعات ربات، override های ویرایشگر متن/دکمه، لیست پنل‌های
VPN، استیکرهای هر بخش) در عمل خیلی کم تغییر می‌کنن ولی روی *هر* ساخت کیبورد/
پیام (یعنی تقریباً هر بار که ادمین یا مشتری هر دکمه‌ای می‌زنه) چندین‌بار
خونده می‌شن؛ همین باعث تاخیر چندثانیه‌ای در پاسخ ربات می‌شد. این ماژول یک
کش حافظه‌ای ساده با TTL (به‌علاوه‌ی امکان پاک‌سازی صریح هنگام تغییر مقدار)
جلوی این کوئری‌های تکراری را می‌گیرد.

استفاده:
    value, hit = cache.get("bot_info:welcome_text")
    if not hit:
        value = ...  # از دیتابیس بخوان
        cache.set("bot_info:welcome_text", value)

    # بعد از تغییر مقدار در دیتابیس:
    cache.invalidate("bot_info:welcome_text")
    # یا برای پاک‌کردن یک دسته کامل:
    cache.invalidate_prefix("bot_info:")
"""

import threading
import time

_lock = threading.Lock()
_store: dict[str, tuple[float, object]] = {}
_hits = 0
_misses = 0

DEFAULT_TTL = 60  # ثانیه؛ برای دیتاهایی که خیلی کم تغییر می‌کنن کافیه


def get(key: str):
    """(value, hit: bool) را برمی‌گرداند. اگر hit=False باشد، value همیشه None است."""
    global _hits, _misses
    with _lock:
        item = _store.get(key)
        if not item:
            _misses += 1
            return None, False
        expires_at, value = item
        if time.monotonic() > expires_at:
            _store.pop(key, None)
            _misses += 1
            return None, False
        _hits += 1
        return value, True


def set(key: str, value, ttl: float = DEFAULT_TTL):
    with _lock:
        _store[key] = (time.monotonic() + ttl, value)


def invalidate(key: str):
    with _lock:
        _store.pop(key, None)


def invalidate_prefix(prefix: str):
    with _lock:
        for k in [k for k in _store if k.startswith(prefix)]:
            _store.pop(k, None)


def clear_all():
    global _hits, _misses
    with _lock:
        _store.clear()
        _hits = 0
        _misses = 0


def stats() -> dict:
    with _lock:
        total = _hits + _misses
        hit_rate = round((_hits / total) * 100, 1) if total else 0.0
        return {
            "entries": len(_store),
            "hits": _hits,
            "misses": _misses,
            "hit_rate": hit_rate,
        }


def cached(prefix: str, ttl: float = DEFAULT_TTL):
    """دکوریتور: نتیجه‌ی یک تابع sync بدون آرگومان یا با آرگومان‌های ساده را
    زیر یک کلید مبتنی بر prefix+آرگومان‌ها کش می‌کند."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            key = f"{prefix}:{args}:{sorted(kwargs.items())}"
            value, hit = get(key)
            if hit:
                return value
            value = fn(*args, **kwargs)
            set(key, value, ttl)
            return value
        wrapper.__wrapped__ = fn
        return wrapper
    return decorator
