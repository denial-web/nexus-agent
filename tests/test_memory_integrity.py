"""
Tests for the belief hash-chain integrity service (Phase 12 Week 4).

Two layers:

* Unit tests against `app.core.memory.integrity.verify_chain` +
  `compute_belief_hash` — happy path, tamper detection on every
  hashed field, per-user scoping, `as_of` bitemporal restriction,
  NULL-user chain, multi-user audit mode, tz-aware guard.
* HTTP tests against `GET /v1/memory/integrity` — feature-flag 503,
  Covernor gating via the seeded `memory-allow-integrity-read`
  policy, query-param semantics for scope_all / user_id / as_of,
  broken chain surfaces as a structured 200 response.

The tamper tests mutate already-committed rows and re-run the verifier
against the mutated DB; this is the only way to prove the production
verifier matches the writer byte-for-byte, because a mistake in the
hash payload composition would pass a round-trip-only test but fail
once a real auditor tries to reproduce a stored hash.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings
from app.core.memory.confidence import from_mean_and_strength
from app.core.memory.integrity import (
    _HASH_GENESIS,
    compute_belief_hash,
    verify_chain,
)
from app.core.memory.skepticism import BeliefDraft
from app.core.memory.writer import write_belief
from app.main import _seed_memory_policies
from app.models.belief import Belief
from app.models.episode import Episode
from app.models.policy import Policy
from app.models.step_trace import StepTrace
from app.models.trace import Trace


@pytest.fixture(autouse=True)
def _purge_memory_rows(db_session):
    yield
    db_session.rollback()
    db_session.query(StepTrace).delete()
    db_session.query(Episode).delete()
    db_session.query(Trace).delete()
    db_session.query(Belief).delete()
    # Policies are seeded on demand; wipe between tests so each case
    # controls its own governance state (including the case that tests
    # what happens when the allow-integrity-read policy is absent).
    db_session.query(Policy).filter(Policy.name.like("memory-%")).delete()
    db_session.commit()


@pytest.fixture
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)


def _seed(db) -> None:
    _seed_memory_policies(db)
    db.commit()


def _write_pref(
    db,
    *,
    user: str | None,
    value: str,
    predicate: str = "prefers",
    mean: float = 0.9,
    strength: float = 20.0,
    observed_at: datetime | None = None,
) -> str:
    """Write one accepted preference belief; return its id.

    Uses the real writer so the hash chain is built the same way a
    production run would build it. Any test that fabricates rows
    directly would prove nothing about the verifier. `observed_at`
    is forwarded as-is so as_of-style tests can plant rows at
    specific belief-times without relying on sleep().
    """
    draft = BeliefDraft(
        entity=f"user:{user or 'anon'}",
        predicate=predicate,
        value=value,
        entity_type="preference",
        confidence=from_mean_and_strength(mean, strength),
        source_type="user_stated",
        user_id=user,
        session_id="integrity-test",
        keywords=[predicate, value],
    )
    out = write_belief(draft, db, observed_at=observed_at)
    assert out.status in {"accepted", "superseded"}, out.reason
    db.commit()
    return out.belief_id


# ────────────────────────────────────────────────────────────────────────
# Unit tests — core verifier
# ────────────────────────────────────────────────────────────────────────


class TestVerifyChain:
    def test_raises_when_memory_disabled(self, db_session):
        """The verifier must be loud when MEMORY_ENABLED is false —
        the beliefs table may not exist on such deployments and a
        silent `verified=True` would be dangerously misleading."""
        with pytest.raises(RuntimeError, match="MEMORY_ENABLED=false"):
            verify_chain(db_session, user_id="whoever")

    def test_empty_chain_verifies_clean(self, db_session, memory_on):
        """No rows → nothing to disprove. Counts as verified with
        rows_checked=0 (not as 'skipped') so audit dashboards can
        distinguish empty from broken."""
        _seed(db_session)
        result = verify_chain(db_session, user_id="nobody")
        assert result.verified is True
        assert result.rows_checked == 0
        assert result.first_break_at is None

    def test_single_row_chain(self, db_session, memory_on):
        """First row must link to 'genesis' and reproduce its own hash."""
        _seed(db_session)
        _write_pref(db_session, user="u1", value="dark_mode")

        result = verify_chain(db_session, user_id="u1")
        assert result.verified is True
        assert result.rows_checked == 1
        row = db_session.query(Belief).filter(Belief.user_id == "u1").one()
        assert row.prev_hash == _HASH_GENESIS
        assert row.belief_hash == compute_belief_hash(row)

    def test_multi_row_chain(self, db_session, memory_on):
        """Second row's prev_hash must be the first row's belief_hash."""
        _seed(db_session)
        _write_pref(db_session, user="u2", predicate="p1", value="a")
        _write_pref(db_session, user="u2", predicate="p2", value="b")
        _write_pref(db_session, user="u2", predicate="p3", value="c")

        result = verify_chain(db_session, user_id="u2")
        assert result.verified is True
        assert result.rows_checked == 3

    def test_per_user_chains_are_independent(self, db_session, memory_on):
        """Alice's chain must not see Bob's rows — the writer chains
        per-user for tenant isolation. Breaking Bob's chain must not
        fail Alice's verification."""
        _seed(db_session)
        _write_pref(db_session, user="alice", value="a1")
        _write_pref(db_session, user="bob", value="b1")
        _write_pref(db_session, user="alice", predicate="p2", value="a2")

        # Tamper Bob's only row.
        bob = db_session.query(Belief).filter(Belief.user_id == "bob").one()
        bob.value = "tampered"
        db_session.commit()

        alice_result = verify_chain(db_session, user_id="alice")
        bob_result = verify_chain(db_session, user_id="bob")
        assert alice_result.verified is True, "Bob's break leaked into Alice's scope"
        assert bob_result.verified is False

    def test_null_user_chain_distinct_from_default_sentinel(self, db_session, memory_on):
        """`user_id=None` (explicit) verifies only the NULL-user chain.
        This is distinct from the default `user_id=...` sentinel which
        walks every chain — conflating the two would either skip the
        NULL chain entirely (bug) or refuse to let callers target it."""
        _seed(db_session)
        _write_pref(db_session, user=None, value="shared-1")
        _write_pref(db_session, user="alice", value="alice-1")

        null_only = verify_chain(db_session, user_id=None)
        assert null_only.verified is True
        assert null_only.rows_checked == 1
        assert null_only.scope_user_ids == [None]

        all_chains = verify_chain(db_session)  # default sentinel
        assert all_chains.verified is True
        assert all_chains.rows_checked == 2
        assert set(all_chains.scope_user_ids) == {None, "alice"}

    def test_audit_mode_walks_every_chain(self, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="u_a", value="va")
        _write_pref(db_session, user="u_b", value="vb")
        _write_pref(db_session, user="u_c", value="vc")

        result = verify_chain(db_session)
        assert result.verified is True
        assert result.rows_checked == 3
        assert set(result.scope_user_ids) == {"u_a", "u_b", "u_c"}

    def test_audit_mode_reports_first_break(self, db_session, memory_on):
        """The verifier stops at the first break and identifies it by
        id. Don't try to 'continue past' a tampered row — one break
        invalidates the tamper-evident claim for everything that
        follows in the same chain."""
        _seed(db_session)
        _write_pref(db_session, user="broken", predicate="p1", value="ok")
        bad_id = _write_pref(db_session, user="broken", predicate="p2", value="ok")
        _write_pref(db_session, user="broken", predicate="p3", value="ok")

        target = db_session.query(Belief).filter(Belief.id == bad_id).one()
        target.value = "tampered_after_write"
        db_session.commit()

        result = verify_chain(db_session, user_id="broken")
        assert result.verified is False
        assert result.first_break_at == bad_id
        assert "belief_hash mismatch" in (result.reason or "")


