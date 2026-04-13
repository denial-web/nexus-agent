"""Structured JSON logging and request correlation ID support."""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

from app.config import settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get("-"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter that includes request_id when present."""

    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get("-")
        record.request_id = rid
        return super().format(record)


def configure_logging() -> None:
    """Set up logging based on environment.

    - production/staging: JSON to stdout (machine-parseable)
    - development/test: human-readable text
    """
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    env = settings.ENVIRONMENT.lower()
    if env in ("development", "dev", "test"):
        handler.setFormatter(
            TextFormatter(
                fmt="%(asctime)s %(levelname)-8s [%(name)s] [%(request_id)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    else:
        handler.setFormatter(JSONFormatter())

    root.addHandler(handler)
