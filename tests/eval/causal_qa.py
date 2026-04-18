"""
Causal QA benchmark — "Why did you recommend X?" derivation DAG.

Nexus stores an explicit causal graph on every belief:

    Belief.derived_from: list[belief_id]   # parents
    Belief.contradicts:  list[belief_id]   # rivals (set by supersession)
    Belief.source_trace_id                 # originating pipeline run

That graph is the differentiator vs OpenClaw/Hermes runtimes: it lets a
deployed agent answer "why did you conclude X?" with a real audit
trail, not a re-prompted hallucinated explanation.

This benchmark builds a deterministic multi-level derivation DAG,
materializes it through the governed writer (via the new
`BeliefDraft.derived_from` field), then proves that for every derived
leaf we can:

1. **Recover the full ancestor set** via BFS over `derived_from`.
2. **Find cycles if any exist** (there should be none — DAG, not cyclic).
3. **Confirm every ancestor is a real, live belief row** — no dangling
   foreign keys in the causal graph.
4. **Distinguish root facts** (empty `derived_from`) from **derived
   conclusions** (non-empty).

EXIT GATE (Phase 12B, MEMORY_FLAGSHIP_PLAN.md §4):
    100% returns valid non-empty derivation DAG.

Deterministic, LLM-free, flag-gated. CLI:

    python -m tests.eval.causal_qa --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.config import settings
from app.core.memory.confidence import from_mean_and_strength
from app.core.memory.skepticism import BeliefDraft
from app.core.memory.writer import write_belief
from app.main import _seed_memory_policies
from app.models.belief import Belief
from app.models.episode import Episode
from app.models.step_trace import StepTrace
from app.models.trace import Trace
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Scenario: a 3-level DAG
# ---------------------------------------------------------------------------
#
# Level 0 (roots, observed):
#   f_location: user:alice lives_in Tokyo
#   f_geo: Tokyo located_in Japan
#   f_tz: Japan uses_timezone JST
#
# Level 1 (derived one hop):
#   f_country:  user:alice country = Japan
#     derived_from = [f_location, f_geo]
#   f_user_tz:  user:alice timezone = JST
#     derived_from = [f_country, f_tz]
#
# Level 2 (derived two hops):
#   f_business_hours: user:alice prefers_meetings_between 09:00_JST_18:00_JST
#     derived_from = [f_user_tz]
#
# Every derived belief must have ≥1 parent; every path from a leaf
# eventually reaches a root with `derived_from == []`. That invariant
# is what makes the "why?" query answerable.

USER_ID = "causal-qa-alice"


@dataclass(frozen=True)
class NodeSpec:
    key: str  # scenario-local alias (maps to assigned belief_id at runtime)
    entity: str
    predicate: str
    value: object
    entity_type: str
    source_type: str
    parents: list[str] = field(default_factory=list)  # NodeSpec.key refs


def _scenario() -> list[NodeSpec]:
    return [
        NodeSpec(
            key="f_location",
            entity=f"user:{USER_ID}",
            predicate="lives_in",
            value="Tokyo",
            entity_type="fact",
            source_type="user_stated",
        ),
        NodeSpec(
            key="f_geo",
            entity="city:Tokyo",
            predicate="located_in",
            value="Japan",
            entity_type="fact",
            source_type="tool",
        ),
        NodeSpec(
            key="f_tz",
            entity="country:Japan",
            predicate="uses_timezone",
            value="JST",
            entity_type="fact",
            source_type="tool",
        ),
        NodeSpec(
            key="f_country",
            entity=f"user:{USER_ID}",
            predicate="country",
            value="Japan",
            entity_type="preference",
            source_type="inferred",
            parents=["f_location", "f_geo"],
        ),
        NodeSpec(
            key="f_user_tz",
            entity=f"user:{USER_ID}",
            predicate="timezone",
            value="JST",
            entity_type="preference",
            source_type="inferred",
            parents=["f_country", "f_tz"],
        ),
        NodeSpec(
            key="f_business_hours",
            entity=f"user:{USER_ID}",
            predicate="prefers_meetings_between",
            value="09:00_JST_18:00_JST",
            entity_type="preference",
            source_type="inferred",
            parents=["f_user_tz"],
        ),
    ]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CausalQAResult:
    user_id: str
    root_count: int = 0
    derived_count: int = 0
    # For each derived leaf, the recovered ancestor set (belief ids).
    derivations: dict[str, list[str]] = field(default_factory=dict)
    missing_parents: list[dict[str, str]] = field(default_factory=list)
    cycles_detected: list[list[str]] = field(default_factory=list)
    all_written: int = 0

    @property
    def passes_exit_gate(self) -> bool:
        return (
            self.derived_count > 0
            and all(len(anc) > 0 for anc in self.derivations.values())
            and not self.missing_parents
            and not self.cycles_detected
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "benchmark": "causal_qa",
            "user_id": self.user_id,
            "all_written": self.all_written,
            "root_count": self.root_count,
            "derived_count": self.derived_count,
            "derivations": self.derivations,
            "missing_parents": self.missing_parents,
            "cycles_detected": self.cycles_detected,
            "passes_exit_gate": self.passes_exit_gate,
        }


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _write_dag(db: Session, specs: list[NodeSpec], *, user_id: str) -> dict[str, str]:
    """Materialize the scenario DAG, one node per write, respecting
    parent ordering. Returns {key -> belief_id}.

    Uses monotonically increasing confidence so leaves never collide
    with earlier roots under the skepticism gate (different
    entity+predicate pairs, but the gate's `priors` query is scoped
    exactly by that pair, so tie-breaks never trigger here).
    """
    key_to_id: dict[str, str] = {}
    for i, spec in enumerate(specs):
        parent_ids = [key_to_id[p] for p in spec.parents]
        draft = BeliefDraft(
            entity=spec.entity,
            predicate=spec.predicate,
            value=spec.value,
            entity_type=spec.entity_type,
            confidence=from_mean_and_strength(0.85, 10.0 + i),
            source_type=spec.source_type,
            user_id=user_id,
            session_id="causal-qa",
            keywords=[spec.predicate],
            rationale=f"causal-qa dag node {spec.key}",
            derived_from=parent_ids or None,
        )
        out = write_belief(draft, db)
        assert out.status == "accepted", f"{spec.key} did not land cleanly: {out.status} / {out.reason}"
        key_to_id[spec.key] = out.belief_id
        db.commit()
    return key_to_id


def _ancestors(db: Session, belief_id: str, *, by_id: dict[str, Belief]) -> tuple[list[str], list[list[str]]]:
    """BFS over `derived_from` from `belief_id`. Returns
    (ordered_unique_ancestors, cycles_found). Cycles are returned as a
    list of paths, not just flagged, so we can log the exact offending
    chain. A valid derivation DAG yields an empty cycles list.
    """
    ancestors: list[str] = []
    seen: set[str] = set()
    cycles: list[list[str]] = []

    frontier: list[tuple[str, tuple[str, ...]]] = [(belief_id, (belief_id,))]
    while frontier:
        node_id, path = frontier.pop(0)
        row = by_id.get(node_id)
        if row is None:
            continue
        for parent_id in row.derived_from or []:
            if parent_id in path:
                cycles.append([*path, parent_id])
                continue
            if parent_id not in seen:
                seen.add(parent_id)
                ancestors.append(parent_id)
            frontier.append((parent_id, (*path, parent_id)))
    return ancestors, cycles


def run_causal_qa(db: Session, *, user_id: str = USER_ID) -> CausalQAResult:
    specs = _scenario()
    key_to_id = _write_dag(db, specs, user_id=user_id)

    rows = db.query(Belief).filter(Belief.user_id == user_id).all()
    by_id: dict[str, Belief] = {r.id: r for r in rows}

    result = CausalQAResult(user_id=user_id, all_written=len(rows))

    for spec in specs:
        bid = key_to_id[spec.key]
        row = by_id[bid]
        parents = list(row.derived_from or [])
        if not parents:
            result.root_count += 1
            continue
        result.derived_count += 1

        ancestors, cycles = _ancestors(db, bid, by_id=by_id)
        result.derivations[bid] = ancestors
        if cycles:
            result.cycles_detected.extend(cycles)
        # Every recorded parent must resolve to a live row.
        for parent_id in parents:
            if parent_id not in by_id:
                result.missing_parents.append({"child": bid, "missing_parent": parent_id})

    return result


# ---------------------------------------------------------------------------
# pytest entrypoints (CI = exit gate)
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


def _seed_fact_write_policy(db: Session) -> None:
    """Supplement the default `memory:*` policies with a scoped allow
    for `memory:write:fact` so the scenario's world-fact nodes
    (Tokyo→Japan, Japan→JST) can be persisted. Without this, the
    default-deny correctly blocks them — which is the point of the
    Covernor integration. The causal_qa benchmark is an explicit
    opt-in for fact writes in a research/eval context."""
    from app.models.policy import Policy

    exists = db.query(Policy).filter(Policy.name == "memory-allow-fact-write").first()
    if exists is not None:
        return
    db.add(
        Policy(
            name="memory-allow-fact-write",
            description="Allow fact-type belief writes for causal_qa benchmark",
            action_pattern="memory:write:fact",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=50,
            is_active=True,
        )
    )


@pytest.fixture
def seeded(db_session):
    _seed_memory_policies(db_session)
    _seed_fact_write_policy(db_session)
    db_session.commit()
    return db_session


def test_causal_qa_exit_gate(memory_on, seeded):
    """Phase 12B exit gate: every derived belief in the canonical DAG
    returns a non-empty, cycle-free, fully-resolved ancestor set."""
    result = run_causal_qa(seeded, user_id="cq-causal-exit")
    assert result.all_written == 6
    assert result.root_count == 3, "expected 3 root facts in the scenario"
    assert result.derived_count == 3, "expected 3 derived conclusions"
    # Each derived belief has at least one ancestor.
    for bid, anc in result.derivations.items():
        assert len(anc) > 0, f"belief {bid} returned empty derivation"
    # No dangling parents.
    assert result.missing_parents == [], result.missing_parents
    # DAG, not cyclic.
    assert result.cycles_detected == [], result.cycles_detected
    # The deepest leaf (business_hours) must transitively reach all 3 roots.
    # We find it as the single row whose own `derived_from` chain is longest.
    biggest = max(result.derivations.values(), key=len)
    assert len(biggest) >= 3, f"transitive closure too small: {biggest} — deepest leaf should reach every root fact"
    assert result.passes_exit_gate


def test_causal_qa_root_facts_have_no_parents(memory_on, seeded):
    """The benchmark's definition of 'root fact' is `derived_from == []`.
    That definition is what makes the 'why?' traversal well-founded
    (no infinite walks). Enforce it explicitly."""
    run_causal_qa(seeded, user_id="cq-causal-roots")
    rows = seeded.query(Belief).filter(Belief.user_id == "cq-causal-roots").all()
    for row in rows:
        parents = row.derived_from or []
        if row.predicate in {"lives_in", "located_in", "uses_timezone"}:
            assert parents == [], f"root fact {row.predicate} leaked a parent: {parents}"
        else:
            assert parents, f"derived belief {row.predicate} has no parents"


def test_causal_qa_schema_stable():
    result = CausalQAResult(user_id="x")
    body = result.to_json()
    assert set(body.keys()) == {
        "benchmark",
        "user_id",
        "all_written",
        "root_count",
        "derived_count",
        "derivations",
        "missing_parents",
        "cycles_detected",
        "passes_exit_gate",
    }
    assert body["benchmark"] == "causal_qa"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    # Route logging to stderr so --json consumers see a clean stdout.
    from tests.eval import reroute_logging_to_stderr

    reroute_logging_to_stderr()

    parser = argparse.ArgumentParser(description="Causal QA benchmark")
    parser.add_argument("--user-id", default=USER_ID)
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
        _seed_fact_write_policy(db)
        db.commit()
        settings.MEMORY_ENABLED = True
        try:
            result = run_causal_qa(db, user_id=args.user_id)
        finally:
            settings.MEMORY_ENABLED = False
    finally:
        db.close()

    body = result.to_json()
    if args.json:
        print(json.dumps(body, indent=2, default=str))
    else:
        print(
            f"causal_qa  user={args.user_id}  "
            f"roots={body['root_count']}  derived={body['derived_count']}  "
            f"missing_parents={len(body['missing_parents'])}  "
            f"cycles={len(body['cycles_detected'])}  "
            f"gate={'PASS' if body['passes_exit_gate'] else 'FAIL'}"
        )
        for bid, anc in body["derivations"].items():
            print(f"  {bid[:8]}: {len(anc)} ancestors -> {[a[:8] for a in anc]}")
    return 0 if result.passes_exit_gate else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
