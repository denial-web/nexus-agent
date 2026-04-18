"""
Skepticism gate — the "should we actually believe this?" check.

The skepticism layer runs BEFORE a belief is written and BEFORE the Covernor
governance check. Its job is epistemic: it decides whether the incoming
evidence is strong enough to justify changing (or creating) a stored belief,
given (a) the stakes of being wrong and (b) what we already believe.

Contract:

    decision = evaluate(
        candidate,                     # Candidate belief (BeliefDraft)
        prior_beliefs,                 # Existing current beliefs for same (entity, predicate)
        settings_thresholds,           # Parsed MEMORY_STAKES_THRESHOLDS
    )

Returned SkepticismDecision carries:

  - verdict: "accept" | "supersede" | "reject" | "needs_evidence"
  - reason: machine-readable code for audit
  - required_confidence: the threshold we compared against
  - contradicts: list of belief ids the candidate would overturn

This module is pure logic — it does not touch the DB, LLMs, or the Covernor.
The writer layer wires it up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.core.memory.confidence import BetaConfidence

# Default stakes table. Keys must stay in sync with Belief.entity_type
# domain values ("identity", "financial", "preference", "state", "context").
# MEMORY_STAKES_THRESHOLDS in app/config.py overrides these at runtime.
DEFAULT_STAKES: dict[str, float] = {
    "identity": 0.9,
    "financial": 0.85,
    "preference": 0.5,
    "state": 0.3,
    "context": 0.2,
}

Verdict = Literal["accept", "supersede", "reject", "needs_evidence"]


@dataclass(frozen=True)
class PriorBelief:
    """Minimal projection of an existing Belief row used by the gate.

    We deliberately don't import app.models.belief here — that would
    couple the pure-logic gate to SQLAlchemy. The writer projects the
    Belief ORM row into this dataclass.
    """

    id: str
    value: object
    confidence: BetaConfidence
    source_type: str


@dataclass(frozen=True)
class BeliefDraft:
    """A candidate belief the extractor wants to commit."""

    entity: str
    predicate: str
    value: object
    entity_type: str
    confidence: BetaConfidence
    source_type: str  # "observed" | "inferred" | "tool" | "user_stated" | "imported"
    # Scope fields — required by the writer for tenant isolation. All
    # default to None so unit tests of the skepticism gate don't need to
    # specify them.
    user_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    # Retrieval signals the extractor pre-computes so the writer can store
    # them without a second LLM/embedding pass.
    keywords: list[str] | None = None
    embedding: list[float] | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class SkepticismDecision:
    verdict: Verdict
    reason: str
    required_confidence: float
    contradicts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Trust weights per source_type. "user_stated" > "tool" > "observed" > "inferred".
# Multiplier scales the effective confidence used for gate comparison without
# mutating the stored Beta parameters.
# ---------------------------------------------------------------------------

_SOURCE_TRUST: dict[str, float] = {
    "user_stated": 1.00,
    "tool": 0.90,
    "observed": 0.75,
    "imported": 0.70,
    "inferred": 0.60,
}


def parse_stakes(raw: str) -> dict[str, float]:
    """Parse "identity=0.9,financial=0.85,..." from settings into a dict.

    Unknown keys are preserved; malformed pairs are dropped with no error
    (the config validator warns about those at boot). Missing keys fall
    through to DEFAULT_STAKES.
    """
    out: dict[str, float] = dict(DEFAULT_STAKES)
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        try:
            out[key] = float(value.strip())
        except ValueError:
            continue
    return out


def _effective_confidence(c: BetaConfidence, source_type: str) -> float:
    trust = _SOURCE_TRUST.get(source_type, _SOURCE_TRUST["observed"])
    return c.mean * trust


def _values_conflict(a: object, b: object) -> bool:
    """Cheap structural inequality check.

    We keep this intentionally dumb: exact Python `!=`. The extractor is
    responsible for normalizing values (strings lowercased, numbers cast)
    before handing drafts to the gate. This keeps the gate deterministic
    and easy to reason about.
    """
    return a != b


def evaluate(
    candidate: BeliefDraft,
    prior_beliefs: list[PriorBelief],
    stakes: dict[str, float] | None = None,
) -> SkepticismDecision:
    """Run the skepticism gate on a candidate belief.

    Algorithm:

    1. Look up the stakes threshold for the candidate's entity_type.
       Unknown types default to 0.5.
    2. Apply the source-trust multiplier to get effective confidence.
    3. If there are no priors:
          - Accept iff effective_confidence >= threshold.
          - Otherwise reject with "needs_evidence" (low stakes still
            need SOME evidence; it's just a lower bar).
    4. If priors exist:
          - If the candidate agrees with every current prior, accept.
          - If the candidate disagrees with at least one current prior:
              - To supersede, the candidate's effective confidence must
                exceed BOTH the stakes threshold AND the strongest
                conflicting prior's effective confidence by a margin
                equal to half the stakes threshold (so high-stakes
                beliefs require strictly more evidence to overturn).
              - Otherwise reject.
    """
    stakes_table = stakes if stakes is not None else DEFAULT_STAKES
    threshold = stakes_table.get(candidate.entity_type, 0.5)

    cand_eff = _effective_confidence(candidate.confidence, candidate.source_type)

    conflicts = [
        p for p in prior_beliefs if _values_conflict(candidate.value, p.value)
    ]

    if not prior_beliefs:
        if cand_eff >= threshold:
            return SkepticismDecision(
                verdict="accept",
                reason="new_belief_over_threshold",
                required_confidence=threshold,
            )
        return SkepticismDecision(
            verdict="needs_evidence",
            reason=f"confidence_{cand_eff:.3f}_below_threshold_{threshold:.3f}",
            required_confidence=threshold,
        )

    if not conflicts:
        return SkepticismDecision(
            verdict="accept",
            reason="agrees_with_existing_beliefs",
            required_confidence=threshold,
        )

    strongest_conflict = max(
        conflicts,
        key=lambda p: _effective_confidence(p.confidence, p.source_type),
    )
    conflict_eff = _effective_confidence(
        strongest_conflict.confidence, strongest_conflict.source_type
    )
    margin = threshold / 2.0

    if cand_eff >= threshold and cand_eff >= conflict_eff + margin:
        return SkepticismDecision(
            verdict="supersede",
            reason=(
                f"candidate_{cand_eff:.3f}_beats_prior_{conflict_eff:.3f}"
                f"_by_margin_{margin:.3f}"
            ),
            required_confidence=threshold,
            contradicts=[c.id for c in conflicts],
        )

    return SkepticismDecision(
        verdict="reject",
        reason=(
            f"conflict_with_stronger_prior_{conflict_eff:.3f}"
            f"_vs_candidate_{cand_eff:.3f}"
        ),
        required_confidence=threshold,
        contradicts=[c.id for c in conflicts],
    )
