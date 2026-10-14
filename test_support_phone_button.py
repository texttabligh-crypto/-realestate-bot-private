"""The support-ticket phone button shows the number the customer shared."""
import ast
import asyncio
import unittest
from pathlib import Path


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class Query:
    def __init__(self, data):
        self.data = data
        self.message = Message()


def run_button(data, ticket):
    source = Path(__file__).parent.joinpath("app", "crm_campaign_hub.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body if getattr(n, "name", None) == "handle_button")
    namespace = {"asyncio": asyncio, "_support_ticket": lambda ticket_id: ticket}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "crm_campaign_hub.py", "exec"), namespace)
    query = Query(data)
    update = type("Update", (), {"callback_query": query})()
    handled = asyncio.run(namespace["handle_button"](update, None, None))
    return handled, query.message.replies


class SupportPhoneButtonTest(unittest.TestCase):
    def test_shows_shared_phone(self):
        ticket = (23, 2325, 111, "۳۰۴۱۱کد واحد", "OPEN", None, "Hanie Ghaffari", "09121234567")
        handled, replies = run_button("crm_support_phone_23", ticket)
        self.assertTrue(handled)
        self.assertEqual(replies, ["📞 شماره موبایل Hanie Ghaffari (سؤال #23):\n09121234567"])

    def test_explains_missing_phone(self):
        ticket = (24, None, 222, "سلام", "OPEN", None, "بدون نام", "بدون شماره")
        handled, replies = run_button("crm_support_phone_24", ticket)
        self.assertTrue(handled)
        self.assertIn("هنوز شماره موبایلش را ثبت نکرده", replies[0])


if __name__ == "__main__":
    unittest.main()
