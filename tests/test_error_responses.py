"""Tests for structured error responses and request timeouts."""

from __future__ import annotations

import time
from unittest.mock import patch

from app.errors import NexusAPIError, _build_error_body


class TestErrorEnvelope:
    def test_build_error_body_shape(self):
        body = _build_error_body(400, "bad_request", "Something went wrong")
        assert "error" in body
        assert "detail" in body
        err = body["error"]
        assert err["code"] == "bad_request"
        assert err["message"] == "Something went wrong"
        assert err["status"] == 400
        assert "request_id" in err
        assert "timestamp" in err
        assert body["detail"] == "Something went wrong"

    def test_build_error_body_with_details(self):
        body = _build_error_body(422, "validation_error", "Bad input", {"fields": [{"field": "prompt"}]})
        assert body["error"]["details"]["fields"][0]["field"] == "prompt"

    def test_nexus_api_error_attributes(self):
        exc = NexusAPIError(400, "prompt_empty", "Prompt cannot be empty", {"hint": "provide a prompt"})
        assert exc.status_code == 400
        assert exc.code == "prompt_empty"
        assert exc.message == "Prompt cannot be empty"
        assert exc.details == {"hint": "provide a prompt"}
        assert str(exc) == "Prompt cannot be empty"


class TestStructuredErrorAPI:
    def test_404_has_error_envelope(self, client):
        resp = client.get("/api/traces/nonexistent-trace-id-xyz")
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == "not_found"
        assert data["error"]["status"] == 404
        assert "request_id" in data["error"]
        assert "timestamp" in data["error"]
        assert "detail" in data

    def test_400_empty_prompt(self, client):
        resp = client.post("/api/agent/run", json={"prompt": ""})
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "bad_request"
        assert "empty" in data["error"]["message"].lower()
        assert data["detail"] == data["error"]["message"]

    def test_413_prompt_too_long(self, client):
        with patch.object(type(client.app.state), "__getattr__", side_effect=AttributeError):
            pass
        from app.config import settings

        orig = settings.MAX_PROMPT_LENGTH
        settings.MAX_PROMPT_LENGTH = 50
        try:
            resp = client.post("/api/agent/run", json={"prompt": "x" * 51})
            assert resp.status_code == 413
            data = resp.json()
            assert data["error"]["code"] == "payload_too_large"
            assert "maximum" in data["error"]["message"].lower()
        finally:
            settings.MAX_PROMPT_LENGTH = orig

    def test_422_validation_has_field_details(self, client):
        resp = client.post("/api/agent/run", json={})
        assert resp.status_code == 422
        data = resp.json()
        assert data["error"]["code"] == "validation_error"
        assert "details" in data["error"]
        assert "fields" in data["error"]["details"]
        fields = data["error"]["details"]["fields"]
        assert len(fields) >= 1
        assert any("prompt" in f["field"].lower() for f in fields)

    def test_401_auth_error_structured(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "NEXUS_API_KEY", "real-key")
        resp = client.get("/api/traces")
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "unauthorized"
        assert "detail" in data

    def test_429_rate_limit_structured(self, client, monkeypatch):
        from app.config import settings
        from app.services.rate_limiter import reset_backend

        reset_backend()
        monkeypatch.setattr(settings, "RATE_LIMIT_RPM", 1)
        client.post("/api/agent/run", json={"prompt": "first"})
        resp = client.post("/api/agent/run", json={"prompt": "second"})
        assert resp.status_code == 429
        data = resp.json()
        assert data["error"]["code"] == "rate_limit_exceeded"
        assert "detail" in data
        assert resp.headers.get("Retry-After") == "60"
        reset_backend()

    def test_500_unhandled_exception(self, client):

        with patch("app.agent.pipeline.run", side_effect=RuntimeError("unexpected crash")):
            from starlette.testclient import TestClient

            with TestClient(client.app, raise_server_exceptions=False) as safe_client:
                resp = safe_client.post("/api/agent/run", json={"prompt": "test"})
        assert resp.status_code == 500
        data = resp.json()
        assert data["error"]["code"] == "internal_error"
        assert data["detail"] == "Internal server error"


