"""Instagram stories for random live listings.

Settings › «📸 استوری اینستاگرام» asks how many stories to make and for which
price range, picks that many random live listings with photos in the range,
and sends one 1080×1920 story per listing, rotating through four layouts.
Each story is built from the listing's real photos and own facts: a red band
with a factual title (no slogan), tiles for the other facts and checked
amenities, with the photos filling whatever room is left. OpenAI only writes
the Instagram caption (a factual template is used when it is not available). The price is left out
on purpose: the call to action asks viewers to comment the short code. No
source text, agency name, phone number or channel handle is ever drawn, and
photos are never generated or altered.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import db_connect

INSTAGRAM_STORY_ACTION = "insta_story"
INSTAGRAM_STORY_COUNT_PREFIX = "insta_story_n_"
INSTAGRAM_STORY_ALL_PRICES = "insta_story_all"
INSTAGRAM_STORY_COUNT_STATE = "insta_story_count"
INSTAGRAM_STORY_PRICE_STATE = "insta_story_price"
INSTAGRAM_STORY_STATES = (INSTAGRAM_STORY_COUNT_STATE, INSTAGRAM_STORY_PRICE_STATE)
INSTAGRAM_STORY_PERMISSION = "instagram_story"
INSTAGRAM_STORY_TITLE = "📸 استوری اینستاگرام"

STORY_SIZE = (1080, 1920)
STORY_PHOTO_LIMIT = 4
STORY_MAX_COUNT = 10
STORY_COUNT_CHOICES = (1, 3, 5, 10)
STORY_SEND_SPACING_SECONDS = 1.5
# Every layout keeps its call to action here, clear of Instagram's reply bar.
# The call to action ends 260px above the bottom, clear of Instagram's
# comment field.
CTA_TOP = 1490
CTA_HEIGHT = 170

OPENAI_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "IRANSansWeb.ttf"
FONT_MEDIUM = FONT_DIR / "IRANSansWeb_Medium.ttf"
FONT_BLACK = FONT_DIR / "IRANSansWeb_Black.ttf"

BACKGROUND = (14, 24, 40)
CHIP = (32, 46, 70)
TEXT = (255, 255, 255)
MUTED = (178, 190, 208)
RED_DEEP = (176, 18, 38)
RED = (222, 38, 56)
RED_TINT = (255, 216, 220)

PERSIAN_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")
LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_PRICE_WORDS = re.compile("میلیارد|میلیون|تومان|ریال")

STYLE_HINTS = (
    "لوکس و باکلاس",
    "گرم و خانوادگی",
    "مینیمال و شیک",
    "احساسی و رویایی",
    "پرانرژی و فوری",
)


# ---------------------------------------------------------------- listings

_LISTING_KEYS = (
    "id", "unit_code", "district", "area_m2", "bedrooms", "floor_text",
    "build_year", "has_parking", "has_elevator", "has_storage",
    "internal_sequence", "direction",
)


def random_story_listings(count, price_min=None, price_max=None):
    """
    Up to count*2 random live published listings with photos, as dicts,
    priced within [price_min, price_max] billion when given.
    """

    price_sql = ""
    params = []
    if price_min is not None:
        price_sql += " AND p.current_price_billion >= %s"
        params.append(price_min)
    if price_max is not None:
        price_sql += " AND p.current_price_billion <= %s"
        params.append(price_max)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    p.id, p.unit_code, p.district, p.area_m2, p.bedrooms,
                    p.floor_text, p.build_year, p.has_parking, p.has_elevator,
                    p.has_storage, p.internal_sequence, p.direction
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.unit_code IS NOT NULL
                  {price_sql}
                  AND EXISTS (
                      SELECT 1
                      FROM property_media pm
                      WHERE pm.property_id = p.id
                        AND pm.media_type = 'PHOTO'
                        AND pm.telegram_file_id IS NOT NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM property_trash pt
                      WHERE pt.property_id = p.id
                  )
                ORDER BY random()
                LIMIT %s
                """,
                (*params, int(count) * 2),
            )
            listings = [dict(zip(_LISTING_KEYS, row)) for row in cur.fetchall()]
            if not listings:
                return []

            cur.execute(
                """
                SELECT property_id, telegram_file_id
                FROM (
                    SELECT
                        property_id,
                        telegram_file_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY property_id
                            ORDER BY position, id
                        ) AS rank
                    FROM property_media
                    WHERE property_id = ANY(%s)
                      AND media_type = 'PHOTO'
                      AND telegram_file_id IS NOT NULL
                ) ranked
                WHERE rank <= %s
                ORDER BY property_id, rank
                """,
                ([item["id"] for item in listings], STORY_PHOTO_LIMIT),
            )
            photos = {}
            for property_id, file_id in cur.fetchall():
                photos.setdefault(property_id, []).append(file_id)

            # The parsed details the channel post already shows.
            cur.execute(
                """
                SELECT property_id, attribute_key, attribute_value
                FROM property_attributes
                WHERE property_id = ANY(%s)
                  AND is_visible = TRUE
                  AND COALESCE(attribute_value, '') <> ''
                ORDER BY property_id, display_order, id
                """,
                ([item["id"] for item in listings],),
            )
            details = {}
            for property_id, key, value in cur.fetchall():
                details.setdefault(property_id, []).append((key, value))

    for listing in listings:
        listing["photo_file_ids"] = photos.get(listing["id"], [])
        listing["attributes"] = details.get(listing["id"], [])
    return listings


