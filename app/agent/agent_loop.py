"""
Multi-step agentic loop with governance, reflection, and critic feedback.

Implements Phase 8 `run_agent()` and episode persistence (Phase 9).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.agent.pipeline import PipelineResult, _record_run, get_arbiter
from app.config import settings
from app.core.agent.registry import ToolRegistry
from app.core.agent.types import RegisteredTool
from app.core.covernor.policy_engine import evaluate_action
from app.core.immune.scanner import Verdict, harden_prompt, scan_input, scan_output
from app.core.llm.provider import _resolve_route, generate
from app.core.training.labeler import push_failure
from app.metrics import record_critic_scores

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


@dataclass
class AgentRunResult:
    """Result of run_agent — extends pipeline fields."""

    trace_id: str
    session_id: str
    status: str
    response: str | None = None
    error: str | None = None
    immune_input: dict = field(default_factory=dict)
    immune_output: dict = field(default_factory=dict)
    critic_result: dict = field(default_factory=dict)
    governance: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    model_id_used: str | None = None
    token_count: int | None = None
    approval_request_id: str | None = None
    task_reward_score: float | None = None
    total_steps: int = 0
    self_corrections: int = 0
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    agent_state: dict[str, Any] | None = None


def _retrieve_episodes(db: Session | None, task: str, limit: int = 3) -> str:
    """Retrieve reward-scored past episodes similar to the current task (Level 2 memory)."""
    if not db:
        return ""
    try:
        from app.models.episode import Episode

        keywords = [w for w in task.lower().split() if len(w) > 3][:6]
        if not keywords:
            return ""
        q = db.query(Episode).filter(Episode.task_reward_score.isnot(None))
        candidates = q.order_by(Episode.task_reward_score.desc()).limit(50).all()
        scored: list[tuple[float, Any]] = []
        for ep in candidates:
            summary_lower = (ep.task_summary or "").lower()
            hits = sum(1 for kw in keywords if kw in summary_lower)
            if hits > 0:
                scored.append((hits * (ep.task_reward_score or 0.0), ep))
        scored.sort(key=lambda x: x[0], reverse=True)
        lines: list[str] = []
        for _score, ep in scored[:limit]:
            label = "SUCCESS" if (ep.task_reward_score or 0) >= 0.7 else "FAILURE"
            tools = ", ".join(ep.tool_sequence) if ep.tool_sequence else "none"
            refl = (ep.reflection or "")[:300]
            lines.append(
                f"- [{label}, reward={ep.task_reward_score:.2f}] "
                f"Task: {ep.task_summary[:200]} | Tools: {tools} | Reflection: {refl}"
            )
        return "\n".join(lines)
    except Exception:
        logger.debug("Episode retrieval failed", exc_info=True)
        return ""


def _retrieve_skills(db: Session | None, task: str, limit: int = 3) -> str:
    """Retrieve enabled skills whose descriptions match the current task."""
    if not db:
        return ""
    try:
        from app.models.skill import Skill

        keywords = [w for w in task.lower().split() if len(w) > 3][:6]
        if not keywords:
            return ""
        candidates = (
            db.query(Skill).filter_by(enabled=True).order_by(Skill.avg_reward.desc().nullslast()).limit(30).all()
        )
        scored: list[tuple[float, Any]] = []
        for sk in candidates:
            desc_lower = (sk.description or "").lower()
            name_lower = (sk.name or "").lower()
            hits = sum(1 for kw in keywords if kw in desc_lower or kw in name_lower)
            if hits > 0:
                reward = sk.avg_reward if sk.avg_reward is not None else sk.expected_reward
                scored.append((hits * (reward or 0.0), sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        lines: list[str] = []
        for _score, sk in scored[:limit]:
            step_names = [s.get("tool", s.get("action", "?")) for s in (sk.steps or [])]
            avg = f"{sk.avg_reward:.2f}" if sk.avg_reward is not None else "n/a"
            lines.append(
                f"- Skill '{sk.name}' (avg_reward={avg}, runs={sk.total_runs}): "
                f"{sk.description[:200]} | Steps: {', '.join(step_names)}"
            )
        return "\n".join(lines)
    except Exception:
        logger.debug("Skill retrieval failed", exc_info=True)
        return ""


def _workspace_path() -> Path:
    raw = (settings.AGENT_WORKSPACE or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _parse_json_action(text: str) -> dict[str, Any]:
    t = text.strip()
    m = _JSON_FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return {"action": "final_answer", "content": t}


def _mock_agent_action(step_index: int, user_prompt: str) -> dict[str, Any]:
    if step_index == 0:
        return {
            "action": "tool_call",
            "tool": "file_read",
            "arguments": {"path": "README.md"},
        }
    return {
        "action": "final_answer",
        "content": f"Mock agent finished after tool step. Original task: {user_prompt[:120]}...",
    }


def _tool_resource(tool: RegisteredTool, args: dict[str, Any]) -> str:
    if tool.name == "shell_exec":
        return str(args.get("command", ""))[:2000]
    if tool.name in ("file_read", "file_write"):
        return str(args.get("path", ""))[:2000]
    if tool.name == "web_fetch":
        return str(args.get("url", ""))[:2000]
    if tool.name == "search":
        return str(args.get("query", ""))[:2000]
    return json.dumps(args, sort_keys=True)[:2000]


def _verifiable_reward(tr: Any) -> float:
    if tr.success:
        return 1.0
    return 0.0


def _reflect_text(
    tool_name: str,
    tool_result: Any,
    model_id: str | None,
) -> str:
    if not settings.AGENT_REFLECT_ON_SUCCESS and tool_result.success:
        return "Step succeeded; proceeding."
    prompt = (
        f"Tool `{tool_name}` finished.\n"
        f"Success: {tool_result.success}\n"
        f"Output (truncated):\n{tool_result.output[:4000]}\n"
        f"Error: {tool_result.error or 'none'}\n"
        "In one or two sentences: what happened and should the plan change?"
    )
    try:
        r = generate(prompt, model_id=model_id)
        return (r.text or "").strip()[:2000]
    except Exception:
        logger.warning("Reflection LLM failed", exc_info=True)
        return "Reflection skipped."


def _step_critic_summary(
    arbiter: Any,
    user_task: str,
    summary: str,
    trace_id: str,
    model_id: str | None,
) -> dict[str, Any]:
    ctx = {
        "prompt": user_task,
        "response": summary[:12000],
        "model_id": model_id or "mock",
        "trace_id": trace_id,
    }
    return arbiter.evaluate(ctx)


def _avg_critic_score(scores_dict: dict[str, Any]) -> float:
    vals: list[float] = []
    for _k, v in scores_dict.items():
        if isinstance(v, dict) and "score" in v:
            vals.append(float(v["score"]))
    return sum(vals) / len(vals) if vals else 0.5


def compute_task_reward(
    step_critic_avgs: list[float],
    outcome: str,
    user_feedback: str | None,
    total_steps: int,
    self_corrections: int,
) -> float:
    critic_part = sum(step_critic_avgs) / len(step_critic_avgs) if step_critic_avgs else 0.5
    if outcome == "success":
        completion = 1.0
    elif outcome in ("partial", "pending_approval"):
        completion = 0.5
    else:
        completion = 0.0
    if user_feedback == "good":
        user_part = 1.0
    elif user_feedback == "bad":
        user_part = 0.0
    else:
        user_part = 0.5
    denom = max(total_steps, 1)
    efficiency = max(0.0, min(1.0, (settings.AGENT_MAX_STEPS - self_corrections) / denom))
    return 0.4 * critic_part + 0.25 * completion + 0.2 * user_part + 0.15 * efficiency


def _insert_stub_trace(
    db: Session,
    trace_id: str,
    session_id: str,
    prompt: str,
    immune: dict,
    sequence: int,
    prev_hash: str,
) -> None:
    from app.models.trace import Trace
    from app.services.integrity import compute_trace_hash

    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    ph = hashlib.sha256(b"").hexdigest()
    th = compute_trace_hash(trace_id, prev_hash, prompt_hash, ph, "agent_running")
    row = Trace(
        id=trace_id,
        session_id=session_id,
        sequence=sequence,
        prompt=prompt,
        prompt_hash=prompt_hash,
        immune_verdict=immune.get("verdict", "unknown"),
        immune_score=immune.get("score"),
        immune_details=immune,
        status="agent_running",
        response="",
        response_hash=ph,
        run_mode="agent",
        prev_hash=prev_hash,
        trace_hash=th,
    )
    db.add(row)
    db.commit()


def _update_trace_final(
    db: Session,
    trace_id: str,
    prompt: str,
    result: AgentRunResult,
    user_feedback: str | None,
) -> None:
    from app.models.trace import Trace
    from app.services.integrity import cascade_rehash_from_trace, compute_trace_hash

    row = db.query(Trace).filter_by(id=trace_id).with_for_update().first()
    if not row:
        return
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    resp = result.response or ""
    response_hash = hashlib.sha256(resp.encode()).hexdigest() if resp else None
    th = compute_trace_hash(
        trace_id,
        row.prev_hash or "genesis",
        prompt_hash,
        response_hash,
        result.status,
    )
    row.prompt_hash = prompt_hash
    row.response = resp
    row.response_hash = response_hash
    row.status = result.status
    row.error = result.error
    row.latency_ms = result.latency_ms
    row.model_id = result.model_id_used
    row.token_count = result.token_count
    row.critic_verdict = result.critic_result.get("verdict")
    row.critic_scores = result.critic_result.get("scores")
    row.critic_rollback_count = result.critic_result.get("rollback_count", 0)
    row.governance_status = result.governance.get("decision")
    row.governance_policy_id = result.governance.get("policy_id")
    row.output_scan_verdict = (result.immune_output or {}).get("verdict")
    row.run_mode = "agent"
    row.trace_hash = th
    row.task_reward_score = result.task_reward_score
    row.user_feedback = user_feedback
    row.total_steps = result.total_steps
    row.self_corrections = result.self_corrections
    row.agent_trajectory = result.trajectory
    row.agent_state = result.agent_state
    db.commit()
    cascade_rehash_from_trace(db, trace_id)


def _persist_step_row(
    db: Session,
    trace_id: str,
    step_number: int,
    action_type: str,
    tool_name: str | None,
    tool_args: dict | None,
    tool_result: dict | None,
    covernor_decision: str | None,
    critic_scores: dict | None,
    reflection: str | None,
    reward_signal: float | None,
) -> None:
    from app.models.step_trace import StepTrace

    st = StepTrace(
        trace_id=trace_id,
        step_number=step_number,
        action_type=action_type,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        covernor_decision=covernor_decision,
        critic_scores=critic_scores,
        reflection=reflection,
        reward_signal=reward_signal,
    )
    db.add(st)
    db.flush()


def _episode_persist(
    db: Session,
    result: AgentRunResult,
    prompt: str,
    user_feedback: str | None,
) -> str | None:
    """Persist an episode and return its ID."""
    from app.models.episode import Episode

    outcome = "success" if result.status == "completed" else "failed"
    if result.status == "halted":
        outcome = "halted"
    elif result.status == "pending_approval":
        outcome = "partial"
    tools_used = [t.get("tool") for t in result.trajectory if t.get("kind") == "tool"]
    tr_score = result.task_reward_score
    if tr_score is None and result.status in ("completed", "halted", "blocked"):
        oc = "success" if result.status == "completed" else "failed"
        tr_score = compute_task_reward(
            [],
            oc,
            user_feedback,
            result.total_steps,
            result.self_corrections,
        )
    ep = Episode(
        trace_id=result.trace_id,
        session_id=result.session_id,
        task_summary=prompt[:8000],
        tool_sequence=tools_used,
        outcome=outcome,
        task_reward_score=tr_score,
        user_feedback=user_feedback,
        reflection=(result.response or result.error or "")[:8000],
        step_count=result.total_steps,
        self_corrections=result.self_corrections,
        agent_trajectory=result.trajectory,
    )
    db.add(ep)
    db.commit()
    return ep.id


def run_agent(
    prompt: str,
    session_id: str | None = None,
    model_id: str | None = None,
    db_session: Session | None = None,
    user_feedback: str | None = None,
    resume_state: dict[str, Any] | None = None,
) -> AgentRunResult:
    """
    Zero-trust agentic loop: plan → tool (governed) → reflect → critic → repeat.

    If a tool requires approval, returns status ``pending_approval`` with ``agent_state`` for resume.
    """
    start = time.time()
    trace_id = resume_state.get("trace_id") if resume_state else uuid.uuid4().hex
    session_id = session_id or (resume_state.get("session_id") if resume_state else None) or uuid.uuid4().hex
    workspace = _workspace_path()
    registry = ToolRegistry()
    arbiter = get_arbiter(db_session)

    result = AgentRunResult(trace_id=trace_id, session_id=session_id, status="running")
    messages: list[dict[str, str]] = []
    step_index = 0
    self_corrections = resume_state.get("self_corrections", 0) if resume_state else 0
    step_critic_avgs: list[float] = []
    token_total = 0

    if resume_state:
        messages = list(resume_state.get("messages", []))
        step_index = int(resume_state.get("step_index", 0))
        step_critic_avgs = list(resume_state.get("step_critic_avgs", []))
        pending = resume_state.get("pending_tool")
        if pending:
            tool_name = str(pending["tool"])
            args = pending.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            tool = registry.get(tool_name)
            if not tool:
                result.status = "error"
                result.error = f"Unknown tool in resume: {tool_name}"
                result.latency_ms = _elapsed(start)
                return result
            tr = registry.execute(tool_name, args, workspace)
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps({"action": "tool_call", "tool": tool_name, "arguments": args}),
                }
            )
            messages.append({"role": "user", "content": f"Tool result ({tool_name}):\n{tr.output}\nerror={tr.error}"})
            result.trajectory.append(
                {
                    "kind": "tool",
                    "tool": tool_name,
                    "arguments": args,
                    "success": tr.success,
                    "resume": True,
                }
            )
            step_index += 1
        result.immune_input = {"verdict": "pass", "score": 0.0, "triggers": [], "resumed": True}
    else:
        input_scan = scan_input(prompt, session_id=session_id)
        result.immune_input = {
            "verdict": input_scan.verdict.value,
            "score": input_scan.score,
            "triggers": input_scan.triggers,
        }
        if input_scan.verdict == Verdict.BLOCK:
            result.status = "blocked"
            result.error = "Input blocked by immune scanner"
            result.latency_ms = _elapsed(start)
            return result
        if input_scan.verdict == Verdict.FLAG:
            hardened, removed = harden_prompt(prompt)
            if removed and hardened.strip():
                prompt = hardened
                result.immune_input["hardened"] = True
            elif removed:
                result.status = "blocked"
                result.error = "Prompt entirely composed of flagged content"
                result.latency_ms = _elapsed(start)
                return result
        messages = [{"role": "user", "content": prompt}]

    tools_json = json.dumps(registry.tool_definitions_json(), indent=2)
    episode_context = _retrieve_episodes(db_session, prompt) if db_session and not resume_state else ""
    skill_context = _retrieve_skills(db_session, prompt) if db_session and not resume_state else ""
    system = (
        "You are a governed agent. Reply with a single JSON object only, no markdown, either:\n"
        '{"action":"tool_call","tool":"<name>","arguments":{...}}\n'
        'or {"action":"final_answer","content":"<markdown or plain text summary>"}\n'
        f"Available tools:\n{tools_json}\n"
        "Obey workspace safety: use paths under the working directory unless policy allows otherwise."
    )
    if skill_context:
        system += f"\n\nReusable skills (follow these proven steps if they match your task):\n{skill_context}"
    if episode_context:
        system += f"\n\nPast experience (use to guide your plan):\n{episode_context}"

    if db_session and not resume_state:
        from app.models.trace import Trace

        prev = (
            db_session.query(Trace)
            .filter_by(session_id=session_id)
            .order_by(Trace.sequence.desc(), Trace.created_at.desc())
            .first()
        )
        prev_hash = prev.trace_hash if prev and prev.trace_hash else "genesis"
        seq = (prev.sequence + 1) if prev and prev.sequence is not None else 0
        _insert_stub_trace(db_session, trace_id, session_id, prompt, result.immune_input, seq, prev_hash)

    max_steps = settings.AGENT_MAX_STEPS
    model_route = model_id
    if settings.LOCAL_ONLY:
        mid = (model_id or "").strip().lower()
        if not mid.startswith(("local", "ollama", "mock")):
            model_route = f"ollama:{settings.OLLAMA_DEFAULT_MODEL}"

    while step_index < max_steps:
        user_block = json.dumps(messages[-4:], default=str) if len(messages) > 4 else json.dumps(messages, default=str)
        planner_prompt = f"{system}\n\nConversation (recent):\n{user_block}\n\nWhat is the next JSON action?"
        try:
            route_provider, _, _ = _resolve_route(model_route)
            if route_provider == "mock":
                llm_text = json.dumps(_mock_agent_action(step_index, prompt))
                used_model = "mock"
                tok = 0
            else:
                llm = generate(planner_prompt, model_id=model_route, system_prompt=None)
                llm_text = llm.text
                used_model = llm.model_id
                tok = llm.token_count or 0
                token_total += tok
        except Exception as exc:
            logger.exception("Agent LLM failed")
            result.status = "error"
            result.error = str(exc) or "LLM error"
            break

        result.model_id_used = used_model
        action = _parse_json_action(llm_text)
        act = action.get("action", "final_answer")

        if act == "final_answer":
            content = str(action.get("content", ""))
            out_scan = scan_output(content)
            result.immune_output = {
                "verdict": out_scan.verdict.value,
                "score": out_scan.score,
                "triggers": out_scan.triggers,
            }
            if out_scan.verdict == Verdict.BLOCK:
                result.status = "blocked"
                result.error = "Final answer blocked by output scanner"
                result.response = None
                break
            final_ctx = _step_critic_summary(arbiter, prompt, content, trace_id, model_route)
            result.critic_result = {
                "verdict": final_ctx.verdict,
                "scores": {k: v for k, v in final_ctx.scores.items()},
                "rollback_count": final_ctx.rollback_count,
                "halted_by": final_ctx.halted_by,
            }
            record_critic_scores(final_ctx.scores)
            step_critic_avgs.append(_avg_critic_score(final_ctx.scores))
            if final_ctx.verdict == "halt":
                result.status = "halted"
                result.total_steps = step_index + 1
                result.error = f"Halted by critic: {final_ctx.halted_by}"
                if db_session:
                    push_failure(
                        trace_id=trace_id,
                        source_node="arbiter",
                        failure_type="halt",
                        prompt=prompt,
                        response=content,
                        critic_output=result.critic_result,
                        db_session=db_session,
                    )
                break
            gov = evaluate_action("respond", "chat", db_session=db_session)
            result.governance = {
                "decision": gov.decision,
                "policy": gov.policy_name,
                "policy_id": gov.policy_id,
                "risk_level": gov.risk_level,
            }
            if gov.decision == "deny":
                result.status = "blocked"
                result.error = gov.reason
                break
            if gov.decision == "require_approval":
                result.status = "pending_approval"
                result.response = content
                result.agent_state = {
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "messages": messages,
                    "step_index": step_index,
                    "self_corrections": self_corrections,
                    "step_critic_avgs": step_critic_avgs,
                    "pending_final": True,
                }
                aid = _create_approval(db_session, trace_id, "agent_final", {"messages": messages}, gov)
                if aid:
                    result.approval_request_id = aid
                break
            result.status = "completed"
            result.response = content
            result.total_steps = step_index + 1
            result.self_corrections = self_corrections
            result.task_reward_score = compute_task_reward(
                step_critic_avgs,
                "success",
                user_feedback,
                result.total_steps,
                self_corrections,
            )
            result.trajectory.append({"kind": "final", "content": content[:500]})
            break

        if act != "tool_call":
            result.status = "error"
            result.error = f"Unknown action: {act}"
            break

        tool_name = str(action.get("tool", "")).strip()
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        tool = registry.get(tool_name)
        if not tool:
            messages.append({"role": "assistant", "content": llm_text})
            messages.append({"role": "user", "content": f"Error: unknown tool {tool_name}"})
            self_corrections += 1
            step_index += 1
            continue

        resource = _tool_resource(tool, arguments)
        gov_tool = evaluate_action(
            tool.covernor_action,
            resource,
            parameters=None,
            db_session=db_session,
        )
        if gov_tool.decision == "deny":
            messages.append({"role": "assistant", "content": llm_text})
            messages.append(
                {
                    "role": "user",
                    "content": f"Governance denied {tool_name}: {gov_tool.reason}. Try a different approach.",
                }
            )
            self_corrections += 1
            if db_session:
                _persist_step_row(
                    db_session,
                    trace_id,
                    step_index,
                    "tool_call",
                    tool_name,
                    arguments,
                    None,
                    "deny",
                    None,
                    None,
                    0.0,
                )
                db_session.commit()
            step_index += 1
            continue

        if gov_tool.decision == "require_approval":
            result.status = "pending_approval"
            result.agent_state = {
                "trace_id": trace_id,
                "session_id": session_id,
                "messages": messages,
                "step_index": step_index,
                "self_corrections": self_corrections,
                "step_critic_avgs": step_critic_avgs,
                "pending_tool": {"tool": tool_name, "arguments": arguments},
            }
            aid = _create_approval(
                db_session,
                trace_id,
                "agent_tool",
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "agent_state": result.agent_state,
                },
                gov_tool,
            )
            if aid:
                result.approval_request_id = aid
            if db_session:
                from app.models.trace import Trace as _Trace

                _pending_trace = db_session.query(_Trace).filter_by(id=trace_id).first()
                if _pending_trace:
                    _pending_trace.agent_state = result.agent_state
                    _pending_trace.status = "pending_approval"
                    db_session.commit()
            result.response = f"Pending approval for tool {tool_name}"
            break

        tr = registry.execute(tool_name, arguments, workspace)
        refl = _reflect_text(tool_name, tr, model_route)
        if not tr.success:
            self_corrections += 1

        step_summary = f"Tool {tool_name} success={tr.success}. Output head:\n{tr.output[:1500]}"
        crit = _step_critic_summary(arbiter, prompt, step_summary, trace_id, model_route)
        record_critic_scores(crit.scores)
        step_critic_avgs.append(_avg_critic_score(crit.scores))
        warn_msg = ""
        if crit.verdict == "halt":
            result.status = "halted"
            result.total_steps = step_index + 1
            result.error = f"Halted by critic on step: {crit.halted_by}"
            if db_session:
                push_failure(
                    trace_id=trace_id,
                    source_node="arbiter",
                    failure_type="halt",
                    prompt=prompt,
                    response=step_summary,
                    critic_output={"scores": crit.scores},
                    db_session=db_session,
                )
            break
        if crit.verdict == "rollback":
            warn_msg = "Critic suggests caution on this step; prefer a safer approach."

        messages.append({"role": "assistant", "content": llm_text})
        messages.append(
            {
                "role": "user",
                "content": f"Tool {tool_name} result:\n{tr.output[:8000]}\n{warn_msg}\nReflection: {refl}",
            }
        )
        result.trajectory.append(
            {
                "kind": "tool",
                "tool": tool_name,
                "arguments": arguments,
                "success": tr.success,
                "reflection": refl,
            }
        )
        if db_session:
            _persist_step_row(
                db_session,
                trace_id,
                step_index,
                "tool_call",
                tool_name,
                arguments,
                {
                    "success": tr.success,
                    "output": tr.output[:12000],
                    "error": tr.error,
                    "exit_code": tr.exit_code,
                    "http_status": tr.http_status,
                },
                "allow",
                {k: v for k, v in crit.scores.items()} if isinstance(crit.scores, dict) else None,
                refl,
                _verifiable_reward(tr),
            )
            db_session.commit()

        step_index += 1

    else:
        result.status = "error"
        result.error = result.error or "Max agent steps exceeded"

    if result.status == "running":
        result.status = "error"
        result.error = result.error or "Agent did not complete"

    result.latency_ms = _elapsed(start)
    result.token_count = token_total or result.token_count

    if db_session and result.status not in ("agent_running",):
        from app.models.trace import Trace

        row = db_session.query(Trace).filter_by(id=trace_id).first()
        if row:
            _update_trace_final(db_session, trace_id, prompt, result, user_feedback)
        if result.status in ("completed", "halted", "blocked"):
            result.task_reward_score = result.task_reward_score or compute_task_reward(
                step_critic_avgs,
                "success" if result.status == "completed" else "failed",
                user_feedback,
                result.total_steps,
                self_corrections,
            )
            ep_id = _episode_persist(db_session, result, prompt, user_feedback)
            if ep_id and result.status == "completed" and result.task_reward_score is not None:
                _maybe_skill(db_session, ep_id, prompt, result)

    pr = PipelineResult(
        trace_id=result.trace_id,
        session_id=result.session_id,
        status=result.status,
        response=result.response,
        immune_input=result.immune_input,
        immune_output=result.immune_output,
        critic_result=result.critic_result,
        governance=result.governance,
        latency_ms=result.latency_ms,
        error=result.error,
        model_id_used=result.model_id_used,
        token_count=result.token_count,
        approval_request_id=result.approval_request_id,
        run_mode="agent",
        task_reward_score=result.task_reward_score,
        user_feedback=user_feedback,
        total_steps=result.total_steps,
        self_corrections=result.self_corrections,
        agent_state=result.agent_state,
        agent_trajectory=result.trajectory,
    )
    _record_run(pr)

    return result


def _maybe_skill(
    db: Session,
    episode_id: str,
    prompt: str,
    result: AgentRunResult,
) -> None:
    """Attempt to generate a skill from a high-reward episode."""
    try:
        from app.core.agent.skills import maybe_generate_skill

        tools = [t.get("tool") for t in result.trajectory if t.get("kind") == "tool"]
        maybe_generate_skill(
            episode_id=episode_id,
            task_summary=prompt[:8000],
            tool_sequence=tools,
            trajectory=result.trajectory,
            reward=result.task_reward_score or 0.0,
            db=db,
        )
    except Exception:
        logger.debug("Skill generation failed", exc_info=True)


def _create_approval(
    db: Session | None,
    trace_id: str,
    action_type: str,
    payload: dict[str, Any],
    gov: Any,
) -> str | None:
    if not db:
        return None
    from app.models.approval_log import ApprovalRequest

    required = max(gov.required_approvals, settings.APPROVAL_QUORUM)
    approval = ApprovalRequest(
        trace_id=trace_id,
        action_type=action_type,
        action_payload=payload,
        risk_level=gov.risk_level,
        policy_id=gov.policy_id,
        required_approvals=str(required),
        received_approvals="0",
        status="pending",
        token_scope={"trace_id": trace_id, "action": action_type},
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db.add(approval)
    db.flush()
    return approval.id


def _elapsed(start: float) -> float:
    return round((time.time() - start) * 1000, 1)
