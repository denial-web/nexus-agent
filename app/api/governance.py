"""Governance and approval endpoints."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.sanitize import sanitize_for_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/governance", tags=["Governance"])


class PolicyCreate(BaseModel):
    name: str
    description: str | None = None
    action_pattern: str
    resource_pattern: str | None = None
    decision: Literal["allow", "require_approval", "deny"]
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    required_approvals: int = Field(default=0, ge=0)
    priority: int = Field(default=100, ge=0)


class ApprovalVoteRequest(BaseModel):
    approver_id: str
    decision: str
    reason: str | None = None


@router.get("/policies")
def list_policies(active_only: bool = True, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)) -> dict:
    """List governance policies."""
    from app.models.policy import Policy

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    q = db.query(Policy)
    if active_only:
        q = q.filter_by(is_active=True)
    policies = q.order_by(Policy.priority).offset(offset).limit(limit).all()

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

    existing = db.query(Policy).filter_by(name=req.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Policy with name {sanitize_for_error(req.name)} already exists")

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
def submit_vote(request_id: str, vote: ApprovalVoteRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Submit an approval or denial vote for a pending action."""
    from app.services.approval import process_vote, resolve_approver_identity

    approver_identity, identity_error = resolve_approver_identity(request, vote.approver_id)
    if identity_error or not approver_identity:
        raise HTTPException(status_code=403, detail=identity_error or "Invalid approver identity")

    result = process_vote(
        request_id=request_id,
        approver_id=approver_identity,
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
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """View the labeling queue for training flywheel."""
    from app.core.training.labeler import get_queue

    items = get_queue(status=status, failure_type=failure_type, limit=limit, db_session=db)
    return {"items": items, "count": len(items)}
