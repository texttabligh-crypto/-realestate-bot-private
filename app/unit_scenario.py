"""
Settings › «🎬 سناریو ریلز»: the owner sends a file code and gets what to
say in a Reels video about that listing, recorded himself on camera: two
strong hooks to choose from, a short flowing spoken text and the closing
line with the short code, plus a cover title and caption. Under 30
seconds, shorter when the facts are few; never an advert, never a
robotic list of specs. The pictures and scenes are his: most listings
have no video, so the script has none.

Like the stories, the text uses only the listing's own facts and never
its price: viewers send the code to get it.
"""

import asyncio
import json
import logging
import os
import random
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from instagram_story import (
    LATIN_DIGITS,
    OPENAI_MODEL,
    OPENAI_URL,
    _PRICE_WORDS,
    listing_by_code,
    listing_by_id,
    persian_digits,
    short_code,
    story_amenities,
    story_facts,
    story_region,
    story_stats,
    story_titles,
)

UNIT_SCENARIO_ACTION = "unit_scenario"
UNIT_SCENARIO_AGAIN_PREFIX = "unit_scenario_again_"
UNIT_SCENARIO_STATE = "unit_scenario_code"
SCENARIO_SECONDS = 30
TELEGRAM_TEXT_LIMIT = 4000

# A different way into the hook each time, for «🔄 یه سناریوی دیگه».
HOOK_STYLES = (
    "a question about a real need that this home's facts answer",
    "the single most useful or surprising fact, said plainly",
    "like telling a friend about a home you just saw",
    "an honest one-line opinion about who this home suits",
)

INSTRUCTIONS = f"""
You write what a real-estate agent in Tabriz says to the camera in an
Instagram Reels video about one home, in spoken Persian (محاوره‌ای), warm and
casual, like telling a friend about a home he just saw. He films the pictures
himself: write NO scenes, shots, camera directions or on-screen text.

It must NOT sound like an advert or a robot:
- No sales clichés: فرصت استثنایی، فرصت طلایی، از دست ندید، بی‌نظیر، رویایی،
  فوق‌العاده، لاکچری، همین حالا، عجله کنید، تکرارنشدنی.
- Do not read out a list of specs. Pick the 3 or 4 facts that matter most and
  weave them into natural sentences. Never say things like «مشخصاتش رو مرور
  کنیم»، «اینا هم از مشخصاتشه»، «همه‌ی این مشخصات».
- Name the neighbourhood at most once in the spoken text.
- A short honest opinion that follows from the facts is fine (who it suits).

The hook must stop the scroll in the first 3 seconds: concrete, curious,
specific to this home; never a bland «دنبال خونه‌ای؟».

Use ONLY the facts given; never invent features, rooms, views or places.
Never mention the price or any amount of money: viewers send the code to get
it. Never write phone numbers, other agencies, channel names or links.

Everything spoken (hook + text + closing line) must take under
{SCENARIO_SECONDS} seconds: about 45 to 65 Persian words in all. Fewer facts
means a shorter text; never pad.

Write plain text for Telegram (no markdown symbols like * or #, except the
hashtags), exactly in this order:

🎣 هوک (۳ ثانیهٔ اول)
۱) one hook
۲) another hook, in a different style

🗣 متن
the spoken text, one or two short paragraphs

📣 آخرش بگو
the closing line, word for word as given

📝 تیتر کاور: up to 5 words
✍️ کپشن:
2 or 3 short natural lines (no clichés), the closing line, then 4 to 6 Persian hashtags

Style example only (never copy its facts): for «ولیعصر، ۸۵ متر، ۲ خواب،
طبقه ۳، ساخت ۱۴۰۰، آسانسور، پارکینگ» a good text is: «۸۵ متره با دو تا خواب،
ساخت ۱۴۰۰. طبقهٔ سومه ولی آسانسور داره، پارکینگم داره؛ برای یه زوج جوون یا یه
خانوادهٔ کوچیک دقیقاً همون چیزیه که لازمه.»

Return ONLY the script.
""".strip()


