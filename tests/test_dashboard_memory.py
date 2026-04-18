"""
Dashboard tests for the memory pages (Phase 12B Week 4).

Two pages under test:
* `GET /dashboard/memory` — subsystem overview + recent-beliefs table.
* `GET /dashboard/memory/integrity` + `POST /dashboard/memory/integrity/verify`
  — hash-chain verification UI wired to the same production verifier
  that backs the REST API.

The tests use the real `write_belief` writer so the rendered chain is
exactly what an operator would see in production — faking rows with
arbitrary hashes would prove nothing about the verifier's behaviour.
"""

from __future__ import annotations

import re

import pytest

from app.config import settings
from app.core.memory.confidence import from_mean_and_strength
from app.core.memory.skepticism import BeliefDraft
from app.core.memory.writer import write_belief
from app.main import _seed_memory_policies
from app.models.belief import Belief
from app.models.episode import Episode
from app.models.policy import Policy
from app.models.step_trace import StepTrace
from app.models.trace import Trace


@pytest.fixture(autouse=True)
def _purge_memory_rows(db_session):
    yield
    db_session.rollback()
    db_session.query(StepTrace).delete()
    db_session.query(Episode).delete()
    db_session.query(Trace).delete()
    db_session.query(Belief).delete()
    db_session.query(Policy).filter(Policy.name.like("memory-%")).delete()
    db_session.commit()


@pytest.fixture
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)


def _seed(db) -> None:
    _seed_memory_policies(db)
    db.commit()


def _write_pref(db, *, user: str | None, value: str, predicate: str = "prefers") -> str:
    draft = BeliefDraft(
        entity=f"user:{user or 'anon'}",
        predicate=predicate,
        value=value,
        entity_type="preference",
        confidence=from_mean_and_strength(0.9, 20.0),
        source_type="user_stated",
        user_id=user,
        session_id="dash-memory-test",
        keywords=[predicate, value],
    )
    out = write_belief(draft, db)
    assert out.status in {"accepted", "superseded"}, out.reason
    db.commit()
    return out.belief_id


# ────────────────────────────────────────────────────────────────────────
# /dashboard/memory — overview
# ────────────────────────────────────────────────────────────────────────


class TestMemoryOverviewPage:
    def test_loads_when_disabled(self, client):
        """Default flag state (off) must still render — the page itself
        is the operator's cue that the feature is off."""
        r = client.get("/dashboard/memory")
        assert r.status_code == 200
        assert "Memory subsystem disabled" in r.text
        assert "MEMORY_ENABLED=false" in r.text

    def test_loads_when_enabled_empty(self, client, memory_on):
        r = client.get("/dashboard/memory")
        assert r.status_code == 200
        assert "Live Beliefs" in r.text
        assert "Verify Hash Chain" in r.text
        assert "No beliefs recorded yet" in r.text

    def test_shows_recent_beliefs(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="alice", value="dark_mode")
        _write_pref(db_session, user="bob", value="light_mode")

        r = client.get("/dashboard/memory")
        assert r.status_code == 200
        assert "alice" in r.text
        assert "bob" in r.text
        assert "dark_mode" in r.text
        assert "live" in r.text.lower()

    def test_stats_reflect_live_count(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="u1", value="v1")
        _write_pref(db_session, user="u2", value="v2")

        r = client.get("/dashboard/memory")
        assert r.status_code == 200
        # 2 live rows → belief count banner should carry the number.
        assert re.search(r'class="value">\s*2\s*</div>\s*<div class="label">Live Beliefs', r.text)

    def test_nav_link_present(self, client):
        r = client.get("/dashboard/memory")
        assert 'href="/dashboard/memory"' in r.text


# ────────────────────────────────────────────────────────────────────────
# /dashboard/memory/integrity — verification UI
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityPageGet:
    def test_initial_get_shows_form_without_running_verifier(
        self, client, db_session, memory_on
    ):
        """The landing GET must NOT auto-verify — operators should
        trigger audits explicitly. If verification ran on GET the
        page would be a heavy side-effect and would spam the audit log."""
        _seed(db_session)
        _write_pref(db_session, user="no-auto-run", value="v")

        r = client.get("/dashboard/memory/integrity")
        assert r.status_code == 200
        assert "Run Verification" in r.text
        # No result card should appear before the user submits.
        assert "Rows checked" not in r.text
        assert "broken" not in r.text.lower() or "Broken chains" in r.text

    def test_get_when_disabled_shows_banner(self, client):
        r = client.get("/dashboard/memory/integrity")
        assert r.status_code == 200
        assert "Memory subsystem disabled" in r.text