def parse_price_range(text):
    """
    «۱۰ تا ۱۲ میلیارد» -> (10, 12); «زیر ۸» -> (None, 8); «بالای ۲۰» ->
    (20, None); a single «۱۰» means around it (±15%). None when no price.
    """

    text = str(text or "").translate(LATIN_DIGITS).replace("٫", ".").replace("/", ".")
    numbers = []
    for raw in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        if 0 < value <= 1000:
            numbers.append(value)
    if not numbers:
        return None

    if len(numbers) >= 2:
        low, high = sorted(numbers[:2])
        return low, high
    value = numbers[0]
    if re.search(r"زیر|کمتر|حداکثر|سقف|تا\s*\d", text):
        return None, value
    if re.search(r"بالا|بیشتر|حداقل|به\s*بالا", text):
        return value, None
    return (value * Decimal("0.85")).quantize(Decimal("0.01")), (value * Decimal("1.15")).quantize(Decimal("0.01"))


def price_range_label(price_range):
    if not price_range:
        return "همه قیمت‌ها"
    low, high = price_range

    def number(value):
        text = f"{value:f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return persian_digits(text)

    if low is not None and high is not None:
        return f"{number(low)} تا {number(high)} میلیارد"
    if high is not None:
        return f"تا {number(high)} میلیارد"
    return f"از {number(low)} میلیارد به بالا"


def persian_digits(value):
    return str(value).translate(PERSIAN_DIGITS)


# Parsed details shown as amenities by their own name: the stored value is
# the whole source line, which may carry other words.
FEATURE_LABELS = {
    "master_bedroom": "خواب مستر",
    "balcony": "بالکن",
    "terrace_kitchen": "آشپزخانه رو به تراس",
    "closet_room": "کلوزت روم",
    "lobby": "لابی",
    "cctv": "دوربین مداربسته",
    "luxury_installations": "فول نصبیات",
    "open_kitchen": "آشپزخانه اوپن",
    "wall_boiler": "پکیج دیواری",
    "fireplace": "شومینه",
    "cabinet_wardrobe": "کمد و کابینت",
    "large_bathroom": "حمام بزرگ",
    "wall_bed": "تخت‌خواب دیواری",
}
_UNSAFE_DETAIL = re.compile(r"@|https?:|t\.me|www\.|[0-9۰-۹٠-٩]{5,}")


def _short_detail(value, limit):
    """A stored value short and clean enough to draw, else None."""

    text = re.sub(r"\s+", " ", str(value or "")).strip(" :-•")
    if not text or len(text) > limit or _UNSAFE_DETAIL.search(text):
        return None
    return text


def _attributes(listing):
    details = {}
    for key, value in listing.get("attributes") or ():
        details.setdefault(key, value)
    return details


def story_stats(listing):
    """
    The listing's secondary facts as (value, label, text) tiles: floor,
    build year, units per floor, direction and deed. Area and bedrooms are
    in the headline.
    """

    details = _attributes(listing)
    stats = []
    floor = _short_detail(listing.get("floor_text"), 12)
    if floor:
        stats.append((persian_digits(floor), "طبقه", f"طبقه {persian_digits(floor)}"))
    if listing.get("build_year"):
        year = persian_digits(listing["build_year"])
        stats.append((year, "سال ساخت", f"ساخت {year}"))
    units = str(details.get("units_per_floor") or "").translate(LATIN_DIGITS).strip()
    if units.isdigit() and 0 < int(units) < 100:
        stats.append((persian_digits(units), "واحد در طبقه", f"{persian_digits(units)} واحد در طبقه"))
    direction = _short_detail(listing.get("direction"), 12)
    if direction:
        stats.append((direction, "جهت", direction))
    deed = _short_detail(details.get("deed_type"), 12)
    if deed:
        stats.append((deed, "سند", f"سند {deed}"))
    return stats[:4]


def story_amenities(listing):
    """What the home has, each as a short label: elevator, parking, lobby…"""

    details = _attributes(listing)
    items = [
        label
        for key, label in (
            ("has_elevator", "آسانسور"),
            ("has_parking", "پارکینگ"),
            ("has_storage", "انباری"),
        )
        if listing.get(key) is True
    ]
    condition = _short_detail(details.get("property_condition"), 22)
    if condition:
        items.append(condition)
    view = _short_detail(details.get("open_view"), 10)
    if view:
        items.append(f"ویو {view}")
    if details.get("installations") == "کامل":
        items.append("فول نصبیات")
    sale_mode = _short_detail(details.get("sale_mode"), 12)
    if sale_mode:
        items.append(sale_mode)
    items.extend(label for key, label in FEATURE_LABELS.items() if key in details)
    return list(dict.fromkeys(items))


def story_facts(listing):
    """All the listing's own facts, for the caption; never the price."""

    facts = []
    if listing.get("district"):
        facts.append(str(listing["district"]).strip())
    if listing.get("area_m2"):
        facts.append(f"{persian_digits(int(Decimal(str(listing['area_m2']))))} متر")
    if listing.get("bedrooms") is not None:
        facts.append(f"{persian_digits(listing['bedrooms'])} خواب")
    facts.extend(text for _value, _label, text in story_stats(listing))
    facts.extend(story_amenities(listing))
    return facts


