"""
keyboards.py
تمام کیبوردهای Inline و Reply ربات. هیچ handlerای نباید خودش InlineKeyboardMarkup
بسازد؛ همه از این فایل صدا زده می‌شوند تا تغییر ظاهر منو در یک‌جا متمرکز باشد.
"""

from aiogram.types import (
    InlineKeyboardMarkup as _RealInlineKeyboardMarkup,
    InlineKeyboardButton as _RealInlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CopyTextButton,
    WebAppInfo,
)

import database as db
import bot_info
import hashlib
from permissions import PERMISSION_LABELS
from uniquepay_cfg import enabled as _uq_enabled
import online_gateways as _og
from config import SHAHRAH_ENABLED, ONLINE_PAYMENT_MIN_AMOUNT
from panels import PANEL_TYPE_LABELS, PANEL_TYPES


# fix: callback_data محدودیت 64 بایت دارد (محدودیت Telegram Bot API).
# نام دسته/پلن توسط ادمین قابل‌ساخت است و ممکن است طولانی باشد،
# به همین دلیل هر callback_data قبل استفاده از این تابع رد می‌شود.
def _safe_callback_data(data: str) -> str:
    encoded = data.encode("utf-8")
    if len(encoded) <= 64:
        return data
    return encoded[:64].decode("utf-8", errors="ignore")


# fix: به‌جای ویرایش تک‌تک ۱۵۰+ محلی که InlineKeyboardButton ساخته می‌شود،
# یک Wrapper مرکزی می‌سازیم تا callback_data همه‌ی دکمه‌ها همیشه از این تابع
# رد شود و هیچ دکمه‌ای هرگز به‌خاطر طول callback_data توسط تلگرام رد نشود.
_BACK_EMOJI_CACHE = {"ts": 0.0, "val": None}


def _default_back_emoji_id():
    """پرتکرارترین ایموجی پریمیومی که ادمین روی دکمه‌های «بازگشت» گذاشته (کش ۶۰ ثانیه‌ای)."""
    import time
    now = time.time()
    if now - _BACK_EMOJI_CACHE["ts"] < 60:
        return _BACK_EMOJI_CACHE["val"]
    val = None
    try:
        cur = db.get_connection().cursor()
        cur.execute(
            "SELECT custom_emoji_id, COUNT(*) AS n FROM ui_button_meta "
            "WHERE custom_emoji_id IS NOT NULL AND custom_emoji_id != '' "
            "AND (callback_key = 'back' OR callback_key LIKE '%#back') "
            "GROUP BY custom_emoji_id ORDER BY n DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            val = row[0]
    except Exception:
        val = None
    _BACK_EMOJI_CACHE["ts"] = now
    _BACK_EMOJI_CACHE["val"] = val
    return val


def _strip_leading_back_emoji(text: str) -> str:
    t = str(text or "")
    for e in ("🔙", "🏠", "⬅️", "◀️", "◀", "⬅"):
        if t.startswith(e):
            return t[len(e):].lstrip() or t
    return t



# دکمه‌هایی که کلید صریح ویرایشگر دارند (مخصوصاً دکمه‌های بدون callback: لینک/کپی/وب‌اپ) تا مخفی‌کردن،
# ترتیب و چیدمان ویرایشگر روی آن‌ها هم اعمال شود: id(button) -> (screen, key, button)
_BTN_TAGS: dict = {}
# متن اصلیِ (پیش‌فرضِ کد) هر دکمه قبل از اعمال ویرایشگر: id(button) -> (orig_text, button)
_BTN_ORIG: dict = {}


def InlineKeyboardButton(*args, **kwargs):
    """Wrapper مرکزی دکمه‌های اینلاین.

    - callback_data را ایمن می‌کند
    - متن/ایموجی/استایل ویرایشگر UI را اعمال می‌کند
    - فقط styleهای معتبر primary/success/danger را نگه می‌دارد
    - اگر استایل ست نشده باشد، به‌صورت پیش‌فرض primary می‌گذارد
    - اگر نسخه‌ی aiogram روی سرور هنوز style را پشتیبانی نکند، بدون کرش fallback می‌کند
    """
    _VALID_STYLES = {"primary", "success", "danger"}
    _tag = None
    _orig_text_for_registry = kwargs.get("text")
    try:
        import ui_editor
        current = kwargs.get("text")
        _orig_text = current  # برچسب اصلی (قبل از override) برای تشخیص نقش دکمه: بازگشت یا دکمه‌ی اصلی
        cb = kwargs.get("callback_data")
        screen = kwargs.pop("ui_screen", None)
        button_key = kwargs.pop("ui_button_key", None)
        if screen and button_key:
            _tag = (screen, str(button_key))
        if screen and button_key and current:
            current = ui_editor.get_button(screen, button_key, current)
            kwargs["text"] = current  # fix: دکمه‌های لینک (بدون callback) هم متن ویرایش‌شده را بگیرند
            meta = ui_editor.get_button_meta(screen, button_key)
            if meta.get("custom_emoji_id"):
                kwargs.setdefault("icon_custom_emoji_id", meta["custom_emoji_id"])
            if meta.get("style") in _VALID_STYLES:
                kwargs["style"] = meta["style"]
        if cb is not None:
            cb = str(cb)
            kwargs["callback_data"] = _safe_callback_data(cb)
            if current:
                if screen and button_key:
                    pass  # دکمه با کلید صریح خودش ویرایش شده؛ حدس‌زدن الگو لازم نیست (جلوی تداخل بازگشت/اصلی)
                elif screen and screen in ui_editor.SCREENS:
                    pattern = ui_editor.pattern_for(screen, cb, _orig_text)
                    if pattern:
                        current = ui_editor.get_button(screen, pattern, current)
                        meta = ui_editor.get_button_meta(screen, pattern)
                        if meta.get("custom_emoji_id"):
                            kwargs.setdefault("icon_custom_emoji_id", meta["custom_emoji_id"])
                        if meta.get("style") in _VALID_STYLES:
                            kwargs["style"] = meta["style"]
                else:
                    current = ui_editor.override_button_text(cb, current)
                    meta = ui_editor.button_meta_for_callback(cb, _orig_text)
                    if meta and meta.get("custom_emoji_id"):
                        kwargs.setdefault("icon_custom_emoji_id", meta["custom_emoji_id"])
                    if meta and meta.get("style") in _VALID_STYLES:
                        kwargs["style"] = meta["style"]
                kwargs["text"] = current
        # 💎 دکمه‌های «بازگشت» که ایموجی پریمیوم ندارند، همان ایموجی پریمیومِ سایر دکمه‌های بازگشت را می‌گیرند
        try:
            if kwargs.get("callback_data") is not None and not kwargs.get("icon_custom_emoji_id"):
                _txt = str(kwargs.get("text") or "")
                if _txt and ui_editor._label_group(_txt) == "back":
                    _bid = _default_back_emoji_id()
                    if _bid:
                        kwargs["icon_custom_emoji_id"] = _bid
                        kwargs["text"] = _strip_leading_back_emoji(_txt)
        except Exception:
            pass
        # 💎 دکمه‌ای که آیکن پریمیوم دارد ولی متنش فقط یک ایموجی است: اگر ایموجی عادی در متن بماند،
        # دو ایموجی کنار هم دیده می‌شود (آیکن + ایموجی). متن را با یک جای‌خالی نامرئی عوض می‌کنیم.
        try:
            if kwargs.get("icon_custom_emoji_id"):
                import rich_registry as _rr
                if _rr.is_emoji_only(kwargs.get("text")):
                    kwargs["text"] = _rr.ICON_ONLY_TEXT
        except Exception:
            pass
        style = kwargs.get("style")
        if style not in _VALID_STYLES:
            kwargs["style"] = "primary"
    except Exception:
        kwargs.pop("ui_screen", None)
        style = kwargs.get("style")
        if style not in _VALID_STYLES:
            kwargs["style"] = "primary"

    try:
        _btn = _RealInlineKeyboardButton(*args, **kwargs)
    except TypeError:
        # Compatibility fallback for aiogram versions without `style` support.
        kwargs.pop("style", None)
        _btn = _RealInlineKeyboardButton(*args, **kwargs)
    try:
        if _tag:
            if len(_BTN_TAGS) > 4000:
                _BTN_TAGS.clear()
            _BTN_TAGS[id(_btn)] = (_tag[0], _tag[1], _btn)  # ارجاع به خود دکمه → id بازاستفاده نمی‌شود
        if len(_BTN_ORIG) > 4000:
            _BTN_ORIG.clear()
        _BTN_ORIG[id(_btn)] = (_orig_text_for_registry, _btn)
    except Exception:
        pass
    return _btn


def _resolve_button_edit(screen: str, btn) -> None:
    """🐛 فیکس: ویرایش متن/ایموجی/رنگ دکمه دقیقاً از «همین صفحه» خوانده می‌شود.

    سازنده‌ی دکمه (InlineKeyboardButton) صفحه را نمی‌داند و فقط از روی callback حدس می‌زد؛ وقتی یک callback در چند
    صفحه ثبت شده بود (مثل «🎟 ثبت کد تخفیف» = discount_cbuild در دو صفحه) هیچ ویرایشی اعمال نمی‌شد و وقتی دو
    دکمه الگوی مشترک داشتند (charge_ برای مبلغ‌ها و «مبلغ دلخواه») ویرایش یکی روی دیگری می‌نشست. اینجا که صفحه
    معلوم است، الگوی درست انتخاب و ویرایش همان الگو اعمال می‌شود. دکمه‌ی ویرایش‌شده دیگر آیکن خودکار
    (rich_registry) نمی‌گیرد تا ایموجی اشتباهِ جای دیگر روی آن نیفتد."""
    import ui_editor
    o = _BTN_ORIG.get(id(btn))
    if not o or o[1] is not btn:
        return
    orig = o[0]
    if not orig:
        return
    tag = _BTN_TAGS.get(id(btn))
    if tag and tag[2] is btn:
        if tag[0] != screen:
            return
        pattern = tag[1]
    else:
        cb = getattr(btn, "callback_data", None)
        if cb is None or ui_editor._label_group(orig) == "back":
            return
        pattern = ui_editor.pattern_for(screen, str(cb), orig)
        if not pattern:
            return
    new = ui_editor.get_button(screen, pattern, orig)
    meta = ui_editor.get_button_meta(screen, pattern)
    if new != orig:
        if getattr(btn, "text", None) != new:
            btn.text = new
        try:
            import rich_registry as _rr
            _rr.mark_no_auto_icon(btn)
        except Exception:
            pass
    elif getattr(btn, "text", None) != orig and not (tag and tag[2] is btn):
        # صفحه برای این دکمه ویرایشی ندارد؛ نتیجه‌ی حدسِ سراسریِ سازنده (ویرایشِ صفحه‌ی دیگر) را برگردان
        btn.text = orig
    if meta.get("custom_emoji_id") and "icon_custom_emoji_id" in type(btn).model_fields:
        btn.icon_custom_emoji_id = meta["custom_emoji_id"]
    if meta.get("style") in {"primary", "success", "danger"}:
        btn.style = meta["style"]



def _pack_buttons(buttons, columns):
    """چیدمان دکمه‌ها بدون نیاز به تقسیم‌پذیری کامل.

    اگر تعداد دکمه‌ها فرد باشد و columns=2 باشد، مثلاً 7 دکمه
    به شکل 2+2+2+1 چیده می‌شوند. آخرین ردیف می‌تواند کمتر از columns
    دکمه داشته باشد و هرگز به خاطر باقی‌مانده، کل چیدمان رد نمی‌شود.
    """
    try:
        columns = max(1, int(columns or 1))
    except (TypeError, ValueError):
        columns = 1
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def InlineKeyboardMarkup(*args, **kwargs):
    """سازنده‌ی مرکزی کیبورد.

    ui_screen یک پارامتر داخلی پروژه است و به Telegram ارسال نمی‌شود. وقتی
    صفحه مشخص باشد، تنظیمات ویرایشگر دقیقاً روی همان صفحه اعمال می‌شود؛ دیگر
    از حدس‌زدن صفحه با callbackهای مشترکی مثل «back» استفاده نمی‌کنیم. همین
    تغییر جلوی باگ جابه‌جایی «بازگشت» به ابتدای منوها را می‌گیرد.
    """
    import ui_editor

    screen = kwargs.pop("ui_screen", None)
    markup = _RealInlineKeyboardMarkup(*args, **kwargs)
    try:
        if screen and screen in ui_editor.SCREENS:
            for _row in markup.inline_keyboard:
                for _b in _row:
                    _resolve_button_edit(screen, _b)
    except Exception:
        import traceback; traceback.print_exc()
    # حتی کیبوردهایی که هنوز ui_screen ندارند هم باید قانون عمومی «بازگشت پایین»
    # را رعایت کنند. در صورت نبود Screen فقط همین قانون عمومی اعمال می‌شود.
    try:
        sc = ui_editor.SCREENS.get(screen, {"buttons": []}) if screen else {"buttons": []}
        lay = ui_editor.get_layout(screen)
        order = ui_editor.get_order(screen)
        pos = {cb: i for i, cb in enumerate(order)}

        _pats = [p for p, _l in sc.get("buttons", [])]

        def _tagged(b):
            """کلید صریحِ دکمه (اگر برای همین صفحه ثبت شده و در رجیستری هست)."""
            t = _BTN_TAGS.get(id(b))
            if t and t[2] is b and t[0] == screen and t[1] in _pats:
                return t[1]
            return None

        # دکمه‌های مخفی را حذف می‌کنیم.
        filtered = []
        for row in markup.inline_keyboard:
            new_row = []
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                matched = _tagged(btn)
                if matched is None and cb is not None:
                    for pattern, _label in sc.get("buttons", []):
                        if ui_editor._pattern_matches(pattern, str(cb)):
                            matched = pattern
                            break
                if matched and ui_editor.get_button_meta(screen, matched).get("hidden"):
                    continue
                new_row.append(btn)
            if new_row:
                filtered.append(new_row)

        # فقط دکمه‌هایی که در رجیستری این صفحه هستند در چیدمان ویرایشگر شرکت می‌کنند.
        managed = []
        other = []
        for row in filtered:
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                # fix: اینجا قبلاً به‌جای فقط رشته‌ی callback pattern، کل تاپل
                # (pattern, label) به _pattern_matches پاس داده می‌شد، که
                # همیشه AttributeError می‌داد («'tuple' object has no
                # attribute 'endswith'»). چون این خطا داخل try/except بیرونی
                # بی‌صدا قورت داده می‌شد، عملاً هیچ‌وقت هیچ‌کدوم از قابلیت‌های
                # ویرایشگر روی کیبورد واقعی کاربر اعمال نمی‌شد: نه چیدمان
                # ۲/۳/۴ دکمه در ردیف، نه مخفی‌کردن دکمه، نه قانون «بازگشت
                # همیشه پایین‌ترین ردیف». همین یک خط باگ اصلی شلختگی و
                # کارنکردن ویرایشگر متن/دکمه روی صفحات واقعی بوده.
                if _tagged(btn) is not None or (cb is not None and any(ui_editor._pattern_matches(pattern, str(cb)) for pattern, _label in sc.get("buttons", []))):
                    managed.append(btn)
                else:
                    other.append([btn])

        # ترتیب با پشتیبانی از الگوهای تکراری (مثل جداکننده noop که چندبار می‌آید):
        # n‌امین دکمه‌ی یک الگو، n‌امین جایگاه همان الگو در order را می‌گیرد.
        _slots = {}
        for _i, _p in enumerate(order):
            _slots.setdefault(_p, []).append(_i)
        _seen = {}
        def _managed_pos(b):
            cbv = str(getattr(b, "callback_data", ""))
            _tg = _tagged(b)
            for pattern, _ in sc.get("buttons", []):
                if (_tg is not None and pattern == _tg) or (_tg is None and ui_editor._pattern_matches(pattern, cbv)):
                    idxs = _slots.get(pattern)
                    if not idxs:
                        return 999
                    n = _seen.get(pattern, 0)
                    _seen[pattern] = n + 1
                    return idxs[min(n, len(idxs) - 1)]
            return 999
        _keyed = [(_managed_pos(b), n, b) for n, b in enumerate(managed)]
        # دکمه‌های سفارشی ادمین هم در همین ترتیب (و چیدمان ستونی) شرکت می‌کنند تا بین بقیه جابه‌جا شوند.
        if screen and screen != "start":
            for _cn, cbtn in enumerate(ui_editor.get_custom_buttons(screen)):
                _cb_btn = _custom_inline_button(cbtn)
                if _cb_btn is None:
                    continue
                _ck = ui_editor.custom_button_key(cbtn["id"])
                _keyed.append((order.index(_ck) if _ck in order else 999, 10000 + _cn, _cb_btn))
        _keyed.sort(key=lambda t: (t[0], t[1]))
        managed = [t[2] for t in _keyed]
        cols = lay["columns"] if lay["mode"] == "inline" else 1
        widths = lay.get("row_widths") or []
        if widths:
            new_rows=[]; _pos=0
            for _width in widths:
                if _pos >= len(managed): break
                new_rows.append(managed[_pos:_pos+_width]); _pos += _width
            if _pos < len(managed): new_rows.extend(_pack_buttons(managed[_pos:], cols))
        else:
            new_rows = _pack_buttons(managed, cols)
        if other:
            if screen == "plans":
                # میانبر VIP: پوشه‌ها و پلن‌ها (دکمه‌های پویا) بالا، بعد سرویس سفارشی، بعد PAYG
                new_rows = other + new_rows
            else:
                new_rows.extend(other)

        # «بازگشت/انصراف» همیشه پایین‌ترین ردیف باشد؛ حتی اگر در دیتابیس قبلاً
        # ترتیب اشتباه ذخیره شده باشد. این قانون عمداً عمومی است تا هیچ صفحه‌ای
        # نتواند دکمه برگشت را به ابتدای منو ببرد.
        back_rows = []
        normal_rows = []
        for row in new_rows:
            is_back = False
            for btn in row:
                cb = str(getattr(btn, "callback_data", "") or "")
                txt = str(getattr(btn, "text", "") or "")
                if cb == "back" or cb == "admin_back" or "بازگشت" in txt or "انصراف" in txt:
                    is_back = True
                    break
            (back_rows if is_back else normal_rows).append(row)
        # 🆕 نمایش کامل نام دکمه‌ها: تلگرام متن دکمه‌ی ردیف‌های چنددکمه‌ای را اگر بلند باشد با «…» می‌بُرد.
        # اگر در یک ردیف متن حتی یک دکمه از ظرفیت آن ردیف بلندتر باشد، دکمه‌های آن ردیف هرکدام
        # در ردیف جدا (تمام‌عرض) قرار می‌گیرند تا اسم کامل دیده شود.
        def _fit(rows):
            out = []
            for row in rows:
                n = len(row)
                if n > 1:
                    cap = {2: 17, 3: 10}.get(n, 7)
                    if any(len(str(getattr(b, "text", "") or "")) > cap for b in row):
                        out.extend([[b] for b in row])
                        continue
                out.append(row)
            return out
        markup.inline_keyboard = _fit(normal_rows) + _fit(back_rows)
    except Exception:
        import traceback; traceback.print_exc()
        # ویرایشگر UI نباید هیچ‌وقت باعث خراب‌شدن کیبورد اصلی ربات شود.
        return markup
    return markup


def _custom_inline_button(cbtn: dict):
    """ساخت دکمه‌ی اینلاین از یک دکمه‌ی سفارشی ادمین (None اگر نامعتبر باشد)."""
    action_type = cbtn.get("action_type")
    action_value = cbtn.get("action_value") or ""
    style = cbtn.get("style") or "primary"
    try:
        import ui_editor
        # مخفی‌کردن/رنگ از «ویرایش دکمه‌ها» (مثل همه‌ی دکمه‌ها)
        _ck = ui_editor.custom_button_key(cbtn["id"])
        _meta = ui_editor.get_button_meta(cbtn["screen_key"], _ck)
        if _meta.get("hidden"):
            return None
        if _meta.get("style") in ("primary", "success", "danger"):
            style = _meta["style"]
    except Exception:
        pass
    try:
        if action_type == "url":
            return InlineKeyboardButton(text=cbtn["text"], url=action_value, style=style)
        if action_type == "webapp":
            if not is_valid_webapp_url(action_value):
                return None  # آدرس نامعتبر کل پیام را از طرف تلگرام رد می‌کند
            return _RealInlineKeyboardButton(text=cbtn["text"], web_app=WebAppInfo(url=action_value), style=style)
        return InlineKeyboardButton(text=cbtn["text"], callback_data=action_value, style=style)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# عضویت اجباری
# ---------------------------------------------------------------------------
def join_channels_keyboard(channels):
    buttons = [[InlineKeyboardButton(text=f"📢 {ch['name']}", url=ch["url"], style="primary")] for ch in channels]
    buttons.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="check_join", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# منوی پایین صفحه (Reply Keyboard) — همیشه در دسترس کاربر
# ---------------------------------------------------------------------------
def main_reply_keyboard(user_id=None):
    # منوی پایین مشتری هم از ویرایشگر UI قابل تغییر است.
    # پیش‌فرض همیشه یک دکمه در هر ردیف است؛ ادمین بعداً می‌تواند چیدمان را تغییر دهد.
    import ui_editor
    items = [
        ("plans", "🛒 خرید اشتراک", "success"),
        ("free_test", "🎁 تست رایگان", "success"),
        ("services", "📱 سرویس‌های من", "primary"),
        ("renew", "🔁 تمدید سرویس", "success"),
        ("wallet", "💰 کیف پول", "primary"),
        ("referral", "👥 دعوت دوستان", "primary"),
        ("profile", "👤 پروفایل من", "primary"),
        ("support", "👨‍💻 پشتیبانی", "primary"),
        ("guides", "راهنما", "primary"),
        ("agency", "🤝 درخواست نمایندگی", "danger"),
    ]
    try:
        if not db.referral_enabled():
            items = [it for it in items if it[0] != "referral"]  # رفرال خاموش: بدون «دعوت دوستان»
    except Exception:
        pass
    try:
        if not db.is_renewal_enabled():
            items = [it for it in items if it[0] != "renew"]  # سوئیچ سراسری تمدید خاموش: بدون دکمه‌ی «تمدید»
    except Exception:
        pass
    if user_id is not None:
        try:
            if db.get_agent(user_id):
                items = [it for it in items if it[0] != "referral"]  # نماینده: بدون «دعوت دوستان»
                items.append(("agent_manage", "🤝 مدیریت نمایندگی", "success"))
        except Exception:
            pass
    _valid_styles = {"primary", "success", "danger"}
    visible = []
    for key, label, style in items:
        _meta = ui_editor.get_button_meta("main_reply", key)
        if _meta.get("hidden"):
            continue
        if _meta.get("style") in _valid_styles:
            style = _meta["style"]
        visible.append((key, ui_editor.get_button("main_reply", key, label), style, _meta.get("custom_emoji_id")))
    # دکمه‌های دلخواهی که ادمین به «منوی اصلی» اضافه کرده (Reply Keyboard فقط متن/WebApp می‌پذیرد؛
    # نوع callback/url با فشردن دکمه توسط هندلر menu.py اجرا می‌شود).
    _cweb = {}
    try:
        for _cb in ui_editor.get_custom_buttons("main_reply"):
            _ck = ui_editor.custom_button_key(_cb["id"])
            _cmeta = ui_editor.get_button_meta(_cb["screen_key"], _ck)
            if _cmeta.get("hidden"):
                continue
            _cs = _cmeta["style"] if _cmeta.get("style") in _valid_styles else (_cb.get("style") if _cb.get("style") in _valid_styles else "primary")
            if _cb.get("action_type") == "webapp":
                if not is_valid_webapp_url(_cb.get("action_value") or ""):
                    continue
                _cweb[_ck] = _cb["action_value"]
            visible.append((_ck, _cb["text"], _cs, _cb.get("custom_emoji_id")))
    except Exception:
        pass
    lay = ui_editor.get_layout("main_reply")
    cols = lay["columns"] if lay["mode"] == "inline" else 1
    # منوی پایین ذاتاً reply است؛ حالت «inline» در ادیتور یعنی چندتایی در هر ردیف.
    if lay["order"]:
        order = ui_editor.get_order("main_reply")
        pos = {k: i for i, k in enumerate(order)}
        visible.sort(key=lambda x: pos.get(x[0], 999))
    def _rk(text, style, emoji, _key=None):
        kw = {"text": text, "style": style}
        if _key in _cweb:
            kw["web_app"] = WebAppInfo(url=_cweb[_key])
        if emoji:
            kw["icon_custom_emoji_id"] = emoji
        try:
            return KeyboardButton(**kw)
        except Exception:
            kw.pop("icon_custom_emoji_id", None)
            return KeyboardButton(**kw)
    rows = []
    widths = lay.get("row_widths") or []
    if widths:
        _pos=0
        for _width in widths:
            if _pos >= len(visible): break
            chunk=visible[_pos:_pos+_width]; _pos += _width
            rows.append([_rk(text, style, emoji, _key) for _key, text, style, emoji in chunk])
        if _pos < len(visible):
            rows.extend([[_rk(text, style, emoji, _key) for _key, text, style, emoji in chunk] for chunk in _pack_buttons(visible[_pos:], cols)])
    else:
        for chunk in _pack_buttons(visible, cols):
            rows.append([_rk(text, style, emoji, _key) for _key, text, style, emoji in chunk])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False, one_time_keyboard=False)


