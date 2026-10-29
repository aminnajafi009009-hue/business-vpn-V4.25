"""کشف خودکار متن‌های واقعی ربات (سمت کاربر) از روی کد — نسخه‌ی ۲.

تفاوت با نسخه‌ی قبل:
- متن‌هایی که اول داخل متغیر (text = f"...") ساخته و بعد ارسال می‌شوند هم پیدا می‌شوند.
- فقط پیام‌هایی که به «کاربر» (یا کانال لاگ) می‌روند ثبت می‌شوند؛ پیام‌های پنل ادمین نه.
- کلید هر متن پایدار است (فقط به مسیر فایل و خود متن وابسته است، نه شماره‌ی خط)؛ پس با
  تغییر کد، ویرایش‌های ادمین از دست نمی‌رود.
- هر متن در دسته‌بندی درست قرار می‌گیرد.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

USER_FILES = [
    "handlers/menu.py", "handlers/start.py", "handlers/plans.py", "handlers/wallet.py",
    "handlers/profile.py", "handlers/referral.py", "handlers/ticket.py",
    "utils.py", "alerts.py", "bot.py", "payment_manager_v3.py", "handlers/gateways.py",
]
MIXED_FILES = ["handlers/admin.py", "handlers/panel_admin.py"]

SELF_SENDS = {"answer", "edit_text", "edit_caption", "reply", "answer_photo", "answer_video",
              "answer_document", "answer_animation"}
BOT_SENDS = {"send_message", "send_photo", "send_video", "send_document", "send_animation"}
HELPERS = {"show_menu_with_sticker": 1, "send_user_notification": 1}  # نام تابع -> ایندکس آرگومان chat_id

_PERSIAN = re.compile(r"[؀-ۿ]")
_PH = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

GATEWAY_CATEGORY = "🌐 درگاه‌های پرداخت آنلاین"

# دسته‌بندی بر اساس «نام تابعِ هندلر» — دقیق‌تر از حدس از روی متن؛ قبل از CATEGORY_RULES بررسی می‌شود.
FUNC_RULES = [
    ("⚡ پرداخت به‌ازای مصرف (PAYG)", re.compile(r"payg", re.I)),
    ("🎁 تست رایگان", re.compile(r"free_?test|freetest|_test_", re.I)),
    ("🛠 سرویس سفارشی", re.compile(r"cbuild|custom_?build|custom_(?:volume|days|name|pay|confirm|option|start|mode)|_custom_", re.I)),
    ("🔁 تمدید سرویس", re.compile(r"renew", re.I)),
    (GATEWAY_CATEGORY, re.compile(r"gateway|gw_|frenzy|tronado|tonpays|unique|online_pay|check_online", re.I)),
    ("💱 پرداخت ارزی", re.compile(r"crypto", re.I)),
    ("🎟 کد تخفیف", re.compile(r"discount", re.I)),
    ("💰 کیف پول", re.compile(r"wallet|charge|topup", re.I)),
    ("📚 راهنما", re.compile(r"guide", re.I)),
    ("👥 دعوت دوستان", re.compile(r"referral|invite", re.I)),
    ("👨‍💻 پشتیبانی و نمایندگی", re.compile(r"ticket|support|agen(?:t|cy)|reseller", re.I)),
]

CATEGORY_RULES = [
    ("📦 سرویس‌های من", re.compile(r"نتیجه برای|متن برای جستجو|کیوآرکد|کانفیگی برای کپی|کانفیگ در یک فایل")),
    ("📤 تحویل سرویس", re.compile(r"WireGuard|وایرگارد")),
    ("🚀 شروع و منوی اصلی", re.compile(r"منوی اصلی|دسترسی‌های شما توسط ادمین")),
    (GATEWAY_CATEGORY, re.compile(r"فرانزی|FrenzyEx|ترونادو|Tronado|تون‌پیز|TonPays|درگاه")),
    ("💱 پرداخت ارزی", re.compile(r"ارزی|crypto|TON\b|USDT|TRX|Hash|هش")),
    ("⚡ پرداخت به‌ازای مصرف (PAYG)", re.compile(r"PAYG|payg|به‌ازای مصرف|به ازای مصرف|پرداخت پس از مصرف")),
    ("🎁 تست رایگان", re.compile(r"تست رایگان|سرویس تست|پلن تست|کانفیگ تست")),
    ("🔁 تمدید سرویس", re.compile(r"تمدید")),
    ("🛠 سرویس سفارشی", re.compile(r"سفارشی|کانفیگ خودتو|بساز سرویس")),
    ("📤 تحویل سرویس", re.compile(r"لینک ساب|لینک اشتراک|کانفیگ شما|سرویس شما (?:آماده|ساخته|ارسال)|تحویل|QR|کیو آر")),
    ("🎟 کد تخفیف", re.compile(r"کد تخفیف|تخفیف")),
    ("💳 فاکتور و پرداخت کارت", re.compile(r"فاکتور|شماره کارت|کارت به کارت|کارت‌به‌کارت|رسید|درگاه|پرداخت آنلاین")),
    ("💰 کیف پول", re.compile(r"کیف پول|شارژ|موجودی")),
    ("👥 دعوت دوستان", re.compile(r"دعوت|رفرال|زیرمجموعه")),
    ("👨‍💻 پشتیبانی و نمایندگی", re.compile(r"نماینده|نمایندگی")),
    ("👨‍💻 پشتیبانی و نمایندگی", re.compile(r"تیکت|پشتیبان")),
    ("📚 راهنما", re.compile(r"راهنما|آموزش")),
    ("👤 پروفایل", re.compile(r"پروفایل")),
    ("🔔 اعلان‌ها", re.compile(r"هشدار|اتمام حجم|انقضا|منقضی|عضویت شما|لغو شده|اعلان")),
    ("📦 سرویس‌های من", re.compile(r"سرویس|اشتراک شما|مصرف|حجم|لینک")),
    ("🛒 خرید اشتراک", re.compile(r"خرید|پلن|سفارش|پرداخت")),
]
FILE_DEFAULT_CATEGORY = {
    "handlers/gateways.py": GATEWAY_CATEGORY,
    "handlers/start.py": "🚀 شروع و منوی اصلی",
    "handlers/menu.py": "🚀 شروع و منوی اصلی",
    "handlers/plans.py": "🛒 خرید اشتراک",
    "handlers/wallet.py": "💰 کیف پول",
    "handlers/profile.py": "👤 پروفایل",
    "handlers/referral.py": "👥 دعوت دوستان",
    "handlers/ticket.py": "👨‍💻 پشتیبانی و نمایندگی",
    "alerts.py": "🔔 اعلان‌ها",
    "bot.py": "🔔 اعلان‌ها",
}
LOG_CATEGORY = "🧾 لاگ سفارش‌ها"
OTHER_CATEGORY = "🧩 سایر پیام‌ها"


def classify(template: str, rel: str, audience: str, func: str = "") -> str:
    if audience == "log":
        return LOG_CATEGORY
    if rel == "handlers/gateways.py":
        return GATEWAY_CATEGORY
    for cat, rx in FUNC_RULES:
        if func and rx.search(func):
            return cat
    for cat, rx in CATEGORY_RULES:
        if rx.search(template):
            return cat
    return FILE_DEFAULT_CATEGORY.get(rel, OTHER_CATEGORY)


def _placeholder_name(node: ast.AST, used: dict) -> str:
    base = "v"
    n = node
    if isinstance(n, ast.Call):
        n = n.func
    if isinstance(n, ast.Subscript):
        sl = n.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            base = sl.value
        else:
            n = n.value
    if base == "v":
        if isinstance(n, ast.Name):
            base = n.id
        elif isinstance(n, ast.Attribute):
            base = n.attr
    base = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "v"
    if base[0].isdigit():
        base = "v_" + base
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base}_{used[base]}"


def _template(node: ast.AST):
    """node -> template string یا None. placeholderها به‌صورت {name} هستند."""
    used: dict = {}

    def walk(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if isinstance(n, ast.JoinedStr):
            out = []
            for v in n.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append(v.value.replace("{", "{{").replace("}", "}}") if False else v.value)
                elif isinstance(v, ast.FormattedValue):
                    out.append("{" + _placeholder_name(v.value, used) + "}")
                else:
                    return None
            return "".join(out)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            a, b = walk(n.left), walk(n.right)
            # بخش پویا (مثلاً progress_bar(...) + "متن") به‌صورت {متغیر} در قالب می‌آید
            if a is None and isinstance(n.left, (ast.Call, ast.Name, ast.Attribute, ast.Subscript)) and b is not None:
                a = "{" + _placeholder_name(n.left, used) + "}"
            if b is None and isinstance(n.right, (ast.Call, ast.Name, ast.Attribute, ast.Subscript)) and a is not None:
                b = "{" + _placeholder_name(n.right, used) + "}"
            if a is None or b is None:
                return None
            return a + b
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format":
            return walk(n.func.value)
        return None

    return walk(node)


def _valid(template: str | None) -> bool:
    if not template:
        return False
    t = template.strip()
    if t.startswith(("http", "/")):
        return False
    static = _PH.sub("", t)
    if len(static.strip()) < 6 or not _PERSIAN.search(static):
        return False
    return True


def _norm(template: str) -> str:
    return re.sub(r"\s+", " ", template.strip())


def make_key(rel: str, template: str) -> str:
    return "auto_" + hashlib.sha1(f"{rel}|{_norm(template)}".encode("utf-8")).hexdigest()[:12]


def _audience(rel: str, name: str, call: ast.Call, in_mixed: bool) -> str | None:
    """'user' | 'log' | None (ادمین/نامشخص → ثبت نشود)."""
    if name in SELF_SENDS:
        return None if in_mixed else "user"
    dest = None
    if name in BOT_SENDS:
        if call.args:
            dest = call.args[0]
        else:
            for kw in call.keywords:
                if kw.arg == "chat_id":
                    dest = kw.value
    elif name in HELPERS:
        idx = HELPERS[name]
        if len(call.args) > idx:
            dest = call.args[idx]
        else:
            for kw in call.keywords:
                if kw.arg == "chat_id":
                    dest = kw.value
    if dest is None:
        return None
    src = ast.unparse(dest)
    low = src.lower()
    if "admin" in low:
        return None
    if re.search(r"channel|log_", low):
        return "log"
    if in_mixed:
        return "user" if re.search(r"user|tid|uid|telegram_id|customer|target|buyer|owner|payment\[", low) else None
    return "user"


_ADMIN_GUARD = re.compile(r"_has_admin_permission|require_admin|sub_admin_has_permission|_is_admin_user")


def _is_admin_func(func: ast.AST, src: str) -> bool:
    """هندلرهایی که فقط برای ادمین/ساب‌ادمین‌اند (گارد دسترسی دارند) — متنشان «ربات کاربر» نیست."""
    name = getattr(func, "name", "")
    if re.match(r"(?:menu_)?admin_|_admin_|^adm_", name):
        return True
    try:
        seg = ast.get_source_segment(src, func) or ""
    except Exception:
        seg = ""
    head = "\n".join(seg.splitlines()[:12])
    return bool(_ADMIN_GUARD.search(head))


def discover(root: Path) -> dict:
    registry: dict = {}
    for rel in USER_FILES + MIXED_FILES:
        path = root / rel
        if not path.exists():
            continue
        in_mixed = rel in MIXED_FILES
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("parse failed: %s", rel)
            continue
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        _src_text = path.read_text(encoding="utf-8")
        for func in funcs:
            if _is_admin_func(func, _src_text):
                continue
            fname = func.name
            # متغیرهای متنی تابع
            var_templates: dict[str, list] = {}
            for st in ast.walk(func):
                tgt = val = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    tgt, val = st.targets[0].id, st.value
                elif isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name) and isinstance(st.op, ast.Add):
                    tgt, val = st.target.id, st.value
                if tgt is None:
                    continue
                tpl = _template(val)
                if _valid(tpl):
                    var_templates.setdefault(tgt, []).append((tpl, getattr(st, "lineno", 0)))
            for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
                fn = call.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                # متن‌های کوتاهِ دارای کلید ثابت (msg_xxxx): کلید همان است و پیش‌فرض آرگومان دوم
                if name in ("get_text", "get_alert_text") and len(call.args) >= 2 \
                        and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str) \
                        and call.args[0].value.startswith("msg_"):
                    tpl = _template(call.args[1])
                    if _valid(tpl):
                        mk = call.args[0].value
                        if mk not in registry:
                            registry[mk] = {
                                "key": mk, "template": tpl.strip(), "source": rel, "line": getattr(call, "lineno", 0),
                                "vars": _PH.findall(tpl), "audience": "user", "category": classify(tpl, rel, "user", fname),
                            }
                    continue
                if name not in SELF_SENDS and name not in BOT_SENDS and name not in HELPERS:
                    continue
                aud = _audience(rel, name, call, in_mixed)
                if aud is None:
                    continue
                cands = list(call.args) + [kw.value for kw in call.keywords if kw.arg in {"text", "caption"}]
                found = []
                for c in cands:
                    tpl = _template(c)
                    if _valid(tpl):
                        found.append((tpl, getattr(c, "lineno", 0), False))
                    elif isinstance(c, ast.Name) and c.id in var_templates:
                        # متنی که تکه‌تکه (text += ...) ساخته می‌شود: هر تکه جداگانه قابل‌ویرایش است
                        found.extend((t, ln, True) for t, ln in var_templates[c.id])
                for tpl, line, partial in found:
                    key = make_key(rel, tpl)
                    if key in registry:
                        if not partial:
                            registry[key]["partial"] = False
                        continue
                    registry[key] = {
                        "key": key, "template": tpl.strip(), "source": rel, "line": line,
                        "vars": _PH.findall(tpl), "audience": aud, "partial": partial,
                        "category": classify(tpl, rel, aud, fname),
                    }
    return registry
