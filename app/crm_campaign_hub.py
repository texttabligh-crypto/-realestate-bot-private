"""CRM campaign audiences, safe broadcasts, and ShahFile support inbox."""

import asyncio
import logging
import os

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def ensure_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_campaign_broadcasts (
                    id BIGSERIAL PRIMARY KEY,
                    showcase_id BIGINT NOT NULL
                        REFERENCES customer_showcases(id) ON DELETE CASCADE,
                    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    message_text TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'QUEUED',
                    total_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    CHECK (status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED'))
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_campaign_broadcast_deliveries (
                    id BIGSERIAL PRIMARY KEY,
                    broadcast_id BIGINT NOT NULL
                        REFERENCES crm_campaign_broadcasts(id) ON DELETE CASCADE,
                    telegram_user_id BIGINT NOT NULL,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    telegram_message_id BIGINT,
                    last_error TEXT,
                    sent_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (broadcast_id, telegram_user_id),
                    CHECK (status IN ('PENDING','SENDING','SENT','FAILED','CANCELLED'))
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_crm_campaign_delivery_queue
                ON crm_campaign_broadcast_deliveries(status, next_attempt_at, id)
            """)
            # A container restart may interrupt a claimed item between the
            # Telegram call and the database update. Return such items to the
            # durable queue so no audience member is silently lost.
            cur.execute("""
                UPDATE crm_campaign_broadcast_deliveries
                SET status='PENDING', next_attempt_at=NOW(), updated_at=NOW()
                WHERE status='SENDING'
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_support_tickets (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    telegram_user_id BIGINT NOT NULL,
                    question_text TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                    answer_text TEXT,
                    answered_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    answered_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CHECK (status IN ('OPEN','ANSWERED','CLOSED'))
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_crm_support_open
                ON crm_support_tickets(status, created_at DESC)
            """)
        conn.commit()


def _back(callback="home", label="⬅️ بازگشت"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback)]])


def _campaign_rows(offset=0, limit=8):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM customer_showcases
            """)
            total = int(cur.fetchone()[0] or 0)
            cur.execute("""
                SELECT s.id, s.query_text, s.is_active, s.created_at,
                       COUNT(DISTINCT v.telegram_user_id) FILTER (
                           WHERE v.is_active = TRUE
                             AND v.phone_collected_at IS NOT NULL
                       ) AS member_count
                FROM customer_showcases s
                LEFT JOIN customer_showcase_visitors v ON v.showcase_id = s.id
                GROUP BY s.id, s.query_text, s.is_active, s.created_at
                ORDER BY s.created_at DESC, s.id DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            return total, cur.fetchall()


def _campaign(showcase_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.query_text, s.filters, s.is_active, s.created_at,
                       COUNT(DISTINCT v.telegram_user_id) FILTER (
                           WHERE v.is_active = TRUE
                             AND v.phone_collected_at IS NOT NULL
                       ) AS member_count
                FROM customer_showcases s
                LEFT JOIN customer_showcase_visitors v ON v.showcase_id = s.id
                WHERE s.id = %s
                GROUP BY s.id, s.query_text, s.filters, s.is_active, s.created_at
            """, (showcase_id,))
            return cur.fetchone()


def _member_rows(showcase_id, offset=0, limit=10):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT v.telegram_user_id)
                FROM customer_showcase_visitors v
                WHERE v.showcase_id = %s AND v.is_active = TRUE
                  AND v.phone_collected_at IS NOT NULL
            """, (showcase_id,))
            total = int(cur.fetchone()[0] or 0)
            cur.execute("""
                SELECT DISTINCT ON (v.telegram_user_id)
                    v.telegram_user_id,
                    v.customer_id,
                    COALESCE(NULLIF(BTRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), ''), 'بدون نام'),
                    COALESCE(cp.phone_number, 'بدون شماره'),
                    COALESCE(v.activated_at, v.joined_at, v.first_opened_at)
                FROM customer_showcase_visitors v
                LEFT JOIN customer_profiles cp ON cp.id = v.customer_id
                WHERE v.showcase_id = %s AND v.is_active = TRUE
                  AND v.phone_collected_at IS NOT NULL
                ORDER BY v.telegram_user_id, COALESCE(v.activated_at, v.joined_at, v.first_opened_at) DESC
                LIMIT %s OFFSET %s
            """, (showcase_id, limit, offset))
            return total, cur.fetchall()


def _campaigns_markup(offset=0, limit=8):
    total, rows = _campaign_rows(offset, limit)
    keyboard = []
    for showcase_id, query_text, active, _created, count in rows:
        icon = "🟢" if active else "⚪️"
        title = (query_text or f"لینک #{showcase_id}").replace("\n", " ")
        keyboard.append([InlineKeyboardButton(
            f"{icon} {title[:38]} | {count} نفر",
            callback_data=f"crm_campaign_view_{showcase_id}",
        )])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"crm_campaign_page_{max(0, offset-limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"crm_campaign_page_{offset+limit}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("💬 سؤال‌های بی‌پاسخ", callback_data="crm_support_inbox")])
    keyboard.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")])
    return InlineKeyboardMarkup(keyboard), total


def _campaign_detail_markup(showcase_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 مشاهده مشتری‌ها", callback_data=f"crm_campaign_members_{showcase_id}_0")],
        [InlineKeyboardButton("📣 ارسال پیام به این دسته", callback_data=f"crm_campaign_broadcast_{showcase_id}")],
        [InlineKeyboardButton("⬅️ دسته‌های مشتریان", callback_data="crm_campaigns")],
    ])


def _members_markup(showcase_id, offset, total, limit=10):
    rows = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"crm_campaign_members_{showcase_id}_{max(0, offset-limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"crm_campaign_members_{showcase_id}_{offset+limit}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📣 ارسال پیام", callback_data=f"crm_campaign_broadcast_{showcase_id}")])
    rows.append([InlineKeyboardButton("⬅️ جزئیات دسته", callback_data=f"crm_campaign_view_{showcase_id}")])
    return InlineKeyboardMarkup(rows)


def _format_members(showcase_id, offset=0):
    total, rows = _member_rows(showcase_id, offset)
    lines = [f"👥 مشتری‌های دسته #{showcase_id}", f"تعداد کل: {total}", ""]
    for index, (_tg, customer_id, name, phone, joined) in enumerate(rows, start=offset + 1):
        joined_text = joined.strftime("%Y-%m-%d %H:%M") if joined else "-"
        lines.append(f"{index}) {name}\n📞 {phone} | CRM #{customer_id or '-'}\n🕐 {joined_text}")
    if not rows:
        lines.append("هنوز مشتریِ دارای شماره در این دسته ثبت نشده است.")
    page = (offset // 10) + 1
    pages = max(1, (total + 9) // 10)
    lines.extend(["", f"صفحه {page} از {pages}"])
    return "\n\n".join(lines), _members_markup(showcase_id, offset, total)


def _create_broadcast(showcase_id, actor_user_id, message_text):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO crm_campaign_broadcasts
                    (showcase_id, created_by_user_id, message_text, status)
                VALUES (%s, %s, %s, 'QUEUED')
                RETURNING id
            """, (showcase_id, actor_user_id, message_text))
            broadcast_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO crm_campaign_broadcast_deliveries
                    (broadcast_id, telegram_user_id, customer_id)
                SELECT %s, v.telegram_user_id, MAX(v.customer_id)
                FROM customer_showcase_visitors v
                WHERE v.showcase_id = %s AND v.is_active = TRUE
                  AND v.phone_collected_at IS NOT NULL
                GROUP BY v.telegram_user_id
                ON CONFLICT (broadcast_id, telegram_user_id) DO NOTHING
            """, (broadcast_id, showcase_id))
            cur.execute("""
                UPDATE crm_campaign_broadcasts
                SET total_count = (
                    SELECT COUNT(*) FROM crm_campaign_broadcast_deliveries
                    WHERE broadcast_id = %s
                ),
                status = CASE WHEN EXISTS (
                    SELECT 1 FROM crm_campaign_broadcast_deliveries
                    WHERE broadcast_id = %s
                ) THEN 'QUEUED' ELSE 'COMPLETED' END,
                completed_at = CASE WHEN EXISTS (
                    SELECT 1 FROM crm_campaign_broadcast_deliveries
                    WHERE broadcast_id = %s
                ) THEN NULL ELSE NOW() END
                WHERE id = %s
            """, (broadcast_id, broadcast_id, broadcast_id, broadcast_id))
        conn.commit()
    return broadcast_id


def _broadcast_status(broadcast_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.id, b.showcase_id, b.status, b.total_count,
                       b.sent_count, b.failed_count, b.created_at,
                       s.query_text
                FROM crm_campaign_broadcasts b
                JOIN customer_showcases s ON s.id = b.showcase_id
                WHERE b.id = %s
            """, (broadcast_id,))
            return cur.fetchone()


