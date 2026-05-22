"""
Principled forgetting — decay (read-time) + tombstoning (sweep-time).

Two independent operations:

1. **Decay (pure, read-time).** `decay_belief(b, now=...)` returns the
   effective `BetaConfidence` a belief has AT `now`, given its original
   `observed_at` and its entity-type half-life. Decay never mutates the
   row: `confidence_alpha` / `confidence_beta` remain the canonical
   "observed strength" written by the extractor/writer. This is what
   gives us idempotency — compounding re-decays on repeated sweeps is a
   foot-gun we avoid by construction.

2. **Tombstoning (sweep-time, persistent).** `run_forget_sweep(db, ...)`
   walks all live beliefs, computes their effective decayed confidence,
   and sets `superseded_at = now` on rows whose mean falls below a
   per-call floor. Tombstoning preserves the row (causal graph stays
   intact) and is the single persistent side-effect.

Design invariants:

- Gated by `settings.MEMORY_ENABLED`. When off, every public function
  is a no-op returning a zero-touch outcome. The regression tripwire
  depends on this.
- Decay ratios are bounded in (0, 1] — never amplifying. Clock skew
  (future `observed_at`) returns 1.0 instead of >1.0.
- Running `run_forget_sweep` with a frozen `now` twice in a row only
  tombstones the same rows on pass one; pass two tombstones nothing
  new because pass one already filtered them out via `superseded_at`.
- `MEMORY_DECAY_PROFILE` is parsed once per call (not cached globally)
  so operators can hot-reload decay rules by updating the env var and
  restarting the scheduler without code changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.memory.confidence import BetaConfidence
from app.models.belief import Belief

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decay profile parsing
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([dhm])\s*$", re.IGNORECASE)


def _parse_duration(spec: str) -> timedelta | None:
    """Parse '180d' / '4h' / '30m' / 'inf' → timedelta or None (infinite).

    Raises ValueError on malformed input so operators get a fast signal
    on a bad `MEMORY_DECAY_PROFILE` env var.
    """
    s = (spec or "").strip().lower()
    if s in ("inf", "infinite", "never", ""):
        return None
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f"Invalid duration spec: {spec!r} (expected e.g. '180d', '4h', '30m', or 'inf')")
    value, unit = float(m.group(1)), m.group(2)
    if value <= 0:
        raise ValueError(f"Duration must be positive: {spec!r}")
    if unit == "d":
        return timedelta(days=value)
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(minutes=value)


def parse_decay_profile(profile: str) -> dict[str, timedelta | None]:
    """Parse `MEMORY_DECAY_PROFILE` into `{entity_type: half_life_or_None}`.

    Format: `"identity=inf,preference=180d,state=4h,context=1h"`.
    Missing entity types fall back to `_FALLBACK_HALFLIFE` at lookup.
    """
    out: dict[str, timedelta | None] = {}
    for chunk in (profile or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Malformed decay profile entry: {chunk!r}")
        k, v = chunk.split("=", 1)
        out[k.strip().lower()] = _parse_duration(v)
    return out


#: Fallback half-life for entity_types not mentioned in the profile.
#: 24h keeps "unknown shape of belief" from living forever by default.
_FALLBACK_HALFLIFE = timedelta(hours=24)


def _effective_halflife(entity_type: str, profile: dict[str, timedelta | None]) -> timedelta | None:
    """Resolve an entity type's half-life. `None` means never decay."""
    key = (entity_type or "").strip().lower()
    if key in profile:
        return profile[key]
    return _FALLBACK_HALFLIFE


# ---------------------------------------------------------------------------
# Core decay math — pure, no DB, no mutation
# ---------------------------------------------------------------------------


