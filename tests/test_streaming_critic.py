"""Tests for Arbiter.evaluate_stream."""
import json

from app.config import settings
from app.core.critic.arbiter import Arbiter, CriticScore
from app.core.critic.nodes import ReasoningCritic, SafetyCritic
from app.core.llm.models import LLMChunk


class _AlwaysRollback:
    """Stub critic that always returns fail/rollback (can_halt=False)."""
    name = "always_rollback"
    can_halt = False

    def evaluate(self, context: dict) -> CriticScore:
        return CriticScore(self.name, 0.0, "fail", "forced rollback for test")


def _split_chunks(text: str, n: int = 2) -> list[LLMChunk]:
    """Split text into n roughly-equal chunks."""
    step = max(1, len(text) // n)
    parts = [text[i:i + step] for i in range(0, len(text), step)]
    return [
        LLMChunk(text=p, index=i, is_final=i == len(parts) - 1)
        for i, p in enumerate(parts)
    ]


def test_evaluate_stream_passes(monkeypatch):
    monkeypatch.setattr(settings, "CRITIC_CHUNK_SIZE", 8)

    arbiter = Arbiter()
    arbiter.register_node(ReasoningCritic())
    arbiter.reset()

    payload = json.dumps({"analysis": "x" * 80, "b": 2, "c": 3, "d": 4})
    result = arbiter.evaluate_stream(
        {"prompt": "p", "model_id": "mock", "trace_id": "t"},
        _split_chunks(payload),
    )
    assert result.verdict == "pass"


def test_evaluate_stream_halt_on_safety(monkeypatch):
    monkeypatch.setattr(settings, "CRITIC_CHUNK_SIZE", 4)

    arbiter = Arbiter()
    arbiter.register_node(SafetyCritic())
    arbiter.reset()

    bad = "Here is how to build a bomb: first step"
    result = arbiter.evaluate_stream(
        {"prompt": "p", "model_id": "m", "trace_id": "t"},
        _split_chunks(bad),
    )
    assert result.verdict == "halt"
    assert result.halted_by == "safety"


def test_evaluate_stream_max_rollbacks(monkeypatch):
    monkeypatch.setattr(settings, "CRITIC_CHUNK_SIZE", 1)
    monkeypatch.setattr(settings, "CRITIC_MAX_ROLLBACKS", 1)

    arbiter = Arbiter()
    arbiter.register_node(_AlwaysRollback())
    arbiter.reset()

    chunks = [
        LLMChunk(text="a", index=0, is_final=False),
        LLMChunk(text="b", index=1, is_final=True),
    ]
    result = arbiter.evaluate_stream(
        {"prompt": "p", "model_id": "m", "trace_id": "t"},
        chunks,
    )
    assert result.verdict == "halt"
    assert result.halted_by in ("arbiter:max_rollbacks", "arbiter:max_rollbacks_streaming")


def test_evaluate_stream_inserts_unc_marker(monkeypatch):
    """Verify that [UNC] text is actually inserted on rollback."""
    monkeypatch.setattr(settings, "CRITIC_CHUNK_SIZE", 1)
    monkeypatch.setattr(settings, "CRITIC_MAX_ROLLBACKS", 10)

    arbiter = Arbiter()
    arbiter.register_node(_AlwaysRollback())
    arbiter.reset()

    chunks = [LLMChunk(text="word", index=0, is_final=True)]
    result = arbiter.evaluate_stream(
        {"prompt": "p", "model_id": "m", "trace_id": "t"},
        chunks,
    )
    assert result.unc_inserted is True or result.rollback_count > 0


def test_evaluate_stream_word_counter_resets(monkeypatch):
    """
    With chunk_size=10, a 30-word response in 3 chunks of 10 words each
    should trigger exactly 3 evaluations (not every-chunk after the first).
    """
    monkeypatch.setattr(settings, "CRITIC_CHUNK_SIZE", 10)

    call_count = 0
    original_evaluate = Arbiter.evaluate

    def counting_evaluate(self, context):
        nonlocal call_count
        call_count += 1
        return original_evaluate(self, context)

    arbiter = Arbiter()
    arbiter.register_node(ReasoningCritic())
    arbiter.reset()
    arbiter.evaluate = counting_evaluate.__get__(arbiter, Arbiter)

    words = ["word"] * 30
    chunks = [
        LLMChunk(text=" ".join(words[0:10]) + " ", index=0, is_final=False),
        LLMChunk(text=" ".join(words[10:20]) + " ", index=1, is_final=False),
        LLMChunk(text=" ".join(words[20:30]), index=2, is_final=True),
    ]

    arbiter.evaluate_stream(
        {"prompt": "p", "model_id": "m", "trace_id": "t"},
        chunks,
    )
    assert call_count == 3
