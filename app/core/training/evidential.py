"""
Evidential loss metadata enrichment for training exports.

Attaches uncertainty signals to each training example so the fine-tuning
process can weight examples appropriately:

- A-S-FLC confidence & chain regret → decision uncertainty
- Critic scores per node → evaluation uncertainty
- ECE calibration adjustments → systematic bias correction
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def enrich_training_item(
    item: dict[str, Any],
    trace: Any,
    calibration_ece: float | None = None,
) -> dict[str, Any]:
    """
    Add evidential loss metadata to a single training export item.

    The returned item has an "evidential" key with uncertainty signals
    that downstream fine-tuning can use for loss weighting.
    """
    evidential: dict[str, Any] = {}

    if trace.asflc_confidence is not None:
        evidential["asflc_confidence"] = trace.asflc_confidence
    if trace.asflc_result and isinstance(trace.asflc_result, dict):
        evidential["chain_regret"] = trace.asflc_result.get("chain_regret", 0.0)
        evidential["asflc_converged"] = trace.asflc_result.get("converged", False)

    if trace.critic_scores and isinstance(trace.critic_scores, dict):
        node_scores = {}
        for node_name, score_data in trace.critic_scores.items():
            if isinstance(score_data, dict):
                node_scores[node_name] = score_data.get("score", 0.0)
        evidential["critic_node_scores"] = node_scores

        scores = list(node_scores.values())
        if scores:
            evidential["critic_mean_score"] = round(sum(scores) / len(scores), 4)
            evidential["critic_min_score"] = round(min(scores), 4)

    evidential["critic_verdict"] = trace.critic_verdict
    evidential["critic_rollback_count"] = trace.critic_rollback_count or 0

    if calibration_ece is not None:
        evidential["calibration_ece"] = calibration_ece
        evidential["calibration_adjustment"] = _compute_adjustment(calibration_ece)

    weight = _compute_sample_weight(evidential)
    evidential["suggested_weight"] = round(weight, 4)

    item["evidential"] = evidential
    return item


def _compute_adjustment(ece: float) -> float:
    """
    Compute a calibration adjustment factor.

    High ECE (poorly calibrated) → lower adjustment (reduce reliance).
    Low ECE (well calibrated) → adjustment near 1.0 (trust scores).
    """
    return max(0.1, 1.0 - ece)


def _compute_sample_weight(evidential: dict) -> float:
    """
    Compute a suggested training weight for this sample.

    Higher weight for:
    - High critic agreement (min_score close to mean_score)
    - High A-S-FLC confidence
    - Low calibration ECE (well-calibrated critic)

    Lower weight for:
    - Rollbacks (model was uncertain)
    - High chain regret (decision was risky)
    """
    weight = 1.0

    asflc_conf = evidential.get("asflc_confidence")
    if asflc_conf is not None:
        weight *= 0.5 + 0.5 * asflc_conf

    regret = evidential.get("chain_regret", 0.0)
    if regret > 0:
        weight *= max(0.3, 1.0 - regret * 0.5)

    rollbacks = evidential.get("critic_rollback_count", 0)
    if rollbacks > 0:
        weight *= max(0.4, 1.0 - rollbacks * 0.15)

    adj = evidential.get("calibration_adjustment")
    if adj is not None:
        weight *= adj

    return max(0.1, min(2.0, weight))
