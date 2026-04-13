"""Training flywheel API — labeling, export, eval, fine-tuning, and calibration endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/training", tags=["Training"])


class LabelRequest(BaseModel):
    label: str
    reviewer_id: str
    corrected_response: str | None = None
    reviewer_notes: str | None = None


class ExportRequest(BaseModel):
    batch_size: int = 100
    dataset_type: str = "agent_safety"
    send_to_doctrine_lab: bool = True
    enrich_evidential: bool = False


class EvalReportRequest(BaseModel):
    model_id: str
    eval_type: str = "agent_safety"
    metrics: dict


class FinetuneRequest(BaseModel):
    model_id: str | None = None
    dataset_type: str = "agent_safety"
    batch_ids: list[str] | None = None


class LoraCompareRequest(BaseModel):
    node_id: str
    new_lora_path: str
    test_trace_ids: list[str]


class PromoteAdapterRequest(BaseModel):
    job_id: str
    node_name: str


@router.get("/queue")
def get_labeling_queue(
    status: str = "pending",
    failure_type: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """View the labeling queue for training flywheel."""
    from app.core.training.labeler import get_queue

    items = get_queue(status=status, failure_type=failure_type, db_session=db)
    return {"items": items, "count": len(items)}


@router.post("/queue/{item_id}/label")
def label_queue_item(item_id: str, req: LabelRequest, db: Session = Depends(get_db)) -> dict:
    """Apply a human label to a queued failure trace."""
    from app.core.training.labeler import label_item

    result = label_item(
        item_id=item_id,
        label=req.label,
        reviewer_id=req.reviewer_id,
        corrected_response=req.corrected_response,
        reviewer_notes=req.reviewer_notes,
        db_session=db,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Labeling queue item not found")
    return result


@router.post("/export")
def export_and_send(req: ExportRequest, db: Session = Depends(get_db)) -> dict:
    """Export labeled items and optionally send to Doctrine Lab."""
    from app.core.training.labeler import export_for_training
    from app.models.labeling_queue import LabelingItem
    from app.services.doctrine_bridge import (
        compute_batch_id,
        import_dataset,
        is_configured,
    )

    pending_items = db.query(LabelingItem).filter_by(status="labeled", label="correct_flag").limit(req.batch_size).all()
    if not pending_items:
        return {"exported": 0, "batch_id": None, "doctrine_lab": None}

    trace_ids = [item.trace_id for item in pending_items]
    batch_id = compute_batch_id(trace_ids)

    items = export_for_training(
        batch_size=req.batch_size,
        batch_id=batch_id,
        enrich_evidential=req.enrich_evidential,
        db_session=db,
    )

    doctrine_result = None
    if req.send_to_doctrine_lab and is_configured():
        try:
            doctrine_result = import_dataset(
                training_items=items,
                batch_id=batch_id,
                dataset_type=req.dataset_type,
            )
        except Exception as exc:
            logger.warning("Failed to send to Doctrine Lab", exc_info=True)
            doctrine_result = {"error": str(exc)}
            try:
                from app.core.training.outbox import enqueue_failed_import

                enqueue_failed_import(
                    db,
                    batch_id=batch_id,
                    dataset_type=req.dataset_type,
                    items=items,
                    error_message=str(exc),
                )
            except Exception:
                logger.exception("Could not enqueue Doctrine Lab retry")

    return {
        "exported": len(items),
        "batch_id": batch_id,
        "doctrine_lab": doctrine_result,
    }


@router.post("/eval")
def submit_eval(req: EvalReportRequest, db: Session = Depends(get_db)) -> dict:
    """Submit an evaluation report to Doctrine Lab."""
    from app.services.doctrine_bridge import is_configured, submit_eval_report

    if not is_configured():
        raise HTTPException(status_code=503, detail="Doctrine Lab not configured")

    report = {
        "model_id": req.model_id,
        "eval_type": req.eval_type,
        "metrics": req.metrics,
    }
    try:
        result = submit_eval_report(report)
    except Exception as exc:
        logger.warning("Eval report failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result


@router.post("/finetune")
def trigger_finetune_endpoint(req: FinetuneRequest) -> dict:
    """Trigger a fine-tuning job via Doctrine Lab."""
    from app.services.doctrine_bridge import is_configured, trigger_finetune

    if not is_configured():
        raise HTTPException(status_code=503, detail="Doctrine Lab not configured")

    try:
        result = trigger_finetune(
            model_id=req.model_id,
            dataset_type=req.dataset_type,
            batch_ids=req.batch_ids,
        )
    except Exception as exc:
        logger.warning("Finetune trigger failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result


@router.get("/finetune/status/{job_id}")
def finetune_job_status(job_id: str) -> dict:
    """Poll Doctrine Lab for fine-tune job status."""
    from app.services.doctrine_bridge import get_finetune_job_status, is_configured

    if not is_configured():
        raise HTTPException(status_code=503, detail="Doctrine Lab not configured")
    try:
        return get_finetune_job_status(job_id)
    except Exception as exc:
        logger.warning("Finetune status failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/promote-adapter")
def promote_adapter_endpoint(req: PromoteAdapterRequest, db: Session = Depends(get_db)) -> dict:
    """
    After a fine-tune job completes, promote the resulting LoRA path to a critic node.

    Expects Doctrine Lab job payload to include adapter_path, lora_adapter_path, or output_path.
    """
    from app.agent.pipeline import invalidate_arbiter_cache
    from app.models.critic_registry import CriticNode
    from app.services.doctrine_bridge import get_finetune_job_status, is_configured

    if not is_configured():
        raise HTTPException(status_code=503, detail="Doctrine Lab not configured")

    try:
        status = get_finetune_job_status(req.job_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if status.get("skipped"):
        raise HTTPException(status_code=503, detail="Doctrine Lab not configured")

    job_status = (status.get("status") or status.get("state") or "").lower()
    if job_status not in ("succeeded", "completed", "success"):
        raise HTTPException(
            status_code=400,
            detail=f"Job not in a promotable state: {job_status or status}",
        )

    adapter = status.get("adapter_path") or status.get("lora_adapter_path") or status.get("output_path")
    if not adapter:
        raise HTTPException(status_code=400, detail="No adapter path in job response")

    node = db.query(CriticNode).filter_by(name=req.node_name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Critic node not found")

    node.lora_adapter_path = adapter
    db.commit()
    invalidate_arbiter_cache()
    return {"promoted": True, "node_name": req.node_name, "lora_adapter_path": adapter}


@router.post("/calibration/persist")
def persist_calibration(db: Session = Depends(get_db)) -> dict:
    """Write the current in-memory ECE metrics to calibration_snapshots."""
    from app.core.training.calibration import persist_calibration_snapshot

    snap_id = persist_calibration_snapshot(db)
    if snap_id is None:
        raise HTTPException(status_code=400, detail="No calibration samples to persist")
    return {"snapshot_id": snap_id}


@router.get("/calibration/snapshots")
def list_calibration_snapshots(limit: int = 20, db: Session = Depends(get_db)) -> dict:
    """Recent persisted calibration snapshots."""
    from app.models.training_meta import CalibrationSnapshot

    rows = db.query(CalibrationSnapshot).order_by(CalibrationSnapshot.recorded_at.desc()).limit(min(limit, 100)).all()
    return {
        "snapshots": [
            {
                "id": r.id,
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
                "ece": r.ece,
                "num_samples": r.num_samples,
                "needs_recalibration": r.needs_recalibration,
                "per_node_ece": r.per_node_ece,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/calibration")
def get_calibration(node_name: str | None = None) -> dict:
    """Get ECE calibration report for the critic tree."""
    from app.core.training.calibration import get_ece_tracker

    tracker = get_ece_tracker()
    report = tracker.compute_ece(node_name=node_name)
    return {
        "ece": report.ece,
        "num_samples": report.num_samples,
        "needs_recalibration": report.needs_recalibration,
        "per_node_ece": report.per_node_ece,
        "bins": report.bins,
    }


@router.post("/lora/compare")
def compare_lora_adapter(req: LoraCompareRequest, db: Session = Depends(get_db)) -> dict:
    """
    Compare critic performance before and after swapping a LoRA adapter.

    Re-evaluates the given traces with the current adapter, then with the new
    adapter, and returns a side-by-side comparison.
    """
    from app.agent.pipeline import invalidate_arbiter_cache
    from app.models.critic_registry import CriticNode
    from app.services.replay import re_evaluate_trace

    node = db.query(CriticNode).filter_by(id=req.node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Critic node not found")

    before_results = []
    for tid in req.test_trace_ids:
        r = re_evaluate_trace(tid, db)
        if r:
            before_results.append(r)

    old_path = node.lora_adapter_path
    node.lora_adapter_path = req.new_lora_path
    db.commit()
    invalidate_arbiter_cache()

    after_results = []
    try:
        for tid in req.test_trace_ids:
            r = re_evaluate_trace(tid, db)
            if r:
                after_results.append(r)
    finally:
        node.lora_adapter_path = old_path
        db.commit()
        invalidate_arbiter_cache()

    def _summarize(results: list[dict]) -> dict:
        if not results:
            return {"count": 0}
        verdicts = [r.get("new_verdict") or "unknown" for r in results]
        return {
            "count": len(results),
            "pass_rate": round(verdicts.count("pass") / len(verdicts), 4),
            "halt_count": verdicts.count("halt"),
            "rollback_count": verdicts.count("rollback"),
        }

    return {
        "node_id": req.node_id,
        "node_name": node.name,
        "old_lora_path": old_path,
        "new_lora_path": req.new_lora_path,
        "traces_evaluated": len(req.test_trace_ids),
        "before": _summarize(before_results),
        "after": _summarize(after_results),
        "before_details": before_results,
        "after_details": after_results,
    }
