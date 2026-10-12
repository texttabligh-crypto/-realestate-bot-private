import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ingest import db_connect
from publisher import publish_property, republish_property
from duplicate_detector import detect_and_store_duplicate


TEHRAN = ZoneInfo("Asia/Tehran")
CHECK_INTERVAL_SECONDS = 20



def get_active_window(now=None):
    """
    Returns the active scheduling window for Tehran time.

    During pause_start -> pause_end,
    the window is treated as temporarily inactive.

    Overnight windows are supported.
    """

    now = now or datetime.now(TEHRAN)

    today = now.date()
    yesterday = today - timedelta(days=1)

    today_weekday = today.weekday()
    yesterday_weekday = yesterday.weekday()

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
                    pause_start,
                    pause_end
                FROM schedule_windows
                WHERE is_active = TRUE
                  AND weekday IN (%s, %s)
                ORDER BY start_time, id
                """,
                (
                    today_weekday,
                    yesterday_weekday,
                ),
            )

            rows = cur.fetchall()

    matches = []

    for (
        window_id,
        weekday,
        start_time,
        end_time,
        interval_minutes,
        max_posts,
        pause_start,
        pause_end,
    ) in rows:

        if weekday == today_weekday:
            start_date = today
        else:
            start_date = yesterday

        start_dt = datetime.combine(
            start_date,
            start_time,
            tzinfo=TEHRAN,
        )

        if end_time > start_time:
            end_dt = datetime.combine(
                start_date,
                end_time,
                tzinfo=TEHRAN,
            )
        else:
            end_dt = datetime.combine(
                start_date + timedelta(days=1),
                end_time,
                tzinfo=TEHRAN,
            )

        if not (
            start_dt <= now < end_dt
        ):
            continue

        # -----------------------------------------
        # Pause window
        # -----------------------------------------

        if (
            pause_start is not None
            and pause_end is not None
        ):
            pause_start_date = start_date

            if (
                end_time <= start_time
                and pause_start < start_time
            ):
                pause_start_date = (
                    start_date
                    + timedelta(days=1)
                )

            pause_end_date = start_date

            if (
                end_time <= start_time
                and pause_end < start_time
            ):
                pause_end_date = (
                    start_date
                    + timedelta(days=1)
                )

            pause_start_dt = datetime.combine(
                pause_start_date,
                pause_start,
                tzinfo=TEHRAN,
            )

            pause_end_dt = datetime.combine(
                pause_end_date,
                pause_end,
                tzinfo=TEHRAN,
            )

            if pause_end_dt <= pause_start_dt:
                pause_end_dt += timedelta(
                    days=1
                )

            if (
                pause_start_dt
                <= now
                < pause_end_dt
            ):
                continue

        matches.append(
            {
                "id": window_id,
                "weekday": weekday,
                "start": start_dt,
                "end": end_dt,
                "interval_minutes": int(
                    interval_minutes
                ),
                "max_posts": (
                    int(max_posts)
                    if max_posts is not None
                    else None
                ),
                "pause_start": pause_start,
                "pause_end": pause_end,
            }
        )

    if not matches:
        return None

    matches.sort(
        key=lambda item: item["start"],
        reverse=True,
    )

    return matches[0]


def ensure_scheduler_publish_log():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_publish_log (
                    id BIGSERIAL PRIMARY KEY,
                    property_id BIGINT NOT NULL,
                    window_id BIGINT,
                    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_scheduler_publish_log_time
                ON scheduler_publish_log (published_at)
                """
            )

            conn.commit()



def ensure_scheduler_attempt_log():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_attempt_log (
                    id BIGSERIAL PRIMARY KEY,
                    property_id BIGINT NOT NULL,
                    window_id BIGINT,
                    attempt_status TEXT NOT NULL,
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_scheduler_attempt_log_time
                ON scheduler_attempt_log (attempted_at)
                """
            )

            conn.commit()


def record_scheduler_attempt(
    window_id,
    property_id,
    attempt_status,
):
    ensure_scheduler_attempt_log()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduler_attempt_log (
                    property_id,
                    window_id,
                    attempt_status,
                    attempted_at
                )
                VALUES (%s, %s, %s, NOW())
                """,
                (
                    property_id,
                    window_id,
                    attempt_status,
                ),
            )

            conn.commit()


