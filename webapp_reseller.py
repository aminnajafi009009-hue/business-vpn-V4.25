"""Reseller (agent) mini-app API: /api/reseller/*."""
from __future__ import annotations

import re
from datetime import timedelta

from flask import g, request

import database as db
from webapp_common import make_helpers, tx_view

PAID = "('completed','active','paid','processing','fulfilled')"


def _rows(sql, params=()):
    cur = db.get_connection().cursor()
    cur.execute(sql, params)
    return db._fetchall(cur)


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def register(app, api):
    H = make_helpers(api)
    R = H.reseller_required

    def me():
        return db.get_user(str(g.tg_user["id"])) or {}

    @app.route("/api/reseller/overview", methods=["GET"])
    @R
    def rs_overview():
        u = me(); a = g.agent; uid = _i(u.get("id"))
        today = api.now_tehran_naive().date()
        since30 = (today - timedelta(days=29)).isoformat()
        since14 = (today - timedelta(days=13)).isoformat()
        pct = _i(a.get("vip_discount_percent"))
        tot = _rows(f"SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM orders WHERE user_id=? AND status IN {PAID}", (uid,))[0]
        m = _rows(f"SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM orders WHERE user_id=? AND status IN {PAID} AND substr(created_at,1,10)>=?", (uid, since30))[0]
        by_day = {r["d"]: _i(r["s"]) for r in _rows(f"SELECT substr(created_at,1,10) d, SUM(price) s FROM orders WHERE user_id=? AND status IN {PAID} AND substr(created_at,1,10)>=? GROUP BY d", (uid, since14))}
        series = [{"d": (today - timedelta(days=13 - i)).isoformat(), "value": by_day.get((today - timedelta(days=13 - i)).isoformat(), 0)} for i in range(14)]
        cust = _rows("SELECT COUNT(*) c, COALESCE(SUM(total_purchase),0) s FROM users WHERE referrer_id=?", (uid,))[0]
        services = _rows("SELECT COUNT(*) c FROM configs WHERE user_id=? AND deleted=0", (uid,))[0]["c"]
        saved = int(round(_i(tot["s"]) * pct / (100 - pct))) if 0 < pct < 100 else 0
        _tid = g.tg_user["id"]
        _q = db.agent_test_quota(_tid)
        _opts = []
        for _k, _t in (("custom_build", "سرویس سفارشی"), ("payg", "PAYG")):
            for _o in db.get_map_options(_k):
                _opts.append({"kind": _k, "kind_label": _t, "name": _o["name"], "percent": db.agent_percent_for_option(_tid, _k, _o["num"])})
        return H.ok(
            test_quota=_q, tests_used=(db.agent_tests_used_this_month(uid) if _q else 0), option_discounts=_opts,
            name=u.get("name"), percent=pct, prefix=a.get("custom_prefix") or "", wallet=_i(u.get("wallet")), locked_wallet=_i(u.get("locked_wallet")),
            orders_total=_i(tot["c"]), spent_total=_i(tot["s"]), orders_30d=_i(m["c"]), spent_30d=_i(m["s"]), estimated_saving=saved,
            services=_i(services), series=series,
            categories=[c["name"] for c in db.get_vip_categories() if _i(c["id"]) in set(db.get_agent_category_ids(g.tg_user["id"]))] or None,
        )

    @app.route("/api/reseller/sales", methods=["GET"])
    @R
    def rs_sales():
        page, per, off = H.page_args(20)
        uid = _i(me().get("id"))
        total = _rows("SELECT COUNT(*) c FROM orders WHERE user_id=?", (uid,))[0]["c"]
        rows = _rows("SELECT id, plan_name, order_type, price, status, created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (uid, per, off))
        return H.ok(items=rows, total=_i(total), page=page, per=per)

    @app.route("/api/reseller/plans", methods=["GET"])
    @R
    def rs_plans():
        tid = g.tg_user["id"]
        out = []
        for c in db.get_vip_categories():
            plans = []
            for p in db.get_vip_plans(c["id"]):
                pct = db.agent_percent_for_plan(tid, p["plan_key"], c["id"])
                price = _i(p.get("price"))
                plans.append(dict(key=p["plan_key"], name=p["name"], price=price, agent_price=int(round(price * (1 - pct / 100))), percent=pct,
                                  days=_i(p.get("days")), volume_gb=_i(p.get("volume_gb"))))
            if plans:
                out.append(dict(id=c["id"], name=c["name"], plans=plans))
        return H.ok(categories=out)

    @app.route("/api/reseller/prefix", methods=["POST"])
    @R
    def rs_prefix():
        raw = str((request.get_json(silent=True) or {}).get("prefix") or "")
        p = re.sub(r"[^A-Za-z0-9_]", "", raw)[:12]
        if raw and not p:
            return H.fail("invalid", "فقط حروف انگلیسی، عدد و _ مجاز است")
        db.set_agent_prefix(g.tg_user["id"], p)
        return H.ok(prefix=p)

    @app.route("/api/reseller/wallet", methods=["GET"])
    @R
    def rs_wallet():
        u = me()
        txs = db.get_transactions(_i(u.get("id")), limit=30)
        return H.ok(wallet=_i(u.get("wallet")), locked_wallet=_i(u.get("locked_wallet")),
                    transactions=[tx_view(t) for t in txs])

    # ------------------------------------------------------------------ services management
    import loopsafe
    import panels as _panels
    GIB = 1024 ** 3

    def own_cfg(cid):
        cfg = db.get_config_by_id(int(cid))
        if not cfg or cfg.get("deleted") or int(cfg["user_id"]) != _i(me().get("id")):
            return None, None
        panel = db.get_vpn_panel(cfg["panel_id"]) if cfg.get("panel_id") else None
        return cfg, panel

    def _opt(raw):
        opts = db.usable_map_options("custom_build")
        nums = [int(o["num"]) for o in opts]
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = None
        if n is not None and (n in nums or not nums):
            return n
        return nums[0] if nums else 0

    def _delta(b):
        try:
            gb = int(b.get("gb") or 0)
            days = int(b.get("days") or 0)
        except (TypeError, ValueError):
            return None, None, "مقدار نامعتبر است."
        if gb == 0 and days == 0:
            return None, None, "حجم یا زمان را وارد کنید."
        if (gb > 0 and days < 0) or (gb < 0 and days > 0):
            return None, None, "افزایش و کاهش را هم‌زمان انجام ندهید."
        return gb, days, None

    @app.route("/api/reseller/services/options", methods=["GET"])
    @R
    def rs_svc_options():
        tid = g.tg_user["id"]
        out = []
        for o in db.usable_map_options("custom_build") or [{"num": 0, "name": "پیش‌فرض"}]:
            st = db.get_effective_custom_build_settings(o["num"])
            out.append({"num": o["num"], "name": o["name"], "price_per_gb": st["price_per_gb"], "price_per_30_days": st["price_per_30_days"],
                        "percent": db.agent_percent_for_option(tid, "custom_build", o["num"]), "max_gb": st["max_gb"], "max_days": st["max_days"]})
        return H.ok(options=out)

    @app.route("/api/reseller/services/<int:cid>/quote", methods=["POST"])
    @R
    def rs_svc_quote(cid):
        cfg, panel = own_cfg(cid)
        if not cfg:
            return H.fail("not_found", "سرویس پیدا نشد", 404)
        b = request.get_json(silent=True) or {}
        gb, days, err = _delta(b)
        if err:
            return H.fail("invalid", err)
        price, _d = api._calc_custom_price(abs(gb), abs(days), g.tg_user["id"], option_num=_opt(b.get("option_num")))
        return H.ok(price=price, mode="charge" if (gb > 0 or days > 0) else "refund", wallet=_i(me().get("wallet")))

    @app.route("/api/reseller/services/<int:cid>/adjust", methods=["POST"])
    @R
    def rs_svc_adjust(cid):
        cfg, panel = own_cfg(cid)
        if not cfg:
            return H.fail("not_found", "سرویس پیدا نشد", 404)
        if not panel or not cfg.get("service_id") or panel.get("panel_type") not in ("marzban", "pasargad", "pasargad_wg", "threexui"):
            return H.fail("not_supported", "این سرویس از پنل قابل تغییر نیست.")
        if not panel.get("enabled"):
            return H.fail("panel_disabled", "پنل این سرویس غیرفعال است.")
        b = request.get_json(silent=True) or {}
        gb, days, err = _delta(b)
        if err:
            return H.fail("invalid", err)
        opt = _opt(b.get("option_num"))
        tid = g.tg_user["id"]
        u = me()
        if gb > 0 or days > 0:
            st = db.get_effective_custom_build_settings(opt)
            if gb > st["max_gb"] or days > st["max_days"]:
                return H.fail("invalid", "مقدار از سقف مجاز بیشتر است.")
            price, _d = api._calc_custom_price(gb, days, tid, option_num=opt)
            if not db.claim_purchase_action(f"rs-adjust:{u['id']}:{cid}", ttl_seconds=8):
                return H.fail("duplicate", "درخواست قبلی در حال پردازش است.", 409)
            if _i(u.get("wallet")) < price:
                return H.fail("insufficient_balance", f"موجودی کیف پول کافی نیست؛ {price - _i(u.get('wallet')):,} تومان کم دارید.", 402)
            ok_d, _rf = db.deduct_wallet_for_purchase(u["id"], price, "افزایش حجم/زمان سرویس (نماینده)")
            if not ok_d:
                return H.fail("insufficient_balance", "موجودی کافی نیست.", 402)
            mode = "time" if gb == 0 else ("volume" if days == 0 else "both")
            oid = db.create_custom_order(u["id"], gb, days, None, price, "renew", cfg["id"], renew_mode=mode, payment_method="کیف پول", option_num=opt)
            db.set_custom_order_status(oid, "paid")
            from handlers.panel_admin import auto_fulfill_custom_via_panel
            handled = api._run_async_with_temp_bot(lambda bot: auto_fulfill_custom_via_panel(bot, u, oid, gb, days, None))
            api.invalidate_live_snapshot(cfg)
            if not handled:
                db.add_to_wallet(u["id"], price, "بازگشت وجه: افزایش سرویس ناموفق بود", tx_type="refund")
                try: db.set_custom_order_status(oid, "refunded")
                except Exception: pass
                return H.fail("panel_error", "اعمال تغییر روی پنل ناموفق بود؛ مبلغ به کیف پول برگشت داده شد.", 502)
            H.audit("reseller_service_increase", cfg["id"], f"+{gb}GB +{days}d {price}")
            return H.ok(mode="charge", price=price, wallet=_i((db.get_user_by_id(u["id"]) or {}).get("wallet")))

        # ---- decrease → refund at the same price
        rgb, rdays = abs(gb), abs(days)
        okp, snap, _m = loopsafe.run(_panels.get_service_snapshot(panel, cfg["service_id"]))
        if not okp or not isinstance(snap, dict):
            return H.fail("panel_error", "دریافت وضعیت سرویس از پنل ناموفق بود.", 502)
        if rgb:
            total, used = int(snap.get("total") or 0), int(snap.get("used") or 0)
            if total <= 0:
                return H.fail("invalid", "این سرویس حجم نامحدود دارد؛ کاهش حجم ممکن نیست.")
            if rgb * GIB >= total or rgb * GIB > total - used:
                return H.fail("invalid", f"حداکثر حجم قابل کاهش {max(0, (total - used) // GIB)} گیگ است (بیشتر از حجم باقی‌مانده نمی‌شود).")
        done_gb = done_days = 0
        errs = []
        if rgb:
            ok1, m1 = loopsafe.run(_panels.reduce_service_quota(panel, cfg["service_id"], rgb))
            if ok1: done_gb = rgb
            else: errs.append(m1)
        if rdays:
            ok2, m2 = loopsafe.run(_panels.reduce_service_days(panel, cfg["service_id"], rdays))
            if ok2: done_days = rdays
            else: errs.append(m2)
        api.invalidate_live_snapshot(cfg)
        if not done_gb and not done_days:
            return H.fail("panel_error", errs[-1] if errs else "کاهش ناموفق بود.", 502)
        refund, _d = api._calc_custom_price(done_gb, done_days, tid, option_num=opt)
        if refund > 0:
            db.add_to_wallet(u["id"], refund, f"بازگشت وجه کاهش سرویس ({done_gb}GB / {done_days} روز)", tx_type="refund")
            try:
                with db.transaction() as cur:
                    cur.execute("UPDATE users SET total_purchase = MAX(0, total_purchase - ?) WHERE id = ?", (refund, u["id"]))
                db._invalidate_user_cache_by_id(u["id"])
            except Exception:
                pass
        try:
            api.tg_send_message(api.ADMIN_ID, f"➖ کاهش سرویس توسط نماینده\n👤 {u.get('name')} ({u.get('telegram_id')})\n🆔 سرویس: {cfg.get('service_id')}\n"
                                f"📉 {done_gb} گیگ / {done_days} روز\n💰 بازگشت به کیف پول: {refund:,} تومان")
        except Exception:
            pass
        H.audit("reseller_service_decrease", cfg["id"], f"-{done_gb}GB -{done_days}d refund {refund}")
        return H.ok(mode="refund", price=refund, partial=bool(errs), message=("؛ ".join(errs) if errs else ""),
                    wallet=_i((db.get_user_by_id(u["id"]) or {}).get("wallet")))

    @app.route("/api/reseller/services/<int:cid>/rename", methods=["POST"])
    @R
    def rs_svc_rename(cid):
        cfg, _p = own_cfg(cid)
        if not cfg:
            return H.fail("not_found", "سرویس پیدا نشد", 404)
        title = str((request.get_json(silent=True) or {}).get("title") or "").strip()[:60]
        db.set_config_title(cid, title or None)
        api.invalidate_live_snapshot(cfg)
        return H.ok(title=title)
