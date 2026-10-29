"""
database.py
لایه‌ی کامل دسترسی به دیتابیس SQLite.
هیچ فایل دیگری در پروژه نباید مستقیماً sqlite3 را import کند؛
همه باید از طریق توابع همین فایل با دیتابیس کار کنند.
"""

import sqlite3
import threading
import secrets
import string
import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta

import cache

from config import (
    DATABASE_PATH, REFERRAL_LOCK_AMOUNT, REFERRAL_MIN_VOLUME_GB, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN,
    VIP_PLANS, FREE_TEST_PLAN_KEY, FREE_TEST_PLAN,
    SHAHRAH_BASE_URL, SHAHRAH_API_KEY, SHAHRAH_ENABLED,
    CUSTOM_BUILD_PRICE_PER_GB, CUSTOM_BUILD_PRICE_PER_30_DAYS,
    CUSTOM_BUILD_MIN_GB, CUSTOM_BUILD_MAX_GB, CUSTOM_BUILD_MIN_DAYS, CUSTOM_BUILD_MAX_DAYS,
)

_local = threading.local()
_lock = threading.Lock()  # برای جلوگیری از تداخل نوشتن همزمان
logger = logging.getLogger(__name__)

# اگر آدرس Turso تنظیم شده باشد، از دیتابیس ابری (دائمی) استفاده می‌کنیم؛
# در غیر این صورت، از فایل SQLite محلی (مناسب تست روی سیستم شخصی) استفاده می‌شود.
USE_TURSO = bool(TURSO_DATABASE_URL)


def _is_stale_turso_stream_error(exc: Exception) -> bool:
    """تشخیص خطای مشخص Hrana/Turso وقتی stream یک اتصال (به‌خاطر بی‌کاری
    طولانی) توسط سرور بسته/منقضی شده و دیگر معتبر نیست."""
    msg = str(exc)
    return "stream not found" in msg or "StreamExpired" in msg or ("Hrana" in msg and "404" in msg)


class _ResilientTursoCursor:
    """Cursor روی یک اتصال Turso/libsql؛ اگر خطای stream منقضی‌شده بگیرد،
    یک‌بار به‌صورت خودکار دوباره وصل می‌شود و همان کوئری را تکرار می‌کند.

    fix: چون یک اتصال libsql برای هر ترد (thread-local) کش می‌شد و برای
    مدت طولانی (بین دو درخواست بی‌کار) نگه داشته می‌شد، سرور Turso
    stream مربوط به آن را می‌بست. اولین کوئری بعدی روی همان اتصال قدیمی با
    «stream not found (404)» شکست می‌خورد و کل آپدیت تلگرام کرش می‌کرد.
    """

    def __init__(self, owner: "_ResilientTursoConnection"):
        self._owner = owner
        self._cur = owner._raw().cursor()

    def execute(self, sql, params=()):
        try:
            return self._cur.execute(sql, params)
        except Exception as exc:
            if _is_stale_turso_stream_error(exc):
                logger.warning("اتصال Turso منقضی شده بود؛ دوباره وصل شدیم و کوئری را تکرار کردیم.")
                self._owner._reconnect()
                self._cur = self._owner._raw().cursor()
                return self._cur.execute(sql, params)
            raise

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _ResilientTursoConnection:
    """Wrapper نازک روی اتصال libsql که در صورت منقضی‌شدن stream، به‌صورت
    شفاف دوباره وصل می‌شود؛ امضای آن دقیقاً مثل یک اتصال sqlite3 معمولی است
    تا بقیه‌ی کد (که همه‌جا get_connection().cursor() صدا می‌زند) بدون هیچ
    تغییری کار کند."""

    def __init__(self):
        self._conn = None
        self._connect()

    def _connect(self):
        import libsql
        self._conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)

    def _reconnect(self):
        self._conn = None
        self._connect()

    def _raw(self):
        if self._conn is None:
            self._connect()
        return self._conn

    def cursor(self):
        return _ResilientTursoCursor(self)

    def execute(self, sql, params=()):
        try:
            return self._raw().execute(sql, params)
        except Exception as exc:
            if _is_stale_turso_stream_error(exc):
                logger.warning("اتصال Turso منقضی شده بود؛ دوباره وصل شدیم و کوئری را تکرار کردیم.")
                self._reconnect()
                return self._raw().execute(sql, params)
            raise

    def commit(self):
        self._raw().commit()

    def rollback(self):
        self._raw().rollback()

    def __getattr__(self, name):
        return getattr(self._raw(), name)


def get_connection():
    if not hasattr(_local, "conn"):
        if USE_TURSO:
            _local.conn = _ResilientTursoConnection()
        else:
            db_dir = os.path.dirname(DATABASE_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            _local.conn = conn
    return _local.conn


def _fetchone(cur):
    """یک سطر را از cursor می‌خواند و به دیکشنری تبدیل می‌کند.
    (به‌جای conn.row_factory چون libsql از آن پشتیبانی نمی‌کند.)"""
    row = cur.fetchone()
    if row is None:
        return None
    return {desc[0]: row[idx] for idx, desc in enumerate(cur.description)}


def _fetchall(cur):
    """تمام سطرها را از cursor می‌خواند و هرکدام را به دیکشنری تبدیل می‌کند."""
    rows = cur.fetchall()
    if not rows:
        return []
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in rows]


@contextmanager
def transaction():
    conn = get_connection()
    cur = conn.cursor()
    with _lock:
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _migrate_legacy_shahrah_panel(cur):
    """مهاجرت یک‌باره: اگر پنل شاهراه در .env تنظیم شده (SHAHRAH_ENABLED) و هنوز
    هیچ ردیفی در vpn_panels از نوع 'shahrah' نباشد، یک نمونه‌ی پیش‌فرض برایش
    می‌سازد و تمام نگاشت‌های قدیمی shahrah_plan_map را به panel_plan_map کپی
    می‌کند (بدون حذف/تغییر جدول قدیمی) تا داده‌های قبلی از دست نروند و کاربر
    بدون هیچ تنظیم دستی اضافه‌ای، دقیقاً همان رفتار قبلی را ببیند."""
    now = _now()
    cur.execute("SELECT id FROM vpn_panels WHERE panel_type = 'shahrah' LIMIT 1")
    row = _fetchone(cur)
    panel_id = row["id"] if row else None

    if panel_id is None and SHAHRAH_ENABLED and SHAHRAH_BASE_URL:
        cur.execute(
            """INSERT INTO vpn_panels
                   (panel_type, name, base_url, api_key, username, password, enabled, sort_order, created_at)
               VALUES ('shahrah', ?, ?, ?, NULL, NULL, 1, 0, ?)""",
            ("شاهراه (پیش‌فرض از .env)", SHAHRAH_BASE_URL, SHAHRAH_API_KEY, now),
        )
        panel_id = cur.lastrowid

    if panel_id is None:
        return

    cur.execute("SELECT scope, scope_id, plan_slug, plan_name FROM shahrah_plan_map")
    legacy_rows = _fetchall(cur)
    for r in legacy_rows:
        cur.execute(
            """INSERT INTO panel_plan_map (scope, scope_id, panel_id, remote_ref, remote_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id) DO NOTHING""",
            (r["scope"], r["scope_id"], panel_id, r["plan_slug"], r["plan_name"], now),
        )


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # ⚡ PAYG: هر بار موجودی کیف پول «زیاد» شود (از هر مسیر: درگاه، ادمین، رفرال، مینی‌اپ…) کاربر در این صف می‌رود
    # تا حلقه‌ی PAYG ظرف چند ثانیه سرویس متوقف‌شده‌اش را فعال کند (بدون دست‌زدن به تک‌تک مسیرهای شارژ).
    cur.execute("CREATE TABLE IF NOT EXISTS payg_resume_queue (user_id INTEGER PRIMARY KEY)")
    # 🪫 هشدار کم‌شدن موجودی کیف پول: هر بار موجودی «کم» شود، موجودی قبلی در صف می‌رود تا حلقه‌ی اعلان بررسی کند
    # آیا از حد تعیین‌شده‌ی ادمین رد شده (قبلاً ≥ حد و حالا < حد).
    cur.execute("CREATE TABLE IF NOT EXISTS wallet_low_queue (user_id INTEGER PRIMARY KEY, old_wallet INTEGER NOT NULL)")


    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id         TEXT UNIQUE NOT NULL,
            name                TEXT NOT NULL,
            wallet              INTEGER NOT NULL DEFAULT 0,
            locked_wallet       INTEGER NOT NULL DEFAULT 0,
            total_purchase      INTEGER NOT NULL DEFAULT 0,
            joined              TEXT NOT NULL,
            referrer_id         INTEGER,
            invite_code         TEXT UNIQUE NOT NULL,
            invited_count       INTEGER NOT NULL DEFAULT 0,
            successful_invites  INTEGER NOT NULL DEFAULT 0,
            referral_wallet    INTEGER NOT NULL DEFAULT 0,
            is_blocked          INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (referrer_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_payg_wallet_credit
        AFTER UPDATE OF wallet ON users
        WHEN NEW.wallet > OLD.wallet AND NEW.wallet > 0
        BEGIN
            INSERT OR IGNORE INTO payg_resume_queue(user_id) VALUES (NEW.id);
        END
    """)


    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_wallet_low_debit
        AFTER UPDATE OF wallet ON users
        WHEN NEW.wallet < OLD.wallet
        BEGIN
            INSERT OR IGNORE INTO wallet_low_queue(user_id, old_wallet) VALUES (NEW.id, OLD.wallet);
        END
    """)

    # مهاجرت برای دیتابیس‌های قدیمی که قبل از اضافه‌شدن قابلیت «مسدودسازی
    # کاربر» ساخته شده‌اند.
    try:
        cur.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
    except Exception as exc:
        # 🐛 فیکس: روی Turso/libsql خطای ستون تکراری، sqlite3.OperationalError نیست (ممکن است ValueError/کلاس دیگری باشد)، برای همین باید هر نوع خطایی را بگیریم و فقط بر اساس متن تشخیص بدهیم.
        if "duplicate column" not in str(exc).lower():
            logging.getLogger(__name__).warning("migration users.is_blocked خطای نامنخواسته: %s", exc)

    # منشأ موجودی رفرالی: موجودی رفرال از کیف پول معمولی جداگانه ردیابی می‌شود
    # تا جریمه‌ی رفرال هیچ‌وقت به‌اشتباه روی سرویس/پولی که از شارژ عادی آمده
    # اعمال نشود. برای دیتابیس‌های قدیمی مقدار اولیه صفر است چون منشأ تاریخی
    # موجودی قبلی قابل تشخیص نیست و حدس‌زدن آن ناامن است.
    try:
        cur.execute("ALTER TABLE users ADD COLUMN referral_wallet INTEGER NOT NULL DEFAULT 0")
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            logging.getLogger(__name__).warning("migration users.referral_wallet خطای نامنخواسته: %s", exc)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'completed',
            description TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            plan        TEXT NOT NULL,
            config      TEXT NOT NULL,
            expiry      TEXT,
            created_at  TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'vip',
            service_id  TEXT,
            deleted     INTEGER NOT NULL DEFAULT 0,
            qr_file_id  TEXT,
            alert_80_sent     INTEGER NOT NULL DEFAULT 0,
            alert_90_sent     INTEGER NOT NULL DEFAULT 0,
            alert_expiry_sent INTEGER NOT NULL DEFAULT 0,
            alert_ended_sent  INTEGER NOT NULL DEFAULT 0,
            sub_link_disabled INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # مهاجرت ستون‌های جدید برای دیتابیس‌هایی که قبل از اضافه‌شدن این قابلیت‌ها
    # ساخته شده‌اند (اگر ستون از قبل موجود باشد فقط خطا را نادیده می‌گیریم).
    for ddl in (
        "ALTER TABLE configs ADD COLUMN type TEXT NOT NULL DEFAULT 'vip'",
        "ALTER TABLE configs ADD COLUMN custom_title TEXT",
        "ALTER TABLE configs ADD COLUMN service_id TEXT",
        "ALTER TABLE configs ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN qr_file_id TEXT",
        "ALTER TABLE configs ADD COLUMN alert_80_sent INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN alert_90_sent INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN alert_expiry_sent INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN alert_ended_sent INTEGER NOT NULL DEFAULT 0",
        # منشأ سرویس: 'manual' (ادمین دستی فرستاده) یا نام یک نوع پنل ('shahrah'/
        # 'marzban'/'pasargad') اگر به‌صورت خودکار از یک پنل ساخته شده باشد.
        # service_id در آن حالت همان slug/username سرویس در همان پنل است (برای
        # تمدید/فعال/غیرفعال‌کردن بعدی لازم است).
        "ALTER TABLE configs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
        # 🆕 چندپنلی: دقیقاً کدام «نمونه‌ی پنل» (ردیف vpn_panels) این سرویس مشخص
        # را ساخته/مدیریت می‌کند. چون حالا ممکن است چند نمونه از یک نوع پنل
        # هم‌زمان فعال باشند، تمدید/فعال/غیرفعال‌سازی باید همیشه سراغ همان
        # نمونه‌ی دقیقی برود که سرویس رویش ساخته شده، نه هر نمونه‌ای که فعلاً
        # برای آن پلن نگاشت شده (که ممکن است بعداً توسط ادمین عوض شده باشد).
        "ALTER TABLE configs ADD COLUMN panel_id INTEGER",
        "ALTER TABLE configs ADD COLUMN sub_link_disabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN product_name TEXT",
        # پلن/پوشه‌ی خریداری‌شده: تنظیمات تمدید (پلن/پوشه) با همین دو ستون روی سرویس اعمال می‌شود.
        "ALTER TABLE configs ADD COLUMN plan_key TEXT",
        "ALTER TABLE configs ADD COLUMN custom_option_num INTEGER",
        "ALTER TABLE configs ADD COLUMN category_id INTEGER",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logging.getLogger(__name__).warning("migration configs.* خطای نامنخواسته: %s", exc)


    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL,
            plan_key          TEXT,
            plan_name         TEXT NOT NULL,
            order_type        TEXT NOT NULL,
            price             INTEGER NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            target_config_id  INTEGER,
            created_at        TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # پرداخت‌های آنلاین (درگاه یونیک‌پی) — هر اینوویس ساخته‌شده تا زمان تایید
    # یا انقضا اینجا ردیابی می‌شود تا هم دکمه‌ی «بررسی پرداخت» و هم پولر
    # پس‌زمینه‌ی ربات بتوانند وضعیتش را چک کنند.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS online_payments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            hash_id           TEXT UNIQUE NOT NULL,
            user_id           INTEGER NOT NULL,
            telegram_id       TEXT NOT NULL,
            kind              TEXT NOT NULL DEFAULT 'plan',
            plan_key          TEXT,
            plan_name         TEXT NOT NULL,
            order_type        TEXT NOT NULL DEFAULT 'vip',
            price             INTEGER NOT NULL,
            discount_code     TEXT,
            payment_link      TEXT,
            ref_id            TEXT,
            status            TEXT NOT NULL DEFAULT 'pending',
            order_id          INTEGER,
            extra             TEXT,
            created_at        TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key    TEXT PRIMARY KEY,
            value  TEXT
        )
    """)

    # ⚠️ مهم: برچسب referral_funded منشأ واقعی سرویس است و نباید در migration
    # پاک شود. نسخه v3 با UPDATE configs SET referral_funded=0 سابقه‌ی سرویس‌های
    # رفرالی قبلی را از بین می‌برد و باعث می‌شد بعداً با وجود چند خرید عادی/کارت‌به‌کارت،
    # ربات دیگر سرویس رفرالی را پیدا نکند. از این نسخه به بعد این برچسب هرگز reset نمی‌شود.
    # اگر تراکنش‌های v3 با برچسب [referral-funded] وجود داشته باشند ولی به‌علت همان
    # reset، فلگ config پاک شده باشد، یک بار امن‌ترین config نزدیک به همان تراکنش
    # دوباره علامت‌گذاری می‌شود.
    try:
        cur.execute("SELECT value FROM settings WHERE key = ?", ("referral_funded_provenance_v4_repaired",))
        repaired = cur.fetchone()
        if not repaired or repaired[0] != "1":
            cur.execute("""
                SELECT t.id, t.user_id, t.created_at, t.description
                FROM transactions t
                WHERE t.type = 'purchase' AND COALESCE(t.description, '') LIKE '%[referral-funded]%'
                ORDER BY t.id ASC
            """)
            referral_txs = _fetchall(cur)
            for tx in referral_txs:
                # همان کاربر، نزدیک‌ترین سرویس ساخته‌شده بعد از تراکنش؛
                # حداکثر 15 دقیقه فاصله تا سرویس اشتباهی انتخاب نشود.
                cur.execute("""
                    SELECT id FROM configs
                    WHERE user_id = ? AND deleted = 0 AND referral_funded = 0
                      AND created_at >= ?
                      AND datetime(created_at) <= datetime(?, '+15 minutes')
                    ORDER BY datetime(created_at) ASC, id ASC LIMIT 1
                """, (tx['user_id'], tx['created_at'], tx['created_at']))
                cfg = _fetchone(cur)
                if cfg:
                    cur.execute("UPDATE configs SET referral_funded = 1 WHERE id = ?", (cfg['id'],))
            cur.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("referral_funded_provenance_v4_repaired", "1"),
            )
    except Exception as exc:
        logging.getLogger(__name__).warning("referral provenance repair failed: %s", exc)

    # 🐛 فیکس: قبلاً FSM (وضعیت مکالمه‌ی چندمرحله‌ای، مثل «در حال ساخت پلن VIP
    # جدید» یا «منتظر آپلود رسید») فقط در حافظه‌ی RAM (MemoryStorage) نگه
    # داشته می‌شد. اگر پروسه‌ی ربات هر دلیلی (دیپلوی مجدد، کرش، خواب رفتن
    # سرویس رایگان و...) ری‌استارت می‌شد، همه‌ی این وضعیت‌ها گم می‌شدند و
    # کاربر/ادمین وسط یک فرآیند چندمرحله‌ای بدون هیچ پیامی گیر می‌کرد. حالا
    # این وضعیت هم مثل بقیه‌ی داده‌ها در همان دیتابیس (SQLite/Turso) پایدار
    # ذخیره می‌شود؛ به fsm_storage.py (کلاس DBStorage) نگاه کنید.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fsm_storage (
            storage_key  TEXT PRIMARY KEY,
            state        TEXT,
            data         TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS discounts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            code                TEXT UNIQUE NOT NULL,
            percent             INTEGER NOT NULL,
            uses                INTEGER NOT NULL,
            created_at          TEXT NOT NULL,
            discount_type       TEXT NOT NULL DEFAULT 'percent',
            amount              INTEGER NOT NULL DEFAULT 0,
            applicable_plans    TEXT,
            min_order_amount    INTEGER NOT NULL DEFAULT 0,
            max_uses_per_user   INTEGER NOT NULL DEFAULT 0,
            expires_at          TEXT,
            allowed_user_ids    TEXT
        )
    """)

    # 🎫 سیستم تیکت پشتیبانی با تاریخچه‌ی کامل (قبلاً کاملاً بی‌حافظه بود: فقط
    # یک پیام فوروارد‌شده به ادمین با یک دکمه‌ی پاسخ، بدون هیچ رکورد پایدار،
    # بدون وضعیت باز/بسته، و بدون امکان دیدن تاریخچه‌ی مکالمه).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            subject       TEXT,
            status        TEXT NOT NULL DEFAULT 'open',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_status ON tickets(user_id, status)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id     INTEGER NOT NULL,
            sender        TEXT NOT NULL,
            text          TEXT,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket ON ticket_messages(ticket_id)")

    # 📣 لینک‌های تبلیغاتی (کمپین): هر تبلیغ یک ردیف با یک کد یکتا دارد که در
    # لینک /start c_<code> استفاده می‌شود. آمار واقعی (عضو/تبدیل/درآمد) هیچ‌وقت
    # اینجا cache نمی‌شود؛ همیشه زنده از روی users.campaign_id محاسبه می‌شود.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ad_campaigns (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            code                TEXT UNIQUE NOT NULL,
            name                TEXT NOT NULL,
            is_active           INTEGER NOT NULL DEFAULT 1,
            created_at          TEXT NOT NULL
        )
    """)

    # مهاجرت برای دیتابیس‌های قدیمی (اگر ستون‌ها از قبل موجود باشند، خطا نادیده گرفته می‌شود).
    for ddl in (
        "ALTER TABLE discounts ADD COLUMN discount_type TEXT NOT NULL DEFAULT 'percent'",
        "ALTER TABLE discounts ADD COLUMN amount INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE discounts ADD COLUMN applicable_plans TEXT",
        "ALTER TABLE discounts ADD COLUMN min_order_amount INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE discounts ADD COLUMN max_uses_per_user INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE discounts ADD COLUMN expires_at TEXT",
        "ALTER TABLE discounts ADD COLUMN allowed_user_ids TEXT",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logging.getLogger(__name__).warning("migration discounts.* خطای نامنخواسته: %s", exc)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS discount_usages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            discount_id   INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            used_at       TEXT NOT NULL,
            FOREIGN KEY (discount_id) REFERENCES discounts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # نمایندگی: آیدی عددی نماینده + درصد تخفیفی که روی محصولات VIP می‌گیرد.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id         TEXT UNIQUE NOT NULL,
            vip_discount_percent INTEGER NOT NULL DEFAULT 50,
            note                TEXT,
            created_at          TEXT NOT NULL,
            vip_category_ids    TEXT NOT NULL DEFAULT '',
            custom_prefix       TEXT NOT NULL DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id   INTEGER NOT NULL,
            invited_id    INTEGER NOT NULL UNIQUE,
            reward        INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            FOREIGN KEY (referrer_id) REFERENCES users(id),
            FOREIGN KEY (invited_id) REFERENCES users(id)
        )
    """)

    # پنل ادمین → «صف درخواست‌ها» → «رسیدهای در انتظار تایید». چون رسیدهای
    # شارژ کیف پول و خرید کارت‌به‌کارت پلن ثابت هیچ ردی در جدول‌های دیگر
    # ندارند (فقط به‌صورت پیام تلگرامی با دکمه برای ادمین فوروارد می‌شوند)،
    # این جدول یک ردِ سبک از هر رسید ارسالی نگه می‌دارد تا بشود همه را در یک
    # لیست دید. این جدول صرفاً یک «مدل نمایشی» است و منطق تایید/رد واقعی
    # (که در جدول‌های wallet/orders انجام می‌شود) به آن وابسته نیست.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_receipts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL,
            telegram_id   TEXT NOT NULL,
            user_id       INTEGER,
            label         TEXT NOT NULL,
            amount        INTEGER NOT NULL,
            extra         TEXT,
            plan_key      TEXT,
            discount_code TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL
        )
    """)

    # ⏱ فاکتورهای کارت‌به‌کارت در «انتظار پرداخت» (پلن ثابت / بساز سرویس خودت /
    # شارژ کیف پول). از لحظه‌ی نمایش شماره کارت + قیمت به کاربر ساخته می‌شود و
    # اگر ظرف INVOICE_EXPIRY_MINUTES دقیقه رسیدی برای آن ثبت نشود، توسط
    # invoice_expiry_loop (bot.py) / پس‌زمینه‌ی مشابه در Mini App به‌طور خودکار
    # منقضی و از دیتابیس حذف می‌شود (و به کاربر پیام داده می‌شود). پرداخت آنلاین
    # نیاز به این جدول ندارد چون خودش created_at دارد (به expire_due_online_payments
    # نگاه کنید).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            telegram_id   TEXT NOT NULL,
            kind          TEXT NOT NULL,
            label         TEXT NOT NULL,
            price         INTEGER NOT NULL,
            payload       TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL
        )
    """)

    # مهاجرت امن رسیدهای نسخه‌های قدیمی.
    for ddl in (
        "ALTER TABLE pending_receipts ADD COLUMN plan_key TEXT",
        "ALTER TABLE pending_receipts ADD COLUMN discount_code TEXT",
        # 🧾 ذخیره‌ی خودِ رسید (عکس یا متن/هش) تا ادمین از «صف درخواست‌ها» هم بتواند ببیند.
        "ALTER TABLE pending_receipts ADD COLUMN file_id TEXT",
        "ALTER TABLE pending_receipts ADD COLUMN file_type TEXT",
        "ALTER TABLE pending_receipts ADD COLUMN note TEXT",
        "ALTER TABLE pending_receipts ADD COLUMN details TEXT",
        # 🏷 نامِ انتخابیِ کاربر برای سرویس (حالت «نامگذاری توسط کاربر») تا بعد از تأیید رسید هم همان یوزرنیم روی پنل ساخته شود.
        "ALTER TABLE pending_receipts ADD COLUMN service_name TEXT",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            # فقط خطای «ستون از قبل وجود دارد» مجاز به نادیده‌گرفتن است.
            if "duplicate column" not in str(exc).lower():
                raise

    # Rate limit مشترک بین تمام workerهای Mini App.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_admin_actions (
            action_key  TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_rate_limits (
            bucket_key TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_rate_limits_key_time ON api_rate_limits(bucket_key, created_at)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_orders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL,
            volume_gb           INTEGER NOT NULL,
            days                INTEGER NOT NULL,
            custom_name         TEXT,
            price               INTEGER NOT NULL,
            order_type          TEXT NOT NULL DEFAULT 'new',
            target_config_id    INTEGER,
            renew_mode         TEXT,
            status              TEXT NOT NULL DEFAULT 'pending',
            created_at          TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    for ddl in (
        "ALTER TABLE custom_orders ADD COLUMN receipt_file_id TEXT",
        "ALTER TABLE custom_orders ADD COLUMN receipt_note TEXT",
        "ALTER TABLE custom_orders ADD COLUMN option_num INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    # ---------------------------------------------------------------------------
    # 🗂 دسته‌بندی‌های VIP و پلن‌های داخل هرکدام (بخش «۶» — قابل مدیریت کامل از
    # پنل ادمین: افزودن دسته‌ی جدید، افزودن/ویرایش/حذف پلن داخل هر دسته).
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)

    # 🆕 migration: افزودن ستون description به دیتابیس‌های قبلی که این ستون را ندارند
    # (تا قبل از این بروزرسانی این فیلد وجود نداشت).
    try:
        cur.execute("ALTER TABLE vip_categories ADD COLUMN description TEXT")
    except Exception:
        pass
    # FIX 11: VIP subfolder — parent_id column
    try:
        cur.execute("ALTER TABLE vip_categories ADD COLUMN parent_id INTEGER")
    except Exception:
        pass
    # entityهای توضیحات پوشه (ایموجی پریمیوم / ریچ‌تکست) به‌صورت JSON
    try:
        cur.execute("ALTER TABLE vip_categories ADD COLUMN description_entities TEXT")
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_plans (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_key     TEXT UNIQUE NOT NULL,
            category_id  INTEGER NOT NULL,
            name         TEXT NOT NULL,
            price        INTEGER NOT NULL,
            days         INTEGER NOT NULL DEFAULT 0,
            volume_gb    INTEGER NOT NULL DEFAULT 0,
            user_limit  INTEGER NOT NULL DEFAULT 0,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES vip_categories(id)
        )
    """)

    try:
        cur.execute("ALTER TABLE vip_plans ADD COLUMN user_limit INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass

    # فقط بار اول (وقتی دیتابیس کاملاً خالی از دسته‌بندی است) پلن‌های ثابت قدیمی
    # (VIP_PLANS در config.py) را به‌عنوان اولین دسته seed می‌کنیم تا چیزی از دست نرود.
    cur.execute("SELECT COUNT(*) AS c FROM vip_categories")
    if _fetchone(cur)["c"] == 0:
        now = _now()
        cur.execute(
            "INSERT INTO vip_categories (key, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
            ("speed_unlimited", "پرسرعت و کاربر نامحدود", 0, now),
        )
        default_cat_id = cur.lastrowid
        for i, (key, plan) in enumerate(VIP_PLANS.items()):
            cur.execute(
                """INSERT INTO vip_plans
                   (plan_key, category_id, name, price, days, volume_gb, user_limit, sort_order, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, default_cat_id, plan["name"], plan["price"], plan.get("days", 0),
                 plan.get("volume_gb", 0), plan.get("user_limit", 0), i, now),
            )

    # ---------------------------------------------------------------------------
    # 🗺️ نگاشت دسته‌بندی‌های VIP (و «بساز سرویس خودت») به یک planSlug
    # مشخص در پنل شاهراه. این‌که آن planSlug از کدام ترافیک (مثلاً «اقتصادی
    # تانل» یا «CDN اروان») تغذیه می‌شود، در خودِ پنل شاهراه هنگام تعریف بسته
    # مشخص می‌شود؛ اینجا فقط تعیین می‌کنیم که هر دسته‌بندی/محصول ما، سراغ کدام
    # بسته‌ی (planSlug) شاهراه برود.
    # scope: 'vip_category' | 'custom_build'
    # scope_id: شناسه‌ی دسته در جدول مربوطه (برای custom_build همیشه 0)
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shahrah_plan_map (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            scope_id    INTEGER NOT NULL,
            plan_slug   TEXT NOT NULL,
            plan_name   TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(scope, scope_id)
        )
    """)

    # ---------------------------------------------------------------------------
    # 🆕 چندپنلی (شاهراه + مرزبان + پاسارگارد، هرکدام با چند نمونه‌ی هم‌زمان)
    # ---------------------------------------------------------------------------
    # هر ردیف یک «نمونه‌ی پنل» مشخص است (مثلاً «مرزبان-آلمان» و «مرزبان-فرانسه»
    # هر دو panel_type='marzban' ولی دو ردیف/دو نمونه‌ی مستقل با اطلاعات ورود
    # جدا هستند). چند نمونه از هر نوع می‌توانند هم‌زمان enabled=1 باشند.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vpn_panels (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_type  TEXT NOT NULL,           -- 'shahrah' | 'marzban' | 'pasargad'
            name        TEXT NOT NULL,            -- برچسب دلخواه ادمین، مثلاً «مرزبان آلمان»
            base_url    TEXT NOT NULL,
            api_key     TEXT,                     -- فقط شاهراه
            username    TEXT,                     -- فقط مرزبان/پاسارگارد
            password    TEXT,                     -- فقط مرزبان/پاسارگارد
            enabled     INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)

    # نگاشتِ عمومیِ «کدام بسته/سرویس از کدام نمونه‌ی پنل استفاده کند» — جایگزین
    # نسل‌جدیدِ shahrah_plan_map که فقط شاهراه را پوشش می‌داد. scope/scope_id
    # دقیقاً همان معنایی را دارند که در shahrah_plan_map داشتند
    # ('vip_category' | 'vip_plan' | 'custom_build' | 'free_test').
    # remote_ref یعنی: برای شاهراه → planSlug، برای مرزبان/پاسارگارد → شناسه‌ی
    # عددی تمپلیت (template id) در همان نمونه‌ی پنل.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS panel_plan_map (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            scope_id    INTEGER NOT NULL,
            panel_id    INTEGER NOT NULL,
            remote_ref  TEXT NOT NULL,
            remote_name TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(scope, scope_id),
            FOREIGN KEY (panel_id) REFERENCES vpn_panels(id)
        )
    """)

    _migrate_legacy_shahrah_panel(cur)

    # ---------------------------------------------------------------------------
    # 📚 راهنما و آموزش‌ها — پنل ادمین می‌تواند هر تعداد آیتم راهنما (متن/عکس/
    # ویدیو/فایل) اضافه کند؛ همه‌ی این آیتم‌ها به‌صورت خودکار به‌عنوان دکمه‌ی
    # شیشه‌ای جدید در بخش «📚 راهنما» ربات (سمت کاربر) ظاهر می‌شوند.
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guides (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            content_type  TEXT NOT NULL DEFAULT 'text',
            body_text     TEXT,
            file_id       TEXT,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )
    """)
    # entityهای متن راهنما (ایموجی پریمیوم/بولد/...) — handlers/admin.py آن را می‌فرستد ولی
    # ستون و پارامترش وجود نداشت (create_guide/update_guide با TypeError می‌ترکید).
    try:
        cur.execute("ALTER TABLE guides ADD COLUMN body_entities TEXT")
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            logging.getLogger(__name__).warning("migration guides.body_entities: %s", exc)

    # ---------------------------------------------------------------------------
    # 🎬 استیکر/ویدیوی تستی هر بخش از منو (🎁 تست رایگان، 🛒 خرید اشتراک،
    # 🚀 انتخاب پلن VIP، 🛠 بساز کانفیگ خودت). هر بخش یک ردیف دارد (کلید
    # section_key)؛ اگر ردیفی وجود نداشته باشد یعنی ادمین هنوز آن را سفارشی نکرده و
    # استیکر پیش‌فرض داخل پروژه نمایش داده می‌شود.
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS section_stickers (
            section_key  TEXT PRIMARY KEY,
            file_id      TEXT,
            is_enabled   INTEGER NOT NULL DEFAULT 1,
            updated_at   TEXT NOT NULL
        )
    """)

    # ---------------------------------------------------------------------------
    # 🦖 لاگ خطاها (همان رویدادهایی که به Sentry هم ارسال می‌شود) — برای اینکه
    # ادمین بدون نیاز به تنظیمات/دسترسی Sentry هم بتونه از داخل ایخود پنل ادمین
    # ربات آخرین خطاها رو ببیند.
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type    TEXT NOT NULL,
            message       TEXT,
            traceback     TEXT,
            context       TEXT,
            occurred_at   TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_error_logs_occurred ON error_logs(occurred_at)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_guides_sort ON guides(sort_order)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_invite_code ON users(invite_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_configs_user ON configs(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_custom_orders_user ON custom_orders(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    # idempotency_key is added below for legacy DBs; the unique index must be
    # created only after the migration so old databases do not fail at startup.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_online_payments_hash ON online_payments(hash_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_online_payments_status ON online_payments(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_receipts_status ON pending_receipts(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vip_plans_category ON vip_plans(category_id)")
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor_type TEXT NOT NULL, actor_id TEXT, actor_name TEXT,
        action TEXT NOT NULL, target_type TEXT, target_id TEXT, details TEXT, created_at TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS panel_health (
        panel_id INTEGER PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, circuit_open INTEGER NOT NULL DEFAULT 0,
        opened_at REAL, last_ok_at REAL, last_fail_at REAL, last_latency_ms INTEGER, last_error TEXT, probe_claimed INTEGER NOT NULL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS purchase_action_locks (
        action_key TEXT PRIMARY KEY, created_at REAL NOT NULL
    )""")
    for ddl in (
        "ALTER TABLE configs ADD COLUMN panel_used_bytes INTEGER",
        "ALTER TABLE configs ADD COLUMN panel_total_bytes INTEGER",
        "ALTER TABLE configs ADD COLUMN panel_status TEXT",
        "ALTER TABLE configs ADD COLUMN last_panel_sync_at TEXT",
        "ALTER TABLE orders ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'",
        "ALTER TABLE orders ADD COLUMN idempotency_key TEXT",
        # 💳 روش پرداخت سفارش (برای گزارش ادمین / لاگ کانال / جزئیات صف سفارش‌ها)
        "ALTER TABLE orders ADD COLUMN payment_method TEXT",
        # 🏷 یوزرنیمی که کاربر برای سرویس انتخاب کرده؛ ساخت روی پنل دقیقاً با همین نام انجام می‌شود (خالی = نام پیش‌فرض ربات).
        "ALTER TABLE orders ADD COLUMN service_name TEXT",
        "ALTER TABLE custom_orders ADD COLUMN payment_method TEXT",
        # 🌐 درگاه‌های آنلاین چندگانه: uniquepay (پیش‌فرض/قدیمی) | frenzyex | tronado.
        # gw_data یک JSON برای وضعیت اختصاصی درگاه است (توکن، مبلغ ترون، تأییدیه‌ی کال‌بک و...).
        "ALTER TABLE online_payments ADD COLUMN gateway TEXT NOT NULL DEFAULT 'uniquepay'",
        "ALTER TABLE online_payments ADD COLUMN gw_data TEXT",
        # 🆕 روش اتصال به پنل قابل‌انتخاب: برای پنل‌هایی که هم از یوزر/پسورد و
        # هم از یک توکن API ثابت پشتیبانی می‌کنند (مرزبان/پاسارگارد)، ادمین
        # از پنل ادمین ربات انتخاب می‌کند کدام روش استفاده شود؛ پیش‌فرض
        # 'userpass' است تا پنل‌های قبلاً ساخته‌شده دقیقاً مثل قبل کار کنند.
        "ALTER TABLE vpn_panels ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'userpass'",
        # 🆕 تخفیف اولین خرید: این کد فقط برای کاربری که هیچ سفارش تکمیل‌شده‌ی
        # قبلی در فروشگاه ندارد قابل استفاده است (مشوق مشتری تازه).
        "ALTER TABLE discounts ADD COLUMN first_purchase_only INTEGER NOT NULL DEFAULT 0",
        # 🆕 لینک‌های تبلیغاتی (کمپین) — هر کاربر فقط به اولین کمپینی که او را
        # آورده بسته می‌شود (first-touch)، دقیقاً مثل referrer_id بالا. عمداً
        # FOREIGN KEY نگذاشتیم چون این فقط برای گزارش‌گیری است، نه یکپارچگی
        # مالی؛ حذف یک کمپین نباید بتواند حذف/rollback کاربران را قفل کند.
        "ALTER TABLE users ADD COLUMN campaign_id INTEGER",
        # 🆕 پیام همگانی پیشرفته: کاربری که تلگرام برایش Forbidden (بلاک‌کردن
        # ربات) برگردانده، اینجا علامت می‌خورد تا در دورهای بعدی پیام همگانی
        # اصلاً برایش تلاش نشود (سرعت پخش بالاتر می‌رود و آمار failed واقعی‌تر
        # می‌شود). اگر کاربر بعداً دوباره /start بزند، این پرچم پاک می‌شود.
        "ALTER TABLE users ADD COLUMN bot_blocked_at TEXT",
        # 🆕 جریمه‌ی رفرال: مشخص می‌کند این سرویس با پولی خریداری شده که
        # (حداقل بخشی از) کیف‌پول کاربر از رفرال به‌دست آمده بود؛ فقط چنین
        # سرویس‌هایی مشمول کسر حجم جریمه می‌شوند، نه هر سرویس دلخواه کاربر.
        "ALTER TABLE configs ADD COLUMN referral_funded INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN payg_price_per_gb INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN payg_last_used_bytes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN payg_paused INTEGER NOT NULL DEFAULT 0",
        # ⚖️ مصرف منصفانه (FUP) برای سرویس‌های نامحدود
        "ALTER TABLE configs ADD COLUMN fup_started INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN fup_blocked INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN fup_base_bytes INTEGER",
        "ALTER TABLE configs ADD COLUMN fup_day TEXT",
        # 🆕 دسته‌بندی مخاطب کد تخفیف (مشابه دسته‌بندی پیام همگانی):
        # all=همه | buyers=فقط خریداران قبلی | non_buyers=فقط بدون خرید قبلی
        # specific=فقط آیدی‌های مشخص‌شده در allowed_user_ids (رفتار قبلی).
        "ALTER TABLE discounts ADD COLUMN audience TEXT NOT NULL DEFAULT 'all'",
        "ALTER TABLE agents ADD COLUMN vip_category_ids TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE agents ADD COLUMN custom_prefix TEXT NOT NULL DEFAULT ''",
    ):
        try: cur.execute(ddl)
        except Exception as exc:
            if "duplicate column" not in str(exc).lower(): raise

    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL")

    # 🐛 مهاجرت خودکار / self-heal: نسخه‌های قدیمی‌تر این پروژه، پلن‌های
    # پیش‌فرض VIP_PLANS را بدون کلید volume_gb در config.py seed
    # می‌کردند و در نتیجه مقدار volume_gb این پلن‌ها در دیتابیس صفر ذخیره شده
    # بود (باعث می‌شد پاداش دعوت هیچ‌وقت برای خرید این پلن‌ها آزاد نشود).
    # اینجا فقط همان plan_keyهای شناخته‌شده‌ای که هنوز volume_gb=0 دارند را با
    # مقدار درست از config.py هماهنگ می‌کنیم؛ پلن‌هایی که ادمین بعداً دستی
    # ساخته/ویرایش کرده دست‌نخورده می‌مانند.
    for _key, _plan in VIP_PLANS.items():
        if _plan.get("volume_gb"):
            cur.execute(
                "UPDATE vip_plans SET volume_gb = ? WHERE plan_key = ? AND volume_gb = 0",
                (_plan["volume_gb"], _key),
            )

    # یک‌بار پاک‌سازی داده‌های محصول حذف‌شده؛ بعد از این migration دیتابیس فقط VIP است.
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gaming_files'")
        has_legacy = cur.fetchone() is not None
        if has_legacy:
            cur.execute("DELETE FROM gaming_files")
        cur.execute("DELETE FROM configs WHERE type = 'gaming'")
        cur.execute("DELETE FROM orders WHERE order_type = 'gaming'")
        cur.execute("DELETE FROM online_payments WHERE order_type = 'gaming'")
        cur.execute("DROP TABLE IF EXISTS gaming_files")
        cur.execute("DROP TABLE IF EXISTS gaming_plans")
        cur.execute("DROP TABLE IF EXISTS gaming_categories")
    except Exception:
        logging.getLogger(__name__).exception("legacy product cleanup failed")

    # Migration: renewal mode for existing custom orders.
    try:
        cur.execute("ALTER TABLE custom_orders ADD COLUMN renew_mode TEXT")
    except Exception:
        pass

    # Mini app: daily usage snapshots (usage charts) + per-user notification prefs.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS service_usage_snapshots (
            config_id INTEGER NOT NULL,
            day       TEXT NOT NULL,
            used      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (config_id, day)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id       INTEGER PRIMARY KEY,
            usage_alerts  INTEGER NOT NULL DEFAULT 1,
            expiry_alerts INTEGER NOT NULL DEFAULT 1,
            wallet_alerts INTEGER NOT NULL DEFAULT 1,
            promo         INTEGER NOT NULL DEFAULT 1,
            default_app   TEXT
        )
    """)

    conn.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _generate_invite_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "BVPN" + "".join(secrets.choice(alphabet) for _ in range(5))
        cur = get_connection().cursor()
        cur.execute("SELECT 1 FROM users WHERE invite_code = ?", (code,))
        if _fetchone(cur) is None:
            return code


def _invalidate_user_caches(user) -> None:
    """Invalidate all in-process caches for one user after a mutation."""
    if not user:
        return
    telegram_id = str(user.get("telegram_id", ""))
    user_id = user.get("id")
    invite_code = user.get("invite_code")
    if telegram_id:
        cache.invalidate(f"user:{telegram_id}")
    if user_id is not None:
        cache.invalidate(f"user_id:{user_id}")
    if invite_code:
        cache.invalidate(f"user_invite:{str(invite_code).upper()}")


def get_user(telegram_id) -> dict | None:
    # perf: در پردازش یک آپدیت تلگرام (یک کلیک/پیام)، معمولاً get_user چند بار
    # پشت‌سرهم صدا زده می‌شه (میدل‌ور بلاک‌شدن، چک موجودی، نمایش پروفایل و...).
    # چون کیف‌پول/بلاک‌بودن جای حساسیه، برای مدت کوتاه (۶۰ ثانیه) کش
    # می‌کنیم؛ تغییرات مهم با invalidate کردن همین کش فوراً اعمال می‌شوند،
    # بنابراین کلیک‌های پشت‌سرهم به Turso نمی‌روند و مقدار
    # تازه خونده می‌شه — بدون نیاز به ردیابی و invalidate کردن ده‌ها نقطه‌ی
    # نوشتن روی جدول users.
    # نکته: نتیجه‌ی «کاربر پیدا نشد» (None) عمداً کش نمی‌شود، چون create_user
    # همیشه اول با همین تابع چک می‌کند که کاربر جدید است یا نه؛ اگر آن None
    # چند ثانیه کش می‌ماند، بلافاصله بعد از ساخت کاربر جدید، همان کاربر
    # «پیدا نشد» برمی‌گشت (باگ واقعی که در تست همین امروز رخ داد).
    cache_key = f"user:{telegram_id}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (str(telegram_id),))
    value = _fetchone(cur)
    if value is not None:
        cache.set(cache_key, value, ttl=60)
    return value


def get_user_by_invite_code(code: str) -> dict | None:
    code = code.upper()
    cache_key = f"user_invite:{code}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM users WHERE invite_code = ?", (code,))
    value = _fetchone(cur)
    if value is not None:
        cache.set(cache_key, value, ttl=30)
    return value


def get_user_by_id(user_id: int) -> dict | None:
    cache_key = f"user_id:{user_id}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    value = _fetchone(cur)
    if value is not None:
        cache.set(cache_key, value, ttl=30)
    return value



# ---------------------------------------------------------------------------
# جستجوی حرفه‌ای — بند ۲
# ---------------------------------------------------------------------------
def search_users_by_name(query: str, limit: int = 20) -> list[dict]:
    """جستجوی جزئی (LIKE) روی ستون name جدول users."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE name LIKE ? ORDER BY id DESC LIMIT ?",
        (f"%{query}%", limit),
    )
    return _fetchall(cur)


def search_users_by_telegram_id_fragment(fragment: str, limit: int = 20) -> list[dict]:
    """جستجوی جزئی روی ستون telegram_id (برای شناسه‌های عددی ناقص)."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE telegram_id LIKE ? ORDER BY id DESC LIMIT ?",
        (f"%{fragment}%", limit),
    )
    return _fetchall(cur)


def search_users_multi(query: str, limit: int = 15) -> list[dict]:
    """جستجو همزمان روی name، telegram_id و invite_code."""
    cur = get_connection().cursor()
    q = f"%{query}%"
    cur.execute(
        """
        SELECT * FROM users
        WHERE name LIKE ? OR telegram_id LIKE ? OR invite_code LIKE ?
        ORDER BY id DESC LIMIT ?
        """,
        (q, q, q.upper(), limit),
    )
    return _fetchall(cur)

def create_user(telegram_id, name: str, referrer_invite_code: str | None = None,
                 campaign_code: str | None = None) -> dict:
    telegram_id = str(telegram_id)
    existing = get_user(telegram_id)
    if existing:
        # 🆕 اگر این کاربر قبلاً به‌خاطر Forbidden از پیام همگانی حذف شده بود،
        # حالا که دوباره با ربات تعامل کرده (یعنی ربات را آنبلاک کرده)، این
        # پرچم پاک می‌شود تا در پیام همگانی‌های بعدی دوباره در نظر گرفته شود.
        # توجه: به‌جای خواندن دوباره از get_user (که به‌خاطر کش ۲ ثانیه‌ای
        # ممکن است هنوز مقدار قدیمی را برگرداند)، دیکشنری محلی مستقیماً
        # به‌روزرسانی می‌شود تا مقدار برگشتی همیشه درست باشد.
        if existing.get("bot_blocked_at"):
            clear_bot_blocked(telegram_id)
            existing = dict(existing)
            existing["bot_blocked_at"] = None
        return existing

    referrer = None
    if referrer_invite_code and referral_enabled():
        referrer = get_user_by_invite_code(referrer_invite_code)
        if referrer and referrer["telegram_id"] == telegram_id:
            referrer = None

    # 🆕 لینک تبلیغاتی: فقط اگر کد کمپینی معتبر و فعال باشد ثبت می‌شود، و چون
    # این تابع فقط وقتی وارد این if می‌شود که کاربر تازه ساخته می‌شود (بالا
    # چک شد existing نیست)، همین یعنی خودبه‌خود «فقط اولین کمپین» ثبت می‌شود.
    campaign = None
    if campaign_code:
        campaign = get_campaign_by_code(campaign_code)
        if campaign and not campaign.get("is_active", 1):
            campaign = None

    invite_code = _generate_invite_code()

    try:
        _create_user_txn(telegram_id, name, referrer, campaign, invite_code)
    except Exception as exc:
        # درخواست هم‌زمان دیگری (مثلاً دو API مینی‌اپ برای کاربر جدید) همین کاربر را ساخته است.
        if "unique" in str(exc).lower() and "telegram_id" in str(exc).lower():
            cache.invalidate(f"user:{telegram_id}")
            existing = get_user(telegram_id)
            if existing:
                return existing
        raise
    if referrer:
        cache.invalidate(f"user:{referrer['telegram_id']}")
    cache.invalidate(f"user:{telegram_id}")
    _nu = get_user(telegram_id)
    if referrer and _nu:
        _adv_hook("referral_event", referrer["id"], _nu["id"], "joined")
    return _nu


def _create_user_txn(telegram_id, name, referrer, campaign, invite_code):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO users (telegram_id, name, wallet, locked_wallet,
                                   total_purchase, joined, referrer_id, invite_code,
                                   invited_count, successful_invites, referral_wallet, campaign_id)
               VALUES (?, ?, 0, 0, 0, ?, ?, ?, 0, 0, 0, ?)""",
            (telegram_id, name, _now(), referrer["id"] if referrer else None, invite_code,
             campaign["id"] if campaign else None),
        )
        new_user_id = cur.lastrowid

        if referrer:
            settings = get_referral_settings()
            reward = settings["reward_amount"]
            immediate = not settings["paid_purchase_required"]
            status = "completed" if immediate else "pending"
            cur.execute(
                """INSERT INTO referrals (referrer_id, invited_id, reward, status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (referrer["id"], new_user_id, reward, status, _now()),
            )
            cur.execute("UPDATE users SET invited_count = invited_count + 1 WHERE id = ?", (referrer["id"],))
            if immediate:
                cur.execute("UPDATE users SET wallet = wallet + ?, referral_wallet = referral_wallet + ?, successful_invites = successful_invites + 1 WHERE id = ?", (reward, reward, referrer["id"]))
                cur.execute("INSERT INTO transactions (user_id, type, amount, status, description, created_at) VALUES (?, 'referral_release', ?, 'completed', ?, ?)", (referrer["id"], reward, "آزادسازی فوری پاداش دعوت (بدون شرط خرید)", _now()))
            else:
                cur.execute("UPDATE users SET locked_wallet = locked_wallet + ? WHERE id = ?", (reward, referrer["id"]))
                cur.execute("INSERT INTO transactions (user_id, type, amount, status, description, created_at) VALUES (?, 'referral_locked', ?, 'pending', ?, ?)", (referrer["id"], reward, "پاداش دعوت (در انتظار خرید واجد شرط)", _now()))


def update_user_name(telegram_id, name: str):
    with transaction() as cur:
        cur.execute("UPDATE users SET name = ? WHERE telegram_id = ?", (name, str(telegram_id)))
    cache.invalidate(f"user:{telegram_id}")


def get_all_users(limit: int | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if limit:
        cur.execute("SELECT * FROM users ORDER BY id DESC LIMIT ?", (limit,))
    else:
        cur.execute("SELECT * FROM users ORDER BY id DESC")
    return _fetchall(cur)


def count_users() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    return _fetchone(cur)["c"]


def count_active_users(days: int = 30) -> int:
    cur = get_connection().cursor()
    cur.execute(
        """SELECT COUNT(DISTINCT user_id) AS c FROM transactions
           WHERE created_at >= datetime('now', ?)""",
        (f"-{days} days",),
    )
    return _fetchone(cur)["c"]


def count_customers() -> int:
    """تعداد کاربرانی که حداقل یک خرید موفق داشته‌اند (مشتریان واقعی)."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE total_purchase > 0")
    return _fetchone(cur)["c"]


def get_customers(limit: int = 30) -> list[dict]:
    """کاربرانی که حداقل یک خرید موفق داشته‌اند، مرتب‌شده بر اساس بیشترین خرید."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE total_purchase > 0 ORDER BY total_purchase DESC LIMIT ?",
        (limit,),
    )
    return _fetchall(cur)


def get_customers_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ی مشخصی از مشتریانی که خرید داشته‌اند، مرتب‌شده بر اساس بیشترین خرید."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE total_purchase > 0 ORDER BY total_purchase DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


def get_non_customers_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """🆕 بند ۱۹ — صفحه‌ای از کاربرانی که *هیچ خریدی نداشته‌اند*.

    🐛 فیکس: قبلاً لیست دوم پنل («کاربران بدون خرید») با get_all_users_page
    پر می‌شد که همه‌ی کاربران — از جمله مشتریان فعال — را برمی‌گرداند، در
    حالی که شمارشش count_non_customers بود. نتیجه: مشتریان فعال در هر دو
    لیست تکرار می‌شدند و صفحه‌بندی هم غلط بود (تعداد صفحه‌ها بر اساس یک عدد
    و محتوا بر اساس یک کوئری دیگر). حالا هر دو از یک شرط واحد می‌آیند.
    """
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE COALESCE(total_purchase, 0) <= 0 ORDER BY id DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


USER_LIST_SORTS = ("tx", "svc", "last", "new", "wal", "nobuy")

_USER_EVENTS_CTE = """
WITH ev AS (
  SELECT user_id, created_at AS t,
         CASE type WHEN 'charge' THEN 'شارژ کیف پول' WHEN 'purchase' THEN 'خرید'
                   WHEN 'referral_release' THEN 'پاداش دعوت' WHEN 'referral_locked' THEN 'پاداش دعوت'
                   WHEN 'referral_pending' THEN 'پاداش دعوت' ELSE type END AS lbl
    FROM transactions WHERE COALESCE(status, 'completed') = 'completed'
  UNION ALL
  SELECT user_id, created_at,
         CASE order_type WHEN 'renew' THEN 'تمدید' WHEN 'test' THEN 'تست رایگان' WHEN 'free_test' THEN 'تست رایگان' ELSE 'خرید' END
    FROM orders WHERE status = 'fulfilled'
  UNION ALL
  SELECT user_id, created_at, CASE order_type WHEN 'renew' THEN 'تمدید' ELSE 'سرویس سفارشی' END
    FROM custom_orders WHERE status = 'fulfilled'
)
"""


def count_users_sorted(sort: str) -> int:
    """تعداد کاربران هر لیست مرتب‌شده‌ی پنل ادمین (sort ∈ USER_LIST_SORTS)."""
    cur = get_connection().cursor()
    if sort == "tx":
        cur.execute("SELECT COUNT(*) FROM users WHERE COALESCE(total_purchase, 0) > 0")
    elif sort == "svc":
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM configs WHERE deleted = 0")
    elif sort == "last":
        cur.execute(_USER_EVENTS_CTE + "SELECT COUNT(DISTINCT user_id) FROM ev")
    elif sort == "wal":
        cur.execute("SELECT COUNT(*) FROM users WHERE COALESCE(wallet, 0) > 0")
    elif sort == "nobuy":
        cur.execute("SELECT COUNT(*) FROM users WHERE COALESCE(total_purchase, 0) <= 0")
    else:
        cur.execute("SELECT COUNT(*) FROM users")
    return int(cur.fetchone()[0] or 0)


def get_users_sorted_page(sort: str, page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ای از کاربران با سورت انتخابی؛ هر ردیف کلید «metric» (مقدار مربوط به سورت) دارد."""
    cur = get_connection().cursor()
    off = (per_page, max(0, page) * per_page)
    if sort == "tx":
        cur.execute("SELECT *, total_purchase AS metric FROM users WHERE COALESCE(total_purchase, 0) > 0 "
                    "ORDER BY total_purchase DESC, id ASC LIMIT ? OFFSET ?", off)
    elif sort == "svc":
        cur.execute("SELECT u.*, c.n AS metric FROM users u JOIN "
                    "(SELECT user_id, COUNT(*) AS n FROM configs WHERE deleted = 0 GROUP BY user_id) c ON c.user_id = u.id "
                    "ORDER BY c.n DESC, u.id ASC LIMIT ? OFFSET ?", off)
    elif sort == "last":
        cur.execute(_USER_EVENTS_CTE +
                    "SELECT u.*, (SELECT e2.lbl FROM ev e2 WHERE e2.user_id = u.id ORDER BY e2.t DESC LIMIT 1) AS metric "
                    "FROM users u JOIN (SELECT user_id, MAX(t) AS t FROM ev GROUP BY user_id) l ON l.user_id = u.id "
                    "ORDER BY l.t DESC, u.id DESC LIMIT ? OFFSET ?", off)
    elif sort == "wal":
        cur.execute("SELECT *, wallet AS metric FROM users WHERE COALESCE(wallet, 0) > 0 "
                    "ORDER BY wallet DESC, id ASC LIMIT ? OFFSET ?", off)
    elif sort == "nobuy":
        cur.execute("SELECT *, telegram_id AS metric FROM users WHERE COALESCE(total_purchase, 0) <= 0 "
                    "ORDER BY id ASC LIMIT ? OFFSET ?", off)
    else:
        cur.execute("SELECT *, telegram_id AS metric FROM users ORDER BY id DESC LIMIT ? OFFSET ?", off)
    return _fetchall(cur)


def get_all_users_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ی مشخصی از همه‌ی کاربران، مرتب‌شده بر اساس بیشترین خرید."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users ORDER BY total_purchase DESC, id DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


def get_transactions_page(user_id: int, page: int = 0, per_page: int = 10) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, per_page, page * per_page),
    )
    return _fetchall(cur)


def _invalidate_user_cache_by_id(user_id: int):
    """کش get_user را برای این کاربر پاک می‌کند؛ روی مسیرهای مالی (شارژ/کسر
    کیف‌پول) صدا زده می‌شود تا کاربر بلافاصله بعد از تراکنش موجودی درست را
    ببیند، حتی با وجود کش کوتاه‌مدت get_user."""
    cur = get_connection().cursor()
    cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        cache.invalidate(f"user:{row[0]}")


def add_to_wallet(user_id: int, amount: int, description: str, tx_type: str = "charge"):
    with transaction() as cur:
        cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (amount, user_id))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, ?, ?, 'completed', ?, ?)""",
            (user_id, tx_type, amount, description, _now()),
        )
    _invalidate_user_cache_by_id(user_id)


def set_user_wallet(user_id: int, new_balance: int, description: str = "ویرایش دستی موجودی توسط ادمین"):
    """🆕 بند ۶ — موجودی کیف‌پول را مستقیماً روی یک مقدار مشخص تنظیم می‌کند
    (نه اضافه‌کردن). تراکنش متناظر برای ردگیری حسابداری ثبت می‌شود، ولی هیچ
    اطلاع‌رسانی‌ای به کاربر ارسال نمی‌شود — این کار عمداً به عهده‌ی هندلر است
    و هندلر بند ۶ هیچ پیامی نمی‌فرستد."""
    if new_balance < 0:
        raise ValueError("موجودی نمی‌تواند منفی باشد")
    with transaction() as cur:
        cur.execute("SELECT wallet FROM users WHERE id = ?", (user_id,))
        row = _fetchone(cur)
        old = int(row["wallet"]) if row else 0
        diff = new_balance - old
        cur.execute("UPDATE users SET wallet = ? WHERE id = ?", (new_balance, user_id))
        if diff != 0:
            cur.execute(
                "INSERT INTO transactions (user_id, type, amount, status, description, created_at) "
                "VALUES (?, 'admin_adjust', ?, 'completed', ?, ?)",
                (user_id, abs(diff), description, _now()),
            )
    _invalidate_user_cache_by_id(user_id)


def add_to_locked_wallet(user_id: int, amount: int, description: str):
    with transaction() as cur:
        cur.execute("UPDATE users SET locked_wallet = locked_wallet + ? WHERE id = ?", (amount, user_id))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'referral_pending', ?, 'pending', ?, ?)""",
            (user_id, amount, description, _now()),
        )
    _invalidate_user_cache_by_id(user_id)


def release_locked_wallet(user_id: int, amount: int, description: str = "آزادسازی پاداش دعوت"):
    """🐛 همان فیکس ریس‌کاندیشن deduct_from_wallet، اینجا برای locked_wallet."""
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET locked_wallet = locked_wallet - ?, wallet = wallet + ? "
            "WHERE id = ? AND locked_wallet >= ?",
            (amount, amount, user_id, amount),
        )
        if (cur.rowcount or 0) == 0:
            raise ValueError("موجودی در انتظار کافی نیست.")
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'referral_release', ?, 'completed', ?, ?)""",
            (user_id, amount, description, _now()),
        )
    _invalidate_user_cache_by_id(user_id)


