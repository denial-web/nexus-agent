"""
Belief retrieval via Reciprocal Rank Fusion (RRF).

RRF is a signal-fusion algorithm that ranks a document by the sum of
reciprocals of its rank across multiple orderings:

    score(doc) = sum over signals s of  1 / (k + rank_s(doc))

Properties that make it a good fit here:

- Deterministic and tunable with a single constant (k ≈ 60).
- Does not require signal scores to be calibrated to each other.
- Missing signals simply contribute nothing — no penalty for a doc that
  only appears in half the orderings.

Signals we fuse (all optional, degrade gracefully):

  1. Semantic similarity — cosine over `Belief.embedding` (when the query
     embedder is available).
  2. Lexical (BM25-ish over Belief.keywords + predicate tokens).
  3. Entity/predicate exact match — massive boost for subject-line hits.
  4. Episodic co-occurrence — beliefs formed in the same session or trace
     as the query context.
  5. Confidence — Beta.strength() as a global tie-breaker so we don't
     rank a weak-but-semantically-close belief above a strong one.

This module is pure ranking logic. It takes a list of candidate beliefs
(already loaded from the DB by a thin caller) plus a query context and
returns the top-k ordered. The DB query layer lives in `writer.py` /
`api/memory.py`.

The whole module is a no-op (returns []) when `settings.MEMORY_ENABLED=False`.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.models.belief import Belief


_RRF_K = 60  # standard RRF smoothing constant (Cormack, Clarke, Büttcher 2009)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    entities: list[str] = field(default_factory=list)  # e.g. ["user:alice"]
    predicates: list[str] = field(default_factory=list)
    session_id: str | None = None
    user_id: str | None = None
    embedding: list[float] | None = None  # query vector (optional)
    limit: int = 5


@dataclass(frozen=True)
class ScoredBelief:
    belief: Belief
    rrf_score: float
    signals: dict[str, float]  # per-signal contribution, for /explain API


# ---------------------------------------------------------------------------
# Signal implementations — each returns a list of (belief_id, score) pairs
# ordered best-first. Scores are informational only; RRF uses ranks.
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _semantic_rank(
    query: RetrievalQuery, beliefs: list[Belief]
) -> list[tuple[str, float]]:
    if not query.embedding:
        return []
    scored: list[tuple[str, float]] = []
    for b in beliefs:
        emb = getattr(b, "embedding", None)
        if not emb:
            continue
        scored.append((b.id, _cosine(query.embedding, list(emb))))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(bid, s) for bid, s in scored if s > 0.0]


def _lexical_rank(
    query: RetrievalQuery, beliefs: list[Belief]
) -> list[tuple[str, float]]:
    """Tiny BM25-ish over keywords + predicate. Not a real BM25 — we don't
    care about term frequency inside keywords lists, just coverage."""
    query_tokens = set(_tokenize(query.text))
    query_tokens |= {t for p in query.predicates for t in _tokenize(p)}
    if not query_tokens:
        return []
    scored: list[tuple[str, float]] = []
    for b in beliefs:
        terms: set[str] = set()
        if b.keywords:
            terms |= {str(k).lower() for k in b.keywords}
        terms |= set(_tokenize(b.predicate or ""))
        if not terms:
            continue
        overlap = query_tokens & terms
        if not overlap:
            continue
        # Inverse-document-frequency flavor: longer belief keyword lists
        # get a small penalty so we don't always top-rank the most
        # verbose belief.
        score = len(overlap) / (1.0 + math.log(1.0 + len(terms)))
        scored.append((b.id, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _entity_rank(
    query: RetrievalQuery, beliefs: list[Belief]
) -> list[tuple[str, float]]:
    if not query.entities and not query.predicates:
        return []
    entities = set(query.entities)
    predicates = set(query.predicates)
    scored: list[tuple[str, float]] = []
    for b in beliefs:
        score = 0.0
        if entities and b.entity in entities:
            score += 1.0
        if predicates and b.predicate in predicates:
            score += 0.5
        if score > 0.0:
            scored.append((b.id, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _episodic_rank(
    query: RetrievalQuery, beliefs: list[Belief]
) -> list[tuple[str, float]]:
    """Beliefs formed in the same session or by the same user win here.

    This is what gives an agent "stickiness" within a session — recent
    beliefs from this session should outrank older beliefs from other
    sessions even if lexical overlap is identical.
    """
    if not query.session_id and not query.user_id:
        return []
    scored: list[tuple[str, float]] = []
    for b in beliefs:
        score = 0.0
        if query.session_id and b.session_id == query.session_id:
            score += 1.0
        if query.user_id and b.user_id == query.user_id:
            score += 0.5
        if score > 0.0:
            scored.append((b.id, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _confidence_rank(beliefs: list[Belief]) -> list[tuple[str, float]]:
    """Global tie-breaker — prefer strongly-held beliefs."""
    from app.core.memory.confidence import BetaConfidence

    scored = [
        (
            b.id,
            BetaConfidence(
                alpha=float(b.confidence_alpha or 1.0),
                beta=float(b.confidence_beta or 1.0),
            ).strength(),
        )
        for b in beliefs
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------


def _rrf_fuse(
    rankings: dict[str, list[tuple[str, float]]],
    k: int = _RRF_K,
) -> dict[str, tuple[float, dict[str, float]]]:
    totals: dict[str, float] = defaultdict(float)
    signals: dict[str, dict[str, float]] = defaultdict(dict)
    for signal_name, ranked in rankings.items():
        for rank, (bid, raw_score) in enumerate(ranked, start=1):
            contribution = 1.0 / (k + rank)
            totals[bid] += contribution
            signals[bid][signal_name] = raw_score
    return {bid: (totals[bid], signals[bid]) for bid in totals}


def retrieve(
    query: RetrievalQuery,
    candidates: list[Belief],
) -> list[ScoredBelief]:
    """Rank `candidates` against `query` via RRF. Returns top-`query.limit`.

    Caller responsibilities:
      - Load `candidates` from the DB (scoped by user_id / session_id /
        entity / not-superseded, whatever makes sense for the call site).
      - Keep the candidate set small enough to rank in-memory (O(100s)).

    When `settings.MEMORY_ENABLED` is False this function short-circuits
    to `[]`. Callers must not rely on any side effects of calling it.
    """
    if not settings.MEMORY_ENABLED:
        return []
    if not candidates:
        return []

    rankings: dict[str, list[tuple[str, float]]] = {
        "semantic": _semantic_rank(query, candidates),
        "lexical": _lexical_rank(query, candidates),
        "entity": _entity_rank(query, candidates),
        "episodic": _episodic_rank(query, candidates),
        "confidence": _confidence_rank(candidates),
    }
    # Drop empty rankings so they don't pollute the signals dict.
    rankings = {name: r for name, r in rankings.items() if r}
    if not rankings:
        return []

    fused = _rrf_fuse(rankings)
    by_id = {b.id: b for b in candidates}

    ordered = sorted(
        fused.items(),
        key=lambda kv: (kv[1][0], by_id[kv[0]].observed_at or 0),
        reverse=True,
    )
    top = ordered[: max(1, query.limit)]
    return [
        ScoredBelief(belief=by_id[bid], rrf_score=score, signals=sig)
        for bid, (score, sig) in top
    ]
