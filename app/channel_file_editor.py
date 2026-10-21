import hashlib
import json
import logging
import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from property_parser import make_price_hashtag
from publisher import (
    build_publish_text,
    get_destination_channel,
)
from telegram_delivery import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    edit_published_message,
    limit_telegram_text,
)

import os

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")

PERMISSION_KEY = "channel_post_edit"
STATE_LOOKUP = "channel_edit_lookup"
STATE_VALUE = "channel_edit_value"

CORE_FIELDS = {
    "district": "📍 منطقه",
    "address_text": "🗺 آدرس عمومی",
    "area_m2": "📐 متراژ",
    "bedrooms": "🛏 تعداد خواب",
    "floor_text": "🏢 طبقه",
    "build_year": "📅 سال ساخت",
    "current_price_toman": "💰 قیمت کل",
    "has_parking": "🚗 پارکینگ",
    "has_elevator": "🛗 آسانسور",
    "has_storage": "📦 انباری",
    "direction": "🧭 جهت ملک",
    "sale_mode": "🏗 نوع عرضه",
    "details": "📝 جزئیات",
}

BOOL_FIELDS = {
    "has_parking": "پارکینگ",
    "has_elevator": "آسانسور",
    "has_storage": "انباری",
}

PRIVATE_MARKERS = (
    "شماره مالک",
    "شماره تماس مالک",
    "تلفن مالک",
    "مالک:",
    "مالک :",
    "آدرس دقیق",
    "لوکیشن دقیق",
    "نشانی دقیق",
)


