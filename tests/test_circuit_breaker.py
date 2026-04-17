"""Tests for the LLM provider circuit breaker."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from app.config import settings
from app.core.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitOpenError,
    CircuitState,
    get_registry,
    reset_registry,
)
from app.core.llm.provider import generate, generate_stream, reset_clients


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(settings, "CB_FALLBACK_TO_MOCK", True)
    reset_clients()
    reset_registry()


class TestCircuitBreakerStates:
    def test_starts_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_stays_closed_below_threshold(self):
        config = CircuitBreakerConfig(failure_threshold=5)
        cb = CircuitBreaker("test", config)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_opens_at_threshold(self):
        config = CircuitBreakerConfig(failure_threshold=3)
        cb = CircuitBreaker("test", config)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_open_to_half_open_after_timeout(self):
        config = CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.05,
        )
        cb = CircuitBreaker("test", config)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request() is True

    def test_half_open_success_closes(self):
        config = CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        config = CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_in_closed_state_is_noop(self):
        cb = CircuitBreaker("test")
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_reset_clears_everything(self):
        config = CircuitBreakerConfig(failure_threshold=2)
        cb = CircuitBreaker("test", config)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True


class TestRollingWindow:
    def test_old_failures_expire(self):
        config = CircuitBreakerConfig(
            failure_threshold=3,
            rolling_window_seconds=0.05,
        )
        cb = CircuitBreaker("test", config)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.06)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_failures_within_window_accumulate(self):
        config = CircuitBreakerConfig(
            failure_threshold=3,
            rolling_window_seconds=10.0,
        )
        cb = CircuitBreaker("test", config)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN


class TestCircuitBreakerStatus:
    def test_get_status(self):
        config = CircuitBreakerConfig(failure_threshold=5)
        cb = CircuitBreaker("gemini", config)
        cb.record_failure()
        cb.record_failure()
        status = cb.get_status()
        assert status["name"] == "gemini"
        assert status["state"] == "closed"
        assert status["recent_failures"] == 2
        assert status["failure_threshold"] == 5


class TestCircuitBreakerRegistry:
    def test_creates_breakers_on_demand(self):
        reg = CircuitBreakerRegistry()
        cb = reg.get("gemini")
        assert cb.name == "gemini"
        assert cb.state == CircuitState.CLOSED
        assert reg.get("gemini") is cb

    def test_separate_breakers_per_provider(self):
        reg = CircuitBreakerRegistry()
        cb_g = reg.get("gemini")
        cb_o = reg.get("openai")
        assert cb_g is not cb_o
        assert cb_g.name == "gemini"
        assert cb_o.name == "openai"

    def test_get_all_status(self):
        reg = CircuitBreakerRegistry()
        reg.get("gemini")
        reg.get("openai")
        statuses = reg.get_all_status()
        assert len(statuses) == 2
        names = {s["name"] for s in statuses}
        assert names == {"gemini", "openai"}

    def test_reset_all(self):
        config = CircuitBreakerConfig(failure_threshold=1)
        reg = CircuitBreakerRegistry(config)
        cb = reg.get("test")
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        reg.reset_all()
        assert cb.state == CircuitState.CLOSED


class TestConcurrency:
    def test_thread_safe_failure_recording(self):
        config = CircuitBreakerConfig(
            failure_threshold=100,
            rolling_window_seconds=10.0,
        )
        cb = CircuitBreaker("test", config)
        errors: list[Exception] = []

        def record_many():
            try:
                for _ in range(50):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        status = cb.get_status()
        assert status["recent_failures"] <= 200

    def test_thread_safe_registry_access(self):
        reg = CircuitBreakerRegistry()
        results: list[CircuitBreaker] = []

        def get_breaker():
            results.append(reg.get("shared"))

        threads = [threading.Thread(target=get_breaker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r is results[0] for r in results)


class TestProviderFallback:
    def _fake_openai_response(self, text="fallback response"):
        usage = MagicMock()
        usage.total_tokens = 10
        message = MagicMock()
        message.content = text
        choice = MagicMock()
        choice.message = message
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = usage
        resp.model = "gpt-4o-mini"
        resp.model_dump = lambda: {}
        return resp

    def test_fallback_to_mock_when_circuit_open(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "CB_FALLBACK_TO_MOCK", True)

        cb = get_registry().get("gemini")
        for _ in range(10):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        result = generate("hello", model_id="gemini-2.0-flash")
        assert result.provider == "mock"

    def test_fallback_to_next_provider_when_circuit_open(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "g-key")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "o-key")

        cb = get_registry().get("gemini")
        for _ in range(10):
            cb.record_failure()

        mock_oai = MagicMock()
        mock_oai.chat.completions.create.return_value = self._fake_openai_response()

        with patch("openai.OpenAI", return_value=mock_oai):
            reset_clients()
            for _ in range(10):
                get_registry().get("gemini").record_failure()
            result = generate("hello", model_id="gemini-2.0-flash")

        assert result.provider == "openai"
        assert result.text == "fallback response"

    def test_circuit_open_error_when_no_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "CB_FALLBACK_TO_MOCK", False)

        cb = get_registry().get("gemini")
        for _ in range(10):
            cb.record_failure()

        with pytest.raises(CircuitOpenError) as exc_info:
            generate("hello", model_id="gemini-2.0-flash")
        assert "gemini" in str(exc_info.value)

    def test_successful_call_records_success(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")

        mock_client = MagicMock()
        resp = MagicMock()
        resp.text = "ok"
        resp.usage_metadata = None
        resp.model_dump = lambda: {}
        mock_client.models.generate_content.return_value = resp

        with patch("google.genai.Client", return_value=mock_client):
            reset_clients()
            result = generate("hello", model_id="gemini-2.0-flash")

        assert result.provider == "gemini"
        cb = get_registry().get("gemini")
        assert cb.state == CircuitState.CLOSED

    def test_failed_call_records_failure(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr(
            settings,
            "CB_FAILURE_THRESHOLD",
            100,
        )
        reset_registry()

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("boom")

        with patch("google.genai.Client", return_value=mock_client):
            reset_clients()
            with pytest.raises(RuntimeError):
                generate("hello", model_id="gemini-2.0-flash")

        cb = get_registry().get("gemini")
        status = cb.get_status()
        assert status["recent_failures"] >= 1


class TestStreamFallback:
    def test_stream_fallback_to_mock_when_open(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "CB_FALLBACK_TO_MOCK", True)

        cb = get_registry().get("gemini")
        for _ in range(10):
            cb.record_failure()

        chunks = list(generate_stream("hello", model_id="gemini-2.0-flash"))
        assert len(chunks) == 1
        assert chunks[0].is_final is True
        assert "mock" in chunks[0].text.lower() or "pipeline" in chunks[0].text.lower()

    def test_stream_circuit_open_error_no_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "CB_FALLBACK_TO_MOCK", False)

        cb = get_registry().get("gemini")
        for _ in range(10):
            cb.record_failure()

        with pytest.raises(CircuitOpenError):
            list(generate_stream("hello", model_id="gemini-2.0-flash"))


class TestMockProviderBypassesCircuitBreaker:
    def test_mock_never_trips_circuit(self):
        result = generate("hello")
        assert result.provider == "mock"
        statuses = get_registry().get_all_status()
        assert len(statuses) == 0
