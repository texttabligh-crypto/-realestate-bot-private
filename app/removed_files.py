"""Listings deleted from their source whose channel post must be removed by hand.

Telegram does not let bots delete channel posts older than 48 hours, so the
source watcher withdraws such a listing and records it here. Settings ›
«🧹 فایل‌های حذف‌شده» lists the posts still in the channel with their links;
a post the operator has deleted drops off the list on the next view.
"""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import (
    build_channel_post_url,
    db_connect,
    get_destination_channel,
)

REMOVED_FILES_ACTION = "removed_files"
REMOVED_FILES_PERMISSION = "removed_files_view"
REMOVED_FILES_TITLE = "🧹 فایل‌های حذف‌شده"
REMOVED_FILES_PAGE_SIZE = 30
POST_CHECK_CONCURRENCY = 5


def ensure_removed_files_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_manual_removals (
                    property_id BIGINT PRIMARY KEY
                        REFERENCES properties(id) ON DELETE CASCADE,
                    published_message_id BIGINT NOT NULL,
                    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    removed_at TIMESTAMPTZ
                )
                """
            )
            # Listings withdrawn before this list existed: recently removed
            # source listings that still point at a channel post. Posts that
            # were in fact deleted drop off on the first view.
            cur.execute(
                """
                INSERT INTO channel_manual_removals (
                    property_id,
                    published_message_id,
                    requested_at
                )
                SELECT p.id, p.published_message_id, p.deleted_from_channel_at
                FROM properties p
                WHERE p.status = 'INACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at >= NOW() - INTERVAL '3 days'
                  AND UPPER(COALESCE(p.source_kind, ''))
                      NOT IN ('MANUAL', 'CUSTOMER')
                  AND EXISTS (
                      SELECT 1
                      FROM property_sources ps
                      WHERE ps.property_id = p.id
                  )
                ON CONFLICT (property_id) DO NOTHING
                """
            )
        conn.commit()


def record_manual_removal(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channel_manual_removals (
                    property_id,
                    published_message_id
                )
                SELECT id, published_message_id
                FROM properties
                WHERE id = %s
                  AND published_message_id IS NOT NULL
                ON CONFLICT (property_id) DO UPDATE SET
                    published_message_id = EXCLUDED.published_message_id,
                    requested_at = NOW(),
                    removed_at = NULL
                """,
                (property_id,),
            )
        conn.commit()


def pending_manual_removals():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.property_id,
                    p.unit_code,
                    r.published_message_id
                FROM channel_manual_removals r
                JOIN properties p ON p.id = r.property_id
                WHERE r.removed_at IS NULL
                ORDER BY r.requested_at, r.property_id
                """
            )
            return cur.fetchall()


def mark_manual_removals_done(property_ids):
    if not property_ids:
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE channel_manual_removals
                SET removed_at = NOW()
                WHERE property_id = ANY(%s)
                  AND removed_at IS NULL
                """,
                (list(property_ids),),
            )
        conn.commit()


async def still_in_channel(rows, username, post_exists):
    """
    Drop the posts Telegram reports as gone. post_exists(username, message_id)
    returns True, False (gone) or None (could not tell; kept on the list).
    """

    if not username:
        return list(rows), []

    gate = asyncio.Semaphore(POST_CHECK_CONCURRENCY)

    async def check(row):
        async with gate:
            return await asyncio.to_thread(post_exists, username, row[2])

    results = await asyncio.gather(*(check(row) for row in rows))
    kept = [row for row, exists in zip(rows, results) if exists is not False]
    gone = [row[0] for row, exists in zip(rows, results) if exists is False]
    return kept, gone


def removed_files_view(rows, username):
    """rows: (property_id, unit_code, published_message_id)."""

    back = [
        InlineKeyboardButton(
            "🔄 بروزرسانی",
            callback_data=REMOVED_FILES_ACTION,
        ),
        InlineKeyboardButton(
            "⬅️ بازگشت به تنظیمات",
            callback_data="settings",
        ),
    ]

    if not rows:
        return (
            f"{REMOVED_FILES_TITLE}\n\n"
            "✅ فایلی برای حذف دستی نمانده؛ همهٔ فایل‌هایی که از منبع "
            "حذف شده‌اند از کانال هم پاک شده‌اند.",
            InlineKeyboardMarkup([back]),
        )

    shown = rows[:REMOVED_FILES_PAGE_SIZE]
    lines = [
        REMOVED_FILES_TITLE,
        "",
        "این فایل‌ها از کانال منبع حذف شده‌اند ولی پستشان هنوز در کانال ما "
        "هست. تلگرام اجازه نمی‌دهد ربات پست‌های قدیمی‌تر از ۴۸ ساعت را پاک "
        "کند؛ با زدن هر دکمه پست باز می‌شود تا آن را از کانال حذف کنید.",
        "ربات این فایل‌ها را از جستجو، دسته‌بندی‌ها و بازدید برداشته است.",
        "",
        f"تعداد: {len(rows)}",
    ]

    buttons = []

    for _property_id, unit_code, message_id in shown:
        url = build_channel_post_url(username, message_id)
        label = f"🗑 کد {unit_code or '-'}"
        if url:
            lines.append(f"• {label}: {url}")
            buttons.append(InlineKeyboardButton(label, url=url))
        else:
            lines.append(f"• {label}: پیام {message_id}")

    if len(rows) > len(shown):
        lines.append("")
        lines.append(
            f"… و {len(rows) - len(shown)} فایل دیگر؛ بعد از حذف این‌ها "
            "«بروزرسانی» را بزنید."
        )

    keyboard = [
        buttons[index:index + 2]
        for index in range(0, len(buttons), 2)
    ]
    keyboard.append(back)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def show_removed_files(query, post_exists):
    destination = get_destination_channel()
    username = (
        str(destination[2] or "").lstrip("@") or None
        if destination
        else None
    )

    rows = await asyncio.to_thread(pending_manual_removals)
    kept, gone = await still_in_channel(rows, username, post_exists)
    await asyncio.to_thread(mark_manual_removals_done, gone)

    text, keyboard = removed_files_view(kept, username)
    try:
        await query.edit_message_text(
            text[:4000],
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        # «بروزرسانی» with nothing changed.
        if "message is not modified" not in str(exc).lower():
            raise
