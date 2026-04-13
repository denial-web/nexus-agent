"""Scheduled data retention — purge old rows to prevent unbounded growth."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)


def run_retention(db: Session) -> dict[str, int]:
    """Delete rows older than configured retention periods.

    Returns a dict of {table_name: rows_deleted}. Tables with retention
    set to 0 (the default) are skipped entirely.
    """
    results: dict[str, int] = {}
    now = datetime.now(UTC)

    if settings.RETENTION_TRACE_DAYS > 0:
        cutoff = now - timedelta(days=settings.RETENTION_TRACE_DAYS)
        results["traces"] = _purge_traces(db, cutoff)

    if settings.RETENTION_LABELING_DAYS > 0:
        cutoff = now - timedelta(days=settings.RETENTION_LABELING_DAYS)
        results["labeling_queue"] = _purge_labeling(db, cutoff)

    if settings.RETENTION_APPROVAL_DAYS > 0:
        cutoff = now - timedelta(days=settings.RETENTION_APPROVAL_DAYS)
        results["approval_requests"] = _purge_approvals(db, cutoff)
        results["approval_votes"] = _purge_approval_votes(db, cutoff)

    if settings.RETENTION_CALIBRATION_DAYS > 0:
        cutoff = now - timedelta(days=settings.RETENTION_CALIBRATION_DAYS)
        results["calibration_snapshots"] = _purge_calibration(db, cutoff)

    return results


_BATCH_SIZE = 500


def _purge_traces(db: Session, cutoff: datetime) -> int:
    from app.models.trace import Trace

    total = 0
    while True:
        ids = [
            r[0]
            for r in db.query(Trace.id)
            .filter(Trace.created_at < cutoff)
            .limit(_BATCH_SIZE)
            .all()
        ]
        if not ids:
            break
        db.query(Trace).filter(Trace.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        total += len(ids)
    return total


def _purge_labeling(db: Session, cutoff: datetime) -> int:
    from app.models.labeling_queue import LabelingItem

    total = 0
    while True:
        ids = [
            r[0]
            for r in db.query(LabelingItem.id)
            .filter(LabelingItem.created_at < cutoff, LabelingItem.status.in_(("exported", "trained")))
            .limit(_BATCH_SIZE)
            .all()
        ]
        if not ids:
            break
        db.query(LabelingItem).filter(LabelingItem.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        total += len(ids)
    return total


def _purge_approvals(db: Session, cutoff: datetime) -> int:
    from app.models.approval_log import ApprovalRequest

    total = 0
    while True:
        ids = [
            r[0]
            for r in db.query(ApprovalRequest.id)
            .filter(ApprovalRequest.created_at < cutoff, ApprovalRequest.status.in_(("approved", "denied", "expired")))
            .limit(_BATCH_SIZE)
            .all()
        ]
        if not ids:
            break
        db.query(ApprovalRequest).filter(ApprovalRequest.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        total += len(ids)
    return total


def _purge_approval_votes(db: Session, cutoff: datetime) -> int:
    from app.models.approval_log import ApprovalVote

    total = 0
    while True:
        ids = [
            r[0]
            for r in db.query(ApprovalVote.id)
            .filter(ApprovalVote.created_at < cutoff)
            .limit(_BATCH_SIZE)
            .all()
        ]
        if not ids:
            break
        db.query(ApprovalVote).filter(ApprovalVote.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        total += len(ids)
    return total


def _purge_calibration(db: Session, cutoff: datetime) -> int:
    from app.models.training_meta import CalibrationSnapshot

    total = 0
    while True:
        ids = [
            r[0]
            for r in db.query(CalibrationSnapshot.id)
            .filter(CalibrationSnapshot.recorded_at < cutoff)
            .limit(_BATCH_SIZE)
            .all()
        ]
        if not ids:
            break
        db.query(CalibrationSnapshot).filter(CalibrationSnapshot.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        total += len(ids)
    return total
