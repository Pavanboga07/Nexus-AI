"""Pydantic v2 schemas for the Discovery API (Part 7)."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


_ENDPOINT_PATTERN = re.compile(r"^https?://[^\s]+$")


class AgentCapabilityOut(BaseModel):
    """One capability in the agent card."""

    name: str
    description: str
    data_category: str


class AgentCardResponse(BaseModel):
    """The full signed agent card."""

    type: str
    protocol: str
    version: str
    agent_id: str
    display_name: str
    public_key: str
    endpoint: str
    capabilities: list[AgentCapabilityOut]
    supported_purposes: list[str]
    issued_at: str
    expires_at: str
    signature: str


class DiscoverRequest(BaseModel):
    """Request to discover and register a remote agent by card URL."""

    url: str = Field(min_length=1, max_length=2048)
    display_name: str | None = Field(default=None, max_length=255)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not _ENDPOINT_PATTERN.fullmatch(value):
            raise ValueError("url must be an http(s) URL")
        return value


class DiscoverResponse(BaseModel):
    """Result of a successful discovery + registration."""

    agent_id: str
    display_name: str
    endpoint: str
    status: str
    card: AgentCardResponse


__all__ = [
    "AgentCapabilityOut",
    "AgentCardResponse",
    "DiscoverRequest",
    "DiscoverResponse",
]
