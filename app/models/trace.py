"""
Execution traces — the append-only audit log for every agent run.

Each trace captures the full lifecycle: input scan → decision → critic
evaluation → governance check → execution → output scan.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, Integer, JSON, String, Text

from app.db import Base


class Trace(Base):
    __tablename__ = "traces"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    session_id = Column(String, nullable=False, index=True)
    sequence = Column(Integer, nullable=False, default=0)

    # Input
    prompt = Column(Text, nullable=False)
    prompt_hash = Column(String(64), nullable=False)

    # Immune scan result
    immune_verdict = Column(String(20), nullable=False)  # "pass", "block", "flag"
    immune_score = Column(Float, nullable=True)
    immune_details = Column(JSON, nullable=True)

    # A-S-FLC decision
    asflc_result = Column(JSON, nullable=True)
    asflc_chosen_path = Column(String, nullable=True)
    asflc_confidence = Column(Float, nullable=True)
    asflc_loops = Column(Integer, nullable=True)

    # Critic evaluation
    critic_verdict = Column(String(20), nullable=True)  # "pass", "rollback", "halt"
    critic_scores = Column(JSON, nullable=True)
    critic_rollback_count = Column(Integer, default=0)

    # Governance
    governance_status = Column(String(20), nullable=True)  # "approved", "pending", "denied", "auto"
    governance_policy_id = Column(String, nullable=True)
    governance_token_id = Column(String, nullable=True)

    # Output
    response = Column(Text, nullable=True)
    response_hash = Column(String(64), nullable=True)
    output_scan_verdict = Column(String(20), nullable=True)

    # Metadata
    model_id = Column(String, nullable=True)
    latency_ms = Column(Float, nullable=True)
    token_count = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # SHA-256 hash chain — each trace links to the previous
    prev_hash = Column(String(64), nullable=True)
    trace_hash = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_trace_session_seq", "session_id", "sequence"),
        Index("ix_trace_status", "status"),
        Index("ix_trace_created", "created_at"),
    )
