"""Owner-scoped database repositories for Nexus Autonomy (Part 10)."""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.autonomy.models import (
    ApprovalStatus,
    AutonomyApproval,
    AutonomyConfig,
    AutonomyDecision,
    AutonomyMode,
    AutonomyRun,
    AutonomyTrigger,
    RunStatus,
)


class AutonomyConfigRepository:
    async def get_or_create_default(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> AutonomyConfig:
        stmt = select(AutonomyConfig).where(AutonomyConfig.owner_id == owner_id)
        res = await session.execute(stmt)
        config = res.scalar_one_or_none()
        if config is None:
            config = AutonomyConfig(
                owner_id=owner_id,
                mode=AutonomyMode.BOUNDED.value,
                enabled=True,
                max_steps_per_run=10,
                max_runtime_seconds=3600,
                max_remote_tasks=5,
                max_tool_calls=10,
                require_approval_for_unknown_actions=True,
                require_approval_for_external_communication=True,
                require_approval_for_sensitive_data=True,
            )
            session.add(config)
            await session.flush()
        return config

    async def update(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        **fields,
    ) -> AutonomyConfig:
        config = await self.get_or_create_default(session, owner_id)
        for k, v in fields.items():
            if v is not None and hasattr(config, k):
                setattr(config, k, v)
        await session.flush()
        return config


class AutonomyRunRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        goal: str,
        plan: list | None = None,
        context_data: dict | None = None,
        workflow_id: uuid.UUID | None = None,
    ) -> AutonomyRun:
        run = AutonomyRun(
            owner_id=owner_id,
            goal=goal,
            plan=plan or [],
            context_data=context_data or {},
            workflow_id=workflow_id,
            status=RunStatus.PENDING.value,
        )
        session.add(run)
        await session.flush()
        return run

    async def get(
        self, session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID | None = None
    ) -> AutonomyRun | None:
        stmt = (
            select(AutonomyRun)
            .options(
                selectinload(AutonomyRun.decisions),
                selectinload(AutonomyRun.approvals),
            )
            .where(AutonomyRun.id == run_id)
        )
        if owner_id is not None:
            stmt = stmt.where(AutonomyRun.owner_id == owner_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def list(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AutonomyRun]:
        stmt = (
            select(AutonomyRun)
            .options(
                selectinload(AutonomyRun.decisions),
                selectinload(AutonomyRun.approvals),
            )
            .where(AutonomyRun.owner_id == owner_id)
        )
        if status:
            stmt = stmt.where(AutonomyRun.status == status)
        stmt = stmt.order_by(AutonomyRun.created_at.desc()).offset(offset).limit(limit)
        res = await session.execute(stmt)
        return res.scalars().all()

    async def list_running_all(self, session: AsyncSession) -> Sequence[AutonomyRun]:
        """Fetch all running runs across owners for crash recovery."""
        stmt = (
            select(AutonomyRun)
            .options(
                selectinload(AutonomyRun.decisions),
                selectinload(AutonomyRun.approvals),
            )
            .where(AutonomyRun.status == RunStatus.RUNNING.value)
        )
        res = await session.execute(stmt)
        return res.scalars().all()

    async def find_by_workflow_id(
        self, session: AsyncSession, workflow_id: uuid.UUID
    ) -> Sequence[AutonomyRun]:
        """Fetch runs linked to a workflow (A8 run linkage)."""
        stmt = select(AutonomyRun).where(AutonomyRun.workflow_id == workflow_id)
        res = await session.execute(stmt)
        return res.scalars().all()


class AutonomyDecisionRepository:
    async def list_by_run(
        self, session: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID
    ) -> Sequence[AutonomyDecision]:
        stmt = (
            select(AutonomyDecision)
            .where(AutonomyDecision.owner_id == owner_id, AutonomyDecision.run_id == run_id)
            .order_by(AutonomyDecision.created_at.asc())
        )
        res = await session.execute(stmt)
        return res.scalars().all()


class AutonomyApprovalRepository:
    async def get(
        self, session: AsyncSession, approval_id: uuid.UUID, owner_id: uuid.UUID
    ) -> AutonomyApproval | None:
        stmt = select(AutonomyApproval).where(
            AutonomyApproval.approval_id == approval_id,
            AutonomyApproval.owner_id == owner_id,
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_pending_by_run(
        self, session: AsyncSession, run_id: uuid.UUID
    ) -> AutonomyApproval | None:
        stmt = select(AutonomyApproval).where(
            AutonomyApproval.run_id == run_id,
            AutonomyApproval.status == ApprovalStatus.PENDING.value,
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_run(
        self, session: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID
    ) -> Sequence[AutonomyApproval]:
        stmt = (
            select(AutonomyApproval)
            .where(AutonomyApproval.owner_id == owner_id, AutonomyApproval.run_id == run_id)
            .order_by(AutonomyApproval.created_at.asc())
        )
        res = await session.execute(stmt)
        return res.scalars().all()


__all__ = [
    "AutonomyApprovalRepository",
    "AutonomyConfigRepository",
    "AutonomyDecisionRepository",
    "AutonomyRunRepository",
]