class TestTamperDetection:
    """Every hashed field must fail verification when mutated.

    If a field is in the hash payload but a tamper test passes, we've
    accidentally excluded it (or included a derived form) and a real
    attacker could swap values undetected. Run one test per field that
    the writer includes.
    """

    def _setup(self, db_session):
        _seed(db_session)
        bid = _write_pref(db_session, user="tamper", value="original")
        row = db_session.query(Belief).filter(Belief.id == bid).one()
        return bid, row

    def test_value_mutation_detected(self, db_session, memory_on):
        bid, row = self._setup(db_session)
        row.value = "swapped"
        db_session.commit()
        result = verify_chain(db_session, user_id="tamper")
        assert result.verified is False
        assert result.first_break_at == bid

    def test_predicate_mutation_detected(self, db_session, memory_on):
        _, row = self._setup(db_session)
        row.predicate = "not_what_was_hashed"
        db_session.commit()
        assert verify_chain(db_session, user_id="tamper").verified is False

    def test_entity_mutation_detected(self, db_session, memory_on):
        _, row = self._setup(db_session)
        row.entity = "user:elsewhere"
        db_session.commit()
        assert verify_chain(db_session, user_id="tamper").verified is False

    def test_observed_at_mutation_detected(self, db_session, memory_on):
        _, row = self._setup(db_session)
        row.observed_at = row.observed_at + timedelta(days=1)
        db_session.commit()
        assert verify_chain(db_session, user_id="tamper").verified is False

    def test_source_type_mutation_detected(self, db_session, memory_on):
        _, row = self._setup(db_session)
        row.source_type = "imported"
        db_session.commit()
        assert verify_chain(db_session, user_id="tamper").verified is False

    def test_prev_hash_mutation_detected(self, db_session, memory_on):
        """A tampered prev_hash manifests as either a prev_hash mismatch
        (because we expect the prior row's belief_hash, or 'genesis')
        or as a belief_hash mismatch (because prev_hash is part of the
        self-hash payload). Either failure mode is acceptable — the
        important invariant is that the verifier catches it."""
        _, row = self._setup(db_session)
        row.prev_hash = "a" * 64  # wrong but plausibly shaped
        db_session.commit()
        result = verify_chain(db_session, user_id="tamper")
        assert result.verified is False

    def test_belief_hash_mutation_detected(self, db_session, memory_on):
        _, row = self._setup(db_session)
        row.belief_hash = "0" * 64
        db_session.commit()
        result = verify_chain(db_session, user_id="tamper")
        assert result.verified is False


