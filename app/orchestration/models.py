"""SQLAlchemy models for Natural Language Agent Orchestration (Part 12)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrchestrationState(str, enum.Enum):
    UNDERSTANDING = "UNDERSTANDING"
    RESOLVING_TARGET = "RESOLVING_TARGET"
    DISCOVERING_AGENT = "DISCOVERING_AGENT"
    WAITING_FOR_TRUST = "WAITING_FOR_TRUST"
    PLANNING = "PLANNING"
    AUTHORIZING = "AUTHORIZING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    WAITING_REMOTE = "WAITING_REMOTE"
    PROCESSING_RESULT = "PROCESSING_RESULT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class Contact(Base):
    """Maps a real-world human / contact to known aliases and their Nexus Agent."""

    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_owner_id", "owner_id"),
        Index("ix_contacts_display_name", "display_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("owners.id"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # List of known aliases or nicknames, e.g. ["Rahul", "Rahul Patil", "RP"]
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )


class OrchestrationRun(Base):
    """Tracks state machine transitions, plan steps, and audit logs for natural language orchestration."""

    __tablename__ = "orchestration_runs"
    __table_args__ = (
        Index("ix_orchestration_runs_owner_id", "owner_id"),
        Index("ix_orchestration_runs_session_id", "session_id"),
        Index("ix_orchestration_runs_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("owners.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    intent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OrchestrationState.UNDERSTANDING.value,
    )
    target_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    approval_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
