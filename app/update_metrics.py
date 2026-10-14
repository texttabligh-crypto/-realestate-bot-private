"""Measure how long each Telegram update takes, without changing behavior.

With max_concurrent_updates=1 updates are still handled strictly one at a
time, exactly as before; the processor only times them and periodically
logs a summary so slow paths can be found from data instead of guesses.
No message text or user identifiers are logged.
"""
import asyncio
import logging
import re
import time
from collections import defaultdict

from telegram.ext import BaseUpdateProcessor


SLOW_UPDATE_SECONDS = 3.0
REPORT_INTERVAL_SECONDS = 600
_MAX_SAMPLES_PER_KIND = 500


def update_kind(update):
    """A coarse, privacy-safe label for what kind of update this was."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        data = str(query.data or "")
        # Drop trailing ids so «crm_support_phone_23» groups with its siblings.
        data = re.sub(r"(_-?\d+)+$", "", data)
        return f"callback:{data[:40] or '-'}"

    message = getattr(update, "message", None)
    if message is not None:
        text = message.text or ""
        if text.startswith("/"):
            return f"command:{text.split()[0].split('@')[0][:30]}"
        if message.contact is not None:
            return "message:contact"
        if message.photo:
            return "message:photo"
        if text:
            return "message:text"
        return "message:other"

    if getattr(update, "channel_post", None) is not None:
        return "channel_post"
    if getattr(update, "edited_channel_post", None) is not None:
        return "edited_channel_post"
    return "other"


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


class TimedUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates=1, db_probe=None):
        super().__init__(max_concurrent_updates)
        self._db_probe = db_probe
        self._durations = defaultdict(list)
        self._lags = []
        self._window_started = time.monotonic()

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        started = time.monotonic()
        try:
            await coroutine
        finally:
            elapsed = time.monotonic() - started
            kind = update_kind(update)
            samples = self._durations[kind]
            if len(samples) < _MAX_SAMPLES_PER_KIND:
                samples.append(elapsed)

            # Telegram stamps messages when sent, so this shows how long the
            # user waited in total, including time queued behind others.
            message = getattr(update, "effective_message", None)
            if getattr(update, "message", None) is not None and message.date:
                lag = time.time() - message.date.timestamp()
                if 0 <= lag < 3600 and len(self._lags) < _MAX_SAMPLES_PER_KIND:
                    self._lags.append(lag)

            if elapsed >= SLOW_UPDATE_SECONDS:
                logging.warning("UPDATE_SLOW kind=%s seconds=%.2f", kind, elapsed)

            if time.monotonic() - self._window_started >= REPORT_INTERVAL_SECONDS:
                await self._report()

    async def _report(self):
        durations, lags = self._durations, self._lags
        self._durations, self._lags = defaultdict(list), []
        window = time.monotonic() - self._window_started
        self._window_started = time.monotonic()

        everything = [value for values in durations.values() for value in values]
        if not everything:
            return

        busiest = sorted(durations.items(), key=lambda item: sum(item[1]), reverse=True)[:8]
        kinds = " ".join(
            f"{kind}:n={len(values)},avg={sum(values) / len(values) * 1000:.0f}ms,max={max(values) * 1000:.0f}ms"
            for kind, values in busiest
        )
        lag_text = (
            f"lag_avg={sum(lags) / len(lags):.1f}s lag_max={max(lags):.1f}s"
            if lags else "lag_avg=- lag_max=-"
        )

        db_text = "db_connect=-"
        if self._db_probe is not None:
            try:
                db_started = time.monotonic()
                await asyncio.to_thread(self._probe_once)
                db_text = f"db_connect={(time.monotonic() - db_started) * 1000:.0f}ms"
            except Exception as exc:
                db_text = f"db_connect=error:{type(exc).__name__}"

        logging.info(
            "UPDATE_TIMING window=%.0fs total=%s avg=%.0fms p95=%.0fms max=%.0fms %s %s busiest=[%s]",
            window,
            len(everything),
            sum(everything) / len(everything) * 1000,
            _percentile(everything, 0.95) * 1000,
            max(everything) * 1000,
            lag_text,
            db_text,
            kinds,
        )

    def _probe_once(self):
        with self._db_probe() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
