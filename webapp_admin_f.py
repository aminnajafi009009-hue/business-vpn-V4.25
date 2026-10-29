"""Admin mini-app API, part F: request queue (orders waiting for delivery), config search,
manual service creation (parity with the bot's «📥 صف درخواست‌ها» / «🔍 جستجوی کانفیگ» / «🛠 ایجاد سرویس»)."""
from __future__ import annotations

import asyncio
import loopsafe
import json
import logging
import re

from flask import request

import database as db
import panels
from webapp_common import make_helpers

log = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _run(coro, timeout=25):
    return loopsafe.run(asyncio.wait_for(coro, timeout))


def register(app, api):
    H = make_helpers(api)
    P = H.admin_perm

    # ------------------------------------------------------------ request queue
    def _queue():
        items = []
        for o in db.get_pending_orders(limit=100):
            u = db.get_user_by_id(o["user_id"]) or {}
            plan = db.get_effective_plan(o["plan_key"]) if o.get("plan_key") else None
            items.append(dict(
                kind="order", id=o["id"], telegram_id=u.get("telegram_id"), user_name=u.get("name"),
                title=o.get("plan_name") or "-", order_type=o.get("order_type"), price=int(o.get("price") or 0),
                payment_method=o.get("payment_method"), created_at=o.get("created_at"),
                volume_gb=(plan or {}).get("volume_gb"), days=(plan or {}).get("days"), service_name=o.get("service_name"),
            ))
        for o in db.get_paid_custom_orders(limit=100):
            u = db.get_user_by_id(o["user_id"]) or {}
            items.append(dict(
                kind="custom", id=o["id"], telegram_id=u.get("telegram_id"), user_name=u.get("name"),
                title=("تمدید سرویس" if o.get("order_type") == "renew" else "سرویس سفارشی"), order_type=o.get("order_type"),
                price=int(o.get("price") or 0), payment_method=o.get("payment_method"), created_at=o.get("created_at"),
                volume_gb=o.get("volume_gb"), days=o.get("days"), service_name=o.get("custom_name"),
            ))
        items.sort(key=lambda x: str(x.get("created_at") or ""))
        return items

    @app.route("/api/admin/queue", methods=["GET"])
    @P("requests")
    def adm_queue():
        items = _queue()
        return H.ok(items=items, total=len(items), receipts=len(db.get_pending_receipts(limit=200)) + len(db.get_pending_custom_order_receipts(limit=200)))

    def _load(kind, oid):
        if kind == "order":
            o = db.get_order(oid)
            if not o or (o.get("status") or "pending") != "pending":
                return None
            return o
        if kind == "custom":
            o = db.get_custom_order(oid)
            if not o or o.get("status") != "paid":
                return None
            return o
        return None

    @app.route("/api/admin/queue/<kind>/<int:oid>/panel", methods=["POST"])
    @P("requests")
    def adm_queue_panel(kind, oid):
        """«📤 ارسال از پنل» — همان auto_fulfill_* ربات."""
        from handlers.panel_admin import auto_fulfill_vip_via_panel, auto_fulfill_custom_via_panel
        o = _load(kind, oid)
        if not o:
            return H.fail("not_found", "این سفارش دیگر در صف نیست", 404)
        u = db.get_user_by_id(int(o["user_id"]))
        if not u:
            return H.fail("not_found", "کاربر پیدا نشد", 404)
        if kind == "order":
            if not o.get("plan_key"):
                return H.fail("unsupported", "این سفارش پلن مشخصی ندارد؛ از ربات (ارسال دستی) انجام دهید")
            ok = api._run_async_with_temp_bot(lambda b: auto_fulfill_vip_via_panel(b, str(u["telegram_id"]), o["plan_key"], int(o["id"])))
        else:
            ok = api._run_async_with_temp_bot(lambda b: auto_fulfill_custom_via_panel(b, u, int(o["id"]), o["volume_gb"], o["days"], o.get("custom_name")))
        if not ok:
            return H.fail("not_mapped", "ارسال خودکار انجام نشد (نگاشت پنل برای این پلن/گزینه تنظیم نیست یا پنل خطا داد). از «ارسال دستی در ربات» استفاده کنید.", 409)
        H.audit("queue_panel_send", str(u["telegram_id"]), f"{kind}:{oid}")
        return H.ok()

    @app.route("/api/admin/queue/<kind>/<int:oid>/manual", methods=["POST"])
    @P("requests")
    def adm_queue_manual(kind, oid):
        """ارسال دستی (لینک ساب) نیاز به گفتگوی چندمرحله‌ای دارد؛ همان دکمه‌ی ربات برای ادمین فرستاده می‌شود."""
        o = _load(kind, oid)
        if not o:
            return H.fail("not_found", "این سفارش دیگر در صف نیست", 404)
        u = db.get_user_by_id(int(o["user_id"])) or {}
        tid = u.get("telegram_id")
        if kind == "order":
            btn = ("🚀 شروع ارسال کانفیگ", f"sendvip_{tid}|{oid}")
            txt = f"📦 سفارش #{oid} — {o.get('plan_name') or '-'}\n🆔 {tid}\n\nبرای ارسال دستی روی دکمه بزنید 👇"
        else:
            btn = ("🚀 شروع ارسال کانفیگ", f"sendcustomorder_{oid}")
            txt = f"🛠 سفارش سفارشی #{oid}\n🆔 {tid}\n📦 {o.get('volume_gb')}GB / {o.get('days')} روز\n\nبرای ارسال دستی روی دکمه بزنید 👇"
        aid = g_actor_id()
        api.tg_send_message(aid, txt, reply_markup=api.inline_kb([btn]))
        return H.ok()

    def g_actor_id():
        from flask import g
        return int(g.tg_user.get("id"))

    @app.route("/api/admin/queue/<kind>/<int:oid>/dismiss", methods=["POST"])
    @P("requests")
    def adm_queue_dismiss(kind, oid):
        if not _load(kind, oid):
            return H.fail("not_found", "این سفارش دیگر در صف نیست", 404)
        if kind == "order":
            db.set_order_status(oid, "dismissed")
        else:
            db.set_custom_order_status(oid, "dismissed")
        H.audit("queue_dismiss", f"{kind}:{oid}", "")
        return H.ok()

    @app.route("/api/admin/queue/clear", methods=["POST"])
    @P("requests")
    def adm_queue_clear():
        n = 0
        for o in db.get_pending_orders(limit=1000):
            db.set_order_status(o["id"], "dismissed"); n += 1
        for o in db.get_paid_custom_orders(limit=1000):
            db.set_custom_order_status(o["id"], "dismissed"); n += 1
        H.audit("queue_clear", str(n), "")
        return H.ok(cleared=n)

    # ------------------------------------------------------------ config search
    @app.route("/api/admin/configs/search", methods=["GET"])
    @P("users")
    def adm_config_search():
        q = (request.args.get("q") or "").strip()
        if len(q) < 1:
            return H.ok(items=[])
        tokens = [t.casefold() for t in q.split() if t.strip()] or [q.casefold()]
        out = []
        try:
            cfgs = db.get_all_configs_for_admin_search(include_deleted=True)
        except Exception:
            cfgs = []
        for c in cfgs:
            hay = " ".join(str(x or "") for x in (c.get("service_id"), c.get("plan"), c.get("product_name"), c.get("owner_name"), c.get("owner_tid"))).casefold()
            if all(t in hay for t in tokens):
                out.append(dict(id=c["id"], name=c.get("service_id") or c.get("plan"), plan=c.get("plan"), type=c.get("type"),
                                expiry=c.get("expiry"), deleted=bool(c.get("deleted")), disabled=bool(c.get("sub_link_disabled")),
                                owner_name=c.get("owner_name"), owner_tid=c.get("owner_tid")))
            if len(out) >= 40:
                break
        return H.ok(items=out)

    # ------------------------------------------------------------ manual service creation
    @app.route("/api/admin/manual-service/panels", methods=["GET"])
    @P("vpn_panel")
    def adm_ms_panels():
        items = [dict(id=p["id"], label=panels.panel_label(p), type=p.get("panel_type"), supports_direct=p.get("panel_type") in ("marzban", "pasargad", "pasargad_wg"))
                 for p in db.list_vpn_panels(enabled_only=True)]
        return H.ok(items=items)

    @app.route("/api/admin/manual-service/catalog", methods=["GET"])
    @P("vpn_panel")
    def adm_ms_catalog():
        p = db.get_vpn_panel(_i(request.args.get("panel_id")))
        if not p or not p.get("enabled"):
            return H.fail("not_found", "پنل پیدا نشد", 404)
        mode = request.args.get("mode") or "template"
        try:
            choices, msg = _run(panels.get_direct_catalog(p) if mode == "direct" else panels.get_catalog(p))
        except Exception as exc:
            return H.fail("panel_error", f"خطا در دریافت لیست پنل: {exc}", 502)
        if not choices:
            return H.fail("empty", msg or "لیستی دریافت نشد", 502)
        return H.ok(items=[{"idx": c["idx"], "label": c.get("label") or c.get("name"), "protocol": c.get("protocol")} for c in choices])

    @app.route("/api/admin/manual-service", methods=["POST"])
    @P("vpn_panel")
    def adm_ms_create():
        from handlers.panel_admin import _deliver_panel_link, _service_username
        b = request.get_json(silent=True) or {}
        p = db.get_vpn_panel(_i(b.get("panel_id")))
        if not p or not p.get("enabled"):
            return H.fail("not_found", "پنل پیدا نشد", 404)
        volume, days = _i(b.get("volume_gb")), _i(b.get("days"))
        if not (1 <= volume <= 1_000_000) or not (1 <= days <= 1_000_000):
            return H.fail("invalid", "حجم و مدت باید عدد صحیح مثبت باشند")
        name = (b.get("name") or "").strip() or None
        if name and not _NAME_RE.match(name):
            return H.fail("invalid_name", "نام فقط حروف انگلیسی و عدد، بدون فاصله (حداکثر ۳۲ کاراکتر)")
        from flask import g
        target = str(b.get("target_tid") or "").strip() or str(g.tg_user.get("id"))
        if db.get_user(target) is None:
            return H.fail("user_not_found", "کاربر هدف پیدا نشد (باید قبلاً ربات را استارت کرده باشد)", 404)
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
                payload = {"group_ids": [c["raw_id"] for c in chosen]}
            else:
                inb: dict = {}
                for c in chosen:
                    inb.setdefault(c["protocol"], []).append(c["raw_id"])
                payload = {"inbounds": inb}
            remote_ref = "direct:" + json.dumps(payload, ensure_ascii=False)
        else:
            remote_ref = chosen[0]["ref"]
        username = name or _service_username(target)

        async def go(bot):
            ok, link, remote_id, resp, msg2 = await panels.create_service(p, username, remote_ref, volume_gb=volume, days=days)
            if not ok:
                return {"ok": False, "message": str(msg2 or "ساخت سرویس ناموفق بود")[:300]}
            if not link:
                return {"ok": False, "message": "سرویس روی پنل ساخته شد ولی لینک ساب خودکار پیدا نشد؛ از داخل پنل بردارید.", "created": True}
            ctx = {"uid": int(target), "plan_key": None, "order_id": None, "order_kind": "admin_manual", "panel_id": p["id"],
                   "panel_type": p["panel_type"], "service_id": remote_id, "snapshot": {"name": name or username, "volume_gb": volume, "days": days}}
            await _deliver_panel_link(bot, ctx, link)
            return {"ok": True}

        res = api._run_async_with_temp_bot(go)
        if not res:
            return H.fail("panel_error", "ساخت سرویس ناموفق بود", 502)
        if not res.get("ok"):
            return H.fail("create_failed", res.get("message") or "ناموفق", 502)
        H.audit("manual_service", target, f"panel {p['id']} {volume}GB/{days}d {username}")
        return H.ok(username=username)
