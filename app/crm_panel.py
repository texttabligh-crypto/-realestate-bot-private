import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import bcrypt
import psycopg
from composite_search import composite_text_predicates
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut
from visit_ops_group import get_visit_ops_target, telegram_thread_kwargs

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
TEHRAN = ZoneInfo("Asia/Tehran")

PERMISSION_LABELS = {
    "channels_manage": "مدیریت کانال‌ها",
    "publish_urgent": "انتشار فوری",
    "files_manage": "مدیریت فایل‌ها و صف",
    "smart_search": "جستجوی هوشمند",
    "unit_search": "جستجوی کد واحد",
    "category_send": "ارسال دسته‌بندی",
    "stats_view": "مشاهده آمار و هشتگ‌ها",
    "schedule_manage": "مدیریت زمان‌بندی انتشار",
    "settings_publish": "تنظیمات انتشار",
    "customers_manage": "👥 مشاهده و ثبت مشتریان / خواهان‌ها",
    "visits_manage_own": "مدیریت بازدید مشتریان خود",
    "logs_view": "مشاهده لاگ‌ها",
    "receive_visit_reminders": "⏰ دریافت یادآوری‌های بازدید",
    "receive_visit_requests": "🔔 دریافت درخواست‌های جدید بازدید",
    "manual_property_add": "افزودن فایل دستی",
    "stats_team_view": "مشاهده آمار کل تیم",
    "reports_export": "دانلود PDF فایل‌های فعال",
    "channel_post_edit": "✏️ ویرایش فایل‌های منتشرشده کانال",
    "removed_files_view": "🧹 فایل‌های حذف‌شده",
    "duplicate_files_view": "🔁 فایل‌های تکراری",
    "instagram_story": "📸 ساخت استوری اینستاگرام",
}

RESTRICTED_DEFAULT_OFF_KEYS = {"receive_visit_requests", "receive_visit_reminders", "reports_export", "channel_post_edit", "removed_files_view", "duplicate_files_view", "instagram_story"}
ADMIN_DEFAULTS = set(PERMISSION_LABELS) - RESTRICTED_DEFAULT_OFF_KEYS
ADVISOR_DEFAULTS = {
    "smart_search",
    "unit_search",
    "customers_manage",
    "visits_manage_own",
}

ACTION_PERMISSIONS = [
    (("add_channel", "add_type_", "sources"), "channels_manage"),
    (("urgent_", "urgent_publish"), "publish_urgent"),
    (("files_menu", "force_delete", "republish", "republish_", "republish_group",
      "duplicate_queue", "dupqueue_", "queue", "queue_", "trash_", "file_validity_"), "files_manage"),
    (("smart_search", "smart_search_", "smart_filter_"), "smart_search"),
    (("search_unit",), "unit_search"),
    (("category_",), "category_send"),
    (("stats", "hashtags"), "stats_view"),
    (("schedule", "schedule_"), "schedule_manage"),
    (("settings", "settings_", "setting_edit_", "attribute_rule"), "settings_publish"),
    (("logs",), "logs_view"),
    (("manual_",), "manual_property_add"),
    (("export_properties_pdf",), "reports_export"),
    (("channel_edit",), "channel_post_edit"),
    (("removed_files",), "removed_files_view"),
    (("duplicate_files",), "duplicate_files_view"),
    (("insta_story",), "instagram_story"),
]

def db_connect():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )

def ensure_crm_schema():
    # Main migration is run by the installer. This is a defensive,
    # idempotent boot-time check for tables that are safe to create.
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_permissions (
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    permission_key VARCHAR(100) NOT NULL,
                    is_allowed BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, permission_key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    action VARCHAR(120) NOT NULL,
                    target_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    visit_id BIGINT REFERENCES visit_requests(id) ON DELETE SET NULL,
                    details JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS region_category_posts (
                    id BIGSERIAL PRIMARY KEY,
                    region_name VARCHAR(255) NOT NULL,
                    page_offset INTEGER NOT NULL DEFAULT 0,
                    page_size INTEGER NOT NULL DEFAULT 10,
                    telegram_channel_id BIGINT NOT NULL,
                    telegram_message_id BIGINT NOT NULL,
                    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()

def is_owner(session):
    return bool(session and session[3] == "OWNER")

def default_permissions_for_role(role):
    if role == "OWNER":
        return set(PERMISSION_LABELS)
    if role == "ADMIN":
        return set(ADMIN_DEFAULTS)
    return set(ADVISOR_DEFAULTS)

def get_user_permissions(user_id, role):
    if role == "OWNER":
        return set(PERMISSION_LABELS)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT permission_key, is_allowed
                FROM user_permissions
                WHERE user_id = %s
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return default_permissions_for_role(role)
    return {key for key, allowed in rows if allowed}

def has_permission(session, key):
    if not session:
        return False
    if is_owner(session):
        return True
    return key in get_user_permissions(session[0], session[3])

def _required_permission(action):
    if action in ("home",):
        return None
    if action in ("users", "settings_users", "reset") or action.startswith("db_backup"):
        return "OWNER_ONLY"
    if action.startswith("crm_"):
        if action.startswith("crm_visit"):
            return "visits_manage_own"
        if action.startswith("crm_customer") or action == "crm_customers":
            return "customers_manage"
        if action.startswith("crm_user"):
            return "OWNER_ONLY"
        if action.startswith("crm_category_"):
            return "category_send"
    for prefixes, key in ACTION_PERMISSIONS:
        if any(action == p or action.startswith(p) for p in prefixes):
            return key
    return None

def check_action_access(session, action):
    required = _required_permission(action or "")
    if required is None:
        return True
    if required == "OWNER_ONLY":
        return is_owner(session)
    return has_permission(session, required)

def _filter_keyboard(session, keyboard):
    if not keyboard:
        return keyboard
    rows = []
    for row in keyboard.inline_keyboard:
        new_row = []
        for button in row:
            action = button.callback_data or ""
            if not action or check_action_access(session, action):
                new_row.append(button)
        if new_row:
            rows.append(new_row)
    return rows

def build_main_menu(session, legacy_keyboard):
    rows = _filter_keyboard(session, legacy_keyboard)
    extra = []
    if has_permission(session, "customers_manage"):
        extra.append(
            InlineKeyboardButton("👥 باشگاه مشتریان", callback_data="crm_customers")
        )
    if has_permission(session, "visits_manage_own"):
        extra.append(
            InlineKeyboardButton("📅 مدیریت بازدید", callback_data="crm_visits")
        )
    if extra:
        # Put CRM before logs / last row where possible.
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, extra)
    if has_permission(session, "manual_property_add"):
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, [InlineKeyboardButton("➕ افزودن فایل", callback_data="manual_add")])
    if has_permission(session, "reports_export"):
        insert_at = max(0, len(rows) - 1)
        rows.insert(
            insert_at,
            [InlineKeyboardButton("📄 دانلود PDF فایل‌ها", callback_data="export_properties_pdf")],
        )
    return InlineKeyboardMarkup(rows)

def build_settings_menu(session, legacy_keyboard):
    rows = [list(row) for row in (legacy_keyboard.inline_keyboard if legacy_keyboard else [])]
    if is_owner(session):
        insert_at = max(0, len(rows) - 1)
        rows.insert(
            insert_at,
            [InlineKeyboardButton("👤 مدیریت کاربران و دسترسی‌ها", callback_data="settings_users")],
        )
    return InlineKeyboardMarkup(rows)

def audit(actor_user_id, action, *, target_user_id=None, customer_id=None, visit_id=None, details=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_audit_log
                    (actor_user_id, action, target_user_id, customer_id, visit_id, details)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    actor_user_id,
                    action,
                    target_user_id,
                    customer_id,
                    visit_id,
                    json.dumps(details or {}, ensure_ascii=False),
                ),
            )
        conn.commit()

def _main_back():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")]]
    )

def _settings_back():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ مدیریت کاربران", callback_data="settings_users")]]
    )

def _normalize_digits(value):
    return str(value or "").translate(str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    ))

def normalize_phone(value):
    digits = re.sub(r"\D+", "", _normalize_digits(value))
    if digits.startswith("0098"):
        digits = "0" + digits[4:]
    elif digits.startswith("98") and len(digits) >= 12:
        digits = "0" + digits[2:]
    elif digits.startswith("9") and len(digits) == 10:
        digits = "0" + digits
    return digits

def _user_rows():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, display_name, role, is_active
                FROM users
                ORDER BY
                    CASE role WHEN 'OWNER' THEN 0 WHEN 'ADMIN' THEN 1 ELSE 2 END,
                    id
            """)
            return cur.fetchall()

def _users_keyboard():
    rows = [
        [InlineKeyboardButton("➕ افزودن کاربر", callback_data="crm_user_add")]
    ]
    for user_id, username, display_name, role, active in _user_rows():
        icon = "✅" if active else "⛔"
        title = display_name or username
        role_fa = {"OWNER": "مالک", "ADMIN": "ادمین", "ADVISOR": "مشاور"}.get(role, role)
        rows.append([
            InlineKeyboardButton(
                f"{icon} {title} | {role_fa}",
                callback_data=f"crm_user_view_{user_id}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ تنظیمات", callback_data="settings")])
    return InlineKeyboardMarkup(rows)

def _get_user(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, display_name, role, is_active
                FROM users
                WHERE id = %s
            """, (user_id,))
            return cur.fetchone()

def _permission_keyboard(user_id, role):
    allowed = get_user_permissions(user_id, role)
    rows = []
    for key, label in PERMISSION_LABELS.items():
        icon = "✅" if key in allowed else "❌"
        rows.append([
            InlineKeyboardButton(
                f"{icon} {label}",
                callback_data=f"crm_user_perm_{user_id}_{key}",
            )
        ])
    rows.append([
        InlineKeyboardButton("⬅️ کاربران", callback_data="settings_users")
    ])
    return InlineKeyboardMarkup(rows)

def _user_text(row):
    user_id, username, display_name, role, active = row
    role_fa = {"OWNER": "مالک", "ADMIN": "ادمین", "ADVISOR": "مشاور"}.get(role, role)
    return (
        f"👤 کاربر #{user_id}\n\n"
        f"نام: {display_name or '-'}\n"
        f"نام کاربری: {username}\n"
        f"نقش: {role_fa}\n"
        f"وضعیت: {'فعال ✅' if active else 'غیرفعال ⛔'}\n\n"
        "دسترسی‌ها را از دکمه‌های زیر روشن یا خاموش کنید."
    )

def _create_user(display_name, username, password_hash, role):
    defaults = default_permissions_for_role(role)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (username, password_hash, display_name, role, is_active)
                VALUES (%s, %s, %s, %s, TRUE)
                RETURNING id
            """, (username, password_hash, display_name, role))
            user_id = cur.fetchone()[0]
            for key in PERMISSION_LABELS:
                cur.execute("""
                    INSERT INTO user_permissions (user_id, permission_key, is_allowed, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (user_id, permission_key)
                    DO UPDATE SET is_allowed = EXCLUDED.is_allowed, updated_at = NOW()
                """, (user_id, key, key in defaults))
        conn.commit()
    return user_id

def _toggle_permission(user_id, key):
    user = _get_user(user_id)
    if not user or user[3] == "OWNER" or key not in PERMISSION_LABELS:
        return None
    current = key in get_user_permissions(user_id, user[3])
    new_value = not current
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_permissions (user_id, permission_key, is_allowed, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, permission_key)
                DO UPDATE SET is_allowed = EXCLUDED.is_allowed, updated_at = NOW()
            """, (user_id, key, new_value))
        conn.commit()
    return new_value

def _toggle_user_active(user_id):
    user = _get_user(user_id)
    if not user or user[3] == "OWNER":
        return None
    new_value = not bool(user[4])
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET is_active = %s, updated_at = NOW()
                WHERE id = %s
            """, (new_value, user_id))
            if not new_value:
                cur.execute("""
                    UPDATE user_sessions
                    SET is_active = FALSE
                    WHERE user_id = %s
                """, (user_id,))
        conn.commit()
    return new_value

def _customer_scope(session):
    if is_owner(session):
        return "", []
    return " AND cp.owner_user_id = %s ", [session[0]]

def _customer_count(session):
    scope, params = _customer_scope(session)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM customer_profiles cp WHERE 1=1 " + scope,
                params,
            )
            return int(cur.fetchone()[0] or 0)

def _list_customers(session, offset=0, limit=20):
    scope, params = _customer_scope(session)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(NULLIF(BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), ''), 'بدون نام'),
                    cp.phone_number,
                    cp.owner_user_id,
                    COALESCE(u.display_name, u.username, 'دفتر')
                FROM customer_profiles cp
                LEFT JOIN users u ON u.id = cp.owner_user_id
                WHERE 1=1
                """ + scope + """
                ORDER BY cp.updated_at DESC, cp.id DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            return cur.fetchall()

def _customers_keyboard(session, offset=0, limit=20):
    total = _customer_count(session)
    rows = []
    for customer_id, name, phone, owner_id, owner_name in _list_customers(session, offset, limit):
        phone_text = phone or "بدون شماره"
        rows.append([
            InlineKeyboardButton(
                f"📞 {phone_text} | {name}"[:60],
                callback_data=f"crm_customer_view_{customer_id}",
            )
        ])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"crm_customers_page_{max(0, offset-limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"crm_customers_page_{offset+limit}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("➕ افزودن مشتری", callback_data="crm_customer_add"),
        InlineKeyboardButton("🔎 جستجوی مشتری", callback_data="crm_customer_search"),
    ])
    rows.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")])
    return InlineKeyboardMarkup(rows), total

def _search_customers(session, query, limit=20):
    scope, scope_params = _customer_scope(session)
    needle = f"%{query.strip()}%"
    phone = normalize_phone(query)
    params = [needle, needle, needle]
    phone_clause = ""
    if phone:
        phone_clause = " OR COALESCE(cp.phone_normalized, '') ILIKE %s "
        params.append(f"%{phone}%")
    params += scope_params
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(NULLIF(BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), ''), 'بدون نام'),
                    cp.phone_number,
                    cp.owner_user_id,
                    COALESCE(u.display_name, u.username, 'دفتر')
                FROM customer_profiles cp
                LEFT JOIN users u ON u.id = cp.owner_user_id
                WHERE (
                    COALESCE(cp.first_name, '') ILIKE %s
                    OR COALESCE(cp.last_name, '') ILIKE %s
                    OR COALESCE(cp.phone_number, '') ILIKE %s
                    """ + phone_clause + """
                )
                """ + scope + """
                ORDER BY cp.updated_at DESC, cp.id DESC
                LIMIT %s
                """,
                params + [limit],
            )
            return cur.fetchall()

def _customer_detail(session, customer_id):
    scope, params = _customer_scope(session)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    cp.first_name,
                    cp.last_name,
                    cp.phone_number,
                    cp.telegram_username,
                    cp.notes,
                    cp.owner_user_id,
                    COALESCE(u.display_name, u.username, 'دفتر'),
                    cp.created_at,
                    cp.last_interaction_at
                FROM customer_profiles cp
                LEFT JOIN users u ON u.id = cp.owner_user_id
                WHERE cp.id = %s
                """ + scope + """
                LIMIT 1
                """,
                [customer_id] + params,
            )
            return cur.fetchone()

def _customer_text(row):
    (
        customer_id, first_name, last_name, phone, tg_username,
        notes, owner_user_id, owner_name, created_at, last_interaction_at
    ) = row
    name = " ".join(x for x in (first_name, last_name) if x).strip() or "بدون نام"
    return (
        f"👤 مشتری #{customer_id}\n\n"
        f"نام: {name}\n"
        f"📞 شماره: {phone or '-'}\n"
        f"تلگرام: @{tg_username}" if tg_username else
        f"👤 مشتری #{customer_id}\n\nنام: {name}\n📞 شماره: {phone or '-'}\nتلگرام: -"
    ) + (
        f"\nمشاور مسئول: {owner_name}\n"
        f"یادداشت: {notes or '-'}"
    )

def _customer_keyboard(session, customer_id):
    rows = [[
        InlineKeyboardButton("📅 ثبت بازدید", callback_data=f"crm_visit_for_customer_{customer_id}")
    ]]
    if is_owner(session):
        rows.append([
            InlineKeyboardButton("👤 واگذاری به مشاور", callback_data=f"crm_customer_assign_{customer_id}")
        ])
    rows.append([
        InlineKeyboardButton("⬅️ مشتریان", callback_data="crm_customers")
    ])
    return InlineKeyboardMarkup(rows)

def _find_customer_by_phone(phone_normalized):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, owner_user_id
                FROM customer_profiles
                WHERE phone_normalized = %s
                ORDER BY id
                LIMIT 1
            """, (phone_normalized,))
            return cur.fetchone()

def _create_manual_customer(session, name, phone):
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        raise ValueError("شماره تماس نامعتبر است.")
    existing = _find_customer_by_phone(phone_norm)
    if existing:
        if is_owner(session) or existing[1] == session[0]:
            return existing[0], False
        raise ValueError("این شماره قبلاً برای مشتری دیگری در سیستم ثبت شده است.")
    parts = name.strip().split(maxsplit=1)
    first_name = parts[0] if parts else None
    last_name = parts[1] if len(parts) > 1 else None
    owner_id = session[0]
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_profiles (
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                    phone_number,
                    phone_normalized,
                    owner_user_id,
                    created_by_user_id,
                    source_label,
                    created_at,
                    updated_at,
                    last_interaction_at
                )
                VALUES (
                    NULL, NULL, %s, %s, %s, %s, %s, %s,
                    'MANUAL', NOW(), NOW(), NOW()
                )
                RETURNING id
            """, (first_name, last_name, phone, phone_norm, owner_id, session[0]))
            customer_id = cur.fetchone()[0]
        conn.commit()
    return customer_id, True

def _assignable_users():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, COALESCE(display_name, username), role
                FROM users
                WHERE is_active = TRUE
                  AND role IN ('ADMIN', 'ADVISOR')
                ORDER BY role, id
            """)
            return cur.fetchall()

def _assign_customer(customer_id, user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE customer_profiles
                SET owner_user_id = %s, updated_at = NOW()
                WHERE id = %s
            """, (user_id, customer_id))
            cur.execute("""
                UPDATE visit_requests
                SET assigned_user_id = COALESCE(assigned_user_id, %s),
                    updated_at = NOW()
                WHERE customer_id = %s
                  AND status IN ('NEW','CONTACTED','SCHEDULED')
            """, (user_id, customer_id))
        conn.commit()

def _visit_scope(session):
    if is_owner(session):
        return "", []
    return """
        AND (
            vr.assigned_user_id = %s
            OR cp.owner_user_id = %s
        )
    """, [session[0], session[0]]

def _visit_rows(session, limit=30):
    scope, params = _visit_scope(session)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    vr.id,
                    vr.status,
                    vr.visit_at,
                    vr.unit_code_snapshot,
                    cp.id,
                    COALESCE(NULLIF(BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), ''), 'بدون نام'),
                    cp.phone_number,
                    COALESCE(u.display_name, u.username, 'دفتر')
                FROM visit_requests vr
                JOIN customer_profiles cp ON cp.id = vr.customer_id
                LEFT JOIN users u ON u.id = COALESCE(vr.assigned_user_id, cp.owner_user_id)
                WHERE vr.status IN ('NEW','CONTACTED','SCHEDULED')
                """ + scope + """
                ORDER BY
                    CASE WHEN vr.visit_at IS NULL THEN 0 ELSE 1 END,
                    vr.visit_at ASC NULLS FIRST,
                    vr.requested_at DESC
                LIMIT %s
                """,
                params + [limit],
            )
            return cur.fetchall()

