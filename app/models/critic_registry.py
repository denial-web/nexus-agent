"""
Critic registry — hot-swappable critic node configurations.

Stores prompt versions, LoRA adapter paths, and scoring weights for
each leaf node in the GrokForge-Nexus Arbiter tree.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Index, Integer, String, Text

from app.db import Base


class CriticNode(Base):
    __tablename__ = "critic_registry"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String, nullable=False, unique=True)
    node_type = Column(String(30), nullable=False)  # "arbiter", "safety", "reasoning", "injection", "quality"
    description = Column(Text, nullable=True)

    # The evaluation prompt template (hot-swappable)
    prompt_template = Column(Text, nullable=True)
    prompt_version = Column(Integer, nullable=False, default=1)

    # Optional LoRA adapter for specialized scoring
    lora_adapter_path = Column(String, nullable=True)
    lora_adapter_version = Column(String, nullable=True)

    # Scoring config
    weight = Column(Float, nullable=False, default=1.0)
    threshold_pass = Column(Float, nullable=False, default=0.7)
    threshold_halt = Column(Float, nullable=False, default=0.3)

    # Arbiter override: if True, a halt from this node stops generation immediately
    can_halt = Column(Boolean, nullable=False, default=False)

    config = Column(JSON, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_critic_type", "node_type"),
        Index("ix_critic_active", "is_active"),
    )
