"""Structured audit log export in SIEM-compatible JSON-lines format.

Each record follows a common envelope:
    {"timestamp": ..., "event_type": ..., "severity": ..., "source": "nexus-agent", "data": {...}}

Event types:
    - pipeline_run: Full pipeline execution trace
    - input_blocked: Immune scanner blocked a prompt
    - output_blocked: Output scan blocked leaked secrets
    - critic_halt: Critic tree halted a generation
    - governance_denied: Covernor denied an action
    - approval_requested: K-of-N approval created
    - approval_resolved: Approval completed (approved/denied/expired)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.approval_log import ApprovalRequest
from app.models.trace import Trace

logger = logging.getLogger(__name__)

_EVENT_TYPES = frozenset(
    {
        "pipeline_run",
        "input_blocked",
        "output_blocked",
        "critic_halt",
        "governance_denied",
        "approval_requested",
        "approval_resolved",
    }
)

_SEVERITY_MAP = {
    "input_blocked": "high",
    "output_blocked": "high",
    "critic_halt": "medium",
    "governance_denied": "medium",
    "approval_requested": "info",
    "approval_resolved": "info",
    "pipeline_run": "info",
}


def get_event_types() -> list[str]:
    return sorted(_EVENT_TYPES)


def _trace_to_record(trace: Trace) -> dict[str, Any]:
    """Convert a Trace row into a SIEM envelope based on its status/verdicts."""
    ts = trace.created_at.isoformat() if trace.created_at else datetime.now(UTC).isoformat()

    if trace.immune_verdict == "block":
        event_type = "input_blocked"
    elif trace.output_scan_verdict == "block":
        event_type = "output_blocked"
    elif trace.critic_verdict == "halt":
        event_type = "critic_halt"
    elif trace.governance_status == "denied":
        event_type = "governance_denied"
    else:
        event_type = "pipeline_run"

    data: dict[str, Any] = {
        "trace_id": trace.id,
        "session_id": trace.session_id,
        "status": trace.status,
        "immune_verdict": trace.immune_verdict,
        "immune_score": trace.immune_score,
        "critic_verdict": trace.critic_verdict,
        "governance_status": trace.governance_status,
        "output_scan_verdict": trace.output_scan_verdict,
        "model_id": trace.model_id,
        "latency_ms": trace.latency_ms,
        "token_count": trace.token_count,
        "trace_hash": trace.trace_hash,
    }
    if trace.error:
        data["error"] = trace.error
    if trace.mcp_backend:
        data["mcp_backend"] = trace.mcp_backend
        data["mcp_tool_name"] = trace.mcp_tool_name

    return {
        "timestamp": ts,
        "event_type": event_type,
        "severity": _SEVERITY_MAP.get(event_type, "info"),
        "source": "nexus-agent",
        "data": data,
    }


def _approval_to_record(approval: ApprovalRequest) -> dict[str, Any]:
    ts = approval.created_at.isoformat() if approval.created_at else datetime.now(UTC).isoformat()

    if approval.status == "pending":
        event_type = "approval_requested"
    else:
        event_type = "approval_resolved"

    data: dict[str, Any] = {
        "approval_id": approval.id,
        "trace_id": approval.trace_id,
        "action_type": approval.action_type,
        "risk_level": approval.risk_level,
        "status": approval.status,
        "required_approvals": approval.required_approvals,
        "received_approvals": approval.received_approvals,
    }
    if approval.resolved_at:
        data["resolved_at"] = approval.resolved_at.isoformat()
    if approval.policy_id:
        data["policy_id"] = approval.policy_id

    return {
        "timestamp": ts,
        "event_type": event_type,
        "severity": _SEVERITY_MAP.get(event_type, "info"),
        "source": "nexus-agent",
        "data": data,
    }


def export_audit_logs(
    db: Session,
    *,
    event_types: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    status: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query traces and approvals, return SIEM-formatted records.

    Filters:
        event_types: Restrict to specific event types (default: all)
        since/until: Time range filter on created_at
        status: Filter traces by status
        limit/offset: Pagination (max 10000 per call)
    """
    limit = min(limit, 10000)
    want_types = set(event_types) if event_types else _EVENT_TYPES

    records: list[dict[str, Any]] = []

    trace_types = want_types & {
        "pipeline_run",
        "input_blocked",
        "output_blocked",
        "critic_halt",
        "governance_denied",
    }
    if trace_types:
        q = db.query(Trace)
        if since:
            q = q.filter(Trace.created_at >= since)
        if until:
            q = q.filter(Trace.created_at <= until)
        if status:
            q = q.filter(Trace.status == status)
        q = q.order_by(Trace.created_at.asc())
        traces = q.offset(offset).limit(limit).all()

        for trace in traces:
            record = _trace_to_record(trace)
            if record["event_type"] in want_types:
                records.append(record)

    approval_types = want_types & {"approval_requested", "approval_resolved"}
    if approval_types:
        remaining = limit - len(records)
        if remaining > 0:
            aq = db.query(ApprovalRequest)
            if since:
                aq = aq.filter(ApprovalRequest.created_at >= since)
            if until:
                aq = aq.filter(ApprovalRequest.created_at <= until)
            aq = aq.order_by(ApprovalRequest.created_at.asc())
            approvals = aq.offset(max(0, offset - len(records))).limit(remaining).all()

            for approval in approvals:
                record = _approval_to_record(approval)
                if record["event_type"] in want_types:
                    records.append(record)

    records.sort(key=lambda r: r["timestamp"])
    return records


def records_to_jsonl(records: list[dict[str, Any]]) -> str:
    """Serialize records to JSON-lines format (one JSON object per line)."""
    lines = []
    for record in records:
        lines.append(json.dumps(record, default=str, separators=(",", ":")))
    return "\n".join(lines) + ("\n" if lines else "")
