"""
Rate limiter backends: in-process (default) and Redis (multi-worker).

When REDIS_URL is configured, the Redis backend uses a sliding-window log
(sorted set + Lua script) that is shared across all workers/instances.
Falls back to in-process memory when Redis is unavailable or unconfigured.

Both backends return a ``RateLimitResult`` with ``allowed``, ``remaining``
count, and ``retry_after`` seconds so callers can set standard headers.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_backend: RateLimiterBackend | None = None


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int
    limit: int


class RateLimiterBackend(ABC):
    @abstractmethod
    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Check rate limit and return result with remaining quota."""

    def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        """Legacy convenience — delegates to check()."""
        return self.check(key, limit, window_seconds).allowed

    @abstractmethod
    def reset(self) -> None:
        """Clear all rate limit state."""

    @property
    @abstractmethod
    def backend_type(self) -> str:
        """Return 'in_process' or 'redis'."""


class InProcessBackend(RateLimiterBackend):
    """Sliding-window rate limiter using in-process memory.

    Accurate for single-worker deployments only.
    """

    _EVICT_INTERVAL = 300.0

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._last_evict: float = time.time()

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.time()
        cutoff = now - window_seconds
        self._evict_stale(now, window_seconds)

        timestamps = self._requests[key]
        self._requests[key] = [t for t in timestamps if t > cutoff]

        current = len(self._requests[key])
        if current >= limit:
            oldest = min(self._requests[key]) if self._requests[key] else now
            retry_after = max(1, int(oldest + window_seconds - now) + 1)
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after=retry_after,
                limit=limit,
            )

        self._requests[key].append(now)
        return RateLimitResult(
            allowed=True,
            remaining=limit - current - 1,
            retry_after=0,
            limit=limit,
        )

    def _evict_stale(self, now: float, window_seconds: int) -> None:
        if now - self._last_evict < self._EVICT_INTERVAL:
            return
        self._last_evict = now
        cutoff = now - window_seconds
        stale = [k for k, ts in self._requests.items() if not ts or ts[-1] <= cutoff]
        for k in stale:
            del self._requests[k]

    def reset(self) -> None:
        self._requests.clear()

    @property
    def backend_type(self) -> str:
        return "in_process"


_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now - window

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window + 1)
    return {1, limit - count - 1, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry = 0
    if #oldest >= 2 then
        retry = math.ceil(tonumber(oldest[2]) + window - now) + 1
    end
    return {0, 0, retry}
end
"""


class RedisBackend(RateLimiterBackend):
    """Sliding-window log rate limiter backed by Redis sorted sets.

    Uses a Lua script for atomic check-and-add. Each member is a unique
    request identifier (timestamp + random) scored by its timestamp.
    Expired members are pruned on every call. The sorted set TTL is
    set to window+1 as a safety net.

    Includes automatic reconnection: if a check fails due to a connection
    error, one reconnect attempt is made. When ``RATE_LIMIT_FAIL_CLOSED`` is
    true (default), requests are blocked if Redis stays unavailable.
    """

    _RECONNECT_COOLDOWN = 5.0
    _FAIL_CLOSED_RETRY_AFTER = 60

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Any = None
        self._script: Any = None
        self._last_reconnect: float = 0.0
        self._connect()

    def _connect(self) -> None:
        try:
            import redis

            self._client = redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self._client.ping()
            self._script = self._client.register_script(_SLIDING_WINDOW_LUA)
            logger.info(
                "Redis rate limiter connected: %s",
                self._redis_url.split("@")[-1],
            )
        except Exception:
            logger.warning(
                "Redis connection failed; rate limiter will use in-process fallback",
                exc_info=True,
            )
            self._client = None
            self._script = None

    def _try_reconnect(self) -> bool:
        now = time.time()
        if now - self._last_reconnect < self._RECONNECT_COOLDOWN:
            return False
        self._last_reconnect = now
        logger.info("Attempting Redis reconnection for rate limiter")
        self._connect()
        return self._client is not None

    def _unavailable_result(self, limit: int) -> RateLimitResult:
        from app.config import settings

        if settings.RATE_LIMIT_FAIL_CLOSED:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after=self._FAIL_CLOSED_RETRY_AFTER,
                limit=limit,
            )
        return RateLimitResult(
            allowed=True,
            remaining=limit,
            retry_after=0,
            limit=limit,
        )

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        if self._client is None:
            if not self._try_reconnect():
                return self._unavailable_result(limit)

        import os

        redis_key = f"nexus:ratelimit:{key}"
        now_ms = time.time()
        member = f"{now_ms}:{os.urandom(4).hex()}"

        try:
            result = self._script(
                keys=[redis_key],
                args=[now_ms, window_seconds, limit, member],
            )
            allowed = bool(result[0])
            remaining = int(result[1])
            retry_after = int(result[2])
            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                retry_after=retry_after,
                limit=limit,
            )
        except Exception:
            logger.warning(
                "Redis rate limit check failed for %s; attempting reconnect",
                key,
                exc_info=True,
            )
            self._client = None
            self._script = None
            if self._try_reconnect():
                try:
                    result = self._script(
                        keys=[redis_key],
                        args=[now_ms, window_seconds, limit, member],
                    )
                    return RateLimitResult(
                        allowed=bool(result[0]),
                        remaining=int(result[1]),
                        retry_after=int(result[2]),
                        limit=limit,
                    )
                except Exception:
                    pass
            return self._unavailable_result(limit)

    def reset(self) -> None:
        if self._client is None:
            return
        try:
            cursor: int = 0
            while True:
                cursor, keys = self._client.scan(
                    cursor,
                    match="nexus:ratelimit:*",
                    count=100,
                )
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            logger.warning("Redis rate limit reset failed", exc_info=True)

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


def get_backend() -> RateLimiterBackend:
    """Get or create the rate limiter backend singleton."""
    global _backend
    if _backend is not None:
        return _backend

    from app.config import settings

    if settings.REDIS_URL.strip():
        try:
            rb = RedisBackend(settings.REDIS_URL)
            if rb.connected:
                _backend = rb
                return _backend
            logger.warning(
                "Redis not reachable; falling back to in-process rate limiter",
            )
        except Exception:
            logger.warning(
                "Redis backend init failed; using in-process fallback",
                exc_info=True,
            )

    _backend = InProcessBackend()
    return _backend


def reset_backend() -> None:
    """Reset the singleton (for testing)."""
    global _backend
    if _backend is not None:
        _backend.reset()
    _backend = None


def get_status() -> dict:
    """Return rate limiter backend status for health checks."""
    backend = get_backend()
    info: dict = {"backend_type": backend.backend_type}
    if isinstance(backend, RedisBackend):
        info["connected"] = backend.connected
    return info
