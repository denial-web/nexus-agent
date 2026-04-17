"""
Request ID propagation tests.

Verifies the correlation ID flows from HTTP request through the pipeline
into LLM provider calls and back in the response.
"""

from unittest.mock import MagicMock, patch

from app.core.llm.models import LLMResponse
from app.core.llm.provider import (
    _correlation_headers,
    get_request_id,
)
from app.logging_config import request_id_var


class TestCorrelationHeaders:
    """Unit tests for the correlation header builder."""

    def test_headers_with_active_request_id(self):
        token = request_id_var.set("abc123")
        try:
            headers = _correlation_headers()
            assert headers == {"X-Request-ID": "abc123"}
        finally:
            request_id_var.reset(token)

    def test_headers_empty_with_default(self):
        token = request_id_var.set("-")
        try:
            headers = _correlation_headers()
            assert headers == {}
        finally:
            request_id_var.reset(token)

    def test_get_request_id_returns_context(self):
        token = request_id_var.set("req-xyz")
        try:
            assert get_request_id() == "req-xyz"
        finally:
            request_id_var.reset(token)


class TestMockResponseCarriesRequestId:
    """Mock provider sets request_id on LLMResponse."""

    def test_mock_response_includes_request_id(self):
        from app.core.llm.provider import _mock_response

        token = request_id_var.set("mock-req-1")
        try:
            resp = _mock_response("hello", 0.0)
            assert resp.request_id == "mock-req-1"
        finally:
            request_id_var.reset(token)

    def test_mock_response_none_when_no_request(self):
        from app.core.llm.provider import _mock_response

        token = request_id_var.set("-")
        try:
            resp = _mock_response("hello", 0.0)
            assert resp.request_id is None
        finally:
            request_id_var.reset(token)


class TestPipelineRequestIdPropagation:
    """End-to-end: HTTP request ID appears in LLM response."""

    def test_run_endpoint_propagates_request_id(self, client):
        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "What is Python?"},
            headers={"X-Request-ID": "e2e-test-123"},
        )
        assert resp.status_code == 200
        assert resp.headers["X-Request-ID"] == "e2e-test-123"

    def test_request_id_in_response_header(self, client):
        resp = client.post(
            "/v1/agent/run",
            json={"prompt": "Hello"},
        )
        assert resp.status_code == 200
        rid = resp.headers.get("X-Request-ID")
        assert rid is not None
        assert len(rid) > 0


class TestOpenAIExtraHeaders:
    """Verify extra_headers are passed to OpenAI SDK calls."""

    def test_openai_call_receives_correlation_header(self):
        token = request_id_var.set("oai-req-42")
        try:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "test response"
            mock_response.usage = MagicMock()
            mock_response.usage.total_tokens = 10
            mock_response.model = "gpt-4o-mini"
            mock_response.model_dump.return_value = {}
            mock_client.chat.completions.create.return_value = mock_response

            with patch(
                "app.core.llm.provider._get_openai_client",
                return_value=mock_client,
            ):
                from app.core.llm.provider import _call_openai

                _call_openai("hello", "gpt-4o-mini", "fake-key", None)

            call_kwargs = mock_client.chat.completions.create.call_args
            assert call_kwargs.kwargs.get("extra_headers") == {
                "X-Request-ID": "oai-req-42"
            }
        finally:
            request_id_var.reset(token)

    def test_deepseek_call_receives_correlation_header(self):
        token = request_id_var.set("ds-req-99")
        try:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "test"
            mock_response.usage = MagicMock()
            mock_response.usage.total_tokens = 5
            mock_response.model = "deepseek-chat"
            mock_response.model_dump.return_value = {}
            mock_client.chat.completions.create.return_value = mock_response

            with patch(
                "app.core.llm.provider._get_deepseek_client",
                return_value=mock_client,
            ):
                from app.core.llm.provider import _call_deepseek

                _call_deepseek("hello", "deepseek-chat", "fake-key", None)

            call_kwargs = mock_client.chat.completions.create.call_args
            assert call_kwargs.kwargs.get("extra_headers") == {
                "X-Request-ID": "ds-req-99"
            }
        finally:
            request_id_var.reset(token)

    def test_no_extra_headers_without_request_id(self):
        token = request_id_var.set("-")
        try:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "test"
            mock_response.usage = MagicMock()
            mock_response.usage.total_tokens = 5
            mock_response.model = "gpt-4o-mini"
            mock_response.model_dump.return_value = {}
            mock_client.chat.completions.create.return_value = mock_response

            with patch(
                "app.core.llm.provider._get_openai_client",
                return_value=mock_client,
            ):
                from app.core.llm.provider import _call_openai

                _call_openai("hello", "gpt-4o-mini", "fake-key", None)

            call_kwargs = mock_client.chat.completions.create.call_args
            assert "extra_headers" not in call_kwargs.kwargs
        finally:
            request_id_var.reset(token)


class TestLLMResponseRequestId:
    """The LLMResponse dataclass carries request_id."""

    def test_default_is_none(self):
        resp = LLMResponse(
            text="hi",
            model_id="mock",
            token_count=1,
            latency_ms=1.0,
            provider="mock",
        )
        assert resp.request_id is None

    def test_explicit_request_id(self):
        resp = LLMResponse(
            text="hi",
            model_id="mock",
            token_count=1,
            latency_ms=1.0,
            provider="mock",
            request_id="req-42",
        )
        assert resp.request_id == "req-42"
