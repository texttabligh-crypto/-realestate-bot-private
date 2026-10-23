"""
An agency's own channel: posting a file there, the agency's posting
schedule, and deleting a file.

Telegram file ids belong to the bot that received the photo (the hub bot or
the agency's own bot), so a photo is downloaded through a bot that can read
it and uploaded again by the agency's bot; reusing the id failed with «wrong
file identifier/http url specified».
"""

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import TelegramError

import db
from formatter import fa, public_caption
from security import decrypt_token

TEHRAN = ZoneInfo("Asia/Tehran")
MEDIA_CAPTION_LIMIT = 1024
ALBUM_LIMIT = 10
# Without a schedule, files queued together go out this far apart.
UNSCHEDULED_SPACING = timedelta(minutes=3)
# Files that can still go to the agency channel.
PUBLISHABLE_STATUSES = ("DRAFT", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "ACTIVE")
# Publishing must not take a file out of the central review queue.
CENTRAL_REVIEW_STATUSES = ("SUBMITTED", "UNDER_REVIEW")
WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
STATUS_LABELS = {
    "DRAFT": "ذخیره‌شده",
    "SUBMITTED": "در انتظار بررسی مرکزی",
    "UNDER_REVIEW": "در حال بررسی",
    "APPROVED": "تأییدشده",
    "REJECTED": "ردشده",
    "ACTIVE": "فعال",
    "SOLD": "فروخته‌شده",
    "INACTIVE": "غیرفعال",
    "DELETED": "حذف‌شده",
}
NOT_AVAILABLE_NOTE = "⛔️ این فایل دیگر موجود نیست."

_hub_bot = None


def hub_bot():
    global _hub_bot
    if _hub_bot is None:
        _hub_bot = Bot(os.environ["PARTNER_HUB_TOKEN"])
    return _hub_bot


def agency_bot(agency_id):
    """The agency's own bot, or None when it has not connected one."""

    connection = db.one(
        "SELECT encrypted_token FROM partner_bot_connections WHERE agency_id=%s AND status='ACTIVE'",
        (agency_id,),
    )
    return Bot(decrypt_token(connection["encrypted_token"])) if connection else None


def default_channel(agency_id):
    return db.one(
        "SELECT * FROM partner_channel_connections WHERE agency_id=%s AND is_default AND is_active LIMIT 1",
        (agency_id,),
    )


# ------------------------------------------------------------ publishing

def fit_caption(submission, brand):
    """The public caption, short enough for a photo (1024 characters)."""

    caption = public_caption(submission, brand)
    if len(caption) <= MEDIA_CAPTION_LIMIT:
        return caption
    caption = public_caption({**submission, "special_notes": None}, brand)
    if len(caption) <= MEDIA_CAPTION_LIMIT:
        return caption
    plain = re.sub(r"<[^>]+>", "", caption)
    return plain[: MEDIA_CAPTION_LIMIT - 1] + "…"


def _parse_mode(caption):
    return ParseMode.HTML if "<" in caption else None


async def photo_bytes(bots, file_id):
    """The photo through the first bot that can read its file id."""

    error = None
    for bot in bots:
        if bot is None:
            continue
        try:
            telegram_file = await bot.get_file(file_id, read_timeout=30, connect_timeout=10)
            return bytes(await telegram_file.download_as_bytearray(read_timeout=60, connect_timeout=10))
        except TelegramError as exc:
            error = exc
    raise error or RuntimeError("no bot can read this photo")


