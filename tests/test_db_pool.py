"""Tests for database connection pool configuration and health check."""

from unittest.mock import patch

from app.config import settings
from app.db import _build_engine_kwargs
from app.services.config_validator import validate


def _warnings(issues: list) -> list[str]:
    return [i.message for i in issues if i.level == "warning"]


def _errors(issues: list) -> list[str]:
    return [i.message for i in issues if i.level == "error"]


def _make_settings(**overrides):
    from tests.test_config_validator import _make_settings as _ms

    return _ms(**overrides)


class TestBuildEngineKwargs:
    """Verify _build_engine_kwargs produces correct pool parameters."""

    def test_sqlite_gets_check_same_thread(self):
        with patch.object(settings, "DATABASE_URL", "sqlite:///./test.db"):
            from app.db import _is_sqlite as mod_flag

            if mod_flag:
                kwargs = _build_engine_kwargs()
                assert kwargs.get("connect_args", {}).get("check_same_thread") is False
                assert "pool_size" not in kwargs

    def test_postgres_gets_pool_settings(self):
        with (
            patch("app.db._is_sqlite", False),
            patch.object(settings, "DB_POOL_SIZE", 8),
            patch.object(settings, "DB_MAX_OVERFLOW", 20),
            patch.object(settings, "DB_POOL_RECYCLE", 900),
            patch.object(settings, "DB_POOL_PRE_PING", True),
            patch.object(settings, "DB_POOL_TIMEOUT", 15),
        ):
            kwargs = _build_engine_kwargs()
            assert kwargs["pool_size"] == 8
            assert kwargs["max_overflow"] == 20
            assert kwargs["pool_recycle"] == 900
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["pool_timeout"] == 15
            assert "connect_args" not in kwargs

    def test_pool_recycle_zero_omits_key(self):
        with (
            patch("app.db._is_sqlite", False),
            patch.object(settings, "DB_POOL_RECYCLE", 0),
        ):
            kwargs = _build_engine_kwargs()
            assert "pool_recycle" not in kwargs

    def test_pool_recycle_nonzero_included(self):
        with (
            patch("app.db._is_sqlite", False),
            patch.object(settings, "DB_POOL_RECYCLE", 3600),
        ):
            kwargs = _build_engine_kwargs()
            assert kwargs["pool_recycle"] == 3600


class TestPoolConfigValidation:
    """Config validator catches invalid pool settings."""

    def test_pool_size_zero_error(self):
        s = _make_settings(DB_POOL_SIZE=0)
        errors = _errors(validate(s))
        assert any("DB_POOL_SIZE" in e for e in errors)

    def test_pool_size_negative_error(self):
        s = _make_settings(DB_POOL_SIZE=-5)
        errors = _errors(validate(s))
        assert any("DB_POOL_SIZE" in e for e in errors)

    def test_pool_size_valid_no_error(self):
        s = _make_settings(DB_POOL_SIZE=10)
        errors = _errors(validate(s))
        assert not any("DB_POOL_SIZE" in e for e in errors)

    def test_max_overflow_negative_error(self):
        s = _make_settings(DB_MAX_OVERFLOW=-1)
        errors = _errors(validate(s))
        assert any("DB_MAX_OVERFLOW" in e for e in errors)

    def test_max_overflow_zero_ok(self):
        s = _make_settings(DB_MAX_OVERFLOW=0)
        errors = _errors(validate(s))
        assert not any("DB_MAX_OVERFLOW" in e for e in errors)

    def test_pool_recycle_negative_error(self):
        s = _make_settings(DB_POOL_RECYCLE=-1)
        errors = _errors(validate(s))
        assert any("DB_POOL_RECYCLE" in e for e in errors)

    def test_pool_timeout_negative_error(self):
        s = _make_settings(DB_POOL_TIMEOUT=-10)
        errors = _errors(validate(s))
        assert any("DB_POOL_TIMEOUT" in e for e in errors)


class TestReadinessPoolInfo:
    """Readiness endpoint includes pool status."""

    def test_readiness_has_database_status(self, client):
        resp = client.get("/health/ready")
        data = resp.json()
        assert isinstance(data["checks"]["database"], dict)
        assert data["checks"]["database"]["status"] == "connected"

    def test_readiness_db_unreachable_status(self, client):
        with patch("app.main.SessionLocal") as mock_session_cls:
            mock_session_cls.side_effect = Exception("db gone")
            resp = client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["checks"]["database"]["status"] == "unreachable"


class TestDefaultSettings:
    """Verify default pool settings are reasonable."""

    def test_defaults(self):
        assert settings.DB_POOL_SIZE == 5
        assert settings.DB_MAX_OVERFLOW == 10
        assert settings.DB_POOL_RECYCLE == 1800
        assert settings.DB_POOL_PRE_PING is True
        assert settings.DB_POOL_TIMEOUT == 30
