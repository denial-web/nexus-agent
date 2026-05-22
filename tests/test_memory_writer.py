"""Integration tests for the governed belief writer (Phase 12A).

Exercises the full write path: skepticism gate → Covernor policy check →
hash chain → persisted `Belief` row. Uses the shared `db_session` fixture
and seeds the Phase 12 memory policies up front so each test gets a clean
default-deny namespace with a single scoped allow for preferences.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.core.memory.confidence import BetaConfidence
from app.core.memory.skepticism import BeliefDraft
from app.core.memory.writer import WriteOutcome, write_belief, write_beliefs
from app.main import _seed_memory_policies
from app.models.belief import Belief


@pytest.fixture
def memory_enabled(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)


@pytest.fixture
def seeded_session(db_session):
    _seed_memory_policies(db_session)
    return db_session


def _pref_draft(
    *,
    value: str = "short",
    alpha: float = 9.0,
    beta: float = 1.0,
    source: str = "user_stated",
    user: str = "alice",
) -> BeliefDraft:
    return BeliefDraft(
        entity=f"user:{user}",
        predicate="answer_length",
        value=value,
        entity_type="preference",
        confidence=BetaConfidence(alpha=alpha, beta=beta),
        source_type=source,
        user_id=user,
        session_id="s-1",
        agent_id="agent-1",
        rationale="user said so",
    )


class TestFeatureFlag:
    def test_returns_skipped_when_disabled(self, seeded_session):
        out = write_belief(_pref_draft(), seeded_session)
        assert out.status == "skipped_flag_off"
        assert seeded_session.query(Belief).count() == 0

    def test_accepts_when_enabled(self, memory_enabled, seeded_session):
        out = write_belief(_pref_draft(), seeded_session)
        assert out.status == "accepted", out.reason
        assert out.belief_id
        assert seeded_session.query(Belief).count() == 1


class TestSkepticism:
    def test_rejects_weak_high_stakes(self, memory_enabled, seeded_session):
        draft = BeliefDraft(
            entity="user:alice",
            predicate="ssn",
            value="123-45-6789",
            entity_type="identity",
            confidence=BetaConfidence(alpha=2.0, beta=3.0),
            source_type="inferred",
            user_id="alice",
        )
        out = write_belief(draft, seeded_session)
        assert out.status in ("rejected", "needs_evidence")
        assert seeded_session.query(Belief).count() == 0


class TestCovernorGate:
    def test_preference_allowed(self, memory_enabled, seeded_session):
        out = write_belief(_pref_draft(), seeded_session)
        assert out.status == "accepted"
        assert out.policy is not None
        assert out.policy.decision == "allow"

    def test_other_entity_type_denied_by_default(self, memory_enabled, seeded_session):
        draft = BeliefDraft(
            entity="user:alice",
            predicate="favorite_color",
            value="blue",
            entity_type="context",
            confidence=BetaConfidence(alpha=9.0, beta=1.0),
            source_type="user_stated",
            user_id="alice",
        )
        out = write_belief(draft, seeded_session)
        assert out.status == "denied_by_policy"
        assert out.policy is not None and out.policy.decision == "deny"
        assert seeded_session.query(Belief).count() == 0


class TestHashChain:
    def test_first_write_uses_genesis(self, memory_enabled, seeded_session):
        out = write_belief(_pref_draft(), seeded_session)
        belief = seeded_session.query(Belief).filter_by(id=out.belief_id).one()
        assert belief.prev_hash == "genesis"
        assert belief.belief_hash and len(belief.belief_hash) == 64

    def test_chain_links_sequentially(self, memory_enabled, seeded_session):
        # Same value on both writes → skepticism accepts ("agrees_with_existing_beliefs")
        # so both rows land and we can verify the chain pointer.
        first = write_belief(_pref_draft(value="short"), seeded_session)
        assert first.status == "accepted"
        seeded_session.flush()
        second = write_belief(
            _pref_draft(value="short", alpha=10.0, beta=1.0),
            seeded_session,
        )
        assert second.status == "accepted"
        b2 = seeded_session.query(Belief).filter_by(id=second.belief_id).one()
        assert b2.prev_hash == first.belief_hash

    def test_per_user_chains_are_independent(self, memory_enabled, seeded_session):
        a = write_belief(_pref_draft(user="alice"), seeded_session)
        seeded_session.flush()
        b = write_belief(_pref_draft(user="bob"), seeded_session)
        bob_row = seeded_session.query(Belief).filter_by(id=b.belief_id).one()
        assert bob_row.prev_hash == "genesis"
        assert bob_row.belief_hash != a.belief_hash


class TestSupersession:
    def test_contradicting_stronger_belief_supersedes(self, memory_enabled, seeded_session):
        # Weak prior (mean 0.5 @ user_stated → effective 0.5 = threshold for
        # preference). New belief must beat that by at least stakes/2 = 0.25.
        first = write_belief(_pref_draft(value="short", alpha=2.0, beta=2.0), seeded_session)
        assert first.status == "accepted"
        seeded_session.flush()

        second = write_belief(
            _pref_draft(value="long", alpha=30.0, beta=1.0),
            seeded_session,
        )
        assert second.status == "superseded", second.reason
        assert first.belief_id in second.superseded_ids

        old = seeded_session.query(Belief).filter_by(id=first.belief_id).one()
        assert old.superseded_at is not None

        new = seeded_session.query(Belief).filter_by(id=second.belief_id).one()
        assert first.belief_id in (new.contradicts or [])

    def test_weaker_conflict_is_rejected(self, memory_enabled, seeded_session):
        first = write_belief(_pref_draft(value="short", alpha=20.0, beta=1.0), seeded_session)
        assert first.status == "accepted"
        seeded_session.flush()

        second = write_belief(
            _pref_draft(value="long", alpha=3.0, beta=1.0),
            seeded_session,
        )
        assert second.status in ("rejected", "needs_evidence")
        old = seeded_session.query(Belief).filter_by(id=first.belief_id).one()
        assert old.superseded_at is None


class TestProvenance:
    def test_records_source_trace_and_extractor_version(self, memory_enabled, seeded_session):
        out = write_belief(
            _pref_draft(),
            seeded_session,
            source_trace_id="trace-abc",
            extractor_version="v1.0.0",
        )
        assert out.status == "accepted"
        row = seeded_session.query(Belief).filter_by(id=out.belief_id).one()
        assert row.source_trace_id == "trace-abc"
        assert row.extractor_version == "v1.0.0"
        assert row.rationale == "user said so"


class TestBatch:
    def test_write_beliefs_returns_outcome_per_draft(self, memory_enabled, seeded_session):
        drafts = [
            _pref_draft(value="a", alpha=9.0, beta=1.0),
            _pref_draft(value="a", alpha=2.0, beta=2.0),  # will need_evidence vs identical prior
        ]
        outs = write_beliefs(drafts, seeded_session)
        assert len(outs) == 2
        assert outs[0].status == "accepted"
        # Second draft is weaker and same value → skepticism keeps prior.
        assert outs[1].status in ("needs_evidence", "rejected", "accepted")


class TestWriteOutcomeShape:
    def test_outcome_dataclass_exposes_diagnostics(self, memory_enabled, seeded_session):
        out = write_belief(_pref_draft(), seeded_session)
        assert isinstance(out, WriteOutcome)
        assert out.status == "accepted"
        assert out.skepticism is not None
        assert out.policy is not None
        assert out.reason
