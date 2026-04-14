"""MCP backend registry — JSON file on disk."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class McpBackend:
    """One MCP server the proxy can forward to."""

    name: str
    url: str = ""
    transport: str = "streamable_http"  # streamable_http | sse | stdio
    command: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpBackend:
        return cls(
            name=str(data["name"]),
            url=str(data.get("url") or ""),
            transport=str(data.get("transport") or "streamable_http"),
            command=data.get("command"),
            env=dict(data.get("env") or {}),
            enabled=bool(data.get("enabled", True)),
        )


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def load_backends(file_path: str) -> list[McpBackend]:
    """Load backends from JSON. Missing file → empty list."""
    path = _resolve_path(file_path)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read MCP backends file %s", path, exc_info=True)
        return []
    if not isinstance(raw, list):
        return []
    out: list[McpBackend] = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            try:
                out.append(McpBackend.from_dict(item))
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid MCP backend entry: %s", item)
    return out


def save_backends(file_path: str, backends: list[McpBackend]) -> None:
    """Atomically write backends JSON."""
    path = _resolve_path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [b.to_dict() for b in backends]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
