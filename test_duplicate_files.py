"""Settings › «🔁 فایل‌های تکراری» lists files published twice from one source post.

Each pair shows both post links, the copy to keep (✅, the one that tracks the
source post) and the copy to delete by hand (🗑). The database test runs
against a scratch PostgreSQL given in PGTEST_DSN and is skipped otherwise.
"""
import ast
import asyncio
import importlib.util
import os
import re
import unittest
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None

ROOT = Path(__file__).parent
APP = ROOT / "app"
DSN = os.getenv("PGTEST_DSN")


class Button:
    def __init__(self, text, url=None, callback_data=None):
        self.text, self.url, self.callback_data = text, url, callback_data


class Markup:
    def __init__(self, rows):
        self.rows = rows


def build_channel_post_url(username, message_id):
    return f"https://t.me/{username}/{message_id}" if username and message_id else None


def load_module():
    removed = ast.parse((APP / "removed_files.py").read_text(encoding="utf-8"))
    still = [n for n in removed.body if getattr(n, "name", None) in {"still_in_channel"}
             or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "POST_CHECK_CONCURRENCY")]
    tree = ast.parse((APP / "duplicate_files.py").read_text(encoding="utf-8"))
    nodes = still + [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    ns = {
        "asyncio": asyncio,
        "InlineKeyboardButton": Button,
        "InlineKeyboardMarkup": Markup,
        "build_channel_post_url": build_channel_post_url,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "duplicate_files.py", "exec"), ns)
    return ns


PAIRS = [("40200", 1502, "40100", 1501), ("50000", 1600, "50100", 1601)]


class DuplicateFilesViewTest(unittest.TestCase):
    def setUp(self):
        self.ns = load_module()

    def test_lists_pairs_with_keep_and_remove_links(self):
        text, markup = self.ns["duplicate_files_view"](PAIRS, "nimatmelk")
        self.assertIn("🔁 فایل‌های تکراری", text)
        self.assertIn("تعداد: 2", text)
        self.assertIn("✅ کد 40200: https://t.me/nimatmelk/1502\n🗑 کد 40100: https://t.me/nimatmelk/1501", text)
        buttons = [b for row in markup.rows[:-1] for b in row]
        self.assertEqual([(b.text, b.url) for b in buttons], [
            ("🗑 کد 40100", "https://t.me/nimatmelk/1501"),
            ("🗑 کد 50100", "https://t.me/nimatmelk/1601"),
        ])
        self.assertEqual([b.callback_data for b in markup.rows[-1]], ["duplicate_files", "settings"])

    def test_empty_and_long_lists(self):
        text, markup = self.ns["duplicate_files_view"]([], "nimatmelk")
        self.assertIn("فایل تکراری در کانال نیست", text)
        pairs = [(str(i), 1000 + i, str(i + 500), 2000 + i) for i in range(25)]
        text, _markup = self.ns["duplicate_files_view"](pairs, "nimatmelk")
        self.assertEqual(text.count("🗑 کد"), 20)
        self.assertIn("و 5 جفت دیگر", text)
        self.assertLess(len(text), 4000)

    def test_pair_drops_off_once_a_copy_is_deleted(self):
        answers = {1502: True, 1501: False, 1600: True, 1601: None}
        kept = asyncio.run(self.ns["pairs_still_in_channel"](
            PAIRS, "nimatmelk", lambda username, message_id: answers[message_id]))
        self.assertEqual(kept, [PAIRS[1]])


class PermissionWiringTest(unittest.TestCase):
    def test_access_is_listed_off_by_default_and_guards_the_button(self):
        source = (APP / "crm_panel.py").read_text(encoding="utf-8")
        self.assertIn('"duplicate_files_view": "🔁 فایل‌های تکراری"', source)
        off = re.search(r"RESTRICTED_DEFAULT_OFF_KEYS = \{([^}]*)\}", source).group(1)
        self.assertIn('"duplicate_files_view"', off)
        self.assertIn('(("duplicate_files",), "duplicate_files_view")', source)
        self.assertIn('if has_permission(session, "duplicate_files_view"):', source)
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        self.assertLess(bot.index("# CRM_ACCESS_GUARD_V1"), bot.index("if action == DUPLICATE_FILES_ACTION:"))


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class DuplicatePairsDatabaseTest(unittest.TestCase):
    def test_pairs_keep_the_copy_that_tracks_the_source_post(self):
        spec = importlib.util.spec_from_file_location("untracked", ROOT / "test_untracked_sources.py")
        untracked = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(untracked)
        connect = lambda: psycopg.connect(DSN)  # noqa: E731
        with connect() as conn:
            conn.execute(untracked.SCHEMA)
            conn.execute("INSERT INTO properties VALUES "
                         "(500, 'ACTIVE', 'SOURCE', 'a', -1002017017266, 9800, 1600, NULL, '50000'), "
                         "(501, 'ACTIVE', 'SOURCE', 'b', -1002017017266, 9800, 1601, NULL, '50100')")
            conn.execute("INSERT INTO property_sources (property_id, source_channel_id, "
                         "source_message_id, is_primary) VALUES (500, 3, 9800, TRUE), (501, 3, 9801, TRUE)")
        tree = ast.parse((APP / "duplicate_files.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if getattr(n, "name", None) == "duplicate_listing_pairs")
        ns = {"db_connect": connect}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "duplicate_files.py", "exec"), ns)
        self.assertEqual(ns["duplicate_listing_pairs"](), PAIRS)


if __name__ == "__main__":
    unittest.main()