def _status_text(row):
    if not row:
        return "ارسال پیدا نشد."
    labels = {"QUEUED": "در صف", "RUNNING": "در حال ارسال", "COMPLETED": "کامل", "PARTIAL": "پایان با خطا", "FAILED": "ناموفق", "CANCELLED": "لغوشده"}
    pending = max(0, row[3] - row[4] - row[5])
    return (
        f"📊 وضعیت ارسال #{row[0]}\n\n"
        f"دسته: {row[7]}\n"
        f"وضعیت: {labels.get(row[2], row[2])}\n"
        f"کل: {row[3]}\n✅ موفق: {row[4]}\n❌ ناموفق: {row[5]}\n⏳ باقی‌مانده: {pending}"
    )


def _claim_deliveries(limit=12):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.broadcast_id, d.telegram_user_id,
                       d.attempts, b.message_text
                FROM crm_campaign_broadcast_deliveries d
                JOIN crm_campaign_broadcasts b ON b.id = d.broadcast_id
                WHERE d.status = 'PENDING'
                  AND d.next_attempt_at <= NOW()
                  AND b.status IN ('QUEUED','RUNNING')
                ORDER BY d.id
                LIMIT %s
                FOR UPDATE OF d SKIP LOCKED
            """, (limit,))
            rows = cur.fetchall()
            if rows:
                ids = [row[0] for row in rows]
                cur.execute("""
                    UPDATE crm_campaign_broadcast_deliveries
                    SET status = 'SENDING', attempts = attempts + 1, updated_at = NOW()
                    WHERE id = ANY(%s)
                """, (ids,))
                cur.execute("""
                    UPDATE crm_campaign_broadcasts
                    SET status = 'RUNNING', started_at = COALESCE(started_at, NOW())
                    WHERE id = ANY(%s) AND status = 'QUEUED'
                """, (list({row[1] for row in rows}),))
        conn.commit()
    return rows


def _finish_delivery(delivery_id, broadcast_id, *, message_id=None, error=None, retry_after=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            if message_id is not None:
                cur.execute("""
                    UPDATE crm_campaign_broadcast_deliveries
                    SET status='SENT', telegram_message_id=%s, sent_at=NOW(),
                        last_error=NULL, updated_at=NOW()
                    WHERE id=%s
                """, (message_id, delivery_id))
            elif retry_after is not None:
                cur.execute("""
                    UPDATE crm_campaign_broadcast_deliveries
                    SET status='PENDING', next_attempt_at=NOW()+(%s * INTERVAL '1 second'),
                        last_error=%s, updated_at=NOW()
                    WHERE id=%s
                """, (retry_after, (error or "retry")[:1000], delivery_id))
            else:
                cur.execute("""
                    UPDATE crm_campaign_broadcast_deliveries
                    SET status='FAILED', last_error=%s, updated_at=NOW()
                    WHERE id=%s
                """, ((error or "unknown")[:1000], delivery_id))
            cur.execute("""
                UPDATE crm_campaign_broadcasts b
                SET sent_count=x.sent, failed_count=x.failed,
                    status=CASE
                        WHEN x.pending > 0 THEN 'RUNNING'
                        WHEN x.failed = 0 THEN 'COMPLETED'
                        WHEN x.sent = 0 THEN 'FAILED'
                        ELSE 'PARTIAL'
                    END,
                    completed_at=CASE WHEN x.pending=0 THEN NOW() ELSE NULL END
                FROM (
                    SELECT COUNT(*) FILTER (WHERE status='SENT')::int sent,
                           COUNT(*) FILTER (WHERE status='FAILED')::int failed,
                           COUNT(*) FILTER (WHERE status IN ('PENDING','SENDING'))::int pending
                    FROM crm_campaign_broadcast_deliveries
                    WHERE broadcast_id=%s
                ) x
                WHERE b.id=%s
            """, (broadcast_id, broadcast_id))
        conn.commit()


async def run_broadcast_queue(application):
    rows = await asyncio.to_thread(_claim_deliveries)
    for delivery_id, broadcast_id, telegram_user_id, attempts, message_text in rows:
        try:
            sent = await application.bot.send_message(
                chat_id=telegram_user_id,
                text="📣 پیام مشاور شما\n\n" + message_text,
                disable_web_page_preview=True,
            )
            await asyncio.to_thread(_finish_delivery, delivery_id, broadcast_id, message_id=sent.message_id)
        except RetryAfter as exc:
            seconds = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
            await asyncio.to_thread(_finish_delivery, delivery_id, broadcast_id, error=str(exc), retry_after=max(2, int(seconds) + 1))
        except TimedOut as exc:
            delay = min(300, 5 * (2 ** min(attempts, 5)))
            await asyncio.to_thread(_finish_delivery, delivery_id, broadcast_id, error=str(exc), retry_after=delay)
        except Exception as exc:
            if attempts + 1 < 5:
                delay = min(600, 10 * (2 ** min(attempts, 5)))
                await asyncio.to_thread(_finish_delivery, delivery_id, broadcast_id, error=str(exc), retry_after=delay)
            else:
                await asyncio.to_thread(_finish_delivery, delivery_id, broadcast_id, error=str(exc))
        await asyncio.sleep(0.15)


async def _broadcast_loop(application):
    logging.info("CRM campaign broadcast worker started.")
    while True:
        try:
            await run_broadcast_queue(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("CRM campaign broadcast worker error")
        await asyncio.sleep(3)


def start_worker(application):
    existing = application.bot_data.get("crm_campaign_broadcast_task")
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_broadcast_loop(application), name="crm_campaign_broadcasts")
    application.bot_data["crm_campaign_broadcast_task"] = task
    return task


def _support_manager_chat_ids():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH permitted AS (
                    SELECT u.id
                    FROM users u
                    WHERE u.is_active=TRUE AND (
                        u.role='OWNER'
                        OR EXISTS (
                            SELECT 1 FROM user_permissions up
                            WHERE up.user_id=u.id
                              AND up.permission_key='customers_manage'
                              AND up.is_allowed=TRUE
                        )
                        OR (
                            u.role IN ('ADMIN','ADVISOR')
                            AND NOT EXISTS (SELECT 1 FROM user_permissions x WHERE x.user_id=u.id)
                        )
                    )
                )
                SELECT DISTINCT ON (p.id) us.telegram_user_id
                FROM permitted p
                JOIN user_sessions us ON us.user_id=p.id
                WHERE us.is_active=TRUE AND us.telegram_user_id IS NOT NULL
                ORDER BY p.id, us.last_activity_at DESC NULLS LAST, us.id DESC
            """)
            return [row[0] for row in cur.fetchall()]


