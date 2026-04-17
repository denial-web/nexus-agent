"""Deep health check — probe LLM provider reachability.

When `?deep=true` is passed to `/health/ready`, each configured LLM provider
is probed with a lightweight connectivity test (list-models or tiny completion).
Probes run concurrently with a per-provider timeout.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


def _probe_gemini(timeout: float) -> dict[str, Any]:
    import urllib.request

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={settings.GEMINI_API_KEY}&pageSize=1"
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = round(time.monotonic() - start, 3)
            return {"reachable": True, "latency_s": latency, "status": resp.status}
    except Exception as exc:
        latency = round(time.monotonic() - start, 3)
        return {"reachable": False, "latency_s": latency, "error": str(exc)}


def _probe_openai(timeout: float) -> dict[str, Any]:
    import urllib.request

    url = "https://api.openai.com/v1/models?limit=1"
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {settings.OPENAI_API_KEY}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = round(time.monotonic() - start, 3)
            return {"reachable": True, "latency_s": latency, "status": resp.status}
    except Exception as exc:
        latency = round(time.monotonic() - start, 3)
        return {"reachable": False, "latency_s": latency, "error": str(exc)}


def _probe_deepseek(timeout: float) -> dict[str, Any]:
    import urllib.request

    base = settings.DEEPSEEK_BASE_URL.rstrip("/")
    url = f"{base}/models"
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {settings.DEEPSEEK_API_KEY}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = round(time.monotonic() - start, 3)
            return {"reachable": True, "latency_s": latency, "status": resp.status}
    except Exception as exc:
        latency = round(time.monotonic() - start, 3)
        return {"reachable": False, "latency_s": latency, "error": str(exc)}


def _probe_ollama(timeout: float) -> dict[str, Any]:
    import urllib.request

    base = settings.OLLAMA_BASE_URL.rstrip("/")
    url = f"{base}/api/tags"
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = round(time.monotonic() - start, 3)
            return {"reachable": True, "latency_s": latency, "status": resp.status}
    except Exception as exc:
        latency = round(time.monotonic() - start, 3)
        return {"reachable": False, "latency_s": latency, "error": str(exc)}


_PROVIDER_PROBES: dict[str, Any] = {
    "gemini": (_probe_gemini, lambda: bool(settings.GEMINI_API_KEY.strip())),
    "openai": (_probe_openai, lambda: bool(settings.OPENAI_API_KEY.strip())),
    "deepseek": (_probe_deepseek, lambda: bool(settings.DEEPSEEK_API_KEY.strip())),
    "ollama": (_probe_ollama, lambda: bool(settings.OLLAMA_BASE_URL.strip())),
}


def probe_providers(
    timeout: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Probe all configured LLM providers concurrently.

    Returns a dict keyed by provider name with reachability info.
    Only probes providers that have credentials/URLs configured.
    """
    if timeout is None:
        timeout = settings.HEALTH_PROBE_TIMEOUT

    targets: dict[str, Any] = {}
    for name, (probe_fn, is_configured) in _PROVIDER_PROBES.items():
        if is_configured():
            targets[name] = probe_fn

    if not targets:
        return {}

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {pool.submit(probe_fn, timeout): name for name, probe_fn in targets.items()}
        for future in as_completed(futures, timeout=timeout + 5):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = {"reachable": False, "error": str(exc)}

    return results
