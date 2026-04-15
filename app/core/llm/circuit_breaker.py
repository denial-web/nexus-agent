"""
Circuit breaker for LLM providers.

States:
  CLOSED  — normal operation, requests flow through
  OPEN    — provider is failing, requests fast-fail immediately
  HALF_OPEN — probe mode: one request allowed through to test recovery

Transitions:
  CLOSED → OPEN: failure_count >= failure_threshold within the rolling window
  OPEN → HALF_OPEN: recovery_timeout has elapsed since last failure
  HALF_OPEN → CLOSED: probe request succeeds
  HALF_OPEN → OPEN: probe request fails (resets the recovery timer)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    rolling_window_seconds: float = 60.0
    success_threshold: int = 1


class CircuitBreaker:
    """Per-provider circuit breaker with thread-safe state transitions."""

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None):
        self.name = name
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_timestamps: list[float] = []
        self._last_failure_time: float = 0.0
        self._half_open_successes: int = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._evaluate_state()

    def _evaluate_state(self) -> CircuitState:
        """Caller must hold _lock."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._config.recovery_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_successes = 0
                logger.info(
                    "Circuit breaker %s: OPEN → HALF_OPEN (%.1fs elapsed)",
                    self.name, elapsed,
                )
        return self._state

    def allow_request(self) -> bool:
        with self._lock:
            state = self._evaluate_state()
            return state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self._config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_timestamps.clear()
                    logger.info("Circuit breaker %s: HALF_OPEN → CLOSED (recovered)", self.name)

    def record_failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._last_failure_time = now
                logger.warning("Circuit breaker %s: HALF_OPEN → OPEN (probe failed)", self.name)
                return

            cutoff = now - self._config.rolling_window_seconds
            self._failure_timestamps = [
                t for t in self._failure_timestamps if t > cutoff
            ]
            self._failure_timestamps.append(now)

            if len(self._failure_timestamps) >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                self._last_failure_time = now
                logger.warning(
                    "Circuit breaker %s: CLOSED → OPEN (%d failures in %.0fs window)",
                    self.name,
                    len(self._failure_timestamps),
                    self._config.rolling_window_seconds,
                )

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_timestamps.clear()
            self._last_failure_time = 0.0
            self._half_open_successes = 0

    def get_status(self) -> dict:
        with self._lock:
            state = self._evaluate_state()
            cutoff = time.monotonic() - self._config.rolling_window_seconds
            recent_failures = sum(
                1 for t in self._failure_timestamps if t > cutoff
            )
            return {
                "name": self.name,
                "state": state.value,
                "recent_failures": recent_failures,
                "failure_threshold": self._config.failure_threshold,
                "recovery_timeout_seconds": self._config.recovery_timeout_seconds,
            }


class CircuitOpenError(Exception):
    """Raised when a request is rejected because the circuit is open."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"Circuit breaker open for provider '{provider}'")


class CircuitBreakerRegistry:
    """Manages per-provider circuit breakers."""

    def __init__(self, default_config: CircuitBreakerConfig | None = None):
        self._default_config = default_config or CircuitBreakerConfig()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, provider: str) -> CircuitBreaker:
        if provider in self._breakers:
            return self._breakers[provider]
        with self._lock:
            if provider not in self._breakers:
                self._breakers[provider] = CircuitBreaker(
                    provider, self._default_config,
                )
            return self._breakers[provider]

    def get_all_status(self) -> list[dict]:
        with self._lock:
            return [cb.get_status() for cb in self._breakers.values()]

    def reset_all(self) -> None:
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()


_registry: CircuitBreakerRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> CircuitBreakerRegistry:
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            from app.config import settings

            _registry = CircuitBreakerRegistry(
                default_config=CircuitBreakerConfig(
                    failure_threshold=settings.CB_FAILURE_THRESHOLD,
                    recovery_timeout_seconds=settings.CB_RECOVERY_TIMEOUT,
                    rolling_window_seconds=settings.CB_WINDOW_SECONDS,
                ),
            )
        return _registry


def reset_registry() -> None:
    """Reset the global registry (for tests)."""
    global _registry
    _registry = None
