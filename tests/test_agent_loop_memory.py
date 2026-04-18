"""Integration tests for Phase 12 Week 2 — belief memory wired into run_agent.

These tests exercise the full memory path (MEMORY_ENABLED=True) and verify
that beliefs flow through retrieval → system-prompt injection → extractor →
writer → trace/episode bookkeeping. The MEMORY_ENABLED=False regression
contract is covered by tests/test_memory_regression.py (tripwire).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent.agent_loop import (
    _extract_and_persist_beliefs,
    _retrieve_beliefs,
    run_agent,
)
from app.core.memory.confidence import BetaConfidence
from app.core.memory.skepticism import BeliefDraft
from app.models.belief import Belief
from app.models.episode import Episode
from app.models.policy import Policy
from app.models.step_trace import StepTrace
from app.models.trace import Trace


@pytest.fixture(autouse=True)
def _purge_memory_rows(db_session):
    """Tests here call `write_belief` / `run_agent` which commit beliefs,
    traces, and episodes to the shared engine. Purge them after each
    test so the regression tripwire (which asserts zero belief rows
    with MEMORY_ENABLED=False) stays deterministic.

    Children first to respect FK constraints on SQLite with
    `PRAGMA foreign_keys=ON`.
    """
    yield
    db_session.rollback()
    db_session.query(StepTrace).delete()
    db_session.query(Episode).delete()
    db_session.query(Trace).delete()
    db_session.query(Belief).delete()
    db_session.commit()


def _seed_agent_and_memory_policies(db) -> None:
    """Seed the minimum policy set so a run_agent + memory write succeeds.

    Intentionally does NOT seed `respond:chat` — conftest already seeds
    `allow-chat-respond` at priority 10, and a lower-priority duplicate
    here would shadow it and break the Tier B regression golden's
    `governance.policy` field.
    """
    existing = {p.name for p in db.query(Policy).all()}
    wanted = [
        Policy(
            name="mem-test-allow-file-read",
            action_pattern="file_read",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=50,  # well below other test policies
        ),
        Policy(
            name="memory-allow-preference-write",
            action_pattern="memory:write:preference",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=10,
        ),
        Policy(
            name="memory-default-deny",
            action_pattern="memory:write:*",
            resource_pattern="*",
            decision="deny",
            risk_level="high",
            required_approvals="0",
            priority=100,
        ),
    ]
    for p in wanted:
        if p.name not in existing:
            db.add(p)
    db.commit()


def _reset_provider() -> None:
    from app.core.llm import provider as prov

    prov.reset_clients()


def _draft_preference(*, user_id: str, mean: float = 0.9, strength: float = 20.0) -> BeliefDraft:
    from app.core.memory.confidence import from_mean_and_strength

    return BeliefDraft(
        entity=f"user:{user_id}",
        predicate="prefers",
        value="dark_mode",
        entity_type="preference",
        confidence=from_mean_and_strength(mean, strength),
        source_type="user_stated",
        user_id=user_id,
        session_id="test-session",
        keywords=["dark", "mode", "theme", "prefers"],
        rationale="User said so in conversation.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# _retrieve_beliefs
# ─────────────────────────────────────────────────────────────────────────────


def test_retrieve_beliefs_noop_when_flag_off(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", False)
    text, ids = _retrieve_beliefs(
        db_session, "dark mode question", user_id="alice", session_id="s1"
    )
    assert text == ""
    assert ids == []


def test_retrieve_beliefs_returns_top_k_for_user(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", True)
    _seed_agent_and_memory_policies(db_session)

    from app.core.memory.writer import write_belief

    uid = "retrieve-topk-user"
    draft = _draft_preference(user_id=uid)
    out = write_belief(draft, db_session, source_trace_id=None)
    assert out.status == "accepted", out.reason
    db_session.commit()

    text, ids = _retrieve_beliefs(
        db_session,
        "what theme does the user prefer",
        user_id=uid,
        session_id="test-session",
    )
    assert ids, f"Expected belief ids, got empty. text={text!r}"
    assert len(ids) == 1
    assert text  # non-empty summary line
    assert "prefers" in text


def test_retrieve_beliefs_ignores_superseded_rows(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", True)
    _seed_agent_and_memory_policies(db_session)

    from datetime import UTC, datetime

    # Scope-unique user id so this test is independent of whatever other
    # tests write to 'alice'. The session-scoped db_engine is shared.
    uid = "ignore-superseded-user"
    b = Belief(
        id="dead-belief",
        entity=f"user:{uid}",
        predicate="prefers",
        value="light_mode",
        entity_type="preference",
        observed_at=datetime.now(UTC),
        superseded_at=datetime.now(UTC),  # tombstoned
        confidence_alpha=10.0,
        confidence_beta=1.0,
        source_type="user_stated",
        user_id=uid,
        keywords=["light", "mode"],
    )
    db_session.add(b)
    db_session.commit()

    _, ids = _retrieve_beliefs(
        db_session, "theme", user_id=uid, session_id="s1"
    )
    assert ids == []


def test_retrieve_beliefs_isolates_users(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", True)
    _seed_agent_and_memory_policies(db_session)

    from app.core.memory.writer import write_belief

    alice = "iso-alice"
    bob = "iso-bob"
    write_belief(_draft_preference(user_id=alice), db_session)
    write_belief(_draft_preference(user_id=bob), db_session)
    db_session.commit()

    _, alice_ids = _retrieve_beliefs(
        db_session, "theme", user_id=alice, session_id="s"
    )
    _, bob_ids = _retrieve_beliefs(
        db_session, "theme", user_id=bob, session_id="s"
    )
    assert alice_ids and bob_ids
    assert set(alice_ids).isdisjoint(set(bob_ids))


# ─────────────────────────────────────────────────────────────────────────────
# _extract_and_persist_beliefs
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_and_persist_noop_when_flag_off(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", False)
    called: list[bool] = []

    def _should_not_run(**_: object) -> list[BeliefDraft]:
        called.append(True)
        return []

    monkeypatch.setattr(
        "app.core.memory.extractor.extract_beliefs", _should_not_run
    )
    out = _extract_and_persist_beliefs(
        db_session,
        prompt="p",
        response="r",
        user_id="alice",
        session_id="s",
        trace_id="t",
    )
    assert out == []
    assert not called, "Extractor must not be called when MEMORY_ENABLED=False"


def test_extract_and_persist_writes_accepted_beliefs(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
    _seed_agent_and_memory_policies(db_session)

    uid = "extract-writes-user"
    drafts = [_draft_preference(user_id=uid)]

    def _fake_extract(**kwargs: object) -> list[BeliefDraft]:
        return drafts

    monkeypatch.setattr(
        "app.core.memory.extractor.extract_beliefs", _fake_extract
    )

    ids = _extract_and_persist_beliefs(
        db_session,
        prompt="user: I prefer dark mode.",
        response="Noted, dark mode preference saved.",
        user_id=uid,
        session_id="s-extract",
        trace_id="t-extract",
    )
    assert len(ids) == 1
    persisted = db_session.query(Belief).filter_by(id=ids[0]).one()
    assert persisted.user_id == uid
    assert persisted.entity_type == "preference"
    assert persisted.source_trace_id == "t-extract"
    assert persisted.extractor_version == "v1.0.0-preference"


def test_extract_and_persist_swallows_errors(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken extractor must never fail a successful agent run."""
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
    _seed_agent_and_memory_policies(db_session)

    def _broken(**_: object) -> list[BeliefDraft]:
        raise RuntimeError("extractor imploded")

    monkeypatch.setattr(
        "app.core.memory.extractor.extract_beliefs", _broken
    )
    ids = _extract_and_persist_beliefs(
        db_session,
        prompt="p",
        response="r",
        user_id="alice",
        session_id="s",
        trace_id="t",
    )
    assert ids == []


