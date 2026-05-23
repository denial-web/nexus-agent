"""Tests for structured audit log export (SIEM-compatible JSONL)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from app.models.approval_log import ApprovalRequest
from app.models.trace import Trace
from app.services.audit_export import (
    _approval_to_record,
    _trace_to_record,
    export_audit_logs,
    get_event_types,
    records_to_jsonl,
)


def _make_trace(db, **overrides):
    defaults = {
        "session_id": "sess-1",
        "prompt": "hello",
        "prompt_hash": hashlib.sha256(b"hello").hexdigest(),
        "immune_verdict": "pass",
        "status": "completed",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    t = Trace(**defaults)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_approval(db, **overrides):
    defaults = {
        "trace_id": "trace-1",
        "action_type": "respond",
        "action_payload": {"key": "val"},
        "risk_level": "high",
        "status": "pending",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    a = ApprovalRequest(**defaults)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


class TestTraceToRecord:
    def test_normal_pipeline_run(self, db_session):
        trace = _make_trace(
            db_session,
            prev_hash="genesis",
            trace_hash="a" * 64,
            full_record_hash="b" * 64,
        )
        record = _trace_to_record(trace)
        assert record["event_type"] == "pipeline_run"
        assert record["severity"] == "info"
        assert record["source"] == "nexus-agent"
        assert record["data"]["trace_id"] == trace.id
        assert record["data"]["prev_hash"] == trace.prev_hash
        assert record["data"]["trace_hash"] == trace.trace_hash
        assert record["data"]["full_record_hash"] == trace.full_record_hash
        assert "timestamp" in record

    def test_input_blocked(self, db_session):
        trace = _make_trace(db_session, immune_verdict="block", status="blocked")
        record = _trace_to_record(trace)
        assert record["event_type"] == "input_blocked"
        assert record["severity"] == "high"

    def test_output_blocked(self, db_session):
        trace = _make_trace(db_session, output_scan_verdict="block")
        record = _trace_to_record(trace)
        assert record["event_type"] == "output_blocked"
        assert record["severity"] == "high"

    def test_critic_halt(self, db_session):
        trace = _make_trace(db_session, critic_verdict="halt")
        record = _trace_to_record(trace)
        assert record["event_type"] == "critic_halt"
        assert record["severity"] == "medium"

    def test_governance_denied(self, db_session):
        trace = _make_trace(db_session, governance_status="denied")
        record = _trace_to_record(trace)
        assert record["event_type"] == "governance_denied"
        assert record["severity"] == "medium"

    def test_mcp_fields_included(self, db_session):
        trace = _make_trace(
            db_session,
            mcp_backend="github",
            mcp_tool_name="list_repos",
        )
        record = _trace_to_record(trace)
        assert record["data"]["mcp_backend"] == "github"
        assert record["data"]["mcp_tool_name"] == "list_repos"

    def test_error_included_when_present(self, db_session):
        trace = _make_trace(db_session, error="LLM timeout")
        record = _trace_to_record(trace)
        assert record["data"]["error"] == "LLM timeout"

    def test_no_error_key_when_absent(self, db_session):
        trace = _make_trace(db_session)
        record = _trace_to_record(trace)
        assert "error" not in record["data"]


class TestApprovalToRecord:
    def test_pending_is_approval_requested(self, db_session):
        approval = _make_approval(db_session)
        record = _approval_to_record(approval)
        assert record["event_type"] == "approval_requested"
        assert record["severity"] == "info"
        assert record["data"]["approval_id"] == approval.id

    def test_approved_is_approval_resolved(self, db_session):
        approval = _make_approval(
            db_session,
            status="approved",
            resolved_at=datetime.now(UTC),
        )
        record = _approval_to_record(approval)
        assert record["event_type"] == "approval_resolved"
        assert "resolved_at" in record["data"]

    def test_denied_is_approval_resolved(self, db_session):
        approval = _make_approval(db_session, status="denied")
        record = _approval_to_record(approval)
        assert record["event_type"] == "approval_resolved"

    def test_policy_id_included(self, db_session):
        approval = _make_approval(db_session, policy_id="pol-123")
        record = _approval_to_record(approval)
        assert record["data"]["policy_id"] == "pol-123"


class TestExportAuditLogs:
    def test_empty_db_returns_empty(self, db_session):
        records = export_audit_logs(db_session, limit=100)
        assert isinstance(records, list)

    def test_returns_trace_records(self, db_session):
        _make_trace(db_session)
        records = export_audit_logs(db_session, limit=100)
        trace_records = [r for r in records if r["event_type"] == "pipeline_run"]
        assert len(trace_records) >= 1

    def test_filter_by_event_type(self, db_session):
        _make_trace(db_session, immune_verdict="block", status="blocked")
        _make_trace(db_session, status="completed")
        records = export_audit_logs(
            db_session,
            event_types=["input_blocked"],
            limit=100,
        )
        assert all(r["event_type"] == "input_blocked" for r in records)

    def test_filter_by_time_range(self, db_session):
        old = datetime.now(UTC) - timedelta(hours=2)
        recent = datetime.now(UTC) - timedelta(minutes=5)
        _make_trace(db_session, created_at=old, status="completed")
        _make_trace(db_session, created_at=recent, status="completed")
        records = export_audit_logs(
            db_session,
            since=datetime.now(UTC) - timedelta(hours=1),
            limit=100,
        )
        assert len(records) >= 1

    def test_filter_by_status(self, db_session):
        _make_trace(db_session, status="blocked", immune_verdict="block")
        _make_trace(db_session, status="completed")
        records = export_audit_logs(db_session, status="blocked", limit=100)
        trace_event_types = {
            "pipeline_run",
            "input_blocked",
            "output_blocked",
            "critic_halt",
            "governance_denied",
        }
        trace_records = [r for r in records if r["event_type"] in trace_event_types]
        assert len(trace_records) >= 1
        for r in trace_records:
            assert r["data"]["status"] == "blocked"

    def test_limit_respected(self, db_session):
        for _ in range(5):
            _make_trace(db_session)
        records = export_audit_logs(db_session, limit=2)
        assert len(records) <= 2

    def test_includes_approvals(self, db_session):
        _make_approval(db_session)
        records = export_audit_logs(
            db_session,
            event_types=["approval_requested"],
            limit=100,
        )
        assert len(records) >= 1
        assert records[0]["event_type"] == "approval_requested"

    def test_sorted_by_timestamp(self, db_session):
        t1 = datetime.now(UTC) - timedelta(minutes=10)
        t2 = datetime.now(UTC) - timedelta(minutes=5)
        _make_trace(db_session, created_at=t2)
        _make_trace(db_session, created_at=t1)
        records = export_audit_logs(db_session, limit=100)
        timestamps = [r["timestamp"] for r in records]
        assert timestamps == sorted(timestamps)

    def test_max_limit_capped(self, db_session):
        records = export_audit_logs(db_session, limit=99999)
        assert isinstance(records, list)


class TestRecordsToJsonl:
    def test_empty_records(self):
        result = records_to_jsonl([])
        assert result == ""

    def test_single_record(self):
        records = [{"event_type": "test", "timestamp": "2026-01-01T00:00:00"}]
        result = records_to_jsonl(records)
        lines = result.strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "test"

    def test_multiple_records(self):
        records = [
            {"event_type": "a", "timestamp": "t1"},
            {"event_type": "b", "timestamp": "t2"},
        ]
        result = records_to_jsonl(records)
        lines = result.strip().split("\n")
        assert len(lines) == 2

    def test_trailing_newline(self):
        records = [{"event_type": "test"}]
        result = records_to_jsonl(records)
        assert result.endswith("\n")

    def test_compact_json(self):
        records = [{"key": "value", "nested": {"a": 1}}]
        result = records_to_jsonl(records)
        assert " " not in result.strip()


class TestGetEventTypes:
    def test_returns_sorted_list(self):
        types = get_event_types()
        assert types == sorted(types)
        assert "pipeline_run" in types
        assert "input_blocked" in types
        assert "approval_requested" in types


class TestAuditExportAPI:
    def test_export_jsonl_format(self, client):
        resp = client.get("/v1/traces/audit/export")
        assert resp.status_code == 200
        assert "application/x-ndjson" in resp.headers.get("content-type", "")
        assert "audit-export.jsonl" in resp.headers.get("content-disposition", "")

    def test_export_json_format(self, client):
        resp = client.get("/v1/traces/audit/export?format=json")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "records" in data

    def test_list_event_types(self, client):
        resp = client.get("/v1/traces/audit/events")
        assert resp.status_code == 200
        data = resp.json()
        assert "event_types" in data
        assert "pipeline_run" in data["event_types"]

    def test_filter_by_event_type_param(self, client):
        resp = client.get("/v1/traces/audit/export?format=json&event_type=input_blocked")
        assert resp.status_code == 200
        data = resp.json()
        for record in data["records"]:
            assert record["event_type"] == "input_blocked"

    def test_legacy_route_works(self, client):
        resp = client.get("/api/traces/audit/events")
        assert resp.status_code == 200

    def test_pagination_params(self, client):
        resp = client.get("/v1/traces/audit/export?format=json&limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["records"]) <= 5

    def test_jsonl_lines_are_valid_json(self, client, db_session):
        _make_trace(db_session)
        resp = client.get("/v1/traces/audit/export")
        body = resp.text.strip()
        if body:
            for line in body.split("\n"):
                parsed = json.loads(line)
                assert "event_type" in parsed
                assert "timestamp" in parsed
                assert "source" in parsed
