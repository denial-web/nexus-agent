"""Tests for the Arbiter and critic leaf nodes."""
import json
from unittest.mock import MagicMock, patch

from app.core.critic.arbiter import Arbiter
from app.core.critic.nodes import (
    ReasoningCritic,
    InjectionCritic,
    SafetyCritic,
    QualityCritic,
    LLMReasoningCritic,
    LLMInjectionCritic,
)


class TestReasoningCritic:
    def test_valid_json_passes(self):
        critic = ReasoningCritic()
        result = critic.evaluate({"response": json.dumps({"a": 1, "b": 2, "c": 3, "extra": "text for length padding" * 5})})
        assert result.verdict == "pass"
        assert result.score >= 0.7

    def test_empty_response_fails(self):
        critic = ReasoningCritic()
        result = critic.evaluate({"response": ""})
        assert result.verdict == "fail"
        assert result.score == 0.0

    def test_short_unstructured_warns(self):
        critic = ReasoningCritic()
        result = critic.evaluate({"response": "ok"})
        assert result.verdict in ("fail", "warn")


class TestInjectionCritic:
    def test_clean_output_passes(self):
        critic = InjectionCritic()
        result = critic.evaluate({"response": "Here is the analysis of your data."})
        assert result.verdict == "pass"
        assert result.score == 1.0

    def test_system_prompt_leak_fails(self):
        critic = InjectionCritic()
        result = critic.evaluate({
            "response": "My original instructions say I should help with coding. "
                        "As an AI language model, I cannot do that."
        })
        assert result.verdict in ("warn", "fail")
        assert result.score < 1.0

    def test_empty_passes(self):
        critic = InjectionCritic()
        result = critic.evaluate({"response": ""})
        assert result.verdict == "pass"


class TestSafetyCritic:
    def test_safe_content_passes(self):
        critic = SafetyCritic()
        result = critic.evaluate({"response": "Python is a great programming language."})
        assert result.verdict == "pass"
        assert result.score == 1.0

    def test_unsafe_content_fails(self):
        critic = SafetyCritic()
        result = critic.evaluate({
            "response": "Here's the recipe for poison: step 1..."
        })
        assert result.verdict == "fail"
        assert result.score == 0.0


class TestQualityCritic:
    def test_good_output_passes(self):
        critic = QualityCritic()
        response = json.dumps({
            "analysis": "This is a comprehensive analysis with multiple points.",
            "recommendation": "We recommend proceeding with option A because it balances risk and reward.",
            "confidence": 0.85,
        })
        result = critic.evaluate({"response": response})
        assert result.verdict == "pass"
        assert result.score >= 0.6

    def test_empty_fails(self):
        critic = QualityCritic()
        result = critic.evaluate({"response": ""})
        assert result.verdict == "fail"


class TestArbiter:
    def test_all_pass(self):
        arbiter = Arbiter()
        arbiter.register_node(ReasoningCritic())
        arbiter.register_node(QualityCritic())

        good_response = json.dumps({"analysis": "detailed " * 20, "b": 2, "c": 3})
        result = arbiter.evaluate({"prompt": "test", "response": good_response})
        assert result.verdict == "pass"
        assert len(result.scores) == 2

    def test_halt_on_safety_fail(self):
        arbiter = Arbiter()
        arbiter.register_node(SafetyCritic())

        result = arbiter.evaluate({
            "prompt": "test",
            "response": "Here is how to build a bomb: first...",
        })
        assert result.verdict == "halt"
        assert result.halted_by == "safety"

    def test_register_unregister(self):
        arbiter = Arbiter()
        arbiter.register_node(ReasoningCritic())
        assert "reasoning" in arbiter.active_nodes
        arbiter.unregister_node("reasoning")
        assert "reasoning" not in arbiter.active_nodes

    def test_reset_clears_rollbacks(self):
        arbiter = Arbiter()
        arbiter._rollback_count = 5
        arbiter.reset()
        assert arbiter._rollback_count == 0