# ─────────────────────────────────────────────────────────────────────────────
# run_agent end-to-end with memory on
# ─────────────────────────────────────────────────────────────────────────────


def test_run_agent_injects_retrieved_beliefs_and_records_formed(
    tmp_path: Path,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: beliefs are retrieved pre-LLM, extractor fires post-final,
    trace + episode rows carry both belief id lists."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", True)
    monkeypatch.setattr(
        "app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path)
    )
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    _seed_agent_and_memory_policies(db_session)
    _reset_provider()

    uid = "runagent-happy-user"
    # Pre-seed one current belief for the user so retrieval has something.
    from app.core.memory.writer import write_belief

    prior_out = write_belief(
        _draft_preference(user_id=uid),
        db_session,
    )
    assert prior_out.status == "accepted"
    db_session.commit()
    prior_id = prior_out.belief_id

    # Make the extractor deterministic — return one new draft per run.
    new_drafts = [
        BeliefDraft(
            entity=f"user:{uid}",
            predicate="uses",
            value="sqlite",
            entity_type="preference",
            confidence=BetaConfidence(alpha=18.0, beta=2.0),
            source_type="user_stated",
            user_id=uid,
            session_id=None,
            keywords=["sqlite", "database", "uses"],
            rationale="Mentioned in turn.",
        )
    ]

    def _fake_extract(**_: object) -> list[BeliefDraft]:
        return new_drafts

    monkeypatch.setattr(
        "app.core.memory.extractor.extract_beliefs", _fake_extract
    )

    out = run_agent(
        "Read README and summarize my preferences.",
        model_id="mock",
        db_session=db_session,
        user_id=uid,
    )
    assert out.status == "completed", out.error

    # beliefs_used must include the pre-seeded belief.
    assert prior_id in out.beliefs_used
    # beliefs_formed must include the freshly extracted one.
    assert len(out.beliefs_formed) == 1
    formed_id = out.beliefs_formed[0]
    persisted = db_session.query(Belief).filter_by(id=formed_id).one()
    assert persisted.predicate == "uses"
    assert persisted.source_trace_id == out.trace_id

    # Trace row must carry both lists.
    trace = db_session.query(Trace).filter_by(id=out.trace_id).one()
    assert trace.beliefs_used == list(out.beliefs_used)
    assert trace.beliefs_formed == list(out.beliefs_formed)

    # Episode row must carry the same lists.
    ep = db_session.query(Episode).filter_by(trace_id=out.trace_id).one()
    assert ep.beliefs_used == list(out.beliefs_used)
    assert ep.beliefs_formed == list(out.beliefs_formed)


