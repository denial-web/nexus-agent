"""
Governed belief writer — the only sanctioned entry point for persisting beliefs.

Pipeline:

    BeliefDraft → load_priors → skepticism.evaluate → Covernor.evaluate_action
               → (on accept/supersede) hash-chain → persist → mark superseded

Invariants:

1. **Feature-flag inert**: when `settings.MEMORY_ENABLED` is False, this
   function returns a "skipped_flag_off" outcome without touching the DB.
   The regression tripwire (`tests/test_memory_regression.py`) depends on
   this being the only way memory writes are created.

2. **Default-deny**: if Covernor's policy engine does not explicitly
   allow the `memory:write:{entity_type}` action, the belief is rejected
   with status `denied_by_policy`. The writer never bypasses Covernor
   even when skepticism says "accept".

3. **Bitemporal supersede**: when a new belief contradicts a strongly-
   held prior with higher confidence, we set the old belief's
   `superseded_at = now()` (belief-time) without touching `valid_to`
   (world-time). The new belief records the superseded ids in
   `contradicts[]` so the causal chain is recoverable.

4. **Hash chain**: every accepted belief carries `prev_hash` (the
   `belief_hash` of the previous accepted belief in the same user scope,
   or "genesis") and `belief_hash` (sha256 of the record's own immutable
   fields). Tampering is detectable independently of the trace chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.config import settings
from app.core.covernor.policy_engine import PolicyDecision, evaluate_action
from app.core.memory.confidence import BetaConfidence
from app.core.memory.skepticism import (
    BeliefDraft,
    PriorBelief,
    SkepticismDecision,
    parse_stakes,
)
from app.core.memory.skepticism import (
    evaluate as skepticism_evaluate,
)
from app.models.belief import Belief

logger = logging.getLogger(__name__)


WriteStatus = Literal[
    "accepted",
    "superseded",
    "rejected",
    "needs_evidence",
    "denied_by_policy",
    "skipped_flag_off",
    "error",
]


@dataclass
class WriteOutcome:
    """What happened to a candidate belief."""

    status: WriteStatus
    reason: str
    belief_id: str | None = None
    belief_hash: str | None = None
    superseded_ids: list[str] = field(default_factory=list)
    skepticism: SkepticismDecision | None = None
    policy: PolicyDecision | None = None


_HASH_GENESIS = "genesis"


def _compute_belief_hash(
    *,
    belief_id: str,
    prev_hash: str,
    entity: str,
    predicate: str,
    value_json: str,
    source_type: str,
    source_trace_id: str | None,
    observed_at: datetime,
) -> str:
    payload = "|".join(
        [
            belief_id,
            prev_hash,
            entity,
            predicate,
            value_json,
            source_type,
            source_trace_id or "",
            observed_at.isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_belief_hash_for_scope(
    db: Session,
    user_id: str | None,
) -> str:
    """Most recently observed `belief_hash` for a given user scope.

    We chain per-user so tenants have independent tamper-evident logs.
    A NULL user_id uses its own chain for system/shared beliefs.
    """
    q = (
        db.query(Belief.belief_hash)
        .filter(Belief.user_id == user_id)
        .order_by(Belief.observed_at.desc())
        .limit(1)
    )
    row = q.first()
    if row and row[0]:
        return row[0]
    return _HASH_GENESIS


def _load_priors(db: Session, draft: BeliefDraft) -> list[PriorBelief]:
    """Current (non-superseded) priors for the same (user, entity, predicate)."""
    rows = (
        db.query(Belief)
        .filter(
            Belief.entity == draft.entity,
            Belief.predicate == draft.predicate,
            Belief.user_id == draft.user_id,
            Belief.superseded_at.is_(None),
        )
        .order_by(Belief.observed_at.desc())
        .limit(20)
        .all()
    )
    priors: list[PriorBelief] = []
    for r in rows:
        priors.append(
            PriorBelief(
                id=r.id,
                value=r.value,
                confidence=BetaConfidence(
                    alpha=float(r.confidence_alpha or 1.0),
                    beta=float(r.confidence_beta or 1.0),
                ),
                source_type=r.source_type,
            )
        )
    return priors


def write_belief(
    draft: BeliefDraft,
    db: Session,
    *,
    source_trace_id: str | None = None,
    extractor_version: str | None = None,
) -> WriteOutcome:
    """Evaluate and persist a single belief draft under governance.

    Returns a `WriteOutcome` describing what happened. Never raises for
    policy or skepticism decisions — only for database errors, and even
    then the session is rolled back cleanly.
    """
    if not settings.MEMORY_ENABLED:
        return WriteOutcome(
            status="skipped_flag_off",
            reason="MEMORY_ENABLED is False",
        )

    stakes = parse_stakes(settings.MEMORY_STAKES_THRESHOLDS)
    priors = _load_priors(db, draft)
    decision = skepticism_evaluate(draft, priors, stakes)

    if decision.verdict == "reject":
        return WriteOutcome(
            status="rejected",
            reason=decision.reason,
            skepticism=decision,
        )
    if decision.verdict == "needs_evidence":
        return WriteOutcome(
            status="needs_evidence",
            reason=decision.reason,
            skepticism=decision,
        )

    action_type = f"memory:write:{draft.entity_type}"
    policy = evaluate_action(
        action_type=action_type,
        resource=draft.entity,
        parameters={"predicate": draft.predicate},
        db_session=db,
    )
    if policy.decision != "allow":
        return WriteOutcome(
            status="denied_by_policy",
            reason=f"Covernor {policy.decision}: {policy.reason}",
            skepticism=decision,
            policy=policy,
        )

    now = datetime.now(UTC)

    try:
        value_json = json.dumps(draft.value, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        return WriteOutcome(
            status="error",
            reason=f"Value is not JSON-serialisable: {exc}",
            skepticism=decision,
            policy=policy,
        )

    # We need the id in the hash payload, so we generate it up front
    # rather than relying on SQLAlchemy's server-side default.
    belief_id = uuid.uuid4().hex

    new_belief = Belief(
        id=belief_id,
        entity=draft.entity,
        predicate=draft.predicate,
        value=draft.value,
        entity_type=draft.entity_type,
        observed_at=now,
        confidence_alpha=draft.confidence.alpha,
        confidence_beta=draft.confidence.beta,
        source_type=draft.source_type,
        source_trace_id=source_trace_id,
        extractor_version=extractor_version,
        rationale=draft.rationale,
        keywords=list(draft.keywords) if draft.keywords else None,
        embedding=list(draft.embedding) if draft.embedding else None,
        user_id=draft.user_id,
        agent_id=draft.agent_id,
        session_id=draft.session_id,
        derived_from=[],
        contradicts=list(decision.contradicts) if decision.contradicts else [],
    )

    prev_hash = _latest_belief_hash_for_scope(db, draft.user_id)
    new_belief.prev_hash = prev_hash
    new_belief.belief_hash = _compute_belief_hash(
        belief_id=belief_id,
        prev_hash=prev_hash,
        entity=draft.entity,
        predicate=draft.predicate,
        value_json=value_json,
        source_type=draft.source_type,
        source_trace_id=source_trace_id,
        observed_at=now,
    )

    superseded_ids: list[str] = []
    if decision.verdict == "supersede":
        for prior_id in decision.contradicts:
            old = db.query(Belief).filter(Belief.id == prior_id).first()
            if old is not None and old.superseded_at is None:
                old.superseded_at = now
                superseded_ids.append(old.id)

    db.add(new_belief)

    try:
        db.flush()
    except Exception as exc:  # pragma: no cover - surfaced through status
        logger.exception("Failed to persist belief: %s", exc)
        db.rollback()
        return WriteOutcome(
            status="error",
            reason=f"Database error: {exc}",
            skepticism=decision,
            policy=policy,
        )

    final_status: WriteStatus = "superseded" if superseded_ids else "accepted"
    return WriteOutcome(
        status=final_status,
        reason=decision.reason,
        belief_id=new_belief.id,
        belief_hash=new_belief.belief_hash,
        superseded_ids=superseded_ids,
        skepticism=decision,
        policy=policy,
    )


def write_beliefs(
    drafts: list[BeliefDraft],
    db: Session,
    *,
    source_trace_id: str | None = None,
    extractor_version: str | None = None,
) -> list[WriteOutcome]:
    """Write many drafts in sequence. Returns per-draft outcomes.

    Commits/rolls back are the caller's responsibility. We `flush()` per
    draft so subsequent drafts in the same batch see prior accepts when
    loading priors, but the whole batch shares a single transaction.
    """
    outcomes: list[WriteOutcome] = []
    for draft in drafts:
        outcomes.append(
            write_belief(
                draft,
                db,
                source_trace_id=source_trace_id,
                extractor_version=extractor_version,
            )
        )
    return outcomes
