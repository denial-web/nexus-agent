"""
Unified LLM provider: Gemini, OpenAI, DeepSeek, local HuggingFace, or mock fallback.

Provider selection:
- Explicit model_id starting with "gpt" or "ft:" → OpenAI
- Explicit model_id starting with "gemini" or "models/" → Gemini
- Explicit model_id starting with "deepseek" → DeepSeek
- Explicit model_id "local", "local:repo/name", or "nexus-spin-..." → local HF (stub if no torch/transformers)
- No model_id: prefer Gemini → OpenAI → DeepSeek → mock

DeepSeek uses the OpenAI SDK with a custom base_url.

Falls back to mock when the required API key is missing (even if model_id
was explicitly requested), logging a warning rather than crashing.
"""

import json
import logging
import time
from collections.abc import Generator
from typing import Any

from app.config import settings
from app.core.llm.models import LLMChunk, LLMResponse
from app.metrics import LLM_CALLS, LLM_ERRORS

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3

_gemini_client: Any = None
_gemini_client_key: str = ""
_openai_client: Any = None
_openai_client_key: str = ""
_deepseek_client: Any = None
_deepseek_client_key: str = ""

_local_models: dict[str, Any] = {}
_local_tokenizers: dict[str, Any] = {}


def _get_local_tokenizer(model: str) -> Any:
    if model not in _local_tokenizers:
        from transformers import AutoTokenizer

        _local_tokenizers[model] = AutoTokenizer.from_pretrained(model, trust_remote_code=True)
    return _local_tokenizers[model]


def _get_local_model(model: str, device: str) -> Any:
    if model not in _local_models:
        from transformers import AutoModelForCausalLM

        mdl = AutoModelForCausalLM.from_pretrained(model, trust_remote_code=True)
        mdl.eval()
        mdl.to(device)
        _local_models[model] = mdl
    return _local_models[model]


def _get_gemini_client(api_key: str) -> Any:
    global _gemini_client, _gemini_client_key
    if _gemini_client is None or _gemini_client_key != api_key:
        from google import genai

        _gemini_client = genai.Client(api_key=api_key)
        _gemini_client_key = api_key
    return _gemini_client


def _get_openai_client(api_key: str) -> Any:
    global _openai_client, _openai_client_key
    if _openai_client is None or _openai_client_key != api_key:
        import openai

        _openai_client = openai.OpenAI(api_key=api_key)
        _openai_client_key = api_key
    return _openai_client


def _get_deepseek_client(api_key: str) -> Any:
    global _deepseek_client, _deepseek_client_key
    if _deepseek_client is None or _deepseek_client_key != api_key:
        import openai

        _deepseek_client = openai.OpenAI(
            api_key=api_key,
            base_url=settings.DEEPSEEK_BASE_URL,
        )
        _deepseek_client_key = api_key
    return _deepseek_client


def reset_clients() -> None:
    """Reset cached clients (useful for tests)."""
    global _gemini_client, _gemini_client_key
    global _openai_client, _openai_client_key
    global _deepseek_client, _deepseek_client_key
    _gemini_client = None
    _gemini_client_key = ""
    _openai_client = None
    _openai_client_key = ""
    _deepseek_client = None
    _deepseek_client_key = ""


def mock_llm_text(prompt: str) -> str:
    return (
        f'{{"analysis": "This is a mock response for testing the pipeline. '
        f'The prompt was {len(prompt)} characters long.", '
        f'"recommendation": "No live LLM provider configured; using mock fallback.", '
        f'"confidence": 0.85}}'
    )


def _estimate_tokens(text: str) -> int:
    if not text.strip():
        return 0
    return len(text.split())