# ────────────────────────────────────────────────────────────────────────
# POST verify — happy paths, scopes, errors
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityVerifyPost:
    def test_verify_all_chains_clean(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="a", value="v1")
        _write_pref(db_session, user="b", value="v2")
        _write_pref(db_session, user=None, value="shared")

        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"scope_all": "true"},
        )
        assert r.status_code == 200
        assert "verified" in r.text.lower()
        assert "all chains" in r.text
        # 3 rows walked across 3 chains.
        assert re.search(r"<dt>Rows checked</dt>\s*<dd[^>]*>3</dd>", r.text)

    def test_verify_specific_user(self, client, db_session, memory_on):
        _seed(db_session)
        _write_pref(db_session, user="alice", value="v1")
        _write_pref(db_session, user="bob", value="v2")

        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"user_id": "alice"},
        )
        assert r.status_code == 200
        assert "alice" in r.text
        # Only Alice's one row should be counted.
        assert re.search(r"<dt>Rows checked</dt>\s*<dd[^>]*>1</dd>", r.text)

    def test_verify_null_user_only(self, client, db_session, memory_on):
        """Blank user_id + scope_all unchecked = NULL-user chain only."""
        _seed(db_session)
        _write_pref(db_session, user="alice", value="v1")
        _write_pref(db_session, user=None, value="shared")

        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={},  # neither scope_all nor user_id
        )
        assert r.status_code == 200
        assert "NULL-user" in r.text
        assert re.search(r"<dt>Rows checked</dt>\s*<dd[^>]*>1</dd>", r.text)

    def test_verify_broken_chain_shows_break(self, client, db_session, memory_on):
        """A broken chain must render as a visible 'broken' finding —
        this is the whole point of the page."""
        _seed(db_session)
        bid = _write_pref(db_session, user="victim", value="ok")
        row = db_session.query(Belief).filter(Belief.id == bid).one()
        row.value = "tampered"
        db_session.commit()

        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"user_id": "victim"},
        )
        assert r.status_code == 200
        assert "broken" in r.text.lower()
        assert bid in r.text
        assert "mismatch" in r.text.lower()

    def test_naive_as_of_rejected_400(self, client, db_session, memory_on):
        _seed(db_session)
        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"user_id": "x", "as_of": "2026-01-01T00:00:00"},
        )
        assert r.status_code == 400
        assert "timezone-aware" in r.text

    def test_malformed_as_of_rejected_400(self, client, db_session, memory_on):
        _seed(db_session)
        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"user_id": "x", "as_of": "not-a-date"},
        )
        assert r.status_code == 400
        assert "Could not parse as_of" in r.text

    def test_post_when_disabled_returns_503(self, client):
        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"scope_all": "true"},
        )
        assert r.status_code == 503
        assert "Memory subsystem disabled" in r.text
        assert "MEMORY_ENABLED=false" in r.text


# ────────────────────────────────────────────────────────────────────────
# POST verify — Covernor gate
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityVerifyGovernance:
    def test_403_when_no_matching_policy(self, client, db_session, memory_on):
        """Dashboard must honor the same Covernor gate as the REST API.

        The `client` fixture's lifespan seeds memory policies; we
        remove the integrity-read allow rule so the default-deny
        verdict fires.
        """
        db_session.query(Policy).filter(
            Policy.name == "memory-allow-integrity-read"
        ).delete()
        db_session.commit()

        _write_pref(db_session, user="victim", value="v")
        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"user_id": "victim"},
        )
        assert r.status_code == 403
        assert "Denied by policy" in r.text


# ────────────────────────────────────────────────────────────────────────
# CSRF
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityVerifyCSRF:
    @pytest.fixture
    def csrf_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "ENFORCE_DASHBOARD_CSRF", True)

    def test_post_rejected_without_token(self, client, memory_on, csrf_enabled):
        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"scope_all": "true"},
        )
        assert r.status_code == 403
        assert "CSRF" in r.text

    def test_post_accepted_with_valid_token(
        self, client, db_session, memory_on, csrf_enabled
    ):
        _seed(db_session)
        _write_pref(db_session, user="csrf-user", value="v")

        page = client.get("/dashboard/memory/integrity")
        assert page.status_code == 200
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        assert m, "csrf hidden field missing from integrity form"
        token = m.group(1)

        r = client.post(
            "/dashboard/memory/integrity/verify",
            data={"user_id": "csrf-user", "csrf_token": token},
        )
        assert r.status_code == 200
        assert "verified" in r.text.lower()
