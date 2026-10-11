from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone

import psycopg
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from shahfile_ai import (
    _search_where,
    activate_search_subscription,
    decline_search_subscription,
    normalize_search_filters,
    search_count,
    search_rows,
)
from shahfile_smart import (
    clear_conversation,
    get_customer,
    set_conversation,
    upsert_customer_from_telegram,
)


PAGE_SIZE = 6
BOT_USERNAME_CACHE = None
CAMPAIGN_WATCH_INTERVAL_SECONDS = max(
    20,
    int(
        os.getenv(
            "CAMPAIGN_WATCH_INTERVAL_SECONDS",
            "30",
        )
        or 30
    ),
)


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def ensure_showcase_schema():
    """
    Upgrade the old static showcase into a live lead campaign.

    All statements are idempotent so the production deploy remains
    restart-safe and requires no manual migration.
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_showcases (
                    id BIGSERIAL PRIMARY KEY,
                    token VARCHAR(40) NOT NULL UNIQUE,
                    created_by_user_id BIGINT
                        REFERENCES users(id)
                        ON DELETE SET NULL,
                    query_text TEXT NOT NULL,
                    filters JSONB NOT NULL
                        DEFAULT '{}'::jsonb,
                    is_active BOOLEAN NOT NULL
                        DEFAULT TRUE,
                    expires_at TIMESTAMPTZ NOT NULL
                        DEFAULT (
                            NOW()
                            + INTERVAL '3650 days'
                        ),
                    created_at TIMESTAMPTZ NOT NULL
                        DEFAULT NOW(),
                    disabled_at TIMESTAMPTZ
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE customer_showcases
                ADD COLUMN IF NOT EXISTS campaign_name TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE customer_showcases
                ADD COLUMN IF NOT EXISTS notify_immediately
                    BOOLEAN NOT NULL DEFAULT TRUE
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    customer_showcase_items (
                        showcase_id BIGINT NOT NULL
                            REFERENCES customer_showcases(id)
                            ON DELETE CASCADE,
                        property_id BIGINT NOT NULL
                            REFERENCES properties(id)
                            ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        PRIMARY KEY (
                            showcase_id,
                            property_id
                        )
                    )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    customer_showcase_visitors (
                        showcase_id BIGINT NOT NULL
                            REFERENCES customer_showcases(id)
                            ON DELETE CASCADE,
                        telegram_user_id BIGINT NOT NULL,
                        customer_id BIGINT
                            REFERENCES customer_profiles(id)
                            ON DELETE SET NULL,
                        subscription_id BIGINT
                            REFERENCES smart_search_subscriptions(id)
                            ON DELETE SET NULL,
                        first_opened_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        last_opened_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        open_count INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (
                            showcase_id,
                            telegram_user_id
                        )
                    )
                """
            )

            for statement in (
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS id BIGSERIAL
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS is_active
                    BOOLEAN NOT NULL DEFAULT TRUE
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS joined_at
                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS activated_at
                    TIMESTAMPTZ
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS deactivated_at
                    TIMESTAMPTZ
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS phone_collected_at
                    TIMESTAMPTZ
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS last_scan_at
                    TIMESTAMPTZ
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS last_catalog_message_id
                    BIGINT
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS last_catalog_offset
                    INTEGER NOT NULL DEFAULT 0
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS last_catalog_hash
                    VARCHAR(64)
                """,
                """
                ALTER TABLE customer_showcase_visitors
                ADD COLUMN IF NOT EXISTS last_catalog_synced_at
                    TIMESTAMPTZ
                """,
            ):
                cur.execute(statement)

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_showcase_visitors_id
                ON customer_showcase_visitors(id)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    customer_campaign_deliveries (
                        id BIGSERIAL PRIMARY KEY,
                        showcase_id BIGINT NOT NULL
                            REFERENCES customer_showcases(id)
                            ON DELETE CASCADE,
                        telegram_user_id BIGINT NOT NULL,
                        property_id BIGINT NOT NULL
                            REFERENCES properties(id)
                            ON DELETE CASCADE,
                        delivery_status VARCHAR(20) NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        queued_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        next_attempt_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        sent_at TIMESTAMPTZ,
                        telegram_message_id BIGINT,
                        last_error TEXT,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT NOW(),
                        UNIQUE (
                            showcase_id,
                            telegram_user_id,
                            property_id
                        ),
                        CONSTRAINT campaign_delivery_status_check
                            CHECK (
                                delivery_status IN (
                                    'INITIAL',
                                    'PENDING',
                                    'SENT',
                                    'FAILED'
                                )
                            )
                    )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_showcase_items_position
                ON customer_showcase_items(
                    showcase_id,
                    position
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_showcase_visitors_user
                ON customer_showcase_visitors(
                    telegram_user_id,
                    last_opened_at DESC
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_showcase_visitors_campaign_active
                ON customer_showcase_visitors(
                    showcase_id,
                    is_active,
                    last_scan_at
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_campaign_deliveries_pending
                ON customer_campaign_deliveries(
                    delivery_status,
                    next_attempt_at,
                    id
                )
                """
            )

            # Existing links become long-lived campaigns too.
            cur.execute(
                """
                UPDATE customer_showcases
                SET
                    expires_at = GREATEST(
                        expires_at,
                        NOW() + INTERVAL '3650 days'
                    ),
                    campaign_name = COALESCE(
                        NULLIF(campaign_name, ''),
                        query_text
                    )
                WHERE is_active = TRUE
                """
            )

        conn.commit()


