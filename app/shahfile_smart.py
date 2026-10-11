from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import psycopg
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from crm_panel import parse_visit_datetime, TEHRAN
from publisher import publish_property
from shahfile_ai import (
    ensure_ai_schema,
    extract_ai_intent,
    subscribe_and_render,
    search_page as ai_search_page,
    cancel_active_subscription,
    activate_search_subscription,
    decline_search_subscription,
    get_active_search_context,
    OPT_OUT_TEXT,
)


# =========================================================
# DB / CONSTANTS
# =========================================================

PAGE_SIZE = 6
MAX_SELLER_PHOTOS = 10

SELLER_STATES = {
    "SELL_DISTRICT",
    "SELL_PRIVATE_ADDRESS",
    "SELL_AREA",
    "SELL_BEDROOMS",
    "SELL_FLOOR",
    "SELL_TOTAL_FLOORS",
    "SELL_BUILD_YEAR",
    "SELL_PARKING",
    "SELL_ELEVATOR",
    "SELL_STORAGE",
    "SELL_PRICE",
    "SELL_DESCRIPTION",
    "SELL_OWNER_NAME",
    "SELL_OWNER_PHONE",
    "SELL_PHOTOS",
    "SELL_CORRECTION_FIELD",
}


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def ensure_smart_schema():
    """Idempotent boot-time schema guard for ShahFile Smart Customer Flow."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_customer_conversations (
                    telegram_user_id BIGINT PRIMARY KEY,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    state VARCHAR(80) NOT NULL DEFAULT 'IDLE',
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_visit_approvals (
                    id BIGSERIAL PRIMARY KEY,
                    visit_request_id BIGINT NOT NULL UNIQUE
                        REFERENCES visit_requests(id) ON DELETE CASCADE,
                    customer_telegram_user_id BIGINT NOT NULL,
                    requested_visit_at TIMESTAMPTZ NOT NULL,
                    proposed_visit_at TIMESTAMPTZ,
                    manager_status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
                    manager_note TEXT,
                    decided_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    decided_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT smart_visit_approvals_status_check CHECK (
                        manager_status IN ('PENDING','APPROVED','PROPOSED','REJECTED')
                    )
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_submissions (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    telegram_user_id BIGINT NOT NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'DRAFT',
                    district VARCHAR(255),
                    public_location TEXT,
                    private_address TEXT,
                    area_m2 NUMERIC(10,2),
                    bedrooms INTEGER,
                    floor_text VARCHAR(100),
                    total_floors INTEGER,
                    build_year INTEGER,
                    has_parking BOOLEAN,
                    has_elevator BOOLEAN,
                    has_storage BOOLEAN,
                    price_toman BIGINT,
                    description TEXT,
                    owner_name VARCHAR(255),
                    owner_phone VARCHAR(64),
                    manager_note TEXT,
                    approved_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    property_id BIGINT REFERENCES properties(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    submitted_at TIMESTAMPTZ,
                    decided_at TIMESTAMPTZ,
                    CONSTRAINT seller_submissions_status_check CHECK (
                        status IN ('DRAFT','PENDING','NEEDS_CHANGES','APPROVED','REJECTED','PUBLISHED')
                    )
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_submission_media (
                    id BIGSERIAL PRIMARY KEY,
                    submission_id BIGINT NOT NULL
                        REFERENCES seller_submissions(id) ON DELETE CASCADE,
                    telegram_file_id TEXT NOT NULL,
                    telegram_file_unique_id TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_purchase_profiles (
                    customer_id BIGINT PRIMARY KEY
                        REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    funding_status VARCHAR(20) NOT NULL,
                    asked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT customer_purchase_profiles_status_check CHECK (
                        funding_status IN ('CASH','SELL_PROPERTY')
                    )
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_smart_visit_status ON smart_visit_approvals(manager_status, created_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_seller_submissions_status ON seller_submissions(status, created_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_seller_submissions_tg ON seller_submissions(telegram_user_id, created_at DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_seller_media_submission ON seller_submission_media(submission_id, position)"
            )
        conn.commit()
    ensure_ai_schema()


# =========================================================
# NORMALIZATION / PARSERS
# =========================================================

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(value: Any) -> str:
    return str(value or "").translate(_DIGIT_MAP)


# =========================================================
# GLOBAL CUSTOMER FLOW CONTROL V1.1.1
# A clear new search must be able to supersede a stale seller form.
# Natural Persian abandon phrases stop the current in-progress form.
# =========================================================

_FLOW_CANCEL_PHRASES = {
    "بیخیال",
    "بی خیال",
    "بیخیالش",
    "ولش کن",
    "اونو تموم کن",
    "اون رو تموم کن",
    "نه اونو تموم کن",
    "نه اون رو تموم کن",
    "فرایند رو لغو کن",
    "فرآیند رو لغو کن",
    "فرایند را لغو کن",
    "فرآیند را لغو کن",
    "این فرایند رو لغو کن",
    "این فرآیند رو لغو کن",
    "فرم رو لغو کن",
    "فرم را لغو کن",
    "از این مرحله بیا بیرون",
    "از این مرحله بیام بیرون",
}


def _normalized_phrase(value: Any) -> str:
    text = normalize_digits(value).strip().casefold()
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[؟?!،,.؛;:\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_flow_cancel_request(text: str) -> bool:
    normalized = _normalized_phrase(text)
    if normalized in _FLOW_CANCEL_PHRASES:
        return True
    # Keep bare «لغو» reserved for search-watch opt-out as requested.
    has_flow_word = any(k in normalized for k in ("فرایند", "فرآیند", "فرم", "مرحله"))
    has_cancel_word = any(k in normalized for k in ("لغو", "تموم", "تمام", "بیخیال", "ولش"))
    return has_flow_word and has_cancel_word


def _looks_like_explicit_property_search(text: str) -> bool:
    normalized = _normalized_phrase(text)
    phrases = (
        "چی دارین", "چی دارید", "چی موجود", "چه فایل", "فایل بده",
        "فایل بدین", "فایل دارید", "فایل دارین", "خونه میخوام",
        "خونه می خوام", "خانه میخوام", "خانه می خوام", "آپارتمان میخوام",
        "آپارتمان می خوام", "واحد میخوام", "واحد می خوام", "دنبال خونه",
        "دنبال خانه", "دنبال آپارتمان", "دنبال واحد",
    )
    if any(p in normalized for p in phrases):
        return True
    # Natural range question such as «70 تا 90 متری؟»
    if re.search(r"\b\d{2,4}\s*(?:تا|الی|-)\s*\d{2,4}\s*(?:متر|متری)\b", normalized):
        return any(q in normalized for q in ("دار", "میخوام", "می خوام", "چی", "فایل"))
    return False


def _parse_area_range_intent(text: str):
    normalized = _normalized_phrase(text)
    match = re.search(
        r"\b(\d{2,4}(?:\.\d+)?)\s*(?:تا|الی|-)\s*(\d{2,4}(?:\.\d+)?)\s*(?:متر|متری)\b",
        normalized,
    )
    if not match:
        return None
    try:
        a = float(match.group(1))
        b = float(match.group(2))
    except Exception:
        return None
    low, high = sorted((a, b))
    if low < 10 or high > 10000:
        return None
    return low, high


def _discard_draft_submission_for_conversation(state: str, payload: dict, telegram_user_id: int):
    if state not in SELLER_STATES:
        return
    sid = int((payload or {}).get("submission_id") or 0)
    if not sid:
        return
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM seller_submissions "
                "WHERE id=%s AND telegram_user_id=%s AND status='DRAFT'",
                (sid, telegram_user_id),
            )
        conn.commit()


def _abandon_local_flow(telegram_user_id: int, state: str, payload: dict, customer_id=None):
    # Only unfinished DRAFT seller forms are removed. Submitted/reviewed files remain safe.
    _discard_draft_submission_for_conversation(state, payload or {}, telegram_user_id)
    clear_conversation(telegram_user_id, customer_id=customer_id)


def _clean_number_text(value: Any) -> str:
    return (
        normalize_digits(value)
        .replace("٬", "")
        .replace(",", "")
        .replace("٫", ".")
        .replace("/", ".")
        .strip()
    )


def parse_budget_intent(text: str):
    raw = normalize_digits(text).lower().strip()
    # Keep fallback conservative: multi-filter requests belong to the AI layer.
    # This avoids confusing «70 تا 90 متر، 5 تا 6 میلیارد» with a 70–90B budget.
    if any(token in raw for token in ("متر", "خواب", "پارکینگ", "آسانسور", "انباری", "سال ساخت")):
        return None
    if not any(token in raw for token in ("میلیارد", "بودجه", "قیمت", "دارین", "دارید", "فایل")):
        return None

    nums = re.findall(r"\d+(?:[./]\d+)?", raw)
    nums = [n.replace("/", ".") for n in nums]

    try:
        values = [Decimal(n) for n in nums]
    except InvalidOperation:
        return None

    if ("زیر" in raw or "کمتر" in raw or "تا" in raw) and len(values) == 1:
        return Decimal("0"), values[0]

    if ("بالای" in raw or "بیشتر" in raw or "از" in raw and "به بالا" in raw) and len(values) == 1:
        return values[0], Decimal("999999")

    if len(values) >= 2 and any(token in raw for token in ("تا", "الی", "بین", "تا حدود")):
        return min(values[0], values[1]), max(values[0], values[1])

    return None


def parse_visit_unit_intent(text: str):
    raw = normalize_digits(text).lower()
    if "بازدید" not in raw:
        return None
    match = re.search(r"(?:کد|واحد)\s*[:#\-]?\s*(\d{2,12})", raw)
    if not match:
        return None
    return match.group(1)


def parse_phone(value: str, *, strict_mobile: bool = True):
    raw = normalize_digits(value)
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0098"):
        digits = "0" + digits[4:]
    elif digits.startswith("98") and len(digits) >= 12:
        digits = "0" + digits[2:]
    elif strict_mobile and digits.startswith("9") and len(digits) == 10:
        digits = "0" + digits

    if strict_mobile:
        if not re.fullmatch(r"09\d{9}", digits):
            return None
    else:
        if not (8 <= len(digits) <= 15):
            return None
    return digits


def parse_decimal(value: str, *, minimum=0, maximum=None):
    raw = _clean_number_text(value)
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        number = Decimal(match.group(0))
    except InvalidOperation:
        return None
    if number < Decimal(str(minimum)):
        return None
    if maximum is not None and number > Decimal(str(maximum)):
        return None
    return number


def parse_int(value: str, *, minimum=0, maximum=None):
    number = parse_decimal(value, minimum=minimum, maximum=maximum)
    if number is None or number != int(number):
        return None
    return int(number)


def parse_price_toman(value: str):
    raw = _clean_number_text(value).lower()
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        number = Decimal(match.group(0))
    except InvalidOperation:
        return None

    if "میلیارد" in raw:
        toman = int(number * Decimal("1000000000"))
    elif "میلیون" in raw:
        toman = int(number * Decimal("1000000"))
    elif number < 1000:
        # Bare small numbers in this flow are interpreted as billions.
        toman = int(number * Decimal("1000000000"))
    else:
        toman = int(number)

    if toman <= 0:
        return None
    return toman


def format_billion(value):
    value = Decimal(str(value))
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text


def _gregorian_to_jalali(gy, gm, gd):
    # Pure-Python Gregorian -> Jalali conversion; keeps this flow dependency-free.
    g_day_no = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_day_no[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def parse_shamsi_visit_datetime(value):
    text = normalize_digits(value).strip()
    match = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    year = int(match.group(1))
    # Smart Customer Flow intentionally accepts Jalali dates only.
    if not 1300 <= year <= 1599:
        return None
    return parse_visit_datetime(text)


def format_local_dt(value):
    if not value:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=TEHRAN)
    local = value.astimezone(TEHRAN)
    jy, jm, jd = _gregorian_to_jalali(local.year, local.month, local.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d} {local:%H:%M}"


# =========================================================
# CUSTOMER PROFILE / CONVERSATION
# =========================================================


def upsert_customer_from_telegram(user):
    if not user:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_profiles (
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                    source_label,
                    created_at,
                    updated_at,
                    last_interaction_at
                )
                VALUES (%s,%s,%s,%s,'SMART_CUSTOMER',NOW(),NOW(),NOW())
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET
                    telegram_username = EXCLUDED.telegram_username,
                    first_name = COALESCE(NULLIF(EXCLUDED.first_name,''), customer_profiles.first_name),
                    last_name = COALESCE(NULLIF(EXCLUDED.last_name,''), customer_profiles.last_name),
                    updated_at = NOW(),
                    last_interaction_at = NOW()
                RETURNING id, phone_number
                """,
                (
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def get_customer(telegram_user_id: int):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, telegram_user_id, first_name, last_name,
                       phone_number, telegram_username
                FROM customer_profiles
                WHERE telegram_user_id = %s
                LIMIT 1
                """,
                (telegram_user_id,),
            )
            return cur.fetchone()


def save_customer_phone(telegram_user_id: int, phone: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_profiles
                SET phone_number=%s,
                    phone_shared_at=NOW(),
                    updated_at=NOW(),
                    last_interaction_at=NOW()
                WHERE telegram_user_id=%s
                RETURNING id
                """,
                (phone, telegram_user_id),
            )
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def get_conversation(telegram_user_id: int):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT state, payload, customer_id
                FROM smart_customer_conversations
                WHERE telegram_user_id=%s
                """,
                (telegram_user_id,),
            )
            row = cur.fetchone()
    if not row:
        return "IDLE", {}, None
    return row[0] or "IDLE", dict(row[1] or {}), row[2]


def set_conversation(telegram_user_id: int, state: str, payload=None, customer_id=None):
    payload = dict(payload or {})
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smart_customer_conversations(
                    telegram_user_id, customer_id, state, payload, created_at, updated_at
                )
                VALUES (%s,%s,%s,%s::jsonb,NOW(),NOW())
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET
                    customer_id=COALESCE(EXCLUDED.customer_id, smart_customer_conversations.customer_id),
                    state=EXCLUDED.state,
                    payload=EXCLUDED.payload,
                    updated_at=NOW()
                """,
                (telegram_user_id, customer_id, state, json.dumps(payload, ensure_ascii=False)),
            )
        conn.commit()


