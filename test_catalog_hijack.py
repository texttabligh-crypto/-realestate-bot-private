"""A catalog that reused a listing's source code must not keep that listing.

Unit 3197 was published from royalmelk's album 7097 (code 68, Mirdamad,
93 m, 11.3 billion). The source later pinned catalog 6401, whose units
include another «code 68». Saving that catalog's source row took over the
listing's row by code, and edits of the catalog then rewrote 3197 with other
units' details under the Mirdamad photos.
"""
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

OWN_POST = """#آپارتمان_11_12 #میرداماد

🏡 کد فایل: 68

📍 میرداماد  | برج الماس

🔹93  متر
🔹 طبقه 8 از 12
🔹 4 واحد در طبقه
🔹 2 خواب
🔹 موقعیت جنوبی
🔹 سند قرار داد شرکت معتبر
🔹 نصبیات کامل
🔹 سال ساخت 1394
🔹 پارکینگ دار
🔹 با آسانسور
🔹با انباری

⚜️ قیمت کل: 11 میلیارد و 300  میلیون تومان
"""

CATALOG = """#آپارتمان_7_9

واحد های بین 7 تا 9 میلیارد:

🏡 کد فایل: 68

📍 یاغچیان  | کوی توحید

🔹75  متر
🔹 طبقه 4 از 4
🔹 3 واحد در طبقه
🔹 بدون آسانسور

⚜️ قیمت کل: 7 میلیارد و 700  میلیون تومان
━━━━━━━━━━
🏡 کد فایل:3412

📍 ولی امر  | ساکو

🔹 65 متر
🔹 تک‌واحدی
🔹 پارکینگ ندارد

⚜️ قیمت کل: 7 میلیارد
"""


def load(names, namespace):
    source = (APP / "bot.py").read_text(encoding="utf-8")
    nodes = [n for n in ast.parse(source).body if getattr(n, "name", None) in names]
    namespace.update({
        "logging": logging,
        "asyncio": asyncio,
        "split_listing_blocks": split_listing_blocks,
        "parse_source_unit_code": parse_source_unit_code,
        "parse_property": parse_property,
        "parse_dynamic_attributes": parse_dynamic_attributes,
        "parse_rule_attributes": lambda text, rules: [],
        "get_active_attribute_rules": lambda: [],
        "AREA_REPAIR_EDIT_SPACING_SECONDS": 0,
    })
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class HijackDetectionTest(unittest.TestCase):
    def setUp(self):
        self.check = load({"catalog_hijacked_listing"}, {})["catalog_hijacked_listing"]

    def test_listing_taken_over_by_a_catalog_is_restored_from_its_own_post(self):
        parsed = self.check(OWN_POST, CATALOG, "68")
        self.assertEqual(parsed["area_m2"], 93.0)
        self.assertEqual(parsed["current_price_billion"], 11.3)
        self.assertEqual(parsed["district"], "میرداماد")

    def test_anything_less_certain_is_left_alone(self):
        self.assertIsNone(self.check(OWN_POST, OWN_POST, "68"))  # source is not a catalog
        self.assertIsNone(self.check(CATALOG, CATALOG, "68"))  # own post is a catalog
        self.assertIsNone(self.check(OWN_POST, CATALOG, "3412"))  # different code
        self.assertIsNone(self.check("", CATALOG, "68"))


class HijackRepairTest(unittest.TestCase):
    def test_repair_rewrites_the_listing_and_tracks_its_own_post(self):
        calls = []

        def rec(name):
            return lambda *a, **k: calls.append((name, a, k))

        async def arec(name, *a, **k):
            calls.append((name, a, k))

        ns = load({"restore_catalog_hijacked_listings", "catalog_hijacked_listing"}, {
            "_catalog_hijack_rows": lambda: [
                # Same column order as _catalog_hijack_rows' SELECT.
                (286, "3197", OWN_POST, -1002343450519, 7097, "royalmelkktbz", 6401, "68", CATALOG),
            ],
            "_get_property_current_price_toman": lambda pid: 7_900_000_000,
            "_get_property_region_category_names": lambda pid: {"نصر"},
            "update_parsed_property": rec("update"),
            "save_property_address": rec("address"),
            "_replace_all_attributes": rec("attributes"),
            "save_property_source": rec("source"),
            "build_source_post_url": lambda chat, user, msg: f"https://t.me/{user}/{msg}",
            "_pending_duplicates_of": lambda pid: [296],
            "detect_and_store_duplicate": lambda pid: None,
            "refresh_published_property_post": lambda bot, pid: arec("refresh", pid),
            "sync_categories_for_price_change": lambda *a, **k: arec("categories", *a, **k),
        })
        app = type("App", (), {"bot": None})()
        with self.assertLogs(level=logging.WARNING) as logs:
            asyncio.run(ns["restore_catalog_hijacked_listings"](app))

        by_name = {name: (a, k) for name, a, k in calls}
        self.assertEqual(by_name["update"][0][1]["area_m2"], 93.0)
        self.assertEqual(by_name["address"][0][1], "میرداماد | برج الماس")
        attrs = {item["attribute_key"]: item["attribute_value"] for item in by_name["attributes"][0][1]}
        self.assertEqual(attrs["units_per_floor"], "4")
        self.assertNotIn("تک‌واحدی", " ".join(attrs.values()))
        source = by_name["source"][1]
        self.assertEqual(source["source_message_id"], 7097)
        self.assertEqual(source["source_post_url"], "https://t.me/royalmelkktbz/7097")
        self.assertEqual(by_name["categories"][1]["extra_region_names"], {"نصر"})
        self.assertIn("released_duplicates=[296]", logs.output[0])


class HijackRowShapeTest(unittest.TestCase):
    def test_loop_unpacks_columns_in_select_order(self):
        source = (APP / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        rows_fn = next(n for n in tree.body if getattr(n, "name", None) == "_catalog_hijack_rows")
        query = next(
            c.value for c in ast.walk(rows_fn)
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and "SELECT" in c.value
        )
        selected = [
            line.strip().rstrip(",").split(".")[-1]
            for line in query.split("SELECT", 1)[1].split("FROM", 1)[0].splitlines()
            if line.strip()
        ]
        repair = next(n for n in tree.body if getattr(n, "name", None) == "restore_catalog_hijacked_listings")
        loop = next(n for n in ast.walk(repair) if isinstance(n, ast.For))
        names = [elt.id for elt in loop.target.elts]
        self.assertEqual(
            selected,
            ["id", "unit_code", "raw_text", "input_channel_id", "input_message_id",
             "username", "source_message_id", "source_unit_code", "source_raw_text"],
        )
        self.assertEqual(
            names,
            ["property_id", "unit_code", "raw_text", "channel_id", "message_id",
             "channel_username", "hijack_message_id", "source_code", "source_text"],
        )


class SourceRowOwnershipTest(unittest.TestCase):
    def test_a_code_never_moves_another_listings_source_row(self):
        source = (APP / "ingest.py").read_text(encoding="utf-8")
        fn = source[source.index("def _save_property_source_raw"):source.index("def save_property_address")]
        self.assertIn("AND property_id = %s", fn)

    def test_edits_follow_a_code_only_to_rows_without_a_message(self):
        source = (APP / "bot.py").read_text(encoding="utf-8")
        fn = source[source.index("def find_property_for_source_sync"):source.index("def get_source_unit_code_for_message")]
        self.assertIn("AND ps.source_message_id IS NULL", fn)


if __name__ == "__main__":
    unittest.main()
