"""Tests for advanced training features: ECE calibration, evidential loss,
LoRA comparison, and scheduled export."""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.core.training.calibration import (
    ECETracker,
    get_ece_tracker,
    record_critic_calibration,
)
from app.core.training.evidential import (
    _compute_adjustment,
    _compute_sample_weight,
    enrich_training_item,
)
from app.core.training.scheduler import (
    _run_export_cycle,
    is_running,
    start_scheduler,
    stop_scheduler,
)


@pytest.fixture(autouse=True)
def _reset_ece():
    get_ece_tracker().clear()
    yield
    get_ece_tracker().clear()


class TestECETracker:
    def test_empty_tracker(self):
        tracker = ECETracker()
        report = tracker.compute_ece()
        assert report.ece == 0.0
        assert report.num_samples == 0
        assert report.needs_recalibration is False

    def test_perfectly_calibrated(self):
        tracker = ECETracker(num_bins=10)
        for i in range(100):
            conf = (i % 10) / 10.0 + 0.05
            correct = conf > 0.5
            tracker.record(conf, correct, "reasoning", f"t{i}")
        report = tracker.compute_ece()
        assert report.num_samples == 100
        assert isinstance(report.ece, float)
        assert len(report.bins) == 10

    def test_miscalibrated_high_ece(self):
        tracker = ECETracker(num_bins=5, ece_threshold=0.1)
        for i in range(50):
            tracker.record(0.95, False, "safety", f"t{i}")
        report = tracker.compute_ece()
        assert report.ece > 0.1
        assert report.needs_recalibration is True

    def test_per_node_ece(self):
        tracker = ECETracker(num_bins=5)
        for i in range(20):
            tracker.record(0.9, True, "reasoning", f"t{i}")
        for i in range(20):
            tracker.record(0.9, False, "safety", f"t{i + 20}")
        report = tracker.compute_ece()
        assert "reasoning" in report.per_node_ece
        assert "safety" in report.per_node_ece
        assert report.per_node_ece["safety"] > report.per_node_ece["reasoning"]

    def test_node_filter(self):
        tracker = ECETracker()
        tracker.record(0.8, True, "reasoning", "t1")
        tracker.record(0.3, False, "safety", "t2")
        report = tracker.compute_ece(node_name="reasoning")
        assert report.num_samples == 1

    def test_window_expiry(self):
        tracker = ECETracker(window_seconds=0.01)
        tracker.record(0.5, True, "test", "t1")
        time.sleep(0.05)
        report = tracker.compute_ece()
        assert report.num_samples == 0

    def test_clear(self):
        tracker = ECETracker()
        tracker.record(0.5, True, "test", "t1")
        assert tracker.record_count == 1
        tracker.clear()
        assert tracker.record_count == 0


class TestRecordCriticCalibration:
    def test_records_from_scores(self):
        tracker = get_ece_tracker()
        scores = {
            "reasoning": {"score": 0.85, "verdict": "pass"},
            "safety": {"score": 0.92, "verdict": "pass"},
        }
        record_critic_calibration(scores, "pass", "trace-1")
        assert tracker.record_count == 2

    def test_halt_marked_incorrect(self):
        tracker = get_ece_tracker()
        scores = {"safety": {"score": 0.2, "verdict": "fail"}}
        record_critic_calibration(scores, "halt", "trace-2")
        report = tracker.compute_ece()
        assert report.num_samples == 1
        bin_with_data = [b for b in report.bins if b["count"] > 0]
        assert bin_with_data[0]["avg_accuracy"] == 0.0


