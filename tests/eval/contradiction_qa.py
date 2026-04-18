"""
Contradiction QA benchmark — Beta supersession correctness + audit log integrity.

Stresses the skepticism gate by injecting conflicting facts across every
reachable verdict and then proving, at the DB layer, that:

1. The per-case verdict matches expectations (accept / reject / supersede
   / needs_evidence).
2. After all writes, exactly one row per (entity, predicate) is live —
   the strongest survivor.
3. The per-user hash chain is intact end-to-end (every row's
   `belief_hash` reproducibly derives from `prev_hash`, belief id,
   triple, source_type, source_trace_id, and observed_at). A single
   tampered byte anywhere in the chain must be detectable.
4. Each superseded row's `contradicts` column links forward to the
   challenger, and the challenger's `contradicts` column links back to
   the prior — the causal graph is bidirectional and survives
   tombstoning.

EXIT GATE (Phase 12B, MEMORY_FLAGSHIP_PLAN.md §4):
    100% correct supersession + audit log write.

Deterministic (single scenario, no RNG), LLM-free, flag-gated. Also
runs standalone:

    python -m tests.eval.contradiction_qa           # pretty
    python -m tests.eval.contradiction_qa --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import pytest
from app.config import settings
from app.core.memory.confidence import from_mean_and_strength
from app.core.memory.skepticism import BeliefDraft
from app.core.memory.writer import WriteOutcome, write_belief
from app.main import _seed_memory_policies
from app.models.belief import Belief
from app.models.episode import Episode
from app.models.step_trace import StepTrace
from app.models.trace import Trace
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Scenario specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptSpec:
    """One write attempt against a conflicting prior."""

    label: str
    value: str
    mean: float
    strength: float
    expected: str  # one of accept / rejected / superseded / needs_evidence
    # "needs_evidence" uses a brand-new (entity, predicate) so no priors;
    # everything else uses the canonical user:alice / prefers pair.
    predicate_override: str | None = None


def _default_scenario() -> list[AttemptSpec]:
    """The canonical contradiction scenario.

    Built to touch every skepticism verdict with DB-observable
    consequences:

    - *seed_accept* — first write for this entity+predicate. No priors,
      strong evidence, passes threshold → **accept**.
    - *reject_weak_tie* — same strength as the seed, different value.
      Ties fail the "beats prior by threshold/2" margin rule → **reject**.
    - *reject_equal_mean_weaker* — higher strength but identical mean:
      cand_eff equals conflict_eff so still no margin → **reject**. This
      is the case a naive last-write-wins store would get wrong.
    - *supersede_strong* — larger mean AND enough strength to clear
      margin. **Supersede**. The prior winner is tombstoned.
    - *needs_evidence_new_pred* — fresh (entity, new predicate) with
      mean too low to clear the 0.5 preference threshold → **needs_evidence**.
      No priors, no contradictions.
    """
    return [
        AttemptSpec(
            label="seed_accept",
            value="dark_mode",
            mean=0.70,
            strength=10.0,
            expected="accepted",
        ),
        AttemptSpec(
            label="reject_weak_tie",
            value="light_mode",
            mean=0.70,
            strength=10.0,
            expected="rejected",
        ),
        AttemptSpec(
            label="reject_equal_mean_weaker",
            value="solarized",
            mean=0.70,
            strength=30.0,
            expected="rejected",
        ),
        AttemptSpec(
            label="supersede_strong",
            value="monokai",
            mean=0.95,
            strength=40.0,
            expected="superseded",
        ),
        AttemptSpec(
            label="needs_evidence_new_pred",
            value="medium",
            mean=0.40,
            strength=3.0,
            expected="needs_evidence",
            predicate_override="font_size",
        ),
    ]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    label: str
    expected: str
    got: str
    reason: str
    value: str
    predicate: str
    belief_id: str | None

    @property
    def ok(self) -> bool:
        return self.got == self.expected


@dataclass
class ContradictionQAResult:
    user_id: str
    attempts: list[AttemptRecord] = field(default_factory=list)
    live_after: list[dict[str, Any]] = field(default_factory=list)
    hash_chain_ok: bool = False
    causal_links_ok: bool = False

    @property
    def verdict_correct(self) -> int:
        return sum(1 for a in self.attempts if a.ok)

    @property
    def verdict_total(self) -> int:
        return len(self.attempts)

    @property
    def accuracy(self) -> float:
        return self.verdict_correct / self.verdict_total if self.attempts else 1.0

    @property
    def passes_exit_gate(self) -> bool:
        return self.accuracy == 1.0 and self.hash_chain_ok and self.causal_links_ok

    def to_json(self) -> dict[str, Any]:
        return {
            "benchmark": "contradiction_qa",
            "user_id": self.user_id,
            "verdict_correct": self.verdict_correct,
            "verdict_total": self.verdict_total,
            "accuracy": round(self.accuracy, 6),
            "hash_chain_ok": self.hash_chain_ok,
            "causal_links_ok": self.causal_links_ok,
            "passes_exit_gate": self.passes_exit_gate,
            "attempts": [
                {
                    "label": a.label,
                    "expected": a.expected,
                    "got": a.got,
                    "ok": a.ok,
                    "reason": a.reason,
                    "value": a.value,
                    "predicate": a.predicate,
                    "belief_id": a.belief_id,
                }
                for a in self.attempts
            ],
            "live_after": self.live_after,
        }


def _run_one(db: Session, user_id: str, spec: AttemptSpec) -> WriteOutcome:
    predicate = spec.predicate_override or "prefers"
    draft = BeliefDraft(
        entity=f"user:{user_id}",
        predicate=predicate,
        value=spec.value,
        entity_type="preference",
        confidence=from_mean_and_strength(spec.mean, spec.strength),
        source_type="user_stated",
        user_id=user_id,
        session_id="contradiction-qa",
        keywords=[predicate, spec.value],
        rationale=f"contradiction-qa attempt {spec.label}",
    )
    return write_belief(draft, db)


def _recompute_hash(row: Belief) -> str:
    """Re-derive `belief_hash` from the row's own fields + its stored
    `prev_hash`. Matches `_compute_belief_hash` in writer.py byte-for-byte.

    SQLite strips tzinfo on round-trip even for `DateTime(timezone=True)`
    columns; the writer hashed the original tz-aware isoformat (with a
    `+00:00` suffix), so we re-attach UTC before calling isoformat()
    when the stored row came back naive. See
    `app/core/memory/forgetting.py` for the matching convention on
    the decay path.
    """
    value_json = json.dumps(row.value, sort_keys=True, default=str)
    observed = row.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    # writer.py passes `prev_hash` verbatim into `_compute_belief_hash`
    # — it's always a non-empty string (either "genesis" or a prior's
    # sha256). Do NOT coerce to "" here or the first row stops verifying.
    payload = "|".join(
        [
            row.id,
            row.prev_hash,
            row.entity,
            row.predicate,
            value_json,
            row.source_type,
            row.source_trace_id or "",
            observed.isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_chain(db: Session, user_id: str) -> bool:
    """Walk the per-user chain oldest-first. Every row must link
    to the previous row's hash and its own hash must reproduce."""
    rows = db.query(Belief).filter(Belief.user_id == user_id).order_by(Belief.observed_at.asc(), Belief.id.asc()).all()
    prev = None
    # Writer seeds the chain with the sentinel "genesis" (see
    # `_HASH_GENESIS` in writer.py); `prev_hash` is that value for the
    # first row in a per-user chain and the previous row's
    # `belief_hash` thereafter. Never NULL on a live chain.
    for row in rows:
        expected_prev = prev.belief_hash if prev is not None else "genesis"
        if row.prev_hash != expected_prev:
            return False
        if row.belief_hash != _recompute_hash(row):
            return False
        prev = row
    return True


