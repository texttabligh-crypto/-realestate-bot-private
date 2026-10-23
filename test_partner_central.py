"""Agency files go to ShahFile's own channel on their own.

The owner wanted every file an agency adds in the partner hub to reach his
channel too, not only the agency's. A file used to reach it only after the
agent pressed «ارسال برای کانال مرکزی» and the owner approved it, and even
then its photos carried the agency bot's file ids, which ShahFile's bot
cannot post. Now each sale file is queued for ShahFile's channel when it is
saved (older files join in turn), its photos are uploaded again by
ShahFile's bot, and deleting, selling or repricing it updates our post.
"""
import ast
import asyncio
import logging
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
HUB = ROOT / "partner_hub"
APP = ROOT / "app"
DSN = os.getenv("PGTEST_DSN")
TEST_DB = "partner_central_test"

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None


class TelegramError(Exception):
    pass


class InputMediaPhoto:
    def __init__(self, media, caption=None, parse_mode=None):
        self.media, self.caption, self.parse_mode = media, caption, parse_mode


def load_hub():
    sys.path.insert(0, str(HUB))
    stubs = {
        "telegram": types.SimpleNamespace(Bot=lambda token: ("bot", token), InputMediaPhoto=InputMediaPhoto),
        "telegram.constants": types.SimpleNamespace(ParseMode=types.SimpleNamespace(HTML="HTML")),
        "telegram.error": types.SimpleNamespace(TelegramError=TelegramError),
    }
    with mock.patch.dict(sys.modules, stubs), mock.patch.dict(os.environ, {"PARTNER_HUB_TOKEN": "hub-token"}):
        for name in ("central_channel", "channel_tools", "db", "formatter", "security"):
            sys.modules.pop(name, None)
        import central_channel
    return central_channel


class File:
    def __init__(self, data):
        self.data = data

    async def download_as_bytearray(self, **timeouts):
        return bytearray(self.data)


class Reader:
    """The bot that received the agency's photos."""

    def __init__(self, readable):
        self.readable = set(readable)

    async def get_file(self, file_id, **timeouts):
        if file_id not in self.readable:
            raise TelegramError("Wrong file identifier/http url specified")
        return File(f"photo:{file_id}".encode())


class Photo:
    def __init__(self, file_id):
        self.file_id, self.file_unique_id = file_id, "u-" + file_id


class Sent:
    def __init__(self, message_id, data):
        self.message_id = message_id
        self.photo = [Photo("small"), Photo("shahfile-" + data.decode())]


