from __future__ import annotations

import re

from typing import Optional

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# =========================================================
# DATABASE CONNECTION
# =========================================================

def _db_connect():
    import os

    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


# =========================================================
# CHANNEL VISIT BUTTON
# =========================================================

def build_visit_keyboard(
    bot_username: str,
    property_id: int,
):
    username = str(
        bot_username or ""
    ).strip().lstrip("@")

    if not username:
        raise ValueError(
            "Bot username is required "
            "for visit deep link"
        )

    url = (
        f"https://t.me/{username}"
        f"?start=visit_{int(property_id)}"
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏠 بازدید از این واحد می‌خوام",
                    url=url,
                )
            ]
        ]
    )


# =========================================================
# PROPERTY AVAILABILITY
# =========================================================

def get_visitable_property(
    property_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    unit_code,
                    address_text,
                    district,
                    area_m2,
                    bedrooms,
                    floor_text,
                    current_price_billion,
                    current_price_toman,
                    channel_post_url,
                    published_message_id
                FROM properties
                WHERE id = %s
                  AND status = 'ACTIVE'
                  AND published_message_id IS NOT NULL
                  AND deleted_from_channel_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM property_trash pt
                      WHERE pt.property_id = properties.id
                  )
                LIMIT 1
                """,
                (property_id,),
            )

            return cur.fetchone()


# =========================================================
# CUSTOMER PROFILE
# =========================================================

def upsert_customer_profile(
    *,
    telegram_user_id: int,
    telegram_username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_profiles (
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                    created_at,
                    updated_at,
                    last_interaction_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    NOW(),
                    NOW(),
                    NOW()
                )
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET
                    telegram_username =
                        EXCLUDED.telegram_username,
                    first_name =
                        EXCLUDED.first_name,
                    last_name =
                        EXCLUDED.last_name,
                    updated_at = NOW(),
                    last_interaction_at = NOW()
                RETURNING
                    id,
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                    phone_number
                """,
                (
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                ),
            )

            row = cur.fetchone()
            conn.commit()

            return row


def get_customer_by_telegram_id(
    telegram_user_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    telegram_user_id,
                    telegram_username,
                    first_name,
                    last_name,
                    phone_number
                FROM customer_profiles
                WHERE telegram_user_id = %s
                LIMIT 1
                """,
                (telegram_user_id,),
            )

            return cur.fetchone()


def save_customer_phone(
    *,
    telegram_user_id: int,
    phone_number: str,
):
    phone_number = str(
        phone_number or ""
    ).strip()

    if not phone_number:
        raise ValueError(
            "phone_number is required"
        )

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_profiles
                SET
                    phone_number = %s,
                    phone_shared_at = NOW(),
                    updated_at = NOW(),
                    last_interaction_at = NOW()
                WHERE telegram_user_id = %s
                RETURNING id
                """,
                (
                    phone_number,
                    telegram_user_id,
                ),
            )

            row = cur.fetchone()
            conn.commit()

            return row


# =========================================================
# PENDING ACTION
# =========================================================

