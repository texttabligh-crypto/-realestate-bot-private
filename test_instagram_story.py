"""Settings › «📸 استوری اینستاگرام» makes stories for random live listings.

The owner picks how many; each story uses the listing's real photos and own
facts, OpenAI only writes the kicker/headline/caption, the price is left out
and the call to action asks viewers to comment the unit code.
"""
import asyncio
import io
import json
import random
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
APP = ROOT / "app"

try:
    import arabic_reshaper  # noqa: F401
    import bidi  # noqa: F401
    from PIL import Image
except ImportError:  # pragma: no cover - local runs without the image stack
    Image = None


class Button:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data = text, callback_data


class Markup:
    def __init__(self, rows):
        self.rows = rows


def load_story_module():
    sys.path.insert(0, str(APP))
    stubs = {
        "telegram": types.SimpleNamespace(InlineKeyboardButton=Button, InlineKeyboardMarkup=Markup),
        "publisher": types.SimpleNamespace(
            db_connect=None, get_destination_channel=lambda: (-1, "کانال", "nimatmelk")),
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("instagram_story", None)
        import instagram_story
    return instagram_story


def photo(color, size=(1280, 960)):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, "JPEG")
    return data.getvalue()


def near(pixel, color, tolerance=16):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, color))


LISTING = dict(
    id=295, unit_code="27310", district="ولیعصر جنوبی", area_m2=81, bedrooms=2,
    floor_text="5 از 4", build_year=1390, has_parking=False, has_elevator=True,
    has_storage=True,
)
PRICE_WORDS = re.compile("میلیارد|میلیون|تومان")


