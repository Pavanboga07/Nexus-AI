"""Pydantic schemas for Part 9 Workflows."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowStepSpec(BaseModel):
    step_type: str = Field(..., min_length=1, max_length=64)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)


class WorkflowCreateRequest(BaseModel):
    workflow_type: str = Field(..., min_length=1, max_length=64)
    purpose: str = Field(..., min_length=1, max_length=255)
    steps: list[WorkflowStepSpec] = Field(..., min_length=1, max_length=50)
    context_data: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=3600, gt=0, le=86400 * 30)


class WorkflowStepOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_id: str
    workflow_id: str
    step_number: int
    step_type: str
    status: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_payload: dict[str, Any] | None = None
    task_id: str | None = None
    attempt_count: int
    max_attempts: int
    failure_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class WorkflowOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workflow_id: str
    owner_id: str
    workflow_type: str
    purpose: str
    status: str
    current_step_number: int
    context_data: dict[str, Any] = Field(default_factory=dict)
    workflow_metadata: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    expires_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    steps: list[WorkflowStepOut] = Field(default_factory=list)


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowOut]
    total: int


class WorkflowActionResponse(BaseModel):
    workflow_id: str
    status: str
    detail: str | None = None


__all__ = [
    "WorkflowActionResponse",
    "WorkflowCreateRequest",
    "WorkflowListResponse",
    "WorkflowOut",
    "WorkflowStepOut",
    "WorkflowStepSpec",
]