def clear_conversation(telegram_user_id: int, customer_id=None):
    set_conversation(telegram_user_id, "IDLE", {}, customer_id=customer_id)


# =========================================================
# BUDGET SEARCH
# =========================================================


def _budget_count(low: Decimal, high: Decimal):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM properties p
                WHERE p.status='ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.current_price_billion IS NOT NULL
                  AND p.current_price_billion >= %s
                  AND p.current_price_billion <= %s
                  AND NOT EXISTS (
                      SELECT 1 FROM property_trash pt WHERE pt.property_id=p.id
                  )
                """,
                (low, high),
            )
            return int(cur.fetchone()[0] or 0)


def _budget_rows(low: Decimal, high: Decimal, offset=0, limit=PAGE_SIZE):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id,p.unit_code,p.district,p.address_text,p.area_m2,
                       p.bedrooms,p.build_year,p.current_price_billion,
                       p.channel_post_url,p.published_message_id
                FROM properties p
                WHERE p.status='ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.current_price_billion IS NOT NULL
                  AND p.current_price_billion >= %s
                  AND p.current_price_billion <= %s
                  AND NOT EXISTS (
                      SELECT 1 FROM property_trash pt WHERE pt.property_id=p.id
                  )
                ORDER BY p.current_price_billion, p.id DESC
                LIMIT %s OFFSET %s
                """,
                (low, high, limit, offset),
            )
            return cur.fetchall()


def _destination_username():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT username
                FROM channels
                WHERE channel_type='DESTINATION' AND is_active=TRUE
                ORDER BY id DESC LIMIT 1
                """
            )
            row = cur.fetchone()
    return str(row[0]).lstrip("@") if row and row[0] else None


def _budget_text(low: Decimal, high: Decimal, rows, offset, total):
    low_text = format_billion(low)
    high_text = format_billion(high)
    if low == 0:
        title = f"🏷 فایل‌های تا {high_text} میلیارد تومان"
    elif high >= Decimal("999999"):
        title = f"🏷 فایل‌های از {low_text} میلیارد تومان به بالا"
    else:
        title = f"🏷 فایل‌های بین {low_text} تا {high_text} میلیارد تومان"

    lines = [title, f"تعداد فایل: {total}", ""]
    dest_user = _destination_username()

    for index, row in enumerate(rows, start=offset + 1):
        pid, unit, district, address, area, beds, year, price, url, msg_id = row
        location = address or district or "بدون منطقه"
        lines.append(f"{index}) 📍 {location}")
        details = []
        if area is not None:
            area_val = float(area)
            details.append(f"📐 {int(area_val) if area_val.is_integer() else area_val:g} متر")
        if beds is not None:
            details.append(f"🛏 {beds} خواب")
        if year:
            details.append(f"📅 سال ساخت {year}")
        if details:
            lines.append(" | ".join(details))
        if price is not None:
            lines.append(f"💰 قیمت: {format_billion(price)} میلیارد تومان")
        if unit:
            lines.append(f"🔢 کد واحد: {unit}")
        final_url = url
        if dest_user and msg_id:
            final_url = f"https://t.me/{dest_user}/{msg_id}"
        if final_url:
            lines.append(f"🔗 مشاهده فایل: {final_url}")
        lines.append(OPT_OUT_TEXT)
        lines.extend(["", "──────────────", ""])

    if rows:
        lines.extend([
            "برای صفحه‌های بعدی از دکمه‌های زیر استفاده کنید 👇",
            "برای بازدید هم کافی است بنویسید: «برای کد ۲۷۲۹ وقت بازدید می‌خوام»",
        ])
    return "\n".join(lines).strip()


def _budget_keyboard(low, high, offset, total):
    rows = []
    nav = []
    low_s = format_billion(low)
    high_s = format_billion(high)
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"sc_b_{low_s}_{high_s}_{max(0, offset-PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"sc_b_{low_s}_{high_s}_{offset+PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 شروع دوباره", callback_data="sc_home")])
    return InlineKeyboardMarkup(rows)


# =========================================================
# VISIT REQUEST + MANAGER APPROVAL
# =========================================================


def find_visitable_by_unit(unit_code: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,unit_code,address_text,district,area_m2,bedrooms,
                       build_year,current_price_billion,channel_post_url
                FROM properties
                WHERE unit_code=%s
                  AND status='ACTIVE'
                  AND published_message_id IS NOT NULL
                  AND deleted_from_channel_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM property_trash pt WHERE pt.property_id=properties.id
                  )
                LIMIT 1
                """,
                (unit_code,),
            )
            return cur.fetchone()


def _owner_user_id():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE role='OWNER' AND is_active=TRUE ORDER BY id LIMIT 1")
            row = cur.fetchone()
    return row[0] if row else None


def _owner_telegram_ids():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (u.id) us.telegram_user_id
                FROM users u
                JOIN user_sessions us ON us.user_id=u.id
                WHERE u.role='OWNER' AND u.is_active=TRUE
                  AND us.telegram_user_id IS NOT NULL
                ORDER BY u.id, us.last_activity_at DESC NULLS LAST, us.id DESC
                """
            )
            return [r[0] for r in cur.fetchall() if r[0]]


def _create_visit_approval(customer_id, telegram_user_id, property_row, visit_at):
    property_id, unit_code, *_rest, post_url = property_row
    owner_id = _owner_user_id()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cs.created_by_user_id
                FROM customer_showcase_visitors csv
                JOIN customer_showcases cs ON cs.id=csv.showcase_id
                WHERE csv.telegram_user_id=%s
                  AND cs.created_by_user_id IS NOT NULL
                ORDER BY csv.last_opened_at DESC
                LIMIT 1
                """,
                (telegram_user_id,),
            )
            referrer = cur.fetchone()
            if referrer and referrer[0]:
                owner_id = referrer[0]
            cur.execute(
                """
                SELECT id FROM visit_requests
                WHERE customer_id=%s AND property_id=%s
                  AND status IN ('NEW','CONTACTED','SCHEDULED')
                ORDER BY id DESC LIMIT 1
                """,
                (customer_id, property_id),
            )
            found = cur.fetchone()
            if found:
                visit_id = found[0]
                cur.execute(
                    """
                    UPDATE visit_requests
                    SET status='NEW', source='SMART_CUSTOMER', visit_at=NULL,
                        assigned_user_id=%s, reminder_sent_at=NULL,
                        updated_at=NOW()
                    WHERE id=%s
                    """,
                    (owner_id, visit_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO visit_requests(
                        customer_id,property_id,unit_code_snapshot,channel_post_url_snapshot,
                        status,source,requested_at,assigned_user_id,created_at,updated_at
                    ) VALUES (%s,%s,%s,%s,'NEW','SMART_CUSTOMER',NOW(),%s,NOW(),NOW())
                    RETURNING id
                    """,
                    (customer_id, property_id, unit_code, post_url, owner_id),
                )
                visit_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO smart_visit_approvals(
                    visit_request_id,customer_telegram_user_id,requested_visit_at,
                    manager_status,created_at,updated_at
                ) VALUES (%s,%s,%s,'PENDING',NOW(),NOW())
                ON CONFLICT (visit_request_id)
                DO UPDATE SET
                    customer_telegram_user_id=EXCLUDED.customer_telegram_user_id,
                    requested_visit_at=EXCLUDED.requested_visit_at,
                    proposed_visit_at=NULL,
                    manager_status='PENDING',
                    manager_note=NULL,
                    decided_by_user_id=NULL,
                    decided_at=NULL,
                    updated_at=NOW()
                RETURNING id
                """,
                (visit_id, telegram_user_id, visit_at),
            )
            approval_id = cur.fetchone()[0]
        conn.commit()
    return approval_id, visit_id


def _visit_approval_detail(approval_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sva.id,sva.visit_request_id,sva.customer_telegram_user_id,
                       sva.requested_visit_at,sva.proposed_visit_at,sva.manager_status,
                       vr.customer_id,vr.property_id,vr.unit_code_snapshot,
                       vr.channel_post_url_snapshot,
                       cp.first_name,cp.last_name,cp.phone_number,
                       p.address_text,p.district,p.area_m2,p.bedrooms,p.current_price_billion
                FROM smart_visit_approvals sva
                JOIN visit_requests vr ON vr.id=sva.visit_request_id
                JOIN customer_profiles cp ON cp.id=vr.customer_id
                LEFT JOIN properties p ON p.id=vr.property_id
                WHERE sva.id=%s
                """,
                (approval_id,),
            )
            return cur.fetchone()


def _visit_manager_text(detail):
    (
        aid, visit_id, tg_id, requested, proposed, status, customer_id, property_id,
        unit, url, first, last, phone, address, district, area, beds, price
    ) = detail
    name = " ".join(x for x in (first, last) if x).strip() or "بدون نام"
    location = address or district or "-"
    lines = [
        "🔔 درخواست هوشمند بازدید ShahFile",
        "",
        f"🆔 درخواست: {visit_id}",
        f"🔢 کد واحد: {unit or '-'}",
        f"📍 {location}",
    ]
    if area is not None:
        lines.append(f"📐 {float(area):g} متر")
    if beds is not None:
        lines.append(f"🛏 {beds} خواب")
    if price is not None:
        lines.append(f"💰 {format_billion(price)} میلیارد تومان")
    if url:
        lines.append(f"🔗 {url}")
    lines.extend([
        "",
        "👤 مشتری",
        f"نام: {name}",
        f"📞 {phone or '-'}",
        f"🕐 زمان پیشنهادی مشتری: {format_local_dt(requested)}",
    ])
    return "\n".join(lines)


def _visit_manager_keyboard(approval_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید همین زمان", callback_data=f"sc_mgr_va_{approval_id}")],
        [InlineKeyboardButton("🕐 پیشنهاد زمان جدید", callback_data=f"sc_mgr_vp_{approval_id}")],
        [InlineKeyboardButton("❌ رد درخواست", callback_data=f"sc_mgr_vr_{approval_id}")],
    ])


async def notify_owner_visit(bot, approval_id):
    detail = _visit_approval_detail(approval_id)
    if not detail:
        return 0
    sent = 0
    recipient_ids = set(_owner_telegram_ids())
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT us.telegram_user_id
                FROM smart_visit_approvals sva
                JOIN visit_requests vr ON vr.id=sva.visit_request_id
                JOIN user_sessions us ON us.user_id=vr.assigned_user_id
                WHERE sva.id=%s AND us.is_active=TRUE
                  AND us.telegram_user_id IS NOT NULL
                """,
                (approval_id,),
            )
            recipient_ids.update(row[0] for row in cur.fetchall() if row[0])
    for telegram_id in recipient_ids:
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=_visit_manager_text(detail),
                reply_markup=_visit_manager_keyboard(approval_id),
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:
            logging.exception("smart visit manager notification failed approval_id=%s", approval_id)
    return sent


def _schedule_approved_visit(approval_id, when, actor_user_id):
    detail = _visit_approval_detail(approval_id)
    if not detail:
        return None
    visit_id = detail[1]
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE visit_requests
                SET status='SCHEDULED', visit_at=%s, scheduled_at=NOW(),
                    assigned_user_id=COALESCE(assigned_user_id,%s),
                    reminder_sent_at=NULL, updated_at=NOW()
                WHERE id=%s
                """,
                (when, actor_user_id, visit_id),
            )
            cur.execute(
                """
                UPDATE smart_visit_approvals
                SET manager_status='APPROVED', decided_by_user_id=%s,
                    decided_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (actor_user_id, approval_id),
            )
        conn.commit()
    return detail


