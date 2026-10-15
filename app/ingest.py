import os
import psycopg

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")


def db_connect():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def get_input_channel_id():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telegram_channel_id
                FROM channels
                WHERE channel_type = 'INPUT'
                  AND is_active = TRUE
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            return row[0] if row else None


def raw_property_exists(channel_id, message_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM properties
                WHERE input_channel_id = %s
                  AND input_message_id = %s
                LIMIT 1
                """,
                (channel_id, message_id),
            )
            row = cur.fetchone()
            return row[0] if row else None


def get_property_by_media_group(channel_id, media_group_id):
    if not media_group_id:
        return None

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM properties
                WHERE input_channel_id = %s
                  AND input_media_group_id = %s
                ORDER BY id ASC
                LIMIT 1
                """,
                (channel_id, str(media_group_id)),
            )
            row = cur.fetchone()
            return row[0] if row else None


def create_raw_property(
    channel_id,
    message_id,
    media_group_id,
    raw_text,
    has_media,
    source_kind="INPUT",
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties
                (
                    input_channel_id,
                    input_message_id,
                    input_media_group_id,
                    raw_text,
                    has_media,
                    ingest_status,
                    source_kind,
                    status
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    'RAW',
                    %s,
                    'ACTIVE'
                )
                RETURNING id
                """,
                (
                    channel_id,
                    message_id,
                    str(media_group_id) if media_group_id else None,
                    raw_text,
                    has_media,
                    source_kind,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0]


def update_raw_text(property_id, raw_text):
    if not raw_text:
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET raw_text = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND (raw_text IS NULL OR raw_text = '')
                """,
                (raw_text, property_id),
            )
            conn.commit()