def _visits_keyboard(session):
    rows = [[
        InlineKeyboardButton("➕ ثبت بازدید", callback_data="crm_visit_add")
    ]]
    for visit_id, status, visit_at, unit_code, customer_id, name, phone, owner in _visit_rows(session):
        if visit_at:
            local = visit_at.astimezone(TEHRAN)
            time_text = local.strftime("%m/%d %H:%M")
        else:
            time_text = "درخواست جدید"
        rows.append([
            InlineKeyboardButton(
                f"📅 {time_text} | {name} | {unit_code or '-'}"[:60],
                callback_data=f"crm_visit_view_{visit_id}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def _visit_detail(session, visit_id):
    scope, params = _visit_scope(session)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    vr.id, vr.status, vr.visit_at, vr.unit_code_snapshot,
                    vr.channel_post_url_snapshot, vr.notes, vr.reminder_sent_at,
                    cp.id, cp.first_name, cp.last_name, cp.phone_number,
                    COALESCE(u.display_name, u.username, 'دفتر')
                FROM visit_requests vr
                JOIN customer_profiles cp ON cp.id = vr.customer_id
                LEFT JOIN users u ON u.id = COALESCE(vr.assigned_user_id, cp.owner_user_id)
                WHERE vr.id = %s
                """ + scope + """
                LIMIT 1
                """,
                [visit_id] + params,
            )
            return cur.fetchone()

def _visit_text(row):
    (
        visit_id, status, visit_at, unit_code, post_url, notes, reminder_sent_at,
        customer_id, first_name, last_name, phone, assigned_name
    ) = row
    name = " ".join(x for x in (first_name, last_name) if x).strip() or "بدون نام"
    if visit_at:
        local = visit_at.astimezone(TEHRAN)
        when = local.strftime("%Y-%m-%d %H:%M")
    else:
        when = "هنوز زمان تعیین نشده"
    status_fa = {
        "NEW": "درخواست جدید",
        "CONTACTED": "تماس گرفته شد",
        "SCHEDULED": "زمان‌بندی‌شده",
        "CLOSED": "انجام‌شده",
        "CANCELLED": "لغوشده",
    }.get(status, status)
    return (
        f"📅 بازدید #{visit_id}\n\n"
        f"وضعیت: {status_fa}\n"
        f"مشتری: {name}\n"
        f"📞 {phone or '-'}\n"
        f"مشاور: {assigned_name}\n"
        f"کد واحد: {unit_code or '-'}\n"
        f"زمان بازدید: {when}\n"
        f"یادداشت: {notes or '-'}"
        + (f"\n🔗 فایل: {post_url}" if post_url else "")
    )

def _visit_keyboard(visit_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ انجام شد", callback_data=f"crm_visit_done_{visit_id}"),
            InlineKeyboardButton("❌ لغو", callback_data=f"crm_visit_cancel_{visit_id}"),
        ],
        [InlineKeyboardButton("⬅️ بازدیدها", callback_data="crm_visits")],
    ])

def _gregorian_from_jalali(jy, jm, jd):
    # Compact integer conversion, valid for modern Jalali dates.
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += ((jm - 7) * 30) + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    sal_a = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 1
    while gm <= 12 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return gy, gm, gd

def parse_visit_datetime(value):
    text = _normalize_digits(value).strip()
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    y, mo, d, hh, mm = map(int, m.groups())
    try:
        if y < 1700:
            y, mo, d = _gregorian_from_jalali(y, mo, d)
        dt = datetime(y, mo, d, hh, mm, tzinfo=TEHRAN)
        return dt
    except ValueError:
        return None

def _find_property_by_unit(unit_code):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, unit_code, channel_post_url
                FROM properties
                WHERE unit_code = %s
                  AND status = 'ACTIVE'
                LIMIT 1
            """, (unit_code,))
            return cur.fetchone()

def _schedule_visit(customer_id, property_row, visit_at, assigned_user_id, created_by_user_id):
    property_id = property_row[0] if property_row else None
    unit_code = property_row[1] if property_row else None
    post_url = property_row[2] if property_row else None
    with db_connect() as conn:
        with conn.cursor() as cur:
            # Reuse the newest open request for the same customer/property where possible.
            if property_id is not None:
                cur.execute("""
                    SELECT id
                    FROM visit_requests
                    WHERE customer_id = %s
                      AND property_id = %s
                      AND status IN ('NEW','CONTACTED','SCHEDULED')
                    ORDER BY id DESC
                    LIMIT 1
                """, (customer_id, property_id))
                found = cur.fetchone()
            else:
                found = None
            if found:
                visit_id = found[0]
                cur.execute("""
                    UPDATE visit_requests
                    SET status = 'SCHEDULED',
                        visit_at = %s,
                        assigned_user_id = %s,
                        created_by_user_id = COALESCE(created_by_user_id, %s),
                        unit_code_snapshot = COALESCE(unit_code_snapshot, %s),
                        channel_post_url_snapshot = COALESCE(channel_post_url_snapshot, %s),
                        reminder_sent_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                """, (visit_at, assigned_user_id, created_by_user_id, unit_code, post_url, visit_id))
            else:
                cur.execute("""
                    INSERT INTO visit_requests (
                        customer_id, property_id, unit_code_snapshot, channel_post_url_snapshot,
                        status, source, requested_at, visit_at, assigned_user_id,
                        created_by_user_id, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'SCHEDULED', 'MANUAL',
                            NOW(), %s, %s, %s, NOW(), NOW())
                    RETURNING id
                """, (
                    customer_id, property_id, unit_code, post_url,
                    visit_at, assigned_user_id, created_by_user_id
                ))
                visit_id = cur.fetchone()[0]
        conn.commit()
    return visit_id

def _set_visit_status(visit_id, status):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE visit_requests
                SET status = %s,
                    closed_at = CASE WHEN %s IN ('CLOSED','CANCELLED') THEN NOW() ELSE closed_at END,
                    updated_at = NOW()
                WHERE id = %s
            """, (status, status, visit_id))
        conn.commit()

def _region_rows(region, offset=0, limit=10):
    clauses, params = composite_text_predicates(region)
    predicate = " AND ".join(clauses) or "FALSE"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id, p.unit_code, p.district, p.address_text, p.area_m2,
                    p.bedrooms, p.build_year, p.current_price_billion,
                    p.current_price_toman, p.channel_post_url, p.published_message_id
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND (""" + predicate + """)
                ORDER BY p.current_price_billion ASC NULLS LAST, p.id ASC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            return cur.fetchall()


def _region_count(region):
    clauses, params = composite_text_predicates(region)
    predicate = " AND ".join(clauses) or "FALSE"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND (""" + predicate + """)
            """, params)
            return int(cur.fetchone()[0] or 0)


def _destination_channel():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT telegram_channel_id, username
                FROM channels
                WHERE channel_type = 'DESTINATION'
                  AND is_active = TRUE
                ORDER BY id
                LIMIT 1
            """)
            return cur.fetchone()

def _build_region_text(region, rows, offset=0):
    safe_tag = re.sub(r"\s+", "_", region.strip())
    lines = [f"#منطقه_{safe_tag}", "", f"📍 فایل‌های منطقه {region.strip()}", ""]
    dest = _destination_channel()
    dest_username = (dest[1] if dest else None)
    if dest_username:
        dest_username = str(dest_username).lstrip("@")
    for index, row in enumerate(rows, start=offset + 1):
        (
            property_id, unit_code, district, address_text, area_m2,
            bedrooms, build_year, price_billion, price_toman,
            channel_post_url, published_message_id
        ) = row
        location = address_text or district or region
        lines.append(f"{index}) 📍 {location}")
        details = []
        if area_m2 is not None:
            details.append(f"📐 {float(area_m2):g} متر")
        if bedrooms is not None:
            details.append(f"🛏 {int(bedrooms)} خواب")
        if build_year is not None:
            details.append(f"📅 {int(build_year)}")
        if details:
            lines.append(" | ".join(details))
        if price_billion is not None:
            lines.append(f"💰 قیمت: {float(price_billion):g} میلیارد تومان")
        if unit_code:
            lines.append(f"🔢 کد واحد: {unit_code}")
        url = channel_post_url
        if dest_username and published_message_id:
            url = f"https://t.me/{dest_username}/{published_message_id}"
        if url:
            lines.append(f"🔗 مشاهده فایل: {url}")
        lines.extend(["", "──────────────", ""])
    return "\n".join(lines).rstrip("─\n ")

def _region_nav(post_id, offset, total, page_size=10):
    if total <= page_size:
        return None
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"regcat_{post_id}_{max(0, offset-page_size)}"))
    row.append(InlineKeyboardButton(
        f"صفحه {offset//page_size+1} از {(total+page_size-1)//page_size}",
        callback_data=f"regcat_{post_id}_{offset}",
    ))
    if offset + page_size < total:
        row.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"regcat_{post_id}_{offset+page_size}"))
    return InlineKeyboardMarkup([row])

def _record_region_post(region, channel_id, message_id, user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO region_category_posts
                    (region_name, page_offset, page_size, telegram_channel_id,
                     telegram_message_id, created_by_user_id, is_active, created_at, updated_at)
                VALUES (%s, 0, 10, %s, %s, %s, TRUE, NOW(), NOW())
                RETURNING id
            """, (region, channel_id, message_id, user_id))
            post_id = cur.fetchone()[0]
        conn.commit()
    return post_id

async def region_category_navigation_handler(update, context):
    query = update.callback_query
    if not query:
        return
    action = query.data or ""
    if not action.startswith("regcat_"):
        return
    try:
        _, post_id, offset = action.split("_", 2)
        post_id = int(post_id)
        offset = max(0, int(offset))
    except Exception:
        await query.answer("صفحه نامعتبر است.", show_alert=True)
        return
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT region_name, page_size, telegram_channel_id,
                       telegram_message_id, is_active
                FROM region_category_posts
                WHERE id = %s
            """, (post_id,))
            meta = cur.fetchone()
    if not meta or not meta[4]:
        await query.answer("این دسته‌بندی دیگر فعال نیست.", show_alert=True)
        return
    region, page_size, channel_id, message_id, _ = meta
    total = _region_count(region)
    if total <= 0:
        await query.answer("فایلی باقی نمانده است.", show_alert=True)
        return
    last_offset = ((total - 1) // page_size) * page_size
    offset = min(offset, last_offset)
    rows = _region_rows(region, offset, page_size)
    text = _build_region_text(region, rows, offset)
    keyboard = _region_nav(post_id, offset, total, page_size)

    # === REGION NAV EARLY CALLBACK ACK V1 ===
    # Acknowledge before long Telegram edit/retry work.
    try:
        await query.answer(
            "⏳ در حال تغییر صفحه..."
        )
    except Exception as exc:
        logging.debug(
            "Region navigation early callback ACK failed: %s",
            exc,
        )

    try:
        await _edit_region_category_with_retry(
            context.bot,
            chat_id=channel_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logging.exception("Region category navigation failed")
            logging.warning(
                "Region category navigation retries exhausted: "
                "post_id=%s offset=%s",
                post_id,
                offset,
            )
            return
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE region_category_posts
                SET page_offset = %s, updated_at = NOW()
                WHERE id = %s
            """, (offset, post_id))
        conn.commit()

async def _edit_region_category_with_retry(
    bot,
    *,
    chat_id,
    message_id,
    text,
    reply_markup,
    max_attempts=4,
):
    """
    Edit region-category messages with automatic retry for
    Telegram Flood Control and temporary timeouts.
    """
    attempt = 0

    while True:
        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )

        except RetryAfter as exc:
            attempt += 1

            retry_after = getattr(
                exc,
                "retry_after",
                1,
            )

            if hasattr(
                retry_after,
                "total_seconds",
            ):
                wait_seconds = float(
                    retry_after.total_seconds()
                )
            else:
                wait_seconds = float(
                    retry_after
                )

            wait_seconds = max(
                1.0,
                wait_seconds,
            ) + 1.0

            logging.warning(
                "Telegram flood control during region category edit: "
                "message_id=%s attempt=%s/%s wait=%.1fs",
                message_id,
                attempt,
                max_attempts,
                wait_seconds,
            )

            if attempt >= max_attempts:
                raise

            await asyncio.sleep(
                wait_seconds
            )

        except TimedOut:
            attempt += 1

            wait_seconds = min(
                1.0 * attempt,
                3.0,
            )

            logging.warning(
                "Telegram timeout during region category edit: "
                "message_id=%s attempt=%s/%s wait=%.1fs",
                message_id,
                attempt,
                max_attempts,
                wait_seconds,
            )

            if attempt >= max_attempts:
                raise

            await asyncio.sleep(
                wait_seconds
            )


