"""
Approval log — K-of-N dual human approval records for the Covernor platform.

High-risk actions require multiple human approvers before an ECDSA
capability token is minted and execution proceeds.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, JSON, String, Text

from app.db import Base


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    trace_id = Column(String, nullable=False, index=True)
    action_type = Column(String, nullable=False)
    action_payload = Column(JSON, nullable=False)
    risk_level = Column(String(20), nullable=False)  # "low", "medium", "high", "critical"
    policy_id = Column(String, nullable=True)

    required_approvals = Column(String, nullable=False, default="2")
    received_approvals = Column(String, nullable=False, default="0")
    status = Column(String(20), nullable=False, default="pending")  # "pending", "approved", "denied", "expired"

    # The minted ECDSA token (only after quorum)
    capability_token = Column(Text, nullable=True)
    token_scope = Column(JSON, nullable=True)

    expires_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_approval_status", "status"),
        Index("ix_approval_trace", "trace_id"),
    )


class ApprovalVote(Base):
    __tablename__ = "approval_votes"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    request_id = Column(String, nullable=False, index=True)
    approver_id = Column(String, nullable=False)
    decision = Column(String(10), nullable=False)  # "approve", "deny"
    reason = Column(Text, nullable=True)
    signature = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