def _resolve_route(
    model_id: str | None,
) -> tuple[str, str, str]:
    """
    Returns (provider, resolved_model_id, api_key).

    Falls back to mock if the needed API key is empty — never routes to a
    real provider with a blank key.
    """
    mid = (model_id or "").strip()
    gemini_key = settings.GEMINI_API_KEY.strip()
    openai_key = settings.OPENAI_API_KEY.strip()
    deepseek_key = settings.DEEPSEEK_API_KEY.strip()

    if mid:
        lower = mid.lower()
        if lower.startswith("gpt") or lower.startswith("ft:"):
            if openai_key:
                return ("openai", mid, openai_key)
            logger.warning("model_id %r requires OpenAI but OPENAI_API_KEY is empty; falling back to mock", mid)
            return ("mock", "mock", "")
        if lower.startswith("gemini") or lower.startswith("models/"):
            if gemini_key:
                return ("gemini", mid, gemini_key)
            logger.warning("model_id %r requires Gemini but GEMINI_API_KEY is empty; falling back to mock", mid)
            return ("mock", "mock", "")
        if lower.startswith("deepseek"):
            if deepseek_key:
                return ("deepseek", mid, deepseek_key)
            logger.warning("model_id %r requires DeepSeek but DEEPSEEK_API_KEY is empty; falling back to mock", mid)
            return ("mock", "mock", "")
        if lower.startswith("local:") or lower == "local" or lower.startswith("nexus-spin"):
            resolved = mid.split(":", 1)[-1].strip() if ":" in mid else mid
            if resolved in ("local", "nexus-spin"):
                resolved = settings.LOCAL_HF_MODEL_ID or "nexus-spin-v5.3"
            return ("local", resolved, "")

    if gemini_key:
        return ("gemini", settings.GEMINI_MODEL, gemini_key)
    if openai_key:
        return ("openai", settings.OPENAI_MODEL, openai_key)
    if deepseek_key:
        return ("deepseek", settings.DEEPSEEK_MODEL, deepseek_key)
    return ("mock", "mock", "")


def _gemini_retryable(exc: BaseException) -> bool:
    try:
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.ServerError):
            return True
        if isinstance(exc, genai_errors.ClientError):
            code = getattr(exc, "code", None)
            return code in (408, 429)
    except ImportError:
        pass
    return False


def _openai_retryable(exc: BaseException) -> bool:
    try:
        import openai

        return isinstance(
            exc,
            (
                openai.RateLimitError,
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.InternalServerError,
            ),
        )
    except ImportError:
        return False


def _should_retry(exc: BaseException, provider: str) -> bool:
    if isinstance(exc, (TimeoutError, OSError)):
        return True
    if provider == "gemini":
        return _gemini_retryable(exc)
    if provider in ("openai", "deepseek"):
        return _openai_retryable(exc)
    return False


def _sleep_backoff(attempt: int) -> None:
    delay = 0.5 * (2**attempt)
    time.sleep(delay)


def _call_gemini(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: str | None,
) -> tuple[str, str, int, dict[str, Any] | None]:
    from google.genai import types

    client = _get_gemini_client(api_key)
    config: types.GenerateContentConfig | None = None
    if system_prompt:
        config = types.GenerateContentConfig(system_instruction=system_prompt)

    kwargs: dict[str, Any] = {"model": model, "contents": prompt}
    if config is not None:
        kwargs["config"] = config

    response = client.models.generate_content(**kwargs)
    text = (response.text or "").strip()
    token_count = 0
    if getattr(response, "usage_metadata", None) is not None:
        um = response.usage_metadata
        token_count = int(getattr(um, "total_token_count", None) or 0)
    if token_count == 0:
        token_count = _estimate_tokens(text)

    raw: dict[str, Any] | None = None
    try:
        raw = response.model_dump() if hasattr(response, "model_dump") else None
    except Exception:
        raw = None

    return text, model, token_count, raw


def _call_openai(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: str | None,
) -> tuple[str, str, int, dict[str, Any] | None]:
    client = _get_openai_client(api_key)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    token_count = 0
    if response.usage is not None:
        token_count = int(response.usage.total_tokens or 0)
    if token_count == 0:
        token_count = _estimate_tokens(text)

    raw: dict[str, Any] | None = None
    try:
        raw = response.model_dump()
    except Exception:
        raw = None

    return text, response.model or model, token_count, raw


