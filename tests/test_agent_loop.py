"""Agentic loop and API tests."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.agent.agent_loop import (
    _retrieve_episodes,
    compute_task_reward,
    run_agent,
)
from app.models.episode import Episode
from app.models.policy import Policy


def _seed_agent_policies(db) -> None:
    """Seed minimal agent tool policies so Covernor allows tool calls."""
    names = [p.name for p in db.query(Policy).all()]
    needed = [
        Policy(
            name="test-allow-file-read",
            action_pattern="file_read",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=5,
        ),
        Policy(
            name="test-allow-file-write",
            action_pattern="file_write",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=5,
        ),
        Policy(
            name="test-allow-shell",
            action_pattern="shell_exec",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=5,
        ),
        Policy(
            name="test-allow-web-fetch",
            action_pattern="web_fetch",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=5,
        ),
        Policy(
            name="test-allow-search",
            action_pattern="search",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=5,
        ),
    ]
    for p in needed:
        if p.name not in names:
            db.add(p)
    db.commit()


def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")


def _reset_provider() -> None:
    from app.core.llm import provider as prov

    prov.reset_clients()


# ── Reward computation ──────────────────────────────────────────────


def test_compute_task_reward_perfect() -> None:
    r = compute_task_reward([0.9, 0.8], "success", "good", 4, 0)
    assert 0.0 <= r <= 1.0
    assert r > 0.7


def test_compute_task_reward_failure() -> None:
    r = compute_task_reward([0.2], "failed", "bad", 10, 5)
    assert 0.0 <= r <= 1.0
    assert r < 0.4


def test_compute_task_reward_no_feedback() -> None:
    r = compute_task_reward([], "success", None, 3, 0)
    assert 0.0 <= r <= 1.0


# ── Mock agent loop ─────────────────────────────────────────────────


def test_run_agent_mock_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    _mock_env(monkeypatch)
    readme = tmp_path / "README.md"
    readme.write_text("# Test project\n", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _seed_agent_policies(db_session)
    _reset_provider()

    out = run_agent("Read README and summarize.", model_id="mock", db_session=db_session)
    assert out.status == "completed"
    assert out.response
    assert out.total_steps >= 1
    assert out.task_reward_score is not None


def test_run_agent_creates_step_traces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("# Hi\n", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _seed_agent_policies(db_session)
    _reset_provider()

    out = run_agent("Read README.", model_id="mock", db_session=db_session)
    assert out.status == "completed"

    from app.models.step_trace import StepTrace

    steps = db_session.query(StepTrace).filter_by(trace_id=out.trace_id).all()
    assert len(steps) >= 1
    assert steps[0].action_type == "tool_call"
    assert steps[0].tool_name == "file_read"


def test_run_agent_creates_episode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _seed_agent_policies(db_session)
    _reset_provider()

    out = run_agent("Read README.", model_id="mock", db_session=db_session)
    assert out.status == "completed"

    ep = db_session.query(Episode).filter_by(trace_id=out.trace_id).first()
    assert ep is not None
    assert ep.outcome == "success"
    assert ep.task_reward_score is not None


# ── Max steps exceeded ──────────────────────────────────────────────


def test_run_agent_max_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_MAX_STEPS", 1)
    _seed_agent_policies(db_session)
    _reset_provider()

    def _always_tool(step_index: int, user_prompt: str) -> dict:
        return {
            "action": "tool_call",
            "tool": "file_read",
            "arguments": {"path": "README.md"},
        }

    monkeypatch.setattr("app.agent.agent_loop._mock_agent_action", _always_tool)

    out = run_agent("Keep reading.", model_id="mock", db_session=db_session)
    assert out.status == "error"
    assert "Max agent steps" in (out.error or "")


# ── Denial feedback loop ────────────────────────────────────────────


def test_run_agent_denial_increments_corrections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    """When Covernor denies a tool, self_corrections should increase."""
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _seed_agent_policies(db_session)
    _reset_provider()

    db_session.add(
        Policy(
            name="test-deny-shell-sudo",
            action_pattern="shell_exec",
            resource_pattern="*sudo*",
            decision="deny",
            risk_level="high",
            required_approvals="0",
            priority=1,
        )
    )
    db_session.commit()

    call_count = 0

    def _denial_then_finish(step_index: int, user_prompt: str) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "action": "tool_call",
                "tool": "shell_exec",
                "arguments": {"command": "sudo reboot"},
            }
        return {"action": "final_answer", "content": "Done without sudo."}

    monkeypatch.setattr("app.agent.agent_loop._mock_agent_action", _denial_then_finish)

    out = run_agent("Do something.", model_id="mock", db_session=db_session)
    assert out.self_corrections >= 1


# ── LOCAL_ONLY routing ──────────────────────────────────────────────


def test_run_agent_local_only_remaps_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr("app.agent.agent_loop.settings.LOCAL_ONLY", True)
    _seed_agent_policies(db_session)
    _reset_provider()

    out = run_agent("Read README.", model_id="mock", db_session=db_session)
    assert out.status == "completed"


# ── Episodic memory retrieval ───────────────────────────────────────


def test_retrieve_episodes_returns_context(db_session) -> None:
    ep = Episode(
        task_summary="deploy the application to production",
        outcome="success",
        task_reward_score=0.92,
        reflection="Ran tests first, then deployed. All green.",
        tool_sequence=["shell_exec", "shell_exec"],
    )
    db_session.add(ep)
    db_session.commit()

    ctx = _retrieve_episodes(db_session, "deploy the app")
    assert "deploy" in ctx.lower() or "SUCCESS" in ctx


def test_retrieve_episodes_empty_on_no_match(db_session) -> None:
    ctx = _retrieve_episodes(db_session, "zzzzz_no_match_here")
    assert ctx == ""


def test_retrieve_episodes_no_db() -> None:
    ctx = _retrieve_episodes(None, "anything")
    assert ctx == ""


# ── Critic halt mid-task ────────────────────────────────────────────


def test_run_agent_halted_by_critic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    """If the Arbiter halts after a tool step, the agent stops."""
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _seed_agent_policies(db_session)
    _reset_provider()

    halt_score = MagicMock()
    halt_score.score = 0.1
    halt_score.verdict = "halt"

    halt_result = MagicMock()
    halt_result.verdict = "halt"
    halt_result.halted_by = "safety"
    halt_result.rollback_count = 0
    halt_result.scores = {"safety": {"score": 0.1, "verdict": "halt"}}

    monkeypatch.setattr(
        "app.agent.agent_loop._step_critic_summary",
        lambda *_a, **_kw: halt_result,
    )

    out = run_agent("Read README.", model_id="mock", db_session=db_session)
    assert out.status == "halted"
    assert "safety" in (out.error or "").lower()


# ── API endpoints ───────────────────────────────────────────────────


def test_agent_run_endpoint(client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_env(monkeypatch)
    (tmp_path / "README.md").write_text("# Hi\n", encoding="utf-8")
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _reset_provider()

    r = client.post(
        "/api/agent/agent/run",
        json={"prompt": "Summarize README", "model_id": "mock"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    assert "trace_id" in data
    assert data["total_steps"] >= 1


def test_agent_feedback_endpoint(client, db_session) -> None:
    from app.models.trace import Trace

    t = Trace(
        id="fb-test-001",
        session_id="s1",
        sequence=0,
        prompt="hello",
        prompt_hash="abc",
        immune_verdict="pass",
        status="completed",
        response="world",
        response_hash="def",
        run_mode="agent",
    )
    db_session.add(t)
    db_session.commit()

    r = client.post(
        "/api/agent/agent/feedback",
        json={"trace_id": "fb-test-001", "feedback": "good"},
    )
    assert r.status_code == 200
    assert r.json()["user_feedback"] == "good"


def test_agent_feedback_bad_value(client) -> None:
    r = client.post(
        "/api/agent/agent/feedback",
        json={"trace_id": "x", "feedback": "maybe"},
    )
    assert r.status_code == 400


def test_agent_resume_missing_trace(client) -> None:
    r = client.post(
        "/api/agent/agent/resume",
        json={"trace_id": "does-not-exist"},
    )
    assert r.status_code == 404


# ── Trajectory export ───────────────────────────────────────────────


def test_export_agent_trajectories(db_session) -> None:
    from app.core.training.labeler import export_agent_trajectories

    e = Episode(
        task_summary="test task",
        outcome="success",
        task_reward_score=0.95,
        reflection="done",
        tool_sequence=["file_read"],
    )
    db_session.add(e)
    db_session.commit()

    rows = export_agent_trajectories(db_session=db_session, batch_size=10)
    assert len(rows) >= 1
    assert rows[0]["type"] == "trajectory"
    assert rows[0]["metadata"]["label"] == "chosen"


def test_export_trajectories_with_stored_trajectory(db_session) -> None:
    from app.core.training.labeler import export_agent_trajectories

    e = Episode(
        task_summary="deploy app",
        outcome="success",
        task_reward_score=0.90,
        reflection="deployed fine",
        tool_sequence=["shell_exec"],
        agent_trajectory=[
            {
                "kind": "tool",
                "tool": "shell_exec",
                "arguments": {"command": "make deploy"},
                "success": True,
                "reflection": "Deployed OK",
            },
            {"kind": "final", "content": "App deployed successfully."},
        ],
    )
    db_session.add(e)
    db_session.commit()

    rows = export_agent_trajectories(db_session=db_session, batch_size=10)
    traj_row = [r for r in rows if r["metadata"].get("task_reward_score") == 0.90]
    assert traj_row
    msgs = traj_row[0]["messages"]
    assert len(msgs) >= 3
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) >= 1


def test_export_rejected_trajectories(db_session) -> None:
    from app.core.training.labeler import export_agent_trajectories

    e = Episode(
        task_summary="bad task",
        outcome="failed",
        task_reward_score=0.2,
        reflection="went wrong",
        tool_sequence=["shell_exec"],
    )
    db_session.add(e)
    db_session.commit()

    rows = export_agent_trajectories(db_session=db_session, min_reward=0.8, max_reward=0.4, batch_size=10)
    rejected = [r for r in rows if r["metadata"].get("label") == "rejected"]
    assert len(rejected) >= 1


# ── Built-in tools ──────────────────────────────────────────────────


def test_web_fetch_blocked_host() -> None:
    from app.core.agent.builtin import WebFetchTool

    tool = WebFetchTool()
    r = tool.execute({"url": "http://localhost:8080/secret"}, Path("/tmp"))
    assert not r.success
    assert "Blocked" in (r.error or "")


def test_web_fetch_blocked_internal_ip() -> None:
    from app.core.agent.builtin import WebFetchTool

    tool = WebFetchTool()
    r = tool.execute({"url": "http://169.254.169.254/latest/meta-data/"}, Path("/tmp"))
    assert not r.success
    assert "Blocked" in (r.error or "")


def test_web_fetch_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.agent.builtin import WebFetchTool

    monkeypatch.setattr("app.core.agent.builtin.settings.LOCAL_ONLY", True)
    tool = WebFetchTool()
    r = tool.execute({"url": "https://example.com"}, Path("/tmp"))
    assert not r.success
    assert "LOCAL_ONLY" in (r.error or "")


def test_search_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.agent.builtin import SearchTool

    monkeypatch.setattr("app.core.agent.builtin.settings.LOCAL_ONLY", True)
    tool = SearchTool()
    r = tool.execute({"query": "test"}, Path("/tmp"))
    assert not r.success
    assert "LOCAL_ONLY" in (r.error or "")


def test_file_read_path_escape(tmp_path: Path) -> None:
    from app.core.agent.builtin import FileReadTool

    tool = FileReadTool()
    r = tool.execute({"path": "/etc/passwd"}, tmp_path)
    assert not r.success
    assert "escape" in (r.error or "").lower()


def test_shell_exec_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.agent.builtin import ShellExecTool

    monkeypatch.setattr("app.core.agent.builtin.settings.AGENT_SHELL_TIMEOUT", 0.1)
    tool = ShellExecTool()
    r = tool.execute({"command": "sleep 10"}, tmp_path)
    assert not r.success
    assert "timed out" in (r.error or "").lower()


# ── Integration: agent → episode → skill → recall cycle ─────────


def test_full_learning_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    """End-to-end: run agent → persist episode → generate skill → recall skill in new run."""
    from app.agent.agent_loop import _retrieve_skills
    from app.core.agent.skills import maybe_generate_skill
    from app.models.episode import Episode
    from app.models.skill import Skill

    _mock_env(monkeypatch)
    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_WORKSPACE", str(tmp_path))
    _seed_agent_policies(db_session)
    _reset_provider()
    (tmp_path / "data.txt").write_text("42", encoding="utf-8")

    monkeypatch.setattr("app.agent.agent_loop.settings.AGENT_MAX_STEPS", 10)
    result = run_agent(
        prompt="Read data.txt and report its content",
        db_session=db_session,
        model_id="mock",
    )
    assert result.status in ("completed", "halted")
    assert result.total_steps >= 1

    episodes = db_session.query(Episode).all()
    assert len(episodes) >= 1, "Episode should be persisted after agent run"
    ep = episodes[0]
    assert ep.task_summary is not None

    tool_seq = ["file_read"] * 6
    traj = [
        {"kind": "tool", "tool": "file_read", "arguments": {"path": f"f{i}.txt"}, "success": True} for i in range(6)
    ] + [{"kind": "final", "content": "Done reading files"}]

    sid = maybe_generate_skill(
        episode_id=ep.id,
        task_summary="Read multiple files and summarize content",
        tool_sequence=tool_seq,
        trajectory=traj,
        reward=0.92,
        db=db_session,
    )
    assert sid is not None
    skill = db_session.query(Skill).filter_by(id=sid).first()
    assert skill is not None
    assert skill.enabled is True

    recalled = _retrieve_skills(db_session, "Read files and summarize content")
    assert skill.name in recalled, "Skill should be recalled for a matching task"

    unrelated = _retrieve_skills(db_session, "deploy kubernetes cluster production")
    assert skill.name not in unrelated, "Skill should NOT be recalled for unrelated task"