# A Persian comma: a middle dot reads as the Persian zero «۰» («۳۰ خواب»).
TITLE_SEPARATOR = "، "


def story_titles(listing):
    """
    The story's tag and headline, from the listing's facts only (no slogan):
    the district as the tag and «۱۵۱ متر، ۳ خواب» as the headline.
    """

    district = str(listing.get("district") or "").strip()
    parts = []
    if listing.get("area_m2"):
        parts.append(f"{persian_digits(int(Decimal(str(listing['area_m2']))))} متر")
    if listing.get("bedrooms") is not None:
        parts.append(f"{persian_digits(listing['bedrooms'])} خواب")
    if parts:
        return district or "تبریز", TITLE_SEPARATOR.join(parts)
    return "تبریز", district or "فایل ویژه"


def short_code(listing):
    """
    The listing's sequence number: the first digits of its unit code (22537
    -> 225), unique per listing and short enough to comment.
    """

    sequence = listing.get("internal_sequence")
    if sequence:
        return str(sequence)
    return str(listing["unit_code"])[:3]


def call_to_action(listing):
    code = persian_digits(short_code(listing))
    return f"برای دریافت قیمت و مشخصات، عدد {code} رو کامنت کنید"


# ---------------------------------------------------------------- copy

def _hashtags(listing):
    tags = ["#املاک_تبریز", "#خرید_آپارتمان", "#تبریز", "#خانه_رویایی"]
    if listing.get("district"):
        tags.insert(0, "#" + re.sub(r"\s+", "_", str(listing["district"]).strip()))
    return " ".join(tags)


def fallback_caption(listing):
    tag, headline = story_titles(listing)
    stats = [text for _value, _label, text in story_stats(listing)]
    amenities = story_amenities(listing)
    facts = [f"🏡 {headline} | {tag}"]
    if stats:
        facts.append("🏢 " + " • ".join(stats))
    if amenities:
        facts.append("✅ " + " • ".join(amenities))
    return "\n\n".join(("\n".join(facts), f"💬 {call_to_action(listing)}", _hashtags(listing)))


def openai_caption(listing, rng=random):
    """The Instagram caption from OpenAI, or a factual template."""

    fallback = fallback_caption(listing)
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback

    instructions = """
You write the Instagram caption for a property story of a real-estate channel
in Tabriz, in Persian. Use ONLY the facts given; never invent features,
addresses, phone numbers, agency or person names. Never mention the price or
any amount of money: the viewer must comment the code to get the price.
Do not open with a slogan or catchphrase; start directly with the home's
facts. Write 3 to 5 short Persian lines (emoji allowed), then the call to
action exactly as given, then 5 to 7 relevant Persian hashtags on one line.
Return ONLY the caption text.
""".strip()
    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": json.dumps(
            {
                "facts": story_facts(listing),
                "style": rng.choice(STYLE_HINTS),
                "call_to_action": call_to_action(listing),
            },
            ensure_ascii=False,
        ),
        "max_output_tokens": 400,
        "store": False,
    }
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.warning("OpenAI story caption failed: %s", exc)
        return fallback

    text = ""
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                text = content["text"]

    # Keep the price out of the caption whatever the model wrote.
    lines = [line for line in str(text).splitlines() if not _PRICE_WORDS.search(line)]
    caption = "\n".join(lines).strip() or fallback
    if short_code(listing) not in caption.translate(LATIN_DIGITS):
        caption = f"{caption}\n\n💬 {call_to_action(listing)}"
    return caption


# ---------------------------------------------------------------- image

# The logo sits beside the fonts: deployment copies app/fonts into the image.
LOGO_PATH = FONT_DIR / "nimat_logo.png"
_LOGO_CACHE = {}

MARGIN = 60
BAND_PADDING = 30
TAG_LINE = 58
HEADLINE_LINE = 112
TILE_HEIGHT = 150
CHIP_HEIGHT = 70
CHIP_STEP = 86
CHECK_SPACE = 52
BLOCK_GAP = 30
# A photo fills its box only while at least this share of it stays visible;
# otherwise it is shown whole on a blurred copy of itself.
MIN_PHOTO_SHOWN = 0.6


def _font(path, size):
    # Text is shaped with arabic_reshaper/bidi first, so the basic layout
    # engine must draw it as given (raqm would reorder it a second time).
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)


def _shape(text):
    return get_display(arabic_reshaper.reshape(text))


def _wrap(text, font, max_width, max_lines):
    lines = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and font.getlength(_shape(candidate)) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:max_lines]


def _fit_font(path, text, max_width, size, minimum):
    while size > minimum:
        font = _font(path, size)
        if font.getlength(_shape(text)) <= max_width:
            return font
        size -= 2
    return _font(path, minimum)