def manager_filters_to_public(filters):
    filters = dict(filters or {})
    out = {}

    for source, target in {
        "price_min": "price_min_billion",
        "price_max": "price_max_billion",
        "area_min": "area_min",
        "area_max": "area_max",
        "bedrooms": "bedrooms",
        "build_year_min": "build_year_min",
        "build_year_max": "build_year_max",
    }.items():
        if filters.get(source) is not None:
            out[target] = filters[source]

    for key in (
        "free_text",
        "floor_text",
        "unit_code",
        "district",
    ):
        if filters.get(key):
            out[key] = filters[key]

    for key in (
        "has_parking",
        "has_elevator",
        "has_storage",
        "deed_required",
    ):
        if filters.get(key) is True:
            out[key] = True

    if filters.get("direction"):
        out["direction"] = filters["direction"]

    return normalize_search_filters(out)


def _campaign_filters(showcase):
    filters = dict(
        showcase.get("filters")
        or {}
    )

    # Old static showcases stored created_after only to distinguish
    # their snapshot from future alerts. A live campaign must always
    # render every currently matching active property.
    filters.pop(
        "created_after",
        None,
    )

    return normalize_search_filters(
        filters
    )


def create_showcase(
    created_by_user_id,
    query_text,
    filters,
    property_ids,
):
    ids = []
    seen = set()

    for value in property_ids or []:
        try:
            property_id = int(value)

        except (
            TypeError,
            ValueError,
        ):
            continue

        if property_id not in seen:
            ids.append(property_id)
            seen.add(property_id)

    filters = normalize_search_filters(
        filters
    )

    if not filters:
        return None

    token = (
        secrets.token_urlsafe(12)
        .replace("-", "")
        .replace("_", "")[:16]
    )

    campaign_name = (
        str(query_text or "").strip()
        or "کمپین جستجوی هوشمند"
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_showcases(
                    token,
                    created_by_user_id,
                    campaign_name,
                    query_text,
                    filters,
                    notify_immediately,
                    expires_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    TRUE,
                    NOW() + INTERVAL '3650 days'
                )
                RETURNING
                    id,
                    expires_at
                """,
                (
                    token,
                    created_by_user_id,
                    campaign_name,
                    campaign_name,
                    json.dumps(
                        filters,
                        ensure_ascii=False,
                    ),
                ),
            )

            showcase_id, expires_at = (
                cur.fetchone()
            )

            if ids:
                cur.executemany(
                    """
                    INSERT INTO customer_showcase_items(
                        showcase_id,
                        property_id,
                        position
                    )
                    VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        (
                            showcase_id,
                            property_id,
                            index,
                        )
                        for index, property_id
                        in enumerate(
                            ids,
                            start=1,
                        )
                    ],
                )

        conn.commit()

    return {
        "id": int(showcase_id),
        "token": token,
        "expires_at": expires_at,
        "count": search_count(filters),
    }


async def build_showcase_link(
    bot,
    token,
):
    global BOT_USERNAME_CACHE

    if not BOT_USERNAME_CACHE:
        me = await bot.get_me()

        BOT_USERNAME_CACHE = (
            me.username
            if me
            else None
        )

    if not BOT_USERNAME_CACHE:
        raise RuntimeError(
            "Bot username is unavailable"
        )

    return (
        f"https://t.me/{BOT_USERNAME_CACHE}"
        f"?start=showcase_{token}"
    )


