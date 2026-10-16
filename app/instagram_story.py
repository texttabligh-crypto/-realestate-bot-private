"""Instagram stories for random live listings.

Settings › «📸 استوری اینستاگرام» asks how many stories to make and for which
price range, picks that many random live listings with photos in the range,
and sends one 1080×1920 story per listing, rotating through four layouts.
Each story is built from the listing's real photos and own facts; OpenAI only
writes the kicker, headline and caption (curated templates are used when it
is not available). The price is left out on purpose: the call to action asks
viewers to comment the unit code. No source text, agency name, phone number
or channel handle is ever drawn, and photos are never generated or altered.
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
MUTED = (196, 205, 220)
ACCENT = (226, 182, 82)
ACCENT_TEXT = (20, 24, 32)
CREAM = (250, 246, 238)
CREAM_CHIP = (236, 229, 214)
DEEP_GOLD = (170, 124, 36)

PERSIAN_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")
LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍#]")
_PRICE_WORDS = re.compile("میلیارد|میلیون|تومان|ریال")

STYLE_HINTS = (
    "لوکس و باکلاس",
    "گرم و خانوادگی",
    "مینیمال و شیک",
    "احساسی و رویایی",
    "پرانرژی و فوری",
)

# Fallback copy when OpenAI is not available. {district}, {area} and
# {bedrooms} are filled from the listing; lines needing a missing fact are
# skipped.
KICKERS = (
    "پیشنهاد ویژه {district}",
    "فقط برای خوش‌سلیقه‌ها",
    "تازه به لیست اضافه شد",
    "یک انتخاب هوشمندانه",
    "از اون فایل‌هایی که زود میره",
    "دل‌نشین در {district}",
)
HEADLINES = (
    "خونه‌ای که دنبالش بودی",
    "اینجا آرامش آدرس داره",
    "نور، فضا، {district}",
    "{area} متر حال خوب",
    "شروع یه زندگی تازه",
    "یه قدم تا خونه‌ی رویایی",
    "{bedrooms} خواب، یک دنیا آرامش",
    "جایی که دلت می‌خواد بمونی",
)


# ---------------------------------------------------------------- listings

_LISTING_KEYS = (
    "id", "unit_code", "district", "area_m2", "bedrooms", "floor_text",
    "build_year", "has_parking", "has_elevator", "has_storage",
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
                    p.has_storage
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


def call_to_action(listing):
    code = persian_digits(listing["unit_code"])
    return f"برای دریافت قیمت و مشخصات، عدد {code} رو کامنت کنید"


# ---------------------------------------------------------------- copy

def _template_values(listing):
    values = {}
    if listing.get("district"):
        values["district"] = str(listing["district"]).strip()
    if listing.get("area_m2"):
        values["area"] = persian_digits(int(Decimal(str(listing["area_m2"]))))
    if listing.get("bedrooms"):
        values["bedrooms"] = persian_digits(listing["bedrooms"])
    return values


def _pick(templates, values, rng):
    usable = []
    for template in templates:
        try:
            usable.append(template.format(**values))
        except KeyError:
            continue
    return rng.choice(usable)


def _hashtags(listing):
    tags = ["#املاک_تبریز", "#خرید_آپارتمان", "#تبریز", "#خانه_رویایی"]
    if listing.get("district"):
        tags.insert(0, "#" + re.sub(r"\s+", "_", str(listing["district"]).strip()))
    return " ".join(tags)


def fallback_story_text(listing, rng=random):
    values = _template_values(listing)
    kicker = _pick(KICKERS, values, rng)
    headline = _pick(HEADLINES, values, rng)
    chips = story_facts(listing)
    caption = "\n".join(
        line for line in (
            f"✨ {kicker}",
            f"🏡 {headline}",
            "",
            "📍 " + " | ".join(chips[:3]) if chips else "",
            "🔑 " + " • ".join(chips[3:]) if chips[3:] else "",
            "",
            f"💬 {call_to_action(listing)}",
            "",
            _hashtags(listing),
        )
    )
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    return {"kicker": kicker, "headline": headline, "caption": caption}


def _clean_title(value, limit):
    value = _EMOJI.sub("", str(value or "")).strip()
    return value if value and len(value) <= limit else ""


def openai_story_text(listing, rng=random):
    """Kicker, headline and caption from OpenAI, or curated templates."""

    fallback = fallback_story_text(listing, rng)
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback

    instructions = """
