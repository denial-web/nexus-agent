"""Tests for deep health check provider probes."""

from unittest.mock import patch

from app.config import settings
from app.services.health_probe import (
    _probe_deepseek,
    _probe_gemini,
    _probe_ollama,
    _probe_openai,
    probe_providers,
)


class TestProbeGemini:
    def test_reachable(self):
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"models":[]}'

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = _probe_gemini(5.0)
        assert result["reachable"] is True
        assert result["status"] == 200
        assert "latency_s" in result

    def test_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = _probe_gemini(5.0)
        assert result["reachable"] is False
        assert "error" in result
        assert "latency_s" in result


class TestProbeOpenAI:
    def test_reachable(self):
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"data":[]}'

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = _probe_openai(5.0)
        assert result["reachable"] is True

    def test_auth_failure(self):
        import urllib.error

        err = urllib.error.HTTPError(
            "https://api.openai.com/v1/models", 401, "Unauthorized", {}, None,
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = _probe_openai(5.0)
        assert result["reachable"] is False
        assert "401" in result.get("error", "")


class TestProbeDeepSeek:
    def test_reachable(self):
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = _probe_deepseek(5.0)
        assert result["reachable"] is True

    def test_timeout(self):

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = _probe_deepseek(1.0)
        assert result["reachable"] is False
        assert "timed out" in result.get("error", "")


class TestProbeOllama:
    def test_reachable(self):
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = _probe_ollama(5.0)
        assert result["reachable"] is True

    def test_connection_refused(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            result = _probe_ollama(5.0)
        assert result["reachable"] is False


class TestProbeProviders:
    def test_no_providers_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")
        result = probe_providers(timeout=1.0)
        assert result == {}

    def test_single_provider_gemini(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = probe_providers(timeout=5.0)
        assert "gemini" in result
        assert result["gemini"]["reachable"] is True
        assert "openai" not in result

    def test_multiple_providers(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "gk")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "ok")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = probe_providers(timeout=5.0)
        assert "gemini" in result
        assert "openai" in result
        assert len(result) == 2

    def test_uses_settings_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "gk")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")
        monkeypatch.setattr(settings, "HEALTH_PROBE_TIMEOUT", 2.0)

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            result = probe_providers()
        assert "gemini" in result

    def test_mixed_reachable_unreachable(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "gk")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "ok")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        call_count = {"n": 0}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"

        def side_effect(req, **kwargs):
            call_count["n"] += 1
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "openai" in url:
                raise ConnectionError("refused")
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = probe_providers(timeout=5.0)
        assert result["gemini"]["reachable"] is True
        assert result["openai"]["reachable"] is False


class TestReadinessDeepEndpoint:
    def test_shallow_check_no_probes(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "provider_probes" not in data.get("checks", {})

    def test_deep_check_includes_probes(self, client, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        resp = client.get("/health/ready?deep=true")
        data = resp.json()
        assert "provider_probes" in data["checks"]
        assert data["checks"]["provider_probes"] == {}

    def test_deep_check_with_provider(self, client, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            resp = client.get("/health/ready?deep=true")
        data = resp.json()
        probes = data["checks"]["provider_probes"]
        assert "gemini" in probes
        assert probes["gemini"]["reachable"] is True

    def test_deep_check_unreachable_warning(self, client, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            resp = client.get("/health/ready?deep=true")
        data = resp.json()
        probes = data["checks"]["provider_probes"]
        assert probes["gemini"]["reachable"] is False
        assert "provider_probes_warning" in data["checks"]
        assert "gemini" in data["checks"]["provider_probes_warning"]

    def test_deep_false_same_as_shallow(self, client):
        resp = client.get("/health/ready?deep=false")
        data = resp.json()
        assert "provider_probes" not in data.get("checks", {})

    def test_deep_does_not_affect_overall_status(self, client, monkeypatch):
        """Unreachable providers don't make overall status 'degraded'."""
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "")

        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            resp = client.get("/health/ready?deep=true")
        data = resp.json()
        assert data["status"] == "ready"
        assert resp.status_code == 200


class TestConfigValidation:
    def test_zero_probe_timeout(self, monkeypatch):
        from app.services.config_validator import validate

        monkeypatch.setattr(settings, "HEALTH_PROBE_TIMEOUT", 0.0)
        issues = validate(settings)
        messages = [i.message for i in issues]
        assert any("HEALTH_PROBE_TIMEOUT" in m for m in messages)

    def test_negative_probe_timeout(self, monkeypatch):
        from app.services.config_validator import validate

        monkeypatch.setattr(settings, "HEALTH_PROBE_TIMEOUT", -1.0)
        issues = validate(settings)
        messages = [i.message for i in issues]
        assert any("HEALTH_PROBE_TIMEOUT" in m for m in messages)
