import asyncio
import urllib.request
import urllib.error
import re
import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
import os
import json
import hashlib
import logging
import bcrypt
import psycopg

from logging_security import secure_telegram_logging
from telegram_delivery import edit_published_message
from telegram.error import RetryAfter, TimedOut

from property_filter import classify_property
from property_parser import parse_property, parse_dynamic_attributes, parse_rule_attributes, parse_source_unit_code
from publisher import get_property_for_publish, build_publish_text, publish_property, get_property_media, republish_property, get_destination_channel

from scheduler import (
    log_scheduler_health_snapshot,
    start_scheduler,
)
from visit_flow import (
    handle_visit_start,
    handle_visit_contact,
    handle_visit_source_callback,
    start_visit_notification_worker,
)
from visit_ops_group import (
    ensure_schema as ensure_visit_ops_schema,
    handle_connect_command as handle_visit_ops_connect,
)

from crm_panel import (
    ensure_crm_schema,
    build_main_menu,
    build_settings_menu,
    check_action_access,
    handle_crm_button,
    handle_crm_text,
    region_category_navigation_handler,
    sync_region_category_posts,
    start_visit_reminder_worker,
)

from channel_file_editor import (
    ensure_channel_file_editor_schema,
    handle_file_editor_button,
    handle_file_editor_text,
)

from footer_bulk_refresh import (
    ensure_footer_bulk_refresh_schema,
    start_footer_bulk_refresh_worker,
    handle_footer_bulk_button,
    footer_bulk_offer_text,
    footer_bulk_offer_markup,
    audit_footer_setting_change,
)

from duplicate_detector import (
    detect_and_store_duplicate,
    set_duplicate_review_status,
)

from final_features import (
    handle_button as handle_final_button,
    handle_text as handle_final_text,
    handle_aux_text as handle_final_aux_text,
    handle_photo as handle_final_photo,
    audit_login as final_audit_login,
)

from shahfile_smart import (
    ensure_smart_schema,
    handle_customer_start as handle_smart_customer_start,
    handle_customer_text as handle_smart_customer_text,
    handle_customer_callback as handle_smart_customer_callback,
    handle_customer_photo as handle_smart_customer_photo,
    handle_customer_contact as handle_smart_customer_contact,
    handle_manager_text as handle_smart_manager_text,
)
from shahfile_ai import start_search_watch_worker
from crm_campaign_hub import (
    ensure_schema as ensure_campaign_crm_schema,
    start_worker as start_campaign_broadcast_worker,
)
from customer_showcase import (
    ensure_showcase_schema,
    manager_filters_to_public,
    create_showcase,
    build_showcase_link,
    handle_showcase_start,
    handle_showcase_callback,
    start_campaign_watch_worker,
    campaign_summary_text,
)

from ingest import (
    get_input_channel_id,
    raw_property_exists,
    get_property_by_media_group,
    create_raw_property,
    update_raw_text,
    add_media,
    add_to_queue,
    set_ingest_classification,
    update_parsed_property,
    save_property_attributes,
    get_active_attribute_rules,
    save_property_source,
    save_property_address,
)

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
secure_telegram_logging()

BOT_TOKEN = os.getenv("BOT_TOKEN")

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")



PUBLISH_SETTING_LABELS = {
    "publish_footer": "📝 متن ثابت انتهای پست",
    "contact_people": "👥 نام و شماره تماس",
    "contact_telegram": "📱 آیدی تلگرام",
    "emoji_location": "📍 ایموجی لوکیشن",
    "emoji_area": "📐 ایموجی متراژ",
    "emoji_bedrooms": "🛏 ایموجی خواب",
    "emoji_floor": "🏢 ایموجی طبقه",
    "emoji_build_year": "📅 ایموجی سال ساخت",
    "emoji_direction": "🧭 ایموجی جهت",
    "emoji_parking": "🚗 ایموجی پارکینگ",
    "emoji_elevator": "🛗 ایموجی آسانسور",
    "emoji_storage": "📦 ایموجی انباری",
    "emoji_price": "💰 ایموجی قیمت",
    "emoji_unknown": "🔹 ایموجی موارد ناشناخته",
}


def settings_menu():
    rows = [
        [
            InlineKeyboardButton(
                "🎨 تغییرات ایموجی پیام",
                callback_data="settings_emojis",
            )
        ],
        [
            InlineKeyboardButton(
                "📝 تغییرات اطلاعات ثابت فوتر",
                callback_data="settings_footer",
            )
        ],
        [
            InlineKeyboardButton(
                "🧩 مدیریت مشخصات پویا",
                callback_data="attribute_rules",
            )
        ],
        [
            InlineKeyboardButton(
                "🗓 اعتبار فایل‌ها",
                callback_data="settings_file_validity",
            )
        ],
        [
            InlineKeyboardButton(
                "👁 نمایش تنظیمات فعلی",
                callback_data="settings_show",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="home",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def emoji_settings_menu():
    rows = [
        [
            InlineKeyboardButton(
                "📍 لوکیشن",
                callback_data="setting_edit_emoji_location",
            ),
            InlineKeyboardButton(
                "📐 متراژ",
                callback_data="setting_edit_emoji_area",
            ),
        ],
        [
            InlineKeyboardButton(
                "🛏 خواب",
                callback_data="setting_edit_emoji_bedrooms",
            ),
            InlineKeyboardButton(
                "🏢 طبقه",
                callback_data="setting_edit_emoji_floor",
            ),
        ],
        [
            InlineKeyboardButton(
                "📅 سال ساخت",
                callback_data="setting_edit_emoji_build_year",
            ),
            InlineKeyboardButton(
                "🧭 جهت",
                callback_data="setting_edit_emoji_direction",
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 قیمت",
                callback_data="setting_edit_emoji_price",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚗 پارکینگ",
                callback_data="setting_edit_emoji_parking",
            ),
            InlineKeyboardButton(
                "🛗 آسانسور",
                callback_data="setting_edit_emoji_elevator",
            ),
        ],
        [
            InlineKeyboardButton(
                "📦 انباری",
                callback_data="setting_edit_emoji_storage",
            )
        ],
        [
            InlineKeyboardButton(
                "🔹 موارد ناشناخته",
                callback_data="setting_edit_emoji_unknown",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به تنظیمات",
                callback_data="settings",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def footer_settings_menu():
    rows = [
        [
            InlineKeyboardButton(
                "📝 متن ثابت انتهای پست",
                callback_data="setting_edit_publish_footer",
            )
        ],
        [
            InlineKeyboardButton(
                "👥 نام و شماره تماس",
                callback_data="settings_footer_contacts_edit",
            )
        ],
        [
            InlineKeyboardButton(
                "📱 آیدی تلگرام",
                callback_data="setting_edit_contact_telegram",
            )
        ],
        [
            InlineKeyboardButton(
                "♻️ اعمال فوتر فعلی روی پست‌های قبلی",
                callback_data="footer_bulk_offer",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به تنظیمات",
                callback_data="settings",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)



def attribute_rules_menu():
    rows = [
        [
            InlineKeyboardButton(
                "➕ افزودن مشخصه جدید",
                callback_data="attribute_rule_add",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 مشاهده مشخصه‌ها",
                callback_data="attribute_rules_list",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به تنظیمات",
                callback_data="settings",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)



def attribute_rule_actions_menu(rule_id, is_active):
    toggle_text = (
        "⛔ غیرفعال کردن"
        if is_active
        else "✅ فعال کردن"
    )

    rows = [
        [
            InlineKeyboardButton(
                toggle_text,
                callback_data=f"attribute_rule_toggle_{rule_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 حذف مشخصه",
                callback_data=f"attribute_rule_delete_{rule_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به لیست",
                callback_data="attribute_rules_list",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def get_attribute_rule(rule_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    attribute_key,
                    attribute_label,
                    match_type,
                    trigger_text,
                    default_value,
                    display_order,
                    is_active
                FROM property_attribute_rules
                WHERE id = %s
                """,
                (rule_id,),
            )

            return cur.fetchone()


def toggle_attribute_rule(rule_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE property_attribute_rules
                SET
                    is_active = NOT is_active,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING is_active
                """,
                (rule_id,),
            )

            row = cur.fetchone()
            conn.commit()

            return row[0] if row else None


def delete_attribute_rule(rule_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM property_attribute_rules
                WHERE id = %s
                RETURNING attribute_label
                """,
                (rule_id,),
            )

            row = cur.fetchone()
            conn.commit()

            return row[0] if row else None

def get_attribute_rules():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    attribute_key,
                    attribute_label,
                    match_type,
                    trigger_text,
                    default_value,
                    display_order,
                    is_active
                FROM property_attribute_rules
                ORDER BY display_order, id
                """
            )

            return cur.fetchall()


def create_attribute_rule(
    attribute_key,
    attribute_label,
    match_type,
    trigger_text,
    default_value=None,
    display_order=100,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO property_attribute_rules (
                    attribute_key,
                    attribute_label,
                    match_type,
                    trigger_text,
                    default_value,
                    display_order,
                    is_active,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    NOW()
                )
                ON CONFLICT (attribute_key)
                DO UPDATE SET
                    attribute_label = EXCLUDED.attribute_label,
                    match_type = EXCLUDED.match_type,
                    trigger_text = EXCLUDED.trigger_text,
                    default_value = EXCLUDED.default_value,
                    display_order = EXCLUDED.display_order,
                    is_active = TRUE,
                    updated_at = NOW()
                RETURNING
                    id,
                    attribute_key,
                    attribute_label,
                    trigger_text,
                    is_active
                """,
                (
                    attribute_key,
                    attribute_label,
                    match_type,
                    trigger_text,
                    default_value,
                    display_order,
                ),
            )

            row = cur.fetchone()
            conn.commit()

            return row

def save_app_setting(key, value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (
                    setting_key,
                    setting_value,
                    updated_at
                )
                VALUES (
                    %s,
                    %s::jsonb,
                    NOW()
                )
                ON CONFLICT (setting_key)
                DO UPDATE SET
                    setting_value = EXCLUDED.setting_value,
                    updated_at = NOW()
                """,
                (
                    key,
                    payload,
                ),
            )

            conn.commit()


def get_app_setting(key, default=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT setting_value
                FROM app_settings
                WHERE setting_key = %s
                """,
                (key,),
            )

            row = cur.fetchone()

            if not row:
                return default

            return row[0]




def _legacy_contact_people():
    name = get_app_setting("contact_name", "") or ""
    phone = get_app_setting("contact_phone", "") or ""

    name = str(name).strip()
    phone = str(phone).strip()

    if not name and not phone:
        return []

    return [
        {
            "name": name,
            "phone": phone,
        }
    ]


def get_contact_people_setting():
    people = get_app_setting(
        "contact_people",
        None,
    )

    if isinstance(people, list):
        cleaned = []

        for item in people:
            if not isinstance(item, dict):
                continue

            name = str(
                item.get("name") or ""
            ).strip()
            phone = str(
                item.get("phone") or ""
            ).strip()

            if not name and not phone:
                continue

            cleaned.append(
                {
                    "name": name,
                    "phone": phone,
                }
            )

        if cleaned:
            return cleaned

    return _legacy_contact_people()


def format_contact_people(people=None):
    people = (
        people
        if people is not None
        else get_contact_people_setting()
    )

    if not people:
        return "تنظیم نشده"

    lines = []

    for index, person in enumerate(
        people,
        start=1,
    ):
        name = str(
            person.get("name") or ""
        ).strip()
        phone = str(
            person.get("phone") or ""
        ).strip()

        lines.append(f"شخص {index}:")
        lines.append(f"👤 {name or '-'}")
        lines.append(f"📞 {phone or '-'}")

        if index != len(people):
            lines.append("")

    return "\n".join(lines)


def _normalize_contact_phone(value):
    value = str(value or "").strip()

    digit_map = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    value = value.translate(digit_map)

    if not value:
        raise ValueError("شماره تماس خالی است.")

    if not re.fullmatch(
        r"[+0-9][0-9+()\-\s]{5,24}",
        value,
    ):
        raise ValueError(
            "شماره تماس معتبر نیست."
        )

    return re.sub(r"\s+", " ", value)


def _contact_people_question_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ بله، شخص دیگری هست",
                    callback_data="settings_footer_contacts_add_another",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ خیر، ذخیره کن",
                    callback_data="settings_footer_contacts_finish",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ انصراف",
                    callback_data="settings_footer",
                )
            ],
        ]
    )


def ensure_property_trash_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS property_trash (
                    property_id BIGINT PRIMARY KEY
                        REFERENCES properties(id)
                        ON DELETE CASCADE,
                    previous_status TEXT,
                    previous_published_message_id BIGINT,
                    previous_channel_post_url TEXT,
                    archive_reason TEXT,
                    validity_weeks INTEGER,
                    archived_at TIMESTAMPTZ NOT NULL
                        DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_property_trash_archived_at
                ON property_trash (archived_at DESC)
                """
            )

            conn.commit()



def enqueue_republish_candidates(limit):
    try:
        limit = int(limit)
    except Exception:
        raise ValueError("limit must be an integer")

    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")

    validity_weeks = get_file_validity_weeks()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT p.id
                    FROM properties p
                    WHERE p.status = 'ACTIVE'
                      AND p.published_message_id IS NOT NULL
                      AND p.channel_post_url IS NOT NULL
                      AND p.deleted_from_channel_at IS NULL

                      AND p.created_at >=
                          NOW() - (%s * INTERVAL '1 week')

                      AND NOT EXISTS (
                          SELECT 1
                          FROM property_trash t
                          WHERE t.property_id = p.id
                      )

                      AND NOT EXISTS (
                          SELECT 1
                          FROM republish_queue rq
                          WHERE rq.property_id = p.id
                            AND rq.queue_status IN (
                                'READY',
                                'SCHEDULED',
                                'PROCESSING'
                            )
                      )

                    ORDER BY
                        p.created_at ASC,
                        p.id ASC

                    LIMIT %s
                )

                INSERT INTO republish_queue (
                    property_id,
                    queue_status,
                    requested_at,
                    created_at,
                    updated_at
                )
                SELECT
                    id,
                    'READY',
                    NOW(),
                    NOW(),
                    NOW()
                FROM candidates
                ON CONFLICT DO NOTHING
                RETURNING property_id
                """,
                (
                    validity_weeks,
                    limit,
                ),
            )

            rows = cur.fetchall()
            conn.commit()

    return {
        "requested": limit,
        "added": len(rows),
        "property_ids": [
            row[0]
            for row in rows
        ],
        "validity_weeks": validity_weeks,
    }


def get_republish_queue_summary():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    queue_status,
                    COUNT(*)
                FROM republish_queue
                GROUP BY queue_status
                ORDER BY queue_status
                """
            )

            rows = cur.fetchall()

    return {
        row[0]: row[1]
        for row in rows
    }


def get_file_validity_weeks():
    value = get_app_setting(
        "file_validity_weeks",
        8,
    )

    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 8

    return max(1, min(value, 104))


def get_expired_property_rows(weeks=None):
    if weeks is None:
        weeks = get_file_validity_weeks()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.current_price_billion,
                    p.created_at,
                    p.published_message_id,
                    p.channel_post_url
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.created_at <
                      NOW() - (%s * INTERVAL '1 week')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM property_trash t
                      WHERE t.property_id = p.id
                  )
                ORDER BY
                    p.created_at ASC,
                    p.id ASC
                """,
                (weeks,),
            )

            return cur.fetchall()


def get_trash_rows(limit=10, offset=0):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.property_id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.current_price_billion,
                    t.archived_at,
                    t.archive_reason
                FROM property_trash t
                JOIN properties p
                  ON p.id = t.property_id
                ORDER BY
                    t.archived_at DESC,
                    t.property_id DESC
                LIMIT %s
                OFFSET %s
                """,
                (limit, offset),
            )

            return cur.fetchall()


def get_trash_count():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM property_trash
                """
            )

            row = cur.fetchone()

    return int(row[0] or 0)


def get_trash_property_detail(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.property_id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.floor_text,
                    p.build_year,
                    p.current_price_billion,
                    t.archived_at,
                    t.archive_reason,
                    t.validity_weeks
                FROM property_trash t
                JOIN properties p
                  ON p.id = t.property_id
                WHERE t.property_id = %s
                LIMIT 1
                """,
                (property_id,),
            )

            return cur.fetchone()


def trash_list_keyboard(offset=0, page_size=10):
    total = get_trash_count()
    rows = get_trash_rows(
        limit=page_size,
        offset=offset,
    )

    buttons = []

    for row in rows:
        (
            property_id,
            unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            archived_at,
            archive_reason,
        ) = row

        location = (
            address_text
            or district
            or "لوکیشن نامشخص"
        )

        location = str(location).strip()

        if len(location) > 25:
            location = location[:25] + "…"

        code = (
            str(unit_code)
            if unit_code
            else f"ID {property_id}"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"🗑 {code} | {location}",
                    callback_data=(
                        f"trash_view_{property_id}"
                    ),
                )
            ]
        )

    navigation = []

    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ قبلی",
                callback_data=(
                    f"trash_page_{max(0, offset-page_size)}"
                ),
            )
        )

    if offset + page_size < total:
        navigation.append(
            InlineKeyboardButton(
                "بعدی ➡️",
                callback_data=(
                    f"trash_page_{offset+page_size}"
                ),
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data=f"trash_page_{offset}",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به فایل‌ها",
                callback_data="files_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def build_trash_detail(row):
    (
        property_id,
        unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        floor_text,
        build_year,
        price_billion,
        archived_at,
        archive_reason,
        validity_weeks,
    ) = row

    location = (
        address_text
        or district
        or "ثبت نشده"
    )

    price = (
        f"{float(price_billion):g} میلیارد"
        if price_billion is not None
        else "ثبت نشده"
    )

    archive_time = (
        archived_at.astimezone(
            ZoneInfo("Asia/Tehran")
        ).strftime("%Y-%m-%d %H:%M")
        if archived_at
        else "-"
    )

    return (
        "🗑 فایل آرشیوشده\n\n"
        f"کد واحد: {unit_code or '-'}\n"
        f"📍 {location}\n"
        f"📐 متراژ: {area_m2 or '-'}\n"
        f"🛏 خواب: "
        f"{bedrooms if bedrooms is not None else '-'}\n"
        f"🏢 طبقه: {floor_text or '-'}\n"
        f"📅 سال ساخت: {build_year or '-'}\n"
        f"💰 قیمت: {price}\n\n"
        f"🕒 زمان انتقال: {archive_time}\n"
        f"📝 علت: {archive_reason or '-'}\n"
        f"اعتبار هنگام حذف: "
        f"{validity_weeks or '-'} هفته"
    )


async def archive_property_to_trash(
    bot,
    property_id,
    validity_weeks,
):
    destination = get_destination_channel()

    if not destination:
        raise RuntimeError(
            "کانال اصلی فعال پیدا نشد."
        )

    destination_channel_id = destination[0]

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    unit_code,
                    published_message_id,
                    channel_post_url,
                    status,
                    deleted_from_channel_at
                FROM properties
                WHERE id = %s
                LIMIT 1
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        raise RuntimeError(
            "فایل در دیتابیس پیدا نشد."
        )

    (
        unit_code,
        published_message_id,
        channel_post_url,
        previous_status,
        deleted_from_channel_at,
    ) = row

    if (
        previous_status != "ACTIVE"
        or deleted_from_channel_at is not None
    ):
        raise RuntimeError(
            "فایل دیگر فعال نیست."
        )

    media_rows = get_property_media(
        property_id
    )

    photo_count = len(
        [
            item
            for item in media_rows
            if item[1] == "PHOTO"
        ]
    )

    message_ids = []

    if published_message_id:
        album_size = (
            min(photo_count, 10)
            if photo_count > 1
            else 1
        )

        message_ids = [
            int(published_message_id) + offset
            for offset in range(album_size)
        ]

    missing_markers = (
        "message to delete not found",
        "message not found",
    )

    for message_id in message_ids:
        try:
            await bot.delete_message(
                chat_id=destination_channel_id,
                message_id=message_id,
            )

        except Exception as exc:
            error_text = str(exc).lower()

            if any(
                marker in error_text
                for marker in missing_markers
            ):
                continue

            if (
                "timed out" in error_text
                or "timeout" in error_text
            ):
                try:
                    await bot.delete_message(
                        chat_id=destination_channel_id,
                        message_id=message_id,
                    )
                    continue

                except Exception as retry_exc:
                    retry_text = str(
                        retry_exc
                    ).lower()

                    if any(
                        marker in retry_text
                        for marker in missing_markers
                    ):
                        continue

                    raise

            raise

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO property_trash (
                    property_id,
                    previous_status,
                    previous_published_message_id,
                    previous_channel_post_url,
                    archive_reason,
                    validity_weeks,
                    archived_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    NOW()
                )
                ON CONFLICT (property_id)
                DO UPDATE SET
                    previous_status =
                        EXCLUDED.previous_status,
                    previous_published_message_id =
                        EXCLUDED.previous_published_message_id,
                    previous_channel_post_url =
                        EXCLUDED.previous_channel_post_url,
                    archive_reason =
                        EXCLUDED.archive_reason,
                    validity_weeks =
                        EXCLUDED.validity_weeks,
                    archived_at = NOW()
                """,
                (
                    property_id,
                    previous_status,
                    published_message_id,
                    channel_post_url,
                    (
                        "Expired by file validity policy "
                        f"({validity_weeks} weeks)"
                    ),
                    validity_weeks,
                ),
            )

            cur.execute(
                """
                UPDATE properties
                SET
                    status = 'INACTIVE',
                    deleted_from_channel_at = NOW(),
                    published_message_id = NULL,
                    channel_post_url = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE property_sources
                SET
                    is_active = FALSE,
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'SKIPPED',
                    last_error =
                        'Archived by file validity policy',
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status IN (
                      'NEW',
                      'PARSED',
                      'READY',
                      'SCHEDULED',
                      'PENDING_DUPLICATE',
                      'FAILED',
                      'PUBLISHED'
                  )
                """,
                (property_id,),
            )

            conn.commit()

    return {
        "property_id": property_id,
        "unit_code": unit_code,
    }


async def archive_expired_properties(
    bot,
    weeks,
):
    rows = get_expired_property_rows(
        weeks
    )

    archived = []
    failed = []

    for row in rows:
        property_id = row[0]

        try:
            result = (
                await archive_property_to_trash(
                    bot,
                    property_id,
                    weeks,
                )
            )

            archived.append(result)

        except Exception as exc:
            logging.exception(
                "Expiry archive failed: "
                "property_id=%s",
                property_id,
            )

            failed.append(
                (
                    property_id,
                    str(exc)[:300],
                )
            )

    try:
        await queue_targeted_category_sync(
            bot,
            [item.get("property_id") for item in archived],
            reason="expiry-archive",
        )
    except Exception:
        logging.exception(
            "Category sync after expiry archive failed"
        )

    return {
        "archived": archived,
        "failed": failed,
    }


def restore_property_from_trash(
    property_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT property_id
                FROM property_trash
                WHERE property_id = %s
                FOR UPDATE
                """,
                (property_id,),
            )

            row = cur.fetchone()

            if not row:
                return False

            cur.execute(
                """
                UPDATE properties
                SET
                    status = 'ACTIVE',
                    deleted_from_channel_at = NULL,
                    published_message_id = NULL,
                    channel_post_url = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE property_sources
                SET
                    is_active = TRUE,
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'READY',
                    scheduled_for = NULL,
                    published_at = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (property_id,),
            )

            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO publish_queue (
                        property_id,
                        queue_status
                    )
                    VALUES (%s, 'READY')
                    """,
                    (property_id,),
                )

            cur.execute(
                """
                DELETE FROM property_trash
                WHERE property_id = %s
                """,
                (property_id,),
            )

            conn.commit()

    return True


def current_publish_settings_text():
    lines = [
        "⚙️ تنظیمات فعلی انتشار",
        "",
    ]

    for key, label in PUBLISH_SETTING_LABELS.items():
        if key == "contact_people":
            value = format_contact_people()
        else:
            value = get_app_setting(
                key,
                "تنظیم نشده",
            )

            if value in (None, ""):
                value = "تنظیم نشده"

        lines.append(
            f"{label}:\n{value}\n"
        )

    return "\n".join(lines)


def find_properties_by_source_code(source_code):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.district,
                    p.area_m2,
                    p.current_price_billion,
                    p.published_message_id,
                    p.unit_code
                FROM property_sources ps
                JOIN properties p
                    ON p.id = ps.property_id
                WHERE ps.source_unit_code = %s
                  AND ps.is_active = TRUE
                  AND p.status = 'ACTIVE'
                ORDER BY p.id DESC
                """,
                (source_code,),
            )

            return cur.fetchall()



# === URGENT LOOKUP WITHOUT SOURCE CODE V1 ===

# === URGENT QUEUE TRACKING LOOKUP V1 ===
def _parse_urgent_queue_property_id(value):
    """Return internal property_id for inputs like `صف 45`."""
    lookup = str(value or "").strip()
    if not lookup:
        return None

    normalized = lookup.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )

    match = re.fullmatch(
        r"\s*صف\s*[:：#-]?\s*(\d+)\s*",
        normalized,
        flags=re.IGNORECASE,
    )

    return int(match.group(1)) if match else None


def _find_urgent_property_by_queue_tracking(tracking_number):
    """Resolve ``صف N`` robustly.

    Historically the UI used the internal property id as the queue tracking
    number (``صف <property_id>``).  Some operators can also encounter the
    physical ``publish_queue.id``.  Prefer the historical property-id meaning,
    then fall back to queue-row id.  Published rows are intentionally allowed
    so the urgent flow can say "already published" instead of falsely saying
    "file not found".
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            # Primary meaning used by PDF/search UI: صف <property_id>.
            cur.execute(
                """
                SELECT DISTINCT
                    p.id,
                    p.district,
                    p.area_m2,
                    p.current_price_billion,
                    p.published_message_id,
                    p.unit_code
                FROM properties p
                WHERE p.id = %s
                  AND p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM publish_queue q
                      WHERE q.property_id = p.id
                  )
                LIMIT 1
                """,
                (tracking_number,),
            )
            rows = cur.fetchall()
            if rows:
                return rows

            # Defensive fallback: accept the actual publish_queue row id too.
            cur.execute(
                """
                SELECT DISTINCT
                    p.id,
                    p.district,
                    p.area_m2,
                    p.current_price_billion,
                    p.published_message_id,
                    p.unit_code
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                WHERE q.id = %s
                  AND p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                LIMIT 1
                """,
                (tracking_number,),
            )
            return cur.fetchall()


def find_properties_for_urgent_lookup(value):
    """
    Robust urgent lookup accepts:
      1) queue tracking number such as `صف 45`
      2) source unit/file code, including Persian/Arabic digits
      3) t.me source post URL, including private /c/ links
      4) source Telegram message ID
      5) destination unit code as a last-resort fallback

    Return shape matches find_properties_by_source_code().
    """

    lookup = str(value or "").strip()

    if not lookup:
        return []

    queue_property_id = _parse_urgent_queue_property_id(
        lookup
    )

    if queue_property_id is not None:
        return _find_urgent_property_by_queue_tracking(
            queue_property_id
        )

    digit_map = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    normalized = lookup.translate(
        digit_map
    ).strip()

    # Accept labels such as «کد فایل 10338», but keep a bare number
    # valid as both source code and message id fallback.
    code_match = re.fullmatch(
        r"(?:کد\s*(?:فایل|واحد|ملک|آگهی)\s*[:：\-]?\s*)?(\d+)",
        normalized,
        flags=re.IGNORECASE,
    )

    normalized_code = (
        code_match.group(1)
        if code_match
        else None
    )

    # Source code must be tried AFTER digit normalization. Older code
    # tried the raw Persian digits first, then accidentally treated the
    # normalized value only as a Telegram message id.
    if normalized_code:
        rows = find_properties_by_source_code(
            normalized_code
        )

        if rows:
            return rows

    def _run_lookup(where_sql, params):
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT
                        p.id,
                        p.district,
                        p.area_m2,
                        p.current_price_billion,
                        p.published_message_id,
                        p.unit_code
                    FROM properties p
                    LEFT JOIN property_sources ps
                        ON ps.property_id = p.id
                    LEFT JOIN channels c
                        ON c.id = ps.source_channel_id
                    WHERE p.status = 'ACTIVE'
                      AND p.deleted_from_channel_at IS NULL
                      AND ({where_sql})
                    ORDER BY p.id DESC
                    """,
                    params,
                )
                return cur.fetchall()

    clean_url = None
    message_id = None
    public_username = None
    telegram_channel_id = None

    url_match = re.search(
        r"(?:https?://)?(?:www\.)?t\.me/"
        r"(?:(?:c/(\d+))|([A-Za-z0-9_]+))/"
        r"(\d+)",
        normalized,
        flags=re.IGNORECASE,
    )

    if url_match:
        private_channel_part = url_match.group(1)
        public_username = url_match.group(2)
        message_id = int(url_match.group(3))

        clean_url = re.sub(
            r"[?#].*$",
            "",
            normalized,
        ).rstrip("/")

        if not clean_url.startswith(
            ("http://", "https://")
        ):
            clean_url = "https://" + clean_url

        if private_channel_part:
            telegram_channel_id = int(
                "-100" + private_channel_part
            )

    elif normalized_code:
        # Exact source-code lookup above already failed. A bare number can
        # also be the source Telegram message id.
        message_id = int(normalized_code)

    # Stage 1: strict source URL / channel-aware Telegram lookup.
    clauses = []
    params = []

    if clean_url:
        clauses.append(
            """
            regexp_replace(
                split_part(
                    COALESCE(ps.source_post_url, ''),
                    '?',
                    1
                ),
                '/+$',
                ''
            ) = %s
            """
        )
        params.append(clean_url)

    if (
        message_id is not None
        and telegram_channel_id is not None
    ):
        clauses.append(
            """
            (
                ps.source_message_id = %s
                AND c.telegram_channel_id = %s
            )
            """
        )
        params.extend([
            message_id,
            telegram_channel_id,
        ])

    if (
        message_id is not None
        and public_username
    ):
        clauses.append(
            """
            (
                ps.source_message_id = %s
                AND LOWER(
                    REPLACE(
                        COALESCE(c.username, ''),
                        '@',
                        ''
                    )
                ) = %s
            )
            """
        )
        params.extend([
            message_id,
            public_username.lower(),
        ])

    if clauses:
        rows = _run_lookup(
            " OR ".join(
                f"({clause})"
                for clause in clauses
            ),
            params,
        )
        if rows:
            return rows

    # Stage 2: message-id fallback. This intentionally returns multiple
    # candidates when the source-channel mapping is incomplete, so the
    # operator can select instead of getting a false “not found”.
    if message_id is not None:
        rows = _run_lookup(
            """
            ps.source_message_id = %s
            OR p.input_message_id = %s
            OR COALESCE(ps.source_post_url, '') ~ %s
            """,
            [
                message_id,
                message_id,
                rf"/{message_id}(?:[/?#].*)?$",
            ],
        )
        if rows:
            return rows

    # Stage 3: if a source code was visible in imported/raw text but an
    # old row missed source_unit_code, recover it from labelled raw text.
    if normalized_code:
        raw_code_pattern = (
            r"کد[[:space:]‌]*(?:فایل|واحد|ملک|آگهی)"
            r"[[:space:]]*[:：\-]?[[:space:]]*"
            + re.escape(normalized_code)
            + r"([^0-9]|$)"
        )

        rows = _run_lookup(
            """
            translate(
                COALESCE(ps.source_raw_text, ''),
                '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩',
                '01234567890123456789'
            ) ~* %s
            OR translate(
                COALESCE(p.raw_text, ''),
                '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩',
                '01234567890123456789'
            ) ~* %s
            """,
            [
                raw_code_pattern,
                raw_code_pattern,
            ],
        )
        if rows:
            return rows

        # Final operator-friendly fallback: destination unit code.
        rows = _run_lookup(
            "p.unit_code = %s",
            [normalized_code],
        )
        if rows:
            return rows

    return []


def republish_confirm_keyboard(property_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ بازنشر با همین کد",
                    callback_data=f"republish_confirm_{property_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ انصراف",
                    callback_data="home",
                )
            ],
        ]
    )



def get_unit_code_full_details(unit_code):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.build_year,
                    p.current_price_billion,
                    p.status,
                    p.published_message_id,
                    p.channel_post_url
                FROM properties p
                WHERE p.unit_code = %s
                LIMIT 1
                """,
                (unit_code,),
            )

            property_row = cur.fetchone()

            if not property_row:
                return None, []

            property_id = property_row[0]

            cur.execute(
                """
                SELECT
                    ps.source_unit_code,
                    ps.source_message_id,
                    ps.is_active,
                    c.title,
                    c.username,
                    c.telegram_channel_id,
                    ps.source_post_url
                FROM property_sources ps
                LEFT JOIN channels c
                    ON c.id = ps.source_channel_id
                WHERE ps.property_id = %s
                ORDER BY
                    ps.is_primary DESC,
                    ps.id ASC
                """,
                (property_id,),
            )

            source_rows = cur.fetchall()

            return property_row, source_rows


def get_property_attribution_detail(property_id):
    """Management-only attribution for a property.

    Manual staff files are linked through properties.manual_created_by_user_id.
    ShahFile seller files stay linked to seller_submissions so owner/private data
    does not need to be copied into the public property payload.
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.source_kind,
                    p.manual_created_by_user_id,
                    u.display_name,
                    u.username,
                    u.role,
                    (
                        SELECT us.telegram_user_id
                        FROM user_sessions us
                        WHERE us.user_id = u.id
                        ORDER BY us.last_activity_at DESC, us.id DESC
                        LIMIT 1
                    ) AS registrar_telegram_id
                FROM properties p
                LEFT JOIN users u
                  ON u.id = p.manual_created_by_user_id
                WHERE p.id = %s
                LIMIT 1
                """,
                (property_id,),
            )
            base = cur.fetchone()

            cur.execute(
                """
                SELECT
                    ss.id,
                    ss.owner_name,
                    ss.owner_phone,
                    ss.private_address,
                    ss.telegram_user_id,
                    ss.status,
                    au.display_name,
                    au.username
                FROM seller_submissions ss
                LEFT JOIN users au
                  ON au.id = ss.approved_by_user_id
                WHERE ss.property_id = %s
                ORDER BY ss.id DESC
                LIMIT 1
                """,
                (property_id,),
            )
            seller = cur.fetchone()

    if not base and not seller:
        return None

    result = {
        "source_kind": base[0] if base else None,
        "registrar_user_id": base[1] if base else None,
        "registrar_display_name": base[2] if base else None,
        "registrar_username": base[3] if base else None,
        "registrar_role": base[4] if base else None,
        "registrar_telegram_id": base[5] if base else None,
        "seller": None,
    }

    if seller:
        result["seller"] = {
            "submission_id": seller[0],
            "owner_name": seller[1],
            "owner_phone": seller[2],
            "private_address": seller[3],
            "telegram_user_id": seller[4],
            "status": seller[5],
            "approved_by": seller[6] or seller[7],
        }

    return result


def build_unit_code_search_text(
    property_row,
    source_rows,
    session=None,
):
    (
        property_id,
        unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        build_year,
        price_billion,
        status,
        published_message_id,
        channel_post_url,
    ) = property_row

    status_names = {
        "ACTIVE": "فعال ✅",
        "SOLD": "فروخته شده 🔴",
        "INACTIVE": "غیرفعال ⛔",
        "INVALID": "نامعتبر ⚠️",
        "DUPLICATE": "تکراری 🧩",
    }

    lines = [
        f"🔎 اطلاعات کد واحد {unit_code}",
        "",
    ]

    location = (
        address_text
        or district
        or "ثبت نشده"
    )

    lines.append(
        f"📍 {location}"
    )

    if area_m2 is not None:
        area = float(area_m2)

        if area.is_integer():
            area = int(area)

        lines.append(
            f"📐 {area} متر"
        )

    if bedrooms is not None:
        lines.append(
            f"🛏 {bedrooms} خواب"
        )

    if build_year:
        lines.append(
            f"📅 سال ساخت {build_year}"
        )

    if price_billion is not None:
        price = float(price_billion)

        if price.is_integer():
            price_text = str(int(price))
        else:
            price_text = (
                f"{price:.3f}"
                .rstrip("0")
                .rstrip(".")
            )

        lines.append(
            f"💰 قیمت: {price_text} میلیارد تومان"
        )

    lines.append(
        "📌 وضعیت: "
        + status_names.get(
            status,
            status or "نامشخص",
        )
    )

    if channel_post_url:
        lines.extend(
            [
                "",
                "📢 پست منتشرشده در کانال ما:",
                channel_post_url,
            ]
        )

    attribution = get_property_attribution_detail(property_id)
    if attribution:
        source_kind = (attribution.get("source_kind") or "").upper()
        seller = attribution.get("seller")
        registrar_name = (
            attribution.get("registrar_display_name")
            or attribution.get("registrar_username")
        )

        if seller or source_kind in {"MANUAL", "CUSTOMER"} or registrar_name:
            lines.extend(["", "━━━━━━━━━━━━━━━━━━━━", "👥 مالکیت / ثبت فایل"])

        if seller or source_kind == "CUSTOMER":
            lines.append("📥 مسیر ورود: فروشنده از ShahFile")
            if seller:
                lines.append(f"🆔 Submission: {seller.get('submission_id') or '-'}")
                if seller.get("approved_by"):
                    lines.append(f"✅ تأییدکننده: {seller['approved_by']}")

                viewer_role = session[3] if session and len(session) > 3 else None
                if viewer_role in ("OWNER", "ADMIN"):
                    lines.append(f"👤 مالک: {seller.get('owner_name') or '-'}")
                    lines.append(f"📞 شماره مالک: {seller.get('owner_phone') or '-'}")
                    lines.append(f"📌 آدرس خصوصی: {seller.get('private_address') or '-'}")
                    lines.append(f"🆔 Telegram فروشنده: {seller.get('telegram_user_id') or '-'}")
                else:
                    lines.append("🔐 اطلاعات خصوصی مالک فقط برای مدیران نمایش داده می‌شود.")

        elif source_kind == "MANUAL" or registrar_name:
            role_names = {
                "OWNER": "مدیر اصلی",
                "ADMIN": "ادمین",
                "ADVISOR": "مشاور",
                "USER": "کاربر",
            }
            lines.append("📥 مسیر ورود: افزودن فایل مدیریتی")
            lines.append(
                "🧑‍💼 ثبت‌کننده/مشاور: "
                + (registrar_name or "نامشخص")
            )
            if attribution.get("registrar_role"):
                lines.append(
                    "🔑 نقش: "
                    + role_names.get(
                        attribution.get("registrar_role"),
                        attribution.get("registrar_role"),
                    )
                )
            if attribution.get("registrar_telegram_id"):
                lines.append(
                    f"🆔 Telegram ID ثبت‌کننده: {attribution['registrar_telegram_id']}"
                )

    if source_rows:
        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━",
                "📡 منبع فایل",
            ]
        )

        for index, source in enumerate(
            source_rows,
            start=1,
        ):
            (
                source_unit_code,
                source_message_id,
                source_is_active,
                channel_title,
                channel_username,
                telegram_channel_id,
                source_post_url,
            ) = source

            if len(source_rows) > 1:
                lines.append(
                    f"\nمنبع {index}:"
                )

            lines.append(
                "🏷 کانال: "
                + (
                    channel_title
                    or "نامشخص"
                )
            )

            if channel_username:
                username = str(
                    channel_username
                ).lstrip("@")

                lines.append(
                    f"👤 @{username}"
                )

            lines.append(
                "🔢 کد فایل در منبع: "
                + (
                    str(source_unit_code)
                    if source_unit_code
                    else "ندارد"
                )
            )

            if source_message_id:
                lines.append(
                    f"✉️ Message ID منبع: {source_message_id}"
                )

            final_source_url = source_post_url

            if not final_source_url:
                final_source_url = (
                    build_source_post_url(
                        telegram_channel_id,
                        channel_username,
                        source_message_id,
                    )
                )

            if final_source_url:
                lines.extend(
                    [
                        "🔗 مشاهده پست منبع:",
                        final_source_url,
                    ]
                )
            else:
                lines.append(
                    "🔗 لینک مستقیم منبع در دسترس نیست."
                )

            lines.append(
                "وضعیت منبع: "
                + (
                    "فعال ✅"
                    if source_is_active
                    else "غیرفعال ⛔"
                )
            )

    elif attribution and source_kind in {"MANUAL", "CUSTOMER"}:
        # Internal/manual and ShahFile seller files already have a stronger
        # attribution section above; do not append a misleading "unknown source".
        pass

    else:
        try:
            from visit_leads import get_operator_source_context

            source = get_operator_source_context(
                property_id
            )

        except Exception:
            logging.exception(
                "Unit search source fallback failed: "
                "property_id=%s",
                property_id,
            )
            source = None

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━",
                "📡 منبع فایل",
            ]
        )

        if source:
            source_name = (
                source.get("source_name")
                or "ثبت نشده"
            )

            source_code = source.get(
                "source_unit_code"
            )

            lines.append(
                f"🏷 منبع: {source_name}"
            )

            lines.append(
                "🔢 کد فایل در منبع: "
                + (
                    str(source_code)
                    if source_code
                    else "ثبت نشده"
                )
            )

            direct_url = source.get(
                "direct_post_url"
            )

            channel_url = source.get(
                "channel_url"
            )

            if direct_url:
                lines.extend(
                    [
                        "🔗 مشاهده پست منبع:",
                        direct_url,
                    ]
                )

            elif channel_url:
                lines.extend(
                    [
                        "🔗 ورود به کانال منبع:",
                        channel_url,
                    ]
                )

            else:
                lines.append(
                    "🔗 لینک منبع در دسترس نیست."
                )

        else:
            lines.append(
                "⚠️ منبع این فایل قابل تشخیص نیست."
            )

    return "\n".join(lines)


