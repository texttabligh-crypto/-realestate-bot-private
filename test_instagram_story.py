"""Settings › «📸 استوری اینستاگرام» makes stories for random live listings.

The owner picks how many and a price range; each story uses the listing's
real photos and own facts in one of four layouts: a red title band with a
factual title (no slogan), tiles for the other facts, checked amenities, and
the photos filling whatever room is left. OpenAI only writes the caption, the
price and the channel handle are left out, the call to action asks viewers to
send the short code as a story reply, and every story is sent as a 1080×1920 file.
"""
import asyncio
import io
import json
import os
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
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None

DSN = os.getenv("PGTEST_DSN")

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
            db_connect=None,
            get_destination_channel=lambda: (-100, "NIMAT", "nimatmelk"),
            build_channel_post_url=lambda username, message_id: (
                f"https://t.me/{username.lstrip('@')}/{message_id}" if username and message_id else None),
        ),
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
    has_storage=True, internal_sequence=273,
)
PRICE_WORDS = re.compile("میلیارد|میلیون|تومان")


@unittest.skipIf(Image is None, "Pillow / arabic-reshaper / python-bidi not installed")
class StoryTest(unittest.TestCase):
    def setUp(self):
        self.story = load_story_module()

    def test_story_asks_viewers_to_send_the_code_not_comment(self):
        source = (APP / "instagram_story.py").read_text(encoding="utf-8")
        self.assertNotIn("کامنت", source)
        self.assertIn('before, after = "عدد", "رو ارسال کنید"', source)
        self.assertIn('"ارسال کن"', source)

    def test_facts_and_send_the_code_call_to_action(self):
        self.assertEqual(self.story.story_facts(LISTING), [
            "ولیعصر جنوبی", "۸۱ متر", "۲ خواب", "طبقه ۵ از ۴", "ساخت ۱۳۹۰", "آسانسور", "انباری",
        ])
        self.assertEqual(self.story.call_to_action(LISTING),
                         "برای دریافت قیمت و مشخصات، عدد ۲۷۳ رو ارسال کنید")

    def test_short_code_is_the_sequence(self):
        self.assertEqual(self.story.short_code(LISTING), "273")
        self.assertEqual(self.story.short_code(dict(LISTING, internal_sequence=None, unit_code="22537")), "225")
        self.assertEqual(self.story.short_code(dict(LISTING, internal_sequence=1004)), "1004")

    def test_title_is_the_listing_facts_not_a_slogan(self):
        self.assertEqual(self.story.story_titles(LISTING), ("منطقه: ولیعصر جنوبی", "۸۱ متر، ۲ خواب"))
        self.assertEqual(self.story.story_titles(dict(LISTING, district=None)), ("تبریز", "۸۱ متر، ۲ خواب"))
        self.assertEqual(self.story.story_titles(dict(LISTING, area_m2=None, bedrooms=None)),
                         ("منطقه: ولیعصر جنوبی", "فایل ویژه"))
        bare = dict(LISTING, district=None, area_m2=None, bedrooms=None)
        self.assertEqual(self.story.story_titles(bare), ("تبریز", "فایل ویژه"))
        source = (APP / "instagram_story.py").read_text(encoding="utf-8")
        self.assertNotIn("KICKERS", source)
        self.assertNotIn("HEADLINES", source)
        self.assertNotIn("رو ببین", source)

    def test_tiles_and_amenities_add_the_listing_details(self):
        self.assertEqual(self.story.story_stats(LISTING), [
            ("۵ از ۴", "طبقه", "طبقه ۵ از ۴"), ("۱۳۹۰", "سال ساخت", "ساخت ۱۳۹۰"),
        ])
        self.assertEqual(self.story.story_amenities(LISTING), ["آسانسور", "انباری"])
        rich = dict(LISTING, direction="جنوبی", attributes=[
            ("units_per_floor", "2"), ("deed_type", "تک برگ"),
            ("master_bedroom", "✨ خواب مستر با سرویس"), ("lobby", "لابی مجلل"),
            ("property_condition", "کلید نخورده"), ("open_view", "باز"),
            ("installations", "کامل"), ("source_photo_authenticity", "واقعی"),
        ])
        self.assertEqual([value for value, _label, _text in self.story.story_stats(rich)],
                         ["۵ از ۴", "۱۳۹۰", "۲", "جنوبی"])  # four tiles at most
        self.assertEqual(self.story.story_amenities(rich), [
            "آسانسور", "انباری", "کلید نخورده", "ویو باز", "فول نصبیات", "خواب مستر", "لابی",
        ])
        self.assertIn("سند تک برگ", self.story.story_facts(dict(rich, floor_text=None)))

    def test_details_that_could_carry_contact_text_are_dropped(self):
        risky = dict(LISTING, floor_text=None, build_year=None, direction="تماس 09141234567",
                     attributes=[("deed_type", "@agency"), ("property_condition", "x" * 40),
                                 ("units_per_floor", "دو")])
        self.assertEqual(self.story.story_stats(risky), [])
        self.assertEqual(self.story.story_amenities(risky), ["آسانسور", "انباری"])

    def test_fallback_caption_is_factual(self):
        self.assertEqual(self.story.fallback_caption(LISTING), "\n".join([
            "🏡 ۸۱ متر، ۲ خواب | منطقه: ولیعصر جنوبی",
            "🏢 طبقه ۵ از ۴ • ساخت ۱۳۹۰",
            "✅ آسانسور • انباری",
            "",
            "💬 برای دریافت قیمت و مشخصات، عدد ۲۷۳ رو ارسال کنید",
            "",
            "#ولیعصر_جنوبی #املاک_تبریز #خرید_آپارتمان #تبریز #خانه_رویایی",
        ]))
        bare = dict(LISTING, district=None, area_m2=None, bedrooms=None, floor_text=None,
                    build_year=None, has_elevator=False, has_storage=False)
        caption = self.story.fallback_caption(bare)
        self.assertEqual(caption.split("\n\n")[0], "🏡 فایل ویژه | تبریز")
        self.assertIn("عدد ۲۷۳ رو ارسال کنید", caption)
        self.assertIsNone(PRICE_WORDS.search(caption))

    def test_four_layouts_render_full_hd_stories(self):
        story = self.story
        self.assertEqual([name for name, _render, _fill in story.STORY_LAYOUTS],
                         ["classic", "magazine", "mosaic", "cinematic"])
        cta_middle = story.CTA_TOP + story.CTA_HEIGHT // 2
        # The call to action ends at least 250px above the bottom, clear of
        # Instagram's reply field.
        self.assertLessEqual(story.CTA_TOP + story.CTA_HEIGHT, 1920 - 250)
        for listing in (LISTING, dict(LISTING, floor_text=None, build_year=None, has_elevator=False)):
            info = story._info(listing, *story.story_titles(listing), 1080)
            for photos in ([photo((70, 110, 150))], [photo((70, 110, 150))] * 4):
                for index, (name, _render, cta_fill) in enumerate(story.STORY_LAYOUTS):
                    image = Image.open(io.BytesIO(story.render_story(
                        photos, listing, *story.story_titles(listing), index)))
                    self.assertEqual(image.size, (1080, 1920), name)
                    self.assertTrue(near(image.getpixel((story.MARGIN + 14, cta_middle)), cta_fill), name)
                    self.assertFalse(near(image.getpixel((story.MARGIN + 14, 1700)), cta_fill), name)
                    if name in ("classic", "mosaic"):
                        # A straight red band right under the photos.
                        self.assertTrue(near(image.getpixel((8, info["top"] + 8)), story.RED_DEEP), name)
                        self.assertTrue(near(image.getpixel((1072, info["top"] + 8)), story.RED), name)
                        self.assertTrue(near(image.getpixel((300, info["top"] - 8)), (70, 110, 150)), name)

    def test_photos_fill_the_room_the_details_leave(self):
        story = self.story
        few = dict(LISTING, floor_text=None, build_year=None, has_elevator=False, has_storage=False)
        rich = dict(LISTING, direction="جنوبی", attributes=[("units_per_floor", "2"), ("lobby", "لابی")])
        tops = [story._info(item, *story.story_titles(item), 1080)["top"] for item in (few, LISTING, rich)]
        self.assertEqual(tops, sorted(tops, reverse=True))
        self.assertGreater(tops[0] - tops[2], 200)

    def test_every_photo_fills_its_box(self):
        canvas = Image.new("RGB", (1000, 300))
        self.story._paste_photo(canvas, Image.new("RGB", (300, 600), (200, 30, 30)), (0, 0, 1000, 300))
        for x in (2, 500, 997):
            self.assertTrue(near(canvas.getpixel((x, 150)), (200, 30, 30)))

    def test_grid_puts_photos_in_boxes_of_their_shape(self):
        choose = self.story._choose_grid
        upright = [Image.new("RGB", (600, 800), (i, 0, 0)) for i in range(4)]
        wide = [Image.new("RGB", (1280, 960), (i, 0, 0)) for i in range(4)]
        classic = ("top_strip", "top_pair", "columns", "rows", "single")
        mosaic = ("side_stack", "top_pair", "columns", "rows", "top_strip", "single")
        area = (0, 0, 1080, 900)
        self.assertEqual(choose(upright, area, classic)[0], "columns")
        self.assertEqual(choose(upright, area, mosaic)[0], "side_stack")
        self.assertEqual(choose(wide, area, classic)[0], "top_strip")
        name, boxes, chosen = choose(wide, area, classic)
        self.assertEqual(len(boxes), 4)
        self.assertIs(chosen[0], wide[0])  # the first photo keeps the big box
        # In a mixed set the upright photo takes the tall column.
        mixed = [wide[0], upright[1], wide[2]]
        name, boxes, chosen = choose(mixed, area, mosaic)
        self.assertEqual(name, "side_stack")
        self.assertIs(chosen[0], upright[1])
        self.assertEqual(choose(upright[:1], area, classic)[0], "single")
        # The boxes tile the area without overlapping it.
        for grid in self.story._photo_grids(area, 4).values():
            for left, top, right, bottom in grid:
                self.assertTrue(0 <= left < right <= 1080 and 0 <= top < bottom <= 900)

    def test_magazine_frame_takes_the_photo_shape(self):
        fitted = self.story._fitted_box
        area = (60, 390, 1020, 900)
        left, top, right, bottom = fitted(Image.new("RGB", (1280, 640)), area)
        self.assertEqual((left, right), (60, 1020))
        self.assertAlmostEqual((right - left) / (bottom - top), 2, delta=0.02)
        left, top, right, bottom = fitted(Image.new("RGB", (600, 800)), area)
        self.assertEqual((top, bottom), (390, 900))
        self.assertGreaterEqual(right - left, 0.62 * 960 - 1)
        self.assertLess(right - left, 960)

    def test_band_names_the_region(self):
        tag = lambda **fields: self.story.story_titles(dict(LISTING, **fields))[0]
        self.assertEqual(tag(district="▫️سازنده مطمئن", address_text="ولیعصر پروین اعتصامی"),
                         "منطقه: ولیعصر")
        self.assertEqual(tag(district="منظریه", address_text="نصبیات فوق لوکس"), "منطقه: منظریه")
        self.assertEqual(tag(district=None, address_text="واحد: زعفرانیه"), "منطقه: زعفرانیه")
        self.assertEqual(tag(district=None, address_text="ائل‌گلی، خیابان ۲۲ بهمن"), "منطقه: ائل گلی")
        # The source post's hashtags, when the stored fields name no region.
        self.assertEqual(tag(district=None, raw_text="#آپارتمان_20_25\n#فرشته_جنوبی\nنزدیک ولیعصر"),
                         "منطقه: فرشته جنوبی")
        self.assertEqual(tag(district="کوی استانداری"), "منطقه: کوی استانداری")
        self.assertEqual(tag(district="منطقه: یاغچیان"), "منطقه: یاغچیان")
        self.assertEqual(tag(district="▫️سازنده مطمئن"), "تبریز")
        self.assertEqual(tag(district="خیابان امام، کوچه شهید فلانی، پلاک ۱۲"), "تبریز")
        self.assertEqual(self.story.story_facts(dict(LISTING, district="#ولیعصر"))[0], "ولیعصر")
        caption = self.story.fallback_caption(dict(LISTING, district="#ولیعصر"))
        self.assertIn("\n#ولیعصر #املاک_تبریز", caption)

    def test_emoji_the_font_cannot_draw_are_dropped(self):
        self.assertEqual(self.story._drawable("\u203c\ufe0f\u203c\ufe0fسازنده مطمئن 🔸"), "سازنده مطمئن")
        self.assertEqual(self.story._shape("✅ پیش‌فروش ✨"), self.story._shape("پیش‌فروش"))
        self.assertIn("\u200c", self.story._drawable("پیش‌فروش"))  # ZWNJ stays

    def test_logo_is_shipped_and_loaded(self):
        self.assertTrue(self.story.LOGO_PATH.is_file())
        self.assertEqual(self.story.LOGO_PATH.parent.name, "fonts")  # deployment copies app/fonts
        self.assertEqual(self.story._logo(170).height, 170)

    def test_no_channel_handle_is_drawn(self):
        source = (APP / "instagram_story.py").read_text(encoding="utf-8")
        self.assertNotIn("brand", source)
        image_code = source[source.index("# ---------------------------------------------------------------- image"):
                            source.index("# ---------------------------------------------------------------- telegram")]
        for name in ("username", "get_destination_channel", "t.me", "channel_post"):
            self.assertNotIn(name, image_code)

    def test_caption_ends_with_our_channel_post(self):
        story = self.story
        listing = dict(LISTING, published_message_id=1620, channel_post_url="https://t.me/old/1")
        self.assertEqual(story.channel_post_link(listing, "@nimatmelk"), "https://t.me/nimatmelk/1620")
        self.assertEqual(story.channel_post_link(listing, None), "https://t.me/old/1")
        self.assertIsNone(story.channel_post_link(LISTING, "nimatmelk"))
        self.assertEqual(story.with_channel_link("متن", "https://t.me/nimatmelk/1620"),
                         "متن\n\n🔗 لینک فایل در کانال:\nhttps://t.me/nimatmelk/1620")
        self.assertEqual(story.with_channel_link("متن", None), "متن")
        long = story.with_channel_link("x" * 1500, "https://t.me/nimatmelk/1620")
        self.assertEqual(len(long), 1024)
        self.assertTrue(long.endswith("https://t.me/nimatmelk/1620"))

    def test_region_name_is_bold_after_a_light_label(self):
        font = self.story._font(self.story.FONT_MEDIUM, 44)
        (label, label_font, label_fill), (region, region_font, region_fill) = \
            self.story._tag_parts("منطقه: ولیعصر", font)
        self.assertEqual((label, label_font, label_fill), ("منطقه:", font, self.story.RED_TINT))
        self.assertEqual((region, region_fill), ("ولیعصر", self.story.TEXT))
        self.assertTrue(region_font.path.endswith("IRANSansWeb_Black.ttf"))
        self.assertEqual(self.story._tag_parts("تبریز", font), [("تبریز", font, self.story.RED_TINT)])

    def test_unreadable_photos_are_reported(self):
        with self.assertRaises(ValueError):
            self.story.render_story([b"not an image"], LISTING, "x", "y")

    def openai(self, answer):
        body = json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": answer}]}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(self.story.urllib.request, "urlopen", return_value=response) as call:
            caption = self.story.openai_caption(LISTING, random.Random(1))
        return caption, json.loads(call.call_args[0][0].data.decode())

    def test_openai_caption_keeps_price_out_and_the_call_to_action_in(self):
        caption, sent = self.openai("۸۱ متر، ۲ خواب در ولیعصر جنوبی\nفقط ۱۰ میلیارد تومان\n#تبریز")
        self.assertEqual(caption, "\n".join([
            "۸۱ متر، ۲ خواب در ولیعصر جنوبی",
            "#تبریز",
            "",
            "💬 برای دریافت قیمت و مشخصات، عدد ۲۷۳ رو ارسال کنید",
        ]))
        facts = json.loads(sent["input"])
        self.assertEqual(facts["facts"][1], "۸۱ متر")
        self.assertEqual(facts["call_to_action"], "برای دریافت قیمت و مشخصات، عدد ۲۷۳ رو ارسال کنید")
        self.assertNotIn("price", sent["input"])
        self.assertIn("Do not open with a slogan", sent["instructions"])

    def test_openai_caption_with_the_code_is_kept_as_written(self):
        written = "۸۱ متری نورگیر\n💬 عدد ۲۷۳ رو ارسال کنید"
        self.assertEqual(self.openai(written)[0], written)

    def test_empty_or_failed_openai_caption_falls_back_to_the_template(self):
        self.assertEqual(self.openai("فقط ۱۰ میلیارد")[0], self.story.fallback_caption(LISTING))
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(self.story.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(self.story.openai_caption(LISTING), self.story.fallback_caption(LISTING))
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.story.openai_caption(LISTING), self.story.fallback_caption(LISTING))

    def test_price_range(self):
        parse = self.story.parse_price_range
        self.assertEqual(parse("۱۰ تا ۱۲ میلیارد"), (10, 12))
        self.assertEqual(parse("12-10"), (10, 12))
        self.assertEqual(parse("زیر ۸"), (None, 8))
        self.assertEqual(parse("بالای ۲۰ میلیارد"), (20, None))
        self.assertEqual(parse("۱۰٫۵"), (parse("10.5")[0], parse("10.5")[1]))
        low, high = parse("10")
        self.assertTrue(low < 10 < high)
        self.assertIsNone(parse("سلام"))
        label = self.story.price_range_label
        self.assertEqual(label(parse("۱۰ تا ۱۲")), "۱۰ تا ۱۲ میلیارد")
        self.assertEqual(label(None), "همه قیمت‌ها")
        self.assertEqual(label((None, 8)), "تا ۸ میلیارد")
        self.assertEqual(label((20, None)), "از ۲۰ میلیارد به بالا")

    def test_story_count(self):
        self.assertEqual(self.story.parse_story_count("۵"), 5)
        self.assertEqual(self.story.parse_story_count("10 تا"), 10)
        self.assertIsNone(self.story.parse_story_count("11"))
        self.assertIsNone(self.story.parse_story_count("سلام"))

    def test_sends_the_requested_number_of_full_size_story_files_one_by_one(self):
        calls = self.send_stories(fail_first=False)
        # Only files: Telegram would shrink a photo to 720×1280.
        self.assertEqual([c for c in calls if c[0] in ("photo", "file")], [
            ("file", "story-273.jpg", (1080, 1920), "🏡 ۸۱ متر، ۲ خواب | منطقه: ولیعصر جنوبی"),
            ("file", "story-274.jpg", (1080, 1920), "🏡 ۸۱ متر، ۲ خواب | منطقه: ولیعصر جنوبی"),
        ])
        self.assertEqual(calls[-1], ("message", "✅ ۲ استوری ساخته شد (۱۰ تا ۱۲ میلیارد)."))
        # Each sent story is logged, so the listing is skipped for 48 hours.
        self.assertEqual(self.logged, [(0, 7), (1, 7)])

    def test_a_story_that_fails_to_send_is_not_counted(self):
        calls = self.send_stories(fail_first=True)
        self.assertEqual([c[1] for c in calls if c[0] == "file"], ["story-274.jpg", "story-275.jpg"])
        self.assertEqual(calls[-1], ("message", "✅ ۲ استوری ساخته شد (۱۰ تا ۱۲ میلیارد)."))
        self.assertEqual(self.logged, [(1, 7), (2, 7)])

    def test_running_out_of_listings_mentions_the_48_hours(self):
        calls = self.send_stories(fail_first=False, available=1)
        self.assertEqual(calls[-1][0], "message")
        self.assertIn("✅ ۱ استوری ساخته شد", calls[-1][1])
        self.assertIn("۴۸ ساعت گذشته", calls[-1][1])
        calls = self.send_stories(fail_first=False, available=0)
        self.assertIn("۴۸ ساعت گذشته استوری نشده باشه", calls[-1][1])

    def send_stories(self, fail_first, available=4):
        data = photo((90, 120, 160))
        calls = []
        self.logged = []
        self_test = self

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

            async def send_photo(self, *args, **kwargs):
                calls.append(("photo",))

            async def send_document(self, chat_id, document, filename, caption, **timeouts):
                if fail_first and not any(c[0] == "failed" for c in calls):
                    calls.append(("failed",))
                    raise TimeoutError("Timed out")
                # A big file gets more time than Telegram's 5s default.
                self_test.assertEqual(timeouts["read_timeout"], 60)
                size = Image.open(document).size
                lines = caption.splitlines()
                self_test.assertEqual(lines[-2:], ["🔗 لینک فایل در کانال:", f"https://t.me/nimatmelk/{filename[6:9]}"])
                calls.append(("file", filename, size, lines[0]))

        listings = [dict(LISTING, id=i, unit_code=f"{273 + i}10", internal_sequence=273 + i,
                         published_message_id=273 + i, photo_file_ids=["a", "b"])
                    for i in range(available)]
        layouts = []
        real_render = self.story.render_story

        def render(photos, listing, kicker, headline, layout=0):
            layouts.append(layout % 4)
            self.assertEqual((kicker, headline), ("منطقه: ولیعصر جنوبی", "۸۱ متر، ۲ خواب"))
            return real_render(photos, listing, kicker, headline, layout)

        with mock.patch.object(self.story, "random_story_listings", return_value=listings) as pick, \
                mock.patch.object(self.story, "render_story", render), \
                mock.patch.object(self.story, "record_story", lambda *row: self.logged.append(row)), \
                mock.patch.object(self.story, "STORY_SEND_SPACING_SECONDS", 0), \
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            asyncio.run(self.story.make_stories(Bot(), 7, 2, (10, 12)))
        pick.assert_called_once_with(2, 10, 12)
        self.assertEqual(len(set(layouts)), min(2, available))  # a failed send leaves its layout
        return calls

    def test_flow_asks_count_then_price(self):
        started = []
        context = types.SimpleNamespace(user_data={})
        edits = []

        class Query:
            data = "insta_story_n_5"
            message = types.SimpleNamespace(chat_id=7)

            async def edit_message_text(self, text, reply_markup=None):
                edits.append(text)

        with mock.patch.object(self.story, "_start_batch",
                               lambda ctx, chat, count, price: started.append((count, price)) or True):
            asyncio.run(self.story.handle_instagram_story_button(Query(), context))
            self.assertEqual(context.user_data, {"insta_story_count": 5, "state": "insta_story_price"})
            self.assertIn("بازهٔ قیمت", edits[-1])

            update = types.SimpleNamespace(
                message=types.SimpleNamespace(text="۱۰ تا ۱۲ میلیارد"),
                effective_chat=types.SimpleNamespace(id=7),
            )
            asyncio.run(self.story.handle_instagram_story_text(update, context, allowed=True))
            self.assertEqual(started, [(5, (10, 12))])
            self.assertEqual(context.user_data, {})

            context.user_data.update({"insta_story_count": 3, "state": "insta_story_price"})
            Query.data = "insta_story_all"
            asyncio.run(self.story.handle_instagram_story_button(Query(), context))
            self.assertEqual(started[-1], (3, None))


