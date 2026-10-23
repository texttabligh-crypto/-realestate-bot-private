"""
Phase 0 of the Bale (بله) bot: can this server use Bale's bot API through
python-telegram-bot, and do the calls the Telegram bot relies on behave the
same there?

Runs once at start-up, only when BALE_BOT_TOKEN is set. With
BALE_TEST_CHAT_ID (a test channel where the Bale bot is an admin) it also
sends, edits and deletes a few test posts there. Every result is logged as
«BALE_CHECK …» for the diagnostics workflow. Nothing here touches Telegram
or the database.
"""

import io
import logging
import os

from PIL import Image
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.request import HTTPXRequest

BALE_BASE_URL = "https://tapi.bale.ai/bot"
BALE_FILE_URL = "https://tapi.bale.ai/file/bot"


def bale_bot(token):
    """A python-telegram-bot Bot that talks to Bale instead of Telegram."""

    return Bot(
        token,
        base_url=BALE_BASE_URL,
        base_file_url=BALE_FILE_URL,
        request=HTTPXRequest(connect_timeout=15, read_timeout=30, write_timeout=30),
    )


def _photo(color):
    data = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(data, "JPEG")
    return data.getvalue()


def _chat(value):
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


async def _step(name, call):
    try:
        result = await call
    except Exception as exc:
        logging.warning("BALE_CHECK %s failed: %s: %s", name, type(exc).__name__, exc)
        return None
    logging.info("BALE_CHECK %s ok", name)
    return result


async def _post_checks(bot, chat_id):
    sent = []
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("دکمهٔ تست", callback_data="bale_check")]])
    message = await _step("sendMessage", bot.send_message(chat_id, "🧪 تست اتصال ربات املاک به بله", reply_markup=keyboard))
    if message:
        sent.append(message)
        await _step("editMessageText", message.edit_text("🧪 تست اتصال (ویرایش‌شده)"))
    photo = await _step("sendPhoto", bot.send_photo(chat_id, _photo((176, 18, 38)), caption="🧪 عکس تست"))
    if photo:
        sent.append(photo)
        await _step("editMessageCaption", photo.edit_caption("🧪 عکس تست (کپشن ویرایش‌شده)"))
        if photo.photo:
            await _step("getFile", bot.get_file(photo.photo[-1].file_id))
    album = await _step("sendMediaGroup", bot.send_media_group(chat_id, [
        InputMediaPhoto(_photo((222, 38, 56)), caption="🧪 آلبوم تست"),
        InputMediaPhoto(_photo((90, 120, 160))),
    ]))
    sent.extend(album or ())
    document = await _step("sendDocument", bot.send_document(chat_id, io.BytesIO(_photo((20, 20, 20))), filename="story-test.jpg"))
    if document:
        sent.append(document)
    for item in sent:
        await _step(f"deleteMessage:{item.message_id}", item.delete())


async def run_bale_check():
    token = (os.getenv("BALE_BOT_TOKEN") or "").strip()
    if not token:
        return
    bot = bale_bot(token)
    try:
        await bot.initialize()
    except Exception as exc:
        logging.warning("BALE_CHECK getMe failed: %s: %s", type(exc).__name__, exc)
        return
    try:
        logging.info("BALE_CHECK getMe ok: username=%s id=%s", bot.username, bot.id)
        chat_id = os.getenv("BALE_TEST_CHAT_ID") or ""
        if chat_id.strip():
            await _post_checks(bot, _chat(chat_id))
        else:
            logging.info("BALE_CHECK posting checks skipped: BALE_TEST_CHAT_ID is not set")
        logging.info("BALE_CHECK done")
    except Exception:
        logging.exception("BALE_CHECK stopped")
    finally:
        try:
            await bot.shutdown()
        except Exception:
            pass
