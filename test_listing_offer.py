"""A customer asking about a listing's code gets the listing and a visit offer.

«کد ۳۵۱۳» went to an advisor as an unanswered question. Now «کد ۳۵۱۳»,
«۳۵۱۳» or a story's short code «۳۵۱» gets the listing's photo, facts, price
and channel link with «می‌خواید براتون یه بازدید هماهنگ کنم؟»; «آره» or the
button files a visit request like the channel post's visit link does.

The database test runs against a scratch PostgreSQL given in PGTEST_DSN and is
skipped otherwise.
"""
import ast
import asyncio
import os
import re
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

try:
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None

APP = Path(__file__).parent / "app"
DSN = os.getenv("PGTEST_DSN")


class Button:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data = text, callback_data


class Markup:
    def __init__(self, rows):
        self.rows = rows


def load_offer_module(connect=None):
    sys.path.insert(0, str(APP))
    stubs = {
        "telegram": types.SimpleNamespace(InlineKeyboardButton=Button, InlineKeyboardMarkup=Markup),
        "publisher": types.SimpleNamespace(
            db_connect=connect,
            get_destination_channel=lambda: (-100, "NIMAT", "nimatmelk"),
            build_channel_post_url=lambda username, message_id: (
                f"https://t.me/{username.lstrip('@')}/{message_id}" if username and message_id else None),
            clean_publish_location=lambda value: str(value).replace("#", "").strip() if value else value,
        ),
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("listing_offer", None)
        import listing_offer
    return listing_offer


LISTING = dict(
    id=357, unit_code="30825", district="ولیعصر پروین اعتصامی", address_text="ولیعصر پروین اعتصامی",
    area_m2=Decimal("125.00"), bedrooms=2, floor_text="2", build_year=None, direction="شمالی",
    has_parking=True, has_elevator=True, has_storage=None, current_price_billion=Decimal("25.000"),
    published_message_id=1620, channel_post_url=None, photo_file_id="photo-357",
)


class ListingOfferTest(unittest.TestCase):
    def setUp(self):
        self.offer = load_offer_module()

    def test_codes_customers_send(self):
        parse = self.offer.parse_listing_code
        for text, expected in (
            ("کد ۳۵۱۳", ("3513", True)),
            ("کد واحد: 30825", ("30825", True)),
            ("سلام کد۳۵۱۳ رو میخواستم", ("3513", True)),
            ("فایل ۳۲۰", ("320", True)),
            ("کد ۳۵۱۳ چند متره؟", ("3513", True)),
            ("۳۵۱۳", ("3513", False)),
            ("۳۲۰", ("320", False)),
            ("مشخصات ۳۲۰ لطفا", ("320", False)),
            ("قیمت ۳۲۰ چنده؟", ("320", False)),
        ):
            self.assertEqual(parse(text), expected, text)

    def test_budgets_areas_years_and_visits_are_left_alone(self):
        parse = self.offer.parse_listing_code
        for text in (
            "۱۲", "100", "۲۰ تا ۲۵ میلیارد", "واحد ۱۲۰ متری", "۱۲۰ متر", "ساخت ۱۴۰۵", "بالای ۲۰۰",
            "۳ خواب ۱۵۰ متر", "09141234567", "سلام", "", "برای کد ۲۷۲۹ وقت بازدید می‌خوام",
            "من دنبال یه خونه خوب در ولیعصر هستم که حدود ۳۵۰ باشه",
        ):
            self.assertIsNone(parse(text), text)

    def test_answers_to_the_visit_offer(self):
        reply = self.offer.offer_reply
        for text in ("آره", "اره", "بله", "حتما", "حتماً!", "اوکی", "باشه", "آره میخوام",
                     "بله لطفا", "هماهنگ کنید", "بازدید میخوام", "میخوام"):
            self.assertEqual(reply(text), "yes", text)
        for text in ("نه", "نه ممنون", "فعلا نه", "نمیخوام", "بعدا"):
            self.assertEqual(reply(text), "no", text)
        for text in ("قیمتش قابل مذاکره هست؟", "۲۰ تا ۲۵ میلیارد", ""):
            self.assertIsNone(reply(text), text)

    def test_offer_text_has_the_facts_price_link_and_question(self):
        text = self.offer.listing_offer_text(LISTING, "nimatmelk")
        self.assertEqual(text.splitlines(), [
            "اینم مشخصات فایل کد ۳۰۸۲۵ 😊",
            "",
            "📍 ولیعصر پروین اعتصامی",
            "📐 ۱۲۵ متر | 🛏 ۲ خواب | 🏢 طبقه ۲",
            "🧭 شمالی",
            "✅ آسانسور • پارکینگ",
            "💰 قیمت: ۲۵ میلیارد تومان",
            "🔗 مشاهده در کانال: https://t.me/nimatmelk/1620",
            "",
            "می‌خواید براتون یه بازدید هماهنگ کنم؟ 🏡",
        ])
        self.assertEqual(self.offer.format_billion(Decimal("5.600")), "۵٫۶")
        bare = dict(LISTING, area_m2=None, bedrooms=None, floor_text=None, direction=None,
                    has_parking=None, has_elevator=None, current_price_billion=None,
                    published_message_id=None, channel_post_url="https://t.me/nimatmelk/9")
        self.assertEqual(self.offer.listing_offer_text(bare, None).splitlines()[2:4],
                         ["📍 ولیعصر پروین اعتصامی", "🔗 مشاهده در کانال: https://t.me/nimatmelk/9"])

    def test_buttons_offer_a_visit_and_similar_listings(self):
        rows = self.offer.listing_offer_keyboard(357).rows
        self.assertEqual([(b.text, b.callback_data) for row in rows for b in row], [
            ("✅ آره، بازدید می‌خوام", "sc_offer_visit_357"),
            ("🔍 فایل‌های مشابه", "sc_offer_similar_357"),
        ])
        self.assertEqual(self.offer.callback_property_id("sc_offer_visit_357", "sc_offer_visit_"), 357)
        self.assertIsNone(self.offer.callback_property_id("sc_offer_visit_x", "sc_offer_visit_"))

    def test_sends_the_photo_with_the_offer_and_falls_back_to_text(self):
        sent = []

        class Message:
            def __init__(self, photo_fails=False):
                self.photo_fails = photo_fails

            async def reply_photo(self, photo, caption, reply_markup):
                if self.photo_fails:
                    raise RuntimeError("wrong file id")
                sent.append(("photo", photo, caption.splitlines()[0]))

            async def reply_text(self, text, reply_markup, disable_web_page_preview):
                sent.append(("text", text.splitlines()[0]))

        asyncio.run(self.offer.send_listing_offer(Message(), LISTING, "nimatmelk"))
        asyncio.run(self.offer.send_listing_offer(Message(photo_fails=True), LISTING, "nimatmelk"))
        asyncio.run(self.offer.send_listing_offer(Message(), dict(LISTING, photo_file_id=None), None))
        self.assertEqual(sent, [
            ("photo", "photo-357", "اینم مشخصات فایل کد ۳۰۸۲۵ 😊"),
            ("text", "اینم مشخصات فایل کد ۳۰۸۲۵ 😊"),
            ("text", "اینم مشخصات فایل کد ۳۰۸۲۵ 😊"),
        ])


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class ListingLookupDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute("""
                DROP TABLE IF EXISTS property_media, property_trash, properties CASCADE;
                CREATE TABLE properties (
                    id BIGINT PRIMARY KEY, unit_code TEXT, internal_sequence INT, district TEXT,
                    address_text TEXT, area_m2 NUMERIC, bedrooms INT, floor_text TEXT,
                    build_year INT, direction TEXT, has_parking BOOL, has_elevator BOOL,
                    has_storage BOOL, current_price_billion NUMERIC, published_message_id BIGINT,
                    channel_post_url TEXT, status TEXT, deleted_from_channel_at TIMESTAMPTZ);
                CREATE TABLE property_media (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, telegram_file_id TEXT,
                    media_type TEXT DEFAULT 'PHOTO', position INT DEFAULT 0);
                CREATE TABLE property_trash (property_id BIGINT PRIMARY KEY);
                INSERT INTO properties (id, unit_code, internal_sequence, status, published_message_id,
                                        current_price_billion, deleted_from_channel_at) VALUES
                    (357, '30825', 308, 'ACTIVE', 1620, 25, NULL),
                    (358, '3513', 351, 'ACTIVE', 1700, 3, NULL),
                    (359, '35120', 3513, 'ACTIVE', 1701, 20, NULL),
                    (360, '31010', 310, 'INACTIVE', 1702, 10, NULL),
                    (361, '31111', 311, 'ACTIVE', 1703, 11, NOW()),
                    (362, '31212', 312, 'ACTIVE', 1704, 12, NULL);
                INSERT INTO property_media (property_id, telegram_file_id, media_type, position) VALUES
                    (357, 'video', 'VIDEO', 0), (357, 'second', 'PHOTO', 2), (357, 'first', 'PHOTO', 1);
                INSERT INTO property_trash VALUES (362);
            """)

    def test_finds_live_listings_by_code_or_short_code(self):
        offer = load_offer_module(self.connect)
        find = lambda code: (offer.find_listing_by_code(code) or {}).get("id")
        self.assertEqual(find("30825"), 357)
        self.assertEqual(find("308"), 357)  # the story's short code
        self.assertEqual(offer.find_listing_by_code("308")["photo_file_id"], "first")
        # A full code wins over a sequence number that reads the same.
        self.assertEqual(find("3513"), 358)
        for gone in ("31010", "31111", "31212", "999"):
            self.assertIsNone(find(gone), gone)
        self.assertEqual(offer.find_listing_by_id(357)["unit_code"], "30825")
        self.assertIsNone(offer.find_listing_by_id(360))


SMART = (APP / "shahfile_smart.py").read_text(encoding="utf-8")


class WiringTest(unittest.TestCase):
    def test_code_question_is_answered_before_budget_ai_and_advisor(self):
        handler = SMART[SMART.index("async def handle_customer_text"):SMART.index("async def handle_customer_contact")]
        check = handler.index("listing_code = parse_listing_code(text)")
        for later in ("bare_budget_billion(text)", "ai_intent = await extract_ai_intent(",
                      "escalate_customer_question"):
            self.assertLess(check, handler.index(later), later)
        # The offer's answer comes first, before a reply could be read as a cancel or a search.
        offer = handler.index("if state == LISTING_OFFER_STATE:")
        self.assertLess(offer, handler.index("_is_flow_cancel_request(text)"))
        self.assertIn("await start_visit_request(update, context, int(offered))", handler)

    def test_buttons_reach_the_visit_flow_and_search(self):
        callbacks = SMART[SMART.index("async def handle_customer_callback"):]
        self.assertIn("if action.startswith(VISIT_CALLBACK_PREFIX):", callbacks)
        self.assertIn("await start_visit_request(update, context, property_id)", callbacks)
        self.assertIn("if action.startswith(SIMILAR_CALLBACK_PREFIX):", callbacks)
        visit = (APP / "visit_flow.py").read_text(encoding="utf-8")
        start = visit[visit.index("async def handle_visit_start"):visit.index("async def start_visit_request")]
        self.assertIn("return await start_visit_request(", start)

    def test_visit_by_short_code_or_bare_number(self):
        tree = ast.parse(SMART)
        nodes = [n for n in tree.body if getattr(n, "name", None) in {"parse_visit_unit_intent", "normalize_digits"}
                 or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "_DIGIT_MAP")]
        ns = {"re": re, "Any": object}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "shahfile_smart.py", "exec"), ns)
        parse = ns["parse_visit_unit_intent"]
        self.assertEqual(parse("برای کد ۲۷۲۹ وقت بازدید می‌خوام"), "2729")
        self.assertEqual(parse("بازدید ۲۷۲۹"), "2729")
        self.assertIsNone(parse("بازدید 1405/07/20 18:30"))
        self.assertIsNone(parse("کد ۲۷۲۹"))
        lookup = SMART[SMART.index("def find_visitable_by_unit"):SMART.index("def _owner_user_id")]
        self.assertIn("OR internal_sequence::text=%s", lookup)


if __name__ == "__main__":
    unittest.main()