def call_to_action(listing):
    code = persian_digits(short_code(listing))
    return f"اگه قیمتش رو می‌خوای، عدد {code} رو کامنت کن یا دایرکت بده"


def scenario_header(listing):
    tag, headline = story_titles(listing)
    code = persian_digits(listing.get("unit_code") or short_code(listing))
    return (
        f"🎬 متن ریلز (زیر {persian_digits(SCENARIO_SECONDS)} ثانیه) | کد {code}\n"
        f"🏡 {headline} | {tag}"
    )


def _spoken_details(listing):
    """«طبقه ۱ از ۴، ساخت ۱۳۹۲؛ آسانسور و پارکینگ هم داره.» or None."""

    stats = [text for _value, _label, text in story_stats(listing)][:2]
    amenities = story_amenities(listing)[:2]
    parts = []
    if stats:
        parts.append("، ".join(stats))
    if amenities:
        parts.append(" و ".join(amenities) + " هم داره")
    return "؛ ".join(parts) + "." if parts else None


def fallback_scenario(listing):
    """A plain script from the listing's facts when OpenAI is unavailable."""

    _tag, headline = story_titles(listing)
    region = story_region(listing) or "تبریز"
    details = _spoken_details(listing)
    cta = call_to_action(listing)
    area = listing.get("area_m2")
    area = persian_digits(int(float(area))) if area else None
    bedrooms = listing.get("bedrooms")
    bedrooms = persian_digits(bedrooms) if bedrooms else None
    home = "یه خونهٔ" + (f" {area} متری" if area else "") + (f" {bedrooms}خوابه" if bedrooms else "")
    if area and bedrooms:
        opening = f"{area} متره با {bedrooms} تا خواب، توی {region}."
    elif area:
        opening = f"{area} متره، توی {region}."
    else:
        opening = f"توی {region}ه."
    return "\n".join([
        "🎣 هوک (۳ ثانیهٔ اول)",
        f"۱) «امروز {home} دیدم؛ بذار بگم چرا ارزش دیدن داره.»",
        f"۲) «توی {region} {home} پیدا کردم که حیفم اومد نشونت ندم.»",
        "",
        "🗣 متن",
        f"«{opening}" + (f" {details}" if details else "") + "»",
        "",
        "📣 آخرش بگو",
        f"«{cta}»",
        "",
        f"📝 تیتر کاور: {headline} | {region}",
        "✍️ کپشن:",
        f"{headline} توی {region}",
        *([details] if details else []),
        cta,
        "#املاک_تبریز #خرید_آپارتمان #تبریز",
    ])


def _response_text(data):
    text = ""
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                text = content["text"]
    return text


def write_scenario(listing, rng=random):
    """The Reels script for listing, from OpenAI or the plain template."""

    fallback = fallback_scenario(listing)
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback
    headline = story_titles(listing)[1]
    payload = {
        "model": OPENAI_MODEL,
        "instructions": INSTRUCTIONS,
        "input": json.dumps(
            {
                "facts": story_facts(listing),
                "region": story_region(listing),
                "headline": headline,
                "first_hook_style": rng.choice(HOOK_STYLES),
                "closing_line": call_to_action(listing),
            },
            ensure_ascii=False,
        ),
        "max_output_tokens": 3000,
        "store": False,
    }
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.warning("OpenAI Reels scenario failed: %s", exc)
        return fallback

    # Keep the price out whatever the model wrote.
    lines = [line for line in _response_text(data).splitlines() if not _PRICE_WORDS.search(line)]
    script = "\n".join(lines).strip()
    if not script:
        return fallback
    if short_code(listing) not in script.translate(LATIN_DIGITS):
        script = f"{script}\n\n📣 آخرش بگو\n«{call_to_action(listing)}»"
    return script


def scenario_message(listing, script):
    text = f"{scenario_header(listing)}\n\n{script}"
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = text[:TELEGRAM_TEXT_LIMIT - 1].rstrip() + "…"
    return text


