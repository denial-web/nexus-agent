"""Tests for the training flywheel — labeling, export, and Doctrine Lab bridge."""
from unittest.mock import MagicMock, patch

from app.core.training.labeler import push_failure, label_item, export_for_training
from app.services.doctrine_bridge import compute_batch_id, is_configured


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

            result = import_dataset([{"messages": []}], "batch-x")
            assert result["imported"] == 2
            mock_client_cls.return_value.post.assert_called_once()

    def test_import_dataset_raises_on_error(self):
        from app.services.doctrine_bridge import import_dataset, DoctrineBridgeError

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
                assert False, "Should have raised"
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
