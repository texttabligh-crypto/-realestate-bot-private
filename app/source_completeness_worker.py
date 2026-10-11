import asyncio
import os
import re
import sys
import time

import bot

from telegram import Bot as TelegramBot
from telegram.error import RetryAfter, TimedOut, NetworkError

from ingest import get_active_attribute_rules
from property_parser import (
    parse_property,
    parse_dynamic_attributes,
    parse_rule_attributes,
)


# ============================================================
# SOURCE COMPLETENESS HISTORICAL WORKER V1
# ============================================================

REFRESH_DELAY = 20
NO_CHANGE_DELAY = 0.4

SAFE_ADDRESS_FIX_IDS = {
    123,
}

BOILERPLATE_PATTERNS = (
    r"فایل‌های با موقعیت خوب و ویو ابدی",
    r"گزینه‌ای مناسب برای افرادی که دنبال",
    r"همیشه جزو گزینه‌های قابل توجه بازار",
)

DETAIL_MARKERS = (
    "موقعیت",
    "جنوبی",
    "شمالی",
    "شرقی",
    "غربی",
    "دوکله",
    "دو کله",
    "دونبش",
    "دو نبش",
    "نبش",
    "نما",
    "تراس",
    "بالکن",
    "کابینت",
    "کمد",
    "کلوزت",
    "نصبیات",
    "خواب",
    "مستر",
    "متر",
    "طبقه",
    "ساخت",
    "رضایت",
    "عرض",
    "آشپزخانه",
    "اشپزخانه",
    "ویو باز",
    "نورگیری",
    "پیش فروش",
    "پیش‌فروش",
    "صفر کلید",
    "کلید نخورده",
    "کلید نخوره",
    "روف",
    "حیاط",
    "بر اصلی",
)


def clean(value):
    if value is None:
        return ""

    text = str(value).strip()

    text = (
        text.replace("ي", "ی")
        .replace("ك", "ک")
        .replace("ۀ", "ه")
        .replace("،", ",")
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text


def is_boilerplate(value):
    text = clean(value)

    return any(
        re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )
        for pattern in BOILERPLATE_PATTERNS
    )


def keep_source_detail(value):
    text = clean(value)

    if not text:
        return False

    if is_boilerplate(text):
        return False

    # Numeric property information is usually meaningful:
    # 145 متر, 3 خواب, 70 درصد, ...
    if re.search(r"\d", text):
        return True

    return any(
        marker in text
        for marker in DETAIL_MARKERS
    )


def ensure_worker_state():
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS
                source_completeness_worker_state (
                    id SMALLINT PRIMARY KEY,
                    last_processed_property_id BIGINT
                        NOT NULL DEFAULT 0,
                    pending_refresh_property_id BIGINT,
                    completed BOOLEAN
                        NOT NULL DEFAULT FALSE,
                    processed_count INTEGER
                        NOT NULL DEFAULT 0,
                    changed_count INTEGER
                        NOT NULL DEFAULT 0,
                    refreshed_count INTEGER
                        NOT NULL DEFAULT 0,
                    skipped_count INTEGER
                        NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW(),
                    CHECK (id = 1)
                )
                """
            )

            cur.execute(
                """
                INSERT INTO
                    source_completeness_worker_state (
                        id
                    )
                VALUES (1)
                ON CONFLICT (id) DO NOTHING
                """
            )

            conn.commit()


def get_state():
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    last_processed_property_id,
                    pending_refresh_property_id,
                    completed,
                    processed_count,
                    changed_count,
                    refreshed_count,
                    skipped_count,
                    last_error
                FROM source_completeness_worker_state
                WHERE id = 1
                """
            )

            return cur.fetchone()


