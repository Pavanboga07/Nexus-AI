"""The deterministic Decision Engine for Nexus Autonomy (Part 10).

The Decision Engine proposes and evaluates actions deterministically across
a 13-step pipeline. The LLM is NEVER the security authority.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.autonomy.limits import check_limits, classify_risk
from app.autonomy.models import (
    ActionType,
    AutonomyConfig,
    AutonomyMode,
    AutonomyRun,
    DecisionResult,
    RiskLevel,
)
from app.policy.engine import EvaluationRequest
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService

logger = logging.getLogger("nexus.autonomy.decision_engine")

# Explicit action allowlist
ALLOWED_ACTIONS = frozenset(a.value for a in ActionType)


@dataclass(frozen=True)
class DecisionRequest:
    action_type: str
    proposed_action: str
    purpose: str
    goal: str
    target_agent_id: str | None = None
    tool_name: str | None = None
    required_data_categories: Sequence[str] | None = None
    payload: dict[str, Any] | None = None
    trigger: str = "user_request"
    run: AutonomyRun | None = None


@dataclass(frozen=True)
class DecisionOutcome:
    decision: DecisionResult
    reason: str
    risk_level: RiskLevel
    action_type: str
    purpose: str
    policy_decision: str | None = None
    consent_decision: str | None = None
    requires_approval: bool = False


class DecisionEngine:
    """Authoritative decision engine for all autonomous actions."""

    def __init__(
        self,
        *,
        policy_service: PolicyService,
        a2a_service: Any = None,
        tool_service: Any = None,
    ) -> None:
        self._policy = policy_service
        self._a2a = a2a_service
        self._tools = tool_service

    async def evaluate(
        self,
        owner_id: uuid.UUID,
        config: AutonomyConfig,
        request: DecisionRequest,
    ) -> DecisionOutcome:
        """Evaluate a proposed action across the deterministic 13-step pipeline."""
        # 1. Validate request structure
        if not request.proposed_action or not request.purpose or not request.goal:
            return DecisionOutcome(
                decision=DecisionResult.DENY,
                reason="Invalid request: missing proposed_action, purpose, or goal",
                risk_level=RiskLevel.HIGH,
                action_type=request.action_type,
                purpose=request.purpose or "unknown",
            )

        # 2. Identify owner
        if not owner_id:
            return DecisionOutcome(
                decision=DecisionResult.DENY,
                reason="Missing owner identity",
                risk_level=RiskLevel.HIGH,
                action_type=request.action_type,
                purpose=request.purpose,
            )

        # 3. Validate purpose slug
        purpose_slug = request.purpose.lower().replace(" ", "_")

        # 4. Validate action against explicit allowlist
        act_type_norm = request.action_type.lower()
        if act_type_norm not in ALLOWED_ACTIONS:
            return DecisionOutcome(
                decision=DecisionResult.DENY,
                reason=f"Action '{request.action_type}' is not in the explicit allowlist",
                risk_level=RiskLevel.CRITICAL,
                action_type=request.action_type,
                purpose=purpose_slug,
            )

        # 5. Determine required data categories
        categories = list(request.required_data_categories or [])

        # 6. Check trusted-agent state if remote contact
        is_trusted_agent = True
        if request.target_agent_id:
            is_trusted_agent = False
            if self._a2a:
                trusted = await self._a2a.get_trusted_agent(request.target_agent_id)
                if trusted and trusted.status == "active":
                    is_trusted_agent = True

        # 7. Determine deterministic RiskLevel
        risk = classify_risk(
            action_type=act_type_norm,
            data_categories=categories,
            target_agent_id=request.target_agent_id,
            is_trusted=is_trusted_agent,
            tool_name=request.tool_name,
        )

        # 8. Check AutonomyConfig mode and enabled status
        mode = AutonomyMode(config.mode) if config.mode in [m.value for m in AutonomyMode] else AutonomyMode.BOUNDED
        if not config.enabled or mode is AutonomyMode.OFF:
            return DecisionOutcome(
                decision=DecisionResult.STOP,
                reason="Autonomy is disabled (mode=OFF or enabled=False)",
                risk_level=risk,
                action_type=act_type_norm,
                purpose=purpose_slug,
            )

        # 9. Untrusted remote agent check
        if request.target_agent_id and not is_trusted_agent:
            return DecisionOutcome(
                decision=DecisionResult.DENY,
                reason=f"Remote agent '{request.target_agent_id}' is not in the trusted registry or has been revoked",
                risk_level=RiskLevel.CRITICAL,
                action_type=act_type_norm,
                purpose=purpose_slug,
            )

        # 10. Check runtime limits against run state
        if request.run is not None:
            within_limits, violation = check_limits(request.run, config)
            if not within_limits:
                return DecisionOutcome(
                    decision=DecisionResult.STOP,
                    reason=f"Autonomy limit reached: {violation}",
                    risk_level=risk,
                    action_type=act_type_norm,
                    purpose=purpose_slug,
                )

        # 11. Evaluate PolicyService & ConsentService
        primary_category = categories[0] if categories else "general"
        requester_id = request.target_agent_id or "nexus:self"

        eval_req = EvaluationRequest(
            requester_agent_id=requester_id,
            data_category=primary_category,
            action=act_type_norm,
            purpose=purpose_slug,
        )
        policy_res = await self._policy.evaluate(owner_id, eval_req)

        if policy_res.decision is PolicyDecision.DENY:
            return DecisionOutcome(
                decision=DecisionResult.DENY,
                reason=f"Policy DENY: {policy_res.reason}",
                risk_level=risk,
                action_type=act_type_norm,
                purpose=purpose_slug,
                policy_decision="DENY",
            )

        # 12. Mode-based approval checks
        if mode is AutonomyMode.ASSISTED:
            # Assisted mode requires owner approval for any external or state-changing action
            if act_type_norm not in {ActionType.READ_MEMORY.value, ActionType.READ_CONTEXT.value}:
                return DecisionOutcome(
                    decision=DecisionResult.ASK,
                    reason="Assisted mode: owner approval required before execution",
                    risk_level=risk,
                    action_type=act_type_norm,
                    purpose=purpose_slug,
                    requires_approval=True,
                )

        if config.require_approval_for_sensitive_data and risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            if categories:
                return DecisionOutcome(
                    decision=DecisionResult.ASK,
                    reason=f"Action involves sensitive data categories: {categories}",
                    risk_level=risk,
                    action_type=act_type_norm,
                    purpose=purpose_slug,
                    requires_approval=True,
                )

        if config.require_approval_for_external_communication and request.target_agent_id:
            # In BOUNDED mode, external communication requires approval unless low-risk read
            if mode is AutonomyMode.BOUNDED and risk is not RiskLevel.LOW:
                return DecisionOutcome(
                    decision=DecisionResult.ASK,
                    reason=f"External communication to '{request.target_agent_id}' requires owner approval",
                    risk_level=risk,
                    action_type=act_type_norm,
                    purpose=purpose_slug,
                    requires_approval=True,
                )

        # If an explicit policy or consent rule matched
        if policy_res.matched_policy_id or policy_res.matched_consent_id:
            if policy_res.decision is PolicyDecision.ASK:
                return DecisionOutcome(
                    decision=DecisionResult.ASK,
                    reason=f"Policy requires owner consent: {policy_res.reason}",
                    risk_level=risk,
                    action_type=act_type_norm,
                    purpose=purpose_slug,
                    policy_decision="ASK",
                    requires_approval=True,
                )
        else:
            # Default policy fallback (no explicit rule matched)
            # In BOUNDED and FULLY_DELEGATED modes, low-risk actions are pre-approved by autonomy config
            if mode in {AutonomyMode.BOUNDED, AutonomyMode.FULLY_DELEGATED} and risk is RiskLevel.LOW:
                pass
            elif policy_res.decision is PolicyDecision.ASK:
                return DecisionOutcome(
                    decision=DecisionResult.ASK,
                    reason=f"Policy requires owner consent: {policy_res.reason}",
                    risk_level=risk,
                    action_type=act_type_norm,
                    purpose=purpose_slug,
                    policy_decision="ASK",
                    requires_approval=True,
                )

        # 13. All gates passed -> ALLOW
        return DecisionOutcome(
            decision=DecisionResult.ALLOW,
            reason=f"Action permitted under {mode.value} mode. Policy: ALLOW ({policy_res.reason})",
            risk_level=risk,
            action_type=act_type_norm,
            purpose=purpose_slug,
            policy_decision="ALLOW",
            consent_decision=policy_res.matched_consent_id,
        )


__all__ = [
    "ALLOWED_ACTIONS",
    "DecisionEngine",
    "DecisionOutcome",
    "DecisionRequest",
]
