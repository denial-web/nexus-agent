"""Unit tests for the forgetting layer (decay + tombstoning).

See `app/core/memory/forgetting.py` for design notes. Key invariants
under test:

- Flag gating — every public function is a strict no-op when
  `MEMORY_ENABLED=False`.
- Decay is pure: `decay_belief` never mutates the row.
- Scaling α and β by the same ratio preserves the Beta mean, so
  tombstoning uses `strength` (which folds in variance) rather than
  `mean`.
- `run_forget_sweep` is idempotent at a frozen `now`: pass two
  tombstones nothing new because pass one already filtered the victims.
- `forget_by_entity` tombstones user-scoped rows AND genuinely global
  rows (user_id IS NULL), but never other tenants' data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.memory.forgetting import (
    _FALLBACK_HALFLIFE,
    DecayOutcome,
    ForgetSweepOutcome,
    _decay_ratio,
    _effective_halflife,
    _parse_duration,
    decay_belief,
    effective_sample_size,
    forget_by_entity,
    parse_decay_profile,
    run_forget_sweep,
)
from app.models.belief import Belief


@pytest.fixture(autouse=True)
def _purge_beliefs_table(db_session):
    """The shared session-scoped engine preserves committed rows across
    tests. Memory tests that exercise persistence must leave the
    `beliefs` table empty when they're done, or the regression
    tripwire (which asserts zero belief rows with MEMORY_ENABLED=False)
    will flake depending on test ordering.
    """
    yield
    db_session.rollback()
    db_session.query(Belief).delete()
    db_session.commit()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_days_hours_minutes(self) -> None:
        assert _parse_duration("180d") == timedelta(days=180)
        assert _parse_duration("4h") == timedelta(hours=4)
        assert _parse_duration("30m") == timedelta(minutes=30)

    def test_fractional(self) -> None:
        assert _parse_duration("1.5h") == timedelta(hours=1.5)

    def test_case_and_whitespace(self) -> None:
        assert _parse_duration("  7D ") == timedelta(days=7)

    def test_inf_variants(self) -> None:
        for v in ("inf", "INF", "infinite", "never", "", "   "):
            assert _parse_duration(v) is None

    def test_malformed_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("soon")
        with pytest.raises(ValueError):
            _parse_duration("0d")
        with pytest.raises(ValueError):
            _parse_duration("-5h")


class TestParseDecayProfile:
    def test_round_trip(self) -> None:
        profile = parse_decay_profile("identity=inf,preference=180d,state=4h,context=1h")
        assert profile["identity"] is None
        assert profile["preference"] == timedelta(days=180)
        assert profile["state"] == timedelta(hours=4)
        assert profile["context"] == timedelta(hours=1)

    def test_empty(self) -> None:
        assert parse_decay_profile("") == {}
        assert parse_decay_profile("   ") == {}

    def test_bad_entry_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_decay_profile("foo")
        with pytest.raises(ValueError):
            parse_decay_profile("foo=bar")


class TestEffectiveHalflife:
    def test_lookup_hit(self) -> None:
        p = {"preference": timedelta(days=7), "state": None}
        assert _effective_halflife("preference", p) == timedelta(days=7)
        assert _effective_halflife("state", p) is None

    def test_fallback(self) -> None:
        assert _effective_halflife("unknown", {}) == _FALLBACK_HALFLIFE


# ---------------------------------------------------------------------------
# Decay math
# ---------------------------------------------------------------------------


class TestDecayRatio:
    def test_zero_age_is_one(self) -> None:
        assert _decay_ratio(timedelta(0), timedelta(days=1)) == 1.0

    def test_one_halflife_halves(self) -> None:
        r = _decay_ratio(timedelta(hours=24), timedelta(hours=24))
        assert r == pytest.approx(0.5, abs=1e-9)

    def test_two_halflives_quarters(self) -> None:
        r = _decay_ratio(timedelta(hours=48), timedelta(hours=24))
        assert r == pytest.approx(0.25, abs=1e-9)

    def test_negative_age_does_not_amplify(self) -> None:
        # clock skew — future observed_at must not produce ratio > 1
        r = _decay_ratio(timedelta(hours=-5), timedelta(hours=1))
        assert r == 1.0

    def test_zero_halflife_returns_one(self) -> None:
        # pathological input shouldn't crash
        assert _decay_ratio(timedelta(hours=1), timedelta(0)) == 1.0


# ---------------------------------------------------------------------------
# decay_belief — pure, no mutation
# ---------------------------------------------------------------------------


def _belief(
    *,
    alpha: float = 18.0,
    beta: float = 2.0,
    entity_type: str = "preference",
    observed_at: datetime | None = None,
    user_id: str | None = "alice",
    superseded_at: datetime | None = None,
) -> Belief:
    """Construct an in-memory Belief without touching the DB."""
    return Belief(
        id=uuid.uuid4().hex,
        entity=f"user:{user_id or 'global'}",
        predicate="prefers",
        value="dark_mode",
        entity_type=entity_type,
        observed_at=observed_at or datetime.now(UTC),
        superseded_at=superseded_at,
        confidence_alpha=alpha,
        confidence_beta=beta,
        source_type="user_stated",
        user_id=user_id,
    )


class TestDecayBelief:
    def test_noop_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", False)
        b = _belief()
        out = decay_belief(b, now=datetime.now(UTC))
        assert isinstance(out, DecayOutcome)
        assert out.skipped is True
        assert out.before == out.after
        # no mutation
        assert b.confidence_alpha == 18.0
        assert b.confidence_beta == 2.0

    def test_infinite_halflife_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        b = _belief(entity_type="identity")
        profile = {"identity": None}
        out = decay_belief(b, now=datetime.now(UTC), profile=profile)
        assert out.skipped is True
        assert out.ratio == 1.0
        assert out.after == out.before

    def test_does_not_mutate_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        origin = datetime(2025, 1, 1, tzinfo=UTC)
        b = _belief(observed_at=origin)
        now = origin + timedelta(days=365)
        profile = {"preference": timedelta(days=180)}
        out = decay_belief(b, now=now, profile=profile)
        # Row is untouched:
        assert b.confidence_alpha == 18.0
        assert b.confidence_beta == 2.0
        # Effective values are decayed:
        assert out.after.alpha < out.before.alpha
        assert out.after.beta < out.before.beta

    def test_decay_preserves_mean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scaling α and β by the same ratio leaves α/(α+β) invariant.

        This is exactly WHY the tombstone floor uses strength rather
        than mean — capturing this property in a test means a future
        refactor that breaks it can't silently change the semantics.
        """
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        origin = datetime(2025, 1, 1, tzinfo=UTC)
        b = _belief(observed_at=origin)
        now = origin + timedelta(days=365)
        profile = {"preference": timedelta(days=90)}
        out = decay_belief(b, now=now, profile=profile)
        assert out.before.mean == pytest.approx(out.after.mean, abs=1e-9)

    def test_effective_sample_size_decays_with_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        origin = datetime(2025, 1, 1, tzinfo=UTC)
        b = _belief(observed_at=origin)
        profile = {"preference": timedelta(days=30)}
        # Further out in time should yield strictly lower effective
        # (α+β) — this is the metric we use for tombstoning.
        ss_fresh = effective_sample_size(b, now=origin + timedelta(days=1), profile=profile)
        ss_old = effective_sample_size(b, now=origin + timedelta(days=365), profile=profile)
        assert ss_fresh > ss_old

    def test_naive_observed_at_treated_as_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        # SQLite can hand back naive datetimes — must not raise
        naive = datetime(2025, 1, 1)
        b = _belief(observed_at=naive)
        now = datetime(2025, 2, 1, tzinfo=UTC)
        profile = {"preference": timedelta(days=30)}
        out = decay_belief(b, now=now, profile=profile)
        assert out.skipped is False
        assert 0.0 < out.ratio <= 1.0


