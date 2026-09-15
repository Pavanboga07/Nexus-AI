"""Owner-scoped persistence for A2A data: trusted agents, tasks, messages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.models import A2AMessageRecord, A2ATask, TrustedAgent, TrustStatus


class TrustedAgentRepository:
    async def add(self, session: AsyncSession, agent: TrustedAgent) -> TrustedAgent:
        session.add(agent)
        await session.flush()
        return agent

    async def get(
        self, session: AsyncSession, owner_id: uuid.UUID, agent_id: str
    ) -> TrustedAgent | None:
        result = await session.execute(
            select(TrustedAgent).where(
                TrustedAgent.owner_id == owner_id,
                TrustedAgent.agent_id == agent_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_active(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> list[TrustedAgent]:
        result = await session.execute(
            select(TrustedAgent)
            .where(TrustedAgent.owner_id == owner_id)
            .order_by(TrustedAgent.created_at.asc())
        )
        return list(result.scalars())

    async def set_status(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        agent_id: str,
        status: TrustStatus,
    ) -> TrustedAgent | None:
        agent = await self.get(session, owner_id, agent_id)
        if agent is None:
            return None
        agent.status = status.value
        await session.flush()
        return agent

    async def delete(
        self, session: AsyncSession, owner_id: uuid.UUID, agent_id: str
    ) -> bool:
        result = await session.execute(
            delete(TrustedAgent).where(
                TrustedAgent.owner_id == owner_id,
                TrustedAgent.agent_id == agent_id,
            )
        )
        return bool(result.rowcount)


class TaskRepository:
    async def upsert(
        self, session: AsyncSession, task: A2ATask
    ) -> A2ATask:
        existing = await session.execute(
            select(A2ATask).where(
                A2ATask.owner_id == task.owner_id,
                A2ATask.task_id == task.task_id,
            )
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            found.status = task.status
            if task.task_type is not None:
                found.task_type = task.task_type
            if task.purpose is not None:
                found.purpose = task.purpose
            if task.request_payload is not None:
                found.request_payload = task.request_payload
            if task.response_payload is not None:
                found.response_payload = task.response_payload
            if task.completed_at is not None:
                found.completed_at = task.completed_at
            if task.negotiation_round is not None:
                found.negotiation_round = task.negotiation_round
            if task.expires_at is not None:
                found.expires_at = task.expires_at
            found.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return found
        if task.negotiation_round is None:
            task.negotiation_round = 0
        session.add(task)
        await session.flush()
        return task

    async def get(
        self, session: AsyncSession, owner_id: uuid.UUID, task_id: str
    ) -> A2ATask | None:
        result = await session.execute(
            select(A2ATask).where(
                A2ATask.owner_id == owner_id, A2ATask.task_id == task_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_owner(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[A2ATask]:
        query = select(A2ATask).where(A2ATask.owner_id == owner_id)
        if status is not None:
            query = query.where(A2ATask.status == status)
        query = query.order_by(A2ATask.created_at.desc()).limit(limit)
        result = await session.execute(query)
        return list(result.scalars())

    async def update_status(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        task_id: str,
        status: str,
        *,
        response_payload: dict | None = None,
        failure_reason: str | None = None,
        completed_at: datetime | None = None,
    ) -> A2ATask | None:
        task = await self.get(session, owner_id, task_id)
        if task is None:
            return None
        task.status = status
        if response_payload is not None:
            task.response_payload = response_payload
        if failure_reason is not None:
            task.failure_reason = failure_reason
        if completed_at is not None:
            task.completed_at = completed_at
        task.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return task


class MessageRecordRepository:
    """Replay protection (unique owner+message_id) + audit queries."""

    async def try_record(
        self, session: AsyncSession, record: A2AMessageRecord
    ) -> bool:
        """Insert the message ID atomically.

        Returns False when the (owner, message_id) pair already exists -
        i.e. a replay. The unique constraint makes this race-safe.
        """
        try:
            session.add(record)
            await session.flush()
            return True
        except IntegrityError:
            await session.rollback()
            return False

    async def list_for_owner(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> list[A2AMessageRecord]:
        result = await session.execute(
            select(A2AMessageRecord)
            .where(A2AMessageRecord.owner_id == owner_id)
            .order_by(A2AMessageRecord.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_processed(
        self, session: AsyncSession, record: A2AMessageRecord, status: str,
        policy_decision: str | None, error_code: str | None,
    ) -> A2AMessageRecord:
        record.status = status
        record.policy_decision = policy_decision
        record.error_code = error_code
        record.processed_at = datetime.now(timezone.utc)
        await session.flush()
        return record

    async def get_by_message_id(
        self, session: AsyncSession, owner_id: uuid.UUID, message_id: str
    ) -> A2AMessageRecord | None:
        result = await session.execute(
            select(A2AMessageRecord).where(
                A2AMessageRecord.owner_id == owner_id,
                A2AMessageRecord.message_id == message_id,
            )
        )
        return result.scalar_one_or_none()


__all__ = [
    "MessageRecordRepository",
    "TaskRepository",
    "TrustedAgentRepository",
]