# === TARGETED REGION CATEGORY SYNC V1 ===
async def sync_region_category_posts(
    bot,
    region_names=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, region_name, page_offset, page_size,
                       telegram_channel_id, telegram_message_id
                FROM region_category_posts
                WHERE is_active = TRUE
                ORDER BY id
            """)
            posts = cur.fetchall()
    paused = False

    if region_names is not None:
        wanted = {
            str(name).strip().casefold()
            for name in region_names
            if name is not None
            and str(name).strip()
        }

        posts = [
            row
            for row in posts
            if str(row[1]).strip().casefold()
            in wanted
        ]

    for post_id, region, offset, page_size, channel_id, message_id in posts:
        total = _region_count(region)
        if total <= 0:
            try:
                await bot.delete_message(chat_id=channel_id, message_id=message_id)
            except Exception:
                pass
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE region_category_posts SET is_active=FALSE, updated_at=NOW() WHERE id=%s", (post_id,))
                conn.commit()
            continue
        last_offset = ((total - 1) // page_size) * page_size
        offset = min(int(offset or 0), last_offset)
        rows = _region_rows(region, offset, page_size)
        text = _build_region_text(region, rows, offset)
        keyboard = _region_nav(post_id, offset, total, page_size)
        try:
            await _edit_region_category_with_retry(
                bot,
                chat_id=channel_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
            await asyncio.sleep(2.0)
        except Exception as exc:
            error_text = str(exc).lower()
            if "message to edit not found" in error_text or "message to be edited not found" in error_text:
                # The region post was removed from the channel; stop syncing it,
                # matching how price category posts are deactivated.
                with db_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE region_category_posts SET is_active=FALSE, updated_at=NOW() WHERE id=%s", (post_id,))
                    conn.commit()
                logging.warning("Region category post deactivated because its message is gone: post_id=%s", post_id)
                continue
            if "message is not modified" not in error_text:
                logging.warning("Region category sync failed post_id=%s: %s", post_id, exc)
            if isinstance(exc, RetryAfter):
                logging.warning(
                    "Region category sync batch paused after Telegram flood control: post_id=%s",
                    post_id,
                )
                paused = True
                break
    return {"paused": paused}

async def handle_crm_button(update, context, session):
    query = update.callback_query
    action = query.data or ""
    user_id = session[0]

    if action in ("users", "settings_users"):
        if not is_owner(session):
            return False
        await query.edit_message_text(
            "👤 مدیریت کاربران و دسترسی‌ها\n\n"
            "از این بخش ادمین/مشاور اضافه کنید و دسترسی هر نفر را جداگانه تعیین کنید.",
            reply_markup=_users_keyboard(),
        )
        return True

    if action == "crm_user_add":
        if not is_owner(session):
            return False
        context.user_data.clear()
        context.user_data["state"] = "crm_user_add_display"
        await query.edit_message_text("➕ افزودن کاربر\n\nنام نمایشی را ارسال کنید.", reply_markup=_settings_back())
        return True

    if action.startswith("crm_user_view_"):
        if not is_owner(session):
            return False
        target_id = int(action.rsplit("_", 1)[1])
        row = _get_user(target_id)
        if not row:
            await query.edit_message_text("❌ کاربر پیدا نشد.", reply_markup=_settings_back())
            return True
        rows = []
        if row[3] != "OWNER":
            rows.append([
                InlineKeyboardButton(
                    "⛔ غیرفعال کردن" if row[4] else "✅ فعال کردن",
                    callback_data=f"crm_user_active_{target_id}",
                )
            ])
        perm = _permission_keyboard(target_id, row[3])
        rows.extend([list(r) for r in perm.inline_keyboard])
        await query.edit_message_text(_user_text(row), reply_markup=InlineKeyboardMarkup(rows))
        return True

    if action.startswith("crm_user_active_"):
        if not is_owner(session):
            return False
        target_id = int(action.rsplit("_", 1)[1])
        result = _toggle_user_active(target_id)
        audit(user_id, "USER_TOGGLE_ACTIVE", target_user_id=target_id, details={"active": result})
        row = _get_user(target_id)
        await query.edit_message_text(_user_text(row), reply_markup=_permission_keyboard(target_id, row[3]))
        return True

    if action.startswith("crm_user_perm_"):
        if not is_owner(session):
            return False
        payload = action[len("crm_user_perm_"):]
        target_str, key = payload.split("_", 1)
        target_id = int(target_str)
        new_value = _toggle_permission(target_id, key)
        audit(user_id, "USER_PERMISSION", target_user_id=target_id, details={"permission": key, "allowed": new_value})
        row = _get_user(target_id)
        await query.edit_message_text(_user_text(row), reply_markup=_permission_keyboard(target_id, row[3]))
        return True

    if action.startswith("crm_user_role_"):
        if not is_owner(session):
            return False
        role = action.rsplit("_", 1)[1]
        if role not in ("ADMIN", "ADVISOR"):
            return True
        data = context.user_data
        display = data.get("crm_new_display")
        username = data.get("crm_new_username")
        password_hash = data.get("crm_new_password_hash")
        if not display or not username or not password_hash:
            await query.edit_message_text("⚠️ اطلاعات ساخت کاربر منقضی شده است.", reply_markup=_users_keyboard())
            return True
        try:
            new_id = _create_user(display, username, password_hash, role)
        except Exception as exc:
            await query.edit_message_text(f"❌ ساخت کاربر انجام نشد:\n{str(exc)[:400]}", reply_markup=_users_keyboard())
            return True
        audit(user_id, "USER_CREATED", target_user_id=new_id, details={"role": role, "username": username})
        context.user_data.clear()
        await query.edit_message_text("✅ کاربر ساخته شد.", reply_markup=_users_keyboard())
        return True

    if action == "crm_customers" or action.startswith("crm_customers_page_"):
        if action.startswith("crm_customers_page_"):
            offset = int(action.rsplit("_", 1)[1])
        else:
            offset = 0
        kb, total = _customers_keyboard(session, offset)
        scope_text = "همه مشتریان دفتر و مشاوران" if is_owner(session) else "فقط مشتریان شما"
        await query.edit_message_text(
            f"👥 باشگاه مشتریان\n\n{scope_text}\nتعداد: {total}\n\nروی شماره موردنظر بزنید:",
            reply_markup=kb,
        )
        return True

    if action == "crm_customer_add":
        context.user_data.clear()
        context.user_data["state"] = "crm_customer_add_name"
        await query.edit_message_text("➕ افزودن مشتری\n\nنام و نام خانوادگی مشتری را ارسال کنید.", reply_markup=_main_back())
        return True

    if action == "crm_customer_search":
        context.user_data.clear()
        context.user_data["state"] = "crm_customer_search"
        await query.edit_message_text("🔎 جستجوی مشتری\n\nنام یا شماره تماس را ارسال کنید.", reply_markup=_main_back())
        return True

    if action.startswith("crm_customer_view_"):
        customer_id = int(action.rsplit("_", 1)[1])
        row = _customer_detail(session, customer_id)
        if not row:
            await query.edit_message_text("⛔ این مشتری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        await query.edit_message_text(_customer_text(row), reply_markup=_customer_keyboard(session, customer_id))
        return True

    if action.startswith("crm_customer_assign_"):
        if not is_owner(session):
            return False
        customer_id = int(action.rsplit("_", 1)[1])
        rows = []
        for target_id, name, role in _assignable_users():
            rows.append([InlineKeyboardButton(name, callback_data=f"crm_customer_doassign_{customer_id}_{target_id}")])
        rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data=f"crm_customer_view_{customer_id}")])
        await query.edit_message_text("👤 واگذاری مشتری\n\nمشاور/ادمین مسئول را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
        return True

    if action.startswith("crm_customer_doassign_"):
        if not is_owner(session):
            return False
        payload = action[len("crm_customer_doassign_"):]
        customer_id, target_id = map(int, payload.split("_", 1))
        _assign_customer(customer_id, target_id)
        audit(user_id, "CUSTOMER_ASSIGNED", target_user_id=target_id, customer_id=customer_id)
        row = _customer_detail(session, customer_id)
        await query.edit_message_text("✅ مشتری واگذار شد.\n\n" + _customer_text(row), reply_markup=_customer_keyboard(session, customer_id))
        return True

    if action == "crm_visits":
        await query.edit_message_text(
            "📅 مدیریت بازدید\n\n"
            + ("شما همه درخواست‌ها و بازدیدها را می‌بینید." if is_owner(session) else "فقط بازدیدهای مشتریان خودتان نمایش داده می‌شود."),
            reply_markup=_visits_keyboard(session),
        )
        return True

    if action == "crm_visit_add":
        context.user_data.clear()
        context.user_data["state"] = "crm_visit_customer_search"
        await query.edit_message_text("➕ ثبت بازدید\n\nنام یا شماره مشتری را ارسال کنید.", reply_markup=_main_back())
        return True

    if action.startswith("crm_visit_for_customer_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _customer_detail(session, customer_id):
            await query.edit_message_text("⛔ این مشتری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        context.user_data.clear()
        context.user_data["crm_visit_customer_id"] = customer_id
        context.user_data["state"] = "crm_visit_unit"
        await query.edit_message_text("🔢 کد واحد را ارسال کنید.\n\nاگر بازدید بدون فایل مشخص است عدد 0 را بفرستید.", reply_markup=_main_back())
        return True

    if action.startswith("crm_visit_pick_customer_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _customer_detail(session, customer_id):
            await query.edit_message_text("⛔ این مشتری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        context.user_data["crm_visit_customer_id"] = customer_id
        context.user_data["state"] = "crm_visit_unit"
        await query.edit_message_text("🔢 کد واحد را ارسال کنید.\n\nاگر بازدید بدون فایل مشخص است عدد 0 را بفرستید.", reply_markup=_main_back())
        return True

    if action.startswith("crm_visit_view_"):
        visit_id = int(action.rsplit("_", 1)[1])
        row = _visit_detail(session, visit_id)
        if not row:
            await query.edit_message_text("⛔ این بازدید در دسترس شما نیست.", reply_markup=_main_back())
            return True
        await query.edit_message_text(_visit_text(row), reply_markup=_visit_keyboard(visit_id), disable_web_page_preview=True)
        return True

    if action.startswith("crm_visit_done_") or action.startswith("crm_visit_cancel_"):
        status = "CLOSED" if action.startswith("crm_visit_done_") else "CANCELLED"
        visit_id = int(action.rsplit("_", 1)[1])
        if not _visit_detail(session, visit_id):
            await query.edit_message_text("⛔ این بازدید در دسترس شما نیست.", reply_markup=_main_back())
            return True
        _set_visit_status(visit_id, status)
        audit(user_id, "VISIT_STATUS", visit_id=visit_id, details={"status": status})
        await query.edit_message_text("✅ وضعیت بازدید بروزرسانی شد.", reply_markup=_visits_keyboard(session))
        return True

    if action == "category_region_start":
        context.user_data.clear()
        context.user_data["state"] = "crm_category_region"
        await query.edit_message_text(
            "📍 ارسال دسته‌بندی بر اساس منطقه\n\nنام منطقه را دقیق ارسال کنید.\nمثال:\nباغمیشه",
            reply_markup=_main_back(),
        )
        return True

    if action == "crm_category_region_send":
        region = context.user_data.get("crm_category_region_name")
        if not region:
            await query.edit_message_text("⚠️ منطقه منقضی شده است. دوباره شروع کنید.", reply_markup=_main_back())
            return True
        total = _region_count(region)
        if total <= 0:
            await query.edit_message_text("⚠️ فایل فعالی برای این منطقه پیدا نشد.", reply_markup=_main_back())
            return True
        rows = _region_rows(region, 0, 10)
        text = _build_region_text(region, rows, 0)
        dest = _destination_channel()
        if not dest:
            await query.edit_message_text("❌ کانال مقصد فعال پیدا نشد.", reply_markup=_main_back())
            return True
        sent = await context.bot.send_message(
            chat_id=dest[0],
            text=text,
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        post_id = _record_region_post(region, dest[0], sent.message_id, user_id)
        keyboard = _region_nav(post_id, 0, total, 10)
        if keyboard:
            await context.bot.edit_message_reply_markup(
                chat_id=dest[0],
                message_id=sent.message_id,
                reply_markup=keyboard,
            )
        context.user_data.clear()
        audit(user_id, "REGION_CATEGORY_SENT", details={"region": region, "count": total})
        await query.edit_message_text(
            f"✅ دسته‌بندی منطقه «{region}» ارسال شد.\nتعداد فایل: {total}",
            reply_markup=_main_back(),
        )
        return True

    return False

async def handle_crm_text(update, context, session):
    state = context.user_data.get("state")
    if not state or not state.startswith("crm_"):
        return False
    text = (update.message.text or "").strip()
    user_id = session[0]

    if state == "crm_user_add_display":
        context.user_data["crm_new_display"] = text
        context.user_data["state"] = "crm_user_add_username"
        await update.message.reply_text("نام کاربری ورود را ارسال کنید.")
        return True

    if state == "crm_user_add_username":
        username = re.sub(r"\s+", "", text)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,100}", username):
            await update.message.reply_text("❌ نام کاربری حداقل ۳ کاراکتر و فقط شامل حروف انگلیسی، عدد، نقطه، خط تیره یا زیرخط باشد.")
            return True
        context.user_data["crm_new_username"] = username
        context.user_data["state"] = "crm_user_add_password"
        await update.message.reply_text("🔑 رمز عبور اولیه را ارسال کنید.")
        return True

    if state == "crm_user_add_password":
        if len(text) < 6:
            await update.message.reply_text("❌ رمز عبور حداقل ۶ کاراکتر باشد.")
            return True
        try:
            await update.message.delete()
        except Exception:
            pass
        password_hash = bcrypt.hashpw(text.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        context.user_data["crm_new_password_plain"] = text
        context.user_data["crm_new_password_hash"] = password_hash
        context.user_data["state"] = None
        await update.effective_chat.send_message(
            "نقش کاربر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👔 ادمین", callback_data="crm_user_role_ADMIN")],
                [InlineKeyboardButton("🏠 مشاور", callback_data="crm_user_role_ADVISOR")],
                [InlineKeyboardButton("❌ انصراف", callback_data="settings_users")],
            ]),
        )
        return True

    if state == "crm_customer_add_name":
        context.user_data["crm_customer_name"] = text
        context.user_data["state"] = "crm_customer_add_phone"
        await update.message.reply_text("📞 شماره تماس مشتری را ارسال کنید.")
        return True

    if state == "crm_customer_add_phone":
        name = context.user_data.get("crm_customer_name", "")
        try:
            customer_id, created = _create_manual_customer(session, name, text)
        except ValueError as exc:
            await update.message.reply_text(f"❌ {exc}")
            return True
        context.user_data.clear()
        audit(user_id, "CUSTOMER_ADDED" if created else "CUSTOMER_DUPLICATE_OPENED", customer_id=customer_id)
        row = _customer_detail(session, customer_id)
        await update.message.reply_text(
            ("✅ مشتری ثبت شد.\n\n" if created else "ℹ️ این شماره قبلاً ثبت شده بود.\n\n") + _customer_text(row),
            reply_markup=_customer_keyboard(session, customer_id),
        )
        return True

    if state == "crm_customer_search":
        rows = _search_customers(session, text)
        context.user_data.clear()
        if not rows:
            await update.message.reply_text("⚠️ مشتری پیدا نشد.", reply_markup=_main_back())
            return True
        buttons = []
        for customer_id, name, phone, owner_id, owner_name in rows:
            buttons.append([InlineKeyboardButton(
                f"📞 {phone or '-'} | {name}"[:60],
                callback_data=f"crm_customer_view_{customer_id}",
            )])
        buttons.append([InlineKeyboardButton("⬅️ مشتریان", callback_data="crm_customers")])
        await update.message.reply_text(
            f"🔎 نتایج جستجو: {len(rows)}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return True

    if state == "crm_visit_customer_search":
        rows = _search_customers(session, text)
        if not rows:
            await update.message.reply_text("⚠️ مشتری پیدا نشد. نام یا شماره دیگری بفرستید.")
            return True
        context.user_data["state"] = None
        buttons = []
        for customer_id, name, phone, owner_id, owner_name in rows:
            buttons.append([InlineKeyboardButton(
                f"{name} | {phone or '-'}"[:60],
                callback_data=f"crm_visit_pick_customer_{customer_id}",
            )])
        buttons.append([InlineKeyboardButton("⬅️ بازدیدها", callback_data="crm_visits")])
        await update.message.reply_text("مشتری را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if state == "crm_visit_unit":
        property_row = None
        if text not in ("0", "-", "بدون فایل"):
            property_row = _find_property_by_unit(_normalize_digits(text))
            if not property_row:
                await update.message.reply_text("❌ کد واحد پیدا نشد. دوباره ارسال کنید یا عدد 0 را بفرستید.")
                return True
        context.user_data["crm_visit_property"] = property_row
        context.user_data["state"] = "crm_visit_datetime"
        await update.message.reply_text(
            "🕐 تاریخ و ساعت بازدید را ارسال کنید.\n\n"
            "مثال شمسی:\n1405/07/12 18:30\n\n"
            "یا میلادی:\n2026-10-04 18:30"
        )
        return True

    if state == "crm_visit_datetime":
        visit_at = parse_visit_datetime(text)
        if not visit_at:
            await update.message.reply_text("❌ تاریخ/ساعت نامعتبر است. مثال: 1405/07/12 18:30")
            return True
        if visit_at <= datetime.now(TEHRAN):
            await update.message.reply_text("❌ زمان بازدید باید در آینده باشد.")
            return True
        customer_id = context.user_data.get("crm_visit_customer_id")
        property_row = context.user_data.get("crm_visit_property")
        customer = _customer_detail(session, customer_id) if customer_id else None
        if not customer:
            context.user_data.clear()
            await update.message.reply_text("⚠️ اطلاعات مشتری منقضی شده است.", reply_markup=_main_back())
            return True
        owner_user_id = customer[6] or user_id
        assigned_user_id = owner_user_id if not is_owner(session) else owner_user_id
        visit_id = _schedule_visit(customer_id, property_row, visit_at, assigned_user_id, user_id)
        context.user_data.clear()
        audit(user_id, "VISIT_SCHEDULED", customer_id=customer_id, visit_id=visit_id, target_user_id=assigned_user_id)
        row = _visit_detail(session, visit_id)
        await update.message.reply_text(
            "✅ زمان بازدید ثبت شد.\nیک ساعت قبل به مشاور مسئول یادآوری می‌شود.\n\n" + _visit_text(row),
            reply_markup=_visit_keyboard(visit_id),
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return True

    if state == "crm_category_region":
        region = text.strip()
        if len(region) < 2:
            await update.message.reply_text("❌ نام منطقه معتبر نیست.")
            return True
        total = _region_count(region)
        context.user_data["crm_category_region_name"] = region
        context.user_data["state"] = None
        if total <= 0:
            await update.message.reply_text(
                f"⚠️ فایل فعال با منطقه «{region}» پیدا نشد.",
                reply_markup=_main_back(),
            )
            return True
        await update.message.reply_text(
            f"📍 منطقه: {region}\nتعداد فایل فعال: {total}\n\nفقط district/neighborhood ملک بررسی شده و متن خام منبع وارد فیلتر نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 ارسال به کانال", callback_data="crm_category_region_send")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
            ]),
        )
        return True

    return False

def _latest_telegram_user_id(user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT telegram_user_id
                FROM user_sessions
                WHERE user_id = %s
                ORDER BY last_activity_at DESC NULLS LAST, id DESC
                LIMIT 1
            """, (user_id,))
            row = cur.fetchone()
            return row[0] if row else None

def _visit_reminder_recipient_telegram_ids(assigned_user_id):
    # OWNER + assigned advisor/user + explicit reminder permission holders.
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidate_users AS (
                    SELECT u.id
                    FROM users u
                    WHERE u.is_active = TRUE
                      AND u.role = 'OWNER'

                    UNION

                    SELECT u.id
                    FROM users u
                    WHERE u.is_active = TRUE
                      AND u.id = %s

                    UNION

                    SELECT u.id
                    FROM users u
                    JOIN user_permissions up ON up.user_id = u.id
                    WHERE u.is_active = TRUE
                      AND up.permission_key = 'receive_visit_reminders'
                      AND up.is_allowed = TRUE
                )
                SELECT DISTINCT ON (cu.id)
                    cu.id,
                    us.telegram_user_id
                FROM candidate_users cu
                JOIN user_sessions us ON us.user_id = cu.id
                WHERE us.telegram_user_id IS NOT NULL
                ORDER BY
                    cu.id,
                    us.last_activity_at DESC NULLS LAST,
                    us.id DESC
                """,
                (assigned_user_id,),
            )
            return [row[1] for row in cur.fetchall() if row[1]]


def _due_visit_reminders():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    vr.id,
                    vr.visit_at,
                    COALESCE(vr.assigned_user_id, cp.owner_user_id) AS assigned_user_id,
                    vr.unit_code_snapshot,
                    vr.channel_post_url_snapshot,
                    cp.first_name,
                    cp.last_name,
                    cp.phone_number
                FROM visit_requests vr
                JOIN customer_profiles cp ON cp.id = vr.customer_id
                WHERE vr.status = 'SCHEDULED'
                  AND vr.visit_at IS NOT NULL
                  AND vr.reminder_sent_at IS NULL
                  AND vr.visit_at > NOW()
                  AND vr.visit_at <= NOW() + INTERVAL '1 hour'
                ORDER BY vr.visit_at
                LIMIT 50
            """)
            return cur.fetchall()

def _mark_reminder_sent(visit_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE visit_requests
                SET reminder_sent_at = NOW(), updated_at = NOW()
                WHERE id = %s
                  AND reminder_sent_at IS NULL
            """, (visit_id,))
        conn.commit()

async def run_visit_reminders(application):
    for visit_id, visit_at, assigned_user_id, unit_code, post_url, first_name, last_name, phone in _due_visit_reminders():
        visit_group = get_visit_ops_target()
        if visit_group:
            recipients = [
                (
                    int(visit_group[0]),
                    telegram_thread_kwargs(visit_group),
                )
            ]
        else:
            recipients = [
                (int(recipient_id), {})
                for recipient_id in _visit_reminder_recipient_telegram_ids(
                    assigned_user_id
                )
            ]
        if not recipients:
            continue

        name = " ".join(x for x in (first_name, last_name) if x).strip() or "بدون نام"
        local = visit_at.astimezone(TEHRAN)
        text = (
            "⏰ یادآوری بازدید\n\n"
            "حدود یک ساعت تا زمان بازدید باقی مانده.\n\n"
            f"👤 مشتری: {name}\n"
            f"📞 {normalize_phone(phone) or '-'}\n"
            f"🔢 کد واحد: {unit_code or '-'}\n"
            f"🕐 زمان: {local.strftime('%Y-%m-%d %H:%M')}"
        )
        if post_url:
            text += f"\n🔗 فایل: {post_url}"

        sent_count = 0
        for telegram_id, thread_kwargs in recipients:
            try:
                await application.bot.send_message(
                    chat_id=telegram_id,
                    text=text,
                    disable_web_page_preview=True,
                    **thread_kwargs,
                )
                sent_count += 1
            except Exception:
                logging.exception(
                    "Visit reminder failed visit_id=%s recipient=%s",
                    visit_id,
                    telegram_id,
                )

        if sent_count > 0:
            _mark_reminder_sent(visit_id)
            logging.info(
                "Visit reminder sent visit_id=%s recipients=%s",
                visit_id,
                sent_count,
            )
async def _reminder_loop(application):
    logging.info("CRM visit reminder worker started.")
    while True:
        try:
            await run_visit_reminders(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("CRM reminder worker error")
        await asyncio.sleep(30)

def start_visit_reminder_worker(application):
    existing = application.bot_data.get("crm_visit_reminder_task")
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_reminder_loop(application), name="crm_visit_reminders")
    application.bot_data["crm_visit_reminder_task"] = task
    return task




# =========================================================
# USER_LOGIN_CARD_V1
# Show initial credentials exactly once after successful
# user creation. Plain password is never stored in DB.
# =========================================================

_handle_crm_button_before_login_card = handle_crm_button


def _login_card_user_by_username(username):
    if not username:
        return None

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    username,
                    display_name,
                    role,
                    is_active
                FROM users
                WHERE LOWER(username) = LOWER(%s)
                LIMIT 1
                """,
                (username,),
            )

            return cur.fetchone()


async def handle_crm_button(
    update,
    context,
    session,
):
    query = update.callback_query

    action = (
        query.data
        if query
        else ""
    ) or ""

    is_creation_action = (
        action.startswith(
            "crm_user_role_"
        )
    )

    username = None
    display_name = None
    plain_password = None
    before_user = None

    if is_creation_action:
        username = context.user_data.get(
            "crm_new_username"
        )

        display_name = context.user_data.get(
            "crm_new_display"
        )

        plain_password = context.user_data.get(
            "crm_new_password_plain"
        )

        before_user = (
            _login_card_user_by_username(
                username
            )
        )

    try:
        result = await (
            _handle_crm_button_before_login_card(
                update,
                context,
                session,
            )
        )

        if (
            is_creation_action
            and username
            and plain_password
            and before_user is None
        ):
            created_user = (
                _login_card_user_by_username(
                    username
                )
            )

            if created_user:
                (
                    created_user_id,
                    created_username,
                    created_display_name,
                    created_role,
                    created_active,
                ) = created_user

                role_fa = {
                    "OWNER": "مالک",
                    "ADMIN": "ادمین",
                    "ADVISOR": "مشاور",
                }.get(
                    created_role,
                    created_role,
                )

                login_card = (
                    "✅ کاربر با موفقیت ساخته شد\n\n"
                    f"👤 نام: "
                    f"{created_display_name or display_name or '-'}\n"
                    f"🏷 نقش: {role_fa}\n\n"
                    f"🔐 نام کاربری: {created_username}\n"
                    f"🔑 رمز عبور موقت: {plain_password}\n\n"
                    "🤖 ورود به پنل مدیریت:\n"
                    "https://t.me/nahadmelkbot\n\n"
                    "برای ورود /start یا /login را بزنید.\n\n"
                    "⚠️ رمز عبور فقط همین یک‌بار "
                    "در این پیام نمایش داده می‌شود."
                )

                await update.effective_chat.send_message(
                    login_card
                )

        return result

    finally:
        if is_creation_action:
            context.user_data.pop(
                "crm_new_password_plain",
                None,
            )




# =========================================================
# === CUSTOMER LEADS FLOW V1 ===
# =========================================================

from decimal import Decimal, InvalidOperation


_crm_leads_legacy_build_main_menu = build_main_menu
_crm_leads_legacy_handle_crm_button = handle_crm_button
_crm_leads_legacy_handle_crm_text = handle_crm_text


def _lead_name_parts(name):
    parts = str(name or "").strip().split(maxsplit=1)
    return (
        parts[0] if parts else None,
        parts[1] if len(parts) > 1 else None,
    )


def _lead_customer_by_phone(phone_normalized):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    owner_user_id,
                    created_by_user_id,
                    first_name,
                    last_name,
                    phone_number
                FROM customer_profiles
                WHERE phone_normalized = %s
                ORDER BY id
                LIMIT 1
                """,
                (phone_normalized,),
            )
            return cur.fetchone()


def _lead_ensure_access(customer_id, user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT owner_user_id, created_by_user_id
                FROM customer_profiles
                WHERE id = %s
                """,
                (customer_id,),
            )
            row = cur.fetchone()

            if not row:
                return False

            owner_id, creator_id = row

            if owner_id is None:
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET
                        owner_user_id = %s,
                        created_by_user_id =
                            COALESCE(created_by_user_id, %s),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (user_id, user_id, customer_id),
                )

            elif (
                owner_id != user_id
                and creator_id != user_id
            ):
                cur.execute(
                    """
                    INSERT INTO customer_shares (
                        customer_id,
                        user_id,
                        shared_by_user_id
                    )
                    VALUES (%s, %s, %s)
                    ON CONFLICT (customer_id, user_id)
                    DO NOTHING
                    """,
                    (customer_id, user_id, user_id),
                )

        conn.commit()

    return True


