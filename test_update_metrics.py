"""The timing processor must keep one-at-a-time handling and only measure."""
import asyncio
import datetime
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

try:
    from telegram import CallbackQuery, Chat, Message, Update, User
    import update_metrics
except ImportError:  # python-telegram-bot is only installed in the bot image
    update_metrics = None


def message_update(update_id, text):
    user = User(1, "u", False)
    chat = Chat(1, "private")
    sent = datetime.datetime.now(datetime.timezone.utc)
    return Update(update_id, message=Message(update_id, sent, chat, from_user=user, text=text))


def callback_update(update_id, data):
    user = User(1, "u", False)
    return Update(update_id, callback_query=CallbackQuery(str(update_id), user, "c", data=data))


@unittest.skipIf(update_metrics is None, "python-telegram-bot not installed")
class TimedUpdateProcessorTest(unittest.TestCase):
    def test_labels_hide_content_and_ids(self):
        kind = update_metrics.update_kind
        self.assertEqual(kind(callback_update(1, "crm_support_phone_23")), "callback:crm_support_phone")
        self.assertEqual(kind(callback_update(2, "crm_support_history_23_8")), "callback:crm_support_history")
        self.assertEqual(kind(message_update(3, "/start visit_350")), "command:/start")
        self.assertEqual(kind(message_update(4, "آپارتمان ولیعصر ۱۰ میلیارد")), "message:text")

    def test_updates_stay_sequential_and_are_reported(self):
        events = []
        probes = []

        class Probe:
            def __enter__(self):
                probes.append(1)
                return self
            def __exit__(self, *args): pass
            def cursor(self): return self
            def execute(self, sql): pass
            def fetchone(self): return (1,)

        async def handle(name):
            events.append(("start", name))
            await asyncio.sleep(0.05)
            events.append(("end", name))

        async def run():
            processor = update_metrics.TimedUpdateProcessor(1, db_probe=Probe)
            await processor.initialize()
            update_metrics.REPORT_INTERVAL_SECONDS = 0
            tasks = [
                asyncio.create_task(processor.process_update(message_update(i, f"m{i}"), handle(i)))
                for i in range(3)
            ]
            await asyncio.gather(*tasks)

        with self.assertLogs(level=logging.INFO) as logs:
            asyncio.run(run())

        self.assertEqual(events, [(s, i) for i in range(3) for s in ("start", "end")])
        self.assertTrue(any("UPDATE_TIMING" in line and "message:text" in line for line in logs.output))
        self.assertTrue(probes)
        self.assertFalse(any("m1" in line for line in logs.output))

    def test_errors_still_propagate(self):
        async def boom():
            raise ValueError("handler failed")

        async def run():
            processor = update_metrics.TimedUpdateProcessor(1)
            await processor.process_update(message_update(9, "x"), boom())

        with self.assertRaises(ValueError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