def get_property_raw_text(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_text FROM properties WHERE id = %s",
                (property_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def delete_property_media(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM property_media WHERE property_id = %s",
                (property_id,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def media_exists(property_id, file_unique_id):
    if not file_unique_id:
        return False

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM property_media
                WHERE property_id = %s
                  AND telegram_file_unique_id = %s
                LIMIT 1
                """,
                (
                    property_id,
                    file_unique_id,
                ),
            )

            return cur.fetchone() is not None


def add_media(
    property_id,
    file_id,
    file_unique_id,
    media_type,
    media_group_id,
    position=0,
):
    if media_exists(
        property_id,
        file_unique_id,
    ):
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO property_media
                (
                    property_id,
                    telegram_file_id,
                    telegram_file_unique_id,
                    media_type,
                    media_group_id,
                    position
                )
                VALUES
                (%s, %s, %s, %s, %s, %s)
                """,
                (
                    property_id,
                    file_id,
                    file_unique_id,
                    media_type,
                    str(media_group_id) if media_group_id else None,
                    position,
                ),
            )
            conn.commit()


def queue_exists(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM publish_queue
                WHERE property_id = %s
                LIMIT 1
                """,
                (property_id,),
            )
            return cur.fetchone() is not None


def add_to_queue(property_id):
    if queue_exists(property_id):
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO publish_queue
                (
                    property_id,
                    queue_status,
                    created_at,
                    updated_at
                )
                VALUES
                (%s, 'READY', NOW(), NOW())
                """,
                (property_id,),
            )
            conn.commit()


def set_ingest_classification(property_id, classification):
    status_map = {
        "PROPERTY": "ACTIVE",
        "NON_PROPERTY": "INACTIVE",
        "INVALID_PROPERTY": "INVALID",
    }

    property_status = status_map.get(
        classification,
        "INVALID",
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET ingest_status = %s,
                    status = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    classification,
                    property_status,
                    property_id,
                ),
            )
            conn.commit()


# === PRICE PRIORITY POLICY V1 ===

def _price_hashtag_from_toman(price_toman):
    if price_toman is None:
        return None

    billion = float(price_toman) / 1_000_000_000

    try:
        from property_parser import make_price_hashtag
        return make_price_hashtag(billion)
    except Exception:
        lower = int(billion)
        return f"#آپارتمان_{lower}_{lower + 1}"


def _resolve_price_before_parse_update(
    cur,
    property_id,
    incoming_price_toman,
):
    """
    Operator price acts as a persistent floor.

    Example:
        source 40 -> operator 43 => 43
        source 42 -> operator 43 => 43
        source 45 -> operator 43 => 45

    A manual edit is inferred when properties.current_price_toman
    differs from the previous effective source/manual price.
    """

    cur.execute(
        """
        SELECT
            current_price_toman,
            manual_price_toman
        FROM properties
        WHERE id = %s
        FOR UPDATE
        """,
        (property_id,),
    )

    row = cur.fetchone()

    if not row:
        return incoming_price_toman

    current_price, manual_price = row

    cur.execute(
        """
        SELECT MAX(source_price_toman)
        FROM property_sources
        WHERE property_id = %s
          AND is_active = TRUE
          AND source_price_toman IS NOT NULL
        """,
        (property_id,),
    )

    source_row = cur.fetchone()
    previous_source_price = (
        source_row[0]
        if source_row
        else None
    )

    # Detect an operator/manual price change that happened
    # outside the source parser.
    previous_candidates = [
        int(value)
        for value in (
            previous_source_price,
            manual_price,
        )
        if value is not None
    ]

    if (
        current_price is not None
        and previous_candidates
    ):
        expected_previous = max(
            previous_candidates
        )

        if int(current_price) != int(expected_previous):
            manual_price = int(current_price)

            cur.execute(
                """
                UPDATE properties
                SET
                    manual_price_toman = %s,
                    manual_price_updated_at = NOW()
                WHERE id = %s
                """,
                (
                    manual_price,
                    property_id,
                ),
            )

    candidates = [
        int(value)
        for value in (
            incoming_price_toman,
            previous_source_price,
            manual_price,
        )
        if value is not None
    ]

    return max(candidates) if candidates else None


def reconcile_property_price(property_id):
    """
    Final authoritative price after a source row has been saved:
        MAX(operator price, active source prices)
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    current_price_toman,
                    manual_price_toman
                FROM properties
                WHERE id = %s
                FOR UPDATE
                """,
                (property_id,),
            )

            row = cur.fetchone()

            if not row:
                return None

            current_price, manual_price = row

            cur.execute(
                """
                SELECT MAX(source_price_toman)
                FROM property_sources
                WHERE property_id = %s
                  AND is_active = TRUE
                  AND source_price_toman IS NOT NULL
                """,
                (property_id,),
            )

            source_row = cur.fetchone()
            source_price = (
                source_row[0]
                if source_row
                else None
            )

            candidates = [
                int(value)
                for value in (
                    manual_price,
                    source_price,
                )
                if value is not None
            ]

            if not candidates:
                return current_price

            effective_price = max(
                candidates
            )

            effective_billion = (
                effective_price
                / 1_000_000_000
            )

            price_hashtag = (
                _price_hashtag_from_toman(
                    effective_price
                )
            )

            cur.execute(
                """
                UPDATE properties
                SET
                    current_price_toman = %s,
                    current_price_billion = %s,
                    price_hashtag = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    effective_price,
                    effective_billion,
                    price_hashtag,
                    property_id,
                ),
            )

            conn.commit()

            return effective_price


def update_parsed_property(property_id, parsed):
    """
    Normal parser update with operator/source price priority protection.
    """

    parsed = dict(parsed or {})

    incoming_price = parsed.get(
        "current_price_toman"
    )

    with db_connect() as conn:
        with conn.cursor() as cur:

            effective_price = incoming_price

            if incoming_price is not None:
                effective_price = (
                    _resolve_price_before_parse_update(
                        cur,
                        property_id,
                        incoming_price,
                    )
                )

            if effective_price is not None:
                effective_billion = (
                    int(effective_price)
                    / 1_000_000_000
                )

                effective_hashtag = (
                    _price_hashtag_from_toman(
                        effective_price
                    )
                )
            else:
                effective_billion = parsed.get(
                    "current_price_billion"
                )

                effective_hashtag = parsed.get(
                    "price_hashtag"
                )

            cur.execute(
                """
                UPDATE properties
                SET
                    area_m2 = %s,
                    bedrooms = %s,
                    floor_text = %s,
                    build_year = %s,
                    district = %s,
                    has_parking = %s,
                    has_elevator = %s,
                    has_storage = %s,
                    direction = %s,
                    current_price_billion = %s,
                    current_price_toman = %s,
                    price_hashtag = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    parsed.get("area_m2"),
                    parsed.get("bedrooms"),
                    parsed.get("floor_text"),
                    parsed.get("build_year"),
                    parsed.get("district"),
                    parsed.get("has_parking"),
                    parsed.get("has_elevator"),
                    parsed.get("has_storage"),
                    parsed.get("direction"),
                    effective_billion,
                    effective_price,
                    effective_hashtag,
                    property_id,
                ),
            )

            conn.commit()


