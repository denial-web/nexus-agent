"""Training flywheel persistence — Doctrine export retries and ECE snapshots."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Index, Integer, String, Text

from app.db import Base


class DoctrineOutbox(Base):
    """Failed Doctrine Lab dataset imports queued for retry with exponential backoff."""

    __tablename__ = "doctrine_outbox"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    batch_id = Column(String(64), nullable=False, index=True)
    dataset_type = Column(String(64), nullable=False, default="agent_safety")
    items_json = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    __table_args__ = (Index("ix_doctrine_outbox_status_next", "status", "next_retry_at"),)


class CalibrationSnapshot(Base):
    """Point-in-time ECE calibration metrics (survives process restarts)."""

    __tablename__ = "calibration_snapshots"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    recorded_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True)
    ece = Column(Float, nullable=False)
    num_samples = Column(Integer, nullable=False)
    needs_recalibration = Column(Boolean, nullable=False, default=False)
    per_node_ece = Column(JSON, nullable=True)
    bins = Column(JSON, nullable=True)
