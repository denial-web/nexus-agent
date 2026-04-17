"""
Webhook configuration — persistent notification targets for system events.

Each webhook subscribes to one or more event types and receives HMAC-signed
HTTP POST payloads when those events fire.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text

from app.db import Base


class Webhook(Base):
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    url = Column(Text, nullable=False)
    events = Column(JSON, nullable=False)
    secret = Column(String, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    description = Column(Text, nullable=True)

    failure_count = Column(Integer, nullable=False, default=0)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
