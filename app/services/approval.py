"""Shared approval vote logic used by both the JSON API and the dashboard UI."""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class VoteResult:
    status: str
    received: str
    required: str
    error: str | None = None
    http_status: int = 200


def compute_action_hash(action_type: str, action_payload: dict | None) -> str:
    """Stable hash binding an approval to the exact action payload reviewed."""
    payload = {
        "action_type": action_type,
        "action_payload": action_payload or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def approval_token_scope(trace_id: str, action_type: str, action_payload: dict | None) -> dict:
    """Build the token scope shared by approval requests and capability tokens."""
    return {
        "trace_id": trace_id,
        "action": action_type,
        "action_hash": compute_action_hash(action_type, action_payload),
    }


def resolve_approver_identity(request: Request, requested_approver_id: str | None) -> tuple[str | None, str | None]:
    """Resolve a vote identity from auth/session/config, never only from arbitrary body text."""
    requested = (requested_approver_id or "").strip()
    configured_reviewers = {x.strip() for x in settings.APPROVAL_REVIEWERS.split(",") if x.strip()}
    if configured_reviewers:
        if requested not in configured_reviewers:
            return None, "Approver is not in the configured reviewer list"

    api_key = request.headers.get("X-API-Key", "").strip()
    if api_key:
        from app.middleware import check_api_key

        valid, _is_primary = check_api_key(api_key)
        if valid:
            digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
            return f"api-key:{digest}", None

    if request.session.get("dashboard_authed"):
        return str(request.session.get("dashboard_reviewer_id") or "dashboard-session"), None

    if configured_reviewers:
        return requested, None
    if requested:
        return requested, None
    return None, "Approver identity is required"


def expected_resume_approval(resume_state: dict) -> tuple[str | None, dict | None]:
    """Return the approval action expected by a stored agent resume payload."""
    pending = resume_state.get("pending_tool")
    if pending:
        args = pending.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        return (
            "agent_tool",
            {
                "tool": str(pending.get("tool", "")),
                "arguments": args,
                "agent_state": resume_state,
            },
        )
    if resume_state.get("pending_final"):
        return "agent_final", {"messages": list(resume_state.get("messages", []))}
    return None, None


def validate_resume_approval(db: Session, trace_id: str, resume_state: dict) -> tuple[bool, str | None]:
    """Verify the approved action hash still matches the pending resume action."""
    from app.models.approval_log import ApprovalRequest

    action_type, payload = expected_resume_approval(resume_state)
    if not action_type or payload is None:
        return True, None

    current_hash = compute_action_hash(action_type, payload)
    approvals = (
        db.query(ApprovalRequest)
        .filter_by(trace_id=trace_id, action_type=action_type, status="approved")
        .order_by(ApprovalRequest.created_at.desc())
        .all()
    )
    if not approvals:
        return False, "No approved request found for pending agent action"

    for approval in approvals:
        scope = approval.token_scope if isinstance(approval.token_scope, dict) else {}
        stored_hash = scope.get("action_hash") or compute_action_hash(
            approval.action_type,
            approval.action_payload,
        )
        if hmac.compare_digest(str(stored_hash), current_hash):
            return True, None
    return False, "Approved action hash does not match pending agent action"


def process_vote(
    request_id: str,
    approver_id: str,
    decision: str,
    db: Session,
) -> VoteResult:
    """
    Record a vote, enforce quorum/expiry/dedup, issue token + finalize trace on approval.

    Returns a VoteResult with an http_status indicating success or the kind of failure.
    """
    from app.core.covernor.token_manager import issue_token
    from app.core.immune.scanner import Verdict, scan_output
    from app.models.approval_log import ApprovalRequest, ApprovalVote
    from app.models.trace import Trace
    from app.services.integrity import cascade_rehash_from_trace

    if decision not in ("approve", "deny"):
        return VoteResult(
            status="error",
            received="0",
            required="0",
            error="Decision must be 'approve' or 'deny'",
            http_status=422,
        )

    req = db.query(ApprovalRequest).filter_by(id=request_id).with_for_update().first()
    if not req:
        return VoteResult(
            status="error",
            received="0",
            required="0",
            error="Approval request not found",
            http_status=404,
        )
    if req.status != "pending":
        return VoteResult(
            status="error",
            received=req.received_approvals,
            required=req.required_approvals,
            error=f"Request is already {req.status}",
            http_status=400,
        )

    if req.expires_at:
        exp = req.expires_at if req.expires_at.tzinfo else req.expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) > exp:
            req.status = "expired"
            db.commit()
            return VoteResult(
                status="expired",
                received=req.received_approvals,
                required=req.required_approvals,
                error="Approval request has expired",
                http_status=400,
            )

    dup = db.query(ApprovalVote).filter_by(request_id=request_id, approver_id=approver_id).first()
    if dup:
        return VoteResult(
            status="error",
            received=req.received_approvals,
            required=req.required_approvals,
            error="Approver already voted on this request",
            http_status=409,
        )

    try:
        required = max(int(req.required_approvals), settings.APPROVAL_QUORUM)
        received = int(req.received_approvals)
    except (ValueError, TypeError):
        logger.error("Corrupt approval counts for request %s", request_id)
        return VoteResult(
            status="error",
            received=req.received_approvals,
            required=req.required_approvals,
            error="Corrupt approval count data",
            http_status=500,
        )

    req.required_approvals = str(required)

    vote = ApprovalVote(
        request_id=request_id,
        approver_id=approver_id,
        decision=decision,
    )
    db.add(vote)

    if decision == "deny":
        req.status = "denied"
        req.resolved_at = datetime.now(UTC)
    else:
        received += 1
        req.received_approvals = str(received)
        if received >= required:
            trace = db.query(Trace).filter_by(id=req.trace_id).first()
            if not trace:
                logger.error("Trace %s missing for approval %s — refusing to issue token", req.trace_id, request_id)
                received -= 1
                req.received_approvals = str(received)
                db.delete(vote)
                db.commit()
                return VoteResult(
                    status="error",
                    received=req.received_approvals,
                    required=req.required_approvals,
                    error="Associated trace not found — cannot finalize approval",
                    http_status=500,
                )

            req.status = "approved"
            req.token_scope = approval_token_scope(req.trace_id, req.action_type, req.action_payload)
            token = issue_token(
                trace_id=req.trace_id,
                action_type=req.action_type,
                scope=req.token_scope,
            )
            req.capability_token = token.signature
            req.resolved_at = datetime.now(UTC)

            trace.governance_token_id = token.token_id
            if req.action_type in ("agent_tool", "agent_final"):
                trace.governance_status = "approved"
                trace.status = "pending_agent_resume"
                trace.error = None
            else:
                response_text = trace.response or ""
                if response_text:
                    out = scan_output(response_text)
                    trace.output_scan_verdict = out.verdict.value
                    if out.verdict == Verdict.BLOCK:
                        trace.status = "blocked"
                        trace.error = "Output blocked by immune scanner after approval"
                    else:
                        trace.status = "completed"
                        trace.governance_status = "approved"
                        trace.error = None
                else:
                    trace.status = "blocked"
                    trace.error = "No response to release"

            cascade_rehash_from_trace(db, trace.id)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("Duplicate vote attempt by %s on %s (caught by DB constraint)", approver_id, request_id)
        return VoteResult(
            status="error",
            received=req.received_approvals,
            required=req.required_approvals,
            error="Approver already voted on this request",
            http_status=409,
        )
    logger.info("Vote on %s: %s by %s → %s", request_id, decision, approver_id, req.status)
    return VoteResult(status=req.status, received=req.received_approvals, required=req.required_approvals)