def find_property_by_unit_code(unit_code):
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
                    current_price_billion,
                    published_message_id,
                    channel_post_url
                FROM properties
                WHERE unit_code = %s
                LIMIT 1
                """,
                (unit_code,),
            )

            return cur.fetchone()

def urgent_confirm_keyboard(property_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ همین الان منتشر شود",
                    callback_data=f"urgent_confirm_{property_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ انصراف",
                    callback_data="home",
                )
            ],
        ]
    )



# === DUPLICATE SIMILAR VIEW LINK V1 ===
def get_duplicate_property_view_url(
    property_id,
):
    """
    Return the best available link for the OLD/similar property:
    1) destination channel post
    2) active source post
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(
                        NULLIF(
                            BTRIM(old.channel_post_url),
                            ''
                        ),
                        (
                            SELECT
                                NULLIF(
                                    BTRIM(ps.source_post_url),
                                    ''
                                )
                            FROM property_sources ps
                            WHERE
                                ps.property_id = old.id
                                AND ps.is_active = TRUE
                                AND ps.source_post_url
                                    IS NOT NULL
                                AND BTRIM(
                                    ps.source_post_url
                                ) <> ''
                            ORDER BY
                                ps.is_primary DESC,
                                ps.id DESC
                            LIMIT 1
                        )
                    )
                FROM duplicate_reviews dr
                LEFT JOIN properties old
                  ON old.id =
                     dr.duplicate_of_property_id
                WHERE dr.property_id = %s
                ORDER BY dr.id DESC
                LIMIT 1
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        return None

    return row[0]


def urgent_duplicate_keyboard(property_id):
    buttons = []

    similar_url = (
        get_duplicate_property_view_url(
            property_id
        )
    )

    if similar_url:
        buttons.append(
            [
                InlineKeyboardButton(
                    "👁 مشاهده فایل مشابه",
                    url=similar_url,
                )
            ]
        )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "✅ بله، منتشر کن",
                    callback_data=(
                        f"urgent_dup_approve_"
                        f"{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ خیر، منتشر نشود",
                    callback_data=(
                        f"urgent_dup_reject_"
                        f"{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت",
                    callback_data="home",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(buttons)

def get_duplicate_property_summary(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.current_price_billion,
                    (
                        SELECT ps.source_unit_code
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                          AND ps.source_unit_code IS NOT NULL
                          AND ps.source_unit_code <> ''
                        ORDER BY ps.id DESC
                        LIMIT 1
                    )
                FROM properties p
                WHERE p.id = %s
                """,
                (property_id,),
            )

            return cur.fetchone()


def build_duplicate_warning_text(property_id, duplicate):
    similar_id = duplicate.get(
        "duplicate_of_property_id"
    )

    row = get_duplicate_property_summary(
        similar_id
    )

    lines = [
        "⚠️ این فایل احتمالاً تکراری است.",
        "",
    ]

    if row:
        (
            db_id,
            unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            source_code,
        ) = row

        lines.append("فایل مشابه قبلی:")

        if source_code:
            lines.append(
                f"کد فایل منبع: {source_code}"
            )

        if unit_code:
            lines.append(
                f"کد واحد: {unit_code}"
            )

        location = (
            address_text
            or district
            or "بدون آدرس"
        )

        lines.append(
            f"📍 {location}"
        )

        if area_m2 is not None:
            area = float(area_m2)

            if area.is_integer():
                area = int(area)

            lines.append(
                f"📐 {area} متر"
            )

        if price_billion is not None:
            price = float(price_billion)
            price_text = (
                f"{price:.3f}"
                .rstrip("0")
                .rstrip(".")
            )

            lines.append(
                f"💰 {price_text} میلیارد"
            )

    score = duplicate.get(
        "similarity_score"
    )

    reason = duplicate.get(
        "match_reason"
    )

    if score is not None:
        lines.extend(
            [
                "",
                f"درجه شباهت: {score} از 100",
            ]
        )

    if reason:
        lines.append(
            f"دلیل: {reason}"
        )

    lines.extend(
        [
            "",
            "آیا با این حال همین فایل منتشر شود؟",
        ]
    )

    return "\n".join(lines)

def urgent_candidates_keyboard(rows):
    buttons = []

    for row in rows:
        (
            property_id,
            district,
            area_m2,
            price_billion,
            published_message_id,
            unit_code,
        ) = row

        district_text = district or "بدون منطقه"

        if area_m2 is not None:
            area_text = str(int(area_m2))
        else:
            area_text = "؟"

        if price_billion is not None:
            price_text = str(float(price_billion)).rstrip("0").rstrip(".")
        else:
            price_text = "؟"

        buttons.append(
            [
                InlineKeyboardButton(
                    f"{district_text} | {area_text}م | {price_text} میلیارد",
                    callback_data=f"urgent_select_{property_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)

def db_connect():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def get_session(telegram_user_id: int):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.display_name, u.role
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.telegram_user_id = %s
                  AND s.is_active = TRUE
                  AND u.is_active = TRUE
                """,
                (telegram_user_id,),
            )
            row = cur.fetchone()

            if row:
                cur.execute(
                    """
                    UPDATE user_sessions
                    SET last_activity_at = NOW()
                    WHERE telegram_user_id = %s
                    """,
                    (telegram_user_id,),
                )
                conn.commit()

            return row


def authenticate(username: str, password: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, display_name, role
                FROM users
                WHERE username = %s
                  AND is_active = TRUE
                """,
                (username,),
            )
            row = cur.fetchone()

            if not row:
                return None

            user_id, db_username, password_hash, display_name, role = row

            if not bcrypt.checkpw(
                password.encode("utf-8"),
                password_hash.encode("utf-8"),
            ):
                return None

            return {
                "id": user_id,
                "username": db_username,
                "display_name": display_name,
                "role": role,
            }


def create_session(user_id: int, telegram_user_id: int):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_sessions SET is_active = FALSE WHERE user_id = %s",
                (user_id,),
            )

            cur.execute(
                """
                INSERT INTO user_sessions
                    (user_id, telegram_user_id, logged_in_at, last_activity_at, is_active)
                VALUES
                    (%s, %s, NOW(), NOW(), TRUE)
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    logged_in_at = NOW(),
                    last_activity_at = NOW(),
                    is_active = TRUE
                """,
                (user_id, telegram_user_id),
            )
            conn.commit()


def logout_session(telegram_user_id: int):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_sessions
                SET is_active = FALSE
                WHERE telegram_user_id = %s
                """,
                (telegram_user_id,),
            )
            conn.commit()


def save_channel(
    telegram_channel_id,
    title,
    username,
    channel_type,
    added_by_user_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channels
                    (
                        telegram_channel_id,
                        title,
                        username,
                        channel_type,
                        access_mode,
                        added_by_user_id,
                        last_verified_at,
                        is_active
                    )
                VALUES
                    (%s, %s, %s, %s, 'BOT', %s, NOW(), TRUE)
                ON CONFLICT (telegram_channel_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    channel_type = EXCLUDED.channel_type,
                    added_by_user_id = EXCLUDED.added_by_user_id,
                    last_verified_at = NOW(),
                    is_active = TRUE,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    telegram_channel_id,
                    title,
                    username,
                    channel_type,
                    added_by_user_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0]


def get_active_source_channel_ids():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telegram_channel_id
                FROM channels
                WHERE channel_type = 'SOURCE'
                  AND is_active = TRUE
                """
            )

            return {
                int(row[0])
                for row in cur.fetchall()
            }


def get_channels():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    telegram_channel_id,
                    title,
                    username,
                    channel_type,
                    is_active
                FROM channels
                ORDER BY channel_type, id
                """
            )
            return cur.fetchall()




def normalize_category_digits(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return str(text).translate(table)


def parse_category_range(text):
    text = normalize_category_digits(text)

    text = (
        text
        .replace("الی", "تا")
        .replace("–", "-")
        .replace("—", "-")
    )

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:تا|-)\s*(\d+(?:\.\d+)?)",
        text,
    )

    if not match:
        return None

    low = float(match.group(1))
    high = float(match.group(2))

    if low < 0 or high <= low:
        return None

    return low, high


def format_category_number(value):
    value = float(value)

    if value.is_integer():
        return str(int(value))

    return (
        f"{value:.3f}"
        .rstrip("0")
        .rstrip(".")
    )


def build_category_hashtag(low, high):
    low_text = (
        format_category_number(low)
        .replace(".", "_")
    )

    high_text = (
        format_category_number(high)
        .replace(".", "_")
    )

    return (
        f"#آپارتمان_{low_text}_{high_text}"
    )



def normalize_category_location(value):
    if not value:
        return ""

    text = str(value).strip().lower()

    text = (
        text
        .replace("|", " ")
        .replace("-", " ")
        .replace("ـ", " ")
        .replace("،", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("؛", " ")
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return text


def category_similarity_score(row_a, row_b):
    """
    Similarity score from 0 to 100.

    40 points: location/address
    20 points: area
    10 points: bedrooms
    10 points: build year
    20 points: price
    """

    score = 0

    # Row structure:
    # 0 id
    # 1 unit_code
    # 2 district
    # 3 address_text
    # 4 area_m2
    # 5 bedrooms
    # 6 build_year
    # 7 current_price_billion
    # 8 current_price_toman
    # 9 channel_post_url
    # 10 published_message_id

    location_a = normalize_category_location(
        row_a[3] or row_a[2]
    )

    location_b = normalize_category_location(
        row_b[3] or row_b[2]
    )

    if location_a and location_b:
        if location_a == location_b:
            score += 40
        elif (
            location_a in location_b
            or location_b in location_a
        ):
            score += 35

    area_a = row_a[4]
    area_b = row_b[4]

    if (
        area_a is not None
        and area_b is not None
    ):
        diff = abs(
            float(area_a)
            - float(area_b)
        )

        if diff == 0:
            score += 20
        elif diff <= 2:
            score += 15

    bedrooms_a = row_a[5]
    bedrooms_b = row_b[5]

    if (
        bedrooms_a is not None
        and bedrooms_b is not None
        and bedrooms_a == bedrooms_b
    ):
        score += 10

    year_a = row_a[6]
    year_b = row_b[6]

    if (
        year_a
        and year_b
        and str(year_a) == str(year_b)
    ):
        score += 10

    price_a = row_a[8]
    price_b = row_b[8]

    if (
        price_a is not None
        and price_b is not None
    ):
        a = float(price_a)
        b = float(price_b)

        base = max(a, b)

        if base > 0:
            percent_diff = (
                abs(a - b)
                / base
            )

            if percent_diff <= 0.02:
                score += 20
            elif percent_diff <= 0.05:
                score += 10

    return score


def dedupe_category_rows(
    rows,
    threshold=90,
):
    if not rows:
        return []

    property_ids = [
        int(row[0])
        for row in rows
    ]

    source_keys = {}

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (property_id)
                    property_id,
                    source_channel_id,
                    source_unit_code
                FROM property_sources
                WHERE property_id = ANY(%s)
                  AND is_active = TRUE
                  AND source_unit_code IS NOT NULL
                  AND BTRIM(source_unit_code) <> ''
                ORDER BY
                    property_id,
                    is_primary DESC,
                    id DESC
                """,
                (property_ids,),
            )

            for (
                property_id,
                source_channel_id,
                source_unit_code,
            ) in cur.fetchall():
                source_keys[int(property_id)] = (
                    int(source_channel_id),
                    str(source_unit_code).strip(),
                )

    kept = []

    for candidate in rows:
        duplicate_index = None

        candidate_property_id = int(
            candidate[0]
        )
        candidate_source_key = (
            source_keys.get(
                candidate_property_id
            )
        )

        for index, existing in enumerate(
            kept
        ):
            existing_property_id = int(
                existing[0]
            )
            existing_source_key = (
                source_keys.get(
                    existing_property_id
                )
            )

            # Exact source match = definite duplicate.
            if (
                candidate_source_key
                and existing_source_key
                and candidate_source_key
                == existing_source_key
            ):
                duplicate_index = index
                break

            # Fallback for files without a reliable source code.
            score = category_similarity_score(
                candidate,
                existing,
            )

            if score >= threshold:
                duplicate_index = index
                break

        if duplicate_index is None:
            kept.append(candidate)
            continue

        existing = kept[
            duplicate_index
        ]

        # Higher property ID = newer imported/published file.
        if int(candidate[0]) > int(existing[0]):
            kept[
                duplicate_index
            ] = candidate

    kept.sort(
        key=lambda row: (
            float(row[7])
            if row[7] is not None
            else float("inf"),
            int(row[0]),
        )
    )

    return kept



def get_published_properties_in_range(
    low,
    high,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.build_year,
                    p.current_price_billion,
                    p.current_price_toman,
                    p.channel_post_url,
                    p.published_message_id
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.channel_post_url IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.current_price_billion IS NOT NULL
                  AND p.current_price_billion >= %s
                  AND p.current_price_billion <= %s
                ORDER BY
                    p.current_price_billion ASC,
                    p.id ASC
                """,
                (
                    low,
                    high,
                ),
            )

            rows = cur.fetchall()

    return dedupe_category_rows(
        rows,
        threshold=90,
    )


def get_category_range_suggestions(
    limit=3,
):
    candidate_ranges = [
        (3, 5),
        (5, 8),
        (7, 10),
        (8, 12),
        (10, 15),
        (12, 15),
        (15, 20),
        (20, 25),
        (25, 30),
        (30, 40),
        (40, 50),
        (50, 70),
        (70, 100),
    ]

    results = []

    for low, high in candidate_ranges:
        rows = get_published_properties_in_range(
            low,
            high,
        )

        count = len(rows)

        if count > 0:
            results.append(
                {
                    "low": low,
                    "high": high,
                    "count": count,
                }
            )

    results.sort(
        key=lambda item: (
            -item["count"],
            item["high"] - item["low"],
            item["low"],
        )
    )

    selected = []

    for item in results:
        if len(selected) >= limit:
            break

        selected.append(item)

    return selected


def category_start_keyboard():
    suggestions = (
        get_category_range_suggestions(
            limit=3
        )
    )

    buttons = []

    for item in suggestions:
        low = item["low"]
        high = item["high"]
        count = item["count"]

        low_text = format_category_number(
            low
        )
        high_text = format_category_number(
            high
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"🔥 {low_text} تا {high_text} "
                        f"میلیارد — {count} فایل"
                    ),
                    callback_data=(
                        f"category_suggest_{low}_{high}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(
        buttons
    )


def category_pages_keyboard(total):
    buttons = []

    if total > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    "📤 ارسال دسته‌بندی به کانال",
                    callback_data="category_page_0",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(
        buttons
    )


def category_navigation_keyboard(
    category_post_id,
    offset,
    total,
    page_size=10,
):
    """
    Inline navigation shown under the category post itself.
    """
    total = int(total or 0)
    page_size = int(page_size or 10)
    offset = max(0, int(offset or 0))

    if total <= 0:
        return None

    total_pages = (
        total + page_size - 1
    ) // page_size

    current_page = (
        offset // page_size
    ) + 1

    last_offset = (
        total_pages - 1
    ) * page_size

    row = []

    if offset > 0:
        previous_offset = max(
            0,
            offset - page_size,
        )

        row.append(
            InlineKeyboardButton(
                "◀️ صفحه قبل",
                callback_data=(
                    f"catnav_{category_post_id}_"
                    f"{previous_offset}"
                ),
            )
        )

    row.append(
        InlineKeyboardButton(
            f"صفحه {current_page} از {total_pages}",
            callback_data=(
                f"catnav_{category_post_id}_"
                f"{offset}"
            ),
        )
    )

    if offset < last_offset:
        next_offset = min(
            last_offset,
            offset + page_size,
        )

        row.append(
            InlineKeyboardButton(
                "صفحه بعد ▶️",
                callback_data=(
                    f"catnav_{category_post_id}_"
                    f"{next_offset}"
                ),
            )
        )

    return InlineKeyboardMarkup(
        [row]
    )


def get_category_post_for_navigation(
    category_post_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    min_price_billion,
                    max_price_billion,
                    page_offset,
                    page_size,
                    telegram_channel_id,
                    telegram_message_id,
                    is_active
                FROM category_posts
                WHERE id = %s
                LIMIT 1
                """,
                (category_post_id,),
            )

            return cur.fetchone()


def update_category_post_offset(
    category_post_id,
    offset,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE category_posts
                SET
                    page_offset = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    int(offset),
                    category_post_id,
                ),
            )

            conn.commit()


def build_category_post_text(
    low,
    high,
    rows,
    offset=0,
    total=None,
    page_size=10,
):
    hashtag = build_category_hashtag(
        low,
        high,
    )

    low_text = format_category_number(low)
    high_text = format_category_number(high)

    lines = [
        hashtag,
        "",
        (
            "🏷 واحدهای بین قیمت "
            f"{low_text} تا {high_text} "
            "میلیارد تومان"
        ),
        "",
    ]

    # Always use the CURRENT destination username.
    # This keeps category links valid even if the
    # destination channel username changes later.
    destination = get_destination_channel()

    current_destination_username = None

    if destination:
        current_destination_username = (
            destination[2]
        )

        if current_destination_username:
            current_destination_username = (
                str(
                    current_destination_username
                ).lstrip("@")
            )

    for index, row in enumerate(
        rows,
        start=offset + 1,
    ):
        (
            property_id,
            unit_code,
            district,
            address_text,
            area_m2,
            bedrooms,
            build_year,
            price_billion,
            price_toman,
            channel_post_url,
            published_message_id,
        ) = row

        location = (
            address_text
            or district
            or "بدون منطقه"
        )

        lines.append(
            f"{index}) 📍 {location}"
        )

        detail_parts = []

        if area_m2 is not None:
            area = float(area_m2)

            if area.is_integer():
                area = int(area)

            detail_parts.append(
                f"📐 {area} متر"
            )

        if bedrooms is not None:
            detail_parts.append(
                f"🛏 {bedrooms} خواب"
            )

        if build_year:
            detail_parts.append(
                f"📅 سال ساخت {build_year}"
            )

        if detail_parts:
            lines.append(
                " | ".join(detail_parts)
            )

        if price_billion is not None:
            lines.append(
                "💰 قیمت: "
                f"{format_category_number(price_billion)} "
                "میلیارد تومان"
            )

        if unit_code:
            lines.append(
                f"🔢 کد واحد: {unit_code}"
            )

        final_post_url = channel_post_url

        if (
            current_destination_username
            and published_message_id
        ):
            final_post_url = (
                "https://t.me/"
                f"{current_destination_username}/"
                f"{published_message_id}"
            )

        if final_post_url:
            lines.append(
                f"🔗 مشاهده فایل: {final_post_url}"
            )

        lines.append("")

        if index != offset + len(rows):
            lines.append(
                "──────────────"
            )
            lines.append("")

    effective_total = (
        len(rows)
        if total is None
        else max(0, int(total))
    )

    if effective_total > int(page_size or 10):
        lines.extend([
            "",
            "برای رفتن به صفحه‌های بعدی از دکمه‌های زیر استفاده کنید 👇",
            "بعد از زدن دکمه، لطفاً چند ثانیه صبر کنید تا صفحه جدید نمایش داده شود.",
        ])

    return "\n".join(lines).strip()

def record_category_post(
    low,
    high,
    offset,
    destination_channel_id,
    telegram_message_id,
    hashtag,
    rows,
    created_by_user_id=None,
    is_active=True,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO category_posts
                (
                    min_price_billion,
                    max_price_billion,
                    page_offset,
                    page_size,
                    telegram_channel_id,
                    telegram_message_id,
                    hashtag,
                    created_by_user_id,
                    is_active
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    10,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                RETURNING id
                """,
                (
                    low,
                    high,
                    offset,
                    destination_channel_id,
                    telegram_message_id,
                    hashtag,
                    created_by_user_id,
                    bool(is_active),
                ),
            )

            category_post_id = (
                cur.fetchone()[0]
            )

            for position, row in enumerate(
                rows,
                start=1,
            ):
                property_id = row[0]
                price_toman = row[8]

                cur.execute(
                    """
                    INSERT INTO category_post_items
                    (
                        category_post_id,
                        property_id,
                        position,
                        snapshot_price_toman
                    )
                    VALUES
                    (%s, %s, %s, %s)
                    """,
                    (
                        category_post_id,
                        property_id,
                        position,
                        (
                            int(price_toman)
                            if price_toman is not None
                            else None
                        ),
                    ),
                )

            conn.commit()

            return category_post_id


def activate_category_post_delivery(
    category_post_id,
    destination_channel_id,
    telegram_message_id,
):
    """Finalize a reserved category after Telegram accepts the post."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE category_posts
                SET
                    telegram_channel_id = %s,
                    telegram_message_id = %s,
                    is_active = TRUE,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    destination_channel_id,
                    telegram_message_id,
                    category_post_id,
                ),
            )

            if cur.rowcount != 1:
                raise RuntimeError(
                    "Reserved category post could not be activated"
                )

            conn.commit()


WEEKDAY_NAMES = {
    0: "دوشنبه",
    1: "سه‌شنبه",
    2: "چهارشنبه",
    3: "پنجشنبه",
    4: "جمعه",
    5: "شنبه",
    6: "یکشنبه",
}


def schedule_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ افزودن بازه",
                    callback_data="schedule_add",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 بازه‌های ثبت‌شده",
                    callback_data="schedule_list",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت به تنظیمات",
                    callback_data="settings",
                )
            ],
        ]
    )


def schedule_weekday_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 همه روزها",
                    callback_data="schedule_day_all",
                )
            ],
            [
                InlineKeyboardButton(
                    "شنبه",
                    callback_data="schedule_day_5",
                ),
                InlineKeyboardButton(
                    "یکشنبه",
                    callback_data="schedule_day_6",
                ),
            ],
            [
                InlineKeyboardButton(
                    "دوشنبه",
                    callback_data="schedule_day_0",
                ),
                InlineKeyboardButton(
                    "سه‌شنبه",
                    callback_data="schedule_day_1",
                ),
            ],
            [
                InlineKeyboardButton(
                    "چهارشنبه",
                    callback_data="schedule_day_2",
                ),
                InlineKeyboardButton(
                    "پنجشنبه",
                    callback_data="schedule_day_3",
                ),
            ],
            [
                InlineKeyboardButton(
                    "جمعه",
                    callback_data="schedule_day_4",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت",
                    callback_data="schedule",
                )
            ],
        ]
    )


def get_schedule_windows():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    weekday,
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts,
                    is_active
                FROM schedule_windows
                ORDER BY
                    weekday,
                    start_time,
                    id
                """
            )

            return cur.fetchall()




def create_schedule_window(
    weekday,
    start_time,
    end_time,
    interval_minutes,
    max_posts,
    group_key=None,
    pause_start=None,
    pause_end=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule_windows
                (
                    weekday,
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts,
                    is_active,
                    group_key,
                    pause_start,
                    pause_end
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    %s,
                    %s,
                    %s
                )
                RETURNING id
                """,
                (
                    weekday,
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts,
                    group_key,
                    pause_start,
                    pause_end,
                ),
            )

            row = cur.fetchone()
            conn.commit()

            return row[0]

def toggle_schedule_window(window_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE schedule_windows
                SET
                    is_active = NOT is_active,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING is_active
                """,
                (window_id,),
            )

            row = cur.fetchone()
            conn.commit()

            return row[0] if row else None




def update_schedule_window(
    window_id,
    weekday,
    start_time,
    end_time,
    interval_minutes,
    max_posts,
    pause_start=None,
    pause_end=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE schedule_windows
                SET
                    weekday = %s,
                    start_time = %s,
                    end_time = %s,
                    interval_minutes = %s,
                    max_posts = %s,
                    pause_start = %s,
                    pause_end = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (
                    weekday,
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts,
                    pause_start,
                    pause_end,
                    window_id,
                ),
            )

            row = cur.fetchone()
            conn.commit()

            return row[0] if row else None

def delete_schedule_window(window_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM schedule_windows
                WHERE id = %s
                """,
                (window_id,),
            )

            deleted = cur.rowcount > 0
            conn.commit()

            return deleted




def get_grouped_schedule_windows():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    weekday,
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts,
                    is_active,
                    group_key,
                    pause_start,
                    pause_end
                FROM schedule_windows
                ORDER BY
                    weekday,
                    start_time,
                    id
                """
            )

            rows = cur.fetchall()

    grouped = {}
    singles = []

    for row in rows:
        (
            window_id,
            weekday,
            start_time,
            end_time,
            interval_minutes,
            max_posts,
            is_active,
            group_key,
            pause_start,
            pause_end,
        ) = row

        if not group_key:
            singles.append(
                {
                    "kind": "single",
                    "id": window_id,
                    "weekday": weekday,
                    "start_time": start_time,
                    "end_time": end_time,
                    "interval_minutes": interval_minutes,
                    "max_posts": max_posts,
                    "is_active": is_active,
                    "pause_start": pause_start,
                    "pause_end": pause_end,
                }
            )
            continue

        item = grouped.setdefault(
            group_key,
            {
                "kind": "group",
                "group_key": group_key,
                "ids": [],
                "weekdays": [],
                "start_time": start_time,
                "end_time": end_time,
                "interval_minutes": interval_minutes,
                "max_posts": max_posts,
                "is_active": True,
                "pause_start": pause_start,
                "pause_end": pause_end,
            },
        )

        item["ids"].append(window_id)
        item["weekdays"].append(weekday)

        if not is_active:
            item["is_active"] = False

    result = list(grouped.values())
    result.extend(singles)

    return result


def update_schedule_group(
    group_key,
    start_time,
    end_time,
    interval_minutes,
    max_posts,
    pause_start=None,
    pause_end=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE schedule_windows
                SET
                    start_time = %s,
                    end_time = %s,
                    interval_minutes = %s,
                    max_posts = %s,
                    pause_start = %s,
                    pause_end = %s,
                    updated_at = NOW()
                WHERE group_key = %s
                """,
                (
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts,
                    pause_start,
                    pause_end,
                    group_key,
                ),
            )

            count = cur.rowcount
            conn.commit()

            return count

def toggle_schedule_group(group_key):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT BOOL_AND(is_active)
                FROM schedule_windows
                WHERE group_key = %s
                """,
                (group_key,),
            )

            row = cur.fetchone()

            currently_active = bool(
                row and row[0]
            )

            new_value = not currently_active

            cur.execute(
                """
                UPDATE schedule_windows
                SET
                    is_active = %s,
                    updated_at = NOW()
                WHERE group_key = %s
                """,
                (
                    new_value,
                    group_key,
                ),
            )

            conn.commit()

            return new_value


def delete_schedule_group(group_key):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM schedule_windows
                WHERE group_key = %s
                """,
                (group_key,),
            )

            deleted = cur.rowcount
            conn.commit()

            return deleted




def format_schedule_windows():
    rows = get_grouped_schedule_windows()

    if not rows:
        return (
            "⏰ زمان‌بندی انتشار\n\n"
            "هنوز هیچ بازه‌ای ثبت نشده است."
        )

    lines = [
        "⏰ زمان‌بندی انتشار",
        "",
        "منطقه زمانی: تهران",
        "",
    ]

    number = 1

    for item in rows:

        status = (
            "🟢 فعال"
            if item["is_active"]
            else "🔴 غیرفعال"
        )

        if item["kind"] == "group":
            day_name = "📅 همه روزها"
        else:
            day_name = WEEKDAY_NAMES.get(
                item["weekday"],
                str(item["weekday"]),
            )

        start_text = item[
            "start_time"
        ].strftime("%H:%M")

        end_text = item[
            "end_time"
        ].strftime("%H:%M")

        max_text = (
            str(item["max_posts"])
            if item["max_posts"] is not None
            else "نامحدود"
        )

        pause_start = item.get(
            "pause_start"
        )

        pause_end = item.get(
            "pause_end"
        )

        lines.extend(
            [
                f"#{number} | {day_name}",
                status,
                f"🕐 {start_text} تا {end_text}",
            ]
        )

        if pause_start and pause_end:
            lines.append(
                "⏸ توقف انتشار: "
                f"{pause_start.strftime('%H:%M')} "
                f"تا {pause_end.strftime('%H:%M')}"
            )
        else:
            lines.append(
                "⏸ توقف انتشار: ندارد"
            )

        lines.extend(
            [
                (
                    "⏱ فاصله انتشار: "
                    f"{item['interval_minutes']} دقیقه"
                ),
                f"📦 سقف پست: {max_text}",
                "────────────",
            ]
        )

        number += 1

    return "\n".join(lines)

def schedule_windows_keyboard():
    rows = get_grouped_schedule_windows()

    buttons = []

    number = 1

    for item in rows:

        if item["kind"] == "group":

            group_key = item[
                "group_key"
            ]

            buttons.append(
                [
                    InlineKeyboardButton(
                        f"✏️ ویرایش همه روزها #{number}",
                        callback_data=(
                            f"schedule_edit_group_{group_key}"
                        ),
                    )
                ]
            )

            toggle_text = (
                f"🔴 غیرفعال کردن همه روزها #{number}"
                if item["is_active"]
                else
                f"🟢 فعال کردن همه روزها #{number}"
            )

            buttons.append(
                [
                    InlineKeyboardButton(
                        toggle_text,
                        callback_data=(
                            f"schedule_toggle_group_{group_key}"
                        ),
                    ),
                    InlineKeyboardButton(
                        f"🗑 حذف همه روزها #{number}",
                        callback_data=(
                            f"schedule_delete_group_{group_key}"
                        ),
                    ),
                ]
            )

        else:

            window_id = item["id"]

            buttons.append(
                [
                    InlineKeyboardButton(
                        f"✏️ ویرایش #{number}",
                        callback_data=(
                            f"schedule_edit_{window_id}"
                        ),
                    )
                ]
            )

            toggle_text = (
                f"🔴 غیرفعال کردن #{number}"
                if item["is_active"]
                else
                f"🟢 فعال کردن #{number}"
            )

            buttons.append(
                [
                    InlineKeyboardButton(
                        toggle_text,
                        callback_data=(
                            f"schedule_toggle_{window_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        f"🗑 حذف #{number}",
                        callback_data=(
                            f"schedule_delete_{window_id}"
                        ),
                    ),
                ]
            )

        number += 1

    buttons.append(
        [
            InlineKeyboardButton(
                "➕ افزودن بازه",
                callback_data="schedule_add",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="schedule",
            )
        ]
    )

    return InlineKeyboardMarkup(
        buttons
    )

def parse_schedule_time(value):
    from datetime import datetime

    value = value.strip()

    try:
        parsed = datetime.strptime(
            value,
            "%H:%M",
        )

        return parsed.time()

    except ValueError:
        return None



def get_pending_duplicate_properties():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.current_price_billion,
                    dr.duplicate_of_property_id,
                    dr.similarity_score,
                    dr.match_reason,
                    q.created_at
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                JOIN duplicate_reviews dr
                  ON dr.property_id = p.id
                WHERE q.queue_status = 'PENDING_DUPLICATE'
                  AND dr.review_status = 'PENDING'
                ORDER BY
                    q.created_at ASC,
                    p.id ASC
                """
            )

            return cur.fetchall()


