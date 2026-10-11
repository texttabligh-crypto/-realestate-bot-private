import logging
import re


TOKEN_PATTERNS = (
    re.compile(r"(https?://api\.telegram\.org/bot)[^/\s]+", re.IGNORECASE),
    re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]+\b", re.IGNORECASE),
)


class TelegramTokenRedactionFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        for pattern in TOKEN_PATTERNS:
            message = pattern.sub(r"\1<redacted>" if pattern.groups else "bot<redacted>", message)
        record.msg = message
        record.args = ()
        return True


def secure_telegram_logging():
    """Prevent HTTP request URLs and Telegram bot tokens reaching logs."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    redaction_filter = TelegramTokenRedactionFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(redaction_filter)
    for handler in root_logger.handlers:
        handler.addFilter(redaction_filter)
