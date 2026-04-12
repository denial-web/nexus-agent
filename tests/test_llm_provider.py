"""Tests for the unified LLM provider (mocked providers)."""
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.core.llm.provider import (
    generate,
    generate_stream,
    mock_llm_text,
    reset_clients,
    _resolve_route,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    reset_clients()


class TestResolveRoute:
    def test_mock_when_no_keys(self):
        assert _resolve_route(None) == ("mock", "mock", "")

    def test_gemini_when_key_set(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
        prov, model, key = _resolve_route(None)
        assert prov == "gemini"
        assert key == "k"

    def test_openai_explicit_model_needs_key(self, monkeypatch):
        prov, model, key = _resolve_route("gpt-4o")
        assert prov == "mock"

    def test_openai_explicit_model_with_key(self, monkeypatch):
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-x")
        prov, model, key = _resolve_route("gpt-4o")
        assert prov == "openai"
        assert model == "gpt-4o"

    def test_gemini_explicit_model_needs_key(self, monkeypatch):
        prov, model, key = _resolve_route("gemini-2.0-flash")
        assert prov == "mock"

    def test_finetuned_routes_to_openai(self, monkeypatch):
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-x")
        prov, model, _ = _resolve_route("ft:gpt-4o:my-org:custom:id")
        assert prov == "openai"
        assert model.startswith("ft:")


class TestMockFallback:
    def test_generate_returns_mock(self):
        out = generate("hello world")
        assert out.provider == "mock"
        assert out.model_id == "mock"
        assert out.token_count == 0
        assert out.latency_ms >= 0

    def test_generate_stream_returns_single_chunk(self):
        chunks = list(generate_stream("hello"))
        assert len(chunks) == 1
        assert chunks[0].is_final is True
        assert chunks[0].text == mock_llm_text("hello")


class TestGeminiProvider:
    def _fake_response(self, text="hello from gemini", token_count=42):
        resp = MagicMock()
        resp.text = f"  {text}  "
        um = MagicMock()
        um.total_token_count = token_count
        resp.usage_metadata = um
        resp.model_dump = lambda: {"text": text}
        return resp

    def test_generate(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._fake_response()

        with patch("google.genai.Client", return_value=mock_client):
            reset_clients()
            out = generate("prompt", model_id="gemini-2.0-flash")

        assert out.provider == "gemini"
        assert out.model_id == "gemini-2.0-flash"
        assert out.token_count == 42
        assert out.text == "hello from gemini"
        mock_client.models.generate_content.assert_called_once()

    def test_retries_on_rate_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key")
        from google.genai import errors as genai_errors

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            genai_errors.ClientError(429, {"error": {"code": 429}}, None),
            self._fake_response("ok", 10),
        ]

        with patch("google.genai.Client", return_value=mock_client):
            reset_clients()
            with patch("app.core.llm.provider._sleep_backoff"):
                out = generate("x", model_id="gemini-2.0-flash")

        assert out.text == "ok"
        assert mock_client.models.generate_content.call_count == 2

    def test_no_retry_on_auth_error(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "bad-key")
        from google.genai import errors as genai_errors

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = genai_errors.ClientError(
            401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}, None,
        )

        with patch("google.genai.Client", return_value=mock_client):
            reset_clients()
            with pytest.raises(genai_errors.ClientError):
                generate("x", model_id="gemini-2.0-flash")

        assert mock_client.models.generate_content.call_count == 1


class TestOpenAIProvider:
    def _fake_response(self, text="openai says hi", total_tokens=100, model="gpt-4o-mini"):
        usage = MagicMock()
        usage.total_tokens = total_tokens
        message = MagicMock()
        message.content = f"  {text}  "
        choice = MagicMock()
        choice.message = message
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = usage
        resp.model = model
        resp.model_dump = lambda: {"id": "cmpl-1"}
        return resp

    def test_generate(self, monkeypatch):
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-fake")
        monkeypatch.setattr(settings, "OPENAI_MODEL", "gpt-4o-mini")

        mock_oai = MagicMock()
        mock_oai.chat.completions.create.return_value = self._fake_response()

        with patch("openai.OpenAI", return_value=mock_oai):
            reset_clients()
            out = generate("hi", model_id="gpt-4o-mini")

        assert out.provider == "openai"
        assert out.token_count == 100
        assert out.text == "openai says hi"
        mock_oai.chat.completions.create.assert_called_once()

    def test_retries_on_rate_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-fake")
        import openai

        mock_oai = MagicMock()
        rate_err = openai.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        mock_oai.chat.completions.create.side_effect = [
            rate_err,
            self._fake_response("ok after retry", 5),
        ]

        with patch("openai.OpenAI", return_value=mock_oai):
            reset_clients()
            with patch("app.core.llm.provider._sleep_backoff"):
                out = generate("x", model_id="gpt-4o-mini")

        assert out.text == "ok after retry"
        assert mock_oai.chat.completions.create.call_count == 2

    def test_no_retry_on_auth_error(self, monkeypatch):
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-bad")
        import openai

        mock_oai = MagicMock()
        auth_err = openai.AuthenticationError(
            message="invalid api key",
            response=MagicMock(status_code=401, headers={}),
            body=None,
        )
        mock_oai.chat.completions.create.side_effect = auth_err

        with patch("openai.OpenAI", return_value=mock_oai):
            reset_clients()
            with pytest.raises(openai.AuthenticationError):
                generate("x", model_id="gpt-4o-mini")

        assert mock_oai.chat.completions.create.call_count == 1
