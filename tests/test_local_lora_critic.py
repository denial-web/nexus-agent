"""Tests for local LoRA adapter routing and critic registry wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.llm.local_lora import normalize_adapter_model_id


class TestNormalizeAdapterModelId:
    def test_prefers_config_model_id(self):
        assert normalize_adapter_model_id("/tmp/foo", {"model_id": "ollama:injecagent-safety-3b"}) == (
            "ollama:injecagent-safety-3b"
        )

    def test_passes_through_local_lora_id(self):
        assert normalize_adapter_model_id("local-lora:injecagent-safety-3b", None) == (
            "local-lora:injecagent-safety-3b"
        )

    def test_filesystem_path_to_local_lora_suffix(self):
        assert normalize_adapter_model_id("/data/models/injecagent-safety-3b", None) == (
            "local-lora:injecagent-safety-3b"
        )

    def test_empty_returns_none(self):
        assert normalize_adapter_model_id(None, None) is None
        assert normalize_adapter_model_id("", {}) is None


class TestLLMCriticUsesRegistryModelId:
    @patch("app.core.critic.nodes.generate")
    def test_injection_critic_passes_adapter_model_id(self, mock_gen):
        from app.core.critic.nodes import LLMInjectionCritic

        mock_gen.return_value = MagicMock(
            text='{"score": 0.85, "reasoning": "clean"}',
            provider="local_lora",
        )
        c = LLMInjectionCritic(
            name="injection",
            prompt_template="{prompt}\n{response}",
            threshold_pass=0.7,
            threshold_halt=0.3,
            model_id="local-lora:injecagent-safety-3b",
        )
        r = c.evaluate(
            {
                "prompt": "user task about weather",
                "response": "As an AI language model, I cannot do that specific action.",
            }
        )
        assert r.details.get("source") == "llm"
        mock_gen.assert_called_once()
        assert mock_gen.call_args.kwargs["model_id"] == "local-lora:injecagent-safety-3b"


class TestArbiterRegistryAdapterModel:
    @patch("app.core.critic.nodes.generate")
    def test_load_from_registry_wires_lora_model_id(self, mock_gen, db_session):
        from app.core.critic.arbiter import Arbiter
        from app.models.critic_registry import CriticNode

        mock_gen.return_value = MagicMock(
            text='{"score": 0.9, "reasoning": "ok"}',
            provider="mock",
        )
        node = db_session.query(CriticNode).filter_by(name="injection").first()
        assert node is not None
        node.prompt_template = "Prompt: {prompt}\nResponse: {response}"
        node.lora_adapter_path = "local-lora:injecagent-safety-3b"
        db_session.commit()

        arbiter = Arbiter.load_from_registry(db_session)
        injection = arbiter._nodes.get("injection")
        assert injection is not None
        assert getattr(injection, "model_id", None) == "local-lora:injecagent-safety-3b"

        injection.evaluate(
            {
                "prompt": "summarize this email",
                "response": "As an AI language model, I cannot comply with that override.",
            }
        )
        assert mock_gen.call_args.kwargs["model_id"] == "local-lora:injecagent-safety-3b"

        node.lora_adapter_path = None
        db_session.commit()


class TestProviderLocalLoraRoute:
    def test_resolve_route_local_lora(self):
        from app.core.llm.provider import _resolve_route

        provider, resolved, _key = _resolve_route("local-lora:injecagent-safety-3b")
        assert provider == "local_lora"
        assert resolved == "injecagent-safety-3b"
