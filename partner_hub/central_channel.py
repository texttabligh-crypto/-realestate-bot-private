"""
Agency files in ShahFile's own channel.

Every sale file an agency saves joins ShahFile's channel queue as a partner
listing, and ShahFile's scheduler posts it like any other file (duplicate
review included). Files saved before this join the queue in turn instead of
ahead of it.

Telegram file ids belong to the bot that received a photo, so ShahFile's bot
could not post the agency's photos. They are downloaded through a bot that
can read them, uploaded again by ShahFile's bot into the owner's chat
(silently, deleted right after) and ShahFile's own file ids are stored with
the listing.
"""

import os
from datetime import timedelta

from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError

import channel_tools as ct
import db

TARGET = "CENTRAL_CHANNEL"
NEW_FILE_PRIORITY = 100  # like a file staff add by hand
BACKLOG_PRIORITY = 0
# A job made this soon after its file was saved is for a new file.
NEW_FILE_WINDOW = timedelta(hours=1)
MAX_ATTEMPTS = 5
# ShahFile's channel is for sale listings with a price.
ELIGIBLE_SQL = "COALESCE(s.deal_type,'SALE')='SALE' AND s.current_price_toman IS NOT NULL"
GONE_STATUSES = ("REJECTED", "SOLD", "DELETED")
TIMEOUTS = dict(read_timeout=60, write_timeout=60, connect_timeout=10, pool_timeout=30)

_main_bot = None


def main_bot():
    """ShahFile's own bot, or None when its token is not configured here."""

    global _main_bot
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        return None
    if _main_bot is None:
        _main_bot = Bot(token)
    return _main_bot


def queue(submission_id):
    """Queue a file for ShahFile's channel unless it is already on its way."""

    return db.one(
        f"""INSERT INTO partner_publication_jobs (submission_id, target_type, status)
            SELECT s.id, '{TARGET}', 'PENDING' FROM partner_submissions s
            JOIN partner_agencies a ON a.id=s.agency_id
            WHERE s.id=%s AND COALESCE(a.central_publish_enabled, TRUE)
              AND s.central_property_id IS NULL AND {ELIGIBLE_SQL}
              AND NOT EXISTS (
                  SELECT 1 FROM partner_publication_jobs j
                  WHERE j.submission_id=s.id AND j.target_type='{TARGET}'
                    AND j.status IN ('PENDING','PROCESSING','PUBLISHED'))
            RETURNING id""",
        (submission_id,),
    ) is not None


def queue_backlog():
    """Queue files saved before this feature; returns how many."""

    rows = db.all_rows(
        f"""INSERT INTO partner_publication_jobs (submission_id, target_type, status)
            SELECT s.id, '{TARGET}', 'PENDING' FROM partner_submissions s
            JOIN partner_agencies a ON a.id=s.agency_id
            WHERE a.status='ACTIVE' AND COALESCE(a.central_publish_enabled, TRUE)
              AND s.status = ANY(%s) AND s.central_property_id IS NULL AND {ELIGIBLE_SQL}
              AND NOT EXISTS (
                  SELECT 1 FROM partner_publication_jobs j
                  WHERE j.submission_id=s.id AND j.target_type='{TARGET}')
            ORDER BY s.id
            RETURNING id""",
        (list(ct.PUBLISHABLE_STATUSES),),
    )
    return len(rows)


async def upload_photos(bot, chat_id, photos):
    """Upload photos with ShahFile's bot; returns its own {file_id, unique_id}."""

    if not photos:
        return []
    if len(photos) == 1:
        sent = [await bot.send_photo(chat_id, photos[0], disable_notification=True, **TIMEOUTS)]
    else:
        album = [InputMediaPhoto(data) for data in photos]
        sent = list(await bot.send_media_group(chat_id, album, disable_notification=True, **TIMEOUTS))
    try:
        await bot.delete_messages(chat_id, [message.message_id for message in sent])
    except TelegramError:
        pass
    return [{"file_id": m.photo[-1].file_id, "unique_id": m.photo[-1].file_unique_id} for m in sent]


