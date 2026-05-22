"""Tests for governance approval flow and K-of-N."""

from datetime import UTC

import pytest
from app.models.approval_log import ApprovalRequest
from app.models.policy import Policy


@pytest.fixture
def require_approval_policy(db_session):
    db_session.add(
        Policy(
            name="gov-require-approval",
            action_pattern="respond",
            resource_pattern="chat",
            decision="require_approval",
            risk_level="high",
            required_approvals="2",
            priority=1,
        )
    )
    db_session.commit()
    yield
    p = db_session.query(Policy).filter_by(name="gov-require-approval").first()
    if p:
        db_session.delete(p)
        db_session.commit()


def test_approval_full_cycle(client, require_approval_policy):
    run_resp = client.post("/api/agent/run", json={"prompt": "Approve me"})
    assert run_resp.status_code == 200
    data = run_resp.json()
    assert data["status"] == "pending_approval"
    req_id = data.get("approval_request_id")
    assert req_id

    client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "approve"},
    )
    r2 = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "bob", "decision": "approve"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "approved"


def test_approval_updates_trace(client, db_session, require_approval_policy):
    """After quorum approval, the trace should be completed with token and output scan."""
    from app.models.trace import Trace

    run_resp = client.post("/api/agent/run", json={"prompt": "Trace update test"})
    data = run_resp.json()
    assert data["status"] == "pending_approval"
    trace_id = data["trace_id"]
    req_id = data["approval_request_id"]

    client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "approve"},
    )
    client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "bob", "decision": "approve"},
    )

    db_session.expire_all()
    trace = db_session.query(Trace).filter_by(id=trace_id).first()
    assert trace is not None
    assert trace.status == "completed"
    assert trace.governance_token_id is not None
    assert trace.governance_status == "approved"
    assert trace.output_scan_verdict is not None


def test_duplicate_vote_rejected(client, require_approval_policy):
    run_resp = client.post("/api/agent/run", json={"prompt": "Dup vote"})
    req_id = run_resp.json()["approval_request_id"]

    client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "approve"},
    )
    dup = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "approve"},
    )
    assert dup.status_code == 409


def test_invalid_decision_rejected(client, require_approval_policy):
    run_resp = client.post("/api/agent/run", json={"prompt": "Bad decision"})
    req_id = run_resp.json()["approval_request_id"]

    r = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "abstain"},
    )
    assert r.status_code == 422


def test_expired_request_rejected(client, db_session, require_approval_policy):
    from datetime import datetime, timedelta

    from app.models.approval_log import ApprovalRequest

    run_resp = client.post("/api/agent/run", json={"prompt": "Expired test"})
    req_id = run_resp.json()["approval_request_id"]

    ar = db_session.query(ApprovalRequest).filter_by(id=req_id).first()
    ar.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    r = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "approve"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


def test_deny_vote_immediately_denies(client, require_approval_policy):
    run_resp = client.post("/api/agent/run", json={"prompt": "Deny me"})
    req_id = run_resp.json()["approval_request_id"]

    r = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "deny"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "denied"


