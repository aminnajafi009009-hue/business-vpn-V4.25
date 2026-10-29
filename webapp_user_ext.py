"""Extended end-user mini-app API: service detail ("sub page"), usage history,
revoke/toggle link, app deep-link relay, home / profile / settings.

register(app, api) is called by webapp_api._register_extensions().
"""
from __future__ import annotations

import asyncio
import loopsafe
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Response, g, jsonify, request

import crypto
import database as db
import panels
from subscription import days_remaining

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
_ALLOWED_SCHEMES = {
    "happ", "v2box", "v2rayng", "v2rayn", "hiddify", "streisand", "shadowrocket", "karing",
    "sing-box", "clash", "flclash", "clashmeta", "nekobox", "v2raytun", "foxray", "loon", "surge",
    "quantumult-x", "hexated", "incy",
}


def to_epoch(v):
    """ISO string / epoch (s or ms) -> epoch seconds (int) or None."""
    if v is None or v == "" or v is False:
        return None
    try:
        if isinstance(v, (int, float)):
            f = float(v)
            if f <= 0:
                return None
            return int(f / 1000) if f > 1e11 else int(f)
        s = str(v).strip()
        if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
            return to_epoch(float(s))
        s = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except Exception:
        return None


_UA_APPS = [
    # (regex, name, platform hint)
    (r"happ", "Happ", None),
    (r"v2box", "V2Box", None),
    (r"v2rayng", "v2rayNG", "Android"),
    (r"v2rayn(?!g)", "v2rayN", "Windows"),
    (r"hiddify", "Hiddify", None),
    (r"streisand", "Streisand", "iOS"),
    (r"shadowrocket", "Shadowrocket", "iOS"),
    (r"karing", "Karing", None),
    (r"sing-?box|sfa|sfi|sfm", "sing-box", None),
    (r"flclash", "FlClash", None),
    (r"clash[\s._-]?meta|mihomo|verge", "Clash Meta", None),
    (r"clash", "Clash", None),
    (r"nekobox|nekoray", "NekoBox", None),
    (r"quantumult", "Quantumult X", "iOS"),
    (r"loon", "Loon", "iOS"),
    (r"surge", "Surge", None),
    (r"v2raytun", "v2RayTun", None),
    (r"foxray", "FoxRay", None),
    (r"hexated", "Hexated", None),
    (r"incy", "INCY", None),
    (r"v2ray", "V2Ray", None),
    (r"xray|streamsettings", "Xray", None),
    (r"python|curl|wget|okhttp|go-http", "برنامه/ابزار", None),
    (r"mozilla|chrome|safari|firefox|edg", "مرورگر", None),
]
_PLATFORMS = [
    (r"iphone|ipad|ios|cfnetwork|darwin/\d+.*mobile", "iOS"),
    (r"android", "Android"),
    (r"windows|win32|win64|winnt", "Windows"),
    (r"macintosh|mac os|macos|darwin", "macOS"),
    (r"linux|ubuntu|debian", "Linux"),
]


def parse_user_agent(ua: str | None):
    """Return {name, version, platform, device} or None."""
    if not ua or not str(ua).strip():
        return None
    s = str(ua).strip()
    low = s.lower()
    name = None
    plat_hint = None
    for rx, nm, hint in _UA_APPS:
        if re.search(rx, low):
            name, plat_hint = nm, hint
            break
    if name is None:
        name = s.split("/")[0].split(" ")[0][:32] or "نامشخص"
    version = None
    m = re.search(re.escape(name.split()[0].lower()) + r"[/ _-]?v?(\d+(?:\.\d+){0,3})", low)
    if m:
        version = m.group(1)
    else:
        m = re.search(r"/(\d+(?:\.\d+){0,3})", s)
        if m:
            version = m.group(1)
    platform = None
    for rx, pl in _PLATFORMS:
        if re.search(rx, low):
            platform = pl
            break
    platform = platform or plat_hint
    device = None
    m = re.search(r"\(([^)]{3,60})\)", s)
    if m:
        inner = m.group(1)
        parts = [p.strip() for p in inner.split(";") if p.strip()]
        for p in parts:
            pl = p.lower()
            if re.fullmatch(r"(linux|u|ios|android|windows nt [\d.]+|wow64|x64|arm64|arm|win64|kfhwi|mobile|like.*)", pl):
                continue
            if re.match(r"(android|ios|iphone os|cpu|windows|mac os|macintosh|darwin)", pl):
                continue
            device = p[:40]
            break
    return {"name": name, "version": version, "platform": platform, "device": device}