def get_last_scheduler_attempt(window):
    ensure_scheduler_attempt_log()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(attempted_at)
                FROM scheduler_attempt_log
                WHERE attempted_at >= %s
                  AND attempted_at < %s
                  AND window_id = %s
                """,
                (
                    window["start"],
                    window["end"],
                    window["id"],
                ),
            )

            row = cur.fetchone()

    return row[0] if row else None


def record_scheduler_publish(window_id, property_id):
    ensure_scheduler_publish_log()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduler_publish_log (
                    property_id,
                    window_id,
                    published_at
                )
                VALUES (%s, %s, NOW())
                """,
                (
                    property_id,
                    window_id,
                ),
            )

            conn.commit()


def get_window_publish_stats(window):
    ensure_scheduler_publish_log()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*),
                    MAX(published_at)
                FROM scheduler_publish_log
                WHERE published_at >= %s
                  AND published_at < %s
                  AND window_id = %s
                """,
                (
                    window["start"],
                    window["end"],
                    window["id"],
                ),
            )

            row = cur.fetchone()

    return {
        "count": int(row[0] or 0),
        "last_published_at": row[1],
    }


def get_queue_candidates(limit=50):
    """
    Get files which can be considered for scheduled publishing.

    PENDING_DUPLICATE is included because its review may later
    become APPROVED or REJECTED.
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.property_id,
                    q.queue_status,
                    q.scheduled_for,
                    q.created_at
                FROM publish_queue q
                JOIN properties p
                  ON p.id = q.property_id
                WHERE q.queue_status IN (
                    'READY',
                    'SCHEDULED',
                    'PENDING_DUPLICATE'
                )
                  AND p.status = 'ACTIVE'
                  AND (
                      q.scheduled_for IS NULL
                      OR q.scheduled_for <= NOW()
                  )
                ORDER BY
                    COALESCE(q.priority, 0) DESC,
                    q.created_at ASC,
                    q.id ASC
                LIMIT %s
                """,
                (limit,),
            )

            return cur.fetchall()


def set_queue_status(
    property_id,
    status,
    error_text=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = %s,
                    last_error = %s,
                    updated_at = NOW()
                WHERE property_id = %s
                """,
                (
                    status,
                    error_text,
                    property_id,
                ),
            )

            conn.commit()