def _soft_shadow(canvas, mask, position, blur=16, opacity=0.6, offset=(0, 8)):
    """A blurred dark copy of mask under whatever is pasted at position."""

    pad = blur * 3
    layer = Image.new("L", (mask.width + 2 * pad, mask.height + 2 * pad), 0)
    layer.paste(mask, (pad, pad))
    layer = layer.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * opacity))
    left = position[0] - pad + offset[0]
    top = position[1] - pad + offset[1]
    canvas.paste((0, 0, 0), (left, top, left + layer.width, top + layer.height), layer)


def _draw_centered(draw, text, font, box, fill):
    left, top, right, bottom = box
    shaped = _shape(text)
    bbox = font.getbbox(shaped)
    draw.text(
        (left + (right - left - font.getlength(shaped)) / 2,
         top + (bottom - top - (bbox[3] - bbox[1])) / 2 - bbox[1]),
        shaped,
        font=font,
        fill=fill,
    )


def _draw_right(draw, text, font, right, top, bottom, fill):
    _draw_centered(draw, text, font, (right - font.getlength(_shape(text)), top, right, bottom), fill)


def _gradient(size, start, end):
    """Horizontal gradient from start (left) to end (right)."""

    mask = Image.linear_gradient("L").rotate(90).resize(size)
    return Image.composite(Image.new("RGB", size, end), Image.new("RGB", size, start), mask)


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _rounded(canvas, box, fill, radius, shadow=True):
    """A rounded box in a colour or a (left, right) gradient."""

    left, top, right, bottom = box
    size = (right - left, bottom - top)
    mask = _rounded_mask(size, radius)
    if shadow:
        _soft_shadow(canvas, mask, (left, top), blur=18, opacity=0.5, offset=(0, 10))
    surface = _gradient(size, *fill) if isinstance(fill[0], tuple) else Image.new("RGB", size, fill)
    canvas.paste(surface, (left, top), mask)


def _glass(canvas, boxes, radius):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for box in boxes:
        draw.rounded_rectangle(box, radius=radius, fill=(255, 255, 255, 40),
                               outline=(255, 255, 255, 110), width=2)
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB"))


