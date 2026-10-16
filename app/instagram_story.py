"""Instagram stories for random live listings.

Settings › «📸 استوری اینستاگرام» asks how many stories to make and for which
price range, picks that many random live listings with photos in the range,
and sends one 1080×1920 story per listing, rotating through four layouts.
Each story is built from the listing's real photos and own facts, with a
factual title (no slogan); OpenAI only writes the Instagram caption (a
factual template is used when it is not available). The price is left out
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
CTA_TOP = 1530
CTA_HEIGHT = 110

OPENAI_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "IRANSansWeb.ttf"
FONT_MEDIUM = FONT_DIR / "IRANSansWeb_Medium.ttf"
FONT_BLACK = FONT_DIR / "IRANSansWeb_Black.ttf"

BACKGROUND = (14, 24, 40)
CHIP = (32, 46, 70)
TEXT = (255, 255, 255)
ACCENT = (226, 182, 82)
ACCENT_TEXT = (20, 24, 32)

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
    "internal_sequence",
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
                    p.has_storage, p.internal_sequence
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

    for listing in listings:
        listing["photo_file_ids"] = photos.get(listing["id"], [])
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


def story_facts(listing):
    """The listing's own facts as short chips; never the price."""

    chips = []
    if listing.get("district"):
        chips.append(str(listing["district"]).strip())
    if listing.get("area_m2"):
        chips.append(f"{persian_digits(int(Decimal(str(listing['area_m2']))))} متر")
    if listing.get("bedrooms") is not None:
        chips.append(f"{persian_digits(listing['bedrooms'])} خواب")
    if listing.get("floor_text"):
        chips.append(f"طبقه {persian_digits(listing['floor_text'])}")
    if listing.get("build_year"):
        chips.append(f"ساخت {persian_digits(listing['build_year'])}")
    for key, label in (
        ("has_elevator", "آسانسور"),
        ("has_parking", "پارکینگ"),
        ("has_storage", "انباری"),
    ):
        if listing.get(key) is True:
            chips.append(label)
    return chips


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


def story_chips(listing):
    """Facts shown as chips: everything the tag and headline do not already say."""

    tag, headline = story_titles(listing)
    shown = {tag, headline, *headline.split(TITLE_SEPARATOR)}
    return [chip for chip in story_facts(listing) if chip not in shown]


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
    chips = story_chips(listing)
    caption = "\n".join(
        line for line in (
            f"🏡 {headline} | {tag}",
            "🔑 " + " • ".join(chips) if chips else "",
            "",
            f"💬 {call_to_action(listing)}",
            "",
            _hashtags(listing),
        )
    )
    return re.sub(r"\n{3,}", "\n\n", caption).strip()


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
GOLD_DEEP = (198, 144, 52)
GOLD_LIGHT = (247, 216, 134)
_LOGO_CACHE = {}


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


def _text_mask(shaped, font):
    bbox = font.getbbox(shaped)
    mask = Image.new("L", (int(font.getlength(shaped)) + 8, bbox[3] + 8), 0)
    ImageDraw.Draw(mask).text((0, 0), shaped, font=font, fill=255)
    return mask


def _draw_rtl(canvas, draw, text, font, right, top, fill, shadow=False):
    shaped = _shape(text)
    left = int(right - font.getlength(shaped))
    if shadow:
        _soft_shadow(canvas, _text_mask(shaped, font), (left, int(top)), blur=10, opacity=0.7, offset=(0, 4))
    draw.text((left, top), shaped, font=font, fill=fill)


def _draw_headline(canvas, draw, lines, font, right, top, line_height, fill, highlight, shadow=False):
    """Right-aligned headline whose last word is drawn in the highlight colour."""

    space = font.getlength(" ")
    for index, line in enumerate(lines):
        if shadow:
            shaped = _shape(line)
            left = int(right - font.getlength(shaped))
            _soft_shadow(canvas, _text_mask(shaped, font), (left, int(top)), blur=12, opacity=0.75, offset=(0, 5))
        words = line.split()
        x = right
        for position, word in enumerate(words):
            shaped = _shape(word)
            width = font.getlength(shaped)
            last = index == len(lines) - 1 and position == len(words) - 1
            draw.text((x - width, top), shaped, font=font, fill=highlight if last and len(lines) + len(words) > 1 else fill)
            x -= width + space
        top += line_height
    return top


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


