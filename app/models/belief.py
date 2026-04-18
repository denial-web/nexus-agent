"""
Belief — the atomic unit of Nexus semantic memory (Phase 12).

Design decisions baked into this schema (see MEMORY_FLAGSHIP_PLAN.md §2, §3):

- Bitemporal. `valid_from` / `valid_to` describe when a fact was true in the
  world. `observed_at` / `superseded_at` describe when the agent learned or
  discarded the fact. A query like "what does Alice prefer as of yesterday,
  based on what we knew last week?" needs both axes.

- Beta-distributed confidence. We store `confidence_alpha` and
  `confidence_beta` (conjugate prior parameters) instead of a flat scalar.
  Updates are principled (α ← α + evidence_true, β ← β + evidence_false),
  expected value is α/(α+β), and variance is available for uncertainty-
  aware retrieval.

- Causal provenance. `derived_from` lists belief ids that produced this
  belief via inference; `source_trace_id` points to the pipeline run that
  created it. Together they answer "why do you believe this?".

- Governed by default. Writes are routed through the Covernor
  (`memory:write:{entity_type}` actions) and every row carries a
  hash-chain pointer (`prev_hash` → `belief_hash`) so tampering is
  detectable independently of the trace hash chain.

- Scoped. `user_id` / `agent_id` / `session_id` / `visibility` let the
  retrieval layer isolate per-user memory without leaking across tenants.

This module MUST stay importable even when MEMORY_ENABLED=False — the
regression tripwire (tests/test_memory_regression.py) transitions the
`test_no_belief_rows_written` test from skip to pass once this file lands,
and that transition is the signal that Phase 12A Week 1 foundation is in.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, Float, Index, String, Text

from app.db import Base


class Belief(Base):
    """A single atomic fact the agent believes about an entity."""

    __tablename__ = "beliefs"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)

    # Subject-predicate-object triple. Value is JSON because predicates
    # vary in shape (string preference, numeric score, structured object).
    entity = Column(String(200), nullable=False)
    predicate = Column(String(200), nullable=False)
    value = Column(JSON, nullable=False)

    # Bitemporal axes.
    #   valid_from / valid_to  → world time (when the fact was true)
    #   observed_at / superseded_at → belief time (when we knew it)
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    observed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    superseded_at = Column(DateTime(timezone=True), nullable=True)

    # Beta(α, β) confidence. Expected confidence = α / (α + β).
    # Defaults to Beta(1,1) = uniform prior.
    confidence_alpha = Column(Float, nullable=False, default=1.0)
    confidence_beta = Column(Float, nullable=False, default=1.0)

    # Provenance.
    # source_type: "observed" | "inferred" | "tool" | "user_stated" | "imported"
    source_type = Column(String(30), nullable=False, default="observed")
    source_trace_id = Column(String, nullable=True, index=True)
    extractor_version = Column(String(60), nullable=True)
    derived_from = Column(JSON, nullable=True)  # list[str] of belief ids
    contradicts = Column(JSON, nullable=True)  # list[str] of belief ids

    # Retrieval support. embedding is stored JSON (list[float]) because
    # Nexus is SQLite/Postgres-first and pgvector is optional. Real
    # vector stores can back this column via a separate index later.
    embedding = Column(JSON, nullable=True)
    keywords = Column(JSON, nullable=True)  # list[str]

    # Classification for stakes/decay policy lookups.
    # entity_type: "identity" | "preference" | "state" | "financial" | "context"
    entity_type = Column(String(30), nullable=False, default="context")

    # Scope. NULL means "applies across scopes" but retrieval should still
    # prefer scoped beliefs.
    user_id = Column(String(120), nullable=True, index=True)
    agent_id = Column(String(120), nullable=True, index=True)
    session_id = Column(String(120), nullable=True, index=True)
    visibility = Column(String(20), nullable=False, default="private")

    # Independent hash chain. Verified by the same integrity service that
    # verifies trace chains.
    prev_hash = Column(String(64), nullable=True)
    belief_hash = Column(String(64), nullable=True)

    # Free-text reasoning field — the extractor's one-liner explanation
    # (e.g., "user said 'I prefer short answers'"). Useful for debugging
    # and for the /beliefs/{id}/explain endpoint.
    rationale = Column(Text, nullable=True)

    __table_args__ = (
        # "current belief for entity+predicate" — the hot query.
        Index(
            "ix_belief_entity_predicate_current",
            "entity",
            "predicate",
            "superseded_at",
        ),
        Index("ix_belief_entity_type", "entity_type"),
        Index("ix_belief_user_scope", "user_id", "session_id"),
        Index("ix_belief_observed_at", "observed_at"),
    )

    def confidence(self) -> float:
        """Expected belief confidence from Beta(α, β)."""
        a = float(self.confidence_alpha or 1.0)
        b = float(self.confidence_beta or 1.0)
        denom = a + b
        return a / denom if denom > 0 else 0.5

    def is_current(self) -> bool:
        """True iff this belief has not been superseded."""
        return self.superseded_at is None
