"""Bounded action planner for Nexus Autonomy (Part 10).

The planner produces an ordered list of explicit allowlisted actions for a goal.
The planner does NOT execute actions; every action must pass through the
Decision Engine before execution.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.autonomy.errors import AutonomyInvalidActionError
from app.autonomy.models import ActionType

logger = logging.getLogger("nexus.autonomy.planner")

MAX_PLAN_STEPS = 10


@dataclass
class PlanAction:
    step_number: int
    action_type: str
    purpose: str
    proposed_action: str
    target_agent_id: str | None = None
    tool_name: str | None = None
    required_data_categories: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            "action_type": self.action_type,
            "purpose": self.purpose,
            "proposed_action": self.proposed_action,
            "target_agent_id": self.target_agent_id,
            "tool_name": self.tool_name,
            "required_data_categories": self.required_data_categories,
            "payload": self.payload,
        }


@dataclass
class ActionPlan:
    goal: str
    actions: list[PlanAction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "actions": [a.to_dict() for a in self.actions],
        }


class ActionPlanner:
    """Bounded, deterministic-capable action planner."""

    def __init__(self, max_steps: int = MAX_PLAN_STEPS) -> None:
        self._max_steps = max_steps

    def create_plan(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
        custom_actions: Sequence[dict[str, Any]] | None = None,
    ) -> ActionPlan:
        """Construct a bounded action plan for the given goal."""
        ctx = context or {}

        # If custom actions were proposed (e.g. from LLM or caller), validate them
        if custom_actions:
            actions: list[PlanAction] = []
            for i, raw in enumerate(custom_actions[: self._max_steps], start=1):
                act_type = str(raw.get("action_type", "")).lower()
                valid_types = [a.value for a in ActionType]
                if act_type not in valid_types:
                    raise AutonomyInvalidActionError(
                        f"Action '{act_type}' is forbidden. Must be one of: {valid_types}"
                    )
                actions.append(
                    PlanAction(
                        step_number=i,
                        action_type=act_type,
                        purpose=raw.get("purpose", "general_task"),
                        proposed_action=raw.get("proposed_action", f"Step {i}"),
                        target_agent_id=raw.get("target_agent_id"),
                        tool_name=raw.get("tool_name"),
                        required_data_categories=list(raw.get("required_data_categories", [])),
                        payload=dict(raw.get("payload", {})),
                    )
                )
            return ActionPlan(goal=goal, actions=actions)

        # Deterministic goal templates
        goal_lower = goal.lower()
        target_agent = ctx.get("target_agent_id") or ctx.get("peer_agent_id")

        # 1. Meeting coordination pattern
        if any(w in goal_lower for w in ["meeting", "dinner", "coffee", "lunch", "schedule", "coordinate"]):
            actions = [
                PlanAction(
                    step_number=1,
                    action_type=ActionType.READ_MEMORY.value,
                    purpose="read_schedule_preferences",
                    proposed_action="Check owner's calendar and scheduling preferences",
                    required_data_categories=["preferences"],
                    payload={"query": "meeting schedule preferences"},
                ),
                PlanAction(
                    step_number=2,
                    action_type=ActionType.CONTACT_AGENT.value if target_agent else ActionType.READ_CONTEXT.value,
                    purpose="check_peer_availability",
                    proposed_action=f"Query availability from peer {target_agent or 'specified agent'}",
                    target_agent_id=target_agent,
                    required_data_categories=["availability"],
                    payload={"task_type": "availability_query", "goal": goal},
                ),
                PlanAction(
                    step_number=3,
                    action_type=ActionType.CREATE_WORKFLOW.value,
                    purpose="coordinate_meeting",
                    proposed_action="Initiate multi-step meeting proposal workflow",
                    target_agent_id=target_agent,
                    required_data_categories=["availability"],
                    payload={"workflow_type": "meeting_coordination"},
                ),
                PlanAction(
                    step_number=4,
                    action_type=ActionType.REQUEST_APPROVAL.value,
                    purpose="confirm_meeting",
                    proposed_action="Request owner confirmation for final meeting time",
                    payload={"goal": goal},
                ),
            ]
            return ActionPlan(goal=goal, actions=actions[: self._max_steps])

        # 2. Tool computation pattern
        if any(w in goal_lower for w in ["calculate", "compute", "math", "calculator"]):
            tool_name = "calculator"
            actions = [
                PlanAction(
                    step_number=1,
                    action_type=ActionType.EXECUTE_TOOL.value,
                    purpose="perform_calculation",
                    proposed_action=f"Execute tool '{tool_name}'",
                    tool_name=tool_name,
                    payload={"expression": ctx.get("expression", "2 + 2")},
                ),
                PlanAction(
                    step_number=2,
                    action_type=ActionType.UPDATE_MEMORY.value,
                    purpose="record_result",
                    proposed_action="Save computation result to persistent memory",
                    payload={"content": f"Computation for: {goal}"},
                ),
            ]
            return ActionPlan(goal=goal, actions=actions[: self._max_steps])

        # 3. Information retrieval pattern
        if any(w in goal_lower for w in ["recall", "search", "find", "what is", "remember"]):
            actions = [
                PlanAction(
                    step_number=1,
                    action_type=ActionType.READ_MEMORY.value,
                    purpose="retrieve_facts",
                    proposed_action="Search long-term semantic memory for relevant facts",
                    required_data_categories=["general"],
                    payload={"query": goal},
                ),
                PlanAction(
                    step_number=2,
                    action_type=ActionType.READ_CONTEXT.value,
                    purpose="analyze_context",
                    proposed_action="Analyze recalled facts against request",
                    payload={"goal": goal},
                ),
            ]
            return ActionPlan(goal=goal, actions=actions[: self._max_steps])

        # 4. Default bounded plan
        actions = [
            PlanAction(
                step_number=1,
                action_type=ActionType.READ_CONTEXT.value,
                purpose="evaluate_goal",
                proposed_action="Assess goal requirements and constraints",
                payload={"goal": goal},
            ),
            PlanAction(
                step_number=2,
                action_type=ActionType.REQUEST_APPROVAL.value,
                purpose="seek_owner_guidance",
                proposed_action="Request owner approval to proceed with task",
                payload={"goal": goal},
            ),
        ]
        return ActionPlan(goal=goal, actions=actions[: self._max_steps])


__all__ = [
    "ActionPlan",
    "ActionPlanner",
    "MAX_PLAN_STEPS",
    "PlanAction",
]