def get_pending_duplicate_detail(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.floor_text,
                    p.build_year,
                    p.current_price_billion,
                    dr.duplicate_of_property_id,
                    dr.similarity_score,
                    dr.match_reason,
                    old.unit_code,
                    old.district,
                    old.address_text,
                    old.area_m2,
                    old.current_price_billion
                FROM properties p
                JOIN duplicate_reviews dr
                  ON dr.property_id = p.id
                LEFT JOIN properties old
                  ON old.id = dr.duplicate_of_property_id
                WHERE p.id = %s
                  AND dr.review_status = 'PENDING'
                LIMIT 1
                """,
                (property_id,),
            )

            return cur.fetchone()


def duplicate_queue_list_keyboard():
    rows = get_pending_duplicate_properties()

    buttons = []

    for row in rows:
        (
            property_id,
            unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            duplicate_of_property_id,
            similarity_score,
            match_reason,
            created_at,
        ) = row

        location = (
            address_text
            or district
            or "بدون لوکیشن"
        )

        area_text = (
            f"{area_m2}م"
            if area_m2 is not None
            else "متراژ؟"
        )

        price_text = (
            f"{price_billion:g}"
            if price_billion is not None
            else "قیمت؟"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"⚠️ {location} | "
                        f"{area_text} | "
                        f"{price_text} میلیارد"
                    )[:60],
                    callback_data=(
                        f"dupqueue_view_{property_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data="duplicate_queue",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(
        buttons
    )


# === DUPLICATE QUEUE SIMILAR VIEW LINK V1 ===
def duplicate_queue_decision_keyboard(
    property_id,
):
    buttons = []

    similar_url = (
        get_duplicate_property_view_url(
            property_id
        )
    )

    if similar_url:
        buttons.append(
            [
                InlineKeyboardButton(
                    "👁 مشاهده فایل مشابه",
                    url=similar_url,
                )
            ]
        )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "✅ تأیید و بازگشت به صف",
                    callback_data=(
                        f"dupqueue_approve_"
                        f"{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ رد فایل",
                    callback_data=(
                        f"dupqueue_reject_"
                        f"{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت به لیست",
                    callback_data="duplicate_queue",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(buttons)

def approve_duplicate_for_scheduler(
    property_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE duplicate_reviews
                SET
                    review_status = 'APPROVED',
                    reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE property_id = %s
                  AND review_status = 'PENDING'
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'READY',
                    scheduled_for = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status = 'PENDING_DUPLICATE'
                """,
                (property_id,),
            )

            conn.commit()


def reject_duplicate_for_scheduler(
    property_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE duplicate_reviews
                SET
                    review_status = 'REJECTED',
                    reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE property_id = %s
                  AND review_status = 'PENDING'
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'SKIPPED',
                    last_error = 'Duplicate rejected by reviewer',
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status = 'PENDING_DUPLICATE'
                """,
                (property_id,),
            )

            conn.commit()


def build_duplicate_queue_detail(row):
    (
        property_id,
        unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        floor_text,
        build_year,
        price_billion,
        duplicate_of_property_id,
        similarity_score,
        match_reason,
        old_unit_code,
        old_district,
        old_address_text,
        old_area_m2,
        old_price_billion,
    ) = row

    location = (
        address_text
        or district
        or "ثبت نشده"
    )

    old_location = (
        old_address_text
        or old_district
        or "ثبت نشده"
    )

    unit_text = (
        str(unit_code)
        if unit_code
        else "هنوز اختصاص داده نشده"
    )

    old_unit_text = (
        str(old_unit_code)
        if old_unit_code
        else "نامشخص"
    )

    area_text = (
        str(area_m2)
        if area_m2 is not None
        else "نامشخص"
    )

    old_area_text = (
        str(old_area_m2)
        if old_area_m2 is not None
        else "نامشخص"
    )

    price_text = (
        f"{price_billion:g} میلیارد"
        if price_billion is not None
        else "نامشخص"
    )

    old_price_text = (
        f"{old_price_billion:g} میلیارد"
        if old_price_billion is not None
        else "نامشخص"
    )

    return (
        "⚠️ بررسی فایل مشکوک به تکراری\n\n"
        "فایل جدید:\n"
        f"کد واحد: {unit_text}\n"
        f"📍 {location}\n"
        f"📐 {area_text} متر\n"
        f"💰 {price_text}\n\n"
        "فایل مشابه قبلی:\n"
        f"کد واحد: {old_unit_text}\n"
        f"📍 {old_location}\n"
        f"📐 {old_area_text} متر\n"
        f"💰 {old_price_text}\n\n"
        f"درصد شباهت: {similarity_score}%\n"
        f"دلیل تشخیص: {match_reason or 'ثبت نشده'}\n\n"
        "در صورت تأیید، فایل به صف زمان‌بندی "
        "برمی‌گردد و در زمان مناسب منتشر می‌شود."
    )



QUEUE_STATUS_LABELS = {
    "NEW": "🆕 جدید",
    "PARSED": "🟡 پردازش‌شده",
    "READY": "🟢 آماده",
    "SCHEDULED": "⏰ زمان‌بندی‌شده",
    "PENDING_DUPLICATE": "🧩 تکراری منتظر تأیید",
    "FAILED": "❌ خطادار",
    "SKIPPED": "⏭ ردشده",
    "PUBLISHED": "✅ منتشرشده",
}


def get_real_publish_queue(limit=10, offset=0):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.property_id,
                    q.queue_status,
                    q.scheduled_for,
                    q.attempts,
                    q.last_error,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.current_price_billion,
                    (
                        SELECT ps.source_unit_code
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                        ORDER BY
                            ps.is_primary DESC,
                            ps.id ASC
                        LIMIT 1
                    ) AS source_unit_code
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                WHERE q.queue_status IN (
                    'NEW',
                    'PARSED',
                    'READY',
                    'SCHEDULED',
                    'PENDING_DUPLICATE',
                    'FAILED'
                )
                ORDER BY
                    COALESCE(q.priority, 0) DESC,
                    q.created_at ASC,
                    q.id ASC
                LIMIT %s
                OFFSET %s
                """,
                (limit, offset),
            )

            return cur.fetchall()



def get_failed_queue_properties():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.property_id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.current_price_billion,
                    q.attempts,
                    q.last_error,
                    (
                        SELECT ps.source_unit_code
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                        ORDER BY
                            ps.is_primary DESC,
                            ps.id ASC
                        LIMIT 1
                    )
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                WHERE q.queue_status = 'FAILED'
                ORDER BY
                    q.updated_at DESC,
                    q.id DESC
                """
            )

            return cur.fetchall()


def get_failed_queue_detail(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.property_id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.floor_text,
                    p.build_year,
                    p.current_price_billion,
                    q.attempts,
                    q.last_error,
                    p.raw_text,
                    (
                        SELECT ps.source_unit_code
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                        ORDER BY
                            ps.is_primary DESC,
                            ps.id ASC
                        LIMIT 1
                    )
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                WHERE q.property_id = %s
                  AND q.queue_status = 'FAILED'
                LIMIT 1
                """,
                (property_id,),
            )

            return cur.fetchone()


def failed_queue_list_keyboard():
    rows = get_failed_queue_properties()
    buttons = []

    for row in rows:
        (
            property_id,
            unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            attempts,
            last_error,
            source_unit_code,
        ) = row

        location = (
            address_text
            or district
            or "لوکیشن نامشخص"
        )

        location = str(location).strip()

        if len(location) > 28:
            location = location[:28] + "…"

        code_text = (
            f"کد {unit_code}"
            if unit_code
            else (
                f"منبع {source_unit_code}"
                if source_unit_code
                else f"ID {property_id}"
            )
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"❌ {code_text} | {location}",
                    callback_data=(
                        f"queue_failed_view_{property_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data="queue_failed",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به فایل‌ها",
                callback_data="files_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def failed_queue_actions_keyboard(property_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛠 بازخوانی و بازگشت به صف",
                    callback_data=(
                        f"queue_failed_repair_{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "🚨 تلاش فوری برای انتشار",
                    callback_data=(
                        f"queue_failed_publish_{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 حذف از صف",
                    callback_data=(
                        f"queue_failed_skip_{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت به فایل‌های خطادار",
                    callback_data="queue_failed",
                )
            ],
        ]
    )


def build_failed_queue_detail(row):
    (
        property_id,
        unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        floor_text,
        build_year,
        price_billion,
        attempts,
        last_error,
        raw_text,
        source_unit_code,
    ) = row

    location = (
        address_text
        or district
        or "ثبت نشده"
    )

    price_text = (
        f"{float(price_billion):g} میلیارد"
        if price_billion is not None
        else "ثبت نشده"
    )

    error_text = (
        str(last_error)
        if last_error
        else "متن خطا ثبت نشده"
    )

    if len(error_text) > 1200:
        error_text = error_text[:1200] + "…"

    return (
        "❌ فایل خطادار\n\n"
        f"شناسه داخلی: {property_id}\n"
        f"کد واحد: {unit_code or 'هنوز ندارد'}\n"
        f"کد منبع: {source_unit_code or 'ثبت نشده'}\n"
        f"📍 {location}\n"
        f"📐 متراژ: {area_m2 or 'ثبت نشده'}\n"
        f"🛏 خواب: {bedrooms if bedrooms is not None else 'ثبت نشده'}\n"
        f"🏢 طبقه: {floor_text or 'ثبت نشده'}\n"
        f"📅 سال ساخت: {build_year or 'ثبت نشده'}\n"
        f"💰 قیمت: {price_text}\n"
        f"🔁 تعداد تلاش: {attempts or 0}\n\n"
        f"⚠️ خطای ثبت‌شده:\n{error_text}"
    )


def set_failed_property_ready(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'READY',
                    last_error = NULL,
                    scheduled_for = NULL,
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status = 'FAILED'
                """,
                (property_id,),
            )

            changed = cur.rowcount > 0
            conn.commit()

    return changed


def skip_failed_property(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'SKIPPED',
                    last_error = 'Removed from queue by operator',
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status = 'FAILED'
                """,
                (property_id,),
            )

            changed = cur.rowcount > 0
            conn.commit()

    return changed


def repair_failed_property(property_id):
    latest_source_raw_text = None

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_raw_text
                FROM property_sources
                WHERE property_id = %s
                  AND source_raw_text IS NOT NULL
                  AND source_raw_text <> ''
                ORDER BY
                    is_primary DESC,
                    is_active DESC,
                    id DESC
                LIMIT 1
                """,
                (property_id,),
            )

            row = cur.fetchone()

            if row:
                latest_source_raw_text = row[0]

            if not latest_source_raw_text:
                cur.execute(
                    """
                    SELECT raw_text
                    FROM properties
                    WHERE id = %s
                    """,
                    (property_id,),
                )

                row = cur.fetchone()

                if row:
                    latest_source_raw_text = row[0]

    if not latest_source_raw_text:
        raise ValueError(
            "متن اصلی فایل برای بازخوانی پیدا نشد."
        )

    parsed = parse_property(
        latest_source_raw_text
    )

    update_parsed_property(
        property_id,
        parsed,
    )

    save_property_address(
        property_id,
        parsed.get("address_text"),
    )

    dynamic_attributes = (
        parse_dynamic_attributes(
            latest_source_raw_text
        )
    )

    rule_attributes = (
        parse_rule_attributes(
            latest_source_raw_text,
            get_active_attribute_rules(),
        )
    )

    merged = {}

    for item in (
        dynamic_attributes
        + rule_attributes
    ):
        merged[
            item["attribute_key"]
        ] = item

    save_property_attributes(
        property_id,
        list(merged.values()),
    )

    if not set_failed_property_ready(
        property_id
    ):
        raise ValueError(
            "فایل دیگر در وضعیت خطادار نیست."
        )

    return parsed


# === QUEUE TRACKING SEARCH V1 ===
def get_queue_search_detail(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.property_id,
                    q.queue_status,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.floor_text,
                    p.build_year,
                    p.current_price_billion,
                    (
                        SELECT ps.source_unit_code
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                          AND ps.is_active = TRUE
                        ORDER BY
                            ps.is_primary DESC,
                            ps.id DESC
                        LIMIT 1
                    ) AS source_unit_code,
                    (
                        SELECT ps.source_post_url
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                          AND ps.is_active = TRUE
                          AND ps.source_post_url IS NOT NULL
                          AND BTRIM(ps.source_post_url) <> ''
                        ORDER BY
                            ps.is_primary DESC,
                            ps.id DESC
                        LIMIT 1
                    ) AS source_post_url
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                WHERE q.property_id = %s
                  AND q.queue_status IN (
                      'NEW',
                      'PARSED',
                      'READY',
                      'SCHEDULED',
                      'PENDING_DUPLICATE',
                      'FAILED'
                  )
                ORDER BY
                    q.updated_at DESC,
                    q.id DESC
                LIMIT 1
                """,
                (property_id,),
            )

            return cur.fetchone()


def build_queue_search_detail(row):
    (
        property_id,
        queue_status,
        unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        floor_text,
        build_year,
        price_billion,
        source_unit_code,
        source_post_url,
    ) = row

    status_text = QUEUE_STATUS_LABELS.get(
        queue_status,
        queue_status,
    )

    location = (
        address_text
        or district
        or "ثبت نشده"
    )

    if area_m2 is not None:
        area_value = float(area_m2)
        if area_value.is_integer():
            area_value = int(area_value)
        area_text = f"{area_value} متر"
    else:
        area_text = "ثبت نشده"

    if price_billion is not None:
        price_text = (
            f"{float(price_billion):g} میلیارد تومان"
        )
    else:
        price_text = "ثبت نشده"

    lines = [
        f"🔎 فایل صف {property_id}",
        "",
        f"وضعیت: {status_text}",
        f"📍 {location}",
        f"📐 متراژ: {area_text}",
    ]

    if bedrooms is not None:
        lines.append(
            f"🛏 خواب: {bedrooms}"
        )

    if floor_text:
        lines.append(
            f"🏢 طبقه: {floor_text}"
        )

    if build_year:
        lines.append(
            f"🏗 سال ساخت: {build_year}"
        )

    lines.append(
        f"💰 قیمت: {price_text}"
    )

    if unit_code:
        lines.append(
            f"🔢 کد واحد: {unit_code}"
        )
    else:
        lines.append(
            "🔢 کد واحد: هنوز اختصاص داده نشده"
        )

    if source_unit_code:
        lines.append(
            f"📎 کد منبع: {source_unit_code}"
        )

    return "\n".join(lines)


def queue_search_result_keyboard(row):
    property_id = row[0]
    source_post_url = row[11]

    buttons = []

    if source_post_url:
        buttons.append(
            [
                InlineKeyboardButton(
                    "🔗 مشاهده فایل منبع",
                    url=source_post_url,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "📥 بازگشت به صف انتشار",
                callback_data="queue",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "🏠 منوی اصلی",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def get_real_queue_counts():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    queue_status,
                    COUNT(*)
                FROM publish_queue
                WHERE queue_status IN (
                    'NEW',
                    'PARSED',
                    'READY',
                    'SCHEDULED',
                    'PENDING_DUPLICATE',
                    'FAILED'
                )
                GROUP BY queue_status
                """
            )

            rows = cur.fetchall()

    counts = {
        "NEW": 0,
        "PARSED": 0,
        "READY": 0,
        "SCHEDULED": 0,
        "PENDING_DUPLICATE": 0,
        "FAILED": 0,
    }

    for status, count in rows:
        counts[status] = int(count)

    return counts


def queue_menu_keyboard(
    offset=0,
    total=0,
    page_size=10,
):
    buttons = []

    navigation = []

    if offset > 0:
        previous_offset = max(
            0,
            offset - page_size,
        )

        navigation.append(
            InlineKeyboardButton(
                "⬅️ قبلی",
                callback_data=(
                    f"queue_page_{previous_offset}"
                ),
            )
        )

    if offset + page_size < total:
        next_offset = offset + page_size

        navigation.append(
            InlineKeyboardButton(
                "بعدی ➡️",
                callback_data=(
                    f"queue_page_{next_offset}"
                ),
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 بروزرسانی همین صفحه",
                callback_data=f"queue_page_{offset}",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "🔎 جستجو در صف",
                callback_data="queue_search",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "🧩 بررسی فایل‌های تکراری",
                callback_data="duplicate_queue",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت به فایل‌ها",
                callback_data="files_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)

def get_queue_scheduler_status():
    tehran = ZoneInfo("Asia/Tehran")
    now = datetime.now(tehran)

    weekday = now.weekday()
    today = now.date()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    start_time,
                    end_time,
                    interval_minutes,
                    max_posts
                FROM schedule_windows
                WHERE weekday = %s
                  AND is_active = TRUE
                ORDER BY start_time ASC, id ASC
                """,
                (weekday,),
            )

            windows = cur.fetchall()

    if not windows:
        return {
            "has_window": False,
            "active_now": False,
            "window_text": "برای امروز بازه‌ای ثبت نشده",
            "interval_text": "-",
            "next_publish_text": "بازه فعال ندارد",
        }

    active_window = None
    next_window = None

    for (
        window_id,
        start_time,
        end_time,
        interval_minutes,
        max_posts,
    ) in windows:

        start_dt = datetime.combine(
            today,
            start_time,
            tzinfo=tehran,
        )

        if end_time > start_time:
            end_dt = datetime.combine(
                today,
                end_time,
                tzinfo=tehran,
            )
        else:
            end_dt = datetime.combine(
                today + timedelta(days=1),
                end_time,
                tzinfo=tehran,
            )

        if start_dt <= now < end_dt:
            active_window = {
                "id": window_id,
                "start": start_dt,
                "end": end_dt,
                "interval": int(interval_minutes),
                "max_posts": max_posts,
            }
            break

        if now < start_dt and next_window is None:
            next_window = {
                "id": window_id,
                "start": start_dt,
                "end": end_dt,
                "interval": int(interval_minutes),
                "max_posts": max_posts,
            }

    selected = active_window or next_window

    if selected is None:
        first = windows[0]

        (
            window_id,
            start_time,
            end_time,
            interval_minutes,
            max_posts,
        ) = first

        tomorrow = today + timedelta(days=1)

        start_dt = datetime.combine(
            tomorrow,
            start_time,
            tzinfo=tehran,
        )

        if end_time > start_time:
            end_dt = datetime.combine(
                tomorrow,
                end_time,
                tzinfo=tehran,
            )
        else:
            end_dt = datetime.combine(
                tomorrow + timedelta(days=1),
                end_time,
                tzinfo=tehran,
            )

        selected = {
            "id": window_id,
            "start": start_dt,
            "end": end_dt,
            "interval": int(interval_minutes),
            "max_posts": max_posts,
        }

    if active_window:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        MAX(published_at)
                    FROM publish_queue
                    WHERE published_at IS NOT NULL
                      AND published_at >= %s
                      AND published_at < %s
                    """,
                    (
                        active_window["start"],
                        active_window["end"],
                    ),
                )

                row = cur.fetchone()

        last_published = (
            row[0]
            if row
            else None
        )

        if last_published:
            if last_published.tzinfo is None:
                last_published = last_published.replace(
                    tzinfo=tehran
                )
            else:
                last_published = last_published.astimezone(
                    tehran
                )

            next_publish = (
                last_published
                + timedelta(
                    minutes=active_window[
                        "interval"
                    ]
                )
            )

            if next_publish < now:
                next_publish = now

            next_text = next_publish.strftime(
                "%H:%M"
            )

        else:
            next_text = (
                "در اولین بررسی Scheduler"
            )

        return {
            "has_window": True,
            "active_now": True,
            "window_text": (
                f"{active_window['start'].strftime('%H:%M')} "
                f"تا {active_window['end'].strftime('%H:%M')}"
            ),
            "interval_text": (
                f"{active_window['interval']} دقیقه"
            ),
            "next_publish_text": next_text,
        }

    return {
        "has_window": True,
        "active_now": False,
        "window_text": (
            f"{selected['start'].strftime('%H:%M')} "
            f"تا {selected['end'].strftime('%H:%M')}"
        ),
        "interval_text": (
            f"{selected['interval']} دقیقه"
        ),
        "next_publish_text": (
            selected["start"].strftime("%H:%M")
        ),
    }


def build_real_queue_text(
    offset=0,
    page_size=10,
):
    counts = get_real_queue_counts()

    total = sum(
        counts.values()
    )

    if total > 0:
        last_valid_offset = (
            (total - 1) // page_size
        ) * page_size

        offset = min(
            max(0, offset),
            last_valid_offset,
        )
    else:
        offset = 0

    rows = get_real_publish_queue(
        limit=page_size,
        offset=offset,
    )

    scheduler_status = get_queue_scheduler_status()

    scheduler_icon = (
        "🟢"
        if scheduler_status["active_now"]
        else "🟡"
    )

    lines = [
        "📥 صف انتشار",
        "",
        f"کل فایل‌های در انتظار: {total}",
        "",
        f"🟢 آماده انتشار: {counts['READY']}",
        (
            f"{scheduler_icon} بازه انتشار: "
            f"{scheduler_status['window_text']}"
        ),
        (
            "⏱ فاصله انتشار: "
            f"{scheduler_status['interval_text']}"
        ),
        (
            "🕒 انتشار بعدی تقریبی: "
            f"{scheduler_status['next_publish_text']}"
        ),
        (
            "🧩 تکراری منتظر تأیید: "
            f"{counts['PENDING_DUPLICATE']}"
        ),
        f"❌ خطادار: {counts['FAILED']}",
    ]

    if not rows:
        lines.extend(
            [
                "",
                "✅ در حال حاضر فایلی در صف انتشار وجود ندارد.",
            ]
        )

        return "\n".join(lines)

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "فایل‌های صف:",
            "",
        ]
    )

    for index, row in enumerate(
        rows,
        start=offset + 1,
    ):
        (
            property_id,
            queue_status,
            scheduled_for,
            attempts,
            last_error,
            unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            source_unit_code,
        ) = row

        status_text = QUEUE_STATUS_LABELS.get(
            queue_status,
            queue_status,
        )

        location = (
            address_text
            or district
            or "لوکیشن نامشخص"
        )

        # Clean common source prefixes for display.
        location = str(
            location
        ).strip()

        for prefix in (
            "منطقه واحد:",
            "منطقه واحد :",
            "واحد:",
            "واحد :",
        ):
            if location.startswith(prefix):
                location = location[
                    len(prefix):
                ].strip()

        location = (
            location
            .replace("|", " | ")
            .replace("-", " - ")
        )

        while "  " in location:
            location = location.replace(
                "  ",
                " ",
            )

        if area_m2 is not None:
            area_value = float(
                area_m2
            )

            if area_value.is_integer():
                area_value = int(
                    area_value
                )

            area_text = (
                f"{area_value}م"
            )
        else:
            area_text = "متراژ؟"

        if price_billion is not None:
            price_value = float(
                price_billion
            )

            if price_value.is_integer():
                price_text = (
                    f"{int(price_value)} میلیارد"
                )
            else:
                price_text = (
                    f"{price_value:g} میلیارد"
                )
        else:
            price_text = "قیمت؟"

        lines.append(
            f"{index}. {status_text}"
        )

        lines.append(
            f"📍 {location} | "
            f"{area_text} | "
            f"{price_text}"
        )

        lines.append(
            f"🔎 شماره پیگیری: صف {property_id}"
        )

        if unit_code:
            lines.append(
                f"کد واحد: {unit_code}"
            )

        if (
            queue_status == "FAILED"
            and last_error
        ):
            error_text = str(
                last_error
            ).replace(
                "\n",
                " ",
            )

            lines.append(
                "خطا: "
                + error_text[:100]
            )

        lines.append("────────────")

    if total > 0:
        total_pages = (
            total + page_size - 1
        ) // page_size

        current_page = (
            offset // page_size
        ) + 1

        first_item = (
            offset + 1
            if rows
            else 0
        )

        last_item = (
            offset + len(rows)
        )

        lines.extend(
            [
                "",
                (
                    f"📄 صفحه {current_page} از "
                    f"{total_pages}"
                ),
                (
                    f"نمایش فایل‌های {first_item} "
                    f"تا {last_item} از {total} فایل."
                ),
            ]
        )

    return "\n".join(lines)


def files_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 بازنشر فایل",
                    callback_data="republish",
                ),
                InlineKeyboardButton(
                    "✅ فایل‌های منتشرشده",
                    callback_data="published",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔁 بازنشر گروهی",
                    callback_data="republish_group",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📥 صف انتشار",
                    callback_data="queue",
                ),
                InlineKeyboardButton(
                    "🧩 بررسی فایل‌های تکراری",
                    callback_data="duplicate_queue",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚠️ فایل‌های ناقص",
                    callback_data="invalid",
                ),
                InlineKeyboardButton(
                    "🗑 حذف فورس فایل",
                    callback_data="force_delete",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ فایل‌های خطادار",
                    callback_data="queue_failed",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑 سطل آشغال",
                    callback_data="trash_page_0",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت به تنظیمات",
                    callback_data="settings",
                )
            ],
        ]
    )



def normalize_search_text(value):
    value = str(value or "").strip()

    value = value.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )

    value = value.replace("ي", "ی")
    value = value.replace("ك", "ک")
    value = value.replace("ۀ", "ه")

    value = re.sub(r"\s+", " ", value).strip()

    return value


def parse_smart_search_query(raw_query):
    text = normalize_search_text(raw_query)

    result = {
        "raw": text,
        "price_min": None,
        "price_max": None,
        "area_min": None,
        "area_max": None,
        "bedrooms": None,
        "floor_text": None,
        "unit_code": None,
        "free_text": None,
        "has_parking": None,
        "has_elevator": None,
        "has_storage": None,
        "deed_required": None,
        "direction": None,
    }

    working = text

    # Structured management filters. Canonical markers are also used by the
    # inline filter buttons, so selected amenities never leak into free text.
    feature_specs = (
        ("has_parking", r"(?:فیلتر:)?پارکینگ(?:‌|\s)*(?:دار|دارد)?"),
        ("has_elevator", r"(?:فیلتر:)?آسانسور(?:‌|\s)*(?:دار|دارد)?"),
        ("has_storage", r"(?:فیلتر:)?انباری(?:‌|\s)*(?:دار|دارد)?"),
    )
    for key, pattern in feature_specs:
        match = re.search(pattern, working, flags=re.IGNORECASE)
        if match:
            result[key] = True
            working = working.replace(match.group(0), " ")

    deed_match = re.search(r"(?:فیلتر:)?سند(?:‌|\s)*(?:دار|دارد)?", working)
    if deed_match:
        result["deed_required"] = True
        working = working.replace(deed_match.group(0), " ")

    direction_match = re.search(
        r"(?:جهت\s*[:\-]?\s*|فیلتر:)(شمالی|جنوبی)",
        working,
        flags=re.IGNORECASE,
    )
    if direction_match:
        result["direction"] = direction_match.group(1)
        working = working.replace(direction_match.group(0), " ")

    # ---------------------------------------------
    # Unit code
    # ---------------------------------------------

    match = re.search(
        r"(?:کد\s*(?:واحد|ملک|فایل)?\s*[:\-]?\s*)(\d+)",
        working,
        flags=re.IGNORECASE,
    )

    if match:
        result["unit_code"] = match.group(1)
        working = working.replace(match.group(0), " ")

    # ---------------------------------------------
    # Price range: 5 تا 18 میلیارد / بین 5 و 18 میلیارد
    # ---------------------------------------------

    price_range_patterns = [
        r"بین\s*(\d+(?:\.\d+)?)\s*(?:تا|و|-)\s*(\d+(?:\.\d+)?)\s*میلیارد",
        r"(\d+(?:\.\d+)?)\s*(?:تا|-)\s*(\d+(?:\.\d+)?)\s*میلیارد",
    ]

    for pattern in price_range_patterns:
        match = re.search(
            pattern,
            working,
            flags=re.IGNORECASE,
        )

        if match:
            low = float(match.group(1))
            high = float(match.group(2))

            if low > high:
                low, high = high, low

            result["price_min"] = low
            result["price_max"] = high

            working = working.replace(
                match.group(0),
                " ",
            )
            break

    # ---------------------------------------------
    # Price below / above / exact
    # ---------------------------------------------

    if (
        result["price_min"] is None
        and result["price_max"] is None
    ):
        match = re.search(
            r"(?:زیر|کمتر\s*از)\s*(\d+(?:\.\d+)?)\s*میلیارد",
            working,
            flags=re.IGNORECASE,
        )

        if match:
            result["price_max"] = float(
                match.group(1)
            )

            working = working.replace(
                match.group(0),
                " ",
            )

    if (
        result["price_min"] is None
        and result["price_max"] is None
    ):
        match = re.search(
            r"(?:بالای|بالاتر\s*از|بیشتر\s*از)\s*(\d+(?:\.\d+)?)\s*میلیارد",
            working,
            flags=re.IGNORECASE,
        )

        if match:
            result["price_min"] = float(
                match.group(1)
            )

            working = working.replace(
                match.group(0),
                " ",
            )

    if (
        result["price_min"] is None
        and result["price_max"] is None
    ):
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*میلیارد",
            working,
            flags=re.IGNORECASE,
        )

        if match:
            value = float(match.group(1))

            result["price_min"] = value
            result["price_max"] = value

            working = working.replace(
                match.group(0),
                " ",
            )

    # ---------------------------------------------
    # Area range: 60 تا 90 متر
    # ---------------------------------------------

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:تا|-)\s*(\d+(?:\.\d+)?)\s*متر",
        working,
        flags=re.IGNORECASE,
    )

    if match:
        low = float(match.group(1))
        high = float(match.group(2))

        if low > high:
            low, high = high, low

        result["area_min"] = low
        result["area_max"] = high

        working = working.replace(
            match.group(0),
            " ",
        )

    # ---------------------------------------------
    # Exact area: 60 متر
    # ---------------------------------------------

    if (
        result["area_min"] is None
        and result["area_max"] is None
    ):
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*متر(?:ی)?",
            working,
            flags=re.IGNORECASE,
        )

        if match:
            value = float(match.group(1))

            result["area_min"] = value
            result["area_max"] = value

            working = working.replace(
                match.group(0),
                " ",
            )

    # ---------------------------------------------
    # Bedrooms
    # ---------------------------------------------

    match = re.search(
        r"(\d+)\s*خواب",
        working,
        flags=re.IGNORECASE,
    )

    if match:
        result["bedrooms"] = int(
            match.group(1)
        )

        working = working.replace(
            match.group(0),
            " ",
        )

    # ---------------------------------------------
    # Floor
    # ---------------------------------------------

    match = re.search(
        r"طبقه\s*[:\-]?\s*"
        r"(همکف|زیرزمین|اول|دوم|سوم|چهارم|پنجم|ششم|هفتم|هشتم|نهم|دهم|\d+)",
        working,
        flags=re.IGNORECASE,
    )

    if match:
        result["floor_text"] = (
            match.group(1).strip()
        )

        working = working.replace(
            match.group(0),
            " ",
        )

    # ---------------------------------------------
    # Whatever remains becomes free text:
    # neighborhood, attributes, arbitrary words, etc.
    # ---------------------------------------------

    working = re.sub(
        r"\s+",
        " ",
        working,
    ).strip(" -،,")

    if working:
        result["free_text"] = working

    return result


def smart_search_properties(raw_query, offset=0, limit=10):
    filters = parse_smart_search_query(
        raw_query
    )

    validity_weeks = get_file_validity_weeks()

    where = [
        "p.status = 'ACTIVE'",
        "p.published_message_id IS NOT NULL",
        "p.deleted_from_channel_at IS NULL",
        "p.created_at >= NOW() - (%s * INTERVAL '1 week')",
        """
        NOT EXISTS (
            SELECT 1
            FROM property_trash pt
            WHERE pt.property_id = p.id
        )
        """,
    ]

    params = [
        validity_weeks,
    ]

    if filters["unit_code"]:
        where.append(
            """
            (
                p.unit_code = %s
                OR EXISTS (
                    SELECT 1
                    FROM property_sources ps
                    WHERE ps.property_id = p.id
                      AND ps.source_unit_code = %s
                )
            )
            """
        )
        params.extend(
            [
                filters["unit_code"],
                filters["unit_code"],
            ]
        )

    if filters["price_min"] is not None:
        where.append(
            "p.current_price_billion >= %s"
        )
        params.append(
            filters["price_min"]
        )

    if filters["price_max"] is not None:
        where.append(
            "p.current_price_billion <= %s"
        )
        params.append(
            filters["price_max"]
        )

    if filters["area_min"] is not None:
        where.append(
            "p.area_m2 >= %s"
        )
        params.append(
            filters["area_min"]
        )

    if filters["area_max"] is not None:
        where.append(
            "p.area_m2 <= %s"
        )
        params.append(
            filters["area_max"]
        )

    if filters["bedrooms"] is not None:
        where.append(
            "p.bedrooms = %s"
        )
        params.append(
            filters["bedrooms"]
        )

    if filters["floor_text"]:
        where.append(
            "COALESCE(p.floor_text, '') ILIKE %s"
        )
        params.append(
            f"%{filters['floor_text']}%"
        )

    for key, column in (
        ("has_parking", "p.has_parking"),
        ("has_elevator", "p.has_elevator"),
        ("has_storage", "p.has_storage"),
    ):
        if filters.get(key) is True:
            where.append(f"{column} IS TRUE")

    if filters.get("direction"):
        where.append("COALESCE(p.direction, '') ILIKE %s")
        params.append(f"%{filters['direction']}%")

    if filters.get("deed_required") is True:
        where.append(
            "(NULLIF(TRIM(COALESCE(p.deed_text,'')), '') IS NOT NULL OR "
            "EXISTS (SELECT 1 FROM property_attributes pa WHERE pa.property_id=p.id "
            "AND pa.attribute_key IN ('deed','deed_type') "
            "AND NULLIF(TRIM(COALESCE(pa.attribute_value,'')), '') IS NOT NULL))"
        )

    free_text = filters["free_text"]

    if free_text:
        needle = f"%{free_text}%"

        # -------------------------------------------------
        # Location-first search
        #
        # If the entered phrase is actually present in the
        # normalized district/address of at least one live
        # property, treat it as a LOCATION search.
        #
        # Example:
        #   باغمیشه
        #
        # must not match another district merely because
        # "باغمیشه" appears somewhere in raw source text.
        # -------------------------------------------------

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM properties p
                    WHERE p.status = 'ACTIVE'
                      AND p.published_message_id IS NOT NULL
                      AND p.deleted_from_channel_at IS NULL
                      AND p.created_at >= NOW() - (%s * INTERVAL '1 week')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM property_trash pt
                          WHERE pt.property_id = p.id
                      )
                      AND (
                          COALESCE(p.district, '') ILIKE %s
                          OR COALESCE(p.address_text, '') ILIKE %s
                      )
                    LIMIT 1
                    """,
                    (
                        validity_weeks,
                        needle,
                        needle,
                    ),
                )

                location_match = (
                    cur.fetchone() is not None
                )

        if location_match:
            where.append(
                """
                (
                    COALESCE(p.district, '') ILIKE %s
                    OR COALESCE(p.address_text, '') ILIKE %s
                )
                """
            )

            params.extend(
                [
                    needle,
                    needle,
                ]
            )

        else:
            # General free-text search for non-location
            # phrases such as attributes/features.
            where.append(
                """
                (
                    COALESCE(p.unit_code, '') ILIKE %s
                    OR COALESCE(p.district, '') ILIKE %s
                    OR COALESCE(p.address_text, '') ILIKE %s
                    OR COALESCE(p.floor_text, '') ILIKE %s
                    OR COALESCE(p.raw_text, '') ILIKE %s
                    OR COALESCE(p.area_m2::text, '') ILIKE %s
                    OR COALESCE(p.current_price_billion::text, '') ILIKE %s
                    OR EXISTS (
                        SELECT 1
                        FROM property_attributes pa
                        WHERE pa.property_id = p.id
                          AND (
                              COALESCE(pa.attribute_label, '') ILIKE %s
                              OR COALESCE(pa.attribute_value, '') ILIKE %s
                          )
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM property_sources ps
                        WHERE ps.property_id = p.id
                          AND COALESCE(
                              ps.source_unit_code,
                              ''
                          ) ILIKE %s
                    )
                )
                """
            )

            params.extend(
                [needle] * 10
            )

    # Bare number such as "60":
    # If there were no structured filters, treat it as
    # broad search everywhere.
    if (
        not free_text
        and re.fullmatch(
            r"\d+(?:\.\d+)?",
            filters["raw"],
        )
        and filters["unit_code"] is None
        and filters["price_min"] is None
        and filters["price_max"] is None
        and filters["area_min"] is None
        and filters["area_max"] is None
        and filters["bedrooms"] is None
        and filters["floor_text"] is None
    ):
        needle = f"%{filters['raw']}%"

        where.append(
            """
            (
                COALESCE(p.unit_code, '') ILIKE %s
                OR COALESCE(p.area_m2::text, '') ILIKE %s
                OR COALESCE(p.current_price_billion::text, '') ILIKE %s
                OR COALESCE(p.raw_text, '') ILIKE %s
                OR EXISTS (
                    SELECT 1
                    FROM property_attributes pa
                    WHERE pa.property_id = p.id
                      AND COALESCE(pa.attribute_value, '') ILIKE %s
                )
                OR EXISTS (
                    SELECT 1
                    FROM property_sources ps
                    WHERE ps.property_id = p.id
                      AND COALESCE(
                          ps.source_unit_code,
                          ''
                      ) ILIKE %s
                )
            )
            """
        )

        params.extend(
            [needle] * 6
        )

    where_sql = "\n AND ".join(
        f"({item})"
        for item in where
    )

    # SMART_PRICE_ASC_V1
    price_sort_requested = (
        filters["price_min"] is not None
        or filters["price_max"] is not None
    )

    order_sql = (
        "p.current_price_billion ASC NULLS LAST, p.id ASC"
        if price_sort_requested
        else "p.updated_at DESC, p.id DESC"
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM properties p
                WHERE {where_sql}
                """,
                params,
            )

            total = int(
                cur.fetchone()[0] or 0
            )

            cur.execute(
                f"""
                SELECT
                    p.id,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.floor_text,
                    p.current_price_billion,
                    p.channel_post_url,
                    p.build_year,
                    p.current_price_toman,
                    p.published_message_id
                FROM properties p
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT %s
                OFFSET %s
                """,
                params + [limit, offset],
            )

            rows = cur.fetchall()

    return {
        "filters": filters,
        "total": total,
        "rows": rows,
        "offset": offset,
        "limit": limit,
    }


def smart_search_result_text(
    raw_query,
    result,
):
    total = result["total"]
    offset = result["offset"]
    limit = result["limit"]
    filters = result["filters"]

    if total <= 0:
        return (
            "🔍 جستجوی هوشمند\n\n"
            f"عبارت: {smart_search_display_query(raw_query)}\n\n"
            "⚠️ هیچ فایل فعال و معتبری پیدا نشد."
        )

    # -------------------------------------------------
    # Preserve existing pure price-range output exactly
    # as the category-post format.
    # -------------------------------------------------

    pure_price_range = (
        filters["price_min"] is not None
        and filters["price_max"] is not None
        and filters["price_min"]
            != filters["price_max"]
        and filters["area_min"] is None
        and filters["area_max"] is None
        and filters["bedrooms"] is None
        and filters["floor_text"] is None
        and filters["unit_code"] is None
        and not filters["free_text"]
        and not filters.get("has_parking")
        and not filters.get("has_elevator")
        and not filters.get("has_storage")
        and not filters.get("deed_required")
        and not filters.get("direction")
    )

    if pure_price_range:
        category_rows = []

        for row in result["rows"]:
            (
                property_id,
                unit_code,
                district,
                address_text,
                area_m2,
                bedrooms,
                floor_text,
                price_billion,
                channel_post_url,
                build_year,
                price_toman,
                published_message_id,
            ) = row

            category_rows.append(
                (
                    property_id,
                    unit_code,
                    district,
                    address_text,
                    area_m2,
                    bedrooms,
                    build_year,
                    price_billion,
                    price_toman,
                    channel_post_url,
                    published_message_id,
                )
            )

        return build_category_post_text(
            filters["price_min"],
            filters["price_max"],
            category_rows,
            offset=offset,
            total=total,
            page_size=limit,
        )

    # -------------------------------------------------
    # All other searches:
    # show complete listing cards just like category
    # output, including the direct Telegram post link.
    # -------------------------------------------------

    start_number = offset + 1
    end_number = min(
        offset + len(result["rows"]),
        total,
    )

    lines = [
        "🔍 جستجوی هوشمند",
        "",
        f"عبارت: {smart_search_display_query(raw_query)}",
        f"نتایج: {total} فایل",
        f"نمایش {start_number} تا {end_number}",
        "",
    ]

    selected = []
    if filters.get("has_parking"): selected.append("🚗 پارکینگ")
    if filters.get("has_elevator"): selected.append("🛗 آسانسور")
    if filters.get("has_storage"): selected.append("📦 انباری")
    if filters.get("deed_required"): selected.append("📄 سند")
    if filters.get("direction"): selected.append(f"🧭 {filters['direction']}")
    if selected:
        lines.extend(["شرایط قطعی: " + " | ".join(selected), ""])

    for index, row in enumerate(
        result["rows"],
        start=start_number,
    ):
        (
            property_id,
            unit_code,
            district,
            address_text,
            area_m2,
            bedrooms,
            floor_text,
            price_billion,
            channel_post_url,
            build_year,
            price_toman,
            published_message_id,
        ) = row

        location = (
            address_text
            or district
            or "نامشخص"
        )

        location = str(location).replace(
            "منطقه:",
            "",
        ).strip()

        lines.append(
            f"{index}) 📍 {location}"
        )

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
                f"📅 سال ساخت {int(build_year)}"
            )

        if details:
            lines.append(
                " | ".join(details)
            )

        if price_billion is not None:
            lines.append(
                f"💰 قیمت: "
                f"{float(price_billion):g} "
                f"میلیارد تومان"
            )
        else:
            lines.append(
                "💰 قیمت: ثبت نشده"
            )

        lines.append(
            f"🔢 کد واحد: "
            f"{unit_code or '-'}"
        )

        post_url = (
            channel_post_url
            or (
                f"https://t.me/nimatmelk/"
                f"{published_message_id}"
                if published_message_id
                else None
            )
        )

        if post_url:
            lines.append(
                f"🔗 مشاهده فایل: {post_url}"
            )
        else:
            lines.append(
                "🔗 مشاهده فایل: لینک ثبت نشده"
            )

        if index < end_number:
            lines.extend(
                [
                    "",
                    "──────────────",
                    "",
                ]
            )

    return "\n".join(lines)


def smart_search_keyboard(result, raw_query=None):
    rows = []

    offset = result["offset"]
    limit = result["limit"]
    total = result["total"]

    nav = []

    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ قبلی",
                callback_data=(
                    f"smart_search_page_"
                    f"{max(0, offset-limit)}"
                ),
            )
        )

    if offset + limit < total:
        nav.append(
            InlineKeyboardButton(
                "بعدی ➡️",
                callback_data=(
                    f"smart_search_page_"
                    f"{offset+limit}"
                ),
            )
        )

    if nav:
        rows.append(nav)

    filters = result.get("filters") or {}
    rows.extend([
        [
            InlineKeyboardButton(
                ("✅ " if filters.get("has_parking") else "▫️ ") + "پارکینگ",
                callback_data="smart_filter_parking",
            ),
            InlineKeyboardButton(
                ("✅ " if filters.get("has_elevator") else "▫️ ") + "آسانسور",
                callback_data="smart_filter_elevator",
            ),
        ],
        [
            InlineKeyboardButton(
                ("✅ " if filters.get("has_storage") else "▫️ ") + "انباری",
                callback_data="smart_filter_storage",
            ),
            InlineKeyboardButton(
                ("✅ " if filters.get("deed_required") else "▫️ ") + "سند",
                callback_data="smart_filter_deed",
            ),
        ],
        [
            InlineKeyboardButton(
                ("✅ " if filters.get("direction") == "جنوبی" else "▫️ ") + "جنوبی",
                callback_data="smart_filter_south",
            ),
            InlineKeyboardButton(
                ("✅ " if filters.get("direction") == "شمالی" else "▫️ ") + "شمالی",
                callback_data="smart_filter_north",
            ),
        ],
    ])

    rows.append(
        [InlineKeyboardButton(
            "🔗 ساخت لینک جذب مشتری",
            callback_data="customer_showcase_create",
        )]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "🔎 جستجوی جدید",
                callback_data="smart_search",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ منوی اصلی",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


SMART_FILTER_TOKENS = {
    "parking": "فیلتر:پارکینگ",
    "elevator": "فیلتر:آسانسور",
    "storage": "فیلتر:انباری",
    "deed": "فیلتر:سند",
    "south": "فیلتر:جنوبی",
    "north": "فیلتر:شمالی",
}


def smart_search_display_query(raw_query):
    text = normalize_search_text(raw_query)
    for token in SMART_FILTER_TOKENS.values():
        text = text.replace(token, " ")
    return re.sub(r"\s+", " ", text).strip()


def toggle_smart_filter(raw_query, filter_key):
    text = normalize_search_text(raw_query)
    token = SMART_FILTER_TOKENS.get(filter_key)
    if not token:
        return text
    # Direction is mutually exclusive.
    if filter_key in {"south", "north"}:
        other = SMART_FILTER_TOKENS["north" if filter_key == "south" else "south"]
        text = text.replace(other, " ")
    if token in text:
        text = text.replace(token, " ")
    else:
        text = f"{text} {token}"
    return re.sub(r"\s+", " ", text).strip()


def main_menu(role="USER"):
    rows = [
        [
            InlineKeyboardButton(
                "➕ افزودن کانال",
                callback_data="add_channel",
            ),
            InlineKeyboardButton(
                "📡 مدیریت کانال‌های منبع",
                callback_data="sources",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 انتشار فوری",
                callback_data="urgent_publish",
            ),
            InlineKeyboardButton(
                "📁 فایل‌ها",
                callback_data="files_menu",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔍 جستجوی هوشمند",
                callback_data="smart_search",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔎 جستجوی کد واحد",
                callback_data="search_unit",
            ),
            InlineKeyboardButton(
                "🗂 ارسال دسته‌بندی",
                callback_data="category_send",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="stats",
            ),
            InlineKeyboardButton(
                "#️⃣ هشتگ‌های تا کنون استفاده شده",
                callback_data="hashtags",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏰ زمان‌بندی",
                callback_data="schedule",
            ),
            InlineKeyboardButton(
                "⚙️ تنظیمات",
                callback_data="settings",
            ),
        ],
    ]

    if role in ("ADMIN", "OWNER"):
        rows.append(
            [
                InlineKeyboardButton(
                    "👥 کاربران",
                    callback_data="users",
                ),
                InlineKeyboardButton(
                    "♻️ ریست",
                    callback_data="reset",
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🧾 لاگ‌ها",
                callback_data="logs",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def get_hashtag_stats():
    """
    Count only live listings which are currently available
    in the destination channel.
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    price_hashtag,
                    COUNT(*) AS property_count,
                    MIN(current_price_billion) AS sort_price
                FROM properties
                WHERE status = 'ACTIVE'
                  AND published_message_id IS NOT NULL
                  AND deleted_from_channel_at IS NULL
                  AND price_hashtag IS NOT NULL
                  AND BTRIM(price_hashtag) <> ''
                GROUP BY price_hashtag
                ORDER BY
                    MIN(current_price_billion) ASC NULLS LAST,
                    price_hashtag ASC
                """
            )

            return cur.fetchall()


def format_hashtag_stats():
    rows = get_hashtag_stats()

    if not rows:
        return "#️⃣ هنوز هشتگی ثبت نشده است."

    total = sum(
        int(row[1] or 0)
        for row in rows
    )

    lines = [
        "#️⃣ هشتگ‌های تا کنون استفاده شده",
        "",
        "تعداد فایل‌های فعال در هر بازه قیمت:",
        "",
    ]

    for hashtag, count, sort_price in rows:
        lines.append(
            f"{hashtag} — {int(count)} واحد"
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"جمع کل فایل‌های فعال: {total} واحد",
        ]
    )

    return "\n".join(lines)


def back_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="home")]]
    )


def settings_back_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")]]
    )


def channel_type_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📥 کانال ورودی",
                    callback_data="add_type_INPUT",
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 کانال اصلی",
                    callback_data="add_type_DESTINATION",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧾 کانال لاگ",
                    callback_data="add_type_LOG",
                )
            ],
            [
                InlineKeyboardButton(
                    "📡 کانال منبع",
                    callback_data="add_type_SOURCE",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت به تنظیمات",
                    callback_data="settings",
                )
            ],
        ]
    )



SOURCE_DELETE_WATCH_INTERVAL_SECONDS = 300
SOURCE_DELETE_WATCH_BATCH_SIZE = 25


def get_source_delete_watch_rows(limit=25):
    """
    Inspect recent SOURCE posts that are queued or already published.
    Least-recently checked rows are selected first so the watcher rotates.
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT
                    ps.id,
                    ps.property_id,
                    ps.source_message_id,
                    ps.source_post_url,
                    c.username
                FROM property_sources ps
                JOIN channels c
                  ON c.id = ps.source_channel_id
                JOIN properties p
                  ON p.id = ps.property_id
                WHERE ps.is_active = TRUE
                  AND c.channel_type = 'SOURCE'
                  AND c.is_active = TRUE
                  AND COALESCE(p.published_at, p.created_at)
                      >= NOW() - INTERVAL '11 days'
                  AND c.username IS NOT NULL
                  AND BTRIM(c.username) <> ''
                  AND ps.source_message_id IS NOT NULL
                  AND (
                        EXISTS (
                            SELECT 1
                            FROM publish_queue q
                            WHERE q.property_id = ps.property_id
                              AND q.queue_status IN (
                                  'NEW',
                                  'PARSED',
                                  'READY',
                                  'SCHEDULED',
                                  'PENDING_DUPLICATE',
                                  'FAILED'
                              )
                        )
                        OR
                        EXISTS (
                            SELECT 1
                            FROM republish_queue rq
                            WHERE rq.property_id = ps.property_id
                              AND rq.queue_status IN (
                                  'READY',
                                  'SCHEDULED',
                                  'PROCESSING'
                              )
                        )
                        OR (
                            p.status = 'ACTIVE'
                            AND p.published_message_id IS NOT NULL
                            AND p.deleted_from_channel_at IS NULL
                        )
                  )
                ORDER BY ps.last_synced_at ASC NULLS FIRST, ps.id ASC
                LIMIT %s
                """,
                (limit,),
            )

            return cur.fetchall()


def touch_property_source_check(property_source_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE property_sources
                SET last_synced_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (property_source_id,),
            )
        conn.commit()


def should_remove_published_property_after_source_delete(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.source_kind,
                    p.status,
                    p.published_message_id,
                    p.deleted_from_channel_at,
                    COUNT(ps.id) FILTER (WHERE ps.is_active = TRUE)
                FROM properties p
                LEFT JOIN property_sources ps ON ps.property_id = p.id
                WHERE p.id = %s
                GROUP BY p.id
                """,
                (property_id,),
            )
            row = cur.fetchone()

    if not row:
        return False

    source_kind, status, message_id, deleted_at, active_sources = row
    return (
        str(source_kind or "").upper() not in {"MANUAL", "CUSTOMER"}
        and status == "ACTIVE"
        and message_id is not None
        and deleted_at is None
        and int(active_sources or 0) == 0
    )


def source_public_post_exists(
    username,
    message_id,
):
    """
    Returns:
        True  -> post definitely appears to exist
        False -> Telegram page explicitly says post is missing
        None  -> network/temporary/unknown condition
    """

    username = str(username or "").strip().lstrip("@")

    if not username or not message_id:
        return None

    url = (
        f"https://t.me/{username}/"
        f"{int(message_id)}?embed=1"
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; RealEstateBot/1.0)"
            )
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            body = response.read().decode(
                "utf-8",
                errors="ignore",
            ).lower()

    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return False

        return None

    except Exception:
        return None

    missing_markers = (
        "post not found",
        "message not found",
        "tgme_widget_message_error",
        "this post is unavailable",
    )

    if any(
        marker in body
        for marker in missing_markers
    ):
        return False

    # Telegram embed pages for existing channel posts
    # normally contain this widget container.
    existing_markers = (
        "tgme_widget_message",
        "tgme_widget_message_text",
        "tgme_widget_message_bubble",
    )

    if any(
        marker in body
        for marker in existing_markers
    ):
        return True

    return None