def get_duplicate_review(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    review_status,
                    duplicate_of_property_id,
                    similarity_score,
                    match_reason
                FROM duplicate_reviews
                WHERE property_id = %s
                LIMIT 1
                """,
                (property_id,),
            )

            row = cur.fetchone()

    if not row:
        return None

    return {
        "review_status": row[0],
        "duplicate_of_property_id": row[1],
        "similarity_score": row[2],
        "match_reason": row[3],
    }


def prepare_candidate(property_id):
    """
    Returns:
        PUBLISH
        PENDING_DUPLICATE
        REJECTED
    """

    existing_review = get_duplicate_review(
        property_id
    )

    if existing_review:
        status = existing_review[
            "review_status"
        ]

        if status == "APPROVED":
            return "PUBLISH"

        if status == "REJECTED":
            return "REJECTED"

        if status == "PENDING":
            return "PENDING_DUPLICATE"

    duplicate = detect_and_store_duplicate(
        property_id
    )

    if not duplicate:
        return "PUBLISH"

    review_status = duplicate.get(
        "review_status"
    )

    if review_status == "APPROVED":
        return "PUBLISH"

    if review_status == "REJECTED":
        return "REJECTED"

    return "PENDING_DUPLICATE"



def get_republish_candidate():
    """
    Return the oldest READY/SCHEDULED republish item
    whose property is still active and valid.
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    rq.id,
                    rq.property_id,
                    rq.queue_status
                FROM republish_queue rq
                JOIN properties p
                  ON p.id = rq.property_id
                WHERE rq.queue_status IN (
                    'READY',
                    'SCHEDULED'
                )
                  AND p.status = 'ACTIVE'
                  AND p.deleted_from_channel_at IS NULL
                  AND p.published_message_id IS NOT NULL
                  AND p.channel_post_url IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM property_trash t
                      WHERE t.property_id = p.id
                  )
                  AND p.created_at >= NOW() - (
                      COALESCE(
                          (
                              SELECT
                                  (setting_value #>> '{}')::int
                              FROM app_settings
                              WHERE setting_key = 'file_validity_weeks'
                              LIMIT 1
                          ),
                          8
                      ) * INTERVAL '1 week'
                  )
                ORDER BY
                    rq.requested_at ASC,
                    rq.id ASC
                LIMIT 1
                """
            )

            return cur.fetchone()


def set_republish_queue_status(
    queue_id,
    status,
    error_text=None,
    published=False,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE republish_queue
                SET
                    queue_status = %s,
                    last_error = %s,
                    attempts = attempts + 1,
                    published_at = CASE
                        WHEN %s THEN NOW()
                        ELSE published_at
                    END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    status,
                    error_text,
                    published,
                    queue_id,
                ),
            )

            conn.commit()


async def try_republish_queue(
    application,
    window,
):
    candidate = get_republish_candidate()

    if not candidate:
        return False

    (
        queue_id,
        property_id,
        queue_status,
    ) = candidate

    try:
        set_republish_queue_status(
            queue_id,
            "PROCESSING",
        )

        result = await republish_property(
            application.bot,
            property_id,
        )

        set_republish_queue_status(
            queue_id,
            "PUBLISHED",
            published=True,
        )

        record_scheduler_attempt(
            window["id"],
            property_id,
            "REPUBLISH_SUCCESS",
        )

        record_scheduler_publish(
            window["id"],
            property_id,
        )

        logging.info(
            "Scheduler republished property_id=%s "
            "unit_code=%s new_message_id=%s",
            property_id,
            result.get("unit_code"),
            result.get("new_message_id"),
        )

        return True

    except Exception as exc:
        logging.exception(
            "Scheduler republish failed property_id=%s",
            property_id,
        )

        set_republish_queue_status(
            queue_id,
            "FAILED",
            str(exc)[:2000],
        )

        record_scheduler_attempt(
            window["id"],
            property_id,
            "REPUBLISH_FAILED",
        )

        return True


async def run_scheduler_tick(application):
    now = datetime.now(TEHRAN)

    window = get_active_window(now)

    if not window:
        return

    stats = get_window_publish_stats(
        window
    )

    # -------------------------------------------------
    # Respect max_posts
    # -------------------------------------------------

    if (
        window["max_posts"] is not None
        and stats["count"] >= window["max_posts"]
    ):
        return

    # -------------------------------------------------
    # Respect interval_minutes
    # -------------------------------------------------

    last_published_at = stats[
        "last_published_at"
    ]

    last_attempt_at = get_last_scheduler_attempt(
        window
    )

    reference_time = last_published_at

    if (
        last_attempt_at is not None
        and (
            reference_time is None
            or last_attempt_at > reference_time
        )
    ):
        reference_time = last_attempt_at

    if reference_time:
        if reference_time.tzinfo is None:
            reference_time = (
                reference_time.replace(
                    tzinfo=TEHRAN
                )
            )
        else:
            reference_time = (
                reference_time.astimezone(
                    TEHRAN
                )
            )

        next_allowed = (
            reference_time
            + timedelta(
                minutes=window[
                    "interval_minutes"
                ]
            )
        )

        if now < next_allowed:
            return

    candidates = get_queue_candidates()

    if not candidates:
        await try_republish_queue(
            application,
            window,
        )
        return

    # -------------------------------------------------
    # Find first publishable candidate.
    #
    # Duplicate pending files do NOT stop the queue.
    # -------------------------------------------------

    for (
        property_id,
        queue_status,
        scheduled_for,
        created_at,
    ) in candidates:

        try:
            decision = prepare_candidate(
                property_id
            )

            if decision == "PENDING_DUPLICATE":
                if (
                    queue_status
                    != "PENDING_DUPLICATE"
                ):
                    set_queue_status(
                        property_id,
                        "PENDING_DUPLICATE",
                    )

                    logging.warning(
                        "Scheduler duplicate pending: property_id=%s",
                        property_id,
                    )

                # IMPORTANT:
                # continue to next property instead of stopping queue
                continue

            if decision == "REJECTED":
                set_queue_status(
                    property_id,
                    "SKIPPED",
                    "Duplicate rejected by reviewer",
                )

                logging.info(
                    "Scheduler duplicate rejected: property_id=%s",
                    property_id,
                )

                continue

            # -------------------------------------------------
            # APPROVED duplicate or normal property
            # -------------------------------------------------

            if queue_status != "SCHEDULED":
                set_queue_status(
                    property_id,
                    "SCHEDULED",
                )

            result = await publish_property(
                application.bot,
                property_id,
            )

            record_scheduler_attempt(
                window["id"],
                property_id,
                "SUCCESS",
            )

            record_scheduler_publish(
                window["id"],
                property_id,
            )

            logging.info(
                "Scheduler published property_id=%s unit_code=%s message_id=%s",
                property_id,
                result.get("unit_code"),
                result.get("message_id"),
            )

            # Only one scheduled post per interval.
            return

        except Exception as exc:
            logging.exception(
                "Scheduler failed property_id=%s",
                property_id,
            )

            set_queue_status(
                property_id,
                "FAILED",
                str(exc)[:2000],
            )

            record_scheduler_attempt(
                window["id"],
                property_id,
                "FAILED",
            )

            # One real publish attempt per interval.
            # A failed file must NOT trigger another
            # property immediately.
            return

    # No normal property was actually published in this tick.
    # Pending/rejected duplicates must not block republish queue.
    await try_republish_queue(
        application,
        window,
    )


def log_scheduler_health_snapshot():
    """Log a compact, non-sensitive operational snapshot."""
    ensure_scheduler_publish_log()
    ensure_scheduler_attempt_log()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    weekday,
                    start_time::TEXT,
                    end_time::TEXT,
                    interval_minutes,
                    max_posts,
                    pause_start::TEXT,
                    pause_end::TEXT
                FROM schedule_windows
                WHERE is_active = TRUE
                ORDER BY weekday, start_time, id
                """
            )
            windows = cur.fetchall()

            cur.execute(
                """
                SELECT queue_status, COUNT(*)
                FROM publish_queue
                GROUP BY queue_status
                ORDER BY queue_status
                """
            )
            queue_counts = dict(cur.fetchall())

            cur.execute(
                """
                SELECT attempt_status, COUNT(*)
                FROM scheduler_attempt_log
                WHERE attempted_at >= NOW() - INTERVAL '36 hours'
                GROUP BY attempt_status
                ORDER BY attempt_status
                """
            )
            attempts = dict(cur.fetchall())

            cur.execute(
                """
                SELECT COUNT(*)
                FROM scheduler_publish_log
                WHERE published_at >= NOW() - INTERVAL '36 hours'
                """
            )
            published_36h = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT
                    sal.property_id,
                    p.unit_code,
                    sal.attempt_status,
                    TO_CHAR(
                        sal.attempted_at AT TIME ZONE 'Asia/Tehran',
                        'YYYY-MM-DD HH24:MI:SS'
                    )
                FROM scheduler_attempt_log sal
                LEFT JOIN properties p ON p.id = sal.property_id
                WHERE sal.attempted_at >= NOW() - INTERVAL '36 hours'
                ORDER BY sal.attempted_at DESC, sal.id DESC
                LIMIT 10
                """
            )
            recent_attempts = cur.fetchall()

            cur.execute(
                """
                SELECT
                    q.property_id,
                    p.unit_code,
                    LEFT(COALESCE(q.last_error, ''), 500),
                    TO_CHAR(
                        q.updated_at AT TIME ZONE 'Asia/Tehran',
                        'YYYY-MM-DD HH24:MI:SS'
                    )
                FROM publish_queue q
                JOIN properties p ON p.id = q.property_id
                WHERE q.queue_status = 'FAILED'
                ORDER BY q.updated_at DESC, q.id DESC
                LIMIT 5
                """
            )
            recent_failures = cur.fetchall()

    eligible_count = len(get_queue_candidates(limit=500))

    logging.info(
        "SCHEDULER_HEALTH tehran_now=%s windows=%s "
        "queue_counts=%s eligible=%s attempts_36h=%s "
        "published_36h=%s recent_attempts=%s "
        "recent_failures=%s",
        datetime.now(TEHRAN).isoformat(timespec="seconds"),
        windows,
        queue_counts,
        eligible_count,
        attempts,
        published_36h,
        recent_attempts,
        recent_failures,
    )


_scheduler_tick_lock = None


def _get_scheduler_tick_lock():
    global _scheduler_tick_lock

    if _scheduler_tick_lock is None:
        _scheduler_tick_lock = asyncio.Lock()

    return _scheduler_tick_lock


def ensure_scheduler_runtime_state():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_runtime_state (
                    worker_name TEXT PRIMARY KEY,
                    last_started_at TIMESTAMPTZ,
                    last_finished_at TIMESTAMPTZ,
                    last_error TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.commit()


def record_scheduler_runtime(
    worker_name,
    *,
    started=False,
    finished=False,
    error_text=None,
):
    ensure_scheduler_runtime_state()

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduler_runtime_state (
                    worker_name,
                    last_started_at,
                    last_finished_at,
                    last_error,
                    updated_at
                )
                VALUES (
                    %s,
                    CASE WHEN %s THEN NOW() ELSE NULL END,
                    CASE WHEN %s THEN NOW() ELSE NULL END,
                    %s,
                    NOW()
                )
                ON CONFLICT (worker_name)
                DO UPDATE SET
                    last_started_at = CASE
                        WHEN %s THEN NOW()
                        ELSE scheduler_runtime_state.last_started_at
                    END,
                    last_finished_at = CASE
                        WHEN %s THEN NOW()
                        ELSE scheduler_runtime_state.last_finished_at
                    END,
                    last_error = %s,
                    updated_at = NOW()
                """,
                (
                    worker_name,
                    started,
                    finished,
                    error_text,
                    started,
                    finished,
                    error_text,
                ),
            )
            conn.commit()


