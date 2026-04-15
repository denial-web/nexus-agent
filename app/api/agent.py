"""Agent execution endpoints."""

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["Agent"])


class RunRequest(BaseModel):
    prompt: str
    session_id: str | None = None
    model_id: str | None = None


class CompareRequest(BaseModel):
    prompt: str
    model_ids: list[str] | None = None
    session_id: str | None = None


class AgentRunRequest(BaseModel):
    prompt: str
    session_id: str | None = None
    model_id: str | None = None
    user_feedback: str | None = None
    resume_state: dict | None = None


class AgentRunResponse(BaseModel):
    trace_id: str
    session_id: str
    status: str
    response: str | None = None
    error: str | None = None
    model_id: str | None = None
    token_count: int | None = None
    latency_ms: float | None = None
    task_reward_score: float | None = None
    total_steps: int | None = None
    self_corrections: int | None = None
    approval_request_id: str | None = None
    agent_state: dict | None = None
    trajectory: list | None = None


class AgentFeedbackRequest(BaseModel):
    trace_id: str
    feedback: str  # "good" or "bad"


class AgentResumeRequest(BaseModel):
    trace_id: str


class PipelineDetail(BaseModel):
    immune_input: dict | None = None
    asflc: dict | None = None
    critic: dict | None = None
    governance: dict | None = None
    immune_output: dict | None = None


class RunResponse(BaseModel):
    trace_id: str
    session_id: str
    status: str
    response: str | None = None
    model_id: str | None = None
    token_count: int | None = None
    pipeline: PipelineDetail | None = None
    latency_ms: float | None = None
    error: str | None = None
    approval_request_id: str | None = None


class ErrorResponse(BaseModel):
    detail: str


