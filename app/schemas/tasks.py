"""Pydantic v2 schemas for the Task Delegation & Negotiation API (Part 8)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.a2a.schemas import AGENT_ID_PATTERN, PURPOSE_PATTERN, _reject_floats


class TaskDelegateRequest(BaseModel):
    """Request to delegate a task to a remote trusted agent."""

    recipient_agent_id: str
    task_type: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    endpoint: str | None = None

    @field_validator("recipient_agent_id")
    @classmethod
    def _recipient(cls, value: str) -> str:
        if not AGENT_ID_PATTERN.fullmatch(value):
            raise ValueError("recipient_agent_id must be a valid nexus agent id")
        return value

    @field_validator("task_type", "purpose")
    @classmethod
    def _slugs(cls, value: str, info) -> str:
        if not PURPOSE_PATTERN.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a lowercase slug")
        return value

    @field_validator("payload")
    @classmethod
    def _payload_no_floats(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_floats(value)
        return value


class TaskNegotiateRequest(BaseModel):
    """Submit a counter-proposal / next negotiation round for an active task."""

    proposal_payload: dict[str, Any] = Field(default_factory=dict)
    purpose: str | None = None

    @field_validator("proposal_payload")
    @classmethod
    def _payload_no_floats(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_floats(value)
        return value

    @field_validator("purpose")
    @classmethod
    def _purpose_slug(cls, value: str | None) -> str | None:
        if value is not None and not PURPOSE_PATTERN.fullmatch(value):
            raise ValueError("purpose must be a lowercase slug")
        return value


class TaskApproveRequest(BaseModel):
    """Optional payload when manually approving a pending task."""

    notes: str | None = Field(default=None, max_length=255)


class TaskRejectRequest(BaseModel):
    """Reason for rejecting a pending task."""

    reason: str | None = Field(default="Rejected by owner", max_length=255)


class TaskOut(BaseModel):
    """Public representation of an A2A task."""

    task_id: str
    sender_agent_id: str
    recipient_agent_id: str
    status: str
    task_type: str | None = None
    purpose: str | None = None
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None
    negotiation_round: int = 0
    failure_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    expires_at: str | None = None
    completed_at: str | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskOut]
    total: int


class TaskActionResponse(BaseModel):
    task_id: str
    status: str
    detail: str | None = None
    response_payload: dict[str, Any] | None = None


__all__ = [
    "TaskActionResponse",
    "TaskApproveRequest",
    "TaskDelegateRequest",
    "TaskListResponse",
    "TaskNegotiateRequest",
    "TaskOut",
    "TaskRejectRequest",
]
