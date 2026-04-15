"""Prometheus application metrics.

All metric objects are lazily created: if prometheus_client is not installed,
every collector is a no-op stub so callers never need to guard imports.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram

    PIPELINE_LATENCY = Histogram(
        "nexus_pipeline_duration_seconds",
        "End-to-end pipeline latency",
        ["status"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    )
    PIPELINE_RUNS = Counter(
        "nexus_pipeline_runs_total",
        "Total pipeline executions",
        ["status"],
    )
    LLM_CALLS = Counter(
        "nexus_llm_calls_total",
        "Total LLM provider calls",
        ["provider"],
    )
    LLM_ERRORS = Counter(
        "nexus_llm_errors_total",
        "LLM call failures",
        ["provider", "error_type"],
    )
    CRITIC_SCORES = Histogram(
        "nexus_critic_score",
        "Per-node critic score distribution",
        ["node"],
        buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    )
    LABELING_QUEUE_DEPTH = Gauge(
        "nexus_labeling_queue_pending",
        "Number of pending items in the labeling queue",
    )
    ACTIVE_SESSIONS = Gauge(
        "nexus_active_sessions",
        "Approximate active session count",
    )
    CB_STATE_CHANGES = Counter(
        "nexus_circuit_breaker_state_changes_total",
        "Circuit breaker state transitions",
        ["provider", "from_state", "to_state"],
    )
    CB_REJECTIONS = Counter(
        "nexus_circuit_breaker_rejections_total",
        "Requests rejected by open circuit breaker",
        ["provider"],
    )
    CB_FALLBACKS = Counter(
        "nexus_circuit_breaker_fallbacks_total",
        "Requests routed to fallback provider due to open circuit",
        ["original_provider", "fallback_provider"],
    )
    CACHE_HITS = Counter(
        "nexus_llm_cache_hits_total",
        "LLM response cache hits",
    )
    CACHE_MISSES = Counter(
        "nexus_llm_cache_misses_total",
        "LLM response cache misses",
    )
    _HAS_PROMETHEUS = True

except ImportError:
    _HAS_PROMETHEUS = False

    class _Noop:
        """Drop-in stub when prometheus_client is absent."""

        def labels(self, *_args: Any, **_kwargs: Any) -> _Noop:
            return self

        def observe(self, _v: float) -> None:
            pass

        def inc(self, _v: float = 1) -> None:
            pass

        def dec(self, _v: float = 1) -> None:
            pass

        def set(self, _v: float) -> None:
            pass

    _noop = _Noop()
    PIPELINE_LATENCY = _noop
    PIPELINE_RUNS = _noop
    LLM_CALLS = _noop
    LLM_ERRORS = _noop
    CRITIC_SCORES = _noop
    LABELING_QUEUE_DEPTH = _noop
    ACTIVE_SESSIONS = _noop
    CB_STATE_CHANGES = _noop
    CB_REJECTIONS = _noop
    CB_FALLBACKS = _noop
    CACHE_HITS = _noop
    CACHE_MISSES = _noop

    logger.debug("prometheus_client not installed; metrics are no-ops")


@contextmanager
def pipeline_timer(status: str = "unknown") -> Any:
    """Context manager that records pipeline latency + run count on exit."""
    start = time.monotonic()
    _status = {"value": status}
    try:
        yield _status
    finally:
        elapsed = time.monotonic() - start
        PIPELINE_LATENCY.labels(status=_status["value"]).observe(elapsed)
        PIPELINE_RUNS.labels(status=_status["value"]).inc()


def record_critic_scores(scores: dict) -> None:
    for node, score_data in scores.items():
        if isinstance(score_data, (int, float)):
            CRITIC_SCORES.labels(node=node).observe(score_data)
        elif hasattr(score_data, "score"):
            CRITIC_SCORES.labels(node=node).observe(score_data.score)
        elif isinstance(score_data, dict) and "score" in score_data:
            CRITIC_SCORES.labels(node=node).observe(score_data["score"])


def is_available() -> bool:
    return _HAS_PROMETHEUS
