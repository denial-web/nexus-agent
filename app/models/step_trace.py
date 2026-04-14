"""Per-step audit rows for multi-step agentic runs."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.db import Base


class StepTrace(Base):
    __tablename__ = "step_traces"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    trace_id = Column(String, ForeignKey("traces.id"), nullable=False, index=True)
    step_number = Column(Integer, nullable=False)
    action_type = Column(String(32), nullable=False)  # tool_call, reflection, final_answer
    tool_name = Column(String(64), nullable=True)
    tool_args = Column(JSON, nullable=True)
    tool_result = Column(JSON, nullable=True)
    covernor_decision = Column(String(32), nullable=True)
    critic_scores = Column(JSON, nullable=True)
    reflection = Column(Text, nullable=True)
    reward_signal = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