def db_connect():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def ensure_channel_file_editor_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_file_edit_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    property_id BIGINT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                    actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    actor_telegram_user_id BIGINT,
                    status VARCHAR(40) NOT NULL DEFAULT 'PENDING',
                    changes JSONB NOT NULL DEFAULT '{}'::jsonb,
                    before_snapshot JSONB,
                    after_snapshot JSONB,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    applied_at TIMESTAMPTZ,
                    CONSTRAINT channel_file_edit_jobs_status_check CHECK (
                        status IN (
                            'PENDING',
                            'APPLYING',
                            'APPLIED',
                            'FAILED',
                            'FAILED_COMPENSATION'
                        )
                    )
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_channel_file_edit_jobs_property
                ON channel_file_edit_jobs(property_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_channel_file_edit_jobs_actor
                ON channel_file_edit_jobs(actor_user_id, created_at DESC)
                """
            )
        conn.commit()


def _digit_normalize(value):
    return str(value or "").translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )


def _clean_numeric_text(value):
    text = _digit_normalize(value).strip().lower()
    text = text.replace("٬", "").replace(",", "")
    text = text.replace("٫", ".")
    text = re.sub(r"\s+", " ", text)
    return text


def parse_price_toman(value):
    text = _clean_numeric_text(value)
    if not text:
        raise ValueError("قیمت خالی است")

    multiplier = None
    if "میلیارد" in text:
        multiplier = 1_000_000_000
        text = text.replace("میلیارد", "")
    elif "میلیون" in text:
        multiplier = 1_000_000
        text = text.replace("میلیون", "")
    else:
        text = text.replace("تومان", "")

    text = text.replace("/", ".")
    text = re.sub(r"[^0-9.]", "", text)
    if text.count(".") > 1 or not text:
        raise ValueError("فرمت قیمت نامعتبر است")

    number = float(text)
    if number <= 0:
        raise ValueError("قیمت باید بزرگ‌تر از صفر باشد")

    if multiplier is None:
        multiplier = 1_000_000_000 if number < 10_000 else 1

    result = int(round(number * multiplier))
    if result < 10_000_000:
        raise ValueError("قیمت واردشده غیرعادی است")
    return result


def _parse_decimal(value, *, min_value=0, max_value=None):
    text = _clean_numeric_text(value).replace("/", ".")
    text = re.sub(r"[^0-9.]", "", text)
    if not text or text.count(".") > 1:
        raise ValueError("عدد معتبر وارد کنید")
    number = Decimal(text)
    if number < Decimal(str(min_value)):
        raise ValueError("عدد واردشده خارج از محدوده است")
    if max_value is not None and number > Decimal(str(max_value)):
        raise ValueError("عدد واردشده خارج از محدوده است")
    return number


def _parse_int(value, *, min_value=0, max_value=None):
    number = _parse_decimal(value, min_value=min_value, max_value=max_value)
    if number != number.to_integral_value():
        raise ValueError("عدد باید صحیح باشد")
    return int(number)


def _contains_private_data(value):
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in PRIVATE_MARKERS):
        return True
    if re.search(r"(?:^|\D)0?9\d{9}(?:\D|$)", _digit_normalize(text)):
        return True
    if re.search(r"(?:https?://)?(?:www\.)?t\.me/", lowered):
        return True
    if re.search(r"(?<!\w)@[A-Za-z0-9_]{4,}", text):
        return True
    return False


def parse_field_value(field_key, value):
    raw = str(value or "").strip()
    if field_key in {"district", "address_text", "floor_text", "direction"}:
        if raw in {"-", "—", "حذف", "پاک"}:
            return None
        if _contains_private_data(raw):
            raise ValueError("این متن شامل اطلاعات خصوصی/تماس است و قابل انتشار نیست")
        if not raw:
            raise ValueError("مقدار خالی است؛ برای حذف «-» بفرستید")
        return raw[:255] if field_key != "address_text" else raw[:1000]

    if field_key == "area_m2":
        return _parse_decimal(raw, min_value=1, max_value=100000)
    if field_key == "bedrooms":
        return _parse_int(raw, min_value=0, max_value=30)
    if field_key == "build_year":
        if raw in {"-", "—", "حذف", "پاک"}:
            return None
        return _parse_int(raw, min_value=1000, max_value=2200)
    if field_key == "current_price_toman":
        return parse_price_toman(raw)
    if field_key.startswith("attr:") or field_key == "details":
        if raw in {"-", "—", "حذف", "پاک"}:
            return "" if field_key == "details" else None
        if _contains_private_data(raw):
            raise ValueError("این متن شامل اطلاعات خصوصی/تماس است و قابل انتشار نیست")
        return raw
    raise ValueError("فیلد ناشناخته است")


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _property_row_to_dict(row):
    keys = (
        "id", "unit_code", "district", "address_text", "area_m2",
        "bedrooms", "floor_text", "build_year", "has_parking",
        "has_elevator", "has_storage", "direction", "current_price_toman",
        "current_price_billion", "price_hashtag", "raw_text", "status",
        "published_message_id", "channel_post_url", "deleted_from_channel_at",
        "updated_at", "manual_price_toman",
    )
    return dict(zip(keys, row))


def _publish_row_from_property(p):
    return (
        p["id"], p["unit_code"], p["district"], p["address_text"],
        p["area_m2"], p["bedrooms"], p["floor_text"], p["build_year"],
        p["has_parking"], p["has_elevator"], p["has_storage"],
        p["direction"], p["current_price_toman"], p["current_price_billion"],
        p["price_hashtag"], p["raw_text"], p["status"],
    )


def _attrs_for_publish(attrs):
    return [
        (
            a["attribute_key"], a["attribute_label"], a["attribute_value"],
            a["display_order"],
        )
        for a in attrs
        if a.get("is_visible", True) and str(a.get("attribute_value") or "").strip()
    ]


def _load_snapshot(property_id, conn=None, *, for_update=False):
    owns = conn is None
    if owns:
        conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    id, unit_code, district, address_text, area_m2, bedrooms,
                    floor_text, build_year, has_parking, has_elevator,
                    has_storage, direction, current_price_toman,
                    current_price_billion, price_hashtag, raw_text, status,
                    published_message_id, channel_post_url,
                    deleted_from_channel_at, updated_at, manual_price_toman
                FROM properties
                WHERE id = %s
                {"FOR UPDATE" if for_update else ""}
                """,
                (property_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            prop = _property_row_to_dict(row)

            cur.execute(
                """
                SELECT id, attribute_key, attribute_label, attribute_value,
                       display_order, is_visible
                FROM property_attributes
                WHERE property_id = %s
                ORDER BY display_order, id
                """,
                (property_id,),
            )
            attrs = []
            for attr_row in cur.fetchall():
                attrs.append({
                    "id": attr_row[0],
                    "attribute_key": attr_row[1],
                    "attribute_label": attr_row[2],
                    "attribute_value": attr_row[3],
                    "display_order": attr_row[4],
                    "is_visible": attr_row[5],
                })

            cur.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM property_media
                    WHERE property_id = %s AND media_type = 'PHOTO'
                )
                """,
                (property_id,),
            )
            has_photo = bool(cur.fetchone()[0])

            return {"property": prop, "attributes": attrs, "has_photo": has_photo}
    finally:
        if owns:
            conn.close()


def _is_channel_editable(snapshot):
    if not snapshot:
        return False
    p = snapshot["property"]
    return bool(
        p.get("published_message_id")
        and p.get("deleted_from_channel_at") is None
        and p.get("status") in {"ACTIVE", "SOLD"}
    )


def find_published_properties(value, limit=10):
    lookup = str(value or "").strip()
    if not lookup:
        return []

    normalized = _digit_normalize(lookup)
    params = []
    clauses = []

    queue_match = re.fullmatch(r"\s*صف\s*[:：#-]?\s*(\d+)\s*", normalized, re.I)
    id_match = re.fullmatch(r"\s*(?:id|آیدی|شناسه)\s*[:：#-]?\s*(\d+)\s*", normalized, re.I)
    link_match = re.search(
        r"(?:https?://)?(?:www\.)?t\.me/(?:(?:c/(\d+))|([A-Za-z0-9_]+))/(\d+)",
        normalized,
        re.I,
    )
    code_match = re.fullmatch(
        r"\s*(?:کد\s*(?:واحد|فایل|ملک|آگهی)\s*[:：#-]?\s*)?(\d+)\s*",
        normalized,
        re.I,
    )

    if queue_match:
        n = int(queue_match.group(1))
        # Historical queue-tracking semantics are property_id first.
        # Only if no published property has that internal id do we fall
        # back to the physical publish_queue row id.
        with db_connect() as conn:
            with conn.cursor() as cur:
                for where_sql in ("p.id = %s", "q.id = %s"):
                    cur.execute(
                        f"""
                        SELECT DISTINCT
                            p.id, p.unit_code, p.district, p.address_text,
                            p.area_m2, p.current_price_billion,
                            p.published_message_id, p.channel_post_url, p.status
                        FROM properties p
                        LEFT JOIN publish_queue q ON q.property_id = p.id
                        WHERE p.published_message_id IS NOT NULL
                          AND p.deleted_from_channel_at IS NULL
                          AND p.status IN ('ACTIVE', 'SOLD')
                          AND ({where_sql})
                        ORDER BY p.id DESC
                        LIMIT %s
                        """,
                        (n, int(limit)),
                    )
                    rows = cur.fetchall()
                    if rows:
                        return rows
        return []
    elif id_match:
        clauses.append("p.id = %s")
        params.append(int(id_match.group(1)))
    elif link_match:
        message_id = int(link_match.group(3))
        clean_url = re.sub(r"[?#].*$", "", normalized).rstrip("/")
        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url
        clauses.extend([
            "p.channel_post_url = %s",
            "p.published_message_id = %s",
            "ps.source_post_url = %s",
            "ps.source_message_id = %s",
        ])
        params.extend([clean_url, message_id, clean_url, message_id])
    elif code_match:
        code = code_match.group(1)
        n = int(code)
        clauses.extend([
            "p.unit_code = %s",
            "ps.source_unit_code = %s",
            "p.id = %s",
            "p.published_message_id = %s",
            "ps.source_message_id = %s",
        ])
        params.extend([code, code, n, n, n])
    else:
        clauses.extend([
            "p.unit_code ILIKE %s",
            "COALESCE(p.district, '') ILIKE %s",
            "COALESCE(p.address_text, '') ILIKE %s",
        ])
        like = f"%{lookup}%"
        params.extend([like, like, like])

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT
                    p.id, p.unit_code, p.district, p.address_text,
                    p.area_m2, p.current_price_billion,
                    p.published_message_id, p.channel_post_url, p.status
                FROM properties p
                LEFT JOIN property_sources ps ON ps.property_id = p.id
                LEFT JOIN publish_queue q ON q.property_id = p.id
                WHERE p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.status IN ('ACTIVE', 'SOLD')
                  AND ({' OR '.join(f'({c})' for c in clauses)})
                ORDER BY p.id DESC
                LIMIT %s
                """,
                [*params, int(limit)],
            )
            return cur.fetchall()