def _decay_ratio(age: timedelta, half_life: timedelta) -> float:
    """Exponential-decay scale factor in (0, 1]: `2 ** (-age / half_life)`.

    Negative age (future `observed_at` from clock skew) returns 1.0 so
    we never amplify. A zero-or-negative `half_life` also returns 1.0
    so operators who disable decay don't trip assertions elsewhere.
    """
    if half_life <= timedelta(0):
        return 1.0
    seconds = max(0.0, age.total_seconds())
    half_seconds = half_life.total_seconds()
    if half_seconds <= 0:
        return 1.0
    return float(2.0 ** (-seconds / half_seconds))


@dataclass(frozen=True)
class DecayOutcome:
    """What `decay_belief` returns. Row-level effective-at-`now` view."""

    belief_id: str
    before: BetaConfidence  # stored alpha/beta (provenance)
    after: BetaConfidence  # effective at `now`
    ratio: float
    skipped: bool  # True when entity_type has infinite half-life or flag off


def decay_belief(
    belief: Belief,
    *,
    now: datetime | None = None,
    profile: dict[str, timedelta | None] | None = None,
) -> DecayOutcome:
    """Pure decay — returns the effective Beta at `now` without mutating.

    Callers that need the decayed values use `outcome.after`. The Belief
    row is never touched; `confidence_alpha` / `confidence_beta` remain
    the canonical observed-at-write values. This is the property that
    makes the sweep idempotent and keeps provenance intact.

    No-op (returns `skipped=True` with `after == before`) when
    `settings.MEMORY_ENABLED` is False or when the entity type has an
    infinite half-life.
    """
    now = now or datetime.now(UTC)
    profile = profile if profile is not None else parse_decay_profile(settings.MEMORY_DECAY_PROFILE)

    before = BetaConfidence(
        alpha=float(belief.confidence_alpha or 1.0),
        beta=float(belief.confidence_beta or 1.0),
    )

    if not settings.MEMORY_ENABLED:
        return DecayOutcome(belief_id=belief.id, before=before, after=before, ratio=1.0, skipped=True)

    half_life = _effective_halflife(belief.entity_type or "", profile)
    if half_life is None:
        return DecayOutcome(belief_id=belief.id, before=before, after=before, ratio=1.0, skipped=True)

    observed = belief.observed_at or now
    if observed.tzinfo is None:
        # SQLite sometimes hands back naive datetimes; treat as UTC.
        observed = observed.replace(tzinfo=UTC)

    age = now - observed
    ratio = _decay_ratio(age, half_life)
    # Numerical floor so we never collapse to exactly zero, which would
    # break downstream BetaConfidence construction (its `_MIN_PARAM`
    # clamp would silently truncate the ratio and make tests flaky).
    ratio = max(ratio, 1e-6)

    after = BetaConfidence(alpha=before.alpha * ratio, beta=before.beta * ratio)
    return DecayOutcome(belief_id=belief.id, before=before, after=after, ratio=ratio, skipped=False)


def effective_sample_size(
    belief: Belief,
    *,
    now: datetime | None = None,
    profile: dict[str, timedelta | None] | None = None,
) -> float:
    """Return `(α + β) × decay_ratio(age, half_life)` — effective evidence count.

    This is the natural "how much evidence do we still have?" metric
    for a decaying Beta belief:

    - Mean (α/(α+β)) is invariant under equal-ratio scaling, so it
      can't detect aged-out beliefs on its own.
    - `BetaConfidence.strength()` blends mean and variance, but its
      floor is mean × (1 - 0.25) even at zero effective sample size,
      which is too high to be a useful decay gate.
    - `(α + β) × ratio` goes cleanly to zero as age → ∞ and stays
      intuitive: "0.5 pseudo-observations remaining" means we know
      effectively nothing, regardless of the original mean.

    For infinite-half-life entity types the ratio is 1.0 so this is
    just (α+β) — use `decay_belief(...).skipped` to detect those.
    """
    outcome = decay_belief(belief, now=now, profile=profile)
    return outcome.after.alpha + outcome.after.beta


# ---------------------------------------------------------------------------
# Tombstoning — sets superseded_at. The only persistent side-effect.
# ---------------------------------------------------------------------------