# ─────────────────────────────────────────────────────────────────────────────
# دسته‌بندی‌های ۶گانه Admin Panel
# ─────────────────────────────────────────────────────────────────────────────
# (text, style) — style برای KeyboardButton همانند Bot API 9.4
_ADMIN_CAT_BUTTONS = [
    ("👥 کاربران و سفارشات", "primary"),
    ("💰 فروش و مالی",       "success"),
    ("🛠 سرویس و پنل",       "primary"),
    ("📢 ارتباط و تبلیغات",  "success"),
    ("🎨 ظاهر و محتوا",      "primary"),
    ("⚙️ سیستم و آمار",      "danger"),
]

_ADMIN_CAT_CALLBACKS = {
    "👥 کاربران و سفارشات": "admin_cat_users",
    "💰 فروش و مالی": "admin_cat_finance",
    "🛠 سرویس و پنل": "admin_cat_service",
    "📢 ارتباط و تبلیغات": "admin_cat_comm",
    "🎨 ظاهر و محتوا": "admin_cat_appearance",
    "⚙️ سیستم و آمار": "admin_cat_system",
}


def _admin_menu_allowed(perm: str, permissions: set[str] | None, is_main_admin: bool) -> bool:
    return is_main_admin or permissions is None or perm in permissions


def admin_reply_keyboard(orders_enabled: bool | None = None, permissions: set[str] | None = None, is_main_admin: bool = True):
    """Reply Keyboard ادمین — ۶ دکمه دسته‌بندی رنگی، ۲ تا در هر ردیف."""
    buttons = _ADMIN_CAT_BUTTONS  # list of (text, style)
    rows = []
    for i in range(0, len(buttons), 2):
        try:
            t0, s0 = buttons[i]
        except (TypeError, ValueError):
            t0, s0 = buttons[i], "primary"
        row = [KeyboardButton(text=t0, style=s0)]
        if i + 1 < len(buttons):
            try:
                t1, s1 = buttons[i + 1]
            except (TypeError, ValueError):
                t1, s1 = buttons[i + 1], "primary"
            row.append(KeyboardButton(text=t1, style=s1))
        rows.append(row)
    if not rows:
        rows = [[KeyboardButton(text="⛔ بدون دسترسی", style="danger")]]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=False,
        one_time_keyboard=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inline Keyboardهای هر دسته
# ─────────────────────────────────────────────────────────────────────────────

