"""
A-S-FLC Analyzer — LLM-powered decision path decomposition.

Given a user prompt, asks the LLM to decompose the request into
alternative decision paths with probabilistic event chains.  The
engine then evaluates those paths with asymmetric risk treatment and
returns the safest high-confidence path to guide generation.

Short / simple prompts skip analysis to avoid unnecessary latency.
"""

import json
import logging
import re
from dataclasses import dataclass

from app.config import settings
from app.core.asflc.engine import (
    ASFLCResult,
    DecisionPath,
    EventNode,
    build_paths_from_llm_output,
    evaluate_paths,
)

logger = logging.getLogger(__name__)

_MIN_PROMPT_WORDS = 8

_DECOMPOSITION_SYSTEM_PROMPT = """\
You are a decision-path analyst. Given a user request, decompose it into \
2-4 alternative decision paths the AI agent could take to fulfill it. \
For each path, list 2-5 event nodes with probability, impact, and polarity.

Respond with ONLY a JSON array. Each element:
{
  "name": "short path label",
  "events": [
    {"description": "what happens", "probability": 0.0-1.0, "impact": -100 to 100, "is_positive": true/false}
  ]
}

Rules:
- probability: likelihood this event occurs (0.0-1.0)
- impact: magnitude of consequence (-100 to 100, negative = harmful)
- is_positive: true if the outcome is beneficial, false if risky/harmful
- Always include at least one path that represents a cautious/safe approach
- Always include at least one path that represents the most direct approach
"""


@dataclass
class AnalysisResult:
    asflc: ASFLCResult
    chosen_path: str
    confidence: float
    loops: int
    raw_paths: list[dict]
    system_hint: str


def _looks_trivial(prompt: str) -> bool:
    return len(prompt.split()) < _MIN_PROMPT_WORDS


def _extract_json_array(text: str) -> list | None:
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    bracket = text.find("[")
    if bracket == -1:
        return None

    depth = 0
    end = bracket
    for i in range(bracket, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    else:
        return None

    try:
        return json.loads(text[bracket:end])
    except (json.JSONDecodeError, ValueError):
        return None


def _build_system_hint(result: ASFLCResult) -> str:
    parts = [f"Chosen approach: {result.chosen_path}."]
    if result.chain_regret > 0:
        parts.append(
            f"Note: regret score is {result.chain_regret:.2f} — consider mentioning trade-offs in your response."
        )
    if result.confidence < 0.7:
        parts.append("Confidence is low — hedge your claims and note uncertainty.")
    return " ".join(parts)


def _default_paths(prompt: str) -> list[DecisionPath]:
    return [
        DecisionPath(
            name="direct_response",
            events=[
                EventNode("Provide straightforward answer", 0.9, 60, True),
                EventNode("May miss nuance", 0.3, -20, False),
            ],
        ),
        DecisionPath(
            name="cautious_response",
            events=[
                EventNode("Provide hedged answer with caveats", 0.85, 40, True),
                EventNode("May be overly verbose", 0.4, -10, False),
            ],
        ),
    ]


def analyze(prompt: str, model_id: str | None = None) -> AnalysisResult | None:
    """
    Decompose a prompt into decision paths via the LLM, then evaluate them.

    Returns None for trivial prompts (skips analysis to save latency).
    Falls back to heuristic default paths if the LLM call or parsing fails.
    """
    if _looks_trivial(prompt):
        logger.debug("Skipping A-S-FLC analysis for short prompt (%d words)", len(prompt.split()))
        return None

    raw_paths: list | None = None
    used_fallback = False

    try:
        from app.core.llm.provider import generate

        llm_model = (model_id or settings.CRITIC_MODEL or "").strip() or None
        llm_resp = generate(
            prompt=f"Analyze this user request:\n\n{prompt}",
            model_id=llm_model,
            system_prompt=_DECOMPOSITION_SYSTEM_PROMPT,
        )
        raw_paths = _extract_json_array(llm_resp.text)
    except Exception:
        logger.warning("A-S-FLC LLM decomposition failed; using default paths", exc_info=True)

    if not raw_paths or not isinstance(raw_paths, list) or len(raw_paths) < 2:
        logger.info("A-S-FLC falling back to default paths")
        paths = _default_paths(prompt)
        raw_paths = []
        used_fallback = True
    else:
        try:
            paths = build_paths_from_llm_output(raw_paths)
        except Exception:
            logger.warning("Failed to parse LLM decision paths; using defaults", exc_info=True)
            paths = []
        if len(paths) < 2:
            paths = _default_paths(prompt)
            raw_paths = []
            used_fallback = True

    result = evaluate_paths(paths)

    return AnalysisResult(
        asflc=result,
        chosen_path=result.chosen_path,
        confidence=result.confidence,
        loops=result.loops_taken,
        raw_paths=raw_paths if not used_fallback else [{"fallback": True}],
        system_hint=_build_system_hint(result),
    )
