"""Instagram stories for random live listings.

Settings › «📸 استوری اینستاگرام» asks how many stories to make, picks that
many random live listings that have photos, and sends one 1080×1920 story per
listing. Each story is built from the listing's real photos and own facts;
OpenAI only writes the kicker, headline and caption (curated templates are
used when it is not available). The price is left out on purpose: the call
to action asks viewers to comment the unit code. No source text, agency name
or phone number is ever used, and photos are never generated or altered.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import urllib.request
from decimal import Decimal
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from publisher import db_connect, get_destination_channel

INSTAGRAM_STORY_ACTION = "insta_story"
INSTAGRAM_STORY_COUNT_PREFIX = "insta_story_n_"
INSTAGRAM_STORY_COUNT_STATE = "insta_story_count"
INSTAGRAM_STORY_PERMISSION = "instagram_story"
INSTAGRAM_STORY_TITLE = "📸 استوری اینستاگرام"

STORY_SIZE = (1080, 1920)
STORY_PHOTO_LIMIT = 4
STORY_MAX_COUNT = 10
STORY_COUNT_CHOICES = (1, 3, 5, 10)
STORY_SEND_SPACING_SECONDS = 1.5

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

PERSIAN_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")
LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
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


def random_story_listings(count):
    """Up to count*2 random live published listings with photos, as dicts."""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id, p.unit_code, p.district, p.area_m2, p.bedrooms,
                    p.floor_text, p.build_year, p.has_parking, p.has_elevator,
                    p.has_storage
                FROM properties p
                WHERE p.status = 'ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.unit_code IS NOT NULL
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
                (int(count) * 2,),
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


def render_story(photos, listing, kicker, headline, brand):
    """photos: list of image bytes, the first is the main photo. Returns JPEG bytes."""

    width, height = STORY_SIZE
    margin = 60
    right = width - margin
    canvas = Image.new("RGB", STORY_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    images = []
    for data in photos:
        try:
            images.append(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB"))
        except Exception:
            logging.warning("Story photo could not be read; skipped")
    if not images:
        raise ValueError("no readable photo")

    kicker_font = _fit_font(FONT_MEDIUM, kicker, width - 2 * margin, 40, 30)
    headline_font = _font(FONT_BLACK, 76)
    headline_lines = _wrap(headline, headline_font, width - 2 * margin, 2)
    chip_font = _font(FONT_MEDIUM, 34)
    chip_rows = _chip_rows(story_facts(listing), chip_font, width - 2 * margin)
    extra = images[1:4]

    # The text block sits right above the call to action, which stays clear
    # of Instagram's reply bar; the main photo fills everything above it.
    cta_height = 110
    cta_top = 1530
    block_height = (
        (64 if kicker else 0)
        + 104 * len(headline_lines)
        + 80 * len(chip_rows)
        + (250 if extra else 0)
        + 10
    )
    y = cta_top - 40 - block_height

    photo_height = min(max(y + 200, 900), height)
    canvas.paste(ImageOps.fit(images[0], (width, photo_height), Image.LANCZOS), (0, 0))
    fade_length = 460
    fade = Image.linear_gradient("L").resize((width, fade_length))
    canvas.paste(
        Image.new("RGB", (width, fade_length), BACKGROUND),
        (0, photo_height - fade_length),
        fade,
    )

    # Brand tag inside Instagram's top safe zone.
    if brand:
        brand_font = _font(FONT_MEDIUM, 34)
        brand_text = f"@{brand}"
        brand_width = brand_font.getlength(brand_text)
        draw.rounded_rectangle(
            (right - brand_width - 40, 260, right, 320), radius=30, fill=(0, 0, 0)
        )
        draw.text((right - brand_width - 20, 266), brand_text, font=brand_font, fill=TEXT)

    if kicker:
        _draw_rtl(draw, kicker, kicker_font, right, y, ACCENT)
        y += 64
    for line in headline_lines:
        _draw_rtl(draw, line, headline_font, right, y, TEXT)
        y += 104

    y += 6
    for row in chip_rows:
        x = right
        for chip, chip_width in row:
            draw.rounded_rectangle((x - chip_width, y, x, y + 62), radius=31, fill=CHIP)
            shaped = _shape(chip)
            box = chip_font.getbbox(shaped)
            draw.text(
                (x - chip_width + 28, y + (62 - (box[3] - box[1])) / 2 - box[1]),
                shaped,
                font=chip_font,
                fill=TEXT,
            )
            x -= chip_width + 16
        y += 80

    # Up to three more photos in a row.
    if extra:
        y += 22
        gap = 18
        thumb_width = (width - 2 * margin - gap * (len(extra) - 1)) // len(extra)
        thumb_height = 210
        x = margin
        for image in extra:
            thumb = _rounded(ImageOps.fit(image, (thumb_width, thumb_height), Image.LANCZOS), 26)
            canvas.paste(thumb, (x, y), thumb)
            x += thumb_width + gap

    cta = call_to_action(listing)
    cta_font = _fit_font(FONT_BLACK, cta, width - 2 * margin - 60, 44, 28)
    draw.rounded_rectangle(
        (margin, cta_top, width - margin, cta_top + cta_height), radius=55, fill=ACCENT
    )
    shaped = _shape(cta)
    box = cta_font.getbbox(shaped)
    draw.text(
        ((width - cta_font.getlength(shaped)) / 2,
         cta_top + (cta_height - (box[3] - box[1])) / 2 - box[1]),
        shaped,
        font=cta_font,
        fill=ACCENT_TEXT,
    )

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


def story_done_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 ساخت استوری‌های بیشتر", callback_data=INSTAGRAM_STORY_ACTION)],
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


async def make_stories(bot, chat_id, count):
    """Pick count random listings and send one story each, one by one."""

    status = await bot.send_message(chat_id, f"⏳ در حال ساخت {persian_digits(count)} استوری…")
    destination = get_destination_channel()
    brand = str(destination[2] or "").lstrip("@") if destination else ""
    made = 0

    try:
        listings = await asyncio.to_thread(random_story_listings, count)
        for listing in listings:
            if made >= count:
                break
            photos = await _download_photos(bot, listing["photo_file_ids"])
            if not photos:
                continue
            text = await asyncio.to_thread(openai_story_text, listing)
            try:
                image = await asyncio.to_thread(
                    render_story, photos, listing, text["kicker"], text["headline"], brand
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
                caption=f"📎 فایل باکیفیت استوری {persian_digits(made)} (کد {persian_digits(listing['unit_code'])})",
            )
            logging.info(
                "Instagram story created: property_id=%s unit_code=%s",
                listing["id"],
                listing["unit_code"],
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
        summary = f"✅ {persian_digits(made)} استوری ساخته شد."
    elif made:
        summary = (
            f"✅ {persian_digits(made)} استوری ساخته شد؛ "
            "فایل فعال عکس‌دار بیشتری پیدا نشد."
        )
    else:
        summary = "⚠️ فایل فعال عکس‌داری برای ساخت استوری پیدا نشد."
    await bot.send_message(chat_id, summary, reply_markup=story_done_keyboard())


def _start_batch(context, chat_id, count):
    running = context.application.bot_data.setdefault("instagram_story_chats", set())
    if chat_id in running:
        return False
    running.add(chat_id)

    async def run():
        try:
            await make_stories(context.bot, chat_id, count)
        finally:
            running.discard(chat_id)

    # A batch takes a while; run it beside the update handlers.
    context.application.create_task(run())
    return True


async def handle_instagram_story_button(query, context):
    action = query.data or ""
    chat_id = query.message.chat_id

    if action.startswith(INSTAGRAM_STORY_COUNT_PREFIX):
        context.user_data.pop("state", None)
        count = parse_story_count(action[len(INSTAGRAM_STORY_COUNT_PREFIX):])
        if count and not _start_batch(context, chat_id, count):
            await query.message.reply_text("⏳ استوری‌های قبلی هنوز در حال ساخت‌اند.")
        return

    context.user_data["state"] = INSTAGRAM_STORY_COUNT_STATE
    await query.edit_message_text(
        f"{INSTAGRAM_STORY_TITLE}\n\n"
        "چند تا استوری بسازم؟\n"
        f"یکی از دکمه‌ها رو بزنید یا عددش رو بنویسید (۱ تا {persian_digits(STORY_MAX_COUNT)}).\n\n"
        "ربات خودش فایل‌ها رو رندوم انتخاب می‌کنه، با عکس‌های واقعی طراحی می‌کنه "
        "و یکی‌یکی می‌فرسته. قیمت روی استوری نمیاد تا مخاطب کد رو کامنت کنه.",
        reply_markup=story_count_keyboard(),
    )


async def handle_instagram_story_count_text(update, context, allowed):
    context.user_data.pop("state", None)
    if not allowed:
        return

    count = parse_story_count(update.message.text)
    if not count:
        context.user_data["state"] = INSTAGRAM_STORY_COUNT_STATE
        await update.message.reply_text(
            f"لطفاً یه عدد بین ۱ تا {persian_digits(STORY_MAX_COUNT)} بفرستید.",
            reply_markup=story_count_keyboard(),
        )
        return

    if not _start_batch(context, update.effective_chat.id, count):
        await update.message.reply_text("⏳ استوری‌های قبلی هنوز در حال ساخت‌اند.")
