"""Instagram story images for random live listings.

Settings › «📸 استوری اینستاگرام» picks a random live listing that has
photos, asks OpenAI for a short Persian headline and caption written only from
the listing's own facts, and lays them over the listing's real photos as a
1080×1920 story with a «send the unit code» call to action. Photos are never
generated or altered, and no source text, agency name or phone number is used.
"""

import asyncio
import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import db_connect, get_destination_channel

INSTAGRAM_STORY_ACTION = "insta_story"
INSTAGRAM_STORY_PERMISSION = "instagram_story"
INSTAGRAM_STORY_TITLE = "📸 استوری اینستاگرام"

STORY_SIZE = (1080, 1920)
STORY_PHOTO_LIMIT = 4
STORY_PICK_ATTEMPTS = 3

OPENAI_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "IRANSansWeb.ttf"
FONT_MEDIUM = FONT_DIR / "IRANSansWeb_Medium.ttf"
FONT_BLACK = FONT_DIR / "IRANSansWeb_Black.ttf"

BACKGROUND = (14, 24, 40)
TEXT = (255, 255, 255)
MUTED = (196, 205, 220)
ACCENT = (226, 182, 82)
ACCENT_TEXT = (20, 24, 32)

PERSIAN_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


# ---------------------------------------------------------------- listing

def random_story_listing(exclude_ids=()):
    """A random live published listing with photos, as a dict, or None."""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id, p.unit_code, p.district, p.area_m2, p.bedrooms,
                    p.floor_text, p.build_year, p.has_parking, p.has_elevator,
                    p.has_storage, p.current_price_billion, p.current_price_toman
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.unit_code IS NOT NULL
                  AND NOT (p.id = ANY(%s))
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
                LIMIT 1
                """,
                (list(exclude_ids),),
            )
            row = cur.fetchone()
            if not row:
                return None

            cur.execute(
                """
                SELECT telegram_file_id
                FROM property_media
                WHERE property_id = %s
                  AND media_type = 'PHOTO'
                  AND telegram_file_id IS NOT NULL
                ORDER BY position, id
                LIMIT %s
                """,
                (row[0], STORY_PHOTO_LIMIT),
            )
            photos = [item[0] for item in cur.fetchall()]

    keys = (
        "id", "unit_code", "district", "area_m2", "bedrooms", "floor_text",
        "build_year", "has_parking", "has_elevator", "has_storage",
        "price_billion", "price_toman",
    )
    listing = dict(zip(keys, row))
    listing["photo_file_ids"] = photos
    return listing


def persian_digits(value):
    return str(value).translate(PERSIAN_DIGITS)


def price_text(listing):
    billion = listing.get("price_billion")
    if billion is not None:
        text = f"{Decimal(str(billion)):f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{persian_digits(text)} میلیارد تومان"
    toman = listing.get("price_toman")
    if toman:
        return f"{persian_digits(format(int(toman), ','))} تومان".replace(",", "٬")
    return "قیمت توافقی"


def story_facts(listing):
    """The listing's own facts, worded for the story and for OpenAI."""

    facts = []
    if listing.get("district"):
        facts.append(str(listing["district"]).strip())
    if listing.get("area_m2"):
        facts.append(f"{persian_digits(int(Decimal(str(listing['area_m2']))))} متر")
    if listing.get("bedrooms") is not None:
        facts.append(f"{persian_digits(listing['bedrooms'])} خواب")

    details = []
    if listing.get("floor_text"):
        details.append(f"طبقه {persian_digits(listing['floor_text'])}")
    if listing.get("build_year"):
        details.append(f"ساخت {persian_digits(listing['build_year'])}")
    for key, label in (
        ("has_parking", "پارکینگ"),
        ("has_elevator", "آسانسور"),
        ("has_storage", "انباری"),
    ):
        if listing.get(key) is True:
            details.append(label)

    return {
        "main": facts,
        "details": details,
        "price": price_text(listing),
        "code": persian_digits(listing["unit_code"]),
    }


def call_to_action(listing):
    return f"برای دریافت مشخصات، عدد {persian_digits(listing['unit_code'])} را ارسال کنید"