def set_error(text):
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE source_completeness_worker_state
                SET
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = 1
                """,
                (
                    str(text)[:2000],
                ),
            )

            conn.commit()


def next_candidate(last_id):
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.unit_code,
                    p.published_message_id,
                    p.channel_post_url,
                    p.address_text,
                    q.queue_status,
                    ps.source_unit_code,
                    ps.source_post_url,
                    ps.source_raw_text

                FROM properties p

                JOIN LATERAL (
                    SELECT
                        source_unit_code,
                        source_post_url,
                        source_raw_text
                    FROM property_sources
                    WHERE property_id = p.id
                      AND is_active = TRUE
                      AND source_raw_text IS NOT NULL
                      AND BTRIM(source_raw_text) <> ''
                    ORDER BY
                        is_primary DESC,
                        id DESC
                    LIMIT 1
                ) ps ON TRUE

                LEFT JOIN LATERAL (
                    SELECT queue_status
                    FROM publish_queue
                    WHERE property_id = p.id
                    ORDER BY
                        updated_at DESC,
                        id DESC
                    LIMIT 1
                ) q ON TRUE

                WHERE p.id > %s
                  AND p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND (
                        p.published_message_id IS NOT NULL
                        OR q.queue_status IN (
                            'READY',
                            'FAILED'
                        )
                      )

                ORDER BY p.id ASC
                LIMIT 1
                """,
                (
                    last_id,
                ),
            )

            return cur.fetchone()


def get_stored_attributes(property_id):
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    attribute_key,
                    attribute_label,
                    attribute_value
                FROM property_attributes
                WHERE property_id = %s
                  AND is_visible = TRUE
                """,
                (
                    property_id,
                ),
            )

            return {
                key: {
                    "label": label,
                    "value": value,
                }
                for key, label, value
                in cur.fetchall()
            }


def mark_skipped(property_id):
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE source_completeness_worker_state
                SET
                    last_processed_property_id = %s,
                    processed_count =
                        processed_count + 1,
                    skipped_count =
                        skipped_count + 1,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = 1
                """,
                (
                    property_id,
                ),
            )

            conn.commit()


def finish_processed(
    property_id,
    refreshed=False,
):
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE source_completeness_worker_state
                SET
                    last_processed_property_id =
                        GREATEST(
                            last_processed_property_id,
                            %s
                        ),
                    pending_refresh_property_id = NULL,
                    processed_count =
                        processed_count + 1,
                    refreshed_count =
                        refreshed_count + %s,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = 1
                """,
                (
                    property_id,
                    1 if refreshed else 0,
                ),
            )

            conn.commit()


def apply_changes_atomically(
    property_id,
    attr_changes,
    new_address,
    needs_refresh,
):
    """
    Attribute/address updates and the pending-refresh marker
    are committed together.

    If the process dies after this commit, the next run sees
    pending_refresh_property_id and refreshes the destination
    before advancing the checkpoint.
    """

    with bot.db_connect() as conn:
        with conn.cursor() as cur:

            for item in attr_changes:
                cur.execute(
                    """
                    INSERT INTO property_attributes (
                        property_id,
                        attribute_key,
                        attribute_label,
                        attribute_value,
                        display_order,
                        is_visible,
                        updated_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        TRUE,
                        NOW()
                    )
                    ON CONFLICT (
                        property_id,
                        attribute_key
                    )
                    DO UPDATE SET
                        attribute_label =
                            EXCLUDED.attribute_label,
                        attribute_value =
                            EXCLUDED.attribute_value,
                        display_order =
                            EXCLUDED.display_order,
                        is_visible = TRUE,
                        updated_at = NOW()
                    """,
                    (
                        property_id,
                        item["attribute_key"],
                        item.get(
                            "attribute_label"
                        )
                        or item["attribute_key"],
                        item.get(
                            "attribute_value"
                        ),
                        int(
                            item.get(
                                "display_order"
                            )
                            or 100
                        ),
                    ),
                )

            if new_address is not None:
                cur.execute(
                    """
                    UPDATE properties
                    SET
                        address_text = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        new_address,
                        property_id,
                    ),
                )

            if needs_refresh:
                cur.execute(
                    """
                    UPDATE
                        source_completeness_worker_state
                    SET
                        pending_refresh_property_id = %s,
                        changed_count =
                            changed_count + 1,
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE id = 1
                    """,
                    (
                        property_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE
                        source_completeness_worker_state
                    SET
                        last_processed_property_id = %s,
                        pending_refresh_property_id = NULL,
                        processed_count =
                            processed_count + 1,
                        changed_count =
                            changed_count + 1,
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE id = 1
                    """,
                    (
                        property_id,
                    ),
                )

            conn.commit()


def mark_completed():
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE source_completeness_worker_state
                SET
                    completed = TRUE,
                    pending_refresh_property_id = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = 1
                """
            )

            conn.commit()


def resolve_token():
    for name in (
        "BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TOKEN",
    ):
        value = (
            getattr(
                bot,
                name,
                None,
            )
            or os.getenv(name)
        )

        if value and ":" in str(value):
            return str(value)

    for key, value in os.environ.items():
        if (
            "TOKEN" in key.upper()
            and value
            and ":" in value
        ):
            return str(value)

    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN_NOT_FOUND"
    )


