"""Mini-app PAYG purchase (pay-as-you-go): options with their own price, create service from wallet rules.
Mirrors handlers/plans._start_payg_purchase + payg_name_input (same checks, same panel creation, same log)."""
from __future__ import annotations

import asyncio
import loopsafe
import logging
import re

from flask import g, jsonify, request

import database as db
import panels

log = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[A-Za-z0-9]+$")
_PAYG_TYPES = ("marzban", "pasargad", "pasargad_wg", "threexui")


def _options(tid=None):
    out = []
    for o in db.usable_map_options("payg", _PAYG_TYPES):
        out.append({"num": int(o["num"]), "name": o.get("name") or f"گزینه {o['num']}",
                    "price_per_gb": (db.payg_price_for_user(tid, int(o["num"]), db.resolve_payg_price_per_gb(None, None, option_num=int(o["num"]))) if tid else db.resolve_payg_price_per_gb(None, None, option_num=int(o["num"])))})
    return out


def register(app, api):
    auth = api.require_telegram_auth

    @app.route("/api/payg", methods=["GET"])
    @auth
    def api_payg_info():
        s = db.get_payg_settings()
        u = g.user
        return jsonify({
            "enabled": bool(s["enabled"]) and bool(_options()),
            "orders_enabled": db.is_orders_enabled(),
            "min_balance": int(s.get("min_balance") or 0),
            "wallet": int(u.get("wallet") or 0),
            "empty_action": s.get("empty_action") or "suspend",
            "options": _options(u["telegram_id"]),
        })

    @app.route("/api/payg/create", methods=["POST"])
    @auth
    @api.rate_limit(max_calls=5, period_seconds=60)
    def api_payg_create():
        from handlers import panel_admin
        body = request.get_json(silent=True) or {}
        u = g.user
        if not db.is_orders_enabled():
            return jsonify({"success": False, "error": "orders_disabled", "message": "ثبت سفارش موقتاً غیرفعال است."}), 403
        s = db.get_payg_settings()
        if not s["enabled"]:
            return jsonify({"success": False, "error": "disabled", "message": "سرویس PAYG در حال حاضر غیرفعال است."}), 400
        name = str(body.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            return jsonify({"success": False, "error": "invalid_name", "message": "نام فقط می‌تواند حروف انگلیسی و عدد (بدون فاصله) باشد."}), 400
        opts = _options()
        nums = [o["num"] for o in opts]
        raw = body.get("option_num")
        if raw in (None, ""):
            if len(nums) > 1:
                return jsonify({"success": False, "error": "invalid_option", "message": "یکی از پلن‌های PAYG را انتخاب کنید."}), 400
            num = nums[0] if nums else 0
        else:
            try:
                num = int(raw)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "invalid_option", "message": "گزینه نامعتبر است."}), 400
            if num not in nums:
                return jsonify({"success": False, "error": "invalid_option", "message": "این پلن PAYG در دسترس نیست."}), 400
        mapping = db.get_option_mapping("payg", num)
        panel = db.get_vpn_panel(mapping["panel_id"]) if mapping and mapping.get("panel_id") else None
        if not mapping or not panel or not panel.get("enabled") or panel.get("panel_type") not in _PAYG_TYPES:
            return jsonify({"success": False, "error": "panel_unavailable", "message": "پنل این پلن در حال حاضر در دسترس نیست."}), 503
        min_balance = int(s.get("min_balance") or 0)
        if int(u.get("wallet") or 0) < min_balance:
            return jsonify({"success": False, "error": "insufficient_balance", "min_balance": min_balance,
                            "needed": min_balance - int(u.get("wallet") or 0),
                            "message": f"برای ساخت PAYG حداقل {min_balance:,} تومان موجودی لازم است."}), 402
        if not db.claim_purchase_action(f"mini-payg:{u['id']}", ttl_seconds=15):
            return jsonify({"success": False, "error": "duplicate_request", "message": "درخواست قبلی در حال پردازش است."}), 409

        price = db.payg_price_for_user(u["telegram_id"], num, db.resolve_payg_price_per_gb(None, None, option_num=num))
        try:
            ok, link, remote_id, raw_data, err = loopsafe.run(panels.create_service(panel, name, mapping["remote_ref"], volume_gb=0, days=0))
        except Exception as exc:
            log.exception("payg create failed")
            ok, link, remote_id, raw_data, err = False, None, None, None, str(exc)
        if not ok or not link:
            return jsonify({"success": False, "error": "panel_error", "message": f"ساخت سرویس ناموفق بود: {err or 'خطای پنل'}"}), 502
        ctx = {
            "uid": str(u["telegram_id"]), "plan_key": None, "order_id": None, "order_kind": "payg",
            "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_id,
            "snapshot": {"name": name, "volume_gb": 0, "days": 0}, "config_type": "payg", "payg_price_per_gb": price,
        }
        api._run_async_with_temp_bot(lambda bot: panel_admin._deliver_panel_link(bot, ctx, link))
        cfg_id = None
        try:
            for c in db.get_configs(u["id"]):
                if str(c.get("service_id")) == str(remote_id):
                    cfg_id = c["id"]
                    break
        except Exception:
            pass
        api.invalidate_live_snapshot(None)
        return jsonify({"success": True, "config_id": cfg_id, "price_per_gb": price})
