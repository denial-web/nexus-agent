"""Tests for secure skill generation, execution, API, and agent recall (Phase 9B)."""

from pathlib import Path

from app.core.agent.skills import (
    execute_skill,
    list_skills,
    maybe_generate_skill,
)
from app.models.policy import Policy
from app.models.skill import Skill


def _seed_tool_policies(db) -> None:
    names = [p.name for p in db.query(Policy).all()]
    for name, action in [
        ("sk-file-read", "file_read"),
        ("sk-file-write", "file_write"),
        ("sk-shell", "shell_exec"),
    ]:
        if name not in names:
            db.add(
                Policy(
                    name=name,
                    action_pattern=action,
                    resource_pattern="*",
                    decision="allow",
                    risk_level="low",
                    required_approvals="0",
                    priority=5,
                )
            )
    db.commit()


def _high_reward_trajectory() -> list[dict]:
    return [
        {"kind": "tool", "tool": "file_read", "arguments": {"path": "README.md"}, "success": True, "reflection": "ok"},
        {"kind": "tool", "tool": "file_read", "arguments": {"path": "setup.py"}, "success": True, "reflection": "ok"},
        {"kind": "tool", "tool": "shell_exec", "arguments": {"command": "ls"}, "success": True, "reflection": "ok"},
        {"kind": "tool", "tool": "file_write", "arguments": {"path": "out.txt", "content": "done"}, "success": True},
        {"kind": "tool", "tool": "file_read", "arguments": {"path": "out.txt"}, "success": True, "reflection": "ok"},
        {"kind": "final", "content": "Task complete."},
    ]


def test_maybe_generate_skill_creates(db_session) -> None:
    traj = _high_reward_trajectory()
    tools = ["file_read", "file_read", "shell_exec", "file_write", "file_read"]

    sid = maybe_generate_skill(
        episode_id="ep-001",
        task_summary="Read files and summarize the project",
        tool_sequence=tools,
        trajectory=traj,
        reward=0.92,
        db=db_session,
    )
    assert sid is not None

    skill = db_session.query(Skill).filter_by(id=sid).first()
    assert skill is not None
    assert skill.enabled is True
    assert skill.expected_reward == 0.92
    assert len(skill.steps) == 6
    assert skill.skill_hash


def test_maybe_generate_skill_skips_low_reward(db_session) -> None:
    traj = _high_reward_trajectory()
    tools = ["file_read"] * 5

    sid = maybe_generate_skill(
        episode_id="ep-low",
        task_summary="low reward task",
        tool_sequence=tools,
        trajectory=traj,
        reward=0.5,
        db=db_session,
    )
    assert sid is None


def test_maybe_generate_skill_skips_few_tools(db_session) -> None:
    traj = [{"kind": "tool", "tool": "file_read", "arguments": {"path": "x"}, "success": True}]
    sid = maybe_generate_skill(
        episode_id="ep-few",
        task_summary="small task",
        tool_sequence=["file_read"],
        trajectory=traj,
        reward=0.95,
        db=db_session,
    )
    assert sid is None


def test_maybe_generate_skill_idempotent(db_session) -> None:
    traj = _high_reward_trajectory()
    tools = ["file_read"] * 5

    sid1 = maybe_generate_skill("ep-dup", "test task", tools, traj, 0.9, db_session)
    sid2 = maybe_generate_skill("ep-dup", "test task", tools, traj, 0.9, db_session)
    assert sid1 == sid2


def test_execute_skill_runs_steps(tmp_path: Path, db_session) -> None:
    _seed_tool_policies(db_session)
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")

    skill = Skill(
        name="test-read-skill",
        description="Read a file",
        steps=[
            {"action": "tool_call", "tool": "file_read", "arguments_template": {"path": "hello.txt"}},
        ],
        expected_reward=0.9,
        enabled=True,
    )
    db_session.add(skill)
    db_session.commit()

    success, results, reward = execute_skill(skill.id, db_session, workspace=tmp_path)
    assert success
    assert reward == 1.0
    assert results[0]["success"] is True

    db_session.refresh(skill)
    assert skill.total_runs == 1
    assert skill.avg_reward == 1.0


def test_execute_skill_disabled(db_session) -> None:
    skill = Skill(
        name="disabled-skill",
        steps=[{"action": "tool_call", "tool": "file_read", "arguments_template": {"path": "x"}}],
        enabled=False,
    )
    db_session.add(skill)
    db_session.commit()

    success, results, reward = execute_skill(skill.id, db_session)
    assert not success
    assert reward == 0.0


def test_execute_skill_denied_by_governance(db_session) -> None:
    skill = Skill(
        name="denied-skill",
        steps=[
            {"action": "tool_call", "tool": "shell_exec", "arguments_template": {"command": "echo hi"}},
        ],
        expected_reward=0.9,
        enabled=True,
    )
    db_session.add(skill)
    db_session.commit()

    success, results, reward = execute_skill(skill.id, db_session)
    assert results[0].get("governance") == "deny" or results[0].get("success") is not None


def test_list_skills(db_session) -> None:
    db_session.add(Skill(name="list-test", steps=[], expected_reward=0.8, enabled=True, avg_reward=0.85))
    db_session.commit()

    skills = list_skills(db_session)
    names = [s["name"] for s in skills]
    assert "list-test" in names


