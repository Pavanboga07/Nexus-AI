"""Job queue operations: enqueue, claim, complete, fail, reap (M7).

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` with a LEASE rather than a
lock held across the work. A lock held for the duration of a network call would
stall the queue behind one slow job; a lease lets another worker pick the job up
once it expires, which is what bounds the damage of a crashed worker.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.jobs.models import Job, JobState

logger = logging.getLogger("nexus.jobs")


def compute_backoff_seconds(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    factor: float = 2.0,
    max_seconds: float = 300.0,
    jitter_ratio: float = 0.25,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with jitter.

    Jitter is not decoration: without it every job that failed at the same
    moment retries at the same moment, so a downstream outage is followed by a
    synchronised stampede that keeps it down.
    """
    attempt = max(1, int(attempt))
    delay = min(max_seconds, base_seconds * (factor ** (attempt - 1)))
    source = rng or random
    spread = delay * jitter_ratio
    jittered = delay + source.uniform(-spread, spread)
    return max(0.0, min(max_seconds, jittered))


class JobQueue:
    """Enqueue, claim, complete, fail, and requeue durable jobs."""

    def __init__(self, *, session_factory, worker_id: str | None = None) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        owner_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int = 5,
        priority: int = 0,
    ) -> Job | None:
        """Create a job. Returns None when the idempotency key already exists.

        Returning None rather than raising is deliberate: a caller retrying a
        request should treat "already queued" as success, not as a conflict.
        """
        async with self._session_factory() as session:
            stmt = (
                insert(Job)
                .values(
                    id=uuid.uuid4(),
                    kind=kind,
                    owner_id=owner_id,
                    payload=payload or {},
                    idempotency_key=idempotency_key,
                    state=JobState.PENDING.value,
                    attempt=0,
                    max_attempts=max(1, int(max_attempts)),
                    run_after=run_after or datetime.now(timezone.utc),
                    priority=priority,
                )
                # In PostgreSQL a NULL idempotency_key does not conflict, so
                # jobs without a key are never deduplicated - which is correct:
                # only the caller knows what "the same work" means.
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(Job)
            )
            job = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
            if job is None:
                logger.info(
                    "job_deduplicated kind=%s idempotency_key=%s",
                    kind,
                    idempotency_key,
                )
            else:
                logger.debug("job_enqueued id=%s kind=%s", job.id, kind)
            return job

    async def claim(
        self,
        *,
        kinds: list[str] | None = None,
        limit: int = 1,
        lease_seconds: float = 300.0,
    ) -> list[Job]:
        """Lease up to ``limit`` runnable jobs.

        SKIP LOCKED so two workers never claim the same job, and a job whose
        lease has expired (its worker died) becomes claimable again.
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            conditions = [
                Job.state.in_([JobState.PENDING.value, JobState.RUNNING.value]),
                Job.run_after <= now,
                or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now),
            ]
            if kinds:
                conditions.append(Job.kind.in_(kinds))

            stmt = (
                select(Job)
                .where(*conditions)
                .order_by(Job.priority.desc(), Job.run_after)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            jobs = list((await session.execute(stmt)).scalars().all())

            lease_until = now + timedelta(seconds=lease_seconds)
            for job in jobs:
                job.state = JobState.RUNNING.value
                job.attempt += 1
                job.claimed_by = self._worker_id
                job.lease_expires_at = lease_until
                job.started_at = job.started_at or now
            await session.commit()
            for job in jobs:
                session.expunge(job)
            return jobs

    async def complete(self, job_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            job.state = JobState.SUCCEEDED.value
            job.finished_at = datetime.now(timezone.utc)
            job.lease_expires_at = None
            job.claimed_by = None
            job.last_error = None
            await session.commit()

    async def dead_letter(self, job_id: uuid.UUID, reason: str) -> Job | None:
        """Move a job straight to the dead-letter state, skipping retries.

        Used for failures that can NEVER succeed (an unregistered job kind, a
        payload referencing something that no longer exists). Retrying those
        burns the budget, delays the inevitable, and buries the real error
        under N copies of the same message.
        """
        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return None
            job.state = JobState.DEAD_LETTER.value
            job.last_error = reason[:2000]
            history = list(job.error_history or [])
            history.append({"attempt": job.attempt, "error": reason[:300]})
            job.error_history = history[-10:]
            job.finished_at = datetime.now(timezone.utc)
            job.lease_expires_at = None
            job.claimed_by = None
            await session.commit()
            logger.error(
                "job_dead_lettered id=%s kind=%s attempts=%d reason=%s",
                job.id,
                job.kind,
                job.attempt,
                job.last_error,
            )
            session.expunge(job)
            return job

    async def fail(
        self,
        job_id: uuid.UUID,
        error: str,
        *,
        retry_in_seconds: float | None = None,
    ) -> Job | None:
        """Record a failed attempt, then requeue with backoff or dead-letter.

        Dead-lettering once attempts are exhausted is what stops a poison job
        (a handler that always throws, a payload that can never succeed) from
        being retried for ever.
        """
        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return None

            job.last_error = error[:2000]
            history = list(job.error_history or [])
            history.append({"attempt": job.attempt, "error": error[:300]})
            job.error_history = history[-10:]

            if job.attempt >= job.max_attempts:
                job.state = JobState.DEAD_LETTER.value
                job.finished_at = datetime.now(timezone.utc)
                job.lease_expires_at = None
                job.claimed_by = None
                await session.commit()
                logger.error(
                    "job_dead_lettered id=%s kind=%s attempts=%d last_error=%s",
                    job.id,
                    job.kind,
                    job.attempt,
                    job.last_error,
                )
                session.expunge(job)
                return job

            # NOTE: compare against None explicitly. `if retry_in_seconds:` would
            # treat 0.0 ("retry immediately", used for permanent failures) as
            # unset and silently apply exponential backoff instead.
            delay = (
                retry_in_seconds
                if retry_in_seconds is not None
                else compute_backoff_seconds(job.attempt)
            )
            job.state = JobState.PENDING.value
            job.run_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
            job.lease_expires_at = None
            job.claimed_by = None
            await session.commit()
            logger.warning(
                "job_retry_scheduled id=%s kind=%s attempt=%d/%d in=%.2fs error=%s",
                job.id,
                job.kind,
                job.attempt,
                job.max_attempts,
                delay,
                job.last_error,
            )
            session.expunge(job)
            return job

    async def requeue_expired_leases(self) -> int:
        """Return jobs with an expired lease to PENDING.

        A worker that died mid-job leaves a RUNNING row with a stale lease;
        without this the row would sit RUNNING for ever.
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            jobs = list(
                (
                    await session.execute(
                        select(Job)
                        .where(
                            Job.state == JobState.RUNNING.value,
                            Job.lease_expires_at.is_not(None),
                            Job.lease_expires_at < now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
            )
            for job in jobs:
                job.state = JobState.PENDING.value
                job.lease_expires_at = None
                job.claimed_by = None
            if jobs:
                await session.commit()
                logger.warning("requeued %d job(s) with expired leases", len(jobs))
            return len(jobs)

    async def count_pending(self, kinds: list[str] | None = None) -> int:
        """Jobs claimable right now, optionally restricted to some kinds (M11).

        Used as the queue-depth metric sample. `kinds` is passed so a worker
        registered for one kind reports the backlog it is actually responsible
        for, rather than a total it cannot drain - which would make the metric
        look like a worker failure.
        """
        statement = (
            select(func.count())
            .select_from(Job)
            .where(
                Job.state == JobState.PENDING.value,
                Job.run_after <= datetime.now(timezone.utc),
            )
        )
        if kinds:
            statement = statement.where(Job.kind.in_(kinds))
        async with self._session_factory() as session:
            return int((await session.execute(statement)).scalar_one())

    async def stats(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Job.state, func.count()).group_by(Job.state)
                )
            ).all()
            return {state: int(count) for state, count in rows}

    async def get(self, job_id: uuid.UUID) -> Job | None:
        async with self._session_factory() as session:
            return await session.get(Job, job_id)

    async def list_for_owner(
        self, owner_id: uuid.UUID, *, limit: int = 100
    ) -> list[Job]:
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(Job)
                        .where(Job.owner_id == owner_id)
                        .order_by(Job.created_at.desc())
                        .limit(limit)
                    )
                ).scalars().all()
            )


__all__ = ["JobQueue", "compute_backoff_seconds"]