def _money_display(value):
    if value is None:
        return "نامشخص"
    billion = Decimal(str(value)) / Decimal("1000000000")
    text = f"{billion:.3f}".rstrip("0").rstrip(".")
    return f"{text} میلیارد تومان"


def _bool_display(value):
    if value is True:
        return "دارد"
    if value is False:
        return "ندارد"
    return "نامشخص"


def _value_display(field_key, value):
    if field_key == "current_price_toman":
        return _money_display(value)
    if field_key in BOOL_FIELDS:
        return _bool_display(value)
    if value is None or value == "":
        return "—"
    return str(value)


def _summary(snapshot):
    p = snapshot["property"]
    location = p.get("address_text") or p.get("district") or "نامشخص"
    area = p.get("area_m2")
    area_text = f"{area:g}" if isinstance(area, Decimal) else str(area or "نامشخص")
    return (
        "✏️ ویرایش فایل کانال\n\n"
        f"کد واحد: {p.get('unit_code') or '—'}\n"
        f"شناسه داخلی: {p.get('id')}\n"
        f"📍 {location}\n"
        f"📐 {area_text} متر\n"
        f"💰 {_money_display(p.get('current_price_toman'))}\n"
        f"🔗 {p.get('channel_post_url') or '—'}"
    )


def _lookup_prompt():
    return (
        "✏️ ویرایش فایل منتشرشده\n\n"
        "یکی از این موارد را بفرستید:\n"
        "• کد واحد\n"
        "• لینک پست کانال\n"
        "• لینک/کد فایل منبع\n"
        "• شناسه داخلی مثل: ID 230\n"
        "• شماره پیگیری مثل: صف 45"
    )


def _editor_keyboard(snapshot, draft=None):
    p = snapshot["property"]
    rows = [
        [InlineKeyboardButton("📍 منطقه", callback_data="channel_edit_field_district"),
         InlineKeyboardButton("🗺 آدرس عمومی", callback_data="channel_edit_field_address_text")],
        [InlineKeyboardButton("📐 متراژ", callback_data="channel_edit_field_area_m2"),
         InlineKeyboardButton("🛏 خواب", callback_data="channel_edit_field_bedrooms")],
        [InlineKeyboardButton("🏢 طبقه", callback_data="channel_edit_field_floor_text"),
         InlineKeyboardButton("📅 سال ساخت", callback_data="channel_edit_field_build_year")],
        [InlineKeyboardButton("💰 قیمت", callback_data="channel_edit_field_current_price_toman"),
         InlineKeyboardButton("🧭 جهت", callback_data="channel_edit_field_direction")],
        [InlineKeyboardButton("🚗 پارکینگ", callback_data="channel_edit_field_has_parking"),
         InlineKeyboardButton("🛗 آسانسور", callback_data="channel_edit_field_has_elevator")],
        [InlineKeyboardButton("📦 انباری", callback_data="channel_edit_field_has_storage"),
         InlineKeyboardButton("🏗 نوع عرضه", callback_data="channel_edit_field_sale_mode")],
        [InlineKeyboardButton("📝 جزئیات", callback_data="channel_edit_field_details")],
    ]

    dynamic = [
        a for a in snapshot["attributes"]
        if a.get("is_visible", True)
        and not str(a["attribute_key"]).startswith("source_detail_")
        and a["attribute_key"] != "sale_mode"
    ]
    for a in dynamic[:12]:
        label = str(a.get("attribute_label") or a.get("attribute_key") or "ویژگی")[:30]
        rows.append([
            InlineKeyboardButton(
                f"🔹 {label}",
                callback_data=f"channel_edit_attr_{a['id']}",
            )
        ])

    if draft:
        rows.append([
            InlineKeyboardButton("👁 پیش‌نمایش تغییرات", callback_data="channel_edit_preview")
        ])

    rows.extend([
        [InlineKeyboardButton("🔎 انتخاب فایل دیگر", callback_data="channel_edit")],
        [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
    ])
    return InlineKeyboardMarkup(rows)


def _select_keyboard(rows):
    buttons = []
    for row in rows:
        property_id, unit_code, district, address_text, area, price_b, message_id, url, status = row
        loc = address_text or district or "نامشخص"
        area_text = f"{area:g}" if isinstance(area, Decimal) else str(area or "؟")
        price_text = f"{price_b:g}" if isinstance(price_b, Decimal) else str(price_b or "؟")
        buttons.append([
            InlineKeyboardButton(
                f"{unit_code or 'بدون کد'} | {loc[:22]} | {area_text}م | {price_text}م",
                callback_data=f"channel_edit_pick_{property_id}",
            )
        ])
    buttons.append([InlineKeyboardButton("❌ انصراف", callback_data="home")])
    return InlineKeyboardMarkup(buttons)


def _boolean_keyboard(field_key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ دارد", callback_data=f"channel_edit_bool_{field_key}_yes"),
         InlineKeyboardButton("❌ ندارد", callback_data=f"channel_edit_bool_{field_key}_no")],
        [InlineKeyboardButton("➖ نامشخص/پاک", callback_data=f"channel_edit_bool_{field_key}_clear")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="channel_edit_back_fields")],
    ])


def _sale_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 فروش", callback_data="channel_edit_sale_SALE"),
         InlineKeyboardButton("🏗 پیش‌فروش", callback_data="channel_edit_sale_PRE_SALE")],
        [InlineKeyboardButton("➖ حذف نوع عرضه", callback_data="channel_edit_sale_CLEAR")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="channel_edit_back_fields")],
    ])


