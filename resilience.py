"""Panel resilience, circuit breaker, health and periodic synchronization."""
import asyncio, time, logging
from datetime import datetime
import database as db
import panels

logger = logging.getLogger(__name__)
FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS = 30

async def before_panel_call(panel_id: int) -> tuple[bool, str | None]:
    state = db.get_panel_health(panel_id)
    if not state:
        return True, None
    if state.get("circuit_open"):
        opened = float(state.get("opened_at") or 0)
        if time.time() - opened < COOLDOWN_SECONDS:
            return False, f"مدار حفاظتی پنل باز است؛ تا {COOLDOWN_SECONDS - int(time.time()-opened)} ثانیه دیگر تلاش می‌شود."
        # half-open: allow one probe
        if not db.claim_panel_probe(panel_id):
            return False, "پنل در حال بررسی سلامت است؛ کمی بعد دوباره تلاش کنید."
    return True, None

def record_panel_result(panel_id: int, ok: bool, latency_ms: int | None = None, error: str | None = None):
    db.record_panel_health_result(panel_id, ok, latency_ms, error, FAILURE_THRESHOLD)

async def panel_health_check(panel: dict) -> dict:
    pid = int(panel["id"])
    allowed, reason = await before_panel_call(pid)
    if not allowed:
        st = db.get_panel_health(pid) or {}
        return {"ok": False, "latency_ms": None, "error": reason, "circuit_open": True, "state": st}
    started = time.perf_counter()
    try:
        ok, _data, msg = await panels.get_panel_info(panel)
        latency = int((time.perf_counter() - started) * 1000)
        record_panel_result(pid, ok, latency, None if ok else msg)
        return {"ok": ok, "latency_ms": latency, "error": None if ok else msg, "circuit_open": not ok and (db.get_panel_health(pid) or {}).get("circuit_open", False), "state": db.get_panel_health(pid) or {}}
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        record_panel_result(pid, False, latency, str(exc))
        return {"ok": False, "latency_ms": latency, "error": str(exc), "circuit_open": (db.get_panel_health(pid) or {}).get("circuit_open", False), "state": db.get_panel_health(pid) or {}}

async def sync_panel_configs(limit: int = 500) -> dict:
    """Refresh local panel metadata from the authoritative panel API."""
    configs = db.get_panel_backed_active_configs(limit)
    result = {"checked": 0, "updated": 0, "failed": 0}
    for cfg in configs:
        result["checked"] += 1
        panel = db.get_vpn_panel(cfg.get("panel_id")) if cfg.get("panel_id") else None
        if not panel or not panel.get("enabled") or not cfg.get("service_id"):
            continue
        allowed, _ = await before_panel_call(int(panel["id"]))
        if not allowed:
            continue
        started = time.perf_counter()
        try:
            ok, snap, msg = await panels.get_service_snapshot(panel, str(cfg["service_id"]))
            record_panel_result(int(panel["id"]), ok, int((time.perf_counter()-started)*1000), None if ok else msg)
            if not ok or not snap:
                result["failed"] += 1
                continue
            db.sync_config_from_panel(int(cfg["id"]), snap)
            result["updated"] += 1
        except Exception as exc:
            result["failed"] += 1
            record_panel_result(int(panel["id"]), False, int((time.perf_counter()-started)*1000), str(exc))
            logger.exception("panel sync failed cfg=%s", cfg.get("id"))
    return result
