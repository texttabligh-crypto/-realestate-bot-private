"""
Unit and integration tests for Bale Bot Phase 1 (bale_bot.py).
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

APP_PATH = Path(__file__).parent / "app"
sys.path.insert(0, str(APP_PATH))

import bale_bot


class TestBaleBotPhase1(unittest.TestCase):
    def setUp(self):
        self.bot_username = "nimat_bale_bot"

    def test_build_bale_visit_url(self):
        # With explicit username
        url = bale_bot.build_bale_visit_url(1234, bot_username="my_agency_bot")
        self.assertEqual(url, "https://ble.ir/my_agency_bot?start=visit_1234")

        # Strip leading @
        url2 = bale_bot.build_bale_visit_url(5678, bot_username="@test_bot")
        self.assertEqual(url2, "https://ble.ir/test_bot?start=visit_5678")

        # Fallback when no username
        with patch.object(bale_bot, "BALE_BOT_USERNAME", ""):
            url3 = bale_bot.build_bale_visit_url(999)
            self.assertEqual(url3, "https://ble.ir/?start=visit_999")

    def test_build_bale_visit_keyboard(self):
        kb = bale_bot.build_bale_visit_keyboard(4321, bot_username="bale_realestate_bot")
        self.assertIsNotNone(kb)
        self.assertEqual(len(kb.inline_keyboard), 1)
        button = kb.inline_keyboard[0][0]
        self.assertEqual(button.text, "🏠 بازدید از این واحد می‌خوام")
        self.assertEqual(button.url, "https://ble.ir/bale_realestate_bot?start=visit_4321")

    def test_create_bale_application_requires_token(self):
        with patch.object(bale_bot, "BALE_BOT_TOKEN", ""):
            with self.assertRaises(ValueError):
                bale_bot.create_bale_application("")

    def test_create_bale_application_structure(self):
        token = "999999999:AAABBBCCCDDD"
        app = bale_bot.create_bale_application(token=token)

        self.assertIsNotNone(app)
        # Check base_url
        self.assertTrue(app.bot.base_url.startswith("https://tapi.bale.ai/bot"))
        self.assertTrue(app.bot.base_file_url.startswith("https://tapi.bale.ai/file/bot"))

        # Verify handlers exist across registered groups
        all_handlers = []
        for group in app.handlers.values():
            all_handlers.extend(group)

        self.assertTrue(len(all_handlers) >= 9)

    def test_ensure_bale_schema_executes_ddl(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch("bale_bot.db_connect", return_value=mock_conn):
            bale_bot.ensure_bale_schema()

        self.assertTrue(mock_cur.execute.called)
        query = mock_cur.execute.call_args[0][0]
        self.assertIn("customer_profiles", query)
        self.assertIn("platform", query)
        self.assertIn("user_sessions", query)
        self.assertIn("channels", query)
        mock_conn.commit.assert_called_once()

    def test_bale_post_init(self):
        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_bot.username = "test_bale_bot"
        mock_bot.id = 12345
        mock_app.bot = mock_bot

        with patch("bale_bot.ensure_bale_schema") as mock_ensure:
            asyncio.run(bale_bot.bale_post_init(mock_app))
            self.assertTrue(mock_ensure.called)
            self.assertTrue(mock_bot.set_my_commands.called)
            self.assertTrue(mock_bot.set_chat_menu_button.called)

    def test_publish_to_bale_channel_no_channel(self):
        mock_bot = AsyncMock()
        with patch.object(bale_bot, "BALE_CHANNEL_ID", ""):
            with self.assertRaises(RuntimeError):
                asyncio.run(bale_bot.publish_to_bale_channel(mock_bot, 100, channel_id=""))

    def test_publish_to_bale_channel_with_photo(self):
        mock_bot = AsyncMock()
        mock_bot.send_photo = AsyncMock()

        mock_row = (
            10, "3197", "ولیعصر", "ولیعصر خیابان اول",
            120, 2, "طبقه ۲", 1400, True, True, True, "جنوبی",
            5000000000, 5.0, "#فروش", "ACTIVE", "raw text", None, None
        )
        mock_media = [("file_unique_123", "PHOTO", 0)]

        with patch("bale_bot.get_property_for_publish", return_value=mock_row), \
             patch("bale_bot.get_property_media", return_value=mock_media), \
             patch("bale_bot.build_publish_text", return_value="متن ملک کد ۳۱۹۷"):
            
            asyncio.run(
                bale_bot.publish_to_bale_channel(
                    mock_bot, 10, channel_id="-100999888", bot_username="bale_test"
                )
            )

            mock_bot.send_photo.assert_called_once()
            args, kwargs = mock_bot.send_photo.call_args
            self.assertEqual(kwargs["chat_id"], "-100999888")
            self.assertEqual(kwargs["photo"], "file_unique_123")
            self.assertIn("۳۱۹۷", kwargs["caption"])
            self.assertIsNotNone(kwargs["reply_markup"])

    def test_publish_to_bale_channel_fallback_to_text(self):
        mock_bot = AsyncMock()
        mock_bot.send_photo = AsyncMock(side_effect=Exception("Bale photo error"))
        mock_bot.send_message = AsyncMock()

        mock_row = (
            10, "3197", "ولیعصر", "ولیعصر خیابان اول",
            120, 2, "طبقه ۲", 1400, True, True, True, "جنوبی",
            5000000000, 5.0, "#فروش", "ACTIVE", "raw text", None, None
        )
        mock_media = [("file_unique_123", "PHOTO", 0)]

        with patch("bale_bot.get_property_for_publish", return_value=mock_row), \
             patch("bale_bot.get_property_media", return_value=mock_media), \
             patch("bale_bot.build_publish_text", return_value="متن ملک کد ۳۱۹۷"):

            asyncio.run(
                bale_bot.publish_to_bale_channel(
                    mock_bot, 10, channel_id="-100999888", bot_username="bale_test"
                )
            )

            mock_bot.send_photo.assert_called_once()
            mock_bot.send_message.assert_called_once()
            args, kwargs = mock_bot.send_message.call_args
            self.assertEqual(kwargs["chat_id"], "-100999888")
            self.assertEqual(kwargs["text"], "متن ملک کد ۳۱۹۷")

    def test_bale_transcript_bot_absorbs_menu_button(self):
        bot = bale_bot.BaleTranscriptBot("123:abc")
        res = asyncio.run(bot.set_chat_menu_button(chat_id=123))
        self.assertTrue(res)

    def test_bale_transcript_bot_post_adaptations(self):
        bot = bale_bot.BaleTranscriptBot("123:abc")
        captured = {}

        async def fake_super_post(endpoint, data, **kwargs):
            captured.update(data)
            return {"ok": True}

        from telegram import LinkPreviewOptions, ReplyParameters

        with patch("customer_transcript.TranscriptBot._do_post", side_effect=fake_super_post):
            data = {
                "chat_id": 1234,
                "text": "test message",
                "reply_parameters": ReplyParameters(message_id=55),
                "link_preview_options": LinkPreviewOptions(is_disabled=True),
                "business_connection_id": "none",
                "message_effect_id": "none",
            }
            asyncio.run(bot._do_post("sendMessage", data))

            self.assertEqual(captured["chat_id"], 1234)
            self.assertEqual(captured["text"], "test message")
            self.assertEqual(captured["reply_to_message_id"], 55)
            self.assertTrue(captured.get("disable_web_page_preview"))
            self.assertNotIn("reply_parameters", captured)
            self.assertNotIn("link_preview_options", captured)
            self.assertNotIn("business_connection_id", captured)


if __name__ == "__main__":
    unittest.main()
