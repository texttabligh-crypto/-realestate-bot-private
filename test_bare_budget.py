"""A customer who answers with just «11» is shown homes around 11 billion."""
import ast
import re
import unittest
from decimal import Decimal
from pathlib import Path

SOURCE = (Path(__file__).parent / "app" / "shahfile_smart.py").read_text(encoding="utf-8")


def load_reader():
    tree = ast.parse(SOURCE)
    wanted = {"bare_budget_billion", "normalize_digits", "BARE_BUDGET_MIN_BILLION", "BARE_BUDGET_MAX_BILLION"}
    nodes = [
        n for n in tree.body
        if getattr(n, "name", None) in wanted
        or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in wanted | {"_DIGIT_MAP"})
    ]
    ns = {"re": re, "Decimal": Decimal, "Any": object}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "shahfile_smart.py", "exec"), ns)
    return ns["bare_budget_billion"]


class BareBudgetTest(unittest.TestCase):
    def setUp(self):
        self.read = load_reader()

    def test_numbers_alone_are_a_budget(self):
        for text, value in (("11", 11.0), ("۱۱", 11.0), (" 40 ", 40.0), ("7.5", 7.5), ("۷/۵", 7.5), ("۷٫۵", 7.5)):
            self.assertEqual(self.read(text), value, text)

    def test_other_messages_are_left_alone(self):
        for text in ("1", "2", "150", "32536", "09144139040", "11 میلیارد", "واحد 11", "", "سلام"):
            self.assertIsNone(self.read(text), text)

    def test_customer_handler_offers_it_before_ai_and_advisor(self):
        handler = SOURCE[SOURCE.index("async def handle_customer_text"):]
        check = handler.index("bare_budget_billion(text)")
        self.assertLess(check, handler.index("ai_intent = await extract_ai_intent("))
        self.assertLess(check, handler.index("escalate_customer_question"))
        self.assertIn('"price_target_billion": bare_budget', handler)


if __name__ == "__main__":
    unittest.main()
