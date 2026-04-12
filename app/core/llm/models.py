from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class LLMResponse:
    text: str
    model_id: str
    token_count: int
    latency_ms: float
    provider: str  # "gemini", "openai", "mock"
    raw_response: Optional[dict[str, Any]] = None


@dataclass
class LLMChunk:
    text: str
    index: int
    is_final: bool = False