class TestEffectiveSampleSize:
    def test_matches_decay_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        b = _belief()
        now = datetime.now(UTC)
        profile = {"preference": timedelta(days=30)}
        outcome = decay_belief(b, now=now, profile=profile)
        expected = outcome.after.alpha + outcome.after.beta
        actual = effective_sample_size(b, now=now, profile=profile)
        assert expected == pytest.approx(actual, abs=1e-12)

    def test_infinite_halflife_returns_raw_total(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        b = _belief(alpha=10.0, beta=2.0, entity_type="identity")
        profile = {"identity": None}
        ss = effective_sample_size(b, now=datetime.now(UTC), profile=profile)
        assert ss == pytest.approx(12.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Sweep — persistent tombstoning
# ---------------------------------------------------------------------------


class TestRunForgetSweep:
    def test_noop_when_flag_off(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", False)
        out = run_forget_sweep(db_session)
        assert isinstance(out, ForgetSweepOutcome)
        assert out.scanned == 0
        assert out.tombstoned == 0

    def test_sweep_tombstones_aged_weak_rows(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        monkeypatch.setattr(
            "app.config.settings.MEMORY_DECAY_PROFILE",
            "preference=1h,identity=inf",
        )

        uid = f"sweep-aged-{uuid.uuid4().hex[:6]}"
        origin = datetime(2025, 1, 1, tzinfo=UTC)
        old = _belief(
            alpha=2.0,
            beta=1.0,
            entity_type="preference",
            observed_at=origin,
            user_id=uid,
        )
        fresh_now = datetime.now(UTC)
        fresh = _belief(
            alpha=18.0,
            beta=2.0,
            entity_type="preference",
            observed_at=fresh_now - timedelta(minutes=5),
            user_id=uid,
        )
        db_session.add_all([old, fresh])
        db_session.flush()

        out = run_forget_sweep(
            db_session,
            now=fresh_now,
            sample_size_floor=1.0,
        )
        db_session.flush()

        assert out.scanned >= 2
        # The aged row should be tombstoned, the fresh one should not.
        refreshed_old = db_session.query(Belief).filter_by(id=old.id).one()
        refreshed_fresh = db_session.query(Belief).filter_by(id=fresh.id).one()
        assert refreshed_old.superseded_at is not None
        assert refreshed_fresh.superseded_at is None

    def test_infinite_halflife_never_tombstoned(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        monkeypatch.setattr("app.config.settings.MEMORY_DECAY_PROFILE", "identity=inf")
        uid = f"sweep-inf-{uuid.uuid4().hex[:6]}"
        b = _belief(
            alpha=1.1,
            beta=20.0,  # strength is already very low, but identity == immortal
            entity_type="identity",
            observed_at=datetime(2020, 1, 1, tzinfo=UTC),
            user_id=uid,
        )
        db_session.add(b)
        db_session.flush()

        out = run_forget_sweep(db_session, now=datetime.now(UTC))
        db_session.flush()

        assert out.skipped_infinite >= 1
        refreshed = db_session.query(Belief).filter_by(id=b.id).one()
        assert refreshed.superseded_at is None

    def test_dry_run_does_not_persist(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        monkeypatch.setattr("app.config.settings.MEMORY_DECAY_PROFILE", "preference=1h")
        uid = f"sweep-dry-{uuid.uuid4().hex[:6]}"
        b = _belief(
            alpha=2.0,
            beta=1.0,
            entity_type="preference",
            observed_at=datetime(2020, 1, 1, tzinfo=UTC),
            user_id=uid,
        )
        db_session.add(b)
        db_session.flush()

        out = run_forget_sweep(db_session, now=datetime.now(UTC), dry_run=True)
        db_session.flush()

        assert out.dry_run is True
        assert out.tombstoned >= 1  # would have tombstoned
        refreshed = db_session.query(Belief).filter_by(id=b.id).one()
        assert refreshed.superseded_at is None  # but didn't

    def test_idempotent_at_frozen_now(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pass two at the same `now` tombstones nothing new."""
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        monkeypatch.setattr("app.config.settings.MEMORY_DECAY_PROFILE", "preference=1h")
        uid = f"sweep-idem-{uuid.uuid4().hex[:6]}"
        b = _belief(
            alpha=2.0,
            beta=1.0,
            entity_type="preference",
            observed_at=datetime(2020, 1, 1, tzinfo=UTC),
            user_id=uid,
        )
        db_session.add(b)
        db_session.flush()

        frozen_now = datetime.now(UTC)
        out1 = run_forget_sweep(db_session, now=frozen_now)
        db_session.flush()
        out2 = run_forget_sweep(db_session, now=frozen_now)
        db_session.flush()

        assert out1.tombstoned >= 1
        assert out2.tombstoned == 0  # nothing new to tombstone

    def test_bad_sample_size_floor_rejected(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        with pytest.raises(ValueError):
            run_forget_sweep(db_session, sample_size_floor=-0.1)


# ---------------------------------------------------------------------------
# User-directed forget
# ---------------------------------------------------------------------------


class TestForgetByEntity:
    def test_noop_when_flag_off(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", False)
        assert forget_by_entity(db_session, entity="user:x") == 0

    def test_requires_entity(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        with pytest.raises(ValueError):
            forget_by_entity(db_session, entity="")

    def test_tombstones_matching_rows(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        uid = f"forget-entity-{uuid.uuid4().hex[:6]}"
        entity = f"user:{uid}"
        b1 = _belief(user_id=uid)
        b1.entity = entity
        b2 = _belief(user_id=uid)
        b2.entity = entity
        b2.predicate = "dislikes"
        db_session.add_all([b1, b2])
        db_session.flush()

        n = forget_by_entity(db_session, entity=entity, user_id=uid, predicate="prefers")
        db_session.flush()
        assert n == 1

        r1 = db_session.query(Belief).filter_by(id=b1.id).one()
        r2 = db_session.query(Belief).filter_by(id=b2.id).one()
        assert r1.superseded_at is not None
        assert r2.superseded_at is None

    def test_user_scope_isolation(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tenant A cannot forget tenant B's beliefs."""
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        suffix = uuid.uuid4().hex[:6]
        alice = f"forget-iso-alice-{suffix}"
        bob = f"forget-iso-bob-{suffix}"
        entity = f"user:shared-{suffix}"

        a = _belief(user_id=alice)
        a.entity = entity
        b = _belief(user_id=bob)
        b.entity = entity
        db_session.add_all([a, b])
        db_session.flush()

        # Alice asks to forget her belief scoped to this entity:
        n = forget_by_entity(db_session, entity=entity, user_id=alice)
        db_session.flush()
        assert n == 1

        r_a = db_session.query(Belief).filter_by(id=a.id).one()
        r_b = db_session.query(Belief).filter_by(id=b.id).one()
        assert r_a.superseded_at is not None
        assert r_b.superseded_at is None

    def test_forgets_globals_under_user_scope(self, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
        """user_id IS NULL rows count as 'everyone's' and are fair game
        when a user asks to forget — they see them in retrieval."""
        monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
        suffix = uuid.uuid4().hex[:6]
        entity = f"global-entity-{suffix}"
        alice = f"forget-global-{suffix}"

        global_row = _belief(user_id=None)
        global_row.entity = entity
        db_session.add(global_row)
        db_session.flush()

        n = forget_by_entity(db_session, entity=entity, user_id=alice)
        db_session.flush()
        assert n == 1
        r = db_session.query(Belief).filter_by(id=global_row.id).one()
        assert r.superseded_at is not None
