"""Persistent workflow data models (Part 9).

- Workflow: durable multi-step coordination instance scoped to an owner.
- WorkflowStep: ordered atomic step with bounded retry, status, and task linkage.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models import Base


class WorkflowStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_REMOTE = "waiting_remote"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class StepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Workflow(Base):
    __tablename__ = "workflows"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowStatus.PENDING.value, index=True
    )
    current_step_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_data: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    workflow_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="workflow",
        order_by="WorkflowStep.step_number",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow_id": str(self.workflow_id),
            "owner_id": str(self.owner_id),
            "workflow_type": self.workflow_type,
            "purpose": self.purpose,
            "status": self.status,
            "current_step_number": self.current_step_number,
            "context_data": self.context_data or {},
            "workflow_metadata": self.workflow_metadata or {},
            "failure_reason": self.failure_reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "steps": [s.to_dict() for s in (self.steps or [])],
        }


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("workflow_id", "step_number", name="uq_workflow_steps_workflow_step_number"),
    )

    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StepStatus.PENDING.value, index=True
    )
    input_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    output_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    workflow: Mapped[Workflow] = relationship(back_populates="steps")

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": str(self.step_id),
            "workflow_id": str(self.workflow_id),
            "step_number": self.step_number,
            "step_type": self.step_type,
            "status": self.status,
            "input_payload": self.input_payload or {},
            "output_payload": self.output_payload,
            "task_id": self.task_id,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "failure_reason": self.failure_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "StepStatus",
    "Workflow",
    "WorkflowStatus",
    "WorkflowStep",
]