def test_create_policy(client, db_session):
    resp = client.post(
        "/api/governance/policies",
        json={
            "name": "test-create-policy",
            "action_pattern": "analyze",
            "resource_pattern": "data",
            "decision": "allow",
            "risk_level": "low",
            "required_approvals": 0,
            "priority": 999,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["policy"]["name"] == "test-create-policy"
    assert data["policy"]["decision"] == "allow"

    from app.models.policy import Policy

    p = db_session.query(Policy).filter_by(name="test-create-policy").first()
    if p:
        db_session.delete(p)
        db_session.commit()


def test_create_policy_duplicate_name_rejected(client, db_session):
    payload = {
        "name": "dupe-policy-test",
        "action_pattern": "respond",
        "resource_pattern": "*",
        "decision": "allow",
        "risk_level": "low",
        "required_approvals": 0,
        "priority": 500,
    }
    resp1 = client.post("/api/governance/policies", json=payload)
    assert resp1.status_code == 200

    resp2 = client.post("/api/governance/policies", json=payload)
    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"]

    from app.models.policy import Policy

    p = db_session.query(Policy).filter_by(name="dupe-policy-test").first()
    if p:
        db_session.delete(p)
        db_session.commit()


def test_create_policy_invalid_decision_rejected(client):
    resp = client.post(
        "/api/governance/policies",
        json={
            "name": "bad-decision",
            "action_pattern": "respond",
            "decision": "maybe",
            "risk_level": "low",
        },
    )
    assert resp.status_code == 422


def test_create_policy_invalid_risk_level_rejected(client):
    resp = client.post(
        "/api/governance/policies",
        json={
            "name": "bad-risk",
            "action_pattern": "respond",
            "decision": "allow",
            "risk_level": "extreme",
        },
    )
    assert resp.status_code == 422


def test_list_policies(client):
    resp = client.get("/api/governance/policies")
    assert resp.status_code == 200
    assert "policies" in resp.json()
    assert len(resp.json()["policies"]) >= 1


def test_default_seeded_tool_policies_require_approval(db_session):
    from app.core.covernor.policy_engine import evaluate_action
    from app.main import _seed_agent_policies

    for policy in (
        db_session.query(Policy)
        .filter(Policy.name.in_(("test-allow-file-write", "test-allow-shell")))
        .all()
    ):
        policy.is_active = False
    db_session.commit()
    _seed_agent_policies(db_session)

    file_read = evaluate_action("file_read", "README.md", db_session=db_session)
    file_write = evaluate_action("file_write", "out.txt", db_session=db_session)
    shell = evaluate_action("shell_exec", "echo hello", db_session=db_session)
    destructive_shell = evaluate_action("shell_exec", "rm -rf tmp", db_session=db_session)

    assert file_read.decision == "allow"
    assert file_write.decision == "require_approval"
    assert shell.decision == "require_approval"
    assert destructive_shell.decision in {"require_approval", "deny"}
    assert destructive_shell.risk_level == "high"


def test_same_api_key_cannot_forge_second_reviewer(client, monkeypatch, require_approval_policy):
    monkeypatch.setattr("app.config.settings.NEXUS_API_KEY", "beta-key")

    headers = {"X-API-Key": "beta-key"}
    run_resp = client.post("/api/agent/run", json={"prompt": "Need approval"}, headers=headers)
    assert run_resp.status_code == 200
    req_id = run_resp.json()["approval_request_id"]

    first = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "alice", "decision": "approve"},
        headers=headers,
    )
    second = client.post(
        f"/api/governance/approve/{req_id}",
        json={"approver_id": "bob", "decision": "approve"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_governance_training_queue_alias(client):
    resp = client.get("/api/governance/training/queue")
    assert resp.status_code == 200
    assert "items" in resp.json()
    assert "count" in resp.json()


def test_dashboard_vote_finalizes_trace(client, db_session, require_approval_policy):
    """Dashboard vote must issue token, run output scan, and complete trace — same as API."""
    from app.models.trace import Trace

    run_resp = client.post("/api/agent/run", json={"prompt": "Dashboard vote test"})
    data = run_resp.json()
    assert data["status"] == "pending_approval"
    trace_id = data["trace_id"]
    req_id = data["approval_request_id"]

    client.post(
        f"/dashboard/approvals/{req_id}/vote",
        data={"decision": "approve", "approver_id": "alice"},
    )
    client.post(
        f"/dashboard/approvals/{req_id}/vote",
        data={"decision": "approve", "approver_id": "bob"},
    )

    db_session.expire_all()
    trace = db_session.query(Trace).filter_by(id=trace_id).first()
    assert trace is not None
    assert trace.status == "completed"
    assert trace.governance_token_id is not None
    assert trace.governance_status == "approved"
    assert trace.output_scan_verdict is not None

    ar = db_session.query(ApprovalRequest).filter_by(id=req_id).first()
    assert ar.status == "approved"
    assert ar.capability_token is not None


def test_dashboard_vote_deny(client, db_session, require_approval_policy):
    """Dashboard deny vote must immediately deny the request."""
    run_resp = client.post("/api/agent/run", json={"prompt": "Dashboard deny test"})
    req_id = run_resp.json()["approval_request_id"]

    client.post(
        f"/dashboard/approvals/{req_id}/vote",
        data={"decision": "deny", "approver_id": "alice"},
    )

    db_session.expire_all()
    ar = db_session.query(ApprovalRequest).filter_by(id=req_id).first()
    assert ar.status == "denied"


def test_approval_refuses_without_trace(client, db_session):
    """Quorum met but trace is missing — must refuse to issue token."""
    from app.models.approval_log import ApprovalRequest as AR

    db_session.add(
        AR(
            id="orphan-req-1",
            trace_id="nonexistent-trace",
            action_type="respond",
            action_payload={},
            risk_level="high",
            required_approvals="2",
            received_approvals="1",
            status="pending",
        )
    )
    db_session.commit()

    r = client.post(
        "/api/governance/approve/orphan-req-1",
        json={"approver_id": "alice", "decision": "approve"},
    )
    assert r.status_code == 500
    assert "trace not found" in r.json()["detail"].lower()

    db_session.expire_all()
    ar = db_session.query(AR).filter_by(id="orphan-req-1").first()
    assert ar.status == "pending"
    assert ar.capability_token is None
    assert int(ar.received_approvals) == 1

    from app.models.approval_log import ApprovalVote

    votes = db_session.query(ApprovalVote).filter_by(request_id="orphan-req-1", approver_id="alice").all()
    assert len(votes) == 0


def test_approval_quorum_floor(client, db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.APPROVAL_QUORUM", 2)

    db_session.add(
        Policy(
            name="gov-quorum-floor",
            action_pattern="respond",
            resource_pattern="chat",
            decision="require_approval",
            risk_level="high",
            required_approvals="1",
            priority=1,
        )
    )
    db_session.commit()

    try:
        run_resp = client.post("/api/agent/run", json={"prompt": "Quorum floor"})
        req_id = run_resp.json()["approval_request_id"]

        ar = db_session.query(ApprovalRequest).filter_by(id=req_id).first()
        assert int(ar.required_approvals) >= 2

        client.post(
            f"/api/governance/approve/{req_id}",
            json={"approver_id": "alice", "decision": "approve"},
        )
        r2 = client.post(
            f"/api/governance/approve/{req_id}",
            json={"approver_id": "bob", "decision": "approve"},
        )
        assert r2.json()["status"] == "approved"
    finally:
        p = db_session.query(Policy).filter_by(name="gov-quorum-floor").first()
        if p:
            db_session.delete(p)
            db_session.commit()