async def safe_refresh(
    telegram_bot,
    property_id,
):
    for attempt in range(1, 6):
        try:
            result = (
                await bot
                .refresh_published_property_post(
                    telegram_bot,
                    property_id,
                )
            )

            if result is True:
                return True

            raise RuntimeError(
                "refresh_published_property_post "
                "returned False"
            )

        except Exception as exc:
            text = str(exc).lower()

            # A retry after a crash may encounter an already
            # refreshed post. That is considered success.
            if "message is not modified" in text:
                return True

            if attempt >= 5:
                raise

            if isinstance(
                exc,
                RetryAfter,
            ):
                wait = getattr(
                    exc,
                    "retry_after",
                    10,
                )

                if hasattr(
                    wait,
                    "total_seconds",
                ):
                    wait = (
                        wait.total_seconds()
                    )

                wait = float(wait) + 5

            elif isinstance(
                exc,
                (
                    TimedOut,
                    NetworkError,
                ),
            ):
                wait = 5 * attempt

            else:
                wait = 5 * attempt

            print(
                "REFRESH_RETRY",
                f"property_id={property_id}",
                f"attempt={attempt}",
                f"wait={wait}",
                f"error={exc}",
                flush=True,
            )

            await asyncio.sleep(wait)

    return False


async def resolve_pending(
    telegram_bot,
    property_id,
):
    with bot.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    published_message_id,
                    deleted_from_channel_at
                FROM properties
                WHERE id = %s
                """,
                (
                    property_id,
                ),
            )

            row = cur.fetchone()

    if (
        not row
        or not row[0]
        or row[1] is not None
    ):
        print(
            "PENDING_REFRESH_NO_LONGER_ELIGIBLE",
            property_id,
            flush=True,
        )

        finish_processed(
            property_id,
            refreshed=False,
        )

        return

    print(
        "RESUMING_PENDING_REFRESH",
        property_id,
        flush=True,
    )

    await safe_refresh(
        telegram_bot,
        property_id,
    )

    finish_processed(
        property_id,
        refreshed=True,
    )

    print(
        "PENDING_REFRESH_OK",
        property_id,
        flush=True,
    )

    await asyncio.sleep(
        REFRESH_DELAY
    )


async def process_candidate(
    telegram_bot,
    row,
    rules,
):
    (
        property_id,
        unit_code,
        published_message_id,
        channel_post_url,
        current_address,
        queue_status,
        source_unit_code,
        source_post_url,
        source_raw_text,
    ) = row

    parsed = parse_property(
        source_raw_text
    )

    generated = {}

    for item in (
        parse_dynamic_attributes(
            source_raw_text
        )
        +
        parse_rule_attributes(
            source_raw_text,
            rules,
        )
    ):
        key = item.get(
            "attribute_key"
        )

        value = item.get(
            "attribute_value"
        )

        if not key:
            continue

        if not clean(value):
            continue

        if str(key).startswith(
            "source_detail_"
        ):
            if not keep_source_detail(
                value
            ):
                continue

        elif is_boilerplate(
            value
        ):
            continue

        generated[key] = item

    stored = get_stored_attributes(
        property_id
    )

    attr_changes = []

    for key, item in generated.items():
        old_value = (
            stored.get(
                key,
                {},
            ).get(
                "value"
            )
        )

        new_value = item.get(
            "attribute_value"
        )

        if clean(
            old_value
        ) == clean(
            new_value
        ):
            continue

        attr_changes.append(
            item
        )

    new_address = None

    # Precision audit found exactly one actionable Core
    # difference. Do not automatically overwrite any other
    # manually curated Core field.
    if property_id in SAFE_ADDRESS_FIX_IDS:
        parsed_address = clean(
            parsed.get(
                "address_text"
            )
        )

        if (
            parsed_address
            and "دفتر" not in parsed_address
            and "املاک" not in parsed_address
            and clean(
                current_address
            ) != parsed_address
        ):
            new_address = (
                parsed.get(
                    "address_text"
                )
            )

    if (
        not attr_changes
        and new_address is None
    ):
        mark_skipped(
            property_id
        )

        print(
            "NO_ACTION",
            f"id={property_id}",
            f"unit={unit_code or '-'}",
            flush=True,
        )

        await asyncio.sleep(
            NO_CHANGE_DELAY
        )

        return

    before_text = None

    if published_message_id:
        before_row = (
            bot.get_property_for_publish(
                property_id
            )
        )

        if before_row:
            before_text = (
                bot.build_publish_text(
                    before_row
                )
            )

    # Published rows get a persistent pending marker in
    # the same transaction as the database changes.
    apply_changes_atomically(
        property_id,
        attr_changes,
        new_address,
        needs_refresh=bool(
            published_message_id
        ),
    )

    print(
        "DATABASE_UPDATED",
        f"id={property_id}",
        f"unit={unit_code or '-'}",
        f"attrs={len(attr_changes)}",
        f"address={bool(new_address)}",
        f"state={'PUBLISHED' if published_message_id else queue_status}",
        flush=True,
    )

    if not published_message_id:
        await asyncio.sleep(
            NO_CHANGE_DELAY
        )

        return

    after_row = (
        bot.get_property_for_publish(
            property_id
        )
    )

    after_text = (
        bot.build_publish_text(
            after_row
        )
        if after_row
        else None
    )

    if (
        before_text is not None
        and after_text == before_text
    ):
        finish_processed(
            property_id,
            refreshed=False,
        )

        print(
            "NO_DESTINATION_TEXT_CHANGE",
            f"id={property_id}",
            flush=True,
        )

        await asyncio.sleep(
            NO_CHANGE_DELAY
        )

        return

    print(
        "REFRESH_START",
        f"id={property_id}",
        f"unit={unit_code or '-'}",
        f"url={channel_post_url or '-'}",
        flush=True,
    )

    await safe_refresh(
        telegram_bot,
        property_id,
    )

    finish_processed(
        property_id,
        refreshed=True,
    )

    print(
        "REFRESH_OK",
        f"id={property_id}",
        flush=True,
    )

    await asyncio.sleep(
        REFRESH_DELAY
    )


async def main():
    ensure_worker_state()

    state = get_state()

    if state[2]:
        print(
            "SOURCE_COMPLETENESS_ALREADY_COMPLETED",
            flush=True,
        )
        return

    token = resolve_token()

    rules = (
        get_active_attribute_rules()
    )

    async with TelegramBot(
        token=token
    ) as telegram_bot:

        state = get_state()

        pending = state[1]

        if pending:
            await resolve_pending(
                telegram_bot,
                pending,
            )

        while True:
            state = get_state()

            last_id = state[0]

            row = next_candidate(
                last_id
            )

            if not row:
                mark_completed()

                final = get_state()

                print(
                    "================================",
                    flush=True,
                )
                print(
                    "SOURCE_COMPLETENESS_COMPLETED",
                    flush=True,
                )
                print(
                    "LAST_ID:",
                    final[0],
                    flush=True,
                )
                print(
                    "PROCESSED:",
                    final[3],
                    flush=True,
                )
                print(
                    "CHANGED:",
                    final[4],
                    flush=True,
                )
                print(
                    "REFRESHED:",
                    final[5],
                    flush=True,
                )
                print(
                    "NO_ACTION:",
                    final[6],
                    flush=True,
                )
                print(
                    "================================",
                    flush=True,
                )

                return

            try:
                await process_candidate(
                    telegram_bot,
                    row,
                    rules,
                )

            except Exception as exc:
                set_error(
                    repr(exc)
                )

                print(
                    "WORKER_ERROR:",
                    repr(exc),
                    flush=True,
                )

                raise


if __name__ == "__main__":
    asyncio.run(
        main()
    )
