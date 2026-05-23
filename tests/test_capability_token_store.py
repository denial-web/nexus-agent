"""Tests for Redis/in-process capability token persistence."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.services.capability_token_store import (
    InProcessCapabilityTokenStore,
    RedisCapabilityTokenStore,
    StoredCapabilityToken,
    get_token_store,
    reset_token_store,
)


def _sample_token(token_id: str = "tok1", *, used: bool = False) -> StoredCapabilityToken:
    now = datetime.now(UTC)
    return StoredCapabilityToken(
        token_id=token_id,
        trace_id="trace-1",
        action_type="respond",
        scope={"action_hash": "abc"},
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        signature="deadbeef",
        used=used,
    )


class TestInProcessCapabilityTokenStore:
    def test_put_pop_round_trip(self):
        store = InProcessCapabilityTokenStore(max_tokens=10)
        token = _sample_token()
        store.put(token, ttl_seconds=60)
        popped = store.pop("tok1")
        assert popped is not None
        assert popped.token_id == "tok1"
        assert store.pop("tok1") is None

    def test_peek_does_not_remove(self):
        store = InProcessCapabilityTokenStore(max_tokens=10)
        token = _sample_token("peek-me")
        store.put(token, ttl_seconds=60)
        assert store.peek("peek-me") is not None
        assert store.pop("peek-me") is not None

    def test_reset_clears_tokens(self):
        store = InProcessCapabilityTokenStore(max_tokens=10)
        store.put(_sample_token("a"), ttl_seconds=60)
        store.reset()
        assert store.peek("a") is None

    def test_backend_type(self):
        store = InProcessCapabilityTokenStore(max_tokens=10)
        assert store.backend_type == "in_process"


class TestRedisCapabilityTokenStore:
    def test_pop_uses_lua_script(self):
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_script = MagicMock()
        mock_script.return_value = (
            '{"token_id":"tok1","trace_id":"trace-1","action_type":"respond",'
            '"scope":{},"issued_at":"2026-01-01T00:00:00+00:00",'
            '"expires_at":"2026-01-02T00:00:00+00:00","signature":"abc","used":false}'
        )
        mock_client.register_script.return_value = mock_script

        with patch("redis.from_url", return_value=mock_client):
            store = RedisCapabilityTokenStore("redis://localhost:6379/0")

        popped = store.pop("tok1")
        assert popped is not None
        assert popped.token_id == "tok1"
        mock_script.assert_called_once()

    def test_disconnected_put_is_noop(self):
        store = RedisCapabilityTokenStore.__new__(RedisCapabilityTokenStore)
        store._client = None
        store._pop_script = None
        store.put(_sample_token(), ttl_seconds=60)
        assert store.pop("tok1") is None


class TestCapabilityTokenStoreSingleton:
    def test_get_store_defaults_in_process(self):
        reset_token_store()
        store = get_token_store()
        assert store.backend_type == "in_process"
        reset_token_store()

    def test_get_store_uses_redis_when_configured(self):
        reset_token_store()
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.register_script.return_value = MagicMock()

        with patch("redis.from_url", return_value=mock_client):
            with patch("app.config.settings.REDIS_URL", "redis://localhost:6379/0"):
                store = get_token_store()
                assert store.backend_type == "redis"
        reset_token_store()


class TestCrossWorkerTokenRoundTrip:
    def test_issue_on_one_store_pop_on_another_via_redis_mock(self):
        """Simulate two workers sharing Redis by reusing the same mock client."""
        shared: dict[str, str] = {}

        mock_client = MagicMock()

        def _setex(key, ttl, value):
            shared[key] = value

        def _get(key):
            return shared.get(key)

        def _delete(*keys):
            for key in keys:
                shared.pop(key, None)

        mock_client.ping.return_value = True
        mock_client.setex.side_effect = _setex
        mock_client.get.side_effect = _get
        mock_client.delete.side_effect = _delete

        pop_script = MagicMock()

        def _pop(keys=None):
            key = keys[0]
            value = shared.pop(key, None)
            return value

        pop_script.side_effect = _pop
        mock_client.register_script.return_value = pop_script

        with patch("redis.from_url", return_value=mock_client):
            worker_a = RedisCapabilityTokenStore("redis://localhost:6379/0")
            worker_b = RedisCapabilityTokenStore("redis://localhost:6379/0")

        token = _sample_token("shared-tok")
        worker_a.put(token, ttl_seconds=60)
        popped = worker_b.pop("shared-tok")
        assert popped is not None
        assert popped.trace_id == "trace-1"
        assert worker_a.pop("shared-tok") is None
