"""A listing deleted from its source must leave our channel even if the first cleanup missed.

27310 stayed in the channel for days: the watcher saw its source post was gone
and deactivated the source row, but the channel post was not removed in that
tick, and inactive source rows were never looked at again. Telegram refuses
to let bots delete channel posts older than 48 hours, so such a listing is
withdrawn from the bot and the owner gets a list to delete by hand.
"""
import ast
import asyncio
import logging
import sqlite3
import unittest
from pathlib import Path

BOT = Path(__file__).parent / "app" / "bot.py"


class Cursor:
    def __init__(self, db): self.db = db
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args): self.result = self.db.execute(sql.replace("%s", "?"), args)
    def fetchall(self): return self.result.fetchall()


class Connection(Cursor):
    def cursor(self): return Cursor(self.db)


def load(name, namespace):
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    names = {name} if isinstance(name, str) else set(name)
    nodes = [
        n for n in tree.body
        if getattr(n, "name", None) in names
        or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in names)
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace[name] if isinstance(name, str) else namespace


def build_channel_post_url(username, message_id):
    return f"https://t.me/{username}/{message_id}" if username and message_id else None


class TooOld(RuntimeError):
    pass


async def no_sleep(seconds):
    pass


async def to_thread(func, *args):
    return func(*args)


ASYNCIO = type("A", (), {"to_thread": staticmethod(to_thread), "sleep": staticmethod(no_sleep)})


class WatchRowsTest(unittest.TestCase):
    def test_leftover_listing_is_checked_again(self):
        db = sqlite3.connect(":memory:")
        db.create_function("BTRIM", 1, lambda value: None if value is None else value.strip())
        db.executescript("""
            CREATE TABLE channels(id INTEGER, channel_type TEXT, is_active BOOLEAN, username TEXT);
            CREATE TABLE properties(id INTEGER, status TEXT, published_message_id INTEGER,
                                    deleted_from_channel_at TEXT, source_kind TEXT);
            CREATE TABLE property_sources(id INTEGER, property_id INTEGER, source_channel_id INTEGER,
                                          source_message_id INTEGER, source_post_url TEXT,
                                          is_primary BOOLEAN, is_active BOOLEAN, last_synced_at TEXT);
            CREATE TABLE publish_queue(property_id INTEGER, queue_status TEXT);
            CREATE TABLE republish_queue(property_id INTEGER, queue_status TEXT);
            INSERT INTO channels VALUES (1, 'SOURCE', 1, 'royalmelkktbz');
            INSERT INTO properties VALUES
                (295, 'ACTIVE', 1282, NULL, 'SOURCE'),   -- source gone, still in channel
                (10, 'ACTIVE', 100, NULL, 'SOURCE'),     -- normal live listing
                (11, 'INACTIVE', 101, '2026-10-14', 'SOURCE'),  -- already removed
                (12, 'ACTIVE', 102, NULL, 'MANUAL'),     -- operator listing
                (13, 'ACTIVE', 103, NULL, 'SOURCE'),     -- reposted: new row is live
                (14, 'ACTIVE', NULL, NULL, 'SOURCE'),    -- never published
                (15, 'SOLD', 105, NULL, 'SOURCE');
            INSERT INTO property_sources VALUES
                (254, 295, 1, 7102, NULL, 1, 0, '2026-10-14 08:01'),
                (1, 10, 1, 5000, NULL, 1, 1, '2026-10-15 10:00'),
                (2, 11, 1, 5001, NULL, 1, 0, NULL),
                (3, 12, 1, 5002, NULL, 1, 0, NULL),
                (4, 13, 1, 5003, NULL, 1, 0, NULL),
                (5, 13, 1, 5004, NULL, 0, 1, '2026-10-15 09:00'),
                (6, 14, 1, 5005, NULL, 1, 0, NULL),
                (7, 15, 1, 5006, NULL, 1, 0, NULL);
        """)
        rows = load("get_source_delete_watch_rows", {"db_connect": lambda: Connection(db)})(25)
        self.assertEqual([(row[0], row[1]) for row in rows], [(254, 295), (5, 13), (1, 10)])


