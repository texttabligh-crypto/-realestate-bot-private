"""A source's title line and «موقعیت: جنوبی» only repeat lines already shown."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from property_parser import (  # noqa: E402
    is_direction_only_detail,
    is_headline_detail,
    parse_dynamic_attributes,
    parse_property,
)

# Shaped like the source of unit 32932.
SOURCE = """فروش آپارتمان بازسازی شده 170 متری در شهرک پرواز

📍 شهرک پرواز
📐 170 متر
🛏 3 خواب
🏢 طبقه دوم
موقعیت: جنوبی
سند ششدانگ
بازسازی کامل از صفر تا صد
موقعیت محلی: بهترین کوی شهرک پرواز
اعتبار قیمت: حداکثر 20 روز از تاریخ اعلام

💰 قیمت کل: 32 میلیارد تومان
"""


class RedundantDetailTest(unittest.TestCase):
    def test_title_lines(self):
        self.assertTrue(is_headline_detail("فروش آپارتمان بازسازی شده 170 متری در شهرک پرواز"))
        self.assertTrue(is_headline_detail("فروش فوری واحد 90 متری"))
        self.assertTrue(is_headline_detail("پیش فروش آپارتمان در ولیعصر"))
        self.assertFalse(is_headline_detail("بازسازی کامل از صفر تا صد"))
        self.assertFalse(is_headline_detail("فروشنده معتبر"))

    def test_direction_only(self):
        self.assertTrue(is_direction_only_detail("موقعیت", "جنوبی"))
        self.assertTrue(is_direction_only_detail("جزئیات", "موقعیت جنوبی"))
        self.assertTrue(is_direction_only_detail("جزئیات", "شمالی جنوبی"))
        self.assertTrue(is_direction_only_detail("جهت ساختمان", "شمالی"))
        self.assertFalse(is_direction_only_detail("موقعیت محلی", "بهترین کوی شهرک پرواز"))
        self.assertFalse(is_direction_only_detail("جزئیات", "واحدی جنوبی با پلان داخلی جذاب"))
        self.assertFalse(is_direction_only_detail("موقعیت", "دو نبش"))

    def test_listing_keeps_only_new_details(self):
        values = [item["attribute_value"] for item in parse_dynamic_attributes(SOURCE)]
        joined = " | ".join(values)
        self.assertNotIn("فروش آپارتمان", joined)
        self.assertNotIn("جنوبی", joined)
        self.assertIn("بهترین کوی شهرک پرواز", joined)
        self.assertIn("حداکثر 20 روز از تاریخ اعلام", joined)
        parsed = parse_property(SOURCE)
        self.assertEqual(parsed["direction"], "جنوبی")
        self.assertEqual(parsed["area_m2"], 170.0)


if __name__ == "__main__":
    unittest.main()
