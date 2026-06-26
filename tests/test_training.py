"""Tests for the training flywheel — labeling, export, and Doctrine Lab bridge."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from app.core.training.labeler import (
    ASFLC_CRITIC_SOURCE,
    ASFLC_FAILURE_TYPE,
    ASFLC_REVIEWER_ID,
    ASFLC_SOURCE_NODE,
    ASFLC_TRACE_ID_PREFIX,
    classify_labeling_item_origin,
    export_for_training,
    label_item,
    push_failure,
)
from app.services.doctrine_bridge import compute_batch_id, is_configured


def _labeling_item(**overrides):
    defaults = {
        "critic_output": {},
        "source_node": "immune",
        "failure_type": "injection",
        "reviewer_id": "reviewer-1",
        "trace_id": "runtime-trace-1",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestClassifyLabelingItemOrigin:
    def test_organic_runtime_default(self):
        assert classify_labeling_item_origin(_labeling_item()) == "organic"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"critic_output": {"source": ASFLC_CRITIC_SOURCE}},
            {"source_node": ASFLC_SOURCE_NODE},
            {"failure_type": ASFLC_FAILURE_TYPE},
            {"reviewer_id": ASFLC_REVIEWER_ID},
            {"trace_id": f"{ASFLC_TRACE_ID_PREFIX}deadbeef"},
        ],
    )
    def test_asflc_markers_classify_synthetic(self, overrides):
        assert classify_labeling_item_origin(_labeling_item(**overrides)) == "synthetic"


class TestBatchId:
    def test_deterministic(self):
        ids = ["trace-a", "trace-b", "trace-c"]
        assert compute_batch_id(ids) == compute_batch_id(ids)

    def test_order_independent(self):
        assert compute_batch_id(["a", "b"]) == compute_batch_id(["b", "a"])

    def test_different_ids_different_hash(self):
        assert compute_batch_id(["x"]) != compute_batch_id(["y"])


class TestIsConfigured:
    def test_not_configured_without_key(self):
        with patch("app.services.doctrine_bridge.settings") as mock_settings:
            mock_settings.DOCTRINE_LAB_URL = "http://localhost:8000"
            mock_settings.DOCTRINE_LAB_API_KEY = ""
            assert is_configured() is False

    def test_configured_with_both(self):
        with patch("app.services.doctrine_bridge.settings") as mock_settings:
            mock_settings.DOCTRINE_LAB_URL = "http://localhost:8000"
            mock_settings.DOCTRINE_LAB_API_KEY = "test-key"
            assert is_configured() is True


class TestDoctrineLabBridge:
    def test_import_dataset_skips_when_not_configured(self):
        from app.services.doctrine_bridge import import_dataset

        with patch("app.services.doctrine_bridge.is_configured", return_value=False):
            result = import_dataset([], "batch-1")
            assert result["skipped"] is True

    def test_import_dataset_posts_to_doctrine_lab(self):
        from app.services.doctrine_bridge import import_dataset

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"imported": 2}

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__ = lambda s: s
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value.post.return_value = mock_resp

            item = {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "do the risky thing"},
                    {"role": "assistant", "content": "I will not do that."},
                ],
                "metadata": {"trace_id": "t-1", "failure_type": "injection"},
            }
            result = import_dataset([item], "batch-x")
            assert result["imported"] == 2
            mock_client_cls.return_value.post.assert_called_once()

            # Lock the Doctrine Lab import contract: flat `entries`, not `items`.
            sent = mock_client_cls.return_value.post.call_args.kwargs["json"]
            assert "items" not in sent
            assert sent["batch_id"] == "batch-x"
            assert sent["category"] == "agent_safety"
            assert sent["origin"] == "organic"
            assert len(sent["entries"]) == 1
            entry = sent["entries"][0]
            assert entry["prompt"] == "do the risky thing"
            assert entry["response"] == "I will not do that."
            assert entry["failure_type"] == "injection"
            assert entry["trace_id"] == "t-1"
            assert "origin" not in entry

    def test_import_dataset_passes_per_entry_origin(self):
        from app.services.doctrine_bridge import import_dataset

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"imported": 2}

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__ = lambda s: s
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value.post.return_value = mock_resp

            items = [
                {
                    "messages": [
                        {"role": "user", "content": "asflc prompt"},
                        {"role": "assistant", "content": "asflc response"},
                    ],
                    "metadata": {
                        "trace_id": "asflc-import-abc",
                        "failure_type": "golden_example",
                        "origin": "synthetic",
                    },
                },
                {
                    "messages": [
                        {"role": "user", "content": "live prompt"},
                        {"role": "assistant", "content": "live response"},
                    ],
                    "metadata": {
                        "trace_id": "runtime-trace-1",
                        "failure_type": "injection",
                        "origin": "organic",
                    },
                },
            ]
            import_dataset(items, "batch-mixed")
            sent = mock_client_cls.return_value.post.call_args.kwargs["json"]
            assert sent["origin"] == "organic"
            assert sent["entries"][0]["origin"] == "synthetic"
            assert sent["entries"][1]["origin"] == "organic"

    def test_training_item_to_entry_omits_origin_when_unset(self):
        from app.services.doctrine_bridge import _training_item_to_entry

        entry = _training_item_to_entry(
            {
                "messages": [
                    {"role": "user", "content": "p"},
                    {"role": "assistant", "content": "r"},
                ],
                "metadata": {"trace_id": "t-legacy", "failure_type": "injection"},
            }
        )
        assert "origin" not in entry

    def test_import_dataset_sends_synthetic_origin(self):
        from app.services.doctrine_bridge import import_dataset

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"imported": 1}

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__ = lambda s: s
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value.post.return_value = mock_resp

            item = {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "harness prompt"},
                    {"role": "assistant", "content": "blocked"},
                ],
                "metadata": {"trace_id": "t-harness", "failure_type": "injection"},
            }
            import_dataset([item], "batch-synthetic", origin="synthetic")
            sent = mock_client_cls.return_value.post.call_args.kwargs["json"]
            assert sent["origin"] == "synthetic"

    def test_import_dataset_raises_on_error(self):
        from app.services.doctrine_bridge import DoctrineBridgeError, import_dataset

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__ = lambda s: s
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value.post.return_value = mock_resp

            try:
                import_dataset([], "batch-err")
                pytest.fail("Should have raised")
            except DoctrineBridgeError as exc:
                assert exc.status_code == 500

    def test_submit_eval_report_posts(self):
        from app.services.doctrine_bridge import submit_eval_report

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"accepted": True}

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__ = lambda s: s
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value.post.return_value = mock_resp

            result = submit_eval_report({"model_id": "test", "metrics": {}})
            assert result["accepted"] is True

    def test_trigger_finetune_posts(self):
        from app.services.doctrine_bridge import trigger_finetune

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"job_id": "ft-123"}

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__ = lambda s: s
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value.post.return_value = mock_resp

            result = trigger_finetune(model_id="gpt-4o-mini")
            assert result["job_id"] == "ft-123"


class TestLabelingFlow:
    def test_push_and_label(self, db_session):
        item = push_failure(
            trace_id="t-label-1",
            source_node="safety",
            failure_type="safety",
            prompt="harmful prompt",
            response="harmful response",
            critic_output={"safety": 0.1},
            db_session=db_session,
        )
        assert item["status"] == "pending"

        labeled = label_item(
            item_id=item["id"],
            label="correct_flag",
            reviewer_id="reviewer-1",
            corrected_response="safe response",
            reviewer_notes="Fixed safety issue",
            db_session=db_session,
        )
        assert labeled["status"] == "labeled"
        assert labeled["label"] == "correct_flag"

    def test_export_for_training(self, db_session):
        item = push_failure(
            trace_id="t-export-1",
            source_node="reasoning",
            failure_type="reasoning",
            prompt="bad reasoning prompt",
            response="bad reasoning response",
            critic_output={"reasoning": 0.2},
            db_session=db_session,
        )
        label_item(
            item_id=item["id"],
            label="correct_flag",
            reviewer_id="reviewer-2",
            corrected_response="good reasoning response",
            db_session=db_session,
        )

        exported = export_for_training(db_session=db_session)
        assert len(exported) >= 1
        found = [m for m in exported if m["metadata"]["trace_id"] == "t-export-1"]
        assert len(found) == 1
        assert found[0]["messages"][-1]["content"] == "good reasoning response"
        assert found[0]["metadata"]["origin"] == "organic"

    def test_export_tags_asflc_import_as_synthetic(self, db_session):
        item = push_failure(
            trace_id=f"{ASFLC_TRACE_ID_PREFIX}abc12345",
            source_node=ASFLC_SOURCE_NODE,
            failure_type=ASFLC_FAILURE_TYPE,
            prompt="golden prompt",
            response="golden response",
            critic_output={"source": ASFLC_CRITIC_SOURCE, "category": "reasoning"},
            db_session=db_session,
        )
        label_item(
            item_id=item["id"],
            label="correct_flag",
            reviewer_id=ASFLC_REVIEWER_ID,
            corrected_response="corrected golden response",
            db_session=db_session,
        )

        exported = export_for_training(db_session=db_session)
        found = [m for m in exported if m["metadata"]["trace_id"] == f"{ASFLC_TRACE_ID_PREFIX}abc12345"]
        assert len(found) == 1
        assert found[0]["metadata"]["origin"] == "synthetic"

    def test_export_with_custom_batch_id(self, db_session):
        item = push_failure(
            trace_id="t-batch-1",
            source_node="injection",
            failure_type="injection",
            prompt="injection prompt",
            response="injection response",
            critic_output={"injection": 0.1},
            db_session=db_session,
        )
        label_item(
            item_id=item["id"],
            label="correct_flag",
            reviewer_id="reviewer-3",
            db_session=db_session,
        )

        exported = export_for_training(batch_id="custom-batch-123", db_session=db_session)
        assert len(exported) >= 1
        assert exported[0]["metadata"]["batch_id"] == "custom-batch-123"

    def test_label_nonexistent_item(self, db_session):
        result = label_item(
            item_id="nonexistent",
            label="correct_flag",
            reviewer_id="reviewer",
            db_session=db_session,
        )
        assert result is None


class TestExportRevert:
    def test_revert_on_double_failure(self, client, db_engine):
        """If Doctrine import and outbox enqueue both fail, items revert to labeled."""
        from app.models.labeling_queue import LabelingItem
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=db_engine)
        setup_db = Session()
        try:
            item = push_failure(
                trace_id="t-revert-1",
                source_node="safety",
                failure_type="safety",
                prompt="revert test",
                response="revert response",
                critic_output={"safety": 0.1},
                db_session=setup_db,
            )
            label_item(
                item_id=item["id"],
                label="correct_flag",
                reviewer_id="reviewer",
                db_session=setup_db,
            )
        finally:
            setup_db.close()

        with (
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
            patch("app.services.doctrine_bridge.import_dataset", side_effect=RuntimeError("boom")),
            patch("app.core.training.outbox.enqueue_failed_import", side_effect=RuntimeError("double boom")),
        ):
            resp = client.post(
                "/api/training/export",
                json={"batch_size": 10, "send_to_doctrine_lab": True},
            )

        assert resp.status_code == 200

        check_db = Session()
        try:
            reverted = check_db.query(LabelingItem).filter_by(id=item["id"]).first()
            assert reverted is not None
            assert reverted.status == "labeled"
            check_db.delete(reverted)
            check_db.commit()
        finally:
            check_db.close()


class TestInputValidation:
    def test_export_batch_size_too_large(self, client):
        resp = client.post(
            "/api/training/export",
            json={"batch_size": 9999, "send_to_doctrine_lab": False},
        )
        assert resp.status_code == 422

    def test_export_batch_size_zero(self, client):
        resp = client.post(
            "/api/training/export",
            json={"batch_size": 0, "send_to_doctrine_lab": False},
        )
        assert resp.status_code == 422

    def test_export_batch_size_negative(self, client):
        resp = client.post(
            "/api/training/export",
            json={"batch_size": -1, "send_to_doctrine_lab": False},
        )
        assert resp.status_code == 422


class TestTrainingAPI:
    def test_queue_endpoint(self, client):
        resp = client.get("/api/training/queue")
        assert resp.status_code == 200
        assert "items" in resp.json()
        assert "count" in resp.json()

    def test_label_endpoint_404(self, client):
        resp = client.post(
            "/api/training/queue/nonexistent/label",
            json={"label": "correct_flag", "reviewer_id": "alice"},
        )
        assert resp.status_code == 404

    def test_export_empty_queue(self, client):
        resp = client.post(
            "/api/training/export",
            json={"batch_size": 10, "send_to_doctrine_lab": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exported"] == 0

    def test_eval_not_configured(self, client):
        resp = client.post(
            "/api/training/eval",
            json={"model_id": "test", "metrics": {"accuracy": 0.9}},
        )
        assert resp.status_code == 503

    def test_finetune_not_configured(self, client):
        resp = client.post(
            "/api/training/finetune",
            json={"model_id": "gpt-4o-mini"},
        )
        assert resp.status_code == 503

    def test_finetune_status_not_configured(self, client):
        resp = client.get("/api/training/finetune/status/ft-test-123")
        assert resp.status_code == 503

    def test_finetune_status_success(self, client):
        mock_status = {"status": "running", "job_id": "ft-test-123"}
        with (
            patch(
                "app.services.doctrine_bridge.get_finetune_job_status",
                return_value=mock_status,
            ),
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
        ):
            resp = client.get("/api/training/finetune/status/ft-test-123")
            assert resp.status_code == 200
            assert resp.json()["status"] == "running"

    def test_finetune_status_failure_returns_502(self, client):
        with (
            patch(
                "app.services.doctrine_bridge.get_finetune_job_status",
                side_effect=Exception("connection refused"),
            ),
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
        ):
            resp = client.get("/api/training/finetune/status/ft-bad-job")
            assert resp.status_code == 502
            assert "connection refused" not in resp.json()["detail"]

    def test_promote_adapter_not_configured(self, client):
        resp = client.post(
            "/api/training/promote-adapter",
            json={"job_id": "ft-x", "node_name": "reasoning"},
        )
        assert resp.status_code == 503

    def test_promote_adapter_job_not_ready(self, client):
        mock_status = {"status": "running"}
        with (
            patch(
                "app.services.doctrine_bridge.get_finetune_job_status",
                return_value=mock_status,
            ),
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
        ):
            resp = client.post(
                "/api/training/promote-adapter",
                json={"job_id": "ft-x", "node_name": "reasoning"},
            )
            assert resp.status_code == 400
            assert "promotable" in resp.json()["detail"].lower()

    def test_promote_adapter_success(self, client, db_session):
        from app.models.critic_registry import CriticNode

        node = db_session.query(CriticNode).filter_by(name="reasoning").first()
        assert node is not None
        old_lora = node.lora_adapter_path

        mock_status = {"status": "succeeded", "adapter_path": "/models/lora-v2"}
        with (
            patch(
                "app.services.doctrine_bridge.get_finetune_job_status",
                return_value=mock_status,
            ),
            patch("app.services.doctrine_bridge.is_configured", return_value=True),
        ):
            resp = client.post(
                "/api/training/promote-adapter",
                json={"job_id": "ft-done", "node_name": "reasoning"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["promoted"] is True
            assert data["lora_adapter_path"] == "/models/lora-v2"

        db_session.expire_all()
        node = db_session.query(CriticNode).filter_by(name="reasoning").first()
        node.lora_adapter_path = old_lora
        db_session.commit()

    def test_calibration_persist_no_samples(self, client):
        from app.core.training.calibration import get_ece_tracker

        tracker = get_ece_tracker()
        tracker._records.clear()

        resp = client.post("/api/training/calibration/persist")
        assert resp.status_code == 400

    def test_calibration_persist_with_samples(self, client):
        from app.core.training.calibration import get_ece_tracker

        tracker = get_ece_tracker()
        tracker._records.clear()
        tracker.record(0.9, True, "reasoning", "trace-cal-1")
        tracker.record(0.7, False, "reasoning", "trace-cal-2")

        resp = client.post("/api/training/calibration/persist")
        assert resp.status_code == 200
        assert "snapshot_id" in resp.json()

    def test_calibration_snapshots_endpoint(self, client):
        from app.core.training.calibration import get_ece_tracker

        tracker = get_ece_tracker()
        tracker._records.clear()
        tracker.record(0.8, True, "safety", "trace-cal-3")
        client.post("/api/training/calibration/persist")

        resp = client.get("/api/training/calibration/snapshots")
        assert resp.status_code == 200
        data = resp.json()
        assert "snapshots" in data
        assert data["count"] >= 1

    def test_calibration_snapshots_negative_limit_safe(self, client):
        resp = client.get("/api/training/calibration/snapshots?limit=-5")
        assert resp.status_code == 200
        data = resp.json()
        assert "snapshots" in data
