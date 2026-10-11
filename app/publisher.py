import re
import logging
import os
import psycopg

from ingest import assign_unit_code_on_publish
from telegram_delivery import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    edit_published_message,
    limit_telegram_text,
)


DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")


def db_connect():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def get_destination_channel():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telegram_channel_id, title, username
                FROM channels
                WHERE channel_type = 'DESTINATION'
                  AND is_active = TRUE
                ORDER BY id
                LIMIT 1
                """
            )

            return cur.fetchone()


def get_property_for_publish(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    unit_code,
                    district,
                    address_text,
                    area_m2,
                    bedrooms,
                    floor_text,
                    build_year,
                    has_parking,
                    has_elevator,
                    has_storage,
                    direction,
                    current_price_toman,
                    current_price_billion,
                    price_hashtag,
                    raw_text,
                    status,
                    source_kind
                FROM properties
                WHERE id = %s
                """,
                (property_id,),
            )

            return cur.fetchone()


def get_property_media(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    telegram_file_id,
                    media_type,
                    position
                FROM property_media
                WHERE property_id = %s
                ORDER BY position, id
                """,
                (property_id,),
            )

            return cur.fetchall()


def yes_no(value, yes_text, no_text):
    if value is True:
        return yes_text

    if value is False:
        return no_text

    return None


def format_compact_number(value, max_decimals=2):
    value = float(value)

    if value.is_integer():
        return str(int(value))

    text = f"{value:.{max_decimals}f}"
    return text.rstrip("0").rstrip(".")


def format_price(price_toman):
    if price_toman is None:
        return None

    price_toman = float(price_toman)

    billion = 1_000_000_000
    million = 1_000_000

    if price_toman >= billion:
        value = price_toman / billion
        return (
            f"{format_compact_number(value, 2)} "
            "میلیارد تومان"
        )

    if price_toman >= million:
        value = price_toman / million
        return (
            f"{format_compact_number(value, 1)} "
            "میلیون تومان"
        )

    return f"{int(round(price_toman)):,} تومان"


def format_price_per_meter(
    price_toman,
    area_m2,
):
    if (
        price_toman is None
        or area_m2 is None
    ):
        return None

    try:
        price_toman = float(
            price_toman
        )
        area_m2 = float(
            area_m2
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if (
        price_toman <= 0
        or area_m2 <= 0
    ):
        return None

    price_per_meter = (
        price_toman / area_m2
    )

    # For real-estate display, round to nearest million toman.
    rounded_million = round(
        price_per_meter / 1_000_000
    )

    return (
        f"{int(rounded_million):,} "
        "میلیون تومان"
    )



def get_property_attributes(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    attribute_key,
                    attribute_label,
                    attribute_value,
                    display_order
                FROM property_attributes
                WHERE property_id = %s
                  AND is_visible = TRUE
                  AND COALESCE(attribute_value, '') <> ''
                ORDER BY display_order, id
                """,
                (property_id,),
            )

            return cur.fetchall()

