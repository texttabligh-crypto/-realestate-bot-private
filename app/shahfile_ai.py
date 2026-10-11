from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from crm_panel import TEHRAN


PAGE_SIZE = 6
WATCH_INTERVAL_SECONDS = max(30, int(os.getenv("SMART_SEARCH_WATCH_INTERVAL_SECONDS", "120") or 120))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
OPENAI_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
OPT_OUT_TEXT = "🔕 اگه می‌خواین دیگه پیشنهادات ما براتون ارسال نشه کلمه «لغو» رو بفرستین."

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def normalize_digits(value: Any) -> str:
    return str(value or "").translate(_DIGIT_MAP)


def ensure_ai_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_search_subscriptions (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_user_id BIGINT NOT NULL,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    query_text TEXT,
                    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    cancelled_at TIMESTAMPTZ,
                    last_scan_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_smart_search_one_active_per_user
                ON smart_search_subscriptions(telegram_user_id)
                WHERE is_active = TRUE
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_smart_search_active
                ON smart_search_subscriptions(is_active, updated_at)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_search_seen (
                    subscription_id BIGINT NOT NULL
                        REFERENCES smart_search_subscriptions(id) ON DELETE CASCADE,
                    property_id BIGINT NOT NULL
                        REFERENCES properties(id) ON DELETE CASCADE,
                    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    notification_sent_at TIMESTAMPTZ,
                    PRIMARY KEY (subscription_id, property_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_smart_search_seen_property
                ON smart_search_seen(property_id)
                """
            )
        conn.commit()


def _gregorian_to_jalali(gy, gm, gd):
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


def _today_jalali():
    now = datetime.now(TEHRAN)
    jy, jm, jd = _gregorian_to_jalali(now.year, now.month, now.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def _num(value, *, integer=False, minimum=None, maximum=None):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if minimum is not None and number < Decimal(str(minimum)):
        return None
    if maximum is not None and number > Decimal(str(maximum)):
        return None
    if integer:
        if number != int(number):
            return None
        return int(number)
    return float(number)


def normalize_search_filters(raw: Optional[dict]):
    raw = dict(raw or {})
    out = {}

    numeric_specs = {
        "price_min_billion": (False, 0, 1000000),
        "price_max_billion": (False, 0, 1000000),
        "area_min": (False, 1, 100000),
        "area_max": (False, 1, 100000),
        "bedrooms": (True, 0, 100),
        "build_year_min": (True, 1200, 2500),
        "build_year_max": (True, 1200, 2500),
    }
    for key, (integer, minimum, maximum) in numeric_specs.items():
        val = _num(raw.get(key), integer=integer, minimum=minimum, maximum=maximum)
        if val is not None:
            out[key] = val

    for low_key, high_key in (
        ("price_min_billion", "price_max_billion"),
        ("area_min", "area_max"),
        ("build_year_min", "build_year_max"),
    ):
        if low_key in out and high_key in out and out[low_key] > out[high_key]:
            out[low_key], out[high_key] = out[high_key], out[low_key]

    district = str(raw.get("district") or "").strip()
    if district:
        out["district"] = district[:160]

    free_text = str(raw.get("free_text") or "").strip()
    if free_text:
        out["free_text"] = free_text[:240]

    floor_text = str(raw.get("floor_text") or "").strip()
    if floor_text:
        out["floor_text"] = floor_text[:80]

    unit_code = re.sub(r"\D", "", normalize_digits(raw.get("unit_code") or ""))
    if unit_code:
        out["unit_code"] = unit_code[:40]

    created_after = str(raw.get("created_after") or "").strip()
    if created_after:
        try:
            datetime.fromisoformat(created_after.replace("Z", "+00:00"))
            out["created_after"] = created_after[:64]
        except ValueError:
            pass

    for key in ("has_parking", "has_elevator", "has_storage", "deed_required"):
        value = raw.get(key)
        if isinstance(value, bool):
            out[key] = value

    direction = str(raw.get("direction") or "").strip()
    if direction in {"شمالی", "جنوبی", "شرقی", "غربی", "شمالی-جنوبی", "جنوبی-شمالی"}:
        out["direction"] = direction

    return out


def _filters_to_context(filters):
    return normalize_search_filters(filters)


def get_active_search_context(telegram_user_id: int):
    """Return the latest search context, even if push notifications are off.

    Search context and notification subscription are intentionally separate:
    «لغو» stops future pushes but does not make natural follow-ups such as
    «یکم بزرگتر» forget the user's most recent search.
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT filters, query_text
                FROM smart_search_subscriptions
                WHERE telegram_user_id=%s
                ORDER BY id DESC LIMIT 1
                """,
                (telegram_user_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {"filters": dict(row[0] or {}), "query_text": row[1] or ""}


def _extract_response_text(data):
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    return ""


def _parse_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _openai_intent_sync(text: str, *, previous_context=None, pending_visit=False):
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None

    previous_context = previous_context or {}
    instructions = """
You are a strict Persian real-estate intent parser for ShahFile in Tabriz.
Return ONLY one valid JSON object. Never invent a listing, price, address, phone, or database fact.
Your job is only to extract the user's intent and filters from the user's own words.

Allowed intent values:
search_property, visit_request, sell_property, finance_cash, finance_sell, cancel_updates, unknown.

JSON keys, always include all of them:
intent, price_min_billion, price_max_billion, area_min, area_max, bedrooms, district,
build_year_min, build_year_max, has_parking, has_elevator, has_storage,
deed_required, direction,
unit_code, visit_datetime_jalali.
Use null for unspecified fields.

Rules:
- Persian/Arabic/English digits may appear.
- Price numbers such as 5 to 6 in a property-shopping context normally mean billion tomans.
- "70 تا 90 متر" means area_min=70 and area_max=90, not price.
- Extract bedrooms and neighborhood/district when explicitly expressed.
- Boolean amenities and deed_required are true/false only when explicitly requested.
- Extract direction only when the user means the property's orientation, not when it is part of a neighborhood name.
- For follow-up search phrases such as "یکم بزرگتر" or "ارزون‌تر" you may adjust the PREVIOUS SEARCH FILTERS; otherwise a new explicit search replaces the previous filters.
- A visit request must include unit_code when the user gives one.
- Convert natural Persian visit time to a concrete Jalali YYYY/MM/DD HH:MM only when the meaning is clear. Today is supplied below. Never output Gregorian dates.
- "لغو" when used to stop property suggestions means cancel_updates.
- Never include private owner information. You are not given any private owner information.
""".strip()

    user_payload = {
        "today_jalali": _today_jalali(),
        "pending_visit": bool(pending_visit),
        "previous_search": previous_context,
        "customer_message": text,
    }

    payload = {
        "model": os.getenv("OPENAI_MODEL", OPENAI_MODEL),
        "instructions": instructions,
        "input": json.dumps(user_payload, ensure_ascii=False),
        "max_output_tokens": 450,
        "store": False,
    }
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    data = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                body = ""
            logging.warning("OpenAI intent HTTP error status=%s attempt=%s body=%s", exc.code, attempt + 1, body)
            if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                return None
        except Exception as exc:
            logging.warning("OpenAI intent request failed attempt=%s: %s", attempt + 1, exc)
        if attempt == 0:
            import time
            time.sleep(0.8)
    if not data:
        return None

    parsed = _parse_json_object(_extract_response_text(data))
    if not parsed:
        logging.warning("OpenAI intent returned non-JSON output")
        return None

    allowed = {
        "search_property",
        "visit_request",
        "sell_property",
        "finance_cash",
        "finance_sell",
        "cancel_updates",
        "unknown",
    }
    intent = str(parsed.get("intent") or "unknown").strip()
    if intent not in allowed:
        intent = "unknown"

    filters = normalize_search_filters(parsed)
    unit_code = re.sub(r"\D", "", normalize_digits(parsed.get("unit_code") or "")) or None
    visit_dt = normalize_digits(parsed.get("visit_datetime_jalali") or "").strip() or None
    return {
        "intent": intent,
        "filters": filters,
        "unit_code": unit_code,
        "visit_datetime_jalali": visit_dt,
    }


async def extract_ai_intent(text: str, *, telegram_user_id=None, pending_visit=False):
    previous = get_active_search_context(telegram_user_id) if telegram_user_id else None
    return await asyncio.to_thread(
        _openai_intent_sync,
        text,
        previous_context=previous,
        pending_visit=pending_visit,
    )


def _search_where(filters):
    filters = normalize_search_filters(filters)
    conditions = [
        "p.status='ACTIVE'",
        "p.published_message_id IS NOT NULL",
        "p.deleted_from_channel_at IS NULL",
        "NOT EXISTS (SELECT 1 FROM property_trash pt WHERE pt.property_id=p.id)",
    ]
    params = []

    mapping = [
        ("price_min_billion", "p.current_price_billion >= %s"),
        ("price_max_billion", "p.current_price_billion <= %s"),
        ("area_min", "p.area_m2 >= %s"),
        ("area_max", "p.area_m2 <= %s"),
        ("bedrooms", "p.bedrooms = %s"),
        ("build_year_min", "p.build_year >= %s"),
        ("build_year_max", "p.build_year <= %s"),
        ("has_parking", "p.has_parking = %s"),
        ("has_elevator", "p.has_elevator = %s"),
        ("has_storage", "p.has_storage = %s"),
    ]
    for key, clause in mapping:
        if key in filters:
            conditions.append(clause)
            params.append(filters[key])

    if filters.get("district"):
        conditions.append(
            "(COALESCE(p.district,'') ILIKE %s OR COALESCE(p.neighborhood,'') ILIKE %s OR COALESCE(p.address_text,'') ILIKE %s)"
        )
        pattern = f"%{filters['district']}%"
        params.extend([pattern, pattern, pattern])

    if filters.get("direction"):
        conditions.append("COALESCE(p.direction,'') ILIKE %s")
        params.append(f"%{filters['direction']}%")

    if filters.get("deed_required") is True:
        conditions.append(
            "(NULLIF(TRIM(COALESCE(p.deed_text,'')), '') IS NOT NULL OR "
            "EXISTS (SELECT 1 FROM property_attributes pa WHERE pa.property_id=p.id "
            "AND pa.attribute_key IN ('deed','deed_type') "
            "AND NULLIF(TRIM(COALESCE(pa.attribute_value,'')), '') IS NOT NULL))"
        )

    if filters.get("free_text"):
        pattern = f"%{filters['free_text']}%"
        conditions.append(
            "(COALESCE(p.unit_code,'') ILIKE %s OR "
            "COALESCE(p.district,'') ILIKE %s OR "
            "COALESCE(p.neighborhood,'') ILIKE %s OR "
            "COALESCE(p.address_text,'') ILIKE %s OR "
            "COALESCE(p.raw_text,'') ILIKE %s OR "
            "EXISTS (SELECT 1 FROM property_attributes pa "
            "WHERE pa.property_id=p.id AND ("
            "COALESCE(pa.attribute_label,'') ILIKE %s OR "
            "COALESCE(pa.attribute_value,'') ILIKE %s)))"
        )
        params.extend([pattern] * 7)

    if filters.get("floor_text"):
        conditions.append("COALESCE(p.floor_text,'') ILIKE %s")
        params.append(f"%{filters['floor_text']}%")

    if filters.get("unit_code"):
        conditions.append(
            "(p.unit_code=%s OR EXISTS (SELECT 1 FROM property_sources ps "
            "WHERE ps.property_id=p.id AND ps.source_unit_code=%s))"
        )
        params.extend([filters["unit_code"], filters["unit_code"]])

    if filters.get("created_after"):
        conditions.append("p.created_at > %s::timestamptz")
        params.append(filters["created_after"])

    return " AND ".join(conditions), params


_SELECT_FIELDS = """
    p.id,p.unit_code,p.district,p.address_text,p.area_m2,
    p.bedrooms,p.build_year,p.current_price_billion,
    p.channel_post_url,p.published_message_id
"""


def search_count(filters):
    where, params = _search_where(filters)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM properties p WHERE {where}", params)
            return int(cur.fetchone()[0] or 0)


def search_rows(filters, offset=0, limit=PAGE_SIZE):
    where, params = _search_where(filters)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM properties p
                WHERE {where}
                ORDER BY p.current_price_billion ASC NULLS LAST, p.id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, limit, offset],
            )
            return cur.fetchall()


def _search_all_ids(filters):
    where, params = _search_where(filters)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT p.id FROM properties p WHERE {where}", params)
            return [int(r[0]) for r in cur.fetchall()]


def _destination_username():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT username FROM channels
                WHERE channel_type='DESTINATION' AND is_active=TRUE
                ORDER BY id DESC LIMIT 1
                """
            )
            row = cur.fetchone()
    return str(row[0]).lstrip("@") if row and row[0] else None


def _fmt_number(value):
    if value is None:
        return "-"
    number = Decimal(str(value))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _filters_title(filters):
    f = normalize_search_filters(filters)
    parts = []
    pmin, pmax = f.get("price_min_billion"), f.get("price_max_billion")
    if pmin is not None and pmax is not None:
        parts.append(f"💰 {_fmt_number(pmin)} تا {_fmt_number(pmax)} میلیارد")
    elif pmax is not None:
        parts.append(f"💰 تا {_fmt_number(pmax)} میلیارد")
    elif pmin is not None:
        parts.append(f"💰 از {_fmt_number(pmin)} میلیارد")

    amin, amax = f.get("area_min"), f.get("area_max")
    if amin is not None and amax is not None:
        parts.append(f"📐 {_fmt_number(amin)} تا {_fmt_number(amax)} متر")
    elif amax is not None:
        parts.append(f"📐 تا {_fmt_number(amax)} متر")
    elif amin is not None:
        parts.append(f"📐 از {_fmt_number(amin)} متر")

    if "bedrooms" in f:
        parts.append(f"🛏 {f['bedrooms']} خواب")
    if f.get("district"):
        parts.append(f"📍 {f['district']}")
    if f.get("free_text"):
        parts.append(f"🔎 {f['free_text']}")
    if f.get("floor_text"):
        parts.append(f"🏢 طبقه {f['floor_text']}")
    if f.get("unit_code"):
        parts.append(f"🔢 کد {f['unit_code']}")
    if f.get("has_parking") is True:
        parts.append("🚗 پارکینگ")
    if f.get("has_elevator") is True:
        parts.append("🛗 آسانسور")
    if f.get("has_storage") is True:
        parts.append("📦 انباری")
    if f.get("deed_required") is True:
        parts.append("📄 سند")
    if f.get("direction"):
        parts.append(f"🧭 {f['direction']}")
    return " | ".join(parts) if parts else "فایل‌های مطابق درخواست شما"


def property_card_text(row, *, index=None, heading=None):
    pid, unit, district, address, area, beds, year, price, url, msg_id = row
    lines = []
    if heading:
        lines.extend([heading, ""])
    prefix = f"{index}) " if index is not None else ""
    location = address or district or "بدون منطقه"
    lines.append(f"{prefix}📍 {location}")
    details = []
    if area is not None:
        details.append(f"📐 {_fmt_number(area)} متر")
    if beds is not None:
        details.append(f"🛏 {beds} خواب")
    if year:
        details.append(f"📅 سال ساخت {year}")
    if details:
        lines.append(" | ".join(details))
    if price is not None:
        lines.append(f"💰 قیمت: {_fmt_number(price)} میلیارد تومان")
    if unit:
        lines.append(f"🔢 کد واحد: {unit}")
    dest = _destination_username()
    final_url = url
    if dest and msg_id:
        final_url = f"https://t.me/{dest}/{msg_id}"
    if final_url:
        lines.append(f"🔗 مشاهده فایل: {final_url}")
    lines.extend(["", OPT_OUT_TEXT])
    return "\n".join(lines)


def create_search_preview(telegram_user_id, customer_id, filters, query_text):
    """Create a search row for pagination/context, but do NOT enable pushes.

    Any older active watch is disabled immediately. This guarantees that a new
    search range supersedes the old range even before the user answers the
    opt-in question for the new search.
    """
    filters = normalize_search_filters(filters)
    if not filters:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE smart_search_subscriptions
                SET is_active=FALSE, cancelled_at=NOW(), updated_at=NOW()
                WHERE telegram_user_id=%s AND is_active=TRUE
                """,
                (telegram_user_id,),
            )
            cur.execute(
                """
                INSERT INTO smart_search_subscriptions(
                    telegram_user_id, customer_id, query_text, filters,
                    is_active, created_at, updated_at, cancelled_at
                ) VALUES (%s,%s,%s,%s::jsonb,FALSE,NOW(),NOW(),NULL)
                RETURNING id
                """,
                (
                    telegram_user_id,
                    customer_id,
                    query_text,
                    json.dumps(filters, ensure_ascii=False),
                ),
            )
            subscription_id = int(cur.fetchone()[0])
            where, params = _search_where(filters)
            cur.execute(
                f"""
                INSERT INTO smart_search_seen(subscription_id, property_id, seen_at)
                SELECT %s, p.id, NOW()
                FROM properties p
                WHERE {where}
                ON CONFLICT DO NOTHING
                """,
                [subscription_id, *params],
            )
        conn.commit()
    return subscription_id


def activate_search_subscription(subscription_id, telegram_user_id):
    """Explicitly opt the user into future matching-property notifications."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM smart_search_subscriptions
                WHERE id=%s AND telegram_user_id=%s
                FOR UPDATE
                """,
                (subscription_id, telegram_user_id),
            )
            if not cur.fetchone():
                conn.rollback()
                return False
            cur.execute(
                """
                UPDATE smart_search_subscriptions
                SET is_active=FALSE, cancelled_at=NOW(), updated_at=NOW()
                WHERE telegram_user_id=%s AND is_active=TRUE AND id<>%s
                """,
                (telegram_user_id, subscription_id),
            )
            cur.execute(
                """
                UPDATE smart_search_subscriptions
                SET is_active=TRUE, cancelled_at=NULL, updated_at=NOW(), last_scan_at=NOW()
                WHERE id=%s AND telegram_user_id=%s
                """,
                (subscription_id, telegram_user_id),
            )
        conn.commit()
    return True


def decline_search_subscription(subscription_id, telegram_user_id):
    """Leave this search available for pagination/context without future pushes."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE smart_search_subscriptions
                SET is_active=FALSE, cancelled_at=NOW(), updated_at=NOW()
                WHERE id=%s AND telegram_user_id=%s
                RETURNING id
                """,
                (subscription_id, telegram_user_id),
            )
            row = cur.fetchone()
        conn.commit()
    return bool(row)

def cancel_active_subscription(telegram_user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE smart_search_subscriptions
                SET is_active=FALSE, cancelled_at=NOW(), updated_at=NOW()
                WHERE telegram_user_id=%s AND is_active=TRUE
                RETURNING id
                """,
                (telegram_user_id,),
            )
            row = cur.fetchone()
        conn.commit()
    return bool(row)


def get_subscription(subscription_id, telegram_user_id=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            if telegram_user_id is None:
                cur.execute(
                    "SELECT id,telegram_user_id,customer_id,query_text,filters,is_active FROM smart_search_subscriptions WHERE id=%s",
                    (subscription_id,),
                )
            else:
                cur.execute(
                    "SELECT id,telegram_user_id,customer_id,query_text,filters,is_active FROM smart_search_subscriptions WHERE id=%s AND telegram_user_id=%s",
                    (subscription_id, telegram_user_id),
                )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "telegram_user_id": row[1],
        "customer_id": row[2],
        "query_text": row[3],
        "filters": dict(row[4] or {}),
        "is_active": bool(row[5]),
    }


def search_page(subscription_id, telegram_user_id, offset=0):
    sub = get_subscription(subscription_id, telegram_user_id)
    if not sub:
        return None, None, 0
    filters = normalize_search_filters(sub["filters"])
    total = search_count(filters)
    rows = search_rows(filters, offset=offset, limit=PAGE_SIZE)
    lines = ["🔎 " + _filters_title(filters), f"تعداد فایل: {total}", ""]
    if not rows:
        lines.extend([
            "فعلاً فایل فعالی مطابق این درخواست پیدا نکردم.",
            "",
            "برای بازدید کافی است کد واحد را همراه کلمه «بازدید» بفرستید.",
        ])
    else:
        for index, row in enumerate(rows, start=offset + 1):
            lines.append(property_card_text(row, index=index))
            lines.extend(["", "──────────────", ""])
        lines.append("برای بازدید کافی است کد واحد را همراه کلمه «بازدید» بفرستید.")

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"sc_s_{subscription_id}_{max(0, offset-PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"sc_s_{subscription_id}_{offset+PAGE_SIZE}"))
    keyboard_rows = []
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.append([InlineKeyboardButton("🏠 شروع دوباره", callback_data="sc_home")])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(keyboard_rows), total


def prepare_search_and_render(telegram_user_id, customer_id, filters, query_text):
    sub_id = create_search_preview(telegram_user_id, customer_id, filters, query_text)
    if not sub_id:
        return None, None, 0, None
    text, keyboard, total = search_page(sub_id, telegram_user_id, 0)
    return text, keyboard, total, sub_id


# Backward-compatible alias for external callers; unlike V1.1 this no longer
# silently opts a customer into notifications.
def subscribe_and_render(telegram_user_id, customer_id, filters, query_text):
    return prepare_search_and_render(telegram_user_id, customer_id, filters, query_text)


def _active_subscriptions(limit=200):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,telegram_user_id,filters
                FROM smart_search_subscriptions
                WHERE is_active=TRUE
                ORDER BY COALESCE(last_scan_at, created_at), id
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [(r[0], r[1], dict(r[2] or {})) for r in rows]


def _unseen_rows(subscription_id, filters, limit=10):
    where, params = _search_where(filters)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM properties p
                WHERE {where}
                  AND NOT EXISTS (
                      SELECT 1 FROM smart_search_seen ss
                      WHERE ss.subscription_id=%s AND ss.property_id=p.id
                  )
                ORDER BY COALESCE(p.published_at,p.created_at), p.id
                LIMIT %s
                """,
                [*params, subscription_id, limit],
            )
            return cur.fetchall()


def _mark_seen(subscription_id, property_id, *, notified=False):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smart_search_seen(
                    subscription_id,property_id,seen_at,notification_sent_at
                ) VALUES (%s,%s,NOW(),CASE WHEN %s THEN NOW() ELSE NULL END)
                ON CONFLICT (subscription_id,property_id)
                DO UPDATE SET
                    seen_at=EXCLUDED.seen_at,
                    notification_sent_at=CASE
                        WHEN EXCLUDED.notification_sent_at IS NOT NULL
                        THEN EXCLUDED.notification_sent_at
                        ELSE smart_search_seen.notification_sent_at
                    END
                """,
                (subscription_id, property_id, notified),
            )
            cur.execute(
                "UPDATE smart_search_subscriptions SET last_scan_at=NOW(), updated_at=updated_at WHERE id=%s",
                (subscription_id,),
            )
        conn.commit()