def _load_showcase(
    token,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    token,
                    query_text,
                    filters,
                    expires_at,
                    created_by_user_id,
                    campaign_name,
                    notify_immediately
                FROM customer_showcases
                WHERE token = %s
                  AND is_active = TRUE
                  AND expires_at > NOW()
                LIMIT 1
                """,
                (token,),
            )

            row = cur.fetchone()

    if not row:
        return None

    return {
        "id": int(row[0]),
        "token": row[1],
        "query_text": row[2],
        "filters": dict(
            row[3]
            or {}
        ),
        "expires_at": row[4],
        "created_by_user_id": row[5],
        "campaign_name": (
            row[6]
            or row[2]
        ),
        "notify_immediately": bool(
            row[7]
        ),
    }


def _contact_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 ارسال شماره تماس من",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=(
            "برای دریافت فایل‌ها شماره‌تان را ارسال کنید"
        ),
    )


def _seed_current_matches(
    cur,
    showcase,
    telegram_user_id,
):
    where, params = _search_where(
        _campaign_filters(showcase)
    )

    cur.execute(
        f"""
        INSERT INTO customer_campaign_deliveries(
            showcase_id,
            telegram_user_id,
            property_id,
            delivery_status,
            queued_at,
            next_attempt_at,
            created_at,
            updated_at
        )
        SELECT
            %s,
            %s,
            p.id,
            'INITIAL',
            NOW(),
            NOW(),
            NOW(),
            NOW()
        FROM properties p
        WHERE {where}
        ON CONFLICT (
            showcase_id,
            telegram_user_id,
            property_id
        )
        DO UPDATE SET
            delivery_status = CASE
                WHEN customer_campaign_deliveries.delivery_status
                    = 'PENDING'
                THEN 'INITIAL'
                ELSE customer_campaign_deliveries.delivery_status
            END,
            updated_at = NOW()
        """,
        [
            showcase["id"],
            telegram_user_id,
            *params,
        ],
    )


def _activate_visitor(
    showcase,
    user,
):
    profile = upsert_customer_from_telegram(
        user
    )

    customer_id = (
        profile[0]
        if profile
        else None
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    is_active
                FROM customer_showcase_visitors
                WHERE showcase_id = %s
                  AND telegram_user_id = %s
                FOR UPDATE
                """,
                (
                    showcase["id"],
                    user.id,
                ),
            )

            existing = cur.fetchone()

            if existing:
                visitor_id = int(
                    existing[0]
                )
                was_active = bool(
                    existing[1]
                )

                cur.execute(
                    """
                    UPDATE customer_showcase_visitors
                    SET
                        customer_id = COALESCE(
                            customer_id,
                            %s
                        ),
                        is_active = TRUE,
                        activated_at = COALESCE(
                            activated_at,
                            NOW()
                        ),
                        deactivated_at = NULL,
                        phone_collected_at = COALESCE(
                            phone_collected_at,
                            NOW()
                        ),
                        last_opened_at = NOW(),
                        open_count = open_count + 1
                    WHERE id = %s
                    """,
                    (
                        customer_id,
                        visitor_id,
                    ),
                )

                if not was_active:
                    _seed_current_matches(
                        cur,
                        showcase,
                        user.id,
                    )

            else:
                cur.execute(
                    """
                    INSERT INTO customer_showcase_visitors(
                        showcase_id,
                        telegram_user_id,
                        customer_id,
                        is_active,
                        joined_at,
                        activated_at,
                        phone_collected_at,
                        first_opened_at,
                        last_opened_at,
                        open_count
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        TRUE,
                        NOW(),
                        NOW(),
                        NOW(),
                        NOW(),
                        NOW(),
                        1
                    )
                    RETURNING id
                    """,
                    (
                        showcase["id"],
                        user.id,
                        customer_id,
                    ),
                )

                visitor_id = int(
                    cur.fetchone()[0]
                )

                _seed_current_matches(
                    cur,
                    showcase,
                    user.id,
                )

        conn.commit()

    return visitor_id


