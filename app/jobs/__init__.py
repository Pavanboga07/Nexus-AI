"""Durable job queue, worker, and handler registry (M7).

    model    the `jobs` table and its state machine
    queue    enqueue / claim / complete / fail / reap
    worker   claim loop, timeouts, circuit breaker, graceful shutdown

Handlers are registered explicitly by the app at startup, so an enqueued job
kind with no handler is a startup/test failure rather than something that
dead-letters in production.
"""

from app.jobs.models import TERMINAL_STATES, Job, JobState
from app.jobs.queue import JobQueue, compute_backoff_seconds
from app.jobs.worker import (
    CircuitBreaker,
    JobHandler,
    JobHandlerError,
    JobRegistry,
    JobWorker,
    PermanentJobError,
    UnknownJobKindError,
)

__all__ = [
    "CircuitBreaker",
    "Job",
    "JobHandler",
    "JobHandlerError",
    "JobQueue",
    "JobRegistry",
    "JobState",
    "JobWorker",
    "PermanentJobError",
    "TERMINAL_STATES",
    "UnknownJobKindError",
    "compute_backoff_seconds",
]
