from app.models.approval_log import ApprovalRequest
from app.models.labeling_queue import LabelingItem
from app.models.skill import Skill
from app.models.trace import Trace


class TestDashboardTraces:
    def test_traces_page_loads(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Execution Traces" in resp.text

    def test_traces_page_shows_data(self, client, db_session):
        db_session.add(
            Trace(
                id="dash-trace-1",
                session_id="s1",
                prompt="Hello",
                prompt_hash="abc",
                immune_verdict="pass",
                status="completed",
                model_id="mock",
                latency_ms=42.0,
            )
        )
        db_session.commit()

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "dash-trace-1" in resp.text

    def test_trace_detail_page(self, client, db_session):
        db_session.add(
            Trace(
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
            )
        )
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
        db_session.add(
            LabelingItem(
                id="label-item-1",
                trace_id="t1",
                source_node="safety",
                failure_type="injection",
                prompt="bad prompt",
                response="bad response",
                critic_output={"safety": {"score": 0.2}},
                status="pending",
            )
        )
        db_session.commit()

        resp = client.get("/dashboard/labeling")
        assert resp.status_code == 200
        assert "bad prompt" in resp.text

    def test_apply_label(self, client, db_session):
        db_session.add(
            LabelingItem(
                id="label-item-2",
                trace_id="t2",
                source_node="reasoning",
                failure_type="reasoning",
                prompt="test",
                critic_output={},
                status="pending",
            )
        )
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

    def test_apply_invalid_label_rejected(self, client, db_session):
        db_session.add(
            LabelingItem(
                id="label-item-invalid",
                trace_id="t-inv",
                source_node="reasoning",
                failure_type="reasoning",
                prompt="test",
                critic_output={},
                status="pending",
            )
        )
        db_session.commit()

        resp = client.post(
            "/dashboard/labeling/label-item-invalid/label",
            data={"label": "garbage_value"},
        )
        assert resp.status_code == 400

        db_session.expire_all()
        item = db_session.query(LabelingItem).filter_by(id="label-item-invalid").first()
        assert item.status == "pending"


class TestDashboardApprovals:
    def test_approvals_page_loads(self, client):
        resp = client.get("/dashboard/approvals")
        assert resp.status_code == 200
        assert "Approval Console" in resp.text

    def test_approvals_shows_requests(self, client, db_session):
        db_session.add(
            ApprovalRequest(
                id="approval-1",
                trace_id="t1",
                action_type="file_write",
                action_payload={"path": "/tmp/test"},
                risk_level="high",
                required_approvals="2",
                received_approvals="0",
                status="pending",
            )
        )
        db_session.commit()

        resp = client.get("/dashboard/approvals")
        assert resp.status_code == 200
        assert "file_write" in resp.text

    def test_cast_approve_vote(self, client, db_session):
        db_session.add(
            Trace(
                id="t3",
                session_id="s-approval",
                prompt="test approval",
                prompt_hash="abc",
                immune_verdict="pass",
                status="pending_approval",
                response="approved response",
                model_id="mock",
            )
        )
        db_session.add(
            ApprovalRequest(
                id="approval-vote-1",
                trace_id="t3",
                action_type="api_call",
                action_payload={},
                risk_level="high",
                required_approvals="2",
                received_approvals="0",
                status="pending",
            )
        )
        db_session.commit()

        client.post(
            "/dashboard/approvals/approval-vote-1/vote",
            data={"decision": "approve", "approver_id": "alice"},
        )
        resp = client.post(
            "/dashboard/approvals/approval-vote-1/vote",
            data={"decision": "approve", "approver_id": "bob"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        req = db_session.query(ApprovalRequest).filter_by(id="approval-vote-1").first()
        assert req.status == "approved"
        assert int(req.received_approvals) == 2

    def test_cast_deny_vote(self, client, db_session):
        db_session.add(
            ApprovalRequest(
                id="approval-deny-1",
                trace_id="t4",
                action_type="fund_transfer",
                action_payload={},
                risk_level="critical",
                required_approvals="2",
                received_approvals="0",
                status="pending",
            )
        )
        db_session.commit()

        resp = client.post(
            "/dashboard/approvals/approval-deny-1/vote",
            data={"decision": "deny", "approver_id": "tester"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        req = db_session.query(ApprovalRequest).filter_by(id="approval-deny-1").first()
        assert req.status == "denied"

    def test_vote_on_nonexistent_request_returns_error(self, client):
        resp = client.post(
            "/dashboard/approvals/nonexistent-req/vote",
            data={"decision": "approve", "approver_id": "tester"},
            follow_redirects=False,
        )
        assert resp.status_code >= 400
        assert "Vote failed" in resp.text


class TestDashboardCalibration:
    def test_calibration_page_loads(self, client):
        resp = client.get("/dashboard/calibration")
        assert resp.status_code == 200
        assert "Calibration Dashboard" in resp.text
        assert "Expected Calibration Error" in resp.text


class TestDashboardSkills:
    def test_skills_page_loads(self, client):
        resp = client.get("/dashboard/skills")
        assert resp.status_code == 200
        assert "Skills Library" in resp.text

    def test_skills_page_shows_data(self, client, db_session):
        db_session.add(
            Skill(
                id="dash-skill-1",
                name="test-dashboard-skill",
                description="A skill shown in the dashboard",
                steps=[{"action": "tool_call", "tool": "file_read", "arguments_template": {"path": "x"}}],
                expected_reward=0.9,
                enabled=True,
                avg_reward=0.85,
                total_runs=3,
            )
        )
        db_session.commit()

        resp = client.get("/dashboard/skills")
        assert resp.status_code == 200
        assert "test-dashboard-skill" in resp.text
        assert "0.85" in resp.text

    def test_skills_page_shows_empty_state(self, client):
        resp = client.get("/dashboard/skills")
        assert resp.status_code == 200
        assert "auto-generated" in resp.text or "Skills Library" in resp.text

    def test_skill_detail_page(self, client, db_session):
        db_session.add(
            Skill(
                id="dash-detail-skill",
                name="detail-view-skill",
                description="Full detail view test",
                steps=[
                    {"action": "tool_call", "tool": "shell_exec", "arguments_template": {"command": "ls"}},
                    {"action": "final_answer", "content_hint": "Done"},
                ],
                expected_reward=0.88,
                enabled=True,
                avg_reward=0.9,
                total_runs=5,
                skill_hash="abc123",
                immune_scanned=True,
                critic_scanned=False,
                source_episode_id="ep-src-1",
            )
        )
        db_session.commit()

        resp = client.get("/dashboard/skills/dash-detail-skill")
        assert resp.status_code == 200
        assert "detail-view-skill" in resp.text
        assert "shell_exec" in resp.text
        assert "ep-src-1" in resp.text

    def test_skill_detail_404(self, client):
        resp = client.get("/dashboard/skills/nonexistent")
        assert resp.status_code == 404

    def test_toggle_skill_disable(self, client, db_session):
        db_session.add(
            Skill(
                id="dash-toggle-1",
                name="toggle-me",
                steps=[],
                expected_reward=0.9,
                enabled=True,
            )
        )
        db_session.commit()

        resp = client.post(
            "/dashboard/skills/dash-toggle-1/toggle",
            data={"enabled": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        skill = db_session.query(Skill).filter_by(id="dash-toggle-1").first()
        assert skill.enabled is False

    def test_toggle_skill_enable_clears_flag(self, client, db_session):
        db_session.add(
            Skill(
                id="dash-toggle-2",
                name="flagged-skill",
                steps=[],
                expected_reward=0.9,
                enabled=False,
                flagged=True,
            )
        )
        db_session.commit()

        resp = client.post(
            "/dashboard/skills/dash-toggle-2/toggle",
            data={"enabled": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        skill = db_session.query(Skill).filter_by(id="dash-toggle-2").first()
        assert skill.enabled is True
        assert skill.flagged is False

    def test_toggle_skill_not_found(self, client):
        resp = client.post(
            "/dashboard/skills/nonexistent/toggle",
            data={"enabled": "false"},
        )
        assert resp.status_code == 404