def _call_deepseek(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: str | None,
) -> tuple[str, str, int, dict[str, Any] | None]:
    client = _get_deepseek_client(api_key)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    token_count = 0
    if response.usage is not None:
        token_count = int(response.usage.total_tokens or 0)
    if token_count == 0:
        token_count = _estimate_tokens(text)

    raw: dict[str, Any] | None = None
    try:
        raw = response.model_dump()
    except Exception:
        raw = None

    return text, response.model or model, token_count, raw


def _call_local_hf(
    prompt: str,
    model: str,
    system_prompt: str | None,
) -> tuple[str, str, int, dict[str, Any] | None]:
    """
    Local HuggingFace causal LM (e.g. Nexus Spin v5.3).

    Requires `transformers` and `torch`. If unavailable, returns a deterministic stub
    so the pipeline still runs in dev/CI without GPU deps.
    """
    try:
        import torch
        import transformers  # noqa: F401 — availability check

        device = settings.LOCAL_HF_DEVICE or "cpu"
        tok = _get_local_tokenizer(model)
        mdl = _get_local_model(model, device)
        full = (system_prompt + "\n\n" + prompt) if system_prompt else prompt
        inputs = tok(full, return_tensors="pt").to(device)
        input_len = int(inputs["input_ids"].shape[-1])
        with torch.no_grad():
            out = mdl.generate(**inputs, max_new_tokens=256, do_sample=False)
        text = tok.decode(out[0], skip_special_tokens=True)
        if full in text:
            text = text.split(full, 1)[-1].strip()
        tc = input_len + int(out.shape[-1]) - input_len
        return text, model, tc, None
    except ImportError:
        logger.warning(
            "Local HF model %r: transformers/torch not installed; using stub output",
            model,
        )
        stub = (
            f'{{"local_stub": true, "model": "{model}", '
            f'"note": "Install torch+transformers for real local inference.", '
            f'"echo": {json.dumps(prompt[:200])}}}'
        )
        return stub, model, _estimate_tokens(stub), None
    except Exception:
        logger.exception("Local HF generation failed for %r", model)
        raise


def _mock_response(prompt: str, latency_start: float) -> LLMResponse:
    text = mock_llm_text(prompt)
    latency_ms = (time.perf_counter() - latency_start) * 1000
    return LLMResponse(
        text=text,
        model_id="mock",
        token_count=0,
        latency_ms=round(latency_ms, 2),
        provider="mock",
        raw_response=None,
    )


def generate(
    prompt: str,
    model_id: str | None = None,
    system_prompt: str | None = None,
) -> LLMResponse:
    """Generate a full response from the configured provider."""
    route_provider, resolved_model, api_key = _resolve_route(model_id)
    start = time.perf_counter()

    if route_provider == "mock":
        return _mock_response(prompt, start)

    LLM_CALLS.labels(provider=route_provider).inc()

    if route_provider == "local":
        text, used_model, token_count, raw = _call_local_hf(prompt, resolved_model, system_prompt)
        latency_ms = (time.perf_counter() - start) * 1000
        return LLMResponse(
            text=text,
            model_id=used_model,
            token_count=token_count,
            latency_ms=round(latency_ms, 2),
            provider="local",
            raw_response=raw,
        )

    last_error: BaseException | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            if route_provider == "gemini":
                text, used_model, token_count, raw = _call_gemini(prompt, resolved_model, api_key, system_prompt)
            elif route_provider == "deepseek":
                text, used_model, token_count, raw = _call_deepseek(prompt, resolved_model, api_key, system_prompt)
            else:
                text, used_model, token_count, raw = _call_openai(prompt, resolved_model, api_key, system_prompt)
            latency_ms = (time.perf_counter() - start) * 1000
            return LLMResponse(
                text=text,
                model_id=used_model,
                token_count=token_count,
                latency_ms=round(latency_ms, 2),
                provider=route_provider,
                raw_response=raw,
            )
        except Exception as exc:
            last_error = exc
            LLM_ERRORS.labels(provider=route_provider, error_type=type(exc).__name__).inc()
            logger.warning(
                "LLM %s attempt %s/%s failed: %s",
                route_provider,
                attempt + 1,
                _MAX_ATTEMPTS,
                exc,
            )
            if attempt < _MAX_ATTEMPTS - 1 and _should_retry(exc, route_provider):
                _sleep_backoff(attempt)
                continue
            raise

    assert last_error is not None
    raise last_error


