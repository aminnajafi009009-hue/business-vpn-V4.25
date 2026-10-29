"""
online_gateways.py
لایه‌ی یکپارچه‌ی درگاه‌های پرداخت آنلاین «جدید» (FrenzyEx و Tronado) در کنار یونیک‌پی.

همه‌ی مسیرهای ربات (خرید پلن، بساز-سرویس/تمدید، شارژ کیف پول) و مینی‌اپ فقط با همین ماژول حرف
می‌زنند: ساخت فاکتور، استعلام وضعیت، لغو، پردازش کال‌بک وب‌هوک. ردیف هر پرداخت در جدول
online_payments است (ستون gateway + gw_data) و تحویل نهایی با claim اتمیک همان جدول انجام می‌شود،
پس پولر ربات، دکمه‌ی «بررسی کن»، مینی‌اپ و وب‌هوک هرگز نمی‌توانند یک پرداخت را دوبار تحویل بدهند.

وضعیت‌های ردیف: pending → processing → paid | expired (نگه‌داری برای پرداخت دیرهنگام) |
review (مغایرت مبلغ/شناسه؛ تأیید دستی ادمین) | rejected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import database as db
import frenzyex
import tonpays
import tronado

logger = logging.getLogger(__name__)

NEW_GATEWAYS = ("frenzyex", "tronado", "tonpays")

# short → کلید کامل؛ برای callback_data تلگرام (حداکثر ۶۴ بایت) کوتاه نگه داشته می‌شود.
SHORT_TO_GATEWAY = {"fx": "frenzyex", "tr": "tronado", "tp": "tonpays"}
GATEWAY_TO_SHORT = {v: k for k, v in SHORT_TO_GATEWAY.items()}

_INFO = {
    "frenzyex": {
        "title": "FrenzyEx",
        "button": "💳 پرداخت با FrenzyEx",
        "menu_button": "💳 پرداخت آنلاین (FrenzyEx)",
        "method_label": "پرداخت آنلاین (FrenzyEx)",
        "module": frenzyex,
        "callback_path": "/api/payments/frenzyex/callback",
    },
    "tronado": {
        "title": "ترونادو",
        "button": "🌪 پرداخت با ترونادو",
        "menu_button": "🌪 پرداخت آنلاین (ترونادو)",
        "method_label": "پرداخت آنلاین (ترونادو)",
        "module": tronado,
        "callback_path": "/api/payments/tronado/callback",
    },
    "tonpays": {
        "title": "تون‌پیز",
        "button": "💰 کارت به کارت(Tonpays)",
        "menu_button": "💎 پرداخت آنلاین (تون‌پیز)",
        "method_label": "پرداخت آنلاین (تون‌پیز)",
        "module": tonpays,
        "callback_path": "/api/payments/tonpays/callback",
    },
}

# اگر وضعیت «پرداخت‌شده» دیده شد ولی داده‌ی مبلغ/تأییدیه‌ی امضاشده هنوز نیامده، این‌قدر برای
# کال‌بک صبر می‌کنیم؛ بعد از آن پرداخت به بررسی دستی ادمین می‌رود.
CALLBACK_GRACE_SECONDS = 600


def is_new_gateway(name: str | None) -> bool:
    return name in NEW_GATEWAYS


def gateway_of(payment: dict | None) -> str:
    return (payment or {}).get("gateway") or "uniquepay"


def resolve_short(short: str) -> str | None:
    return SHORT_TO_GATEWAY.get(short)


def short_of(name: str) -> str:
    return GATEWAY_TO_SHORT[name]


def title(name: str) -> str:
    return _INFO.get(name, {}).get("title", name)


def button_text(name: str) -> str:
    return _INFO[name]["button"]


def menu_button_text(name: str) -> str:
    return _INFO[name]["menu_button"]


def method_label(name_or_payment) -> str:
    """برچسب «روش پرداخت» سفارش (در گزارش/آمار ادمین)."""
    name = name_or_payment if isinstance(name_or_payment, str) else gateway_of(name_or_payment)
    return _INFO.get(name, {}).get("method_label", "پرداخت آنلاین")


def admin_label(payment: dict) -> str:
    """برچسب داخل پیام‌های ادمین: «پرداخت آنلاین - یونیک‌پی» برای درگاه قدیمی، و نام درگاه برای جدیدها."""
    name = gateway_of(payment)
    if name in _INFO:
        return _INFO[name]["method_label"]
    return "پرداخت آنلاین - یونیک‌پی"


def enabled(name: str) -> bool:
    mod = _INFO.get(name, {}).get("module")
    try:
        return bool(mod and mod.enabled())
    except Exception:
        logger.exception("خطا در بررسی فعال‌بودن درگاه %s", name)
        return False


def enabled_gateways() -> list[str]:
    return [g for g in NEW_GATEWAYS if enabled(g)]


def min_amount(name: str) -> int:
    return _INFO[name]["module"].min_amount()


def amount_ok(name: str, amount: int) -> bool:
    return _INFO[name]["module"].amount_ok(amount)


def new_ref(prefix: str = "order", gw: str | None = None) -> str:
    """شناسه‌ی یکتای فاکتور (order_ref فرنزی / PaymentID ترونادو). فقط [A-Za-z0-9_-] تا هر دو درگاه بپذیرند.
    تون‌پیز: order_id حداکثر ۲۰ کاراکتر است."""
    if gw == "tonpays":
        return "tp" + uuid.uuid4().hex[:18]
    prefix = re.sub(r"[^A-Za-z0-9_\-]", "", prefix) or "order"
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# آدرس عمومی کال‌بک
# ---------------------------------------------------------------------------
def public_base_url(name: str) -> str:
    """آدرس عمومی HTTPS سرویس: تنظیم ادمین → GATEWAY_PUBLIC_URL → RENDER_EXTERNAL_URL → RAILWAY_PUBLIC_DOMAIN."""
    mod = _INFO[name]["module"]
    raw = (mod.cfg().public_url or "").strip()
    if not raw:
        raw = (os.environ.get("GATEWAY_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
    if not raw:
        dom = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        raw = dom
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw.rstrip("/")


def callback_url(name: str) -> str:
    base = public_base_url(name)
    return (base + _INFO[name]["callback_path"]) if base else ""


def dispute_callback_url() -> str:
    base = public_base_url("tronado")
    return (base + "/api/payments/tronado/dispute") if base else ""


# ---------------------------------------------------------------------------
# ساخت فاکتور
# ---------------------------------------------------------------------------
@dataclass
class CreateResult:
    ok: bool
    error: str = ""
    link: str | None = None
    link2: str | None = None           # لینک دوم (مثلاً «باز کردن در چت ربات» فرنزی)
    ref_id: str | None = None          # request_id فرنزی / token ترونادو
    gw_data: dict = field(default_factory=dict)
    info_lines: list[str] = field(default_factory=list)


def _tonpays_info_lines(data: dict, amount: int) -> list[str]:
    info = []
    fa = data.get("final_amount")
    try:
        if fa and int(fa) != int(amount):
            info.append(f"💳 مبلغ دقیق قابل پرداخت در تون‌پیز: {int(fa):,} تومان")
    except (TypeError, ValueError):
        pass
    return info


def _create_tonpays_tg(ref: str, amount: int, buyer_id) -> CreateResult:
    """حالت «تلگرام» تون‌پیز: فاکتور کاستوم (کلید جدا) که کارت مقصد را خودِ API برمی‌گرداند.
    کارت داخل ربات نشان داده می‌شود (تعویض کارت، ارسال رسید، بررسی وضعیت)."""
    cb = callback_url("tonpays") or None
    res = tonpays.create_tg_invoice(int(amount), ref, cb, buyer_id)
    if not res["ok"]:
        return CreateResult(False, res["error"] or "خطای نامشخص تون‌پیز")
    data = res["data"] or {}
    iid = str(data.get("invoice_id") or "")
    gw = {"invoice_id": iid, "final_amount": data.get("final_amount"), "kind": "tg"}
    gw.update(tonpays.extract_card_info(data))
    gw.setdefault("tracking_code", iid)  # کد پیگیری نمایشی = شناسه‌ی فاکتور (پاسخ حالت تلگرام کد جدا ندارد)
    if not gw.get("card_number"):
        # بدون شماره‌کارت صفحه‌ی کارت ساخته نمی‌شود؛ فاکتور تازه‌ساخته را لغو می‌کنیم
        try:
            tonpays.cancel_invoice(iid, "tg")
        except Exception:
            pass
        return CreateResult(False, "تون‌پیز شماره کارت مقصد را در پاسخ حالت تلگرام نفرستاد؛ حالت «لینک» را در تنظیمات امتحان کنید.")
    return CreateResult(True, link=None, link2=None, ref_id=iid, gw_data=gw, info_lines=_tonpays_info_lines(data, amount))


def _create_tonpays(ref: str, amount: int, buyer_id) -> CreateResult:
    if tonpays.cfg().link_mode == "telegram" and tonpays.custom_tg_key() and buyer_id:
        return _create_tonpays_tg(ref, amount, buyer_id)
    cb = callback_url("tonpays") or None
    res = tonpays.create_invoice(int(amount), ref, cb, buyer_id)
    if not res["ok"] and res.get("code") == "BUYER_HAS_PENDING_INVOICE" and buyer_id:
        # خریدار فاکتور pending قبلی دارد: فاکتورهای pending خودِ ربات برای همین کاربر را لغو و یک‌بار دوباره تلاش می‌کنیم
        try:
            cur = db.get_connection().cursor()
            cur.execute("SELECT id, ref_id, gw_data FROM online_payments WHERE gateway = 'tonpays' AND status = 'pending' AND telegram_id = ?",
                        (str(buyer_id),))
            old = cur.fetchall()
            for row in old:
                rid = row[1] if not isinstance(row, dict) else row.get("ref_id")
                rpid = row[0] if not isinstance(row, dict) else row.get("id")
                raw_gw = row[2] if not isinstance(row, dict) else row.get("gw_data")
                if rid:
                    cr = tonpays.cancel_invoice(rid, tonpays.kind_of({"gw_data": raw_gw}))
                    if cr["ok"] or cr.get("code") in ("INVOICE_ALREADY_CANCELED", "INVOICE_EXPIRED", "INVOICE_NOT_FOUND"):
                        try:
                            db.merge_gw_data(rpid, closed=True)
                        except Exception:
                            pass
            res = tonpays.create_invoice(int(amount), ref, cb, buyer_id)
        except Exception:
            logger.exception("لغو فاکتور pending قبلی تون‌پیز ناموفق بود")
    if not res["ok"]:
        return CreateResult(False, res["error"] or "خطای نامشخص تون‌پیز")
    data = res["data"] or {}
    links = tonpays.pick_links(data)
    if not links["primary"]:
        return CreateResult(False, "تون‌پیز لینک پرداخت برنگرداند")
    iid = str(data["invoice_id"])
    gw = {"invoice_id": iid, "final_amount": data.get("final_amount"), "link2": links["secondary"]}
    gw.update(tonpays.extract_card_info(data))
    return CreateResult(True, link=links["primary"], link2=links["secondary"], ref_id=iid, gw_data=gw,
                        info_lines=_tonpays_info_lines(data, amount))


def create_payment_sync(name: str, ref: str, amount: int, description: str | None = None, buyer_id=None) -> CreateResult:
    if name not in _INFO:
        return CreateResult(False, "درگاه نامعتبر")
    if not enabled(name):
        return CreateResult(False, "درگاه فعال/کامل تنظیم نشده است")
    if not amount_ok(name, amount):
        return CreateResult(False, f"مبلغ کمتر از حداقل درگاه ({min_amount(name):,} تومان) است")

    if name == "frenzyex":
        res = frenzyex.create_payment_request(int(amount), ref, description)
        if not res["ok"]:
            return CreateResult(False, res["error"] or "خطای نامشخص FrenzyEx")
        data = res["data"] or {}
        links = frenzyex.pick_links(data)
        if not links["primary"]:
            return CreateResult(False, "FrenzyEx لینک پرداخت برنگرداند")
        gw = {"request_id": str(data["request_id"]), "expires_at": data.get("expires_at"), "link2": links["secondary"]}
        return CreateResult(True, link=links["primary"], link2=links["secondary"],
                            ref_id=str(data["request_id"]), gw_data=gw)

    if name == "tonpays":
        return _create_tonpays(ref, amount, buyer_id)

    # tronado
    cb = callback_url("tronado")
    res = tronado.create_order(ref, int(amount), cb)
    if not res["ok"]:
        return CreateResult(False, res["error"] or "خطای نامشخص ترونادو")
    gw = {
        "token": res["token"], "tron_amount": res["tron_amount"], "wage": res["wage"],
        "estimated_toman": res["estimated_toman"],
    }
    info = [f"🪙 مقدار ترون فاکتور: {res['tron_amount']:g} TRX"]
    if res["estimated_toman"]:
        info.append(f"💰 مبلغ تقریبی پرداختی شما در ترونادو: {int(res['estimated_toman']):,} تومان")
    return CreateResult(True, link=res["link"], ref_id=res["token"][:80], gw_data=gw, info_lines=info)


async def create_payment(name: str, ref: str, amount: int, description: str | None = None, buyer_id=None) -> CreateResult:
    return await asyncio.to_thread(create_payment_sync, name, ref, amount, description, buyer_id)


# ---------------------------------------------------------------------------
# استعلام وضعیت
# ---------------------------------------------------------------------------
@dataclass
class GwResult:
    state: str                  # paid | pending | closed | review | error | action_less | action_more
    reason: str = ""
    paid_toman: int | None = None
    data: dict | None = None    # پاسخ خام درگاه (برای action_less/action_more تون‌پیز)


def _flatten_tronado_status(data: dict) -> dict:
    if "OrderStatusID" not in data and isinstance(data.get("Data"), dict):
        return data["Data"]
    return data


def check_payment_sync(payment: dict) -> GwResult:
    """وضعیت واقعی یک پرداخت درگاه جدید را از خود درگاه می‌پرسد (یا تأییدیه‌ی امضاشده‌ی ذخیره‌شده را برمی‌گرداند).
    هیچ استثنایی نمی‌دهد؛ خطا → state="error"."""
    name = gateway_of(payment)
    if name not in NEW_GATEWAYS:
        return GwResult("error", "درگاه نامعتبر")
    gw = db.gw_data_of(payment)
    pid = payment["id"]

    if gw.get("confirmed"):
        return GwResult("paid", paid_toman=gw.get("paid_toman"))

    try:
        if payment.get("status") == "expired":
            db.merge_gw_data(pid, last_check=time.time())

        if name == "frenzyex":
            res = frenzyex.get_status(payment.get("ref_id") or "")
            if not res["ok"]:
                return GwResult("error", res["error"] or "")
            state, reason = frenzyex.evaluate_status(payment, res["data"])
            if state == "review" and gw.get("approved"):
                state, reason = "paid", ""
            if state == "closed":
                db.merge_gw_data(pid, closed=True)
            return GwResult(state, reason)

        if name == "tonpays":
            res = tonpays.get_status(payment.get("ref_id") or "", tonpays.kind_of(payment))
            if not res["ok"]:
                return GwResult("error", res["error"] or "")
            d = res["data"] or {}
            state, reason = tonpays.evaluate(payment, d)
            if state == "review" and gw.get("approved"):
                state, reason = "paid", ""
            if state == "closed":
                db.merge_gw_data(pid, closed=True)
                return GwResult("closed", reason)
            if state == "action_less":
                return GwResult("action_less", tonpays_less_text(d), data=d)
            if state == "action_more":
                return GwResult("action_more", tonpays_more_text(d), data=d)
            if state == "pending":
                return GwResult("pending")
            return GwResult(state, reason)

        # tronado
        res = tronado.get_status(payment["hash_id"])
        if not res["ok"]:
            return GwResult("error", res["error"] or "")
        data = _flatten_tronado_status(res["data"])
        if "OrderStatusID" not in data and "IsPaid" not in data:
            return GwResult("error", "پاسخ استعلام ترونادو شامل وضعیت نبود")
        verdict, reason, paid = tronado.evaluate(payment, gw, data, "status")
        if verdict == "paid":
            return GwResult("paid", paid_toman=paid)
        if verdict == "review":
            return GwResult("review", reason, paid)
        if verdict == "wait":
            first = gw.get("first_paid_seen")
            if not first:
                db.merge_gw_data(pid, first_paid_seen=time.time())
                return GwResult("pending", "منتظر کال‌بک امضاشده")
            if time.time() - float(first) >= CALLBACK_GRACE_SECONDS:
                return GwResult("review", reason or "کال‌بک امضاشده‌ی ترونادو دریافت نشد", paid)
            return GwResult("pending", "منتظر کال‌بک امضاشده")
        code = tronado.status_code_of(data)
        if code in (tronado.STATUS_REJECTED, tronado.STATUS_EXPIRED):
            db.merge_gw_data(pid, closed=True)
            return GwResult("closed", "rejected" if code == tronado.STATUS_REJECTED else "expired")
        return GwResult("pending")
    except Exception:
        logger.exception("خطا در بررسی وضعیت پرداخت %s (%s)", payment.get("hash_id"), name)
        return GwResult("error", "خطای داخلی")


async def check_payment(payment: dict) -> GwResult:
    return await asyncio.to_thread(check_payment_sync, payment)


def tonpays_less_text(d: dict) -> str:
    """متن راهنمای «مبلغ کمتر واریز شده» برای کاربر."""
    try:
        amt = int(float(d.get("remaining_amount") or 0))
    except (TypeError, ValueError):
        amt = 0
    card = str(d.get("remaining_card_number") or "")
    holder = str(d.get("remaining_card_holder") or "")
    received = bool(d.get("remaining_receipt_received"))
    lines = ["⚠️ مبلغ کمتر از فاکتور واریز شده است."]
    if amt:
        lines.append(f"💰 مابقی: {amt:,} تومان")
    if card:
        lines.append(f"💳 کارت مقصد: {card}" + (f" به نام {holder}" if holder else ""))
    if received:
        lines.append("✅ رسید مابقی ثبت شده و در انتظار تأیید است.")
    else:
        lines.append("همین مبلغ مابقی را واریز کنید؛ تأیید خودکار انجام می‌شود. اگر بعد از واریز تأیید نشد، با دکمه‌ی «📸 ارسال رسید مابقی» رسید را بفرستید.")
    return "\n".join(lines)


def tonpays_more_text(d: dict) -> str:
    """متن راهنمای «مبلغ بیشتر واریز شده» برای کاربر."""
    try:
        amt = int(float(d.get("refund_amount") or 0))
    except (TypeError, ValueError):
        amt = 0
    lines = ["⚠️ مبلغ بیشتر از فاکتور واریز شده است."]
    if amt:
        lines.append(f"💰 مازاد قابل عودت: {amt:,} تومان")
    if d.get("refund_pending"):
        lines.append("✅ کارت عودت ثبت شده؛ مازاد توسط تون‌پیز واریز می‌شود.")
    else:
        lines.append("برای ادامه، با دکمه‌ی «💳 ثبت کارت عودت» شماره کارت و نام صاحب کارت خودتان را ثبت کنید؛ مازاد به همان کارت برمی‌گردد و سفارش شما تأیید می‌شود.")
    return "\n".join(lines)


def cancel_payment_sync(payment: dict) -> None:
    """لغو درخواست پرداخت‌نشده روی درگاه (best-effort؛ فقط FrenzyEx این API را دارد)."""
    try:
        if gateway_of(payment) == "frenzyex" and payment.get("ref_id"):
            res = frenzyex.cancel_request(payment["ref_id"])
            if not res["ok"] and res.get("status") not in (404, 409):
                logger.info("لغو درخواست FrenzyEx %s نشد: %s", payment.get("ref_id"), res.get("error"))
        if gateway_of(payment) == "tonpays" and payment.get("ref_id"):
            res = tonpays.cancel_invoice(payment["ref_id"], tonpays.kind_of(payment))
            if not res["ok"]:
                logger.info("لغو فاکتور تون‌پیز %s نشد: %s", payment.get("ref_id"), res.get("error"))
    except Exception:
        logger.exception("لغو درخواست درگاه ناموفق بود")


async def cancel_payment(payment: dict) -> None:
    await asyncio.to_thread(cancel_payment_sync, payment)


# ---------------------------------------------------------------------------
# متن‌های مشترک (کاربر/ادمین)
# ---------------------------------------------------------------------------
def kind_label(payment: dict) -> str:
    k = payment.get("kind")
    if k == "wallet_charge":
        return "شارژ کیف پول"
    if k == "custom":
        return "سرویس سفارشی"
    return payment.get("plan_name") or "سفارش"


def review_admin_text(payment: dict, reason: str) -> str:
    name = gateway_of(payment)
    gw = db.gw_data_of(payment)
    lines = [
        f"⚠️ پرداخت {title(name)} نیازمند بررسی دستی است؛ تحویل/شارژ خودکار انجام نشد.",
        "",
        f"🧾 فاکتور: {payment.get('hash_id')}",
        f"🆔 کاربر: {payment.get('telegram_id')}",
        f"📦 {kind_label(payment)}",
        f"💰 مبلغ فاکتور: {int(payment['price']):,} تومان",
    ]
    if gw.get("paid_toman"):
        lines.append(f"💸 مبلغ پرداختی کاربر: {int(gw['paid_toman']):,} تومان")
    lines.append(f"📝 دلیل: {reason}")
    lines.append("")
    lines.append("با «تأیید» پرداخت دقیقاً مثل یک پرداخت موفق تحویل می‌شود؛ با «رد» هیچ اتفاقی نمی‌افتد.")
    return "\n".join(lines)


def review_user_text(payment: dict) -> str:
    return (
        "⏳ پرداخت شما ثبت شد اما برای تأیید نهایی به بررسی مدیریت ارجاع شد.\n"
        "لطفاً کمی صبر کنید یا با پشتیبانی در ارتباط باشید.\n\n"
        f"🧾 کد پیگیری: {payment.get('id')}"
    )


def review_buttons(payment_id: int) -> list[tuple[str, str]]:
    return [("✅ تأیید و تحویل", f"gwok_{payment_id}"), ("❌ رد", f"gwno_{payment_id}")]


def hold_for_review_sync(payment: dict, reason: str, notify_admin: Callable | None, notify_user: Callable | None) -> bool:
    """انتقال یک‌باره‌ی پرداخت به review + اعلان ادمین/کاربر (sync؛ برای وب‌هوک و مینی‌اپ). True=واقعاً منتقل شد."""
    changed = db.hold_online_payment_for_review(payment["id"], reason)
    if changed:
        fresh = db.get_online_payment(payment["id"]) or payment
        try:
            if notify_admin:
                notify_admin(review_admin_text(fresh, reason), review_buttons(payment["id"]))
        except Exception:
            logger.exception("اعلان بررسی دستی به ادمین ناموفق بود")
        try:
            if notify_user:
                notify_user(payment["telegram_id"], review_user_text(payment))
        except Exception:
            logger.exception("اعلان بررسی دستی به کاربر ناموفق بود")
    return changed


# ---------------------------------------------------------------------------
# وب‌هوک‌ها (sync؛ توسط روت‌های Flask صدا زده می‌شوند). خروجی: (http_status, body_text)
# ---------------------------------------------------------------------------
def _json_body(raw: bytes) -> dict | None:
    try:
        d = json.loads((raw or b"").decode("utf-8"))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def handle_frenzyex_callback(raw: bytes, headers, notify_admin: Callable | None = None,
                             notify_user: Callable | None = None) -> tuple[int, str]:
    if not frenzyex.callback_secret():
        logger.error("FrenzyEx callback رسید ولی Callback Secret تنظیم نشده است.")
        return 500, "Callback secret not configured"
    if not frenzyex.verify_signature(raw, headers.get("X-Frenzy-Signature")):
        logger.warning("FrenzyEx callback با امضای نامعتبر رد شد.")
        return 401, "Invalid signature"
    data = _json_body(raw)
    if data is None:
        return 400, "Invalid body"

    event = str(headers.get("X-Frenzy-Event") or data.get("event") or "")
    if event == "payment.test" or data.get("test") is True:
        return 200, "OK"

    order_ref = data.get("order_ref") if data.get("order_ref") not in (None, "") else data.get("order_id")
    rid = next((str(data[k]) for k in ("request_id", "payment_id", "purchase_id") if data.get(k) not in (None, "")), "")
    payment = None
    if order_ref not in (None, ""):
        payment = db.get_online_payment_for_gateway_ref("frenzyex", str(order_ref))
    if payment is None and rid:
        payment = db.get_online_payment_by_gateway_ref_id("frenzyex", rid)
    if payment is None:
        logger.warning("FrenzyEx callback برای فاکتور ناشناخته: order_ref=%s request_id=%s", order_ref, rid)
        return 404, "Order not found"

    if payment["status"] in ("paid", "processing", "rejected"):
        return 200, "Already processed"

    # callback حتماً باید payment.completed با status=paid باشد؛ بقیه بدون هیچ اثری 200 می‌گیرند
    # (evaluate_callback شناسه/مبلغ/ارز را هم با فاکتور تطبیق می‌دهد).
    verdict, reason = frenzyex.evaluate_callback(payment, dict(data, event=event or data.get("event")))
    if verdict == "ignore":
        return 200, "OK"
    if verdict == "review":
        hold_for_review_sync(payment, reason, notify_admin, notify_user)
        return 200, "OK"
    db.merge_gw_data(payment["id"], confirmed=True, confirmed_at=time.time(),
                     tx_hash=str(data.get("tx_hash") or "")[:200] or None)
    return 200, "OK"


def handle_tonpays_callback(raw: bytes, headers, notify_admin: Callable | None = None,
                            notify_user: Callable | None = None) -> tuple[int, str]:
    sig = headers.get("X-TonPays-Signature")
    signed = False
    if webhook_secret_set := bool(tonpays.webhook_secret()):
        if not tonpays.verify_signature(raw, sig):
            logger.warning("TonPays callback با امضای نامعتبر رد شد.")
            return 401, "Invalid signature"
        signed = True
    data = _json_body(raw)
    if data is None:
        return 400, "Invalid body"
    oid = data.get("order_id")
    iid = str(data.get("invoice_id") or "")
    payment = None
    if oid not in (None, ""):
        payment = db.get_online_payment_for_gateway_ref("tonpays", str(oid))
    if payment is None and iid:
        payment = db.get_online_payment_by_gateway_ref_id("tonpays", iid)
    if payment is None:
        logger.warning("TonPays callback برای فاکتور ناشناخته: order_id=%s invoice_id=%s", oid, iid)
        return 404, "Order not found"
    if payment["status"] in ("paid", "processing", "rejected"):
        return 200, "Already processed"

    if not signed:
        # سکرت امضا تنظیم نشده: به بدنه‌ی وب‌هوک اعتماد نمی‌کنیم؛ وضعیت را مستقیم از API تون‌پیز می‌پرسیم
        res = tonpays.get_status(payment.get("ref_id") or iid, tonpays.kind_of(payment))
        if not res["ok"]:
            return 503, "Verification failed"
        data = res["data"] or {}
    state, reason = tonpays.evaluate(payment, data)
    if state == "paid":
        db.merge_gw_data(payment["id"], confirmed=True, confirmed_at=time.time(),
                         final_amount=data.get("final_amount"), credit_amount=data.get("credit_amount"))
    elif state == "review":
        hold_for_review_sync(payment, reason, notify_admin, notify_user)
    elif state in ("action_less", "action_more"):
        # دکمه‌ی «ارسال رسید مابقی/ثبت کارت عودت» را پولر/دکمه‌ی «بررسی کن» ربات می‌فرستد (متن ساده از وب‌هوک
        # دکمه ندارد)؛ اینجا فقط یک‌بار متن راهنما را به کاربر می‌گوییم.
        flag = "less_sent" if state == "action_less" else "more_sent"
        note = tonpays_less_text(data) if state == "action_less" else tonpays_more_text(data)
        gw = db.gw_data_of(payment)
        if not gw.get(flag):
            db.merge_gw_data(payment["id"], **{flag: True})
            try:
                if notify_user:
                    notify_user(payment["telegram_id"], note)
            except Exception:
                logger.exception("اعلان مبلغ کمتر/بیشتر تون‌پیز به کاربر ناموفق بود")
    elif state == "closed":
        db.merge_gw_data(payment["id"], closed=True)
    return 200, "OK"


def handle_tronado_callback(raw: bytes, headers, notify_admin: Callable | None = None,
                            notify_user: Callable | None = None) -> tuple[int, str]:
    if not tronado.ipn_signing_key():
        logger.error("Tronado callback رسید ولی IpnSigningKey تنظیم نشده است.")
        return 500, "Signing key not configured"
    if not tronado.verify_signature(raw, headers.get("X-Tronado-Sig")):
        logger.warning("Tronado callback با امضای نامعتبر رد شد.")
        return 401, "Invalid signature"
    data = _json_body(raw)
    if data is None:
        return 400, "Invalid body"
    pid = data.get("PaymentId", data.get("PaymentID"))
    if pid is None:
        return 400, "Missing payment identifier"
    if not isinstance(pid, (str, int)) or isinstance(pid, bool) or not re.match(r"^[A-Za-z0-9_\-]{1,128}$", str(pid)):
        return 400, "Invalid payment identifier"
    payment = db.get_online_payment_for_gateway_ref("tronado", str(pid))
    if payment is None:
        logger.warning("Tronado callback برای فاکتور ناشناخته: %s", str(pid)[:40])
        return 404, "Order not found"
    if payment["status"] in ("paid", "processing", "rejected"):
        return 200, "Already processed"

    gw = db.gw_data_of(payment)
    verdict, reason, paid = tronado.evaluate(payment, gw, data, "callback")
    code = tronado.status_code_of(data)
    if verdict == "not_paid":
        if code == tronado.STATUS_REJECTED and notify_user:
            try:
                notify_user(payment["telegram_id"], "❌ رسید پرداخت شما توسط ترونادو رد شد. لطفاً دوباره تلاش کنید یا روش دیگری انتخاب کنید.")
            except Exception:
                logger.exception("اعلان رد رسید ترونادو به کاربر ناموفق بود")
        if code in (tronado.STATUS_REJECTED, tronado.STATUS_EXPIRED):
            db.merge_gw_data(payment["id"], closed=True, remote_status=code)
        return 200, "OK"
    if verdict in ("review", "wait"):
        # در کال‌بک امضاشده «wait» معنی ندارد (evaluate برای callback فقط paid/review می‌دهد)؛ محافظه‌کارانه review.
        if paid:
            db.merge_gw_data(payment["id"], paid_toman=paid)
        fresh = db.get_online_payment(payment["id"]) or payment
        hold_for_review_sync(fresh, reason or "مغایرت در کال‌بک ترونادو", notify_admin, notify_user)
        return 200, "OK"
    db.merge_gw_data(payment["id"], confirmed=True, confirmed_at=time.time(),
                     paid_toman=paid, tx_hash=str(data.get("TransactionHash") or data.get("TxHash") or "")[:200] or None)
    return 200, "OK"


def handle_tronado_dispute(raw: bytes, headers, notify_admin: Callable | None = None) -> tuple[int, str]:
    """کال‌بک اعتراض (Dispute): فقط رویداد DisputeAccepted؛ هیچ برگشت مالی خودکاری انجام نمی‌شود،
    فقط ادمین مطلع می‌شود و اگر هنوز تحویل نشده، پرداخت برای بررسی دستی نگه داشته می‌شود."""
    if not tronado.ipn_signing_key():
        return 500, "Signing key not configured"
    if not tronado.verify_signature(raw, headers.get("X-Tronado-Sig")):
        return 401, "Invalid signature"
    data = _json_body(raw)
    if data is None:
        return 400, "Invalid body"
    if str(data.get("Event") or "") != "DisputeAccepted":
        return 200, "Ignored"
    pid = str(data.get("PaymentId") or "").strip()
    event_id = str(data.get("EventId") or "").strip()[:64]
    try:
        dispute_id = int(data.get("DisputeId"))
    except (TypeError, ValueError):
        dispute_id = 0
    if dispute_id <= 0 or not event_id or not re.match(r"^[A-Za-z0-9_\-]{1,128}$", pid):
        return 400, "Invalid dispute payload"
    payment = db.get_online_payment_for_gateway_ref("tronado", pid)
    if payment is None:
        return 200, "Ignored"

    gw = db.gw_data_of(payment)
    seen = list(gw.get("dispute_events") or [])
    if event_id in seen:
        return 200, "Duplicate"
    seen.append(event_id)
    db.merge_gw_data(payment["id"], dispute_events=seen[-20:])

    already = payment["status"] == "paid"
    if payment["status"] in ("pending", "expired"):
        db.hold_online_payment_for_review(payment["id"], f"اعتراض ترونادو پذیرفته شد (dispute {dispute_id})")
    if notify_admin:
        text = (
            "⚠️ اعتراض پذیرفته‌شده‌ی ترونادو — نیاز به بررسی دستی (هیچ برگشت مالی خودکاری انجام نشد)\n\n"
            f"🧾 فاکتور: {pid}\n🆔 کاربر: {payment.get('telegram_id')}\n🆔 شناسه اعتراض: {dispute_id}\n"
            f"📌 نوع: {data.get('DisputeTypeTitle') or data.get('DisputeType') or '-'}\n"
            f"🔁 نتیجه: {data.get('Outcome') or '-'}\n"
            f"💰 مبلغ فاکتور ما: {int(payment['price']):,} تومان\n"
            f"💳 مبلغ تومانی قبل: {int(_f(data.get('OriginalUserMustPayToman'))):,} ← بعد: {int(_f(data.get('UserMustPayToman'))):,}\n"
            f"🪙 ترون قبل: {data.get('OriginalTronAmount', '-')} ← بعد: {data.get('TronAmount', '-')}\n"
            f"📦 وضعیت فاکتور در ربات: {'پرداخت‌شده و تحویل‌شده' if already else 'هنوز تحویل نشده (به بررسی دستی رفت)'}"
        )
        try:
            notify_admin(text, review_buttons(payment["id"]) if not already else None)
        except Exception:
            logger.exception("اعلان اعتراض ترونادو به ادمین ناموفق بود")
    return 200, "OK"


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
