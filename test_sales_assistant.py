"""Customer messages no rule answers get a friendly, fact-bound sales reply.

They used to go straight to an advisor as an unanswered question. The
assistant now answers from the conversation and the facts of the listings in
it, steers toward a visit, and hands the question to an advisor when the
facts don't answer it. A reply with an invented price, a phone number, a
handle or a foreign link is never sent.
"""
import ast
import json
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
SMART = (APP / "shahfile_smart.py").read_text(encoding="utf-8")


def ai_helpers():
    """_extract_response_text and _parse_json_object from shahfile_ai, as they are."""

    tree = ast.parse((APP / "shahfile_ai.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in {"_extract_response_text", "_parse_json_object"}]
    ns = {"re": re, "json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "shahfile_ai.py", "exec"), ns)
    return ns


def load_assistant():
    sys.path.insert(0, str(APP))
    helpers = ai_helpers()
    stubs = {
        "telegram": types.SimpleNamespace(InlineKeyboardButton=object, InlineKeyboardMarkup=object),
        "publisher": types.SimpleNamespace(
            db_connect=None,
            get_destination_channel=lambda: (-100, "NIMAT", "nimatmelk"),
            build_channel_post_url=lambda username, message_id: (
                f"https://t.me/{username}/{message_id}" if username and message_id else None),
            clean_publish_location=lambda value: value,
        ),
        "shahfile_ai": types.SimpleNamespace(
            _extract_response_text=helpers["_extract_response_text"],
            _parse_json_object=helpers["_parse_json_object"],
            get_active_search_context=lambda user_id: None,
            normalize_search_filters=lambda raw: {k: v for k, v in dict(raw or {}).items() if v is not None},
            search_rows=lambda filters, limit=3: [],
        ),
    }
    with mock.patch.dict(sys.modules, stubs):
        for name in ("sales_assistant", "listing_offer"):
            sys.modules.pop(name, None)
        import sales_assistant
    return sales_assistant


LISTING = dict(
    id=357, unit_code="30825", district="ولیعصر", address_text="ولیعصر پروین اعتصامی",
    area_m2=Decimal("125.00"), bedrooms=2, floor_text="2", build_year=None, direction="شمالی",
    has_parking=True, has_elevator=True, has_storage=None, current_price_billion=Decimal("25.000"),
    published_message_id=1620, channel_post_url=None, photo_file_id="p",
)
HISTORY = [
    {"from": "customer", "text": "کد ۳۰۸۲۵"},
    {"from": "bot", "text": "اینم مشخصات فایل کد ۳۰۸۲۵ 😊\n💰 قیمت: ۲۵ میلیارد تومان"},
    {"from": "customer", "text": "بودجه‌م حدود ۲۴ میلیارده"},
]


class SalesAssistantTest(unittest.TestCase):
    def setUp(self):
        self.assistant = load_assistant()

    def context(self, message="نورگیرش خوبه؟"):
        return {
            "customer_first_name": "آرمین",
            "history": HISTORY,
            "listings": [self.assistant.listing_facts(LISTING, "nimatmelk")],
            "active_search": {"price_min_billion": 20, "price_max_billion": 26.5},
            "customer_message": message,
        }

    def test_codes_in_the_conversation_newest_first(self):
        codes = self.assistant.codes_in(
            HISTORY + [{"from": "bot", "text": "🔢 کد واحد: 31822"}], "کد ۳۲۰ چی؟")
        self.assertEqual(codes, ["320", "31822", "30825"])

    def test_facts_given_to_the_model(self):
        self.assertEqual(self.assistant.listing_facts(LISTING, "nimatmelk"), {
            "code": "30825", "location": "ولیعصر پروین اعتصامی", "area_m2": 125, "bedrooms": 2,
            "floor": "2", "build_year": None, "direction": "شمالی", "amenities": ["آسانسور", "پارکینگ"],
            "price_billion": "25", "channel_link": "https://t.me/nimatmelk/1620",
        })

    def test_safe_replies(self):
        safe = self.assistant.reply_is_safe
        context = self.context()
        for reply in (
            "قیمت این واحد ۲۵ میلیارد تومانه 😊 می‌خواید براتون بازدید هماهنگ کنم؟",
            "با بودجه ۲۴ میلیارد هم گزینه داریم؛ اینم لینکش https://t.me/nimatmelk/1620",
            "بین ۲۰ تا ۲۶.۵ میلیارد براتون می‌گردم.",
            "واحد ۱۲۵ متریه و ۲ خواب داره. کی وقت دارید ببینید؟",
        ):
            self.assertTrue(safe(reply, context), reply)
        for reply in (
            "قیمتش ۲۳ میلیارده ولی تخفیف هم میدن",
            "با ۸۰۰ میلیون پیش‌پرداخت قابل خریده",
            "با همکارم ۰۹۱۴۱۲۳۴۵۶۷ تماس بگیرید",
            "به آیدی @agency پیام بدید",
            "این کانال هم ببینید t.me/otherchannel",
        ):
            self.assertFalse(safe(reply, context), reply)

    def ask(self, answer, listings=(LISTING,)):
        context = self.context()
        with mock.patch.object(self.assistant, "build_context", return_value=(context, list(listings))), \
                mock.patch.object(self.assistant, "_ask_openai", return_value=answer):
            return self.assistant.assistant_turn(1, "آرمین", context["customer_message"])

    def test_visit_action_resolves_the_listing(self):
        turn = self.ask({"reply": "عالیه! الان ثبتش می‌کنم 🌷", "action": "visit", "unit_code": "۳۰۸۲۵"})
        self.assertEqual((turn["action"], turn["listing"]["id"]), ("visit", 357))
        # The only listing in the conversation, when the code is missing.
        self.assertEqual(self.ask({"reply": "حتماً", "action": "visit"})["listing"]["id"], 357)
        self.assertEqual(self.ask({"reply": "حتماً", "action": "visit"}, listings=())["action"], "none")

    def test_search_and_other_actions(self):
        turn = self.ask({"reply": "می‌گردم براتون", "action": "search",
                         "filters": {"price_max_billion": 24, "district": None}})
        self.assertEqual((turn["action"], turn["filters"]), ("search", {"price_max_billion": 24}))
        self.assertEqual(self.ask({"reply": "می‌گردم", "action": "search", "filters": {}})["action"], "none")
        self.assertEqual(self.ask({"reply": "از همکارم می‌پرسم 🌷", "action": "handoff"})["action"], "handoff")
        self.assertEqual(self.ask({"reply": "سلام", "action": "dance"})["action"], "none")

    def test_unusable_answers_go_to_the_advisors(self):
        self.assertIsNone(self.ask(None))
        self.assertIsNone(self.ask({"reply": "", "action": "none"}))
        self.assertIsNone(self.ask({"reply": "قیمتش ۱۹ میلیارده", "action": "none"}))
        self.assertIsNone(self.ask({"reply": "x" * 901, "action": "none"}))

    def test_openai_request_is_fact_bound(self):
        body = json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"reply": "سلام آرمین", "action": "none"}'}]}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test", "OPENAI_MODEL": "m"}), \
                mock.patch.object(self.assistant.urllib.request, "urlopen", return_value=response) as call:
            answer = self.assistant._ask_openai(self.context())
        self.assertEqual(answer, {"reply": "سلام آرمین", "action": "none"})
        sent = json.loads(call.call_args[0][0].data.decode())
        self.assertEqual(sent["model"], "m")
        self.assertIn("Use ONLY the facts", sent["instructions"])
        self.assertIn("Never", sent["instructions"])
        self.assertEqual(json.loads(sent["input"])["listings"][0]["price_billion"], "25")
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            self.assertIsNone(self.assistant._ask_openai(self.context()))

    def test_context_uses_conversation_listings_else_the_latest_search(self):
        assistant = self.assistant
        with mock.patch.object(assistant, "recent_history", return_value=HISTORY), \
                mock.patch.object(assistant, "find_listing_by_code", side_effect=lambda code: LISTING if code == "30825" else None):
            context, listings = assistant.build_context(1, "آرمین", "نورگیرش خوبه؟")
        self.assertEqual([item["code"] for item in context["listings"]], ["30825"])
        self.assertEqual(listings, [LISTING])
        row = (9, "40010", "رشدیه", None, Decimal("90"), 2, 1400, Decimal("10"), None, 7)
        with mock.patch.object(assistant, "recent_history", return_value=[]), \
                mock.patch.object(assistant, "get_active_search_context",
                                  return_value={"filters": {"price_max_billion": 11}}), \
                mock.patch.object(assistant, "search_rows", return_value=[row]):
            context, listings = assistant.build_context(1, None, "چی دارید؟")
        self.assertEqual(context["listings"][0]["code"], "40010")
        self.assertEqual(context["active_search"], {"price_max_billion": 11})


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class HistoryDatabaseTest(unittest.TestCase):
    def test_latest_messages_oldest_first_without_phone_numbers(self):
        connect = lambda: psycopg.connect(DSN)
        with connect() as conn:
            conn.execute("""
                DROP TABLE IF EXISTS customer_chat_history;
                CREATE TABLE customer_chat_history (
                    id BIGSERIAL PRIMARY KEY, telegram_user_id BIGINT, message_id BIGINT,
                    direction TEXT, body TEXT, recorded_at TIMESTAMPTZ DEFAULT NOW());
            """)
            conn.execute(
                "INSERT INTO customer_chat_history (telegram_user_id, message_id, direction, body) VALUES "
                "(7, 1, 'customer', 'سلام'), (8, 2, 'customer', 'other customer'), "
                "(7, 3, 'customer', '📞 اشتراک شماره: ۰۹۱۴۱۲۳۴۵۶۷'), (7, 4, 'bot', 'ممنون'), "
                "(7, 5, 'customer', 'شمارم 0914-123-4567 هست')")
        assistant = load_assistant()
        with mock.patch.object(assistant, "db_connect", connect):
            history = assistant.recent_history(7, limit=3)
        self.assertEqual(history, [
            {"from": "customer", "text": "📞 اشتراک شماره: [شماره]"},
            {"from": "bot", "text": "ممنون"},
            {"from": "customer", "text": "شمارم [شماره] هست"},
        ])
        with mock.patch.object(assistant, "recent_history", return_value=[]):
            context, _listings = assistant.build_context(7, None, "شماره من 09141234567")
        self.assertEqual(context["customer_message"], "شماره من [شماره]")


class WiringTest(unittest.TestCase):
    def test_assistant_answers_before_an_advisor_is_asked(self):
        handler = SMART[SMART.index("async def handle_customer_text"):SMART.index("async def handle_customer_contact")]
        turn = handler.index("assistant_turn, user.id, user.first_name, text")
        self.assertLess(turn, handler.index("if await escalate_customer_question("))
        # Rules still come first.
        for earlier in ("listing_code = parse_listing_code(text)", "bare_budget_billion(text)",
                        "ai_intent = await extract_ai_intent(", "unit_code = parse_visit_unit_intent(text)"):
            self.assertLess(handler.index(earlier), turn, earlier)
        block = handler[turn:handler.index("if await escalate_customer_question(")]
        self.assertIn("await start_visit_request(update, context, turn[\"listing\"][\"id\"])", block)
        self.assertIn("await _offer_search_confirmation(update, context, turn[\"filters\"], text, customer_id)", block)
        self.assertIn("await escalate_customer_question(context.bot, user, customer_id, text)", block)


if __name__ == "__main__":
    unittest.main()
