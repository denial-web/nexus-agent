"""Tests for the BetaConfidence primitive."""

import pytest
from app.core.memory.confidence import (
    STRONG_POSITIVE,
    UNIFORM,
    WEAK_POSITIVE,
    BetaConfidence,
    combine,
    from_mean_and_strength,
)


class TestBasics:
    def test_uniform_prior_mean_is_half(self):
        assert UNIFORM.mean == pytest.approx(0.5)

    def test_mean_monotonic_in_alpha(self):
        a = BetaConfidence(2.0, 1.0).mean
        b = BetaConfidence(5.0, 1.0).mean
        assert b > a

    def test_variance_decreases_with_sample_size(self):
        small = BetaConfidence(2.0, 2.0).variance
        large = BetaConfidence(20.0, 20.0).variance
        assert large < small

    def test_strength_nonnegative_and_bounded(self):
        for c in [UNIFORM, WEAK_POSITIVE, STRONG_POSITIVE]:
            s = c.strength()
            assert 0.0 <= s <= 1.0


class TestEdgeCases:
    def test_zero_params_are_floored(self):
        c = BetaConfidence(0.0, 0.0)
        assert c.alpha > 0 and c.beta > 0

    def test_negative_update_rejected(self):
        with pytest.raises(ValueError):
            UNIFORM.update(positive=-1.0)
        with pytest.raises(ValueError):
            UNIFORM.update(negative=-0.5)

    def test_decay_factor_range_enforced(self):
        with pytest.raises(ValueError):
            UNIFORM.decay(0.0)
        with pytest.raises(ValueError):
            UNIFORM.decay(1.5)

    def test_from_mean_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            from_mean_and_strength(mean=-0.1, strength=5.0)
        with pytest.raises(ValueError):
            from_mean_and_strength(mean=1.5, strength=5.0)


class TestUpdateAndDecay:
    def test_positive_evidence_raises_mean(self):
        before = UNIFORM.mean
        after = UNIFORM.update(positive=5.0).mean
        assert after > before

    def test_negative_evidence_lowers_mean(self):
        before = UNIFORM.mean
        after = UNIFORM.update(negative=5.0).mean
        assert after < before

    def test_decay_preserves_mean(self):
        c = BetaConfidence(8.0, 2.0)
        decayed = c.decay(0.5)
        assert decayed.mean == pytest.approx(c.mean)
        assert decayed.sample_size == pytest.approx(c.sample_size * 0.5)


class TestCombineAndConstruct:
    def test_combine_adds_pseudocounts(self):
        a = BetaConfidence(3.0, 1.0)
        b = BetaConfidence(2.0, 2.0)
        c = combine(a, b)
        assert c.alpha == pytest.approx(5.0)
        assert c.beta == pytest.approx(3.0)

    def test_from_mean_roundtrips(self):
        c = from_mean_and_strength(mean=0.8, strength=10.0)
        assert c.mean == pytest.approx(0.8, abs=1e-6)
        assert c.sample_size == pytest.approx(10.0)
