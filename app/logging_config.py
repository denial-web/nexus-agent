"""Structured JSON logging with request correlation and OTel trace context."""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

from app.config import settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def _trace_context() -> dict[str, str]:
    """Get current OTel trace_id/span_id without importing at module level."""
    try:
        from app.tracing import get_current_trace_context

        return get_current_trace_context()
    except Exception:
        return {"trace_id": "-", "span_id": "-"}


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects with trace context."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _trace_context()
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get("-"),
            "trace_id": ctx["trace_id"],
            "span_id": ctx["span_id"],
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter with request_id and trace context."""

    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get("-")
        ctx = _trace_context()
        record.request_id = rid
        record.trace_id = ctx["trace_id"]
        record.span_id = ctx["span_id"]
        return super().format(record)


def configure_logging() -> None:
    """Set up logging based on environment.

    - production/staging: JSON to stdout (machine-parseable, includes trace_id/span_id)
    - development/test: human-readable text with trace context when active
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