def _set_visit_proposed(approval_id, proposed_at, actor_user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE smart_visit_approvals
                SET proposed_visit_at=%s, manager_status='PROPOSED',
                    decided_by_user_id=%s, decided_at=NOW(), updated_at=NOW()
                WHERE id=%s
                RETURNING customer_telegram_user_id
                """,
                (proposed_at, actor_user_id, approval_id),
            )
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def _reject_visit(approval_id, actor_user_id, note):
    detail = _visit_approval_detail(approval_id)
    if not detail:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE visit_requests SET status='CANCELLED', updated_at=NOW() WHERE id=%s", (detail[1],))
            cur.execute(
                """
                UPDATE smart_visit_approvals
                SET manager_status='REJECTED', manager_note=%s,
                    decided_by_user_id=%s, decided_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (note, actor_user_id, approval_id),
            )
        conn.commit()
    return detail


def _finance_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 سرمایه نقدی دارم", callback_data="sc_fin_cash")],
        [InlineKeyboardButton("🏠 باید ملکم را بفروشم", callback_data="sc_fin_sell")],
    ])


def get_customer_funding_status(customer_id):
    if not customer_id:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT funding_status FROM customer_purchase_profiles WHERE customer_id=%s",
                (customer_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def set_customer_funding_status(customer_id, funding_status):
    if not customer_id or funding_status not in {"CASH", "SELL_PROPERTY"}:
        return False
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_purchase_profiles(
                    customer_id,funding_status,asked_at,answered_at,updated_at
                )
                VALUES (%s,%s,NOW(),NOW(),NOW())
                ON CONFLICT (customer_id) DO UPDATE
                SET funding_status=EXCLUDED.funding_status,
                    answered_at=NOW(),
                    updated_at=NOW()
                """,
                (customer_id, funding_status),
            )
        conn.commit()
    return True


async def ask_finance_once(bot, telegram_user_id, customer_id):
    """Ask the purchase-funding question only for the customer's first visit."""
    if get_customer_funding_status(customer_id):
        return False
    set_conversation(telegram_user_id, "FINANCE_QUESTION", {}, customer_id=customer_id)
    await bot.send_message(
        chat_id=telegram_user_id,
        text=(
            "برای خرید این ملک، سرمایه‌تون نقده یا برای خرید باید ملک فعلی‌تون رو بفروشید؟"
        ),
        reply_markup=_finance_keyboard(),
    )
    return True


# =========================================================
# SELLER SUBMISSION
# =========================================================


def _create_or_reset_submission(customer_id, telegram_user_id, existing_id=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            if existing_id:
                cur.execute(
                    """
                    UPDATE seller_submissions
                    SET status='DRAFT', manager_note=NULL, updated_at=NOW()
                    WHERE id=%s AND telegram_user_id=%s
                    RETURNING id
                    """,
                    (existing_id, telegram_user_id),
                )
                row = cur.fetchone()
                if row:
                    conn.commit()
                    return row[0]
            cur.execute(
                """
                INSERT INTO seller_submissions(customer_id,telegram_user_id,status,created_at,updated_at)
                VALUES (%s,%s,'DRAFT',NOW(),NOW()) RETURNING id
                """,
                (customer_id, telegram_user_id),
            )
            sid = cur.fetchone()[0]
        conn.commit()
    return sid


def _submission_update(submission_id, **fields):
    allowed = {
        "district","public_location","private_address","area_m2","bedrooms",
        "floor_text","total_floors","build_year","has_parking","has_elevator",
        "has_storage","price_toman","description","owner_name","owner_phone",
        "status","manager_note","approved_by_user_id","property_id","submitted_at","decided_at",
    }
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    set_sql = ", ".join(f"{k}=%s" for k, _ in items) + ", updated_at=NOW()"
    params = [v for _, v in items] + [submission_id]
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE seller_submissions SET {set_sql} WHERE id=%s", params)
        conn.commit()


def _submission(submission_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,customer_id,telegram_user_id,status,district,public_location,
                       private_address,area_m2,bedrooms,floor_text,total_floors,build_year,
                       has_parking,has_elevator,has_storage,price_toman,description,
                       owner_name,owner_phone,manager_note,property_id
                FROM seller_submissions WHERE id=%s
                """,
                (submission_id,),
            )
            return cur.fetchone()


def _submission_media(submission_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telegram_file_id,telegram_file_unique_id,position
                FROM seller_submission_media
                WHERE submission_id=%s ORDER BY position,id
                """,
                (submission_id,),
            )
            return cur.fetchall()


def _submission_media_count(submission_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM seller_submission_media WHERE submission_id=%s", (submission_id,))
            return int(cur.fetchone()[0] or 0)


def _add_submission_photo(submission_id, file_id, unique_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM seller_submission_media WHERE submission_id=%s", (submission_id,))
            count = int(cur.fetchone()[0] or 0)
            if count >= MAX_SELLER_PHOTOS:
                return count, False
            if unique_id:
                cur.execute(
                    "SELECT 1 FROM seller_submission_media WHERE submission_id=%s AND telegram_file_unique_id=%s",
                    (submission_id, unique_id),
                )
                if cur.fetchone():
                    return count, False
            cur.execute(
                """
                INSERT INTO seller_submission_media(
                    submission_id,telegram_file_id,telegram_file_unique_id,position,created_at
                ) VALUES (%s,%s,%s,%s,NOW())
                """,
                (submission_id, file_id, unique_id, count),
            )
        conn.commit()
    return count + 1, True


def _clear_submission_media(submission_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seller_submission_media WHERE submission_id=%s", (submission_id,))
        conn.commit()


def _yes_no_keyboard(field):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ دارد", callback_data=f"sc_bool_{field}_1"),
        InlineKeyboardButton("❌ ندارد", callback_data=f"sc_bool_{field}_0"),
    ]])


_CORRECTION_SPECS = {
    "district": ("منطقه/محله", "📍 محله/منطقه اصلاح‌شده را بفرستید. مثال: رشدیه-بوستان"),
    "private_address": ("آدرس دقیق", "📌 آدرس دقیق اصلاح‌شده ملک را بفرستید. این مورد خصوصی می‌ماند."),
    "area_m2": ("متراژ", "📐 متراژ اصلاح‌شده را فقط با عدد بفرستید؛ مثلاً 140."),
    "bedrooms": ("تعداد خواب", "🛏 تعداد خواب اصلاح‌شده را بفرستید؛ مثلاً 3."),
    "floor_text": ("طبقه واحد", "🏢 طبقه اصلاح‌شده واحد را بفرستید؛ مثلاً 4 یا همکف."),
    "total_floors": ("تعداد طبقات", "🏙 تعداد طبقات اصلاح‌شده ساختمان را بفرستید؛ اگر نامشخص است - بفرستید."),
    "build_year": ("سال ساخت", "📅 سال ساخت اصلاح‌شده را شمسی بفرستید؛ مثلاً 1400."),
    "has_parking": ("پارکینگ", "🚗 وضعیت پارکینگ را اصلاح کنید."),
    "has_elevator": ("آسانسور", "🛗 وضعیت آسانسور را اصلاح کنید."),
    "has_storage": ("انباری", "📦 وضعیت انباری را اصلاح کنید."),
    "price_toman": ("قیمت", "💰 قیمت اصلاح‌شده یا حداقل قیمت مدنظرتان را بفرستید؛ مثلاً 8.5 میلیارد."),
    "description": ("توضیحات", "📝 توضیحات اصلاح‌شده را بفرستید؛ برای حذف توضیحات - بفرستید."),
    "owner_name": ("نام مالک", "👤 نام مالک اصلاح‌شده را بفرستید. این مورد عمومی نمی‌شود."),
    "owner_phone": ("شماره مالک", "📞 شماره مالک اصلاح‌شده را بفرستید. این مورد عمومی نمی‌شود."),
    "photos": ("عکس‌ها", "📸 عکس‌های جدید ملک را ارسال کنید و در پایان روی «پایان ارسال عکس» بزنید."),
}


def _infer_correction_fields(note):
    raw = normalize_digits(note).strip().lower()
    found = []

    def add(field):
        if field not in found:
            found.append(field)

    # More specific phrases first.
    if any(k in raw for k in ("شماره مالک", "تلفن مالک", "شماره تماس مالک")):
        add("owner_phone")
    if "نام مالک" in raw:
        add("owner_name")
    if any(k in raw for k in ("آدرس دقیق", "آدرس ملک", "آدرس")):
        add("private_address")
    if any(k in raw for k in ("منطقه", "محله", "لوکیشن")):
        add("district")
    if any(k in raw for k in ("متراژ", "متراژش", "چند متر", "مترش")):
        add("area_m2")
    if any(k in raw for k in ("تعداد خواب", "خواب")):
        add("bedrooms")
    if any(k in raw for k in ("تعداد طبقات", "کل طبقات", "طبقات ساختمان")):
        add("total_floors")
    elif any(k in raw for k in ("طبقه واحد", "طبقه")):
        add("floor_text")
    if any(k in raw for k in ("سال ساخت", "ساخت")):
        add("build_year")
    if "پارکینگ" in raw:
        add("has_parking")
    if "آسانسور" in raw:
        add("has_elevator")
    if "انباری" in raw:
        add("has_storage")
    if any(k in raw for k in ("قیمت", "مبلغ", "گرون", "گران", "حداقل قیمت", "قیمت نهایی")):
        add("price_toman")
    if any(k in raw for k in ("توضیحات", "توضیح")):
        add("description")
    if any(k in raw for k in ("عکس", "تصویر", "عکسها", "عکس‌ها")):
        add("photos")

    # Photos are always last so textual corrections can be completed first.
    if "photos" in found:
        found = [f for f in found if f != "photos"] + ["photos"]
    return found


def _correction_field_keyboard(submission_id, fields=None):
    choices = fields or list(_CORRECTION_SPECS.keys())
    buttons = []
    row = []
    for field in choices:
        spec = _CORRECTION_SPECS.get(field)
        if not spec:
            continue
        row.append(InlineKeyboardButton(spec[0], callback_data=f"sc_seller_field_{submission_id}_{field}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _current_correction_field(payload):
    fields = list(payload.get("correction_fields") or [])
    index = int(payload.get("correction_index") or 0)
    if 0 <= index < len(fields):
        return fields[index]
    return None


def _correction_prompt(field):
    spec = _CORRECTION_SPECS.get(field)
    return spec[1] if spec else "مقدار اصلاح‌شده را بفرستید."


def _correction_keyboard(field):
    mapping = {
        "has_parking": "parking",
        "has_elevator": "elevator",
        "has_storage": "storage",
    }
    callback_field = mapping.get(field)
    return _yes_no_keyboard(callback_field) if callback_field else None


async def _start_seller_correction(target, telegram_user_id, customer_id, submission_id, fields):
    fields = [f for f in fields if f in _CORRECTION_SPECS]
    if not fields:
        await target.edit_message_text(
            "مدیر توضیح اصلاح را فرستاده، اما مورد مشخصی از متن تشخیص داده نشد. فقط همان بخشی که لازم است اصلاح شود را انتخاب کنید:",
            reply_markup=_correction_field_keyboard(submission_id),
        )
        return True

    payload = {
        "submission_id": submission_id,
        "correction_mode": True,
        "correction_fields": fields,
        "correction_index": 0,
    }
    field = fields[0]
    if field == "photos":
        _clear_submission_media(submission_id)
        set_conversation(telegram_user_id, "SELL_PHOTOS", payload, customer_id)
        await target.edit_message_text(
            _correction_prompt(field),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان ارسال عکس", callback_data="sc_photos_done")]]),
        )
        return True

    set_conversation(telegram_user_id, "SELL_CORRECTION_FIELD", payload, customer_id)
    await target.edit_message_text(_correction_prompt(field), reply_markup=_correction_keyboard(field))
    return True


async def _advance_seller_correction(message, context, telegram_user_id, customer_id, payload):
    fields = list(payload.get("correction_fields") or [])
    index = int(payload.get("correction_index") or 0) + 1
    submission_id = payload.get("submission_id")
    if index >= len(fields):
        _submission_update(submission_id, status="PENDING", submitted_at=datetime.now(TEHRAN))
        clear_conversation(telegram_user_id, customer_id)
        row = _submission(submission_id)
        await message.reply_text(
            "✅ اصلاح موردنظر ثبت شد و فقط همان تغییر برای بررسی مجدد به ShahFile ارسال شد.\n\n" + _seller_preview(row, private=False)
        )
        await notify_owner_submission(context.bot, submission_id)
        return True

    payload["correction_index"] = index
    field = fields[index]
    if field == "photos":
        _clear_submission_media(submission_id)
        set_conversation(telegram_user_id, "SELL_PHOTOS", payload, customer_id)
        await message.reply_text(
            _correction_prompt(field),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان ارسال عکس", callback_data="sc_photos_done")]]),
        )
        return True

    set_conversation(telegram_user_id, "SELL_CORRECTION_FIELD", payload, customer_id)
    await message.reply_text(_correction_prompt(field), reply_markup=_correction_keyboard(field))
    return True


def _seller_preview(row, *, private=False):
    if not row:
        return "-"
    (
        sid, customer_id, tg_id, status, district, public_location, private_address,
        area, bedrooms, floor_text, total_floors, build_year, parking, elevator,
        storage, price_toman, description, owner_name, owner_phone, manager_note, property_id
    ) = row
    lines = [
        "🏠 پیش‌نمایش عمومی فایل",
        "",
        f"📍 {public_location or district or '-'}",
        f"📐 متراژ: {float(area):g} متر" if area is not None else "📐 متراژ: -",
        f"🛏 خواب: {bedrooms if bedrooms is not None else '-'}",
        f"🏢 طبقه: {floor_text or '-'}",
    ]
    if total_floors is not None:
        lines.append(f"🏙 تعداد طبقات ساختمان: {total_floors}")
    if build_year:
        lines.append(f"📅 سال ساخت: {build_year}")
    lines.extend([
        f"🚗 پارکینگ: {'دارد' if parking else 'ندارد'}",
        f"🛗 آسانسور: {'دارد' if elevator else 'ندارد'}",
        f"📦 انباری: {'دارد' if storage else 'ندارد'}",
    ])
    if price_toman:
        lines.append(f"💰 قیمت: {price_toman/1_000_000_000:g} میلیارد تومان")
    if description:
        lines.extend(["", "📝 توضیحات:", description])
    if private:
        lines.extend([
            "",
            "🔐 اطلاعات خصوصی — هرگز در کانال نمایش داده نمی‌شود",
            f"📌 آدرس دقیق: {private_address or '-'}",
            f"👤 مالک: {owner_name or '-'}",
            f"📞 شماره مالک: {owner_phone or '-'}",
            f"🆔 Submission: {sid}",
        ])
    return "\n".join(lines)


def _seller_manager_keyboard(submission_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید و انتشار", callback_data=f"sc_mgr_sa_{submission_id}")],
        [InlineKeyboardButton("✏️ درخواست اصلاح", callback_data=f"sc_mgr_sc_{submission_id}")],
        [InlineKeyboardButton("❌ رد فایل", callback_data=f"sc_mgr_sr_{submission_id}")],
    ])


async def notify_owner_submission(bot, submission_id):
    row = _submission(submission_id)
    if not row:
        return 0
    media = _submission_media(submission_id)
    sent = 0
    for telegram_id in _owner_telegram_ids():
        try:
            if media:
                album = [InputMediaPhoto(m[0]) for m in media[:MAX_SELLER_PHOTOS]]
                if len(album) == 1:
                    await bot.send_photo(chat_id=telegram_id, photo=album[0].media)
                else:
                    await bot.send_media_group(chat_id=telegram_id, media=album)
            await bot.send_message(
                chat_id=telegram_id,
                text=("📥 فایل جدید فروشنده برای بررسی ShahFile\n\n" + _seller_preview(row, private=True)),
                reply_markup=_seller_manager_keyboard(submission_id),
            )
            sent += 1
        except Exception:
            logging.exception("seller submission manager notification failed submission_id=%s", submission_id)
    return sent


def _property_hashtag(price_toman):
    billion = Decimal(price_toman) / Decimal("1000000000")
    low = int(math.floor(float(billion)))
    high = int(math.ceil(float(billion)))
    if high <= low:
        high = low + 1
    return f"#آپارتمان_{low}_{high}"


def _materialize_submission_property(submission_id, actor_user_id):
    row = _submission(submission_id)
    if not row:
        raise ValueError("submission not found")
    (
        sid, customer_id, tg_id, status, district, public_location, private_address,
        area, bedrooms, floor_text, total_floors, build_year, parking, elevator,
        storage, price_toman, description, owner_name, owner_phone, manager_note, property_id
    ) = row

    if property_id:
        return property_id
    if not price_toman:
        raise ValueError("submission has no price")

    sanitized_lines = [
        f"منطقه: {public_location or district or '-'}",
        f"متراژ: {float(area):g}" if area is not None else "",
        f"خواب: {bedrooms}" if bedrooms is not None else "",
        f"طبقه: {floor_text}" if floor_text else "",
        f"سال ساخت: {build_year}" if build_year else "",
        f"قیمت: {price_toman}",
        description or "",
    ]
    raw_public = "\n".join(x for x in sanitized_lines if x).strip()
    billion = Decimal(price_toman) / Decimal("1000000000")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties(
                    property_type,district,address_text,area_m2,bedrooms,floor_text,total_floors,
                    build_year,has_parking,has_elevator,has_storage,current_price_toman,
                    current_price_billion,price_hashtag,status,raw_text,has_media,ingest_status,
                    source_kind,manual_created_by_user_id,manual_created_at,manual_price_toman,
                    manual_price_updated_at,created_at,updated_at
                ) VALUES (
                    'APARTMENT',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s,
                    'READY','CUSTOMER',%s,NOW(),%s,NOW(),NOW(),NOW()
                ) RETURNING id
                """,
                (
                    district, public_location or district, area, bedrooms, floor_text, total_floors,
                    build_year, parking, elevator, storage, price_toman, billion,
                    _property_hashtag(price_toman), raw_public, bool(_submission_media(submission_id)),
                    actor_user_id, price_toman,
                ),
            )
            pid = cur.fetchone()[0]

            for file_id, unique_id, position in _submission_media(submission_id):
                cur.execute(
                    """
                    INSERT INTO property_media(
                        property_id,telegram_file_id,telegram_file_unique_id,media_type,position,created_at
                    ) VALUES (%s,%s,%s,'PHOTO',%s,NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    (pid, file_id, unique_id, position),
                )

            if description:
                cur.execute(
                    """
                    INSERT INTO property_attributes(
                        property_id,attribute_key,attribute_label,attribute_value,display_order,is_visible,created_at,updated_at
                    ) VALUES (%s,'source_detail_customer_description','توضیحات فروشنده',%s,90,TRUE,NOW(),NOW())
                    ON CONFLICT (property_id,attribute_key)
                    DO UPDATE SET attribute_value=EXCLUDED.attribute_value,updated_at=NOW()
                    """,
                    (pid, description),
                )

            cur.execute(
                """
                UPDATE seller_submissions
                SET property_id=%s,status='APPROVED',approved_by_user_id=%s,
                    decided_at=NOW(),updated_at=NOW()
                WHERE id=%s
                """,
                (pid, actor_user_id, submission_id),
            )
        conn.commit()
    return pid


# =========================================================
# USER-FACING PROMPTS
# =========================================================


def _welcome_text():
    return (
        "سلام 👋 به ShahFile خوش اومدید.\n\n"
        "اینجا دو مسیر اصلی دارید و می‌تونید هر کدوم رو که به کارتون میاد انتخاب کنید:\n\n"
        "🏠 می‌خوام سریع ملکم رو بفروشم\n"
        "اگر قصد فروش ملک دارید، مشخصات اصلی ملک، قیمت پیشنهادی، امکانات، توضیحات و عکس‌ها رو مرحله‌به‌مرحله ازتون می‌گیریم. "
        "اطلاعات خصوصی مثل نام و شماره مالک و آدرس دقیق فقط برای بررسی مدیریت نگهداری می‌شن و در کانال عمومی نمایش داده نمی‌شن. "
        "بعد از تکمیل اطلاعات، فایل برای بررسی ShahFile ارسال می‌شه و بعد از تأیید مدیر وارد سیستم و مسیر انتشار می‌شه.\n\n"
        "🔎 واحد می‌خوام\n"
        "اگر دنبال خرید هستید، بودجه، متراژ، منطقه، تعداد خواب و ویژگی‌های مدنظرتون رو می‌گید و ShahFile بین فایل‌های واقعی موجود جستجو می‌کنه. "
        "بعد از پیدا کردن گزینه مناسب، می‌تونید از همینجا درخواست بازدید ثبت کنید و در صورت تمایل پیشنهاد فایل‌های جدید مشابه رو هم دریافت کنید.\n\n"
        "یکی از مسیرهای زیر رو انتخاب کنید 👇"
    )


def _start_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🏠 می‌خوام سریع ملکم رو بفروشم",
            callback_data="sc_start_sell",
        )],
        [InlineKeyboardButton(
            "🔎 واحد می‌خوام",
            callback_data="sc_start_buy",
        )],
    ])


