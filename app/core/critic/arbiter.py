"""
GrokForge-Nexus Arbiter — the central critic tree node.

The Arbiter governs specialized leaf nodes (Safety, Reasoning, Quality,
Injection). It implements the chunked generate-then-verify loop:

1. Model generates a chunk of tokens
2. Each active leaf node evaluates the chunk
3. If any halt-capable node scores below threshold → rollback + [UNC] insertion
4. If rollback count exceeds limit → halt generation entirely
5. All critic scores are recorded for the trace

Phase 1: Post-hoc evaluation (evaluate full response after generation).
Phase 2: Streaming chunked evaluation during generation.
"""
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Protocol

from app.config import settings
from app.core.llm.models import LLMChunk

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class CriticNodeProtocol(Protocol):
    """Interface that all leaf nodes must implement."""
    name: str
    can_halt: bool

    def evaluate(self, context: dict) -> "CriticScore": ...


@dataclass
class CriticScore:
    node_name: str
    score: float  # 0.0 - 1.0
    verdict: str  # "pass", "warn", "fail"
    reasoning: str
    details: dict = field(default_factory=dict)


@dataclass
class ArbiterResult:
    verdict: str  # "pass", "rollback", "halt"
    scores: dict  # node_name -> CriticScore
    rollback_count: int
    halted_by: Optional[str]
    unc_inserted: bool


