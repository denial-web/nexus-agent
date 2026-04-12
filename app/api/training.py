"""Training flywheel API — labeling, export, eval, fine-tuning, and calibration endpoints."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/training", tags=["Training"])


class LabelRequest(BaseModel):
    label: str
    reviewer_id: str
    corrected_response: Optional[str] = None
    reviewer_notes: Optional[str] = None


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
    model_id: Optional[str] = None
    dataset_type: str = "agent_safety"
    batch_ids: Optional[list[str]] = None


class LoraCompareRequest(BaseModel):
    node_id: str
    new_lora_path: str
    test_trace_ids: list[str]


@router.get("/queue")
def get_labeling_queue(
    status: str = "pending",
    failure_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """View the labeling queue for training flywheel."""
    from app.core.training.labeler import get_queue

    items = get_queue(status=status, failure_type=failure_type, db_session=db)
    return {"items": items, "count": len(items)}


@router.post("/queue/{item_id}/label")
def label_queue_item(item_id: str, req: LabelRequest, db: Session = Depends(get_db)):
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
def export_and_send(req: ExportRequest, db: Session = Depends(get_db)):
    """Export labeled items and optionally send to Doctrine Lab."""
    from app.core.training.labeler import export_for_training
    from app.models.labeling_queue import LabelingItem
    from app.services.doctrine_bridge import (
        compute_batch_id,
        import_dataset,
        is_configured,
    )

    pending_items = (
        db.query(LabelingItem)
        .filter_by(status="labeled", label="correct_flag")
        .limit(req.batch_size)
        .all()
    )
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
        except Exception:
            logger.warning("Failed to send to Doctrine Lab", exc_info=True)
            doctrine_result = {"error": "Failed to send to Doctrine Lab"}

    return {
        "exported": len(items),
        "batch_id": batch_id,
        "doctrine_lab": doctrine_result,
    }


@router.post("/eval")
def submit_eval(req: EvalReportRequest, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=502, detail=str(exc))

    return result


@router.post("/finetune")
def trigger_finetune_endpoint(req: FinetuneRequest):
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
        raise HTTPException(status_code=502, detail=str(exc))

    return result


@router.get("/calibration")
def get_calibration(node_name: Optional[str] = None):
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
def compare_lora_adapter(req: LoraCompareRequest, db: Session = Depends(get_db)):
    """
    Compare critic performance before and after swapping a LoRA adapter.

    Re-evaluates the given traces with the current adapter, then with the new
    adapter, and returns a side-by-side comparison.
    """
    from app.models.critic_registry import CriticNode
    from app.services.replay import re_evaluate_trace
    from app.agent.pipeline import invalidate_arbiter_cache

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
