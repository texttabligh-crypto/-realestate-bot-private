"""
Settings › «💾 بکاپ دیتابیس»: a full copy of the database for the owners in
Telegram every night, so a lost server can be rebuilt from it.

The dump is made by pg_dump inside the postgres container (COPY … TO
PROGRAM, as the database superuser): the bot image has no pg_dump, and the
server's own pg_dump always matches its version.
"""

import asyncio
import io
import logging
import os
import shlex
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shahfile_ai import _gregorian_to_jalali

DB_BACKUP_ACTION = "db_backup"
DB_BACKUP_NOW = "db_backup_now"
DB_BACKUP_TOGGLE = "db_backup_toggle"
BACKUP_SETTING_KEY = "daily_backup"
TEHRAN = ZoneInfo("Asia/Tehran")
# Every night after 03:30 Tehran time, when the bot is quiet; a night the
# bot was down is made up as soon as it is back.
BACKUP_TIME = dtime(3, 30)
BACKUP_CHECK_SECONDS = 300
# Telegram bots may send files up to 50 MB.
TELEGRAM_FILE_LIMIT = 49 * 1024 * 1024
# Inside the postgres container; removed right after each backup.
DUMP_PATH = "/tmp/shahfile-backup.sql.gz"
ERROR_PATH = "/tmp/shahfile-backup.err"
PERSIAN_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")

_LOCK = asyncio.Lock()


class BackupError(Exception):
    """A backup that could not be made; the message is shown to the owner."""


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def backup_settings():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                (BACKUP_SETTING_KEY,),
            )
            row = cur.fetchone()
    value = row[0] if row and isinstance(row[0], dict) else {}
    return {"enabled": True, **value}


def save_backup_settings(**changes):
    settings = {**backup_settings(), **changes}
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (setting_key)
                DO UPDATE SET setting_value = EXCLUDED.setting_value, updated_at = NOW()
                """,
                (BACKUP_SETTING_KEY, Jsonb(settings)),
            )
        conn.commit()
    return settings


def owner_chat_ids():
    """The Telegram chat of every active owner, from their latest session."""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (u.id) us.telegram_user_id
                FROM users u
                JOIN user_sessions us ON us.user_id = u.id
                WHERE u.is_active = TRUE
                  AND u.role = 'OWNER'
                  AND us.telegram_user_id IS NOT NULL
                ORDER BY u.id, us.last_activity_at DESC NULLS LAST, us.id DESC
                """
            )
            return [row[0] for row in cur.fetchall()]


def _run_on_server(cur, command):
    cur.execute(sql.SQL("COPY (SELECT 1) TO PROGRAM {}").format(sql.Literal(command)))


def _server_error(cur):
    try:
        cur.execute("SELECT pg_read_file(%s)", (ERROR_PATH,))
        return (cur.fetchone()[0] or "").strip()[-300:]
    except psycopg.Error:
        return ""


def dump_database():
    """The whole database as gzip-compressed SQL from the server's pg_dump."""

    with db_connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if not cur.fetchone()[0]:
                raise BackupError("کاربر دیتابیس اجازهٔ بکاپ گرفتن نداره.")
            cur.execute(
                """
                SELECT current_database(), current_user,
                       split_part(current_setting('unix_socket_directories'), ',', 1),
                       current_setting('port')
                """
            )
            database, user, socket_dir, port = cur.fetchone()
            command = " ".join([
                "pg_dump",
                f"--host={shlex.quote(socket_dir.strip())}",
                f"--port={shlex.quote(port)}",
                f"--username={shlex.quote(user)}",
                f"--dbname={shlex.quote(database)}",
                "--no-owner --no-privileges --clean --if-exists --compress=6",
                f"--file={DUMP_PATH} 2>{ERROR_PATH}",
            ])
            try:
                _run_on_server(cur, command)
                cur.execute("SELECT pg_read_binary_file(%s)", (DUMP_PATH,))
                data = bytes(cur.fetchone()[0])
            except psycopg.Error as exc:
                logging.error("Backup dump failed: %s %s", exc, _server_error(cur))
                raise BackupError("ساخت فایل بکاپ روی سرور انجام نشد.") from exc
            finally:
                try:
                    _run_on_server(cur, f"rm -f {DUMP_PATH} {ERROR_PATH}")
                except psycopg.Error:
                    logging.exception("Backup temp files could not be removed")
    if not data:
        raise BackupError("فایل بکاپ خالی ساخته شد.")
    return data