def set_pending_visit(
    *,
    customer_id: int,
    property_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_pending_actions (
                    customer_id,
                    action_type,
                    property_id,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    'VISIT',
                    %s,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (customer_id)
                DO UPDATE SET
                    action_type = 'VISIT',
                    property_id =
                        EXCLUDED.property_id,
                    updated_at = NOW()
                """,
                (
                    customer_id,
                    property_id,
                ),
            )

            conn.commit()


def get_pending_visit(
    customer_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    customer_id,
                    property_id
                FROM customer_pending_actions
                WHERE customer_id = %s
                  AND action_type = 'VISIT'
                LIMIT 1
                """,
                (customer_id,),
            )

            return cur.fetchone()


def clear_pending_action(
    customer_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM customer_pending_actions
                WHERE customer_id = %s
                """,
                (customer_id,),
            )

            conn.commit()


# =========================================================
# VISIT REQUEST
# =========================================================

def create_or_get_visit_request(
    *,
    customer_id: int,
    property_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    status
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
                return (
                    existing[0],
                    False,
                )

            cur.execute(
                """
                SELECT
                    unit_code,
                    channel_post_url
                FROM properties
                WHERE id = %s
                LIMIT 1
                """,
                (property_id,),
            )

            property_row = cur.fetchone()

            if not property_row:
                raise ValueError(
                    "Property not found"
                )

            unit_code = property_row[0]
            post_url = property_row[1]

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
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    'NEW',
                    'CHANNEL_BUTTON',
                    NOW(),
                    NOW(),
                    NOW()
                )
                RETURNING id
                """,
                (
                    customer_id,
                    property_id,
                    unit_code,
                    post_url,
                ),
            )

            request_id = cur.fetchone()[0]

            conn.commit()

            return (
                request_id,
                True,
            )


def get_visit_request_detail(
    request_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    vr.id,
                    vr.status,
                    vr.property_id,
                    vr.unit_code_snapshot,
                    vr.channel_post_url_snapshot,

                    cp.id,
                    cp.telegram_user_id,
                    cp.telegram_username,
                    cp.first_name,
                    cp.last_name,
                    cp.phone_number,

                    p.address_text,
                    p.district,
                    p.area_m2,
                    p.bedrooms,
                    p.floor_text,
                    p.current_price_billion,
                    p.current_price_toman

                FROM visit_requests vr

                JOIN customer_profiles cp
                  ON cp.id = vr.customer_id

                LEFT JOIN properties p
                  ON p.id = vr.property_id

                WHERE vr.id = %s

                LIMIT 1
                """,
                (request_id,),
            )

            return cur.fetchone()


def mark_visit_notified(
    request_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE visit_requests
                SET
                    notified_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (request_id,),
            )

            conn.commit()


def set_visit_status(
    request_id: int,
    status: str,
):
    allowed = {
        "NEW",
        "CONTACTED",
        "SCHEDULED",
        "CLOSED",
        "CANCELLED",
    }

    if status not in allowed:
        raise ValueError(
            f"Invalid visit status: {status}"
        )

    timestamp_sql = {
        "CONTACTED":
            "contacted_at = COALESCE(contacted_at, NOW()),",
        "SCHEDULED":
            "scheduled_at = COALESCE(scheduled_at, NOW()),",
        "CLOSED":
            "closed_at = COALESCE(closed_at, NOW()),",
        "CANCELLED":
            "closed_at = COALESCE(closed_at, NOW()),",
    }.get(
        status,
        "",
    )

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE visit_requests
                SET
                    status = %s,
                    {timestamp_sql}
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    status,
                    request_id,
                ),
            )

            conn.commit()


# =========================================================
# OPERATORS
# =========================================================

def get_visit_operator_telegram_ids():
    # Recipients for NEW visit requests:
    # OWNER + explicit permission holders + legacy visit_operators.
    with _db_connect() as conn:
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
                    JOIN user_permissions up ON up.user_id = u.id
                    WHERE u.is_active = TRUE
                      AND up.permission_key = 'receive_visit_requests'
                      AND up.is_allowed = TRUE

                    UNION

                    SELECT u.id
                    FROM users u
                    JOIN visit_operators vo ON vo.user_id = u.id
                    WHERE u.is_active = TRUE
                      AND vo.is_active = TRUE
                      AND vo.notify_new_visits = TRUE
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
                """
            )
            return [row[1] for row in cur.fetchall() if row[1]]
def list_visit_operators():
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    vo.id,
                    u.id,
                    u.username,
                    u.display_name,
                    u.role,
                    vo.is_active,
                    vo.notify_new_visits
                FROM visit_operators vo
                JOIN users u
                  ON u.id = vo.user_id
                ORDER BY u.id
                """
            )

            return cur.fetchall()


def visit_request_is_notified(
    request_id: int,
):
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT notified_at
                FROM visit_requests
                WHERE id = %s
                LIMIT 1
                """,
                (request_id,),
            )

            row = cur.fetchone()

            return bool(
                row
                and row[0] is not None
            )



# =========================================================
# ORIGINAL / PRIMARY PROPERTY SOURCE
# =========================================================