def deduct_from_wallet(user_id: int, amount: int, description: str) -> bool:
    """🐛 فیکس ریس‌کاندیشن: نسخه‌ی قبلی ابتدا موجودی را با SELECT می‌خواند،
    در پایتون چک می‌کرد، و بعد UPDATE می‌زد. قفل داخلی این ماژول (_lock) فقط
    داخل یک پروسه اثر دارد؛ چون ربات (bot.py) و Mini App API (webapp_api.py)
    دو پروسه‌ی جدا هستند و هر دو روی همان دیتابیس کیف‌پول کم می‌کنند، دو
    خرید هم‌زمان (یکی از ربات، یکی از مینی‌اپ) می‌توانستند هر دو موجودی کافی
    را ببینند و هر دو کسر انجام شود (برداشت بیش از موجودی/overdraft).
    حالا چک و کسر در یک UPDATE شرطی اتمیک انجام می‌شود؛ خود SQLite تضمین
    می‌کند این عملیات به‌صورت غیرقابل‌تقسیم اجرا شود، حتی از پروسه‌های جدا."""
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET wallet = wallet - ?, total_purchase = total_purchase + ? "
            "WHERE id = ? AND wallet >= ?",
            (amount, amount, user_id, amount),
        )
        if (cur.rowcount or 0) == 0:
            return False
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user_id, amount, description, _now()),
        )
    _invalidate_user_cache_by_id(user_id)
    audit_event("user", "wallet.debit", "user", str(user_id), f"amount={amount};description={description}")
    return True


def deduct_wallet_for_purchase(user_id: int, amount: int, description: str) -> tuple[bool, bool]:
    """کسر اتمیک خرید سرویس از کیف پول + ردیابی منشأ رفرالی.

    اگر کل مبلغ خرید از referral_wallet تأمین شود، سرویس ساخته‌شده می‌تواند
    به‌عنوان سرویس رفرالی علامت بخورد. اگر حتی بخشی از مبلغ از موجودی عادی
    تأمین شود، سرویس referral-funded محسوب نمی‌شود تا جریمه‌ی رفرال هیچ‌وقت
    روی یک بسته‌ی خریداری‌شده با پول عادی اعمال نشود.
    """
    amount = int(amount or 0)
    if amount < 0:
        return False, False
    with transaction() as cur:
        cur.execute("SELECT wallet, referral_wallet FROM users WHERE id = ?", (user_id,))
        row = _fetchone(cur)
        if row is None:
            return False, False
        wallet = int(row["wallet"] or 0)
        referral_balance = min(int(row["referral_wallet"] or 0), wallet)
        if wallet < amount:
            return False, False
        referral_funded = amount > 0 and referral_balance >= amount
        referral_used = amount if referral_funded else min(amount, referral_balance)
        cur.execute(
            "UPDATE users SET wallet = wallet - ?, referral_wallet = referral_wallet - ?, total_purchase = total_purchase + ? WHERE id = ? AND wallet >= ? AND referral_wallet >= ?",
            (amount, referral_used, amount, user_id, amount, referral_used),
        )
        if (cur.rowcount or 0) == 0:
            return False, False
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user_id, amount, description + (" [referral-funded]" if referral_funded else ""), _now()),
        )
    _invalidate_user_cache_by_id(user_id)
    audit_event("user", "wallet.debit", "user", str(user_id), f"amount={amount};description={description};referral_funded={referral_funded}")
    return True, referral_funded


def record_purchase(user_id: int, amount: int, description: str):
    """ثبت یک خرید موفق بدون کسر از کیف پول (برای پرداخت کارت‌به‌کارت)."""
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET total_purchase = total_purchase + ? WHERE id = ?",
            (amount, user_id),
        )
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user_id, amount, description, _now()),
        )
    _invalidate_user_cache_by_id(user_id)


def get_transactions(user_id: int, limit: int = 10) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return _fetchall(cur)


_SOLD_STATUSES = "('paid', 'processing', 'fulfilled')"


def get_dashboard_stats(start_utc: str | None, end_utc: str | None) -> dict:
    """آمار داشبورد ادمین، متناسب با همین ربات (فروش VIP، بساز سرویس خودت،
    تمدید، تست رایگان، روش‌های پرداخت، کیف پول، رسیدها و سرویس‌ها).
    start_utc/end_utc هر دو None یعنی «آمار کل».

    سفارش‌هایی که تحویل شده‌اند (status='fulfilled') هم فروش حساب می‌شوند؛
    فقط pending/rejected/dismissed کنار گذاشته می‌شوند."""
    cur = get_connection().cursor()
    ranged = bool(start_utc)
    where = "AND created_at >= ? AND created_at <= ?" if ranged else ""
    p_dt = (start_utc, end_utc) if ranged else ()

    def scalar(sql, params=()):
        try:
            cur.execute(sql, params)
            row = _fetchone(cur)
            return list(row.values())[0] if row else 0
        except Exception:
            return 0

    def rows(sql, params=()):
        try:
            cur.execute(sql, params)
            return _fetchall(cur)
        except Exception:
            return []

    sold = _SOLD_STATUSES
    # ----- کاربران
    total_users = scalar("SELECT COUNT(*) AS c FROM users")
    new_users = scalar(f"SELECT COUNT(*) AS c FROM users WHERE 1=1 " + ("AND joined >= ? AND joined <= ?" if ranged else ""), p_dt)
    blocked_users = scalar("SELECT COUNT(*) AS c FROM users WHERE is_blocked = 1")
    total_wallet = scalar("SELECT COALESCE(SUM(wallet), 0) AS c FROM users")
    total_locked = scalar("SELECT COALESCE(SUM(locked_wallet), 0) AS c FROM users")

    # ----- فروش سرویس آماده (پلن VIP) — خرید جدید
    plan_new_count = scalar(
        f"SELECT COUNT(*) AS c FROM orders WHERE status IN {sold} AND order_type != 'test' "
        f"AND order_type != 'renew' AND target_config_id IS NULL {where}", p_dt)
    plan_new_amount = scalar(
        f"SELECT COALESCE(SUM(price), 0) AS c FROM orders WHERE status IN {sold} AND order_type != 'test' "
        f"AND order_type != 'renew' AND target_config_id IS NULL {where}", p_dt)
    # ----- سرویس سفارشی (بساز سرویس خودت) — جدید
    custom_new_count = scalar(
        f"SELECT COUNT(*) AS c FROM custom_orders WHERE status IN {sold} AND order_type != 'renew' {where}", p_dt)
    custom_new_amount = scalar(
        f"SELECT COALESCE(SUM(price), 0) AS c FROM custom_orders WHERE status IN {sold} AND order_type != 'renew' {where}", p_dt)
    # ----- تمدید
    renew_count = scalar(
        f"SELECT COUNT(*) AS c FROM orders WHERE status IN {sold} AND (order_type = 'renew' OR target_config_id IS NOT NULL) {where}", p_dt)
    renew_amount = scalar(
        f"SELECT COALESCE(SUM(price), 0) AS c FROM orders WHERE status IN {sold} AND (order_type = 'renew' OR target_config_id IS NOT NULL) {where}", p_dt)
    renew_count += scalar(
        f"SELECT COUNT(*) AS c FROM custom_orders WHERE status IN {sold} AND order_type = 'renew' {where}", p_dt)
    renew_amount += scalar(
        f"SELECT COALESCE(SUM(price), 0) AS c FROM custom_orders WHERE status IN {sold} AND order_type = 'renew' {where}", p_dt)
    # ----- تست رایگان
    test_count = scalar(
        f"SELECT COUNT(*) AS c FROM orders WHERE order_type = 'test' AND status IN {sold} {where}", p_dt)

    sales_count = plan_new_count + custom_new_count
    sales_amount = plan_new_amount + custom_new_amount
    total_revenue = sales_amount + renew_amount

    buyers = scalar(
        f"SELECT COUNT(*) AS c FROM (SELECT user_id FROM orders WHERE status IN {sold} AND order_type != 'test' AND price > 0 {where} "
        f"UNION SELECT user_id FROM custom_orders WHERE status IN {sold} AND price > 0 {where})", p_dt + p_dt)
    total_buyers_ever = scalar(
        f"SELECT COUNT(*) AS c FROM (SELECT user_id FROM orders WHERE status IN {sold} AND order_type NOT IN ('test','wallet_charge') AND price > 0 "
        f"UNION SELECT user_id FROM custom_orders WHERE status IN {sold} AND price > 0)")

    # ----- به تفکیک روش پرداخت (فروش + تمدید، بدون تست)
    by_method: dict[str, dict] = {}
    for tbl in ("orders", "custom_orders"):
        extra = " AND order_type != 'test'" if tbl == "orders" else ""
        for r in rows(
            f"SELECT COALESCE(NULLIF(payment_method, ''), 'نامشخص') AS m, COUNT(*) AS n, COALESCE(SUM(price), 0) AS s "
            f"FROM {tbl} WHERE status IN {sold} AND price > 0{extra} {where} GROUP BY m", p_dt):
            slot = by_method.setdefault(r["m"], {"count": 0, "amount": 0})
            slot["count"] += int(r["n"] or 0)
            slot["amount"] += int(r["s"] or 0)

    # ----- شارژ کیف پول در بازه
    tx_where = "AND created_at >= ? AND created_at <= ?" if ranged else ""
    wallet_charged = scalar(
        f"SELECT COALESCE(SUM(amount), 0) AS c FROM transactions WHERE type = 'charge' AND status = 'completed' {tx_where}", p_dt)

    # ----- صف‌ها و وضعیت لحظه‌ای
    pending_receipts = scalar("SELECT COUNT(*) AS c FROM pending_receipts WHERE status = 'pending'")
    pending_orders = scalar("SELECT COUNT(*) AS c FROM orders WHERE status = 'paid'") + \
        scalar("SELECT COUNT(*) AS c FROM custom_orders WHERE status = 'paid'")
    open_tickets = scalar("SELECT COUNT(*) AS c FROM tickets WHERE status != 'closed'")
    active_services = scalar(
        "SELECT COUNT(*) AS c FROM configs WHERE deleted = 0 AND (expiry IS NULL OR expiry = '' OR expiry >= date('now'))")
    total_services = scalar("SELECT COUNT(*) AS c FROM configs WHERE deleted = 0")
    total_agents = scalar("SELECT COUNT(*) AS c FROM agents")
    total_panels = scalar("SELECT COUNT(*) AS c FROM vpn_panels")
    enabled_panels = scalar("SELECT COUNT(*) AS c FROM vpn_panels WHERE enabled = 1")

    # ----- نسبت‌ها
    conversion_rate = round((total_buyers_ever / total_users) * 100, 1) if total_users else 0.0
    avg_per_customer = round(total_revenue / buyers) if buyers else 0
    renew_pct = round((renew_amount / total_revenue) * 100, 1) if total_revenue else 0.0
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    last7 = scalar(f"SELECT COALESCE(SUM(price), 0) AS c FROM orders WHERE status IN {sold} AND order_type != 'test' AND created_at >= ?", (seven_days_ago,))
    last7 += scalar(f"SELECT COALESCE(SUM(price), 0) AS c FROM custom_orders WHERE status IN {sold} AND created_at >= ?", (seven_days_ago,))
    predicted_monthly = round((last7 / 7) * 30)

    return {
        "total_users": total_users, "new_users": new_users, "blocked_users": blocked_users,
        "buyers": buyers, "total_buyers_ever": total_buyers_ever,
        "total_wallet": total_wallet, "total_locked": total_locked, "wallet_charged": wallet_charged,
        "plan_new_count": plan_new_count, "plan_new_amount": plan_new_amount,
        "custom_new_count": custom_new_count, "custom_new_amount": custom_new_amount,
        "sales_count": sales_count, "sales_amount": sales_amount,
        "renew_count": renew_count, "renew_amount": renew_amount, "renew_pct": renew_pct,
        "test_count": test_count, "total_revenue": total_revenue,
        "by_method": by_method,
        "pending_receipts": pending_receipts, "pending_orders": pending_orders, "open_tickets": open_tickets,
        "active_services": active_services, "total_services": total_services,
        "total_agents": total_agents, "total_panels": total_panels, "enabled_panels": enabled_panels,
        "conversion_rate": conversion_rate, "avg_per_customer": avg_per_customer,
        "predicted_monthly": predicted_monthly,
    }


