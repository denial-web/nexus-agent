"""Tests for API authentication and rate limiting middleware."""

from unittest.mock import patch

import app.middleware as mw_module
import pytest
from app.config import settings


class TestAuthMiddleware:
    def test_no_auth_when_key_empty(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "")
        resp = client.get("/api/traces")
        assert resp.status_code == 200

    def test_health_exempt_from_auth(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_exempt_from_auth(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/health/ready")
        assert resp.status_code == 200

    def test_rejects_missing_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/api/traces")
        assert resp.status_code == 401
        assert "Invalid or missing API key" in resp.json()["detail"]

    def test_rejects_wrong_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/api/traces", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_accepts_correct_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/api/traces", headers={"X-API-Key": "secret-key-123"})
        assert resp.status_code == 200

    def test_post_requires_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.post("/api/agent/run", json={"prompt": "test"})
        assert resp.status_code == 401

    def test_post_with_key_works(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.post(
            "/api/agent/run",
            json={"prompt": "test"},
            headers={"X-API-Key": "secret-key-123"},
        )
        assert resp.status_code == 200


class TestRateLimitMiddleware:
    @pytest.fixture(autouse=True)
    def _clear_rate_limits(self, client):
        if mw_module._rate_limiter_instance is not None:
            mw_module._rate_limiter_instance.reset()

    def test_no_limit_when_rpm_zero(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 0)
        for _ in range(5):
            resp = client.post("/api/agent/run", json={"prompt": "test"})
            assert resp.status_code == 200

    def test_rate_limit_enforced(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 3)
        for i in range(3):
            resp = client.post("/api/agent/run", json={"prompt": f"test {i}"})
            assert resp.status_code == 200, f"Request {i + 1} should succeed"

        resp = client.post("/api/agent/run", json={"prompt": "over limit"})
        assert resp.status_code == 429
        assert "Rate limit exceeded" in resp.json()["detail"]
        assert resp.headers.get("Retry-After") == "60"

    def test_rate_limit_only_on_agent_run(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 1)
        client.post("/api/agent/run", json={"prompt": "first"})
        resp = client.get("/api/traces")
        assert resp.status_code == 200

    def test_rate_limit_window_expires(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 1)
        client.post("/api/agent/run", json={"prompt": "first"})

        resp = client.post("/api/agent/run", json={"prompt": "blocked"})
        assert resp.status_code == 429

        with patch("app.middleware.time") as mock_time:
            mock_time.time.return_value = 9999999999.0
            resp = client.post("/api/agent/run", json={"prompt": "after window"})
            assert resp.status_code == 200
