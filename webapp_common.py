"""Shared helpers for the extended mini-app API modules
(webapp_admin_a.py, webapp_admin_b.py, webapp_reseller.py).

Each module exposes ``register(app, api)`` where ``api`` is the loaded
``webapp_api`` module (so decorators/helpers defined there can be reused
without circular imports).  Use ``make_helpers(api)`` to get:

  admin_perm(key)   decorator: Telegram-auth + main admin OR sub-admin holding
                    permission ``key`` (see permissions.REGISTRY keys). key=None
                    means "any admin".
  reseller_required decorator: Telegram-auth + agent with percent > 0
                    (sets g.agent).
  ok(**data) / fail(code, message, status)  JSON helpers.
  audit(action, target, details)   writes admin activity log for current admin.
  page_args(default_per=20, max_per=100) -> (page, per, offset)
  is_main_admin()  True if current user is the main ADMIN_ID.
"""
from __future__ import annotations

import logging
from functools import wraps
from types import SimpleNamespace

from flask import g, jsonify, request

import database as db

log = logging.getLogger(__name__)

# permission keys a sub-admin may hold (mirrors permissions.REGISTRY)
ADMIN_SECTIONS = (
    "stats", "requests", "receipts", "users", "broadcast", "discounts", "agency",
    "plans", "vpn_panel", "referrals", "botinfo", "orders_toggle", "settings",
    "logs", "health", "tickets", "guides",
)


_TX_IN = {"charge", "referral_release", "gift", "refund", "deposit", "topup", "reward"}
_TX_PENDING = {"referral_pending", "referral_locked"}


def tx_direction(tx: dict) -> str:
    """جهت واقعی تراکنش کیف پول: in (واریز/سبز) | out (خرج/قرمز، حتی اگر مبلغ ۰ باشد) | pending (در انتظار)."""
    t = str(tx.get("type") or "")
    try:
        amt = int(tx.get("amount") or 0)
    except Exception:
        amt = 0
    if t in _TX_IN:
        return "in"
    if t in _TX_PENDING:
        return "pending"
    if t == "admin_adjust":
        return "in" if amt >= 0 else "out"
    return "out"


def tx_view(tx: dict) -> dict:
    """نمای یکسان تراکنش برای همه‌ی صفحات مینی‌اپ: amount علامت‌دار + direction."""
    try:
        raw = int(tx.get("amount") or 0)
    except Exception:
        raw = 0
    d = tx_direction(tx)
    mag = abs(raw)
    signed = mag if d == "in" else (-mag if d == "out" else mag)
    return {"type": tx.get("type"), "amount": signed, "direction": d,
            "description": tx.get("description"), "created_at": tx.get("created_at"), "status": tx.get("status")}


def make_helpers(api) -> SimpleNamespace:
    ADMIN_ID = api.ADMIN_ID

    def is_main_admin() -> bool:
        return str(g.tg_user.get("id")) == str(ADMIN_ID)

    def allowed_sections() -> list[str]:
        uid = g.tg_user.get("id")
        if str(uid) == str(ADMIN_ID):
            return list(ADMIN_SECTIONS)
        adm = db.get_sub_admin(str(uid))
        if not adm:
            return []
        perms = adm.get("permissions") or []
        return [p for p in ADMIN_SECTIONS if p in perms]

    def admin_perm(key: str | None = None):
        def deco(f):
            @wraps(f)
            @api.require_telegram_auth
            def wrapper(*a, **kw):
                uid = g.tg_user.get("id")
                if str(uid) != str(ADMIN_ID):
                    if not db.is_sub_admin(uid):
                        return jsonify({"error": "forbidden"}), 403
                    if key and not db.sub_admin_has_permission(str(uid), key):
                        return jsonify({"error": "forbidden", "message": "دسترسی این بخش را ندارید.", "permission": key}), 403
                return f(*a, **kw)
            return wrapper
        return deco

    def reseller_required(f):
        @wraps(f)
        @api.require_telegram_auth
        def wrapper(*a, **kw):
            agent = db.get_agent(g.tg_user.get("id"))
            if not agent or int(agent.get("vip_discount_percent") or 0) <= 0:
                return jsonify({"error": "forbidden"}), 403
            g.agent = agent
            return f(*a, **kw)
        return wrapper

    def ok(**data):
        out = {"success": True}
        out.update(data)
        return jsonify(out)

    def fail(code: str, message: str = "", status: int = 400):
        return jsonify({"success": False, "error": code, "message": message}), status

    def audit(action: str, target: str = "", details: str = ""):
        try:
            u = g.tg_user
            name = (u.get("first_name") or "") + (" " + u["last_name"] if u.get("last_name") else "")
            db.log_admin_action(str(u.get("id")), name.strip() or str(u.get("id")), action, str(target), str(details)[:500])
        except Exception:
            log.exception("audit failed")

    def page_args(default_per: int = 20, max_per: int = 100):
        try:
            page = max(1, int(request.args.get("page", 1)))
        except Exception:
            page = 1
        try:
            per = min(max_per, max(1, int(request.args.get("per", default_per))))
        except Exception:
            per = default_per
        return page, per, (page - 1) * per

    return SimpleNamespace(
        admin_perm=admin_perm, reseller_required=reseller_required, ok=ok, fail=fail,
        audit=audit, page_args=page_args, is_main_admin=is_main_admin,
        allowed_sections=allowed_sections, ADMIN_SECTIONS=ADMIN_SECTIONS,
    )
