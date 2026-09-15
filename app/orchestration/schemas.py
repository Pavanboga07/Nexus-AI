"""Pydantic schemas for Natural Language Agent Orchestration (Part 12)."""

from __future__ import annotations

import enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class IntentType(str, enum.Enum):
    ASK_PERSON = "ASK_PERSON"
    CONTACT_AGENT = "CONTACT_AGENT"
    CHECK_AVAILABILITY = "CHECK_AVAILABILITY"
    SEND_INFORMATION = "SEND_INFORMATION"
    COORDINATE_MEETING = "COORDINATE_MEETING"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    NEGOTIATE = "NEGOTIATE"
    DELEGATE_TASK = "DELEGATE_TASK"
    CREATE_WORKFLOW = "CREATE_WORKFLOW"
    CONFIRM_ACTION = "CONFIRM_ACTION"
    GENERAL_CHAT = "GENERAL_CHAT"


class Intent(BaseModel):
    """Structured representation of natural language user intent."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(description="Normalized summary of what the user wants to accomplish")
    intent_type: IntentType
    target: str | None = Field(default=None, description="Primary person / agent to contact")
    secondary_targets: list[str] = Field(default_factory=list, description="Additional persons for multi-agent coordination")
    purpose: str = Field(default="collaboration", description="Purpose for A2A and Policy authorization")
    requested_information: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict, description="Time, date, and scope constraints")
    action_payload: dict[str, Any] = Field(default_factory=dict)


class TargetResolutionStatus(str, enum.Enum):
    KNOWN_AGENT = "KNOWN_AGENT"
    DISCOVERED_AGENT = "DISCOVERED_AGENT"
    AMBIGUOUS_AGENT = "AMBIGUOUS_AGENT"
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    UNAVAILABLE_AGENT = "UNAVAILABLE_AGENT"


class TargetResolution(BaseModel):
    """Result of mapping a human name / alias to an agent identity."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    status: TargetResolutionStatus
    agent_id: str | None = None
    endpoint: str | None = None
    display_name: str | None = None
    is_trusted: bool = False
    candidates: list[str] = Field(default_factory=list)
    card: dict[str, Any] | None = None


class PlanStep(BaseModel):
    """A bounded, authorized step in an orchestration plan."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    step_type: str
    description: str
    target_agent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    approval_prompt: str | None = None


class OrchestrationPlan(BaseModel):
    """Ordered sequence of steps to fulfill an intent."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    intent_type: str
    target_person: str | None = None
    target_agent_id: str | None = None
    steps: list[PlanStep] = Field(default_factory=list)


# --- API Request & Response Models ---


class OrchestrationExecuteRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class OrchestrationExecuteResponse(BaseModel):
    run_id: str
    status: str
    message: str
    intent_type: str
    requires_approval: bool = False
    approval_prompt: str | None = None
    target: str | None = None
    details: dict[str, Any] | None = None


class OrchestrationRunResponse(BaseModel):
    run_id: str
    session_id: str
    goal: str
    intent_type: str
    state: str
    target_person: str | None = None
    target_agent_id: str | None = None
    requires_approval: bool = False
    approval_prompt: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str


class ContactCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    agent_id: str | None = None
    endpoint: str | None = None
    notes: str | None = None


class ContactResponse(BaseModel):
    id: str
    display_name: str
    aliases: list[str]
    agent_id: str | None = None
    endpoint: str | None = None
    notes: str | None = None
    created_at: str
