"""Tests for multi-model generation and the /api/agent/compare endpoint."""

from unittest.mock import patch

import pytest
from app.config import settings
from app.core.llm.models import LLMResponse
from app.core.llm.provider import generate_multi, get_available_providers, reset_clients


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
    reset_clients()


def _mock_response(model_id: str = "mock", provider: str = "mock", text: str = "response") -> LLMResponse:
    return LLMResponse(
        text=text,
        model_id=model_id,
        token_count=10,
        latency_ms=50.0,
        provider=provider,
        raw_response=None,
    )


class TestGetAvailableProviders:
    def test_empty_when_no_keys(self):
        assert get_available_providers() == []

    def test_returns_gemini_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "gk")
        providers = get_available_providers()
        assert len(providers) == 1
        assert providers[0]["provider"] == "gemini"

    def test_returns_all_when_all_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "gk")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "ok")
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "dk")
        providers = get_available_providers()
        assert len(providers) == 3
        names = {p["provider"] for p in providers}
        assert names == {"gemini", "openai", "deepseek"}


class TestGenerateMulti:
    def test_falls_back_to_single_mock_when_no_providers(self):
        results = generate_multi("hello")
        assert len(results) == 1
        assert results[0].provider == "mock"

    def test_calls_each_model_id(self):
        with patch("app.core.llm.provider.generate") as mock_gen:
            mock_gen.side_effect = [
                _mock_response("model-a", "gemini", "response-a"),
                _mock_response("model-b", "openai", "response-b"),
            ]
            results = generate_multi("hello", model_ids=["model-a", "model-b"])

        assert len(results) == 2
        assert {r.model_id for r in results} == {"model-a", "model-b"}
        assert mock_gen.call_count == 2

    def test_skips_failed_calls(self):
        with patch("app.core.llm.provider.generate") as mock_gen:
            mock_gen.side_effect = [
                _mock_response("model-a", "gemini", "ok"),
                RuntimeError("provider down"),
            ]
            results = generate_multi("hello", model_ids=["model-a", "model-b"])

        assert len(results) == 1
        assert results[0].model_id == "model-a"

    def test_uses_configured_providers_when_no_model_ids(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "gk")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "ok")

        with patch("app.core.llm.provider.generate") as mock_gen:
            mock_gen.side_effect = [
                _mock_response(settings.GEMINI_MODEL, "gemini"),
                _mock_response(settings.OPENAI_MODEL, "openai"),
            ]
            results = generate_multi("hello")

        assert len(results) == 2

    def test_timeout_returns_partial_results(self, monkeypatch):
        import time

        monkeypatch.setattr(settings, "COMPARE_TIMEOUT_SECONDS", 0.05)

        def _slow_generate(prompt, model_id=None, system_prompt=None):
            if model_id == "model-slow":
                time.sleep(2)
            return _mock_response(model_id or "mock", "mock", "fast response")

        with patch("app.core.llm.provider.generate", side_effect=_slow_generate):
            results = generate_multi(
                "hello",
                model_ids=["model-fast", "model-slow"],
                timeout_seconds=0.05,
            )

        assert len(results) <= 2
        fast_ids = [r.model_id for r in results]
        assert "model-fast" in fast_ids

    def test_uses_settings_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "COMPARE_TIMEOUT_SECONDS", 99.0)

        with patch("app.core.llm.provider.generate") as mock_gen:
            mock_gen.return_value = _mock_response("m", "mock")
            with patch("concurrent.futures.as_completed") as mock_ac:
                mock_ac.return_value = iter([])
                generate_multi("hello", model_ids=["m"])
                mock_ac.assert_called_once()
                _, kwargs = mock_ac.call_args
                assert kwargs.get("timeout") == 99.0


