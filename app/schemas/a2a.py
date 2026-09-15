"""Pydantic v2 schemas for the A2A API."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.a2a.schemas import AGENT_ID_PATTERN, PURPOSE_PATTERN

_ENDPOINT_PATTERN = re.compile(r"^https?://[^\s]+$")


class TrustedAgentCreate(BaseModel):
    agent_id: str
    public_key: str = Field(min_length=8, max_length=512)
    display_name: str = Field(min_length=1, max_length=255)
    endpoint: str

    @field_validator("agent_id")
    @classmethod
    def _agent_id(cls, value: str) -> str:
        if not AGENT_ID_PATTERN.fullmatch(value):
            raise ValueError("agent_id must be nexus:ed25519:<32 hex chars>")
        return value

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        if not _ENDPOINT_PATTERN.fullmatch(value):
            raise ValueError("endpoint must be an http(s) URL")
        return value


class TrustedAgentOut(BaseModel):
    agent_id: str
    display_name: str
    endpoint: str
    status: str
    created_at: str | None = None


class TrustedAgentListResponse(BaseModel):
    agents: list[TrustedAgentOut]
    total: int


class A2AMessageIn(BaseModel):
    """Inbound signed envelope - validated structurally; cryptographic
    verification happens in the service, never in the schema."""

    model_config = {"extra": "forbid"}

    protocol: Literal["nexus-a2a"]
    version: Literal["0.1"]
    message_id: str
    task_id: str
    sender: str
    recipient: str
    timestamp: str
    expires_at: str
    message_type: str
    purpose: str
    payload: dict[str, Any]
    signature: str


class A2ASendRequest(BaseModel):
    recipient_agent_id: str
    purpose: str
    action: str
    data_category: str
    payload: dict[str, Any] = Field(default_factory=dict)
    endpoint: str | None = None

    @field_validator("recipient_agent_id")
    @classmethod
    def _recipient(cls, value: str) -> str:
        if not AGENT_ID_PATTERN.fullmatch(value):
            raise ValueError("recipient_agent_id must be a nexus agent id")
        return value

    @field_validator("purpose", "action", "data_category")
    @classmethod
    def _slugs(cls, value: str, info) -> str:
        if not PURPOSE_PATTERN.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a lowercase slug")
        return value


class A2ASendResponse(BaseModel):
    task_id: str
    recipient: str
    status: str
    payload: dict[str, Any]


class A2AAuditEntryOut(BaseModel):
    id: str
    message_id: str
    task_id: str
    sender_agent_id: str
    recipient_agent_id: str
    message_type: str
    purpose: str
    policy_decision: str | None = None
    status: str
    error_code: str | None = None
    created_at: str | None = None
    processed_at: str | None = None


class A2AAuditResponse(BaseModel):
    messages: list[A2AAuditEntryOut]
    total: int


class DeleteResponse(BaseModel):
    deleted: bool
    agent_id: str


__all__ = [
    "A2AAuditEntryOut",
    "A2AAuditResponse",
    "A2AMessageIn",
    "A2ASendRequest",
    "A2ASendResponse",
    "DeleteResponse",
    "TrustedAgentCreate",
    "TrustedAgentListResponse",
    "TrustedAgentOut",
]
