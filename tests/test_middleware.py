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


class TestDashboardAuth:
    def test_dashboard_open_when_no_api_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "")
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 200

    def test_dashboard_redirects_to_login_when_api_key_set(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/dashboard/login" in resp.headers.get("location", "")

    def test_login_page_accessible_without_auth(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/dashboard/login")
        assert resp.status_code == 200
        assert "API Key" in resp.text or "api_key" in resp.text

    def test_login_with_correct_key_sets_session(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.post(
            "/dashboard/login",
            data={"api_key": "secret-key-123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/dashboard" in resp.headers.get("location", "")

        dashboard = client.get("/dashboard", follow_redirects=False)
        assert dashboard.status_code == 200

    def test_login_with_wrong_key_rejected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.post(
            "/dashboard/login",
            data={"api_key": "wrong-key"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "Invalid" in resp.text

    def test_logout_clears_session(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        client.post(
            "/dashboard/login",
            data={"api_key": "secret-key-123"},
            follow_redirects=False,
        )
        dashboard = client.get("/dashboard", follow_redirects=False)
        assert dashboard.status_code == 200

        client.get("/dashboard/logout", follow_redirects=False)

        after = client.get("/dashboard", follow_redirects=False)
        assert after.status_code == 302

    def test_query_param_no_longer_grants_access(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/dashboard?api_key=secret-key-123", follow_redirects=False)
        assert resp.status_code == 302
        assert "/dashboard/login" in resp.headers.get("location", "")


class TestMetricsAuth:
    def test_metrics_requires_auth_when_key_set(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/metrics")
        assert resp.status_code == 401

    def test_metrics_accessible_with_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "secret-key-123")
        resp = client.get("/metrics", headers={"X-API-Key": "secret-key-123"})
        assert resp.status_code in (200, 404)

    def test_metrics_open_when_no_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "")
        resp = client.get("/metrics")
        assert resp.status_code in (200, 404)


class TestProductionConfig:
    def test_prod_requires_api_key(self, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "")
        monkeypatch.setattr(settings, "SESSION_SECRET", "test-secret")
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        from app.main import _validate_startup_config

        with pytest.raises(RuntimeError, match="NEXUS_API_KEY"):
            _validate_startup_config()

    def test_prod_ok_with_api_key(self, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "my-secret")
        monkeypatch.setattr(settings, "SESSION_SECRET", "test-secret")
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        from app.main import _validate_startup_config

        _validate_startup_config()

    def test_dev_allows_empty_api_key(self, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "")
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        from app.main import _validate_startup_config

        _validate_startup_config()


class TestSafeKeyCompare:
    def test_matching_keys(self):
        from app.middleware import _safe_key_compare

        assert _safe_key_compare("secret", "secret") is True

    def test_different_keys(self):
        from app.middleware import _safe_key_compare

        assert _safe_key_compare("wrong", "secret") is False

    def test_different_length_keys(self):
        from app.middleware import _safe_key_compare

        assert _safe_key_compare("s", "very-long-secret-key-here") is False

    def test_empty_vs_nonempty(self):
        from app.middleware import _safe_key_compare

        assert _safe_key_compare("", "secret") is False


class TestRateLimitMiddleware:
    @pytest.fixture(autouse=True)
    def _clear_rate_limits(self, client):
        from app.services.rate_limiter import reset_backend

        reset_backend()
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

    def test_rate_limit_skips_get_requests(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 1)
        client.post("/api/agent/run", json={"prompt": "first"})
        resp = client.get("/api/traces")
        assert resp.status_code == 200

    def test_rate_limit_shared_across_expensive_endpoints(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 2)
        r1 = client.post("/api/agent/run", json={"prompt": "first"})
        assert r1.status_code == 200
        r2 = client.post(
            "/api/training/export",
            json={"batch_size": 1, "send_to_doctrine_lab": False},
        )
        assert r2.status_code == 200

        r3 = client.post("/api/agent/run", json={"prompt": "over limit"})
        assert r3.status_code == 429

    def test_rate_limit_window_expires(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 1)
        client.post("/api/agent/run", json={"prompt": "first"})

        resp = client.post("/api/agent/run", json={"prompt": "blocked"})
        assert resp.status_code == 429

        with patch("app.services.rate_limiter.time") as mock_time:
            mock_time.time.return_value = 9999999999.0
            resp = client.post("/api/agent/run", json={"prompt": "after window"})
            assert resp.status_code == 200

    def test_login_is_rate_limited(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 2)
        client.post("/dashboard/login", data={"api_key": "wrong1"})
        client.post("/dashboard/login", data={"api_key": "wrong2"})
        resp = client.post("/dashboard/login", data={"api_key": "wrong3"})
        assert resp.status_code == 429


class TestKeyRotation:
    """API key rotation: comma-separated keys, primary vs secondary."""

    def test_parse_single_key(self):
        from app.middleware import _parse_api_keys

        with patch.object(settings, "NEXUS_API_KEY", "key-one"):
            assert _parse_api_keys() == ["key-one"]

    def test_parse_multiple_keys(self):
        from app.middleware import _parse_api_keys

        with patch.object(settings, "NEXUS_API_KEY", "primary,secondary,third"):
            assert _parse_api_keys() == ["primary", "secondary", "third"]

    def test_parse_strips_whitespace(self):
        from app.middleware import _parse_api_keys

        with patch.object(settings, "NEXUS_API_KEY", " primary , secondary "):
            assert _parse_api_keys() == ["primary", "secondary"]

    def test_parse_empty(self):
        from app.middleware import _parse_api_keys

        with patch.object(settings, "NEXUS_API_KEY", ""):
            assert _parse_api_keys() == []

    def test_check_primary_key(self):
        from app.middleware import check_api_key

        with patch.object(settings, "NEXUS_API_KEY", "primary,secondary"):
            valid, is_primary = check_api_key("primary")
            assert valid is True
            assert is_primary is True

    def test_check_secondary_key(self):
        from app.middleware import check_api_key

        with patch.object(settings, "NEXUS_API_KEY", "primary,secondary"):
            valid, is_primary = check_api_key("secondary")
            assert valid is True
            assert is_primary is False

    def test_check_invalid_key(self):
        from app.middleware import check_api_key

        with patch.object(settings, "NEXUS_API_KEY", "primary,secondary"):
            valid, is_primary = check_api_key("wrong")
            assert valid is False

    def test_check_single_key_is_primary(self):
        from app.middleware import check_api_key

        with patch.object(settings, "NEXUS_API_KEY", "only-key"):
            valid, is_primary = check_api_key("only-key")
            assert valid is True
            assert is_primary is True

    def test_primary_key_no_deprecation_header(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "new-key,old-key")
        resp = client.get("/v1/traces", headers={"X-API-Key": "new-key"})
        assert resp.status_code == 200
        assert "X-API-Key-Deprecated" not in resp.headers

    def test_secondary_key_gets_deprecation_header(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "new-key,old-key")
        resp = client.get("/v1/traces", headers={"X-API-Key": "old-key"})
        assert resp.status_code == 200
        assert resp.headers.get("X-API-Key-Deprecated") == "true"

    def test_wrong_key_rejected_with_multi_keys(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "new-key,old-key")
        resp = client.get("/v1/traces", headers={"X-API-Key": "neither"})
        assert resp.status_code == 401

    def test_dashboard_login_accepts_any_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "new-key,old-key")
        resp = client.post(
            "/dashboard/login",
            data={"api_key": "old-key"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_dashboard_login_rejects_invalid(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "new-key,old-key")
        resp = client.post(
            "/dashboard/login",
            data={"api_key": "wrong"},
        )
        assert resp.status_code == 403

    def test_three_key_rotation(self, client, monkeypatch):
        monkeypatch.setattr(settings, "NEXUS_API_KEY", "newest,current,oldest")
        r1 = client.get("/v1/traces", headers={"X-API-Key": "newest"})
        assert r1.status_code == 200
        assert "X-API-Key-Deprecated" not in r1.headers

        r2 = client.get("/v1/traces", headers={"X-API-Key": "current"})
        assert r2.status_code == 200
        assert r2.headers.get("X-API-Key-Deprecated") == "true"

        r3 = client.get("/v1/traces", headers={"X-API-Key": "oldest"})
        assert r3.status_code == 200
        assert r3.headers.get("X-API-Key-Deprecated") == "true"


class TestBodySizeLimitMiddleware:
    """Request body size limit enforcement."""

    def test_small_body_allowed(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 1_000_000)
        resp = client.post("/v1/agent/run", json={"prompt": "hello"})
        assert resp.status_code == 200

    def test_oversized_content_length_rejected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 100)
        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "x" * 200},
        )
        assert resp.status_code == 413
        data = resp.json()
        assert data["error"]["code"] == "payload_too_large"
        assert "limit" in data["error"]["message"].lower()

    def test_disabled_when_zero(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 0)
        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "x" * 10_000},
        )
        assert resp.status_code == 200

    def test_health_not_affected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 1)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_get_requests_pass_through(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 1)
        resp = client.get("/v1/traces")
        assert resp.status_code == 200

    def test_error_has_backward_compat_detail(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 50)
        resp = client.post("/v1/agent/run", json={"prompt": "x" * 200})
        assert resp.status_code == 413
        data = resp.json()
        assert "detail" in data


class TestSecurityHeaders:
    """Verify security headers on API and dashboard responses."""

    def test_api_has_baseline_headers(self, client):
        resp = client.get("/v1/traces")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert resp.headers["X-Permitted-Cross-Domain-Policies"] == "none"

    def test_api_has_permissions_policy(self, client):
        resp = client.get("/v1/traces")
        pp = resp.headers["Permissions-Policy"]
        assert "camera=()" in pp
        assert "microphone=()" in pp
        assert "geolocation=()" in pp

    def test_api_csp_minimal(self, client):
        resp = client.get("/v1/traces")
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src" not in csp

    def test_dashboard_csp_allows_styles_and_fonts(self, client):
        resp = client.get("/dashboard")
        csp = resp.headers["Content-Security-Policy"]
        assert "style-src" in csp
        assert "fonts.googleapis.com" in csp
        assert "fonts.gstatic.com" in csp
        assert "script-src 'none'" in csp

    def test_dashboard_csp_blocks_scripts(self, client):
        resp = client.get("/dashboard")
        csp = resp.headers["Content-Security-Policy"]
        assert "script-src 'none'" in csp

    def test_dashboard_csp_form_action_self(self, client):
        resp = client.get("/dashboard")
        csp = resp.headers["Content-Security-Policy"]
        assert "form-action 'self'" in csp

    def test_no_hsts_in_dev(self, client):
        resp = client.get("/v1/traces")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_in_production(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        resp = client.get("/v1/traces")
        hsts = resp.headers.get("Strict-Transport-Security", "")
        assert "max-age=" in hsts
        assert "includeSubDomains" in hsts

    def test_health_endpoint_has_headers(self, client):
        resp = client.get("/health")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in resp.headers

    def test_headers_present_on_error(self, client):
        resp = client.get("/v1/traces/nonexistent-id")
        assert resp.status_code == 404
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in resp.headers


class TestLegacyApiDeprecation:
    """Verify /api/ legacy routes carry deprecation headers."""

    def test_legacy_route_has_deprecation_header(self, client):
        resp = client.get("/api/traces")
        assert resp.headers.get("Deprecation") == "true"

    def test_v1_route_no_deprecation_header(self, client):
        resp = client.get("/v1/traces")
        assert "Deprecation" not in resp.headers

    def test_legacy_route_has_link_to_v1(self, client):
        resp = client.get("/api/traces")
        link = resp.headers.get("Link", "")
        assert "/v1/traces" in link
        assert 'rel="successor-version"' in link

    def test_legacy_post_has_deprecation(self, client):
        resp = client.post("/api/agent/run", json={"prompt": "hello"})
        assert resp.headers.get("Deprecation") == "true"
        link = resp.headers.get("Link", "")
        assert "/v1/agent/run" in link

    def test_v1_post_no_deprecation(self, client):
        resp = client.post("/v1/agent/run", json={"prompt": "hello"})
        assert "Deprecation" not in resp.headers

    def test_sunset_header_when_configured(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LEGACY_SUNSET", "2026-12-31")
        resp = client.get("/api/traces")
        assert resp.headers.get("Sunset") == "2026-12-31"

    def test_no_sunset_header_when_empty(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LEGACY_SUNSET", "")
        resp = client.get("/api/traces")
        assert "Sunset" not in resp.headers

    def test_health_no_deprecation(self, client):
        resp = client.get("/health")
        assert "Deprecation" not in resp.headers

    def test_dashboard_no_deprecation(self, client):
        resp = client.get("/dashboard")
        assert "Deprecation" not in resp.headers

    def test_nested_legacy_path_correct_link(self, client):
        resp = client.get("/api/critic/registry")
        link = resp.headers.get("Link", "")
        assert "/v1/critic/registry" in link
