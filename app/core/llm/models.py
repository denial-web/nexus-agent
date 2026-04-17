from dataclasses import dataclass
from typing import Any


@dataclass
class LLMResponse:
    text: str
    model_id: str
    token_count: int
    latency_ms: float
    provider: str  # "gemini", "openai", "mock"
    raw_response: dict[str, Any] | None = None
    request_id: str | None = None


@dataclass
class LLMChunk:
    text: str
    index: int
    is_final: bool = False
