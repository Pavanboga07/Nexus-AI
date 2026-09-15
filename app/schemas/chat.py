"""Pydantic v2 request/response models for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["system", "user", "assistant"]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    environment: str
    llm_provider: str
    llm_configured: bool
    database: bool = False
    memory: bool = False
    identity: bool = False
    tools: bool = False
    a2a: bool = False
    autonomy: bool = False
    gateway: bool = False


class SessionCreateResponse(BaseModel):
    session_id: str


class MessageModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Role
    content: str


class SessionResponse(BaseModel):
    session_id: str
    messages: list[MessageModel]
    created_at: str
    updated_at: str


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)

    @field_validator("session_id", "message", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("message must not be empty")
        return value


class ChatResponse(BaseModel):
    session_id: str
    response: str


class ErrorResponse(BaseModel):
    """Uniform error envelope. Never contains stack traces or credentials."""

    error: str
    detail: str


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ErrorResponse",
    "HealthResponse",
    "MessageModel",
    "Role",
    "SessionCreateResponse",
    "SessionResponse",
]
