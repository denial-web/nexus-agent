"""
Belief hash-chain integrity service.

Every accepted `Belief` row carries a tamper-evident per-user hash chain:
`prev_hash` points at the previous row's `belief_hash` (or the string
`"genesis"` for the first row in a chain), and `belief_hash` is a
sha256 of the row's immutable fields. `app.core.memory.writer`
produces the chain; this module verifies it.

Prior to this module, the only verifier lived in
`tests/eval/contradiction_qa.py` as `_recompute_hash` + `_verify_chain`.
That's good enough to prove the writer doesn't produce a broken chain,
but the "tamper-evident hash chain" claim in the public pitch needs an
externally-callable audit primitive. This module promotes those helpers
to the production API surface and adds two capabilities the benchmark
didn't need:

* **Cross-user verification.** When `user_id=None`, iterate over every
  distinct per-user chain in the DB. This is the answer auditors want:
  "is the whole store intact?" not "is Alice's chain intact?"
* **Point-in-time verification (`as_of`).** Restrict verification to
  rows with `observed_at <= as_of`. Matches the bitemporal convention
  used by `retrieval.beliefs_as_of()` and lets operators ask "was the
  chain intact on 2026-04-01?" without being affected by later writes.

Conventions (must match the writer byte-for-byte):

* `_HASH_GENESIS = "genesis"` — the sentinel seeding every chain's
  first row's `prev_hash`. Never NULL on a live chain.
* Hash payload = `"|".join([belief_id, prev_hash, entity, predicate,
  value_json, source_type, source_trace_id_or_empty,
  observed_at.isoformat()])`, UTF-8, sha256.
* `value_json = json.dumps(value, sort_keys=True, default=str)`.
* `source_trace_id or ""` — NULL collapses to the empty string.
* `observed_at.isoformat()` on a **tz-aware** datetime. SQLite strips
  tzinfo on round-trip even on `DateTime(timezone=True)` columns;
  when the stored row comes back naive we re-attach UTC before
  calling `isoformat()` so we reproduce the `+00:00` suffix the
  writer hashed over. Postgres TIMESTAMPTZ round-trips tz-aware
  already and this branch is a no-op there.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models.belief import Belief

logger = logging.getLogger(__name__)

# Must match writer._HASH_GENESIS verbatim. Duplicated (rather than
# imported) because writer.py depends on config/session/settings and
# keeping the verifier free of those transitive imports makes it safe
# to call from any context including a read-only audit worker.
_HASH_GENESIS = "genesis"


@dataclass(frozen=True)
class IntegrityResult:
    """Outcome of a hash-chain verification run.

    `verified` is True only when every row checked produced a matching
    `prev_hash` and a reproducible `belief_hash`. On failure,
    `first_break_at` identifies the id of the offending row (the first
    one detected in walking order) and `reason` gives a human-readable
    explanation.

    `scope_user_ids` is the exhaustive list of per-user chains that
    were walked — useful for audit logs that need to prove "we checked
    every chain, not just Alice's."
    """

    verified: bool
    rows_checked: int
    first_break_at: str | None
    reason: str | None
    scope_user_ids: list[str | None]
    as_of: datetime | None


def compute_belief_hash(row: Belief) -> str:
    """Re-derive `belief_hash` from the row's own fields + its stored
    `prev_hash`. Byte-for-byte compatible with
    `app.core.memory.writer._compute_belief_hash`.

    SQLite returns `DateTime(timezone=True)` columns as naive after a
    round-trip; Postgres returns them tz-aware. The writer hashed a
    tz-aware `observed_at`, so we must re-attach UTC here whenever the
    stored row came back naive — otherwise the verifier would spuriously
    flag every SQLite-stored row as broken.
    """
    value_json = json.dumps(row.value, sort_keys=True, default=str)
    observed = row.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    payload = "|".join(
        [
            row.id,
            row.prev_hash or "",
            row.entity,
            row.predicate,
            value_json,
            row.source_type,
            row.source_trace_id or "",
            observed.isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iter_user_ids(db: Session) -> Iterable[str | None]:
    """Yield every distinct `user_id` that owns at least one belief.

    NULL user_id is a legitimate scope (shared / system beliefs) and
    has its own chain; it must be verified alongside the keyed chains.
    """
    rows = db.query(Belief.user_id).distinct().all()
    return [row[0] for row in rows]


def _verify_single_chain(
    db: Session,
    user_id: str | None,
    as_of: datetime | None,
) -> tuple[bool, int, str | None, str | None]:
    """Walk one per-user chain oldest-first.

    Returns `(verified, rows_checked, first_break_at, reason)`. A
    broken row means *this* row's prev_hash didn't point at the prior
    row's belief_hash, or this row's belief_hash doesn't reproduce.
    Either way we stop at the first break and report it; we don't
    try to recover and continue because a single break already
    invalidates the "tamper-evident" claim for the rest of the chain.
    """
    q = db.query(Belief).filter(Belief.user_id == user_id)
    if as_of is not None:
        q = q.filter(Belief.observed_at <= as_of)
    rows = q.order_by(Belief.observed_at.asc(), Belief.id.asc()).all()

    prev: Belief | None = None
    checked = 0
    for row in rows:
        expected_prev = prev.belief_hash if prev is not None else _HASH_GENESIS
        if row.prev_hash != expected_prev:
            return (
                False,
                checked,
                row.id,
                (
                    f"prev_hash mismatch at belief_id={row.id}: "
                    f"expected={expected_prev!r} got={row.prev_hash!r}"
                ),
            )
        if row.belief_hash != compute_belief_hash(row):
            return (
                False,
                checked,
                row.id,
                f"belief_hash mismatch at belief_id={row.id}: row fields do not reproduce stored hash",
            )
        prev = row
        checked += 1

    return True, checked, None, None


def verify_chain(
    db: Session,
    *,
    user_id: str | None | object = ...,  # sentinel: ... = "all chains"
    as_of: datetime | None = None,
) -> IntegrityResult:
    """Verify one or more per-user belief hash chains.

    Three scope modes controlled by `user_id`:

    * `user_id=...` (default sentinel) — walk **every** distinct chain
      in the DB. This is the audit mode: "is the whole store intact?"
    * `user_id="alice"` — walk only Alice's chain.
    * `user_id=None` — walk the NULL-user chain (shared / system beliefs).
      Distinct from the default sentinel so callers can intentionally
      target the NULL scope.

    `as_of` restricts verification to rows with `observed_at <= as_of`.
    Must be timezone-aware when provided; we enforce this for the same
    reason `beliefs_as_of()` does (deterministic comparisons against
    Postgres TIMESTAMPTZ, no server-tz sensitivity). `None` means "to
    the end of the chain."

    Returns `IntegrityResult`. Never raises for tamper detection — a
    broken chain is a correct non-error outcome of an audit call. Does
    raise on obvious programmer errors (naive `as_of`, feature flag
    off) so misuse is loud.

    No writes. Safe to call from any read-only context.
    """
    if not settings.MEMORY_ENABLED:
        raise RuntimeError(
            "verify_chain() called with MEMORY_ENABLED=false; "
            "the beliefs table is not guaranteed to exist on this deployment."
        )

    if as_of is not None and as_of.tzinfo is None:
        raise ValueError(
            "verify_chain() requires a timezone-aware datetime for `as_of`; "
            "got a naive datetime. Attach tzinfo (e.g. datetime.now(UTC)) "
            "before calling."
        )

    if user_id is ...:
        scope_ids: list[str | None] = list(_iter_user_ids(db))
    else:
        # Caller explicitly picked a single scope — respect it even if
        # that scope has zero rows (answer is then verified=True, 0 rows).
        scope_ids = [user_id]  # type: ignore[list-item]

    total_checked = 0
    for uid in scope_ids:
        ok, checked, break_id, reason = _verify_single_chain(db, uid, as_of)
        total_checked += checked
        if not ok:
            logger.warning(
                "belief hash chain broken: user_id=%s break_at=%s reason=%s",
                uid,
                break_id,
                reason,
            )
            return IntegrityResult(
                verified=False,
                rows_checked=total_checked,
                first_break_at=break_id,
                reason=reason,
                scope_user_ids=scope_ids,
                as_of=as_of,
            )

    return IntegrityResult(
        verified=True,
        rows_checked=total_checked,
        first_break_at=None,
        reason=None,
        scope_user_ids=scope_ids,
        as_of=as_of,
    )
