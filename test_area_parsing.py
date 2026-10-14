"""Area must come from the listing's size, not from street names like «۳۵ متری سینا»."""
import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from property_parser import normalize_text, parse_property  # noqa: E402

SOURCE_10338 = """#آپارتمان_۱۴۳_متری #ائل_گلی

🏡 کد فایل: ۱۰۳۳۸

📍 ائل‌گلی | ۳۵ متری سینا

🔷 ۱۴۳/۵ متر
🔷 دوکله
🔷 ساخت ۱۴۰۳
🔷 ۳ خواب
🔷 سند تک‌برگ

⚜️ قیمت کل: ۳۸ میلیارد تومان"""


class AreaParsingTest(unittest.TestCase):
    def area(self, text):
        return parse_property(text)["area_m2"]

    def test_street_name_in_location_is_not_the_area(self):
        parsed = parse_property(SOURCE_10338)
        self.assertEqual(parsed["area_m2"], 143.5)
        self.assertIn("35 متری سینا", parsed["address_text"])

    def test_slash_decimal(self):
        self.assertEqual(self.area("۱۴۳/۵ متر\nقیمت ۳۸ میلیارد"), 143.5)

    def test_common_area_forms(self):
        self.assertEqual(self.area("🔷75متر\nقیمت 7 میلیارد"), 75.0)
        self.assertEqual(self.area("متراژ: 120\nقیمت 10 میلیارد"), 120.0)
        self.assertEqual(self.area("۱۲۰ مترمربع"), 120.0)
        self.assertEqual(self.area("آپارتمان ۷۵ متری نوساز\nقیمت ۷ میلیارد"), 75.0)

    def test_metri_on_location_line_is_skipped(self):
        self.assertEqual(self.area("📍 ولیعصر | ۲۰ متری دوم\nواحد ۹۰ متری\nقیمت ۹ میلیارد"), 90.0)
        self.assertIsNone(self.area("📍 ائل گلی | ۳۵ متری سینا\nقیمت ۳۸ میلیارد"))


def load_repair():
    source = (Path(__file__).parent / "app" / "bot.py").read_text(encoding="utf-8")
    names = {"_legacy_parsed_area", "find_misparsed_area"}
    nodes = [n for n in ast.parse(source).body if getattr(n, "name", None) in names]
    ns = {"re": re, "normalize_text": normalize_text, "parse_property": parse_property}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), ns)
    return ns["find_misparsed_area"]


class MisparsedAreaRepairTest(unittest.TestCase):
    def setUp(self):
        self.find = load_repair()

    def test_street_name_area_is_corrected(self):
        self.assertEqual(self.find(35, [SOURCE_10338]), 143.5)

    def test_correct_or_manually_edited_area_is_left_alone(self):
        self.assertIsNone(self.find(143.5, [SOURCE_10338]))  # already fixed by hand
        self.assertIsNone(self.find(80, [SOURCE_10338]))     # operator's own value
        self.assertIsNone(self.find(75, ["🔷75متر\nقیمت 7 میلیارد"]))

    def test_no_reliable_size_means_no_change(self):
        self.assertIsNone(self.find(35, ["📍 ائل گلی | ۳۵ متری سینا\nقیمت ۳۸ میلیارد"]))

    def test_first_text_matching_the_stored_value_decides(self):
        self.assertEqual(self.find(35, [None, "🔷75متر", SOURCE_10338]), 143.5)


if __name__ == "__main__":
    unittest.main()
