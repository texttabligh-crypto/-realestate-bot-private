"""The agency bot posts to its own channel, deletes files and keeps a schedule.

Publishing failed with «wrong file identifier/http url specified»: photos
received by the hub bot were sent by the agency's own bot, and Telegram file
ids belong to one bot. Photos now go through a bot that can read them and
are uploaded again. Agencies can delete files, send files never posted to
their channel, and set daily posting times.
"""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

HUB = Path(__file__).parent / "partner_hub"
DSN = os.getenv("PGTEST_DSN")
TEHRAN = ZoneInfo("Asia/Tehran")

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


def load_tools():
    sys.path.insert(0, str(HUB))
    stubs = {
        "telegram": types.SimpleNamespace(Bot=lambda token: ("hub", token), InputMediaPhoto=InputMediaPhoto),
        "telegram.constants": types.SimpleNamespace(ParseMode=types.SimpleNamespace(HTML="HTML")),
        "telegram.error": types.SimpleNamespace(TelegramError=TelegramError),
    }
    with mock.patch.dict(sys.modules, stubs), mock.patch.dict(os.environ, {"PARTNER_HUB_TOKEN": "hub-token"}):
        for name in ("channel_tools", "db", "formatter", "security"):
            sys.modules.pop(name, None)
        import channel_tools
    return channel_tools


class File:
    def __init__(self, data):
        self.data = data

    async def download_as_bytearray(self, **timeouts):
        return bytearray(self.data)


class Message:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self, name, readable=(), fail_delete=False):
        self.name, self.readable, self.fail_delete, self.calls = name, set(readable), fail_delete, []

    async def get_file(self, file_id, **timeouts):
        if file_id not in self.readable:
            raise TelegramError("Wrong file identifier/http url specified")
        return File(f"{self.name}:{file_id}".encode())

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append(("message", chat_id, text))
        return Message(100)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **timeouts):
        self.calls.append(("photo", chat_id, photo, caption))
        return Message(200)

    async def send_media_group(self, chat_id, media, **timeouts):
        self.calls.append(("album", chat_id, [(m.media, m.caption) for m in media]))
        return [Message(300 + index) for index in range(len(media))]

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append(("delete", chat_id, list(message_ids)))
        if self.fail_delete:
            raise TelegramError("Message can't be deleted")

    async def edit_message_caption(self, chat_id, message_id, caption, parse_mode=None):
        self.calls.append(("edit_caption", chat_id, message_id, caption))

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.calls.append(("edit_text", chat_id, message_id, text))


