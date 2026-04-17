"""Idempotency key store — prevents duplicate processing of retried requests.

Clients send an ``Idempotency-Key`` header on mutating endpoints (POST).
The first request with a given key is processed normally and the response
is cached. Subsequent requests with the same key return the cached response
without re-executing the pipeline.

Two backends:
    - InProcessStore: thread-safe dict with TTL eviction (single-worker)
    - RedisStore: shared across workers when REDIS_URL is set
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_store: IdempotencyStore | None = None
_store_lock = threading.Lock()


@dataclass
class CachedResponse:
    status_code: int
    body: bytes
    content_type: str
    created_at: float


class IdempotencyStore:
    def get(self, key: str) -> CachedResponse | None:
        raise NotImplementedError

    def set(self, key: str, response: CachedResponse) -> None:
        raise NotImplementedError

    def acquire_inflight(self, key: str) -> bool:
        """Try to mark a key as in-flight. Returns True if acquired."""
        raise NotImplementedError

    def release_inflight(self, key: str) -> None:
        """Release the in-flight lock for a key."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    @property
    def backend_type(self) -> str:
        raise NotImplementedError


class InProcessStore(IdempotencyStore):
    """Thread-safe in-process LRU store with TTL eviction."""

    def __init__(self, max_keys: int = 10000, ttl_seconds: int = 86400) -> None:
        self._cache: OrderedDict[str, CachedResponse] = OrderedDict()
        self._max_keys = max_keys
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    def get(self, key: str) -> CachedResponse | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() - entry.created_at > self._ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return entry

    def set(self, key: str, response: CachedResponse) -> None:
        with self._lock:
            self._cache[key] = response
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_keys:
                self._cache.popitem(last=False)

    def acquire_inflight(self, key: str) -> bool:
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def release_inflight(self, key: str) -> None:
        with self._lock:
            self._inflight.discard(key)

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()
            self._inflight.clear()

    @property
    def backend_type(self) -> str:
        return "in_process"

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)


class RedisStore(IdempotencyStore):
    """Redis-backed idempotency store, shared across workers."""

    _KEY_PREFIX = "nexus:idempotency:"
    _INFLIGHT_PREFIX = "nexus:idempotency:inflight:"
    _INFLIGHT_TTL = 300  # 5 min auto-expire in case of crash

    def __init__(self, redis_url: str, ttl_seconds: int = 86400) -> None:
        self._ttl = ttl_seconds
        self._client: Any = None
        try:
            import redis

            self._client = redis.from_url(
                redis_url,
                decode_responses=False,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self._client.ping()
            logger.info("Redis idempotency store connected")
        except Exception:
            logger.warning(
                "Redis idempotency store connection failed; using in-process fallback",
                exc_info=True,
            )
            self._client = None

    def get(self, key: str) -> CachedResponse | None:
        if self._client is None:
            return None
        try:
            data = self._client.get(f"{self._KEY_PREFIX}{key}")
            if data is None:
                return None
            parsed = json.loads(data)
            return CachedResponse(
                status_code=parsed["status_code"],
                body=parsed["body"].encode("utf-8"),
                content_type=parsed["content_type"],
                created_at=parsed["created_at"],
            )
        except Exception:
            logger.warning("Redis idempotency get failed for %s", key, exc_info=True)
            return None

    def set(self, key: str, response: CachedResponse) -> None:
        if self._client is None:
            return
        try:
            data = json.dumps(
                {
                    "status_code": response.status_code,
                    "body": response.body.decode("utf-8"),
                    "content_type": response.content_type,
                    "created_at": response.created_at,
                }
            )
            self._client.setex(f"{self._KEY_PREFIX}{key}", self._ttl, data)
        except Exception:
            logger.warning("Redis idempotency set failed for %s", key, exc_info=True)

    def acquire_inflight(self, key: str) -> bool:
        if self._client is None:
            return True
        try:
            result = self._client.set(
                f"{self._INFLIGHT_PREFIX}{key}",
                b"1",
                nx=True,
                ex=self._INFLIGHT_TTL,
            )
            return result is not None
        except Exception:
            logger.warning("Redis inflight acquire failed for %s", key, exc_info=True)
            return True

    def release_inflight(self, key: str) -> None:
        if self._client is None:
            return
        try:
            self._client.delete(f"{self._INFLIGHT_PREFIX}{key}")
        except Exception:
            logger.warning("Redis inflight release failed for %s", key, exc_info=True)

    def reset(self) -> None:
        if self._client is None:
            return
        try:
            for prefix in (self._KEY_PREFIX, self._INFLIGHT_PREFIX):
                cursor: int = 0
                while True:
                    cursor, keys = self._client.scan(
                        cursor,
                        match=f"{prefix}*",
                        count=100,
                    )
                    if keys:
                        self._client.delete(*keys)
                    if cursor == 0:
                        break
        except Exception:
            logger.warning("Redis idempotency reset failed", exc_info=True)

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


def get_store() -> IdempotencyStore:
    """Get or create the idempotency store singleton."""
    global _store
    if _store is not None:
        return _store

    with _store_lock:
        if _store is not None:
            return _store

        from app.config import settings

        ttl = settings.IDEMPOTENCY_TTL
        max_keys = settings.IDEMPOTENCY_MAX_KEYS

        if settings.REDIS_URL.strip():
            try:
                rs = RedisStore(settings.REDIS_URL, ttl_seconds=ttl)
                if rs.connected:
                    _store = rs
                    return _store
            except Exception:
                logger.warning("Redis idempotency init failed; using in-process")

        _store = InProcessStore(max_keys=max_keys, ttl_seconds=ttl)
        return _store


def reset_store() -> None:
    """Reset the singleton (for testing)."""
    global _store
    if _store is not None:
        _store.reset()
    _store = None
