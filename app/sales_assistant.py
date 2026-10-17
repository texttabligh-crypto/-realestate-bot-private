"""A friendly sales assistant for the customer messages no rule answers.

Those messages used to go straight to an advisor as an unanswered question.
Now OpenAI answers first as ShahFile's warm Persian advisor: it sees the
recent conversation and the facts of the listings in it (or of the
customer's latest search), talks only from those facts, and steers toward
a visit. It hands the question to an advisor when the facts don't answer
it (price negotiation, loans, legal questions…) or the customer asks for a
person.

The reply is checked before it is sent: any price must be one it was given
or the customer said, and no phone number, handle or foreign link may
appear. A reply that fails the check, or no reply at all (no API key,
network error), leaves the message to the advisors as before.
"""

import json
import logging
import os
import re
import urllib.request
from decimal import Decimal, InvalidOperation

from listing_offer import find_listing_by_code, parse_listing_code
from publisher import build_channel_post_url, clean_publish_location, db_connect, get_destination_channel
from shahfile_ai import (
    _extract_response_text,
    _parse_json_object,
    get_active_search_context,
    normalize_search_filters,
    search_count,
    search_rows,
)

OPENAI_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
HISTORY_LIMIT = 12
LISTING_LIMIT = 3
REPLY_LIMIT = 900
ACTIONS = ("none", "visit", "search", "handoff")

_LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫", "01234567890123456789.")
# «کد ۳۰۸۲۵», «کد واحد: 30825», «فایل کد ۳۰۸۲۵».
_CODE_IN_TEXT = re.compile(r"کد(?:\s*(?:واحد|فایل))?\s*[:：#]?\s*(\d{3,7})(?!\d)")
_MONEY = re.compile(r"(\d+(?:\.\d+)?)\s*(میلیارد|ملیارد|میلیون|ملیون)")
_PHONE = re.compile(r"(?:\+?98|0)?9\d{2}[\s\-]?\d{3}[\s\-]?\d{4}")
_LINK = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.IGNORECASE)

# ShahFile has listings in Tabriz only.
OTHER_CITIES = (
    "تهران", "کرج", "اصفهان", "شیراز", "مشهد", "ارومیه", "اردبیل", "زنجان", "قم", "رشت",
    "اهواز", "کرمانشاه", "همدان", "قزوین", "یزد", "کرمان", "ساری", "گرگان", "کیش",
)
_OTHER_CITY = re.compile(r"(?<![\w\u200c])(" + "|".join(OTHER_CITIES) + r")(?![\w\u200c])")
# «خیابان تهران» or «جاده تهران» is a street or road, not the city.
_STREET_BEFORE = re.compile(r"(?:خیابان|خ\.|جاده|اتوبان|بزرگراه|بلوار|میدان|کوی|کوچه|شهرک|مجتمع|برج)\s*$")


def other_city_in(text):
    """The other city a customer wants a home in («شمال تهران»), else None."""

    raw = str(text or "")
    if "تبریز" in raw:
        return None
    for match in _OTHER_CITY.finditer(raw):
        if not _STREET_BEFORE.search(raw[: match.start()]):
            return match.group(1)
    return None

INSTRUCTIONS = """
You are ShahFile's real-estate advisor in Tabriz, chatting with a customer on
Telegram. Write casual, warm, polite Persian (محاوره‌ای، صمیمی و مؤدب), like a
friendly advisor who wants to help them find the right home.

ShahFile has listings in Tabriz only. If they want another city, say warmly
that we only work in Tabriz for now and ask whether Tabriz could suit them;
never search or promise other cities.

Goals, in order: understand what they need, build trust, suggest suitable
listings from the facts you are given, and move toward booking a visit
(بازدید). Every reply ends with one short question that moves things forward:
offer a visit for a listing they like, or ask for their budget, area or
number of bedrooms.

Hard rules:
- Use ONLY the facts in "listings" and "active_search". Never invent a
  listing, price, feature, address, discount, payment plan, loan, deed or
  legal status, owner, or phone number.
- Quote a price only exactly as a listing's price_billion gives it. Never
  negotiate or promise a discount.
- Never write phone numbers, Telegram handles, or links other than a
  listing's channel_link. Never name other agencies or channels.
- If the answer is not in the facts (negotiation, loans, legal questions,
  exact address, anything else unknown), or the customer asks for a person or
  is upset, say warmly that you'll ask a colleague and they'll answer here,
  and set action "handoff".
- If they want to see or visit a listing from "listings", set action "visit"
  with its code.
- If they describe what they want to buy (budget, size, rooms, area), set
  action "search" with the filters they gave. If "active_search_matches" is
  0, that search found nothing: don't offer it again, suggest widening the
  budget, size or area instead.
- If "codes_not_found" lists a code they asked about, that listing is no
  longer available (maybe sold): say so kindly and offer to find similar
  ones by asking their budget and area. Don't hand this off.
- 1 to 4 short sentences of plain text (no Markdown), at most two emoji.
  Don't greet again if the conversation has already started; use their first
  name now and then.

Return ONLY one JSON object:
{"reply": "...", "action": "none|visit|search|handoff", "unit_code": null,
 "filters": {"price_min_billion": null, "price_max_billion": null,
             "area_min": null, "area_max": null, "bedrooms": null,
             "district": null}}
""".strip()


def _latin(text):
    return str(text or "").translate(_LATIN_DIGITS)


def _without_phones(text):
    """Customers' phone numbers (a shared contact is logged too) never reach OpenAI."""

    return _PHONE.sub("[شماره]", _latin(text))


