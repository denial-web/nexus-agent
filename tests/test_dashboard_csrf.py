"""Dashboard CSRF when ENFORCE_DASHBOARD_CSRF is enabled."""

import re

import pytest
from app.config import settings
from app.models.approval_log import ApprovalRequest
from app.models.labeling_queue import LabelingItem


@pytest.fixture
def csrf_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_DASHBOARD_CSRF", True)


class TestDashboardCSRF:
    def test_labeling_post_rejected_without_token(self, client, db_session, csrf_enabled):
        db_session.add(
            LabelingItem(
                id="csrf-label-1",
                trace_id="t-csrf",
                source_node="safety",
                failure_type="safety",
                prompt="p",
                response="r",
                critic_output={},
                status="pending",
            )
        )
        db_session.commit()

        resp = client.post(
            "/dashboard/labeling/csrf-label-1/label",
            data={"label": "false_positive"},
        )
        assert resp.status_code == 403

    def test_labeling_post_succeeds_with_valid_token(self, client, db_session, csrf_enabled):
        db_session.add(
            LabelingItem(
                id="csrf-label-2",
                trace_id="t-csrf-2",
                source_node="safety",
                failure_type="safety",
                prompt="p",
                response="r",
                critic_output={},
                status="pending",
            )
        )
        db_session.commit()

        page = client.get("/dashboard/labeling")
        assert page.status_code == 200
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        assert m, "csrf hidden field missing"
        token = m.group(1)

        resp = client.post(
            "/dashboard/labeling/csrf-label-2/label",
            data={"label": "false_positive", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers.get("location", "").endswith("/dashboard/labeling")

    def test_vote_rejected_without_token(self, client, db_session, csrf_enabled):
        db_session.add(
            ApprovalRequest(
                id="csrf-appr-1",
                trace_id="t1",
                action_type="api_call",
                action_payload={},
                risk_level="high",
                required_approvals="1",
                received_approvals="0",
                status="pending",
            )
        )
        db_session.commit()

        resp = client.post(
            "/dashboard/approvals/csrf-appr-1/vote",
            data={"decision": "approve", "approver_id": "tester"},
        )
        assert resp.status_code == 403

    def test_circuit_breaker_reset_rejected_without_token(self, client, csrf_enabled):
        from app.core.llm.circuit_breaker import get_registry, reset_registry

        reset_registry()
        reg = get_registry()
        cb = reg.get("gemini")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()

        resp = client.post(
            "/dashboard/circuit-breakers/gemini/reset",
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert cb.state.value == "open"
        reset_registry()

    def test_circuit_breaker_reset_succeeds_with_valid_token(self, client, csrf_enabled):
        from app.core.llm.circuit_breaker import (
            CircuitState,
            get_registry,
            reset_registry,
        )

        reset_registry()
        reg = get_registry()
        cb = reg.get("openai")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()

        page = client.get("/dashboard/circuit-breakers")
        assert page.status_code == 200
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        assert m, "csrf hidden field missing on circuit breakers page"
        token = m.group(1)

        resp = client.post(
            "/dashboard/circuit-breakers/openai/reset",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert cb.state == CircuitState.CLOSED
        reset_registry()

    def test_providers_dashboard_includes_csrf_in_reset_form(self, client, csrf_enabled):
        from app.core.llm.circuit_breaker import get_registry, reset_registry

        reset_registry()
        reg = get_registry()
        cb = reg.get("gemini")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()

        resp = client.get("/dashboard/providers")
        assert resp.status_code == 200
        if "/dashboard/circuit-breakers/gemini/reset" in resp.text:
            assert 'name="csrf_token"' in resp.text
        reset_registry()
