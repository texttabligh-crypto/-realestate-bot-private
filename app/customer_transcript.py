"""Durable customer-only text history, visible through the CRM permission gate."""
import asyncio
import logging
from zoneinfo import ZoneInfo

from telegram.ext import ExtBot
from crm_campaign_hub import db_connect


def ensure_schema():
    with db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS customer_chat_history (
            id BIGSERIAL PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('customer','bot')),
            body TEXT NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(telegram_user_id, message_id, direction)
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS customer_chat_history_user
            ON customer_chat_history(telegram_user_id, id)""")


def save_message(user_id, message_id, direction, body):
    if not body or user_id <= 0:
        return
    with db_connect() as conn:
        # Never retain staff conversations or login credentials.
        if conn.execute("SELECT 1 FROM user_sessions WHERE telegram_user_id=%s LIMIT 1", (user_id,)).fetchone():
            return
        if not conn.execute("SELECT 1 FROM customer_profiles WHERE telegram_user_id=%s LIMIT 1", (user_id,)).fetchone():
            return
        conn.execute("""INSERT INTO customer_chat_history
            (telegram_user_id,message_id,direction,body) VALUES (%s,%s,%s,%s)
            ON CONFLICT (telegram_user_id,message_id,direction)
            DO UPDATE SET body=EXCLUDED.body""", (user_id,message_id,direction,body))


async def record_message(message, direction):
    if not message or not getattr(message, 'chat', None) or message.chat.type != 'private':
        return
    body = message.text or message.caption
    if message.contact:
        body = '📞 اشتراک شماره: ' + message.contact.phone_number
    if not body:
        body = '[پیام غیرمتنی]'
    try:
        await asyncio.to_thread(save_message, message.chat.id, message.message_id, direction, body)
    except Exception as exc:
        logging.error('Customer history write failed: %s', type(exc).__name__)


async def record_update(update, context):
    if (context.user_data or {}).get('login_step'):
        return
    message = update.message
    if message and not (message.text or '').startswith(('/login', '/logout')):
        await record_message(message, 'customer')
    query = update.callback_query
    if query and query.message and query.message.chat.type == 'private':
        markup = query.message.reply_markup
        labels = [b.text for row in (markup.inline_keyboard if markup else [])
                  for b in row if b.callback_data == query.data]
        if labels:
            try:
                await asyncio.to_thread(save_message, query.from_user.id,
                                        -update.update_id, 'customer', 'انتخاب: ' + labels[0])
            except Exception as exc:
                logging.error('Customer history choice failed: %s', type(exc).__name__)


class TranscriptBot(ExtBot):
    async def _send_message(self, *args, **kwargs):
        result = await super()._send_message(*args, **kwargs)
        if not isinstance(result, bool):
            await record_message(result, 'bot')
        return result

    async def send_media_group(self, *args, **kwargs):
        results = await super().send_media_group(*args, **kwargs)
        for message in results:
            await record_message(message, 'bot')
        return results


def history_page(user_id, offset=0):
    with db_connect() as conn:
        rows = conn.execute("""SELECT id,direction,body,recorded_at
            FROM customer_chat_history WHERE telegram_user_id=%s
            ORDER BY id LIMIT 9 OFFSET %s""", (user_id,offset)).fetchall()
    lines = ['💬 سابقهٔ گفتگو', 'پیام‌های ثبت‌شده از زمان فعال‌شدن این قابلیت؛ پیام‌های قدیمیِ ثبت‌نشده در دسترس نیستند.']
    for _, direction, body, created in rows[:8]:
        label = 'مشتری' if direction == 'customer' else 'بات / مشاور'
        stamp = created.astimezone(ZoneInfo('Asia/Tehran')).strftime('%Y-%m-%d %H:%M')
        lines.append(f'{label} — {stamp}\n{body}')
    if not rows:
        lines.append('هنوز گفتگویی ثبت نشده است.')
    # Send chunks without truncating any customer text.
    text = '\n\n'.join(lines)
    return [text[i:i+3500] for i in range(0,len(text),3500)], len(rows)>8
