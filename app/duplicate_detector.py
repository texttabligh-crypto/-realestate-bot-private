from ingest import db_connect

DUPLICATE_THRESHOLD = 80


def normalize_text(value):
    if value is None:
        return None

    value = str(value).strip()

    return value or None


def normalize_floor(value):
    value = normalize_text(value)

    if not value:
        return None

    return " ".join(value.split())


def get_source_codes(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_unit_code
                FROM property_sources
                WHERE property_id = %s
                  AND source_unit_code IS NOT NULL
                  AND source_unit_code <> ''
                  AND is_active = TRUE
                """,
                (property_id,),
            )

            return {
                str(row[0]).strip()
                for row in cur.fetchall()
                if row[0]
            }


def get_property(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    district,
                    address_text,
                    area_m2,
                    bedrooms,
                    floor_text,
                    build_year,
                    current_price_toman,
                    unit_code,
                    published_message_id,
                    status
                FROM properties
                WHERE id = %s
                """,
                (property_id,),
            )

            return cur.fetchone()


def score_candidate(current, candidate):
    (
        current_id,
        current_district,
        current_address,
        current_area,
        current_bedrooms,
        current_floor,
        current_build_year,
        current_price,
        current_unit_code,
        current_message_id,
        current_status,
    ) = current

    (
        candidate_id,
        candidate_district,
        candidate_address,
        candidate_area,
        candidate_bedrooms,
        candidate_floor,
        candidate_build_year,
        candidate_price,
        candidate_unit_code,
        candidate_message_id,
        candidate_status,
    ) = candidate

    score = 0
    reasons = []

    current_codes = get_source_codes(current_id)
    candidate_codes = get_source_codes(candidate_id)

    common_codes = current_codes.intersection(candidate_codes)

    if common_codes:
        score = 100
        reasons.append(
            "کد فایل منبع یکسان: "
            + ", ".join(sorted(common_codes))
        )
        return score, reasons

    if (
        normalize_text(current_district)
        and normalize_text(candidate_district)
        and normalize_text(current_district)
        == normalize_text(candidate_district)
    ):
        score += 25
        reasons.append("منطقه یکسان")

    if (
        normalize_text(current_address)
        and normalize_text(candidate_address)
        and normalize_text(current_address)
        == normalize_text(candidate_address)
    ):
        score += 15
        reasons.append("آدرس یکسان")

    if current_area is not None and candidate_area is not None:
        diff = abs(float(current_area) - float(candidate_area))

        if diff == 0:
            score += 25
            reasons.append("متراژ یکسان")
        elif diff <= 2:
            score += 15
            reasons.append("متراژ بسیار نزدیک")

    if (
        current_bedrooms is not None
        and candidate_bedrooms is not None
        and current_bedrooms == candidate_bedrooms
    ):
        score += 10
        reasons.append("تعداد خواب یکسان")

    if (
        normalize_floor(current_floor)
        and normalize_floor(candidate_floor)
        and normalize_floor(current_floor)
        == normalize_floor(candidate_floor)
    ):
        score += 10
        reasons.append("طبقه یکسان")

    if (
        current_build_year is not None
        and candidate_build_year is not None
        and current_build_year == candidate_build_year
    ):
        score += 10
        reasons.append("سال ساخت یکسان")

    if (
        current_price
        and candidate_price
        and current_price > 0
        and candidate_price > 0
    ):
        maximum = max(
            float(current_price),
            float(candidate_price),
        )

        difference_percent = (
            abs(float(current_price) - float(candidate_price))
            / maximum
        ) * 100

        if difference_percent <= 2:
            score += 20
            reasons.append("قیمت تقریباً یکسان")
        elif difference_percent <= 5:
            score += 10
            reasons.append("قیمت نزدیک")

    return min(score, 100), reasons


