"""
Scheduled training export — background job that periodically checks the
labeling queue and auto-exports reviewed items.

Runs in a daemon thread started from the FastAPI lifespan.
"""
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 300
_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def _run_export_cycle() -> dict:
    from app.db import SessionLocal
    from app.core.training.labeler import export_for_training
    from app.models.labeling_queue import LabelingItem
    from app.services.doctrine_bridge import compute_batch_id, import_dataset, is_configured

    db = SessionLocal()
    try:
        pending = (
            db.query(LabelingItem)
            .filter_by(status="labeled", label="correct_flag")
            .limit(100)
            .all()
        )
        if not pending:
            return {"exported": 0}

        trace_ids = [item.trace_id for item in pending]
        batch_id = compute_batch_id(trace_ids)

        items = export_for_training(
            batch_size=100,
            batch_id=batch_id,
            enrich_evidential=True,
            db_session=db,
        )

        doctrine_result = None
        if is_configured():
            try:
                doctrine_result = import_dataset(
                    training_items=items,
                    batch_id=batch_id,
                )
            except Exception:
                logger.warning("Scheduled export: Doctrine Lab send failed", exc_info=True)

        return {
            "exported": len(items),
            "batch_id": batch_id,
            "doctrine_lab": doctrine_result,
        }
    finally:
        db.close()


def _scheduler_loop(interval: float) -> None:
    logger.info("Training scheduler started (interval=%ds)", interval)
    while not _stop_event.is_set():
        _stop_event.wait(interval)
        if _stop_event.is_set():
            break
        try:
            result = _run_export_cycle()
            if result.get("exported", 0) > 0:
                logger.info("Scheduled export: %s", result)
        except Exception:
            logger.exception("Scheduled export cycle failed")
    logger.info("Training scheduler stopped")


def start_scheduler(interval_seconds: float = _DEFAULT_INTERVAL_SECONDS) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        logger.warning("Training scheduler already running")
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_scheduler_loop,
        args=(interval_seconds,),
        daemon=True,
        name="training-scheduler",
    )
    _thread.start()


def stop_scheduler() -> None:
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
        _thread = None


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