def _gradient(size, start, end):
    """Horizontal gradient from start (left) to end (right)."""

    mask = Image.linear_gradient("L").rotate(90).resize(size)
    return Image.composite(Image.new("RGB", size, end), Image.new("RGB", size, start), mask)


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _pill(canvas, box, fill, shadow=True):
    left, top, right, bottom = box
    size = (right - left, bottom - top)
    mask = _rounded_mask(size, size[1] // 2)
    if shadow:
        _soft_shadow(canvas, mask, (left, top), blur=18, opacity=0.55, offset=(0, 10))
    surface = _gradient(size, *fill) if isinstance(fill[0], tuple) else Image.new("RGB", size, fill)
    canvas.paste(surface, (left, top), mask)


def _paste_photo(canvas, image, box, radius=0):
    left, top, right, bottom = box
    photo = ImageOps.fit(image, (right - left, bottom - top), Image.LANCZOS)
    if radius:
        canvas.paste(photo, (left, top), _rounded_mask(photo.size, radius))
    else:
        canvas.paste(photo, (left, top))


def _paste_framed(canvas, image, size, center, angle, border=14):
    """A photo in a white frame, tilted by angle degrees, with a drop shadow."""

    photo = ImageOps.fit(image, size, Image.LANCZOS)
    framed = Image.new("RGBA", (size[0] + 2 * border, size[1] + 2 * border), (255, 255, 255, 255))
    framed.paste(photo, (border, border))
    framed = framed.rotate(angle, resample=Image.BICUBIC, expand=True)
    position = (int(center[0] - framed.width / 2), int(center[1] - framed.height / 2))
    _soft_shadow(canvas, framed.getchannel("A"), position, blur=22, opacity=0.6, offset=(0, 16))
    canvas.paste(framed, position, framed)


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
        width = font.getlength(_shape(chip)) + 56
        if rows[-1] and used + width > max_width:
            if len(rows) == max_rows:
                break
            rows.append([])
            used = 0
        rows[-1].append((chip, width))
        used += width + 16
    return [row for row in rows if row]


def _draw_chips(canvas, rows, font, right, top, fill, text_fill, outline=None, glass=False):
    if glass:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        glass_draw = ImageDraw.Draw(layer)
    draw = ImageDraw.Draw(canvas)
    for row in rows:
        x = right
        for chip, width in row:
            box = (int(x - width), top, int(x), top + 62)
            if glass:
                glass_draw.rounded_rectangle(box, radius=31, fill=(255, 255, 255, 46),
                                             outline=(255, 255, 255, 120), width=2)
            else:
                draw.rounded_rectangle(box, radius=31, fill=fill, outline=outline,
                                       width=3 if outline else 0)
            x -= width + 16
        top += 80
    if glass:
        canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB"))
        draw = ImageDraw.Draw(canvas)
    top -= 80 * len(rows)
    for row in rows:
        x = right
        for chip, width in row:
            _draw_centered(draw, chip, font, (x - width, top, x, top + 62), text_fill)
            x -= width + 16
        top += 80
    return top


def _cta(canvas, listing, box, fill, text_fill):
    text = call_to_action(listing)
    left, top, right, bottom = box
    _pill(canvas, box, fill)
    font = _fit_font(FONT_BLACK, text, right - left - 60, 44, 28)
    _draw_centered(ImageDraw.Draw(canvas), text, font, box, text_fill)


def _code_badge(canvas, center, radius, listing):
    """A gold disc asking viewers to comment the short code."""

    box = (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
    mask = Image.new("L", (2 * radius, 2 * radius), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 2 * radius - 1, 2 * radius - 1), fill=255)
    _soft_shadow(canvas, mask, box[:2], blur=20, opacity=0.6, offset=(0, 10))
    canvas.paste(_gradient(mask.size, GOLD_DEEP, GOLD_LIGHT), box[:2], mask)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((box[0] + 10, box[1] + 10, box[2] - 10, box[3] - 10), outline=(255, 255, 255), width=4)
    _draw_centered(draw, "کامنت کن", _font(FONT_MEDIUM, 30),
                   (box[0], center[1] - radius * 0.62, box[2], center[1] - radius * 0.12), ACCENT_TEXT)
    _draw_centered(draw, persian_digits(short_code(listing)), _font(FONT_BLACK, int(radius * 0.72)),
                   (box[0], center[1] - radius * 0.1, box[2], center[1] + radius * 0.62), ACCENT_TEXT)


def _texts(listing, kicker, headline, width, headline_size):
    margin = 60
    headline_font = _font(FONT_BLACK, headline_size)
    chip_font = _font(FONT_MEDIUM, 34)
    return {
        "kicker_font": _fit_font(FONT_MEDIUM, kicker, width - 2 * margin - 60, 38, 28),
        "headline_font": headline_font,
        "lines": _wrap(headline, headline_font, width - 2 * margin, 2),
        "chip_font": chip_font,
        "rows": _chip_rows(story_chips(listing), chip_font, width - 2 * margin),
    }


def _kicker_tag(canvas, kicker, font, right, top, fill=ACCENT, text_fill=ACCENT_TEXT):
    width = int(font.getlength(_shape(kicker)) + 48)
    _pill(canvas, (right - width, top, right, top + 58), fill, shadow=False)
    _draw_centered(ImageDraw.Draw(canvas), kicker, font, (right - width, top, right, top + 58), text_fill)


def _layout_diagonal(canvas, draw, images, listing, kicker, headline):
    """Photo cut on a slant with a gold stripe; tilted photo prints, gold CTA."""

    width, height = canvas.size
    margin, right = 60, width - 60
    t = _texts(listing, kicker, headline, width, 82)
    extra = images[1:4]
    block = 76 + 108 * len(t["lines"]) + 80 * len(t["rows"]) + (300 if extra else 0) + 10
    y = CTA_TOP - 40 - block

    _paste_photo(canvas, images[0], (0, 0, width, y + 160))
    low, high = y - 30, y - 220
    draw.polygon([(0, low), (width, high), (width, height), (0, height)], fill=BACKGROUND)
    draw.polygon([(0, low - 34), (width, high - 34), (width, high - 14), (0, low - 14)], fill=ACCENT)
    _paste_logo(canvas, 170, left=84, plate=True)

    _kicker_tag(canvas, kicker, t["kicker_font"], right, y)
    y += 76
    y = _draw_headline(canvas, draw, t["lines"], t["headline_font"], right, y, 108, TEXT, ACCENT)
    y = _draw_chips(canvas, t["rows"], t["chip_font"], right, y + 4, CHIP, TEXT)
    if extra:
        slot = (width - 2 * margin) / len(extra)
        for index, (image, angle) in enumerate(zip(extra, (-5, 4, -3))):
            _paste_framed(canvas, image, (int(slot) - 70, 190),
                          (int(right - slot * (index + 0.5)), y + 150), angle, border=10)
    _cta(canvas, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), (GOLD_DEEP, GOLD_LIGHT), ACCENT_TEXT)


