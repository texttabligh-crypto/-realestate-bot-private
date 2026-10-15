"""A visit request completes when the customer types their number.

Many customers type «09144139040» instead of pressing the share-contact
button; that number used to reach an advisor as a question and the visit
request was never sent.
"""
import ast
import asyncio
import re
import sys
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
sys.path.insert(0, str(APP))

from phone_utils import normalize_iran_phone  # noqa: E402


def load(names, namespace):
    source = (APP / "visit_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        n for n in tree.body
        if getattr(n, "name", None) in names
        or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "_MOBILE_IN_TEXT")
    ]
    namespace.update({"re": re, "normalize_iran_phone": normalize_iran_phone})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "visit_flow.py", "exec"), namespace)
    return namespace


class TypedMobileTest(unittest.TestCase):
    def setUp(self):
        self.read = load({"typed_mobile_number"}, {})["typed_mobile_number"]

    def test_typed_numbers(self):
        for text in (
            "09144139040",
            "۰۹۱۴۴۱۳۹۰۴۰",
            "0914 413 9040",
            "0914-413-9040",
            "+989144139040",
            "9144139040",
            "شمارم 09144139040 هست",
        ):
            self.assertEqual(self.read(text), "09144139040", text)

    def test_not_a_phone_answer(self):
        for text in (
            "",
            "واحد 250 رو میخوام",
            "02134567890",
            "0914413904",
            "09144139040 یا 09121234567",
            "سلام " * 20 + "09144139040",
        ):
            self.assertIsNone(self.read(text), text)


class Message:
    def __init__(self, text):
        self.text = text


class Update:
    def __init__(self, text):
        self.effective_user = type("User", (), {"id": 7})()
        self.effective_message = Message(text)


class VisitPhoneTextTest(unittest.TestCase):
    def run_handler(self, text, pending=(1, 42, 250)):
        calls = []

        async def complete(update, context, *, customer_id, property_id):
            calls.append(("complete", customer_id, property_id))

        ns = load({"typed_mobile_number", "handle_visit_phone_text"}, {
            "get_customer_by_telegram_id": lambda uid: (42,),
            "get_pending_visit": lambda cid: pending,
            "save_customer_phone": lambda **kw: calls.append(("phone", kw["phone_number"])),
            "_complete_visit_request": complete,
        })
        handled = asyncio.run(ns["handle_visit_phone_text"](Update(text), None))
        return handled, calls

    def test_typed_number_completes_the_waiting_visit(self):
        handled, calls = self.run_handler("09144139040")
        self.assertTrue(handled)
        self.assertEqual(calls, [("phone", "09144139040"), ("complete", 42, 250)])

    def test_without_a_waiting_visit_the_text_is_left_alone(self):
        handled, calls = self.run_handler("09144139040", pending=None)
        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_other_text_is_left_alone(self):
        handled, calls = self.run_handler("قیمت آخرش چنده؟")
        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_smart_flow_checks_it_first_when_idle(self):
        source = (APP / "shahfile_smart.py").read_text(encoding="utf-8")
        handler = source[source.index("async def handle_customer_text"):]
        self.assertLess(
            handler.index("handle_visit_phone_text(update, context)"),
            handler.index("escalate_customer_question"),
        )


if __name__ == "__main__":
    unittest.main()