def _open_or_create_ticket(customer_id, telegram_user_id, question_text):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM crm_support_tickets
                WHERE telegram_user_id=%s AND question_text=%s AND status='OPEN'
                  AND created_at > NOW() - INTERVAL '10 minutes'
                ORDER BY id DESC LIMIT 1
            """, (telegram_user_id, question_text))
            row = cur.fetchone()
            if row:
                return row[0], False
            cur.execute("""
                INSERT INTO crm_support_tickets(customer_id, telegram_user_id, question_text)
                VALUES (%s,%s,%s) RETURNING id
            """, (customer_id, telegram_user_id, question_text[:2000]))
            ticket_id = cur.fetchone()[0]
        conn.commit()
    return ticket_id, True


async def escalate_customer_question(bot, user, customer_id, question_text):
    question = (question_text or "").strip()[:2000]
    if not question:
        return False
    ticket_id, created = await asyncio.to_thread(
        _open_or_create_ticket, customer_id, user.id, question
    )
    if created:
        name = " ".join(part for part in (user.first_name, user.last_name) if part) or "بدون نام"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "✍️ پاسخ به مشتری", callback_data=f"crm_support_reply_{ticket_id}"
        )]])
        for chat_id in await asyncio.to_thread(_support_manager_chat_ids):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(f"💬 سؤال بی‌پاسخ جدید #{ticket_id}\n\n"
                          f"مشتری: {name}\nCRM: #{customer_id or '-'}\n\n{question}"),
                    reply_markup=markup,
                )
            except Exception:
                logging.exception("support ticket notification failed ticket=%s chat=%s", ticket_id, chat_id)
    return True


def _support_rows(limit=20):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.question_text, t.created_at,
                       COALESCE(NULLIF(BTRIM(CONCAT_WS(' ',cp.first_name,cp.last_name)),''),'بدون نام')
                FROM crm_support_tickets t
                LEFT JOIN customer_profiles cp ON cp.id=t.customer_id
                WHERE t.status='OPEN'
                ORDER BY t.created_at, t.id
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


def _support_ticket(ticket_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id,t.customer_id,t.telegram_user_id,t.question_text,t.status,t.created_at,
                       COALESCE(NULLIF(BTRIM(CONCAT_WS(' ',cp.first_name,cp.last_name)),''),'بدون نام'),
                       COALESCE(cp.phone_number,'بدون شماره')
                FROM crm_support_tickets t
                LEFT JOIN customer_profiles cp ON cp.id=t.customer_id
                WHERE t.id=%s
            """, (ticket_id,))
            return cur.fetchone()


