"""Admin mini-app API, part E: FrenzyEx/Tronado gateways, topup switches, config naming,
referral management, campaigns, guides (parity with the bot's admin panel)."""
from __future__ import annotations

import json
import logging
import loopsafe
import re

from flask import request

import bot_info
import database as db
import frenzyex as _frenzyex
import online_gateways as _og
import tronado as _tronado
import tonpays as _tonpays
from webapp_common import make_helpers

log = logging.getLogger(__name__)

_FIELDS = {
    "frenzyex": {"api_key": "secret", "callback_secret": "secret", "base_url": "url", "usd_rate_toman": "int0",
                 "min_amount": "int", "description": "text", "public_url": "url_opt"},
    "tronado": {"api_key": "secret", "ipn_signing_key": "secret", "wallet_address": "address", "wage_percent": "wage",
                "min_amount": "int", "base_url": "url", "public_url": "url_opt"},
    "tonpays": {"api_key": "secret", "custom_tg_key": "secret", "webhook_secret": "secret", "base_url": "url", "min_amount": "int", "public_url": "url_opt"},
}
_PM_ORDER = ["auto", "standard", "nowpay"]


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _mask(v):
    v = str(v or "")
    if not v:
        return ""
    return "••••" + v[-4:] if len(v) >= 8 else "••••"


def register(app, api):
    H = make_helpers(api)
    P = H.admin_perm

    def _pm():
        from webapp_admin_b import _pm as pm
        return pm()

    def _reload():
        from webapp_admin_b import _reload_bot_pm
        _reload_bot_pm()

    # ------------------------------------------------------------ gateways
    def _gw_payload():
        pm = _pm()
        f, t, tp = pm.frenzyex, pm.tronado, pm.tonpays
        sw = db.get_topup_switches()
        return dict(
            frenzyex=dict(enabled=f.enabled, api_key=_mask(f.api_key), callback_secret=_mask(f.callback_secret), base_url=f.base_url,
                          payment_method=f.payment_method, link_mode=f.link_mode, usd_rate_toman=_i(f.usd_rate_toman),
                          min_amount=_i(f.min_amount), description=f.description or "", public_url=getattr(f, "public_url", "") or "",
                          callback_url=_og.callback_url("frenzyex") or ""),
            tronado=dict(enabled=t.enabled, api_key=_mask(t.api_key), ipn_signing_key=_mask(t.ipn_signing_key),
                         wallet_address=t.wallet_address or "",
                         wallet_valid=(_tronado.is_valid_tron_address(t.wallet_address) if t.wallet_address else None),
                         wage_percent=_i(t.wage_percent), min_amount=_i(t.min_amount), base_url=t.base_url,
                         public_url=getattr(t, "public_url", "") or "",
                         ipn_url=_og.callback_url("tronado") or "", dispute_url=_og.dispute_callback_url() or ""),
            tonpays=dict(enabled=tp.enabled, api_key=_mask(tp.api_key), custom_tg_key=_mask(tp.custom_tg_key), webhook_secret=_mask(tp.webhook_secret),
                         base_url=tp.base_url, link_mode=tp.link_mode, send_buyer=bool(tp.send_buyer),
                         min_amount=_i(tp.min_amount), public_url=getattr(tp, "public_url", "") or "",
                         callback_url=_og.callback_url("tonpays") or ""),
            topup=sw,
        )

    @app.route("/api/admin/gateways", methods=["GET"])
    @P("botinfo")
    def adm_gw_get():
        return H.ok(**_gw_payload())

    @app.route("/api/admin/gateways", methods=["POST"])
    @P("botinfo")
    def adm_gw_set():
        b = request.get_json(silent=True) or {}
        act = b.get("action")
        gw = b.get("gateway")
        pm = _pm()
        if act == "topup":
            m = b.get("method")
            try:
                db.toggle_topup_switch(m)
            except ValueError:
                return H.fail("invalid", "روش نامعتبر")
            H.audit("topup_switch", str(m), "")
            return H.ok(**_gw_payload())
        if gw not in _FIELDS:
            return H.fail("invalid", "درگاه نامعتبر")
        cur = {"frenzyex": pm.frenzyex, "tronado": pm.tronado, "tonpays": pm.tonpays}[gw]
        upd = {"frenzyex": pm.update_frenzyex, "tronado": pm.update_tronado, "tonpays": pm.update_tonpays}[gw]
        if act == "toggle":
            if not cur.enabled:
                missing = []
                if not (cur.api_key or "").strip():
                    missing.append("API Key")
                if gw == "tronado":
                    if not (cur.wallet_address or "").strip():
                        missing.append("آدرس کیف پول")
                    elif not _tronado.is_valid_tron_address(cur.wallet_address):
                        missing.append("آدرس کیف پول معتبر")
                if missing:
                    return H.fail("missing", "ابتدا این موارد را تنظیم کنید: " + "، ".join(missing))
            upd(enabled=not cur.enabled)
        elif act == "cycle_method" and gw == "frenzyex":
            c = cur.payment_method if cur.payment_method in _PM_ORDER else "auto"
            upd(payment_method=_PM_ORDER[(_PM_ORDER.index(c) + 1) % 3])
        elif act == "cycle_link" and gw == "tonpays":
            upd(link_mode="telegram" if cur.link_mode == "web" else "web")
        elif act == "toggle_buyer" and gw == "tonpays":
            upd(send_buyer=not cur.send_buyer)
        elif act == "cycle_link" and gw == "frenzyex":
            upd(link_mode="bot" if cur.link_mode == "checkout" else "checkout")
        elif act == "set":
            field = b.get("field")
            kind = _FIELDS[gw].get(field)
            if kind is None:
                return H.fail("invalid", "فیلد نامعتبر")
            raw = str(b.get("value") or "").strip()
            value = None
            if kind in ("secret", "text", "url", "address") and not raw:
                return H.fail("invalid", "مقدار خالی است")
            if kind in ("int", "int0", "wage"):
                d = raw.replace(",", "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
                if not d.isdigit():
                    return H.fail("invalid", "فقط عدد صحیح")
                n = int(d)
                if kind == "wage" and not 0 <= n <= 100:
                    return H.fail("invalid", "عدد باید بین ۰ و ۱۰۰ باشد")
                if kind == "int" and n < 1000:
                    return H.fail("invalid", "حداقل مبلغ باید حداقل ۱۰۰۰ تومان باشد")
                value = n
            elif kind in ("url", "url_opt"):
                if kind == "url_opt" and raw in ("", "-"):
                    value = ""
                else:
                    v = raw if raw.lower().startswith(("http://", "https://")) else "https://" + raw
                    if " " in v or "." not in v.split("//", 1)[-1]:
                        return H.fail("invalid", "آدرس نامعتبر است")
                    value = v.rstrip("/")
            elif kind == "address":
                if not _tronado.is_valid_tron_address(raw):
                    return H.fail("invalid", "آدرس ترون نامعتبر است (حروف بزرگ/کوچک مهم است)")
                wallets = [str(c.wallet or "").strip().lower() for c in pm.cryptos if c.symbol.upper() == "TRX"]
                if raw.lower() in wallets:
                    return H.fail("invalid", "این آدرس با کیف پول TRX بخش «ارز دیجیتال» یکی است")
                value = raw
            else:
                value = raw
            upd(**{field: value})
        elif act == "test":
            import requests as rq
            if gw == "frenzyex":
                try:
                    r = rq.get(_frenzyex.base_url() + "/api/v1/health", timeout=(8, 15), allow_redirects=False)
                    okk = r.status_code == 200 and (r.json() or {}).get("ok") is True
                    msg = "✅ سرور FrenzyEx در دسترس است." if okk else f"❌ پاسخ نامعتبر (HTTP {r.status_code})"
                except Exception as exc:
                    msg = f"❌ خطای ارتباط: {type(exc).__name__}"
            elif gw == "tonpays":
                _ok, _m = _tonpays.test_connection()
                msg = ("✅ " if _ok else "❌ ") + _m
            else:
                price = _tronado.get_tron_price_toman(False)
                msg = f"✅ ارتباط برقرار است. قیمت هر TRX: {price:,.0f} تومان" if price else "❌ دریافت قیمت ترون ناموفق بود"
            return H.ok(message=msg, **_gw_payload())
        else:
            return H.fail("invalid", "عملیات نامعتبر")
        _reload()
        H.audit("gateway_" + str(gw), str(act), str(b.get("field") or ""))
        return H.ok(**_gw_payload())

    # ------------------------------------------------------------ naming
    @app.route("/api/admin/naming", methods=["GET"])
    @P("botinfo")
    def adm_naming_get():
        return H.ok(mode=bot_info.get_config_name_mode(), prefix=bot_info.get("config_name_prefix") or "")

    @app.route("/api/admin/naming", methods=["POST"])
    @P("botinfo")
    def adm_naming_set():
        b = request.get_json(silent=True) or {}
        if "mode" in b:
            try:
                bot_info.set_config_name_mode(str(b["mode"]))
            except ValueError:
                return H.fail("invalid", "حالت نامعتبر")
        if "prefix" in b:
            v = str(b["prefix"] or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{1,20}", v):
                return H.fail("invalid", "پیشوند فقط حروف/عدد انگلیسی و _ باشد (حداکثر ۲۰)")
            bot_info.set("config_name_prefix", v)
        H.audit("naming", "", str(b)[:100])
        return adm_naming_get()

    # ------------------------------------------------------------ referrals
    def _u(u):
        return dict(id=u["id"], telegram_id=u.get("telegram_id"), name=u.get("name") or "", username=u.get("username") or "",
                    invited_count=_i(u.get("invited_count")), successful_invites=_i(u.get("successful_invites")))

    @app.route("/api/admin/referrals", methods=["GET"])
    @P("referrals")
    def adm_refs():
        page, per, _o = H.page_args(10, 50)
        return H.ok(enabled=db.referral_enabled(), total=db.count_referrers(), page=page, per=per,
                    items=[_u(u) for u in db.get_referrers_page(page - 1, per)])

    @app.route("/api/admin/referrals/<int:uid>", methods=["GET"])
    @P("referrals")
    def adm_ref_detail(uid):
        items = [dict(id=u["id"], telegram_id=u.get("telegram_id"), name=u.get("name") or "", username=u.get("username") or "",
                      status=u.get("referral_status"), reward=_i(u.get("referral_reward")), created_at=u.get("referral_created_at"))
                 for u in db.get_referred_users(uid)]
        return H.ok(items=items)

    # ------------------------------------------------------------ campaigns
    @app.route("/api/admin/campaigns", methods=["GET"])
    @P("referrals")
    def adm_camps():
        out = []
        for c in db.list_campaigns():
            out.append(dict(id=c["id"], code=c["code"], name=c["name"], active=bool(c.get("is_active")),
                            created_at=c.get("created_at"), stats=db.get_campaign_stats(c["id"])))
        return H.ok(items=out)

    @app.route("/api/admin/campaigns", methods=["POST"])
    @P("referrals")
    def adm_camp_post():
        b = request.get_json(silent=True) or {}
        act = b.get("action") or "create"
        try:
            if act == "create":
                name = str(b.get("name") or "").strip()[:60]
                if not name:
                    return H.fail("invalid", "نام کمپین لازم است")
                db.create_campaign(str(b.get("code") or ""), name)
            elif act == "toggle":
                db.set_campaign_active(_i(b.get("id")), bool(b.get("active")))
            elif act == "rename":
                db.rename_campaign(_i(b.get("id")), str(b.get("name") or "")[:60])
            elif act == "delete":
                db.delete_campaign(_i(b.get("id")))
            else:
                return H.fail("invalid", "عملیات نامعتبر")
        except ValueError as e:
            return H.fail("invalid", str(e))
        except Exception as e:
            return H.fail("invalid", "کد کمپین تکراری یا نامعتبر است" if "UNIQUE" in str(e) else "خطا")
        H.audit("campaign_" + act, str(b.get("id") or b.get("code") or ""), "")
        return adm_camps()

    # ------------------------------------------------------------ guides
    @app.route("/api/admin/guides", methods=["GET"])
    @P("guides")
    def adm_guides():
        return H.ok(items=[dict(id=x["id"], title=x["title"], content_type=x.get("content_type"), body_text=x.get("body_text") or "",
                                has_file=bool(x.get("file_id"))) for x in db.get_guides()])

    @app.route("/api/admin/guides", methods=["POST"])
    @P("guides")
    def adm_guide_post():
        b = request.get_json(silent=True) or {}
        act = b.get("action") or "create"
        gid = _i(b.get("id"))
        if act == "create":
            title, body = str(b.get("title") or "").strip()[:100], str(b.get("body_text") or "").strip()
            if not title or not body:
                return H.fail("invalid", "عنوان و متن لازم است")
            db.create_guide(title, "text", body)
        elif act == "update":
            if not db.get_guide(gid):
                return H.fail("not_found", "یافت نشد", 404)
            kw = {}
            if b.get("title"): kw["title"] = str(b["title"]).strip()[:100]
            if "body_text" in b: kw["body_text"] = str(b["body_text"]).strip()
            db.update_guide(gid, **kw)
        elif act == "delete":
            db.delete_guide(gid)
        elif act in ("up", "down"):
            db.move_guide(gid, act)
        else:
            return H.fail("invalid", "عملیات نامعتبر")
        H.audit("guide_" + act, str(gid), "")
        return adm_guides()

    # ------------------------------------------------------------ fair usage (مصرف منصفانه)
    def _fup_state():
        import fair_usage as fu
        c = fu.get_cfg()
        def row(x):
            return dict(id=x["id"], name=x.get("service_id") or x.get("plan"), blocked=bool(x.get("fup_blocked")))
        return H.ok(enabled=c["enabled"], free_gb=round(c["free_bytes"] / fu.GB, 3), daily_mb=round(c["daily_bytes"] / fu.MB, 1),
                    started=len(db.get_fup_started_configs()), blocked=len(db.get_fup_blocked_configs()),
                    items=[row(x) for x in db.get_fup_blocked_configs(limit=30)])

    @app.route("/api/admin/fup", methods=["GET"])
    @P("vpn_panel")
    def adm_fup():
        return _fup_state()

    @app.route("/api/admin/fup", methods=["POST"])
    @P("vpn_panel")
    def adm_fup_set():
        import fair_usage as fu
        b = request.get_json(silent=True) or {}
        c = fu.get_cfg()
        if "enabled" in b:
            c["enabled"] = bool(b["enabled"])
        if "free_gb" in b:
            try:
                v = float(b["free_gb"])
            except (TypeError, ValueError):
                return H.fail("invalid", "حد مصرف آزاد نامعتبر")
            if v <= 0 or v > 10_000_000:
                return H.fail("invalid", "حد مصرف آزاد نامعتبر")
            c["free_bytes"] = int(v * fu.GB)
        if "daily_mb" in b:
            try:
                v = float(b["daily_mb"])
            except (TypeError, ValueError):
                return H.fail("invalid", "سهمیه‌ی روزانه نامعتبر")
            if v < 0 or v > 10_000_000_000:
                return H.fail("invalid", "سهمیه‌ی روزانه نامعتبر")
            c["daily_bytes"] = int(v * fu.MB)
        fu.save_cfg(c)
        H.audit("fup_setting", "", json.dumps({k: b[k] for k in ("enabled", "free_gb", "daily_mb") if k in b}))
        return _fup_state()

    @app.route("/api/admin/fup/<int:cid>/release", methods=["POST"])
    @P("vpn_panel")
    def adm_fup_release(cid):
        """آزادسازی دستی: روی پنل فعال می‌شود و سهمیه‌ی امروز از همین لحظه شروع می‌شود."""
        import asyncio
        import panels
        import fair_usage as fu
        cfg = db.get_config_by_id(cid)
        panel = db.get_vpn_panel(cfg["panel_id"]) if cfg and cfg.get("panel_id") else None
        if not cfg or not panel or not cfg.get("service_id"):
            return H.fail("not_found", "سرویس پیدا نشد", 404)
        ok, msg = loopsafe.run(panels.enable_service(panel, cfg["service_id"]))
        if not ok:
            return H.fail("panel_error", str(msg)[:200], 502)
        db.set_config_link_disabled(cid, False)
        fu.admin_release(cid)
        H.audit("fup_release", str(cid), "")
        return _fup_state()

    # ------------------------------------------------------------ backup (دریافت فوری + خودکار)
    def _bk_state():
        import adv_reports
        from handlers import admin_backup as ab
        st = ab.settings()
        return H.ok(enabled=st["enabled"], days=st["days"], last_date=st["last_date"], topic_ready=adv_reports.ready("backup"), time="00:00")

    @app.route("/api/admin/backup", methods=["GET"])
    @P("botinfo")
    def adm_backup():
        return _bk_state()

    @app.route("/api/admin/backup", methods=["POST"])
    @P("botinfo")
    def adm_backup_set():
        import adv_reports
        b = request.get_json(silent=True) or {}
        act = str(b.get("action") or "")
        if act == "now":
            res = adv_reports.backup_everywhere(True)
            if not res["admin"] and not res["topic"]:
                return H.fail("backup_failed", "ساخت/ارسال بک‌آپ ناموفق بود", 502)
            H.audit("backup_now", "", "")
            return H.ok(sent_admin=res["admin"], sent_topic=res["topic"])
        if act == "toggle":
            db.set_setting("autobackup_enabled", "1" if b.get("enabled") else "0")
        elif act == "days":
            d = _i(b.get("days"))
            if not 1 <= d <= 365:
                return H.fail("invalid", "عدد ۱ تا ۳۶۵")
            db.set_setting("autobackup_days", str(d))
        else:
            return H.fail("invalid", "عملیات نامعتبر")
        H.audit("backup_setting", act, "")
        return _bk_state()

    # ------------------------------------------------------------ advanced reports (گروه تاپیک‌دار)
    def _adv_state():
        import adv_reports, notif_settings
        c = adv_reports.get_cfg()
        return H.ok(configured=bool(c["chat_id"]), chat_id=c["chat_id"], enabled=c["enabled"], topics=[
            {"key": k, "title": t, "desc": d, "created": bool(c["topics"].get(k)), "enabled": notif_settings.enabled("adv_" + k)}
            for k, t, d in adv_reports.TOPICS])

    @app.route("/api/admin/adv", methods=["GET"])
    @P("botinfo")
    def adm_adv():
        return _adv_state()

    @app.route("/api/admin/adv", methods=["POST"])
    @P("botinfo")
    def adm_adv_set():
        import adv_reports, notif_settings
        b = request.get_json(silent=True) or {}
        act = str(b.get("action") or "")
        c = adv_reports.get_cfg()
        if act == "toggle_all":
            c["enabled"] = bool(b.get("enabled"))
            adv_reports.save_cfg(c)
        elif act == "toggle_topic":
            key = str(b.get("key") or "")
            if key not in adv_reports.TOPIC_TITLES:
                return H.fail("invalid", "تاپیک نامعتبر")
            notif_settings.set_enabled("adv_" + key, bool(b.get("enabled")))
        elif act == "backup":
            if not adv_reports.ready("backup"):
                return H.fail("not_ready", "تاپیک بک‌آپ هنوز ساخته نشده (راه‌اندازی از داخل ربات)")
            import threading
            threading.Thread(target=adv_reports.send_backup, args=(True,), daemon=True).start()
        else:
            return H.fail("invalid", "عملیات نامعتبر")
        H.audit("adv_reports", act, str(b.get("key") or ""))
        return _adv_state()

    # ------------------------------------------------------------ notification settings
    @app.route("/api/admin/notifs", methods=["GET"])
    @P("botinfo")
    def adm_notifs():
        import notif_settings
        return H.ok(items=notif_settings.all_items())

    @app.route("/api/admin/notifs", methods=["POST"])
    @P("botinfo")
    def adm_notifs_set():
        import notif_settings
        b = request.get_json(silent=True) or {}
        try:
            notif_settings.set_enabled(str(b.get("key") or ""), bool(b.get("enabled")))
        except ValueError:
            return H.fail("invalid", "مورد نامعتبر")
        H.audit("notif_setting", str(b.get("key")), str(bool(b.get("enabled"))))
        return adm_notifs()