def get_publish_settings():
    defaults = {
        "publish_footer": "",
        "contact_people": [],
        "contact_name": "",
        "contact_phone": "",
        "contact_telegram": "",
        "emoji_location": "📍",
        "emoji_area": "📐",
        "emoji_bedrooms": "🛏",
        "emoji_floor": "🏢",
        "emoji_build_year": "📅",
        "emoji_direction": "🧭",
        "emoji_parking": "🚗",
        "emoji_elevator": "🛗",
        "emoji_storage": "📦",
        "emoji_price": "💰",
        "emoji_unknown": "🔹",
    }

    keys = list(defaults.keys())

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT setting_key, setting_value
                FROM app_settings
                WHERE setting_key = ANY(%s)
                """,
                (keys,),
            )

            for key, value in cur.fetchall():
                if key == "contact_people":
                    defaults[key] = (
                        value
                        if isinstance(value, list)
                        else []
                    )
                elif isinstance(value, str):
                    defaults[key] = value
                elif value is not None:
                    defaults[key] = str(value)

    return defaults


def clean_publish_location(value):
    if not value:
        return value

    value = str(value).strip()

    # Location hashtags belong to source formatting and should not
    # appear in the published location line.
    value = value.replace("#", "").strip()

    value = re.sub(
        r"^\s*(?:منطقه\s*)?واحد\s*[:：]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s*\|\s*",
        " | ",
        value,
    )

    value = re.sub(
        r"\s+-\s+|\s*-\s*",
        " - ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


def build_publish_text(row, unit_code=None, dynamic_attributes_override=None):
    settings = get_publish_settings()

    (
        property_id,
        existing_unit_code,
        district,
        address_text,
        area_m2,
        bedrooms,
        floor_text,
        build_year,
        has_parking,
        has_elevator,
        has_storage,
        direction,
        current_price_toman,
        current_price_billion,
        price_hashtag,
        raw_text,
        status,
    ) = row[:17]

    source_kind = (
        row[17]
        if len(row) > 17
        else None
    )

    final_unit_code = unit_code or existing_unit_code

    lines = []

    if status == "SOLD":
        lines.append("🔴 فروخته شد")
        lines.append("")

    if price_hashtag:
        lines.append(price_hashtag)

    if final_unit_code:
        lines.append(f"کد واحد: {final_unit_code}")

    lines.append("")

    if str(source_kind or "").upper() == "MANUAL":
        clean_district = clean_publish_location(district)
        clean_address = clean_publish_location(address_text)
        location_parts = []

        for value in (clean_district, clean_address):
            if value and value not in location_parts:
                location_parts.append(value)

        location_text = " — ".join(location_parts)
    else:
        location_text = clean_publish_location(
            address_text or district
        )

    if location_text:
        lines.append(
            f"{settings['emoji_location']} {location_text}"
        )
        lines.append("")

    if area_m2 is not None:
        area = float(area_m2)

        if area.is_integer():
            area = int(area)

        lines.append(
            f"{settings['emoji_area']} {area} متر"
        )

    if bedrooms is not None:
        lines.append(
            f"{settings['emoji_bedrooms']} {bedrooms} خواب"
        )

    if floor_text:
        lines.append(
            f"{settings['emoji_floor']} طبقه {floor_text}"
        )

    if build_year:
        lines.append(
            f"{settings['emoji_build_year']} سال ساخت {build_year}"
        )

    if direction:
        lines.append(
            f"{settings['emoji_direction']} {direction}"
        )

    dynamic_attributes = (
        dynamic_attributes_override
        if dynamic_attributes_override is not None
        else get_property_attributes(property_id)
    )

    for (
        attribute_key,
        attribute_label,
        attribute_value,
        display_order,
    ) in dynamic_attributes:

        if attribute_key == "deed_type":
            lines.append(f"📄 سند {attribute_value}")

        elif attribute_key == "property_condition":
            lines.append(f"🛠 {attribute_value}")

        elif attribute_key == "open_view":
            lines.append(f"🌇 ویو {attribute_value}")

        elif attribute_key == "source_photo_authenticity":
            lines.append(f"📸 عکس‌ها {attribute_value}")

        elif attribute_key == "sale_mode":
            lines.append(f"🏗 {attribute_value}")

        # === SOURCE DETAIL PUBLISH V1 ===
        elif str(attribute_key).startswith(
            "source_detail_"
        ):
            lines.append(
                f"{settings['emoji_unknown']} "
                f"{attribute_value}"
            )

        else:
            lines.append(
                f"{settings['emoji_unknown']} "
                f"{attribute_label}: {attribute_value}"
            )

    if has_parking is True:
        lines.append(
            f"{settings['emoji_parking']} پارکینگ دارد"
        )
    elif has_parking is False:
        lines.append(
            f"{settings['emoji_parking']} پارکینگ ندارد"
        )

    if has_elevator is True:
        lines.append(
            f"{settings['emoji_elevator']} آسانسور دارد"
        )
    elif has_elevator is False:
        lines.append(
            f"{settings['emoji_elevator']} آسانسور ندارد"
        )

    if has_storage is True:
        lines.append(
            f"{settings['emoji_storage']} انباری دارد"
        )
    elif has_storage is False:
        lines.append(
            f"{settings['emoji_storage']} انباری ندارد"
        )

    price_per_meter = format_price_per_meter(
        current_price_toman,
        area_m2,
    )

    price = format_price(
        current_price_toman
    )

    if price_per_meter or price:
        lines.append("")

    if price_per_meter:
        lines.append(
            f"{settings['emoji_price']} "
            f"قیمت هر متر: {price_per_meter}"
        )

    if price:
        lines.append(
            f"{settings['emoji_price']} "
            f"قیمت کل: {price}"
        )

    footer_lines = []

    publish_footer = settings.get(
        "publish_footer",
        "",
    ).strip()

    contact_people = settings.get(
        "contact_people",
        [],
    )

    contact_name = str(settings.get(
        "contact_name",
        "",
    ) or "").strip()

    contact_phone = str(settings.get(
        "contact_phone",
        "",
    ) or "").strip()

    contact_telegram = str(settings.get(
        "contact_telegram",
        "",
    ) or "").strip()

    if publish_footer:
        footer_lines.append(publish_footer)

    normalized_people = []

    if isinstance(contact_people, list):
        for person in contact_people:
            if not isinstance(person, dict):
                continue

            name = str(
                person.get("name") or ""
            ).strip()
            phone = str(
                person.get("phone") or ""
            ).strip()

            if name or phone:
                normalized_people.append(
                    (name, phone)
                )

    # Backward compatibility: until the new combined contact setting
    # is saved, keep rendering the legacy single name/phone pair.
    if not normalized_people and (
        contact_name or contact_phone
    ):
        normalized_people.append(
            (contact_name, contact_phone)
        )

    for name, phone in normalized_people:
        if name:
            footer_lines.append(
                f"👤 {name}"
            )
        if phone:
            footer_lines.append(
                f"📞 {phone}"
            )

    if contact_telegram:
        if not contact_telegram.startswith("@"):
            contact_telegram = "@" + contact_telegram

        footer_lines.append(
            f"📱 {contact_telegram}"
        )

    if footer_lines:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.extend(footer_lines)

    return "\n".join(lines).strip()


def mark_property_published(
    property_id,
    message_id,
    channel_post_url=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    published_message_id = %s,
                    channel_post_url = %s,
                    published_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    message_id,
                    channel_post_url,
                    property_id,
                ),
            )

            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'PUBLISHED',
                    published_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                WHERE property_id = %s
                  AND queue_status <> 'PUBLISHED'
                """,
                (property_id,),
            )

            conn.commit()