async def run_guarded_scheduler_tick(
    application,
    worker_name,
):
    lock = _get_scheduler_tick_lock()

    try:
        await asyncio.wait_for(
            lock.acquire(),
            timeout=5,
        )
    except TimeoutError:
        logging.warning(
            "Scheduler %s skipped: tick lock busy",
            worker_name,
        )
        return

    try:
        record_scheduler_runtime(
            worker_name,
            started=True,
        )

        await asyncio.wait_for(
            run_scheduler_tick(application),
            timeout=120,
        )

        record_scheduler_runtime(
            worker_name,
            finished=True,
        )

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        record_scheduler_runtime(
            worker_name,
            finished=True,
            error_text=str(exc)[:1000],
        )
        logging.exception(
            "Scheduler %s tick failed",
            worker_name,
        )

    finally:
        lock.release()


async def scheduler_loop(application):
    logging.info(
        "Scheduler primary loop started."
    )

    while True:
        try:
            await run_guarded_scheduler_tick(
                application,
                "primary",
            )

        except asyncio.CancelledError:
            logging.info(
                "Scheduler primary loop stopped."
            )
            raise

        except Exception:
            logging.exception(
                "Unhandled scheduler primary loop error"
            )

        await asyncio.sleep(
            CHECK_INTERVAL_SECONDS
        )


async def scheduler_watchdog_loop(application):
    logging.info(
        "Scheduler watchdog loop started."
    )

    # Stagger the watchdog from the primary worker.
    await asyncio.sleep(10)

    while True:
        try:
            await run_guarded_scheduler_tick(
                application,
                "watchdog",
            )

        except asyncio.CancelledError:
            logging.info(
                "Scheduler watchdog loop stopped."
            )
            raise

        except Exception:
            logging.exception(
                "Unhandled scheduler watchdog loop error"
            )

        await asyncio.sleep(60)


def start_scheduler(application):
    ensure_scheduler_runtime_state()

    primary = application.bot_data.get(
        "scheduler_task"
    )

    if not primary or primary.done():
        primary = asyncio.create_task(
            scheduler_loop(application),
            name="realestate_scheduler",
        )
        application.bot_data[
            "scheduler_task"
        ] = primary

    watchdog = application.bot_data.get(
        "scheduler_watchdog_task"
    )

    if not watchdog or watchdog.done():
        watchdog = asyncio.create_task(
            scheduler_watchdog_loop(
                application
            ),
            name="realestate_scheduler_watchdog",
        )
        application.bot_data[
            "scheduler_watchdog_task"
        ] = watchdog

    return primary