class TestEvidentialLoss:
    def test_compute_adjustment(self):
        assert _compute_adjustment(0.0) == 1.0
        assert _compute_adjustment(0.5) == 0.5
        assert _compute_adjustment(1.0) == 0.1

    def test_sample_weight_default(self):
        weight = _compute_sample_weight({})
        assert weight == 1.0

    def test_sample_weight_with_rollbacks(self):
        weight = _compute_sample_weight({"critic_rollback_count": 3})
        assert weight < 1.0

    def test_sample_weight_high_confidence(self):
        weight = _compute_sample_weight({"asflc_confidence": 0.95})
        assert weight > 0.9

    def test_sample_weight_low_confidence(self):
        weight = _compute_sample_weight({"asflc_confidence": 0.2})
        assert weight < 0.7

    def test_enrich_training_item(self):
        item = {
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {"trace_id": "t1"},
        }
        trace = MagicMock()
        trace.asflc_confidence = 0.85
        trace.asflc_result = {"chain_regret": 0.1, "converged": True}
        trace.critic_scores = {
            "reasoning": {"score": 0.9},
            "safety": {"score": 0.8},
        }
        trace.critic_verdict = "pass"
        trace.critic_rollback_count = 0

        enriched = enrich_training_item(item, trace, calibration_ece=0.05)
        ev = enriched["evidential"]

        assert ev["asflc_confidence"] == 0.85
        assert ev["chain_regret"] == 0.1
        assert ev["critic_mean_score"] == 0.85
        assert ev["calibration_ece"] == 0.05
        assert ev["suggested_weight"] > 0

    def test_enrich_missing_asflc(self):
        item = {"messages": [], "metadata": {}}
        trace = MagicMock()
        trace.asflc_confidence = None
        trace.asflc_result = None
        trace.critic_scores = None
        trace.critic_verdict = "pass"
        trace.critic_rollback_count = 0

        enriched = enrich_training_item(item, trace)
        ev = enriched["evidential"]

        assert "asflc_confidence" not in ev
        assert ev["critic_verdict"] == "pass"
        assert ev["suggested_weight"] > 0


class TestLoraCompare:
    def test_compare_endpoint(self, client, db_session):
        from app.models.critic_registry import CriticNode

        node = db_session.query(CriticNode).filter_by(name="reasoning").first()
        if not node:
            pytest.skip("No reasoning node in registry")

        from app.agent.pipeline import run

        r = run("What is 2+2?", db_session=db_session)
        trace_id = r.trace_id

        resp = client.post("/api/training/lora/compare", json={
            "node_id": node.id,
            "new_lora_path": "/tmp/test_adapter.bin",
            "test_trace_ids": [trace_id],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_name"] == "reasoning"
        assert data["old_lora_path"] is None or isinstance(data["old_lora_path"], str)
        assert "before" in data
        assert "after" in data
        assert data["before"]["count"] == 1

    def test_compare_node_not_found(self, client):
        resp = client.post("/api/training/lora/compare", json={
            "node_id": "nonexistent",
            "new_lora_path": "/tmp/test.bin",
            "test_trace_ids": ["t1"],
        })
        assert resp.status_code == 404


class TestCalibrationEndpoint:
    def test_calibration_empty(self, client):
        resp = client.get("/api/training/calibration")
        assert resp.status_code == 200
        data = resp.json()
        assert data["num_samples"] == 0
        assert data["needs_recalibration"] is False

    def test_calibration_with_data(self, client, db_session):
        from app.agent.pipeline import run

        run("What is the capital of France?", db_session=db_session)
        resp = client.get("/api/training/calibration")
        assert resp.status_code == 200
        data = resp.json()
        assert data["num_samples"] > 0


class TestScheduler:
    def test_start_stop(self):
        start_scheduler(interval_seconds=1000)
        assert is_running() is True
        stop_scheduler()
        assert is_running() is False

    def test_double_start_safe(self):
        start_scheduler(interval_seconds=1000)
        start_scheduler(interval_seconds=1000)
        assert is_running() is True
        stop_scheduler()

    @patch("app.db.SessionLocal")
    def test_export_cycle_empty(self, mock_session_cls):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.limit.return_value.all.return_value = []
        result = _run_export_cycle()
        assert result["exported"] == 0


class TestExportWithEvidential:
    def test_export_enriches_items(self, db_session):
        from app.agent.pipeline import run
        from app.core.training.labeler import export_for_training, push_failure

        r = run(
            "Explain quantum mechanics in detail for a graduate student",
            db_session=db_session,
        )

        push_failure(
            trace_id=r.trace_id,
            source_node="reasoning",
            failure_type="reasoning",
            prompt="Explain quantum mechanics in detail for a graduate student",
            response=r.response,
            critic_output=r.critic_result,
            db_session=db_session,
        )

        from app.core.training.labeler import label_item
        from app.models.labeling_queue import LabelingItem

        item = db_session.query(LabelingItem).filter_by(trace_id=r.trace_id).first()
        label_item(
            item_id=item.id,
            label="correct_flag",
            reviewer_id="test",
            db_session=db_session,
        )

        exported = export_for_training(
            batch_size=10,
            enrich_evidential=True,
            db_session=db_session,
        )

        matching = [e for e in exported if e["metadata"]["trace_id"] == r.trace_id]
        assert len(matching) == 1
        assert "evidential" in matching[0]
        ev = matching[0]["evidential"]
        assert "suggested_weight" in ev
        assert ev["critic_verdict"] is not None
