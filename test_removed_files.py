"""Settings › «🧹 فایل‌های حذف‌شده» lists source-deleted posts still in the channel.

Telegram does not let the bot delete channel posts older than 48 hours, so
an owner or an admin given the access removes them by hand from this list.
"""
import ast
import asyncio
import re
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"


class Button:
    def __init__(self, text, url=None, callback_data=None):
        self.text, self.url, self.callback_data = text, url, callback_data


class Markup:
    def __init__(self, rows):
        self.rows = rows


def build_channel_post_url(username, message_id):
    return f"https://t.me/{username}/{message_id}" if username and message_id else None


def load_module():
    tree = ast.parse((APP / "removed_files.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    ns = {
        "asyncio": asyncio,
        "InlineKeyboardButton": Button,
        "InlineKeyboardMarkup": Markup,
        "build_channel_post_url": build_channel_post_url,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "removed_files.py", "exec"), ns)
    return ns


ROWS = [(295, "27310", 1282), (233, "3301", 1200), (277, "28890", 1386)]


class RemovedFilesViewTest(unittest.TestCase):
    def setUp(self):
        self.ns = load_module()

    def test_lists_posts_with_link_buttons(self):
        text, markup = self.ns["removed_files_view"](ROWS, "nimatmelk")
        self.assertIn("🧹 فایل‌های حذف‌شده", text)
        self.assertIn("تعداد: 3", text)
        self.assertIn("• 🗑 کد 27310: https://t.me/nimatmelk/1282", text)
        buttons = [b for row in markup.rows[:-1] for b in row]
        self.assertEqual([(b.text, b.url) for b in buttons], [
            ("🗑 کد 27310", "https://t.me/nimatmelk/1282"),
            ("🗑 کد 3301", "https://t.me/nimatmelk/1200"),
            ("🗑 کد 28890", "https://t.me/nimatmelk/1386"),
        ])
        self.assertEqual([b.callback_data for b in markup.rows[-1]], ["removed_files", "settings"])

    def test_empty_list(self):
        text, markup = self.ns["removed_files_view"]([], "nimatmelk")
        self.assertIn("فایلی برای حذف دستی نمانده", text)
        self.assertEqual(len(markup.rows), 1)

    def test_long_list_is_capped(self):
        rows = [(i, str(i), 1000 + i) for i in range(45)]
        text, markup = self.ns["removed_files_view"](rows, "nimatmelk")
        self.assertEqual(text.count("• 🗑"), 30)
        self.assertIn("و 15 فایل دیگر", text)
        self.assertLess(len(text), 4000)

    def test_posts_already_deleted_drop_off(self):
        answers = {1282: True, 1200: False, 1386: None}
        kept, gone = asyncio.run(self.ns["still_in_channel"](
            ROWS, "nimatmelk", lambda username, message_id: answers[message_id]))
        self.assertEqual([row[0] for row in kept], [295, 277])
        self.assertEqual(gone, [233])


class PermissionWiringTest(unittest.TestCase):
    def test_access_is_listed_off_by_default_and_guards_the_button(self):
        source = (APP / "crm_panel.py").read_text(encoding="utf-8")
        self.assertIn('"removed_files_view": "🧹 فایل‌های حذف‌شده"', source)
        off = re.search(r"RESTRICTED_DEFAULT_OFF_KEYS = \{([^}]*)\}", source).group(1)
        self.assertIn('"removed_files_view"', off)
        self.assertIn('(("removed_files",), "removed_files_view")', source)
        self.assertIn('if has_permission(session, "removed_files_view"):', source)
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        guard = bot.index("# CRM_ACCESS_GUARD_V1")
        route = bot.index("if action == REMOVED_FILES_ACTION:")
        self.assertLess(guard, route)


if __name__ == "__main__":
    unittest.main()
