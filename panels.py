import asyncio
import aiohttp
"""
panels.py
لایه‌ی یکپارچه‌شده‌ی مشترک برای هر سه نوع پنل پشتیبانی‌شده (شاهراه/مرزبان/پاسارگارد).

هیچ‌جای دیگری از ربات (هندلرها/منطق فروش و...) نباید مستقیماً به shahrah.py /
 marzban_panel.py / pasargad_panel.py وصل شود؛ همه باید از همین دو تابع استفاده کنند تا بتوان
 همزمان از هر سه نوع پنل (و از چند نمونه همزمان از هر نوع) پشتیبانی کرد.

هر `panel` یک dict (ردیف جدول vpn_panels) است شامل کلیدهای:
id, panel_type ('shahrah'|'marzban'|'pasargad'), name, base_url, api_key, username, password, enabled.
"""

import json
import uuid as uuid_lib
import shahrah
import marzban_panel
import pasargad_panel
import threexui_panel
import remnawave_panel
import marzneshin_panel
import hiddify_panel
import v2board_panel
import xboard_panel
import sui_panel
import alireza_xui_panel
import outline_panel
import wireguard_dashboard_panel
import rebeka_panel

PANEL_TYPE_LABELS = {
    "shahrah":             "شاهراه",
    "marzban":             "مرزبان",
    "pasargad":            "پاسارگاد (ویتوری)",
    "pasargad_wg":         "پاسارگاد (وایرگارد)",
    "threexui":            "3X-UI",
    "alireza_xui":         "Alireza X-UI",
    "remnawave":           "Remnawave",
    "marzneshin":          "Marzneshin",
    "hiddify":             "Hiddify",
    "v2board":             "V2Board",
    "xboard":              "XBoard",
    "sui":                 "S-UI",
    "outline":             "Outline",
    "wireguard_dashboard": "WireGuard Dashboard",
    "rebeka":              "ربکا (Rebeka)",
}

PANEL_TYPES = list(PANEL_TYPE_LABELS.keys())

# پنل‌هایی که علاوه بر link ساب، فایل WireGuard config هم تحویل می‌دهند
PANEL_WG_DELIVERY_TYPES = {"pasargad_wg", "wireguard_dashboard"}


# 🆕 کدام نوع پنل کدام روش‌های اتصال را پشتیبانی می‌کند. شاهراه از ابتدا فقط
# با API Key کار می‌کند (تغییری نکرده). مرزبان/پاسارگارد/3X-UI هم یوزرنیم/پسورد
# (پیش‌فرض قدیمی) و هم یک API Key ثابت را پشتیبانی می‌کنند و ادمین از پنل
# مدیریت ربات انتخاب می‌کند کدام‌یک برای هر نمونه پنل استفاده شود.
PANEL_AUTH_METHODS = {
    "shahrah":            ("api_key",),
    "marzban":            ("userpass", "api_key"),
    "pasargad":           ("userpass", "api_key"),
    "pasargad_wg":        ("userpass", "api_key"),
    "threexui":           ("userpass", "api_key"),
    "alireza_xui":        ("userpass", "api_key"),
    "remnawave":          ("api_key",),
    "marzneshin":         ("userpass", "api_key"),
    "hiddify":            ("api_key",),
    "v2board":            ("api_key",),
    "xboard":             ("api_key",),
    "sui":                ("userpass", "api_key"),
    "outline":            ("api_key",),
    "wireguard_dashboard":("api_key",),
    "rebeka":             ("api_key",),
}
AUTH_METHOD_LABELS = {
    "userpass": "👤 یوزرنیم و پسورد",
    "api_key": "🔑 API Key",
}

async def _panel_guard(panel: dict):
    pid = panel.get("id")
    if not pid:
        return True, None, None
    try:
        from resilience import before_panel_call, record_panel_result
        allowed, reason = await before_panel_call(int(pid))
        return allowed, reason, record_panel_result
    except Exception:
        return True, None, None

def _panel_done(record, panel_id, ok, started, error=None):
    if record and panel_id:
        import time
        record(int(panel_id), bool(ok), int((time.perf_counter()-started)*1000), error)


def panel_label(panel: dict) -> str:
    type_label = PANEL_TYPE_LABELS.get(panel.get("panel_type"), panel.get("panel_type") or "?")
    auth = panel.get("auth_method") or "userpass"
    auth_badge = AUTH_METHOD_LABELS.get(auth, auth)
    return f"{type_label} — {panel.get('name') or ('#' + str(panel.get('id')))} ({auth_badge})"


def _client(panel: dict):
    ptype = panel.get("panel_type")
    if ptype == "shahrah":            return shahrah
    if ptype == "marzban":            return marzban_panel
    if ptype in ("pasargad", "pasargad_wg"): return pasargad_panel
    if ptype == "threexui":           return threexui_panel
    if ptype == "alireza_xui":        return alireza_xui_panel
    if ptype == "remnawave":          return remnawave_panel
    if ptype == "marzneshin":         return marzneshin_panel
    if ptype == "hiddify":            return hiddify_panel
    if ptype == "v2board":            return v2board_panel
    if ptype == "xboard":             return xboard_panel
    if ptype == "sui":                return sui_panel
    if ptype == "outline":            return outline_panel
    if ptype == "wireguard_dashboard":return wireguard_dashboard_panel
    if ptype == "rebeka":             return rebeka_panel
    return None


async def test_connection(panel: dict) -> tuple[bool, dict | None, str]:
    ptype = panel.get("panel_type")
    if ptype == "shahrah":             return await shahrah.get_me(panel)
    if ptype == "marzban":             return await marzban_panel.test_connection(panel)
    if ptype in ("pasargad", "pasargad_wg"): return await pasargad_panel.test_connection(panel)
    if ptype == "threexui":            return await threexui_panel.test_connection(panel)
    client = _client(panel)
    if client: return await client.test_connection(panel)
    return False, None, f"نوع پنل '{ptype}' شناخته نیست."