@unittest.skipIf(Image is None, "Pillow / arabic-reshaper / python-bidi not installed")
class StoryTest(unittest.TestCase):
    def setUp(self):
        self.story = load_story_module()

    def test_facts_and_comment_call_to_action(self):
        self.assertEqual(self.story.story_facts(LISTING), [
            "ولیعصر جنوبی", "۸۱ متر", "۲ خواب", "طبقه ۵ از ۴", "ساخت ۱۳۹۰", "آسانسور", "انباری",
        ])
        self.assertEqual(self.story.call_to_action(LISTING),
                         "برای دریافت قیمت و مشخصات، عدد ۲۷۳۱۰ رو کامنت کنید")

    def test_templates_skip_lines_needing_missing_facts(self):
        bare = dict(LISTING, district=None, area_m2=None, bedrooms=None)
        for seed in range(30):
            text = self.story.fallback_story_text(bare, random.Random(seed))
            self.assertNotIn("{", text["kicker"] + text["headline"])
            self.assertIn("عدد ۲۷۳۱۰ رو کامنت کنید", text["caption"])
            self.assertIsNone(PRICE_WORDS.search(text["caption"]))

    def test_renders_a_full_hd_story_without_price(self):
        for photos in ([photo((70, 110, 150))], [photo((70, 110, 150))] * 4):
            image = Image.open(io.BytesIO(self.story.render_story(
                photos, LISTING, "پیشنهاد ویژه ولیعصر جنوبی", "نور، فضا، ولیعصر جنوبی", "nimatmelk")))
            self.assertEqual(image.size, (1080, 1920))
            # The call to action sits above Instagram's reply bar.
            self.assertTrue(near(image.getpixel((90, 1585)), (226, 182, 82)))
            self.assertFalse(near(image.getpixel((90, 1700)), (226, 182, 82)))

    def test_unreadable_photos_are_reported(self):
        with self.assertRaises(ValueError):
            self.story.render_story([b"not an image"], LISTING, "x", "y", "")

    def openai(self, answer):
        body = json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(answer, ensure_ascii=False)}]}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(self.story.urllib.request, "urlopen", return_value=response) as call:
            text = self.story.openai_story_text(LISTING, random.Random(1))
        return text, json.loads(call.call_args[0][0].data.decode())

    def test_openai_copy_keeps_price_out_and_the_call_to_action_in(self):
        text, sent = self.openai({
            "kicker": "لوکس در ولیعصر ✨",
            "headline": "خونه‌ای که منتظرش بودی 🏡",
            "caption": "خانه‌ای دنج و نورگیر\nفقط ۱۰ میلیارد تومان\n#تبریز",
        })
        self.assertEqual(text["kicker"], "لوکس در ولیعصر")
        self.assertEqual(text["headline"], "خونه‌ای که منتظرش بودی")
        self.assertIsNone(PRICE_WORDS.search(text["caption"]))
        self.assertTrue(text["caption"].endswith("💬 برای دریافت قیمت و مشخصات، عدد ۲۷۳۱۰ رو کامنت کنید"))
        facts = json.loads(sent["input"])
        self.assertEqual(facts["facts"][1], "۸۱ متر")
        self.assertNotIn("price", sent["input"])

    def test_price_in_headline_falls_back_to_templates(self):
        text, _sent = self.openai({"kicker": "فقط ۱۰ میلیارد", "headline": "ارزان", "caption": ""})
        self.assertIsNone(PRICE_WORDS.search(text["kicker"] + text["headline"]))
        self.assertIn("عدد ۲۷۳۱۰ رو کامنت کنید", text["caption"])

    def test_story_count(self):
        self.assertEqual(self.story.parse_story_count("۵"), 5)
        self.assertEqual(self.story.parse_story_count("10 تا"), 10)
        self.assertIsNone(self.story.parse_story_count("11"))
        self.assertIsNone(self.story.parse_story_count("سلام"))

    def test_sends_the_requested_number_of_stories_one_by_one(self):
        data = photo((90, 120, 160))
        calls = []

        class File:
            async def download_as_bytearray(self):
                return bytearray(data)

        class Message:
            async def edit_text(self, text):
                calls.append("progress")

            async def delete(self):
                calls.append("status deleted")

        class Bot:
            async def send_message(self, chat_id, text, reply_markup=None):
                calls.append(("message", text))
                return Message()

            async def get_file(self, file_id):
                return File()

            async def send_photo(self, chat_id, photo, caption):
                calls.append(("photo", caption.splitlines()[-1]))

            async def send_document(self, chat_id, document, filename, caption):
                calls.append(("file", filename))

        listings = [dict(LISTING, id=i, unit_code=str(27310 + i), photo_file_ids=["a", "b"]) for i in range(4)]
        with mock.patch.object(self.story, "random_story_listings", return_value=listings), \
                mock.patch.object(self.story, "STORY_SEND_SPACING_SECONDS", 0), \
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            asyncio.run(self.story.make_stories(Bot(), 7, 2))
        self.assertEqual([c for c in calls if c[0] in ("photo", "file")], [
            ("photo", "#ولیعصر_جنوبی #املاک_تبریز #خرید_آپارتمان #تبریز #خانه_رویایی"),
            ("file", "story-27310.jpg"),
            ("photo", "#ولیعصر_جنوبی #املاک_تبریز #خرید_آپارتمان #تبریز #خانه_رویایی"),
            ("file", "story-27311.jpg"),
        ])
        self.assertEqual(calls[-1], ("message", "✅ ۲ استوری ساخته شد."))


class WiringTest(unittest.TestCase):
    def test_access_button_and_count_routing(self):
        source = (APP / "crm_panel.py").read_text(encoding="utf-8")
        self.assertIn('"instagram_story": "📸 ساخت استوری اینستاگرام"', source)
        off = re.search(r"RESTRICTED_DEFAULT_OFF_KEYS = \{([^}]*)\}", source).group(1)
        self.assertIn('"instagram_story"', off)
        self.assertIn('(("insta_story",), "instagram_story")', source)
        self.assertIn('if has_permission(session, "instagram_story"):', source)
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        self.assertLess(bot.index("# CRM_ACCESS_GUARD_V1"),
                        bot.index('if action == INSTAGRAM_STORY_ACTION or action.startswith(f"{INSTAGRAM_STORY_ACTION}_"):'))
        self.assertIn("allowed=check_action_access(session, INSTAGRAM_STORY_ACTION)", bot)


if __name__ == "__main__":
    unittest.main()
