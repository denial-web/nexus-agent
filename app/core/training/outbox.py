"""Doctrine Lab export retry queue — persisted failed imports with backoff."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_BACKOFF_SECONDS = (60, 300, 900, 3600, 7200)


def enqueue_failed_import(
    db: Session,
    batch_id: str,
    dataset_type: str,
    items: list[dict[str, Any]],
    error_message: str,
) -> str:
    """Persist a failed import for later retry."""
    from app.models.training_meta import DoctrineOutbox

    oid = uuid.uuid4().hex
    row = DoctrineOutbox(
        id=oid,
        batch_id=batch_id,
        dataset_type=dataset_type,
        items_json=json.dumps(items),
        attempts=0,
        last_error=error_message[:4000],
        next_retry_at=datetime.now(UTC) + timedelta(seconds=_BACKOFF_SECONDS[0]),
        status="pending",
    )
    db.add(row)
    db.commit()
    logger.warning("Queued Doctrine import retry: id=%s batch=%s", oid, batch_id)
    return oid


def process_outbox_retries(db: Session) -> dict[str, Any]:
    """Attempt pending outbox rows whose next_retry_at has passed."""
    from app.models.training_meta import DoctrineOutbox
    from app.services.doctrine_bridge import import_dataset, is_configured

    if not is_configured():
        return {"retried": 0, "reason": "doctrine_not_configured"}

    now = datetime.now(UTC)
    pending = (
        db.query(DoctrineOutbox)
        .filter(DoctrineOutbox.status == "pending", DoctrineOutbox.next_retry_at <= now)
        .order_by(DoctrineOutbox.next_retry_at.asc())
        .limit(20)
        .all()
    )

    succeeded = 0
    for row in pending:
        try:
            items = json.loads(row.items_json)
            import_dataset(training_items=items, batch_id=row.batch_id, dataset_type=row.dataset_type)
            row.status = "sent"
            row.updated_at = now
            succeeded += 1
            logger.info("Doctrine outbox send succeeded: id=%s batch=%s", row.id, row.batch_id)
        except Exception as exc:
            row.attempts += 1
            row.last_error = str(exc)[:4000]
            row.updated_at = now
            if row.attempts >= _MAX_ATTEMPTS:
                row.status = "dead"
                row.next_retry_at = None
                logger.error("Doctrine outbox gave up after %d attempts: id=%s", row.attempts, row.id)
            else:
                delay = _BACKOFF_SECONDS[min(row.attempts, len(_BACKOFF_SECONDS) - 1)]
                row.next_retry_at = now + timedelta(seconds=delay)
                logger.warning(
                    "Doctrine outbox retry failed (attempt %d): id=%s next_in=%ds",
                    row.attempts,
                    row.id,
                    delay,
                )
        db.commit()

    return {"retried": len(pending), "succeeded": succeeded}
