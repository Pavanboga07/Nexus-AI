"""Orchestration Executor: Runs plan steps through authorized services.

Never bypasses PolicyService, DecisionEngine, ConsentService, or A2AService.
Interprets raw cryptographic results into natural conversational language.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.service import A2AService
from app.autonomy.decision_engine import DecisionEngine, DecisionRequest
from app.autonomy.models import ActionType, DecisionResult
from app.orchestration.errors import (
    OrchestrationExecutionError,
    OrchestrationPolicyError,
    UntrustedAgentError,
)
from app.orchestration.schemas import (
    OrchestrationPlan,
    PlanStep,
    TargetResolution,
    TargetResolutionStatus,
)
from app.policy.engine import EvaluationRequest
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService

logger = logging.getLogger("nexus.orchestration.executor")


class OrchestrationExecutor:
    """Executes orchestration plans through authorized underlying services."""

    def __init__(
        self,
        *,
        a2a_service: A2AService,
        policy_service: PolicyService,
        decision_engine: DecisionEngine | None = None,
        workflow_service: Any = None,
    ) -> None:
        self._a2a = a2a_service
        self._policy = policy_service
        self._decision_engine = decision_engine
        self._workflow = workflow_service

    async def execute_plan(
        self,
        owner_id: uuid.UUID,
        plan: OrchestrationPlan,
        target: TargetResolution,
    ) -> dict[str, Any]:
        """Execute all steps of an OrchestrationPlan."""
        target_agent_id = plan.target_agent_id or target.agent_id
        target_name = plan.target_person or target.display_name or target.target_name
        step_results: list[dict[str, Any]] = []

        # Check trust of target agent
        if target.status == TargetResolutionStatus.DISCOVERED_AGENT or not target.is_trusted:
            raise UntrustedAgentError(
                target_name=target_name,
                agent_id=target_agent_id or "",
            )

        for step in plan.steps:
            if step.step_type == "resolve_target":
                continue

            # 1. Authoritative Policy & Decision Check (NEVER BYPASSED)
            approval_needed = await self._authorize_step(owner_id, step, target_agent_id, target_name)
            if approval_needed is not None:
                return approval_needed

            # 2. Execute step
            if step.step_type == "delegate_task":
                task_type = step.payload.get("task_type", "availability_check")
                purpose = step.payload.get("purpose", "collaboration")
                payload = step.payload.get("payload", {})

                try:
                    res = await self._a2a.delegate_task(
                        owner_id,
                        recipient_agent_id=target_agent_id,
                        task_type=task_type,
                        purpose=purpose,
                        payload=payload,
                    )
                    step_results.append(res)
                except A2AError as exc:
                    if exc.code == A2AErrorCode.QUEUED:
                        details: dict[str, Any] = {
                            "task_type": task_type,
                            "target": target_name,
                        }
                        if exc.details.get("task_id"):
                            details["task_id"] = exc.details["task_id"]
                        return {
                            "status": "queued",
                            "message": f"{target_name}'s agent is currently offline. Your request has been queued on the Gateway.",
                            "details": details,
                        }
                    raise OrchestrationExecutionError(str(exc), user_message=self._friendly_a2a_error(exc, target_name)) from exc

            elif step.step_type == "send_message":
                action = step.payload.get("action", "notify")
                data_category = step.payload.get("data_category", "conversation")
                purpose = step.payload.get("purpose", "notification")
                payload = step.payload.get("payload", {})

                try:
                    res = await self._a2a.send_request(
                        owner_id,
                        recipient_agent_id=target_agent_id,
                        purpose=purpose,
                        action=action,
                        data_category=data_category,
                        payload=payload,
                    )
                    step_results.append(res)
                except A2AError as exc:
                    raise OrchestrationExecutionError(str(exc), user_message=self._friendly_a2a_error(exc, target_name)) from exc

        # 3. Format conversational result
        return self._format_result(plan, target_name, step_results)

    async def _authorize_step(
        self,
        owner_id: uuid.UUID,
        step: PlanStep,
        target_agent_id: str | None,
        target_name: str,
    ) -> dict[str, Any] | None:
        """Evaluate policy deterministically."""
        purpose = step.payload.get("purpose", "collaboration")
        data_cat = step.payload.get("data_category", "availability")
        action = step.payload.get("action", "disclose_information")

        # 1. Direct PolicyService evaluation
        eval_req = EvaluationRequest(
            requester_agent_id=target_agent_id or "nexus:anonymous",
            data_category=data_cat,
            action=action,
            purpose=purpose,
        )
        policy_res = await self._policy.evaluate(owner_id, eval_req)

        if policy_res.decision == PolicyDecision.DENY:
            logger.warning("Orchestration step denied by policy for %s: %s", target_agent_id, policy_res.reason)
            raise OrchestrationPolicyError(
                f"Policy denied interaction with {target_name}: {policy_res.reason}",
                user_message=f"I can't complete this action because your privacy permissions do not permit sharing {data_cat} with {target_name}'s agent.",
            )

        if policy_res.decision == PolicyDecision.ASK:
            return {
                "status": "waiting_approval",
                "message": f"This action requires your confirmation: {policy_res.reason}",
                "requires_approval": True,
                "approval_prompt": policy_res.reason,
                "approval_reason": policy_res.reason,
                "requested_action": action,
                "approval_target": target_name,
                "approval_category": data_cat,
                "approval_purpose": purpose,
                "approval_step": step.step_id,
            }

        # 2. Decision Engine evaluation if available
        if self._decision_engine is not None:
            dec_req = DecisionRequest(
                action_type=ActionType.CONTACT_AGENT.value,
                proposed_action=step.description,
                purpose=purpose,
                goal=step.description,
                target_agent_id=target_agent_id,
                required_data_categories=[data_cat],
            )
            outcome = await self._decision_engine.evaluate(owner_id, dec_req)
            if outcome.decision == DecisionResult.DENY:
                raise OrchestrationPolicyError(
                    f"DecisionEngine denied action: {outcome.reason}",
                    user_message=f"I cannot proceed: {outcome.reason}",
                )
            if outcome.decision in (DecisionResult.ASK, DecisionResult.STOP) or outcome.requires_approval:
                return {
                    "status": "waiting_approval",
                    "message": f"This action requires your confirmation: {outcome.reason}",
                    "requires_approval": True,
                    "approval_prompt": outcome.reason,
                    "approval_reason": outcome.reason,
                    "requested_action": action,
                    "approval_target": target_name,
                    "approval_category": data_cat,
                    "approval_purpose": purpose,
                    "approval_step": step.step_id,
                }

        return None

    def _format_result(
        self,
        plan: OrchestrationPlan,
        target_name: str,
        step_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Format raw execution results into a human-friendly response."""
        if not step_results:
            return {
                "status": "completed",
                "message": f"Contacted {target_name}'s agent successfully.",
                "details": {},
            }

        last_result = step_results[-1]
        payload = last_result.get("payload", {})
        status = last_result.get("status", "completed")

        # Check offline queue status
        if payload.get("status") == "queued" or last_result.get("status") == "queued":
            return {
                "status": "queued",
                "message": f"{target_name}'s agent is currently offline. I have left the request queued with the Gateway.",
                "details": last_result,
            }

        # Format availability check response
        if "available" in payload:
            is_avail = payload.get("available")
            alts = payload.get("alternative_times", [])
            req_time = ""
            for s in plan.steps:
                if "requested_time" in s.payload.get("payload", {}):
                    req_time = s.payload["payload"]["requested_time"]
                    break

            if is_avail:
                time_str = f" for {req_time}" if req_time else ""
                msg = f"{target_name} is free{time_str}."
            else:
                if alts:
                    alt_str = ", ".join(alts)
                    msg = f"{target_name} is not free at that time, but is available at: {alt_str}."
                else:
                    msg = f"{target_name} is not available at that time."

            return {
                "status": "completed",
                "message": msg,
                "available": is_avail,
                "alternatives": alts,
                "details": last_result,
            }

        # Format meeting proposal response
        if "confirmed_time" in payload or payload.get("status") == "accepted":
            time_str = payload.get("confirmed_time") or "the proposed time"
            return {
                "status": "completed",
                "message": f"The meeting with {target_name} is confirmed for {time_str}.",
                "details": last_result,
            }

        if payload.get("status") == "counter_proposal":
            alts = payload.get("alternative_times", [])
            alt_str = ", ".join(alts) if alts else "an alternative slot"
            return {
                "status": "completed",
                "message": f"{target_name} proposed {alt_str} instead. Would you like me to book that?",
                "alternatives": alts,
                "details": last_result,
            }

        # Default completion message
        return {
            "status": "completed",
            "message": f"Successfully completed request with {target_name}.",
            "details": last_result,
        }

    def _friendly_a2a_error(self, exc: A2AError, target_name: str) -> str:
        """Generate human-friendly error messages hiding raw codes."""
        if exc.code == A2AErrorCode.UNTRUSTED_SENDER:
            return f"You have not trusted {target_name}'s agent yet."
        elif exc.code == A2AErrorCode.REVOKED_SENDER:
            return f"Trust for {target_name}'s agent was previously revoked."
        elif exc.code == A2AErrorCode.NOT_FOUND:
            return f"I couldn't find a registered agent for {target_name}."
        elif exc.code == A2AErrorCode.TRANSPORT_ERROR:
            return f"Could not reach {target_name}'s agent over the network."
        elif exc.code == A2AErrorCode.EXPIRED:
            return f"The request to {target_name} expired before a response was received."
        return f"Unable to complete the action with {target_name}."
