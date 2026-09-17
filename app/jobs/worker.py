"""Job worker and handler registry (M7).

The worker claims jobs, dispatches them to a registered handler, and records
success or failure. Two properties it must have:

  * **A failing handler must never kill the loop.** An exception is recorded on
    the job and the worker continues; otherwise one poison payload takes down
    all asynchronous work in the process.
  * **A hung handler must not hold the queue.** Each job runs under a timeout;
    on timeout the attempt fails and the job is retried with backoff. Combined
    with the lease, that means a wedged job cannot block the worker for ever.

Graceful shutdown: on stop, the worker finishes the job in flight and stops
claiming new ones, so a deploy does not abandon a half-done unit of work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.jobs.models import Job, JobState
from app.jobs.queue import JobQueue
from app.observability import JOB_OUTCOMES, JOB_QUEUE_DEPTH

logger = logging.getLogger("nexus.jobs.worker")

#: A handler receives the claimed job and does the work. It MUST be idempotent:
#: a crash between the side effect and the completion record means the job runs
#: again after its lease expires.
JobHandler = Callable[[Job], Awaitable[None]]


class JobHandlerError(Exception):
    """Raised by a handler for a failure that should be retried."""


class PermanentJobError(JobHandlerError):
    """Raised for a failure that will never succeed (bad payload, missing
    entity). Dead-letters immediately instead of burning retries."""


class UnknownJobKindError(PermanentJobError):
    """No handler is registered for this job kind."""


@dataclass
class CircuitBreaker:
    """Stops hammering a dependency that is clearly down.

    Without one, every job in a backlog independently discovers the same outage
    and piles retries onto it. The breaker opens after ``failure_threshold``
    consecutive failures, then refuses calls for ``reset_timeout`` seconds;
    while open, jobs are requeued with backoff rather than attempted, so the
    dependency gets room to recover.
    """

    name: str
    failure_threshold: int = 5
    reset_timeout: float = 30.0
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_timeout:
            # Half-open: allow a probe through.
            return False
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            if self._opened_at is None:
                logger.error(
                    "circuit_breaker_opened name=%s after %d consecutive failures",
                    self.name,
                    self._consecutive_failures,
                )
            self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self.is_open:
            return "open"
        return "half_open"


class JobWorker:
    """Claims and runs jobs registered in a :class:`JobRegistry`."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        registry: "JobRegistry",
        kinds: list[str] | None = None,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 5,
        lease_seconds: float = 300.0,
        job_timeout_seconds: float = 120.0,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._kinds = kinds
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._job_timeout = job_timeout_seconds
        self._breaker = circuit_breaker or CircuitBreaker(name="jobs")
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._processed = 0
        self._failed = 0

    @property
    def is_running(self) -> bool:
        """Whether the claim loop is alive *and* not shutting down (M11).

        Used by `/readyz`. The distinction matters: a worker that has been asked
        to stop is draining its last job and must not be advertised as ready,
        and a worker whose task died (an unexpected exception) must not either -
        a queue nobody is claiming means approvals never advance, which looks
        like a hung product rather than a crashed component.
        """
        return (
            self._task is not None
            and not self._task.done()
            and not self._stopping
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "worker_id": self._queue.worker_id,
            "processed": self._processed,
            "failed": self._failed,
            "circuit_state": self._breaker.state,
            "kinds": sorted(self._kinds or self._registry.kinds),
        }

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "job_worker_started id=%s kinds=%s",
            self._queue.worker_id,
            sorted(self._kinds or self._registry.kinds),
        )

    async def stop(self, *, drain_seconds: float = 10.0) -> None:
        """Stop claiming new jobs and let the in-flight one finish."""
        self._stopping = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=drain_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        logger.info("job_worker_stopped id=%s", self._queue.worker_id)

    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                ran = await self.run_once()
                if not ran:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                # The loop must survive anything a handler or the database does.
                logger.exception("job_worker_loop_error detail=%s", exc)
                await asyncio.sleep(self._poll_interval)

    async def run_once(self) -> int:
        """Claim and run one batch. Returns how many jobs ran.

        Used directly by tests, and by the loop.
        """
        # Recover jobs whose worker died before running anything.
        await self._queue.requeue_expired_leases()

        # Observed before claiming: this is the number that says "is the queue
        # backing up", which is the signal to add a worker. Sampling after the
        # batch would read zero exactly when the worker is keeping up, which is
        # the only time the answer is uninteresting.
        try:
            depth = await self._queue.count_pending(kinds=self._kinds)
            JOB_QUEUE_DEPTH.observe(float(depth), status="pending")
        except Exception as exc:  # noqa: BLE001
            # A metrics sample must never stop the worker from working.
            logger.debug("job_queue_depth_unavailable detail=%s", exc)

        if self._breaker.is_open:
            logger.warning(
                "job_worker_paused circuit=%s state=%s",
                self._breaker.name,
                self._breaker.state,
            )
            return 0

        jobs = await self._queue.claim(
            kinds=self._kinds,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        for job in jobs:
            if self._stopping:
                # Leave it claimable rather than half-done: release the lease.
                await self._queue.fail(
                    job.id, "worker stopping", retry_in_seconds=0.0
                )
                continue
            await self.run_job(job)
        return len(jobs)

    async def run_job(self, job: Job) -> None:
        """Run ONE job, applying the timeout and recording the outcome.

        Every outcome is counted (M11). The series are distinct on purpose: a
        rising `dead_letter` count and a rising `retry` count are different
        incidents (a bad deploy versus a flaky dependency), and a single
        `nexus_jobs_failed_total` would conflate them.
        """
        handler = self._registry.get(job.kind)
        if handler is None:
            # Unknown kind is permanent: retrying will never help.
            await self._queue.dead_letter(
                job.id, f"No handler registered for job kind {job.kind!r}"
            )
            self._failed += 1
            JOB_OUTCOMES.inc(1, kind=job.kind, outcome="no_handler")
            return

        try:
            await asyncio.wait_for(handler(job), timeout=self._job_timeout)
        except asyncio.TimeoutError:
            self._failed += 1
            self._breaker.record_failure()
            await self._queue.fail(
                job.id,
                f"Handler timed out after {self._job_timeout:g}s",
            )
            JOB_OUTCOMES.inc(1, kind=job.kind, outcome="timeout")
            logger.error(
                "job_timeout id=%s kind=%s after=%.1fs",
                job.id,
                job.kind,
                self._job_timeout,
            )
            return
        except PermanentJobError as exc:
            self._failed += 1
            # Dead-letter immediately: retrying cannot help, so burn no budget.
            await self._queue.dead_letter(
                job.id, f"{type(exc).__name__}: {exc}"
            )
            JOB_OUTCOMES.inc(1, kind=job.kind, outcome="dead_letter")
            return
        except Exception as exc:  # noqa: BLE001
            self._failed += 1
            self._breaker.record_failure()
            await self._queue.fail(job.id, f"{type(exc).__name__}: {exc}")
            JOB_OUTCOMES.inc(1, kind=job.kind, outcome="retry")
            return

        self._processed += 1
        self._breaker.record_success()
        await self._queue.complete(job.id)
        JOB_OUTCOMES.inc(1, kind=job.kind, outcome="completed")
        logger.debug("job_succeeded id=%s kind=%s", job.id, job.kind)


class JobRegistry:
    """Maps a job kind to its handler.

    Registration is explicit so an enqueued kind with no handler is a startup
    or test failure rather than a job that dead-letters in production.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, kind: str, handler: JobHandler) -> None:
        if not kind:
            raise ValueError("Job kind must be a non-empty string.")
        if kind in self._handlers:
            raise ValueError(f"Handler for job kind {kind!r} is already registered.")
        self._handlers[kind] = handler

    def get(self, kind: str) -> JobHandler | None:
        return self._handlers.get(kind)

    @property
    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def __contains__(self, kind: object) -> bool:
        return kind in self._handlers


__all__ = [
    "CircuitBreaker",
    "JobHandler",
    "JobHandlerError",
    "JobRegistry",
    "JobWorker",
    "PermanentJobError",
    "UnknownJobKindError",
]
