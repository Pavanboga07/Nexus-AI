"""Event and trigger subsystem for Nexus Autonomy (Part 10).

Provides simple database-backed trigger recording and query capabilities.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.autonomy.models import AutonomyTrigger, TriggerType

logger = logging.getLogger("nexus.autonomy.triggers")


class TriggerService:
    """Manages recording and observation of triggers."""

    async def record_trigger(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        trigger_type: str | TriggerType,
        run_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AutonomyTrigger:
        ttype = trigger_type.value if isinstance(trigger_type, TriggerType) else str(trigger_type)
        trigger = AutonomyTrigger(
            owner_id=owner_id,
            run_id=run_id,
            trigger_type=ttype,
            payload=payload or {},
        )
        session.add(trigger)
        await session.flush()
        logger.info(
            "autonomy_trigger_recorded type=%s owner_id=%s run_id=%s",
            ttype,
            owner_id,
            run_id or "-",
        )
        return trigger

    async def list_triggers(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[AutonomyTrigger]:
        stmt = select(AutonomyTrigger).where(AutonomyTrigger.owner_id == owner_id)
        if run_id is not None:
            stmt = stmt.where(AutonomyTrigger.run_id == run_id)
        stmt = stmt.order_by(AutonomyTrigger.created_at.desc()).limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())


__all__ = ["TriggerService"]
