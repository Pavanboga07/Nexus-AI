"""Pydantic v2 schemas for the identity API (public material only)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IdentityResponse(BaseModel):
    agent_id: str
    public_key: str
    key_algorithm: str
    fingerprint: str


class IdentityVerifyRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    public_key: str = Field(min_length=1)  # base64 raw Ed25519 public key
    message: str = Field(min_length=1, max_length=100_000)
    signature: str = Field(min_length=1)  # base64 signature


class IdentityVerifyResponse(BaseModel):
    valid: bool
    agent_id_matches: bool
    reason: str | None = None


# --- M4: multi-agent registry -------------------------------------------------


class AgentCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    handle: str | None = Field(default=None, max_length=64)
    endpoint: str | None = Field(default=None, max_length=2048)
    make_primary: bool = False


class AgentOut(BaseModel):
    id: str
    agent_id: str
    display_name: str
    handle: str | None = None
    endpoint: str | None = None
    status: str
    is_primary: bool
    created_at: str | None = None


class AgentListResponse(BaseModel):
    agents: list[AgentOut]
    total: int


class AgentStatusUpdate(BaseModel):
    status: Literal["active", "paused", "revoked"]


class AgentKeyOut(BaseModel):
    public_key: str
    algorithm: str
    fingerprint: str
    not_before: str | None = None
    not_after: str | None = None
    revoked_at: str | None = None
    revoked_reason: str | None = None
    is_current: bool


class AgentKeyRotateResponse(BaseModel):
    agent: AgentOut
    keys: list[AgentKeyOut]
    note: str


class AgentCardResponse(BaseModel):
    """A signed agent card. Signature covers the canonical form (minus the
    signature field itself)."""

    model_config = ConfigDict(extra="allow")

    type: str
    protocol: str
    version: str
    agent_id: str
    display_name: str
    public_key: str
    endpoint: str
    capabilities: list[dict] = Field(default_factory=list)
    supported_purposes: list[str] = Field(default_factory=list)
    issued_at: str
    expires_at: str
    signature: str | None = None


class DeleteResponse(BaseModel):
    deleted: bool
    agent_id: str | None = None


class CapabilityOut(BaseModel):
    """One typed capability contract from the live registry.

    Field names match CapabilitySpec.to_dict() exactly, so the frontend can
    render what the agent will actually accept - not a static list.
    """

    id: str
    version: str
    description: str = ""
    data_category: str = "custom"
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityOut]
    total: int


__all__ = [
    "AgentCardResponse",
    "AgentCreateRequest",
    "AgentKeyOut",
    "AgentKeyRotateResponse",
    "AgentListResponse",
    "AgentOut",
    "AgentStatusUpdate",
    "CapabilityListResponse",
    "CapabilityOut",
    "DeleteResponse",
    "IdentityResponse",
    "IdentityVerifyRequest",
    "IdentityVerifyResponse",
]
