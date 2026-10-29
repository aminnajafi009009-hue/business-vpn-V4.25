"""تنظیمات اعلان‌ها و لاگ‌ها (روشن/خاموش برای هر مورد).

هر مورد یک کلید دارد؛ پیش‌فرض همه روشن است. فقط کلیدهای خاموش در setting «notif_off» نگه داشته می‌شوند.
منبع واحد برای ربات و مینی‌اپ (هر دو از همین ماژول می‌خوانند). این بخش فقط مربوط به ادمین است و
در «ویرایش متن/دکمه» نیست."""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

SETTING_KEY = "notif_off"

# (key, label, audience) — audience: "user" = پیام به کاربر | "admin" = اعلان/لاگ ویژه‌ی ادمین
REGISTRY: list[tuple[str, str, str]] = [
    ("receipt_approved", "✅ تأیید رسید/سفارش", "user"),
    ("receipt_rejected", "❌ رد رسید/سفارش", "user"),
    ("wallet_charged", "💰 شارژ کیف پول", "user"),
    ("gift_received", "🎁 دریافت هدیه", "user"),
    ("usage_volume", "📊 هشدار حجم مصرف", "user"),
    ("usage_time", "⏳ هشدار زمان/انقضا", "user"),
    ("service_ended", "🔚 اتمام سرویس", "user"),
    ("payg_empty", "💤 اتمام موجودی PAYG", "user"),
    ("wallet_low", "🪫 کم‌شدن موجودی کیف پول (زیر حد تعیین‌شده)", "user"),
    ("left_channel", "🚪 لفت دادن از کانال‌ها", "user"),
    ("referral_penalty", "🚨 جریمه‌ی رفرال", "user"),
    ("referral_reward", "🎉 دعوت موفق/پاداش رفرال", "user"),
    ("fup_start", "⚖️ شروع مصرف منصفانه (رسیدن به حد مصرف آزاد)", "user"),
    ("fup_blocked", "⛔ توقف روزانه‌ی سرویس (سقف مصرف منصفانه)", "user"),
    ("admin_panel_log", "🧾 لاگ پاسخ پنل (متن JSON)", "admin"),
    ("admin_order_log", "📋 لاگ سفارش‌ها (کانال)", "admin"),
    ("admin_order_report", "🛒 گزارش خرید/تمدید/تست برای ادمین", "admin"),
    ("admin_delivery_report", "📦 گزارش تحویل خودکار سرویس", "admin"),
    ("admin_subadmin_report", "📝 گزارش عملکرد ادمین‌های فرعی", "admin"),
    ("admin_gateway_report", "🌐 گزارش وضعیت/خطای درگاه‌ها", "admin"),
    ("adv_payments", "📊 گزارشات پیشرفته ← 💳 پرداخت‌ها", "admin"),
    ("adv_delivery", "📊 گزارشات پیشرفته ← 📦 تحویل سرویس", "admin"),
    ("adv_errors", "📊 گزارشات پیشرفته ← 🚨 خطاها و پنل", "admin"),
    ("adv_backup", "📊 گزارشات پیشرفته ← 💾 بک‌آپ هفتگی", "admin"),
    ("adv_autopay", "📊 گزارشات پیشرفته ← 🌐 پرداخت خودکار", "admin"),
    ("adv_collab", "📊 گزارشات پیشرفته ← 🤝 همکاری", "admin"),
    ("adv_tickets", "📊 گزارشات پیشرفته ← 🎫 تیکت‌ها", "admin"),
    ("adv_discounts", "📊 گزارشات پیشرفته ← 🎟 کد تخفیف", "admin"),
    ("adv_referrals", "📊 گزارشات پیشرفته ← 👥 دعوت‌ها", "admin"),
]
_LABELS = {k: (l, a) for k, l, a in REGISTRY}


def _off() -> set[str]:
    try:
        import database as db
        raw = json.loads(db.get_setting(SETTING_KEY, "[]") or "[]")
        return {str(x) for x in raw}
    except Exception:
        return set()


def enabled(key: str) -> bool:
    """روشن بودن یک اعلان/لاگ؛ در هر خطایی روشن (fail-open) تا اعلان مهمی گم نشود."""
    try:
        return key not in _off()
    except Exception:
        return True


def set_enabled(key: str, on: bool) -> bool:
    if key not in _LABELS:
        raise ValueError("اعلان نامعتبر")
    import database as db
    off = _off()
    if on:
        off.discard(key)
    else:
        off.add(key)
    db.set_setting(SETTING_KEY, json.dumps(sorted(off)))
    return on


def toggle(key: str) -> bool:
    new = not enabled(key)
    set_enabled(key, new)
    return new


def all_items() -> list[dict]:
    off = _off()
    return [{"key": k, "label": l, "audience": a, "enabled": k not in off} for k, l, a in REGISTRY]


class Skip(Exception):
    """برای پرش از یک بلوک try/except ارسال اعلان وقتی خاموش است (توسط except Exception گرفته می‌شود)."""


def gate(key: str) -> None:
    if not enabled(key):
        raise Skip(key)
