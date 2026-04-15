"""
Semantic cache for LLM responses.

Exact-match cache keyed on (prompt_hash, model_id, system_prompt_hash).
Cached responses still pass through critic evaluation, governance, and
output scan — only the LLM call is skipped.

Thread-safe LRU with TTL eviction. In-process only; use Redis for
multi-worker deployments.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.core.llm.models import LLMResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = False
    ttl_seconds: float = 300.0
    max_entries: int = 1000


@dataclass
class CacheEntry:
    response: LLMResponse
    created_at: float
    hit_count: int = 0


def _cache_key(prompt: str, model_id: str | None, system_prompt: str | None) -> str:
    h = hashlib.sha256()
    h.update(prompt.encode())
    h.update((model_id or "auto").encode())
    h.update((system_prompt or "").encode())
    return h.hexdigest()


class LLMResponseCache:
    """Thread-safe LRU cache with TTL for LLM responses."""

    def __init__(self, config: CacheConfig | None = None):
        self._config = config or CacheConfig()
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def get(
        self, prompt: str, model_id: str | None, system_prompt: str | None,
    ) -> LLMResponse | None:
        if not self._config.enabled:
            return None

        key = _cache_key(prompt, model_id, system_prompt)
        now = time.monotonic()

        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            age = now - entry.created_at
            if age > self._config.ttl_seconds:
                del self._store[key]
                self._evictions += 1
                self._misses += 1
                return None

            entry.hit_count += 1
            self._hits += 1
            self._store.move_to_end(key)
            return entry.response

    def put(
        self,
        prompt: str,
        model_id: str | None,
        system_prompt: str | None,
        response: LLMResponse,
    ) -> None:
        if not self._config.enabled:
            return

        key = _cache_key(prompt, model_id, system_prompt)
        now = time.monotonic()

        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = CacheEntry(response=response, created_at=now)
                return

            self._store[key] = CacheEntry(response=response, created_at=now)
            self._store.move_to_end(key)

            while len(self._store) > self._config.max_entries:
                self._store.popitem(last=False)
                self._evictions += 1

    def invalidate(
        self, prompt: str, model_id: str | None, system_prompt: str | None,
    ) -> bool:
        if not self._config.enabled:
            return False
        key = _cache_key(prompt, model_id, system_prompt)
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def get_stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "enabled": self._config.enabled,
                "size": len(self._store),
                "max_entries": self._config.max_entries,
                "ttl_seconds": self._config.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
            }


_cache: LLMResponseCache | None = None
_cache_lock = threading.Lock()


def get_cache() -> LLMResponseCache:
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is None:
            from app.config import settings

            _cache = LLMResponseCache(
                config=CacheConfig(
                    enabled=settings.LLM_CACHE_ENABLED,
                    ttl_seconds=settings.LLM_CACHE_TTL,
                    max_entries=settings.LLM_CACHE_MAX_ENTRIES,
                ),
            )
        return _cache


def reset_cache() -> None:
    """Reset the global cache (for tests)."""
    global _cache
    _cache = None
