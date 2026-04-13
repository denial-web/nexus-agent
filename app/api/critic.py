"""Critic registry management endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/critic", tags=["Critic"])


class CriticNodeCreate(BaseModel):
    name: str
    node_type: str
    description: str | None = None
    prompt_template: str | None = None
    weight: float = 1.0
    threshold_pass: float = 0.7
    threshold_halt: float = 0.3
    can_halt: bool = False
    lora_adapter_path: str | None = None
    config: dict | None = None


class CriticNodeUpdate(BaseModel):
    prompt_template: str | None = None
    weight: float | None = None
    threshold_pass: float | None = None
    threshold_halt: float | None = None
    can_halt: bool | None = None
    lora_adapter_path: str | None = None
    is_active: bool | None = None
    config: dict | None = None


@router.get("/registry")
def list_critic_nodes(
    node_type: str | None = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """List registered critic nodes."""
    from app.models.critic_registry import CriticNode

    q = db.query(CriticNode)
    if node_type:
        q = q.filter_by(node_type=node_type)
    if active_only:
        q = q.filter_by(is_active=True)

    nodes = q.order_by(CriticNode.node_type, CriticNode.name).all()
    return {"nodes": [_node_dict(n) for n in nodes]}


@router.post("/registry")
def create_critic_node(req: CriticNodeCreate, db: Session = Depends(get_db)):
    """Register a new critic node configuration."""
    from app.models.critic_registry import CriticNode

    existing = db.query(CriticNode).filter_by(name=req.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Node '{req.name}' already exists")

    node = CriticNode(
        name=req.name,
        node_type=req.node_type,
        description=req.description,
        prompt_template=req.prompt_template,
        weight=req.weight,
        threshold_pass=req.threshold_pass,
        threshold_halt=req.threshold_halt,
        can_halt=req.can_halt,
        lora_adapter_path=req.lora_adapter_path,
        config=req.config,
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    from app.agent.pipeline import invalidate_arbiter_cache

    invalidate_arbiter_cache()

    return {"node": _node_dict(node)}


@router.patch("/registry/{node_id}")
def update_critic_node(node_id: str, req: CriticNodeUpdate, db: Session = Depends(get_db)):
    """Update a critic node (hot-swap prompt or LoRA adapter)."""
    from app.models.critic_registry import CriticNode

    node = db.query(CriticNode).filter_by(id=node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    updates = req.model_dump(exclude_unset=True)
    if "prompt_template" in updates:
        node.prompt_version += 1

    for key, value in updates.items():
        setattr(node, key, value)

    db.commit()
    db.refresh(node)

    from app.agent.pipeline import invalidate_arbiter_cache

    invalidate_arbiter_cache()

    return {"node": _node_dict(node)}


def _node_dict(node) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "node_type": node.node_type,
        "description": node.description,
        "prompt_version": node.prompt_version,
        "weight": node.weight,
        "threshold_pass": node.threshold_pass,
        "threshold_halt": node.threshold_halt,
        "can_halt": node.can_halt,
        "lora_adapter_path": node.lora_adapter_path,
        "is_active": node.is_active,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }
