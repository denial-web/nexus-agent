"""Unified provider health — merges configuration, circuit breaker state,
and optional live connectivity probes into a single view per provider.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    "gemini": {
        "display_name": "Google Gemini",
        "model_setting": "GEMINI_MODEL",
        "is_configured": lambda: bool(settings.GEMINI_API_KEY.strip()),
    },
    "openai": {
        "display_name": "OpenAI",
        "model_setting": "OPENAI_MODEL",
        "is_configured": lambda: bool(settings.OPENAI_API_KEY.strip()),
    },
    "deepseek": {
        "display_name": "DeepSeek",
        "model_setting": "DEEPSEEK_MODEL",
        "is_configured": lambda: bool(settings.DEEPSEEK_API_KEY.strip()),
    },
    "ollama": {
        "display_name": "Ollama (Local)",
        "model_setting": "OLLAMA_DEFAULT_MODEL",
        "is_configured": lambda: bool(settings.OLLAMA_BASE_URL.strip()),
    },
}


def get_provider_health(
    *, run_probes: bool = False, probe_timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Assemble unified health info for all known providers.

    Each entry contains:
        name, display_name, configured, default_model,
        circuit_breaker (dict|None), probe (dict|None), overall_status
    """
    from app.core.llm.circuit_breaker import get_registry

    registry = get_registry()
    cb_statuses = {s["name"]: s for s in registry.get_all_status()}

    probe_results: dict[str, dict[str, Any]] = {}
    if run_probes:
        try:
            from app.services.health_probe import probe_providers

            probe_results = probe_providers(timeout=probe_timeout)
        except Exception:
            logger.warning("Provider probe failed", exc_info=True)

    providers: list[dict[str, Any]] = []
    for name, cfg in _PROVIDER_CONFIG.items():
        configured = cfg["is_configured"]()
        model_attr = cfg["model_setting"]
        default_model = getattr(settings, model_attr, "")

        cb = cb_statuses.get(name)
        probe = probe_results.get(name)

        overall = _compute_overall(configured, cb, probe)

        providers.append({
            "name": name,
            "display_name": cfg["display_name"],
            "configured": configured,
            "default_model": default_model,
            "circuit_breaker": cb,
            "probe": probe,
            "overall_status": overall,
        })

    return providers


def _compute_overall(
    configured: bool,
    cb: dict[str, Any] | None,
    probe: dict[str, Any] | None,
) -> str:
    """Derive a single status label from the available signals.

    Returns: "healthy", "degraded", "down", or "unconfigured".
    """
    if not configured:
        return "unconfigured"

    if cb and cb.get("state") == "open":
        return "down"

    if cb and cb.get("state") == "half_open":
        return "degraded"

    if probe is not None and not probe.get("reachable", False):
        return "degraded"

    return "healthy"