def _draft_label(field_key, snapshot):
    if field_key.startswith("attr:"):
        attr_id = int(field_key.split(":", 1)[1])
        for a in snapshot["attributes"]:
            if int(a["id"]) == attr_id:
                return f"ویژگی: {a['attribute_label']}"
        return "ویژگی پویا"
    return CORE_FIELDS.get(field_key, field_key)


def _current_value(field_key, snapshot):
    p = snapshot["property"]
    if field_key in p:
        return p.get(field_key)
    if field_key == "sale_mode":
        for a in snapshot["attributes"]:
            if a["attribute_key"] == "sale_mode":
                return a["attribute_value"]
        return None
    if field_key == "details":
        vals = [
            a["attribute_value"] for a in snapshot["attributes"]
            if str(a["attribute_key"]).startswith("source_detail_")
            and str(a.get("attribute_value") or "").strip()
        ]
        return "\n".join(vals)
    if field_key.startswith("attr:"):
        attr_id = int(field_key.split(":", 1)[1])
        for a in snapshot["attributes"]:
            if int(a["id"]) == attr_id:
                return a["attribute_value"]
    return None


def _apply_draft_to_snapshot(snapshot, draft):
    new_snapshot = deepcopy(snapshot)
    p = new_snapshot["property"]
    attrs = new_snapshot["attributes"]

    old_district = p.get("district")
    district_changed = "district" in draft
    address_explicit = "address_text" in draft

    for key, value in draft.items():
        if key in p:
            p[key] = value
        elif key == "sale_mode":
            attrs[:] = [a for a in attrs if a["attribute_key"] != "sale_mode"]
            if value:
                attrs.append({
                    "id": None,
                    "attribute_key": "sale_mode",
                    "attribute_label": "نوع عرضه",
                    "attribute_value": value,
                    "display_order": 33,
                    "is_visible": True,
                })
        elif key == "details":
            attrs[:] = [a for a in attrs if not str(a["attribute_key"]).startswith("source_detail_")]
            lines = [x.strip() for x in str(value or "").splitlines() if x.strip()]
            for index, line in enumerate(lines[:25]):
                h = hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]
                attrs.append({
                    "id": None,
                    "attribute_key": f"source_detail_{h}",
                    "attribute_label": "جزئیات",
                    "attribute_value": line[:220],
                    "display_order": 80 + index,
                    "is_visible": True,
                })
        elif key.startswith("attr:"):
            attr_id = int(key.split(":", 1)[1])
            for a in attrs:
                if a.get("id") is not None and int(a["id"]) == attr_id:
                    a["attribute_value"] = value
                    break

    if district_changed and not address_explicit:
        address = p.get("address_text")
        if not address or str(address).strip() == str(old_district or "").strip():
            p["address_text"] = p.get("district")

    if "current_price_toman" in draft:
        price = int(p["current_price_toman"])
        p["current_price_billion"] = Decimal(price) / Decimal("1000000000")
        p["price_hashtag"] = make_price_hashtag(p["current_price_billion"])
        p["manual_price_toman"] = price

    return new_snapshot


def _changes_for_preview(snapshot, draft):
    new_snapshot = _apply_draft_to_snapshot(snapshot, draft)
    lines = ["🔍 پیش‌نمایش تغییرات", ""]
    changes = []
    for key, value in draft.items():
        old_value = _current_value(key, snapshot)
        new_value = _current_value(key, new_snapshot)
        label = _draft_label(key, snapshot)
        lines.extend([
            f"{label}",
            f"قبل: {_value_display(key, old_value)}",
            f"بعد: {_value_display(key, new_value)}",
            "",
        ])
        changes.append({"field": key, "label": label, "old": old_value, "new": new_value})

    row = _publish_row_from_property(new_snapshot["property"])
    rendered = build_publish_text(
        row,
        unit_code=new_snapshot["property"].get("unit_code"),
        dynamic_attributes_override=_attrs_for_publish(new_snapshot["attributes"]),
        has_photo_override=new_snapshot["has_photo"],
    )
    exact_rendered = limit_telegram_text(
        rendered,
        CAPTION_LIMIT if new_snapshot["has_photo"] else MESSAGE_LIMIT,
    )
    lines.extend(["———", "👁 پیش‌نمایش پست بعد از ویرایش:", "", exact_rendered])
    return "\n".join(lines), changes, new_snapshot, exact_rendered


def _confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید و اعمال", callback_data="channel_edit_confirm")],
        [InlineKeyboardButton("✏️ ویرایش مورد دیگر", callback_data="channel_edit_back_fields")],
        [InlineKeyboardButton("❌ لغو همه تغییرات", callback_data="channel_edit_cancel")],
    ])


def _create_job(property_id, session, telegram_user_id, draft, before_snapshot):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channel_file_edit_jobs
                    (property_id, actor_user_id, actor_telegram_user_id,
                     status, changes, before_snapshot, attempts)
                VALUES (%s, %s, %s, 'PENDING', %s::jsonb, %s::jsonb, 0)
                RETURNING id
                """,
                (
                    property_id,
                    session[0],
                    telegram_user_id,
                    json.dumps(_json_safe(draft), ensure_ascii=False),
                    json.dumps(_json_safe(before_snapshot), ensure_ascii=False),
                ),
            )
            job_id = cur.fetchone()[0]
        conn.commit()
    return job_id


def _update_job(job_id, status, *, error=None, after_snapshot=None, increment_attempt=True):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE channel_file_edit_jobs
                SET status = %s,
                    error_text = %s,
                    after_snapshot = COALESCE(%s::jsonb, after_snapshot),
                    attempts = attempts + CASE WHEN %s THEN 1 ELSE 0 END,
                    applied_at = CASE WHEN %s = 'APPLIED' THEN NOW() ELSE applied_at END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    status,
                    str(error)[:4000] if error else None,
                    json.dumps(_json_safe(after_snapshot), ensure_ascii=False) if after_snapshot is not None else None,
                    bool(increment_attempt),
                    status,
                    job_id,
                ),
            )
        conn.commit()


def _audit_applied(session, property_id, job_id, changes, channel_post_url):
    details = {
        "property_id": property_id,
        "job_id": job_id,
        "channel_post_url": channel_post_url,
        "changes": _json_safe(changes),
    }
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_audit_log
                    (actor_user_id, action, details)
                VALUES (%s, 'CHANNEL_FILE_EDIT_APPLIED', %s::jsonb)
                """,
                (session[0], json.dumps(details, ensure_ascii=False)),
            )
        conn.commit()


