"""Tamper-evident trace hash chain verification."""

import hashlib
import json
import logging
from datetime import date, datetime
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


_FULL_RECORD_FIELDS = (
    "id",
    "session_id",
    "sequence",
    "prompt",
    "prompt_hash",
    "immune_verdict",
    "immune_score",
    "immune_details",
    "asflc_result",
    "asflc_chosen_path",
    "asflc_confidence",
    "asflc_loops",
    "critic_verdict",
    "critic_scores",
    "critic_rollback_count",
    "governance_status",
    "governance_policy_id",
    "governance_token_id",
    "response",
    "response_hash",
    "output_scan_verdict",
    "model_id",
    "latency_ms",
    "token_count",
    "error",
    "status",
    "prev_hash",
    "trace_hash",
    "run_mode",
    "task_reward_score",
    "user_feedback",
    "total_steps",
    "self_corrections",
    "agent_state",
    "agent_trajectory",
    "mcp_backend",
    "mcp_tool_name",
    "beliefs_used",
    "beliefs_formed",
)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


def compute_full_record_hash(trace: Any) -> str:
    """Hash all persisted trace audit fields except full_record_hash itself."""
    payload = {field: _canonicalize(getattr(trace, field, None)) for field in _FULL_RECORD_FIELDS}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_MAX_CHAIN_TRACES = 10_000


def verify_chain(session_id: str, db_session: Session) -> list[dict[str, Any]]:
    """
    Walk traces for a session in sequence order and verify hash links.

    Returns a list of problem dicts; empty list means the chain is intact.
    Capped at _MAX_CHAIN_TRACES rows to prevent unbounded memory use.
    """
    from app.models.trace import Trace

    rows = (
        db_session.query(Trace)
        .filter_by(session_id=session_id)
        .order_by(Trace.sequence.asc(), Trace.created_at.asc())
        .limit(_MAX_CHAIN_TRACES)
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

        if getattr(t, "full_record_hash", None):
            expected_full = compute_full_record_hash(t)
            if t.full_record_hash != expected_full:
                problems.append(
                    {
                        "trace_id": t.id,
                        "issue": "full_record_hash_mismatch",
                        "expected": expected_full,
                        "got": t.full_record_hash,
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
        .limit(_MAX_CHAIN_TRACES)
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
        if hasattr(current, "full_record_hash"):
            current.full_record_hash = compute_full_record_hash(current)
        if i + 1 < len(remaining):
            remaining[i + 1].prev_hash = current.trace_hash
