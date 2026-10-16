"""A customer asks about a listing by its code and is offered a visit.

«کد ۳۵۱۳», «۳۵۱۳» or a story's short code «۳۵۱» (stories ask viewers to
comment the listing's sequence number) gets the listing's photo, facts,
price and channel link, then «می‌خواید براتون یه بازدید هماهنگ کنم؟» with
a button. «آره» / «حتماً» or the button files a visit request the same way
the channel post's visit link does: the phone is asked for when missing and
the visit operators call the customer to set the time.
"""

import logging
import re
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import build_channel_post_url, clean_publish_location, db_connect, get_destination_channel

LISTING_OFFER_STATE = "VISIT_OFFER"
VISIT_CALLBACK_PREFIX = "sc_offer_visit_"
SIMILAR_CALLBACK_PREFIX = "sc_offer_similar_"

_LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_PERSIAN_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")

# «کد ۳۵۱۳», «کد واحد: 3513», «فایل ۳۵۱».
_LABELLED_CODE = re.compile(
    r"(?:کد|کُد|code|فایل|واحد)\s*(?:واحد|فایل)?\s*[:#\-]?\s*#?(\d{3,7})(?!\d)",
    re.IGNORECASE,
)
# A number right before a unit is a measure, not a code: «واحد ۱۲۰ متری».
_MEASURE_AFTER = re.compile(r"\s*(?:متر|میلیارد|ملیارد|میلیون|ملیون|تومان|خواب|ساله)")
# A bare number among these words is a budget, an area or a year.
_SEARCH_WORDS = re.compile(
    r"متر|میلیارد|ملیارد|میلیون|ملیون|تومان|خواب|طبقه|سال|ساخت|پلاک|بودجه|حدود|زیر|بالای|الی"
    r"|(?:^|\s)تا(?:\s|$)"
)

_YES = re.compile(
    r"^(?:آره|اره|آری|بله|بلی|حتما|حتماً|اوکی|اوکیه|باشه|چشم|لطفا|لطفاً|موافقم|ok|okay|yes|ye)"
    r"(?:\s|$)|هماهنگ\s*(?:کن|کنید|بشه)|بازدید\s*(?:می|می‌|)\s*خوام|میخوام|می\s*خوام",
    re.IGNORECASE,
)
_NO = re.compile(r"^(?:نه|نخیر|خیر|نمیخوام|نمی\s*خوام|فعلا\s*نه|فعلاً\s*نه|بعدا|بعداً|no)(?:\s|$)", re.IGNORECASE)

_LISTING_KEYS = (
    "id", "unit_code", "district", "address_text", "area_m2", "bedrooms", "floor_text",
    "build_year", "direction", "has_parking", "has_elevator", "has_storage",
    "current_price_billion", "published_message_id", "channel_post_url", "photo_file_id",
)


def _clean(text):
    text = str(text or "").translate(_LATIN_DIGITS).replace("‌", " ")
    return " ".join(text.split())


def persian_digits(value):
    return str(value).translate(_PERSIAN_DIGITS)


def parse_listing_code(text):
    """
    (code, labelled) for a short message asking about one listing, else None.
    labelled is True when the customer said «کد» / «فایل» / «واحد».
    """

    raw = _clean(text)
    if not raw or len(raw) > 60 or "بازدید" in raw:
        return None
    labelled = _LABELLED_CODE.search(raw)
    if labelled and not _MEASURE_AFTER.match(raw, labelled.end()):
        return labelled.group(1), True
    if _SEARCH_WORDS.search(raw):
        return None
    numbers = re.findall(r"(?<!\d)\d+(?!\d)", raw)
    # Two-digit numbers and 100 are budgets («۱۲» means about 12 billion).
    if len(numbers) != 1 or not 3 <= len(numbers[0]) <= 7 or int(numbers[0]) <= 100:
        return None
    # Only a few words around it: «۳۵۱», «مشخصات ۳۵۱ لطفا», «قیمت ۳۵۱ چنده؟».
    words = re.sub(r"[^\w\s]|\d", " ", raw).split()
    return (numbers[0], False) if len(words) <= 4 else None


def offer_reply(text):
    """'yes' / 'no' for an answer to the visit offer, else None."""

    raw = _clean(text).strip(" .!؟?،,")
    if not raw or len(raw) > 40:
        return None
    if _NO.search(raw):
        return "no"
    if _YES.search(raw):
        return "yes"
    return None