def total_sales() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE type = 'purchase'")
    return _fetchone(cur)["s"]


def sales_since(days: int) -> int:
    cur = get_connection().cursor()
    cur.execute(
        """SELECT COALESCE(SUM(amount), 0) AS s FROM transactions
           WHERE type = 'purchase' AND created_at >= datetime('now', ?)""",
        (f"-{days} days",),
    )
    return _fetchone(cur)["s"]


def add_config(
    user_id: int,
    plan_name: str,
    encrypted_config: str,
    expiry: str | None,
    config_type: str = "vip",
    service_id: str | None = None,
    qr_file_id: str | None = None,
    source: str = "manual",
    panel_id: int | None = None,
    product_name: str | None = None,
    plan_key: str | None = None,
):
    category_id = None
    if plan_key:
        try:
            _p = get_vip_plan(plan_key)
            category_id = int(_p["category_id"]) if _p and _p.get("category_id") else None
        except Exception:
            category_id = None
    with transaction() as cur:
        cur.execute(
            """INSERT INTO configs (user_id, plan, config, expiry, created_at, type, service_id, qr_file_id, source, panel_id, product_name, plan_key, category_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, plan_name, encrypted_config, expiry, _now(), config_type, service_id, qr_file_id, source, panel_id, product_name,
             (plan_key if category_id else None), category_id),
        )
        return cur.lastrowid


def resolve_config_plan(cfg: dict) -> tuple[str | None, int | None]:
    """(plan_key, category_id) سرویس برای اعمال تنظیمات تمدید.
    اول مقدار ذخیره‌شده؛ برای سرویس‌های قدیمی (بدون ستون) از روی حجم/مدت نوشته‌شده در نام بسته حدس می‌زند:
    اگر فقط یک پلن (یا فقط یک پوشه) با همان حجم و مدت وجود داشته باشد همان انتخاب و برای دفعات بعد ذخیره می‌شود."""
    import re as _re
    pk = (cfg.get("plan_key") or "").strip() or None
    cid = cfg.get("category_id")
    try:
        cid = int(cid) if cid else None
    except Exception:
        cid = None
    if pk or cid:
        if pk and not cid:
            _p = get_vip_plan(pk)
            cid = int(_p["category_id"]) if _p and _p.get("category_id") else None
        return pk, cid
    try:
        raw = " | ".join(str(x) for x in (cfg.get("product_name"), cfg.get("plan")) if x)
        mv = _re.search(r"([0-9]+(?:\.[0-9]+)?)\s*گیگ", raw)
        md = _re.search(r"([0-9]+(?:\.[0-9]+)?)\s*روز", raw)
        mm = _re.search(r"([0-9]+(?:\.[0-9]+)?)\s*ماهه", raw)
        if not mv:
            return None, None
        vol = float(mv.group(1))
        days = float(md.group(1)) if md else (float(mm.group(1)) * 30 if mm else 0.0)
        matches = []
        for cat in get_vip_categories():
            for pl in get_vip_plans(cat["id"]):
                if abs(float(pl.get("volume_gb") or 0) - vol) < 1e-6 and abs(float(pl.get("days") or 0) - days) < 1e-6:
                    matches.append(pl)
        if len(matches) == 1:
            pk, cid = matches[0]["plan_key"], int(matches[0]["category_id"])
        elif matches and len({int(m["category_id"]) for m in matches}) == 1:
            pk, cid = None, int(matches[0]["category_id"])
        else:
            return None, None
        if cfg.get("id"):
            with transaction() as cur:
                cur.execute("UPDATE configs SET plan_key = ?, category_id = ? WHERE id = ?", (pk, cid, int(cfg["id"])))
        return pk, cid
    except Exception:
        return None, None


def set_config_title(config_id: int, title: str | None) -> None:
    """نام نمایشی دلخواه (مثلاً توسط نماینده) برای یک سرویس؛ روی پنل تأثیری ندارد."""
    with transaction() as cur:
        cur.execute("UPDATE configs SET custom_title = ? WHERE id = ?", ((title or "").strip()[:60] or None, int(config_id)))


def update_config(
    config_id: int,
    plan_name: str,
    encrypted_config: str,
    expiry: str | None,
    service_id: str | None = None,
    qr_file_id: str | None = None,
    panel_id: int | None = None,
):
    """آپدیت یک سرویس موجود (برای تمدید سرویس)؛ آلارم‌های حجم/انقضا هم ریست می‌شوند."""
    with transaction() as cur:
        if panel_id is not None:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?, service_id = ?,
                   qr_file_id = COALESCE(?, qr_file_id), panel_id = ?,
                   deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), service_id, qr_file_id, panel_id, config_id),
            )
        elif qr_file_id is not None:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?, service_id = ?,
                   qr_file_id = ?, deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), service_id, qr_file_id, config_id),
            )
        elif service_id is not None:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?, service_id = ?,
                   deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), service_id, config_id),
            )
        else:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?,
                   deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), config_id),
            )


def update_config_link(config_id: int, encrypted_config: str):
    """فقط لینک ساب یک سرویس را عوض می‌کند (برای ادیت دستی توسط ادمین)."""
    with transaction() as cur:
        cur.execute(
            """UPDATE configs SET config = ?, alert_80_sent = 0, alert_90_sent = 0,
               alert_expiry_sent = 0 WHERE id = ?""",
            (encrypted_config, config_id),
        )


def replace_config_link(config_id: int, encrypted_config: str, service_id: str | None = None):
    """تغییر لینک ساب یک سرویس پنلی: فقط لینک را عوض می‌کند (created_at دست‌نخورده می‌ماند)
    و QR قدیمی را پاک می‌کند، چون QR ذخیره‌شده مربوط به لینک قبلی است و بعد از تغییر لینک
    نباید دوباره به کاربر نشان داده شود."""
    with transaction() as cur:
        cur.execute(
            """UPDATE configs SET config = ?, qr_file_id = NULL, service_id = COALESCE(?, service_id),
               alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
            (encrypted_config, service_id, config_id),
        )


def link_config_to_panel(config_id: int, panel_id: int, service_id: str, source: str,
                         service_name: str | None = None, plan: str | None = None):
    """سرویسی که دستی ارسال شده را به کاربر واقعی همان پنل وصل می‌کند تا اطلاعات زنده
    (مصرف، اتصال، دستگاه‌ها، تغییر لینک، فعال/غیرفعال) از پنل میرور شود."""
    with transaction() as cur:
        cur.execute(
            """UPDATE configs SET panel_id = ?, service_id = ?, source = ?,
               service_name = COALESCE(?, service_name), plan = COALESCE(?, plan) WHERE id = ?""",
            (int(panel_id), service_id, source, service_name, plan, int(config_id)),
        )


def set_config_qr(config_id: int, qr_file_id: str):
    with transaction() as cur:
        cur.execute("UPDATE configs SET qr_file_id = ? WHERE id = ?", (qr_file_id, config_id))


def set_config_custom_option(config_id: int, option_num) -> None:
    """سرویسِ ساخته‌شده از «بساز سرویس خودت»: گزینه‌ی نگاشت (قیمت/محدوده) برای تمدید همان گزینه ذخیره می‌شود."""
    with transaction() as cur:
        cur.execute("UPDATE configs SET custom_option_num = ? WHERE id = ?", (int(option_num or 0), int(config_id)))


def config_custom_option(cfg: dict | None):
    """None = سرویس سفارشی نیست؛ عدد (۰ = تنظیمات پیش‌فرض) = سرویس سفارشی با آن گزینه."""
    v = (cfg or {}).get("custom_option_num")
    if v is None or v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


def set_config_deleted(config_id: int, deleted: bool):
    with transaction() as cur:
        cur.execute("UPDATE configs SET deleted = ? WHERE id = ?", (1 if deleted else 0, config_id))


def set_config_link_disabled(config_id: int, disabled: bool):
    with transaction() as cur:
        cur.execute("UPDATE configs SET sub_link_disabled = ? WHERE id = ?", (1 if disabled else 0, config_id))


def mark_config_as_payg(config_id: int, price_per_gb: int, last_used_bytes: int = 0, paused: bool = False):
    with transaction() as cur:
        cur.execute(
            "UPDATE configs SET type = 'payg', payg_price_per_gb = ?, payg_last_used_bytes = ?, payg_paused = ? WHERE id = ?",
            (int(price_per_gb or 0), int(last_used_bytes or 0), 1 if paused else 0, int(config_id)),
        )


def update_payg_usage_snapshot(config_id: int, used_bytes: int):
    with transaction() as cur:
        cur.execute("UPDATE configs SET payg_last_used_bytes = ? WHERE id = ?", (int(used_bytes or 0), int(config_id)))


def get_fup_candidates(limit: int = 2000) -> list[dict]:
    """سرویس‌های متصل به پنل (غیر PAYG) که ممکن است نامحدود باشند؛ نامحدودبودن از اسنپ‌شات زنده‌ی پنل تشخیص داده می‌شود."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM configs WHERE type != 'payg' AND deleted = 0 AND panel_id IS NOT NULL AND service_id IS NOT NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return _fetchall(cur)


def get_fup_blocked_configs(limit: int = 500) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM configs WHERE fup_blocked = 1 AND deleted = 0 ORDER BY id DESC LIMIT ?", (limit,))
    return _fetchall(cur)


def get_fup_started_configs(limit: int = 500) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM configs WHERE fup_started = 1 AND deleted = 0 ORDER BY id DESC LIMIT ?", (limit,))
    return _fetchall(cur)


def set_fup_state(config_id: int, started=None, blocked=None, base="__keep__", day="__keep__"):
    sets, vals = [], []
    if started is not None:
        sets.append("fup_started = ?"); vals.append(1 if started else 0)
    if blocked is not None:
        sets.append("fup_blocked = ?"); vals.append(1 if blocked else 0)
    if base != "__keep__":
        sets.append("fup_base_bytes = ?"); vals.append(base)
    if day != "__keep__":
        sets.append("fup_day = ?"); vals.append(day)
    if not sets:
        return
    vals.append(int(config_id))
    with transaction() as cur:
        cur.execute(f"UPDATE configs SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def fup_reset_config(config_id: int):
    """پاک‌کردن وضعیت مصرف منصفانه (مثلاً بعد از آزادسازی دستی ادمین یا تغییر سرویس به محدود)."""
    set_fup_state(config_id, started=False, blocked=False, base=None, day=None)


def set_payg_paused(config_id: int, paused: bool):
    with transaction() as cur:
        cur.execute("UPDATE configs SET payg_paused = ? WHERE id = ?", (1 if paused else 0, int(config_id)))


def get_wallet_fresh(user_id: int) -> int:
    """موجودی لحظه‌ای کاربر مستقیم از دیتابیس (بدون کش)."""
    cur = get_connection().cursor()
    cur.execute("SELECT wallet FROM users WHERE id = ?", (int(user_id),))
    r = cur.fetchone()
    return int(r[0] or 0) if r else 0


def pop_wallet_low_candidates() -> list[tuple[int, int]]:
    """[(user_id, old_wallet)] کاربرانی که از آخرین بررسی موجودی‌شان کم شده (صف را خالی می‌کند)."""
    with transaction() as cur:
        cur.execute("SELECT user_id, old_wallet FROM wallet_low_queue")
        rows = [(int(r[0]), int(r[1])) for r in cur.fetchall()]
        if rows:
            cur.execute("DELETE FROM wallet_low_queue")
    return rows


def pop_payg_resume_users() -> list[int]:
    """کاربرانی که از آخرین بررسی موجودی‌شان زیاد شده (صف را خالی می‌کند)."""
    with transaction() as cur:
        cur.execute("SELECT user_id FROM payg_resume_queue")
        ids = [int(r[0]) for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM payg_resume_queue")
    return ids


def get_paused_payg_configs_for_user(user_id: int) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM configs WHERE type = 'payg' AND deleted = 0 AND payg_paused = 1 AND user_id = ? "
        "AND panel_id IS NOT NULL AND service_id IS NOT NULL", (user_id,))
    return _fetchall(cur)


def get_active_payg_configs(limit: int = 500) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM configs WHERE type = 'payg' AND deleted = 0 AND panel_id IS NOT NULL AND service_id IS NOT NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return _fetchall(cur)


def archive_expired_configs() -> int:
    """کانفیگ‌های منقضی‌شده (expiry < today) را به‌صورت خودکار آرشیو می‌کند (deleted=1).
    تعداد کانفیگ‌های آرشیو‌شده را برمی‌گرداند. (تاریخ امروز بر اساس ساعت تهران محاسبه می‌شود.)"""
    from utils import now_tehran
    today_str = now_tehran().strftime("%Y-%m-%d")
    with transaction() as cur:
        cur.execute(
            """UPDATE configs SET deleted = 1
               WHERE deleted = 0 AND expiry IS NOT NULL AND expiry != ''
               AND substr(expiry, 1, 10) < ?""",
            (today_str,),
        )
        return cur.rowcount


def delete_config_permanently(config_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM configs WHERE id = ?", (config_id,))


def set_config_alert_sent(config_id: int, field: str):
    if field not in ("alert_80_sent", "alert_90_sent", "alert_expiry_sent", "alert_ended_sent"):
        return
    with transaction() as cur:
        cur.execute(f"UPDATE configs SET {field} = 1 WHERE id = ?", (config_id,))


def get_active_vip_configs() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM configs WHERE type = 'vip' AND deleted = 0")
    return _fetchall(cur)




# ---------------------------------------------------------------------------
# صف سفارشات (پیگیری خریدهای تأییدشده‌ای که هنوز کانفیگ‌شان ارسال نشده)
# ---------------------------------------------------------------------------
ORDER_STATES = {"pending", "processing", "paid", "fulfilled", "rejected", "dismissed", "failed"}
ALLOWED_ORDER_TRANSITIONS = {
    "pending": {"processing", "paid", "fulfilled", "rejected", "dismissed", "failed"},
    "paid": {"processing", "fulfilled", "failed", "dismissed"},
    "processing": {"fulfilled", "paid", "pending", "failed"},
    "fulfilled": set(), "rejected": set(), "dismissed": set(), "failed": {"pending", "processing"},
}

def create_order(user_id: int, plan_key: str | None, plan_name: str, order_type: str, price: int, idempotency_key: str | None = None, payment_method: str | None = None, created_at: str | None = None, service_name: str | None = None) -> int:
    with transaction() as cur:
        if idempotency_key:
            cur.execute("SELECT id FROM orders WHERE idempotency_key = ?", (idempotency_key,))
            existing = cur.fetchone()
            if existing: return int(existing[0])
        cur.execute(
            """INSERT INTO orders (user_id, plan_key, plan_name, order_type, price, status, state, idempotency_key, payment_method, created_at, service_name)
               VALUES (?, ?, ?, ?, ?, 'pending', 'pending', ?, ?, ?, ?)""",
            (user_id, plan_key, plan_name, order_type, price, idempotency_key, payment_method, (str(created_at)[:19] if created_at else _now()), (str(service_name).strip() or None) if service_name else None),
        )
        oid = cur.lastrowid
    audit_event("system", "order.created", "order", str(oid), f"user={user_id};type={order_type};price={price}")
    return oid

def set_order_status(order_id: int, status: str):
    if status not in ORDER_STATES: raise ValueError(f"invalid order state: {status}")
    with transaction() as cur:
        cur.execute("SELECT status,state FROM orders WHERE id=?", (order_id,)); row=cur.fetchone()
        if not row: raise ValueError("order not found")
        current=row[0] or row[1] or "pending"
        if current != status and status not in ALLOWED_ORDER_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid order transition {current}->{status}")
        cur.execute("UPDATE orders SET status=?, state=? WHERE id=?", (status,status,order_id))
    audit_event("system", f"order.{status}", "order", str(order_id), f"from={current}")

def claim_order_for_processing(order_id: int) -> bool:
    with transaction() as cur:
        cur.execute("UPDATE orders SET status='processing', state='processing' WHERE id=? AND status IN ('pending','paid')", (order_id,))
        ok = (cur.rowcount or 0) == 1
    if ok: audit_event("system", "order.processing", "order", str(order_id))
    return ok


def get_order(order_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    return _fetchone(cur)


def get_pending_orders(limit: int = 30) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM orders WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# پرداخت آنلاین (درگاه یونیک‌پی — UniquePay)
# ---------------------------------------------------------------------------
def create_online_payment(
    user_id: int,
    telegram_id: str,
    hash_id: str,
    plan_name: str,
    price: int,
    order_type: str = "vip",
    plan_key: str | None = None,
    discount_code: str | None = None,
    payment_link: str | None = None,
    ref_id: str | None = None,
    kind: str = "plan",
    extra: str | None = None,
    gateway: str = "uniquepay",
    gw_data: dict | None = None,
) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO online_payments
               (hash_id, user_id, telegram_id, kind, plan_key, plan_name, order_type,
                price, discount_code, payment_link, ref_id, status, extra, created_at,
                gateway, gw_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (hash_id, user_id, str(telegram_id), kind, plan_key, plan_name, order_type,
             price, discount_code, payment_link, ref_id, extra, _now(),
             gateway or "uniquepay",
             json.dumps(gw_data, ensure_ascii=False) if gw_data else None),
        )
        _pid = cur.lastrowid
    _adv_hook("payment_event", _pid, "created")
    return _pid


def get_online_payment_by_hash(hash_id: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE hash_id = ?", (hash_id,))
    return _fetchone(cur)


def get_online_payment(payment_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
    return _fetchone(cur)


def _consume_discount_in_transaction(cur, code: str | None, user_id: int) -> bool:
    """نسخه‌ی داخلی use_discount برای استفاده داخل تراکنش‌های مالی بزرگ‌تر."""
    if not code:
        return False
    cur.execute("SELECT * FROM discounts WHERE code = ?", (code.upper(),))
    row = _fetchone(cur)
    if row is None or row["uses"] <= 0:
        return False
    max_per_user = int(row.get("max_uses_per_user") or 0)
    if max_per_user > 0:
        cur.execute(
            "SELECT COUNT(*) AS c FROM discount_usages WHERE discount_id = ? AND user_id = ?",
            (row["id"], user_id),
        )
        if _fetchone(cur)["c"] >= max_per_user:
            return False
    cur.execute("UPDATE discounts SET uses = uses - 1 WHERE id = ? AND uses > 0", (row["id"],))
    if (cur.rowcount or 0) == 0:
        return False
    cur.execute(
        "INSERT INTO discount_usages (discount_id, user_id, used_at) VALUES (?, ?, ?)",
        (row["id"], user_id, _now()),
    )
    _adv_hook("discount_used", code.upper(), user_id)
    return True


def finalize_online_plan_payment_atomic(payment_id: int) -> tuple[int | None, bool]:
    """Claim، ساخت سفارش، مصرف تخفیف و paid کردن پرداخت در یک تراکنش.
    خروجی (order_id, created_now) است."""
    with transaction() as cur:
        cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
        payment = _fetchone(cur)
        if payment is None:
            return None, False
        if payment["status"] == "paid":
            return payment.get("order_id"), False
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            return None, False
        cur.execute(
            """INSERT INTO orders (user_id, plan_key, plan_name, order_type, price, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (payment["user_id"], payment["plan_key"], payment["plan_name"],
             payment["order_type"], payment["price"], _now()),
        )
        order_id = cur.lastrowid
        _consume_discount_in_transaction(cur, payment.get("discount_code"), payment["user_id"])
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = ? WHERE id = ? AND status = 'processing'",
            (order_id, payment_id),
        )
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("finalize online plan lost payment claim")
        return order_id, True


def finalize_online_custom_payment_atomic(payment_id: int) -> tuple[int | None, bool]:
    with transaction() as cur:
        cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
        payment = _fetchone(cur)
        if payment is None:
            return None, False
        if payment["status"] == "paid":
            return payment.get("order_id"), False
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            return None, False
        extra = json.loads(payment.get("extra") or "{}")
        volume, days = int(extra["volume"]), int(extra["days"])
        cur.execute(
            """INSERT INTO custom_orders
               (user_id, volume_gb, days, custom_name, price, order_type, target_config_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'paid', ?)""",
            (payment["user_id"], volume, days, extra.get("custom_name"), payment["price"],
             extra.get("order_type", "new"), extra.get("target_config_id"), _now()),
        )
        order_id = cur.lastrowid
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = ? WHERE id = ? AND status = 'processing'",
            (order_id, payment_id),
        )
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("finalize online custom lost payment claim")
        return order_id, True


def finalize_online_wallet_payment_atomic(payment_id: int) -> tuple[int | None, bool]:
    with transaction() as cur:
        cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
        payment = _fetchone(cur)
        if payment is None:
            return None, False
        if payment["status"] == "paid":
            return payment_id, False
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            return None, False
        if int(payment["price"]) <= 0:
            raise ValueError("invalid wallet charge amount")
        cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (payment["price"], payment["user_id"]))
        if (cur.rowcount or 0) == 0:
            raise ValueError("wallet charge user not found")
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'charge', ?, 'completed', ?, ?)""",
            (payment["user_id"], payment["price"], "شارژ کیف پول (پرداخت آنلاین)", _now()),
        )
        _bonus = wallet_gift_bonus(int(payment["price"]))
        if _bonus > 0:
            cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (_bonus, payment["user_id"]))
            cur.execute(
                """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
                   VALUES (?, 'gift', ?, 'completed', ?, ?)""",
                (payment["user_id"], _bonus, f"🎁 هدیه‌ی شارژ ({wallet_gift_percent():g}٪)", _now()),
            )
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = NULL WHERE id = ? AND status = 'processing'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("finalize wallet charge lost payment claim")
    _invalidate_user_cache_by_id(payment["user_id"])
    return payment_id, True


def recover_stuck_online_payments() -> int:
    """بازیابی داده‌های processing باقی‌مانده از نسخه‌های قدیمی/کرش قبلی."""
    with transaction() as cur:
        cur.execute("UPDATE online_payments SET status = 'pending' WHERE status = 'processing'")
        return cur.rowcount or 0


def claim_online_payment_for_finalize(payment_id: int) -> bool:
    """🐛 فیکس ریس‌کاندیشن: قبلاً finalize_online_payment فقط با یک شرط ساده
    در پایتون (status == 'paid' and order_id) چک می‌کرد که آیا قبلاً پردازش
    شده یا نه؛ چون این تابع هم از پولر پس‌زمینه‌ی ربات (bot.py) و هم از
    endpoint وضعیت پرداخت Mini App (webapp_api.py، در یک پروسه‌ی کاملاً جدا)
    صدا زده می‌شود، دو فراخوانی هم‌زمان می‌توانستند هر دو تشخیص «هنوز پردازش
    نشده» بدهند و هر دو یک سفارش/سرویس جداگانه برای همون یک پرداخت بسازند.

    این تابع با یک UPDATE شرطی اتمیک (status='pending' → 'processing')
    تضمین می‌کند که از بین چند فراخوانی هم‌زمان، فقط دقیقاً یکی برنده شود؛
    فقط همان فراخوانی باید ادامه‌ی مسیر (ساخت سفارش/ارسال کانفیگ) را انجام
    دهد. بقیه باید False دریافت کنند و کاری نکنند."""
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        claimed = (cur.rowcount or 0) > 0
    return claimed


def get_pending_online_payments(limit: int = 50) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM online_payments WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
    )
    return _fetchall(cur)


def mark_online_payment_paid(payment_id: int, order_id: int | None):
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = ? WHERE id = ?",
            (order_id, payment_id),
        )
    _adv_hook("payment_event", int(payment_id), "paid")


def set_online_payment_status(payment_id: int, status: str):
    with transaction() as cur:
        cur.execute("UPDATE online_payments SET status = ? WHERE id = ?", (status, payment_id))
    if status not in ("pending",):
        _adv_hook("payment_event", int(payment_id), "status:" + str(status))


# ---------------------------------------------------------------------------
# ⏱ فاکتورهای مهلت‌دار (30 دقیقه‌ای) برای پرداخت کارت‌به‌کارت
# (پلن/بساز-سرویس/شارژ کیف‌پول)‌‌ — هم در ربات (FSM state) و هم در Mini App
# (که بین دو درخواست بی‌حالت است و نمی‌تواند به FSM تکیه کند) استفاده می‌شود.
# پرداخت آنلاین (تابل online_payments) از همین مهلت استفاده می‌کند ولی رد جداگانه‌ای
# ندارد (به expire_due_online_payments نگاه کنید).
# ---------------------------------------------------------------------------
INVOICE_EXPIRY_MINUTES = 30


def online_payment_expires_at(created_at: str, minutes: int = INVOICE_EXPIRY_MINUTES) -> str:
    """زمان واقعی انقضای یک پرداخت آنلاین را از روی زمان ساخت (created_at) محاسبه می‌کند.
    جدول online_payments فقط created_at را دارد (نه expires_at)، پس این تابع همان
    مقداری که توسط expire_due_online_payments برای حذف واقعی استفاده می‌شود را برمی‌گرداند
    تا به مینی‌اپ (برای شمارش‌معکوس واقعی) برگردانده شود، نه created_at."""
    from datetime import timedelta
    dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def create_invoice(user_id, telegram_id, kind: str, label: str, price: int, payload: dict | None = None,
                    minutes: int = INVOICE_EXPIRY_MINUTES) -> dict:
    """یک «فاکتور» جدید برای مرحله‌ی کارت‌به‌کارت (از همان لحظه‌یی که شماره‌ی
    کارت و قیمت نهایی به کاربر نمایش داده می‌شود) می‌سازد. مهلت این فاکتور
    دقیقاً "minutes" دقیقه (پیش‌فرض: 30) است. پس از این مدت، اگر هیچ رسیدی برایش
    ثبت نشود (consume_invoice صدا زده نشود)، توسط invoice_expiry_loop حذف می‌شود."""
    now = datetime.now()
    from datetime import timedelta
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    with transaction() as cur:
        cur.execute(
            "INSERT INTO invoices (user_id, telegram_id, kind, label, price, payload, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (user_id, telegram_id, kind, label, price, payload_json, created_at, expires_at),
        )
        invoice_id = cur.lastrowid
    return get_invoice(invoice_id)


def get_invoice(invoice_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    return _fetchone(cur)


def consume_invoice(invoice_id: int) -> dict | None:
    """وقتی کاربر رسید را واقعاً ارسال می‌کند صدا زده می‌شود: اگر فاکتور هنوز
    pending و منقضی نشده باشد، اتمیکاً وضعیتش را 'submitted' می‌کند (تا فرایند
    پس‌زمینه‌ی پاک‌سازی فاکتورهای منقضی دیگر آن را حذف نکند) و ردیف را برمی‌گرداند، وگرنه None (یعنی وجود ندارد یا
    از قبل منقضی/مصرف‌شده است — یعنی همین مهلت 30 دقیقه‌ای به پایان رسیده)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as cur:
        cur.execute(
            "UPDATE invoices SET status = 'submitted' WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (invoice_id, now),
        )
        claimed = (cur.rowcount or 0) > 0
    if not claimed:
        return None
    return get_invoice(invoice_id)


def delete_invoice(invoice_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))


def expire_due_invoices() -> list[dict]:
    """فاکتورهای کارت‌به‌کارت (pending) که مهلت 30 دقیقه‌ای‌شان تمام شده و هنوز هیچ
    رسیدی برایشان ثبت نشده را از دیتابیس حذف می‌کند و لیستشان را (برای اطلاع‌رسانی
    به کاربر توسط invoice_expiry_loop) برمی‌گرداند."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM invoices WHERE status = 'pending' AND expires_at <= ?", (now,))
    rows = _fetchall(cur)
    for row in rows:
        with transaction() as cur2:
            cur2.execute("DELETE FROM invoices WHERE id = ? AND status = 'pending'", (row["id"],))
    return rows


def expire_due_online_payments() -> list[dict]:
    """پرداخت‌های آنلاین pending (پلن/بساز-سرویس/شارژ کیف‌پول — همه با kind
    یکتا در همین جدول هستند) که بیش از INVOICE_EXPIRY_MINUTES دقیقه از ساختشان گذشته را
    به‌صورت اتمیک claim (فقط اگر هنوز pending باشند و توسط پولر/دکمه‌ی «بررسی کن» در
    حال finalize قرار نگرفته‌اند) منقضی و لیستشان را برمی‌گرداند.

    - یونیک‌پی (رفتار قدیمی): ردیف حذف می‌شود.
    - درگاه‌های جدید (FrenzyEx/Tronado): ردیف حذف نمی‌شود و status='expired' می‌گیرد؛ چون
      ممکن است کاربر دقیقاً لحظه‌ی انقضا پرداخت کرده باشد و کال‌بک/استعلام بعداً برسد؛
      اگر ردیف حذف می‌شد، پول دریافت‌شده بدون هیچ ردی گم می‌شد (پرداخت دیرهنگام در
      online_gateways بررسی و به کیف پول کاربر واریز می‌شود)."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(minutes=INVOICE_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    cur = get_connection().cursor()
    # تون‌پیز مهلت داخلی ۳۰ دقیقه‌ای ندارد؛ انقضا/لغو فقط از طرف خود درگاه (status=expired/canceled) اعمال می‌شود.
    cur.execute("SELECT * FROM online_payments WHERE status = 'pending' AND created_at <= ? AND COALESCE(gateway, 'uniquepay') != 'tonpays'", (cutoff,))
    candidates = _fetchall(cur)
    expired = []
    for row in candidates:
        keep_row = (row.get("gateway") or "uniquepay") != "uniquepay"
        with transaction() as cur2:
            if keep_row:
                cur2.execute(
                    "UPDATE online_payments SET status = 'expired' WHERE id = ? AND status = 'pending'",
                    (row["id"],),
                )
            else:
                cur2.execute("DELETE FROM online_payments WHERE id = ? AND status = 'pending'", (row["id"],))
            changed = (cur2.rowcount or 0) > 0
        if changed:
            expired.append(row)
    return expired


# ---------------------------------------------------------------------------
# 🌐 درگاه‌های آنلاین جدید (FrenzyEx / Tronado): وضعیت اختصاصی و پرداخت دیرهنگام
# ---------------------------------------------------------------------------
GATEWAY_LATE_WINDOW_HOURS = 72      # تا این مدت بعد از انقضا هنوز منتظر پرداخت دیرهنگام می‌مانیم
GATEWAY_LATE_POLL_SECONDS = 300     # فاصله‌ی استعلام ردیف‌های منقضی‌شده (کال‌بک بی‌درنگ است)


def gw_data_of(payment: dict | None) -> dict:
    """gw_data یک ردیف online_payments را به‌صورت dict برمی‌گرداند (همیشه dict، حتی اگر خراب باشد)."""
    if not payment:
        return {}
    raw = payment.get("gw_data")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def merge_gw_data(payment_id: int, **fields) -> dict:
    """فیلدهای gw_data را به‌صورت اتمیک (خواندن+نوشتن داخل یک تراکنش) ادغام می‌کند و نتیجه را برمی‌گرداند.
    مقدار None یعنی حذف کلید."""
    with transaction() as cur:
        cur.execute("SELECT gw_data FROM online_payments WHERE id = ?", (payment_id,))
        row = cur.fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row[0]) if row[0] else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        for k, v in fields.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        cur.execute(
            "UPDATE online_payments SET gw_data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), payment_id),
        )
    return data


def get_gateway_work_payments(limit: int = 50) -> list[dict]:
    """ردیف‌هایی که پولر باید برای درگاه‌های جدید بررسی کند:
    - همه‌ی pending ها (هر درگاهی؛ پولر خودش بر اساس gateway مسیر را انتخاب می‌کند)
    - expired های درگاه‌های جدید در پنجره‌ی ۷۲ ساعته: اگر کال‌بک تأییدشده دارند همیشه، وگرنه
      حداکثر هر ۵ دقیقه یک بار (برای پرداخت دیرهنگام بدون کال‌بک)."""
    import time as _t
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    rows = _fetchall(cur)
    cutoff = (datetime.now() - timedelta(hours=GATEWAY_LATE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT * FROM online_payments WHERE status = 'expired' AND gateway <> 'uniquepay' "
        "AND created_at >= ? ORDER BY id DESC LIMIT ?",
        (cutoff, limit),
    )
    now = _t.time()
    for row in _fetchall(cur):
        data = gw_data_of(row)
        if data.get("closed") and not data.get("confirmed"):
            continue  # خود درگاه اعلام کرده منقضی/لغو/رد شده؛ دیگر استعلام نمی‌کنیم
        if data.get("confirmed"):
            rows.append(row)
        elif now - float(data.get("last_check") or 0) >= GATEWAY_LATE_POLL_SECONDS:
            rows.append(row)
    return rows


def claim_expired_gateway_payment(payment_id: int) -> bool:
    """claim اتمیک ردیف expired (برای پرداخت دیرهنگام): expired → processing."""
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'expired' AND gateway <> 'uniquepay'",
            (payment_id,),
        )
        return (cur.rowcount or 0) > 0


def approve_review_gateway_payment(payment_id: int) -> bool:
    """تأیید دستی ادمین برای پرداخت در وضعیت review: review → pending با پرچم approved
    (تا پولر دوباره آن را به‌خاطر همان مغایرت مبلغ به review برنگرداند)."""
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'pending' WHERE id = ? AND status = 'review'",
            (payment_id,),
        )
        changed = (cur.rowcount or 0) > 0
    if changed:
        merge_gw_data(payment_id, approved=True)
    return changed


def reject_review_gateway_payment(payment_id: int) -> bool:
    """رد دستی ادمین: review → rejected (هیچ شارژ/سفارشی انجام نمی‌شود)."""
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'rejected' WHERE id = ? AND status = 'review'",
            (payment_id,),
        )
        return (cur.rowcount or 0) > 0