def _contact_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ارسال شماره تماس من", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def handle_customer_start(update, context):
    user = update.effective_user
    profile = upsert_customer_from_telegram(user)
    customer_id = profile[0] if profile else None
    state, payload, _ = get_conversation(user.id)

    # /start is always a clear ShahFile landing page.  We keep any
    # submitted/reviewed seller record safe; only an unfinished DRAFT is
    # abandoned when the user actually chooses a new path below.
    extra = ""
    if state != "IDLE":
        extra = (
            "\n\nℹ️ یک فرایند نیمه‌کاره هم دارید. اگر یکی از مسیرهای زیر رو "
            "انتخاب کنید، فرایند نیمه‌کاره قبلی بسته می‌شه؛ فایل‌هایی که "
            "قبلاً برای بررسی ارسال شده‌اند محفوظ می‌مونن."
        )

    await update.effective_message.reply_text(
        _welcome_text() + extra,
        reply_markup=_start_choice_keyboard(),
    )
    return True


def _resume_prompt(state):
    prompts = {
        "VISIT_PHONE": "شماره تماس‌تون رو ارسال کنید یا دکمه ارسال شماره تماس رو بزنید.",
        "VISIT_DATETIME": "تاریخ و ساعت پیشنهادی بازدید رو بفرستید. مثال: 1405/07/20 18:30",
        "WAITING_VISIT_APPROVAL": "درخواست بازدید برای ShahFile ارسال شده و منتظر تأیید مدیر هستیم.",
        "FINANCE_QUESTION": "بفرمایید سرمایه خرید نقدی است یا باید ملک فعلی‌تون رو بفروشید؟",
        "SELL_DISTRICT": "محله/منطقه عمومی ملک رو بفرستید.",
        "SELL_PRIVATE_ADDRESS": "آدرس دقیق ملک رو بفرستید. این مورد عمومی نمی‌شه.",
        "SELL_AREA": "متراژ ملک رو بفرستید.",
        "SELL_BEDROOMS": "تعداد خواب رو بفرستید.",
        "SELL_FLOOR": "طبقه واحد رو بفرستید.",
        "SELL_TOTAL_FLOORS": "تعداد کل طبقات ساختمان رو بفرستید یا - بزنید.",
        "SELL_BUILD_YEAR": "سال ساخت رو بفرستید.",
        "SELL_PARKING": "پارکینگ دارد یا ندارد؟",
        "SELL_ELEVATOR": "آسانسور دارد یا ندارد؟",
        "SELL_STORAGE": "انباری دارد یا ندارد؟",
        "SELL_PRICE": "قیمت پیشنهادی کل رو بفرستید؛ مثلاً 9.5 میلیارد.",
        "SELL_DESCRIPTION": "توضیحات تکمیلی رو بفرستید یا - بزنید.",
        "SELL_OWNER_NAME": "نام مالک رو بفرستید.",
        "SELL_OWNER_PHONE": "شماره مالک رو بفرستید؛ این شماره عمومی نمی‌شه.",
        "SELL_PHOTOS": "عکس‌های ملک رو ارسال کنید و در پایان «پایان ارسال عکس» رو بزنید.",
    }
    return prompts.get(state, "پیام قبلی‌تون رو ادامه بدید.")