class MainBot:
    """ShahFile's bot: uploads into a chat and hands back its own file ids."""

    def __init__(self):
        self.calls = []

    async def send_photo(self, chat_id, photo, disable_notification=False, **timeouts):
        self.calls.append(("photo", chat_id, photo, disable_notification))
        return Sent(500, photo)

    async def send_media_group(self, chat_id, media, disable_notification=False, **timeouts):
        self.calls.append(("album", chat_id, [m.media for m in media], disable_notification))
        return [Sent(600 + index, m.media) for index, m in enumerate(media)]

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append(("delete", chat_id, list(message_ids)))


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class CentralQueueTest(unittest.TestCase):
    def setUp(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
        self.connect = lambda: psycopg.connect(DSN, dbname=TEST_DB, row_factory=dict_row)
        self.central = load_hub()
        db = self.db = self.central.db
        self.patch = mock.patch.object(db, "connect", self.connect)
        self.patch.start()
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE properties (
                    id BIGSERIAL PRIMARY KEY, property_type TEXT, district TEXT, neighborhood TEXT,
                    address_text TEXT, area_m2 NUMERIC, bedrooms INT, floor_text TEXT, total_floors INT,
                    units_per_floor INT, build_year INT, direction TEXT, parking_text TEXT, elevator_text TEXT,
                    storage_text TEXT, deed_text TEXT, installations_text TEXT, special_notes TEXT,
                    has_parking BOOL, has_elevator BOOL, has_storage BOOL, current_price_toman BIGINT,
                    current_price_billion NUMERIC, price_hashtag TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
                    source_kind TEXT, ingest_status TEXT, has_media BOOL NOT NULL DEFAULT FALSE, raw_text TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW());
                CREATE TABLE property_media (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, telegram_file_id TEXT,
                    telegram_file_unique_id TEXT, media_type TEXT, position INT, UNIQUE (property_id, position));
                CREATE TABLE property_attributes (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, attribute_key TEXT, attribute_label TEXT,
                    attribute_value TEXT, display_order INT, is_visible BOOL, UNIQUE (property_id, attribute_key));
                CREATE TABLE publish_queue (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, queue_status TEXT, priority INT,
                    queue_source TEXT, queued_by_user_id BIGINT);
            """)
            conn.execute((HUB / "migrations" / "001_partner_hub.sql").read_text(encoding="utf-8"))
            conn.commit()
        db.ensure_partner_v2_schema()
        db.ensure_partner_v3_schema()
        db.ensure_central_schema()
        db.ensure_central_schema()  # idempotent at every start
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO partner_agencies (id, legal_name, brand_name, slug, manager_name, manager_phone, status)
                VALUES (6, 'پاشا', 'پاشا ملک', 'pasha', 'مدیر', '09140000000', 'ACTIVE');
                INSERT INTO partner_members (id, agency_id, telegram_user_id, display_name, phone, role)
                VALUES (1, 6, 555, 'رضا', '09141111111', 'AGENCY_OWNER');
                INSERT INTO partner_submissions (id, tracking_code, agency_id, submitted_by_member_id,
                    property_type, deal_type, district, address_text, area_m2, bedrooms, total_floors,
                    parking_text, elevator_text, storage_text, deed_text, current_price_toman,
                    special_notes, owner_name, owner_phone, status, created_at)
                VALUES
                    (1, 'A6-1', 6, 1, 'APARTMENT', 'SALE', 'نصر', 'نصر خیابان یکم', 151, 3, 5,
                     'دارد', 'ندارد', 'دارد', 'تک برگ', 12500000000,
                     'تماس با پاشا ملک 09140000000', 'مالک', '09123456789', 'DRAFT', NOW()),
                    (2, 'A6-2', 6, 1, 'APARTMENT', 'RENT', 'نصر', 'x', 90, 2, NULL,
                     NULL, NULL, NULL, NULL, NULL, NULL, 'م', '0912', 'DRAFT', NOW()),
                    (3, 'A6-3', 6, 1, 'APARTMENT', 'SALE', 'منصور', 'منصور', 120, 2, NULL,
                     NULL, NULL, NULL, NULL, 8000000000, NULL, 'م', '0912', 'ACTIVE', NOW() - INTERVAL '5 days'),
                    (4, 'A6-4', 6, 1, 'APARTMENT', 'SALE', 'ولیعصر', 'ولیعصر', 100, 2, NULL,
                     NULL, NULL, NULL, NULL, 7000000000, NULL, 'م', '0912', 'SOLD', NOW() - INTERVAL '5 days');
                INSERT INTO partner_submission_media (submission_id, telegram_file_id, telegram_file_unique_id, position)
                VALUES (1, 'a1', 'ua1', 1), (1, 'a2', 'ua2', 2), (3, 'c1', 'uc1', 1);
            """)
            conn.commit()
        self.main = MainBot()
        self.bot_patch = mock.patch.object(self.central, "main_bot", lambda: self.main)
        self.bot_patch.start()
        self.readers_patch = mock.patch.object(self.central.ct, "agency_bot", lambda agency_id: None)
        self.readers_patch.start()
        self.hub_patch = mock.patch.object(self.central.ct, "hub_bot", lambda: Reader({"a1", "a2", "c1"}))
        self.hub_patch.start()

    def tearDown(self):
        for patch in (self.hub_patch, self.readers_patch, self.bot_patch, self.patch):
            patch.stop()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")

    def rows(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def run_due(self):
        return asyncio.run(self.central.run_due(146752428, [], logging.getLogger("test")))

    def test_a_new_sale_file_goes_to_shahfile_with_its_own_photos(self):
        self.assertTrue(self.central.queue(1))
        self.assertFalse(self.central.queue(1))  # already on its way
        self.assertFalse(self.central.queue(2))  # rentals stay in the agency channel
        self.assertEqual(self.run_due(), 1)
        # Uploaded silently by ShahFile's bot into the owner's chat, then deleted.
        self.assertEqual(self.main.calls, [
            ("album", 146752428, [b"photo:a1", b"photo:a2"], True),
            ("delete", 146752428, [600, 601]),
        ])
        prop = self.rows("SELECT * FROM properties")[0]
        self.assertEqual((prop["source_kind"], prop["status"], prop["has_media"]), ("PARTNER", "ACTIVE", True))
        self.assertEqual((prop["has_parking"], prop["has_elevator"], prop["has_storage"]), (True, False, True))
        self.assertEqual(prop["price_hashtag"], "#آپارتمان_12_13")
        # Private owner details and the agent's notes (the agency's phone) stay out.
        self.assertIsNone(prop["special_notes"])
        for private in ("09123456789", "09140000000", "مالک"):
            self.assertNotIn(private, prop["raw_text"])
        self.assertIn("A6-1", prop["raw_text"])
        media = self.rows("SELECT telegram_file_id, position FROM property_media ORDER BY position")
        self.assertEqual([(m["telegram_file_id"], m["position"]) for m in media],
                         [("shahfile-photo:a1", 1), ("shahfile-photo:a2", 2)])
        attrs = {r["attribute_key"]: r["attribute_value"] for r in self.rows("SELECT * FROM property_attributes")}
        self.assertEqual(attrs, {"total_floors": "5", "deed": "تک برگ"})
        queue = self.rows("SELECT queue_status, priority, queue_source FROM publish_queue")
        self.assertEqual([(q["queue_status"], q["priority"], q["queue_source"]) for q in queue], [("READY", 100, "PARTNER")])
        sub = self.rows("SELECT status, central_property_id FROM partner_submissions WHERE id=1")[0]
        self.assertEqual((sub["status"], sub["central_property_id"]), ("ACTIVE", prop["id"]))
        job = self.rows("SELECT status FROM partner_publication_jobs WHERE submission_id=1")[0]
        self.assertEqual(job["status"], "PUBLISHED")
        self.assertEqual(self.run_due(), 0)

    def test_older_files_join_in_turn(self):
        # 3 is an older sale file; 2 is a rental, 4 is sold, 1 is new (also picked up).
        self.assertEqual(self.central.queue_backlog(), 2)
        self.assertEqual(self.central.queue_backlog(), 0)
        self.assertEqual(self.run_due(), 2)
        queue = self.rows("""SELECT s.id, q.priority FROM publish_queue q
                             JOIN partner_submissions s ON s.central_property_id = q.property_id ORDER BY s.id""")
        self.assertEqual([(q["id"], q["priority"]) for q in queue], [(1, 100), (3, 0)])
        # One photo is uploaded as a photo, not an album.
        self.assertIn(("photo", 146752428, b"photo:c1", True), self.main.calls)

    def test_an_unreadable_photo_is_retried_later(self):
        with self.connect() as conn:
            conn.execute("UPDATE partner_submission_media SET telegram_file_id = 'gone' WHERE submission_id = 1 AND position = 2")
            conn.commit()
        self.central.queue(1)
        self.assertEqual(self.run_due(), 0)
        job = self.rows("SELECT status, attempts, last_error, scheduled_for > NOW() AS later FROM partner_publication_jobs")[0]
        self.assertEqual((job["status"], job["attempts"], job["later"]), ("PENDING", 1, True))
        self.assertIn("Wrong file identifier", job["last_error"])
        self.assertEqual(self.rows("SELECT * FROM properties"), [])
        self.assertEqual(self.run_due(), 0)  # not before its retry time

    def test_a_deleted_or_paused_file_waits_or_drops_out(self):
        self.central.queue(1)
        with self.connect() as conn:
            conn.execute("UPDATE partner_submissions SET status = 'INACTIVE' WHERE id = 1")
            conn.commit()
        self.assertEqual(self.run_due(), 0)  # paused: waits for the agent
        self.assertEqual(self.rows("SELECT status FROM partner_publication_jobs")[0]["status"], "PENDING")
        with self.connect() as conn:
            conn.execute("UPDATE partner_submissions SET status = 'DELETED' WHERE id = 1")
            conn.commit()
        self.run_due()
        self.assertEqual(self.rows("SELECT status FROM partner_publication_jobs")[0]["status"], "CANCELLED")

    def test_deleting_the_file_tells_shahfile(self):
        self.central.queue(1)
        self.run_due()
        submission = self.db.one("SELECT * FROM partner_submissions WHERE id=1")
        asyncio.run(self.central.ct.delete_submission(submission))
        events = self.rows("SELECT property_id, event_type FROM partner_central_events")
        self.assertEqual([(e["property_id"], e["event_type"]) for e in events],
                         [(submission["central_property_id"], "DELETED")])

    def test_without_shahfiles_token_nothing_is_sent(self):
        self.central.queue(1)
        with mock.patch.object(self.central, "main_bot", lambda: None):
            self.assertEqual(self.run_due(), 0)
        self.assertEqual(self.rows("SELECT status FROM partner_publication_jobs")[0]["status"], "PENDING")


def load_bot(names, namespace):
    tree = ast.parse((APP / "bot.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class TooOld(RuntimeError):
    pass


class Flood(Exception):
    pass


class ChannelSyncTest(unittest.TestCase):
    """ShahFile's bot follows the agency: delete, sold, new price, paused."""

    def setUp(self):
        self.calls = []

        def record(name, result=None, raises=None):
            async def call(*args, **kwargs):
                self.calls.append((name, args[1:] if args and args[0] == "bot" else args))
                if raises:
                    raise raises
                return result
            return call

        self.ns = load_bot(
            {"apply_partner_central_event", "run_partner_central_events_tick"},
            {
                "logging": logging,
                "ChannelPostTooOldToDelete": TooOld,
                "RetryAfter": Flood,
                "force_delete_property": record("force_delete"),
                "retire_listing_kept_in_channel": record("retire"),
                "notify_manual_channel_removals": record("notify"),
                "refresh_published_property_post": record("refresh"),
                "queue_targeted_category_sync": record("categories"),
                "sync_categories_for_price_change": record("price_categories"),
                "mark_property_sold": lambda pid: self.calls.append(("sold", (pid,))),
                "_get_property_current_price_toman": lambda pid: 9_000_000_000,
            },
        )
        self.record = record

    def apply(self, *args):
        asyncio.run(self.ns["apply_partner_central_event"]("bot", *args))
        return [name for name, _ in self.calls]

    def test_each_change(self):
        self.assertEqual(self.apply(7, "DELETED"), ["force_delete"])
        self.calls.clear()
        self.assertEqual(self.apply(7, "SOLD"), ["sold", "refresh", "categories"])
        self.calls.clear()
        self.assertEqual(self.apply(7, "PRICE", 10_000_000_000), ["refresh", "price_categories"])
        self.assertEqual(self.calls[-1][1], (7, 10_000_000_000, 9_000_000_000))
        self.calls.clear()
        self.assertEqual(self.apply(7, "STATUS"), ["categories"])

    def test_an_old_post_goes_on_the_manual_removal_list(self):
        self.ns["force_delete_property"] = self.record("force_delete", raises=TooOld())
        self.assertEqual(self.apply(7, "DELETED"), ["force_delete", "retire", "notify"])
        self.assertEqual(self.calls[-1][1], ([7],))

    def test_the_tick_marks_events_and_keeps_failures_for_a_retry(self):
        finished = []
        self.ns["pending_partner_central_events"] = lambda: [(1, 7, "SOLD", None), (2, 8, "DELETED", None)]
        self.ns["finish_partner_central_event"] = lambda event_id, error=None: finished.append((event_id, error))
        self.ns["force_delete_property"] = self.record("force_delete", raises=RuntimeError("حذف تلگرامی کامل انجام نشد"))
        app = types.SimpleNamespace(bot="bot")
        with self.assertLogs(level="ERROR"):
            asyncio.run(self.ns["run_partner_central_events_tick"](app))
        self.assertEqual(finished, [(1, None), (2, "حذف تلگرامی کامل انجام نشد")])


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class EventsTableTest(unittest.TestCase):
    def setUp(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
        connect = lambda: psycopg.connect(DSN, dbname=TEST_DB)  # noqa: E731
        self.ns = load_bot(
            {"pending_partner_central_events", "finish_partner_central_event"},
            {"db_connect": connect, "psycopg": psycopg, "PARTNER_EVENT_MAX_ATTEMPTS": 5},
        )
        self.connect = connect

    def tearDown(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")

    def test_events(self):
        # Before the partner hub creates the table there is nothing to do.
        with self.connect() as conn:
            conn.execute("CREATE TABLE properties (id BIGINT PRIMARY KEY, source_kind TEXT)")
            conn.commit()
        self.assertEqual(self.ns["pending_partner_central_events"](), [])
        hub = load_hub()
        with mock.patch.object(hub.db, "connect", lambda: psycopg.connect(DSN, dbname=TEST_DB, row_factory=dict_row)):
            hub.db.ensure_central_schema()
            with self.connect() as conn:
                conn.execute("INSERT INTO properties VALUES (7, 'PARTNER'), (8, 'SOURCE')")
                conn.commit()
            hub.db.central_event(7, "PRICE", 9_500_000_000)
            hub.db.central_event(8, "DELETED")  # never touches a listing from a source channel
            hub.db.central_event(7, "SOLD")
        pending = self.ns["pending_partner_central_events"]()
        self.assertEqual([row[1:] for row in pending], [(7, "PRICE", 9_500_000_000), (7, "SOLD", None)])
        self.ns["finish_partner_central_event"](pending[0][0])
        for _ in range(4):
            self.ns["finish_partner_central_event"](pending[1][0], "timeout")
        self.assertEqual([row[0] for row in self.ns["pending_partner_central_events"]()], [pending[1][0]])
        self.ns["finish_partner_central_event"](pending[1][0], "timeout")  # fifth failure: give up
        self.assertEqual(self.ns["pending_partner_central_events"](), [])


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class VisitSourceTest(unittest.TestCase):
    """A visit request for an agency file says which agency to call."""

    def setUp(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
        connect = lambda: psycopg.connect(DSN, dbname=TEST_DB)  # noqa: E731
        with connect() as conn:
            conn.execute("""
                CREATE TABLE properties (id BIGINT PRIMARY KEY, raw_text TEXT, source_kind TEXT);
                CREATE TABLE channels (id BIGINT PRIMARY KEY, title TEXT, username TEXT, telegram_channel_id BIGINT);
                CREATE TABLE property_sources (
                    id BIGSERIAL PRIMARY KEY, property_id BIGINT, source_unit_code TEXT, source_post_url TEXT,
                    source_message_id BIGINT, source_raw_text TEXT, source_channel_id BIGINT,
                    is_active BOOL, is_primary BOOL);
            """)
            conn.execute((HUB / "migrations" / "001_partner_hub.sql").read_text(encoding="utf-8"))
            conn.execute("""
                INSERT INTO properties VALUES (7, 'ورودی همکار | کد A6-1', 'PARTNER');
                INSERT INTO partner_agencies (id, legal_name, brand_name, slug, manager_name, manager_phone, status)
                VALUES (6, 'پاشا', 'پاشا ملک', 'pasha', 'مدیر', '09140000000', 'ACTIVE');
                INSERT INTO partner_members (id, agency_id, telegram_user_id, display_name, phone, role)
                VALUES (1, 6, 555, 'رضا', '09141111111', 'AGENCY_OWNER');
                INSERT INTO partner_channel_connections (id, agency_id, telegram_channel_id, channel_username, is_default)
                VALUES (9, 6, -100777, '@pashamelktbz', TRUE);
                INSERT INTO partner_submissions (id, tracking_code, agency_id, submitted_by_member_id,
                    property_type, district, address_text, area_m2, status, central_property_id)
                VALUES (1, 'A6-1', 6, 1, 'APARTMENT', 'نصر', 'x', 151, 'ACTIVE', 7);
                INSERT INTO partner_publication_jobs (submission_id, target_type, channel_connection_id, status,
                    telegram_message_id, published_at)
                VALUES (1, 'AGENCY_CHANNEL', 9, 'PUBLISHED', 12, NOW());
            """)
            conn.commit()
        sys.path.insert(0, str(APP))
        stubs = {}
        try:
            import telegram  # noqa: F401
        except ImportError:
            stubs["telegram"] = types.SimpleNamespace(InlineKeyboardButton=object, InlineKeyboardMarkup=object)
        with mock.patch.dict(sys.modules, stubs):
            sys.modules.pop("visit_leads", None)
            import visit_leads
        self.visit_leads = visit_leads
        self.patch = mock.patch.object(visit_leads, "_db_connect", connect)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")

    def test_the_agency_and_its_post(self):
        source = self.visit_leads.get_operator_source_context(7)
        self.assertEqual(source["kind"], "PARTNER")
        self.assertEqual(source["source_name"], "همکار «پاشا ملک» (رضا – 09141111111)")
        self.assertEqual(source["source_unit_code"], "A6-1")
        self.assertEqual(source["direct_post_url"], "https://t.me/pashamelktbz/12")
        self.assertTrue(source["source_is_active"])


class WiringTest(unittest.TestCase):
    def test_wiring(self):
        app = (HUB / "app.py").read_text(encoding="utf-8")
        self.assertIn("    db.ensure_central_schema()\n", app)
        self.assertIn("if central.queue(sid):", app)
        self.assertIn("await central.run_due(OWNER_ID,[app.bot],log)", app)
        self.assertNotIn('B("🌐 ارسال برای کانال مرکزی"', app)
        self.assertNotIn("pid=db.push_to_central(sid,OWNER_ID)", app)
        self.assertIn('db.central_event(submission["central_property_id"],"SOLD")', app)
        self.assertIn('db.central_event(submission["central_property_id"],"PRICE",submission.get("current_price_toman"))', app)
        self.assertIn('db.central_event(submission["central_property_id"],"STATUS")', app)
        self.assertIn('db.central_event(row["central_property_id"],"STATUS")', app)
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.create_task(partner_central_events_loop(application))", bot)


if __name__ == "__main__":
    unittest.main()
