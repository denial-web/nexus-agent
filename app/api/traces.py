"""Trace replay and audit log endpoints."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/traces", tags=["Traces"])


@router.post("/{trace_id}/re-evaluate")
def re_evaluate_trace_endpoint(trace_id: str, db: Session = Depends(get_db)):
    """Re-run the current critic tree on a stored trace (does not modify the trace)."""
    from app.services.replay import re_evaluate_trace

    payload = re_evaluate_trace(trace_id, db)
    if payload is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return payload


@router.get("")
def list_traces(
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List execution traces with optional filters."""
    from app.models.trace import Trace

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


@router.get("/session/{session_id}/verify-chain")
def verify_chain_endpoint(session_id: str, db: Session = Depends(get_db)):
    """Verify tamper-evident hash chain for all traces in a session."""
    from app.services.integrity import verify_chain

    problems = verify_chain(session_id, db)
    return {
        "session_id": session_id,
        "valid": len(problems) == 0,
        "problems": problems,
    }


@router.get("/{trace_id}")
def get_trace(trace_id: str, db: Session = Depends(get_db)):
    """Get full trace details for replay."""
    from app.models.trace import Trace

    trace = db.query(Trace).filter_by(id=trace_id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    return {
        "trace": _trace_detail(trace),
    }


@router.get("/{trace_id}/replay")
def replay_trace(trace_id: str, db: Session = Depends(get_db)):
    """Replay a trace showing each pipeline step in order."""
    from app.models.trace import Trace

    trace = db.query(Trace).filter_by(id=trace_id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    steps = []

    steps.append({
        "step": 1,
        "name": "input_scan",
        "verdict": trace.immune_verdict,
        "score": trace.immune_score,
        "details": trace.immune_details,
    })

    if trace.asflc_result:
        steps.append({
            "step": 2,
            "name": "asflc_analysis",
            "chosen_path": trace.asflc_chosen_path,
            "confidence": trace.asflc_confidence,
            "loops": trace.asflc_loops,
            "result": trace.asflc_result,
        })

    if trace.critic_verdict:
        steps.append({
            "step": 3,
            "name": "critic_evaluation",
            "verdict": trace.critic_verdict,
            "scores": trace.critic_scores,
            "rollback_count": trace.critic_rollback_count,
        })

    if trace.governance_status:
        steps.append({
            "step": 4,
            "name": "governance_check",
            "status": trace.governance_status,
            "policy_id": trace.governance_policy_id,
        })

    if trace.output_scan_verdict:
        steps.append({
            "step": 5,
            "name": "output_scan",
            "verdict": trace.output_scan_verdict,
        })

    return {
        "trace_id": trace.id,
        "session_id": trace.session_id,
        "status": trace.status,
        "steps": steps,
        "latency_ms": trace.latency_ms,
    }


def _trace_summary(trace) -> dict:
    return {
        "id": trace.id,
        "session_id": trace.session_id,
        "status": trace.status,
        "immune_verdict": trace.immune_verdict,
        "critic_verdict": trace.critic_verdict,
        "latency_ms": trace.latency_ms,
        "created_at": trace.created_at.isoformat() if trace.created_at else None,
    }


def _trace_detail(trace) -> dict:
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