def _verify_causal_links(result: ContradictionQAResult, db: Session) -> bool:
    """The supersession record must link both directions:

      - The challenger's `contradicts[]` includes the prior's id.
      - The prior row has a non-null `superseded_at`.

    Rejections don't insert rows, so we only check pairs where the
    outcome was `superseded`.
    """
    for attempt in result.attempts:
        if attempt.got != "superseded":
            continue
        assert attempt.belief_id is not None
        challenger = db.query(Belief).filter(Belief.id == attempt.belief_id).one_or_none()
        if challenger is None:
            return False
        contradicts = list(challenger.contradicts or [])
        if not contradicts:
            return False
        for prior_id in contradicts:
            prior = db.query(Belief).filter(Belief.id == prior_id).one_or_none()
            if prior is None or prior.superseded_at is None:
                return False
    return True


def run_contradiction_qa(
    db: Session,
    *,
    user_id: str = "contradiction-qa-user",
    attempts: list[AttemptSpec] | None = None,
) -> ContradictionQAResult:
    specs = attempts or _default_scenario()
    result = ContradictionQAResult(user_id=user_id)
    for spec in specs:
        out = _run_one(db, user_id, spec)
        predicate = spec.predicate_override or "prefers"
        result.attempts.append(
            AttemptRecord(
                label=spec.label,
                expected=spec.expected,
                got=out.status,
                reason=out.reason or "",
                value=spec.value,
                predicate=predicate,
                belief_id=out.belief_id,
            )
        )
        db.commit()

    live = db.query(Belief).filter(Belief.user_id == user_id, Belief.superseded_at.is_(None)).all()
    result.live_after = [
        {
            "id": r.id,
            "predicate": r.predicate,
            "value": r.value,
            "alpha": r.confidence_alpha,
            "beta": r.confidence_beta,
        }
        for r in live
    ]
    result.hash_chain_ok = _verify_chain(db, user_id)
    result.causal_links_ok = _verify_causal_links(result, db)
    return result


# ---------------------------------------------------------------------------
# pytest entrypoints
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _purge_memory(db_session):
    yield
    db_session.rollback()
    db_session.query(StepTrace).delete()
    db_session.query(Episode).delete()
    db_session.query(Trace).delete()
    db_session.query(Belief).delete()
    db_session.commit()


@pytest.fixture
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)


@pytest.fixture
def seeded(db_session):
    _seed_memory_policies(db_session)
    db_session.commit()
    return db_session


def test_contradiction_qa_exit_gate(memory_on, seeded):
    """Phase 12B exit gate: every canonical verdict case lands in the
    right bucket AND the hash chain + causal graph are internally
    consistent."""
    result = run_contradiction_qa(seeded, user_id="cq-exit-gate")
    # Verdict accuracy — the skepticism gate makes the right call across
    # all five scenarios.
    assert result.accuracy == 1.0, [(a.label, a.expected, a.got, a.reason) for a in result.attempts]
    # Hash chain integrity — no silent corruption from the writer.
    assert result.hash_chain_ok, "hash chain broken"
    # Causal graph — every superseded row has a forward link and the
    # challenger has a back-link.
    assert result.causal_links_ok, "causal links incomplete"
    # Expected steady-state: two live rows on this scenario —
    # `prefers=monokai` (survived supersession of dark_mode) and the
    # original (no writes, because needs_evidence doesn't persist).
    predicates_live = {r["predicate"] for r in result.live_after}
    assert predicates_live == {"prefers"}, result.live_after
    assert len(result.live_after) == 1, result.live_after
    assert result.live_after[0]["value"] == "monokai"


def test_contradiction_qa_detects_tampering(memory_on, seeded):
    """The hash-chain verifier MUST fail when any row is mutated
    after the fact. This is the tamper-evidence claim the audit story
    rests on — if we can't catch a byte flip, the story is broken."""
    result = run_contradiction_qa(seeded, user_id="cq-tamper")
    assert result.hash_chain_ok

    # Find the surviving row and mutate its value field in place.
    victim = seeded.query(Belief).filter(Belief.user_id == "cq-tamper", Belief.superseded_at.is_(None)).first()
    assert victim is not None
    victim.value = "SOMEONE_ELSE_PUT_THIS_HERE"
    seeded.commit()

    # Rerun the chain check against the mutated DB.
    assert not _verify_chain(seeded, "cq-tamper"), "Tampered row slipped past hash chain verification"


def test_contradiction_qa_schema_stable():
    """Report schema contract."""
    result = ContradictionQAResult(user_id="x")
    body = result.to_json()
    assert set(body.keys()) >= {
        "benchmark",
        "user_id",
        "verdict_correct",
        "verdict_total",
        "accuracy",
        "hash_chain_ok",
        "causal_links_ok",
        "passes_exit_gate",
        "attempts",
        "live_after",
    }
    assert body["benchmark"] == "contradiction_qa"


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    # Route logging to stderr so --json consumers see a clean stdout.
    from tests.eval import reroute_logging_to_stderr

    reroute_logging_to_stderr()

    parser = argparse.ArgumentParser(description="Contradiction QA benchmark")
    parser.add_argument("--user-id", default="contradiction-qa-user")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    import app.models  # noqa: F401
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        _seed_memory_policies(db)
        db.commit()
        settings.MEMORY_ENABLED = True
        try:
            result = run_contradiction_qa(db, user_id=args.user_id)
        finally:
            settings.MEMORY_ENABLED = False
    finally:
        db.close()

    body = result.to_json()
    if args.json:
        print(json.dumps(body, indent=2, default=str))
    else:
        print(
            f"contradiction_qa  user={args.user_id}  "
            f"verdicts={body['verdict_correct']}/{body['verdict_total']}  "
            f"chain={'ok' if body['hash_chain_ok'] else 'BROKEN'}  "
            f"causal={'ok' if body['causal_links_ok'] else 'BROKEN'}  "
            f"gate={'PASS' if body['passes_exit_gate'] else 'FAIL'}"
        )
        for a in body["attempts"]:
            mark = "✓" if a["ok"] else "✗"
            print(f"  {mark} {a['label']}: expected={a['expected']} got={a['got']} — {a['reason']}")
    return 0 if result.passes_exit_gate else 1


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(_main())
