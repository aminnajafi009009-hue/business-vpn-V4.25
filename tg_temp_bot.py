"""Bot موقت برای پروسه‌ی Flask (Mini App).

هندلرها/alerts به send_message پارامتر داخلی `_skip_auto_text` می‌دهند که فقط
patch سراسری bot.py آن را می‌پذیرد. این زیرکلاس همان پارامتر را حذف می‌کند تا
مسیرهای Mini App هم دقیقاً مثل ربات کار کنند (چه patch فعال باشد چه نباشد).
"""
from aiogram import Bot


class TempBot(Bot):
    async def send_message(self, *args, **kwargs):
        kwargs.pop("_skip_auto_text", None)
        return await super().send_message(*args, **kwargs)