def _raw(snapshot):
    return (snapshot or {}).get("raw") or {}


def _pick(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return None


def _deep_find(obj, key_regex, accept=None, _depth=0):
    """اولین مقدار غیرخالی که نام کلیدش با key_regex بخواند را در هر عمقی از پاسخ پنل پیدا می‌کند
    (پنل‌های مختلف همین فیلدها را با نام‌ها/سطح‌های متفاوت برمی‌گردانند: online_at / onlineAt / userTraffic.onlineAt …)."""
    if _depth > 4:
        return None
    rx = re.compile(key_regex, re.I)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if rx.search(str(k)) and v not in (None, "", [], {}) and (accept is None or accept(v)):
                return v
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _deep_find(v, key_regex, accept, _depth + 1)
                if r not in (None, "", [], {}):
                    return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            if isinstance(v, (dict, list)):
                r = _deep_find(v, key_regex, accept, _depth + 1)
                if r not in (None, "", [], {}):
                    return r
    return None


def build_connection(snapshot):
    raw = _raw(snapshot)
    node = raw.get("user") if isinstance(raw.get("user"), dict) else raw
    ua = _pick(node, "sub_last_user_agent", "last_user_agent", "user_agent", "ua",
               "subLastUserAgent", "lastUserAgent", "userAgent")
    if not ua:
        ua = _deep_find(raw, r"user_?agent", accept=lambda v: isinstance(v, str))
    online_at = to_epoch(_pick(node, "online_at", "last_online_at", "lastOnline", "last_online", "last_connection",
                               "onlineAt", "lastOnlineAt", "lastConnectedAt", "last_connected_at", "lastSeen", "last_seen"))
    if not online_at:
        online_at = to_epoch(_deep_find(
            raw, r"^(online_?at|last_?online(_?at)?|last_?conn(ect(ed|ion))?(_?at)?|last_?seen(_?at)?|last_?activity(_?at)?)$",
            accept=lambda v: to_epoch(v) is not None))
    last_fetch = to_epoch(_pick(node, "sub_updated_at", "last_sub_fetch", "sub_last_fetch_at",
                                "subUpdatedAt", "subLastOpenedAt", "lastSubFetch"))
    if not last_fetch:
        last_fetch = to_epoch(_deep_find(
            raw, r"^(sub_?(updated|last_?opened|last_?fetch)(_?at)?|last_?sub_?fetch(_?at)?)$",
            accept=lambda v: to_epoch(v) is not None))
    ip = _pick(node, "last_ip", "ip", "online_ip", "last_connected_ip", "lastIp", "lastConnectedIp")
    if isinstance(ip, list):
        ip = ip[0] if ip else None
    devices = []
    raw_devs = _pick(node, "hwid_devices", "devices", "hwids", "hwid_list", "hwidDevices")
    if not isinstance(raw_devs, list):
        raw_devs = _deep_find(raw, r"^(hwid_?devices|hwids?|devices|hwid_?list|connected_?devices)$",
                              accept=lambda v: isinstance(v, list))
    if isinstance(raw_devs, list):
        for d in raw_devs[:20]:
            if isinstance(d, str):
                devices.append({"name": d[:60], "platform": None, "last_seen": None, "ip": None})
            elif isinstance(d, dict):
                devices.append({
                    "name": str(_pick(d, "device_model", "deviceModel", "model", "name", "hwid", "id") or "دستگاه")[:60],
                    "platform": _pick(d, "platform", "os", "device_os", "deviceOs", "osVersion"),
                    "last_seen": to_epoch(_pick(d, "last_seen", "last_seen_at", "updated_at", "updatedAt", "created_at", "createdAt")),
                    "ip": _pick(d, "ip", "last_ip"),
                })
    limit = _pick(node, "hwid_limit", "device_limit", "devices_limit", "hwidDeviceLimit", "deviceLimit", "hwidLimit")
    if limit is None:
        limit = _deep_find(raw, r"^(hwid_?(device_?)?limit|device_?limit|devices_?limit|max_?devices)$",
                           accept=lambda v: isinstance(v, (int, float, str)) and str(v).lstrip("-").isdigit())
    try:
        limit = int(limit) if limit not in (None, "") else None
    except Exception:
        limit = None
    now = int(time.time())
    return {
        "online_at": online_at,
        "is_online": bool(online_at and now - online_at <= 120),
        "last_sub_fetch_at": last_fetch,
        "user_agent": str(ua)[:300] if ua else None,
        "app": parse_user_agent(ua),
        "ip": str(ip) if ip else None,
        "devices": devices,
        "devices_known": isinstance(raw_devs, list),
        "devices_limit": limit,
    }


def service_names(cfg, panel, snapshot):
    name = None
    if snapshot:
        name = snapshot.get("username")
    name = name or cfg.get("service_id")
    if isinstance(name, str) and ":" in name and panel and panel.get("panel_type") == "threexui":
        name = (snapshot or {}).get("username") or name.split(":", 1)[1][:12]
    pkg = (cfg.get("plan") or "").strip() or None
    name = (str(name).strip() if name else None) or None
    if name and pkg and name == pkg:
        name = None
    # نام بسته به‌صورت «نام | حجم | مدت» ذخیره می‌شود؛ اگر بخش اول همان یوزرنیم سرویس باشد، تکرارش در عنوان
    # («نام | نام | حجم | مدت») حذف می‌شود.
    if name and pkg and "|" in pkg:
        _segs = [x.strip() for x in pkg.split("|")]
        if _segs and _segs[0].lower() == str(name).lower():
            _rest = " | ".join(x for x in _segs[1:] if x)
            pkg = _rest or None
    title = f"{name} | {pkg}" if name and pkg else (name or pkg or "سرویس")
    _ct = (cfg.get("custom_title") or "").strip()
    if _ct:
        title = _ct
    return name, pkg, title


def enrich_list_item(item: dict, cfg: dict, panel, live: dict | None):
    """Used by webapp_api.api_services to add title/status/online/app fields."""
    snap = live or None
    name, pkg, title = service_names(cfg, panel, snap)
    conn = build_connection(snap) if snap else None
    item.update({
        "service_name": name, "package_name": pkg, "title": title,
        "online_at": conn["online_at"] if conn else None,
        "last_app": (conn["app"] or {}).get("name") if conn and conn.get("app") else None,
        "link_disabled": bool(cfg.get("sub_link_disabled")),
    })
    try:
        if snap and snap.get("used") is not None:
            db.record_usage_snapshot(cfg["id"], snap.get("used"))
    except Exception:
        pass
    return item


# ----------------------------------------------------------------------------
def register(app, api):
    from webapp_common import make_helpers, tx_view
    H = make_helpers(api)
    auth = api.require_telegram_auth

    # ---- service detail ----------------------------------------------------
    @app.route("/api/services/<int:config_id>", methods=["GET"])
    @auth
    def api_service_detail(config_id):
        cfg = api._own_config_or_none(config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "not_found"}), 404
        panel, snap, err = api._live_panel_snapshot(cfg, fresh=True)
        live = snap or {}
        sub_link = None
        if panel and panel.get("panel_type") == "shahrah":
            sub_link = live.get("link")
        if not sub_link:
            try:
                sub_link = crypto.decrypt_config(cfg["config"])
            except Exception:
                sub_link = None
        if sub_link and not str(sub_link).lower().startswith(("http://", "https://")):
            sub_link = None
        m = api._service_metrics(cfg, live, snap is not None)
        total, used = m["total"], m["used"]
        expire_ts = m["expire_ts"]
        name, pkg, title = service_names(cfg, panel, snap)
        if snap and used is not None:
            db.record_usage_snapshot(cfg["id"], used)
        managed = bool(panel and cfg.get("service_id") and panel.get("panel_type") in panels.PANEL_TYPES)
        out = {
            "id": cfg["id"], "title": title, "service_name": name, "package_name": pkg,
            "status": m["status"], "created_at": cfg["created_at"], "expiry": cfg.get("expiry"),
            "expire_ts": expire_ts,
            "remaining_days": m["remaining_days"], "unlimited_time": m["unlimited_time"], "unlimited_volume": m["unlimited_volume"],
            "usage": {"total": total, "used": used, "remaining": m["remaining"], "percent": m["percent"],
                      "upload": live.get("upload"), "download": live.get("download")},
            "sub_link": sub_link, "has_qr": bool(cfg.get("qr_file_id") or sub_link),
            "panel_type": panel.get("panel_type") if panel else cfg.get("source"),
            "panel_name": panel.get("name") if panel else None,
            "connection": build_connection(snap) if snap else {
                "online_at": None, "is_online": False, "last_sub_fetch_at": None, "user_agent": None,
                "app": None, "ip": None, "devices": [], "devices_limit": None},
            "can": {
                "renew": bool(db.config_allows_renew(cfg)),
                "revoke": bool(managed and panel.get("panel_type") != "shahrah"),
                "delete": True,
                "configs": bool(managed or sub_link),
                "toggle": bool(managed),
            },
            "link_disabled": bool(cfg.get("sub_link_disabled")),
            "live": snap is not None,
        }
        return jsonify(out)

    # ---- usage history -----------------------------------------------------
    @app.route("/api/services/<int:config_id>/usage-history", methods=["GET"])
    @auth
    def api_service_usage_history(config_id):
        cfg = api._own_config_or_none(config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "not_found"}), 404
        try:
            days = max(1, min(90, int(request.args.get("days", 30))))
        except Exception:
            days = 30
        today = datetime.now().date()
        start = today - timedelta(days=days)
        # one extra day before the window to compute the first delta
        rows = db.get_usage_snapshots(cfg["id"], (start - timedelta(days=1)).strftime("%Y-%m-%d"))
        snaps = {r["day"]: int(r["used"]) for r in rows}
        points, prev = [], None
        # سرویسی که داخل همین بازه ساخته شده از صفر شروع شده؛ بدون این پایه، مصرف روز(های) اول (قبل از اولین
        # اسنپ‌شات) در نمودار گم می‌شد و «جمع دوره» از مصرف واقعی کمتر بود.
        try:
            _cd = datetime.strptime(str(cfg.get("created_at") or "")[:10], "%Y-%m-%d").date()
        except Exception:
            _cd = None
        if _cd and _cd >= start - timedelta(days=1):
            prev = 0
        # find baseline: last snapshot at or before the day before window start
        for d_str in sorted(snaps):
            if d_str <= (start - timedelta(days=1)).strftime("%Y-%m-%d"):
                prev = snaps[d_str]
        for i in range(days):
            d = start + timedelta(days=i + 1)
            ds = d.strftime("%Y-%m-%d")
            cur = snaps.get(ds)
            used = 0
            if cur is not None:
                if prev is not None:
                    used = max(0, cur - prev) if cur >= prev else cur  # counter reset => treat as new
                prev = cur
            points.append({"date": ds, "used": int(used)})
        total_period = sum(p["used"] for p in points)
        peak = max(points, key=lambda p: p["used"]) if points else None
        return jsonify({
            "days": days, "points": points, "total_period": total_period,
            "avg_per_day": int(total_period / days) if days else 0,
            "peak": peak if peak and peak["used"] > 0 else None,
            "has_data": len(snaps) >= (1 if _cd and _cd >= start - timedelta(days=1) else 2), "source": "snapshots",
        })

    # ---- revoke / toggle ---------------------------------------------------
    def _managed(cfg):
        if not cfg.get("panel_id") or not cfg.get("service_id"):
            return None
        return db.get_vpn_panel(cfg["panel_id"])

    @app.route("/api/services/<int:config_id>/revoke", methods=["POST"])
    @auth
    def api_service_revoke(config_id):
        cfg = api._own_config_or_none(config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "not_found"}), 404
        panel = _managed(cfg)
        if not panel:
            return jsonify({"success": False, "error": "not_supported", "message": "تغییر خودکار لینک برای این سرویس فعال نیست؛ با پشتیبانی تماس بگیرید."}), 400
        try:
            ok, link, new_id, _raw_data, msg = loopsafe.run(panels.regenerate_sub_link(panel, cfg["service_id"]))
        except Exception as exc:
            log.exception("revoke failed")
            return jsonify({"success": False, "error": "panel_error", "message": str(exc)}), 502
        if not ok or not link:
            return jsonify({"success": False, "error": "panel_error", "message": msg or "ساخت لینک جدید ممکن نشد."}), 502
        enc = crypto.encrypt_config(link)
        db.replace_config_link(config_id, enc, new_id if (new_id and new_id != cfg.get("service_id")) else None)
        db.set_config_link_disabled(config_id, False)
        api.invalidate_live_snapshot(cfg)
        api.invalidate_live_snapshot(config_id)
        return jsonify({"success": True, "sub_link": link})

    @app.route("/api/services/<int:config_id>/toggle", methods=["POST"])
    @auth
    def api_service_toggle(config_id):
        cfg = api._own_config_or_none(config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "not_found"}), 404
        panel = _managed(cfg)
        if not panel:
            return jsonify({"success": False, "error": "not_supported", "message": "این قابلیت برای این سرویس فعال نیست."}), 400
        body = request.get_json(silent=True) or {}
        enable = bool(body.get("enabled"))
        if enable:
            import fair_usage
            _deny = fair_usage.enable_denied(cfg)
            if _deny:
                return jsonify({"success": False, "error": "fair_usage_blocked", "message": _deny}), 403
        try:
            fn = panels.enable_service if enable else panels.disable_service
            ok, msg = loopsafe.run(fn(panel, cfg["service_id"]))
        except Exception as exc:
            return jsonify({"success": False, "error": "panel_error", "message": str(exc)}), 502
        if not ok:
            return jsonify({"success": False, "error": "panel_error", "message": msg}), 502
        db.set_config_link_disabled(config_id, not enable)
        api.invalidate_live_snapshot(cfg)
        return jsonify({"success": True, "link_disabled": not enable})

    # ---- deep-link relay (public) -----------------------------------------
    @app.route("/open-app", methods=["GET"])
    def open_app():
        to = (request.args.get("to") or "").strip()
        scheme = to.split(":", 1)[0].lower() if ":" in to else ""
        if scheme not in _ALLOWED_SCHEMES or len(to) > 4000 or re.search(r"[\s<>\"'`]", to):
            return Response("invalid", status=400, mimetype="text/plain")
        fb = (request.args.get("fallback") or "").strip()
        if fb:
            u = urlparse(fb)
            if u.scheme not in ("http", "https") or not u.netloc:
                fb = ""
        e = html.escape(to, quote=True)
        js = to.replace("\\", "\\\\").replace("'", "\\'")
        fb_html = f'<a class="b2" href="{html.escape(fb, quote=True)}">دانلود برنامه</a>' if fb else ""
        page = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>باز کردن برنامه</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#080a0f;color:#f2f4f9;font-family:system-ui,Tahoma,sans-serif;text-align:center;padding:24px}}
