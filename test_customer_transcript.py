import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).parent / 'app'))
import customer_transcript as t
from telegram.request import HTTPXRequest


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_and_login_exclusion(self):
        msg = SimpleNamespace(chat=SimpleNamespace(type='private', id=12), message_id=5,
                              text='بودجه ۸ میلیارد', caption=None, contact=None)
        with patch.object(t, 'save_message') as save:
            await t.record_message(msg, 'customer')
            self.assertEqual(save.call_args.args[:4], (12,5,'customer','بودجه ۸ میلیارد'))
            await t.record_update(SimpleNamespace(message=msg), SimpleNamespace(user_data={'login_step':'password'}))
            self.assertEqual(save.call_count, 1)
            msg.chat.type = 'channel'
            await t.record_message(msg, 'customer')
            self.assertEqual(save.call_count, 1)

    async def test_outgoing_ptb_hook_and_inline_edit(self):
        req = HTTPXRequest(httpx_kwargs={'trust_env':False})
        bot = t.TranscriptBot('123:ABC',request=req,get_updates_request=req)
        msg = object()
        with patch('telegram.ext.ExtBot._send_message', new=AsyncMock(return_value=msg)), patch.object(t,'record_message',new=AsyncMock()) as record:
            self.assertIs(await bot._send_message('sendMessage', {}), msg)
            record.assert_awaited_once_with(msg, 'bot')
        with patch('telegram.ext.ExtBot._send_message', new=AsyncMock(return_value=True)), patch.object(t,'record_message',new=AsyncMock()) as record:
            self.assertTrue(await bot._send_message('editMessageText', {}))
            record.assert_not_awaited()
        await req.shutdown()

    def test_staff_not_recorded(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        with patch.object(t,'db_connect') as db:
            db.return_value.__enter__.return_value = conn
            t.save_message(12,5,'customer','secret')
            self.assertEqual(conn.execute.call_count, 1)

    def test_long_history_not_truncated(self):
        now = datetime.now(timezone.utc)
        body = 'پاسیو حذف / خواب‌ها پنجره دارند ' * 500
        rows = [(i,'customer',body,now) for i in range(9)]
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = rows
        with patch.object(t,'db_connect') as db:
            db.return_value.__enter__.return_value = conn
            chunks, more = t.history_page(12)
        self.assertTrue(more)
        self.assertTrue(all(len(c)<=3500 for c in chunks))
        self.assertEqual(''.join(chunks).count(body), 8)


if __name__ == '__main__':
    unittest.main()
