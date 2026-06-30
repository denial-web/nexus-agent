"""JSON-safe serialization for critic score payloads on the training export path."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.critic.arbiter import CriticScore


def serialize_critic_scores(scores: dict[str, Any]) -> dict[str, Any]:
    """Convert CriticScore dataclass objects to JSON-safe dicts for labeling export."""
    out: dict[str, Any] = {}
    for key, value in (scores or {}).items():
        if isinstance(value, CriticScore):
            out[key] = asdict(value)
        elif isinstance(value, dict):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = {"score": float(value)}
        else:
            out[key] = str(value)
    return out


def flatten_critic_output(critic_output: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize labeling-queue critic_output for Doctrine Lab import."""
    if not critic_output:
        return {}
    if "scores" in critic_output and isinstance(critic_output["scores"], dict):
        nested = critic_output["scores"]
        if nested and all(isinstance(v, (dict, CriticScore, int, float)) for v in nested.values()):
            return serialize_critic_scores(nested)
    return serialize_critic_scores(critic_output) if any(
        isinstance(v, CriticScore) for v in critic_output.values()
    ) else dict(critic_output)
