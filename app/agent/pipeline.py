"""
Agent pipeline — the full zero-trust execution flow.

Orchestrates: Input Scan → A-S-FLC Decision → LLM Generation →
Critic Evaluation → Governance Check → Output Scan → Response.

Each step writes to the trace for full auditability.
"""

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.core.covernor.policy_engine import evaluate_action
from app.core.critic.arbiter import Arbiter
from app.core.immune.scanner import Verdict, harden_prompt, scan_input, scan_output
from app.core.llm.provider import generate
from app.core.training.calibration import record_critic_calibration
from app.core.training.labeler import push_failure
from app.metrics import PIPELINE_LATENCY, PIPELINE_RUNS, record_critic_scores

logger = logging.getLogger(__name__)

_ARBITER_TTL_SECONDS = 60
_arbiter_cache: Arbiter | None = None
_arbiter_cache_time: float = 0.0
_arbiter_lock = threading.Lock()


@dataclass
class PipelineResult:
    trace_id: str
    session_id: str
    status: str  # "completed", "blocked", "halted", "pending_approval", "error"
    response: str | None = None
    immune_input: dict = field(default_factory=dict)
    immune_output: dict = field(default_factory=dict)
    critic_result: dict = field(default_factory=dict)
    governance: dict = field(default_factory=dict)
    asflc: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str | None = None
    model_id_used: str | None = None
    token_count: int | None = None
    approval_request_id: str | None = None


def invalidate_arbiter_cache() -> None:
    """Force the next get_arbiter() call to rebuild from the registry."""
    global _arbiter_cache, _arbiter_cache_time
    with _arbiter_lock:
        _arbiter_cache = None
        _arbiter_cache_time = 0.0


def get_arbiter(db_session: Session | None = None) -> Arbiter:
    """Return a TTL-cached Arbiter loaded from the DB, or built-in heuristics."""
    global _arbiter_cache, _arbiter_cache_time

    if db_session is None:
        return Arbiter.default_from_heuristics()

    now = time.time()
    with _arbiter_lock:
        if _arbiter_cache is not None and (now - _arbiter_cache_time) < _ARBITER_TTL_SECONDS:
            arb = _arbiter_cache.__class__.__new__(_arbiter_cache.__class__)
            arb._nodes = dict(_arbiter_cache._nodes)
            arb._rollback_count = 0
            return arb

    arbiter = Arbiter.load_from_registry(db_session)
    with _arbiter_lock:
        _arbiter_cache = arbiter
        _arbiter_cache_time = time.time()

    fresh = Arbiter()
    fresh._nodes = dict(arbiter._nodes)
    return fresh