def _lead_get_or_create_customer(
    session,
    phone,
    name=None,
):
    phone_norm = normalize_phone(phone)

    if (
        not phone_norm
        or not phone_norm.startswith("0")
        or len(phone_norm) < 10
        or len(phone_norm) > 12
    ):
        raise ValueError(
            "شماره تماس معتبر نیست. "
            "شماره را با صفر اول ارسال کنید."
        )

    existing = _lead_customer_by_phone(
        phone_norm
    )

    if existing:
        customer_id = existing[0]

        if name:
            first_name, last_name = _lead_name_parts(name)

            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE customer_profiles
                        SET
                            first_name = COALESCE(
                                NULLIF(BTRIM(first_name), ''),
                                %s
                            ),
                            last_name = COALESCE(
                                NULLIF(BTRIM(last_name), ''),
                                %s
                            ),
                            updated_at = NOW(),
                            last_interaction_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            first_name,
                            last_name,
                            customer_id,
                        ),
                    )
                conn.commit()

        _lead_ensure_access(
            customer_id,
            session[0],
        )

        return customer_id, False

    if not name:
        return None, False

    first_name, last_name = _lead_name_parts(name)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_profiles (
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                    phone_number,
                    phone_normalized,
                    owner_user_id,
                    created_by_user_id,
                    source_label,
                    created_at,
                    updated_at,
                    last_interaction_at
                )
                VALUES (
                    NULL, NULL,
                    %s, %s, %s, %s,
                    %s, %s,
                    'MANUAL_LEAD',
                    NOW(), NOW(), NOW()
                )
                RETURNING id
                """,
                (
                    first_name,
                    last_name,
                    phone,
                    phone_norm,
                    session[0],
                    session[0],
                ),
            )

            customer_id = cur.fetchone()[0]

        conn.commit()

    return customer_id, True


def _lead_parse_price_range(value):
    text = _normalize_digits(value)
    text = (
        text.replace("٬", "")
        .replace("٫", ".")
        .replace(",", " ")
    )

    values = re.findall(
        r"\d+(?:\.\d+)?",
        text,
    )

    if len(values) < 2:
        return None

    try:
        first = Decimal(values[0])
        second = Decimal(values[1])
    except InvalidOperation:
        return None

    if first <= 0 or second <= 0:
        return None

    return min(first, second), max(first, second)


def _lead_set_price_range(customer_id, low, high):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_profiles
                SET
                    desired_price_min_billion = %s,
                    desired_price_max_billion = %s,
                    updated_at = NOW(),
                    last_interaction_at = NOW()
                WHERE id = %s
                """,
                (low, high, customer_id),
            )

        conn.commit()


def _lead_parse_unit_codes(value):
    return list(dict.fromkeys(
        re.findall(
            r"\d+",
            _normalize_digits(value),
        )
    ))


def _lead_find_property(unit_code):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    unit_code,
                    district,
                    address_text,
                    area_m2,
                    bedrooms,
                    build_year,
                    current_price_billion,
                    channel_post_url
                FROM properties
                WHERE unit_code = %s
                  AND status = 'ACTIVE'
                  AND deleted_from_channel_at IS NULL
                LIMIT 1
                """,
                (
                    _normalize_digits(
                        unit_code
                    ).strip(),
                ),
            )

            return cur.fetchone()


def _lead_properties_by_ids(property_ids):
    result = []

    for property_id in property_ids or []:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        unit_code,
                        district,
                        address_text,
                        area_m2,
                        bedrooms,
                        build_year,
                        current_price_billion,
                        channel_post_url
                    FROM properties
                    WHERE id = %s
                      AND status = 'ACTIVE'
                      AND deleted_from_channel_at IS NULL
                    LIMIT 1
                    """,
                    (property_id,),
                )

                row = cur.fetchone()

        if row:
            result.append(row)

    return result


def _lead_property_text(row, index=None):
    (
        property_id,
        unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        build_year,
        price_billion,
        post_url,
    ) = row

    prefix = f"{index}) " if index else ""
    location = address_text or district or "-"

    lines = [
        f"{prefix}📍 {location}"
    ]

    details = []

    if area_m2 is not None:
        details.append(
            f"📐 {float(area_m2):g} متر"
        )

    if bedrooms is not None:
        details.append(
            f"🛏 {int(bedrooms)} خواب"
        )

    if build_year is not None:
        details.append(
            f"📅 {int(build_year)}"
        )

    if details:
        lines.append(
            " | ".join(details)
        )

    if price_billion is not None:
        lines.append(
            f"💰 قیمت: {float(price_billion):g} "
            "میلیارد تومان"
        )

    if unit_code:
        lines.append(
            f"🔢 کد واحد: {unit_code}"
        )

    if post_url:
        lines.append(
            f"🔗 مشاهده فایل: {post_url}"
        )

    return "\n".join(lines)


def _lead_create_visit_requests(
    customer_id,
    property_rows,
    creator_id,
):
    visit_ids = []

    with db_connect() as conn:
        with conn.cursor() as cur:

            for row in property_rows:
                property_id = row[0]
                unit_code = row[1]
                post_url = row[8]

                cur.execute(
                    """
                    SELECT id
                    FROM visit_requests
                    WHERE customer_id = %s
                      AND property_id = %s
                      AND status IN (
                          'NEW',
                          'CONTACTED',
                          'SCHEDULED'
                      )
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        customer_id,
                        property_id,
                    ),
                )

                existing = cur.fetchone()

                if existing:
                    visit_ids.append(
                        existing[0]
                    )
                    continue

                cur.execute(
                    """
                    INSERT INTO visit_requests (
                        customer_id,
                        property_id,
                        unit_code_snapshot,
                        channel_post_url_snapshot,
                        status,
                        source,
                        requested_at,
                        assigned_user_id,
                        created_by_user_id,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        'NEW',
                        'MANUAL',
                        NOW(),
                        %s, %s,
                        NOW(), NOW()
                    )
                    RETURNING id
                    """,
                    (
                        customer_id,
                        property_id,
                        unit_code,
                        post_url,
                        creator_id,
                        creator_id,
                    ),
                )

                visit_ids.append(
                    cur.fetchone()[0]
                )

        conn.commit()

    return visit_ids


def _lead_share_users(current_user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    COALESCE(
                        NULLIF(BTRIM(display_name), ''),
                        username
                    ),
                    role
                FROM users
                WHERE is_active = TRUE
                  AND id <> %s
                  AND role IN (
                      'OWNER',
                      'ADMIN',
                      'ADVISOR'
                  )
                ORDER BY
                    CASE role
                        WHEN 'OWNER' THEN 0
                        WHEN 'ADMIN' THEN 1
                        ELSE 2
                    END,
                    id
                """,
                (current_user_id,),
            )

            return cur.fetchall()


def _lead_share_keyboard(session, selected_ids=None):
    selected = set(
        int(x)
        for x in (selected_ids or [])
    )

    rows = []

    for target_id, name, role in _lead_share_users(
        session[0]
    ):
        role_fa = {
            "OWNER": "مالک",
            "ADMIN": "ادمین",
            "ADVISOR": "مشاور",
        }.get(role, role)

        icon = (
            "✅"
            if target_id in selected
            else "⬜"
        )

        rows.append([
            InlineKeyboardButton(
                f"{icon} {name} | {role_fa}",
                callback_data=(
                    f"crm_lead_share_toggle_{target_id}"
                ),
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "✅ ثبت اشتراک و ارسال",
            callback_data="crm_lead_share_finish",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ بدون اشتراک",
            callback_data="crm_lead_share_skip",
        )
    ])

    return InlineKeyboardMarkup(rows)


def _lead_share_prompt_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بله، انتخاب مشاور",
                callback_data="crm_lead_share_yes",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ خیر، تمام",
                callback_data="crm_lead_share_skip",
            )
        ],
    ])


def _lead_save_shares(
    customer_id,
    target_ids,
    actor_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            for target_id in target_ids:
                cur.execute(
                    """
                    INSERT INTO customer_shares (
                        customer_id,
                        user_id,
                        shared_by_user_id,
                        created_at
                    )
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (customer_id, user_id)
                    DO UPDATE SET
                        shared_by_user_id =
                            EXCLUDED.shared_by_user_id,
                        created_at = NOW()
                    """,
                    (
                        customer_id,
                        target_id,
                        actor_id,
                    ),
                )

        conn.commit()


def _lead_set_notes(customer_id, note):
    note = str(note or "").strip()

    if not note:
        return False

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_profiles
                SET
                    notes = CASE
                        WHEN notes IS NULL
                             OR BTRIM(notes) = ''
                        THEN %s
                        ELSE notes || E'\\n' || %s
                    END,
                    updated_at = NOW(),
                    last_interaction_at = NOW()
                WHERE id = %s
                """,
                (
                    note,
                    note,
                    customer_id,
                ),
            )

        conn.commit()

    return True


def _lead_customer_row(customer_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(
                                CONCAT_WS(
                                    ' ',
                                    cp.first_name,
                                    cp.last_name
                                )
                            ),
                            ''
                        ),
                        'بدون نام'
                    ),
                    cp.phone_number,
                    cp.desired_price_min_billion,
                    cp.desired_price_max_billion,
                    cp.notes,
                    cp.owner_user_id,
                    COALESCE(
                        u.display_name,
                        u.username,
                        'دفتر'
                    )
                FROM customer_profiles cp
                LEFT JOIN users u
                    ON u.id = cp.owner_user_id
                WHERE cp.id = %s
                LIMIT 1
                """,
                (customer_id,),
            )

            return cur.fetchone()


def _lead_customer_text(customer_id):
    row = _lead_customer_row(
        customer_id
    )

    if not row:
        return "⚠️ مشتری پیدا نشد."

    (
        cid,
        name,
        phone,
        low,
        high,
        notes,
        owner_id,
        owner_name,
    ) = row

    lines = [
        f"👤 مشتری #{cid}",
        "",
        f"نام: {name}",
        f"📞 شماره: {phone or '-'}",
        f"👤 مشاور مسئول: {owner_name}",
    ]

    if low is not None and high is not None:
        lines.append(
            "💰 بازه قیمت: "
            f"{float(low):g} تا "
            f"{float(high):g} میلیارد تومان"
        )

    if notes:
        lines.extend([
            "",
            "📝 توضیحات:",
            str(notes).strip(),
        ])

    return "\n".join(lines)


def _lead_customer_keyboard(customer_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📅 ثبت بازدید",
                callback_data=(
                    f"crm_visit_for_customer_{customer_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ خواهان‌های اضافه‌شده",
                callback_data="crm_lead_added",
            )
        ],
    ])


def _lead_notification_text(
    customer_id,
    property_ids,
):
    row = _lead_customer_row(
        customer_id
    )

    if not row:
        return None

    (
        cid,
        name,
        phone,
        low,
        high,
        notes,
        owner_id,
        owner_name,
    ) = row

    lines = [
        "👤 خواهان جدید با شما به اشتراک گذاشته شد",
        "",
        f"نام: {name}",
        f"📞 شماره: {phone or '-'}",
    ]

    if low is not None and high is not None:
        lines.append(
            "💰 بازه قیمت: "
            f"{float(low):g} تا "
            f"{float(high):g} میلیارد تومان"
        )

    if notes:
        lines.extend([
            "",
            "📝 توضیحات مشتری:",
            str(notes).strip(),
        ])

    properties = _lead_properties_by_ids(
        property_ids
    )

    if properties:
        lines.extend([
            "",
            "🏠 فایل‌های موردنظر:",
            "",
        ])

        for index, prop in enumerate(
            properties,
            start=1,
        ):
            lines.append(
                _lead_property_text(
                    prop,
                    index,
                )
            )
            lines.append("")

    return "\n".join(lines).strip()


async def _lead_notify_users(
    context,
    customer_id,
    property_ids,
    target_ids,
):
    text = _lead_notification_text(
        customer_id,
        property_ids,
    )

    sent = 0
    failed = []

    for target_id in target_ids:
        telegram_id = _latest_telegram_user_id(
            target_id
        )

        if not telegram_id:
            failed.append(target_id)
            continue

        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=text,
                disable_web_page_preview=True,
                read_timeout=10,
                connect_timeout=3,
            )
            sent += 1

        except Exception:
            logging.exception(
                "Lead share notification failed "
                "customer_id=%s user_id=%s",
                customer_id,
                target_id,
            )
            failed.append(target_id)

    return sent, failed


def _lead_added_rows(session, offset=0, limit=15):
    user_id = session[0]

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(
                                CONCAT_WS(
                                    ' ',
                                    cp.first_name,
                                    cp.last_name
                                )
                            ),
                            ''
                        ),
                        'بدون نام'
                    ),
                    cp.phone_number,
                    cp.desired_price_min_billion,
                    cp.desired_price_max_billion
                FROM customer_profiles cp
                WHERE
                    cp.owner_user_id = %s
                    OR cp.created_by_user_id = %s
                    OR EXISTS (
                        SELECT 1
                        FROM customer_shares cs
                        WHERE cs.customer_id = cp.id
                          AND cs.user_id = %s
                    )
                ORDER BY
                    cp.updated_at DESC,
                    cp.id DESC
                LIMIT %s OFFSET %s
                """,
                (
                    user_id,
                    user_id,
                    user_id,
                    limit,
                    offset,
                ),
            )

            return cur.fetchall()


def _lead_added_count(session):
    user_id = session[0]

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM customer_profiles cp
                WHERE
                    cp.owner_user_id = %s
                    OR cp.created_by_user_id = %s
                    OR EXISTS (
                        SELECT 1
                        FROM customer_shares cs
                        WHERE cs.customer_id = cp.id
                          AND cs.user_id = %s
                    )
                """,
                (
                    user_id,
                    user_id,
                    user_id,
                ),
            )

            return int(
                cur.fetchone()[0] or 0
            )


def _lead_added_keyboard(session, offset=0, limit=15):
    total = _lead_added_count(session)
    rows = []

    for (
        customer_id,
        name,
        phone,
        low,
        high,
    ) in _lead_added_rows(
        session,
        offset,
        limit,
    ):
        price_text = ""

        if low is not None and high is not None:
            price_text = (
                f" | {float(low):g}-"
                f"{float(high):g}"
            )

        rows.append([
            InlineKeyboardButton(
                (
                    f"📞 {phone or '-'} | "
                    f"{name}{price_text}"
                )[:60],
                callback_data=(
                    f"crm_lead_view_{customer_id}"
                ),
            )
        ])

    nav = []

    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️ قبلی",
                callback_data=(
                    "crm_lead_added_page_"
                    f"{max(0, offset-limit)}"
                ),
            )
        )

    if offset + limit < total:
        nav.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=(
                    "crm_lead_added_page_"
                    f"{offset+limit}"
                ),
            )
        )

    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(
            "➕ ثبت مشتری",
            callback_data="crm_lead_add",
        ),
        InlineKeyboardButton(
            "🔎 جستجو",
            callback_data="crm_customer_search",
        ),
    ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ منوی اصلی",
            callback_data="home",
        )
    ])

    return InlineKeyboardMarkup(rows), total


# Shared customers must also be visible in the legacy CRM/search.
def _customer_scope(session):
    if is_owner(session):
        return "", []

    return (
        """
        AND (
            cp.owner_user_id = %s
            OR cp.created_by_user_id = %s
            OR EXISTS (
                SELECT 1
                FROM customer_shares cs
                WHERE cs.customer_id = cp.id
                  AND cs.user_id = %s
            )
        )
        """,
        [
            session[0],
            session[0],
            session[0],
        ],
    )


# Shared customers' visits must be visible too.
def _visit_scope(session):
    if is_owner(session):
        return "", []

    return (
        """
        AND (
            vr.assigned_user_id = %s
            OR cp.owner_user_id = %s
            OR cp.created_by_user_id = %s
            OR EXISTS (
                SELECT 1
                FROM customer_shares cs
                WHERE cs.customer_id = cp.id
                  AND cs.user_id = %s
            )
        )
        """,
        [
            session[0],
            session[0],
            session[0],
            session[0],
        ],
    )


# Replace old main-menu customer club with the new two-button lead UX.
def build_main_menu(session, legacy_keyboard):
    markup = _crm_leads_legacy_build_main_menu(
        session,
        legacy_keyboard,
    )

    rows = []

    for row in (
        markup.inline_keyboard
        if markup
        else []
    ):
        clean_row = [
            button
            for button in row
            if button.callback_data != "crm_customers"
        ]

        if clean_row:
            rows.append(clean_row)

    if has_permission(
        session,
        "customers_manage",
    ):
        insert_at = max(
            0,
            len(rows) - 1,
        )

        rows.insert(
            insert_at,
            [
                InlineKeyboardButton(
                    "➕ ثبت مشتری",
                    callback_data="crm_lead_add",
                ),
                InlineKeyboardButton(
                    "👥 خواهان‌های اضافه‌شده",
                    callback_data="crm_lead_added",
                ),
            ],
        )

    return InlineKeyboardMarkup(rows)


async def _lead_finish_no_share(
    update,
    context,
    session,
):
    customer_id = context.user_data.get(
        "crm_lead_customer_id"
    )

    if not customer_id:
        context.user_data.clear()
        await update.callback_query.edit_message_text(
            "⚠️ اطلاعات مشتری منقضی شده است.",
            reply_markup=_main_back(),
        )
        return True

    text = (
        "✅ مشتری ثبت شد.\n\n"
        + _lead_customer_text(
            customer_id
        )
    )

    context.user_data.clear()

    await update.callback_query.edit_message_text(
        text,
        reply_markup=_lead_customer_keyboard(
            customer_id
        ),
    )

    return True


async def handle_crm_button(update, context, session):
    query = update.callback_query
    action = (
        query.data
        if query
        else ""
    ) or ""

    if not action.startswith("crm_lead_"):
        return await (
            _crm_leads_legacy_handle_crm_button(
                update,
                context,
                session,
            )
        )

    if not has_permission(
        session,
        "customers_manage",
    ):
        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )
        return True

    user_id = session[0]

    if action == "crm_lead_add":
        context.user_data.clear()

        await query.edit_message_text(
            "➕ ثبت مشتری\n\n"
            "نوع ثبت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📅 وقت بازدید میخواد",
                        callback_data="crm_lead_visit_start",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📞 اضافه کردن شماره",
                        callback_data="crm_lead_phone_start",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ منوی اصلی",
                        callback_data="home",
                    )
                ],
            ]),
        )

        return True

    if (
        action == "crm_lead_added"
        or action.startswith(
            "crm_lead_added_page_"
        )
    ):
        offset = 0

        if action.startswith(
            "crm_lead_added_page_"
        ):
            offset = int(
                action.rsplit("_", 1)[1]
            )

        keyboard, total = _lead_added_keyboard(
            session,
            offset,
        )

        await query.edit_message_text(
            "👥 خواهان‌های اضافه‌شده\n\n"
            "مشتری‌هایی که شما ثبت کرده‌اید "
            "یا با شما به اشتراک گذاشته شده‌اند.\n\n"
            f"تعداد: {total}",
            reply_markup=keyboard,
        )

        return True

    if action.startswith("crm_lead_view_"):
        customer_id = int(
            action.rsplit("_", 1)[1]
        )

        rows = {
            row[0]
            for row in _lead_added_rows(
                session,
                0,
                100000,
            )
        }

        if customer_id not in rows:
            await query.edit_message_text(
                "⛔ این مشتری در دسترس شما نیست.",
                reply_markup=_main_back(),
            )
            return True

        await query.edit_message_text(
            _lead_customer_text(
                customer_id
            ),
            reply_markup=_lead_customer_keyboard(
                customer_id
            ),
        )

        return True

    if action == "crm_lead_visit_start":
        context.user_data.clear()
        context.user_data[
            "state"
        ] = "crm_lead_visit_phone"

        await query.edit_message_text(
            "📅 مشتری وقت بازدید می‌خواهد\n\n"
            "شماره تماس مشتری را ارسال کنید.",
            reply_markup=_main_back(),
        )

        return True

    if action == "crm_lead_phone_start":
        context.user_data.clear()
        context.user_data[
            "state"
        ] = "crm_lead_phone_name"

        await query.edit_message_text(
            "📞 اضافه کردن شماره\n\n"
            "نام و نام خانوادگی مشتری را ارسال کنید.",
            reply_markup=_main_back(),
        )

        return True

    if action == "crm_lead_notes_skip":
        customer_id = context.user_data.get(
            "crm_lead_customer_id"
        )

        if not customer_id:
            context.user_data.clear()

            await query.edit_message_text(
                "⚠️ اطلاعات مشتری منقضی شده است.",
                reply_markup=_main_back(),
            )

            return True

        context.user_data[
            "state"
        ] = None

        await query.edit_message_text(
            "⏭ بدون توضیحات ادامه می‌دهیم.\n\n"
            "این مشتری برای مشاور یا ادمین "
            "دیگری هم ارسال شود؟",
            reply_markup=_lead_share_prompt_keyboard(),
        )

        return True

    if action == "crm_lead_share_yes":
        if not context.user_data.get(
            "crm_lead_customer_id"
        ):
            context.user_data.clear()
            await query.edit_message_text(
                "⚠️ اطلاعات مشتری منقضی شده است.",
                reply_markup=_main_back(),
            )
            return True

        context.user_data[
            "crm_lead_share_selected"
        ] = []

        await query.edit_message_text(
            "👥 اشتراک مشتری\n\n"
            "افراد موردنظر را انتخاب کنید:",
            reply_markup=_lead_share_keyboard(
                session,
                [],
            ),
        )

        return True

    if action.startswith(
        "crm_lead_share_toggle_"
    ):
        target_id = int(
            action.rsplit("_", 1)[1]
        )

        valid_ids = {
            row[0]
            for row in _lead_share_users(
                user_id
            )
        }

        if target_id not in valid_ids:
            await query.answer(
                "این کاربر فعال نیست.",
                show_alert=True,
            )
            return True

        selected = set(
            context.user_data.get(
                "crm_lead_share_selected",
                [],
            )
        )

        if target_id in selected:
            selected.remove(target_id)
        else:
            selected.add(target_id)

        context.user_data[
            "crm_lead_share_selected"
        ] = sorted(selected)

        await query.edit_message_reply_markup(
            reply_markup=_lead_share_keyboard(
                session,
                selected,
            )
        )

        await query.answer()
        return True

    if action == "crm_lead_share_skip":
        return await _lead_finish_no_share(
            update,
            context,
            session,
        )

    if action == "crm_lead_share_finish":
        customer_id = context.user_data.get(
            "crm_lead_customer_id"
        )

        if not customer_id:
            context.user_data.clear()
            await query.edit_message_text(
                "⚠️ اطلاعات مشتری منقضی شده است.",
                reply_markup=_main_back(),
            )
            return True

        valid_ids = {
            row[0]
            for row in _lead_share_users(
                user_id
            )
        }

        selected = [
            int(x)
            for x in context.user_data.get(
                "crm_lead_share_selected",
                [],
            )
            if int(x) in valid_ids
        ]

        selected = list(
            dict.fromkeys(selected)
        )

        _lead_save_shares(
            customer_id,
            selected,
            user_id,
        )

        property_ids = context.user_data.get(
            "crm_lead_property_ids",
            [],
        )

        sent, failed = await _lead_notify_users(
            context,
            customer_id,
            property_ids,
            selected,
        )

        audit(
            user_id,
            "CUSTOMER_SHARED",
            customer_id=customer_id,
            details={
                "shared_user_ids": selected,
                "notification_sent": sent,
                "notification_failed": failed,
            },
        )

        text = (
            "✅ مشتری ثبت و اشتراک‌گذاری شد.\n"
            f"👥 اشتراک: {len(selected)} نفر\n"
            f"📨 پیام موفق: {sent} نفر"
        )

        if failed:
            text += (
                f"\n⚠️ ارسال پیام برای "
                f"{len(failed)} نفر ناموفق بود؛ "
                "ولی دسترسی آنها ثبت شد."
            )

        text += (
            "\n\n"
            + _lead_customer_text(
                customer_id
            )
        )

        context.user_data.clear()

        await query.edit_message_text(
            text,
            reply_markup=_lead_customer_keyboard(
                customer_id
            ),
        )

        return True

    return True


