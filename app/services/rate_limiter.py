"""
Rate limiter backends: in-process (default) and Redis (multi-worker).

When REDIS_URL is configured, the Redis backend uses a sliding-window
counter (INCR + EXPIRE) that is shared across all workers/instances.
Falls back to in-process memory when Redis is unavailable or unconfigured.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict

logger = logging.getLogger(__name__)

_backend: RateLimiterBackend | None = None


class RateLimiterBackend(ABC):
    @abstractmethod
    def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        """Return True if the request is within the rate limit."""

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

    def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        self._evict_stale(now, window_seconds)

        timestamps = self._requests[key]
        self._requests[key] = [t for t in timestamps if t > cutoff]

        if len(self._requests[key]) >= limit:
            return False

        self._requests[key].append(now)
        return True

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


class RedisBackend(RateLimiterBackend):
    """Sliding-window rate limiter backed by Redis INCR + EXPIRE.

    Each rate-limit key maps to a Redis key with a TTL equal to the window.
    The counter auto-expires so no cleanup is needed.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: _RedisType | None = None
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
            logger.info("Redis rate limiter connected: %s", self._redis_url.split("@")[-1])
        except Exception:
            logger.warning("Redis connection failed; rate limiter will use in-process fallback", exc_info=True)
            self._client = None

    def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        if self._client is None:
            return True

        redis_key = f"nexus:ratelimit:{key}"
        try:
            pipe = self._client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds, nx=True)
            results = pipe.execute()
            count = results[0]
            return count <= limit
        except Exception:
            logger.warning("Redis rate limit check failed for %s", key, exc_info=True)
            return True

    def reset(self) -> None:
        if self._client is None:
            return
        try:
            cursor: int = 0
            while True:
                cursor, keys = self._client.scan(cursor, match="nexus:ratelimit:*", count=100)
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


try:
    import redis as _redis_mod

    _RedisType = _redis_mod.Redis
except ImportError:
    _RedisType = object


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
            logger.warning("Redis not reachable; falling back to in-process rate limiter")
        except Exception:
            logger.warning("Redis backend init failed; using in-process fallback", exc_info=True)

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
