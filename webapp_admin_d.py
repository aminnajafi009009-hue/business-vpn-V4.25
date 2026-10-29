"""Admin mini-app API, part D: panel CRUD, mapping (catalog), free-test entries, renewal settings, bot info."""
from __future__ import annotations

import asyncio
import loopsafe
import json
import logging

from flask import request

import bot_info
import database as db
import panels
from webapp_common import make_helpers

log = logging.getLogger(__name__)
_SCOPES = ("free_test", "payg", "custom_build", "vip_plan", "vip_category")
_PANEL_TYPES = ("marzban", "pasargad", "threexui", "shahrah", "pasargad_wg")


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _run(coro, timeout=25):
    return loopsafe.run(asyncio.wait_for(coro, timeout))


def _mask(s):
    s = str(s or "")
    return ("•" * 4 + s[-3:]) if len(s) > 4 else ("•" * len(s))


def _panel_view(p):
    return {"id": p["id"], "type": p.get("panel_type"), "name": p.get("name"), "base_url": p.get("base_url"),
            "username": p.get("username") or "", "has_password": bool(p.get("password")), "has_api_key": bool(p.get("api_key")),
            "auth_method": p.get("auth_method") or "userpass", "enabled": bool(p.get("enabled"))}


def _set_duration(e, unit, value):
    """مدت تست: روزانه (۱–۳۰) یا ساعتی (۱–۷۲۰) — مثل ربات."""
    unit = "hours" if str(unit or "days") == "hours" else "days"
    try:
        v = int(float(value))
    except Exception:
        return "مدت نامعتبر است"
    mx = 720 if unit == "hours" else 30
    if not (1 <= v <= mx):
        return f"مدت باید بین ۱ تا {mx} {'ساعت' if unit == 'hours' else 'روز'} باشد"
    e["duration_unit"] = unit
    e["duration_value"] = v
    e["days"] = (v / 24) if unit == "hours" else v
    return None


def _free_tests():
    out = []
    for e in db.get_free_test_panels():
        m = db.get_panel_plan_map_with_panel("free_test", int(e.get("num") or 0))
        out.append({"id": e.get("id"), "num": int(e.get("num") or 0), "name": e.get("name"), "volume_gb": e.get("volume_gb"),
                    "days": e.get("days"), "duration_unit": "hours" if e.get("duration_unit") == "hours" else "days",
                    "duration_value": e.get("duration_value") if e.get("duration_unit") == "hours" else e.get("days"), "price": int(e.get("price") or 0), "enabled": bool(e.get("enabled", True)),
                    "mapped": bool(m), "panel": (m or {}).get("panel_name") or "", "remote": (m or {}).get("remote_name") or ""})
    return out


