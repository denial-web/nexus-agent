"""Tests for idempotency key support."""

import time

from app.services.idempotency import (
    CachedResponse,
    InProcessStore,
    RedisStore,
    get_store,
    reset_store,
)

# ---------------------------------------------------------------------------
# InProcessStore
# ---------------------------------------------------------------------------

class TestInProcessStore:
    def test_set_and_get(self):
        store = InProcessStore(max_keys=100, ttl_seconds=60)
        cr = CachedResponse(
            status_code=200, body=b'{"ok":true}',
            content_type="application/json", created_at=time.time(),
        )
        store.set("key1", cr)
        result = store.get("key1")
        assert result is not None
        assert result.status_code == 200
        assert result.body == b'{"ok":true}'

    def test_get_missing_key(self):
        store = InProcessStore()
        assert store.get("nonexistent") is None

    def test_ttl_expiration(self):
        store = InProcessStore(ttl_seconds=1)
        cr = CachedResponse(
            status_code=200, body=b"ok",
            content_type="text/plain", created_at=time.time() - 2,
        )
        store.set("expired", cr)
        assert store.get("expired") is None

    def test_lru_eviction(self):
        store = InProcessStore(max_keys=2, ttl_seconds=60)
        now = time.time()
        for i in range(3):
            store.set(f"k{i}", CachedResponse(
                status_code=200, body=f"v{i}".encode(),
                content_type="text/plain", created_at=now,
            ))
        assert store.get("k0") is None
        assert store.get("k1") is not None
        assert store.get("k2") is not None

    def test_reset_clears_all(self):
        store = InProcessStore()
        store.set("a", CachedResponse(
            status_code=200, body=b"x",
            content_type="text/plain", created_at=time.time(),
        ))
        assert store.size == 1
        store.reset()
        assert store.size == 0

    def test_backend_type(self):
        store = InProcessStore()
        assert store.backend_type == "in_process"

    def test_move_to_end_on_get(self):
        store = InProcessStore(max_keys=2, ttl_seconds=60)
        now = time.time()
        store.set("a", CachedResponse(200, b"a", "text/plain", now))
        store.set("b", CachedResponse(200, b"b", "text/plain", now))
        store.get("a")
        store.set("c", CachedResponse(200, b"c", "text/plain", now))
        assert store.get("a") is not None
        assert store.get("b") is None
        assert store.get("c") is not None


# ---------------------------------------------------------------------------
# RedisStore (mocked)
# ---------------------------------------------------------------------------

class TestRedisStoreMocked:
    def _make_store(self):
        from unittest.mock import MagicMock

        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = MagicMock()
        return store

    def test_set_and_get(self):
        store = self._make_store()
        cr = CachedResponse(200, b'{"ok":true}', "application/json", 1000.0)

        store.set("key1", cr)
        store._client.setex.assert_called_once()
        call_args = store._client.setex.call_args
        assert call_args[0][0] == "nexus:idempotency:key1"
        assert call_args[0][1] == 60

        stored_json = call_args[0][2]
        store._client.get.return_value = stored_json.encode("utf-8")
        result = store.get("key1")
        assert result is not None
        assert result.status_code == 200
        assert result.body == b'{"ok":true}'

    def test_get_none_returns_none(self):
        store = self._make_store()
        store._client.get.return_value = None
        assert store.get("missing") is None

    def test_get_error_returns_none(self):
        store = self._make_store()
        store._client.get.side_effect = RuntimeError("conn lost")
        assert store.get("broken") is None

    def test_set_error_is_silent(self):
        store = self._make_store()
        store._client.setex.side_effect = RuntimeError("conn lost")
        cr = CachedResponse(200, b"ok", "text/plain", time.time())
        store.set("key1", cr)

    def test_backend_type_connected(self):
        store = self._make_store()
        assert store.backend_type == "redis"

    def test_backend_type_disconnected(self):
        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = None
        assert store.backend_type == "redis_disconnected"

    def test_connected_property(self):
        store = self._make_store()
        store._client.ping.return_value = True
        assert store.connected is True

    def test_connected_property_error(self):
        store = self._make_store()
        store._client.ping.side_effect = RuntimeError("fail")
        assert store.connected is False

    def test_reset_scans_and_deletes(self):
        store = self._make_store()
        store._client.scan.return_value = (0, [b"nexus:idempotency:k1"])
        store.reset()
        assert store._client.delete.call_count >= 1

    def test_get_with_none_client(self):
        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = None
        assert store.get("key") is None

    def test_set_with_none_client(self):
        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = None
        cr = CachedResponse(200, b"ok", "text/plain", time.time())
        store.set("key", cr)


# ---------------------------------------------------------------------------
# get_store singleton
# ---------------------------------------------------------------------------