class Arbiter:
    """Central governor of the critic tree."""

    def __init__(self):
        self._nodes: dict[str, CriticNodeProtocol] = {}
        self._rollback_count = 0

    def register_node(self, node: CriticNodeProtocol):
        self._nodes[node.name] = node
        logger.info("Registered critic node: %s (can_halt=%s)", node.name, node.can_halt)

    def unregister_node(self, name: str):
        self._nodes.pop(name, None)

    @property
    def active_nodes(self) -> list[str]:
        return list(self._nodes.keys())

    def evaluate(self, context: dict) -> ArbiterResult:
        """
        Run all registered leaf nodes on the given context.

        Context should include:
        - prompt: the original prompt
        - response: the model's response (or chunk)
        - model_id: which model generated it
        - trace_id: for linking to the audit log
        """
        scores = {}
        halted_by = None
        verdict = "pass"

        for name, node in self._nodes.items():
            try:
                score = node.evaluate(context)
                scores[name] = score

                if score.verdict == "fail" and node.can_halt:
                    halted_by = name
                    verdict = "halt"
                    logger.warning(
                        "Critic %s HALTED generation (score=%.3f): %s",
                        name, score.score, score.reasoning,
                    )
                    break

                if score.verdict == "fail":
                    verdict = "rollback"
                    self._rollback_count += 1
                    logger.warning(
                        "Critic %s flagged rollback (score=%.3f): %s",
                        name, score.score, score.reasoning,
                    )

            except Exception:
                logger.exception("Critic node %s raised an exception", name)
                scores[name] = CriticScore(
                    node_name=name,
                    score=0.0,
                    verdict="fail",
                    reasoning="Node raised an exception",
                )

        if self._rollback_count > settings.CRITIC_MAX_ROLLBACKS and verdict == "rollback":
            verdict = "halt"
            halted_by = "arbiter:max_rollbacks"

        return ArbiterResult(
            verdict=verdict,
            scores={k: _score_to_dict(v) for k, v in scores.items()},
            rollback_count=self._rollback_count,
            halted_by=halted_by,
            unc_inserted=verdict == "rollback",
        )

    def evaluate_stream(self, context: dict, chunks: Iterable[LLMChunk]) -> ArbiterResult:
        """
        Evaluate a response delivered as streaming chunks.

        Accumulates text and triggers critic evaluation every CRITIC_CHUNK_SIZE
        *new* words (or on the final chunk). Inserts [UNC] on rollback, halts
        if max rollbacks exceeded.

        Rollback counting is managed here — evaluate() still increments
        _rollback_count as a side-effect, but evaluate_stream checks the limit
        after each batch.
        """
        accumulated = ""
        chunk_size = settings.CRITIC_CHUNK_SIZE
        words_at_last_eval = 0
        last_result: Optional[ArbiterResult] = None
        last_evaluated_snapshot: Optional[str] = None

        for chunk in chunks:
            accumulated += chunk.text
            words_now = len(accumulated.split())
            new_words = words_now - words_at_last_eval

            if new_words >= chunk_size or chunk.is_final:
                eval_context = {**context, "response": accumulated}
                last_result = self.evaluate(eval_context)
                last_evaluated_snapshot = accumulated
                words_at_last_eval = words_now

                if last_result.verdict == "halt":
                    return last_result

                if last_result.verdict == "rollback":
                    accumulated += " [UNC] "
                    words_at_last_eval = len(accumulated.split())
                    if self._rollback_count > settings.CRITIC_MAX_ROLLBACKS:
                        return ArbiterResult(
                            verdict="halt",
                            scores=last_result.scores,
                            rollback_count=self._rollback_count,
                            halted_by="arbiter:max_rollbacks_streaming",
                            unc_inserted=True,
                        )

        if last_evaluated_snapshot != accumulated:
            final_context = {**context, "response": accumulated}
            return self.evaluate(final_context)

        if last_result is not None:
            return last_result

        final_context = {**context, "response": accumulated}
        return self.evaluate(final_context)

    def reset(self):
        self._rollback_count = 0

    @classmethod
    def default_from_heuristics(cls) -> "Arbiter":
        """Built-in four-node tree (used when the registry is empty or no DB session)."""
        from app.core.critic.nodes import (
            InjectionCritic,
            QualityCritic,
            ReasoningCritic,
            SafetyCritic,
        )

        arbiter = cls()
        arbiter.register_node(ReasoningCritic())
        arbiter.register_node(InjectionCritic())
        arbiter.register_node(SafetyCritic())
        arbiter.register_node(QualityCritic())
        return arbiter

    @classmethod
    def load_from_registry(cls, db_session: "Session") -> "Arbiter":
        """Build an Arbiter from active `critic_registry` rows; fallback if none."""
        from app.models.critic_registry import CriticNode as RegistryCriticNode
        from app.core.critic.nodes import (
            InjectionCritic,
            LLMInjectionCritic,
            LLMReasoningCritic,
            QualityCritic,
            ReasoningCritic,
            SafetyCritic,
        )

        rows = (
            db_session.query(RegistryCriticNode)
            .filter_by(is_active=True)
            .order_by(RegistryCriticNode.node_type, RegistryCriticNode.name)
            .all()
        )
        if not rows:
            logger.info("No active critic_registry rows; using default heuristic Arbiter")
            return cls.default_from_heuristics()

        arbiter = cls()
        for row in rows:
            nt = (row.node_type or "").strip().lower()
            name = row.name
            tpl = (row.prompt_template or "").strip()
            use_llm = bool(tpl)

            try:
                if nt == "reasoning":
                    if use_llm:
                        arbiter.register_node(
                            LLMReasoningCritic(
                                name=name,
                                prompt_template=row.prompt_template,
                                threshold_pass=row.threshold_pass,
                                threshold_halt=row.threshold_halt,
                                can_halt=row.can_halt,
                                weight=row.weight,
                            )
                        )
                    else:
                        arbiter.register_node(
                            ReasoningCritic(
                                threshold_pass=row.threshold_pass,
                                threshold_halt=row.threshold_halt,
                                name=name,
                                can_halt=row.can_halt,
                            )
                        )
                elif nt == "injection":
                    if use_llm:
                        arbiter.register_node(
                            LLMInjectionCritic(
                                name=name,
                                prompt_template=row.prompt_template,
                                threshold_pass=row.threshold_pass,
                                threshold_halt=row.threshold_halt,
                                can_halt=row.can_halt,
                                weight=row.weight,
                            )
                        )
                    else:
                        arbiter.register_node(
                            InjectionCritic(name=name, can_halt=row.can_halt)
                        )
                elif nt == "safety":
                    arbiter.register_node(SafetyCritic(name=name, can_halt=row.can_halt))
                elif nt == "quality":
                    arbiter.register_node(
                        QualityCritic(
                            threshold_pass=row.threshold_pass,
                            name=name,
                            can_halt=row.can_halt,
                        )
                    )
                else:
                    logger.warning("Skipping unknown critic node_type %r for %r", row.node_type, name)
            except Exception:
                logger.exception("Failed to register critic %r; skipping", name)

        if not arbiter.active_nodes:
            logger.warning("Registry produced no nodes; using default heuristic Arbiter")
            return cls.default_from_heuristics()

        return arbiter


def _score_to_dict(score: CriticScore) -> dict:
    return {
        "node_name": score.node_name,
        "score": round(score.score, 4),
        "verdict": score.verdict,
        "reasoning": score.reasoning,
        "details": score.details,
    }