def assign_unit_code_on_publish(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT unit_code, current_price_billion
                FROM properties
                WHERE id = %s
                FOR UPDATE
                """,
                (property_id,),
            )

            row = cur.fetchone()

            if not row:
                raise ValueError(
                    f"Property {property_id} not found"
                )

            existing_unit_code = row[0]
            price_billion = row[1]

            if existing_unit_code:
                conn.commit()
                return existing_unit_code

            if price_billion is None:
                raise ValueError(
                    f"Property {property_id} has no price"
                )

            cur.execute(
                """
                SELECT setting_value
                FROM app_settings
                WHERE setting_key = 'unit_sequence'
                FOR UPDATE
                """
            )

            setting_row = cur.fetchone()

            if not setting_row:
                raise ValueError(
                    "unit_sequence setting not found"
                )

            setting_value = setting_row[0]

            if isinstance(setting_value, dict):
                current_sequence = int(
                    setting_value.get("current", 99)
                )
            else:
                raise ValueError(
                    "Invalid unit_sequence setting"
                )

            next_sequence = current_sequence + 1
            integer_price = int(price_billion)

            unit_code = f"{next_sequence}{integer_price}"

            cur.execute(
                """
                UPDATE properties
                SET
                    internal_sequence = %s,
                    unit_code = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    next_sequence,
                    unit_code,
                    property_id,
                ),
            )

            cur.execute(
                """
                UPDATE app_settings
                SET
                    setting_value =
                        jsonb_build_object(
                            'current',
                            %s
                        ),
                    updated_at = NOW()
                WHERE setting_key =
                    'unit_sequence'
                """,
                (next_sequence,),
            )

            conn.commit()

            return unit_code


def save_property_attributes(property_id, attributes):
    if not attributes:
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            for item in attributes:
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
                        attribute_label = EXCLUDED.attribute_label,
                        attribute_value = EXCLUDED.attribute_value,
                        display_order = EXCLUDED.display_order,
                        is_visible = TRUE,
                        updated_at = NOW()
                    """,
                    (
                        property_id,
                        item["attribute_key"],
                        item["attribute_label"],
                        item["attribute_value"],
                        item["display_order"],
                    ),
                )

            conn.commit()


def get_active_attribute_rules():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    attribute_key,
                    attribute_label,
                    match_type,
                    trigger_text,
                    default_value,
                    display_order
                FROM property_attribute_rules
                WHERE is_active = TRUE
                ORDER BY display_order, id
                """
            )

            rows = cur.fetchall()

    return [
        {
            "attribute_key": row[0],
            "attribute_label": row[1],
            "match_type": row[2],
            "trigger_text": row[3],
            "default_value": row[4],
            "display_order": row[5],
        }
        for row in rows
    ]


def get_channel_db_id(telegram_channel_id):
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

            row = cur.fetchone()

            return row[0] if row else None


