"""Unit tests for the belief extractor.

The extractor is LLM-backed, so every test here monkeypatches
`app.core.memory.extractor.generate` to return a canned LLMResponse.
We never hit a real provider.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.core.llm.models import LLMResponse
from app.core.memory import extractor as ext
from app.core.memory.extractor import EXTRACTOR_VERSION, extract_beliefs


@pytest.fixture
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)


def _fake_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        model_id="mock",
        token_count=0,
        latency_ms=0.0,
        provider="mock",
    )


def _stub_llm(monkeypatch, text: str) -> list[dict]:
    calls: list[dict] = []

    def fake_generate(**kwargs):
        calls.append(kwargs)
        return _fake_response(text)

    monkeypatch.setattr(ext, "generate", fake_generate)
    return calls


class TestFeatureFlag:
    def test_returns_empty_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
        called = _stub_llm(monkeypatch, "[]")
        out = extract_beliefs(
            user_message="I prefer short answers.",
            assistant_response="Got it.",
        )
        assert out == []
        assert called == []  # LLM never called

    def test_returns_empty_for_empty_input(self, memory_on, monkeypatch):
        called = _stub_llm(monkeypatch, "[]")
        assert extract_beliefs(user_message="", assistant_response="") == []
        assert called == []


class TestJsonParsing:
    def test_parses_plain_json_array(self, memory_on, monkeypatch):
        _stub_llm(
            monkeypatch,
            '[{"entity":"user:alice","predicate":"answer_length",'
            '"value":"short","entity_type":"preference","confidence":0.9,'
            '"rationale":"user said so"}]',
        )
        drafts = extract_beliefs(
            user_message="I prefer short answers.",
            assistant_response="ok",
            user_id="alice",
        )
        assert len(drafts) == 1
        d = drafts[0]
        assert d.entity == "user:alice"
        assert d.predicate == "answer_length"
        assert d.value == "short"
        assert d.entity_type == "preference"
        assert d.user_id == "alice"
        assert d.rationale == "user said so"
        # confidence 0.9 + strength 8 → alpha/beta close to 7.2/0.8
        assert 0.85 <= d.confidence.mean <= 0.95

    def test_parses_code_fenced_json(self, memory_on, monkeypatch):
        _stub_llm(
            monkeypatch,
            '```json\n[{"entity":"user:a","predicate":"p","value":"v",'
            '"entity_type":"preference","confidence":0.7}]\n```',
        )
        drafts = extract_beliefs(
            user_message="something", assistant_response="something"
        )
        assert len(drafts) == 1

    def test_parses_json_with_surrounding_text(self, memory_on, monkeypatch):
        _stub_llm(
            monkeypatch,
            'Here is the JSON: [{"entity":"u","predicate":"p","value":"v",'
            '"entity_type":"preference","confidence":0.6}] (done)',
        )
        drafts = extract_beliefs(
            user_message="q", assistant_response="a"
        )
        assert len(drafts) == 1

    def test_empty_array_returns_empty(self, memory_on, monkeypatch):
        _stub_llm(monkeypatch, "[]")
        assert extract_beliefs(user_message="hi", assistant_response="hello") == []

    def test_garbage_returns_empty(self, memory_on, monkeypatch):
        _stub_llm(monkeypatch, "I don't know what you want")
        assert extract_beliefs(user_message="x", assistant_response="y") == []

    def test_non_list_returns_empty(self, memory_on, monkeypatch):
        _stub_llm(monkeypatch, '{"entity":"u","predicate":"p"}')
        assert extract_beliefs(user_message="x", assistant_response="y") == []


class TestValidation:
    def test_drops_invalid_entity_type(self, memory_on, monkeypatch):
        _stub_llm(
            monkeypatch,
            '[{"entity":"u","predicate":"p","value":"v",'
            '"entity_type":"not_real","confidence":0.7}]',
        )
        assert extract_beliefs(user_message="x", assistant_response="y") == []

    def test_drops_missing_predicate(self, memory_on, monkeypatch):
        _stub_llm(
            monkeypatch,
            '[{"entity":"u","value":"v","entity_type":"preference"}]',
        )
        assert extract_beliefs(user_message="x", assistant_response="y") == []

    def test_clamps_confidence_out_of_range(self, memory_on, monkeypatch):
        _stub_llm(
            monkeypatch,
            '[{"entity":"u","predicate":"p","value":"v",'
            '"entity_type":"preference","confidence":5.0}]',
        )
        drafts = extract_beliefs(user_message="x", assistant_response="y")
        assert len(drafts) == 1
        assert drafts[0].confidence.mean <= 0.99

    def test_cap_on_number_of_drafts(self, memory_on, monkeypatch):
        items = ",".join(
            f'{{"entity":"u{i}","predicate":"p","value":"v",'
            f'"entity_type":"preference","confidence":0.7}}'
            for i in range(20)
        )
        _stub_llm(monkeypatch, f"[{items}]")
        drafts = extract_beliefs(user_message="x", assistant_response="y")
        assert len(drafts) <= 8  # _MAX_DRAFTS


class TestLLMInvocation:
    def test_uses_extraction_model_when_set(self, memory_on, monkeypatch):
        monkeypatch.setattr(settings, "EXTRACTION_MODEL", "gemini-1.5-flash")
        calls = _stub_llm(monkeypatch, "[]")
        extract_beliefs(user_message="x", assistant_response="y")
        assert calls[0]["model_id"] == "gemini-1.5-flash"

    def test_respects_model_override(self, memory_on, monkeypatch):
        calls = _stub_llm(monkeypatch, "[]")
        extract_beliefs(user_message="x", assistant_response="y", model_id="mock")
        assert calls[0]["model_id"] == "mock"

    def test_clips_long_input(self, memory_on, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_EXTRACTOR_MAX_CHARS", 100)
        calls = _stub_llm(monkeypatch, "[]")
        extract_beliefs(
            user_message="u" * 1000,
            assistant_response="a" * 1000,
        )
        sent = calls[0]["prompt"]
        assert "truncated" in sent
        assert len(sent) < 500  # half-half split + template overhead

    def test_llm_error_returns_empty(self, memory_on, monkeypatch):
        def boom(**_kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr(ext, "generate", boom)
        assert extract_beliefs(user_message="x", assistant_response="y") == []


class TestKeywords:
    def test_keywords_include_predicate_and_value_tokens(
        self, memory_on, monkeypatch
    ):
        _stub_llm(
            monkeypatch,
            '[{"entity":"user:alice","predicate":"answer_length",'
            '"value":"very short","entity_type":"preference","confidence":0.8}]',
        )
        drafts = extract_beliefs(
            user_message="I like short answers",
            assistant_response="ok",
        )
        assert len(drafts) == 1
        kws = drafts[0].keywords or []
        assert "answer" in kws
        assert "length" in kws
        assert "very" in kws
        assert "short" in kws


class TestVersioning:
    def test_version_constant_is_exported(self):
        assert isinstance(EXTRACTOR_VERSION, str)
        assert EXTRACTOR_VERSION