async def send_listing(bot, chat_id, caption, media, readers):
    """Post the file to chat_id with bot; returns every message id."""

    mode = _parse_mode(caption)
    if not media:
        sent = await bot.send_message(chat_id, caption, parse_mode=mode)
        return [sent.message_id]
    photos = [await photo_bytes(readers, item["telegram_file_id"]) for item in media[:ALBUM_LIMIT]]
    timeouts = dict(read_timeout=60, write_timeout=60, connect_timeout=10, pool_timeout=30)
    if len(photos) == 1:
        sent = await bot.send_photo(chat_id, photos[0], caption=caption, parse_mode=mode, **timeouts)
        return [sent.message_id]
    album = [
        InputMediaPhoto(data, caption=caption if index == 0 else None, parse_mode=mode if index == 0 else None)
        for index, data in enumerate(photos)
    ]
    sent = await bot.send_media_group(chat_id, album, **timeouts)
    return [message.message_id for message in sent]


async def publish_submission(submission, connection, readers=()):
    """Post a file to the agency channel with the agency's own bot."""

    bot = agency_bot(submission["agency_id"])
    if bot is None or connection is None:
        raise RuntimeError("ربات یا کانال بنگاه متصل نیست.")
    media = db.all_rows(
        "SELECT * FROM partner_submission_media WHERE submission_id=%s ORDER BY position",
        (submission["id"],),
    )
    caption = fit_caption(submission, db.brand(submission["agency_id"]))
    return await send_listing(
        bot, connection["telegram_channel_id"], caption, media, [bot, *readers, hub_bot()]
    )


def record_publication(submission_id, connection_id, message_ids, job_id=None):
    if job_id:
        db.execute(
            """UPDATE partner_publication_jobs
               SET status='PUBLISHED', published_at=NOW(), telegram_message_id=%s,
                   telegram_message_ids=%s, last_error=NULL, updated_at=NOW()
               WHERE id=%s""",
            (message_ids[0], message_ids, job_id),
        )
    else:
        db.execute(
            """INSERT INTO partner_publication_jobs
                 (submission_id, target_type, channel_connection_id, status, published_at,
                  telegram_message_id, telegram_message_ids)
               VALUES (%s, 'AGENCY_CHANNEL', %s, 'PUBLISHED', NOW(), %s, %s)""",
            (submission_id, connection_id, message_ids[0], message_ids),
        )
    db.execute(
        """UPDATE partner_submissions
           SET status=CASE WHEN status = ANY(%s) THEN status ELSE 'ACTIVE' END,
               last_availability_confirmed_at=NOW(),
               availability_check_due_at=NOW()+interval '3 days',
               availability_check_sent_at=NULL,
               availability_reminder_sent_at=NULL,
               availability_missed_cycles=0,
               availability_paused_at=NULL,
               updated_at=NOW()
           WHERE id=%s""",
        (list(CENTRAL_REVIEW_STATUSES), submission_id),
    )


def channel_state(submission_id):
    """('published'|'scheduled'|None, when) for the file's agency channel post."""

    row = db.one(
        """SELECT status, scheduled_for FROM partner_publication_jobs
           WHERE submission_id=%s AND target_type='AGENCY_CHANNEL'
             AND status IN ('PUBLISHED','SCHEDULED','PROCESSING')
           ORDER BY (status='PUBLISHED') DESC, id DESC LIMIT 1""",
        (submission_id,),
    )
    if not row:
        return None, None
    return ("published", None) if row["status"] == "PUBLISHED" else ("scheduled", row["scheduled_for"])


def unpublished_files(agency_id):
    """Files never posted to the agency channel and not waiting in its queue."""

    return db.all_rows(
        """SELECT s.* FROM partner_submissions s
           WHERE s.agency_id=%s AND s.status = ANY(%s)
             AND NOT EXISTS (
                 SELECT 1 FROM partner_publication_jobs j
                 WHERE j.submission_id=s.id AND j.target_type='AGENCY_CHANNEL'
                   AND j.status IN ('SCHEDULED','PROCESSING','PUBLISHED'))
           ORDER BY s.id""",
        (agency_id, list(PUBLISHABLE_STATUSES)),
    )


# -------------------------------------------------------------- schedule

