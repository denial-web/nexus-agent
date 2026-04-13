"""Tests for data retention purge logic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.models.approval_log import ApprovalRequest, ApprovalVote
from app.models.labeling_queue import LabelingItem
from app.models.trace import Trace
from app.models.training_meta import CalibrationSnapshot
from app.services.retention import run_retention


def _old_ts(days: int = 100) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


class TestRetention:
    def test_no_purge_when_retention_disabled(self, db_session):
        db_session.add(
            Trace(
                id="ret-nopurge-1",
                session_id="ret-s1",
                prompt="hi",
                prompt_hash="abc",
                immune_verdict="pass",
                status="completed",
                created_at=_old_ts(365),
            )
        )
        db_session.commit()

        with patch("app.services.retention.settings") as mock_settings:
            mock_settings.RETENTION_TRACE_DAYS = 0
            mock_settings.RETENTION_LABELING_DAYS = 0
            mock_settings.RETENTION_APPROVAL_DAYS = 0
            mock_settings.RETENTION_CALIBRATION_DAYS = 0
            results = run_retention(db_session)

        assert results == {}
        assert db_session.query(Trace).filter_by(id="ret-nopurge-1").first() is not None

    def test_purge_old_traces(self, db_session):
        db_session.add(
            Trace(
                id="ret-old-trace",
                session_id="ret-s2",
                prompt="hi",
                prompt_hash="abc",
                immune_verdict="pass",
                status="completed",
                created_at=_old_ts(100),
            )
        )
        db_session.add(
            Trace(
                id="ret-new-trace",
                session_id="ret-s2",
                prompt="hi",
                prompt_hash="def",
                immune_verdict="pass",
                status="completed",
                created_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        with patch("app.services.retention.settings") as mock_settings:
            mock_settings.RETENTION_TRACE_DAYS = 90
            mock_settings.RETENTION_LABELING_DAYS = 0
            mock_settings.RETENTION_APPROVAL_DAYS = 0
            mock_settings.RETENTION_CALIBRATION_DAYS = 0
            results = run_retention(db_session)

        assert results["traces"] >= 1
        assert db_session.query(Trace).filter_by(id="ret-old-trace").first() is None
        assert db_session.query(Trace).filter_by(id="ret-new-trace").first() is not None

    def test_purge_old_labeling_only_exported(self, db_session):
        db_session.add(
            LabelingItem(
                id="exported-old",
                trace_id="t1",
                source_node="safety",
                failure_type="safety",
                prompt="test",
                critic_output={},
                status="exported",
                created_at=_old_ts(100),
            )
        )
        db_session.add(
            LabelingItem(
                id="pending-old",
                trace_id="t2",
                source_node="safety",
                failure_type="safety",
                prompt="test",
                critic_output={},
                status="pending",
                created_at=_old_ts(100),
            )
        )
        db_session.commit()

        with patch("app.services.retention.settings") as mock_settings:
            mock_settings.RETENTION_TRACE_DAYS = 0
            mock_settings.RETENTION_LABELING_DAYS = 90
            mock_settings.RETENTION_APPROVAL_DAYS = 0
            mock_settings.RETENTION_CALIBRATION_DAYS = 0
            results = run_retention(db_session)

        assert results["labeling_queue"] == 1
        assert db_session.query(LabelingItem).filter_by(id="exported-old").first() is None
        assert db_session.query(LabelingItem).filter_by(id="pending-old").first() is not None

    def test_purge_old_approvals(self, db_session):
        db_session.add(
            ApprovalRequest(
                id="approved-old",
                trace_id="t1",
                action_type="test",
                action_payload={},
                risk_level="low",
                required_approvals="1",
                received_approvals="1",
                status="approved",
                created_at=_old_ts(100),
            )
        )
        db_session.add(
            ApprovalRequest(
                id="pending-old",
                trace_id="t2",
                action_type="test",
                action_payload={},
                risk_level="low",
                required_approvals="1",
                received_approvals="0",
                status="pending",
                created_at=_old_ts(100),
            )
        )
        db_session.commit()

        with patch("app.services.retention.settings") as mock_settings:
            mock_settings.RETENTION_TRACE_DAYS = 0
            mock_settings.RETENTION_LABELING_DAYS = 0
            mock_settings.RETENTION_APPROVAL_DAYS = 90
            mock_settings.RETENTION_CALIBRATION_DAYS = 0
            results = run_retention(db_session)

        assert results["approval_requests"] == 1
        assert db_session.query(ApprovalRequest).filter_by(id="approved-old").first() is None
        assert db_session.query(ApprovalRequest).filter_by(id="pending-old").first() is not None

    def test_purge_old_calibration_snapshots(self, db_session):
        db_session.add(
            CalibrationSnapshot(
                id="old-snap",
                ece=0.1,
                num_samples=100,
                recorded_at=_old_ts(200),
            )
        )
        db_session.add(
            CalibrationSnapshot(
                id="new-snap",
                ece=0.05,
                num_samples=200,
                recorded_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        with patch("app.services.retention.settings") as mock_settings:
            mock_settings.RETENTION_TRACE_DAYS = 0
            mock_settings.RETENTION_LABELING_DAYS = 0
            mock_settings.RETENTION_APPROVAL_DAYS = 0
            mock_settings.RETENTION_CALIBRATION_DAYS = 90
            results = run_retention(db_session)

        assert results["calibration_snapshots"] == 1
        assert db_session.query(CalibrationSnapshot).filter_by(id="old-snap").first() is None
        assert db_session.query(CalibrationSnapshot).filter_by(id="new-snap").first() is not None
