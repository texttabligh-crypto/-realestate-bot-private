"""Durable customer-only text history, visible through the CRM permission gate."""
import asyncio
import logging
import re
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


def save_message(user_id, message_id, direction, body, event_time=None):
    if not body or user_id <= 0:
        return
    with db_connect() as conn:
        # Never retain staff conversations or login credentials.
        if conn.execute("SELECT 1 FROM user_sessions WHERE telegram_user_id=%s LIMIT 1", (user_id,)).fetchone():
            return
        if not conn.execute("SELECT 1 FROM customer_profiles WHERE telegram_user_id=%s LIMIT 1", (user_id,)).fetchone():
            return
        conn.execute("""INSERT INTO customer_chat_history
            (telegram_user_id,message_id,direction,body,recorded_at) VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()))
            ON CONFLICT (telegram_user_id,message_id,direction)
            DO UPDATE SET body=EXCLUDED.body""", (user_id,message_id,direction,body,event_time))


async def record_message(message, direction):
    if not message or not getattr(message, 'chat', None) or message.chat.type != 'private':
        return
    body = message.text or message.caption
    if message.contact:
        body = '📞 اشتراک شماره: ' + message.contact.phone_number
    if not body:
        body = '[پیام غیرمتنی]'
    try:
        await asyncio.to_thread(save_message, message.chat.id, message.message_id, direction, body, getattr(message, 'date', None))
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


# A visit link opens the bot with «/start visit_<property id>»; the id is the
# bot's internal number, so show staff the unit code from the channel instead.
_VISIT_START = re.compile(r'^/start\s+visit_(\d+)\s*$')


def _visit_unit_codes(conn, rows):
    ids = sorted({int(m.group(1)) for _, _, body, _ in rows if (m := _VISIT_START.match(body or ''))})
    if not ids:
        return {}
    found = conn.execute('SELECT id, unit_code FROM properties WHERE id = ANY(%s)', (ids,)).fetchall()
    return {int(pid): code for pid, code in found if code}


def _readable_body(body, unit_codes):
    match = _VISIT_START.match(body or '')
    if not match:
        return body
    code = unit_codes.get(int(match.group(1)))
    if code:
        return f'🏠 درخواست بازدید واحد {code}'
    return f'🏠 درخواست بازدید (شمارهٔ داخلی فایل: {match.group(1)})'


def history_page(user_id, offset=0):
    with db_connect() as conn:
        rows = conn.execute("""SELECT id,direction,body,recorded_at
            FROM customer_chat_history WHERE telegram_user_id=%s
            ORDER BY recorded_at,message_id,id LIMIT 9 OFFSET %s""", (user_id,offset)).fetchall()
        unit_codes = _visit_unit_codes(conn, rows[:8])
    lines = ['💬 سابقهٔ گفتگو', 'پیام‌های ثبت‌شده از زمان فعال‌شدن این قابلیت؛ پیام‌های قدیمیِ ثبت‌نشده در دسترس نیستند.']
    for _, direction, body, created in rows[:8]:
        label = 'مشتری' if direction == 'customer' else 'بات / مشاور'
        stamp = created.astimezone(ZoneInfo('Asia/Tehran')).strftime('%Y-%m-%d %H:%M')
        lines.append(f'{label} — {stamp}\n{_readable_body(body, unit_codes)}')
    if not rows:
        lines.append('هنوز گفتگویی ثبت نشده است.')
    # Send chunks without truncating any customer text.
    text = '\n\n'.join(lines)
    return [text[i:i+3500] for i in range(0,len(text),3500)], len(rows)>8