def recent_history(telegram_user_id, limit=HISTORY_LIMIT):
    """The latest messages with the customer, oldest first, phone numbers masked."""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT direction, body
                FROM customer_chat_history
                WHERE telegram_user_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (telegram_user_id, limit),
            )
            rows = cur.fetchall()
    return [{"from": direction, "text": _without_phones(body)[:500]} for direction, body in reversed(rows)]


def codes_in(history, message):
    """Listing codes named in the conversation, newest first."""

    codes = []
    for text in [message] + [item["text"] for item in reversed(history)]:
        for code in _CODE_IN_TEXT.findall(_latin(text)):
            if code not in codes:
                codes.append(code)
    return codes


def listing_facts(listing, username):
    amenities = [
        label
        for key, label in (("has_elevator", "آسانسور"), ("has_parking", "پارکینگ"), ("has_storage", "انباری"))
        if listing.get(key) is True
    ]
    price = listing.get("current_price_billion")
    return {
        "code": listing.get("unit_code"),
        "location": clean_publish_location(listing.get("address_text") or listing.get("district")),
        "area_m2": int(Decimal(str(listing["area_m2"]))) if listing.get("area_m2") else None,
        "bedrooms": listing.get("bedrooms"),
        "floor": listing.get("floor_text"),
        "build_year": listing.get("build_year"),
        "direction": listing.get("direction"),
        "amenities": amenities,
        "price_billion": f"{Decimal(str(price)).normalize():f}" if price else None,
        "channel_link": build_channel_post_url(username, listing.get("published_message_id"))
        or listing.get("channel_post_url"),
    }


def _search_listings(filters):
    keys = ("id", "unit_code", "district", "address_text", "area_m2", "bedrooms", "build_year",
            "current_price_billion", "channel_post_url", "published_message_id")
    return [dict(zip(keys, row)) for row in search_rows(filters, limit=LISTING_LIMIT)]


def build_context(telegram_user_id, first_name, message):
    history = recent_history(telegram_user_id)
    try:
        destination = get_destination_channel()
    except Exception:
        destination = None
    username = destination[2] if destination else None

    listings = []
    for code in codes_in(history, message)[:LISTING_LIMIT]:
        listing = find_listing_by_code(code)
        if listing:
            listings.append(listing)
    asked = parse_listing_code(message)
    codes_not_found = [asked[0]] if asked and not find_listing_by_code(asked[0]) else []

    search = get_active_search_context(telegram_user_id) or {}
    filters = normalize_search_filters(search.get("filters") or {})
    matches = search_count(filters) if filters else None
    if not listings and matches:
        listings = _search_listings(filters)
    return {
        "customer_first_name": first_name or None,
        "history": history,
        "listings": [listing_facts(item, username) for item in listings],
        "active_search": search.get("filters") or None,
        "active_search_matches": matches,
        "codes_not_found": codes_not_found,
        "customer_message": _without_phones(message),
    }, listings


def _ask_openai(context):
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "instructions": INSTRUCTIONS,
        "input": json.dumps(context, ensure_ascii=False, default=str),
        "max_output_tokens": 600,
        "store": False,
    }
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.warning("Sales assistant request failed: %s", exc)
        return None
    return _parse_json_object(_extract_response_text(data))


def _amount(value, unit):
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return (amount / 1000 if unit in ("میلیون", "ملیون") else amount).normalize()


def _numbers(text):
    return {Decimal(n).normalize() for n in re.findall(r"\d+(?:\.\d+)?", _latin(text))}


def reply_is_safe(reply, context):
    """
    True when every amount of money in reply is a listing price or a number
    the customer used, and it carries no phone number, handle or other link.
    """

    text = _latin(reply)
    if _PHONE.search(text) or "@" in text:
        return False
    allowed_links = {item.get("channel_link") for item in context["listings"] if item.get("channel_link")}
    for link in _LINK.findall(reply):
        if link.rstrip(".,)،") not in allowed_links:
            return False
    allowed = {
        Decimal(item["price_billion"]).normalize()
        for item in context["listings"] if item.get("price_billion")
    }
    allowed |= _numbers(context["customer_message"])
    allowed |= {n for item in context["history"] if item["from"] == "customer" for n in _numbers(item["text"])}
    for value in (context.get("active_search") or {}).values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            allowed.add(Decimal(str(value)).normalize())
    for value, unit in _MONEY.findall(text):
        amount = _amount(value, unit)
        if amount is None or amount not in allowed:
            return False
    return True


def assistant_turn(telegram_user_id, first_name, message):
    """
    The assistant's turn as {"reply", "action", "listing", "filters"}, or None
    when there is no usable reply and the advisors should answer.
    """

    context, listings = build_context(telegram_user_id, first_name, message)
    answer = _ask_openai(context)
    if not answer:
        return None
    reply = str(answer.get("reply") or "").strip()
    action = str(answer.get("action") or "none").strip()
    if action not in ACTIONS:
        action = "none"
    if not reply or len(reply) > REPLY_LIMIT or not reply_is_safe(reply, context):
        logging.warning("Sales assistant reply rejected: user=%s action=%s", telegram_user_id, action)
        return None

    listing = None
    if action == "visit":
        code = re.sub(r"\D", "", _latin(answer.get("unit_code") or ""))
        listing = next((item for item in listings if code and str(item.get("unit_code")) == code), None)
        if listing is None and len(listings) == 1:
            listing = listings[0]
        if listing is None:
            action = "none"
    filters = normalize_search_filters(answer.get("filters") or {}) if action == "search" else {}
    active = context.get("active_search") or {}
    # No filters, or the search the customer already has: offering it again
    # is the loop customers saw («۸۵ تا ۸۵ متر، تهران» → 0 → again).
    if action == "search" and (not filters or all(active.get(k) == v for k, v in filters.items())):
        action = "none"
    return {"reply": reply, "action": action, "listing": listing, "filters": filters}
