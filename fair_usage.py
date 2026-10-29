"""⚖️ مصرف منصفانه (Fair Usage Policy) برای سرویس‌های «نامحدود» متصل به پنل (به‌جز PAYG).

منطق:
  • تا «حد مصرف آزاد» (free) کاربر هیچ محدودیتی ندارد. با رسیدن به آن، یک اعلان (قابل‌ویرایش) می‌رود و FUP شروع می‌شود.
  • از آن لحظه، هر روز فقط «سهمیه‌ی روزانه» (daily) مجاز است. با رسیدن به سقف، سرویس روی پنل غیرفعال می‌شود.
  • ساعت ۰۰:۰۰ (تهران) سهمیه صفر می‌شود و سرویس دوباره فعال می‌شود.
  • تا قبل از نیمه‌شب، کاربر نمی‌تواند با دکمه‌ی «فعال‌سازی» سرویس را دور بزند (enable_denied).
وضعیت هر سرویس در ستون‌های fup_started / fup_blocked / fup_base_bytes / fup_day ذخیره می‌شود."""
from __future__ import annotations

import json
import logging
import re

import database as db
import notif_settings
import panels

logger = logging.getLogger(__name__)

KEY = "fup_cfg"
GB = 1024 ** 3
MB = 1024 ** 2
CYCLE_SECONDS = 300


def get_cfg() -> dict:
    try:
        raw = json.loads(db.get_setting(KEY, "{}") or "{}")
    except Exception:
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "free_bytes": max(0, int(raw.get("free_bytes", 100 * GB))),
        "daily_bytes": max(0, int(raw.get("daily_bytes", 5 * GB))),
    }


def save_cfg(cfg: dict) -> None:
    db.set_setting(KEY, json.dumps({"enabled": bool(cfg["enabled"]), "free_bytes": int(cfg["free_bytes"]), "daily_bytes": int(cfg["daily_bytes"])}))


def set_enabled(on: bool) -> dict:
    c = get_cfg(); c["enabled"] = bool(on); save_cfg(c); return c


def fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n >= GB:
        v = n / GB
        return f"{v:g} گیگابایت" if abs(v - round(v)) > 1e-9 else f"{int(round(v))} گیگابایت"
    return f"{round(n / MB):g} مگابایت"


_UNIT_RE = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(gb|g|گیگ|گیگابایت|mb|m|مگ|مگابایت)?\s*$", re.I)


def parse_amount(text: str, default_unit: str = "gb") -> int | None:
    """«5» ← واحد پیش‌فرض، «500MB»، «2.5 گیگ» → بایت؛ نامعتبر → None."""
    t = (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٫", "0123456789."))
    m = _UNIT_RE.match(t)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or default_unit).lower()
    mult = MB if unit in ("mb", "m", "مگ", "مگابایت") else GB
    return int(val * mult)


def enable_denied(cfg: dict | None) -> str | None:
    """اگر سرویس به‌خاطر مصرف منصفانه متوقف است، پیام امتناع از «فعال‌سازی» دستی کاربر؛ وگرنه None."""
    try:
        if cfg and int(cfg.get("payg_paused") or 0):
            # PAYG متوقف‌شده به‌خاطر اتمام کیف پول: فعال‌سازی دستی ممنوع؛ بعد از شارژ، خودکار فعال می‌شود
            try:
                _w = int((db.get_user_by_id(cfg["user_id"]) or {}).get("wallet") or 0)
            except Exception:
                _w = 0
            if _w > 0:
                return "⏳ کیف پولت شارژ شده؛ سرویس PAYG در چند لحظه به‌صورت خودکار فعال می‌شود."
            return "⛔ این سرویس PAYG به‌دلیل اتمام موجودی کیف پول متوقف شده و فعال‌سازی دستی ممکن نیست. کیف پولت را شارژ کن تا سرویس به‌صورت خودکار فعال شود."
        if cfg and int(cfg.get("fup_blocked") or 0) and get_cfg()["enabled"]:
            return "⛔ این سرویس به‌خاطر رسیدن به سقف مصرف منصفانه‌ی امروز متوقف است و تا ساعت ۰۰:۰۰ امشب قابل فعال‌سازی نیست."
    except Exception:
        pass
    return None


def admin_release(config_id: int) -> None:
    """آزادسازی دستی توسط ادمین؛ سهمیه‌ی امروز از همین لحظه دوباره شروع می‌شود."""
    db.set_fup_state(config_id, blocked=False, base=None, day=None)


def _today() -> str:
    from utils import now_tehran_naive
    return now_tehran_naive().strftime("%Y-%m-%d")


def _is_active(snap: dict) -> bool:
    st = snap.get("status")
    if st is None:
        return True
    return str(st).strip().lower() in ("active", "enabled", "1", "true", "on")


def _name(cfg: dict) -> str:
    return str(cfg.get("service_id") or cfg.get("plan") or cfg.get("id"))


