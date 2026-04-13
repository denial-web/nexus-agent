"""Tests for the A-S-FLC decision engine."""

import pytest
from app.core.asflc.engine import (
    DecisionPath,
    EventNode,
    build_paths_from_llm_output,
    evaluate_paths,
)


class TestEventNode:
    def test_positive_value(self):
        node = EventNode("good outcome", probability=0.8, impact=100, is_positive=True)
        assert node.signed_value == 80.0

    def test_negative_value_has_delta(self):
        node = EventNode("bad outcome", probability=0.3, impact=-50, is_positive=False)
        # With delta=0.15: adjusted_prob = 0.45, value = -(0.45 * 50) = -22.5
        assert node.signed_value == pytest.approx(-22.5)

    def test_negative_capped_at_one(self):
        node = EventNode("very likely bad", probability=0.95, impact=-100, is_positive=False)
        # 0.95 + 0.15 = 1.10 -> capped at 1.0
        assert node.signed_value == -100.0


class TestDecisionPath:
    def test_chain_score(self):
        path = DecisionPath(
            name="test",
            events=[
                EventNode("good", 0.8, 100, True),  # +80
                EventNode("bad", 0.2, -50, False),  # -(0.35 * 50) = -17.5
            ],
        )
        assert abs(path.chain_score - 62.5) < 0.01

    def test_empty_path(self):
        path = DecisionPath(name="empty", events=[])
        assert path.chain_score == 0.0

    def test_confidence_needs_history(self):
        path = DecisionPath(name="test", events=[EventNode("x", 0.5, 10, True)])
        assert path.confidence == 0.0
        path.record_score()
        assert path.confidence == 0.0
        path.record_score()
        assert path.confidence > 0.9


class TestEvaluatePaths:
    def test_chooses_highest_score(self):
        paths = [
            DecisionPath("safe", [EventNode("good", 0.9, 100, True)]),
            DecisionPath("risky", [EventNode("bad", 0.8, -200, False)]),
        ]
        result = evaluate_paths(paths)
        assert result.chosen_path == "safe"
        assert result.chosen_score > 0

    def test_converges(self):
        paths = [
            DecisionPath("a", [EventNode("x", 0.5, 50, True)]),
            DecisionPath("b", [EventNode("y", 0.5, 40, True)]),
        ]
        result = evaluate_paths(paths)
        assert result.converged is True
        assert result.loops_taken >= 2

    def test_zero_regret_when_best_is_positive(self):
        paths = [
            DecisionPath("winner", [EventNode("win", 0.9, 100, True)]),
            DecisionPath("loser", [EventNode("lose", 0.1, 10, True)]),
        ]
        result = evaluate_paths(paths)
        assert result.chain_regret == 0.0

    def test_all_paths_in_result(self):
        paths = [
            DecisionPath("a", [EventNode("x", 0.5, 50, True)]),
            DecisionPath("b", [EventNode("y", 0.5, 40, True)]),
            DecisionPath("c", [EventNode("z", 0.5, 30, True)]),
        ]
        result = evaluate_paths(paths)
        assert len(result.all_paths) == 3
        assert "a" in result.all_paths


class TestBuildPaths:
    def test_parses_raw_format(self):
        raw = [
            {
                "name": "Accept",
                "events": [
                    {"description": "profit", "probability": 0.7, "impact": 100, "is_positive": True},
                    {"description": "lawsuit", "probability": 0.1, "impact": -500, "is_positive": False},
                ],
            },
        ]
        paths = build_paths_from_llm_output(raw)
        assert len(paths) == 1
        assert paths[0].name == "Accept"
        assert len(paths[0].events) == 2

    def test_handles_empty(self):
        paths = build_paths_from_llm_output([])
        assert paths == []
