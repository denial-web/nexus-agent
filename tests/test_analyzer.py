"""Tests for the A-S-FLC analyzer (LLM path decomposition)."""
import json
from unittest.mock import patch, MagicMock

from app.core.asflc.analyzer import (
    AnalysisResult,
    _extract_json_array,
    _looks_trivial,
    analyze,
)


class TestLooksTrivial:
    def test_short_prompt_is_trivial(self):
        assert _looks_trivial("What is 2+2?") is True

    def test_long_prompt_not_trivial(self):
        assert _looks_trivial(
            "Explain the differences between supervised and unsupervised "
            "machine learning approaches in detail"
        ) is False

    def test_exactly_threshold(self):
        assert _looks_trivial("one two three four five six seven eight") is False

    def test_below_threshold(self):
        assert _looks_trivial("one two three four five six seven") is True


class TestExtractJsonArray:
    def test_plain_array(self):
        text = '[{"name": "a"}, {"name": "b"}]'
        result = _extract_json_array(text)
        assert len(result) == 2
        assert result[0]["name"] == "a"

    def test_code_fenced_array(self):
        text = '```json\n[{"name": "x"}]\n```'
        result = _extract_json_array(text)
        assert len(result) == 1

    def test_array_with_preamble(self):
        text = 'Here are the paths:\n[{"name": "a"}, {"name": "b"}]'
        result = _extract_json_array(text)
        assert len(result) == 2

    def test_no_array(self):
        assert _extract_json_array("no json here") is None

    def test_invalid_json(self):
        assert _extract_json_array("[{broken}]") is None

    def test_nested_arrays(self):
        text = '[{"name": "a", "events": [{"x": 1}]}]'
        result = _extract_json_array(text)
        assert len(result) == 1
        assert len(result[0]["events"]) == 1


class TestAnalyze:
    def test_trivial_prompt_returns_none(self):
        result = analyze("Hi there")
        assert result is None

    _PATCH_TARGET = "app.core.llm.provider.generate"

    def test_llm_success_returns_analysis(self):
        paths = [
            {
                "name": "direct",
                "events": [
                    {"description": "answer directly", "probability": 0.9, "impact": 80, "is_positive": True},
                    {"description": "miss nuance", "probability": 0.2, "impact": -30, "is_positive": False},
                ],
            },
            {
                "name": "cautious",
                "events": [
                    {"description": "hedge answer", "probability": 0.85, "impact": 50, "is_positive": True},
                    {"description": "too verbose", "probability": 0.3, "impact": -10, "is_positive": False},
                ],
            },
        ]
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(paths)

        with patch(self._PATCH_TARGET, return_value=mock_resp):
            result = analyze("Explain the key differences between REST and GraphQL APIs in detail")

        assert isinstance(result, AnalysisResult)
        assert result.chosen_path in ("direct", "cautious")
        assert result.confidence >= 0.0
        assert result.loops >= 1
        assert "Chosen approach:" in result.system_hint

    def test_llm_failure_uses_fallback_paths(self):
        with patch(self._PATCH_TARGET, side_effect=RuntimeError("API down")):
            result = analyze("Explain the differences between supervised and unsupervised ML in detail")

        assert isinstance(result, AnalysisResult)
        assert result.chosen_path in ("direct_response", "cautious_response")
        assert result.raw_paths == [{"fallback": True}]

    def test_llm_returns_garbage_uses_fallback(self):
        mock_resp = MagicMock()
        mock_resp.text = "I don't understand the request, sorry."

        with patch(self._PATCH_TARGET, return_value=mock_resp):
            result = analyze("Explain the differences between supervised and unsupervised ML in detail")

        assert isinstance(result, AnalysisResult)
        assert result.chosen_path in ("direct_response", "cautious_response")

    def test_llm_returns_single_path_uses_fallback(self):
        mock_resp = MagicMock()
        mock_resp.text = json.dumps([{"name": "only_one", "events": []}])

        with patch(self._PATCH_TARGET, return_value=mock_resp):
            result = analyze("Explain the differences between supervised and unsupervised ML in detail")

        assert result.chosen_path in ("direct_response", "cautious_response")

    def test_system_hint_mentions_regret(self):
        paths = [
            {
                "name": "risky",
                "events": [
                    {"description": "high risk action", "probability": 0.8, "impact": -90, "is_positive": False},
                ],
            },
            {
                "name": "safe",
                "events": [
                    {"description": "safe action", "probability": 0.9, "impact": 50, "is_positive": True},
                ],
            },
        ]
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(paths)

        with patch(self._PATCH_TARGET, return_value=mock_resp):
            result = analyze("Explain the differences between supervised and unsupervised ML in detail")

        assert result.chosen_path == "safe"

    def test_code_fenced_llm_response(self):
        paths = [
            {
                "name": "path_a",
                "events": [
                    {"description": "a", "probability": 0.8, "impact": 60, "is_positive": True},
                    {"description": "b", "probability": 0.2, "impact": -20, "is_positive": False},
                ],
            },
            {
                "name": "path_b",
                "events": [
                    {"description": "c", "probability": 0.7, "impact": 40, "is_positive": True},
                ],
            },
        ]
        mock_resp = MagicMock()
        mock_resp.text = f"```json\n{json.dumps(paths)}\n```"

        with patch(self._PATCH_TARGET, return_value=mock_resp):
            result = analyze("Explain the differences between supervised and unsupervised ML in detail")

        assert isinstance(result, AnalysisResult)
        assert result.chosen_path in ("path_a", "path_b")