You are an elite Persian real-estate copywriter and art director writing
Instagram story copy for a property channel in Tabriz. Write in Persian.
Use ONLY the facts given; never invent features, addresses, phone numbers,
agency or person names. Never mention the price or any amount of money: the
viewer must comment the unit code to get the price.
Return ONLY one JSON object: {"kicker": "...", "headline": "...", "caption": "..."}
- kicker: a short premium tagline shown above the headline, at most 24
  characters, no emoji, no hashtags.
- headline: a bold, emotional, scroll-stopping headline, at most 28
  characters, no emoji, no hashtags. Conversational Instagram Persian is fine.
  Avoid tired clichés such as «فرصت استثنایی» and «بی‌نظیر». Match the
  requested style.
- caption: 3 to 5 short engaging Persian lines about the home from the facts
  (emoji allowed), then the call to action exactly as given, then 5 to 7
  relevant Persian hashtags on one line.
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
        "max_output_tokens": 500,
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
        logging.warning("OpenAI story text failed: %s", exc)
        return fallback

    text = ""
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                text = content["text"]
    match = re.search(r"\{.*\}", text or "", flags=re.S)
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except ValueError:
        parsed = {}

    kicker = _clean_title(parsed.get("kicker"), 30) or fallback["kicker"]
    headline = _clean_title(parsed.get("headline"), 34) or fallback["headline"]
    if _PRICE_WORDS.search(kicker + headline):
        kicker, headline = fallback["kicker"], fallback["headline"]

    # Keep the price out of the caption whatever the model wrote.
    lines = [
        line for line in str(parsed.get("caption") or "").splitlines()
        if not _PRICE_WORDS.search(line)
    ]
    caption = "\n".join(lines).strip() or fallback["caption"]
    if listing["unit_code"] not in caption.translate(LATIN_DIGITS):
        caption = f"{caption}\n\n💬 {call_to_action(listing)}"
    return {"kicker": kicker, "headline": headline, "caption": caption}


# ---------------------------------------------------------------- image

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


def _draw_rtl(draw, text, font, right, top, fill):
    shaped = _shape(text)
    draw.text((right - font.getlength(shaped), top), shaped, font=font, fill=fill)


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


def _fit_font(path, text, max_width, size, minimum):
    while size > minimum:
        font = _font(path, size)
        if font.getlength(_shape(text)) <= max_width:
            return font
        size -= 2
    return _font(path, minimum)


def _rounded(image, radius):
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0) + image.size, radius=radius, fill=255)
    image.putalpha(mask)
    return image


def _paste_photo(canvas, image, box, radius=0):
    left, top, right, bottom = box
    photo = ImageOps.fit(image, (right - left, bottom - top), Image.LANCZOS)
    if radius:
        photo = _rounded(photo, radius)
        canvas.paste(photo, (left, top), photo)
    else:
        canvas.paste(photo, (left, top))


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


def _draw_chips(draw, rows, font, right, top, fill, text_fill, outline=None):
    for row in rows:
        x = right
        for chip, width in row:
            draw.rounded_rectangle(
                (x - width, top, x, top + 62), radius=31,
                fill=fill, outline=outline, width=3 if outline else 0,
            )
            _draw_centered(draw, chip, font, (x - width, top, x, top + 62), text_fill)
            x -= width + 16
        top += 80
    return top


def _fade(canvas, top, length, color, height=None):
    """Blend into color from transparent at top to opaque at top+length."""

    width = canvas.size[0]
    mask = Image.linear_gradient("L").resize((width, length))
    canvas.paste(Image.new("RGB", (width, length), color), (0, top), mask)
    below = (height or canvas.size[1]) - (top + length)
    if below > 0:
        canvas.paste(Image.new("RGB", (width, below), color), (0, top + length))


def _thumb_row(canvas, images, left, right, top, height, radius=26):
    gap = 18
    width = (right - left - gap * (len(images) - 1)) // len(images)
    x = left
    for image in images:
        _paste_photo(canvas, image, (x, top, x + width, top + height), radius)
        x += width + gap