def persian_digits(value):
    return str(value).translate(PERSIAN_DIGITS)


def jalali_label(moment):
    """«۱۴۰۵/۰۷/۲۶ ساعت ۰۳:۳۰» for a Tehran datetime."""

    jy, jm, jd = _gregorian_to_jalali(moment.year, moment.month, moment.day)
    return persian_digits(f"{jy:04d}/{jm:02d}/{jd:02d} ساعت {moment:%H:%M}")


def size_label(size):
    if size < 1024 * 1024:
        return f"{persian_digits(max(1, round(size / 1024)))} کیلوبایت"
    return f"{persian_digits(f'{size / 1024 / 1024:.1f}')} مگابایت"


def backup_filename(moment):
    jy, jm, jd = _gregorian_to_jalali(moment.year, moment.month, moment.day)
    return f"shahfile-backup-{jy:04d}-{jm:02d}-{jd:02d}-{moment:%H%M}.sql.gz"


def backup_caption(moment, size):
    return (
        "💾 بکاپ کامل دیتابیس ربات\n"
        f"🗓 {jalali_label(moment)}\n"
        f"📦 حجم: {size_label(size)}\n\n"
        "این فایل رو نگه دارید؛ اگه سرور به مشکل خورد، همه چیز با همین فایل برمی‌گرده.\n"
        "🔒 اطلاعات مشتری‌ها توشه؛ برای کسی فوروارد نکنید."
    )


async def send_backup(bot, chat_ids, moment=None):
    """Dump the database and send it to chat_ids; returns (size, sent)."""

    moment = moment or datetime.now(TEHRAN)
    async with _LOCK:
        data = await asyncio.to_thread(dump_database)
    if len(data) > TELEGRAM_FILE_LIMIT:
        raise BackupError(
            f"حجم بکاپ ({size_label(len(data))}) از سقف ۵۰ مگابایتی تلگرام بیشتره."
        )
    filename, caption = backup_filename(moment), backup_caption(moment, len(data))
    sent = 0
    for chat_id in chat_ids:
        try:
            await bot.send_document(
                chat_id,
                document=io.BytesIO(data),
                filename=filename,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=10,
                pool_timeout=30,
            )
            sent += 1
        except Exception:
            logging.exception("Backup could not be sent: chat_id=%s", chat_id)
    if chat_ids and not sent:
        raise BackupError("فایل بکاپ ساخته شد ولی ارسالش توی تلگرام انجام نشد.")
    return len(data), sent


async def run_due_backup(bot, now=None):
    """Send tonight's backup to the owners once it is due; True when sent."""

    now = now or datetime.now(TEHRAN)
    if now.time() < BACKUP_TIME:
        return False
    settings = await asyncio.to_thread(backup_settings)
    today = now.date().isoformat()
    if not settings["enabled"] or settings.get("last_date") == today:
        return False
    # One try a night: a failure is reported, not retried every few minutes.
    await asyncio.to_thread(save_backup_settings, last_date=today)
    chat_ids = await asyncio.to_thread(owner_chat_ids)
    if not chat_ids:
        logging.warning("Daily backup skipped: no owner chat")
        return False
    try:
        size, sent = await send_backup(bot, chat_ids, now)
    except BackupError as exc:
        logging.error("Daily backup failed: %s", exc)
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, f"⚠️ بکاپ شبانهٔ دیتابیس انجام نشد.\n{exc}\nبه ادریس خبر بدید 🙏")
            except Exception:
                logging.exception("Backup failure notice could not be sent: chat_id=%s", chat_id)
        return False
    await asyncio.to_thread(
        save_backup_settings, last_at=now.isoformat(timespec="minutes"), last_size=size
    )
    logging.info("Daily backup sent: size=%s chats=%s", size, sent)
    return True