.c{{max-width:380px}}h1{{font-size:18px;margin:0 0 8px}}p{{color:#a4abbd;font-size:13px;line-height:1.9;margin:0 0 18px}}
a.b{{display:block;padding:14px;border-radius:14px;background:linear-gradient(135deg,#6c8cff,#35d6c0);color:#06101f;font-weight:700;text-decoration:none;margin-bottom:10px}}
a.b2{{display:block;padding:12px;border-radius:14px;border:1px solid #2a3040;color:#f2f4f9;text-decoration:none;font-size:13px}}</style></head>
<body><div class="c"><h1>در حال باز کردن برنامه…</h1>
<p>اگر برنامه به‌صورت خودکار باز نشد، دکمه‌ی زیر را بزنید. اگر برنامه نصب نیست ابتدا آن را نصب کنید.</p>
<a class="b" href="{e}">باز کردن برنامه</a>{fb_html}</div>
<script>setTimeout(function(){{location.href='{js}'}},120)</script></body></html>"""
        r = Response(page, mimetype="text/html")
        r.headers["Cache-Control"] = "no-store"
        r.headers["X-Robots-Tag"] = "noindex"
        return r

    # ---- profile / home / settings ----------------------------------------
    TIERS = [("برنزی", 0), ("نقره‌ای", 2_000_000), ("طلایی", 10_000_000), ("الماس", 30_000_000)]

    def _rank(total):
        try:
            custom = db.get_setting("loyalty_tiers", "")
            if custom:
                import json
                parsed = json.loads(custom)
                tiers = [(str(a), int(b)) for a, b in parsed]
            else:
                tiers = TIERS
        except Exception:
            tiers = TIERS
        lvl = 0
        for i, (_n, th) in enumerate(tiers):
            if total >= th:
                lvl = i
        nxt = tiers[lvl + 1][1] if lvl + 1 < len(tiers) else None
        cur_th = tiers[lvl][1]
        prog = 100 if nxt is None else max(0, min(100, round((total - cur_th) / max(1, nxt - cur_th) * 100)))
        return {"name": tiers[lvl][0], "level": lvl + 1, "progress": prog, "next_at": nxt}

    def _orders_count(uid):
        try:
            cur = db.get_connection().cursor()
            cur.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND status IN ('paid','fulfilled','completed','delivered')", (uid,))
            return int(cur.fetchone()[0] or 0)
        except Exception:
            return 0

    @app.route("/api/profile", methods=["GET"])
    @auth
    def api_profile():
        u = g.user
        agent = db.get_agent(g.tg_user["id"])
        return jsonify({
            "name": u["name"], "username": g.tg_user.get("username"), "telegram_id": u["telegram_id"],
            "joined": u["joined"], "wallet": u["wallet"], "locked_wallet": u["locked_wallet"],
            "total_purchase": u["total_purchase"], "orders_count": _orders_count(u["id"]),
            "services_count": len([c for c in db.get_configs(u["id"]) if not c.get("deleted")]),
            "invited_count": u["invited_count"], "successful_invites": u["successful_invites"],
            "rank": _rank(int(u["total_purchase"] or 0)),
            "agent_percent": int(agent["vip_discount_percent"]) if agent else 0,
            "is_agent": bool(agent),
            "referral_enabled": db.referral_enabled(),
            "photo_url": g.tg_user.get("photo_url"),
        })

    def _payg_on():
        try:
            return bool(db.get_payg_settings()["enabled"]) and bool(db.usable_map_options("payg", ("marzban", "pasargad", "pasargad_wg", "threexui")))
        except Exception:
            return False

    @app.route("/api/home", methods=["GET"])
    @auth
    def api_home():
        u = g.user
        cfgs = [c for c in db.get_configs(u["id"]) if not c.get("deleted")]
        total = active = used_sum = quota_sum = 0
        attention = []
        now = int(time.time())
        try:
            _snaps = api._live_snapshots(cfgs)
        except Exception:
            _snaps = [(c, None, None, None) for c in cfgs]
        for cfg, panel, snap, _e in _snaps:
            live = snap or {}
            exp = to_epoch(live.get("expire")) if live.get("expire") else None
            if exp and exp <= now:
                continue
            if not panel and api.is_config_expired(cfg):
                continue
            total += 1
            st = (live.get("status") or "active")
            if st in ("active", "on_hold", None):
                active += 1
            t, uu = live.get("total"), live.get("used")
            if t:
                quota_sum += int(t)
            if uu:
                used_sum += int(uu)
            if snap and uu is not None:
                db.record_usage_snapshot(cfg["id"], uu)
            name, pkg, title = service_names(cfg, panel, snap)
            pct = round(uu / t * 100) if t and uu is not None else None
            rem = days_remaining(live.get("expire")) if live.get("expire") else None
            if rem is not None and rem <= 3:
                attention.append({"id": cfg["id"], "title": title, "kind": "expiring", "remaining_days": rem, "percent": pct})
            elif pct is not None and pct >= 85:
                attention.append({"id": cfg["id"], "title": title, "kind": "low_traffic", "remaining_days": rem, "percent": pct})
        activity = []
        try:
            for t in db.get_transactions(u["id"], limit=6):
                _v = tx_view(t); activity.append({"type": t.get("type"), "title": t.get("description") or t.get("type") or "تراکنش", "amount": _v["amount"], "direction": _v["direction"], "at": t.get("created_at")})
        except Exception:
            pass
        free_ok = False
        try:
            _ents = db.get_enabled_free_test_panels()
            _keys = [db.free_test_plan_key(e["num"]) for e in _ents] or [api.FREE_TEST_PLAN_KEY]
            free_ok = bool(db.get_effective_plan(api.FREE_TEST_PLAN_KEY)) and any(not db.has_used_free_test(u["id"], k) for k in _keys) and db.is_orders_enabled()
        except Exception:
            free_ok = False
        ann = None
        try:
            ann = (db.get_setting("miniapp_announcement", "") or "").strip() or None
        except Exception:
            pass
        return jsonify({
            "name": u["name"], "wallet": u["wallet"], "orders_enabled": db.is_orders_enabled(),
            "free_trial_available": free_ok, "custom_build_enabled": db.get_setting("custom_build_enabled", "1") != "0", "renewal_enabled": db.is_renewal_enabled(),
            "payg_enabled": _payg_on(),
            "services": {"total": total, "active": active, "total_used": used_sum, "total_quota": quota_sum},
            "attention": attention[:6], "activity": activity, "announcement": ann,
        })

    @app.route("/api/settings", methods=["GET", "POST"])
    @auth
    def api_user_settings():
        uid = g.user["id"]
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            notif = body.get("notifications") if isinstance(body.get("notifications"), dict) else None
            kw = {"notifications": notif}
            if "default_app" in body:
                kw["default_app"] = body.get("default_app")
            prefs = db.set_user_prefs(uid, **kw)
        else:
            prefs = db.get_user_prefs(uid)
        return jsonify({
            "notifications": {k: bool(prefs[k]) for k in ("usage_alerts", "expiry_alerts", "wallet_alerts", "promo")},
            "default_app": prefs.get("default_app"),
        })
