"""Trace replay and audit log endpoints."""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.db import get_db
from app.models.trace import Trace

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/traces", tags=["Traces"])


class TraceSummary(BaseModel):
    id: str
    session_id: str
    status: str
    immune_verdict: str | None = None
    critic_verdict: str | None = None
    latency_ms: float | None = None
    created_at: str | None = None


class TraceListResponse(BaseModel):
    total: int
    traces: list[TraceSummary]


class TraceDetailResponse(BaseModel):
    trace: dict[str, Any]


class ReplayStep(BaseModel):
    step: int
    name: str


class ReplayResponse(BaseModel):
    trace_id: str
    session_id: str
    status: str
    steps: list[dict[str, Any]]
    latency_ms: float | None = None


class ChainVerifyResponse(BaseModel):
    session_id: str
    valid: bool
    problems: list[dict[str, Any]]


@router.post("/{trace_id}/re-evaluate")
def re_evaluate_trace_endpoint(trace_id: str, db: Session = Depends(get_db)) -> dict:
    """Re-run the current critic tree on a stored trace (does not modify the trace)."""
    from app.services.replay import re_evaluate_trace

    payload = re_evaluate_trace(trace_id, db)
    if payload is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return payload


@router.get("", response_model=TraceListResponse)
def list_traces(
    session_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    """List execution traces with optional filters."""
    q = db.query(Trace)
    if session_id:
        q = q.filter_by(session_id=session_id)
    if status:
        q = q.filter_by(status=status)

    total = q.count()
    traces = q.order_by(Trace.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "traces": [_trace_summary(t) for t in traces],
    }


@router.get("/session/{session_id}/verify-chain", response_model=ChainVerifyResponse)
def verify_chain_endpoint(session_id: str, db: Session = Depends(get_db)) -> dict:
    """Verify tamper-evident hash chain for all traces in a session."""
    from app.services.integrity import verify_chain

    problems = verify_chain(session_id, db)
    return {
        "session_id": session_id,
        "valid": len(problems) == 0,
        "problems": problems,
    }


@router.get("/{trace_id}", response_model=TraceDetailResponse, responses={404: {"description": "Trace not found"}})
def get_trace(trace_id: str, db: Session = Depends(get_db)) -> dict:
    """Get full trace details for replay."""
    trace = db.query(Trace).filter_by(id=trace_id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    return {
        "trace": _trace_detail(trace),
    }


@router.get("/{trace_id}/replay", response_model=ReplayResponse, responses={404: {"description": "Trace not found"}})
def replay_trace(trace_id: str, db: Session = Depends(get_db)) -> dict:
    """Replay a trace showing each pipeline step in order."""
    trace = db.query(Trace).filter_by(id=trace_id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    steps = []

    steps.append(
        {
            "step": 1,
            "name": "input_scan",
            "verdict": trace.immune_verdict,
            "score": trace.immune_score,
            "details": trace.immune_details,
        }
    )

    if trace.asflc_result:
        steps.append(
            {
                "step": 2,
                "name": "asflc_analysis",
                "chosen_path": trace.asflc_chosen_path,
                "confidence": trace.asflc_confidence,
                "loops": trace.asflc_loops,
                "result": trace.asflc_result,
            }
        )

    if trace.critic_verdict:
        steps.append(
            {
                "step": 3,
                "name": "critic_evaluation",
                "verdict": trace.critic_verdict,
                "scores": trace.critic_scores,
                "rollback_count": trace.critic_rollback_count,
            }
        )

    if trace.governance_status:
        steps.append(
            {
                "step": 4,
                "name": "governance_check",
                "status": trace.governance_status,
                "policy_id": trace.governance_policy_id,
            }
        )

    if trace.output_scan_verdict:
        steps.append(
            {
                "step": 5,
                "name": "output_scan",
                "verdict": trace.output_scan_verdict,
            }
        )

    return {
        "trace_id": trace.id,
        "session_id": trace.session_id,
        "status": trace.status,
        "steps": steps,
        "latency_ms": trace.latency_ms,
    }


def _trace_summary(trace: Trace) -> dict:
    return {
        "id": trace.id,
        "session_id": trace.session_id,
        "status": trace.status,
        "immune_verdict": trace.immune_verdict,
        "critic_verdict": trace.critic_verdict,
        "latency_ms": trace.latency_ms,
        "created_at": trace.created_at.isoformat() if trace.created_at else None,
    }


def _trace_detail(trace: Trace) -> dict:
    return {
        "id": trace.id,
        "session_id": trace.session_id,
        "sequence": trace.sequence,
        "prompt": trace.prompt,
        "prompt_hash": trace.prompt_hash,
        "immune_verdict": trace.immune_verdict,
        "immune_score": trace.immune_score,
        "immune_details": trace.immune_details,
        "asflc_result": trace.asflc_result,
        "asflc_chosen_path": trace.asflc_chosen_path,
        "asflc_confidence": trace.asflc_confidence,
        "critic_verdict": trace.critic_verdict,
        "critic_scores": trace.critic_scores,
        "critic_rollback_count": trace.critic_rollback_count,
        "governance_status": trace.governance_status,
        "governance_policy_id": trace.governance_policy_id,
        "response": trace.response,
        "output_scan_verdict": trace.output_scan_verdict,
        "model_id": trace.model_id,
        "token_count": trace.token_count,
        "latency_ms": trace.latency_ms,
        "status": trace.status,
        "error": trace.error,
        "prev_hash": trace.prev_hash,
        "trace_hash": trace.trace_hash,
        "governance_token_id": trace.governance_token_id,
        "created_at": trace.created_at.isoformat() if trace.created_at else None,
    }


@router.get("/audit/events")
def list_audit_event_types() -> dict:
    """List available audit event types for filtering."""
    from app.services.audit_export import get_event_types

    return {"event_types": get_event_types()}


@router.get("/audit/export")
def export_audit_logs(
    event_type: list[str] | None = Query(None),
    since: datetime | None = None,
    until: datetime | None = None,
    status: str | None = None,
    limit: int = Query(1000, le=10000),
    offset: int = Query(0, ge=0),
    format: str = Query("jsonl"),
    db: Session = Depends(get_db),
) -> Response:
    """Export audit logs in SIEM-compatible JSON-lines format.

    Supports filtering by event type, time range, status, and pagination.
    Returns application/x-ndjson (JSON-lines) by default, or JSON array.
    """
    from app.services.audit_export import export_audit_logs as do_export
    from app.services.audit_export import records_to_jsonl

    records = do_export(
        db,
        event_types=event_type,
        since=since,
        until=until,
        status=status,
        limit=limit,
        offset=offset,
    )

    if format == "json":
        import json

        body = json.dumps({"total": len(records), "records": records}, default=str)
        return Response(content=body, media_type="application/json")

    body = records_to_jsonl(records)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=audit-export.jsonl"},
    )
