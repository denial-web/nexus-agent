"""Re-run critic evaluation against stored traces (audit-safe; does not mutate traces)."""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.critic.arbiter import Arbiter

logger = logging.getLogger(__name__)


def re_evaluate_trace(trace_id: str, db_session: Session) -> dict[str, Any] | None:
    """
    Load a trace and run the current critic registry against its prompt/response.

    Does not modify the trace row. Returns None if the trace does not exist.
    """
    from app.models.trace import Trace

    trace = db_session.query(Trace).filter_by(id=trace_id).first()
    if not trace:
        return None

    if not trace.response:
        return {
            "trace_id": trace_id,
            "original_verdict": trace.critic_verdict,
            "original_rollback_count": trace.critic_rollback_count,
            "new_verdict": None,
            "new_rollback_count": 0,
            "new_scores": {},
            "halted_by": None,
            "drift": False,
            "skipped": "no_response",
        }

    arbiter = Arbiter.load_from_registry(db_session)

    context = {
        "prompt": trace.prompt,
        "response": trace.response,
        "model_id": trace.model_id or "mock",
        "trace_id": trace_id,
    }
    result = arbiter.evaluate(context)

    original_verdict = trace.critic_verdict
    new_verdict = result.verdict
    drift = new_verdict != original_verdict or (result.rollback_count or 0) != (trace.critic_rollback_count or 0)

    return {
        "trace_id": trace_id,
        "original_verdict": original_verdict,
        "original_rollback_count": trace.critic_rollback_count,
        "new_verdict": new_verdict,
        "new_rollback_count": result.rollback_count,
        "new_scores": result.scores,
        "halted_by": result.halted_by,
        "drift": drift,
    }
