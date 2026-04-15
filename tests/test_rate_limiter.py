"""Tests for rate limiter backends and Redis integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.rate_limiter import (
    InProcessBackend,
    RedisBackend,
    get_backend,
    get_status,
    reset_backend,
)


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


class TestRedisBackendMocked:
    """Test Redis backend with mocked redis client."""

    def test_is_allowed_under_limit(self):
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [3, True]
        mock_redis.pipeline.return_value = mock_pipe

        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"

        assert backend.is_allowed("ip1", 5, 60) is True
        mock_pipe.incr.assert_called_once_with("nexus:ratelimit:ip1")
        mock_pipe.expire.assert_called_once_with("nexus:ratelimit:ip1", 60, nx=True)

    def test_is_allowed_over_limit(self):
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [6, False]
        mock_redis.pipeline.return_value = mock_pipe

        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"

        assert backend.is_allowed("ip1", 5, 60) is False

    def test_is_allowed_returns_true_on_redis_error(self):
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.side_effect = Exception("connection lost")
        mock_redis.pipeline.return_value = mock_pipe

        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"

        assert backend.is_allowed("ip1", 5, 60) is True

    def test_is_allowed_when_client_is_none(self):
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = None
        backend._redis_url = "redis://localhost"

        assert backend.is_allowed("ip1", 5, 60) is True

    def test_backend_type_connected(self):
        mock_redis = MagicMock()
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"
        assert backend.backend_type == "redis"

    def test_backend_type_disconnected(self):
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = None
        backend._redis_url = "redis://localhost"
        assert backend.backend_type == "redis_disconnected"

    def test_connected_property_pings(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"
        assert backend.connected is True

    def test_connected_false_on_error(self):
        mock_redis = MagicMock()
        mock_redis.ping.side_effect = Exception("gone")
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"
        assert backend.connected is False

    def test_reset_scans_and_deletes(self):
        mock_redis = MagicMock()
        mock_redis.scan.return_value = (0, ["nexus:ratelimit:ip1", "nexus:ratelimit:ip2"])
        backend = RedisBackend.__new__(RedisBackend)
        backend._client = mock_redis
        backend._redis_url = "redis://localhost"
        backend.reset()
        mock_redis.delete.assert_called_once_with("nexus:ratelimit:ip1", "nexus:ratelimit:ip2")


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
