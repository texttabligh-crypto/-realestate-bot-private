"""Source channels' own links and messenger handles must never reach our posts."""
import ast
import asyncio
import logging
import sys
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
sys.path.insert(0, str(APP))

from property_parser import (  # noqa: E402
    is_direction_only_detail,
    is_headline_detail,
    is_promotional_attribute,
    parse_dynamic_attributes,
)

SOURCE = """🏡 کد فایل: ۱۰۴۴۱
📍 زعفرانیه
🔷 ۲۰۵ متر
🔷 تک‌مستر
⚜️ قیمت کل: ۴۱ میلیارد تومان
🔗 تلگرام: https://t.me/bashirimelk
🔗 بله: https://ble.ir/bashiri_melk
ایتا: eitaa.com/bashiri
روبیکا: @bashiri_melk"""


class PromotionalDetailTest(unittest.TestCase):
    def test_parser_drops_source_links(self):
        values = " ".join(str(a["attribute_value"]) for a in parse_dynamic_attributes(SOURCE))
        for leaked in ("ble.ir", "eitaa", "t.me", "@bashiri"):
            self.assertNotIn(leaked, values)
        self.assertIn("تک مستر", values)

    def test_yes_as_a_value_is_not_an_ad(self):
        self.assertFalse(is_promotional_attribute("پارکینگ", "بله"))
        self.assertTrue(is_promotional_attribute("🔗 بله", "https://ble.ir/x"))
        self.assertTrue(is_promotional_attribute("جزئیات", "پیج ما: instagram.com/x"))

    def test_saved_promotional_details_are_removed_and_posts_refreshed(self):
        deleted, refreshed = [], []

        async def refresh(bot, property_id):
            refreshed.append(property_id)

        async def no_sleep(seconds):
            return None

        source = (APP / "bot.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if getattr(n, "name", None) == "remove_promotional_details")
        ns = {
            "asyncio": type("A", (), {"sleep": staticmethod(no_sleep)}),
            "logging": logging,
            "AREA_REPAIR_EDIT_SPACING_SECONDS": 0,
            "is_promotional_attribute": is_promotional_attribute,
            "is_headline_detail": is_headline_detail,
            "is_direction_only_detail": is_direction_only_detail,
            "_auto_detail_attribute_rows": lambda: [
                (5, "generic_aaa", "بله", "https://ble.ir/bashiri_melk"),
                (5, "source_detail_b", "جزئیات", "تک مستر"),
                (7, "generic_ccc", "پارکینگ", "بله"),
                (9, "generic_ddd", "ایتا", "eitaa.com/x"),
                # Unit 32932: the source's title line and «موقعیت: جنوبی».
                (11, "source_detail_e", "جزئیات", "فروش آپارتمان بازسازی شده 170 متری در شهرک پرواز"),
                (11, "generic_fff", "موقعیت", "جنوبی"),
                (11, "generic_ggg", "موقعیت محلی", "بهترین کوی شهرک پرواز"),
                # No direction stored: its only direction line stays.
                (13, "source_detail_h", "جزئیات", "موقعیت شمالی"),
            ],
            "_properties_with_direction": lambda ids: {11} & set(ids),
            "_delete_attributes": lambda keys: deleted.extend(keys),
            "refresh_published_property_post": refresh,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), ns)
        asyncio.run(ns["remove_promotional_details"](type("App", (), {"bot": None})()))
        self.assertEqual(
            deleted,
            [(5, "generic_aaa"), (9, "generic_ddd"), (11, "source_detail_e"), (11, "generic_fff")],
        )
        self.assertEqual(refreshed, [5, 9, 11])


if __name__ == "__main__":
    unittest.main()