class TestAdditionalErrorPaths:
    """Verify remaining HTTP status codes use the structured envelope."""

    def test_409_conflict_critic_duplicate(self, client):
        import uuid

        name = f"dup-node-{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "node_type": "heuristic", "weight": 1.0, "enabled": True}
        r1 = client.post("/v1/critic/registry", json=payload)
        assert r1.status_code in (200, 201)

        resp = client.post("/v1/critic/registry", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["error"]["code"] == "conflict"
        assert "detail" in data
        assert "request_id" in data["error"]
        assert "timestamp" in data["error"]

    def test_409_conflict_policy_duplicate(self, client):
        import uuid

        name = f"dup-policy-{uuid.uuid4().hex[:8]}"
        payload = {
            "name": name,
            "action_pattern": "test_action",
            "decision": "allow",
        }
        r1 = client.post("/v1/governance/policies", json=payload)
        assert r1.status_code in (200, 201)

        resp = client.post("/v1/governance/policies", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["error"]["code"] == "conflict"
        assert "already exists" in data["error"]["message"].lower()

    def test_503_mcp_local_only_structured(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "LOCAL_ONLY", True)
        resp = client.get("/mcp/anything")
        assert resp.status_code == 503
        data = resp.json()
        assert data["error"]["code"] == "service_unavailable"
        assert "detail" in data
        assert "request_id" in data["error"]

    def test_503_shutdown_guard_structured(self, client):
        from app.services.shutdown import get_coordinator

        coord = get_coordinator()
        coord._draining = True
        try:
            resp = client.post("/v1/agent/run", json={"prompt": "test"})
            assert resp.status_code == 503
            data = resp.json()
            assert data["error"]["code"] == "shutting_down"
            assert "detail" in data
            assert resp.headers.get("Retry-After") == "30"
        finally:
            coord._draining = False

    def test_413_body_size_structured(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 50)
        resp = client.post("/v1/agent/run", json={"prompt": "x" * 200})
        assert resp.status_code == 413
        data = resp.json()
        assert data["error"]["code"] == "payload_too_large"
        assert "detail" in data
        assert "request_id" in data["error"]

    def test_envelope_consistent_across_v1_and_legacy(self, client):
        resp_v1 = client.get("/v1/traces/nonexistent")
        resp_api = client.get("/api/traces/nonexistent")
        assert resp_v1.status_code == 404
        assert resp_api.status_code == 404
        d1 = resp_v1.json()
        d2 = resp_api.json()
        assert d1["error"]["code"] == d2["error"]["code"] == "not_found"
        assert "error" in d1 and "error" in d2
        assert "detail" in d1 and "detail" in d2

    def test_all_status_codes_in_map_produce_known_code(self):
        from app.errors import _STATUS_CODE_MAP, _build_error_body

        for status, code in _STATUS_CODE_MAP.items():
            body = _build_error_body(status, code, f"Test message for {status}")
            assert body["error"]["code"] == code
            assert body["error"]["status"] == status
            assert body["detail"] == f"Test message for {status}"

    def test_unknown_status_produces_http_prefix(self):
        from app.errors import _STATUS_CODE_MAP

        assert 418 not in _STATUS_CODE_MAP
        from app.errors import _build_error_body

        body = _build_error_body(418, "http_418", "I'm a teapot")
        assert body["error"]["code"] == "http_418"

    def test_error_envelope_has_iso_timestamp(self, client):
        resp = client.get("/v1/traces/nonexistent")
        data = resp.json()
        ts = data["error"]["timestamp"]
        from datetime import datetime

        datetime.fromisoformat(ts)


class TestRequestTimeout:
    def test_timeout_returns_504(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "REQUEST_TIMEOUT_SECONDS", 0.1)

        def slow_pipeline(*args, **kwargs):
            time.sleep(5)

        with patch("app.api.agent._timeout_pool") as mock_pool:
            from concurrent.futures import Future

            fut = Future()
            mock_pool.submit.return_value = fut

            from concurrent.futures import TimeoutError as FTE

            fut.set_exception(FTE())

            resp = client.post("/api/agent/run", json={"prompt": "test"})

        assert resp.status_code == 504
        data = resp.json()
        assert data["error"]["code"] == "request_timeout"
        assert "timeout" in data["error"]["message"].lower()

    def test_timeout_disabled_when_zero(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "REQUEST_TIMEOUT_SECONDS", 0)
        resp = client.post("/api/agent/run", json={"prompt": "Hello"})
        assert resp.status_code == 200
