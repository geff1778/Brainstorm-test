"""Structured JSON logging with automatic secret redaction.

Two guarantees matter here:

1. **Structured** — every record is a single JSON object so logs can be shipped
   and queried without a parser.
2. **Redacted** — known secret values and secret-looking patterns are masked
   before a record is emitted. Logging a token by accident is the most common
   way secrets leak, so we defend at the sink.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

_SECRET_ENV_VARS = (
    "SESSION_API_KEY",
    "GITHUB_TOKEN",
    "OPENHANDS_API_KEY",
    "OPENHANDS_CLOUD_API_KEY",
    "OH_SECRET_KEY",
)

# Patterns that look like credentials even when we do not know the value.
_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghu_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
)


def _secret_values() -> list[str]:
    out: list[str] = []
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if value and len(value) >= 8:
            out.append(value)
    return out


def redact(text: str) -> str:
    """Mask known secret values and credential-looking substrings."""
    if not text:
        return text
    for value in _secret_values():
        text = text.replace(value, "***REDACTED***")
    for pattern in _PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    return text


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with redacted messages."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("task_id", "conversation_id", "actor", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact(str(value))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        if getattr(handler, "_ohp", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._ohp = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