#: Default effective-sample-size floor below which a decayed row is
#: tombstoned. `(α+β) × decay_ratio`. A value of 1.0 means "less than
#: one pseudo-observation of evidence survives" — safe to retire.
_DEFAULT_TOMBSTONE_SAMPLE_SIZE = 1.0


@dataclass(frozen=True)
class ForgetSweepOutcome:
    """Summary returned by `run_forget_sweep`."""

    scanned: int
    tombstoned: int
    skipped_infinite: int  # rows with an infinite half-life
    dry_run: bool


def run_forget_sweep(
    db: Session,
    *,
    sample_size_floor: float = _DEFAULT_TOMBSTONE_SAMPLE_SIZE,
    now: datetime | None = None,
    dry_run: bool = False,
    batch_size: int = 1000,
) -> ForgetSweepOutcome:
    """Tombstone live beliefs whose effective sample size has decayed below the floor.

    The sweep never mutates `confidence_alpha` / `confidence_beta`
    (decay is computed on read). It only sets `superseded_at = now` on
    rows whose `(α+β) × decay_ratio(age, half_life)` has dropped below
    `sample_size_floor`.

    Caller owns commit. This function issues `flush()` so SQLAlchemy
    sees the tombstoning mutations, but does NOT commit. The scheduler
    or sysadmin CLI commits.

    When `settings.MEMORY_ENABLED` is False, returns a zero-touch
    outcome without querying anything.
    """
    if not settings.MEMORY_ENABLED:
        return ForgetSweepOutcome(scanned=0, tombstoned=0, skipped_infinite=0, dry_run=dry_run)
    if sample_size_floor < 0:
        raise ValueError("sample_size_floor must be non-negative")

    now = now or datetime.now(UTC)
    profile = parse_decay_profile(settings.MEMORY_DECAY_PROFILE)

    q = db.query(Belief).filter(Belief.superseded_at.is_(None))

    scanned = 0
    tombstoned = 0
    skipped_infinite = 0

    for b in q.yield_per(batch_size):
        scanned += 1
        outcome = decay_belief(b, now=now, profile=profile)
        if outcome.skipped:
            # Infinite half-life rows are immune to decay-based
            # tombstoning. Operators who want to purge them must go
            # through `forget_by_entity` (user-directed forget).
            skipped_infinite += 1
            continue
        effective_ss = outcome.after.alpha + outcome.after.beta
        if effective_ss < sample_size_floor:
            if not dry_run:
                b.superseded_at = now
            tombstoned += 1

    if not dry_run and tombstoned:
        db.flush()

    return ForgetSweepOutcome(
        scanned=scanned,
        tombstoned=tombstoned,
        skipped_infinite=skipped_infinite,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# User-directed forget — powers the /v1/memory/forget endpoint
# ---------------------------------------------------------------------------


def forget_by_entity(
    db: Session,
    *,
    entity: str,
    user_id: str | None = None,
    predicate: str | None = None,
    now: datetime | None = None,
) -> int:
    """Tombstone live beliefs matching (entity, optional predicate, user_id).

    Returns the number of rows tombstoned. Used by the user-facing
    "forget this fact" API. Respects `settings.MEMORY_ENABLED` (no-op
    when off).

    When `user_id` is given, matches beliefs scoped to that user OR to
    no user at all — tenants purging their own data should not touch
    genuinely global rows unless they explicitly want to.
    """
    if not settings.MEMORY_ENABLED:
        return 0
    if not entity:
        raise ValueError("entity is required")

    now = now or datetime.now(UTC)
    q = db.query(Belief).filter(
        Belief.entity == entity,
        Belief.superseded_at.is_(None),
    )
    if predicate is not None:
        q = q.filter(Belief.predicate == predicate)
    if user_id is not None:
        q = q.filter(or_(Belief.user_id == user_id, Belief.user_id.is_(None)))

    rows = q.all()
    for b in rows:
        b.superseded_at = now
    if rows:
        db.flush()
    return len(rows)