def find_best_duplicate(property_id):
    current = get_property(property_id)

    if not current:
        return None

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    district,
                    address_text,
                    area_m2,
                    bedrooms,
                    floor_text,
                    build_year,
                    current_price_toman,
                    unit_code,
                    published_message_id,
                    status
                FROM properties
                WHERE id <> %s
                  AND status IN ('ACTIVE', 'SOLD')
                  -- === TRACEABLE DUPLICATE CANDIDATES V1 ===
                  AND (
                      published_message_id IS NOT NULL
                      OR NULLIF(BTRIM(channel_post_url), '') IS NOT NULL
                      OR EXISTS (
                          SELECT 1
                          FROM property_sources ps_trace
                          WHERE ps_trace.property_id = properties.id
                      )
                  )
                  AND created_at <= (
                      SELECT created_at
                      FROM properties
                      WHERE id = %s
                  )
                ORDER BY id DESC
                """,
                (
                    property_id,
                    property_id,
                ),
            )

            candidates = cur.fetchall()

    best = None

    for candidate in candidates:
        score, reasons = score_candidate(
            current,
            candidate,
        )

        if score < DUPLICATE_THRESHOLD:
            continue

        result = {
            "property_id": property_id,
            "duplicate_of_property_id": candidate[0],
            "similarity_score": score,
            "match_reason": "، ".join(reasons),
            "duplicate_unit_code": candidate[8],
            "duplicate_published_message_id": candidate[9],
        }

        if (
            best is None
            or result["similarity_score"]
            > best["similarity_score"]
        ):
            best = result

    return best


def get_duplicate_review(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    property_id,
                    duplicate_of_property_id,
                    similarity_score,
                    match_reason,
                    review_status
                FROM duplicate_reviews
                WHERE property_id = %s
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "property_id": row[1],
        "duplicate_of_property_id": row[2],
        "similarity_score": row[3],
        "match_reason": row[4],
        "review_status": row[5],
    }


def detect_and_store_duplicate(property_id):
    existing_review = get_duplicate_review(
        property_id
    )

    # === DUPLICATE REVIEW REVALIDATION V1 ===
    # APPROVED is a deliberate permanent decision.
    # REJECTED/PENDING must be checked again because
    # the old candidate may later become invalid.
    if (
        existing_review
        and existing_review["review_status"] == "APPROVED"
    ):
        return existing_review

    duplicate = find_best_duplicate(
        property_id
    )

    if not duplicate:
        # Remove stale duplicate decisions when the
        # previously matched property is no longer
        # a valid/traceable duplicate candidate.
        if (
            existing_review
            and existing_review["review_status"]
            != "APPROVED"
        ):
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM duplicate_reviews
                        WHERE property_id = %s
                          AND review_status <> 'APPROVED'
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
                          AND queue_status =
                              'PENDING_DUPLICATE'
                        """,
                        (property_id,),
                    )

                conn.commit()

        return None

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO duplicate_reviews (
                    property_id,
                    duplicate_of_property_id,
                    similarity_score,
                    match_reason,
                    review_status,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    'PENDING',
                    NOW()
                )
                ON CONFLICT (property_id)
                DO UPDATE SET
                    duplicate_of_property_id =
                        EXCLUDED.duplicate_of_property_id,
                    similarity_score =
                        EXCLUDED.similarity_score,
                    match_reason =
                        EXCLUDED.match_reason,
                    review_status = 'PENDING',
                    reviewed_at = NULL,
                    updated_at = NOW()
                """,
                (
                    property_id,
                    duplicate[
                        "duplicate_of_property_id"
                    ],
                    duplicate[
                        "similarity_score"
                    ],
                    duplicate[
                        "match_reason"
                    ],
                ),
            )

            conn.commit()

    duplicate["review_status"] = "PENDING"

    return duplicate


def set_duplicate_review_status(
    property_id,
    status,
):
    if status not in (
        "APPROVED",
        "REJECTED",
        "PENDING",
    ):
        raise ValueError(
            "Invalid duplicate review status"
        )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE duplicate_reviews
                SET
                    review_status = %s,
                    reviewed_at = CASE
                        WHEN %s IN (
                            'APPROVED',
                            'REJECTED'
                        )
                        THEN NOW()
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE property_id = %s
                RETURNING id
                """,
                (
                    status,
                    status,
                    property_id,
                ),
            )

            row = cur.fetchone()
            conn.commit()

    return bool(row)