class TestGetStore:
    def setup_method(self):
        reset_store()

    def teardown_method(self):
        reset_store()

    def test_returns_in_process_by_default(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.REDIS_URL", "")
        store = get_store()
        assert store.backend_type == "in_process"

    def test_singleton_pattern(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.REDIS_URL", "")
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2

    def test_reset_clears_singleton(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.REDIS_URL", "")
        s1 = get_store()
        reset_store()
        s2 = get_store()
        assert s1 is not s2


# ---------------------------------------------------------------------------
# IdempotencyMiddleware via API
# ---------------------------------------------------------------------------

class TestIdempotencyMiddleware:
    """Integration tests using the FastAPI TestClient."""

    def _run_pipeline(self, client, payload, key=None):
        headers = {}
        if key:
            headers["Idempotency-Key"] = key
        return client.post("/v1/agent/run", json=payload, headers=headers)

    def test_no_key_processes_normally(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        resp = self._run_pipeline(client, {"prompt": "hello"})
        assert resp.status_code == 200
        assert "X-Idempotent-Replayed" not in resp.headers

    def test_first_request_caches_response(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        reset_store()
        key = "test-key-12345678"
        resp = self._run_pipeline(client, {"prompt": "hello"}, key=key)
        assert resp.status_code == 200
        assert "X-Idempotent-Replayed" not in resp.headers
        trace_id = resp.json()["trace_id"]

        resp2 = self._run_pipeline(client, {"prompt": "hello"}, key=key)
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Idempotent-Replayed") == "true"
        assert resp2.json()["trace_id"] == trace_id

    def test_different_keys_different_responses(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        reset_store()
        r1 = self._run_pipeline(client, {"prompt": "hello"}, key="keyAAAA-1111")
        r2 = self._run_pipeline(client, {"prompt": "hello"}, key="keyBBBB-2222")
        assert r1.json()["trace_id"] != r2.json()["trace_id"]

    def test_key_too_short_rejected(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        resp = self._run_pipeline(client, {"prompt": "hello"}, key="short")
        assert resp.status_code == 400
        body = resp.json()
        error = body.get("error", body)
        if isinstance(error, dict):
            assert error.get("code") == "invalid_idempotency_key"
        else:
            assert "invalid_idempotency_key" in error

    def test_key_too_long_rejected(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        resp = self._run_pipeline(client, {"prompt": "hello"}, key="x" * 257)
        assert resp.status_code == 400

    def test_get_request_ignores_key(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        resp = client.get(
            "/health/ready",
            headers={"Idempotency-Key": "test-key-12345678"},
        )
        assert resp.status_code == 200
        assert "X-Idempotent-Replayed" not in resp.headers

    def test_non_idempotent_path_ignores_key(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        resp = client.post(
            "/v1/agent/feedback",
            json={"trace_id": "fake", "feedback": "good"},
            headers={"Idempotency-Key": "test-key-12345678"},
        )
        assert "X-Idempotent-Replayed" not in resp.headers

    def test_legacy_api_path_works(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        reset_store()
        key = "legacy-test-key-1"
        self._run_pipeline(
            client, {"prompt": "hello"},
        )
        resp = client.post(
            "/api/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 200
        resp2 = client.post(
            "/api/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert resp2.headers.get("X-Idempotent-Replayed") == "true"


# ---------------------------------------------------------------------------
# In-flight deduplication (InProcessStore)
# ---------------------------------------------------------------------------


class TestInProcessInflight:
    def test_acquire_and_release(self):
        store = InProcessStore()
        assert store.acquire_inflight("key1") is True
        assert store.inflight_count == 1
        store.release_inflight("key1")
        assert store.inflight_count == 0

    def test_double_acquire_fails(self):
        store = InProcessStore()
        assert store.acquire_inflight("key1") is True
        assert store.acquire_inflight("key1") is False

    def test_release_allows_reacquire(self):
        store = InProcessStore()
        store.acquire_inflight("key1")
        store.release_inflight("key1")
        assert store.acquire_inflight("key1") is True

    def test_different_keys_independent(self):
        store = InProcessStore()
        assert store.acquire_inflight("a") is True
        assert store.acquire_inflight("b") is True
        assert store.inflight_count == 2

    def test_release_nonexistent_is_safe(self):
        store = InProcessStore()
        store.release_inflight("nonexistent")

    def test_reset_clears_inflight(self):
        store = InProcessStore()
        store.acquire_inflight("key1")
        store.reset()
        assert store.inflight_count == 0
        assert store.acquire_inflight("key1") is True


# ---------------------------------------------------------------------------
# In-flight deduplication (RedisStore mocked)
# ---------------------------------------------------------------------------


class TestRedisInflight:
    def _make_store(self):
        from unittest.mock import MagicMock

        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = MagicMock()
        return store

    def test_acquire_calls_set_nx(self):
        store = self._make_store()
        store._client.set.return_value = True
        assert store.acquire_inflight("key1") is True
        store._client.set.assert_called_once()
        call_kwargs = store._client.set.call_args
        assert call_kwargs[1]["nx"] is True

    def test_acquire_returns_false_when_nx_fails(self):
        store = self._make_store()
        store._client.set.return_value = None
        assert store.acquire_inflight("key1") is False

    def test_release_calls_delete(self):
        store = self._make_store()
        store.release_inflight("key1")
        store._client.delete.assert_called_once_with(
            "nexus:idempotency:inflight:key1"
        )

    def test_acquire_with_none_client_returns_true(self):
        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = None
        assert store.acquire_inflight("key1") is True

    def test_release_with_none_client_is_safe(self):
        store = RedisStore.__new__(RedisStore)
        store._ttl = 60
        store._client = None
        store.release_inflight("key1")

    def test_acquire_error_returns_true(self):
        store = self._make_store()
        store._client.set.side_effect = RuntimeError("conn lost")
        assert store.acquire_inflight("key1") is True


# ---------------------------------------------------------------------------
# Middleware in-flight conflict (409)
# ---------------------------------------------------------------------------


class TestInflightMiddleware:
    def test_concurrent_duplicate_returns_409(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        reset_store()

        store = get_store()
        key = "inflight-test-key-1"
        store.acquire_inflight(key)

        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 409
        body = resp.json()
        error = body.get("error", body)
        if isinstance(error, dict):
            assert error.get("code") == "duplicate_request"
        else:
            assert "duplicate_request" in error
        assert resp.headers.get("Retry-After") == "2"

        store.release_inflight(key)

    def test_inflight_released_after_success(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        reset_store()

        key = "release-test-key-1"
        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 200

        store = get_store()
        assert store.acquire_inflight(key) is True
        store.release_inflight(key)

    def test_cached_response_skips_inflight(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.RATE_LIMIT_RPM", 0)
        reset_store()

        key = "cached-skip-inflight"
        client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )

        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Idempotent-Replayed") == "true"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestIdempotencyConfigValidation:
    def test_low_ttl_warning(self):
        from app.config import Settings
        from app.services.config_validator import validate
        s = Settings(IDEMPOTENCY_TTL=0)
        issues = validate(s)
        msgs = [i.message for i in issues]
        assert any("IDEMPOTENCY_TTL" in m for m in msgs)

    def test_zero_max_keys_error(self):
        from app.config import Settings
        from app.services.config_validator import validate
        s = Settings(IDEMPOTENCY_MAX_KEYS=0)
        issues = validate(s)
        errors = [i for i in issues if i.level == "error"]
        assert any("IDEMPOTENCY_MAX_KEYS" in e.message for e in errors)

    def test_default_values_no_idempotency_issues(self):
        from app.config import Settings
        from app.services.config_validator import validate
        s = Settings()
        issues = validate(s)
        msgs = [i.message for i in issues]
        assert not any("IDEMPOTENCY" in m for m in msgs)


# ---------------------------------------------------------------------------
# Middleware ordering: auth MUST run before idempotency
# ---------------------------------------------------------------------------

class TestAuthRunsBeforeIdempotency:
    """Regression: unauthenticated requests must never touch the idempotency store.

    Idempotency runs inside auth, so a missing/invalid X-API-Key short-circuits
    with 401 before the cache is consulted or an in-flight lock is acquired.
    """

    def test_unauthed_request_does_not_cache(self, client, monkeypatch):
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "NEXUS_API_KEY", "auth-order-key-1")
        monkeypatch.setattr(_settings, "RATE_LIMIT_RPM", 0)
        reset_store()

        key = "auth-order-test-key-1"
        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert resp.status_code == 401
        assert "X-Idempotent-Replayed" not in resp.headers

        store = get_store()
        assert store.get(key) is None
        assert store.acquire_inflight(key) is True
        store.release_inflight(key)

    def test_authed_after_unauthed_runs_fresh(self, client, monkeypatch):
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "NEXUS_API_KEY", "auth-order-key-2")
        monkeypatch.setattr(_settings, "RATE_LIMIT_RPM", 0)
        reset_store()

        key = "auth-order-test-key-2"
        bad = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert bad.status_code == 401

        good = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={
                "Idempotency-Key": key,
                "X-API-Key": "auth-order-key-2",
            },
        )
        assert good.status_code == 200
        assert "X-Idempotent-Replayed" not in good.headers

    def test_invalid_short_key_rejected_only_when_authed(self, client, monkeypatch):
        """Auth runs first: invalid-length key from unauthed client returns 401, not 400."""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "NEXUS_API_KEY", "auth-order-key-3")
        monkeypatch.setattr(_settings, "RATE_LIMIT_RPM", 0)

        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": "short"},
        )
        assert resp.status_code == 401