async def handle_crm_text(update, context, session):
    state = context.user_data.get(
        "state"
    )

    if (
        not state
        or not state.startswith("crm_lead_")
    ):
        return await (
            _crm_leads_legacy_handle_crm_text(
                update,
                context,
                session,
            )
        )

    if not has_permission(
        session,
        "customers_manage",
    ):
        context.user_data.clear()
        return True

    text = (
        update.message.text
        or ""
    ).strip()

    user_id = session[0]

    if state == "crm_lead_visit_phone":
        phone_norm = normalize_phone(text)

        if (
            not phone_norm
            or not phone_norm.startswith("0")
            or len(phone_norm) < 10
            or len(phone_norm) > 12
        ):
            await update.message.reply_text(
                "❌ شماره معتبر نیست. "
                "شماره را با صفر اول ارسال کنید."
            )
            return True

        existing = _lead_customer_by_phone(
            phone_norm
        )

        if existing:
            customer_id = existing[0]

            _lead_ensure_access(
                customer_id,
                user_id,
            )

            context.user_data[
                "crm_lead_customer_id"
            ] = customer_id

            context.user_data[
                "state"
            ] = "crm_lead_visit_units"

            await update.message.reply_text(
                "✅ مشتری از روی شماره پیدا شد.\n\n"
                "🔢 کد واحد یا کد واحدهای موردنظر "
                "را ارسال کنید.\n"
                "مثال: 27027، 26527"
            )

            return True

        context.user_data[
            "crm_lead_phone"
        ] = text

        context.user_data[
            "state"
        ] = "crm_lead_visit_name"

        await update.message.reply_text(
            "این شماره قبلاً ثبت نشده است.\n\n"
            "👤 نام و نام خانوادگی مشتری را ارسال کنید."
        )

        return True

    if state == "crm_lead_visit_name":
        phone = context.user_data.get(
            "crm_lead_phone"
        )

        if not phone:
            context.user_data.clear()
            await update.message.reply_text(
                "⚠️ اطلاعات شماره منقضی شده است.",
                reply_markup=_main_back(),
            )
            return True

        try:
            customer_id, created = (
                _lead_get_or_create_customer(
                    session,
                    phone,
                    name=text,
                )
            )
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ {exc}"
            )
            return True

        context.user_data[
            "crm_lead_customer_id"
        ] = customer_id

        context.user_data[
            "state"
        ] = "crm_lead_visit_units"

        audit(
            user_id,
            (
                "CUSTOMER_ADDED"
                if created
                else "CUSTOMER_DUPLICATE_OPENED"
            ),
            customer_id=customer_id,
        )

        await update.message.reply_text(
            "✅ مشتری ثبت شد.\n\n"
            "🔢 کد واحد یا کد واحدهای موردنظر "
            "را ارسال کنید.\n"
            "مثال: 27027، 26527"
        )

        return True

    if state == "crm_lead_visit_units":
        codes = _lead_parse_unit_codes(
            text
        )

        if not codes:
            await update.message.reply_text(
                "❌ کد واحد تشخیص داده نشد."
            )
            return True

        properties = []
        missing = []

        for code in codes:
            row = _lead_find_property(code)

            if row:
                properties.append(row)
            else:
                missing.append(code)

        if missing:
            await update.message.reply_text(
                "❌ این کدها پیدا نشدند "
                "یا فعال نیستند:\n"
                + "، ".join(missing)
                + "\n\nهمه کدها را دوباره ارسال کنید."
            )
            return True

        customer_id = context.user_data.get(
            "crm_lead_customer_id"
        )

        if not customer_id:
            context.user_data.clear()
            await update.message.reply_text(
                "⚠️ اطلاعات مشتری منقضی شده است.",
                reply_markup=_main_back(),
            )
            return True

        visit_ids = _lead_create_visit_requests(
            customer_id,
            properties,
            user_id,
        )

        property_ids = [
            row[0]
            for row in properties
        ]

        context.user_data[
            "crm_lead_property_ids"
        ] = property_ids

        context.user_data[
            "crm_lead_visit_ids"
        ] = visit_ids

        context.user_data[
            "state"
        ] = "crm_lead_notes"

        audit(
            user_id,
            "VISIT_LEAD_ADDED",
            customer_id=customer_id,
            details={
                "property_ids": property_ids,
                "visit_ids": visit_ids,
            },
        )

        lines = [
            "✅ درخواست بازدید ثبت شد.",
            "",
        ]

        for index, row in enumerate(
            properties,
            start=1,
        ):
            lines.append(
                _lead_property_text(
                    row,
                    index,
                )
            )
            lines.append("")

        lines.extend([
            "",
            "📝 توضیحات مشتری را بنویسید.",
            "مثلاً: مشتری خیلی حساسه، "
            "فقط طبقات بالا، تماس عصرها و ...",
            "",
            "اگر توضیحی ندارید «رد کردن» را بزنید.",
        ])

        await update.message.reply_text(
            "\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⏭ رد کردن",
                        callback_data="crm_lead_notes_skip",
                    )
                ]
            ]),
            disable_web_page_preview=True,
        )

        return True

    if state == "crm_lead_notes":
        customer_id = context.user_data.get(
            "crm_lead_customer_id"
        )

        if not customer_id:
            context.user_data.clear()

            await update.message.reply_text(
                "⚠️ اطلاعات مشتری منقضی شده است.",
                reply_markup=_main_back(),
            )

            return True

        note = text.strip()

        if not note:
            await update.message.reply_text(
                "❌ توضیحات خالی است. "
                "متن را ارسال کنید یا دکمه «رد کردن» را بزنید."
            )
            return True

        if len(note) > 1500:
            await update.message.reply_text(
                "❌ توضیحات خیلی طولانی است. "
                "حداکثر ۱۵۰۰ کاراکتر ارسال کنید."
            )
            return True

        _lead_set_notes(
            customer_id,
            note,
        )

        audit(
            user_id,
            "CUSTOMER_NOTE_ADDED",
            customer_id=customer_id,
            details={
                "note_length": len(note),
            },
        )

        context.user_data[
            "state"
        ] = None

        await update.message.reply_text(
            "✅ توضیحات مشتری ثبت شد.\n\n"
            "این مشتری برای مشاور یا ادمین "
            "دیگری هم ارسال شود؟",
            reply_markup=_lead_share_prompt_keyboard(),
        )

        return True

    if state == "crm_lead_phone_name":
        if len(text) < 2:
            await update.message.reply_text(
                "❌ نام مشتری معتبر نیست."
            )
            return True

        context.user_data[
            "crm_lead_name"
        ] = text

        context.user_data[
            "state"
        ] = "crm_lead_phone_phone"

        await update.message.reply_text(
            "📞 شماره تماس مشتری را ارسال کنید."
        )

        return True

    if state == "crm_lead_phone_phone":
        try:
            customer_id, created = (
                _lead_get_or_create_customer(
                    session,
                    text,
                    name=context.user_data.get(
                        "crm_lead_name"
                    ),
                )
            )
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ {exc}"
            )
            return True

        context.user_data[
            "crm_lead_customer_id"
        ] = customer_id

        context.user_data[
            "state"
        ] = "crm_lead_phone_price"

        audit(
            user_id,
            (
                "CUSTOMER_ADDED"
                if created
                else "CUSTOMER_DUPLICATE_OPENED"
            ),
            customer_id=customer_id,
        )

        await update.message.reply_text(
            "💰 بازه قیمت موردنظر مشتری را "
            "به میلیارد تومان ارسال کنید.\n\n"
            "مثال:\n15 تا 25"
        )

        return True

    if state == "crm_lead_phone_price":
        parsed = _lead_parse_price_range(
            text
        )

        if not parsed:
            await update.message.reply_text(
                "❌ بازه قیمت تشخیص داده نشد.\n"
                "مثال: 15 تا 25"
            )
            return True

        customer_id = context.user_data.get(
            "crm_lead_customer_id"
        )

        if not customer_id:
            context.user_data.clear()
            await update.message.reply_text(
                "⚠️ اطلاعات مشتری منقضی شده است.",
                reply_markup=_main_back(),
            )
            return True

        low, high = parsed

        _lead_set_price_range(
            customer_id,
            low,
            high,
        )

        context.user_data[
            "crm_lead_property_ids"
        ] = []

        context.user_data[
            "state"
        ] = "crm_lead_notes"

        audit(
            user_id,
            "CUSTOMER_PRICE_RANGE",
            customer_id=customer_id,
            details={
                "min_billion": str(low),
                "max_billion": str(high),
            },
        )

        await update.message.reply_text(
            "✅ مشتری و بازه قیمت ثبت شد.\n\n"
            f"💰 {float(low):g} تا "
            f"{float(high):g} میلیارد تومان\n\n"
            "📝 توضیحات مشتری را بنویسید.\n"
            "مثلاً: مشتری خیلی حساسه، "
            "فقط طبقات بالا، تماس عصرها و ...\n\n"
            "اگر توضیحی ندارید «رد کردن» را بزنید.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⏭ رد کردن",
                        callback_data="crm_lead_notes_skip",
                    )
                ]
            ]),
        )

        return True

    return True


# === END CUSTOMER LEADS FLOW V1 ===


# =========================================================
# === CUSTOMER LEADS NOTES V1.1 ===
# Optional advisor/customer notes before sharing.
# =========================================================


# =========================================================
# === CRM SUITE V2 ===
# Customer editing/archive/history + follow-up reminders
# + visit follow-up controls + unified CRM search/report.
# =========================================================


def _crm_v2_access_sql(session, alias="cp", *, include_archived=False):
    archive_sql = "" if include_archived else f" AND {alias}.archived_at IS NULL"
    if is_owner(session):
        return archive_sql, []
    return (
        f"""
        AND (
            {alias}.owner_user_id = %s
            OR {alias}.created_by_user_id = %s
            OR EXISTS (
                SELECT 1
                FROM customer_shares cs_v2
                WHERE cs_v2.customer_id = {alias}.id
                  AND cs_v2.user_id = %s
            )
        )
        {archive_sql}
        """,
        [session[0], session[0], session[0]],
    )


def _crm_v2_customer_access(session, customer_id, *, include_archived=False):
    scope, params = _crm_v2_access_sql(
        session,
        "cp",
        include_archived=include_archived,
    )
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    cp.owner_user_id,
                    cp.created_by_user_id,
                    cp.archived_at
                FROM customer_profiles cp
                WHERE cp.id = %s
                """ + scope + """
                LIMIT 1
                """,
                [customer_id] + params,
            )
            return cur.fetchone()


def _crm_v2_can_manage_customer(session, customer_id):
    row = _crm_v2_customer_access(
        session,
        customer_id,
        include_archived=True,
    )
    if not row:
        return False
    if is_owner(session):
        return True
    return row[1] == session[0] or row[2] == session[0]


def _crm_v2_customer_shares(customer_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cs.user_id,
                    COALESCE(
                        NULLIF(BTRIM(u.display_name), ''),
                        u.username,
                        'کاربر'
                    ),
                    u.role,
                    u.is_active
                FROM customer_shares cs
                JOIN users u ON u.id = cs.user_id
                WHERE cs.customer_id = %s
                ORDER BY
                    CASE u.role
                        WHEN 'OWNER' THEN 0
                        WHEN 'ADMIN' THEN 1
                        ELSE 2
                    END,
                    u.id
                """,
                (customer_id,),
            )
            return cur.fetchall()


def _crm_v2_customer_record(customer_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(
                                CONCAT_WS(
                                    ' ',
                                    cp.first_name,
                                    cp.last_name
                                )
                            ),
                            ''
                        ),
                        'بدون نام'
                    ) AS customer_name,
                    cp.phone_number,
                    cp.desired_price_min_billion,
                    cp.desired_price_max_billion,
                    cp.notes,
                    cp.preferred_region,
                    cp.owner_user_id,
                    COALESCE(
                        NULLIF(BTRIM(owner_u.display_name), ''),
                        owner_u.username,
                        'دفتر'
                    ) AS owner_name,
                    cp.created_by_user_id,
                    COALESCE(
                        NULLIF(BTRIM(creator_u.display_name), ''),
                        creator_u.username,
                        'نامشخص'
                    ) AS creator_name,
                    cp.created_at,
                    cp.updated_at,
                    cp.last_interaction_at,
                    cp.archived_at,
                    (
                        SELECT COUNT(*)
                        FROM visit_requests vr
                        WHERE vr.customer_id = cp.id
                          AND vr.status IN (
                              'NEW',
                              'CONTACTED',
                              'SCHEDULED'
                          )
                    ) AS open_visits,
                    (
                        SELECT COUNT(*)
                        FROM customer_followups cf
                        WHERE cf.customer_id = cp.id
                          AND cf.status = 'PENDING'
                    ) AS pending_followups,
                    (
                        SELECT MIN(cf.due_at)
                        FROM customer_followups cf
                        WHERE cf.customer_id = cp.id
                          AND cf.status = 'PENDING'
                    ) AS next_followup
                FROM customer_profiles cp
                LEFT JOIN users owner_u
                    ON owner_u.id = cp.owner_user_id
                LEFT JOIN users creator_u
                    ON creator_u.id = cp.created_by_user_id
                WHERE cp.id = %s
                LIMIT 1
                """,
                (customer_id,),
            )
            return cur.fetchone()


def _crm_v2_format_dt(value):
    if not value:
        return "-"
    try:
        return value.astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _crm_v2_customer_text(customer_id):
    row = _crm_v2_customer_record(customer_id)
    if not row:
        return "⚠️ مشتری پیدا نشد."

    (
        cid,
        name,
        phone,
        low,
        high,
        notes,
        preferred_region,
        owner_id,
        owner_name,
        creator_id,
        creator_name,
        created_at,
        updated_at,
        last_interaction_at,
        archived_at,
        open_visits,
        pending_followups,
        next_followup,
    ) = row

    lines = [
        f"👤 مشتری #{cid}",
        "",
        f"نام: {name}",
        f"📞 شماره: {phone or '-'}",
        f"👤 مسئول: {owner_name}",
    ]

    if creator_name and creator_id != owner_id:
        lines.append(f"✍️ ثبت‌کننده: {creator_name}")

    if low is not None or high is not None:
        if low is not None and high is not None:
            price_text = f"{float(low):g} تا {float(high):g} میلیارد تومان"
        elif low is not None:
            price_text = f"از {float(low):g} میلیارد تومان"
        else:
            price_text = f"تا {float(high):g} میلیارد تومان"
        lines.append(f"💰 بازه قیمت: {price_text}")

    if preferred_region:
        lines.append(f"📍 منطقه موردنظر: {preferred_region}")

    shares = _crm_v2_customer_shares(customer_id)
    if shares:
        names = "، ".join(row[1] for row in shares if row[1])
        if names:
            lines.append(f"👥 اشتراک با: {names}")

    lines.extend([
        f"📅 بازدید باز: {open_visits}",
        f"⏰ پیگیری در انتظار: {pending_followups}",
    ])

    if next_followup:
        lines.append(
            "⏭ پیگیری بعدی: "
            + _crm_v2_format_dt(next_followup)
        )

    if notes:
        lines.extend([
            "",
            "📝 توضیحات:",
            str(notes).strip(),
        ])

    if archived_at:
        lines.extend([
            "",
            "🗄 وضعیت: آرشیوشده",
            f"زمان آرشیو: {_crm_v2_format_dt(archived_at)}",
        ])

    return "\n".join(lines)


# Upgrade all V1 customer detail responses without changing their call sites.
def _lead_customer_text(customer_id):
    return _crm_v2_customer_text(customer_id)


def _lead_customer_keyboard(customer_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📅 ثبت بازدید",
                callback_data=f"crm_visit_for_customer_{customer_id}",
            ),
            InlineKeyboardButton(
                "⏰ ثبت پیگیری",
                callback_data=f"crm_v2_followup_add_{customer_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "📝 افزودن یادداشت",
                callback_data=f"crm_v2_customer_note_{customer_id}",
            ),
            InlineKeyboardButton(
                "🧾 تاریخچه",
                callback_data=f"crm_v2_customer_history_{customer_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ مدیریت مشتری",
                callback_data=f"crm_v2_customer_manage_{customer_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ خواهان‌های اضافه‌شده",
                callback_data="crm_lead_added",
            )
        ],
    ])


def _crm_v2_customer_manage_keyboard(customer_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ نام",
                callback_data=f"crm_v2_customer_edit_name_{customer_id}",
            ),
            InlineKeyboardButton(
                "📞 شماره",
                callback_data=f"crm_v2_customer_edit_phone_{customer_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 بازه قیمت",
                callback_data=f"crm_v2_customer_edit_price_{customer_id}",
            ),
            InlineKeyboardButton(
                "📝 ویرایش توضیحات",
                callback_data=f"crm_v2_customer_edit_notes_{customer_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 اشتراک‌گذاری",
                callback_data=f"crm_v2_customer_share_{customer_id}",
            ),
            InlineKeyboardButton(
                "🗄 آرشیو مشتری",
                callback_data=f"crm_v2_customer_archive_{customer_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ مشتری",
                callback_data=f"crm_lead_view_{customer_id}",
            )
        ],
    ])


def _crm_v2_visible_where(session, *, include_archived=False):
    if is_owner(session):
        archive = "" if include_archived else " AND cp.archived_at IS NULL"
        return " WHERE 1=1" + archive, []
    archive = "" if include_archived else " AND cp.archived_at IS NULL"
    return (
        """
        WHERE (
            cp.owner_user_id = %s
            OR cp.created_by_user_id = %s
            OR EXISTS (
                SELECT 1
                FROM customer_shares cs_v2_scope
                WHERE cs_v2_scope.customer_id = cp.id
                  AND cs_v2_scope.user_id = %s
            )
        )
        """ + archive,
        [session[0], session[0], session[0]],
    )


def _lead_added_rows(session, offset=0, limit=15):
    user_id = session[0]
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(
                                CONCAT_WS(
                                    ' ',
                                    cp.first_name,
                                    cp.last_name
                                )
                            ),
                            ''
                        ),
                        'بدون نام'
                    ),
                    cp.phone_number,
                    cp.desired_price_min_billion,
                    cp.desired_price_max_billion
                FROM customer_profiles cp
                WHERE cp.archived_at IS NULL
                  AND (
                      cp.owner_user_id = %s
                      OR cp.created_by_user_id = %s
                      OR EXISTS (
                          SELECT 1
                          FROM customer_shares cs
                          WHERE cs.customer_id = cp.id
                            AND cs.user_id = %s
                      )
                  )
                ORDER BY
                    cp.updated_at DESC,
                    cp.id DESC
                LIMIT %s OFFSET %s
                """,
                (user_id, user_id, user_id, limit, offset),
            )
            return cur.fetchall()