def scenario_keyboard(listing=None):
    rows = []
    if listing:
        rows.append([InlineKeyboardButton(
            "🔄 یه سناریوی دیگه برای همین فایل",
            callback_data=f"{UNIT_SCENARIO_AGAIN_PREFIX}{listing['id']}",
        )])
    rows.append([InlineKeyboardButton("🎬 سناریو برای کد دیگه", callback_data=UNIT_SCENARIO_ACTION)])
    rows.append([InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")])
    return InlineKeyboardMarkup(rows)


def code_prompt():
    return (
        "🎬 سناریو ریلز\n\n"
        "کد فایل رو بفرستید (مثلاً ۳۵۱۳ یا کد کوتاهش ۳۵۱).\n"
        f"یه متن روون و خودمونی زیر {persian_digits(SCENARIO_SECONDS)} ثانیه می‌نویسم که خودتون "
        "جلوی دوربین بگید: دو تا هوک قوی، متن اصلی و جملهٔ آخر، با تیتر کاور و کپشن. "
        "تصویرها و صحنه‌ها با خودتون."
    )


def _code_in(text):
    digits = "".join(ch for ch in str(text or "").translate(LATIN_DIGITS) if ch.isdigit())
    return digits or None


async def _write_and_send(bot, chat_id, listing, status):
    try:
        script = await asyncio.to_thread(write_scenario, listing)
        text = scenario_message(listing, script)
        logging.info("Reels scenario written: property_id=%s unit_code=%s", listing.get("id"), listing.get("unit_code"))
    except Exception:
        logging.exception("Reels scenario failed: property_id=%s", listing.get("id"))
        text = "⚠️ نوشتن سناریو انجام نشد؛ یه بار دیگه امتحان کنید 🙏"
    try:
        await status.edit_text(text, reply_markup=scenario_keyboard(listing))
    except Exception:
        await bot.send_message(chat_id, text, reply_markup=scenario_keyboard(listing))


def _start(context, chat_id, listing, status):
    # Writing takes a while; other chats keep being answered meanwhile.
    context.application.create_task(_write_and_send(context.bot, chat_id, listing, status))


async def handle_unit_scenario_button(query, context):
    action = query.data or ""
    if action.startswith(UNIT_SCENARIO_AGAIN_PREFIX):
        property_id = action[len(UNIT_SCENARIO_AGAIN_PREFIX):]
        listing = await asyncio.to_thread(listing_by_id, property_id) if property_id.isdigit() else None
        if not listing:
            await query.edit_message_text("این فایل دیگه پیدا نشد.", reply_markup=scenario_keyboard())
            return
        await query.edit_message_reply_markup(reply_markup=None)
        status = await query.message.reply_text("⏳ دارم یه سناریوی تازه می‌نویسم…")
        _start(context, query.message.chat_id, listing, status)
        return

    context.user_data["state"] = UNIT_SCENARIO_STATE
    await query.edit_message_text(code_prompt(), reply_markup=scenario_keyboard())


async def handle_unit_scenario_text(update, context, allowed):
    if not allowed:
        context.user_data.pop("state", None)
        return
    code = _code_in(update.message.text)
    if not code:
        await update.message.reply_text("کد فایل رو به عدد بفرستید، مثلاً ۳۵۱۳ 🙏", reply_markup=scenario_keyboard())
        return
    listing = await asyncio.to_thread(listing_by_code, code)
    if not listing:
        await update.message.reply_text(
            f"فایلی با کد {persian_digits(code)} پیدا نکردم 🤔 کد رو دوباره بفرستید.",
            reply_markup=scenario_keyboard(),
        )
        return
    context.user_data.pop("state", None)
    status = await update.message.reply_text(
        f"⏳ دارم سناریوی ریلز کد {persian_digits(listing.get('unit_code') or code)} رو می‌نویسم…"
    )
    _start(context, update.effective_chat.id, listing, status)