@unittest.skipUnless(Image is not None and psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class StoryRepeatDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.story = load_story_module()
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute("""
                DROP TABLE IF EXISTS instagram_story_log, property_attributes, property_media,
                    property_trash, properties CASCADE;
                CREATE TABLE properties (
                    id BIGINT PRIMARY KEY, unit_code TEXT, district TEXT, area_m2 NUMERIC,
                    bedrooms INT, floor_text TEXT, build_year INT, has_parking BOOL,
                    has_elevator BOOL, has_storage BOOL, internal_sequence INT, direction TEXT,
                    address_text TEXT, raw_text TEXT, status TEXT, published_message_id BIGINT,
                    channel_post_url TEXT,
                    deleted_from_channel_at TIMESTAMPTZ, current_price_billion NUMERIC);
                CREATE TABLE property_media (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, telegram_file_id TEXT,
                    media_type TEXT DEFAULT 'PHOTO', position INT DEFAULT 0);
                CREATE TABLE property_trash (property_id BIGINT PRIMARY KEY);
                CREATE TABLE property_attributes (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, attribute_key TEXT,
                    attribute_label TEXT, attribute_value TEXT, display_order INT,
                    is_visible BOOL DEFAULT TRUE);
                INSERT INTO properties (id, unit_code, internal_sequence, status,
                                        published_message_id, current_price_billion)
                SELECT g, g::text || '11', g, 'ACTIVE', g, 11 FROM generate_series(1, 3) g;
                INSERT INTO property_media (property_id, telegram_file_id)
                SELECT g, 'photo' || g FROM generate_series(1, 3) g;
            """)

    def test_a_listing_gets_one_story_per_48_hours(self):
        with mock.patch.object(self.story, "db_connect", self.connect):
            self.story.ensure_story_log_schema()
            self.story.ensure_story_log_schema()  # idempotent at every start
            pick = lambda: sorted(item["id"] for item in self.story.random_story_listings(5, 10, 12))
            self.assertEqual(pick(), [1, 2, 3])
            self.story.record_story(1, 7)
            self.assertEqual(pick(), [2, 3])
            with self.connect() as conn:
                conn.execute("INSERT INTO instagram_story_log (property_id, chat_id, created_at) "
                             "VALUES (2, 8, NOW() - INTERVAL '49 hours'), (3, 8, NOW() - INTERVAL '47 hours')")
            # Another admin's story counts too; one older than 48 hours does not.
            self.assertEqual(pick(), [2])


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
        self.assertIn("    ensure_story_log_schema()\n", bot)
        # The unit search finds a listing by the short code the stories ask for.
        self.assertEqual(bot.count("OR internal_sequence::text = %s"), 1)
        self.assertEqual(bot.count("OR p.internal_sequence::text = %s"), 1)


if __name__ == "__main__":
    unittest.main()
