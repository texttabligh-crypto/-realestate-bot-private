"""Startup location repair must not re-edit channel posts that are already correct."""
import ast
import asyncio
import logging
import unittest
from pathlib import Path


def load_repair(namespace):
    source = Path(__file__).parent.joinpath("app", "bot.py").read_text(encoding="utf-8")
    nodes = [
        n for n in ast.parse(source).body
        if getattr(n, "name", None) in {"_location_compare_key", "repair_extended_source_locations"}
    ]
    namespace.update({"asyncio": asyncio, "logging": logging})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace["repair_extended_source_locations"]


class LocationRepairTest(unittest.TestCase):
    def test_only_changed_addresses_are_saved_and_refreshed(self):
        saved, refreshed = [], []

        async def refresh(bot, property_id):
            refreshed.append(property_id)
            return True

        async def no_sleep(*args):
            return None

        repair = load_repair({
            "FORCED_SOURCE_ADDRESS_BY_UNIT_CODE": {"100": "ونک خیابان ملاصدرا", "101": "پونک"},
            "_forced_location_repair_rows": lambda: [
                (1, "100", "ونک خیابان ملاصدرا"),  # already applied
                (2, "101", "پون"),                 # needs repair
            ],
            "_extended_location_repair_rows": lambda: [
                (3, "200", "ولیعصر", "raw-same"),        # parsed address unchanged
                (4, "201", "ولیعصر", "raw-longer"),      # parsed address is more precise
                (5, "31427", "#ولیعصر", "raw-longer"),   # old hashtag-only address
                (6, "300", "ونک", "raw-longer"),         # unrelated address is never applied
            ],
            "parse_property": lambda raw: {
                "address_text": "ولیعصر" if raw == "raw-same" else "ولیعصر مولوی"
            },
            "save_property_address": lambda pid, address: saved.append((pid, address)),
            "refresh_published_property_post": refresh,
        })
        asyncio.sleep, real_sleep = no_sleep, asyncio.sleep
        try:
            asyncio.run(repair(type("App", (), {"bot": None})()))
        finally:
            asyncio.sleep = real_sleep

        self.assertEqual(saved, [(2, "پونک"), (4, "ولیعصر مولوی"), (5, "ولیعصر مولوی")])
        self.assertEqual(refreshed, [2, 4, 5])


if __name__ == "__main__":
    unittest.main()
