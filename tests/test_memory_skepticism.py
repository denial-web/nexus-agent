"""Tests for the stakes-aware skepticism gate."""

import pytest
from app.core.memory.confidence import (
    from_mean_and_strength,
)
from app.core.memory.skepticism import (
    DEFAULT_STAKES,
    BeliefDraft,
    PriorBelief,
    evaluate,
    parse_stakes,
)


def _draft(**overrides):
    base = {
        "entity": "user:alice",
        "predicate": "prefers_communication_style",
        "value": "concise",
        "entity_type": "preference",
        "confidence": from_mean_and_strength(mean=0.9, strength=10.0),
        "source_type": "user_stated",
    }
    base.update(overrides)
    return BeliefDraft(**base)


def _prior(value, confidence=None, source_type="observed", bid="prior-1"):
    return PriorBelief(
        id=bid,
        value=value,
        confidence=confidence or from_mean_and_strength(mean=0.8, strength=10.0),
        source_type=source_type,
    )


class TestParseStakes:
    def test_empty_returns_defaults(self):
        assert parse_stakes("") == DEFAULT_STAKES

    def test_overrides_and_adds(self):
        got = parse_stakes("identity=0.95,custom=0.42")
        assert got["identity"] == pytest.approx(0.95)
        assert got["custom"] == pytest.approx(0.42)
        # Preserves unmentioned defaults.
        assert got["preference"] == DEFAULT_STAKES["preference"]

    def test_malformed_pairs_ignored(self):
        got = parse_stakes("identity=0.9,notapair,financial=bogus,preference=0.6")
        assert got["identity"] == pytest.approx(0.9)
        assert got["preference"] == pytest.approx(0.6)
        assert "notapair" not in got


class TestNoPriorBeliefs:
    def test_accept_when_confident_enough(self):
        decision = evaluate(_draft(), prior_beliefs=[])
        assert decision.verdict == "accept"
        assert "over_threshold" in decision.reason

    def test_reject_when_below_stakes(self):
        weak_draft = _draft(
            entity_type="identity",  # stakes = 0.9
            confidence=from_mean_and_strength(mean=0.5, strength=4.0),
            source_type="inferred",
        )
        decision = evaluate(weak_draft, prior_beliefs=[])
        assert decision.verdict == "needs_evidence"
        assert decision.required_confidence == pytest.approx(DEFAULT_STAKES["identity"])


class TestWithPriors:
    def test_agreement_accepts_without_conflict(self):
        draft = _draft(value="concise")
        prior = _prior(value="concise")
        decision = evaluate(draft, prior_beliefs=[prior])
        assert decision.verdict == "accept"
        assert decision.contradicts == []

    def test_supersede_when_candidate_is_much_stronger(self):
        draft = _draft(
            value="verbose",
            confidence=from_mean_and_strength(mean=0.97, strength=20.0),
            source_type="user_stated",
        )
        prior = _prior(
            value="concise",
            confidence=from_mean_and_strength(mean=0.6, strength=5.0),
            source_type="inferred",
        )
        decision = evaluate(draft, prior_beliefs=[prior])
        assert decision.verdict == "supersede"
        assert prior.id in decision.contradicts

    def test_reject_when_stronger_prior_conflicts(self):
        draft = _draft(
            value="verbose",
            confidence=from_mean_and_strength(mean=0.55, strength=4.0),
            source_type="inferred",
        )
        prior = _prior(
            value="concise",
            confidence=from_mean_and_strength(mean=0.9, strength=30.0),
            source_type="user_stated",
        )
        decision = evaluate(draft, prior_beliefs=[prior])
        assert decision.verdict == "reject"
        assert prior.id in decision.contradicts


class TestStakesGradient:
    """High-stakes beliefs must require stronger evidence to be accepted."""

    def test_identity_requires_more_than_preference(self):
        # Same effective confidence, different entity_type.
        # Mean 0.75 with user_stated source → effective = 0.75 * 1.0 = 0.75.
        # Preference threshold = 0.5 → accept. Identity threshold = 0.9 → reject.
        moderate_conf = from_mean_and_strength(mean=0.75, strength=10.0)
        pref_draft = _draft(
            entity_type="preference",
            confidence=moderate_conf,
            source_type="user_stated",
        )
        id_draft = _draft(
            entity_type="identity",
            confidence=moderate_conf,
            source_type="user_stated",
        )
        pref_decision = evaluate(pref_draft, prior_beliefs=[])
        id_decision = evaluate(id_draft, prior_beliefs=[])
        assert pref_decision.verdict == "accept"
        assert id_decision.verdict == "needs_evidence"


class TestSourceTrust:
    def test_user_stated_beats_inferred_with_equal_means(self):
        # Same Beta, same entity_type, only source_type differs.
        conf = from_mean_and_strength(mean=0.7, strength=10.0)
        user = _draft(source_type="user_stated", confidence=conf)
        inferred = _draft(source_type="inferred", confidence=conf)
        assert evaluate(user, prior_beliefs=[]).verdict == "accept"
        # Inferred source multiplied by 0.6 takes effective conf to ~0.42,
        # below the 0.5 preference threshold.
        assert evaluate(inferred, prior_beliefs=[]).verdict == "needs_evidence"
