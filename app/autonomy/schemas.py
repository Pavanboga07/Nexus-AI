"""Pydantic v2 schemas for the Nexus Autonomy & Decision Engine API (Part 10)."""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.autonomy.models import (
    ActionType,
    ApprovalStatus,
    AutonomyMode,
    DecisionResult,
    RiskLevel,
    RunStatus,
    TriggerType,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AutonomyConfigOut(BaseModel):
    id: str
    owner_id: str
    mode: str
    enabled: bool
    max_steps_per_run: int
    max_runtime_seconds: int
    max_remote_tasks: int
    max_tool_calls: int
    require_approval_for_unknown_actions: bool
    require_approval_for_external_communication: bool
    require_approval_for_sensitive_data: bool
    created_at: str | None = None
    updated_at: str | None = None


class AutonomyConfigUpdate(BaseModel):
    mode: AutonomyMode | None = None
    enabled: bool | None = None
    max_steps_per_run: int | None = Field(default=None, ge=1, le=50)
    max_runtime_seconds: int | None = Field(default=None, ge=10, le=86400)
    max_remote_tasks: int | None = Field(default=None, ge=0, le=20)
    max_tool_calls: int | None = Field(default=None, ge=0, le=50)
    require_approval_for_unknown_actions: bool | None = None
    require_approval_for_external_communication: bool | None = None
    require_approval_for_sensitive_data: bool | None = None


class PlanActionSpec(BaseModel):
    step_number: int = Field(ge=1)
    action_type: str
    purpose: str = Field(min_length=1, max_length=64)
    proposed_action: str = Field(min_length=1, max_length=128)
    target_agent_id: str | None = None
    tool_name: str | None = None
    required_data_categories: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action_type")
    @classmethod
    def _validate_action_type(cls, value: str) -> str:
        val = value.lower()
        valid = [a.value for a in ActionType]
        if val not in valid:
            raise ValueError(f"action_type '{value}' is not allowed. Allowed: {valid}")
        return val

    @field_validator("purpose")
    @classmethod
    def _validate_purpose(cls, value: str) -> str:
        val = value.lower().replace(" ", "_")
        if not SLUG_PATTERN.match(val):
            raise ValueError(f"purpose '{value}' must be an alphanumeric slug")
        return val


class ActionPlanSpec(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    actions: list[PlanActionSpec] = Field(default_factory=list, max_length=20)


class AutonomyRunCreateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    context_data: dict[str, Any] = Field(default_factory=dict)
    plan: list[PlanActionSpec] | None = None
    execute_immediately: bool = True


class AutonomyDecisionOut(BaseModel):
    decision_id: str
    owner_id: str
    run_id: str | None = None
    trigger: str
    goal: str
    proposed_action: str
    action_type: str
    purpose: str
    risk_level: str
    required_capability: str | None = None
    required_data_categories: list[str] = Field(default_factory=list)
    decision: str
    reason: str
    policy_decision: str | None = None
    consent_decision: str | None = None
    created_at: str | None = None


class AutonomyDecisionListResponse(BaseModel):
    decisions: list[AutonomyDecisionOut]
    total: int


class AutonomyApprovalOut(BaseModel):
    approval_id: str
    run_id: str
    decision_id: str | None = None
    owner_id: str
    status: str
    requested_action: str
    purpose: str
    risk_level: str
    required_data: dict[str, Any] = Field(default_factory=dict)
    recipient: str | None = None
    notes: str | None = None
    created_at: str | None = None
    resolved_at: str | None = None


class AutonomyApprovalListResponse(BaseModel):
    approvals: list[AutonomyApprovalOut]
    total: int


class AutonomyApprovalDecisionRequest(BaseModel):
    approved: bool
    notes: str | None = None


class AutonomyRunOut(BaseModel):
    id: str
    owner_id: str
    workflow_id: str | None = None
    goal: str
    status: str
    current_step: int
    steps_executed: int
    tool_calls: int
    remote_tasks: int
    plan: list[dict[str, Any]] = Field(default_factory=list)
    context_data: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    stop_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    decisions: list[AutonomyDecisionOut] = Field(default_factory=list)
    approvals: list[AutonomyApprovalOut] = Field(default_factory=list)


class AutonomyRunListResponse(BaseModel):
    runs: list[AutonomyRunOut]
    total: int


class AutonomyEvaluateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    proposed_action: str = Field(min_length=1, max_length=128)
    action_type: str
    purpose: str = Field(min_length=1, max_length=64)
    target_agent_id: str | None = None
    tool_name: str | None = None
    required_data_categories: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    trigger: str = "user_request"

    @field_validator("action_type")
    @classmethod
    def _validate_action_type(cls, value: str) -> str:
        val = value.lower()
        valid = [a.value for a in ActionType]
        if val not in valid:
            raise ValueError(f"action_type '{value}' is not allowed. Allowed: {valid}")
        return val


class AutonomyEvaluateResponse(BaseModel):
    decision: str
    reason: str
    risk_level: str
    action_type: str
    purpose: str
    policy_decision: str | None = None
    consent_decision: str | None = None
    requires_approval: bool = False


class AutonomyAuditOut(BaseModel):
    run_id: str | None = None
    event: str
    timestamp: str
    details: dict[str, Any] = Field(default_factory=dict)


class AutonomyAuditListResponse(BaseModel):
    audits: list[AutonomyAuditOut]
    total: int


__all__ = [
    "ActionPlanSpec",
    "AutonomyApprovalDecisionRequest",
    "AutonomyApprovalListResponse",
    "AutonomyApprovalOut",
    "AutonomyAuditListResponse",
    "AutonomyAuditOut",
    "AutonomyConfigOut",
    "AutonomyConfigUpdate",
    "AutonomyDecisionListResponse",
    "AutonomyDecisionOut",
    "AutonomyEvaluateRequest",
    "AutonomyEvaluateResponse",
    "AutonomyRunCreateRequest",
    "AutonomyRunListResponse",
    "AutonomyRunOut",
    "PlanActionSpec",
]
