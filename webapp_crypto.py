"""شارژ کیف پول با ارز دیجیتال در مینی‌اپ — همان منطق handlers/wallet.py (نرخ زنده/پشتیبان، فاکتور ۳۰ دقیقه‌ای، رسید برای ادمین)."""
from __future__ import annotations

import asyncio
import loopsafe
import logging

from flask import g, jsonify, request

import database as db

log = logging.getLogger(__name__)


def _topup_error(amount: int):
    lim = db.get_wallet_settings()
    if amount < lim["min_topup"]:
        return f"حداقل مبلغ شارژ {lim['min_topup']:,} تومان است."
    if amount > lim["max_topup"]:
        return f"حداکثر مبلغ شارژ {lim['max_topup']:,} تومان است."
    return None


def register(app, api):
    auth = api.require_telegram_auth

    def _pm():
        from handlers.admin import _pm as pm
        return pm()

    @app.route("/api/wallet/crypto/coins", methods=["GET"])
    @auth
    def api_crypto_coins():
        if "crypto" not in db.available_topup_methods():
            return jsonify({"coins": []})
        coins = [c for c in _pm().enabled_cryptos if str(c.wallet or "").strip()]
        return jsonify({"coins": [{"symbol": c.symbol, "network": c.network, "name": c.name, "emoji": getattr(c, "emoji", "💎")} for c in coins]})

    @app.route("/api/wallet/crypto/create", methods=["POST"])
    @auth
    @api.rate_limit(max_calls=8, period_seconds=60)
    def api_crypto_create():
        b = request.get_json(silent=True) or {}
        amount, symbol = b.get("amount"), str(b.get("symbol") or "").upper()
        if not isinstance(amount, int) or amount <= 0:
            return jsonify({"error": "invalid_amount"}), 400
        err = _topup_error(amount)
        if err:
            return jsonify({"error": "amount_out_of_range", "message": err}), 400
        if "crypto" not in db.available_topup_methods():
            return jsonify({"error": "method_disabled", "message": "این روش برای شارژ کیف پول فعال نیست."}), 403
        pm = _pm()
        coin = pm.get_crypto(symbol)
        if not coin or not coin.enabled or not pm.crypto_master_enabled or not str(coin.wallet or "").strip():
            return jsonify({"error": "coin_unavailable", "message": "این ارز در حال حاضر در دسترس نیست."}), 400
        rate = None
        try:
            import crypto_rates
            rate, _src = loopsafe.run(crypto_rates.get_rate_toman(symbol))
        except Exception:
            log.exception("live crypto rate failed for %s", symbol)
        if not rate or float(rate) <= 0:
            try:
                backup_on = db.get_crypto_backup_enabled(symbol)
            except Exception:
                backup_on = True
            if backup_on:
                rate = coin.backup_price or db.get_crypto_backup_price(symbol)
        if not rate or float(rate) <= 0:
            return jsonify({"error": "rate_unavailable", "message": "دریافت قیمت لحظه‌ای این ارز ممکن نشد؛ چند دقیقه دیگر تلاش کنید."}), 503
        amount_asset = round(amount * (1 + float(coin.margin_percent or 0) / 100.0) / float(rate), 6)
        if amount_asset <= 0:
            return jsonify({"error": "calc_failed", "message": "محاسبه مبلغ ارزی ناموفق بود."}), 500
        user = g.user
        inv = db.create_invoice(user_id=user["id"], telegram_id=str(user["telegram_id"]), kind="wallet_crypto", label="شارژ کیف پول", price=amount,
                                payload={"asset": coin.symbol, "amount": amount_asset, "network": coin.network})
        return jsonify({"invoice_id": inv["id"], "expires_at": inv["expires_at"], "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
                        "symbol": coin.symbol, "network": coin.network, "wallet": str(coin.wallet).strip(), "amount_asset": str(amount_asset), "price": amount})

    @app.route("/api/wallet/crypto/receipt", methods=["POST"])
    @auth
    @api.rate_limit(max_calls=5, period_seconds=60)
    def api_crypto_receipt():
        invoice_id = request.form.get("invoice_id", type=int)
        hash_text = (request.form.get("hash") or "").strip()
        photo = request.files.get("receipt")
        if photo is None and len(hash_text) < 8:
            return jsonify({"error": "invalid_request", "message": "عکس رسید یا هش تراکنش را ارسال کنید."}), 400
        if photo is not None and not api._valid_receipt_photo(photo):
            return jsonify({"error": "invalid_file_type"}), 400
        inv = db.get_invoice(invoice_id) if invoice_id else None
        if not inv or inv.get("kind") != "wallet_crypto" or str(inv.get("telegram_id")) != str(g.user["telegram_id"]):
            return jsonify({"error": "invoice_expired"}), 410
        if db.consume_invoice(invoice_id) is None:
            return jsonify({"error": "invoice_expired"}), 410
        payload = inv.get("payload") or {}
        if isinstance(payload, str):
            import json
            try: payload = json.loads(payload)
            except Exception: payload = {}
        amount = int(inv["price"])
        user = g.user
        rid = None
        try:
            rid = db.create_pending_receipt("charge", user["telegram_id"], user["id"], "شارژ کیف پول (ارز دیجیتال)", amount, note=(hash_text or None))
        except Exception:
            log.exception("ثبت رسید شارژ ارزی ناموفق بود")
        caption = (f"💱 رسید شارژ کیف پول (ارز دیجیتال)\n\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
                   f"💰 {amount:,} تومان\n💵 {payload.get('amount')} {payload.get('asset')}\n🌐 شبکه: {payload.get('network') or '-'}\n"
                   f"🧾 کد پیگیری: {rid if rid is not None else invoice_id}" + (f"\n📝 Hash / Receipt: {hash_text}" if hash_text else ""))
        from keyboards import admin_charge_approval_keyboard
        kb = api._kb_json(admin_charge_approval_keyboard(str(user["telegram_id"]), amount, rid))
        if photo is not None:
            sent = api.tg_send_photo_bytes(api.ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=kb)
        else:
            sent = api.tg_send_message(api.ADMIN_ID, caption, reply_markup=kb)
        if not sent:
            if rid is not None:
                try: db.delete_pending_receipt(rid)
                except Exception: pass
            return jsonify({"error": "telegram_delivery_failed", "message": "ارسال رسید به ادمین ناموفق بود؛ دوباره تلاش کنید."}), 502
        if rid is not None:
            try: db.set_pending_receipt_media(rid, details=caption)
            except Exception: pass
        db.delete_invoice(invoice_id)
        return jsonify({"success": True})
