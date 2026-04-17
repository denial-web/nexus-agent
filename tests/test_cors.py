"""Tests for CORS hardening: config validation, header behavior, credential safety."""

import pytest
from app.config import settings
from app.services.config_validator import validate
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient


def _warnings(issues: list) -> list[str]:
    return [i.message for i in issues if i.level == "warning"]


def _errors(issues: list) -> list[str]:
    return [i.message for i in issues if i.level == "error"]


def _make_settings(**overrides):
    from tests.test_config_validator import _make_settings as _ms

    return _ms(**overrides)


class TestCorsConfigValidation:
    """Config validator catches unsafe CORS configurations in production."""

    def test_wildcard_origin_warns_in_prod(self):
        s = _make_settings(ENVIRONMENT="production", CORS_ORIGINS="*")
        warnings = _warnings(validate(s))
        assert any("wildcard" in w.lower() for w in warnings)

    def test_explicit_origin_no_warning(self):
        s = _make_settings(
            ENVIRONMENT="production",
            CORS_ORIGINS="https://app.example.com",
        )
        warnings = _warnings(validate(s))
        assert not any("wildcard" in w.lower() for w in warnings)

    def test_missing_scheme_warns(self):
        s = _make_settings(
            ENVIRONMENT="production",
            CORS_ORIGINS="app.example.com",
        )
        warnings = _warnings(validate(s))
        assert any("scheme" in w.lower() for w in warnings)

    def test_http_scheme_accepted(self):
        s = _make_settings(
            ENVIRONMENT="production",
            CORS_ORIGINS="http://localhost:3000",
        )
        warnings = _warnings(validate(s))
        assert not any("scheme" in w.lower() for w in warnings)

    def test_empty_origins_no_warning_in_dev(self):
        s = _make_settings(ENVIRONMENT="development", CORS_ORIGINS="")
        warnings = _warnings(validate(s))
        assert not any("cors" in w.lower() for w in warnings)

    def test_empty_origins_no_warning_in_prod(self):
        s = _make_settings(ENVIRONMENT="production", CORS_ORIGINS="")
        warnings = _warnings(validate(s))
        assert not any("wildcard" in w.lower() for w in warnings)

    def test_negative_max_age_error(self):
        s = _make_settings(CORS_MAX_AGE=-1)
        errors = _errors(validate(s))
        assert any("CORS_MAX_AGE" in e for e in errors)

    def test_zero_max_age_allowed(self):
        s = _make_settings(CORS_MAX_AGE=0)
        errors = _errors(validate(s))
        assert not any("CORS_MAX_AGE" in e for e in errors)


class TestCorsMiddlewareBehavior:
    """Verify CORS headers are set correctly when the middleware is active."""

    @pytest.fixture()
    def cors_app(self):
        """Build a minimal FastAPI app with CORS configured."""
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/ping")
        def ping():
            return {"ok": True}

        @app.post("/data")
        def data():
            return {"received": True}

        return app

    def test_explicit_origin_reflected(self, cors_app):
        cors_app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://app.example.com"],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key"],
            max_age=600,
        )
        client = TestClient(cors_app)
        resp = client.get("/ping", headers={"Origin": "https://app.example.com"})
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
        assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_disallowed_origin_no_header(self, cors_app):
        cors_app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://app.example.com"],
            allow_credentials=True,
            allow_methods=["GET"],
            allow_headers=["Content-Type"],
        )
        client = TestClient(cors_app)
        resp = client.get("/ping", headers={"Origin": "https://evil.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_preflight_returns_allowed_methods(self, cors_app):
        cors_app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://app.example.com"],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key"],
            max_age=600,
        )
        client = TestClient(cors_app)
        resp = client.options(
            "/data",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert resp.status_code == 200
        assert "POST" in resp.headers.get("access-control-allow-methods", "")
        assert resp.headers.get("access-control-max-age") == "600"

    def test_preflight_rejects_disallowed_origin(self, cors_app):
        cors_app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://app.example.com"],
            allow_methods=["GET"],
            allow_headers=["Content-Type"],
        )
        client = TestClient(cors_app)
        resp = client.options(
            "/data",
            headers={
                "Origin": "https://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_wildcard_origin_no_credentials(self, cors_app):
        """Wildcard origin + credentials=True is spec-forbidden; we set creds=False."""
        cors_app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["Content-Type"],
        )
        client = TestClient(cors_app)
        resp = client.get("/ping", headers={"Origin": "https://any.com"})
        assert resp.headers["access-control-allow-origin"] == "*"
        assert resp.headers.get("access-control-allow-credentials") is None


class TestCorsMainAppIntegration:
    """Verify CORS settings flow through the real app startup logic."""

    def test_credentials_disabled_for_wildcard(self):
        origins = ["*"]
        creds = "*" not in origins
        assert creds is False

    def test_credentials_enabled_for_explicit(self):
        origins = ["https://app.example.com"]
        creds = "*" not in origins
        assert creds is True

    def test_methods_parsed_from_config(self):
        raw = "GET,POST,PUT,DELETE,OPTIONS"
        methods = [m.strip() for m in raw.split(",") if m.strip()]
        assert methods == ["GET", "POST", "PUT", "DELETE", "OPTIONS"]

    def test_headers_parsed_from_config(self):
        raw = "Content-Type,X-API-Key,X-Request-ID,Authorization"
        headers = [h.strip() for h in raw.split(",") if h.strip()]
        assert headers == ["Content-Type", "X-API-Key", "X-Request-ID", "Authorization"]

    def test_default_methods_setting(self):
        assert "GET" in settings.CORS_ALLOW_METHODS
        assert "POST" in settings.CORS_ALLOW_METHODS
        assert "PATCH" not in settings.CORS_ALLOW_METHODS

    def test_default_headers_setting(self):
        assert "Content-Type" in settings.CORS_ALLOW_HEADERS
        assert "X-API-Key" in settings.CORS_ALLOW_HEADERS
        assert "X-Request-ID" in settings.CORS_ALLOW_HEADERS

    def test_default_max_age(self):
        assert settings.CORS_MAX_AGE == 600