def _cta(draw, listing, box, fill, text_fill):
    text = call_to_action(listing)
    left, top, right, bottom = box
    font = _fit_font(FONT_BLACK, text, right - left - 60, 44, 28)
    draw.rounded_rectangle(box, radius=(bottom - top) // 2, fill=fill)
    _draw_centered(draw, text, font, box, text_fill)


def _layout_classic(canvas, draw, images, listing, kicker, headline):
    """Photo on top fading into navy; gold kicker, chips, thumbnails, gold CTA."""

    width = canvas.size[0]
    margin, right = 60, width - 60
    kicker_font = _fit_font(FONT_MEDIUM, kicker, width - 2 * margin, 40, 30)
    headline_font = _font(FONT_BLACK, 76)
    lines = _wrap(headline, headline_font, width - 2 * margin, 2)
    chip_font = _font(FONT_MEDIUM, 34)
    rows = _chip_rows(story_facts(listing), chip_font, width - 2 * margin)
    extra = images[1:4]

    block = 64 + 104 * len(lines) + 80 * len(rows) + (250 if extra else 0) + 10
    y = CTA_TOP - 40 - block
    photo_height = min(max(y + 200, 900), canvas.size[1])
    _paste_photo(canvas, images[0], (0, 0, width, photo_height))
    _fade(canvas, photo_height - 460, 460, BACKGROUND)

    _draw_rtl(draw, kicker, kicker_font, right, y, ACCENT)
    y += 64
    for line in lines:
        _draw_rtl(draw, line, headline_font, right, y, TEXT)
        y += 104
    y = _draw_chips(draw, rows, chip_font, right, y + 6, CHIP, TEXT)
    if extra:
        _thumb_row(canvas, extra, margin, right, y + 22, 210)
    _cta(draw, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), ACCENT, ACCENT_TEXT)


def _layout_card(canvas, draw, images, listing, kicker, headline):
    """Blurred photo backdrop with a cream card: framed photo and dark text."""

    width, height = canvas.size
    backdrop = ImageOps.fit(images[0], (width, height), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(28)
    )
    canvas.paste(Image.blend(backdrop, Image.new("RGB", (width, height), (0, 0, 0)), 0.45))

    card = (50, 250, width - 50, CTA_TOP + CTA_HEIGHT + 40)
    draw.rounded_rectangle(card, radius=48, fill=CREAM)
    left, right = card[0] + 36, card[2] - 36

    kicker_font = _fit_font(FONT_MEDIUM, kicker, right - left, 38, 28)
    headline_font = _font(FONT_BLACK, 66)
    lines = _wrap(headline, headline_font, right - left, 2)
    chip_font = _font(FONT_MEDIUM, 32)
    rows = _chip_rows(story_facts(listing), chip_font, right - left)
    extra = images[1:4]

    # The framed photo takes whatever the text leaves free in the card.
    block = 58 + 92 * len(lines) + 8 + 80 * len(rows) + (200 if extra else 0)
    y = CTA_TOP - 30 - block
    _paste_photo(canvas, images[0], (left, card[1] + 36, right, y - 34), 34)

    _draw_rtl(draw, kicker, kicker_font, right, y, DEEP_GOLD)
    y += 58
    for line in lines:
        _draw_rtl(draw, line, headline_font, right, y, ACCENT_TEXT)
        y += 92
    y = _draw_chips(draw, rows, chip_font, right, y + 8, CREAM_CHIP, ACCENT_TEXT)
    if extra:
        _thumb_row(canvas, extra, left, right, y + 10, 170)
    _cta(draw, listing, (left, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), BACKGROUND, ACCENT)


