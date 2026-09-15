"""Step handlers and execution registry for Part 9 Workflows.

Security invariants:
- Every step maps to a known, registered handler.
- Handlers receive a restricted WorkflowStepContext.
- Handlers enforce minimum disclosure (never leak complete calendars or raw memory dumps).
- Handlers declare data_category and action for independent policy authorization.
"""

from __future__ import annotations

import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.policy.engine import EvaluationRequest
from app.policy.models import DisclosureScope
from app.tools.schemas import ToolInvocation
from app.workflows.models import StepStatus

logger = logging.getLogger("nexus.workflows.handlers")


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9:_\-.]+", "_", text.lower()).strip("_")
    return cleaned[:64] if cleaned else "workflow"


@dataclass
class WorkflowStepContext:
    owner_id: uuid.UUID
    workflow_id: uuid.UUID
    step_id: uuid.UUID
    step_number: int
    purpose: str
    workflow_context: dict[str, Any]
    memory_manager: Any = None
    policy_service: Any = None
    tool_service: Any = None
    a2a_service: Any = None
    identity_service: Any = None


@dataclass
class StepResult:
    status: StepStatus
    output_payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    failure_reason: str | None = None
    is_transient: bool = False


class BaseWorkflowStepHandler(ABC):
    step_type: ClassVar[str]
    data_category: ClassVar[str] = "workflow"
    action: ClassVar[str] = "execute_step"

    def get_evaluation_request(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> EvaluationRequest:
        """Structured policy evaluation question for this step."""
        requester = input_payload.get("requester_agent_id") or "nexus:self"
        purpose = input_payload.get("purpose") or context.purpose
        return EvaluationRequest(
            requester_agent_id=requester,
            data_category=self.data_category,
            action=self.action,
            purpose=_slugify(purpose),
        )

    @abstractmethod
    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        """Execute the step deterministically."""


class AvailabilityStepHandler(BaseWorkflowStepHandler):
    """Checks owner's availability under strict minimum disclosure."""

    step_type = "availability_check"
    data_category = "calendar"
    action = "read"

    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        candidate_slots = input_payload.get("candidate_slots")
        if candidate_slots is None:
            candidate_slots = ["09:00", "10:00", "14:00", "16:00", "19:00"]

        busy_slots: set[str] = set()
        date_str = input_payload.get("date", "tomorrow")

        # If explicit busy slots provided in input, use them
        if "busy_slots" in input_payload:
            busy_slots.update(input_payload["busy_slots"])

        # Check memories if available for busy/conflict notes
        if context.memory_manager:
            try:
                results = await context.memory_manager.search_memories(
                    context.owner_id,
                    f"calendar schedule busy {date_str}",
                    limit=3,
                )
                for r in results:
                    content_lower = r.memory.content.lower()
                    for slot in candidate_slots:
                        if slot.lower() in content_lower and any(
                            w in content_lower
                            for w in ("busy", "conflict", "meeting", "occupied", "booked")
                        ):
                            busy_slots.add(slot)
            except Exception as exc:
                logger.warning("Failed searching memories for availability: %s", exc)

        available_slots = [s for s in candidate_slots if s not in busy_slots]

        # Minimum disclosure output: only boolean & candidate slots, NEVER full calendar
        output = {
            "date": date_str,
            "available": len(available_slots) > 0,
            "available_slots": available_slots,
        }
        return StepResult(status=StepStatus.COMPLETED, output_payload=output)


class CandidateSelectionHandler(BaseWorkflowStepHandler):
    """Computes mutual candidate times without exposing raw schedules."""

    step_type = "candidate_selection"
    data_category = "scheduling"
    action = "compute"

    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        # Determine local slots: from input_payload, or from earlier step in workflow_context
        user_slots = input_payload.get("user_slots")
        if user_slots is None:
            user_avail = context.workflow_context.get("user_availability", {})
            user_slots = user_avail.get("available_slots", [])

        # Determine remote slots: from input_payload, or from earlier step in workflow_context
        remote_slots = input_payload.get("remote_slots")
        if remote_slots is None:
            remote_avail = context.workflow_context.get("remote_availability", {})
            if isinstance(remote_avail, dict):
                # May be in response payload or available_slots or alternative_times
                remote_slots = remote_avail.get(
                    "available_slots",
                    remote_avail.get("alternative_times", []),
                )
            else:
                remote_slots = []

        # Find mutual slots
        mutual = [s for s in user_slots if s in remote_slots]
        selected = mutual[0] if mutual else None

        output = {
            "mutual_slots": mutual,
            "selected_slot": selected,
            "has_mutual_slot": bool(mutual),
        }
        return StepResult(status=StepStatus.COMPLETED, output_payload=output)


class A2ATaskStepHandler(BaseWorkflowStepHandler):
    """Delegates a task to a remote agent using Part 8 A2AService."""

    step_type = "a2a_task"
    data_category = "a2a"
    action = "delegate_task"

    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        if not context.a2a_service:
            return StepResult(
                status=StepStatus.FAILED,
                failure_reason="A2AService not available in workflow context",
                is_transient=False,
            )

        recipient_agent_id = input_payload.get("recipient_agent_id")
        task_type = input_payload.get("task_type", "availability_check")
        purpose = input_payload.get("purpose") or context.purpose
        payload = dict(input_payload.get("payload") or {})
        endpoint = input_payload.get("endpoint")

        # Dynamic parameter resolution from workflow context
        # e.g. if payload has {"use_selected_slot": True} or requested_time is missing
        if input_payload.get("use_selected_slot") or payload.get("use_selected_slot"):
            selected = context.workflow_context.get("selected_candidate", {}).get(
                "selected_slot"
            ) or context.workflow_context.get("selected_slot")
            if selected:
                payload["requested_time"] = selected
                payload["time"] = selected
                payload["proposed_time"] = selected

        if task_type == "meeting_proposal" and "proposed_time" not in payload:
            time_val = payload.get("time") or payload.get("requested_time") or context.workflow_context.get("candidate_selection", {}).get("selected_slot")
            if time_val:
                payload["proposed_time"] = time_val

        try:
            result = await context.a2a_service.delegate_task(
                context.owner_id,
                recipient_agent_id=recipient_agent_id,
                task_type=task_type,
                purpose=purpose,
                payload=payload,
                endpoint=endpoint,
            )
        except Exception as exc:
            # Check if this error is transient (e.g. network/timeout vs policy deny/revocation)
            err_msg = str(exc)
            is_non_transient = any(
                term in err_msg.lower()
                for term in (
                    "policy",
                    "denied",
                    "revoked",
                    "unauthorized",
                    "not_found",
                    "invalid_envelope",
                    "signature",
                )
            )
            logger.error("A2A delegation failed: %s", exc)
            return StepResult(
                status=StepStatus.FAILED,
                failure_reason=err_msg,
                is_transient=not is_non_transient,
            )

        task_id = result.get("task_id")
        resp_status = result.get("status")
        resp_payload = result.get("payload") or {}

        if resp_status == "completed":
            return StepResult(
                status=StepStatus.COMPLETED,
                output_payload=resp_payload,
                task_id=task_id,
            )
        elif resp_status in {"pending", "pending_approval"}:
            return StepResult(
                status=StepStatus.WAITING,
                output_payload=resp_payload,
                task_id=task_id,
            )
        else:
            return StepResult(
                status=StepStatus.FAILED,
                output_payload=resp_payload,
                task_id=task_id,
                failure_reason=f"Task returned status: {resp_status}",
                is_transient=False,
            )


class ToolStepHandler(BaseWorkflowStepHandler):
    """Executes a local tool through ToolService with policy enforcement."""

    step_type = "tool_execution"
    data_category = "tools"
    action = "execute"

    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        if not context.tool_service:
            return StepResult(
                status=StepStatus.FAILED,
                failure_reason="ToolService not available in workflow context",
                is_transient=False,
            )

        tool_name = input_payload.get("tool_name")
        arguments = input_payload.get("arguments", {})
        purpose = input_payload.get("purpose") or context.purpose

        invocation = ToolInvocation(
            tool_name=tool_name,
            arguments=arguments,
            purpose=purpose,
        )

        try:
            res = await context.tool_service.execute(context.owner_id, invocation)
        except Exception as exc:
            return StepResult(
                status=StepStatus.FAILED,
                failure_reason=str(exc),
                is_transient=True,
            )

        if not res.success:
            return StepResult(
                status=StepStatus.FAILED,
                failure_reason=res.error.message if res.error else res.status,
                output_payload={"status": res.status},
                is_transient=False,
            )

        return StepResult(
            status=StepStatus.COMPLETED,
            output_payload=res.data if isinstance(res.data, dict) else {"data": res.data},
        )


class WorkflowStepHandlerRegistry:
    """Registry of known step handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, BaseWorkflowStepHandler] = {}

    def register(self, handler: BaseWorkflowStepHandler) -> None:
        self._handlers[handler.step_type] = handler

    def get(self, step_type: str) -> BaseWorkflowStepHandler | None:
        return self._handlers.get(step_type)


def build_default_step_registry() -> WorkflowStepHandlerRegistry:
    registry = WorkflowStepHandlerRegistry()
    registry.register(AvailabilityStepHandler())
    registry.register(CandidateSelectionHandler())
    registry.register(A2ATaskStepHandler())
    registry.register(ToolStepHandler())
    return registry


__all__ = [
    "A2ATaskStepHandler",
    "AvailabilityStepHandler",
    "BaseWorkflowStepHandler",
    "CandidateSelectionHandler",
    "StepResult",
    "ToolStepHandler",
    "WorkflowStepContext",
    "WorkflowStepHandlerRegistry",
    "build_default_step_registry",
]