def cancel_property_queues_for_deleted_source(
    property_id,
    property_source_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE property_sources
                SET
                    is_active = FALSE,
                    last_synced_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (property_source_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'SKIPPED',
                    last_error =
                        'Source post deleted from Telegram',
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status IN (
                      'NEW',
                      'PARSED',
                      'READY',
                      'SCHEDULED',
                      'PENDING_DUPLICATE',
                      'FAILED'
                  )
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE republish_queue
                SET
                    queue_status = 'CANCELLED',
                    last_error =
                        'Source post deleted from Telegram',
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status IN (
                      'READY',
                      'SCHEDULED',
                      'PROCESSING'
                  )
                """,
                (property_id,),
            )

            conn.commit()


async def run_source_deleted_posts_watch_tick(
    application,
):
    rows = get_source_delete_watch_rows(
        SOURCE_DELETE_WATCH_BATCH_SIZE
    )

    checked = 0
    missing = 0
    unknown = 0
    destination_deleted = 0

    for (
        property_source_id,
        property_id,
        source_message_id,
        source_post_url,
        username,
    ) in rows:

        exists = await asyncio.to_thread(
            source_public_post_exists,
            username,
            source_message_id,
        )

        if exists is True:
            touch_property_source_check(property_source_id)
            checked += 1
            continue

        if exists is None:
            touch_property_source_check(property_source_id)
            unknown += 1

            logging.warning(
                "SOURCE delete watcher could not verify: "
                "property_id=%s source_message_id=%s "
                "username=%s",
                property_id,
                source_message_id,
                username,
            )

            continue

        cancel_property_queues_for_deleted_source(
            property_id,
            property_source_id,
        )

        missing += 1
        checked += 1

        logging.warning(
            "SOURCE post missing; queues cancelled: "
            "property_id=%s source_message_id=%s "
            "username=%s url=%s",
            property_id,
            source_message_id,
            username,
            source_post_url,
        )

        if (
            destination_deleted < 5
            and should_remove_published_property_after_source_delete(property_id)
        ):
            try:
                await force_delete_property(application.bot, property_id)
                destination_deleted += 1
                logging.warning(
                    "DESTINATION post removed after final source deletion: property_id=%s",
                    property_id,
                )
                await asyncio.sleep(5.0)
            except RetryAfter as exc:
                logging.warning(
                    "SOURCE delete cleanup paused by Telegram flood control: %s",
                    exc,
                )
                break
            except Exception:
                logging.exception(
                    "Could not remove destination post after source deletion: property_id=%s",
                    property_id,
                )

    logging.info(
        "SOURCE deleted-post watcher completed: "
        "checked=%s missing=%s unknown=%s destination_deleted=%s",
        checked,
        missing,
        unknown,
        destination_deleted,
    )


async def source_deleted_posts_watcher_loop(
    application,
):
    logging.info(
        "SOURCE deleted-post watcher started: "
        "interval=%ss batch=%s",
        SOURCE_DELETE_WATCH_INTERVAL_SECONDS,
        SOURCE_DELETE_WATCH_BATCH_SIZE,
    )

    while True:
        try:
            await asyncio.sleep(
                SOURCE_DELETE_WATCH_INTERVAL_SECONDS
            )

            await run_source_deleted_posts_watch_tick(
                application
            )

        except asyncio.CancelledError:
            logging.info(
                "SOURCE deleted-post watcher stopped."
            )
            raise

        except Exception:
            logging.exception(
                "Unhandled SOURCE deleted-post watcher error"
            )


def start_source_deleted_posts_watcher(
    application,
):
    existing = application.bot_data.get(
        "source_deleted_posts_watcher_task"
    )

    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        source_deleted_posts_watcher_loop(
            application
        ),
        name="source_deleted_posts_watcher",
    )

    application.bot_data[
        "source_deleted_posts_watcher_task"
    ] = task

    return task


DELETE_WATCH_INTERVAL_SECONDS = 300
DELETE_WATCH_BATCH_SIZE = 25


def get_delete_watch_property_ids(
    after_id=0,
    limit=25,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM properties
                WHERE status = 'ACTIVE'
                  AND published_message_id IS NOT NULL
                  AND deleted_from_channel_at IS NULL
                  AND COALESCE(published_at, created_at)
                      >= NOW() - INTERVAL '11 days'
                  AND id > %s
                ORDER BY id
                LIMIT %s
                """,
                (
                    after_id,
                    limit,
                ),
            )

            rows = cur.fetchall()

            if len(rows) < limit:
                remaining = limit - len(rows)

                cur.execute(
                    """
                    SELECT id
                    FROM properties
                    WHERE status = 'ACTIVE'
                      AND published_message_id IS NOT NULL
                      AND deleted_from_channel_at IS NULL
                      AND COALESCE(published_at, created_at)
                          >= NOW() - INTERVAL '11 days'
                      AND id <= %s
                    ORDER BY id
                    LIMIT %s
                    """,
                    (
                        after_id,
                        remaining,
                    ),
                )

                rows.extend(
                    cur.fetchall()
                )

    return [
        row[0]
        for row in rows
    ]


async def run_deleted_posts_watch_tick(
    application,
):
    after_id = application.bot_data.get(
        "delete_watch_last_property_id",
        0,
    )

    property_ids = get_delete_watch_property_ids(
        after_id=after_id,
        limit=DELETE_WATCH_BATCH_SIZE,
    )

    if not property_ids:
        application.bot_data[
            "delete_watch_last_property_id"
        ] = 0
        return

    checked = 0

    for property_id in property_ids:
        try:
            await refresh_published_property_post(
                application.bot,
                property_id,
            )

            checked += 1

        except Exception:
            logging.exception(
                "Deleted-post watcher failed: "
                "property_id=%s",
                property_id,
            )

    application.bot_data[
        "delete_watch_last_property_id"
    ] = property_ids[-1]

    logging.info(
        "Deleted-post watcher completed: "
        "checked=%s last_property_id=%s",
        checked,
        property_ids[-1],
    )


async def deleted_posts_watcher_loop(
    application,
):
    logging.info(
        "Deleted-post watcher started: "
        "interval=%ss batch=%s",
        DELETE_WATCH_INTERVAL_SECONDS,
        DELETE_WATCH_BATCH_SIZE,
    )

    while True:
        try:
            await asyncio.sleep(
                DELETE_WATCH_INTERVAL_SECONDS
            )

            await run_deleted_posts_watch_tick(
                application
            )

        except asyncio.CancelledError:
            logging.info(
                "Deleted-post watcher stopped."
            )
            raise

        except Exception:
            logging.exception(
                "Unhandled deleted-post watcher error"
            )


def start_deleted_posts_watcher(
    application,
):
    existing = application.bot_data.get(
        "deleted_posts_watcher_task"
    )

    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        deleted_posts_watcher_loop(
            application
        ),
        name="deleted_posts_watcher",
    )

    application.bot_data[
        "deleted_posts_watcher_task"
    ] = task

    return task



# === DESTINATION PUBLIC PRICE WATCHER V1 ===

DESTINATION_PRICE_WATCH_INTERVAL_SECONDS = 60
DESTINATION_PRICE_WATCH_MAX_PAGES = 30


def _destination_public_fetch_page(
    username,
    before_message_id,
):
    import urllib.request

    username = str(username).lstrip("@")

    url = (
        f"https://t.me/s/{username}"
        f"?before={int(before_message_id)}"
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "Chrome/124 Safari/537.36"
            )
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=15,
    ) as response:
        return response.read().decode(
            "utf-8",
            errors="replace",
        )


def _destination_public_extract_posts(
    page_html,
):
    import html as html_module

    marker_re = re.compile(
        r'data-post="[^"]+/(\d+)"',
        re.I,
    )

    text_re = re.compile(
        (
            r'<div[^>]*class="[^"]*'
            r'tgme_widget_message_text'
            r'[^"]*"[^>]*>'
            r'(.*?)'
            r'</div>'
        ),
        re.I | re.S,
    )

    markers = list(
        marker_re.finditer(page_html)
    )

    all_ids = []
    posts = {}

    for index, marker in enumerate(markers):

        message_id = int(
            marker.group(1)
        )

        all_ids.append(message_id)

        start = marker.end()

        if index + 1 < len(markers):
            end = markers[index + 1].start()
        else:
            end = len(page_html)

        chunk = page_html[start:end]

        text_match = text_re.search(
            chunk
        )

        if not text_match:
            continue

        fragment = text_match.group(1)

        fragment = re.sub(
            r"<br\s*/?>",
            "\n",
            fragment,
            flags=re.I,
        )

        fragment = re.sub(
            r"</p\s*>",
            "\n",
            fragment,
            flags=re.I,
        )

        fragment = re.sub(
            r"<[^>]+>",
            "",
            fragment,
        )

        public_text = (
            html_module.unescape(fragment)
            .replace("\xa0", " ")
            .strip()
        )

        if public_text:
            posts[message_id] = public_text

    return posts, all_ids


def _destination_price_watch_rows():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.published_message_id,
                    p.current_price_toman,
                    p.manual_price_toman
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id
                      IS NOT NULL
                  AND p.deleted_from_channel_at
                      IS NULL
                ORDER BY
                    p.published_message_id ASC
                """
            )

            return cur.fetchall()


def _destination_price_watch_observe(
    property_id,
    message_id,
    public_price,
):
    """
    Persistent public-price snapshot.

    BASELINE:
        first observation or republished message.

    SAME:
        public price did not change.

    CHANGED:
        public price changed since last observation.
    """

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    published_message_id,
                    last_seen_price_toman
                FROM destination_price_watch_state
                WHERE property_id = %s
                FOR UPDATE
                """,
                (property_id,),
            )

            row = cur.fetchone()

            if not row:
                cur.execute(
                    """
                    INSERT INTO
                    destination_price_watch_state (
                        property_id,
                        published_message_id,
                        last_seen_price_toman,
                        last_seen_at,
                        updated_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        NOW(),
                        NOW()
                    )
                    """,
                    (
                        property_id,
                        message_id,
                        public_price,
                    ),
                )

                conn.commit()

                return (
                    "BASELINE",
                    None,
                )

            (
                previous_message_id,
                previous_price,
            ) = row

            if (
                int(previous_message_id)
                != int(message_id)
            ):
                cur.execute(
                    """
                    UPDATE destination_price_watch_state
                    SET
                        published_message_id = %s,
                        last_seen_price_toman = %s,
                        last_seen_at = NOW(),
                        updated_at = NOW()
                    WHERE property_id = %s
                    """,
                    (
                        message_id,
                        public_price,
                        property_id,
                    ),
                )

                conn.commit()

                return (
                    "BASELINE",
                    previous_price,
                )

            if (
                previous_price is not None
                and int(previous_price)
                    == int(public_price)
            ):
                cur.execute(
                    """
                    UPDATE destination_price_watch_state
                    SET last_seen_at = NOW()
                    WHERE property_id = %s
                    """,
                    (property_id,),
                )

                conn.commit()

                return (
                    "SAME",
                    previous_price,
                )

            cur.execute(
                """
                UPDATE destination_price_watch_state
                SET
                    last_seen_price_toman = %s,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (
                    public_price,
                    property_id,
                ),
            )

            conn.commit()

            return (
                "CHANGED",
                previous_price,
            )


def _mark_destination_post_deleted(
    property_id,
):
    """
    Atomically remove a Telegram-deleted destination post from every
    live customer search surface. Returns True only when this call
    changed an ACTIVE property.
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    status = 'INACTIVE',
                    deleted_from_channel_at = COALESCE(
                        deleted_from_channel_at,
                        NOW()
                    ),
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'ACTIVE'
                  AND deleted_from_channel_at IS NULL
                RETURNING id
                """,
                (property_id,),
            )

            changed = cur.fetchone() is not None

            if changed:
                cur.execute(
                    """
                    UPDATE publish_queue
                    SET
                        queue_status = 'SKIPPED',
                        last_error =
                            'Destination post deleted from Telegram',
                        updated_at = NOW()
                    WHERE property_id = %s
                      AND queue_status IN (
                          'NEW',
                          'PARSED',
                          'READY',
                          'SCHEDULED',
                          'PENDING_DUPLICATE',
                          'FAILED'
                      )
                    """,
                    (property_id,),
                )

                cur.execute(
                    """
                    UPDATE republish_queue
                    SET
                        queue_status = 'CANCELLED',
                        last_error =
                            'Destination post deleted from Telegram',
                        updated_at = NOW()
                    WHERE property_id = %s
                      AND queue_status IN (
                          'READY',
                          'SCHEDULED',
                          'PROCESSING'
                      )
                    """,
                    (property_id,),
                )

        conn.commit()

    return changed


def _destination_price_watch_property_state(
    property_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.current_price_toman,
                    p.manual_price_toman,
                    p.status,
                    p.deleted_from_channel_at,
                    (
                        SELECT MAX(
                            ps.source_price_toman
                        )
                        FROM property_sources ps
                        WHERE
                            ps.property_id = p.id
                            AND ps.is_active = TRUE
                            AND ps.source_price_toman
                                IS NOT NULL
                    )
                FROM properties p
                WHERE p.id = %s
                """,
                (property_id,),
            )

            return cur.fetchone()


def _destination_price_watch_set_manual(
    property_id,
    manual_price,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    manual_price_toman = %s,
                    manual_price_updated_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'ACTIVE'
                  AND deleted_from_channel_at
                      IS NULL
                """,
                (
                    int(manual_price),
                    property_id,
                ),
            )

        conn.commit()


async def run_destination_public_price_watch_tick(
    application,
):
    destination = get_destination_channel()

    if not destination:
        return

    (
        destination_channel_id,
        destination_title,
        destination_username,
    ) = destination

    if not destination_username:
        logging.warning(
            "DESTINATION public price watcher "
            "requires a public channel username."
        )
        return

    rows = _destination_price_watch_rows()

    if not rows:
        return

    watched = {
        int(row[1]): row
        for row in rows
        if row[1] is not None
    }

    if not watched:
        return

    min_message_id = min(watched)
    max_message_id = max(watched)

    observed = {}

    before = max_message_id + 1

    for page_number in range(
        DESTINATION_PRICE_WATCH_MAX_PAGES
    ):
        try:
            page_html = await asyncio.to_thread(
                _destination_public_fetch_page,
                destination_username,
                before,
            )

        except Exception as exc:
            logging.warning(
                "DESTINATION public page fetch failed: "
                "before=%s error=%s",
                before,
                exc,
            )
            break

        (
            page_posts,
            all_page_ids,
        ) = _destination_public_extract_posts(
            page_html
        )

        for message_id, public_text in (
            page_posts.items()
        ):
            if message_id in watched:
                observed[
                    message_id
                ] = public_text

        if not all_page_ids:
            break

        lowest = min(all_page_ids)

        if lowest <= min_message_id:
            break

        if lowest >= before:
            break

        before = lowest

    baseline_count = 0
    same_count = 0
    changed_count = 0
    deleted_count = 0

    # A channel page may legitimately omit old posts because of
    # pagination limits. Verify every unobserved property through its
    # own Telegram embed URL before deactivating it, so stale catalog
    # entries disappear without false positives.
    for message_id in (
        set(watched) - set(observed)
    ):
        (
            property_id,
            _db_message_id,
            _current_price,
            _manual_price,
        ) = watched[message_id]

        exists = await asyncio.to_thread(
            source_public_post_exists,
            destination_username,
            message_id,
        )

        if exists is not False:
            continue

        if not _mark_destination_post_deleted(
            property_id
        ):
            continue

        deleted_count += 1

        logging.warning(
            "DESTINATION public post missing; "
            "property deactivated: property_id=%s "
            "message_id=%s",
            property_id,
            message_id,
        )

        try:
            await queue_targeted_category_sync(
                application.bot,
                [property_id],
                reason="destination-public-post-deleted",
            )
        except Exception:
            logging.exception(
                "Category sync after public deletion failed: "
                "property_id=%s",
                property_id,
            )

    for message_id, public_text in (
        observed.items()
    ):
        (
            property_id,
            _db_message_id,
            _current_price,
            _manual_price,
        ) = watched[message_id]

        try:
            parsed = parse_property(
                public_text
            )

            public_price = parsed.get(
                "current_price_toman"
            )

            if public_price is None:
                continue

            public_price = int(
                public_price
            )

            (
                observation_status,
                previous_public_price,
            ) = _destination_price_watch_observe(
                property_id,
                message_id,
                public_price,
            )

            if observation_status == "BASELINE":
                baseline_count += 1
                continue

            if observation_status == "SAME":
                same_count += 1
                continue

            changed_count += 1

            state = (
                _destination_price_watch_property_state(
                    property_id
                )
            )

            if not state:
                continue

            (
                current_price,
                previous_manual_price,
                status,
                deleted_from_channel_at,
                max_source_price,
            ) = state

            if (
                status != "ACTIVE"
                or deleted_from_channel_at
                    is not None
            ):
                continue

            # If public price changed because our own
            # source-sync updated the post, DB already
            # contains that exact effective price.
            if (
                current_price is not None
                and int(current_price)
                    == public_price
            ):
                logging.info(
                    "DESTINATION public price change "
                    "recognized as bot/source update: "
                    "property_id=%s "
                    "old_public=%s "
                    "new_public=%s "
                    "db_effective=%s",
                    property_id,
                    previous_public_price,
                    public_price,
                    current_price,
                )
                continue

            _destination_price_watch_set_manual(
                property_id,
                public_price,
            )

            from ingest import (
                reconcile_property_price,
            )

            effective_price = (
                reconcile_property_price(
                    property_id
                )
            )

            await refresh_published_property_post(
                application.bot,
                property_id,
            )

            category_result = (
                await sync_categories_for_price_change(
                    application.bot,
                    property_id,
                    current_price,
                    effective_price,
                )
            )

            logging.info(
                "DESTINATION PUBLIC MANUAL PRICE ACCEPTED: "
                "property_id=%s "
                "message_id=%s "
                "old_public=%s "
                "manual_before=%s "
                "manual_new=%s "
                "source_max=%s "
                "effective=%s "
                "categories_synced=%s "
                "categories_removed=%s",
                property_id,
                message_id,
                previous_public_price,
                previous_manual_price,
                public_price,
                max_source_price,
                effective_price,
                category_result.get(
                    "synced",
                    0,
                ),
                category_result.get(
                    "removed",
                    0,
                ),
            )

        except Exception:
            logging.exception(
                "DESTINATION public price processing "
                "failed: property_id=%s "
                "message_id=%s",
                property_id,
                message_id,
            )

    logging.info(
        "DESTINATION public price scan: "
        "watched=%s observed=%s "
        "baseline=%s same=%s changed=%s deleted=%s",
        len(watched),
        len(observed),
        baseline_count,
        same_count,
        changed_count,
        deleted_count,
    )


async def destination_public_price_watcher_loop(
    application,
):
    logging.info(
        "DESTINATION public price watcher started: "
        "interval=%ss",
        DESTINATION_PRICE_WATCH_INTERVAL_SECONDS,
    )

    while True:
        try:
            await run_destination_public_price_watch_tick(
                application
            )

        except asyncio.CancelledError:
            logging.info(
                "DESTINATION public price watcher stopped."
            )
            raise

        except Exception:
            logging.exception(
                "Unhandled DESTINATION public "
                "price watcher error"
            )

        await asyncio.sleep(
            DESTINATION_PRICE_WATCH_INTERVAL_SECONDS
        )


def start_destination_public_price_watcher(
    application,
):
    existing = application.bot_data.get(
        "destination_public_price_watcher_task"
    )

    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        destination_public_price_watcher_loop(
            application
        ),
        name="destination_public_price_watcher",
    )

    application.bot_data[
        "destination_public_price_watcher_task"
    ] = task

    return task


# === EXPLICIT SOURCE FEATURE REPAIR V2 ===
FORCED_NO_ELEVATOR_UNIT_CODES = {"25610"}


def _explicit_feature_repair_rows():
    """
    Read the property raw text plus every recorded source copy.
    The known reported unit is forced once; other units change only when stored source text explicitly says a
    parking, elevator or storage facility is absent.
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.raw_text,
                    p.has_parking,
                    p.has_elevator,
                    p.has_storage,
                    COALESCE(
                        ARRAY_AGG(ps.source_raw_text)
                            FILTER (
                                WHERE ps.source_raw_text IS NOT NULL
                                  AND ps.source_raw_text <> ''
                            ),
                        ARRAY[]::TEXT[]
                    )
                FROM properties p
                LEFT JOIN property_sources ps
                  ON ps.property_id = p.id
                WHERE p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND (
                      p.has_parking IS TRUE
                      OR p.has_elevator IS TRUE
                      OR p.has_storage IS TRUE
                  )
                GROUP BY
                    p.id,
                    p.unit_code,
                    p.raw_text,
                    p.has_parking,
                    p.has_elevator,
                    p.has_storage
                ORDER BY p.id
                """,
            )
            return cur.fetchall()


def _set_explicit_missing_features(
    property_id,
    missing_features,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    has_parking = CASE
                        WHEN %s THEN FALSE
                        ELSE has_parking
                    END,
                    has_elevator = CASE
                        WHEN %s THEN FALSE
                        ELSE has_elevator
                    END,
                    has_storage = CASE
                        WHEN %s THEN FALSE
                        ELSE has_storage
                    END,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'ACTIVE'
                  AND deleted_from_channel_at IS NULL
                """,
                (
                    "parking" in missing_features,
                    "elevator" in missing_features,
                    "storage" in missing_features,
                    property_id,
                ),
            )
            changed = cur.rowcount > 0
            conn.commit()
            return changed


