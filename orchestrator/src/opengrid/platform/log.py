"""JSON structured logging for every process (02b S1.1 platform/log.py).

One line of JSON per log record on stdout, captured by systemd/journald. Never logs secret values
(BUILD.md safety rule) -- callers pass env-var *names*, not resolved secrets, in `extra`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED_LOG_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    """Renders each LogRecord as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED_LOG_RECORD_KEYS}
        payload.update(extra)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(process_name: str, *, level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once per process. Idempotent: calling twice does not duplicate handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    return logging.getLogger(process_name)
