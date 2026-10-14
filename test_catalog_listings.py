"""Multi-listing «catalog» source posts must never mix units into one listing."""
import ast
import asyncio
import logging
import sys
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
sys.path.insert(0, str(APP))

from property_parser import (  # noqa: E402
    parse_dynamic_attributes,
    parse_property,
    parse_source_unit_code,
    split_listing_blocks,
)

UNIT_152 = """🏡 کد فایل:152

📍 نصر |کوی دانش

🔷75متر
🔷 طبقه 1 از 5
🔷 5 واحد در طبقه
🔷 1 خواب
🔷 سند قرار داد شرکت معتبر
🔷 نصبیات کامل
🔷 سال ساخت 1394
🔷 پارکینگ دار
🔷 با آسانسور

⚜️ قیمت کل: 7 میلیارد و 900 میلیون تومان
______
"""

UNIT_3412 = """🏡 کد فایل: 3412

📍 ولی امر | ساکو

🔴 90متر
🔴 تک‌واحدی
🔴 تعداد واحد در طبقه: 3
🔴 موقعیت شمالی
🔴 سند ششدانگ
🔴 پارکینگ ندارد
🔴 آسانسور ندارد
🔴 انباری دارد
🔴 این واحد فروش رفت

⚜️ قیمت کل: 8 میلیارد و 500 میلیون تومان
"""

CATALOG = "#آپارتمان_7_9\n\nواحد های بین 7 تا 9 میلیارد:\n\n" + UNIT_152 + "\n" + UNIT_3412


def attrs(text):
    return {a["attribute_label"]: a["attribute_value"] for a in parse_dynamic_attributes(text)}


class CatalogParsingTest(unittest.TestCase):
    def test_catalog_is_split_per_unit(self):
        self.assertEqual([code for code, _ in split_listing_blocks(CATALOG)], ["152", "3412"])
        self.assertEqual(parse_source_unit_code(CATALOG), "152")

    def test_catalog_parses_as_its_first_unit_only(self):
        parsed = parse_property(CATALOG)
        self.assertEqual(parsed["area_m2"], 75.0)
        self.assertIs(parsed["has_parking"], True)
        self.assertIs(parsed["has_elevator"], True)
        self.assertIsNone(parsed["has_storage"])
        self.assertIsNone(parsed["direction"])
        self.assertEqual(parsed["current_price_billion"], 7.9)
        details = attrs(CATALOG)
        self.assertEqual(details.get("تعداد واحد در طبقه"), "5")
        self.assertEqual(details.get("نوع سند"), "قرار داد شرکت معتبر")
        self.assertNotIn("جزئیات", details)

    def test_single_listing_is_untouched(self):
        single = "🏡 کد فایل:152\n📍 نصر\n75 متر\nپارکینگ ندارد\nمشابه کد فایل 140 هم داریم\nقیمت کل: 7 میلیارد"
        self.assertEqual(split_listing_blocks(single), [])
        self.assertIs(parse_property(single)["has_parking"], False)


def load_sync(namespace):
    source = (APP / "bot.py").read_text(encoding="utf-8")
    names = {"sync_edited_source_post", "source_text_is_sold"}
    nodes = [n for n in ast.parse(source).body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace["sync_edited_source_post"]


class EditedCatalogSyncTest(unittest.TestCase):
    def run_sync(self, stored_code):
        calls = []

        async def record_async(name, *args, **kwargs):
            calls.append((name, args, kwargs))
            return {"synced": 0, "removed": 0}

        def recorder(name):
            def call(*args, **kwargs):
                calls.append((name, args, kwargs))
            return call

        sync = load_sync({
            "logging": logging,
            "re": __import__("re"),
            "normalize_digits": lambda value: value,
            "parse_source_unit_code": parse_source_unit_code,
            "split_listing_blocks": split_listing_blocks,
            "parse_property": parse_property,
            "parse_dynamic_attributes": parse_dynamic_attributes,
            "parse_rule_attributes": lambda text, rules: [],
            "get_active_attribute_rules": lambda: [],
            "find_property_for_source_sync": lambda **kwargs: 3197,
            "get_source_unit_code_for_message": lambda channel, message: stored_code,
            "mark_property_sold": recorder("sold"),
            "_get_property_current_price_toman": lambda pid: 7_900_000_000,
            "_get_property_region_category_names": lambda pid: set(),
            "update_parsed_property": recorder("update"),
            "save_property_address": recorder("address"),
            "save_property_source": recorder("source"),
            "clear_source_detail_attributes": recorder("clear_details"),
            "save_property_attributes": recorder("attributes"),
            "refresh_published_property_post": lambda bot, pid: record_async("refresh", pid),
            "queue_targeted_category_sync": lambda *a, **k: record_async("categories"),
            "sync_categories_for_price_change": lambda *a, **k: record_async("categories"),
        })
        post = type("Post", (), {"caption": None, "text": CATALOG, "message_id": 6401,
                                 "chat": type("Chat", (), {"id": -100})()})()
        context = type("Ctx", (), {"bot": None})()
        result = asyncio.run(sync(post, context))
        return result, calls

    def test_applies_only_the_stored_units_block(self):
        result, calls = self.run_sync("152")
        self.assertTrue(result)
        names = [name for name, _, _ in calls]
        self.assertNotIn("sold", names)  # «فروش رفت» belongs to unit 3412
        update = next(args for name, args, _ in calls if name == "update")
        self.assertIs(update[1]["has_parking"], True)
        self.assertEqual(update[1]["area_m2"], 75.0)
        source = next(kwargs for name, _, kwargs in calls if name == "source")
        self.assertEqual(source["source_unit_code"], "152")
        self.assertNotIn("3412", source["source_raw_text"])
        self.assertIn("clear_details", names)

    def test_other_unit_in_catalog_is_followed_by_its_own_code(self):
        result, calls = self.run_sync("3412")
        self.assertTrue(result)
        self.assertIn("sold", [name for name, _, _ in calls])

    def test_unit_missing_from_catalog_is_left_alone(self):
        with self.assertLogs(level=logging.WARNING):
            result, calls = self.run_sync("68")
        self.assertFalse(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