def find_listing_by_code(code):
    """The live listing with this unit code, or this sequence number (a story's short code)."""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id, p.unit_code, p.district, p.address_text, p.area_m2, p.bedrooms,
                    p.floor_text, p.build_year, p.direction, p.has_parking, p.has_elevator,
                    p.has_storage, p.current_price_billion, p.published_message_id,
                    p.channel_post_url,
                    (
                        SELECT pm.telegram_file_id
                        FROM property_media pm
                        WHERE pm.property_id = p.id
                          AND pm.media_type = 'PHOTO'
                          AND pm.telegram_file_id IS NOT NULL
                        ORDER BY pm.position, pm.id
                        LIMIT 1
                    )
                FROM properties p
                WHERE (p.unit_code = %s OR p.internal_sequence::text = %s)
                  AND p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM property_trash pt WHERE pt.property_id = p.id
                  )
                ORDER BY (p.unit_code = %s) DESC
                LIMIT 1
                """,
                (code, code, code),
            )
            row = cur.fetchone()
    return dict(zip(_LISTING_KEYS, row)) if row else None


def find_listing_by_id(property_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT unit_code FROM properties WHERE id = %s AND status = 'ACTIVE'",
                (property_id,),
            )
            row = cur.fetchone()
    return find_listing_by_code(row[0]) if row and row[0] else None


def format_billion(value):
    return persian_digits(f"{Decimal(str(value)).normalize():f}")


def channel_username():
    try:
        destination = get_destination_channel()
    except Exception:
        logging.exception("Listing offer: destination channel unavailable")
        return None
    return destination[2] if destination else None


def listing_offer_text(listing, username):
    lines = [f"اینم مشخصات فایل کد {persian_digits(listing['unit_code'])} 😊", ""]
    location = clean_publish_location(listing.get("address_text") or listing.get("district"))
    if location:
        lines.append(f"📍 {location}")

    facts = []
    if listing.get("area_m2"):
        facts.append(f"📐 {persian_digits(int(Decimal(str(listing['area_m2']))))} متر")
    if listing.get("bedrooms") is not None:
        facts.append(f"🛏 {persian_digits(listing['bedrooms'])} خواب")
    if listing.get("floor_text"):
        facts.append(f"🏢 طبقه {persian_digits(_clean(listing['floor_text']))}")
    if facts:
        lines.append(" | ".join(facts))

    more = []
    if listing.get("build_year"):
        more.append(f"📅 ساخت {persian_digits(listing['build_year'])}")
    if listing.get("direction"):
        more.append(f"🧭 {_clean(listing['direction'])}")
    if more:
        lines.append(" | ".join(more))

    amenities = [
        label
        for key, label in (("has_elevator", "آسانسور"), ("has_parking", "پارکینگ"), ("has_storage", "انباری"))
        if listing.get(key) is True
    ]
    if amenities:
        lines.append("✅ " + " • ".join(amenities))
    if listing.get("current_price_billion"):
        lines.append(f"💰 قیمت: {format_billion(listing['current_price_billion'])} میلیارد تومان")

    link = (
        build_channel_post_url(username, listing.get("published_message_id"))
        or listing.get("channel_post_url")
    )
    if link:
        lines.append(f"🔗 مشاهده در کانال: {link}")
    lines.extend(["", "می‌خواید براتون یه بازدید هماهنگ کنم؟ 🏡"])
    return "\n".join(lines)


def listing_offer_keyboard(property_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ آره، بازدید می‌خوام", callback_data=f"{VISIT_CALLBACK_PREFIX}{property_id}")],
        [InlineKeyboardButton("🔍 فایل‌های مشابه", callback_data=f"{SIMILAR_CALLBACK_PREFIX}{property_id}")],
    ])


async def send_listing_offer(message, listing, username):
    """The listing's first photo with its facts as the caption, and the visit offer."""

    text = listing_offer_text(listing, username)
    markup = listing_offer_keyboard(listing["id"])
    if listing.get("photo_file_id"):
        try:
            await message.reply_photo(listing["photo_file_id"], caption=text[:1024], reply_markup=markup)
            return
        except Exception:
            logging.warning("Listing offer photo failed: property_id=%s", listing["id"], exc_info=True)
    await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


def callback_property_id(action, prefix):
    try:
        return int(action[len(prefix):])
    except ValueError:
        return None