async def get_catalog(panel: dict) -> tuple[list[dict], str]:
    """فهرست بسته/تمپلیت‌های قابل‌نگاشت روی این نمونه‌ی پنل را به یک قالب یکسان
    (idx/ref/name/label) برمی‌گرداند تا منوی نگاشت بدون توجه به نوع پنل یکسان باشد."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.get_plans(panel)
        if not ok:
            return [], msg
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("plans") or data.get("data") or []
        choices = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            slug = it.get("slug") or it.get("planSlug")
            if not slug:
                continue
            name = it.get("name") or it.get("title") or slug
            label = f"📦 {name} ({slug})"
            if len(label) > 60:
                label = label[:57] + "..."
            choices.append({"idx": i, "ref": slug, "name": name, "label": label})
        if not choices:
            return [], "هیچ بسته‌ای در پاسخ /plans پیدا نشد."
        return choices, "موفق"

    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype in ("pasargad", "pasargad_wg") else None
    if client is not None:
        ok, data, msg = await client.get_templates(panel, force_refresh=True)
        if not ok:
            return [], msg
        items = data if isinstance(data, list) else []
        choices = []
        for i, it in enumerate(items):
            if not isinstance(it, dict) or it.get("id") is None:
                continue
            ref = str(it["id"])
            name = it.get("name") or f"Template {ref}"
            label = f"📦 {name} (id: {ref})"
            if len(label) > 60:
                label = label[:57] + "..."
            choices.append({"idx": i, "ref": ref, "name": name, "label": label})
        if not choices:
            return [], "هیچ تمپلیتی در پنل پیدا نشد. اول یک تمپلیت در خود پنل بساز."
        return choices, "موفق"

    if ptype == "threexui":
        # 🆕 3X-UI اصلاً مفهوم «تمپلیت» ندارد؛ خود اینباند دقیقاً همان چیزی است
        # که پلن مستقیماً به آن نگاشت می‌شود، پس اینباندهای زنده‌ی سرور همان
        # «کاتالوگ» این نوع پنل‌اند.
        ok, data, msg = await threexui_panel.get_inbounds(panel, force_refresh=True)
        if not ok:
            return [], msg
        items = data if isinstance(data, list) else []
        choices = []
        for i, it in enumerate(items):
            if not isinstance(it, dict) or it.get("id") is None:
                continue
            ref = str(it["id"])
            remark = it.get("remark") or f"Inbound {ref}"
            protocol = it.get("protocol") or "?"
            port = it.get("port")
            label = f"🔌 {remark} ({protocol}:{port})" if port else f"🔌 {remark} ({protocol})"
            if len(label) > 60:
                label = label[:57] + "..."
            choices.append({"idx": i, "ref": ref, "name": remark, "label": label})
        if not choices:
            return [], "هیچ اینباندی روی این پنل 3X-UI پیدا نشد. اول از خود پنل حداقل یک Inbound بساز."
        return choices, "موفق"

    return [], "نوع پنل نامعتبر."


async def get_panel_info(panel: dict):
    """Lightweight authoritative health check for each panel type."""
    import time
    allowed, reason, record = await _panel_guard(panel)
    if not allowed: return False, None, reason
    started=time.perf_counter(); ptype=panel.get("panel_type")
    try:
        if ptype == "shahrah": ok,data,msg = await shahrah.get_me(panel)
        elif ptype == "marzban": ok,data,msg = await marzban_panel.get_system_stats(panel)
        elif ptype in ("pasargad","pasargad_wg"): ok,data,msg = await pasargad_panel.get_system_stats(panel)
        elif ptype == "threexui": ok,data,msg = await threexui_panel.test_connection(panel)
        else:
            client = _client(panel)
            if client: ok,data,msg = await client.test_connection(panel)
            else: return False,None,f"نوع پنل '{ptype}' شناخته نیست."
        _panel_done(record,panel.get("id"),ok,started,None if ok else msg)
        return ok,data,msg
    except Exception as exc:
        _panel_done(record,panel.get("id"),False,started,str(exc)); return False,None,str(exc)

async def get_direct_catalog(panel: dict) -> tuple[list[dict], str]:
    """گزینه‌های «بدون تمپلیت» این نمونه پنل را برمی‌گرداند: برای مرزبان
    اینباندهای فعال سرور (GET /api/inbounds)، برای پاسارگارد گروه‌های تعریف‌شده
    (GET /api/groups/simple). شاهراه اصلاً این حالت را ندارد (تمام API آن
    plan-محور است، نه inbound/group-محور)."""
    ptype = panel.get("panel_type")
    if ptype == "marzban":
        ok, data, msg = await marzban_panel.get_inbounds(panel, force_refresh=True)
        if not ok:
            return [], msg
        choices = []
        if isinstance(data, dict):
            idx = 0
            for protocol, items in data.items():
                for it in (items or []):
                    tag = it.get("tag") if isinstance(it, dict) else None
                    if not tag:
                        continue
                    choices.append({
                        "idx": idx, "key": f"{protocol}:{tag}", "protocol": protocol, "raw_id": tag,
                        "label": f"🔌 {tag} ({protocol})",
                    })
                    idx += 1
        if not choices:
            return [], "هیچ اینباندی روی این پنل مرزبان پیدا نشد."
        return choices, "موفق"

    if ptype in ("pasargad", "pasargad_wg"):
        ok, data, msg = await pasargad_panel.get_groups_simple(panel, force_refresh=True)
        if not ok:
            return [], msg
        choices = []
        items = data if isinstance(data, list) else []
        for idx, it in enumerate(items):
            if not isinstance(it, dict) or it.get("id") is None:
                continue
            choices.append({
                "idx": idx, "key": str(it["id"]), "protocol": None, "raw_id": it["id"],
                "label": f"🗂 {it.get('name') or ('گروه ' + str(it['id']))}",
            })
        if not choices:
            return [], "هیچ گروهی روی این پنل پاسارگارد تعریف نشده. اول از خود پنل حداقل یک Group بساز."
        return choices, "موفق"

    return [], "این نوع پنل حالت «بدون تمپلیت» ندارد (تمام کاتالوگ آن plan-محور است، نه inbound/group-محور)."


async def create_service(panel: dict, username: str, remote_ref: str, volume_gb=None, days=None, device_limit=None):
    """(ok, link, remote_service_id, raw_data, message)"""
    import time
    allowed, reason, _record = await _panel_guard(panel)
    if not allowed:
        return False, None, None, None, reason
    _started = time.perf_counter()
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.create_service(panel, remote_ref, username)
        if not ok:
            _panel_done(_record, panel.get("id"), False, _started, msg)
            return False, None, None, data, msg
        link, slug = shahrah.extract_link_and_slug(data)
        _panel_done(_record, panel.get("id"), True, _started)
        return True, link, slug or username, data, msg

    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype in ("pasargad", "pasargad_wg") else None
    if client is None and ptype != "threexui":
        return False, None, None, None, "نوع پنل نامعتبر."

    if ptype == "threexui":
        # 🆕 اینجا remote_ref همیشه شناسه‌ی همان اینباند نگاشت‌شده است (چون
        # 3X-UI اصلاً تمپلیت ندارد). service_id به‌شکل ترکیبی
        # "<inbound_id>:<client_uuid>" ذخیره می‌شود تا تمدید/حذف بعداً نیازی
        # به جست‌وجوی email در کل پنل نداشته باشد.
        ok, data, msg = await threexui_panel.create_user(panel, int(remote_ref), username, volume_gb, days)
        if not ok:
            return False, None, None, data, msg
        service_id = f"{data['inbound_id']}:{data['uuid']}"
        link = data.get("sub_link")
        _panel_done(_record, panel.get("id"), True, _started)
        return True, link, service_id, data, msg

    # 🆕 نگاشت «بدون تمپلیت»: remote_ref با پیشوند "direct:" و یک JSON بعدش
    # ذخیره شده (نگاه کن به vpn_catalog_pick_set / vpn_direct_confirm در
    # handlers/panel_admin.py). این یعنی به‌جای خواندن proxies/group_ids از
    # یک User Template، مستقیماً از همان انتخاب زنده‌ی ادمین استفاده می‌شود.
    if isinstance(remote_ref, str) and remote_ref.startswith("direct:"):
        try:
            direct = json.loads(remote_ref[len("direct:"):])
        except Exception:
            return False, None, None, None, "نگاشت «بدون تمپلیت» خراب شده؛ دوباره از پنل ادمین تنظیمش کن."
        if ptype in ("pasargad", "pasargad_wg"):
            ok, data, msg = await pasargad_panel.create_user_direct(
                panel, direct.get("group_ids") or [], username, volume_gb, days, device_limit=device_limit
            )
        else:
            ok, data, msg = await marzban_panel.create_user_direct(
                panel, direct.get("inbounds") or {}, username, volume_gb, days
            )
        if not ok:
            return False, None, None, data, msg
        link, uname = client.extract_link_and_username(panel, data)
        _panel_done(_record, panel.get("id"), True, _started)
        return True, link, uname or username, data, msg

    template_id = int(remote_ref)

    # fix: قبلاً برای هر دو نوع پنل یک فراخوانی مشترک با کوارگ device_limit=...
    # انجام می‌شد. امضای مرزبان اصلاً device_limit ندارد (این پارامتر فقط
    # مخصوص پاسارگارده)، پس هر بار که تمپلیت مرزبان استفاده می‌شد،
    # create_user_custom/create_user_from_template بلافاصله با
    # «TypeError: unexpected keyword argument 'device_limit'» کرش می‌کرد و کل
    # تحویل خودکار سرویس روی پنل مرزبان (چه دستی چه در فلوی خرید) شکست
    # می‌خورد. الان هر پنل با امضای واقعی خودش صدا زده می‌شود.
    if volume_gb is not None or days is not None:
        if ptype in ("pasargad", "pasargad_wg"):
            ok, data, msg = await pasargad_panel.create_user_custom(
                panel, template_id, username, volume_gb, days, device_limit=device_limit
            )
        else:
            ok, data, msg = await marzban_panel.create_user_custom(
                panel, template_id, username, volume_gb, days
            )
    else:
        if ptype in ("pasargad", "pasargad_wg"):
            ok, data, msg = await pasargad_panel.create_user_from_template(
                panel, template_id, username, device_limit=device_limit
            )
        else:
            ok, data, msg = await marzban_panel.create_user_from_template(
                panel, template_id, username
            )
    if not ok:
        return False, None, None, data, msg
    link, uname = client.extract_link_and_username(panel, data)
    _panel_done(_record, panel.get("id"), True, _started)
    return True, link, uname or username, data, msg


async def renew_service(panel: dict, service_id: str, remote_ref: str | None = None, volume_gb=None, days=None, device_limit=None, volume_unlimited=False, days_unlimited=False):
    """(ok, link, remote_service_id, raw_data, message)"""
    import time
    allowed, reason, _record = await _panel_guard(panel)
    if not allowed:
        return False, None, service_id, None, reason
    _started = time.perf_counter()
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.renew_service(panel, service_id, remote_ref)
        if not ok:
            return False, None, service_id, data, msg
        link, slug = shahrah.extract_link_and_slug(data)
        _panel_done(_record, panel.get("id"), True, _started)
        return True, link, slug or service_id, data, msg

    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype in ("pasargad", "pasargad_wg") else None
    if client is None and ptype != "threexui":
        return False, None, service_id, None, "نوع پنل نامعتبر."

    if ptype == "threexui":
        # 3X-UI هم مثل حالت «بدون تمپلیت» همیشه تمدید افزایشی است (هیچ مقدار
        # ثابتی از یک تمپلیت برای بازنشانی وجود ندارد).
        ok, data, msg = await threexui_panel.renew_user_custom(panel, service_id, volume_gb, days, volume_unlimited=volume_unlimited, days_unlimited=days_unlimited)
        if not ok:
            return False, None, service_id, data, msg
        link, uname = threexui_panel.extract_link_and_username(panel, data)
        _panel_done(_record, panel.get("id"), True, _started)
        return True, link, uname or service_id, data, msg

    # fix: همون باگ device_limit بالا (create_service) اینجا هم تکرار شده بود؛
    # renew_user/renew_user_custom مرزبان اصلاً device_limit قبول نمی‌کنن، پس
    # تمدید سرویس روی مرزبان همیشه با TypeError کرش می‌کرد.
    # 🆕 نگاشت «بدون تمپلیت» مقدار ثابتی برای تمدید ندارد (فقط دسترسی
    # اینباند/گروه را مشخص می‌کند، نه حجم/روز)؛ پس مثل حالت remote_ref=None
    # همیشه از تمدید افزایشی (custom) روی حجم/روز پلن استفاده می‌شود —
    # renew_user_custom اصلاً به inbounds/group_ids دست نمی‌زند، پس این کار
    # امن است چه سرویس اولیه از روی تمپلیت ساخته شده باشد چه مستقیم.
    is_direct = isinstance(remote_ref, str) and remote_ref.startswith("direct:")
    if remote_ref is not None and not is_direct:
        if ptype in ("pasargad", "pasargad_wg"):
            ok, data, msg = await pasargad_panel.renew_user(panel, service_id, int(remote_ref), device_limit=device_limit)
        else:
            ok, data, msg = await marzban_panel.renew_user(panel, service_id, int(remote_ref))
    else:
        if ptype in ("pasargad", "pasargad_wg"):
            ok, data, msg = await pasargad_panel.renew_user_custom(panel, service_id, volume_gb, days, device_limit=device_limit, volume_unlimited=volume_unlimited, days_unlimited=days_unlimited)
        else:
            ok, data, msg = await marzban_panel.renew_user_custom(panel, service_id, volume_gb, days, volume_unlimited=volume_unlimited, days_unlimited=days_unlimited)
    if not ok:
        return False, None, service_id, data, msg
    link, uname = client.extract_link_and_username(panel, data)
    _panel_done(_record, panel.get("id"), True, _started)
    return True, link, uname or service_id, data, msg


async def renew_existing_service(panel: dict, service_id: str, volume_gb=0, days=0, volume_unlimited=False, days_unlimited=False):
    """تمدید واقعی همان سرویس، بدون تعویض Template و بدون تغییر لینک ساب."""
    if not panel or not service_id:
        return False, None, "سرویس یا پنل برای تمدید مشخص نیست."
    ok, _link, _remote_id, _raw, msg = await renew_service(
        panel, service_id, remote_ref=None, volume_gb=volume_gb or 0, days=days or 0,
        volume_unlimited=volume_unlimited, days_unlimited=days_unlimited,
    )
    if not ok:
        return False, None, msg
    snap_ok, snap, snap_msg = await get_service_snapshot(panel, service_id)
    if not snap_ok or not isinstance(snap, dict):
        return False, None, f"تمدید ارسال شد اما تأیید سرویس از پنل ممکن نشد: {snap_msg}"
    return True, snap, "تمدید و تأیید شد"


async def disable_service(panel: dict, service_id: str) -> tuple[bool, str]:
    import time
    allowed, reason, _record = await _panel_guard(panel)
    if not allowed:
        return False, reason
    _started = time.perf_counter()
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.disable_service(panel, service_id)
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.disable_user(panel, service_id)
    elif ptype in ("pasargad", "pasargad_wg"):
        ok, data, msg = await pasargad_panel.disable_user(panel, service_id)
    elif ptype == "threexui":
        ok, data, msg = await threexui_panel.disable_user(panel, service_id)
    else:
        return False, "نوع پنل نامعتبر."
    _panel_done(_record, panel.get("id"), ok, _started, None if ok else msg)
    return ok, msg


async def enable_service(panel: dict, service_id: str) -> tuple[bool, str]:
    import time
    allowed, reason, _record = await _panel_guard(panel)
    if not allowed:
        return False, reason
    _started = time.perf_counter()
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.enable_service(panel, service_id)
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.enable_user(panel, service_id)
    elif ptype in ("pasargad", "pasargad_wg"):
        ok, data, msg = await pasargad_panel.enable_user(panel, service_id)
    elif ptype == "threexui":
        ok, data, msg = await threexui_panel.enable_user(panel, service_id)
    else:
        return False, "نوع پنل نامعتبر."
    _panel_done(_record, panel.get("id"), ok, _started, None if ok else msg)
    return ok, msg


async def regenerate_sub_link(panel: dict, service_id: str):
    """(ok, link, remote_service_id, raw_data, message) — فقط لینک ساب/توکن سرویس را عوض می‌کند بدون اینکه حجم یا تاریخ انقضای
    باقی‌مانده‌ی سرویس تغییر کند. برخلاف renew_service، اینجا هیچ پلان/حجم/روزی
    گرفته نمی‌شود چون قرار نیست چیزی اضافه یا جایگزین شود؛ فقط دسترسی قبلی
    (لینک قدیمی) قطع و یک لینک جدید صادر می‌شود."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        return False, None, service_id, None, "برای این نوع پنل امکان تغییر خودکار لینک بدون تغییر بسته وجود ندارد؛ لطفا با پشتیبانی تماس بگیرید."
    if ptype == "threexui":
        def mutate(c):
            c["subId"] = uuid_lib.uuid4().hex[:16]
            return c
        ok, data, msg = await threexui_panel._mutate_client(panel, service_id, mutate)
        if not ok:
            return False, None, service_id, data, msg
        link, uname = threexui_panel.extract_link_and_username(panel, data)
        return True, link, uname or service_id, data, msg
    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype in ("pasargad", "pasargad_wg") else None
    if client is None:
        return False, None, service_id, None, "نوع پنل نامعتبر."

    # قبل از Revoke لینک فعلی را از خود پنل می‌گیریم تا مطمئن شویم عملیات
    # واقعاً یک لینک/توکن جدید ساخته است و صرفاً دیتابیس را با همان لینک
    # قبلی overwrite نمی‌کنیم.
    old_link = None
    old_creds = None
    def _creds(d):
        if not isinstance(d, dict):
            return None
        return json.dumps([d.get("proxy_settings"), d.get("proxies")], sort_keys=True, default=str)
    try:
        ok_old, old_data, _ = await client.get_user(panel, service_id)
        if ok_old:
            old_link, _ = client.extract_link_and_username(panel, old_data)
            old_creds = _creds(old_data)
    except Exception:
        # نبودن لینک قبلی مانع Revoke نیست؛ خود endpoint پنل مرجع نهایی است.
        old_link = None

    # این endpoint روی مرزبان/پاسارگارد باید توکن/لینک قبلی را باطل کرده و
    # subscription جدید صادر کند.
    ok, data, msg = await client.revoke_sub(panel, service_id)
    if not ok:
        return False, None, service_id, data, msg

    link, uname = client.extract_link_and_username(panel, data)

    # بعضی نسخه‌های پنل پاسخ revoke را بدون subscription_url برمی‌گردانند؛
    # بعد از revoke دوباره خود سرویس را می‌خوانیم تا لینک جدید را بگیریم.
    if not link:
        try:
            ok_new, new_data, new_msg = await client.get_user(panel, service_id)
            if ok_new:
                link2, uname2 = client.extract_link_and_username(panel, new_data)
                link = link2 or link
                uname = uname2 or uname
            elif not msg:
                msg = new_msg
        except Exception:
            pass

    if not link:
        return False, None, service_id, data, "Revoke در پنل انجام شد اما لینک جدید از پنل دریافت نشد؛ لینک قبلی در دیتابیس جایگزین نشد."

    # اگر پنل همان URL قبلی را برگرداند، نباید به کاربر اعلام کنیم که لینک
    # قبلی باطل شده؛ چون هدف این قابلیت دقیقاً قطع دسترسی لینک قبلی است.
    if old_link and link.strip() == old_link.strip():
        # بعضی نسخه‌های پاسارگارد آدرس ساب را از روی نام کاربر می‌سازند و بعد از Revoke تغییر نمی‌کند،
        # ولی UUID/کلیدهای اتصال داخل ساب عوض می‌شوند (کانفیگ‌های قبلی از کار می‌افتند).
        # اگر این تغییر در خود پنل دیده شد، Revoke موفق است و فقط آدرس ثابت مانده.
        try:
            ok_new, new_data, _ = await client.get_user(panel, service_id)
            new_creds = _creds(new_data) if ok_new else None
        except Exception:
            new_creds = None
        if old_creds and new_creds and old_creds != new_creds:
            return True, link, uname or service_id, data, "SAME_LINK_CREDS_ROTATED"
        return False, None, service_id, data, "پنل لینک جدیدی صادر نکرد و همان لینک قبلی را برگرداند؛ تغییر لینک لغو شد."

    return True, link, uname or service_id, data, msg


