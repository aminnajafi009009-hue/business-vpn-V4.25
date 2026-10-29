"""
generic_panel.py
Adapter پایه برای پنل‌های جدید. هر پنلی که API کاملش هنوز پیاده‌سازی نشده
aز این فایل به عنوان stub استفاده می‌کند.
"""
from typing import Any

async def test_connection(panel: dict) -> tuple[bool, Any, str]:
    name = panel.get('panel_type', 'unknown')
    return False, None, f"⚙️ پنل {name} در لیست پشتیبانی قرار داد ولی API client آن هنوز نصب نشده. با پشتیبانی تماس بگیرید."

async def get_system_stats(panel: dict) -> tuple[bool, Any, str]:
    return await test_connection(panel)

async def get_templates(panel: dict, force_refresh=False) -> tuple[bool, Any, str]:
    return False, [], "این نوع پنل هنوز پشتیبانی کامل ندارد."

async def get_inbounds(panel: dict, force_refresh=False) -> tuple[bool, Any, str]:
    return False, [], "این نوع پنل هنوز پشتیبانی کامل ندارد."

async def create_user(panel: dict, *args, **kwargs) -> tuple[bool, Any, str]:
    return False, None, "این نوع پنل هنوز پشتیبانی کامل ندارد."

async def create_user_custom(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def create_user_from_template(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def renew_user(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def renew_user_custom(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def disable_user(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def enable_user(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def delete_user(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def get_user(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def reduce_user_quota(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def revoke_sub(panel, *a, **kw): return False, None, "پشتیبانی نشده."
async def get_users_count(panel: dict) -> int: return 0

def extract_link_and_username(panel, data):
    if not isinstance(data, dict): return None, None
    link = data.get("subscription_url") or data.get("sub_url")
    username = data.get("username") or data.get("slug")
    return link, username
