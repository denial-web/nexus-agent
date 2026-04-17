"""Tests for graceful shutdown coordinator and middleware."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from app.services.shutdown import ShutdownCoordinator, get_coordinator, reset_coordinator


class TestShutdownCoordinator:
    def setup_method(self):
        reset_coordinator()

    def test_initial_state(self):
        coord = ShutdownCoordinator()
        assert not coord.is_draining
        assert coord.in_flight == 0

    def test_track_request_increments(self):
        coord = ShutdownCoordinator()
        with coord.track_request():
            assert coord.in_flight == 1
        assert coord.in_flight == 0

    def test_track_request_nested(self):
        coord = ShutdownCoordinator()
        with coord.track_request():
            with coord.track_request():
                assert coord.in_flight == 2
            assert coord.in_flight == 1
        assert coord.in_flight == 0

    def test_track_request_decrements_on_exception(self):
        coord = ShutdownCoordinator()
        with pytest.raises(ValueError):
            with coord.track_request():
                assert coord.in_flight == 1
                raise ValueError("boom")
        assert coord.in_flight == 0

    def test_start_drain(self):
        coord = ShutdownCoordinator()
        assert not coord.is_draining
        coord.start_drain()
        assert coord.is_draining

    def test_wait_for_drain_immediate(self):
        coord = ShutdownCoordinator()
        coord.start_drain()
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(coord.wait_for_drain(1.0))
        loop.close()
        assert result is True

    def test_wait_for_drain_with_active_request(self):
        coord = ShutdownCoordinator()
        barrier = threading.Event()

        def hold_request():
            with coord.track_request():
                barrier.wait(timeout=5)

        t = threading.Thread(target=hold_request)
        t.start()
        time.sleep(0.05)

        assert coord.in_flight == 1
        coord.start_drain()

        loop = asyncio.new_event_loop()
        drained = loop.run_until_complete(coord.wait_for_drain(0.1))
        loop.close()
        assert drained is False
        assert coord.in_flight == 1

        barrier.set()
        t.join(timeout=2)
        assert coord.in_flight == 0

    def test_wait_for_drain_completes_when_request_finishes(self):
        coord = ShutdownCoordinator()
        barrier = threading.Event()

        def hold_then_release():
            with coord.track_request():
                barrier.wait(timeout=5)

        t = threading.Thread(target=hold_then_release)
        t.start()
        time.sleep(0.05)

        coord.start_drain()

        def release_later():
            time.sleep(0.1)
            barrier.set()

        t2 = threading.Thread(target=release_later)
        t2.start()

        loop = asyncio.new_event_loop()
        drained = loop.run_until_complete(coord.wait_for_drain(5.0))
        loop.close()
        assert drained is True
        t.join(timeout=2)
        t2.join(timeout=2)

    def test_reset(self):
        coord = ShutdownCoordinator()
        coord.start_drain()
        assert coord.is_draining
        coord.reset()
        assert not coord.is_draining
        assert coord.in_flight == 0

    def test_singleton(self):
        reset_coordinator()
        c1 = get_coordinator()
        c2 = get_coordinator()
        assert c1 is c2


class TestShutdownMiddleware:
    def test_normal_request_tracked(self, client):
        resp = client.post("/api/agent/run", json={"prompt": "hello"})
        assert resp.status_code == 200

    def test_draining_rejects_api_requests(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.post("/api/agent/run", json={"prompt": "hello"})
        assert resp.status_code == 503
        data = resp.json()
        assert data["error"]["code"] == "shutting_down"
        assert resp.headers.get("Retry-After") == "30"
        coord.reset()

    def test_draining_allows_health_probe(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "draining"
        coord.reset()

    def test_draining_allows_readiness_probe(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/health/ready")
        data = resp.json()
        assert data["checks"]["shutdown"]["draining"] is True
        coord.reset()

    def test_draining_allows_static(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/static/css/style.css")
        assert resp.status_code in (200, 404)
        coord.reset()

    def test_health_ok_when_not_draining(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_includes_shutdown_info(self, client):
        resp = client.get("/health/ready")
        data = resp.json()
        assert "shutdown" in data["checks"]
        assert data["checks"]["shutdown"]["draining"] is False
        assert data["checks"]["shutdown"]["in_flight"] >= 0

    def test_draining_rejects_v1_endpoints(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.post("/v1/agent/run", json={"prompt": "hello"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "shutting_down"
        coord.reset()

    def test_draining_rejects_traces_endpoint(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/v1/traces")
        assert resp.status_code == 503
        coord.reset()

    def test_draining_rejects_critic_endpoint(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/v1/critic/registry")
        assert resp.status_code == 503
        coord.reset()

    def test_draining_allows_docs(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/docs")
        assert resp.status_code == 200
        coord.reset()

    def test_draining_allows_openapi(self, client):
        coord = get_coordinator()
        coord.start_drain()
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        coord.reset()


class TestCoordinatorConcurrency:
    """Thread safety and concurrent drain behavior."""

    def test_concurrent_track_increments(self):
        coord = ShutdownCoordinator()
        barrier = threading.Barrier(10)
        results = []

        def worker():
            barrier.wait()
            with coord.track_request():
                results.append(coord.in_flight)
                time.sleep(0.02)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert coord.in_flight == 0
        assert max(results) > 1

    def test_drain_waits_for_multiple_in_flight(self):
        coord = ShutdownCoordinator()
        barriers = [threading.Event() for _ in range(3)]

        def hold(barrier: threading.Event):
            with coord.track_request():
                barrier.wait(timeout=5)

        threads = [threading.Thread(target=hold, args=(b,)) for b in barriers]
        for t in threads:
            t.start()
        time.sleep(0.05)

        assert coord.in_flight == 3
        coord.start_drain()

        loop = asyncio.new_event_loop()
        drained = loop.run_until_complete(coord.wait_for_drain(0.1))
        loop.close()
        assert drained is False
        assert coord.in_flight == 3

        for b in barriers:
            b.set()
        for t in threads:
            t.join(timeout=2)
        assert coord.in_flight == 0

    def test_in_flight_request_completes_after_drain_starts(self):
        coord = ShutdownCoordinator()
        request_completed = threading.Event()
        request_started = threading.Event()

        def simulate_request():
            with coord.track_request():
                request_started.set()
                time.sleep(0.15)
            request_completed.set()

        t = threading.Thread(target=simulate_request)
        t.start()
        request_started.wait(timeout=2)

        coord.start_drain()
        assert coord.is_draining
        assert coord.in_flight == 1

        request_completed.wait(timeout=3)
        assert request_completed.is_set()
        assert coord.in_flight == 0

        loop = asyncio.new_event_loop()
        drained = loop.run_until_complete(coord.wait_for_drain(1.0))
        loop.close()
        assert drained is True
        t.join(timeout=2)

    def test_drain_timeout_returns_false(self):
        coord = ShutdownCoordinator()
        hold = threading.Event()

        def stuck():
            with coord.track_request():
                hold.wait(timeout=10)

        t = threading.Thread(target=stuck)
        t.start()
        time.sleep(0.05)

        coord.start_drain()
        loop = asyncio.new_event_loop()
        drained = loop.run_until_complete(coord.wait_for_drain(0.05))
        loop.close()
        assert drained is False
        assert coord.in_flight == 1

        hold.set()
        t.join(timeout=2)

    def test_track_request_exception_safety_concurrent(self):
        coord = ShutdownCoordinator()
        errors = []

        def worker(should_raise: bool):
            try:
                with coord.track_request():
                    time.sleep(0.01)
                    if should_raise:
                        raise RuntimeError("boom")
            except RuntimeError:
                pass
            errors.append(should_raise)

        threads = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert coord.in_flight == 0
        assert len(errors) == 10
