"""Pydantic v2 schemas for the tools API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

import re

_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SLUG_PATTERN = re.compile(r"^[a-z0-9:_\-.]{1,64}$")


class ToolMetadataOut(BaseModel):
    name: str
    description: str
    inputSchema: dict[str, Any]


class ToolListResponse(BaseModel):
    tools: list[ToolMetadataOut]
    total: int


class ToolExecuteRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(default=None, max_length=128)
    purpose: str = Field(min_length=1, max_length=64)

    @field_validator("tool_name")
    @classmethod
    def _validate_tool_name(cls, value: str) -> str:
        if not _TOOL_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "tool_name must be lowercase letters, digits, underscore or "
                "hyphen, starting with a letter"
            )
        return value

    @field_validator("purpose")
    @classmethod
    def _validate_purpose(cls, value: str) -> str:
        if not _SLUG_PATTERN.fullmatch(value):
            raise ValueError(
                "purpose may contain only lowercase letters, digits, and "
                "- : _ ."
            )
        return value

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", value):
            raise ValueError(
                "request_id may contain only letters, digits, underscores, "
                "and hyphens"
            )
        return value


class ToolExecuteResponse(BaseModel):
    success: bool
    status: Literal[
        "executed", "approval_required", "denied", "failed"
    ]
    tool_name: str
    request_id: str
    data: dict[str, Any] | None = None
    error: dict[str, str] | None = None


class ToolAuditEntryOut(BaseModel):
    id: str
    request_id: str
    tool_name: str
    purpose: str
    status: str
    policy_decision: str
    error_code: str | None = None
    created_at: str | None = None
    completed_at: str | None = None


class ToolAuditResponse(BaseModel):
    executions: list[ToolAuditEntryOut]
    total: int


__all__ = [
    "ToolAuditEntryOut",
    "ToolAuditResponse",
    "ToolExecuteRequest",
    "ToolExecuteResponse",
    "ToolListResponse",
    "ToolMetadataOut",
]