def mark_gateway_payment_late(payment_id: int) -> bool:
    """expired → pending با پرچم gw_data.late (در یک تراکنش) تا واریز اتمیک به کیف پول انجام شود.
    پرچم late باعث می‌شود اگر وسط کار خطا افتاد، تلاش بعدی هم به‌جای تحویل سفارش، به کیف پول واریز کند."""
    with transaction() as cur:
        cur.execute("SELECT status, gw_data FROM online_payments WHERE id = ?", (payment_id,))
        row = cur.fetchone()
        if row is None or row[0] != "expired":
            return False
        try:
            data = json.loads(row[1]) if row[1] else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["late"] = True
        cur.execute(
            "UPDATE online_payments SET status = 'pending', gw_data = ? WHERE id = ? AND status = 'expired'",
            (json.dumps(data, ensure_ascii=False), payment_id),
        )
        return (cur.rowcount or 0) > 0


def reopen_expired_gateway_payment(payment_id: int) -> bool:
    """پرداخت دیرهنگام: expired → pending با پرچم late_paid (در یک تراکنش) تا سفارش/شارژ اصلی
    همان‌طور که کاربر انتظار دارد خودکار تحویل داده شود (نه واریز به کیف پول)."""
    with transaction() as cur:
        cur.execute("SELECT status, gw_data FROM online_payments WHERE id = ?", (payment_id,))
        row = cur.fetchone()
        if row is None or row[0] != "expired":
            return False
        try:
            data = json.loads(row[1]) if row[1] else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["late_paid"] = True
        cur.execute(
            "UPDATE online_payments SET status = 'pending', gw_data = ? WHERE id = ? AND status = 'expired'",
            (json.dumps(data, ensure_ascii=False), payment_id),
        )
        return (cur.rowcount or 0) > 0


def hold_online_payment_for_review(payment_id: int, reason: str) -> bool:
    """pending/expired → review (یک‌بار؛ True فقط برای اولین انتقال تا اعلان ادمین تکراری نشود)."""
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'review' WHERE id = ? AND status IN ('pending', 'expired')",
            (payment_id,),
        )
        changed = (cur.rowcount or 0) > 0
    if changed:
        merge_gw_data(payment_id, review_reason=str(reason)[:300])
    return changed


def get_online_payment_by_gateway_ref_id(gateway: str, ref_id: str) -> dict | None:
    """ردیف را با ref_id (request_id فرنزی / توکن ترونادو) برمی‌گرداند."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE ref_id = ? AND gateway = ?", (ref_id, gateway))
    return _fetchone(cur)


def get_online_payment_for_gateway_ref(gateway: str, ref: str) -> dict | None:
    """ردیف را با شناسه‌ی ارجاع درگاه (hash_id = order_ref/PaymentID) برای یک درگاه مشخص برمی‌گرداند."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE hash_id = ? AND gateway = ?", (ref, gateway))
    return _fetchone(cur)


# ---------------------------------------------------------------------------
# ⚙️ تنظیمات کلی (key-value) — مثل روشن/خاموش بودن بخش سفارشات
# ---------------------------------------------------------------------------
REFERRAL_SETTING_PREFIX = "referral_"

def get_referral_settings() -> dict:
    """تنظیمات رفرال را از دیتابیس می‌خواند؛ config فقط fallback اولیه است."""
    return {
        # 🔘 کلید اصلی رفرال: خاموش = دریافت رفرال/پاداش/جریمه و همه‌ی بخش‌های دعوت دوستان غیرفعال
        "enabled": get_setting(REFERRAL_SETTING_PREFIX + "enabled", "1") == "1",
        "min_volume_enabled": get_setting(REFERRAL_SETTING_PREFIX + "min_volume_enabled", "1") == "1",
        "paid_purchase_required": get_setting(REFERRAL_SETTING_PREFIX + "paid_purchase_required", "1") == "1",
        "min_volume_gb": max(0, int(get_setting(REFERRAL_SETTING_PREFIX + "min_volume_gb", str(REFERRAL_MIN_VOLUME_GB)) or REFERRAL_MIN_VOLUME_GB)),
        "reward_amount": max(0, int(get_setting(REFERRAL_SETTING_PREFIX + "reward_amount", str(REFERRAL_LOCK_AMOUNT)) or REFERRAL_LOCK_AMOUNT)),
        "channel_leave_lock_enabled": get_setting(REFERRAL_SETTING_PREFIX + "channel_leave_lock_enabled", "1") == "1",
        # 🆕 سیستم جریمه‌ی رفرال: اگر فرد دعوت‌شده (B) از یکی از کانال‌های
        # اجباری خارج شود، این دو جریمه‌ی مستقل روی معرف (A) اعمال می‌شود:
        #   ۱) penalty_wallet_amount تومان از کیف پول *قابل‌برداشت* او کم می‌شود
        #      (فرقش با channel_leave_lock بالا: اونجا فقط پاداشِ همین رفرال
        #      دوباره قفل می‌شد و قابل‌برگشت بود؛ این‌جا یک کسر مجزا و
        #      مجازاتی است، مبلغش هم دستی و مستقل تنظیم می‌شود).
        #   ۲) اگر A سرویسی دارد که با پول کیف‌پولِ رفرالی‌اش خریده،
        #      penalty_service_gb گیگابایت مستقیماً از حجم همان سرویس در
        #      خودِ پنل کم می‌شود.
        # پیش‌فرض خاموش است چون رفتاری تنبیهی و به‌طور بالقوه برگشت‌ناپذیر
        # (روی حجم سرویس در پنل) دارد؛ فقط با تأیید صریح ادمین فعال می‌شود.
        "channel_leave_penalty_enabled": get_setting(REFERRAL_SETTING_PREFIX + "channel_leave_penalty_enabled", "0") == "1",
        "penalty_wallet_amount": max(0, int(get_setting(REFERRAL_SETTING_PREFIX + "penalty_wallet_amount", "0") or 0)),
        "penalty_service_gb": max(0, int(get_setting(REFERRAL_SETTING_PREFIX + "penalty_service_gb", "0") or 0)),
    }

def referral_enabled() -> bool:
    """آیا کل سیستم رفرال روشن است؟ (پیش‌فرض روشن)"""
    try:
        return get_setting(REFERRAL_SETTING_PREFIX + "enabled", "1") == "1"
    except Exception:
        return True


def set_referral_setting(key: str, value) -> None:
    allowed = {
        "enabled",
        "min_volume_enabled", "paid_purchase_required", "min_volume_gb", "reward_amount",
        "channel_leave_lock_enabled", "channel_leave_penalty_enabled", "penalty_wallet_amount", "penalty_service_gb",
    }
    if key not in allowed:
        raise ValueError("تنظیم رفرال نامعتبر است")
    if key in {"min_volume_enabled", "paid_purchase_required", "channel_leave_lock_enabled", "channel_leave_penalty_enabled"}:
        value = "1" if bool(value) else "0"
    else:
        value = str(max(0, int(value)))
    set_setting(REFERRAL_SETTING_PREFIX + key, value)

def referral_purchase_qualifies(volume_gb, paid_purchase: bool = True, is_free_test: bool = False) -> bool:
    """آیا خرید فعلی باید پاداش رفرال را آزاد کند؟
    حالت‌ها: حداقل حجم، هر خرید پولی، یا بدون هیچ شرط خرید.
    تست رایگان هیچ‌وقت خرید واجد شرایط محسوب نمی‌شود.
    """
    settings = get_referral_settings()
    if is_free_test or not paid_purchase:
        return False
    if not settings["paid_purchase_required"]:
        return True
    if settings["min_volume_enabled"]:
        try:
            return float(volume_gb or 0) >= settings["min_volume_gb"]
        except (TypeError, ValueError):
            return False
    return True

def release_all_pending_referrals() -> int:
    """تمام رفرال‌های pending را در یک تراکنش آزاد می‌کند؛ برای خاموش‌کردن کامل شرط خرید."""
    released = 0
    with transaction() as cur:
        cur.execute("SELECT * FROM referrals WHERE status = 'pending'")
        refs = _fetchall(cur)
        for ref in refs:
            reward = int(ref["reward"] or 0)
            cur.execute("SELECT locked_wallet FROM users WHERE id = ?", (ref["referrer_id"],))
            row = _fetchone(cur)
            if row is None or int(row["locked_wallet"] or 0) < reward:
                continue
            cur.execute("UPDATE referrals SET status = 'completed' WHERE id = ?", (ref["id"],))
            cur.execute("UPDATE users SET successful_invites = successful_invites + 1, locked_wallet = locked_wallet - ?, wallet = wallet + ? WHERE id = ?", (reward, reward, ref["referrer_id"]))
            cur.execute("INSERT INTO transactions (user_id, type, amount, status, description, created_at) VALUES (?, 'referral_release', ?, 'completed', ?, ?)", (ref["referrer_id"], reward, "آزادسازی فوری پاداش دعوت (بدون شرط خرید)", _now()))
            released += 1
    return released


# ---------------------------------------------------------------------------
# 🚪 قفل‌شدن پاداش رفرال هنگام لفت‌دادن از کانال اجباری
# اگر کاربری که با رفرال آمده از یکی از کانال‌های اجباری خارج شود و پاداش
# معرفش قبلاً واقعاً آزاد شده باشد (status='completed')، آن مبلغ دوباره از
# کیف پول قابل‌برداشت معرف به کیف پول مسدودش برمی‌گردد (status='held') تا
# کاربر دوباره عضو همه‌ی کانال‌ها شود؛ آن‌وقت دوباره آزاد می‌شود.
# اگر پاداش هنوز اصلاً آزاد نشده بود (status='pending', چون هنوز خرید
# واجدشرایط نکرده)، این تابع کاری نمی‌کند — آن از قبل قفل است.
# ---------------------------------------------------------------------------
def user_has_referral_earnings(user_id: int) -> bool:
    """آیا این کاربر تا الان (به‌عنوان معرف) حداقل یک بار پاداش رفرال واقعاً
    به کیف‌پولش آزاد شده؟ (status IN ('completed','held') یعنی حداقل یک‌بار
    واقعاً پول جابه‌جا شده، حتی اگر الان دوباره قفل باشد). چون کیف‌پول یک
    موجودی واحد و قابل‌اختلاط است، این ساده‌ترین و صادقانه‌ترین معیار برای
    «این کاربر پول رفرالی در کیف‌پولش دارد/داشته» است."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT 1 FROM referrals WHERE referrer_id = ? AND status IN (\'completed\',\'held\') LIMIT 1",
        (user_id,),
    )
    return _fetchone(cur) is not None


def mark_latest_config_referral_funded(user_id: int, config_type: str) -> None:
    """سرویس تازه‌ساخته‌شده را فقط وقتی که caller مطمئن است کل خرید از
    موجودی رفرالی تأمین شده، به‌عنوان referral-funded علامت می‌زند."""
    with transaction() as cur:
        cur.execute(
            "SELECT id FROM configs WHERE user_id = ? AND type = ? AND deleted = 0 ORDER BY id DESC LIMIT 1",
            (user_id, config_type),
        )
        row = _fetchone(cur)
        if row:
            cur.execute("UPDATE configs SET referral_funded = 1 WHERE id = ?", (row["id"],))


def set_config_referral_funded(config_id: int, funded: bool) -> None:
    """منشأ مالی نسخه‌ی فعلی سرویس را مشخص می‌کند.
    برای تمدید با پول عادی یا کارت‌به‌کارت باید فلگ قبلی پاک شود؛ در غیر این صورت
    یک سرویس رفرالی که بعداً با پول عادی تمدید شده، اشتباهاً دوباره جریمه می‌شود."""
    with transaction() as cur:
        cur.execute("UPDATE configs SET referral_funded = ? WHERE id = ?", (1 if funded else 0, config_id))


def _is_active_config_row(row: dict, now=None) -> bool:
    """بررسی امن انقضای یک سرویس، بدون وابستگی به فرمت واحد تاریخ."""
    from datetime import datetime
    now = now or datetime.now()
    expiry = str(row.get("expiry") or "").strip()
    if not expiry:
        return True
    for fmt, value in (("%Y-%m-%d %H:%M:%S", expiry[:19]), ("%Y-%m-%d", expiry[:10])):
        try:
            return datetime.strptime(value, fmt) >= now
        except Exception:
            pass
    # تاریخ خراب را فعلاً فعال فرض می‌کنیم؛ پنل هنگام کسر حجم منبع حقیقت است.
    return True


def _repair_referral_funded_config_from_purchase(user_id: int) -> dict | None:
    """اگر یک خرید واقعاً با referral_wallet انجام شده ولی به‌علت تحویل دستی/
    نسخه قدیمی فلگ configs=0 مانده باشد، سرویس ساخته‌شده‌ی همان خرید را یک‌بار
    ترمیم می‌کند. این مسیر عمداً فقط نزدیک‌ترین سرویس بعد از آخرین تراکنش
    [referral-funded] را انتخاب می‌کند تا خریدهای عادی اشتباهاً رفرالی نشوند."""
    from datetime import datetime
    cur = get_connection().cursor()
    tx = None
    try:
        cur.execute(
            "SELECT created_at FROM transactions "
            "WHERE user_id = ? AND type = 'purchase' "
            "AND COALESCE(description, '') LIKE '%[referral-funded]%' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        tx = _fetchone(cur)
    except Exception:
        return None
    if not tx or not tx.get("created_at"):
        return None

    tx_time = str(tx["created_at"])[:19]
    cur.execute(
        "SELECT * FROM configs WHERE user_id = ? AND deleted = 0 AND referral_funded = 0 "
        "AND datetime(created_at) >= datetime(?) "
        "AND datetime(created_at) <= datetime(?, '+24 hours') "
        "ORDER BY ABS(strftime('%s', created_at) - strftime('%s', ?)) ASC, id DESC LIMIT 1",
        (user_id, tx_time, tx_time, tx_time),
    )
    cfg = _fetchone(cur)
    if not cfg:
        return None
    if not _is_active_config_row(cfg):
        return None
    try:
        with transaction() as wcur:
            wcur.execute("UPDATE configs SET referral_funded = 1 WHERE id = ? AND referral_funded = 0", (cfg["id"],))
    except Exception:
        return None
    cfg["referral_funded"] = 1
    return cfg


def get_referral_funded_active_config(user_id: int) -> dict | None:
    """سرویس فعالِ رفرالی را پیدا می‌کند. اگر فلگ سرویس به هر دلیل جا افتاده باشد
    (مثلاً سفارش با پرداخت کیف‌پول انجام شده ولی لینک توسط ادمین دستی تحویل شده)،
    از تراکنش قطعی [referral-funded] خودکار بازیابی می‌شود."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM configs WHERE user_id = ? AND referral_funded = 1 AND deleted = 0 ORDER BY id DESC",
        (user_id,),
    )
    for row in _fetchall(cur):
        if _is_active_config_row(row):
            return row

    # 🛠 self-heal: فلگ 0 ولی منشأ مالی خرید واقعاً رفرالی بوده است.
    return _repair_referral_funded_config_from_purchase(user_id)


def order_is_referral_funded(order_id: int) -> bool:
    """آیا سفارش عادی با تراکنش کیف‌پولِ کاملاً رفرالی ساخته شده است؟"""
    cur = get_connection().cursor()
    cur.execute("SELECT user_id, created_at FROM orders WHERE id = ?", (order_id,))
    order = _fetchone(cur)
    if not order:
        return False
    cur.execute(
        "SELECT 1 FROM transactions WHERE user_id = ? AND type = 'purchase' "
        "AND COALESCE(description, '') LIKE '%[referral-funded]%' "
        "AND datetime(created_at) BETWEEN datetime(?, '-2 minutes') AND datetime(?, '+15 minutes') "
        "ORDER BY id DESC LIMIT 1",
        (order["user_id"], order["created_at"], order["created_at"]),
    )
    return _fetchone(cur) is not None


def custom_order_is_referral_funded(order_id: int) -> bool:
    """نسخه‌ی همان provenance برای سفارش‌های «بساز سرویس خودت»."""
    cur = get_connection().cursor()
    cur.execute("SELECT user_id, created_at FROM custom_orders WHERE id = ?", (order_id,))
    order = _fetchone(cur)
    if not order:
        return False
    cur.execute(
        "SELECT 1 FROM transactions WHERE user_id = ? AND type = 'purchase' "
        "AND COALESCE(description, '') LIKE '%[referral-funded]%' "
        "AND datetime(created_at) BETWEEN datetime(?, '-2 minutes') AND datetime(?, '+15 minutes') "
        "ORDER BY id DESC LIMIT 1",
        (order["user_id"], order["created_at"], order["created_at"]),
    )
    return _fetchone(cur) is not None


def apply_referral_wallet_penalty(invited_telegram_id) -> dict | None:
    """جریمه‌ی رفرال (فقط روی پول/سرویس رفرالی معرف):
      ۱) اگر «کیف پول رفرالی» او (min(referral_wallet, wallet)) حداقل به‌اندازه‌ی مبلغ جریمه باشد، فقط
         همان مبلغ از همان کسر می‌شود (پول عادی هرگز دست نمی‌خورد).
      ۲) در غیر این صورت (موجودی رفرالی کافی نیست)، اگر سرویس فعالی دارد که خریدش کاملاً از پول رفرالی
         بوده، penalty_service_gb گیگابایت از حجم همان سرویس کم می‌شود (سرویس‌های عادی هدف نیستند).
      هر رویداد فقط یکی از این دو را اجرا می‌کند؛ نتیجه همیشه توضیح می‌دهد چه شد (برای اعلان دقیق)."""
    if not referral_enabled():
        return None
    invited = get_user(invited_telegram_id)
    if invited is None or not invited.get("referrer_id"):
        return None
    settings = get_referral_settings()
    referrer_id = invited["referrer_id"]
    amount = int(settings["penalty_wallet_amount"] or 0)
    gb_penalty = int(settings["penalty_service_gb"] or 0)
    out = {
        "referrer_id": referrer_id, "deducted": 0, "requested": amount,
        "service_gb_penalty": 0, "service_config_id": None, "has_referral_service": False,
        "referral_available": 0, "gb_setting": gb_penalty, "mode": "none",
    }
    if amount <= 0 and gb_penalty <= 0:
        return out
    if amount > 0:
        with transaction() as cur:
            cur.execute("SELECT wallet, referral_wallet FROM users WHERE id = ?", (referrer_id,))
            row = _fetchone(cur)
            wallet = int(row["wallet"] or 0) if row else 0
            avail = min(int((row or {}).get("referral_wallet") or 0), wallet) if row else 0
            out["referral_available"] = avail
            if avail >= amount:
                cur.execute(
                    "UPDATE users SET wallet = wallet - ?, referral_wallet = referral_wallet - ? "
                    "WHERE id = ? AND wallet >= ? AND referral_wallet >= ?",
                    (amount, amount, referrer_id, amount, amount),
                )
                if (cur.rowcount or 0) > 0:
                    out["deducted"] = amount
                    out["mode"] = "wallet"
                    cur.execute(
                        "INSERT INTO transactions (user_id, type, amount, status, description, created_at) VALUES (?, 'referral_penalty', ?, 'completed', ?, ?)",
                        (referrer_id, amount, "جریمه: کاربر دعوت‌شده از کانال اجباری خارج شد (از کیف پول رفرالی)", _now()),
                    )
        if out["mode"] == "wallet":
            _invalidate_user_cache_by_id(referrer_id)
            return out
    if gb_penalty > 0:
        cfg = get_referral_funded_active_config(referrer_id)
        if cfg:
            out.update({"service_gb_penalty": gb_penalty, "service_config_id": cfg.get("id"),
                        "has_referral_service": True, "mode": "service"})
    return out


def _ref_pen_key(ref_id) -> str:
    return f"referral_lockpen_{int(ref_id)}"


def _ref_pen_total(ref_id) -> int:
    """جمع جریمه‌هایی که تا الان از «پاداش مسدودشده»ی این رفرال کم شده است."""
    try:
        return max(0, int(get_setting(_ref_pen_key(ref_id), "0") or 0))
    except Exception:
        return 0


def relock_referral_reward_for_invited(invited_telegram_id) -> dict | None:
    """پاداشِ آزادشده را (منهای جریمه‌های قبلی همین رفرال) از کیف پول رفرالی معرف به مسدود برمی‌گرداند.
    فقط وقتی پول واقعاً جابه‌جا شد ref برمی‌گردد (کلید locked_amount = مبلغ قفل‌شده)."""
    if not referral_enabled():
        return None
    invited = get_user(invited_telegram_id)
    if invited is None:
        return None
    with transaction() as cur:
        cur.execute("SELECT * FROM referrals WHERE invited_id = ? AND status = 'completed'", (invited["id"],))
        ref = _fetchone(cur)
        if ref is None:
            return None
        ref = dict(ref)
        reward = max(0, int(ref["reward"] or 0) - _ref_pen_total(ref["id"]))
        if reward <= 0:
            return None
        cur.execute(
            "UPDATE users SET wallet = wallet - ?, locked_wallet = locked_wallet + ?, referral_wallet = referral_wallet - ? "
            "WHERE id = ? AND wallet >= ? AND referral_wallet >= ?",
            (reward, reward, reward, ref["referrer_id"], reward, reward),
        )
        moved = cur.rowcount > 0
        if moved:
            cur.execute("UPDATE referrals SET status = 'held' WHERE id = ?", (ref["id"],))
            cur.execute(
                "INSERT INTO transactions (user_id, type, amount, status, description, created_at) "
                "VALUES (?, 'referral_relock', ?, 'completed', ?, ?)",
                (ref["referrer_id"], reward, "قفل‌شدن موقت پاداش دعوت (کاربر دعوت‌شده از کانال لفت داد)", _now()),
            )
    _invalidate_user_cache_by_id(ref["referrer_id"])
    # اگر پاداش قبلاً خرج شده باشد چیزی قفل نمی‌شود (و وضعیت 'completed' می‌ماند تا با رجوین پولِ دیگری آزاد نشود)
    if not moved:
        return None
    ref["locked_amount"] = reward
    return ref


def penalize_locked_reward(ref: dict, amount: int) -> dict:
    """جریمه‌ی مبلغی از «همان پاداشِ تازه مسدودشده»: از locked_wallet کم می‌شود و با بازگشت دعوت‌شده فقط باقی‌مانده
    (پاداش − جریمه‌ها) آزاد می‌شود. خروجی: {deducted, remaining}."""
    amount = max(0, int(amount or 0))
    remaining = max(0, int(ref.get("locked_amount") or 0))
    take = min(amount, remaining)
    if take <= 0:
        return {"deducted": 0, "remaining": remaining}
    with transaction() as cur:
        cur.execute("UPDATE users SET locked_wallet = locked_wallet - ? WHERE id = ? AND locked_wallet >= ?",
                    (take, ref["referrer_id"], take))
        if (cur.rowcount or 0) == 0:
            return {"deducted": 0, "remaining": remaining}
        cur.execute(
            "INSERT INTO transactions (user_id, type, amount, status, description, created_at) VALUES (?, 'referral_penalty', ?, 'completed', ?, ?)",
            (ref["referrer_id"], take, "جریمه: کاربر دعوت‌شده از کانال اجباری خارج شد (از همان پاداش مسدودشده)", _now()),
        )
    set_setting(_ref_pen_key(ref["id"]), str(_ref_pen_total(ref["id"]) + take))
    _invalidate_user_cache_by_id(ref["referrer_id"])
    return {"deducted": take, "remaining": remaining - take}


def rerelease_referral_reward_for_invited(invited_telegram_id) -> dict | None:
    """وقتی کاربر دعوت‌شده دوباره عضو همه‌ی کانال‌های اجباری شد صدا زده می‌شود؛ پاداش منهای جریمه‌ها آزاد می‌شود
    (ref['released'] = مبلغ آزادشده، ref['penalty_total'] = جمع جریمه‌های کسرشده از همین پاداش)."""
    if not referral_enabled():
        return None
    invited = get_user(invited_telegram_id)
    if invited is None:
        return None
    with transaction() as cur:
        cur.execute("SELECT * FROM referrals WHERE invited_id = ? AND status = 'held'", (invited["id"],))
        ref = _fetchone(cur)
        if ref is None:
            return None
        ref = dict(ref)
        pen = _ref_pen_total(ref["id"])
        reward = max(0, int(ref["reward"] or 0) - pen)
        moved = False
        if reward > 0:
            cur.execute(
                "UPDATE users SET locked_wallet = locked_wallet - ?, wallet = wallet + ?, referral_wallet = referral_wallet + ? "
                "WHERE id = ? AND locked_wallet >= ?",
                (reward, reward, reward, ref["referrer_id"], reward),
            )
            moved = cur.rowcount > 0
        cur.execute("UPDATE referrals SET status = 'completed' WHERE id = ?", (ref["id"],))
        if moved:
            cur.execute(
                "INSERT INTO transactions (user_id, type, amount, status, description, created_at) "
                "VALUES (?, 'referral_release', ?, 'completed', ?, ?)",
                (ref["referrer_id"], reward, "آزادسازی مجدد پاداش دعوت (کاربر دعوت‌شده دوباره عضو کانال‌ها شد)", _now()),
            )
    _invalidate_user_cache_by_id(ref["referrer_id"])
    if not moved:
        return None
    ref["released"] = reward
    ref["penalty_total"] = pen
    return ref

# ---------------------------------------------------------------------------
# 💰 تنظیمات شارژ کیف‌پول (حداقل/حداکثر مبلغ + دکمه‌های مبلغ پیشنهادی) —
# مثل تنظیمات رفرال بالا، در دیتابیس ذخیره می‌شود و از پنل ادمین («ℹ️ اطلاعات
# ربات» ← «💳 مدیریت شارژ کیف‌پول») بدون نیاز به ری‌دیپلوی قابل تغییر است.
# مقادیر config.py (MIN_WALLET_TOPUP / MAX_WALLET_TOPUP) فقط fallback اولیه‌اند.
# ---------------------------------------------------------------------------
WALLET_SETTING_PREFIX = "wallet_"
_DEFAULT_WALLET_QUICK_AMOUNTS = [50000, 100000, 200000]

def get_wallet_settings() -> dict:
    from config import MIN_WALLET_TOPUP, MAX_WALLET_TOPUP
    min_topup = int(get_setting(WALLET_SETTING_PREFIX + "min_topup", str(MIN_WALLET_TOPUP)) or MIN_WALLET_TOPUP)
    max_topup = int(get_setting(WALLET_SETTING_PREFIX + "max_topup", str(MAX_WALLET_TOPUP)) or MAX_WALLET_TOPUP)
    raw_amounts = get_setting(WALLET_SETTING_PREFIX + "quick_amounts", "")
    quick_amounts = _DEFAULT_WALLET_QUICK_AMOUNTS
    if raw_amounts:
        try:
            parsed = [int(x) for x in raw_amounts.split(",") if x.strip()]
            if parsed:
                quick_amounts = parsed
        except Exception:
            pass
    try:
        low_balance = max(0, int(get_setting(WALLET_SETTING_PREFIX + "low_balance", "0") or 0))
    except Exception:
        low_balance = 0
    return {"min_topup": min_topup, "max_topup": max_topup, "quick_amounts": quick_amounts,
            "gift_percent": wallet_gift_percent(), "low_balance": low_balance}


_CARD_OPTION_DEFAULTS = {"show_copy_card": True, "show_copy_amount": True, "forward_lock": False, "random_amount": True}


def card_option(name: str) -> bool:
    """تنظیمات کارت‌به‌کارت (pm_card): کپی کارت/مبلغ، قفل فوروارد، رندوم‌سازی مبلغ."""
    import json as _json
    try:
        raw = _json.loads(get_setting("pm_card", "{}") or "{}")
    except Exception:
        raw = {}
    return bool(raw.get(name, _CARD_OPTION_DEFAULTS.get(name, False)))


def wallet_gift_percent() -> float:
    """درصد هدیه‌ی شارژ کیف پول (منبع واحد: pm_wallet.gift_percent؛ هم ربات هم مینی‌اپ ادمین همین را می‌نویسند)."""
    import json as _json
    try:
        v = float((_json.loads(get_setting("pm_wallet", "{}") or "{}")).get("gift_percent", 0) or 0)
    except Exception:
        v = 0.0
    return max(0.0, min(100.0, v))


def wallet_gift_bonus(amount: int) -> int:
    """مبلغ هدیه‌ی یک شارژ (تومان، گرد به پایین)."""
    try:
        return max(0, int(int(amount) * wallet_gift_percent() / 100.0))
    except Exception:
        return 0


def credit_charge_bonus(user_id: int, amount: int) -> int:
    """هدیه‌ی شارژ را (اگر درصد > ۰ باشد) به کیف پول اضافه می‌کند و مبلغ هدیه را برمی‌گرداند."""
    bonus = wallet_gift_bonus(amount)
    if bonus > 0:
        add_to_wallet(user_id, bonus, f"🎁 هدیه‌ی شارژ ({wallet_gift_percent():g}٪)", tx_type="gift")
    return bonus

def set_wallet_setting(key: str, value) -> None:
    if key == "quick_amounts":
        # لیستی از اعداد مثبت، صعودی مرتب و بدون تکرار ذخیره می‌شود.
        amounts = sorted({int(v) for v in value if int(v) > 0})
        if not amounts:
            raise ValueError("حداقل یک مبلغ معتبر لازم است")
        set_setting(WALLET_SETTING_PREFIX + key, ",".join(str(v) for v in amounts))
        return
    if key not in {"min_topup", "max_topup", "low_balance"}:
        raise ValueError("تنظیم کیف‌پول نامعتبر است")
    set_setting(WALLET_SETTING_PREFIX + key, str(max(0, int(value))))


def get_setting(key: str, default: str | None = None) -> str | None:
    # fix/perf: این تابع تقریباً روی هر ساخت پیام/کیبورد (متن‌های ربات،
    # کانال‌های عضویت اجباری، تنظیمات custom-build و...) صدا زده می‌شود؛ وقتی
    # DATABASE_PATH روی Turso (دیتابیس ابری) باشد، هر صدا زدن یعنی یک
    # round-trip شبکه‌ی واقعی. چون این مقادیر خیلی کم تغییر می‌کنن، نتیجه رو
    # چند ثانیه کش می‌کنیم تا سرعت پاسخ‌گویی ربات چند برابر بشه.
    cache_key = f"setting:{key}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value if cached_value is not None else default
    cur = get_connection().cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    value = row[0] if row is not None else None
    cache.set(cache_key, value, ttl=300)
    return value if value is not None else default


def set_setting(key: str, value: str):
    with transaction() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    cache.set(f"setting:{key}", value)


# ---------------------------------------------------------------------------
# 💾 FSM پایدار (state/data مکالمه‌ی چندمرحله‌ای) — پشتیبان fsm_storage.py
# ---------------------------------------------------------------------------
def fsm_get_state(storage_key: str) -> str | None:
    cur = get_connection().cursor()
    cur.execute("SELECT state FROM fsm_storage WHERE storage_key = ?", (storage_key,))
    row = cur.fetchone()
    return row[0] if row else None


def fsm_set_state(storage_key: str, state: str | None):
    with transaction() as cur:
        if state is None:
            cur.execute(
                "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, NULL, '{}') "
                "ON CONFLICT(storage_key) DO UPDATE SET state = NULL",
                (storage_key,),
            )
        else:
            cur.execute(
                "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, ?, '{}') "
                "ON CONFLICT(storage_key) DO UPDATE SET state = excluded.state",
                (storage_key, state),
            )


def fsm_get_data(storage_key: str) -> str | None:
    cur = get_connection().cursor()
    cur.execute("SELECT data FROM fsm_storage WHERE storage_key = ?", (storage_key,))
    row = cur.fetchone()
    return row[0] if row else None


def fsm_set_data(storage_key: str, data_json: str):
    with transaction() as cur:
        cur.execute(
            "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, NULL, ?) "
            "ON CONFLICT(storage_key) DO UPDATE SET data = excluded.data",
            (storage_key, data_json),
        )

def fsm_get_all() -> list:
    """همه‌ی FSM state/data‌ها را یک‌جا برمی‌گرداند (برای warm-up هیبرید)."""
    cur = get_connection().cursor()
    cur.execute("SELECT storage_key, state, data FROM fsm_storage")
    return _fetchall(cur)




def is_orders_enabled() -> bool:
    return get_setting("orders_enabled", "1") != "0"


def set_orders_enabled(enabled: bool):
    set_setting("orders_enabled", "1" if enabled else "0")


def get_configs(user_id: int, include_deleted: bool = False) -> list[dict]:
    cur = get_connection().cursor()
    if include_deleted:
        cur.execute("SELECT * FROM configs WHERE user_id = ? ORDER BY id DESC", (user_id,))
    else:
        cur.execute(
            "SELECT * FROM configs WHERE user_id = ? AND deleted = 0 ORDER BY id DESC", (user_id,)
        )
    return _fetchall(cur)


# 🔎 جستجوی سرویس — طبق درخواست، مشابه «سرویس‌های من» ولی روی همه‌ی
# سرویس‌های کاربر (نه فقط VIP) با تطبیق زیررشته روی نام سرویس جست‌وجو می‌کند
# (کوچک/بزرگ حروف انگلیسی نادیده گرفته می‌شود). حداکثر ۲۰ نتیجه برمی‌گردد.
def search_user_configs(user_id: int, query: str, limit: int = 20) -> list[dict]:
    """جستجوی واقعی بین نام سرویس (service_id) و نام پلن.

    نامی که کاربر در لیست «سرویس‌های من» می‌بیند از service_id می‌آید، نه
    از plan؛ بنابراین جستجو باید روی هر دو ستون انجام شود و نسبت به حروف
    انگلیسی حساس نباشد.
    """
    query = (query or "").strip()
    if not query:
        return []
    cur = get_connection().cursor()
    pattern = f"%{query}%"
    cur.execute(
        "SELECT * FROM configs "
        "WHERE user_id = ? AND deleted = 0 "
        "AND (LOWER(COALESCE(service_id, '')) LIKE LOWER(?) "
        "     OR LOWER(COALESCE(plan, '')) LIKE LOWER(?)) "
        "ORDER BY id DESC LIMIT ?",
        (user_id, pattern, pattern, limit),
    )
    return _fetchall(cur)



def get_all_configs_for_admin_search(include_deleted: bool = True) -> list[dict]:
    """FIX 19: برگرداندن همه کانفیگ‌ها برای جستجوی ادمین."""
    cur = get_connection().cursor()
    if include_deleted:
        cur.execute(
            "SELECT c.*, u.telegram_id AS owner_tid, u.name AS owner_name "
            "FROM configs c LEFT JOIN users u ON u.id = c.user_id ORDER BY c.id DESC"
        )
    else:
        cur.execute(
            "SELECT c.*, u.telegram_id AS owner_tid, u.name AS owner_name "
            "FROM configs c LEFT JOIN users u ON u.id = c.user_id "
            "WHERE c.deleted = 0 ORDER BY c.id DESC"
        )
    return _fetchall(cur)


def get_agent_plan_keys(telegram_id) -> list[str]:
    """FIX 4: پلن‌های تخفیف‌خور هر نماینده."""
    agent = get_agent(telegram_id) or {}
    raw = str(agent.get('vip_plan_keys') or '').strip()
    return [k for k in raw.split(',') if k.strip()] if raw else []


def set_agent_plan_keys(telegram_id, plan_keys: list) -> None:
    """FIX 4: تنظیم پلن‌های تخفیف‌خور هر نماینده."""
    raw = ','.join(str(k).strip() for k in (plan_keys or []) if str(k).strip())
    with transaction() as cur:
        cur.execute("UPDATE agents SET vip_plan_keys=? WHERE telegram_id=?", (raw, str(telegram_id)))


