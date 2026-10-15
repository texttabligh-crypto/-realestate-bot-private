"""Settings › «📸 استوری اینستاگرام» builds a story from a random live listing.

The story uses the listing's real photos and own facts, OpenAI only writes the
headline and caption, and the call to action asks for the unit code.
"""
import io
import json
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


def load_story_module():
    sys.path.insert(0, str(APP))
    stubs = {
        "telegram": types.SimpleNamespace(InlineKeyboardButton=object, InlineKeyboardMarkup=object),
        "publisher": types.SimpleNamespace(db_connect=None, get_destination_channel=lambda: None),
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
    has_storage=True, price_billion="10.500", price_toman=None,
)


@unittest.skipIf(Image is None, "Pillow / arabic-reshaper / python-bidi not installed")
class StoryTest(unittest.TestCase):
    def setUp(self):
        self.story = load_story_module()

    def test_facts_price_and_call_to_action(self):
        facts = self.story.story_facts(LISTING)
        self.assertEqual(facts["main"], ["ولیعصر جنوبی", "۸۱ متر", "۲ خواب"])
        self.assertEqual(facts["details"], ["طبقه ۵ از ۴", "ساخت ۱۳۹۰", "آسانسور", "انباری"])
        self.assertEqual(facts["price"], "۱۰٫۵ میلیارد تومان")
        self.assertEqual(self.story.price_text(dict(LISTING, price_billion="40.000")), "۴۰ میلیارد تومان")
        self.assertEqual(self.story.call_to_action(LISTING), "برای دریافت مشخصات، عدد ۲۷۳۱۰ را ارسال کنید")

    def test_renders_a_full_hd_story(self):
        for photos in ([photo((70, 110, 150))], [photo((70, 110, 150))] * 4):
            image = Image.open(io.BytesIO(self.story.render_story(
                photos, LISTING, "واحد خوش‌نقشه و نورگیر در قلب ولیعصر", "nimatmelk")))
            self.assertEqual(image.size, (1080, 1920))
            # The call to action sits above Instagram's reply bar.
            self.assertTrue(near(image.getpixel((90, 1592)), (226, 182, 82)))
            self.assertFalse(near(image.getpixel((90, 1700)), (226, 182, 82)))

    def test_unreadable_photos_are_reported(self):
        with self.assertRaises(ValueError):
            self.story.render_story([b"not an image"], LISTING, "x", "")

    def openai(self, answer):
        body = json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(answer, ensure_ascii=False)}]}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        facts = self.story.story_facts(LISTING)
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(self.story.urllib.request, "urlopen", return_value=response) as call:
            text = self.story.openai_story_text(LISTING, facts)
        return text, call

    def test_openai_writes_headline_and_caption_from_facts_only(self):
        text, call = self.openai({"headline": "واحد نورگیر ولیعصر 🏡", "caption": "خانه‌ای دنج\n#تبریز"})
        self.assertEqual(text["headline"], "واحد نورگیر ولیعصر")
        self.assertTrue(text["caption"].endswith("📩 برای دریافت مشخصات، عدد ۲۷۳۱۰ را ارسال کنید"))
        sent = json.loads(call.call_args[0][0].data.decode())
        self.assertNotIn("raw_text", sent["input"])
        self.assertIn("۸۱ متر", sent["input"])

    def test_template_when_openai_is_unavailable(self):
        facts = self.story.story_facts(LISTING)
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            text = self.story.openai_story_text(LISTING, facts)
        self.assertEqual(text["headline"], "آپارتمان ۸۱ متر در ولیعصر جنوبی")
        self.assertIn("عدد ۲۷۳۱۰ را ارسال کنید", text["caption"])


class PermissionWiringTest(unittest.TestCase):
    def test_access_is_listed_off_by_default_and_guards_the_button(self):
        source = (APP / "crm_panel.py").read_text(encoding="utf-8")
        self.assertIn('"instagram_story": "📸 ساخت استوری اینستاگرام"', source)
        off = re.search(r"RESTRICTED_DEFAULT_OFF_KEYS = \{([^}]*)\}", source).group(1)
        self.assertIn('"instagram_story"', off)
        self.assertIn('(("insta_story",), "instagram_story")', source)
        self.assertIn('if has_permission(session, "instagram_story"):', source)
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        self.assertLess(bot.index("# CRM_ACCESS_GUARD_V1"), bot.index("if action == INSTAGRAM_STORY_ACTION:"))


if __name__ == "__main__":
    unittest.main()
