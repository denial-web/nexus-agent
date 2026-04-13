"""
Covernor policy engine — default-deny governance layer.

Evaluates proposed actions against the policy table. Unknown actions
are denied by default. Matching policies determine whether an action
is auto-allowed, requires K-of-N approval, or is blocked.
"""

import fnmatch
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class PolicyDecision:
    action: str
    decision: str  # "allow", "require_approval", "deny"
    policy_id: str | None
    policy_name: str | None
    risk_level: str
    required_approvals: int
    reason: str


def evaluate_action(
    action_type: str,
    resource: str | None = None,
    parameters: dict | None = None,
    db_session: Session | None = None,
) -> PolicyDecision:
    """
    Evaluate a proposed action against the policy table.

    Default-deny: if no policy matches, the action is denied.
    """
    if not db_session:
        return PolicyDecision(
            action=action_type,
            decision="deny",
            policy_id=None,
            policy_name=None,
            risk_level="unknown",
            required_approvals=0,
            reason="No database session — default deny",
        )

    from app.models.policy import Policy

    policies = db_session.query(Policy).filter_by(is_active=True).order_by(Policy.priority).all()

    for policy in policies:
        if not _matches(policy.action_pattern, action_type):
            continue
        if policy.resource_pattern:
            if not resource or not _matches(policy.resource_pattern, resource):
                continue

        if parameters and policy.blocked_parameters:
            for blocked_key in policy.blocked_parameters:
                if blocked_key in parameters:
                    return PolicyDecision(
                        action=action_type,
                        decision="deny",
                        policy_id=policy.id,
                        policy_name=policy.name,
                        risk_level=policy.risk_level,
                        required_approvals=0,
                        reason=f"Blocked parameter: {blocked_key}",
                    )

        try:
            approvals = int(policy.required_approvals)
        except (ValueError, TypeError):
            logger.error(
                "Policy %s has non-integer required_approvals=%r — defaulting to deny",
                policy.name,
                policy.required_approvals,
            )
            return PolicyDecision(
                action=action_type,
                decision="deny",
                policy_id=policy.id,
                policy_name=policy.name,
                risk_level=policy.risk_level,
                required_approvals=0,
                reason=f"Corrupt required_approvals in policy: {policy.name}",
            )

        return PolicyDecision(
            action=action_type,
            decision=policy.decision,
            policy_id=policy.id,
            policy_name=policy.name,
            risk_level=policy.risk_level,
            required_approvals=approvals,
            reason=f"Matched policy: {policy.name}",
        )

    return PolicyDecision(
        action=action_type,
        decision="deny",
        policy_id=None,
        policy_name=None,
        risk_level="unknown",
        required_approvals=0,
        reason="No matching policy — default deny",
    )


def _matches(pattern: str, value: str) -> bool:
    """Match using glob-style patterns."""
    return fnmatch.fnmatch(value.lower(), pattern.lower())