def agent_discount_applies_to_plan(telegram_id, plan_key: str) -> bool:
    """FIX 4: بررسی تخفیف پلن خاص."""
    keys = get_agent_plan_keys(telegram_id)
    return not keys or plan_key in keys


def get_agent_plan_percent_override(telegram_id, plan_key: str):
    """تخفیف اختصاصی یک پلن برای نماینده؛ None یعنی همان درصد پیش‌فرض نماینده."""
    raw = get_setting(f"agentpp_{telegram_id}_{plan_key}", "")
    if raw in (None, ""):
        return None
    try:
        return max(0, min(100, int(float(raw))))
    except Exception:
        return None


def set_agent_plan_percent(telegram_id, plan_key: str, percent) -> None:
    set_setting(f"agentpp_{telegram_id}_{plan_key}", "" if percent is None else str(int(percent)))


def agent_percent_for_plan(telegram_id, plan_key, category_id=None) -> int:
    """درصد تخفیف مؤثر نماینده روی یک پلن VIP (۰ یعنی مشمول نیست)."""
    agent = get_agent(telegram_id)
    if not agent:
        return 0
    if category_id is not None and not agent_discount_applies_to_category(telegram_id, category_id):
        return 0
    if plan_key and not agent_discount_applies_to_plan(telegram_id, plan_key):
        return 0
    ov = get_agent_plan_percent_override(telegram_id, plan_key) if plan_key else None
    return ov if ov is not None else int(agent.get("vip_discount_percent") or 0)


# ---------------------------------------------------------------------------
# 🤝 نماینده: سهمیه‌ی تست ماهانه + تخفیف اختصاصی به‌ازای هر «گزینه‌ی نگاشت»
# (سرویس سفارشی / PAYG). ذخیره در settings: agent_tq_{tid} و agentopt_{tid}_{kind}_{num}
# ---------------------------------------------------------------------------
def agent_test_quota(telegram_id) -> int:
    """۰ = بدون سهمیه (رفتار عادی: هر پلن تست یک‌بار)."""
    try:
        if not get_agent(telegram_id):
            return 0
        return max(0, int(float(get_setting(f"agent_tq_{telegram_id}", "0") or 0)))
    except Exception:
        return 0


def set_agent_test_quota(telegram_id, n) -> None:
    set_setting(f"agent_tq_{telegram_id}", str(max(0, int(n or 0))))


def agent_tests_used_this_month(user_id: int) -> int:
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    cur = get_connection().cursor()
    cur.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ? AND (plan_key = ? OR plan_key LIKE ?) AND created_at >= ?",
        (user_id, FREE_TEST_PLAN_KEY, FT_KEY_PREFIX + "%", since),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def get_agent_option_percent_override(telegram_id, kind: str, num):
    raw = get_setting(f"agentopt_{telegram_id}_{kind}_{int(num or 0)}", "")
    if raw in (None, ""):
        return None
    try:
        return max(0, min(100, int(float(raw))))
    except Exception:
        return None


def set_agent_option_percent(telegram_id, kind: str, num, percent) -> None:
    set_setting(f"agentopt_{telegram_id}_{kind}_{int(num or 0)}", "" if percent is None else str(max(0, min(100, int(percent)))))


def agent_percent_for_option(telegram_id, kind: str, num) -> int:
    """درصد تخفیف نماینده روی یک گزینه‌ی سفارشی/PAYG. اگر اختصاصی نداشته باشد:
    سفارشی → درصد پیش‌فرض نماینده (مثل قبل)؛ PAYG → بدون تخفیف."""
    agent = get_agent(telegram_id)
    if not agent:
        return 0
    ov = get_agent_option_percent_override(telegram_id, kind, num)
    if ov is not None:
        return ov
    if kind == "custom_build":
        if not agent_discount_applies_to_category(telegram_id, None):
            return 0
        return int(agent.get("vip_discount_percent") or 0)
    return 0


def payg_price_for_user(telegram_id, option_num, price: int) -> int:
    """نرخ هر گیگ PAYG بعد از تخفیف نمایندگیِ همان گزینه."""
    try:
        pct = agent_percent_for_option(telegram_id, "payg", option_num)
    except Exception:
        pct = 0
    return int(round(int(price) * (1 - pct / 100))) if pct > 0 else int(price)


def get_payg_settings() -> dict:
    """FIX 7: تنظیمات Pay-as-you-go."""
    return {
        'enabled': get_setting('payg_enabled', '0') == '1',
        'price_per_gb': int(get_setting('payg_price_per_gb', '0') or 0),
        'min_balance': int(get_setting('payg_min_balance', '0') or 0),
        'empty_action': get_setting('payg_empty_action', 'suspend'),
        'empty_message': get_setting('payg_empty_message', ''),
    }


def set_payg_settings(**kwargs) -> None:
    """FIX 7: ذخیره تنظیمات payg."""
    mapping = {
        'enabled': ('payg_enabled', lambda v: '1' if v else '0'),
        'price_per_gb': ('payg_price_per_gb', str),
        'min_balance': ('payg_min_balance', str),
        'empty_action': ('payg_empty_action', str),
        'empty_message': ('payg_empty_message', str),
    }
    for k, v in kwargs.items():
        if k in mapping:
            db_key, transform = mapping[k]
            set_setting(db_key, transform(v))


def get_configs_by_type(user_id: int, config_type: str, include_deleted: bool = False) -> list[dict]:
    cur = get_connection().cursor()
    if include_deleted:
        cur.execute(
            "SELECT * FROM configs WHERE user_id = ? AND type = ? ORDER BY id DESC",
            (user_id, config_type),
        )
    else:
        cur.execute(
            "SELECT * FROM configs WHERE user_id = ? AND type = ? AND deleted = 0 ORDER BY id DESC",
            (user_id, config_type),
        )
    return _fetchall(cur)


def get_config_by_id(config_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM configs WHERE id = ?", (config_id,))
    return _fetchone(cur)


# ---------------------------------------------------------------------------
# 📣 لینک‌های تبلیغاتی (کمپین) — هر تبلیغ یک کد اختصاصی دارد که در لینک
# /start c_<code> استفاده می‌شود. هر کاربر فقط به اولین کمپینی که او را آورده
# بسته می‌شود (first-touch، دقیقاً مثل referrer_id در create_user)، پس لینک
# بعدی که همان کاربر رویش کلیک کند آمار کمپین قبلی را خراب نمی‌کند.
# ---------------------------------------------------------------------------
def normalize_campaign_code(code: str) -> str:
    return "".join(ch for ch in code.strip().upper() if ch.isalnum() or ch == "_")[:32]


def create_campaign(code: str, name: str) -> int:
    code = normalize_campaign_code(code)
    if not code:
        raise ValueError("کد کمپین نامعتبر است")
    with transaction() as cur:
        cur.execute(
            "INSERT INTO ad_campaigns (code, name, is_active, created_at) VALUES (?, ?, 1, ?)",
            (code, name.strip(), _now()),
        )
        return cur.lastrowid


def get_campaign_by_code(code: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM ad_campaigns WHERE code = ?", (normalize_campaign_code(code),))
    return _fetchone(cur)


def get_campaign_by_id(campaign_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM ad_campaigns WHERE id = ?", (campaign_id,))
    return _fetchone(cur)


def list_campaigns() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM ad_campaigns ORDER BY id DESC")
    return _fetchall(cur)


def set_campaign_active(campaign_id: int, is_active: bool) -> None:
    with transaction() as cur:
        cur.execute("UPDATE ad_campaigns SET is_active = ? WHERE id = ?", (1 if is_active else 0, campaign_id))


def rename_campaign(campaign_id: int, name: str) -> None:
    with transaction() as cur:
        cur.execute("UPDATE ad_campaigns SET name = ? WHERE id = ?", (name.strip(), campaign_id))


def delete_campaign(campaign_id: int) -> None:
    """کمپین حذف می‌شود ولی کاربرانی که از طریق آن آمده بودند حذف/دست‌کاری
    نمی‌شوند؛ فقط چسبندگی‌شان به این کمپین پاک می‌شود (campaign_id = NULL)
    تا آماری که قبلاً جمع شده گم نشود ولی هم‌زمان هیچ FOREIGN KEY یا ارجاع
    آویزان (dangling reference) هم باقی نماند."""
    with transaction() as cur:
        cur.execute("UPDATE users SET campaign_id = NULL WHERE campaign_id = ?", (campaign_id,))
        cur.execute("DELETE FROM ad_campaigns WHERE id = ?", (campaign_id,))


def get_campaign_stats(campaign_id: int) -> dict:
    """آمار زنده‌ی یک کمپین: چند نفر از این لینک عضو شدند، چند نفرشان حداقل
    یک خرید واقعی (نه شارژ کیف‌پول) تکمیل کردند، و رویهم چقدر خرج کردند."""
    cur = get_connection().cursor()
    cur.execute("SELECT id FROM users WHERE campaign_id = ?", (campaign_id,))
    user_ids = [r["id"] for r in _fetchall(cur)]
    members = len(user_ids)
    if not user_ids:
        return {"members": 0, "converted": 0, "total_spent": 0}
    placeholders = ",".join("?" * len(user_ids))
    cur.execute(
        f"SELECT DISTINCT user_id FROM orders WHERE user_id IN ({placeholders}) "
        f"AND order_type != 'wallet_charge' AND status IN ('paid','processing','completed')",
        user_ids,
    )
    converted_from_orders = {r["user_id"] for r in _fetchall(cur)}
    cur.execute(
        f"SELECT DISTINCT user_id FROM custom_orders WHERE user_id IN ({placeholders}) "
        f"AND status IN ('paid','processing')",
        user_ids,
    )
    converted_from_custom = {r["user_id"] for r in _fetchall(cur)}
    converted = len(converted_from_orders | converted_from_custom)
    cur.execute(
        f"SELECT COALESCE(SUM(price), 0) AS s FROM orders WHERE user_id IN ({placeholders}) "
        f"AND order_type != 'wallet_charge' AND status IN ('paid','processing','completed')",
        user_ids,
    )
    spent_orders = _fetchone(cur)["s"]
    cur.execute(
        f"SELECT COALESCE(SUM(price), 0) AS s FROM custom_orders WHERE user_id IN ({placeholders}) "
        f"AND status IN ('paid','processing')",
        user_ids,
    )
    spent_custom = _fetchone(cur)["s"]
    return {"members": members, "converted": converted, "total_spent": spent_orders + spent_custom}


# ---------------------------------------------------------------------------
# 🎫 توابع سیستم تیکت — هر پیام کاربر اگر تیکت باز داشته باشد به همان تیکت
# اضافه می‌شود (نه یک تیکت جدید جدا برای هر پیام)، تا مکالمه یک‌جا و پیوسته
# بماند. هر پاسخ ادمین وضعیت را 'answered' می‌کند؛ پیام بعدی کاربر روی همان
# تیکت وضعیتش را دوباره 'open' می‌کند (یعنی «هنوز نیاز به توجه ادمین دارد»).
# ---------------------------------------------------------------------------
def get_or_create_open_ticket(user_id: int, subject: str | None = None) -> dict:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM tickets WHERE user_id = ? AND status != 'closed' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    existing = _fetchone(cur)
    if existing:
        return existing
    with transaction() as cur:
        cur.execute(
            "INSERT INTO tickets (user_id, subject, status, created_at, updated_at) VALUES (?, ?, 'open', ?, ?)",
            (user_id, subject, _now(), _now()),
        )
        ticket_id = cur.lastrowid
    return get_ticket(ticket_id)


def get_ticket(ticket_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    return _fetchone(cur)


def add_ticket_message(ticket_id: int, sender: str, text: str) -> None:
    """sender: 'user' یا 'admin'. پیام کاربر تیکت را 'open' می‌کند (نیاز به
    پاسخ دارد)، پیام ادمین آن را 'answered' می‌کند."""
    new_status = "open" if sender == "user" else "answered"
    with transaction() as cur:
        cur.execute(
            "INSERT INTO ticket_messages (ticket_id, sender, text, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, sender, text, _now()),
        )
        cur.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (new_status, _now(), ticket_id))
    if sender == "user":
        _adv_hook("ticket_message", int(ticket_id), text)


def get_ticket_messages(ticket_id: int) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,))
    return _fetchall(cur)


def list_tickets(status: str | None = None, limit: int = 10, offset: int = 0) -> list[dict]:
    """لیست تیکت‌ها برای پنل ادمین، جدیدترین (بر اساس آخرین فعالیت) بالاتر.
    نام کاربر هم برای نمایش راحت‌تر join می‌شود."""
    cur = get_connection().cursor()
    if status and status != "all":
        cur.execute(
            "SELECT t.*, u.name AS user_name, u.telegram_id AS user_telegram_id "
            "FROM tickets t LEFT JOIN users u ON u.id = t.user_id "
            "WHERE t.status = ? ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        cur.execute(
            "SELECT t.*, u.name AS user_name, u.telegram_id AS user_telegram_id "
            "FROM tickets t LEFT JOIN users u ON u.id = t.user_id "
            "ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return _fetchall(cur)


def count_tickets(status: str | None = None) -> int:
    cur = get_connection().cursor()
    if status and status != "all":
        cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status = ?", (status,))
    else:
        cur.execute("SELECT COUNT(*) AS c FROM tickets")
    return _fetchone(cur)["c"]


def set_ticket_status(ticket_id: int, status: str) -> None:
    if status not in ("open", "answered", "closed"):
        raise ValueError("وضعیت تیکت نامعتبر است")
    with transaction() as cur:
        cur.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), ticket_id))


def list_user_tickets(user_id: int, limit: int = 20) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM tickets WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    return _fetchall(cur)


def user_has_completed_purchase(user_id: int) -> bool:
    """آیا این کاربر تا الان حداقل یک خرید واقعیِ محصول (نه شارژ کیف‌پول و
    نه تست رایگان) در فروشگاه داشته؟

    🐛 فیکس: قبلاً فقط 'wallet_charge' کنار گذاشته می‌شد، یعنی یک اکانت
    تستِ رایگان هم «خرید» حساب می‌شد — هم برای «تخفیف اولین خرید» (بند ۱۴)
    و هم حالا برای دسته‌بندی «خریداران قبلی» کد تخفیف اشتباه بود. طبق
    تأکید صریح («تست قبول نیست»)، اینجا order_type='test' هم کنار گذاشته شد.
    """
    cur = get_connection().cursor()
    cur.execute(
        "SELECT 1 FROM orders WHERE user_id = ? AND order_type NOT IN ('wallet_charge', 'test') "
        "AND status IN ('paid','processing','completed') LIMIT 1",
        (user_id,),
    )
    if _fetchone(cur):
        return True
    cur.execute(
        "SELECT 1 FROM custom_orders WHERE user_id = ? AND status IN ('paid','processing') LIMIT 1",
        (user_id,),
    )
    return _fetchone(cur) is not None


def discount_allowed_for_first_purchase(discount: dict, user_id: int) -> bool:
    """اگر این کد «مخصوص اولین خرید» باشد، فقط برای کاربری مجاز است که هیچ
    خرید تکمیل‌شده‌ی قبلی نداشته باشد."""
    if not discount.get("first_purchase_only"):
        return True
    return not user_has_completed_purchase(user_id)


def get_discount(code: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM discounts WHERE code = ?", (code.upper(),))
    return _fetchone(cur)


def create_discount(
    code: str,
    percent: int = 0,
    uses: int = 1,
    discount_type: str = "percent",
    amount: int = 0,
    applicable_plans: list | None = None,
    min_order_amount: int = 0,
    max_uses_per_user: int = 0,
    expires_at: str | None = None,
    allowed_user_ids: list | None = None,
    first_purchase_only: bool = False,
):
    """
    discount_type: 'percent' یا 'amount' (تخفیف درصدی یا مبلغ ثابت تومانی).
    applicable_plans: لیستی از plan_key ها که این کد رویشان اعمال می‌شود؛
                       None یا [] یعنی روی همه‌ی پلن‌ها قابل استفاده است.
    max_uses_per_user: 0 یعنی بدون محدودیت برای هر کاربر.
    expires_at: تاریخ/زمان انقضا به‌فرمت 'YYYY-MM-DD HH:MM:SS'؛ None یعنی بدون انقضا.
    allowed_user_ids: لیستی از آیدی عددی تلگرام که مجاز به استفاده از این کدند؛
                       None یا [] یعنی همه‌ی کاربران مجازند.
    first_purchase_only: اگر True باشد، فقط برای کاربری که تا حالا هیچ
                       خرید تکمیل‌شده‌ای در فروشگاه نداشته قابل استفاده است.
    """
    plans_json = json.dumps(applicable_plans) if applicable_plans else None
    users_json = json.dumps([str(u) for u in allowed_user_ids]) if allowed_user_ids else None
    with transaction() as cur:
        cur.execute(
            """INSERT INTO discounts
               (code, percent, uses, created_at, discount_type, amount,
                applicable_plans, min_order_amount, max_uses_per_user, expires_at, allowed_user_ids,
                first_purchase_only)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code.upper(), percent, uses, _now(), discount_type, amount,
             plans_json, min_order_amount, max_uses_per_user, expires_at, users_json,
             1 if first_purchase_only else 0),
        )


def _discount_plans(discount: dict) -> list | None:
    raw = discount.get("applicable_plans")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def discount_plans(discount: dict) -> list | None:
    """نسخه‌ی public از _discount_plans برای استفاده‌ی بیرون از ماژول."""
    return _discount_plans(discount)


DISCOUNT_TOKEN_CUSTOM = "__custom__"   # سفارش «بساز سرویس خودت» (جدید)
DISCOUNT_TOKEN_RENEW = "__renew__"     # تمدید سرویس
DISCOUNT_TOKEN_WALLET = "__wallet__"   # شارژ کیف پول (فقط وقتی صریحاً انتخاب شده باشد)


def _category_ids_with_descendants(cat_id: int) -> set:
    out, stack = set(), [int(cat_id)]
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.add(cid)
        try:
            stack.extend(int(c["id"]) for c in get_child_vip_categories(cid))
        except Exception:
            pass
    return out


def discount_applies_to_plan(discount: dict, plan_key: str | None) -> bool:
    """None/خالی = همه. توکن‌ها: کلید پلن، «cat:<id>» (دسته و زیردسته‌هایش)، __custom__ و __renew__ (برای پلن ثابت اثری ندارند)."""
    plans = _discount_plans(discount)
    if not plans:
        return True
    if plan_key in plans:
        return True
    cats = [t for t in plans if isinstance(t, str) and t.startswith("cat:")]
    if cats and plan_key:
        try:
            _p = get_vip_plan(plan_key)
            pcid = int(_p["category_id"]) if _p and _p.get("category_id") else None
            if pcid is not None:
                for t in cats:
                    if pcid in _category_ids_with_descendants(int(t[4:])):
                        return True
        except Exception:
            pass
    return False


def discount_applies_to_scope(discount: dict, scope: str) -> bool:
    """scope: 'custom' (سفارشی جدید) یا 'renew' (تمدید). کد بدون محدودیت روی همه اعمال می‌شود."""
    plans = _discount_plans(discount)
    if not plans:
        return True
    return f"__{scope}__" in plans


def discount_applies_to_wallet(discount: dict) -> bool:
    """کد تخفیف شارژ کیف پول فقط وقتی «شارژ کیف پول» صریحاً در محدوده‌ی کد انتخاب شده باشد؛
    کد «همه (بدون محدودیت)» روی شارژ کیف پول اعمال نمی‌شود تا ناخواسته شارژها تخفیف نخورند."""
    plans = _discount_plans(discount)
    return bool(plans) and DISCOUNT_TOKEN_WALLET in plans


def parse_wallet_discount_extra(extra) -> int | None:
    """extra رسید/فاکتور شارژ با تخفیف: «wd:<مبلغ شارژ‌شده>»؛ در غیر این صورت None."""
    s = str(extra or "")
    if s.startswith("wd:") and s[3:].isdigit():
        return int(s[3:])
    return None


def apply_wallet_discount_credit(user_id: int, paid: int, code: str | None, extra) -> int:
    """بعد از شارژ مبلغ پرداخت‌شده، مابه‌التفاوت تخفیف را هم به کیف پول اضافه می‌کند تا کل مبلغ اصلی شارژ شود
    (مثلاً ۴۰۰ پرداخت‌شده + ۱۰۰ تخفیف = ۵۰۰). مصرف کد هم همین‌جا ثبت می‌شود. خروجی: مبلغ کل شارژ‌شده (پرداخت + تخفیف)."""
    credit = parse_wallet_discount_extra(extra)
    paid = int(paid)
    if not code or credit is None or credit <= paid:
        return paid
    bonus = credit - paid
    add_to_wallet(user_id, bonus, f"🎟 تخفیف شارژ ({code})", tx_type="gift")
    try:
        use_discount(code, user_id)
    except Exception:
        pass
    return credit


def _discount_allowed_users(discount: dict) -> list | None:
    """لیستی از آیدی‌های عددی (به‌صورت رشته) که مجاز به استفاده‌اند؛ None یعنی بدون محدودیت."""
    raw = discount.get("allowed_user_ids")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if parsed else None
    except Exception:
        return None


def discount_allowed_for_user(discount: dict, telegram_id) -> bool:
    """بررسی می‌کند که این کد برای این آیدی عددی تلگرام خاص مجاز است یا خیر.

    🆕 دسته‌بندی مخاطب (مشابه پیام همگانی):
      • all        → برای همه (پیش‌فرض، رفتار قبلی وقتی allowed_user_ids خالی بود)
      • specific   → فقط آیدی‌های داخل allowed_user_ids (رفتار اصلیِ قبلی)
      • buyers     → فقط کسانی که حداقل یک خرید واقعی قبلی دارند (تست حساب نمی‌شود)
      • non_buyers → فقط کسانی که هیچ خرید واقعی‌ای نداشته‌اند
    """
    audience = discount.get("audience") or "all"
    if audience == "specific":
        allowed = _discount_allowed_users(discount)
        if not allowed:
            return True
        return str(telegram_id) in allowed
    if audience in ("buyers", "non_buyers"):
        user = get_user(telegram_id)
        has_purchase = user_has_completed_purchase(user["id"]) if user else False
        return has_purchase if audience == "buyers" else not has_purchase
    if audience in AUDIENCE_LABELS and audience != "all":
        try:
            return user_in_audience(telegram_id, audience)
        except Exception:
            return False
    # audience == "all" (یا هر مقدار ناشناخته‌ی دیگر، برای سازگاری با عقب)
    return True


def discount_is_expired(discount: dict) -> bool:
    exp = discount.get("expires_at")
    if not exp:
        return False
    return _now() > exp


def user_discount_uses(discount_id: int, user_id: int) -> int:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM discount_usages WHERE discount_id = ? AND user_id = ?",
        (discount_id, user_id),
    )
    return _fetchone(cur)["c"]


def compute_discount(discount: dict, price: int) -> int:
    """قیمت نهایی بعد از اعمال کد تخفیف (درصدی یا مبلغ ثابت) را برمی‌گرداند."""
    if discount.get("discount_type") == "amount":
        final_price = price - discount.get("amount", 0)
    else:
        final_price = int(round(price * (1 - discount.get("percent", 0) / 100)))
    return max(final_price, 0)


def use_discount(code: str, user_id: int | None = None) -> bool:
    """مصرف اتمیک کد تخفیف؛ هرگز uses را منفی نمی‌کند و محدودیت هر کاربر
    را داخل همان تراکنش کنترل می‌کند تا ربات و Mini App نتوانند هم‌زمان آن
    را دور بزنند."""
    with transaction() as cur:
        cur.execute("SELECT * FROM discounts WHERE code = ?", (code.upper(),))
        row = _fetchone(cur)
        if row is None or row["uses"] <= 0:
            return False
        if user_id is not None:
            max_per_user = int(row.get("max_uses_per_user") or 0)
            if max_per_user > 0:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM discount_usages WHERE discount_id = ? AND user_id = ?",
                    (row["id"], user_id),
                )
                if _fetchone(cur)["c"] >= max_per_user:
                    return False
        cur.execute(
            "UPDATE discounts SET uses = uses - 1 WHERE id = ? AND uses > 0",
            (row["id"],),
        )
        if (cur.rowcount or 0) == 0:
            return False
        if user_id is not None:
            cur.execute(
                "INSERT INTO discount_usages (discount_id, user_id, used_at) VALUES (?, ?, ?)",
                (row["id"], user_id, _now()),
            )
    if user_id is not None:
        _adv_hook("discount_used", code.upper(), user_id)
    return True


def get_all_discounts() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM discounts ORDER BY id DESC")
    return _fetchall(cur)


def get_discount_by_id(discount_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM discounts WHERE id = ?", (discount_id,))
    return _fetchone(cur)


_DISCOUNT_EDITABLE_FIELDS = {
    "percent", "amount", "discount_type", "uses", "min_order_amount",
    "max_uses_per_user", "expires_at", "applicable_plans", "allowed_user_ids",
    "first_purchase_only", "audience",
}


def update_discount(discount_id: int, **fields):
    """آپدیت جزئی یک کد تخفیف موجود؛ فقط کلیدهای مجاز در _DISCOUNT_EDITABLE_FIELDS پذیرفته می‌شوند.
    applicable_plans و allowed_user_ids باید لیست (یا None) باشند و اینجا خودکار به JSON تبدیل می‌شوند."""
    sets = []
    values = []
    for key, value in fields.items():
        if key not in _DISCOUNT_EDITABLE_FIELDS:
            continue
        if key in ("applicable_plans", "allowed_user_ids"):
            if key == "allowed_user_ids" and value:
                value = json.dumps([str(v) for v in value])
            else:
                value = json.dumps(value) if value else None
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return
    values.append(discount_id)
    with transaction() as cur:
        cur.execute(f"UPDATE discounts SET {', '.join(sets)} WHERE id = ?", values)


def delete_discount(code: str):
    # 🐛 همان فیکس بالا: قبل از حذف کد، سابقه‌ی مصرفش را هم پاک می‌کنیم تا به
    # FOREIGN KEY برنخورد.
    with transaction() as cur:
        cur.execute(
            "DELETE FROM discount_usages WHERE discount_id = (SELECT id FROM discounts WHERE code = ?)",
            (code.upper(),),
        )
        cur.execute("DELETE FROM discounts WHERE code = ?", (code.upper(),))


def delete_discount_by_id(discount_id: int):
    # 🐛 فیکس: اگر کد تخفیف حتی یک‌بار استفاده شده باشد، در جدول
    # discount_usages رکورد وابسته (FOREIGN KEY) دارد و DELETE مستقیم از
    # discounts با «FOREIGN KEY constraint failed» شکست می‌خورد و از دید
    # ادمین یعنی «دکمه‌ی حذف اصلاً کار نمی‌کند». چون حذف کامل یک کد تخفیف
    # یعنی آن پروموشن دیگر برای همیشه کنار گذاشته می‌شود، سابقه‌ی مصرفش هم
    # (که فقط برای اعمال «هر کاربر فقط یک‌بار» همان کد استفاده می‌شد) در همان
    # تراکنش پاک می‌شود.
    with transaction() as cur:
        cur.execute("DELETE FROM discount_usages WHERE discount_id = ?", (discount_id,))
        cur.execute("DELETE FROM discounts WHERE id = ?", (discount_id,))


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — تخفیف ثابت (پیش‌فرض ۵۰٪) روی محصولات VIP برای آیدی‌های خاص
# ---------------------------------------------------------------------------
def add_agent(telegram_id, vip_discount_percent: int = 50, note: str | None = None,
              vip_category_ids=None, custom_prefix: str | None = None):
    telegram_id = str(telegram_id)
    with transaction() as cur:
        cur.execute(
            """INSERT INTO agents (telegram_id, vip_discount_percent, note, created_at, vip_category_ids, custom_prefix)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                    vip_discount_percent = excluded.vip_discount_percent,
                    note = excluded.note,
                    vip_category_ids = CASE WHEN excluded.vip_category_ids != '' THEN excluded.vip_category_ids ELSE agents.vip_category_ids END,
                    custom_prefix = CASE WHEN excluded.custom_prefix != '' THEN excluded.custom_prefix ELSE agents.custom_prefix END""",
            (telegram_id, vip_discount_percent, note, _now(),
             ",".join(str(int(x)) for x in (vip_category_ids or []) if str(x).isdigit()),
             str(custom_prefix or "").strip()),
        )


def get_agent(telegram_id) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM agents WHERE telegram_id = ?", (str(telegram_id),))
    return _fetchone(cur)


def remove_agent(telegram_id):
    with transaction() as cur:
        cur.execute("DELETE FROM agents WHERE telegram_id = ?", (str(telegram_id),))


def get_all_agents() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM agents ORDER BY id DESC")
    return _fetchall(cur)

def get_agent_category_ids(telegram_id) -> list[int]:
    agent = get_agent(telegram_id) or {}
    raw = str(agent.get("vip_category_ids") or "").strip()
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]

def agent_discount_applies_to_category(telegram_id, category_id) -> bool:
    ids = get_agent_category_ids(telegram_id)
    # لیست خالی = همه‌ی دسته‌ها (رفتار سازگار با نسخه‌ی قبلی).
    return not ids or int(category_id or 0) in ids

def set_agent_categories(telegram_id, category_ids) -> None:
    raw = ",".join(str(int(x)) for x in (category_ids or []) if str(x).isdigit())
    with transaction() as cur:
        cur.execute("UPDATE agents SET vip_category_ids=? WHERE telegram_id=?", (raw, str(telegram_id)))

def set_agent_prefix(telegram_id, prefix: str) -> None:
    with transaction() as cur:
        cur.execute("UPDATE agents SET custom_prefix=? WHERE telegram_id=?", (str(prefix or "").strip(), str(telegram_id)))


def get_referral(invited_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM referrals WHERE invited_id = ?", (invited_id,))
    return _fetchone(cur)


def complete_referral(invited_id: int, qualifying_order_id: int | None = None):
    """پاداش دعوت را (در صورت وجود رفرال pending) آزاد می‌کند.

    🐛 فیکس بحرانی: پارامتر qualifying_order_id قبلاً در امضای این تابع وجود
    نداشت، ولی handlers/admin.py هنگام «تأیید رسید کارت‌به‌کارت» آن را
    به‌صورت kwarg پاس می‌داد. چون آنجا فقط `except ValueError` گرفته شده بود
    و این خطا TypeError بود (نه ValueError)، استثنا مهار نمی‌شد و کل هندلر
    تأیید خرید درست بعد از ثبت خرید کرش می‌کرد — یعنی پول کاربر کم می‌شد ولی
    ادمین نه پیام تأیید می‌دید و نه دکمه‌ی «ارسال کانفیگ». این دقیقاً همان
    «تایید و ارسال کانفیگ به مشکل خورده» بود.

    خود شناسه فقط برای رد ممیزی (audit) استفاده می‌شود؛ منطق آزادسازی به آن
    وابسته نیست، پس اختیاری و بدون اثر جانبی اضافه شده است.
    """
    if not referral_enabled():
        return None
    with transaction() as cur:
        cur.execute(
            "SELECT * FROM referrals WHERE invited_id = ? AND status = 'pending'",
            (invited_id,),
        )
        ref = _fetchone(cur)
        if ref is None:
            return

        reward = int(ref["reward"] or 0)

        cur.execute(
            "SELECT locked_wallet FROM users WHERE id = ?", (ref["referrer_id"],)
        )
        referrer_row = _fetchone(cur)
        if referrer_row is None:
            raise ValueError("معرف پیدا نشد.")
        # 🆕 تنظیمات رفرال فقط هنگام «دعوت» روی ردیف ثبت می‌شد؛ اگر ادمین بعد از دعوت مبلغ پاداش را
        # عوض می‌کرد، همچنان مبلغ پیش‌فرض/قدیمی آزاد می‌شد. حالا هنگام آزادسازی مبلغ فعلیِ تنظیمات اعمال
        # می‌شود و موجودی قفل‌شده‌ی معرف هم به‌اندازه‌ی اختلاف اصلاح می‌شود.
        _cur_reward = int(get_referral_settings()["reward_amount"])
        _locked = int(referrer_row["locked_wallet"] or 0)
        if _cur_reward != reward:
            _locked = max(0, _locked + _cur_reward - reward)
            reward = _cur_reward
            cur.execute("UPDATE referrals SET reward = ? WHERE id = ?", (reward, ref["id"]))
            cur.execute("UPDATE users SET locked_wallet = ? WHERE id = ?", (_locked, ref["referrer_id"]))
        if _locked < reward:
            # ناهماهنگی حسابداری (مثلاً قفل قبلاً کم شده): کسری را همین‌جا جبران می‌کنیم تا پاداش گم نشود
            cur.execute("UPDATE users SET locked_wallet = ? WHERE id = ?", (reward, ref["referrer_id"]))

        cur.execute(
            "UPDATE referrals SET status = 'completed' WHERE id = ?",
            (ref["id"],),
        )
        cur.execute(
            "UPDATE users SET successful_invites = successful_invites + 1 WHERE id = ?",
            (ref["referrer_id"],),
        )
        cur.execute(
            "UPDATE users SET locked_wallet = locked_wallet - ?, wallet = wallet + ?, referral_wallet = referral_wallet + ? WHERE id = ?",
            (reward, reward, reward, ref["referrer_id"]),
        )
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'referral_release', ?, 'completed', ?, ?)""",
            (ref["referrer_id"], reward, "آزادسازی پاداش دعوت (خرید واجد شرط فرد دعوت‌شده)", _now()),
        )
    _invalidate_user_cache_by_id(ref["referrer_id"])
    _adv_hook("referral_event", ref["referrer_id"], invited_id, "completed")
    _adv_hook("referral_reward_notice", ref["referrer_id"], invited_id, reward)
    if qualifying_order_id is not None:
        audit_event("system", "referral.released", "referral", str(ref["id"]), f"order={qualifying_order_id}")


def get_referral_stats(user_id: int) -> dict:
    user = get_user_by_id(user_id)
    cur = get_connection().cursor()
    cur.execute(
        "SELECT COALESCE(SUM(reward), 0) AS released FROM referrals WHERE referrer_id = ? AND status = 'completed'",
        (user_id,),
    )
    released = _fetchone(cur)["released"]
    cur.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND status = 'pending'",
        (user_id,),
    )
    pending_count = _fetchone(cur)["c"]
    return {
        "invite_code": user["invite_code"],
        "invited_count": user["invited_count"],
        "successful_invites": user["successful_invites"],
        "released_amount": released,
        "pending_count": pending_count,
    }


def create_custom_order(
    user_id: int, volume_gb: int, days: int, custom_name: str | None, price: int,
    order_type: str = "new", target_config_id: int | None = None, renew_mode: str | None = None,
    payment_method: str | None = None,
    option_num: int | None = 0,
) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO custom_orders
               (user_id, volume_gb, days, custom_name, price, order_type, target_config_id, renew_mode, payment_method, option_num, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (user_id, volume_gb, days, custom_name, price, order_type, target_config_id, renew_mode, payment_method, int(option_num or 0), _now()),
        )
        return cur.lastrowid