def mark_publish_failed(property_id, error_text):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE publish_queue
                SET
                    queue_status = 'FAILED',
                    attempts = attempts + 1,
                    last_error = %s,
                    updated_at = NOW()
                WHERE property_id = %s
                  AND queue_status <> 'PUBLISHED'
                """,
                (
                    str(error_text)[:2000],
                    property_id,
                ),
            )

            conn.commit()


def build_channel_post_url(username, message_id):
    if not username or not message_id:
        return None

    username = username.lstrip("@")

    return f"https://t.me/{username}/{message_id}"


def get_publish_guard(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.unit_code,
                    p.published_message_id,
                    p.channel_post_url,
                    (
                        SELECT q.queue_status
                        FROM publish_queue q
                        WHERE q.property_id = p.id
                        ORDER BY q.id DESC
                        LIMIT 1
                    )
                FROM properties p
                WHERE p.id = %s
                """,
                (property_id,),
            )

            return cur.fetchone()


def record_sent_message(
    property_id,
    message_id,
    channel_post_url=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE properties
                SET
                    published_message_id = %s,
                    channel_post_url = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    message_id,
                    channel_post_url,
                    property_id,
                ),
            )

            conn.commit()


async def publish_property(bot, property_id):
    from telegram import InputMediaPhoto

    destination = get_destination_channel()

    if not destination:
        raise RuntimeError(
            "No active DESTINATION channel found"
        )

    channel_id, channel_title, channel_username = destination

    row = get_property_for_publish(property_id)

    if not row:
        raise RuntimeError(
            f"Property {property_id} not found"
        )

    if row[-1] != "ACTIVE":
        raise RuntimeError(
            f"Property {property_id} is not ACTIVE"
        )

    guard = get_publish_guard(property_id)

    if not guard:
        raise RuntimeError(
            f"Property {property_id} publish state not found"
        )

    (
        existing_unit_code,
        existing_message_id,
        existing_post_url,
        existing_queue_status,
    ) = guard

    media_rows = get_property_media(property_id)

    photos = [
        media
        for media in media_rows
        if media[1] == "PHOTO"
    ]

    # =====================================================
    # RECOVERY:
    # A Telegram post already exists.
    # If it has no unit code, create one NOW and repair
    # the existing post instead of sending a duplicate.
    # =====================================================
    if existing_message_id:
        unit_code = (
            existing_unit_code
            or assign_unit_code_on_publish(property_id)
        )

        fresh_row = get_property_for_publish(
            property_id
        )

        final_text = build_publish_text(
            fresh_row,
            unit_code=unit_code,
        )

        await edit_published_message(
            bot,
            chat_id=channel_id,
            message_id=existing_message_id,
            text=final_text,
            has_photo=bool(photos),
        )

        mark_property_published(
            property_id,
            existing_message_id,
            existing_post_url,
        )

        return {
            "property_id": property_id,
            "unit_code": unit_code,
            "message_id": existing_message_id,
            "channel_post_url": existing_post_url,
            "recovered_after_error": True,
            "repaired_missing_unit_code": (
                not bool(existing_unit_code)
            ),
        }

    # =====================================================
    # ABSOLUTE PUBLISH GATE:
    # Generate/store unit code BEFORE Telegram send.
    # Nothing may be sent without a unit code.
    # =====================================================
    unit_code = (
        existing_unit_code
        or assign_unit_code_on_publish(property_id)
    )

    if not unit_code:
        raise RuntimeError(
            "UNIT_CODE_REQUIRED: refusing Telegram publish "
            f"for property {property_id}"
        )

    fresh_row = get_property_for_publish(
        property_id
    )

    final_text = build_publish_text(
        fresh_row,
        unit_code=unit_code,
    )

    # Extra safety gate:
    # The final outgoing text itself must contain the code.
    if f"کد واحد: {unit_code}" not in final_text:
        raise RuntimeError(
            "UNIT_CODE_TEXT_GUARD_FAILED: "
            f"property {property_id}"
        )

    sent_messages = []

    try:
        if len(photos) > 1:
            media_group = []

            for index, media in enumerate(
                photos[:10]
            ):
                file_id = media[0]

                if index == 0:
                    media_group.append(
                        InputMediaPhoto(
                            media=file_id,
                            caption=limit_telegram_text(
                                final_text,
                                CAPTION_LIMIT,
                            ),
                        )
                    )
                else:
                    media_group.append(
                        InputMediaPhoto(
                            media=file_id,
                        )
                    )

            sent_messages = await bot.send_media_group(
                chat_id=channel_id,
                media=media_group,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
                pool_timeout=30,
            )

            primary_message = sent_messages[0]

        elif len(photos) == 1:
            primary_message = await bot.send_photo(
                chat_id=channel_id,
                photo=photos[0][0],
                caption=limit_telegram_text(
                    final_text,
                    CAPTION_LIMIT,
                ),
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
                pool_timeout=30,
            )

            sent_messages = [
                primary_message
            ]

        else:
            primary_message = await bot.send_message(
                chat_id=channel_id,
                text=limit_telegram_text(
                    final_text,
                    MESSAGE_LIMIT,
                ),
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
                pool_timeout=30,
            )

            sent_messages = [
                primary_message
            ]

        post_url = build_channel_post_url(
            channel_username,
            primary_message.message_id,
        )

        # Save Telegram message immediately.
        record_sent_message(
            property_id,
            primary_message.message_id,
            post_url,
        )

        mark_property_published(
            property_id,
            primary_message.message_id,
            post_url,
        )

        return {
            "property_id": property_id,
            "unit_code": unit_code,
            "message_id": primary_message.message_id,
            "channel_post_url": post_url,
        }

    except Exception as exc:
        mark_publish_failed(
            property_id,
            str(exc),
        )

        raise