def _paste_photo(canvas, image, box):
    """
    The photo filling box, or, when that would cut away too much of it (a
    tall photo in a wide box), the whole photo on a blurred copy of itself.
    """

    left, top, right, bottom = box
    size = (right - left, bottom - top)
    box_ratio = size[0] / size[1]
    photo_ratio = image.width / image.height
    photo = ImageOps.fit(image, size, Image.LANCZOS)
    if min(box_ratio, photo_ratio) / max(box_ratio, photo_ratio) < MIN_PHOTO_SHOWN:
        photo = Image.blend(photo.filter(ImageFilter.GaussianBlur(28)),
                            Image.new("RGB", size, BACKGROUND), 0.35)
        whole = ImageOps.contain(image, size, Image.LANCZOS)
        photo.paste(whole, ((size[0] - whole.width) // 2, (size[1] - whole.height) // 2))
    canvas.paste(photo, (left, top))


def _paste_framed(canvas, image, box, border=14):
    """A photo in a white frame with a drop shadow."""

    left, top, right, bottom = box
    _soft_shadow(canvas, Image.new("L", (right - left, bottom - top), 255), (left, top),
                 blur=22, opacity=0.6, offset=(0, 16))
    canvas.paste(TEXT, box)
    _paste_photo(canvas, image, (left + border, top + border, right - border, bottom - border))


def _logo(height):
    if height not in _LOGO_CACHE:
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            logo = logo.resize((round(logo.width * height / logo.height), height), Image.LANCZOS)
        except Exception:
            logging.warning("Story logo could not be loaded")
            logo = None
        _LOGO_CACHE[height] = logo
    return _LOGO_CACHE[height]


def _paste_logo(canvas, height, left=None, top=270, center=False, plate=False):
    logo = _logo(height)
    if logo is None:
        return
    if center:
        left = (canvas.size[0] - logo.width) // 2
    if plate:
        # A dark glass plate keeps the gold logo readable on bright photos.
        box = (left - 24, top - 18, left + logo.width + 24, top + logo.height + 18)
        region = canvas.crop(box)
        darker = Image.blend(region, Image.new("RGB", region.size, (8, 12, 20)), 0.5)
        canvas.paste(darker, box[:2], _rounded_mask(region.size, 30))
    else:
        _soft_shadow(canvas, logo.getchannel("A"), (left, top), blur=16, opacity=0.8, offset=(0, 6))
    canvas.paste(logo, (left, top), logo)


def _chip_rows(chips, font, max_width, max_rows=2):
    """Chips laid out right to left, wrapping into at most max_rows rows."""

    rows = [[]]
    used = 0
    for chip in chips:
        width = font.getlength(_shape(chip)) + 56 + CHECK_SPACE
        if rows[-1] and used + width > max_width:
            if len(rows) == max_rows:
                break
            rows.append([])
            used = 0
        rows[-1].append((chip, width))
        used += width + 16
    return [row for row in rows if row]


def _info(listing, tag, headline, width):
    """Fonts, lines and heights of the title band, the tiles and the amenities."""

    text_width = width - 2 * MARGIN - 40
    tag_font = _fit_font(FONT_MEDIUM, tag, text_width, 44, 30)
    headline_font = _fit_font(FONT_BLACK, headline, text_width, 96, 76)
    lines = _wrap(headline, headline_font, text_width, 2)
    chip_font = _font(FONT_MEDIUM, 38)
    rows = _chip_rows(story_amenities(listing), chip_font, width - 2 * MARGIN)
    stats = story_stats(listing)

    band = 2 * BAND_PADDING + TAG_LINE + HEADLINE_LINE * len(lines)
    height = band
    if stats:
        height += BLOCK_GAP + TILE_HEIGHT
    if rows:
        height += BLOCK_GAP + CHIP_STEP * len(rows) - (CHIP_STEP - CHIP_HEIGHT)
    title_width = max(font.getlength(_shape(text)) for font, text in
                      [(tag_font, tag)] + [(headline_font, line) for line in lines])
    return {
        "tag": tag, "tag_font": tag_font, "lines": lines, "headline_font": headline_font,
        "stats": stats, "chip_font": chip_font, "rows": rows,
        "band": band, "title_width": title_width,
        # The block ends just above the call to action; photos fill the rest.
        "top": CTA_TOP - 40 - height,
    }


def _draw_band(canvas, info, box, radius=0):
    """The red title band: district, then «۱۵۱ متر، ۳ خواب» in big white type."""

    _rounded(canvas, box, (RED_DEEP, RED), radius)
    draw = ImageDraw.Draw(canvas)
    right = min(box[2], canvas.width) - (MARGIN if box[2] >= canvas.width else 44)
    y = box[1] + BAND_PADDING
    _draw_right(draw, info["tag"], info["tag_font"], right, y, y + TAG_LINE, RED_TINT)
    y += TAG_LINE
    for line in info["lines"]:
        _draw_right(draw, line, info["headline_font"], right, y, y + HEADLINE_LINE, TEXT)
        y += HEADLINE_LINE


def _draw_stats(canvas, stats, top, glass=False):
    """Tiles with a big value over a small label: «۱۴۰۵ / سال ساخت»."""

    gap = 16
    tile = (canvas.width - 2 * MARGIN - gap * (len(stats) - 1)) / len(stats)
    boxes = []
    for index in range(len(stats)):
        right = canvas.width - MARGIN - index * (tile + gap)
        boxes.append((int(right - tile), top, int(right), top + TILE_HEIGHT))
    if glass:
        _glass(canvas, boxes, 28)
    else:
        for box in boxes:
            _rounded(canvas, box, CHIP, 28, shadow=False)
    draw = ImageDraw.Draw(canvas)
    for (value, label, _text), (left, top_, right, _bottom) in zip(stats, boxes):
        middle = (left + right) / 2
        draw.rounded_rectangle((middle - 26, top_ + 14, middle + 26, top_ + 20), radius=3, fill=RED)
        _draw_centered(draw, value, _fit_font(FONT_BLACK, value, right - left - 28, 56, 30),
                       (left, top_ + 26, right, top_ + 94), TEXT)
        _draw_centered(draw, label, _fit_font(FONT_MEDIUM, label, right - left - 20, 30, 22),
                       (left, top_ + 96, right, top_ + 136), MUTED)


def _check(draw, center, radius, fill, mark):
    x, y = center
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
    unit = radius / 20
    draw.line([(x - 9 * unit, y + unit), (x - 3 * unit, y + 8 * unit), (x + 10 * unit, y - 7 * unit)],
              fill=mark, width=max(3, int(5 * unit)), joint="curve")


def _draw_amenities(canvas, rows, font, top, style="solid"):
    """Chips with a red check: «✓ آسانسور», «✓ پارکینگ», «✓ لابی»…"""

    chips = []
    for row in rows:
        right = canvas.width - MARGIN
        for chip, width in row:
            chips.append((chip, (int(right - width), top, int(right), top + CHIP_HEIGHT)))
            right -= width + 16
        top += CHIP_STEP
    if style == "glass":
        _glass(canvas, [box for _chip, box in chips], CHIP_HEIGHT // 2)
    draw = ImageDraw.Draw(canvas)
    for chip, (left, top_, right, bottom) in chips:
        if style == "solid":
            draw.rounded_rectangle((left, top_, right, bottom), radius=CHIP_HEIGHT // 2, fill=CHIP)
        elif style == "outline":
            draw.rounded_rectangle((left, top_, right, bottom), radius=CHIP_HEIGHT // 2, outline=TEXT, width=3)
        _check(draw, (right - 42, (top_ + bottom) / 2), 20, RED, TEXT)
        _draw_centered(draw, chip, font, (left + 32, top_, right - 76, bottom), TEXT)


def _info_block(canvas, info, band_box, radius=0, style="solid"):
    _draw_band(canvas, info, band_box, radius)
    y = band_box[3] + BLOCK_GAP
    if info["stats"]:
        _draw_stats(canvas, info["stats"], y, glass=style != "solid")
        y += TILE_HEIGHT + BLOCK_GAP
    if info["rows"]:
        _draw_amenities(canvas, info["rows"], info["chip_font"], y, style)


def _cta(canvas, listing, fill=(RED_DEEP, RED), text_fill=TEXT, code_fill=TEXT, code_text=RED_DEEP):
    """«برای دریافت قیمت و مشخصات» over «عدد [۲۲۹] رو کامنت کنید», the code boxed."""

    left, top, right, bottom = MARGIN, CTA_TOP, canvas.width - MARGIN, CTA_TOP + CTA_HEIGHT
    _rounded(canvas, (left, top, right, bottom), fill, 48)
    draw = ImageDraw.Draw(canvas)
    _draw_centered(draw, "برای دریافت قیمت و مشخصات", _font(FONT_MEDIUM, 40),
                   (left, top + 16, right, top + 72), text_fill)

    font = _font(FONT_BLACK, 56)
    code = persian_digits(short_code(listing))
    before, after = "عدد", "رو کامنت کنید"
    widths = [font.getlength(_shape(before)), font.getlength(_shape(code)) + 44,
              font.getlength(_shape(after))]
    gap = 18
    x = (left + right + sum(widths) + 2 * gap) / 2
    line_top, line_bottom = top + 80, top + 154
    _draw_centered(draw, before, font, (x - widths[0], line_top, x, line_bottom), text_fill)
    x -= widths[0] + gap
    draw.rounded_rectangle((x - widths[1], line_top, x, line_bottom), radius=18, fill=code_fill)
    _draw_centered(draw, code, font, (x - widths[1], line_top, x, line_bottom), code_text)
    x -= widths[1] + gap
    _draw_centered(draw, after, font, (x - widths[2], line_top, x, line_bottom), text_fill)


def _chevrons(canvas, top):
    """Arrows pointing at Instagram's comment field under the story."""

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    middle = canvas.width // 2
    for index, alpha in enumerate((235, 150, 80)):
        y = top + index * 30
        draw.line([(middle - 34, y), (middle, y + 24), (middle + 34, y)],
                  fill=(255, 255, 255, alpha), width=9, joint="curve")
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB"))


def _code_badge(canvas, center, radius, listing):
    """A red disc asking viewers to comment the short code."""

    box = (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
    mask = Image.new("L", (2 * radius, 2 * radius), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 2 * radius - 1, 2 * radius - 1), fill=255)
    _soft_shadow(canvas, mask, box[:2], blur=20, opacity=0.6, offset=(0, 10))
    canvas.paste(_gradient(mask.size, RED_DEEP, RED), box[:2], mask)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((box[0] + 10, box[1] + 10, box[2] - 10, box[3] - 10), outline=TEXT, width=4)
    _draw_centered(draw, "کامنت کن", _font(FONT_MEDIUM, 30),
                   (box[0], center[1] - radius * 0.62, box[2], center[1] - radius * 0.12), TEXT)
    _draw_centered(draw, persian_digits(short_code(listing)), _font(FONT_BLACK, int(radius * 0.72)),
                   (box[0], center[1] - radius * 0.1, box[2], center[1] + radius * 0.62), TEXT)


def _layout_classic(canvas, images, listing, tag, headline):
    """Main photo over a strip of small ones, a full-width red title band."""

    width = canvas.width
    info = _info(listing, tag, headline, width)
    band_top = info["top"]
    extra = images[1:4]
    if extra:
        strip, gap = 250, 10
        _paste_photo(canvas, images[0], (0, 0, width, band_top - strip - gap))
        slot = (width - gap * (len(extra) - 1)) / len(extra)
        for index, image in enumerate(extra):
            right = width - index * (slot + gap)
            _paste_photo(canvas, image, (int(right - slot), band_top - strip, int(right), band_top))
    else:
        _paste_photo(canvas, images[0], (0, 0, width, band_top))
    _paste_logo(canvas, 165, left=84, plate=True)
    _info_block(canvas, info, (0, band_top, width, band_top + info["band"]))
    _cta(canvas, listing)


def _layout_magazine(canvas, images, listing, tag, headline):
    """Blurred backdrop, the logo, a framed photo, a red title card, white CTA."""

    width, height = canvas.size
    backdrop = ImageOps.fit(images[0], (width, height), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
    canvas.paste(Image.blend(backdrop, Image.new("RGB", (width, height), BACKGROUND), 0.6))
    info = _info(listing, tag, headline, width)
    _paste_logo(canvas, 120, center=True, top=236)
    _paste_framed(canvas, images[0], (MARGIN, 390, width - MARGIN, info["top"] - 40))
    _info_block(canvas, info, (40, info["top"], width - 40, info["top"] + info["band"]),
                radius=36, style="glass")
    _cta(canvas, listing, fill=TEXT, text_fill=BACKGROUND, code_fill=RED, code_text=TEXT)


def _layout_mosaic(canvas, images, listing, tag, headline):
    """A photo grid, then the red title band."""

    width = canvas.width
    info = _info(listing, tag, headline, width)
    bottom, gap = info["top"], 10
    half = (bottom - gap) // 2
    tall = min(images, key=lambda image: image.width / image.height)
    if len(images) >= 3 and tall.width < tall.height:
        # An upright photo in a tall column beside two stacked ones.
        rest = [image for image in images if image is not tall]
        side = width - int(width * 0.58) - gap
        _paste_photo(canvas, tall, (side + gap, 0, width, bottom))
        _paste_photo(canvas, rest[0], (0, 0, side, half))
        _paste_photo(canvas, rest[1], (0, half + gap, side, bottom))
    elif len(images) >= 3:
        # Wide photos: one across the top, two side by side under it.
        split = int(bottom * 0.56)
        middle = (width - gap) // 2
        _paste_photo(canvas, images[0], (0, 0, width, split))
        _paste_photo(canvas, images[1], (middle + gap, split + gap, width, bottom))
        _paste_photo(canvas, images[2], (0, split + gap, middle, bottom))
    elif len(images) == 2:
        _paste_photo(canvas, images[0], (0, 0, width, half))
        _paste_photo(canvas, images[1], (0, half + gap, width, bottom))
    else:
        _paste_photo(canvas, images[0], (0, 0, width, bottom))
    _paste_logo(canvas, 150, left=84, plate=True)
    _info_block(canvas, info, (0, bottom, width, bottom + info["band"]))
    _cta(canvas, listing)


def _layout_cinematic(canvas, images, listing, tag, headline):
    """Full-bleed photo under a dark fade, a red ribbon title, a code disc."""

    width, height = canvas.size
    # The most upright photo fills the tall story best.
    canvas.paste(ImageOps.fit(min(images, key=lambda image: image.width / image.height),
                              (width, height), Image.LANCZOS))
    info = _info(listing, tag, headline, width)
    top = info["top"]

    # Darken the top for the logo and everything from the title down for the text.
    fade = Image.new("L", (width, height), 0)
    fade.paste(ImageOps.flip(Image.linear_gradient("L").resize((width, 520))).point(lambda v: int(v * 0.55)))
    fade.paste(Image.linear_gradient("L").resize((width, 420)).point(lambda v: int(v * 0.9)), (0, top - 420))
    fade.paste(Image.new("L", (width, height - top), 230), (0, top))
    canvas.paste((6, 10, 18), (0, 0, width, height), fade)

    _paste_logo(canvas, 165, left=84, plate=True)
    _code_badge(canvas, (width - MARGIN - 100, 352), 100, listing)
    band_left = max(40, int(width - info["title_width"] - MARGIN - 70))
    _info_block(canvas, info, (band_left, top, width + 40, top + info["band"]),
                radius=36, style="outline")
    _cta(canvas, listing)


# (name, renderer, call-to-action colour at its left end)
STORY_LAYOUTS = (
    ("classic", _layout_classic, RED_DEEP),
    ("magazine", _layout_magazine, TEXT),
    ("mosaic", _layout_mosaic, RED_DEEP),
    ("cinematic", _layout_cinematic, RED_DEEP),
)


def render_story(photos, listing, tag, headline, layout=0):
    """
    photos: image bytes, the first is the main photo; layout: index into
    STORY_LAYOUTS. Returns JPEG bytes.
    """

    images = []
    for data in photos:
        try:
            images.append(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB"))
        except Exception:
            logging.warning("Story photo could not be read; skipped")
    if not images:
        raise ValueError("no readable photo")

    canvas = Image.new("RGB", STORY_SIZE, BACKGROUND)
    _name, render, _cta_fill = STORY_LAYOUTS[layout % len(STORY_LAYOUTS)]
    render(canvas, images, listing, tag, headline)
    _chevrons(canvas, CTA_TOP + CTA_HEIGHT + 34)

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


# ---------------------------------------------------------------- telegram

def parse_story_count(text):
    digits = re.sub(r"\D", "", str(text or "").translate(LATIN_DIGITS))
    if not digits:
        return None
    count = int(digits)
    return count if 1 <= count <= STORY_MAX_COUNT else None


def story_count_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    persian_digits(count),
                    callback_data=f"{INSTAGRAM_STORY_COUNT_PREFIX}{count}",
                )
                for count in STORY_COUNT_CHOICES
            ],
            [InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")],
        ]
    )


def story_price_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 همه قیمت‌ها", callback_data=INSTAGRAM_STORY_ALL_PRICES)],
            [InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")],
        ]
    )


def story_done_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 ساخت استوری‌های بیشتر", callback_data=INSTAGRAM_STORY_ACTION)],
            [InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")],
        ]
    )


def _count_prompt():
    return (
        f"{INSTAGRAM_STORY_TITLE}\n\n"
        "چند تا استوری بسازم؟\n"
        f"یکی از دکمه‌ها رو بزنید یا عددش رو بنویسید (۱ تا {persian_digits(STORY_MAX_COUNT)})."
    )


def _price_prompt(count):
    return (
        f"{INSTAGRAM_STORY_TITLE}\n\n"
        f"✅ {persian_digits(count)} استوری\n\n"
        "بازهٔ قیمت رو بنویسید، مثلاً:\n"
        "«۱۰ تا ۱۲ میلیارد» یا «زیر ۸» یا «بالای ۲۰»\n\n"
        "یا «💰 همه قیمت‌ها» رو بزنید.\n"
        "قیمت روی استوری نمیاد تا مخاطب کد رو کامنت کنه."
    )


async def _download_photos(bot, file_ids):
    photos = []
    for file_id in file_ids:
        try:
            telegram_file = await bot.get_file(file_id)
            photos.append(bytes(await telegram_file.download_as_bytearray()))
        except Exception as exc:
            logging.warning("Story photo download failed: %s", exc)
    return photos


async def make_stories(bot, chat_id, count, price_range=None, rng=random):
    """Pick count random listings in the price range and send one story each."""

    label = price_range_label(price_range)
    status = await bot.send_message(
        chat_id, f"⏳ در حال ساخت {persian_digits(count)} استوری ({label})…"
    )
    low, high = price_range or (None, None)
    first_layout = rng.randrange(len(STORY_LAYOUTS))
    made = 0

    try:
        listings = await asyncio.to_thread(random_story_listings, count, low, high)
        for listing in listings:
            if made >= count:
                break
            photos = await _download_photos(bot, listing["photo_file_ids"])
            if not photos:
                continue
            caption = await asyncio.to_thread(openai_caption, listing, rng)
            tag, headline = story_titles(listing)
            try:
                image = await asyncio.to_thread(
                    render_story, photos, listing, tag, headline,
                    first_layout + made,
                )
            except ValueError:
                continue

            made += 1
            # Sent as a file: Telegram shrinks photos to 720×1280, the file
            # stays exactly 1080×1920 for Instagram.
            await bot.send_document(
                chat_id,
                document=io.BytesIO(image),
                filename=f"story-{short_code(listing)}.jpg",
                caption=caption[:1024],
            )
            logging.info(
                "Instagram story created: property_id=%s unit_code=%s layout=%s",
                listing["id"],
                listing["unit_code"],
                STORY_LAYOUTS[(first_layout + made - 1) % len(STORY_LAYOUTS)][0],
            )
            try:
                await status.edit_text(
                    f"⏳ استوری {persian_digits(made)} از {persian_digits(count)} ارسال شد…"
                )
            except Exception:
                pass
            await asyncio.sleep(STORY_SEND_SPACING_SECONDS)
    except Exception:
        logging.exception("Instagram story batch failed: chat_id=%s", chat_id)
    finally:
        try:
            await status.delete()
        except Exception:
            pass

    if made == count:
        summary = f"✅ {persian_digits(made)} استوری ساخته شد ({label})."
    elif made:
        summary = (
            f"✅ {persian_digits(made)} استوری ساخته شد؛ "
            f"در بازهٔ «{label}» فایل فعال عکس‌دار بیشتری پیدا نشد."
        )
    else:
        summary = f"⚠️ در بازهٔ «{label}» فایل فعال عکس‌داری برای ساخت استوری پیدا نشد."
    await bot.send_message(chat_id, summary, reply_markup=story_done_keyboard())


def _start_batch(context, chat_id, count, price_range):
    running = context.application.bot_data.setdefault("instagram_story_chats", set())
    if chat_id in running:
        return False
    running.add(chat_id)

    async def run():
        try:
            await make_stories(context.bot, chat_id, count, price_range)
        finally:
            running.discard(chat_id)

    # A batch takes a while; run it beside the update handlers.
    context.application.create_task(run())
    return True


def _clear(context):
    context.user_data.pop("state", None)
    return context.user_data.pop("insta_story_count", None)


async def handle_instagram_story_button(query, context):
    action = query.data or ""
    chat_id = query.message.chat_id

    if action.startswith(INSTAGRAM_STORY_COUNT_PREFIX):
        count = parse_story_count(action[len(INSTAGRAM_STORY_COUNT_PREFIX):])
        if not count:
            return
        context.user_data["insta_story_count"] = count
        context.user_data["state"] = INSTAGRAM_STORY_PRICE_STATE
        await query.edit_message_text(_price_prompt(count), reply_markup=story_price_keyboard())
        return

    if action == INSTAGRAM_STORY_ALL_PRICES:
        count = _clear(context)
        if not count:
            await query.edit_message_text(_count_prompt(), reply_markup=story_count_keyboard())
            context.user_data["state"] = INSTAGRAM_STORY_COUNT_STATE
            return
        if not _start_batch(context, chat_id, count, None):
            await query.message.reply_text("⏳ استوری‌های قبلی هنوز در حال ساخت‌اند.")
        return

    _clear(context)
    context.user_data["state"] = INSTAGRAM_STORY_COUNT_STATE
    await query.edit_message_text(_count_prompt(), reply_markup=story_count_keyboard())


async def handle_instagram_story_text(update, context, allowed):
    state = context.user_data.get("state")
    if not allowed:
        _clear(context)
        return

    if state == INSTAGRAM_STORY_COUNT_STATE:
        count = parse_story_count(update.message.text)
        if not count:
            await update.message.reply_text(
                f"لطفاً یه عدد بین ۱ تا {persian_digits(STORY_MAX_COUNT)} بفرستید.",
                reply_markup=story_count_keyboard(),
            )
            return
        context.user_data["insta_story_count"] = count
        context.user_data["state"] = INSTAGRAM_STORY_PRICE_STATE
        await update.message.reply_text(_price_prompt(count), reply_markup=story_price_keyboard())
        return

    price_range = parse_price_range(update.message.text)
    if price_range is None:
        await update.message.reply_text(
            "بازهٔ قیمت رو متوجه نشدم 🙏 مثلاً بنویسید «۱۰ تا ۱۲ میلیارد».",
            reply_markup=story_price_keyboard(),
        )
        return

    count = _clear(context) or STORY_COUNT_CHOICES[2]
    if not _start_batch(context, update.effective_chat.id, count, price_range):
        await update.message.reply_text("⏳ استوری‌های قبلی هنوز در حال ساخت‌اند.")
