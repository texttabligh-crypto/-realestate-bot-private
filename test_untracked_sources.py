"""A forwarded copy of a source post must not leave the original listing untracked.

26527 was made when a Fouladian post the bot already had (27027) was forwarded
into the input channel; saving its source row moved that row off 27027, so the
bot no longer noticed when the source edited or deleted the post.

The database tests run against a scratch PostgreSQL given in PGTEST_DSN
(for example "host=/tmp/pgtest port=55432 user=postgres dbname=t2") and are
skipped otherwise.
"""
import ast
import os
import unittest
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None

APP = Path(__file__).parent / "app"
DSN = os.getenv("PGTEST_DSN")


def load(path, names, namespace):
    tree = ast.parse((APP / path).read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), namespace)
    return namespace


SCHEMA = """
DROP TABLE IF EXISTS property_media, property_sources, properties, channels CASCADE;
CREATE TABLE channels (id BIGINT PRIMARY KEY, telegram_channel_id BIGINT, channel_type TEXT);
CREATE TABLE properties (
    id BIGINT PRIMARY KEY, status TEXT, source_kind TEXT, raw_text TEXT,
    input_channel_id BIGINT, input_message_id BIGINT,
    published_message_id BIGINT, deleted_from_channel_at TIMESTAMPTZ);
CREATE TABLE property_media (property_id BIGINT, position INTEGER);
CREATE TABLE property_sources (
    id BIGSERIAL PRIMARY KEY, property_id BIGINT, source_channel_id BIGINT,
    source_message_id BIGINT, source_unit_code TEXT, source_price_toman BIGINT,
    source_raw_text TEXT, source_post_url TEXT,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE, is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_synced_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
    UNIQUE (source_channel_id, source_message_id));
INSERT INTO channels VALUES (1, -1003466235066, 'SOURCE'), (2, -1004370416481, 'INPUT'),
                            (3, -1002017017266, 'SOURCE');
INSERT INTO properties VALUES
    (287, 'ACTIVE', 'SOURCE', 'zafaranieh', -1003466235066, 740, 1266, NULL),
    (289, 'INACTIVE', 'INPUT', 'zafaranieh', -1004370416481, 1217, 1235, NOW()),
    (292, 'ACTIVE', 'SOURCE', 'sarkhab', -1002017017266, 9507, 1286, NULL),
    (400, 'ACTIVE', 'SOURCE', 'no stale row', -1002017017266, 9600, 1500, NULL),
    (401, 'ACTIVE', 'SOURCE', 'live duplicate', -1002017017266, 9700, 1501, NULL),
    (402, 'ACTIVE', 'SOURCE', 'holds 9700', -1002017017266, 9701, 1502, NULL),
    (404, 'ACTIVE', 'INPUT', 'origin unknown', -1004370416481, 1300, 1504, NULL);
INSERT INTO property_media SELECT 287, g FROM generate_series(740, 747) g;
INSERT INTO property_sources (id, property_id, source_channel_id, source_message_id,
                              source_unit_code, is_primary, is_active, last_synced_at) VALUES
    (244, 289, 1, 747, '123456', TRUE, FALSE, NOW()),
    (251, 292, 3, 9507, NULL, TRUE, TRUE, NOW()),
    (500, 402, 3, 9700, NULL, TRUE, TRUE, NOW());
"""


class TakeoverGuardSourceTest(unittest.TestCase):
    def test_conflict_keeps_a_live_holder(self):
        source = (APP / "ingest.py").read_text(encoding="utf-8")
        self.assertIn("THEN property_sources.property_id", source)
        self.assertNotIn("DO UPDATE SET\n                    property_id = EXCLUDED.property_id,", source)

    def test_forwarded_copy_is_skipped_before_a_listing_is_made(self):
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        check = bot.index("already_listed = live_property_for_source_post(")
        create = bot.index("property_id = create_raw_property(", check)
        self.assertLess(check, create)


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class UntrackedSourceDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute(SCHEMA)

    def rows(self):
        with self.connect() as conn:
            return {
                row[0]: row[1:]
                for row in conn.execute(
                    "SELECT id, property_id, source_message_id, is_active, "
                    "last_synced_at IS NULL FROM property_sources")
            }

    def test_reconnects_lost_rows_and_leaves_live_duplicates(self):
        ns = load("bot.py", {"reconnect_untracked_source_listings"}, {"db_connect": self.connect})
        restored, created, skipped = ns["reconnect_untracked_source_listings"]()
        self.assertEqual((restored, created, skipped), ([287], [400], [401]))
        rows = self.rows()
        self.assertEqual(rows[244], (287, 747, True, True))
        self.assertEqual(rows[500][0], 402)
        self.assertEqual(ns["reconnect_untracked_source_listings"](), ([], [], [401]))

    def test_forwarded_copy_does_not_take_the_row(self):
        ns = load("ingest.py", {
            "get_channel_db_id", "_save_property_source_raw", "live_property_for_source_post",
        }, {"db_connect": self.connect})
        with self.connect() as conn:
            conn.execute("UPDATE property_sources SET property_id = 287, is_active = TRUE WHERE id = 244")
            conn.execute("INSERT INTO properties VALUES (600, 'ACTIVE', 'INPUT', 'copy', "
                         "-1004370416481, 1400, NULL, NULL)")
        self.assertEqual(ns["live_property_for_source_post"](-1003466235066, 747), 287)
        self.assertEqual(ns["live_property_for_source_post"](-1003466235066, 740), 287)
        self.assertIsNone(ns["live_property_for_source_post"](-1003466235066, 999))
        ns["_save_property_source_raw"](600, -1003466235066, 747, None, "copy")
        self.assertEqual(self.rows()[244][0], 287)
        # No other live listing can take it either ...
        ns["_save_property_source_raw"](292, -1003466235066, 747, None, "x")
        self.assertEqual(self.rows()[244][0], 287)
        with self.connect() as conn:
            conn.execute("UPDATE properties SET status = 'INACTIVE' WHERE id = 287")
        # ... but once its holder is gone the row may follow the post.
        ns["_save_property_source_raw"](600, -1003466235066, 747, None, "copy")
        self.assertEqual(self.rows()[244][0], 600)


if __name__ == "__main__":
    unittest.main()
