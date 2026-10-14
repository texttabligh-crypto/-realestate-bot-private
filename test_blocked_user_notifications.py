"""Users who blocked the bot must stop receiving retried notifications."""
import ast
import asyncio
import unittest
from pathlib import Path


class Forbidden(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class BlockedBot:
    def __init__(self):
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        raise Forbidden("Forbidden: bot was blocked by the user")


class App:
    def __init__(self):
        self.bot = BlockedBot()


def load(filename, function_name, namespace):
    source = Path(__file__).parent.joinpath("app", filename).read_text(encoding="utf-8")
    node = next(
        n for n in ast.parse(source).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == function_name
    )
    namespace.update({"Forbidden": Forbidden, "logging": __import__("logging"), "asyncio": asyncio})
    exec(compile(ast.Module(body=[node], type_ignores=[]), filename, "exec"), namespace)
    return namespace[function_name]


class BlockedUserTest(unittest.TestCase):
    def test_smart_search_subscription_is_disabled(self):
        declined, seen = [], []
        run = load("shahfile_ai.py", "run_search_watch_once", {
            "_active_subscriptions": lambda: [(122, 7630459796, {})],
            "_unseen_rows": lambda *a, **k: [(346,), (347,)],
            "get_subscription": lambda *a: {"is_active": True},
            "property_card_text": lambda *a, **k: "card",
            "_mark_seen": lambda *a, **k: seen.append(a),
            "_touch_scan": lambda *a: None,
            "decline_search_subscription": lambda *a: declined.append(a),
            "OPT_OUT_TEXT": "opt out",
        })
        app = App()
        asyncio.run(run(app))
        self.assertEqual(declined, [(122, 7630459796)])
        self.assertEqual(seen, [])
        self.assertEqual(app.bot.calls, 1)

    def test_campaign_visitor_is_disabled_without_retry(self):
        disabled, retried = [], []
        row = (5560, 3, 6077924044, 276, 0, "token")

        async def keyboard(*a):
            return None

        async def refresh(*a):
            return None

        run = load("customer_showcase.py", "run_campaign_watch_once", {
            "_active_memberships": lambda: [
                {"visitor_id": 41, "showcase_id": 3, "telegram_user_id": 6077924044}
            ],
            "_queue_new_matches": lambda *a: None,
            "_pending_deliveries": lambda: [row],
            "_get_visitor": lambda *a: None,
            "_new_match_text": lambda *a: "match",
            "_delivery_keyboard": keyboard,
            "_mark_delivery_sent": lambda *a: None,
            "_mark_delivery_failed": lambda *a: retried.append(a),
            "_disable_blocked_visitor": lambda *a: disabled.append(a[:2]),
            "_refresh_live_catalogs": refresh,
        })
        asyncio.run(run(App()))
        self.assertEqual(disabled, [(5560, 41)])
        self.assertEqual(retried, [])


if __name__ == "__main__":
    unittest.main()
