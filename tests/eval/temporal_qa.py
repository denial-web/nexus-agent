"""
Temporal QA benchmark — bitemporal recall on synthetic transition sets.

Question template:
    "User moved X -> Y on date D. What did you believe on date D - 1?"

A correct run returns the *pre-transition* value for any query time
before the transition, and the *post-transition* value for any query
time after it. Multi-hop scenarios extend this to N transitions.

This is the benchmark that proves Nexus's bitemporal axis works — a
capability OpenClaw / Hermes runtimes do not have out of the box.

EXIT GATE (Phase 12B, MEMORY_FLAGSHIP_PLAN.md §4):
    100% correct belief-at-time-T queries on the synthetic set.

The harness is deterministic (seed-reproducible), synthetic (no
external datasets), and LLM-free (we measure DB-layer bitemporal
semantics directly). It also runs as a standalone report script:

    python -m tests.eval.temporal_qa            # pretty
    python -m tests.eval.temporal_qa --json     # JSON to stdout
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.config import settings
from app.core.memory.retrieval import beliefs_as_of
from app.main import _seed_memory_policies
from app.models.belief import Belief
from app.models.episode import Episode
from app.models.step_trace import StepTrace
from app.models.trace import Trace
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    at: datetime
    value: str


@dataclass(frozen=True)
class Scenario:
    user_id: str
    entity: str
    predicate: str
    transitions: list[Transition]
    # Query timestamps: for each transition t, we emit (t - 1d, expected_pre)
    # and (t + 1d, expected_post). Plus one pre-first and one post-last.
    queries: list[tuple[datetime, str | None]]


_VALUE_POOL = [
    "dark_mode",
    "light_mode",
    "auto_theme",
    "high_contrast",
    "solarized",
    "monokai",
    "dracula",
    "nord",
    "gruvbox",
    "one_dark",
]


def generate_scenario(
    seed: int,
    n_transitions: int = 3,
    *,
    user_id: str | None = None,
    base_time: datetime | None = None,
) -> Scenario:
    """Build a deterministic bitemporal scenario for a single user.

    `n_transitions` produces `n_transitions + 1` distinct belief values
    spaced at least 7 days apart, and `2 * n_transitions + 2` queries
    (pre/post each transition, plus one on each tail). The tail queries
    ensure we test "before any belief was written" (→ None) and "long
    after the final transition" (→ last value) — both are real-world
    failure modes for naive last-write-wins stores.
    """
    rng = random.Random(seed)
    base = base_time or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    uid = user_id or f"temporal-qa-user-{seed}"
    # Sample values without replacement so each transition is a genuine
    # value change (otherwise the "as-of" answer is trivially the same).
    values = rng.sample(_VALUE_POOL, k=n_transitions + 1)

    transitions: list[Transition] = []
    cursor = base
    for v in values:
        transitions.append(Transition(at=cursor, value=v))
        cursor = cursor + timedelta(days=rng.randint(7, 30))

    queries: list[tuple[datetime, str | None]] = []
    # Tail: one full day before the first write — we should know nothing.
    queries.append((transitions[0].at - timedelta(days=1), None))
    # One day before each subsequent transition → previous value still live.
    for i in range(1, len(transitions)):
        queries.append((transitions[i].at - timedelta(days=1), transitions[i - 1].value))
    # One day after each transition → that transition's value is live.
    for t in transitions:
        queries.append((t.at + timedelta(days=1), t.value))
    # Tail: 365 days after the final transition → still the last value.
    queries.append((transitions[-1].at + timedelta(days=365), transitions[-1].value))

    return Scenario(
        user_id=uid,
        entity=f"user:{uid}",
        predicate="prefers",
        transitions=transitions,
        queries=queries,
    )


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


@dataclass
class TemporalQAResult:
    scenario_seed: int
    n_transitions: int
    n_queries: int
    n_correct: int
    n_wrong: int
    wrong_examples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        total = self.n_correct + self.n_wrong
        return (self.n_correct / total) if total else 1.0

    def to_json(self) -> dict[str, Any]:
        return {
            "benchmark": "temporal_qa",
            "scenario_seed": self.scenario_seed,
            "n_transitions": self.n_transitions,
            "n_queries": self.n_queries,
            "n_correct": self.n_correct,
            "n_wrong": self.n_wrong,
            "accuracy": round(self.accuracy, 6),
            # Uniform boolean gate across every benchmark in tests/eval/.
            # The nightly workflow reads this single key to decide
            # pass/fail — numeric metrics above are for the scoreboard.
            # See .github/workflows/nightly_benchmark.yml.
            "passes_exit_gate": self.accuracy == 1.0,
            "wrong_examples": self.wrong_examples,
        }


def _write_scenario(db: Session, scenario: Scenario) -> list[str]:
    """Materialize the scenario's belief transitions directly as rows.

    Deliberately bypasses the skepticism gate: this benchmark measures
    the DB-layer bitemporal axis (the `beliefs_as_of` query's
    correctness), not the skepticism policy. Supersession semantics
    are exercised end-to-end by `tests/eval/contradiction_qa.py`.

    Each transition's row is stamped with its scenario `observed_at`,
    and the previous live row (same entity+predicate+user) is
    superseded *at that same timestamp* — the canonical bitemporal
    invariant (`superseded_at_i = observed_at_{i+1}`). Without this,
    an "as-of T" query between two transitions would see both rows as
    live, which is exactly the bug mode we want to be impossible.
    """
    import uuid

    written: list[str] = []
    prior_id: str | None = None
    for i, transition in enumerate(scenario.transitions):
        bid = uuid.uuid4().hex
        row = Belief(
            id=bid,
            entity=scenario.entity,
            predicate=scenario.predicate,
            value=transition.value,
            entity_type="preference",
            observed_at=transition.at,
            confidence_alpha=9.0,
            confidence_beta=1.0,
            source_type="user_stated",
            user_id=scenario.user_id,
            session_id="temporal-qa",
            keywords=[scenario.predicate, transition.value],
            rationale=f"scenario transition {i}",
            derived_from=[],
            contradicts=[prior_id] if prior_id else [],
        )
        if prior_id is not None:
            prior = db.query(Belief).filter(Belief.id == prior_id).one()
            prior.superseded_at = transition.at
        db.add(row)
        db.flush()
        written.append(bid)
        prior_id = bid
    db.commit()
    return written


def run_temporal_qa(
    db: Session,
    *,
    seed: int = 0,
    n_transitions: int = 3,
) -> TemporalQAResult:
    """Execute one seeded temporal QA scenario end-to-end.

    Precondition: caller has set `MEMORY_ENABLED=True` and seeded the
    `memory:write:preference` Covernor allow-policy (both handled by
    the pytest fixtures below and by `main()` for CLI use).
    """
    scenario = generate_scenario(seed=seed, n_transitions=n_transitions)
    _write_scenario(db, scenario)

    n_correct = 0
    n_wrong = 0
    wrong: list[dict[str, Any]] = []
    for query_at, expected_value in scenario.queries:
        rows = beliefs_as_of(
            db,
            query_at,
            entity=scenario.entity,
            predicate=scenario.predicate,
            user_id=scenario.user_id,
        )
        # Order-of-predicate is `observed_at DESC`, so `rows[0]` is the
        # most recent live row at `query_at` — the canonical answer.
        got = rows[0].value if rows else None

        if got == expected_value:
            n_correct += 1
        else:
            n_wrong += 1
            wrong.append(
                {
                    "query_at": query_at.isoformat(),
                    "expected": expected_value,
                    "got": got,
                    "n_rows_returned": len(rows),
                }
            )

    return TemporalQAResult(
        scenario_seed=seed,
        n_transitions=n_transitions,
        n_queries=len(scenario.queries),
        n_correct=n_correct,
        n_wrong=n_wrong,
        wrong_examples=wrong,
    )


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


@pytest.fixture
def seeded(db_session):
    _seed_memory_policies(db_session)
    db_session.commit()
    return db_session


@pytest.mark.parametrize(
    "seed,n_transitions",
    [
        (0, 1),  # minimal: a single transition
        (1, 3),  # typical: multi-hop
        (2, 5),  # stress: longer chain
        (3, 3),  # different RNG path
        (4, 2),
    ],
)
def test_temporal_qa_exit_gate(memory_on, seeded, seed, n_transitions):
    """Phase 12B exit gate: 100% correct belief-at-time-T on the
    synthetic set. Parametrized across 5 seeds to catch order-sensitivity
    bugs (e.g. "we got lucky because transitions happened to come in
    sorted order in seed 0")."""
    result = run_temporal_qa(seeded, seed=seed, n_transitions=n_transitions)
    assert result.accuracy == 1.0, (
        f"Temporal QA failed gate: accuracy={result.accuracy:.3f}, wrong={result.wrong_examples}"
    )


def test_temporal_qa_schema_stable():
    """The JSON report schema is a contract for docs/benchmarks.md and
    the nightly workflow PR comment. Guard against accidental renames."""
    result = TemporalQAResult(
        scenario_seed=0,
        n_transitions=1,
        n_queries=4,
        n_correct=4,
        n_wrong=0,
    )
    body = result.to_json()
    assert set(body.keys()) == {
        "benchmark",
        "scenario_seed",
        "n_transitions",
        "n_queries",
        "n_correct",
        "n_wrong",
        "accuracy",
        "passes_exit_gate",
        "wrong_examples",
    }
    assert body["benchmark"] == "temporal_qa"
    assert body["passes_exit_gate"] is True


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    # Route logging to stderr so --json consumers (nightly workflow,
    # docs/benchmarks.md generator) see a clean stdout.
    from tests.eval import reroute_logging_to_stderr

    reroute_logging_to_stderr()

    parser = argparse.ArgumentParser(description="Temporal QA benchmark runner")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--transitions", type=int, default=3)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the canonical JSON result to stdout",
    )
    args = parser.parse_args(argv)

    # Spin up a private in-memory SQLite so the benchmark never touches
    # a developer's real DB when invoked as a CLI.
    import app.models  # noqa: F401  (register ORM models)
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
            result = run_temporal_qa(db, seed=args.seed, n_transitions=args.transitions)
        finally:
            settings.MEMORY_ENABLED = False
    finally:
        db.close()

    body = result.to_json()
    if args.json:
        print(json.dumps(body, indent=2, default=str))
    else:
        print(
            f"temporal_qa  seed={args.seed} transitions={args.transitions}  "
            f"accuracy={body['accuracy']:.3f}  "
            f"({body['n_correct']}/{body['n_queries']})"
        )
        if body["wrong_examples"]:
            print("FAILURES:")
            for w in body["wrong_examples"]:
                print(f"  {w}")
    return 0 if result.accuracy == 1.0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(_main())