# ---------------------------------------------------------------- text

def fallback_story_text(listing, facts):
    district = listing.get("district") or "تبریز"
    area = facts["main"][1] if len(facts["main"]) > 1 else ""
    headline = f"آپارتمان {area} در {district}".replace("  ", " ").strip()
    caption = "\n".join(
        [
            f"🏡 {headline}",
            "📍 " + " • ".join(facts["main"]),
            ("✨ " + " • ".join(facts["details"])) if facts["details"] else "",
            f"💰 {facts['price']}",
            "",
            f"📩 {call_to_action(listing)}",
        ]
    ).replace("\n\n\n", "\n\n")
    return {"headline": headline, "caption": caption}


def openai_story_text(listing, facts):
    """Headline and caption from OpenAI, or the template when it is unavailable."""

    fallback = fallback_story_text(listing, facts)
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback

    instructions = """
You write short Persian Instagram story copy for a real-estate channel in Tabriz.
Use ONLY the facts given. Never invent features, prices, addresses, phone numbers,
agency or person names, and never change the price. Return ONLY one JSON object:
{"headline": "...", "caption": "..."}
- headline: catchy Persian title, at most 32 characters, no emoji, no hashtags,
  no price.
- caption: an Instagram caption of 3 to 5 short Persian lines describing the home
  from the facts (emoji allowed), then a line with the given call to action
  exactly as written, then 4 to 6 relevant Persian hashtags on one line.
""".strip()
    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": json.dumps(
            {
                "facts": facts["main"] + facts["details"],
                "price": facts["price"],
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

    headline = _EMOJI.sub("", str(parsed.get("headline") or "")).strip()
    caption = str(parsed.get("caption") or "").strip()
    if not headline or len(headline) > 40:
        headline = fallback["headline"]
    if not caption:
        caption = fallback["caption"]
    elif listing["unit_code"] not in caption.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")):
        caption = f"{caption}\n\n📩 {call_to_action(listing)}"
    return {"headline": headline, "caption": caption}


# ---------------------------------------------------------------- image

def _font(path, size):
    # Text is shaped with arabic_reshaper/bidi first, so the basic layout
    # engine must draw it as given (raqm would reorder it a second time).
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)


def _shape(text):
    return get_display(arabic_reshaper.reshape(text))


def _wrap(text, font, max_width, max_lines):
    words = text.split()
    lines = []
    current = ""
    for word in words:
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
    width = font.getlength(shaped)
    draw.text((right - width, top), shaped, font=font, fill=fill)
    return font.getbbox(shaped)[3]


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