def _get_visitor(
    showcase_id,
    telegram_user_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    is_active,
                    customer_id
                FROM customer_showcase_visitors
                WHERE showcase_id = %s
                  AND telegram_user_id = %s
                LIMIT 1
                """,
                (
                    showcase_id,
                    telegram_user_id,
                ),
            )

            return cur.fetchone()


def _fmt(
    value,
):
    if value is None:
        return "-"

    return f"{float(value):g}"


def _page(
    showcase,
    offset=0,
):
    filters = _campaign_filters(
        showcase
    )

    total = search_count(
        filters
    )

    if total:
        offset = min(
            max(0, int(offset or 0)),
            (
                (total - 1)
                // PAGE_SIZE
            )
            * PAGE_SIZE,
        )

    else:
        offset = 0

    rows = search_rows(
        filters,
        offset=offset,
        limit=PAGE_SIZE,
    )

    return rows, total, offset


def _render_hash(
    text,
):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def render_showcase(
    showcase,
    visitor_id,
    offset=0,
):
    rows, total, offset = _page(
        showcase,
        offset,
    )

    lines = [
        "🏠 فایل‌های اختصاصی شما",
        "",
        (
            "🔎 درخواست: "
            f"{showcase['query_text']}"
        ),
        f"📂 فایل‌های فعال: {total}",
        "",
    ]

    if not rows:
        lines.extend(
            [
                (
                    "در حال حاضر فایل فعالی مطابق این "
                    "درخواست وجود ندارد."
                ),
                (
                    "به‌محض اضافه‌شدن فایل مناسب، "
                    "همین‌جا برایتان ارسال می‌کنیم."
                ),
                "",
            ]
        )

    for index, row in enumerate(
        rows,
        start=offset + 1,
    ):
        (
            _property_id,
            unit_code,
            district,
            address,
            area,
            bedrooms,
            build_year,
            price,
            url,
            message_id,
        ) = row

        lines.append(
            f"{index}) 📍 "
            f"{address or district or 'بدون منطقه'}"
        )

        details = []

        if area is not None:
            details.append(
                f"📐 {_fmt(area)} متر"
            )

        if bedrooms is not None:
            details.append(
                f"🛏 {bedrooms} خواب"
            )

        if build_year:
            details.append(
                f"📅 سال ساخت {build_year}"
            )

        if details:
            lines.append(
                " | ".join(details)
            )

        if price is not None:
            lines.append(
                "💰 قیمت: "
                f"{_fmt(price)} میلیارد تومان"
            )

        lines.append(
            f"🔢 کد واحد: {unit_code or '-'}"
        )

        final_url = (
            url
            or (
                f"https://t.me/nimatmelk/"
                f"{message_id}"
                if message_id
                else None
            )
        )

        if final_url:
            lines.append(
                f"🔗 مشاهده فایل: {final_url}"
            )

        lines.extend(
            [
                "",
                "──────────────",
                "",
            ]
        )

    lines.extend(
        [
            (
                "📅 برای گرفتن وقت بازدید، فقط کافیه "
                "کلمه «بازدید» رو به همراه کد فایل ارسال کنید."
            ),
            "مثال: بازدید ۲۷۲۹",
        ]
    )

    keyboard = []
    navigation = []

    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️ فایل‌های قبلی",
                callback_data=(
                    f"cs_page_{showcase['token']}_"
                    f"{max(0, offset - PAGE_SIZE)}"
                ),
            )
        )

    if offset + PAGE_SIZE < total:
        navigation.append(
            InlineKeyboardButton(
                "فایل‌های دیگر ▶️",
                callback_data=(
                    f"cs_page_{showcase['token']}_"
                    f"{offset + PAGE_SIZE}"
                ),
            )
        )

    if navigation:
        keyboard.append(
            navigation
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "🔄 بروزرسانی فایل‌ها",
                callback_data=(
                    f"cs_page_{showcase['token']}_"
                    f"{offset}"
                ),
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                "🔕 توقف ارسال فایل‌های جدید",
                callback_data=(
                    f"cs_unsub_{visitor_id}"
                ),
            )
        ]
    )

    return (
        "\n".join(lines).strip(),
        InlineKeyboardMarkup(
            keyboard
        ),
        offset,
    )


def _record_catalog_message(
    visitor_id,
    message_id,
    offset,
    text,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_showcase_visitors
                SET
                    last_catalog_message_id = %s,
                    last_catalog_offset = %s,
                    last_catalog_hash = %s,
                    last_catalog_synced_at = NOW(),
                    last_opened_at = NOW()
                WHERE id = %s
                """,
                (
                    message_id,
                    offset,
                    _render_hash(text),
                    visitor_id,
                ),
            )

        conn.commit()


