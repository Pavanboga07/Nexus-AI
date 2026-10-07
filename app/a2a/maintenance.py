"""Periodic A2A maintenance: task expiry sweeper.

``TaskStatus.EXPIRED`` was write-only: ``validate_task_active`` raises
``TASK_EXPIRED`` for past-``expires_at`` tasks, but no path ever flipped the
row to EXPIRED, so expired tasks lingered in active states indefinitely.
This module provides the sweeper that transitions them, plus the
start/stop helpers mirroring the episodic-memory retention task in
``app.main``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("nexus.a2a.maintenance")


async def sweep_expired_tasks(session_factory: Any) -> int:
    """Flip past-expires_at, non-terminal tasks to EXPIRED. Returns the count."""
    from app.a2a.repository import TaskRepository

    repo = TaskRepository()
    async with session_factory() as session:
        expired = await repo.expire_overdue_tasks(session)
        await session.commit()
    if expired:
        logger.info("task_expiry_sweep expired=%d", expired)
    return expired


async def _expiry_sweep_loop(
    session_factory: Any, interval_seconds: float
) -> None:
    """Run the expiry sweep every ``interval_seconds`` until cancelled."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await sweep_expired_tasks(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad tick
            logger.warning("task_expiry_sweep_failed detail=%s", exc)


def start_task_expiry_sweeper(
    session_factory: Any, *, interval_seconds: float = 3600.0
) -> asyncio.Task | None:
    """Start the periodic task-expiry sweep; None when there is no DB."""
    if session_factory is None:
        return None
    logger.info(
        "task_expiry_sweeper_enabled interval_seconds=%s", interval_seconds
    )
    return asyncio.create_task(_expiry_sweep_loop(session_factory, interval_seconds))


__all__ = [
    "start_task_expiry_sweeper",
    "sweep_expired_tasks",
]