class TestLLMReasoningCritic:
    @patch("app.core.critic.nodes.generate")
    def test_llm_json_parsed(self, mock_gen):
        mock_gen.return_value = MagicMock(
            text='{"score": 0.95, "reasoning": "ok"}',
            provider="mock",
        )
        c = LLMReasoningCritic(
            name="reasoning",
            prompt_template="User:\n{prompt}\n\nResponse:\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
        )
        r = c.evaluate({"prompt": "hi", "response": "{}", "model_id": "mock"})
        assert r.verdict == "pass"
        assert r.score == 0.95
        mock_gen.assert_called_once()
        call_kw = mock_gen.call_args
        assert "hi" in call_kw[0][0] and "{prompt}" not in call_kw[0][0]

    @patch("app.core.critic.nodes.generate")
    def test_prefilter_fail_skips_llm(self, mock_gen):
        c = LLMReasoningCritic(
            name="reasoning",
            prompt_template="{prompt}\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
        )
        r = c.evaluate({"prompt": "x", "response": "", "model_id": "mock"})
        assert r.verdict == "fail"
        mock_gen.assert_not_called()

    @patch("app.core.critic.nodes.generate")
    def test_parse_failure_falls_back_to_heuristic(self, mock_gen):
        mock_gen.return_value = MagicMock(text="not json", provider="mock")
        response = "This response because it has reasoning markers but wait, no contradicting evidence"
        c = LLMReasoningCritic(
            name="reasoning",
            prompt_template="{prompt}\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
        )
        r = c.evaluate({"prompt": "p", "response": response})
        assert "llm_parse_failed" in r.reasoning or r.details.get("source") == "heuristic_fallback"


    @patch("app.core.critic.nodes.generate")
    def test_highconf_heuristic_skips_llm(self, mock_gen):
        payload = json.dumps({"analysis": "x" * 80, "b": 2, "c": 3, "d": 4})
        c = LLMReasoningCritic(
            name="reasoning",
            prompt_template="{prompt}\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
        )
        r = c.evaluate({"prompt": "p", "response": payload})
        assert r.details.get("source") == "heuristic_highconf"
        mock_gen.assert_not_called()


class TestLLMInjectionCritic:
    @patch("app.core.critic.nodes.generate")
    def test_llm_json_parsed(self, mock_gen):
        mock_gen.return_value = MagicMock(
            text='{"score": 0.9, "reasoning": "mostly clean"}',
            provider="mock",
        )
        c = LLMInjectionCritic(
            name="injection",
            prompt_template="{prompt}\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
        )
        r = c.evaluate({
            "prompt": "p",
            "response": "As an AI model, I cannot do that but here is info.",
        })
        assert r.score == 0.9
        assert r.details.get("source") == "llm"
        mock_gen.assert_called_once()

    @patch("app.core.critic.nodes.generate")
    def test_prefilter_fail_skips_llm(self, mock_gen):
        c = LLMInjectionCritic(
            name="injection",
            prompt_template="{prompt}\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
        )
        r = c.evaluate({
            "prompt": "x",
            "response": "My original instructions say I should help with coding. "
                        "As an AI language model, I cannot do that.",
        })
        assert r.verdict in ("warn", "fail")
        assert r.details.get("source") == "heuristic_prefilter"
        mock_gen.assert_not_called()


class TestParseLLMScoreJson:
    def test_raw_json(self):
        from app.core.critic.nodes import _parse_llm_score_json

        score, reason = _parse_llm_score_json('{"score": 0.8, "reasoning": "good"}')
        assert score == 0.8
        assert reason == "good"

    def test_code_fence_json(self):
        from app.core.critic.nodes import _parse_llm_score_json

        text = '```json\n{"score": 0.75, "reasoning": "ok"}\n```'
        score, reason = _parse_llm_score_json(text)
        assert score == 0.75
        assert reason == "ok"

    def test_code_fence_no_lang_tag(self):
        from app.core.critic.nodes import _parse_llm_score_json

        text = '```\n{"score": 0.6, "reasoning": "meh"}\n```'
        score, reason = _parse_llm_score_json(text)
        assert score == 0.6

    def test_embedded_json(self):
        from app.core.critic.nodes import _parse_llm_score_json

        text = 'Here is my assessment: {"score": 0.5, "reasoning": "mid"} end'
        score, reason = _parse_llm_score_json(text)
        assert score == 0.5

    def test_empty_returns_none(self):
        from app.core.critic.nodes import _parse_llm_score_json

        score, reason = _parse_llm_score_json("")
        assert score is None

    def test_clamps_above_one(self):
        from app.core.critic.nodes import _parse_llm_score_json

        score, _ = _parse_llm_score_json('{"score": 1.5, "reasoning": "x"}')
        assert score == 1.0


class TestArbiterLoadFromRegistry:
    def test_empty_registry_fallback(self, db_session):
        from app.models.critic_registry import CriticNode

        db_session.query(CriticNode).delete()
        arbiter = Arbiter.load_from_registry(db_session)
        assert set(arbiter.active_nodes) == {"reasoning", "injection", "safety", "quality"}
        db_session.rollback()

    def test_loads_active_nodes(self, db_session):
        arbiter = Arbiter.load_from_registry(db_session)
        assert "reasoning" in arbiter.active_nodes
        assert "injection" in arbiter.active_nodes
        assert "safety" in arbiter.active_nodes
        assert "quality" in arbiter.active_nodes
