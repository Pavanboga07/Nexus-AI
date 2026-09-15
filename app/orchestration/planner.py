"""Orchestration Planner: Generates bounded, minimal-disclosure plans.

Translates high-level user intents into step-by-step execution plans
using only allowlisted task types and minimal data disclosure.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.orchestration.schemas import (
    Intent,
    IntentType,
    OrchestrationPlan,
    PlanStep,
    TargetResolution,
)

logger = logging.getLogger("nexus.orchestration.planner")

ALLOWLISTED_TASK_TYPES = frozenset({
    "availability_check",
    "meeting_proposal",
    "information_request",
    "send_message",
    "notification",
})


class OrchestrationPlanner:
    """Generates execution plans from structured user intents."""

    def build_plan(
        self,
        intent: Intent,
        target_resolution: TargetResolution,
        secondary_resolutions: list[TargetResolution] | None = None,
    ) -> OrchestrationPlan:
        """Create a bounded OrchestrationPlan for the resolved target(s)."""
        steps: list[PlanStep] = []
        target_agent_id = target_resolution.agent_id
        target_name = target_resolution.display_name or target_resolution.target_name

        # 1. Step: Target validation
        steps.append(
            PlanStep(
                step_id=f"step_{uuid.uuid4().hex[:8]}",
                step_type="resolve_target",
                description=f"Verify target agent identity for {target_name}",
                target_agent_id=target_agent_id,
            )
        )

        # 2. Steps depending on IntentType
        if intent.intent_type == IntentType.CHECK_AVAILABILITY:
            # Minimum disclosure: extract only the requested time parameter
            requested_time = (
                intent.constraints.get("time")
                or intent.constraints.get("date")
                or "tomorrow evening"
            )
            date_str = intent.constraints.get("date", "")
            time_str = intent.constraints.get("time", "")
            slot_spec = f"{date_str} {time_str}".strip() or requested_time

            steps.append(
                PlanStep(
                    step_id=f"step_{uuid.uuid4().hex[:8]}",
                    step_type="delegate_task",
                    description=f"Ask {target_name}'s agent for availability at {slot_spec}",
                    target_agent_id=target_agent_id,
                    payload={
                        "task_type": "availability_check",
                        "purpose": intent.purpose,
                        "payload": {"requested_time": slot_spec},
                    },
                )
            )

        elif intent.intent_type == IntentType.COORDINATE_MEETING:
            # Check primary target
            slot_spec = intent.constraints.get("time") or intent.constraints.get("date") or "mutually available time"
            steps.append(
                PlanStep(
                    step_id=f"step_{uuid.uuid4().hex[:8]}",
                    step_type="delegate_task",
                    description=f"Query {target_name}'s availability for meeting",
                    target_agent_id=target_agent_id,
                    payload={
                        "task_type": "availability_check",
                        "purpose": intent.purpose,
                        "payload": {"requested_time": slot_spec},
                    },
                )
            )

            # Check secondary targets if multi-agent
            for sec in (secondary_resolutions or []):
                sec_name = sec.display_name or sec.target_name
                steps.append(
                    PlanStep(
                        step_id=f"step_{uuid.uuid4().hex[:8]}",
                        step_type="delegate_task",
                        description=f"Query {sec_name}'s availability for meeting",
                        target_agent_id=sec.agent_id,
                        payload={
                            "task_type": "availability_check",
                            "purpose": intent.purpose,
                            "payload": {"requested_time": slot_spec},
                        },
                    )
                )

        elif intent.intent_type in {IntentType.NEGOTIATE, IntentType.CONFIRM_ACTION}:
            # Meeting proposal / counter proposal
            proposed_time = (
                intent.constraints.get("proposed_time")
                or intent.constraints.get("time")
                or "agreed time"
            )
            is_confirm = intent.intent_type == IntentType.CONFIRM_ACTION
            action_desc = f"Confirm meeting with {target_name} at {proposed_time}" if is_confirm else f"Propose alternative time ({proposed_time}) to {target_name}"

            steps.append(
                PlanStep(
                    step_id=f"step_{uuid.uuid4().hex[:8]}",
                    step_type="delegate_task",
                    description=action_desc,
                    target_agent_id=target_agent_id,
                    payload={
                        "task_type": "meeting_proposal",
                        "purpose": intent.purpose,
                        "payload": {
                            "proposed_time": proposed_time,
                            "status": "accepted" if is_confirm else "counter_proposal",
                        },
                    },
                    requires_approval=False,
                )
            )

        elif intent.intent_type == IntentType.SEND_INFORMATION:
            message_text = intent.action_payload.get("message", intent.goal)
            steps.append(
                PlanStep(
                    step_id=f"step_{uuid.uuid4().hex[:8]}",
                    step_type="send_message",
                    description=f"Send message to {target_name}",
                    target_agent_id=target_agent_id,
                    payload={
                        "purpose": intent.purpose,
                        "action": "notify",
                        "data_category": "conversation",
                        "payload": {"message": message_text},
                    },
                )
            )

        elif intent.intent_type == IntentType.DELEGATE_TASK:
            task_desc = intent.action_payload.get("task_description", intent.goal)
            steps.append(
                PlanStep(
                    step_id=f"step_{uuid.uuid4().hex[:8]}",
                    step_type="delegate_task",
                    description=f"Delegate task to {target_name}: {task_desc}",
                    target_agent_id=target_agent_id,
                    payload={
                        "task_type": "information_request",
                        "purpose": intent.purpose,
                        "payload": {"query": task_desc, "data_category": "work"},
                    },
                )
            )

        else:
            # Generic contact or query
            steps.append(
                PlanStep(
                    step_id=f"step_{uuid.uuid4().hex[:8]}",
                    step_type="send_message",
                    description=f"Reach out to {target_name}",
                    target_agent_id=target_agent_id,
                    payload={
                        "purpose": intent.purpose,
                        "action": "inquiry",
                        "data_category": "conversation",
                    },
                )
            )

        return OrchestrationPlan(
            goal=intent.goal,
            intent_type=intent.intent_type.value,
            target_person=target_name,
            target_agent_id=target_agent_id,
            steps=steps,
        )
