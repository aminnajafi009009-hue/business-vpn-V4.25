"""
db_async.py — Async-safe, cached wrapper around database.py

🎯 هدف: هر فانکشن sync Turso را با:
  1. run_in_executor — blocking call رو از event loop خارج کن (کارایی thread pool)
  2. کش با TTL مناسب — نتیجه رو برای N ثانیه در RAM نگه داره
  3. Invalidation خودکار — وقتی write اتفاق می‌افتد، کش پاک میشه

خلاصه تاثیر:
  • get_user():           Turso round-trip → <1ms  (از RAM)
  • get_vpn_panel():      Turso round-trip → <1ms  (از RAM)
  • list_vip_categories(): Turso round-trip → <1ms  (از RAM)
  • get_setting():        Turso round-trip → <1ms  (از RAM)
  • is_sub_admin():       Turso round-trip → <1ms  (از RAM)
جمعاً: هر پیام که قبلا 3-6 round-trip می‌زد حالا صفر یا یک round-trip می‌زنه
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import cache
import database as _db

logger = logging.getLogger(__name__)

# Thread pool اختصاصی برای blocking Turso calls
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="turso")


async def _run(fn, *args, **kwargs):
    """یک تابع sync را در thread pool اجرا کن بدون block کردن event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, partial(fn, *args, **kwargs))


# ╔════════════════════════════════════════════════════════════════
# USERS
# ╚════════════════════════════════════════════════════════════════

def _user_cache_key(telegram_id) -> str:
    return f"user:tid:{telegram_id}"

def _user_id_cache_key(user_id) -> str:
    return f"user:id:{user_id}"


async def get_user(telegram_id) -> dict | None:
    """کش‌شده - TTL 30s. بعد از خرید یا تغییر invalidate بشه."""
    key = _user_cache_key(telegram_id)
    val, hit = cache.get(key)
    if hit:
        return val
    user = await _run(_db.get_user, telegram_id)
    cache.set(key, user, ttl=5)
    if user:
        cache.set(_user_id_cache_key(user["id"]), user, ttl=5)
    return user


async def get_user_by_id(user_id: int) -> dict | None:
    key = _user_id_cache_key(user_id)
    val, hit = cache.get(key)
    if hit:
        return val
    user = await _run(_db.get_user_by_id, user_id)
    if user:
        cache.set(key, user, ttl=5)
        cache.set(_user_cache_key(user["telegram_id"]), user, ttl=5)
    return user


def invalidate_user(telegram_id=None, user_id=None):
    """بعد از هر write روی user صدا بزن."""
    if telegram_id:
        cache.invalidate(_user_cache_key(telegram_id))
    if user_id:
        cache.invalidate(_user_id_cache_key(user_id))


async def ensure_user(telegram_id, name: str, invite_code: str | None = None) -> dict:
    """کاربر رو اگه نیست بسازد، بعد cache رو invalidate کنه."""
    user = await get_user(telegram_id)
    if user:
        # نام رو async update کند بدون block
        if user.get("name") != name:
            asyncio.create_task(_run(_db.update_user_name, telegram_id, name))
            invalidate_user(telegram_id=telegram_id, user_id=user.get("id"))
        return user
    # کاربر جدید - باید بسازیم
    await _run(_db.create_user, telegram_id, name, invite_code)
    user = await _run(_db.get_user, telegram_id)
    invalidate_user(telegram_id=telegram_id)
    return user


async def add_wallet(user_id: int, amount: int, description: str = ""):
    await _run(_db.add_to_wallet, user_id, amount, description)
    user = await get_user_by_id(user_id)
    if user:
        invalidate_user(telegram_id=user["telegram_id"], user_id=user_id)


# ╔════════════════════════════════════════════════════════════════
# SETTINGS
# ╚════════════════════════════════════════════════════════════════

async def get_setting(key: str, default: str | None = None) -> str | None:
    """کش‌شده - TTL 5دقیقه. settings خیلی کم تغییر می‌کنن."""
    cache_key = f"setting:{key}"
    val, hit = cache.get(cache_key)
    if hit:
        return val if val is not None else default
    result = await _run(_db.get_setting, key, default)
    cache.set(cache_key, result, ttl=300)
    return result


def invalidate_setting(key: str):
    cache.invalidate(f"setting:{key}")


async def set_setting(key: str, value: str):
    await _run(_db.set_setting, key, value)
    invalidate_setting(key)


# ╔════════════════════════════════════════════════════════════════
# PANELS
# ╚════════════════════════════════════════════════════════════════

async def get_vpn_panel(panel_id: int) -> dict | None:
    """کش‌شده - TTL 2دقیقه."""
    key = f"panel:{panel_id}"
    val, hit = cache.get(key)
    if hit:
        return val
    panel = await _run(_db.get_vpn_panel, panel_id)
    cache.set(key, panel, ttl=120)
    return panel