class PublishingTest(unittest.TestCase):
    def setUp(self):
        self.ct = load_tools()

    def test_a_photo_is_read_through_the_bot_that_received_it(self):
        agency, hub = FakeBot("agency"), FakeBot("hub", readable={"f1"})
        data = asyncio.run(self.ct.photo_bytes([agency, None, hub], "f1"))
        self.assertEqual(data, b"hub:f1")
        with self.assertRaises(TelegramError):
            asyncio.run(self.ct.photo_bytes([agency, hub], "unknown"))

    def test_albums_single_photos_and_text(self):
        agency, hub = FakeBot("agency"), FakeBot("hub", readable={"a", "b", "c"})
        media = [{"telegram_file_id": x} for x in ("a", "b", "c")]
        ids = asyncio.run(self.ct.send_listing(agency, -100, "<b>کپشن</b>", media, [agency, hub]))
        self.assertEqual(ids, [300, 301, 302])
        self.assertEqual(agency.calls[-1], ("album", -100, [(b"hub:a", "<b>کپشن</b>"), (b"hub:b", None), (b"hub:c", None)]))
        # Telegram needs 2 to 10 photos in an album: one photo goes as a photo.
        self.assertEqual(asyncio.run(self.ct.send_listing(agency, -100, "x", media[:1], [hub])), [200])
        self.assertEqual(agency.calls[-1], ("photo", -100, b"hub:a", "x"))
        self.assertEqual(asyncio.run(self.ct.send_listing(agency, -100, "x", [], [hub])), [100])

    def test_caption_fits_a_photo(self):
        long_notes = {"tracking_code": "A6-1", "district": "نصر", "address_text": "x", "area_m2": 151,
                      "deal_type": "SALE", "special_notes": "خیلی " * 400}
        caption = self.ct.fit_caption(long_notes, {"display_name": "پاشا ملک"})
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("A6-1", caption)
        self.assertIn("پاشا ملک", caption)
        self.assertNotIn("خیلی", caption)

    def test_album_ids_for_older_posts(self):
        self.assertEqual(self.ct.album_message_ids({"telegram_message_ids": [7, 8], "telegram_message_id": 7}, 5), [7, 8])
        self.assertEqual(self.ct.album_message_ids({"telegram_message_ids": None, "telegram_message_id": 40}, 3), [40, 41, 42])
        self.assertEqual(self.ct.album_message_ids({"telegram_message_id": 40}, 0), [40])


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.ct = load_tools()

    def test_parse_slot_times(self):
        self.assertEqual(self.ct.parse_slot_times("۱۰ ۱۴ ۱۸:۳۰ ۲۱"), ["10:00", "14:00", "18:30", "21:00"])
        self.assertEqual(self.ct.parse_slot_times("9:05، 9:05 و 25"), ["09:05"])
        self.assertEqual(self.ct.parse_slot_times("سلام"), [])

    def test_free_slots_skip_past_and_held_times(self):
        now = datetime(2026, 10, 23, 13, 0, tzinfo=TEHRAN)
        held = [datetime(2026, 10, 23, 14, 0, tzinfo=TEHRAN)]
        slots = self.ct.free_slots(["10:00", "14:00", "18:30"], held, now, 3)
        self.assertEqual([s.strftime("%d %H:%M") for s in slots], ["23 18:30", "24 10:00", "24 14:00"])

    def test_when_label(self):
        now = datetime(2026, 10, 23, 13, 0, tzinfo=TEHRAN)  # a Friday
        self.assertEqual(self.ct.when_label(now.replace(hour=18, minute=30), now), "امروز ۱۸:۳۰")
        self.assertEqual(self.ct.when_label(now + timedelta(days=1), now), "فردا ۱۳:۰۰")
        self.assertEqual(self.ct.when_label(now + timedelta(days=3), now), "دوشنبه ۱۳:۰۰")


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.ct = load_tools()
        db = self.db = self.ct.db  # the partner hub's db module
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS partner_test CASCADE")
            conn.execute("CREATE SCHEMA partner_test")
        self.connect = lambda: psycopg.connect(DSN, row_factory=dict_row, options="-c search_path=partner_test")
        self.patch = mock.patch.object(db, "connect", self.connect)
        self.patch.start()
        with self.connect() as conn:
            conn.execute("CREATE TABLE properties (id BIGSERIAL PRIMARY KEY, status TEXT, updated_at TIMESTAMPTZ)")
            # ShahFile's own channel schedule: hourly 10:30–22:00, paused 15:00–16:30, every day.
            conn.execute("""
                CREATE TABLE schedule_windows (
                    id BIGSERIAL PRIMARY KEY, weekday INT, start_time TIME, end_time TIME,
                    interval_minutes INT, max_posts INT, is_active BOOL DEFAULT TRUE,
                    pause_start TIME, pause_end TIME);
                INSERT INTO schedule_windows (weekday, start_time, end_time, interval_minutes, pause_start, pause_end)
                SELECT d, '10:30', '22:00', 60, '15:00', '16:30' FROM generate_series(0, 6) d;
            """)
            conn.execute((HUB / "migrations" / "001_partner_hub.sql").read_text(encoding="utf-8"))
            conn.commit()
        db.ensure_partner_v2_schema()
        db.ensure_partner_v3_schema()
        db.ensure_partner_v3_schema()  # idempotent at every start
        db.ensure_central_schema()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO partner_agencies (id, legal_name, brand_name, slug, manager_name, manager_phone, status)
                VALUES (6, 'پاشا', 'پاشا ملک', 'pasha', 'مدیر', '09140000000', 'ACTIVE');
                INSERT INTO partner_members (id, agency_id, telegram_user_id, role)
                VALUES (1, 6, 555, 'AGENCY_OWNER');
                INSERT INTO partner_channel_connections (id, agency_id, telegram_channel_id, is_default)
                VALUES (9, 6, -100777, TRUE);
                INSERT INTO properties (id, status) VALUES (50, 'ACTIVE');
                INSERT INTO partner_submissions (id, tracking_code, agency_id, submitted_by_member_id,
                    property_type, district, address_text, area_m2, status, central_property_id)
                VALUES (1, 'A6-1', 6, 1, 'APARTMENT', 'نصر', 'x', 151, 'SUBMITTED', NULL),
                       (2, 'A6-2', 6, 1, 'APARTMENT', 'شاهگلی', 'x', 380, 'DRAFT', 50),
                       (3, 'A6-3', 6, 1, 'APARTMENT', 'منصور', 'x', 145, 'SOLD', NULL),
                       (4, 'A6-4', 6, 1, 'APARTMENT', 'زعفرانیه', 'x', 130, 'SUBMITTED', NULL);
                INSERT INTO partner_submission_media (submission_id, telegram_file_id, telegram_file_unique_id, position)
                VALUES (2, 'p1', 'u1', 1), (2, 'p2', 'u2', 2);
                INSERT INTO partner_publication_jobs (submission_id, target_type, channel_connection_id, status, telegram_message_id)
                VALUES (4, 'AGENCY_CHANNEL', 9, 'PUBLISHED', 70);
            """)
            conn.commit()

    def tearDown(self):
        self.patch.stop()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS partner_test CASCADE")

    def test_unpublished_files_and_backfill_on_the_schedule(self):
        self.assertEqual([r["id"] for r in self.ct.unpublished_files(6)], [1, 2])  # sold and published left out
        self.ct.save_schedule(6, slot_times=["10:00", "18:00"])
        now = datetime(2026, 10, 23, 12, 0, tzinfo=TEHRAN)
        planned = self.ct.queue_files(6, [1, 2], 9, now=now)
        self.assertEqual([(sid, t.strftime("%d %H:%M")) for sid, t in planned], [(1, "23 18:00"), (2, "24 10:00")])
        self.assertEqual(self.ct.unpublished_files(6), [])
        self.assertEqual(self.ct.channel_state(1)[0], "scheduled")
        self.ct.save_schedule(6, is_active=False)
        self.assertEqual(self.ct.schedule_settings(6), {"slot_times": ["10:00", "18:00"], "is_active": False})
        self.ct.save_schedule(6, slot_times=[])
        self.assertEqual(self.ct.schedule_settings(6)["slot_times"], [])

    def test_the_default_schedule_is_shahfiles(self):
        self.assertEqual(self.ct.default_slot_times(4), [
            "10:30", "11:30", "12:30", "13:30", "14:30", "16:30", "17:30", "18:30", "19:30", "20:30", "21:30"])
        now = datetime(2026, 10, 23, 21, 0, tzinfo=TEHRAN)
        times = self.ct.plan_times(6, 3, now=now)
        self.assertEqual([t.strftime("%d %H:%M") for t in times], ["23 21:30", "24 10:30", "24 11:30"])
        # The agency's own times win.
        self.ct.save_schedule(6, slot_times=["09:00"])
        self.assertEqual([t.strftime("%d %H:%M") for t in self.ct.plan_times(6, 1, now=now)], ["24 09:00"])

    def test_old_files_are_queued_on_their_own(self):
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO partner_bot_connections (agency_id, bot_id, encrypted_token, token_fingerprint)
                VALUES (6, 1, 'x', 'y');
                UPDATE partner_submissions SET created_at = NOW() - INTERVAL '1 day' WHERE id = 1;
                UPDATE partner_submissions SET created_at = NOW() WHERE id = 2;
            """)
            conn.commit()
        self.assertEqual(self.ct.auto_queue_all(), 1)  # 2 was just saved: the agent may still pick
        self.assertEqual(self.ct.channel_state(1)[0], "scheduled")
        self.assertEqual(self.ct.auto_queue_all(), 0)
        self.ct.cancel_queued(1)
        self.assertEqual(self.ct.channel_state(1), (None, None))
        with self.connect() as conn:  # a file whose posts keep failing is left alone
            conn.execute("""INSERT INTO partner_publication_jobs (submission_id, target_type, channel_connection_id, status)
                            VALUES (1, 'AGENCY_CHANNEL', 9, 'FAILED'), (1, 'AGENCY_CHANNEL', 9, 'FAILED')""")
            conn.commit()
        self.assertEqual(self.ct.auto_queue_all(), 0)

    def test_publishing_keeps_a_file_in_central_review(self):
        self.ct.record_publication(1, 9, [301, 302])
        self.ct.record_publication(2, 9, [401])
        with self.connect() as conn:
            rows = conn.execute("SELECT id, status FROM partner_submissions WHERE id IN (1,2) ORDER BY id").fetchall()
            job = conn.execute("SELECT telegram_message_id, telegram_message_ids FROM partner_publication_jobs WHERE submission_id=1").fetchone()
        self.assertEqual([(r["id"], r["status"]) for r in rows], [(1, "SUBMITTED"), (2, "ACTIVE")])
        self.assertEqual((job["telegram_message_id"], job["telegram_message_ids"]), (301, [301, 302]))
        self.assertEqual(self.ct.channel_state(1), ("published", None))

    def test_delete_removes_the_post_queue_and_central_listing(self):
        bot = FakeBot("agency")
        self.ct.record_publication(2, 9, [401, 402])
        self.ct.queue_files(6, [1], 9)
        with mock.patch.object(self.ct, "agency_bot", lambda agency_id: bot):
            result = asyncio.run(self.ct.delete_submission(self.db.one("SELECT * FROM partner_submissions WHERE id=2")))
        self.assertEqual(result, {"deleted_posts": 1, "kept_posts": 0})
        self.assertEqual(bot.calls, [("delete", -100777, [401, 402])])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM partner_submissions WHERE id=2").fetchone()["status"], "DELETED")
            self.assertEqual(conn.execute("SELECT status FROM properties WHERE id=50").fetchone()["status"], "INACTIVE")
            # ShahFile's bot is told to take its own post down too.
            self.assertEqual(conn.execute("SELECT property_id, event_type FROM partner_central_events").fetchone(),
                             {"property_id": 50, "event_type": "DELETED"})
            self.assertEqual(conn.execute("SELECT status FROM partner_publication_jobs WHERE submission_id=2").fetchone()["status"], "CANCELLED")
        self.assertEqual([r["id"] for r in self.ct.unpublished_files(6)], [])  # 1 is queued, 2 deleted

    def test_an_old_post_is_marked_instead(self):
        bot = FakeBot("agency", fail_delete=True)
        with mock.patch.object(self.ct, "agency_bot", lambda agency_id: bot):
            result = asyncio.run(self.ct.delete_submission(self.db.one("SELECT * FROM partner_submissions WHERE id=4")))
        self.assertEqual(result, {"deleted_posts": 0, "kept_posts": 1})
        self.assertEqual(bot.calls[0], ("delete", -100777, [70]))
        # No photos: the text post gets the note.
        self.assertEqual(bot.calls[1][:3], ("edit_text", -100777, 70))
        self.assertTrue(bot.calls[1][3].endswith("⛔️ این فایل دیگر موجود نیست."))
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM partner_submissions WHERE id=4").fetchone()["status"], "DELETED")


