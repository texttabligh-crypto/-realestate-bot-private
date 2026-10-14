"""Full category reconciliation, quiet «not modified» errors and the typing indicator."""
import ast
import asyncio
import logging
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"

try:
    from telegram.error import BadRequest
except ImportError:  # python-telegram-bot is only installed in the bot image
    BadRequest = None


def load(filename, names, namespace):
    source = (APP / filename).read_text(encoding="utf-8")
    nodes = [n for n in ast.parse(source).body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class StopLoop(Exception):
    pass


class CategoryReconcileTest(unittest.TestCase):
    def test_runs_full_sync_under_lock_and_survives_errors(self):
        calls, sleeps = [], []
        results = [RuntimeError("telegram down"), {"synced": 4, "removed": 1}]

        async def sync(bot):
            calls.append(bot)
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise StopLoop

        ns = load("bot.py", {"category_reconcile_loop"}, {
            "asyncio": type("A", (), {"sleep": staticmethod(fake_sleep), "CancelledError": asyncio.CancelledError}),
            "logging": logging,
            "_category_sync_lock": asyncio.Lock(),
            "sync_active_category_posts": sync,
            "CATEGORY_RECONCILE_STARTUP_DELAY_SECONDS": 300,
            "CATEGORY_RECONCILE_INTERVAL_SECONDS": 21600,
        })
        app = type("App", (), {"bot": "bot"})()
        with self.assertLogs(level=logging.INFO) as logs, self.assertRaises(StopLoop):
            asyncio.run(ns["category_reconcile_loop"](app))
        self.assertEqual(calls, ["bot", "bot"])
        self.assertEqual(sleeps, [300, 21600, 21600])
        self.assertTrue(any("full reconcile failed" in line for line in logs.output))
        self.assertTrue(any("synced=4 removed=1" in line for line in logs.output))


@unittest.skipIf(BadRequest is None, "python-telegram-bot not installed")
class ErrorHandlerTest(unittest.TestCase):
    def run_handler(self, error):
        ns = load("bot.py", {"log_unhandled_update_error"}, {"BadRequest": BadRequest, "logging": logging})
        context = type("Ctx", (), {"error": error})()
        asyncio.run(ns["log_unhandled_update_error"](None, context))

    def test_not_modified_is_silent(self):
        with self.assertNoLogs(level=logging.ERROR):
            self.run_handler(BadRequest("Message is not modified: specified new message content ..."))

    def test_other_errors_are_logged(self):
        with self.assertLogs(level=logging.ERROR):
            self.run_handler(ValueError("real bug"))


class TypingIndicatorTest(unittest.TestCase):
    def test_typing_is_sent_without_blocking(self):
        sent = []

        class Bot:
            async def send_chat_action(self, chat_id, action):
                sent.append((chat_id, action))
                raise RuntimeError("network")  # must be swallowed

        ns = load("shahfile_smart.py", {"_show_typing"}, {
            "asyncio": asyncio, "ChatAction": type("CA", (), {"TYPING": "typing"}), "_typing_tasks": set(),
        })
        update = type("U", (), {"effective_chat": type("C", (), {"id": 42})()})()
        context = type("Ctx", (), {"bot": Bot()})()

        async def run():
            ns["_show_typing"](update, context)
            self.assertEqual(sent, [])  # returned before the API call ran
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(sent, [(42, "typing")])
        self.assertEqual(ns["_typing_tasks"], set())


if __name__ == "__main__":
    unittest.main()
