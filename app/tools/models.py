"""Persistent audit records for tool executions (Part 5).

One row per tool invocation, whatever the outcome. Stores WHO requested,
WHAT tool, WHY (purpose), the policy decision, and the execution status -
never the raw arguments or tool output (those can contain sensitive data).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


class ToolExecutionStatus(str, enum.Enum):
    APPROVAL_REQUIRED = "approval_required"
    ALLOWED = "allowed"          # policy ALLOW, execution about to start
    DENIED = "denied"
    EXECUTED = "executed"
    FAILED = "failed"


class ToolExecutionRecord(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        Index("ix_tool_executions_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_decision: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "purpose": self.purpose,
            "status": self.status,
            "policy_decision": self.policy_decision,
            "error_code": self.error_code,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


__all__ = ["ToolExecutionRecord", "ToolExecutionStatus"]
