"""Repositories for persistent workflows and workflow steps (Part 9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.workflows.models import StepStatus, Workflow, WorkflowStatus, WorkflowStep


class WorkflowRepository:
    """Persistence operations for workflows."""

    async def create(self, session: AsyncSession, workflow: Workflow) -> Workflow:
        session.add(workflow)
        await session.flush()
        return workflow

    async def get(
        self, session: AsyncSession, workflow_id: uuid.UUID
    ) -> Workflow | None:
        result = await session.execute(
            select(Workflow)
            .options(selectinload(Workflow.steps))
            .where(Workflow.workflow_id == workflow_id)
        )
        return result.scalar_one_or_none()

    async def get_for_owner(
        self, session: AsyncSession, owner_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> Workflow | None:
        result = await session.execute(
            select(Workflow)
            .options(selectinload(Workflow.steps))
            .where(
                Workflow.workflow_id == workflow_id,
                Workflow.owner_id == owner_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_owner(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[Workflow]:
        query = (
            select(Workflow)
            .options(selectinload(Workflow.steps))
            .where(Workflow.owner_id == owner_id)
        )
        if status:
            query = query.where(Workflow.status == status)
        query = query.order_by(Workflow.created_at.desc()).limit(limit)
        result = await session.execute(query)
        return result.scalars().all()

    async def update_status(
        self,
        session: AsyncSession,
        workflow_id: uuid.UUID,
        status: str,
        current_step_number: int | None = None,
        context_data: dict | None = None,
        failure_reason: str | None = None,
        completed_at: datetime | None = None,
    ) -> Workflow | None:
        workflow = await self.get(session, workflow_id)
        if not workflow:
            return None
        workflow.status = status
        if current_step_number is not None:
            workflow.current_step_number = current_step_number
        if context_data is not None:
            workflow.context_data = context_data
        if failure_reason is not None:
            workflow.failure_reason = failure_reason
        if completed_at is not None:
            workflow.completed_at = completed_at
        await session.flush()
        return workflow

    async def find_interrupted_workflows(
        self, session: AsyncSession
    ) -> Sequence[Workflow]:
        """Find workflows left in RUNNING status (e.g. after crash)."""
        result = await session.execute(
            select(Workflow)
            .options(selectinload(Workflow.steps))
            .where(Workflow.status == WorkflowStatus.RUNNING.value)
            .order_by(Workflow.created_at.asc())
        )
        return result.scalars().all()


class WorkflowStepRepository:
    """Persistence operations for workflow steps."""

    async def create_steps(
        self, session: AsyncSession, steps: list[WorkflowStep]
    ) -> list[WorkflowStep]:
        session.add_all(steps)
        await session.flush()
        return steps

    async def get(
        self, session: AsyncSession, step_id: uuid.UUID
    ) -> WorkflowStep | None:
        result = await session.execute(
            select(WorkflowStep).where(WorkflowStep.step_id == step_id)
        )
        return result.scalar_one_or_none()

    async def get_step_by_number(
        self, session: AsyncSession, workflow_id: uuid.UUID, step_number: int
    ) -> WorkflowStep | None:
        result = await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == workflow_id,
                WorkflowStep.step_number == step_number,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_workflow(
        self, session: AsyncSession, workflow_id: uuid.UUID
    ) -> Sequence[WorkflowStep]:
        result = await session.execute(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == workflow_id)
            .order_by(WorkflowStep.step_number.asc())
        )
        return result.scalars().all()

    async def acquire_next_pending_step(
        self, session: AsyncSession, workflow_id: uuid.UUID
    ) -> WorkflowStep | None:
        """Lock and return the next pending step to prevent duplicate worker execution."""
        result = await session.execute(
            select(WorkflowStep)
            .where(
                WorkflowStep.workflow_id == workflow_id,
                WorkflowStep.status == StepStatus.PENDING.value,
            )
            .order_by(WorkflowStep.step_number.asc())
            .limit(1)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def find_step_by_task_id(
        self, session: AsyncSession, task_id: str
    ) -> WorkflowStep | None:
        result = await session.execute(
            select(WorkflowStep).where(WorkflowStep.task_id == task_id)
        )
        return result.scalar_one_or_none()

    async def find_running_steps_for_workflow(
        self, session: AsyncSession, workflow_id: uuid.UUID
    ) -> Sequence[WorkflowStep]:
        """Find steps left in RUNNING status (e.g. after crash)."""
        result = await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == workflow_id,
                WorkflowStep.status == StepStatus.RUNNING.value,
            )
        )
        return result.scalars().all()


__all__ = [
    "WorkflowRepository",
    "WorkflowStepRepository",
]