def _collect_stream_gemini(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: str | None,
) -> list[LLMChunk]:
    """Consume the Gemini stream fully before returning chunks (true is_final)."""
    from google.genai import types

    client = _get_gemini_client(api_key)

    kwargs: dict[str, Any] = {"model": model, "contents": prompt}
    if system_prompt:
        kwargs["config"] = types.GenerateContentConfig(system_instruction=system_prompt)

    pieces: list[str] = []
    for chunk in client.models.generate_content_stream(**kwargs):
        pieces.append(getattr(chunk, "text", None) or "")

    return [LLMChunk(text=p, index=i, is_final=i == len(pieces) - 1) for i, p in enumerate(pieces)]


def _collect_stream_openai(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: str | None,
) -> list[LLMChunk]:
    """Consume the OpenAI stream fully before returning chunks (true is_final)."""
    client = _get_openai_client(api_key)

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
    )
    pieces: list[str] = []
    for event in stream:
        choice = event.choices[0]
        delta = choice.delta
        if delta and delta.content:
            pieces.append(delta.content)

    return [LLMChunk(text=p, index=i, is_final=i == len(pieces) - 1) for i, p in enumerate(pieces)]


def _collect_stream_deepseek(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: str | None,
) -> list[LLMChunk]:
    """Consume the DeepSeek stream fully before returning chunks."""
    client = _get_deepseek_client(api_key)

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
    )
    pieces: list[str] = []
    for event in stream:
        choice = event.choices[0]
        delta = choice.delta
        if delta and delta.content:
            pieces.append(delta.content)

    return [LLMChunk(text=p, index=i, is_final=i == len(pieces) - 1) for i, p in enumerate(pieces)]


def generate_stream(
    prompt: str,
    model_id: str | None = None,
    system_prompt: str | None = None,
) -> Generator[LLMChunk]:
    """
    Collect all chunks from the provider, then yield them.

    Retries are safe because chunks are fully collected before any are yielded,
    preventing partial-yield-then-retry corruption.
    """
    route_provider, resolved_model, api_key = _resolve_route(model_id)

    if route_provider == "mock":
        text = mock_llm_text(prompt)
        yield LLMChunk(text=text, index=0, is_final=True)
        return

    if route_provider == "local":
        text, _, _, _ = _call_local_hf(prompt, resolved_model, system_prompt)
        yield LLMChunk(text=text, index=0, is_final=True)
        return

    last_error: BaseException | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            if route_provider == "gemini":
                collected = _collect_stream_gemini(prompt, resolved_model, api_key, system_prompt)
            elif route_provider == "deepseek":
                collected = _collect_stream_deepseek(prompt, resolved_model, api_key, system_prompt)
            else:
                collected = _collect_stream_openai(prompt, resolved_model, api_key, system_prompt)
            yield from collected
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                "LLM stream %s attempt %s/%s failed: %s",
                route_provider,
                attempt + 1,
                _MAX_ATTEMPTS,
                exc,
            )
            if attempt < _MAX_ATTEMPTS - 1 and _should_retry(exc, route_provider):
                _sleep_backoff(attempt)
                continue
            raise

    assert last_error is not None
    raise last_error
