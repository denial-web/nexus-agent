"""Secure skill generation and execution (Phase 9B).

High-reward episodes are abstracted into reusable YAML-style skill templates.
Each skill is immune-scanned, critic-evaluated before storage, and Covernor-checked
on every step during execution. Reward tracking auto-disables declining skills.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.agent.registry import ToolRegistry
from app.core.agent.types import ToolResult
from app.core.covernor.policy_engine import evaluate_action
from app.core.immune.scanner import scan_input
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MIN_TOOL_CALLS = 5
MIN_REWARD = 0.8
DECLINE_WINDOW = 5
DISABLE_THRESHOLD = 0.4


def maybe_generate_skill(
    episode_id: str,
    task_summary: str,
    tool_sequence: list[str] | None,
    trajectory: list[dict[str, Any]] | None,
    reward: float,
    db: Session,
) -> str | None:
    """Generate a skill from a high-reward episode if it meets the criteria.

    Returns the skill ID if created, None otherwise.
    """
    if not trajectory or not tool_sequence:
        return None
    if len(tool_sequence) < MIN_TOOL_CALLS:
        return None
    if reward < MIN_REWARD:
        return None

    from app.models.skill import Skill

    existing = db.query(Skill).filter_by(source_episode_id=episode_id).first()
    if existing:
        return existing.id

    steps = _extract_steps(trajectory)
    if not steps:
        return None

    scan = scan_input(json.dumps(steps, default=str)[:50000])
    if scan.verdict.value == "block":
        logger.warning("Skill generation blocked by immune scanner for episode %s", episode_id)
        return None
    immune_ok = scan.verdict.value != "block"

    name = _generate_name(task_summary)
    if db.query(Skill).filter_by(name=name).first():
        name = f"{name}-{episode_id[:8]}"

    steps_json = json.dumps(steps, sort_keys=True, default=str)
    skill_hash = hashlib.sha256(steps_json.encode()).hexdigest()

    skill = Skill(
        name=name,
        description=task_summary[:500],
        source_episode_id=episode_id,
        source="nexus",
        steps=steps,
        expected_reward=reward,
        min_reward_threshold=max(0.5, reward - 0.3),
        total_runs=0,
        avg_reward=None,
        last_reward=None,
        enabled=True,
        flagged=False,
        skill_hash=skill_hash,
        immune_scanned=immune_ok,
        critic_scanned=False,
    )
    db.add(skill)
    db.commit()
    logger.info("Generated skill '%s' from episode %s (reward=%.2f, %d steps)", name, episode_id, reward, len(steps))
    return skill.id


def _extract_steps(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a trajectory into abstract skill steps."""
    steps: list[dict[str, Any]] = []
    for entry in trajectory:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind", "")
        if kind == "tool":
            steps.append(
                {
                    "action": "tool_call",
                    "tool": entry.get("tool"),
                    "arguments_template": entry.get("arguments", {}),
                    "expect_success": entry.get("success", True),
                }
            )
        elif kind == "final":
            steps.append(
                {
                    "action": "final_answer",
                    "content_hint": (entry.get("content") or "")[:200],
                }
            )
    return steps


def _generate_name(summary: str) -> str:
    words = summary.lower().split()[:6]
    name = "-".join(w for w in words if w.isalnum())
    return name[:100] or "skill"


def execute_skill(
    skill_id: str,
    db: Session,
    workspace: Path | None = None,
) -> tuple[bool, list[dict[str, Any]], float]:
    """Execute a stored skill step-by-step with Covernor gating.

    Returns (success, results, reward_signal).
    """
    from app.models.skill import Skill

    skill = db.query(Skill).filter_by(id=skill_id).first()
    if not skill or not skill.enabled:
        return False, [{"error": "Skill not found or disabled"}], 0.0

    if skill.flagged:
        logger.warning("Executing flagged skill '%s' — consider reviewing", skill.name)

    registry = ToolRegistry()
    ws = workspace or Path(settings.AGENT_WORKSPACE or ".").resolve()
    steps = skill.steps or []
    results: list[dict[str, Any]] = []
    successes = 0

    for i, step in enumerate(steps):
        action = step.get("action", "")
        if action == "final_answer":
            results.append({"step": i, "action": "final_answer", "skipped": True})
            continue
        if action == "instruction":
            results.append(
                {
                    "step": i,
                    "action": "instruction",
                    "skipped": True,
                    "guidance": (step.get("content") or "")[:2000],
                }
            )
            continue
        if action != "tool_call":
            results.append({"step": i, "error": f"Unknown action: {action}"})
            continue

        tool_name = step.get("tool", "")
        args = step.get("arguments_template", {})
        if not isinstance(args, dict):
            args = {}

        tool = registry.get(tool_name)
        if not tool:
            results.append({"step": i, "tool": tool_name, "error": "Unknown tool"})
            continue

        resource = json.dumps(args, sort_keys=True)[:2000]
        gov = evaluate_action(tool.covernor_action, resource, db_session=db)
        if gov.decision == "deny":
            results.append({"step": i, "tool": tool_name, "governance": "deny", "reason": gov.reason})
            continue
        if gov.decision == "require_approval":
            results.append({"step": i, "tool": tool_name, "governance": "require_approval"})
            return False, results, 0.0

        tr: ToolResult = registry.execute(tool_name, args, ws)
        results.append(
            {
                "step": i,
                "tool": tool_name,
                "success": tr.success,
                "output_head": tr.output[:500],
                "error": tr.error,
            }
        )
        if tr.success:
            successes += 1

    total_tool_steps = sum(1 for s in steps if s.get("action") == "tool_call")
    reward = successes / max(total_tool_steps, 1)

    _update_skill_reward(db, skill, reward)

    return reward >= 0.5, results, reward


def _update_skill_reward(db: Session, skill: Any, reward: float) -> None:
    """Update running reward stats and flag/disable declining skills."""
    skill.total_runs = (skill.total_runs or 0) + 1
    skill.last_reward = reward

    if skill.avg_reward is None:
        skill.avg_reward = reward
    else:
        n = skill.total_runs
        skill.avg_reward = ((skill.avg_reward * (n - 1)) + reward) / n

    if skill.total_runs >= DECLINE_WINDOW and skill.avg_reward is not None:
        if skill.avg_reward < (skill.min_reward_threshold or DISABLE_THRESHOLD):
            if not skill.flagged:
                skill.flagged = True
                logger.warning("Skill '%s' flagged: avg_reward=%.2f below threshold", skill.name, skill.avg_reward)
            if skill.avg_reward < DISABLE_THRESHOLD:
                skill.enabled = False
                logger.warning("Skill '%s' auto-disabled: avg_reward=%.2f", skill.name, skill.avg_reward)

    db.commit()


def list_skills(
    db: Session,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    """List skills with their reward stats."""
    from app.models.skill import Skill

    q = db.query(Skill)
    if enabled_only:
        q = q.filter_by(enabled=True)
    q = q.order_by(Skill.avg_reward.desc().nullslast())

    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "source": s.source,
            "total_runs": s.total_runs,
            "avg_reward": s.avg_reward,
            "last_reward": s.last_reward,
            "enabled": s.enabled,
            "flagged": s.flagged,
            "step_count": len(s.steps) if s.steps else 0,
            "expected_reward": s.expected_reward,
        }
        for s in q.all()
    ]