def _lead_added_count(session):
    user_id = session[0]
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM customer_profiles cp
                WHERE cp.archived_at IS NULL
                  AND (
                      cp.owner_user_id = %s
                      OR cp.created_by_user_id = %s
                      OR EXISTS (
                          SELECT 1
                          FROM customer_shares cs
                          WHERE cs.customer_id = cp.id
                            AND cs.user_id = %s
                      )
                  )
                """,
                (user_id, user_id, user_id),
            )
            return int(cur.fetchone()[0] or 0)


def _lead_added_keyboard(session, offset=0, limit=15):
    total = _lead_added_count(session)
    rows = []

    for customer_id, name, phone, low, high in _lead_added_rows(
        session,
        offset,
        limit,
    ):
        price_text = ""
        if low is not None and high is not None:
            price_text = f" | {float(low):g}-{float(high):g}"
        rows.append([
            InlineKeyboardButton(
                f"📞 {phone or '-'} | {name}{price_text}"[:60],
                callback_data=f"crm_lead_view_{customer_id}",
            )
        ])

    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️ قبلی",
                callback_data=f"crm_lead_added_page_{max(0, offset-limit)}",
            )
        )
    if offset + limit < total:
        nav.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=f"crm_lead_added_page_{offset+limit}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(
            "➕ ثبت مشتری",
            callback_data="crm_lead_add",
        ),
        InlineKeyboardButton(
            "🔎 جستجو",
            callback_data="crm_customer_search",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            "🗄 آرشیو مشتریان",
            callback_data="crm_v2_archive_list",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ منوی اصلی",
            callback_data="home",
        )
    ])
    return InlineKeyboardMarkup(rows), total


def _crm_v2_search_customers(session, query, limit=20):
    q = str(query or "").strip()
    norm = normalize_phone(q)
    scope, params = _crm_v2_visible_where(session)
    pattern = f"%{q}%"
    phone_pattern = f"%{norm}%" if norm else pattern
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(
                                CONCAT_WS(
                                    ' ',
                                    cp.first_name,
                                    cp.last_name
                                )
                            ),
                            ''
                        ),
                        'بدون نام'
                    ) AS customer_name,
                    cp.phone_number,
                    COALESCE(
                        NULLIF(BTRIM(u.display_name), ''),
                        u.username,
                        'دفتر'
                    ) AS owner_name
                FROM customer_profiles cp
                LEFT JOIN users u ON u.id = cp.owner_user_id
                """ + scope + """
                  AND (
                      CONCAT_WS(' ', cp.first_name, cp.last_name) ILIKE %s
                      OR COALESCE(cp.phone_number, '') ILIKE %s
                      OR COALESCE(cp.phone_normalized, '') ILIKE %s
                  )
                ORDER BY cp.updated_at DESC, cp.id DESC
                LIMIT %s
                """,
                params + [pattern, pattern, phone_pattern, limit],
            )
            return cur.fetchall()


def _crm_v2_archive_rows(session, limit=50):
    scope, params = _crm_v2_visible_where(
        session,
        include_archived=True,
    )
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(
                                CONCAT_WS(
                                    ' ',
                                    cp.first_name,
                                    cp.last_name
                                )
                            ),
                            ''
                        ),
                        'بدون نام'
                    ),
                    cp.phone_number,
                    cp.archived_at
                FROM customer_profiles cp
                """ + scope + """
                  AND cp.archived_at IS NOT NULL
                ORDER BY cp.archived_at DESC, cp.id DESC
                LIMIT %s
                """,
                params + [limit],
            )
            return cur.fetchall()


def _crm_v2_archive_keyboard(session):
    rows = []
    for customer_id, name, phone, archived_at in _crm_v2_archive_rows(session):
        rows.append([
            InlineKeyboardButton(
                f"🗄 {phone or '-'} | {name}"[:60],
                callback_data=f"crm_v2_archive_view_{customer_id}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ خواهان‌های اضافه‌شده",
            callback_data="crm_lead_added",
        )
    ])
    return InlineKeyboardMarkup(rows)


def _crm_v2_history_text(customer_id):
    labels = {
        "CUSTOMER_ADDED": "ثبت مشتری",
        "CUSTOMER_DUPLICATE_OPENED": "باز کردن مشتری موجود",
        "CUSTOMER_SHARED": "اشتراک‌گذاری",
        "CUSTOMER_ASSIGNED": "واگذاری مشتری",
        "CUSTOMER_PRICE_RANGE": "ثبت بازه قیمت",
        "CUSTOMER_NOTE_ADDED": "افزودن یادداشت",
        "CUSTOMER_EDIT_NAME": "ویرایش نام",
        "CUSTOMER_EDIT_PHONE": "ویرایش شماره",
        "CUSTOMER_EDIT_PRICE": "ویرایش بازه قیمت",
        "CUSTOMER_EDIT_NOTES": "ویرایش توضیحات",
        "CUSTOMER_ARCHIVED": "آرشیو مشتری",
        "CUSTOMER_RESTORED": "بازیابی مشتری",
        "FOLLOWUP_CREATED": "ثبت پیگیری",
        "FOLLOWUP_DONE": "انجام پیگیری",
        "FOLLOWUP_CANCELLED": "لغو پیگیری",
        "VISIT_LEAD_ADDED": "ثبت درخواست بازدید",
        "VISIT_SCHEDULED": "تعیین وقت بازدید",
        "VISIT_STATUS": "تغییر وضعیت بازدید",
        "VISIT_CONTACTED": "تماس با مشتری",
        "VISIT_NOTE_ADDED": "یادداشت بازدید",
    }
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.action,
                    a.created_at,
                    COALESCE(
                        NULLIF(BTRIM(u.display_name), ''),
                        u.username,
                        'سیستم'
                    ) AS actor_name
                FROM admin_audit_log a
                LEFT JOIN users u ON u.id = a.actor_user_id
                LEFT JOIN visit_requests vr ON vr.id = a.visit_id
                WHERE a.customer_id = %s
                   OR vr.customer_id = %s
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 20
                """,
                (customer_id, customer_id),
            )
            rows = cur.fetchall()

    if not rows:
        return "🧾 تاریخچه مشتری\n\nهنوز رویدادی ثبت نشده است."

    lines = ["🧾 تاریخچه مشتری", ""]
    for action, created_at, actor in rows:
        label = labels.get(action, action)
        lines.append(
            f"• {_crm_v2_format_dt(created_at)} | {actor} | {label}"
        )
    return "\n".join(lines)


def _crm_v2_customer_property_ids(customer_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT property_id
                FROM visit_requests
                WHERE customer_id = %s
                  AND property_id IS NOT NULL
                  AND status IN ('NEW','CONTACTED','SCHEDULED')
                ORDER BY property_id
                """,
                (customer_id,),
            )
            return [row[0] for row in cur.fetchall() if row and row[0]]


def _crm_v2_share_keyboard(session, customer_id, selected_ids):
    selected = {int(x) for x in (selected_ids or [])}
    rows = []
    for target_id, name, role in _lead_share_users(session[0]):
        role_fa = {
            "OWNER": "مالک",
            "ADMIN": "ادمین",
            "ADVISOR": "مشاور",
        }.get(role, role)
        icon = "✅" if target_id in selected else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{icon} {name} | {role_fa}"[:60],
                callback_data=f"crm_v2_share_toggle_{target_id}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "✅ ذخیره اشتراک‌ها",
            callback_data="crm_v2_share_finish",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ مدیریت مشتری",
            callback_data=f"crm_v2_customer_manage_{customer_id}",
        )
    ])
    return InlineKeyboardMarkup(rows)


def _crm_v2_sync_shares(customer_id, selected_ids, actor_id):
    selected = {int(x) for x in selected_ids}
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM customer_shares WHERE customer_id = %s",
                (customer_id,),
            )
            before = {row[0] for row in cur.fetchall()}

            cur.execute(
                "DELETE FROM customer_shares WHERE customer_id = %s",
                (customer_id,),
            )
            for target_id in sorted(selected):
                cur.execute(
                    """
                    INSERT INTO customer_shares(
                        customer_id,
                        user_id,
                        shared_by_user_id,
                        created_at
                    )
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (customer_id, user_id)
                    DO UPDATE SET
                        shared_by_user_id = EXCLUDED.shared_by_user_id,
                        created_at = NOW()
                    """,
                    (customer_id, target_id, actor_id),
                )
        conn.commit()
    return before, selected


def _crm_v2_create_followup(customer_id, assigned_user_id, created_by_user_id, due_at, note=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_followups(
                    customer_id,
                    assigned_user_id,
                    created_by_user_id,
                    due_at,
                    note,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, 'PENDING', NOW(), NOW())
                RETURNING id
                """,
                (
                    customer_id,
                    assigned_user_id,
                    created_by_user_id,
                    due_at,
                    note,
                ),
            )
            followup_id = cur.fetchone()[0]
        conn.commit()
    return followup_id


def _crm_v2_followup_scope(session, alias="cf"):
    if is_owner(session):
        return "", []
    return f" AND {alias}.assigned_user_id = %s", [session[0]]


def _crm_v2_followup_rows(session, limit=30):
    scope, params = _crm_v2_followup_scope(session, "cf")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cf.id,
                    cf.due_at,
                    cf.status,
                    cf.note,
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)),
                            ''
                        ),
                        'بدون نام'
                    ),
                    cp.phone_number,
                    COALESCE(
                        NULLIF(BTRIM(u.display_name), ''),
                        u.username,
                        'کاربر'
                    )
                FROM customer_followups cf
                JOIN customer_profiles cp ON cp.id = cf.customer_id
                LEFT JOIN users u ON u.id = cf.assigned_user_id
                WHERE cf.status = 'PENDING'
                """ + scope + """
                ORDER BY cf.due_at ASC, cf.id ASC
                LIMIT %s
                """,
                params + [limit],
            )
            return cur.fetchall()


def _crm_v2_followup_detail(session, followup_id):
    scope, params = _crm_v2_followup_scope(session, "cf")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cf.id,
                    cf.customer_id,
                    cf.assigned_user_id,
                    cf.created_by_user_id,
                    cf.due_at,
                    cf.note,
                    cf.status,
                    cf.reminded_at,
                    cf.completed_at,
                    cf.cancelled_at,
                    COALESCE(
                        NULLIF(
                            BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)),
                            ''
                        ),
                        'بدون نام'
                    ) AS customer_name,
                    cp.phone_number,
                    COALESCE(
                        NULLIF(BTRIM(u.display_name), ''),
                        u.username,
                        'کاربر'
                    ) AS assigned_name
                FROM customer_followups cf
                JOIN customer_profiles cp ON cp.id = cf.customer_id
                LEFT JOIN users u ON u.id = cf.assigned_user_id
                WHERE cf.id = %s
                """ + scope + """
                LIMIT 1
                """,
                [followup_id] + params,
            )
            return cur.fetchone()


def _crm_v2_followup_text(row):
    if not row:
        return "⚠️ پیگیری پیدا نشد."
    (
        followup_id,
        customer_id,
        assigned_user_id,
        created_by_user_id,
        due_at,
        note,
        status,
        reminded_at,
        completed_at,
        cancelled_at,
        customer_name,
        phone,
        assigned_name,
    ) = row
    status_fa = {
        "PENDING": "در انتظار",
        "DONE": "انجام‌شده",
        "CANCELLED": "لغوشده",
    }.get(status, status)
    overdue = status == "PENDING" and due_at and due_at < datetime.now(TEHRAN)
    lines = [
        f"⏰ پیگیری #{followup_id}",
        "",
        f"وضعیت: {status_fa}" + (" 🔴 عقب‌افتاده" if overdue else ""),
        f"👤 مشتری: {customer_name}",
        f"📞 {phone or '-'}",
        f"🧑‍💼 مسئول: {assigned_name}",
        f"🕐 زمان: {_crm_v2_format_dt(due_at)}",
        f"📝 توضیح: {note or '-'}",
    ]
    return "\n".join(lines)


def _crm_v2_followups_keyboard(session):
    rows = []
    now = datetime.now(TEHRAN)
    for followup_id, due_at, status, note, customer_id, name, phone, assigned in _crm_v2_followup_rows(session):
        local = due_at.astimezone(TEHRAN) if due_at else None
        time_text = local.strftime("%m/%d %H:%M") if local else "-"
        icon = "🔴" if due_at and due_at < now else "⏰"
        rows.append([
            InlineKeyboardButton(
                f"{icon} {time_text} | {name}"[:60],
                callback_data=f"crm_v2_followup_view_{followup_id}",
            )
        ])
    if not rows:
        rows.append([
            InlineKeyboardButton(
                "➕ ثبت مشتری/پیگیری از لیست خواهان‌ها",
                callback_data="crm_lead_added",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ منوی اصلی",
            callback_data="home",
        )
    ])
    return InlineKeyboardMarkup(rows)


def _crm_v2_followup_detail_keyboard(row):
    followup_id = row[0]
    customer_id = row[1]
    status = row[6]
    rows = []
    if status == "PENDING":
        rows.append([
            InlineKeyboardButton(
                "✅ انجام شد",
                callback_data=f"crm_v2_followup_done_{followup_id}",
            ),
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"crm_v2_followup_cancel_{followup_id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            "👤 مشاهده مشتری",
            callback_data=f"crm_lead_view_{customer_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ پیگیری‌ها",
            callback_data="crm_v2_followups",
        )
    ])
    return InlineKeyboardMarkup(rows)


def _crm_v2_set_followup_status(followup_id, status):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_followups
                SET
                    status = %s,
                    completed_at = CASE
                        WHEN %s = 'DONE' THEN NOW()
                        ELSE completed_at
                    END,
                    cancelled_at = CASE
                        WHEN %s = 'CANCELLED' THEN NOW()
                        ELSE cancelled_at
                    END,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING customer_id
                """,
                (status, status, status, followup_id),
            )
            row = cur.fetchone()
            if row and status == "DONE":
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET last_interaction_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (row[0],),
                )
        conn.commit()
    return row[0] if row else None


def _crm_v2_visit_keyboard(session, row):
    visit_id = row[0]
    status = row[1]
    customer_id = row[7]
    rows = []
    if status == "NEW":
        rows.append([
            InlineKeyboardButton(
                "📞 تماس گرفته شد",
                callback_data=f"crm_v2_visit_contact_{visit_id}",
            ),
            InlineKeyboardButton(
                "🕐 تعیین وقت",
                callback_data=f"crm_v2_visit_schedule_{visit_id}",
            ),
        ])
    elif status == "CONTACTED":
        rows.append([
            InlineKeyboardButton(
                "🕐 تعیین وقت",
                callback_data=f"crm_v2_visit_schedule_{visit_id}",
            )
        ])
    elif status == "SCHEDULED":
        rows.append([
            InlineKeyboardButton(
                "🕐 تغییر زمان",
                callback_data=f"crm_v2_visit_schedule_{visit_id}",
            )
        ])
    if status in ("NEW", "CONTACTED", "SCHEDULED"):
        rows.append([
            InlineKeyboardButton(
                "📝 یادداشت بازدید",
                callback_data=f"crm_v2_visit_note_{visit_id}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                "✅ انجام شد",
                callback_data=f"crm_visit_done_{visit_id}",
            ),
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"crm_visit_cancel_{visit_id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            "👤 مشتری",
            callback_data=f"crm_lead_view_{customer_id}",
        ),
        InlineKeyboardButton(
            "⬅️ بازدیدها",
            callback_data="crm_visits",
        ),
    ])
    return InlineKeyboardMarkup(rows)


def _crm_v2_report_text(session):
    scope, params = _crm_v2_visible_where(session)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM customer_profiles cp" + scope,
                params,
            )
            active_customers = cur.fetchone()[0]

            scope_all, params_all = _crm_v2_visible_where(
                session,
                include_archived=True,
            )
            cur.execute(
                "SELECT COUNT(*) FROM customer_profiles cp" + scope_all + " AND cp.archived_at IS NOT NULL",
                params_all,
            )
            archived_customers = cur.fetchone()[0]

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE cp.desired_price_min_billion IS NOT NULL
                           OR cp.desired_price_max_billion IS NOT NULL
                    ),
                    COUNT(*) FILTER (
                        WHERE cp.notes IS NOT NULL
                          AND BTRIM(cp.notes) <> ''
                    )
                FROM customer_profiles cp
                """ + scope,
                params,
            )
            with_price, with_notes = cur.fetchone()

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE vr.status IN ('NEW','CONTACTED','SCHEDULED')),
                    COUNT(*) FILTER (WHERE vr.status = 'CLOSED'),
                    COUNT(*) FILTER (WHERE vr.status = 'CANCELLED')
                FROM visit_requests vr
                JOIN customer_profiles cp ON cp.id = vr.customer_id
                """ + scope,
                params,
            )
            open_visits, done_visits, cancelled_visits = cur.fetchone()

            if is_owner(session):
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'PENDING'),
                        COUNT(*) FILTER (
                            WHERE status = 'PENDING'
                              AND due_at < NOW()
                        ),
                        COUNT(*) FILTER (WHERE status = 'DONE')
                    FROM customer_followups
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'PENDING'),
                        COUNT(*) FILTER (
                            WHERE status = 'PENDING'
                              AND due_at < NOW()
                        ),
                        COUNT(*) FILTER (WHERE status = 'DONE')
                    FROM customer_followups
                    WHERE assigned_user_id = %s
                    """,
                    (session[0],),
                )
            pending_followups, overdue_followups, done_followups = cur.fetchone()

            advisor_rows = []
            if is_owner(session):
                cur.execute(
                    """
                    SELECT
                        COALESCE(
                            NULLIF(BTRIM(u.display_name), ''),
                            u.username,
                            'بدون مسئول'
                        ),
                        COUNT(*)
                    FROM customer_profiles cp
                    LEFT JOIN users u ON u.id = cp.owner_user_id
                    WHERE cp.archived_at IS NULL
                    GROUP BY 1
                    ORDER BY 2 DESC, 1
                    LIMIT 15
                    """
                )
                advisor_rows = cur.fetchall()

    lines = [
        "📊 گزارش CRM",
        "",
        f"👥 مشتری فعال: {active_customers}",
        f"🗄 آرشیوشده: {archived_customers}",
        f"💰 دارای بازه قیمت: {with_price}",
        f"📝 دارای توضیحات: {with_notes}",
        "",
        f"📅 بازدید باز: {open_visits}",
        f"✅ بازدید انجام‌شده: {done_visits}",
        f"❌ بازدید لغوشده: {cancelled_visits}",
        "",
        f"⏰ پیگیری در انتظار: {pending_followups}",
        f"🔴 پیگیری عقب‌افتاده: {overdue_followups}",
        f"✅ پیگیری انجام‌شده: {done_followups}",
    ]

    if advisor_rows:
        lines.extend(["", "🧑‍💼 مشتری فعال بر اساس مسئول:"])
        lines.extend(f"• {name}: {count}" for name, count in advisor_rows)

    return "\n".join(lines)


