"""loopsafe.py — تشخیص این‌که aiohttp.ClientSession کش‌شده هنوز روی همین event loop قابل استفاده است.

مسیرهای Flask/مینی‌اپ با asyncio.run(...) هر بار یک loop تازه می‌سازند؛ اگر Session
روی loop قبلی (بسته‌شده) ساخته شده باشد خطای «Timeout context manager should be
used inside a task» می‌دهد. پس Session فقط وقتی دوباره استفاده می‌شود که برای همین loop باشد."""
import asyncio


def usable(sess) -> bool:
    if sess is None or sess.closed:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    sloop = getattr(sess, "_loop", None)
    return sloop is loop and not loop.is_closed()


# --- event loop پس‌زمینه‌ی مشترک برای مسیرهای همگام (Flask/مینی‌اپ) --------------------
# قبلاً هر درخواست با asyncio.run(...) یک loop تازه می‌ساخت و می‌بست؛ Session/اتصال‌های کش‌شده
# روی loop بسته‌شده می‌ماندند (خطای «Timeout context manager should be used inside a task»)
# و در نتیجه مصرف/وضعیت سرویس‌ها در مینی‌اپ خالی می‌آمد. حالا همه‌ی این فراخوانی‌ها روی یک loop
# دائمی اجرا می‌شوند تا Session و اتصال‌ها معتبر بمانند و سریع‌تر هم شود.
import threading

_bg_loop = None
_bg_lock = threading.Lock()


def _ensure_bg_loop():
    global _bg_loop
    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop
    with _bg_lock:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        loop = asyncio.new_event_loop()
        started = threading.Event()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        threading.Thread(target=_runner, name="webapp-async-loop", daemon=True).start()
        started.wait(5)
        _bg_loop = loop
        return loop


def run(coro, timeout: float | None = None):
    """معادل asyncio.run(coro) برای کدهای همگام، ولی روی یک loop دائمی."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:  # داخل یک loop در حال اجرا هستیم؛ نباید بلاک کنیم
        raise RuntimeError("loopsafe.run از داخل event loop صدا زده شد")
    loop = _ensure_bg_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
