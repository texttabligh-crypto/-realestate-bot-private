"""Staff see the unit code, not «/start visit_250», in a customer's history."""
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

for name, attrs in (
    ("telegram", {}),
    ("telegram.ext", {"ExtBot": object}),
    ("crm_campaign_hub", {"db_connect": None}),
):
    module = sys.modules.setdefault(name, types.ModuleType(name))
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)

import customer_transcript as t  # noqa: E402


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, history, properties):
        self.history = history
        self.properties = properties
        self.queries = []

    def execute(self, sql, params):
        self.queries.append(sql)
        if "FROM properties" in sql:
            return Result([(pid, code) for pid, code in self.properties if pid in params[0]])
        return Result(self.history)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VisitLabelTest(unittest.TestCase):
    def page(self, bodies, properties=((250, "32611"),)):
        now = datetime.now(timezone.utc)
        conn = Conn([(i, "customer", body, now) for i, body in enumerate(bodies)], list(properties))
        t.db_connect = lambda: conn
        chunks, _more = t.history_page(7)
        return "".join(chunks), conn

    def test_visit_link_shows_the_unit_code(self):
        text, _conn = self.page(["/start visit_250", "09144139040"])
        self.assertIn("🏠 درخواست بازدید واحد 32611", text)
        self.assertNotIn("visit_250", text)
        self.assertIn("09144139040", text)

    def test_unpublished_file_shows_its_internal_number(self):
        text, _conn = self.page(["/start visit_251"])
        self.assertIn("🏠 درخواست بازدید (شمارهٔ داخلی فایل: 251)", text)

    def test_no_lookup_without_visit_links(self):
        _text, conn = self.page(["سلام", "قیمت چنده؟"])
        self.assertFalse(any("FROM properties" in q for q in conn.queries))


if __name__ == "__main__":
    unittest.main()
