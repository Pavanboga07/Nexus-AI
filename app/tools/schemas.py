"""Domain schemas for the tool subsystem (not HTTP models).

ToolInvocation is what a caller (today: the API; later: the LLM/agent)
submits. ToolResult is the structured outcome. ToolContext is the ONLY
information a tool implementation is allowed to see - no database sessions,
no secrets, no conversation history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Arguments model base for tools: rejects unknown fields
    (additionalProperties: false in the derived JSON schema)."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolInvocation:
    """A request to execute a tool.

    ``purpose`` is mandatory: it flows into the Part 4 policy evaluation, so
    every tool execution is purpose-bound. ``request_id`` is optional; one is
    generated when absent.
    """

    tool_name: str
    arguments: dict[str, Any]
    purpose: str
    request_id: str | None = None


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool is allowed to know about the invocation."""

    owner_id: uuid.UUID
    request_id: str
    purpose: str


@dataclass(frozen=True)
class ToolErrorInfo:
    code: str
    message: str


@dataclass(frozen=True)
class ToolResult:
    success: bool
    status: str  # executed | approval_required | denied | failed
    tool_name: str
    request_id: str
    data: dict[str, Any] | None = None
    error: ToolErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "tool_name": self.tool_name,
            "request_id": self.request_id,
            "data": self.data,
            "error": (
                {"code": self.error.code, "message": self.error.message}
                if self.error
                else None
            ),
        }


__all__ = ["ToolContext", "ToolErrorInfo", "ToolInvocation", "ToolResult"]