def parse_slot_times(text):
    """«۱۰ ۱۴ ۱۸:۳۰ ۲۱» -> ['10:00', '14:00', '18:30', '21:00']; [] when none."""

    raw = str(text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    times = set()
    for hour, minute in re.findall(r"(?<!\d)(\d{1,2})(?:\s*[:٫.]\s*(\d{2}))?(?!\d)", raw):
        hour, minute = int(hour), int(minute or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.add(f"{hour:02d}:{minute:02d}")
    return sorted(times)[:24]


def schedule_settings(agency_id):
    row = db.one("SELECT * FROM partner_publish_schedules WHERE agency_id=%s", (agency_id,))
    return {"slot_times": list(row["slot_times"] or []), "is_active": row["is_active"]} if row else {
        "slot_times": [], "is_active": True,
    }


def save_schedule(agency_id, **changes):
    settings = {**schedule_settings(agency_id), **changes}
    db.execute(
        """INSERT INTO partner_publish_schedules (agency_id, slot_times, is_active, updated_at)
           VALUES (%s, %s, %s, NOW())
           ON CONFLICT (agency_id) DO UPDATE SET slot_times=EXCLUDED.slot_times,
               is_active=EXCLUDED.is_active, updated_at=NOW()""",
        (agency_id, settings["slot_times"], settings["is_active"]),
    )
    return settings


def free_slots(slot_times, taken, now, count, days=60):
    """The next count slot times after now that no queued post holds."""

    held = {moment.astimezone(TEHRAN).replace(second=0, microsecond=0) for moment in taken}
    slots = []
    for day in range(days):
        date = (now + timedelta(days=day)).date()
        for value in sorted(slot_times):
            hour, minute = map(int, value.split(":"))
            moment = datetime(date.year, date.month, date.day, hour, minute, tzinfo=TEHRAN)
            if moment > now + timedelta(minutes=1) and moment not in held:
                slots.append(moment)
                if len(slots) == count:
                    return slots
    return slots


def taken_slots(agency_id):
    rows = db.all_rows(
        """SELECT j.scheduled_for FROM partner_publication_jobs j
           JOIN partner_submissions s ON s.id=j.submission_id
           WHERE s.agency_id=%s AND j.target_type='AGENCY_CHANNEL'
             AND j.status IN ('SCHEDULED','PROCESSING') AND j.scheduled_for > NOW()""",
        (agency_id,),
    )
    return [row["scheduled_for"] for row in rows]


def plan_times(agency_id, count, now=None):
    """When count queued files would go out: the schedule's free slots, else a few minutes apart."""

    now = now or datetime.now(TEHRAN)
    settings = schedule_settings(agency_id)
    if settings["slot_times"]:
        return free_slots(settings["slot_times"], taken_slots(agency_id), now, count)
    return [now + timedelta(minutes=1) + UNSCHEDULED_SPACING * index for index in range(count)]


def queue_files(agency_id, submission_ids, connection_id, now=None):
    """Queue files for the agency channel; returns their (submission_id, time) pairs."""

    times = plan_times(agency_id, len(submission_ids), now)
    planned = list(zip(submission_ids, times))
    for submission_id, moment in planned:
        db.execute(
            """INSERT INTO partner_publication_jobs
                 (submission_id, target_type, channel_connection_id, status, scheduled_for)
               VALUES (%s, 'AGENCY_CHANNEL', %s, 'SCHEDULED', %s)""",
            (submission_id, connection_id, moment),
        )
    return planned


def reslot_overdue(agency_id, now=None):
    """Spread posts held while the schedule was paused over the next free slots."""

    overdue = db.all_rows(
        """SELECT j.id FROM partner_publication_jobs j
           JOIN partner_submissions s ON s.id=j.submission_id
           WHERE s.agency_id=%s AND j.target_type='AGENCY_CHANNEL'
             AND j.status='SCHEDULED' AND j.scheduled_for <= NOW()
           ORDER BY j.scheduled_for, j.id""",
        (agency_id,),
    )
    for row, moment in zip(overdue, plan_times(agency_id, len(overdue), now)):
        db.execute(
            "UPDATE partner_publication_jobs SET scheduled_for=%s, updated_at=NOW() WHERE id=%s",
            (moment, row["id"]),
        )
    return len(overdue)


def when_label(moment, now=None):
    """«امروز ۱۴:۰۰»، «فردا ۱۰:۰۰» or «پنجشنبه ۱۰:۰۰»."""

    now = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
    moment = moment.astimezone(TEHRAN)
    days = (moment.date() - now.date()).days
    day = "امروز" if days == 0 else "فردا" if days == 1 else WEEKDAYS[moment.weekday()] if days < 7 else fa(moment.strftime("%m/%d"))
    return f"{day} {fa(moment.strftime('%H:%M'))}"


# ---------------------------------------------------------------- delete

def album_message_ids(job, media_count):
    """Every message of a published post; older rows kept only the first id."""

    if job.get("telegram_message_ids"):
        return list(job["telegram_message_ids"])
    first = job["telegram_message_id"]
    # An album is sent at once, so its messages have consecutive ids.
    return list(range(first, first + max(1, min(media_count, ALBUM_LIMIT))))


async def delete_submission(submission):
    """
    Take a file out: its channel post is deleted (or marked no longer
    available when Telegram refuses, e.g. after 48 hours), queued posts are
    cancelled and a central listing made from it is withdrawn.
    """

    sid = submission["id"]
    bot = agency_bot(submission["agency_id"])
    jobs = db.all_rows(
        """SELECT j.*, c.telegram_channel_id FROM partner_publication_jobs j
           JOIN partner_channel_connections c ON c.id=j.channel_connection_id
           WHERE j.submission_id=%s AND j.target_type='AGENCY_CHANNEL'
             AND j.status='PUBLISHED' AND j.telegram_message_id IS NOT NULL""",
        (sid,),
    )
    media_count = (db.one(
        "SELECT count(*) AS n FROM partner_submission_media WHERE submission_id=%s", (sid,)
    ) or {}).get("n", 0)
    kept = []
    for job in jobs:
        try:
            if bot is None:
                raise RuntimeError("agency bot not connected")
            await bot.delete_messages(job["telegram_channel_id"], album_message_ids(job, media_count))
            db.execute("UPDATE partner_publication_jobs SET status='CANCELLED', updated_at=NOW() WHERE id=%s", (job["id"],))
        except Exception:
            kept.append(job)
    if kept and bot is not None:
        caption = fit_caption(submission, db.brand(submission["agency_id"])) + f"\n\n{NOT_AVAILABLE_NOTE}"
        for job in kept:
            try:
                if media_count:
                    await bot.edit_message_caption(chat_id=job["telegram_channel_id"], message_id=job["telegram_message_id"],
                                                   caption=caption[:MEDIA_CAPTION_LIMIT], parse_mode=_parse_mode(caption))
                else:
                    await bot.edit_message_text(chat_id=job["telegram_channel_id"], message_id=job["telegram_message_id"],
                                                text=caption, parse_mode=_parse_mode(caption))
            except Exception:
                pass
    db.execute(
        """UPDATE partner_publication_jobs SET status='CANCELLED', updated_at=NOW()
           WHERE submission_id=%s AND status IN ('PENDING','SCHEDULED')""",
        (sid,),
    )
    db.execute(
        """UPDATE partner_submissions
           SET status='DELETED', deleted_at=NOW(), availability_check_due_at=NULL,
               availability_check_sent_at=NULL, availability_reminder_sent_at=NULL, updated_at=NOW()
           WHERE id=%s""",
        (sid,),
    )
    if submission.get("central_property_id"):
        db.execute("UPDATE properties SET status='INACTIVE', updated_at=NOW() WHERE id=%s",
                   (submission["central_property_id"],))
    return {"deleted_posts": len(jobs) - len(kept), "kept_posts": len(kept)}