def admin_cat_users_keyboard(orders_enabled: bool | None = None, permissions: set[str] | None = None, is_main_admin: bool = True) -> InlineKeyboardMarkup:
    """👥 کاربران و سفارشات"""
    if orders_enabled is None:
        try:
            orders_enabled = db.is_orders_enabled()
        except Exception:
            orders_enabled = True
    order_btn_text = "🔴 خاموش کردن سفارشات" if orders_enabled else "🟢 روشن کردن سفارشات"
    order_btn_cb = "admin_orders_off" if orders_enabled else "admin_orders_on"
    rows = [
        [InlineKeyboardButton(text="📥 صف درخواست‌ها", callback_data="admin_request_queue", style="success"),
         InlineKeyboardButton(text="👥 لیست کاربران", callback_data="admin_userlist", style="primary")],
        [InlineKeyboardButton(text="🔍 جستجوی کاربر", callback_data="admin_search", style="primary"),
         InlineKeyboardButton(text="🔎 جستجوی کانفیگ", callback_data="admin_config_search", style="primary")],
        [InlineKeyboardButton(text="🎫 تیکت‌ها", callback_data="admin_tickets", style="success"),
         InlineKeyboardButton(text="🎟 مدیریت تخفیف", callback_data="admin_discount", style="primary")],
        [InlineKeyboardButton(text="🤝 نمایندگی", callback_data="admin_agency", style="primary"),
         InlineKeyboardButton(text="🤝 مدیریت دعوت‌ها", callback_data="admin_referrals", style="primary")],
        [InlineKeyboardButton(text=order_btn_text, callback_data=order_btn_cb,
                              style="danger" if orders_enabled else "success")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cat_finance_keyboard(permissions: set[str] | None = None, is_main_admin: bool = True) -> InlineKeyboardMarkup:
    """💰 فروش و مالی"""
    rows = [
        [InlineKeyboardButton(text="💳 روش‌های پرداخت", callback_data="admin_payment_methods", style="primary"),
         InlineKeyboardButton(text="🎁 تنظیمات رفرال", callback_data="admin_referral_settings", style="success")],
        [InlineKeyboardButton(text="📊 گزارش مالی", callback_data="admin_stats", style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cat_service_keyboard(permissions: set[str] | None = None, is_main_admin: bool = True) -> InlineKeyboardMarkup:
    """🛠 سرویس و پنل"""
    rows = [
        [InlineKeyboardButton(text="🗂 دسته‌بندی‌های VIP", callback_data="admin_vip_categories", style="primary"),
         InlineKeyboardButton(text="🖥 پنل‌های VPN", callback_data="admin_vpn_panels", style="primary")],
        [InlineKeyboardButton(text="🛠 ایجاد سرویس", callback_data="admin_create_service", style="success"),
         InlineKeyboardButton(text="🎁 تنظیم تست رایگان", callback_data="admin_free_test_settings", style="primary")],
        [InlineKeyboardButton(text="🔁 تنظیمات تمدید", callback_data="admin_renewal_settings", style="primary"),
         InlineKeyboardButton(text="🧩 سرویس سفارشی", callback_data="admin_custom_build_settings", style="primary")],
        [InlineKeyboardButton(text="⚡ تنظیمات PayG", callback_data="admin_payg", style="primary"),
         InlineKeyboardButton(text="⚖️ مصرف منصفانه", callback_data="fup_open", style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cat_comm_keyboard(permissions: set[str] | None = None, is_main_admin: bool = True) -> InlineKeyboardMarkup:
    """📢 ارتباط و تبلیغات"""
    rows = [
        [InlineKeyboardButton(text="📢 پیام همگانی", callback_data="admin_broadcast", style="primary"),
         InlineKeyboardButton(text="📣 لینک‌های تبلیغاتی", callback_data="admin_campaigns", style="success")],
        [InlineKeyboardButton(text="📚 مدیریت راهنما", callback_data="admin_guides", style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cat_appearance_keyboard(permissions: set[str] | None = None, is_main_admin: bool = True) -> InlineKeyboardMarkup:
    """🎨 ظاهر و محتوا"""
    rows = [
        [InlineKeyboardButton(text="✏️ ویرایش متن و دکمه‌ها", callback_data="admin_text_editor", style="primary"),
         InlineKeyboardButton(text="🎬 استیکرهای منو", callback_data="admin_stickers", style="primary")],
        [InlineKeyboardButton(text="ℹ️ اطلاعات ربات", callback_data="admin_botinfo", style="primary"),
         InlineKeyboardButton(text="🖥 Mini App ادمین", callback_data="admin_miniapp_open", style="success")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cat_system_keyboard(permissions: set[str] | None = None, is_main_admin: bool = True) -> InlineKeyboardMarkup:
    """⚙️ سیستم و آمار"""
    rows = [
        [InlineKeyboardButton(text="📊 آمار", callback_data="admin_stats", style="primary"),
         InlineKeyboardButton(text="🩺 سلامت ربات", callback_data="admin_health", style="primary")],
        [InlineKeyboardButton(text="🚀 وضعیت کش", callback_data="admin_cache_status", style="primary"),
         InlineKeyboardButton(text="💾 بکاپ", callback_data="admin_backup", style="primary")],
        [InlineKeyboardButton(text="🦖 لاگ خطاها", callback_data="errlog", style="danger"),
         InlineKeyboardButton(text="📋 گزارش فعالیت", callback_data="admin_audit_logs", style="primary")],
        [InlineKeyboardButton(text="👮 مدیریت ادمین‌ها", callback_data="admin_manage_admins", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# حفظ backward compatibility — ادمین‌های قدیمی که هنوز از admin_panel_menu استفاده می‌کنند
def _admin_menu_items(orders_enabled: bool | None = None):
    """Backward-compatible stub — فقط برای code paths قدیمی."""
    return []


# ---------------------------------------------------------------------------
# ℹ️ اطلاعات ربات — منوی ادمین برای ویرایش هر یک از فیلدهای bot_info + مدیریت کانال‌ها
# ---------------------------------------------------------------------------
def admin_botinfo_menu():
    labels = bot_info.labels()
    # «پیشوند نام کانفیگ» جدا از این لیست و داخل دکمه‌ی «🏷 نامگذاری سرویس» (پیشوند / پسوند) مدیریت می‌شود.
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"botinfo_edit_{key}", style="primary")]
        for key, label in labels.items() if key != "config_name_prefix"
    ]
    rows.append([InlineKeyboardButton(text="🏷 نامگذاری سرویس", callback_data="botinfo_cfgname", style="primary")])
    rows.append([InlineKeyboardButton(text=f"🎭 ری‌اکشن روی /start: {bot_info.start_reaction_label()}", callback_data="botinfo_reaction", style="primary")])
    rows.append([InlineKeyboardButton(text="📢 مدیریت کانال‌های اجباری", callback_data="botinfo_channels", style="primary")])
    rows.append([InlineKeyboardButton(text="🔔 تنظیمات اعلان‌ها", callback_data="ntf_open", style="primary")])
    rows.append([InlineKeyboardButton(text="📊 گزارشات پیشرفته", callback_data="adv_open", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cfgname_root_menu():
    """منوی «🏷 نامگذاری سرویس»: پیشوند / پسوند."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷 پیشوند", callback_data="botinfo_pfx", style="primary")],
        [InlineKeyboardButton(text="🔢 پسوند", callback_data="botinfo_sfx", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ])


def admin_cfgname_menu(mode: str, prefix: str):
    """منوی «🏷 پیشوند»: نامگذاری پیش‌فرض / نامگذاری توسط کاربر (دوحالته)، و برای حالت کاربر: تنظیم «انتخاب خودکار»."""
    default_on = mode != "user"
    rows = [
        [InlineKeyboardButton(
            text=("🟢 " if default_on else "⚪️ ") + f"نامگذاری پیش‌فرض ({prefix})",
            callback_data="botinfo_cfgname_default", style=("success" if default_on else "primary"))],
        [InlineKeyboardButton(
            text=("🟢 " if not default_on else "⚪️ ") + "نامگذاری توسط کاربر",
            callback_data="botinfo_cfgname_user", style=("success" if not default_on else "primary"))],
        [InlineKeyboardButton(text="✏️ تغییر پیشوند پیش‌فرض", callback_data="botinfo_edit_config_name_prefix", style="primary")],
    ]
    if not default_on:
        _am = bot_info.get_auto_name_mode()
        rows.append([InlineKeyboardButton(
            text=("🟢 " if _am == "username" else "⚪️ ") + "انتخاب خودکار: ساخت از یوزرنیم کاربر",
            callback_data="botinfo_autoname_username", style=("success" if _am == "username" else "primary"))])
        rows.append([InlineKeyboardButton(
            text=("🟢 " if _am == "prefix" else "⚪️ ") + f"انتخاب خودکار: با پیشوند پیش‌فرض ({prefix})",
            callback_data="botinfo_autoname_prefix", style=("success" if _am == "prefix" else "primary"))])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_cfgname", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cfgsuffix_menu():
    """منوی «🔢 پسوند»: تعداد کاراکتر + نوع (عدد / حروف / ترکیب)."""
    _kind = bot_info.get_suffix_kind()
    rows = [
        [InlineKeyboardButton(text=f"🔢 تعداد کاراکتر پسوند: {bot_info.get_suffix_len()}  (تغییر)",
                              callback_data="botinfo_sfx_len", style="primary")],
        [InlineKeyboardButton(text=("🟢 " if _kind == k else "⚪️ ") + bot_info.SUFFIX_KIND_LABELS[k],
                              callback_data=f"botinfo_sfx_kind_{k}", style=("success" if _kind == k else "primary"))
         for k in bot_info.SUFFIX_KINDS],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_cfgname", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def config_name_reply_keyboard():
    """دکمه‌های مرحله‌ی انتخاب نام سرویس (Inline تا Reply Keyboard اصلی عوض نشود)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=db.get_text_override("config_name_auto", "🤖 انتخاب خودکار نام"), callback_data="config_name_auto", style="success")],
        [InlineKeyboardButton(text=db.get_text_override("config_name_back", "🔙 بازگشت به مرحله قبل"), callback_data="config_name_back", style="primary")],
    ])


def admin_botinfo_wallet_menu(settings: dict):
    amounts_text = " / ".join(f"{a:,}" for a in settings["quick_amounts"])
    rows = [
        [InlineKeyboardButton(text=f"📉 تغییر حداقل شارژ: {settings['min_topup']:,} تومان", callback_data="wset_edit_min_topup", style="primary")],
        [InlineKeyboardButton(text=f"📈 تغییر حداکثر شارژ: {settings['max_topup']:,} تومان", callback_data="wset_edit_max_topup", style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های مبلغ پیشنهادی: {amounts_text}", callback_data="wset_edit_quick_amounts", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_botinfo_referral_menu(settings: dict):
    min_enabled = settings["min_volume_enabled"]
    paid_required = settings["paid_purchase_required"]
    leave_lock = settings.get("channel_leave_lock_enabled", True)
    penalty_on = settings.get("channel_leave_penalty_enabled", False)
    _ref_on = settings.get("enabled", True)
    rows = [
        [InlineKeyboardButton(
            text=("🟢 سیستم رفرال: روشن (برای خاموش‌کردن بزنید)" if _ref_on else "🔴 سیستم رفرال: خاموش (برای روشن‌کردن بزنید)"),
            callback_data="refset_toggle_enabled", style="success" if _ref_on else "danger"
        )],
        [InlineKeyboardButton(
            text=("🟢 شرط حداقل حجم روشن" if min_enabled else "🔴 شرط حداقل حجم خاموش"),
            callback_data="refset_toggle_min_volume", style="primary"
        )],
        [InlineKeyboardButton(
            text=("🟢 شرط خرید پولی روشن" if paid_required else "🔴 شرط خرید پولی خاموش"),
            callback_data="refset_toggle_paid_purchase", style="primary"
        )],
        [InlineKeyboardButton(text=f"📦 تغییر حداقل حجم: {settings['min_volume_gb']} گیگ", callback_data="refset_edit_min_volume", style="primary")],
        [InlineKeyboardButton(text=f"💰 تغییر مبلغ رفرال: {settings['reward_amount']:,} تومان", callback_data="refset_edit_reward", style="primary")],
        [InlineKeyboardButton(
            text=("🟢 قفل‌شدن پاداش با لفت‌دادن از کانال: روشن" if leave_lock else "🔴 قفل‌شدن پاداش با لفت‌دادن از کانال: خاموش"),
            callback_data="refset_toggle_channel_leave_lock", style="primary"
        )],
        [InlineKeyboardButton(
            text=("🚨 سیستم جریمه: روشن" if penalty_on else "⚪️ سیستم جریمه: خاموش"),
            callback_data="refset_toggle_channel_leave_penalty", style="danger" if penalty_on else "primary",
        )],
        [InlineKeyboardButton(text=f"💸 مبلغ جریمه‌ی کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان", callback_data="refset_edit_penalty_wallet", style="primary")],
        [InlineKeyboardButton(text=f"📉 گیگ جریمه از سرویس: {settings.get('penalty_service_gb', 0)} گیگ", callback_data="refset_edit_penalty_gb", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_botinfo_field_keyboard(key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ])


def admin_botinfo_channels_menu(channels: list):
    rows = []
    for ch in channels:
        name = ch.get("name") or str(ch.get("id"))
        rows.append([
            InlineKeyboardButton(text=f"❌ حذف «{name}»", callback_data=f"botinfo_channel_del_{ch.get('id')}", style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن کانال جدید", callback_data="botinfo_channel_add", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def admin_referral_settings_menu(settings: dict, back: str = "admin_back"):
    """کیبورد مستقل تنظیمات رفرال — جدا از اطلاعات ربات"""
    min_enabled = settings["min_volume_enabled"]
    paid_required = settings["paid_purchase_required"]
    leave_lock = settings.get("channel_leave_lock_enabled", True)
    penalty_on = settings.get("channel_leave_penalty_enabled", False)
    _ref_on = settings.get("enabled", True)
    rows = [
        [InlineKeyboardButton(
            text=("🟢 سیستم رفرال: روشن (برای خاموش‌کردن بزنید)" if _ref_on else "🔴 سیستم رفرال: خاموش (برای روشن‌کردن بزنید)"),
            callback_data="refset_toggle_enabled", style="success" if _ref_on else "danger"
        )],
        [InlineKeyboardButton(
            text=("🟢 شرط حداقل حجم: روشن" if min_enabled else "🔴 شرط حداقل حجم: خاموش"),
            callback_data="refset_toggle_min_volume", style="success" if min_enabled else "danger"
        )],
        [InlineKeyboardButton(
            text=("🟢 شرط خرید پولی: روشن" if paid_required else "🔴 شرط خرید پولی: خاموش"),
            callback_data="refset_toggle_paid_purchase", style="success" if paid_required else "danger"
        )],
        [InlineKeyboardButton(text=f"📦 حداقل حجم: {settings['min_volume_gb']} گیگ", callback_data="refset_edit_min_volume", style="primary")],
        [InlineKeyboardButton(text=f"💰 مبلغ رفرال: {settings['reward_amount']:,} تومان", callback_data="refset_edit_reward", style="primary")],
        [InlineKeyboardButton(
            text=("🟢 قفل پاداش با لفت: روشن" if leave_lock else "🔴 قفل پاداش با لفت: خاموش"),
            callback_data="refset_toggle_channel_leave_lock", style="success" if leave_lock else "danger"
        )],
        [InlineKeyboardButton(
            text=("🚨 سیستم جریمه: روشن" if penalty_on else "⚪️ سیستم جریمه: خاموش"),
            callback_data="refset_toggle_channel_leave_penalty", style="danger" if penalty_on else "primary",
        )],
        [InlineKeyboardButton(text=f"💸 جریمه کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان", callback_data="refset_edit_penalty_wallet", style="primary")],
        [InlineKeyboardButton(text=f"📉 جریمه سرویس: {settings.get('penalty_service_gb', 0)} گیگ", callback_data="refset_edit_penalty_gb", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=back, style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_wallet_settings_menu(settings: dict):
    """کیبورد مستقل تنظیمات کیف‌پول — جدا از اطلاعات ربات"""
    amounts_text = " / ".join(f"{a:,}" for a in settings["quick_amounts"])
    rows = [
        [InlineKeyboardButton(text=f"📉 حداقل شارژ: {settings['min_topup']:,} تومان", callback_data="wset_edit_min_topup", style="primary")],
        [InlineKeyboardButton(text=f"📈 حداکثر شارژ: {settings['max_topup']:,} تومان", callback_data="wset_edit_max_topup", style="primary")],
        [InlineKeyboardButton(text=f"🔘 مبالغ پیشنهادی: {amounts_text}", callback_data="wset_edit_quick_amounts", style="primary")],
        [InlineKeyboardButton(text=f"🎁 درصد هدیه‌ی شارژ: {float(settings.get('gift_percent', 0) or 0):g}٪", callback_data="wset_edit_gift_percent", style="primary")],
        [InlineKeyboardButton(text=f"🪫 هشدار موجودی کم: {('غیرفعال' if not int(settings.get('low_balance') or 0) else format(int(settings.get('low_balance')), ',') + ' تومان')}", callback_data="wset_edit_low_balance", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_payment_methods_menu(card_enabled: bool | None = None,
                              wallet_enabled: bool | None = None,
                              uniquepay_enabled: bool | None = None,
                              crypto_enabled: bool | None = None,
                              payg_enabled: bool | None = None):
    """کیبورد روش‌های پرداخت با نشانگر 🟢/🔴 فعال/غیرفعال.

    وضعیت PAYG از جدول settings خوانده می‌شود، نه pm_paygo؛ چون کل منوی ادمین
    و مسیرهای کاربر بر همین جدول سوار هستند.
    """
    try:
        import database as _db, json as _json
        if card_enabled is None:
            card_enabled = bool((_json.loads(_db.get_setting('pm_card', '{}') or '{}')).get('enabled', True))
        if wallet_enabled is None:
            wallet_enabled = bool((_json.loads(_db.get_setting('pm_wallet', '{}') or '{}')).get('enabled', True))
        if uniquepay_enabled is None:
            up = _json.loads(_db.get_setting('pm_uniquepay', '{}') or '{}')
            uniquepay_enabled = bool(up.get('enabled', False) and (up.get('business_token') or '').strip())
        if crypto_enabled is None:
            try:
                cryptos = _json.loads(_db.get_setting('pm_cryptos', '[]') or '[]')
                crypto_enabled = _db.get_setting('pm_crypto_master', '1') != '0' and any(bool(c.get('enabled', False)) for c in cryptos)
            except Exception:
                crypto_enabled = False
        if payg_enabled is None:
            payg_enabled = bool(_db.get_payg_settings().get('enabled'))
    except Exception:
        card_enabled = wallet_enabled = True
        crypto_enabled = False
        uniquepay_enabled = payg_enabled = False

    def _st(v): return "🟢" if v else "🔴"

    rows = [
        [InlineKeyboardButton(
            text=f"{_st(card_enabled)} 💳 کارت‌به‌کارت",
            callback_data="paymt_card", style="primary"),
         InlineKeyboardButton(
            text=f"{_st(wallet_enabled)} 💰 کیف‌پول کاربران",
            callback_data="paymt_wallet", style="primary")],
        [InlineKeyboardButton(
            text=f"{_st(uniquepay_enabled)} 🌐 UniquePay (آنلاین)",
            callback_data="paymt_uniquepay", style="primary"),
         InlineKeyboardButton(
            text=f"{_st(crypto_enabled)} 💎 ارز دیجیتال",
            callback_data="paymt_crypto", style="primary")],
        [InlineKeyboardButton(
            text=f"{_st(_og.enabled('frenzyex'))} 💳 FrenzyEx (آنلاین)",
            callback_data="paymt_frenzyex", style="primary"),
         InlineKeyboardButton(
            text=f"{_st(_og.enabled('tronado'))} 🌪 ترونادو (Tronado)",
            callback_data="paymt_tronado", style="primary")],
        [InlineKeyboardButton(
            text=f"{_st(_og.enabled('tonpays'))} 💎 تون‌پیز (TonPays)",
            callback_data="paymt_tonpays", style="primary")],
    ]
    try:
        import database as _db2
        _sw = _db2.get_topup_switches()
    except Exception:
        _sw = {"card": True, "online": True, "crypto": True, "frenzyex": True, "tronado": True, "tonpays": True}
    rows.append([InlineKeyboardButton(text="— شارژ کیف پول کاربران (فعال/غیرفعال هر روش) —", callback_data="paymt_topup_info", style="primary")])
    rows.append([
        InlineKeyboardButton(text=f"{_st(_sw['card'])} شارژ با کارت", callback_data="paymt_topup|card", style="primary"),
        InlineKeyboardButton(text=f"{_st(_sw['online'])} شارژ آنلاین", callback_data="paymt_topup|online", style="primary"),
        InlineKeyboardButton(text=f"{_st(_sw['crypto'])} شارژ ارزی", callback_data="paymt_topup|crypto", style="primary"),
    ])
    rows.append([
        InlineKeyboardButton(text=f"{_st(_sw.get('frenzyex', True))} شارژ FrenzyEx", callback_data="paymt_topup|frenzyex", style="primary"),
        InlineKeyboardButton(text=f"{_st(_sw.get('tronado', True))} شارژ ترونادو", callback_data="paymt_topup|tronado", style="primary"),
        InlineKeyboardButton(text=f"{_st(_sw.get('tonpays', True))} شارژ تون‌پیز", callback_data="paymt_topup|tonpays", style="primary"),
    ])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_log_settings_menu():
    """کیبورد مدیریت لاگ‌ها"""
    rows = [
        [InlineKeyboardButton(text="🦖 لاگ خطاها", callback_data="errlog", style="danger")],
        [InlineKeyboardButton(text="📋 گزارش فعالیت ادمین", callback_data="admin_audit_logs", style="primary")],
        [InlineKeyboardButton(text="📊 کانال یا گروه لاگ سفارشات", callback_data="admin_order_log_channel", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)



def admin_config_search_keyboard():
    """کیبورد صفحه‌ی جستجو (جستجوی کانفیگ حالا بخشی از جستجوی یکپارچه است)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def admin_unified_search_results_keyboard(items: list, page: int, page_size: int = 8, mode: str = "user") -> InlineKeyboardMarkup:
    """نتایج جستجوی یکپارچه (کاربر + سرویس) با صفحه‌بندی."""
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    rows = []
    for kind, obj in items[page * page_size:(page + 1) * page_size]:
        if kind == "u":
            label = f"👤 {obj.get('name') or 'کاربر'} | {obj.get('telegram_id')}"
            cb = f"useropen_{obj['telegram_id']}"
        else:
            owner = obj.get("owner_name") or obj.get("owner_tid") or "کاربر"
            mark = "❌ " if obj.get("deleted") else ""
            label = f"{mark}🚀 {obj.get('service_id') or obj.get('plan') or 'سرویس'} | 👤 {owner}"
            cb = f"svcdetail_{obj['id']}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=cb, style="primary")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"srchpg_{page - 1}", style="primary"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop", style="primary"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"srchpg_{page + 1}", style="primary"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔍 جستجوی جدید", callback_data=("admin_config_search" if mode == "config" else "admin_search"), style="success"),
                 InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_agent_plan_select_keyboard(agent_tid: str, all_vip_plans: list, selected_keys: list) -> InlineKeyboardMarkup:
    """انتخاب پلن‌های تخفیف‌خور + تخفیف اختصاصی هر پلن برای هر نماینده."""
    import database as _db
    agent = _db.get_agent(agent_tid) or {}
    default_pct = int(agent.get("vip_discount_percent") or 0)
    rows = []
    for plan in all_vip_plans:
        key = plan.get('plan_key', '')
        name = plan.get('name', key)
        check = "✅" if (not selected_keys or key in selected_keys) else "◻️"
        ov = _db.get_agent_plan_percent_override(agent_tid, key)
        pct_text = f"💯 {ov}٪" if ov is not None else f"💯 {default_pct}٪ (پیش‌فرض)"
        rows.append([
            InlineKeyboardButton(text=f"{check} {name}", callback_data=f"agentplan_{agent_tid}_{key}", style="primary"),
            InlineKeyboardButton(text=pct_text, callback_data=f"agentppct_{agent_tid}_{key}", style="success" if ov is not None else None),
        ])
    rows.append([InlineKeyboardButton(text="♾️ همه پلن‌ها", callback_data=f"agentplanall_{agent_tid}", style="success")])
    rows.append([InlineKeyboardButton(text="✅ ذخیره", callback_data=f"agentplansave_{agent_tid}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agentopen_{agent_tid}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_discount_audience_keyboard() -> InlineKeyboardMarkup:
    """انتخاب مخاطب تخفیف — ۱۴ دسته (بند ۳)."""
    OPTS = [
        ("👥 همه کاربران",        "discaud_all"),
        ("🛒 خرید‌کاران",          "discaud_buyers"),
        ("🆕 بدون خرید",           "discaud_non_buyers"),
        ("💰 کیف پول مثبت",       "discaud_wallet_positive"),
        ("🤝 نمایندگان",           "discaud_agents"),
        ("🟢 سرویس فعال",         "discaud_active_service"),
        ("🔴 سرویس منقضی",        "discaud_expired_service"),
        ("🆕 کاربران جدید",        "discaud_new_users"),
        ("📵 بدون سرویس",         "discaud_no_service"),
        ("📦 چند سرویس",          "discaud_multi_service"),
        ("😴 غیرفعال",            "discaud_inactive"),
        ("🎁 دریافت‌کننده تست",   "discaud_has_test"),
        ("🎟 دارای تخفیف",        "discaud_has_discount"),
        ("📋 آیدی خاص",           "discaud_specific"),
    ]
    rows = []
    for i in range(0, len(OPTS) - 1, 2):
        row = [InlineKeyboardButton(text=OPTS[i][0], callback_data=OPTS[i][1], style="primary")]
        if i + 1 < len(OPTS) - 1:
            row.append(InlineKeyboardButton(text=OPTS[i+1][0], callback_data=OPTS[i+1][1], style="primary"))
        rows.append(row)
    # آیدی خاص تنها در ردیف آخر
    rows.append([InlineKeyboardButton(text=OPTS[-1][0], callback_data=OPTS[-1][1], style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_discount_list", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_payg_menu(settings: dict) -> InlineKeyboardMarkup:
    """FIX 7: منوی Pay-as-you-go. قیمت هر گیگ دیگر عمومی نیست؛ هر «پلن PAYG» (گزینه‌ی نگاشت)
    قیمت اختصاصی خودش را دارد و از «مدیریت پلن‌های PAYG» تنظیم می‌شود."""
    status = "✅ فعال" if settings.get('enabled') else "❌ غیرفعال"
    action_lbl = "🗑 حذف کانفیگ" if settings.get('empty_action') == 'delete' else "⏸️ توقف کانفیگ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"وضعیت: {status}", callback_data="payg_toggle", style="primary")],
        [InlineKeyboardButton(text="📋 مدیریت پلن‌های PAYG (نام، پنل، قیمت)", callback_data="mopt_list|payg", style="success")],
        [InlineKeyboardButton(text="💳 حداقل موجودی برای ساخت", callback_data="payg_min_balance", style="primary")],
        [InlineKeyboardButton(text=f"عملکرد اتمام کیف: {action_lbl}", callback_data="payg_empty_action_toggle", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def admin_panel_folders_keyboard() -> InlineKeyboardMarkup:
    """FIX 16: منوی اصلی ادمین — دو دکمه در هر ردیف، دسته‌بندی."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 آمار", callback_data="admin_stats", style="primary"),
            InlineKeyboardButton(text="👥 لیست کاربران", callback_data="admin_userlist", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🔍 جستجوی حرفه‌ای", callback_data="admin_search", style="primary"),
            InlineKeyboardButton(text="🔎 جستجوی کانفیگ", callback_data="admin_config_search", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🎫 تیکت‌ها", callback_data="admin_tickets", style="primary"),
            InlineKeyboardButton(text="📢 پیام همگانی", callback_data="admin_broadcast", style="primary"),
        ],
        [
            InlineKeyboardButton(text="📥 صف درخواست‌ها", callback_data="admin_request_queue", style="primary"),
            InlineKeyboardButton(text="🤝 نمایندگی‌ها", callback_data="admin_agency", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🗂 دسته VIP", callback_data="admin_vip_categories", style="primary"),
            InlineKeyboardButton(text="🖥 پنل‌های VPN", callback_data="admin_vpn_panels", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🎟 تخفیف‌ها", callback_data="admin_discount", style="primary"),
            InlineKeyboardButton(text="📣 لینک تبلیغاتی", callback_data="admin_campaigns", style="primary"),
        ],
        [
            InlineKeyboardButton(text="💳 روش‌های پرداخت", callback_data="admin_payment_methods", style="primary"),
            InlineKeyboardButton(text="🤝 دعوت‌ها", callback_data="admin_referrals", style="primary"),
        ],
        [
            InlineKeyboardButton(text="📚 راهنما", callback_data="admin_guides", style="primary"),
            InlineKeyboardButton(text="✅️ ویرایش متن", callback_data="admin_text_editor", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🛠 ایجاد سرویس", callback_data="admin_create_service", style="primary"),
            InlineKeyboardButton(text="🖥 مینی اپ", callback_data="admin_miniapp_open", style="primary"),
        ],
            ])

# ---------------------------------------------------------------------------
# منوی اصلی (Inline) — کاربر عادی
# ---------------------------------------------------------------------------
def main_menu():
    return InlineKeyboardMarkup(ui_screen="start", inline_keyboard=[
        [InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data="plans", style="success")],
        [InlineKeyboardButton(text="🎁 تست رایگان", callback_data="buy_plan_test", style="success")],
        [InlineKeyboardButton(text="📱 سرویس‌های من", callback_data="my_configs", style="primary")],
        [InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet", style="primary")],
        *([[InlineKeyboardButton(text="👥 دعوت دوستان و کسب درآمد", callback_data="referral", style="primary")]] if db.referral_enabled() else []),
        [InlineKeyboardButton(text="👤 پروفایل من", callback_data="profile", style="primary")],
        [InlineKeyboardButton(text="👨‍💻 پشتیبانی", callback_data="support", style="primary")],
        [InlineKeyboardButton(text="راهنما", callback_data="user_guides", style="primary")],
       
    ])


def back_button(callback_data: str = "back", text: str = "🏠 بازگشت به منوی اصلی", screen: str | None = None):
    """دکمه‌ی بازگشت/انصراف تکی.

    fix: پارامتر screen قبلاً هیچ‌جا در امضای این تابع تعریف نشده بود ولی
    در ده‌ها جای کد (menu.py/plans.py/ticket.py/wallet.py/profile.py) با
    back_button(..., screen="...") صدا زده می‌شد؛ در نتیجه همه‌ی این صفحات
    (راهنما، لغو تیکت، سبد خالی سرویس‌ها، کد تخفیف، کیف پول قفل/آزاد و...)
    با TypeError کرش می‌کردن. الان این پارامتر پذیرفته می‌شه و اگر داده بشه،
    دکمه به ویرایشگر متن/دکمه هم وصل می‌شه تا متن و رنگش هم از پنل ادمین قابل
    تغییر باشه (درست مثل بقیه‌ی دکمه‌های ثابت ربات).
    """
    if screen:
        import ui_editor as _ue
        _pats = [p for p, _l in (_ue.SCREENS.get(screen) or {}).get("buttons", [])]
        # کلید ثبت‌شده‌ی دکمه‌ی بازگشت همین صفحه: «back»؛ اگر نبود، الگوی هم‌خوان با callback
        _key = "back" if "back" in _pats else _ue.pattern_for(screen, callback_data, text)
        btn = InlineKeyboardButton(
            text=text, callback_data=callback_data, style="danger",
            ui_screen=screen, ui_button_key=_key,
        )
    else:
        btn = InlineKeyboardButton(text=text, callback_data=callback_data, style="danger")
    return InlineKeyboardMarkup(ui_screen=screen, inline_keyboard=[[btn]])


# ✅ کیبورد نمایش‌داده‌شده به کاربر بعد از ارسال رسید کارت‌به‌کارت (خرید سرویس/شارژ کیف پول/سرویس سفارشی)
def receipt_submitted_keyboard():
    return InlineKeyboardMarkup(ui_screen="receipt_submitted", inline_keyboard=[
        [
            InlineKeyboardButton(text="👨‍💻 ارتباط با پشتیبانی", url=bot_info.get_support_url(), style="primary", ui_screen="receipt_submitted", ui_button_key="support_url"),
            InlineKeyboardButton(text="🏠 بازگشت به صفحه اصلی", callback_data="back", style="danger", ui_screen="receipt_submitted", ui_button_key="back"),
        ],
    ])


# ✅ سه دکمه‌ی کمکی زیر پیام «پرداخت کارت به کارت» (خرید سرویس/شارژ کیف پول/سرویس سفارشی):
# دو دکمه‌ی کپی (شماره کارت و مبلغ به ریال) در ردیف بالا، و دکمه‌ی تغییر روش پرداخت
# در ردیف پایین که فاکتور فعلی را منقضی کرده و به مرحله‌ی انتخاب روش پرداخت برمی‌گردد.
def card_payment_actions_keyboard(card_number: str, amount_toman: int, change_method_callback: str):
    amount_rial = amount_toman * 10
    import database as _cdb
    copy_row = []
    if _cdb.card_option("show_copy_card"):
        copy_row.append(InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=str(card_number)), style="primary", ui_screen="card_payment_actions", ui_button_key="copy_card"))
    if _cdb.card_option("show_copy_amount"):
        copy_row.append(InlineKeyboardButton(text="📋 کپی مبلغ به ریال", copy_text=CopyTextButton(text=str(amount_rial)), style="primary", ui_screen="card_payment_actions", ui_button_key="copy_amount"))
    return InlineKeyboardMarkup(ui_screen="card_payment_actions", inline_keyboard=[
        *([copy_row] if copy_row else []),
        [
            InlineKeyboardButton(text="🔄 انتخاب روش پرداخت دیگر", callback_data=change_method_callback, style="danger", ui_screen="card_payment_actions", ui_button_key="changepay_"),
        ],
    ])


def profile_menu():
    _ref = True
    try:
        _ref = db.referral_enabled()
    except Exception:
        pass
    rows = [[InlineKeyboardButton(text="💰 کیف پول آزاد", callback_data="wallet_free", style="primary")]]
    if _ref:
        rows.append([InlineKeyboardButton(text="🔒 کیف پول مسدود", callback_data="wallet_locked", style="danger")])
    rows.append([InlineKeyboardButton(text="🛒 تاریخچه خرید", callback_data="purchase_history", style="primary")])
    rows.append([InlineKeyboardButton(text="📋 تاریخچه تراکنش", callback_data="transactions", style="primary")])
    if _ref:
        rows.append([InlineKeyboardButton(text="🔗 لینک دعوت اختصاصی", callback_data="referral", style="success")])
    rows.append([InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")])
    return InlineKeyboardMarkup(ui_screen="profile", inline_keyboard=rows)


def wallet_menu():
    return InlineKeyboardMarkup(ui_screen="wallet", inline_keyboard=[
        [InlineKeyboardButton(text="💳 شارژ کیف پول", callback_data="charge", style="success", ui_screen="wallet", ui_button_key="charge")],
        [InlineKeyboardButton(text="🎟 ثبت کد تخفیف", callback_data="use_discount", style="success", ui_screen="wallet", ui_button_key="use_discount")],
        [InlineKeyboardButton(text="📋 تراکنش‌های من", callback_data="transactions", style="primary", ui_screen="wallet", ui_button_key="transactions")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def charge_amount_keyboard(amounts: list[int] | None = None):
    # 💡 دکمه‌های مبلغ پیشنهادی دیگر ثابت نیستند: از تنظیمات «مدیریت شارژ
    # کیف‌پول» در دیتابیس خوانده می‌شوند (پیش‌فرض: ۵۰,۱۰۰,۲۰۰ هزار تومان).
    if amounts is None:
        import database as db
        amounts = db.get_wallet_settings()["quick_amounts"]
    rows = [
        [InlineKeyboardButton(text=f"💰 {amount:,} تومان", callback_data=f"charge_{amount}", style="primary", ui_screen="wallet_charge", ui_button_key="charge_")]
        for amount in amounts
    ]
    rows.append([InlineKeyboardButton(text="💵 مبلغ دلخواه", callback_data="charge_custom", style="primary", ui_screen="wallet_charge", ui_button_key="charge_custom")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet", style="danger")])
    return InlineKeyboardMarkup(ui_screen="wallet_charge", inline_keyboard=rows)


def charge_payment_method_keyboard(amount: int, methods: list[str] | None = None):
    """انتخاب روش پرداخت برای شارژ کیف پول: همان روش‌های خرید (کارت، آنلاین، ارز دیجیتال)،
    ولی با سوییچ اختصاصی «شارژ کیف پول» برای هر روش. درگاه آنلاین فقط برای مبلغ بیشتر از
    ONLINE_PAYMENT_MIN_AMOUNT پیشنهاد می‌شود."""
    if methods is None:
        import database as _db
        methods = _db.available_topup_methods()
    buttons = []
    if "online" in methods and amount > ONLINE_PAYMENT_MIN_AMOUNT:
        buttons.append(
            [InlineKeyboardButton(text="🌐 پرداخت آنلاین (تایید خودکار)", callback_data=f"chargepay_online_{amount}", style="success")]
        )
    for _gw in _og.NEW_GATEWAYS:
        if _gw in methods and _og.amount_ok(_gw, amount):
            buttons.append([InlineKeyboardButton(text=_og.menu_button_text(_gw), callback_data=f"chargepay_{_og.short_of(_gw)}_{amount}", style="success")])
    if "card" in methods:
        buttons.append([InlineKeyboardButton(text="💳 پرداخت کارت به کارت", callback_data=f"chargepay_card_{amount}", style="success")])
    if "crypto" in methods:
        buttons.append([InlineKeyboardButton(text="💎 پرداخت ارز دیجیتال", callback_data=f"chargepay_crypto_{amount}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="charge", style="danger")])
    return InlineKeyboardMarkup(ui_screen="walletcharge_method", inline_keyboard=buttons)


def charge_crypto_select_keyboard(cryptos: list, amount: int):
    buttons = []
    for coin in cryptos or []:
        symbol = str(getattr(coin, "symbol", "") or "").upper()
        network = str(getattr(coin, "network", "") or "")
        emoji = str(getattr(coin, "emoji", "💎") or "💎")
        label = f"{emoji} {symbol}" + (f" ({network})" if network else "")
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"chargecrypto|{amount}|{symbol}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"charge_{amount}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def charge_crypto_detail_keyboard(wallet_address: str, amount_text: str, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👛 کپی ولت", copy_text=CopyTextButton(text=str(wallet_address)), style="primary", ui_screen="crypto_payment_detail", ui_button_key="copy_wallet"),
            InlineKeyboardButton(text="📋 کپی مبلغ", copy_text=CopyTextButton(text=str(amount_text)), style="primary", ui_screen="crypto_payment_detail", ui_button_key="copy_amount"),
        ],
        [InlineKeyboardButton(text="🔙 انتخاب ارز دیگر", callback_data=f"chargepay_crypto_{amount}", style="danger", ui_screen="crypto_payment_detail", ui_button_key="other_asset")],
    ], ui_screen="crypto_payment_detail")


def _toman_to_rial_text(v) -> str:
    """مبلغ دقیق واریز یونیک‌پی تومان است؛ برای کپی در اپ بانک باید ریال (×۱۰) باشد."""
    t = str(v or "").strip().replace(",", "")
    return str(int(t) * 10) if t.isdigit() else t


def online_payment_wallet_keyboard(payment_link: str, online_payment_id: int, white_label: dict | None = None):
    rows = []
    if white_label:
        rows.append([
            InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=str(white_label["cardNumber"])), style="primary", ui_screen="walletcharge_pay_online", ui_button_key="uq_copy_card"),
            InlineKeyboardButton(text="📋 کپی مبلغ به ریال", copy_text=CopyTextButton(text=_toman_to_rial_text(white_label["payableAmount"])), style="primary", ui_screen="walletcharge_pay_online", ui_button_key="uq_copy_amount"),
        ])
    rows.append([InlineKeyboardButton(text="💳 پرداخت در صفحه UniquePay", url=payment_link, style="success", ui_screen="walletcharge_pay_online", ui_button_key="uq_pay")])
    rows.append([InlineKeyboardButton(text="✅ پرداخت را انجام دادم / بررسی کن", callback_data=f"checkpay_{online_payment_id}", style="success", ui_screen="walletcharge_pay_online", ui_button_key="checkpay_")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data="wallet", style="danger")])
    return InlineKeyboardMarkup(ui_screen="walletcharge_pay_online", inline_keyboard=rows)


def referral_menu():
    return InlineKeyboardMarkup(ui_screen="referral", inline_keyboard=[
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def support_menu():
    return InlineKeyboardMarkup(ui_screen="support", inline_keyboard=[
        [InlineKeyboardButton(text="🎫 ارسال تیکت", callback_data="ticket", style="primary")],
        [InlineKeyboardButton(text="📋 تیکت‌های من", callback_data="my_tickets", style="primary")],
        [InlineKeyboardButton(text="📢 کانال اصلی و پشتیبان", url=bot_info.get_support_url(), style="primary", ui_screen="support", ui_button_key="support_channel")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


_TICKET_STATUS_BADGE = {"open": "🟢 باز", "answered": "🟡 پاسخ داده شد", "closed": "🔴 بسته شده"}


def my_tickets_list_keyboard(tickets: list[dict]):
    buttons = [
        [InlineKeyboardButton(
            text=f"{_TICKET_STATUS_BADGE.get(t['status'], t['status'])} — تیکت #{t['id']}",
            callback_data=f"myticketview_{t['id']}", style="primary",
        )]
        for t in tickets
    ]
    buttons.append([InlineKeyboardButton(text="🎫 تیکت جدید", callback_data="ticket", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="support", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def ticket_thread_user_keyboard(ticket_id: int, status: str):
    buttons = []
    if status != "closed":
        buttons.append([InlineKeyboardButton(text="✍️ پاسخ", callback_data=f"ticketreply_{ticket_id}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست تیکت‌ها", callback_data="my_tickets", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# سرویس‌ها / خرید اشتراک
# ---------------------------------------------------------------------------
def plans_direct_to_vip() -> bool:
    """True اگر «کانفیگ خودتو بساز» و PAYG هر دو خاموش باشند؛ در این حالت دکمه‌ی «سرور VIP (V2Ray)»
    نمایش داده نمی‌شود و «خرید اشتراک» مستقیم پوشه‌ها و پلن‌ها را باز می‌کند."""
    try:
        import database as _db
        custom_build_on = (_db.get_setting("custom_build_enabled", "1") == "1")
        payg_on = bool(_db.get_payg_settings().get("enabled"))
        return (not custom_build_on) and (not payg_on)
    except Exception:
        return False


def vip_shortcut_on() -> bool:
    """میانبر VIP: پوشه‌ها و پلن‌ها مستقیم کنار «ساخت سفارشی» و PAYG زیر متن «خرید اشتراک» نشان داده می‌شوند."""
    try:
        import database as _db
        return _db.get_setting("vip_shortcut", "0") == "1"
    except Exception:
        return False


def _vip_top_rows(discount_percent: int = 0) -> list:
    # ترتیب میانبر: ۱) پوشه‌ها به ترتیب ساخته‌شدن  ۲) پلن‌های بدون پوشه (زیر پوشه‌ها)  ۳) (در plans_menu) سرویس سفارشی و PAYG
    rows = []
    for cat in sorted(db.get_child_vip_categories(None), key=lambda c: int(c.get("id") or 0)):
        cat_emoji_id = db.get_button_custom_emoji_id("text:" + str(cat.get("name") or ""))
        rows.append([InlineKeyboardButton(text=f"{cat['name']}", callback_data=f"vipcat_{cat['key']}", style="primary",
                                          icon_custom_emoji_id=cat_emoji_id or None)])
    root = db.get_vip_root_bucket(create=False)
    if root:
        rows.extend(_vip_plan_buttons(sorted(db.get_vip_plans(root["id"]), key=lambda p: int(p.get("id") or 0)), discount_percent))
    return rows


def plans_menu():
    if vip_shortcut_on():
        rows = _vip_top_rows() or [[InlineKeyboardButton(text="سرور VIP (V2Ray)", callback_data="plans_vip", style="success")]]
    else:
        rows = [
            [InlineKeyboardButton(text="سرور VIP (V2Ray)", callback_data="plans_vip", style="success")],
        ]
    try:
        import database as _db
        custom_build_on = (_db.get_setting("custom_build_enabled", "1") == "1")
        payg_on = bool(_db.get_payg_settings().get("enabled"))
        if custom_build_on:
            rows.extend([
                [InlineKeyboardButton(text="🛠 کانفیگ خودتو بساز (ویژه VIP)", callback_data="cbuild_start", style="primary")],
            ])
        if payg_on:
            rows.extend([
                [InlineKeyboardButton(text=__import__("ui_editor").get_button("plans", "payg_buy", "⚡ خرید سرویس PAYG"), callback_data="payg_buy", style="success")],
            ])
    except Exception:
        rows.extend([
            [InlineKeyboardButton(text="🛠 کانفیگ خودتو بساز (ویژه VIP)", callback_data="cbuild_start", style="primary")],
        ])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="back", style="danger")])
    return InlineKeyboardMarkup(ui_screen="plans", inline_keyboard=rows)


def custom_build_payment_keyboard(show_discount: bool = True):
    buttons = [
        [InlineKeyboardButton(text="👛 پرداخت از کیف پول", callback_data="cbuild_pay_wallet", style="success")],
    ]
    if _uq_enabled():
        buttons.append(
            [InlineKeyboardButton(text="🌐 پرداخت آنلاین (تایید خودکار)", callback_data="cbuild_pay_online", style="success")]
        )
    for _gw in _og.enabled_gateways():
        buttons.append([InlineKeyboardButton(text=_og.menu_button_text(_gw), callback_data=f"cbuild_pay_{_og.short_of(_gw)}", style="success")])
    buttons.append([InlineKeyboardButton(text="💳 پرداخت کارت به کارت", callback_data="cbuild_pay_card", style="success")])
    if show_discount:
        buttons.append([InlineKeyboardButton(text="🎟 ثبت کد تخفیف", callback_data="discount_cbuild", style="primary", ui_screen="cbuild_payment_method", ui_button_key="discount_cbuild")])
    buttons.append([InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="cbuild_payment_method", inline_keyboard=buttons)


def payg_name_keyboard():
    """مرحله‌ی نام سرویس PAYG: انتخاب خودکار (بالای انصراف) + انصراف."""
    return InlineKeyboardMarkup(ui_screen="payg_name", inline_keyboard=[
        [InlineKeyboardButton(text="🤖 انتخاب خودکار", callback_data="payg_name_auto", style="success", ui_screen="payg_name", ui_button_key="payg_name_auto")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger", ui_screen="payg_name", ui_button_key="plans")],
    ])


def custom_build_cancel_keyboard():
    return InlineKeyboardMarkup(ui_screen="custom_build", inline_keyboard=[
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger")],
    ])

def renew_services_keyboard(configs):
    """لیست سرویس‌های قابل تمدید کاربر (دکمه‌ی «تمدید» منوی پایین)؛ انتخاب هر سرویس وارد همان فرآیند renewcfg_ می‌شود."""
    rows = [[InlineKeyboardButton(text=f"🔁 {cfg.get('service_id') or cfg.get('plan') or 'سرویس'}".strip(), callback_data=f"renewcfg_{cfg['id']}", style="success")] for cfg in configs]
    rows.append([InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_payment_keyboard(show_discount: bool = True):
    buttons=[[InlineKeyboardButton(text="💰 پرداخت از کیف پول", callback_data="cbuild_pay_wallet", style="success")]]
    if _uq_enabled():
        buttons.append([InlineKeyboardButton(text="🌐 پرداخت آنلاین", callback_data="cbuild_pay_online", style="success")])
    for _gw in _og.enabled_gateways():
        buttons.append([InlineKeyboardButton(text=_og.menu_button_text(_gw), callback_data=f"cbuild_pay_{_og.short_of(_gw)}", style="success")])
    buttons.append([InlineKeyboardButton(text="💳 کارت به کارت", callback_data="cbuild_pay_card", style="success")])
    if show_discount:
        buttons.append([InlineKeyboardButton(text="🎟 ثبت کد تخفیف", callback_data="discount_cbuild", style="primary", ui_screen="renew_payment_method", ui_button_key="discount_cbuild")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="renew_cancel", style="danger")])
    return InlineKeyboardMarkup(ui_screen="renew_payment_method", inline_keyboard=buttons)

def _renewal_edit_menu(category_id:int, plan_key:str|None=None):
    target=f"plan_{plan_key}" if plan_key else f"cat_{int(category_id)}"
    st=bot_info.get_renewal_plan_settings(plan_key,category_id) if plan_key else bot_info.get_renewal_settings(category_id)
    mode_labels={"day":"⏳ فقط زمان","gb":"📦 فقط حجم","both":"📦⏳ حجم و زمان"}
    day_opts=str(st.get("day_options") or "30,60,90")
    gb_opts=str(st.get("gb_options") or "10,20,50")
    rows=[
        [InlineKeyboardButton(text=f"🔁 نوع تمدید: {mode_labels.get(st.get('mode'),'⏳ فقط زمان')}",callback_data=f"renewsetmode_{target}",style="primary")],
        [InlineKeyboardButton(text=f"💰 قیمت هر روز: {int(st.get('price_day') or 0):,}",callback_data=f"renewset_price_day_{target}",style="primary"), InlineKeyboardButton(text=f"💰 قیمت هر گیگ: {int(st.get('price_gb') or 0):,}",callback_data=f"renewset_price_gb_{target}",style="primary")],
        [InlineKeyboardButton(text=f"⬇️ حداقل روز: {int(st.get('min_day') or 1)}",callback_data=f"renewset_min_day_{target}",style="primary"), InlineKeyboardButton(text=f"⬆️ حداکثر روز: {'نامحدود' if not int(st.get('max_day') or 0) else int(st.get('max_day'))}",callback_data=f"renewset_max_day_{target}",style="primary")],
        [InlineKeyboardButton(text=f"⬇️ حداقل گیگ: {int(st.get('min_gb') or 1)}",callback_data=f"renewset_min_gb_{target}",style="primary"), InlineKeyboardButton(text=f"⬆️ حداکثر گیگ: {'نامحدود' if not int(st.get('max_gb') or 0) else int(st.get('max_gb'))}",callback_data=f"renewset_max_gb_{target}",style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های روز: {day_opts}",callback_data=f"renewset_options_day_{target}",style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های گیگ: {gb_opts}",callback_data=f"renewset_options_gb_{target}",style="primary")],
    ]
    if plan_key:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت به تنظیمات دسته",callback_data=f"renewsetscope_{int(category_id)}",style="danger")])
    elif int(category_id) <= -1:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="renewcustlist",style="danger")])
    else:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_renewal_settings",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_custom_options_menu():
    """لیست گزینه‌های سرویس سفارشی (هر گزینه قیمت/محدوده‌ی خودش را دارد) برای تنظیم تمدید."""
    rows=[]
    try:
        opts=db.get_map_options("custom_build") or []
    except Exception:
        opts=[]
    for o in opts:
        rows.append([InlineKeyboardButton(text=f"🛠 {o.get('name') or ('گزینه '+str(o['num']))}",callback_data=f"renewsetscope_all_{bot_info.custom_option_id(o['num'])}",style="primary")])
    rows.append([InlineKeyboardButton(text="🌐 پیش‌فرض همه‌ی گزینه‌های سفارشی",callback_data="renewsetscope_all_-1",style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_renewal_settings",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_categories_menu():
    cats=db.get_vip_categories() or []
    _on = db.is_renewal_enabled()
    rows=[[InlineKeyboardButton(text=("🟢 قابلیت تمدید: روشن (برای خاموش‌کردن بزن)" if _on else "🔴 قابلیت تمدید: خاموش (برای روشن‌کردن بزن)"),callback_data="renewsw_toggle",style=("success" if _on else "danger"))]]
    rows.append([InlineKeyboardButton(text="🛠 تنظیمات سرویس سفارشی",callback_data="renewcustlist",style="success")])
    rows.append([InlineKeyboardButton(text="🌐 پیش‌فرض (بدون دسته/همه)",callback_data="renewsetscope_all_0",style="success")])
    rows+=[[InlineKeyboardButton(text=str(c.get('name') or c.get('id')),callback_data=f"renewsetcat_{int(c['id'])}",style="primary")] for c in cats]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_back",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_scope_menu(category_id:int):
    plans=db.get_vip_plans(int(category_id)) or []
    rows=[[InlineKeyboardButton(text="🌐 همه پلن‌های این دسته",callback_data=f"renewsetscope_all_{int(category_id)}",style="success")]]
    rows += [[InlineKeyboardButton(text=f"📦 فقط {p.get('name') or p.get('plan_key')}",callback_data=f"renewsetscope_plan_{int(category_id)}_{p['plan_key']}",style="primary")] for p in plans]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_renewal_settings",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_category_menu(category_id:int): return _renewal_edit_menu(category_id)
def admin_renewal_plan_menu(category_id:int,plan_key:str): return _renewal_edit_menu(category_id,plan_key)
def admin_renewal_mode_menu(category_id:int,plan_key:str|None=None):
    target=f"plan_{plan_key}" if plan_key else f"cat_{int(category_id)}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏳ فقط زمان",callback_data=f"renewsetmodeval_day_{target}",style="primary")],[InlineKeyboardButton(text="📦 فقط حجم",callback_data=f"renewsetmodeval_gb_{target}",style="primary")],[InlineKeyboardButton(text="📦⏳ حجم و زمان",callback_data=f"renewsetmodeval_both_{target}",style="primary")],[InlineKeyboardButton(text="🔙 بازگشت",callback_data=(f"renewsetscope_{category_id}" if plan_key else f"renewsetscope_all_{category_id}"),style="danger")]])

def renew_mode_keyboard():
    """منوی انتخاب حالت تمدید برای مسیر قدیمی custom-build.

    این callbackها با renew_mode_start در handlers/plans.py هماهنگ هستند.
    مسیر جدید renewcfg_* نوع تمدید را مستقیماً از تنظیمات دسته/پلن می‌گیرد
    و این کیبورد فقط برای سازگاری با callbackهای renew_* قدیمی نگه داشته شده است.
    """
    return InlineKeyboardMarkup(ui_screen="renew_menu", inline_keyboard=[
        [InlineKeyboardButton(text="⏳ تمدید زمان", callback_data="renewmode_time", style="success")],
        [InlineKeyboardButton(text="🗜 تمدید حجم", callback_data="renewmode_volume", style="success")],
        [InlineKeyboardButton(text="🔁 تمدید حجم و زمان", callback_data="renewmode_both", style="success")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger", ui_screen="renew_menu", ui_button_key="renew_cancel")],
    ])


def _plans_keyboard(plans_dict: dict, icon: str, discount_percent: int = 0):
    buttons = []
    for key, plan in plans_dict.items():
        price = plan["price"]
        if discount_percent:
            price = int(price * (1 - discount_percent / 100))
        plan_emoji_id = db.get_button_custom_emoji_id("text:" + str(plan.get("name") or ""))
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {plan['name']} — {price:,} تومان",
            callback_data=f"buy_{key}", style="success",
            icon_custom_emoji_id=plan_emoji_id or None)])
    import ui_editor as _ui
    buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "plans", "🔙 بازگشت"), callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="plan_select", inline_keyboard=buttons)


def _vip_plan_buttons(plans: list[dict], discount_percent: int = 0) -> list:
    rows = []
    for plan in plans:
        price = plan["price"]
        if discount_percent:
            price = int(price * (1 - discount_percent / 100))
        plan_emoji_id = db.get_button_custom_emoji_id("text:" + str(plan.get("name") or ""))
        rows.append([InlineKeyboardButton(
            text=f"{plan['name']} — {price:,} تومان", callback_data=f"buy_{plan['plan_key']}",
            style="primary", icon_custom_emoji_id=plan_emoji_id or None)])
    return rows


def vip_categories_keyboard(discount_percent: int = 0):
    """مرحله‌ی اول «خرید اشتراک»: پوشه‌های اصلی + پلن‌هایی که مستقیم بدون پوشه قرار داده شده‌اند
    (هر پوشه‌ی جدیدی که ادمین بسازد اینجا خودکار ظاهر می‌شود)."""
    buttons = []
    for cat in db.get_child_vip_categories(None):
        cat_emoji_id = db.get_button_custom_emoji_id("text:" + str(cat.get("name") or ""))
        buttons.append([InlineKeyboardButton(text=f"{cat['name']}", callback_data=f"vipcat_{cat['key']}", style="primary",
                                             icon_custom_emoji_id=cat_emoji_id or None)])
    root = db.get_vip_root_bucket(create=False)
    if root:
        buttons.extend(_vip_plan_buttons(db.get_vip_plans(root["id"]), discount_percent))
    if not buttons:
        buttons.append([InlineKeyboardButton(text="😔 فعلاً هیچ دسته‌ای موجود نیست", callback_data="noop", style="primary")])
    # اگر منوی میانی (سرور VIP / سفارشی / PAYG) نمایش داده نمی‌شود، بازگشت باید به منوی اصلی برود
    # (وگرنه «plans» دوباره همین صفحه را باز می‌کرد).
    _back_cb = "back" if plans_direct_to_vip() else "plans"
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=_back_cb, style="danger")])
    return InlineKeyboardMarkup(ui_screen="vip_category_list", inline_keyboard=buttons)


def vip_category_plans_keyboard(category_key: str, discount_percent: int = 0):
    """داخل یک پوشه: زیرپوشه‌ها (هر عمق) + پلن‌های همان پوشه، کنار هم."""
    cat = db.get_vip_category(category_key)
    buttons = []
    if cat:
        for sub in db.get_child_vip_categories(cat["id"]):
            sub_emoji_id = db.get_button_custom_emoji_id("text:" + str(sub.get("name") or ""))
            buttons.append([InlineKeyboardButton(text=f"{sub['name']}", callback_data=f"vipcat_{sub['key']}", style="primary",
                                                 icon_custom_emoji_id=sub_emoji_id or None)])
        buttons.extend(_vip_plan_buttons(db.get_vip_plans(cat["id"]), discount_percent))
    if not buttons:
        buttons.append([InlineKeyboardButton(text="😔 فعلاً هیچ پلنی در این دسته نیست", callback_data="noop", style="primary")])
    # بازگشت: یک مرحله به پوشه‌ی والد (یا به صفحه‌ی اصلی «خرید اشتراک»)
    back_cb = "plans_vip"
    if cat and cat.get("parent_id") is not None:
        parent = db.get_vip_category(int(cat["parent_id"]))
        if parent and not db.is_vip_root_bucket(parent):
            back_cb = f"vipcat_{parent['key']}"
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=back_cb, style="danger")])
    return InlineKeyboardMarkup(ui_screen="vip_plans", inline_keyboard=buttons)



def all_plans_discount_keyboard(discount_percent: int):
    return _plans_keyboard(db.get_all_plans(), "📅", discount_percent)


def free_claim_keyboard(plan_key: str):
    """سفارش با مبلغ نهایی صفر (پلن رایگان یا کد تخفیف ۱۰۰٪): هیچ روش پرداختی نشان داده نمی‌شود، فقط «دریافت رایگان»
    (همان هندلر pay_wallet_ که با مبلغ صفر بدون کسر موجودی سرویس را می‌سازد)."""
    return InlineKeyboardMarkup(ui_screen="free_test", inline_keyboard=[
        [InlineKeyboardButton(text="🎁 دریافت رایگان", callback_data=f"pay_wallet_{plan_key}", style="success", ui_screen="free_test", ui_button_key="pay_wallet_")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")],
    ])


def free_test_confirm_keyboard(plan_key: str):
    """🆕 صفحه‌ی تایید تست رایگان: چون قیمت تست رایگان صفر است، هیچ روش پرداختی
    (کیف پول/کارت/آنلاین) نشان داده نمی‌شود و فقط یک دکمه‌ی سبز تایید وجود دارد که
    همان هندلر موجود پرداخت از کیف پول (pay_wallet_) را صدا می‌زند و چون قیمت صفر است، کسر
    شدن از کیف پول بدون هیچ مشکلی انجام می‌شود و سرویس بلافاصله از پنل فعال نگاشته‌شده ساخته
    و ارسال می‌شود (auto_fulfill_vip_via_panel).
    """
    return InlineKeyboardMarkup(ui_screen="free_test", inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ همین الان تست رایگان بگیر", callback_data=f"pay_wallet_{plan_key}", style="success", ui_screen="free_test", ui_button_key="pay_wallet_")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")],
    ])


def free_test_list_keyboard(options: list):
    """چند پلن تست: هر کدام با نام نمادینی که ادمین تعریف کرده به‌جای دکمه‌ی سبز تکی."""
    rows = []
    for plan_key, plan in options:
        price = int(plan.get("price") or 0)
        label = str(plan.get("name") or "تست رایگان")
        if price > 0:
            label += f" — {price:,} تومان"
        rows.append([InlineKeyboardButton(text=f"{label}", callback_data=f"pay_wallet_{plan_key}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_payment_keyboard(plan_key: str, show_discount: bool = True):
    """نمایش روش‌های پرداخت بر اساس تنظیمات فعال و متن‌های قابل ویرایش."""
    import database as _db, json as _json
    import ui_editor as _ui
    try:
        pm_card = _json.loads(_db.get_setting('pm_card', '{}') or '{}')
        pm_wallet = _json.loads(_db.get_setting('pm_wallet', '{}') or '{}')
        pm_uniquepay = _json.loads(_db.get_setting('pm_uniquepay', '{}') or '{}')
        pm_cryptos = _json.loads(_db.get_setting('pm_cryptos', '[]') or '[]')
        card_on = bool(pm_card.get('enabled', True))
        wallet_on = bool(pm_wallet.get('enabled', True))
        online_on = bool(pm_uniquepay.get('enabled', False) and (pm_uniquepay.get('business_token') or '').strip())
        crypto_on = bool(_db.payment_method_flags().get('crypto'))
    except Exception:
        card_on = wallet_on = True
        online_on = bool(_uq_enabled())
        crypto_on = False
    try:
        _gw_flags = _db.payment_method_flags()
    except Exception:
        _gw_flags = {}
    buttons = []
    if wallet_on:
        buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "pay_wallet_", "👛 پرداخت از کیف پول"), callback_data=f"pay_wallet_{plan_key}", style="success", ui_screen="plan_payment_method", ui_button_key="pay_wallet_")])
    if online_on or _uq_enabled():
        buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "pay_online_", "🌐 پرداخت آنلاین (تایید خودکار)"), callback_data=f"pay_online_{plan_key}", style="success", ui_screen="plan_payment_method", ui_button_key="pay_online_")])
    for _gw in _og.NEW_GATEWAYS:
        if _gw_flags.get(_gw):
            _sh = _og.short_of(_gw)
            buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", f"pay_{_sh}_", _og.menu_button_text(_gw)), callback_data=f"pay_{_sh}_{plan_key}", style="success", ui_screen="plan_payment_method", ui_button_key=f"pay_{_sh}_")])
    if card_on:
        buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "pay_card_", "💳 پرداخت کارت به کارت"), callback_data=f"pay_card_{plan_key}", style="success", ui_screen="plan_payment_method", ui_button_key="pay_card_")])
    if crypto_on:
        buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "pay_crypto_", "💎 پرداخت ارز دیجیتال"), callback_data=f"pay_crypto_{plan_key}", style="success", ui_screen="plan_payment_method", ui_button_key="pay_crypto_")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "pay_card_", "💳 پرداخت کارت به کارت"), callback_data=f"pay_card_{plan_key}", style="success", ui_screen="plan_payment_method", ui_button_key="pay_card_")])
    if show_discount:
        buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "discount_plan_", "🎟 ثبت کد تخفیف"), callback_data=f"discount_plan_{plan_key}", style="primary", ui_screen="plan_payment_method", ui_button_key="discount_plan_")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="plan_payment_method", inline_keyboard=buttons)


def crypto_select_keyboard(cryptos: list, plan_key: str, final_price: int):
    """انتخاب ارز برای پرداخت یک پلن کاربر."""
    import ui_editor as _ui
    buttons = []
    short_plan = hashlib.md5(str(plan_key).encode("utf-8")).hexdigest()[:10]
    for coin in cryptos or []:
        symbol = str(getattr(coin, 'symbol', '') or (coin.get('symbol', '') if isinstance(coin, dict) else '')).upper()
        network = str(getattr(coin, 'network', '') or (coin.get('network', '') if isinstance(coin, dict) else ''))
        emoji = str(getattr(coin, 'emoji', '💎') or (coin.get('emoji', '💎') if isinstance(coin, dict) else '💎'))
        enabled = bool(getattr(coin, 'enabled', True) if not isinstance(coin, dict) else coin.get('enabled', True))
        if not enabled:
            continue
        label = f"{emoji} {symbol}" + (f" ({network})" if network else "")
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pay_crypto_asset|{short_plan}|{symbol}", style="success")])
    buttons.append([InlineKeyboardButton(text=_ui.get_button("plan_payment_method", "plans", "🔙 بازگشت"), callback_data=f"buy_{plan_key}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def crypto_payment_detail_keyboard(wallet_address: str, amount_text: str, plan_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👛 کپی ولت", copy_text=CopyTextButton(text=str(wallet_address)), style="primary", ui_screen="crypto_payment_detail", ui_button_key="copy_wallet"),
            InlineKeyboardButton(text="📋 کپی مبلغ", copy_text=CopyTextButton(text=str(amount_text)), style="primary", ui_screen="crypto_payment_detail", ui_button_key="copy_amount"),
        ],
        [InlineKeyboardButton(text="🔙 انتخاب ارز دیگر", callback_data=f"pay_crypto_{plan_key}", style="danger", ui_screen="crypto_payment_detail", ui_button_key="other_asset")],
    ], ui_screen="crypto_payment_detail")


def online_payment_keyboard(payment_link: str, online_payment_id: int, white_label: dict | None = None, back_callback: str = "plans"):
    rows = []
    if white_label:
        rows.append([
            InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=str(white_label["cardNumber"])), style="primary", ui_screen="plan_pay_online", ui_button_key="uq_copy_card"),
            InlineKeyboardButton(text="📋 کپی مبلغ به ریال", copy_text=CopyTextButton(text=_toman_to_rial_text(white_label["payableAmount"])), style="primary", ui_screen="plan_pay_online", ui_button_key="uq_copy_amount"),
        ])
    rows.append([InlineKeyboardButton(text="💳 پرداخت در صفحه UniquePay", url=payment_link, style="primary", ui_screen="plan_pay_online", ui_button_key="uq_pay")])
    rows.append([InlineKeyboardButton(text="✅ پرداخت را انجام دادم / بررسی کن", callback_data=f"checkpay_{online_payment_id}", style="success", ui_screen="plan_pay_online", ui_button_key="checkpay_")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen="plan_pay_online", inline_keyboard=rows)


def gateway_payment_keyboard(gateway: str, payment_link: str, payment_id: int, back_callback: str = "plans",
                             ui_screen: str = "plan_pay_online", link2: str | None = None,
                             price: int | None = None, gw_data: dict | None = None):
    """فاکتور درگاه‌های جدید (FrenzyEx/Tronado): دکمه‌های کپی مبلغ (و کپی ولت اگر درگاه آدرس واریز بدهد)،
    پرداخت، بررسی، انصراف — هم‌شکل فاکتورهای کریپتو/آنلاین."""
    gw_data = gw_data or {}
    rows = []
    copy_row = []
    if price:
        copy_row.append(InlineKeyboardButton(text="📋 کپی مبلغ (تومان)", copy_text=CopyTextButton(text=str(int(price))), style="primary", ui_screen=ui_screen, ui_button_key="gw_copy_amount"))
    if gw_data.get("tron_amount"):
        copy_row.append(InlineKeyboardButton(text="📋 کپی مقدار TRX", copy_text=CopyTextButton(text=f"{float(gw_data['tron_amount']):g}"), style="primary", ui_screen=ui_screen, ui_button_key="gw_copy_trx"))
    if gw_data.get("pay_address"):
        copy_row.insert(0, InlineKeyboardButton(text="👛 کپی ولت", copy_text=CopyTextButton(text=str(gw_data["pay_address"])), style="primary", ui_screen=ui_screen, ui_button_key="gw_copy_wallet"))
    if copy_row:
        rows.append(copy_row)
    rows.append([InlineKeyboardButton(text=_og.button_text(gateway), url=payment_link, style="success", ui_screen=ui_screen, ui_button_key=f"gw_pay_{gateway}")])
    if link2:
        rows.append([InlineKeyboardButton(text="🤖 باز کردن در چت ربات", url=link2, style="primary", ui_screen=ui_screen, ui_button_key="gw_open_bot")])
    rows.append([InlineKeyboardButton(text="✅ پرداخت را انجام دادم / بررسی کن", callback_data=f"checkpay_{payment_id}", style="success", ui_screen=ui_screen, ui_button_key="checkpay_")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen=ui_screen, inline_keyboard=rows)


def insufficient_balance_keyboard():
    return InlineKeyboardMarkup(ui_screen="insufficient_balance", inline_keyboard=[
        [InlineKeyboardButton(text="💵 شارژ کیف پول", callback_data="wallet", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger", ui_screen="insufficient_balance", ui_button_key="back")],
    ])


# ---------------------------------------------------------------------------
# سرویس‌های من
# ---------------------------------------------------------------------------
def my_configs_menu():
    return InlineKeyboardMarkup(ui_screen="services", inline_keyboard=[
        [InlineKeyboardButton(text="سرویس‌های VIP من", callback_data="my_configs_vip", style="primary")],
        [InlineKeyboardButton(text="🔎 جستجوی سرویس", callback_data="service_search", style="primary")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def my_configs_list_keyboard(configs, icon: str, back_callback: str, show_search: bool = True):
    # 🆕 اسم روی دکمه‌ی هر سرویس دیگر نام نمایشی پلن فروشگاه نیست؛ همان نامی
    # است که واقعاً در پنل ساخته شده (و داخل خود لینک ساب هم همین است) —
    # چون service_id همیشه دقیقاً همان username ارسالی به پنل است، نیازی به
    # فراخوانی زنده‌ی پنل برای نمایش لیست نیست.
    buttons = []
    for cfg in configs:
        cfg_icon = "⚡" if cfg.get("type") == "payg" else icon
        buttons.append([
            InlineKeyboardButton(text=f"{cfg_icon} {cfg.get('service_id') or cfg['plan']}".strip(), callback_data=f"viewconfig_{cfg['id']}", style="primary")
        ])
    if show_search:
        buttons.append([InlineKeyboardButton(text="🔎 جستجوی سرویس", callback_data="service_search", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen="my_configs_list_has", inline_keyboard=buttons)


def service_search_results_keyboard(configs, query: str):
    buttons = [
        [InlineKeyboardButton(text=f"🔎 {cfg.get('service_id') or cfg['plan']}", callback_data=f"viewconfig_{cfg['id']}", style="primary")]
        for cfg in configs
    ]
    buttons.append([InlineKeyboardButton(text="🔁 جستجوی دوباره", callback_data="service_search", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به سرویس‌های من", callback_data="my_configs", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _copy_sub_link_button(url: str, fallback_cb: str):
    """دکمه‌ی «کپی لینک ساب»؛ تلگرام برای copy_text حداکثر ۲۵۶ کاراکتر می‌پذیرد،
    برای لینک‌های بلندتر دکمه‌ی callback می‌گذاریم که لینک را در پیام قابل‌کپی می‌فرستد."""
    url = str(url or "").strip()
    if 0 < len(url) <= 256:
        return InlineKeyboardButton(text="📋 کپی لینک ساب", copy_text=CopyTextButton(text=url), style="primary", ui_screen="config_detail", ui_button_key="copy_sublink")
    return InlineKeyboardButton(text="📋 کپی لینک ساب", callback_data=fallback_cb, style="primary", ui_screen="config_detail", ui_button_key="copy_sublink")


def is_valid_webapp_url(url: str) -> bool:
    """آیا این آدرس را تلگرام برای دکمه‌ی Web App می‌پذیرد؟

    تلگرام فقط https با یک دامنه‌ی واقعی (حتماً دارای «.») را قبول می‌کند. آدرسی مثل
    «https://TGsinaa» (بدون دامنه) باعث می‌شد کل پیام با خطای «Wrong HTTP URL» رد شود و
    کاربر در «سرویس‌های من» فقط «خطا در نمایش جزئیات سرویس» ببیند."""
    from urllib.parse import urlparse
    try:
        u = str(url or "").strip()
        if not u.lower().startswith("https://") or any(ch.isspace() for ch in u):
            return False
        host = (urlparse(u).hostname or "").strip(".")
        if not host or "." not in host:
            return False
        if host.lower() in ("localhost",) or host.replace(".", "").isdigit():
            return False
        return True
    except Exception:
        return False


def _miniapp_service_url(cfg_id) -> str:
    """Mini app page URL of one service, or "" when no VALID mini app URL is configured."""
    import os
    try:
        base = (os.environ.get("MINIAPP_WEBAPP_URL", "") or "").strip().rstrip("/")
        if not is_valid_webapp_url(base):
            base = (db.get_setting("miniapp_webapp_url", "") or "").strip().rstrip("/")
        if not is_valid_webapp_url(base):
            return ""
        return f"{base}/services/{int(cfg_id)}"
    except Exception:
        return ""


def config_detail_keyboard(cfg_id, sub_link_url: str | None = None, has_qr: bool = False, back_callback: str = "my_configs_vip", sub_link_disabled: bool = False, can_manage_link: bool = False, panel_direct_configs: bool = False, allow_renew: bool = True, can_change_link: bool | None = None, is_wireguard: bool = False):
    """کیبورد جزئیات سرویس VIP/PAYG: تمدید + کیوآرکد + لینک ساب + حذف سرویس."""
    buttons = []
    if allow_renew:
        buttons.append([InlineKeyboardButton(text="🔁 تمدید سرویس", callback_data=f"renewcfg_{cfg_id}", style="success")])
    else:
        buttons.append([InlineKeyboardButton(text="💰 شارژ کیف پول", callback_data="wallet", style="success")])
    # وقتی لینک ساب خود کاربر غیرفعال کرده، دکمههای بازکردن/دریافت کانفیگ از روی آن معنا ندارند
    # (چون خود لینک فعلاً در پنل غیرفعال است و هیچ کانفیگی برنمی‌گرداند)
    # تا کاربر به جای یک خطای گنگ متوجه شود وضعیت فعلی را ببیند و اول لینک را فعال کند.
    row = []
    if has_qr:
        row.append(InlineKeyboardButton(text="🖼 مشاهده کیوآرکد", callback_data=f"viewqr_{cfg_id}", style="primary"))
    if sub_link_url and not sub_link_disabled:
        _app_url = _miniapp_service_url(cfg_id)
        if _app_url:
            row.append(InlineKeyboardButton(text="🔗 باز کردن لینک ساب", web_app=WebAppInfo(url=_app_url), style="primary", ui_screen="config_detail", ui_button_key="open_sublink"))
        else:
            row.append(InlineKeyboardButton(text="🔗 باز کردن لینک ساب", url=sub_link_url, style="primary", ui_screen="config_detail", ui_button_key="open_sublink"))
    if row:
        buttons.append(row)
    if sub_link_url and not sub_link_disabled:
        buttons.append([_copy_sub_link_button(sub_link_url, f"copysub_{cfg_id}")])
    if is_wireguard:
        buttons.append([InlineKeyboardButton(text="📁 دریافت فایل", callback_data=f"wgfiles_{cfg_id}", style="success", ui_screen="config_detail", ui_button_key="wgfiles_")])
    elif (sub_link_url and not sub_link_disabled) or panel_direct_configs:
        buttons.append([InlineKeyboardButton(text="🔗 دریافت کانفیگ‌های تکی", callback_data=f"mirrorconfigs_{cfg_id}", style="success")])
    if can_change_link is None:
        can_change_link = can_manage_link
    if can_change_link:
        buttons.append([InlineKeyboardButton(text="🔄 تغییر لینک ساب", callback_data=f"usersvclink_{cfg_id}", style="primary")])
    if can_manage_link:
        if sub_link_disabled:
            buttons.append([InlineKeyboardButton(text="▶️ فعال‌کردن لینک ساب", callback_data=f"usersvcenable_{cfg_id}", style="success")])
        else:
            buttons.append([InlineKeyboardButton(text="⏸ غیرفعال‌کردن لینک ساب", callback_data=f"usersvcdisable_{cfg_id}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"delconfig_{cfg_id}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به سرویس‌های VIP من", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen="config_detail", inline_keyboard=buttons)


def confirm_change_sublink_keyboard(cfg_id, back_callback: str = "my_configs_vip"):
    """پیام هشدار قبل از تغییر واقعی لینک ساب (قطع دسترسی سایرین)."""
    return InlineKeyboardMarkup(ui_screen="config_detail", inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"viewconfig_{cfg_id}", style="danger", ui_screen="config_detail", ui_button_key="viewconfig_")],
        [InlineKeyboardButton(text="✅ تغییر لینک ساب", callback_data=f"usersvclinkconfirm_{cfg_id}", style="success", ui_screen="config_detail", ui_button_key="usersvclinkconfirm_")],
    ])


def confirm_delete_config_keyboard(cfg_id):
    return InlineKeyboardMarkup(ui_screen="config_delete_confirm", inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"delconfirm_{cfg_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"viewconfig_{cfg_id}", style="danger")],
    ])


# ---------------------------------------------------------------------------
# پنل ادمین
# ---------------------------------------------------------------------------
def admin_panel_menu(orders_enabled: bool = True, permissions: set[str] | None = None, is_main_admin: bool = True):
    """Top admin inline menu.

    Uses the exact same capability registry as the bottom admin keyboard.
    Two buttons are intentionally placed on every row.
    """
    buttons = []
    row = []
    for perm, text, cb, style in _admin_menu_items(orders_enabled):
        if not _admin_menu_allowed(perm, permissions, is_main_admin):
            continue
        row.append(InlineKeyboardButton(text=text, callback_data=cb, style=style))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if not buttons:
        buttons.append([InlineKeyboardButton(text="⛔ بدون دسترسی", callback_data="noop", style="danger")])
    return InlineKeyboardMarkup(ui_screen="admin_panel", inline_keyboard=buttons)

def admin_manage_admins_keyboard(admins=None):
    rows=[[InlineKeyboardButton(text=f"👤 {a.get('name') or a['telegram_id']} — {a['telegram_id']}", callback_data=f"subadm_{a['telegram_id']}", style="primary")] for a in (admins or [])]
    rows.append([InlineKeyboardButton(text="➕ افزودن ادمین فرعی", callback_data="subadm_add", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_permissions_keyboard(admin_id: str, selected=None):
    selected=set(selected or [])
    rows=[]
    for key,label in PERMISSION_LABELS.items():
        mark="✅" if key in selected else "☑️"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"subadmperm_{admin_id}:{key}", style="success" if key in selected else "primary")])
    rows.append([InlineKeyboardButton(text="🗑 حذف این ادمین", callback_data=f"subadmdel_{admin_id}", style="danger")])
    rows.append([InlineKeyboardButton(text="🔙 لیست ادمین‌ها", callback_data="admin_manage_admins", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_order_log_channel_keyboard(has_channel: bool = True):
    rows = []
    if has_channel:
        rows.append([InlineKeyboardButton(text="🗑 حذف کانال یا گروه لاگ سفارشات", callback_data="admin_order_log_channel_del", style="danger")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_log_settings", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")]])


def admin_userlist_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 مشتریان فعال (خریدکرده)", callback_data="admin_userlist_active", style="success")],
        [InlineKeyboardButton(text="👥 کل کاربران", callback_data="admin_userlist_all", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])


def admin_discount_menu(discounts: list | None = None):
    buttons = []
    for d in (discounts or []):
        value_text = f"{d['amount']:,}ت" if d.get("discount_type") == "amount" else f"{d['percent']}٪"
        buttons.append([InlineKeyboardButton(
            text=f"🎟 {d['code']} | {value_text} | 🔁 {d['uses']}",
            callback_data=f"discdetail_{d['id']}", style="primary",
        )])
    buttons.append([InlineKeyboardButton(text="➕ ساخت کد تخفیف جدید", callback_data="new_discount", style="primary")])
    buttons.append([InlineKeyboardButton(text="🎁 هدیه همگانی", callback_data="admin_gift_all", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _discount_audience_labels() -> dict:
    from database import AUDIENCE_LABELS
    d = dict(AUDIENCE_LABELS)
    d["specific"] = "🎯 فقط افراد خاص (با آیدی)"
    return d


_DISCOUNT_AUDIENCE_LABELS = _discount_audience_labels()


def discount_detail_keyboard(discount_id: int, first_purchase_only: bool = False, audience: str = "all"):
    fp_text = "🆕 تخفیف اولین خرید: ✅ فعال (بزن تا خاموش شود)" if first_purchase_only \
        else "🆕 تخفیف اولین خرید: ❌ غیرفعال (بزن تا فعال شود)"
    aud_label = _DISCOUNT_AUDIENCE_LABELS.get(audience, audience)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 ویرایش مقدار تخفیف", callback_data=f"discedit_value_{discount_id}", style="primary")],
        [InlineKeyboardButton(text=f"👥 مخاطب: {aud_label}", callback_data=f"discedit_audience_{discount_id}", style="success")],
        [InlineKeyboardButton(text="🎯 ویرایش پلن‌های مجاز", callback_data=f"discedit_plans_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="🔁 ویرایش تعداد استفاده", callback_data=f"discedit_uses_{discount_id}", style="success")],
        [InlineKeyboardButton(text="💰 ویرایش حداقل مبلغ سفارش", callback_data=f"discedit_minorder_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="🔂 ویرایش سقف استفاده هر کاربر", callback_data=f"discedit_maxuser_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="⏰ ویرایش تاریخ انقضا", callback_data=f"discedit_expiry_{discount_id}", style="primary")],
        [InlineKeyboardButton(text=fp_text, callback_data=f"discedit_firstpurchase_{discount_id}", style="success" if first_purchase_only else None)],
        [InlineKeyboardButton(text="🗑 حذف کد تخفیف", callback_data=f"discdelete_{discount_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_discount", style="primary")],
    ])


# 🆕 دسته‌بندی مخاطب کد تخفیف — دقیقاً هم‌خانواده با admin_broadcast_audience_keyboard
# پایین‌تر؛ «افراد خاص» به فلوی موجود discedit_users_ (وارد کردن آیدی عددی) می‌رود.
def discount_audience_keyboard(discount_id: int, current: str = "all"):
    from database import AUDIENCE_LABELS
    def mark(v):
        return "✅ " if current == v else ""
    btns = [InlineKeyboardButton(text=f"{mark(k)}{lbl}", callback_data=f"discaud_{k}_{discount_id}", style="primary")
            for k, lbl in AUDIENCE_LABELS.items()]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text=f"{mark('specific')}🎯 فقط افراد خاص (با آیدی عددی)", callback_data=f"discedit_users_{discount_id}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"discdetail_{discount_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 📣 لینک‌های تبلیغاتی (کمپین) — برای هر تبلیغ یک کد اختصاصی ساخته می‌شود و
# لینکش (t.me/<bot>?start=c_<code>) منتشر می‌شود؛ آمار عضو/تبدیل/درآمد هر
# کمپین همین‌جا قابل مشاهده است.
# ---------------------------------------------------------------------------
# 🆕 بند ۱۸ — انتخاب مخاطب پیام همگانی
def admin_broadcast_audience_keyboard():
    """انتخاب مخاطب پیام همگانی — ۱۴ دسته (بند ۱ + ۲۲)."""
    OPTS = [
        ("👥 همه",         "bcaud_all"),
        ("🛒 خریدار",      "bcaud_buyers"),
        ("🆕 بی‌خرید",     "bcaud_non_buyers"),
        ("💰 کیف+",        "bcaud_wallet_positive"),
        ("🤝 نماینده",     "bcaud_agents"),
        ("🟢 فعال",        "bcaud_active_service"),
        ("🔴 منقضی",       "bcaud_expired_service"),
        ("🆕 ۷روزه",       "bcaud_new_users"),
        ("📵 بی‌سرویس",    "bcaud_no_service"),
        ("📦 چندسرویس",    "bcaud_multi_service"),
        ("😴 غیرفعال",     "bcaud_inactive"),
        ("🎁 تست",         "bcaud_has_test"),
        ("🎟 تخفیف",       "bcaud_has_discount"),
        ("💳 PAYG",        "bcaud_sufficient_wallet"),
    ]
    rows = []
    for i in range(0, len(OPTS), 3):
        row = [InlineKeyboardButton(text=text, callback_data=data, style="primary") for text, data in OPTS[i:i+3]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# 🆕 بند ۴ — تأیید نهایی هدیه همگانی
def admin_gift_all_confirm_keyboard(amount: int, audience: str = "all"):
    """تأیید هدیه هدف‌مند — نمایش مخاطب در دکمه (بند ۲۵)."""
    try:
        from database import AUDIENCE_LABELS
        aud_lbl = AUDIENCE_LABELS.get(audience, audience)
    except Exception:
        aud_lbl = audience
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ {amount:,} تومان → {aud_lbl}",
            callback_data=f"giftdo_{amount}_{audience}",
            style="success")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_back", style="danger")],
    ])


def admin_campaigns_menu(campaigns: list | None = None):
    buttons = []
    for c in (campaigns or []):
        status = "🟢" if c.get("is_active", 1) else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {c['name']} ({c['code']})",
            callback_data=f"campdetail_{c['id']}", style="primary",
        )])
    buttons.append([InlineKeyboardButton(text="➕ ساخت کمپین جدید", callback_data="new_campaign", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_discount", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def campaign_detail_keyboard(campaign: dict):
    toggle_text = "⏸ غیرفعال کردن لینک" if campaign.get("is_active", 1) else "▶️ فعال کردن لینک"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"camptoggle_{campaign['id']}", style="primary")],
        [InlineKeyboardButton(text="✏️ تغییر نام کمپین", callback_data=f"campedit_{campaign['id']}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف کمپین", callback_data=f"campdelete_{campaign['id']}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_campaigns", style="primary")],
    ])


def campaign_delete_confirm_keyboard(campaign_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"campdeleteconfirm_{campaign_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"campdetail_{campaign_id}", style="danger")],
    ])


def discount_delete_confirm_keyboard(discount_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"discdeleteconfirm_{discount_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"discdetail_{discount_id}", style="danger")],
    ])


def admin_user_actions_keyboard(uid: str, is_blocked: bool = False, show_pm_link: bool = True):
    block_btn = (
        InlineKeyboardButton(text="✅ رفع مسدودیت کاربر", callback_data=f"toggleblock_{uid}", style="success")
        if is_blocked else
        InlineKeyboardButton(text="🚫 مسدود کردن کاربر", callback_data=f"toggleblock_{uid}", style="danger")
    )
    pm_row = [InlineKeyboardButton(text="✉️ پیام خصوصی به کاربر", callback_data=f"pm_{uid}", style="primary")]
    # دکمه‌ی "رفتن به پیوی کاربر" (لینک tg://user) برای برخی کاربران با تنظیمات حریم‌خصوصی محدودتر
    # توسط تلگرام رد می‌شود، پس handlers/admin.py در صورت خطای BUTTON_USER_PRIVACY_RESTRICTED همین کیبورد را با
    # show_pm_link=False دوباره می‌سازد تا فقط همین دکمه حذف شود.
    if show_pm_link:
        pm_row.append(InlineKeyboardButton(text="💬 رفتن به پیوی کاربر", url=f"tg://user?id={uid}", style="primary"))
    return InlineKeyboardMarkup(inline_keyboard=[
        pm_row,
        [InlineKeyboardButton(text="💰 شارژ دستی", callback_data=f"custom_{uid}", style="primary")],
        # 🆕 بند ۶: ویرایش مستقیم موجودی کیف‌پول، *بدون* هیچ پیام اطلاع‌رسانی
        # به کاربر (برخلاف «شارژ دستی» بالا که به کاربر خبر می‌دهد).
        [InlineKeyboardButton(text="✏️ ویرایش موجودی کیف‌پول (بی‌صدا)", callback_data=f"walletedit_{uid}", style="primary")],
        [InlineKeyboardButton(text="📒 حسابداری کاربر (تراکنش‌ها/منشأ پول)", callback_data=f"accounting_{uid}_0", style="primary")],
        [InlineKeyboardButton(text="🚀 ارسال دستی کانفیگ", callback_data=f"sendvip_{uid}", style="primary"),
         InlineKeyboardButton(text="⚡ ارسال خودکار کانفیگ", callback_data=f"autosend_{uid}", style="success")],
        [InlineKeyboardButton(text="📦 مشاهده و مدیریت سرویس‌های کاربر", callback_data=f"svcs_{uid}", style="primary")],
        [block_btn],
        [InlineKeyboardButton(text="🗑 حذف کامل کاربر از دیتابیس", callback_data=f"deleteuser_{uid}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])


def admin_pm_cancel_keyboard(uid: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف از پیام خصوصی", callback_data=f"useropen_{uid}", style="danger")],
    ])


def admin_charge_approval_keyboard(uid: str, amount: int, receipt_id: int | None = None):
    suffix = f"_{receipt_id}" if receipt_id is not None else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ تأیید {amount:,}", callback_data=f"approve_{uid}_{amount}{suffix}", style="success")],
        [InlineKeyboardButton(text="💵 مبلغ دلخواه", callback_data=f"custom_{uid}{suffix}", style="primary")],
        [InlineKeyboardButton(text="❌ رد", callback_data=f"reject_{uid}{suffix}", style="danger")],
    ])


def admin_purchase_card_approval_keyboard(uid: str, plan_key: str, price: int, receipt_id: int | None = None):
    approve_cb = f"approvepay|{uid}|{plan_key}|{price}|{receipt_id}" if receipt_id is not None else f"approvepay|{uid}|{plan_key}|{price}"
    if len(approve_cb.encode("utf-8")) > 64 and receipt_id is not None:
        # محدودیت ۶۴ بایتی callback_data تلگرام: کلید پلن را از روی خودِ رسید برمی‌داریم.
        approve_cb = f"approvepay|{uid}|@|{price}|{receipt_id}"
    reject_cb = f"rejectpay|{uid}|{receipt_id}" if receipt_id is not None else f"rejectpay|{uid}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ تأیید پرداخت ({price:,} ت)", callback_data=approve_cb, style="success")],
        [InlineKeyboardButton(text="❌ رد رسید", callback_data=reject_cb, style="danger")],
    ])


def admin_purchase_notify_keyboard(uid: str, plan_key: str | None = None, order_id: int | None = None):
    suffix = f"|{order_id}" if order_id else ""
    oid = order_id or 0

    auto_row = []
    if plan_key:
        mapping = db.get_panel_map_for_plan_key(plan_key)
        if mapping and mapping.get("enabled"):
            type_label = PANEL_TYPE_LABELS.get(mapping.get("panel_type"), mapping.get("panel_type"))
            auto_row = [[InlineKeyboardButton(
                text=f"📤 ارسال خودکار از پنل {type_label}", callback_data=f"panelsend|{uid}|{plan_key}|{oid}", style="primary"
            )]]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 ارسال کانفیگ VIP — دستی", callback_data=f"sendvip_{uid}{suffix}", style="primary")],
        *auto_row,
    ])


def admin_custom_order_card_approval_keyboard(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید پرداخت", callback_data=f"approvecustom_{order_id}", style="success")],
        [InlineKeyboardButton(text="❌ رد رسید", callback_data=f"rejectcustom_{order_id}", style="danger")],
    ])


def admin_custom_order_notify_keyboard(order_id: int):
    buttons = [
        [InlineKeyboardButton(text="📤 شروع ارسال کانفیگ — دستی", callback_data=f"sendcustomorder_{order_id}", style="primary")],
    ]
    if db.list_vpn_panels(enabled_only=True):
        buttons.append([InlineKeyboardButton(
            text="🧩 ساخت خودکار از یک پنل (کانفیگ خودتو بساز)",
            callback_data=f"panelcustom_{order_id}", style="primary",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def config_delivery_keyboard(guide_url: str = "", is_test: bool = False, wg_cfg_id: int | None = None):
    buttons = []
    if wg_cfg_id:
        buttons.append([InlineKeyboardButton(
            text="📁 دریافت فایل", callback_data=f"wgfiles_{int(wg_cfg_id)}", style="success",
            ui_screen="config_delivery", ui_button_key="wgfiles_",
        )])
    if guide_url and guide_url.strip().lower().startswith(("http://", "https://")):
        buttons.append([InlineKeyboardButton(
            text="🧑‍🦯 دریافت روش اتصال", url=guide_url, style="primary",
            ui_screen="config_delivery", ui_button_key="guide",
        )])
    return InlineKeyboardMarkup(ui_screen="config_delivery", inline_keyboard=buttons) if buttons else None


def ticket_reply_keyboard(uid: str):
    return InlineKeyboardMarkup(ui_screen="ticket_reply", inline_keyboard=[
        [InlineKeyboardButton(text="↩️ پاسخ", callback_data=f"replyticket_{uid}", style="primary", ui_screen="ticket_reply", ui_button_key="replyticket_")],
    ])


# ---------------------------------------------------------------------------
# 🎫 مدیریت تیکت‌ها (سمت ادمین) — قبلاً تیکت اصلاً حافظه‌ی پایدار نداشت (فقط
# یک پیام فوروارد‌شده با یک دکمه‌ی پاسخ)، پس هیچ لیست/وضعیت/تاریخچه‌ای هم
# ممکن نبود. الان با جدول tickets/ticket_messages، همه‌ی این‌ها اضافه شده.
# ---------------------------------------------------------------------------
def admin_tickets_menu(counts: dict):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🟢 باز ({counts.get('open', 0)})", callback_data="admtickets_open_0", style="success")],
        [InlineKeyboardButton(text=f"🟡 پاسخ‌داده‌شده ({counts.get('answered', 0)})", callback_data="admtickets_answered_0", style="primary")],
        [InlineKeyboardButton(text=f"🔴 بسته‌شده ({counts.get('closed', 0)})", callback_data="admtickets_closed_0", style="primary")],
        [InlineKeyboardButton(text="📋 همه‌ی تیکت‌ها", callback_data="admtickets_all_0", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def admin_tickets_list_keyboard(tickets: list[dict], status: str, offset: int, has_more: bool):
    buttons = [
        [InlineKeyboardButton(
            text=f"{_TICKET_STATUS_BADGE.get(t['status'], t['status'])} — #{t['id']} — {t.get('user_name') or 'کاربر'}",
            callback_data=f"admticketview_{t['id']}", style="primary",
        )]
        for t in tickets
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"admtickets_{status}_{max(0, offset - 10)}", style="primary"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️ بعدی", callback_data=f"admtickets_{status}_{offset + 10}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_tickets", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_ticket_detail_keyboard(ticket: dict):
    buttons = []
    if ticket["status"] != "closed":
        buttons.append([InlineKeyboardButton(text="↩️ پاسخ", callback_data=f"admticketreply_{ticket['id']}", style="primary")])
        buttons.append([InlineKeyboardButton(text="✅ بستن تیکت", callback_data=f"admticketclose_{ticket['id']}", style="danger")])
    else:
        buttons.append([InlineKeyboardButton(text="🔓 بازگشایی تیکت", callback_data=f"admticketreopen_{ticket['id']}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data=f"admtickets_{ticket['status']}_0", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 📦 مدیریت سرویس‌های کاربران توسط ادمین
# ---------------------------------------------------------------------------
def admin_services_list_keyboard(configs, uid: str):
    buttons = []
    for cfg in configs:
        icon = "🚀"
        mark = "❌ " if cfg.get("deleted") else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{icon} {cfg['plan']}", callback_data=f"svcdetail_{cfg['id']}"
        , style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"useractions_{uid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_service_detail_keyboard(cfg: dict, uid: str, sub_link_url: str | None = None):
    cfg_id = cfg["id"]
    is_deleted = bool(cfg.get("deleted"))
    is_vip = True
    buttons = []

    if is_deleted:
        buttons.append([InlineKeyboardButton(text="♻️ بازگردانی سرویس", callback_data=f"svcrestore_{cfg_id}", style="primary")])
        buttons.append([InlineKeyboardButton(text="🗑 حذف همیشگی (غیرقابل بازگشت)", callback_data=f"svcpurge_{cfg_id}", style="danger")])
    else:
        panel_managed = cfg.get("source") in PANEL_TYPES and cfg.get("service_id") and cfg.get("panel_id")
        if sub_link_url:
            buttons.append([_copy_sub_link_button(sub_link_url, f"copysub_{cfg_id}")])
        if panel_managed:
            buttons.append([InlineKeyboardButton(text="🔄 تغییر لینک از پنل", callback_data=f"panelrevoke_{cfg_id}", style="primary")])
        else:
            buttons.append([InlineKeyboardButton(text="✏️ تغییر لینک ساب", callback_data=f"svcedit_link_{cfg_id}", style="primary")])
        if cfg.get("qr_file_id"):
            buttons.append([InlineKeyboardButton(text="🖼 تغییر عکس کیوآرکد", callback_data=f"svcedit_qr_{cfg_id}", style="primary")])

        if panel_managed and db.is_renewal_enabled():  # سوئیچ سراسری تمدید خاموش: بدون دکمه‌ی تمدید
            buttons.append([InlineKeyboardButton(text="🔁 تمدید از پنل", callback_data=f"panelrenew_{cfg_id}", style="success")])
            if cfg.get("sub_link_disabled"):
                buttons.append([InlineKeyboardButton(text="▶️ فعال‌کردن لینک ساب", callback_data=f"panelenable_{cfg_id}", style="success")])
            else:
                buttons.append([InlineKeyboardButton(text="⏸ غیرفعال‌کردن لینک ساب", callback_data=f"paneldisable_{cfg_id}", style="danger")])

        buttons.append([InlineKeyboardButton(text="🗑 حذف سرویس (مخفی از کاربر)", callback_data=f"svcdelete_{cfg_id}", style="danger")])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست سرویس‌ها", callback_data=f"svcs_{uid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_purge_confirm_keyboard(cfg_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، برای همیشه حذف کن", callback_data=f"svcpurgeconfirm_{cfg_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"svcdetail_{cfg_id}", style="danger")],
    ])


def admin_request_queue_menu(order_count: int = 0, receipt_count: int = 0):
    order_label = f"📦 سفارش‌های در انتظار ({order_count})" if order_count else "📦 سفارش‌های در انتظار"
    receipt_label = f"🧾 رسیدهای در انتظار تایید ({receipt_count})" if receipt_count else "🧾 رسیدهای در انتظار تایید"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=order_label, callback_data="admin_order_queue", style="primary")],
        [InlineKeyboardButton(text=receipt_label, callback_data="admin_pending_receipts", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])


def admin_pending_receipts_keyboard(receipts, custom_receipts):
    """لیست رسیدهای در انتظار: هر ردیف فقط «باز کردن» رسید است (نه تأیید/رد مستقیم)؛
    با زدن روی آن، خودِ رسید (عکس/متن) نمایش داده می‌شود و زیرش دکمه‌ی تأیید/رد قرار دارد."""
    buttons = []
    kind_icon = {"charge": "💰", "plan_card": "💳", "plan_crypto": "💱"}
    for r in receipts:
        icon = kind_icon.get(r["kind"], "🧾")
        label = f"{icon} #{r['id']} | {r['label']} | {int(r['amount']):,} ت | {r['telegram_id']}"
        buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"rcptview_{r['id']}", style="primary")])
    for co in custom_receipts:
        label = f"🛠 سفارشی #{co['id']} | {co['volume_gb']}GB/{co['days']}روز | {int(co['price']):,} ت"
        buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"rcptviewc_{co['id']}", style="primary")])
    if receipts or custom_receipts:
        buttons.append([InlineKeyboardButton(text="🧹 علامت‌گذاری همه به‌عنوان بررسی‌شده", callback_data="clearreceipts_confirm", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_request_queue", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def with_back_row(markup, text: str, callback_data: str):
    """یک ردیف «بازگشت» به انتهای یک کیبورد موجود اضافه می‌کند."""
    rows = [list(r) for r in (markup.inline_keyboard if markup else [])]
    rows.append([InlineKeyboardButton(text=text, callback_data=callback_data, style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_clear_receipts_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، همه رو علامت بزن", callback_data="clearreceipts_do", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_pending_receipts", style="danger")],
    ])


def admin_order_queue_keyboard(orders, custom_orders=None):
    """صف سفارش‌های در انتظار ارسال: هر ردیف جزئیات سفارش را باز می‌کند و از آنجا
    ادمین «ارسال دستی» یا «ارسال از پنل» را انتخاب می‌کند.
    orders: هر آیتم باید کلید 'telegram_id' هم داشته باشد (توسط admin.py اضافه می‌شود)."""
    buttons = []
    for o in orders:
        buttons.append([
            InlineKeyboardButton(
                text=f"🚀 #{o['id']} | {o['plan_name']} | {int(o['price']):,} ت | {o.get('telegram_id', '')}"[:60],
                callback_data=f"qordview_{o['id']}", style="primary",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"dismissorder_{o['id']}", style="danger"),
        ])
    for co in custom_orders or []:
        buttons.append([
            InlineKeyboardButton(
                text=f"🛠 سفارشی #{co['id']} | {co['volume_gb']}GB/{co['days']}روز | {int(co['price']):,} ت"[:60],
                callback_data=f"qcustview_{co['id']}", style="primary",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"dismisscustomorder_{co['id']}", style="danger"),
        ])
    if orders or custom_orders:
        buttons.append([InlineKeyboardButton(text="🧹 پاک کردن همه‌ی سفارش‌های این صف", callback_data="clearorders_confirm", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_request_queue", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_clear_orders_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، همه رو پاک کن", callback_data="clearorders_do", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_order_queue", style="danger")],
    ])


# ---------------------------------------------------------------------------
# 👥 لیست کاربران با صفحه‌بندی ۱۰تا۱۰تا (مرتب‌شده بر اساس بیشترین خرید)
# ---------------------------------------------------------------------------
_USERLIST_SORT_BUTTONS = (
    ("tx", "بیشترین تراکنش", "success"),
    ("svc", "بیشترین سرویس", "success"),
    ("last", "آخرین تراکنش", "success"),
    ("new", "جدیدترین عضو", "primary"),
    ("wal", "موجودی کیف پول", "primary"),
    ("nobuy", "بدون خرید", "danger"),
)


def _userlist_row_text(u: dict, sort: str) -> str:
    name = str(u.get("name") or "-")
    m = u.get("metric")
    if sort == "tx":
        return f"👤 {name} | 💰 {int(m or 0):,} ت"
    if sort == "svc":
        return f"👤 {name} | 📦 {int(m or 0):,} سرویس"
    if sort == "last":
        return f"👤 {name} | 🕒 {m or '-'}"
    if sort == "wal":
        return f"👤 {name} | 👛 {int(m or 0):,} ت"
    return f"👤 {name} | 🆔 {u.get('telegram_id')}"


def admin_userlist_page_keyboard(users: list, page: int, has_next: bool, sort: str = "new"):
    """لیست کاربران: دو ردیف سه‌تایی دکمه‌ی سورت + کاربران + صفحه‌بندی؛ مقدار هر سورت روی دکمه‌ی کاربر."""
    sort_btns = [
        InlineKeyboardButton(text=("✔️ " if key == sort else "") + label, callback_data=f"ul_{key}_0", style=style)
        for key, label, style in _USERLIST_SORT_BUTTONS
    ]
    buttons = [sort_btns[:3], sort_btns[3:]]
    for u in users:
        buttons.append([InlineKeyboardButton(
            text=_userlist_row_text(u, sort), callback_data=f"useropen_{u['telegram_id']}", style="primary",
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ صفحه قبل", callback_data=f"ul_{sort}_{page - 1}", style="primary"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️ صفحه بعد", callback_data=f"ul_{sort}_{page + 1}", style="primary"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    # مارکاپ خام: چیدمان سه‌تایی/دوتایی دقیقاً همین‌طور بماند (لایه‌ی ویرایشگر دکمه‌ها ردیف‌ها را تک‌دکمه‌ای می‌کرد)
    return _RealInlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 📚 راهنما و اموزش — فهرست قابل‌رشد از پنل ادمین (متن/عکس/فیلم)
# ---------------------------------------------------------------------------
def user_guides_menu(guides: list):
    if not guides:
        buttons = []
    else:
        buttons = []
        for g in guides:
            title = str(g.get("title") or "")
            # ایموجی پریمیومِ عنوان راهنما (ذخیره‌شده هنگام ساخت/ویرایش عنوان) مستقیم به‌عنوان آیکن دکمه
            # گذاشته می‌شود؛ قبلاً فقط اگر عنوان در رجیستری متن‌ها بود پریمیوم می‌شد.
            emoji_id = db.get_button_custom_emoji_id("text:" + title)
            if emoji_id:
                try:
                    import rich_registry
                    title = rich_registry.strip_first_emoji(title)
                except Exception:
                    pass
            buttons.append([InlineKeyboardButton(
                text=title, callback_data=f"guideopen_{g['id']}", style="primary",
                icon_custom_emoji_id=emoji_id or None)])
    buttons.append([InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="primary")])
    return InlineKeyboardMarkup(ui_screen="guides_has", inline_keyboard=buttons)


def user_guide_detail_keyboard():
    return InlineKeyboardMarkup(ui_screen="guides_has", inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به لیست راهنما", callback_data="user_guides", style="primary")],
    ])


def admin_guides_menu(guides: list):
    buttons = []
    for i, g in enumerate(guides):
        buttons.append([InlineKeyboardButton(text=f"{g['title']}", callback_data=f"guideadminopen_{g['id']}", style="primary")])
    buttons.append([InlineKeyboardButton(text="➕ افزودن راهنما/اموزش جدید", callback_data="guidenew", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_guide_detail_keyboard(guide_id: int, index: int, total: int):
    move_row = []
    if index > 0:
        move_row.append(InlineKeyboardButton(text="⬆️ بالاتر", callback_data=f"guidemove_{guide_id}_up", style="primary"))
    if index < total - 1:
        move_row.append(InlineKeyboardButton(text="⬇️ پایین‌تر", callback_data=f"guidemove_{guide_id}_down", style="primary"))
    buttons = [move_row] if move_row else []
    buttons += [
        [InlineKeyboardButton(text="✏️ ویرایش عنوان", callback_data=f"guideeditname_{guide_id}", style="primary")],
        [InlineKeyboardButton(text="📝 ویرایش محتوا (متن/عکس/فیلم)", callback_data=f"guideeditcontent_{guide_id}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این راهنما", callback_data=f"guidedelete_{guide_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست راهنما", callback_data="admin_guides", style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_guide_delete_confirm_keyboard(guide_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"guidedeleteconfirm_{guide_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"guideadminopen_{guide_id}", style="danger")],
    ])


def admin_guide_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_guides", style="danger")],
    ])


def admin_stickers_menu(sections: list[dict]):
    """sections: [{"key": ..., "label": ..., "status_emoji": ...}, ...]"""
    buttons = [
        [InlineKeyboardButton(
            text=f"{s['status_emoji']} {s['label']}",
            callback_data=f"stickeropen_{s['key']}",
            style="primary",
        )]
        for s in sections
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_sticker_detail_keyboard(section_key: str, has_custom: bool, is_enabled: bool):
    buttons = [
        [InlineKeyboardButton(text="📤 آپلود/تغییر استیکر", callback_data=f"stickerset_{section_key}", style="success")],
    ]
    if is_enabled:
        buttons.append([InlineKeyboardButton(text="🛑 غیرفعال کردن (بدون استیکر)", callback_data=f"stickeroff_{section_key}", style="danger")])
    else:
        buttons.append([InlineKeyboardButton(text="✅ فعال‌سازی دوباره", callback_data=f"stickeron_{section_key}", style="success")])
    if has_custom:
        buttons.append([InlineKeyboardButton(text="♻️ بازگرداندن به پیش‌فرض", callback_data=f"stickerreset_{section_key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست بخش‌ها", callback_data="admin_stickers", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_sticker_cancel_keyboard(section_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"stickeropen_{section_key}", style="danger")],
    ])


def admin_error_logs_keyboard(logs: list):
    buttons = []
    for log in logs:
        ts = str(log.get("occurred_at") or "")[:16]
        buttons.append([InlineKeyboardButton(
            text=f"⚠️ {ts} | {log['error_type']}",
            callback_data=f"errlogdetail_{log['id']}", style="danger",
        )])
    if logs:
        buttons.append([InlineKeyboardButton(text="🗑 این لاگ پاک‌سازیشون", callback_data="errlogclear", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data="errlogrefresh", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔗 راهنمای فعال‌سازی Sentry", callback_data="errlogsentryguide", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_error_log_detail_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به لیست لاگ‌ها", callback_data="errlogrefresh", style="primary")],
    ])


def admin_error_logs_clear_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، پاکشون", callback_data="errlogclearconfirm", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="errlogrefresh", style="danger")],
    ])


def admin_referrers_page_keyboard(users: list, page: int, has_next: bool):
    buttons = []
    for u in users:
        buttons.append([InlineKeyboardButton(
            text=f"🤝 {u['name']} | 👥 دعوت: {u['invited_count']} | ✅ موفق: {u['successful_invites']}",
            callback_data=f"refdetail_{u['telegram_id']}_{page}", style="primary",
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ صفحه قبل", callback_data=f"refpage_{page - 1}", style="primary"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️ صفحه بعد", callback_data=f"refpage_{page + 1}", style="primary"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_referred_detail_keyboard(referrer_uid: str, back_page: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 مشاهدهی کامل کاربر دعوت‌کننده", callback_data=f"useropen_{referrer_uid}", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست دعوت‌کنندگان", callback_data=f"refpage_{back_page}", style="primary")],
    ])


def admin_accounting_keyboard(uid: str, page: int, has_next: bool):
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ قبل", callback_data=f"accounting_{uid}_{page - 1}", style="primary"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️ بعد", callback_data=f"accounting_{uid}_{page + 1}", style="primary"))
    buttons = [nav_row] if nav_row else []
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به کاربر", callback_data=f"useropen_{uid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🎟 ساخت کد تخفیف — نوع تخفیف و پلن‌های قابل‌اعمال
# ---------------------------------------------------------------------------
def discount_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 درصدی", callback_data="disctype_percent", style="primary")],
        [InlineKeyboardButton(text="💵 مبلغ ثابت (تومان)", callback_data="disctype_amount", style="primary")],
    ])


def discount_plans_select_keyboard(selected: list):
    """با هر بار زدن روی یک پلن، انتخاب/عدم‌انتخابش toggle می‌شود؛ ✅ همه یعنی روی همه‌ی پلن‌ها اعمال شود."""
    buttons = [[InlineKeyboardButton(
        text="✅ همه‌ی پلن‌ها (بدون محدودیت)" if not selected else "☑️ همه‌ی پلن‌ها (بدون محدودیت)",
        callback_data="discplan_all", style="success",
    )]]
    for key, plan in db.get_all_plans().items():
        mark = "☑️" if key in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {plan['name']}", callback_data=f"discplan_{key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="✅ تأیید و ادامه", callback_data="discplan_done", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def discount_scope_summary(selected: list) -> str:
    """خلاصه‌ی متنی محدوده‌ی کد تخفیف (توکن‌ها: کلید پلن، cat:<id>، __custom__، __renew__)."""
    if not selected:
        return "♾ همه‌ی خریدها و تمدید (شارژ کیف پول شامل نمی‌شود)"
    all_plans = db.get_all_plans()
    parts = []
    for t in selected:
        if t == "__custom__":
            parts.append("🛠 سرویس سفارشی")
        elif t == "__renew__":
            parts.append("🔁 تمدید")
        elif t == "__wallet__":
            parts.append("💳 شارژ کیف پول")
        elif str(t).startswith("cat:"):
            try:
                parts.append("📁 " + str((db.get_vip_category(int(str(t)[4:])) or {}).get("name") or t))
            except Exception:
                parts.append(str(t))
        else:
            parts.append("🧩 " + str(all_plans.get(t, {}).get("name", t)))
    return "، ".join(parts)


def discount_scope_keyboard(mode: str, did, selected: list, view: str = "main"):
    """انتخاب محدوده‌ی کد تخفیف: دسته‌بندی VIP / پلن / تمدید / سرویس سفارشی / شارژ کیف پول / همه.
    mode: n = ساخت کد جدید ، e = ویرایش کد موجود (did = شناسه‌ی کد)."""
    did = int(did or 0)
    def cb(act, arg=""):
        return f"dsc|{mode}|{did}|{act}|{arg}"
    sel = set(selected or [])
    rows = []
    if view == "cats":
        for c in db.get_vip_categories():
            tok = f"cat:{int(c['id'])}"
            rows.append([InlineKeyboardButton(text=f"{'☑️' if tok in sel else '⬜️'} 📁 {c['name']}", callback_data=cb("t", tok), style="primary")])
        rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=cb("main"), style="danger")])
    elif view == "plans":
        for key, plan in db.get_all_plans().items():
            rows.append([InlineKeyboardButton(text=f"{'☑️' if key in sel else '⬜️'} {plan['name']}", callback_data=cb("t", key), style="primary")])
        rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=cb("main"), style="danger")])
    else:
        n_cat = sum(1 for t in sel if str(t).startswith("cat:"))
        n_plan = sum(1 for t in sel if not str(t).startswith("cat:") and not str(t).startswith("__"))
        rows.append([InlineKeyboardButton(text=f"📁 کل یک دسته‌بندی VIP{f' ({n_cat})' if n_cat else ''}", callback_data=cb("cats"), style="primary"),
                     InlineKeyboardButton(text=f"🧩 پلن‌های مشخص{f' ({n_plan})' if n_plan else ''}", callback_data=cb("plans"), style="primary")])
        rows.append([InlineKeyboardButton(text=f"{'☑️' if '__renew__' in sel else '⬜️'} 🔁 تمدید", callback_data=cb("t", "__renew__"), style="primary"),
                     InlineKeyboardButton(text=f"{'☑️' if '__custom__' in sel else '⬜️'} 🛠 سرویس سفارشی", callback_data=cb("t", "__custom__"), style="primary")])
        rows.append([InlineKeyboardButton(text=f"{'☑️' if '__wallet__' in sel else '⬜️'} 💳 شارژ کیف پول", callback_data=cb("t", "__wallet__"), style="primary")])
        rows.append([InlineKeyboardButton(text=("✅ " if not sel else "") + "♾ همه (بدون محدودیت؛ بدون شارژ کیف پول)", callback_data=cb("all"), style="success")])
        rows.append([InlineKeyboardButton(text="✅ تأیید و ادامه" if mode == "n" else "✅ ذخیره", callback_data=cb("done"), style="success")])
        if mode == "e":
            rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"discdetail_{did}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def discount_new_audience_keyboard():
    """مرحله‌ی «کاربران مجاز» در ساخت کد: همان لیست دسته‌بندی کاربران صفحه‌ی ویرایش کد."""
    from database import AUDIENCE_LABELS
    btns = [InlineKeyboardButton(text=lbl, callback_data=f"ndaud|{k}", style="primary") for k, lbl in AUDIENCE_LABELS.items()]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="🎯 فقط افراد خاص (با آیدی عددی)", callback_data="ndaud|specific", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def discount_plans_edit_keyboard(discount_id: int, selected: list):
    """نسخه‌ی ویرایشِ کد تخفیف موجود؛ همان discount_plans_select_keyboard است اما با
    callback_data متفاوت (discplaned_) تا با مسیر ساخت کد جدید تداخل نکند."""
    buttons = [[InlineKeyboardButton(
        text="✅ همه‌ی پلن‌ها (بدون محدودیت)" if not selected else "☑️ همه‌ی پلن‌ها (بدون محدودیت)",
        callback_data=f"discplaned_{discount_id}_all", style="success",
    )]]
    for key, plan in db.get_all_plans().items():
        mark = "☑️" if key in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {plan['name']}", callback_data=f"discplaned_{discount_id}_{key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="✅ ذخیره", callback_data=f"discplaned_{discount_id}_done", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"discdetail_{discount_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — تخفیف خودکار روی VIP برای آیدی عددی‌های خاص
# ---------------------------------------------------------------------------
def admin_agency_menu(agents: list | None = None):
    """لیست نمایندگان به‌صورت دکمه؛ با زدن روی هرکدام دقیقاً همان صفحه‌ی
    مدیریت کاربر (مثل بخش «کاربران») باز می‌شود، به‌علاوه‌ی گزینه‌ی تغییر درصد تخفیف."""
    buttons = []
    for a in (agents or []):
        _nm = ""
        try:
            _u = db.get_user(a["telegram_id"]) or {}
            _nm = str(_u.get("name") or "").strip()
            if not _nm and _u.get("username"):
                _nm = "@" + str(_u["username"]).lstrip("@")
        except Exception:
            _nm = ""
        if len(_nm) > 18:
            _nm = _nm[:17] + "…"
        buttons.append([InlineKeyboardButton(
            text=f"🆔 {a['telegram_id']}" + (f" | 👤 {_nm}" if _nm else "") + f" | 💯 {a['vip_discount_percent']}٪",
            callback_data=f"agentopen_{a['telegram_id']}", style="primary",
        )])
    buttons.append([InlineKeyboardButton(text="➕ افزودن نماینده", callback_data="new_agent", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_agent_row_keyboard(telegram_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 حذف این نماینده", callback_data=f"deleteagent_{telegram_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_agency", style="primary")],
    ])


def admin_agent_actions_keyboard(uid: str):
    """صفحه‌ی مدیریت نماینده: ابزارهای مدیریت کاربر + «مدیریت تخفیف نمایندگی» (همه‌ی تنظیمات تخفیف آنجاست)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 مدیریت تخفیف نمایندگی", callback_data=f"agdm|{uid}", style="success")],
        [InlineKeyboardButton(text="🏷 انتخاب پیشوند ساخت کانفیگ", callback_data=f"editagentprefix_{uid}", style="primary")],
        [InlineKeyboardButton(text="💰 شارژ دستی", callback_data=f"custom_{uid}", style="primary")],
        [InlineKeyboardButton(text="📒 حسابداری کاربر (تراکنش‌ها/منشأ پول)", callback_data=f"accounting_{uid}_0", style="primary")],
        [InlineKeyboardButton(text="🚀 ارسال کانفیگ دستی", callback_data=f"sendvip_{uid}", style="primary"),
         InlineKeyboardButton(text="⚡ ارسال کانفیگ خودکار", callback_data=f"autosend_{uid}", style="success")],
        [InlineKeyboardButton(text="📦 مشاهده و مدیریت سرویس‌های کاربر", callback_data=f"svcs_{uid}", style="primary")],
        [InlineKeyboardButton(text="🎁 سهمیه‌ی تست ماهانه", callback_data=f"agtq_{uid}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این نماینده", callback_data=f"deleteagent_{uid}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست نمایندگان", callback_data="admin_agency", style="primary")],
    ])


# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP (پنل ادمین) — افزودن دسته‌ی جدید، ورود به هر دسته برای
# افزودن/ویرایش/حذف پلن‌های داخلش + تغییر ترتیب نمایش (⬆️/⬇️) دسته‌ها و پلن‌ها.
# ---------------------------------------------------------------------------
def _admin_vip_folder_and_plan_rows(parent_cat: dict | None) -> list:
    """ردیف‌های مشترک صفحه‌ی ادمین: زیرپوشه‌ها و بعد پلن‌های همان سطح (هرکدام با ⬆️/⬇️).
    parent_cat=None یعنی سطح اصلی «خرید اشتراک»."""
    buttons = []
    folders = db.get_child_vip_categories(parent_cat["id"] if parent_cat else None)
    for i, cat in enumerate(folders):
        n_plans = len(db.get_vip_plans(cat["id"]))
        n_sub = len(db.get_child_vip_categories(cat["id"]))
        label = f"📁 {cat['name']} ({n_plans} پلن" + (f"، {n_sub} زیرپوشه)" if n_sub else ")")
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admincat_{cat['key']}", style="primary")])
        move_row = []
        if i > 0:
            move_row.append(InlineKeyboardButton(text="⬆️", callback_data=f"movevipcat_{cat['key']}_up", style="primary"))
        if i < len(folders) - 1:
            move_row.append(InlineKeyboardButton(text="⬇️", callback_data=f"movevipcat_{cat['key']}_down", style="primary"))
        if move_row:
            buttons.append(move_row)

    holder = parent_cat if parent_cat else db.get_vip_root_bucket(create=False)
    plans = db.get_vip_plans(holder["id"]) if holder else []
    for i, plan in enumerate(plans):
        buttons.append([InlineKeyboardButton(
            text=f"{plan['name']} — {plan['price']:,} ت" + (" 🔌" if (db.get_panel_plan_map('vip_plan', plan['id']) or db.get_panel_plan_map('vip_category', plan['category_id'])) else ""), callback_data=f"vipplan_{plan['plan_key']}"
        , style="primary")])
        move_row = []
        if i > 0:
            move_row.append(InlineKeyboardButton(text="⬆️", callback_data=f"movevipplan_{plan['plan_key']}_up", style="primary"))
        if i < len(plans) - 1:
            move_row.append(InlineKeyboardButton(text="⬇️", callback_data=f"movevipplan_{plan['plan_key']}_down", style="primary"))
        if move_row:
            buttons.append(move_row)
    return buttons


def admin_vip_categories_keyboard():
    """سطح اصلی «خرید اشتراک» در پنل ادمین: پوشه‌ها و پلن‌های بدون پوشه."""
    buttons = _admin_vip_folder_and_plan_rows(None)
    _sc = vip_shortcut_on()
    buttons.insert(0, [InlineKeyboardButton(
        text=("⚡ میانبر: روشن ✅" if _sc else "⚡ میانبر: خاموش ⛔"),
        callback_data="vipsc_toggle", style=("success" if _sc else "danger"))])
    buttons.append([InlineKeyboardButton(text="➕ پوشه‌ی جدید", callback_data="newvipcat", style="primary")])
    buttons.append([InlineKeyboardButton(text="➕ افزودن پلن (بدون پوشه)", callback_data="newvipplan_root", style="success")])
    _root_cat = db.get_vip_root_bucket(create=False)
    _root_has_desc = bool(_root_cat and str(_root_cat.get("description") or "").strip())
    buttons.append([InlineKeyboardButton(
        text=("📝 ویرایش توضیحات صفحه ✅" if _root_has_desc else "📝 افزودن توضیحات صفحه"),
        callback_data="vipcatdesc_root", style="primary")])
    root = db.get_vip_root_bucket(create=False)
    if root and db.get_vip_plans(root["id"]):
        buttons.append([InlineKeyboardButton(text="📤 انتقال پلن‌های این بخش به پوشه", callback_data="vipmvsel_root", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vip_category_detail_keyboard(category_key: str):
    cat = db.get_vip_category(category_key)
    if cat and db.is_vip_root_bucket(cat):
        return admin_vip_categories_keyboard()
    buttons = _admin_vip_folder_and_plan_rows(cat) if cat else []
    buttons.append([InlineKeyboardButton(text="➕ زیرپوشه‌ی جدید", callback_data=f"newvipsub_{category_key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="➕ افزودن پلن به این پوشه", callback_data=f"newvipplan_{category_key}", style="success")])
    if cat:
        # 📝 توضیحات بالای منوی داخل پوشه (کاربر بالای زیرپوشه‌ها/پلن‌ها می‌بیند؛ ایموجی پریمیوم و ریچ‌تکست پشتیبانی می‌شود)
        _has_desc = bool(str(cat.get("description") or "").strip())
        buttons.append([InlineKeyboardButton(
            text=("📝 ویرایش توضیحات پوشه ✅" if _has_desc else "📝 افزودن توضیحات پوشه"),
            callback_data=f"vipcatdesc_{category_key}", style="primary")])
    if cat and db.get_vip_plans(cat["id"]):
        buttons.append([InlineKeyboardButton(text="📤 انتقال پلن‌های این پوشه", callback_data=f"vipmvsel_{category_key}", style="primary")])
    if cat:
        _cm = db.get_panel_plan_map("vip_category", cat["id"])
        buttons.append([InlineKeyboardButton(
            text=("🔌 نگاشت کل پوشه: ✅ " + str(_cm.get("remote_name") or _cm.get("remote_ref"))[:28]) if _cm else "🔌 نگاشت کل پوشه به پنل",
            callback_data=f"vmap|c|{cat['id']}", style="success")])
    buttons.append([InlineKeyboardButton(text="📂 انتقال این پوشه به جای دیگر", callback_data=f"vipmvcat_{category_key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🗑 حذف این پوشه (فقط اگر خالی باشد)", callback_data=f"delvipcat_{category_key}", style="danger")])
    back_cb = "admin_vip_categories"
    if cat and cat.get("parent_id") is not None:
        parent = db.get_vip_category(int(cat["parent_id"]))
        if parent and not db.is_vip_root_bucket(parent):
            back_cb = f"admincat_{parent['key']}"
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=back_cb, style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _plan_map_label(plan_key: str) -> str:
    try:
        p = db.get_vip_plan(plan_key)
        m = db.get_panel_plan_map("vip_plan", p["id"]) if p else None
        if m:
            return "🔌 نگاشت پنل: ✅ " + str(m.get("remote_name") or m.get("remote_ref"))[:26]
        c = db.get_panel_plan_map("vip_category", p["category_id"]) if p else None
        if c:
            return "🔌 نگاشت پنل: ارث از پوشه ✅"
    except Exception:
        pass
    return "🔌 نگاشت پنل (اختیاری)"


def admin_vip_plan_detail_keyboard(plan_key: str, category_key: str):
    _c = db.get_vip_category(category_key)
    _back_cb = "admin_vip_categories" if (_c and db.is_vip_root_bucket(_c)) else f"admincat_{category_key}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ ویرایش نام", callback_data=f"vipplanname_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="💰 ویرایش قیمت", callback_data=f"vipplanprice_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="📦 ویرایش حجم (گیگ)", callback_data=f"vipplangb_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="⏳ ویرایش مدت (روز، ۰=نامحدود)", callback_data=f"vipplandays_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="👥 سقف دستگاه / HWID (۰=نامحدود)", callback_data=f"vipplanlimit_{plan_key}", style="primary")],
        [InlineKeyboardButton(text=_plan_map_label(plan_key), callback_data=f"vmap|p|{(db.get_vip_plan(plan_key) or {}).get('id', 0)}", style="success")],
        [InlineKeyboardButton(text="📤 انتقال به پوشه‌ی دیگر", callback_data=f"vipmvplan_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این پلن", callback_data=f"delvipplan_{plan_key}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به پوشه", callback_data=_back_cb, style="primary")],
    ])


# ---------------------------------------------------------------------------

# 🖥 مدیریت پنل‌های VPN — هر سه نوع (شاهراه/مرزبان/پاسارگارد) هم‌زمان
# فعال هستند و هر کدام می‌تواند چند نمونه (Instance) هم‌زمان داشته باشد.
# ---------------------------------------------------------------------------
def admin_vpn_panel_types_keyboard():
    """قدم اول: انتخاب نوع پنل برای مدیریت. هر سه نوع مستقل هم‌زمان قابل فعال‌شدن هستند."""
    buttons = [
        [InlineKeyboardButton(text=PANEL_TYPE_LABELS[t], callback_data=f"vpntype|{t}", style="primary")]
        for t in PANEL_TYPES
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_panel_list_keyboard(panel_type: str, panels: list[dict]):
    """لیست نمونه‌های ساخته‌شده از یک نوع پنل (می‌توانند چندتایی باشند
    و همه هم‌زمان فعال بمانند) + دکمه‌ی افزودن نمونه‌ی جدید."""
    buttons = []
    for p in panels:
        mark = "🟢" if p.get("enabled") else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {p['name']}", callback_data=f"vpndetail|{p['id']}", style="primary"
        )])
    buttons.append([InlineKeyboardButton(
        text=f"➕ افزودن پنل {PANEL_TYPE_LABELS.get(panel_type, panel_type)} جدید",
        callback_data=f"vpnadd|{panel_type}", style="success",
    )])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به انتخاب نوع پنل", callback_data="admin_vpn_panels", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_panel_detail_keyboard(panel: dict):
    """منوی مدیریت یک نمونه‌ی مشخص از پنل."""
    pid = panel["id"]
    if panel.get("enabled"):
        toggle_text = "🔴 غیرفعال کردن این پنل"
    else:
        toggle_text = "🟢 فعال‌کردن این پنل"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 تست اتصال", callback_data=f"vpntest|{pid}", style="primary")],
        [InlineKeyboardButton(text="✏️ ویرایش اطلاعات پنل", callback_data=f"vpnedit|{pid}", style="primary")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"vpntoggle|{pid}", style="danger" if panel.get("enabled") else "success")],
        [InlineKeyboardButton(text="🗑 حذف این پنل", callback_data=f"vpndelete|{pid}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data=f"vpntype|{panel['panel_type']}", style="primary")],
    ])


def admin_vpn_panel_delete_confirm_keyboard(panel_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"vpndeleteconfirm|{panel_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data=f"vpndetail|{panel_id}", style="primary")],
    ])


def admin_vpn_panel_edit_menu_keyboard(panel: dict):
    """فیلدهای قابل‌ویرایش به نوع پنل و روش اتصال انتخاب‌شده بستگی دارد:
    شاهراه همیشه با API Key کار می‌کند. مرزبان/پاسارگارد بسته به auth_method
    یا با یوزرنیم+پسورد یا با یک API Key ثابت کار می‌کنند و ادمین می‌تواند
    با دکمه‌ی «تغییر روش اتصال» بین این دو جابه‌جا شود."""
    pid = panel["id"]
    buttons = [
        [InlineKeyboardButton(text="✏️ نام", callback_data=f"vpneditfield|{pid}|name", style="primary")],
        [InlineKeyboardButton(text="✏️ آدرس پایه (base URL)", callback_data=f"vpneditfield|{pid}|base_url", style="primary")],
    ]
    if panel["panel_type"] == "shahrah":
        buttons.append([InlineKeyboardButton(text="✏️ API Key", callback_data=f"vpneditfield|{pid}|api_key", style="primary")])
    else:
        auth_method = panel.get("auth_method") or "userpass"
        if auth_method == "api_key":
            buttons.append([InlineKeyboardButton(text="✏️ API Key", callback_data=f"vpneditfield|{pid}|api_key", style="primary")])
        else:
            buttons.append([InlineKeyboardButton(text="✏️ نام کاربری", callback_data=f"vpneditfield|{pid}|username", style="primary")])
            buttons.append([InlineKeyboardButton(text="✏️ رمز عبور", callback_data=f"vpneditfield|{pid}|password", style="primary")])
        other = "👤 یوزرنیم/پسورد" if auth_method == "api_key" else "🔑 API Key"
        buttons.append([InlineKeyboardButton(text=f"🔀 تغییر روش اتصال به {other}", callback_data=f"vpnauthswitch|{pid}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{pid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🛠 ایجاد سرویس دستی توسط ادمین (برای خودش) — مشابه «بساز سرویس خودت» ولی
# بدون قیمت/محدودیت حجم-روز و بدون نیاز به پرداخت؛ ادمین آزاد است از هر
# پنل فعال و هر تمپلیت/اینباند آن، هر حجم/روزی که بخواهد بسازد.
# ---------------------------------------------------------------------------
def admin_create_service_panel_keyboard(panels_list: list[dict]):
    import panels as _panels
    buttons = [
        [InlineKeyboardButton(text=_panels.panel_label(p), callback_data=f"adminsvcpanel_{p['id']}", style="primary")]
        for p in panels_list
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_create_service_mode_keyboard(panel_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 با تمپلیت پنل", callback_data=f"adminsvcmode|{panel_id}|template", style="primary")],
        [InlineKeyboardButton(text="🗂 با گروه‌های پنل (بدون تمپلیت)", callback_data=f"adminsvcmode|{panel_id}|group", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_create_service", style="danger")],
    ])


def admin_create_service_group_keyboard(items: list[dict], panel_id: int, selected: set):
    buttons = []
    for it in items:
        mark = "✅" if it["idx"] in selected else "⬜️"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {it['label']}"[:60], callback_data=f"adminsvcgrp|{panel_id}|{it['idx']}", style="primary")])
    all_selected = bool(items) and len(selected) >= len(items)
    buttons.append([InlineKeyboardButton(
        text=("⬜️ برداشتن همه" if all_selected else "☑️ انتخاب همه"), callback_data=f"adminsvcgrpall|{panel_id}", style="primary")])
    buttons.append([InlineKeyboardButton(
        text=f"➡️ ادامه ({len(selected)} انتخاب‌شده)", callback_data=f"adminsvcgrpok|{panel_id}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"adminsvcpanel_{panel_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_create_service_catalog_keyboard(items: list[dict], panel_id: int):
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"adminsvccatalog_{panel_id}_{p['idx']}", style="primary")]
        for p in items
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_create_service", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_panel_auth_choice_keyboard(panel_type: str):
    """قدم اضافه‌ی «افزودن پنل جدید» برای انواعی که هم یوزر/پسورد و هم API Key
    را پشتیبانی می‌کنند: ادمین انتخاب می‌کند کدام روش برای این نمونه استفاده شود."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 یوزرنیم و پسورد", callback_data=f"vpnauthadd|{panel_type}|userpass", style="primary")],
        [InlineKeyboardButton(text="🔑 API Key", callback_data=f"vpnauthadd|{panel_type}|api_key", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_vpn_panels", style="danger")],
    ])


def vpn_panel_back_keyboard(panel_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")],
    ])


def admin_vpn_panel_types_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_vpn_panels", style="primary")],
    ])


def admin_vpn_panel_map_menu_keyboard(panel_id: int):
    """نگاشت‌ها دیگر از مدیریت پنل انجام نمی‌شوند: VIP از خود پوشه/پلن، تست رایگان/PAYG/سفارشی از تنظیمات خودشان."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")],
    ])


def vpn_map_category_pick_keyboard(categories: list[dict], scope: str, panel_id: int):
    """لیست دسته‌بندی‌های VIP برای نگاشت پیش‌فرض کل دسته به این نمونه‌ی پنل."""
    buttons = []
    for cat in categories:
        mapping = db.get_panel_plan_map(scope, cat["id"])
        mark = f" ✅ ({mapping['remote_name'] or mapping['remote_ref']})" if mapping else ""
        buttons.append([InlineKeyboardButton(
            text=f"{cat['name']}{mark}", callback_data=f"vpnmapcat|{panel_id}|{scope}|{cat['id']}"
        , style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_map_vip_category_pick_keyboard(categories: list[dict], panel_id: int):
    """قدم اول نگاشت اختصاصی VIP برای این نمونه‌ی پنل: انتخاب دسته‌بندی."""
    buttons = [
        [InlineKeyboardButton(text=cat["name"], callback_data=f"vpnmapvipcat|{panel_id}|{cat['id']}", style="primary")]
        for cat in categories
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_map_vip_plans_keyboard(category_id: int, plans: list[dict], panel_id: int):
    """قدم دوم نگاشت اختصاصی VIP: هر پلن با نگاشت اختصاصی خودش به این نمونه‌ی پنل،
    یا نگاشت پیش‌فرض کل دسته."""
    buttons = []
    for p in plans:
        mapping = db.get_panel_plan_map("vip_plan", p["id"])
        mark = f" ✅ ({mapping['remote_name'] or mapping['remote_ref']})" if mapping else " ⚪️ نگاشت‌نشده"
        label = f"{p['name']} — {p['volume_gb']}GB/{p['days']}روز{mark}"
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(
            text=label, callback_data=f"vpnmapvipplan|{panel_id}|{category_id}|{p['id']}"
        , style="primary")])
        if mapping:
            buttons.append([InlineKeyboardButton(text="🗑 حذف نگاشت این پلن", callback_data=f"vpnmapdelplan|{panel_id}|{p['id']}", style="danger")])
    buttons.append([InlineKeyboardButton(
        text="🗂 نگاشت پیش‌فرض کل این دسته (اختیاری)",
        callback_data=f"vpnmapcat|{panel_id}|vip_category|{category_id}", style="primary",
    )])
    if db.get_panel_plan_map("vip_category", category_id):
        buttons.append([InlineKeyboardButton(text="🗑 حذف نگاشت پیش‌فرض این دسته", callback_data=f"vpnmapdelcat|{panel_id}|vip_category|{category_id}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpnmapvip|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_catalog_pick_keyboard(items: list[dict], panel_id: int):
    """لیست بسته‌ها/تمپلیت‌های واقعی گرفته‌شده از خودِ پنل برای انتخاب نهایی — items هرکدام
    حداقل 'idx' (اندیس محلی در state) و متن نمایشی 'label' داشته باشند."""
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"vpncatalogpick|{panel_id}|{p['idx']}", style="primary")]
        for p in items
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🧩 نگاشت «بدون تمپلیت» — ادمین بین ساختن سرویس از روی یک تمپلیت آماده‌ی پنل
# یا مستقیم از روی ای��باند/گروه‌های زنده‌ی پنل (بدون نیاز به هیچ تمپلیتی)
# انتخاب می‌کند.
# ---------------------------------------------------------------------------
def vpn_map_mode_keyboard(panel_id: int, supports_direct: bool):
    buttons = [[InlineKeyboardButton(text="📦 از تمپلیت پنل استفاده کن", callback_data=f"vpnmapmode|{panel_id}|template", style="primary")]]
    if supports_direct:
        buttons.append([InlineKeyboardButton(text="🧩 بدون تمپلیت (مستقیم از اینباند/گروه)", callback_data=f"vpnmapmode|{panel_id}|direct", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_direct_protocol_keyboard(protocols: list[str], panel_id: int):
    rows=[[InlineKeyboardButton(text=f"🔌 پروتکل {proto}", callback_data=f"vpndirectprotocol|{panel_id}|{proto}", style="primary")] for proto in protocols]
    rows.append([InlineKeyboardButton(text="🌐 همه پروتکل‌ها", callback_data=f"vpndirectprotocol|{panel_id}|all", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"vpndetail|{panel_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vpn_direct_multiselect_keyboard(items: list[dict], panel_id: int, selected: set):
    buttons = []
    for it in items:
        mark = "✅" if it["idx"] in selected else "⬜️"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {it['label']}", callback_data=f"vpndirecttoggle|{panel_id}|{it['idx']}", style="primary",
        )])
    all_selected = bool(items) and len(selected) >= len(items)
    buttons.append([InlineKeyboardButton(
        text=("⬜️ برداشتن انتخاب همه" if all_selected else "☑️ انتخاب همه‌ی گروه‌ها/اینباندها"), callback_data=f"vpndirectall|{panel_id}", style="primary",
    )])
    buttons.append([InlineKeyboardButton(
        text=f"✅ ذخیره ({len(selected)} انتخاب‌شده)", callback_data=f"vpndirectconfirm|{panel_id}", style="success",
    )])
    buttons.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"vpndetail|{panel_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🩺 سلامت / 🚀 کش / 🧩 بساز سرویس خودت / 📋 گزارش فعالیت ادمین
# (کیبوردهای این چهار صفحه هم همراه خود صفحات حذف شده بودند)
# ---------------------------------------------------------------------------
def admin_health_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="admin_health", style="primary")],
        [InlineKeyboardButton(text="🚀 وضعیت کش", callback_data="admin_cache_status", style="primary")],
    ])


def admin_cache_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی آمار", callback_data="admin_cache_status", style="primary")],
        [InlineKeyboardButton(text="🧹 پاک‌کردن کامل کش", callback_data="cache_clear_do", style="danger")],
        [InlineKeyboardButton(text="🩺 سلامت ربات", callback_data="admin_health", style="primary")],
    ])


def admin_custom_build_keyboard(enabled: bool = True):
    toggle_text = "✅ بساز سرویس خودت: فعال" if enabled else "❌ بساز سرویس خودت: غیرفعال"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="admin_custom_build_toggle", style="success" if enabled else "danger")],
        [InlineKeyboardButton(text="🔌 گزینه‌ها و نگاشت پنل‌ها", callback_data="mopt_list|custom_build", style="success")],
        [InlineKeyboardButton(text="📏 تنظیم قیمت و محدوده‌ها (به‌تفکیک پلن)", callback_data="cbset_edit", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def admin_audit_logs_keyboard(page: int, has_next: bool):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"auditpage_{page - 1}", style="primary"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️ بعدی", callback_data=f"auditpage_{page + 1}", style="primary"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="admin_audit_logs", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 📊 بازطراحی کامل صفحه‌ی آمار (بند ۲۳) — مطابق اسکرین‌شاتی که ارسال شد
# ---------------------------------------------------------------------------
def admin_stats_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار کل", callback_data="stat_all", style="success")],
        [InlineKeyboardButton(text="🕐 یک ساعت اخیر", callback_data="stat_hour", style="primary")],
        [
            InlineKeyboardButton(text="☁️ امروز", callback_data="stat_today", style="primary"),
            InlineKeyboardButton(text="☀️ دیروز", callback_data="stat_yesterday", style="primary"),
        ],
        [
            InlineKeyboardButton(text="☀️ ماه فعلی", callback_data="stat_this_month", style="primary"),
            InlineKeyboardButton(text="☁️ ماه قبل", callback_data="stat_last_month", style="primary"),
        ],
        [InlineKeyboardButton(text="🗓 مشاهده آمار در تاریخ مشخص", callback_data="stat_custom", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])

def admin_crypto_list_menu(cryptos: list, master_enabled: bool = True) -> InlineKeyboardMarkup:
    """لیست ارزهای دیجیتال با دکمه تنظیمات مستقل برای هر ارز"""
    rows = [[InlineKeyboardButton(
        text=("✅ بخش ارز دیجیتال: فعال (برای غیرفعال‌کردن بزنید)" if master_enabled
              else "❌ بخش ارز دیجیتال: غیرفعال (برای فعال‌کردن بزنید)"),
        callback_data="paymt_crypto_master", style=("danger" if master_enabled else "success"))]]
    for c in cryptos:
        status = "✅" if c.enabled else "❌"
        rows.append([
            InlineKeyboardButton(text=f"{status} {c.emoji} {c.name} ({c.symbol})",
                                 callback_data=f"paymt_coin:{c.symbol}", style="primary",
                                 icon_custom_emoji_id=getattr(c, "custom_emoji_id", None) or None),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن ارز دلخواه", callback_data="paymt_add_coin", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_crypto_coin_keyboard(c) -> InlineKeyboardMarkup:
    """تنظیمات کامل یک ارز دیجیتال"""
    status_text = "❌ غیرفعال کردن" if c.enabled else "✅ فعال کردن"
    status_style = "danger" if c.enabled else "success"
    wallet_short = ((c.wallet[:20] + "...") if len(c.wallet or "") > 20 else (c.wallet or "تنظیم نشده"))
    backup_text = f"{c.backup_price:,.0f} تومان" if c.backup_price else "تنظیم نشده"
    rows = [
        [InlineKeyboardButton(text=f"✅ فعال" if c.enabled else "❌ غیرفعال",
                              callback_data=f"paymt_coin_toggle:{c.symbol}", style=status_style)],
        [InlineKeyboardButton(text=f"💼 Wallet: {wallet_short}",
                              callback_data=f"paymt_coin_wallet:{c.symbol}", style="primary")],
        [InlineKeyboardButton(text=f"💰 Backup Price: {backup_text}",
                              callback_data=f"paymt_coin_backup:{c.symbol}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این ارز",
                              callback_data=f"paymt_del_coin:{c.symbol}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست ارزها",
                              callback_data="paymt_crypto", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_card_info_menu(card) -> InlineKeyboardMarkup:
    """کیبورد مدیریت کارت‌به‌کارت"""
    status = "❌ غیرفعال" if not card.enabled else "✅ فعال"
    rows = [
        [InlineKeyboardButton(text=f"{status}", callback_data="paymt_card_toggle", style="primary")],
        [InlineKeyboardButton(text="💳 شماره کارت", callback_data="paymt_card_number", style="primary"),
         InlineKeyboardButton(text="👤 نام دارنده", callback_data="paymt_card_owner", style="primary")],
        [InlineKeyboardButton(text=f"📋 دکمه‌ی کپی کارت: {'✅ روشن' if getattr(card, 'show_copy_card', True) else '❌ خاموش'}", callback_data="paymt_card_opt|show_copy_card", style="success" if getattr(card, 'show_copy_card', True) else "danger"),
         InlineKeyboardButton(text=f"📋 دکمه‌ی کپی مبلغ: {'✅ روشن' if getattr(card, 'show_copy_amount', True) else '❌ خاموش'}", callback_data="paymt_card_opt|show_copy_amount", style="success" if getattr(card, 'show_copy_amount', True) else "danger")],
        [InlineKeyboardButton(text=f"🔒 قفل فوروارد فاکتور: {'✅ روشن' if getattr(card, 'forward_lock', False) else '❌ خاموش'}", callback_data="paymt_card_opt|forward_lock", style="success" if getattr(card, 'forward_lock', False) else "danger")],
        [InlineKeyboardButton(text=f"🎲 مبلغ رندوم (±۱۵۰۰): {'✅ روشن' if getattr(card, 'random_amount', True) else '❌ خاموش (رند)'}", callback_data="paymt_card_opt|random_amount", style="success" if getattr(card, 'random_amount', True) else "danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_wallet_info_menu(wallet) -> InlineKeyboardMarkup:
    """کیبورد مدیریت کیف‌پول با تنظیمات شارژ."""
    enabled = bool(wallet.get("enabled", True)) if isinstance(wallet, dict) else bool(getattr(wallet, "enabled", True))
    min_topup = int(wallet.get("min_topup", 0)) if isinstance(wallet, dict) else int(getattr(wallet, "min_topup", getattr(wallet, "min_charge", 0)) or 0)
    max_topup = int(wallet.get("max_topup", 0)) if isinstance(wallet, dict) else int(getattr(wallet, "max_topup", getattr(wallet, "max_charge", 0)) or 0)
    quick_amounts = wallet.get("quick_amounts", []) if isinstance(wallet, dict) else getattr(wallet, "quick_amounts", [])
    quick_text = " / ".join(f"{int(a):,}" for a in quick_amounts) if quick_amounts else "تنظیم نشده"
    rows = [
        [InlineKeyboardButton(text=f"کیف‌پول: ✅ فعال" if enabled else "کیف‌پول: ❌ غیرفعال", callback_data="paymt_wallet_toggle", style="primary")],
        [InlineKeyboardButton(text=f"📉 حداقل شارژ: {min_topup:,} تومان", callback_data="wset_edit_min_topup", style="primary")],
        [InlineKeyboardButton(text=f"📈 حداکثر شارژ: {max_topup:,} تومان", callback_data="wset_edit_max_topup", style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های مبلغ: {quick_text}", callback_data="wset_edit_quick_amounts", style="primary")],
        [InlineKeyboardButton(text=f"🎁 درصد هدیه‌ی شارژ: {float(wallet.get('gift_percent', 0) if isinstance(wallet, dict) else getattr(wallet, 'gift_percent', 0) or 0):g}٪", callback_data="wset_edit_gift_percent", style="primary")],
        [InlineKeyboardButton(text=f"🪫 هشدار موجودی کم: {('غیرفعال' if not int((wallet.get('low_balance') if isinstance(wallet, dict) else getattr(wallet, 'low_balance', 0)) or 0) else format(int((wallet.get('low_balance') if isinstance(wallet, dict) else getattr(wallet, 'low_balance', 0))), ',') + ' تومان')}", callback_data="wset_edit_low_balance", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_vip_move_select_keyboard(source_token: str, plans: list, selected: set):
    buttons = []
    for plan in plans:
        mark = "☑️" if plan["plan_key"] in selected else "⬜️"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {plan["name"]} — {plan["price"]:,} ت",
            callback_data=f"vipmvtog_{plan["plan_key"]}", style="primary"
        )])
    all_selected = bool(plans) and len(selected) >= len(plans)
    buttons.append([InlineKeyboardButton(
        text=("⬜️ برداشتن همه" if all_selected else "☑️ انتخاب همه"),
        callback_data="vipmvall", style="primary"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"➡️ ادامه — انتخاب پوشه‌ی مقصد ({len(selected)} پلن)",
        callback_data="vipmvgo", style="success"
    )])
    buttons.append([InlineKeyboardButton(text="❌ انصراف", callback_data="vipmvcancel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vip_move_dest_keyboard(browse_cat, exclude_ids=None, allow_here: bool = True):
    exclude_ids = exclude_ids or set()
    buttons = []
    for cat in db.get_child_vip_categories(browse_cat["id"] if browse_cat else None):
        if int(cat["id"]) in exclude_ids:
            continue
        n_sub = len(db.get_child_vip_categories(cat["id"]))
        label = f"📁 {cat["name"]}" + (f" ({n_sub} زیرپوشه)" if n_sub else "")
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"vipmvdst_{cat["key"]}", style="primary")])
    here_token = browse_cat["key"] if browse_cat else "root"
    if allow_here:
        buttons.append([InlineKeyboardButton(text="✅ انتقال به همین‌جا", callback_data=f"vipmvhere_{here_token}", style="success")])
    if browse_cat:
        parent_token = "root"
        if browse_cat.get("parent_id") is not None:
            parent = db.get_vip_category(int(browse_cat["parent_id"]))
            if parent and not db.is_vip_root_bucket(parent):
                parent_token = parent["key"]
        buttons.append([InlineKeyboardButton(text="⬆️ یک سطح بالاتر", callback_data=f"vipmvdst_{parent_token}", style="primary")])
    buttons.append([InlineKeyboardButton(text="❌ انصراف", callback_data="vipmvcancel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# 🎁 GIFT AUDIENCE KEYBOARD  (بند ۲۵)
# ═══════════════════════════════════════════════════════════════════════════════

def admin_gift_audience_keyboard() -> InlineKeyboardMarkup:
    """انتخاب مخاطب هدیه — ۱۴ دسته."""
    OPTS = [
        ("👥 همه",         "giftaud_all"),
        ("🛒 خریدار",      "giftaud_buyers"),
        ("🆕 بی‌خرید",     "giftaud_non_buyers"),
        ("💰 کیف+",        "giftaud_wallet_positive"),
        ("🤝 نماینده",     "giftaud_agents"),
        ("🟢 فعال",        "giftaud_active_service"),
        ("🔴 منقضی",       "giftaud_expired_service"),
        ("🆕 ۷روزه",       "giftaud_new_users"),
        ("📵 بی‌سرویس",    "giftaud_no_service"),
        ("📦 چندسرویس",    "giftaud_multi_service"),
        ("😴 غیرفعال",     "giftaud_inactive"),
        ("🎁 تست",         "giftaud_has_test"),
        ("🎟 تخفیف",       "giftaud_has_discount"),
        ("💳 PAYG",        "giftaud_sufficient_wallet"),
    ]
    rows = []
    for i in range(0, len(OPTS), 3):
        row = [InlineKeyboardButton(text=text, callback_data=data, style="primary") for text, data in OPTS[i:i+3]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════════════════════════
# ⚡ PAYG PRICING KEYBOARDS  (بند ۷)
# ═══════════════════════════════════════════════════════════════════════════════

def admin_payg_pricing_mode_keyboard() -> InlineKeyboardMarkup:
    """انتخاب رویه قیمت‌گذاری PAYG."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 بر اساس دسته‌بندی", callback_data="payg_price_by_category", style="primary"),
         InlineKeyboardButton(text="📋 بر اساس پلن", callback_data="payg_price_by_plan", style="primary")],
        [InlineKeyboardButton(text="💰 قیمت عمومی (هر GB)", callback_data="payg_price_per_gb", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payg", style="danger")],
    ])


def admin_payg_category_pricing_keyboard(categories: list) -> InlineKeyboardMarkup:
    """لیست دسته‌بندی‌ها برای تنظیم قیمت PAYG."""
    rows = []
    for i in range(0, len(categories), 2):
        row = []
        for cat in categories[i:i+2]:
            cat_id = cat.get('id') or cat.get('category_id', 0)
            name = cat.get('name', str(cat_id))
            row.append(InlineKeyboardButton(
                text=f"📂 {name}",
                callback_data=f"payg_catprice_{cat_id}",
                style="primary"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="payg_price_mode", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_payg_plan_pricing_keyboard(plans: list) -> InlineKeyboardMarkup:
    """لیست پلن‌ها برای تنظیم قیمت PAYG."""
    rows = []
    for i in range(0, len(plans), 2):
        row = []
        for plan in plans[i:i+2]:
            key = plan.get('plan_key', plan.get('key', ''))
            name = plan.get('name', key)
            row.append(InlineKeyboardButton(
                text=f"📋 {name}",
                callback_data=f"payg_planprice_{key}",
                style="primary"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="payg_price_mode", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════════════════════════
# 💱 CRYPTO BACKUP KEYBOARD  (بند ۲۳)
# ═══════════════════════════════════════════════════════════════════════════════

def admin_crypto_backup_keyboard(symbol: str, current_price: float | None, enabled: bool) -> InlineKeyboardMarkup:
    st = "🟢 فعال" if enabled else "🔴 غیرفعال"
    toggle_lbl = "🔴 غیرفعال کردن" if enabled else "🟢 فعال کردن"
    price_txt = f"{current_price:,.0f} تومان" if current_price else "تنظیم نشده"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Fallback {symbol}: {st}", callback_data=f"crypto_backup_toggle_{symbol}", style="primary")],
        [InlineKeyboardButton(text=f"💰 قیمت Backup: {price_txt}", callback_data=f"crypto_backup_price_{symbol}", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="paymt_crypto", style="danger")],
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# 🎁 FREE TEST PANELS KEYBOARDS  (بند ۱۴)
# ═══════════════════════════════════════════════════════════════════════════════

def admin_free_test_panels_keyboard(panels: list) -> InlineKeyboardMarkup:
    """لیست پنل‌های تست برای مدیر."""
    rows = []
    for p in panels:
        st = "🟢" if p.get("enabled", True) else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{st} {p['name']}",
            callback_data=f"ftpanel_open_{p['id']}",
            style="primary")])
    rows.append([InlineKeyboardButton(text="➕ افزودن پنل تست جدید", callback_data="admin_free_test_add_panel", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_free_test_settings", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_free_test_map_panels_keyboard(num: int, panels: list) -> InlineKeyboardMarkup:
    """انتخاب پنل فعال برای نگاشت یک پلن تست (بعد از ساخت پلن)."""
    rows = [[InlineKeyboardButton(text=f"🖥 {p.get('name') or p.get('id')}", callback_data=f"ftmap|{num}|{p['id']}", style="primary")]
            for p in panels]
    rows.append([InlineKeyboardButton(text="⏭ بعداً", callback_data="ftpanel_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_free_test_panel_detail_keyboard(panel_id: str, enabled: bool, num: int = 0) -> InlineKeyboardMarkup:
    toggle_lbl = "🔴 غیرفعال کردن" if enabled else "🟢 فعال کردن"
    return InlineKeyboardMarkup(inline_keyboard=([
        [InlineKeyboardButton(text="🔌 نگاشت پنل (تمپلیت/گروه)", callback_data=f"ftmappick|{num}", style="success")]] if num else []) + [
        [InlineKeyboardButton(text=toggle_lbl, callback_data=f"ftpanel_toggle_{panel_id}", style="primary"),
         InlineKeyboardButton(text="🗑 حذف", callback_data=f"ftpanel_del_{panel_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="ftpanel_list", style="primary")],
    ])


def user_free_test_panel_select_keyboard(panels: list) -> InlineKeyboardMarkup:
    """نمایش پنل‌های تست به کاربر برای انتخاب."""
    rows = []
    for p in panels:
        vol = p.get('volume_gb', 0)
        days = p.get('days', 0)
        rows.append([InlineKeyboardButton(
            text=f"{p['name']} — {vol}GB / {days} روز",
            callback_data=f"get_test_{p['id']}",
            style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_search_mode_keyboard():
    """کیبورد انتخاب حالت جستجوی حرفه‌ای"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔢 جستجو با آیدی عددی', callback_data='searchmode_id', style="primary")],
        [InlineKeyboardButton(text='👤 جستجو با نام', callback_data='searchmode_name', style="primary")],
        [InlineKeyboardButton(text='🎟 جستجو با کد دعوت', callback_data='searchmode_invite', style="primary")],
        [InlineKeyboardButton(text='🔍 جستجوی هوشمند (همه)', callback_data='searchmode_all', style="primary")],
        [InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_panel', style="danger")],
    ])


def admin_search_results_keyboard(users: list) -> InlineKeyboardMarkup:
    """لیست کاربران یافت‌شده با دکمه برای هر کاربر"""
    buttons = []
    for u in users:
        label = f"{u['name']} | {u['telegram_id']}"
        if len(label) > 48:
            label = label[:45] + '...'
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"searchopen_{u['telegram_id']}",
         style="primary")])
    buttons.append([InlineKeyboardButton(text='🔙 جستجوی جدید', callback_data='admin_search', style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────────────────────────────────────
# بند ۱۳ — Rich Text Template keyboards
# ─────────────────────────────────────────────────────────────────────────────
def admin_rich_text_list_keyboard(templates: list) -> InlineKeyboardMarkup:
    """لیست قالب‌های Rich Text در پنل ادمین"""
    buttons = []
    for t in templates:
        label = f"📄 {t.get('label', t.get('id', '?'))}"[:48]
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"rt_open_{t['id']}",
         style="primary")])
    buttons.append([InlineKeyboardButton(text="➕ قالب جدید", callback_data="rt_new", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_rich_text_detail_keyboard(template_id: int) -> InlineKeyboardMarkup:
    """کیبورد جزئیات یک قالب Rich Text"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ ویرایش متن", callback_data=f"rt_edit_{template_id}", style="primary")],
        [InlineKeyboardButton(text="🏷 ویرایش برچسب (label)", callback_data=f"rt_relabel_{template_id}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف قالب", callback_data=f"rt_del_{template_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="rt_list", style="danger")],
    ])


def admin_rich_text_confirm_del_keyboard(template_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"rt_delconfirm_{template_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"rt_open_{template_id}", style="danger")],
    ])


# ---------------------------------------------------------------------------
# 🔌 نگاشت چندگانه (PAYG / بساز سرویس خودت) + نگاشت پلن/پوشه‌ی VIP از داخل خودشان
# ---------------------------------------------------------------------------
MAP_KIND_TITLES = {"payg": "⚡ PAYG", "custom_build": "🛠 سرویس سفارشی"}
MAP_KIND_BACK = {"payg": "admin_payg", "custom_build": "admin_custom_build_settings"}


def _opt_price_suffix(kind: str, num: int) -> str:
    try:
        import database as _d
        if kind == "payg":
            pr = _d.get_map_option_cfg("payg", num).get("price_per_gb") or int(_d.get_payg_settings().get("price_per_gb") or 0)
            return f" — {pr:,} ت/GB" if pr else " — قیمت؟"
        cb = _d.get_effective_custom_build_settings(num)
        return f" — {cb['price_per_gb']:,}/GB"
    except Exception:
        return ""


def admin_map_options_keyboard(kind: str, options: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for o in options:
        mark = "✅" if o.get("mapped") else "⚪️"
        rows.append([InlineKeyboardButton(text=f"{mark} {o['name']}{_opt_price_suffix(kind, int(o['num']))}", callback_data=f"mopt_open|{kind}|{o['num']}", style="primary")])
    rows.append([InlineKeyboardButton(text="➕ افزودن پلن/نگاشت جدید", callback_data=f"mopt_add|{kind}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=MAP_KIND_BACK.get(kind, "admin_back"), style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_map_option_detail_keyboard(kind: str, num: int, mapped: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=("🔌 تغییر نگاشت پنل" if mapped else "🔌 نگاشت به پنل"), callback_data=f"mopt_pick|{kind}|{num}", style="success")]]
    if kind == "payg":
        rows.append([InlineKeyboardButton(text="💰 قیمت هر گیگ این پلن", callback_data=f"moptcfg|payg|{num}|price_per_gb", style="primary")])
    else:
        rows.append([InlineKeyboardButton(text="💰 قیمت و محدوده‌های این پلن", callback_data=f"cbset_o|{num}", style="primary")])
    rows += [
        [InlineKeyboardButton(text="✏️ تغییر نام", callback_data=f"mopt_ren|{kind}|{num}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این گزینه", callback_data=f"mopt_del|{kind}|{num}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"mopt_list|{kind}", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_pick_keyboard(panels_list: list[dict], cb_fmt: str, back_cb: str, extra_rows: list | None = None) -> InlineKeyboardMarkup:
    """لیست پنل‌های فعال؛ cb_fmt باید جای {pid} داشته باشد."""
    from panels import panel_label as _pl
    rows = [[InlineKeyboardButton(text=_pl(p), callback_data=cb_fmt.format(pid=p["id"]), style="primary")] for p in panels_list]
    rows.extend(extra_rows or [])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=back_cb, style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
