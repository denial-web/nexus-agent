"""Tests for startup configuration validation rules."""

from __future__ import annotations

import os
from unittest.mock import patch

from app.config import Settings
from app.services.config_validator import validate


def _make_settings(**overrides: object) -> Settings:
    """Create a Settings instance with sensible defaults, overriding specified fields."""
    defaults = {
        "ENVIRONMENT": "development",
        "NEXUS_API_KEY": "",
        "SESSION_SECRET": "",
        "DATABASE_URL": "sqlite:///./test.db",
        "MCP_ENABLED": False,
        "LOCAL_ONLY": False,
        "OTEL_ENABLED": False,
        "OTEL_SAMPLE_RATE": 1.0,
        "WEBHOOKS_ENABLED": False,
        "RATE_LIMIT_RPM": 30,
        "MAX_PROMPT_LENGTH": 50_000,
        "MAX_REQUEST_BODY_BYTES": 10_485_760,
        "REQUEST_TIMEOUT_SECONDS": 120.0,
        "SHUTDOWN_DRAIN_SECONDS": 30.0,
        "APPROVAL_QUORUM": 2,
        "APPROVAL_REVIEWERS": "",
        "CB_FAILURE_THRESHOLD": 5,
        "AGENT_MAX_STEPS": 15,
        "GEMINI_API_KEY": "",
        "OPENAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "LOCAL_HF_MODEL_ID": "",
        "REDIS_URL": "",
        "CORS_ORIGINS": "",
        "CORS_MAX_AGE": 600,
        "DB_POOL_SIZE": 5,
        "DB_MAX_OVERFLOW": 10,
        "DB_POOL_RECYCLE": 1800,
        "DB_POOL_PRE_PING": True,
        "DB_POOL_TIMEOUT": 30,
    }
    defaults.update(overrides)
    s = Settings()
    for k, v in defaults.items():
        object.__setattr__(s, k, v)
    return s


def _errors(issues: list) -> list[str]:
    return [i.message for i in issues if i.level == "error"]


def _warnings(issues: list) -> list[str]:
    return [i.message for i in issues if i.level == "warning"]


class TestSecurityChecks:
    def test_dev_no_errors(self):
        s = _make_settings(ENVIRONMENT="development")
        issues = validate(s)
        assert not _errors(issues)

    def test_prod_missing_api_key(self):
        s = _make_settings(ENVIRONMENT="production", NEXUS_API_KEY="", SESSION_SECRET="ok")
        errors = _errors(validate(s))
        assert any("NEXUS_API_KEY" in e for e in errors)

    def test_prod_missing_session_secret(self):
        s = _make_settings(ENVIRONMENT="production", NEXUS_API_KEY="key", SESSION_SECRET="")
        errors = _errors(validate(s))
        assert any("SESSION_SECRET" in e for e in errors)

    def test_prod_both_missing(self):
        s = _make_settings(ENVIRONMENT="production", NEXUS_API_KEY="", SESSION_SECRET="")
        errors = _errors(validate(s))
        assert len(errors) >= 3

    def test_prod_all_set_no_security_errors(self):
        s = _make_settings(
            ENVIRONMENT="production",
            NEXUS_API_KEY="key",
            SESSION_SECRET="secret",
            APPROVAL_REVIEWERS="alice,bob",
        )
        errors = _errors(validate(s))
        security_errors = [
            e for e in errors if "NEXUS_API_KEY" in e or "SESSION_SECRET" in e or "APPROVAL_REVIEWERS" in e
        ]
        assert not security_errors

    def test_prod_missing_approval_reviewers(self):
        s = _make_settings(
            ENVIRONMENT="production",
            NEXUS_API_KEY="key",
            SESSION_SECRET="secret",
            APPROVAL_REVIEWERS="",
        )
        errors = _errors(validate(s))
        assert any("APPROVAL_REVIEWERS" in e for e in errors)

    def test_beta_missing_approval_reviewers(self):
        s = _make_settings(
            ENVIRONMENT="beta",
            NEXUS_API_KEY="key",
            SESSION_SECRET="secret",
            APPROVAL_REVIEWERS="",
        )
        errors = _errors(validate(s))
        assert any("APPROVAL_REVIEWERS" in e for e in errors)