class TestAsOf:
    def test_as_of_requires_tzaware(self, db_session, memory_on):
        """Match `beliefs_as_of()` semantics — naive is a programmer
        error, not a silent UTC coerce."""
        _seed(db_session)
        with pytest.raises(ValueError, match="timezone-aware"):
            verify_chain(
                db_session,
                user_id="x",
                as_of=datetime(2026, 1, 1),  # naive
            )

    def test_as_of_excludes_later_rows(self, db_session, memory_on):
        """A row written after `as_of` must not contribute — otherwise
        a later write could retroactively break a previously-verified
        historical window. Verify by writing two rows at explicit
        belief-times (via the writer's `observed_at` override) so the
        test is independent of wall-clock resolution, then tamper the
        later one and pin `as_of` to between them."""
        _seed(db_session)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t1 = datetime(2026, 1, 2, tzinfo=UTC)
        t2 = datetime(2026, 1, 3, tzinfo=UTC)

        _write_pref(
            db_session,
            user="asof",
            predicate="p1",
            value="a",
            observed_at=t0,
        )
        _write_pref(
            db_session,
            user="asof",
            predicate="p2",
            value="b",
            observed_at=t2,
        )

        later = db_session.query(Belief).filter(Belief.user_id == "asof", Belief.predicate == "p2").one()
        later.value = "tampered"
        db_session.commit()

        scoped = verify_chain(db_session, user_id="asof", as_of=t1)
        assert scoped.verified is True, "as_of should have excluded the tampered later row"
        assert scoped.rows_checked == 1

        full = verify_chain(db_session, user_id="asof")
        assert full.verified is False


class TestComputeBeliefHash:
    def test_matches_stored_hash_for_fresh_rows(self, db_session, memory_on):
        """Round-trip check: a freshly written row's stored hash must
        equal `compute_belief_hash(row)`. If this fails, the verifier
        and the writer have drifted and every tamper test is a false
        positive."""
        _seed(db_session)
        _write_pref(db_session, user="hash-check", value="v1")
        row = db_session.query(Belief).filter(Belief.user_id == "hash-check").one()
        assert compute_belief_hash(row) == row.belief_hash

    def test_sqlite_naive_observed_at_still_reproduces(self, db_session, memory_on):
        """SQLite strips tzinfo on round-trip. The verifier must
        re-attach UTC so `observed_at.isoformat()` reproduces the
        `+00:00`-suffixed string the writer hashed. If we forgot to
        do that, every SQLite-stored row would spuriously fail."""
        _seed(db_session)
        _write_pref(db_session, user="tz-check", value="v")
        row = db_session.query(Belief).filter(Belief.user_id == "tz-check").one()
        # On SQLite the column returns naive; on Postgres tz-aware.
        # Either way the verifier must produce the same hash.
        recomputed = compute_belief_hash(row)
        assert recomputed == row.belief_hash