def register(app, api):
    H = make_helpers(api)
    P = H.admin_perm

    # ------------------------------------------------------------ panels CRUD
    @app.route("/api/admin/panels/manage", methods=["GET"])
    @P("vpn_panel")
    def adm_pn_list():
        return H.ok(items=[_panel_view(p) for p in db.list_vpn_panels()], types=list(_PANEL_TYPES))

    @app.route("/api/admin/panels/manage", methods=["POST"])
    @P("vpn_panel")
    def adm_pn_save():
        b = request.get_json(silent=True) or {}
        pid = _i(b.get("id"))
        name = str(b.get("name") or "").strip()[:60]
        url = str(b.get("base_url") or "").strip().rstrip("/")
        if not name or not url.startswith(("http://", "https://")):
            return H.fail("invalid", "نام و آدرس معتبر (http/https) لازم است")
        if pid:
            if not db.get_vpn_panel(pid):
                return H.fail("not_found", "پنل پیدا نشد", 404)
            db.update_vpn_panel(pid, name=name, base_url=url, username=(b.get("username") or None),
                                password=(b.get("password") or None), api_key=(b.get("api_key") or None))
        else:
            t = b.get("type")
            if t not in _PANEL_TYPES:
                return H.fail("invalid", "نوع پنل نامعتبر است")
            pid = db.create_vpn_panel(t, name, url, api_key=(b.get("api_key") or None), username=(b.get("username") or None),
                                      password=(b.get("password") or None), auth_method=b.get("auth_method") or "userpass")
        H.audit("panel_save", str(pid), name)
        return adm_pn_list()

    @app.route("/api/admin/panels/manage/<int:pid>/delete", methods=["POST"])
    @P("vpn_panel")
    def adm_pn_delete(pid):
        if not db.get_vpn_panel(pid):
            return H.fail("not_found", "پنل پیدا نشد", 404)
        db.delete_vpn_panel(pid)
        H.audit("panel_delete", str(pid), "")
        return adm_pn_list()

    @app.route("/api/admin/panels/manage/<int:pid>/test", methods=["POST"])
    @P("vpn_panel")
    def adm_pn_test(pid):
        p = db.get_vpn_panel(pid)
        if not p:
            return H.fail("not_found", "پنل پیدا نشد", 404)
        try:
            r = _run(panels.test_connection(p), 20)
            ok, msg = bool(r[0]), (r[2] if len(r) > 2 else "")
        except Exception as exc:
            ok, msg = False, str(exc)
        return H.ok(connected=ok, message=str(msg or ("اتصال برقرار است" if ok else "اتصال ناموفق"))[:300])

    # ------------------------------------------------------------ mapping
    @app.route("/api/admin/mapping/catalog", methods=["GET"])
    @P("vpn_panel")
    def adm_map_catalog():
        p = db.get_vpn_panel(_i(request.args.get("panel_id")))
        if not p:
            return H.fail("not_found", "پنل پیدا نشد", 404)
        mode = request.args.get("mode") or "template"
        try:
            choices, msg = _run(panels.get_direct_catalog(p) if mode == "direct" else panels.get_catalog(p))
        except Exception as exc:
            return H.fail("panel_error", f"خطا در دریافت لیست پنل: {exc}", 502)
        if not choices:
            return H.fail("empty", msg or "لیستی دریافت نشد", 502)
        items = [{"idx": c["idx"], "label": c.get("label") or c.get("name"), "protocol": c.get("protocol")} for c in choices]
        return H.ok(items=items, supports_direct=p.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"))

    @app.route("/api/admin/mapping", methods=["GET"])
    @P("vpn_panel")
    def adm_map_get():
        scope = request.args.get("scope")
        if scope not in _SCOPES:
            return H.fail("invalid", "scope نامعتبر")
        m = db.get_panel_plan_map_with_panel(scope, _i(request.args.get("scope_id")))
        return H.ok(mapped=bool(m), panel_id=(m or {}).get("panel_id"), panel=(m or {}).get("panel_name") or "", remote=(m or {}).get("remote_name") or "")

    @app.route("/api/admin/mapping", methods=["POST"])
    @P("vpn_panel")
    def adm_map_set():
        b = request.get_json(silent=True) or {}
        scope, sid = b.get("scope"), _i(b.get("scope_id"))
        if scope not in _SCOPES:
            return H.fail("invalid", "scope نامعتبر")
        if b.get("delete"):
            db.delete_panel_plan_map(scope, sid)
            H.audit("map_delete", f"{scope}:{sid}", "")
            return H.ok()
        p = db.get_vpn_panel(_i(b.get("panel_id")))
        if not p:
            return H.fail("not_found", "پنل پیدا نشد", 404)
        mode = b.get("mode") or "template"
        idxs = [_i(x, -1) for x in (b.get("idx") if isinstance(b.get("idx"), list) else [b.get("idx")])]
        try:
            choices, msg = _run(panels.get_direct_catalog(p) if mode == "direct" else panels.get_catalog(p))
        except Exception as exc:
            return H.fail("panel_error", f"خطا در دریافت لیست پنل: {exc}", 502)
        chosen = [c for c in choices if c["idx"] in idxs]
        if not chosen:
            return H.fail("invalid", "موردی انتخاب نشده")
        if mode == "direct":
            if p.get("panel_type") in ("pasargad", "pasargad_wg"):
                payload, summary = {"group_ids": [c["raw_id"] for c in chosen]}, f"بدون تمپلیت ({len(chosen)} گروه)"
            else:
                inb: dict = {}
                for c in chosen:
                    inb.setdefault(c["protocol"], []).append(c["raw_id"])
                payload, summary = {"inbounds": inb}, f"بدون تمپلیت ({len(chosen)} اینباند)"
            db.set_panel_plan_map(scope, sid, p["id"], "direct:" + json.dumps(payload, ensure_ascii=False), summary)
        else:
            c = chosen[0]
            db.set_panel_plan_map(scope, sid, p["id"], c["ref"], c["name"])
        if scope == "vip_category":
            try:
                db.clear_panel_plan_overrides_for_category(sid)
            except Exception:
                pass
        H.audit("map_set", f"{scope}:{sid}", f"panel {p['id']}")
        return H.ok()

    # ------------------------------------------------------------ free-test entries
    @app.route("/api/admin/free-tests", methods=["GET"])
    @P("settings")
    def adm_ft_list():
        return H.ok(items=_free_tests(), shortcut=db.get_setting("free_test_shortcut", "0") == "1")

    @app.route("/api/admin/free-tests", methods=["POST"])
    @P("settings")
    def adm_ft_save():
        b = request.get_json(silent=True) or {}
        act = b.get("action")
        if act == "shortcut":
            db.set_setting("free_test_shortcut", "1" if b.get("value") else "0")
        elif act == "add":
            name = str(b.get("name") or "").strip()[:60]
            if not name:
                return H.fail("invalid", "نام را وارد کنید")
            ent = {"name": name, "volume_gb": max(0.0, _f(b.get("volume_gb"), 0.5)), "price": max(0, _i(b.get("price"))), "enabled": True}
            err = _set_duration(ent, b.get("duration_unit"), b.get("duration_value", b.get("days")))
            if err:
                return H.fail("invalid", err)
            db.add_free_test_panel(ent)
        else:
            pid = str(b.get("id") or "")
            if act == "toggle":
                db.toggle_free_test_panel(pid)
            elif act == "remove":
                e = next((x for x in db.get_free_test_panels() if x.get("id") == pid), None)
                if e:
                    db.delete_panel_plan_map("free_test", int(e.get("num") or 0))
                db.remove_free_test_panel(pid)
            elif act == "edit":
                items = db.get_free_test_panels()
                for e in items:
                    if e.get("id") == pid:
                        if b.get("name"): e["name"] = str(b["name"]).strip()[:60]
                        if "volume_gb" in b: e["volume_gb"] = max(0.0, _f(b["volume_gb"]))
                        if "duration_value" in b or "days" in b:
                            err = _set_duration(e, b.get("duration_unit") or e.get("duration_unit"), b.get("duration_value", b.get("days")))
                            if err:
                                return H.fail("invalid", err)
                        if "price" in b: e["price"] = max(0, _i(b["price"]))
                db.save_free_test_panels(items)
            else:
                return H.fail("invalid", "عملیات نامعتبر")
        H.audit("free_test", act, "")
        return H.ok(items=_free_tests(), shortcut=db.get_setting("free_test_shortcut", "0") == "1")

    # ------------------------------------------------------------ renewal settings (per category)
    @app.route("/api/admin/renewal", methods=["GET"])
    @P("plans")
    def adm_rn_get():
        _cs = dict(bot_info.get_renewal_settings(bot_info.CUSTOM_RENEWAL_ID)); _cs["price_day"] = int(round(float(_cs.get("price_day") or 0)))
        out = [{"id": bot_info.CUSTOM_RENEWAL_ID, "name": "🛠 سرویس سفارشی", **_cs},
               {"id": 0, "name": "🌐 پیش‌فرض (بدون دسته/همه)", **bot_info.get_renewal_settings(0)}]
        out[0]["name"] = "🛠 سرویس سفارشی (پیش‌فرض همه‌ی گزینه‌ها)"
        for o in db.get_map_options("custom_build") or []:
            _oc = dict(bot_info.get_renewal_settings(bot_info.custom_option_id(o["num"]))); _oc["price_day"] = int(round(float(_oc.get("price_day") or 0)))
            out.insert(1, {"id": bot_info.custom_option_id(o["num"]), "name": f"🛠 سفارشی: {o.get('name') or o['num']}", **_oc})
        for c in db.get_vip_categories():
            out.append({"id": c["id"], "name": c["name"], **bot_info.get_renewal_settings(c["id"])})
        return H.ok(items=out, enabled=db.is_renewal_enabled())

    @app.route("/api/admin/renewal", methods=["POST"])
    @P("plans")
    def adm_rn_set():
        b = request.get_json(silent=True) or {}
        if "enabled" in b and "category_id" not in b:
            db.set_renewal_enabled(bool(b["enabled"]))
            H.audit("renewal_switch", "", "on" if b["enabled"] else "off")
            return adm_rn_get()
        cid = _i(b.get("category_id"))
        if cid not in (0, bot_info.CUSTOM_RENEWAL_ID) and not (cid <= bot_info.CUSTOM_OPTION_BASE and any(int(o['num']) == bot_info.custom_option_from_id(cid) for o in (db.get_map_options('custom_build') or []))) and not db.get_vip_category(cid):
            return H.fail("not_found", "دسته پیدا نشد", 404)
        try:
            for k, v in (b.get("values") or {}).items():
                bot_info.set_renewal_setting(cid, k, v)
        except Exception as exc:
            return H.fail("invalid", str(exc))
        return adm_rn_get()

    # ------------------------------------------------------------ bot info
    def _bi():
        return H.ok(fields=[{"key": k, "label": lbl, "value": bot_info.get(k)} for k, lbl in bot_info.labels().items()],
                    channels=bot_info.get_required_channels(),
                    start_reaction=bot_info.get_start_reaction(), quick_reactions=bot_info.QUICK_REACTIONS,
                    announcement=db.get_setting("miniapp_announcement", "") or "")

    @app.route("/api/admin/botinfo", methods=["GET"])
    @P("botinfo")
    def adm_bi_get():
        return _bi()

    @app.route("/api/admin/botinfo", methods=["POST"])
    @P("botinfo")
    def adm_bi_set():
        b = request.get_json(silent=True) or {}
        act = b.get("action") or "field"
        if act == "field":
            k = b.get("key")
            if k not in bot_info.labels():
                return H.fail("invalid", "فیلد نامعتبر")
            v = str(b.get("value") or "").strip()
            if k == "config_name_prefix" and not __import__("re").fullmatch(r"[A-Za-z0-9_]{1,20}", v):
                return H.fail("invalid", "پیشوند فقط حروف/عدد انگلیسی و _ باشد")
            bot_info.set(k, v)
        elif act == "start_reaction":
            kind, val = b.get("type"), str(b.get("value") or "").strip()
            if not val or kind in (None, "", "none"):
                bot_info.set_start_reaction(None)
            elif kind == "emoji" and len(val) <= 8 and not any(ch.isalnum() and ch.isascii() for ch in val):
                bot_info.set_start_reaction("emoji", val)
            elif kind == "custom" and val.isdigit():
                bot_info.set_start_reaction("custom", val)
            else:
                return H.fail("invalid", "ایموجی نامعتبر است")
        elif act == "announcement":
            db.set_setting("miniapp_announcement", str(b.get("text") or "").strip()[:300])
        elif act == "add_channel":
            cid = str(b.get("channel_id") or "").strip()
            if not cid:
                return H.fail("invalid", "آیدی کانال لازم است")
            bot_info.add_required_channel(cid, str(b.get("name") or cid)[:60], str(b.get("url") or "").strip())
        elif act == "remove_channel":
            bot_info.remove_required_channel(b.get("channel_id"))
        else:
            return H.fail("invalid", "عملیات نامعتبر")
        H.audit("botinfo", act, "")
        return _bi()
