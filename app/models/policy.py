"""
Governance policies — the Covernor default-deny rule engine.

Each policy defines what actions are allowed, what requires approval,
and what is blocked outright. Unknown actions are denied by default.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Index, Integer, String, Text

from app.db import Base


class Policy(Base):
    __tablename__ = "policies"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)

    # What this policy matches
    action_pattern = Column(String, nullable=False)  # glob or regex pattern for action types
    resource_pattern = Column(String, nullable=True)  # optional resource scope

    # Decision
    decision = Column(String(20), nullable=False)  # "allow", "require_approval", "deny"
    risk_level = Column(String(20), nullable=False, default="medium")
    required_approvals = Column(String, nullable=False, default="0")

    # Constraints
    max_executions_per_hour = Column(String, nullable=True)
    allowed_parameters = Column(JSON, nullable=True)
    blocked_parameters = Column(JSON, nullable=True)

    priority = Column(Integer, nullable=False, default=100)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_policy_active", "is_active"),
        Index("ix_policy_decision", "decision"),
    )
