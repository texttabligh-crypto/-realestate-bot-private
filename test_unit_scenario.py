"""Settings › «🎬 سناریو ریلز»: a 30-second Reels script for a file code.

The owner records himself in a friendly voice, so the script gives the hook,
timed scenes (what to film, what to say, the text on screen), the call to
action with the short code, a cover title, the caption and recording tips.
Only the listing's own facts are used and never its price. Others get the
button only when the owner grants «🎬 نوشتن سناریو ریلز».
"""
import asyncio
import io
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from test_instagram_story import DSN, Image, LISTING, load_story_module, psycopg

APP = Path(__file__).parent / "app"
PRICE_WORDS = re.compile("میلیارد|میلیون|تومان")


class Button:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data = text, callback_data


def load_scenario_module():
    story = load_story_module()
    stubs = {
        "telegram": types.SimpleNamespace(InlineKeyboardButton=Button, InlineKeyboardMarkup=lambda rows: rows),
        "instagram_story": story,
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("unit_scenario", None)
        import unit_scenario
    return unit_scenario


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def openai_reply(text):
    body = {"output": [{"content": [{"type": "output_text", "text": text}]}]}
    return Response(json.dumps(body, ensure_ascii=False).encode("utf-8"))


@unittest.skipIf(Image is None, "Pillow / arabic-reshaper / python-bidi not installed")
class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario_module()

    def test_plain_script_without_openai(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            script = self.scenario.write_scenario(LISTING)
        for heading in ("🎣 هوک (۰ تا ۳ ثانیه)", "🎬 صحنه‌ها", "📣 پایان (۲۷ تا ۳۰ ثانیه)",
                        "📝 تیتر کاور:", "✍️ کپشن:", "💡 نکته‌های ضبط:"):
            self.assertIn(heading, script)
        self.assertIn("عدد ۲۷۳ رو کامنت کنید یا دایرکت بدید", script)
        self.assertIn("ولیعصر جنوبی", script)
        self.assertIsNone(PRICE_WORDS.search(script))

    def test_openai_script_keeps_price_out_and_the_code_in(self):
        requests = []

        def urlopen(request, timeout):
            requests.append((json.loads(request.data.decode("utf-8")), timeout))
            return openai_reply("🎣 هوک\n🗣 این خونه رو از دست ندید!\n🔤 قیمت فقط ۹ میلیارد\n🎬 صحنه‌ها")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(self.scenario.urllib.request, "urlopen", urlopen):
            script = self.scenario.write_scenario(LISTING, rng=__import__("random").Random(1))
        self.assertIn("این خونه رو از دست ندید!", script)
        self.assertNotIn("میلیارد", script)
        self.assertTrue(script.endswith("📣 برای قیمت و مشخصات، عدد ۲۷۳ رو کامنت کنید یا دایرکت بدید"))
        payload, timeout = requests[0]
        self.assertGreaterEqual(timeout, 60)
        self.assertIn("Never mention the price", payload["instructions"])
        self.assertIn("صمیمی و خودمونی", payload["instructions"])
        self.assertIn("30-second", payload["instructions"])
        facts = json.loads(payload["input"])
        self.assertEqual(facts["headline"], "۸۱ متر، ۲ خواب")
        self.assertIsNone(PRICE_WORDS.search(payload["input"]))

    def test_openai_failure_falls_back(self):
        def broken(request, timeout):
            raise TimeoutError("Timed out")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(self.scenario.urllib.request, "urlopen", broken):
            self.assertEqual(self.scenario.write_scenario(LISTING), self.scenario.fallback_scenario(LISTING))

    def test_message_and_keyboard(self):
        text = self.scenario.scenario_message(LISTING, "x" * 5000)
        self.assertTrue(text.startswith("🎬 سناریو ریلز ۳۰ ثانیه‌ای | کد ۲۷۳۱۰\n🏡 ۸۱ متر، ۲ خواب | منطقه: ولیعصر جنوبی"))
        self.assertLessEqual(len(text), 4000)
        rows = self.scenario.scenario_keyboard(LISTING)
        self.assertEqual([row[0].callback_data for row in rows],
                         ["unit_scenario_again_295", "unit_scenario", "settings"])

    def type_code(self, text, found):
        replies, started = [], []
        context = types.SimpleNamespace(user_data={"state": "unit_scenario_code"})

        class Message:
            async def reply_text(self, text, reply_markup=None):
                replies.append(text)
                return "status"

        update = types.SimpleNamespace(message=Message(), effective_chat=types.SimpleNamespace(id=7))
        update.message.text = text
        with mock.patch.object(self.scenario, "listing_by_code", lambda code: found), \
                mock.patch.object(self.scenario, "_start", lambda ctx, chat, listing, status: started.append(
                    (chat, listing["id"], status))):
            asyncio.run(self.scenario.handle_unit_scenario_text(update, context, allowed=True))
        return replies, started, context.user_data

    def test_a_code_starts_the_script(self):
        replies, started, state = self.type_code("کد ۲۷۳۱۰", LISTING)
        self.assertEqual(replies, ["⏳ دارم سناریوی ریلز کد ۲۷۳۱۰ رو می‌نویسم…"])
        self.assertEqual(started, [(7, 295, "status")])
        self.assertEqual(state, {})

    def test_unknown_code_or_no_digits_asks_again(self):
        replies, started, state = self.type_code("۹۹۹", None)
        self.assertEqual(replies, ["فایلی با کد ۹۹۹ پیدا نکردم 🤔 کد رو دوباره بفرستید."])
        self.assertEqual((started, state), ([], {"state": "unit_scenario_code"}))
        replies, started, state = self.type_code("سلام", LISTING)
        self.assertEqual(replies, ["کد فایل رو به عدد بفرستید، مثلاً ۳۵۱۳ 🙏"])
        self.assertEqual(started, [])

    def test_button_asks_for_the_code(self):
        edits = []
        context = types.SimpleNamespace(user_data={})

        class Query:
            data = "unit_scenario"

            async def edit_message_text(self, text, reply_markup=None):
                edits.append(text)

        asyncio.run(self.scenario.handle_unit_scenario_button(Query(), context))
        self.assertEqual(context.user_data, {"state": "unit_scenario_code"})
        self.assertIn("کد فایل رو بفرستید", edits[0])


@unittest.skipUnless(Image is not None and psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class ListingByCodeTest(unittest.TestCase):
    def test_full_and_short_codes(self):
        story = load_story_module()
        connect = lambda: psycopg.connect(DSN)  # noqa: E731
        with connect() as conn:
            conn.execute("""
                DROP TABLE IF EXISTS property_attributes, property_media, property_trash, properties CASCADE;
                CREATE TABLE properties (
                    id BIGINT PRIMARY KEY, unit_code TEXT, district TEXT, area_m2 NUMERIC,
                    bedrooms INT, floor_text TEXT, build_year INT, has_parking BOOL,
                    has_elevator BOOL, has_storage BOOL, internal_sequence INT, direction TEXT,
                    address_text TEXT, raw_text TEXT, status TEXT, published_message_id BIGINT,
                    channel_post_url TEXT);
                CREATE TABLE property_media (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, telegram_file_id TEXT,
                    media_type TEXT DEFAULT 'PHOTO', position INT DEFAULT 0);
                CREATE TABLE property_trash (property_id BIGINT PRIMARY KEY);
                CREATE TABLE property_attributes (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, attribute_key TEXT,
                    attribute_label TEXT, attribute_value TEXT, display_order INT,
                    is_visible BOOL DEFAULT TRUE);
                INSERT INTO properties (id, unit_code, internal_sequence, status) VALUES
                    (1, '35129', 351, 'SOLD'), (2, '35130', 351, 'ACTIVE'),
                    (3, '320', 32, 'ACTIVE'), (4, '32011', 320, 'ACTIVE'), (5, '40010', 400, 'ACTIVE');
                INSERT INTO property_trash VALUES (5);
                INSERT INTO property_attributes (property_id, attribute_key, attribute_value, display_order)
                VALUES (2, 'deed_type', 'تک برگ', 1);
            """)
        with mock.patch.object(story, "db_connect", connect):
            self.assertEqual(story.listing_by_code("۳۵۱")["id"], 2)  # the live one
            self.assertEqual(story.listing_by_code("35129")["id"], 1)
            self.assertEqual(story.listing_by_code("320")["id"], 3)  # a full code wins
            self.assertIsNone(story.listing_by_code("400"))  # in the trash
            self.assertIsNone(story.listing_by_code("abc"))
            self.assertEqual(story.listing_by_id(2)["attributes"], [("deed_type", "تک برگ")])


class WiringTest(unittest.TestCase):
    def test_wiring(self):
        crm = (APP / "crm_panel.py").read_text(encoding="utf-8")
        self.assertIn('"unit_scenario": "🎬 نوشتن سناریو ریلز"', crm)
        off = re.search(r"RESTRICTED_DEFAULT_OFF_KEYS = \{([^}]*)\}", crm).group(1)
        self.assertIn('"unit_scenario"', off)
        self.assertIn('(("unit_scenario",), "unit_scenario")', crm)
        self.assertIn('if has_permission(session, "unit_scenario"):', crm)
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        handler = bot[bot.index("async def button_handler("):]
        self.assertLess(handler.index("# CRM_ACCESS_GUARD_V1"),
                        handler.index("await handle_unit_scenario_button(query, context)"))
        self.assertLess(handler.index('context.user_data.get("state") == UNIT_SCENARIO_STATE'),
                        handler.index("# CRM_ACCESS_GUARD_V1"))
        self.assertIn("allowed=check_action_access(session, UNIT_SCENARIO_ACTION)", bot)


if __name__ == "__main__":
    unittest.main()
