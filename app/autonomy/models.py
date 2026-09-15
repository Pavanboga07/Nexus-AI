"""Persistent data models for Nexus Autonomy & Decision Engine (Part 10)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models import Base


class AutonomyMode(str, enum.Enum):
    OFF = "off"
    ASSISTED = "assisted"
    BOUNDED = "bounded"
    FULLY_DELEGATED = "fully_delegated"


class ActionType(str, enum.Enum):
    READ_MEMORY = "read_memory"
    READ_CONTEXT = "read_context"
    SEND_MESSAGE = "send_message"
    CREATE_TASK = "create_task"
    CREATE_WORKFLOW = "create_workflow"
    EXECUTE_TOOL = "execute_tool"
    CONTACT_AGENT = "contact_agent"
    UPDATE_MEMORY = "update_memory"
    SCHEDULE_ACTION = "schedule_action"
    REQUEST_APPROVAL = "request_approval"


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionResult(str, enum.Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    STOP = "stop"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_REMOTE = "waiting_remote"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TriggerType(str, enum.Enum):
    USER_REQUEST = "user_request"
    WORKFLOW_COMPLETED = "workflow_completed"
    REMOTE_TASK_COMPLETED = "remote_task_completed"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    SCHEDULED_EVENT = "scheduled_event"
    SYSTEM_EVENT = "system_event"


class AutonomyConfig(Base):
    __tablename__ = "autonomy_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, unique=True, index=True
    )
    mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AutonomyMode.BOUNDED.value
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_steps_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    max_remote_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    require_approval_for_unknown_actions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    require_approval_for_external_communication: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    require_approval_for_sensitive_data: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            "mode": self.mode,
            "enabled": self.enabled,
            "max_steps_per_run": self.max_steps_per_run,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_remote_tasks": self.max_remote_tasks,
            "max_tool_calls": self.max_tool_calls,
            "require_approval_for_unknown_actions": self.require_approval_for_unknown_actions,
            "require_approval_for_external_communication": self.require_approval_for_external_communication,
            "require_approval_for_sensitive_data": self.require_approval_for_sensitive_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AutonomyRun(Base):
    __tablename__ = "autonomy_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflows.workflow_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    goal: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RunStatus.PENDING.value, index=True
    )
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    context_data: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    decisions: Mapped[list[AutonomyDecision]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AutonomyDecision.created_at",
    )
    approvals: Mapped[list[AutonomyApproval]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AutonomyApproval.created_at",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            "workflow_id": str(self.workflow_id) if self.workflow_id else None,
            "goal": self.goal,
            "status": self.status,
            "current_step": self.current_step,
            "steps_executed": self.steps_executed,
            "tool_calls": self.tool_calls,
            "remote_tasks": self.remote_tasks,
            "plan": self.plan or [],
            "context_data": self.context_data or {},
            "failure_reason": self.failure_reason,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "decisions": [d.to_dict() for d in (self.decisions or [])],
            "approvals": [a.to_dict() for a in (self.approvals or [])],
        }


class AutonomyDecision(Base):
    __tablename__ = "autonomy_decisions"

    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("autonomy_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    goal: Mapped[str] = mapped_column(String(500), nullable=False)
    proposed_action: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    required_capability: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_data_categories: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consent_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped[AutonomyRun | None] = relationship(back_populates="decisions")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": str(self.decision_id),
            "owner_id": str(self.owner_id),
            "run_id": str(self.run_id) if self.run_id else None,
            "trigger": self.trigger,
            "goal": self.goal,
            "proposed_action": self.proposed_action,
            "action_type": self.action_type,
            "purpose": self.purpose,
            "risk_level": self.risk_level,
            "required_capability": self.required_capability,
            "required_data_categories": self.required_data_categories or [],
            "decision": self.decision,
            "reason": self.reason,
            "policy_decision": self.policy_decision,
            "consent_decision": self.consent_decision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AutonomyApproval(Base):
    __tablename__ = "autonomy_approvals"

    approval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("autonomy_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("autonomy_decisions.decision_id", ondelete="SET NULL"),
        nullable=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ApprovalStatus.PENDING.value, index=True
    )
    requested_action: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    required_data: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    recipient: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped[AutonomyRun] = relationship(back_populates="approvals")

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_id": str(self.approval_id),
            "run_id": str(self.run_id),
            "decision_id": str(self.decision_id) if self.decision_id else None,
            "owner_id": str(self.owner_id),
            "status": self.status,
            "requested_action": self.requested_action,
            "purpose": self.purpose,
            "risk_level": self.risk_level,
            "required_data": self.required_data or {},
            "recipient": self.recipient,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class AutonomyTrigger(Base):
    __tablename__ = "autonomy_triggers"

    trigger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("autonomy_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger_id": str(self.trigger_id),
            "owner_id": str(self.owner_id),
            "run_id": str(self.run_id) if self.run_id else None,
            "trigger_type": self.trigger_type,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = [
    "ActionType",
    "ApprovalStatus",
    "AutonomyApproval",
    "AutonomyConfig",
    "AutonomyDecision",
    "AutonomyMode",
    "AutonomyRun",
    "AutonomyTrigger",
    "DecisionResult",
    "RiskLevel",
    "RunStatus",
    "TriggerType",
]