def _execute_db_changes(cur, property_id, old_snapshot, new_snapshot, draft):
    old_p = old_snapshot["property"]
    new_p = new_snapshot["property"]

    column_keys = [
        "district", "address_text", "area_m2", "bedrooms", "floor_text",
        "build_year", "has_parking", "has_elevator", "has_storage", "direction",
    ]
    set_parts = []
    values = []
    for key in column_keys:
        if old_p.get(key) != new_p.get(key):
            set_parts.append(f"{key} = %s")
            values.append(new_p.get(key))

    if "current_price_toman" in draft:
        set_parts.extend([
            "current_price_toman = %s",
            "current_price_billion = %s",
            "price_hashtag = %s",
            "manual_price_toman = %s",
            "manual_price_updated_at = NOW()",
        ])
        values.extend([
            int(new_p["current_price_toman"]),
            new_p["current_price_billion"],
            new_p["price_hashtag"],
            int(new_p["current_price_toman"]),
        ])

    if set_parts:
        set_parts.append("updated_at = NOW()")
        cur.execute(
            f"UPDATE properties SET {', '.join(set_parts)} WHERE id = %s",
            [*values, property_id],
        )

    if "sale_mode" in draft:
        value = draft["sale_mode"]
        if value:
            cur.execute(
                """
                INSERT INTO property_attributes
                    (property_id, attribute_key, attribute_label, attribute_value,
                     display_order, is_visible, updated_at)
                VALUES (%s, 'sale_mode', 'نوع عرضه', %s, 33, TRUE, NOW())
                ON CONFLICT (property_id, attribute_key)
                DO UPDATE SET attribute_label = EXCLUDED.attribute_label,
                              attribute_value = EXCLUDED.attribute_value,
                              display_order = EXCLUDED.display_order,
                              is_visible = TRUE,
                              updated_at = NOW()
                """,
                (property_id, value),
            )
        else:
            cur.execute(
                "DELETE FROM property_attributes WHERE property_id = %s AND attribute_key = 'sale_mode'",
                (property_id,),
            )

    if "details" in draft:
        cur.execute(
            "DELETE FROM property_attributes WHERE property_id = %s AND starts_with(attribute_key, %s)",
            (property_id, "source_detail_"),
        )
        lines = [x.strip() for x in str(draft["details"] or "").splitlines() if x.strip()]
        for index, line in enumerate(lines[:25]):
            h = hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]
            cur.execute(
                """
                INSERT INTO property_attributes
                    (property_id, attribute_key, attribute_label, attribute_value,
                     display_order, is_visible, updated_at)
                VALUES (%s, %s, 'جزئیات', %s, %s, TRUE, NOW())
                ON CONFLICT (property_id, attribute_key)
                DO UPDATE SET attribute_value = EXCLUDED.attribute_value,
                              display_order = EXCLUDED.display_order,
                              is_visible = TRUE,
                              updated_at = NOW()
                """,
                (property_id, f"source_detail_{h}", line[:220], 80 + index),
            )

    for key, value in draft.items():
        if not key.startswith("attr:"):
            continue
        attr_id = int(key.split(":", 1)[1])
        cur.execute(
            """
            UPDATE property_attributes
            SET attribute_value = %s, updated_at = NOW()
            WHERE id = %s AND property_id = %s
            """,
            (value, attr_id, property_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"ویژگی پویا {attr_id} دیگر وجود ندارد")


def _same_version(a, b):
    av = a["property"].get("updated_at")
    bv = b["property"].get("updated_at")
    if av is None or bv is None:
        return True
    return av == bv


async def apply_edit_job(bot, *, session, telegram_user_id, property_id, draft, lookup_snapshot, sync_callback=None):
    if not draft:
        raise ValueError("هیچ تغییری برای اعمال وجود ندارد")

    job_id = _create_job(property_id, session, telegram_user_id, draft, lookup_snapshot)
    _update_job(job_id, "APPLYING", increment_attempt=True)

    destination = get_destination_channel()
    if not destination:
        _update_job(job_id, "FAILED", error="DESTINATION channel not configured", increment_attempt=False)
        raise RuntimeError("کانال مقصد فعال پیدا نشد")
    channel_id = destination[0]

    conn = db_connect()
    telegram_changed = False
    old_text = None
    message_id = None
    has_photo = False
    old_price = None
    old_regions = []
    new_snapshot = None
    changes = []

    try:
        with conn.transaction():
            current = _load_snapshot(property_id, conn, for_update=True)
            if not _is_channel_editable(current):
                raise RuntimeError("این فایل دیگر روی کانال قابل ویرایش نیست")
            if not _same_version(lookup_snapshot, current):
                raise RuntimeError("فایل بعد از باز شدن ویرایش تغییر کرده؛ دوباره آن را باز کنید")

            preview_text, changes, new_snapshot, new_text = _changes_for_preview(current, draft)
            old_text = build_publish_text(
                _publish_row_from_property(current["property"]),
                unit_code=current["property"].get("unit_code"),
                dynamic_attributes_override=_attrs_for_publish(current["attributes"]),
                has_photo_override=current["has_photo"],
            )
            old_text = limit_telegram_text(
                old_text,
                CAPTION_LIMIT if current["has_photo"] else MESSAGE_LIMIT,
            )

            message_id = current["property"]["published_message_id"]
            has_photo = current["has_photo"]
            old_price = current["property"].get("current_price_toman")
            old_regions = [
                current["property"].get("district"),
                current["property"].get("address_text"),
            ]

            _execute_db_changes(conn.cursor(), property_id, current, new_snapshot, draft)

            # Telegram is edited while the DB transaction is still open.
            # If Telegram fails, the DB transaction is rolled back automatically.
            await edit_published_message(
                bot,
                chat_id=channel_id,
                message_id=message_id,
                text=new_text,
                has_photo=has_photo,
                attempts=5,
            )
            telegram_changed = True

        # Transaction committed here.

    except Exception as exc:
        compensation_ok = True
        if telegram_changed and old_text and message_id:
            try:
                await edit_published_message(
                    bot,
                    chat_id=channel_id,
                    message_id=message_id,
                    text=old_text,
                    has_photo=has_photo,
                    attempts=5,
                )
            except Exception:
                compensation_ok = False
                logging.exception(
                    "CHANNEL_FILE_EDIT compensation failed: job_id=%s property_id=%s",
                    job_id,
                    property_id,
                )

        _update_job(
            job_id,
            "FAILED" if compensation_ok else "FAILED_COMPENSATION",
            error=exc,
            increment_attempt=False,
        )
        raise
    finally:
        conn.close()

    applied_snapshot = _load_snapshot(property_id)
    _update_job(
        job_id,
        "APPLIED",
        after_snapshot=applied_snapshot,
        increment_attempt=False,
    )
    _audit_applied(
        session,
        property_id,
        job_id,
        changes,
        applied_snapshot["property"].get("channel_post_url") if applied_snapshot else None,
    )

    sync_error = None
    category_sensitive = any(
        key in {"current_price_toman", "district", "address_text"}
        for key in draft
    )
    if sync_callback is not None and category_sensitive:
        try:
            new_price = applied_snapshot["property"].get("current_price_toman") if applied_snapshot else None
            new_regions = [
                applied_snapshot["property"].get("district") if applied_snapshot else None,
                applied_snapshot["property"].get("address_text") if applied_snapshot else None,
            ]
            await sync_callback(
                bot,
                property_id,
                old_price,
                new_price,
                extra_region_names=[x for x in [*old_regions, *new_regions] if x],
            )
        except Exception as exc:
            sync_error = str(exc)
            logging.exception(
                "CHANNEL_FILE_EDIT category sync failed: job_id=%s property_id=%s",
                job_id,
                property_id,
            )

    return {
        "job_id": job_id,
        "snapshot": applied_snapshot,
        "changes": changes,
        "sync_error": sync_error,
    }


def _get_job(job_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, property_id, actor_user_id, status, changes
                FROM channel_file_edit_jobs
                WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "property_id": row[1], "actor_user_id": row[2],
        "status": row[3], "changes": row[4] or {},
    }


async def _show_property(query, context, property_id):
    snapshot = _load_snapshot(property_id)
    if not _is_channel_editable(snapshot):
        await query.edit_message_text(
            "❌ این فایل دیگر به‌صورت فعال روی کانال قابل ویرایش نیست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 جستجوی مجدد", callback_data="channel_edit")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
            ]),
        )
        return True

    context.user_data["channel_edit_property_id"] = property_id
    context.user_data["channel_edit_snapshot"] = _json_safe(snapshot)
    context.user_data["channel_edit_snapshot_version"] = snapshot["property"]["updated_at"].isoformat() if snapshot["property"].get("updated_at") else None
    context.user_data.setdefault("channel_edit_draft", {})

    await query.edit_message_text(
        _summary(snapshot) + "\n\nفیلدی که می‌خواهید تغییر کند را انتخاب کنید:",
        reply_markup=_editor_keyboard(snapshot, context.user_data.get("channel_edit_draft")),
        disable_web_page_preview=True,
    )
    return True


