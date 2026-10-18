"""Settings › «💾 بکاپ دیتابیس»: the owners get a full database copy every night.

pg_dump runs inside the postgres container (COPY … TO PROGRAM), the gzip'd
SQL is sent to every owner in Telegram after 03:30 Tehran time, once a
night, and a failure is reported instead of retried every few minutes.
"""
import asyncio
import gzip
import os
import shutil
import subprocess
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
APP = ROOT / "app"
DSN = os.getenv("PGTEST_DSN")
TEHRAN = ZoneInfo("Asia/Tehran")

try:
    import psycopg
except ImportError:  # pragma: no cover - local runs without the driver
    psycopg = None


class Button:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data = text, callback_data


def gregorian_to_jalali(gy, gm, gd):
    source = (APP / "shahfile_ai.py").read_text(encoding="utf-8")
    start = source.index("def _gregorian_to_jalali")
    namespace = {}
    exec(source[start:source.index("def _today_jalali")], namespace)
    return namespace["_gregorian_to_jalali"](gy, gm, gd)


def load_backup_module():
    sys.path.insert(0, str(APP))
    stubs = {
        "telegram": types.SimpleNamespace(InlineKeyboardButton=Button, InlineKeyboardMarkup=lambda rows: rows),
        "shahfile_ai": types.SimpleNamespace(_gregorian_to_jalali=gregorian_to_jalali),
    }
    if psycopg is None:
        stubs.update({
            "psycopg": types.SimpleNamespace(Error=Exception, connect=None),
            "psycopg.sql": types.SimpleNamespace(),
            "psycopg.types": types.SimpleNamespace(),
            "psycopg.types.json": types.SimpleNamespace(Jsonb=dict),
        })
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("daily_backup", None)
        import daily_backup
    return daily_backup


class Bot:
    def __init__(self, fail=False):
        self.documents, self.messages, self.fail = [], [], fail

    async def send_document(self, chat_id, document, filename, caption, **timeouts):
        if self.fail:
            raise TimeoutError("Timed out")
        self.documents.append((chat_id, filename, document.read(), caption, timeouts["write_timeout"]))

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))


NIGHT = datetime(2026, 10, 18, 3, 45, tzinfo=TEHRAN)


