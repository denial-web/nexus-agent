"""
Unified LLM provider: Gemini, OpenAI, DeepSeek, or mock fallback.

Provider selection:
- Explicit model_id starting with "gpt" or "ft:" → OpenAI
- Explicit model_id starting with "gemini" or "models/" → Gemini
- Explicit model_id starting with "deepseek" → DeepSeek
- No model_id: prefer Gemini → OpenAI → DeepSeek → mock

DeepSeek uses the OpenAI SDK with a custom base_url.

Falls back to mock when the required API key is missing (even if model_id
was explicitly requested), logging a warning rather than crashing.
"""
import logging
import time
from collections.abc import Generator
from typing import Any, Optional

from app.config import settings
from app.core.llm.models import LLMChunk, LLMResponse

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3

_gemini_client: Any = None
_gemini_client_key: str = ""
_openai_client: Any = None
_openai_client_key: str = ""
_deepseek_client: Any = None
_deepseek_client_key: str = ""


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
    model_id: Optional[str],
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
    system_prompt: Optional[str],
) -> tuple[str, str, int, Optional[dict[str, Any]]]:
    from google.genai import types

    client = _get_gemini_client(api_key)
    config: Optional[types.GenerateContentConfig] = None
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

    raw: Optional[dict[str, Any]] = None
    try:
        raw = response.model_dump() if hasattr(response, "model_dump") else None
    except Exception:
        raw = None

    return text, model, token_count, raw


def _call_openai(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: Optional[str],
) -> tuple[str, str, int, Optional[dict[str, Any]]]:
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

    raw: Optional[dict[str, Any]] = None
    try:
        raw = response.model_dump()
    except Exception:
        raw = None

    return text, response.model or model, token_count, raw


def _call_deepseek(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: Optional[str],
) -> tuple[str, str, int, Optional[dict[str, Any]]]:
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

    raw: Optional[dict[str, Any]] = None
    try:
        raw = response.model_dump()
    except Exception:
        raw = None

    return text, response.model or model, token_count, raw


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
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> LLMResponse:
    """Generate a full response from the configured provider."""
    route_provider, resolved_model, api_key = _resolve_route(model_id)
    start = time.perf_counter()

    if route_provider == "mock":
        return _mock_response(prompt, start)

    last_error: Optional[BaseException] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            if route_provider == "gemini":
                text, used_model, token_count, raw = _call_gemini(
                    prompt, resolved_model, api_key, system_prompt
                )
            elif route_provider == "deepseek":
                text, used_model, token_count, raw = _call_deepseek(
                    prompt, resolved_model, api_key, system_prompt
                )
            else:
                text, used_model, token_count, raw = _call_openai(
                    prompt, resolved_model, api_key, system_prompt
                )
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
    system_prompt: Optional[str],
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

    return [
        LLMChunk(text=p, index=i, is_final=i == len(pieces) - 1)
        for i, p in enumerate(pieces)
    ]


def _collect_stream_openai(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: Optional[str],
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

    return [
        LLMChunk(text=p, index=i, is_final=i == len(pieces) - 1)
        for i, p in enumerate(pieces)
    ]


def _collect_stream_deepseek(
    prompt: str,
    model: str,
    api_key: str,
    system_prompt: Optional[str],
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

    return [
        LLMChunk(text=p, index=i, is_final=i == len(pieces) - 1)
        for i, p in enumerate(pieces)
    ]


def generate_stream(
    prompt: str,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
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

    last_error: Optional[BaseException] = None
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
