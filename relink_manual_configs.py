"""یک‌بار اجرا: سرویس‌های قدیمیِ «ارسال دستی» را به کاربر واقعی پنل وصل می‌کند.

اجرا (کنار فایل‌های ربات و با همان .env):
    python relink_manual_configs.py          # فقط گزارش (چیزی تغییر نمی‌کند)
    python relink_manual_configs.py --apply  # اعمال در دیتابیس

بعد از اتصال، «سرویس‌های من / جزئیات سرویس» نام، مصرف، آخرین اتصال، اپ، دستگاه‌ها و دکمه‌ی
تغییر لینک ساب را از خود پنل می‌گیرد. (بدون اجرای این فایل هم ربات هر سرویس را هنگام
باز شدن جزئیات خودش وصل می‌کند؛ این اسکریپت فقط همه را یک‌جا انجام می‌دهد.)
"""
import asyncio
import sys

import crypto
import database as db
import panels


async def main(apply: bool):
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM configs WHERE source = 'manual' AND deleted = 0 "
                "AND (panel_id IS NULL OR panel_id = '') AND (service_id IS NULL OR service_id = '')")
    rows = [dict(r) for r in cur.fetchall()]
    print(f"{len(rows)} سرویس دستی پیدا شد")
    linked = failed = 0
    for cfg in rows:
        try:
            link = crypto.decrypt_config(cfg["config"])
            res = await asyncio.wait_for(panels.resolve_manual_link(link), timeout=30)
        except Exception as exc:
            res, link = None, None
            print(f"  #{cfg['id']}: خطا {exc}")
        if not res:
            failed += 1
            print(f"  #{cfg['id']}: در هیچ پنلی پیدا نشد")
            continue
        linked += 1
        print(f"  #{cfg['id']}: ← {res['username']} ({res['panel'].get('name')})")
        if apply:
            db.link_config_to_panel(cfg["id"], res["panel"]["id"], res["service_id"], res["panel_type"],
                                    service_name=res["username"])
    print(f"\nوصل‌شده: {linked} | پیدا‌نشده: {failed} | {'اعمال شد' if apply else 'فقط گزارش (برای اعمال --apply)'}")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
