"""Owner-scoped persistence for tool execution audit records."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.models import ToolExecutionRecord


class ToolExecutionRepository:
    async def add(
        self, session: AsyncSession, record: ToolExecutionRecord
    ) -> ToolExecutionRecord:
        session.add(record)
        await session.flush()
        return record

    async def list_for_owner(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> list[ToolExecutionRecord]:
        result = await session.execute(
            select(ToolExecutionRecord)
            .where(ToolExecutionRecord.owner_id == owner_id)
            .order_by(ToolExecutionRecord.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_completed(
        self, session: AsyncSession, record: ToolExecutionRecord
    ) -> ToolExecutionRecord:
        record.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return record


__all__ = ["ToolExecutionRepository"]
