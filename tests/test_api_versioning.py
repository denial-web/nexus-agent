"""Tests for API versioning headers and version endpoint."""

from unittest.mock import patch

from app.version import API_VERSION, get_version_info

# ---------------------------------------------------------------------------
# version module
# ---------------------------------------------------------------------------


class TestVersionModule:
    def test_api_version_is_semver(self):
        parts = API_VERSION.split(".")
        assert len(parts) == 3
        for p in parts:
            assert p.isdigit()

    def test_get_version_info_has_required_keys(self):
        info = get_version_info()
        assert "api_version" in info
        assert "project_version" in info
        assert "python_version" in info
        assert "platform" in info
        assert "git_sha" in info

    def test_api_version_matches(self):
        info = get_version_info()
        assert info["api_version"] == API_VERSION

    def test_python_version_present(self):
        info = get_version_info()
        assert info["python_version"]
        assert "." in info["python_version"]

    def test_git_sha_is_string_or_none(self):
        info = get_version_info()
        assert info["git_sha"] is None or isinstance(info["git_sha"], str)

    def test_git_sha_when_subprocess_fails(self):
        with patch("app.version.get_git_sha", return_value=None):
            info = get_version_info()
            info["git_sha"] = None
        assert info["git_sha"] is None


# ---------------------------------------------------------------------------
# X-API-Version header on every response
# ---------------------------------------------------------------------------


class TestVersionHeader:
    def test_health_has_version_header(self, client):
        resp = client.get("/health")
        assert resp.headers.get("X-API-Version") == API_VERSION

    def test_readiness_has_version_header(self, client):
        resp = client.get("/health/ready")
        assert resp.headers.get("X-API-Version") == API_VERSION

    def test_api_endpoint_has_version_header(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        resp = client.post("/v1/agent/run", json={"prompt": "hello"})
        assert resp.headers.get("X-API-Version") == API_VERSION

    def test_dashboard_has_version_header(self, client):
        resp = client.get("/dashboard")
        assert resp.headers.get("X-API-Version") == API_VERSION

    def test_404_has_version_header(self, client):
        resp = client.get("/nonexistent")
        assert resp.headers.get("X-API-Version") == API_VERSION


# ---------------------------------------------------------------------------
# Accept-Version mismatch header
# ---------------------------------------------------------------------------


class TestAcceptVersionMismatch:
    def test_no_mismatch_when_no_header(self, client):
        resp = client.get("/health")
        assert "X-API-Version-Mismatch" not in resp.headers

    def test_no_mismatch_when_matching(self, client):
        resp = client.get(
            "/health",
            headers={"Accept-Version": API_VERSION},
        )
        assert "X-API-Version-Mismatch" not in resp.headers

    def test_mismatch_when_different(self, client):
        resp = client.get(
            "/health",
            headers={"Accept-Version": "99.99.99"},
        )
        assert resp.headers.get("X-API-Version-Mismatch") == "true"
        assert resp.headers.get("X-API-Version") == API_VERSION

    def test_mismatch_with_empty_string(self, client):
        resp = client.get(
            "/health",
            headers={"Accept-Version": ""},
        )
        assert "X-API-Version-Mismatch" not in resp.headers


# ---------------------------------------------------------------------------
# /v1/agent/version endpoint
# ---------------------------------------------------------------------------


class TestVersionEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/v1/agent/version")
        assert resp.status_code == 200

    def test_contains_required_fields(self, client):
        data = client.get("/v1/agent/version").json()
        assert data["api_version"] == API_VERSION
        assert "project_version" in data
        assert "python_version" in data
        assert "platform" in data
        assert "git_sha" in data

    def test_legacy_route(self, client):
        resp = client.get("/api/agent/version")
        assert resp.status_code == 200
        assert resp.json()["api_version"] == API_VERSION
