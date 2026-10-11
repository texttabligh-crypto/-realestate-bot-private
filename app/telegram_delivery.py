import logging
import asyncio

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
DEFAULT_TIMEOUTS = {
    "read_timeout": 60,
    "write_timeout": 60,
    "connect_timeout": 30,
    "pool_timeout": 30,
}


def limit_telegram_text(text, limit):
    """Limit text by Telegram's UTF-16 character counting."""
    text = str(text or "")
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text

    suffix = "\n…"
    budget = limit - len(suffix.encode("utf-16-le")) // 2
    used = 0
    result = []

    for character in text:
        width = len(character.encode("utf-16-le")) // 2
        if used + width > budget:
            break
        result.append(character)
        used += width

    return "".join(result).rstrip() + suffix


def _retry_delay(error, attempt):
    if isinstance(error, RetryAfter):
        retry_after = error.retry_after
        if hasattr(retry_after, "total_seconds"):
            retry_after = retry_after.total_seconds()
        # Telegram's advertised RetryAfter can be right on the boundary.
        # Keep a small safety margin so the retry does not immediately hit
        # the same flood-control window again.
        return max(float(retry_after), 0.1) + 1.5
    return 0.75 * (2 ** attempt)


def is_message_not_modified(error):
    """Return whether Telegram rejected an edit because it was idempotent."""
    return (
        isinstance(error, BadRequest)
        and "message is not modified" in str(error).lower()
    )


async def edit_published_message(
    bot,
    *,
    chat_id,
    message_id,
    text,
    has_photo,
    property_id=None,
    attempts=5,
):
    """Edit a channel post safely and retry only transient failures.

    Telegram reports an idempotent edit as HTTP 400.  That response means the
    requested final state is already present, so callers must treat it as a
    successful no-op rather than marking the publication as failed.
    """
    reply_markup = None

    if property_id is not None:
        try:
            from visit_leads import (
                build_visit_keyboard_for_bot,
            )

            reply_markup = (
                await build_visit_keyboard_for_bot(
                    bot,
                    property_id,
                )
            )

        except Exception:
            # A visit-button problem must never block
            # the property text refresh itself.
            reply_markup = None

    edit_kwargs = dict(
        DEFAULT_TIMEOUTS
    )

    if reply_markup is not None:
        edit_kwargs[
            "reply_markup"
        ] = reply_markup

    for attempt in range(attempts):
        try:
            if has_photo:
                return await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=limit_telegram_text(text, CAPTION_LIMIT),
                    **edit_kwargs,
                )

            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=limit_telegram_text(text, MESSAGE_LIMIT),
                disable_web_page_preview=True,
                **edit_kwargs,
            )
        except BadRequest as error:
            if is_message_not_modified(error):
                return None
            raise
        except (TimedOut, NetworkError, RetryAfter) as error:
            if attempt + 1 >= attempts:
                raise
            await asyncio.sleep(_retry_delay(error, attempt))