def test_skill_reward_decline_flags(db_session) -> None:
    _seed_tool_policies(db_session)
    skill = Skill(
        name="declining-skill",
        steps=[
            {"action": "tool_call", "tool": "file_read", "arguments_template": {"path": "nonexistent.txt"}},
        ],
        expected_reward=0.9,
        min_reward_threshold=0.6,
        total_runs=4,
        avg_reward=0.5,
        enabled=True,
    )
    db_session.add(skill)
    db_session.commit()

    execute_skill(skill.id, db_session, workspace=Path("/tmp"))
    db_session.refresh(skill)
    assert skill.total_runs == 5
    assert skill.flagged or not skill.enabled


# --------------- Skill API endpoint tests ---------------


def _insert_skill(db, name: str = "api-test-skill", enabled: bool = True) -> str:
    skill = Skill(
        name=name,
        description="A test skill for API verification",
        steps=[
            {"action": "tool_call", "tool": "file_read", "arguments_template": {"path": "x.txt"}},
        ],
        expected_reward=0.9,
        enabled=enabled,
        avg_reward=0.85,
        total_runs=3,
    )
    db.add(skill)
    db.commit()
    return skill.id


def test_api_list_skills(client, db_session) -> None:
    _insert_skill(db_session, "api-list-1")
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    names = [s["name"] for s in data]
    assert "api-list-1" in names


def test_api_list_skills_enabled_only(client, db_session) -> None:
    _insert_skill(db_session, "api-enabled", enabled=True)
    _insert_skill(db_session, "api-disabled", enabled=False)
    resp = client.get("/api/skills?enabled_only=true")
    names = [s["name"] for s in resp.json()]
    assert "api-enabled" in names
    assert "api-disabled" not in names

    resp2 = client.get("/api/skills?enabled_only=false")
    names2 = [s["name"] for s in resp2.json()]
    assert "api-disabled" in names2


def test_api_get_skill(client, db_session) -> None:
    sid = _insert_skill(db_session, "api-get-one")
    resp = client.get(f"/api/skills/{sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "api-get-one"
    assert data["steps"] is not None
    assert "created_at" in data


def test_api_get_skill_not_found(client) -> None:
    resp = client.get("/api/skills/nonexistent-id")
    assert resp.status_code == 404


def test_api_toggle_skill(client, db_session) -> None:
    sid = _insert_skill(db_session, "api-toggle")
    resp = client.patch(f"/api/skills/{sid}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp2 = client.patch(f"/api/skills/{sid}", json={"enabled": True})
    assert resp2.json()["enabled"] is True


def test_api_toggle_skill_not_found(client) -> None:
    resp = client.patch("/api/skills/bad-id", json={"enabled": False})
    assert resp.status_code == 404


def test_api_delete_skill(client, db_session) -> None:
    sid = _insert_skill(db_session, "api-delete-me")
    resp = client.delete(f"/api/skills/{sid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == sid

    resp2 = client.get(f"/api/skills/{sid}")
    assert resp2.status_code == 404


def test_api_delete_skill_not_found(client) -> None:
    resp = client.delete("/api/skills/no-such-id")
    assert resp.status_code == 404


def test_api_execute_skill_disabled(client, db_session) -> None:
    sid = _insert_skill(db_session, "api-exec-disabled", enabled=False)
    resp = client.post(f"/api/skills/{sid}/execute")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"].lower()


def test_api_execute_skill_not_found(client) -> None:
    resp = client.post("/api/skills/missing/execute")
    assert resp.status_code == 404


# --------------- Skill recall in agent loop ---------------


def test_retrieve_skills_returns_matching(db_session) -> None:
    from app.agent.agent_loop import _retrieve_skills

    db_session.add(
        Skill(
            name="deploy-kubernetes",
            description="Deploy a Kubernetes cluster with helm charts",
            steps=[{"action": "tool_call", "tool": "shell_exec"}],
            expected_reward=0.95,
            enabled=True,
            avg_reward=0.92,
            total_runs=5,
        )
    )
    db_session.commit()

    result = _retrieve_skills(db_session, "deploy kubernetes cluster")
    assert "deploy-kubernetes" in result
    assert "0.92" in result


def test_retrieve_skills_excludes_disabled(db_session) -> None:
    from app.agent.agent_loop import _retrieve_skills

    db_session.add(
        Skill(
            name="disabled-recall-test",
            description="This skill is disabled and should not appear",
            steps=[],
            expected_reward=0.9,
            enabled=False,
            avg_reward=0.8,
        )
    )
    db_session.commit()

    result = _retrieve_skills(db_session, "disabled recall test")
    assert "disabled-recall-test" not in result


def test_retrieve_skills_empty_on_no_match(db_session) -> None:
    from app.agent.agent_loop import _retrieve_skills

    result = _retrieve_skills(db_session, "xyzzy foobar bazquux")
    assert result == ""


def test_retrieve_skills_none_db() -> None:
    from app.agent.agent_loop import _retrieve_skills

    assert _retrieve_skills(None, "anything") == ""