async def list_vpn_panels(panel_type: str | None = None, enabled_only: bool = False) -> list:
    """کش‌شده - TTL 2دقیقه."""
    key = f"panels:list:{panel_type}:{enabled_only}"
    val, hit = cache.get(key)
    if hit:
        return val
    panels = await _run(_db.list_vpn_panels, panel_type, enabled_only)
    cache.set(key, panels, ttl=120)
    # هر پنل رو جداگانه هم کش کن (for get_vpn_panel)
    for p in panels:
        cache.set(f"panel:{p['id']}", p, ttl=120)
    return panels


def invalidate_panels():
    cache.invalidate_prefix("panel:")
    cache.invalidate_prefix("panels:")


async def get_panel_map_for_plan_key(plan_key: str) -> dict | None:
    key = f"panel_map:{plan_key}"
    val, hit = cache.get(key)
    if hit:
        return val
    result = await _run(_db.get_panel_map_for_plan_key, plan_key)
    cache.set(key, result, ttl=120)
    return result


def invalidate_panel_maps():
    cache.invalidate_prefix("panel_map:")


# ╔════════════════════════════════════════════════════════════════
# VIP PLANS / CATEGORIES
# ╚════════════════════════════════════════════════════════════════

async def list_vip_categories() -> list:
    """کش‌شده - TTL 5دقیقه. کمتر تغییر می‌کنه."""
    key = "vip:categories"
    val, hit = cache.get(key)
    if hit:
        return val
    cats = await _run(_db.list_vip_categories)
    cache.set(key, cats, ttl=300)
    return cats


async def get_vip_plans_for_category(category_id: int) -> list:
    key = f"vip:plans:{category_id}"
    val, hit = cache.get(key)
    if hit:
        return val
    plans = await _run(_db.get_vip_plans_for_category, category_id)
    cache.set(key, plans, ttl=300)
    return plans


async def list_all_vip_plans() -> list:
    key = "vip:plans:all"
    val, hit = cache.get(key)
    if hit:
        return val
    plans = await _run(_db.get_all_vip_plans_flat)
    cache.set(key, plans, ttl=300)
    return plans


def invalidate_vip():
    cache.invalidate_prefix("vip:")


async def get_effective_plan(plan_key: str) -> dict | None:
    key = f"plan:{plan_key}"
    val, hit = cache.get(key)
    if hit:
        return val
    plan = await _run(_db.get_effective_plan, plan_key)
    cache.set(key, plan, ttl=300)
    return plan


def invalidate_plan(plan_key: str | None = None):
    if plan_key:
        cache.invalidate(f"plan:{plan_key}")
    else:
        cache.invalidate_prefix("plan:")


# ╔════════════════════════════════════════════════════════════════
# SUB-ADMIN / ADMIN CHECK
# ╚════════════════════════════════════════════════════════════════

async def is_sub_admin(telegram_id: str) -> bool:
    """کش‌شده - TTL 60s. برای کاهش DB hit در هر send_message."""
    key = f"subadmin:{telegram_id}"
    val, hit = cache.get(key)
    if hit:
        return val
    result = await _run(_db.is_sub_admin, telegram_id)
    cache.set(key, result, ttl=60)
    return result


def invalidate_sub_admins():
    cache.invalidate_prefix("subadmin:")


# ╔════════════════════════════════════════════════════════════════
# UI TEXTS / STICKERS
# ╚════════════════════════════════════════════════════════════════

async def get_ui_text(key: str, default: str = "") -> str:
    """کش‌شده - TTL 10دقیقه."""
    cache_key = f"ui_text:{key}"
    val, hit = cache.get(cache_key)
    if hit:
        return val if val is not None else default
    result = await _run(_db.get_text_override, key)
    out = result if result is not None else default
    cache.set(cache_key, out, ttl=600)
    return out


async def get_section_sticker(section_key: str) -> dict | None:
    """کش‌شده - TTL 10دقیقه."""
    key = f"sticker:{section_key}"
    val, hit = cache.get(key)
    if hit:
        return val
    result = await _run(_db.get_section_sticker, section_key)
    cache.set(key, result, ttl=600)
    return result


async def list_guides() -> list:
    """کش‌شده - TTL 10دقیقه."""
    key = "guides:all"
    val, hit = cache.get(key)
    if hit:
        return val
    result = await _run(_db.get_guides)
    cache.set(key, result, ttl=600)
    return result


def invalidate_ui():
    cache.invalidate_prefix("ui_text:")
    cache.invalidate_prefix("sticker:")
    cache.invalidate_prefix("guides:")


# ╔════════════════════════════════════════════════════════════════
# CACHE WARMING — در startup صدا بزن
# ╚════════════════════════════════════════════════════════════════

async def warm_cache():
    """
    در startup یک‌بار داده‌های پرتکرار رو بارگذاری کن؛ اولین پیام زیر 1ms پاسخ می‌گیرد.
    """
    try:
        tasks = [
            list_vip_categories(),
            list_vpn_panels(),
            list_guides(),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("✅ Cache warm-up کامل شد")
    except Exception:
        logger.exception("Cache warm-up ناموفق — ربات ادامه میدهد")