async def complete_showcase_registration(
    update,
    context,
    token,
):
    showcase = _load_showcase(
        token
    )

    if not showcase:
        await update.effective_message.reply_text(
            (
                "این لینک غیرفعال شده است. "
                "برای دریافت لینک جدید با مشاور تماس بگیرید."
            ),
            reply_markup=ReplyKeyboardRemove(),
        )
        return True

    user = update.effective_user
    customer = get_customer(
        user.id
    )

    if (
        not customer
        or not customer[4]
    ):
        set_conversation(
            user.id,
            "SHOWCASE_PHONE",
            {
                "showcase_token": token,
            },
            customer_id=(
                customer[0]
                if customer
                else None
            ),
        )

        await update.effective_message.reply_text(
            (
                "برای دریافت فایل‌های این دسته، "
                "لطفاً شماره تماس خودتون رو از دکمه زیر ارسال کنید."
            ),
            reply_markup=_contact_keyboard(),
        )

        return True

    visitor_id = _activate_visitor(
        showcase,
        user,
    )

    clear_conversation(
        user.id,
        customer_id=customer[0],
    )

    text, keyboard, offset = (
        render_showcase(
            showcase,
            visitor_id,
            0,
        )
    )

    sent = await update.effective_message.reply_text(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    _record_catalog_message(
        visitor_id,
        sent.message_id,
        offset,
        text,
    )

    await update.effective_message.reply_text(
        (
            "✅ عضویت شما در این دسته فعال شد.\n"
            "از این به بعد هر فایل جدیدی که دقیقاً "
            "با همین درخواست جور باشد، فوری برایتان ارسال می‌شود."
        ),
        reply_markup=ReplyKeyboardRemove(),
    )

    return True


async def handle_showcase_start(
    update,
    context,
):
    if (
        not context.args
        or not context.args[0].startswith(
            "showcase_"
        )
    ):
        return False

    token = context.args[0][
        len("showcase_"):
    ]

    showcase = _load_showcase(
        token
    )

    if not showcase:
        await update.message.reply_text(
            (
                "این لینک منقضی یا غیرفعال شده است. "
                "برای دریافت فایل‌های جدید با مشاور خود تماس بگیرید."
            )
        )
        return True

    profile = upsert_customer_from_telegram(
        update.effective_user
    )

    phone_number = (
        profile[1]
        if profile
        else None
    )

    if not phone_number:
        set_conversation(
            update.effective_user.id,
            "SHOWCASE_PHONE",
            {
                "showcase_token": token,
            },
            customer_id=(
                profile[0]
                if profile
                else None
            ),
        )

        await update.message.reply_text(
            (
                "سلام 👋\n"
                f"برای مشاهده «{showcase['campaign_name']}» "
                "ابتدا شماره تماس خودتون رو از دکمه زیر ارسال کنید.\n\n"
                "شماره شما فقط برای هماهنگی مشاوره و بازدید استفاده می‌شود."
            ),
            reply_markup=_contact_keyboard(),
        )

        return True

    return await complete_showcase_registration(
        update,
        context,
        token,
    )


async def handle_showcase_callback(
    update,
    context,
    session=None,
):
    query = update.callback_query
    action = (
        query.data
        if query
        else ""
    ) or ""

    if not action.startswith(
        "cs_"
    ):
        return False

    user_id = update.effective_user.id

    if action.startswith(
        "cs_page_"
    ):
        try:
            token, offset_text = action[
                len("cs_page_"):
            ].rsplit("_", 1)

            offset = max(
                0,
                int(offset_text),
            )

        except Exception:
            await query.answer(
                "صفحه نامعتبر است.",
                show_alert=True,
            )
            return True

        showcase = _load_showcase(
            token
        )

        if not showcase:
            await query.answer(
                "این لینک غیرفعال شده است.",
                show_alert=True,
            )
            return True

        visitor = _get_visitor(
            showcase["id"],
            user_id,
        )

        if (
            not visitor
            or not visitor[1]
        ):
            await query.answer(
                (
                    "عضویت این دسته فعال نیست؛ "
                    "لینک را دوباره باز کنید."
                ),
                show_alert=True,
            )
            return True

        visitor_id = int(
            visitor[0]
        )

        text, keyboard, offset = (
            render_showcase(
                showcase,
                visitor_id,
                offset,
            )
        )

        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        _record_catalog_message(
            visitor_id,
            query.message.message_id,
            offset,
            text,
        )

        try:
            await query.answer(
                "فهرست بروزرسانی شد ✅"
            )
        except Exception:
            pass

        return True

    if action.startswith(
        "cs_unsub_"
    ):
        try:
            visitor_id = int(
                action.rsplit("_", 1)[1]
            )

        except Exception:
            await query.answer(
                "درخواست نامعتبر است.",
                show_alert=True,
            )
            return True

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_showcase_visitors
                    SET
                        is_active = FALSE,
                        deactivated_at = NOW()
                    WHERE id = %s
                      AND telegram_user_id = %s
                    RETURNING id
                    """,
                    (
                        visitor_id,
                        user_id,
                    ),
                )

                changed = cur.fetchone()

            conn.commit()

        if changed:
            await query.edit_message_reply_markup(
                reply_markup=None
            )

            await query.message.reply_text(
                (
                    "ارسال فایل‌های جدید این دسته متوقف شد. "
                    "هر زمان لینک را دوباره باز کنید، عضویت فعال می‌شود."
                )
            )

        else:
            await query.answer(
                "این عضویت در دسترس نیست.",
                show_alert=True,
            )

        return True

    # Backward compatibility for buttons sent by V1.1 links.
    if (
        action.startswith(
            "cs_watch_yes_"
        )
        or action.startswith(
            "cs_watch_no_"
        )
    ):
        try:
            subscription_id = int(
                action.rsplit("_", 1)[1]
            )

        except Exception:
            await query.answer(
                "درخواست نامعتبر است.",
                show_alert=True,
            )
            return True

        if action.startswith(
            "cs_watch_yes_"
        ):
            ok = activate_search_subscription(
                subscription_id,
                user_id,
            )

            await query.message.reply_text(
                (
                    "اعلان فایل‌های جدید فعال شد ✅"
                    if ok
                    else "این درخواست در دسترس نیست."
                )
            )

        else:
            decline_search_subscription(
                subscription_id,
                user_id,
            )

            await query.message.reply_text(
                "اعلان فایل‌های مشابه متوقف شد."
            )

        return True

    return False


def _active_memberships(
    limit=200,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.id,
                    v.showcase_id,
                    v.telegram_user_id,
                    s.token,
                    s.query_text,
                    s.filters,
                    s.campaign_name
                FROM customer_showcase_visitors v
                JOIN customer_showcases s
                  ON s.id = v.showcase_id
                JOIN customer_profiles cp
                  ON cp.id = v.customer_id
                WHERE v.is_active = TRUE
                  AND s.is_active = TRUE
                  AND s.notify_immediately = TRUE
                  AND s.expires_at > NOW()
                  AND NULLIF(
                      TRIM(
                          COALESCE(
                              cp.phone_number,
                              ''
                          )
                      ),
                      ''
                  ) IS NOT NULL
                ORDER BY
                    v.last_scan_at NULLS FIRST,
                    v.id
                LIMIT %s
                """,
                (limit,),
            )

            rows = cur.fetchall()

    return [
        {
            "visitor_id": int(row[0]),
            "showcase_id": int(row[1]),
            "telegram_user_id": int(row[2]),
            "token": row[3],
            "query_text": row[4],
            "filters": dict(
                row[5]
                or {}
            ),
            "campaign_name": (
                row[6]
                or row[4]
            ),
        }
        for row in rows
    ]


