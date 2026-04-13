"""
ECE (Expected Calibration Error) tracker for critic confidence calibration.

After each critic evaluation, we record the predicted confidence vs the
actual outcome. Over a rolling window we compute ECE — a measure of how
well-calibrated the critic's scores are.

ECE = Σ (|bin_count|/N) * |avg_confidence - avg_accuracy| per bin

High ECE means the critic is over- or under-confident and needs recalibration.
"""

import logging
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_DEFAULT_NUM_BINS = 10
_DEFAULT_WINDOW_SECONDS = 3600.0
_DEFAULT_ECE_THRESHOLD = 0.15


@dataclass
class CalibrationRecord:
    predicted_confidence: float
    actual_correct: bool
    node_name: str
    trace_id: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class CalibrationReport:
    ece: float
    num_samples: int
    bins: list[dict]
    needs_recalibration: bool
    per_node_ece: dict[str, float]


class ECETracker:
    """
    Rolling-window Expected Calibration Error tracker.

    Records (predicted_confidence, actual_correct) observations and computes
    ECE over a configurable time window.
    """

    def __init__(
        self,
        num_bins: int = _DEFAULT_NUM_BINS,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        ece_threshold: float = _DEFAULT_ECE_THRESHOLD,
    ):
        self._num_bins = num_bins
        self._window_seconds = window_seconds
        self._ece_threshold = ece_threshold
        self._records: list[CalibrationRecord] = []
        self._lock = threading.Lock()

    def record(
        self,
        predicted_confidence: float,
        actual_correct: bool,
        node_name: str,
        trace_id: str,
    ) -> None:
        with self._lock:
            self._records.append(
                CalibrationRecord(
                    predicted_confidence=max(0.0, min(1.0, predicted_confidence)),
                    actual_correct=actual_correct,
                    node_name=node_name,
                    trace_id=trace_id,
                )
            )

    def compute_ece(self, node_name: str | None = None) -> CalibrationReport:
        with self._lock:
            cutoff = time.time() - self._window_seconds
            active = [r for r in self._records if r.timestamp >= cutoff]
            self._records = active

        if node_name:
            active = [r for r in active if r.node_name == node_name]

        if not active:
            return CalibrationReport(
                ece=0.0,
                num_samples=0,
                bins=[],
                needs_recalibration=False,
                per_node_ece={},
            )

        bins = self._bin_records(active)
        ece = self._ece_from_bins(bins, len(active))

        nodes = {r.node_name for r in active}
        per_node: dict[str, float] = {}
        for n in nodes:
            node_recs = [r for r in active if r.node_name == n]
            node_bins = self._bin_records(node_recs)
            per_node[n] = self._ece_from_bins(node_bins, len(node_recs))

        return CalibrationReport(
            ece=round(ece, 6),
            num_samples=len(active),
            bins=bins,
            needs_recalibration=ece > self._ece_threshold,
            per_node_ece={k: round(v, 6) for k, v in per_node.items()},
        )

    def _bin_records(self, records: list[CalibrationRecord]) -> list[dict]:
        bin_width = 1.0 / self._num_bins
        bins: list[dict] = []

        for i in range(self._num_bins):
            lo = i * bin_width
            hi = lo + bin_width
            in_bin = [
                r
                for r in records
                if lo <= r.predicted_confidence < hi or (i == self._num_bins - 1 and r.predicted_confidence == 1.0)
            ]
            if not in_bin:
                bins.append(
                    {
                        "bin_lo": round(lo, 2),
                        "bin_hi": round(hi, 2),
                        "count": 0,
                        "avg_confidence": 0.0,
                        "avg_accuracy": 0.0,
                        "gap": 0.0,
                    }
                )
                continue

            avg_conf = sum(r.predicted_confidence for r in in_bin) / len(in_bin)
            avg_acc = sum(1.0 for r in in_bin if r.actual_correct) / len(in_bin)
            bins.append(
                {
                    "bin_lo": round(lo, 2),
                    "bin_hi": round(hi, 2),
                    "count": len(in_bin),
                    "avg_confidence": round(avg_conf, 4),
                    "avg_accuracy": round(avg_acc, 4),
                    "gap": round(abs(avg_conf - avg_acc), 4),
                }
            )

        return bins

    @staticmethod
    def _ece_from_bins(bins: list[dict], total: int) -> float:
        if total == 0:
            return 0.0
        return sum(b["count"] / total * b["gap"] for b in bins)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._records)


_tracker = ECETracker()


def get_ece_tracker() -> ECETracker:
    return _tracker


def persist_calibration_snapshot(db_session: Session) -> str | None:
    """
    Persist the current in-memory ECE report to calibration_snapshots for durability.
    Returns the new snapshot id, or None if no samples.
    """
    from app.models.training_meta import CalibrationSnapshot

    report = _tracker.compute_ece()
    if report.num_samples == 0:
        return None

    snap = CalibrationSnapshot(
        ece=report.ece,
        num_samples=report.num_samples,
        needs_recalibration=report.needs_recalibration,
        per_node_ece=report.per_node_ece,
        bins=report.bins,
    )
    db_session.add(snap)
    db_session.commit()
    db_session.refresh(snap)
    logger.info("Persisted calibration snapshot id=%s ece=%.4f n=%d", snap.id, report.ece, report.num_samples)
    return snap.id


def record_critic_calibration(
    critic_scores: dict,
    actual_verdict: str,
    trace_id: str,
) -> None:
    """
    Record calibration data from a critic evaluation.

    Extracts each node's score and compares it against the actual outcome
    (pass = correct, halt/rollback = incorrect for the model).
    """
    actual_correct = actual_verdict == "pass"

    for node_name, score_data in critic_scores.items():
        confidence = score_data.get("score", 0.0) if isinstance(score_data, dict) else 0.0
        _tracker.record(
            predicted_confidence=confidence,
            actual_correct=actual_correct,
            node_name=node_name,
            trace_id=trace_id,
        )