def _touch_scan(subscription_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE smart_search_subscriptions SET last_scan_at=NOW() WHERE id=%s", (subscription_id,))
        conn.commit()


async def run_search_watch_once(application):
    for subscription_id, telegram_user_id, filters in _active_subscriptions():
        rows = _unseen_rows(subscription_id, filters, limit=10)
        if not rows:
            _touch_scan(subscription_id)
            continue
        for row in rows:
            # Recheck that the subscription is still active. A newer search or «لغو» wins immediately.
            sub = get_subscription(subscription_id, telegram_user_id)
            if not sub or not sub["is_active"]:
                break
            try:
                await application.bot.send_message(
                    chat_id=telegram_user_id,
                    text=property_card_text(row, heading="🆕 فایل جدید مطابق درخواست شما"),
                    disable_web_page_preview=True,
                )
                _mark_seen(subscription_id, int(row[0]), notified=True)
            except Exception:
                logging.exception(
                    "Smart search notification failed subscription=%s user=%s property=%s",
                    subscription_id,
                    telegram_user_id,
                    row[0],
                )
                # Do not mark as seen so a transient Telegram failure can retry later.
                break


async def _search_watch_loop(application):
    logging.info("ShahFile smart search watch worker started: interval=%ss", WATCH_INTERVAL_SECONDS)
    while True:
        try:
            await run_search_watch_once(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("ShahFile smart search watch worker error")
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)


def start_search_watch_worker(application):
    existing = application.bot_data.get("shahfile_search_watch_task")
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_search_watch_loop(application), name="shahfile_search_watch")
    application.bot_data["shahfile_search_watch_task"] = task
    return task


# Backward-compatible pure helper for installer smoke tests.
def smoke_normalize_ai_result(raw):
    raw = dict(raw or {})
    return {
        "intent": raw.get("intent") or "unknown",
        "filters": normalize_search_filters(raw),
        "unit_code": re.sub(r"\D", "", normalize_digits(raw.get("unit_code") or "")) or None,
        "visit_datetime_jalali": normalize_digits(raw.get("visit_datetime_jalali") or "").strip() or None,
    }
