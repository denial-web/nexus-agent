"""
Scheduled training export — background job that periodically checks the
labeling queue and auto-exports reviewed items.

Runs in a daemon thread started from the FastAPI lifespan.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 300
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _run_export_cycle() -> dict:
    from app.core.training.labeler import export_for_training
    from app.core.training.outbox import enqueue_failed_import, process_outbox_retries
    from app.db import SessionLocal
    from app.models.labeling_queue import LabelingItem
    from app.services.doctrine_bridge import compute_batch_id, import_dataset, is_configured

    db = SessionLocal()
    try:
        from app.metrics import LABELING_QUEUE_DEPTH

        pending_count = db.query(LabelingItem).filter_by(status="pending").count()
        LABELING_QUEUE_DEPTH.set(pending_count)

        outbox_stats = process_outbox_retries(db)

        pending = (
            db.query(LabelingItem)
            .filter_by(status="labeled", label="correct_flag")
            .order_by(LabelingItem.created_at.asc(), LabelingItem.id.asc())
            .limit(100)
            .all()
        )
        if not pending:
            from app.core.training.calibration import persist_calibration_snapshot

            snap_id = None
            try:
                snap_id = persist_calibration_snapshot(db)
            except Exception:
                logger.debug("Calibration snapshot skipped", exc_info=True)
            return {"exported": 0, "outbox_retries": outbox_stats, "calibration_snapshot_id": snap_id}

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
            except Exception as exc:
                logger.warning("Scheduled export: Doctrine Lab send failed", exc_info=True)
                try:
                    enqueue_failed_import(
                        db,
                        batch_id=batch_id,
                        dataset_type="agent_safety",
                        items=items,
                        error_message=str(exc),
                    )
                except Exception:
                    logger.exception("Failed to enqueue Doctrine retry")

        from app.core.training.calibration import persist_calibration_snapshot

        snap_id = None
        try:
            snap_id = persist_calibration_snapshot(db)
        except Exception:
            logger.debug("Calibration snapshot skipped or failed", exc_info=True)

        return {
            "exported": len(items),
            "batch_id": batch_id,
            "doctrine_lab": doctrine_result,
            "outbox_retries": outbox_stats,
            "calibration_snapshot_id": snap_id,
        }
    finally:
        db.close()


_RETENTION_INTERVAL_CYCLES = 12


def _run_retention_cycle() -> None:
    from app.db import SessionLocal
    from app.services.retention import run_retention

    db = SessionLocal()
    try:
        results = run_retention(db)
        if any(v > 0 for v in results.values()):
            logger.info("Retention purge: %s", results)
    except Exception:
        logger.exception("Retention cycle failed")
    finally:
        db.close()


def _scheduler_loop(interval: float) -> None:
    logger.info("Training scheduler started (interval=%ds)", interval)
    cycle_count = 0
    while not _stop_event.is_set():
        _stop_event.wait(interval)
        if _stop_event.is_set():
            break
        cycle_count += 1
        try:
            result = _run_export_cycle()
            if result.get("exported", 0) > 0:
                logger.info("Scheduled export: %s", result)
        except Exception:
            logger.exception("Scheduled export cycle failed")

        if cycle_count % _RETENTION_INTERVAL_CYCLES == 0:
            _run_retention_cycle()

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
