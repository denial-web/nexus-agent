"""
Training flywheel — labeling queue service.

When the critic tree halts or flags a generation, the failure trace
is pushed to the labeling queue. Reviewed items are exported in
fine-tuning format and fed back into the model improvement loop.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def push_failure(
    trace_id: str,
    source_node: str,
    failure_type: str,
    prompt: str,
    response: str | None,
    critic_output: dict,
    db_session: Session | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Push a failure trace to the labeling queue.

    When *commit* is False the row is flushed but not committed, allowing the
    caller to batch it with other writes (e.g. the Trace insert) in a single
    atomic transaction.
    """
    from app.models.labeling_queue import LabelingItem

    item = LabelingItem(
        id=uuid.uuid4().hex,
        trace_id=trace_id,
        source_node=source_node,
        failure_type=failure_type,
        prompt=prompt,
        response=response or "",
        critic_output=critic_output,
        status="pending",
    )

    if db_session:
        db_session.add(item)
        if commit:
            db_session.commit()
        else:
            db_session.flush()
        logger.info(
            "Pushed failure to labeling queue: %s (source=%s, type=%s)",
            item.id,
            source_node,
            failure_type,
        )
        return _to_dict(item)

    return {
        "id": item.id,
        "trace_id": trace_id,
        "source_node": source_node,
        "failure_type": failure_type,
        "status": "pending",
    }


def label_item(
    item_id: str,
    label: str,
    reviewer_id: str,
    corrected_response: str | None = None,
    reviewer_notes: str | None = None,
    db_session: Session | None = None,
) -> dict | None:
    """Apply a human label to a queued item."""
    if not db_session:
        return None

    from app.models.labeling_queue import LabelingItem

    item = db_session.query(LabelingItem).filter_by(id=item_id).first()
    if not item:
        return None

    item.label = label
    item.reviewer_id = reviewer_id
    item.corrected_response = corrected_response
    item.reviewer_notes = reviewer_notes
    item.status = "labeled"
    item.labeled_at = datetime.now(UTC)

    db_session.commit()
    db_session.refresh(item)
    return _to_dict(item)


def get_queue(
    status: str = "pending",
    failure_type: str | None = None,
    limit: int = 50,
    db_session: Session | None = None,
) -> list[dict]:
    """Get items from the labeling queue."""
    if not db_session:
        return []

    from app.models.labeling_queue import LabelingItem

    q = db_session.query(LabelingItem).filter_by(status=status)
    if failure_type:
        q = q.filter_by(failure_type=failure_type)
    items = q.order_by(LabelingItem.created_at.desc()).limit(limit).all()
    return [_to_dict(i) for i in items]


def export_for_training(
    batch_size: int = 100,
    batch_id: str | None = None,
    enrich_evidential: bool = False,
    db_session: Session | None = None,
) -> list[dict]:
    """
    Export labeled items in fine-tuning format.

    Only exports items with status='labeled' and label='correct_flag'.
    Marks exported items so they aren't re-exported.

    Uses deterministic ordering (created_at ASC, id ASC) so callers computing
    batch_id from a pre-query get the same rows this function exports.

    If batch_id is provided, uses it for idempotency; otherwise generates one.
    If enrich_evidential is True, attaches uncertainty metadata from the trace.
    """
    if not db_session:
        return []

    from app.models.labeling_queue import LabelingItem

    items = (
        db_session.query(LabelingItem)
        .filter_by(status="labeled", label="correct_flag")
        .order_by(LabelingItem.created_at.asc(), LabelingItem.id.asc())
        .limit(batch_size)
        .all()
    )

    if not batch_id:
        batch_id = uuid.uuid4().hex[:12]

    trace_map: dict = {}
    calibration_ece: float | None = None
    if enrich_evidential and items:
        from app.core.training.calibration import get_ece_tracker
        from app.models.trace import Trace

        trace_ids = [i.trace_id for i in items]
        traces = db_session.query(Trace).filter(Trace.id.in_(trace_ids)).all()
        trace_map = {t.id: t for t in traces}
        report = get_ece_tracker().compute_ece()
        calibration_ece = report.ece if report.num_samples > 0 else None

    training_data = []

    for item in items:
        corrected = item.corrected_response or item.response
        export_item: dict = {
            "messages": [
                {"role": "system", "content": "You are a safe, accurate AI assistant."},
                {"role": "user", "content": item.prompt},
                {"role": "assistant", "content": corrected},
            ],
            "metadata": {
                "source": "labeling_queue",
                "trace_id": item.trace_id,
                "failure_type": item.failure_type,
                "source_node": item.source_node,
                "batch_id": batch_id,
            },
        }

        if enrich_evidential and item.trace_id in trace_map:
            from app.core.training.evidential import enrich_training_item

            export_item = enrich_training_item(
                export_item,
                trace_map[item.trace_id],
                calibration_ece=calibration_ece,
            )

        training_data.append(export_item)

        item.status = "exported"
        item.training_batch_id = batch_id
        item.exported_at = datetime.now(UTC)

    if items:
        db_session.commit()

    logger.info("Exported %d items for training (batch=%s)", len(training_data), batch_id)
    return training_data


def _to_dict(item: Any) -> dict:
    return {
        "id": item.id,
        "trace_id": item.trace_id,
        "source_node": item.source_node,
        "failure_type": item.failure_type,
        "status": item.status,
        "label": item.label,
        "reviewer_id": item.reviewer_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