def _queue_new_matches(
    membership,
    limit=10,
):
    showcase = {
        "filters": membership["filters"],
    }

    where, params = _search_where(
        _campaign_filters(showcase)
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO customer_campaign_deliveries(
                    showcase_id,
                    telegram_user_id,
                    property_id,
                    delivery_status,
                    queued_at,
                    next_attempt_at,
                    created_at,
                    updated_at
                )
                SELECT
                    %s,
                    %s,
                    p.id,
                    'PENDING',
                    NOW(),
                    NOW(),
                    NOW(),
                    NOW()
                FROM properties p
                WHERE {where}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM customer_campaign_deliveries d
                      WHERE d.showcase_id = %s
                        AND d.telegram_user_id = %s
                        AND d.property_id = p.id
                  )
                ORDER BY p.id
                LIMIT %s
                ON CONFLICT DO NOTHING
                """,
                [
                    membership["showcase_id"],
                    membership["telegram_user_id"],
                    *params,
                    membership["showcase_id"],
                    membership["telegram_user_id"],
                    limit,
                ],
            )

            cur.execute(
                """
                UPDATE customer_showcase_visitors
                SET last_scan_at = NOW()
                WHERE id = %s
                """,
                (
                    membership["visitor_id"],
                ),
            )

        conn.commit()


def _pending_deliveries(
    limit=25,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.id,
                    d.showcase_id,
                    d.telegram_user_id,
                    d.property_id,
                    d.attempts,
                    s.token,
                    s.query_text,
                    s.campaign_name,
                    p.unit_code,
                    p.district,
                    p.address_text,
                    p.area_m2,
                    p.bedrooms,
                    p.build_year,
                    p.current_price_billion,
                    p.channel_post_url,
                    p.published_message_id
                FROM customer_campaign_deliveries d
                JOIN customer_showcases s
                  ON s.id = d.showcase_id
                JOIN customer_showcase_visitors v
                  ON v.showcase_id = d.showcase_id
                 AND v.telegram_user_id =
                    d.telegram_user_id
                JOIN properties p
                  ON p.id = d.property_id
                WHERE d.delivery_status = 'PENDING'
                  AND d.next_attempt_at <= NOW()
                  AND v.is_active = TRUE
                  AND s.is_active = TRUE
                  AND p.status = 'ACTIVE'
                  AND p.published_message_id
                    IS NOT NULL
                  AND p.deleted_from_channel_at
                    IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM property_trash pt
                      WHERE pt.property_id = p.id
                  )
                ORDER BY d.id
                LIMIT %s
                """,
                (limit,),
            )

            return cur.fetchall()


def _mark_delivery_sent(
    delivery_id,
    message_id,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_campaign_deliveries
                SET
                    delivery_status = 'SENT',
                    telegram_message_id = %s,
                    sent_at = NOW(),
                    attempts = attempts + 1,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    message_id,
                    delivery_id,
                ),
            )

        conn.commit()


def _mark_delivery_failed(
    delivery_id,
    attempts,
    error_text,
):
    next_attempts = int(
        attempts
        or 0
    ) + 1

    final_status = (
        "FAILED"
        if next_attempts >= 8
        else "PENDING"
    )

    delay_minutes = min(
        60,
        2 ** min(
            next_attempts,
            5,
        ),
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customer_campaign_deliveries
                SET
                    delivery_status = %s,
                    attempts = %s,
                    next_attempt_at = (
                        NOW()
                        + (
                            %s
                            * INTERVAL '1 minute'
                        )
                    ),
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    final_status,
                    next_attempts,
                    delay_minutes,
                    str(error_text)[:1000],
                    delivery_id,
                ),
            )

        conn.commit()


def _new_match_text(
    row,
):
    (
        _delivery_id,
        _showcase_id,
        _telegram_user_id,
        _property_id,
        _attempts,
        _token,
        query_text,
        campaign_name,
        unit_code,
        district,
        address,
        area,
        bedrooms,
        build_year,
        price,
        url,
        message_id,
    ) = row

    lines = [
        "🆕 فایل جدید مطابق درخواست شما",
        "",
        (
            "🔎 دسته: "
            f"{campaign_name or query_text}"
        ),
        (
            "📍 "
            f"{address or district or 'بدون منطقه'}"
        ),
    ]

    details = []

    if area is not None:
        details.append(
            f"📐 {_fmt(area)} متر"
        )

    if bedrooms is not None:
        details.append(
            f"🛏 {bedrooms} خواب"
        )

    if build_year:
        details.append(
            f"📅 سال ساخت {build_year}"
        )

    if details:
        lines.append(
            " | ".join(details)
        )

    if price is not None:
        lines.append(
            "💰 قیمت: "
            f"{_fmt(price)} میلیارد تومان"
        )

    lines.append(
        f"🔢 کد واحد: {unit_code or '-'}"
    )

    final_url = (
        url
        or (
            f"https://t.me/nimatmelk/"
            f"{message_id}"
            if message_id
            else None
        )
    )

    if final_url:
        lines.append(
            f"🔗 مشاهده فایل: {final_url}"
        )

    lines.extend(
        [
            "",
            (
                "برای گرفتن وقت بازدید، کلمه «بازدید» "
                "را همراه کد فایل ارسال کنید."
            ),
        ]
    )

    return "\n".join(
        lines
    )


async def _delivery_keyboard(
    bot,
    token,
    visitor_id,
):
    link = await build_showcase_link(
        bot,
        token,
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📂 مشاهده فهرست زنده",
                    url=link,
                )
            ],
            [
                InlineKeyboardButton(
                    "🔕 توقف این دسته",
                    callback_data=(
                        f"cs_unsub_{visitor_id}"
                    ),
                )
            ],
        ]
    )


def _catalogs_to_refresh(
    limit=25,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.id,
                    v.telegram_user_id,
                    v.last_catalog_message_id,
                    v.last_catalog_offset,
                    v.last_catalog_hash,
                    s.id,
                    s.token,
                    s.query_text,
                    s.filters,
                    s.expires_at,
                    s.created_by_user_id,
                    s.campaign_name,
                    s.notify_immediately
                FROM customer_showcase_visitors v
                JOIN customer_showcases s
                  ON s.id = v.showcase_id
                WHERE v.is_active = TRUE
                  AND s.is_active = TRUE
                  AND v.last_catalog_message_id
                    IS NOT NULL
                  AND v.last_opened_at
                    >= NOW() - INTERVAL '30 days'
                  AND (
                      v.last_catalog_synced_at IS NULL
                      OR v.last_catalog_synced_at
                        <= NOW() - INTERVAL '5 minutes'
                  )
                ORDER BY
                    v.last_catalog_synced_at NULLS FIRST,
                    v.id
                LIMIT %s
                """,
                (limit,),
            )

            return cur.fetchall()


def _touch_catalog_sync(
    visitor_id,
    *,
    text_hash=None,
    offset=None,
    clear_message=False,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            if clear_message:
                cur.execute(
                    """
                    UPDATE customer_showcase_visitors
                    SET
                        last_catalog_message_id = NULL,
                        last_catalog_synced_at = NOW()
                    WHERE id = %s
                    """,
                    (visitor_id,),
                )

            else:
                cur.execute(
                    """
                    UPDATE customer_showcase_visitors
                    SET
                        last_catalog_hash = COALESCE(
                            %s,
                            last_catalog_hash
                        ),
                        last_catalog_offset = COALESCE(
                            %s,
                            last_catalog_offset
                        ),
                        last_catalog_synced_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        text_hash,
                        offset,
                        visitor_id,
                    ),
                )

        conn.commit()


