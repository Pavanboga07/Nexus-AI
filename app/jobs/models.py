"""Durable job model (M7).

Why a job table exists at all: before M7 there was no durable asynchronous work
and no retry machinery anywhere in the codebase. Work happened inline in a
request, or in a fire-and-forget ``asyncio.create_task`` whose failure was
logged and lost. The concrete consequences the audit found:

  * a retried HTTP request could double-execute side effects, because nothing
    carried an idempotency key,
  * a transient failure (a timeout, a peer 500) was terminal - there was no
    retry with backoff in the A2A or LLM paths,
  * "one retry for an empty LLM response" was the entire resilience story,
  * and a workflow interrupted mid-run had no way to resume.

PostgreSQL rather than a broker (decision D6): ``SKIP LOCKED`` gives safe
concurrent claiming, the job row is transactionally consistent with the
workflow/autonomy state it acts on, and self-hosting stays one
``docker compose up``. A broker would add an operational dependency and a
second source of truth without adding a guarantee we need.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


class JobState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


#: Terminal states - a job here will not be retried.
TERMINAL_STATES = frozenset(
    {JobState.SUCCEEDED.value, JobState.DEAD_LETTER.value}
)


class Job(Base):
    """A unit of durable asynchronous work.

    Contract for handlers (stated, not assumed): a job may run more than once.
    A worker can crash after the side effect and before the completion is
    recorded, and the lease will expire. Handlers MUST therefore be idempotent,
    and callers SHOULD supply an ``idempotency_key`` so the same logical work
    is only enqueued once.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Enqueueing with the same key twice must not create two jobs. This is
        # what makes a retried request safe: the caller derives the key from
        # the work it wants done exactly once.
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        Index("ix_jobs_claimable", "state", "run_after"),
        Index("ix_jobs_kind_state", "kind", "state"),
        Index("ix_jobs_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        # Generated in Python, not by a database function: the queue always
        # supplies an explicit id, and a server-side ``gen_random_uuid()`` is
        # PostgreSQL-only (it makes the model unusable against SQLite, which
        # parts of the test suite use).
        default=uuid.uuid4,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: NULL means "any tenant" - reserved for system jobs (reaping, refresh).
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Deduplication key. Two enqueues with the same key produce one job.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobState.PENDING.value
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    #: Earliest time this job may run (the backoff target).
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Lease held while a worker runs it; expiry makes it claimable again.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Recent error class names, newest last (diagnostics).
    error_history: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "state": self.state,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "run_after": self.run_after.isoformat() if self.run_after else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


__all__ = ["Job", "JobState", "TERMINAL_STATES"]