def _source_deed_repair_rows():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.id)
                    p.id,
                    ps.source_raw_text,
                    pa.attribute_value
                FROM properties p
                JOIN property_sources ps
                  ON ps.property_id = p.id
                 AND ps.is_active = TRUE
                LEFT JOIN property_attributes pa
                  ON pa.property_id = p.id
                 AND pa.attribute_key = 'deed_type'
                WHERE p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND ps.source_raw_text IS NOT NULL
                  AND ps.source_raw_text <> ''
                  AND ps.source_raw_text LIKE '%%سند%%'
                ORDER BY
                    p.id,
                    ps.is_primary DESC,
                    ps.id DESC
                """
            )
            return cur.fetchall()


def _parsed_deed_attribute(source_raw_text):
    for item in parse_dynamic_attributes(source_raw_text):
        if item.get("attribute_key") == "deed_type":
            return item
    return None


async def repair_explicit_source_feature_mismatches(application):
    repairs_by_property = {}

    for (
        property_id,
        unit_code,
        property_raw_text,
        has_parking,
        has_elevator,
        has_storage,
        source_raw_texts,
    ) in _explicit_feature_repair_rows():
        candidate_texts = [
            property_raw_text,
            *(source_raw_texts or []),
        ]
        parsed_candidates = [
            parse_property(text)
            for text in candidate_texts
            if text
        ]
        missing_features = set()

        if (
            has_elevator is True
            and str(unit_code or "").strip()
                in FORCED_NO_ELEVATOR_UNIT_CODES
        ):
            missing_features.add("elevator")

        if (
            has_parking is True
            and any(
                parsed.get("has_parking") is False
                for parsed in parsed_candidates
            )
        ):
            missing_features.add("parking")

        if (
            has_elevator is True
            and any(
                parsed.get("has_elevator") is False
                for parsed in parsed_candidates
            )
        ):
            missing_features.add("elevator")

        if (
            has_storage is True
            and any(
                parsed.get("has_storage") is False
                for parsed in parsed_candidates
            )
        ):
            missing_features.add("storage")

        if missing_features:
            repairs_by_property[int(property_id)] = (
                missing_features
            )

    repaired = []

    # Repair elevator mismatches first so the reported channel post
    # is corrected immediately, before slower metadata maintenance.
    for property_id in sorted(repairs_by_property):
        if not _set_explicit_missing_features(
            property_id,
            repairs_by_property[property_id],
        ):
            continue

        repaired.append(property_id)

        try:
            await refresh_published_property_post(
                application.bot,
                property_id,
            )
        except Exception:
            logging.exception(
                "Explicit facility post refresh failed: "
                "property_id=%s",
                property_id,
            )

        await asyncio.sleep(0.35)

    if repaired:
        try:
            await queue_targeted_category_sync(
                application.bot,
                repaired,
                reason="explicit-facility-repair",
            )
        except Exception:
            logging.exception(
                "Category sync after facility repair failed"
            )

    deed_repairs = []

    for property_id, source_raw_text, current_value in _source_deed_repair_rows():
        deed_attribute = _parsed_deed_attribute(source_raw_text)

        if not deed_attribute:
            continue

        new_value = deed_attribute.get("attribute_value")

        if (
            not new_value
            or new_value == current_value
            or new_value in {"دارد", "موجود"}
        ):
            continue

        save_property_attributes(
            property_id,
            [deed_attribute],
        )
        deed_repairs.append(int(property_id))

    for property_id in sorted(set(deed_repairs) - set(repaired)):
        try:
            await refresh_published_property_post(
                application.bot,
                property_id,
            )
        except Exception:
            logging.exception(
                "Exact deed type post refresh failed: "
                "property_id=%s",
                property_id,
            )
        await asyncio.sleep(0.35)

    logging.info(
        "Explicit source feature repair completed: "
        "feature_posts=%s deed_type=%s forced_units=%s",
        len(repaired),
        len(deed_repairs),
        sorted(FORCED_NO_ELEVATOR_UNIT_CODES),
    )


def _extended_location_repair_rows():
    """Return live posts whose source contains the reported named subarea."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.id)
                    p.id,
                    p.address_text,
                    ps.source_raw_text
                FROM properties p
                JOIN property_sources ps
                  ON ps.property_id = p.id
                 AND ps.is_active = TRUE
                WHERE p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND p.published_message_id IS NOT NULL
                  AND ps.source_raw_text IS NOT NULL
                  AND ps.source_raw_text ILIKE '%%لیدای جدید%%'
                ORDER BY
                    p.id,
                    ps.is_primary DESC,
                    ps.id DESC
                """
            )
            return cur.fetchall()


async def repair_extended_source_locations(application):
    repaired = []

    for property_id, current_address, source_raw_text in (
        _extended_location_repair_rows()
    ):
        parsed = parse_property(source_raw_text)
        new_address = parsed.get("address_text")

        if (
            not new_address
            or "لیدای جدید" not in new_address
        ):
            continue

        dynamic_attributes = parse_dynamic_attributes(
            source_raw_text
        )
        rule_attributes = parse_rule_attributes(
            source_raw_text,
            get_active_attribute_rules(),
        )
        merged_attributes = {
            item["attribute_key"]: item
            for item in dynamic_attributes + rule_attributes
        }

        save_property_address(
            property_id,
            new_address,
        )
        save_property_attributes(
            property_id,
            list(merged_attributes.values()),
        )

        if new_address != current_address:
            repaired.append(int(property_id))

        try:
            await refresh_published_property_post(
                application.bot,
                property_id,
            )
        except Exception:
            logging.exception(
                "Extended source location post refresh failed: "
                "property_id=%s",
                property_id,
            )

        await asyncio.sleep(0.35)

    logging.info(
        "Extended source location repair completed: posts=%s",
        len(repaired),
    )


def start_extended_source_location_repair(application):
    existing = application.bot_data.get(
        "extended_source_location_repair_task"
    )

    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        repair_extended_source_locations(application),
        name="extended_source_location_repair",
    )
    application.bot_data[
        "extended_source_location_repair_task"
    ] = task
    return task


def start_explicit_source_feature_repair(application):
    existing = application.bot_data.get(
        "explicit_source_feature_repair_task"
    )

    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        repair_explicit_source_feature_mismatches(
            application
        ),
        name="explicit_source_feature_repair",
    )
    application.bot_data[
        "explicit_source_feature_repair_task"
    ] = task
    return task


async def post_init(application):
    commands = [
        BotCommand("start", "شروع ربات"),
        BotCommand("login", "ورود به حساب"),
        BotCommand("logout", "خروج از حساب"),
    ]

    await application.bot.set_my_commands(commands)
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    ensure_property_trash_schema()

    if get_app_setting(
        "file_validity_weeks",
        None,
    ) is None:
        save_app_setting(
            "file_validity_weeks",
            8,
        )

    ensure_crm_schema()
    ensure_visit_ops_schema()
    ensure_channel_file_editor_schema()
    ensure_footer_bulk_refresh_schema()
    ensure_smart_schema()
    ensure_showcase_schema()
    ensure_campaign_crm_schema()
    start_campaign_watch_worker(application)
    start_campaign_broadcast_worker(application)
    start_visit_reminder_worker(application)
    start_visit_notification_worker(application)
    start_search_watch_worker(application)
    start_footer_bulk_refresh_worker(application)
    start_scheduler(application)
    start_deleted_posts_watcher(application)
    start_destination_public_price_watcher(application)
    start_source_deleted_posts_watcher(application)
    start_explicit_source_feature_repair(application)
    start_extended_source_location_repair(application)

    # Let startup workers emit their immediate errors first, then place
    # the scheduler snapshot at the end of the deployment health log.
    await asyncio.sleep(2)
    log_scheduler_health_snapshot()

    logging.info(
        "Automatic scheduler initialized."
    )


async def show_main_menu(update, session):
    _, username, display_name, role = session
    name = display_name or username

    text = (
        f"🏠 خوش آمدید {name}\n\n"
        "پنل مدیریت و انتشار فایل‌های املاک\n"
        "بخش موردنظر را انتخاب کنید:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=build_main_menu(
                    session,
                    main_menu("ADMIN" if role == "OWNER" else role),
                ),
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=build_main_menu(
                    session,
                    main_menu("ADMIN" if role == "OWNER" else role),
                ),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id

    if await handle_showcase_start(update, context):
        return

    # Customer deep links must be handled BEFORE
    # management-session/login routing.
    if await handle_visit_start(
        update,
        context,
    ):
        return

    await context.bot.set_chat_menu_button(
        chat_id=update.effective_chat.id,
        menu_button=MenuButtonCommands(),
    )

    session = get_session(telegram_user_id)

    if session:
        cleanup = await update.message.reply_text(
            "✅",
            reply_markup=ReplyKeyboardRemove(),
        )
        try:
            await cleanup.delete()
        except Exception:
            pass

        await show_main_menu(update, session)
        return

    # Public customer mode: /start opens ShahFile Smart Customer Flow.
    # Managers can still use /login explicitly.
    context.user_data.pop("login_step", None)
    context.user_data.pop("login_username", None)
    await handle_smart_customer_start(update, context)
    return


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    session = get_session(telegram_user_id)

    if session:
        await show_main_menu(update, session)
        return

    context.user_data.clear()
    context.user_data["login_step"] = "username"

    await update.message.reply_text(
        "🔐 ورود به سیستم\n\n"
        "نام کاربری خود را ارسال کنید:"
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    logout_session(telegram_user_id)
    context.user_data.clear()

    await update.message.reply_text(
        "✅ از حساب کاربری خارج شدید.\n\n"
        "برای ورود مجدد /login را بزنید."
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    session = get_session(telegram_user_id)

    if not session and not context.user_data.get("login_step"):
        if await handle_smart_customer_text(update, context):
            return

    if not session:
        step = context.user_data.get("login_step")

        if step == "username":
            context.user_data["login_username"] = update.message.text.strip()
            context.user_data["login_step"] = "password"

            await update.message.reply_text("🔑 رمز عبور را ارسال کنید:")
            return

        if step == "password":
            username = context.user_data.get("login_username")
            password = update.message.text

            try:
                await update.message.delete()
            except Exception:
                pass

            user = authenticate(username, password)

            if not user:
                final_audit_login(None, False, username=username, telegram_user_id=telegram_user_id)
                context.user_data.clear()
                context.user_data["login_step"] = "username"

                await update.effective_chat.send_message(
                    "❌ نام کاربری یا رمز عبور اشتباه است.\n\n"
                    "نام کاربری را دوباره ارسال کنید:"
                )
                return

            create_session(user["id"], telegram_user_id)
            final_audit_login(user["id"], True, username=username, telegram_user_id=telegram_user_id)
            context.user_data.clear()

            session = get_session(telegram_user_id)

            await update.effective_chat.send_message("✅ ورود موفق")

            _, db_username, display_name, role = session
            name = display_name or db_username

            await update.effective_chat.send_message(
                f"🏠 خوش آمدید {name}\n\n"
                "پنل مدیریت و انتشار فایل‌های املاک",
                reply_markup=build_main_menu(
                    session,
                    main_menu("ADMIN" if role == "OWNER" else role),
                ),
            )
            return

        context.user_data["login_step"] = "username"

        await update.message.reply_text(
            "🔐 ابتدا وارد سیستم شوید.\n\n"
            "نام کاربری خود را ارسال کنید:"
        )
        return

    state = context.user_data.get("state")

    # CHANNEL_FILE_EDITOR_TEXT_ROUTER_V1
    if await handle_file_editor_text(
        update,
        context,
        session,
        allowed=check_action_access(session, "channel_edit"),
    ):
        return

    # SHAHFILE_SMART_MANAGER_TEXT_ROUTER_V1
    if await handle_smart_manager_text(update, context, session):
        return

    # FINAL_FEATURES_TEXT_ROUTER
    if await handle_final_aux_text(update, context, session):
        return
    if await handle_final_text(update, context, session):
        return

    # CRM_TEXT_ROUTER_V1
    if await handle_crm_text(
        update,
        context,
        session,
    ):
        return

    if state == "attribute_rule_label":
        value = update.message.text.strip()

        context.user_data["attribute_rule_label"] = value
        context.user_data.pop(
            "attribute_rule_match_type",
            None,
        )
        context.user_data["state"] = None

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ وجود عبارت",
                        callback_data="attribute_rule_type_PRESENCE",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📝 مقدار بعد از عبارت",
                        callback_data="attribute_rule_type_VALUE_AFTER",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔢 عدد قبل از عبارت",
                        callback_data="attribute_rule_type_NUMBER_BEFORE",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ بازگشت",
                        callback_data="attribute_rules",
                    )
                ],
            ]
        )

        await update.message.reply_text(
            f"مشخصه: {value}\n\n"
            "روش استخراج را انتخاب کنید:",
            reply_markup=keyboard,
        )
        return

    if state == "attribute_rule_trigger":
        trigger = update.message.text.strip()

        label = context.user_data.get(
            "attribute_rule_label"
        )

        match_type = context.user_data.get(
            "attribute_rule_match_type"
        )

        if not label or not match_type:
            context.user_data.pop("state", None)

            await update.message.reply_text(
                "❌ اطلاعات Rule کامل نیست.",
                reply_markup=attribute_rules_menu(),
            )
            return

        key = (
            "custom_"
            + hashlib.sha1(
                label.encode("utf-8")
            ).hexdigest()[:12]
        )

        default_value = (
            "دارد"
            if match_type == "PRESENCE"
            else None
        )

        saved_rule = create_attribute_rule(
            attribute_key=key,
            attribute_label=label,
            match_type=match_type,
            trigger_text=trigger,
            default_value=default_value,
            display_order=100,
        )

        if not saved_rule:
            await update.message.reply_text(
                "❌ ذخیره مشخصه در دیتابیس ناموفق بود."
            )
            return

        context.user_data.pop("state", None)
        context.user_data.pop(
            "attribute_rule_label",
            None,
        )
        context.user_data.pop(
            "attribute_rule_match_type",
            None,
        )

        type_names = {
            "PRESENCE": "وجود عبارت",
            "VALUE_AFTER": "مقدار بعد از عبارت",
            "NUMBER_BEFORE": "عدد قبل از عبارت",
        }

        await update.message.reply_text(
            f"✅ مشخصه «{label}» ذخیره شد.\n\n"
            f"روش استخراج: {type_names[match_type]}\n"
            f"عبارت تشخیص: {trigger}",
            reply_markup=attribute_rules_menu(),
        )
        return

    if state == "file_validity_weeks_input":
        value = (
            update.message.text
            .strip()
            .replace("۰", "0")
            .replace("۱", "1")
            .replace("۲", "2")
            .replace("۳", "3")
            .replace("۴", "4")
            .replace("۵", "5")
            .replace("۶", "6")
            .replace("۷", "7")
            .replace("۸", "8")
            .replace("۹", "9")
        )

        try:
            weeks = int(value)
        except ValueError:
            weeks = 0

        if weeks < 1 or weeks > 104:
            await update.message.reply_text(
                "❌ عدد معتبر وارد کنید.\n\n"
                "مدت اعتبار باید بین 1 تا 104 هفته باشد."
            )
            return

        expired_rows = get_expired_property_rows(
            weeks
        )

        expired_count = len(
            expired_rows
        )

        context.user_data.pop(
            "state",
            None,
        )

        context.user_data[
            "pending_file_validity_weeks"
        ] = weeks

        if expired_count:
            warning_text = (
                f"⚠️ مدت اعتبار جدید: {weeks} هفته\n\n"
                f"با اعمال این تنظیم، "
                f"{expired_count} فایل قدیمی‌تر از "
                f"{weeks} هفته از کانال اصلی حذف می‌شوند "
                "و به سطل آشغال منتقل خواهند شد.\n\n"
                "اطلاعات آن‌ها از دیتابیس پاک نمی‌شود "
                "و بعداً قابل بازیابی هستند.\n\n"
                "آیا مطمئن هستید؟"
            )
        else:
            warning_text = (
                f"🗓 مدت اعتبار جدید: {weeks} هفته\n\n"
                "در حال حاضر هیچ فایل فعالی "
                "قدیمی‌تر از این بازه وجود ندارد.\n\n"
                "آیا این مقدار ذخیره شود؟"
            )

        await update.message.reply_text(
            warning_text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ تأیید نهایی",
                            callback_data=(
                                f"file_validity_confirm_{weeks}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ انصراف",
                            callback_data="settings_file_validity",
                        )
                    ],
                ]
            ),
        )
        return


    if state == "footer_contact_name":
        value = update.message.text.strip()

        if not value:
            await update.message.reply_text(
                "❌ نام نمی‌تواند خالی باشد.\n\n"
                "نام شخص را دوباره ارسال کنید."
            )
            return

        if len(value) > 120:
            await update.message.reply_text(
                "❌ نام بیش از حد طولانی است.\n\n"
                "نام کوتاه‌تری ارسال کنید."
            )
            return

        context.user_data[
            "footer_contact_pending_name"
        ] = value
        context.user_data[
            "state"
        ] = "footer_contact_phone"

        person_number = len(
            context.user_data.get(
                "footer_contact_people_draft",
                [],
            )
        ) + 1

        await update.message.reply_text(
            f"📞 شماره تماس شخص {person_number} را ارسال کنید.\n\n"
            f"نام: {value}"
        )
        return

    if state == "footer_contact_phone":
        pending_name = context.user_data.get(
            "footer_contact_pending_name"
        )

        if not pending_name:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ اطلاعات مخاطب ناقص شد. دوباره تلاش کنید.",
                reply_markup=footer_settings_menu(),
            )
            return

        try:
            phone = _normalize_contact_phone(
                update.message.text
            )
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ {exc}\n\n"
                "شماره تماس را دوباره ارسال کنید."
            )
            return

        people = context.user_data.setdefault(
            "footer_contact_people_draft",
            [],
        )
        people.append(
            {
                "name": pending_name,
                "phone": phone,
            }
        )

        context.user_data.pop(
            "footer_contact_pending_name",
            None,
        )
        context.user_data[
            "state"
        ] = None

        await update.message.reply_text(
            "✅ این شخص اضافه شد.\n\n"
            + format_contact_people(people)
            + "\n\nآیا شخص دیگری هم هست؟",
            reply_markup=_contact_people_question_keyboard(),
        )
        return


    if state == "edit_publish_setting":
        setting_key = context.user_data.get(
            "setting_key"
        )

        value = update.message.text.strip()

        if not setting_key:
            context.user_data.pop("state", None)

            await update.message.reply_text(
                "❌ تنظیم موردنظر مشخص نیست.",
                reply_markup=settings_menu(),
            )
            return

        old_value = get_app_setting(
            setting_key,
            None,
        )

        save_app_setting(
            setting_key,
            value,
        )

        label = PUBLISH_SETTING_LABELS.get(
            setting_key,
            setting_key,
        )

        context.user_data.pop("state", None)
        context.user_data.pop("setting_key", None)

        if setting_key in {
            "publish_footer",
            "contact_telegram",
        }:
            audit_footer_setting_change(
                session[0],
                setting_key,
                old_value,
                value,
            )
            await update.message.reply_text(
                footer_bulk_offer_text(
                    f"✅ {label} ذخیره شد.\n\n"
                    f"مقدار جدید:\n{value}"
                ),
                reply_markup=footer_bulk_offer_markup(),
            )
        else:
            await update.message.reply_text(
                f"✅ {label} ذخیره شد.\n\n"
                f"مقدار جدید:\n{value}",
                reply_markup=settings_menu(),
            )
        return

    if state == "force_delete_unit_code":
        unit_code = (
            update.message.text
            .strip()
            .replace("۰", "0")
            .replace("۱", "1")
            .replace("۲", "2")
            .replace("۳", "3")
            .replace("۴", "4")
            .replace("۵", "5")
            .replace("۶", "6")
            .replace("۷", "7")
            .replace("۸", "8")
            .replace("۹", "9")
        )

        row = find_property_by_unit_code(
            unit_code
        )

        if not row:
            await update.message.reply_text(
                f"❌ فایلی با کد واحد {unit_code} پیدا نشد.\n\n"
                "کد را دوباره ارسال کنید.",
                reply_markup=files_menu(),
            )
            return

        (
            property_id,
            found_unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            published_message_id,
            channel_post_url,
        ) = row

        if not published_message_id:
            await update.message.reply_text(
                "⚠️ این فایل در کانال اصلی پست منتشرشده‌ای ندارد.\n\n"
                "اگر هدفتان حذف آن از صف انتشار است، "
                "این عملیات را جداگانه انجام می‌دهیم.",
                reply_markup=files_menu(),
            )
            return

        context.user_data.pop(
            "state",
            None,
        )

        await update.message.reply_text(
            "⏳ در حال حذف فوری فایل از کانال..."
        )

        try:
            result = await force_delete_property(
                context.bot,
                property_id,
            )

            await update.message.reply_text(
                "✅ فایل فوراً حذف شد.\n\n"
                f"کد واحد: {result['unit_code']}\n"
                f"پیام‌های حذف‌شده: "
                f"{result['deleted_messages']}\n"
                "وضعیت فایل: غیرفعال\n"
                "دسته‌بندی‌ها نیز بروزرسانی شدند.",
                reply_markup=files_menu(),
            )

        except Exception as exc:
            logging.exception(
                "Force delete failed: "
                "property_id=%s unit_code=%s",
                property_id,
                unit_code,
            )

            await update.message.reply_text(
                "❌ حذف فورس کامل انجام نشد.\n\n"
                f"خطا: {str(exc)[:700]}",
                reply_markup=files_menu(),
            )

        return

    if state == "republish_group_custom_count":
        raw_count = (
            update.message.text
            .strip()
            .replace("۰", "0")
            .replace("۱", "1")
            .replace("۲", "2")
            .replace("۳", "3")
            .replace("۴", "4")
            .replace("۵", "5")
            .replace("۶", "6")
            .replace("۷", "7")
            .replace("۸", "8")
            .replace("۹", "9")
        )

        try:
            count = int(raw_count)
        except ValueError:
            await update.message.reply_text(
                "❌ لطفاً فقط یک عدد ارسال کنید.\n\n"
                "مثال: 10",
                reply_markup=files_menu(),
            )
            return

        if count < 1 or count > 100:
            await update.message.reply_text(
                "❌ تعداد باید بین 1 تا 100 فایل باشد.",
                reply_markup=files_menu(),
            )
            return

        context.user_data.pop("state", None)
        context.user_data["pending_group_republish_count"] = count

        await update.message.reply_text(
            "🔁 تأیید بازنشر گروهی\n\n"
            f"تعداد درخواستی: {count} فایل\n\n"
            "فقط فایل‌های قدیمیِ معتبر انتخاب می‌شوند.\n"
            "فایل منقضی، فروخته‌شده، حذف‌شده یا موجود در سطل آشغال "
            "انتخاب نمی‌شود.\n\n"
            "کد واحد فایل‌ها تغییر نمی‌کند.\n"
            "این عملیات فایل جدیدی در INPUT یا SOURCE ایجاد نمی‌کند.\n\n"
            "تأیید می‌کنید؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ تأیید و افزودن به صف",
                            callback_data=f"republish_group_confirm_{count}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ انصراف",
                            callback_data="republish_group",
                        )
                    ],
                ]
            ),
        )
        return

    if state == "republish_unit_code":
        unit_code = update.message.text.strip()

        row = find_property_by_unit_code(
            unit_code
        )

        if not row:
            await update.message.reply_text(
                f"❌ فایلی با کد واحد {unit_code} پیدا نشد.\n\n"
                "کد را دوباره ارسال کنید.",
                reply_markup=back_menu(),
            )
            return

        context.user_data.pop(
            "state",
            None,
        )

        (
            property_id,
            unit_code,
            district,
            address_text,
            area_m2,
            price_billion,
            published_message_id,
            channel_post_url,
        ) = row

        if not published_message_id:
            await update.message.reply_text(
                "⚠️ این فایل هنوز قبلاً منتشر نشده است.\n"
                "برای اولین انتشار از «انتشار فوری» استفاده کنید.",
                reply_markup=back_menu(),
            )
            return

        publish_row = get_property_for_publish(
            property_id
        )

        preview = build_publish_text(
            publish_row,
            unit_code=unit_code,
        )

        media_rows = get_property_media(
            property_id
        )

        photo_count = len(
            [
                item
                for item in media_rows
                if item[1] == "PHOTO"
            ]
        )

        await update.message.reply_text(
            "🔄 پیش‌نمایش بازنشر\n\n"
            f"تعداد عکس‌ها: {photo_count}\n"
            f"پیام فعلی: {published_message_id}\n\n"
            + preview
            + "\n\n"
            "⚠️ بازنشر با همان کد واحد انجام می‌شود "
            "و کد جدیدی مصرف نمی‌شود.",
            reply_markup=republish_confirm_keyboard(
                property_id
            ),
        )
        return

    if state == "schedule_start_time":
        start_time = parse_schedule_time(
            update.message.text
        )

        if not start_time:
            await update.message.reply_text(
                "❌ ساعت نامعتبر است.\n\n"
                "مثال صحیح:\n09:30"
            )
            return

        context.user_data[
            "schedule_start_time"
        ] = start_time

        context.user_data[
            "state"
        ] = "schedule_end_time"

        await update.message.reply_text(
            "🕐 ساعت پایان بازه را ارسال کنید.\n\n"
            "مثال:\n23:00"
        )
        return


    if state == "schedule_end_time":
        end_time = parse_schedule_time(
            update.message.text
        )

        if not end_time:
            await update.message.reply_text(
                "❌ ساعت نامعتبر است.\n\n"
                "مثال صحیح:\n23:00"
            )
            return

        context.user_data[
            "schedule_end_time"
        ] = end_time

        context.user_data[
            "state"
        ] = "schedule_pause"

        await update.message.reply_text(
            "⏸ آیا وسط این بازه ساعتی هست که "
            "نخواهید هیچ فایلی منتشر شود؟\n\n"
            "مثال:\n14:00 تا 17:00\n\n"
            "اگر توقف نمی‌خواهید عدد 0 را ارسال کنید."
        )
        return


    if state == "schedule_pause":
        value = update.message.text.strip()

        if value == "0":
            pause_start = None
            pause_end = None

        else:
            normalized = (
                value
                .replace("الی", "تا")
                .replace("–", "تا")
                .replace("—", "تا")
                .replace("-", "تا")
            )

            parts = [
                part.strip()
                for part in normalized.split("تا")
                if part.strip()
            ]

            if len(parts) != 2:
                await update.message.reply_text(
                    "❌ فرمت بازه توقف صحیح نیست.\n\n"
                    "مثال:\n14:00 تا 17:00\n\n"
                    "اگر توقف نمی‌خواهید: 0"
                )
                return

            pause_start = parse_schedule_time(
                parts[0]
            )

            pause_end = parse_schedule_time(
                parts[1]
            )

            if not pause_start or not pause_end:
                await update.message.reply_text(
                    "❌ ساعت توقف نامعتبر است.\n\n"
                    "مثال صحیح:\n14:00 تا 17:00"
                )
                return

            window_start = context.user_data.get(
                "schedule_start_time"
            )

            window_end = context.user_data.get(
                "schedule_end_time"
            )

            def minute_value(t):
                return (
                    t.hour * 60
                    + t.minute
                )

            ws = minute_value(window_start)
            we = minute_value(window_end)

            ps = minute_value(pause_start)
            pe = minute_value(pause_end)

            if we <= ws:
                we += 1440

            if ps < ws:
                ps += 1440

            if pe < ws:
                pe += 1440

            if pe <= ps:
                pe += 1440

            if not (
                ws <= ps
                and ps < pe
                and pe <= we
            ):
                await update.message.reply_text(
                    "❌ بازه توقف باید کاملاً داخل "
                    "بازه اصلی انتشار باشد.\n\n"
                    "مثلاً اگر انتشار 10:30 تا 22:00 است:\n"
                    "14:00 تا 17:00"
                )
                return

        context.user_data[
            "schedule_pause_start"
        ] = pause_start

        context.user_data[
            "schedule_pause_end"
        ] = pause_end

        context.user_data[
            "state"
        ] = "schedule_interval"

        await update.message.reply_text(
            "⏱ فاصله بین هر انتشار چند دقیقه باشد؟\n\n"
            "مثال:\n30"
        )
        return


    if state == "schedule_interval":
        value = update.message.text.strip()

        try:
            interval_minutes = int(
                value
            )

        except ValueError:
            interval_minutes = 0

        if interval_minutes < 1:
            await update.message.reply_text(
                "❌ فاصله انتشار باید حداقل 1 دقیقه باشد."
            )
            return

        context.user_data[
            "schedule_interval"
        ] = interval_minutes

        context.user_data[
            "state"
        ] = "schedule_max_posts"

        await update.message.reply_text(
            "📦 حداکثر چند پست در این بازه منتشر شود؟\n\n"
            "اگر محدودیت نمی‌خواهید عدد 0 را ارسال کنید.\n\n"
            "مثال:\n10"
        )
        return


    if state == "schedule_max_posts":
        value = update.message.text.strip()

        try:
            max_posts_value = int(
                value
            )

        except ValueError:
            max_posts_value = -1

        if max_posts_value < 0:
            await update.message.reply_text(
                "❌ یک عدد صفر یا بیشتر ارسال کنید."
            )
            return

        max_posts = (
            None
            if max_posts_value == 0
            else max_posts_value
        )

        weekday = context.user_data.get(
            "schedule_weekday"
        )

        start_time = context.user_data.get(
            "schedule_start_time"
        )

        end_time = context.user_data.get(
            "schedule_end_time"
        )

        interval_minutes = context.user_data.get(
            "schedule_interval"
        )

        pause_start = context.user_data.get(
            "schedule_pause_start"
        )

        pause_end = context.user_data.get(
            "schedule_pause_end"
        )

        all_days = context.user_data.get(
            "schedule_all_days",
            False,
        )

        edit_window_id = context.user_data.get(
            "schedule_edit_id"
        )

        edit_group_key = context.user_data.get(
            "schedule_edit_group_key"
        )

        if (
            start_time is None
            or end_time is None
            or interval_minutes is None
        ):
            context.user_data.clear()

            await update.message.reply_text(
                "❌ اطلاعات زمان‌بندی ناقص شد. "
                "لطفاً دوباره از منوی زمان‌بندی شروع کنید.",
                reply_markup=back_menu(),
            )
            return

        if (
            not edit_group_key
            and not all_days
            and weekday is None
        ):
            context.user_data.clear()

            await update.message.reply_text(
                "❌ روز زمان‌بندی مشخص نشده است.",
                reply_markup=back_menu(),
            )
            return

        max_text = (
            str(max_posts)
            if max_posts is not None
            else "نامحدود"
        )

        if edit_group_key:
            updated_count = update_schedule_group(
                group_key=edit_group_key,
                start_time=start_time,
                end_time=end_time,
                interval_minutes=interval_minutes,
                max_posts=max_posts,
                pause_start=pause_start,
                pause_end=pause_end,
            )

            context.user_data.clear()

            if not updated_count:
                await update.message.reply_text(
                    "❌ گروه زمان‌بندی پیدا نشد.",
                    reply_markup=schedule_menu(),
                )
                return

            await update.message.reply_text(
                "✅ زمان‌بندی همه روزها ویرایش شد.\n\n"
                f"ساعت: {start_time.strftime('%H:%M')} "
                f"تا {end_time.strftime('%H:%M')}\n"
                f"فاصله انتشار: {interval_minutes} دقیقه\n"
                f"سقف پست: {max_text}\n\n"
                f"تعداد روزهای بروزشده: {updated_count}",
                reply_markup=schedule_menu(),
            )
            return

        if edit_window_id:
            updated_id = update_schedule_window(
                window_id=edit_window_id,
                weekday=weekday,
                start_time=start_time,
                end_time=end_time,
                interval_minutes=interval_minutes,
                max_posts=max_posts,
                pause_start=pause_start,
                pause_end=pause_end,
            )

            if not updated_id:
                context.user_data.clear()

                await update.message.reply_text(
                    "❌ بازه موردنظر برای ویرایش پیدا نشد.",
                    reply_markup=schedule_menu(),
                )
                return

            day_name = WEEKDAY_NAMES.get(
                weekday,
                str(weekday),
            )

            context.user_data.clear()

            await update.message.reply_text(
                "✅ بازه زمان‌بندی ویرایش شد.\n\n"
                f"شناسه: #{updated_id}\n"
                f"روز: {day_name}\n"
                f"ساعت: {start_time.strftime('%H:%M')} "
                f"تا {end_time.strftime('%H:%M')}\n"
                f"فاصله انتشار: {interval_minutes} دقیقه\n"
                f"سقف پست: {max_text}",
                reply_markup=schedule_menu(),
            )
            return

        if all_days:
            created_ids = []

            group_key = (
                "weekly-"
                + uuid.uuid4().hex[:16]
            )

            for day in range(7):
                window_id = create_schedule_window(
                    weekday=day,
                    start_time=start_time,
                    end_time=end_time,
                    interval_minutes=interval_minutes,
                    max_posts=max_posts,
                    pause_start=pause_start,
                    pause_end=pause_end,
                    group_key=group_key,
                )

                created_ids.append(
                    window_id
                )

            context.user_data.clear()

            await update.message.reply_text(
                "✅ بازه برای همه روزهای هفته ساخته شد.\n\n"
                f"تعداد بازه‌ها: {len(created_ids)}\n"
                f"ساعت: {start_time.strftime('%H:%M')} "
                f"تا {end_time.strftime('%H:%M')}\n"
                f"فاصله انتشار: {interval_minutes} دقیقه\n"
                f"سقف پست در هر روز: {max_text}\n"
                "وضعیت همه بازه‌ها: 🟢 فعال",
                reply_markup=schedule_menu(),
            )
            return

        window_id = create_schedule_window(
            weekday=weekday,
            start_time=start_time,
            end_time=end_time,
            interval_minutes=interval_minutes,
            max_posts=max_posts,
            pause_start=pause_start,
            pause_end=pause_end,
        )

        day_name = WEEKDAY_NAMES.get(
            weekday,
            str(weekday),
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ بازه زمان‌بندی ساخته شد.\n\n"
            f"شناسه: #{window_id}\n"
            f"روز: {day_name}\n"
            f"ساعت: {start_time.strftime('%H:%M')} "
            f"تا {end_time.strftime('%H:%M')}\n"
            f"فاصله انتشار: {interval_minutes} دقیقه\n"
            f"سقف پست: {max_text}\n"
            "وضعیت: 🟢 فعال",
            reply_markup=schedule_menu(),
        )
        return


    if state == "urgent_source_code":
        source_code = update.message.text.strip()

        rows = find_properties_for_urgent_lookup(
            source_code
        )

        if not rows:
            await update.message.reply_text(
                "❌ فایل پیدا نشد.\n\n"
                "یکی از این موارد را ارسال کنید:\n"
                "• شماره پیگیری صف، مثل: صف 45\n"
                "• کد فایل منبع\n"
                "• لینک همان پست در کانال منبع\n"
                "• Message ID پست منبع",
                reply_markup=back_menu(),
            )
            return

        context.user_data.pop("state", None)

        if len(rows) > 1:
            await update.message.reply_text(
                f"🔎 برای کد {source_code} چند فایل پیدا شد.\n"
                "فایل موردنظر را انتخاب کنید:",
                reply_markup=urgent_candidates_keyboard(rows),
            )
            return

        property_id = rows[0][0]
        published_message_id = rows[0][4]
        unit_code = rows[0][5]

        # Re-parse the latest raw source text before building
        # the urgent publish preview. This keeps older files
        # compatible with newer parser improvements.
        latest_source_raw_text = None

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_raw_text
                    FROM property_sources
                    WHERE property_id = %s
                      AND source_raw_text IS NOT NULL
                      AND source_raw_text <> ''
                    ORDER BY
                        is_primary DESC,
                        is_active DESC,
                        id DESC
                    LIMIT 1
                    """,
                    (property_id,),
                )

                source_row = cur.fetchone()

                if source_row:
                    latest_source_raw_text = (
                        source_row[0]
                    )

        if latest_source_raw_text:
            reparsed = parse_property(
                latest_source_raw_text
            )

            update_parsed_property(
                property_id,
                reparsed,
            )

            save_property_address(
                property_id,
                reparsed.get("address_text"),
            )

            dynamic_attributes = (
                parse_dynamic_attributes(
                    latest_source_raw_text
                )
            )

            rule_attributes = (
                parse_rule_attributes(
                    latest_source_raw_text,
                    get_active_attribute_rules(),
                )
            )

            merged_attributes = {}

            for item in (
                dynamic_attributes
                + rule_attributes
            ):
                merged_attributes[
                    item["attribute_key"]
                ] = item

            save_property_attributes(
                property_id,
                list(
                    merged_attributes.values()
                ),
            )

            logging.info(
                "Urgent preview re-parsed: "
                "property_id=%s source_code=%s",
                property_id,
                source_code,
            )

        duplicate = detect_and_store_duplicate(
            property_id
        )

        if (
            duplicate
            and duplicate.get("review_status")
            != "APPROVED"
        ):
            await update.message.reply_text(
                build_duplicate_warning_text(
                    property_id,
                    duplicate,
                ),
                reply_markup=urgent_duplicate_keyboard(
                    property_id
                ),
            )
            return

        row = get_property_for_publish(
            property_id
        )

        preview = build_publish_text(
            row
        )

        await update.message.reply_text(
            "🚨 پیش‌نمایش انتشار فوری\n\n"
            + preview,
            reply_markup=urgent_confirm_keyboard(
                property_id
            ),
        )
        return

    if state == "smart_search":
        raw_query = (
            update.message.text
            or ""
        ).strip()

        if not raw_query:
            await update.message.reply_text(
                "❌ عبارت جستجو خالی است."
            )
            return

        result = smart_search_properties(
            smart_search_display_query(raw_query),
            offset=0,
            limit=10,
        )

        context.user_data[
            "smart_search_query"
        ] = raw_query

        context.user_data[
            "smart_search_offset"
        ] = 0

        context.user_data.pop(
            "state",
            None,
        )

        await update.message.reply_text(
            smart_search_result_text(
                raw_query,
                result,
            ),
            reply_markup=smart_search_keyboard(
                result
            ),
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return

    if state == "search_queue":
        raw_value = (
            update.message.text
            or ""
        ).strip()

        normalized = raw_value.translate(
            str.maketrans(
                "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                "01234567890123456789",
            )
        )

        normalized = normalized.replace(
            "#",
            " ",
        )

        match = re.search(
            r"(\d+)",
            normalized,
        )

        if not match:
            await update.message.reply_text(
                "❌ شماره صف معتبر نیست.\n\n"
                "مثال:\n"
                "289\n"
                "یا\n"
                "صف 289"
            )
            return

        property_id = int(
            match.group(1)
        )

        row = get_queue_search_detail(
            property_id
        )

        if not row:
            await update.message.reply_text(
                f"❌ فایل «صف {property_id}» "
                "در صف فعال پیدا نشد.\n\n"
                "ممکن است فایل منتشر، رد یا حذف شده باشد.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت به صف",
                                callback_data="queue",
                            )
                        ]
                    ]
                ),
            )
            return

        context.user_data.pop(
            "state",
            None,
        )

        await update.message.reply_text(
            build_queue_search_detail(
                row
            ),
            reply_markup=queue_search_result_keyboard(
                row
            ),
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return


    if state == "search_unit":
        unit_code = (
            update.message.text
            .strip()
            .replace("۰", "0")
            .replace("۱", "1")
            .replace("۲", "2")
            .replace("۳", "3")
            .replace("۴", "4")
            .replace("۵", "5")
            .replace("۶", "6")
            .replace("۷", "7")
            .replace("۸", "8")
            .replace("۹", "9")
        )

        property_row, source_rows = (
            get_unit_code_full_details(
                unit_code
            )
        )

        if not property_row:
            await update.message.reply_text(
                f"❌ فایلی با کد واحد {unit_code} پیدا نشد.\n\n"
                "کد را دوباره ارسال کنید.",
                reply_markup=back_menu(),
            )
            return

        context.user_data.pop(
            "state",
            None,
        )

        await update.message.reply_text(
            build_unit_code_search_text(
                property_row,
                source_rows,
                session=session,
            ),
            reply_markup=back_menu(),
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return

    if state == "category_range":
        price_range_text = (
            update.message.text.strip()
        )

        parsed_range = parse_category_range(
            price_range_text
        )

        if not parsed_range:
            await update.message.reply_text(
                "❌ بازه قیمت صحیح نیست.\n\n"
                "مثال صحیح:\n8 تا 15"
            )
            return

        low, high = parsed_range

        rows = get_published_properties_in_range(
            low,
            high,
        )

        total = len(rows)

        if total == 0:
            context.user_data.pop(
                "state",
                None,
            )

            await update.message.reply_text(
                "⚠️ هیچ فایل منتشرشده‌ای "
                "در این بازه قیمت پیدا نشد.",
                reply_markup=back_menu(),
            )
            return

        context.user_data[
            "category_low"
        ] = low

        context.user_data[
            "category_high"
        ] = high

        context.user_data.pop(
            "state",
            None,
        )

        hashtag = build_category_hashtag(
            low,
            high,
        )

        await update.message.reply_text(
            "🗂 دسته‌بندی آماده است.\n\n"
            f"{hashtag}\n"
            f"تعداد فایل‌های منتشرشده: {total}\n\n"
            "کدام گروه 10 تایی به کانال ارسال شود؟",
            reply_markup=category_pages_keyboard(
                total
            ),
        )
        return

    await update.message.reply_text(
        "از دکمه‌های پنل مدیریت استفاده کنید.",
        reply_markup=build_main_menu(session, main_menu("ADMIN" if session[3] == "OWNER" else session[3])),
    )





def find_property_for_source_sync(
    telegram_channel_id,
    source_message_id,
    source_unit_code=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM channels
                WHERE telegram_channel_id = %s
                LIMIT 1
                """,
                (telegram_channel_id,),
            )

            channel_row = cur.fetchone()

            if not channel_row:
                return None

            channel_db_id = channel_row[0]

            # 1) Highest priority:
            # exact source channel + Telegram message ID.
            if source_message_id:
                cur.execute(
                    """
                    SELECT p.id
                    FROM property_sources ps
                    JOIN properties p
                      ON p.id = ps.property_id
                    WHERE ps.source_channel_id = %s
                      AND ps.source_message_id = %s
                      AND ps.is_active = TRUE
                    ORDER BY
                        ps.is_primary DESC,
                        ps.id DESC
                    LIMIT 1
                    """,
                    (
                        channel_db_id,
                        source_message_id,
                    ),
                )

                row = cur.fetchone()

                if row:
                    return row[0]

            # 2) Fallback:
            # source code inside the same source channel.
            if source_unit_code:
                cur.execute(
                    """
                    SELECT p.id
                    FROM property_sources ps
                    JOIN properties p
                      ON p.id = ps.property_id
                    WHERE ps.source_channel_id = %s
                      AND ps.source_unit_code = %s
                      AND ps.is_active = TRUE
                    ORDER BY
                        ps.is_primary DESC,
                        ps.id DESC
                    LIMIT 1
                    """,
                    (
                        channel_db_id,
                        source_unit_code,
                    ),
                )

                row = cur.fetchone()

                if row:
                    return row[0]

            return None


def replace_category_post_items(
    category_post_id,
    rows,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM category_post_items
                WHERE category_post_id = %s
                """,
                (category_post_id,),
            )

            for position, row in enumerate(
                rows,
                start=1,
            ):
                property_id = row[0]
                price_toman = row[8]

                cur.execute(
                    """
                    INSERT INTO category_post_items
                    (
                        category_post_id,
                        property_id,
                        position,
                        snapshot_price_toman
                    )
                    VALUES
                    (%s, %s, %s, %s)
                    """,
                    (
                        category_post_id,
                        property_id,
                        position,
                        (
                            int(price_toman)
                            if price_toman is not None
                            else None
                        ),
                    ),
                )

            cur.execute(
                """
                UPDATE category_posts
                SET updated_at = NOW()
                WHERE id = %s
                """,
                (category_post_id,),
            )

            conn.commit()


def deactivate_category_post(
    category_post_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE category_posts
                SET
                    is_active = FALSE,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (category_post_id,),
            )

            cur.execute(
                """
                DELETE FROM category_post_items
                WHERE category_post_id = %s
                """,
                (category_post_id,),
            )

            conn.commit()


async def _edit_category_message_with_retry(
    bot,
    *,
    chat_id,
    message_id,
    text,
    reply_markup,
    max_attempts=4,
):
    """
    Edit category messages with automatic retry for
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
                "Telegram flood control during category edit: "
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
                "Telegram timeout during category edit: "
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


async def sync_active_category_posts(
    bot,
    category_ids=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    min_price_billion,
                    max_price_billion,
                    page_offset,
                    page_size,
                    telegram_channel_id,
                    telegram_message_id
                FROM category_posts
                WHERE is_active = TRUE
                  AND COALESCE(updated_at, created_at)
                      >= NOW() - INTERVAL '11 days'
                ORDER BY id
                """
            )

            category_rows = cur.fetchall()

    # === TARGETED PRICE CATEGORY FILTER V1 ===
    if category_ids is not None:
        wanted_category_ids = {
            int(value)
            for value in category_ids
        }

        category_rows = [
            row
            for row in category_rows
            if int(row[0])
            in wanted_category_ids
        ]

    synced = 0
    removed = 0

    for category_row in category_rows:
        (
            category_post_id,
            low,
            high,
            offset,
            page_size,
            channel_id,
            message_id,
        ) = category_row

        page_size = int(
            page_size or 10
        )

        all_rows = (
            get_published_properties_in_range(
                float(low),
                float(high),
            )
        )

        total = len(all_rows)

        # No files left in this category.
        if total == 0:
            try:
                await bot.delete_message(
                    chat_id=channel_id,
                    message_id=message_id,
                )
            except Exception as exc:
                logging.warning(
                    "Could not delete empty category post %s: %s",
                    category_post_id,
                    exc,
                )

            deactivate_category_post(
                category_post_id
            )

            removed += 1
            continue

        # If deletion/sale caused the current page
        # to disappear, move to the new last page.
        last_offset = (
            (total - 1) // page_size
        ) * page_size

        offset = int(offset or 0)

        if offset > last_offset:
            offset = last_offset

            update_category_post_offset(
                category_post_id,
                offset,
            )

        page_rows = all_rows[
            offset:
            offset + page_size
        ]

        text = build_category_post_text(
            float(low),
            float(high),
            page_rows,
            offset=offset,
            total=total,
            page_size=page_size,
        )

        keyboard = (
            category_navigation_keyboard(
                category_post_id,
                offset,
                total,
                page_size,
            )
        )

        try:
            await _edit_category_message_with_retry(
                bot,
                chat_id=channel_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )

        except Exception as exc:
            error_text = str(
                exc
            ).lower()

            if (
                "message is not modified"
                in error_text
            ):
                pass

            elif isinstance(exc, RetryAfter):
                logging.warning(
                    "Category sync batch paused after Telegram flood control: "
                    "category_post_id=%s",
                    category_post_id,
                )
                break

            elif (
                "message to edit not found"
                in error_text
                or
                "message to be edited not found"
                in error_text
            ):
                deactivate_category_post(
                    category_post_id
                )

                removed += 1

                logging.warning(
                    "Category post deactivated because "
                    "Telegram message is missing: "
                    "category_post_id=%s "
                    "channel_id=%s message_id=%s",
                    category_post_id,
                    channel_id,
                    message_id,
                )

                continue

            elif (
                "timed out" in error_text
                or "timeout" in error_text
            ):
                logging.warning(
                    "Category sync timed out; "
                    "will retry later: "
                    "category_post_id=%s",
                    category_post_id,
                )

                continue

            else:
                logging.exception(
                    "Category sync failed: "
                    "category_post_id=%s",
                    category_post_id,
                )

                continue

        replace_category_post_items(
            category_post_id,
            page_rows,
        )

        synced += 1

        # Keep background maintenance below Telegram's burst limits.
        await asyncio.sleep(2.0)

    logging.info(
        "Category sync completed: "
        "synced=%s removed=%s",
        synced,
        removed,
    )

    return {
        "synced": synced,
        "removed": removed,
    }



async def force_delete_property(
    bot,
    property_id,
):
    """
    Immediately remove a published property from the
    destination channel while preserving its DB history.
    """

    destination = get_destination_channel()

    if not destination:
        raise RuntimeError(
            "کانال اصلی فعال پیدا نشد."
        )

    destination_channel_id = destination[0]

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    unit_code,
                    published_message_id,
                    status,
                    deleted_from_channel_at
                FROM properties
                WHERE id = %s
                LIMIT 1
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        raise RuntimeError(
            "فایل در دیتابیس پیدا نشد."
        )

    (
        unit_code,
        published_message_id,
        current_status,
        deleted_from_channel_at,
    ) = row

    # Determine how many Telegram messages belong to the album.
    media_rows = get_property_media(
        property_id
    )

    photo_count = len(
        [
            item
            for item in media_rows
            if item[1] == "PHOTO"
        ]
    )

    message_ids = []

    if published_message_id:
        album_size = (
            min(photo_count, 10)
            if photo_count > 1
            else 1
        )

        message_ids = [
            int(published_message_id) + offset
            for offset in range(album_size)
        ]

    deleted_messages = 0
    already_missing = 0
    warnings = []

    # Remove Telegram messages first.
    # Only after this do we deactivate the DB record.
    for message_id in message_ids:
        try:
            await bot.delete_message(
                chat_id=destination_channel_id,
                message_id=message_id,
            )

            deleted_messages += 1
            await asyncio.sleep(2.0)

        except Exception as exc:
            error_text = str(exc).lower()

            missing_markers = (
                "message to delete not found",
                "message not found",
            )

            if any(
                marker in error_text
                for marker in missing_markers
            ):
                already_missing += 1
                continue

            if (
                "timed out" in error_text
                or "timeout" in error_text
            ):
                # Retry once for a temporary Telegram timeout.
                try:
                    await bot.delete_message(
                        chat_id=destination_channel_id,
                        message_id=message_id,
                    )

                    deleted_messages += 1
                    await asyncio.sleep(2.0)
                    continue

                except Exception as retry_exc:
                    retry_text = str(
                        retry_exc
                    ).lower()

                    if any(
                        marker in retry_text
                        for marker in missing_markers
                    ):
                        already_missing += 1
                        continue

                    warnings.append(
                        f"پیام {message_id}: {retry_exc}"
                    )
                    continue

            warnings.append(
                f"پیام {message_id}: {exc}"
            )

    # If Telegram deletion had a real failure, do not pretend
    # that the force-delete was completely successful.
    if warnings:
        raise RuntimeError(
            "حذف تلگرامی کامل انجام نشد: "
            + " | ".join(warnings)[:1000]
        )

    # Preserve history, but make the property inactive everywhere.
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE properties
                SET
                    status = 'INACTIVE',
                    deleted_from_channel_at = COALESCE(
                        deleted_from_channel_at,
                        NOW()
                    ),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE property_sources
                SET
                    is_active = FALSE,
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'SKIPPED',
                    last_error = 'Force deleted by operator',
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status IN (
                      'NEW',
                      'PARSED',
                      'READY',
                      'SCHEDULED',
                      'PENDING_DUPLICATE',
                      'FAILED'
                  )
                """,
                (property_id,),
            )

            conn.commit()

    category_result = await queue_targeted_category_sync(
        bot,
        [property_id],
        reason="force-delete",
    )

    logging.warning(
        "FORCE DELETE completed: "
        "property_id=%s unit_code=%s "
        "telegram_deleted=%s already_missing=%s "
        "categories_synced=%s categories_removed=%s",
        property_id,
        unit_code,
        deleted_messages,
        already_missing,
        category_result.get("synced", 0),
        category_result.get("removed", 0),
    )

    return {
        "property_id": property_id,
        "unit_code": unit_code,
        "deleted_messages": deleted_messages,
        "already_missing": already_missing,
        "categories_synced": category_result.get(
            "synced",
            0,
        ),
        "categories_removed": category_result.get(
            "removed",
            0,
        ),
    }


async def refresh_published_property_post(
    bot,
    property_id,
):
    row = get_property_for_publish(
        property_id
    )

    if not row:
        return False

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT published_message_id
                FROM properties
                WHERE id = %s
                """,
                (property_id,),
            )

            published_row = cur.fetchone()

    if not published_row:
        return False

    message_id = published_row[0]

    if not message_id:
        return False

    destination = get_destination_channel()

    if not destination:
        return False

    destination_channel_id = destination[0]

    text = build_publish_text(
        row
    )

    media_rows = get_property_media(
        property_id
    )

    try:
        has_photo = any(
            media[1] == "PHOTO"
            for media in media_rows
        )
        await edit_published_message(
            bot,
            chat_id=destination_channel_id,
            message_id=message_id,
            text=text,
            has_photo=has_photo,
        )

    except Exception as exc:
        error_text = str(exc).lower()

        missing_markers = (
            "message to edit not found",
            "message to be edited not found",
        )

        if any(
            marker in error_text
            for marker in missing_markers
        ):
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE properties
                        SET
                            status = 'INACTIVE',
                            deleted_from_channel_at = COALESCE(
                                deleted_from_channel_at,
                                NOW()
                            ),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (property_id,),
                    )

                    cur.execute(
                        """
                        UPDATE publish_queue
                        SET
                            queue_status = 'SKIPPED',
                            last_error =
                                'Destination post deleted from Telegram',
                            updated_at = NOW()
                        WHERE property_id = %s
                          AND queue_status IN (
                              'NEW',
                              'PARSED',
                              'READY',
                              'SCHEDULED',
                              'PENDING_DUPLICATE',
                              'FAILED'
                          )
                        """,
                        (property_id,),
                    )

                    cur.execute(
                        """
                        UPDATE republish_queue
                        SET
                            queue_status = 'CANCELLED',
                            last_error =
                                'Destination post deleted from Telegram',
                            updated_at = NOW()
                        WHERE property_id = %s
                          AND queue_status IN (
                              'READY',
                              'SCHEDULED',
                              'PROCESSING'
                          )
                        """,
                        (property_id,),
                    )

                conn.commit()

            logging.warning(
                "Published property marked deleted from channel: "
                "property_id=%s message_id=%s",
                property_id,
                message_id,
            )

            try:
                result = await queue_targeted_category_sync(
                    bot,
                    [property_id],
                    reason="destination-post-deleted",
                )

                logging.info(
                    "Categories synced after deleted property: "
                    "property_id=%s synced=%s removed=%s",
                    property_id,
                    result.get("synced", 0),
                    result.get("removed", 0),
                )
            except Exception:
                logging.exception(
                    "Category sync after deleted property failed: "
                    "property_id=%s",
                    property_id,
                )

            return False

        if (
            "timed out" in error_text
            or "timeout" in error_text
        ):
            logging.warning(
                "Published property refresh timed out; "
                "will retry later: property_id=%s",
                property_id,
            )
            return False

        logging.exception(
            "Published property refresh failed: property_id=%s",
            property_id,
        )
        return False

    return True