async def _refresh_live_catalogs(
    application,
):
    for row in _catalogs_to_refresh():
        (
            visitor_id,
            telegram_user_id,
            message_id,
            offset,
            previous_hash,
            showcase_id,
            token,
            query_text,
            filters,
            expires_at,
            created_by_user_id,
            campaign_name,
            notify_immediately,
        ) = row

        showcase = {
            "id": int(showcase_id),
            "token": token,
            "query_text": query_text,
            "filters": dict(
                filters
                or {}
            ),
            "expires_at": expires_at,
            "created_by_user_id": created_by_user_id,
            "campaign_name": (
                campaign_name
                or query_text
            ),
            "notify_immediately": bool(
                notify_immediately
            ),
        }

        text, keyboard, final_offset = (
            render_showcase(
                showcase,
                int(visitor_id),
                int(offset or 0),
            )
        )

        current_hash = _render_hash(
            text
        )

        if current_hash == previous_hash:
            _touch_catalog_sync(
                visitor_id
            )
            continue

        try:
            await application.bot.edit_message_text(
                chat_id=telegram_user_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            _touch_catalog_sync(
                visitor_id,
                text_hash=current_hash,
                offset=final_offset,
            )

        except Exception as exc:
            error_text = str(
                exc
            ).lower()

            if (
                "message is not modified"
                in error_text
            ):
                _touch_catalog_sync(
                    visitor_id,
                    text_hash=current_hash,
                    offset=final_offset,
                )

            elif any(
                value in error_text
                for value in (
                    "message to edit not found",
                    "message can't be edited",
                    "chat not found",
                    "bot was blocked",
                )
            ):
                _touch_catalog_sync(
                    visitor_id,
                    clear_message=True,
                )

            else:
                logging.exception(
                    (
                        "Campaign catalog refresh failed: "
                        "visitor=%s"
                    ),
                    visitor_id,
                )

                _touch_catalog_sync(
                    visitor_id
                )

        await asyncio.sleep(
            0.08
        )


async def run_campaign_watch_once(
    application,
):
    memberships = _active_memberships()

    for membership in memberships:
        try:
            _queue_new_matches(
                membership
            )

        except Exception:
            logging.exception(
                (
                    "Campaign match queue failed: "
                    "visitor=%s showcase=%s"
                ),
                membership["visitor_id"],
                membership["showcase_id"],
            )

    visitor_ids = {
        (
            membership["showcase_id"],
            membership["telegram_user_id"],
        ): membership["visitor_id"]
        for membership in memberships
    }

    for row in _pending_deliveries():
        delivery_id = int(
            row[0]
        )

        visitor_id = visitor_ids.get(
            (
                int(row[1]),
                int(row[2]),
            )
        )

        if visitor_id is None:
            visitor = _get_visitor(
                int(row[1]),
                int(row[2]),
            )

            visitor_id = (
                int(visitor[0])
                if visitor
                else None
            )

        if visitor_id is None:
            _mark_delivery_failed(
                delivery_id,
                row[4],
                "campaign membership missing",
            )
            continue

        try:
            sent = await application.bot.send_message(
                chat_id=int(row[2]),
                text=_new_match_text(
                    row
                ),
                reply_markup=(
                    await _delivery_keyboard(
                        application.bot,
                        row[5],
                        visitor_id,
                    )
                ),
                disable_web_page_preview=True,
            )

            _mark_delivery_sent(
                delivery_id,
                sent.message_id,
            )

        except Exception as exc:
            logging.exception(
                (
                    "Campaign notification failed: "
                    "delivery=%s user=%s property=%s"
                ),
                delivery_id,
                row[2],
                row[3],
            )

            _mark_delivery_failed(
                delivery_id,
                row[4],
                exc,
            )

        await asyncio.sleep(
            0.08
        )

    await _refresh_live_catalogs(
        application
    )


async def _campaign_watch_loop(
    application,
):
    logging.info(
        (
            "Customer lead campaign worker started: "
            "interval=%ss"
        ),
        CAMPAIGN_WATCH_INTERVAL_SECONDS,
    )

    while True:
        try:
            await run_campaign_watch_once(
                application
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logging.exception(
                "Customer lead campaign worker error"
            )

        await asyncio.sleep(
            CAMPAIGN_WATCH_INTERVAL_SECONDS
        )


def start_campaign_watch_worker(
    application,
):
    existing = application.bot_data.get(
        "customer_campaign_watch_task"
    )

    if (
        existing
        and not existing.done()
    ):
        return existing

    task = asyncio.create_task(
        _campaign_watch_loop(
            application
        ),
        name="customer_campaign_watch",
    )

    application.bot_data[
        "customer_campaign_watch_task"
    ] = task

    return task


def campaign_summary_rows(
    created_by_user_id=None,
    limit=15,
):
    params = []
    owner_clause = ""

    if created_by_user_id is not None:
        owner_clause = (
            "AND s.created_by_user_id = %s"
        )
        params.append(
            created_by_user_id
        )

    params.append(
        limit
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    s.id,
                    s.token,
                    s.campaign_name,
                    s.query_text,
                    s.is_active,
                    COUNT(DISTINCT v.id) FILTER (
                        WHERE v.is_active = TRUE
                    ) AS active_customers,
                    COUNT(DISTINCT v.id) AS total_customers,
                    COUNT(d.id) FILTER (
                        WHERE d.delivery_status = 'SENT'
                    ) AS sent_files
                FROM customer_showcases s
                LEFT JOIN customer_showcase_visitors v
                  ON v.showcase_id = s.id
                LEFT JOIN customer_campaign_deliveries d
                  ON d.showcase_id = s.id
                 AND d.telegram_user_id =
                    v.telegram_user_id
                WHERE s.expires_at > NOW()
                  {owner_clause}
                GROUP BY
                    s.id,
                    s.token,
                    s.campaign_name,
                    s.query_text,
                    s.is_active
                ORDER BY s.id DESC
                LIMIT %s
                """,
                params,
            )

            return cur.fetchall()


def campaign_summary_text(
    created_by_user_id=None,
):
    rows = campaign_summary_rows(
        created_by_user_id
    )

    lines = [
        "👥 کمپین‌های جذب مشتری",
        "",
    ]

    if not rows:
        lines.extend(
            [
                "هنوز کمپینی ساخته نشده است.",
                (
                    "ابتدا جستجوی هوشمند را انجام دهید و "
                    "«ساخت لینک جذب مشتری» را بزنید."
                ),
            ]
        )

        return "\n".join(
            lines
        )

    for (
        _showcase_id,
        _token,
        campaign_name,
        query_text,
        is_active,
        active_customers,
        total_customers,
        sent_files,
    ) in rows:
        lines.extend(
            [
                (
                    ("✅" if is_active else "⛔")
                    + " "
                    + str(
                        campaign_name
                        or query_text
                    )
                ),
                (
                    "👤 مشتری فعال: "
                    f"{int(active_customers or 0)}"
                ),
                (
                    "📊 کل ورودی: "
                    f"{int(total_customers or 0)}"
                ),
                (
                    "🚀 فایل جدید ارسال‌شده: "
                    f"{int(sent_files or 0)}"
                ),
                "",
                "──────────────",
                "",
            ]
        )

    return "\n".join(
        lines
    ).strip()
