"""Task Handlers and execution context (Part 8).

Task handlers define how an agent executes a delegated task on behalf of its
owner. Handlers are strictly bounded:

- They receive a restricted ``TaskContext``, NEVER raw database connections,
  private keys, filesystem, or unrestricted tools.
- They MUST construct purpose-specific, minimum-disclosure responses.
  They NEVER leak raw database rows, raw memory dumps, or sensitive notes.
- Remote agents NEVER invoke handlers directly: requests arrive as signed
  A2A envelopes, pass through the Part 6 security pipeline, and are evaluated
  by PolicyService BEFORE handler execution.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.a2a.disclosure import build_disclosure
from app.policy.models import DisclosureScope

logger = logging.getLogger("nexus.a2a.handlers")


@dataclass(frozen=True)
class TaskContext:
    """Restricted execution context provided to a task handler.

    No private keys, no raw database connections, no unrestricted memory,
    no filesystem, no shell access.
    """

    owner_id: Any
    requester_agent_id: str
    task_id: str
    purpose: str
    disclosure_scope: DisclosureScope
    _memory_manager: Any = field(default=None, repr=False)
    _tool_service: Any = field(default=None, repr=False)

    async def get_relevant_memories(
        self, query: str, limit: int = 3
    ) -> list[str]:
        """Bounded memory query. Returns at most ``limit`` memory strings."""
        if self._memory_manager is None:
            return []
        try:
            records = await self._memory_manager.get_relevant_memories(
                self.owner_id, query, limit=limit
            )
            return [m.content for m in records]
        except Exception as exc:
            logger.warning("task_context_memory_failed detail=%s", exc)
            return []

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a local tool under the owner's policy. Never bypasses policy."""
        if self._tool_service is None:
            raise RuntimeError("Tool service is not configured.")
        result = await self._tool_service.execute_tool(
            self.owner_id,
            tool_name=tool_name,
            arguments=arguments,
            purpose=self.purpose,
        )
        if not result.get("success"):
            raise RuntimeError(
                result.get("error") or "Tool execution failed policy check."
            )
        return result.get("data") or {}