def source_text_is_sold(raw_text):
    text = (
        raw_text
        .replace("\u200c", " ")
        .replace("\u200f", " ")
        .strip()
        .lower()
    )

    sold_markers = (
        "فروخته شد",
        "فروش رفت",
        "معامله شد",
        "sold",
    )

    return any(
        marker in text
        for marker in sold_markers
    )


def mark_property_sold(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE properties
                SET status = 'SOLD',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE property_sources
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (property_id,),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET queue_status = 'SKIPPED',
                    updated_at = NOW(),
                    last_error = 'Source marked property as SOLD'
                WHERE property_id = %s
                  AND queue_status IN (
                      'NEW',
                      'PARSED',
                      'READY',
                      'SCHEDULED',
                      'PENDING_DUPLICATE'
                  )
                """,
                (property_id,),
            )

            conn.commit()


async def sync_edited_source_post(
    post,
    context,
):
    raw_text = (
        post.caption
        or post.text
        or ""
    ).strip()

    if not raw_text:
        return False

    source_unit_code = parse_source_unit_code(
        raw_text
    )

    property_id = find_property_for_source_sync(
        telegram_channel_id=post.chat.id,
        source_message_id=post.message_id,
        source_unit_code=source_unit_code,
    )

    if not property_id:
        logging.warning(
            "Edited SOURCE post could not be matched: channel=%s message=%s source_code=%s",
            post.chat.id,
            post.message_id,
            source_unit_code,
        )
        return False

    if source_text_is_sold(raw_text):
        mark_property_sold(
            property_id
        )

        await refresh_published_property_post(
            context.bot,
            property_id,
        )

        category_result = await queue_targeted_category_sync(
            context.bot,
            [property_id],
            reason="source-sold",
        )

        logging.info(
            "SOURCE sold sync completed: "
            "property_id=%s source_code=%s "
            "categories_synced=%s categories_removed=%s",
            property_id,
            source_unit_code,
            category_result["synced"],
            category_result["removed"],
        )

        return True

    parsed = parse_property(
        raw_text
    )

    new_price = parsed.get(
        "current_price_toman"
    )

    if new_price is None:
        logging.warning(
            "Edited SOURCE post has no parseable price: property_id=%s",
            property_id,
        )
        return False

    # === SOURCE TARGETED CATEGORY SNAPSHOT V1 ===
    old_effective_price = (
        _get_property_current_price_toman(
            property_id
        )
    )

    old_region_names = (
        _get_property_region_category_names(
            property_id
        )
    )

    # Re-parse property with the updated source text.
    update_parsed_property(
        property_id,
        parsed,
    )

    save_property_address(
        property_id,
        parsed.get("address_text"),
    )

    save_property_source(
        property_id=property_id,
        telegram_channel_id=post.chat.id,
        source_message_id=post.message_id,
        source_unit_code=source_unit_code,
        source_raw_text=raw_text,
        source_price_toman=new_price,
    )

    dynamic_attributes = parse_dynamic_attributes(
        raw_text
    )

    rule_attributes = parse_rule_attributes(
        raw_text,
        get_active_attribute_rules(),
    )

    merged_attributes = {}

    for item in (
        dynamic_attributes
        + rule_attributes
    ):
        merged_attributes[
            item["attribute_key"]
        ] = item

    save_property_attributes(
        property_id,
        list(merged_attributes.values()),
    )

    await refresh_published_property_post(
        context.bot,
        property_id,
    )

    category_result = (
        await sync_categories_for_price_change(
            context.bot,
            property_id,
            old_effective_price,
            _get_property_current_price_toman(
                property_id
            ),
            extra_region_names=old_region_names,
        )
    )

    logging.info(
        "SOURCE price sync completed: property_id=%s source_code=%s price=%s categories_synced=%s categories_removed=%s",
        property_id,
        source_unit_code,
        new_price,
        category_result["synced"],
        category_result["removed"],
    )

    return True



def build_source_post_url(
    telegram_channel_id,
    username,
    message_id,
):
    if not message_id:
        return None

    if username:
        username = str(username).lstrip("@")
        return f"https://t.me/{username}/{message_id}"

    channel_id_text = str(telegram_channel_id)

    if channel_id_text.startswith("-100"):
        private_id = channel_id_text[4:]

        return f"https://t.me/c/{private_id}/{message_id}"

    return None



# === DESTINATION MANUAL PRICE EDIT V1 ===
async def sync_edited_destination_post(
    post,
    context,
):
    """
    A human edit of the published DESTINATION post becomes
    the operator/manual price floor.

    Effective price remains:
        MAX(manual_price_toman, active source prices)

    Bot-generated edits are ignored when the published price
    already equals properties.current_price_toman.
    """

    raw_text = (
        post.caption
        or post.text
        or ""
    ).strip()

    if not raw_text:
        logging.warning(
            "DESTINATION edit ignored: empty text "
            "message_id=%s",
            post.message_id,
        )
        return False

    parsed = parse_property(raw_text)

    edited_price = parsed.get(
        "current_price_toman"
    )

    if edited_price is None:
        logging.warning(
            "DESTINATION edit has no parseable price: "
            "channel_id=%s message_id=%s",
            post.chat.id,
            post.message_id,
        )
        return False

    edited_price = int(edited_price)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    current_price_toman,
                    manual_price_toman,
                    status,
                    deleted_from_channel_at
                FROM properties
                WHERE published_message_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (post.message_id,),
            )

            row = cur.fetchone()

    if not row:
        logging.info(
            "DESTINATION edit is not a property post: "
            "message_id=%s",
            post.message_id,
        )
        return False

    (
        property_id,
        current_price,
        previous_manual_price,
        status,
        deleted_from_channel_at,
    ) = row

    if (
        status != "ACTIVE"
        or deleted_from_channel_at is not None
    ):
        logging.info(
            "DESTINATION edit ignored for inactive property: "
            "property_id=%s message_id=%s",
            property_id,
            post.message_id,
        )
        return False

    # If Telegram reports an edit made by the bot itself,
    # the rendered price is already the DB effective price.
    # Do not turn that into a new manual override.
    if (
        current_price is not None
        and int(current_price) == edited_price
    ):
        logging.info(
            "DESTINATION price edit echo ignored: "
            "property_id=%s price=%s",
            property_id,
            edited_price,
        )
        return True

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    manual_price_toman = %s,
                    manual_price_updated_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    edited_price,
                    property_id,
                ),
            )

        conn.commit()

    from ingest import reconcile_property_price

    effective_price = reconcile_property_price(
        property_id
    )

    # Normalize/rebuild the original property post.
    await refresh_published_property_post(
        context.bot,
        property_id,
    )

    # Price categories must immediately follow
    # the new effective price, without global sync.
    category_result = (
        await sync_categories_for_price_change(
            context.bot,
            property_id,
            current_price,
            effective_price,
        )
    )

    logging.info(
        "DESTINATION manual price accepted: "
        "property_id=%s "
        "previous_manual=%s "
        "requested=%s "
        "effective=%s "
        "categories_synced=%s "
        "categories_removed=%s",
        property_id,
        previous_manual_price,
        edited_price,
        effective_price,
        category_result.get("synced", 0),
        category_result.get("removed", 0),
    )

    return True


async def input_channel_post_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    post = (
        update.channel_post
        or update.edited_channel_post
    )

    if not post:
        return

    is_edited_channel_post = (
        update.edited_channel_post
        is not None
    )

    # === DESTINATION MANUAL PRICE ROUTE V1 ===
    destination = get_destination_channel()

    if (
        is_edited_channel_post
        and destination
        and int(post.chat.id) == int(destination[0])
    ):
        await sync_edited_destination_post(
            post,
            context,
        )
        return

    input_channel_id = get_input_channel_id()
    source_channel_ids = get_active_source_channel_ids()

    allowed_channel_ids = set(
        source_channel_ids
    )

    if input_channel_id:
        allowed_channel_ids.add(
            int(input_channel_id)
        )

    if post.chat.id not in allowed_channel_ids:
        return

    ingest_channel_type = (
        "INPUT"
        if (
            input_channel_id
            and post.chat.id == int(input_channel_id)
        )
        else "SOURCE"
    )

    logging.info(
        "Accepted channel post: type=%s channel_id=%s message_id=%s edited=%s",
        ingest_channel_type,
        post.chat.id,
        post.message_id,
        is_edited_channel_post,
    )

    if (
        ingest_channel_type == "SOURCE"
        and is_edited_channel_post
    ):
        await sync_edited_source_post(
            post,
            context,
        )
        return

    if (
        ingest_channel_type == "INPUT"
        and is_edited_channel_post
    ):
        raw_text = (
            post.caption
            or post.text
            or ""
        ).strip()

        input_property_id = None

        if post.media_group_id:
            input_property_id = (
                get_property_by_media_group(
                    post.chat.id,
                    post.media_group_id,
                )
            )

        if not input_property_id:
            input_property_id = raw_property_exists(
                post.chat.id,
                post.message_id,
            )

        if (
            input_property_id
            and source_text_is_sold(raw_text)
        ):
            mark_property_sold(
                input_property_id
            )

            await refresh_published_property_post(
                context.bot,
                input_property_id,
            )

            category_result = await queue_targeted_category_sync(
                context.bot,
                [input_property_id],
                reason="input-sold",
            )

            logging.info(
                "INPUT sold sync completed: "
                "property_id=%s message_id=%s "
                "categories_synced=%s "
                "categories_removed=%s",
                input_property_id,
                post.message_id,
                category_result["synced"],
                category_result["removed"],
            )

            return

    message_id = post.message_id
    raw_text = (post.caption or post.text or "").strip()
    media_group_id = post.media_group_id

    effective_source_channel_id = post.chat.id
    effective_source_message_id = message_id

    effective_source_url = build_source_post_url(
        post.chat.id,
        getattr(post.chat, "username", None),
        message_id,
    )

    if ingest_channel_type == "INPUT":
        origin = getattr(
            post,
            "forward_origin",
            None,
        )

        origin_chat = getattr(
            origin,
            "chat",
            None,
        )

        origin_message_id = getattr(
            origin,
            "message_id",
            None,
        )

        if (
            origin_chat
            and origin_chat.type == "channel"
            and origin_message_id
        ):
            effective_source_channel_id = (
                origin_chat.id
            )

            effective_source_message_id = (
                origin_message_id
            )

            effective_source_url = (
                build_source_post_url(
                    origin_chat.id,
                    origin_chat.username,
                    origin_message_id,
                )
            )

            save_channel(
                telegram_channel_id=origin_chat.id,
                title=origin_chat.title,
                username=origin_chat.username,
                channel_type="SOURCE",
                added_by_user_id=None,
            )

            logging.info(
                "Real forwarded source detected: "
                "channel_id=%s title=%s "
                "message_id=%s url=%s",
                origin_chat.id,
                origin_chat.title,
                origin_message_id,
                effective_source_url,
            )

    has_media = bool(
        post.photo
        or post.video
        or post.document
    )

    property_id = None
    existing_album_property = False

    if media_group_id:
        property_id = get_property_by_media_group(
            post.chat.id,
            media_group_id,
        )

        if property_id:
            existing_album_property = True

    if not property_id:
        property_id = raw_property_exists(
            post.chat.id,
            message_id,
        )

    if not property_id:
        property_id = create_raw_property(
            channel_id=post.chat.id,
            message_id=message_id,
            media_group_id=media_group_id,
            raw_text=raw_text,
            has_media=has_media,
            source_kind=(
                "SOURCE"
                if ingest_channel_type == "SOURCE"
                else "INPUT"
            ),
        )

    elif raw_text:
        # Never overwrite an album caption with an empty media update.
        update_raw_text(
            property_id,
            raw_text,
        )

    if post.photo:
        photo = post.photo[-1]

        add_media(
            property_id=property_id,
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            media_type="PHOTO",
            media_group_id=media_group_id,
            position=message_id,
        )

    elif post.video:
        add_media(
            property_id=property_id,
            file_id=post.video.file_id,
            file_unique_id=post.video.file_unique_id,
            media_type="VIDEO",
            media_group_id=media_group_id,
            position=message_id,
        )

    elif post.document:
        add_media(
            property_id=property_id,
            file_id=post.document.file_id,
            file_unique_id=post.document.file_unique_id,
            media_type="DOCUMENT",
            media_group_id=media_group_id,
            position=message_id,
        )

    # Album items after/before the caption often contain no text.
    # Store their media only. Do not reclassify or erase parsed data.
    if media_group_id and not raw_text:
        logging.info(
            "Album media attached without caption: property_id=%s media_group_id=%s message_id=%s",
            property_id,
            media_group_id,
            message_id,
        )
        return

    classification = classify_property(
        raw_text,
        has_media,
    )

    result = classification["classification"]

    set_ingest_classification(
        property_id,
        result,
    )

    if result == "PROPERTY":
        parsed = parse_property(raw_text)

        update_parsed_property(
            property_id,
            parsed,
        )

        save_property_address(
            property_id,
            parsed.get("address_text"),
        )

        source_unit_code = parse_source_unit_code(
            raw_text
        )

        save_property_source(
            property_id=property_id,
            telegram_channel_id=effective_source_channel_id,
            source_message_id=effective_source_message_id,
            source_unit_code=source_unit_code,
            source_raw_text=raw_text,
            source_price_toman=parsed.get(
                "current_price_toman"
            ),
            source_post_url=effective_source_url,
        )

        dynamic_attributes = parse_dynamic_attributes(
            raw_text
        )

        rule_attributes = parse_rule_attributes(
            raw_text,
            get_active_attribute_rules(),
        )

        merged_attributes = {}

        for item in (
            dynamic_attributes
            + rule_attributes
        ):
            merged_attributes[
                item["attribute_key"]
            ] = item

        save_property_attributes(
            property_id,
            list(merged_attributes.values()),
        )

        add_to_queue(property_id)

    logging.info(
        "Channel classified: property_id=%s result=%s score=%s reason=%s",
        property_id,
        result,
        classification.get("score"),
        classification.get("reason"),
    )


async def forwarded_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user is None or update.message is None:
        return

    telegram_user_id = update.effective_user.id
    session = get_session(telegram_user_id)

    if not session:
        return

    if context.user_data.get("state") != "waiting_channel_forward":
        return

    pending_type = context.user_data.get("pending_channel_type")
    msg = update.message

    origin = msg.forward_origin

    if not origin:
        await msg.reply_text(
            "❌ لطفاً یک پست از کانال موردنظر Forward کنید.",
            reply_markup=back_menu(),
        )
        return

    origin_chat = getattr(origin, "chat", None)

    if not origin_chat or origin_chat.type != "channel":
        await msg.reply_text(
            "❌ اطلاعات کانال از این پیام قابل تشخیص نیست.\n"
            "یک پست معمولی از خود کانال Forward کنید.",
            reply_markup=back_menu(),
        )
        return

    channel_id = origin_chat.id
    title = origin_chat.title
    username = origin_chat.username

    save_channel(
        telegram_channel_id=channel_id,
        title=title,
        username=username,
        channel_type=pending_type,
        added_by_user_id=session[0],
    )

    context.user_data.pop("state", None)
    context.user_data.pop("pending_channel_type", None)

    type_names = {
        "INPUT": "📥 کانال ورودی",
        "DESTINATION": "📢 کانال اصلی",
        "LOG": "🧾 کانال لاگ",
        "SOURCE": "📡 کانال منبع",
    }

    uname = f"@{username}" if username else "ندارد"

    await msg.reply_text(
        "✅ کانال با موفقیت ثبت شد.\n\n"
        f"نوع: {type_names.get(pending_type)}\n"
        f"نام: {title}\n"
        f"Username: {uname}\n"
        f"Channel ID: {channel_id}",
        reply_markup=settings_back_menu(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id
    session = get_session(telegram_user_id)

    if await handle_showcase_callback(update, context, session):
        return

    # ShahFile Smart Customer/Owner callbacks work for public customers
    # and authenticated OWNER actions.
    if await handle_smart_customer_callback(update, context, session):
        return

    if not session:
        context.user_data.clear()
        context.user_data["login_step"] = "username"

        await query.edit_message_text(
            "🔐 نشست شما فعال نیست.\n\n"
            "برای ورود /login را بزنید."
        )
        return

    user_id, username, display_name, role = session
    action = query.data

    if action == "customer_campaigns":
        await query.edit_message_text(
            campaign_summary_text(session[0]),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔍 جستجو و ساخت کمپین جدید",
                    callback_data="smart_search",
                )],
                [InlineKeyboardButton(
                    "⬅️ منوی اصلی",
                    callback_data="home",
                )],
            ]),
        )
        return

    if action == "customer_showcase_create":
        raw_query = context.user_data.get("smart_search_query")
        if not raw_query:
            await query.edit_message_text(
                "⚠️ جستجوی قبلی منقضی شده است. دوباره جستجو کنید.",
                reply_markup=back_menu(),
            )
            return
        full_result = smart_search_properties(raw_query, offset=0, limit=10000)
        showcase = create_showcase(
            session[0],
            smart_search_display_query(raw_query),
            manager_filters_to_public(full_result["filters"]),
            [row[0] for row in full_result["rows"]],
        )
        if not showcase:
            await query.edit_message_text(
                "⚠️ فیلتر معتبری برای ساخت کمپین پیدا نشد. دوباره جستجو کنید.",
                reply_markup=back_menu(),
            )
            return
        link = await build_showcase_link(context.bot, showcase["token"])
        await query.message.reply_text(
            "✅ لینک جذب مشتری ساخته شد\n\n"
            f"🔎 درخواست: {smart_search_display_query(raw_query)}\n"
            f"📂 فایل‌های فعال فعلی: {showcase['count']}\n\n"
            f"{link}\n\n"
            "مشتری فقط بار اول شماره‌اش را ثبت می‌کند و بعد فهرست زنده و "
            "صفحه‌بندی‌شده را می‌بیند. فایل حذف‌شده خودکار کنار می‌رود و "
            "هر فایل جدیدِ مطابق همین فیلتر با صف هوشمند فوراً ارسال می‌شود.",
            disable_web_page_preview=True,
        )
        return

    # CRM_ACCESS_GUARD_V1
    if not check_action_access(session, action or ""):
        await query.edit_message_text(
            "⛔ شما به این بخش دسترسی ندارید.",
            reply_markup=build_main_menu(
                session,
                main_menu("ADMIN" if role == "OWNER" else role),
            ),
        )
        return

    # CHANNEL_FILE_EDITOR_BUTTON_ROUTER_V1
    if await handle_file_editor_button(
        update,
        context,
        session,
        allowed=check_action_access(session, "channel_edit"),
        sync_callback=sync_categories_for_price_change,
    ):
        return

    # FOOTER_BULK_REFRESH_BUTTON_ROUTER_V1_2
    if await handle_footer_bulk_button(
        update,
        context,
        session,
        allowed=check_action_access(session, "settings"),
        back_markup=footer_settings_menu(),
    ):
        return

    if await handle_final_button(update, context, session):
        return

    if await handle_crm_button(
        update,
        context,
        session,
    ):
        return

    if action == "home":
        context.user_data.clear()
        await show_main_menu(update, session)
        return

    if action == "add_channel":
        context.user_data.clear()

        await query.edit_message_text(
            "➕ افزودن کانال\n\n"
            "نوع کانال موردنظر را انتخاب کنید:",
            reply_markup=channel_type_keyboard(),
        )
        return

    if action.startswith("add_type_"):
        channel_type = action.replace("add_type_", "", 1)

        context.user_data["state"] = "waiting_channel_forward"
        context.user_data["pending_channel_type"] = channel_type

        type_names = {
            "INPUT": "📥 کانال ورودی",
            "DESTINATION": "📢 کانال اصلی",
            "LOG": "🧾 کانال لاگ",
            "SOURCE": "📡 کانال منبع",
        }

        await query.edit_message_text(
            f"{type_names.get(channel_type)}\n\n"
            "حالا یک پست از همان کانال را برای این ربات Forward کنید.",
            reply_markup=settings_back_menu(),
        )
        return

    if action == "sources":
        rows = get_channels()

        if not rows:
            await query.edit_message_text(
                "📡 کانال‌ها\n\n"
                "هنوز هیچ کانالی ثبت نشده است.",
                reply_markup=settings_back_menu(),
            )
            return

        type_names = {
            "INPUT": "📥 ورودی",
            "DESTINATION": "📢 اصلی",
            "LOG": "🧾 لاگ",
            "SOURCE": "📡 منبع",
        }

        lines = ["📡 کانال‌های ثبت‌شده:\n"]

        for row in rows:
            db_id, tg_id, title, username, channel_type, is_active = row

            status = "✅" if is_active else "⛔"
            uname = f"@{username}" if username else "بدون یوزرنیم"

            lines.append(
                f"{status} {type_names.get(channel_type)}\n"
                f"{title}\n"
                f"{uname}\n"
                f"ID: {tg_id}\n"
            )

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=settings_back_menu(),
        )
        return

    if action.startswith("urgent_select_"):
        property_id = int(
            action.replace(
                "urgent_select_",
                "",
                1,
            )
        )

        row = get_property_for_publish(
            property_id
        )

        if not row:
            await query.edit_message_text(
                "❌ فایل پیدا نشد.",
                reply_markup=back_menu(),
            )
            return

        duplicate = detect_and_store_duplicate(
            property_id
        )

        if (
            duplicate
            and duplicate.get("review_status")
            != "APPROVED"
        ):
            await query.edit_message_text(
                build_duplicate_warning_text(
                    property_id,
                    duplicate,
                ),
                reply_markup=urgent_duplicate_keyboard(
                    property_id
                ),
            )
            return

        preview = build_publish_text(
            row
        )

        await query.edit_message_text(
            "🚨 پیش‌نمایش انتشار فوری\n\n"
            + preview,
            reply_markup=urgent_confirm_keyboard(
                property_id
            ),
        )
        return

    if action.startswith("urgent_dup_approve_"):
        property_id = int(
            action.replace(
                "urgent_dup_approve_",
                "",
                1,
            )
        )

        set_duplicate_review_status(
            property_id,
            "APPROVED",
        )

        await query.edit_message_text(
            "⏳ تأیید شد؛ در حال انتشار فایل..."
        )

        try:
            result = await publish_property(
                context.bot,
                property_id,
            )

            if result.get("already_published"):
                await query.edit_message_text(
                    "⚠️ این فایل قبلاً منتشر شده است.",
                    reply_markup=back_menu(),
                )
                return

            await query.edit_message_text(
                "✅ فایل با تأیید شما منتشر شد.\n\n"
                f"کد واحد: {result.get('unit_code')}\n"
                f"لینک پست:\n"
                f"{result.get('channel_post_url') or 'ثبت نشد'}",
                reply_markup=back_menu(),
            )

        except Exception as exc:
            logging.exception(
                "Urgent approved duplicate publish failed "
                "for property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "❌ انتشار فایل ناموفق بود.\n\n"
                f"خطا: {str(exc)[:500]}",
                reply_markup=back_menu(),
            )

        return

    if action.startswith("urgent_dup_reject_"):
        property_id = int(
            action.replace(
                "urgent_dup_reject_",
                "",
                1,
            )
        )

        set_duplicate_review_status(
            property_id,
            "REJECTED",
        )

        await query.edit_message_text(
            "❌ انتشار این فایل لغو شد.\n\n"
            "فایل حذف نشده و در دیتابیس باقی می‌ماند.",
            reply_markup=back_menu(),
        )
        return

    if action.startswith("urgent_confirm_"):
        property_id = int(
            action.replace(
                "urgent_confirm_",
                "",
                1,
            )
        )

        await query.edit_message_text(
            "⏳ در حال انتشار فوری فایل..."
        )

        try:
            result = await publish_property(
                context.bot,
                property_id,
            )

            if result.get("already_published"):
                await query.edit_message_text(
                    "⚠️ این فایل قبلاً منتشر شده است.",
                    reply_markup=back_menu(),
                )
                return

            await query.edit_message_text(
                "✅ فایل با موفقیت منتشر شد.\n\n"
                f"کد واحد: {result.get('unit_code')}\n"
                f"لینک پست:\n{result.get('channel_post_url') or 'ثبت نشد'}",
                reply_markup=back_menu(),
            )

        except Exception as exc:
            logging.exception(
                "Urgent publish failed for property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "❌ انتشار فوری ناموفق بود.\n\n"
                f"خطا: {str(exc)[:500]}",
                reply_markup=back_menu(),
            )

        return

    if action.startswith(
        "republish_confirm_"
    ):
        property_id = int(
            action.replace(
                "republish_confirm_",
                "",
                1,
            )
        )

        await query.edit_message_text(
            "⏳ در حال بازنشر فایل...\n\n"
            "کد واحد تغییر نمی‌کند."
        )

        try:
            result = await republish_property(
                context.bot,
                property_id,
            )

            text = (
                "✅ بازنشر با موفقیت انجام شد.\n\n"
                f"کد واحد: {result.get('unit_code')}\n"
                f"تعداد عکس‌ها: {result.get('photo_count')}\n"
                f"پیام جدید: {result.get('new_message_id')}\n"
                f"لینک پست:\n"
                f"{result.get('channel_post_url') or 'ثبت نشد'}"
            )

            if result.get("old_deleted"):
                text += (
                    "\n\n🗑 پست قبلی نیز حذف شد."
                )

            elif result.get("old_message_id"):
                text += (
                    "\n\n⚠️ نسخه جدید منتشر شد، "
                    "اما حذف پست قبلی ممکن نشد."
                )

            await query.edit_message_text(
                text,
                reply_markup=back_menu(),
            )

        except Exception as exc:
            logging.exception(
                "Republish failed property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "❌ بازنشر ناموفق بود.\n\n"
                f"خطا: {str(exc)[:500]}",
                reply_markup=back_menu(),
            )

        return

    if action.startswith("trash_page_"):
        try:
            offset = int(
                action.replace(
                    "trash_page_",
                    "",
                    1,
                )
            )
        except ValueError:
            offset = 0

        offset = max(
            0,
            offset,
        )

        total = get_trash_count()

        if total > 0:
            last_offset = (
                (total - 1) // 10
            ) * 10

            offset = min(
                offset,
                last_offset,
            )
        else:
            offset = 0

        rows = get_trash_rows(
            limit=10,
            offset=offset,
        )

        if not rows:
            await query.edit_message_text(
                "🗑 سطل آشغال\n\n"
                "✅ در حال حاضر فایل آرشیوشده‌ای وجود ندارد.",
                reply_markup=trash_list_keyboard(
                    offset=0,
                    page_size=10,
                ),
            )
            return

        current_page = (
            offset // 10
        ) + 1

        total_pages = (
            total + 9
        ) // 10

        await query.edit_message_text(
            "🗑 سطل آشغال\n\n"
            f"تعداد فایل‌های آرشیوشده: {total}\n"
            f"صفحه {current_page} از {total_pages}\n\n"
            "برای مشاهده و بازیابی هر فایل "
            "روی آن بزنید:",
            reply_markup=trash_list_keyboard(
                offset=offset,
                page_size=10,
            ),
        )
        return


    if action.startswith("trash_view_"):
        property_id = int(
            action.replace(
                "trash_view_",
                "",
                1,
            )
        )

        row = get_trash_property_detail(
            property_id
        )

        if not row:
            await query.edit_message_text(
                "⚠️ این فایل دیگر در سطل آشغال وجود ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ سطل آشغال",
                                callback_data="trash_page_0",
                            )
                        ]
                    ]
                ),
            )
            return

        await query.edit_message_text(
            build_trash_detail(row),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "♻️ بازیابی فایل",
                            callback_data=(
                                f"trash_restore_confirm_{property_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ بازگشت به سطل آشغال",
                            callback_data="trash_page_0",
                        )
                    ],
                ]
            ),
        )
        return


    if action.startswith(
        "trash_restore_confirm_"
    ):
        property_id = int(
            action.replace(
                "trash_restore_confirm_",
                "",
                1,
            )
        )

        row = get_trash_property_detail(
            property_id
        )

        if not row:
            await query.edit_message_text(
                "⚠️ فایل موردنظر دیگر در سطل آشغال نیست.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ سطل آشغال",
                                callback_data="trash_page_0",
                            )
                        ]
                    ]
                ),
            )
            return

        unit_code = row[1]

        await query.edit_message_text(
            "⚠️ تأیید بازیابی\n\n"
            f"کد واحد: {unit_code or '-'}\n\n"
            "این فایل از سطل آشغال خارج می‌شود "
            "و دوباره به حالت آماده انتشار برمی‌گردد.\n\n"
            "فایل همین لحظه در کانال منتشر نمی‌شود.\n\n"
            "ادامه می‌دهید؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ بله، بازیابی شود",
                            callback_data=(
                                f"trash_restore_do_{property_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ انصراف",
                            callback_data=(
                                f"trash_view_{property_id}"
                            ),
                        )
                    ],
                ]
            ),
        )
        return


    if action.startswith(
        "trash_restore_do_"
    ):
        property_id = int(
            action.replace(
                "trash_restore_do_",
                "",
                1,
            )
        )

        restored = restore_property_from_trash(
            property_id
        )

        if restored:
            try:
                await queue_targeted_category_sync(
                    context.bot,
                    [property_id],
                    reason="trash-restore",
                )
            except Exception:
                logging.exception(
                    "Category sync after trash restore failed: "
                    "property_id=%s",
                    property_id,
                )

        if not restored:
            await query.edit_message_text(
                "⚠️ فایل قابل بازیابی نبود.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ سطل آشغال",
                                callback_data="trash_page_0",
                            )
                        ]
                    ]
                ),
            )
            return

        await query.edit_message_text(
            "✅ فایل با موفقیت بازیابی شد.\n\n"
            "وضعیت فایل: 🟢 آماده انتشار\n"
            "فایل به صف عادی برگشت و "
            "در زمان مجاز توسط Scheduler منتشر می‌شود.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📥 صف انتشار",
                            callback_data="queue",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🗑 سطل آشغال",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ فایل‌ها",
                            callback_data="files_menu",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "republish_group":
        context.user_data.pop("state", None)
        context.user_data.pop(
            "pending_group_republish_count",
            None,
        )

        summary = get_republish_queue_summary()

        active_count = (
            summary.get("READY", 0)
            + summary.get("SCHEDULED", 0)
            + summary.get("PROCESSING", 0)
        )

        validity_weeks = get_file_validity_weeks()

        await query.edit_message_text(
            "🔁 بازنشر گروهی فایل‌ها\n\n"
            f"اعتبار فعلی فایل‌ها: {validity_weeks} هفته\n"
            f"در انتظار بازنشر: {active_count} فایل\n\n"
            "از قدیمی‌ترین فایل‌های معتبر انتخاب می‌شود.\n"
            "تعداد موردنظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "5 فایل",
                            callback_data="republish_group_pick_5",
                        ),
                        InlineKeyboardButton(
                            "10 فایل",
                            callback_data="republish_group_pick_10",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "20 فایل",
                            callback_data="republish_group_pick_20",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "✏️ تعداد دلخواه",
                            callback_data="republish_group_custom",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ بازگشت به فایل‌ها",
                            callback_data="files_menu",
                        ),
                    ],
                ]
            ),
        )
        return


    if action.startswith("republish_group_pick_"):
        try:
            count = int(
                action.replace(
                    "republish_group_pick_",
                    "",
                    1,
                )
            )
        except ValueError:
            await query.edit_message_text(
                "❌ تعداد انتخاب‌شده نامعتبر است.",
                reply_markup=files_menu(),
            )
            return

        if count < 1 or count > 100:
            await query.edit_message_text(
                "❌ تعداد باید بین 1 تا 100 باشد.",
                reply_markup=files_menu(),
            )
            return

        context.user_data[
            "pending_group_republish_count"
        ] = count

        await query.edit_message_text(
            "🔁 تأیید بازنشر گروهی\n\n"
            f"تعداد درخواستی: {count} فایل\n\n"
            "سیستم از قدیمی‌ترین فایل‌هایی انتخاب می‌کند که "
            "هنوز معتبر و فعال هستند.\n\n"
            "کد واحد تغییر نمی‌کند و این فایل‌ها "
            "به‌عنوان ورودی جدید ثبت نمی‌شوند.\n\n"
            "تأیید می‌کنید؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ تأیید و افزودن به صف",
                            callback_data=(
                                f"republish_group_confirm_{count}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ انصراف",
                            callback_data="republish_group",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "republish_group_custom":
        context.user_data.clear()
        context.user_data[
            "state"
        ] = "republish_group_custom_count"

        await query.edit_message_text(
            "✏️ تعداد بازنشر گروهی\n\n"
            "تعداد فایل موردنظر را ارسال کنید.\n"
            "حداقل: 1\n"
            "حداکثر: 100\n\n"
            "مثال:\n15",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ بازگشت",
                            callback_data="republish_group",
                        )
                    ]
                ]
            ),
        )
        return


    if action.startswith(
        "republish_group_confirm_"
    ):
        try:
            count = int(
                action.replace(
                    "republish_group_confirm_",
                    "",
                    1,
                )
            )
        except ValueError:
            await query.edit_message_text(
                "❌ تعداد بازنشر نامعتبر است.",
                reply_markup=files_menu(),
            )
            return

        await query.edit_message_text(
            "⏳ در حال انتخاب فایل‌های معتبر..."
        )

        try:
            result = enqueue_republish_candidates(
                count
            )
        except Exception as exc:
            logging.exception(
                "Group republish enqueue failed"
            )

            await query.edit_message_text(
                "❌ افزودن فایل‌ها به صف بازنشر ناموفق بود.\n\n"
                f"خطا: {str(exc)[:500]}",
                reply_markup=files_menu(),
            )
            return

        added = result.get("added", 0)
        requested = result.get("requested", count)
        validity_weeks = result.get(
            "validity_weeks",
            get_file_validity_weeks(),
        )

        context.user_data.pop(
            "pending_group_republish_count",
            None,
        )

        text = (
            "✅ فایل‌ها به صف بازنشر اضافه شدند.\n\n"
            f"درخواست: {requested} فایل\n"
            f"اضافه‌شده: {added} فایل\n"
            f"اعتبار فعلی: {validity_weeks} هفته\n\n"
        )

        if added < requested:
            text += (
                "ℹ️ تعداد فایل واجد شرایط کمتر از "
                "تعداد درخواستی بوده است.\n\n"
            )

        text += (
            "هیچ کد واحد جدیدی ساخته نشده و "
            "این فایل‌ها به‌عنوان ورودی جدید ثبت نشده‌اند."
        )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔁 بازنشر گروهی",
                            callback_data="republish_group",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ فایل‌ها",
                            callback_data="files_menu",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "files_menu":
        context.user_data.pop("state", None)

        await query.edit_message_text(
            "📁 فایل‌ها\n\n"
            "بخش موردنظر را انتخاب کنید:",
            reply_markup=files_menu(),
        )
        return

    if action == "force_delete":
        context.user_data.clear()

        context.user_data[
            "state"
        ] = "force_delete_unit_code"

        await query.edit_message_text(
            "🗑 حذف فورس فایل\n\n"
            "کد واحد داخلی را ارسال کنید.\n\n"
            "⚠️ فایل بلافاصله از کانال اصلی حذف می‌شود، "
            "اما سابقه آن در دیتابیس باقی می‌ماند.\n\n"
            "مثال:\n11728",
            reply_markup=files_menu(),
        )
        return

    if action == "republish":
        context.user_data["state"] = (
            "republish_unit_code"
        )

        await query.edit_message_text(
            "🔄 بازنشر فایل\n\n"
            "کد واحد داخلی را ارسال کنید.\n\n"
            "مثال:\n1055",
            reply_markup=back_menu(),
        )
        return

    if action == "urgent_publish":
        context.user_data["state"] = "urgent_source_code"

        await query.edit_message_text(
            "🚨 انتشار فوری\n\n"
            "شماره پیگیری صف، کد فایل منبع یا لینک پست منبع را ارسال کنید.\n\n"
            "مثال شماره صف:\nصف 45\n\n"
            "مثال کد منبع:\n3949\n\n"
            "مثال لینک:\nhttps://t.me/sourcechannel/1234",
            reply_markup=back_menu(),
        )
        return

    if action == "duplicate_queue":
        rows = get_pending_duplicate_properties()

        if not rows:
            await query.edit_message_text(
                "🧩 فایل‌های تکراری\n\n"
                "✅ در حال حاضر هیچ فایل تکراری "
                "منتظر تأییدی وجود ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 بروزرسانی",
                                callback_data="duplicate_queue",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت",
                                callback_data="home",
                            )
                        ],
                    ]
                ),
            )
            return

        await query.edit_message_text(
            "🧩 فایل‌های تکراری منتظر تأیید\n\n"
            f"تعداد: {len(rows)}\n\n"
            "برای بررسی هر فایل روی آن بزنید:",
            reply_markup=duplicate_queue_list_keyboard(),
        )
        return


    if action.startswith("dupqueue_view_"):
        property_id = int(
            action.replace(
                "dupqueue_view_",
                "",
                1,
            )
        )

        row = get_pending_duplicate_detail(
            property_id
        )

        if not row:
            await query.edit_message_text(
                "⚠️ این فایل دیگر در انتظار بررسی نیست.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت به لیست",
                                callback_data="duplicate_queue",
                            )
                        ]
                    ]
                ),
            )
            return

        await query.edit_message_text(
            build_duplicate_queue_detail(
                row
            ),
            reply_markup=duplicate_queue_decision_keyboard(
                property_id
            ),
        )
        return


    if action.startswith("dupqueue_approve_"):
        property_id = int(
            action.replace(
                "dupqueue_approve_",
                "",
                1,
            )
        )

        approve_duplicate_for_scheduler(
            property_id
        )

        await query.edit_message_text(
            "✅ فایل تأیید شد.\n\n"
            "این فایل دوباره به صف انتشار برگشت "
            "و Scheduler در اولین زمان مجاز "
            "آن را منتشر می‌کند.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🧩 فایل‌های تکراری",
                            callback_data="duplicate_queue",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ منوی اصلی",
                            callback_data="home",
                        )
                    ],
                ]
            ),
        )
        return


    if action.startswith("dupqueue_reject_"):
        property_id = int(
            action.replace(
                "dupqueue_reject_",
                "",
                1,
            )
        )

        reject_duplicate_for_scheduler(
            property_id
        )

        await query.edit_message_text(
            "❌ فایل رد شد.\n\n"
            "فایل از دیتابیس حذف نشده، "
            "اما دیگر توسط Scheduler منتشر نمی‌شود.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🧩 فایل‌های تکراری",
                            callback_data="duplicate_queue",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ منوی اصلی",
                            callback_data="home",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "queue_failed":
        rows = get_failed_queue_properties()

        if not rows:
            await query.edit_message_text(
                "❌ فایل‌های خطادار\n\n"
                "✅ در حال حاضر هیچ فایل خطاداری "
                "در صف وجود ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 بروزرسانی",
                                callback_data="queue_failed",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت به فایل‌ها",
                                callback_data="files_menu",
                            )
                        ],
                    ]
                ),
            )
            return

        await query.edit_message_text(
            "❌ فایل‌های خطادار\n\n"
            f"تعداد فایل‌های خطادار: {len(rows)}\n\n"
            "برای بررسی هر فایل روی آن بزنید:",
            reply_markup=failed_queue_list_keyboard(),
        )
        return


    if action.startswith("queue_failed_view_"):
        property_id = int(
            action.replace(
                "queue_failed_view_",
                "",
                1,
            )
        )

        row = get_failed_queue_detail(
            property_id
        )

        if not row:
            await query.edit_message_text(
                "⚠️ این فایل دیگر در وضعیت خطادار نیست.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت",
                                callback_data="queue_failed",
                            )
                        ]
                    ]
                ),
            )
            return

        await query.edit_message_text(
            build_failed_queue_detail(row),
            reply_markup=failed_queue_actions_keyboard(
                property_id
            ),
        )
        return


    if action.startswith("queue_failed_repair_"):
        property_id = int(
            action.replace(
                "queue_failed_repair_",
                "",
                1,
            )
        )

        await query.edit_message_text(
            "🛠 در حال بازخوانی و آماده‌سازی مجدد فایل..."
        )

        try:
            parsed = repair_failed_property(
                property_id
            )

            await query.edit_message_text(
                "✅ فایل با موفقیت بازخوانی شد.\n\n"
                "وضعیت جدید: 🟢 آماده انتشار\n"
                "فایل دوباره وارد صف Scheduler شد.\n\n"
                f"متراژ: {parsed.get('area_m2') or '-'}\n"
                f"قیمت: {parsed.get('current_price_billion') or '-'} میلیارد",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ فایل‌های خطادار",
                                callback_data="queue_failed",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "📥 صف انتشار",
                                callback_data="queue",
                            )
                        ],
                    ]
                ),
            )

        except Exception as exc:
            logging.exception(
                "Failed queue repair error: property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "❌ بازخوانی فایل انجام نشد.\n\n"
                f"خطا:\n{str(exc)[:700]}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت",
                                callback_data=(
                                    f"queue_failed_view_{property_id}"
                                ),
                            )
                        ]
                    ]
                ),
            )

        return


    if action.startswith("queue_failed_publish_"):
        property_id = int(
            action.replace(
                "queue_failed_publish_",
                "",
                1,
            )
        )

        await query.edit_message_text(
            "🚨 در حال تلاش مجدد برای انتشار فایل..."
        )

        try:
            result = await publish_property(
                context.bot,
                property_id,
            )

            if result.get("already_published"):
                await query.edit_message_text(
                    "⚠️ این فایل قبلاً در کانال منتشر شده است.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "⬅️ فایل‌های خطادار",
                                    callback_data="queue_failed",
                                )
                            ]
                        ]
                    ),
                )
                return

            await query.edit_message_text(
                "✅ فایل با موفقیت منتشر شد.\n\n"
                f"کد واحد: {result.get('unit_code')}\n"
                f"لینک پست:\n"
                f"{result.get('channel_post_url') or 'ثبت نشد'}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ فایل‌های خطادار",
                                callback_data="queue_failed",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "📥 صف انتشار",
                                callback_data="queue",
                            )
                        ],
                    ]
                ),
            )

        except Exception as exc:
            logging.exception(
                "Manual failed-queue publish failed: property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "❌ انتشار مجدد ناموفق بود.\n\n"
                f"خطا:\n{str(exc)[:700]}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ بازگشت به فایل",
                                callback_data=(
                                    f"queue_failed_view_{property_id}"
                                ),
                            )
                        ]
                    ]
                ),
            )

        return


    if action.startswith("queue_failed_skip_"):
        property_id = int(
            action.replace(
                "queue_failed_skip_",
                "",
                1,
            )
        )

        changed = skip_failed_property(
            property_id
        )

        if changed:
            text = (
                "🗑 فایل از صف انتشار حذف شد.\n\n"
                "خود فایل و سابقه آن در دیتابیس "
                "حذف نشده است."
            )
        else:
            text = (
                "⚠️ فایل دیگر در وضعیت خطادار نبود "
                "و تغییری انجام نشد."
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "❌ فایل‌های خطادار",
                            callback_data="queue_failed",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ فایل‌ها",
                            callback_data="files_menu",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "queue_search":
        context.user_data.clear()
        context.user_data["state"] = "search_queue"

        await query.edit_message_text(
            "🔎 جستجو در صف انتشار\n\n"
            "شماره صف را ارسال کنید.\n\n"
            "مثال:\n"
            "289\n"
            "یا\n"
            "صف 289",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ بازگشت به صف",
                            callback_data="queue",
                        )
                    ]
                ]
            ),
        )
        return


    if action.startswith("queue_page_"):
        try:
            offset = int(
                action.replace(
                    "queue_page_",
                    "",
                    1,
                )
            )
        except ValueError:
            offset = 0

        counts = get_real_queue_counts()
        total = sum(counts.values())

        if total > 0:
            last_valid_offset = (
                (total - 1) // 10
            ) * 10

            offset = min(
                max(0, offset),
                last_valid_offset,
            )
        else:
            offset = 0

        await query.edit_message_text(
            build_real_queue_text(
                offset=offset,
                page_size=10,
            ),
            reply_markup=queue_menu_keyboard(
                offset=offset,
                total=total,
                page_size=10,
            ),
        )
        return

    if action == "queue":
        context.user_data.pop("state", None)

        counts = get_real_queue_counts()
        total = sum(counts.values())

        await query.edit_message_text(
            build_real_queue_text(
                offset=0,
                page_size=10,
            ),
            reply_markup=queue_menu_keyboard(
                offset=0,
                total=total,
                page_size=10,
            ),
        )
        return

    if action == "published":
        await query.edit_message_text(
            "✅ هنوز فایلی منتشر نشده است.",
            reply_markup=back_menu(),
        )
        return

    if action == "invalid":
        await query.edit_message_text(
            "⚠️ فعلاً فایل ناقصی ثبت نشده است.",
            reply_markup=back_menu(),
        )
        return

    if action == "smart_search":
        context.user_data.clear()
        context.user_data[
            "state"
        ] = "smart_search"

        await query.edit_message_text(
            "🔍 جستجوی هوشمند ملک\n\n"
            "هر چیزی که می‌خواهید بنویسید.\n\n"
            "مثال‌ها:\n"
            "• 60\n"
            "• 60 متر\n"
            "• 60 تا 90 متر\n"
            "• 5 تا 18 میلیارد\n"
            "• زیر 8 میلیارد\n"
            "• بالای 20 میلیارد\n"
            "• باغمیشه\n"
            "• 3 خواب باغمیشه\n"
            "• باغمیشه 120 متر 5 تا 18 میلیارد\n"
            "• طبقه پنجم",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "👥 کمپین‌های جذب مشتری",
                    callback_data="customer_campaigns",
                )],
                [InlineKeyboardButton(
                    "⬅️ بازگشت",
                    callback_data="home",
                )],
            ]),
        )
        return

    if action.startswith("smart_filter_"):
        filter_key = action.replace("smart_filter_", "", 1)
        raw_query = context.user_data.get("smart_search_query")
        if not raw_query:
            await query.edit_message_text(
                "⚠️ جستجوی قبلی منقضی شده است. دوباره جستجو کنید.",
                reply_markup=back_menu(),
            )
            return
        raw_query = toggle_smart_filter(raw_query, filter_key)
        result = smart_search_properties(raw_query, offset=0, limit=10)
        context.user_data["smart_search_query"] = raw_query
        context.user_data["smart_search_offset"] = 0
        await query.edit_message_text(
            smart_search_result_text(raw_query, result),
            reply_markup=smart_search_keyboard(result, raw_query),
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return

    if action.startswith(
        "smart_search_page_"
    ):
        try:
            offset = int(
                action.replace(
                    "smart_search_page_",
                    "",
                    1,
                )
            )
        except ValueError:
            offset = 0

        raw_query = context.user_data.get(
            "smart_search_query"
        )

        if not raw_query:
            await query.edit_message_text(
                "⚠️ جستجوی قبلی منقضی شده است.\n\n"
                "دوباره جستجوی هوشمند را شروع کنید.",
                reply_markup=back_menu(),
            )
            return

        result = smart_search_properties(
            raw_query,
            offset=offset,
            limit=10,
        )

        context.user_data[
            "smart_search_offset"
        ] = offset

        await query.edit_message_text(
            smart_search_result_text(
                raw_query,
                result,
            ),
            reply_markup=smart_search_keyboard(
                result
            ),
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return

    if action.startswith(
        "smart_search_view_"
    ):
        try:
            property_id = int(
                action.replace(
                    "smart_search_view_",
                    "",
                    1,
                )
            )
        except ValueError:
            await query.edit_message_text(
                "❌ فایل نامعتبر است.",
                reply_markup=back_menu(),
            )
            return

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT unit_code
                    FROM properties
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (property_id,),
                )

                row = cur.fetchone()

        if not row or not row[0]:
            await query.edit_message_text(
                "❌ فایل پیدا نشد.",
                reply_markup=back_menu(),
            )
            return

        unit_code = row[0]

        property_row, source_rows = (
            get_unit_code_full_details(
                unit_code
            )
        )

        if not property_row:
            await query.edit_message_text(
                "❌ جزئیات فایل پیدا نشد.",
                reply_markup=back_menu(),
            )
            return

        offset = context.user_data.get(
            "smart_search_offset",
            0,
        )

        back_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ بازگشت به نتایج",
                        callback_data=(
                            f"smart_search_page_{offset}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 منوی اصلی",
                        callback_data="home",
                    )
                ],
            ]
        )

        await query.edit_message_text(
            build_unit_code_search_text(
                property_row,
                source_rows,
                session=session,
            ),
            reply_markup=back_keyboard,
            disable_web_page_preview=True,
            read_timeout=10,
            connect_timeout=3,
        )
        return

    if action == "search_unit":
        context.user_data["state"] = "search_unit"

        await query.edit_message_text(
            "🔎 کد داخلی واحد را ارسال کنید.\n\nمثال:\n10380",
            reply_markup=back_menu(),
        )
        return

    if action == "category_send":
        context.user_data.clear()

        context.user_data[
            "state"
        ] = "category_range"

        suggestions = (
            get_category_range_suggestions(
                limit=3
            )
        )

        if suggestions:
            text = (
                "🗂 ارسال دسته‌بندی\n\n"
                "🔥 سه بازه پیشنهادی با بیشترین فایل:\n\n"
                "یکی از پیشنهادها را انتخاب کنید، "
                "یا بازه دلخواهتان را به میلیارد ارسال کنید.\n\n"
                "مثال:\n8 تا 15"
            )
        else:
            text = (
                "🗂 ارسال دسته‌بندی\n\n"
                "فعلاً بازه پیشنهادی مناسبی پیدا نشد.\n\n"
                "بازه دلخواهتان را به میلیارد ارسال کنید.\n\n"
                "مثال:\n8 تا 15"
            )

        await query.edit_message_text(
            text,
            reply_markup=category_start_keyboard(),
        )
        return

    if action.startswith(
        "category_suggest_"
    ):
        raw_range = action.replace(
            "category_suggest_",
            "",
            1,
        )

        parts = raw_range.split("_")

        if len(parts) != 2:
            await query.edit_message_text(
                "❌ بازه پیشنهادی نامعتبر است.",
                reply_markup=back_menu(),
            )
            return

        try:
            low = float(parts[0])
            high = float(parts[1])
        except ValueError:
            await query.edit_message_text(
                "❌ بازه پیشنهادی نامعتبر است.",
                reply_markup=back_menu(),
            )
            return

        rows = get_published_properties_in_range(
            low,
            high,
        )

        total = len(rows)

        if total == 0:
            await query.edit_message_text(
                "⚠️ در این بازه فایل منتشرشده‌ای "
                "وجود ندارد.",
                reply_markup=back_menu(),
            )
            return

        context.user_data.pop(
            "state",
            None,
        )

        context.user_data[
            "category_low"
        ] = low

        context.user_data[
            "category_high"
        ] = high

        hashtag = build_category_hashtag(
            low,
            high,
        )

        low_text = format_category_number(
            low
        )

        high_text = format_category_number(
            high
        )

        await query.edit_message_text(
            "✅ بازه انتخاب شد.\n\n"
            f"{hashtag}\n"
            f"🏷 {low_text} تا {high_text} میلیارد تومان\n\n"
            f"📊 تعداد فایل‌های غیرتکراری: {total}\n\n"
            "کدام گروه به کانال ارسال شود؟",
            reply_markup=category_pages_keyboard(
                total
            ),
        )
        return


    if action.startswith("category_page_"):
        try:
            offset = int(
                action.replace(
                    "category_page_",
                    "",
                    1,
                )
            )
        except ValueError:
            await query.edit_message_text(
                "❌ شماره صفحه نامعتبر است.",
                reply_markup=back_menu(),
            )
            return

        low = context.user_data.get(
            "category_low"
        )

        high = context.user_data.get(
            "category_high"
        )

        if low is None or high is None:
            await query.edit_message_text(
                "⚠️ اطلاعات بازه منقضی شده است.\n\n"
                "دوباره از «ارسال دسته‌بندی» شروع کنید.",
                reply_markup=back_menu(),
            )
            return

        all_rows = get_published_properties_in_range(
            low,
            high,
        )

        page_rows = all_rows[
            offset:offset + 10
        ]

        if not page_rows:
            await query.edit_message_text(
                "⚠️ در این صفحه فایلی وجود ندارد.",
                reply_markup=back_menu(),
            )
            return

        destination = get_destination_channel()

        if not destination:
            await query.edit_message_text(
                "❌ کانال اصلی فعال پیدا نشد.",
                reply_markup=back_menu(),
            )
            return

        (
            destination_channel_id,
            destination_title,
            destination_username,
        ) = destination

        category_text = build_category_post_text(
            low,
            high,
            page_rows,
            offset=offset,
            total=len(all_rows),
            page_size=10,
        )

        await query.edit_message_text(
            "⏳ در حال ارسال دسته‌بندی به کانال..."
        )

        sent = None
        category_post_id = None

        try:
            hashtag = build_category_hashtag(
                low,
                high,
            )

            # Reserve the database id first so the very first Telegram
            # send can already contain working callback data. The row
            # remains inactive until Telegram confirms delivery.
            category_post_id = record_category_post(
                low=low,
                high=high,
                offset=offset,
                destination_channel_id=destination_channel_id,
                telegram_message_id=0,
                hashtag=hashtag,
                rows=page_rows,
                created_by_user_id=session[0],
                is_active=False,
            )

            navigation_keyboard = (
                category_navigation_keyboard(
                    category_post_id,
                    offset,
                    len(all_rows),
                    10,
                )
            )

            sent = await context.bot.send_message(
                chat_id=destination_channel_id,
                text=category_text,
                reply_markup=navigation_keyboard,
                disable_web_page_preview=True,
            )

            activate_category_post_delivery(
                category_post_id,
                destination_channel_id,
                sent.message_id,
            )

        except Exception as exc:
            logging.exception(
                "Category publish failed: %s",
                exc,
            )

            # Never leave a visible but unusable category post behind.
            if sent is not None:
                try:
                    await context.bot.delete_message(
                        chat_id=destination_channel_id,
                        message_id=sent.message_id,
                    )
                except Exception:
                    logging.exception(
                        "Could not remove incomplete category post: "
                        "message_id=%s",
                        sent.message_id,
                    )

            if category_post_id is not None:
                try:
                    deactivate_category_post(
                        category_post_id
                    )
                except Exception:
                    logging.exception(
                        "Could not deactivate incomplete category post: "
                        "category_post_id=%s",
                        category_post_id,
                    )

            await query.edit_message_text(
                "❌ ارسال دسته‌بندی ناموفق بود.\n\n"
                f"خطا: {exc}",
                reply_markup=back_menu(),
            )
            return

        start_num = offset + 1
        end_num = offset + len(
            page_rows
        )

        context.user_data.pop(
            "category_low",
            None,
        )

        context.user_data.pop(
            "category_high",
            None,
        )

        await query.edit_message_text(
            "✅ دسته‌بندی با موفقیت به کانال ارسال شد.\n\n"
            f"{hashtag}\n"
            f"فایل‌های {start_num} تا {end_num}\n"
            f"تعداد فایل: {len(page_rows)}\n"
            f"شناسه دسته‌بندی: #{category_post_id}",
            reply_markup=back_menu(),
        )
        return


    if action == "stats":
        await query.edit_message_text(
            "📊 آمار\n\n"
            "این بخش بعد از ورود فایل‌های واقعی فعال می‌شود.",
            reply_markup=settings_back_menu(),
        )
        return

    if action == "hashtags":
        await query.edit_message_text(
            format_hashtag_stats(),
            reply_markup=settings_back_menu(),
        )
        return

    if action == "schedule_add":
        context.user_data.clear()

        await query.edit_message_text(
            "➕ افزودن بازه زمان‌بندی\n\n"
            "روز هفته را انتخاب کنید:",
            reply_markup=schedule_weekday_keyboard(),
        )
        return


    if action == "schedule_day_all":
        if context.user_data.get(
            "schedule_edit_id"
        ):
            await query.answer(
                "برای ویرایش یک بازه، یک روز مشخص را انتخاب کنید.",
                show_alert=True,
            )
            return

        context.user_data[
            "schedule_all_days"
        ] = True

        context.user_data[
            "schedule_weekday"
        ] = None

        context.user_data[
            "state"
        ] = "schedule_start_time"

        await query.edit_message_text(
            "✅ روزها انتخاب شد: همه روزها\n\n"
            "🕐 ساعت شروع را ارسال کنید.\n\n"
            "فرمت 24 ساعته باشد.\n"
            "مثال:\n09:00"
        )
        return


    if action.startswith("schedule_day_"):
        context.user_data.pop(
            "schedule_all_days",
            None,
        )

        weekday = int(
            action.replace(
                "schedule_day_",
                "",
                1,
            )
        )

        if weekday not in WEEKDAY_NAMES:
            await query.edit_message_text(
                "❌ روز نامعتبر است.",
                reply_markup=schedule_menu(),
            )
            return

        context.user_data[
            "schedule_weekday"
        ] = weekday

        context.user_data[
            "state"
        ] = "schedule_start_time"

        await query.edit_message_text(
            "✅ روز انتخاب شد: "
            f"{WEEKDAY_NAMES[weekday]}\n\n"
            "🕐 ساعت شروع را ارسال کنید.\n\n"
            "فرمت 24 ساعته باشد.\n"
            "مثال:\n09:00"
        )
        return


    if action == "schedule_list":
        await query.edit_message_text(
            format_schedule_windows(),
            reply_markup=schedule_windows_keyboard(),
        )
        return


    if action.startswith(
        "schedule_edit_group_"
    ):
        group_key = action.replace(
            "schedule_edit_group_",
            "",
            1,
        )

        context.user_data.clear()

        context.user_data[
            "schedule_edit_group_key"
        ] = group_key

        context.user_data[
            "state"
        ] = "schedule_start_time"

        await query.edit_message_text(
            "✏️ ویرایش زمان‌بندی همه روزها\n\n"
            "🕐 ساعت شروع جدید را ارسال کنید.\n\n"
            "مثال:\n10:30"
        )
        return


    if action.startswith(
        "schedule_toggle_group_"
    ):
        group_key = action.replace(
            "schedule_toggle_group_",
            "",
            1,
        )

        toggle_schedule_group(
            group_key
        )

        await query.edit_message_text(
            format_schedule_windows(),
            reply_markup=schedule_windows_keyboard(),
        )
        return


    if action.startswith(
        "schedule_delete_group_"
    ):
        group_key = action.replace(
            "schedule_delete_group_",
            "",
            1,
        )

        delete_schedule_group(
            group_key
        )

        await query.edit_message_text(
            "🗑 زمان‌بندی همه روزها حذف شد.\n\n"
            + format_schedule_windows(),
            reply_markup=schedule_windows_keyboard(),
        )
        return


    if action.startswith("schedule_edit_"):
        window_id = int(
            action.replace(
                "schedule_edit_",
                "",
                1,
            )
        )

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        weekday,
                        start_time,
                        end_time,
                        interval_minutes,
                        max_posts,
                        is_active
                    FROM schedule_windows
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (window_id,),
                )

                row = cur.fetchone()

        if not row:
            await query.edit_message_text(
                "❌ بازه زمان‌بندی پیدا نشد.",
                reply_markup=schedule_menu(),
            )
            return

        context.user_data.clear()

        context.user_data[
            "schedule_edit_id"
        ] = window_id

        await query.edit_message_text(
            f"✏️ ویرایش بازه #{window_id}\n\n"
            "اول روز هفته جدید را انتخاب کنید:\n\n"
            "تمام مشخصات این بازه دوباره گرفته می‌شود.",
            reply_markup=schedule_weekday_keyboard(),
        )
        return


    if action.startswith("schedule_toggle_"):
        window_id = int(
            action.replace(
                "schedule_toggle_",
                "",
                1,
            )
        )

        result = toggle_schedule_window(
            window_id
        )

        if result is None:
            await query.edit_message_text(
                "❌ بازه پیدا نشد.",
                reply_markup=schedule_menu(),
            )
            return

        await query.edit_message_text(
            format_schedule_windows(),
            reply_markup=schedule_windows_keyboard(),
        )
        return


    if action.startswith("schedule_delete_"):
        window_id = int(
            action.replace(
                "schedule_delete_",
                "",
                1,
            )
        )

        delete_schedule_window(
            window_id
        )

        await query.edit_message_text(
            "🗑 بازه حذف شد.\n\n"
            + format_schedule_windows(),
            reply_markup=schedule_windows_keyboard(),
        )
        return


    if action == "schedule":
        context.user_data.pop(
            "state",
            None,
        )

        await query.edit_message_text(
            format_schedule_windows(),
            reply_markup=schedule_menu(),
        )
        return

    if action == "users":
        await query.edit_message_text(
            "👥 مدیریت کاربران",
            reply_markup=back_menu(),
        )
        return

    if action == "reset":
        current_weeks = get_file_validity_weeks()

        await query.edit_message_text(
            "♻️ مدیریت فایل‌های قدیمی\n\n"
            f"مدت اعتبار فعلی فایل‌ها: "
            f"{current_weeks} هفته\n\n"
            "مدیریت فایل‌های قدیمی از بخش "
            "«اعتبار فایل‌ها» انجام می‌شود.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗓 اعتبار فایل‌ها",
                            callback_data="settings_file_validity",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🗑 سطل آشغال",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ بازگشت به تنظیمات",
                            callback_data="settings",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "settings_file_validity":
        context.user_data.pop(
            "state",
            None,
        )

        current_weeks = get_file_validity_weeks()

        expired_count = len(
            get_expired_property_rows(
                current_weeks
            )
        )

        await query.edit_message_text(
            "🗓 اعتبار فایل‌ها\n\n"
            f"مدت اعتبار فعلی: {current_weeks} هفته\n\n"
            f"فایل‌های فعال قدیمی‌تر از این بازه: "
            f"{expired_count}\n\n"
            "فایل‌های قدیمی فقط پس از تأیید شما "
            "از کانال حذف و به سطل آشغال منتقل می‌شوند.\n"
            "اطلاعات آن‌ها در دیتابیس باقی می‌ماند.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✏️ تغییر مدت اعتبار",
                            callback_data="settings_file_validity_edit",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🗑 مشاهده سطل آشغال",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ بازگشت به تنظیمات",
                            callback_data="settings",
                        )
                    ],
                ]
            ),
        )
        return


    if action == "settings_file_validity_edit":
        context.user_data.clear()

        context.user_data[
            "state"
        ] = "file_validity_weeks_input"

        current_weeks = get_file_validity_weeks()

        await query.edit_message_text(
            "✏️ تغییر مدت اعتبار فایل‌ها\n\n"
            f"مقدار فعلی: {current_weeks} هفته\n\n"
            "تعداد هفته جدید را ارسال کنید.\n\n"
            "مثال:\n8\n\n"
            "قبل از حذف هر فایلی، "
            "یک تأیید نهایی از شما گرفته می‌شود."
        )
        return


    if action.startswith("file_validity_confirm_"):
        try:
            weeks = int(
                action.replace(
                    "file_validity_confirm_",
                    "",
                    1,
                )
            )
        except ValueError:
            await query.edit_message_text(
                "❌ مقدار اعتبار نامعتبر است.",
                reply_markup=settings_menu(),
            )
            return

        if weeks < 1 or weeks > 104:
            await query.edit_message_text(
                "❌ مقدار اعتبار نامعتبر است.",
                reply_markup=settings_menu(),
            )
            return

        await query.edit_message_text(
            "⏳ در حال اعمال تنظیم اعتبار فایل‌ها...\n\n"
            "لطفاً چند لحظه صبر کنید."
        )

        save_app_setting(
            "file_validity_weeks",
            weeks,
        )

        result = await archive_expired_properties(
            context.bot,
            weeks,
        )

        archived_count = len(
            result["archived"]
        )

        failed_count = len(
            result["failed"]
        )

        text = (
            "✅ تنظیم اعتبار فایل‌ها اعمال شد.\n\n"
            f"مدت اعتبار: {weeks} هفته\n"
            f"🗑 منتقل‌شده به سطل آشغال: "
            f"{archived_count}\n"
        )

        if failed_count:
            text += (
                f"⚠️ ناموفق: {failed_count}\n\n"
                "فایل‌های ناموفق حذف نشده‌اند "
                "و در سیستم باقی مانده‌اند."
            )
        else:
            text += (
                "\nهمه عملیات بدون خطا انجام شد."
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 سطل آشغال",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🗓 اعتبار فایل‌ها",
                            callback_data="settings_file_validity",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ تنظیمات",
                            callback_data="settings",
                        )
                    ],
                ]
            ),
        )

        context.user_data.pop(
            "pending_file_validity_weeks",
            None,
        )

        return


    if action == "settings":
        # CRM_SETTINGS_MENU_V1
        await query.edit_message_text(
            "⚙️ تنظیمات\n\n"
            "مدیریت فایل‌ها، زمان‌بندی، آمار، CRM و تنظیمات انتشار از این بخش انجام می‌شود.",
            reply_markup=build_settings_menu(
                session,
                settings_menu(),
            ),
        )
        return

    if action == "settings_emojis":
        await query.edit_message_text(
            "🎨 تغییرات ایموجی پیام\n\n"
            "ایموجی هر بخش از متن فایل را از اینجا تغییر دهید.",
            reply_markup=emoji_settings_menu(),
        )
        return

    if action == "settings_footer":
        await query.edit_message_text(
            "📝 تغییرات اطلاعات ثابت فوتر\n\n"
            "متن و اطلاعات ثابتی که زیر همه فایل‌ها نمایش داده می‌شود را از اینجا تنظیم کنید.",
            reply_markup=footer_settings_menu(),
        )
        return

    if action == "settings_footer_contacts_edit":
        context.user_data.clear()
        context.user_data[
            "state"
        ] = "footer_contact_name"
        context.user_data[
            "footer_contact_people_draft"
        ] = []

        current_people = get_contact_people_setting()

        current_text = format_contact_people(
            current_people
        )

        await query.edit_message_text(
            "👥 نام و شماره تماس\n\n"
            "مخاطبین فعلی:\n"
            f"{current_text}\n\n"
            "نام شخص اول را ارسال کنید.\n\n"
            "بعد از ثبت نام و شماره، ربات می‌پرسد "
            "آیا شخص دیگری هم هست یا نه.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 حذف همه مخاطبین",
                            callback_data="settings_footer_contacts_clear_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ انصراف",
                            callback_data="settings_footer",
                        )
                    ],
                ]
            ),
        )
        return

    if action == "settings_footer_contacts_add_another":
        people = context.user_data.get(
            "footer_contact_people_draft",
            [],
        )

        if not people:
            await query.edit_message_text(
                "❌ هنوز شخصی ثبت نشده است.",
                reply_markup=footer_settings_menu(),
            )
            return

        context.user_data[
            "state"
        ] = "footer_contact_name"

        await query.edit_message_text(
            f"👥 ثبت شخص {len(people) + 1}\n\n"
            "نام شخص بعدی را ارسال کنید."
        )
        return

    if action == "settings_footer_contacts_finish":
        people = context.user_data.get(
            "footer_contact_people_draft",
            [],
        )

        if not people:
            await query.edit_message_text(
                "❌ هیچ شخصی برای ذخیره ثبت نشده است.",
                reply_markup=footer_settings_menu(),
            )
            return

        old_people = get_contact_people_setting()

        save_app_setting(
            "contact_people",
            people,
        )
        audit_footer_setting_change(
            session[0],
            "contact_people",
            old_people,
            people,
        )

        context.user_data.clear()

        await query.edit_message_text(
            footer_bulk_offer_text(
                "✅ نام و شماره تماس‌ها ذخیره شد.\n\n"
                + format_contact_people(people)
            ),
            reply_markup=footer_bulk_offer_markup(),
        )
        return

    if action == "settings_footer_contacts_clear_confirm":
        await query.edit_message_text(
            "⚠️ حذف همه نام‌ها و شماره‌های تماس\n\n"
            "آیا مطمئن هستید؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ بله، حذف شوند",
                            callback_data="settings_footer_contacts_clear_do",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ خیر",
                            callback_data="settings_footer",
                        )
                    ],
                ]
            ),
        )
        return

    if action == "settings_footer_contacts_clear_do":
        old_people = get_contact_people_setting()
        save_app_setting(
            "contact_people",
            [],
        )
        save_app_setting(
            "contact_name",
            "",
        )
        save_app_setting(
            "contact_phone",
            "",
        )
        audit_footer_setting_change(
            session[0],
            "contact_people",
            old_people,
            [],
        )
        context.user_data.clear()

        await query.edit_message_text(
            footer_bulk_offer_text(
                "✅ همه نام‌ها و شماره‌های تماس حذف شدند."
            ),
            reply_markup=footer_bulk_offer_markup(),
        )
        return

    if action == "attribute_rules":
        await query.edit_message_text(
            "🧩 مدیریت مشخصات پویا\n\n"
            "از این بخش می‌توانید مشخصه‌های جدید برای استخراج خودکار از فایل‌ها تعریف کنید.",
            reply_markup=attribute_rules_menu(),
        )
        return

    if action == "attribute_rules_list":
        rows = get_attribute_rules()

        if not rows:
            await query.edit_message_text(
                "📋 هنوز مشخصه پویایی تعریف نشده است.",
                reply_markup=attribute_rules_menu(),
            )
            return

        buttons = []

        for row in rows:
            (
                rule_id,
                attribute_key,
                attribute_label,
                match_type,
                trigger_text,
                default_value,
                display_order,
                is_active,
            ) = row

            status = "✅" if is_active else "⛔"

            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{status} {attribute_label}",
                        callback_data=f"attribute_rule_view_{rule_id}",
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    "⬅️ بازگشت",
                    callback_data="attribute_rules",
                )
            ]
        )

        await query.edit_message_text(
            "📋 مشخصات پویا\n\n"
            "برای مدیریت هر مشخصه روی آن بزنید.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action.startswith("attribute_rule_view_"):
        rule_id = int(
            action.replace(
                "attribute_rule_view_",
                "",
                1,
            )
        )

        rule = get_attribute_rule(rule_id)

        if not rule:
            await query.edit_message_text(
                "❌ این مشخصه پیدا نشد.",
                reply_markup=attribute_rules_menu(),
            )
            return

        (
            rule_id,
            attribute_key,
            attribute_label,
            match_type,
            trigger_text,
            default_value,
            display_order,
            is_active,
        ) = rule

        status = "فعال ✅" if is_active else "غیرفعال ⛔"

        await query.edit_message_text(
            f"🧩 {attribute_label}\n\n"
            f"وضعیت: {status}\n"
            f"نوع: {match_type}\n"
            f"عبارت تشخیص: {trigger_text}\n"
            f"مقدار پیش‌فرض: {default_value or '-'}",
            reply_markup=attribute_rule_actions_menu(
                rule_id,
                is_active,
            ),
        )
        return

    if action.startswith("attribute_rule_toggle_"):
        rule_id = int(
            action.replace(
                "attribute_rule_toggle_",
                "",
                1,
            )
        )

        new_status = toggle_attribute_rule(
            rule_id
        )

        rule = get_attribute_rule(rule_id)

        if not rule:
            await query.edit_message_text(
                "❌ این مشخصه پیدا نشد.",
                reply_markup=attribute_rules_menu(),
            )
            return

        (
            rule_id,
            attribute_key,
            attribute_label,
            match_type,
            trigger_text,
            default_value,
            display_order,
            is_active,
        ) = rule

        status_text = (
            "فعال شد ✅"
            if new_status
            else "غیرفعال شد ⛔"
        )

        await query.edit_message_text(
            f"🧩 {attribute_label}\n\n"
            f"{status_text}",
            reply_markup=attribute_rule_actions_menu(
                rule_id,
                is_active,
            ),
        )
        return

    if action.startswith("attribute_rule_delete_"):
        rule_id = int(
            action.replace(
                "attribute_rule_delete_",
                "",
                1,
            )
        )

        label = delete_attribute_rule(
            rule_id
        )

        if not label:
            await query.edit_message_text(
                "❌ این مشخصه پیدا نشد.",
                reply_markup=attribute_rules_menu(),
            )
            return

        await query.edit_message_text(
            f"🗑 مشخصه «{label}» حذف شد.",
            reply_markup=attribute_rules_menu(),
        )
        return

    if action == "attribute_rule_add":
        context.user_data["state"] = "attribute_rule_label"

        await query.edit_message_text(
            "➕ افزودن مشخصه جدید\n\n"
            "نام نمایشی مشخصه را ارسال کنید.\n"
            "مثال:\nویو",
            reply_markup=back_menu(),
        )
        return

    if action.startswith("attribute_rule_type_"):
        match_type = action.replace(
            "attribute_rule_type_",
            "",
            1,
        )

        if match_type not in (
            "PRESENCE",
            "VALUE_AFTER",
            "NUMBER_BEFORE",
        ):
            await query.edit_message_text(
                "❌ نوع Rule نامعتبر است.",
                reply_markup=attribute_rules_menu(),
            )
            return

        context.user_data["attribute_rule_match_type"] = match_type
        context.user_data["state"] = "attribute_rule_trigger"

        examples = {
            "PRESENCE": "مثال:\nلابی",
            "VALUE_AFTER": "مثال:\nویو",
            "NUMBER_BEFORE": "مثال:\nواحد در طبقه",
        }

        await query.edit_message_text(
            "عبارت تشخیص را ارسال کنید.\n\n"
            + examples[match_type],
            reply_markup=back_menu(),
        )
        return

    if action == "settings_show":
        await query.edit_message_text(
            current_publish_settings_text(),
            reply_markup=settings_menu(),
        )
        return

    if action.startswith("setting_edit_"):
        setting_key = action.replace(
            "setting_edit_",
            "",
            1,
        )

        label = PUBLISH_SETTING_LABELS.get(
            setting_key
        )

        if not label:
            await query.edit_message_text(
                "❌ تنظیم موردنظر پیدا نشد.",
                reply_markup=settings_menu(),
            )
            return

        context.user_data["state"] = "edit_publish_setting"
        context.user_data["setting_key"] = setting_key

        current_value = get_app_setting(
            setting_key,
            "تنظیم نشده",
        )

        await query.edit_message_text(
            f"{label}\n\n"
            f"مقدار فعلی:\n{current_value}\n\n"
            "مقدار جدید را ارسال کنید.",
            reply_markup=back_menu(),
        )
        return

    if action == "logs":
        await query.edit_message_text(
            "🧾 لاگ‌ها",
            reply_markup=back_menu(),
        )
        return


async def category_navigation_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    action = query.data or ""

    if not action.startswith(
        "catnav_"
    ):
        return

    try:
        parts = action.split(
            "_",
            2,
        )

        category_post_id = int(
            parts[1]
        )

        requested_offset = int(
            parts[2]
        )

    except (
        ValueError,
        IndexError,
    ):
        await query.answer(
            "صفحه نامعتبر است.",
            show_alert=True,
        )
        return

    category_row = (
        get_category_post_for_navigation(
            category_post_id
        )
    )

    if not category_row:
        await query.answer(
            "این دسته‌بندی دیگر موجود نیست.",
            show_alert=True,
        )
        return

    (
        category_post_id,
        low,
        high,
        current_offset,
        page_size,
        channel_id,
        message_id,
        is_active,
    ) = category_row

    if not is_active:
        await query.answer(
            "این دسته‌بندی دیگر فعال نیست.",
            show_alert=True,
        )
        return

    page_size = int(
        page_size or 10
    )

    all_rows = (
        get_published_properties_in_range(
            float(low),
            float(high),
        )
    )

    total = len(all_rows)

    if total == 0:
        await query.answer(
            "فایلی در این دسته‌بندی باقی نمانده است.",
            show_alert=True,
        )
        return

    last_offset = (
        (total - 1) // page_size
    ) * page_size

    offset = max(
        0,
        min(
            requested_offset,
            last_offset,
        ),
    )

    current_saved_offset = int(
        current_offset or 0
    )

    if offset == current_saved_offset:
        total_pages = (
            total + page_size - 1
        ) // page_size

        current_page = (
            offset // page_size
        ) + 1

        await query.answer(
            (
                f"📄 شما همین حالا در صفحه "
                f"{current_page} از {total_pages} هستید."
            )
        )
        return

    page_rows = all_rows[
        offset:
        offset + page_size
    ]

    text = build_category_post_text(
        float(low),
        float(high),
        page_rows,
        offset=offset,
        total=total,
        page_size=page_size,
    )

    keyboard = (
        category_navigation_keyboard(
            category_post_id,
            offset,
            total,
            page_size,
        )
    )

    # === CATEGORY NAV EARLY CALLBACK ACK V1 ===
    # Telegram callback queries expire quickly.
    # Acknowledge the click BEFORE long edit/retry work.
    try:
        await query.answer(
            "⏳ در حال تغییر صفحه..."
        )
    except Exception as exc:
        logging.debug(
            "Category navigation early callback ACK failed: %s",
            exc,
        )

    try:
        await _edit_category_message_with_retry(
            context.bot,
            chat_id=channel_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )

    except Exception as exc:
        error_text = str(
            exc
        ).lower()

        if (
            "message is not modified"
            not in error_text
        ):
            logging.exception(
                "Category navigation failed: "
                "category_post_id=%s "
                "offset=%s",
                category_post_id,
                offset,
            )

            logging.warning(
                "Category navigation retries exhausted: "
                "category_post_id=%s offset=%s",
                category_post_id,
                offset,
            )

            return

    update_category_post_offset(
        category_post_id,
        offset,
    )

    replace_category_post_items(
        category_post_id,
        page_rows,
    )

    logging.info(
        "Category page changed: "
        "category_post_id=%s "
        "offset=%s total=%s",
        category_post_id,
        offset,
        total,
    )




# =========================================================
# CRM_RUNTIME_WRAPPERS_V1
# =========================================================

_legacy_category_start_keyboard_crm = category_start_keyboard


def category_start_keyboard():
    keyboard = _legacy_category_start_keyboard_crm()

    rows = [
        list(row)
        for row in keyboard.inline_keyboard
    ]

    already_present = any(
        button.callback_data == "category_region_start"
        for row in rows
        for button in row
    )

    if not already_present:
        insert_at = max(0, len(rows) - 1)

        rows.insert(
            insert_at,
            [
                InlineKeyboardButton(
                    "📍 ارسال بر اساس منطقه",
                    callback_data="category_region_start",
                )
            ],
        )

    return InlineKeyboardMarkup(rows)



# === TARGETED CATEGORY SYNC V1 ===

def _get_property_current_price_toman(
    property_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_price_toman
                FROM properties
                WHERE id = %s
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        return None

    return row[0]


def _get_affected_price_category_ids(
    *prices_toman,
):
    prices_billion = [
        float(value) / 1_000_000_000
        for value in prices_toman
        if value is not None
    ]

    if not prices_billion:
        return []

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    min_price_billion,
                    max_price_billion
                FROM category_posts
                WHERE is_active = TRUE
                  AND COALESCE(updated_at, created_at)
                      >= NOW() - INTERVAL '11 days'
                ORDER BY id
                """
            )

            rows = cur.fetchall()

    result = []

    for (
        category_post_id,
        low,
        high,
    ) in rows:

        low = float(low)
        high = float(high)

        if any(
            low <= price <= high
            for price in prices_billion
        ):
            result.append(
                int(category_post_id)
            )

    return result


