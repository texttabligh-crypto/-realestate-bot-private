"""Phase 0 of the Bale bot: a start-up check that logs what works on Bale.

Nothing happens without BALE_BOT_TOKEN. With it the bot's getMe goes to
Bale's API; with BALE_TEST_CHAT_ID it also sends, edits and deletes test
posts in that test channel. A failing call is logged and the rest go on.
"""
import asyncio
import logging
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).parent / "app"

try:
    from PIL import Image  # noqa: F401
except ImportError:  # pragma: no cover - local runs without Pillow
    Image = None


class Message:
    def __init__(self, bot, message_id, photo=False):
        self.bot, self.message_id = bot, message_id
        self.photo = [types.SimpleNamespace(file_id=f"file{message_id}")] if photo else None

    async def edit_text(self, text):
        self.bot.calls.append("edit_text")

    async def edit_caption(self, caption):
        self.bot.calls.append("edit_caption")

    async def delete(self):
        self.bot.calls.append(f"delete:{self.message_id}")


class FakeBot:
    instances = []

    def __init__(self, token, base_url, base_file_url, request):
        self.token, self.base_url, self.base_file_url = token, base_url, base_file_url
        self.calls, self.username, self.id = [], "nimat_bale_bot", 42
        FakeBot.instances.append(self)

    async def initialize(self):
        self.calls.append("getMe")

    async def shutdown(self):
        self.calls.append("shutdown")

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append(("send_message", chat_id))
        return Message(self, 1)

    async def send_photo(self, chat_id, photo, caption):
        self.calls.append("send_photo")
        return Message(self, 2, photo=True)

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id))

    async def send_media_group(self, chat_id, media):
        raise RuntimeError("Bad Request: method not supported")

    async def send_document(self, chat_id, document, filename):
        self.calls.append("send_document")
        return Message(self, 5)


def load_module():
    sys.path.insert(0, str(APP))
    stubs = {
        "telegram": types.SimpleNamespace(
            Bot=FakeBot, InlineKeyboardButton=lambda *a, **k: None,
            InlineKeyboardMarkup=lambda rows: rows, InputMediaPhoto=lambda *a, **k: None),
        "telegram.request": types.SimpleNamespace(HTTPXRequest=lambda **k: k),
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("bale_check", None)
        import bale_check
    return bale_check


@unittest.skipIf(Image is None, "Pillow not installed")
class BaleCheckTest(unittest.TestCase):
    def setUp(self):
        FakeBot.instances = []
        self.check = load_module()

    def run_check(self, env):
        with mock.patch.dict(os.environ, env, clear=False), self.assertLogs(level="INFO") as logs:
            logging.info("start")
            asyncio.run(self.check.run_bale_check())
        return logs.output

    def test_nothing_without_a_token(self):
        with mock.patch.dict(os.environ, {"BALE_BOT_TOKEN": ""}):
            asyncio.run(self.check.run_bale_check())
        self.assertEqual(FakeBot.instances, [])

    def test_get_me_only_without_a_test_channel(self):
        logs = self.run_check({"BALE_BOT_TOKEN": "123:abc", "BALE_TEST_CHAT_ID": ""})
        bot = FakeBot.instances[0]
        self.assertEqual((bot.base_url, bot.base_file_url),
                         ("https://tapi.bale.ai/bot", "https://tapi.bale.ai/file/bot"))
        self.assertEqual(bot.calls, ["getMe", "shutdown"])
        self.assertTrue(any("BALE_CHECK getMe ok: username=nimat_bale_bot id=42" in line for line in logs))
        self.assertTrue(any("posting checks skipped" in line for line in logs))

    def test_posting_checks_go_on_after_a_failure_and_clean_up(self):
        logs = self.run_check({"BALE_BOT_TOKEN": "123:abc", "BALE_TEST_CHAT_ID": "-100555"})
        self.assertEqual(FakeBot.instances[0].calls, [
            "getMe", ("send_message", -100555), "edit_text", "send_photo", "edit_caption",
            ("get_file", "file2"), "send_document", "delete:1", "delete:2", "delete:5", "shutdown",
        ])
        self.assertTrue(any("BALE_CHECK sendMediaGroup failed: RuntimeError" in line for line in logs))
        self.assertTrue(any("BALE_CHECK done" in line for line in logs))

    def test_a_channel_username_is_kept(self):
        self.run_check({"BALE_BOT_TOKEN": "123:abc", "BALE_TEST_CHAT_ID": "@nimat_test"})
        self.assertIn(("send_message", "@nimat_test"), FakeBot.instances[0].calls)

    def test_wiring(self):
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        self.assertIn("application.bot_data['bale_check'] = asyncio.create_task(run_bale_check())", bot)
        workflow = (APP.parent / ".github" / "workflows" / "bale-connectivity.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: [self-hosted, Linux, X64, nimatmelk-production]", workflow)
        self.assertIn("https://tapi.bale.ai/botTEST/getMe", workflow)
        self.assertNotIn("secrets.", workflow)


if __name__ == "__main__":
    unittest.main()