class TestCompareEndpoint:
    @patch("app.core.llm.provider.generate")
    def test_compare_basic(self, mock_gen, client):
        mock_gen.side_effect = [
            _mock_response("model-a", "gemini", "great detailed analytical response"),
            _mock_response("model-b", "openai", "another detailed careful response here"),
        ]

        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "What is 2+2?",
                "model_ids": ["model-a", "model-b"],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["candidate_count"] == 2
        assert data["winner"] is not None
        assert data["winner"]["model_id"] in ("model-a", "model-b")
        assert "aggregate_score" in data["winner"]
        assert len(data["candidates"]) == 2

        for c in data["candidates"]:
            assert "model_id" in c
            assert "critic_scores" in c
            assert "aggregate_score" in c
            assert "critic_verdict" in c

    def test_compare_empty_prompt(self, client):
        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "",
                "model_ids": ["model-a"],
            },
        )
        assert resp.status_code == 400

    def test_compare_too_many_models(self, client, monkeypatch):
        monkeypatch.setattr(settings, "COMPARE_MAX_MODELS", 3)
        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "hello",
                "model_ids": ["m1", "m2", "m3", "m4"],
            },
        )
        assert resp.status_code == 400
        assert "exceeds maximum" in resp.json()["detail"]

    @patch("app.core.llm.provider.generate")
    def test_compare_harden_empty_blocks(self, mock_gen, client):
        from app.core.immune.scanner import ScanResult, Verdict

        flagged = ScanResult(verdict=Verdict.FLAG, score=0.3, triggers=["injection:test"])
        with patch("app.core.immune.scanner.scan_input", return_value=flagged):
            with patch("app.core.immune.scanner.harden_prompt", return_value=("", ["everything"])):
                resp = client.post("/api/agent/compare", json={"prompt": "bad stuff"})
        data = resp.json()
        assert data["status"] == "blocked"
        assert "entirely composed" in data["error"]
        mock_gen.assert_not_called()

    @patch("app.core.llm.provider.generate")
    def test_compare_scores_include_weight(self, mock_gen, client):
        mock_gen.return_value = _mock_response("mock", "mock", "detailed response text here")
        resp = client.post("/api/agent/compare", json={"prompt": "hello world"})
        data = resp.json()
        assert data["status"] == "completed"
        for c in data["candidates"]:
            for node_scores in c["critic_scores"].values():
                assert "weight" in node_scores

    def test_compare_prompt_too_long(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_PROMPT_LENGTH", 10)
        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "a" * 20,
                "model_ids": ["model-a"],
            },
        )
        assert resp.status_code == 413

    @patch("app.core.llm.provider.generate")
    def test_compare_injection_blocked(self, mock_gen, client):
        from app.core.immune.scanner import ScanResult, Verdict

        blocked = ScanResult(verdict=Verdict.BLOCK, score=1.0, triggers=["injection:test"])
        with patch("app.core.immune.scanner.scan_input", return_value=blocked):
            resp = client.post(
                "/api/agent/compare",
                json={
                    "prompt": "anything",
                },
            )
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["candidates"] == []
        assert data["winner"] is None
        mock_gen.assert_not_called()

    @patch("app.core.llm.provider.generate")
    def test_compare_no_responses(self, mock_gen, client):
        mock_gen.side_effect = RuntimeError("all providers down")
        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "hello",
                "model_ids": ["model-a"],
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert data["candidates"] == []
        assert data["winner"] is None

    @patch("app.core.llm.provider.generate")
    def test_compare_picks_higher_scoring(self, mock_gen, client):
        mock_gen.side_effect = [
            _mock_response("model-a", "gemini", "A short bad response"),
            _mock_response(
                "model-b",
                "openai",
                "A much longer and more detailed response that covers multiple aspects of the "
                "question with careful analysis and thorough reasoning throughout the answer",
            ),
        ]
        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "Explain quantum computing in detail",
                "model_ids": ["model-a", "model-b"],
            },
        )
        data = resp.json()
        assert data["status"] == "completed"
        assert data["candidate_count"] == 2
        scores = {c["model_id"]: c["aggregate_score"] for c in data["candidates"]}
        assert data["winner"]["model_id"] == max(scores, key=scores.get)

    @patch("app.core.llm.provider.generate")
    def test_compare_with_default_providers(self, mock_gen, client):
        mock_gen.return_value = _mock_response("mock", "mock", "mock fallback response here")
        resp = client.post(
            "/api/agent/compare",
            json={
                "prompt": "hello world",
            },
        )
        data = resp.json()
        assert data["status"] == "completed"
        assert data["candidate_count"] >= 1

    @patch("app.core.llm.provider.generate")
    def test_compare_hardens_flagged_prompts(self, mock_gen, client):
        from app.core.immune.scanner import ScanResult, Verdict

        flagged = ScanResult(verdict=Verdict.FLAG, score=0.3, triggers=["injection:mild"])
        mock_gen.return_value = _mock_response("mock", "mock", "response from hardened prompt")

        with patch("app.core.immune.scanner.scan_input", return_value=flagged):
            with patch(
                "app.core.immune.scanner.harden_prompt",
                return_value=("safe prompt", ["bad fragment"]),
            ) as mock_harden:
                resp = client.post("/api/agent/compare", json={"prompt": "original prompt"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        mock_harden.assert_called_once()

    def test_compare_critic_exception_handled(self, client):
        with patch("app.core.llm.provider.generate") as mock_gen:
            mock_gen.return_value = _mock_response("mock", "mock", "response text here")
            with patch("app.core.critic.arbiter.Arbiter.evaluate", side_effect=RuntimeError("boom")):
                resp = client.post(
                    "/api/agent/compare",
                    json={"prompt": "test prompt", "model_ids": ["mock"]},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["critic_verdict"] == "error"
        assert data["candidates"][0]["halted"] is True
        assert data["candidates"][0]["aggregate_score"] == 0.0


class TestCompareGovernance:
    def test_compare_governance_deny_blocks(self, client):
        from app.core.covernor.policy_engine import PolicyDecision

        deny = PolicyDecision(
            action="respond",
            decision="deny",
            policy_id="p1",
            policy_name="block-all",
            risk_level="critical",
            required_approvals=0,
            reason="Denied by policy",
        )

        with (
            patch("app.core.llm.provider.generate") as mock_gen,
            patch("app.core.covernor.policy_engine.evaluate_action", return_value=deny),
        ):
            mock_gen.return_value = _mock_response(text="ok response text here")
            resp = client.post("/api/agent/compare", json={"prompt": "hello"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["winner"] is None
        assert "governance" in data
        assert data["governance"]["decision"] == "deny"

    def test_compare_governance_require_approval(self, client):
        from app.core.covernor.policy_engine import PolicyDecision

        approval = PolicyDecision(
            action="respond",
            decision="require_approval",
            policy_id="p2",
            policy_name="needs-review",
            risk_level="high",
            required_approvals=2,
            reason="Requires approval",
        )

        with (
            patch("app.core.llm.provider.generate") as mock_gen,
            patch("app.core.covernor.policy_engine.evaluate_action", return_value=approval),
        ):
            mock_gen.return_value = _mock_response(text="ok response text here")
            resp = client.post("/api/agent/compare", json={"prompt": "hello"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending_approval"
        assert data["winner"] is not None
        assert data["governance"]["decision"] == "require_approval"


class TestCompareRateLimit:
    def test_compare_is_rate_limited(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 2)

        from app.middleware import _rate_limiter_instance

        if _rate_limiter_instance:
            _rate_limiter_instance.reset()

        with patch("app.core.llm.provider.generate") as mock_gen:
            mock_gen.return_value = _mock_response("mock", "mock", "ok response text here")
            for _ in range(2):
                resp = client.post("/api/agent/compare", json={"prompt": "hello"})
                assert resp.status_code == 200

            resp = client.post("/api/agent/compare", json={"prompt": "hello"})
            assert resp.status_code == 429
