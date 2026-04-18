"""
Beta-distributed confidence primitive.

We store agent belief strength as Beta(alpha, beta) instead of a flat scalar
because Beta is the conjugate prior for Bernoulli evidence, which means:

- Expected confidence = alpha / (alpha + beta) — trivially cheap.
- Updating on new evidence is O(1) and principled:
      alpha ← alpha + k_pos    (k_pos pieces of supporting evidence)
      beta  ← beta  + k_neg    (k_neg pieces of contradicting evidence)
- Variance = alpha*beta / ((alpha+beta)^2 * (alpha+beta+1)) gives us a
  proper uncertainty signal for retrieval ranking and the /explain API.
- Weak priors (e.g., Beta(1,1)) are genuinely uniform; strong priors
  (e.g., Beta(50,2)) require substantial contradicting evidence to flip.

This module is pure math. It has no DB, no LLM, no network — just a tiny
dataclass and the update/query rules. Everything above it (skepticism,
retrieval, writer) depends only on these primitives.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN_PARAM = 1e-6  # numerical floor so alpha/beta never go non-positive


@dataclass(frozen=True)
class BetaConfidence:
    """Beta(alpha, beta) belief strength."""

    alpha: float
    beta: float

    def __post_init__(self) -> None:
        # Frozen dataclass — object.__setattr__ is the supported escape hatch
        # for sanitizing inputs during construction.
        if self.alpha < _MIN_PARAM:
            object.__setattr__(self, "alpha", _MIN_PARAM)
        if self.beta < _MIN_PARAM:
            object.__setattr__(self, "beta", _MIN_PARAM)

    @property
    def mean(self) -> float:
        """Expected confidence, E[X] = alpha / (alpha + beta)."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def sample_size(self) -> float:
        """Effective pseudo-observation count (alpha + beta)."""
        return self.alpha + self.beta

    @property
    def variance(self) -> float:
        """Var[X] — higher means less certain."""
        a, b = self.alpha, self.beta
        s = a + b
        return (a * b) / ((s * s) * (s + 1.0))

    def strength(self) -> float:
        """Convex combination of mean and (1 - variance proxy).

        Useful as a single-number retrieval rank. Weight mean heavily,
        deduct for uncertainty. Values in (0, 1].
        """
        return max(0.0, min(1.0, self.mean * (1.0 - self.variance)))

    def update(self, *, positive: float = 0.0, negative: float = 0.0) -> BetaConfidence:
        """Return a new BetaConfidence updated with (positive, negative) evidence."""
        if positive < 0 or negative < 0:
            raise ValueError("evidence counts must be non-negative")
        return BetaConfidence(self.alpha + positive, self.beta + negative)

    def decay(self, factor: float) -> BetaConfidence:
        """Shrink both counts by `factor` (in [0, 1]).

        Used by the forgetting layer: old evidence counts less than new
        evidence, so we scale sample size down on each decay tick while
        preserving the mean.
        """
        if not 0.0 < factor <= 1.0:
            raise ValueError("decay factor must be in (0, 1]")
        return BetaConfidence(self.alpha * factor, self.beta * factor)

    def as_tuple(self) -> tuple[float, float]:
        return (self.alpha, self.beta)


# ---------------------------------------------------------------------------
# Named priors used across the memory subsystem.
# ---------------------------------------------------------------------------

#: Uniform prior — no information.
UNIFORM = BetaConfidence(alpha=1.0, beta=1.0)

#: Weak confirming prior — new observation reported by a single reliable source.
WEAK_POSITIVE = BetaConfidence(alpha=2.0, beta=1.0)

#: Strong confirming prior — multi-source corroboration or user-stated fact.
STRONG_POSITIVE = BetaConfidence(alpha=10.0, beta=1.0)

#: Weakly-held disbelief — we have a hint this is wrong but not much.
WEAK_NEGATIVE = BetaConfidence(alpha=1.0, beta=2.0)


def combine(a: BetaConfidence, b: BetaConfidence) -> BetaConfidence:
    """Independent-evidence combination: add the pseudo-counts.

    This is the right operation when two independent observations both
    support (or refute) the same proposition. It is NOT the right operation
    for averaging two inconsistent confidences — for that, use a skepticism
    check first (see `app.core.memory.skepticism`).
    """
    return BetaConfidence(a.alpha + b.alpha, a.beta + b.beta)


def from_mean_and_strength(mean: float, strength: float) -> BetaConfidence:
    """Construct Beta parameters from a target mean and effective sample size.

    `strength` is the pseudo-observation count (alpha + beta). Useful when
    the extractor reports a confidence score on a 0-1 scale and a rough
    "how sure was the evidence" knob.
    """
    if not 0.0 <= mean <= 1.0:
        raise ValueError("mean must be in [0, 1]")
    if strength < 2 * _MIN_PARAM:
        raise ValueError("strength must be >= 2 * _MIN_PARAM")
    alpha = mean * strength
    beta = (1.0 - mean) * strength
    return BetaConfidence(alpha=alpha, beta=beta)
