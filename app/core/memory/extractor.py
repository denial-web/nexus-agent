"""
Belief extractor — turns (prompt, response) into `BeliefDraft` candidates.

The extractor is intentionally small. It prompts the configured LLM with a
constrained JSON schema and parses the output into drafts. Every draft
still has to survive the skepticism gate and the Covernor policy engine
downstream — the extractor is not trusted to decide what is worth storing,
only to *propose* triples it noticed.

Contract:

    drafts = extract_beliefs(
        user_message="I prefer short answers",
        assistant_response="Got it.",
        user_id="alice",
        session_id="s-1",
    )

Invariants:

1. **Feature-flag inert.** When `settings.MEMORY_ENABLED` is False, the
   function returns an empty list without making any LLM call.

2. **Never raises.** Malformed LLM output, provider errors, and schema
   mismatches all degrade to an empty list with a logged warning. The
   extractor must never break a pipeline run.

3. **Input capped.** The combined prompt/response is clipped to
   `settings.MEMORY_EXTRACTOR_MAX_CHARS` (default 8k) to keep cost bounded.

4. **Version-stamped.** The module exposes `EXTRACTOR_VERSION` so writers
   can record which extractor produced a belief. Bump on prompt or schema
   changes so stale beliefs can be re-labeled later.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.core.llm.provider import generate
from app.core.memory.confidence import from_mean_and_strength
from app.core.memory.skepticism import BeliefDraft

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "v1.0.0-preference"

_ALLOWED_ENTITY_TYPES = {"preference", "identity", "state", "context", "financial"}
_ALLOWED_SOURCE_TYPES = {
    "user_stated",
    "observed",
    "inferred",
    "tool",
    "imported",
}

_MAX_DRAFTS = 8  # hard cap per extraction call — prevents runaway storage

_SYSTEM_PROMPT = (
    "You extract durable beliefs about the user from a conversation. "
    "Return ONLY a JSON array. Each element is an object with keys: "
    "entity, predicate, value, entity_type, confidence, rationale. "
    "entity_type must be one of: preference, identity, state, context. "
    "confidence is a float in [0.1, 0.99] reflecting how sure you are. "
    "Extract beliefs the user stated directly or strongly implied. "
    "Skip ephemeral facts (what they are asking RIGHT NOW). "
    "If nothing durable is present, return []."
)

_USER_TEMPLATE = (
    "User message:\n{user}\n\n"
    "Assistant response:\n{assistant}\n\n"
    "Return JSON array of beliefs (max {max_drafts}). If none: []."
)


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    """Best-effort JSON array extraction from an LLM reply.

    Handles ```json fences and free-form text around a JSON block.
    Returns [] on any parse failure.
    """
    if not raw:
        return []
    text = raw.strip()
    # Strip ``` fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(text)
        if not match:
            return []
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _coerce_confidence(raw: Any) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.5
    # Clamp — Beta params become degenerate outside this band.
    if val < 0.1:
        return 0.1
    if val > 0.99:
        return 0.99
    return val


def _to_draft(
    item: dict[str, Any],
    *,
    user_id: str | None,
    session_id: str | None,
    agent_id: str | None,
) -> BeliefDraft | None:
    """Validate one LLM-proposed triple and build a BeliefDraft.

    Returns None on any validation failure so the caller can silently skip.
    """
    entity = item.get("entity")
    predicate = item.get("predicate")
    value = item.get("value")
    entity_type = item.get("entity_type", "preference")

    if not isinstance(entity, str) or not entity.strip():
        return None
    if not isinstance(predicate, str) or not predicate.strip():
        return None
    if value is None:
        return None
    if entity_type not in _ALLOWED_ENTITY_TYPES:
        return None

    confidence_mean = _coerce_confidence(item.get("confidence", 0.7))
    # Strength=8 gives BetaConfidence a modest sample size so a single
    # contradicting observation can still be overruled by a stronger one.
    confidence = from_mean_and_strength(confidence_mean, strength=8.0)

    source_type = item.get("source_type", "user_stated")
    if source_type not in _ALLOWED_SOURCE_TYPES:
        source_type = "user_stated"

    rationale = item.get("rationale")
    if not isinstance(rationale, str):
        rationale = None

    # Normalize string values for the skepticism gate (lowercased strings
    # compare deterministically and match retrieval keywords).
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

    return BeliefDraft(
        entity=entity.strip(),
        predicate=predicate.strip(),
        value=value,
        entity_type=entity_type,
        confidence=confidence,
        source_type=source_type,
        user_id=user_id,
        session_id=session_id,
        agent_id=agent_id,
        keywords=_keywords_for(predicate, value),
        rationale=rationale,
    )


def _keywords_for(predicate: str, value: Any) -> list[str] | None:
    """Cheap keyword seed for the retrieval layer.

    Splits predicate tokens and includes short stringified values so the
    lexical signal in `retrieval.py` has something to match without
    requiring an embedding call at write time.
    """
    tokens: list[str] = []
    for chunk in re.split(r"[^a-zA-Z0-9]+", predicate):
        if len(chunk) >= 3:
            tokens.append(chunk.lower())
    if isinstance(value, str):
        for chunk in re.split(r"\s+", value):
            chunk = chunk.strip().lower()
            if 3 <= len(chunk) <= 40:
                tokens.append(chunk)
    if not tokens:
        return None
    seen: dict[str, None] = {}
    for t in tokens:
        seen.setdefault(t, None)
    return list(seen.keys())


def extract_beliefs(
    *,
    user_message: str,
    assistant_response: str,
    user_id: str | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    model_id: str | None = None,
) -> list[BeliefDraft]:
    """Run the LLM-backed belief extractor on a conversation turn.

    Returns zero or more BeliefDraft candidates. Callers should pass each
    draft through `app.core.memory.writer.write_belief` to persist under
    governance.
    """
    if not settings.MEMORY_ENABLED:
        return []

    if not user_message and not assistant_response:
        return []

    max_chars = max(0, settings.MEMORY_EXTRACTOR_MAX_CHARS)
    user_clipped = _clip(user_message or "", max_chars // 2)
    asst_clipped = _clip(assistant_response or "", max_chars // 2)

    prompt = _USER_TEMPLATE.format(
        user=user_clipped,
        assistant=asst_clipped,
        max_drafts=_MAX_DRAFTS,
    )

    effective_model = model_id or settings.EXTRACTION_MODEL or None

    try:
        response = generate(
            prompt=prompt,
            model_id=effective_model,
            system_prompt=_SYSTEM_PROMPT,
        )
    except Exception as exc:  # noqa: BLE001 — extractor must never raise
        logger.warning("Belief extractor LLM call failed: %s", exc)
        return []

    raw_items = _parse_json_array(response.text or "")
    if not raw_items:
        return []

    drafts: list[BeliefDraft] = []
    for item in raw_items[:_MAX_DRAFTS]:
        draft = _to_draft(
            item,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
        )
        if draft is not None:
            drafts.append(draft)

    return drafts
