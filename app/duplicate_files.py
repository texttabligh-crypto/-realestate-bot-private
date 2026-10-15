"""Files published twice in our channel from the same source post.

Settings › «🔁 فایل‌های تکراری» lists each pair with both post links and
marks which copy to keep (the one the bot tracks the source post with) and
which to remove by hand; Telegram does not let the bot delete posts older
than 48 hours. A pair drops off once either post is gone from the channel.
"""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import (
    build_channel_post_url,
    db_connect,
    get_destination_channel,
)
from removed_files import still_in_channel

DUPLICATE_FILES_ACTION = "duplicate_files"
DUPLICATE_FILES_PERMISSION = "duplicate_files_view"
DUPLICATE_FILES_TITLE = "🔁 فایل‌های تکراری"
DUPLICATE_FILES_PAGE_SIZE = 20


def duplicate_listing_pairs():
    """
    Live published listings that come from the same source post, as
    (keep_code, keep_message_id, remove_code, remove_message_id).
    """

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH live AS (
                    SELECT
                        id,
                        unit_code,
                        published_message_id,
                        input_channel_id,
                        input_message_id
                    FROM properties
                    WHERE status = 'ACTIVE'
                      AND published_message_id IS NOT NULL
                      AND deleted_from_channel_at IS NULL
                ),
                posts AS (
                    SELECT
                        ps.property_id,
                        ps.source_channel_id AS channel_id,
                        ps.source_message_id AS message_id,
                        TRUE AS holds_row
                    FROM property_sources ps
                    JOIN live ON live.id = ps.property_id
                    WHERE ps.source_message_id IS NOT NULL

                    UNION ALL

                    SELECT
                        live.id,
                        c.id,
                        live.input_message_id,
                        FALSE
                    FROM live
                    JOIN channels c
                      ON c.telegram_channel_id = live.input_channel_id
                     AND c.channel_type = 'SOURCE'
                    WHERE live.input_message_id IS NOT NULL
                ),
                pairs AS (
                    SELECT
                        a.property_id AS a_id,
                        b.property_id AS b_id,
                        BOOL_OR(a.holds_row) AS a_holds,
                        BOOL_OR(b.holds_row) AS b_holds
                    FROM posts a
                    JOIN posts b
                      ON b.channel_id = a.channel_id
                     AND b.message_id = a.message_id
                     AND b.property_id > a.property_id
                    GROUP BY a.property_id, b.property_id
                )
                SELECT
                    la.unit_code,
                    la.published_message_id,
                    pairs.a_holds,
                    lb.unit_code,
                    lb.published_message_id,
                    pairs.b_holds
                FROM pairs
                JOIN live la ON la.id = pairs.a_id
                JOIN live lb ON lb.id = pairs.b_id
                ORDER BY pairs.a_id, pairs.b_id
                """
            )
            rows = cur.fetchall()

    pairs = []

    for a_code, a_message, a_holds, b_code, b_message, b_holds in rows:
        # Keep the copy that tracks the source post; otherwise the older one.
        if b_holds and not a_holds:
            pairs.append((b_code, b_message, a_code, a_message))
        else:
            pairs.append((a_code, a_message, b_code, b_message))

    return pairs


async def pairs_still_in_channel(pairs, username, post_exists):
    """Drop pairs where either post is already gone from the channel."""

    message_ids = sorted(
        {pair[1] for pair in pairs} | {pair[3] for pair in pairs}
    )
    _kept, gone = await still_in_channel(
        [(message_id, None, message_id) for message_id in message_ids],
        username,
        post_exists,
    )
    gone = set(gone)
    return [
        pair for pair in pairs
        if pair[1] not in gone and pair[3] not in gone
    ]


def duplicate_files_view(pairs, username):
    """pairs: (keep_code, keep_message_id, remove_code, remove_message_id)."""

    back = [
        InlineKeyboardButton(
            "🔄 بروزرسانی",
            callback_data=DUPLICATE_FILES_ACTION,
        ),
        InlineKeyboardButton(
            "⬅️ بازگشت به تنظیمات",
            callback_data="settings",
        ),
    ]

    if not pairs:
        return (
            f"{DUPLICATE_FILES_TITLE}\n\n"
            "✅ فایل تکراری در کانال نیست.",
            InlineKeyboardMarkup([back]),
        )

    def link(message_id):
        return build_channel_post_url(username, message_id) or f"پیام {message_id}"

    shown = pairs[:DUPLICATE_FILES_PAGE_SIZE]
    lines = [
        DUPLICATE_FILES_TITLE,
        "",
        "این فایل‌ها دو بار در کانال منتشر شده‌اند (هر دو از یک پست منبع). "
        "نسخهٔ ✅ را نگه دارید و نسخهٔ 🗑 را از کانال حذف کنید؛ با زدن هر "
        "دکمه همان پست باز می‌شود.",
        "",
        f"تعداد: {len(pairs)}",
    ]

    buttons = []

    for keep_code, keep_message, remove_code, remove_message in shown:
        lines.append("")
        lines.append(f"✅ کد {keep_code or '-'}: {link(keep_message)}")
        lines.append(f"🗑 کد {remove_code or '-'}: {link(remove_message)}")
        url = build_channel_post_url(username, remove_message)
        if url:
            buttons.append(
                InlineKeyboardButton(f"🗑 کد {remove_code or '-'}", url=url)
            )

    if len(pairs) > len(shown):
        lines.append("")
        lines.append(
            f"… و {len(pairs) - len(shown)} جفت دیگر؛ بعد از حذف این‌ها "
            "«بروزرسانی» را بزنید."
        )

    keyboard = [
        buttons[index:index + 2]
        for index in range(0, len(buttons), 2)
    ]
    keyboard.append(back)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def show_duplicate_files(query, post_exists):
    destination = get_destination_channel()
    username = (
        str(destination[2] or "").lstrip("@") or None
        if destination
        else None
    )

    pairs = await asyncio.to_thread(duplicate_listing_pairs)
    pairs = await pairs_still_in_channel(pairs, username, post_exists)

    text, keyboard = duplicate_files_view(pairs, username)
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
