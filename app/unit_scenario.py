"""
Settings › «🎬 سناریو ریلز»: the owner sends a file code and gets a
30-second Reels script for that listing, to record himself on camera in a
friendly, casual voice: the hook, timed scenes (what to film, what to say,
the text on screen), the closing call to action, a cover title, the
caption and recording tips.

Like the stories, the script uses only the listing's own facts and never
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
    story_facts,
    story_region,
    story_titles,
)

UNIT_SCENARIO_ACTION = "unit_scenario"
UNIT_SCENARIO_AGAIN_PREFIX = "unit_scenario_again_"
UNIT_SCENARIO_STATE = "unit_scenario_code"
SCENARIO_SECONDS = 30
TELEGRAM_TEXT_LIMIT = 4000

ANGLES = (
    "از نور و دلبازی خونه شروع کن",
    "از حس زندگی کردن توی این خونه شروع کن",
    "از یه سؤال از بیننده شروع کن",
    "از منطقه و دسترسی‌هاش شروع کن",
    "از مهم‌ترین امکان خونه شروع کن",
)

INSTRUCTIONS = f"""
You write a {SCENARIO_SECONDS}-second Instagram Reels script in Persian for a
real-estate agent in Tabriz who talks to the camera himself. Tone: warm,
friendly and casual (صمیمی و خودمونی), like talking to a friend; short
natural spoken sentences, no formal or literary words.

Use ONLY the facts given. Never invent features, rooms, views or places.
Never mention the price or any amount of money: viewers must send the code
to get it. Never write phone numbers, other agencies, channel names or links.

Write plain text for Telegram (no markdown symbols like * or #, except the
hashtags), exactly in this order:

🎣 هوک (۰ تا ۳ ثانیه)
🗣 one spoken line that stops the scroll
🔤 متن روی تصویر: up to 5 words

🎬 صحنه‌ها
4 or 5 scenes with Persian time ranges that end at ۲۷ ثانیه; for each:
⏱ the time range
📍 what to film (a concrete shot of this home)
🗣 what to say
🔤 text on screen (up to 5 words)

📣 پایان (۲۷ تا ۳۰ ثانیه)
🗣 the call to action, word for word as given

📝 تیتر کاور: up to 5 words
✍️ کپشن: 3 or 4 short lines, then the call to action, then 5 to 7 Persian hashtags
💡 نکته‌های ضبط: 2 or 3 short tips (vertical video, light, steady movement)

All spoken lines together must fit in {SCENARIO_SECONDS} seconds (about 70 to 80 words).
Return ONLY the script.
""".strip()


def call_to_action(listing):
    code = persian_digits(short_code(listing))
    return f"برای قیمت و مشخصات، عدد {code} رو کامنت کنید یا دایرکت بدید"


def scenario_header(listing):
    tag, headline = story_titles(listing)
    code = persian_digits(listing.get("unit_code") or short_code(listing))
    return f"🎬 سناریو ریلز {persian_digits(SCENARIO_SECONDS)} ثانیه‌ای | کد {code}\n🏡 {headline} | {tag}"


def fallback_scenario(listing):
    """A plain script from the listing's facts when OpenAI is unavailable."""

    tag, headline = story_titles(listing)
    region = story_region(listing) or "تبریز"
    facts = story_facts(listing)
    extras = [fact for fact in facts if fact not in (region,) and fact not in headline][:4]
    extra_line = "، ".join(extras) if extras else "یه خونهٔ تمیز و آماده"
    cta = call_to_action(listing)
    return "\n".join([
        "🎣 هوک (۰ تا ۳ ثانیه)",
        f"🗣 «یه خونهٔ {headline} توی {region} پیدا کردم که باید ببینید!»",
        f"🔤 متن روی تصویر: {headline} در {region}",
        "",
        "🎬 صحنه‌ها",
        "⏱ ۳ تا ۸ ثانیه",
        "📍 از در ورودی وارد شو و آروم کل پذیرایی رو نشون بده",
        "🗣 «ببینید چه فضای دلبازی داره.»",
        f"🔤 {headline}",
        "",
        "⏱ ۸ تا ۱۵ ثانیه",
        "📍 آشپزخونه و نور پنجره‌ها",
        f"🗣 «{extra_line}.»",
        "🔤 امکانات کامل",
        "",
        "⏱ ۱۵ تا ۲۲ ثانیه",
        "📍 اتاق‌خواب‌ها، یکی‌یکی",
        "🗣 «اتاق‌ها هم جادار و آماده‌ن.»",
        "🔤 آمادهٔ سکونت",
        "",
        "⏱ ۲۲ تا ۲۷ ثانیه",
        "📍 خودت جلوی دوربین، کنار پنجره",
        f"🗣 «موقعیتش هم توی {region}ه؛ دسترسی عالی.»",
        f"🔤 {tag}",
        "",
        "📣 پایان (۲۷ تا ۳۰ ثانیه)",
        f"🗣 «{cta}»",
        "",
        f"📝 تیتر کاور: {headline} در {region}",
        f"✍️ کپشن: یه خونهٔ {headline} توی {region} 🏡",
        f"{extra_line}",
        cta,
        "#املاک_تبریز #خرید_آپارتمان #تبریز #خانه_رویایی",
        "",
        "💡 نکته‌های ضبط: گوشی رو عمودی بگیر، روز و با نور طبیعی فیلم بگیر، آروم و بدون تکون راه برو.",
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
                "angle": rng.choice(ANGLES),
                "call_to_action": call_to_action(listing),
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
        script = f"{script}\n\n📣 {call_to_action(listing)}"
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
        f"یه سناریوی {persian_digits(SCENARIO_SECONDS)} ثانیه‌ای صمیمی می‌نویسم که خودتون "
        "جلوی دوربین بگید: هوک، صحنه‌به‌صحنه، متن روی تصویر، تیتر و کپشن."
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
