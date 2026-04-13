"""Tests for the unified LLM provider (mocked providers)."""

from unittest.mock import MagicMock, patch

import pytest
from app.config import settings
from app.core.llm.provider import (
    _resolve_route,
    generate,
    generate_stream,
    mock_llm_text,
    reset_clients,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
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

    def test_deepseek_explicit_model_with_key(self, monkeypatch):
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-k")
        prov, model, key = _resolve_route("deepseek-chat")
        assert prov == "deepseek"
        assert model == "deepseek-chat"
        assert key == "ds-k"

    def test_deepseek_explicit_model_needs_key(self):
        prov, model, key = _resolve_route("deepseek-reasoner")
        assert prov == "mock"

    def test_deepseek_auto_select_after_others(self, monkeypatch):
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-k")
        prov, model, key = _resolve_route(None)
        assert prov == "deepseek"
        assert key == "ds-k"

    def test_auto_select_prefers_gemini_over_deepseek(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "g-k")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-k")
        prov, _, _ = _resolve_route(None)
        assert prov == "gemini"

    def test_auto_select_prefers_openai_over_deepseek(self, monkeypatch):
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-k")
        prov, _, _ = _resolve_route(None)
        assert prov == "openai"


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


class TestLocalProvider:
    def test_resolve_nexus_spin(self):
        prov, model, _ = _resolve_route("nexus-spin-v5.3")
        assert prov == "local"
        assert model == "nexus-spin-v5.3"

    def test_resolve_local_uses_config_model(self, monkeypatch):
        monkeypatch.setattr(settings, "LOCAL_HF_MODEL_ID", "org/nexus-custom")
        prov, model, _ = _resolve_route("local")
        assert prov == "local"
        assert model == "org/nexus-custom"

    def test_generate_stub_without_transformers(self):
        out = generate("hello local", model_id="local:test-model")
        assert out.provider == "local"
        assert out.model_id == "test-model"
        assert "local_stub" in out.text


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
            401,
            {"error": {"code": 401, "status": "UNAUTHENTICATED"}},
            None,
        )

        with patch("google.genai.Client", return_value=mock_client):
            reset_clients()
            with pytest.raises(genai_errors.ClientError):
                generate("x", model_id="gemini-2.0-flash")

        assert mock_client.models.generate_content.call_count == 1


class TestDeepSeekProvider:
    def _fake_response(self, text="deepseek says hi", total_tokens=80, model="deepseek-chat"):
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
        resp.model_dump = lambda: {"id": "cmpl-ds"}
        return resp

    def test_generate(self, monkeypatch):
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-fake")

        mock_ds = MagicMock()
        mock_ds.chat.completions.create.return_value = self._fake_response()

        with patch("openai.OpenAI", return_value=mock_ds):
            reset_clients()
            out = generate("hi", model_id="deepseek-chat")

        assert out.provider == "deepseek"
        assert out.token_count == 80
        assert out.text == "deepseek says hi"
        mock_ds.chat.completions.create.assert_called_once()

    def test_retries_on_rate_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-fake")
        import openai

        mock_ds = MagicMock()
        rate_err = openai.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        mock_ds.chat.completions.create.side_effect = [
            rate_err,
            self._fake_response("ok after retry", 5),
        ]

        with patch("openai.OpenAI", return_value=mock_ds):
            reset_clients()
            with patch("app.core.llm.provider._sleep_backoff"):
                out = generate("x", model_id="deepseek-chat")

        assert out.text == "ok after retry"
        assert mock_ds.chat.completions.create.call_count == 2

    def test_uses_custom_base_url(self, monkeypatch):
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "ds-fake")
        monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://custom.deepseek.test")

        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = self._fake_response()
            mock_openai_cls.return_value = mock_client
            reset_clients()
            generate("hi", model_id="deepseek-chat")

            mock_openai_cls.assert_called_with(
                api_key="ds-fake",
                base_url="https://custom.deepseek.test",
            )


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
