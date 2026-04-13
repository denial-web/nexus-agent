"""
A-S-FLC (Asymmetric Signed Force-Loop-Chain) decision engine.

Forces the LLM to map out decision trees with asymmetric risk treatment:
- Positive outcomes are trusted at face value (100%)
- Negative outcomes get a conservative uncertainty buffer (delta)

The engine loops until scores converge, then outputs the highest-confidence
path. This prevents the model from choosing adversarial "trap" decisions.
"""

import logging
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EventNode:
    """A single node in a decision chain."""

    description: str
    probability: float
    impact: float
    is_positive: bool

    @property
    def signed_value(self) -> float:
        if self.is_positive:
            return self.probability * self.impact
        delta = settings.ASFLC_UNCERTAINTY_DELTA
        adjusted_prob = min(self.probability + delta, 1.0)
        return -(adjusted_prob * abs(self.impact))


@dataclass
class DecisionPath:
    """A chain of events representing one possible decision path."""

    name: str
    events: list[EventNode] = field(default_factory=list)
    _score_history: list[float] = field(default_factory=list)

    @property
    def chain_score(self) -> float:
        if not self.events:
            return 0.0
        return sum(e.signed_value for e in self.events)

    @property
    def confidence(self) -> float:
        if len(self._score_history) < 2:
            return 0.0
        recent = self._score_history[-2:]
        delta = abs(recent[-1] - recent[-2])
        return max(0.0, 1.0 - delta)

    def record_score(self) -> None:
        self._score_history.append(self.chain_score)


@dataclass
class ASFLCResult:
    """Result of the decision engine evaluation."""

    chosen_path: str
    chosen_score: float
    confidence: float
    loops_taken: int
    all_paths: dict  # name -> {score, confidence, events}
    converged: bool
    chain_regret: float


def evaluate_paths(paths: list[DecisionPath]) -> ASFLCResult:
    """
    Run the force-loop-chain evaluation until convergence.

    Loops until all path scores are stable (delta < threshold)
    or max iterations are reached.
    """
    if not paths:
        return ASFLCResult(
            chosen_path="none",
            chosen_score=0.0,
            confidence=0.0,
            loops_taken=0,
            all_paths={},
            converged=True,
            chain_regret=0.0,
        )

    max_loops = max(settings.ASFLC_MAX_LOOPS, 1)
    threshold = settings.ASFLC_CONVERGENCE_THRESHOLD
    converged = False
    loop = 0

    for loop in range(1, max_loops + 1):
        for path in paths:
            path.record_score()

        if loop >= 2:
            all_stable = all(path.confidence >= (1.0 - threshold) for path in paths)
            if all_stable:
                converged = True
                break

    ranked = sorted(paths, key=lambda p: p.chain_score, reverse=True)
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None

    regret = 0.0
    if second and best.chain_score < 0 and second.chain_score >= 0:
        regret = abs(best.chain_score - second.chain_score)

    all_paths = {}
    for p in paths:
        all_paths[p.name] = {
            "score": round(p.chain_score, 4),
            "confidence": round(p.confidence, 4),
            "event_count": len(p.events),
        }

    return ASFLCResult(
        chosen_path=best.name,
        chosen_score=round(best.chain_score, 4),
        confidence=round(best.confidence, 4),
        loops_taken=loop,
        all_paths=all_paths,
        converged=converged,
        chain_regret=round(regret, 4),
    )


def build_paths_from_llm_output(raw_paths: list[dict]) -> list[DecisionPath]:
    """
    Parse LLM-generated decision analysis into DecisionPath objects.

    Expected format per path:
    {
        "name": "Path A: Accept offer",
        "events": [
            {"description": "...", "probability": 0.8, "impact": 100, "is_positive": true},
            {"description": "...", "probability": 0.3, "impact": -50, "is_positive": false}
        ]
    }
    """
    paths = []
    for raw in raw_paths:
        if not isinstance(raw, dict):
            continue
        events = []
        for e in raw.get("events", []):
            if not isinstance(e, dict):
                continue
            try:
                events.append(
                    EventNode(
                        description=str(e.get("description", "")),
                        probability=float(e.get("probability", 0.5)),
                        impact=float(e.get("impact", 0)),
                        is_positive=bool(e.get("is_positive", True)),
                    )
                )
            except (ValueError, TypeError):
                continue
        paths.append(DecisionPath(name=str(raw.get("name", "unnamed")), events=events))
    return paths
