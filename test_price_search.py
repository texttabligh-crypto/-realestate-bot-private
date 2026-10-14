"""«در حد ۴۰ میلیارد» must list homes around 40, not everything up to 40."""
import sys
import types
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
sys.path.insert(0, str(APP))


def load_ai():
    stubs = {
        "psycopg": types.ModuleType("psycopg"),
        "telegram": types.ModuleType("telegram"),
        "telegram.error": types.ModuleType("telegram.error"),
        "crm_panel": types.ModuleType("crm_panel"),
    }
    stubs["telegram"].InlineKeyboardButton = stubs["telegram"].InlineKeyboardMarkup = object
    stubs["telegram.error"].Forbidden = Exception
    stubs["crm_panel"].TEHRAN = None
    for name, module in stubs.items():
        sys.modules.setdefault(name, module)
    import shahfile_ai
    return shahfile_ai


ai = load_ai()


class AroundPriceTest(unittest.TestCase):
    def test_around_phrases(self):
        for text, target in (
            ("سلام خونه در حد ۴۰ میلیارد طرفای ولیعصر", 40),
            ("واحد ۴۰ میلیاردی چی دارید", 40),
            ("حدودا ۷/۵ میلیارد", 7.5),
            ("در حدود ۱۵ میلیارد", 15),
            ("تو مایه‌های ۱۲ میلیارد", 12),
            ("دور و بر ۲۰ میلیارد تو رشدیه", 20),
            ("در حد ۸۰۰ میلیون", 0.8),
        ):
            self.assertEqual(ai.approximate_price_billion(text), target, text)

    def test_limits_and_ranges_are_not_a_target(self):
        for text in (
            "تا ۴۰ میلیارد",
            "تا حدود ۴۰ میلیارد",
            "زیر ۴۰ میلیاردی",
            "بین ۳۰ تا ۴۰ میلیاردی",
            "۳۰ میلیاردی تا ۴۰ میلیاردی",
            "حدود ۳۰ تا ۴۰ میلیارد",
            "از ۴۰ میلیاردی به بالا",
            "نزدیک مترو تا ۸ میلیارد",
            "۵ تا ۶ میلیارد",
        ):
            self.assertIsNone(ai.approximate_price_billion(text), text)

    def test_ai_maximum_becomes_a_band_around_the_target(self):
        filters = ai.apply_price_target(
            {"price_max_billion": 40, "district": "ولیعصر"},
            "سلام خونه در حد ۴۰ میلیارد طرفای ولیعصر",
        )
        self.assertEqual(filters["price_min_billion"], 34.0)
        self.assertEqual(filters["price_max_billion"], 46.0)
        self.assertEqual(filters["price_target_billion"], 40.0)
        self.assertEqual(filters["district"], "ولیعصر")
        self.assertIn("حدود 40 میلیارد (34 تا 46)", ai._filters_title(filters))

    def test_ai_band_around_the_target_is_kept(self):
        filters = ai.apply_price_target(
            {"price_min_billion": 35, "price_max_billion": 45}, "در حد ۴۰ میلیارد"
        )
        self.assertEqual((filters["price_min_billion"], filters["price_max_billion"]), (35.0, 45.0))

    def test_plain_search_is_unchanged(self):
        filters = {"price_max_billion": 40.0, "district": "ولیعصر"}
        self.assertEqual(ai.apply_price_target(filters, "تا ۴۰ میلیارد ولیعصر"), filters)

    def test_closest_price_comes_first(self):
        order, params = ai._search_order({"price_min_billion": 34, "price_max_billion": 46, "price_target_billion": 40})
        self.assertTrue(order.startswith("ABS(p.current_price_billion - %s)"))
        self.assertEqual(params, [40.0])
        self.assertTrue(ai._search_order({"price_max_billion": 40})[0].startswith("p.current_price_billion DESC"))
        self.assertTrue(ai._search_order({"price_min_billion": 30, "price_max_billion": 40})[0].startswith("p.current_price_billion ASC"))


class FallbackBudgetTest(unittest.TestCase):
    """The rule-based fallback, used when the AI layer is unavailable."""

    def test_fallback_budget_around_a_price_becomes_a_band(self):
        # «ستارخان» contains «تا», so the fallback reads «up to 40».
        filters = ai.apply_price_target(
            {"price_min_billion": 0.0, "price_max_billion": 40.0}, "در حد ۴۰ میلیارد ستارخان"
        )
        self.assertEqual((filters["price_min_billion"], filters["price_max_billion"]), (34.0, 46.0))

    def test_fallback_call_site_applies_the_target(self):
        source = (APP / "shahfile_smart.py").read_text(encoding="utf-8")
        self.assertIn("search_filters = apply_price_target({", source)


if __name__ == "__main__":
    unittest.main()