async def _backup_loop(application):
    # Let the bot finish starting up first.
    await asyncio.sleep(90)
    while True:
        try:
            await run_due_backup(application.bot)
        except Exception:
            logging.exception("Daily backup check failed")
        await asyncio.sleep(BACKUP_CHECK_SECONDS)


def start_daily_backup_worker(application):
    existing = application.bot_data.get("daily_backup_task")
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_backup_loop(application), name="daily_backup")
    application.bot_data["daily_backup_task"] = task
    return task


def backup_menu_text(settings, note=None):
    lines = [
        "💾 بکاپ دیتابیس",
        "",
        f"هر شب ساعت {persian_digits(f'{BACKUP_TIME:%H:%M}')} یه نسخهٔ کامل از دیتابیس "
        "(فایل‌ها، مشتری‌ها، بازدیدها و تنظیمات) برای مالک‌ها توی همین چت فرستاده می‌شه. "
        "اگه سرور به مشکل خورد، همه چیز با همون فایل برمی‌گرده.",
        "",
        f"بکاپ شبانه: {'✅ روشن' if settings['enabled'] else '⏸ خاموش'}",
    ]
    if settings.get("last_at"):
        moment = datetime.fromisoformat(settings["last_at"])
        size = f" ({size_label(settings['last_size'])})" if settings.get("last_size") else ""
        lines.append(f"آخرین بکاپ: {jalali_label(moment)}{size}")
    if note:
        lines += ["", note]
    return "\n".join(lines)


def backup_keyboard(settings):
    toggle = "⏸ خاموش کردن بکاپ شبانه" if settings["enabled"] else "▶️ روشن کردن بکاپ شبانه"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 همین الان بکاپ بگیر", callback_data=DB_BACKUP_NOW)],
        [InlineKeyboardButton(toggle, callback_data=DB_BACKUP_TOGGLE)],
        [InlineKeyboardButton("⬅️ بازگشت به تنظیمات", callback_data="settings")],
    ])


async def _backup_now(bot, chat_id, status):
    try:
        moment = datetime.now(TEHRAN)
        size, _sent = await send_backup(bot, [chat_id], moment)
        settings = await asyncio.to_thread(
            save_backup_settings, last_at=moment.isoformat(timespec="minutes"), last_size=size
        )
        note = "✅ بکاپ فرستاده شد."
    except BackupError as exc:
        settings = await asyncio.to_thread(backup_settings)
        note = f"⚠️ بکاپ انجام نشد.\n{exc}"
    except Exception:
        logging.exception("Backup on request failed: chat_id=%s", chat_id)
        settings = await asyncio.to_thread(backup_settings)
        note = "⚠️ بکاپ انجام نشد. به ادریس خبر بدید 🙏"
    try:
        await status.delete()
    except Exception:
        pass
    await bot.send_message(chat_id, backup_menu_text(settings, note), reply_markup=backup_keyboard(settings))


async def handle_backup_button(query, context, session):
    if not session or session[3] != "OWNER":
        await query.edit_message_text("⛔ بکاپ دیتابیس فقط برای مالک در دسترسه.")
        return

    action = query.data or ""
    if action == DB_BACKUP_NOW:
        if _LOCK.locked():
            settings = await asyncio.to_thread(backup_settings)
            await query.edit_message_text(
                backup_menu_text(settings, "⏳ یه بکاپ در حال ساخته؛ چند لحظه دیگه دوباره امتحان کنید."),
                reply_markup=backup_keyboard(settings),
            )
            return
        await query.edit_message_text("⏳ دارم بکاپ می‌گیرم؛ چند ثانیه صبر کنید…")
        # Running beside the update handlers: other chats keep being answered.
        context.application.create_task(_backup_now(context.bot, query.message.chat_id, query.message))
        return

    if action == DB_BACKUP_TOGGLE:
        current = await asyncio.to_thread(backup_settings)
        settings = await asyncio.to_thread(save_backup_settings, enabled=not current["enabled"])
    else:
        settings = await asyncio.to_thread(backup_settings)
    await query.edit_message_text(backup_menu_text(settings), reply_markup=backup_keyboard(settings))