def _layout_magazine(canvas, draw, images, listing, kicker, headline):
    """Blurred dark backdrop, a tilted framed photo, glass chips, white CTA."""

    width, height = canvas.size
    margin, right = 60, width - 60
    backdrop = ImageOps.fit(images[0], (width, height), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
    canvas.paste(Image.blend(backdrop, Image.new("RGB", (width, height), BACKGROUND), 0.55))
    draw = ImageDraw.Draw(canvas)

    t = _texts(listing, kicker, headline, width, 80)
    block = 76 + 106 * len(t["lines"]) + 80 * len(t["rows"])
    text_top = CTA_TOP - 40 - block
    _paste_logo(canvas, 130, center=True, top=262)

    frame_top = 420
    frame_height = max(360, min(660, text_top - 70 - frame_top))
    _paste_framed(canvas, images[0], (860, frame_height - 28),
                  (width // 2, frame_top + frame_height // 2), -3, border=14)
    if len(images) > 1:
        _paste_framed(canvas, images[1], (250, 180),
                      (right - 110, frame_top + frame_height - 30), 6, border=8)

    y = text_top
    _kicker_tag(canvas, kicker, t["kicker_font"], right, y)
    y += 76
    y = _draw_headline(canvas, draw, t["lines"], t["headline_font"], right, y, 106, TEXT, GOLD_LIGHT)
    _draw_chips(canvas, t["rows"], t["chip_font"], right, y + 4, None, TEXT, glass=True)
    _cta(canvas, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), TEXT, ACCENT_TEXT)


def _layout_mosaic(canvas, draw, images, listing, kicker, headline):
    """Photo mosaic under a slanted gold band carrying the headline."""

    width, height = canvas.size
    margin, right = 60, width - 60
    gap = 12
    mosaic_bottom = 1130
    if len(images) >= 3:
        _paste_photo(canvas, images[0], (0, 0, width, 740))
        half = (width - gap) // 2
        _paste_photo(canvas, images[1], (0, 740 + gap, half, mosaic_bottom))
        _paste_photo(canvas, images[2], (half + gap, 740 + gap, width, mosaic_bottom))
    elif len(images) == 2:
        _paste_photo(canvas, images[0], (0, 0, width, 740))
        _paste_photo(canvas, images[1], (0, 740 + gap, width, mosaic_bottom))
    else:
        _paste_photo(canvas, images[0], (0, 0, width, mosaic_bottom))
    draw.rectangle((0, mosaic_bottom, width, height), fill=BACKGROUND)
    _paste_logo(canvas, 165, left=84, plate=True)

    headline_font = _font(FONT_BLACK, 66)
    lines = _wrap(headline, headline_font, width - 2 * margin, 2)
    band_top = 960
    band_height = 96 + 88 * len(lines) + 30
    band = [(0, band_top + 70), (width, band_top), (width, band_top + band_height),
            (0, band_top + band_height + 70)]
    shadow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(shadow).polygon(band, fill=255)
    _soft_shadow(canvas, shadow.crop((0, 0, width, height)), (0, 0), blur=18, opacity=0.5, offset=(0, 12))
    surface = _gradient((width, height), GOLD_DEEP, GOLD_LIGHT)
    canvas.paste(surface, (0, 0), shadow)
    draw = ImageDraw.Draw(canvas)

    kicker_font = _fit_font(FONT_MEDIUM, kicker, width - 2 * margin, 36, 28)
    _draw_rtl(canvas, draw, kicker, kicker_font, right, band_top + 30, ACCENT_TEXT)
    y = _draw_headline(canvas, draw, lines, headline_font, right, band_top + 84, 88, ACCENT_TEXT, (255, 255, 255))

    chip_font = _font(FONT_MEDIUM, 34)
    rows = _chip_rows(story_chips(listing), chip_font, width - 2 * margin)
    chips_top = max(y + 70, band_top + band_height + 100)
    room = max(1, (CTA_TOP - 30 - chips_top) // 80)
    _draw_chips(canvas, rows[:room], chip_font, right, chips_top, CHIP, TEXT)
    _cta(canvas, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), (GOLD_DEEP, GOLD_LIGHT), ACCENT_TEXT)


def _layout_cinematic(canvas, draw, images, listing, kicker, headline):
    """Full-bleed photo, vignette, gold frame, logo and a comment-code disc."""

    width, height = canvas.size
    margin, right = 60, width - 60
    _paste_photo(canvas, images[0], (0, 0, width, height))

    t = _texts(listing, kicker, headline, width, 86)
    block = 76 + 112 * len(t["lines"]) + 80 * len(t["rows"]) + 10
    y = CTA_TOP - 40 - block

    # Darken the edges and the lower half so white text always reads.
    vignette = Image.new("L", (width, height), 0)
    ImageDraw.Draw(vignette).ellipse((-260, -120, width + 260, height + 40), fill=255)
    vignette = ImageOps.invert(vignette.filter(ImageFilter.GaussianBlur(160))).point(lambda v: int(v * 0.8))
    lower = Image.new("L", (width, height), 0)
    lower.paste(Image.linear_gradient("L").resize((width, 560)).point(lambda v: int(v * 0.92)), (0, y - 460))
    lower.paste(Image.new("L", (width, height - (y + 100)), 235), (0, y + 100))
    shade = Image.new("RGB", (width, height), (6, 10, 18))
    canvas.paste(shade, (0, 0), vignette)
    canvas.paste(shade, (0, 0), lower)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=44, outline=ACCENT, width=4)

    _paste_logo(canvas, 180, left=84, top=270, plate=True)
    _code_badge(canvas, (right - 105, 372), 105, listing)

    _kicker_tag(canvas, kicker, t["kicker_font"], right, y)
    y += 76
    y = _draw_headline(canvas, draw, t["lines"], t["headline_font"], right, y, 112, TEXT, GOLD_LIGHT, shadow=True)
    _draw_chips(canvas, t["rows"], t["chip_font"], right, y + 4, None, TEXT, outline=TEXT)
    _cta(canvas, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), (GOLD_DEEP, GOLD_LIGHT), ACCENT_TEXT)


# (name, renderer, call-to-action colour at its left end)
STORY_LAYOUTS = (
    ("diagonal", _layout_diagonal, GOLD_DEEP),
    ("magazine", _layout_magazine, TEXT),
    ("mosaic", _layout_mosaic, GOLD_DEEP),
    ("cinematic", _layout_cinematic, GOLD_DEEP),
)


def render_story(photos, listing, kicker, headline, layout=0):
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
    draw = ImageDraw.Draw(canvas)
    _name, render, _cta_fill = STORY_LAYOUTS[layout % len(STORY_LAYOUTS)]
    render(canvas, draw, images, listing, kicker, headline)

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
