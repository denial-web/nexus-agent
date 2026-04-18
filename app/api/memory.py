"""
Belief memory REST API (Phase 12 Week 2).

Five endpoints, all under both `/v1/memory` and `/api/memory`:

- `GET  /memory`                       — list live beliefs (scoped + paged)
- `GET  /memory/{belief_id}/history`   — bitemporal history for an entity+predicate
- `GET  /memory/{belief_id}/explain`   — ranked retrieval signals for one belief
- `POST /memory/forget`                — user-directed tombstoning
- `GET  /memory/stats`                 — health/metrics for the memory subsystem

All endpoints return HTTP 503 `memory_disabled` when `MEMORY_ENABLED=False`.
This matches the rest of the codebase's "feature flag at the edge" pattern
and keeps the regression tripwire happy: the default-disabled path never
touches the beliefs table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.memory.confidence import BetaConfidence
from app.core.memory.forgetting import (
    effective_sample_size,
    forget_by_entity,
    parse_decay_profile,
)
from app.core.memory.retrieval import RetrievalQuery, retrieve
from app.db import get_db
from app.errors import NexusAPIError
from app.models.belief import Belief

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["Memory"])


# ---------------------------------------------------------------------------
# Response models (Pydantic v2 — `model_config` not `class Config`)
# ---------------------------------------------------------------------------


class BeliefView(BaseModel):
    """User-facing belief shape. Flattens confidence into mean + sample size."""

    id: str
    entity: str
    predicate: str
    value: Any
    entity_type: str | None
    source_type: str
    observed_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    superseded_at: datetime | None
    confidence_alpha: float
    confidence_beta: float
    mean: float
    effective_sample_size: float
    is_current: bool
    user_id: str | None
    session_id: str | None
    agent_id: str | None
    source_trace_id: str | None
    extractor_version: str | None
    rationale: str | None
    keywords: list[str] | None
    derived_from: list[str] | None
    contradicts: list[str] | None


class ListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    beliefs: list[BeliefView]


class HistoryResponse(BaseModel):
    entity: str
    predicate: str
    versions: list[BeliefView]


class SignalBreakdown(BaseModel):
    signal: str
    score: float


class ExplainResponse(BaseModel):
    belief: BeliefView
    query_text: str
    rrf_score: float
    signals: list[SignalBreakdown]
    rank_in_scope: int  # 1-indexed rank among current-user beliefs


class ForgetRequest(BaseModel):
    entity: str = Field(..., min_length=1, max_length=500)
    predicate: str | None = Field(None, max_length=200)
    user_id: str | None = Field(None, max_length=200)


class ForgetResponse(BaseModel):
    tombstoned: int
    entity: str
    predicate: str | None
    user_id: str | None


class StatsResponse(BaseModel):
    enabled: bool
    total_live: int
    total_tombstoned: int
    by_entity_type: dict[str, int]
    by_source_type: dict[str, int]
    decay_profile: dict[str, str]  # human-readable ("180d", "inf", ...)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_enabled() -> None:
    """Uniform 503 when the feature flag is off.

    Raised via `NexusAPIError` so the response follows the project's
    structured error envelope (`error.code`, `error.message`, `request_id`,
    etc.) rather than a bare FastAPI detail string.
    """
    if not settings.MEMORY_ENABLED:
        raise NexusAPIError(
            503,
            "memory_disabled",
            "Belief memory is disabled. Set MEMORY_ENABLED=true to use this endpoint.",
        )


def _to_view(b: Belief, *, now: datetime | None = None) -> BeliefView:
    """Shape a Belief row for JSON response. Uses stored α/β (canonical)
    and reports an *effective* sample size that reflects age-based decay.
    """
    conf = BetaConfidence(
        alpha=float(b.confidence_alpha or 1.0),
        beta=float(b.confidence_beta or 1.0),
    )
    eff_ss = effective_sample_size(b, now=now)
    return BeliefView(
        id=b.id,
        entity=b.entity,
        predicate=b.predicate,
        value=b.value,
        entity_type=b.entity_type,
        source_type=b.source_type,
        observed_at=b.observed_at,
        valid_from=b.valid_from,
        valid_to=b.valid_to,
        superseded_at=b.superseded_at,
        confidence_alpha=conf.alpha,
        confidence_beta=conf.beta,
        mean=conf.mean,
        effective_sample_size=eff_ss,
        is_current=b.superseded_at is None,
        user_id=b.user_id,
        session_id=b.session_id,
        agent_id=b.agent_id,
        source_trace_id=b.source_trace_id,
        extractor_version=b.extractor_version,
        rationale=b.rationale,
        keywords=list(b.keywords) if b.keywords else None,
        derived_from=list(b.derived_from) if b.derived_from else None,
        contradicts=list(b.contradicts) if b.contradicts else None,
    )


def _format_half_life(raw: str) -> dict[str, str]:
    """Return the decay profile in a display-friendly form."""
    parsed = parse_decay_profile(raw)
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if v is None:
            out[k] = "inf"
            continue
        total_seconds = int(v.total_seconds())
        if total_seconds % 86400 == 0:
            out[k] = f"{total_seconds // 86400}d"
        elif total_seconds % 3600 == 0:
            out[k] = f"{total_seconds // 3600}h"
        else:
            out[k] = f"{total_seconds // 60}m"
    return out


# ---------------------------------------------------------------------------
# GET /memory — list live beliefs
# ---------------------------------------------------------------------------


@router.get("", response_model=ListResponse)
def list_beliefs(
    user_id: str | None = Query(None, description="Filter by user scope"),
    session_id: str | None = Query(None, description="Filter by session scope"),
    entity: str | None = Query(None, description="Exact entity match"),
    predicate: str | None = Query(None, description="Exact predicate match"),
    entity_type: str | None = Query(None, description="Exact entity_type match"),
    include_tombstoned: bool = Query(
        False, description="Include superseded rows (default: false)"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ListResponse:
    """List current (non-superseded) beliefs matching the given scope.

    Pagination is stable under writes only when you filter on enough
    scope to produce a deterministic ordering; we always `ORDER BY
    observed_at DESC, id DESC` as a tie-break.
    """
    _ensure_enabled()

    q = db.query(Belief)
    if not include_tombstoned:
        q = q.filter(Belief.superseded_at.is_(None))
    if user_id is not None:
        q = q.filter(Belief.user_id == user_id)
    if session_id is not None:
        q = q.filter(Belief.session_id == session_id)
    if entity is not None:
        q = q.filter(Belief.entity == entity)
    if predicate is not None:
        q = q.filter(Belief.predicate == predicate)
    if entity_type is not None:
        q = q.filter(Belief.entity_type == entity_type)

    total = q.count()
    rows = (
        q.order_by(Belief.observed_at.desc(), Belief.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    now = datetime.now(UTC) if rows else None
    return ListResponse(
        total=total,
        limit=limit,
        offset=offset,
        beliefs=[_to_view(b, now=now) for b in rows],
    )


# ---------------------------------------------------------------------------
# GET /memory/{belief_id}/history — bitemporal version chain
# ---------------------------------------------------------------------------


@router.get("/{belief_id}/history", response_model=HistoryResponse)
def belief_history(
    belief_id: str,
    db: Session = Depends(get_db),
) -> HistoryResponse:
    """Return all versions (live + superseded) of the (entity, predicate)
    that this belief belongs to, oldest-first.

    Answers "how did the agent's view of X evolve?" — the bitemporal
    question that motivated the `observed_at` / `superseded_at` columns.
    Scope (user/session/agent) is pulled from the seed belief so callers
    don't accidentally mix tenants when asking "what did we know
    about alice's theme?".
    """
    _ensure_enabled()

    seed = db.query(Belief).filter(Belief.id == belief_id).first()
    if seed is None:
        raise NexusAPIError(
            404, "belief_not_found", f"No belief with id {belief_id!r}"
        )

    q = db.query(Belief).filter(
        Belief.entity == seed.entity,
        Belief.predicate == seed.predicate,
    )
    # Preserve tenant scope so history doesn't leak across users.
    if seed.user_id is not None:
        q = q.filter(Belief.user_id == seed.user_id)

    rows = q.order_by(Belief.observed_at.asc(), Belief.id.asc()).all()
    return HistoryResponse(
        entity=seed.entity,
        predicate=seed.predicate,
        versions=[_to_view(b) for b in rows],
    )


# ---------------------------------------------------------------------------
# GET /memory/{belief_id}/explain — why was this belief ranked where it was?
# ---------------------------------------------------------------------------


@router.get("/{belief_id}/explain", response_model=ExplainResponse)
def explain_belief(
    belief_id: str,
    query_text: str = Query("", description="Query context for ranking"),
    db: Session = Depends(get_db),
) -> ExplainResponse:
    """Re-run retrieval ranking over the belief's scope and report the
    per-signal breakdown for this belief.

    This is the user-facing "why did Nexus surface this?" inspector.
    The score depends on `query_text`; passing an empty string yields
    ranking purely from confidence + episodic signals.
    """
    _ensure_enabled()

    belief = db.query(Belief).filter(Belief.id == belief_id).first()
    if belief is None:
        raise NexusAPIError(
            404, "belief_not_found", f"No belief with id {belief_id!r}"
        )

    # Build the scope candidate set. Same filter as retrieval uses at
    # runtime — live rows within the belief's user scope.
    candidates_q = db.query(Belief).filter(Belief.superseded_at.is_(None))
    if belief.user_id is not None:
        candidates_q = candidates_q.filter(Belief.user_id == belief.user_id)
    candidates = candidates_q.limit(500).all()

    query = RetrievalQuery(
        text=query_text,
        user_id=belief.user_id,
        session_id=belief.session_id,
        limit=max(len(candidates), 1),
    )
    scored = retrieve(query, candidates)
    match = next((s for s in scored if s.belief.id == belief_id), None)

    if match is None:
        return ExplainResponse(
            belief=_to_view(belief),
            query_text=query_text,
            rrf_score=0.0,
            signals=[],
            rank_in_scope=0,  # 0 = not in retrieval output (e.g. scope too small)
        )

    rank = next(
        (i + 1 for i, s in enumerate(scored) if s.belief.id == belief_id),
        0,
    )
    return ExplainResponse(
        belief=_to_view(belief),
        query_text=query_text,
        rrf_score=match.rrf_score,
        signals=[
            SignalBreakdown(signal=name, score=score)
            for name, score in sorted(
                match.signals.items(), key=lambda kv: kv[1], reverse=True
            )
        ],
        rank_in_scope=rank,
    )


# ---------------------------------------------------------------------------
# POST /memory/forget — tombstone
# ---------------------------------------------------------------------------


@router.post("/forget", response_model=ForgetResponse)
def forget_belief(
    req: ForgetRequest,
    db: Session = Depends(get_db),
) -> ForgetResponse:
    """Tombstone all live beliefs matching the request.

    Tombstoning preserves the row (causal graph + audit trail stay
    intact) and sets `superseded_at = now`. Retrieval filters these
    out by default.

    Scope: when `user_id` is provided, only rows scoped to that user
    OR globally (user_id IS NULL) are tombstoned. This lets tenants
    purge their own data without accidentally touching other users.
    """
    _ensure_enabled()

    n = forget_by_entity(
        db,
        entity=req.entity,
        predicate=req.predicate,
        user_id=req.user_id,
    )
    if n:
        db.commit()

    return ForgetResponse(
        tombstoned=n,
        entity=req.entity,
        predicate=req.predicate,
        user_id=req.user_id,
    )


# ---------------------------------------------------------------------------
# GET /memory/stats — subsystem health
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=StatsResponse)
def memory_stats(db: Session = Depends(get_db)) -> StatsResponse:
    """Point-in-time counts + configured decay profile.

    When `MEMORY_ENABLED=False` this still returns a structured
    response (rather than 503) so operators can verify that the
    subsystem is genuinely disabled without flipping the flag.
    """
    if not settings.MEMORY_ENABLED:
        return StatsResponse(
            enabled=False,
            total_live=0,
            total_tombstoned=0,
            by_entity_type={},
            by_source_type={},
            decay_profile=_format_half_life(settings.MEMORY_DECAY_PROFILE),
        )

    total_live = (
        db.query(Belief).filter(Belief.superseded_at.is_(None)).count()
    )
    total_tombstoned = (
        db.query(Belief).filter(Belief.superseded_at.isnot(None)).count()
    )

    live_rows = (
        db.query(Belief.entity_type, Belief.source_type)
        .filter(Belief.superseded_at.is_(None))
        .all()
    )
    by_entity_type: dict[str, int] = {}
    by_source_type: dict[str, int] = {}
    for et, st in live_rows:
        et_key = et or "unknown"
        st_key = st or "unknown"
        by_entity_type[et_key] = by_entity_type.get(et_key, 0) + 1
        by_source_type[st_key] = by_source_type.get(st_key, 0) + 1

    return StatsResponse(
        enabled=True,
        total_live=total_live,
        total_tombstoned=total_tombstoned,
        by_entity_type=by_entity_type,
        by_source_type=by_source_type,
        decay_profile=_format_half_life(settings.MEMORY_DECAY_PROFILE),
    )
