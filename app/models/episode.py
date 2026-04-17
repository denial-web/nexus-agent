"""Reward-scored episodic memory for cross-session learning (Phase 9)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text

from app.db import Base


class Episode(Base):
    __tablename__ = "episodes"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    trace_id = Column(String, nullable=True, index=True)
    session_id = Column(String, nullable=True, index=True)
    task_summary = Column(Text, nullable=False)
    tool_sequence = Column(JSON, nullable=True)  # list[str]
    outcome = Column(String(20), nullable=False)  # success, partial, failed, halted
    task_reward_score = Column(Float, nullable=True)
    user_feedback = Column(String(20), nullable=True)
    reflection = Column(Text, nullable=True)
    step_count = Column(Integer, nullable=True)
    self_corrections = Column(Integer, nullable=True)
    agent_trajectory = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