class TestContradictions:
    def test_mcp_with_local_only(self):
        s = _make_settings(MCP_ENABLED=True, LOCAL_ONLY=True)
        errors = _errors(validate(s))
        assert any("MCP_ENABLED" in e and "LOCAL_ONLY" in e for e in errors)

    def test_mcp_without_local_only_ok(self):
        s = _make_settings(MCP_ENABLED=True, LOCAL_ONLY=False)
        errors = _errors(validate(s))
        assert not any("MCP_ENABLED" in e for e in errors)

    def test_otel_with_local_only_warns(self):
        s = _make_settings(OTEL_ENABLED=True, LOCAL_ONLY=True)
        warnings = _warnings(validate(s))
        assert any("OTEL_ENABLED" in w for w in warnings)

    def test_webhooks_with_local_only_warns(self):
        s = _make_settings(WEBHOOKS_ENABLED=True, LOCAL_ONLY=True)
        warnings = _warnings(validate(s))
        assert any("WEBHOOKS_ENABLED" in w for w in warnings)


class TestNumericBounds:
    def test_otel_sample_rate_too_high(self):
        s = _make_settings(OTEL_SAMPLE_RATE=1.5)
        errors = _errors(validate(s))
        assert any("OTEL_SAMPLE_RATE" in e for e in errors)

    def test_otel_sample_rate_negative(self):
        s = _make_settings(OTEL_SAMPLE_RATE=-0.1)
        errors = _errors(validate(s))
        assert any("OTEL_SAMPLE_RATE" in e for e in errors)

    def test_otel_sample_rate_valid_boundary(self):
        for rate in (0.0, 0.5, 1.0):
            s = _make_settings(OTEL_SAMPLE_RATE=rate)
            errors = _errors(validate(s))
            assert not any("OTEL_SAMPLE_RATE" in e for e in errors)

    def test_negative_rate_limit(self):
        s = _make_settings(RATE_LIMIT_RPM=-1)
        errors = _errors(validate(s))
        assert any("RATE_LIMIT_RPM" in e for e in errors)

    def test_negative_max_prompt_length(self):
        s = _make_settings(MAX_PROMPT_LENGTH=-100)
        errors = _errors(validate(s))
        assert any("MAX_PROMPT_LENGTH" in e for e in errors)

    def test_negative_max_request_body_bytes(self):
        s = _make_settings(MAX_REQUEST_BODY_BYTES=-1)
        errors = _errors(validate(s))
        assert any("MAX_REQUEST_BODY_BYTES" in e for e in errors)

    def test_low_approval_quorum_warns(self):
        s = _make_settings(APPROVAL_QUORUM=0)
        warnings = _warnings(validate(s))
        assert any("APPROVAL_QUORUM" in w for w in warnings)

    def test_low_cb_threshold_warns(self):
        s = _make_settings(CB_FAILURE_THRESHOLD=0)
        warnings = _warnings(validate(s))
        assert any("CB_FAILURE_THRESHOLD" in w for w in warnings)

    def test_zero_agent_steps_warns(self):
        s = _make_settings(AGENT_MAX_STEPS=0)
        warnings = _warnings(validate(s))
        assert any("AGENT_MAX_STEPS" in w for w in warnings)


class TestTimeouts:
    def test_negative_request_timeout(self):
        s = _make_settings(REQUEST_TIMEOUT_SECONDS=-1.0)
        errors = _errors(validate(s))
        assert any("REQUEST_TIMEOUT_SECONDS" in e for e in errors)

    def test_negative_drain_seconds(self):
        s = _make_settings(SHUTDOWN_DRAIN_SECONDS=-5.0)
        errors = _errors(validate(s))
        assert any("SHUTDOWN_DRAIN_SECONDS" in e for e in errors)

    def test_drain_less_than_request_timeout_warns(self):
        s = _make_settings(REQUEST_TIMEOUT_SECONDS=120.0, SHUTDOWN_DRAIN_SECONDS=10.0)
        warnings = _warnings(validate(s))
        assert any("SHUTDOWN_DRAIN_SECONDS" in w for w in warnings)

    def test_drain_equals_request_timeout_no_warn(self):
        s = _make_settings(REQUEST_TIMEOUT_SECONDS=30.0, SHUTDOWN_DRAIN_SECONDS=30.0)
        warnings = _warnings(validate(s))
        assert not any("SHUTDOWN_DRAIN_SECONDS" in w and "REQUEST_TIMEOUT" in w for w in warnings)

    @patch.dict(os.environ, {"GUNICORN_TIMEOUT": "60"})
    def test_gunicorn_timeout_less_than_request_timeout_warns(self):
        s = _make_settings(REQUEST_TIMEOUT_SECONDS=120.0)
        warnings = _warnings(validate(s))
        assert any("GUNICORN_TIMEOUT" in w for w in warnings)

    @patch.dict(os.environ, {"GUNICORN_TIMEOUT": "180"})
    def test_gunicorn_timeout_sufficient_no_warn(self):
        s = _make_settings(REQUEST_TIMEOUT_SECONDS=120.0)
        warnings = _warnings(validate(s))
        assert not any("GUNICORN_TIMEOUT" in w for w in warnings)


