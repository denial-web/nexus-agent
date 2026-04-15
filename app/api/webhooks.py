"""Webhook management API — CRUD + test delivery."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.webhook import Webhook
from app.services.webhooks import WebhookEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


class WebhookCreate(BaseModel):
    url: str
    events: list[str]
    secret: str | None = None
    description: str | None = None


class WebhookUpdate(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    secret: str | None = None
    description: str | None = None
    enabled: bool | None = None


_VALID_EVENTS = {e.value for e in WebhookEvent} | {"*"}


def _validate_events(events: list[str]) -> None:
    invalid = [e for e in events if e not in _VALID_EVENTS]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid event types: {invalid}. Valid: {sorted(_VALID_EVENTS)}",
        )


@router.post("")
def create_webhook(body: WebhookCreate, db: Session = Depends(get_db)) -> dict:
    _validate_events(body.events)
    wh = Webhook(
        url=body.url,
        events=body.events,
        secret=body.secret,
        description=body.description,
    )
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return _to_dict(wh)


@router.get("")
def list_webhooks(db: Session = Depends(get_db)) -> dict:
    webhooks = db.query(Webhook).order_by(Webhook.created_at.desc()).all()
    return {"webhooks": [_to_dict(wh) for wh in webhooks]}


@router.get("/{webhook_id}")
def get_webhook(webhook_id: str, db: Session = Depends(get_db)) -> dict:
    wh = db.query(Webhook).filter_by(id=webhook_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return _to_dict(wh)


@router.patch("/{webhook_id}")
def update_webhook(
    webhook_id: str, body: WebhookUpdate, db: Session = Depends(get_db),
) -> dict:
    wh = db.query(Webhook).filter_by(id=webhook_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if body.events is not None:
        _validate_events(body.events)
        wh.events = body.events
    if body.url is not None:
        wh.url = body.url
    if body.secret is not None:
        wh.secret = body.secret
    if body.description is not None:
        wh.description = body.description
    if body.enabled is not None:
        wh.enabled = body.enabled
        if body.enabled:
            wh.failure_count = 0
            wh.last_error = None
    wh.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(wh)
    return _to_dict(wh)


@router.delete("/{webhook_id}")
def delete_webhook(webhook_id: str, db: Session = Depends(get_db)) -> dict:
    wh = db.query(Webhook).filter_by(id=webhook_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")
    db.delete(wh)
    db.commit()
    return {"deleted": webhook_id}


@router.post("/{webhook_id}/test")
def test_webhook(webhook_id: str, db: Session = Depends(get_db)) -> dict:
    wh = db.query(Webhook).filter_by(id=webhook_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")

    from app.services.webhooks import WebhookPayload, _deliver

    payload = WebhookPayload(
        event="test",
        timestamp=datetime.now(UTC).isoformat(),
        data={"message": "Test webhook from Nexus Agent", "webhook_id": webhook_id},
    )
    success = _deliver(wh.url, payload, wh.secret, wh.id)
    return {"success": success, "url": wh.url}


@router.get("/events/list")
def list_events() -> dict:
    return {"events": sorted(_VALID_EVENTS)}


def _to_dict(wh: Webhook) -> dict:
    return {
        "id": wh.id,
        "url": wh.url,
        "events": wh.events,
        "enabled": wh.enabled,
        "description": wh.description,
        "failure_count": wh.failure_count,
        "last_triggered_at": wh.last_triggered_at.isoformat() if wh.last_triggered_at else None,
        "last_error": wh.last_error,
        "created_at": wh.created_at.isoformat() if wh.created_at else None,
    }
