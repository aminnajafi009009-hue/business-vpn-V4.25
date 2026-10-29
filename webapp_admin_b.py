"""Admin mini-app API, part B: discounts, agents, team, settings, payments, broadcast, catalog, panels, logs."""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from datetime import datetime

from flask import request, g

import database as db
import payment_manager_v3 as pmv3
from webapp_common import make_helpers

log = logging.getLogger(__name__)

AUDIENCES = {
    "all": "همه‌ی کاربران", "buyers": "خریداران", "non_buyers": "بدون خرید", "wallet_positive": "دارای موجودی",
    "agents": "نمایندگان", "active_service": "سرویس فعال", "expired_service": "سرویس منقضی", "new_users": "کاربران جدید (۷ روز)",
    "no_service": "بدون سرویس", "multi_service": "چند سرویسه", "inactive": "غیرفعال", "has_test": "تست‌گرفته‌ها",
}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _pm():
    pm = pmv3.PaymentManager(db.get_setting, db.set_setting)
    pm.load()
    return pm


def _reload_bot_pm():
    """The bot runs in the same process: refresh its in-memory PaymentManager after we edit settings."""
    try:
        mod = sys.modules.get("handlers.admin")
        inst = getattr(mod, "_pm_instance", None) if mod else None
        if inst is not None:
            inst.load()
    except Exception:
        log.exception("reload pm failed")


def _mask(s: str, keep=4) -> str:
    s = str(s or "")
    return ("•" * max(0, len(s) - keep) + s[-keep:]) if s else ""


def _payg_payload():
    """PAYG در ربات از جدول settings خوانده می‌شود (نه pm_paygo)؛ مینی‌اپ هم همان را نشان می‌دهد."""
    s = db.get_payg_settings()
    return dict(enabled=bool(s["enabled"]), price_per_gb=_i(s["price_per_gb"]), min_balance=_i(s["min_balance"]),
                empty_action=s.get("empty_action") or "suspend", empty_message=s.get("empty_message") or "")


