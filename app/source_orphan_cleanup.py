import asyncio
import logging
import os

from telegram import Bot
from telegram.error import RetryAfter

import bot as realestate


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)


def candidates():
    with realestate.db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.unit_code, p.published_message_id
                FROM properties p
                JOIN property_sources ps ON ps.property_id = p.id
                WHERE p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND p.status = 'ACTIVE'
                  AND COALESCE(p.source_kind, '') NOT IN ('MANUAL', 'CUSTOMER')
                GROUP BY p.id
                HAVING COUNT(ps.id) FILTER (WHERE ps.is_active = TRUE) = 0
                ORDER BY p.id
                """
            )
            return cur.fetchall()


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    rows = candidates()
    print(f"SOURCE_ORPHAN_CLEANUP candidates={len(rows)}")
    deleted = 0
    failed = 0
    manual_required = 0

    async with Bot(token=token) as telegram_bot:
        for property_id, unit_code, message_id in rows:
            try:
                await realestate.force_delete_property(telegram_bot, property_id)
                deleted += 1
                print(
                    "SOURCE_ORPHAN_DELETED "
                    f"property_id={property_id} unit_code={unit_code} message_id={message_id}"
                )
            except RetryAfter as exc:
                failed += 1
                logging.warning("Cleanup stopped by Telegram flood control: %s", exc)
                break
            except Exception as exc:
                if "message can't be deleted" in str(exc).lower():
                    manual_required += 1
                    print(
                        "SOURCE_ORPHAN_MANUAL_DELETE_REQUIRED "
                        f"property_id={property_id} unit_code={unit_code} "
                        f"message_id={message_id}"
                    )
                else:
                    failed += 1
                    logging.exception("Cleanup failed property_id=%s", property_id)

            await asyncio.sleep(5.0)

        if deleted:
            # Let the debounced targeted category task complete before exit.
            await asyncio.sleep(65.0)

    print(
        f"✅ SOURCE_ORPHAN_CLEANUP_DONE candidates={len(rows)} "
        f"deleted={deleted} manual_required={manual_required} failed={failed}"
    )
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
