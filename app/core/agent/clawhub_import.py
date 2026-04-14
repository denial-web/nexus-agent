"""Import ClawHub-style SKILL.md into the Nexus Skill model."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from app.config import settings
from app.core.agent.clawhub_convert import markdown_to_steps
from app.core.immune.scanner import scan_input
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _split_front_matter(content: str) -> tuple[dict[str, Any], str]:
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    body = parts[2].lstrip("\n")
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _extract_requires(meta: dict[str, Any]) -> dict[str, Any] | None:
    md = meta.get("metadata")
    if isinstance(md, dict):
        oc = md.get("openclaw")
        if isinstance(oc, dict) and "requires" in oc:
            return oc["requires"]
    return None


def _slug_from_source(source_tag: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", source_tag)[:80].strip("-") or "imported"


def import_skill_md(
    *,
    content: str,
    db: Session,
    source_label: str,
    force: bool = False,
) -> str | None:
    """Parse SKILL.md content and persist a Skill. Returns skill id or None if blocked."""
    scan = scan_input(content[:50000])
    if scan.verdict.value == "block":
        logger.warning("Skill import blocked by immune scanner (%s)", source_label)
        return None

    meta, body = _split_front_matter(content)
    name = str(meta.get("name") or _slug_from_source(source_label))[:200]
    description = str(meta.get("description") or "")[:2000]
    requires = _extract_requires(meta)

    steps = markdown_to_steps(body)
    steps_json = json.dumps(steps, sort_keys=True, default=str)
    skill_hash = hashlib.sha256(steps_json.encode()).hexdigest()

    from app.models.skill import Skill

    existing = db.query(Skill).filter_by(skill_hash=skill_hash).first()
    if existing and not force:
        return existing.id

    if db.query(Skill).filter_by(name=name).first():
        name = f"{name}-{uuid.uuid4().hex[:8]}"

    skill = Skill(
        name=name,
        description=description or source_label[:500],
        source_episode_id=None,
        steps=steps,
        expected_reward=None,
        min_reward_threshold=0.5,
        total_runs=0,
        avg_reward=None,
        last_reward=None,
        enabled=True,
        flagged=False,
        skill_hash=skill_hash,
        immune_scanned=True,
        critic_scanned=False,
        source=source_label[:100],
        requirements=requires,
        raw_source=content[:500000],
    )
    db.add(skill)
    db.commit()
    logger.info("Imported skill %s from %s", skill.id, source_label)
    return skill.id


def import_skill_from_path(path: Path, db: Session, force: bool = False) -> str | None:
    content = path.read_text(encoding="utf-8")
    label = f"clawhub:{path.stem}"
    return import_skill_md(content=content, db=db, source_label=label, force=force)


def import_skill_from_url(url: str, db: Session, force: bool = False) -> str | None:
    if settings.LOCAL_ONLY:
        logger.warning("Skill import from URL blocked (LOCAL_ONLY)")
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        content = r.text
    host = parsed.netloc.replace(":", "_")[:40]
    label = f"import:url:{host}{parsed.path}"[:100]
    return import_skill_md(content=content, db=db, source_label=label, force=force)
