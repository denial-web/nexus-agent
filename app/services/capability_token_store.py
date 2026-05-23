"""Capability token persistence — shared across workers when REDIS_URL is set."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

_store: CapabilityTokenStore | None = None
_store_lock = threading.Lock()


@dataclass
class StoredCapabilityToken:
    token_id: str
    trace_id: str
    action_type: str
    scope: dict
    issued_at: str
    expires_at: str
    signature: str
    used: bool = False


class CapabilityTokenStore:
    def put(self, token: StoredCapabilityToken, ttl_seconds: int) -> None:
        raise NotImplementedError

    def pop(self, token_id: str) -> StoredCapabilityToken | None:
        """Atomically remove and return a token, if present."""
        raise NotImplementedError

    def peek(self, token_id: str) -> StoredCapabilityToken | None:
        """Return a token without removing it (tests / diagnostics only)."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    @property
    def backend_type(self) -> str:
        raise NotImplementedError


class InProcessCapabilityTokenStore(CapabilityTokenStore):
    def __init__(self, max_tokens: int = 10_000) -> None:
        self._tokens: dict[str, StoredCapabilityToken] = {}
        self._max_tokens = max_tokens
        self._lock = threading.Lock()

    def put(self, token: StoredCapabilityToken, ttl_seconds: int) -> None:
        with self._lock:
            if len(self._tokens) >= self._max_tokens:
                self._evict_stale_locked()
            self._tokens[token.token_id] = token

    def pop(self, token_id: str) -> StoredCapabilityToken | None:
        with self._lock:
            return self._tokens.pop(token_id, None)

    def peek(self, token_id: str) -> StoredCapabilityToken | None:
        with self._lock:
            return self._tokens.get(token_id)

    def reset(self) -> None:
        with self._lock:
            self._tokens.clear()

    @property
    def backend_type(self) -> str:
        return "in_process"

    def _evict_stale_locked(self) -> None:
        now = time.time()
        stale: list[str] = []
        for token_id, token in self._tokens.items():
            if token.used:
                stale.append(token_id)
                continue
            try:
                expires = _parse_iso(token.expires_at)
            except ValueError:
                stale.append(token_id)
                continue
            if expires < now:
                stale.append(token_id)
        for token_id in stale:
            del self._tokens[token_id]


class RedisCapabilityTokenStore(CapabilityTokenStore):
    _KEY_PREFIX = "nexus:capability_token:"

    _POP_SCRIPT = """
local data = redis.call('GET', KEYS[1])
if not data then
  return nil
end
redis.call('DEL', KEYS[1])
return data
"""

    def __init__(self, redis_url: str) -> None:
        self._client: Any = None
        self._pop_script: Any = None
        try:
            import redis

            self._client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self._client.ping()
            self._pop_script = self._client.register_script(self._POP_SCRIPT)
            logger.info("Redis capability token store connected")
        except Exception:
            logger.warning(
                "Redis capability token store connection failed; using in-process fallback",
                exc_info=True,
            )
            self._client = None
            self._pop_script = None

    def put(self, token: StoredCapabilityToken, ttl_seconds: int) -> None:
        if self._client is None:
            return
        ttl = max(int(ttl_seconds), 1)
        try:
            self._client.setex(
                f"{self._KEY_PREFIX}{token.token_id}",
                ttl,
                json.dumps(asdict(token), separators=(",", ":"), sort_keys=True),
            )
        except Exception:
            logger.warning("Redis capability token put failed for %s", token.token_id, exc_info=True)

    def pop(self, token_id: str) -> StoredCapabilityToken | None:
        if self._client is None or self._pop_script is None:
            return None
        try:
            raw = self._pop_script(keys=[f"{self._KEY_PREFIX}{token_id}"])
            if not raw:
                return None
            parsed = json.loads(raw)
            return StoredCapabilityToken(**parsed)
        except Exception:
            logger.warning("Redis capability token pop failed for %s", token_id, exc_info=True)
            return None

    def peek(self, token_id: str) -> StoredCapabilityToken | None:
        if self._client is None:
            return None
        try:
            raw = self._client.get(f"{self._KEY_PREFIX}{token_id}")
            if not raw:
                return None
            parsed = json.loads(raw)
            return StoredCapabilityToken(**parsed)
        except Exception:
            logger.warning("Redis capability token peek failed for %s", token_id, exc_info=True)
            return None

    def reset(self) -> None:
        if self._client is None:
            return
        try:
            cursor: int = 0
            while True:
                cursor, keys = self._client.scan(cursor, match=f"{self._KEY_PREFIX}*", count=100)
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            logger.warning("Redis capability token reset failed", exc_info=True)

    @property
    def backend_type(self) -> str:
        return "redis" if self._client is not None else "redis_disconnected"

    @property
    def connected(self) -> bool:
        if self._client is None:
            return False
        try:
            self._client.ping()
            return True
        except Exception:
            return False


def _parse_iso(value: str) -> float:
    from datetime import UTC, datetime

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def get_token_store() -> CapabilityTokenStore:
    global _store
    if _store is not None:
        return _store

    with _store_lock:
        if _store is not None:
            return _store

        from app.config import settings

        if settings.REDIS_URL.strip():
            try:
                rs = RedisCapabilityTokenStore(settings.REDIS_URL)
                if rs.connected:
                    _store = rs
                    return _store
            except Exception:
                logger.warning("Redis capability token init failed; using in-process")

        _store = InProcessCapabilityTokenStore()
        return _store


def reset_token_store() -> None:
    global _store
    if _store is not None:
        _store.reset()
    _store = None
