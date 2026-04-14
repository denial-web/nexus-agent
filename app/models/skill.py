"""Secure skill model — auto-generated workflows from high-reward episodes (Phase 9B)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String, Text

from app.db import Base


class Skill(Base):
    __tablename__ = "skills"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(200), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    source_episode_id = Column(String, nullable=True, index=True)

    steps = Column(JSON, nullable=False)
    expected_reward = Column(Float, nullable=True)
    min_reward_threshold = Column(Float, default=0.5)

    total_runs = Column(Integer, default=0)
    avg_reward = Column(Float, nullable=True)
    last_reward = Column(Float, nullable=True)
    enabled = Column(Boolean, default=True, index=True)
    flagged = Column(Boolean, default=False)

    skill_hash = Column(String(64), nullable=True)
    immune_scanned = Column(Boolean, default=False)
    critic_scanned = Column(Boolean, default=False)

    source = Column(String(100), nullable=True)  # nexus | clawhub:slug | manual | import:url
    requirements = Column(JSON, nullable=True)  # metadata.openclaw.requires
    raw_source = Column(Text, nullable=True)  # original SKILL.md

    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