def get_primary_property_source(
    property_id: int,
):
    """
    Return the best known original source for a property.

    Active + primary sources are preferred, while older
    inactive source records remain available as fallback.
    """

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ps.source_unit_code,
                    ps.source_post_url,
                    ps.source_message_id,
                    ps.source_raw_text,
                    c.title,
                    c.username,
                    c.telegram_channel_id,
                    ps.is_active,
                    ps.is_primary

                FROM property_sources ps

                LEFT JOIN channels c
                  ON c.id = ps.source_channel_id

                WHERE ps.property_id = %s

                ORDER BY
                    ps.is_active DESC,
                    ps.is_primary DESC,
                    ps.id ASC

                LIMIT 1
                """,
                (property_id,),
            )

            return cur.fetchone()


_BOT_USERNAME_CACHE = None


async def build_visit_keyboard_for_bot(
    bot,
    property_id: int,
):
    global _BOT_USERNAME_CACHE

    if not _BOT_USERNAME_CACHE:
        me = await bot.get_me()

        _BOT_USERNAME_CACHE = (
            me.username
            if me
            else None
        )

    return build_visit_keyboard(
        _BOT_USERNAME_CACHE,
        property_id,
    )



# =========================================================
# OPERATOR SOURCE CONTEXT
# =========================================================

def get_operator_source_context(
    property_id: int,
):
    """
    Prefer structured property_sources.

    For legacy INPUT properties that predate proper source
    tracking, safely recover:
      - Telegram channel username
      - source listing code

    from raw_text.

    We NEVER invent a direct Telegram post URL.
    """

    source = get_primary_property_source(
        property_id
    )

    if source:
        source_unit_code = source[0]
        source_post_url = source[1]
        source_message_id = source[2]
        source_title = source[4]
        source_username = source[5]

        username = (
            str(source_username)
            .strip()
            .lstrip("@")
            if source_username
            else None
        )

        direct_post_url = (
            source_post_url
        )

        if (
            not direct_post_url
            and username
            and source_message_id
        ):
            direct_post_url = (
                f"https://t.me/{username}/"
                f"{source_message_id}"
            )

        channel_url = (
            f"https://t.me/{username}"
            if username
            else None
        )

        if source_title:
            source_name = str(
                source_title
            ).strip()

        elif username:
            source_name = (
                f"@{username}"
            )

        else:
            source_name = (
                "منبع ثبت‌شده"
            )

        return {
            "kind": "STRUCTURED",
            "source_name": source_name,
            "source_username": username,
            "source_unit_code": (
                source_unit_code
                or None
            ),
            "direct_post_url": (
                direct_post_url
                or None
            ),
            "channel_url": (
                channel_url
                or None
            ),
        }

    # -----------------------------------------------------
    # Legacy fallback from properties.raw_text
    # -----------------------------------------------------

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    raw_text,
                    source_kind
                FROM properties
                WHERE id = %s
                LIMIT 1
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        return None

    raw_text = str(
        row[0] or ""
    )

    source_kind = row[1]

    username = None

    # Strongest pattern:
    # کانال تلگرام
    # @ChannelUsername
    match = re.search(
        r"کانال\s*تلگرام"
        r"[\s:：\-–—]*"
        r"@([A-Za-z0-9_]{5,})",
        raw_text,
        flags=re.IGNORECASE,
    )

    if match:
        username = match.group(1)

    # Fallback: first Telegram-compatible @username.
    # Instagram handles containing "." won't match.
    if not username:
        match = re.search(
            r"@([A-Za-z0-9_]{5,})",
            raw_text,
        )

        if match:
            username = match.group(1)

    source_unit_code = None

    try:
        from property_parser import (
            parse_source_unit_code,
        )

        source_unit_code = (
            parse_source_unit_code(
                raw_text
            )
        )

    except Exception:
        source_unit_code = None

    if username:
        return {
            "kind": "RAW_TEXT",
            "source_name": (
                f"@{username}"
            ),
            "source_username": username,
            "source_unit_code": (
                source_unit_code
                or None
            ),
            "direct_post_url": None,
            "channel_url": (
                f"https://t.me/{username}"
            ),
        }

    return {
        "kind": (
            "MANUAL"
            if source_kind == "INPUT"
            else "UNKNOWN"
        ),
        "source_name": (
            "ورود دستی / منبع ثبت نشده"
        ),
        "source_username": None,
        "source_unit_code": (
            source_unit_code
            or None
        ),
        "direct_post_url": None,
        "channel_url": None,
    }
