"""Features a source writes on one line, separated by ✨, go on their own lines."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from property_parser import (  # noqa: E402
    parse_dynamic_attributes,
    parse_property,
    split_inline_features,
)

# Source of unit 32536 (code 12936 at the source).
INLINE = """📍 ولیعصر، اوحدی صدف

مشخصات ملک ✨ کد واحد: 12936 ✨ 180 متر (خوش‌متراژ) ✨ جنوبی ✨ طبقه 6 از 7 ✨ 2 خواب بزرگ ✨ کلوزت و مستر لوکس ✨ نصبیات کامل ✨ پلان داخل واحد جذاب و متفاوت ✨ هر طبقه 2 واحد ✨ ساخت 1392 ✨ پارکینگ و آسانسور ✨ سند شش‌دانگ

واحدی جنوبی با پلان داخلی جذاب و متفاوت و مستر لوکس.

💰 قیمت هر متر: 200 میلیون تومان
💰 قیمت کل: 36 میلیارد تومان
"""


def attrs(text):
    return {a["attribute_key"]: (a["attribute_label"], a["attribute_value"]) for a in parse_dynamic_attributes(text)}


class InlineFeaturesTest(unittest.TestCase):
    def test_each_feature_gets_its_own_line(self):
        details = attrs(INLINE)
        values = [value for _label, value in details.values()]
        self.assertFalse(any("✨" in value for value in values), values)
        self.assertFalse(any("12936" in value for value in values), values)  # the source's own code
        self.assertEqual(details["units_per_floor"][1], "2")
        self.assertEqual(details["installations"][1], "کامل")
        self.assertIn("کلوزت و مستر لوکس", values)

    def test_inline_floor_is_read_and_location_is_unchanged(self):
        parsed = parse_property(INLINE)
        self.assertEqual(parsed["floor_text"], "6 از 7")
        self.assertEqual(parsed["area_m2"], 180.0)
        self.assertEqual(parsed["bedrooms"], 2)
        self.assertEqual(parsed["direction"], "جنوبی")
        self.assertEqual(parsed["address_text"], "ولیعصر, اوحدی صدف")
        self.assertEqual(parsed["current_price_billion"], 36.0)

    def test_split_feature_is_never_the_address(self):
        parsed = parse_property("آپارتمان 90 متری ✨✨ فول امکانات ✨ جنوبی ✨ قیمت 5 میلیارد")
        self.assertIsNone(parsed["address_text"])
        self.assertEqual(parsed["area_m2"], 90.0)

    def test_lines_already_bulleted_are_unchanged(self):
        text = "✨ 120 متر\n✨ طبقه 3 از 5\n✨ پارکینگ دارد\nقیمت کل: 8 میلیارد"
        self.assertEqual(split_inline_features(text), text)


if __name__ == "__main__":
    unittest.main()
