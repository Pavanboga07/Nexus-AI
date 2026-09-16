"""Persistent A2A data (Part 6).

- TrustedAgent: "these are the agents I currently recognize" (per owner).
  Trust means recognition of a cryptographic identity - NOT authorization.
- A2ATask: request/response correlation. Lightweight by design.
- A2AMessageRecord: replay protection (UNIQUE owner+message_id) AND the
  audit trail. Metadata only - never payloads, keys, or disclosed content.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


class TrustStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PENDING_APPROVAL = "pending_approval"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    WAITING_REMOTE = "waiting_remote"


class TrustedAgent(Base):
    __tablename__ = "trusted_agents"
    __table_args__ = (
        UniqueConstraint("owner_id", "agent_id", name="uq_trusted_agents_owner_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # base64 raw Ed25519 public key; verified against agent_id at registration.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # http(s) endpoint of the remote agent's /a2a/messages route.
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TrustStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "endpoint": self.endpoint,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class A2ATask(Base):
    __tablename__ = "a2a_tasks"
    __table_args__ = (
        UniqueConstraint("owner_id", "task_id", name="uq_a2a_tasks_owner_task"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=TaskStatus.PENDING.value)
    task_type: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    negotiation_round: Mapped[int] = mapped_column(
        Integer, nullable=True, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "sender_agent_id": self.sender_agent_id,
            "recipient_agent_id": self.recipient_agent_id,
            "status": self.status,
            "task_type": self.task_type,
            "purpose": self.purpose,
            "request_payload": self.request_payload,
            "response_payload": self.response_payload,
            "negotiation_round": self.negotiation_round or 0,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class A2AMessageRecord(Base):
    """Replay protection + audit, one row per processed inbound message."""

    __tablename__ = "a2a_messages"
    __table_args__ = (
        UniqueConstraint("owner_id", "message_id", name="uq_a2a_messages_owner_message"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_type: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "message_id": self.message_id,
            "task_id": self.task_id,
            "sender_agent_id": self.sender_agent_id,
            "recipient_agent_id": self.recipient_agent_id,
            "message_type": self.message_type,
            "purpose": self.purpose,
            "policy_decision": self.policy_decision,
            "status": self.status,
            "error_code": self.error_code,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "processed_at": (
                self.processed_at.isoformat() if self.processed_at else None
            ),
        }


__all__ = ["A2AMessageRecord", "A2ATask", "TaskStatus", "TrustedAgent", "TrustStatus"]