class WiringTest(unittest.TestCase):
    def test_wiring(self):
        app = (HUB / "app.py").read_text(encoding="utf-8")
        self.assertIn('B("⏰ زمان‌بندی ارسال به کانال",callback_data="p_sched")', app)
        self.assertIn("    db.ensure_partner_v3_schema()\n", app)
        self.assertIn("ids=await ct.publish_submission(s,conn,[context.bot])", app)
        self.assertIn("ids=await publish_scheduled_job(job,app.bot)", app)
        self.assertIn("AND COALESCE(ps.is_active,TRUE)", app)
        self.assertNotIn("InputMediaPhoto(m['telegram_file_id']", app)
        self.assertIn("queued=ct.auto_queue_all()", app)
        self.assertIn('log.info("agency channel post published:', app)
        self.assertIn('log.warning("agency channel post failed:', app)
        self.assertIn("    app.add_error_handler(on_error)\n", app)
        # Posting now or at a chosen time replaces a queued post.
        self.assertEqual(app.count("ct.cancel_queued("), 3)


if __name__ == "__main__":
    unittest.main()


class Button:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data = text, callback_data


def load_app():
    tools = load_tools()
    stubs = {
        "telegram": types.SimpleNamespace(
            Bot=lambda token: ("bot", token), BotCommand=lambda *a: a, InlineKeyboardButton=Button,
            InlineKeyboardMarkup=lambda rows: rows, MenuButtonCommands=lambda: None, Update=object,
            InputMediaPhoto=InputMediaPhoto),
        "telegram.constants": types.SimpleNamespace(ParseMode=types.SimpleNamespace(HTML="HTML")),
        "telegram.error": types.SimpleNamespace(TelegramError=TelegramError),
        "telegram.ext": types.SimpleNamespace(
            Application=object, CallbackQueryHandler=object, CommandHandler=object,
            ContextTypes=types.SimpleNamespace(DEFAULT_TYPE=object), MessageHandler=object,
            filters=types.SimpleNamespace(PHOTO=1, TEXT=1, COMMAND=1)),
        "channel_tools": tools,
        "jdatetime": types.SimpleNamespace(),
    }
    with mock.patch.dict(sys.modules, stubs), mock.patch.dict(os.environ, {"PARTNER_HUB_TOKEN": "hub-token"}):
        sys.modules.pop("app", None)
        import app
    return app, tools


