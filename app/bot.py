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

from scheduler import start_scheduler
from visit_flow import (
    handle_visit_start,
    handle_visit_contact,
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
from customer_showcase import (
    ensure_showcase_schema,
    manager_filters_to_public,
    create_showcase,
    build_showcase_link,
    handle_showcase_start,
    handle_showcase_callback,
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
    "publish_footer": "ğŸ“ Ù…ØªÙ† Ø«Ø§Ø¨Øª Ø§Ù†ØªÙ‡Ø§ÛŒ Ù¾Ø³Øª",
    "contact_people": "ğŸ‘¥ Ù†Ø§Ù… Ùˆ Ø´Ù…Ø§Ø±Ù‡ ØªÙ…Ø§Ø³",
    "contact_telegram": "ğŸ“± Ø¢ÛŒØ¯ÛŒ ØªÙ„Ú¯Ø±Ø§Ù…",
    "emoji_location": "ğŸ“ Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù„ÙˆÚ©ÛŒØ´Ù†",
    "emoji_area": "ğŸ“ Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù…ØªØ±Ø§Ú˜",
    "emoji_bedrooms": "ğŸ› Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ø®ÙˆØ§Ø¨",
    "emoji_floor": "ğŸ¢ Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ø·Ø¨Ù‚Ù‡",
    "emoji_build_year": "ğŸ“… Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ø³Ø§Ù„ Ø³Ø§Ø®Øª",
    "emoji_direction": "ğŸ§­ Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ø¬Ù‡Øª",
    "emoji_parking": "ğŸš— Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù¾Ø§Ø±Ú©ÛŒÙ†Ú¯",
    "emoji_elevator": "ğŸ›— Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ø¢Ø³Ø§Ù†Ø³ÙˆØ±",
    "emoji_storage": "ğŸ“¦ Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ø§Ù†Ø¨Ø§Ø±ÛŒ",
    "emoji_price": "ğŸ’° Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù‚ÛŒÙ…Øª",
    "emoji_unknown": "ğŸ”¹ Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù…ÙˆØ§Ø±Ø¯ Ù†Ø§Ø´Ù†Ø§Ø®ØªÙ‡",
}


def settings_menu():
    rows = [
        [
            InlineKeyboardButton(
                "ğŸ¨ ØªØºÛŒÛŒØ±Ø§Øª Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù¾ÛŒØ§Ù…",
                callback_data="settings_emojis",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ“ ØªØºÛŒÛŒØ±Ø§Øª Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ø«Ø§Ø¨Øª ÙÙˆØªØ±",
                callback_data="settings_footer",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ§© Ù…Ø¯ÛŒØ±ÛŒØª Ù…Ø´Ø®ØµØ§Øª Ù¾ÙˆÛŒØ§",
                callback_data="attribute_rules",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ—“ Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§",
                callback_data="settings_file_validity",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ‘ Ù†Ù…Ø§ÛŒØ´ ØªÙ†Ø¸ÛŒÙ…Ø§Øª ÙØ¹Ù„ÛŒ",
                callback_data="settings_show",
            )
        ],
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª",
                callback_data="home",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def emoji_settings_menu():
    rows = [
        [
            InlineKeyboardButton(
                "ğŸ“ Ù„ÙˆÚ©ÛŒØ´Ù†",
                callback_data="setting_edit_emoji_location",
            ),
            InlineKeyboardButton(
                "ğŸ“ Ù…ØªØ±Ø§Ú˜",
                callback_data="setting_edit_emoji_area",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ› Ø®ÙˆØ§Ø¨",
                callback_data="setting_edit_emoji_bedrooms",
            ),
            InlineKeyboardButton(
                "ğŸ¢ Ø·Ø¨Ù‚Ù‡",
                callback_data="setting_edit_emoji_floor",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ“… Ø³Ø§Ù„ Ø³Ø§Ø®Øª",
                callback_data="setting_edit_emoji_build_year",
            ),
            InlineKeyboardButton(
                "ğŸ§­ Ø¬Ù‡Øª",
                callback_data="setting_edit_emoji_direction",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ’° Ù‚ÛŒÙ…Øª",
                callback_data="setting_edit_emoji_price",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸš— Ù¾Ø§Ø±Ú©ÛŒÙ†Ú¯",
                callback_data="setting_edit_emoji_parking",
            ),
            InlineKeyboardButton(
                "ğŸ›— Ø¢Ø³Ø§Ù†Ø³ÙˆØ±",
                callback_data="setting_edit_emoji_elevator",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ“¦ Ø§Ù†Ø¨Ø§Ø±ÛŒ",
                callback_data="setting_edit_emoji_storage",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ”¹ Ù…ÙˆØ§Ø±Ø¯ Ù†Ø§Ø´Ù†Ø§Ø®ØªÙ‡",
                callback_data="setting_edit_emoji_unknown",
            )
        ],
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
                callback_data="settings",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def footer_settings_menu():
    rows = [
        [
            InlineKeyboardButton(
                "ğŸ“ Ù…ØªÙ† Ø«Ø§Ø¨Øª Ø§Ù†ØªÙ‡Ø§ÛŒ Ù¾Ø³Øª",
                callback_data="setting_edit_publish_footer",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ‘¥ Ù†Ø§Ù… Ùˆ Ø´Ù…Ø§Ø±Ù‡ ØªÙ…Ø§Ø³",
                callback_data="settings_footer_contacts_edit",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ“± Ø¢ÛŒØ¯ÛŒ ØªÙ„Ú¯Ø±Ø§Ù…",
                callback_data="setting_edit_contact_telegram",
            )
        ],
        [
            InlineKeyboardButton(
                "â™»ï¸ Ø§Ø¹Ù…Ø§Ù„ ÙÙˆØªØ± ÙØ¹Ù„ÛŒ Ø±ÙˆÛŒ Ù¾Ø³Øªâ€ŒÙ‡Ø§ÛŒ Ù‚Ø¨Ù„ÛŒ",
                callback_data="footer_bulk_offer",
            )
        ],
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
                callback_data="settings",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)



def attribute_rules_menu():
    rows = [
        [
            InlineKeyboardButton(
                "â• Ø§ÙØ²ÙˆØ¯Ù† Ù…Ø´Ø®ØµÙ‡ Ø¬Ø¯ÛŒØ¯",
                callback_data="attribute_rule_add",
            )
        ],
        [
            InlineKeyboardButton(
                "ğŸ“‹ Ù…Ø´Ø§Ù‡Ø¯Ù‡ Ù…Ø´Ø®ØµÙ‡â€ŒÙ‡Ø§",
                callback_data="attribute_rules_list",
            )
        ],
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
                callback_data="settings",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)



def attribute_rule_actions_menu(rule_id, is_active):
    toggle_text = (
        "â›” ØºÛŒØ±ÙØ¹Ø§Ù„ Ú©Ø±Ø¯Ù†"
        if is_active
        else "âœ… ÙØ¹Ø§Ù„ Ú©Ø±Ø¯Ù†"
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
                "ğŸ—‘ Ø­Ø°Ù Ù…Ø´Ø®ØµÙ‡",
                callback_data=f"attribute_rule_delete_{rule_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ Ù„ÛŒØ³Øª",
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
        return "ØªÙ†Ø¸ÛŒÙ… Ù†Ø´Ø¯Ù‡"

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

        lines.append(f"Ø´Ø®Øµ {index}:")
        lines.append(f"ğŸ‘¤ {name or '-'}")
        lines.append(f"ğŸ“ {phone or '-'}")

        if index != len(people):
            lines.append("")

    return "\n".join(lines)


def _normalize_contact_phone(value):
    value = str(value or "").strip()

    digit_map = str.maketrans(
        "Û°Û±Û²Û³Û´ÛµÛ¶Û·Û¸Û¹Ù Ù¡Ù¢Ù£Ù¤Ù¥Ù¦Ù§Ù¨Ù©",
        "01234567890123456789",
    )
    value = value.translate(digit_map)

    if not value:
        raise ValueError("Ø´Ù…Ø§Ø±Ù‡ ØªÙ…Ø§Ø³ Ø®Ø§Ù„ÛŒ Ø§Ø³Øª.")

    if not re.fullmatch(
        r"[+0-9][0-9+()\-\s]{5,24}",
        value,
    ):
        raise ValueError(
            "Ø´Ù…Ø§Ø±Ù‡ ØªÙ…Ø§Ø³ Ù…Ø¹ØªØ¨Ø± Ù†ÛŒØ³Øª."
        )

    return re.sub(r"\s+", " ", value)


def _contact_people_question_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "â• Ø¨Ù„Ù‡ØŒ Ø´Ø®Øµ Ø¯ÛŒÚ¯Ø±ÛŒ Ù‡Ø³Øª",
                    callback_data="settings_footer_contacts_add_another",
                )
            ],
            [
                InlineKeyboardButton(
                    "âœ… Ø®ÛŒØ±ØŒ Ø°Ø®ÛŒØ±Ù‡ Ú©Ù†",
                    callback_data="settings_footer_contacts_finish",
                )
            ],
            [
                InlineKeyboardButton(
                    "âŒ Ø§Ù†ØµØ±Ø§Ù",
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
             m«ëŒ+Š×®º+º$zzb¥â4TÄT5@¢Bç&÷W'G•ö–BÀ¢çVæ—Eö6öFRÀ¢æF—7G&–7BÀ¢æFG&W75÷FW‡BÀ¢æ&VöÓ"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢Bæ&6†—fVEöBÀ¢Bæ&6†—fU÷&V6öà¢e$ôÒ&÷W'G•÷G&6‚@¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒBç&÷W'G•ö–@¢õ$DU"%¢Bæ&6†—fVEöBDU42À¢Bç&÷W'G•ö–BDU40¢Ä”Ô•BW0¢ôde4UBW0¢"""À¢†Æ–Ö—BÂöfg6WB’À¢ ¢&WGW&â7W"æfWF6†ÆÂ‚  ¦FVbvWE÷G&6…ö6÷VçB‚“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5B4õTåB‚¢¢e$ôÒ&÷W'G•÷G&6€¢"" ¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢&WGW&â–çB‡&÷u³Ò÷"  ¦FVbvWE÷G&6…÷&÷W'G•öFWF–Â‡&÷W'G•ö–B“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢Bç&÷W'G•ö–BÀ¢çVæ—Eö6öFRÀ¢æF—7G&–7BÀ¢æFG&W75÷FW‡BÀ¢æ&VöÓ"À¢æ&VG&öö×2À¢æfÆö÷%÷FW‡BÀ¢æ'V–ÆE÷–V"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢Bæ&6†—fVEöBÀ¢Bæ&6†—fU÷&V6öâÀ¢BçfÆ–F—G•÷vVV·0¢e$ôÒ&÷W'G•÷G&6‚@¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒBç&÷W'G•ö–@¢t„U$RBç&÷W'G•ö–BÒW0¢Ä”Ô•B¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&WGW&â7W"æfWF6†öæR‚  ¦FVbG&6…öÆ—7Eö¶W–&ö&B†öfg6WCÓÂvU÷6—¦SÓ“ ¢F÷FÂÒvWE÷G&6…ö6÷VçB‚¢&÷w2ÒvWE÷G&6…÷&÷w2€¢Æ–Ö—C×vU÷6—¦RÀ¢öfg6WCÖöfg6WBÀ¢ ¢'WGFöç2ÒµĞ ¢f÷"&÷r–â&÷w3 ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&–6Uö&–ÆÆ–öâÀ¢&6†—fVEöBÀ¢&6†—fU÷&V6öâÀ¢’Ò&÷p ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-˜M˜ª¸Í‹M˜b˜mŠ}˜]‹MŠí‹R ¢ ¢Æö6F–öâÒ7G"†Æö6F–öâ’ç7G&—‚ ¢–bÆVâ†Æö6F–öâ’â#S ¢Æö6F–öâÒÆö6F–öå³£#UÒ².(
b  ¢6öFRÒ€¢7G"‡Væ—Eö6öFR¢–bVæ—Eö6öFP¢VÇ6Rb$”B·&÷W'G•ö–GÒ ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢b/	ùy¶6öFWÒÂ¶Æö6F–öçÒ"À¢6ÆÆ&6µöFFÒ€¢b'G&6…÷f–Wu÷·&÷W'G•ö–GÒ ¢’À¢¢Ğ¢ ¢æf–vF–öâÒµĞ ¢–böfg6WBâ ¢æf–vF–öâæVæB€¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜-Š˜M¸Â"À¢6ÆÆ&6µöFFÒ€¢b'G&6…÷vU÷¶Ö‚ƒÂöfg6WB×vU÷6—¦R—Ò ¢’À¢¢ ¢–böfg6WB²vU÷6—¦RÂF÷FÃ ¢æf–vF–öâæVæB€¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-Š‹Šı¸Â)êûˆò"À¢6ÆÆ&6µöFFÒ€¢b'G&6…÷vU÷¶öfg6WB·vU÷6—¦WÒ ¢’À¢¢ ¢–bæf–vF–öã ¢'WGFöç2æVæB†æf–vF–öâ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHBŠ‹˜‹-‹‹=Š}˜m¸Â"À¢6ÆÆ&6µöFFÖb'G&6…÷vU÷¶öfg6WGÒ"À¢¢Ğ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r˜Š}¸Í˜N(Í˜}Šr"À¢6ÆÆ&6µöFFÒ&f–ÆW5öÖVçR"À¢¢Ğ¢ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W†'WGFöç2  ¦FVb'V–ÆE÷G&6…öFWF–Â‡&÷r“ ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&VG&öö×2À¢fÆö÷%÷FW‡BÀ¢'V–ÆE÷–V"À¢&–6Uö&–ÆÆ–öâÀ¢&6†—fVEöBÀ¢&6†—fU÷&V6öâÀ¢fÆ–F—G•÷vVV·2À¢’Ò&÷p ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-Š½ŠŠ¢˜m‹MŠı˜r ¢ ¢&–6RÒ€¢b'¶fÆöB‡&–6Uö&–ÆÆ–öâ“¦wÒ˜]¸Í˜M¸ÍŠ}‹Šò ¢–b&–6Uö&–ÆÆ–öâ—2æ÷BæöæP¢VÇ6R-Š½ŠŠ¢˜m‹MŠı˜r ¢ ¢&6†—fU÷F–ÖRÒ€¢&6†—fVEöBæ7F–ÖW¦öæR€¢¦öæT–æfò‚$6–õFV‡&â"¢’ç7G&gF–ÖR‚"U’ÒVÒÒVBTƒ¢TÒ"¢–b&6†—fVEö@¢VÇ6R"Ò ¢ ¢&WGW&â€¢/	ùy˜Š}¸Í˜BŠ-‹‹M¸Í˜‹MŠı˜uÆåÆâ ¢b-ªŠò˜Š}ŠİŠó¢·Væ—Eö6öFR÷"rÒwÕÆâ ¢b/	ù8Ò¶Æö6F–öçÕÆâ ¢b/	ù9˜]Š­‹Š}©ƒ¢¶&VöÓ"÷"rÒwÕÆâ ¢b/	ù¸òŠí˜Š}Šƒ¢ ¢b'¶&VG&öö×2–b&VG&öö×2—2æ÷BæöæRVÇ6RrÒwÕÆâ ¢b/	øú"‹}Š˜-˜s¢¶fÆö÷%÷FW‡B÷"rÒwÕÆâ ¢b/	ù8R‹=Š}˜B‹=Š}ŠíŠ£¢¶'V–ÆE÷–V"÷"rÒwÕÆâ ¢b/	ù+˜-¸Í˜]Š£¢·&–6WÕÆåÆâ ¢b/	ùY"‹-˜]Š}˜bŠ}˜mŠ­˜-Š}˜C¢¶&6†—fU÷F–ÖWÕÆâ ¢b/	ù9Ò‹˜MŠ£¢¶&6†—fU÷&V6öâ÷"rÒwÕÆâ ¢b-Š}‹Š­ŠŠ}‹˜}˜mªıŠ}˜RŠİ‹˜¢ ¢b'·fÆ–F—G•÷vVV·2÷"rÒwÒ˜}˜Š­˜r ¢  ¦7–æ2FVb&6†—fU÷&÷W'G•÷Fõ÷G&6‚€¢&÷BÀ¢&÷W'G•ö–BÀ¢fÆ–F—G•÷vVV·2À¢“ ¢FW7F–æF–öâÒvWEöFW7F–æF–öåö6†ææVÂ‚ ¢–bæ÷BFW7F–æF–öã ¢&—6R'VçF–ÖTW'&÷"€¢-ªŠ}˜mŠ}˜BŠ}‹]˜M¸Â˜‹Š}˜B›í¸ÍŠıŠr˜m‹MŠòâ ¢ ¢FW7F–æF–öåö6†ææVÅö–BÒFW7F–æF–öå³Ğ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢Væ—Eö6öFRÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢7FGW2À¢FVÆWFVEög&öÕö6†ææVÅö@¢e$ôÒ&÷W'F–W0¢t„U$R–BÒW0¢Ä”Ô•B¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢–bæ÷B&÷s ¢&—6R'VçF–ÖTW'&÷"€¢-˜Š}¸Í˜BŠı‹Šı¸ÍŠ­Š}Š¸Í‹2›í¸ÍŠıŠr˜m‹MŠòâ ¢ ¢€¢Væ—Eö6öFRÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢&Wf–÷W5÷7FGW2À¢FVÆWFVEög&öÕö6†ææVÅöBÀ¢’Ò&÷p ¢–b€¢&Wf–÷W5÷7FGW2Ò$5D•dR ¢÷"FVÆWFVEög&öÕö6†ææVÅöB—2æ÷BæöæP¢“ ¢&—6R'VçF–ÖTW'&÷"€¢-˜Š}¸Í˜BŠı¸Íªı‹˜‹Š}˜B˜m¸Í‹=Š¢â ¢ ¢ÖVF–÷&÷w2ÒvWE÷&÷W'G•öÖVF–€¢&÷W'G•ö–@¢ ¢†÷Fõö6÷VçBÒÆVâ€¢°¢—FVĞ¢f÷"—FVÒ–âÖVF–÷&÷w0¢–b—FVÕ³ÒÓÒ%„õDò ¢Ğ¢ ¢ÖW76vUö–G2ÒµĞ ¢–bV&Æ—6†VEöÖW76vUö–C ¢Æ'VÕ÷6—¦RÒ€¢Ö–â‡†÷Fõö6÷VçBÂ¢–b†÷Fõö6÷VçBâ¢VÇ6R¢ ¢ÖW76vUö–G2Ò°¢–çB‡V&Æ—6†VEöÖW76vUö–B’²öfg6W@¢f÷"öfg6WB–â&ævR†Æ'VÕ÷6—¦R¢Ğ ¢Ö—76–æuöÖ&¶W'2Ò€¢&ÖW76vRFòFVÆWFRæ÷Bf÷VæB"À¢&ÖW76vRæ÷Bf÷VæB"À¢ ¢f÷"ÖW76vUö–B–âÖW76vUö–G3 ¢G'“ ¢v—B&÷BæFVÆWFUöÖW76vR€¢6†Eö–CÖFW7F–æF–öåö6†ææVÅö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢ ¢W†6WBW†6WF–öâ2W†3 ¢W'&÷%÷FW‡BÒ7G"†W†2’æÆ÷vW"‚ ¢–bç’€¢Ö&¶W"–âW'&÷%÷FW‡@¢f÷"Ö&¶W"–âÖ—76–æuöÖ&¶W'0¢“ ¢6öçF–çVP ¢–b€¢'F–ÖVB÷WB"–âW'&÷%÷FW‡@¢÷"'F–ÖV÷WB"–âW'&÷%÷FW‡@¢“ ¢G'“ ¢v—B&÷BæFVÆWFUöÖW76vR€¢6†Eö–CÖFW7F–æF–öåö6†ææVÅö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢¢6öçF–çVP ¢W†6WBW†6WF–öâ2&WG'•öW†3 ¢&WG'•÷FW‡BÒ7G"€¢&WG'•öW†0¢’æÆ÷vW"‚ ¢–bç’€¢Ö&¶W"–â&WG'•÷FW‡@¢f÷"Ö&¶W"–âÖ—76–æuöÖ&¶W'0¢“ ¢6öçF–çVP ¢&—6P ¢&—6P ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDò&÷W'G•÷G&6‚€¢&÷W'G•ö–BÀ¢&Wf–÷W5÷7FGW2À¢&Wf–÷W5÷V&Æ—6†VEöÖW76vUö–BÀ¢&Wf–÷W5ö6†ææVÅ÷÷7E÷W&ÂÀ¢&6†—fU÷&V6öâÀ¢fÆ–F—G•÷vVV·2À¢&6†—fVEö@¢¢dÅTU2€¢W2À¢W2À¢W2À¢W2À¢W2À¢W2À¢äõr‚¢¢ôâ4ôädÄ”5B‡&÷W'G•ö–B¢DòUDDR4U@¢&Wf–÷W5÷7FGW2Ğ¢U„4ÅTDTBç&Wf–÷W5÷7FGW2À¢&Wf–÷W5÷V&Æ—6†VEöÖW76vUö–BĞ¢U„4ÅTDTBç&Wf–÷W5÷V&Æ—6†VEöÖW76vUö–BÀ¢&Wf–÷W5ö6†ææVÅ÷÷7E÷W&ÂĞ¢U„4ÅTDTBç&Wf–÷W5ö6†ææVÅ÷÷7E÷W&ÂÀ¢&6†—fU÷&V6öâĞ¢U„4ÅTDTBæ&6†—fU÷&V6öâÀ¢fÆ–F—G•÷vVV·2Ğ¢U„4ÅTDTBçfÆ–F—G•÷vVV·2À¢&6†—fVEöBÒäõr‚¢"""À¢€¢&÷W'G•ö–BÀ¢&Wf–÷W5÷7FGW2À¢V&Æ—6†VEöÖW76vUö–BÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢€¢$W‡—&VB'’f–ÆRfÆ–F—G’öÆ–7’ ¢b"‡·fÆ–F—G•÷vVV·7ÒvVV·2’ ¢’À¢fÆ–F—G•÷vVV·2À¢’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'F–W0¢4U@¢7FGW2Òt”ä5D•dRrÀ¢FVÆWFVEög&öÕö6†ææVÅöBÒäõr‚’À¢V&Æ—6†VEöÖW76vUö–BÒåTÄÂÀ¢6†ææVÅ÷÷7E÷W&ÂÒåTÄÂÀ¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'G•÷6÷W&6W0¢4U@¢—5ö7F—fRÒdÅ4RÀ¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDRV&Æ—6…÷VWVP¢4U@¢VWVU÷7FGW2Òu4´•TBrÀ¢Æ7EöW'&÷"Ğ¢t&6†—fVB'’f–ÆRfÆ–F—G’öÆ–7’rÀ¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢äBVWVU÷7FGW2”â€¢täUrrÀ¢u%4TBrÀ¢u$TE’rÀ¢u44„TETÄTBrÀ¢uTäD”äuôEUÄ”4DRrÀ¢td”ÄTBrÀ¢uT$Ä•4„TBp¢¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&â°¢'&÷W'G•ö–B#¢&÷W'G•ö–BÀ¢'Væ—Eö6öFR#¢Væ—Eö6öFRÀ¢Ğ  ¦7–æ2FVb&6†—fUöW‡—&VE÷&÷W'F–W2€¢&÷BÀ¢vVV·2À¢“ ¢&÷w2ÒvWEöW‡—&VE÷&÷W'G•÷&÷w2€¢vVV·0¢ ¢&6†—fVBÒµĞ¢f–ÆVBÒµĞ ¢f÷"&÷r–â&÷w3 ¢&÷W'G•ö–BÒ&÷u³Ğ ¢G'“ ¢&W7VÇBÒ€¢v—B&6†—fU÷&÷W'G•÷Fõ÷G&6‚€¢&÷BÀ¢&÷W'G•ö–BÀ¢vVV·2À¢¢ ¢&6†—fVBæVæB‡&W7VÇB ¢W†6WBW†6WF–öâ2W†3 ¢Æövv–æræW†6WF–öâ€¢$W‡—'’&6†—fRf–ÆVC¢ ¢'&÷W'G•ö–CÒW2"À¢&÷W'G•ö–BÀ¢ ¢f–ÆVBæVæB€¢€¢&÷W'G•ö–BÀ¢7G"†W†2•³£3ÒÀ¢¢ ¢G'“ ¢v—BVWVU÷F&vWFVEö6FVv÷'•÷7–æ2€¢&÷BÀ¢¶—FVÒævWB‚'&÷W'G•ö–B"’f÷"—FVÒ–â&6†—fVEÒÀ¢&V6öãÒ&W‡—'’Ö&6†—fR"À¢¢W†6WBW†6WF–öã ¢Æövv–æræW†6WF–öâ€¢$6FVv÷'’7–æ2gFW"W‡—'’&6†—fRf–ÆVB ¢ ¢&WGW&â°¢&&6†—fVB#¢&6†—fVBÀ¢&f–ÆVB#¢f–ÆVBÀ¢Ğ  ¦FVb&W7F÷&U÷&÷W'G•ög&öÕ÷G&6‚€¢&÷W'G•ö–BÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5B&÷W'G•ö–@¢e$ôÒ&÷W'G•÷G&6€¢t„U$R&÷W'G•ö–BÒW0¢dõ"UDDP¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢–bæ÷B&÷s ¢&WGW&âfÇ6P ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'F–W0¢4U@¢7FGW2Òt5D•dRrÀ¢FVÆWFVEög&öÕö6†ææVÅöBÒåTÄÂÀ¢V&Æ—6†VEöÖW76vUö–BÒåTÄÂÀ¢6†ææVÅ÷÷7E÷W&ÂÒåTÄÂÀ¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'G•÷6÷W&6W0¢4U@¢—5ö7F—fRÒE%TRÀ¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDRV&Æ—6…÷VWVP¢4U@¢VWVU÷7FGW2Òu$TE’rÀ¢66†VGVÆVEöf÷"ÒåTÄÂÀ¢V&Æ—6†VEöBÒåTÄÂÀ¢Æ7EöW'&÷"ÒåTÄÂÀ¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢–b7W"ç&÷v6÷VçBÓÒ ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDòV&Æ—6…÷VWVR€¢&÷W'G•ö–BÀ¢VWVU÷7FGW0¢¢dÅTU2‚W2Âu$TE’r¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢DTÄUDRe$ôÒ&÷W'G•÷G&6€¢t„U$R&÷W'G•ö–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&âG'VP  ¦FVb7W'&VçE÷V&Æ—6…÷6WGF–æw5÷FW‡B‚“ ¢Æ–æW2Ò°¢.)©ûˆòŠ­˜m‹¸Í˜]Š}Š¢˜‹˜M¸ÂŠ}˜mŠ­‹MŠ}‹"À¢""À¢Ğ ¢f÷"¶W’ÂÆ&VÂ–âT$Ä•4…õ4UED”äuôÄ$TÅ2æ—FV×2‚“ ¢–b¶W’ÓÒ&6öçF7E÷V÷ÆR# ¢fÇVRÒf÷&ÖEö6öçF7E÷V÷ÆR‚¢VÇ6S ¢fÇVRÒvWEö÷6WGF–ær€¢¶W’À¢-Š­˜m‹¸Í˜R˜m‹MŠı˜r"À¢ ¢–bfÇVR–â„æöæRÂ""“ ¢fÇVRÒ-Š­˜m‹¸Í˜R˜m‹MŠı˜r  ¢Æ–æW2æVæB€¢b'¶Æ&VÇÓ¥Æç·fÇVWÕÆâ ¢ ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2  ¦FVbf–æE÷&÷W'F–W5ö'•÷6÷W&6Uö6öFR‡6÷W&6Uö6öFR“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢æ–BÀ¢æF—7G&–7BÀ¢æ&VöÓ"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢çV&Æ—6†VEöÖW76vUö–BÀ¢çVæ—Eö6öFP¢e$ôÒ&÷W'G•÷6÷W&6W20¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒ2ç&÷W'G•ö–@¢t„U$R2ç6÷W&6U÷Væ—Eö6öFRÒW0¢äB2æ—5ö7F—fRÒE%TP¢äBç7FGW2Òt5D•dRp¢õ$DU"%’æ–BDU40¢"""À¢‡6÷W&6Uö6öFRÂ’À¢ ¢&WGW&â7W"æfWF6†ÆÂ‚   ¢2ÓÓÒU$tTåBÄôôµUt•D„õUB4õU$4R4ôDRcÓÓĞ ¢2ÓÓÒU$tTåBTUTRE$4´”ärÄôôµUcÓÓĞ¦FVb÷'6U÷W&vVçE÷VWVU÷&÷W'G•ö–B‡fÇVR“ ¢""%&WGW&â–çFW&æÂ&÷W'G•ö–Bf÷"–çWG2Æ–¶R‹]˜CVâ"" ¢Æöö·WÒ7G"‡fÇVR÷"""’ç7G&—‚¢–bæ÷BÆöö·W ¢&WGW&âæöæP ¢æ÷&ÖÆ—¦VBÒÆöö·WçG&ç6ÆFR€¢7G"æÖ¶WG&ç2€¢-»»»-»=»M»]»m»}»»ššš-š=šMš]šmš}šš’"À¢##3CScsƒ“#3CScsƒ’"À¢¢ ¢ÖF6‚Ò&RægVÆÆÖF6‚€¢"%Ç2­‹]˜Ç2¥³®ûÉ¢2ÕÓõÇ2¢…ÆB²•Ç2¢"À¢æ÷&ÖÆ—¦VBÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢&WGW&â–çB†ÖF6‚æw&÷Wƒ’’–bÖF6‚VÇ6RæöæP  ¦FVböf–æE÷W&vVçE÷&÷W'G•ö'•÷VWVU÷G&6¶–ær‡G&6¶–æuöçVÖ&W"“ ¢""%&W6öÇfR‹]˜æ&ö'W7FÇ’à ¢†—7F÷&–6ÆÇ’F†RT’W6VBF†R–çFW&æÂ&÷W'G’–B2F†RVWVRG&6¶–æp¢çVÖ&W"†‹]˜Ç&÷W'G•ö–Cæ’â6öÖR÷W&F÷'26âÇ6òVæ6÷VçFW"F†P¢‡—6–6ÂV&Æ—6…÷VWVRæ–Fâ&VfW"F†R†—7F÷&–6Â&÷W'G’Ö–BÖVæ–ærÀ¢F†VâfÆÂ&6²FòVWVR×&÷r–BâV&Æ—6†VB&÷w2&R–çFVçF–öæÆÇ’ÆÆ÷vV@¢6òF†RW&vVçBfÆ÷r6â6’&Ç&VG’V&Æ—6†VB"–ç7FVBöbfÇ6VÇ’6––æp¢&f–ÆRæ÷Bf÷VæB"à¢"" ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢2&–Ö'’ÖVæ–ærW6VB'’Db÷6V&6‚T“¢‹]˜Ç&÷W'G•ö–Câà¢7W"æW†V7WFR€¢"" ¢4TÄT5BD•5D”ä5@¢æ–BÀ¢æF—7G&–7BÀ¢æ&VöÓ"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢çV&Æ—6†VEöÖW76vUö–BÀ¢çVæ—Eö6öFP¢e$ôÒ&÷W'F–W2 ¢t„U$Ræ–BÒW0¢äBç7FGW2Òt5D•dRp¢äBæFVÆWFVEög&öÕö6†ææVÅöB•2åTÄÀ¢äBU„•5E2€¢4TÄT5B¢e$ôÒV&Æ—6…÷VWVR¢t„U$Rç&÷W'G•ö–BÒæ–@¢¢Ä”Ô•B¢"""À¢‡G&6¶–æuöçVÖ&W"Â’À¢¢&÷w2Ò7W"æfWF6†ÆÂ‚¢–b&÷w3 ¢&WGW&â&÷w0 ¢2FVfVç6—fRfÆÆ&6³¢66WBF†R7GVÂV&Æ—6…÷VWVR&÷r–BFöòà¢7W"æW†V7WFR€¢"" ¢4TÄT5BD•5D”ä5@¢æ–BÀ¢æF—7G&–7BÀ¢æ&VöÓ"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢çV&Æ—6†VEöÖW76vUö–BÀ¢çVæ—Eö6öFP¢e$ôÒV&Æ—6…÷VWVR¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒç&÷W'G•ö–@¢t„U$Ræ–BÒW0¢äBç7FGW2Òt5D•dRp¢äBæFVÆWFVEög&öÕö6†ææVÅöB•2åTÄÀ¢Ä”Ô•B¢"""À¢‡G&6¶–æuöçVÖ&W"Â’À¢¢&WGW&â7W"æfWF6†ÆÂ‚  ¦FVbf–æE÷&÷W'F–W5öf÷%÷W&vVçEöÆöö·W‡fÇVR“ ¢"" ¢&ö'W7BW&vVçBÆöö·W66WG3 ¢’VWVRG&6¶–ærçVÖ&W"7V6‚2‹]˜CV ¢"’6÷W&6RVæ—Böf–ÆR6öFRÂ–æ6ÇVF–ærW'6–âô&&–2F–v—G0¢2’BæÖR6÷W&6R÷7BU$ÂÂ–æ6ÇVF–ær&—fFRö2òÆ–æ·0¢B’6÷W&6RFVÆVw&ÒÖW76vR”@¢R’FW7F–æF–öâVæ—B6öFR2Æ7B×&W6÷'BfÆÆ&6° ¢&WGW&â6†RÖF6†W2f–æE÷&÷W'F–W5ö'•÷6÷W&6Uö6öFR‚’à¢""  ¢Æöö·WÒ7G"‡fÇVR÷"""’ç7G&—‚ ¢–bæ÷BÆöö·W ¢&WGW&âµĞ ¢VWVU÷&÷W'G•ö–BÒ÷'6U÷W&vVçE÷VWVU÷&÷W'G•ö–B€¢Æöö·W ¢ ¢–bVWVU÷&÷W'G•ö–B—2æ÷BæöæS ¢&WGW&âöf–æE÷W&vVçE÷&÷W'G•ö'•÷VWVU÷G&6¶–ær€¢VWVU÷&÷W'G•ö–@¢ ¢F–v—EöÖÒ7G"æÖ¶WG&ç2€¢-»»»-»=»M»]»m»}»»ššš-š=šMš]šmš}šš’"À¢##3CScsƒ“#3CScsƒ’"À¢ ¢æ÷&ÖÆ—¦VBÒÆöö·WçG&ç6ÆFR€¢F–v—EöÖ ¢’ç7G&—‚ ¢266WBÆ&VÇ27V6‚2*½ªŠò˜Š}¸Í˜B33Œ+²Â'WB¶VW&&RçVÖ&W ¢2fÆ–B2&÷F‚6÷W&6R6öFRæBÖW76vR–BfÆÆ&6²à¢6öFUöÖF6‚Ò&RægVÆÆÖF6‚€¢""ƒó­ªŠõÇ2¢ƒó­˜Š}¸Í˜GÍ˜Š}ŠİŠ÷Í˜]˜Mª—ÍŠ-ªı˜}¸Â•Ç2¥³®ûÉ¥ÂÕÓõÇ2¢“ò…ÆB²’"À¢æ÷&ÖÆ—¦VBÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢æ÷&ÖÆ—¦VEö6öFRÒ€¢6öFUöÖF6‚æw&÷Wƒ¢–b6öFUöÖF6€¢VÇ6RæöæP¢ ¢26÷W&6R6öFR×W7B&RG&–VBeDU"F–v—Bæ÷&ÖÆ—¦F–öââöÆFW"6öFP¢2G&–VBF†R&rW'6–âF–v—G2f—'7BÂF†Vâ66–FVçFÆÇ’G&VFVBF†P¢2æ÷&ÖÆ—¦VBfÇVRöæÇ’2FVÆVw&ÒÖW76vR–Bà¢–bæ÷&ÖÆ—¦VEö6öFS ¢&÷w2Òf–æE÷&÷W'F–W5ö'•÷6÷W&6Uö6öFR€¢æ÷&ÖÆ—¦VEö6öFP¢ ¢–b&÷w3 ¢&WGW&â&÷w0 ¢FVb÷'VåöÆöö·W‡v†W&U÷7ÂÂ&×2“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢b"" ¢4TÄT5BD•5D”ä5@¢æ–BÀ¢æF—7G&–7BÀ¢æ&VöÓ"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢çV&Æ—6†VEöÖW76vUö–BÀ¢çVæ—Eö6öFP¢e$ôÒ&÷W'F–W2 ¢ÄTeB¤ô”â&÷W'G•÷6÷W&6W20¢ôâ2ç&÷W'G•ö–BÒæ–@¢ÄTeB¤ô”â6†ææVÇ20¢ôâ2æ–BÒ2ç6÷W&6Uö6†ææVÅö–@¢t„U$Rç7FGW2Òt5D•dRp¢äBæFVÆWFVEög&öÕö6†ææVÅöB•2åTÄÀ¢äB‡·v†W&U÷7ÇÒ¢õ$DU"%’æ–BDU40¢"""À¢&×2À¢¢&WGW&â7W"æfWF6†ÆÂ‚ ¢6ÆVå÷W&ÂÒæöæP¢ÖW76vUö–BÒæöæP¢V&Æ–5÷W6W&æÖRÒæöæP¢FVÆVw&Õö6†ææVÅö–BÒæöæP ¢W&ÅöÖF6‚Ò&Rç6V&6‚€¢""ƒó¦‡GG3ó¢òò“òƒó§wwuÂâ“÷EÂæÖRò ¢""ƒó¢ƒó¦2ò…ÆB²’—Â…´Õ¦×£Ó•õÒ²’’ò ¢""…ÆB²’"À¢æ÷&ÖÆ—¦VBÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bW&ÅöÖF6ƒ ¢&—fFUö6†ææVÅ÷'BÒW&ÅöÖF6‚æw&÷Wƒ¢V&Æ–5÷W6W&æÖRÒW&ÅöÖF6‚æw&÷Wƒ"¢ÖW76vUö–BÒ–çB‡W&ÅöÖF6‚æw&÷Wƒ2’ ¢6ÆVå÷W&ÂÒ&Rç7V"€¢"%³ò5Òâ¢B"À¢""À¢æ÷&ÖÆ—¦VBÀ¢’ç'7G&—‚"ò" ¢–bæ÷B6ÆVå÷W&Âç7F'G7v—F‚€¢‚&‡GG¢òò"Â&‡GG3¢òò"¢“ ¢6ÆVå÷W&ÂÒ&‡GG3¢òò"²6ÆVå÷W&À ¢–b&—fFUö6†ææVÅ÷'C ¢FVÆVw&Õö6†ææVÅö–BÒ–çB€¢"Ó"²&—fFUö6†ææVÅ÷'@¢ ¢VÆ–bæ÷&ÖÆ—¦VEö6öFS ¢2W†7B6÷W&6RÖ6öFRÆöö·W&÷fRÇ&VG’f–ÆVBâ&&RçVÖ&W"6à¢2Ç6ò&RF†R6÷W&6RFVÆVw&ÒÖW76vR–Bà¢ÖW76vUö–BÒ–çB†æ÷&ÖÆ—¦VEö6öFR ¢27FvR¢7G&–7B6÷W&6RU$Âò6†ææVÂÖv&RFVÆVw&ÒÆöö·Wà¢6ÆW6W2ÒµĞ¢&×2ÒµĞ ¢–b6ÆVå÷W&Ã ¢6ÆW6W2æVæB€¢"" ¢&VvW‡÷&WÆ6R€¢7Æ—E÷'B€¢4ôÄU44R‡2ç6÷W&6U÷÷7E÷W&ÂÂrr’À¢sòrÀ¢¢’À¢rò²BrÀ¢rp¢’ÒW0¢"" ¢¢&×2æVæB†6ÆVå÷W&Â ¢–b€¢ÖW76vUö–B—2æ÷BæöæP¢æBFVÆVw&Õö6†ææVÅö–B—2æ÷BæöæP¢“ ¢6ÆW6W2æVæB€¢"" ¢€¢2ç6÷W&6UöÖW76vUö–BÒW0¢äB2çFVÆVw&Õö6†ææVÅö–BÒW0¢¢"" ¢¢&×2æW‡FVæB…°¢ÖW76vUö–BÀ¢FVÆVw&Õö6†ææVÅö–BÀ¢Ò ¢–b€¢ÖW76vUö–B—2æ÷BæöæP¢æBV&Æ–5÷W6W&æÖP¢“ ¢6ÆW6W2æVæB€¢"" ¢€¢2ç6÷W&6UöÖW76vUö–BÒW0¢äBÄõtU"€¢$UÄ4R€¢4ôÄU44R†2çW6W&æÖRÂrr’À¢trÀ¢rp¢¢’ÒW0¢¢"" ¢¢&×2æW‡FVæB…°¢ÖW76vUö–BÀ¢V&Æ–5÷W6W&æÖRæÆ÷vW"‚’À¢Ò ¢–b6ÆW6W3 ¢&÷w2Ò÷'VåöÆöö·W€¢"õ""æ¦ö–â€¢b"‡¶6ÆW6WÒ’ ¢f÷"6ÆW6R–â6ÆW6W0¢’À¢&×2À¢¢–b&÷w3 ¢&WGW&â&÷w0 ¢27FvR#¢ÖW76vRÖ–BfÆÆ&6²âF†—2–çFVçF–öæÆÇ’&WGW&ç2×VÇF—ÆP¢26æF–FFW2v†VâF†R6÷W&6RÖ6†ææVÂÖ–ær—2–æ6ö×ÆWFRÂ6òF†P¢2÷W&F÷"6â6VÆV7B–ç7FVBöbvWGF–ærfÇ6R(	Ææ÷Bf÷VæN(	Òà¢–bÖW76vUö–B—2æ÷BæöæS ¢&÷w2Ò÷'VåöÆöö·W€¢"" ¢2ç6÷W&6UöÖW76vUö–BÒW0¢õ"æ–çWEöÖW76vUö–BÒW0¢õ"4ôÄU44R‡2ç6÷W&6U÷÷7E÷W&ÂÂrr’âW0¢"""À¢°¢ÖW76vUö–BÀ¢ÖW76vUö–BÀ¢&b"÷¶ÖW76vUö–GÒƒó¥²óò5Òâ¢“òB"À¢ÒÀ¢¢–b&÷w3 ¢&WGW&â&÷w0 ¢27FvR3¢–b6÷W&6R6öFRv2f—6–&ÆR–â–×÷'FVB÷&rFW‡B'WBà¢2öÆB&÷rÖ—76VB6÷W&6U÷Væ—Eö6öFRÂ&V6÷fW"—Bg&öÒÆ&VÆÆVB&rFW‡Bà¢–bæ÷&ÖÆ—¦VEö6öFS ¢&uö6öFU÷GFW&âÒ€¢"-ªŠõµ³§76S¥Ş(ÅÒ¢ƒó­˜Š}¸Í˜GÍ˜Š}ŠİŠ÷Í˜]˜Mª—ÍŠ-ªı˜}¸Â’ ¢"%µ³§76S¥ÕÒ¥³®ûÉ¥ÂÕÓõµ³§76S¥ÕÒ¢ ¢²&RæW66R†æ÷&ÖÆ—¦VEö6öFR¢²""…µãÓ•×ÂB’ ¢ ¢&÷w2Ò÷'VåöÆöö·W€¢"" ¢G&ç6ÆFR€¢4ôÄU44R‡2ç6÷W&6U÷&u÷FW‡BÂrr’À¢}»»»-»=»M»]»m»}»»ššš-š=šMš]šmš}šš’rÀ¢s#3CScsƒ“#3CScsƒ’p¢’â¢W0¢õ"G&ç6ÆFR€¢4ôÄU44R‡ç&u÷FW‡BÂrr’À¢}»»»-»=»M»]»m»}»»ššš-š=šMš]šmš}šš’rÀ¢s#3CScsƒ“#3CScsƒ’p¢’â¢W0¢"""À¢°¢&uö6öFU÷GFW&âÀ¢&uö6öFU÷GFW&âÀ¢ÒÀ¢¢–b&÷w3 ¢&WGW&â&÷w0 ¢2f–æÂ÷W&F÷"Ög&–VæFÇ’fÆÆ&6³¢FW7F–æF–öâVæ—B6öFRà¢&÷w2Ò÷'VåöÆöö·W€¢'çVæ—Eö6öFRÒW2"À¢¶æ÷&ÖÆ—¦VEö6öFUÒÀ¢¢–b&÷w3 ¢&WGW&â&÷w0 ¢&WGW&âµĞ  ¦FVb&WV&Æ—6…ö6öæf—&Õö¶W–&ö&B‡&÷W'G•ö–B“ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈRŠŠ}‹-˜m‹M‹ŠŠr˜}˜]¸Í˜bªŠò"À¢6ÆÆ&6µöFFÖb'&WV&Æ—6…ö6öæf—&Õ÷·&÷W'G•ö–GÒ"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂŠ}˜m‹]‹Š}˜"À¢6ÆÆ&6µöFFÒ&†öÖR"À¢¢ÒÀ¢Ğ¢   ¦FVbvWE÷Væ—Eö6öFUögVÆÅöFWF–Ç2‡Væ—Eö6öFR“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢æ–BÀ¢çVæ—Eö6öFRÀ¢æF—7G&–7BÀ¢æFG&W75÷FW‡BÀ¢æ&VöÓ"À¢æ&VG&öö×2À¢æ'V–ÆE÷–V"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢ç7FGW2À¢çV&Æ—6†VEöÖW76vUö–BÀ¢æ6†ææVÅ÷÷7E÷W&À¢e$ôÒ&÷W'F–W2 ¢t„U$RçVæ—Eö6öFRÒW0¢Ä”Ô•B¢"""À¢‡Væ—Eö6öFRÂ’À¢ ¢&÷W'G•÷&÷rÒ7W"æfWF6†öæR‚ ¢–bæ÷B&÷W'G•÷&÷s ¢&WGW&âæöæRÂµĞ ¢&÷W'G•ö–BÒ&÷W'G•÷&÷u³Ğ ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢2ç6÷W&6U÷Væ—Eö6öFRÀ¢2ç6÷W&6UöÖW76vUö–BÀ¢2æ—5ö7F—fRÀ¢2çF—FÆRÀ¢2çW6W&æÖRÀ¢2çFVÆVw&Õö6†ææVÅö–BÀ¢2ç6÷W&6U÷÷7E÷W&À¢e$ôÒ&÷W'G•÷6÷W&6W20¢ÄTeB¤ô”â6†ææVÇ20¢ôâ2æ–BÒ2ç6÷W&6Uö6†ææVÅö–@¢t„U$R2ç&÷W'G•ö–BÒW0¢õ$DU"%¢2æ—5÷&–Ö'’DU42À¢2æ–B40¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢6÷W&6U÷&÷w2Ò7W"æfWF6†ÆÂ‚ ¢&WGW&â&÷W'G•÷&÷rÂ6÷W&6U÷&÷w0  ¦FVbvWE÷&÷W'G•öGG&–'WF–öåöFWF–Â‡&÷W'G•ö–B“ ¢""$ÖævVÖVçBÖöæÇ’GG&–'WF–öâf÷"&÷W'G’à ¢ÖçVÂ7Ffbf–ÆW2&RÆ–æ¶VBF‡&÷Vv‚&÷W'F–W2æÖçVÅö7&VFVEö'•÷W6W%ö–Bà¢6†„f–ÆR6VÆÆW"f–ÆW27F’Æ–æ¶VBFò6VÆÆW%÷7V&Ö—76–öç26ò÷væW"÷&—fFRFF¢FöW2æ÷BæVVBFò&R6÷–VB–çFòF†RV&Æ–2&÷W'G’–ÆöBà¢"" ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢ç6÷W&6Uö¶–æBÀ¢æÖçVÅö7&VFVEö'•÷W6W%ö–BÀ¢RæF—7Æ•öæÖRÀ¢RçW6W&æÖRÀ¢Rç&öÆRÀ¢€¢4TÄT5BW2çFVÆVw&Õ÷W6W%ö–@¢e$ôÒW6W%÷6W76–öç2W0¢t„U$RW2çW6W%ö–BÒRæ–@¢õ$DU"%’W2æÆ7Eö7F—f—G•öBDU42ÂW2æ–BDU40¢Ä”Ô•B¢’2&Vv—7G&%÷FVÆVw&Õö–@¢e$ôÒ&÷W'F–W2 ¢ÄTeB¤ô”âW6W'2P¢ôâRæ–BÒæÖçVÅö7&VFVEö'•÷W6W%ö–@¢t„U$Ræ–BÒW0¢Ä”Ô•B¢"""À¢‡&÷W'G•ö–BÂ’À¢¢&6RÒ7W"æfWF6†öæR‚ ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢72æ–BÀ¢72æ÷væW%öæÖRÀ¢72æ÷væW%÷†öæRÀ¢72ç&—fFUöFG&W72À¢72çFVÆVw&Õ÷W6W%ö–BÀ¢72ç7FGW2À¢RæF—7Æ•öæÖRÀ¢RçW6W&æÖP¢e$ôÒ6VÆÆW%÷7V&Ö—76–öç270¢ÄTeB¤ô”âW6W'2P¢ôâRæ–BÒ72æ&÷fVEö'•÷W6W%ö–@¢t„U$R72ç&÷W'G•ö–BÒW0¢õ$DU"%’72æ–BDU40¢Ä”Ô•B¢"""À¢‡&÷W'G•ö–BÂ’À¢¢6VÆÆW"Ò7W"æfWF6†öæR‚ ¢–bæ÷B&6RæBæ÷B6VÆÆW# ¢&WGW&âæöæP ¢&W7VÇBÒ°¢'6÷W&6Uö¶–æB#¢&6U³Ò–b&6RVÇ6RæöæRÀ¢'&Vv—7G&%÷W6W%ö–B#¢&6U³Ò–b&6RVÇ6RæöæRÀ¢'&Vv—7G&%öF—7Æ•öæÖR#¢&6U³%Ò–b&6RVÇ6RæöæRÀ¢'&Vv—7G&%÷W6W&æÖR#¢&6U³5Ò–b&6RVÇ6RæöæRÀ¢'&Vv—7G&%÷&öÆR#¢&6U³EÒ–b&6RVÇ6RæöæRÀ¢'&Vv—7G&%÷FVÆVw&Õö–B#¢&6U³UÒ–b&6RVÇ6RæöæRÀ¢'6VÆÆW"#¢æöæRÀ¢Ğ ¢–b6VÆÆW# ¢&W7VÇE²'6VÆÆW"%ÒÒ°¢'7V&Ö—76–öåö–B#¢6VÆÆW%³ÒÀ¢&÷væW%öæÖR#¢6VÆÆW%³ÒÀ¢&÷væW%÷†öæR#¢6VÆÆW%³%ÒÀ¢'&—fFUöFG&W72#¢6VÆÆW%³5ÒÀ¢'FVÆVw&Õ÷W6W%ö–B#¢6VÆÆW%³EÒÀ¢'7FGW2#¢6VÆÆW%³UÒÀ¢&&÷fVEö'’#¢6VÆÆW%³eÒ÷"6VÆÆW%³uÒÀ¢Ğ ¢&WGW&â&W7VÇ@  ¦FVb'V–ÆE÷Væ—Eö6öFU÷6V&6…÷FW‡B€¢&÷W'G•÷&÷rÀ¢6÷W&6U÷&÷w2À¢6W76–öãÔæöæRÀ¢“ ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&VG&öö×2À¢'V–ÆE÷–V"À¢&–6Uö&–ÆÆ–öâÀ¢7FGW2À¢V&Æ—6†VEöÖW76vUö–BÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢’Ò&÷W'G•÷&÷p ¢7FGW5öæÖW2Ò°¢$5D•dR#¢-˜‹Š}˜B)ÈR"À¢%4ôÄB#¢-˜‹˜ŠíŠ­˜r‹MŠı˜r	ùKB"À¢$”ä5D•dR#¢-‹­¸Í‹˜‹Š}˜B)¹B"À¢$”ådÄ”B#¢-˜mŠ}˜]‹Š­Š‹)ªûˆò"À¢$EUÄ”4DR#¢-Š­ª‹Š}‹¸Â	úz’"À¢Ğ ¢Æ–æW2Ò°¢b/	ùHâŠ}‹}˜MŠ}‹Š}Š¢ªŠò˜Š}ŠİŠò·Væ—Eö6öFWÒ"À¢""À¢Ğ ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-Š½ŠŠ¢˜m‹MŠı˜r ¢ ¢Æ–æW2æVæB€¢b/	ù8Ò¶Æö6F–öçÒ ¢ ¢–b&VöÓ"—2æ÷BæöæS ¢&VÒfÆöB†&VöÓ" ¢–b&Væ—5ö–çFVvW"‚“ ¢&VÒ–çB†&V ¢Æ–æW2æVæB€¢b/	ù9¶&VÒ˜]Š­‹ ¢ ¢–b&VG&öö×2—2æ÷BæöæS ¢Æ–æW2æVæB€¢b/	ù¸ò¶&VG&öö×7ÒŠí˜Š}Š‚ ¢ ¢–b'V–ÆE÷–V# ¢Æ–æW2æVæB€¢b/	ù8R‹=Š}˜B‹=Š}ŠíŠ¢¶'V–ÆE÷–V'Ò ¢ ¢–b&–6Uö&–ÆÆ–öâ—2æ÷BæöæS ¢&–6RÒfÆöB‡&–6Uö&–ÆÆ–öâ ¢–b&–6Ræ—5ö–çFVvW"‚“ ¢&–6U÷FW‡BÒ7G"†–çB‡&–6R’¢VÇ6S ¢&–6U÷FW‡BÒ€¢b'·&–6S¢ã6gÒ ¢ç'7G&—‚#"¢ç'7G&—‚"â"¢ ¢Æ–æW2æVæB€¢b/	ù+˜-¸Í˜]Š£¢·&–6U÷FW‡GÒ˜]¸Í˜M¸ÍŠ}‹ŠòŠ­˜˜]Š}˜b ¢ ¢Æ–æW2æVæB€¢/	ù8Â˜‹m‹¸ÍŠ£¢ ¢²7FGW5öæÖW2ævWB€¢7FGW2À¢7FGW2÷"-˜mŠ}˜]‹MŠí‹R"À¢¢ ¢–b6†ææVÅ÷÷7E÷W&Ã ¢Æ–æW2æW‡FVæB€¢°¢""À¢/	ù:"›í‹=Š¢˜]˜mŠ­‹M‹‹MŠı˜rŠı‹ªŠ}˜mŠ}˜B˜]Šs¢"À¢6†ææVÅ÷÷7E÷W&ÂÀ¢Ğ¢ ¢GG&–'WF–öâÒvWE÷&÷W'G•öGG&–'WF–öåöFWF–Â‡&÷W'G•ö–B¢–bGG&–'WF–öã ¢6÷W&6Uö¶–æBÒ†GG&–'WF–öâævWB‚'6÷W&6Uö¶–æB"’÷"""’çWW"‚¢6VÆÆW"ÒGG&–'WF–öâævWB‚'6VÆÆW""¢&Vv—7G&%öæÖRÒ€¢GG&–'WF–öâævWB‚'&Vv—7G&%öF—7Æ•öæÖR"¢÷"GG&–'WF–öâævWB‚'&Vv—7G&%÷W6W&æÖR"¢ ¢–b6VÆÆW"÷"6÷W&6Uö¶–æB–â²$ÔåTÂ"Â$5U5DôÔU"'Ò÷"&Vv—7G&%öæÖS ¢Æ–æW2æW‡FVæB…²""Â.)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H"Â/	ùR˜]Š}˜Mª¸ÍŠ¢òŠ½ŠŠ¢˜Š}¸Í˜B%Ò ¢–b6VÆÆW"÷"6÷W&6Uö¶–æBÓÒ$5U5DôÔU"# ¢Æ–æW2æVæB‚/	ù:R˜]‹=¸Í‹˜‹˜Šó¢˜‹˜‹M˜mŠı˜rŠ}‹"6†„f–ÆR"¢–b6VÆÆW# ¢Æ–æW2æVæB†b/	øiB7V&Ö—76–öã¢·6VÆÆW"ævWB‚w7V&Ö—76–öåö–Br’÷"rÒwÒ"¢–b6VÆÆW"ævWB‚&&÷fVEö'’"“ ¢Æ–æW2æVæB†b.)ÈRŠ­Š=¸Í¸ÍŠıª˜m˜mŠı˜s¢·6VÆÆW%²v&÷fVEö'’u×Ò" ¢f–WvW%÷&öÆRÒ6W76–öå³5Ò–b6W76–öâæBÆVâ‡6W76–öâ’â2VÇ6RæöæP¢–bf–WvW%÷&öÆR–â‚$õtäU""Â$DÔ”â"“ ¢Æ–æW2æVæB†b/	ùB˜]Š}˜Mª“¢·6VÆÆW"ævWB‚v÷væW%öæÖRr’÷"rÒwÒ"¢Æ–æW2æVæB†b/	ù9â‹M˜]Š}‹˜r˜]Š}˜Mª“¢·6VÆÆW"ævWB‚v÷væW%÷†öæRr’÷"rÒwÒ"¢Æ–æW2æVæB†b/	ù8ÂŠ-Šı‹‹2Ší‹]˜‹]¸Ã¢·6VÆÆW"ævWB‚w&—fFUöFG&W72r’÷"rÒwÒ"¢Æ–æW2æVæB†b/	øiBFVÆVw&Ò˜‹˜‹M˜mŠı˜s¢·6VÆÆW"ævWB‚wFVÆVw&Õ÷W6W%ö–Br’÷"rÒwÒ"¢VÇ6S ¢Æ–æW2æVæB‚/	ùIŠ}‹}˜MŠ}‹Š}Š¢Ší‹]˜‹]¸Â˜]Š}˜Mª’˜˜-‹rŠ‹Š}¸Â˜]Šı¸Í‹Š}˜b˜m˜]Š}¸Í‹BŠıŠ}Šı˜r˜]¸Î(Í‹M˜Šòâ" ¢VÆ–b6÷W&6Um«ëŒ+Š×®º+º$zzb¥ïkind == "MANUAL" or registrar_name:
            role_names = {
                "OWNER": "Ù…Ø¯ÛŒØ± Ø§ØµÙ„ÛŒ",
                "ADMIN": "Ø§Ø¯Ù…ÛŒÙ†",
                "ADVISOR": "Ù…Ø´Ø§ÙˆØ±",
                "USER": "Ú©Ø§Ø±Ø¨Ø±",
            }
            lines.append("ğŸ“¥ Ù…Ø³ÛŒØ± ÙˆØ±ÙˆØ¯: Ø§ÙØ²ÙˆØ¯Ù† ÙØ§ÛŒÙ„ Ù…Ø¯ÛŒØ±ÛŒØªÛŒ")
            lines.append(
                "ğŸ§‘â€ğŸ’¼ Ø«Ø¨Øªâ€ŒÚ©Ù†Ù†Ø¯Ù‡/Ù…Ø´Ø§ÙˆØ±: "
                + (registrar_name or "Ù†Ø§Ù…Ø´Ø®Øµ")
            )
            if attribution.get("registrar_role"):
                lines.append(
                    "ğŸ”‘ Ù†Ù‚Ø´: "
                    + role_names.get(
                        attribution.get("registrar_role"),
                        attribution.get("registrar_role"),
                    )
                )
            if attribution.get("registrar_telegram_id"):
                lines.append(
                    f"ğŸ†” Telegram ID Ø«Ø¨Øªâ€ŒÚ©Ù†Ù†Ø¯Ù‡: {attribution['registrar_telegram_id']}"
                )

    if source_rows:
        lines.extend(
            [
                "",
                "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”",
                "ğŸ“¡ Ù…Ù†Ø¨Ø¹ ÙØ§ÛŒÙ„",
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
                    f"\nÙ…Ù†Ø¨Ø¹ {index}:"
                )

            lines.append(
                "ğŸ· Ú©Ø§Ù†Ø§Ù„: "
                + (
                    channel_title
                    or "Ù†Ø§Ù…Ø´Ø®Øµ"
                )
            )

            if channel_username:
                username = str(
                    channel_username
                ).lstrip("@")

                lines.append(
                    f"ğŸ‘¤ @{username}"
                )

            lines.append(
                "ğŸ”¢ Ú©Ø¯ ÙØ§ÛŒÙ„ Ø¯Ø± Ù…Ù†Ø¨Ø¹: "
                + (
                    str(source_unit_code)
                    if source_unit_code
                    else "Ù†Ø¯Ø§Ø±Ø¯"
                )
            )

            if source_message_id:
                lines.append(
                    f"âœ‰ï¸ Message ID Ù…Ù†Ø¨Ø¹: {source_message_id}"
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
                        "ğŸ”— Ù…Ø´Ø§Ù‡Ø¯Ù‡ Ù¾Ø³Øª Ù…Ù†Ø¨Ø¹:",
                        final_source_url,
                    ]
                )
            else:
                lines.append(
                    "ğŸ”— Ù„ÛŒÙ†Ú© Ù…Ø³ØªÙ‚ÛŒÙ… Ù…Ù†Ø¨Ø¹ Ø¯Ø± Ø¯Ø³ØªØ±Ø³ Ù†ÛŒØ³Øª."
                )

            lines.append(
                "ÙˆØ¶Ø¹ÛŒØª Ù…Ù†Ø¨Ø¹: "
                + (
                    "ÙØ¹Ø§Ù„ âœ…"
                    if source_is_active
                    else "ØºÛŒØ±ÙØ¹Ø§Ù„ â›”"
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
                "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”",
                "ğŸ“¡ Ù…Ù†Ø¨Ø¹ ÙØ§ÛŒÙ„",
            ]
        )

        if source:
            source_name = (
                source.get("source_name")
                or "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
            )

            source_code = source.get(
                "source_unit_code"
            )

            lines.append(
                f"ğŸ· Ù…Ù†Ø¨Ø¹: {source_name}"
            )

            lines.append(
                "ğŸ”¢ Ú©Ø¯ ÙØ§ÛŒÙ„ Ø¯Ø± Ù…Ù†Ø¨Ø¹: "
                + (
                    str(source_code)
                    if source_code
                    else "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
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
                        "ğŸ”— Ù…Ø´Ø§Ù‡Ø¯Ù‡ Ù¾Ø³Øª Ù…Ù†Ø¨Ø¹:",
                        direct_url,
                    ]
                )

            elif channel_url:
                lines.extend(
                    [
                        "ğŸ”— ÙˆØ±ÙˆØ¯ Ø¨Ù‡ Ú©Ø§Ù†Ø§Ù„ Ù…Ù†Ø¨Ø¹:",
                        channel_url,
                    ]
                )

            else:
                lines.append(
                    "ğŸ”— Ù„ÛŒÙ†Ú© Ù…Ù†Ø¨Ø¹ Ø¯Ø± Ø¯Ø³ØªØ±Ø³ Ù†ÛŒØ³Øª."
                )

        else:
            lines.append(
                "âš ï¸ Ù…Ù†Ø¨Ø¹ Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ù‚Ø§Ø¨Ù„ ØªØ´Ø®ÛŒØµ Ù†ÛŒØ³Øª."
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
                    "âœ… Ù‡Ù…ÛŒÙ† Ø§Ù„Ø§Ù† Ù…Ù†ØªØ´Ø± Ø´ÙˆØ¯",
                    callback_data=f"urgent_confirm_{property_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "âŒ Ø§Ù†ØµØ±Ø§Ù",
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
                    "ğŸ‘ Ù…Ø´Ø§Ù‡Ø¯Ù‡ ÙØ§ÛŒÙ„ Ù…Ø´Ø§Ø¨Ù‡",
                    url=similar_url,
                )
            ]
        )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "âœ… Ø¨Ù„Ù‡ØŒ Ù…Ù†ØªØ´Ø± Ú©Ù†",
                    callback_data=(
                        f"urgent_dup_approve_"
                        f"{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "âŒ Ø®ÛŒØ±ØŒ Ù…Ù†ØªØ´Ø± Ù†Ø´ÙˆØ¯",
                    callback_data=(
                        f"urgent_dup_reject_"
                        f"{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª",
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
        "âš ï¸ Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ø§Ø­ØªÙ…Ø§Ù„Ø§Ù‹ ØªÚ©Ø±Ø§Ø±ÛŒ Ø§Ø³Øª.",
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

        lines.append("ÙØ§ÛŒÙ„ Ù…Ø´Ø§Ø¨Ù‡ Ù‚Ø¨Ù„ÛŒ:")

        if source_code:
            lines.append(
                f"Ú©Ø¯ ÙØ§ÛŒÙ„ Ù…Ù†Ø¨Ø¹: {source_code}"
            )

        if unit_code:
            lines.append(
                f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {unit_code}"
            )

        location = (
            address_text
            or district
            or "Ø¨Ø¯ÙˆÙ† Ø¢Ø¯Ø±Ø³"
        )

        lines.append(
            f"ğŸ“ {location}"
        )

        if area_m2 is not None:
            area = float(area_m2)

            if area.is_integer():
                area = int(area)

            lines.append(
                f"ğŸ“ {area} Ù…ØªØ±"
            )

        if price_billion is not None:
            price = float(price_billion)
            price_text = (
                f"{price:.3f}"
                .rstrip("0")
                .rstrip(".")
            )

            lines.append(
                f"ğŸ’° {price_text} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯"
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
                f"Ø¯Ø±Ø¬Ù‡ Ø´Ø¨Ø§Ù‡Øª: {score} Ø§Ø² 100",
            ]
        )

    if reason:
        lines.append(
            f"Ø¯Ù„ÛŒÙ„: {reason}"
        )

    lines.extend(
        [
            "",
            "Ø¢ÛŒØ§ Ø¨Ø§ Ø§ÛŒÙ† Ø­Ø§Ù„ Ù‡Ù…ÛŒÙ† ÙØ§ÛŒÙ„ Ù…Ù†ØªØ´Ø± Ø´ÙˆØ¯ØŸ",
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

        district_text = district or "Ø¨Ø¯ÙˆÙ† Ù…Ù†Ø·Ù‚Ù‡"

        if area_m2 is not None:
            area_text = str(int(area_m2))
        else:
            area_text = "ØŸ"

        if price_billion is not None:
            price_text = str(float(price_billion)).rstrip("0").rstrip(".")
        else:
            price_text = "ØŸ"

        buttons.append(
            [
                InlineKeyboardButton(
                    f"{district_text} | {area_text}Ù… | {price_text} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯",
                    callback_data=f"urgent_select_{property_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª",
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
        "Û°Û±Û²Û³Û´ÛµÛ¶Û·Û¸Û¹Ù Ù¡Ù¢Ù£Ù¤Ù¥Ù¦Ù§Ù¨Ù©",
        "01234567890123456789",
    )

    return str(text).translate(table)


def parse_category_range(text):
    text = normalize_category_digits(text)

    text = (
        text
        .replace("Ø§Ù„ÛŒ", "ØªØ§")
        .replace("â€“", "-")
        .replace("â€”", "-")
    )

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:ØªØ§|-)\s*(\d+(?:\.\d+)?)",
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
        f"#Ø¢Ù¾Ø§Ø±ØªÙ…Ø§Ù†_{low_text}_{high_text}"
    )



def normalize_category_location(value):
    if not value:
        return ""

    text = str(value).strip().lower()

    text = (
        text
        .replace("|", " ")
        .replace("-", " ")
        .replace("Ù€", " ")
        .replace("ØŒ", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("Ø›", " ")
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
                        f"ğŸ”¥ {low_text} ØªØ§ {high_text} "
                        f"Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯ â€” {count} ÙØ§ÛŒÙ„"
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
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª",
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
                    "ğŸ“¤ Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ø¨Ù‡ Ú©Ø§Ù†Ø§Ù„",
                    callback_data="category_page_0",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª",
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
                "â—€ï¸ ØµÙØ­Ù‡ Ù‚Ø¨Ù„",
                callback_data=(
                    f"catnav_{category_post_id}_"
                    f"{previous_offset}"
                ),
            )
        )

    row.append(
        InlineKeyboardButton(
            f"ØµÙØ­Ù‡ {current_page} Ø§Ø² {total_pages}",
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
                "ØµÙØ­Ù‡ Ø¨Ø¹Ø¯ â–¶ï¸",
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
      m«ëŒ+Š×®º+º$zzb¥âvUööfg6WBÀ¢vU÷6—¦RÀ¢FVÆVw&Õö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–BÀ¢—5ö7F—fP¢e$ôÒ6FVv÷'•÷÷7G0¢t„U$R–BÒW0¢Ä”Ô•B¢"""À¢†6FVv÷'•÷÷7Eö–BÂ’À¢ ¢&WGW&â7W"æfWF6†öæR‚  ¦FVbWFFUö6FVv÷'•÷÷7Eööfg6WB€¢6FVv÷'•÷÷7Eö–BÀ¢öfg6WBÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢UDDR6FVv÷'•÷÷7G0¢4U@¢vUööfg6WBÒW2À¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢€¢–çB†öfg6WB’À¢6FVv÷'•÷÷7Eö–BÀ¢’À¢ ¢6öæâæ6öÖÖ—B‚  ¦FVb'V–ÆEö6FVv÷'•÷÷7E÷FW‡B€¢Æ÷rÀ¢†–v‚À¢&÷w2À¢öfg6WCÓÀ¢F÷FÃÔæöæRÀ¢vU÷6—¦SÓÀ¢“ ¢†6‡FrÒ'V–ÆEö6FVv÷'•ö†6‡Fr€¢Æ÷rÀ¢†–v‚À¢ ¢Æ÷u÷FW‡BÒf÷&ÖEö6FVv÷'•öçVÖ&W"†Æ÷r¢†–v…÷FW‡BÒf÷&ÖEö6FVv÷'•öçVÖ&W"††–v‚ ¢Æ–æW2Ò°¢†6‡FrÀ¢""À¢€¢/	øûr˜Š}ŠİŠı˜}Š}¸ÂŠ¸Í˜b˜-¸Í˜]Š¢ ¢b'¶Æ÷u÷FW‡GÒŠ­Šr¶†–v…÷FW‡GÒ ¢-˜]¸Í˜M¸ÍŠ}‹ŠòŠ­˜˜]Š}˜b ¢’À¢""À¢Ğ ¢2Çv—2W6RF†R5U%$TåBFW7F–æF–öâW6W&æÖRà¢2F†—2¶VW26FVv÷'’Æ–æ·2fÆ–BWfVâ–bF†P¢2FW7F–æF–öâ6†ææVÂW6W&æÖR6†ævW2ÆFW"à¢FW7F–æF–öâÒvWEöFW7F–æF–öåö6†ææVÂ‚ ¢7W'&VçEöFW7F–æF–öå÷W6W&æÖRÒæöæP ¢–bFW7F–æF–öã ¢7W'&VçEöFW7F–æF–öå÷W6W&æÖRÒ€¢FW7F–æF–öå³%Ğ¢ ¢–b7W'&VçEöFW7F–æF–öå÷W6W&æÖS ¢7W'&VçEöFW7F–æF–öå÷W6W&æÖRÒ€¢7G"€¢7W'&VçEöFW7F–æF–öå÷W6W&æÖP¢’æÇ7G&—‚$"¢ ¢f÷"–æFW‚Â&÷r–âVçVÖW&FR€¢&÷w2À¢7F'CÖöfg6WB²À¢“ ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&VG&öö×2À¢'V–ÆE÷–V"À¢&–6Uö&–ÆÆ–öâÀ¢&–6U÷FöÖâÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢’Ò&÷p ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-ŠŠı˜˜b˜]˜m‹}˜-˜r ¢ ¢Æ–æW2æVæB€¢b'¶–æFW‡Ò’	ù8Ò¶Æö6F–öçÒ ¢ ¢FWF–Å÷'G2ÒµĞ ¢–b&VöÓ"—2æ÷BæöæS ¢&VÒfÆöB†&VöÓ" ¢–b&Væ—5ö–çFVvW"‚“ ¢&VÒ–çB†&V ¢FWF–Å÷'G2æVæB€¢b/	ù9¶&VÒ˜]Š­‹ ¢ ¢–b&VG&öö×2—2æ÷BæöæS ¢FWF–Å÷'G2æVæB€¢b/	ù¸ò¶&VG&öö×7ÒŠí˜Š}Š‚ ¢ ¢–b'V–ÆE÷–V# ¢FWF–Å÷'G2æVæB€¢b/	ù8R‹=Š}˜B‹=Š}ŠíŠ¢¶'V–ÆE÷–V'Ò ¢ ¢–bFWF–Å÷'G3 ¢Æ–æW2æVæB€¢"Â"æ¦ö–â†FWF–Å÷'G2¢ ¢–b&–6Uö&–ÆÆ–öâ—2æ÷BæöæS ¢Æ–æW2æVæB€¢/	ù+˜-¸Í˜]Š£¢ ¢b'¶f÷&ÖEö6FVv÷'•öçVÖ&W"‡&–6Uö&–ÆÆ–öâ—Ò ¢-˜]¸Í˜M¸ÍŠ}‹ŠòŠ­˜˜]Š}˜b ¢ ¢–bVæ—Eö6öFS ¢Æ–æW2æVæB€¢b/	ùJ"ªŠò˜Š}ŠİŠó¢·Væ—Eö6öFWÒ ¢ ¢f–æÅ÷÷7E÷W&ÂÒ6†ææVÅ÷÷7E÷W&À ¢–b€¢7W'&VçEöFW7F–æF–öå÷W6W&æÖP¢æBV&Æ—6†VEöÖW76vUö–@¢“ ¢f–æÅ÷÷7E÷W&ÂÒ€¢&‡GG3¢ò÷BæÖRò ¢b'¶7W'&VçEöFW7F–æF–öå÷W6W&æÖWÒò ¢b'·V&Æ—6†VEöÖW76vUö–GÒ ¢ ¢–bf–æÅ÷÷7E÷W&Ã ¢Æ–æW2æVæB€¢b/	ùIr˜]‹MŠ}˜}Šı˜r˜Š}¸Í˜C¢¶f–æÅ÷÷7E÷W&ÇÒ ¢ ¢Æ–æW2æVæB‚"" ¢–b–æFW‚Òöfg6WB²ÆVâ‡&÷w2“ ¢Æ–æW2æVæB€¢.)H)H)H)H)H)H)H)H)H)H)H)H)H)H ¢¢Æ–æW2æVæB‚"" ¢VffV7F—fU÷F÷FÂÒ€¢ÆVâ‡&÷w2¢–bF÷FÂ—2æöæP¢VÇ6RÖ‚ƒÂ–çB‡F÷FÂ’¢ ¢–bVffV7F—fU÷F÷FÂâ–çB‡vU÷6—¦R÷"“ ¢Æ–æW2æW‡FVæB…°¢""À¢-Š‹Š}¸Â‹˜Š­˜bŠ˜r‹]˜Šİ˜~(Í˜}Š}¸ÂŠ‹Šı¸ÂŠ}‹"Šıª˜]˜~(Í˜}Š}¸Â‹-¸Í‹Š}‹=Š­˜Š}Šı˜rª˜m¸ÍŠò	ùr"À¢-Š‹ŠòŠ}‹"‹-Šı˜bŠıª˜]˜}ˆÂ˜M‹}˜Š}˜²¨m˜mŠòŠ½Š}˜m¸Í˜r‹]Š‹ª˜m¸ÍŠòŠ­Šr‹]˜Šİ˜rŠÍŠı¸ÍŠò˜m˜]Š}¸Í‹BŠıŠ}Šı˜r‹M˜Šòâ"À¢Ò ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2’ç7G&—‚ ¦FVb&V6÷&Eö6FVv÷'•÷÷7B€¢Æ÷rÀ¢†–v‚À¢öfg6WBÀ¢FW7F–æF–öåö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–BÀ¢†6‡FrÀ¢&÷w2À¢7&VFVEö'•÷W6W%ö–CÔæöæRÀ¢—5ö7F—fSÕG'VRÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDò6FVv÷'•÷÷7G0¢€¢Ö–å÷&–6Uö&–ÆÆ–öâÀ¢Ö…÷&–6Uö&–ÆÆ–öâÀ¢vUööfg6WBÀ¢vU÷6—¦RÀ¢FVÆVw&Õö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–BÀ¢†6‡FrÀ¢7&VFVEö'•÷W6W%ö–BÀ¢—5ö7F—fP¢¢dÅTU0¢€¢W2À¢W2À¢W2À¢À¢W2À¢W2À¢W2À¢W2À¢W0¢¢$UEU$ä”är–@¢"""À¢€¢Æ÷rÀ¢†–v‚À¢öfg6WBÀ¢FW7F–æF–öåö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–BÀ¢†6‡FrÀ¢7&VFVEö'•÷W6W%ö–BÀ¢&ööÂ†—5ö7F—fR’À¢’À¢ ¢6FVv÷'•÷÷7Eö–BÒ€¢7W"æfWF6†öæR‚•³Ğ¢ ¢f÷"÷6—F–öâÂ&÷r–âVçVÖW&FR€¢&÷w2À¢7F'CÓÀ¢“ ¢&÷W'G•ö–BÒ&÷u³Ğ¢&–6U÷FöÖâÒ&÷u³…Ğ ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDò6FVv÷'•÷÷7Eö—FV×0¢€¢6FVv÷'•÷÷7Eö–BÀ¢&÷W'G•ö–BÀ¢÷6—F–öâÀ¢6æ6†÷E÷&–6U÷FöÖà¢¢dÅTU0¢‚W2ÂW2ÂW2ÂW2¢"""À¢€¢6FVv÷'•÷÷7Eö–BÀ¢&÷W'G•ö–BÀ¢÷6—F–öâÀ¢€¢–çB‡&–6U÷FöÖâ¢–b&–6U÷FöÖâ—2æ÷BæöæP¢VÇ6RæöæP¢’À¢’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&â6FVv÷'•÷÷7Eö–@  ¦FVb7F—fFUö6FVv÷'•÷÷7EöFVÆ—fW'’€¢6FVv÷'•÷÷7Eö–BÀ¢FW7F–æF–öåö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–BÀ¢“ ¢""$f–æÆ—¦R&W6W'fVB6FVv÷'’gFW"FVÆVw&Ò66WG2F†R÷7Bâ"" ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢UDDR6FVv÷'•÷÷7G0¢4U@¢FVÆVw&Õö6†ææVÅö–BÒW2À¢FVÆVw&ÕöÖW76vUö–BÒW2À¢—5ö7F—fRÒE%TRÀ¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢€¢FW7F–æF–öåö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–BÀ¢6FVv÷'•÷÷7Eö–BÀ¢’À¢ ¢–b7W"ç&÷v6÷VçBÒ ¢&—6R'VçF–ÖTW'&÷"€¢%&W6W'fVB6FVv÷'’÷7B6÷VÆBæ÷B&R7F—fFVB ¢ ¢6öæâæ6öÖÖ—B‚  ¥tTT´D•ôäÔU2Ò°¢¢-Šı˜‹M˜mŠ˜r"À¢¢-‹=˜~(Í‹M˜mŠ˜r"À¢#¢-¨m˜}Š}‹‹M˜mŠ˜r"À¢3¢-›í˜mŠÍ‹M˜mŠ˜r"À¢C¢-ŠÍ˜]‹˜r"À¢S¢-‹M˜mŠ˜r"À¢c¢-¸Íª‹M˜mŠ˜r"À§Ğ  ¦FVb66†VGVÆUöÖVçR‚“ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)éRŠ}˜‹-˜Šı˜bŠŠ}‹-˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöFB"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù8²ŠŠ}‹-˜~(Í˜}Š}¸ÂŠ½ŠŠ®(Í‹MŠı˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöÆ—7B"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜rŠ­˜m‹¸Í˜]Š}Š¢"À¢6ÆÆ&6µöFFÒ'6WGF–æw2"À¢¢ÒÀ¢Ğ¢  ¦FVb66†VGVÆU÷vVV¶F•ö¶W–&ö&B‚“ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù8R˜}˜]˜r‹˜‹-˜}Šr"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•öÆÂ"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-‹M˜mŠ˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•óR"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-¸Íª‹M˜mŠ˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•ób"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-Šı˜‹M˜mŠ˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•ó"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-‹=˜~(Í‹M˜mŠ˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•ó"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-¨m˜}Š}‹‹M˜mŠ˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•ó""À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-›í˜mŠÍ‹M˜mŠ˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•ó2"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-ŠÍ˜]‹˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöF•óB"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ'66†VGVÆR"À¢¢ÒÀ¢Ğ¢  ¦FVbvWE÷66†VGVÆU÷v–æF÷w2‚“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢–BÀ¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢—5ö7F—fP¢e$ôÒ66†VGVÆU÷v–æF÷w0¢õ$DU"%¢vVV¶F’À¢7F'E÷F–ÖRÀ¢–@¢"" ¢ ¢&WGW&â7W"æfWF6†ÆÂ‚    ¦FVb7&VFU÷66†VGVÆU÷v–æF÷r€¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢w&÷Wö¶W“ÔæöæRÀ¢W6U÷7F'CÔæöæRÀ¢W6UöVæCÔæöæRÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDò66†VGVÆU÷v–æF÷w0¢€¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢—5ö7F—fRÀ¢w&÷Wö¶W’À¢W6U÷7F'BÀ¢W6UöVæ@¢¢dÅTU0¢€¢W2À¢W2À¢W2À¢W2À¢W2À¢E%TRÀ¢W2À¢W2À¢W0¢¢$UEU$ä”är–@¢"""À¢€¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢w&÷Wö¶W’À¢W6U÷7F'BÀ¢W6UöVæBÀ¢’À¢ ¢&÷rÒ7W"æfWF6†öæR‚¢6öæâæ6öÖÖ—B‚ ¢&WGW&â&÷u³Ğ ¦FVbFövvÆU÷66†VGVÆU÷v–æF÷r‡v–æF÷uö–B“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢UDDR66†VGVÆU÷v–æF÷w0¢4U@¢—5ö7F—fRÒäõB—5ö7F—fRÀ¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢$UEU$ä”är—5ö7F—fP¢"""À¢‡v–æF÷uö–BÂ’À¢ ¢&÷rÒ7W"æfWF6†öæR‚¢6öæâæ6öÖÖ—B‚ ¢&WGW&â&÷u³Ò–b&÷rVÇ6RæöæP    ¦FVbWFFU÷66†VGVÆU÷v–æF÷r€¢v–æF÷uö–BÀ¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢W6U÷7F'CÔæöæRÀ¢W6UöVæCÔæöæRÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢UDDR66†VGVÆU÷v–æF÷w0¢4U@¢vVV¶F’ÒW2À¢7F'E÷F–ÖRÒW2À¢VæE÷F–ÖRÒW2À¢–çFW'fÅöÖ–çWFW2ÒW2À¢Ö…÷÷7G2ÒW2À¢W6U÷7F'BÒW2À¢W6UöVæBÒW2À¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢$UEU$ä”är–@¢"""À¢€¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢W6U÷7F'BÀ¢W6UöVæBÀ¢v–æF÷uö–BÀ¢’À¢ ¢&÷rÒ7W"æfWF6†öæR‚¢6öæâæ6öÖÖ—B‚ ¢&WGW&â&÷u³Ò–b&÷rVÇ6RæöæP ¦FVbFVÆWFU÷66†VGVÆU÷v–æF÷r‡v–æF÷uö–B“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢DTÄUDRe$ôÒ66†VGVÆU÷v–æF÷w0¢t„U$R–BÒW0¢"""À¢‡v–æF÷uö–BÂ’À¢ ¢FVÆWFVBÒ7W"ç&÷v6÷VçBâ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&âFVÆWFV@    ¦FVbvWEöw&÷WVE÷66†VGVÆU÷v–æF÷w2‚“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢–BÀ¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢—5ö7F—fRÀ¢w&÷Wö¶W’À¢W6U÷7F'BÀ¢W6UöVæ@¢e$ôÒ66†VGVÆU÷v–æF÷w0¢õ$DU"%¢vVV¶F’À¢7F'E÷F–ÖRÀ¢–@¢"" ¢ ¢&÷w2Ò7W"æfWF6†ÆÂ‚ ¢w&÷WVBÒ·Ğ¢6–ævÆW2ÒµĞ ¢f÷"&÷r–â&÷w3 ¢€¢v–æF÷uö–BÀ¢vVV¶F’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢—5ö7F—fRÀ¢w&÷Wö¶W’À¢W6U÷7F'BÀ¢W6UöVæBÀ¢’Ò&÷p ¢–bæ÷Bw&÷Wö¶W“ ¢6–ævÆW2æVæB€¢°¢&¶–æB#¢'6–ævÆR"À¢&–B#¢v–æF÷uö–BÀ¢'vVV¶F’#¢vVV¶F’À¢'7F'E÷F–ÖR#¢7F'E÷F–ÖRÀ¢&VæE÷F–ÖR#¢VæE÷F–ÖRÀ¢&–çFW'fÅöÖ–çWFW2#¢–çFW'fÅöÖ–çWFW2À¢&Ö…÷÷7G2#¢Ö…÷÷7G2À¢&—5ö7F—fR#¢—5ö7F—fRÀ¢'W6U÷7F'B#¢W6U÷7F'BÀ¢'W6UöVæB#¢W6UöVæBÀ¢Ğ¢¢6öçF–çVP ¢—FVÒÒw&÷WVBç6WFFVfVÇB€¢w&÷Wö¶W’À¢°¢&¶–æB#¢&w&÷W"À¢&w&÷Wö¶W’#¢w&÷Wö¶W’À¢&–G2#¢µÒÀ¢'vVV¶F—2#¢µÒÀ¢'7F'E÷F–ÖR#¢7F'E÷F–ÖRÀ¢&VæE÷F–ÖR#¢VæE÷F–ÖRÀ¢&–çFW'fÅöÖ–çWFW2#¢–çFW'fÅöÖ–çWFW2À¢&Ö…÷÷7G2#¢Ö…÷÷7G2À¢&—5ö7F—fR#¢G'VRÀ¢'W6U÷7F'B#¢W6U÷7F'BÀ¢'W6UöVæB#¢W6UöVæBÀ¢ÒÀ¢ ¢—FVÕ²&–G2%ÒæVæB‡v–æF÷uö–B¢—FVÕ²'vVV¶F—2%ÒæVæB‡vVV¶F’ ¢–bæ÷B—5ö7F—fS ¢—FVÕ²&—5ö7F—fR%ÒÒfÇ6P ¢&W7VÇBÒÆ—7B†w&÷WVBçfÇVW2‚’¢&W7VÇBæW‡FVæB‡6–ævÆW2 ¢&WGW&â&W7VÇ@  ¦FVbWFFU÷66†VGVÆUöw&÷W€¢w&÷Wö¶W’À¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢W6U÷7F'CÔæöæRÀ¢W6UöVæCÔæöæRÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢UDDR66†VGVÆU÷v–æF÷w0¢4U@¢7F'E÷F–ÖRÒW2À¢VæE÷F–ÖRÒW2À¢–çFW'fÅöÖ–çWFW2ÒW2À¢Ö…÷÷7G2ÒW2À¢W6U÷7F'BÒW2À¢W6UöVæBÒW2À¢WFFVEöBÒäõr‚¢t„U$Rw&÷Wö¶W’ÒW0¢"""À¢€¢7F'E÷F–ÖRÀ¢VæE÷F–ÖRÀ¢–çFW'fÅöÖ–çWFW2À¢Ö…÷÷7G2À¢W6U÷7F'BÀ¢W6UöVæBÀ¢w&÷Wö¶W’À¢’À¢ ¢6÷VçBÒ7W"ç&÷v6÷Vç@¢6öæâæ6öÖÖ—B‚ ¢&WGW&â6÷Vç@ ¦FVbFövvÆU÷66†VGVÆUöw&÷W†w&÷Wö¶W’“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5B$ôôÅôäB†—5ö7F—fR¢e$ôÒ66†VGVÆU÷v–æF÷w0¢t„U$Rw&÷Wö¶W’ÒW0¢"""À¢†w&÷Wö¶W’Â’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢7W'&VçFÇ•ö7F—fRÒ&ööÂ€¢&÷ræB&÷u³Ğ¢ ¢æWu÷fÇVRÒæ÷B7W'&VçFÇ•ö7F—fP ¢7W"æW†V7WFR€¢"" ¢UDDR66†VGVÆU÷v–æF÷w0¢4U@¢—5ö7F—fRÒW2À¢WFFVEöBÒäõr‚¢t„U$Rw&÷Wö¶W’ÒW0¢"""À¢€¢æWu÷fÇVRÀ¢w&÷Wö¶W’À¢’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&âæWu÷fÇVP  ¦FVbFVÆWFU÷66†VGVÆUöw&÷W†w&÷Wö¶W’“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢DTÄUDRe$ôÒ66†VGVÆU÷v–æF÷w0¢t„U$Rw&÷Wö¶W’ÒW0¢"""À¢†w&÷Wö¶W’Â’À¢ ¢FVÆWFVBÒ7W"ç&÷v6÷Vç@¢6öæâæ6öÖÖ—B‚ ¢&WGW&âFVÆWFV@    ¦FVbf÷&ÖE÷66†VGVÆU÷v–æF÷w2‚“ ¢&÷w2ÒvWEöw&÷WVE÷66†VGVÆU÷v–æF÷w2‚ ¢–bæ÷B&÷w3 ¢&WGW&â€¢.(û‹-˜]Š}˜n(ÍŠ˜mŠı¸ÂŠ}˜mŠ­‹MŠ}‹ÆåÆâ ¢-˜}˜m˜‹"˜}¸Í¨bŠŠ}‹-˜~(ÍŠ}¸ÂŠ½ŠŠ¢˜m‹MŠı˜rŠ}‹=Š¢â ¢ ¢Æ–æW2Ò°¢.(û‹-˜]Š}˜n(ÍŠ˜mŠı¸ÂŠ}˜mŠ­‹MŠ}‹"À¢""À¢-˜]˜m‹}˜-˜r‹-˜]Š}˜m¸Ã¢Š­˜}‹Š}˜b"À¢""À¢Ğ ¢çVÖ&W"Ò ¢f÷"—FVÒ–â&÷w3  ¢7FGW2Ò€¢/	ùú"˜‹Š}˜B ¢–b—FVÕ²&—5ö7F—fR%Ğ¢VÇ6R/	ùKB‹­¸Í‹˜‹Š}˜B ¢ ¢–b—FVÕ²&¶–æB%ÒÓÒ&w&÷W# ¢F•öæÖRÒ/	ù8R˜}˜]˜r‹˜‹-˜}Šr ¢VÇ6S ¢F•öæÖRÒtTT´D•ôäÔU2ævWB€¢—FVÕ²'vVV¶F’%ÒÀ¢7G"†—FVÕ²'vVV¶F’%Ò’À¢ ¢7F'E÷FW‡BÒ—FVÕ°¢'7F'E÷F–ÖR ¢Òç7G&gF–ÖR‚"Tƒ¢TÒ" ¢VæE÷FW‡BÒ—FVÕ°¢&VæE÷F–ÖR ¢Òç7G&gF–ÖR‚"Tƒ¢TÒ" ¢Ö…÷FW‡BÒ€¢7G"†—FVÕ²&Ö…÷÷7G2%Ò¢–b—FVÕ²&Ö…÷÷7G2%Ò—2æ÷BæöæP¢VÇ6R-˜mŠ}˜]ŠİŠı˜Šò ¢ ¢W6U÷7F'BÒ—FVÒævWB€¢'W6U÷7F'B ¢ ¢W6UöVæBÒ—FVÒævWB€¢'W6UöVæB ¢ ¢Æ–æW2æW‡FVæB€¢°¢b"7¶çVÖ&W'ÒÂ¶F•öæÖWÒ"À¢7FGW2À¢b/	ùY·7F'E÷FW‡GÒŠ­Šr¶VæE÷FW‡GÒ"À¢Ğ¢ ¢–bW6U÷7F'BæBW6UöVæC ¢Æ–æW2æVæB€¢.(û‚Š­˜˜-˜Š}˜mŠ­‹MŠ}‹¢ ¢b'·W6U÷7F'Bç7G&gF–ÖR‚rTƒ¢TÒr—Ò ¢b-Š­Šr·W6UöVæBç7G&gF–ÖR‚rTƒ¢TÒr—Ò ¢¢VÇ6S ¢Æ–æW2æVæB€¢.(û‚Š­˜˜-˜Š}˜mŠ­‹MŠ}‹¢˜mŠıŠ}‹Šò ¢ ¢Æ–æW2æW‡FVæB€¢°¢€¢.(û˜Š}‹]˜M˜rŠ}˜mŠ­‹MŠ}‹¢ ¢b'¶—FVÕ²v–çFW'fÅöÖ–çWFW2u×ÒŠı˜-¸Í˜-˜r ¢’À¢b/	ù:b‹=˜-˜›í‹=Š£¢¶Ö…÷FW‡GÒ"À¢.)H)H)H)H)H)H)H)H)H)H)H)H"À¢Ğ¢ ¢çVÖ&W"³Ò ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2 ¦FVb66†VGVÆU÷v–æF÷w5ö¶W–&ö&B‚“ ¢&÷w2ÒvWEöw&÷WVE÷66†VGVÆU÷v–æF÷w2‚ ¢'WGFöç2ÒµĞ ¢çVÖ&W"Ò ¢f÷"—FVÒ–â&÷w3  ¢–b—FVÕ²&¶–æB%ÒÓÒ&w&÷W#  ¢w&÷Wö¶W’Ò—FVÕ°¢&w&÷Wö¶W’ ¢Ğ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢b.)Èşûˆò˜¸Í‹Š}¸Í‹B˜}˜]˜r‹˜‹-˜}Šr7¶çVÖ&W'Ò"À¢6ÆÆ&6µöFFÒ€¢b'66†VGVÆUöVF—Eöw&÷W÷¶w&÷Wö¶W—Ò ¢’À¢¢Ğ¢ ¢FövvÆU÷FW‡BÒ€¢b/	ùKB‹­¸Í‹˜‹Š}˜Bª‹Šı˜b˜}˜]˜r‹˜‹-˜}Šr7¶çVÖ&W'Ò ¢–b—FVÕ²&—5ö7F—fR%Ğ¢VÇ6P¢b/	ùú"˜‹Š}˜Bª‹Šı˜b˜}˜]˜r‹˜‹-˜}Šr7¶çVÖ&W'Ò ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢FövvÆU÷FW‡BÀ¢6ÆÆ&6µöFFÒ€¢b'66†VGVÆU÷FövvÆUöw&÷W÷¶w&÷Wö¶W—Ò ¢’À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢b/	ùyŠİ‹˜˜}˜]˜r‹˜‹-˜}Šr7¶çVÖ&W'Ò"À¢6ÆÆ&6µöFFÒ€¢b'66†VGVÆUöFVÆWFUöw&÷W÷¶w&÷Wö¶W—Ò ¢’À¢’À¢Ğ¢ ¢VÇ6S  ¢v–æF÷uö–BÒ—FVÕ²&–B%Ğ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢b.)Èşûˆò˜¸Í‹Š}¸Í‹B7¶çVÖ&W'Ò"À¢6ÆÆ&6µöFFÒ€¢b'66†VGVÆUöVF—E÷·v–æF÷uö–GÒ ¢’À¢¢Ğ¢ ¢FövvÆU÷FW‡BÒ€¢b/	ùKB‹­¸Í‹˜‹Š}˜Bª‹Šı˜b7¶çVÖ&W'Ò ¢–b—FVÕ²&—5ö7F—fR%Ğ¢VÇ6P¢b/	ùú"˜‹Š}˜Bª‹Šı˜b7¶çVÖ&W'Ò ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢FövvÆU÷FW‡BÀ¢6ÆÆ&6µöFFÒ€¢b'66†VGVÆU÷FövvÆU÷·v–æF÷uö–GÒ ¢’À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢b/	ùyŠİ‹˜7¶çVÖ&W'Ò"À¢6ÆÆ&6µöFFÒ€¢b'66†VGVÆUöFVÆWFU÷·v–æF÷uö–GÒ ¢’À¢’À¢Ğ¢ ¢çVÖ&W"³Ò ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)éRŠ}˜‹-˜Šı˜bŠŠ}‹-˜r"À¢6ÆÆ&6µöFFÒ'66†VGVÆUöFB"À¢¢Ğ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ'66†VGVÆR"À¢¢Ğ¢ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W€¢'WGFöç0¢ ¦FVb'6U÷66†VGVÆU÷F–ÖR‡fÇVR“ ¢g&öÒFFWF–ÖR–×÷'BFFWF–ÖP ¢fÇVRÒfÇVRç7G&—‚ ¢G'“ ¢'6VBÒFFWF–ÖRç7G'F–ÖR€¢fÇVRÀ¢"Tƒ¢TÒ"À¢ ¢&WGW&â'6VBçF–ÖR‚ ¢W†6WBfÇVTW'&÷# ¢&WGW&âæöæP   ¦FVbvWE÷VæF–æuöGWÆ–6FU÷&÷W'F–W2‚“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢æ–BÀ¢çVæ—Eö6öFRÀ¢æF—7G&–7BÀ¢æFG&W75÷FW‡BÀ¢æ&VöÓ"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢G"æGWÆ–6FUööe÷&÷W'G•ö–BÀ¢G"ç6–Ö–Æ&—G•÷66÷&RÀ¢G"æÖF6…÷&V6öâÀ¢æ7&VFVEö@¢e$ôÒV&Æ—6…÷VWVR¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒç&÷W'G•ö–@¢¤ô”âGWÆ–6FU÷&Wf–Ww2G ¢ôâG"ç&÷W'G•ö–BÒæ–@¢t„U$RçVWVU÷7FGW2ÒuTäD”äuôEUÄ”4DRp¢äBG"ç&Wf–Wu÷7FGW2ÒuTäD”ärp¢õ$DU"%¢æ7&VFVEöB42À¢æ–B40¢"" ¢ ¢&WGW&â7W"æfWF6†ÆÂ‚  ¦FVbvWE÷VæF–æuöGWÆ–6FUöFWF–Â‡&÷W'G•ö–B“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢æ–BÀ¢çVæ—Eö6öFRÀ¢æF—7G&–7BÀ¢æFG&W75÷FW‡BÀ¢æ&VöÓ"À¢æ&VG&öö×2À¢æfÆö÷%÷FW‡BÀ¢æ'V–ÆE÷–V"À¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢G"æGWÆ–6FUööe÷&÷W'G•ö–BÀ¢G"ç6–Ö–Æ&—G•÷66÷&RÀ¢G"æÖF6…÷&V6öâÀ¢öÆBçVæ—Eö6öFRÀ¢öÆBæF—7G&–7BÀ¢öÆBæFG&W75÷FW‡BÀ¢öÆBæ&VöÓ"À¢öÆBæ7W'&VçE÷&–6Uö&–ÆÆ–öà¢e$ôÒ&÷W'F–W2 ¢¤ô”âGWÆ–6FU÷&Wf–Ww2G ¢ôâG"ç&÷W'G•ö–BÒæ–@¢ÄTeB¤ô”â&÷W'F–W2öÆ@¢ôâöÆBæ–BÒG"æGWÆ–6FUööe÷&÷W'G•ö–@¢t„U$Ræ–BÒW0¢äBG"ç&Wf–Wu÷7FGW2ÒuTäD”ärp¢Ä”Ô•B¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&WGW&â7W"æfWF6†öæR‚  ¦FVbGWÆ–6FU÷VWVUöÆ—7Eö¶W–&ö&B‚“ ¢&÷w2ÒvWE÷VæF–æuöGWÆ–6FU÷&÷W'F–W2‚ ¢'WGFöç2ÒµĞ ¢f÷"&÷r–â&÷w3 ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&–6Uö&–ÆÆ–öâÀ¢GWÆ–6FUööe÷&÷W'G•ö–BÀ¢6–Ö–Æ&—G•÷66÷&RÀ¢ÖF6…÷&V6öâÀ¢7&VFVEöBÀ¢’Ò&÷p ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-ŠŠı˜˜b˜M˜ª¸Í‹M˜b ¢ ¢&V÷FW‡BÒ€¢b'¶&VöÓ'İ˜R ¢–b&VöÓ"—2æ÷BæöæP¢VÇ6R-˜]Š­‹Š}©‰ò ¢ ¢&–6U÷FW‡BÒ€¢b'·&–6Uö&–ÆÆ–öã¦wÒ ¢–b&–6Uö&–ÆÆ–öâ—2æ÷BæöæP¢VÇ6R-˜-¸Í˜]Š­‰ò ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢€¢b.)ªûˆò¶Æö6F–öçÒÂ ¢b'¶&V÷FW‡GÒÂ ¢b'·&–6U÷FW‡GÒ˜]¸Í˜M¸ÍŠ}‹Šò ¢•³£cÒÀ¢6ÆÆ&6µöFFÒ€¢b&GWVWVU÷f–Wu÷·&÷W'G•ö–GÒ ¢’À¢¢Ğ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHBŠ‹˜‹-‹‹=Š}˜m¸Â"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢¢Ğ¢ ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ&†öÖR"À¢¢Ğ¢ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W€¢'WGFöç0¢  ¢2ÓÓÒEUÄ”4DRTUTR4”Ô”Ä"d”UrÄ”ä²cÓÓĞ¦FVbGWÆ–6FU÷VWVUöFV6—6–öåö¶W–&ö&B€¢&÷W'G•ö–BÀ¢“ ¢'WGFöç2ÒµĞ ¢6–Ö–Æ%÷W&ÂÒ€¢vWEöGWÆ–6FU÷&÷W'G•÷f–Wu÷W&Â€¢&÷W'G•ö–@¢¢ ¢–b6–Ö–Æ%÷W&Ã ¢'WGFöç2æVæB€¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù˜]‹MŠ}˜}Šı˜r˜Š}¸Í˜B˜]‹MŠ}Š˜r"À¢W&Ã×6–Ö–Æ%÷W&ÂÀ¢¢Ğ¢ ¢'WGFöç2æW‡FVæB€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈRŠ­Š=¸Í¸ÍŠò˜‚ŠŠ}‹-ªı‹MŠ¢Š˜r‹]˜"À¢6ÆÆ&6µöFFÒ€¢b&GWVWVUö&÷fUò ¢b'·&÷W'G•ö–GÒ ¢’À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂ‹Šò˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ€¢b&GWVWVU÷&V¦V7Eò ¢b'·&÷W'G•ö–GÒ ¢’À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r˜M¸Í‹=Š¢"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢¢ÒÀ¢Ğ¢ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W†'WGFöç2 ¦FVb&÷fUöGWÆ–6FUöf÷%÷66†VGVÆW"€¢&÷W'G•ö–BÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W#  ¢7W"æW†V7WFR€¢"" ¢UDDRGWÆ–6FU÷&Wf–Ww0¢4U@¢&Wf–Wu÷7FGW2Òt$õdTBrÀ¢&Wf–WvVEöBÒäõr‚’À¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢äFÚ±î¸Â¸­yêë¢°k¢G§¦*^ review_status = 'PENDING'
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
        or "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
    )

    old_location = (
        old_address_text
        or old_district
        or "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
    )

    unit_text = (
        str(unit_code)
        if unit_code
        else "Ù‡Ù†ÙˆØ² Ø§Ø®ØªØµØ§Øµ Ø¯Ø§Ø¯Ù‡ Ù†Ø´Ø¯Ù‡"
    )

    old_unit_text = (
        str(old_unit_code)
        if old_unit_code
        else "Ù†Ø§Ù…Ø´Ø®Øµ"
    )

    area_text = (
        str(area_m2)
        if area_m2 is not None
        else "Ù†Ø§Ù…Ø´Ø®Øµ"
    )

    old_area_text = (
        str(old_area_m2)
        if old_area_m2 is not None
        else "Ù†Ø§Ù…Ø´Ø®Øµ"
    )

    price_text = (
        f"{price_billion:g} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯"
        if price_billion is not None
        else "Ù†Ø§Ù…Ø´Ø®Øµ"
    )

    old_price_text = (
        f"{old_price_billion:g} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯"
        if old_price_billion is not None
        else "Ù†Ø§Ù…Ø´Ø®Øµ"
    )

    return (
        "âš ï¸ Ø¨Ø±Ø±Ø³ÛŒ ÙØ§ÛŒÙ„ Ù…Ø´Ú©ÙˆÚ© Ø¨Ù‡ ØªÚ©Ø±Ø§Ø±ÛŒ\n\n"
        "ÙØ§ÛŒÙ„ Ø¬Ø¯ÛŒØ¯:\n"
        f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {unit_text}\n"
        f"ğŸ“ {location}\n"
        f"ğŸ“ {area_text} Ù…ØªØ±\n"
        f"ğŸ’° {price_text}\n\n"
        "ÙØ§ÛŒÙ„ Ù…Ø´Ø§Ø¨Ù‡ Ù‚Ø¨Ù„ÛŒ:\n"
        f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {old_unit_text}\n"
        f"ğŸ“ {old_location}\n"
        f"ğŸ“ {old_area_text} Ù…ØªØ±\n"
        f"ğŸ’° {old_price_text}\n\n"
        f"Ø¯Ø±ØµØ¯ Ø´Ø¨Ø§Ù‡Øª: {similarity_score}%\n"
        f"Ø¯Ù„ÛŒÙ„ ØªØ´Ø®ÛŒØµ: {match_reason or 'Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡'}\n\n"
        "Ø¯Ø± ØµÙˆØ±Øª ØªØ£ÛŒÛŒØ¯ØŒ ÙØ§ÛŒÙ„ Ø¨Ù‡ ØµÙ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ "
        "Ø¨Ø±Ù…ÛŒâ€ŒÚ¯Ø±Ø¯Ø¯ Ùˆ Ø¯Ø± Ø²Ù…Ø§Ù† Ù…Ù†Ø§Ø³Ø¨ Ù…Ù†ØªØ´Ø± Ù…ÛŒâ€ŒØ´ÙˆØ¯."
    )



QUEUE_STATUS_LABELS = {
    "NEW": "ğŸ†• Ø¬Ø¯ÛŒØ¯",
    "PARSED": "ğŸŸ¡ Ù¾Ø±Ø¯Ø§Ø²Ø´â€ŒØ´Ø¯Ù‡",
    "READY": "ğŸŸ¢ Ø¢Ù…Ø§Ø¯Ù‡",
    "SCHEDULED": "â° Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒâ€ŒØ´Ø¯Ù‡",
    "PENDING_DUPLICATE": "ğŸ§© ØªÚ©Ø±Ø§Ø±ÛŒ Ù…Ù†ØªØ¸Ø± ØªØ£ÛŒÛŒØ¯",
    "FAILED": "âŒ Ø®Ø·Ø§Ø¯Ø§Ø±",
    "SKIPPED": "â­ Ø±Ø¯Ø´Ø¯Ù‡",
    "PUBLISHED": "âœ… Ù…Ù†ØªØ´Ø±Ø´Ø¯Ù‡",
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
            or "Ù„ÙˆÚ©ÛŒØ´Ù† Ù†Ø§Ù…Ø´Ø®Øµ"
        )

        location = str(location).strip()

        if len(location) > 28:
            location = location[:28] + "â€¦"

        code_text = (
            f"Ú©Ø¯ {unit_code}"
            if unit_code
            else (
                f"Ù…Ù†Ø¨Ø¹ {source_unit_code}"
                if source_unit_code
                else f"ID {property_id}"
            )
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"âŒ {code_text} | {location}",
                    callback_data=(
                        f"queue_failed_view_{property_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "ğŸ”„ Ø¨Ø±ÙˆØ²Ø±Ø³Ø§Ù†ÛŒ",
                callback_data="queue_failed",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§",
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
                    "ğŸ›  Ø¨Ø§Ø²Ø®ÙˆØ§Ù†ÛŒ Ùˆ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØµÙ",
                    callback_data=(
                        f"queue_failed_repair_{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "ğŸš¨ ØªÙ„Ø§Ø´ ÙÙˆØ±ÛŒ Ø¨Ø±Ø§ÛŒ Ø§Ù†ØªØ´Ø§Ø±",
                    callback_data=(
                        f"queue_failed_publish_{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "ğŸ—‘ Ø­Ø°Ù Ø§Ø² ØµÙ",
                    callback_data=(
                        f"queue_failed_skip_{property_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ø®Ø·Ø§Ø¯Ø§Ø±",
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
        or "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
    )

    price_text = (
        f"{float(price_billion):g} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯"
        if price_billion is not None
        else "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
    )

    error_text = (
        str(last_error)
        if last_error
        else "Ù…ØªÙ† Ø®Ø·Ø§ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
    )

    if len(error_text) > 1200:
        error_text = error_text[:1200] + "â€¦"

    return (
        "âŒ ÙØ§ÛŒÙ„ Ø®Ø·Ø§Ø¯Ø§Ø±\n\n"
        f"Ø´Ù†Ø§Ø³Ù‡ Ø¯Ø§Ø®Ù„ÛŒ: {property_id}\n"
        f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {unit_code or 'Ù‡Ù†ÙˆØ² Ù†Ø¯Ø§Ø±Ø¯'}\n"
        f"Ú©Ø¯ Ù…Ù†Ø¨Ø¹: {source_unit_code or 'Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡'}\n"
        f"ğŸ“ {location}\n"
        f"ğŸ“ Ù…ØªØ±Ø§Ú˜: {area_m2 or 'Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡'}\n"
        f"ğŸ› Ø®ÙˆØ§Ø¨: {bedrooms if bedrooms is not None else 'Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡'}\n"
        f"ğŸ¢ Ø·Ø¨Ù‚Ù‡: {floor_text or 'Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡'}\n"
        f"ğŸ“… Ø³Ø§Ù„ Ø³Ø§Ø®Øª: {build_year or 'Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡'}\n"
        f"ğŸ’° Ù‚ÛŒÙ…Øª: {price_text}\n"
        f"ğŸ” ØªØ¹Ø¯Ø§Ø¯ ØªÙ„Ø§Ø´: {attempts or 0}\n\n"
        f"âš ï¸ Ø®Ø·Ø§ÛŒ Ø«Ø¨Øªâ€ŒØ´Ø¯Ù‡:\n{error_text}"
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
            "Ù…ØªÙ† Ø§ØµÙ„ÛŒ ÙØ§ÛŒÙ„ Ø¨Ø±Ø§ÛŒ Ø¨Ø§Ø²Ø®ÙˆØ§Ù†ÛŒ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯."
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
            "ÙØ§ÛŒÙ„ Ø¯ÛŒÚ¯Ø± Ø¯Ø± ÙˆØ¶Ø¹ÛŒØª Ø®Ø·Ø§Ø¯Ø§Ø± Ù†ÛŒØ³Øª."
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
        or "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"
    )

    if area_m2 is not None:
        area_value = float(area_m2)
        if area_value.is_integer():
            area_value = int(area_value)
        area_text = f"{area_value} Ù…ØªØ±"
    else:
        area_text = "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"

    if price_billion is not None:
        price_text = (
            f"{float(price_billion):g} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯ ØªÙˆÙ…Ø§Ù†"
        )
    else:
        price_text = "Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡"

    lines = [
        f"ğŸ” ÙØ§ÛŒÙ„ ØµÙ {property_id}",
        "",
        f"ÙˆØ¶Ø¹ÛŒØª: {status_text}",
        f"ğŸ“ {location}",
        f"ğŸ“ Ù…ØªØ±Ø§Ú˜: {area_text}",
    ]

    if bedrooms is not None:
        lines.append(
            f"ğŸ› Ø®ÙˆØ§Ø¨: {bedrooms}"
        )

    if floor_text:
        lines.append(
            f"ğŸ¢ Ø·Ø¨Ù‚Ù‡: {floor_text}"
        )

    if build_year:
        lines.append(
            f"ğŸ— Ø³Ø§Ù„ Ø³Ø§Ø®Øª: {build_year}"
        )

    lines.append(
        f"ğŸ’° Ù‚ÛŒÙ…Øª: {price_text}"
    )

    if unit_code:
        lines.append(
            f"ğŸ”¢ Ú©Ø¯ ÙˆØ§Ø­Ø¯: {unit_code}"
        )
    else:
        lines.append(
            "ğŸ”¢ Ú©Ø¯ ÙˆØ§Ø­Ø¯: Ù‡Ù†ÙˆØ² Ø§Ø®ØªØµØ§Øµ Ø¯Ø§Ø¯Ù‡ Ù†Ø´Ø¯Ù‡"
        )

    if source_unit_code:
        lines.append(
            f"ğŸ“ Ú©Ø¯ Ù…Ù†Ø¨Ø¹: {source_unit_code}"
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
                    "ğŸ”— Ù…Ø´Ø§Ù‡Ø¯Ù‡ ÙØ§ÛŒÙ„ Ù…Ù†Ø¨Ø¹",
                    url=source_post_url,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "ğŸ“¥ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØµÙ Ø§Ù†ØªØ´Ø§Ø±",
                callback_data="queue",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "ğŸ  Ù…Ù†ÙˆÛŒ Ø§ØµÙ„ÛŒ",
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
                "â¬…ï¸ Ù‚Ø¨Ù„ÛŒ",
                callback_data=(
                    f"queue_page_{previous_offset}"
                ),
            )
        )

    if offset + page_size < total:
        next_offset = offset + page_size

        navigation.append(
            InlineKeyboardButton(
                "Ø¨Ø¹Ø¯ÛŒ â¡ï¸",
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
                "ğŸ”„ Ø¨Ø±ÙˆØ²Ø±Ø³Ø§Ù†ÛŒ Ù‡Ù…ÛŒÙ† ØµÙØ­Ù‡",
                callback_data=f"queue_page_{offset}",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "ğŸ” Ø¬Ø³ØªØ¬Ùˆ Ø¯Ø± ØµÙ",
                callback_data="queue_search",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "ğŸ§© Ø¨Ø±Ø±Ø³ÛŒ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ ØªÚ©Ø±Ø§Ø±ÛŒ",
                callback_data="duplicate_queue",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§",
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
            "window_text": "Ø¨Ø±Ø§ÛŒ Ø§Ù…Ø±ÙˆØ² Ø¨Ø§Ø²Ù‡â€ŒØ§ÛŒ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡",
            "interval_text": "-",
            "next_publish_text": "Ø¨Ø§Ø²Ù‡ ÙØ¹Ø§Ù„ Ù†Ø¯Ø§Ø±Ø¯",
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
                "Ø¯Ø± Ø§ÙˆÙ„ÛŒÙ† Ø¨Ø±Ø±Ø³ÛŒ Scheduler"
            )

        return {
            "has_window": True,
            "active_now": True,
            "window_text": (
                f"{active_window['start'].strftime('%H:%M')} "
                f"ØªØ§ {active_window['end'].strftime('%H:%M')}"
            ),
            "interval_text": (
         !¶¬{®0®+^zºè¬è‘ééŠ—‚b'¶7F—fU÷v–æF÷u²v–çFW'fÂu×ÒŠı˜-¸Í˜-˜r ¢’À¢&æW‡E÷V&Æ—6…÷FW‡B#¢æW‡E÷FW‡BÀ¢Ğ ¢&WGW&â°¢&†5÷v–æF÷r#¢G'VRÀ¢&7F—fUöæ÷r#¢fÇ6RÀ¢'v–æF÷u÷FW‡B#¢€¢b'·6VÆV7FVE²w7F'BuÒç7G&gF–ÖR‚rTƒ¢TÒr—Ò ¢b-Š­Šr·6VÆV7FVE²vVæBuÒç7G&gF–ÖR‚rTƒ¢TÒr—Ò ¢’À¢&–çFW'fÅ÷FW‡B#¢€¢b'·6VÆV7FVE²v–çFW'fÂu×ÒŠı˜-¸Í˜-˜r ¢’À¢&æW‡E÷V&Æ—6…÷FW‡B#¢€¢6VÆV7FVE²'7F'B%Òç7G&gF–ÖR‚"Tƒ¢TÒ"¢’À¢Ğ  ¦FVb'V–ÆE÷&VÅ÷VWVU÷FW‡B€¢öfg6WCÓÀ¢vU÷6—¦SÓÀ¢“ ¢6÷VçG2ÒvWE÷&VÅ÷VWVUö6÷VçG2‚ ¢F÷FÂÒ7VÒ€¢6÷VçG2çfÇVW2‚¢ ¢–bF÷FÂâ ¢Æ7E÷fÆ–Eööfg6WBÒ€¢‡F÷FÂÒ’òòvU÷6—¦P¢’¢vU÷6—¦P ¢öfg6WBÒÖ–â€¢Ö‚ƒÂöfg6WB’À¢Æ7E÷fÆ–Eööfg6WBÀ¢¢VÇ6S ¢öfg6WBÒ  ¢&÷w2ÒvWE÷&VÅ÷V&Æ—6…÷VWVR€¢Æ–Ö—C×vU÷6—¦RÀ¢öfg6WCÖöfg6WBÀ¢ ¢66†VGVÆW%÷7FGW2ÒvWE÷VWVU÷66†VGVÆW%÷7FGW2‚ ¢66†VGVÆW%ö–6öâÒ€¢/	ùú" ¢–b66†VGVÆW%÷7FGW5²&7F—fUöæ÷r%Ğ¢VÇ6R/	ùú ¢ ¢Æ–æW2Ò°¢/	ù:R‹]˜Š}˜mŠ­‹MŠ}‹"À¢""À¢b-ª˜B˜Š}¸Í˜N(Í˜}Š}¸ÂŠı‹Š}˜mŠ­‹Š}‹¢·F÷FÇÒ"À¢""À¢b/	ùú"Š-˜]Š}Šı˜rŠ}˜mŠ­‹MŠ}‹¢¶6÷VçG5²u$TE’u×Ò"À¢€¢b'·66†VGVÆW%ö–6öçÒŠŠ}‹-˜rŠ}˜mŠ­‹MŠ}‹¢ ¢b'·66†VGVÆW%÷7FGW5²wv–æF÷u÷FW‡Bu×Ò ¢’À¢€¢.(û˜Š}‹]˜M˜rŠ}˜mŠ­‹MŠ}‹¢ ¢b'·66†VGVÆW%÷7FGW5²v–çFW'fÅ÷FW‡Bu×Ò ¢’À¢€¢/	ùY"Š}˜mŠ­‹MŠ}‹Š‹Šı¸ÂŠ­˜-‹¸ÍŠ¸Ã¢ ¢b'·66†VGVÆW%÷7FGW5²væW‡E÷V&Æ—6…÷FW‡Bu×Ò ¢’À¢€¢/	úz’Š­ª‹Š}‹¸Â˜]˜mŠ­‹‹Š­Š=¸Í¸ÍŠó¢ ¢b'¶6÷VçG5²uTäD”äuôEUÄ”4DRu×Ò ¢’À¢b.)ØÂŠí‹}Š}ŠıŠ}‹¢¶6÷VçG5²td”ÄTBu×Ò"À¢Ğ ¢–bæ÷B&÷w3 ¢Æ–æW2æW‡FVæB€¢°¢""À¢.)ÈRŠı‹ŠİŠ}˜BŠİŠ}‹m‹˜Š}¸Í˜M¸ÂŠı‹‹]˜Š}˜mŠ­‹MŠ}‹˜ŠÍ˜Šò˜mŠıŠ}‹Šòâ"À¢Ğ¢ ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2 ¢Æ–æW2æW‡FVæB€¢°¢""À¢.)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H"À¢-˜Š}¸Í˜N(Í˜}Š}¸Â‹]˜¢"À¢""À¢Ğ¢ ¢f÷"–æFW‚Â&÷r–âVçVÖW&FR€¢&÷w2À¢7F'CÖöfg6WB²À¢“ ¢€¢&÷W'G•ö–BÀ¢VWVU÷7FGW2À¢66†VGVÆVEöf÷"À¢GFV×G2À¢Æ7EöW'&÷"À¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&–6Uö&–ÆÆ–öâÀ¢6÷W&6U÷Væ—Eö6öFRÀ¢’Ò&÷p ¢7FGW5÷FW‡BÒTUTUõ5DEU5ôÄ$TÅ2ævWB€¢VWVU÷7FGW2À¢VWVU÷7FGW2À¢ ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-˜M˜ª¸Í‹M˜b˜mŠ}˜]‹MŠí‹R ¢ ¢26ÆVâ6öÖÖöâ6÷W&6R&Vf—†W2f÷"F—7Æ’à¢Æö6F–öâÒ7G"€¢Æö6F–öà¢’ç7G&—‚ ¢f÷"&Vf—‚–â€¢-˜]˜m‹}˜-˜r˜Š}ŠİŠó¢"À¢-˜]˜m‹}˜-˜r˜Š}ŠİŠò¢"À¢-˜Š}ŠİŠó¢"À¢-˜Š}ŠİŠò¢"À¢“ ¢–bÆö6F–öâç7F'G7v—F‚‡&Vf—‚“ ¢Æö6F–öâÒÆö6F–öå°¢ÆVâ‡&Vf—‚“ ¢Òç7G&—‚ ¢Æö6F–öâÒ€¢Æö6F–öà¢ç&WÆ6R‚'Â"Â"Â"¢ç&WÆ6R‚"Ò"Â"Ò"¢ ¢v†–ÆR""–âÆö6F–öã ¢Æö6F–öâÒÆö6F–öâç&WÆ6R€¢""À¢""À¢ ¢–b&VöÓ"—2æ÷BæöæS ¢&V÷fÇVRÒfÆöB€¢&VöÓ ¢ ¢–b&V÷fÇVRæ—5ö–çFVvW"‚“ ¢&V÷fÇVRÒ–çB€¢&V÷fÇVP¢ ¢&V÷FW‡BÒ€¢b'¶&V÷fÇVWİ˜R ¢¢VÇ6S ¢&V÷FW‡BÒ-˜]Š­‹Š}©‰ò  ¢–b&–6Uö&–ÆÆ–öâ—2æ÷BæöæS ¢&–6U÷fÇVRÒfÆöB€¢&–6Uö&–ÆÆ–öà¢ ¢–b&–6U÷fÇVRæ—5ö–çFVvW"‚“ ¢&–6U÷FW‡BÒ€¢b'¶–çB‡&–6U÷fÇVR—Ò˜]¸Í˜M¸ÍŠ}‹Šò ¢¢VÇ6S ¢&–6U÷FW‡BÒ€¢b'·&–6U÷fÇVS¦wÒ˜]¸Í˜M¸ÍŠ}‹Šò ¢¢VÇ6S ¢&–6U÷FW‡BÒ-˜-¸Í˜]Š­‰ò  ¢Æ–æW2æVæB€¢b'¶–æFW‡Òâ·7FGW5÷FW‡GÒ ¢ ¢Æ–æW2æVæB€¢b/	ù8Ò¶Æö6F–öçÒÂ ¢b'¶&V÷FW‡GÒÂ ¢b'·&–6U÷FW‡GÒ ¢ ¢Æ–æW2æVæB€¢b/	ùHâ‹M˜]Š}‹˜r›í¸Íªı¸Í‹¸Ã¢‹]˜·&÷W'G•ö–GÒ ¢ ¢–bVæ—Eö6öFS ¢Æ–æW2æVæB€¢b-ªŠò˜Š}ŠİŠó¢·Væ—Eö6öFWÒ ¢ ¢–b€¢VWVU÷7FGW2ÓÒ$d”ÄTB ¢æBÆ7EöW'&÷ ¢“ ¢W'&÷%÷FW‡BÒ7G"€¢Æ7EöW'&÷ ¢’ç&WÆ6R€¢%Æâ"À¢""À¢ ¢Æ–æW2æVæB€¢-Ší‹}Šs¢ ¢²W'&÷%÷FW‡E³£Ğ¢ ¢Æ–æW2æVæB‚.)H)H)H)H)H)H)H)H)H)H)H)H" ¢–bF÷FÂâ ¢F÷FÅ÷vW2Ò€¢F÷FÂ²vU÷6—¦RÒ¢’òòvU÷6—¦P ¢7W'&VçE÷vRÒ€¢öfg6WBòòvU÷6—¦P¢’² ¢f—'7Eö—FVÒÒ€¢öfg6WB²¢–b&÷w0¢VÇ6R ¢ ¢Æ7Eö—FVÒÒ€¢öfg6WB²ÆVâ‡&÷w2¢ ¢Æ–æW2æW‡FVæB€¢°¢""À¢€¢b/	ù8B‹]˜Šİ˜r¶7W'&VçE÷vWÒŠ}‹" ¢b'·F÷FÅ÷vW7Ò ¢’À¢€¢b-˜m˜]Š}¸Í‹B˜Š}¸Í˜N(Í˜}Š}¸Â¶f—'7Eö—FV×Ò ¢b-Š­Šr¶Æ7Eö—FV×ÒŠ}‹"·F÷FÇÒ˜Š}¸Í˜Bâ ¢’À¢Ğ¢ ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2  ¦FVbf–ÆW5öÖVçR‚“ ¢&WGW&â–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHBŠŠ}‹-˜m‹M‹˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6‚"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈR˜Š}¸Í˜N(Í˜}Š}¸Â˜]˜mŠ­‹M‹‹MŠı˜r"À¢6ÆÆ&6µöFFÒ'V&Æ—6†VB"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHŠŠ}‹-˜m‹M‹ªı‹˜˜}¸Â"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù:R‹]˜Š}˜mŠ­‹MŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVR"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	úz’Š‹‹‹=¸Â˜Š}¸Í˜N(Í˜}Š}¸ÂŠ­ª‹Š}‹¸Â"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ªûˆò˜Š}¸Í˜N(Í˜}Š}¸Â˜mŠ}˜-‹R"À¢6ÆÆ&6µöFFÒ&–çfÆ–B"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùyŠİ‹˜˜˜‹‹2˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ&f÷&6UöFVÆWFR"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂ˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùy‹=‹}˜BŠ-‹M‹­Š}˜B"À¢6ÆÆ&6µöFFÒ'G&6…÷vUó"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜rŠ­˜m‹¸Í˜]Š}Š¢"À¢6ÆÆ&6µöFFÒ'6WGF–æw2"À¢¢ÒÀ¢Ğ¢   ¦FVbæ÷&ÖÆ—¦U÷6V&6…÷FW‡B‡fÇVR“ ¢fÇVRÒ7G"‡fÇVR÷"""’ç7G&—‚ ¢fÇVRÒfÇVRçG&ç6ÆFR€¢7G"æÖ¶WG&ç2€¢-»»»-»=»M»]»m»}»»ššš-š=šMš]šmš}šš’"À¢##3CScsƒ“#3CScsƒ’"À¢¢ ¢fÇVRÒfÇVRç&WÆ6R‚-˜¢"Â-¸Â"¢fÇVRÒfÇVRç&WÆ6R‚-˜2"Â-ª’"¢fÇVRÒfÇVRç&WÆ6R‚-¸"Â-˜r" ¢fÇVRÒ&Rç7V"‡"%Ç2²"Â""ÂfÇVR’ç7G&—‚ ¢&WGW&âfÇVP  ¦FVb'6U÷6Ö'E÷6V&6…÷VW'’‡&u÷VW'’“ ¢FW‡BÒæ÷&ÖÆ—¦U÷6V&6…÷FW‡B‡&u÷VW'’ ¢&W7VÇBÒ°¢'&r#¢FW‡BÀ¢'&–6UöÖ–â#¢æöæRÀ¢'&–6UöÖ‚#¢æöæRÀ¢&&VöÖ–â#¢æöæRÀ¢&&VöÖ‚#¢æöæRÀ¢&&VG&öö×2#¢æöæRÀ¢&fÆö÷%÷FW‡B#¢æöæRÀ¢'Væ—Eö6öFR#¢æöæRÀ¢&g&VU÷FW‡B#¢æöæRÀ¢&†5÷&¶–ær#¢æöæRÀ¢&†5öVÆWfF÷"#¢æöæRÀ¢&†5÷7F÷&vR#¢æöæRÀ¢&FVVE÷&WV—&VB#¢æöæRÀ¢&F—&V7F–öâ#¢æöæRÀ¢Ğ ¢v÷&¶–ærÒFW‡@ ¢27G'V7GW&VBÖævVÖVçBf–ÇFW'2â6æöæ–6ÂÖ&¶W'2&RÇ6òW6VB'’F†P¢2–æÆ–æRf–ÇFW"'WGFöç2Â6ò6VÆV7FVBÖVæ—F–W2æWfW"ÆV²–çFòg&VRFW‡Bà¢fVGW&U÷7V72Ò€¢‚&†5÷&¶–ær"Â""ƒó­˜¸Í˜MŠ­‹¢“ı›íŠ}‹ª¸Í˜mªòƒó®(ÇÅÇ2’¢ƒó­ŠıŠ}‹ÍŠıŠ}‹Šò“ò"’À¢‚&†5öVÆWfF÷""Â""ƒó­˜¸Í˜MŠ­‹¢“ıŠ-‹=Š}˜m‹=˜‹ƒó®(ÇÅÇ2’¢ƒó­ŠıŠ}‹ÍŠıŠ}‹Šò“ò"’À¢‚&†5÷7F÷&vR"Â""ƒó­˜¸Í˜MŠ­‹¢“ıŠ}˜mŠŠ}‹¸Âƒó®(ÇÅÇ2’¢ƒó­ŠıŠ}‹ÍŠıŠ}‹Šò“ò"’À¢¢f÷"¶W’ÂGFW&â–âfVGW&U÷7V73 ¢ÖF6‚Ò&Rç6V&6‚‡GFW&âÂv÷&¶–ærÂfÆw3×&Rä”täõ$T44R¢–bÖF6ƒ ¢&W7VÇE¶¶W•ÒÒG'VP¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R†ÖF6‚æw&÷Wƒ’Â"" ¢FVVEöÖF6‚Ò&Rç6V&6‚‡""ƒó­˜¸Í˜MŠ­‹¢“ı‹=˜mŠòƒó®(ÇÅÇ2’¢ƒó­ŠıŠ}‹ÍŠıŠ}‹Šò“ò"Âv÷&¶–ær¢–bFVVEöÖF6ƒ ¢&W7VÇE²&FVVE÷&WV—&VB%ÒÒG'VP¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R†FVVEöÖF6‚æw&÷Wƒ’Â"" ¢F—&V7F–öåöÖF6‚Ò&Rç6V&6‚€¢""ƒó­ŠÍ˜}Š¥Ç2¥³¥ÂÕÓõÇ2§Í˜¸Í˜MŠ­‹¢’‹M˜]Š}˜M¸ÇÍŠÍ˜m˜Š¸Â’"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢¢–bF—&V7F–öåöÖF6ƒ ¢&W7VÇE²&F—&V7F–öâ%ÒÒF—&V7F–öåöÖF6‚æw&÷Wƒ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R†F—&V7F–öåöÖF6‚æw&÷Wƒ’Â"" ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2Væ—B6öFP¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢ÖF6‚Ò&Rç6V&6‚€¢""ƒó­ªŠõÇ2¢ƒó­˜Š}ŠİŠ÷Í˜]˜Mª—Í˜Š}¸Í˜B“õÇ2¥³¥ÂÕÓõÇ2¢’…ÆB²’"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢&W7VÇE²'Væ—Eö6öFR%ÒÒÖF6‚æw&÷Wƒ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R†ÖF6‚æw&÷Wƒ’Â"" ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2&–6R&ævS¢RŠ­Šr‚˜]¸Í˜M¸ÍŠ}‹ŠòòŠ¸Í˜bR˜‚‚˜]¸Í˜M¸ÍŠ}‹Šğ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢&–6U÷&ævU÷GFW&ç2Ò°¢"-Š¸Í˜eÇ2¢…ÆB²ƒó¥ÂåÆB²“ò•Ç2¢ƒó­Š­ŠwÍ˜‡ÂÒ•Ç2¢…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]¸Í˜M¸ÍŠ}‹Šò"À¢""…ÆB²ƒó¥ÂåÆB²“ò•Ç2¢ƒó­Š­ŠwÂÒ•Ç2¢…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]¸Í˜M¸ÍŠ}‹Šò"À¢Ğ ¢f÷"GFW&â–â&–6U÷&ævU÷GFW&ç3 ¢ÖF6‚Ò&Rç6V&6‚€¢GFW&âÀ¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢Æ÷rÒfÆöB†ÖF6‚æw&÷Wƒ’¢†–v‚ÒfÆöB†ÖF6‚æw&÷Wƒ"’ ¢–bÆ÷râ†–vƒ ¢Æ÷rÂ†–v‚Ò†–v‚ÂÆ÷p ¢&W7VÇE²'&–6UöÖ–â%ÒÒÆ÷p¢&W7VÇE²'&–6UöÖ‚%ÒÒ†–v€ ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢¢'&V° ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2&–6R&VÆ÷rò&÷fRòW†7@¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢–b€¢&W7VÇE²'&–6UöÖ–â%Ò—2æöæP¢æB&W7VÇE²'&–6UöÖ‚%Ò—2æöæP¢“ ¢ÖF6‚Ò&Rç6V&6‚€¢""ƒó­‹-¸Í‹Íª˜]Š­‹Ç2­Š}‹"•Ç2¢…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]¸Í˜M¸ÍŠ}‹Šò"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢&W7VÇE²'&–6UöÖ‚%ÒÒfÆöB€¢ÖF6‚æw&÷Wƒ¢ ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢–b€¢&W7VÇE²'&–6UöÖ–â%Ò—2æöæP¢æB&W7VÇE²'&–6UöÖ‚%Ò—2æöæP¢“ ¢ÖF6‚Ò&Rç6V&6‚€¢""ƒó­ŠŠ}˜MŠ}¸ÇÍŠŠ}˜MŠ}Š­‹Ç2­Š}‹'ÍŠ¸Í‹MŠ­‹Ç2­Š}‹"•Ç2¢…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]¸Í˜M¸ÍŠ}‹Šò"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢&W7VÇE²'&–6UöÖ–â%ÒÒfÆöB€¢ÖF6‚æw&÷Wƒ¢ ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢–b€¢&W7VÇE²'&–6UöÖ–â%Ò—2æöæP¢æB&W7VÇE²'&–6UöÖ‚%Ò—2æöæP¢“ ¢ÖF6‚Ò&Rç6V&6‚€¢""…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]¸Í˜M¸ÍŠ}‹Šò"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢fÇVRÒfÆöB†ÖF6‚æw&÷Wƒ’ ¢&W7VÇE²'&–6UöÖ–â%ÒÒfÇVP¢&W7VÇE²'&–6UöÖ‚%ÒÒfÇVP ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2&V&ævS¢cŠ­Šr“˜]Š­‹¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢ÖF6‚Ò&Rç6V&6‚€¢""…ÆB²ƒó¥ÂåÆB²“ò•Ç2¢ƒó­Š­ŠwÂÒ•Ç2¢…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]Š­‹"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢Æ÷rÒfÆöB†ÖF6‚æw&÷Wƒ’¢†–v‚ÒfÆöB†ÖF6‚æw&÷Wƒ"’ ¢–bÆ÷râ†–vƒ ¢Æ÷rÂ†–v‚Ò†–v‚ÂÆ÷p ¢&W7VÇE²&&VöÖ–â%ÒÒÆ÷p¢&W7VÇE²&&VöÖ‚%ÒÒ†–v€ ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2W†7B&V¢c˜]Š­‹¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢–b€¢&W7VÇE²&&VöÖ–â%Ò—2æöæP¢æB&W7VÇE²&&VöÖ‚%Ò—2æöæP¢“ ¢ÖF6‚Ò&Rç6V&6‚€¢""…ÆB²ƒó¥ÂåÆB²“ò•Ç2­˜]Š­‹ƒó­¸Â“ò"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢fÇVRÒfÆöB†ÖF6‚æw&÷Wƒ’ ¢&W7VÇE²&&VöÖ–â%ÒÒfÇVP¢&W7VÇE²&&VöÖ‚%ÒÒfÇVP ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2&VG&öö×0¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢ÖF6‚Ò&Rç6V&6‚€¢""…ÆB²•Ç2­Ší˜Š}Š‚"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢&W7VÇE²&&VG&öö×2%ÒÒ–çB€¢ÖF6‚æw&÷Wƒ¢ ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2fÆö÷ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢ÖF6‚Ò&Rç6V&6‚€¢"-‹}Š˜-˜uÇ2¥³¥ÂÕÓõÇ2¢ ¢""˜}˜]ª˜Í‹-¸Í‹‹-˜]¸Í˜gÍŠ}˜˜GÍŠı˜˜WÍ‹=˜˜WÍ¨m˜}Š}‹˜WÍ›í˜mŠÍ˜WÍ‹M‹M˜WÍ˜}˜Š­˜WÍ˜}‹MŠ­˜WÍ˜m˜}˜WÍŠı˜}˜WÅÆB²’"À¢v÷&¶–ærÀ¢fÆw3×&Rä”täõ$T44RÀ¢ ¢–bÖF6ƒ ¢&W7VÇE²&fÆö÷%÷FW‡B%ÒÒ€¢ÖF6‚æw&÷Wƒ’ç7G&—‚¢ ¢v÷&¶–ærÒv÷&¶–ærç&WÆ6R€¢ÖF6‚æw&÷Wƒ’À¢""À¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2v†FWfW"&VÖ–ç2&V6öÖW2g&VRFW‡C ¢2æV–v†&÷&†ööBÂGG&–'WFW2Â&&—G&'’v÷&G2ÂWF2à¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢v÷&¶–ærÒ&Rç7V"€¢"%Ç2²"À¢""À¢v÷&¶–ærÀ¢’ç7G&—‚"İˆÂÂ" ¢–bv÷&¶–æs ¢&W7VÇE²&g&VU÷FW‡B%ÒÒv÷&¶–æp ¢&WGW&â&W7VÇ@  ¦FVb6Ö'E÷6V&6…÷&÷W'F–W2‡&u÷VW'’Âöfg6WCÓÂÆ–Ö—CÓ“ ¢f–ÇFW'2Ò'6U÷6Ö'E÷6V&6…÷VW'’€¢&u÷VW'¢ ¢fÆ–F—G•÷vVV·2ÒvWEöf–ÆU÷fÆ–F—G•÷vVV·2‚ ¢v†W&RÒ°¢'ç7FGW2Òt5D•dRr"À¢'çV&Æ—6†VEöÖW76vUö–B•2äõBåTÄÂ"À¢'æFVÆWFVEög&öÕö6†ææVÅöB•2åTÄÂ"À¢'æ7&VFVEöBãÒäõr‚’Ò‚W2¢”åDU%dÂsvVV²r’"À¢"" ¢äõBU„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•÷G&6‚@¢t„U$RBç&÷W'G•ö–BÒæ–@¢¢"""À¢Ğ ¢&×2Ò°¢fÆ–F—G•÷vVV·2À¢Ğ ¢–bf–ÇFW'5²'Væ—Eö6öFR%Ó ¢v†W&RæVæB€¢"" ¢€¢çVæ—Eö6öFRÒW0¢õ"U„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•÷6÷W&6W20¢t„U$R2ç&÷W'G•ö–BÒæ–@¢äB2ç6÷W&6U÷Væ—Eö6öFRÒW0¢¢¢"" ¢¢&×2æW‡FVæB€¢°¢f–ÇFW'5²'Væ—Eö6öFR%ÒÀ¢f–ÇFW'5²'Væ—Eö6öFR%ÒÀ¢Ğ¢ ¢–bf–ÇFW'5²'&–6UöÖ–â%Ò—2æ÷BæöæS ¢v†W&RæVæB€¢'æ7W'&VçE÷&–6Uö&–ÆÆ–öâãÒW2 ¢¢&×2æVæB€¢f–ÇFW'5²'&–6UöÖ–â%Ğ¢ ¢–bf–ÇFW'5²'&–6UöÖ‚%Ò—2æ÷BæöæS ¢v†W&RæVæB€¢'æ7W'&VçE÷&–6Uö&–ÆÆ–öâÃÒW2 ¢¢&×2æVæB€¢f–ÇFW'5²'&–6UöÖ‚%Ğ¢ ¢–bf–ÇFW'5²&&VöÖ–â%Ò—2æ÷BæöæS ¢v†W&RæVæB€¢'æ&VöÓ"ãÒW2 ¢¢&×2æVæB€¢f–ÇFW'5²&&VöÖ–â%Ğ¢ ¢–bf–ÇFW'5²&&VöÖ‚%Ò—2æ÷BæöæS ¢v†W&RæVæB€¢'æ&VöÓ"ÃÒW2 ¢¢&×2æVæB€¢f–ÇFW'5²&&VöÖ‚%Ğ¢ ¢–bf–ÇFW'5²&&VG&öö×2%Ò—2æ÷BæöæS ¢v†W&RæVæB€¢'æ&VG&öö×2ÒW2 ¢¢&×2æVæB€¢f–ÇFW'5²&&VG&öö×2%Ğ¢ ¢–bf–ÇFW'5²&fÆö÷%÷FW‡B%Ó ¢v†W&RæVæB€¢$4ôÄU44R‡æfÆö÷%÷FW‡BÂrr’”Ä”´RW2 ¢¢&×2æVæB€¢b"W¶f–ÇFW'5²vfÆö÷%÷FW‡Bu×ÒR ¢ ¢f÷"¶W’Â6öÇVÖâ–â€¢‚&†5÷&¶–ær"Â'æ†5÷&¶–ær"’À¢‚&†5öVÆWfF÷""Â'æ†5öVÆWfF÷""’À¢‚&†5÷7F÷&vR"Â'æ†5÷7F÷&vR"’À¢“ ¢–bf–ÇFW'2ævWB†¶W’’—2G'VS ¢v†W&RæVæB†b'¶6öÇVÖçÒ•2E%TR" ¢–bf–ÇFW'2ævWB‚&F—&V7F–öâ"“ ¢v†W&RæVæB‚$4ôÄU44R‡æF—&V7F–öâÂrr’”Ä”´RW2"¢&×2æVæB†b"W¶f–ÇFW'5²vF—&V7F–öâu×ÒR" ¢–bf–ÇFW'2ævWB‚&FVVE÷&WV—&VB"’—2G'VS ¢v†W&RæVæB€¢"„åTÄÄ”b…E$”Ò„4ôÄU44R‡æFVVE÷FW‡BÂrr’’Ârr’•2äõBåTÄÂõ" ¢$U„•5E2…4TÄT5Be$ôÒ&÷W'G•öGG&–'WFW2t„U$Rç&÷W'G•ö–C×æ–B ¢$äBæGG&–'WFUö¶W’”â‚vFVVBrÂvFVVE÷G—Rr’ ¢$äBåTÄÄ”b…E$”Ò„4ôÄU44R‡æGG&–'WFU÷fÇVRÂrr’’Ârr’•2äõBåTÄÂ’’ ¢ ¢g&VU÷FW‡BÒf–ÇFW'5²&g&VU÷FW‡B%Ğ ¢–bg&VU÷FW‡C ¢æVVFÆRÒb"W¶g&VU÷FW‡GÒR  ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2Æö6F–öâÖf—'7B6V&6€¢0¢2–bF†RVçFW&VB‡&6R—27GVÆÇ’&W6VçB–âF†P¢2æ÷&ÖÆ—¦VBF—7G&–7BöFG&W72öbBÆV7BöæRÆ—fP¢2&÷W'G’ÂG&VB—B2Äô4D”ôâ6V&6‚à¢0¢2W†×ÆS ¢2ŠŠ}‹­˜]¸Í‹M˜p¢0¢2×W7Bæ÷BÖF6‚æ÷F†W"F—7G&–7BÖW&VÇ’&V6W6P¢2-ŠŠ}‹­˜]¸Í‹M˜r"V'26öÖWv†W&R–â&r6÷W&6RFW‡Bà¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5B¢e$ôÒ&÷W'F–W2 ¢t„U$Rç7FGW2Òt5D•dRp¢äBçV&Æ—6†VEöÖW76vUö–B•2äõBåTÄÀ¢äBæFVÆWFVEög&öÕö6†ææVÅöB•2åTÄÀ¢äBæ7&VFVEöBãÒäõr‚’Ò‚W2¢”åDU%dÂsvVV²r¢äBäõBU„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•÷G&6‚@¢t„U$RBç&÷W'G•ö–BÒæ–@¢¢äB€¢4ôÄU44R‡æF—7G&–7BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æFG&W75÷FW‡BÂrr’”Ä”´RW0¢¢Ä”Ô•B¢"""À¢€¢fÆ–F—G•÷vVV·2À¢æVVFÆRÀ¢æVVFÆRÀ¢’À¢ ¢Æö6F–öåöÖF6‚Ò€¢7W"æfWF6†öæR‚’—2æ÷BæöæP¢ ¢–bÆö6F–öåöÖF6ƒ ¢v†W&RæVæB€¢"" ¢€¢4ôÄU44R‡æF—7G&–7BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æFG&W75÷FW‡BÂrr’”Ä”´RW0¢¢"" ¢ ¢&×2æW‡FVæB€¢°¢æVVFÆRÀ¢æVVFÆRÀ¢Ğ¢ ¢VÇ6S ¢2vVæW&Âg&VR×FW‡B6V&6‚f÷"æöâÖÆö6F–öà¢2‡&6W27V6‚2GG&–'WFW2öfVGW&W2à¢v†W&RæVæB€¢"" ¢€¢4ôÄU44R‡çVæ—Eö6öFRÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æF—7G&–7BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æFG&W75÷FW‡BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æfÆö÷%÷FW‡BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡ç&u÷FW‡BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æ&VöÓ#£§FW‡BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æ7W'&VçE÷&–6Uö&–ÆÆ–öã£§FW‡BÂrr’”Ä”´RW0¢õ"U„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•öGG&–'WFW2¢t„U$Rç&÷W'G•ö–BÒæ–@¢äB€¢4ôÄU44R‡æGG&–'WFUöÆ&VÂÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æGG&–'WFU÷fÇVRÂrr’”Ä”´RW0¢¢¢õ"U„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•÷6÷W&6W20¢t„U$R2ç&÷W'G•ö–BÒæ–@¢äB4ôÄU44R€¢2ç6÷W&6U÷Væ—Eö6öFRÀ¢rp¢’”Ä”´RW0¢¢¢"" ¢ ¢&×2æW‡FVæB€¢¶æVVFÆUÒ¢ ¢ ¢2&&RçVÖ&W"7V6‚2#c# ¢2–bF†W&RvW&Ræò7G'V7GW&VBf–ÇFW'2ÂG&VB—B0¢2'&öB6V&6‚WfW'—v†W&Rà¢–b€¢æ÷Bg&VU÷FW‡@¢æB&RægVÆÆÖF6‚€¢"%ÆB²ƒó¥ÂåÆB²“ò"À¢f–ÇFW'5²'&r%ÒÀ¢¢æBf–ÇFW'5²'Væ—Eö6öFR%Ò—2æöæP¢æBf–ÇFW'5²'&–6UöÖ–â%Ò—2æöæP¢æBf–ÇFW'5²'&–6UöÖ‚%Ò—2æöæP¢æBf–ÇFW'5²&&VöÖ–â%Ò—2æöæP¢æBf–ÇFW'5²&&VöÖ‚%Ò—2æöæP¢æBf–ÇFW'5²&&VG&öö×2%Ò—2æöæP¢æBf–ÇFW'5²&fÆö÷%÷FW‡B%Ò—2æöæP¢“ ¢æVVFÆRÒb"W¶f–ÇFW'5²w&ru×ÒR  ¢v†W&RæVæB€¢"" ¢€¢4ôÄU44R‡çVæ—Eö6öFRÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æ&VöÓ#£§FW‡BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡æ7W'&VçE÷&–6Uö&–ÆÆ–öã£§FW‡BÂrr’”Ä”´RW0¢õ"4ôÄU44R‡ç&u÷FW‡BÂrr’”Ä”´RW0¢õ"U„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•öGG&–'WFW2¢t„U$Rç&÷W'G•ö–BÒæ–@¢äB4ôÄU44R‡æGG&–'WFU÷fÇVRÂrr’”Ä”´RW0¢¢õ"U„•5E2€¢4TÄT5B¢e$ôÒ&÷W'G•÷6÷W&6W20¢t„U$R2ç&÷W'G•ö–BÒæ–@¢äB4ôÄU44R€¢2ç6÷W&6U÷Væ—Eö6öFRÀ¢rp¢’”Ä”´RW0¢¢¢"" ¢ ¢&×2æW‡FVæB€¢¶æVVFÆUÒ¢`¢ ¢v†W&U÷7ÂÒ%ÆâäB"æ¦ö–â€¢b"‡¶—FV×Ò’ ¢f÷"—FVÒ–âv†W&P¢ ¢24Ô%Eõ$”4Uô45õc¢&–6U÷6÷'E÷&WVW7FVBÒ€¢f–ÇFW'5²'&–6UöÖ–â%Ò—2æ÷BæöæP¢÷"f–ÇFW'5²'&–6UöÖ‚%Ò—2æ÷BæöæP¢ ¢÷&FW%÷7ÂÒ€¢'æ7W'&VçE÷&–6Uö&–ÆÆ–öâ42åTÄÅ2Ä5BÂæ–B42 ¢–b&–6U÷6÷'E÷&WVW7FV@¢VÇ6R'çWFFVEöBDU42Âæ–BDU42 ¢ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢b"" ¢4TÄT5B4õTåB‚¢¢e$ôÒ&÷W'F–W2 ¢t„U$R·v†W&U÷7ÇĞ¢"""À¢&×2À¢ ¢F÷FÂÒ–çB€¢7W"æfWF6†öæR‚•³Ò÷" ¢ ¢7W"æW†V7WFR€¢b"" ¢4TÄT5@¢æ–BÀ¢çVæ—Eö6öFRÀ¢æF—7G&–7BÀ¢æFG&W75÷FW‡BÀ¢æ&VöÓ"À¢æ&VG&öö×2À¢æfÆö÷%÷FW‡BÀ¢æ7W'&VçE÷&–6Uö&–ÆÆ–öâÀ¢æ6†ææVÅ÷÷7E÷W&ÂÀ¢æ'V–ÆE÷–V"À¢æ7W'&VçE÷&–6U÷FöÖâÀ¢çV&Æ—6†VEöÖW76vUö–@¢e$ôÒ&÷W'F–W2 ¢t„U$R·v†W&U÷7ÇĞ¢õ$DU"%’¶÷&FW%÷7ÇĞ¢Ä”Ô•BW0¢ôde4UBW0¢"""À¢&×2²¶Æ–Ö—BÂöfg6WEÒÀ¢ ¢&÷w2Ò7W"æfWF6†ÆÂ‚ ¢&WGW&â°¢&f–ÇFW'2#¢f–ÇFW'2À¢'F÷FÂ#¢F÷FÂÀ¢'&÷w2#¢&÷w2À¢&öfg6WB#¢öfg6WBÀ¢&Æ–Ö—B#¢Æ–Ö—BÀ¢Ğ  ¦FVb6Ö'E÷6V&6…÷&W7VÇE÷FW‡B€¢&u÷VW'’À¢&W7VÇBÀ¢“ ¢F÷FÂÒ&W7VÇE²'F÷FÂ%Ğ¢öfg6WBÒ&W7VÇE²&öfg6WB%Ğ¢Æ–Ö—BÒ&W7VÇE²&Æ–Ö—B%Ğ¢f–ÇFW'2Ò&W7VÇE²&f–ÇFW'2%Ğ ¢–bF÷FÂÃÒ ¢&WGW&â€¢/	ùHÒŠÍ‹=Š­ŠÍ˜¸Â˜}˜‹M˜]˜mŠõÆåÆâ ¢b-‹ŠŠ}‹Š£¢·6Ö'E÷6V&6…öF—7Æ•÷VW'’‡&u÷VW'’—ÕÆåÆâ ¢.)ªûˆò˜}¸Í¨b˜Š}¸Í˜B˜‹Š}˜B˜‚˜]‹Š­Š‹¸Â›í¸ÍŠıŠr˜m‹MŠòâ ¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2&W6W'fRW†—7F–ærW&R&–6R×&ævR÷WGWBW†7FÇ¢22F†R6FVv÷'’×÷7Bf÷&ÖBà¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢W&U÷&–6U÷&ævRÒ€¢f–ÇFW'5²'&–6UöÖ–â%Ò—2æ÷BæöæP¢æBf–ÇFW'5²'&–6UöÖ‚%Ò—2æ÷BæöæP¢æBf–ÇFW'5²'&–6UöÖ–â%Ğ¢Òf–ÇFW'5²'&–6UöÖ‚%Ğ¢æBf–ÇFW'5²&&VöÖ–â%Ò—2æöæP¢æBf–ÇFW'5²&&VöÖ‚%Ò—2æöæP¢æBf–ÇFW'5²&&VG&öö×2%Ò—2æöæP¢æBf–ÇFW'5²&fÆö÷%÷FW‡B%Ò—2æöæP¢æBf–ÇFW'5²'Væ—Eö6öFR%Ò—2æöæP¢æBæ÷Bf–ÇFW'5²&g&VU÷FW‡B%Ğ¢æBæ÷Bf–ÇFW'2ævWB‚&†5÷&¶–ær"¢æBæ÷Bf–ÇFW'2ævWB‚&†5öVÆWfF÷""¢æBæ÷Bf–ÇFW'2ævWB‚&†5÷7F÷&vR"¢æBæ÷Bf–ÇFW'2ævWB‚&FVVE÷&WV—&VB"¢æBæ÷Bf–ÇFW'2ævWB‚&F—&V7F–öâ"¢ ¢–bW&U÷&–6U÷&ævS ¢6FVv÷'•÷&÷w2ÒµĞ ¢f÷"&÷r–â&W7VÇE²'&÷w2%Ó ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&VG&öö×2À¢fÆö÷%÷FW‡BÀ¢&–6Uö&–ÆÆ–öâÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢'V–ÆE÷–V"À¢&–6U÷FöÖâÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢’Ò&÷p ¢6FVv÷'•÷&÷w2æVæB€¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&VG&öö×2À¢'V–ÆE÷–V"À¢&–6Uö&–ÆÆ–öâÀ¢&–6U÷FöÖâÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢¢ ¢&WGW&â'V–ÆEö6FVv÷'•÷÷7E÷FW‡B€¢f–ÇFW'5²'&–6UöÖ–â%ÒÀ¢f–ÇFW'5²'&–6UöÖ‚%ÒÀ¢6FVv÷'•÷&÷w2À¢öfg6WCÖöfg6WBÀ¢F÷FÃ×F÷FÂÀ¢vU÷6—¦SÖÆ–Ö—BÀ¢ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2ÆÂ÷F†W"6V&6†W3 ¢26†÷r6ö×ÆWFRÆ—7F–ær6&G2§W7BÆ–¶R6FVv÷'¢2÷WGWBÂ–æ6ÇVF–ærF†RF—&V7BFVÆVw&Ò÷7BÆ–æ²à¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¢7F'EöçVÖ&W"Òöfg6WB²¢VæEöçVÖ&W"ÒÖ–â€¢öfg6WB²ÆVâ‡&W7VÇE²'&÷w2%Ò’À¢F÷FÂÀ¢ ¢Æ–æW2Ò°¢/	ùHÒŠÍ‹=Š­ŠÍ˜¸Â˜}˜‹M˜]˜mŠò"À¢""À¢b-‹ŠŠ}‹Š£¢·6Ö'E÷6V&6…öF—7Æ•÷VW'’‡&u÷VW'’—Ò"À¢b-˜mŠ­Š}¸ÍŠÃ¢·F÷FÇÒ˜Š}¸Í˜B"À¢b-˜m˜]Š}¸Í‹B·7F'EöçVÖ&W'ÒŠ­Šr¶VæEöçVÖ&W'Ò"À¢""À¢Ğ ¢6VÆV7FVBÒµĞ¢–bf–ÇFW'2ævWB‚&†5÷&¶–ær"“¢6VÆV7FVBæVæB‚/	ù©r›íŠ}‹ª¸Í˜mªò"¢–bf–ÇFW'2ævWB‚&†5öVÆWfF÷""“¢6VÆV7FVBæVæB‚/	ù¹rŠ-‹=Š}˜m‹=˜‹"¢–bf–ÇFW'2ævWB‚&†5÷7F÷&vR"“¢6VÆV7FVBæVæB‚/	ù:bŠ}˜mŠŠ}‹¸Â"¢–bf–ÇFW'2ævWB‚&FVVE÷&WV—&VB"“¢6VÆV7FVBæVæB‚/	ù8B‹=˜mŠò"¢–bf–ÇFW'2ævWB‚&F—&V7F–öâ"“¢6VÆV7FVBæVæB†b/	úzÒ¶f–ÇFW'5²vF—&V7F–öâu×Ò"¢–b6VÆV7FVC ¢Æ–æW2æW‡FVæB…²-‹M‹Š}¸Í‹r˜-‹}‹¸Ã¢"²"Â"æ¦ö–â‡6VÆV7FVB’Â"%Ò ¢f÷"–æFW‚Â&÷r–âVçVÖW&FR€¢&W7VÇE²'&÷w2%ÒÀ¢7F'C×7F'EöçVÖ&W"À¢“ ¢€¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&VG&öö×2À¢fÆö÷%÷FW‡BÀ¢&–6Uö&–ÆÆ–öâÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢'V–ÆE÷–V"À¢&–6U÷FöÖâÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢’Ò&÷p ¢Æö6F–öâÒ€¢FG&W75÷FW‡@¢÷"F—7G&–7@¢÷"-˜mŠ}˜]‹MŠí‹R ¢ ¢Æö6F–öâÒ7G"†Æö6F–öâ’ç&WÆ6R€¢-˜]˜m‹}˜-˜s¢"À¢""À¢’ç7G&—‚ ¢Æ–æW2æVæB€¢b'¶–æFW‡Ò’	ù8Ò¶Æö6F–öçÒ ¢ ¢FWF–Ç2ÒµĞ ¢–b&VöÓ"—2æ÷BæöæS ¢FWF–Ç2æVæB€¢b/	ù9¶fÆöB†&VöÓ"“¦wÒ˜]Š­‹ ¢ ¢–b&VG&öö×2—2æ÷BæöæS ¢FWF–Ç2æVæB€¢b/	ù¸ò¶–çB†&VG&öö×2—ÒŠí˜Š}Š‚ ¢ ¢–b'V–ÆE÷–V"—2æ÷BæöæS ¢FWF–Ç2æVæB€¢b/	ù8R‹=Š}˜B‹=Š}ŠíŠ¢¶–çB†'V–ÆE÷–V"—Ò ¢ ¢–bFWF–Ç3 ¢Æ–æW2æVæB€¢"Â"æ¦ö–â†FWF–Ç2¢ ¢–b&–6Uö&–ÆÆ–öâ—2æ÷BæöæS ¢Æ–æW2æVæB€¢b/	ù+˜-¸Í˜]Š£¢ ¢b'¶fÆöB‡&–6Uö&–ÆÆ–öâ“¦wÒ ¢b-˜]¸Í˜M¸ÍŠ}‹ŠòŠ­˜˜]Š}˜b ¢¢VÇ6S ¢Æ–æW2æVæB€¢/	ù+˜-¸Í˜]Š£¢Š½ŠŠ¢˜m‹MŠı˜r ¢ ¢Æ–æW2æVæB€¢b/	ùJ"ªŠò˜Š}ŠİŠó¢ ¢b'·Væ—Eö6öFR÷"rÒwÒ ¢ ¢÷7E÷W&ÂÒ€¢6†ææVÅ÷÷7E÷W&À¢÷"€¢b&‡GG3¢ò÷BæÖRöæ–ÖFÖVÆ²ò ¢b'·V&Æ—6†VEöÖW76vUö–GÒ ¢–bV&Æ—6†VEöÖW76vUö–@¢VÇ6RæöæP¢¢ ¢–b÷7E÷W&Ã ¢Æ–æW2æVæB€¢b/	ùIr˜]‹MŠ}˜}Šı˜r˜Š}¸Í˜C¢·÷7E÷W&ÇÒ ¢¢VÇ6S ¢Æ–æW2æVæB€¢/	ùIr˜]‹MŠ}˜}Šı˜r˜Š}¸Í˜C¢˜M¸Í˜mª’Š½ŠŠ¢˜m‹MŠı˜r ¢ ¢–b–æFW‚ÂVæEöçVÖ&W# ¢Æ–æW2æW‡FVæB€¢°¢""À¢.)H)H)H)H)H)H)H)H)H)H)H)H)H)H"À¢""À¢Ğ¢ ¢&WGW&â%Æâ"æ¦ö–â†Æ–æW2  ¦FVb6Ö'E÷6V&6…ö¶W–&ö&B‡&W7VÇBÂ&u÷VW'“ÔæöæR“ ¢&÷w2ÒµĞ ¢öfg6WBÒ&W7VÇE²&öfg6WB%Ğ¢Æ–Ö—BÒ&W7VÇE²&Æ–Ö—B%Ğ¢F÷FÂÒ&W7VÇE²'F÷FÂ%Ğ ¢æbÒµĞ ¢–böfg6WBâ ¢æbæVæB€¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜-Š˜M¸Â"À¢6ÆÆ&6µöFFÒ€¢b'6Ö'E÷6V&6…÷vUò ¢b'¶Ö‚ƒÂöfg6WBÖÆ–Ö—B—Ò ¢’À¢¢ ¢–böfg6WB²Æ–Ö—BÂF÷FÃ ¢æbæVæB€¢–æÆ–æT¶W–&ö&D'WGFöâ€¢-Š‹Šı¸Â)êûˆò"À¢6ÆÆ&6µöFFÒ€¢b'6Ö'E÷6V&6…÷vUò ¢b'¶öfg6WB¶Æ–Ö—GÒ ¢’À¢¢ ¢–bæc ¢&÷w2æVæB†æb ¢f–ÇFW'2Ò&W7VÇBævWB‚&f–ÇFW'2"’÷"·Ğ¢&÷w2æW‡FVæB…°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢‚.)ÈR"–bf–ÇFW'2ævWB‚&†5÷&¶–ær"’VÇ6R.)j¾ûˆò"’²-›íŠ}‹ª¸Í˜mªò"À¢6ÆÆ&6µöFFÒ'6Ö'Eöf–ÇFW%÷&¶–ær"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢‚.)ÈR"–bf–ÇFW'2ævWB‚&†5öVÆWfF÷""’VÇ6R.)j¾ûˆò"’²-Š-‹=Š}˜m‹=˜‹"À¢6ÆÆ&6µöFFÒ'6Ö'Eöf–ÇFW%öVÆWfF÷""À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢‚.)ÈR"–bf–ÇFW'2ævWB‚&†5÷7F÷&vR"’VÇ6R.)j¾ûˆò"’²-Š}˜mŠŠ}‹¸Â"À¢6ÆÆ&6µöFFÒ'6Ö'Eöf–ÇFW%÷7FöÚ±î¸Â¸­yêë¢°k¢G§¦*^rage",
            ),
            InlineKeyboardButton(
                ("âœ… " if filters.get("deed_required") else "â–«ï¸ ") + "Ø³Ù†Ø¯",
                callback_data="smart_filter_deed",
            ),
        ],
        [
            InlineKeyboardButton(
                ("âœ… " if filters.get("direction") == "Ø¬Ù†ÙˆØ¨ÛŒ" else "â–«ï¸ ") + "Ø¬Ù†ÙˆØ¨ÛŒ",
                callback_data="smart_filter_south",
            ),
            InlineKeyboardButton(
                ("âœ… " if filters.get("direction") == "Ø´Ù…Ø§Ù„ÛŒ" else "â–«ï¸ ") + "Ø´Ù…Ø§Ù„ÛŒ",
                callback_data="smart_filter_north",
            ),
        ],
    ])

    if total > 0:
        rows.append(
            [InlineKeyboardButton(
                "ğŸ”— Ø³Ø§Ø®Øª Ù„ÛŒÙ†Ú© Ø§Ø®ØªØµØ§ØµÛŒ Ù…Ø´ØªØ±ÛŒ",
                callback_data="customer_showcase_create",
            )]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "ğŸ” Ø¬Ø³ØªØ¬ÙˆÛŒ Ø¬Ø¯ÛŒØ¯",
                callback_data="smart_search",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "â¬…ï¸ Ù…Ù†ÙˆÛŒ Ø§ØµÙ„ÛŒ",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


SMART_FILTER_TOKENS = {
    "parking": "ÙÛŒÙ„ØªØ±:Ù¾Ø§Ø±Ú©ÛŒÙ†Ú¯",
    "elevator": "ÙÛŒÙ„ØªØ±:Ø¢Ø³Ø§Ù†Ø³ÙˆØ±",
    "storage": "ÙÛŒÙ„ØªØ±:Ø§Ù†Ø¨Ø§Ø±ÛŒ",
    "deed": "ÙÛŒÙ„ØªØ±:Ø³Ù†Ø¯",
    "south": "ÙÛŒÙ„ØªØ±:Ø¬Ù†ÙˆØ¨ÛŒ",
    "north": "ÙÛŒÙ„ØªØ±:Ø´Ù…Ø§Ù„ÛŒ",
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
                "â• Ø§ÙØ²ÙˆØ¯Ù† Ú©Ø§Ù†Ø§Ù„",
                callback_data="add_channel",
            ),
            InlineKeyboardButton(
                "ğŸ“¡ Ù…Ø¯ÛŒØ±ÛŒØª Ú©Ø§Ù†Ø§Ù„â€ŒÙ‡Ø§ÛŒ Ù…Ù†Ø¨Ø¹",
                callback_data="sources",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸš¨ Ø§Ù†ØªØ´Ø§Ø± ÙÙˆØ±ÛŒ",
                callback_data="urgent_publish",
            ),
            InlineKeyboardButton(
                "ğŸ“ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§",
                callback_data="files_menu",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ” Ø¬Ø³ØªØ¬ÙˆÛŒ Ù‡ÙˆØ´Ù…Ù†Ø¯",
                callback_data="smart_search",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ” Ø¬Ø³ØªØ¬ÙˆÛŒ Ú©Ø¯ ÙˆØ§Ø­Ø¯",
                callback_data="search_unit",
            ),
            InlineKeyboardButton(
                "ğŸ—‚ Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ",
                callback_data="category_send",
            ),
        ],
        [
            InlineKeyboardButton(
                "ğŸ“Š Ø¢Ù…Ø§Ø±",
                callback_data="stats",
            ),
            InlineKeyboardButton(
                "#ï¸âƒ£ Ù‡Ø´ØªÚ¯â€ŒÙ‡Ø§ÛŒ ØªØ§ Ú©Ù†ÙˆÙ† Ø§Ø³ØªÙØ§Ø¯Ù‡ Ø´Ø¯Ù‡",
                callback_data="hashtags",
            ),
        ],
        [
            InlineKeyboardButton(
                "â° Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ",
                callback_data="schedule",
            ),
            InlineKeyboardButton(
                "âš™ï¸ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
                callback_data="settings",
            ),
        ],
    ]

    if role in ("ADMIN", "OWNER"):
        rows.append(
            [
                InlineKeyboardButton(
                    "ğŸ‘¥ Ú©Ø§Ø±Ø¨Ø±Ø§Ù†",
                    callback_data="users",
                ),
                InlineKeyboardButton(
                    "â™»ï¸ Ø±ÛŒØ³Øª",
                    callback_data="reset",
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "ğŸ§¾ Ù„Ø§Ú¯â€ŒÙ‡Ø§",
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
        return "#ï¸âƒ£ Ù‡Ù†ÙˆØ² Ù‡Ø´ØªÚ¯ÛŒ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª."

    total = sum(
        int(row[1] or 0)
        for row in rows
    )

    lines = [
        "#ï¸âƒ£ Ù‡Ø´ØªÚ¯â€ŒÙ‡Ø§ÛŒ ØªØ§ Ú©Ù†ÙˆÙ† Ø§Ø³ØªÙØ§Ø¯Ù‡ Ø´Ø¯Ù‡",
        "",
        "ØªØ¹Ø¯Ø§Ø¯ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ ÙØ¹Ø§Ù„ Ø¯Ø± Ù‡Ø± Ø¨Ø§Ø²Ù‡ Ù‚ÛŒÙ…Øª:",
        "",
    ]

    for hashtag, count, sort_price in rows:
        lines.append(
            f"{hashtag} â€” {int(count)} ÙˆØ§Ø­Ø¯"
        )

    lines.extend(
        [
            "",
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”",
            f"Ø¬Ù…Ø¹ Ú©Ù„ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ ÙØ¹Ø§Ù„: {total} ÙˆØ§Ø­Ø¯",
        ]
    )

    return "\n".join(lines)


def back_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ Ù…Ù†ÙˆÛŒ Ø§ØµÙ„ÛŒ", callback_data="home")]]
    )


def settings_back_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª", callback_data="settings")]]
    )


def channel_type_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "ğŸ“¥ Ú©Ø§Ù†Ø§Ù„ ÙˆØ±ÙˆØ¯ÛŒ",
                    callback_data="add_type_INPUT",
                )
            ],
            [
                InlineKeyboardButton(
                    "ğŸ“¢ Ú©Ø§Ù†Ø§Ù„ Ø§ØµÙ„ÛŒ",
                    callback_data="add_type_DESTINATION",
                )
            ],
            [
                InlineKeyboardButton(
                    "ğŸ§¾ Ú©Ø§Ù†Ø§Ù„ Ù„Ø§Ú¯",
                    callback_data="add_type_LOG",
                )
            ],
            [
                InlineKeyboardButton(
                    "ğŸ“¡ Ú©Ø§Ù†Ø§Ù„ Ù…Ù†Ø¨Ø¹",
                    callback_data="add_type_SOURCE",
                )
            ],
            [
                InlineKeyboardButton(
                    "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
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
                ORDER BYjÇºã
âµç«®ŠÁ®‰˜©z–@¢Ä”Ô•BW0¢"""À¢€¢gFW%ö–BÀ¢Æ–Ö—BÀ¢’À¢ ¢&÷w2Ò7W"æfWF6†ÆÂ‚ ¢–bÆVâ‡&÷w2’ÂÆ–Ö—C ¢&VÖ–æ–ærÒÆ–Ö—BÒÆVâ‡&÷w2 ¢7W"æW†V7WFR€¢"" ¢4TÄT5B–@¢e$ôÒ&÷W'F–W0¢t„U$R7FGW2Òt5D•dRp¢äBV&Æ—6†VEöÖW76vUö–B•2äõBåTÄÀ¢äBFVÆWFVEög&öÕö6†ææVÅöB•2åTÄÀ¢äB4ôÄU44R‡V&Æ—6†VEöBÂ7&VFVEöB¢ãÒäõr‚’Ò”åDU%dÂsF—2p¢äB–BÃÒW0¢õ$DU"%’–@¢Ä”Ô•BW0¢"""À¢€¢gFW%ö–BÀ¢&VÖ–æ–ærÀ¢’À¢ ¢&÷w2æW‡FVæB€¢7W"æfWF6†ÆÂ‚¢ ¢&WGW&â°¢&÷u³Ğ¢f÷"&÷r–â&÷w0¢Ğ  ¦7–æ2FVb'VåöFVÆWFVE÷÷7G5÷vF6…÷F–6²€¢Æ–6F–öâÀ¢“ ¢gFW%ö–BÒÆ–6F–öâæ&÷EöFFævWB€¢&FVÆWFU÷vF6…öÆ7E÷&÷W'G•ö–B"À¢À¢ ¢&÷W'G•ö–G2ÒvWEöFVÆWFU÷vF6…÷&÷W'G•ö–G2€¢gFW%ö–CÖgFW%ö–BÀ¢Æ–Ö—CÔDTÄUDUõtD4…ô$D4…õ4•¤RÀ¢ ¢–bæ÷B&÷W'G•ö–G3 ¢Æ–6F–öâæ&÷EöFF°¢&FVÆWFU÷vF6…öÆ7E÷&÷W'G•ö–B ¢ÒÒ ¢&WGW&à ¢6†V6¶VBÒ  ¢f÷"&÷W'G•ö–B–â&÷W'G•ö–G3 ¢G'“ ¢v—B&Vg&W6…÷V&Æ—6†VE÷&÷W'G•÷÷7B€¢Æ–6F–öâæ&÷BÀ¢&÷W'G•ö–BÀ¢ ¢6†V6¶VB³Ò ¢W†6WBW†6WF–öã ¢Æövv–æræW†6WF–öâ€¢$FVÆWFVB×÷7BvF6†W"f–ÆVC¢ ¢'&÷W'G•ö–CÒW2"À¢&÷W'G•ö–BÀ¢ ¢Æ–6F–öâæ&÷EöFF°¢&FVÆWFU÷vF6…öÆ7E÷&÷W'G•ö–B ¢ÒÒ&÷W'G•ö–G5²ÓĞ ¢Æövv–æræ–æfò€¢$FVÆWFVB×÷7BvF6†W"6ö×ÆWFVC¢ ¢&6†V6¶VCÒW2Æ7E÷&÷W'G•ö–CÒW2"À¢6†V6¶VBÀ¢&÷W'G•ö–G5²ÓÒÀ¢  ¦7–æ2FVbFVÆWFVE÷÷7G5÷vF6†W%öÆö÷€¢Æ–6F–öâÀ¢“ ¢Æövv–æræ–æfò€¢$FVÆWFVB×÷7BvF6†W"7F'FVC¢ ¢&–çFW'fÃÒW72&F6ƒÒW2"À¢DTÄUDUõtD4…ô”åDU%dÅõ4T4ôäE2À¢DTÄUDUõtD4…ô$D4…õ4•¤RÀ¢ ¢v†–ÆRG'VS ¢G'“ ¢v—B7–æ6–òç6ÆVW€¢DTÄUDUõtD4…ô”åDU%dÅõ4T4ôäE0¢ ¢v—B'VåöFVÆWFVE÷÷7G5÷vF6…÷F–6²€¢Æ–6F–öà¢ ¢W†6WB7–æ6–òä6æ6VÆÆVDW'&÷# ¢Æövv–æræ–æfò€¢$FVÆWFVB×÷7BvF6†W"7F÷VBâ ¢¢&—6P ¢W†6WBW†6WF–öã ¢Æövv–æræW†6WF–öâ€¢%Væ†æFÆVBFVÆWFVB×÷7BvF6†W"W'&÷" ¢  ¦FVb7F'EöFVÆWFVE÷÷7G5÷vF6†W"€¢Æ–6F–öâÀ¢“ ¢W†—7F–ærÒÆ–6F–öâæ&÷EöFFævWB€¢&FVÆWFVE÷÷7G5÷vF6†W%÷F6² ¢ ¢–bW†—7F–æræBæ÷BW†—7F–æræFöæR‚“ ¢&WGW&âW†—7F–æp ¢F6²Ò7–æ6–òæ7&VFU÷F6²€¢FVÆWFVE÷÷7G5÷vF6†W%öÆö÷€¢Æ–6F–öà¢’À¢æÖSÒ&FVÆWFVE÷÷7G5÷vF6†W""À¢ ¢Æ–6F–öâæ&÷EöFF°¢&FVÆWFVE÷÷7G5÷vF6†W%÷F6² ¢ÒÒF6° ¢&WGW&âF6°   ¢2ÓÓÒDU5D”äD”ôâT$Ä”2$”4RtD4„U"cÓÓĞ ¤DU5D”äD”ôåõ$”4UõtD4…ô”åDU%dÅõ4T4ôäE2Òc ¤DU5D”äD”ôåõ$”4UõtD4…ôÔ…õtU2Ò3   ¦FVböFW7F–æF–öå÷V&Æ–5öfWF6…÷vR€¢W6W&æÖRÀ¢&Vf÷&UöÖW76vUö–BÀ¢“ ¢–×÷'BW&ÆÆ–"ç&WVW7@ ¢W6W&æÖRÒ7G"‡W6W&æÖR’æÇ7G&—‚$" ¢W&ÂÒ€¢b&‡GG3¢ò÷BæÖR÷2÷·W6W&æÖWÒ ¢b#ö&Vf÷&S×¶–çB†&Vf÷&UöÖW76vUö–B—Ò ¢ ¢&WVW7BÒW&ÆÆ–"ç&WVW7Bå&WVW7B€¢W&ÂÀ¢†VFW'3×°¢%W6W"ÔvVçB#¢€¢$Ö÷¦–ÆÆóRã ¢"…ƒ²Æ–çW‚ƒƒeócB’ ¢$ÆUvV$¶—BóS3rã3b ¢$6‡&öÖRó#B6f&’óS3rã3b ¢¢ÒÀ¢ ¢v—F‚W&ÆÆ–"ç&WVW7BçW&Æ÷Vâ€¢&WVW7BÀ¢F–ÖV÷WCÓRÀ¢’2&W7öç6S ¢&WGW&â&W7öç6Rç&VB‚’æFV6öFR€¢'WFbÓ‚"À¢W'&÷'3Ò'&WÆ6R"À¢  ¦FVböFW7F–æF–öå÷V&Æ–5öW‡G&7E÷÷7G2€¢vUö‡FÖÂÀ¢“ ¢–×÷'B‡FÖÂ2‡FÖÅöÖöGVÆP ¢Ö&¶W%÷&RÒ&Ræ6ö×–ÆR€¢"vFF×÷7CÒ%µâ%Ò²ò…ÆB²’"rÀ¢&Rä’À¢ ¢FW‡E÷&RÒ&Ræ6ö×–ÆR€¢€¢"sÆF—eµãåÒ¦6Æ73Ò%µâ%Ò¢p¢"wFvÖU÷v–FvWEöÖW76vU÷FW‡Bp¢"uµâ%Ò¢%µãåÒ£âp¢"r‚â£ò’p¢"sÂöF—câp¢’À¢&Rä’Â&Rå2À¢ ¢Ö&¶W'2ÒÆ—7B€¢Ö&¶W%÷&Ræf–æF—FW"‡vUö‡FÖÂ¢ ¢ÆÅö–G2ÒµĞ¢÷7G2Ò·Ğ ¢f÷"–æFW‚ÂÖ&¶W"–âVçVÖW&FR†Ö&¶W'2“  ¢ÖW76vUö–BÒ–çB€¢Ö&¶W"æw&÷Wƒ¢ ¢ÆÅö–G2æVæB†ÖW76vUö–B ¢7F'BÒÖ&¶W"æVæB‚ ¢–b–æFW‚²ÂÆVâ†Ö&¶W'2“ ¢VæBÒÖ&¶W'5¶–æFW‚²Òç7F'B‚¢VÇ6S ¢VæBÒÆVâ‡vUö‡FÖÂ ¢6‡Væ²ÒvUö‡FÖÅ·7F'C¦VæEĞ ¢FW‡EöÖF6‚ÒFW‡E÷&Rç6V&6‚€¢6‡Væ°¢ ¢–bæ÷BFW‡EöÖF6ƒ ¢6öçF–çVP ¢g&vÖVçBÒFW‡EöÖF6‚æw&÷Wƒ ¢g&vÖVçBÒ&Rç7V"€¢"#Æ'%Ç2¢óóâ"À¢%Æâ"À¢g&vÖVçBÀ¢fÆw3×&Rä’À¢ ¢g&vÖVçBÒ&Rç7V"€¢"#Â÷Ç2£â"À¢%Æâ"À¢g&vÖVçBÀ¢fÆw3×&Rä’À¢ ¢g&vÖVçBÒ&Rç7V"€¢"#ÅµãåÒ³â"À¢""À¢g&vÖVçBÀ¢ ¢V&Æ–5÷FW‡BÒ€¢‡FÖÅöÖöGVÆRçVæW66R†g&vÖVçB¢ç&WÆ6R‚%Ç†"Â""¢ç7G&—‚¢ ¢–bV&Æ–5÷FW‡C ¢÷7G5¶ÖW76vUö–EÒÒV&Æ–5÷FW‡@ ¢&WGW&â÷7G2ÂÆÅö–G0  ¦FVböFW7F–æF–öå÷&–6U÷vF6…÷&÷w2‚“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢æ–BÀ¢çV&Æ—6†VEöÖW76vUö–BÀ¢æ7W'&VçE÷&–6U÷FöÖâÀ¢æÖçVÅ÷&–6U÷FöÖà¢e$ôÒ&÷W'F–W2 ¢t„U$Rç7FGW2Òt5D•dRp¢äBçV&Æ—6†VEöÖW76vUö–@¢•2äõBåTÄÀ¢äBæFVÆWFVEög&öÕö6†ææVÅö@¢•2åTÄÀ¢õ$DU"%¢çV&Æ—6†VEöÖW76vUö–B40¢"" ¢ ¢&WGW&â7W"æfWF6†ÆÂ‚  ¦FVböFW7F–æF–öå÷&–6U÷vF6…öö'6W'fR€¢&÷W'G•ö–BÀ¢ÖW76vUö–BÀ¢V&Æ–5÷&–6RÀ¢“ ¢"" ¢W'6—7FVçBV&Æ–2×&–6R6æ6†÷Bà ¢$4TÄ”äS ¢f—'7Bö'6W'fF–öâ÷"&WV&Æ—6†VBÖW76vRà ¢4ÔS ¢V&Æ–2&–6RF–Bæ÷B6†ævRà ¢4„ätTC ¢V&Æ–2&–6R6†ævVB6–æ6RÆ7Bö'6W'fF–öâà¢""  ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W#  ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢V&Æ—6†VEöÖW76vUö–BÀ¢Æ7E÷6VVå÷&–6U÷FöÖà¢e$ôÒFW7F–æF–öå÷&–6U÷vF6…÷7FFP¢t„U$R&÷W'G•ö–BÒW0¢dõ"UDDP¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢–bæ÷B&÷s ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDğ¢FW7F–æF–öå÷&–6U÷vF6…÷7FFR€¢&÷W'G•ö–BÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢Æ7E÷6VVå÷&–6U÷FöÖâÀ¢Æ7E÷6VVåöBÀ¢WFFVEö@¢¢dÅTU2€¢W2À¢W2À¢W2À¢äõr‚’À¢äõr‚¢¢"""À¢€¢&÷W'G•ö–BÀ¢ÖW76vUö–BÀ¢V&Æ–5÷&–6RÀ¢’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&â€¢$$4TÄ”äR"À¢æöæRÀ¢ ¢€¢&Wf–÷W5öÖW76vUö–BÀ¢&Wf–÷W5÷&–6RÀ¢’Ò&÷p ¢–b€¢–çB‡&Wf–÷W5öÖW76vUö–B¢Ò–çB†ÖW76vUö–B¢“ ¢7W"æW†V7WFR€¢"" ¢UDDRFW7F–æF–öå÷&–6U÷vF6…÷7FFP¢4U@¢V&Æ—6†VEöÖW76vUö–BÒW2À¢Æ7E÷6VVå÷&–6U÷FöÖâÒW2À¢Æ7E÷6VVåöBÒäõr‚’À¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢€¢ÖW76vUö–BÀ¢V&Æ–5÷&–6RÀ¢&÷W'G•ö–BÀ¢’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&â€¢$$4TÄ”äR"À¢&Wf–÷W5÷&–6RÀ¢ ¢–b€¢&Wf–÷W5÷&–6R—2æ÷BæöæP¢æB–çB‡&Wf–÷W5÷&–6R¢ÓÒ–çB‡V&Æ–5÷&–6R¢“ ¢7W"æW†V7WFR€¢"" ¢UDDRFW7F–æF–öå÷&–6U÷vF6…÷7FFP¢4UBÆ7E÷6VVåöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&â€¢%4ÔR"À¢&Wf–÷W5÷&–6RÀ¢ ¢7W"æW†V7WFR€¢"" ¢UDDRFW7F–æF–öå÷&–6U÷vF6…÷7FFP¢4U@¢Æ7E÷6VVå÷&–6U÷FöÖâÒW2À¢Æ7E÷6VVåöBÒäõr‚’À¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢€¢V&Æ–5÷&–6RÀ¢&÷W'G•ö–BÀ¢’À¢ ¢6öæâæ6öÖÖ—B‚ ¢&WGW&â€¢$4„ätTB"À¢&Wf–÷W5÷&–6RÀ¢  ¦FVböFW7F–æF–öå÷&–6U÷vF6…÷&÷W'G•÷7FFR€¢&÷W'G•ö–BÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢æ7W'&VçE÷&–6U÷FöÖâÀ¢æÖçVÅ÷&–6U÷FöÖâÀ¢ç7FGW2À¢æFVÆWFVEög&öÕö6†ææVÅöBÀ¢€¢4TÄT5BÔ‚€¢2ç6÷W&6U÷&–6U÷FöÖà¢¢e$ôÒ&÷W'G•÷6÷W&6W20¢t„U$P¢2ç&÷W'G•ö–BÒæ–@¢äB2æ—5ö7F—fRÒE%TP¢äB2ç6÷W&6U÷&–6U÷FöÖà¢•2äõBåTÄÀ¢¢e$ôÒ&÷W'F–W2 ¢t„U$Ræ–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&WGW&â7W"æfWF6†öæR‚  ¦FVböFW7F–æF–öå÷&–6U÷vF6…÷6WEöÖçVÂ€¢&÷W'G•ö–BÀ¢ÖçVÅ÷&–6RÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'F–W0¢4U@¢ÖçVÅ÷&–6U÷FöÖâÒW2À¢ÖçVÅ÷&–6U÷WFFVEöBÒäõr‚’À¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢äB7FGW2Òt5D•dRp¢äBFVÆWFVEög&öÕö6†ææVÅö@¢•2åTÄÀ¢"""À¢€¢–çB†ÖçVÅ÷&–6R’À¢&÷W'G•ö–BÀ¢’À¢ ¢6öæâæ6öÖÖ—B‚  ¦7–æ2FVb'VåöFW7F–æF–öå÷V&Æ–5÷&–6U÷vF6…÷F–6²€¢Æ–6F–öâÀ¢“ ¢FW7F–æF–öâÒvWEöFW7F–æF–öåö6†ææVÂ‚ ¢–bæ÷BFW7F–æF–öã ¢&WGW&à ¢€¢FW7F–æF–öåö6†ææVÅö–BÀ¢FW7F–æF–öå÷F—FÆRÀ¢FW7F–æF–öå÷W6W&æÖRÀ¢’ÒFW7F–æF–öà ¢–bæ÷BFW7F–æF–öå÷W6W&æÖS ¢Æövv–ærçv&æ–ær€¢$DU5D”äD”ôâV&Æ–2&–6RvF6†W" ¢'&WV—&W2V&Æ–26†ææVÂW6W&æÖRâ ¢¢&WGW&à ¢&÷w2ÒöFW7F–æF–öå÷&–6U÷vF6…÷&÷w2‚ ¢–bæ÷B&÷w3 ¢&WGW&à ¢vF6†VBÒ°¢–çB‡&÷u³Ò“¢&÷p¢f÷"&÷r–â&÷w0¢–b&÷u³Ò—2æ÷BæöæP¢Ğ ¢–bæ÷BvF6†VC ¢&WGW&à ¢Ö–åöÖW76vUö–BÒÖ–â‡vF6†VB¢Ö…öÖW76vUö–BÒÖ‚‡vF6†VB ¢ö'6W'fVBÒ·Ğ ¢&Vf÷&RÒÖ…öÖW76vUö–B² ¢f÷"vUöçVÖ&W"–â&ævR€¢DU5D”äD”ôåõ$”4UõtD4…ôÔ…õtU0¢“ ¢G'“ ¢vUö‡FÖÂÒv—B7–æ6–òçFõ÷F‡&VB€¢öFW7F–æF–öå÷V&Æ–5öfWF6…÷vRÀ¢FW7F–æF–öå÷W6W&æÖRÀ¢&Vf÷&RÀ¢ ¢W†6WBW†6WF–öâ2W†3 ¢Æövv–ærçv&æ–ær€¢$DU5D”äD”ôâV&Æ–2vRfWF6‚f–ÆVC¢ ¢&&Vf÷&SÒW2W'&÷#ÒW2"À¢&Vf÷&RÀ¢W†2À¢¢'&V° ¢€¢vU÷÷7G2À¢ÆÅ÷vUö–G2À¢’ÒöFW7F–æF–öå÷V&Æ–5öW‡G&7E÷÷7G2€¢vUö‡FÖÀ¢ ¢f÷"ÖW76vUö–BÂV&Æ–5÷FW‡B–â€¢vU÷÷7G2æ—FV×2‚¢“ ¢–bÖW76vUö–B–âvF6†VC ¢ö'6W'fVE°¢ÖW76vUö–@¢ÒÒV&Æ–5÷FW‡@ ¢–bæ÷BÆÅ÷vUö–G3 ¢'&V° ¢Æ÷vW7BÒÖ–â†ÆÅ÷vUö–G2 ¢–bÆ÷vW7BÃÒÖ–åöÖW76vUö–C ¢'&V° ¢–bÆ÷vW7BãÒ&Vf÷&S ¢'&V° ¢&Vf÷&RÒÆ÷vW7@ ¢&6VÆ–æUö6÷VçBÒ ¢6ÖUö6÷VçBÒ ¢6†ævVEö6÷VçBÒ  ¢f÷"ÖW76vUö–BÂV&Æ–5÷FW‡B–â€¢ö'6W'fVBæ—FV×2‚¢“ ¢€¢&÷W'G•ö–BÀ¢öF%öÖW76vUö–BÀ¢ö7W'&VçE÷&–6RÀ¢öÖçVÅ÷&–6RÀ¢’ÒvF6†VE¶ÖW76vUö–EĞ ¢G'“ ¢'6VBÒ'6U÷&÷W'G’€¢V&Æ–5÷FW‡@¢ ¢V&Æ–5÷&–6RÒ'6VBævWB€¢&7W'&VçE÷&–6U÷FöÖâ ¢ ¢–bV&Æ–5÷&–6R—2æöæS ¢6öçF–çVP ¢V&Æ–5÷&–6RÒ–çB€¢V&Æ–5÷&–6P¢ ¢€¢ö'6W'fF–öå÷7FGW2À¢&Wf–÷W5÷V&Æ–5÷&–6RÀ¢’ÒöFW7F–æF–öå÷&–6U÷vF6…öö'6W'fR€¢&÷W'G•ö–BÀ¢ÖW76vUö–BÀ¢V&Æ–5÷&–6RÀ¢ ¢–bö'6W'fF–öå÷7FGW2ÓÒ$$4TÄ”äR# ¢&6VÆ–æUö6÷VçB³Ò¢6öçF–çVP ¢–bö'6W'fF–öå÷7FGW2ÓÒ%4ÔR# ¢6ÖUö6÷VçB³Ò¢6öçF–çVP ¢6†ævVEö6÷VçB³Ò ¢7FFRÒ€¢öFW7F–æF–öå÷&–6U÷vF6…÷&÷W'G•÷7FFR€¢&÷W'G•ö–@¢¢ ¢–bæ÷B7FFS ¢6öçF–çVP ¢€¢7W'&VçE÷&–6RÀ¢&Wf–÷W5öÖçVÅ÷&–6RÀ¢7FGW2À¢FVÆWFVEög&öÕö6†ææVÅöBÀ¢Ö…÷6÷W&6U÷&–6RÀ¢’Ò7FFP ¢–b€¢7FGW2Ò$5D•dR ¢÷"FVÆWFVEög&öÕö6†ææVÅö@¢—2æ÷BæöæP¢“ ¢6öçF–çVP ¢2–bV&Æ–2&–6R6†ævVB&V6W6R÷W"÷và¢26÷W&6R×7–æ2WFFVBF†R÷7BÂD"Ç&VG¢26öçF–ç2F†BW†7BVffV7F—fR&–6Rà¢–b€¢7W'&VçE÷&–6R—2æ÷BæöæP¢æB–çB†7W'&VçE÷&–6R¢ÓÒV&Æ–5÷&–6P¢“ ¢Æövv–æræ–æfò€¢$DU5D”äD”ôâV&Æ–2&–6R6†ævR ¢'&V6övæ—¦VB2&÷B÷6÷W&6RWFFS¢ ¢'&÷W'G•ö–CÒW2 ¢&öÆE÷V&Æ–3ÒW2 ¢&æWu÷V&Æ–3ÒW2 ¢&F%öVffV7F—fSÒW2"À¢&÷W'G•ö–BÀ¢&Wf–÷W5÷V&Æ–5÷&–6RÀ¢V&Æ–5÷&–6RÀ¢7W'&VçE÷&–6RÀ¢¢6öçF–çVP ¢öFW7F–æF–öå÷&–6U÷vF6…÷6WEöÖçVÂ€¢&÷W'G•ö–BÀ¢V&Æ–5÷&–6RÀ¢ ¢g&öÒ–ævW7B–×÷'B€¢&V6öæ6–ÆU÷&÷W'G•÷&–6RÀ¢ ¢VffV7F—fU÷&–6RÒ€¢&V6öæ6–ÆU÷&÷W'G•÷&–6R€¢&÷W'G•ö–@¢¢ ¢v—B&Vg&W6…÷V&Æ—6†VE÷&÷W'G•÷÷7B€¢Æ–6F–öâæ&÷BÀ¢&÷W'G•ö–BÀ¢ ¢6FVv÷'•÷&W7VÇBÒ€¢v—B7–æ5ö6FVv÷&–W5öf÷%÷&–6Uö6†ævR€¢Æ–6F–öâæ&÷BÀ¢&÷W'G•ö–BÀ¢7W'&VçE÷&–6RÀ¢VffV7F—fU÷&–6RÀ¢¢ ¢Æövv–æræ–æfò€¢$DU5D”äD”ôâT$Ä”2ÔåTÂ$”4R44UDTC¢ ¢'&÷W'G•ö–CÒW2 ¢&ÖW76vUö–CÒW2 ¢&öÆE÷V&Æ–3ÒW2 ¢&ÖçVÅö&Vf÷&SÒW2 ¢&ÖçVÅöæWsÒW2 ¢'6÷W&6UöÖƒÒW2 ¢&VffV7F—fSÒW2 ¢&6FVv÷&–W5÷7–æ6VCÒW2 ¢&6FVv÷&–W5÷&VÖ÷fVCÒW2"À¢&÷W'G•ö–BÀ¢ÖW76vUö–BÀ¢&Wf–÷W5÷V&Æ–5÷&–6RÀ¢&Wf–÷W5öÖçVÅ÷&–6RÀ¢V&Æ–5÷&–6RÀ¢Ö…÷6÷W&6U÷&–6RÀ¢VffV7F—fU÷&–6RÀ¢6FVv÷'•÷&W7VÇBævWB€¢'7–æ6VB"À¢À¢’À¢6FVv÷'•÷&W7VÇBævWB€¢'&VÖ÷fVB"À¢À¢’À¢ ¢W†6WBW†6WF–öã ¢Æövv–æræW†6WF–öâ€¢$DU5D”äD”ôâV&Æ–2&–6R&ö6W76–ær ¢&f–ÆVC¢&÷W'G•ö–CÒW2 ¢&ÖW76vUö–CÒW2"À¢&÷W'G•ö–BÀ¢ÖW76vUö–BÀ¢ ¢Æövv–æræ–æfò€¢$DU5D”äD”ôâV&Æ–2&–6R66ã¢ ¢'vF6†VCÒW2ö'6W'fVCÒW2 ¢&&6VÆ–æSÒW26ÖSÒW26†ævVCÒW2"À¢ÆVâ‡vF6†VB’À¢ÆVâ†ö'6W'fVB’À¢&6VÆ–æUö6÷VçBÀ¢6ÖUö6÷VçBÀ¢6†ævVEö6÷VçBÀ¢  ¦7–æ2FVbFW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W%öÆö÷€¢Æ–6F–öâÀ¢“ ¢Æövv–æræ–æfò€¢$DU5D”äD”ôâV&Æ–2&–6RvF6†W"7F'FVC¢ ¢&–çFW'fÃÒW72"À¢DU5D”äD”ôåõ$”4UõtD4…ô”åDU%dÅõ4T4ôäE2À¢ ¢v†–ÆRG'VS ¢G'“ ¢v—B'VåöFW7F–æF–öå÷V&Æ–5÷&–6U÷vF6…÷F–6²€¢Æ–6F–öà¢ ¢W†6WB7–æ6–òä6æ6VÆÆVDW'&÷# ¢Æövv–æræ–æfò€¢$DU5D”äD”ôâV&Æ–2&–6RvF6†W"7F÷VBâ ¢¢&—6P ¢W†6WBW†6WF–öã ¢Æövv–æræW†6WF–öâ€¢%Væ†æFÆVBDU5D”äD”ôâV&Æ–2 ¢'&–6RvF6†W"W'&÷" ¢ ¢v—B7–æ6–òç6ÆVW€¢DU5D”äD”ôåõ$”4UõtD4…ô”åDU%dÅõ4T4ôäE0¢  ¦FVb7F'EöFW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W"€¢Æ–6F–öâÀ¢“ ¢W†—7F–ærÒÆ–6F–öâæ&÷EöFFævWB€¢&FW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W%÷F6² ¢ ¢–bW†—7F–æræBæ÷BW†—7F–æræFöæR‚“ ¢&WGW&âW†—7F–æp ¢F6²Ò7–æ6–òæ7&VFU÷F6²€¢FW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W%öÆö÷€¢Æ–6F–öà¢’À¢æÖSÒ&FW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W""À¢ ¢Æ–6F–öâæ&÷EöFF°¢&FW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W%÷F6² ¢ÒÒF6° ¢&WGW&âF6°  ¦7–æ2FVb÷7Eö–æ—B†Æ–6F–öâ“ ¢6öÖÖæG2Ò°¢&÷D6öÖÖæB‚'7F'B"Â-‹M‹˜‹’‹ŠŠ}Š¢"’À¢&÷D6öÖÖæB‚&Æöv–â"Â-˜‹˜ŠòŠ˜rŠİ‹=Š}Š‚"’À¢&÷D6öÖÖæB‚&Æöv÷WB"Â-Ší‹˜ŠÂŠ}‹"Šİ‹=Š}Š‚"’À¢Ğ ¢v—BÆ–6F–öâæ&÷Bç6WEö×•ö6öÖÖæG2†6öÖÖæG2¢v—BÆ–6F–öâæ&÷Bç6WEö6†EöÖVçUö'WGFöâ€¢ÖVçUö'WGFöãÔÖVçT'WGFöä6öÖÖæG2‚¢ ¢Vç7W&U÷&÷W'G•÷G&6…÷66†VÖ‚ ¢–bvWEö÷6WGF–ær€¢&f–ÆU÷fÆ–F—G•÷vVV·2"À¢æöæRÀ¢’—2æöæS ¢6fUö÷6WGF–ær€¢&f–ÆU÷fÆ–F—G•÷vVV·2"À¢‚À¢ ¢Vç7W&Uö7&Õ÷66†VÖ‚¢Vç7W&Uö6†ææVÅöf–ÆUöVF—F÷%÷66†VÖ‚¢Vç7W&Uöfö÷FW%ö'VÆµ÷&Vg&W6…÷66†VÖ‚¢Vç7W&U÷6Ö'E÷66†VÖ‚¢Vç7W&U÷6†÷v66U÷66†VÖ‚¢7F'E÷f—6—E÷&VÖ–æFW%÷v÷&¶W"†Æ–6F–öâ¢7F'E÷6V&6…÷vF6…÷v÷&¶W"†Æ–6F–öâ¢7F'Eöfö÷FW%ö'VÆµ÷&Vg&W6…÷v÷&¶W"†Æ–6F–öâ¢7F'E÷66†VGVÆW"†Æ–6F–öâ¢7F'EöFVÆWFVE÷÷7G5÷vF6†W"†Æ–6F–öâ¢7F'EöFW7F–æF–öå÷V&Æ–5÷&–6U÷vF6†W"†Æ–6F–öâ¢7F'E÷6÷W&6UöFVÆWFVE÷÷7G5÷vF6†W"†Æ–6F–öâ ¢Æövv–æræ–æfò€¢$WFöÖF–266†VGVÆW"–æ—F–Æ—¦VBâ ¢  ¦7–æ2FVb6†÷uöÖ–åöÖVçR‡WFFRÂ6W76–öâ“ ¢òÂW6W&æÖRÂF—7Æ•öæÖRÂ&öÆRÒ6W76–öà¢æÖRÒF—7Æ•öæÖR÷"W6W&æÖP ¢FW‡BÒ€¢b/	øúŠí˜‹BŠ-˜]Šı¸ÍŠò¶æÖWÕÆåÆâ ¢-›í˜m˜B˜]Šı¸Í‹¸ÍŠ¢˜‚Š}˜mŠ­‹MŠ}‹˜Š}¸Í˜N(Í˜}Š}¸ÂŠ}˜]˜MŠ}ª•Æâ ¢-ŠŠí‹B˜]˜‹Šı˜m‹‹‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠó¢ ¢ ¢–bWFFRæ6ÆÆ&6µ÷VW'“ ¢v—BWFFRæ6ÆÆ&6µ÷VW'’æVF—EöÖW76vU÷FW‡B€¢FW‡BÀ¢&WÇ•öÖ&·WÖ'V–ÆEöÖ–åöÖVçR€¢6W76–öâÀ¢Ö–åöÖVçR‚$DÔ”â"–b&öÆRÓÒ$õtäU""VÇ6R&öÆR’À¢’À¢¢VÇ6S ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢FW‡BÀ¢&WÇ•öÖ&·WÖ'V–ÆEöÖ–åöÖVçR€¢6W76–öâÀ¢Ö–åöÖVçR‚$DÔ”â"–b&öÆRÓÒ$õtäU""VÇ6R&öÆR’À¢’À¢  ¦7–æ2FVb7F'B‡WFFS¢WFFRÂ6öçFW‡C¢6öçFW‡EG—W2äDTdTÅEõE•R“ ¢FVÆVw&Õ÷W6W%ö–BÒWFFRæVffV7F—fU÷W6W"æ–@ ¢–bv—B†æFÆU÷6†÷v66U÷7F'B‡WFFRÂ6öçFW‡B“ ¢&WGW&à ¢27W7FöÖW"FVWÆ–æ·2×W7B&R†æFÆVB$Tdõ$P¢2ÖævVÖVçB×6W76–öâöÆöv–â&÷WF–ærà¢–bv—B†æFÆU÷f—6—E÷7F'B€¢WFFRÀ¢6öçFW‡BÀ¢“ ¢&WGW&à ¢v—B6öçFW‡Bæ&÷Bç6WEö6†EöÖVçUö'WGFöâ€¢6†Eö–C×WFFRæVffV7F—fUö6†Bæ–BÀ¢ÖVçUö'WGFöãÔÖVçT'WGFöä6öÖÖæG2‚’À¢ ¢6W76–öâÒvWE÷6W76–öâ‡FVÆVw&Õ÷W6W%ö–B ¢–b6W76–öã ¢6ÆVçWÒv—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ÈR"À¢&WÇ•öÖ&·WÕ&WÇ”¶W–&ö&E&VÖ÷fR‚’À¢¢G'“ ¢v—B6ÆVçWæFVÆWFR‚¢W†6WBW†6WF–öã ¢70 ¢v—B6†÷uöÖ–åöÖVçR‡WFFRÂ6W76–öâ¢&WGW&à ¢2V&Æ–27W7FöÖW"ÖöFS¢÷7F'B÷Vç26†„f–ÆR6Ö'B7W7FöÖW"fÆ÷rà¢2ÖævW'26â7F–ÆÂW6RöÆöv–âW‡Æ–6—FÇ’à¢6öçFW‡BçW6W%öFFç÷‚&Æöv–å÷7FW"ÂæöæR¢6öçFW‡BçW6W%öFFç÷‚&Æöv–å÷W6W&æÖR"ÂæöæR¢v—B†æFÆU÷6Ö'Eö7W7FöÖW%÷7F'B‡WFFRÂ6öçFW‡B¢&WGW&à  ¦7–æ2FVbÆöv–åö6öÖÖæB‡WFFS¢WFFRÂ6öçFW‡C¢6öçFW‡EG—W2äDTdTÅEõE•R“ ¢FVÆVw&Õ÷W6W%ö–BÒWFFRæVffV7F—fU÷W6W"æ–@¢6W76–öâÒvWE÷6W76–öâ‡FVÆVw&Õ÷W6W%ö–B ¢–b6W76–öã ¢v—B6†÷uöÖ–åöÖVçR‡WFFRÂ6W76–öâ¢&WGW&à ¢6öçFW‡BçW6W%öFFæ6ÆV"‚¢6öçFW‡BçW6W%öFF²&Æöv–å÷7FW%ÒÒ'W6W&æÖR  ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢/	ùI˜‹˜ŠòŠ˜r‹=¸Í‹=Š­˜UÆåÆâ ¢-˜mŠ}˜RªŠ}‹Š‹¸ÂŠí˜Šò‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠó¢ ¢  ¦7–æ2FVbÆöv÷WB‡WFFS¢WFFRÂ6öçFW‡C¢6öçFW‡EG—W2äDTdTÅEõE•R“ ¢FVÆVw&Õ÷W6W%ö–BÒWFFRæVffV7F—fU÷W6W"æ–@¢Æöv÷WE÷6W76–öâ‡FVÆVw&Õ÷W6W%ö–B¢6öçFW‡BçW6W%öFFæ6ÆV"‚ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ÈRŠ}‹"Šİ‹=Š}Š‚ªŠ}‹Š‹¸ÂŠíŠ}‹ŠÂ‹MŠı¸ÍŠòåÆåÆâ ¢-Š‹Š}¸Â˜‹˜Šò˜]ŠÍŠıŠòöÆöv–â‹ŠrŠ‹-˜m¸ÍŠòâ ¢  ¦7–æ2FVbFW‡Eö†æFÆW"‡WFFS¢WFFRÂ6öçFW‡C¢6öçFW‡EG—W2äDTdTÅEõE•R“ ¢FVÆVw&Õ÷W6W%ö–BÒWFFRæVffV7F—fU÷W6W"æ–@¢6W76–öâÒvWE÷6W76–öâ‡FVÆVw&Õ÷W6W%ö–B ¢–bæ÷B6W76–öâæBæ÷B6öçFW‡BçW6W%öFFævWB‚&Æöv–å÷7FW"“ ¢–bv—B†æFÆU÷6Ö'Eö7W7FöÖW%÷FW‡B‡WFFRÂ6öçFW‡B“ ¢&WGW&à ¢–bæ÷B6W76–öã ¢7FWÒ6öçFW‡BçW6W%öFFævWB‚&Æöv–å÷7FW" ¢–b7FWÓÒ'W6W&æÖR# ¢6öçFW‡BçW6W%öFF²&Æöv–å÷W6W&æÖR%ÒÒWFFRæÖW76vRçFW‡Bç7G&—‚¢6öçFW‡BçW6W%öFF²&Æöv–å÷7FW%ÒÒ'77v÷&B  ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B‚/	ùI‹˜]‹"‹Š˜‹‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠó¢"¢&WGW&à ¢–b7FWÓÒ'77v÷&B# ¢W6W&æÖRÒ6öçFW‡BçW6W%öFFævWB‚&Æöv–å÷W6W&æÖR"¢77v÷&BÒWFFRæÖW76vRçFW‡@ ¢G'“ ¢v—BWFFRæÖW76vRæFVÆWFR‚¢W†6WBW†6WF–öã ¢70 ¢W6W"ÒWF†VçF–6FR‡W6W&æÖRÂ77v÷&B ¢–bæ÷BW6W# ¢f–æÅöVF—EöÆöv–â„æöæRÂfÇ6RÂW6W&æÖS×W6W&æÖRÂFVÆVw&Õ÷W6W%ö–C×FVÆVw&Õ÷W6W%ö–B¢6öçFW‡BçW6W%öFFæ6ÆV"‚¢6öçFW‡BçW6W%öFF²&Æöv–å÷7FW%ÒÒ'W6W&æÖR  ¢v—BWFFRæVffV7F—fUö6†Bç6VæEöÖW76vR€¢.)ØÂ˜mŠ}˜RªŠ}‹Š‹¸Â¸ÍŠr‹˜]‹"‹Š˜‹Š}‹MŠ­ŠŠ}˜rŠ}‹=Š¢åÆåÆâ ¢-˜mŠ}˜RªŠ}‹Š‹¸Â‹ŠrŠı˜ŠŠ}‹˜rŠ}‹‹=Š}˜Bª˜m¸ÍŠó¢ ¢¢&WGW&à ¢7&VFU÷6W76–öâ‡W6W%²&–B%ÒÂFVÆVw&Õ÷W6W%ö–B¢f–æÅöVF—EöÆöv–â‡W6W%²&–B%ÒÂG'VRÂW6W&æÖS×W6W&æÖRÂFVÆVw&Õ÷W6W%ö–C×FVÆVw&Õ÷W6W%ö–B¢6öçFW‡BçW6W%öFFæ6ÆV"‚ ¢6W76–öâÒvWE÷6W76–öâ‡FVÆVw&Õ÷W6W%ö–B ¢v—BWFFRæVffV7F—fUö6†Bç6VæEöÖW76vR‚.)ÈR˜‹˜Šò˜]˜˜˜"" ¢òÂF%÷W6W&æÖRÂF—7Æ•öæÖRÂ&öÆRÒ6W76–öà¢æÖRÒF—7Æ•öæÖR÷"F%÷W6W&æÖP ¢v—BWFFRæVffV7F—fUö6†Bç6VæEöÖW76vR€¢b/	øúŠí˜‹BŠ-˜]Šı¸ÍŠò¶æÖWÕÆåÆâ ¢-›í˜m˜B˜]Šı¸Í‹¸ÍŠ¢˜‚Š}˜mŠ­‹MŠ}‹˜Š}¸Í˜N(Í˜}Š}¸ÂŠ}˜]˜MŠ}ª’"À¢&WÇ•öÖ&·WÖ'V–ÆEöÖ–åöÖVçR€¢6W76–öâÀ¢Ö–åöÖVçR‚$DÔ”â"–b&öÆRÓÒ$õtäU""VÇ6R&öÆR’À¢’À¢¢&WGW&à ¢6öçFW‡BçW6W%öFF²&Æöv–å÷7FW%ÒÒ'W6W&æÖR  ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢/	ùIŠ}ŠŠ­ŠıŠr˜Š}‹Šò‹=¸Í‹=Š­˜R‹M˜¸ÍŠòåÆåÆâ ¢-˜mŠ}˜RªŠ}‹Š‹¸ÂŠí˜Šò‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠó¢ ¢¢&WGW&à ¢7FFRÒ6öçFW‡BçW6W%öFFævWB‚'7FFR" ¢24„ääTÅôd”ÄUôTD•Dõ%õDU…Eõ$õUDU%õc¢–bv—B†æFÆUöf–ÆUöVF—F÷%÷FW‡B€¢WFFRÀ¢6öçFW‡BÀ¢6W76–öâÀ¢ÆÆ÷vVCÖ6†V6µö7F–öåö66W72‡6W76–öâÂ&6†ææVÅöVF—B"’À¢“ ¢&WGW&à ¢24„„d”ÄUõ4Ô%EôÔätU%õDU…Eõ$õUDU%õc¢–bv—B†æFÆU÷6Ö'EöÖævW%÷FW‡B‡WFFRÂ6öçFW‡BÂ6W76–öâ“ ¢&WGW&à ¢2d”äÅôdTEU$U5õDU…Eõ$õUDU ¢–bv—B†æFÆUöf–æÅöW…÷FW‡B‡WFFRÂ6öçFW‡BÂ6W76–öâ“ ¢&WGW&à¢–bv—B†æFÆUöf–æÅ÷FW‡B‡WFFRÂ6öçFW‡BÂ6W76–öâ“ ¢&WGW&à ¢25$ÕõDU…Eõ$õUDU%õc¢–bv—B†æFÆUö7&Õ÷FW‡B€¢WFFRÀ¢6öçFW‡BÀ¢6W76–öâÀ¢“ ¢&WGW&à ¢–b7FFRÓÒ&GG&–'WFU÷'VÆUöÆ&VÂ# ¢fÇVRÒWFFRæÖW76vRçFW‡Bç7G&—‚ ¢6öçFW‡BçW6W%öFF²&GG&–'WFU÷'VÆUöÆ&VÂ%ÒÒfÇVP¢6öçFW‡BçW6W%öFFç÷€¢&GG&–'WFU÷'VÆUöÖF6…÷G—R"À¢æöæRÀ¢¢6öçFW‡BçW6W%öFF²'7FFR%ÒÒæöæP ¢¶W–&ö&BÒ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈR˜ŠÍ˜Šò‹ŠŠ}‹Š¢"À¢6ÆÆ&6µöFFÒ&GG&–'WFU÷'VÆU÷G—Uõ$U4Tä4R"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù9Ò˜]˜-ŠıŠ}‹Š‹ŠòŠ}‹"‹ŠŠ}‹Š¢"À¢6ÆÆ&6µöFFÒ&GG&–'WFU÷'VÆU÷G—UõdÅTUôeDU""À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùJ"‹ŠıŠò˜-Š˜BŠ}‹"‹ŠŠ}‹Š¢"À¢6ÆÆ&6µöFFÒ&GG&–'WFU÷'VÆU÷G—UôåTÔ$U%ô$Tdõ$R"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ&GG&–'WFU÷'VÆW2"À¢¢ÒÀ¢Ğ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b-˜]‹MŠí‹]˜s¢·fÇVWÕÆåÆâ ¢-‹˜‹BŠ}‹=Š­Ší‹Š}ŠÂ‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠó¢"À¢&WÇ•öÖ&·WÖ¶W–&ö&BÀ¢¢&WGW&à ¢–b7FFRÓÒ&GG&–'WFU÷'VÆU÷G&–vvW"# ¢G&–vvW"ÒWFFRæÖW76vRçFW‡Bç7G&—‚ ¢Æ&VÂÒ6öçFW‡BçW6W%öFFævWB€¢&GG&–'WFU÷'VÆUöÆ&VÂ ¢ ¢ÖF6…÷G—RÒ6öçFW‡BçW6W%öFFævWB€¢&GG&–'WFU÷'VÆUöÖF6…÷G—R ¢ ¢–bæ÷BÆ&VÂ÷"æ÷BÖF6…÷G—S ¢6öçFW‡BçW6W%öFFç÷‚'7FFR"ÂæöæR ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂŠ}‹}˜MŠ}‹Š}Š¢'VÆRªŠ}˜]˜B˜m¸Í‹=Š¢â"À¢&WÇ•öÖ&·WÖGG&–'WFU÷'VÆW5öÖVçR‚’À¢¢&WGW&à ¢¶W’Ò€¢&7W7FöÕò ¢²†6†Æ–"ç6†€¢Æ&VÂæVæ6öFR‚'WFbÓ‚"¢’æ†W†F–vW7B‚•³£%Ğ¢ ¢FVfVÇE÷fÇVRÒ€¢-ŠıŠ}‹Šò ¢–bÖF6…÷G—RÓÒ%$U4Tä4R ¢VÇ6RæöæP¢ ¢6fVE÷'VÆRÒ7&VFUöGG&–'WFU÷'VÆR€¢GG&–'WFUö¶W“Ö¶W’À¢GG&–'WFUöÆ&VÃÖÆ&VÂÀ¢ÖF6…÷G—SÖÖF6…÷G—RÀ¢G&–vvW%÷FW‡C×G&–vvW"À¢FVfVÇE÷fÇVSÖFVfVÇE÷fÇVRÀ¢F—7Æ•ö÷&FW#ÓÀ¢ ¢–bæ÷B6fVE÷'VÆS ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂ‹Ší¸Í‹˜r˜]‹MŠí‹]˜rŠı‹Šı¸ÍŠ­Š}Š¸Í‹2˜mŠ}˜]˜˜˜"Š˜Šòâ ¢¢&WGW&à ¢6öçFW‡BçW6W%öFFç÷‚'7FFR"ÂæöæR¢6öçFW‡BçW6W%öFFç÷€¢&GG&–'WFU÷'VÆUöÆ&VÂ"À¢æöæRÀ¢¢6öçFW‡BçW6W%öFFç÷€¢&GG&–'WFU÷'VÆUöÖF6…÷G—R"À¢æöæRÀ¢ ¢G—UöæÖW2Ò°¢%$U4Tä4R#¢-˜ŠÍ˜Šò‹ŠŠ}‹Š¢"À¢%dÅTUôeDU"#¢-˜]˜-ŠıŠ}‹Š‹ŠòŠ}‹"‹ŠŠ}‹Š¢"À¢$åTÔ$U%ô$Tdõ$R#¢-‹ŠıŠò˜-Š˜BŠ}‹"‹ŠŠ}‹Š¢"À¢Ğ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b.)ÈR˜]‹MŠí‹]˜r*·¶Æ&VÇÜ+²‹Ší¸Í‹˜r‹MŠòåÆåÆâ ¢b-‹˜‹BŠ}‹=Š­Ší‹Š}ŠÃ¢·G—UöæÖW5¶ÖF6…÷G—U×ÕÆâ ¢b-‹ŠŠ}‹Š¢Š­‹MŠí¸Í‹S¢·G&–vvW'Ò"À¢&WÇ•öÖ&·WÖGG&–'WFU÷'VÆW5öÖVçR‚’À¢¢&WGW&à ¢–b7FFRÓÒ&f–ÆU÷fÆ–F—G•÷vVV·5ö–çWB# ¢fÇVRÒ€¢WFFRæÖW76vRçFW‡@¢ç7G&—‚¢ç&WÆ6R‚-»"Â#"¢ç&WÆ6R‚-»"Â#"¢ç&WÆ6R‚-»""Â#""¢ç&WÆ6R‚-»2"Â#2"¢ç&WÆ6R‚-»B"Â#B"¢ç&WÆ6R‚-»R"Â#R"¢ç&WÆ6R‚-»b"Â#b"¢ç&WÆ6R‚-»r"Â#r"¢ç&WÆ6R‚-»‚"Â#‚"¢ç&WÆ6R‚-»’"Â#’"¢ ¢G'“ ¢vVV·2Ò–çB‡fÇVR¢W†6WBfÇVTW'&÷# ¢vVV·2Ò  ¢–bvVV·2Â÷"vVV·2âC ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂ‹ŠıŠò˜]‹Š­Š‹˜Š}‹Šòª˜m¸ÍŠòåÆåÆâ ¢-˜]ŠıŠ¢Š}‹Š­ŠŠ}‹ŠŠ}¸ÍŠòŠ¸Í˜bŠ­ŠrB˜}˜Š­˜rŠŠ}‹MŠòâ ¢¢&WGW&à ¢W‡—&VE÷&÷w2ÒvWEöW‡—&VE÷&÷W'G•÷&÷w2€¢vVV·0¢ ¢W‡—&VEö6÷VçBÒÆVâ€¢W‡—&VE÷&÷w0¢ ¢6öçFW‡BçW6W%öFFç÷€¢'7FFR"À¢æöæRÀ¢ ¢6öçFW‡BçW6W%öFF°¢'VæF–æuöf–ÆU÷fÆ–F—G•÷vVV·2 ¢ÒÒvVV·0 ¢–bW‡—&VEö6÷VçC ¢v&æ–æu÷FW‡BÒ€¢b.)ªûˆò˜]ŠıŠ¢Š}‹Š­ŠŠ}‹ŠÍŠı¸ÍŠó¢·vVV·7Ò˜}˜Š­˜uÆåÆâ ¢b-ŠŠrŠ}‹˜]Š}˜BŠ}¸Í˜bŠ­˜m‹¸Í˜]ˆÂ ¢b'¶W‡—&VEö6÷VçGÒ˜Š}¸Í˜B˜-Šı¸Í˜]¸Î(ÍŠ­‹Š}‹" ¢b'·vVV·7Ò˜}˜Š­˜rŠ}‹"ªŠ}˜mŠ}˜BŠ}‹]˜M¸ÂŠİ‹˜˜]¸Î(Í‹M˜˜mŠò ¢-˜‚Š˜r‹=‹}˜BŠ-‹M‹­Š}˜B˜]˜mŠ­˜-˜BŠí˜Š}˜}˜mŠò‹MŠòåÆåÆâ ¢-Š}‹}˜MŠ}‹Š}Š¢Š-˜n(Í˜}ŠrŠ}‹"Šı¸ÍŠ­Š}Š¸Í‹2›íŠ}ª’˜m˜]¸Î(Í‹M˜Šò ¢-˜‚Š‹ŠıŠ}˜²˜-Š}Š˜BŠŠ}‹-¸ÍŠ}Š¸Â˜}‹=Š­˜mŠòåÆåÆâ ¢-Š-¸ÍŠr˜]‹}˜]Šm˜b˜}‹=Š­¸ÍŠı‰ò ¢¢VÇ6S ¢v&æ–æu÷FW‡BÒ€¢b/	ùy2˜]ŠıŠ¢Š}‹Š­ŠŠ}‹ŠÍŠı¸ÍŠó¢·vVV·7Ò˜}˜Š­˜uÆåÆâ ¢-Šı‹ŠİŠ}˜BŠİŠ}‹m‹˜}¸Í¨b˜Š}¸Í˜B˜‹Š}˜M¸Â ¢-˜-Šı¸Í˜]¸Î(ÍŠ­‹Š}‹"Š}¸Í˜bŠŠ}‹-˜r˜ŠÍ˜Šò˜mŠıŠ}‹ŠòåÆåÆâ ¢-Š-¸ÍŠrŠ}¸Í˜b˜]˜-ŠıŠ}‹‹Ší¸Í‹˜r‹M˜Šı‰ò ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢v&æ–æu÷FW‡BÀ¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈRŠ­Š=¸Í¸ÍŠò˜m˜}Š}¸Í¸Â"À¢6ÆÆ&6µöFFÒ€¢b&f–ÆU÷fÆ–F—G•ö6öæf—&Õ÷·vVV·7Ò ¢’À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂŠ}˜m‹]‹Š}˜"À¢6ÆÆ&6µöFFÒ'6WGF–æw5öf–ÆU÷fÆ–F—G’"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7FFRÓÒ&fö÷FW%ö6öçF7EöæÖR# ¢fÇVRÒWFFRæÖW76vRçFW‡Bç7G&—‚ ¢–bæ÷BfÇVS ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂ˜mŠ}˜R˜m˜]¸Î(ÍŠ­˜Š}˜mŠòŠíŠ}˜M¸ÂŠŠ}‹MŠòåÆåÆâ ¢-˜mŠ}˜R‹MŠí‹R‹ŠrŠı˜ŠŠ}‹˜rŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ ¢¢&WGW&à ¢–bÆVâ‡fÇVR’â# ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂ˜mŠ}˜RŠ¸Í‹BŠ}‹"ŠİŠò‹}˜˜MŠ}˜m¸ÂŠ}‹=Š¢åÆåÆâ ¢-˜mŠ}˜Rª˜Š­Š}˜~(ÍŠ­‹¸ÂŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ ¢¢&WGW&à ¢6öçFW‡BçW6W%öFF°¢&fö÷FW%ö6öçF7E÷VæF–æuöæÖR ¢ÒÒfÇVP¢6öçFW‡BçW6W%öFF°¢'7FFR ¢ÒÒ&fö÷FW%ö6öçF7E÷†öæR  ¢W'6öåöçVÖ&W"ÒÆVâ€¢6öçFW‡BçW6W%öFFævWB€¢&fö÷FW%ö6öçF7E÷V÷ÆUöG&gB"À¢µÒÀ¢¢’² ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b/	ù9â‹M˜]Š}‹˜rŠ­˜]Š}‹2‹MŠí‹R·W'6öåöçVÖ&W'Ò‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòåÆåÆâ ¢b-˜mŠ}˜S¢·fÇVWÒ ¢¢&WGW&à ¢–b7FFRÓÒ&fö÷FW%ö6öçF7E÷†öæR# ¢VæF–æuöæÖRÒ6öçFW‡BçW6W%öFFævWB€¢&fö÷FW%ö6öçF7E÷VæF–æuöæÖR ¢ ¢–bæ÷BVæF–æuöæÖS ¢6öçFW‡BçW6W%öFFæ6ÆV"‚¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂŠ}‹}˜MŠ}‹Š}Š¢˜]ŠíŠ}‹}Š‚˜mŠ}˜-‹R‹MŠòâŠı˜ŠŠ}‹˜rŠ­˜MŠ}‹Bª˜m¸ÍŠòâ"À¢&WÇ•öÖ&·WÖfö÷FW%÷6WGF–æw5öÖVçR‚’À¢¢&WGW&à ¢G'“ ¢†öæRÒöæ÷&ÖÆ—¦Uö6öçF7E÷†öæR€¢WFFRæÖW76vRçFW‡@¢¢W†6WBfÇVTW'&÷"2W†3 ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b.)ØÂ¶W†7ÕÆåÆâ ¢-‹M˜]Š}‹˜rŠ­˜]Š}‹2‹ŠrŠı˜ŠŠ}‹˜rŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ ¢¢&WGW&à ¢V÷ÆRÒ6öçFW‡BçW6W%öFFç6WFFVfVÇB€¢&fö÷FW%ö6öçF7E÷V÷ÆUöG&gB"À¢µÒÀ¢¢V÷ÆRæVæB€¢°¢&æÖR#¢VæF–æuöæÖRÀ¢'†öæR#¢†öæRÀ¢Ğ¢ ¢6öçFW‡BçW6W%öFFç÷€¢&fö÷FW%ö6öçF7E÷VæF–æuöæÖR"À¢æöæRÀ¢¢6öçFW‡BçW6W%öFF°¢'7FFR ¢ÒÒæöæP ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ÈRŠ}¸Í˜b‹MŠí‹RŠ}‹mŠ}˜˜r‹MŠòåÆåÆâ ¢²f÷&ÖEö6öçF7E÷V÷ÆR‡V÷ÆR¢²%ÆåÆíŠ-¸ÍŠr‹MŠí‹RŠı¸Íªı‹¸Â˜}˜R˜}‹=Š­‰ò"À¢&WÇ•öÖ&·WÕö6öçF7E÷V÷ÆU÷VW7F–öåö¶W–&ö&B‚’À¢¢&WGW&à  ¢–b7FFRÓÒ&VF—E÷V&Æ—6…÷6WGF–ær# ¢6WGF–æuö¶W’Ò6öçFW‡BçW6W%öFFævWB€¢'6WGF–æuö¶W’ ¢ ¢fÇVRÒWFFRæÖW76vRçFW‡Bç7G&—‚ ¢–bæ÷B6WGF–æuö¶W“ ¢6öçFW‡BçW6W%öFFç÷‚'7FFR"ÂæöæR ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂŠ­˜m‹¸Í˜R˜]˜‹Šı˜m‹‹˜]‹MŠí‹R˜m¸Í‹=Š¢â"À¢&WÇ•öÖ&·W×6WGF–æw5öÖVçR‚’À¢¢&WGW&à ¢öÆE÷fÇVRÒvWEö÷6WGF–ær€¢6WGF–æuö¶W’À¢æöæRÀ¢ ¢6fUö÷6WGF–ær€¢6WGF–æuö¶W’À¢fÇVRÀ¢ ¢Æ&VÂÒT$Ä•4…õ4UED”äuôÄ$TÅ2ævWB€¢6WGF–æuö¶W’À¢6WGF–æuö¶W’À¢ ¢6öçFW‡BçW6W%öFFç÷‚'7FFR"ÂæöæR¢6öçFW‡BçW6W%öFFç÷‚'6WGF–æuö¶W’"ÂæöæR ¢–b6WGF–æuö¶W’–â°¢'V&Æ—6…öfö÷FW""À¢&6öçF7E÷FVÆVw&Ò"À¢Ó ¢VF—Eöfö÷FW%÷6WGF–æuö6†ævR€¢6W76–öå³ÒÀ¢6WGF–æuö¶W’À¢öÆE÷fÇVRÀ¢fÇVRÀ¢¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢fö÷FW%ö'VÆµööffW%÷FW‡B€¢b.)ÈR¶Æ&VÇÒ‹Ší¸Í‹˜r‹MŠòåÆåÆâ ¢b-˜]˜-ŠıŠ}‹ŠÍŠı¸ÍŠó¥Æç·fÇVWÒ ¢’À¢&WÇ•öÖ&·WÖfö÷FW%ö'VÆµööffW%öÖ&·W‚’À¢¢VÇ6S ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b.)ÈR¶Æ&VÇÒ‹Ší¸Í‹˜r‹MŠòåÆåÆâ ¢b-˜]˜-ŠıŠ}‹ŠÍŠı¸ÍŠó¥Æç·fÇVWÒ"À¢&WÇ•öÖ&·W×6WGF–æw5öÖVçR‚’À¢¢&WGW&à ¢–b7FFRÓÒ&f÷&6UöFVÆWFU÷Væ—Eö6öFR# ¢Væ—Eö6öFRÒ€¢WFFRæÖW76vRçFW‡@¢ç7G&—‚¢ç&WÆ6R‚-»"Â#"¢ç&WÆ6R‚-»"Â#"¢ç&WÆ6R‚-»""Â#""¢ç&WÆ6R‚-»2"Â#2"¢ç&WÆ6R‚-»B"Â#B"¢ç&WÆ6R‚-»R"Â#R"¢ç&WÆ6R‚-»b"Â#b"¢ç&WÆ6R‚-»r"Â#r"¢ç&WÆ6R‚-»‚"Â#‚"¢ç&WÆ6R‚-»’"Â#’"¢ ¢&÷rÒf–æE÷&÷W'G•ö'•÷Væ—Eö6öFR€¢Væ—Eö6öFP¢ ¢–bæ÷B&÷s ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b.)ØÂ˜Š}¸Í˜M¸ÂŠŠrªŠò˜Š}ŠİŠò·Væ—Eö6öFWÒ›í¸ÍŠıŠr˜m‹MŠòåÆåÆâ ¢-ªŠò‹ŠrŠı˜ŠŠ}‹˜rŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢€¢&÷W'G•ö–BÀ¢f÷VæE÷Væ—Eö6öFRÀ¢F—7G&–7BÀ¢FG&W75÷FW‡BÀ¢&VöÓ"À¢&–6Uö&–ÆÆ–öâÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢6†ææVÅ÷÷7E÷W&ÂÀ¢’Ò&÷p ¢–bæ÷BV&Æ—6†VEöÖW76vUö–C ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ªûˆòŠ}¸Í˜b˜Š}¸Í˜BŠı‹ªŠ}˜mŠ}˜BŠ}‹]˜M¸Â›í‹=Š¢˜]˜mŠ­‹M‹‹MŠı˜~(ÍŠ}¸Â˜mŠıŠ}‹ŠòåÆåÆâ ¢-Š}ªı‹˜}Šı˜Š­Š}˜bŠİ‹˜Š-˜bŠ}‹"‹]˜Š}˜mŠ­‹MŠ}‹Š}‹=Š­ˆÂ ¢-Š}¸Í˜b‹˜]˜M¸ÍŠ}Š¢‹ŠrŠÍŠıŠ}ªıŠ}˜m˜rŠ}˜mŠÍŠ}˜R˜]¸Î(ÍŠı˜}¸Í˜Râ"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢6öçFW‡BçW6W%öFFç÷€¢'7FFR"À¢æöæRÀ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.(û2Šı‹ŠİŠ}˜BŠİ‹˜˜˜‹¸Â˜Š}¸Í˜BŠ}‹"ªŠ}˜mŠ}˜Bâââ ¢ ¢G'“ ¢&W7VÇBÒv—Bf÷&6UöFVÆWFU÷&÷W'G’€¢6öçFW‡Bæ&÷BÀ¢&÷W'G•ö–BÀ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ÈR˜Š}¸Í˜B˜˜‹Š}˜²Šİ‹˜‹MŠòåÆåÆâ ¢b-ªŠò˜Š}ŠİŠó¢·&W7VÇE²wVæ—Eö6öFRu×ÕÆâ ¢b-›í¸ÍŠ}˜^(Í˜}Š}¸ÂŠİ‹˜(Í‹MŠı˜s¢ ¢b'·&W7VÇE²vFVÆWFVEöÖW76vW2u×ÕÆâ ¢-˜‹m‹¸ÍŠ¢˜Š}¸Í˜C¢‹­¸Í‹˜‹Š}˜EÆâ ¢-Šı‹=Š­˜~(ÍŠ˜mŠı¸Î(Í˜}Šr˜m¸Í‹"Š‹˜‹-‹‹=Š}˜m¸Â‹MŠı˜mŠòâ"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢ ¢W†6WBW†6WF–öâ2W†3 ¢Æövv–æræW†6WF–öâ€¢$f÷&6RFVÆWFRf–ÆVC¢ ¢'&÷W'G•ö–CÒW2Væ—Eö6öFSÒW2"À¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂŠİ‹˜˜˜‹‹2ªŠ}˜]˜BŠ}˜mŠÍŠ}˜R˜m‹MŠòåÆåÆâ ¢b-Ší‹}Šs¢·7G"†W†2•³£s×Ò"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢ ¢&WGW&à ¢–b7FFRÓÒ'&WV&Æ—6…öw&÷Wö7W7FöÕö6÷VçB# ¢&uö6÷VçBÒ€¢WFFRæÖW76vRçFW‡@¢ç7G&—‚¢ç&WÆ6R‚-»"Â#"¢ç&WÆ6R‚-»"Â#"¢ç&WÆ6R‚-»""Â#""¢ç&WÆ6R‚-»2"Â#2"¢m«ëŒ+Š×®º+º$zzb¥îreplace("Û´", "4")
            .replace("Ûµ", "5")
            .replace("Û¶", "6")
            .replace("Û·", "7")
            .replace("Û¸", "8")
            .replace("Û¹", "9")
        )

        try:
            count = int(raw_count)
        except ValueError:
            await update.message.reply_text(
                "âŒ Ù„Ø·ÙØ§Ù‹ ÙÙ‚Ø· ÛŒÚ© Ø¹Ø¯Ø¯ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
                "Ù…Ø«Ø§Ù„: 10",
                reply_markup=files_menu(),
            )
            return

        if count < 1 or count > 100:
            await update.message.reply_text(
                "âŒ ØªØ¹Ø¯Ø§Ø¯ Ø¨Ø§ÛŒØ¯ Ø¨ÛŒÙ† 1 ØªØ§ 100 ÙØ§ÛŒÙ„ Ø¨Ø§Ø´Ø¯.",
                reply_markup=files_menu(),
            )
            return

        context.user_data.pop("state", None)
        context.user_data["pending_group_republish_count"] = count

        await update.message.reply_text(
            "ğŸ” ØªØ£ÛŒÛŒØ¯ Ø¨Ø§Ø²Ù†Ø´Ø± Ú¯Ø±ÙˆÙ‡ÛŒ\n\n"
            f"ØªØ¹Ø¯Ø§Ø¯ Ø¯Ø±Ø®ÙˆØ§Ø³ØªÛŒ: {count} ÙØ§ÛŒÙ„\n\n"
            "ÙÙ‚Ø· ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ù‚Ø¯ÛŒÙ…ÛŒÙ Ù…Ø¹ØªØ¨Ø± Ø§Ù†ØªØ®Ø§Ø¨ Ù…ÛŒâ€ŒØ´ÙˆÙ†Ø¯.\n"
            "ÙØ§ÛŒÙ„ Ù…Ù†Ù‚Ø¶ÛŒØŒ ÙØ±ÙˆØ®ØªÙ‡â€ŒØ´Ø¯Ù‡ØŒ Ø­Ø°Ùâ€ŒØ´Ø¯Ù‡ ÛŒØ§ Ù…ÙˆØ¬ÙˆØ¯ Ø¯Ø± Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„ "
            "Ø§Ù†ØªØ®Ø§Ø¨ Ù†Ù…ÛŒâ€ŒØ´ÙˆØ¯.\n\n"
            "Ú©Ø¯ ÙˆØ§Ø­Ø¯ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ ØªØºÛŒÛŒØ± Ù†Ù…ÛŒâ€ŒÚ©Ù†Ø¯.\n"
            "Ø§ÛŒÙ† Ø¹Ù…Ù„ÛŒØ§Øª ÙØ§ÛŒÙ„ Ø¬Ø¯ÛŒØ¯ÛŒ Ø¯Ø± INPUT ÛŒØ§ SOURCE Ø§ÛŒØ¬Ø§Ø¯ Ù†Ù…ÛŒâ€ŒÚ©Ù†Ø¯.\n\n"
            "ØªØ£ÛŒÛŒØ¯ Ù…ÛŒâ€ŒÚ©Ù†ÛŒØ¯ØŸ",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "âœ… ØªØ£ÛŒÛŒØ¯ Ùˆ Ø§ÙØ²ÙˆØ¯Ù† Ø¨Ù‡ ØµÙ",
                            callback_data=f"republish_group_confirm_{count}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "âŒ Ø§Ù†ØµØ±Ø§Ù",
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
                f"âŒ ÙØ§ÛŒÙ„ÛŒ Ø¨Ø§ Ú©Ø¯ ÙˆØ§Ø­Ø¯ {unit_code} Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.\n\n"
                "Ú©Ø¯ Ø±Ø§ Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.",
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
                "âš ï¸ Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ù‡Ù†ÙˆØ² Ù‚Ø¨Ù„Ø§Ù‹ Ù…Ù†ØªØ´Ø± Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.\n"
                "Ø¨Ø±Ø§ÛŒ Ø§ÙˆÙ„ÛŒÙ† Ø§Ù†ØªØ´Ø§Ø± Ø§Ø² Â«Ø§Ù†ØªØ´Ø§Ø± ÙÙˆØ±ÛŒÂ» Ø§Ø³ØªÙØ§Ø¯Ù‡ Ú©Ù†ÛŒØ¯.",
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
            "ğŸ”„ Ù¾ÛŒØ´â€ŒÙ†Ù…Ø§ÛŒØ´ Ø¨Ø§Ø²Ù†Ø´Ø±\n\n"
            f"ØªØ¹Ø¯Ø§Ø¯ Ø¹Ú©Ø³â€ŒÙ‡Ø§: {photo_count}\n"
            f"Ù¾ÛŒØ§Ù… ÙØ¹Ù„ÛŒ: {published_message_id}\n\n"
            + preview
            + "\n\n"
            "âš ï¸ Ø¨Ø§Ø²Ù†Ø´Ø± Ø¨Ø§ Ù‡Ù…Ø§Ù† Ú©Ø¯ ÙˆØ§Ø­Ø¯ Ø§Ù†Ø¬Ø§Ù… Ù…ÛŒâ€ŒØ´ÙˆØ¯ "
            "Ùˆ Ú©Ø¯ Ø¬Ø¯ÛŒØ¯ÛŒ Ù…ØµØ±Ù Ù†Ù…ÛŒâ€ŒØ´ÙˆØ¯.",
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
                "âŒ Ø³Ø§Ø¹Øª Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.\n\n"
                "Ù…Ø«Ø§Ù„ ØµØ­ÛŒØ­:\n09:30"
            )
            return

        context.user_data[
            "schedule_start_time"
        ] = start_time

        context.user_data[
            "state"
        ] = "schedule_end_time"

        await update.message.reply_text(
            "ğŸ• Ø³Ø§Ø¹Øª Ù¾Ø§ÛŒØ§Ù† Ø¨Ø§Ø²Ù‡ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "Ù…Ø«Ø§Ù„:\n23:00"
        )
        return


    if state == "schedule_end_time":
        end_time = parse_schedule_time(
            update.message.text
        )

        if not end_time:
            await update.message.reply_text(
                "âŒ Ø³Ø§Ø¹Øª Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.\n\n"
                "Ù…Ø«Ø§Ù„ ØµØ­ÛŒØ­:\n23:00"
            )
            return

        context.user_data[
            "schedule_end_time"
        ] = end_time

        context.user_data[
            "state"
        ] = "schedule_pause"

        await update.message.reply_text(
            "â¸ Ø¢ÛŒØ§ ÙˆØ³Ø· Ø§ÛŒÙ† Ø¨Ø§Ø²Ù‡ Ø³Ø§Ø¹ØªÛŒ Ù‡Ø³Øª Ú©Ù‡ "
            "Ù†Ø®ÙˆØ§Ù‡ÛŒØ¯ Ù‡ÛŒÚ† ÙØ§ÛŒÙ„ÛŒ Ù…Ù†ØªØ´Ø± Ø´ÙˆØ¯ØŸ\n\n"
            "Ù…Ø«Ø§Ù„:\n14:00 ØªØ§ 17:00\n\n"
            "Ø§Ú¯Ø± ØªÙˆÙ‚Ù Ù†Ù…ÛŒâ€ŒØ®ÙˆØ§Ù‡ÛŒØ¯ Ø¹Ø¯Ø¯ 0 Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯."
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
                .replace("Ø§Ù„ÛŒ", "ØªØ§")
                .replace("â€“", "ØªØ§")
                .replace("â€”", "ØªØ§")
                .replace("-", "ØªØ§")
            )

            parts = [
                part.strip()
                for part in normalized.split("ØªØ§")
                if part.strip()
            ]

            if len(parts) != 2:
                await update.message.reply_text(
                    "âŒ ÙØ±Ù…Øª Ø¨Ø§Ø²Ù‡ ØªÙˆÙ‚Ù ØµØ­ÛŒØ­ Ù†ÛŒØ³Øª.\n\n"
                    "Ù…Ø«Ø§Ù„:\n14:00 ØªØ§ 17:00\n\n"
                    "Ø§Ú¯Ø± ØªÙˆÙ‚Ù Ù†Ù…ÛŒâ€ŒØ®ÙˆØ§Ù‡ÛŒØ¯: 0"
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
                    "âŒ Ø³Ø§Ø¹Øª ØªÙˆÙ‚Ù Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.\n\n"
                    "Ù…Ø«Ø§Ù„ ØµØ­ÛŒØ­:\n14:00 ØªØ§ 17:00"
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
                    "âŒ Ø¨Ø§Ø²Ù‡ ØªÙˆÙ‚Ù Ø¨Ø§ÛŒØ¯ Ú©Ø§Ù…Ù„Ø§Ù‹ Ø¯Ø§Ø®Ù„ "
                    "Ø¨Ø§Ø²Ù‡ Ø§ØµÙ„ÛŒ Ø§Ù†ØªØ´Ø§Ø± Ø¨Ø§Ø´Ø¯.\n\n"
                    "Ù…Ø«Ù„Ø§Ù‹ Ø§Ú¯Ø± Ø§Ù†ØªØ´Ø§Ø± 10:30 ØªØ§ 22:00 Ø§Ø³Øª:\n"
                    "14:00 ØªØ§ 17:00"
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
            "â± ÙØ§ØµÙ„Ù‡ Ø¨ÛŒÙ† Ù‡Ø± Ø§Ù†ØªØ´Ø§Ø± Ú†Ù†Ø¯ Ø¯Ù‚ÛŒÙ‚Ù‡ Ø¨Ø§Ø´Ø¯ØŸ\n\n"
            "Ù…Ø«Ø§Ù„:\n30"
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
                "âŒ ÙØ§ØµÙ„Ù‡ Ø§Ù†ØªØ´Ø§Ø± Ø¨Ø§ÛŒØ¯ Ø­Ø¯Ø§Ù‚Ù„ 1 Ø¯Ù‚ÛŒÙ‚Ù‡ Ø¨Ø§Ø´Ø¯."
            )
            return

        context.user_data[
            "schedule_interval"
        ] = interval_minutes

        context.user_data[
            "state"
        ] = "schedule_max_posts"

        await update.message.reply_text(
            "ğŸ“¦ Ø­Ø¯Ø§Ú©Ø«Ø± Ú†Ù†Ø¯ Ù¾Ø³Øª Ø¯Ø± Ø§ÛŒÙ† Ø¨Ø§Ø²Ù‡ Ù…Ù†ØªØ´Ø± Ø´ÙˆØ¯ØŸ\n\n"
            "Ø§Ú¯Ø± Ù…Ø­Ø¯ÙˆØ¯ÛŒØª Ù†Ù…ÛŒâ€ŒØ®ÙˆØ§Ù‡ÛŒØ¯ Ø¹Ø¯Ø¯ 0 Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "Ù…Ø«Ø§Ù„:\n10"
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
                "âŒ ÛŒÚ© Ø¹Ø¯Ø¯ ØµÙØ± ÛŒØ§ Ø¨ÛŒØ´ØªØ± Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯."
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
                "âŒ Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù†Ø§Ù‚Øµ Ø´Ø¯. "
                "Ù„Ø·ÙØ§Ù‹ Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø§Ø² Ù…Ù†ÙˆÛŒ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ø´Ø±ÙˆØ¹ Ú©Ù†ÛŒØ¯.",
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
                "âŒ Ø±ÙˆØ² Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù…Ø´Ø®Øµ Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
                reply_markup=back_menu(),
            )
            return

        max_text = (
            str(max_posts)
            if max_posts is not None
            else "Ù†Ø§Ù…Ø­Ø¯ÙˆØ¯"
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
                    "âŒ Ú¯Ø±ÙˆÙ‡ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
                    reply_markup=schedule_menu(),
                )
                return

            await update.message.reply_text(
                "âœ… Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù‡Ù…Ù‡ Ø±ÙˆØ²Ù‡Ø§ ÙˆÛŒØ±Ø§ÛŒØ´ Ø´Ø¯.\n\n"
                f"Ø³Ø§Ø¹Øª: {start_time.strftime('%H:%M')} "
                f"ØªØ§ {end_time.strftime('%H:%M')}\n"
                f"ÙØ§ØµÙ„Ù‡ Ø§Ù†ØªØ´Ø§Ø±: {interval_minutes} Ø¯Ù‚ÛŒÙ‚Ù‡\n"
                f"Ø³Ù‚Ù Ù¾Ø³Øª: {max_text}\n\n"
                f"ØªØ¹Ø¯Ø§Ø¯ Ø±ÙˆØ²Ù‡Ø§ÛŒ Ø¨Ø±ÙˆØ²Ø´Ø¯Ù‡: {updated_count}",
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
                    "âŒ Ø¨Ø§Ø²Ù‡ Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± Ø¨Ø±Ø§ÛŒ ÙˆÛŒØ±Ø§ÛŒØ´ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
                    reply_markup=schedule_menu(),
                )
                return

            day_name = WEEKDAY_NAMES.get(
                weekday,
                str(weekday),
            )

            context.user_data.clear()

            await update.message.reply_text(
                "âœ… Ø¨Ø§Ø²Ù‡ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ ÙˆÛŒØ±Ø§ÛŒØ´ Ø´Ø¯.\n\n"
                f"Ø´Ù†Ø§Ø³Ù‡: #{updated_id}\n"
                f"Ø±ÙˆØ²: {day_name}\n"
                f"Ø³Ø§Ø¹Øª: {start_time.strftime('%H:%M')} "
                f"ØªØ§ {end_time.strftime('%H:%M')}\n"
                f"ÙØ§ØµÙ„Ù‡ Ø§Ù†ØªØ´Ø§Ø±: {interval_minutes} Ø¯Ù‚ÛŒÙ‚Ù‡\n"
                f"Ø³Ù‚Ù Ù¾Ø³Øª: {max_text}",
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
                "âœ… Ø¨Ø§Ø²Ù‡ Ø¨Ø±Ø§ÛŒ Ù‡Ù…Ù‡ Ø±ÙˆØ²Ù‡Ø§ÛŒ Ù‡ÙØªÙ‡ Ø³Ø§Ø®ØªÙ‡ Ø´Ø¯.\n\n"
                f"ØªØ¹Ø¯Ø§Ø¯ Ø¨Ø§Ø²Ù‡â€ŒÙ‡Ø§: {len(created_ids)}\n"
                f"Ø³Ø§Ø¹Øª: {start_time.strftime('%H:%M')} "
                f"ØªØ§ {end_time.strftime('%H:%M')}\n"
                f"ÙØ§ØµÙ„Ù‡ Ø§Ù†ØªØ´Ø§Ø±: {interval_minutes} Ø¯Ù‚ÛŒÙ‚Ù‡\n"
                f"Ø³Ù‚Ù Ù¾Ø³Øª Ø¯Ø± Ù‡Ø± Ø±ÙˆØ²: {max_text}\n"
                "ÙˆØ¶Ø¹ÛŒØª Ù‡Ù…Ù‡ Ø¨Ø§Ø²Ù‡â€ŒÙ‡Ø§: ğŸŸ¢ ÙØ¹Ø§Ù„",
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
            "âœ… Ø¨Ø§Ø²Ù‡ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ø³Ø§Ø®ØªÙ‡ Ø´Ø¯.\n\n"
            f"Ø´Ù†Ø§Ø³Ù‡: #{window_id}\n"
            f"Ø±ÙˆØ²: {day_name}\n"
            f"Ø³Ø§Ø¹Øª: {start_time.strftime('%H:%M')} "
            f"ØªØ§ {end_time.strftime('%H:%M')}\n"
            f"ÙØ§ØµÙ„Ù‡ Ø§Ù†ØªØ´Ø§Ø±: {interval_minutes} Ø¯Ù‚ÛŒÙ‚Ù‡\n"
            f"Ø³Ù‚Ù Ù¾Ø³Øª: {max_text}\n"
            "ÙˆØ¶Ø¹ÛŒØª: ğŸŸ¢ ÙØ¹Ø§Ù„",
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
                "âŒ ÙØ§ÛŒÙ„ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.\n\n"
                "ÛŒÚ©ÛŒ Ø§Ø² Ø§ÛŒÙ† Ù…ÙˆØ§Ø±Ø¯ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯:\n"
                "â€¢ Ø´Ù…Ø§Ø±Ù‡ Ù¾ÛŒÚ¯ÛŒØ±ÛŒ ØµÙØŒ Ù…Ø«Ù„: ØµÙ 45\n"
                "â€¢ Ú©Ø¯ ÙØ§ÛŒÙ„ Ù…Ù†Ø¨Ø¹\n"
                "â€¢ Ù„ÛŒÙ†Ú© Ù‡Ù…Ø§Ù† Ù¾Ø³Øª Ø¯Ø± Ú©Ø§Ù†Ø§Ù„ Ù…Ù†Ø¨Ø¹\n"
                "â€¢ Message ID Ù¾Ø³Øª Ù…Ù†Ø¨Ø¹",
                reply_markup=back_menu(),
            )
            return

        context.user_data.pop("state", None)

        if len(rows) > 1:
            await update.message.reply_text(
                f"ğŸ” Ø¨Ø±Ø§ÛŒ Ú©Ø¯ {source_code} Ú†Ù†Ø¯ ÙØ§ÛŒÙ„ Ù¾ÛŒØ¯Ø§ Ø´Ø¯.\n"
                "ÙØ§ÛŒÙ„ Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:",
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
            "ğŸš¨ Ù¾ÛŒØ´â€ŒÙ†Ù…Ø§ÛŒØ´ Ø§Ù†ØªØ´Ø§Ø± ÙÙˆØ±ÛŒ\n\n"
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
                "âŒ Ø¹Ø¨Ø§Ø±Øª Ø¬Ø³ØªØ¬Ùˆ Ø®Ø§Ù„ÛŒ Ø§Ø³Øª."
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
                "Û°Û±Û²Û³Û´ÛµÛ¶Û·Û¸Û¹Ù Ù¡Ù¢Ù£Ù¤Ù¥Ù¦Ù§Ù¨Ù©",
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
                "âŒ Ø´Ù…Ø§Ø±Ù‡ ØµÙ Ù…Ø¹ØªØ¨Ø± Ù†ÛŒØ³Øª.\n\n"
                "Ù…Ø«Ø§Ù„:\n"
                "289\n"
                "ÛŒØ§\n"
                "ØµÙ 289"
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
                f"âŒ ÙØ§ÛŒÙ„ Â«ØµÙ {property_id}Â» "
                "Ø¯Ø± ØµÙ ÙØ¹Ø§Ù„ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.\n\n"
                "Ù…Ù…Ú©Ù† Ø§Ø³Øª ÙØ§ÛŒÙ„ Ù…Ù†ØªØ´Ø±ØŒ Ø±Ø¯ ÛŒØ§ Ø­Ø°Ù Ø´Ø¯Ù‡ Ø¨Ø§Ø´Ø¯.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØµÙ",
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
            .replace("Û°", "0")
            .replace("Û±", "1")
            .replace("Û²", "2")
            .replace("Û³", "3")
            .replace("Û´", "4")
            .replace("Ûµ", "5")
            .replace("Û¶", "6")
            .replace("Û·", "7")
          m«ëŒ+Š×®º+º$zzb¥âç&WÆ6R‚-»‚"Â#‚"¢ç&WÆ6R‚-»’"Â#’"¢ ¢&÷W'G•÷&÷rÂ6÷W&6U÷&÷w2Ò€¢vWE÷Væ—Eö6öFUögVÆÅöFWF–Ç2€¢Væ—Eö6öFP¢¢ ¢–bæ÷B&÷W'G•÷&÷s ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢b.)ØÂ˜Š}¸Í˜M¸ÂŠŠrªŠò˜Š}ŠİŠò·Væ—Eö6öFWÒ›í¸ÍŠıŠr˜m‹MŠòåÆåÆâ ¢-ªŠò‹ŠrŠı˜ŠŠ}‹˜rŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ"À¢&WÇ•öÖ&·WÖ&6µöÖVçR‚’À¢¢&WGW&à ¢6öçFW‡BçW6W%öFFç÷€¢'7FFR"À¢æöæRÀ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢'V–ÆE÷Væ—Eö6öFU÷6V&6…÷FW‡B€¢&÷W'G•÷&÷rÀ¢6÷W&6U÷&÷w2À¢6W76–öã×6W76–öâÀ¢’À¢&WÇ•öÖ&·WÖ&6µöÖVçR‚’À¢F—6&ÆU÷vV%÷vU÷&Wf–WsÕG'VRÀ¢&VE÷F–ÖV÷WCÓÀ¢6öææV7E÷F–ÖV÷WCÓ2À¢¢&WGW&à ¢–b7FFRÓÒ&6FVv÷'•÷&ævR# ¢&–6U÷&ævU÷FW‡BÒ€¢WFFRæÖW76vRçFW‡Bç7G&—‚¢ ¢'6VE÷&ævRÒ'6Uö6FVv÷'•÷&ævR€¢&–6U÷&ævU÷FW‡@¢ ¢–bæ÷B'6VE÷&ævS ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ØÂŠŠ}‹-˜r˜-¸Í˜]Š¢‹]Šİ¸ÍŠÒ˜m¸Í‹=Š¢åÆåÆâ ¢-˜]Š½Š}˜B‹]Šİ¸ÍŠÓ¥Æã‚Š­ŠrR ¢¢&WGW&à ¢Æ÷rÂ†–v‚Ò'6VE÷&ævP ¢&÷w2ÒvWE÷V&Æ—6†VE÷&÷W'F–W5ö–å÷&ævR€¢Æ÷rÀ¢†–v‚À¢ ¢F÷FÂÒÆVâ‡&÷w2 ¢–bF÷FÂÓÒ ¢6öçFW‡BçW6W%öFFç÷€¢'7FFR"À¢æöæRÀ¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢.)ªûˆò˜}¸Í¨b˜Š}¸Í˜B˜]˜mŠ­‹M‹‹MŠı˜~(ÍŠ}¸Â ¢-Šı‹Š}¸Í˜bŠŠ}‹-˜r˜-¸Í˜]Š¢›í¸ÍŠıŠr˜m‹MŠòâ"À¢&WÇ•öÖ&·WÖ&6µöÖVçR‚’À¢¢&WGW&à ¢6öçFW‡BçW6W%öFF°¢&6FVv÷'•öÆ÷r ¢ÒÒÆ÷p ¢6öçFW‡BçW6W%öFF°¢&6FVv÷'•ö†–v‚ ¢ÒÒ†–v€ ¢6öçFW‡BçW6W%öFFç÷€¢'7FFR"À¢æöæRÀ¢ ¢†6‡FrÒ'V–ÆEö6FVv÷'•ö†6‡Fr€¢Æ÷rÀ¢†–v‚À¢ ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢/	ùx"Šı‹=Š­˜~(ÍŠ˜mŠı¸ÂŠ-˜]Š}Šı˜rŠ}‹=Š¢åÆåÆâ ¢b'¶†6‡FwÕÆâ ¢b-Š­‹ŠıŠ}Šò˜Š}¸Í˜N(Í˜}Š}¸Â˜]˜mŠ­‹M‹‹MŠı˜s¢·F÷FÇÕÆåÆâ ¢-ªŠıŠ}˜Rªı‹˜˜rŠ­Š}¸Í¸ÂŠ˜rªŠ}˜mŠ}˜BŠ}‹‹=Š}˜B‹M˜Šı‰ò"À¢&WÇ•öÖ&·WÖ6FVv÷'•÷vW5ö¶W–&ö&B€¢F÷FÀ¢’À¢¢&WGW&à ¢v—BWFFRæÖW76vRç&WÇ•÷FW‡B€¢-Š}‹"Šıª˜]˜~(Í˜}Š}¸Â›í˜m˜B˜]Šı¸Í‹¸ÍŠ¢Š}‹=Š­˜Š}Šı˜rª˜m¸ÍŠòâ"À¢&WÇ•öÖ&·WÖ'V–ÆEöÖ–åöÖVçR‡6W76–öâÂÖ–åöÖVçR‚$DÔ”â"–b6W76–öå³5ÒÓÒ$õtäU""VÇ6R6W76–öå³5Ò’’À¢     ¦FVbf–æE÷&÷W'G•öf÷%÷6÷W&6U÷7–æ2€¢FVÆVw&Õö6†ææVÅö–BÀ¢6÷W&6UöÖW76vUö–BÀ¢6÷W&6U÷Væ—Eö6öFSÔæöæRÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W#  ¢7W"æW†V7WFR€¢"" ¢4TÄT5B–@¢e$ôÒ6†ææVÇ0¢t„U$RFVÆVw&Õö6†ææVÅö–BÒW0¢Ä”Ô•B¢"""À¢‡FVÆVw&Õö6†ææVÅö–BÂ’À¢ ¢6†ææVÅ÷&÷rÒ7W"æfWF6†öæR‚ ¢–bæ÷B6†ææVÅ÷&÷s ¢&WGW&âæöæP ¢6†ææVÅöF%ö–BÒ6†ææVÅ÷&÷u³Ğ ¢2’†–v†W7B&–÷&—G“ ¢2W†7B6÷W&6R6†ææVÂ²FVÆVw&ÒÖW76vR”Bà¢–b6÷W&6UöÖW76vUö–C ¢7W"æW†V7WFR€¢"" ¢4TÄT5Bæ–@¢e$ôÒ&÷W'G•÷6÷W&6W20¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒ2ç&÷W'G•ö–@¢t„U$R2ç6÷W&6Uö6†ææVÅö–BÒW0¢äB2ç6÷W&6UöÖW76vUö–BÒW0¢äB2æ—5ö7F—fRÒE%TP¢õ$DU"%¢2æ—5÷&–Ö'’DU42À¢2æ–BDU40¢Ä”Ô•B¢"""À¢€¢6†ææVÅöF%ö–BÀ¢6÷W&6UöÖW76vUö–BÀ¢’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢–b&÷s ¢&WGW&â&÷u³Ğ ¢2"’fÆÆ&6³ ¢26÷W&6R6öFR–ç6–FRF†R6ÖR6÷W&6R6†ææVÂà¢–b6÷W&6U÷Væ—Eö6öFS ¢7W"æW†V7WFR€¢"" ¢4TÄT5Bæ–@¢e$ôÒ&÷W'G•÷6÷W&6W20¢¤ô”â&÷W'F–W2 ¢ôâæ–BÒ2ç&÷W'G•ö–@¢t„U$R2ç6÷W&6Uö6†ææVÅö–BÒW0¢äB2ç6÷W&6U÷Væ—Eö6öFRÒW0¢äB2æ—5ö7F—fRÒE%TP¢õ$DU"%¢2æ—5÷&–Ö'’DU42À¢2æ–BDU40¢Ä”Ô•B¢"""À¢€¢6†ææVÅöF%ö–BÀ¢6÷W&6U÷Væ—Eö6öFRÀ¢’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢–b&÷s ¢&WGW&â&÷u³Ğ ¢&WGW&âæöæP  ¦FVb&WÆ6Uö6FVv÷'•÷÷7Eö—FV×2€¢6FVv÷'•÷÷7Eö–BÀ¢&÷w2À¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W#  ¢7W"æW†V7WFR€¢"" ¢DTÄUDRe$ôÒ6FVv÷'•÷÷7Eö—FV×0¢t„U$R6FVv÷'•÷÷7Eö–BÒW0¢"""À¢†6FVv÷'•÷÷7Eö–BÂ’À¢ ¢f÷"÷6—F–öâÂ&÷r–âVçVÖW&FR€¢&÷w2À¢7F'CÓÀ¢“ ¢&÷W'G•ö–BÒ&÷u³Ğ¢&–6U÷FöÖâÒ&÷u³…Ğ ¢7W"æW†V7WFR€¢"" ¢”å4U%B”åDò6FVv÷'•÷÷7Eö—FV×0¢€¢6FVv÷'•÷÷7Eö–BÀ¢&÷W'G•ö–BÀ¢÷6—F–öâÀ¢6æ6†÷E÷&–6U÷FöÖà¢¢dÅTU0¢‚W2ÂW2ÂW2ÂW2¢"""À¢€¢6FVv÷'•÷÷7Eö–BÀ¢&÷W'G•ö–BÀ¢÷6—F–öâÀ¢€¢–çB‡&–6U÷FöÖâ¢–b&–6U÷FöÖâ—2æ÷BæöæP¢VÇ6RæöæP¢’À¢’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDR6FVv÷'•÷÷7G0¢4UBWFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢†6FVv÷'•÷÷7Eö–BÂ’À¢ ¢6öæâæ6öÖÖ—B‚  ¦FVbFV7F—fFUö6FVv÷'•÷÷7B€¢6FVv÷'•÷÷7Eö–BÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W#  ¢7W"æW†V7WFR€¢"" ¢UDDR6FVv÷'•÷÷7G0¢4U@¢—5ö7F—fRÒdÅ4RÀ¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢†6FVv÷'•÷÷7Eö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢DTÄUDRe$ôÒ6FVv÷'•÷÷7Eö—FV×0¢t„U$R6FVv÷'•÷÷7Eö–BÒW0¢"""À¢†6FVv÷'•÷÷7Eö–BÂ’À¢ ¢6öæâæ6öÖÖ—B‚  ¦7–æ2FVböVF—Eö6FVv÷'•öÖW76vU÷v—F…÷&WG'’€¢&÷BÀ¢¢À¢6†Eö–BÀ¢ÖW76vUö–BÀ¢FW‡BÀ¢&WÇ•öÖ&·WÀ¢Ö…öGFV×G3ÓBÀ¢“ ¢"" ¢VF—B6FVv÷'’ÖW76vW2v—F‚WFöÖF–2&WG'’f÷ ¢FVÆVw&ÒfÆööB6öçG&öÂæBFV×÷&'’F–ÖV÷WG2à¢"" ¢GFV×BÒ  ¢v†–ÆRG'VS ¢G'“ ¢&WGW&âv—B&÷BæVF—EöÖW76vU÷FW‡B€¢6†Eö–CÖ6†Eö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢FW‡C×FW‡BÀ¢&WÇ•öÖ&·W×&WÇ•öÖ&·WÀ¢F—6&ÆU÷vV%÷vU÷&Wf–WsÕG'VRÀ¢ ¢W†6WB&WG'”gFW"2W†3 ¢GFV×B³Ò ¢&WG'•ögFW"ÒvWFGG"€¢W†2À¢'&WG'•ögFW""À¢À¢ ¢–b†6GG"€¢&WG'•ögFW"À¢'F÷FÅ÷6V6öæG2"À¢“ ¢v—E÷6V6öæG2ÒfÆöB€¢&WG'•ögFW"çF÷FÅ÷6V6öæG2‚¢¢VÇ6S ¢v—E÷6V6öæG2ÒfÆöB€¢&WG'•ögFW ¢ ¢v—E÷6V6öæG2ÒÖ‚€¢ãÀ¢v—E÷6V6öæG2À¢’²ã  ¢Æövv–ærçv&æ–ær€¢%FVÆVw&ÒfÆööB6öçG&öÂGW&–ær6FVv÷'’VF—C¢ ¢&ÖW76vUö–CÒW2GFV×CÒW2òW2v—CÒRãg2"À¢ÖW76vUö–BÀ¢GFV×BÀ¢Ö…öGFV×G2À¢v—E÷6V6öæG2À¢ ¢–bGFV×BãÒÖ…öGFV×G3 ¢&—6P ¢v—B7–æ6–òç6ÆVW€¢v—E÷6V6öæG0¢ ¢W†6WBF–ÖVD÷WC ¢GFV×B³Ò ¢v—E÷6V6öæG2ÒÖ–â€¢ã¢GFV×BÀ¢2ãÀ¢ ¢Æövv–ærçv&æ–ær€¢%FVÆVw&ÒF–ÖV÷WBGW&–ær6FVv÷'’VF—C¢ ¢&ÖW76vUö–CÒW2GFV×CÒW2òW2v—CÒRãg2"À¢ÖW76vUö–BÀ¢GFV×BÀ¢Ö…öGFV×G2À¢v—E÷6V6öæG2À¢ ¢–bGFV×BãÒÖ…öGFV×G3 ¢&—6P ¢v—B7–æ6–òç6ÆVW€¢v—E÷6V6öæG0¢  ¦7–æ2FVb7–æ5ö7F—fUö6FVv÷'•÷÷7G2€¢&÷BÀ¢6FVv÷'•ö–G3ÔæöæRÀ¢“ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢–BÀ¢Ö–å÷&–6Uö&–ÆÆ–öâÀ¢Ö…÷&–6Uö&–ÆÆ–öâÀ¢vUööfg6WBÀ¢vU÷6—¦RÀ¢FVÆVw&Õö6†ææVÅö–BÀ¢FVÆVw&ÕöÖW76vUö–@¢e$ôÒ6FVv÷'•÷÷7G0¢t„U$R—5ö7F—fRÒE%TP¢äB4ôÄU44R‡WFFVEöBÂ7&VFVEöB¢ãÒäõr‚’Ò”åDU%dÂsF—2p¢õ$DU"%’–@¢"" ¢ ¢6FVv÷'•÷&÷w2Ò7W"æfWF6†ÆÂ‚ ¢2ÓÓÒD$tUDTB$”4R4DTtõ%’d”ÅDU"cÓÓĞ¢–b6FVv÷'•ö–G2—2æ÷BæöæS ¢vçFVEö6FVv÷'•ö–G2Ò°¢–çB‡fÇVR¢f÷"fÇVR–â6FVv÷'•ö–G0¢Ğ ¢6FVv÷'•÷&÷w2Ò°¢&÷p¢f÷"&÷r–â6FVv÷'•÷&÷w0¢–b–çB‡&÷u³Ò¢–âvçFVEö6FVv÷'•ö–G0¢Ğ ¢7–æ6VBÒ ¢&VÖ÷fVBÒ  ¢f÷"6FVv÷'•÷&÷r–â6FVv÷'•÷&÷w3 ¢€¢6FVv÷'•÷÷7Eö–BÀ¢Æ÷rÀ¢†–v‚À¢öfg6WBÀ¢vU÷6—¦RÀ¢6†ææVÅö–BÀ¢ÖW76vUö–BÀ¢’Ò6FVv÷'•÷&÷p ¢vU÷6—¦RÒ–çB€¢vU÷6—¦R÷" ¢ ¢ÆÅ÷&÷w2Ò€¢vWE÷V&Æ—6†VE÷&÷W'F–W5ö–å÷&ævR€¢fÆöB†Æ÷r’À¢fÆöB††–v‚’À¢¢ ¢F÷FÂÒÆVâ†ÆÅ÷&÷w2 ¢2æòf–ÆW2ÆVgB–âF†—26FVv÷'’à¢–bF÷FÂÓÒ ¢G'“ ¢v—B&÷BæFVÆWFUöÖW76vR€¢6†Eö–CÖ6†ææVÅö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢¢W†6WBW†6WF–öâ2W†3 ¢Æövv–ærçv&æ–ær€¢$6÷VÆBæ÷BFVÆWFRV×G’6FVv÷'’÷7BW3¢W2"À¢6FVv÷'•÷÷7Eö–BÀ¢W†2À¢ ¢FV7F—fFUö6FVv÷'•÷÷7B€¢6FVv÷'•÷÷7Eö–@¢ ¢&VÖ÷fVB³Ò¢6öçF–çVP ¢2–bFVÆWF–öâ÷6ÆR6W6VBF†R7W'&VçBvP¢2FòF—6V"ÂÖ÷fRFòF†RæWrÆ7BvRà¢Æ7Eööfg6WBÒ€¢‡F÷FÂÒ’òòvU÷6—¦P¢’¢vU÷6—¦P ¢öfg6WBÒ–çB†öfg6WB÷" ¢–böfg6WBâÆ7Eööfg6WC ¢öfg6WBÒÆ7Eööfg6W@ ¢WFFUö6FVv÷'•÷÷7Eööfg6WB€¢6FVv÷'•÷÷7Eö–BÀ¢öfg6WBÀ¢ ¢vU÷&÷w2ÒÆÅ÷&÷w5°¢öfg6WC ¢öfg6WB²vU÷6—¦P¢Ğ ¢FW‡BÒ'V–ÆEö6FVv÷'•÷÷7E÷FW‡B€¢fÆöB†Æ÷r’À¢fÆöB††–v‚’À¢vU÷&÷w2À¢öfg6WCÖöfg6WBÀ¢F÷FÃ×F÷FÂÀ¢vU÷6—¦S×vU÷6—¦RÀ¢ ¢¶W–&ö&BÒ€¢6FVv÷'•öæf–vF–öåö¶W–&ö&B€¢6FVv÷'•÷÷7Eö–BÀ¢öfg6WBÀ¢F÷FÂÀ¢vU÷6—¦RÀ¢¢ ¢G'“ ¢v—BöVF—Eö6FVv÷'•öÖW76vU÷v—F…÷&WG'’€¢&÷BÀ¢6†Eö–CÖ6†ææVÅö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢FW‡C×FW‡BÀ¢&WÇ•öÖ&·WÖ¶W–&ö&BÀ¢ ¢W†6WBW†6WF–öâ2W†3 ¢W'&÷%÷FW‡BÒ7G"€¢W†0¢’æÆ÷vW"‚ ¢–b€¢&ÖW76vR—2æ÷BÖöF–f–VB ¢–âW'&÷%÷FW‡@¢“ ¢70 ¢VÆ–b—6–ç7Fæ6R†W†2Â&WG'”gFW"“ ¢Æövv–ærçv&æ–ær€¢$6FVv÷'’7–æ2&F6‚W6VBgFW"FVÆVw&ÒfÆööB6öçG&öÃ¢ ¢&6FVv÷'•÷÷7Eö–CÒW2"À¢6FVv÷'•÷÷7Eö–BÀ¢¢'&V° ¢VÆ–b€¢&ÖW76vRFòVF—Bæ÷Bf÷VæB ¢–âW'&÷%÷FW‡@¢÷ ¢&ÖW76vRFò&RVF—FVBæ÷Bf÷VæB ¢–âW'&÷%÷FW‡@¢“ ¢FV7F—fFUö6FVv÷'•÷÷7B€¢6FVv÷'•÷÷7Eö–@¢ ¢&VÖ÷fVB³Ò ¢Æövv–ærçv&æ–ær€¢$6FVv÷'’÷7BFV7F—fFVB&V6W6R ¢%FVÆVw&ÒÖW76vR—2Ö—76–æs¢ ¢&6FVv÷'•÷÷7Eö–CÒW2 ¢&6†ææVÅö–CÒW2ÖW76vUö–CÒW2"À¢6FVv÷'•÷÷7Eö–BÀ¢6†ææVÅö–BÀ¢ÖW76vUö–BÀ¢ ¢6öçF–çVP ¢VÆ–b€¢'F–ÖVB÷WB"–âW'&÷%÷FW‡@¢÷"'F–ÖV÷WB"–âW'&÷%÷FW‡@¢“ ¢Æövv–ærçv&æ–ær€¢$6FVv÷'’7–æ2F–ÖVB÷WC² ¢'v–ÆÂ&WG'’ÆFW#¢ ¢&6FVv÷'•÷÷7Eö–CÒW2"À¢6FVv÷'•÷÷7Eö–BÀ¢ ¢6öçF–çVP ¢VÇ6S ¢Æövv–æræW†6WF–öâ€¢$6FVv÷'’7–æ2f–ÆVC¢ ¢&6FVv÷'•÷÷7Eö–CÒW2"À¢6FVv÷'•÷÷7Eö–BÀ¢ ¢6öçF–çVP ¢&WÆ6Uö6FVv÷'•÷÷7Eö—FV×2€¢6FVv÷'•÷÷7Eö–BÀ¢vU÷&÷w2À¢ ¢7–æ6VB³Ò ¢2¶VW&6¶w&÷VæBÖ–çFVææ6R&VÆ÷rFVÆVw&Òw2'W'7BÆ–Ö—G2à¢v—B7–æ6–òç6ÆVWƒ"ã ¢Æövv–æræ–æfò€¢$6FVv÷'’7–æ26ö×ÆWFVC¢ ¢'7–æ6VCÒW2&VÖ÷fVCÒW2"À¢7–æ6VBÀ¢&VÖ÷fVBÀ¢ ¢&WGW&â°¢'7–æ6VB#¢7–æ6VBÀ¢'&VÖ÷fVB#¢&VÖ÷fVBÀ¢Ğ   ¦7–æ2FVbf÷&6UöFVÆWFU÷&÷W'G’€¢&÷BÀ¢&÷W'G•ö–BÀ¢“ ¢"" ¢–ÖÖVF–FVÇ’&VÖ÷fRV&Æ—6†VB&÷W'G’g&öÒF†P¢FW7F–æF–öâ6†ææVÂv†–ÆR&W6W'f–ær—G2D"†—7F÷'’à¢""  ¢FW7F–æF–öâÒvWEöFW7F–æF–öåö6†ææVÂ‚ ¢–bæ÷BFW7F–æF–öã ¢&—6R'VçF–ÖTW'&÷"€¢-ªŠ}˜mŠ}˜BŠ}‹]˜M¸Â˜‹Š}˜B›í¸ÍŠıŠr˜m‹MŠòâ ¢ ¢FW7F–æF–öåö6†ææVÅö–BÒFW7F–æF–öå³Ğ ¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W# ¢7W"æW†V7WFR€¢"" ¢4TÄT5@¢Væ—Eö6öFRÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢7FGW2À¢FVÆWFVEög&öÕö6†ææVÅö@¢e$ôÒ&÷W'F–W0¢t„U$R–BÒW0¢Ä”Ô•B¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢&÷rÒ7W"æfWF6†öæR‚ ¢–bæ÷B&÷s ¢&—6R'VçF–ÖTW'&÷"€¢-˜Š}¸Í˜BŠı‹Šı¸ÍŠ­Š}Š¸Í‹2›í¸ÍŠıŠr˜m‹MŠòâ ¢ ¢€¢Væ—Eö6öFRÀ¢V&Æ—6†VEöÖW76vUö–BÀ¢7W'&VçE÷7FGW2À¢FVÆWFVEög&öÕö6†ææVÅöBÀ¢’Ò&÷p ¢2FWFW&Ö–æR†÷rÖç’FVÆVw&ÒÖW76vW2&VÆöærFòF†RÆ'VÒà¢ÖVF–÷&÷w2ÒvWE÷&÷W'G•öÖVF–€¢&÷W'G•ö–@¢ ¢†÷Fõö6÷VçBÒÆVâ€¢°¢—FVĞ¢f÷"—FVÒ–âÖVF–÷&÷w0¢–b—FVÕ³ÒÓÒ%„õDò ¢Ğ¢ ¢ÖW76vUö–G2ÒµĞ ¢–bV&Æ—6†VEöÖW76vUö–C ¢Æ'VÕ÷6—¦RÒ€¢Ö–â‡†÷Fõö6÷VçBÂ¢–b†÷Fõö6÷VçBâ¢VÇ6R¢ ¢ÖW76vUö–G2Ò°¢–çB‡V&Æ—6†VEöÖW76vUö–B’²öfg6W@¢f÷"öfg6WB–â&ævR†Æ'VÕ÷6—¦R¢Ğ ¢FVÆWFVEöÖW76vW2Ò ¢Ç&VG•öÖ—76–ærÒ ¢v&æ–æw2ÒµĞ ¢2&VÖ÷fRFVÆVw&ÒÖW76vW2f—'7Bà¢2öæÇ’gFW"F†—2FòvRFV7F—fFRF†RD"&V6÷&Bà¢f÷"ÖW76vUö–B–âÖW76vUö–G3 ¢G'“ ¢v—B&÷BæFVÆWFUöÖW76vR€¢6†Eö–CÖFW7F–æF–öåö6†ææVÅö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢ ¢FVÆWFVEöÖW76vW2³Ò¢v—B7–æ6–òç6ÆVWƒ"ã ¢W†6WBW†6WF–öâ2W†3 ¢W'&÷%÷FW‡BÒ7G"†W†2’æÆ÷vW"‚ ¢Ö—76–æuöÖ&¶W'2Ò€¢&ÖW76vRFòFVÆWFRæ÷Bf÷VæB"À¢&ÖW76vRæ÷Bf÷VæB"À¢ ¢–bç’€¢Ö&¶W"–âW'&÷%÷FW‡@¢f÷"Ö&¶W"–âÖ—76–æuöÖ&¶W'0¢“ ¢Ç&VG•öÖ—76–ær³Ò¢6öçF–çVP ¢–b€¢'F–ÖVB÷WB"–âW'&÷%÷FW‡@¢÷"'F–ÖV÷WB"–âW'&÷%÷FW‡@¢“ ¢2&WG'’öæ6Rf÷"FV×÷&'’FVÆVw&ÒF–ÖV÷WBà¢G'“ ¢v—B&÷BæFVÆWFUöÖW76vR€¢6†Eö–CÖFW7F–æF–öåö6†ææVÅö–BÀ¢ÖW76vUö–CÖÖW76vUö–BÀ¢ ¢FVÆWFVEöÖW76vW2³Ò¢v—B7–æ6–òç6ÆVWƒ"ã¢6öçF–çVP ¢W†6WBW†6WF–öâ2&WG'•öW†3 ¢&WG'•÷FW‡BÒ7G"€¢&WG'•öW†0¢’æÆ÷vW"‚ ¢–bç’€¢Ö&¶W"–â&WG'•÷FW‡@¢f÷"Ö&¶W"–âÖ—76–æuöÖ&¶W'0¢“ ¢Ç&VG•öÖ—76–ær³Ò¢6öçF–çVP ¢v&æ–æw2æVæB€¢b-›í¸ÍŠ}˜R¶ÖW76vUö–GÓ¢·&WG'•öW†7Ò ¢¢6öçF–çVP ¢v&æ–æw2æVæB€¢b-›í¸ÍŠ}˜R¶ÖW76vUö–GÓ¢¶W†7Ò ¢ ¢2–bFVÆVw&ÒFVÆWF–öâ†B&VÂf–ÇW&RÂFòæ÷B&WFVæ@¢2F†BF†Rf÷&6RÖFVÆWFRv26ö×ÆWFVÇ’7V66W76gVÂà¢–bv&æ–æw3 ¢&—6R'VçF–ÖTW'&÷"€¢-Šİ‹˜Š­˜Mªı‹Š}˜]¸ÂªŠ}˜]˜BŠ}˜mŠÍŠ}˜R˜m‹MŠó¢ ¢²"Â"æ¦ö–â‡v&æ–æw2•³£Ğ¢ ¢2&W6W'fR†—7F÷'’Â'WBÖ¶RF†R&÷W'G’–æ7F—fRWfW'—v†W&Rà¢v—F‚F%ö6öææV7B‚’26öæã ¢v—F‚6öæâæ7W'6÷"‚’27W#  ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'F–W0¢4U@¢7FGW2Òt”ä5D•dRrÀ¢FVÆWFVEög&öÕö6†ææVÅöBÒ4ôÄU44R€¢FVÆWFVEög&öÕö6†ææVÅöBÀ¢äõr‚¢’À¢WFFVEöBÒäõr‚¢t„U$R–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDR&÷W'G•÷6÷W&6W0¢4U@¢—5ö7F—fRÒdÅ4RÀ¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢7W"æW†V7WFR€¢"" ¢UDDRV&Æ—6…÷VWVP¢4U@¢VWVU÷7FGW2Òu4´•TBrÀ¢Æ7EöW'&÷"Òtf÷&6RFVÆWFVB'’÷W&F÷"rÀ¢WFFVEöBÒäõr‚¢t„U$R&÷W'G•ö–BÒW0¢äBVWVU÷7FGW2”â€¢täUrrÀ¢u%4TBrÀ¢u$TE’rÀ¢u44„TETÄTBrÀ¢uTäD”äuôEUÄ”4DRrÀ¢td”ÄTBp¢¢"""À¢‡&÷W'G•ö–BÂ’À¢ ¢6öæâæ6öÖÖ—B‚ ¢6FVv÷'•÷&W7VÇBÒv—BVWVU÷F&vWFVEö6FVv÷'•÷7–æ2€¢&÷BÀ¢·&÷W'G•ö–EÒÀ¢&V6öãÒ&f÷&6RÖFVÆWFR"À¢ ¢Æövv–ærçv&æ–ær€¢$dõ$4RDTÄUDR6ö×ÆWFVC¢ ¢'&÷W'G•ö–CÒW2Væ—Eö6öFSÒW2 ¢'FVÆVw&ÕöFVÆWFVCÒW2Ç&VG•öÖ—76–æsÒW2 ¢&6FVv÷&–W5÷7–æ6VCÒW26FVv÷&–W5÷&VÖ÷fVCÒW2"À¢&÷W'G•ö–BÀ¢Væ—Eö6öFRÀ¢FVÆWFVEöÖW76vW2À¢Ç&VG•öÖ—76–ærÀ¢6FVv÷'•÷&W7VÇBævWB‚'7–æ6VB"Â’À¢6FVv÷'•÷&W7VÇBævWB‚'&VÖ÷fVB"Â’À¢ ¢&WGW&â°¢'&÷W'G•ö–B#¢&÷W'G•ö–BÀ¢'Væ—Eö6öFR#¢Væ—Eö6öFRÀ¢&FVÆWFVEöÖW76vW2#¢FVÆWFVEöÖW76vW2À¢&Ç&VG•öÖ—76–ær#¢Ç&VG•öÖ—76–ærÀ¢&6FVv÷&–W5÷7–æ6VB#¢6FVv÷'•÷&W7VÇBævWB€¢'7–æ6VB"À¢À¢’À¢&6FVv÷&–W5÷&VÖ÷fVB#¢6FVv÷'•÷&W7VÇBævWB€¢'&VÖ÷fV[jÇºã
âµç«®ŠÁ®‰˜©x",
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
        "ÙØ±ÙˆØ®ØªÙ‡ Ø´Ø¯",
        "ÙØ±ÙˆØ´ Ø±ÙØª",
        "Ù…Ø¹Ø§Ù…Ù„Ù‡ Ø´Ø¯",
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
            "âŒ Ù„Ø·ÙØ§Ù‹ ÛŒÚ© Ù¾Ø³Øª Ø§Ø² Ú©Ø§Ù†Ø§Ù„ Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± Forward Ú©Ù†ÛŒØ¯.",
            reply_markup=back_menu(),
        )
        return

    origin_chat = getattr(origin, "chat", None)

    if not origin_chat or origin_chat.type != "channel":
        await msg.reply_text(
            "âŒ Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ú©Ø§Ù†Ø§Ù„ Ø§Ø² Ø§ÛŒÙ† Ù¾ÛŒØ§Ù… Ù‚Ø§Ø¨Ù„ ØªØ´Ø®ÛŒØµ Ù†ÛŒØ³Øª.\n"
            "ÛŒÚ© Ù¾Ø³Øª Ù…Ø¹Ù…ÙˆÙ„ÛŒ Ø§Ø² Ø®ÙˆØ¯ Ú©Ø§Ù†Ø§Ù„ Forward Ú©Ù†ÛŒØ¯.",
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
        "INPUT": "ğŸ“¥ Ú©Ø§Ù†Ø§Ù„ ÙˆØ±ÙˆØ¯ÛŒ",
        "DESTINATION": "ğŸ“¢ Ú©Ø§Ù†Ø§Ù„ Ø§ØµÙ„ÛŒ",
        "LOG": "ğŸ§¾ Ú©Ø§Ù†Ø§Ù„ Ù„Ø§Ú¯",
        "SOURCE": "ğŸ“¡ Ú©Ø§Ù†Ø§Ù„ Ù…Ù†Ø¨Ø¹",
    }

    uname = f"@{username}" if username else "Ù†Ø¯Ø§Ø±Ø¯"

    await msg.reply_text(
        "âœ… Ú©Ø§Ù†Ø§Ù„ Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª Ø«Ø¨Øª Ø´Ø¯.\n\n"
        f"Ù†ÙˆØ¹: {type_names.get(pending_type)}\n"
        f"Ù†Ø§Ù…: {title}\n"
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
            "ğŸ” Ù†Ø´Ø³Øª Ø´Ù…Ø§ ÙØ¹Ø§Ù„ Ù†ÛŒØ³Øª.\n\n"
            "Ø¨Ø±Ø§ÛŒ ÙˆØ±ÙˆØ¯ /login Ø±Ø§ Ø¨Ø²Ù†ÛŒØ¯."
        )
        return

    user_id, username, display_name, role = session
    action = query.data

    if action == "customer_showcase_create":
        raw_query = context.user_data.get("smart_search_query")
        if not raw_query:
            await query.edit_message_text(
                "âš ï¸ Ø¬Ø³ØªØ¬ÙˆÛŒ Ù‚Ø¨Ù„ÛŒ Ù…Ù†Ù‚Ø¶ÛŒ Ø´Ø¯Ù‡ Ø§Ø³Øª. Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø¬Ø³ØªØ¬Ùˆ Ú©Ù†ÛŒØ¯.",
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
                "âš ï¸ Ø¨Ø±Ø§ÛŒ Ø§ÛŒÙ† Ø¬Ø³ØªØ¬Ùˆ ÙØ§ÛŒÙ„ ÙØ¹Ø§Ù„ÛŒ ÙˆØ¬ÙˆØ¯ Ù†Ø¯Ø§Ø±Ø¯ Ùˆ Ù„ÛŒÙ†Ú© Ø³Ø§Ø®ØªÙ‡ Ù†Ø´Ø¯.",
                reply_markup=back_menu(),
            )
            return
        link = await build_showcase_link(context.bot, showcase["token"])
        expires = showcase["expires_at"].astimezone(
            ZoneInfo("Asia/Tehran")
        ).strftime("%Y/%m/%d %H:%M")
        await query.message.reply_text(
            "âœ… Ù„ÛŒÙ†Ú© Ø§Ø®ØªØµØ§ØµÛŒ Ù…Ø´ØªØ±ÛŒ Ø³Ø§Ø®ØªÙ‡ Ø´Ø¯\n\n"
            f"ğŸ” Ø¯Ø±Ø®ÙˆØ§Ø³Øª: {smart_search_display_query(raw_query)}\n"
            f"ğŸ“‚ ØªØ¹Ø¯Ø§Ø¯ ÙØ§ÛŒÙ„ Ø§ÙˆÙ„ÛŒÙ‡: {showcase['count']}\n"
            f"â³ Ø§Ø¹ØªØ¨Ø§Ø± ØªØ§: {expires}\n\n"
            f"{link}\n\n"
            "Ø§ÛŒÙ† ÙˆÛŒØªØ±ÛŒÙ† Ø«Ø§Ø¨Øª Ø§Ø³ØªØ› ÙØ§ÛŒÙ„ Ø­Ø°Ùâ€ŒØ´Ø¯Ù‡ Ø§Ø² Ø¢Ù† Ú©Ù†Ø§Ø± Ù…ÛŒâ€ŒØ±ÙˆØ¯ØŒ "
            "Ø§Ù…Ø§ ÙØ§ÛŒÙ„ Ø¬Ø¯ÛŒØ¯ Ø¨Ù‡ Ù„ÛŒØ³Øª Ø§Ø¶Ø§ÙÙ‡ Ù†Ù…ÛŒâ€ŒØ´ÙˆØ¯ Ùˆ ÙÙ‚Ø· Ø¯Ø± ØµÙˆØ±Øª ÙØ¹Ø§Ù„â€ŒÚ©Ø±Ø¯Ù† "
            "Ø§Ø¹Ù„Ø§Ù†ØŒ Ø¬Ø¯Ø§Ú¯Ø§Ù†Ù‡ Ø¨Ø±Ø§ÛŒ Ù…Ø´ØªØ±ÛŒ Ø§Ø±Ø³Ø§Ù„ Ø®ÙˆØ§Ù‡Ø¯ Ø´Ø¯.",
            disable_web_page_preview=True,
        )
        return

    # CRM_ACCESS_GUARD_V1
    if not check_action_access(session, action or ""):
        await query.edit_message_text(
            "â›” Ø´Ù…Ø§ Ø¨Ù‡ Ø§ÛŒÙ† Ø¨Ø®Ø´ Ø¯Ø³ØªØ±Ø³ÛŒ Ù†Ø¯Ø§Ø±ÛŒØ¯.",
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
            "â• Ø§ÙØ²ÙˆØ¯Ù† Ú©Ø§Ù†Ø§Ù„\n\n"
            "Ù†ÙˆØ¹ Ú©Ø§Ù†Ø§Ù„ Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:",
            reply_markup=channel_type_keyboard(),
        )
        return

    if action.startswith("add_type_"):
        channel_type = action.replace("add_type_", "", 1)

        context.user_data["state"] = "waiting_channel_forward"
        context.user_data["pending_channel_type"] = channel_type

        type_names = {
            "INPUT": "ğŸ“¥ Ú©Ø§Ù†Ø§Ù„ ÙˆØ±ÙˆØ¯ÛŒ",
            "DESTINATION": "ğŸ“¢ Ú©Ø§Ù†Ø§Ù„ Ø§ØµÙ„ÛŒ",
            "LOG": "ğŸ§¾ Ú©Ø§Ù†Ø§Ù„ Ù„Ø§Ú¯",
            "SOURCE": "ğŸ“¡ Ú©Ø§Ù†Ø§Ù„ Ù…Ù†Ø¨Ø¹",
        }

        await query.edit_message_text(
            f"{type_names.get(channel_type)}\n\n"
            "Ø­Ø§Ù„Ø§ ÛŒÚ© Ù¾Ø³Øª Ø§Ø² Ù‡Ù…Ø§Ù† Ú©Ø§Ù†Ø§Ù„ Ø±Ø§ Ø¨Ø±Ø§ÛŒ Ø§ÛŒÙ† Ø±Ø¨Ø§Øª Forward Ú©Ù†ÛŒØ¯.",
            reply_markup=settings_back_menu(),
        )
        return

    if action == "sources":
        rows = get_channels()

        if not rows:
            await query.edit_message_text(
                "ğŸ“¡ Ú©Ø§Ù†Ø§Ù„â€ŒÙ‡Ø§\n\n"
                "Ù‡Ù†ÙˆØ² Ù‡ÛŒÚ† Ú©Ø§Ù†Ø§Ù„ÛŒ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
                reply_markup=settings_back_menu(),
            )
            return

        type_names = {
            "INPUT": "ğŸ“¥ ÙˆØ±ÙˆØ¯ÛŒ",
            "DESTINATION": "ğŸ“¢ Ø§ØµÙ„ÛŒ",
            "LOG": "ğŸ§¾ Ù„Ø§Ú¯",
            "SOURCE": "ğŸ“¡ Ù…Ù†Ø¨Ø¹",
        }

        lines = ["ğŸ“¡ Ú©Ø§Ù†Ø§Ù„â€ŒÙ‡Ø§ÛŒ Ø«Ø¨Øªâ€ŒØ´Ø¯Ù‡:\n"]

        for row in rows:
            db_id, tg_id, title, username, channel_type, is_active = row

            status = "âœ…" if is_active else "â›”"
            uname = f"@{username}" if username else "Ø¨Ø¯ÙˆÙ† ÛŒÙˆØ²Ø±Ù†ÛŒÙ…"

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
                "âŒ ÙØ§ÛŒÙ„ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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
            "ğŸš¨ Ù¾ÛŒØ´â€ŒÙ†Ù…Ø§ÛŒØ´ Ø§Ù†ØªØ´Ø§Ø± ÙÙˆØ±ÛŒ\n\n"
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
            "â³ ØªØ£ÛŒÛŒØ¯ Ø´Ø¯Ø› Ø¯Ø± Ø­Ø§Ù„ Ø§Ù†ØªØ´Ø§Ø± ÙØ§ÛŒÙ„..."
        )

        try:
            result = await publish_property(
                context.bot,
                property_id,
            )

            if result.get("already_published"):
                await query.edit_message_text(
                    "âš ï¸ Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ù‚Ø¨Ù„Ø§Ù‹ Ù…Ù†ØªØ´Ø± Ø´Ø¯Ù‡ Ø§Ø³Øª.",
                    reply_markup=back_menu(),
                )
                return

            await query.edit_message_text(
                "âœ… ÙØ§ÛŒÙ„ Ø¨Ø§ ØªØ£ÛŒÛŒØ¯ Ø´Ù…Ø§ Ù…Ù†ØªØ´Ø± Ø´Ø¯.\n\n"
                f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {result.get('unit_code')}\n"
                f"Ù„ÛŒÙ†Ú© Ù¾Ø³Øª:\n"
                f"{result.get('channel_post_url') or 'Ø«Ø¨Øª Ù†Ø´Ø¯'}",
                reply_markup=back_menu(),
            )

        except Exception as exc:
            logging.exception(
                "Urgent approved duplicate publish failed "
                "for property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "âŒ Ø§Ù†ØªØ´Ø§Ø± ÙØ§ÛŒÙ„ Ù†Ø§Ù…ÙˆÙÙ‚ Ø¨ÙˆØ¯.\n\n"
                f"Ø®Ø·Ø§: {str(exc)[:500]}",
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
            "âŒ Ø§Ù†ØªØ´Ø§Ø± Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ù„ØºÙˆ Ø´Ø¯.\n\n"
            "ÙØ§ÛŒÙ„ Ø­Ø°Ù Ù†Ø´Ø¯Ù‡ Ùˆ Ø¯Ø± Ø¯ÛŒØªØ§Ø¨ÛŒØ³ Ø¨Ø§Ù‚ÛŒ Ù…ÛŒâ€ŒÙ…Ø§Ù†Ø¯.",
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
            "â³ Ø¯Ø± Ø­Ø§Ù„ Ø§Ù†ØªØ´Ø§Ø± ÙÙˆØ±ÛŒ ÙØ§ÛŒÙ„..."
        )

        try:
            result = await publish_property(
                context.bot,
                property_id,
            )

            if result.get("already_published"):
                await query.edit_message_text(
                    "âš ï¸ Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ù‚Ø¨Ù„Ø§Ù‹ Ù…Ù†ØªØ´Ø± Ø´Ø¯Ù‡ Ø§Ø³Øª.",
                    reply_markup=back_menu(),
                )
                return

            await query.edit_message_text(
                "âœ… ÙØ§ÛŒÙ„ Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª Ù…Ù†ØªØ´Ø± Ø´Ø¯.\n\n"
                f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {result.get('unit_code')}\n"
                f"Ù„ÛŒÙ†Ú© Ù¾Ø³Øª:\n{result.get('channel_post_url') or 'Ø«Ø¨Øª Ù†Ø´Ø¯'}",
                reply_markup=back_menu(),
            )

        except Exception as exc:
            logging.exception(
                "Urgent publish failed for property_id=%s",
                property_id,
            )

            await query.edit_message_text(
                "âŒ Ø§Ù†ØªØ´Ø§Ø± ÙÙˆØ±ÛŒ Ù†Ø§Ù…ÙˆÙÙ‚ Ø¨ÙˆØ¯.\n\n"
                f"Ø®Ø·Ø§: {str(exc)[:500]}",
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
            "â³ Ø¯Ø± Ø­Ø§Ù„ Ø¨Ø§Ø²Ù†Ø´Ø± ÙØ§ÛŒÙ„...\n\n"
            "Ú©Ø¯ ÙˆØ§Ø­Ø¯ ØªØºÛŒÛŒØ± Ù†Ù…ÛŒâ€ŒÚ©Ù†Ø¯."
        )

        try:
            result = await republish_property(
                context.bot,
                property_id,
            )

            text = (
                "âœ… Ø¨Ø§Ø²Ù†Ø´Ø± Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª Ø§Ù†Ø¬Ø§Ù… Ø´Ø¯.\n\n"
                f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {result.get('unit_code')}\n"
                f"ØªØ¹Ø¯Ø§Ø¯ Ø¹Ú©Ø³â€ŒÙ‡Ø§: {result.get('photo_count')}\n"
                f"Ù¾ÛŒØ§Ù… Ø¬Ø¯ÛŒØ¯: {result.get('new_message_id')}\n"
                f"Ù„ÛŒÙ†Ú© Ù¾Ø³Øª:\n"
                f"{result.get('channel_post_url') or 'Ø«Ø¨Øª Ù†Ø´Ø¯'}"
            )

            if result.get("old_deleted"):
                text += (
                    "\n\nğŸ—‘ Ù¾Ø³Øª Ù‚Ø¨Ù„ÛŒ Ù†ÛŒØ² Ø­Ø°Ù Ø´Ø¯."
                )

            elif result.get("old_message_id"):
                text += (
                    "\n\nâš ï¸ Ù†Ø³Ø®Ù‡ Ø¬Ø¯ÛŒØ¯ Ù…Ù†ØªØ´Ø± Ø´Ø¯ØŒ "
                    "Ø§Ù…Ø§ Ø­Ø°Ù Ù¾Ø³Øª Ù‚Ø¨Ù„ÛŒ Ù…Ù…Ú©Ù† Ù†Ø´Ø¯."
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
                "âŒ Ø¨Ø§Ø²Ù†Ø´Ø± Ù†Ø§Ù…ÙˆÙÙ‚ Ø¨ÙˆØ¯.\n\n"
                f"Ø®Ø·Ø§: {str(exc)[:500]}",
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
                "ğŸ—‘ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„\n\n"
                "âœ… Ø¯Ø± Ø­Ø§Ù„ Ø­Ø§Ø¶Ø± ÙØ§ÛŒÙ„ Ø¢Ø±Ø´ÛŒÙˆØ´Ø¯Ù‡â€ŒØ§ÛŒ ÙˆØ¬ÙˆØ¯ Ù†Ø¯Ø§Ø±Ø¯.",
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
            "ğŸ—‘ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„\n\n"
            f"ØªØ¹Ø¯Ø§Ø¯ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ø¢Ø±Ø´ÛŒÙˆØ´Ø¯Ù‡: {total}\n"
            f"ØµÙØ­Ù‡ {current_page} Ø§Ø² {total_pages}\n\n"
            "Ø¨Ø±Ø§ÛŒ Ù…Ø´Ø§Ù‡Ø¯Ù‡ Ùˆ Ø¨Ø§Ø²ÛŒØ§Ø¨ÛŒ Ù‡Ø± ÙØ§ÛŒÙ„ "
            "Ø±ÙˆÛŒ Ø¢Ù† Ø¨Ø²Ù†ÛŒØ¯:",
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
                "âš ï¸ Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ø¯ÛŒÚ¯Ø± Ø¯Ø± Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„ ÙˆØ¬ÙˆØ¯ Ù†Ø¯Ø§Ø±Ø¯.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "â¬…ï¸ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„",
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
                            "â™»ï¸ Ø¨Ø§Ø²ÛŒØ§Ø¨ÛŒ ÙØ§ÛŒÙ„",
                            callback_data=(
                                f"trash_restore_confirm_{property_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„",
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
                "âš ï¸ ÙØ§ÛŒÙ„ Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± Ø¯ÛŒÚ¯Ø± Ø¯Ø± Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„ Ù†ÛŒØ³Øª.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "â¬…ï¸ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„",
                                callback_data="trash_page_0",
                            )
                        ]
                    ]
                ),
            )
            return

        unit_code = row[1]

        await query.edit_message_text(
            "âš ï¸ ØªØ£ÛŒÛŒØ¯ Ø¨Ø§Ø²ÛŒØ§Ø¨ÛŒ\n\n"
            f"Ú©Ø¯ ÙˆØ§Ø­Ø¯: {unit_code or '-'}\n\n"
            "Ø§ÛŒÙ† ÙØ§ÛŒÙ„ Ø§Ø² Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„ Ø®Ø§Ø±Ø¬ Ù…ÛŒâ€ŒØ´ÙˆØ¯ "
            "Ùˆ Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø¨Ù‡ Ø­Ø§Ù„Øª Ø¢Ù…Ø§Ø¯Ù‡ Ø§Ù†ØªØ´Ø§Ø± Ø¨Ø±Ù…ÛŒâ€ŒÚ¯Ø±Ø¯Ø¯.\n\n"
            "ÙØ§ÛŒÙ„ Ù‡Ù…ÛŒÙ† Ù„Ø­Ø¸Ù‡ Ø¯Ø± Ú©Ø§Ù†Ø§Ù„ Ù…Ù†ØªØ´Ø± Ù†Ù…ÛŒâ€ŒØ´ÙˆØ¯.\n\n"
            "Ø§Ø¯Ø§Ù…Ù‡ Ù…ÛŒâ€ŒØ¯Ù‡ÛŒØ¯ØŸ",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "âœ… Ø¨Ù„Ù‡ØŒ Ø¨Ø§Ø²ÛŒØ§Ø¨ÛŒ Ø´ÙˆØ¯",
                            callback_data=(
                                f"trash_restore_do_{property_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "âŒ Ø§Ù†ØµØ±Ø§Ù",
                            callback_data=(
                                f"trash_view_{property_id}"
                m«ëŒ+Š×®º+º$zzb¥â’À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚€¢'G&6…÷&W7F÷&UöFõò ¢“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢'G&6…÷&W7F÷&UöFõò"À¢""À¢À¢¢ ¢&W7F÷&VBÒ&W7F÷&U÷&÷W'G•ög&öÕ÷G&6‚€¢&÷W'G•ö–@¢ ¢–b&W7F÷&VC ¢G'“ ¢v—BVWVU÷F&vWFVEö6FVv÷'•÷7–æ2€¢6öçFW‡Bæ&÷BÀ¢·&÷W'G•ö–EÒÀ¢&V6öãÒ'G&6‚×&W7F÷&R"À¢¢W†6WBW†6WF–öã ¢Æövv–æræW†6WF–öâ€¢$6FVv÷'’7–æ2gFW"G&6‚&W7F÷&Rf–ÆVC¢ ¢'&÷W'G•ö–CÒW2"À¢&÷W'G•ö–BÀ¢ ¢–bæ÷B&W7F÷&VC ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ªûˆò˜Š}¸Í˜B˜-Š}Š˜BŠŠ}‹-¸ÍŠ}Š¸Â˜mŠ˜Šòâ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò‹=‹}˜BŠ-‹M‹­Š}˜B"À¢6ÆÆ&6µöFFÒ'G&6…÷vUó"À¢¢Ğ¢Ğ¢’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ÈR˜Š}¸Í˜BŠŠr˜]˜˜˜-¸ÍŠ¢ŠŠ}‹-¸ÍŠ}Š¸Â‹MŠòåÆåÆâ ¢-˜‹m‹¸ÍŠ¢˜Š}¸Í˜C¢	ùú"Š-˜]Š}Šı˜rŠ}˜mŠ­‹MŠ}‹Æâ ¢-˜Š}¸Í˜BŠ˜r‹]˜‹Š}Šı¸ÂŠ‹ªı‹MŠ¢˜‚ ¢-Šı‹‹-˜]Š}˜b˜]ŠÍŠ}‹"Š­˜‹=‹r66†VGVÆW"˜]˜mŠ­‹M‹˜]¸Î(Í‹M˜Šòâ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù:R‹]˜Š}˜mŠ­‹MŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVR"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùy‹=‹}˜BŠ-‹M‹­Š}˜B"À¢6ÆÆ&6µöFFÒ'G&6…÷vUó"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜Š}¸Í˜N(Í˜}Šr"À¢6ÆÆ&6µöFFÒ&f–ÆW5öÖVçR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâÓÒ'&WV&Æ—6…öw&÷W# ¢6öçFW‡BçW6W%öFFç÷‚'7FFR"ÂæöæR¢6öçFW‡BçW6W%öFFç÷€¢'VæF–æuöw&÷W÷&WV&Æ—6…ö6÷VçB"À¢æöæRÀ¢ ¢7VÖÖ'’ÒvWE÷&WV&Æ—6…÷VWVU÷7VÖÖ'’‚ ¢7F—fUö6÷VçBÒ€¢7VÖÖ'’ævWB‚%$TE’"Â¢²7VÖÖ'’ævWB‚%44„TETÄTB"Â¢²7VÖÖ'’ævWB‚%$ô4U54”är"Â¢ ¢fÆ–F—G•÷vVV·2ÒvWEöf–ÆU÷fÆ–F—G•÷vVV·2‚ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùHŠŠ}‹-˜m‹M‹ªı‹˜˜}¸Â˜Š}¸Í˜N(Í˜}ŠuÆåÆâ ¢b-Š}‹Š­ŠŠ}‹˜‹˜M¸Â˜Š}¸Í˜N(Í˜}Šs¢·fÆ–F—G•÷vVV·7Ò˜}˜Š­˜uÆâ ¢b-Šı‹Š}˜mŠ­‹Š}‹ŠŠ}‹-˜m‹M‹¢¶7F—fUö6÷VçGÒ˜Š}¸Í˜EÆåÆâ ¢-Š}‹"˜-Šı¸Í˜]¸Î(ÍŠ­‹¸Í˜b˜Š}¸Í˜N(Í˜}Š}¸Â˜]‹Š­Š‹Š}˜mŠ­ŠíŠ}Š‚˜]¸Î(Í‹M˜ŠòåÆâ ¢-Š­‹ŠıŠ}Šò˜]˜‹Šı˜m‹‹‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠó¢"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢#R˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W÷–6µóR"À¢’À¢–æÆ–æT¶W–&ö&D'WGFöâ€¢#˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W÷–6µó"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢##˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W÷–6µó#"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈşûˆòŠ­‹ŠıŠ}ŠòŠı˜MŠí˜Š}˜r"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷Wö7W7FöÒ"À¢’À¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r˜Š}¸Í˜N(Í˜}Šr"À¢6ÆÆ&6µöFFÒ&f–ÆW5öÖVçR"À¢’À¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚'&WV&Æ—6…öw&÷W÷–6µò"“ ¢G'“ ¢6÷VçBÒ–çB€¢7F–öâç&WÆ6R€¢'&WV&Æ—6…öw&÷W÷–6µò"À¢""À¢À¢¢¢W†6WBfÇVTW'&÷# ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂŠ­‹ŠıŠ}ŠòŠ}˜mŠ­ŠíŠ}Š(Í‹MŠı˜r˜mŠ}˜]‹Š­Š‹Š}‹=Š¢â"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢–b6÷VçBÂ÷"6÷VçBâ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂŠ­‹ŠıŠ}ŠòŠŠ}¸ÍŠòŠ¸Í˜bŠ­ŠrŠŠ}‹MŠòâ"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢6öçFW‡BçW6W%öFF°¢'VæF–æuöw&÷W÷&WV&Æ—6…ö6÷VçB ¢ÒÒ6÷Vç@ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùHŠ­Š=¸Í¸ÍŠòŠŠ}‹-˜m‹M‹ªı‹˜˜}¸ÅÆåÆâ ¢b-Š­‹ŠıŠ}ŠòŠı‹Ší˜Š}‹=Š­¸Ã¢¶6÷VçGÒ˜Š}¸Í˜EÆåÆâ ¢-‹=¸Í‹=Š­˜RŠ}‹"˜-Šı¸Í˜]¸Î(ÍŠ­‹¸Í˜b˜Š}¸Í˜N(Í˜}Š}¸Í¸ÂŠ}˜mŠ­ŠíŠ}Š‚˜]¸Î(Íª˜mŠòª˜r ¢-˜}˜m˜‹"˜]‹Š­Š‹˜‚˜‹Š}˜B˜}‹=Š­˜mŠòåÆåÆâ ¢-ªŠò˜Š}ŠİŠòŠ­‹­¸Í¸Í‹˜m˜]¸Î(Íª˜mŠò˜‚Š}¸Í˜b˜Š}¸Í˜N(Í˜}Šr ¢-Š˜~(Í‹˜m˜Š}˜b˜‹˜Šı¸ÂŠÍŠı¸ÍŠòŠ½ŠŠ¢˜m˜]¸Î(Í‹M˜˜mŠòåÆåÆâ ¢-Š­Š=¸Í¸ÍŠò˜]¸Î(Íª˜m¸ÍŠı‰ò"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ÈRŠ­Š=¸Í¸ÍŠò˜‚Š}˜‹-˜Šı˜bŠ˜r‹]˜"À¢6ÆÆ&6µöFFÒ€¢b'&WV&Æ—6…öw&÷Wö6öæf—&Õ÷¶6÷VçGÒ ¢’À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂŠ}˜m‹]‹Š}˜"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâÓÒ'&WV&Æ—6…öw&÷Wö7W7FöÒ# ¢6öçFW‡BçW6W%öFFæ6ÆV"‚¢6öçFW‡BçW6W%öFF°¢'7FFR ¢ÒÒ'&WV&Æ—6…öw&÷Wö7W7FöÕö6÷VçB  ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ÈşûˆòŠ­‹ŠıŠ}ŠòŠŠ}‹-˜m‹M‹ªı‹˜˜}¸ÅÆåÆâ ¢-Š­‹ŠıŠ}Šò˜Š}¸Í˜B˜]˜‹Šı˜m‹‹‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòåÆâ ¢-ŠİŠıŠ}˜-˜C¢Æâ ¢-ŠİŠıŠ}ªŠ½‹¢ÆåÆâ ¢-˜]Š½Š}˜C¥ÆãR"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W"À¢¢Ğ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚€¢'&WV&Æ—6…öw&÷Wö6öæf—&Õò ¢“ ¢G'“ ¢6÷VçBÒ–çB€¢7F–öâç&WÆ6R€¢'&WV&Æ—6…öw&÷Wö6öæf—&Õò"À¢""À¢À¢¢¢W†6WBfÇVTW'&÷# ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂŠ­‹ŠıŠ}ŠòŠŠ}‹-˜m‹M‹˜mŠ}˜]‹Š­Š‹Š}‹=Š¢â"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.(û2Šı‹ŠİŠ}˜BŠ}˜mŠ­ŠíŠ}Š‚˜Š}¸Í˜N(Í˜}Š}¸Â˜]‹Š­Š‹âââ ¢ ¢G'“ ¢&W7VÇBÒVçVWVU÷&WV&Æ—6…ö6æF–FFW2€¢6÷Vç@¢¢W†6WBW†6WF–öâ2W†3 ¢Æövv–æræW†6WF–öâ€¢$w&÷W&WV&Æ—6‚VçVWVRf–ÆVB ¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂŠ}˜‹-˜Šı˜b˜Š}¸Í˜N(Í˜}ŠrŠ˜r‹]˜ŠŠ}‹-˜m‹M‹˜mŠ}˜]˜˜˜"Š˜ŠòåÆåÆâ ¢b-Ší‹}Šs¢·7G"†W†2•³£S×Ò"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢FFVBÒ&W7VÇBævWB‚&FFVB"Â¢&WVW7FVBÒ&W7VÇBævWB‚'&WVW7FVB"Â6÷VçB¢fÆ–F—G•÷vVV·2Ò&W7VÇBævWB€¢'fÆ–F—G•÷vVV·2"À¢vWEöf–ÆU÷fÆ–F—G•÷vVV·2‚’À¢ ¢6öçFW‡BçW6W%öFFç÷€¢'VæF–æuöw&÷W÷&WV&Æ—6…ö6÷VçB"À¢æöæRÀ¢ ¢FW‡BÒ€¢.)ÈR˜Š}¸Í˜N(Í˜}ŠrŠ˜r‹]˜ŠŠ}‹-˜m‹M‹Š}‹mŠ}˜˜r‹MŠı˜mŠòåÆåÆâ ¢b-Šı‹Ší˜Š}‹=Š£¢·&WVW7FVGÒ˜Š}¸Í˜EÆâ ¢b-Š}‹mŠ}˜˜~(Í‹MŠı˜s¢¶FFVGÒ˜Š}¸Í˜EÆâ ¢b-Š}‹Š­ŠŠ}‹˜‹˜M¸Ã¢·fÆ–F—G•÷vVV·7Ò˜}˜Š­˜uÆåÆâ ¢ ¢–bFFVBÂ&WVW7FVC ¢FW‡B³Ò€¢.(KûˆòŠ­‹ŠıŠ}Šò˜Š}¸Í˜B˜Š}ŠÍŠò‹M‹Š}¸Í‹rª˜]Š­‹Š}‹" ¢-Š­‹ŠıŠ}ŠòŠı‹Ší˜Š}‹=Š­¸ÂŠ˜Šı˜rŠ}‹=Š¢åÆåÆâ ¢ ¢FW‡B³Ò€¢-˜}¸Í¨bªŠò˜Š}ŠİŠòŠÍŠı¸ÍŠı¸Â‹=Š}ŠíŠ­˜r˜m‹MŠı˜r˜‚ ¢-Š}¸Í˜b˜Š}¸Í˜N(Í˜}ŠrŠ˜~(Í‹˜m˜Š}˜b˜‹˜Šı¸ÂŠÍŠı¸ÍŠòŠ½ŠŠ¢˜m‹MŠı˜~(ÍŠ}˜mŠòâ ¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢FW‡BÀ¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHŠŠ}‹-˜m‹M‹ªı‹˜˜}¸Â"À¢6ÆÆ&6µöFFÒ'&WV&Æ—6…öw&÷W"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜Š}¸Í˜N(Í˜}Šr"À¢6ÆÆ&6µöFFÒ&f–ÆW5öÖVçR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâÓÒ&f–ÆW5öÖVçR# ¢6öçFW‡BçW6W%öFFç÷‚'7FFR"ÂæöæR ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ù8˜Š}¸Í˜N(Í˜}ŠuÆåÆâ ¢-ŠŠí‹B˜]˜‹Šı˜m‹‹‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠó¢"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢–b7F–öâÓÒ&f÷&6UöFVÆWFR# ¢6öçFW‡BçW6W%öFFæ6ÆV"‚ ¢6öçFW‡BçW6W%öFF°¢'7FFR ¢ÒÒ&f÷&6UöFVÆWFU÷Væ—Eö6öFR  ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùyŠİ‹˜˜˜‹‹2˜Š}¸Í˜EÆåÆâ ¢-ªŠò˜Š}ŠİŠòŠıŠ}Ší˜M¸Â‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòåÆåÆâ ¢.)ªûˆò˜Š}¸Í˜BŠ˜MŠ}˜Š}‹]˜M˜rŠ}‹"ªŠ}˜mŠ}˜BŠ}‹]˜M¸ÂŠİ‹˜˜]¸Î(Í‹M˜ŠıˆÂ ¢-Š}˜]Šr‹=Š}Š˜-˜rŠ-˜bŠı‹Šı¸ÍŠ­Š}Š¸Í‹2ŠŠ}˜-¸Â˜]¸Î(Í˜]Š}˜mŠòåÆåÆâ ¢-˜]Š½Š}˜C¥Æãs#‚"À¢&WÇ•öÖ&·WÖf–ÆW5öÖVçR‚’À¢¢&WGW&à ¢–b7F–öâÓÒ'&WV&Æ—6‚# ¢6öçFW‡BçW6W%öFF²'7FFR%ÒÒ€¢'&WV&Æ—6…÷Væ—Eö6öFR ¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùHBŠŠ}‹-˜m‹M‹˜Š}¸Í˜EÆåÆâ ¢-ªŠò˜Š}ŠİŠòŠıŠ}Ší˜M¸Â‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòåÆåÆâ ¢-˜]Š½Š}˜C¥ÆãSR"À¢&WÇ•öÖ&·WÖ&6µöÖVçR‚’À¢¢&WGW&à ¢–b7F–öâÓÒ'W&vVçE÷V&Æ—6‚# ¢6öçFW‡BçW6W%öFF²'7FFR%ÒÒ'W&vVçE÷6÷W&6Uö6öFR  ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùª‚Š}˜mŠ­‹MŠ}‹˜˜‹¸ÅÆåÆâ ¢-‹M˜]Š}‹˜r›í¸Íªı¸Í‹¸Â‹]˜ˆÂªŠò˜Š}¸Í˜B˜]˜mŠ‹’¸ÍŠr˜M¸Í˜mª’›í‹=Š¢˜]˜mŠ‹’‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòåÆåÆâ ¢-˜]Š½Š}˜B‹M˜]Š}‹˜r‹]˜¥Æí‹]˜CUÆåÆâ ¢-˜]Š½Š}˜BªŠò˜]˜mŠ‹“¥Æã3“C•ÆåÆâ ¢-˜]Š½Š}˜B˜M¸Í˜mª“¥Ææ‡GG3¢ò÷BæÖR÷6÷W&6V6†ææVÂó#3B"À¢&WÇ•öÖ&·WÖ&6µöÖVçR‚’À¢¢&WGW&à ¢–b7F–öâÓÒ&GWÆ–6FU÷VWVR# ¢&÷w2ÒvWE÷VæF–æuöGWÆ–6FU÷&÷W'F–W2‚ ¢–bæ÷B&÷w3 ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	úz’˜Š}¸Í˜N(Í˜}Š}¸ÂŠ­ª‹Š}‹¸ÅÆåÆâ ¢.)ÈRŠı‹ŠİŠ}˜BŠİŠ}‹m‹˜}¸Í¨b˜Š}¸Í˜BŠ­ª‹Š}‹¸Â ¢-˜]˜mŠ­‹‹Š­Š=¸Í¸ÍŠı¸Â˜ŠÍ˜Šò˜mŠıŠ}‹Šòâ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHBŠ‹˜‹-‹‹=Š}˜m¸Â"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ&†öÖR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	úz’˜Š}¸Í˜N(Í˜}Š}¸ÂŠ­ª‹Š}‹¸Â˜]˜mŠ­‹‹Š­Š=¸Í¸ÍŠõÆåÆâ ¢b-Š­‹ŠıŠ}Šó¢¶ÆVâ‡&÷w2—ÕÆåÆâ ¢-Š‹Š}¸ÂŠ‹‹‹=¸Â˜}‹˜Š}¸Í˜B‹˜¸ÂŠ-˜bŠ‹-˜m¸ÍŠó¢"À¢&WÇ•öÖ&·WÖGWÆ–6FU÷VWVUöÆ—7Eö¶W–&ö&B‚’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚&GWVWVU÷f–Wuò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢&GWVWVU÷f–Wuò"À¢""À¢À¢¢ ¢&÷rÒvWE÷VæF–æuöGWÆ–6FUöFWF–Â€¢&÷W'G•ö–@¢ ¢–bæ÷B&÷s ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ªûˆòŠ}¸Í˜b˜Š}¸Í˜BŠı¸Íªı‹Šı‹Š}˜mŠ­‹Š}‹Š‹‹‹=¸Â˜m¸Í‹=Š¢â"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r˜M¸Í‹=Š¢"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢¢Ğ¢Ğ¢’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢'V–ÆEöGWÆ–6FU÷VWVUöFWF–Â€¢&÷p¢’À¢&WÇ•öÖ&·WÖGWÆ–6FU÷VWVUöFV6—6–öåö¶W–&ö&B€¢&÷W'G•ö–@¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚&GWVWVUö&÷fUò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢&GWVWVUö&÷fUò"À¢""À¢À¢¢ ¢&÷fUöGWÆ–6FUöf÷%÷66†VGVÆW"€¢&÷W'G•ö–@¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ÈR˜Š}¸Í˜BŠ­Š=¸Í¸ÍŠò‹MŠòåÆåÆâ ¢-Š}¸Í˜b˜Š}¸Í˜BŠı˜ŠŠ}‹˜rŠ˜r‹]˜Š}˜mŠ­‹MŠ}‹Š‹ªı‹MŠ¢ ¢-˜‚66†VGVÆW"Šı‹Š}˜˜M¸Í˜b‹-˜]Š}˜b˜]ŠÍŠ}‹" ¢-Š-˜b‹Šr˜]˜mŠ­‹M‹˜]¸Î(Íª˜mŠòâ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	úz’˜Š}¸Í˜N(Í˜}Š}¸ÂŠ­ª‹Š}‹¸Â"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜]˜m˜¸ÂŠ}‹]˜M¸Â"À¢6ÆÆ&6µöFFÒ&†öÖR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚&GWVWVU÷&V¦V7Eò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢&GWVWVU÷&V¦V7Eò"À¢""À¢À¢¢ ¢&V¦V7EöGWÆ–6FUöf÷%÷66†VGVÆW"€¢&÷W'G•ö–@¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂ˜Š}¸Í˜B‹Šò‹MŠòåÆåÆâ ¢-˜Š}¸Í˜BŠ}‹"Šı¸ÍŠ­Š}Š¸Í‹2Šİ‹˜˜m‹MŠı˜}ˆÂ ¢-Š}˜]ŠrŠı¸Íªı‹Š­˜‹=‹r66†VGVÆW"˜]˜mŠ­‹M‹˜m˜]¸Î(Í‹M˜Šòâ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	úz’˜Š}¸Í˜N(Í˜}Š}¸ÂŠ­ª‹Š}‹¸Â"À¢6ÆÆ&6µöFFÒ&GWÆ–6FU÷VWVR"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜]˜m˜¸ÂŠ}‹]˜M¸Â"À¢6ÆÆ&6µöFFÒ&†öÖR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâÓÒ'VWVUöf–ÆVB# ¢&÷w2ÒvWEöf–ÆVE÷VWVU÷&÷W'F–W2‚ ¢–bæ÷B&÷w3 ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂ˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹ÆåÆâ ¢.)ÈRŠı‹ŠİŠ}˜BŠİŠ}‹m‹˜}¸Í¨b˜Š}¸Í˜BŠí‹}Š}ŠıŠ}‹¸Â ¢-Šı‹‹]˜˜ŠÍ˜Šò˜mŠıŠ}‹Šòâ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ùHBŠ‹˜‹-‹‹=Š}˜m¸Â"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r˜Š}¸Í˜N(Í˜}Šr"À¢6ÆÆ&6µöFFÒ&f–ÆW5öÖVçR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂ˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹ÆåÆâ ¢b-Š­‹ŠıŠ}Šò˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹¢¶ÆVâ‡&÷w2—ÕÆåÆâ ¢-Š‹Š}¸ÂŠ‹‹‹=¸Â˜}‹˜Š}¸Í˜B‹˜¸ÂŠ-˜bŠ‹-˜m¸ÍŠó¢"À¢&WÇ•öÖ&·WÖf–ÆVE÷VWVUöÆ—7Eö¶W–&ö&B‚’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚'VWVUöf–ÆVE÷f–Wuò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢'VWVUöf–ÆVE÷f–Wuò"À¢""À¢À¢¢ ¢&÷rÒvWEöf–ÆVE÷VWVUöFWF–Â€¢&÷W'G•ö–@¢ ¢–bæ÷B&÷s ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ªûˆòŠ}¸Í˜b˜Š}¸Í˜BŠı¸Íªı‹Šı‹˜‹m‹¸ÍŠ¢Ší‹}Š}ŠıŠ}‹˜m¸Í‹=Š¢â"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢¢Ğ¢Ğ¢’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢'V–ÆEöf–ÆVE÷VWVUöFWF–Â‡&÷r’À¢&WÇ•öÖ&·WÖf–ÆVE÷VWVUö7F–öç5ö¶W–&ö&B€¢&÷W'G•ö–@¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚'VWVUöf–ÆVE÷&W—%ò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢'VWVUöf–ÆVE÷&W—%ò"À¢""À¢À¢¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùºŠı‹ŠİŠ}˜BŠŠ}‹-Ší˜Š}˜m¸Â˜‚Š-˜]Š}Šı˜~(Í‹=Š}‹-¸Â˜]ŠÍŠıŠò˜Š}¸Í˜Bâââ ¢ ¢G'“ ¢'6VBÒ&W—%öf–ÆVE÷&÷W'G’€¢&÷W'G•ö–@¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ÈR˜Š}¸Í˜BŠŠr˜]˜˜˜-¸ÍŠ¢ŠŠ}‹-Ší˜Š}˜m¸Â‹MŠòåÆåÆâ ¢-˜‹m‹¸ÍŠ¢ŠÍŠı¸ÍŠó¢	ùú"Š-˜]Š}Šı˜rŠ}˜mŠ­‹MŠ}‹Æâ ¢-˜Š}¸Í˜BŠı˜ŠŠ}‹˜r˜Š}‹Šò‹]˜66†VGVÆW"‹MŠòåÆåÆâ ¢b-˜]Š­‹Š}©ƒ¢·'6VBævWB‚v&VöÓ"r’÷"rÒwÕÆâ ¢b-˜-¸Í˜]Š£¢·'6VBævWB‚v7W'&VçE÷&–6Uö&–ÆÆ–öâr’÷"rÒwÒ˜]¸Í˜M¸ÍŠ}‹Šò"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂ˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù:R‹]˜Š}˜mŠ­‹MŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVR"À¢¢ÒÀ¢Ğ¢’À¢ ¢W†6WBW†6WF–öâ2W†3 ¢Æövv–æræW†6WF–öâ€¢$f–ÆVBVWVR&W—"W'&÷#¢&÷W'G•ö–CÒW2"À¢&÷W'G•ö–BÀ¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂŠŠ}‹-Ší˜Š}˜m¸Â˜Š}¸Í˜BŠ}˜mŠÍŠ}˜R˜m‹MŠòåÆåÆâ ¢b-Ší‹}Šs¥Æç·7G"†W†2•³£s×Ò"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢"À¢6ÆÆ&6µöFFÒ€¢b'VWVUöf–ÆVE÷f–Wu÷·&÷W'G•ö–GÒ ¢’À¢¢Ğ¢Ğ¢’À¢ ¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚'VWVUöf–ÆVE÷V&Æ—6…ò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢'VWVUöf–ÆVE÷V&Æ—6…ò"À¢""À¢À¢¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùª‚Šı‹ŠİŠ}˜BŠ­˜MŠ}‹B˜]ŠÍŠıŠòŠ‹Š}¸ÂŠ}˜mŠ­‹MŠ}‹˜Š}¸Í˜Bâââ ¢ ¢G'“ ¢&W7VÇBÒv—BV&Æ—6…÷&÷W'G’€¢6öçFW‡Bæ&÷BÀ¢&÷W'G•ö–BÀ¢ ¢–b&W7VÇBævWB‚&Ç&VG•÷V&Æ—6†VB"“ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ªûˆòŠ}¸Í˜b˜Š}¸Í˜B˜-Š˜MŠ}˜²Šı‹ªŠ}˜mŠ}˜B˜]˜mŠ­‹M‹‹MŠı˜rŠ}‹=Š¢â"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢¢Ğ¢Ğ¢’À¢¢&WGW&à ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ÈR˜Š}¸Í˜BŠŠr˜]˜˜˜-¸ÍŠ¢˜]˜mŠ­‹M‹‹MŠòåÆåÆâ ¢b-ªŠò˜Š}ŠİŠó¢·&W7VÇBævWB‚wVæ—Eö6öFRr—ÕÆâ ¢b-˜M¸Í˜mª’›í‹=Š£¥Æâ ¢b'·&W7VÇBævWB‚v6†ææVÅ÷÷7E÷W&Âr’÷"}Š½ŠŠ¢˜m‹MŠòwÒ"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂ˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢/	ù:R‹]˜Š}˜mŠ­‹MŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVR"À¢¢ÒÀ¢Ğ¢’À¢ ¢W†6WBW†6WF–öâ2W†3 ¢Æövv–æræW†6WF–öâ€¢$ÖçVÂf–ÆVB×VWVRV&Æ—6‚f–ÆVC¢&÷W'G•ö–CÒW2"À¢&÷W'G•ö–BÀ¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢.)ØÂŠ}˜mŠ­‹MŠ}‹˜]ŠÍŠıŠò˜mŠ}˜]˜˜˜"Š˜ŠòåÆåÆâ ¢b-Ší‹}Šs¥Æç·7G"†W†2•³£s×Ò"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r˜Š}¸Í˜B"À¢6ÆÆ&6µöFFÒ€¢b'VWVUöf–ÆVE÷f–Wu÷·&÷W'G•ö–GÒ ¢’À¢¢Ğ¢Ğ¢’À¢ ¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚'VWVUöf–ÆVE÷6¶—ò"“ ¢&÷W'G•ö–BÒ–çB€¢7F–öâç&WÆ6R€¢'VWVUöf–ÆVE÷6¶—ò"À¢""À¢À¢¢ ¢6†ævVBÒ6¶—öf–ÆVE÷&÷W'G’€¢&÷W'G•ö–@¢ ¢–b6†ævVC ¢FW‡BÒ€¢/	ùy˜Š}¸Í˜BŠ}‹"‹]˜Š}˜mŠ­‹MŠ}‹Šİ‹˜‹MŠòåÆåÆâ ¢-Ší˜Šò˜Š}¸Í˜B˜‚‹=Š}Š˜-˜rŠ-˜bŠı‹Šı¸ÍŠ­Š}Š¸Í‹2 ¢-Šİ‹˜˜m‹MŠı˜rŠ}‹=Š¢â ¢¢VÇ6S ¢FW‡BÒ€¢.)ªûˆò˜Š}¸Í˜BŠı¸Íªı‹Šı‹˜‹m‹¸ÍŠ¢Ší‹}Š}ŠıŠ}‹˜mŠ˜Šò ¢-˜‚Š­‹­¸Í¸Í‹¸ÂŠ}˜mŠÍŠ}˜R˜m‹MŠòâ ¢ ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢FW‡BÀ¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.)ØÂ˜Š}¸Í˜N(Í˜}Š}¸ÂŠí‹}Š}ŠıŠ}‹"À¢6ÆÆ&6µöFFÒ'VWVUöf–ÆVB"À¢¢ÒÀ¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆò˜Š}¸Í˜N(Í˜}Šr"À¢6ÆÆ&6µöFFÒ&f–ÆW5öÖVçR"À¢¢ÒÀ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâÓÒ'VWVU÷6V&6‚# ¢6öçFW‡BçW6W%öFFæ6ÆV"‚¢6öçFW‡BçW6W%öFF²'7FFR%ÒÒ'6V&6…÷VWVR  ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢/	ùHâŠÍ‹=Š­ŠÍ˜‚Šı‹‹]˜Š}˜mŠ­‹MŠ}‹ÆåÆâ ¢-‹M˜]Š}‹˜r‹]˜‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòåÆåÆâ ¢-˜]Š½Š}˜C¥Æâ ¢##ƒ•Æâ ¢-¸ÍŠuÆâ ¢-‹]˜#ƒ’"À¢&WÇ•öÖ&·WÔ–æÆ–æT¶W–&ö&DÖ&·W€¢°¢°¢–æÆ–æT¶W–&ö&D'WGFöâ€¢.*È^ûˆòŠŠ}‹-ªı‹MŠ¢Š˜r‹]˜"À¢6ÆÆ&6µöFFÒ'VWVR"À¢¢Ğ¢Ğ¢’À¢¢&WGW&à  ¢–b7F–öâç7F'G7v—F‚‚'VWVU÷vUò"“ ¢G'“ ¢öfg6WBÒ–çB€¢7F–öâç&WÆ6R€¢'VWVU÷vUò"À¢""À¢À¢¢¢W†6WBfÇVTW'&÷# ¢öfg6WBÒ  ¢6÷VçG2ÒvWE÷&VÅ÷VWVUö6÷VçG2‚¢F÷FÂÒ7VÒ†6÷VçG2çfÇVW2‚’ ¢–bF÷FÂâ ¢Æ7E÷fÆ–Eööfg6WBÒ€¢‡F÷FÂÒ’òò ¢’¢  ¢öfg6WBÒÖ–â€¢Ö‚ƒÂöfg6WB’À¢Æ7E÷fÆ–Eööfg6WBÀ¢¢VÇ6S ¢öfg6WBÒ  ¢v—BVW'’æVF—EöÖW76vU÷FW‡B€¢'V–ÆE÷&VÅ÷VWVU÷FW‡B€¢öfg6WCÖöfg6WBÀ¢vU÷6—¦SÓÀ¢’À¢&WÇ•öÖ&·W×VWVUöÖVçUö¶W–&ö&B€¢öfg6WCÖöfg6WBÀ¢F÷FÃ×F÷FÂÀ¢Ú±î¸Â¸­yêë¢°k¢G§¦*^    page_size=10,
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
            "âœ… Ù‡Ù†ÙˆØ² ÙØ§ÛŒÙ„ÛŒ Ù…Ù†ØªØ´Ø± Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
            reply_markup=back_menu(),
        )
        return

    if action == "invalid":
        await query.edit_message_text(
            "âš ï¸ ÙØ¹Ù„Ø§Ù‹ ÙØ§ÛŒÙ„ Ù†Ø§Ù‚ØµÛŒ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
            reply_markup=back_menu(),
        )
        return

    if action == "smart_search":
        context.user_data.clear()
        context.user_data[
            "state"
        ] = "smart_search"

        await query.edit_message_text(
            "ğŸ” Ø¬Ø³ØªØ¬ÙˆÛŒ Ù‡ÙˆØ´Ù…Ù†Ø¯ Ù…Ù„Ú©\n\n"
            "Ù‡Ø± Ú†ÛŒØ²ÛŒ Ú©Ù‡ Ù…ÛŒâ€ŒØ®ÙˆØ§Ù‡ÛŒØ¯ Ø¨Ù†ÙˆÛŒØ³ÛŒØ¯.\n\n"
            "Ù…Ø«Ø§Ù„â€ŒÙ‡Ø§:\n"
            "â€¢ 60\n"
            "â€¢ 60 Ù…ØªØ±\n"
            "â€¢ 60 ØªØ§ 90 Ù…ØªØ±\n"
            "â€¢ 5 ØªØ§ 18 Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯\n"
            "â€¢ Ø²ÛŒØ± 8 Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯\n"
            "â€¢ Ø¨Ø§Ù„Ø§ÛŒ 20 Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯\n"
            "â€¢ Ø¨Ø§ØºÙ…ÛŒØ´Ù‡\n"
            "â€¢ 3 Ø®ÙˆØ§Ø¨ Ø¨Ø§ØºÙ…ÛŒØ´Ù‡\n"
            "â€¢ Ø¨Ø§ØºÙ…ÛŒØ´Ù‡ 120 Ù…ØªØ± 5 ØªØ§ 18 Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯\n"
            "â€¢ Ø·Ø¨Ù‚Ù‡ Ù¾Ù†Ø¬Ù…",
            reply_markup=back_menu(),
        )
        return

    if action.startswith("smart_filter_"):
        filter_key = action.replace("smart_filter_", "", 1)
        raw_query = context.user_data.get("smart_search_query")
        if not raw_query:
            await query.edit_message_text(
                "âš ï¸ Ø¬Ø³ØªØ¬ÙˆÛŒ Ù‚Ø¨Ù„ÛŒ Ù…Ù†Ù‚Ø¶ÛŒ Ø´Ø¯Ù‡ Ø§Ø³Øª. Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø¬Ø³ØªØ¬Ùˆ Ú©Ù†ÛŒØ¯.",
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
                "âš ï¸ Ø¬Ø³ØªØ¬ÙˆÛŒ Ù‚Ø¨Ù„ÛŒ Ù…Ù†Ù‚Ø¶ÛŒ Ø´Ø¯Ù‡ Ø§Ø³Øª.\n\n"
                "Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø¬Ø³ØªØ¬ÙˆÛŒ Ù‡ÙˆØ´Ù…Ù†Ø¯ Ø±Ø§ Ø´Ø±ÙˆØ¹ Ú©Ù†ÛŒØ¯.",
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
                "âŒ ÙØ§ÛŒÙ„ Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
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
                "âŒ ÙØ§ÛŒÙ„ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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
                "âŒ Ø¬Ø²Ø¦ÛŒØ§Øª ÙØ§ÛŒÙ„ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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
                        "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ Ù†ØªØ§ÛŒØ¬",
                        callback_data=(
                            f"smart_search_page_{offset}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "ğŸ  Ù…Ù†ÙˆÛŒ Ø§ØµÙ„ÛŒ",
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
            "ğŸ” Ú©Ø¯ Ø¯Ø§Ø®Ù„ÛŒ ÙˆØ§Ø­Ø¯ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\nÙ…Ø«Ø§Ù„:\n10380",
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
                "ğŸ—‚ Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ\n\n"
                "ğŸ”¥ Ø³Ù‡ Ø¨Ø§Ø²Ù‡ Ù¾ÛŒØ´Ù†Ù‡Ø§Ø¯ÛŒ Ø¨Ø§ Ø¨ÛŒØ´ØªØ±ÛŒÙ† ÙØ§ÛŒÙ„:\n\n"
                "ÛŒÚ©ÛŒ Ø§Ø² Ù¾ÛŒØ´Ù†Ù‡Ø§Ø¯Ù‡Ø§ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯ØŒ "
                "ÛŒØ§ Ø¨Ø§Ø²Ù‡ Ø¯Ù„Ø®ÙˆØ§Ù‡ØªØ§Ù† Ø±Ø§ Ø¨Ù‡ Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
                "Ù…Ø«Ø§Ù„:\n8 ØªØ§ 15"
            )
        else:
            text = (
                "ğŸ—‚ Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ\n\n"
                "ÙØ¹Ù„Ø§Ù‹ Ø¨Ø§Ø²Ù‡ Ù¾ÛŒØ´Ù†Ù‡Ø§Ø¯ÛŒ Ù…Ù†Ø§Ø³Ø¨ÛŒ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.\n\n"
                "Ø¨Ø§Ø²Ù‡ Ø¯Ù„Ø®ÙˆØ§Ù‡ØªØ§Ù† Ø±Ø§ Ø¨Ù‡ Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
                "Ù…Ø«Ø§Ù„:\n8 ØªØ§ 15"
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
                "âŒ Ø¨Ø§Ø²Ù‡ Ù¾ÛŒØ´Ù†Ù‡Ø§Ø¯ÛŒ Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
                reply_markup=back_menu(),
            )
            return

        try:
            low = float(parts[0])
            high = float(parts[1])
        except ValueError:
            await query.edit_message_text(
                "âŒ Ø¨Ø§Ø²Ù‡ Ù¾ÛŒØ´Ù†Ù‡Ø§Ø¯ÛŒ Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
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
                "âš ï¸ Ø¯Ø± Ø§ÛŒÙ† Ø¨Ø§Ø²Ù‡ ÙØ§ÛŒÙ„ Ù…Ù†ØªØ´Ø±Ø´Ø¯Ù‡â€ŒØ§ÛŒ "
                "ÙˆØ¬ÙˆØ¯ Ù†Ø¯Ø§Ø±Ø¯.",
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
            "âœ… Ø¨Ø§Ø²Ù‡ Ø§Ù†ØªØ®Ø§Ø¨ Ø´Ø¯.\n\n"
            f"{hashtag}\n"
            f"ğŸ· {low_text} ØªØ§ {high_text} Ù…ÛŒÙ„ÛŒØ§Ø±Ø¯ ØªÙˆÙ…Ø§Ù†\n\n"
            f"ğŸ“Š ØªØ¹Ø¯Ø§Ø¯ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ ØºÛŒØ±ØªÚ©Ø±Ø§Ø±ÛŒ: {total}\n\n"
            "Ú©Ø¯Ø§Ù… Ú¯Ø±ÙˆÙ‡ Ø¨Ù‡ Ú©Ø§Ù†Ø§Ù„ Ø§Ø±Ø³Ø§Ù„ Ø´ÙˆØ¯ØŸ",
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
                "âŒ Ø´Ù…Ø§Ø±Ù‡ ØµÙØ­Ù‡ Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
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
                "âš ï¸ Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ø¨Ø§Ø²Ù‡ Ù…Ù†Ù‚Ø¶ÛŒ Ø´Ø¯Ù‡ Ø§Ø³Øª.\n\n"
                "Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ø§Ø² Â«Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒÂ» Ø´Ø±ÙˆØ¹ Ú©Ù†ÛŒØ¯.",
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
                "âš ï¸ Ø¯Ø± Ø§ÛŒÙ† ØµÙØ­Ù‡ ÙØ§ÛŒÙ„ÛŒ ÙˆØ¬ÙˆØ¯ Ù†Ø¯Ø§Ø±Ø¯.",
                reply_markup=back_menu(),
            )
            return

        destination = get_destination_channel()

        if not destination:
            await query.edit_message_text(
                "âŒ Ú©Ø§Ù†Ø§Ù„ Ø§ØµÙ„ÛŒ ÙØ¹Ø§Ù„ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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
            "â³ Ø¯Ø± Ø­Ø§Ù„ Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ø¨Ù‡ Ú©Ø§Ù†Ø§Ù„..."
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
                "âŒ Ø§Ø±Ø³Ø§Ù„ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ù†Ø§Ù…ÙˆÙÙ‚ Ø¨ÙˆØ¯.\n\n"
                f"Ø®Ø·Ø§: {exc}",
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
            "âœ… Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª Ø¨Ù‡ Ú©Ø§Ù†Ø§Ù„ Ø§Ø±Ø³Ø§Ù„ Ø´Ø¯.\n\n"
            f"{hashtag}\n"
            f"ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ {start_num} ØªØ§ {end_num}\n"
            f"ØªØ¹Ø¯Ø§Ø¯ ÙØ§ÛŒÙ„: {len(page_rows)}\n"
            f"Ø´Ù†Ø§Ø³Ù‡ Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ: #{category_post_id}",
            reply_markup=back_menu(),
        )
        return


    if action == "stats":
        await query.edit_message_text(
            "ğŸ“Š Ø¢Ù…Ø§Ø±\n\n"
            "Ø§ÛŒÙ† Ø¨Ø®Ø´ Ø¨Ø¹Ø¯ Ø§Ø² ÙˆØ±ÙˆØ¯ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ ÙˆØ§Ù‚Ø¹ÛŒ ÙØ¹Ø§Ù„ Ù…ÛŒâ€ŒØ´ÙˆØ¯.",
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
            "â• Ø§ÙØ²ÙˆØ¯Ù† Ø¨Ø§Ø²Ù‡ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ\n\n"
            "Ø±ÙˆØ² Ù‡ÙØªÙ‡ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:",
            reply_markup=schedule_weekday_keyboard(),
        )
        return


    if action == "schedule_day_all":
        if context.user_data.get(
            "schedule_edit_id"
        ):
            await query.answer(
                "Ø¨Ø±Ø§ÛŒ ÙˆÛŒØ±Ø§ÛŒØ´ ÛŒÚ© Ø¨Ø§Ø²Ù‡ØŒ ÛŒÚ© Ø±ÙˆØ² Ù…Ø´Ø®Øµ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯.",
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
            "âœ… Ø±ÙˆØ²Ù‡Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ø´Ø¯: Ù‡Ù…Ù‡ Ø±ÙˆØ²Ù‡Ø§\n\n"
            "ğŸ• Ø³Ø§Ø¹Øª Ø´Ø±ÙˆØ¹ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "ÙØ±Ù…Øª 24 Ø³Ø§Ø¹ØªÙ‡ Ø¨Ø§Ø´Ø¯.\n"
            "Ù…Ø«Ø§Ù„:\n09:00"
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
                "âŒ Ø±ÙˆØ² Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
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
            "âœ… Ø±ÙˆØ² Ø§Ù†ØªØ®Ø§Ø¨ Ø´Ø¯: "
            f"{WEEKDAY_NAMES[weekday]}\n\n"
            "ğŸ• Ø³Ø§Ø¹Øª Ø´Ø±ÙˆØ¹ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "ÙØ±Ù…Øª 24 Ø³Ø§Ø¹ØªÙ‡ Ø¨Ø§Ø´Ø¯.\n"
            "Ù…Ø«Ø§Ù„:\n09:00"
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
            "âœï¸ ÙˆÛŒØ±Ø§ÛŒØ´ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù‡Ù…Ù‡ Ø±ÙˆØ²Ù‡Ø§\n\n"
            "ğŸ• Ø³Ø§Ø¹Øª Ø´Ø±ÙˆØ¹ Ø¬Ø¯ÛŒØ¯ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "Ù…Ø«Ø§Ù„:\n10:30"
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
            "ğŸ—‘ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù‡Ù…Ù‡ Ø±ÙˆØ²Ù‡Ø§ Ø­Ø°Ù Ø´Ø¯.\n\n"
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
                "âŒ Ø¨Ø§Ø²Ù‡ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
                reply_markup=schedule_menu(),
            )
            return

        context.user_data.clear()

        context.user_data[
            "schedule_edit_id"
        ] = window_id

        await query.edit_message_text(
            f"âœï¸ ÙˆÛŒØ±Ø§ÛŒØ´ Ø¨Ø§Ø²Ù‡ #{window_id}\n\n"
            "Ø§ÙˆÙ„ Ø±ÙˆØ² Ù‡ÙØªÙ‡ Ø¬Ø¯ÛŒØ¯ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:\n\n"
            "ØªÙ…Ø§Ù… Ù…Ø´Ø®ØµØ§Øª Ø§ÛŒÙ† Ø¨Ø§Ø²Ù‡ Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ú¯Ø±ÙØªÙ‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯.",
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
                "âŒ Ø¨Ø§Ø²Ù‡ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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
            "ğŸ—‘ Ø¨Ø§Ø²Ù‡ Ø­Ø°Ù Ø´Ø¯.\n\n"
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
            "ğŸ‘¥ Ù…Ø¯ÛŒØ±ÛŒØª Ú©Ø§Ø±Ø¨Ø±Ø§Ù†",
            reply_markup=back_menu(),
        )
        return

    if action == "reset":
        current_weeks = get_file_validity_weeks()

        await query.edit_message_text(
            "â™»ï¸ Ù…Ø¯ÛŒØ±ÛŒØª ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ù‚Ø¯ÛŒÙ…ÛŒ\n\n"
            f"Ù…Ø¯Øª Ø§Ø¹ØªØ¨Ø§Ø± ÙØ¹Ù„ÛŒ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§: "
            f"{current_weeks} Ù‡ÙØªÙ‡\n\n"
            "Ù…Ø¯ÛŒØ±ÛŒØª ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ù‚Ø¯ÛŒÙ…ÛŒ Ø§Ø² Ø¨Ø®Ø´ "
            "Â«Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§Â» Ø§Ù†Ø¬Ø§Ù… Ù…ÛŒâ€ŒØ´ÙˆØ¯.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "ğŸ—“ Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§",
                            callback_data="settings_file_validity",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "ğŸ—‘ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
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
            "ğŸ—“ Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§\n\n"
            f"Ù…Ø¯Øª Ø§Ø¹ØªØ¨Ø§Ø± ÙØ¹Ù„ÛŒ: {current_weeks} Ù‡ÙØªÙ‡\n\n"
            f"ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ ÙØ¹Ø§Ù„ Ù‚Ø¯ÛŒÙ…ÛŒâ€ŒØªØ± Ø§Ø² Ø§ÛŒÙ† Ø¨Ø§Ø²Ù‡: "
            f"{expired_count}\n\n"
            "ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ù‚Ø¯ÛŒÙ…ÛŒ ÙÙ‚Ø· Ù¾Ø³ Ø§Ø² ØªØ£ÛŒÛŒØ¯ Ø´Ù…Ø§ "
            "Ø§Ø² Ú©Ø§Ù†Ø§Ù„ Ø­Ø°Ù Ùˆ Ø¨Ù‡ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„ Ù…Ù†ØªÙ‚Ù„ Ù…ÛŒâ€ŒØ´ÙˆÙ†Ø¯.\n"
            "Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ø¢Ù†â€ŒÙ‡Ø§ Ø¯Ø± Ø¯ÛŒØªØ§Ø¨ÛŒØ³ Ø¨Ø§Ù‚ÛŒ Ù…ÛŒâ€ŒÙ…Ø§Ù†Ø¯.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "âœï¸ ØªØºÛŒÛŒØ± Ù…Ø¯Øª Ø§Ø¹ØªØ¨Ø§Ø±",
                            callback_data="settings_file_validity_edit",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "ğŸ—‘ Ù…Ø´Ø§Ù‡Ø¯Ù‡ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
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
            "âœï¸ ØªØºÛŒÛŒØ± Ù…Ø¯Øª Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§\n\n"
            f"Ù…Ù‚Ø¯Ø§Ø± ÙØ¹Ù„ÛŒ: {current_weeks} Ù‡ÙØªÙ‡\n\n"
            "ØªØ¹Ø¯Ø§Ø¯ Ù‡ÙØªÙ‡ Ø¬Ø¯ÛŒØ¯ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "Ù…Ø«Ø§Ù„:\n8\n\n"
            "Ù‚Ø¨Ù„ Ø§Ø² Ø­Ø°Ù Ù‡Ø± ÙØ§ÛŒÙ„ÛŒØŒ "
            "ÛŒÚ© ØªØ£ÛŒÛŒØ¯ Ù†Ù‡Ø§ÛŒÛŒ Ø§Ø² Ø´Ù…Ø§ Ú¯Ø±ÙØªÙ‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯."
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
                "âŒ Ù…Ù‚Ø¯Ø§Ø± Ø§Ø¹ØªØ¨Ø§Ø± Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
                reply_markup=settings_menu(),
            )
            return

        if weeks < 1 or weeks > 104:
            await query.edit_message_text(
                "âŒ Ù…Ù‚Ø¯Ø§Ø± Ø§Ø¹ØªØ¨Ø§Ø± Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
                reply_markup=settings_menu(),
            )
            return

        await query.edit_message_text(
            "â³ Ø¯Ø± Ø­Ø§Ù„ Ø§Ø¹Ù…Ø§Ù„ ØªÙ†Ø¸ÛŒÙ… Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§...\n\n"
            "Ù„Ø·ÙØ§Ù‹ Ú†Ù†Ø¯ Ù„Ø­Ø¸Ù‡ ØµØ¨Ø± Ú©Ù†ÛŒØ¯."
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
            "âœ… ØªÙ†Ø¸ÛŒÙ… Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ Ø§Ø¹Ù…Ø§Ù„ Ø´Ø¯.\n\n"
            f"Ù…Ø¯Øª Ø§Ø¹ØªØ¨Ø§Ø±: {weeks} Ù‡ÙØªÙ‡\n"
            f"ğŸ—‘ Ù…Ù†ØªÙ‚Ù„â€ŒØ´Ø¯Ù‡ Ø¨Ù‡ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„: "
            f"{archived_count}\n"
        )

        if failed_count:
            text += (
                f"âš ï¸ Ù†Ø§Ù…ÙˆÙÙ‚: {failed_count}\n\n"
                "ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ÛŒ Ù†Ø§Ù…ÙˆÙÙ‚ Ø­Ø°Ù Ù†Ø´Ø¯Ù‡â€ŒØ§Ù†Ø¯ "
                "Ùˆ Ø¯Ø± Ø³ÛŒØ³ØªÙ… Ø¨Ø§Ù‚ÛŒ Ù…Ø§Ù†Ø¯Ù‡â€ŒØ§Ù†Ø¯."
            )
        else:
            text += (
                "\nÙ‡Ù…Ù‡ Ø¹Ù…Ù„ÛŒØ§Øª Ø¨Ø¯ÙˆÙ† Ø®Ø·Ø§ Ø§Ù†Ø¬Ø§Ù… Ø´Ø¯."
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "ğŸ—‘ Ø³Ø·Ù„ Ø¢Ø´ØºØ§Ù„",
                            callback_data="trash_page_0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "ğŸ—“ Ø§Ø¹ØªØ¨Ø§Ø± ÙØ§ÛŒÙ„â€ŒÙ‡Ø§",
                            callback_data="settings_file_validity",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "â¬…ï¸ ØªÙ†Ø¸ÛŒÙ…Ø§Øª",
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
            "âš™ï¸ ØªÙ†Ø¸ÛŒÙ…Ø§Øª\n\n"
            "Ù…Ø¯ÛŒØ±ÛŒØª ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ØŒ Ø²Ù…Ø§Ù†â€ŒØ¨Ù†Ø¯ÛŒØŒ Ø¢Ù…Ø§Ø±ØŒ CRM Ùˆ ØªÙ†Ø¸ÛŒÙ…Ø§Øª Ø§Ù†ØªØ´Ø§Ø± Ø§Ø² Ø§ÛŒÙ† Ø¨Ø®Ø´ Ø§Ù†Ø¬Ø§Ù… Ù…ÛŒâ€ŒØ´ÙˆØ¯.",
            reply_markup=build_settings_menu(
                session,
                settings_menu(),
            ),
        )
        return

    if action == "settings_emojis":
        await query.edit_message_text(
            "ğŸ¨ ØªØºÛŒÛŒØ±Ø§Øª Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù¾ÛŒØ§Ù…\n\n"
            "Ø§ÛŒÙ…ÙˆØ¬ÛŒ Ù‡Ø± Ø¨Ø®Ø´ Ø§Ø² Ù…ØªÙ† ÙØ§ÛŒÙ„ Ø±Ø§ Ø§Ø² Ø§ÛŒÙ†Ø¬Ø§ ØªØºÛŒÛŒØ± Ø¯Ù‡ÛŒØ¯.",
            reply_markup=emoji_settings_menu(),
        )
        return

    if action == "settings_footer":
        await query.edit_message_text(
            "ğŸ“ ØªØºÛŒÛŒØ±Ø§Øª Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ø«Ø§Ø¨Øª ÙÙˆØªØ±\n\n"
            "Ù…ØªÙ† Ùˆ Ø§Ø·Ù„Ø§Ø¹Ø§Øª Ø«Ø§Ø¨ØªÛŒ Ú©Ù‡ Ø²ÛŒØ± Ù‡Ù…Ù‡ ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ Ù†Ù…Ø§ÛŒØ´ Ø¯Ø§Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯ Ø±Ø§ Ø§Ø² Ø§ÛŒÙ†Ø¬Ø§ ØªÙ†Ø¸ÛŒÙ… Ú©Ù†ÛŒØ¯.",
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
            "ğŸ‘¥ Ù†Ø§Ù… Ùˆ Ø´Ù…Ø§Ø±Ù‡ ØªÙ…Ø§Ø³\n\n"
            "Ù…Ø®Ø§Ø·Ø¨ÛŒÙ† ÙØ¹Ù„ÛŒ:\n"
            f"{current_text}\n\n"
            "Ù†Ø§Ù… Ø´Ø®Øµ Ø§ÙˆÙ„ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
            "Ø¨Ø¹Ø¯ Ø§Ø² Ø«Ø¨Øª Ù†Ø§Ù… Ùˆ Ø´Ù…Ø§Ø±Ù‡ØŒ Ø±Ø¨Ø§Øª Ù…ÛŒâ€ŒÙ¾Ø±Ø³Ø¯ "
            "Ø¢ÛŒØ§ Ø´Ø®Øµ Ø¯ÛŒÚ¯Ø±ÛŒ Ù‡Ù… Ù‡Ø³Øª ÛŒØ§ Ù†Ù‡.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "ğŸ—‘ Ø­Ø°Ù Ù‡Ù…Ù‡ Ù…Ø®Ø§Ø·Ø¨ÛŒÙ†",
                            callback_data="settings_footer_contacts_clear_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "âŒ Ø§Ù†ØµØ±Ø§Ù",
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
                "âŒ Ù‡Ù†ÙˆØ² Ø´Ø®ØµÛŒ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
                reply_markup=footer_settings_menu(),
            )
            return

        context.user_data[
            "state"
        ] = "footer_contact_name"

        await query.edit_message_text(
            f"ğŸ‘¥ Ø«Ø¨Øª Ø´Ø®Øµ {len(people) + 1}\n\n"
            "Ù†Ø§Ù… Ø´Ø®Øµ Ø¨Ø¹Ø¯ÛŒ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯."
        )
        return

    if action == "settings_footer_contacts_finish":
        people = context.user_data.get(
            "footer_contact_people_draft",
            [],
        )

        if not people:
            await query.edit_message_text(
                "âŒ Ù‡ÛŒÚ† Ø´Ø®ØµÛŒ Ø¨Ø±Ø§ÛŒ Ø°Ø®ÛŒØ±Ù‡ Ø«Ø¨Øª Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
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
                "âœ… Ù†Ø§Ù… Ùˆ Ø´Ù…Ø§Ø±Ù‡ ØªÙ…Ø§Ø³â€ŒÙ‡Ø§ Ø°Ø®ÛŒØ±Ù‡ Ø´Ø¯.\n\n"
                + format_contact_people(people)
            ),
            reply_markup=footer_bulk_offer_markup(),
        )
        return

    if action == "settings_footer_contacts_clear_confirm":
        await query.edit_message_text(
            "âš ï¸ Ø­Ø°Ù Ù‡Ù…Ù‡ Ù†Ø§Ù…â€ŒÙ‡Ø§ Ùˆ Ø´Ù…Ø§Ø±Ù‡â€ŒÙ‡Ø§ÛŒ ØªÙ…Ø§Ø³\n\n"
            "Ø¢ÛŒØ§ Ù…Ø·Ù…Ø¦Ù† Ù‡Ø³ØªÛŒØ¯ØŸ",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "âœ… Ø¨Ù„Ù‡ØŒ Ø­Ø°Ù Ø´ÙˆÙ†Ø¯",
                            callback_data="settings_footer_contacts_clear_do",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "âŒ Ø®ÛŒØ±",
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
                "âœ… Ù‡Ù…Ù‡ Ù†Ø§Ù…â€ŒÙ‡Ø§ Ùˆ Ø´Ù…Ø§Ø±Ù‡â€ŒÙ‡Ø§ÛŒ ØªÙ…Ø§Ø³ Ø­Ø°Ù Ø´Ø¯Ù†Ø¯."
            ),
            reply_markup=footer_bulk_offer_markup(),
        )
        return

    if action == "attribute_rules":
        await query.edit_message_text(
            "ğŸ§© Ù…Ø¯ÛŒØ±ÛŒØª Ù…Ø´Ø®ØµØ§Øª Ù¾ÙˆÛŒØ§\n\n"
            "Ø§Ø² Ø§ÛŒÙ† Ø¨Ø®Ø´ Ù…ÛŒâ€ŒØªÙˆØ§Ù†ÛŒØ¯ Ù…Ø´Ø®ØµÙ‡â€ŒÙ‡Ø§ÛŒ Ø¬Ø¯ÛŒØ¯ Ø¨Ø±Ø§ÛŒ Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø®ÙˆØ¯Ú©Ø§Ø± Ø§Ø² ÙØ§ÛŒÙ„â€ŒÙ‡Ø§ ØªØ¹Ø±ÛŒÙ Ú©Ù†ÛŒØ¯.",
            reply_markup=attribute_rules_menu(),
        )
        return

    if action == "attribute_rules_list":
        rows = get_attribute_rules()

        if not rows:
            await query.edit_message_text(
                "ğŸ“‹ Ù‡Ù†ÙˆØ² Ù…Ø´Ø®ØµÙ‡ Ù¾ÙˆÛŒØ§ÛŒÛŒ ØªØ¹Ø±ÛŒÙ Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.",
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

            status = "âœ…" if is_active else "â›”"

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
                    "â¬…ï¸ Ø¨Ø§Ø²Ú¯Ø´Øª",
                    callback_data="attribute_rules",
                )
            ]
        )

        await query.edit_message_text(
            "ğŸ“‹ Ù…Ø´Ø®ØµØ§Øª Ù¾ÙˆÛŒØ§\n\n"
            "Ø¨Ø±Ø§ÛŒ Ù…Ø¯ÛŒØ±ÛŒØª Ù‡Ø± Ù…Ø´Ø®ØµÙ‡ Ø±ÙˆÛŒ Ø¢Ù† Ø¨Ø²Ù†ÛŒØ¯.",
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
                "âŒ Ø§ÛŒÙ† Ù…Ø´Ø®ØµÙ‡ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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

        status = "ÙØ¹Ø§Ù„ âœ…" if is_active else "ØºÛŒØ±ÙØ¹Ø§Ù„ â›”"

        await query.edit_message_text(
            f"ğŸ§© {attribute_label}\n\n"
            f"ÙˆØ¶Ø¹ÛŒØª: {status}\n"
            f"Ù†ÙˆØ¹: {match_type}\n"
            f"Ø¹Ø¨Ø§Ø±Øª ØªØ´Ø®ÛŒØµ: {trigger_text}\n"
            f"Ù…Ù‚Ø¯Ø§Ø± Ù¾ÛŒØ´â€ŒÙØ±Ø¶: {default_value or '-'}",
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
                "âŒ Ø§ÛŒÙ† Ù…Ø´Ø®ØµÙ‡ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
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
            "ÙØ¹Ø§Ù„ Ø´Ø¯ âœ…"
            if new_status
            else "ØºÛŒØ±ÙØ¹Ø§Ù„ Ø´Ø¯ â›”"
        )

        await query.edit_message_text(
            f"ğŸ§© {attribute_label}\n\n"
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
                "âŒ Ø§ÛŒÙ† Ù…Ø´Ø®ØµÙ‡ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
                reply_markup=attribute_rules_menu(),
            )
            return

        await query.edit_message_text(
            f"ğŸ—‘ Ù…Ø´Ø®ØµÙ‡ Â«{label}Â» Ø­Ø°Ù Ø´Ø¯.",
            reply_markup=attribute_rules_menu(),
        )
        return

    if action == "attribute_rule_add":
        context.user_data["state"] = "attribute_rule_label"

        await query.edit_message_text(
            "â• Ø§ÙØ²ÙˆØ¯Ù† Ù…Ø´Ø®ØµÙ‡ Ø¬Ø¯ÛŒØ¯\n\n"
            "Ù†Ø§Ù… Ù†Ù…Ø§ÛŒØ´ÛŒ Ù…Ø´Ø®ØµÙ‡ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n"
            "Ù…Ø«Ø§Ù„:\nÙˆÛŒÙˆ",
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
                "âŒ Ù†ÙˆØ¹ Rule Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
                reply_markup=attribute_rules_menu(),
            )
            return

        context.user_data["attribute_rule_match_type"] = match_type
        context.user_data["state"] = "attribute_rule_trigger"

        examples = {
            "PRESENCE": "Ù…Ø«Ø§Ù„:\nÙ„Ø§Ø¨ÛŒ",
            "VALUE_AFTER": "Ù…Ø«Ø§Ù„:\nÙˆÛŒÙˆ",
            "NUMBER_BEFORE": "Ù…Ø«Ø§Ù„:\nÙˆØ§Ø­Ø¯ Ø¯Ø± Ø·Ø¨Ù‚Ù‡",
        }

        await query.edit_message_text(
            "Ø¹Ø¨Ø§Ø±Øª ØªØ´Ø®ÛŒØµ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\n\n"
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
                "âŒ ØªÙ†Ø¸ÛŒÙ… Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.",
                reply_markup=settings_menu(),
            )
            return

        context.user_data["state"] = "edit_publish_setting"
        context.user_data["setting_key"] = setting_key

        current_value = get_app_setting(
            setting_key,
            "ØªÙ†Ø¸ÛŒÙ… Ù†Ø´Ø¯Ù‡",
        )

        await query.edit_message_text(
            f"{label}\n\n"
            f"Ù…Ù‚Ø¯Ø§Ø± ÙØ¹Ù„ÛŒ:\n{current_value}\n\n"
            "Ù…Ù‚Ø¯Ø§Ø± Ø¬Ø¯ÛŒØ¯ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.",
            reply_markup=back_menu(),
        )
        return

    if action == "logs":
        await query.edit_message_text(
            "ğŸ§¾ Ù„Ø§Ú¯â€ŒÙ‡Ø§",
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
            "ØµÙØ­Ù‡ Ù†Ø§Ù…Ø¹ØªØ¨Ø± Ø§Ø³Øª.",
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
            "Ø§ÛŒÙ† Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ø¯ÛŒÚ¯Ø± Ù…ÙˆØ¬ÙˆØ¯ Ù†ÛŒØ³Øª.",
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
            "Ø§ÛŒÙ† Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ø¯ÛŒÚ¯Ø± ÙØ¹Ø§Ù„ Ù†ÛŒØ³Øª.",
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
            "ÙØ§ÛŒÙ„ÛŒ Ø¯Ø± Ø§ÛŒÙ† Ø¯Ø³ØªÙ‡â€ŒØ¨Ù†Ø¯ÛŒ Ø¨Ø§Ù‚ÛŒ Ù†Ù…Ø§Ù†Ø¯Ù‡ Ø§Ø³Øª.",
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
                f"ğŸ“„ Ø´Ù…Ø§ Ù‡Ù…ÛŒÙ† Ø­Ø§Ù„Ø§ Ø¯Ø± ØµÙØ­Ù‡ "
                f"{current_page} Ø§Ø² {total_pages} Ù‡Ø³ØªÛŒØ¯."
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
            "â³ Ø¯Ø± Ø­Ø§Ù„ ØªØºÛŒÛŒØ± ØµÙØ­Ù‡..."
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
                    "ğŸ“ Ø§Ø±Ø³Ø§Ù„ Ø¨Ø± Ø§Ø³Ø§Ø³ Ù…Ù†Ø·Ù‚Ù‡",
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
            button_handler,
            pattern=r"^(?!catnav_|regcat_)",
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