@router.post(
    "/run",
    response_model=RunResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Empty prompt"},
        413: {"model": ErrorResponse, "description": "Prompt too long"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
def run_agent(req: RunRequest, db: Session = Depends(get_db)) -> dict:
    """Execute the full zero-trust agent pipeline on a prompt."""
    from app.agent.pipeline import run

    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    max_len = settings.MAX_PROMPT_LENGTH
    if max_len > 0 and len(req.prompt) > max_len:
        raise HTTPException(
            status_code=413,
            detail=f"Prompt exceeds maximum length of {max_len} characters",
        )

    result = run(
        prompt=req.prompt,
        session_id=req.session_id,
        model_id=req.model_id,
        db_session=db,
    )

    payload = {
        "trace_id": result.trace_id,
        "session_id": result.session_id,
        "status": result.status,
        "response": result.response,
        "model_id": result.model_id_used,
        "token_count": result.token_count,
        "pipeline": {
            "immune_input": result.immune_input,
            "asflc": result.asflc,
            "critic": result.critic_result,
            "governance": result.governance,
            "immune_output": result.immune_output,
        },
        "latency_ms": result.latency_ms,
        "error": result.error,
    }
    if result.approval_request_id:
        payload["approval_request_id"] = result.approval_request_id
    return payload


@router.post("/agent/run", response_model=AgentRunResponse)
def run_agentic(req: AgentRunRequest, db: Session = Depends(get_db)) -> dict:
    """Multi-step governed agent with tools, reflection, and critic feedback."""
    from app.agent.agent_loop import run_agent

    if not req.prompt.strip() and not req.resume_state:
        raise HTTPException(status_code=400, detail="prompt cannot be empty unless resuming")

    max_len = settings.MAX_PROMPT_LENGTH
    if max_len > 0 and len(req.prompt) > max_len:
        raise HTTPException(
            status_code=413,
            detail=f"Prompt exceeds maximum length of {max_len} characters",
        )

    r = run_agent(
        prompt=req.prompt,
        session_id=req.session_id,
        model_id=req.model_id,
        db_session=db,
        user_feedback=req.user_feedback,
        resume_state=req.resume_state,
    )
    return {
        "trace_id": r.trace_id,
        "session_id": r.session_id,
        "status": r.status,
        "response": r.response,
        "error": r.error,
        "model_id": r.model_id_used,
        "token_count": r.token_count,
        "latency_ms": r.latency_ms,
        "task_reward_score": r.task_reward_score,
        "total_steps": r.total_steps,
        "self_corrections": r.self_corrections,
        "approval_request_id": r.approval_request_id,
        "agent_state": r.agent_state,
        "trajectory": r.trajectory,
    }


@router.post("/agent/resume", response_model=AgentRunResponse)
def resume_agentic(req: AgentResumeRequest, db: Session = Depends(get_db)) -> dict:
    """Resume an agent run after governance approval (trace status pending_agent_resume)."""
    from app.agent.agent_loop import run_agent
    from app.models.trace import Trace

    row = db.query(Trace).filter_by(id=req.trace_id).first()
    if not row or not row.agent_state:
        raise HTTPException(status_code=404, detail="Trace not found or no agent state")
    if row.status != "pending_agent_resume":
        raise HTTPException(status_code=400, detail=f"Trace not resumable (status={row.status})")

    prompt = row.prompt or ""
    resume_state = row.agent_state
    if isinstance(resume_state, dict):
        r = run_agent(
            prompt=prompt,
            session_id=row.session_id,
            model_id=row.model_id,
            db_session=db,
            resume_state=resume_state,
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid agent_state")

    return {
        "trace_id": r.trace_id,
        "session_id": r.session_id,
        "status": r.status,
        "response": r.response,
        "error": r.error,
        "model_id": r.model_id_used,
        "token_count": r.token_count,
        "latency_ms": r.latency_ms,
        "task_reward_score": r.task_reward_score,
        "total_steps": r.total_steps,
        "self_corrections": r.self_corrections,
        "approval_request_id": r.approval_request_id,
        "agent_state": r.agent_state,
        "trajectory": r.trajectory,
    }


@router.post("/agent/feedback")
def agent_feedback(req: AgentFeedbackRequest, db: Session = Depends(get_db)) -> dict:
    """Attach user reward signal to a completed agent trace."""
    from app.models.trace import Trace

    if req.feedback not in ("good", "bad"):
        raise HTTPException(status_code=400, detail="feedback must be 'good' or 'bad'")
    row = db.query(Trace).filter_by(id=req.trace_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Trace not found")
    row.user_feedback = req.feedback
    db.commit()
    return {"trace_id": req.trace_id, "user_feedback": req.feedback}


@router.post("/stream")
def stream_agent(req: RunRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    """Stream LLM tokens through the zero-trust pipeline via Server-Sent Events."""
    from app.agent.pipeline import run_stream

    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    max_len = settings.MAX_PROMPT_LENGTH
    if max_len > 0 and len(req.prompt) > max_len:
        raise HTTPException(
            status_code=413,
            detail=f"Prompt exceeds maximum length of {max_len} characters",
        )

    def _event_generator() -> Any:
        for event in run_stream(
            prompt=req.prompt,
            session_id=req.session_id,
            model_id=req.model_id,
            db_session=db,
        ):
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/compare")
def compare_models(req: CompareRequest, db: Session = Depends(get_db)) -> dict:
    """Run the same prompt through multiple LLM providers, critic-score each, pick the best."""
    from app.agent.pipeline import get_arbiter
    from app.core.immune.scanner import Verdict, harden_prompt, scan_input, scan_output
    from app.core.llm.provider import generate_multi
    from app.metrics import PIPELINE_LATENCY, PIPELINE_RUNS, record_critic_scores

    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    max_len = settings.MAX_PROMPT_LENGTH
    if max_len > 0 and len(req.prompt) > max_len:
        raise HTTPException(
            status_code=413,
            detail=f"Prompt exceeds maximum length of {max_len} characters",
        )

    if req.model_ids and len(req.model_ids) > settings.COMPARE_MAX_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model_ids exceeds maximum of {settings.COMPARE_MAX_MODELS} models",
        )

    start = time.time()
    prompt = req.prompt

    input_scan = scan_input(prompt, session_id=req.session_id)
    if input_scan.verdict == Verdict.BLOCK:
        PIPELINE_RUNS.labels(status="blocked").inc()
        return {
            "status": "blocked",
            "error": "Input blocked by immune scanner",
            "candidates": [],
            "winner": None,
            "latency_ms": round((time.time() - start) * 1000, 1),
        }

    if input_scan.verdict == Verdict.FLAG:
        hardened, removed = harden_prompt(prompt)
        if removed:
            if hardened.strip():
                prompt = hardened
            else:
                PIPELINE_RUNS.labels(status="blocked").inc()
                return {
                    "status": "blocked",
                    "error": "Prompt entirely composed of flagged content",
                    "candidates": [],
                    "winner": None,
                    "latency_ms": round((time.time() - start) * 1000, 1),
                }

    responses = generate_multi(
        prompt=prompt,
        model_ids=req.model_ids,
    )

    if not responses:
        PIPELINE_RUNS.labels(status="error").inc()
        return {
            "status": "error",
            "error": "No LLM providers returned a response",
            "candidates": [],
            "winner": None,
            "latency_ms": round((time.time() - start) * 1000, 1),
        }

    arbiter = get_arbiter(db)
    node_weights = arbiter.get_node_weights()
    candidates = []

    for llm_resp in responses:
        output_scan = scan_output(llm_resp.text)
        output_blocked = output_scan.verdict == Verdict.BLOCK

        try:
            critic_result = arbiter.evaluate(
                {
                    "prompt": prompt,
                    "response": llm_resp.text,
                    "model_id": llm_resp.model_id,
                }
            )
        except Exception:
            logger.exception("Critic evaluation failed for model %s", llm_resp.model_id)
            candidates.append(
                {
                    "model_id": llm_resp.model_id,
                    "provider": llm_resp.provider,
                    "response": llm_resp.text,
                    "token_count": llm_resp.token_count,
                    "llm_latency_ms": llm_resp.latency_ms,
                    "critic_verdict": "error",
                    "critic_scores": {},
                    "aggregate_score": 0.0,
                    "output_blocked": output_blocked,
                    "halted": True,
                }
            )
            continue

        record_critic_scores(critic_result.scores)

        weighted_sum = 0.0
        weight_total = 0.0
        scores_dict = {}
        for node_name, cs in critic_result.scores.items():
            s = cs.score if hasattr(cs, "score") else (cs.get("score", 0.0) if isinstance(cs, dict) else 0.0)
            w = node_weights.get(node_name, 1.0)
            weighted_sum += s * w
            weight_total += w
            scores_dict[node_name] = {
                "score": s,
                "verdict": cs.verdict if hasattr(cs, "verdict") else cs.get("verdict", "unknown"),
                "weight": w,
            }

        aggregate_score = weighted_sum / weight_total if weight_total > 0 else 0.0

        candidates.append(
            {
                "model_id": llm_resp.model_id,
                "provider": llm_resp.provider,
                "response": llm_resp.text,
                "token_count": llm_resp.token_count,
                "llm_latency_ms": llm_resp.latency_ms,
                "critic_verdict": critic_result.verdict,
                "critic_scores": scores_dict,
                "aggregate_score": round(aggregate_score, 4),
                "output_blocked": output_blocked,
                "halted": critic_result.verdict == "halt",
            }
        )

    viable = [c for c in candidates if not c["output_blocked"] and not c["halted"]]
    if viable:
        winner = max(viable, key=lambda c: float(c["aggregate_score"]))
    else:
        winner = max(candidates, key=lambda c: float(c["aggregate_score"]))

    from app.core.covernor.policy_engine import evaluate_action

    gov_decision = evaluate_action(action_type="respond", resource="chat", db_session=db)
    governance = {
        "decision": gov_decision.decision,
        "policy": gov_decision.policy_name,
        "risk_level": gov_decision.risk_level,
    }

    latency_ms = round((time.time() - start) * 1000, 1)

    if gov_decision.decision == "deny":
        PIPELINE_RUNS.labels(status="blocked").inc()
        return {
            "status": "blocked",
            "error": f"Governance denied: {gov_decision.reason}",
            "governance": governance,
            "candidates": candidates,
            "winner": None,
            "latency_ms": latency_ms,
        }

    final_status = "completed"
    if gov_decision.decision == "require_approval":
        final_status = "pending_approval"

    PIPELINE_RUNS.labels(status=final_status).inc()
    PIPELINE_LATENCY.labels(status=final_status).observe(latency_ms / 1000.0)

    return {
        "status": final_status,
        "winner": {
            "model_id": winner["model_id"],
            "provider": winner["provider"],
            "response": winner["response"],
            "aggregate_score": winner["aggregate_score"],
        },
        "governance": governance,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "latency_ms": latency_ms,
    }


# ── Security Benchmark ──────────────────────────────────────────────


class BenchmarkRequest(BaseModel):
    categories: list[str] | None = None
    threshold: float | None = None


@router.post("/benchmark")
def run_security_benchmark(body: BenchmarkRequest) -> dict:
    from app.core.immune.benchmark import run_benchmark

    report = run_benchmark(categories=body.categories)
    result = report.to_dict()
    if body.threshold is not None and report.composite_score < body.threshold:
        result["gate"] = "failed"
    elif body.threshold is not None:
        result["gate"] = "passed"
    return result


# ── Cache & Circuit Breaker Status ─────────────────────────────────


@router.get("/cache/stats")
def cache_stats() -> dict:
    from app.core.llm.cache import get_cache

    return get_cache().get_stats()


@router.delete("/cache")
def cache_clear() -> dict:
    from app.core.llm.cache import get_cache

    cleared = get_cache().clear()
    return {"cleared": cleared}


@router.get("/circuit-breakers")
def circuit_breaker_status() -> dict:
    from app.core.llm.circuit_breaker import get_registry

    return {"breakers": get_registry().get_all_status()}
