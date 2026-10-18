"""A source price edit reaches our channel even when Telegram never told the bot.

The Mirdamad listing went from 9.5 to 10 billion in its source channel, where
the bot is not an admin, so no edit update came and our channel kept 9.5.
The source watcher already fetches each post's public page; it now reads the
post's text there and applies a new price or «فروش رفت» like a real edit.
"""
import ast
import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
BOT = APP / "bot.py"
sys.path.insert(0, str(APP))

from property_parser import parse_property, split_listing_blocks  # noqa: E402
from source_public_post import public_post_text  # noqa: E402

PAGE = """<html><body>
<div class="tgme_widget_message js-widget_message" data-post="Amlake_Hamidi/912">
<div class="tgme_widget_message_bubble">
<a class="tgme_widget_message_reply" href="https://t.me/Amlake_Hamidi/1">
<div class="tgme_widget_message_author">Amlake_Hamidi</div>
<div class="tgme_widget_message_text js-message_reply_text" dir="auto">قیمت : 7 میلیارد</div></a>
<div class="tgme_widget_message_grouped_wrap"><a class="tgme_widget_message_photo_wrap"></a></div>
<div class="tgme_widget_message_text js-message_text" dir="auto"><a href="?q=%23m">#میرداماد</a><br/><br/>
<i class="emoji" style="background-image:url('//telegram.org/img/emoji/40/F09F938D.png')"><b>📍</b></i>میرداماد مجتمع زمرد<br/><br/>
<i class="emoji"><b>〽️</b></i>واحد 78 متری 2 خواب دوکله<br/>
<i class="emoji"><b>〽️</b></i>ساخت 1384 ؛ غرق در نور<br/><br/>
<i class="emoji"><b>💵</b></i>قیمت : 10 میلیارد<br/><br/>
<a href="tel:09142362376">09142362376</a></div>
<div class="tgme_widget_message_footer"><span class="tgme_widget_message_meta">edited 16:23</span></div>
</div></div></body></html>"""


def load(names, namespace):
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


async def to_thread(func, *args):
    return func(*args)


class PublicTextTest(unittest.TestCase):
    def test_reads_the_posts_own_text(self):
        text = public_post_text(PAGE)
        self.assertEqual(text.splitlines()[0], "#میرداماد")
        self.assertIn("📍میرداماد مجتمع زمرد", text)
        self.assertIn("💵قیمت : 10 میلیارد", text)
        self.assertNotIn("7 میلیارد", text)  # the replied-to post
        self.assertNotIn("edited", text)
        self.assertEqual(parse_property(text)["current_price_toman"], 10_000_000_000)

    def test_no_text(self):
        self.assertIsNone(public_post_text('<div class="tgme_widget_message"></div>'))
        self.assertIsNone(public_post_text(""))


class SyncTest(unittest.TestCase):
    def sync(self, row, text=None, stored_code=None):
        synced = []

        async def sync_edited_source_post(post, context):
            synced.append((post.chat.id, post.message_id, post.text, context.bot))
            return True

        ns = load({"sync_source_public_edit"}, {
            "asyncio": types.SimpleNamespace(to_thread=to_thread),
            "types": types,
            "logging": logging,
            "get_source_sync_row": lambda row_id: row,
            "split_listing_blocks": split_listing_blocks,
            "get_source_unit_code_for_message": lambda channel, message: stored_code,
            "source_text_is_sold": lambda text: "فروش رفت" in text,
            "parse_property": parse_property,
            "sync_edited_source_post": sync_edited_source_post,
        })
        logging.disable(logging.INFO)
        try:
            result = asyncio.run(ns["sync_source_public_edit"]("BOT", 77, text or public_post_text(PAGE)))
        finally:
            logging.disable(logging.NOTSET)
        return result, synced

    def test_a_new_price_is_applied_like_an_edit(self):
        result, synced = self.sync((-1001234, 912, 9_500_000_000, True, "ACTIVE"))
        self.assertTrue(result)
        self.assertEqual(synced[0][:2], (-1001234, 912))
        self.assertIn("قیمت : 10 میلیارد", synced[0][2])
        self.assertEqual(synced[0][3], "BOT")

    def test_the_same_price_changes_nothing(self):
        self.assertEqual(self.sync((-1001234, 912, 10_000_000_000, True, "ACTIVE")), (False, []))

    def test_an_inactive_or_unknown_source_changes_nothing(self):
        self.assertEqual(self.sync((-1001234, 912, 9_500_000_000, False, "ACTIVE")), (False, []))
        self.assertEqual(self.sync(None), (False, []))

    def test_sold_is_applied_once(self):
        sold = public_post_text(PAGE) + "\nفروش رفت"
        result, synced = self.sync((-1001234, 912, 10_000_000_000, True, "ACTIVE"), sold)
        self.assertTrue(result)
        self.assertEqual(self.sync((-1001234, 912, 10_000_000_000, True, "SOLD"), sold), (False, []))

    def test_no_price_on_the_page_changes_nothing(self):
        self.assertEqual(self.sync((-1001234, 912, 9_500_000_000, True, "ACTIVE"), "سلام"), (False, []))


class WatchTickTest(unittest.TestCase):
    def test_the_watcher_applies_a_few_edits_a_tick(self):
        calls = []

        async def sync_source_public_edit(bot, row_id, text):
            calls.append(row_id)
            return True

        ns = load({"run_source_deleted_posts_watch_tick"}, {
            "asyncio": types.SimpleNamespace(to_thread=to_thread),
            "logging": logging,
            "SOURCE_DELETE_WATCH_BATCH_SIZE": 40,
            "SOURCE_PUBLIC_EDIT_SYNC_LIMIT": 3,
            "get_source_delete_watch_rows": lambda limit: [(i, 100 + i, 900 + i, None, "src") for i in range(5)],
            "fetch_source_public_post": lambda username, message_id: (True, "💵قیمت : 10 میلیارد"),
            "touch_property_source_check": lambda row_id: None,
            "sync_source_public_edit": sync_source_public_edit,
            "notify_manual_channel_removals": None,
        })
        logging.disable(logging.INFO)
        try:
            asyncio.run(ns["run_source_deleted_posts_watch_tick"](types.SimpleNamespace(bot="BOT")))
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(calls, [0, 1, 2])

    def test_wiring(self):
        source = BOT.read_text(encoding="utf-8")
        self.assertIn("SOURCE_DELETE_WATCH_BATCH_SIZE = 40\n", source)
        self.assertIn("        return True, public_post_text(page)\n", source)
        # Other callers keep the old yes/no answer.
        self.assertIn("    return fetch_source_public_post(username, message_id)[0]\n", source)


if __name__ == "__main__":
    unittest.main()
