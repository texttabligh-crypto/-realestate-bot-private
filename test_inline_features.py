"""Features a source writes on one line, separated by ✨, go on their own lines."""
import ast
import asyncio
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from property_parser import (  # noqa: E402
    parse_dynamic_attributes,
    parse_property,
    split_inline_features,
    split_listing_blocks,
)

APP = Path(__file__).parent / "app"

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


def load_repair(namespace):
    source = (APP / "bot.py").read_text(encoding="utf-8")
    names = {"inline_feature_repair_plan", "repair_inline_feature_details"}
    nodes = [n for n in ast.parse(source).body if getattr(n, "name", None) in names]
    namespace.update({
        "logging": logging,
        "asyncio": asyncio,
        "split_listing_blocks": split_listing_blocks,
        "parse_dynamic_attributes": parse_dynamic_attributes,
        "parse_rule_attributes": lambda text, rules: [],
        "parse_property": parse_property,
        "get_active_attribute_rules": lambda: [],
        "AREA_REPAIR_EDIT_SPACING_SECONDS": 0,
    })
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class StoredListingRepairTest(unittest.TestCase):
    """Unit 32536 was published before the fix with the run joined on one line."""

    def run_repair(self, existing_keys, floor_text=None):
        calls = []

        async def refresh(bot, pid):
            calls.append(("refresh", pid))

        ns = load_repair({
            "_inline_feature_repair_rows": lambda: [(536, "32536", floor_text, INLINE, [INLINE])],
            "_property_attribute_keys": lambda pid: (existing_keys, ["generic_joined"]),
            "_delete_attributes": lambda keys: calls.append(("delete", keys)),
            "save_property_attributes": lambda pid, attrs: calls.append(("save", [a["attribute_key"] for a in attrs])),
            "_fill_missing_floor": lambda pid, floor: calls.append(("floor", floor)),
            "refresh_published_property_post": refresh,
        })
        app = type("App", (), {"bot": None})()
        asyncio.run(ns["repair_inline_feature_details"](app))
        return dict((name, value) for name, value in calls)

    def test_joined_line_is_replaced_by_one_feature_per_line(self):
        calls = self.run_repair(["generic_joined", "deed_type"])
        self.assertEqual(calls["delete"], [(536, "generic_joined")])
        self.assertIn("units_per_floor", calls["save"])
        self.assertIn("installations", calls["save"])
        self.assertNotIn("deed_type", calls["save"])  # kept as stored
        self.assertEqual(calls["floor"], "6 از 7")
        self.assertEqual(calls["refresh"], 536)

    def test_stored_floor_is_kept(self):
        calls = self.run_repair(["generic_joined"], floor_text="6")
        self.assertNotIn("floor", calls)

    def test_catalog_text_is_left_to_its_own_repair(self):
        ns = load_repair({})
        catalog = "🏡 کد فایل: 1\n75 متر ✨ پارکینگ\nقیمت 7 میلیارد\n🏡 کد فایل: 2\n90 متر\nقیمت 8 میلیارد"
        self.assertIsNone(ns["inline_feature_repair_plan"]([catalog], [], []))


if __name__ == "__main__":
    unittest.main()
