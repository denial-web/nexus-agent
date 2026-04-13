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
            priority="01",
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
            priority="01",
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
