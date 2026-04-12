from app.models.trace import Trace
from app.models.labeling_queue import LabelingItem
from app.models.approval_log import ApprovalRequest


class TestDashboardTraces:
    def test_traces_page_loads(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Execution Traces" in resp.text

    def test_traces_page_shows_data(self, client, db_session):
        db_session.add(Trace(
            id="dash-trace-1",
            session_id="s1",
            prompt="Hello",
            prompt_hash="abc",
            immune_verdict="pass",
            status="completed",
            model_id="mock",
            latency_ms=42.0,
        ))
        db_session.commit()

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "dash-trace-1" in resp.text

    def test_trace_detail_page(self, client, db_session):
        db_session.add(Trace(
            id="dash-detail-1",
            session_id="s1",
            prompt="What is 2+2?",
            prompt_hash="abc",
            immune_verdict="pass",
            status="completed",
            response="4",
            model_id="mock",
            critic_verdict="pass",
            critic_scores={"reasoning": {"score": 0.9, "verdict": "pass", "reasoning": "Good"}},
        ))
        db_session.commit()

        resp = client.get("/dashboard/traces/dash-detail-1")
        assert resp.status_code == 200
        assert "What is 2+2?" in resp.text
        assert "reasoning" in resp.text

    def test_trace_detail_404(self, client):
        resp = client.get("/dashboard/traces/nonexistent")
        assert resp.status_code == 404


class TestDashboardLabeling:
    def test_labeling_page_loads(self, client):
        resp = client.get("/dashboard/labeling")
        assert resp.status_code == 200
        assert "Labeling Queue" in resp.text

    def test_labeling_shows_items(self, client, db_session):
        db_session.add(LabelingItem(
            id="label-item-1",
            trace_id="t1",
            source_node="safety",
            failure_type="injection",
            prompt="bad prompt",
            response="bad response",
            critic_output={"safety": {"score": 0.2}},
            status="pending",
        ))
        db_session.commit()

        resp = client.get("/dashboard/labeling")
        assert resp.status_code == 200
        assert "bad prompt" in resp.text

    def test_apply_label(self, client, db_session):
        db_session.add(LabelingItem(
            id="label-item-2",
            trace_id="t2",
            source_node="reasoning",
            failure_type="reasoning",
            prompt="test",
            critic_output={},
            status="pending",
        ))
        db_session.commit()

        resp = client.post(
            "/dashboard/labeling/label-item-2/label",
            data={"label": "false_positive"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        item = db_session.query(LabelingItem).filter_by(id="label-item-2").first()
        assert item.status == "labeled"
        assert item.label == "false_positive"


class TestDashboardApprovals:
    def test_approvals_page_loads(self, client):
        resp = client.get("/dashboard/approvals")
        assert resp.status_code == 200
        assert "Approval Console" in resp.text

    def test_approvals_shows_requests(self, client, db_session):
        db_session.add(ApprovalRequest(
            id="approval-1",
            trace_id="t1",
            action_type="file_write",
            action_payload={"path": "/tmp/test"},
            risk_level="high",
            required_approvals="2",
            received_approvals="0",
            status="pending",
        ))
        db_session.commit()

        resp = client.get("/dashboard/approvals")
        assert resp.status_code == 200
        assert "file_write" in resp.text

    def test_cast_approve_vote(self, client, db_session):
        db_session.add(ApprovalRequest(
            id="approval-vote-1",
            trace_id="t3",
            action_type="api_call",
            action_payload={},
            risk_level="high",
            required_approvals="1",
            received_approvals="0",
            status="pending",
        ))
        db_session.commit()

        resp = client.post(
            "/dashboard/approvals/approval-vote-1/vote",
            data={"decision": "approve", "approver_id": "tester"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        req = db_session.query(ApprovalRequest).filter_by(id="approval-vote-1").first()
        assert req.status == "approved"
        assert req.received_approvals == "1"

    def test_cast_deny_vote(self, client, db_session):
        db_session.add(ApprovalRequest(
            id="approval-deny-1",
            trace_id="t4",
            action_type="fund_transfer",
            action_payload={},
            risk_level="critical",
            required_approvals="2",
            received_approvals="0",
            status="pending",
        ))
        db_session.commit()

        resp = client.post(
            "/dashboard/approvals/approval-deny-1/vote",
            data={"decision": "deny", "approver_id": "tester"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        req = db_session.query(ApprovalRequest).filter_by(id="approval-deny-1").first()
        assert req.status == "denied"


class TestDashboardCalibration:
    def test_calibration_page_loads(self, client):
        resp = client.get("/dashboard/calibration")
        assert resp.status_code == 200
        assert "Calibration Dashboard" in resp.text
        assert "Expected Calibration Error" in resp.text