def get_custom_order(order_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE id = ?", (order_id,))
    return _fetchone(cur)


def claim_custom_order_for_processing(order_id: int) -> bool:
    with transaction() as cur:
        cur.execute("UPDATE custom_orders SET status='processing' WHERE id=? AND status='paid'", (order_id,))
        ok=(cur.rowcount or 0)==1
    if ok: audit_event("system","custom_order.processing","custom_order",str(order_id))
    return ok

def set_custom_order_status(order_id: int, status: str):
    with transaction() as cur:
        cur.execute("SELECT status FROM custom_orders WHERE id=?",(order_id,)); r=cur.fetchone(); current=(r[0] if r else None)
        cur.execute("UPDATE custom_orders SET status = ? WHERE id = ?", (status, order_id))
    audit_event("system", f"custom_order.{status}", "custom_order", str(order_id), f"from={current}")
    if status in ("paid", "rejected"):
        _adv_hook("custom_decided", int(order_id), "approved" if status == "paid" else "rejected")


def get_pending_custom_orders(limit: int = 30) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE status = 'paid' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


def get_pending_custom_order_receipts(limit: int = 30) -> list[dict]:
    """سفارش‌های «بساز سرویس خودت» که با کارت‌به‌کارت ثبت شده‌اند ولی هنوز
    ادمین رسیدشان را تایید/رد نکرده (status='pending'؛ بعد از تایید 'paid'
    می‌شود و در صف سفارش‌های در انتظار ارسال ظاهر می‌شود)."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# 🧾 رسیدهای در انتظار تایید (شارژ کیف پول + خرید کارت‌به‌کارت پلن ثابت)
# این جدول صرفاً برای نمایش یک‌جای همه‌ی رسیدهای بازبینی‌نشده در پنل ادمین
# است؛ خودِ تایید/رد از همان مسیرهای قبلی (پیام فوروارد‌شده در چت ادمین)
# انجام می‌شود. اگر resolve به هر دلیلی رد را پیدا نکند، تنها اثرش این است
# که آن رد قدیمی در همین لیست باقی می‌ماند؛ روی منطق واقعی شارژ/خرید هیچ
# اثری ندارد.
# ---------------------------------------------------------------------------
def _adv_hook(name: str, *args) -> None:
    """هوک گزارشات پیشرفته (گروه تاپیک‌دار)؛ هیچ‌وقت خطا پرتاب نمی‌کند."""
    try:
        import adv_reports
        getattr(adv_reports, name)(*args)
    except Exception:
        pass


def create_pending_receipt(
    kind: str, telegram_id: str, user_id: int | None, label: str, amount: int,
    extra: str | None = None, plan_key: str | None = None,
    discount_code: str | None = None,
    file_id: str | None = None, file_type: str | None = None,
    note: str | None = None, details: str | None = None,
    service_name: str | None = None,
) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO pending_receipts
               (kind, telegram_id, user_id, label, amount, extra, plan_key, discount_code,
                file_id, file_type, note, details, service_name, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (kind, str(telegram_id), user_id, label, amount, extra, plan_key, discount_code,
             file_id, file_type, note, details, (str(service_name).strip() or None) if service_name else None, _now()),
        )
        _rid = cur.lastrowid
    _adv_hook("receipt_created", _rid)
    return _rid


def set_pending_receipt_media(receipt_id: int, file_id: str | None = None, file_type: str | None = None,
                              note: str | None = None, details: str | None = None) -> None:
    """بعد از ساخت رسید، عکس/متن رسید (و توضیح نمایشی برای ادمین) را روی همان رسید ذخیره می‌کند."""
    sets, vals = [], []
    for col, val in (("file_id", file_id), ("file_type", file_type), ("note", note), ("details", details)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return
    vals.append(int(receipt_id))
    with transaction() as cur:
        cur.execute(f"UPDATE pending_receipts SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def set_order_payment_method(order_id: int, method: str) -> None:
    with transaction() as cur:
        cur.execute("UPDATE orders SET payment_method = ? WHERE id = ?", (method, int(order_id)))


def set_custom_order_payment_method(order_id: int, method: str) -> None:
    with transaction() as cur:
        cur.execute("UPDATE custom_orders SET payment_method = ? WHERE id = ?", (method, int(order_id)))


def set_custom_order_receipt(order_id: int, file_id: str | None = None, note: str | None = None) -> None:
    sets, vals = [], []
    if file_id is not None:
        sets.append("receipt_file_id = ?"); vals.append(file_id)
    if note is not None:
        sets.append("receipt_note = ?"); vals.append(note)
    if not sets:
        return
    vals.append(int(order_id))
    with transaction() as cur:
        cur.execute(f"UPDATE custom_orders SET {', '.join(sets)} WHERE id = ?", tuple(vals))
    if file_id is not None:
        _adv_hook("custom_receipt_set", int(order_id))


def get_paid_custom_orders(limit: int = 30) -> list[dict]:
    """سفارش‌های «بساز سرویس خودت» که رسیدشان تأیید شده (status='paid') ولی هنوز سرویس تحویل داده نشده."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE status = 'paid' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


# fix: اگر ارسال رسید به ادمین در تلگرام شکست بخورد (مثلاً توکن مسدود/رد 403/400)،
# ردیف pending یتیم می‌ماند و تلاش بعدی کاربر ممکن است با همان ردیف قدیمی تداخل پیدا کند.
# این تابع برای همین پاکسازی اضافه شده است.
def delete_pending_receipt(receipt_id: int) -> None:
    with transaction() as cur:
        cur.execute("DELETE FROM pending_receipts WHERE id = ?", (receipt_id,))


def approve_charge_receipt_atomic(telegram_id: str, amount: int) -> bool:
    """رسید شارژ را فقط یک‌بار و در همان تراکنش شارژ می‌کند."""
    with transaction() as cur:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE kind='charge' AND telegram_id=? AND amount=? AND status='pending'
               ORDER BY id DESC LIMIT 1""",
            (str(telegram_id), amount),
        )
        receipt = _fetchone(cur)
        if receipt is None:
            return False
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (str(telegram_id),))
        user = _fetchone(cur)
        if user is None:
            raise ValueError("user not found")
        cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (amount, user["id"]))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'charge', ?, 'completed', ?, ?)""",
            (user["id"], amount, "شارژ کیف پول (تأیید رسید)", _now()),
        )
        cur.execute("UPDATE pending_receipts SET status='resolved' WHERE id=? AND status='pending'", (receipt["id"],))
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("receipt claim lost")
        return True


def approve_plan_receipt_atomic(
    telegram_id: str, plan_key: str, price: int, plan_name: str, order_type: str,
) -> int | None:
    """تأیید رسید، ثبت خرید، ساخت سفارش و مصرف تخفیف را اتمیک انجام می‌دهد."""
    with transaction() as cur:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE kind='plan_card' AND telegram_id=? AND amount=? AND status='pending'
                 AND (plan_key=? OR (plan_key IS NULL AND extra=?))
               ORDER BY id DESC LIMIT 1""",
            (str(telegram_id), price, plan_key, plan_key),
        )
        receipt = _fetchone(cur)
        if receipt is None:
            return None
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (str(telegram_id),))
        user = _fetchone(cur)
        if user is None:
            raise ValueError("user not found")
        if is_free_test_key(plan_key):
            cur.execute("SELECT 1 FROM orders WHERE user_id=? AND (plan_key=? OR plan_key LIKE ?) LIMIT 1",
                        (user["id"], FREE_TEST_PLAN_KEY, FT_KEY_PREFIX + "%"))
            if cur.fetchone() is not None:
                return None
        cur.execute("UPDATE users SET total_purchase = total_purchase + ? WHERE id = ?", (price, user["id"]))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user["id"], price, f"خرید {plan_name} (کارت به کارت)", _now()),
        )
        cur.execute(
            """INSERT INTO orders (user_id, plan_key, plan_name, order_type, price, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (user["id"], plan_key, plan_name, order_type, price, _now()),
        )
        order_id = cur.lastrowid
        _consume_discount_in_transaction(cur, receipt.get("discount_code"), user["id"])
        cur.execute("UPDATE pending_receipts SET status='resolved' WHERE id=? AND status='pending'", (receipt["id"],))
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("receipt claim lost")
        return order_id


def audit_event(actor_type: str, action: str, target_type: str = "", target_id: str = "", details: str = "", actor_id: str | None = None, actor_name: str | None = None):
    ensure_audit_tables()
    with transaction() as cur:
        cur.execute("INSERT INTO admin_audit_log(actor_type,actor_id,actor_name,action,target_type,target_id,details,created_at) VALUES(?,?,?,?,?,?,?,?)", (actor_type,actor_id,actor_name,action,target_type,target_id,details or "",_now()))

def ensure_audit_tables():
    with transaction() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS admin_audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, actor_type TEXT NOT NULL, actor_id TEXT, actor_name TEXT, action TEXT NOT NULL, target_type TEXT, target_id TEXT, details TEXT, created_at TEXT NOT NULL)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at)")
        cur.execute("CREATE TABLE IF NOT EXISTS panel_health(panel_id INTEGER PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, circuit_open INTEGER NOT NULL DEFAULT 0, opened_at REAL, last_ok_at REAL, last_fail_at REAL, last_latency_ms INTEGER, last_error TEXT, probe_claimed INTEGER NOT NULL DEFAULT 0)")
        cur.execute("CREATE TABLE IF NOT EXISTS purchase_action_locks(action_key TEXT PRIMARY KEY, created_at REAL NOT NULL)")

def get_recent_audit_logs(limit: int = 100):
    ensure_audit_tables(); cur=get_connection().cursor(); cur.execute("SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT ?",(limit,)); return _fetchall(cur)

def claim_purchase_action(action_key: str, ttl_seconds: int = 8) -> bool:
    import time
    ensure_audit_tables(); now=time.time()
    with transaction() as cur:
        cur.execute("DELETE FROM purchase_action_locks WHERE created_at < ?", (now-ttl_seconds,))
        try: cur.execute("INSERT INTO purchase_action_locks(action_key,created_at) VALUES(?,?)",(action_key,now))
        except Exception as exc:
            if "unique" in str(exc).lower() or "constraint" in str(exc).lower(): return False
            raise
    return True

def get_panel_health(panel_id: int):
    ensure_audit_tables(); cur=get_connection().cursor(); cur.execute("SELECT * FROM panel_health WHERE panel_id=?",(panel_id,)); return _fetchone(cur)

def claim_panel_probe(panel_id: int) -> bool:
    ensure_audit_tables();
    with transaction() as cur:
        cur.execute("UPDATE panel_health SET probe_claimed=1 WHERE panel_id=? AND probe_claimed=0",(panel_id,))
        if cur.rowcount: return True
        cur.execute("INSERT OR IGNORE INTO panel_health(panel_id,probe_claimed) VALUES(?,1)",(panel_id,))
        return (cur.rowcount or 0)==1

def record_panel_health_result(panel_id:int, ok:bool, latency_ms:int|None, error:str|None, threshold:int=5):
    import time
    now=time.time(); ensure_audit_tables()
    with transaction() as cur:
        cur.execute("INSERT OR IGNORE INTO panel_health(panel_id) VALUES(?)",(panel_id,))
        if ok:
            cur.execute("UPDATE panel_health SET failures=0,circuit_open=0,opened_at=NULL,last_ok_at=?,last_latency_ms=?,last_error=NULL,probe_claimed=0 WHERE panel_id=?",(now,latency_ms,panel_id))
        else:
            cur.execute("SELECT failures FROM panel_health WHERE panel_id=?",(panel_id,)); r=cur.fetchone(); failures=int(r[0] or 0)+1
            opened=now if failures>=threshold else None
            cur.execute("UPDATE panel_health SET failures=?,circuit_open=?,opened_at=COALESCE(opened_at,?),last_fail_at=?,last_latency_ms=?,last_error=?,probe_claimed=0 WHERE panel_id=?",(failures,1 if failures>=threshold else 0,opened,now,latency_ms,(error or "")[:1000],panel_id))

def get_panel_backed_active_configs(limit:int=500):
    cur=get_connection().cursor(); cur.execute("SELECT * FROM configs WHERE deleted=0 AND panel_id IS NOT NULL AND service_id IS NOT NULL AND type='vip' ORDER BY id DESC LIMIT ?",(limit,)); return _fetchall(cur)

def sync_config_from_panel(config_id:int, snap:dict):
    from utils import now_tehran_naive
    total=snap.get("total"); used=snap.get("used"); status=snap.get("status")
    expire=snap.get("expire")
    expiry=None
    if expire is not None:
        try: expiry=datetime.fromtimestamp(int(expire)).strftime("%Y-%m-%d")
        except Exception: pass
    with transaction() as cur:
        cur.execute("UPDATE configs SET panel_used_bytes=?,panel_total_bytes=?,panel_status=?,last_panel_sync_at=?,expiry=COALESCE(?,expiry) WHERE id=?",(used,total,str(status) if status is not None else None,now_tehran_naive().isoformat(timespec='seconds'),expiry,config_id))
    audit_event("system","panel.sync","config",str(config_id),f"used={used};total={total};status={status};expire={expiry}")

def claim_admin_action(action_key: str) -> bool:
    """محافظ دائمی (نه فقط RAM) در برابر پردازش تکراری اقدامات ادمین مثل
    تأیید/رد رسید. بر خلاف is_duplicate_action در utils.py (که فقط چند ثانیه
    در RAM همان پروسه معتبر است)، این تابع یک ردیف با کلید یکتا در دیتابیس
    ثبت می‌کند؛ بنابراین بین چند پروسه/Worker مشترک است و هرگز منقضی نمی‌شود.
    True فقط برای اولین فراخوانی با این کلید برمی‌گردد."""
    with transaction() as cur:
        try:
            cur.execute(
                "INSERT INTO processed_admin_actions (action_key, created_at) VALUES (?, ?)",
                (action_key, _now()),
            )
        except Exception as exc:
            # 🐛 فیکس: روی Turso/libsql خطای نقض قید UNIQUE هم ممکن است sqlite3.IntegrityError نباشد؛ برای همین با متن خطا تشخیص می‌دهیم.
            if "unique" not in str(exc).lower() and "constraint" not in str(exc).lower():
                raise
            return False
        return True


def find_pending_receipt(kind: str, telegram_id: str, amount: int | None = None) -> dict | None:
    """مثل resolve_pending_receipt جدیدترین رسید 'pending' منطبق را پیدا
    می‌کند، اما آن را resolved نمی‌کند؛ برای خواندن discount_code قبل از
    تصمیم‌گیری نهایی (تأیید/رد) استفاده می‌شود."""
    cur = get_connection().cursor()
    if amount is not None:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ? AND amount = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id), amount),
        )
    else:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id)),
        )
    return _fetchone(cur)


def consume_api_rate_limit(bucket_key: str, max_calls: int, period_seconds: int) -> bool:
    """Rate limit دیتابیسی و مشترک بین workerها. True یعنی درخواست مجاز است."""
    import time as _time
    now = int(_time.time())
    cutoff = now - int(period_seconds)
    with transaction() as cur:
        cur.execute("DELETE FROM api_rate_limits WHERE created_at <= ?", (cutoff,))
        cur.execute("SELECT COUNT(*) AS c FROM api_rate_limits WHERE bucket_key=?", (bucket_key,))
        if _fetchone(cur)["c"] >= max_calls:
            return False
        cur.execute("INSERT INTO api_rate_limits(bucket_key, created_at) VALUES (?, ?)", (bucket_key, now))
        return True


def get_pending_receipts(limit: int = 30) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM pending_receipts WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
    )
    return _fetchall(cur)


def resolve_pending_receipt(kind: str, telegram_id: str, amount: int | None = None, outcome: str | None = None):
    """جدیدترین رسید 'pending' منطبق را resolved می‌کند. اگر amount داده نشود
    (مثل شارژ با مبلف دلخواه توسط ادمین)، فقط بر اساس kind+telegram_id
    جدیدترین را می‌بندد. best-effort است و نباید هیچ‌وقت صدا زدنش خطا پرتاب کند
    (تماس‌گیرنده هم آن را در try/except صدا می‌زند)."""
    cur = get_connection().cursor()
    if amount is not None:
        cur.execute(
            """SELECT id FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ? AND amount = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id), amount),
        )
    else:
        cur.execute(
            """SELECT id FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id)),
        )
    row = _fetchone(cur)
    if row is None:
        return
    with transaction() as tcur:
        tcur.execute("UPDATE pending_receipts SET status = 'resolved' WHERE id = ?", (row["id"],))
    if outcome:
        _adv_hook("receipt_decided", row["id"], outcome)
    return row["id"]


def dismiss_all_pending_receipts():
    with transaction() as cur:
        cur.execute("UPDATE pending_receipts SET status = 'resolved' WHERE status = 'pending'")


# ---------------------------------------------------------------------------
# ✏️ ویرایش نام/قیمت پلن‌های VIP از پنل ادمین
# مقادیر اصلی در config.py ثابت هستند؛ اگر ادمین چیزی را عوض کند، اینجا (در
# جدول settings، کلید 'plan_overrides') ذخیره می‌شود و روی مقدار اصلی اولویت دارد.
# ---------------------------------------------------------------------------
_PLAN_OVERRIDES_KEY = "plan_overrides"


def get_plan_overrides() -> dict:
    raw = get_setting(_PLAN_OVERRIDES_KEY, "{}")
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


def set_plan_override(plan_key: str, name: str | None = None, price: int | None = None):
    overrides = get_plan_overrides()
    entry = overrides.get(plan_key, {})
    if name is not None:
        entry["name"] = name
    if price is not None:
        entry["price"] = price
    overrides[plan_key] = entry
    set_setting(_PLAN_OVERRIDES_KEY, json.dumps(overrides, ensure_ascii=False))


def clear_plan_override(plan_key: str):
    overrides = get_plan_overrides()
    if plan_key in overrides:
        del overrides[plan_key]
        set_setting(_PLAN_OVERRIDES_KEY, json.dumps(overrides, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP — هرکدام می‌تواند هر تعداد پلن داشته باشد. کاملاً از پنل
# ادمین قابل مدیریت است (افزودن دسته/پلن جدید، ویرایش، حذف)، بدون نیاز به هیچ
# تغییری در کد. این جدول‌ها منبع اصلی پلن‌های VIP هستند (نه دیکشنری VIP_PLANS
# در config.py که فقط برای seed اولیه استفاده شد).
# ---------------------------------------------------------------------------
def _slugify_key(prefix: str, name: str) -> str:
    return f"{prefix}_{secrets.token_hex(3)}"



VIP_ROOT_BUCKET_KEY = "__root_bucket__"

def is_vip_root_bucket(cat: dict | None) -> bool:
    return bool(cat) and cat.get("key") == VIP_ROOT_BUCKET_KEY

def get_vip_root_bucket(create: bool = True) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_categories WHERE key = ?", (VIP_ROOT_BUCKET_KEY,))
    row = _fetchone(cur)
    if row or not create:
        return row
    with transaction() as cur:
        cur.execute(
            "INSERT INTO vip_categories (key, name, sort_order, created_at, parent_id) VALUES (?, ?, ?, ?, NULL)",
            (VIP_ROOT_BUCKET_KEY, "🏠 پلن‌های بدون پوشه", -1, _now()),
        )
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_categories WHERE key = ?", (VIP_ROOT_BUCKET_KEY,))
    return _fetchone(cur)

def get_child_vip_categories(parent_id: int | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if parent_id is None:
        cur.execute(
            "SELECT * FROM vip_categories WHERE parent_id IS NULL AND key != ? ORDER BY sort_order ASC, id ASC",
            (VIP_ROOT_BUCKET_KEY,),
        )
    else:
        cur.execute(
            "SELECT * FROM vip_categories WHERE parent_id = ? ORDER BY sort_order ASC, id ASC",
            (int(parent_id),),
        )
    return _fetchall(cur)

def get_vip_category_path(category_id: int | None) -> list[dict]:
    path: list[dict] = []
    seen: set[int] = set()
    current = get_vip_category(int(category_id)) if category_id is not None else None
    while current and current["id"] not in seen and len(path) < 50:
        seen.add(current["id"])
        path.append(current)
        parent_id = current.get("parent_id")
        current = get_vip_category(int(parent_id)) if parent_id is not None else None
    path.reverse()
    return path

def get_vip_descendant_ids(category_id: int) -> set[int]:
    result: set[int] = {int(category_id)}
    frontier = [int(category_id)]
    while frontier:
        cur = get_connection().cursor()
        marks = ",".join("?" for _ in frontier)
        cur.execute(f"SELECT id FROM vip_categories WHERE parent_id IN ({marks})", tuple(frontier))
        frontier = [int(r["id"]) for r in _fetchall(cur) if int(r["id"]) not in result]
        result.update(frontier)
    return result

def get_vip_categories() -> list[dict]:
    """همه‌ی پوشه‌ها (از هر سطح) به‌صورت لیست تخت — برای بخش‌هایی مثل تنظیمات تمدید و نگاشت پنل.
    سطل ریشه فقط وقتی برگردانده می‌شود که پلن داشته باشد."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM vip_categories "
        "WHERE key != ? OR EXISTS (SELECT 1 FROM vip_plans WHERE vip_plans.category_id = vip_categories.id) "
        "ORDER BY sort_order ASC, id ASC",
        (VIP_ROOT_BUCKET_KEY,),
    )
    return _fetchall(cur)


def move_vip_category_to(category_id: int, new_parent_id: int | None) -> tuple[bool, str]:
    cat = get_vip_category(int(category_id))
    if cat is None or is_vip_root_bucket(cat):
        return False, "پوشه پیدا نشد."
    if new_parent_id is not None:
        parent = get_vip_category(int(new_parent_id))
        if parent is None or is_vip_root_bucket(parent):
            return False, "پوشه‌ی مقصد پیدا نشد."
        if int(new_parent_id) in get_vip_descendant_ids(int(category_id)):
            return False, "نمی‌توان یک پوشه را داخل خودش یا زیرمجموعه‌اش منتقل کرد."
    current_parent = cat.get("parent_id")
    if (current_parent is None and new_parent_id is None) or (
        current_parent is not None and new_parent_id is not None and int(current_parent) == int(new_parent_id)
    ):
        return False, "این پوشه همین الان در همان مکان است."
    with transaction() as cur:
        if new_parent_id is None:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_categories WHERE parent_id IS NULL AND key != ?",
                        (VIP_ROOT_BUCKET_KEY,))
        else:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_categories WHERE parent_id = ?",
                        (int(new_parent_id),))
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            "UPDATE vip_categories SET parent_id = ?, sort_order = ? WHERE id = ?",
            (int(new_parent_id) if new_parent_id is not None else None, sort_order, int(category_id)),
        )
    return True, "ok"


def move_vip_plans_to_category(plan_keys: list[str], category_id: int) -> int:
    moved = 0
    with transaction() as cur:
        for plan_key in plan_keys:
            cur.execute("SELECT id, category_id FROM vip_plans WHERE plan_key = ?", (plan_key,))
            row = cur.fetchone()
            if not row:
                continue
            plan_id, current_cat = int(row[0]), int(row[1])
            if current_cat == int(category_id):
                continue
            # 🔌 حفظ نگاشت: اگر پلن نگاشت اختصاصی ندارد ولی از نگاشت «کل پوشه‌ی» مبدأ ارث می‌برد،
            # قبل از انتقال همان نگاشت به‌صورت اختصاصی روی خود پلن ثبت می‌شود تا با انتقال از دست نرود.
            cur.execute("SELECT 1 FROM panel_plan_map WHERE scope='vip_plan' AND scope_id=?", (plan_id,))
            if not cur.fetchone():
                cur.execute("SELECT panel_id, remote_ref, remote_name FROM panel_plan_map WHERE scope='vip_category' AND scope_id=?", (current_cat,))
                inh = cur.fetchone()
                if inh:
                    cur.execute(
                        "INSERT INTO panel_plan_map (scope, scope_id, panel_id, remote_ref, remote_name, created_at) VALUES ('vip_plan', ?, ?, ?, ?, ?)",
                        (plan_id, inh[0], inh[1], inh[2], _now()),
                    )
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM vip_plans WHERE category_id = ?", (int(category_id),))
            sort_order = int(cur.fetchone()[0])
            cur.execute(
                "UPDATE vip_plans SET category_id = ?, sort_order = ? WHERE id = ?",
                (int(category_id), sort_order, plan_id),
            )
            moved += 1
    return moved



def get_vip_category(key_or_id) -> dict | None:
    cur = get_connection().cursor()
    if isinstance(key_or_id, int) or (isinstance(key_or_id, str) and key_or_id.isdigit()):
        cur.execute("SELECT * FROM vip_categories WHERE id = ?", (int(key_or_id),))
        row = _fetchone(cur)
        if row:
            return row
    cur.execute("SELECT * FROM vip_categories WHERE key = ?", (str(key_or_id),))
    return _fetchone(cur)


def create_vip_category(name: str, parent_id: int | None = None) -> dict:
    key = _slugify_key("cat", name)
    with transaction() as cur:
        if parent_id is None:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_categories WHERE parent_id IS NULL AND key != ?",
                        (VIP_ROOT_BUCKET_KEY,))
        else:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_categories WHERE parent_id = ?",
                        (int(parent_id),))
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            "INSERT INTO vip_categories (key, name, sort_order, created_at, parent_id) VALUES (?, ?, ?, ?, ?)",
            (key, name, sort_order, _now(), int(parent_id) if parent_id is not None else None),
        )
    return get_vip_category(key)


def rename_vip_category(category_id: int, name: str):
    with transaction() as cur:
        cur.execute("UPDATE vip_categories SET name = ? WHERE id = ?", (name, category_id))


def update_vip_category_description(category_id: int, description: str | None, entities: list | None = None):
    """توضیح یک دسته‌بندی VIP (به‌همراه entityهای تلگرام: ایموجی پریمیوم/ریچ‌تکست) را
    به‌روز می‌کند. مقدار None یعنی توضیحی ثبت نشود/حذف شود."""
    ents_json = json.dumps(entities, ensure_ascii=False) if (description and entities) else None
    with transaction() as cur:
        cur.execute(
            "UPDATE vip_categories SET description = ?, description_entities = ? WHERE id = ?",
            (description, ents_json, category_id),
        )


def get_vip_category_description(cat) -> tuple[str, list]:
    """(متن توضیحات, لیست entityها) یک دسته VIP را برمی‌گرداند. cat می‌تواند dict، id یا key باشد."""
    if not isinstance(cat, dict):
        cat = get_vip_category(cat)
    if not cat:
        return "", []
    text = str(cat.get("description") or "").strip()
    if not text:
        return "", []
    raw = cat.get("description_entities")
    try:
        ents = json.loads(raw) if raw else []
    except Exception:
        ents = []
    return text, (ents if isinstance(ents, list) else [])


# نام‌های مستعار برای سازگاری با db_async.py
def list_vip_categories() -> list[dict]:
    return get_vip_categories()


def get_vip_plans_for_category(category_id: int) -> list[dict]:
    return get_vip_plans(category_id)


def is_admin(telegram_id) -> bool:
    """ادمین فرعی بودن کاربر (ادمین اصلی جداگانه با ADMIN_ID بررسی می‌شود)."""
    try:
        return is_sub_admin(str(telegram_id))
    except Exception:
        return False


def delete_vip_category(category_id: int) -> bool:
    """اگر پوشه خالی (بدون پلن و بدون زیرپوشه) باشد حذف می‌شود و True برمی‌گرداند؛
    در غیر این صورت حذف نمی‌شود (باید اول محتوایش حذف/منتقل شود) و False برمی‌گردد."""
    cat = get_vip_category(int(category_id))
    if cat is None or is_vip_root_bucket(cat):
        return False
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM vip_plans WHERE category_id = ?", (category_id,))
    if _fetchone(cur)["c"] > 0:
        return False
    cur.execute("SELECT COUNT(*) AS c FROM vip_categories WHERE parent_id = ?", (category_id,))
    if _fetchone(cur)["c"] > 0:
        return False
    with transaction() as cur:
        cur.execute("DELETE FROM vip_categories WHERE id = ?", (category_id,))
        # نگاشت پنل آن دسته هم پاک شود (ربات و مینی‌اپ هر دو از همین تابع حذف می‌کنند)
        cur.execute("DELETE FROM panel_plan_map WHERE scope = 'vip_category' AND scope_id = ?", (category_id,))
    return True


def get_vip_plans(category_id: int) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM vip_plans WHERE category_id = ? ORDER BY sort_order ASC, id ASC", (category_id,)
    )
    return _fetchall(cur)


def get_vip_plan(plan_key: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_plans WHERE plan_key = ?", (plan_key,))
    return _fetchone(cur)

def get_vip_plan_by_id(plan_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_plans WHERE id = ?", (plan_id,))
    return _fetchone(cur)


def add_vip_plan(category_id: int, name: str, price: int, days: int = 0, volume_gb: int = 0, user_limit: int = 0) -> str:
    plan_key = _slugify_key("vip", name)
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_plans WHERE category_id = ?",
                    (category_id,))
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            """INSERT INTO vip_plans
               (plan_key, category_id, name, price, days, volume_gb, user_limit, sort_order, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_key, category_id, name, price, days, volume_gb, user_limit, sort_order, _now()),
        )
    return plan_key


def update_vip_plan(plan_key: str, name: str | None = None, price: int | None = None,
                     days: int | None = None, volume_gb: int | None = None, user_limit: int | None = None):
    fields, values = [], []
    for col, val in (("name", name), ("price", price), ("days", days), ("volume_gb", volume_gb), ("user_limit", user_limit)):
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return
    values.append(plan_key)
    with transaction() as cur:
        cur.execute(f"UPDATE vip_plans SET {', '.join(fields)} WHERE plan_key = ?", values)


def delete_vip_plan(plan_key: str):
    with transaction() as cur:
        cur.execute("SELECT id FROM vip_plans WHERE plan_key = ?", (plan_key,))
        row = cur.fetchone()
        cur.execute("DELETE FROM vip_plans WHERE plan_key = ?", (plan_key,))
        if row:
            # نگاشت پنل پلن حذف‌شده هم پاک شود تا نگاشت یتیم نماند
            cur.execute("DELETE FROM panel_plan_map WHERE scope = 'vip_plan' AND scope_id = ?", (row[0],))


def get_all_vip_plans_flat() -> dict:
    """همه‌ی پلن‌های VIP (از همه‌ی دسته‌ها) را به‌شکل {plan_key: plan_dict} برمی‌گرداند."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_plans")
    return {row["plan_key"]: row for row in _fetchall(cur)}


# ---------------------------------------------------------------------------
def _move_row(table: str, row_id: int, direction: str, group_col: str | None = None) -> bool:
    cur = get_connection().cursor()
    group_val = None
    if group_col:
        cur.execute(f"SELECT {group_col} AS g FROM {table} WHERE id = ?", (row_id,))
        row = _fetchone(cur)
        if row is None:
            return False
        group_val = row["g"]

    if group_col:
        cur.execute(
            f"SELECT id, sort_order FROM {table} WHERE {group_col} = ? ORDER BY sort_order ASC, id ASC",
            (group_val,),
        )
    else:
        cur.execute(f"SELECT id, sort_order FROM {table} ORDER BY sort_order ASC, id ASC")
    rows = _fetchall(cur)

    idx = next((i for i, r in enumerate(rows) if r["id"] == row_id), None)
    if idx is None:
        return False

    if direction == "up":
        if idx == 0:
            return False
        other = rows[idx - 1]
    elif direction == "down":
        if idx == len(rows) - 1:
            return False
        other = rows[idx + 1]
    else:
        return False

    with transaction() as cur:
        cur.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (other["sort_order"], rows[idx]["id"]))
        cur.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (rows[idx]["sort_order"], other["id"]))
    return True


def move_vip_category(category_id: int, direction: str) -> bool:
    """جابجایی بالا/پایین یک پوشه بین هم‌سطح‌هایش (پوشه‌های هم‌والد)."""
    cat = get_vip_category(int(category_id))
    if cat is None or is_vip_root_bucket(cat):
        return False
    siblings = get_child_vip_categories(cat.get("parent_id"))
    idx = next((i for i, r in enumerate(siblings) if r["id"] == cat["id"]), None)
    if idx is None:
        return False
    if direction == "up":
        if idx == 0:
            return False
        other = idx - 1
    elif direction == "down":
        if idx == len(siblings) - 1:
            return False
        other = idx + 1
    else:
        return False
    order = [r["id"] for r in siblings]
    order[idx], order[other] = order[other], order[idx]
    with transaction() as cur:
        for position, row_id in enumerate(order):
            cur.execute("UPDATE vip_categories SET sort_order = ? WHERE id = ?", (position, row_id))
    return True


def move_vip_plan(plan_id: int, direction: str) -> bool:
    return _move_row("vip_plans", plan_id, direction, "category_id")






FT_KEY_PREFIX = FREE_TEST_PLAN_KEY + "__"


def is_free_test_key(plan_key) -> bool:
    """کلید پلن تست پیش‌فرض (plan_test) یا یکی از پلن‌های تست چندگانه (plan_test__N)."""
    k = str(plan_key or "")
    return k == FREE_TEST_PLAN_KEY or k.startswith(FT_KEY_PREFIX)


def free_test_plan_key(num) -> str:
    return f"{FT_KEY_PREFIX}{int(num)}"


def free_test_entry_num(plan_key) -> int | None:
    k = str(plan_key or "")
    if k.startswith(FT_KEY_PREFIX) and k[len(FT_KEY_PREFIX):].isdigit():
        return int(k[len(FT_KEY_PREFIX):])
    return None


def get_free_test_entry_by_num(num) -> dict | None:
    for p in get_free_test_panels():
        if int(p.get("num") or 0) == int(num):
            return p
    return None


def _free_test_entry_to_plan(entry: dict) -> dict:
    volume_gb = float(entry.get("volume_gb") or 0)
    unit = "hours" if str(entry.get("duration_unit") or "days") == "hours" else "days"
    if unit == "hours":
        # حالت ساعتی: days به‌صورت اعشاری (ساعت/۲۴) برای محاسبه‌ی انقضا نگه داشته می‌شود.
        hours = float(entry.get("duration_value") or entry.get("hours") or 1)
        duration_value = int(hours) if hours == int(hours) else hours
        days = hours / 24
    else:
        days = entry.get("days") or 1
        duration_value = days
    plan = {
        "name": entry.get("name") or "تست رایگان",
        "price": int(entry.get("price") or 0),
        "days": days,
        "volume_gb": volume_gb,
        "duration_value": duration_value,
        "duration_unit": unit,
        "free_test_entry": int(entry.get("num") or 0),
    }
    return plan


def plan_type(plan_key: str) -> str:
    """نوع پلن را از روی داده‌ی واقعی دیتابیس تشخیص می‌دهد."""
    if is_free_test_key(plan_key):
        return "test"
    if get_vip_plan(plan_key) is not None:
        return "vip"
    return "vip"


RENEWAL_SWITCH_KEY = "renewal_enabled"


def is_renewal_enabled() -> bool:
    """سوئیچ سراسری تمدید (پیش‌فرض روشن). خاموش = قابلیت تمدید و همه‌ی دکمه‌های آن در ربات و مینی‌اپ غیرفعال."""
    return str(get_setting(RENEWAL_SWITCH_KEY, "1") or "1") != "0"


def set_renewal_enabled(value: bool) -> None:
    set_setting(RENEWAL_SWITCH_KEY, "1" if value else "0")


def renew_block_text(cfg: dict | None = None) -> str:
    """پیام عدم‌امکان تمدید؛ اگر سوئیچ سراسری خاموش باشد همان را می‌گوید."""
    if not is_renewal_enabled():
        return "ℹ️ تمدید سرویس فعلاً غیرفعال است."
    return "ℹ️ تمدید برای سرویس‌های تست و PAYG در دسترس نیست."


def config_allows_renew(cfg: dict | None) -> bool:
    """تمدید برای سرویس‌های «تست رایگان» و «PAYG» مجاز نیست و با خاموش‌بودن سوئیچ سراسری تمدید هم برای همه‌ی
    سرویس‌ها غیرفعال است (دکمه نمایش داده نمی‌شود و سمت سرور هم رد می‌شود)."""
    if not is_renewal_enabled():
        return False
    t = str((cfg or {}).get("type") or "").strip().lower()
    if t in ("payg", "test"):
        return False
    try:
        if (cfg or {}).get("payg"):
            return False
    except Exception:
        pass
    return True


def has_used_free_test(user_id: int, plan_key: str | None = None) -> bool:
    """آیا این کاربر قبلاً «همین پلن تست» را دریافت کرده است؟

    هر پلن تست (plan_test__N) جداگانه حساب می‌شود: کاربر از هر پلن تست دقیقاً
    یک‌بار می‌تواند استفاده کند و گرفتن یکی مانع گرفتن بقیه نیست. سفارش تست فقط
    وقتی ثبت می‌شود که پرداخت/تحویل واقعاً انجام شده باشد، پس وجود ردیفی با
    همین plan_key یعنی قبلاً گرفته شده.
    اگر plan_key داده نشود (سازگاری قدیمی)، گرفتن هر تستی «استفاده‌شده» حساب می‌شود."""
    cur = get_connection().cursor()
    # 🤝 سهمیه‌ی تست ماهانه‌ی نماینده: اگر برای این نماینده تعیین شده باشد، به‌جای
    # «یک‌بار برای هر پلن»، تعداد تست‌های ۳۰ روز اخیر (از هر پلن تست) شمرده می‌شود.
    try:
        _u = get_user_by_id(int(user_id))
        _q = agent_test_quota(_u["telegram_id"]) if _u else 0
    except Exception:
        _q = 0
    if _q > 0:
        return agent_tests_used_this_month(user_id) >= _q
    if plan_key:
        cur.execute("SELECT 1 FROM orders WHERE user_id = ? AND plan_key = ? LIMIT 1", (user_id, str(plan_key)))
    else:
        cur.execute(
            "SELECT 1 FROM orders WHERE user_id = ? AND (plan_key = ? OR plan_key LIKE ?) LIMIT 1",
            (user_id, FREE_TEST_PLAN_KEY, FT_KEY_PREFIX + "%"),
        )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# 🎁 تنظیم حجم/مدت/قیمت پلن «تست رایگان» از پنل ادمین — مقدار پیش‌فرض همان
# FREE_TEST_PLAN در config.py است؛ اگر ادمین مقدار جدیدی تنظیم کند، اینجا (در
# جدول settings، کلید 'free_test_override') ذخیره می‌شود و روی مقدار اصلی اولویت
# دارد. مدت اعتبار هم به‌صورت ساعت و هم به‌صورت روز قابل تنظیم است (duration_unit).
# ---------------------------------------------------------------------------
_FREE_TEST_OVERRIDE_KEY = "free_test_override"


def get_free_test_override() -> dict | None:
    raw = get_setting(_FREE_TEST_OVERRIDE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "volume_mb" in data and "duration_value" in data:
            return data
    except Exception:
        pass
    return None


def set_free_test_override(volume_mb: int, duration_value: float, duration_unit: str, price: int) -> None:
    set_setting(
        _FREE_TEST_OVERRIDE_KEY,
        json.dumps(
            {"volume_mb": volume_mb, "duration_value": duration_value, "duration_unit": duration_unit, "price": price},
            ensure_ascii=False,
        ),
    )


def get_effective_free_test_plan() -> dict:
    """پلن «تست رایگان» واقعی را برمی‌گرداند: اگر ادمین از پنل مقدار جدیدی
    (حجم/مدت/قیمت) تنظیم کرده باشد همان استفاده می‌شود، وگرنه مقدار پیش‌فرض
    FREE_TEST_PLAN در config.py استفاده می‌شود. نام پلن همیشه به‌صورت خودکار از روی
    حجم/مدت فعلی ساخته می‌شود تا با تغییر این مقادیر، متن نمایشی هم درست/به‌روز بماند.

    نکته‌ی مهم: پلن["days"] همیشه برحسب روز (ممکن است اعشاری باشد اگر واحد ساعت
    باشد) برمی‌گردد تا منطق محاسبه‌ی انقضای سرویس (panels.create_service/_expire_from_days
    که از float days هم پشتیبانی می‌کند) بدون تغییر باقی بماند."""
    plan = dict(FREE_TEST_PLAN)
    override = get_free_test_override()
    if override:
        volume_mb = override["volume_mb"]
        duration_value = override["duration_value"]
        duration_unit = override.get("duration_unit") or "days"
        days = (duration_value / 24) if duration_unit == "hours" else duration_value
        plan["days"] = days
        plan["volume_gb"] = volume_mb / 1024
        if override.get("price") is not None:
            plan["price"] = override["price"]
    else:
        days = plan.get("days", 7)
        volume_mb = round(plan.get("volume_gb", 1) * 1024)
        duration_value = days
        duration_unit = "days"

    if volume_mb < 1024:
        volume_label = f"{volume_mb} مگابایت"
    else:
        gb_value = volume_mb / 1024
        volume_label = f"{gb_value:.0f} گیگ" if gb_value == int(gb_value) else f"{gb_value:.2f} گیگ"

    if duration_unit == "hours":
        dv = int(duration_value) if float(duration_value) == int(duration_value) else duration_value
        duration_label = f"{dv} ساعته"
    else:
        dv = int(duration_value) if float(duration_value) == int(duration_value) else duration_value
        duration_label = f"{dv} روزه"

    plan["name"] = f"{volume_label} {duration_label}"
    plan["duration_value"] = duration_value
    plan["duration_unit"] = duration_unit
    return plan


# ---------------------------------------------------------------------------
# 🧩 تنظیم قیمت/محدوده‌ی «بساز سرویس خودت» از پنل ادمین — مقدار پیش‌فرض همان
# CUSTOM_BUILD_* در config.py است؛ اگر ادمین مقدار جدیدی تنظیم کند، اینجا (در
# جدول settings، کلید 'custom_build_override') ذخیره می‌شود و روی مقدار اصلی اولویت
# دارد.
# ---------------------------------------------------------------------------
_CUSTOM_BUILD_OVERRIDE_KEY = "custom_build_override"


def get_custom_build_override() -> dict | None:
    raw = get_setting(_CUSTOM_BUILD_OVERRIDE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "price_per_gb" in data:
            return data
    except Exception:
        pass
    return None


def set_custom_build_override(
    price_per_gb: int,
    price_per_30_days: int,
    min_gb: int,
    max_gb: int,
    min_days: int,
    max_days: int,
) -> None:
    set_setting(
        _CUSTOM_BUILD_OVERRIDE_KEY,
        json.dumps(
            {
                "price_per_gb": price_per_gb,
                "price_per_30_days": price_per_30_days,
                "min_gb": min_gb,
                "max_gb": max_gb,
                "min_days": min_days,
                "max_days": max_days,
            },
            ensure_ascii=False,
        ),
    )


def get_effective_custom_build_settings(option_num: int | None = None) -> dict:
    """تنظیمات واقعی «بساز سرویس خودت». اگر option_num (گزینه‌ی نگاشت) داده شود و برای
    آن گزینه قیمت/محدوده‌ی اختصاصی تنظیم شده باشد، همان فیلدها روی مقدار عمومی
    جایگزین می‌شوند؛ فیلدهای تنظیم‌نشده از مقدار عمومی می‌آیند."""
    settings = {
        "price_per_gb": CUSTOM_BUILD_PRICE_PER_GB,
        "price_per_30_days": CUSTOM_BUILD_PRICE_PER_30_DAYS,
        "min_gb": CUSTOM_BUILD_MIN_GB,
        "max_gb": CUSTOM_BUILD_MAX_GB,
        "min_days": CUSTOM_BUILD_MIN_DAYS,
        "max_days": CUSTOM_BUILD_MAX_DAYS,
    }
    override = get_custom_build_override()
    if override:
        settings.update({k: v for k, v in override.items() if v is not None})
    if option_num:
        opt = get_map_option_cfg("custom_build", int(option_num))
        settings.update({k: v for k, v in opt.items() if k in settings and v is not None})
    return settings


# ---------------------------------------------------------------------------
# تنظیمات اختصاصی هر «گزینه‌ی نگاشت» (PAYG / بساز سرویس خودت)
# هر گزینه می‌تواند قیمت/محدوده‌ی خودش را داشته باشد؛ مقدار تنظیم‌نشده = از عمومی.
#   custom_build: price_per_gb, price_per_30_days, min_gb, max_gb, min_days, max_days
#   payg:         price_per_gb
# ---------------------------------------------------------------------------
_OPT_CFG_FIELDS = {
    "custom_build": ("price_per_gb", "price_per_30_days", "min_gb", "max_gb", "min_days", "max_days"),
    "payg": ("price_per_gb",),
}


def get_map_option_cfg(kind: str, num: int) -> dict:
    raw = get_setting(f"map_opt_cfg_{kind}_{int(num)}", "") or ""
    try:
        d = json.loads(raw) if raw else {}
    except Exception:
        d = {}
    allowed = _OPT_CFG_FIELDS.get(kind, ())
    out = {}
    for k in allowed:
        try:
            v = int(d.get(k))
            if v >= 0:
                out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def set_map_option_cfg(kind: str, num: int, cfg: dict) -> dict:
    allowed = _OPT_CFG_FIELDS.get(kind, ())
    clean = {}
    for k in allowed:
        if k in cfg and cfg[k] not in (None, ""):
            clean[k] = max(0, int(cfg[k]))
    set_setting(f"map_opt_cfg_{kind}_{int(num)}", json.dumps(clean, ensure_ascii=False))
    return clean


def clear_map_option_cfg(kind: str, num: int) -> None:
    set_setting(f"map_opt_cfg_{kind}_{int(num)}", "")


def get_all_plans() -> dict:
    """همه‌ی پلن‌های واقعی VIP به‌علاوه‌ی پلن تست را برمی‌گرداند."""
    result = {}
    result.update(get_all_vip_plans_flat())
    result[FREE_TEST_PLAN_KEY] = get_effective_free_test_plan()
    return result


def get_effective_plan(plan_key: str) -> dict | None:
    """نسخه‌ی نهایی/واقعی یک پلن را برمی‌گرداند."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return get_effective_free_test_plan()
    _ft_num = free_test_entry_num(plan_key)
    if _ft_num is not None:
        _entry = get_free_test_entry_by_num(_ft_num)
        return _free_test_entry_to_plan(_entry) if _entry else None

    vip_plan = get_vip_plan(plan_key)
    if vip_plan is not None:
        return dict(vip_plan)


    return None


def get_effective_plans(base_plans: dict) -> dict:
    """پلن‌های پایه را با overrideهای ذخیره‌شده ترکیب می‌کند."""
    overrides = get_plan_overrides()
    result = {}
    for key, plan in base_plans.items():
        merged = dict(plan)
        if key in overrides:
            merged.update({k: v for k, v in overrides[key].items() if v is not None})
        result[key] = merged
    return result


# ---------------------------------------------------------------------------
# 🔗 نگاشت دسته‌بندی‌ها به planSlug پنل شاهراه
# ---------------------------------------------------------------------------
def set_shahrah_plan_map(scope: str, scope_id: int, plan_slug: str, plan_name: str | None = None):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO shahrah_plan_map (scope, scope_id, plan_slug, plan_name, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id) DO UPDATE SET
                   plan_slug = excluded.plan_slug, plan_name = excluded.plan_name""",
            (scope, scope_id, plan_slug, plan_name, _now()),
        )


def get_shahrah_plan_map(scope: str, scope_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM shahrah_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id)
    )
    return _fetchone(cur)


def delete_shahrah_plan_map(scope: str, scope_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM shahrah_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id))


def list_shahrah_plan_maps(scope: str | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if scope:
        cur.execute("SELECT * FROM shahrah_plan_map WHERE scope = ?", (scope,))
    else:
        cur.execute("SELECT * FROM shahrah_plan_map")
    return _fetchall(cur)


def get_shahrah_plan_map_for_plan_key(plan_key: str) -> dict | None:
    """با گرفتن plan_key یک پلن VIP، اول نگاشت اختصاصیِ خودِ همین پلن (بر اساس
    id دقیق پلن، نه دسته‌بندی) را چک می‌کند — چون هر پلن ممکن است حجم/مدت
    متفاوتی داشته باشد و باید به بسته‌ی متناظرش در شاهراه وصل شود.
    اگر پلن نگاشت اختصاصی نداشت، به‌صورت fallback نگاشت سطح دسته‌بندی (رفتار
    قدیمی‌تر، برای وقتی که ادمین فقط یک نگاشت پیش‌فرض برای کل دسته گذاشته)
    برگردانده می‌شود تا نگاشت‌های قبلی از کار نیفتند.

    پلن «تست رایگان» چون در جدول vip_plans نیست (یک پلن ثابت جداگانه در
    config.py است)، یک نگاشت سراسری مستقل با scope="free_test" دارد.

    این تابع فقط پلن‌های VIP و تست را بررسی می‌کند.
    """
    if plan_key == FREE_TEST_PLAN_KEY:
        return get_shahrah_plan_map("free_test", 0)
    _ft_num = free_test_entry_num(plan_key)
    if _ft_num is not None:
        return get_shahrah_plan_map("free_test", _ft_num)

    vip_plan = get_vip_plan(plan_key)
    if vip_plan is not None:
        plan_map = get_shahrah_plan_map("vip_plan", vip_plan["id"])
        if plan_map:
            return plan_map
        return get_shahrah_plan_map("vip_category", vip_plan["category_id"])

    return None


# ---------------------------------------------------------------------------
# 🆕 چندپنلی: مدیریت نمونه‌های پنل (شاهراه/مرزبان/پاسارگارد) + نگاشت عمومی
# ---------------------------------------------------------------------------
def create_vpn_panel(panel_type: str, name: str, base_url: str, api_key: str | None = None,
                      username: str | None = None, password: str | None = None,
                      auth_method: str = "userpass") -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO vpn_panels (panel_type, name, base_url, api_key, username, password, auth_method, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (panel_type, name, base_url, api_key, username, password, auth_method, _now()),
        )
        cache.invalidate_prefix("vpn_panels:")
        return cur.lastrowid


def get_vpn_panel(panel_id: int) -> dict | None:
    # perf: پنل‌ها در فلوی تحویل/تمدید سرویس بارها خونده می‌شن؛ کش می‌کنیم.
    cache_key = f"vpn_panels:one:{panel_id}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vpn_panels WHERE id = ?", (panel_id,))
    value = _fetchone(cur)
    cache.set(cache_key, value)
    return value


def list_vpn_panels(panel_type: str | None = None, enabled_only: bool = False) -> list[dict]:
    # perf: این تابع تقریباً در هر عملیات مربوط به پنل (تحویل سرویس، تمدید،
    # نمایش لیست پنل‌ها در پنل ادمین) صدا زده می‌شود؛ چون لیست پنل‌ها به‌ندرت
    # تغییر می‌کند، نتیجه‌اش کوتاه‌مدت کش می‌شود.
    cache_key = f"vpn_panels:list:{panel_type}:{enabled_only}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    query = "SELECT * FROM vpn_panels"
    conditions = []
    params: list = []
    if panel_type:
        conditions.append("panel_type = ?")
        params.append(panel_type)
    if enabled_only:
        conditions.append("enabled = 1")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY sort_order ASC, id ASC"
    cur.execute(query, params)
    value = _fetchall(cur)
    cache.set(cache_key, value)
    return value


def update_vpn_panel(panel_id: int, name: str | None = None, base_url: str | None = None,
                      api_key: str | None = None, username: str | None = None,
                      password: str | None = None, enabled: bool | None = None,
                      auth_method: str | None = None) -> None:
    """فقط فیلدهای غیرNone آپدیت می‌شوند (برای پاک کردن رشته‌ای مثل رمز عبور، از یک رشته‌‌
    خالی جداگانه مثل clear_password=True استفاده نمی‌شود، فقط مقدار جدید می‌فرستیم)."""
    fields = []
    params: list = []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if base_url is not None:
        fields.append("base_url = ?")
        params.append(base_url)
    if api_key is not None:
        fields.append("api_key = ?")
        params.append(api_key)
    if username is not None:
        fields.append("username = ?")
        params.append(username)
    if password is not None:
        fields.append("password = ?")
        params.append(password)
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if enabled else 0)
    if auth_method is not None:
        if auth_method not in ("userpass", "api_key"):
            raise ValueError("روش اتصال نامعتبر است")
        fields.append("auth_method = ?")
        params.append(auth_method)
    if not fields:
        return
    params.append(panel_id)
    with transaction() as cur:
        cur.execute(f"UPDATE vpn_panels SET {', '.join(fields)} WHERE id = ?", params)
    cache.invalidate_prefix("vpn_panels:")


def delete_vpn_panel(panel_id: int) -> None:
    with transaction() as cur:
        cur.execute("DELETE FROM panel_plan_map WHERE panel_id = ?", (panel_id,))
        cur.execute("DELETE FROM vpn_panels WHERE id = ?", (panel_id,))
    cache.invalidate_prefix("vpn_panels:")


# ---------------------------------------------------------------------------
# 🆕 نگاشت عمومی پلن/بسته → نمونه‌ی پنل (هر سه نوع پنل)
# ---------------------------------------------------------------------------
def set_panel_plan_map(scope: str, scope_id: int, panel_id: int, remote_ref: str, remote_name: str | None = None):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO panel_plan_map (scope, scope_id, panel_id, remote_ref, remote_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id) DO UPDATE SET
                   panel_id = excluded.panel_id, remote_ref = excluded.remote_ref, remote_name = excluded.remote_name""",
            (scope, scope_id, panel_id, remote_ref, remote_name, _now()),
        )


def get_panel_plan_map(scope: str, scope_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM panel_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id))
    return _fetchone(cur)