def _layout_grid(canvas, draw, images, listing, kicker, headline):
    """Photo collage on top, a gold band with the headline, chips below."""

    width = canvas.size[0]
    margin, right = 60, width - 60
    gap = 12
    top_height = 1050
    if len(images) >= 3:
        _paste_photo(canvas, images[0], (0, 0, width, 640))
        half = (width - gap) // 2
        _paste_photo(canvas, images[1], (0, 640 + gap, half, top_height))
        _paste_photo(canvas, images[2], (half + gap, 640 + gap, width, top_height))
    elif len(images) == 2:
        _paste_photo(canvas, images[0], (0, 0, width, 640))
        _paste_photo(canvas, images[1], (0, 640 + gap, width, top_height))
    else:
        _paste_photo(canvas, images[0], (0, 0, width, top_height))

    band_top = top_height
    headline_font = _font(FONT_BLACK, 62)
    lines = _wrap(headline, headline_font, width - 2 * margin, 2)
    band_height = 70 + 86 * len(lines) + 40
    draw.rectangle((0, band_top, width, band_top + band_height), fill=ACCENT)
    kicker_font = _fit_font(FONT_MEDIUM, kicker, width - 2 * margin, 36, 28)
    _draw_rtl(draw, kicker, kicker_font, right, band_top + 24, ACCENT_TEXT)
    y = band_top + 78
    for line in lines:
        _draw_rtl(draw, line, headline_font, right, y, ACCENT_TEXT)
        y += 86

    chip_font = _font(FONT_MEDIUM, 34)
    rows = _chip_rows(story_facts(listing), chip_font, width - 2 * margin)
    chips_top = band_top + band_height + 40
    room = max(1, (CTA_TOP - 30 - chips_top) // 80)
    _draw_chips(draw, rows[:room], chip_font, right, chips_top, CHIP, TEXT)
    _cta(draw, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), TEXT, ACCENT_TEXT)


def _layout_overlay(canvas, draw, images, listing, kicker, headline):
    """Full-bleed photo under a dark gradient; code badge, outlined chips."""

    width, height = canvas.size
    margin, right = 60, width - 60
    _paste_photo(canvas, images[0], (0, 0, width, height))

    headline_font = _font(FONT_BLACK, 80)
    lines = _wrap(headline, headline_font, width - 2 * margin, 2)
    chip_font = _font(FONT_MEDIUM, 34)
    rows = _chip_rows(story_facts(listing), chip_font, width - 2 * margin)
    block = 80 + 110 * len(lines) + 80 * len(rows) + 10
    y = CTA_TOP - 40 - block

    shade = Image.new("RGB", (width, height), (8, 12, 20))
    mask = Image.new("L", (width, height), 0)
    gradient = Image.linear_gradient("L").resize((width, 520)).point(lambda v: int(v * 0.9))
    mask.paste(gradient, (0, y - 420))
    mask.paste(Image.new("L", (width, height - (y + 100)), 230), (0, y + 100))
    canvas.paste(shade, (0, 0), mask)

    # The unit code viewers are asked to comment, inside the top safe zone.
    code_font = _font(FONT_BLACK, 40)
    code_text = f"کد {persian_digits(listing['unit_code'])}"
    code_width = code_font.getlength(_shape(code_text)) + 60
    badge = (right - code_width, 270, right, 340)
    draw.rounded_rectangle(badge, radius=35, fill=ACCENT)
    _draw_centered(draw, code_text, code_font, badge, ACCENT_TEXT)

    kicker_font = _fit_font(FONT_MEDIUM, kicker, width - 2 * margin - 40, 34, 26)
    tag_width = kicker_font.getlength(_shape(kicker)) + 44
    draw.rounded_rectangle((right - tag_width, y, right, y + 58), radius=14, fill=ACCENT)
    _draw_centered(draw, kicker, kicker_font, (right - tag_width, y, right, y + 58), ACCENT_TEXT)
    y += 80
    for line in lines:
        _draw_rtl(draw, line, headline_font, right, y, TEXT)
        y += 110
    _draw_chips(draw, rows, chip_font, right, y + 4, None, TEXT, outline=TEXT)
    _cta(draw, listing, (margin, CTA_TOP, right, CTA_TOP + CTA_HEIGHT), TEXT, ACCENT_TEXT)


STORY_LAYOUTS = (
    ("classic", _layout_classic, ACCENT),
    ("card", _layout_card, BACKGROUND),
    ("grid", _layout_grid, TEXT),
    ("overlay", _layout_overlay, TEXT),
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
            text = await asyncio.to_thread(openai_story_text, listing)
            try:
                image = await asyncio.to_thread(
                    render_story, photos, listing, text["kicker"], text["headline"],
                    first_layout + made,
                )
            except ValueError:
                continue

            made += 1
            await bot.send_photo(
                chat_id,
                photo=io.BytesIO(image),
                caption=text["caption"][:1024],
            )
            await bot.send_document(
                chat_id,
                document=io.BytesIO(image),
                filename=f"story-{listing['unit_code']}.jpg",
                caption=(
                    f"📎 فایل باکیفیت استوری {persian_digits(made)} "
                    f"(کد {persian_digits(listing['unit_code'])})"
                ),
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
