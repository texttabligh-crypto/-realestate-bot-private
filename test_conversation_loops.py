"""The dead ends one customer hit in a single chat stay fixed.

From a showcase link the customer pressed «🔎 واحد می‌خوام», shared their
number and was told «درخواست فعالی برای ثبت شماره ندارید»; tried the help
text's example code «۲۷۲۹» (no longer live) and was handed to an advisor;
asked for a home in Tehran and got an empty «۸۵ تا ۸۵ متر» search, then the
same search offered again.
"""
import ast
import asyncio
import math
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).parent / "app"


def source(name):
    return (APP / name).read_text(encoding="utf-8")


def load(name, names, namespace):
    tree = ast.parse(source(name))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), name, "exec"), namespace)
    return namespace


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class SharedPhoneTest(unittest.TestCase):
    def phone_saved(self, resumed):
        message = Message()
        update = types.SimpleNamespace(effective_message=message)

        async def resume(update, context):
            return resumed

        ns = load("visit_flow.py", {"_phone_saved_without_request"}, {"ReplyKeyboardRemove": lambda: None})
        with mock.patch.dict(sys.modules, {"customer_showcase": types.SimpleNamespace(resume_pending_showcase=resume)}):
            asyncio.run(ns["_phone_saved_without_request"](update, None))
        return message.replies

    def test_a_phone_nothing_waits_for_is_kept_with_a_nudge(self):
        replies = self.phone_saved(resumed=False)
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("ممنون 🌷 شماره‌تون ثبت شد ✅"))
        self.assertIn("کد فایلی که توی کانال دیدید", replies[0])
        self.assertEqual(self.phone_saved(resumed=True), [])

    def test_shared_contact_is_saved_before_looking_for_a_request(self):
        visit = source("visit_flow.py")
        handler = visit[visit.index("async def handle_visit_contact"):]
        self.assertNotIn("درخواست فعالی برای ثبت شماره ندارید", visit)
        self.assertLess(handler.index("save_customer_phone("), handler.index("get_pending_visit("))
        self.assertIn("await _phone_saved_without_request(", handler)
        # Someone else's contact is still refused first.
        self.assertLess(handler.index("contact.user_id != user.id"), handler.index("save_customer_phone("))

    def test_the_showcase_link_survives_another_path(self):
        showcase = source("customer_showcase.py")
        self.assertEqual(showcase.count('remember_pending_showcase(context, token)\n        set_conversation('), 2)
        self.assertIn("context.user_data.pop(PENDING_SHOWCASE_KEY, None)", showcase)
        calls = []

        async def complete(update, context, token):
            calls.append(token)
            return True

        ns = load("customer_showcase.py", {"remember_pending_showcase", "resume_pending_showcase"},
                  {"PENDING_SHOWCASE_KEY": "pending_showcase_token", "complete_showcase_registration": complete})
        context = types.SimpleNamespace(user_data={})
        self.assertFalse(asyncio.run(ns["resume_pending_showcase"](None, context)))
        ns["remember_pending_showcase"](context, "Cnt4w8")
        self.assertTrue(asyncio.run(ns["resume_pending_showcase"](None, context)))
        self.assertEqual((calls, context.user_data), (["Cnt4w8"], {}))


class HelpTextTest(unittest.TestCase):
    def test_no_example_code_customers_could_try(self):
        for path in APP.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("۲۷۲۹", text, path.name)
            self.assertNotIn("همراه کلمه «بازدید»", text, path.name)

    def test_an_empty_search_suggests_widening(self):
        self.assertIn("اگه بودجه، متراژ یا منطقه رو کمی بازتر بگید", source("shahfile_ai.py"))


class SearchTest(unittest.TestCase):
    def test_an_exact_area_means_about_that_area(self):
        widen = load("shahfile_smart.py", {"widen_exact_area"}, {"math": math})["widen_exact_area"]
        self.assertEqual(widen({"area_min": 85, "area_max": 85, "district": "x"}),
                         {"area_min": 76, "area_max": 94, "district": "x"})
        self.assertEqual(widen({"area_min": 80, "area_max": 110}), {"area_min": 80, "area_max": 110})
        self.assertEqual(widen({"price_max_billion": 16}), {"price_max_billion": 16})
        smart = source("shahfile_smart.py")
        confirm = smart[smart.index("async def _offer_search_confirmation"):]
        self.assertTrue(confirm.split("\n", 2)[1].strip().startswith("filters = widen_exact_area(filters)"))

    def test_another_city_is_answered_before_any_search(self):
        smart = source("shahfile_smart.py")
        handler = smart[smart.index("async def handle_customer_text"):smart.index("async def handle_customer_contact")]
        check = handler.index("other_city = other_city_in(text)")
        for later in ("bare_budget_billion(text)", "ai_intent = await extract_ai_intent(", "escalate_customer_question"):
            self.assertLess(check, handler.index(later), later)
        self.assertIn("فعلاً فقط توی تبریز فایل داره", handler)


if __name__ == "__main__":
    unittest.main()
