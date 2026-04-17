"""Input sanitization utilities for user-supplied strings.

Used to prevent log injection (newline/control-char smuggling in text logs),
information leakage in error messages, and overly long values in responses.
"""

from __future__ import annotations

import re

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_log(value: str, max_length: int = 200) -> str:
    """Strip control characters and truncate for safe log output.

    Replaces newlines and tabs with visible placeholders so a single user
    string cannot forge multi-line log entries in text-mode logging.
    """
    s = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    s = _CONTROL_CHAR_RE.sub("", s)
    if len(s) > max_length:
        s = s[:max_length] + "…"
    return s


def sanitize_for_error(value: str, max_length: int = 120) -> str:
    """Prepare a user-supplied value for inclusion in an error message.

    Strips control characters, truncates, and wraps in single quotes so the
    value boundary is unambiguous.
    """
    s = _CONTROL_CHAR_RE.sub("", value)
    s = s.replace("\n", " ").replace("\r", " ")
    if len(s) > max_length:
        s = s[:max_length] + "…"
    return f"'{s}'"