def _get_property_region_category_names(
    property_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT
                    r.region_name
                FROM region_category_posts r
                JOIN properties p
                  ON p.id = %s
                WHERE r.is_active = TRUE
                  AND COALESCE(r.updated_at, r.created_at)
                      >= NOW() - INTERVAL '11 days'
                  AND (
                    COALESCE(
                        p.district,
                        ''
                    ) ILIKE (
                        '%%'
                        || r.region_name
                        || '%%'
                    )
                    OR
                    COALESCE(
                        p.neighborhood,
                        ''
                    ) ILIKE (
                        '%%'
                        || r.region_name
                        || '%%'
                    )
                  )
                ORDER BY r.region_name
                """,
                (property_id,),
            )

            rows = cur.fetchall()

    return [
        row[0]
        for row in rows
        if row
        and row[0]
    ]


async def sync_categories_for_price_change(
    bot,
    property_id,
    old_price_toman,
    new_price_toman=None,
    extra_region_names=None,
):
    """
    Fast path for a property price change.

    Only:
      - price categories containing OLD or NEW price
      - region categories containing this property

    are refreshed.

    This intentionally bypasses the global category
    wrapper so one price edit does not refresh every
    category and every region.
    """

    if new_price_toman is None:
        new_price_toman = (
            _get_property_current_price_toman(
                property_id
            )
        )

    region_names = set(
        _get_property_region_category_names(
            property_id
        )
    )

    for name in (
        extra_region_names or []
    ):
        if name:
            region_names.add(name)

    return await queue_targeted_category_sync(
        bot,
        [property_id],
        reason="price-or-region-change",
        prices_toman=[old_price_toman, new_price_toman],
        region_names=sorted(region_names),
    )


_legacy_sync_active_category_posts_crm = (
    sync_active_category_posts
)


# === CATEGORY SYNC COALESCER V2 ===
CATEGORY_SYNC_DEBOUNCE_SECONDS = 60
_category_sync_pending_property_ids = set()
_category_sync_pending_reasons = set()
_category_sync_pending_category_ids = set()
_category_sync_pending_region_names = set()
_category_sync_worker_task = None
_category_sync_lock = asyncio.Lock()


async def _run_targeted_category_sync_batch(
    bot, property_ids, reasons, category_ids, region_names
):
    result = {"synced": 0, "removed": 0}

    async with _category_sync_lock:
        if category_ids:
            result = await _legacy_sync_active_category_posts_crm(
                bot,
                category_ids=sorted(category_ids),
            )

        if region_names:
            await sync_region_category_posts(
                bot,
                region_names=sorted(region_names),
            )

    logging.info(
        "CATEGORY sync batch completed: properties=%s reasons=%s "
        "price_categories=%s regions=%s synced=%s removed=%s",
        sorted(property_ids), sorted(reasons), sorted(category_ids),
        sorted(region_names), result.get("synced", 0),
        result.get("removed", 0),
    )


async def _category_sync_coalescer_worker(bot):
    global _category_sync_worker_task

    try:
        while _category_sync_pending_property_ids:
            await asyncio.sleep(CATEGORY_SYNC_DEBOUNCE_SECONDS)
            property_ids = set(_category_sync_pending_property_ids)
            reasons = set(_category_sync_pending_reasons)
            category_ids = set(_category_sync_pending_category_ids)
            region_names = set(_category_sync_pending_region_names)
            _category_sync_pending_property_ids.difference_update(property_ids)
            _category_sync_pending_reasons.difference_update(reasons)
            _category_sync_pending_category_ids.difference_update(category_ids)
            _category_sync_pending_region_names.difference_update(region_names)

            try:
                await _run_targeted_category_sync_batch(
                    bot, property_ids, reasons, category_ids, region_names
                )
            except Exception:
                logging.exception(
                    "Targeted category sync batch failed: properties=%s",
                    sorted(property_ids),
                )
    finally:
        _category_sync_worker_task = None
        if _category_sync_pending_property_ids:
            _category_sync_worker_task = asyncio.create_task(
                _category_sync_coalescer_worker(bot),
                name="category-sync-coalescer",
            )


async def queue_targeted_category_sync(
    bot,
    property_ids,
    reason="property-change",
    prices_toman=None,
    region_names=None,
):
    global _category_sync_worker_task

    valid_ids = {
        int(property_id)
        for property_id in property_ids
        if property_id is not None
    }
    if not valid_ids:
        return {"synced": 0, "removed": 0, "queued": 0}

    _category_sync_pending_property_ids.update(valid_ids)
    _category_sync_pending_reasons.add(str(reason))

    prices = list(prices_toman or [])
    for property_id in valid_ids:
        prices.append(_get_property_current_price_toman(property_id))
        _category_sync_pending_region_names.update(
            _get_property_region_category_names(property_id)
        )

    _category_sync_pending_category_ids.update(
        _get_affected_price_category_ids(*prices)
    )
    _category_sync_pending_region_names.update(
        name for name in (region_names or []) if name
    )

    if _category_sync_worker_task is None or _category_sync_worker_task.done():
        _category_sync_worker_task = asyncio.create_task(
            _category_sync_coalescer_worker(bot),
            name="category-sync-coalescer",
        )

    logging.info(
        "CATEGORY sync queued: properties=%s reason=%s debounce=%ss",
        sorted(valid_ids), reason, CATEGORY_SYNC_DEBOUNCE_SECONDS,
    )
    return {"synced": 0, "removed": 0, "queued": len(valid_ids)}


async def sync_active_category_posts(bot):
    result = await _legacy_sync_active_category_posts_crm(
        bot
    )

    try:
        await sync_region_category_posts(bot)

    except Exception:
        logging.exception(
            "Region category sync failed"
        )

    return result


async def final_private_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not session:
        if await handle_smart_customer_photo(update, context):
            return
        return
    await handle_final_photo(update, context, session)


async def smart_private_contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not session:
        if await handle_smart_customer_contact(update, context):
            return
    await handle_visit_contact(update, context)


async def connect_bazdid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id) if update.effective_user else None
    await handle_visit_ops_connect(update, context, session)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("connect_bazdid", connect_bazdid_command))

    app.add_handler(
        MessageHandler(
            filters.CONTACT & filters.ChatType.PRIVATE,
            smart_private_contact_handler,
        ),
        group=-3,
    )

    app.add_handler(
        CallbackQueryHandler(
            category_navigation_handler,
            pattern=r"^catnav_",
        ),
        group=-2,
    )

    app.add_handler(
        CallbackQueryHandler(
            region_category_navigation_handler,
            pattern=r"^regcat_",
        ),
        group=-2,
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_visit_source_callback,
            pattern=r"^visitsrc_",
        ),
        group=-2,
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=r"^(?!catnav_|regcat_|visitsrc_)",
        ),
        group=0,
    )

    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS,
            input_channel_post_handler,
        ),
        group=-1,
    )


    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO & ~filters.FORWARDED,
            final_private_photo_handler,
        ),
        group=-2,
    )

    app.add_handler(
        MessageHandler(
            filters.FORWARDED,
            forwarded_message_handler,
        ),
        group=0,
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        ),
        group=1,
    )

    logging.info("RealEstate Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()


# === MENU SETTINGS HUB V3 ===
# files/stats/hashtags/schedule/reset/CRM report moved under Settings.


# === MENU SETTINGS HUB V3.1 BOT NAVIGATION ===


# === SHAHFILE SMART CUSTOMER FLOW V1 ROUTER ===
# Public Persian intent routing, visit approvals, seller intake and privacy gate.


# === URGENT LOOKUP ROOT FIX V2 ===