async def handle_button(update, context, session):
    query = update.callback_query
    action = (query.data if query else "") or ""
    if not action.startswith("crm_campaign") and not action.startswith("crm_support"):
        return False

    if action == "crm_campaigns" or action.startswith("crm_campaign_page_"):
        offset = int(action.rsplit("_", 1)[1]) if action.startswith("crm_campaign_page_") else 0
        markup, total = await asyncio.to_thread(_campaigns_markup, offset)
        await query.edit_message_text(f"🎯 دسته‌بندی مشتریان لینک‌ها\n\nتعداد لینک‌ها: {total}", reply_markup=markup)
        return True

    if action.startswith("crm_campaign_view_"):
        showcase_id = int(action.rsplit("_", 1)[1])
        row = await asyncio.to_thread(_campaign, showcase_id)
        if not row:
            await query.edit_message_text("این دسته پیدا نشد.", reply_markup=_back("crm_campaigns"))
            return True
        await query.edit_message_text(
            f"🎯 {row[1]}\n\nشناسه دسته: #{row[0]}\nمشتری ثبت‌شده: {row[5]} نفر\nوضعیت لینک: {'فعال' if row[3] else 'غیرفعال'}",
            reply_markup=_campaign_detail_markup(showcase_id),
        )
        return True

    if action.startswith("crm_campaign_members_"):
        parts = action.split("_")
        showcase_id, offset = int(parts[-2]), int(parts[-1])
        text, markup = await asyncio.to_thread(_format_members, showcase_id, offset)
        await query.edit_message_text(text[:4000], reply_markup=markup)
        return True

    if action.startswith("crm_campaign_broadcast_"):
        showcase_id = int(action.rsplit("_", 1)[1])
        row = await asyncio.to_thread(_campaign, showcase_id)
        if not row:
            await query.edit_message_text("این دسته پیدا نشد.", reply_markup=_back("crm_campaigns"))
            return True
        context.user_data.clear()
        context.user_data.update({"state": "crm_campaign_broadcast_text", "crm_campaign_id": showcase_id})
        await query.edit_message_text(
            f"📣 ارسال به دسته «{row[1]}» ({row[5]} نفر)\n\nمتن پیام را بفرستید.",
            reply_markup=_back(f"crm_campaign_view_{showcase_id}", "لغو"),
        )
        return True

    if action == "crm_campaign_broadcast_cancel":
        context.user_data.clear()
        await query.edit_message_text("ارسال لغو شد.", reply_markup=_back("crm_campaigns"))
        return True

    if action.startswith("crm_campaign_broadcast_confirm_"):
        showcase_id = int(action.rsplit("_", 1)[1])
        message = context.user_data.get("crm_campaign_message")
        if not message or context.user_data.get("crm_campaign_id") != showcase_id:
            await query.edit_message_text("متن پیام منقضی شده؛ دوباره شروع کنید.", reply_markup=_back(f"crm_campaign_view_{showcase_id}"))
            return True
        broadcast_id = await asyncio.to_thread(_create_broadcast, showcase_id, session[0], message)
        context.user_data.clear()
        status = await asyncio.to_thread(_broadcast_status, broadcast_id)
        await query.edit_message_text(
            "✅ پیام وارد صف امن ارسال شد.\n\n" + _status_text(status),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تازه‌سازی آمار", callback_data=f"crm_campaign_status_{broadcast_id}")],[InlineKeyboardButton("⬅️ دسته‌ها", callback_data="crm_campaigns")]]),
        )
        return True

    if action.startswith("crm_campaign_status_"):
        broadcast_id = int(action.rsplit("_", 1)[1])
        row = await asyncio.to_thread(_broadcast_status, broadcast_id)
        await query.edit_message_text(_status_text(row), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تازه‌سازی", callback_data=f"crm_campaign_status_{broadcast_id}")],[InlineKeyboardButton("⬅️ دسته‌ها", callback_data="crm_campaigns")]]))
        return True

    if action == "crm_support_inbox":
        rows = await asyncio.to_thread(_support_rows)
        keyboard = [[InlineKeyboardButton(f"#{r[0]} | {r[3]} | {r[1][:28]}", callback_data=f"crm_support_view_{r[0]}")] for r in rows]
        keyboard.append([InlineKeyboardButton("⬅️ دسته‌ها", callback_data="crm_campaigns")])
        await query.edit_message_text(f"💬 سؤال‌های بی‌پاسخ\n\nتعداد نمایش‌داده‌شده: {len(rows)}", reply_markup=InlineKeyboardMarkup(keyboard))
        return True

    if action.startswith("crm_support_view_"):
        ticket_id = int(action.rsplit("_", 1)[1])
        row = await asyncio.to_thread(_support_ticket, ticket_id)
        if not row:
            await query.edit_message_text("سؤال پیدا نشد.", reply_markup=_back("crm_support_inbox"))
            return True
        await query.edit_message_text(
            f"💬 سؤال #{row[0]}\n\nمشتری: {row[6]}\n📞 {row[7]}\nCRM: #{row[1] or '-'}\n\n{row[3]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ پاسخ", callback_data=f"crm_support_reply_{ticket_id}")],[InlineKeyboardButton("⬅️ صندوق سؤال‌ها", callback_data="crm_support_inbox")]]),
        )
        return True

    if action.startswith("crm_support_reply_"):
        ticket_id = int(action.rsplit("_", 1)[1])
        row = await asyncio.to_thread(_support_ticket, ticket_id)
        if not row or row[4] != "OPEN":
            await query.edit_message_text("این سؤال قبلاً پاسخ داده شده یا موجود نیست.", reply_markup=_back("crm_support_inbox"))
            return True
        context.user_data.clear()
        context.user_data.update({"state": "crm_support_reply_text", "crm_support_ticket_id": ticket_id})
        await query.edit_message_text(f"پاسخ سؤال #{ticket_id} را بنویسید:\n\n{row[3]}", reply_markup=_back("crm_support_inbox", "لغو"))
        return True

    return False


