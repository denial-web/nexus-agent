"""Shared approval vote logic used by both the JSON API and the dashboard UI."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

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