def render_story(photos, listing, headline, brand):
    """photos: list of image bytes, first is the main photo. Returns JPEG bytes."""

    width, height = STORY_SIZE
    margin = 60
    right = width - margin
    canvas = Image.new("RGB", STORY_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    facts = story_facts(listing)

    images = []
    for data in photos:
        try:
            images.append(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB"))
        except Exception:
            logging.warning("Story photo could not be read; skipped")
    if not images:
        raise ValueError("no readable photo")

    headline_font = _font(FONT_BLACK, 68)
    headline_lines = _wrap(headline, headline_font, width - 2 * margin, 2)
    fact_font = _font(FONT_MEDIUM, 42)
    main_text = "  •  ".join(facts["main"])
    detail_text = "  •  ".join(facts["details"])
    detail_font = _fit_font(FONT_REGULAR, detail_text, width - 2 * margin, 36, 26)
    price_font = _fit_font(FONT_BLACK, facts["price"], width - 2 * margin, 74, 48)
    extra = images[1:4]

    # The text block sits right above the call to action, which stays clear
    # of Instagram's reply bar; the main photo fills everything above it.
    cta_height = 104
    cta_top = 1540
    block_height = (
        96 * len(headline_lines)
        + (70 if main_text else 0)
        + (64 if detail_text else 0)
        + (252 if extra else 0)
        + 125
    )
    y = cta_top - 30 - block_height

    photo_height = min(max(y + 180, 900), height)
    main = ImageOps.fit(images[0], (width, photo_height), Image.LANCZOS)
    canvas.paste(main, (0, 0))
    fade_length = 420
    fade_start = photo_height - fade_length
    fade = Image.linear_gradient("L").resize((width, fade_length))
    shade = Image.new("RGB", (width, fade_length), BACKGROUND)
    canvas.paste(shade, (0, fade_start), fade)

    # Brand tag inside Instagram's top safe zone.
    if brand:
        brand_font = _font(FONT_MEDIUM, 34)
        brand_text = f"@{brand}"
        brand_width = brand_font.getlength(brand_text)
        draw.rounded_rectangle(
            (right - brand_width - 40, 260, right, 320), radius=30, fill=(0, 0, 0)
        )
        draw.text((right - brand_width - 20, 266), brand_text, font=brand_font, fill=TEXT)

    for line in headline_lines:
        _draw_rtl(draw, line, headline_font, right, y, TEXT)
        y += 96
    if main_text:
        _draw_rtl(draw, main_text, fact_font, right, y + 10, MUTED)
        y += 70
    if detail_text:
        _draw_rtl(draw, detail_text, detail_font, right, y + 10, MUTED)
        y += 64

    # Up to three more photos in a row.
    if extra:
        y += 24
        gap = 18
        thumb_width = (width - 2 * margin - gap * (len(extra) - 1)) // len(extra)
        thumb_height = 200
        x = margin
        for image in extra:
            thumb = _rounded(ImageOps.fit(image, (thumb_width, thumb_height), Image.LANCZOS), 24)
            canvas.paste(thumb, (x, y), thumb)
            x += thumb_width + gap
        y += thumb_height + 28

    _draw_rtl(draw, facts["price"], price_font, right, y, ACCENT)

    cta = call_to_action(listing)
    cta_font = _fit_font(FONT_BLACK, cta, width - 2 * margin - 60, 44, 28)
    draw.rounded_rectangle(
        (margin, cta_top, width - margin, cta_top + cta_height), radius=52, fill=ACCENT
    )
    shaped = _shape(cta)
    text_width = cta_font.getlength(shaped)
    box = cta_font.getbbox(shaped)
    draw.text(
        ((width - text_width) / 2, cta_top + (cta_height - (box[3] - box[1])) / 2 - box[1]),
        shaped,
        font=cta_font,
        fill=ACCENT_TEXT,
    )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


# ---------------------------------------------------------------- telegram

def story_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 یکی دیگه", callback_data=INSTAGRAM_STORY_ACTION)],
            [InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")],
        ]
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


async def show_instagram_story(query, bot):
    chat_id = query.message.chat_id
    status = await bot.send_message(chat_id, "⏳ در حال ساخت استوری…")

    destination = get_destination_channel()
    brand = str(destination[2] or "").lstrip("@") if destination else ""

    tried = []
    try:
        for _attempt in range(STORY_PICK_ATTEMPTS):
            listing = await asyncio.to_thread(random_story_listing, tried)
            if not listing:
                break
            tried.append(listing["id"])

            photos = await _download_photos(bot, listing["photo_file_ids"])
            if not photos:
                continue

            facts = story_facts(listing)
            text = await asyncio.to_thread(openai_story_text, listing, facts)
            try:
                image = await asyncio.to_thread(
                    render_story, photos, listing, text["headline"], brand
                )
            except ValueError:
                continue

            await bot.send_photo(
                chat_id,
                photo=io.BytesIO(image),
                caption=text["caption"][:1024],
                reply_markup=story_keyboard(),
            )
            await bot.send_document(
                chat_id,
                document=io.BytesIO(image),
                filename=f"story-{listing['unit_code']}.jpg",
                caption="📎 نسخهٔ باکیفیت برای آپلود در اینستاگرام",
            )
            logging.info(
                "Instagram story created: property_id=%s unit_code=%s",
                listing["id"],
                listing["unit_code"],
            )
            return

        await bot.send_message(
            chat_id,
            "⚠️ فایل فعال عکس‌داری برای ساخت استوری پیدا نشد.",
            reply_markup=story_keyboard(),
        )
    finally:
        try:
            await status.delete()
        except Exception:
            pass