def test_run_agent_memory_off_leaves_columns_null(
    tmp_path: Path,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY_ENABLED=False keeps beliefs_used/beliefs_formed as NULL on
    persisted rows, matching the pre-memory default path exactly."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setattr("app.agent.agent_loop.settings.MEMORY_ENABLED", False)
    monkeypatch.setattr("app.config.settings.MEMORY_ENABLED", False)
    monkeypatch.setattr(
        "app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path)
    )
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    _seed_agent_and_memory_policies(db_session)
    _reset_provider()

    # Extractor should never run; wire a boom-on-call spy to prove it.
    def _boom(**_: object) -> list[BeliefDraft]:
        raise AssertionError("extractor ran with MEMORY_ENABLED=False")

    monkeypatch.setattr(
        "app.core.memory.extractor.extract_beliefs", _boom
    )

    out = run_agent(
        "Read README.",
        model_id="mock",
        db_session=db_session,
        user_id="off-path-user",
    )
    assert out.status == "completed"
    assert out.beliefs_used == []
    assert out.beliefs_formed == []

    trace = db_session.query(Trace).filter_by(id=out.trace_id).one()
    assert trace.beliefs_used is None
    assert trace.beliefs_formed is None
    ep = db_session.query(Episode).filter_by(trace_id=out.trace_id).one()
    assert ep.beliefs_used is None
    assert ep.beliefs_formed is None