def live_property_for_source_post(
    telegram_channel_id,
    source_message_id,
):
    """The live listing that already holds this source post, if any."""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id
                FROM properties p
                JOIN channels c
                  ON c.telegram_channel_id = %s
                WHERE p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND (
                        EXISTS (
                            SELECT 1
                            FROM property_sources ps
                            WHERE ps.property_id = p.id
                              AND ps.source_channel_id = c.id
                              AND ps.source_message_id = %s
                        )
                        OR (
                            p.input_channel_id = c.telegram_channel_id
                            AND p.input_message_id = %s
                        )
                  )
                ORDER BY p.id
                LIMIT 1
                """,
                (
                    telegram_channel_id,
                    source_message_id,
                    source_message_id,
                ),
            )
            row = cur.fetchone()

    return row[0] if row else None


def _save_property_source_raw(
    property_id,
    telegram_channel_id,
    source_message_id,
    source_unit_code,
    source_raw_text,
    source_price_toman=None,
    source_post_url=None,
):
    channel_db_id = get_channel_db_id(
        telegram_channel_id
    )

    with db_connect() as conn:
        with conn.cursor() as cur:
            if source_unit_code:
                # Only this listing's own row may follow the code to a new
                # message. Sources reuse codes for other units, and taking
                # another listing's row left that listing untracked and let
                # later edits of the new post rewrite the wrong listing.
                cur.execute(
                    """
                    UPDATE property_sources
                    SET
                        property_id = %s,
                        source_message_id = %s,
                        source_price_toman = %s,
                        source_raw_text = %s,
                        source_post_url = %s,
                        is_primary = TRUE,
                        is_active = TRUE,
                        last_synced_at = NOW(),
                        updated_at = NOW()
                    WHERE id = (
                        SELECT id
                        FROM property_sources
                        WHERE source_channel_id = %s
                          AND source_unit_code = %s
                          AND property_id = %s
                        ORDER BY
                            is_active DESC,
                            is_primary DESC,
                            id DESC
                        LIMIT 1
                    )
                    RETURNING id
                    """,
                    (
                        property_id,
                        source_message_id,
                        source_price_toman,
                        source_raw_text,
                        source_post_url,
                        channel_db_id,
                        source_unit_code,
                        property_id,
                    ),
                )

                existing = cur.fetchone()

                if existing:
                    cur.execute(
                        """
                        UPDATE property_sources
                        SET
                            is_primary = FALSE,
                            is_active = FALSE,
                            updated_at = NOW()
                        WHERE property_id = %s
                          AND source_channel_id = %s
                          AND source_unit_code = %s
                          AND id <> %s
                        """,
                        (
                            property_id,
                            channel_db_id,
                            source_unit_code,
                            existing[0],
                        ),
                    )

                    conn.commit()
                    return existing[0]

            cur.execute(
                """
                INSERT INTO property_sources (
                    property_id,
                    source_channel_id,
                    source_message_id,
                    source_unit_code,
                    source_price_toman,
                    source_raw_text,
                    source_post_url,
                    is_primary,
                    is_active,
                    last_synced_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    TRUE,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (
                    source_channel_id,
                    source_message_id
                )
                DO UPDATE SET
                    -- A post forwarded again (e.g. into the input channel)
                    -- must not take the row from the live listing that
                    -- already tracks it; that left the listing untracked.
                    -- An unpublished copy never keeps it from the
                    -- published listing.
                    property_id = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM properties owner
                            WHERE owner.id = property_sources.property_id
                              AND owner.id <> EXCLUDED.property_id
                              AND owner.status = 'ACTIVE'
                              AND owner.deleted_from_channel_at IS NULL
                              AND (
                                    owner.published_message_id IS NOT NULL
                                    OR NOT EXISTS (
                                        SELECT 1
                                        FROM properties incoming
                                        WHERE incoming.id = EXCLUDED.property_id
                                          AND incoming.published_message_id
                                              IS NOT NULL
                                    )
                              )
                        )
                        THEN property_sources.property_id
                        ELSE EXCLUDED.property_id
                    END,
                    source_unit_code = EXCLUDED.source_unit_code,
                    source_price_toman = EXCLUDED.source_price_toman,
                    source_raw_text = EXCLUDED.source_raw_text,
                    source_post_url = EXCLUDED.source_post_url,
                    is_active = TRUE,
                    last_synced_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    property_id,
                    channel_db_id,
                    source_message_id,
                    source_unit_code,
                    source_price_toman,
                    source_raw_text,
                    source_post_url,
                ),
            )

            conn.commit()



def save_property_address(property_id, address_text):
    if not address_text:
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    address_text = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    address_text,
                    property_id,
                ),
            )

            conn.commit()



# === SAVE SOURCE PRICE RECONCILE WRAPPER V1 ===
def save_property_source(
    property_id,
    telegram_channel_id,
    source_message_id,
    source_unit_code,
    source_raw_text,
    source_price_toman=None,
    source_post_url=None,
):
    result = _save_property_source_raw(
        property_id=property_id,
        telegram_channel_id=telegram_channel_id,
        source_message_id=source_message_id,
        source_unit_code=source_unit_code,
        source_raw_text=source_raw_text,
        source_price_toman=source_price_toman,
        source_post_url=source_post_url,
    )

    # Source row now contains the fresh real source price.
    reconcile_property_price(
        property_id
    )

    return result
