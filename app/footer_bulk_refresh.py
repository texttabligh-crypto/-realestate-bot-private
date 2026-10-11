import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import (
    build_publish_text,
    get_destination_channel,
    get_property_for_publish,
    get_property_media,
    get_publish_settings,
)
from telegram_delivery import edit_published_message

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")

JOB_STATUSES = (
    "PENDING",
    "RUNNING",
    "DONE",
    "DONE_WITH_ERRORS",
    "FAILED",
    "CANCELLED_SETTINGS_CHANGED",
)


def db_connect():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def ensure_footer_bulk_refresh_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS footer_bulk_refresh_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    actor_telegram_user_id BIGINT,
                    chat_id BIGINT,
                    control_message_id BIGINT,
                    status VARCHAR(40) NOT NULL DEFAULT 'PENDING',
                    settings_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
                    settings_hash VARCHAR(64) NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS footer_bulk_refresh_items (
                    id BIGSERIAL PRIMARY KEY,
                    job_id BIGINT NOT NULL REFERENCES footer_bulk_refresh_jobs(id) ON DELETE CASCADE,
                    property_id BIGINT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                    telegram_message_id BIGINT,
                    status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(job_id, property_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_footer_bulk_jobs_status
                ON footer_bulk_refresh_jobs(status, created_at)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_footer_bulk_items_job_status
                ON footer_bulk_refresh_items(job_id, status, id)
                """
            )
            # A process restart may leave one row marked PROCESSING.
            cur.execute(
                """
                UPDATE footer_bulk_refresh_items i
                SET status = 'PENDING', updated_at = NOW()
                FROM footer_bulk_refresh_jobs j
                WHERE i.job_id = j.id
                  AND i.status = 'PROCESSING'
                  AND j.status IN ('PENDING', 'RUNNING')
                """
            )
        conn.commit()


def _footer_snapshot():
    settings = get_publish_settings()
    people = settings.get("contact_people")
    if not isinstance(people, list):
        people = []
    return {
        "publish_footer": str(settings.get("publish_footer") or ""),
        "contact_people": people,
        "contact_name": str(settings.get("contact_name") or ""),
        "contact_phone": str(settings.get("contact_phone") or ""),
        "contact_telegram": str(settings.get("contact_telegram") or ""),
    }


def _snapshot_hash(snapshot):
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_footer_hash():
    return _snapshot_hash(_footer_snapshot())


def footer_preview_text():
    snapshot = _footer_snapshot()
    lines = []
    footer = snapshot.get("publish_footer", "").strip()
    if footer:
        lines.append(footer)

    people = snapshot.get("contact_people") or []
    if people:
        for person in people:
            name = str((person or {}).get("name") or "").strip()
            phone = str((person or {}).get("phone") or "").strip()
            if name:
                lines.append(f"👤 {name}")
            if phone:
                lines.append(f"📞 {phone}")
    else:
        name = snapshot.get("contact_name", "").strip()
        phone = snapshot.get("contact_phone", "").strip()
        if name:
            lines.append(f"👤 {name}")
        if phone:
            lines.append(f"📞 {phone}")

    telegram = snapshot.get("contact_telegram", "").strip()
    if telegram:
        if not telegram.startswith("@"):
            telegram = "@" + telegram
        lines.append(f"📱 {telegram}")

    return "\n".join(lines).strip() or "(فوتر خالی است)"


def get_candidate_count():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM properties
                WHERE published_message_id IS NOT NULL
                  AND deleted_from_channel_at IS NULL
                """
            )
            return int(cur.fetchone()[0] or 0)


def footer_bulk_offer_text(saved_message=None):
    count = get_candidate_count()
    parts = []
    if saved_message:
        parts.append(str(saved_message).strip())
    parts.extend([
        "♻️ اعمال فوتر روی پست‌های قبلی",
        "",
        f"{count} پست منتشرشده و قابل بررسی پیدا شد.",
        "",
        "پیش‌نمایش فوتر فعلی:",
        footer_preview_text(),
        "",
        "آیا این فوتر روی همه پست‌های قبلی هم اعمال شود؟",
    ])
    return "\n".join(parts).strip()


def footer_bulk_offer_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، روی همه پست‌های قبلی اعمال کن", callback_data="footer_bulk_apply_all")],
        [InlineKeyboardButton("❌ خیر، فقط از این به بعد", callback_data="footer_bulk_no")],
        [InlineKeyboardButton("⬅️ بازگشت به تنظیمات فوتر", callback_data="settings_footer")],
    ])


