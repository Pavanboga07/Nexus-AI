"""Autonomy run lifecycle states (leaf module).

This enum lives here — not in ``app.autonomy.models`` — so that other
subsystems (e.g. ``app.workflows``) can import it at module top level without
pulling in the SQLAlchemy model layer. It depends on nothing but ``enum``;
keep it that way.
"""

from __future__ import annotations

import enum


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


__all__ = ["RunStatus"]
