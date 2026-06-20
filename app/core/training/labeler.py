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
                "critic_scores": item.critic_output or {},
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

    if training_data:
        try:
            from app.services.webhooks import fire_event

            fire_event(
                "export_complete",
                {
                    "batch_id": batch_id,
                    "exported_count": len(training_data),
                },
            )
        except Exception:
            logger.debug("Webhook fire failed for export_complete", exc_info=True)

    return training_data


def export_agent_trajectories(
    min_reward: float = 0.8,
    max_reward: float = 0.4,
    batch_size: int = 50,
    batch_id: str | None = None,
    db_session: Session | None = None,
) -> list[dict]:
    """
    Export agent trajectories for DPO-style training (Phase 9).

    High-reward episodes (>= min_reward) export as positive examples;
    low-reward (<= max_reward) as negative. Requires ``episodes`` rows.
    """
    if not db_session:
        return []

    from app.models.episode import Episode

    if not batch_id:
        batch_id = uuid.uuid4().hex[:12]

    high = (
        db_session.query(Episode)
        .filter(Episode.task_reward_score.isnot(None))
        .filter(Episode.task_reward_score >= min_reward)
        .order_by(Episode.created_at.asc())
        .limit(batch_size)
        .all()
    )
    low = (
        db_session.query(Episode)
        .filter(Episode.task_reward_score.isnot(None))
        .filter(Episode.task_reward_score <= max_reward)
        .order_by(Episode.created_at.asc())
        .limit(batch_size)
        .all()
    )

    out: list[dict] = []
    for ep in high + low:
        label = "chosen" if ep.task_reward_score and ep.task_reward_score >= min_reward else "rejected"
        messages = _trajectory_to_messages(ep)
        out.append(
            {
                "type": "trajectory",
                "messages": messages,
                "metadata": {
                    "trace_id": ep.trace_id,
                    "task_reward_score": ep.task_reward_score,
                    "outcome": ep.outcome,
                    "label": label,
                    "batch_id": batch_id,
                    "tool_sequence": ep.tool_sequence,
                    "step_count": ep.step_count,
                    "self_corrections": ep.self_corrections,
                },
            }
        )

    logger.info("Exported %d agent trajectory rows (batch=%s)", len(out), batch_id)
    return out


def _trajectory_to_messages(ep: Any) -> list[dict]:
    """Build multi-turn message list from an episode's stored trajectory."""
    msgs: list[dict] = [{"role": "user", "content": ep.task_summary}]
    traj = ep.agent_trajectory
    if isinstance(traj, list) and traj:
        for step in traj:
            if not isinstance(step, dict):
                continue
            kind = step.get("kind", "")
            if kind == "tool":
                tool_name = step.get("tool", "unknown")
                args = step.get("arguments", {})
                msgs.append(
                    {
                        "role": "assistant",
                        "content": f"Calling {tool_name}",
                        "tool_calls": [{"name": tool_name, "arguments": args}],
                    }
                )
                reflection = step.get("reflection", "")
                output = f"success={step.get('success')}"
                if reflection:
                    output += f" | reflection: {reflection[:500]}"
                msgs.append({"role": "tool", "content": output, "name": tool_name})
            elif kind == "final":
                msgs.append({"role": "assistant", "content": step.get("content", "")[:4000]})
    else:
        msgs.append({"role": "assistant", "content": (ep.reflection or "")[:4000]})
    return msgs


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
