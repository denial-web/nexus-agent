"""HTTP tests for the belief memory REST API (Phase 12 Week 2).

Exercises `/v1/memory/*` against the full FastAPI stack via TestClient.
Uses the shared `client` / `db_session` fixtures and the same
autouse purge pattern as `tests/test_agent_loop_memory.py` so rows
committed by the writer don't leak into the MEMORY_ENABLED=False
regression tripwire.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.config import settings
from app.core.memory.confidence import from_mean_and_strength
from app.core.memory.skepticism import BeliefDraft
from app.core.memory.writer import write_belief
from app.main import _seed_memory_policies
from app.models.belief import Belief
from app.models.episode import Episode
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
    db_session.commit()


@pytest.fixture
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)


def _seed(db) -> None:
    _seed_memory_policies(db)
    db.commit()


def _make_pref(
    db,
    *,
    user: str,
    value: str = "dark_mode",
    predicate: str = "prefers",
    mean: float = 0.9,
    strength: float = 20.0,
    source_trace_id: str | None = None,
) -> str:
    draft = BeliefDraft(
        entity=f"user:{user}",
        predicate=predicate,
        value=value,
        entity_type="preference",
        confidence=from_mean_and_strength(mean, strength),
        source_type="user_stated",
        user_id=user,
        session_id="s-1",
        keywords=["theme", "prefers", value],
        rationale="user said so",
    )
    out = write_belief(draft, db, source_trace_id=source_trace_id)
    # "superseded" is also a happy-path terminal status — it means the
    # new belief was persisted AND a stronger prior was marked as
    # superseded in the same transaction. Only genuine gate failures
    # (rejected / needs_evidence / denied / …) should bubble up here.
    assert out.status in {"accepted", "superseded"}, out.reason
    db.commit()
    return out.belief_id


# ────────────────────────────────────────────────────────────────────────
# Feature flag
# ────────────────────────────────────────────────────────────────────────


class TestFeatureFlag:
    def test_list_returns_503_when_disabled(self, client):
        r = client.get("/v1/memory")
        assert r.status_code == 503
        body = r.json()
        assert body["error"]["code"] == "memory_disabled"

    def test_forget_returns_503_when_disabled(self, client):
        r = client.post("/v1/memory/forget", json={"entity": "user:alice"})
        assert r.status_code == 503

    def test_stats_returns_structured_response_when_disabled(self, client):
        """Stats is informational: it must not 503 so operators can
        confirm the subsystem is genuinely off without flipping the flag."""
        r = client.get("/v1/memory/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["total_live"] == 0
        assert body["decay_profile"]  # non-empty even when disabled


# ────────────────────────────────────────────────────────────────────────
# GET /memory — list
# ────────────────────────────────────────────────────────────────────────


class TestList:
    def test_list_current_beliefs(self, client, db_session, memory_on):
        _seed(db_session)
        bid = _make_pref(db_session, user="api-list-u1")

        r = client.get("/v1/memory", params={"user_id": "api-list-u1"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["beliefs"]) == 1
        row = body["beliefs"][0]
        assert row["id"] == bid
        assert row["entity"] == "user:api-list-u1"
        assert row["predicate"] == "prefers"
        assert row["value"] == "dark_mode"
        assert row["is_current"] is True
        assert 0.0 < row["mean"] <= 1.0
        assert row["effective_sample_size"] > 0.0

    def test_list_user_isolation(self, client, db_session, memory_on):
        _seed(db_session)
        _make_pref(db_session, user="api-iso-alice")
        _make_pref(db_session, user="api-iso-bob", value="light_mode")

        r = client.get("/v1/memory", params={"user_id": "api-iso-alice"})
        body = r.json()
        assert body["total"] == 1
        assert body["beliefs"][0]["entity"] == "user:api-iso-alice"

    def test_list_excludes_tombstoned_by_default(
        self, client, db_session, memory_on
    ):
        _seed(db_session)
        uid = "api-tomb-user"
        bid = _make_pref(db_session, user=uid)

        live = db_session.query(Belief).filter(Belief.id == bid).one()
        live.superseded_at = datetime.now(UTC)
        db_session.commit()

        r = client.get("/v1/memory", params={"user_id": uid})
        assert r.json()["total"] == 0

        r2 = client.get(
            "/v1/memory",
            params={"user_id": uid, "include_tombstoned": True},
        )
        assert r2.json()["total"] == 1
        assert r2.json()["beliefs"][0]["is_current"] is False

    def test_list_paging(self, client, db_session, memory_on):
        _seed(db_session)
        uid = "api-page-user"
        for i in range(3):
            _make_pref(
                db_session,
                user=uid,
                predicate=f"likes_{i}",
                value=f"thing_{i}",
            )

        r = client.get(
            "/v1/memory",
            params={"user_id": uid, "limit": 2, "offset": 0},
        )
        body = r.json()
        assert body["total"] == 3
        assert len(body["beliefs"]) == 2

        r2 = client.get(
            "/v1/memory",
            params={"user_id": uid, "limit": 2, "offset": 2},
        )
        assert len(r2.json()["beliefs"]) == 1


# ────────────────────────────────────────────────────────────────────────
# GET /memory/{id}/history
# ────────────────────────────────────────────────────────────────────────


class TestHistory:
    def test_history_returns_all_versions(self, client, db_session, memory_on):
        _seed(db_session)
        uid = "api-hist-user"
        # Two sequential writes of the same (entity, predicate). The
        # skepticism gate requires the challenger's effective confidence
        # to beat the prior by threshold/2 (=0.25 for preferences), so
        # the first write needs to be weak enough that a follow-up
        # user-stated belief can supersede it.
        _make_pref(db_session, user=uid, value="dark_mode", mean=0.6, strength=5.0)
        bid2 = _make_pref(
            db_session, user=uid, value="light_mode", mean=0.95, strength=40.0
        )

        r = client.get(f"/v1/memory/{bid2}/history")
        assert r.status_code == 200
        body = r.json()
        assert body["entity"] == f"user:{uid}"
        assert body["predicate"] == "prefers"
        # Oldest first
        assert len(body["versions"]) == 2
        assert body["versions"][0]["value"] == "dark_mode"
        assert body["versions"][0]["is_current"] is False
        assert body["versions"][1]["value"] == "light_mode"
        assert body["versions"][1]["is_current"] is True

    def test_history_404_for_unknown_id(self, client, db_session, memory_on):
        _seed(db_session)
        r = client.get("/v1/memory/no-such-id/history")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "belief_not_found"

    def test_history_respects_user_scope(self, client, db_session, memory_on):
        """Two users with the same entity/predicate shape must not bleed."""
        _seed(db_session)
        alice_bid = _make_pref(db_session, user="api-hist-iso-alice")
        _make_pref(db_session, user="api-hist-iso-bob")

        r = client.get(f"/v1/memory/{alice_bid}/history")
        body = r.json()
        # Entity pattern is user-specific so filter by user_id is symbolic;
        # the real isolation comes from entity name containing the user id.
        for v in body["versions"]:
            assert v["entity"] == "user:api-hist-iso-alice"


# ────────────────────────────────────────────────────────────────────────
# GET /memory/{id}/explain
# ────────────────────────────────────────────────────────────────────────


class TestExplain:
    def test_explain_returns_signal_breakdown(
        self, client, db_session, memory_on
    ):
        _seed(db_session)
        uid = "api-explain-user"
        bid = _make_pref(db_session, user=uid)

        r = client.get(
            f"/v1/memory/{bid}/explain",
            params={"query_text": "what theme does the user prefer"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["belief"]["id"] == bid
        assert body["rrf_score"] > 0
        assert body["rank_in_scope"] >= 1
        # At least one of the RRF signals fired for a seeded preference
        # belief with matching keywords.
        names = {s["signal"] for s in body["signals"]}
        assert names, "expected at least one signal contribution"

    def test_explain_404_for_unknown_id(self, client, db_session, memory_on):
        _seed(db_session)
        r = client.get("/v1/memory/no-such/explain")
        assert r.status_code == 404


# ────────────────────────────────────────────────────────────────────────
# POST /memory/forget
# ────────────────────────────────────────────────────────────────────────


class TestForget:
    def test_forget_tombstones_matching_beliefs(
        self, client, db_session, memory_on
    ):
        _seed(db_session)
        uid = "api-forget-user"
        _make_pref(db_session, user=uid, value="dark_mode")
        _make_pref(db_session, user=uid, predicate="likes", value="coffee")

        r = client.post(
            "/v1/memory/forget",
            json={
                "entity": f"user:{uid}",
                "predicate": "prefers",
                "user_id": uid,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tombstoned"] == 1

        # `prefers` gone, `likes` still live.
        r_list = client.get("/v1/memory", params={"user_id": uid})
        preds = [b["predicate"] for b in r_list.json()["beliefs"]]
        assert preds == ["likes"]

    def test_forget_isolates_across_users(
        self, client, db_session, memory_on
    ):
        _seed(db_session)
        _make_pref(db_session, user="api-forget-alice")
        _make_pref(db_session, user="api-forget-bob")

        r = client.post(
            "/v1/memory/forget",
            json={"entity": "user:api-forget-alice", "user_id": "api-forget-alice"},
        )
        assert r.json()["tombstoned"] == 1

        # Bob's row untouched
        still_live = (
            db_session.query(Belief)
            .filter(
                Belief.user_id == "api-forget-bob",
                Belief.superseded_at.is_(None),
            )
            .count()
        )
        assert still_live == 1

    def test_forget_rejects_missing_entity(self, client, memory_on):
        r = client.post("/v1/memory/forget", json={})
        # FastAPI/pydantic validation — 422
        assert r.status_code == 422


# ────────────────────────────────────────────────────────────────────────
# GET /memory/stats
# ────────────────────────────────────────────────────────────────────────


class TestStats:
    def test_stats_counts_live_and_tombstoned(
        self, client, db_session, memory_on
    ):
        _seed(db_session)
        uid = "api-stats-user"
        b1 = _make_pref(db_session, user=uid, value="dark_mode")
        _make_pref(db_session, user=uid, predicate="likes", value="coffee")

        # Tombstone one
        row = db_session.query(Belief).filter(Belief.id == b1).one()
        row.superseded_at = datetime.now(UTC)
        db_session.commit()

        r = client.get("/v1/memory/stats")
        body = r.json()
        assert body["enabled"] is True
        # global counts (not user-scoped) — may include noise from sibling
        # tests in the same session, but both of our rows must be present.
        assert body["total_live"] >= 1
        assert body["total_tombstoned"] >= 1
        assert body["by_entity_type"].get("preference", 0) >= 1
        assert body["by_source_type"].get("user_stated", 0) >= 1
        assert body["decay_profile"]
