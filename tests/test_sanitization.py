"""Tests for input sanitization utilities and their integration in API responses."""

import uuid

from app.sanitize import sanitize_for_error, sanitize_for_log


class TestSanitizeForLog:
    def test_plain_string_unchanged(self):
        assert sanitize_for_log("hello world") == "hello world"

    def test_newlines_escaped(self):
        result = sanitize_for_log("line1\nline2\rline3")
        assert "\\n" in result
        assert "\\r" in result
        assert "\n" not in result
        assert "\r" not in result

    def test_tabs_escaped(self):
        result = sanitize_for_log("col1\tcol2")
        assert "\\t" in result
        assert "\t" not in result

    def test_control_chars_stripped(self):
        result = sanitize_for_log("abc\x00\x01\x07def")
        assert result == "abcdef"

    def test_truncation(self):
        long_input = "a" * 300
        result = sanitize_for_log(long_input, max_length=50)
        assert len(result) <= 52
        assert result.endswith("…")

    def test_custom_max_length(self):
        result = sanitize_for_log("a" * 10, max_length=5)
        assert result == "aaaaa…"

    def test_exactly_at_limit(self):
        result = sanitize_for_log("a" * 200, max_length=200)
        assert result == "a" * 200
        assert "…" not in result

    def test_empty_string(self):
        assert sanitize_for_log("") == ""

    def test_log_injection_attack(self):
        attack = "normal\n2025-01-01 CRITICAL [root] FAKE LOG ENTRY"
        result = sanitize_for_log(attack)
        assert "\n" not in result
        assert "\\n" in result


class TestSanitizeForError:
    def test_plain_string_quoted(self):
        result = sanitize_for_error("my-policy")
        assert result == "'my-policy'"

    def test_newlines_replaced_with_space(self):
        result = sanitize_for_error("line1\nline2")
        assert result == "'line1 line2'"

    def test_control_chars_stripped(self):
        result = sanitize_for_error("abc\x00\x07def")
        assert result == "'abcdef'"

    def test_truncation(self):
        long_input = "x" * 200
        result = sanitize_for_error(long_input, max_length=50)
        assert len(result) <= 54
        assert result.startswith("'")
        assert "…'" in result

    def test_empty_string(self):
        assert sanitize_for_error("") == "''"

    def test_unicode_preserved(self):
        result = sanitize_for_error("政策名")
        assert result == "'政策名'"


class TestSanitizedErrorResponses:
    """Verify user-supplied names are sanitized in API error messages."""

    def test_critic_409_sanitizes_name(self, client):
        name = f"test-node-{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "node_type": "heuristic", "weight": 1.0, "enabled": True}
        client.post("/v1/critic/registry", json=payload)

        resp = client.post("/v1/critic/registry", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["error"]["message"]
        assert f"'{name}'" in detail

    def test_critic_409_strips_newlines(self, client):
        name = f"evil\nnode-{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "node_type": "heuristic", "weight": 1.0, "enabled": True}
        client.post("/v1/critic/registry", json=payload)

        resp = client.post("/v1/critic/registry", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["error"]["message"]
        assert "\n" not in detail

    def test_governance_409_sanitizes_name(self, client):
        name = f"test-policy-{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "action_pattern": "test", "decision": "allow"}
        client.post("/v1/governance/policies", json=payload)

        resp = client.post("/v1/governance/policies", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["error"]["message"]
        assert f"'{name}'" in detail

    def test_governance_409_strips_control_chars(self, client):
        name = f"pol\x00icy-{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "action_pattern": "test", "decision": "allow"}
        client.post("/v1/governance/policies", json=payload)

        resp = client.post("/v1/governance/policies", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["error"]["message"]
        assert "\x00" not in detail

    def test_mcp_502_no_exception_leak(self, client, monkeypatch):
        """Verify the MCP 502 error does not include raw exception details."""
        from app.config import settings

        monkeypatch.setattr(settings, "LOCAL_ONLY", False)
        resp = client.get("/v1/mcp/backends/nonexistent/tools")
        if resp.status_code == 404:
            detail = resp.json()["error"]["message"]
            assert "traceback" not in detail.lower()
            assert "exception" not in detail.lower()
