"""Governance and approval endpoints."""

import logging

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
        priority=req.priority,
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
    from app.services.approval import process_vote

    result = process_vote(
        request_id=request_id,
        approver_id=vote.approver_id,
        decision=vote.decision,
        db=db,
    )
    if result.error:
        raise HTTPException(status_code=result.http_status, detail=result.error)
    return {"status": result.status, "received": result.received, "required": result.required}


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