async def reduce_service_quota(panel: dict, service_id: str, gb: float) -> tuple[bool, str]:
    """🆕 جریمه‌ی رفرال: gb گیگابایت مستقیماً از حجم باقی‌مانده‌ی سرویس در
    پنل کم می‌کند. شاهراه پشتیبانی نمی‌شود چون API آن plan-محور است و
    endpoint ای برای ویرایش مستقیم data_limit یک سرویس ندارد."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        return False, "پنل شاهراه امکان کسر مستقیم حجم از یک سرویس را ندارد (API آن plan-محور است)."
    if ptype == "marzban":
        ok, data, msg = await marzban_panel.reduce_user_quota(panel, service_id, gb)
    elif ptype in ("pasargad", "pasargad_wg"):
        ok, data, msg = await pasargad_panel.reduce_user_quota(panel, service_id, gb)
    elif ptype == "threexui":
        ok, data, msg = await threexui_panel.reduce_user_quota(panel, service_id, gb)
    else:
        return False, "نوع پنل نامعتبر."
    return ok, msg


async def reduce_service_days(panel: dict, service_id: str, days: float) -> tuple[bool, str]:
    """کم‌کردن مستقیم روز از سرویس (برای کاهش زمان توسط نماینده)."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        return False, "پنل شاهراه امکان کاهش مستقیم زمان را ندارد."
    if ptype == "marzban":
        ok, _d, msg = await marzban_panel.reduce_user_days(panel, service_id, days)
    elif ptype in ("pasargad", "pasargad_wg"):
        ok, _d, msg = await pasargad_panel.reduce_user_days(panel, service_id, days)
    elif ptype == "threexui":
        ok, _d, msg = await threexui_panel.reduce_user_days(panel, service_id, days)
    else:
        return False, "نوع پنل نامعتبر."
    return ok, msg