async def push(job, upload_chat_id, readers=()):
    """Hand one file to ShahFile's queue; returns (property id, photo count)."""

    bot = main_bot()
    if bot is None:
        raise RuntimeError("BOT_TOKEN is not set for the partner hub")
    submission = db.one("SELECT * FROM partner_submissions WHERE id=%s", (job["submission_id"],))
    media = db.all_rows(
        "SELECT * FROM partner_submission_media WHERE submission_id=%s ORDER BY position",
        (submission["id"],),
    )
    bots = [ct.agency_bot(submission["agency_id"]), *readers, ct.hub_bot()]
    photos = [await ct.photo_bytes(bots, item["telegram_file_id"]) for item in media[: ct.ALBUM_LIMIT]]
    uploaded = await upload_photos(bot, upload_chat_id, photos)
    new_file = job["created_at"] - submission["created_at"] <= NEW_FILE_WINDOW
    property_id = db.push_to_central(
        submission["id"], None, media=uploaded,
        priority=NEW_FILE_PRIORITY if new_file else BACKLOG_PRIORITY,
    )
    return property_id, len(uploaded)


def cancel_gone():
    """Drop queued files that were deleted, sold, rejected or are not for sale."""

    db.execute(
        f"""UPDATE partner_publication_jobs j SET status='CANCELLED', updated_at=NOW()
            FROM partner_submissions s
            WHERE s.id=j.submission_id AND j.target_type='{TARGET}' AND j.status='PENDING'
              AND (s.status = ANY(%s) OR NOT ({ELIGIBLE_SQL}))""",
        (list(GONE_STATUSES),),
    )
    db.execute(
        f"""UPDATE partner_publication_jobs j SET status='PUBLISHED', published_at=NOW(), updated_at=NOW()
            FROM partner_submissions s
            WHERE s.id=j.submission_id AND j.target_type='{TARGET}' AND j.status='PENDING'
              AND s.central_property_id IS NOT NULL"""
    )


def due_jobs(limit):
    """Queued files ready to go; a paused file or agency waits its turn."""

    return db.all_rows(
        f"""SELECT j.* FROM partner_publication_jobs j
            JOIN partner_submissions s ON s.id=j.submission_id
            JOIN partner_agencies a ON a.id=s.agency_id
            WHERE j.target_type='{TARGET}' AND j.status='PENDING'
              AND (j.scheduled_for IS NULL OR j.scheduled_for <= NOW())
              AND a.status='ACTIVE' AND COALESCE(a.central_publish_enabled, TRUE)
              AND s.status = ANY(%s)
            ORDER BY j.id LIMIT %s""",
        (list(ct.PUBLISHABLE_STATUSES), limit),
    )


async def run_due(upload_chat_id, readers=(), log=None, limit=3):
    """Hand the next queued files to ShahFile's queue."""

    if main_bot() is None:
        return 0
    cancel_gone()
    done = 0
    for job in due_jobs(limit):
        locked = db.one(
            """UPDATE partner_publication_jobs SET status='PROCESSING', attempts=attempts+1, updated_at=NOW()
               WHERE id=%s AND status='PENDING' RETURNING attempts""",
            (job["id"],),
        )
        if not locked:
            continue
        try:
            property_id, photos = await push(job, upload_chat_id, readers)
        except Exception as exc:
            if log:
                log.warning("agency file to ShahFile queue failed: job=%s submission=%s attempt=%s error=%s",
                            job["id"], job["submission_id"], locked["attempts"], exc)
            db.execute(
                """UPDATE partner_publication_jobs
                   SET status=CASE WHEN attempts>=%s THEN 'FAILED' ELSE 'PENDING' END,
                       last_error=%s, scheduled_for=NOW()+interval '5 minutes', updated_at=NOW()
                   WHERE id=%s""",
                (MAX_ATTEMPTS, str(exc)[:2000], job["id"]),
            )
            continue
        db.execute(
            """UPDATE partner_publication_jobs SET status='PUBLISHED', published_at=NOW(),
                   last_error=NULL, updated_at=NOW() WHERE id=%s""",
            (job["id"],),
        )
        done += 1
        if log:
            log.info("agency file sent to ShahFile queue: job=%s submission=%s property=%s photos=%s",
                     job["id"], job["submission_id"], property_id, photos)
    return done