class WatchTickTest(unittest.TestCase):
    def run_tick(self, force_delete):
        calls = []

        async def retire(bot, property_id):
            calls.append(("retire", property_id))

        async def notify(bot, property_ids):
            calls.append(("notify", list(property_ids)))

        tick = load("run_source_deleted_posts_watch_tick", {
            "asyncio": ASYNCIO,
            "logging": logging,
            "RetryAfter": type("RetryAfter", (Exception,), {}),
            "ChannelPostTooOldToDelete": TooOld,
            "SOURCE_DELETE_WATCH_BATCH_SIZE": 25,
            "get_source_delete_watch_rows": lambda limit: [(254, 295, 7102, None, "royalmelkktbz")],
            "fetch_source_public_post": lambda username, message_id: (False, None),
            "touch_property_source_check": lambda row_id: calls.append(("touch", row_id)),
            "cancel_property_queues_for_deleted_source": lambda pid, row_id: calls.append(("cancel", pid)),
            "should_remove_published_property_after_source_delete": lambda pid: True,
            "force_delete_property": lambda bot, pid: force_delete(calls, pid),
            "retire_listing_kept_in_channel": retire,
            "notify_manual_channel_removals": notify,
        })
        logging.disable(logging.WARNING)
        try:
            asyncio.run(tick(type("App", (), {"bot": None})()))
        finally:
            logging.disable(logging.NOTSET)
        return calls

    def test_leftover_listing_is_removed_from_channel(self):
        async def force_delete(calls, property_id):
            calls.append(("delete", property_id))

        self.assertEqual(self.run_tick(force_delete), [("cancel", 295), ("delete", 295)])

    def test_post_too_old_to_delete_is_withdrawn_and_reported(self):
        async def force_delete(calls, property_id):
            calls.append(("delete", property_id))
            raise TooOld("too old")

        self.assertEqual(self.run_tick(force_delete), [
            ("cancel", 295), ("delete", 295), ("retire", 295), ("notify", [295]),
        ])


class Row(Cursor):
    def fetchone(self): return self.result.fetchone()


class RowConnection(Row):
    def cursor(self): return Row(self.db)


class ForceDeleteTest(unittest.TestCase):
    def test_stops_at_first_post_too_old_to_delete(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE properties(id INTEGER, unit_code TEXT, published_message_id INTEGER,
                                    status TEXT, deleted_from_channel_at TEXT);
            INSERT INTO properties VALUES (295, '27310', 1282, 'ACTIVE', NULL);
        """)
        attempts = []

        class Bot:
            async def delete_message(self, chat_id, message_id):
                attempts.append(message_id)
                raise Exception("Message can't be deleted")

        ns = load({"ChannelPostTooOldToDelete", "force_delete_property"}, {
            "asyncio": ASYNCIO,
            "logging": logging,
            "db_connect": lambda: RowConnection(db),
            "get_destination_channel": lambda: (-100222, "کانال", "nimatmelk"),
            "get_property_media": lambda pid: [(1, "PHOTO")] * 4,
            "deactivate_removed_property": lambda pid: self.fail("listing must stay until removed"),
        })
        with self.assertRaises(ns["ChannelPostTooOldToDelete"]):
            asyncio.run(ns["force_delete_property"](Bot(), 295))
        self.assertEqual(attempts, [1282])


class ManualRemovalMessageTest(unittest.TestCase):
    def test_lists_unit_codes_with_our_channel_links(self):
        build = load({"MANUAL_REMOVAL_LINES_PER_MESSAGE", "manual_channel_removal_messages"}, {
            "build_channel_post_url": build_channel_post_url,
        })["manual_channel_removal_messages"]
        rows = [("27310", 1282, None), ("3301", 1200, None)]
        (text,) = build(rows, "nimatmelk")
        self.assertIn("• کد 27310: https://t.me/nimatmelk/1282", text)
        self.assertIn("• کد 3301: https://t.me/nimatmelk/1200", text)
        self.assertIn("۴۸ ساعت", text)
        many = build([(str(i), 1000 + i, None) for i in range(65)], "nimatmelk")
        self.assertEqual(len(many), 3)
        self.assertEqual(sum(m.count("• کد") for m in many), 65)


if __name__ == "__main__":
    unittest.main()
