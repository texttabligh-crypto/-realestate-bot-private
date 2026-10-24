"""
Bale (بله) Real-Estate Bot - Phase 1

Implements the real-estate bot for the Bale messenger using python-telegram-bot
against Bale's Bot API (https://tapi.bale.ai/bot).

Shares the central PostgreSQL database and features with the primary bot:
- Natural Persian search for property listings
- Catalog and category browsing with pagination
- Unit code lookup and detail inspection
- Visit request handling and lead capture (with ble.ir deep links)
- Phone contact sharing
- Management and advisor login (/login, /logout)
- Property publication to Bale destination channels
"""

import asyncio
import logging
import os
import sys

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from bot import (
    button_handler,
    category_navigation_handler,
    connect_bazdid_command,
    db_connect,
    final_private_photo_handler,
    forwarded_message_handler,
    handle_visit_source_callback,
    input_channel_post_handler,
    log_unhandled_update_error,
    login_command,
    logout,
    record_update,
    region_category_navigation_handler,
    smart_private_contact_handler,
    start,
    text_handler,
    TimedUpdateProcessor,
)
from customer_transcript import TranscriptBot
from logging_security import secure_telegram_logging
from publisher import (
    build_publish_text,
    get_property_for_publish,
    get_property_media,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
secure_telegram_logging()
log = logging.getLogger("bale-bot")

BALE_BASE_URL = os.getenv("BALE_BASE_URL", "https://tapi.bale.ai/bot")
BALE_FILE_URL = os.getenv("BALE_FILE_URL", "https://tapi.bale.ai/file/bot")
BALE_BOT_TOKEN = (os.getenv("BALE_BOT_TOKEN") or "").strip()
BALE_BOT_USERNAME = (os.getenv("BALE_BOT_USERNAME") or "").strip()
BALE_CHANNEL_ID = (
    os.getenv("BALE_DESTINATION_CHANNEL_ID")
    or os.getenv("BALE_CHANNEL_ID")
    or ""
).strip()


def ensure_bale_schema():
    """Ensure platform columns exist in shared tables for platform awareness."""
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    ALTER TABLE customer_profiles
                    ADD COLUMN IF NOT EXISTS platform VARCHAR(32) NOT NULL DEFAULT 'telegram';

                    ALTER TABLE user_sessions
                    ADD COLUMN IF NOT EXISTS platform VARCHAR(32) NOT NULL DEFAULT 'telegram';

                    ALTER TABLE channels
                    ADD COLUMN IF NOT EXISTS platform VARCHAR(32) NOT NULL DEFAULT 'telegram';

                    CREATE INDEX IF NOT EXISTS idx_customer_profiles_platform
                    ON customer_profiles(platform);

                    CREATE INDEX IF NOT EXISTS idx_user_sessions_platform
                    ON user_sessions(platform);
                    """
                )
            conn.commit()
            log.info("Bale database schema verified.")
    except Exception as exc:
        log.warning("ensure_bale_schema skipped or database unavailable: %s", exc)


def build_bale_visit_url(property_id: int, bot_username: str = None) -> str:
    """Generate a Bale deep-link URL (ble.ir) for scheduling a property visit."""
    username = (bot_username or BALE_BOT_USERNAME or "").lstrip("@")
    if not username:
        return f"https://ble.ir/?start=visit_{int(property_id)}"
    return f"https://ble.ir/{username}?start=visit_{int(property_id)}"


def build_bale_visit_keyboard(
    property_id: int, bot_username: str = None
) -> InlineKeyboardMarkup:
    """Build an inline keyboard with a Bale deep-link for visiting a listing."""
    url = build_bale_visit_url(property_id, bot_username)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 بازدید از این واحد می‌خوام", url=url)]]
    )


async def publish_to_bale_channel(
    bot,
    property_id: int,
    channel_id: str = None,
    bot_username: str = None,
):
    """
    Publish a listing to a Bale destination channel.

    Uses single cover photo or text message with a Bale visit button.
    Bale's Bot API is most reliable with send_photo / send_message.
    """
    target_channel = channel_id or BALE_CHANNEL_ID
    if not target_channel:
        raise RuntimeError("No Bale destination channel configured")

    row = get_property_for_publish(property_id)
    if not row:
        raise RuntimeError(f"Property {property_id} not found")

    unit_code = row[1] or str(property_id)
    text = build_publish_text(row, unit_code=unit_code)
    keyboard = build_bale_visit_keyboard(property_id, bot_username)

    media_rows = get_property_media(property_id)
    photos = [m for m in media_rows if m[1] == "PHOTO"]

    # If photo exists and we have raw photo bytes or an accessible URL, send photo.
    # Otherwise send styled text with the visit button.
    if photos and photos[0][0]:
        file_id_or_path = photos[0][0]
        # In Bale, if file_id is local or URL, send_photo works.
        try:
            return await bot.send_photo(
                chat_id=target_channel,
                photo=file_id_or_path,
                caption=text[:1024],
                reply_markup=keyboard,
            )
        except Exception as exc:
            log.warning(
                "Bale send_photo failed (%s), falling back to send_message", exc
            )

    return await bot.send_message(
        chat_id=target_channel,
        text=text,
        reply_markup=keyboard,
    )


async def bale_post_init(application: Application):
    """Post-initialization for Bale Bot."""
    commands = [
        BotCommand("start", "شروع ربات و جست‌وجوی ملک"),
        BotCommand("login", "ورود به حساب کاربری"),
        BotCommand("logout", "خروج از حساب کاربری"),
    ]

    try:
        await application.bot.set_my_commands(commands)
    except Exception as exc:
        log.warning("Could not set Bale bot commands: %s", exc)

    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )
    except Exception as exc:
        log.warning("Could not set Bale menu button: %s", exc)

    ensure_bale_schema()

    try:
        me = await application.bot.get_me()
        log.info(
            "Bale Bot initialized successfully. Username: @%s (id: %s)",
            me.username,
            me.id,
        )
    except Exception as exc:
        log.info("Bale Bot initialized (getMe notice: %s)", exc)


class BaleTranscriptBot(TranscriptBot):
    """
    ExtBot adapted for Bale's Bot API (https://tapi.bale.ai/bot).

    Handles compatibility differences between Telegram Bot API v7 and Bale Bot API:
    - Bale does not implement setChatMenuButton (returns Handler not found).
    - Bale does not support Telegram 7.0+ reply_parameters (causes permission_denied or 400).
    - Bale does not support link_preview_options (uses disable_web_page_preview).
    - Bale does not support business_connection_id, message_effect_id, etc.
    """

    async def set_chat_menu_button(self, *args, **kwargs):
        # Bale API does not support setChatMenuButton. We absorb it so /start never crashes.
        return True

    async def _do_post(self, endpoint: str, data: dict, **kwargs):
        # Adapt Telegram Bot API v7 payload for Bale compatibility
        # 1. Convert/strip reply_parameters
        if "reply_parameters" in data:
            rp = data.pop("reply_parameters", None)
            if rp and hasattr(rp, "message_id") and rp.message_id:
                data["reply_to_message_id"] = rp.message_id

        # 2. Convert link_preview_options to disable_web_page_preview
        if "link_preview_options" in data:
            lpo = data.pop("link_preview_options", None)
            if hasattr(lpo, "is_disabled") and lpo.is_disabled:
                data["disable_web_page_preview"] = True

        # 3. Strip Telegram-specific modern fields that Bale rejects
        for key in (
            "business_connection_id",
            "message_effect_id",
            "allow_paid_broadcast",
            "direct_messages_topic_id",
            "suggested_post_parameters",
        ):
            data.pop(key, None)

        cleaned_data = {k: v for k, v in data.items() if v is not None}

        try:
            return await super()._do_post(endpoint, cleaned_data, **kwargs)
        except Exception as exc:
            err_msg = str(exc).lower()
            # If Bale refuses reply_to_message_id with permission_denied or not found, retry as direct message
            if "reply_to_message_id" in cleaned_data and (
                "permission_denied" in err_msg
                or "not found" in err_msg
                or "forbidden" in err_msg
            ):
                log.warning("Bale refused reply_to_message_id (%s); retrying without reply", exc)
                cleaned_data.pop("reply_to_message_id", None)
                return await super()._do_post(endpoint, cleaned_data, **kwargs)
            raise


def create_bale_application(
    token: str = None,
    base_url: str = None,
    file_url: str = None,
) -> Application:
    """Build and configure the Application instance for Bale."""
    bot_token = token or BALE_BOT_TOKEN
    if not bot_token:
        raise ValueError("BALE_BOT_TOKEN is required to create Bale application")

    bot_base_url = base_url or BALE_BASE_URL
    bot_file_url = file_url or BALE_FILE_URL

    request = HTTPXRequest(
        connect_timeout=15,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=10,
    )

    bale_bot_instance = BaleTranscriptBot(
        token=bot_token,
        base_url=bot_base_url,
        base_file_url=bot_file_url,
        request=request,
    )

    app = (
        Application.builder()
        .bot(bale_bot_instance)
        .post_init(bale_post_init)
        .concurrent_updates(TimedUpdateProcessor(1, db_probe=db_connect))
        .build()
    )

    # Core error logging
    app.add_error_handler(log_unhandled_update_error)

    # Metrics and activity recording
    app.add_handler(TypeHandler(Update, record_update), group=100)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("connect_bazdid", connect_bazdid_command))

    # Contact sharing (lead phone number)
    app.add_handler(
        MessageHandler(
            filters.CONTACT & filters.ChatType.PRIVATE,
            smart_private_contact_handler,
        ),
        group=-3,
    )

    # Category and catalog browsing
    app.add_handler(
        CallbackQueryHandler(
            category_navigation_handler,
            pattern=r"^catnav_",
        ),
        group=-2,
    )
    app.add_handler(
        CallbackQueryHandler(
            region_category_navigation_handler,
            pattern=r"^regcat_",
        ),
        group=-2,
    )

    # Visit request handling
    app.add_handler(
        CallbackQueryHandler(
            handle_visit_source_callback,
            pattern=r"^visitsrc_",
        ),
        group=-2,
    )

    # All interactive buttons (smart filters, CRM, details)
    app.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=r"^(?!catnav_|regcat_|visitsrc_)",
        ),
        group=0,
    )

    # Input channels / messages
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS,
            input_channel_post_handler,
        ),
        group=-1,
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO & ~filters.FORWARDED,
            final_private_photo_handler,
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(
            filters.FORWARDED,
            forwarded_message_handler,
        ),
        group=0,
    )

    # Smart natural search & customer conversation
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        ),
        group=1,
    )

    return app


async def _standby_loop():
    """Keep the service alive gracefully when no token is configured yet."""
    log.info(
        "BALE_BOT_TOKEN is not configured. Bale bot is standing by and waiting for configuration."
    )
    while True:
        await asyncio.sleep(3600)


def main():
    token = (os.getenv("BALE_BOT_TOKEN") or "").strip()
    if not token:
        try:
            asyncio.run(_standby_loop())
        except (KeyboardInterrupt, SystemExit):
            pass
        return

    log.info("Starting Bale RealEstate Bot (Phase 1)...")
    app = create_bale_application(token)
    app.run_polling()


if __name__ == "__main__":
    main()
