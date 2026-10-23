"""An agency's channel post opens like a ShahFile post.

The owner saw «🧾 کد پیگیری: A6-F3E7FE11» on the agency channel posts and
wanted them like his own channel instead: the price hashtag first, then
«کد واحد», a number each agency counts from 100 followed by the price's
whole billions (ShahFile's «40815» is its 408th file at 15 billion).
"""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import test_partner_channel as partner

HUB = Path(__file__).parent / "partner_hub"
DSN = os.getenv("PGTEST_DSN")
TEST_DB = "partner_unit_code_test"

sys.path.insert(0, str(HUB))
from formatter import price_hashtag, public_caption  # noqa: E402

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None


class CaptionTest(unittest.TestCase):
    def test_opens_with_the_hashtag_and_unit_code(self):
        caption = public_caption({
            "tracking_code": "A6-F3E7FE11", "unit_code": "10038", "deal_type": "SALE",
            "district": "شاهگلی", "area_m2": "184.00", "current_price_toman": 38_600_000_000,
        }, {"display_name": "پاشا", "public_mobile": "09146603999"})
        lines = caption.splitlines()
        self.assertEqual(lines[:4], ["#آپارتمان_38_39", "کد واحد: 10038", "", "🏠 <b>پاشا</b>"])
        self.assertNotIn("A6-F3E7FE11", caption)
        self.assertNotIn("کد پیگیری", caption)
        self.assertIn("💰 قیمت کل: ۳۸.۶ میلیارد تومان", caption)
        self.assertIn("📐 متراژ: ۱۸۴ متر", caption)

    def test_rentals_and_files_without_a_code(self):
        rent = public_caption({"tracking_code": "A6-1", "unit_code": "104", "deal_type": "RENT",
                               "district": "نصر", "deposit_toman": 500_000_000}, {"display_name": "پاشا"})
        self.assertTrue(rent.startswith("کد واحد: 104\n\n🏠"))
        bare = public_caption({"tracking_code": "A6-1", "district": "نصر"}, {"display_name": "پاشا"})
        self.assertTrue(bare.startswith("🏠 <b>پاشا</b>"))
        self.assertEqual(price_hashtag(12_500_000_000), "#آپارتمان_12_13")
        self.assertIsNone(price_hashtag(None))


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class UnitCodeTest(unittest.TestCase):
    def setUp(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
        self.connect = lambda: psycopg.connect(DSN, dbname=TEST_DB, row_factory=dict_row)
        self.app, self.ct = partner.load_app()
        self.db = self.ct.db
        # The app and its channel tools may hold separate copies of db.
        self.patches = [mock.patch.object(module, "connect", self.connect) for module in {self.db, self.app.db}]
        for patch in self.patches:
            patch.start()
        with self.connect() as conn:
            conn.execute("CREATE TABLE properties (id BIGSERIAL PRIMARY KEY, status TEXT, updated_at TIMESTAMPTZ)")
            conn.execute((HUB / "migrations" / "001_partner_hub.sql").read_text(encoding="utf-8"))
            conn.commit()
        self.db.ensure_partner_v2_schema()
        self.db.ensure_partner_v3_schema()
        self.db.ensure_unit_code_schema()
        self.db.ensure_unit_code_schema()  # idempotent at every start
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO partner_agencies (id, legal_name, brand_name, slug, manager_name, manager_phone, status)
                VALUES (5, 'بشیری', 'بشیری ملک', 'bashiri', 'مدیر', '0914', 'ACTIVE'),
                       (6, 'پاشا', 'پاشا ملک', 'pasha', 'مدیر', '0914', 'ACTIVE');
                INSERT INTO partner_members (id, agency_id, telegram_user_id, role)
                VALUES (1, 6, 555, 'AGENCY_OWNER'), (2, 5, 556, 'AGENCY_OWNER');
                INSERT INTO partner_submissions (id, tracking_code, agency_id, submitted_by_member_id,
                    property_type, deal_type, district, address_text, area_m2, current_price_toman, status)
                VALUES (1, 'A6-1', 6, 1, 'APARTMENT', 'SALE', 'شاهگلی', 'x', 184, 38600000000, 'ACTIVE'),
                       (2, 'A6-2', 6, 1, 'APARTMENT', 'SALE', 'نصر', 'x', 151, 9500000000, 'SOLD'),
                       (3, 'A6-3', 6, 1, 'APARTMENT', 'RENT', 'نصر', 'x', 90, NULL, 'DRAFT'),
                       (4, 'A6-4', 6, 1, 'APARTMENT', 'SALE', 'منصور', 'x', 120, 8000000000, 'DELETED'),
                       (5, 'A5-1', 5, 2, 'APARTMENT', 'SALE', 'ولیعصر', 'x', 100, 12000000000, 'DRAFT');
            """)
            conn.commit()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")

    def codes(self):
        with self.connect() as conn:
            return {r["id"]: r["unit_code"] for r in conn.execute("SELECT id, unit_code FROM partner_submissions")}

    def test_each_agency_counts_from_100(self):
        self.assertEqual(self.db.assign_unit_code(1), "10038")
        self.assertEqual(self.db.assign_unit_code(1), "10038")  # given once
        self.assertEqual(self.db.assign_unit_code(3), "101")  # a rental: the number alone
        self.assertEqual(self.db.assign_unit_code(5), "10012")  # another agency starts at 100 too

    def test_older_files_get_codes_and_live_posts_are_edited(self):
        refreshed = []

        async def refresh(submission_id, note=None):
            refreshed.append(submission_id)

        with mock.patch.object(self.app, "refresh_agency_channel_post", refresh):
            self.assertEqual(asyncio.run(self.app.backfill_unit_codes()), 4)
            self.assertEqual(asyncio.run(self.app.backfill_unit_codes()), 0)
        # Oldest first; a deleted file gets none; a sold post keeps its «no longer available» caption.
        self.assertEqual(self.codes(), {1: "10038", 2: "1019", 3: "102", 4: None, 5: "10012"})
        self.assertEqual(refreshed, [1, 3, 5])

    def test_posting_gives_a_missing_code(self):
        class Bot:
            async def send_message(self, chat_id, text, parse_mode=None):
                self.text = text
                return partner.Message(1)

        bot = Bot()
        with mock.patch.object(self.ct, "agency_bot", lambda agency_id: bot), \
                mock.patch.object(self.ct, "hub_bot", lambda: None):
            submission = self.db.one("SELECT * FROM partner_submissions WHERE id=1")
            asyncio.run(self.ct.publish_submission(submission, {"telegram_channel_id": -100}))
        self.assertTrue(bot.text.startswith("#آپارتمان_38_39\nکد واحد: 10038\n"))


class ScreensTest(partner.ScreensTest):
    def test_files_list_and_file_screen(self):
        self.file["unit_code"] = "10012"
        q = partner.Query()
        asyncio.run(self.app.list_files(q, self.mem))
        self.assertEqual(q.edits[-1][1][0][0].text, "10012 | شاهگلی | ۳۸۰م | در انتظار بررسی مرکزی")
        asyncio.run(self.app.show_file(q, self.mem, 2))
        self.assertTrue(q.edits[-1][0].startswith("🧾 کد واحد: 10012\n"))

    test_delete_asks_first = test_an_old_form_button_after_a_restart = test_slot_times_are_saved = None


if __name__ == "__main__":
    unittest.main()