def get_panel_plan_map_with_panel(scope: str, scope_id: int) -> dict | None:
    """مانند get_panel_plan_map ولی اطلاعات نمونه‌ی پنل (نوع/آدرس/اطلاعات ورود) را هم
    برمی‌گرداند تا لایه ارتباط با پنل بتواند مستقیم درخواست بزند."""
    cur = get_connection().cursor()
    cur.execute(
        """SELECT m.scope, m.scope_id, m.remote_ref, m.remote_name, m.panel_id,
                  p.panel_type, p.name AS panel_name, p.base_url, p.api_key, p.username, p.password, p.enabled
           FROM panel_plan_map m JOIN vpn_panels p ON p.id = m.panel_id
           WHERE m.scope = ? AND m.scope_id = ?""",
        (scope, scope_id),
    )
    return _fetchone(cur)


def get_payg_mapping() -> dict | None:
    """نگاشت پنل PAYG: اول نگاشت اختصاصی PAYG، اگر نبود همان نگاشت «بساز سرویس خودت»."""
    return get_panel_plan_map_with_panel("payg", 0) or get_panel_plan_map_with_panel("custom_build", 0)


def delete_panel_plan_map(scope: str, scope_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM panel_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id))


# fix: وقتی ادمین یک نگاشت پیش‌فرض برای کل یک دسته‌بندی VIP تنظیم می‌کند
# (scope='vip_category')، باید نگاشت‌های اختصاصیِ قدیمی‌ترِ تک‌تک پلن‌های همان
# دسته (scope='vip_plan') هم پاک شوند. چون در get_panel_map_for_plan_key نگاشت
# اختصاصی پلن همیشه اولویت دارد، بدون این پاک‌سازی، تنظیم پیش‌فرض جدید دسته هیچ
# اثری روی پلن‌هایی که از قبل نگاشت اختصاصی داشتند نمی‌گذاشت (مثلاً هنوز از
# پنل شاهراه‌ی قدیمی استفاده می‌شد با اینکه پیش‌فرض دسته به پاسارگارد تغییر کرده بود).
def clear_panel_plan_overrides_for_category(category_id: int):
    plan_ids = [p["id"] for p in get_vip_plans(category_id)]
    if not plan_ids:
        return
    placeholders = ",".join("?" for _ in plan_ids)
    with transaction() as cur:
        cur.execute(
            f"DELETE FROM panel_plan_map WHERE scope = 'vip_plan' AND scope_id IN ({placeholders})",
            plan_ids,
        )


def list_panel_plan_maps(scope: str | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if scope:
        cur.execute("SELECT * FROM panel_plan_map WHERE scope = ?", (scope,))
    else:
        cur.execute("SELECT * FROM panel_plan_map")
    return _fetchall(cur)


def get_panel_map_for_plan_key(plan_key: str) -> dict | None:
    """نسخه‌ی عمومی‌شده‌ی get_shahrah_plan_map_for_plan_key که هر سه نوع پنل را پوشش
    می‌دهد و نتیجه را همراه با اطلاعات کامل همان نمونه‌ی پنل (برای اتصال/ساخت) برمی‌گرداند."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return get_panel_plan_map_with_panel("free_test", 0)
    _ft_num = free_test_entry_num(plan_key)
    if _ft_num is not None:
        return get_panel_plan_map_with_panel("free_test", _ft_num)

    vip_plan = get_vip_plan(plan_key)
    if vip_plan is not None:
        plan_map = get_panel_plan_map_with_panel("vip_plan", vip_plan["id"])
        if plan_map:
            return plan_map
        return get_panel_plan_map_with_panel("vip_category", vip_plan["category_id"])

    return None


def export_backup_json(path: str):
    """
    تمام جدول‌های دیتابیس را به یک فایل JSON خروجی می‌گیرد.
    وقتی از Turso استفاده می‌شود (بدون فایل محلی)، دکمه‌ی «💾 بکاپ» ادمین
    از همین تابع برای ساخت فایل بکاپ استفاده می‌کند.
    """
    conn = get_connection()
    cur = conn.cursor()
    tables = ["users", "transactions", "configs", "discounts", "discount_usages", "agents",
              "referrals", "custom_orders", "orders", "vip_categories", "vip_plans", "bot_admins", "admin_activity_logs", "ui_text_overrides", "ui_button_overrides", "ui_layouts"]
    data = {}
    for table in tables:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if cur.fetchone() is None:
            continue
        cur.execute(f"SELECT * FROM {table}")
        data[table] = _fetchall(cur)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 🚫 مسدودسازی کاربر (پیشنهاد خود AI — تا ادمین بتواند ترول‌زن/مزاحمه‌گر بی‌ادب
# را بدون خروج از دیتابیس بلاکه کند: کاربر مسدودشده دیگر نمی‌تواند با ربات کار کند
# و قبل از هر هندلر اصلی بلاک می‌شود (در handlers بررسی می‌شود).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 📢 پیام همگانی پیشرفته: کاربرانی که قبلاً Forbidden داده‌اند از لیست ارسال
# بعدی حذف می‌شوند (mark_bot_blocked)، و اگر کاربر دوباره با ربات تعامل کرد
# (مثلاً /start زد)، خودکار از حالت "بلاک" خارج می‌شود (clear_bot_blocked،
# از create_user/get_or_create صدا زده می‌شود تا وضعیت واقعی همیشه به‌روز بماند).
# ---------------------------------------------------------------------------
def mark_bot_blocked(telegram_id) -> None:
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET bot_blocked_at = ? WHERE telegram_id = ? AND bot_blocked_at IS NULL",
            (_now(), str(telegram_id)),
        )


def clear_bot_blocked(telegram_id) -> None:
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET bot_blocked_at = NULL WHERE telegram_id = ? AND bot_blocked_at IS NOT NULL",
            (str(telegram_id),),
        )



# ─── Audience labels for broadcast / gift / discount ─────────────────────────
AUDIENCE_LABELS: dict[str, str] = {
    "all":              "👥 همه کاربران",
    "buyers":           "🛒 خرید‌کاران",
    "non_buyers":       "🆕 بدون خرید",
    "wallet_positive":  "💰 کیف پول مثبت",
    "agents":           "🤝 نمایندگان",
    "active_service":   "🟢 سرویس فعال",
    "expired_service":  "🔴 سرویس منقضی",
    "new_users":        "🆕 کاربران جدید (7 روز)",
    "no_service":       "📵 بدون سرویس",
    "multi_service":    "📦 چند سرویس",
    "sufficient_wallet":"💳 موجودی کافی PAYG",
    "inactive":         "😴 کاربران غیرفعال",
    "has_test":         "🎁 دریافت‌کننده تست",
    "has_discount":     "🎟 دارای تخفیف",
}

def get_broadcastable_users(audience: str = "all", telegram_id=None, respect_prefs: bool = False) -> list[dict]:
    """لیست کاربران قابل ارسال بر اساس دسته‌بندی مخاطب (۱۴ نوع).

    audience values:
        all, buyers, non_buyers, wallet_positive, agents,
        active_service, expired_service, new_users, no_service,
        multi_service, sufficient_wallet, inactive, has_test, has_discount
    """
    cur = get_connection().cursor()
    base = "SELECT * FROM users WHERE COALESCE(is_blocked, 0) = 0 AND bot_blocked_at IS NULL"

    buyer_exists = (
        "EXISTS (SELECT 1 FROM orders o WHERE o.user_id = users.id "
        "AND o.order_type NOT IN ('wallet_charge','test') AND o.status IN ('paid','processing','fulfilled','completed')) "
        "OR EXISTS (SELECT 1 FROM custom_orders co WHERE co.user_id = users.id "
        "AND co.status IN ('paid','processing','fulfilled','completed'))"
    )
    live_cfg = "c.user_id = users.id AND COALESCE(c.deleted, 0) = 0"
    active_service = (
        f"EXISTS (SELECT 1 FROM configs c WHERE {live_cfg} "
        "AND (c.expiry IS NULL OR c.expiry = '' OR c.expiry > date('now')))"
    )
    any_service = f"EXISTS (SELECT 1 FROM configs c WHERE {live_cfg})"
    has_test_sql = (
        "EXISTS (SELECT 1 FROM orders o WHERE o.user_id = users.id AND o.order_type = 'test')"
    )
    has_discount_sql = (
        "EXISTS (SELECT 1 FROM discount_usages du WHERE du.user_id = users.id)"
    )
    from datetime import datetime as _dt, timedelta as _td
    week_ago = (_dt.utcnow() - _td(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    month_ago = (_dt.utcnow() - _td(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    recent_order = (
        f"EXISTS (SELECT 1 FROM orders o WHERE o.user_id = users.id AND o.created_at >= '{month_ago}') "
        f"OR EXISTS (SELECT 1 FROM custom_orders co WHERE co.user_id = users.id AND co.created_at >= '{month_ago}')"
    )

    conditions = {
        "all":              base,
        "buyers":           f"{base} AND ({buyer_exists})",
        "non_buyers":       f"{base} AND NOT ({buyer_exists})",
        "wallet_positive":  f"{base} AND COALESCE(wallet, 0) > 0",
        "agents":           f"{base} AND EXISTS (SELECT 1 FROM agents ag WHERE ag.telegram_id = users.telegram_id)",
        "active_service":   f"{base} AND ({active_service})",
        "expired_service":  f"{base} AND ({any_service}) AND NOT ({active_service})",
        "new_users":        f"{base} AND joined >= '{week_ago}'",
        "no_service":       f"{base} AND NOT ({any_service})",
        "multi_service":    (
            f"{base} AND (SELECT COUNT(*) FROM configs c WHERE {live_cfg}) > 1"
        ),
        "sufficient_wallet":(f"{base} AND COALESCE(wallet, 0) >= "
                             f"COALESCE((SELECT CAST(value AS REAL) FROM settings WHERE key='payg_min_balance'),50000)"),
        "inactive":         f"{base} AND joined < '{month_ago}' AND NOT ({recent_order}) AND NOT ({active_service})",
        "has_test":         f"{base} AND ({has_test_sql})",
        "has_discount":     f"{base} AND ({has_discount_sql})",
    }
    sql = conditions.get(audience, base)
    if telegram_id is not None:
        sql += " AND users.telegram_id = ?"
        cur.execute(sql, (int(telegram_id),))
    else:
        cur.execute(sql)
    rows = _fetchall(cur)
    if respect_prefs and rows:
        # promo messages must skip users who turned promo notifications off
        off = get_promo_optout_user_ids()
        if off:
            rows = [r for r in rows if r.get("id") not in off]
    return rows


def user_in_audience(telegram_id, audience: str) -> bool:
    """آیا این کاربر در دسته‌ی مخاطب (همان دسته‌های پیام همگانی) قرار می‌گیرد؟"""
    return bool(get_broadcastable_users(audience, telegram_id=telegram_id))


def gift_all_users(amount: int, description: str = "🎁 هدیه", audience: str = "all") -> int:
    """هدیه هدف‌مند: مبلغ را به کیف‌پول گروه انتخابی اضافه می‌کند.
    audience: یکی از مقادیر AUDIENCE_LABELS (پیش‌فرض all = همه).
    """
    if amount <= 0:
        raise ValueError("مبلغ هدیه باید بزرگ‌تر از صفر باشد")
    users = get_broadcastable_users(audience)
    user_ids = [u["id"] for u in users]
    if not user_ids:
        return 0
    now = _now()
    aud_label = AUDIENCE_LABELS.get(audience, audience)
    full_desc = f"{description} — {aud_label}" if audience != "all" else description
    with transaction() as cur:
        cur.executemany(
            "UPDATE users SET wallet = wallet + ? WHERE id = ?",
            [(amount, uid) for uid in user_ids],
        )
        cur.executemany(
            "INSERT INTO transactions (user_id, type, amount, status, description, created_at) "
            "VALUES (?, 'gift', ?, 'completed', ?, ?)",
            [(uid, amount, full_desc, now) for uid in user_ids],
        )
    cache.invalidate_prefix("user:")
    return len(user_ids)


def get_active_users(limit: int | None = None, offset: int = 0) -> list[dict]:
    """🆕 بند ۱۹ — «کاربران فعال» فقط کاربران غیرمسدود را برمی‌گرداند تا با
    لیست «کاربران مسدود» هم‌پوشانی نداشته باشد (قبلاً کاربر مسدود در هر دو
    لیست دیده می‌شد و گیج‌کننده بود)."""
    cur = get_connection().cursor()
    sql = "SELECT * FROM users WHERE COALESCE(is_blocked, 0) = 0 ORDER BY id DESC"
    if limit is not None:
        cur.execute(sql + " LIMIT ? OFFSET ?", (limit, offset))
    else:
        cur.execute(sql)
    return _fetchall(cur)


def count_unblocked_users() -> int:
    """🐛 فیکس: این تابع قبلاً هم‌نام با count_active_users(days=30) بالاتر در
    فایل بود (دو تعریف با یک اسم) — در پایتون تعریف دوم به‌طور خاموش تعریف
    اول را override می‌کرد، یعنی هر جا count_active_users(30) صدا زده
    می‌شد (مثلاً صفحه‌ی آمار قدیمی) با TypeError کرش می‌کرد چون این نسخه
    اصلاً پارامتر days را قبول نمی‌کرد. اینجا با یک اسم جدا نگه داشته شده
    تا هر دو تابع مستقل و سالم کار کنند."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(is_blocked, 0) = 0")
    return _fetchone(cur)["c"]


def set_user_blocked(telegram_id, blocked: bool):
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET is_blocked = ? WHERE telegram_id = ?",
            (1 if blocked else 0, str(telegram_id)),
        )
    cache.invalidate(f"user:{telegram_id}")


def is_user_blocked(telegram_id) -> bool:
    """Hot-path check used by middleware on every incoming update.

    Do not perform another database query here: get_user() already has an
    in-process cache. Keeping this function tiny is important because it runs
    before virtually every handler.
    """
    user = get_user(telegram_id)
    return bool(user and user.get("is_blocked"))