async def republish_property(bot, property_id):
    """
    Republish an already-published property WITHOUT assigning
    a new unit code.

    Safety:
    1) Publish new post first.
    2) Update DB to new Telegram message.
    3) Delete old Telegram post only after new publish succeeds.
    """

    from telegram import InputMediaPhoto

    destination = get_destination_channel()

    if not destination:
        raise RuntimeError(
            "No active DESTINATION channel found"
        )

    (
        channel_id,
        channel_title,
        channel_username,
    ) = destination

    row = get_property_for_publish(
        property_id
    )

    if not row:
        raise RuntimeError(
            f"Property {property_id} not found"
        )

    unit_code = row[1]

    if not unit_code:
        raise RuntimeError(
            "This property has no unit code and "
            "cannot be republished."
        )

    guard = get_publish_guard(
        property_id
    )

    if not guard:
        raise RuntimeError(
            "Publish state not found."
        )

    (
        existing_unit_code,
        old_message_id,
        old_post_url,
        existing_queue_status,
    ) = guard

    final_text = build_publish_text(
        row,
        unit_code=unit_code,
    )

    media_rows = get_property_media(
        property_id
    )

    photos = [
        media
        for media in media_rows
        if media[1] == "PHOTO"
    ]

    # ---------------------------------------------
    # FIRST: publish replacement
    # ---------------------------------------------

    if len(photos) > 1:
        media_group = []

        for index, media in enumerate(
            photos[:10]
        ):
            file_id = media[0]

            if index == 0:
                media_group.append(
                    InputMediaPhoto(
                        media=file_id,
                        caption=limit_telegram_text(
                            final_text,
                            CAPTION_LIMIT,
                        ),
                    )
                )
            else:
                media_group.append(
                    InputMediaPhoto(
                        media=file_id,
                    )
                )

        sent_messages = await bot.send_media_group(
            chat_id=channel_id,
            media=media_group,
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )

        primary_message = sent_messages[0]

    elif len(photos) == 1:
        primary_message = await bot.send_photo(
            chat_id=channel_id,
            photo=photos[0][0],
            caption=limit_telegram_text(
                final_text,
                CAPTION_LIMIT,
            ),
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )

    else:
        primary_message = await bot.send_message(
            chat_id=channel_id,
            text=limit_telegram_text(
                final_text,
                MESSAGE_LIMIT,
            ),
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )

    new_message_id = primary_message.message_id

    post_url = build_channel_post_url(
        channel_username,
        new_message_id,
    )

    # ---------------------------------------------
    # SECOND: save replacement in DB
    # ---------------------------------------------

    mark_property_published(
        property_id,
        new_message_id,
        post_url,
    )

    # ---------------------------------------------
    # THIRD: delete old Telegram post
    # ---------------------------------------------

    old_deleted = False
    delete_warning = None

    if (
        old_message_id
        and old_message_id != new_message_id
    ):
        try:
            await bot.delete_message(
                chat_id=channel_id,
                message_id=old_message_id,
            )

            old_deleted = True

        except Exception as exc:
            delete_warning = str(exc)

    return {
        "property_id": property_id,
        "unit_code": unit_code,
        "old_message_id": old_message_id,
        "new_message_id": new_message_id,
        "channel_post_url": post_url,
        "photo_count": len(photos),
        "old_deleted": old_deleted,
        "delete_warning": delete_warning,
        "republished": True,
    }