def register(app, api):
    H = make_helpers(api)
    P = H.admin_perm

    def main_only(f):
        from functools import wraps

        @wraps(f)
        @P(None)
        def w(*a, **kw):
            if not H.is_main_admin():
                return H.fail("forbidden", "فقط مدیر اصلی به این بخش دسترسی دارد.", 403)
            return f(*a, **kw)
        return w

    # ------------------------------------------------------------ discounts
    def _disc(d):
        def js(x):
            try:
                return json.loads(x) if x else []
            except Exception:
                return []
        return dict(id=d["id"], code=d["code"], type=d.get("discount_type") or "percent", percent=_i(d.get("percent")), amount=_i(d.get("amount")),
                    uses=_i(d.get("uses")), min_order_amount=_i(d.get("min_order_amount")), max_uses_per_user=_i(d.get("max_uses_per_user")),
                    expires_at=d.get("expires_at"), plans=js(d.get("applicable_plans")), users=js(d.get("allowed_user_ids")),
                    first_purchase_only=bool(d.get("first_purchase_only")), created_at=d.get("created_at"))

    @app.route("/api/admin/discounts", methods=["GET"])
    @P("discounts")
    def adm_discounts():
        return H.ok(items=[_disc(d) for d in db.get_all_discounts()])

    def _disc_fields(b):
        t = "amount" if b.get("type") == "amount" else "percent"
        f = dict(discount_type=t, percent=max(0, min(100, _i(b.get("percent")))) if t == "percent" else 0,
                 amount=max(0, _i(b.get("amount"))) if t == "amount" else 0, uses=max(0, _i(b.get("uses"), 1)),
                 min_order_amount=max(0, _i(b.get("min_order_amount"))), max_uses_per_user=max(0, _i(b.get("max_uses_per_user"))),
                 expires_at=(b.get("expires_at") or None))
        if t == "percent" and not f["percent"]:
            return None, "درصد تخفیف باید بین ۱ تا ۱۰۰ باشد"
        if t == "amount" and not f["amount"]:
            return None, "مبلغ تخفیف نامعتبر است"
        if f["expires_at"]:
            try:
                datetime.strptime(f["expires_at"][:19].replace("T", " ") + (":00" if len(f["expires_at"]) == 16 else ""), "%Y-%m-%d %H:%M:%S")
                f["expires_at"] = f["expires_at"][:19].replace("T", " ") + (":00" if len(f["expires_at"]) == 16 else "")
            except Exception:
                return None, "فرمت تاریخ انقضا نامعتبر است (YYYY-MM-DD HH:MM)"
        return f, None

    @app.route("/api/admin/discounts", methods=["POST"])
    @P("discounts")
    def adm_discount_create():
        b = request.get_json(silent=True) or {}
        code = re.sub(r"[^A-Za-z0-9_]", "", str(b.get("code") or "")).upper()
        if len(code) < 3:
            return H.fail("bad_code", "کد باید حداقل ۳ حرف/عدد انگلیسی باشد")
        if db.get_discount(code):
            return H.fail("exists", "این کد قبلاً ساخته شده است", 409)
        f, err = _disc_fields(b)
        if err:
            return H.fail("invalid", err)
        db.create_discount(code, percent=f["percent"], uses=f["uses"], discount_type=f["discount_type"], amount=f["amount"],
                           applicable_plans=b.get("plans") or None, min_order_amount=f["min_order_amount"],
                           max_uses_per_user=f["max_uses_per_user"], expires_at=f["expires_at"],
                           allowed_user_ids=b.get("users") or None, first_purchase_only=bool(b.get("first_purchase_only")))
        H.audit("discount_create", code)
        return H.ok(item=_disc(db.get_discount(code)))

    @app.route("/api/admin/discounts/<int:did>", methods=["POST"])
    @P("discounts")
    def adm_discount_update(did):
        if not db.get_discount_by_id(did):
            return H.fail("not_found", "کد پیدا نشد", 404)
        b = request.get_json(silent=True) or {}
        f, err = _disc_fields(b)
        if err:
            return H.fail("invalid", err)
        # فیلدی که مینی‌اپ نفرستاده دست‌نخورده می‌ماند (قبلاً ویرایش از مینی‌اپ محدوده‌ی پلن/کاربران را پاک می‌کرد)
        if "plans" in b:
            f["applicable_plans"] = b.get("plans") or None
        if "users" in b:
            f["allowed_user_ids"] = b.get("users") or None
        if "first_purchase_only" in b:
            f["first_purchase_only"] = 1 if b.get("first_purchase_only") else 0
        db.update_discount(did, **f)
        H.audit("discount_update", str(did))
        return H.ok(item=_disc(db.get_discount_by_id(did)))

    @app.route("/api/admin/discounts/<int:did>/delete", methods=["POST"])
    @P("discounts")
    def adm_discount_delete(did):
        d = db.get_discount_by_id(did)
        if not d:
            return H.fail("not_found", "کد پیدا نشد", 404)
        db.delete_discount_by_id(did)
        H.audit("discount_delete", d["code"])
        return H.ok()

    # --------------------------------------------------------------- agents
    @app.route("/api/admin/agents", methods=["GET"])
    @P("agency")
    def adm_agents():
        items = []
        for a in db.get_all_agents():
            u = db.get_user(a["telegram_id"]) or {}
            items.append(dict(telegram_id=a["telegram_id"], name=u.get("name") or "", percent=_i(a.get("vip_discount_percent")), prefix=a.get("custom_prefix") or "",
                              note=a.get("note") or "", categories=db.get_agent_category_ids(a["telegram_id"]), wallet=_i(u.get("wallet")),
                              total_purchase=_i(u.get("total_purchase")), created_at=a.get("created_at")))
        cats = [dict(id=c["id"], name=c["name"]) for c in db.get_vip_categories()]
        return H.ok(items=items, categories=cats)

    @app.route("/api/admin/agents", methods=["POST"])
    @P("agency")
    def adm_agent_save():
        b = request.get_json(silent=True) or {}
        tid = str(b.get("telegram_id") or "").strip()
        if not tid.isdigit():
            return H.fail("bad_id", "آیدی عددی تلگرام نامعتبر است")
        pct = _i(b.get("percent"), -1)
        if not 1 <= pct <= 100:
            return H.fail("bad_percent", "درصد تخفیف باید بین ۱ تا ۱۰۰ باشد")
        prefix = re.sub(r"[^A-Za-z0-9_]", "", str(b.get("prefix") or ""))[:12]
        db.add_agent(tid, pct, (b.get("note") or "")[:200], None, prefix)
        if "categories" in b:
            cats = [int(x) for x in (b.get("categories") or []) if str(x).isdigit()]
            db.set_agent_categories(tid, cats)
        if "prefix" in b and not prefix:
            db.set_agent_prefix(tid, "")
        H.audit("agent_save", tid, f"{pct}%")
        if not db.get_user(tid):
            return H.ok(warning="این کاربر هنوز ربات را استارت نکرده است.")
        return H.ok()

    @app.route("/api/admin/agents/<tid>/delete", methods=["POST"])
    @P("agency")
    def adm_agent_delete(tid):
        if not db.get_agent(tid):
            return H.fail("not_found", "نماینده پیدا نشد", 404)
        db.remove_agent(tid)
        H.audit("agent_delete", tid)
        return H.ok()

    # ----------------------------------------------------------------- team
    @app.route("/api/admin/team", methods=["GET"])
    @main_only
    def adm_team():
        return H.ok(items=[dict(telegram_id=a["telegram_id"], name=a.get("name"), permissions=a.get("permissions") or []) for a in db.get_all_sub_admins()],
                    sections=list(H.ADMIN_SECTIONS))

    @app.route("/api/admin/team", methods=["POST"])
    @main_only
    def adm_team_save():
        b = request.get_json(silent=True) or {}
        tid = str(b.get("telegram_id") or "").strip()
        if not tid.isdigit() or tid == str(api.ADMIN_ID):
            return H.fail("bad_id", "آیدی عددی نامعتبر است")
        perms = [p for p in (b.get("permissions") or []) if p in H.ADMIN_SECTIONS]
        db.add_sub_admin(tid, (b.get("name") or "")[:60], perms)
        H.audit("team_save", tid, ",".join(perms))
        return H.ok()

    @app.route("/api/admin/team/<tid>/delete", methods=["POST"])
    @main_only
    def adm_team_delete(tid):
        db.delete_sub_admin(tid)
        H.audit("team_delete", tid)
        return H.ok()

    # ------------------------------------------------------------- settings
    @app.route("/api/admin/settings", methods=["GET"])
    @P("settings")
    def adm_settings_get():
        ft = db.get_effective_free_test_plan()
        return H.ok(
            orders_enabled=db.is_orders_enabled(),
            custom_build=db.get_effective_custom_build_settings(),
            custom_build_enabled=db.get_setting("custom_build_enabled", "1") != "0",
            free_test=dict(volume_gb=round(float(ft.get("volume_gb") or 0), 3), duration_value=ft.get("duration_value"), duration_unit=ft.get("duration_unit"), price=_i(ft.get("price"))),
            referral=db.get_referral_settings(),
            announcement=db.get_setting("miniapp_announcement", "") or "",
            can_orders_toggle=H.is_main_admin() or "orders_toggle" in H.allowed_sections(),
        )

    @app.route("/api/admin/settings/custom-build", methods=["POST"])
    @P("settings")
    def adm_set_cb():
        b = request.get_json(silent=True) or {}
        v = {k: _i(b.get(k), -1) for k in ("price_per_gb", "price_per_30_days", "min_gb", "max_gb", "min_days", "max_days")}
        if any(x < 0 for x in v.values()) or v["min_gb"] < 1 or v["min_days"] < 1 or v["max_gb"] < v["min_gb"] or v["max_days"] < v["min_days"]:
            return H.fail("invalid", "مقادیر نامعتبر است (حداقل نباید از حداکثر بیشتر باشد)")
        db.set_custom_build_override(**v)
        if "enabled" in b:
            db.set_setting("custom_build_enabled", "1" if b.get("enabled") else "0")
        H.audit("settings_custom_build", "", json.dumps(v))
        return H.ok()

    @app.route("/api/admin/settings/free-test", methods=["POST"])
    @P("settings")
    def adm_set_ft():
        b = request.get_json(silent=True) or {}
        try:
            vol_mb = int(float(b.get("volume_gb")) * 1024)
            val = float(b.get("duration_value"))
        except Exception:
            return H.fail("invalid", "مقادیر نامعتبر است")
        unit = "hours" if b.get("duration_unit") == "hours" else "days"
        if vol_mb <= 0 or val <= 0:
            return H.fail("invalid", "حجم و مدت باید بزرگ‌تر از صفر باشند")
        db.set_free_test_override(vol_mb, val, unit, max(0, _i(b.get("price"))))
        H.audit("settings_free_test", "", f"{vol_mb}MB {val}{unit}")
        return H.ok()

    @app.route("/api/admin/settings/referral", methods=["POST"])
    @P("settings")
    def adm_set_ref():
        b = request.get_json(silent=True) or {}
        n = 0
        for k, v in b.items():
            try:
                db.set_referral_setting(k, v)
                n += 1
            except Exception:
                pass
        H.audit("settings_referral", "", json.dumps(b)[:200])
        return H.ok(updated=n, referral=db.get_referral_settings())

    @app.route("/api/admin/referral-settings", methods=["GET"])
    @P("referrals")
    def adm_ref_settings_get():
        return H.ok(referral=db.get_referral_settings())

    @app.route("/api/admin/referral-settings", methods=["POST"])
    @P("referrals")
    def adm_ref_settings_set():
        b = request.get_json(silent=True) or {}
        n = 0
        for k, v in b.items():
            try:
                db.set_referral_setting(k, v)
                n += 1
            except Exception:
                pass
        H.audit("settings_referral", "", json.dumps(b)[:200])
        return H.ok(updated=n, referral=db.get_referral_settings())

    @app.route("/api/admin/settings/announcement", methods=["POST"])
    @P("settings")
    def adm_set_ann():
        t = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:300]
        db.set_setting("miniapp_announcement", t)
        H.audit("announcement", "", t[:60])
        return H.ok()

    # ------------------------------------------------------------- payments
    def _pay_payload():
        pm = _pm()
        c, w, u, p = pm.card, pm.wallet, pm.uniquepay, pm.paygo
        return dict(
            card=dict(enabled=c.enabled, card_number=c.card_number, owner_name=c.owner_name, bank_name=c.bank_name,
                      show_copy_card=bool(c.show_copy_card), show_copy_amount=bool(c.show_copy_amount), forward_lock=bool(c.forward_lock), random_amount=bool(c.random_amount)),
            wallet=dict(enabled=w.enabled, min_charge=w.min_charge, max_charge=w.max_charge, gift_percent=w.gift_percent, low_balance=int(db.get_wallet_settings().get("low_balance") or 0)),
            online=dict(enabled=u.enabled, min_amount=u.min_amount, auto_confirm=u.auto_confirm, token_hint=_mask(u.business_token),
                        redirect_url=u.redirect_url, callback_url=u.callback_url, use_telegram_link=u.use_telegram_link,
                        show_card_white_label=bool(u.show_card_white_label), env_token=bool(__import__("config").UNIQUEPAY_BUSINESS_TOKEN)),
            paygo=_payg_payload(),
            crypto=dict(master=pm.crypto_master_enabled, items=[dict(symbol=x.symbol, name=x.name, network=x.network, enabled=x.enabled, margin_percent=x.margin_percent, min_amount_toman=x.min_amount_toman, has_wallet=bool(x.wallet), wallet=x.wallet or "", backup_price=x.backup_price or 0) for x in pm.cryptos]),
        )

    @app.route("/api/admin/payments", methods=["GET"])
    @P("botinfo")
    def adm_pay_get():
        return H.ok(**_pay_payload())

    @app.route("/api/admin/payments", methods=["POST"])
    @P("botinfo")
    def adm_pay_set():
        b = request.get_json(silent=True) or {}
        pm = _pm()
        sec = b.get("section")
        d = b.get("values") or {}
        try:
            if sec == "card":
                kw = {}
                if "enabled" in d: kw["enabled"] = bool(d["enabled"])
                for _k in ("show_copy_card", "show_copy_amount", "forward_lock", "random_amount"):
                    if _k in d: kw[_k] = bool(d[_k])
                if "card_number" in d:
                    n = re.sub(r"\D", "", str(d["card_number"]))
                    if n and len(n) != 16:
                        return H.fail("invalid", "شماره کارت باید ۱۶ رقم باشد")
                    kw["card_number"] = n
                if "owner_name" in d: kw["owner_name"] = str(d["owner_name"])[:60]
                if "bank_name" in d: kw["bank_name"] = str(d["bank_name"])[:40]
                pm.update_card(**kw)
            elif sec == "wallet":
                kw = {}
                if "enabled" in d: kw["enabled"] = bool(d["enabled"])
                for k in ("min_charge", "max_charge"):
                    if k in d: kw[k] = max(0, _i(d[k]))
                if "gift_percent" in d: kw["gift_percent"] = max(0.0, min(100.0, float(d["gift_percent"])))
                pm.update_wallet(**kw)
                if "low_balance" in d:
                    db.set_wallet_setting("low_balance", max(0, _i(d["low_balance"])))
            elif sec == "online":
                kw = {}
                if "enabled" in d: kw["enabled"] = bool(d["enabled"])
                if "min_amount" in d: kw["min_amount"] = max(0, _i(d["min_amount"]))
                if "auto_confirm" in d: kw["auto_confirm"] = bool(d["auto_confirm"])
                if "use_telegram_link" in d: kw["use_telegram_link"] = bool(d["use_telegram_link"])
                if "show_card_white_label" in d: kw["show_card_white_label"] = bool(d["show_card_white_label"])
                for k in ("redirect_url", "callback_url"):
                    if k in d:
                        v = str(d[k] or "").strip()
                        if v and not re.match(r"^https?://", v):
                            return H.fail("invalid", "آدرس باید با http:// یا https:// شروع شود")
                        kw[k] = v[:300]
                if d.get("business_token"):
                    kw["business_token"] = str(d["business_token"]).strip()[:200]
                pm.update_uniquepay(**kw)
            elif sec == "paygo":
                kw = {}
                if "enabled" in d: kw["enabled"] = bool(d["enabled"])
                if "price_per_gb" in d: kw["price_per_gb"] = max(0, _i(d["price_per_gb"]))
                if "min_balance" in d: kw["min_balance"] = max(0, _i(d["min_balance"]))
                if d.get("empty_action") in ("suspend", "delete"): kw["empty_action"] = d["empty_action"]
                if "empty_message" in d: kw["empty_message"] = str(d["empty_message"])[:500]
                db.set_payg_settings(**kw)
            elif sec == "crypto_master":
                pm.set_crypto_master_enabled(bool(d.get("enabled")))
            elif sec == "crypto":
                c = pm.get_crypto(str(d.get("symbol") or ""))
                if not c:
                    return H.fail("not_found", "ارز پیدا نشد", 404)
                if "enabled" in d: c.enabled = bool(d["enabled"])
                if "margin_percent" in d: c.margin_percent = max(0.0, min(50.0, float(d["margin_percent"])))
                if "min_amount_toman" in d: c.min_amount_toman = max(0, _i(d["min_amount_toman"]))
                if "wallet" in d: c.wallet = str(d["wallet"] or "").strip()[:200]
                if "name" in d and str(d["name"]).strip(): c.name = str(d["name"]).strip()[:40]
                if "backup_price" in d: c.backup_price = max(0.0, float(d["backup_price"] or 0)) or None
                pm._save_cryptos()
            elif sec == "crypto_add":
                sym = re.sub(r"[^A-Za-z0-9]", "", str(d.get("symbol") or "")).upper()[:12]
                name = str(d.get("name") or "").strip()[:40]
                if not sym or not name:
                    return H.fail("invalid", "نماد و نام ارز الزامی است")
                pm.add_crypto(symbol=sym, name=name, network=(str(d.get("network") or "").strip()[:20] or sym), emoji="💎",
                              wallet=str(d.get("wallet") or "").strip()[:200], margin_percent=3.0)
            elif sec == "crypto_delete":
                sym = str(d.get("symbol") or "").strip().upper()
                if not pm.remove_crypto(sym):
                    return H.fail("not_found", "ارز پیدا نشد", 404)
                try:
                    db.set_setting(f"crypto_backup_price_{sym}", "")
                    db.set_setting(f"crypto_backup_enabled_{sym}", "0")
                except Exception:
                    pass
            else:
                return H.fail("invalid", "بخش نامعتبر")
        except (TypeError, ValueError):
            return H.fail("invalid", "مقدار نامعتبر است")
        _reload_bot_pm()
        H.audit("payments_" + str(sec), "", json.dumps(d, ensure_ascii=False)[:200])
        return H.ok(**_pay_payload())

    # ------------------------------------------------------------ broadcast
    @app.route("/api/admin/broadcast/audiences", methods=["GET"])
    @P("broadcast")
    def adm_bc_aud():
        out = []
        for k, label in AUDIENCES.items():
            try:
                n = len(db.get_broadcastable_users(k, respect_prefs=True))
            except Exception:
                n = None
            if n is not None:
                out.append(dict(key=k, label=label, count=n))
        return H.ok(items=out)

    import re as _re
    _HTML_RE = _re.compile(r"</?(b|i|u|s|a|code|pre|tg-emoji|tg-spoiler|blockquote|strong|em)\b", _re.I)

    def _run_bc(job_id, users, text):
        job = _jobs[job_id]
        use_html = bool(_HTML_RE.search(text))
        for u in users:
            if job.get("stop"):
                break
            try:
                ok = False
                if use_html:
                    ok = bool(api.tg_send_message(u.get("telegram_id"), text, parse_mode="HTML"))
                if not ok:
                    ok = bool(api.tg_send_message(u.get("telegram_id"), text))
            except Exception:
                ok = False
            job["sent" if ok else "failed"] += 1
            time.sleep(0.05)
        job["done"] = True

    @app.route("/api/admin/broadcast", methods=["POST"])
    @P("broadcast")
    def adm_bc_send():
        b = request.get_json(silent=True) or {}
        text = (b.get("text") or "").strip()
        aud = b.get("audience") or "all"
        if not text or len(text) > 3500:
            return H.fail("invalid", "متن پیام خالی یا بیش از حد طولانی است")
        if aud not in AUDIENCES:
            return H.fail("invalid", "گروه مخاطب نامعتبر است")
        with _jobs_lock:
            if any(not j.get("done") for j in _jobs.values()):
                return H.fail("busy", "یک ارسال همگانی در حال انجام است", 409)
            users = db.get_broadcastable_users(aud, respect_prefs=True)
            jid = str(int(time.time() * 1000))
            _jobs[jid] = dict(total=len(users), sent=0, failed=0, done=False, audience=aud)
            for k in list(_jobs)[:-5]:
                _jobs.pop(k, None)
        threading.Thread(target=_run_bc, args=(jid, users, text), daemon=True).start()
        H.audit("broadcast", aud, f"{len(users)} users: {text[:60]}")
        return H.ok(job=jid, total=len(users))

    @app.route("/api/admin/broadcast/rich", methods=["POST"])
    @P("broadcast")
    def adm_bc_rich():
        """پیام همگانی «ریچ»: همان مسیر ربات (copyMessage) که ایموجی پرمیوم،
        فرمت، رسانه و دکمه‌ها را عیناً حفظ می‌کند. ادمین در چت ربات پیام می‌فرستد."""
        b = request.get_json(silent=True) or {}
        aud = b.get("audience") or "all"
        if aud not in AUDIENCES:
            return H.fail("invalid", "گروه مخاطب نامعتبر است")
        uid = (getattr(g, "tg_user", None) or {}).get("id")
        if not uid:
            return H.fail("invalid", "کاربر شناسایی نشد")
        n = len(db.get_broadcastable_users(aud, respect_prefs=True))
        kb = {"inline_keyboard": [[{"text": "✍️ ارسال پیام (ریچ)", "callback_data": "bcaud_" + aud}]]}
        sent = api.tg_send_message(
            uid,
            f"📢 پیام همگانی ریچ\n\nمخاطب: {AUDIENCES[aud]} ({n:,} نفر)\n"
            "برای حفظ ایموجی پرمیوم، رسانه و دکمه‌ها، دکمه‌ی زیر را بزنید و پیام را همین‌جا در ربات بفرستید.",
            reply_markup=kb,
        )
        if not sent:
            return H.fail("send_failed", "ارسال پیام به چت ربات ناموفق بود؛ ابتدا ربات را استارت کنید", 502)
        H.audit("broadcast_rich_prepare", aud)
        return H.ok()

    @app.route("/api/admin/broadcast/<job>", methods=["GET"])
    @P("broadcast")
    def adm_bc_status(job):
        j = _jobs.get(job)
        if not j:
            return H.fail("not_found", "ارسال پیدا نشد", 404)
        return H.ok(**{k: v for k, v in j.items() if k != "stop"})

    @app.route("/api/admin/broadcast/<job>/stop", methods=["POST"])
    @P("broadcast")
    def adm_bc_stop(job):
        j = _jobs.get(job)
        if j:
            j["stop"] = True
        return H.ok()

    # -------------------------------------------------------------- catalog
    @app.route("/api/admin/catalog", methods=["GET"])
    @P("plans")
    def adm_catalog():
        cats = []
        for c in db.get_vip_categories():
            plans = [dict(id=p["id"], key=p["plan_key"], name=p["name"], price=_i(p.get("price")), days=_i(p.get("days")), volume_gb=_i(p.get("volume_gb")), user_limit=_i(p.get("user_limit")), mapped=bool(db.get_panel_plan_map("vip_plan", p["id"]))) for p in db.get_vip_plans(c["id"])]
            try:
                _desc = db.get_vip_category_description(c)[0]
            except Exception:
                _desc = ""
            cats.append(dict(id=c["id"], name=c["name"], parent_id=c.get("parent_id"), description=_desc or "", plans=plans, mapped=bool(db.get_panel_plan_map("vip_category", c["id"]))))
        return H.ok(categories=cats)

    @app.route("/api/admin/catalog/category", methods=["POST"])
    @P("plans")
    def adm_cat_create():
        b = request.get_json(silent=True) or {}
        name = (b.get("name") or "").strip()[:60]
        if not name:
            return H.fail("invalid", "نام دسته خالی است")
        c = db.create_vip_category(name, b.get("parent_id"))
        H.audit("category_create", name)
        return H.ok(id=c["id"])

    @app.route("/api/admin/catalog/category/<int:cid>", methods=["POST"])
    @P("plans")
    def adm_cat_rename(cid):
        b = request.get_json(silent=True) or {}
        name = (b.get("name") or "").strip()[:60]
        if not name or not db.get_vip_category(cid):
            return H.fail("invalid", "نام یا دسته نامعتبر است")
        db.rename_vip_category(cid, name)
        if "description" in b:
            new_desc = str(b.get("description") or "").strip()[:1000]
            try:
                old_desc = db.get_vip_category_description(cid)[0] or ""
            except Exception:
                old_desc = ""
            if new_desc != old_desc.strip():   # فقط اگر واقعاً عوض شده (تا entityهای ریچ‌تکست ربات بی‌دلیل پاک نشوند)
                db.update_vip_category_description(cid, new_desc or None, None)
        H.audit("category_rename", str(cid), name)
        return H.ok()

    @app.route("/api/admin/catalog/category/<int:cid>/move", methods=["POST"])
    @P("plans")
    def adm_cat_move(cid):
        b = request.get_json(silent=True) or {}
        if b.get("direction") in ("up", "down"):
            if not db.move_vip_category(cid, b["direction"]):
                return H.fail("invalid", "جابجایی ممکن نیست")
        elif "parent_id" in b:
            pid = b.get("parent_id")
            ok, msg = db.move_vip_category_to(cid, int(pid) if pid not in (None, "", 0) else None)
            if not ok:
                return H.fail("invalid", msg)
        else:
            return H.fail("invalid", "درخواست نامعتبر")
        H.audit("category_move", str(cid), json.dumps(b)[:80])
        return H.ok()

    @app.route("/api/admin/catalog/plan/<key>/move", methods=["POST"])
    @P("plans")
    def adm_plan_move(key):
        plan = db.get_vip_plan(key)
        if not plan:
            return H.fail("not_found", "پلن پیدا نشد", 404)
        b = request.get_json(silent=True) or {}
        if b.get("direction") in ("up", "down"):
            if not db.move_vip_plan(plan["id"], b["direction"]):
                return H.fail("invalid", "جابجایی ممکن نیست")
        elif b.get("category_id") and db.get_vip_category(_i(b["category_id"])):
            db.move_vip_plans_to_category([key], _i(b["category_id"]))
        else:
            return H.fail("invalid", "درخواست نامعتبر")
        H.audit("plan_move", key, json.dumps(b)[:80])
        return H.ok()

    @app.route("/api/admin/catalog/category/<int:cid>/delete", methods=["POST"])
    @P("plans")
    def adm_cat_delete(cid):
        if not db.delete_vip_category(cid):
            return H.fail("not_empty", "فقط دسته‌ی خالی (بدون پلن و زیردسته) قابل حذف است")
        H.audit("category_delete", str(cid))
        return H.ok()

    @app.route("/api/admin/catalog/plan", methods=["POST"])
    @P("plans")
    def adm_plan_create():
        b = request.get_json(silent=True) or {}
        name = (b.get("name") or "").strip()[:80]
        cid = _i(b.get("category_id"))
        if not name or not db.get_vip_category(cid):
            return H.fail("invalid", "نام یا دسته نامعتبر است")
        if _i(b.get("price"), -1) < 0 or _i(b.get("days"), -1) < 0 or _i(b.get("volume_gb"), -1) < 0:
            return H.fail("invalid", "قیمت، روز و حجم نامعتبر است")
        key = db.add_vip_plan(cid, name, _i(b["price"]), _i(b.get("days")), _i(b.get("volume_gb")), _i(b.get("user_limit")))
        H.audit("plan_create", key, name)
        return H.ok(key=key)

    @app.route("/api/admin/catalog/plan/<key>", methods=["POST"])
    @P("plans")
    def adm_plan_update(key):
        if not db.get_vip_plan(key):
            return H.fail("not_found", "پلن پیدا نشد", 404)
        b = request.get_json(silent=True) or {}
        kw = {}
        if b.get("name"): kw["name"] = str(b["name"]).strip()[:80]
        for k in ("price", "days", "volume_gb", "user_limit"):
            if k in b:
                if _i(b[k], -1) < 0:
                    return H.fail("invalid", "مقدار نامعتبر است")
                kw[k] = _i(b[k])
        db.update_vip_plan(key, **kw)
        H.audit("plan_update", key, json.dumps(kw, ensure_ascii=False))
        return H.ok()

    @app.route("/api/admin/catalog/plan/<key>/delete", methods=["POST"])
    @P("plans")
    def adm_plan_delete(key):
        if not db.get_vip_plan(key):
            return H.fail("not_found", "پلن پیدا نشد", 404)
        db.delete_vip_plan(key)
        H.audit("plan_delete", key)
        return H.ok()

    # --------------------------------------------------------------- panels
    @app.route("/api/admin/panels", methods=["GET"])
    @P("vpn_panel")
    def adm_panels():
        out = []
        for p in db.list_vpn_panels():
            h = db.get_panel_health(p["id"]) or {}
            cnt = db.get_connection().cursor()
            cnt.execute("SELECT COUNT(*) FROM configs WHERE panel_id=? AND deleted=0", (p["id"],))
            out.append(dict(id=p["id"], type=p["panel_type"], name=p["name"], enabled=bool(p.get("enabled")), services=_i(cnt.fetchone()[0]),
                            host=re.sub(r"^https?://", "", str(p.get("base_url") or "")).split("/")[0],
                            health=dict(circuit_open=bool(h.get("circuit_open")), failures=_i(h.get("failures")), latency_ms=h.get("last_latency_ms"), last_ok_at=h.get("last_ok_at"), last_error=(h.get("last_error") or "")[:160])))
        return H.ok(items=out)

    @app.route("/api/admin/panels/<int:pid>/toggle", methods=["POST"])
    @P("vpn_panel")
    def adm_panel_toggle(pid):
        p = next((x for x in db.list_vpn_panels() if x["id"] == pid), None)
        if not p:
            return H.fail("not_found", "پنل پیدا نشد", 404)
        en = not bool(p.get("enabled"))
        db.update_vpn_panel(pid, enabled=en)
        H.audit("panel_toggle", p["name"], "on" if en else "off")
        return H.ok(enabled=en)

    # ----------------------------------------------------------------- logs
    @app.route("/api/admin/logs", methods=["GET"])
    @P("logs")
    def adm_logs():
        tab = request.args.get("tab") or "admin"
        if tab == "errors":
            rows = db.get_error_logs(limit=40)
            return H.ok(items=[dict(id=r["id"], created_at=r.get("occurred_at"), title=str(r.get("message") or "")[:200],
                                    where=str(r.get("error_type") or "")[:80]) for r in rows])
        rows = db.get_recent_admin_logs(60)
        return H.ok(items=[dict(id=r["id"], created_at=r.get("created_at"), admin=r.get("admin_name"), action=r.get("action"), target=r.get("target"), details=r.get("details")) for r in rows])
