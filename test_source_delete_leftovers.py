"""A listing deleted from its source must leave our channel even if the first cleanup missed.

27310 stayed in the channel for days: the watcher saw its source post was gone
and deactivated the source row, but the channel post was not removed in that
tick, and inactive source rows were never looked at again.
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
    node = next(n for n in tree.body if getattr(n, "name", None) == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace[name]


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
    def test_leftover_listing_is_removed_from_channel(self):
        calls = []

        async def force_delete(bot, property_id):
            calls.append(("delete", property_id))

        async def to_thread(func, *args):
            return func(*args)

        async def no_sleep(seconds):
            pass

        asyncio_stub = type("A", (), {"to_thread": staticmethod(to_thread), "sleep": staticmethod(no_sleep)})
        tick = load("run_source_deleted_posts_watch_tick", {
            "asyncio": asyncio_stub,
            "logging": logging,
            "RetryAfter": type("RetryAfter", (Exception,), {}),
            "SOURCE_DELETE_WATCH_BATCH_SIZE": 25,
            "get_source_delete_watch_rows": lambda limit: [(254, 295, 7102, None, "royalmelkktbz")],
            "source_public_post_exists": lambda username, message_id: False,
            "touch_property_source_check": lambda row_id: calls.append(("touch", row_id)),
            "cancel_property_queues_for_deleted_source": lambda pid, row_id: calls.append(("cancel", pid)),
            "should_remove_published_property_after_source_delete": lambda pid: True,
            "force_delete_property": force_delete,
        })
        app = type("App", (), {"bot": None})()
        logging.disable(logging.WARNING)
        try:
            asyncio.run(tick(app))
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(calls, [("cancel", 295), ("delete", 295)])


if __name__ == "__main__":
    unittest.main()