class TestProviders:
    def test_no_providers_warns(self):
        s = _make_settings(
            GEMINI_API_KEY="",
            OPENAI_API_KEY="",
            DEEPSEEK_API_KEY="",
            LOCAL_HF_MODEL_ID="",
            LOCAL_ONLY=False,
        )
        warnings = _warnings(validate(s))
        assert any("No LLM provider keys" in w for w in warnings)

    def test_one_provider_set_no_warn(self):
        s = _make_settings(OPENAI_API_KEY="sk-test")
        warnings = _warnings(validate(s))
        assert not any("No LLM provider keys" in w for w in warnings)

    def test_local_only_no_local_model_warns(self):
        s = _make_settings(LOCAL_ONLY=True, LOCAL_HF_MODEL_ID="")
        warnings = _warnings(validate(s))
        assert any("LOCAL_ONLY" in w and "LOCAL_HF_MODEL_ID" in w for w in warnings)

    def test_local_only_with_local_model_ok(self):
        s = _make_settings(LOCAL_ONLY=True, LOCAL_HF_MODEL_ID="my/model")
        warnings = _warnings(validate(s))
        assert not any("LOCAL_HF_MODEL_ID" in w for w in warnings)


class TestDatabase:
    def test_sqlite_in_prod_warns(self):
        s = _make_settings(
            ENVIRONMENT="production",
            DATABASE_URL="sqlite:///./nexus.db",
            NEXUS_API_KEY="key",
            SESSION_SECRET="secret",
            APPROVAL_REVIEWERS="alice",
        )
        warnings = _warnings(validate(s))
        assert any("SQLite" in w for w in warnings)

    def test_postgres_in_prod_no_warn(self):
        s = _make_settings(
            ENVIRONMENT="production",
            DATABASE_URL="postgresql://u:p@host/db",
            NEXUS_API_KEY="key",
            SESSION_SECRET="secret",
            APPROVAL_REVIEWERS="alice",
        )
        warnings = _warnings(validate(s))
        assert not any("SQLite" in w for w in warnings)


class TestMultiWorker:
    @patch.dict(os.environ, {"GUNICORN_WORKERS": "4"})
    def test_multi_worker_no_redis_warns(self):
        s = _make_settings(RATE_LIMIT_RPM=30, REDIS_URL="")
        warnings = _warnings(validate(s))
        assert any("REDIS_URL" in w for w in warnings)

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "4"})
    def test_multi_worker_with_redis_no_warn(self):
        s = _make_settings(RATE_LIMIT_RPM=30, REDIS_URL="redis://localhost:6379/0")
        warnings = _warnings(validate(s))
        assert not any("REDIS_URL" in w for w in warnings)

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "4", "NEXUS_SKIP_SCHEDULER": ""})
    def test_multi_worker_no_skip_scheduler_warns(self):
        s = _make_settings()
        warnings = _warnings(validate(s))
        assert any("NEXUS_SKIP_SCHEDULER" in w for w in warnings)

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "4", "NEXUS_SKIP_SCHEDULER": "1"})
    def test_multi_worker_skip_scheduler_no_warn(self):
        s = _make_settings()
        warnings = _warnings(validate(s))
        assert not any("NEXUS_SKIP_SCHEDULER" in w for w in warnings)

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "1"})
    def test_single_worker_no_multi_warnings(self):
        s = _make_settings(RATE_LIMIT_RPM=30, REDIS_URL="")
        warnings = _warnings(validate(s))
        assert not any("workers" in w.lower() for w in warnings)