def _crm_v2_due_followups(limit=50):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    cf.id,
                    cf.assigned_user_id,
                    cf.due_at,
                    cf.note,
                    cp.id,
                    COALESCE(
                        NULLIF(
                            BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)),
                            ''
                        ),
                        'بدون نام'
                    ),
                    cp.phone_number
                FROM customer_followups cf
                JOIN customer_profiles cp ON cp.id = cf.customer_id
                JOIN users u ON u.id = cf.assigned_user_id
                WHERE cf.status = 'PENDING'
                  AND cf.reminded_at IS NULL
                  AND cf.due_at <= NOW()
                  AND u.is_active = TRUE
                ORDER BY cf.due_at, cf.id
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()


def _crm_v2_mark_followup_reminded(followup_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_followups
                SET reminded_at = NOW(), updated_at = NOW()
                WHERE id = %s
                  AND reminded_at IS NULL
                """,
                (followup_id,),
            )
        conn.commit()


async def run_customer_followup_reminders(application):
    for followup_id, assigned_user_id, due_at, note, customer_id, name, phone in _crm_v2_due_followups():
        telegram_id = _latest_telegram_user_id(assigned_user_id)
        if not telegram_id:
            continue
        text = (
            "⏰ یادآوری پیگیری مشتری\n\n"
            f"👤 {name}\n"
            f"📞 {phone or '-'}\n"
            f"🕐 زمان پیگیری: {_crm_v2_format_dt(due_at)}\n"
            f"📝 {note or '-'}"
        )
        try:
            await application.bot.send_message(
                chat_id=telegram_id,
                text=text,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "مشاهده پیگیری",
                            callback_data=f"crm_v2_followup_view_{followup_id}",
                        )
                    ]
                ]),
            )
            _crm_v2_mark_followup_reminded(followup_id)
        except Exception:
            logging.exception(
                "CRM follow-up reminder failed followup_id=%s user_id=%s",
                followup_id,
                assigned_user_id,
            )


async def _crm_v2_followup_loop(application):
    logging.info("CRM customer follow-up reminder worker started.")
    while True:
        try:
            await run_customer_followup_reminders(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("CRM follow-up reminder worker error")
        await asyncio.sleep(60)


# --- defensive schema extension ---
_crm_v2_legacy_ensure_crm_schema = ensure_crm_schema


def ensure_crm_schema():
    _crm_v2_legacy_ensure_crm_schema()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE customer_profiles
                ADD COLUMN IF NOT EXISTS desired_price_min_billion NUMERIC(12,3)
                """
            )
            cur.execute(
                """
                ALTER TABLE customer_profiles
                ADD COLUMN IF NOT EXISTS desired_price_max_billion NUMERIC(12,3)
                """
            )
            cur.execute(
                """
                ALTER TABLE customer_profiles
                ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ
                """
            )
            cur.execute(
                """
                ALTER TABLE customer_profiles
                ADD COLUMN IF NOT EXISTS archived_by_user_id BIGINT
                REFERENCES users(id) ON DELETE SET NULL
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_shares (
                    customer_id BIGINT NOT NULL
                        REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
                    shared_by_user_id BIGINT
                        REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (customer_id, user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_followups (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT NOT NULL
                        REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    assigned_user_id BIGINT NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
                    created_by_user_id BIGINT
                        REFERENCES users(id) ON DELETE SET NULL,
                    due_at TIMESTAMPTZ NOT NULL,
                    note TEXT,
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','DONE','CANCELLED')),
                    reminded_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    cancelled_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer_followups_customer ON customer_followups(customer_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer_followups_assigned ON customer_followups(assigned_user_id, status, due_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer_followups_due ON customer_followups(status, due_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer_profiles_archived ON customer_profiles(archived_at)"
            )
        conn.commit()


# --- centralized permission mapping for V1/V2 CRM callbacks ---
_crm_v2_legacy_required_permission = _required_permission


def _required_permission(action):
    action = action or ""
    if action.startswith("crm_lead_"):
        return "customers_manage"
    if action.startswith("crm_v2_visit_"):
        return "visits_manage_own"
    if action.startswith("crm_v2_"):
        return "customers_manage"
    return _crm_v2_legacy_required_permission(action)


# --- add Follow-ups + CRM report to the main menu ---
_crm_v2_legacy_build_main_menu = build_main_menu


def build_main_menu(session, legacy_keyboard):
    markup = _crm_v2_legacy_build_main_menu(session, legacy_keyboard)
    rows = [list(row) for row in (markup.inline_keyboard if markup else [])]
    if has_permission(session, "customers_manage"):
        callbacks = {
            button.callback_data
            for row in rows
            for button in row
            if button.callback_data
        }
        if "crm_v2_followups" not in callbacks:
            insert_at = max(0, len(rows) - 1)
            rows.insert(
                insert_at,
                [
                    InlineKeyboardButton(
                        "⏰ پیگیری‌ها",
                        callback_data="crm_v2_followups",
                    ),
                    InlineKeyboardButton(
                        "📊 گزارش CRM",
                        callback_data="crm_v2_report",
                    ),
                ],
            )
    return InlineKeyboardMarkup(rows)


# --- start the new reminder worker together with the existing visit worker ---
_crm_v2_legacy_start_visit_reminder_worker = start_visit_reminder_worker


def start_visit_reminder_worker(application):
    visit_task = _crm_v2_legacy_start_visit_reminder_worker(application)
    existing = application.bot_data.get("crm_customer_followup_task")
    if not existing or existing.done():
        application.bot_data["crm_customer_followup_task"] = asyncio.create_task(
            _crm_v2_followup_loop(application),
            name="crm_customer_followups",
        )
    return visit_task


# --- V2 callback wrapper ---
_crm_v2_legacy_handle_crm_button = handle_crm_button


async def handle_crm_button(update, context, session):
    query = update.callback_query
    action = (query.data if query else "") or ""
    user_id = session[0]

    # Consolidate the old customer list into the new lead list.
    if action == "crm_customers":
        keyboard, total = _lead_added_keyboard(session, 0)
        await query.edit_message_text(
            "👥 خواهان‌های اضافه‌شده\n\n"
            "مشتری‌های فعال شما و مشتری‌هایی که با شما به اشتراک گذاشته شده‌اند.\n\n"
            f"تعداد: {total}",
            reply_markup=keyboard,
        )
        return True

    if action == "crm_customer_search":
        if not has_permission(session, "customers_manage"):
            await query.edit_message_text("⛔ دسترسی ندارید.", reply_markup=_main_back())
            return True
        context.user_data.clear()
        context.user_data["state"] = "crm_v2_search"
        await query.edit_message_text(
            "🔎 جستجوی خواهان\n\nنام یا شماره تماس را ارسال کنید.",
            reply_markup=_main_back(),
        )
        return True

    if action.startswith("crm_customer_view_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_customer_access(session, customer_id):
            await query.edit_message_text("⛔ این مشتری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        await query.edit_message_text(
            _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if action.startswith("crm_customer_doassign_"):
        if not is_owner(session):
            return False
        payload = action[len("crm_customer_doassign_"):]
        customer_id, target_id = map(int, payload.split("_", 1))
        _assign_customer(customer_id, target_id)
        audit(
            user_id,
            "CUSTOMER_ASSIGNED",
            target_user_id=target_id,
            customer_id=customer_id,
        )
        await query.edit_message_text(
            "✅ مشتری واگذار شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if action.startswith("crm_lead_view_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_customer_access(session, customer_id):
            await query.edit_message_text("⛔ این مشتری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        await query.edit_message_text(
            _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if action.startswith("crm_visit_view_"):
        visit_id = int(action.rsplit("_", 1)[1])
        row = _visit_detail(session, visit_id)
        if not row:
            await query.edit_message_text("⛔ این بازدید در دسترس شما نیست.", reply_markup=_main_back())
            return True
        await query.edit_message_text(
            _visit_text(row),
            reply_markup=_crm_v2_visit_keyboard(session, row),
            disable_web_page_preview=True,
        )
        return True

    if not action.startswith("crm_v2_"):
        return await _crm_v2_legacy_handle_crm_button(update, context, session)

    if action.startswith("crm_v2_visit_"):
        if not has_permission(session, "visits_manage_own"):
            await query.edit_message_text("⛔ دسترسی ندارید.", reply_markup=_main_back())
            return True
    elif not has_permission(session, "customers_manage"):
        await query.edit_message_text("⛔ دسترسی ندارید.", reply_markup=_main_back())
        return True

    if action == "crm_v2_followups":
        rows = _crm_v2_followup_rows(session)
        overdue = sum(
            1
            for row in rows
            if row[1] and row[1] < datetime.now(TEHRAN)
        )
        await query.edit_message_text(
            "⏰ پیگیری‌ها\n\n"
            f"در انتظار: {len(rows)}\n"
            f"عقب‌افتاده: {overdue}",
            reply_markup=_crm_v2_followups_keyboard(session),
        )
        return True

    if action == "crm_v2_report":
        await query.edit_message_text(
            _crm_v2_report_text(session)[:4000],
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")]
            ]),
        )
        return True

    if action == "crm_v2_archive_list":
        archived = _crm_v2_archive_rows(session)
        await query.edit_message_text(
            "🗄 آرشیو مشتریان\n\n"
            f"تعداد نمایش‌داده‌شده: {len(archived)}",
            reply_markup=_crm_v2_archive_keyboard(session),
        )
        return True

    if action.startswith("crm_v2_archive_view_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_customer_access(session, customer_id, include_archived=True):
            await query.edit_message_text("⛔ این مشتری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        rows = []
        if _crm_v2_can_manage_customer(session, customer_id):
            rows.append([
                InlineKeyboardButton(
                    "♻️ بازیابی از آرشیو",
                    callback_data=f"crm_v2_customer_restore_{customer_id}",
                )
            ])
        rows.append([
            InlineKeyboardButton("⬅️ آرشیو", callback_data="crm_v2_archive_list")
        ])
        await query.edit_message_text(
            _crm_v2_customer_text(customer_id),
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.startswith("crm_v2_customer_manage_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            await query.edit_message_text(
                "⛔ فقط مسئول/ثبت‌کننده مشتری یا مالک سیستم می‌تواند اطلاعات اصلی را مدیریت کند.",
                reply_markup=_lead_customer_keyboard(customer_id),
            )
            return True
        await query.edit_message_text(
            "⚙️ مدیریت مشتری\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_crm_v2_customer_manage_keyboard(customer_id),
        )
        return True

    if action.startswith("crm_v2_customer_edit_name_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_edit_name",
            "crm_v2_customer_id": customer_id,
        })
        await query.edit_message_text("✏️ نام و نام خانوادگی جدید را ارسال کنید.", reply_markup=_main_back())
        return True

    if action.startswith("crm_v2_customer_edit_phone_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_edit_phone",
            "crm_v2_customer_id": customer_id,
        })
        await query.edit_message_text("📞 شماره تماس جدید را ارسال کنید.", reply_markup=_main_back())
        return True

    if action.startswith("crm_v2_customer_edit_price_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_edit_price",
            "crm_v2_customer_id": customer_id,
        })
        await query.edit_message_text(
            "💰 بازه قیمت جدید را به میلیارد تومان ارسال کنید.\n"
            "مثال: 15 تا 25\n\n"
            "برای پاک کردن بازه، کلمه «حذف» را ارسال کنید.",
            reply_markup=_main_back(),
        )
        return True

    if action.startswith("crm_v2_customer_edit_notes_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_edit_notes",
            "crm_v2_customer_id": customer_id,
        })
        await query.edit_message_text(
            "📝 متن کامل توضیحات جدید را ارسال کنید.\n\n"
            "برای پاک کردن توضیحات، کلمه «حذف» را ارسال کنید.",
            reply_markup=_main_back(),
        )
        return True

    if action.startswith("crm_v2_customer_note_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_customer_access(session, customer_id):
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_add_note",
            "crm_v2_customer_id": customer_id,
        })
        await query.edit_message_text(
            "📝 یادداشت جدید را ارسال کنید.\n"
            "یادداشت به توضیحات قبلی اضافه می‌شود.",
            reply_markup=_main_back(),
        )
        return True

    if action.startswith("crm_v2_customer_history_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_customer_access(session, customer_id, include_archived=True):
            return True
        await query.edit_message_text(
            _crm_v2_history_text(customer_id)[:4000],
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ مشتری", callback_data=f"crm_lead_view_{customer_id}")]
            ]),
        )
        return True

    if action.startswith("crm_v2_customer_share_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            await query.edit_message_text("⛔ اجازه مدیریت اشتراک این مشتری را ندارید.", reply_markup=_main_back())
            return True
        selected = [row[0] for row in _crm_v2_customer_shares(customer_id)]
        valid = {row[0] for row in _lead_share_users(user_id)}
        selected = sorted(x for x in selected if x in valid)
        context.user_data.clear()
        context.user_data.update({
            "crm_v2_share_customer_id": customer_id,
            "crm_v2_share_selected": selected,
        })
        await query.edit_message_text(
            "👥 اشتراک‌گذاری مشتری\n\nافراد موردنظر را روشن/خاموش کنید:",
            reply_markup=_crm_v2_share_keyboard(session, customer_id, selected),
        )
        return True

    if action.startswith("crm_v2_share_toggle_"):
        customer_id = context.user_data.get("crm_v2_share_customer_id")
        if not customer_id or not _crm_v2_can_manage_customer(session, customer_id):
            context.user_data.clear()
            await query.edit_message_text("⚠️ اطلاعات اشتراک منقضی شده است.", reply_markup=_main_back())
            return True
        target_id = int(action.rsplit("_", 1)[1])
        valid = {row[0] for row in _lead_share_users(user_id)}
        if target_id not in valid:
            return True
        selected = set(context.user_data.get("crm_v2_share_selected", []))
        if target_id in selected:
            selected.remove(target_id)
        else:
            selected.add(target_id)
        context.user_data["crm_v2_share_selected"] = sorted(selected)
        await query.edit_message_reply_markup(
            reply_markup=_crm_v2_share_keyboard(session, customer_id, selected)
        )
        return True

    if action == "crm_v2_share_finish":
        customer_id = context.user_data.get("crm_v2_share_customer_id")
        if not customer_id or not _crm_v2_can_manage_customer(session, customer_id):
            context.user_data.clear()
            await query.edit_message_text("⚠️ اطلاعات اشتراک منقضی شده است.", reply_markup=_main_back())
            return True
        valid = {row[0] for row in _lead_share_users(user_id)}
        selected = sorted(
            int(x)
            for x in context.user_data.get("crm_v2_share_selected", [])
            if int(x) in valid
        )
        before, after = _crm_v2_sync_shares(customer_id, selected, user_id)
        newly_added = sorted(after - before)
        property_ids = _crm_v2_customer_property_ids(customer_id)
        sent = 0
        failed = []
        if newly_added:
            sent, failed = await _lead_notify_users(
                context,
                customer_id,
                property_ids,
                newly_added,
            )
        audit(
            user_id,
            "CUSTOMER_SHARED",
            customer_id=customer_id,
            details={
                "shared_user_ids": selected,
                "newly_added": newly_added,
                "removed": sorted(before - after),
                "notification_sent": sent,
                "notification_failed": failed,
            },
        )
        context.user_data.clear()
        await query.edit_message_text(
            "✅ اشتراک‌های مشتری ذخیره شد.\n\n"
            + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if action.startswith("crm_v2_customer_archive_") and not action.startswith("crm_v2_customer_archive_do_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        await query.edit_message_text(
            "🗄 آرشیو مشتری\n\n"
            "مشتری از لیست فعال خارج می‌شود و پیگیری‌های در انتظار او لغو می‌شوند.\n"
            "سوابق و بازدیدها حذف نمی‌شوند.\n\n"
            "تأیید می‌کنید؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، آرشیو شود", callback_data=f"crm_v2_customer_archive_do_{customer_id}")],
                [InlineKeyboardButton("⬅️ انصراف", callback_data=f"crm_lead_view_{customer_id}")],
            ]),
        )
        return True

    if action.startswith("crm_v2_customer_archive_do_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET archived_at = NOW(), archived_by_user_id = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (user_id, customer_id),
                )
                cur.execute(
                    """
                    UPDATE customer_followups
                    SET status = 'CANCELLED', cancelled_at = NOW(), updated_at = NOW()
                    WHERE customer_id = %s AND status = 'PENDING'
                    """,
                    (customer_id,),
                )
            conn.commit()
        audit(user_id, "CUSTOMER_ARCHIVED", customer_id=customer_id)
        await query.edit_message_text(
            "✅ مشتری آرشیو شد.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗄 مشاهده آرشیو", callback_data="crm_v2_archive_list")],
                [InlineKeyboardButton("⬅️ خواهان‌ها", callback_data="crm_lead_added")],
            ]),
        )
        return True

    if action.startswith("crm_v2_customer_restore_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_can_manage_customer(session, customer_id):
            return True
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET archived_at = NULL, archived_by_user_id = NULL, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (customer_id,),
                )
            conn.commit()
        audit(user_id, "CUSTOMER_RESTORED", customer_id=customer_id)
        await query.edit_message_text(
            "✅ مشتری از آرشیو بازیابی شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if action.startswith("crm_v2_followup_add_"):
        customer_id = int(action.rsplit("_", 1)[1])
        if not _crm_v2_customer_access(session, customer_id):
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_followup_datetime",
            "crm_v2_customer_id": customer_id,
        })
        await query.edit_message_text(
            "⏰ زمان پیگیری را ارسال کنید.\n\n"
            "مثال شمسی:\n1405/07/20 18:30\n\n"
            "یا میلادی:\n2026-10-12 18:30",
            reply_markup=_main_back(),
        )
        return True

    if action == "crm_v2_followup_note_skip":
        customer_id = context.user_data.get("crm_v2_customer_id")
        due_iso = context.user_data.get("crm_v2_followup_due")
        if not customer_id or not due_iso or not _crm_v2_customer_access(session, customer_id):
            context.user_data.clear()
            await query.edit_message_text("⚠️ اطلاعات پیگیری منقضی شده است.", reply_markup=_main_back())
            return True
        due_at = datetime.fromisoformat(due_iso)
        followup_id = _crm_v2_create_followup(
            customer_id,
            user_id,
            user_id,
            due_at,
            None,
        )
        audit(
            user_id,
            "FOLLOWUP_CREATED",
            customer_id=customer_id,
            details={"followup_id": followup_id, "due_at": due_iso},
        )
        context.user_data.clear()
        row = _crm_v2_followup_detail(session, followup_id)
        await query.edit_message_text(
            "✅ پیگیری ثبت شد.\n\n" + _crm_v2_followup_text(row),
            reply_markup=_crm_v2_followup_detail_keyboard(row),
        )
        return True

    if action.startswith("crm_v2_followup_view_"):
        followup_id = int(action.rsplit("_", 1)[1])
        row = _crm_v2_followup_detail(session, followup_id)
        if not row:
            await query.edit_message_text("⛔ این پیگیری در دسترس شما نیست.", reply_markup=_main_back())
            return True
        await query.edit_message_text(
            _crm_v2_followup_text(row),
            reply_markup=_crm_v2_followup_detail_keyboard(row),
        )
        return True

    if action.startswith("crm_v2_followup_done_") or action.startswith("crm_v2_followup_cancel_"):
        followup_id = int(action.rsplit("_", 1)[1])
        row = _crm_v2_followup_detail(session, followup_id)
        if not row:
            return True
        status = "DONE" if action.startswith("crm_v2_followup_done_") else "CANCELLED"
        customer_id = _crm_v2_set_followup_status(followup_id, status)
        audit(
            user_id,
            "FOLLOWUP_DONE" if status == "DONE" else "FOLLOWUP_CANCELLED",
            customer_id=customer_id,
            details={"followup_id": followup_id},
        )
        updated = _crm_v2_followup_detail(session, followup_id)
        await query.edit_message_text(
            "✅ وضعیت پیگیری بروزرسانی شد.\n\n" + _crm_v2_followup_text(updated),
            reply_markup=_crm_v2_followup_detail_keyboard(updated),
        )
        return True

    if action.startswith("crm_v2_visit_contact_"):
        visit_id = int(action.rsplit("_", 1)[1])
        row = _visit_detail(session, visit_id)
        if not row:
            return True
        customer_id = row[7]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE visit_requests
                    SET status = 'CONTACTED', contacted_at = NOW(), updated_at = NOW()
                    WHERE id = %s AND status = 'NEW'
                    """,
                    (visit_id,),
                )
                cur.execute(
                    "UPDATE customer_profiles SET last_interaction_at = NOW(), updated_at = NOW() WHERE id = %s",
                    (customer_id,),
                )
            conn.commit()
        audit(
            user_id,
            "VISIT_CONTACTED",
            customer_id=customer_id,
            visit_id=visit_id,
        )
        updated = _visit_detail(session, visit_id)
        await query.edit_message_text(
            "✅ وضعیت روی «تماس گرفته شد» قرار گرفت.\n\n" + _visit_text(updated),
            reply_markup=_crm_v2_visit_keyboard(session, updated),
            disable_web_page_preview=True,
        )
        return True

    if action.startswith("crm_v2_visit_schedule_"):
        visit_id = int(action.rsplit("_", 1)[1])
        row = _visit_detail(session, visit_id)
        if not row:
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_visit_schedule",
            "crm_v2_visit_id": visit_id,
        })
        await query.edit_message_text(
            "🕐 تاریخ و ساعت بازدید را ارسال کنید.\n\n"
            "مثال: 1405/07/20 18:30",
            reply_markup=_main_back(),
        )
        return True

    if action.startswith("crm_v2_visit_note_"):
        visit_id = int(action.rsplit("_", 1)[1])
        row = _visit_detail(session, visit_id)
        if not row:
            return True
        context.user_data.clear()
        context.user_data.update({
            "state": "crm_v2_visit_note",
            "crm_v2_visit_id": visit_id,
        })
        await query.edit_message_text(
            "📝 یادداشت بازدید را ارسال کنید.",
            reply_markup=_main_back(),
        )
        return True

    return True


