"""Pydantic v2 schemas for the policy & consent API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DecisionLiteral = Literal["ALLOW", "ASK", "DENY"]
ScopeLiteral = Literal["none", "category", "summary", "exact"]
ConsentDecisionLiteral = Literal["ALLOW", "DENY"]

_SLUG_PATTERN = r"[a-z0-9:_\-.]+"


class _SlugMixin(BaseModel):
    @field_validator("data_category", "action", "purpose", check_fields=False)
    @classmethod
    def _validate_slugs(cls, value: str, info) -> str:
        if value == "*":
            return value
        if not value or len(value) > 64:
            raise ValueError(f"{info.field_name} must be 1-64 characters")
        import re

        if not re.fullmatch(_SLUG_PATTERN, value):
            raise ValueError(
                f"{info.field_name} may contain only lowercase letters, "
                "digits, and - : _ ."
            )
        return value


class PolicyCreate(_SlugMixin):
    requester_agent_id: str = Field(min_length=1, max_length=255)
    data_category: str
    action: str
    purpose: str
    decision: DecisionLiteral
    disclosure_scope: ScopeLiteral = "category"
    priority: int = Field(default=0, ge=0, le=1000)
    starts_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("requester_agent_id")
    @classmethod
    def _validate_requester(cls, value: str) -> str:
        if value != "*":
            if len(value) > 255:
                raise ValueError("requester_agent_id too long")
            import re

            if not re.fullmatch(_SLUG_PATTERN, value):
                raise ValueError(
                    "requester_agent_id may contain only lowercase letters, "
                    "digits, and - : _ . (or the wildcard *)"
                )
        return value


class PolicyOut(_SlugMixin):
    id: str
    requester_agent_id: str
    data_category: str
    action: str
    purpose: str
    decision: DecisionLiteral
    disclosure_scope: ScopeLiteral
    priority: int
    starts_at: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PolicyListResponse(BaseModel):
    policies: list[PolicyOut]
    total: int


class ConsentCreate(_SlugMixin):
    requester_agent_id: str = Field(min_length=1, max_length=255)
    data_category: str
    action: str
    purpose: str
    decision: ConsentDecisionLiteral
    disclosure_scope: ScopeLiteral = "category"
    expires_at: datetime | None = None
    single_use: bool = False

    @field_validator("requester_agent_id")
    @classmethod
    def _validate_requester(cls, value: str) -> str:
        if value == "*":
            raise ValueError(
                "consents record a concrete approval: wildcard requester "
                "is not allowed"
            )
        if len(value) > 255:
            raise ValueError("requester_agent_id too long")
        import re

        if not re.fullmatch(_SLUG_PATTERN, value):
            raise ValueError(
                "requester_agent_id may contain only lowercase letters, "
                "digits, and - : _ ."
            )
        return value


class ConsentOut(_SlugMixin):
    id: str
    requester_agent_id: str
    data_category: str
    action: str
    purpose: str
    decision: ConsentDecisionLiteral
    disclosure_scope: ScopeLiteral
    expires_at: str | None = None
    single_use: bool
    used_at: str | None = None
    created_at: str | None = None


class ConsentListResponse(BaseModel):
    consents: list[ConsentOut]
    total: int


class PolicyEvaluateRequest(_SlugMixin):
    requester_agent_id: str = Field(min_length=1, max_length=255)
    data_category: str
    action: str
    purpose: str
    resource_id: str | None = Field(default=None, max_length=255)

    @field_validator("requester_agent_id")
    @classmethod
    def _validate_requester(cls, value: str) -> str:
        if value == "*":
            raise ValueError(
                "evaluation requests must name a concrete requester "
                "(wildcard is not a requester)"
            )
        import re

        if not re.fullmatch(_SLUG_PATTERN, value):
            raise ValueError(
                "requester_agent_id may contain only lowercase letters, "
                "digits, and - : _ ."
            )
        return value


class PolicyEvaluateResponse(BaseModel):
    decision: DecisionLiteral
    reason: str
    matched_policy_id: str | None = None
    matched_consent_id: str | None = None
    requires_user_approval: bool
    disclosure_scope: ScopeLiteral | None = None


class AuditEntryOut(BaseModel):
    id: str
    requester_agent_id: str
    data_category: str
    action: str
    purpose: str
    decision: DecisionLiteral
    matched_policy_id: str | None = None
    matched_consent_id: str | None = None
    reason: str
    created_at: str | None = None


class AuditListResponse(BaseModel):
    decisions: list[AuditEntryOut]
    total: int


class DeletedResponse(BaseModel):
    deleted: bool
    id: str


__all__ = [
    "AuditEntryOut",
    "AuditListResponse",
    "ConsentCreate",
    "ConsentDecisionLiteral",
    "ConsentListResponse",
    "ConsentOut",
    "DecisionLiteral",
    "DeletedResponse",
    "PolicyCreate",
    "PolicyEvaluateRequest",
    "PolicyEvaluateResponse",
    "PolicyListResponse",
    "PolicyOut",
    "ScopeLiteral",
]