# =========================================================



# =========================================================
# UNIVERSAL_VISIT_LINK_V1
# Final policy:
# - no separate CTA message
# - no visit inline button
# - every property always carries its visit deep-link
# =========================================================

_base_build_publish_text_visit_link = build_publish_text


def _visit_text_utf16_len(value):
    return len(
        str(value).encode("utf-16-le")
    ) // 2


def _visit_footer(property_id):
    bot_username = os.getenv(
        "BOT_USERNAME",
        "nahadmelkbot",
    ).lstrip("@")

    return (
        "🏠 برای هماهنگی بازدید این واحد "
        "روی لینک زیر کلیک کنید:\n"
        f"https://t.me/{bot_username}"
        f"?start=visit_{property_id}"
    )


def build_publish_text(
    row,
    unit_code=None,
    dynamic_attributes_override=None,
    has_photo_override=None,
):
    text = _base_build_publish_text_visit_link(
        row,
        unit_code=unit_code,
        dynamic_attributes_override=dynamic_attributes_override,
    )

    if not row:
        return text

    property_id = row[0]
    footer = _visit_footer(property_id)

    # Avoid accidental duplication.
    visit_url = footer.splitlines()[-1]

    if visit_url in text:
        return text

    has_photo = (
        bool(has_photo_override)
        if has_photo_override is not None
        else any(
            item[1] == "PHOTO"
            for item in get_property_media(property_id)
        )
    )

    limit = (
        CAPTION_LIMIT
        if has_photo
        else MESSAGE_LIMIT
    )

    separator = "\n\n"

    footer_size = _visit_text_utf16_len(
        separator + footer
    )

    budget = max(
        1,
        limit - footer_size,
    )

    base = limit_telegram_text(
        text,
        budget,
    )

    return (
        base
        + separator
        + footer
    )


