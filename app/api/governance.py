"""Governance and approval endpoints."""

import logging
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/governance", tags=["Governance"])


class PolicyCreate(BaseModel):
    name: str
    description: str | None = None
    action_pattern: str
    resource_pattern: str | None = None
    decision: str  # "allow", "require_approval", "deny"
    risk_level: str = "medium"
    required_approvals: int = 0
    priority: int = 100


class ApprovalVoteRequest(BaseModel):
    approver_id: str
    decision: str
    reason: str | None = None


@router.get("/policies")
def list_policies(active_only: bool = True, db: Session = Depends(get_db)) -> dict:
    """List governance policies."""
    from app.models.policy import Policy

    q = db.query(Policy)
    if active_only:
        q = q.filter_by(is_active=True)
    policies = q.order_by(Policy.priority).all()

    return {
        "policies": [
            {
                "id": p.id,
                "name": p.name,
                "action_pattern": p.action_pattern,
                "decision": p.decision,
                "risk_level": p.risk_level,
                "required_approvals": p.required_approvals,
                "priority": p.priority,
                "is_active": p.is_active,
            }
            for p in policies
        ]
    }


@router.post("/policies")
def create_policy(req: PolicyCreate, db: Session = Depends(get_db)) -> dict:
    """Create a new governance policy."""
    from app.models.policy import Policy

    policy = Policy(
        name=req.name,
        description=req.description,
        action_pattern=req.action_pattern,
        resource_pattern=req.resource_pattern,
        decision=req.decision,
        risk_level=req.risk_level,
        required_approvals=str(req.required_approvals),
        priority=str(req.priority),
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return {"policy": {"id": policy.id, "name": policy.name, "decision": policy.decision}}


@router.get("/approvals")
def list_pending_approvals(status: str = "pending", db: Session = Depends(get_db)) -> dict:
    """List approval requests."""
    from app.models.approval_log import ApprovalRequest

    requests = (
        db.query(ApprovalRequest).filter_by(status=status).order_by(ApprovalRequest.created_at.desc()).limit(50).all()
    )

    return {
        "requests": [
            {
                "id": r.id,
                "trace_id": r.trace_id,
                "action_type": r.action_type,
                "risk_level": r.risk_level,
                "required_approvals": r.required_approvals,
                "received_approvals": r.received_approvals,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in requests
        ]
    }


@router.post("/approve/{request_id}")
def submit_vote(request_id: str, vote: ApprovalVoteRequest, db: Session = Depends(get_db)) -> dict:
    """Submit an approval or denial vote for a pending action."""
    from datetime import datetime

    from app.config import settings
    from app.core.covernor.token_manager import issue_token
    from app.core.immune.scanner import Verdict, scan_output
    from app.models.approval_log import ApprovalRequest, ApprovalVote
    from app.models.trace import Trace

    if vote.decision not in ("approve", "deny"):
        raise HTTPException(status_code=422, detail="Decision must be 'approve' or 'deny'")

    req = db.query(ApprovalRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {req.status}")

    if req.expires_at:
        exp = req.expires_at if req.expires_at.tzinfo else req.expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) > exp:
            req.status = "expired"
            db.commit()
            raise HTTPException(status_code=400, detail="Approval request has expired")

    dup = db.query(ApprovalVote).filter_by(request_id=request_id, approver_id=vote.approver_id).first()
    if dup:
        raise HTTPException(status_code=409, detail="Approver already voted on this request")

    required = max(int(req.required_approvals), settings.APPROVAL_QUORUM)
    req.required_approvals = str(required)

    approval_vote = ApprovalVote(
        request_id=request_id,
        approver_id=vote.approver_id,
        decision=vote.decision,
        reason=vote.reason,
    )
    db.add(approval_vote)

    if vote.decision == "deny":
        req.status = "denied"
        req.resolved_at = datetime.now(UTC)
    else:
        req.received_approvals = str(int(req.received_approvals) + 1)
        if int(req.received_approvals) >= int(req.required_approvals):
            req.status = "approved"
            token = issue_token(
                trace_id=req.trace_id,
                action_type=req.action_type,
                scope=req.token_scope,
            )
            req.capability_token = token.signature
            req.resolved_at = datetime.now(UTC)

            trace = db.query(Trace).filter_by(id=req.trace_id).first()
            if trace:
                trace.governance_token_id = token.token_id
                response_text = trace.response or ""
                if response_text:
                    out = scan_output(response_text)
                    trace.output_scan_verdict = out.verdict.value
                    if out.verdict == Verdict.BLOCK:
                        trace.status = "blocked"
                        trace.error = "Output blocked by immune scanner after approval"
                    else:
                        trace.status = "completed"
                        trace.governance_status = "approved"
                        trace.error = None
                else:
                    trace.status = "blocked"
                    trace.error = "No response to release"

                from app.services.integrity import cascade_rehash_from_trace

                cascade_rehash_from_trace(db, trace.id)

    db.commit()
    return {"status": req.status, "received": req.received_approvals, "required": req.required_approvals}


@router.get("/training/queue")
def get_labeling_queue(
    status: str = "pending",
    failure_type: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """View the labeling queue for training flywheel."""
    from app.core.training.labeler import get_queue

    items = get_queue(status=status, failure_type=failure_type, db_session=db)
    return {"items": items, "count": len(items)}
