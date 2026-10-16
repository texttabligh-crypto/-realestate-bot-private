"""A feature bullet under «✨ ویژگی‌های ملک» must not become the district.

30825 was saved with the district «▫️سازنده مطمئن»: the district fallback
read «ملک» in the feature heading and took the next line. Its story showed
that line, and the listing was left out of the «ولیعصر» category post.

The database test runs against a scratch PostgreSQL given in PGTEST_DSN
(for example "host=/tmp/pgtest port=55432 user=postgres dbname=t3") and is
skipped otherwise.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None

APP = Path(__file__).parent / "app"
sys.path.insert(0, str(APP))
DSN = os.getenv("PGTEST_DSN")

from property_parser import is_feature_line_district, parse_property  # noqa: E402

# The 30825 source post without its contact lines.
FEATURE_LIST_POST = """#آپارتمان_20_25
#ولیعصر

#پیش_فروش

🏷 کد فایل: 517

🏡 پیش فروش آپارتمان |
ولیعصر پروین اعتصامی

📐۱۲۵ متر | 🛏 ۲ خواب |
🏢 طبقه ۲ ☀️ شمالی ویلایی|
🏗 تحویل ۵ ماه


✨ ویژگی‌های ملک

▫️سازنده مطمئن
▫️دارای آسانسور و پارکینگ
▫️بسیار خوش نقشه

💰قیمت کل: ۲۵ میلیارد
"""


class FeatureLineDistrictTest(unittest.TestCase):
    def test_feature_bullet_is_not_the_district(self):
        parsed = parse_property(FEATURE_LIST_POST)
        self.assertEqual(parsed["district"], "ولیعصر پروین اعتصامی")
        self.assertEqual(parsed["address_text"], "ولیعصر پروین اعتصامی")

    def test_plain_line_under_a_feature_heading_is_skipped(self):
        post = "✨ مشخصات ملک\nسازنده مطمئن\n\n۱۰۰ متر"
        self.assertNotEqual(parse_property(post)["district"], "سازنده مطمئن")

    def test_other_fallbacks_still_work(self):
        self.assertEqual(parse_property("فروش آپارتمان ولیعصر\n۱۲۰ متر")["district"], "ولیعصر")
        self.assertEqual(
            parse_property("واحد 131 متری 2 خواب\nآدرس: کوی استانداری")["district"], "کوی استانداری")

    def test_feature_line_check(self):
        self.assertTrue(is_feature_line_district("▫️سازنده مطمئن"))
        self.assertTrue(is_feature_line_district("✨ لوکس"))
        self.assertFalse(is_feature_line_district("ولیعصر"))
        self.assertFalse(is_feature_line_district(""))
        self.assertFalse(is_feature_line_district(None))


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class FeatureLineDistrictRepairTest(unittest.TestCase):
    def setUp(self):
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute("""
                DROP TABLE IF EXISTS properties CASCADE;
                CREATE TABLE properties (
                    id BIGINT PRIMARY KEY, unit_code TEXT, status TEXT, district TEXT,
                    address_text TEXT, raw_text TEXT, deleted_from_channel_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ);
            """)
            conn.execute(
                "INSERT INTO properties (id, unit_code, status, district, address_text, raw_text) VALUES "
                "(357, '30825', 'ACTIVE', '▫️سازنده مطمئن', 'ولیعصر پروین اعتصامی', %s), "
                "(358, '30920', 'ACTIVE', 'کوی استانداری', NULL, 'x'), "
                "(359, '31020', 'INACTIVE', '▫️سازنده مطمئن', NULL, %s), "
                "(360, '31120', 'ACTIVE', '🔸لوکس', NULL, '📐۱۰۰ متر | 🛏 ۲ خواب')",
                (FEATURE_LIST_POST, FEATURE_LIST_POST),
            )

    def test_repairs_live_listings_only(self):
        tree = ast.parse((APP / "bot.py").read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if getattr(n, "name", None) == "repair_feature_line_districts"]
        ns = {"db_connect": self.connect, "parse_property": parse_property,
              "is_feature_line_district": is_feature_line_district}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), ns)

        self.assertEqual(ns["repair_feature_line_districts"](), [
            (357, "30825", "ولیعصر پروین اعتصامی", True),
            (360, "31120", None, False),
        ])
        with self.connect() as conn:
            districts = dict(conn.execute("SELECT id, district FROM properties"))
        self.assertEqual(districts, {
            357: "ولیعصر پروین اعتصامی", 358: "کوی استانداری", 359: "▫️سازنده مطمئن", 360: None,
        })
        self.assertEqual(ns["repair_feature_line_districts"](), [])


class WiringTest(unittest.TestCase):
    def test_repair_runs_before_the_category_reconcile(self):
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        delayed = bot[bot.index("async def delayed_extended_source_location_repair"):]
        self.assertLess(delayed.index("repair_feature_line_districts"),
                        delayed.index("await repair_extended_source_locations(application)"))
        self.assertIn("CATEGORY_RECONCILE_STARTUP_DELAY_SECONDS = 150", bot)
        self.assertIn("await asyncio.sleep(15)", delayed[:600])


if __name__ == "__main__":
    unittest.main()
