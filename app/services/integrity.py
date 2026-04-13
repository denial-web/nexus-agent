"""Tamper-evident trace hash chain verification."""

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def compute_trace_hash(
    trace_id: str,
    prev_hash: str,
    prompt_hash: str,
    response_hash: str | None,
    status: str,
) -> str:
    rh = response_hash or ""
    payload = f"{trace_id}:{prev_hash}:{prompt_hash}:{rh}:{status}"
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_chain(session_id: str, db_session: Session) -> list[dict[str, Any]]:
    """
    Walk traces for a session in sequence order and verify hash links.

    Returns a list of problem dicts; empty list means the chain is intact.
    """
    from app.models.trace import Trace

    rows = (
        db_session.query(Trace)
        .filter_by(session_id=session_id)
        .order_by(Trace.sequence.asc(), Trace.created_at.asc())
        .all()
    )
    if not rows:
        return []

    problems: list[dict[str, Any]] = []
    prev_trace_hash: str | None = None

    for i, t in enumerate(rows):
        expected_prev = prev_trace_hash if i > 0 else "genesis"
        if t.prev_hash != expected_prev:
            problems.append(
                {
                    "trace_id": t.id,
                    "issue": "prev_hash_mismatch",
                    "expected_prev": expected_prev,
                    "got_prev": t.prev_hash,
                }
            )

        expected_self = compute_trace_hash(
            t.id,
            t.prev_hash or "genesis",
            t.prompt_hash,
            t.response_hash,
            t.status,
        )
        if t.trace_hash != expected_self:
            problems.append(
                {
                    "trace_id": t.id,
                    "issue": "trace_hash_mismatch",
                    "expected": expected_self,
                    "got": t.trace_hash,
                }
            )

        prev_trace_hash = t.trace_hash

    return problems


def cascade_rehash_from_trace(db_session: Session, trace_id: str) -> None:
    """
    Recompute trace_hash for a trace and all later traces in the same session.

    Call after mutating a trace row (e.g. approval completion) so the chain stays consistent.
    """
    from app.models.trace import Trace

    start = db_session.query(Trace).filter_by(id=trace_id).first()
    if not start:
        return

    remaining = (
        db_session.query(Trace)
        .filter_by(session_id=start.session_id)
        .filter(Trace.sequence >= start.sequence)
        .order_by(Trace.sequence.asc(), Trace.created_at.asc())
        .all()
    )

    for i, current in enumerate(remaining):
        current.trace_hash = compute_trace_hash(
            current.id,
            current.prev_hash or "genesis",
            current.prompt_hash,
            current.response_hash,
            current.status,
        )
        if i + 1 < len(remaining):
            remaining[i + 1].prev_hash = current.trace_hash