async def handle_text(update, context, session):
    state = context.user_data.get("state")
    text = (update.message.text or "").strip()
    if state == "crm_campaign_broadcast_text":
        if not text or len(text) > 3500:
            await update.message.reply_text("متن پیام باید بین ۱ تا ۳۵۰۰ کاراکتر باشد.")
            return True
        showcase_id = context.user_data.get("crm_campaign_id")
        context.user_data["crm_campaign_message"] = text
        row = await asyncio.to_thread(_campaign, showcase_id)
        count = row[5] if row else 0
        await update.message.reply_text(
            f"پیش‌نمایش پیام برای {count} نفر:\n\n{text}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید و ورود به صف", callback_data=f"crm_campaign_broadcast_confirm_{showcase_id}")],[InlineKeyboardButton("❌ لغو", callback_data="crm_campaign_broadcast_cancel")]]),
        )
        return True

    if state == "crm_support_reply_text":
        if not text or len(text) > 3500:
            await update.message.reply_text("پاسخ باید بین ۱ تا ۳۵۰۰ کاراکتر باشد.")
            return True
        ticket_id = context.user_data.get("crm_support_ticket_id")
        row = await asyncio.to_thread(_support_ticket, ticket_id)
        if not row or row[4] != "OPEN":
            context.user_data.clear()
            await update.message.reply_text("این سؤال قبلاً پاسخ داده شده یا موجود نیست.", reply_markup=_back("crm_support_inbox"))
            return True
        try:
            await context.bot.send_message(chat_id=row[2], text="💬 پاسخ مشاور شما\n\n" + text)
        except Exception as exc:
            logging.exception("support answer delivery failed ticket=%s", ticket_id)
            await update.message.reply_text("❌ پاسخ ارسال نشد و سؤال باز ماند. کمی بعد دوباره تلاش کنید.\n" + str(exc)[:300])
            return True
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE crm_support_tickets
                    SET status='ANSWERED', answer_text=%s, answered_by_user_id=%s,
                        answered_at=NOW(), updated_at=NOW()
                    WHERE id=%s AND status='OPEN'
                """, (text, session[0], ticket_id))
            conn.commit()
        context.user_data.clear()
        await update.message.reply_text("✅ پاسخ مستقیم برای مشتری ارسال شد.", reply_markup=_back("crm_support_inbox"))
        return True
    return False