def audit_footer_setting_change(actor_user_id, key, old_value, new_value):
    details = {
        "setting_key": key,
        "old_value": old_value,
        "new_value": new_value,
    }
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_audit_log(actor_user_id, action, details)
                VALUES (%s, 'FOOTER_SETTING_CHANGED', %s::jsonb)
                """,
                (actor_user_id, json.dumps(details, ensure_ascii=False, default=str)),
            )
        conn.commit()


def _candidate_rows(cur, property_ids=None):
    if property_ids:
        cur.execute(
            """
            SELECT id, published_message_id
            FROM properties
            WHERE id = ANY(%s)
              AND published_message_id IS NOT NULL
              AND deleted_from_channel_at IS NULL
            ORDER BY id
            """,
            (list(property_ids),),
        )
    else:
        cur.execute(
            """
            SELECT id, published_message_id
            FROM properties
            WHERE published_message_id IS NOT NULL
              AND deleted_from_channel_at IS NULL
            ORDER BY id
            """
        )
    return cur.fetchall()


def create_refresh_job(*, session, telegram_user_id, chat_id, control_message_id, property_ids=None, retry_of=None):
    snapshot = _footer_snapshot()
    settings_hash = _snapshot_hash(snapshot)

    with db_connect() as conn:
        with conn.cursor() as cur:
            rows = _candidate_rows(cur, property_ids=property_ids)
            cur.execute(
                """
                INSERT INTO footer_bulk_refresh_jobs(
                    actor_user_id, actor_telegram_user_id, chat_id,
                    control_message_id, status, settings_snapshot,
                    settings_hash, total_count
                )
                VALUES (%s, %s, %s, %s, 'PENDING', %s::jsonb, %s, %s)
                RETURNING id
                """,
                (
                    session[0], telegram_user_id, chat_id, control_message_id,
                    json.dumps(snapshot, ensure_ascii=False), settings_hash, len(rows),
                ),
            )
            job_id = cur.fetchone()[0]
            if rows:
                cur.executemany(
                    """
                    INSERT INTO footer_bulk_refresh_items(
                        job_id, property_id, telegram_message_id, status
                    )
                    VALUES (%s, %s, %s, 'PENDING')
                    ON CONFLICT (job_id, property_id) DO NOTHING
                    """,
                    [(job_id, pid, mid) for pid, mid in rows],
                )
            details = {
                "job_id": job_id,
                "total_count": len(rows),
                "settings_snapshot": snapshot,
                "retry_of": retry_of,
            }
            cur.execute(
                """
                INSERT INTO admin_audit_log(actor_user_id, action, details)
                VALUES (%s, 'FOOTER_BULK_REFRESH_QUEUED', %s::jsonb)
                """,
                (session[0], json.dumps(details, ensure_ascii=False, default=str)),
            )
        conn.commit()
    return job_id, len(rows)


def _job_row(job_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, total_count, processed_count, success_count,
                       failed_count, skipped_count, error_text, actor_user_id
                FROM footer_bulk_refresh_jobs
                WHERE id = %s
                """,
                (job_id,),
            )
            return cur.fetchone()