def _snapshot_from_context(context):
    property_id = context.user_data.get("channel_edit_property_id")
    if not property_id:
        return None
    snapshot = _load_snapshot(property_id)
    if not snapshot:
        return None
    expected = context.user_data.get("channel_edit_snapshot_version")
    if expected and snapshot["property"].get("updated_at"):
        # Keep the original version for optimistic concurrency at confirm.
        original = deepcopy(snapshot)
        try:
            original["property"]["updated_at"] = datetime.fromisoformat(expected)
        except Exception:
            pass
        return original
    return snapshot


async def handle_file_editor_button(update, context, session, *, allowed, sync_callback=None):
    query = update.callback_query
    action = (query.data if query else "") or ""
    if not action.startswith("channel_edit"):
        return False

    if not allowed:
        context.user_data.clear()
        await query.edit_message_text("⛔ مجوز ویرایش فایل‌های کانال را ندارید.")
        return True

    if action == "channel_edit":
        for key in list(context.user_data):
            if str(key).startswith("channel_edit_"):
                context.user_data.pop(key, None)
        context.user_data["state"] = STATE_LOOKUP
        await query.edit_message_text(
            _lookup_prompt(),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")]
            ]),
            disable_web_page_preview=True,
        )
        return True

    if action.startswith("channel_edit_pick_"):
        context.user_data["state"] = None
        context.user_data["channel_edit_draft"] = {}
        property_id = int(action.rsplit("_", 1)[1])
        return await _show_property(query, context, property_id)

    if action == "channel_edit_back_fields":
        property_id = context.user_data.get("channel_edit_property_id")
        if not property_id:
            await query.edit_message_text("❌ فایل انتخاب‌شده پیدا نشد.")
            return True
        context.user_data["state"] = None
        return await _show_property(query, context, int(property_id))

    if action == "channel_edit_preview":
        property_id = context.user_data.get("channel_edit_property_id")
        draft = context.user_data.get("channel_edit_draft") or {}
        if not property_id or not draft:
            await query.edit_message_text("❌ هنوز تغییری ثبت نشده است.")
            return True
        snapshot = _load_snapshot(int(property_id))
        if not _is_channel_editable(snapshot):
            await query.edit_message_text("❌ فایل دیگر قابل ویرایش نیست.")
            return True
        preview, _, _, _ = _changes_for_preview(snapshot, draft)
        await query.edit_message_text(
            limit_telegram_text(preview, MESSAGE_LIMIT),
            reply_markup=_confirm_keyboard(),
            disable_web_page_preview=True,
        )
        return True

    if action == "channel_edit_cancel":
        for key in list(context.user_data):
            if str(key).startswith("channel_edit_"):
                context.user_data.pop(key, None)
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "✅ ویرایش لغو شد و هیچ تغییری اعمال نشد.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ ویرایش فایل دیگر", callback_data="channel_edit")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
            ]),
        )
        return True

    if action.startswith("channel_edit_field_"):
        field_key = action.replace("channel_edit_field_", "", 1)
        if field_key not in CORE_FIELDS:
            await query.edit_message_text("❌ فیلد ناشناخته است.")
            return True
        property_id = context.user_data.get("channel_edit_property_id")
        snapshot = _load_snapshot(int(property_id)) if property_id else None
        if not _is_channel_editable(snapshot):
            await query.edit_message_text("❌ فایل انتخاب‌شده دیگر قابل ویرایش نیست.")
            return True
        context.user_data["channel_edit_field"] = field_key

        if field_key in BOOL_FIELDS:
            await query.edit_message_text(
                f"{CORE_FIELDS[field_key]}\n\nوضعیت جدید را انتخاب کنید:",
                reply_markup=_boolean_keyboard(field_key),
            )
            return True
        if field_key == "sale_mode":
            await query.edit_message_text("🏗 نوع عرضه جدید:", reply_markup=_sale_mode_keyboard())
            return True

        context.user_data["state"] = STATE_VALUE
        current = _current_value(field_key, snapshot)
        hints = {
            "current_price_toman": "مثال: 16.5 میلیارد یا 16500000000 تومان",
            "details": "هر جزئیات را در یک خط بفرستید. متن فعلی با متن جدید جایگزین می‌شود.",
            "district": "مثال: ولیعصر جنوبی",
            "address_text": "فقط آدرس عمومی قابل انتشار؛ شماره تماس/آدرس خصوصی ارسال نشود.",
        }
        hint = hints.get(field_key, "برای پاک‌کردن مقدار اختیاری، فقط - بفرستید.")
        await query.edit_message_text(
            f"{CORE_FIELDS[field_key]}\n\nمقدار فعلی:\n{_value_display(field_key, current)}\n\n{hint}\n\nمقدار جدید را ارسال کنید:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="channel_edit_back_fields")]
            ]),
        )
        return True

    if action.startswith("channel_edit_attr_"):
        attr_id = int(action.rsplit("_", 1)[1])
        property_id = context.user_data.get("channel_edit_property_id")
        snapshot = _load_snapshot(int(property_id)) if property_id else None
        if not _is_channel_editable(snapshot):
            await query.edit_message_text("❌ فایل انتخاب‌شده دیگر قابل ویرایش نیست.")
            return True
        attr = next((a for a in snapshot["attributes"] if int(a["id"]) == attr_id), None)
        if not attr:
            await query.edit_message_text("❌ این ویژگی دیگر وجود ندارد.")
            return True
        field_key = f"attr:{attr_id}"
        context.user_data["channel_edit_field"] = field_key
        context.user_data["state"] = STATE_VALUE
        await query.edit_message_text(
            f"🔹 {attr['attribute_label']}\n\nمقدار فعلی:\n{attr['attribute_value'] or '—'}\n\nمقدار جدید را ارسال کنید:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ بازگشت", callback_data="channel_edit_back_fields")]
            ]),
        )
        return True

    if action.startswith("channel_edit_bool_"):
        m = re.fullmatch(r"channel_edit_bool_(has_parking|has_elevator|has_storage)_(yes|no|clear)", action)
        if not m:
            return True
        field_key, choice = m.groups()
        value = {"yes": True, "no": False, "clear": None}[choice]
        draft = context.user_data.setdefault("channel_edit_draft", {})
        draft[field_key] = value
        context.user_data["state"] = None
        property_id = int(context.user_data["channel_edit_property_id"])
        snapshot = _load_snapshot(property_id)
        preview, _, _, _ = _changes_for_preview(snapshot, draft)
        await query.edit_message_text(
            limit_telegram_text(preview, MESSAGE_LIMIT),
            reply_markup=_confirm_keyboard(),
            disable_web_page_preview=True,
        )
        return True

    if action.startswith("channel_edit_sale_"):
        choice = action.replace("channel_edit_sale_", "", 1)
        mapping = {"SALE": "فروش", "PRE_SALE": "پیش‌فروش", "CLEAR": None}
        if choice not in mapping:
            return True
        draft = context.user_data.setdefault("channel_edit_draft", {})
        draft["sale_mode"] = mapping[choice]
        context.user_data["state"] = None
        property_id = int(context.user_data["channel_edit_property_id"])
        snapshot = _load_snapshot(property_id)
        preview, _, _, _ = _changes_for_preview(snapshot, draft)
        await query.edit_message_text(
            limit_telegram_text(preview, MESSAGE_LIMIT),
            reply_markup=_confirm_keyboard(),
            disable_web_page_preview=True,
        )
        return True

    if action.startswith("channel_edit_retry_"):
        job_id = int(action.rsplit("_", 1)[1])
        job = _get_job(job_id)
        if not job or job["status"] not in {"FAILED", "FAILED_COMPENSATION"}:
            await query.edit_message_text("❌ این ویرایش دیگر قابل Retry نیست.")
            return True
        if session[3] != "OWNER" and int(job["actor_user_id"] or 0) != int(session[0]):
            await query.edit_message_text("⛔ این Retry متعلق به کاربر دیگری است.")
            return True
        snapshot = _load_snapshot(job["property_id"])
        if not _is_channel_editable(snapshot):
            await query.edit_message_text("❌ فایل دیگر روی کانال قابل ویرایش نیست.")
            return True
        context.user_data["channel_edit_property_id"] = job["property_id"]
        context.user_data["channel_edit_draft"] = dict(job["changes"] or {})
        context.user_data["channel_edit_snapshot_version"] = snapshot["property"]["updated_at"].isoformat() if snapshot["property"].get("updated_at") else None
        preview, _, _, _ = _changes_for_preview(snapshot, context.user_data["channel_edit_draft"])
        await query.edit_message_text(
            limit_telegram_text(preview, MESSAGE_LIMIT),
            reply_markup=_confirm_keyboard(),
            disable_web_page_preview=True,
        )
        return True

    if action == "channel_edit_confirm":
        property_id = context.user_data.get("channel_edit_property_id")
        draft = context.user_data.get("channel_edit_draft") or {}
        if not property_id or not draft:
            await query.edit_message_text("❌ اطلاعات ویرایش کامل نیست.")
            return True

        current = _load_snapshot(int(property_id))
        expected = context.user_data.get("channel_edit_snapshot_version")
        lookup_snapshot = deepcopy(current)
        if expected and lookup_snapshot and lookup_snapshot["property"].get("updated_at"):
            try:
                lookup_snapshot["property"]["updated_at"] = datetime.fromisoformat(expected)
            except Exception:
                pass

        await query.edit_message_text("⏳ در حال اعمال همزمان روی دیتابیس و پست کانال…")
        try:
            result = await apply_edit_job(
                context.bot,
                session=session,
                telegram_user_id=update.effective_user.id,
                property_id=int(property_id),
                draft=draft,
                lookup_snapshot=lookup_snapshot,
                sync_callback=sync_callback,
            )
        except Exception as exc:
            logging.exception("CHANNEL_FILE_EDIT apply failed: property_id=%s", property_id)
            # Find newest failed job for this actor/property for retry.
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id FROM channel_file_edit_jobs
                        WHERE property_id = %s AND actor_user_id = %s
                          AND status IN ('FAILED', 'FAILED_COMPENSATION')
                        ORDER BY id DESC LIMIT 1
                        """,
                        (int(property_id), session[0]),
                    )
                    row = cur.fetchone()
            retry_markup = None
            if row:
                retry_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 تلاش مجدد", callback_data=f"channel_edit_retry_{row[0]}")],
                    [InlineKeyboardButton("✏️ بازگشت به ویرایش", callback_data="channel_edit_back_fields")],
                    [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
                ])
            await query.edit_message_text(
                "❌ ویرایش اعمال نشد.\n\n"
                "برای جلوگیری از ناسازگاری، تغییر دیتابیس Rollback شده است.\n"
                f"خطا: {str(exc)[:900]}",
                reply_markup=retry_markup,
            )
            return True

        context.user_data["channel_edit_draft"] = {}
        context.user_data["state"] = None
        snapshot = result["snapshot"]
        note = ""
        if result.get("sync_error"):
            note = "\n\n⚠️ خود فایل و پست اصلاح شدند؛ Sync دسته‌بندی نیاز به Retry جداگانه دارد."
        await query.edit_message_text(
            "✅ ویرایش با موفقیت اعمال شد.\n\n"
            "• دیتای PostgreSQL به‌روزرسانی شد\n"
            "• همان پست کانال ویرایش شد\n"
            f"• Audit Log ثبت شد (Job #{result['job_id']})"
            + note
            + "\n\n"
            + _summary(snapshot),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ ادامه ویرایش همین فایل", callback_data=f"channel_edit_pick_{property_id}")],
                [InlineKeyboardButton("🔎 ویرایش فایل دیگر", callback_data="channel_edit")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
            ]),
            disable_web_page_preview=True,
        )
        return True

    return False


async def handle_file_editor_text(update, context, session, *, allowed):
    state = context.user_data.get("state")
    if state not in {STATE_LOOKUP, STATE_VALUE}:
        return False

    if not allowed:
        context.user_data.clear()
        await update.message.reply_text("⛔ مجوز ویرایش فایل‌های کانال را ندارید.")
        return True

    if state == STATE_LOOKUP:
        rows = find_published_properties(update.message.text)
        if not rows:
            await update.message.reply_text(
                "❌ فایل منتشرشده‌ای با این مشخصات پیدا نشد.\n\n" + _lookup_prompt(),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")]
                ]),
                disable_web_page_preview=True,
            )
            return True
        if len(rows) == 1:
            context.user_data["state"] = None
            context.user_data["channel_edit_draft"] = {}
            snapshot = _load_snapshot(rows[0][0])
            context.user_data["channel_edit_property_id"] = rows[0][0]
            context.user_data["channel_edit_snapshot_version"] = snapshot["property"]["updated_at"].isoformat() if snapshot and snapshot["property"].get("updated_at") else None
            await update.message.reply_text(
                _summary(snapshot) + "\n\nفیلدی که می‌خواهید تغییر کند را انتخاب کنید:",
                reply_markup=_editor_keyboard(snapshot),
                disable_web_page_preview=True,
            )
            return True

        await update.message.reply_text(
            f"🔎 {len(rows)} نتیجه پیدا شد. فایل موردنظر را انتخاب کنید:",
            reply_markup=_select_keyboard(rows),
        )
        return True

    field_key = context.user_data.get("channel_edit_field")
    property_id = context.user_data.get("channel_edit_property_id")
    if not field_key or not property_id:
        context.user_data.pop("state", None)
        await update.message.reply_text("❌ نشست ویرایش ناقص شد؛ دوباره فایل را انتخاب کنید.")
        return True

    snapshot = _load_snapshot(int(property_id))
    if not _is_channel_editable(snapshot):
        context.user_data.pop("state", None)
        await update.message.reply_text("❌ فایل دیگر روی کانال قابل ویرایش نیست.")
        return True

    try:
        value = parse_field_value(field_key, update.message.text)
        if field_key == "details":
            # Allow clearing all details with '-'.
            if str(update.message.text or "").strip() in {"-", "—", "حذف", "پاک"}:
                value = ""
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}\n\nدوباره مقدار درست را ارسال کنید:")
        return True

    draft = context.user_data.setdefault("channel_edit_draft", {})
    draft[field_key] = value
    context.user_data["state"] = None
    preview, _, _, _ = _changes_for_preview(snapshot, draft)
    await update.message.reply_text(
        limit_telegram_text(preview, MESSAGE_LIMIT),
        reply_markup=_confirm_keyboard(),
        disable_web_page_preview=True,
    )
    return True

