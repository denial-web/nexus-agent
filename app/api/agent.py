"""Agent execution endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
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


@router.post("/run")
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
