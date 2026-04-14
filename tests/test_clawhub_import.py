"""ClawHub-style SKILL.md import and instruction steps."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.agent.clawhub_convert import markdown_to_steps
from app.core.agent.clawhub_import import import_skill_md
from app.core.agent.skills import execute_skill
from app.models.skill import Skill


def test_markdown_to_steps_extracts_shell() -> None:
    md = "```bash\necho hello\n```\n"
    steps = markdown_to_steps(md)
    assert any(s.get("action") == "tool_call" and s.get("tool") == "shell_exec" for s in steps)


def test_markdown_to_steps_instruction_fallback() -> None:
    steps = markdown_to_steps("Do something nuanced that has no pattern.")
    assert any(s.get("action") == "instruction" for s in steps)


def test_import_skill_md_persists_raw_source(db_session) -> None:
    content = """---
name: test-skill-alpha
description: A test
metadata:
  openclaw:
    requires:
      bins: [curl]
---

Run `echo ok` then finish.
"""
    sid = import_skill_md(
        content=content,
        db=db_session,
        source_label="clawhub:test",
        force=True,
    )
    assert sid is not None
    row = db_session.query(Skill).filter_by(id=sid).first()
    assert row is not None
    assert row.raw_source is not None
    assert "test-skill-alpha" in row.raw_source
    assert row.source == "clawhub:test"
    assert row.requirements is not None


def test_import_skill_immune_block(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.agent import clawhub_import as chi
    from app.core.immune.scanner import ScanResult, Verdict

    def _block(_prompt: str, session_id: str | None = None) -> ScanResult:
        return ScanResult(verdict=Verdict.BLOCK, score=1.0, triggers=["x"], details={})

    monkeypatch.setattr(chi, "scan_input", _block)
    sid = import_skill_md(
        content="---\nname: bad\n---\n",
        db=db_session,
        source_label="x",
        force=True,
    )
    assert sid is None


def test_import_dedup_by_hash(db_session) -> None:
    content = "---\nname: dedup-skill\n---\nHello\n"
    a = import_skill_md(content=content, db=db_session, source_label="l1", force=False)
    b = import_skill_md(content=content, db=db_session, source_label="l2", force=False)
    assert a == b


def test_execute_skill_skips_instruction_steps(tmp_path: Path, db_session) -> None:
    from app.models.skill import Skill

    sk = Skill(
        name="instr-only",
        description="t",
        steps=[
            {"action": "instruction", "content": "Read the docs carefully."},
            {"action": "tool_call", "tool": "file_read", "arguments_template": {"path": "x.txt"}},
        ],
        enabled=True,
    )
    db_session.add(sk)
    db_session.commit()
    (tmp_path / "x.txt").write_text("ok", encoding="utf-8")
    ok, results, reward = execute_skill(sk.id, db_session, workspace=tmp_path)
    assert any(r.get("action") == "instruction" and r.get("skipped") for r in results)


def test_import_url_blocked_local_only(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.agent import clawhub_import as chi

    monkeypatch.setattr(chi.settings, "LOCAL_ONLY", True)
    sid = chi.import_skill_from_url("https://example.com/SKILL.md", db_session)
    assert sid is None


def test_api_import_skill(client, db_session) -> None:
    content = b"---\nname: api-import\n---\nTest\n"
    r = client.post(
        "/api/skills/import",
        files={"file": ("SKILL.md", content, "text/markdown")},
    )
    assert r.status_code == 200
    sid = r.json()["skill_id"]
    src = client.get(f"/api/skills/{sid}/source")
    assert src.status_code == 200
    assert "api-import" in src.text