# --- V2 text-state wrapper ---
_crm_v2_legacy_handle_crm_text = handle_crm_text


async def handle_crm_text(update, context, session):
    state = context.user_data.get("state")
    if not state or not state.startswith("crm_v2_"):
        return await _crm_v2_legacy_handle_crm_text(update, context, session)

    text = (update.message.text or "").strip()
    user_id = session[0]

    if state == "crm_v2_search":
        rows = _crm_v2_search_customers(session, text)
        context.user_data.clear()
        if not rows:
            await update.message.reply_text(
                "⚠️ مشتری پیدا نشد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ خواهان‌ها", callback_data="crm_lead_added")]
                ]),
            )
            return True
        buttons = [
            [
                InlineKeyboardButton(
                    f"📞 {phone or '-'} | {name}"[:60],
                    callback_data=f"crm_lead_view_{customer_id}",
                )
            ]
            for customer_id, name, phone, owner_name in rows
        ]
        buttons.append([
            InlineKeyboardButton("⬅️ خواهان‌ها", callback_data="crm_lead_added")
        ])
        await update.message.reply_text(
            f"🔎 نتایج جستجو: {len(rows)}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return True

    customer_id = context.user_data.get("crm_v2_customer_id")

    if state == "crm_v2_edit_name":
        if not customer_id or not _crm_v2_can_manage_customer(session, customer_id):
            context.user_data.clear()
            return True
        if len(text) < 2:
            await update.message.reply_text("❌ نام معتبر نیست.")
            return True
        first_name, last_name = _lead_name_parts(text)
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET first_name = %s, last_name = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (first_name, last_name, customer_id),
                )
            conn.commit()
        audit(user_id, "CUSTOMER_EDIT_NAME", customer_id=customer_id)
        context.user_data.clear()
        await update.message.reply_text(
            "✅ نام مشتری بروزرسانی شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if state == "crm_v2_edit_phone":
        if not customer_id or not _crm_v2_can_manage_customer(session, customer_id):
            context.user_data.clear()
            return True
        phone_norm = normalize_phone(text)
        if (
            not phone_norm
            or not phone_norm.startswith("0")
            or len(phone_norm) < 10
            or len(phone_norm) > 12
        ):
            await update.message.reply_text("❌ شماره تماس معتبر نیست.")
            return True
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM customer_profiles
                    WHERE phone_normalized = %s
                      AND id <> %s
                    LIMIT 1
                    """,
                    (phone_norm, customer_id),
                )
                duplicate = cur.fetchone()
                if duplicate:
                    await update.message.reply_text(
                        f"❌ این شماره برای مشتری #{duplicate[0]} ثبت شده است."
                    )
                    return True
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET phone_number = %s,
                        phone_normalized = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (text, phone_norm, customer_id),
                )
            conn.commit()
        audit(user_id, "CUSTOMER_EDIT_PHONE", customer_id=customer_id)
        context.user_data.clear()
        await update.message.reply_text(
            "✅ شماره تماس بروزرسانی شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if state == "crm_v2_edit_price":
        if not customer_id or not _crm_v2_can_manage_customer(session, customer_id):
            context.user_data.clear()
            return True
        if text in ("حذف", "پاک", "پاک کردن", "0", "۰"):
            low = high = None
        else:
            parsed = _lead_parse_price_range(text)
            if not parsed:
                await update.message.reply_text("❌ بازه تشخیص داده نشد. مثال: 15 تا 25")
                return True
            low, high = parsed
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET desired_price_min_billion = %s,
                        desired_price_max_billion = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (low, high, customer_id),
                )
            conn.commit()
        audit(
            user_id,
            "CUSTOMER_EDIT_PRICE",
            customer_id=customer_id,
            details={
                "min_billion": str(low) if low is not None else None,
                "max_billion": str(high) if high is not None else None,
            },
        )
        context.user_data.clear()
        await update.message.reply_text(
            "✅ بازه قیمت بروزرسانی شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if state == "crm_v2_edit_notes":
        if not customer_id or not _crm_v2_can_manage_customer(session, customer_id):
            context.user_data.clear()
            return True
        if text in ("حذف", "پاک", "پاک کردن", "0", "۰"):
            notes_value = None
        else:
            if len(text) > 4000:
                await update.message.reply_text("❌ توضیحات حداکثر ۴۰۰۰ کاراکتر باشد.")
                return True
            notes_value = text
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_profiles
                    SET notes = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (notes_value, customer_id),
                )
            conn.commit()
        audit(
            user_id,
            "CUSTOMER_EDIT_NOTES",
            customer_id=customer_id,
            details={"cleared": notes_value is None},
        )
        context.user_data.clear()
        await update.message.reply_text(
            "✅ توضیحات بروزرسانی شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if state == "crm_v2_add_note":
        if not customer_id or not _crm_v2_customer_access(session, customer_id):
            context.user_data.clear()
            return True
        if not text or len(text) > 1500:
            await update.message.reply_text("❌ یادداشت باید بین ۱ تا ۱۵۰۰ کاراکتر باشد.")
            return True
        actor_name = session[2] or session[1] or "کاربر"
        stamp = datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M")
        note = f"[{stamp} | {actor_name}] {text}"
        _lead_set_notes(customer_id, note)
        audit(
            user_id,
            "CUSTOMER_NOTE_ADDED",
            customer_id=customer_id,
            details={"note_length": len(text)},
        )
        context.user_data.clear()
        await update.message.reply_text(
            "✅ یادداشت اضافه شد.\n\n" + _crm_v2_customer_text(customer_id),
            reply_markup=_lead_customer_keyboard(customer_id),
        )
        return True

    if state == "crm_v2_followup_datetime":
        if not customer_id or not _crm_v2_customer_access(session, customer_id):
            context.user_data.clear()
            return True
        due_at = parse_visit_datetime(text)
        if not due_at:
            await update.message.reply_text("❌ تاریخ/ساعت نامعتبر است. مثال: 1405/07/20 18:30")
            return True
        if due_at <= datetime.now(TEHRAN):
            await update.message.reply_text("❌ زمان پیگیری باید در آینده باشد.")
            return True
        context.user_data["crm_v2_followup_due"] = due_at.isoformat()
        context.user_data["state"] = "crm_v2_followup_note"
        await update.message.reply_text(
            "📝 توضیح پیگیری را ارسال کنید.\n"
            "مثلاً: برای نتیجه بازدید تماس بگیر.\n\n"
            "اگر توضیحی ندارید «رد کردن» را بزنید.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ رد کردن", callback_data="crm_v2_followup_note_skip")]
            ]),
        )
        return True

    if state == "crm_v2_followup_note":
        due_iso = context.user_data.get("crm_v2_followup_due")
        if not customer_id or not due_iso or not _crm_v2_customer_access(session, customer_id):
            context.user_data.clear()
            return True
        if len(text) > 1000:
            await update.message.reply_text("❌ توضیح پیگیری حداکثر ۱۰۰۰ کاراکتر باشد.")
            return True
        due_at = datetime.fromisoformat(due_iso)
        followup_id = _crm_v2_create_followup(
            customer_id,
            user_id,
            user_id,
            due_at,
            text or None,
        )
        audit(
            user_id,
            "FOLLOWUP_CREATED",
            customer_id=customer_id,
            details={
                "followup_id": followup_id,
                "due_at": due_iso,
            },
        )
        context.user_data.clear()
        row = _crm_v2_followup_detail(session, followup_id)
        await update.message.reply_text(
            "✅ پیگیری ثبت شد.\n\n" + _crm_v2_followup_text(row),
            reply_markup=_crm_v2_followup_detail_keyboard(row),
        )
        return True

    if state == "crm_v2_visit_schedule":
        visit_id = context.user_data.get("crm_v2_visit_id")
        row = _visit_detail(session, visit_id) if visit_id else None
        if not row:
            context.user_data.clear()
            return True
        visit_at = parse_visit_datetime(text)
        if not visit_at:
            await update.message.reply_text("❌ تاریخ/ساعت نامعتبر است. مثال: 1405/07/20 18:30")
            return True
        if visit_at <= datetime.now(TEHRAN):
            await update.message.reply_text("❌ زمان بازدید باید در آینده باشد.")
            return True
        customer_id = row[7]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE visit_requests
                    SET status = 'SCHEDULED',
                        visit_at = %s,
                        scheduled_at = NOW(),
                        assigned_user_id = COALESCE(assigned_user_id, %s),
                        reminder_sent_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (visit_at, user_id, visit_id),
                )
                cur.execute(
                    "UPDATE customer_profiles SET last_interaction_at = NOW(), updated_at = NOW() WHERE id = %s",
                    (customer_id,),
                )
            conn.commit()
        audit(
            user_id,
            "VISIT_SCHEDULED",
            customer_id=customer_id,
            visit_id=visit_id,
            details={"visit_at": visit_at.isoformat()},
        )
        context.user_data.clear()
        updated = _visit_detail(session, visit_id)
        await update.message.reply_text(
            "✅ زمان بازدید ثبت شد.\nیک ساعت قبل یادآوری ارسال می‌شود.\n\n"
            + _visit_text(updated),
            reply_markup=_crm_v2_visit_keyboard(session, updated),
            disable_web_page_preview=True,
        )
        return True

    if state == "crm_v2_visit_note":
        visit_id = context.user_data.get("crm_v2_visit_id")
        row = _visit_detail(session, visit_id) if visit_id else None
        if not row:
            context.user_data.clear()
            return True
        if not text or len(text) > 1000:
            await update.message.reply_text("❌ یادداشت باید بین ۱ تا ۱۰۰۰ کاراکتر باشد.")
            return True
        customer_id = row[7]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE visit_requests
                    SET notes = CASE
                            WHEN notes IS NULL OR BTRIM(notes) = '' THEN %s
                            ELSE notes || E'\\n' || %s
                        END,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (text, text, visit_id),
                )
            conn.commit()
        audit(
            user_id,
            "VISIT_NOTE_ADDED",
            customer_id=customer_id,
            visit_id=visit_id,
            details={"note_length": len(text)},
        )
        context.user_data.clear()
        updated = _visit_detail(session, visit_id)
        await update.message.reply_text(
            "✅ یادداشت بازدید ثبت شد.\n\n" + _visit_text(updated),
            reply_markup=_crm_v2_visit_keyboard(session, updated),
            disable_web_page_preview=True,
        )
        return True

    return True


# === END CRM SUITE V2 ===


# =========================================================
# === MENU SETTINGS HUB V3 ===
# Move operational/admin utilities under Settings while
# keeping permission filtering and Settings as the last
# main-menu button.
# =========================================================

_MENU_SETTINGS_MOVED_CALLBACKS = {
    "add_channel",
    "sources",
    "files_menu",
    "stats",
    "hashtags",
    "schedule",
    "reset",
    "crm_v2_report",
    "settings",
}


def _menu_settings_hub_access(session):
    if not session:
        return False
    if is_owner(session):
        return True
    return any(
        has_permission(session, key)
        for key in (
            "settings_publish",
            "channels_manage",
            "files_manage",
            "stats_view",
            "schedule_manage",
            "customers_manage",
            "removed_files_view",
            "duplicate_files_view",
            "instagram_story",
        )
    )


_menu_settings_v3_legacy_check_action_access = check_action_access


def check_action_access(session, action):
    action = action or ""
    if action == "settings":
        return _menu_settings_hub_access(session)
    return _menu_settings_v3_legacy_check_action_access(session, action)


_menu_settings_v3_legacy_build_main_menu = build_main_menu


def _menu_settings_v31_pack_two_columns(buttons):
    rows = []
    for index in range(0, len(buttons), 2):
        rows.append(buttons[index:index + 2])
    return rows


def build_main_menu(session, legacy_keyboard):
    markup = _menu_settings_v3_legacy_build_main_menu(
        session,
        legacy_keyboard,
    )

    buttons = []
    for row in (markup.inline_keyboard if markup else []):
        for button in row:
            if (button.callback_data or "") in _MENU_SETTINGS_MOVED_CALLBACKS:
                continue
            buttons.append(button)

    # Keep the main menu compact: all operational buttons are packed
    # two per row, preserving their existing order.
    rows = _menu_settings_v31_pack_two_columns(buttons)

    # Channel-file editor is permission-based and intentionally gets
    # its own row so the frozen two-column menu ordering is untouched.
    if has_permission(session, "channel_post_edit"):
        rows.append([
            InlineKeyboardButton(
                "✏️ ویرایش فایل کانال",
                callback_data="channel_edit",
            )
        ])

    # Settings always stays at the very end on its own row.
    if _menu_settings_hub_access(session):
        rows.append([
            InlineKeyboardButton(
                "⚙️ تنظیمات",
                callback_data="settings",
            )
        ])

    return InlineKeyboardMarkup(rows)


_menu_settings_v3_legacy_build_settings_menu = build_settings_menu


def build_settings_menu(session, legacy_keyboard):
    base = _menu_settings_v3_legacy_build_settings_menu(
        session,
        legacy_keyboard,
    )

    filtered_rows = _filter_keyboard(session, base)

    back_rows = []
    content_rows = []
    for row in filtered_rows:
        if any((button.callback_data or "") == "home" for button in row):
            back_rows.append(list(row))
        else:
            content_rows.append(list(row))

    hub_rows = []

    if has_permission(session, "channels_manage"):
        hub_rows.append([
            InlineKeyboardButton(
                "➕ افزودن کانال",
                callback_data="add_channel",
            ),
            InlineKeyboardButton(
                "📡 مدیریت کانال‌های منبع",
                callback_data="sources",
            ),
        ])

    first_row = []
    if has_permission(session, "files_manage"):
        first_row.append(
            InlineKeyboardButton(
                "📁 فایل‌ها",
                callback_data="files_menu",
            )
        )
    if has_permission(session, "schedule_manage"):
        first_row.append(
            InlineKeyboardButton(
                "⏰ زمان‌بندی",
                callback_data="schedule",
            )
        )
    if first_row:
        hub_rows.append(first_row)

    if has_permission(session, "stats_view"):
        hub_rows.append([
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="stats",
            ),
            InlineKeyboardButton(
                "#️⃣ هشتگ‌های استفاده‌شده",
                callback_data="hashtags",
            ),
        ])

    if has_permission(session, "customers_manage"):
        hub_rows.append([
            InlineKeyboardButton(
                "📊 گزارش CRM",
                callback_data="crm_v2_report",
            )
        ])

    cleanup_row = []
    if has_permission(session, "removed_files_view"):
        cleanup_row.append(
            InlineKeyboardButton(
                "🧹 فایل‌های حذف‌شده",
                callback_data="removed_files",
            )
        )
    if has_permission(session, "duplicate_files_view"):
        cleanup_row.append(
            InlineKeyboardButton(
                "🔁 فایل‌های تکراری",
                callback_data="duplicate_files",
            )
        )
    if cleanup_row:
        hub_rows.append(cleanup_row)

    if has_permission(session, "instagram_story"):
        hub_rows.append([
            InlineKeyboardButton(
                "📸 استوری اینستاگرام",
                callback_data="insta_story",
            )
        ])

    if is_owner(session):
        hub_rows.append([
            InlineKeyboardButton(
                "💾 بکاپ دیتابیس",
                callback_data="db_backup",
            ),
            InlineKeyboardButton(
                "♻️ ریست",
                callback_data="reset",
            ),
        ])

    return InlineKeyboardMarkup(
        hub_rows + content_rows + back_rows
    )


_menu_settings_v3_legacy_handle_crm_button = handle_crm_button


async def handle_crm_button(update, context, session):
    query = update.callback_query
    action = (query.data if query else "") or ""

    if action == "crm_v2_report":
        if not has_permission(session, "customers_manage"):
            await query.edit_message_text(
                "⛔ دسترسی ندارید.",
                reply_markup=_main_back(),
            )
            return True

        await query.edit_message_text(
            _crm_v2_report_text(session)[:4000],
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ بازگشت به تنظیمات",
                        callback_data="settings",
                    )
                ]
            ]),
        )
        return True

    return await _menu_settings_v3_legacy_handle_crm_button(
        update,
        context,
        session,
    )


# === END MENU SETTINGS HUB V3 ===


# =========================================================
# === MENU SETTINGS HUB V3.1 ===
# Source-channel tools moved into Settings and main menu
# packed into two-column rows.
# =========================================================


# =========================================================
# CRM CAMPAIGN HUB V4
# Link audiences, safe broadcasts, and unanswered questions.
# =========================================================

_crm_campaign_v4_legacy_build_main_menu = build_main_menu
_crm_campaign_v4_legacy_customers_keyboard = _customers_keyboard
_crm_campaign_v4_legacy_handle_button = handle_crm_button
_crm_campaign_v4_legacy_handle_text = handle_crm_text


def build_main_menu(session, legacy_keyboard):
    markup = _crm_campaign_v4_legacy_build_main_menu(session, legacy_keyboard)
    rows = [list(row) for row in (markup.inline_keyboard if markup else [])]
    callbacks = {
        button.callback_data
        for row in rows
        for button in row
        if button.callback_data
    }
    if (
        has_permission(session, "customers_manage")
        and "crm_campaigns" not in callbacks
    ):
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, [
            InlineKeyboardButton(
                "👥 باشگاه مشتریان CRM",
                callback_data="crm_campaigns",
            )
        ])
    return InlineKeyboardMarkup(rows)


def _customers_keyboard(session, offset=0, limit=20):
    markup, total = _crm_campaign_v4_legacy_customers_keyboard(
        session, offset, limit
    )
    rows = [list(row) for row in (markup.inline_keyboard if markup else [])]
    insert_at = max(0, len(rows) - 1)
    rows.insert(insert_at, [
        InlineKeyboardButton("🎯 دسته‌های لینک", callback_data="crm_campaigns"),
        InlineKeyboardButton("💬 سؤال‌های مشتریان", callback_data="crm_support_inbox"),
    ])
    return InlineKeyboardMarkup(rows), total


async def handle_crm_button(update, context, session):
    action = ((update.callback_query.data if update.callback_query else "") or "")
    if action.startswith("crm_campaign") or action.startswith("crm_support"):
        if not has_permission(session, "customers_manage"):
            await update.callback_query.edit_message_text(
                "⛔ دسترسی ندارید.", reply_markup=_main_back()
            )
            return True
        from crm_campaign_hub import handle_button as handle_campaign_hub_button
        if await handle_campaign_hub_button(update, context, session):
            return True
    return await _crm_campaign_v4_legacy_handle_button(update, context, session)


async def handle_crm_text(update, context, session):
    state = context.user_data.get("state") or ""
    if state in {"crm_campaign_broadcast_text", "crm_support_reply_text"}:
        if not has_permission(session, "customers_manage"):
            context.user_data.clear()
            await update.message.reply_text("⛔ دسترسی ندارید.", reply_markup=_main_back())
            return True
        from crm_campaign_hub import handle_text as handle_campaign_hub_text
        if await handle_campaign_hub_text(update, context, session):
            return True
    return await _crm_campaign_v4_legacy_handle_text(update, context, session)


# === END CRM CAMPAIGN HUB V4 ===
