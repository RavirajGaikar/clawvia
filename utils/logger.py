import logging
import re
import sys
import os

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False

# Patterns to redact from log output
_REDACT_PATTERNS = [
    # API keys (nvapi-..., sk-..., etc.)
    (re.compile(r"(nvapi-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]+"), r"\1***REDACTED***"),
    (re.compile(r"(sk-[A-Za-z0-9]{5})[A-Za-z0-9]+"), r"\1***REDACTED***"),
    # Telegram bot tokens
    (re.compile(r"(\d{8,12}:[A-Za-z0-9_-]{10})[A-Za-z0-9_-]+"), r"\1***REDACTED***"),
    # Bearer tokens
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{10,}"), r"\1***REDACTED***"),
    # Generic long secrets (40+ char hex/alphanum strings that look like tokens)
    (re.compile(r"(['\"]?(?:key|token|secret|password|api_key)['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_-]{20,}"),
     r"\1***REDACTED***"),
]


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive data from log messages."""

    def format(self, record):
        message = super().format(record)
        for pattern, replacement in _REDACT_PATTERNS:
            message = pattern.sub(replacement, message)
        return message


def setup_logging(level=None):
    global _initialized
    if _initialized:
        return
    _initialized = True

    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO").upper()

    numeric_level = getattr(logging, level, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name):
    setup_logging()
    return logging.getLogger(name)


def redact(text):
    """Redact sensitive data from a string (for use before logging args/results)."""
    if not text:
        return text
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