def run(
    prompt: str,
    session_id: str | None = None,
    model_id: str | None = None,
    db_session: Session | None = None,
) -> PipelineResult:
    """Execute the full agent pipeline."""
    start = time.time()
    trace_id = uuid.uuid4().hex
    session_id = session_id or uuid.uuid4().hex

    result = PipelineResult(trace_id=trace_id, session_id=session_id, status="pending")

    # ── Step 1: Input scan ──────────────────────────────
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
        if db_session:
            _push_pipeline_failure(
                trace_id=trace_id,
                source_node="immune",
                failure_type="injection",
                prompt=prompt,
                response=None,
                detail={"stage": "input_scan", "immune_input": result.immune_input},
                db_session=db_session,
            )
        _persist_trace(result, prompt, db_session)
        _record_run(result)
        return result

    if input_scan.verdict == Verdict.FLAG:
        hardened, removed = harden_prompt(prompt)
        if removed and hardened.strip():
            prompt = hardened
            result.immune_input["hardened"] = True
            result.immune_input["removed_fragments"] = removed

    # ── Step 2: A-S-FLC decision analysis ─────────────────
    system_hint: str | None = None
    try:
        from app.core.asflc.analyzer import analyze as asflc_analyze

        analysis = asflc_analyze(prompt, model_id=model_id)
        if analysis is not None:
            result.asflc = {
                "chosen_path": analysis.chosen_path,
                "confidence": analysis.confidence,
                "loops": analysis.loops,
                "all_paths": analysis.asflc.all_paths,
                "converged": analysis.asflc.converged,
                "chain_regret": analysis.asflc.chain_regret,
            }
            system_hint = analysis.system_hint
    except Exception:
        logger.warning("A-S-FLC analysis failed; continuing without it", exc_info=True)

    # ── Step 3: LLM generation + Step 4: Critic evaluation ──
    arbiter = get_arbiter(db_session)

    critic_ctx = {
        "prompt": prompt,
        "model_id": model_id or "mock",
        "trace_id": trace_id,
    }

    try:
        llm_result = generate(prompt, model_id=model_id, system_prompt=system_hint)
        response = llm_result.text
        result.model_id_used = llm_result.model_id
        result.token_count = llm_result.token_count
        critic_ctx["model_id"] = result.model_id_used or model_id or "mock"

        critic_result = arbiter.evaluate({**critic_ctx, "response": response})
    except Exception as exc:
        logger.exception("Pipeline LLM or critic evaluation failed: trace_id=%s", trace_id)
        internal_error = str(exc) or type(exc).__name__
        result.status = "error"
        result.error = "Pipeline processing failed"
        result.latency_ms = _elapsed(start)
        if db_session:
            _push_pipeline_failure(
                trace_id=trace_id,
                source_node="pipeline",
                failure_type="pipeline_error",
                prompt=prompt,
                response=None,
                detail={"stage": "llm_or_critic", "error": internal_error},
                db_session=db_session,
            )
        _persist_trace(result, prompt, db_session)
        _record_run(result)
        return result

    result.critic_result = {
        "verdict": critic_result.verdict,
        "scores": critic_result.scores,
        "rollback_count": critic_result.rollback_count,
        "halted_by": critic_result.halted_by,
    }

    record_critic_calibration(
        critic_scores=critic_result.scores,
        actual_verdict=critic_result.verdict,
        trace_id=trace_id,
    )
    record_critic_scores(critic_result.scores)

    if critic_result.verdict == "halt":
        result.status = "halted"
        result.error = f"Halted by critic: {critic_result.halted_by}"
        result.latency_ms = _elapsed(start)

        if db_session:
            _push_critic_failure(trace_id, prompt, response, critic_result, db_session)

        _persist_trace(result, prompt, db_session)
        _record_run(result)
        return result

    # ── Step 5: Governance check ────────────────────────
    gov_decision = evaluate_action(
        action_type="respond",
        resource="chat",
        db_session=db_session,
    )
    result.governance = {
        "decision": gov_decision.decision,
        "policy": gov_decision.policy_name,
        "policy_id": gov_decision.policy_id,
        "risk_level": gov_decision.risk_level,
    }

    if gov_decision.decision == "deny":
        result.status = "blocked"
        result.error = f"Governance denied: {gov_decision.reason}"
        result.latency_ms = _elapsed(start)
        if db_session:
            _push_pipeline_failure(
                trace_id=trace_id,
                source_node="covernor",
                failure_type="governance",
                prompt=prompt,
                response=response,
                detail={"stage": "governance", "governance": result.governance, "reason": gov_decision.reason},
                db_session=db_session,
            )
        _persist_trace(result, prompt, db_session)
        _record_run(result)
        return result

    if gov_decision.decision == "require_approval":
        result.status = "pending_approval"
        result.response = response
        result.latency_ms = _elapsed(start)
        if db_session:
            from app.models.approval_log import ApprovalRequest

            required = max(gov_decision.required_approvals, settings.APPROVAL_QUORUM)
            approval = ApprovalRequest(
                trace_id=trace_id,
                action_type="respond",
                action_payload={"prompt": prompt, "model_id": model_id},
                risk_level=gov_decision.risk_level,
                policy_id=gov_decision.policy_id,
                required_approvals=str(required),
                received_approvals="0",
                status="pending",
                token_scope={"trace_id": trace_id, "action": "respond"},
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            db_session.add(approval)
            db_session.flush()
            result.approval_request_id = approval.id
            result.governance["approval_request_id"] = approval.id
        _persist_trace(result, prompt, db_session)
        _record_run(result)
        return result

    # ── Step 6: Output scan ─────────────────────────────
    output_scan = scan_output(response)
    result.immune_output = {
        "verdict": output_scan.verdict.value,
        "score": output_scan.score,
        "triggers": output_scan.triggers,
    }

    if output_scan.verdict == Verdict.BLOCK:
        result.status = "blocked"
        result.error = "Output blocked by immune scanner"
        result.latency_ms = _elapsed(start)
        if db_session:
            _push_pipeline_failure(
                trace_id=trace_id,
                source_node="immune",
                failure_type="safety",
                prompt=prompt,
                response=response,
                detail={"stage": "output_scan", "immune_output": result.immune_output},
                db_session=db_session,
            )
        _persist_trace(result, prompt, db_session)
        _record_run(result)
        return result

    # ── Step 7: Success ─────────────────────────────────
    result.status = "completed"
    result.response = response
    result.latency_ms = _elapsed(start)
    _persist_trace(result, prompt, db_session)
    _record_run(result)
    return result


def _persist_trace(result: PipelineResult, prompt: str, db_session: Session | None) -> None:
    """Write trace to the audit log.

    If the commit fails, the exception propagates to the caller and the client
    receives a 500 with no trace record persisted. The LLM work is lost.
    Operators should monitor for "Failed to persist trace" log entries; repeated
    occurrences indicate a database connectivity or disk-space issue.
    """
    if not db_session:
        return

    from app.models.trace import Trace
    from app.services.integrity import compute_trace_hash

    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    response_hash = hashlib.sha256(result.response.encode()).hexdigest() if result.response else None

    prev = (
        db_session.query(Trace)
        .filter_by(session_id=result.session_id)
        .order_by(Trace.sequence.desc(), Trace.created_at.desc())
        .with_for_update()
        .first()
    )
    prev_hash = prev.trace_hash if prev and prev.trace_hash else "genesis"
    sequence = (prev.sequence + 1) if prev and prev.sequence is not None else 0

    trace_hash = compute_trace_hash(
        result.trace_id,
        prev_hash,
        prompt_hash,
        response_hash,
        result.status,
    )

    trace = Trace(
        id=result.trace_id,
        session_id=result.session_id,
        sequence=sequence,
        prompt=prompt,
        prompt_hash=prompt_hash,
        immune_verdict=result.immune_input.get("verdict", "unknown"),
        immune_score=result.immune_input.get("score"),
        immune_details=result.immune_input,
        asflc_result=result.asflc if result.asflc else None,
        asflc_chosen_path=result.asflc.get("chosen_path"),
        asflc_confidence=result.asflc.get("confidence"),
        asflc_loops=result.asflc.get("loops"),
        critic_verdict=result.critic_result.get("verdict"),
        critic_scores=result.critic_result.get("scores"),
        critic_rollback_count=result.critic_result.get("rollback_count", 0),
        governance_status=result.governance.get("decision"),
        governance_policy_id=result.governance.get("policy_id"),
        response=result.response,
        response_hash=response_hash,
        output_scan_verdict=result.immune_output.get("verdict"),
        latency_ms=result.latency_ms,
        status=result.status,
        error=result.error,
        model_id=result.model_id_used,
        token_count=result.token_count,
        prev_hash=prev_hash,
        trace_hash=trace_hash,
    )

    try:
        db_session.add(trace)
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("Failed to persist trace %s", result.trace_id)
        raise


def _push_pipeline_failure(
    trace_id: str,
    source_node: str,
    failure_type: str,
    prompt: str,
    response: str | None,
    detail: dict,
    db_session: Session,
) -> None:
    push_failure(
        trace_id=trace_id,
        source_node=source_node,
        failure_type=failure_type,
        prompt=prompt,
        response=response,
        critic_output=detail,
        db_session=db_session,
        commit=False,
    )


def _push_critic_failure(
    trace_id: str,
    prompt: str,
    response: str,
    critic_result: Any,
    db_session: Session,
) -> None:
    """Push critic halts/failures to the labeling queue."""
    halted_by = critic_result.halted_by or "unknown"
    source = halted_by.split(":")[0] if ":" in halted_by else halted_by

    push_failure(
        trace_id=trace_id,
        source_node=source,
        failure_type=source,
        prompt=prompt,
        response=response,
        critic_output=critic_result.scores,
        db_session=db_session,
        commit=False,
    )


def _record_run(result: PipelineResult) -> None:
    PIPELINE_RUNS.labels(status=result.status).inc()
    if result.latency_ms:
        PIPELINE_LATENCY.labels(status=result.status).observe(result.latency_ms / 1000.0)


def _elapsed(start: float) -> float:
    return round((time.time() - start) * 1000, 1)