def _failed_property_ids(job_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT property_id
                FROM footer_bulk_refresh_items
                WHERE job_id = %s AND status = 'FAILED'
                ORDER BY id
                """,
                (job_id,),
            )
            return [r[0] for r in cur.fetchall()]


def _progress_text(job_id):
    row = _job_row(job_id)
    if not row:
        return "❌ عملیات پیدا نشد."
    _, status, total, processed, success, failed, skipped, error_text, _ = row
    labels = {
        "PENDING": "⏳ در صف",
        "RUNNING": "♻️ در حال اجرا",
        "DONE": "✅ تکمیل شد",
        "DONE_WITH_ERRORS": "⚠️ تکمیل شد با خطا",
        "FAILED": "❌ متوقف شد",
        "CANCELLED_SETTINGS_CHANGED": "⛔ لغو شد؛ فوتر در حین اجرا تغییر کرد",
    }
    remaining = max(int(total or 0) - int(processed or 0), 0)
    text = (
        f"♻️ بروزرسانی سراسری فوتر — عملیات #{job_id}\n\n"
        f"وضعیت: {labels.get(status, status)}\n"
        f"کل: {total}\n"
        f"✅ موفق: {success}\n"
        f"❌ ناموفق: {failed}\n"
        f"⏭ ردشده: {skipped}\n"
        f"⏳ باقی‌مانده: {remaining}"
    )
    if error_text:
        text += f"\n\nخطا: {str(error_text)[:800]}"
    return text


def _final_markup(job_id, failed_count):
    rows = []
    if failed_count:
        rows.append([InlineKeyboardButton("🔁 تلاش مجدد ناموفق‌ها", callback_data=f"footer_bulk_retry_{job_id}")])
    rows.append([InlineKeyboardButton("📝 تنظیمات فوتر", callback_data="settings_footer")])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")])
    return InlineKeyboardMarkup(rows)


async def _safe_progress(bot, job_id, *, final=False):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chat_id, control_message_id, failed_count
                FROM footer_bulk_refresh_jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return
    chat_id, message_id, failed_count = row
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_progress_text(job_id),
            reply_markup=_final_markup(job_id, failed_count) if final else None,
            disable_web_page_preview=True,
        )
    except Exception:
        logging.debug("Footer bulk progress message update failed", exc_info=True)


def _claim_next_job():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM footer_bulk_refresh_jobs
                WHERE status IN ('PENDING', 'RUNNING')
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            job_id = row[0]
            cur.execute(
                """
                UPDATE footer_bulk_refresh_jobs
                SET status = 'RUNNING',
                    started_at = COALESCE(started_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (job_id,),
            )
        conn.commit()
    return job_id


def _next_item(job_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, property_id, telegram_message_id
                FROM footer_bulk_refresh_items
                WHERE job_id = %s AND status = 'PENDING'
                ORDER BY id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            cur.execute(
                """
                UPDATE footer_bulk_refresh_items
                SET status = 'PROCESSING', attempts = attempts + 1, updated_at = NOW()
                WHERE id = %s
                """,
                (row[0],),
            )
        conn.commit()
    return row


def _mark_item(job_id, item_id, status, error=None):
    success_inc = 1 if status == "APPLIED" else 0
    failed_inc = 1 if status == "FAILED" else 0
    skipped_inc = 1 if status == "SKIPPED" else 0
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE footer_bulk_refresh_items
                SET status = %s, error_text = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, str(error)[:3000] if error else None, item_id),
            )
            cur.execute(
                """
                UPDATE footer_bulk_refresh_jobs
                SET processed_count = processed_count + 1,
                    success_count = success_count + %s,
                    failed_count = failed_count + %s,
                    skipped_count = skipped_count + %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (success_inc, failed_inc, skipped_inc, job_id),
            )
        conn.commit()


def _job_settings_hash(job_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT settings_hash FROM footer_bulk_refresh_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            return row[0] if row else None


def _finish_job(job_id, *, status=None, error=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT total_count, processed_count, failed_count, actor_user_id
                FROM footer_bulk_refresh_jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            total, processed, failed, actor_user_id = row
            if status is None:
                status = "DONE_WITH_ERRORS" if failed else "DONE"
            cur.execute(
                """
                UPDATE footer_bulk_refresh_jobs
                SET status = %s, error_text = %s, finished_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (status, str(error)[:4000] if error else None, job_id),
            )
            details = {
                "job_id": job_id,
                "status": status,
                "total_count": total,
                "processed_count": processed,
                "failed_count": failed,
            }
            cur.execute(
                """
                INSERT INTO admin_audit_log(actor_user_id, action, details)
                VALUES (%s, 'FOOTER_BULK_REFRESH_FINISHED', %s::jsonb)
                """,
                (actor_user_id, json.dumps(details, ensure_ascii=False)),
            )
        conn.commit()