async def delete_service(panel: dict, service_id: str) -> tuple[bool, str]:
    """شاهراه API حذف مستقیم ندارد؛ برای این نوع فقط سرویس را گیر‌فعال می‌کنیم."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.disable_service(panel, service_id)
        return ok, msg
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.delete_user(panel, service_id)
    elif ptype in ("pasargad", "pasargad_wg"):
        ok, data, msg = await pasargad_panel.delete_user(panel, service_id)
    elif ptype == "threexui":
        ok, data, msg = await threexui_panel.delete_user(panel, service_id)
    else:
        return False, "نوع پنل نامعتبر."
    return ok, msg


async def get_service_snapshot(panel: dict, service_id: str) -> tuple[bool, dict | None, str]:
    """داده زنده سرویس را از خود پنل می‌خواند. شاهراه از endpoint سرویس،
    مرزبان/پاسارگارد از endpoint کاربر استفاده می‌کنند؛ لینک ساب برای این دو
    هرگز منبع محاسبه مصرف نیست."""
    import time
    allowed, reason, _record = await _panel_guard(panel)
    if not allowed:
        return False, None, reason
    _started = time.perf_counter()
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.get_service(panel, service_id)
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.get_user(panel, service_id)
    elif ptype in ("pasargad", "pasargad_wg"):
        ok, data, msg = await pasargad_panel.get_user(panel, service_id)
    elif ptype == "threexui":
        try:
            inbound_id_str, client_uuid = service_id.split(":", 1)
        except Exception:
            return False, None, "شناسه‌ی سرویس این پنل نامعتبر است."
        inbound_raw, err = await threexui_panel._get_inbound_raw(panel, int(inbound_id_str))
        if inbound_raw is None:
            return False, None, err or "این اینباند روی پنل پیدا نشد."
        clients = threexui_panel._parse_clients(inbound_raw)
        found = next((c for c in clients if c.get("id") == client_uuid), None)
        if not found:
            return False, None, "این کلاینت روی پنل پیدا نشد (شاید قبلاً حذف شده)."
        ok, traffic, _ = await threexui_panel.get_client_traffic(panel, found.get("email"))
        node = {
            "used_traffic": (traffic or {}).get("up", 0) + (traffic or {}).get("down", 0) if isinstance(traffic, dict) else 0,
            "data_limit": found.get("totalGB"),
            "total": (int(found.get("totalGB")) if found.get("totalGB") else None),
            "used": ((traffic or {}).get("up", 0) + (traffic or {}).get("down", 0)) if isinstance(traffic, dict) else 0,
            "upload": (traffic or {}).get("up", 0) if isinstance(traffic, dict) else 0,
            "download": (traffic or {}).get("down", 0) if isinstance(traffic, dict) else 0,
            "username": found.get("email") or service_id,
            "expire": (found.get("expiryTime") or 0) // 1000 if found.get("expiryTime") else 0,
            "status": "active" if found.get("enable") else "disabled",
        }
        _panel_done(_record, panel.get("id"), True, _started)
        return True, node, "موفق"
    else:
        return False, None, "نوع پنل نامعتبر."
    if not ok:
        return False, data, msg
    node = data if isinstance(data, dict) else {}
    def pick(keys):
        for k in keys:
            v = node.get(k)
            if v is not None:
                return v
        # one-level nested common containers
        for parent in ("data", "service", "user", "traffic", "usage", "stats"):
            sub = node.get(parent)
            if isinstance(sub, dict):
                for k in keys:
                    if sub.get(k) is not None:
                        return sub.get(k)
        return None
    total = pick(("data_limit", "traffic_limit", "limit", "total"))
    used = pick(("used_traffic", "used", "traffic_used", "consumed"))
    upload = pick(("used_traffic_up", "upload", "up"))
    download = pick(("used_traffic_down", "download", "down"))
    if used is None and (upload is not None or download is not None):
        try: used = (upload or 0) + (download or 0)
        except Exception: pass
    expire = pick(("expire", "expires_at", "expiration", "expire_at"))
    status = pick(("status", "state"))
    link = pick(("subscription_url", "subscriptionUrl", "sub_url", "subLink", "url"))
    username = pick(("username", "slug", "serviceSlug", "service_id")) or service_id
    try:
        total = int(total) if total is not None else None
    except Exception: total = None
    try:
        used = int(used) if used is not None else None
    except Exception: used = None
    raw_node = node
    if ptype in ("pasargad", "pasargad_wg") and isinstance(node, dict):
        # دستگاه‌های ثبت‌شده (HWID) در خود پاسخ کاربر نیست؛ جدا خوانده و به raw اضافه می‌شود
        # تا «تعداد دستگاه‌ها / آخرین دستگاه» از پنل میرور شوند.
        try:
            ok_h, devs = await asyncio.wait_for(pasargad_panel.get_user_hwids(panel, service_id), timeout=3.0)
            if ok_h and devs is not None:
                raw_node = {**node, "hwid_devices": devs}
        except Exception:
            pass
    return True, {"total": total, "used": used, "upload": upload, "download": download, "expire": expire, "status": status, "link": link, "username": username, "raw": raw_node}, msg


def extract_panel_configs(snapshot: dict) -> list[str]:
    """کانفیگ‌های برگشتی مستقیم از پاسخ پنل؛ بدون درخواست mirror/subscription."""
    raw = snapshot.get("raw") if isinstance(snapshot, dict) else {}
    out = []
    def walk(obj):
        if isinstance(obj, dict):
            for k,v in obj.items():
                if isinstance(v, str) and v.startswith(("vless://","vmess://","trojan://","ss://","ssr://","wg://","wireguard://")):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj: walk(v)
    walk(raw)
    return list(dict.fromkeys(out))

# ───────────────────────────────────────────────────────────────────────────────
# 🪂 WireGuard Config Delivery (pasargad_wg / wireguard_dashboard)
async def get_wireguard_config(panel: dict, service_id: str) -> tuple[bool, str | None, str]:
    """کانفیگ WireGuard رو به شکل متن .conf برمی‌گرداند.
    برای pasargad_wg از پاسخ پنل می‌خواند."""
    ptype = panel.get("panel_type")
    if ptype == "pasargad_wg":
        # پاسارگاد فایل WireGuard رو به صورت peer config برمیگرداند۔
        # ابتدا اطلاعات سرویس رو می‌گیریم سپس کانفیگ WG رو ساخته بریم
        ok, data, msg = await pasargad_panel.get_user(panel, service_id)
        if not ok:
            return False, None, f"نتوانستم اطلاعات سرویس رو بگیرم: {msg}"
        # داده‌های WireGuard رو از پاسخ پنل استخراج کن
        if isinstance(data, dict):
            wg_conf = data.get("wireguard_config") or data.get("wg_config") or data.get("conf")
            if wg_conf:
                return True, wg_conf, "موفق"
            # سعی برای ساختن .conf از روی داده‌های wg_ موجود
            priv = data.get("wg_private_key") or data.get("private_key")
            pub  = data.get("wg_server_pub") or data.get("server_public_key")
            addr = data.get("wg_address") or data.get("wg_ip")
            ep   = data.get("wg_endpoint") or data.get("endpoint")
            dns  = data.get("wg_dns") or data.get("dns") or "1.1.1.1"
            if priv and pub and ep:
                conf_text = "[Interface]\nPrivateKey = {}\nAddress = {}\nDNS = {}\n\n[Peer]\nPublicKey = {}\nAllowedIPs = 0.0.0.0/0\nEndpoint = {}\n".format(
                    priv, addr or '10.8.0.2/32', dns, pub, ep
                )
                return True, conf_text, "موفق"
    elif ptype == "wireguard_dashboard":
        ok, data, msg = await wireguard_dashboard_panel.get_user(panel, service_id)
        if ok and isinstance(data, dict):
            conf_text = data.get("config_file") or data.get("conf")
            if conf_text:
                return True, conf_text, "موفق"
    return False, None, "کانفیگ WireGuard در پاسخ پنل یافت نشد."


def needs_wg_delivery(panel: dict) -> bool:
    """آیا علاوه بر link ساب، فایل WG هم باید برای کاربر فرستاده شود."""
    return panel.get("panel_type") in PANEL_WG_DELIVERY_TYPES


def _split_wg_text(text: str) -> list[str]:
    """متن شامل یک یا چند بلوک [Interface]...[Peer] را به چند .conf جدا می‌شکند."""
    import re as _re
    text = (text or "").replace("\r\n", "\n").strip()
    if "[Interface]" not in text:
        return []
    parts = [p.strip() for p in _re.split(r"(?=^\[Interface\])", text, flags=_re.M) if "[Interface]" in p]
    return [p + "\n" for p in parts]


def parse_wireguard_payload(raw: bytes) -> list[tuple[str, bytes]]:
    """خروجی /wireguard ساب را (zip / JSON / متن چندبخشی / URI) به لیست (نام فایل، محتوا) تبدیل می‌کند."""
    import io, zipfile
    files: list[tuple[str, bytes]] = []
    if not raw:
        return files
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for n in z.namelist():
                    if n.endswith("/"):
                        continue
                    files.append((n.rsplit("/", 1)[-1] or "wireguard.conf", z.read(n)))
            return files
        except Exception:
            return []
    try:
        text = raw.decode("utf-8", "ignore").strip()
    except Exception:
        return []
    if text[:1] in "[{":
        try:
            obj = json.loads(text)
            found: list[tuple[str, str]] = []
            def walk(o, name=""):
                if isinstance(o, str):
                    if "[Interface]" in o:
                        found.append((name, o))
                elif isinstance(o, dict):
                    for k, v in o.items():
                        walk(v, str(k) if isinstance(v, str) else name)
                elif isinstance(o, list):
                    for v in o:
                        walk(v, name)
            walk(obj)
            for i, (nm, c) in enumerate(found, 1):
                for j, part in enumerate(_split_wg_text(c), 1):
                    files.append(((nm if nm.endswith(".conf") else f"{nm or 'wireguard'}") + ("" if nm.endswith(".conf") else f"-{i}.conf"), part.encode("utf-8")))
            if files:
                return files
        except Exception:
            pass
    chunks = _split_wg_text(text)
    for i, c in enumerate(chunks, 1):
        name = "wireguard.conf" if len(chunks) == 1 else f"wireguard-{i}.conf"
        files.append((name, c.encode("utf-8")))
    if files:
        return files
    uris = [l.strip() for l in text.splitlines() if l.strip().lower().startswith(("wireguard://", "wg://"))]
    if uris:
        files.append(("wireguard-links.txt", ("\n".join(uris) + "\n").encode("utf-8")))
    return files


async def get_wireguard_files(panel: dict, service_id: str, sub_url: str | None = None) -> tuple[bool, list[tuple[str, bytes]], str]:
    """فایل‌های کانفیگ وایرگارد سرویس را برمی‌گرداند: [(filename, bytes)].
    پاسارگارد: خروجی `<ساب>/wireguard`؛ در صورت نبود، fallback به get_wireguard_config."""
    ptype = (panel or {}).get("panel_type")
    files: list[tuple[str, bytes]] = []
    if ptype == "pasargad_wg":
        url = (sub_url or "").strip()
        if not url and service_id:
            try:
                ok, snap, _m = await get_service_snapshot(panel, service_id)
                if ok and snap:
                    url = str(snap.get("link") or "")
                    if url.startswith("/"):
                        url = str(panel.get("base_url") or "").rstrip("/") + url
            except Exception:
                url = ""
        if url.lower().startswith(("http://", "https://")):
            base = url.split("?", 1)[0].rstrip("/")
            for tail in ("/wireguard", ""):
                try:
                    timeout = aiohttp.ClientTimeout(total=15, connect=6)
                    async with aiohttp.ClientSession(timeout=timeout) as sess:
                        async with sess.get(base + tail, headers={"User-Agent": "wireguard"}) as r:
                            if r.status != 200:
                                continue
                            files = parse_wireguard_payload(await r.read())
                except Exception:
                    files = []
                if files:
                    break
    if not files:
        ok, conf, msg = await get_wireguard_config(panel, service_id)
        if ok and conf:
            for i, c in enumerate(_split_wg_text(conf) or [conf], 1):
                files.append(("wireguard.conf" if i == 1 else f"wireguard-{i}.conf", c.encode("utf-8")))
    if not files:
        return False, [], "فایل وایرگارد برای این سرویس پیدا نشد."
    seen: dict[str, int] = {}
    out = []
    for n, b in files:
        k = seen.get(n, 0); seen[n] = k + 1
        if k:
            stem, dot, ext = n.rpartition(".")
            n = f"{stem}-{k+1}.{ext}" if dot else f"{n}-{k+1}"
        out.append((n, b))
    return True, out, "موفق"


# ───────────────────────────────────────────────────────────────────────────────
# 🔗 تشخیص کاربر واقعی پنل از روی لینک ساب (برای «ارسال دستی»)
def _sub_tokens_from_url(sub_url: str) -> list[str]:
    from urllib.parse import urlparse
    try:
        segs = [x for x in urlparse(sub_url.strip()).path.split("/") if x]
    except Exception:
        return []
    out = []
    for i, seg in enumerate(segs):
        if seg.lower() in ("sub", "info", "usage", "v2ray", "clash", "sing-box", "singbox", "outline", "links", "json"):
            continue
        out.append(seg)
    # توکن معمولاً بعد از /sub/ می‌آید؛ اگر چنین چیزی بود همان را اول بگذار
    low = [x.lower() for x in segs]
    if "sub" in low:
        i = low.index("sub")
        if i + 1 < len(segs):
            t = segs[i + 1]
            out = [t] + [x for x in out if x != t]
    return out


def _usernames_from_token(token: str) -> list[str]:
    """مرزبان/پاسارگارد یوزرنیم را داخل خود توکن می‌گذارند (JWT یا base64('user,ts'))؛ بدون تماس با پنل."""
    import base64, json
    found: list[str] = []

    def _b64(txt: str) -> bytes | None:
        try:
            return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))
        except Exception:
            return None

    parts = token.split(".")
    if len(parts) == 3:
        raw = _b64(parts[1])
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8", "ignore"))
                for k in ("sub", "username", "user", "name"):
                    v = payload.get(k) if isinstance(payload, dict) else None
                    if isinstance(v, str) and v.strip():
                        found.append(v.strip())
            except Exception:
                pass
    for cut in (10, 0):
        body = token[:-cut] if cut and len(token) > cut else token
        raw = _b64(body)
        if raw:
            txt = raw.decode("utf-8", "ignore")
            if "," in txt:
                u = txt.rsplit(",", 1)[0].strip()
                if u and u.isprintable():
                    found.append(u)
    return list(dict.fromkeys(found))


async def _username_from_sub_info(sub_url: str) -> str | None:
    """GET <sub>/info (در مرزبان/پاسارگارد بدون احراز هویت) یوزرنیم را برمی‌گرداند."""
    from urllib.parse import urlparse
    try:
        u = urlparse(sub_url.strip())
        segs = [x for x in u.path.split("/") if x]
        low = [x.lower() for x in segs]
        if "sub" not in low or low.index("sub") + 1 >= len(segs):
            return None
        i = low.index("sub")
        base = f"{u.scheme}://{u.netloc}/" + "/".join(segs[: i + 2]) + "/info"
        timeout = aiohttp.ClientTimeout(total=8, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(base, ssl=False) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        if isinstance(data, dict):
            user_node = data.get("user") if isinstance(data.get("user"), dict) else data
            name = user_node.get("username")
            return str(name).strip() if name else None
    except Exception:
        return None
    return None


async def resolve_manual_link(sub_url: str, name_hint: str | None = None):
    """لینک ساب دستی را به کاربر واقعی در یکی از پنل‌های مرزبان/پاسارگارد وصل می‌کند.
    خروجی: {"panel", "panel_type", "service_id", "username", "snapshot"} یا None.
    ترتیب: یوزرنیم داخل توکن → /info → (ضعیف‌تر) نام Profile-Title با تطبیق توکن."""
    if not sub_url or not str(sub_url).lower().startswith(("http://", "https://")):
        return None
    import database as _db
    panels_list = [p for p in _db.list_vpn_panels(enabled_only=True) if p.get("panel_type") in ("pasargad", "pasargad_wg", "marzban")]
    if not panels_list:
        return None

    tokens = _sub_tokens_from_url(sub_url)
    strong: list[str] = []
    for t in tokens:
        strong += _usernames_from_token(t)
    info_name = await _username_from_sub_info(sub_url)
    if info_name:
        strong.insert(0, info_name)
    strong = list(dict.fromkeys(x for x in strong if x))
    weak = [name_hint.strip()] if name_hint and name_hint.strip() and name_hint.strip() not in strong else []

    def _tok(link: str | None) -> str:
        t = _sub_tokens_from_url(link or "")
        return t[0] if t else ""

    my_token = tokens[0] if tokens else ""
    for candidates, need_token_match in ((strong, False), (weak, True)):
        for uname in candidates:
            for panel in panels_list:
                client = pasargad_panel if panel["panel_type"] in ("pasargad", "pasargad_wg") else marzban_panel
                try:
                    ok, data, _m = await client.get_user(panel, uname)
                except Exception:
                    continue
                if not ok or not isinstance(data, dict):
                    continue
                real_name = data.get("username") or uname
                link, _ = client.extract_link_and_username(panel, data)
                if need_token_match and not (my_token and _tok(link) == my_token):
                    continue
                snap_ok, snap, _ = await get_service_snapshot(panel, real_name)
                return {"panel": panel, "panel_type": panel["panel_type"], "service_id": real_name,
                        "username": real_name, "snapshot": snap if snap_ok else None}
    return None
