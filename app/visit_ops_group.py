"""Dedicated Telegram operations group for property visit requests."""

import logging
import os

import psycopg


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def ensure_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS visit_ops_destination (
                    singleton_id SMALLINT PRIMARY KEY DEFAULT 1
                        CHECK (singleton_id = 1),
                    telegram_chat_id BIGINT NOT NULL,
                    message_thread_id BIGINT,
                    chat_title TEXT,
                    configured_by_user_id BIGINT
                        REFERENCES users(id) ON DELETE SET NULL,
                    configured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()


def get_visit_ops_target():
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT telegram_chat_id, message_thread_id, chat_title
                    FROM visit_ops_destination
                    WHERE singleton_id = 1
                """)
                return cur.fetchone()
    except Exception:
        logging.exception("Could not read visit operations destination")
        return None


def save_visit_ops_target(chat_id, thread_id, title, actor_user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO visit_ops_destination (
                    singleton_id, telegram_chat_id, message_thread_id,
                    chat_title, configured_by_user_id, configured_at, updated_at
                )
                VALUES (1, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (singleton_id)
                DO UPDATE SET
                    telegram_chat_id = EXCLUDED.telegram_chat_id,
                    message_thread_id = EXCLUDED.message_thread_id,
                    chat_title = EXCLUDED.chat_title,
                    configured_by_user_id = EXCLUDED.configured_by_user_id,
                    configured_at = NOW(),
                    updated_at = NOW()
            """, (chat_id, thread_id, title, actor_user_id))
        conn.commit()


def telegram_thread_kwargs(target):
    if target and target[1]:
        return {"message_thread_id": int(target[1])}
    return {}


async def handle_connect_command(update, context, session):
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(
            "این فرمان را داخل گروه خصوصی Bazdid File ارسال کنید."
        )
        return
    if not session or session[3] != "OWNER":
        await message.reply_text(
            "⛔ اتصال گروه بازدید فقط توسط مدیر اصلی انجام می‌شود."
        )
        return
    thread_id = getattr(message, "message_thread_id", None)
    save_visit_ops_target(
        chat.id,
        thread_id,
        chat.title or "Bazdid File",
        session[0],
    )
    await message.reply_text(
        "✅ گروه عملیات بازدید متصل شد.\n\n"
        "از این به بعد درخواست‌های بازدید، تصمیم‌های زمانی و "
        "یادآوری یک ساعت قبل در همین بخش ارسال می‌شوند."
    )
