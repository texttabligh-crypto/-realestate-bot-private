import asyncio
import logging
import os

from telegram import Bot
from telegram.error import RetryAfter

from publisher import (
    build_publish_text,
    db_connect,
    get_destination_channel,
    get_property_for_publish,
    get_property_media,
)
from telegram_delivery import edit_published_message


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)


def manual_rows():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, published_message_id
                FROM properties
                WHERE source_kind = 'MANUAL'
                  AND published_message_id IS NOT NULL
                  AND deleted_from_channel_at IS NULL
                  AND NULLIF(BTRIM(district), '') IS NOT NULL
                  AND NULLIF(BTRIM(address_text), '') IS NOT NULL
                ORDER BY id
                """
            )
            return cur.fetchall()


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    destination = get_destination_channel()
    if not destination:
        raise RuntimeError("Active destination channel not found")

    chat_id = destination[0]
    rows = manual_rows()
    logging.info("MANUAL REGION BACKFILL candidates=%s", len(rows))

    updated = 0
    skipped = 0
    failed = 0

    async with Bot(token=token) as bot:
        for property_id, message_id in rows:
            row = get_property_for_publish(property_id)
            media = get_property_media(property_id)
            text = build_publish_text(row)
            has_photo = any(item[1] == "PHOTO" for item in media)

            for attempt in range(2):
                try:
                    await edit_published_message(
                        bot,
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        has_photo=has_photo,
                    )
                    updated += 1
                    break
                except RetryAfter as exc:
                    if attempt:
                        failed += 1
                        logging.error(
                            "BACKFILL flood retry exhausted property_id=%s",
                            property_id,
                        )
                        break
                    delay = getattr(exc, "retry_after", 5)
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    await asyncio.sleep(float(delay) + 2.0)
                except Exception as exc:
                    if "message is not modified" in str(exc).lower():
                        skipped += 1
                    else:
                        failed += 1
                        logging.error(
                            "BACKFILL failed property_id=%s message_id=%s error=%s",
                            property_id,
                            message_id,
                            exc,
                        )
                    break

            await asyncio.sleep(3.0)

    print(
        "✅ MANUAL_REGION_BACKFILL_DONE "
        f"candidates={len(rows)} updated={updated} "
        f"unchanged={skipped} failed={failed}"
    )
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