class BackupScheduleTest(unittest.TestCase):
    def setUp(self):
        self.backup = load_backup_module()
        self.settings = {"enabled": True}

        def save(**changes):
            self.settings.update(changes)
            return dict(self.settings)

        self.patches = [
            mock.patch.object(self.backup, "backup_settings", lambda: dict(self.settings)),
            mock.patch.object(self.backup, "save_backup_settings", save),
            mock.patch.object(self.backup, "owner_chat_ids", lambda: [11, 22]),
            mock.patch.object(self.backup, "dump_database", lambda: b"dump-bytes"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def run_due(self, bot, now):
        return asyncio.run(self.backup.run_due_backup(bot, now))

    def test_sent_to_every_owner_once_a_night(self):
        bot = Bot()
        self.assertFalse(self.run_due(bot, NIGHT.replace(hour=3, minute=0)))  # before 03:30
        self.assertTrue(self.run_due(bot, NIGHT))
        self.assertEqual([(d[0], d[1], d[2]) for d in bot.documents], [
            (11, "shahfile-backup-1405-07-26-0345.sql.gz", b"dump-bytes"),
            (22, "shahfile-backup-1405-07-26-0345.sql.gz", b"dump-bytes"),
        ])
        caption = bot.documents[0][3]
        self.assertIn("🗓 ۱۴۰۵/۰۷/۲۶ ساعت ۰۳:۴۵", caption)
        self.assertIn("📦 حجم: ۱ کیلوبایت", caption)
        self.assertIn("برای کسی فوروارد نکنید", caption)
        self.assertEqual(bot.documents[0][4], 120)  # a big file gets time to upload
        self.assertEqual(self.settings["last_date"], "2026-10-18")
        self.assertEqual(self.settings["last_size"], 10)
        # The same night again, or later that day: nothing more.
        self.assertFalse(self.run_due(bot, NIGHT.replace(hour=14)))
        self.assertEqual(len(bot.documents), 2)
        # A night the bot was down is made up when it is back.
        self.assertTrue(self.run_due(bot, datetime(2026, 10, 19, 11, 0, tzinfo=TEHRAN)))

    def test_switched_off_sends_nothing(self):
        self.settings["enabled"] = False
        bot = Bot()
        self.assertFalse(self.run_due(bot, NIGHT))
        self.assertEqual(bot.documents, [])

    def test_a_failure_is_reported_once_not_retried(self):
        def broken():
            raise self.backup.BackupError("ساخت فایل بکاپ روی سرور انجام نشد.")

        bot = Bot()
        with mock.patch.object(self.backup, "dump_database", broken):
            self.assertFalse(self.run_due(bot, NIGHT))
            self.assertFalse(self.run_due(bot, NIGHT.replace(minute=50)))
        self.assertEqual([chat for chat, _text in bot.messages], [11, 22])
        self.assertIn("⚠️ بکاپ شبانهٔ دیتابیس انجام نشد.", bot.messages[0][1])
        self.assertNotIn("last_at", self.settings)

    def test_too_big_for_telegram(self):
        bot = Bot()
        with mock.patch.object(self.backup, "TELEGRAM_FILE_LIMIT", 5):
            self.assertFalse(self.run_due(bot, NIGHT))
        self.assertEqual(bot.documents, [])
        self.assertIn("سقف ۵۰ مگابایتی تلگرام", bot.messages[0][1])

    def test_unsent_file_is_a_failure(self):
        bot = Bot(fail=True)
        self.assertFalse(self.run_due(bot, NIGHT))
        self.assertIn("ارسالش توی تلگرام انجام نشد", bot.messages[0][1])

    def test_menu(self):
        text = self.backup.backup_menu_text(
            {"enabled": True, "last_at": "2026-10-18T03:45+03:30", "last_size": 2_500_000})
        self.assertIn("هر شب ساعت ۰۳:۳۰", text)
        self.assertIn("بکاپ شبانه: ✅ روشن", text)
        self.assertIn("آخرین بکاپ: ۱۴۰۵/۰۷/۲۶ ساعت ۰۳:۴۵ (۲٫۴ مگابایت)", text)
        rows = self.backup.backup_keyboard({"enabled": False})
        self.assertEqual([row[0].callback_data for row in rows], ["db_backup_now", "db_backup_toggle", "settings"])
        self.assertEqual(rows[1][0].text, "▶️ روشن کردن بکاپ شبانه")

    def test_only_the_owner(self):
        edits = []

        class Query:
            data = "db_backup_now"

            async def edit_message_text(self, text, reply_markup=None):
                edits.append(text)

        asyncio.run(self.backup.handle_backup_button(Query(), None, (5, "ali", "Ali", "ADMIN")))
        self.assertEqual(edits, ["⛔ بکاپ دیتابیس فقط برای مالک در دسترسه."])


class WiringTest(unittest.TestCase):
    def test_wiring(self):
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        self.assertIn("    start_daily_backup_worker(application)\n", bot)
        handler = bot[bot.index("async def button_handler("):]
        self.assertLess(handler.index("# CRM_ACCESS_GUARD_V1"),
                        handler.index("await handle_backup_button(query, context, session)"))
        crm = (APP / "crm_panel.py").read_text(encoding="utf-8")
        self.assertIn('if action in ("users", "settings_users", "reset") or action.startswith("db_backup"):\n'
                      '        return "OWNER_ONLY"', crm)
        self.assertIn('"💾 بکاپ دیتابیس",\n                callback_data="db_backup",', crm)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("gunzip -c shahfile-backup-", readme)


@unittest.skipUnless(psycopg and DSN, "set PGTEST_DSN to a scratch PostgreSQL")
class DumpDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.backup = load_backup_module()
        with psycopg.connect(DSN) as conn:
            conn.execute("""
                DROP TABLE IF EXISTS backup_probe;
                CREATE TABLE backup_probe (id INT PRIMARY KEY, note TEXT);
                INSERT INTO backup_probe VALUES (1, 'فایل ۳۵۱۳');
            """)

    def test_the_servers_pg_dump_makes_a_restorable_sql_file(self):
        with mock.patch.object(self.backup, "db_connect", lambda: psycopg.connect(DSN)):
            data = self.backup.dump_database()
        sql_text = gzip.decompress(data).decode("utf-8")
        self.assertIn("CREATE TABLE public.backup_probe", sql_text)
        self.assertIn("DROP TABLE IF EXISTS public.backup_probe", sql_text)
        self.assertIn("فایل ۳۵۱۳", sql_text)
        self.assertNotIn("OWNER TO", sql_text)
        with psycopg.connect(DSN) as conn:
            # The temp files on the database server are gone.
            gone = conn.execute("SELECT pg_stat_file(%s, true)", (self.backup.DUMP_PATH,)).fetchone()[0]
        self.assertIsNone(gone)

    @unittest.skipUnless(shutil.which("psql"), "psql not installed")
    def test_the_file_restores_with_the_readme_command(self):
        with mock.patch.object(self.backup, "db_connect", lambda: psycopg.connect(DSN)):
            data = self.backup.dump_database()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS backup_restore_probe")
            conn.execute("CREATE DATABASE backup_restore_probe")
        target = psycopg.conninfo.make_conninfo(DSN, dbname="backup_restore_probe")
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", target],
                       input=gzip.decompress(data), check=True, capture_output=True)
        with psycopg.connect(target) as conn:
            self.assertEqual(conn.execute("SELECT note FROM backup_probe").fetchall(), [("فایل ۳۵۱۳",)])
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP DATABASE backup_restore_probe")

    def test_a_user_without_superuser_rights_gets_a_clear_error(self):
        with psycopg.connect(DSN) as conn:
            conn.execute("DROP ROLE IF EXISTS backup_tester; CREATE ROLE backup_tester LOGIN")
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        params["user"] = "backup_tester"
        with mock.patch.object(self.backup, "db_connect", lambda: psycopg.connect(**params)):
            with self.assertRaises(self.backup.BackupError) as error:
                self.backup.dump_database()
        self.assertIn("اجازهٔ بکاپ گرفتن نداره", str(error.exception))


if __name__ == "__main__":
    unittest.main()
