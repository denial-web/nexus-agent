"""Graceful shutdown coordinator.

Tracks in-flight requests and provides a drain mechanism so that on SIGTERM
the server can stop accepting new work, wait for active requests to finish
(up to a configurable deadline), and then proceed with cleanup.

Usage from middleware:
    from app.services.shutdown import get_coordinator
    coord = get_coordinator()
    if coord.is_draining:
        return 503
    with coord.track_request():
        response = await call_next(request)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class ShutdownCoordinator:
    def __init__(self) -> None:
        self._draining = False
        self._in_flight = 0
        self._lock = threading.Lock()
        self._drained_event = threading.Event()
        self._drained_event.set()

    @property
    def is_draining(self) -> bool:
        return self._draining

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @contextmanager
    def track_request(self) -> Generator[None]:
        with self._lock:
            self._in_flight += 1
            self._drained_event.clear()
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._drained_event.set()

    def start_drain(self) -> None:
        logger.info("Graceful shutdown: entering drain mode (in-flight=%d)", self._in_flight)
        self._draining = True

    async def wait_for_drain(self, timeout: float) -> bool:
        """Wait until all in-flight requests complete or timeout expires.

        Returns True if drained cleanly, False if timed out.
        """
        loop = asyncio.get_event_loop()
        drained = await loop.run_in_executor(None, self._drained_event.wait, timeout)
        if drained:
            logger.info("Graceful shutdown: all requests drained")
        else:
            logger.warning(
                "Graceful shutdown: drain timed out after %.1fs with %d requests still in-flight",
                timeout,
                self._in_flight,
            )
        return bool(drained)

    def reset(self) -> None:
        """Reset state (for testing)."""
        with self._lock:
            self._draining = False
            self._in_flight = 0
            self._drained_event.set()


_coordinator: ShutdownCoordinator | None = None
_init_lock = threading.Lock()


def get_coordinator() -> ShutdownCoordinator:
    global _coordinator
    if _coordinator is not None:
        return _coordinator
    with _init_lock:
        if _coordinator is None:
            _coordinator = ShutdownCoordinator()
        return _coordinator


def reset_coordinator() -> None:
    """Reset the global coordinator (for testing)."""
    global _coordinator
    if _coordinator:
        _coordinator.reset()
    _coordinator = None