# ---------------------------------------------------------------------------
# 👥 لیست کامل دعوت‌کنندگان (بر اساس بیشترین تعداد دعوت) + لیست افرادی که
# هر نفر دعوت کرده — برای بخش جدید «🌟 مدیریت دعوت‌شده‌ها» در پنل ادمین.
# ---------------------------------------------------------------------------
def count_referrers() -> int:
    """تعداد کاربرانی که حداقل یک نفر دعوت کرده‌اند."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE invited_count > 0")
    return _fetchone(cur)["c"]


def get_referrers_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ای از کاربرانی که حداقل یک نفر دعوت کرده‌اند، مرتب‌شده بر اساس
    بیشترین تعداد دعوت (invited_count)."""
    cur = get_connection().cursor()
    cur.execute(
        """SELECT * FROM users WHERE invited_count > 0
           ORDER BY invited_count DESC, successful_invites DESC, id DESC
           LIMIT ? OFFSET ?""",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


def get_referred_users(referrer_id: int) -> list[dict]:
    """لیست همه‌ی کاربرانی که توسط referrer_id دعوت شده‌اند، به‌همراه وضعیت و پاداش
    هر دعوت (از جدول referrals) و اطلاعات اصلی خود کاربر (از جدول users)."""
    cur = get_connection().cursor()
    cur.execute(
        """SELECT u.*, r.reward AS referral_reward, r.status AS referral_status,
                  r.created_at AS referral_created_at
           FROM referrals r
           JOIN users u ON u.id = r.invited_id
           WHERE r.referrer_id = ?
           ORDER BY r.created_at DESC""",
        (referrer_id,),
    )
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# 📚 مدیریت راهنماها / آموزش‌ها — CRUD کامل برای پنل ادمین
# ---------------------------------------------------------------------------
def _guide_row(row: dict | None) -> dict | None:
    """ستون JSON‌ی body_entities را به لیست تبدیل می‌کند."""
    if row is None:
        return None
    raw = row.get("body_entities")
    try:
        row["body_entities"] = json.loads(raw) if raw else []
    except Exception:
        row["body_entities"] = []
    return row


def create_guide(title: str, content_type: str = "text", body_text: str | None = None,
                  file_id: str | None = None, body_entities: list | None = None) -> dict:
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM guides")
        next_order = _fetchone(cur)["m"] + 1
        cur.execute(
            """INSERT INTO guides (title, content_type, body_text, file_id, sort_order, created_at, body_entities)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, content_type, body_text, file_id, next_order, _now(),
             json.dumps(body_entities or [], ensure_ascii=False)),
        )
        new_id = cur.lastrowid
    return get_guide(new_id)


def get_guides() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM guides ORDER BY sort_order ASC, id ASC")
    return [_guide_row(r) for r in _fetchall(cur)]


def get_guide(guide_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM guides WHERE id = ?", (guide_id,))
    return _guide_row(_fetchone(cur))


def update_guide(guide_id: int, title: str | None = None, content_type: str | None = None,
                  body_text: str | None = None, file_id: str | None = None,
                  body_entities: list | None = None):
    guide = get_guide(guide_id)
    if not guide:
        return
    new_entities = body_entities if body_entities is not None else guide.get("body_entities")
    with transaction() as cur:
        cur.execute(
            """UPDATE guides SET title = ?, content_type = ?, body_text = ?, file_id = ?, body_entities = ?
               WHERE id = ?""",
            (
                title if title is not None else guide["title"],
                content_type if content_type is not None else guide["content_type"],
                body_text if body_text is not None else guide["body_text"],
                file_id if file_id is not None else guide["file_id"],
                json.dumps(new_entities or [], ensure_ascii=False),
                guide_id,
            ),
        )


def delete_guide(guide_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM guides WHERE id = ?", (guide_id,))


def move_guide(guide_id: int, direction: str):
    """جابجایی جایگاه یک آیتم راهنما در لیست (direction: 'up' یا 'down')، با
    جابجایی sort_order با ایتم همسایه."""
    guides = get_guides()
    idx = next((i for i, g in enumerate(guides) if g["id"] == guide_id), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        other = guides[idx - 1]
    elif direction == "down" and idx < len(guides) - 1:
        other = guides[idx + 1]
    else:
        return
    current = guides[idx]
    with transaction() as cur:
        cur.execute("UPDATE guides SET sort_order = ? WHERE id = ?", (other["sort_order"], current["id"]))
        cur.execute("UPDATE guides SET sort_order = ? WHERE id = ?", (current["sort_order"], other["id"]))


# ---------------------------------------------------------------------------
# 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو (پنل ادمین)
# ---------------------------------------------------------------------------
def get_section_sticker(section_key: str) -> dict | None:
    """ردیف سفارشی‌شده‌ی این بخش را برمی‌گرداند؛ None یعنی ادمین هنوز آن را سفارشی
    نکرده و باید از استیکر پیش‌فرض داخل پروژه استفاده شود."""
    # perf: تقریباً هر بار که کاربر وارد یک بخش می‌شود این تابع خوانده می‌شود.
    cache_key = f"section_sticker:one:{section_key}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM section_stickers WHERE section_key = ?", (section_key,))
    value = _fetchone(cur)
    cache.set(cache_key, value)
    return value


def get_all_section_stickers() -> list[dict]:
    cache_key = "section_sticker:all"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM section_stickers")
    value = _fetchall(cur)
    cache.set(cache_key, value)
    return value


def _invalidate_section_sticker_cache(section_key: str):
    cache.invalidate(f"section_sticker:one:{section_key}")
    cache.invalidate("section_sticker:all")


def set_section_sticker(section_key: str, file_id: str):
    """یک استیکر/ویدیوی سفارشی برای این بخش ثبت می‌کند و آن را (دوباره) فعال می‌کند."""
    with transaction() as cur:
        cur.execute(
            """INSERT INTO section_stickers (section_key, file_id, is_enabled, updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(section_key) DO UPDATE SET
                   file_id = excluded.file_id,
                   is_enabled = 1,
                   updated_at = excluded.updated_at""",
            (section_key, file_id, _now()),
        )
    _invalidate_section_sticker_cache(section_key)


def set_section_sticker_enabled(section_key: str, enabled: bool):
    """این بخش را فعال/غیرفعال می‌کند؛ فایل استیکر موجود (اگر باشد) حفظ می‌شود."""
    existing = get_section_sticker(section_key)
    file_id = existing["file_id"] if existing else None
    with transaction() as cur:
        cur.execute(
            """INSERT INTO section_stickers (section_key, file_id, is_enabled, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(section_key) DO UPDATE SET
                   is_enabled = excluded.is_enabled,
                   updated_at = excluded.updated_at""",
            (section_key, file_id, 1 if enabled else 0, _now()),
        )
    _invalidate_section_sticker_cache(section_key)


def reset_section_sticker(section_key: str):
    """رکورد سفارشی این بخش را کامل حذف می‌کند تا به حالت پیش‌فرض (استیکر داخل
    پروژه) برگردد."""
    with transaction() as cur:
        cur.execute("DELETE FROM section_stickers WHERE section_key = ?", (section_key,))
    _invalidate_section_sticker_cache(section_key)


# ---------------------------------------------------------------------------
# 🦖 لاگ خطاها (همان رویدادهایی که به Sentry هم ارسال می‌شود)
# ---------------------------------------------------------------------------
def log_error(error_type: str, message: str | None = None, traceback_text: str | None = None, context: str | None = None):
    """یک خطای تازه رو محلی ثبت می‌کند (همزمان با ارسال به Sentry توسط global_error_handler)
    تا ادمین بدون نیاز به داشبورد Sentry هم بتونه از داخل پنل ادمین ربات ببینه‌شون.
    هر خطایی در این تابع نباید توقف کنه اجرای اصلی ربات رو، پس همیشه در try/except محافظت‌شده فراخوانده میشه."""
    try:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO error_logs (error_type, message, traceback, context, occurred_at) VALUES (?, ?, ?, ?, ?)",
                (error_type, (message or "")[:2000], (traceback_text or "")[:8000], (context or "")[:500], _now()),
            )
            # فقط آخرین ۵۰۰ خطا رو نگه دار (تا دیتابیس بی‌نهایت بزرگ نشه)
            cur.execute(
                "DELETE FROM error_logs WHERE id NOT IN (SELECT id FROM error_logs ORDER BY id DESC LIMIT 500)"
            )
    except Exception:
        pass


def get_error_logs(limit: int = 20, offset: int = 0) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM error_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    return _fetchall(cur)


def get_error_log(log_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM error_logs WHERE id = ?", (log_id,))
    return _fetchone(cur)


def count_error_logs() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM error_logs")
    row = cur.fetchone()
    return row[0] if row else 0


def clear_error_logs():
    with transaction() as cur:
        cur.execute("DELETE FROM error_logs")

# ---------------------------------------------------------------------------
# Multi-admin / sub-admin permissions
# ---------------------------------------------------------------------------
from permissions import PERMISSION_LABELS
ADMIN_PERMISSIONS = PERMISSION_LABELS.copy()

_admin_tables_ready = False


def ensure_admin_tables():
    # perf: این تابع قبلاً در ابتدای get_sub_admin/is_sub_admin (که خودش روی
    # *هر* پیام خروجی ربات، برای هر کاربر، صدا زده می‌شه — نگاه کن به پچ
    # send_message در bot.py) هر بار CREATE TABLE IF NOT EXISTS اجرا می‌کرد؛
    # یعنی هر پیام ربات چند رفت‌وبرگشت اضافه به دیتابیس داشت فقط برای همین
    # چک تکراری. الان فقط یک‌بار در طول عمر پروسه واقعاً اجرا می‌شه.
    global _admin_tables_ready
    if _admin_tables_ready:
        return
    with transaction() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                telegram_id TEXT PRIMARY KEY,
                name TEXT,
                permissions TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT NOT NULL,
                admin_name TEXT,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
    _admin_tables_ready = True

def add_sub_admin(telegram_id: str, name: str = "", permissions: list[str] | None = None):
    ensure_admin_tables()
    with transaction() as cur:
        cur.execute("""INSERT INTO bot_admins(telegram_id,name,permissions,is_active,created_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name, permissions=excluded.permissions, is_active=1""",
                    (str(telegram_id), name or str(telegram_id), json.dumps(permissions or [], ensure_ascii=False), 1, _now()))
    cache.invalidate(f"subadmin:{telegram_id}")
    cache.invalidate("subadmin:all")

def delete_sub_admin(telegram_id: str):
    ensure_admin_tables()
    with transaction() as cur:
        cur.execute("DELETE FROM bot_admins WHERE telegram_id=?", (str(telegram_id),))
    cache.invalidate(f"subadmin:{telegram_id}")
    cache.invalidate("subadmin:all")

def get_sub_admin(telegram_id: str):
    # perf: این تابع (از طریق is_sub_admin) روی *هر* پیام خروجی ربات برای هر
    # کاربر صدا زده می‌شه (چک اینکه گیرنده ادمینه یا نه، قبل از اعمال
    # override متن). چون تقریباً همیشه نتیجه‌اش «کاربر عادیه» است و ادمین‌های
    # فرعی به‌ندرت تغییر می‌کنن، این نتیجه رو کش می‌کنیم تا این پرتکرارترین
    # کوئری ربات دیگه هر بار به دیتابیس نره.
    cache_key = f"subadmin:{telegram_id}"
    cached_value, hit = cache.get(cache_key)
    if hit:
        return cached_value
    ensure_admin_tables()
    cur=get_connection().cursor(); cur.execute("SELECT * FROM bot_admins WHERE telegram_id=? AND is_active=1", (str(telegram_id),))
    row=_fetchone(cur)
    if row:
        try: row["permissions"]=json.loads(row.get("permissions") or "[]")
        except Exception: row["permissions"]=[]
    cache.set(cache_key, row)
    return row

def get_all_sub_admins():
    ensure_admin_tables()
    cur=get_connection().cursor(); cur.execute("SELECT * FROM bot_admins WHERE is_active=1 ORDER BY created_at DESC")
    rows=_fetchall(cur)
    for row in rows:
        try: row["permissions"]=json.loads(row.get("permissions") or "[]")
        except Exception: row["permissions"]=[]
    return rows

def update_sub_admin_permissions(telegram_id: str, permissions: list[str]):
    ensure_admin_tables()
    with transaction() as cur:
        cur.execute("UPDATE bot_admins SET permissions=? WHERE telegram_id=?", (json.dumps(sorted(set(permissions)), ensure_ascii=False), str(telegram_id)))
    cache.invalidate(f"subadmin:{telegram_id}")

def is_sub_admin(telegram_id: str) -> bool:
    return get_sub_admin(str(telegram_id)) is not None

def sub_admin_has_permission(telegram_id: str, permission: str) -> bool:
    adm=get_sub_admin(str(telegram_id))
    return bool(adm and permission in (adm.get("permissions") or []))

def log_admin_action(admin_id: str, admin_name: str, action: str, target: str = "", details: str = ""):
    ensure_admin_tables()
    with transaction() as cur:
        cur.execute("INSERT INTO admin_activity_logs(admin_id,admin_name,action,target,details,created_at) VALUES(?,?,?,?,?,?)",
                    (str(admin_id), admin_name or str(admin_id), action, target or "", details or "", _now()))
    audit_event("admin", action, "target", target or "", details or "", actor_id=str(admin_id), actor_name=admin_name)

def get_recent_admin_logs(limit: int = 50):
    ensure_admin_tables(); cur=get_connection().cursor(); cur.execute("SELECT * FROM admin_activity_logs ORDER BY id DESC LIMIT ?", (limit,)); return _fetchall(cur)


# ---------------------------------------------------------------------------
# Compatibility helpers used by the current admin panel.
# These helpers intentionally reuse the existing DB/schema and never drop or
# rewrite user data during startup.
# ---------------------------------------------------------------------------
def count_non_customers() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(total_purchase, 0) <= 0")
    return int(_fetchone(cur)["c"])


def get_pending_receipt_by_id(receipt_id: int):
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM pending_receipts WHERE id = ? LIMIT 1", (int(receipt_id),))
    return _fetchone(cur)


def resolve_pending_receipt_by_id(receipt_id: int, outcome: str | None = None) -> bool:
    with transaction() as cur:
        cur.execute(
            "UPDATE pending_receipts SET status = 'resolved' WHERE id = ? AND status = 'pending'",
            (int(receipt_id),),
        )
        ok = bool(cur.rowcount)
    if ok and outcome:
        _adv_hook("receipt_decided", int(receipt_id), outcome)
    return ok


def get_gaming_files(config_id: int):
    # Gaming tables are optional in this V2.19 build. Older admin code may
    # still ask for them; return an empty list when the legacy table is absent.
    cur = get_connection().cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gaming_files'")
    if cur.fetchone() is None:
        return []
    cur.execute("SELECT * FROM gaming_files WHERE config_id = ?", (int(config_id),))
    return _fetchall(cur)


def get_text_override(key: str, default: str = "") -> str:
    try:
        import ui_editor
        return ui_editor.get_text(key, default)
    except Exception:
        cur = get_connection().cursor()
        try:
            cur.execute("SELECT text FROM ui_text_overrides WHERE key=?", (str(key),))
            row = cur.fetchone()
            return row[0] if row else default
        except Exception:
            return default


def set_text_override(key: str, value: str, entities=None) -> None:
    import ui_editor
    ui_editor.set_text(str(key), str(value), entities=entities or [])

def get_text_override_entities(key: str) -> list:
    """مستقیم از جدول entities_json می‌خواند — بدون وابستگی به ui_editor"""
    try:
        import json
        cur = get_connection().cursor()
        cur.execute("SELECT entities_json FROM ui_text_overrides WHERE key=?", (str(key),))
        row = cur.fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return []
        return []
    except Exception:
        return []



def get_button_custom_emoji_id(key: str):
    try:
        cur = get_connection().cursor()
        cur.execute("SELECT custom_emoji_id FROM ui_legacy_emoji_meta WHERE key=? LIMIT 1", (str(key),))
        row = cur.fetchone()
        if row:
            return row[0]
    except Exception:
        pass
    return None


def set_button_custom_emoji_id(key: str, custom_emoji_id) -> None:
    with transaction() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ui_legacy_emoji_meta (
            key TEXT PRIMARY KEY, custom_emoji_id TEXT
        )""")
        cur.execute(
            "INSERT INTO ui_legacy_emoji_meta(key,custom_emoji_id) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET custom_emoji_id=excluded.custom_emoji_id",
            (str(key), str(custom_emoji_id) if custom_emoji_id else None),
        )


def set_keyboard_hidden(user_id: int, hidden: bool) -> None:
    # V2.19 keeps the permanent reply keyboard visible. The old admin call is
    # retained as a harmless compatibility hook.
    return None


def delete_user_from_bot(telegram_id: str) -> bool:
    """Delete one bot user while respecting the existing FK graph.

    This only touches bot database rows. No external VPN panel API is called.
    """
    tid = str(telegram_id)
    with transaction() as cur:
        cur.execute("SELECT id, invite_code FROM users WHERE telegram_id=?", (tid,))
        row = _fetchone(cur)
        if row is None:
            return False
        uid = int(row["id"])

        # Self-referential referral links must be nulled before deleting users.
        cur.execute("UPDATE users SET referrer_id=NULL WHERE referrer_id=?", (uid,))

        # Delete known child records first. Tables that are optional are simply skipped.
        child_specs = [
            ("transactions", "user_id"), ("configs", "user_id"), ("orders", "user_id"),
            ("online_payments", "user_id"), ("pending_receipts", "user_id"),
            ("invoices", "user_id"), ("custom_orders", "user_id"), ("tickets", "user_id"),
            ("discount_usages", "user_id"), ("referrals", "referrer_id"), ("referrals", "referred_id"),
        ]
        tables = {r["name"] for r in _fetchall(cur.execute("SELECT name FROM sqlite_master WHERE type='table'"))}
        for table, column in child_specs:
            if table not in tables:
                continue
            try:
                cur.execute(f"DELETE FROM {table} WHERE {column}=?", (uid,))
            except Exception:
                # Some legacy tables use telegram_id instead of internal user id.
                if table == "pending_receipts":
                    cur.execute("DELETE FROM pending_receipts WHERE telegram_id=?", (tid,))
                elif table == "online_payments":
                    try:
                        cur.execute("DELETE FROM online_payments WHERE telegram_id=?", (tid,))
                    except Exception:
                        pass

        # حذف کامل: هر جدول دیگری که ستون user_id / telegram_id دارد هم پاک می‌شود (بقایای کاربر نماند).
        _done = {t for t, _c in child_specs}
        for table in sorted(tables):
            if table == "users" or table.startswith("sqlite_"):
                continue
            try:
                cols = {r["name"] for r in _fetchall(cur.execute(f"PRAGMA table_info({table})"))}
            except Exception:
                continue
            for col, val in (("user_id", uid), ("telegram_id", tid)):
                if col in cols and not (table in _done and col == "user_id"):
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE {col}=?", (val,))
                    except Exception:
                        pass

        cur.execute("DELETE FROM users WHERE id=?", (uid,))
        _ok = bool(cur.rowcount)
    try:
        cache.invalidate(f"user:{tid}")
        cache.invalidate(f"user_id:{uid}")
        _ic = (row.get("invite_code") if isinstance(row, dict) else None)
        if _ic:
            cache.invalidate(f"user_invite:{str(_ic).upper()}")
    except Exception:
        pass
    return _ok


# ═══════════════════════════════════════════════════════════════════════════════
# 💱 CRYPTO BACKUP PRICE  (بند ۲۳)
# ═══════════════════════════════════════════════════════════════════════════════

def get_crypto_backup_price(symbol: str) -> float | None:
    """قیمت دستی Backup برای یک ارز دیجیتال."""
    val = get_setting(f"crypto_backup_price_{symbol.upper()}")
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


def set_crypto_backup_price(symbol: str, price: float) -> None:
    """ذخیره قیمت دستی Backup برای یک ارز دیجیتال."""
    set_setting(f"crypto_backup_price_{symbol.upper()}", str(price))


def get_crypto_backup_enabled(symbol: str) -> bool:
    """آیا Fallback این ارز فعال است؟"""
    return get_setting(f"crypto_backup_enabled_{symbol.upper()}", "1") == "1"


def set_crypto_backup_enabled(symbol: str, enabled: bool) -> None:
    set_setting(f"crypto_backup_enabled_{symbol.upper()}", "1" if enabled else "0")


# ═══════════════════════════════════════════════════════════════════════════════
# ⚡ PAYG CATEGORY / PLAN PRICING  (بند ۷)
# ═══════════════════════════════════════════════════════════════════════════════

def get_payg_category_price(category_id: int) -> int | None:
    """قیمت PAYG مخصوص دسته‌بندی (تومان/GB). None = از قیمت عمومی استفاده شود."""
    val = get_setting(f"payg_catprice_{category_id}")
    try:
        v = int(val)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def set_payg_category_price(category_id: int, price: int) -> None:
    set_setting(f"payg_catprice_{category_id}", str(price))


def get_payg_plan_price(plan_key: str) -> int | None:
    """قیمت PAYG مخصوص پلن (تومان/GB). None = از دسته‌بندی یا عمومی استفاده شود."""
    val = get_setting(f"payg_planprice_{plan_key}")
    try:
        v = int(val)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def set_payg_plan_price(plan_key: str, price: int) -> None:
    set_setting(f"payg_planprice_{plan_key}", str(price))


def resolve_payg_price_per_gb(plan_key: str | None = None, category_id: int | None = None, option_num: int | None = None) -> int:
    """اولویت: قیمت اختصاصی «پلن PAYG» (گزینه‌ی نگاشت) > پلن > دسته‌بندی > عمومی (قدیمی)."""
    if option_num is not None:
        p = get_map_option_cfg("payg", int(option_num)).get("price_per_gb")
        if p:
            return int(p)
    if plan_key:
        p = get_payg_plan_price(plan_key)
        if p:
            return p
    if category_id:
        p = get_payg_category_price(category_id)
        if p:
            return p
    settings = get_payg_settings()
    return int(settings.get("price_per_gb", 5000))


# ═══════════════════════════════════════════════════════════════════════════════
# 🎁 FREE TEST PANELS  (بند ۱۴)
# ═══════════════════════════════════════════════════════════════════════════════

def get_free_test_panels() -> list[dict]:
    """لیست پنل‌های تست چندگانه."""
    import json as _json
    val = get_setting("free_test_panels", "[]")
    try:
        items = _json.loads(val)
    except Exception:
        return []
    # شماره‌ی پایدار (num) برای هر پلن تست؛ نگاشت پنل و کلید پلن (plan_test__N) روی آن بنا شده.
    used = {int(p.get("num")) for p in items if str(p.get("num") or "").isdigit()}
    changed = False
    nxt = (max(used) if used else 0) + 1
    for p in items:
        if not str(p.get("num") or "").isdigit():
            p["num"] = nxt; nxt += 1; changed = True
    if changed:
        set_setting("free_test_panels", _json.dumps(items, ensure_ascii=False))
    return items


def save_free_test_panels(panels: list[dict]) -> None:
    import json as _json
    set_setting("free_test_panels", _json.dumps(panels, ensure_ascii=False))


def add_free_test_panel(panel_data: dict) -> str:
    """اضافه کردن پنل تست جدید. خروجی: ID پنل."""
    import uuid as _uuid
    panels = get_free_test_panels()
    pid = str(_uuid.uuid4())[:8]
    panel_data["id"] = pid
    panel_data["num"] = (max([int(p.get("num") or 0) for p in panels] or [0])) + 1
    panel_data.setdefault("enabled", True)
    panels.append(panel_data)
    save_free_test_panels(panels)
    return pid


def toggle_free_test_panel(panel_id: str) -> bool | None:
    """Toggle وضعیت پنل. خروجی: وضعیت جدید یا None اگر پیدا نشد."""
    panels = get_free_test_panels()
    for p in panels:
        if p.get("id") == panel_id:
            p["enabled"] = not p.get("enabled", True)
            save_free_test_panels(panels)
            return p["enabled"]
    return None


def remove_free_test_panel(panel_id: str) -> bool:
    panels = get_free_test_panels()
    new_panels = [p for p in panels if p.get("id") != panel_id]
    if len(new_panels) == len(panels):
        return False
    for p in panels:
        if p.get("id") == panel_id and p.get("num"):
            try:
                delete_panel_plan_map("free_test", int(p["num"]))
            except Exception:
                pass
    save_free_test_panels(new_panels)
    return True


def get_enabled_free_test_panels() -> list[dict]:
    """فقط پنل‌های فعال برای نمایش به کاربر."""
    return [p for p in get_free_test_panels() if p.get("enabled", True)]


# ---------------------------------------------------------------------------
# بند ۱۳ — Rich Text Template CRUD
# ---------------------------------------------------------------------------
def get_rich_text_templates() -> list[dict]:
    """تمام قالب‌های Rich Text را برمی‌گرداند."""
    cur = get_connection().cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS rich_text_templates "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, "
        "content TEXT NOT NULL, emoji_pack TEXT DEFAULT NULL, "
        "created_at TEXT DEFAULT (datetime('now')), "
        "updated_at TEXT DEFAULT (datetime('now')))"
    )
    get_connection().commit()
    cur.execute("SELECT * FROM rich_text_templates ORDER BY id DESC")
    return _fetchall(cur)


def get_rich_text_template(template_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM rich_text_templates WHERE id = ?", (template_id,))
    return _fetchone(cur)


def create_rich_text_template(label: str, content: str) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rich_text_templates (label, content) VALUES (?, ?)",
        (label, content),
    )
    conn.commit()
    return get_rich_text_template(cur.lastrowid)


def update_rich_text_template(template_id: int, label: str | None = None, content: str | None = None) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    if label is not None:
        cur.execute("UPDATE rich_text_templates SET label=?, updated_at=datetime('now') WHERE id=?", (label, template_id))
    if content is not None:
        cur.execute("UPDATE rich_text_templates SET content=?, updated_at=datetime('now') WHERE id=?", (content, template_id))
    conn.commit()
    return cur.rowcount > 0


def delete_rich_text_template(template_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM rich_text_templates WHERE id = ?", (template_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# روش‌های پرداخت: وضعیت کلی + فعال/غیرفعال‌بودن اختصاصی «شارژ کیف پول»
# ---------------------------------------------------------------------------
TOPUP_METHODS = ("card", "online", "crypto", "frenzyex", "tronado", "tonpays")


def payment_method_flags() -> dict:
    """وضعیت واقعی هر روش پرداخت از دید کاربر (برای خرید). رمزارز فقط وقتی فعال است که
    کلید کلی رمزارز روشن باشد و حداقل یک ارز فعال وجود داشته باشد."""
    import json as _json
    try:
        pm_card = _json.loads(get_setting("pm_card", "{}") or "{}")
    except Exception:
        pm_card = {}
    try:
        pm_up = _json.loads(get_setting("pm_uniquepay", "{}") or "{}")
    except Exception:
        pm_up = {}
    try:
        pm_cr = _json.loads(get_setting("pm_cryptos", "[]") or "[]")
    except Exception:
        pm_cr = []
    try:
        from uniquepay_cfg import enabled as _upf
        _UP = bool(_upf())
    except Exception:
        _UP = False
    master = str(get_setting("pm_crypto_master", "1") or "1") != "0"
    try:
        pm_fx = _json.loads(get_setting("pm_frenzyex", "{}") or "{}")
    except Exception:
        pm_fx = {}
    try:
        pm_tr = _json.loads(get_setting("pm_tronado", "{}") or "{}")
    except Exception:
        pm_tr = {}
    try:
        pm_tp = _json.loads(get_setting("pm_tonpays", "{}") or "{}")
    except Exception:
        pm_tp = {}
    return {
        "tonpays": bool(pm_tp.get("enabled", False) and str(pm_tp.get("api_key") or "").strip()),
        "card": bool(pm_card.get("enabled", True)),
        "online": bool((pm_up.get("enabled", False) and (pm_up.get("business_token") or "").strip()) or _UP),
        "crypto": bool(master and any(bool(c.get("enabled", False) and str(c.get("wallet") or "").strip()) for c in pm_cr)),
        # درگاه‌های جدید: فعال‌بودن + اطلاعات حداقلی لازم برای ساخت فاکتور
        "frenzyex": bool(pm_fx.get("enabled", False) and str(pm_fx.get("api_key") or "").strip()),
        "tronado": bool(
            pm_tr.get("enabled", False)
            and str(pm_tr.get("api_key") or "").strip()
            and str(pm_tr.get("wallet_address") or "").strip()
        ),
    }


def get_topup_switches() -> dict:
    import json as _json
    try:
        raw = _json.loads(get_setting("pm_topup", "{}") or "{}")
    except Exception:
        raw = {}
    return {m: bool(raw.get(m, True)) for m in TOPUP_METHODS}


def toggle_topup_switch(method: str) -> bool:
    import json as _json
    if method not in TOPUP_METHODS:
        raise ValueError(method)
    sw = get_topup_switches()
    sw[method] = not sw[method]
    set_setting("pm_topup", _json.dumps(sw))
    return sw[method]


def available_topup_methods() -> list[str]:
    """روش‌هایی که همین الان کاربر می‌تواند برای شارژ کیف پول ببیند (فعال بودن کلی × سوییچ شارژ)."""
    flags = payment_method_flags()
    sw = get_topup_switches()
    return [m for m in TOPUP_METHODS if flags.get(m) and sw.get(m)]


# ---------------------------------------------------------------------------
# 🔌 گزینه‌های نگاشت چندگانه برای «PAYG» و «بساز سرویس خودت»
# هر گزینه = نام نمایشی + نگاشت به یک پنل (scope=kind، scope_id=num).
# num=0 همان نگاشت قدیمی (تک‌نگاشتی) است و بدون نیاز به مهاجرت کار می‌کند.
# ---------------------------------------------------------------------------
MAP_OPTION_KINDS = ("payg", "custom_build")


def _map_options_raw(kind: str) -> list[dict]:
    import json as _json
    try:
        items = _json.loads(get_setting(f"map_options_{kind}", "[]") or "[]")
    except Exception:
        items = []
    return [i for i in items if isinstance(i, dict) and str(i.get("num", "")).lstrip("-").isdigit()]


def _save_map_options(kind: str, items: list[dict]) -> None:
    import json as _json
    set_setting(f"map_options_{kind}", _json.dumps(items, ensure_ascii=False))


def get_map_options(kind: str) -> list[dict]:
    """لیست گزینه‌ها؛ اگر هیچ گزینه‌ای ثبت نشده ولی نگاشت قدیمی (scope_id=0) هست، یک گزینه‌ی مجازی «پیش‌فرض» برمی‌گردد."""
    items = _map_options_raw(kind)
    if not items and get_panel_plan_map(kind, 0):
        items = [{"num": 0, "name": "پیش‌فرض"}]
    out = []
    for it in items:
        num = int(it["num"])
        out.append({"num": num, "name": str(it.get("name") or f"گزینه {num}"), "mapped": bool(get_panel_plan_map(kind, num))})
    return out


def add_map_option(kind: str, name: str) -> int:
    items = _map_options_raw(kind)
    if not items and get_panel_plan_map(kind, 0):
        items = [{"num": 0, "name": "پیش‌فرض"}]
    num = (max([int(i["num"]) for i in items] or [0])) + 1
    items.append({"num": num, "name": name.strip()[:40]})
    _save_map_options(kind, items)
    return num


def rename_map_option(kind: str, num: int, name: str) -> None:
    items = _map_options_raw(kind)
    if not items and int(num) == 0:
        items = [{"num": 0, "name": "پیش‌فرض"}]
    for it in items:
        if int(it["num"]) == int(num):
            it["name"] = name.strip()[:40]
    _save_map_options(kind, items)


def remove_map_option(kind: str, num: int) -> None:
    items = [i for i in _map_options_raw(kind) if int(i["num"]) != int(num)]
    _save_map_options(kind, items)
    delete_panel_plan_map(kind, int(num))
    try:
        clear_map_option_cfg(kind, int(num))
    except Exception:
        pass


def get_map_option(kind: str, num: int) -> dict | None:
    return next((o for o in get_map_options(kind) if o["num"] == int(num)), None)


def get_option_mapping(kind: str, num: int | None) -> dict | None:
    """نگاشت کامل (همراه اطلاعات پنل) یک گزینه. برای PAYG گزینه‌ی ۰ در نبود نگاشت، به «بساز سرویس خودت» برمی‌گردد."""
    num = int(num or 0)
    m = get_panel_plan_map_with_panel(kind, num)
    if m:
        return m
    if kind == "payg" and num == 0:
        return get_panel_plan_map_with_panel("custom_build", 0)
    return None


def usable_map_options(kind: str, panel_types: tuple | None = None) -> list[dict]:
    """گزینه‌هایی که نگاشت دارند و پنلشان فعال است (برای نمایش به کاربر)."""
    out = []
    for o in get_map_options(kind):
        m = get_option_mapping(kind, o["num"])
        if not m or not m.get("enabled"):
            continue
        if panel_types and m.get("panel_type") not in panel_types:
            continue
        out.append(o)
    if not out and kind == "payg":
        m = get_option_mapping("payg", 0)
        if m and m.get("enabled") and (not panel_types or m.get("panel_type") in panel_types):
            out.append({"num": 0, "name": "پیش‌فرض", "mapped": True})
    return out


# ---------------------------------------------------------------------------
# Mini app: usage snapshots + user notification preferences
# ---------------------------------------------------------------------------
_PREF_KEYS = ("usage_alerts", "expiry_alerts", "wallet_alerts", "promo")


def record_usage_snapshot(config_id: int, used, day: str | None = None) -> None:
    """Store the max used-bytes seen for (config, day). Never raises."""
    try:
        if used is None:
            return
        used = int(used)
        if used < 0:
            return
        day = day or datetime.now().strftime("%Y-%m-%d")
        with transaction() as cur:
            cur.execute(
                "INSERT INTO service_usage_snapshots (config_id, day, used) VALUES (?, ?, ?) "
                "ON CONFLICT(config_id, day) DO UPDATE SET used = MAX(used, excluded.used)",
                (int(config_id), day, used),
            )
    except Exception:
        logging.getLogger(__name__).exception("record_usage_snapshot failed")


def get_usage_snapshots(config_id: int, since_day: str | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if since_day:
        cur.execute("SELECT day, used FROM service_usage_snapshots WHERE config_id = ? AND day >= ? ORDER BY day ASC", (int(config_id), since_day))
    else:
        cur.execute("SELECT day, used FROM service_usage_snapshots WHERE config_id = ? ORDER BY day ASC", (int(config_id),))
    return _fetchall(cur)


def get_user_prefs(user_id: int) -> dict:
    out = {k: True for k in _PREF_KEYS}
    out["default_app"] = None
    try:
        cur = get_connection().cursor()
        cur.execute("SELECT * FROM user_prefs WHERE user_id = ?", (int(user_id),))
        row = _fetchone(cur)
    except Exception:
        row = None
    if row:
        for k in _PREF_KEYS:
            out[k] = bool(row.get(k, 1))
        out["default_app"] = row.get("default_app") or None
    return out


def set_user_prefs(user_id: int, notifications: dict | None = None, default_app=...) -> dict:
    cur_prefs = get_user_prefs(user_id)
    for k in _PREF_KEYS:
        if notifications and k in notifications and notifications[k] is not None:
            cur_prefs[k] = bool(notifications[k])
    if default_app is not ...:
        cur_prefs["default_app"] = (str(default_app).strip()[:40] or None) if default_app else None
    with transaction() as cur:
        cur.execute(
            "INSERT INTO user_prefs (user_id, usage_alerts, expiry_alerts, wallet_alerts, promo, default_app) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "usage_alerts=excluded.usage_alerts, expiry_alerts=excluded.expiry_alerts, "
            "wallet_alerts=excluded.wallet_alerts, promo=excluded.promo, default_app=excluded.default_app",
            (int(user_id), int(cur_prefs["usage_alerts"]), int(cur_prefs["expiry_alerts"]),
             int(cur_prefs["wallet_alerts"]), int(cur_prefs["promo"]), cur_prefs["default_app"]),
        )
    return cur_prefs


def get_promo_optout_user_ids() -> set:
    try:
        cur = get_connection().cursor()
        cur.execute("SELECT user_id FROM user_prefs WHERE promo = 0")
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def user_pref_enabled(user_id=None, key: str = "promo", telegram_id=None) -> bool:
    """True unless the user explicitly turned this notification type off. Fails open."""
    try:
        if key not in _PREF_KEYS:
            return True
        if user_id is None and telegram_id is not None:
            u = get_user(telegram_id)
            if not u:
                return True
            user_id = u["id"]
        if user_id is None:
            return True
        cur = get_connection().cursor()
        cur.execute(f"SELECT {key} FROM user_prefs WHERE user_id = ?", (int(user_id),))
        row = cur.fetchone()
        return True if row is None else bool(row[0])
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 🏷 نام سرویس انتخابی کاربر (حالت «نامگذاری توسط کاربر»)
# ---------------------------------------------------------------------------
def service_name_taken(name: str, exclude_config_id: int | None = None) -> bool:
    """آیا این نام/یوزرنیم قبلاً برای یک سرویس، سفارش یا رسیدِ در انتظار استفاده شده؟ (بدون حساسیت به حروف)"""
    name = str(name or "").strip().lower()
    if not name:
        return False
    cur = get_connection().cursor()
    queries = [
        ("SELECT 1 FROM configs WHERE deleted = 0 AND (lower(COALESCE(service_name,'')) = ? OR lower(COALESCE(service_id,'')) = ?)"
         + (" AND id != ?" if exclude_config_id is not None else "") + " LIMIT 1",
         (name, name) + ((int(exclude_config_id),) if exclude_config_id is not None else ())),
        ("SELECT 1 FROM orders WHERE lower(COALESCE(service_name,'')) = ? AND COALESCE(status,'pending') NOT IN ('rejected','dismissed','failed') LIMIT 1", (name,)),
        ("SELECT 1 FROM pending_receipts WHERE lower(COALESCE(service_name,'')) = ? AND status = 'pending' LIMIT 1", (name,)),
    ]
    for sql, params in queries:
        try:
            cur.execute(sql, params)
            if cur.fetchone() is not None:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# گزارشات پیشرفته: بعد از commit شدن finalizeهای پرداخت آنلاین، رویداد «paid» را گزارش کن.
# ---------------------------------------------------------------------------
def _wrap_finalize_for_adv(fn):
    import functools

    @functools.wraps(fn)
    def inner(payment_id, *a, **kw):
        res = fn(payment_id, *a, **kw)
        try:
            if res and res[1]:
                _adv_hook("payment_event", int(payment_id), "paid")
        except Exception:
            pass
        return res
    return inner


finalize_online_plan_payment_atomic = _wrap_finalize_for_adv(finalize_online_plan_payment_atomic)
finalize_online_custom_payment_atomic = _wrap_finalize_for_adv(finalize_online_custom_payment_atomic)
finalize_online_wallet_payment_atomic = _wrap_finalize_for_adv(finalize_online_wallet_payment_atomic)