async def _notify(bot, cfg: dict, user: dict, key: str, notif_key: str, c: dict) -> None:
    if not notif_settings.enabled(notif_key):
        return
    try:
        import ui_editor
        from aiogram.types import MessageEntity
        from keyboards import back_button
        from utils import send_notification_sticker
        default = ui_editor.SCREENS[key]["default"]
        text, raw = ui_editor.render_template(key, {
            "service_name": _name(cfg), "plan": cfg.get("plan") or "-",
            "free_limit": fmt_bytes(c["free_bytes"]), "daily_limit": fmt_bytes(c["daily_bytes"]),
        }, fallback=default)
        ents = [MessageEntity(type=e["type"], offset=e["offset"], length=e["length"], custom_emoji_id=e.get("custom_emoji_id")) for e in (raw or [])]
        try:
            await send_notification_sticker(bot, int(user["telegram_id"]), key)
        except Exception:
            pass
        _kb = back_button(f"viewconfig_{cfg['id']}", "⚙️ مدیریت سرویس", screen=key)
        from utils import send_rich_template as _srt
        _vals = {
            "service_name": _name(cfg), "plan": cfg.get("plan") or "-",
            "free_limit": fmt_bytes(c["free_bytes"]), "daily_limit": fmt_bytes(c["daily_bytes"]),
        }
        if await _srt(bot, int(user["telegram_id"]), key, _vals, reply_markup=_kb) is None:
            await bot.send_message(int(user["telegram_id"]), text, entities=ents or None, reply_markup=_kb)
    except Exception:
        logger.exception("ارسال اعلان مصرف منصفانه ناموفق بود")


async def _release_blocked(cfg: dict, panel: dict) -> bool:
    ok, msg = await panels.enable_service(panel, cfg["service_id"])
    if ok:
        db.set_fup_state(cfg["id"], blocked=False)
        db.set_config_link_disabled(cfg["id"], False)
        return True
    logger.warning("FUP enable failed cfg=%s: %s", cfg.get("id"), msg)
    return False


async def run_cycle(bot) -> None:
    c = get_cfg()
    if not c["enabled"]:
        # خاموش‌شدن سیستم → همه‌ی سرویس‌های متوقف‌شده آزاد می‌شوند
        for cfg in db.get_fup_blocked_configs():
            try:
                panel = db.get_vpn_panel(cfg["panel_id"]) if cfg.get("panel_id") else None
                if panel and panel.get("enabled") and await _release_blocked(cfg, panel):
                    db.fup_reset_config(cfg["id"])
            except Exception:
                logger.exception("FUP release failed cfg=%s", cfg.get("id"))
        return
    today = _today()
    for cfg in db.get_fup_candidates():
        try:
            panel = db.get_vpn_panel(cfg["panel_id"])
            if not panel or not panel.get("enabled"):
                continue
            ok, snap, _msg = await panels.get_service_snapshot(panel, cfg["service_id"])
            if not ok or not isinstance(snap, dict):
                continue
            if snap.get("total"):  # حجم‌دار (نامحدود نیست)
                if cfg.get("fup_started") or cfg.get("fup_blocked"):
                    if cfg.get("fup_blocked"):
                        await _release_blocked(cfg, panel)
                    db.fup_reset_config(cfg["id"])
                continue
            user = db.get_user_by_id(cfg["user_id"])
            if not user:
                continue
            used = int(snap.get("used") or 0)
            blocked = bool(cfg.get("fup_blocked"))
            started = bool(cfg.get("fup_started"))

            if started and cfg.get("fup_day") != today:
                # روز جدید (۰۰:۰۰): سهمیه صفر می‌شود
                if blocked and not await _release_blocked(cfg, panel):
                    continue
                db.set_fup_state(cfg["id"], blocked=False, base=used, day=today)
                continue

            if not started:
                if used >= c["free_bytes"]:
                    # رسیدن به حد مصرف آزاد: سرویس همان روز غیرفعال می‌شود و از ۰۰:۰۰ فردا فعال + سهمیه‌ی روزانه شروع می‌شود
                    ok_d, msg_d = await panels.disable_service(panel, cfg["service_id"])
                    if not ok_d:
                        logger.warning("FUP initial disable failed cfg=%s: %s", cfg.get("id"), msg_d)
                        continue  # چرخه‌ی بعد دوباره تلاش می‌شود
                    db.set_fup_state(cfg["id"], started=True, blocked=True, base=used, day=today)
                    db.set_config_link_disabled(cfg["id"], True)
                    await _notify(bot, cfg, user, "notif_fup_start", "fup_start", c)
                continue

            base = cfg.get("fup_base_bytes")
            if base is None or used < int(base):
                db.set_fup_state(cfg["id"], base=used, day=today)
                continue
            if blocked:
                # دورزدن (مثلاً تمدید/فعال‌سازی مستقیم روی پنل): دوباره غیرفعال شود
                if _is_active(snap):
                    await panels.disable_service(panel, cfg["service_id"])
                continue
            if used - int(base) >= c["daily_bytes"]:
                ok_d, msg_d = await panels.disable_service(panel, cfg["service_id"])
                if ok_d:
                    db.set_fup_state(cfg["id"], blocked=True)
                    db.set_config_link_disabled(cfg["id"], True)
                    await _notify(bot, cfg, user, "notif_fup_blocked", "fup_blocked", c)
                else:
                    logger.warning("FUP disable failed cfg=%s: %s", cfg.get("id"), msg_d)
        except Exception:
            logger.exception("FUP cycle failed cfg=%s", cfg.get("id"))


async def fup_loop(bot) -> None:
    import asyncio
    while True:
        try:
            await run_cycle(bot)
        except Exception:
            logger.exception("fup loop")
        await asyncio.sleep(CYCLE_SECONDS)
