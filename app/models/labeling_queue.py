"""
Labeling queue — failure traces awaiting human review for fine-tuning.

When the critic tree halts a generation or detects flawed reasoning,
the full trace + critic JSONs are pushed here. Reviewed items feed
into the training flywheel via evidential loss.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, Index, String, Text

from app.db import Base


class LabelingItem(Base):
    __tablename__ = "labeling_queue"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    trace_id = Column(String, nullable=False, index=True)
    source_node = Column(String, nullable=False)  # which critic node flagged this
    failure_type = Column(String, nullable=False)  # "reasoning", "injection", "safety", "quality", "hallucination"

    # The original generation context
    prompt = Column(Text, nullable=False)
    response = Column(Text, nullable=True)
    critic_output = Column(JSON, nullable=False)

    # Human review
    label = Column(String(20), nullable=True)  # "correct_flag", "false_positive", "needs_edit"
    corrected_response = Column(Text, nullable=True)
    reviewer_id = Column(String, nullable=True)
    reviewer_notes = Column(Text, nullable=True)

    # Training metadata
    status = Column(String(20), nullable=False, default="pending")  # "pending", "labeled", "exported", "trained"
    training_batch_id = Column(String, nullable=True)
    exported_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    labeled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_label_status", "status"),
        Index("ix_label_failure_type", "failure_type"),
        Index("ix_label_source", "source_node"),
    )