# Keep the original publishers, but ensure any historical
# inline markup is removed after publish/republish.
_publish_final_visit_link_original = publish_property
_republish_final_visit_link_original = republish_property


async def _clear_property_reply_markup(
    bot,
    result,
):
    from telegram.error import BadRequest

    message_id = (
        result.get("new_message_id")
        or result.get("message_id")
    )

    if not message_id:
        return

    destination = get_destination_channel()

    if not destination:
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=destination[0],
            message_id=message_id,
            reply_markup=None,
            read_timeout=10,
            connect_timeout=3,
        )

    except BadRequest as exc:
        error = str(exc).lower()

        if (
            "message is not modified" not in error
            and "message to edit not found" not in error
            and "message not found" not in error
        ):
            raise


# === PUBLISH POST-SUCCESS CLEANUP V1 ===
async def publish_property(
    bot,
    property_id,
):
    result = await _publish_final_visit_link_original(
        bot,
        property_id,
    )

    # The property is already published at this point.
    # Reply-markup cleanup is NON-CRITICAL and must
    # never turn a successful publish into a false failure.
    try:
        await _clear_property_reply_markup(
            bot,
            result,
        )
    except Exception as exc:
        logging.warning(
            "Non-critical post-publish cleanup failed: "
            "property_id=%s error=%s",
            property_id,
            exc,
        )

    return result


async def republish_property(
    bot,
    property_id,
):
    result = await _republish_final_visit_link_original(
        bot,
        property_id,
    )

    # Same rule for republish: Telegram post success
    # must not be reported as failure because a later
    # cosmetic cleanup timed out.
    try:
        await _clear_property_reply_markup(
            bot,
            result,
        )
    except Exception as exc:
        logging.warning(
            "Non-critical post-republish cleanup failed: "
            "property_id=%s error=%s",
            property_id,
            exc,
        )

    return result
