"""Tests for the RRF retrieval layer."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.core.memory.retrieval import (
    RetrievalQuery,
    retrieve,
)


# Lightweight fake Belief matching the ORM surface retrieval.py touches.
# We don't want these tests to depend on the DB — retrieval is pure ranking.
@dataclass
class FakeBelief:
    id: str
    entity: str = "user:alice"
    predicate: str = "prefers"
    value: object = "concise"
    keywords: list = field(default_factory=list)
    embedding: list = field(default_factory=list)
    confidence_alpha: float = 2.0
    confidence_beta: float = 1.0
    session_id: str | None = None
    user_id: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@pytest.fixture(autouse=True)
def _memory_on():
    """Flip the feature flag just for this module."""
    with patch("app.core.memory.retrieval.settings") as s:
        s.MEMORY_ENABLED = True
        yield


class TestFlagGating:
    def test_disabled_returns_empty_list(self):
        """With MEMORY_ENABLED=False the function must be a no-op."""
        with patch("app.core.memory.retrieval.settings") as s:
            s.MEMORY_ENABLED = False
            result = retrieve(
                RetrievalQuery(text="anything"),
                [FakeBelief(id="b1", keywords=["anything"])],
            )
        assert result == []

    def test_empty_candidates_returns_empty(self):
        result = retrieve(RetrievalQuery(text="hello"), [])
        assert result == []


class TestSignalBehavior:
    def test_entity_match_outranks_lexical_only(self):
        entity_hit = FakeBelief(id="entity", entity="user:alice", keywords=["x"])
        lexical_hit = FakeBelief(
            id="lexical", entity="user:bob", keywords=["hello", "world"]
        )
        query = RetrievalQuery(
            text="hello world", entities=["user:alice"], limit=2
        )
        result = retrieve(query, [entity_hit, lexical_hit])
        assert result[0].belief.id == "entity"

    def test_lexical_overlap_wins_when_no_entity_match(self):
        matches = FakeBelief(id="match", keywords=["python", "typing"])
        no_match = FakeBelief(id="nomatch", keywords=["ruby"])
        result = retrieve(
            RetrievalQuery(text="python typing", limit=2),
            [matches, no_match],
        )
        assert result[0].belief.id == "match"

    def test_semantic_cosine_ranks_similar_vectors_first(self):
        query_vec = [1.0, 0.0, 0.0]
        close = FakeBelief(id="close", embedding=[0.9, 0.1, 0.0])
        far = FakeBelief(id="far", embedding=[0.0, 0.0, 1.0])
        result = retrieve(
            RetrievalQuery(text="whatever", embedding=query_vec, limit=2),
            [close, far],
        )
        assert result[0].belief.id == "close"

    def test_episodic_match_boosts_same_session(self):
        same = FakeBelief(id="same", session_id="S-1", keywords=["x"])
        other = FakeBelief(id="other", session_id="S-2", keywords=["x"])
        result = retrieve(
            RetrievalQuery(text="x", session_id="S-1", limit=2),
            [same, other],
        )
        assert result[0].belief.id == "same"


class TestFusion:
    def test_rrf_combines_multiple_signals(self):
        """A belief that wins on several signals must outrank single-signal hits."""
        multi = FakeBelief(
            id="multi",
            entity="user:alice",
            keywords=["api", "keys"],
            session_id="S-1",
            user_id="U-1",
        )
        lexical_only = FakeBelief(
            id="lex_only", entity="user:bob", keywords=["api", "keys"]
        )
        entity_only = FakeBelief(
            id="ent_only", entity="user:alice", keywords=["foo"]
        )
        query = RetrievalQuery(
            text="api keys",
            entities=["user:alice"],
            session_id="S-1",
            user_id="U-1",
            limit=3,
        )
        result = retrieve(query, [multi, lexical_only, entity_only])
        assert result[0].belief.id == "multi"
        # All three should come back — the signals dict should be populated.
        assert len(result) == 3
        assert "signals" in vars(result[0])
        assert result[0].signals  # non-empty

    def test_signals_dict_records_sources(self):
        b = FakeBelief(id="b1", entity="user:alice", keywords=["api"])
        result = retrieve(
            RetrievalQuery(text="api", entities=["user:alice"], limit=1),
            [b],
        )
        assert result
        signals = result[0].signals
        # Lexical + entity + confidence at minimum (no embedding, no session).
        assert "lexical" in signals
        assert "entity" in signals
        assert "confidence" in signals

    def test_limit_is_respected(self):
        beliefs = [
            FakeBelief(id=f"b{i}", keywords=["shared"]) for i in range(10)
        ]
        result = retrieve(
            RetrievalQuery(text="shared", limit=3), beliefs
        )
        assert len(result) == 3


class TestBeliefsAsOfTzContract:
    """The bitemporal read helper is an audit-facing API: we'd rather
    fail loudly on a naive datetime than silently produce
    backend-dependent answers. See app/core/memory/retrieval.py's
    `beliefs_as_of` docstring for the full rationale."""

    def test_naive_datetime_raises_value_error(self):
        from datetime import datetime

        from app.core.memory.retrieval import beliefs_as_of

        naive = datetime(2026, 1, 1, 12, 0)  # no tzinfo
        assert naive.tzinfo is None
        with pytest.raises(ValueError, match="timezone-aware"):
            beliefs_as_of(db=None, at=naive)  # type: ignore[arg-type]

    def test_aware_datetime_short_circuits_when_flag_off(self):
        """The tz guard must run AFTER the feature-flag check, so a
        disabled memory subsystem doesn't raise on callers that happen
        to pass naive datetimes. Matches the rest of the module's
        flag-first posture."""
        from datetime import datetime

        from app.core.memory.retrieval import beliefs_as_of

        with patch("app.core.memory.retrieval.settings") as s:
            s.MEMORY_ENABLED = False
            result = beliefs_as_of(
                db=None,  # type: ignore[arg-type]
                at=datetime(2026, 1, 1, 12, 0),  # naive, but flag is off
            )
        assert result == []
