"""Tests for rate limiter backends and Redis integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.rate_limiter import (
    InProcessBackend,
    RateLimitResult,
    RedisBackend,
    get_backend,
    get_status,
    reset_backend,
)


def _make_redis_backend(
    script_return: list | None = None,
    script_side_effect: Exception | None = None,
) -> RedisBackend:
    """Create a RedisBackend with mocked internals (no real connection)."""
    mock_redis = MagicMock()
    mock_script = MagicMock()
    if script_side_effect:
        mock_script.side_effect = script_side_effect
    elif script_return is not None:
        mock_script.return_value = script_return
    else:
        mock_script.return_value = [1, 4, 0]

    backend = RedisBackend.__new__(RedisBackend)
    backend._client = mock_redis
    backend._script = mock_script
    backend._redis_url = "redis://localhost"
    backend._last_reconnect = 0.0
    return backend


class TestInProcessBackend:
    def setup_method(self):
        self.backend = InProcessBackend()

    def test_allows_under_limit(self):
        for _ in range(5):
            assert self.backend.is_allowed("ip1", 5, 60) is True

    def test_blocks_at_limit(self):
        for _ in range(3):
            self.backend.is_allowed("ip1", 3, 60)
        assert self.backend.is_allowed("ip1", 3, 60) is False

    def test_separate_keys(self):
        for _ in range(3):
            self.backend.is_allowed("ip1", 3, 60)
        assert self.backend.is_allowed("ip1", 3, 60) is False
        assert self.backend.is_allowed("ip2", 3, 60) is True

    def test_window_expiry(self):
        with patch("app.services.rate_limiter.time") as mock_time:
            mock_time.time.return_value = 1000.0
            self.backend.is_allowed("ip1", 1, 60)
            assert self.backend.is_allowed("ip1", 1, 60) is False

            mock_time.time.return_value = 1061.0
            assert self.backend.is_allowed("ip1", 1, 60) is True

    def test_reset_clears_state(self):
        self.backend.is_allowed("ip1", 1, 60)
        assert self.backend.is_allowed("ip1", 1, 60) is False
        self.backend.reset()
        assert self.backend.is_allowed("ip1", 1, 60) is True

    def test_backend_type(self):
        assert self.backend.backend_type == "in_process"

    def test_evict_stale_entries(self):
        with patch("app.services.rate_limiter.time") as mock_time:
            mock_time.time.return_value = 1000.0
            self.backend.is_allowed("ip1", 10, 60)
            self.backend._last_evict = 0.0

            mock_time.time.return_value = 2000.0
            self.backend.is_allowed("ip2", 10, 60)
            assert "ip1" not in self.backend._requests

    def test_check_returns_result_with_remaining(self):
        result = self.backend.check("ip1", 5, 60)
        assert isinstance(result, RateLimitResult)
        assert result.allowed is True
        assert result.remaining == 4
        assert result.limit == 5

    def test_check_blocked_has_retry_after(self):
        for _ in range(3):
            self.backend.check("ip1", 3, 60)
        result = self.backend.check("ip1", 3, 60)
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after > 0

    def test_remaining_decreases(self):
        r1 = self.backend.check("ip1", 3, 60)
        assert r1.remaining == 2
        r2 = self.backend.check("ip1", 3, 60)
        assert r2.remaining == 1
        r3 = self.backend.check("ip1", 3, 60)
        assert r3.remaining == 0
        assert r3.allowed is True
        r4 = self.backend.check("ip1", 3, 60)
        assert r4.allowed is False


class TestRedisBackendMocked:
    """Test Redis backend with mocked Lua script."""

    def test_check_under_limit(self):
        backend = _make_redis_backend(script_return=[1, 4, 0])
        result = backend.check("ip1", 5, 60)
        assert result.allowed is True
        assert result.remaining == 4

    def test_check_over_limit(self):
        backend = _make_redis_backend(script_return=[0, 0, 15])
        result = backend.check("ip1", 5, 60)
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after == 15

    def test_is_allowed_under_limit(self):
        backend = _make_redis_backend(script_return=[1, 4, 0])
        assert backend.is_allowed("ip1", 5, 60) is True

    def test_is_allowed_over_limit(self):
        backend = _make_redis_backend(script_return=[0, 0, 10])
        assert backend.is_allowed("ip1", 5, 60) is False

    def test_allows_on_redis_error_with_failed_reconnect(self):
        backend = _make_redis_backend(
            script_side_effect=Exception("connection lost"),
        )
        backend._last_reconnect = 0.0
        with patch.object(backend, "_try_reconnect", return_value=False):
            result = backend.check("ip1", 5, 60)
        assert result.allowed is True

    def test_check_when_client_is_none_tries_reconnect(self):
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = None
        backend._script = None
        backend._redis_url = "redis://localhost"
        backend._last_reconnect = 0.0
        with patch.object(backend, "_try_reconnect", return_value=False):
            result = backend.check("ip1", 5, 60)
        assert result.allowed is True
        assert result.remaining == 5

    def test_backend_type_connected(self):
        backend = _make_redis_backend()
        assert backend.backend_type == "redis"

    def test_backend_type_disconnected(self):
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = None
        backend._redis_url = "redis://localhost"
        backend._last_reconnect = 0.0
        assert backend.backend_type == "redis_disconnected"

    def test_connected_property_pings(self):
        backend = _make_redis_backend()
        backend._client.ping.return_value = True
        assert backend.connected is True

    def test_connected_false_on_error(self):
        backend = _make_redis_backend()
        backend._client.ping.side_effect = Exception("gone")
        assert backend.connected is False

    def test_reset_scans_and_deletes(self):
        backend = _make_redis_backend()
        backend._client.scan.return_value = (
            0,
            ["nexus:ratelimit:ip1", "nexus:ratelimit:ip2"],
        )
        backend.reset()
        backend._client.delete.assert_called_once_with(
            "nexus:ratelimit:ip1",
            "nexus:ratelimit:ip2",
        )

    def test_reconnect_cooldown(self):
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = None
        backend._script = None
        backend._redis_url = "redis://localhost"
        backend._last_reconnect = 9999999999.0
        result = backend._try_reconnect()
        assert result is False


class TestGetBackend:
    def setup_method(self):
        reset_backend()

    def teardown_method(self):
        reset_backend()

    def test_default_is_in_process(self):
        backend = get_backend()
        assert backend.backend_type == "in_process"

    def test_returns_singleton(self):
        b1 = get_backend()
        b2 = get_backend()
        assert b1 is b2

    def test_falls_back_when_redis_unreachable(self):
        with patch("app.config.settings") as mock_settings:
            mock_settings.REDIS_URL = "redis://unreachable:6379"
            reset_backend()
            backend = get_backend()
            assert backend.backend_type == "in_process"

    def test_get_status_shape(self):
        status = get_status()
        assert "backend_type" in status
        assert status["backend_type"] == "in_process"


class TestTraceIdHeader:
    def test_x_trace_id_header_absent_when_no_span(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID") is not None

    def test_health_ready_includes_rate_limiter(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert "rate_limiter" in data["checks"]
        assert "backend_type" in data["checks"]["rate_limiter"]
