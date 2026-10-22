"""«ولیعصر ۲» in the manager search finds Valiasr 2, not every Valiasr listing.

The owner searched «ولیعصر ۲» and got all 63 Valiasr listings: the district
was matched, and the «۲» on its own was looked for anywhere in the text, where
almost every post has a 2. A small number right after a district is now part
of its name.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from composite_search import composite_search_terms, composite_text_predicates  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None

DSN = os.getenv("PGTEST_DSN")


class TermsTest(unittest.TestCase):
    def test_a_number_after_a_district_belongs_to_it(self):
        for query in ("ولیعصر ۲", "ولیعصر 2", "ولیعصر۲", "#ولیعصر_۲", "ولیعصر فاز ۲", "ولیعصر-۲"):
            self.assertEqual(composite_search_terms(query), [("ولیعصر 2", "numbered")], query)
        self.assertEqual(composite_search_terms("ولیعصر"), [("ولیعصر", "location")])
        self.assertEqual(composite_search_terms("ولیعصر ۲ پارکینگ"),
                         [("ولیعصر 2", "numbered"), ("پارکینگ", "text")])
        # Bigger numbers stay what they were: an area, a price, a code.
        self.assertEqual(composite_search_terms("ولیعصر ۱۲۰"), [("ولیعصر", "location"), ("120", "text")])


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(DSN)
        self.conn.execute("""
            DROP TABLE IF EXISTS district_search_probe, district_search_sources;
            CREATE TABLE district_search_probe (
                id INT, unit_code TEXT, district TEXT, neighborhood TEXT, address_text TEXT,
                raw_text TEXT, floor_text TEXT, area_m2 NUMERIC, current_price_billion NUMERIC);
            CREATE TABLE district_search_sources (
                property_id INT, is_active BOOL, source_raw_text TEXT, source_unit_code TEXT);
            INSERT INTO district_search_probe VALUES
                (1, '1', 'ولیعصر', NULL, 'ولیعصر ۲ خیابان گلستان', '۲ خواب', NULL, 90, 7),
                (2, '2', 'ولیعصر', NULL, 'ولیعصر-اعتصامی', '۲ خواب، طبقه ۲', NULL, 120, 9),
                (3, '3', 'ولیعصر', NULL, 'ولیعصر', '#ولیعصر_۲ #آپارتمان', NULL, 100, 8),
                (4, '4', 'ولیعصر', NULL, 'ولیعصر ۲۰ متری', '۳ خواب', NULL, 150, 12),
                (5, '5', 'ولیعصر', NULL, 'ولیعصر فاز ۲', NULL, NULL, 80, 6),
                (6, '6', 'ولیعصر', NULL, 'ولیعصر', 'نوساز', NULL, 95, 7),
                (7, '7', 'باغمیشه', NULL, 'باغمیشه ۲', NULL, NULL, 95, 7);
            INSERT INTO district_search_sources VALUES (6, TRUE, 'ولیعصر2 نوساز', NULL);
        """)

    def tearDown(self):
        self.conn.execute("DROP TABLE district_search_probe, district_search_sources")
        self.conn.commit()
        self.conn.close()

    def search(self, query):
        clauses, params = composite_text_predicates(query)
        sql = ("SELECT p.id FROM district_search_probe p WHERE " + " AND ".join(clauses) + " ORDER BY p.id")
        sql = sql.replace("FROM property_sources ps", "FROM district_search_sources ps")
        sql = sql.replace("FROM property_attributes pa", "FROM (SELECT NULL::int AS property_id, "
                          "NULL::text AS attribute_label, NULL::text AS attribute_value) pa")
        return [row[0] for row in self.conn.execute(sql, params).fetchall()]

    def test_valiasr_2_only(self):
        self.assertEqual(self.search("ولیعصر ۲"), [1, 3, 5, 6])
        self.assertEqual(self.search("ولیعصر 2"), [1, 3, 5, 6])

    def test_plain_valiasr_still_finds_all(self):
        self.assertEqual(self.search("ولیعصر"), [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
