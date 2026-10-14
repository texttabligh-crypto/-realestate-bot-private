"""Area must come from the listing's size, not from street names like «۳۵ متری سینا»."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from property_parser import parse_property  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
