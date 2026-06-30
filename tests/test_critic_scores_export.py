"""Tests for critic score serialization on the Doctrine export path."""

from __future__ import annotations

from app.core.critic.arbiter import CriticScore
from app.core.critic.scores import flatten_critic_output, serialize_critic_scores
from app.services.doctrine_bridge import _training_item_to_entry


def test_serialize_critic_scores_handles_dataclass():
    scores = {
        "injection": CriticScore("injection", 0.2, "fail", "leak"),
    }
    out = serialize_critic_scores(scores)
    assert out["injection"]["score"] == 0.2
    assert out["injection"]["verdict"] == "fail"


def test_flatten_critic_output_unwraps_scores_key():
    nested = {
        "scores": {
            "injection": CriticScore("injection", 0.1, "fail", "x"),
        }
    }
    flat = flatten_critic_output(nested)
    assert flat["injection"]["score"] == 0.1


def test_training_item_to_entry_exports_flat_critic_scores():
    entry = _training_item_to_entry(
        {
            "messages": [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "bad"},
            ],
            "metadata": {
                "trace_id": "t-1",
                "failure_type": "injection",
                "critic_scores": {
                    "scores": {
                        "injection": CriticScore("injection", 0.15, "fail", "leak"),
                    }
                },
            },
        }
    )
    assert entry["critic_scores"]["injection"]["score"] == 0.15
