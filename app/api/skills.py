"""Skill CRUD and execution endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/skills", tags=["Skills"])


class SkillExecuteResponse(BaseModel):
    skill_id: str
    skill_name: str
    success: bool
    reward: float
    results: list[dict]


class SkillToggleRequest(BaseModel):
    enabled: bool


@router.post("/import")
def import_skill_endpoint(
    db: Session = Depends(get_db),
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    force: bool = Form(False),
) -> dict[str, str]:
    """Import a SKILL.md from upload or URL (v1 — no registry slug API)."""
    from app.core.agent.clawhub_import import import_skill_from_url, import_skill_md

    if url and settings.LOCAL_ONLY:
        raise HTTPException(status_code=503, detail="URL import disabled in LOCAL_ONLY mode")
    url_clean = (url or "").strip()
    has_file = file is not None and bool(getattr(file, "filename", None))
    if url_clean and has_file:
        raise HTTPException(status_code=400, detail="Provide either file or url, not both")
    if not url_clean and not has_file:
        raise HTTPException(status_code=400, detail="Provide file upload or url form field")

    if url_clean:
        sid = import_skill_from_url(url_clean, db, force=force)
    else:
        raw = file.file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File must be UTF-8 text") from None
        fname = file.filename or "skill.md"
        sid = import_skill_md(
            content=text,
            db=db,
            source_label=f"import:upload:{fname}"[:100],
            force=force,
        )
    if not sid:
        raise HTTPException(status_code=400, detail="Import blocked or failed")
    return {"skill_id": sid}


@router.get("")
def list_skills(
    enabled_only: bool = True,
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all skills with reward stats."""
    from app.core.agent.skills import list_skills as _list

    return _list(db, enabled_only=enabled_only)


@router.get("/{skill_id}/source", response_class=PlainTextResponse)
def get_skill_source(skill_id: str, db: Session = Depends(get_db)) -> str:
    """Return original SKILL.md text if stored."""
    from app.models.skill import Skill

    skill = db.query(Skill).filter_by(id=skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if not skill.raw_source:
        raise HTTPException(status_code=404, detail="No raw source stored for this skill")
    return skill.raw_source


@router.get("/{skill_id}")
def get_skill(skill_id: str, db: Session = Depends(get_db)) -> dict:
    """Get a single skill by ID."""
    from app.models.skill import Skill

    skill = db.query(Skill).filter_by(id=skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "steps": skill.steps,
        "total_runs": skill.total_runs,
        "avg_reward": skill.avg_reward,
        "last_reward": skill.last_reward,
        "expected_reward": skill.expected_reward,
        "min_reward_threshold": skill.min_reward_threshold,
        "enabled": skill.enabled,
        "flagged": skill.flagged,
        "skill_hash": skill.skill_hash,
        "immune_scanned": skill.immune_scanned,
        "critic_scanned": skill.critic_scanned,
        "source_episode_id": skill.source_episode_id,
        "source": skill.source,
        "requirements": skill.requirements,
        "has_raw_source": bool(skill.raw_source),
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
    }


@router.post("/{skill_id}/execute", response_model=SkillExecuteResponse)
def execute_skill_endpoint(
    skill_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Execute a skill step-by-step with Covernor gating on every tool call."""
    from app.core.agent.skills import execute_skill
    from app.models.skill import Skill

    skill = db.query(Skill).filter_by(id=skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if not skill.enabled:
        raise HTTPException(status_code=400, detail="Skill is disabled")

    ws = Path(settings.AGENT_WORKSPACE or ".").resolve()
    success, results, reward = execute_skill(skill_id, db, workspace=ws)

    return {
        "skill_id": skill_id,
        "skill_name": skill.name,
        "success": success,
        "reward": round(reward, 4),
        "results": results,
    }


@router.patch("/{skill_id}")
def toggle_skill(
    skill_id: str,
    body: SkillToggleRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Enable or disable a skill."""
    from app.models.skill import Skill

    skill = db.query(Skill).filter_by(id=skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    skill.enabled = body.enabled
    if body.enabled:
        skill.flagged = False
    db.commit()
    return {"id": skill.id, "name": skill.name, "enabled": skill.enabled}


@router.delete("/{skill_id}")
def delete_skill(skill_id: str, db: Session = Depends(get_db)) -> dict:
    """Delete a skill permanently."""
    from app.models.skill import Skill

    skill = db.query(Skill).filter_by(id=skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    db.delete(skill)
    db.commit()
    return {"deleted": skill_id, "name": skill.name}