# ────────────────────────────────────────────────────────────────────────
# HTTP tests — GET /v1/memory/integrity
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityEndpointFeatureFlag:
    def test_returns_503_when_disabled(self, client):
        r = client.get("/v1/memory/integrity")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "memory_disabled"


class TestIntegrityEndpointGovernance:
    def test_403_when_no_matching_policy(self, client, db_session, memory_on):
        """`memory:read:integrity` is default-denied by the policy
        engine when no matching policy exists. Without the
        `memory-allow-integrity-read` allow rule the endpoint must
        reject with a structured 403, not fall through to allow.

        The app lifespan re-seeds memory policies every time the
        `client` fixture is created, so we need to explicitly delete
        the integrity-read allow rule after the app is up but before
        the test request fires. We also seed the allow-preference-write
        policy manually so `_write_pref` still succeeds — without it
        we'd never have a belief row to audit in the first place.
        """
        # client fixture triggered lifespan → memory policies are seeded.
        # Remove the read-allow rule to exercise default-deny; keep the
        # write-allow rule so we can still plant a belief.
        db_session.query(Policy).filter(Policy.name == "memory-allow-integrity-read").delete()
        db_session.commit()

        _write_pref(db_session, user="no-policy", value="v")

        r = client.get("/v1/memory/integrity", params={"user_id": "no-policy"})
        assert r.status_code == 403
        body = r.json()
        assert body["error"]["code"] == "governance_denied"
        assert "integrity" in body["error"]["message"].lower()

    def test_200_when_seeded_policy_present(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="with-policy", value="v")
        r = client.get("/v1/memory/integrity", params={"user_id": "with-policy"})
        assert r.status_code == 200
        body = r.json()
        assert body["verified"] is True
        assert body["rows_checked"] == 1


class TestIntegrityEndpointScope:
    def test_user_id_scope(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="alice", value="a")
        _write_pref(db_session, user="bob", value="b")

        r = client.get("/v1/memory/integrity", params={"user_id": "alice"})
        body = r.json()
        assert body["rows_checked"] == 1
        assert body["scope_user_ids"] == ["alice"]
        assert body["checked_user_count"] == 1

    def test_default_scope_walks_all_chains(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="u1", value="v1")
        _write_pref(db_session, user="u2", value="v2")
        _write_pref(db_session, user=None, value="shared")

        r = client.get("/v1/memory/integrity")
        body = r.json()
        assert body["verified"] is True
        assert body["rows_checked"] == 3
        assert set(body["scope_user_ids"]) == {"u1", "u2", None}

    def test_scope_all_false_targets_null_user_only(self, client, db_session, memory_on):
        """`scope_all=false` with no `user_id` = audit the NULL-user
        chain. Distinct from the default so operators can target it."""
        _seed(db_session)
        _write_pref(db_session, user="u1", value="v1")
        _write_pref(db_session, user=None, value="shared")

        r = client.get("/v1/memory/integrity", params={"scope_all": "false"})
        body = r.json()
        assert body["rows_checked"] == 1
        assert body["scope_user_ids"] == [None]


class TestIntegrityEndpointBrokenChain:
    def test_broken_chain_returns_200_with_structured_result(self, client, db_session, memory_on):
        """A broken chain is an audit FINDING, not an HTTP error.
        Dashboards need 200 + structured body to render the break;
        5xx would look like the endpoint itself was down."""
        _seed(db_session)
        bid = _write_pref(db_session, user="bad-chain", value="ok")
        row = db_session.query(Belief).filter(Belief.id == bid).one()
        row.value = "tampered"
        db_session.commit()

        r = client.get("/v1/memory/integrity", params={"user_id": "bad-chain"})
        assert r.status_code == 200
        body = r.json()
        assert body["verified"] is False
        assert body["first_break_at"] == bid
        assert "mismatch" in (body["reason"] or "")


class TestIntegrityEndpointAsOf:
    def test_naive_as_of_rejected_400(self, client, db_session, memory_on):
        _seed(db_session)
        r = client.get(
            "/v1/memory/integrity",
            params={"user_id": "x", "as_of": "2026-01-01T00:00:00"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_timestamp"

    def test_tzaware_as_of_accepted(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="asof-http", value="v")
        r = client.get(
            "/v1/memory/integrity",
            params={
                "user_id": "asof-http",
                "as_of": datetime.now(UTC).isoformat(),
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["verified"] is True
        assert body["as_of"] is not None