class Query:
    def __init__(self):
        self.edits, self.alerts = [], []
        self.from_user = types.SimpleNamespace(id=555)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


class ScreensTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ct = load_app()
        self.mem = {"id": 1, "agency_id": 6, "role": "AGENCY_OWNER", "can_publish_own": True}
        self.file = {"id": 2, "tracking_code": "A6-2", "district": "شاهگلی", "area_m2": "380.00",
                     "status": "SUBMITTED", "current_price_toman": 12_000_000_000, "agency_id": 6,
                     "submitted_by_member_id": 1}
        self.patches = [
            mock.patch.object(self.app.db, "all_rows", lambda sql, params=(): [self.file]),
            mock.patch.object(self.app.db, "one", lambda sql, params=(): self.file),
            mock.patch.object(self.app.db, "audit", lambda *a, **k: None),
            mock.patch.object(self.ct, "default_channel", lambda aid: {"id": 9, "telegram_channel_id": -100}),
            mock.patch.object(self.ct, "agency_bot", lambda aid: object()),
            mock.patch.object(self.ct, "unpublished_files", lambda aid: [self.file]),
            mock.patch.object(self.ct, "channel_state", lambda sid: (None, None)),
            mock.patch.object(self.ct, "plan_times", lambda aid, count, now=None: [
                datetime(2026, 10, 23, 18, 30, tzinfo=TEHRAN)] * count),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def test_files_list_and_file_screen(self):
        q = Query()
        asyncio.run(self.app.list_files(q, self.mem))
        rows = q.edits[-1][1]
        self.assertEqual(rows[0][0].text, "A6-2 | شاهگلی | ۳۸۰م | در انتظار بررسی مرکزی")
        self.assertEqual(rows[0][0].callback_data, "pfile_2")
        self.assertEqual(rows[1][0].text, "📤 ارسال ۱ فایل منتشرنشده به کانال")
        asyncio.run(self.app.show_file(q, self.mem, 2))
        text, rows = q.edits[-1]
        self.assertIn("کانال بنگاه: ❌ هنوز منتشر نشده", text)
        self.assertEqual([r[0].callback_data for r in rows], ["pfsend_2", "pdel_2", "p_files"])
        self.assertEqual(rows[0][1].callback_data, "pfslot_2")
        self.assertTrue(rows[0][1].text.startswith("⏰ نوبت بعدی ("))

    def test_delete_asks_first(self):
        q = Query()
        deleted = []

        async def delete(submission):
            deleted.append(submission["id"])
            return {"deleted_posts": 1, "kept_posts": 0}

        with mock.patch.object(self.ct, "delete_submission", delete):
            asyncio.run(self.app.delete_file(q, self.mem, "pdel_2"))
            self.assertEqual(deleted, [])
            self.assertIn("حذف بشه؟", q.edits[-1][0])
            asyncio.run(self.app.delete_file(q, self.mem, "pdelok_2"))
        self.assertEqual(deleted, [2])
        self.assertEqual(q.edits[-1][0], "✅ فایل A6-2 حذف شد.")
        # Someone else's file, for an advisor, is refused.
        advisor = dict(self.mem, id=7, role="ADVISOR")
        q = Query()
        asyncio.run(self.app.delete_file(q, advisor, "pdel_2"))
        self.assertEqual(q.alerts, ["اجازه حذف این فایل را ندارید."])

    def test_an_old_form_button_after_a_restart(self):
        q = Query()
        context = types.SimpleNamespace(user_data={})
        with mock.patch.object(self.app, "home_keyboard", lambda mem, owner=False: "home"):
            asyncio.run(self.app.property_callback(q, context, "pf_photos_done", self.mem))
            self.assertTrue(q.edits[-1][0].startswith("⌛️ این فرم منقضی شده"))
            asyncio.run(self.app.property_callback(q, context, "pf_own_now", self.mem))
            self.assertTrue(q.edits[-1][0].startswith("⌛️ این فرم منقضی شده"))

    def test_slot_times_are_saved(self):
        saved = []
        context = types.SimpleNamespace(user_data={"flow": "slot_times"})
        replies = []

        class Message:
            text = "۱۰ ۱۴ ۱۸:۳۰"

            async def reply_text(self, text, reply_markup=None):
                replies.append(text)

        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=555), message=Message())
        with mock.patch.object(self.app.db, "member", lambda uid: self.mem), \
                mock.patch.object(self.ct, "save_schedule", lambda aid, **c: saved.append((aid, c))), \
                mock.patch.object(self.app, "schedule_text_view", lambda aid: ("view", [])):
            asyncio.run(self.app.slot_times_text(update, context))
        self.assertEqual(saved, [(6, {"slot_times": ["10:00", "14:00", "18:30"]})])
        self.assertEqual(context.user_data, {})
        self.assertTrue(replies[0].startswith("✅ ساعت‌ها ذخیره شد."))