# =========================================================
# CUSTOMER TEXT / CONTACT / PHOTO
# =========================================================


async def _submit_smart_visit(update, context, user, customer_id, payload, visit_at):
    prop = None
    if payload.get("property_id"):
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id,unit_code,address_text,district,area_m2,bedrooms,build_year,
                           current_price_billion,channel_post_url
                    FROM properties WHERE id=%s
                    """,
                    (payload["property_id"],),
                )
                prop = cur.fetchone()
    if not prop:
        clear_conversation(user.id, customer_id)
        await update.effective_message.reply_text(
            "فایل موردنظر دیگه در دسترس نیست. دوباره با کد واحد درخواست بدید."
        )
        return True

    approval_id, _visit_id = _create_visit_approval(customer_id, user.id, prop, visit_at)
    sent = await notify_owner_visit(context.bot, approval_id)
    set_conversation(
        user.id,
        "WAITING_VISIT_APPROVAL",
        {"approval_id": approval_id, "property_id": prop[0]},
        customer_id=customer_id,
    )
    await update.effective_message.reply_text(
        "درخواست بازدید ثبت شد ✅\n"
        "زمان پیشنهادی شما برای ShahFile ارسال شد و بعد از تأیید مدیر همینجا بهتون خبر می‌دم."
        + ("" if sent else "\nدرخواست داخل سیستم ثبت شده و در صف بررسی مدیر قرار دارد."),
        reply_markup=ReplyKeyboardRemove(),
    )
    await ask_finance_once(context.bot, user.id, customer_id)
    return True


def _search_watch_offer_keyboard(subscription_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ بله، فایل‌های جدید رو بفرست",
            callback_data=f"sc_watch_yes_{subscription_id}",
        )],
        [InlineKeyboardButton(
            "❌ نه، لازم نیست",
            callback_data=f"sc_watch_no_{subscription_id}",
        )],
    ])


async def _send_search_results_with_watch_offer(update, result_text, keyboard, subscription_id):
    await update.message.reply_text(
        result_text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    if subscription_id:
        await update.message.reply_text(
            "🔔 می‌خواین در روزهای آینده هم هر فایل جدیدی که دقیقاً با همین جستجو جور بود براتون ارسال کنم؟\n\n"
            "هر وقت نخواستید، فقط کلمه «لغو» رو بفرستید.",
            reply_markup=_search_watch_offer_keyboard(subscription_id),
        )


def _search_confirmation_text(filters):
    filters = dict(filters or {})
    lines = ["درخواست شما رو این‌طور متوجه شدم:", ""]
    if filters.get("district"):
        lines.append(f"📍 منطقه: {filters['district']}")
    pmin, pmax = filters.get("price_min_billion"), filters.get("price_max_billion")
    if pmin is not None and pmax is not None:
        lines.append(f"💰 بودجه: {format_billion(pmin)} تا {format_billion(pmax)} میلیارد")
    elif pmax is not None:
        lines.append(f"💰 بودجه: تا {format_billion(pmax)} میلیارد")
    elif pmin is not None:
        lines.append(f"💰 بودجه: از {format_billion(pmin)} میلیارد")
    amin, amax = filters.get("area_min"), filters.get("area_max")
    if amin is not None and amax is not None:
        lines.append(f"📐 متراژ: {format_billion(amin)} تا {format_billion(amax)} متر")
    elif amin is not None:
        lines.append(f"📐 متراژ: از {format_billion(amin)} متر")
    elif amax is not None:
        lines.append(f"📐 متراژ: تا {format_billion(amax)} متر")
    if filters.get("bedrooms") is not None:
        lines.append(f"🛏 تعداد خواب: {filters['bedrooms']}")
    if filters.get("has_parking") is True:
        lines.append("🚗 پارکینگ: الزامی")
    if filters.get("has_elevator") is True:
        lines.append("🛗 آسانسور: الزامی")
    if filters.get("has_storage") is True:
        lines.append("📦 انباری: الزامی")
    if filters.get("deed_required") is True:
        lines.append("📄 سند: الزامی")
    if filters.get("direction"):
        lines.append(f"🧭 جهت: {filters['direction']}")
    lines.extend(["", "اگه درسته، فایل‌ها رو نشون بدم 👇"])
    return "\n".join(lines)


def _search_confirmation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ درسته، فایل‌ها رو نشون بده", callback_data="sc_search_confirm")],
        [InlineKeyboardButton("✏️ اصلاح درخواست", callback_data="sc_search_edit")],
        [InlineKeyboardButton("🏠 شروع دوباره", callback_data="sc_home")],
    ])


async def _offer_search_confirmation(update, context, filters, query_text, customer_id):
    context.user_data["smart_pending_search"] = {
        "filters": dict(filters or {}),
        "query_text": query_text,
        "customer_id": customer_id,
    }
    await update.message.reply_text(
        _search_confirmation_text(filters),
        reply_markup=_search_confirmation_keyboard(),
    )


async def handle_customer_text(update, context):
    if not update.effective_user or not update.message:
        return False
    user = update.effective_user
    text = (update.message.text or "").strip()
    profile = upsert_customer_from_telegram(user)
    customer_id = profile[0] if profile else None
    state, payload, stored_customer_id = get_conversation(user.id)
    customer_id = stored_customer_id or customer_id

    # «لغو» stops only the automatic property suggestion cycle.
    # It intentionally does not destroy an in-progress visit/seller conversation.
    normalized_text = normalize_digits(text).strip()

    # Global natural-language escape hatch for an in-progress form.
    # Example: «نه اونو تموم کن» while a seller form is waiting for an address.
    if state != "IDLE" and _is_flow_cancel_request(text):
        _abandon_local_flow(user.id, state, payload or {}, customer_id)
        await update.message.reply_text(
            "✅ فرایند قبلی بسته شد. حالا بگید دنبال چه ملکی هستید یا چه کاری براتون انجام بدم.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return True

    # A clearly new property search supersedes an unfinished local form.
    # This prevents stale seller states from consuming messages like
    # «70 تا 90 متری چی دارین؟» as an address/area answer.
    if state != "IDLE" and _looks_like_explicit_property_search(text):
        _abandon_local_flow(user.id, state, payload or {}, customer_id)
        state, payload = "IDLE", {}

        override_intent = await extract_ai_intent(
            text,
            telegram_user_id=user.id,
            pending_visit=False,
        )
        if (
            override_intent
            and override_intent.get("intent") == "search_property"
            and override_intent.get("filters")
        ):
            await _offer_search_confirmation(
                update, context, override_intent["filters"], text, customer_id
            )
            return True

        area_range = _parse_area_range_intent(text)
        if area_range:
            low_area, high_area = area_range
            result_text, keyboard, _total, _sub_id = subscribe_and_render(
                user.id,
                customer_id,
                {"area_min": low_area, "area_max": high_area},
                text,
            )
            await _send_search_results_with_watch_offer(
                update, result_text, keyboard, _sub_id
            )
            return True

        await update.message.reply_text(
            "✅ فرایند قبلی بسته شد. درخواست جدیدتون رو یک بار دیگه بفرستید."
        )
        return True

    if normalized_text in {"لغو", "لغو پیشنهادات", "توقف پیشنهادات"}:
        cancelled = cancel_active_subscription(user.id)
        await update.message.reply_text(
            "✅ ارسال پیشنهادات جدید برای شما متوقف شد."
            if cancelled
            else "در حال حاضر پیشنهاد خودکاری برای شما فعال نیست."
        )
        return True

    # AI intent layer understands natural Persian combinations such as
    # «70 تا 90 متر تو رشدیه تا 8 میلیارد». Property data itself always
    # comes from our database; AI only extracts intent/filters.
    ai_intent = None
    if state not in SELLER_STATES and state not in {
        "VISIT_PHONE", "VISIT_DATETIME", "FINANCE_QUESTION", "SHOWCASE_PHONE"
    }:
        ai_intent = await extract_ai_intent(
            text,
            telegram_user_id=user.id,
            pending_visit=(state == "WAITING_VISIT_APPROVAL"),
        )

        if ai_intent and ai_intent.get("intent") == "cancel_updates":
            cancelled = cancel_active_subscription(user.id)
            await update.message.reply_text(
                "✅ ارسال پیشنهادات جدید برای شما متوقف شد."
                if cancelled
                else "در حال حاضر پیشنهاد خودکاری برای شما فعال نیست."
            )
            return True

        if ai_intent and ai_intent.get("intent") == "search_property" and ai_intent.get("filters"):
            await _offer_search_confirmation(
                update, context, ai_intent["filters"], text, customer_id
            )
            return True

    # Rule-based fallback keeps basic price-range search working even if
    # OpenAI is temporarily unavailable. Complex multi-filter requests are
    # intentionally left for the AI layer above.
    budget = parse_budget_intent(text)
    if budget and state not in SELLER_STATES:
        low, high = budget
        search_filters = {
            "price_min_billion": float(low),
            "price_max_billion": float(high),
        }
        result_text, keyboard, _total, _sub_id = subscribe_and_render(
            user.id, customer_id, search_filters, text
        )
        await _send_search_results_with_watch_offer(
            update, result_text, keyboard, _sub_id
        )
        return True

    area_range = _parse_area_range_intent(text)
    if area_range and state not in SELLER_STATES:
        low_area, high_area = area_range
        result_text, keyboard, _total, _sub_id = subscribe_and_render(
            user.id,
            customer_id,
            {"area_min": low_area, "area_max": high_area},
            text,
        )
        await _send_search_results_with_watch_offer(
            update, result_text, keyboard, _sub_id
        )
        return True

    if state == "IDLE":
        unit_code = parse_visit_unit_intent(text)
        ai_visit_dt = None
        if ai_intent and ai_intent.get("intent") == "visit_request":
            unit_code = unit_code or ai_intent.get("unit_code")
            ai_visit_dt = ai_intent.get("visit_datetime_jalali")

        if unit_code:
            prop = find_visitable_by_unit(unit_code)
            if not prop:
                await update.message.reply_text("این کد واحد پیدا نشد یا در حال حاضر برای بازدید فعال نیست.")
                return True
            payload = {"property_id": prop[0], "unit_code": prop[1]}
            if ai_visit_dt:
                payload["visit_datetime_jalali"] = ai_visit_dt
            customer = get_customer(user.id)
            phone = customer[4] if customer else None
            if not phone:
                set_conversation(user.id, "VISIT_PHONE", payload, customer_id=customer_id)
                await update.message.reply_text(
                    f"واحد {prop[1]} پیدا شد ✅\nبرای ثبت درخواست بازدید، شماره تماس‌تون رو ارسال کنید.",
                    reply_markup=_contact_keyboard(),
                )
            else:
                parsed_ai_dt = parse_shamsi_visit_datetime(ai_visit_dt) if ai_visit_dt else None
                if parsed_ai_dt and parsed_ai_dt > datetime.now(TEHRAN):
                    return await _submit_smart_visit(
                        update, context, user, customer_id, payload, parsed_ai_dt
                    )
                set_conversation(user.id, "VISIT_DATETIME", payload, customer_id=customer_id)
                await update.message.reply_text(
                    f"واحد {prop[1]} پیدا شد ✅\nتاریخ و ساعت پیشنهادی بازدید رو بفرستید.\nمثال: 1405/07/20 18:30",
                    reply_markup=ReplyKeyboardRemove(),
                )
            return True

        ai_kind = (ai_intent or {}).get("intent")
        if ai_kind in {"sell_property", "finance_sell"}:
            sid = _create_or_reset_submission(customer_id, user.id)
            set_conversation(user.id, "SELL_DISTRICT", {"submission_id": sid}, customer_id=customer_id)
            await update.message.reply_text("📍 محله/منطقه حدودی ملک رو بفرستید.\nمثال: رشدیه-بوستان")
            return True

        # Rule-based fallback for seller language if OpenAI is temporarily unavailable.
        if (
            any(k in text for k in ("خونه بفروشم", "خانه بفروشم", "ملکم رو بفروشم", "ملکمو بفروشم"))
            or (("فروش" in text or "بفرو" in text) and any(k in text for k in ("ملک", "خونه", "خانه")))
        ):
            sid = _create_or_reset_submission(customer_id, user.id)
            set_conversation(user.id, "SELL_DISTRICT", {"submission_id": sid}, customer_id=customer_id)
            await update.message.reply_text("📍 محله/منطقه حدودی ملک رو بفرستید.\nمثال: رشدیه-بوستان")
            return True

        await update.message.reply_text(_welcome_text(), reply_markup=_start_choice_keyboard())
        return True

    if state == "SHOWCASE_PHONE":
        phone = parse_phone(text)
        if not phone:
            await update.message.reply_text(
                "شماره موبایل معتبر نیست. مثل 09121234567 ارسال کنید یا دکمه ارسال شماره تماس رو بزنید.",
                reply_markup=_contact_keyboard(),
            )
            return True
        customer_id = save_customer_phone(user.id, phone) or customer_id
        from customer_showcase import complete_showcase_registration
        return await complete_showcase_registration(
            update, context, payload.get("showcase_token") or ""
        )

    if state == "VISIT_PHONE":
        phone = parse_phone(text)
        if not phone:
            await update.message.reply_text("شماره موبایل معتبر نیست. مثل 09121234567 ارسال کنید یا دکمه ارسال شماره تماس رو بزنید.", reply_markup=_contact_keyboard())
            return True
        customer_id = save_customer_phone(user.id, phone) or customer_id
        prefilled_dt = parse_shamsi_visit_datetime(payload.get("visit_datetime_jalali") or "")
        if prefilled_dt and prefilled_dt > datetime.now(TEHRAN):
            return await _submit_smart_visit(update, context, user, customer_id, payload, prefilled_dt)
        set_conversation(user.id, "VISIT_DATETIME", payload, customer_id=customer_id)
        await update.message.reply_text("ممنون ✅\nحالا تاریخ و ساعت پیشنهادی بازدید رو بفرستید.\nمثال: 1405/07/20 18:30", reply_markup=ReplyKeyboardRemove())
        return True

    if state == "VISIT_DATETIME":
        visit_at = parse_shamsi_visit_datetime(text)
        if not visit_at:
            # Natural Persian date phrases (e.g. «20 ام این ماه ساعت 19») are normalized by AI.
            ai_visit = await extract_ai_intent(
                text, telegram_user_id=user.id, pending_visit=True
            )
            if ai_visit and ai_visit.get("visit_datetime_jalali"):
                visit_at = parse_shamsi_visit_datetime(ai_visit["visit_datetime_jalali"])
        if not visit_at or visit_at <= datetime.now(TEHRAN):
            await update.message.reply_text("تاریخ و ساعت معتبر نیست یا در گذشته است. تاریخ را حتماً شمسی بفرستید؛ مثال: 1405/07/20 18:30")
            return True
        return await _submit_smart_visit(update, context, user, customer_id, payload, visit_at)

    if state == "WAITING_VISIT_APPROVAL":
        await update.message.reply_text("درخواست بازدیدتون در انتظار بررسی ShahFile هست. به محض تأیید یا پیشنهاد زمان جدید، همینجا خبرتون می‌کنم.")
        return True

    if state == "FINANCE_QUESTION":
        if "نقد" in text:
            set_customer_funding_status(customer_id, "CASH")
            clear_conversation(user.id, customer_id)
            await update.message.reply_text(
                "عالیه ✅ وضعیت خرید شما به‌صورت سرمایه نقدی ثبت شد. "
                "ادامه هماهنگی بازدید از همین‌جا انجام می‌شود."
            )
            return True
        if any(k in text for k in ("فروش", "بفروشم", "ملک دارم", "خونه دارم", "خانه دارم")):
            set_customer_funding_status(customer_id, "SELL_PROPERTY")
            sid = _create_or_reset_submission(customer_id, user.id)
            set_conversation(user.id, "SELL_DISTRICT", {"submission_id": sid}, customer_id=customer_id)
            await update.message.reply_text("📍 محله/منطقه حدودی ملک رو بفرستید.\nمثال: رشدیه-بوستان")
            return True
        await update.message.reply_text("یکی از این دو حالت رو بگید: «نقدی» یا «باید ملکم رو بفروشم».", reply_markup=_finance_keyboard())
        return True

    sid = payload.get("submission_id")
    if state in SELLER_STATES and not sid:
        clear_conversation(user.id, customer_id)
        await update.message.reply_text("اطلاعات ثبت ملک منقضی شده. دوباره بنویسید «می‌خوام ملکم رو بفروشم».")
        return True

    if state == "SELL_CORRECTION_FIELD":
        field = _current_correction_field(payload)
        if not field:
            clear_conversation(user.id, customer_id)
            await update.message.reply_text("مرحله اصلاح منقضی شده. دوباره از پیام ShahFile روی دکمه اصلاح بزنید.")
            return True

        if field == "district":
            if len(text) < 2:
                await update.message.reply_text("نام محله/منطقه رو واضح‌تر بفرستید.")
                return True
            _submission_update(sid, district=text, public_location=text)
        elif field == "private_address":
            if len(text) < 5:
                await update.message.reply_text("آدرس دقیق رو کمی کامل‌تر بفرستید.")
                return True
            _submission_update(sid, private_address=text)
        elif field == "area_m2":
            value = parse_decimal(text, minimum=10, maximum=5000)
            if value is None:
                await update.message.reply_text("متراژ معتبر نیست؛ مثلاً 140.")
                return True
            _submission_update(sid, area_m2=value)
        elif field == "bedrooms":
            value = parse_int(text, minimum=0, maximum=20)
            if value is None:
                await update.message.reply_text("تعداد خواب معتبر نیست؛ مثلاً 3.")
                return True
            _submission_update(sid, bedrooms=value)
        elif field == "floor_text":
            if not text:
                await update.message.reply_text("طبقه رو بفرستید.")
                return True
            _submission_update(sid, floor_text=text)
        elif field == "total_floors":
            if text in ("-", "نمیدونم", "نمی‌دونم"):
                value = None
            else:
                value = parse_int(text, minimum=1, maximum=100)
                if value is None:
                    await update.message.reply_text("تعداد طبقات رو عدد بفرستید یا - بزنید.")
                    return True
            _submission_update(sid, total_floors=value)
        elif field == "build_year":
            value = parse_int(text, minimum=1300, maximum=1599)
            if value is None:
                await update.message.reply_text("سال ساخت را شمسی بفرستید؛ مثلاً 1400.")
                return True
            _submission_update(sid, build_year=value)
        elif field in ("has_parking", "has_elevator", "has_storage"):
            await update.message.reply_text("لطفاً از دکمه «دارد / ندارد» استفاده کنید.", reply_markup=_correction_keyboard(field))
            return True
        elif field == "price_toman":
            value = parse_price_toman(text)
            if value is None:
                await update.message.reply_text("قیمت رو مثل «8.5 میلیارد» یا مبلغ کامل به تومان بفرستید.")
                return True
            _submission_update(sid, price_toman=value)
        elif field == "description":
            _submission_update(sid, description=None if text == "-" else text)
        elif field == "owner_name":
            if len(text) < 2:
                await update.message.reply_text("نام مالک رو واضح‌تر بفرستید.")
                return True
            _submission_update(sid, owner_name=text)
        elif field == "owner_phone":
            value = parse_phone(text, strict_mobile=False)
            if not value:
                await update.message.reply_text("شماره معتبر نیست. شماره مالک رو فقط با عدد بفرستید.")
                return True
            _submission_update(sid, owner_phone=value)
        else:
            await update.message.reply_text("این مورد قابل اصلاح نیست. دوباره از پیام ShahFile وارد اصلاح شوید.")
            return True

        return await _advance_seller_correction(update.message, context, user.id, customer_id, payload)

    if state == "SELL_DISTRICT":
        if len(text) < 2:
            await update.message.reply_text("نام محله/منطقه رو واضح‌تر بفرستید.")
            return True
        _submission_update(sid, district=text, public_location=text)
        set_conversation(user.id, "SELL_PRIVATE_ADDRESS", payload, customer_id)
        await update.message.reply_text("📌 حالا آدرس دقیق ملک رو بفرستید.\nاین آدرس خصوصی می‌مونه و در کانال عمومی نمایش داده نمی‌شه.")
        return True

    if state == "SELL_PRIVATE_ADDRESS":
        if len(text) < 5:
            await update.message.reply_text("آدرس دقیق رو کمی کامل‌تر بفرستید.")
            return True
        _submission_update(sid, private_address=text)
        set_conversation(user.id, "SELL_AREA", payload, customer_id)
        await update.message.reply_text("📐 متراژ ملک چند متره؟")
        return True

    if state == "SELL_AREA":
        area = parse_decimal(text, minimum=10, maximum=5000)
        if area is None:
            await update.message.reply_text("متراژ معتبر نیست. فقط عدد بفرستید؛ مثلاً 125.")
            return True
        _submission_update(sid, area_m2=area)
        set_conversation(user.id, "SELL_BEDROOMS", payload, customer_id)
        await update.message.reply_text("🛏 چند خواب داره؟")
        return True

    if state == "SELL_BEDROOMS":
        beds = parse_int(text, minimum=0, maximum=20)
        if beds is None:
            await update.message.reply_text("تعداد خواب معتبر نیست. مثلاً 3.")
            return True
        _submission_update(sid, bedrooms=beds)
        set_conversation(user.id, "SELL_FLOOR", payload, customer_id)
        await update.message.reply_text("🏢 واحد طبقه چنده؟ مثلاً 4 یا همکف.")
        return True

    if state == "SELL_FLOOR":
        if not text:
            await update.message.reply_text("طبقه رو بفرستید.")
            return True
        _submission_update(sid, floor_text=text)
        set_conversation(user.id, "SELL_TOTAL_FLOORS", payload, customer_id)
        await update.message.reply_text("🏙 ساختمان چند طبقه است؟ اگر نمی‌دونید «-» بفرستید.")
        return True

    if state == "SELL_TOTAL_FLOORS":
        if text in ("-", "نمیدونم", "نمی‌دونم"):
            total_floors = None
        else:
            total_floors = parse_int(text, minimum=1, maximum=100)
            if total_floors is None:
                await update.message.reply_text("تعداد طبقات رو عدد بفرستید یا - بزنید.")
                return True
        _submission_update(sid, total_floors=total_floors)
        set_conversation(user.id, "SELL_BUILD_YEAR", payload, customer_id)
        await update.message.reply_text("📅 سال ساخت رو بفرستید؛ مثلاً 1400.")
        return True

    if state == "SELL_BUILD_YEAR":
        year = parse_int(text, minimum=1300, maximum=2100)
        if year is None:
            await update.message.reply_text("سال ساخت معتبر نیست؛ مثلاً 1400.")
            return True
        _submission_update(sid, build_year=year)
        set_conversation(user.id, "SELL_PARKING", payload, customer_id)
        await update.message.reply_text("🚗 پارکینگ دارد؟", reply_markup=_yes_no_keyboard("parking"))
        return True

    if state in ("SELL_PARKING", "SELL_ELEVATOR", "SELL_STORAGE"):
        await update.message.reply_text("لطفاً از دکمه «دارد / ندارد» استفاده کنید.")
        return True

    if state == "SELL_PRICE":
        price = parse_price_toman(text)
        if price is None:
            await update.message.reply_text("قیمت رو مثل «9.5 میلیارد» یا مبلغ کامل به تومان بفرستید.")
            return True
        _submission_update(sid, price_toman=price)
        set_conversation(user.id, "SELL_DESCRIPTION", payload, customer_id)
        await update.message.reply_text("📝 توضیحات تکمیلی ملک رو بفرستید؛ اگر چیزی ندارید «-» بزنید.")
        return True

    if state == "SELL_DESCRIPTION":
        _submission_update(sid, description=None if text == "-" else text)
        set_conversation(user.id, "SELL_OWNER_NAME", payload, customer_id)
        await update.message.reply_text("👤 نام مالک رو بفرستید. این اطلاعات عمومی نمی‌شه.")
        return True

    if state == "SELL_OWNER_NAME":
        if len(text) < 2:
            await update.message.reply_text("نام مالک رو واضح‌تر بفرستید.")
            return True
        _submission_update(sid, owner_name=text)
        set_conversation(user.id, "SELL_OWNER_PHONE", payload, customer_id)
        await update.message.reply_text("📞 شماره تماس مالک رو بفرستید. این شماره در کانال عمومی نمایش داده نمی‌شه.")
        return True

    if state == "SELL_OWNER_PHONE":
        owner_phone = parse_phone(text, strict_mobile=False)
        if not owner_phone:
            await update.message.reply_text("شماره معتبر نیست. شماره موبایل یا تلفن مالک رو فقط با عدد بفرستید.")
            return True
        _submission_update(sid, owner_phone=owner_phone)
        _clear_submission_media(sid)
        set_conversation(user.id, "SELL_PHOTOS", payload, customer_id)
        await update.message.reply_text(
            f"📸 حالا عکس‌های ملک رو ارسال کنید؛ حداکثر {MAX_SELLER_PHOTOS} عکس.\nوقتی تموم شد روی «پایان ارسال عکس» بزنید.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان ارسال عکس", callback_data="sc_photos_done")]]),
        )
        return True

    if state == "SELL_PHOTOS":
        if text in ("پایان", "تمام", "پایان ارسال عکس"):
            return await _finish_seller_photos(update, context, user.id, customer_id, payload)
        await update.message.reply_text("در این مرحله عکس‌های ملک رو ارسال کنید و بعد «پایان ارسال عکس» رو بزنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان ارسال عکس", callback_data="sc_photos_done")]]))
        return True

    await update.message.reply_text(_welcome_text(), reply_markup=_start_choice_keyboard())
    return True


async def handle_customer_contact(update, context):
    if not update.effective_user or not update.message or not update.message.contact:
        return False
    user = update.effective_user
    state, payload, customer_id = get_conversation(user.id)
    if state not in {"VISIT_PHONE", "SHOWCASE_PHONE"}:
        return False
    contact = update.message.contact
    if contact.user_id and contact.user_id != user.id:
        await update.message.reply_text("لطفاً شماره خودتون رو با دکمه ارسال شماره تماس بفرستید.")
        return True
    phone = parse_phone(contact.phone_number)
    if not phone:
        await update.message.reply_text("شماره تماس معتبر تشخیص داده نشد. لطفاً شماره رو به صورت 09121234567 بفرستید.")
        return True
    customer_id = save_customer_phone(user.id, phone) or customer_id
    if state == "SHOWCASE_PHONE":
        from customer_showcase import complete_showcase_registration
        return await complete_showcase_registration(
            update, context, payload.get("showcase_token") or ""
        )
    prefilled_dt = parse_shamsi_visit_datetime(payload.get("visit_datetime_jalali") or "")
    if prefilled_dt and prefilled_dt > datetime.now(TEHRAN):
        return await _submit_smart_visit(update, context, user, customer_id, payload, prefilled_dt)
    set_conversation(user.id, "VISIT_DATETIME", payload, customer_id=customer_id)
    await update.message.reply_text("ممنون ✅\nحالا تاریخ و ساعت پیشنهادی بازدید رو بفرستید.\nمثال: 1405/07/20 18:30", reply_markup=ReplyKeyboardRemove())
    return True


async def handle_customer_photo(update, context):
    if not update.effective_user or not update.message or not update.message.photo:
        return False
    user = update.effective_user
    state, payload, customer_id = get_conversation(user.id)
    if state != "SELL_PHOTOS":
        return False
    sid = payload.get("submission_id")
    if not sid:
        return False
    photo = update.message.photo[-1]
    count, added = _add_submission_photo(sid, photo.file_id, photo.file_unique_id)
    if added:
        await update.message.reply_text(
            f"✅ عکس {count} ثبت شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان ارسال عکس", callback_data="sc_photos_done")]]),
        )
    elif count >= MAX_SELLER_PHOTOS:
        await update.message.reply_text(
            f"حداکثر {MAX_SELLER_PHOTOS} عکس ثبت شد. روی پایان ارسال عکس بزنید.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ پایان ارسال عکس", callback_data="sc_photos_done")]]),
        )
    return True


async def _finish_seller_photos(update, context, telegram_user_id, customer_id, payload):
    sid = payload.get("submission_id")
    if not sid:
        return True
    count = _submission_media_count(sid)
    if count < 1:
        await update.effective_message.reply_text("حداقل یک عکس از ملک لازم داریم.")
        return True
    _submission_update(sid, status="PENDING", submitted_at=datetime.now(TEHRAN))
    clear_conversation(telegram_user_id, customer_id)
    row = _submission(sid)
    done_text = (
        "✅ اصلاح عکس‌ها ثبت شد و برای بررسی مجدد ShahFile ارسال شد.\n\n"
        if payload.get("correction_mode")
        else "✅ اطلاعات ملک کامل شد و برای بررسی ShahFile ارسال شد.\n\n"
    )
    await update.effective_message.reply_text(
        done_text + _seller_preview(row, private=False),
        reply_markup=ReplyKeyboardRemove(),
    )
    await notify_owner_submission(context.bot, sid)
    return True


# =========================================================
# CALLBACK ROUTER — CUSTOMER + OWNER
# =========================================================


async def handle_customer_callback(update, context, session=None):
    query = update.callback_query
    action = (query.data if query else "") or ""
    if not action.startswith("sc_"):
        return False

    # Manager actions require an authenticated OWNER session.
    if action.startswith("sc_mgr_"):
        if not session or session[3] != "OWNER":
            await query.edit_message_text("⛔ این عملیات فقط برای مدیر اصلی ShahFile مجاز است.")
            return True
        return await _handle_manager_callback(update, context, session, action)

    user = update.effective_user
    if not user:
        return True
    profile = upsert_customer_from_telegram(user)
    customer_id = profile[0] if profile else None
    state, payload, stored_customer_id = get_conversation(user.id)
    customer_id = stored_customer_id or customer_id

    if action == "sc_home":
        _abandon_local_flow(user.id, state, payload, customer_id)
        await query.edit_message_text(
            _welcome_text(),
            reply_markup=_start_choice_keyboard(),
        )
        return True

    if action == "sc_start_buy":
        _abandon_local_flow(user.id, state, payload, customer_id)
        await query.edit_message_text(
            "🔎 واحد می‌خوام\n\n"
            "نیازت رو خیلی عادی برام بنویس؛ ShahFile خودش فیلترها رو از متن درمیاره.\n\n"
            "مثال‌ها:\n"
            "• ۶ تا ۸ میلیارد چی دارین؟\n"
            "• ۸۰ تا ۱۱۰ متر در ولیعصر\n"
            "• سه خواب با پارکینگ زیر ۱۵ میلیارد\n"
            "• برای کد ۲۷۲۹ وقت بازدید می‌خوام\n\n"
            "درخواستت رو همینجا بفرست 👇",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ بازگشت", callback_data="sc_home")
            ]]),
        )
        return True

    if action == "sc_start_sell":
        _abandon_local_flow(user.id, state, payload, customer_id)
        sid = _create_or_reset_submission(customer_id, user.id)
        set_conversation(
            user.id,
            "SELL_DISTRICT",
            {"submission_id": sid, "entry_path": "start_sell"},
            customer_id,
        )
        await query.edit_message_text(
            "🏠 فروش سریع ملک\n\n"
            "مشخصات ملک رو مرحله‌به‌مرحله می‌گیریم. نام و شماره مالک و آدرس دقیق "
            "فقط برای مدیریت ShahFile ذخیره می‌شن و در کانال عمومی نمایش داده نمی‌شن.\n\n"
            "📍 اول محله/منطقه حدودی ملک رو بفرستید.\n"
            "مثال: رشدیه - بوستان"
        )
        return True

    if action == "sc_search_confirm":
        pending = context.user_data.pop("smart_pending_search", None) or {}
        filters = pending.get("filters")
        if not filters:
            await query.edit_message_text(
                "این درخواست منقضی شده؛ لطفاً نیازتون رو دوباره بنویسید."
            )
            return True
        result_text, keyboard, _total, subscription_id = subscribe_and_render(
            user.id,
            pending.get("customer_id") or customer_id,
            filters,
            pending.get("query_text") or "جست‌وجوی مشتری",
        )
        await query.edit_message_text(
            result_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        if subscription_id:
            await query.message.reply_text(
                "🔔 می‌خواین در روزهای آینده هم هر فایل جدیدی که دقیقاً با همین جستجو جور بود براتون ارسال کنم؟\n\n"
                "هر وقت نخواستید، فقط کلمه «لغو» رو بفرستید.",
                reply_markup=_search_watch_offer_keyboard(subscription_id),
            )
        return True

    if action == "sc_search_edit":
        context.user_data.pop("smart_pending_search", None)
        await query.edit_message_text(
            "✏️ خیلی ساده درخواست اصلاح‌شده‌تون رو دوباره بنویسید.\n\n"
            "مثال: زعفرانیه، ۷ تا ۸ میلیارد، پارکینگ و آسانسور داشته باشه"
        )
        return True

    if action.startswith("sc_watch_yes_"):
        try:
            subscription_id = int(action.rsplit("_", 1)[1])
        except Exception:
            await query.answer("درخواست نامعتبر است", show_alert=True)
            return True
        if activate_search_subscription(subscription_id, user.id):
            await query.edit_message_text(
                "✅ فعال شد. از این به بعد هر فایل جدیدی که مطابق همین جستجو اضافه بشه، براتون ارسال می‌کنم.\n\n"
                "برای توقف، هر زمان کلمه «لغو» رو بفرستید."
            )
        else:
            await query.edit_message_text("این جستجو دیگه در دسترس نیست.")
        return True

    if action.startswith("sc_watch_no_"):
        try:
            subscription_id = int(action.rsplit("_", 1)[1])
        except Exception:
            await query.answer("درخواست نامعتبر است", show_alert=True)
            return True
        decline_search_subscription(subscription_id, user.id)
        await query.edit_message_text(
            "باشه 👍 پیشنهاد خودکار برای این جستجو فعال نشد. هر زمان خواستید دوباره جستجو کنید."
        )
        return True

    if action.startswith("sc_s_"):
        parts = action.split("_", 3)
        try:
            subscription_id = int(parts[2])
            offset = int(parts[3])
        except Exception:
            await query.answer("صفحه نامعتبر است", show_alert=True)
            return True
        page_text, keyboard, _total = ai_search_page(subscription_id, user.id, offset)
        if not page_text:
            await query.edit_message_text("این جستجو دیگه در دسترس نیست.")
            return True
        await query.edit_message_text(
            page_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True

    if action.startswith("sc_b_"):
        parts = action.split("_", 4)
        try:
            low = Decimal(parts[2])
            high = Decimal(parts[3])
            offset = int(parts[4])
        except Exception:
            await query.answer("صفحه نامعتبر است", show_alert=True)
            return True
        total = _budget_count(low, high)
        rows = _budget_rows(low, high, offset)
        await query.edit_message_text(
            _budget_text(low, high, rows, offset, total),
            reply_markup=_budget_keyboard(low, high, offset, total),
            disable_web_page_preview=True,
        )
        return True

    if action == "sc_fin_cash":
        set_customer_funding_status(customer_id, "CASH")
        clear_conversation(user.id, customer_id)
        await query.edit_message_text(
            "عالیه ✅ وضعیت خرید شما به‌صورت سرمایه نقدی ثبت شد. "
            "ادامه هماهنگی بازدید از همین‌جا انجام می‌شود."
        )
        return True

    if action == "sc_fin_sell":
        set_customer_funding_status(customer_id, "SELL_PROPERTY")
        sid = _create_or_reset_submission(customer_id, user.id)
        set_conversation(user.id, "SELL_DISTRICT", {"submission_id": sid}, customer_id)
        await query.edit_message_text("📍 محله/منطقه حدودی ملک رو بفرستید.\nمثال: رشدیه-بوستان")
        return True

    if action.startswith("sc_bool_"):
        parts = action.split("_")
        if len(parts) != 4:
            return True
        field, raw_bool = parts[2], parts[3]
        sid = payload.get("submission_id")
        if not sid:
            await query.edit_message_text("اطلاعات ثبت ملک منقضی شده. دوباره فرایند فروش رو شروع کنید.")
            return True
        value = raw_bool == "1"
        if state == "SELL_CORRECTION_FIELD" and payload.get("correction_mode"):
            current = _current_correction_field(payload)
            expected = {"parking": "has_parking", "elevator": "has_elevator", "storage": "has_storage"}.get(field)
            if current != expected:
                await query.answer("این مرحله منقضی شده است", show_alert=True)
                return True
            _submission_update(sid, **{current: value})
            # Advance using a lightweight Message-compatible adapter: send a new message, then remove old buttons.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            class _ReplyTarget:
                async def reply_text(self, text, reply_markup=None, **kwargs):
                    return await context.bot.send_message(chat_id=user.id, text=text, reply_markup=reply_markup, **kwargs)
            return await _advance_seller_correction(_ReplyTarget(), context, user.id, customer_id, payload)

        if field == "parking" and state == "SELL_PARKING":
            _submission_update(sid, has_parking=value)
            set_conversation(user.id, "SELL_ELEVATOR", payload, customer_id)
            await query.edit_message_text("🛗 آسانسور دارد؟", reply_markup=_yes_no_keyboard("elevator"))
            return True
        if field == "elevator" and state == "SELL_ELEVATOR":
            _submission_update(sid, has_elevator=value)
            set_conversation(user.id, "SELL_STORAGE", payload, customer_id)
            await query.edit_message_text("📦 انباری دارد؟", reply_markup=_yes_no_keyboard("storage"))
            return True
        if field == "storage" and state == "SELL_STORAGE":
            _submission_update(sid, has_storage=value)
            set_conversation(user.id, "SELL_PRICE", payload, customer_id)
            await query.edit_message_text("💰 قیمت پیشنهادی کل ملک رو بفرستید؛ مثلاً 9.5 میلیارد.")
            return True
        await query.answer("این مرحله منقضی شده است", show_alert=True)
        return True

    if action == "sc_photos_done":
        return await _finish_seller_photos(update, context, user.id, customer_id, payload)

    if action.startswith("sc_va_accept_"):
        approval_id = int(action.rsplit("_", 1)[1])
        detail = _visit_approval_detail(approval_id)
        if not detail or detail[2] != user.id or detail[5] != "PROPOSED" or not detail[4]:
            await query.edit_message_text("این پیشنهاد زمان دیگه معتبر نیست.")
            return True
        owner_id = _owner_user_id()
        detail = _schedule_approved_visit(approval_id, detail[4], owner_id)
        await query.edit_message_text(f"✅ وقت بازدید برای {format_local_dt(detail[4])} قطعی شد.")
        await ask_finance_once(context.bot, user.id, detail[6])
        return True

    if action.startswith("sc_va_decline_"):
        approval_id = int(action.rsplit("_", 1)[1])
        detail = _visit_approval_detail(approval_id)
        if not detail or detail[2] != user.id:
            await query.edit_message_text("این درخواست معتبر نیست.")
            return True
        set_conversation(user.id, "VISIT_DATETIME", {"property_id": detail[7], "unit_code": detail[8]}, detail[6])
        await query.edit_message_text("باشه. زمان پیشنهادی جدید خودتون رو بفرستید؛ مثال: 1405/07/20 19:00")
        return True

    if action.startswith("sc_seller_restart_"):
        sid = int(action.rsplit("_", 1)[1])
        row = _submission(sid)
        if not row or row[2] != user.id or row[3] not in ("NEEDS_CHANGES", "REJECTED", "DRAFT"):
            await query.edit_message_text("این فایل برای اصلاح در دسترس نیست.")
            return True
        fields = _infer_correction_fields(row[19] or "")
        return await _start_seller_correction(query, user.id, customer_id, sid, fields)

    if action.startswith("sc_seller_field_"):
        payload_part = action[len("sc_seller_field_"):]
        sid_text, field = payload_part.split("_", 1)
        sid = int(sid_text)
        row = _submission(sid)
        if not row or row[2] != user.id or row[3] not in ("NEEDS_CHANGES", "REJECTED", "DRAFT"):
            await query.edit_message_text("این فایل برای اصلاح در دسترس نیست.")
            return True
        if field not in _CORRECTION_SPECS:
            await query.answer("مورد اصلاح نامعتبر است", show_alert=True)
            return True
        return await _start_seller_correction(query, user.id, customer_id, sid, [field])

    return True


async def _handle_manager_callback(update, context, session, action):
    query = update.callback_query
    actor_user_id = session[0]

    if action.startswith("sc_mgr_va_"):
        approval_id = int(action.rsplit("_", 1)[1])
        detail = _visit_approval_detail(approval_id)
        if not detail or detail[5] != "PENDING":
            await query.edit_message_text("این درخواست قبلاً بررسی شده است.")
            return True
        detail = _schedule_approved_visit(approval_id, detail[3], actor_user_id)
        await query.edit_message_text("✅ وقت بازدید تأیید و در سیستم SCHEDULED شد.\n\n" + _visit_manager_text(detail))
        await context.bot.send_message(
            chat_id=detail[2],
            text=f"✅ ShahFile وقت بازدید شما برای کد {detail[8]} را تأیید کرد.\n🕐 {format_local_dt(detail[3])}",
        )
        await ask_finance_once(context.bot, detail[2], detail[6])
        return True

    if action.startswith("sc_mgr_vp_"):
        approval_id = int(action.rsplit("_", 1)[1])
        context.user_data["smart_manager_state"] = "VISIT_PROPOSE"
        context.user_data["smart_manager_id"] = approval_id
        await query.edit_message_text("🕐 زمان جایگزین را ارسال کنید.\nمثال: 1405/07/20 19:30")
        return True

    if action.startswith("sc_mgr_vr_"):
        approval_id = int(action.rsplit("_", 1)[1])
        context.user_data["smart_manager_state"] = "VISIT_REJECT"
        context.user_data["smart_manager_id"] = approval_id
        await query.edit_message_text("دلیل رد درخواست بازدید را کوتاه بنویسید.")
        return True

    if action.startswith("sc_mgr_sa_"):
        sid = int(action.rsplit("_", 1)[1])
        row = _submission(sid)
        if not row or row[3] not in ("PENDING", "APPROVED"):
            await query.edit_message_text("این فایل در وضعیت قابل انتشار نیست.")
            return True
        try:
            property_id = _materialize_submission_property(sid, actor_user_id)
            result = await publish_property(context.bot, property_id)
            _submission_update(sid, status="PUBLISHED", approved_by_user_id=actor_user_id, decided_at=datetime.now(TEHRAN))
            row = _submission(sid)
            await query.edit_message_text(
                "✅ فایل تأیید و منتشر شد.\n\n"
                f"کد واحد: {result.get('unit_code') or '-'}\n"
                f"لینک: {result.get('channel_post_url') or '-'}"
            )
            await context.bot.send_message(
                chat_id=row[2],
                text=(
                    "✅ ملک شما توسط ShahFile تأیید و منتشر شد.\n"
                    f"🔢 کد واحد: {result.get('unit_code') or '-'}\n"
                    f"🔗 {result.get('channel_post_url') or ''}"
                ),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.exception("seller publish failed submission_id=%s", sid)
            await query.edit_message_text("❌ انتشار انجام نشد؛ فایل و اطلاعات محفوظ ماندند.\nخطا: " + str(exc)[:400], reply_markup=_seller_manager_keyboard(sid))
        return True

    if action.startswith("sc_mgr_sc_"):
        sid = int(action.rsplit("_", 1)[1])
        context.user_data["smart_manager_state"] = "SELLER_CORRECTION"
        context.user_data["smart_manager_id"] = sid
        await query.edit_message_text("✏️ توضیح اصلاح موردنیاز را بنویسید تا برای فروشنده ارسال شود.")
        return True

    if action.startswith("sc_mgr_sr_"):
        sid = int(action.rsplit("_", 1)[1])
        context.user_data["smart_manager_state"] = "SELLER_REJECT"
        context.user_data["smart_manager_id"] = sid
        await query.edit_message_text("❌ دلیل رد فایل را کوتاه بنویسید تا برای فروشنده ارسال شود.")
        return True

    return True


async def handle_manager_text(update, context, session):
    state = context.user_data.get("smart_manager_state")
    if not state:
        return False
    if session[3] != "OWNER":
        context.user_data.pop("smart_manager_state", None)
        context.user_data.pop("smart_manager_id", None)
        return False

    text = (update.message.text or "").strip()
    target_id = context.user_data.get("smart_manager_id")
    actor_user_id = session[0]

    if state == "VISIT_PROPOSE":
        visit_at = parse_shamsi_visit_datetime(text)
        if not visit_at or visit_at <= datetime.now(TEHRAN):
            await update.message.reply_text("زمان معتبر نیست. تاریخ را حتماً شمسی بفرستید؛ مثال: 1405/07/20 19:30")
            return True
        tg_id = _set_visit_proposed(target_id, visit_at, actor_user_id)
        if not tg_id:
            await update.message.reply_text("درخواست پیدا نشد.")
        else:
            detail = _visit_approval_detail(target_id)
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"ShahFile برای بازدید کد {detail[8]} این زمان رو پیشنهاد داده:\n"
                    f"🕐 {format_local_dt(visit_at)}\n\nاین زمان براتون مناسبه؟"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ قبول می‌کنم", callback_data=f"sc_va_accept_{target_id}"),
                    InlineKeyboardButton("❌ زمان دیگری می‌خوام", callback_data=f"sc_va_decline_{target_id}"),
                ]]),
            )
            await update.message.reply_text("✅ زمان جایگزین برای مشتری ارسال شد.")

    elif state == "VISIT_REJECT":
        detail = _reject_visit(target_id, actor_user_id, text or "درخواست توسط ShahFile رد شد.")
        if detail:
            clear_conversation(detail[2], detail[6])
            await context.bot.send_message(
                chat_id=detail[2],
                text=f"درخواست بازدید کد {detail[8]} تأیید نشد.\nدلیل: {text or '-'}",
            )
        await update.message.reply_text("✅ نتیجه رد به مشتری اعلام شد.")

    elif state == "SELLER_CORRECTION":
        row = _submission(target_id)
        if row:
            _submission_update(target_id, status="NEEDS_CHANGES", manager_note=text, decided_at=datetime.now(TEHRAN))
            fields = _infer_correction_fields(text)
            if len(fields) == 1:
                label = _CORRECTION_SPECS[fields[0]][0]
                button_text = f"✏️ اصلاح {label}"
                footer = f"فقط «{label}» نیاز به اصلاح دارد؛ بقیه اطلاعات دست‌نخورده می‌ماند."
            elif len(fields) > 1:
                labels = "، ".join(_CORRECTION_SPECS[f][0] for f in fields)
                button_text = "✏️ انجام اصلاحات"
                footer = f"موارد تشخیص‌داده‌شده برای اصلاح: {labels}"
            else:
                button_text = "✏️ انتخاب مورد اصلاح"
                footer = "بعد از زدن دکمه، فقط موردی که نیاز به اصلاح دارد انتخاب می‌شود؛ فرم از اول شروع نمی‌شود."
            await context.bot.send_message(
                chat_id=row[2],
                text=(
                    "✏️ ShahFile برای فایل شما درخواست اصلاح دارد:\n\n"
                    f"{text}\n\n{footer}"
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data=f"sc_seller_restart_{target_id}")]]),
            )
        await update.message.reply_text("✅ درخواست اصلاح هدفمند برای فروشنده ارسال شد.")

    elif state == "SELLER_REJECT":
        row = _submission(target_id)
        if row:
            _submission_update(target_id, status="REJECTED", manager_note=text, decided_at=datetime.now(TEHRAN))
            clear_conversation(row[2], row[1])
            await context.bot.send_message(
                chat_id=row[2],
                text=f"❌ فایل شما توسط ShahFile تأیید نشد.\nدلیل: {text or '-'}",
            )
        await update.message.reply_text("✅ نتیجه رد برای فروشنده ارسال شد.")

    context.user_data.pop("smart_manager_state", None)
    context.user_data.pop("smart_manager_id", None)
    return True


# =========================================================
# END SHAHFILE SMART CUSTOMER FLOW V1.0.2 — SHAMSI + TARGETED CORRECTION
# =========================================================


# === SHAHFILE GLOBAL FLOW PREEMPTION V1.1.1 ===