class BaseTaskHandler(ABC):
    """Contract for a task handler."""

    task_type: ClassVar[str]
    default_data_category: ClassVar[str] = "custom"
    default_action: ClassVar[str] = "disclose_information"

    @abstractmethod
    def validate(self, payload: dict[str, Any]) -> None:
        """Validate the request payload. Raise ValueError on failure."""
        raise NotImplementedError

    @abstractmethod
    async def execute(
        self, context: TaskContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute the task under the restricted context. Return response payload."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Concrete Handlers                                                          #
# --------------------------------------------------------------------------- #


class AvailabilityCheckHandler(BaseTaskHandler):
    """Checks owner availability for a requested time slot.

    Constructs minimum disclosure responses:
        {"available": true}
        or
        {"available": false, "alternative_times": ["18:30", "19:00"]}

    Never returns raw calendar event details or private notes.
    """

    task_type: ClassVar[str] = "availability_check"
    default_data_category: ClassVar[str] = "availability"
    default_action: ClassVar[str] = "disclose_information"

    def validate(self, payload: dict[str, Any]) -> None:
        if "requested_time" not in payload:
            raise ValueError("availability_check requires 'requested_time'")
        if not isinstance(payload["requested_time"], str) or not payload["requested_time"].strip():
            raise ValueError("'requested_time' must be a non-empty string")

    async def execute(
        self, context: TaskContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        requested_time = payload["requested_time"].lower()
        memories = await context.get_relevant_memories(
            f"availability calendar schedule meeting {requested_time}"
        )

        is_available = True
        alternatives: list[str] = []

        # Analyze relevant memories for availability cues
        combined = " ".join(memories).lower()
        is_evening = any(t in requested_time for t in ["18:00", "6 pm", "19:00", "7 pm", "20:00", "8 pm"])
        is_afternoon = any(t in requested_time for t in ["15:00", "3 pm", "12:00", "noon", "14:00", "2 pm"])

        if "after 6 pm" in combined or "after 18:00" in combined:
            if is_evening:
                is_available = True
            else:
                is_available = False
                alternatives = ["18:00", "19:00"]
        elif any(w in combined for w in ["busy", "unavailable", "conflict"]):
            is_available = False
            alternatives = ["18:30", "19:00"]
        elif "doctor" in combined or "appointment" in combined:
            if is_afternoon or "3 pm" in requested_time or "15:00" in requested_time:
                is_available = False
                alternatives = ["18:00", "19:00"]
            else:
                is_available = True

        response: dict[str, Any] = {"available": is_available}
        if not is_available and alternatives:
            response["alternative_times"] = alternatives
        return response


class MeetingProposalHandler(BaseTaskHandler):
    """Proposes a meeting time and topic.

    Returns:
        {"status": "accepted", "confirmed_time": "..."}
        or
        {"status": "counter_proposal", "alternative_times": ["..."]}
        or
        {"status": "rejected"}
    """

    task_type: ClassVar[str] = "meeting_proposal"
    default_data_category: ClassVar[str] = "availability"
    default_action: ClassVar[str] = "disclose_information"

    def validate(self, payload: dict[str, Any]) -> None:
        if "proposed_time" not in payload:
            raise ValueError("meeting_proposal requires 'proposed_time'")
        if not isinstance(payload["proposed_time"], str) or not payload["proposed_time"].strip():
            raise ValueError("'proposed_time' must be a non-empty string")

    async def execute(
        self, context: TaskContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        proposed_time = payload["proposed_time"].lower()
        memories = await context.get_relevant_memories(
            f"availability calendar meeting {proposed_time}"
        )

        combined = " ".join(memories).lower()
        is_evening = any(t in proposed_time for t in ["18:00", "6 pm", "19:00", "7 pm", "20:00", "8 pm"])

        if "after 6 pm" in combined or "after 18:00" in combined:
            if not is_evening:
                return {
                    "status": "counter_proposal",
                    "message": "Proposed time unavailable.",
                    "alternative_times": ["18:00", "19:00"],
                }
        elif any(w in combined for w in ["busy", "unavailable", "conflict", "doctor", "appointment"]):
            return {
                "status": "counter_proposal",
                "message": "Proposed time unavailable.",
                "alternative_times": ["19:00", "20:00"],
            }

        return {
            "status": "accepted",
            "confirmed_time": payload["proposed_time"],
        }


class InformationRequestHandler(BaseTaskHandler):
    """Generic category-scoped information request bounded by disclosure scope."""

    task_type: ClassVar[str] = "information_request"
    default_data_category: ClassVar[str] = "custom"
    default_action: ClassVar[str] = "disclose_information"

    def validate(self, payload: dict[str, Any]) -> None:
        if "query" not in payload:
            raise ValueError("information_request requires 'query'")
        if not isinstance(payload["query"], str) or not payload["query"].strip():
            raise ValueError("'query' must be a non-empty string")

    async def execute(
        self, context: TaskContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        query = payload["query"]
        data_category = payload.get("data_category") or self.default_data_category
        memories = await context.get_relevant_memories(query)

        disclosure = build_disclosure(
            scope=context.disclosure_scope,
            data_category=data_category,
            memories=memories,
        )
        return disclosure.to_payload()


# --------------------------------------------------------------------------- #
#  Registry                                                                   #
# --------------------------------------------------------------------------- #


class TaskHandlerRegistry:
    """Application-scoped registry of supported task handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, BaseTaskHandler] = {}

    def register(self, handler: BaseTaskHandler) -> None:
        self._handlers[handler.task_type] = handler

    def get(self, task_type: str) -> BaseTaskHandler | None:
        return self._handlers.get(task_type)

    def list_task_types(self) -> list[str]:
        return sorted(self._handlers.keys())


def create_default_task_registry() -> TaskHandlerRegistry:
    registry = TaskHandlerRegistry()
    registry.register(AvailabilityCheckHandler())
    registry.register(MeetingProposalHandler())
    registry.register(InformationRequestHandler())
    return registry


__all__ = [
    "AvailabilityCheckHandler",
    "BaseTaskHandler",
    "InformationRequestHandler",
    "MeetingProposalHandler",
    "TaskContext",
    "TaskHandlerRegistry",
    "create_default_task_registry",
]
