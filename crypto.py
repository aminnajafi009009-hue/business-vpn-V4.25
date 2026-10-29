"""
crypto.py
رمزنگاری/رمزگشایی کانفیگ‌های VPN قبل از ذخیره در دیتابیس.
اگر روزی فایل database.db لو برود، کانفیگ مشتری‌ها قابل خواندن نخواهد بود.

از Fernet (رمزنگاری متقارن AES) استفاده می‌شود.
کلید باید در .env تحت CONFIG_ENCRYPTION_KEY ذخیره شود.
"""

import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()


def generate_key() -> str:
    """یک کلید جدید می‌سازد. فقط یک‌بار اجرا کن و نتیجه را در .env بگذار."""
    return Fernet.generate_key().decode()


def _get_fernet() -> Fernet:
    key = os.environ.get("CONFIG_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "CONFIG_ENCRYPTION_KEY در .env تنظیم نشده است.\n"
            "برای ساخت کلید جدید دستور زیر را اجرا کن:\n"
            "  python -c \"from crypto import generate_key; print(generate_key())\"\n"
            "و خروجی را داخل .env قرار بده."
        )
    return Fernet(key.encode())


def encrypt_config(plain_text: str) -> str:
    return _get_fernet().encrypt(plain_text.encode()).decode()


def decrypt_config(encrypted_text: str) -> str:
    return _get_fernet().decrypt(encrypted_text.encode()).decode()