async def _process_job(application, job_id):
    destination = get_destination_channel()
    if not destination:
        _finish_job(job_id, status="FAILED", error="کانال مقصد فعال پیدا نشد")
        await _safe_progress(application.bot, job_id, final=True)
        return
    channel_id = destination[0]
    expected_hash = _job_settings_hash(job_id)
    last_progress_at = 0

    while True:
        if current_footer_hash() != expected_hash:
            _finish_job(
                job_id,
                status="CANCELLED_SETTINGS_CHANGED",
                error="تنظیمات فوتر بعد از شروع عملیات تغییر کرده است؛ عملیات جدید اجرا کنید.",
            )
            await _safe_progress(application.bot, job_id, final=True)
            return

        item = _next_item(job_id)
        if not item:
            break
        item_id, property_id, stored_message_id = item

        try:
            row = get_property_for_publish(property_id)
            if not row:
                _mark_item(job_id, item_id, "SKIPPED", "فایل دیگر قابل انتشار نیست")
                continue

            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT published_message_id, deleted_from_channel_at
                        FROM properties WHERE id = %s
                        """,
                        (property_id,),
                    )
                    current = cur.fetchone()
            if not current or not current[0] or current[1] is not None:
                _mark_item(job_id, item_id, "SKIPPED", "پست دیگر روی کانال فعال نیست")
                continue

            message_id = current[0]
            media = get_property_media(property_id)
            has_photo = any(m[1] == "PHOTO" for m in media)
            text = build_publish_text(
                row,
                unit_code=row[1],
                has_photo_override=has_photo,
            )
            await edit_published_message(
                application.bot,
                chat_id=channel_id,
                message_id=message_id,
                text=text,
                has_photo=has_photo,
                attempts=5,
            )
            _mark_item(job_id, item_id, "APPLIED")

        except Exception as exc:
            logging.exception(
                "FOOTER_BULK_REFRESH item failed job_id=%s property_id=%s",
                job_id,
                property_id,
            )
            _mark_item(job_id, item_id, "FAILED", exc)

        # Deliberately conservative pacing. RetryAfter inside edit_published_message
        # adds its own Telegram-requested wait if the platform asks for more.
        await asyncio.sleep(1.25)

        row = _job_row(job_id)
        if row and (row[3] - last_progress_at >= 5):
            last_progress_at = row[3]
            await _safe_progress(application.bot, job_id)

    _finish_job(job_id)
    await _safe_progress(application.bot, job_id, final=True)


async def footer_bulk_refresh_worker(application):
    logging.info("Footer bulk refresh worker started.")
    while True:
        try:
            job_id = _claim_next_job()
            if not job_id:
                await asyncio.sleep(3)
                continue
            await _process_job(application, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Footer bulk refresh worker loop failed")
            await asyncio.sleep(5)


def start_footer_bulk_refresh_worker(application):
    existing = application.bot_data.get("footer_bulk_refresh_worker_task")
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(
        footer_bulk_refresh_worker(application),
        name="footer_bulk_refresh_worker",
    )
    application.bot_data["footer_bulk_refresh_worker_task"] = task
    return task


async def handle_footer_bulk_button(update, context, session, *, allowed, back_markup):
    query = update.callback_query
    action = (query.data if query else "") or ""
    if not action.startswith("footer_bulk_"):
        return False

    if not allowed:
        await query.edit_message_text("⛔ مجوز تنظیمات انتشار را ندارید.")
        return True

    if action == "footer_bulk_offer":
        await query.edit_message_text(
            footer_bulk_offer_text(),
            reply_markup=footer_bulk_offer_markup(),
        )
        return True

    if action == "footer_bulk_no":
        await query.edit_message_text(
            "✅ تنظیمات ذخیره ماند.\n\n"
            "پست‌های قبلی تغییر نمی‌کنند و فوتر جدید از این به بعد استفاده می‌شود.",
            reply_markup=back_markup,
        )
        return True

    if action == "footer_bulk_apply_all":
        job_id, count = create_refresh_job(
            session=session,
            telegram_user_id=update.effective_user.id,
            chat_id=query.message.chat.id,
            control_message_id=query.message.message_id,
        )
        if count == 0:
            _finish_job(job_id, status="DONE")
            await query.edit_message_text(
                "ℹ️ هیچ پست منتشرشده‌ای برای بروزرسانی پیدا نشد.",
                reply_markup=back_markup,
            )
            return True
        await query.edit_message_text(_progress_text(job_id))
        # Wake immediately; startup worker remains the durable executor.
        await asyncio.sleep(0)
        return True

    if action.startswith("footer_bulk_retry_"):
        try:
            old_job_id = int(action.rsplit("_", 1)[1])
        except Exception:
            await query.edit_message_text("❌ شناسه عملیات نامعتبر است.", reply_markup=back_markup)
            return True
        old = _job_row(old_job_id)
        if not old:
            await query.edit_message_text("❌ عملیات قبلی پیدا نشد.", reply_markup=back_markup)
            return True
        # Only the original actor or OWNER should retry. OWNER role is index 3.
        if session[3] != "OWNER" and old[8] != session[0]:
            await query.edit_message_text("⛔ این عملیات متعلق به کاربر دیگری است.")
            return True
        ids = _failed_property_ids(old_job_id)
        if not ids:
            await query.edit_message_text("✅ مورد ناموفقی برای Retry وجود ندارد.", reply_markup=back_markup)
            return True
        job_id, count = create_refresh_job(
            session=session,
            telegram_user_id=update.effective_user.id,
            chat_id=query.message.chat.id,
            control_message_id=query.message.message_id,
            property_ids=ids,
            retry_of=old_job_id,
        )
        await query.edit_message_text(_progress_text(job_id))
        return True

    return False
