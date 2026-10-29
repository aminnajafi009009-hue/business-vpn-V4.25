"""Admin mini-app API, part C: custom-build / PAYG options (per-option price+limits), agent extras."""
from __future__ import annotations

from flask import request

import database as db
from webapp_common import make_helpers

_KINDS = ("custom_build", "payg")


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _payload():
    s = db.get_payg_settings()
    out = {"payg": {"enabled": bool(s["enabled"]), "min_balance": _i(s["min_balance"]),
                    "empty_action": s.get("empty_action") or "suspend", "empty_message": s.get("empty_message") or ""},
           "custom_build": {"global": db.get_effective_custom_build_settings(None),
                            "enabled": db.get_setting("custom_build_enabled", "1") != "0"}}
    for kind in _KINDS:
        opts = []
        for o in db.get_map_options(kind):
            cfg = db.get_map_option_cfg(kind, o["num"])
            m = db.get_option_mapping(kind, o["num"])
            eff = db.get_effective_custom_build_settings(o["num"]) if kind == "custom_build" else {
                "price_per_gb": db.resolve_payg_price_per_gb(None, None, option_num=o["num"])}
            opts.append({"num": o["num"], "name": o["name"], "mapped": bool(o.get("mapped")),
                         "panel": (m or {}).get("panel_name") or "", "remote": (m or {}).get("remote_name") or "",
                         "cfg": cfg, "effective": eff})
        out[kind]["options"] = opts
    return out


def register(app, api):
    H = make_helpers(api)
    P = H.admin_perm

    @app.route("/api/admin/svc-options", methods=["GET"])
    @P("plans")
    def adm_svc_opts():
        return H.ok(**_payload())

    @app.route("/api/admin/svc-options", methods=["POST"])
    @P("plans")
    def adm_svc_opts_set():
        b = request.get_json(silent=True) or {}
        act = b.get("action")
        kind = b.get("kind")
        if act not in ("payg_settings", "custom_global", "custom_enabled") and kind not in _KINDS:
            return H.fail("invalid", "نوع نامعتبر است")
        try:
            if act == "add":
                name = str(b.get("name") or "").strip()
                if not name:
                    return H.fail("invalid", "نام گزینه را وارد کنید")
                db.add_map_option(kind, name)
            elif act == "rename":
                name = str(b.get("name") or "").strip()
                if not name:
                    return H.fail("invalid", "نام خالی است")
                db.rename_map_option(kind, _i(b.get("num")), name)
            elif act == "remove":
                db.remove_map_option(kind, _i(b.get("num")))
            elif act == "cfg":
                vals = b.get("values") or {}
                if b.get("clear"):
                    db.clear_map_option_cfg(kind, _i(b.get("num")))
                else:
                    db.set_map_option_cfg(kind, _i(b.get("num")), vals)
            elif act == "payg_settings":
                v = b.get("values") or {}
                kw = {}
                if "enabled" in v:
                    kw["enabled"] = bool(v["enabled"])
                if "min_balance" in v:
                    kw["min_balance"] = max(0, _i(v["min_balance"]))
                if v.get("empty_action") in ("suspend", "delete"):
                    kw["empty_action"] = v["empty_action"]
                if "empty_message" in v:
                    kw["empty_message"] = str(v["empty_message"])[:500]
                db.set_payg_settings(**kw)
            elif act == "custom_enabled":
                db.set_setting("custom_build_enabled", "1" if b.get("enabled") else "0")
            elif act == "custom_global":
                v = b.get("values") or {}
                cur = db.get_effective_custom_build_settings(None)
                cur.update({k: max(0, _i(v[k])) for k in cur if k in v})
                db.set_custom_build_override(cur["price_per_gb"], cur["price_per_30_days"], cur["min_gb"], cur["max_gb"], cur["min_days"], cur["max_days"])
            else:
                return H.fail("invalid", "عملیات نامعتبر")
        except Exception as exc:
            return H.fail("failed", f"ذخیره ناموفق بود: {exc}")
        H.audit("svc_options", f"{act}:{kind}", "")
        return H.ok(**_payload())

    # ---- agent extras: test quota + per-option discount
    @app.route("/api/admin/agents/<tid>/extras", methods=["GET"])
    @P("agency")
    def adm_agent_extras(tid):
        if not db.get_agent(tid):
            return H.fail("not_found", "نماینده یافت نشد", 404)
        opts = []
        for kind in _KINDS:
            for o in db.get_map_options(kind):
                opts.append({"kind": kind, "num": o["num"], "name": o["name"],
                             "override": db.get_agent_option_percent_override(tid, kind, o["num"]),
                             "effective": db.agent_percent_for_option(tid, kind, o["num"])})
        return H.ok(test_quota=db.agent_test_quota(tid), options=opts)

    @app.route("/api/admin/agents/<tid>/extras", methods=["POST"])
    @P("agency")
    def adm_agent_extras_set(tid):
        if not db.get_agent(tid):
            return H.fail("not_found", "نماینده یافت نشد", 404)
        b = request.get_json(silent=True) or {}
        if "test_quota" in b:
            db.set_agent_test_quota(tid, max(0, _i(b["test_quota"])))
        if b.get("kind") in _KINDS and "num" in b:
            pct = b.get("percent")
            db.set_agent_option_percent(tid, b["kind"], _i(b["num"]), None if pct in (None, "") else _i(pct))
        return adm_agent_extras(tid)
