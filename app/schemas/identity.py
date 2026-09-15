"""Pydantic v2 schemas for the identity API (public material only)."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


__all__ = [
    "IdentityResponse",
    "IdentityVerifyRequest",
    "IdentityVerifyResponse",
]
