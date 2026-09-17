"""M7 regression tests: durable jobs, retries, idempotency, dead-lettering.

Audit findings covered:

  M6   No retries with backoff existed anywhere in the A2A or LLM paths. The
       entire resilience story was "one retry for an empty LLM response", so a
       transient 5xx, a dropped connection or a rate limit was terminal for the
       user's turn.
  --   Nothing carried an idempotency key, so a retried HTTP request could
       double-execute side effects.
  --   Queue overflow and TTL expiry deleted work silently; the same pattern
       applied to asynchronous work: a fire-and-forget task that failed was
       logged and lost, with no record and no retry.
  H6   Crash recovery existed for workflows and autonomy but had nothing to
       drive it, and asynchronous work had no recovery mechanism at all.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import (
    CircuitBreaker,
    JobQueue,
    JobRegistry,
    JobState,
    JobWorker,
    PermanentJobError,
    compute_backoff_seconds,
)


@pytest.fixture
def job_queue(db_session_factory) -> JobQueue:
    return JobQueue(session_factory=db_session_factory, worker_id="test-worker")


# ---------------------------------------------------------------------------
# Idempotency: the property that makes a retried request safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_idempotency_key_enqueues_once(job_queue) -> None:
    """Retrying an enqueue must not create a second unit of work."""
    first = await job_queue.enqueue(
        "workflow.advance",
        {"workflow_id": str(uuid.uuid4())},
        idempotency_key="advance:abc",
    )
    second = await job_queue.enqueue(
        "workflow.advance",
        {"workflow_id": str(uuid.uuid4())},
        idempotency_key="advance:abc",
    )
    assert first is not None
    assert second is None, "the duplicate enqueue must be a no-op, not a second job"
    stats = await job_queue.stats()
    assert stats.get(JobState.PENDING.value) == 1


@pytest.mark.asyncio
async def test_jobs_without_a_key_are_not_deduplicated(job_queue) -> None:
    """PostgreSQL treats NULL as distinct, so only the caller's key dedupes.

    Correct: only the caller knows what "the same work" means, and silently
    collapsing two genuinely different jobs would lose work.
    """
    for _ in range(3):
        assert await job_queue.enqueue("some.job", {"n": 1}) is not None
    assert (await job_queue.stats()).get(JobState.PENDING.value) == 3


# ---------------------------------------------------------------------------
# Claiming: SKIP LOCKED + leases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workers_do_not_claim_the_same_job(db_session_factory) -> None:
    queue_a = JobQueue(session_factory=db_session_factory, worker_id="worker-a")
    queue_b = JobQueue(session_factory=db_session_factory, worker_id="worker-b")
    await queue_a.enqueue("some.job", {})

    claimed_a = await queue_a.claim(limit=1, lease_seconds=60)
    claimed_b = await queue_b.claim(limit=1, lease_seconds=60)
    assert len(claimed_a) == 1
    assert claimed_b == [], "a leased job must not be claimable concurrently"
    assert claimed_a[0].claimed_by == "worker-a"
    assert claimed_a[0].attempt == 1


@pytest.mark.asyncio
async def test_expired_lease_recovers_a_crashed_workers_job(db_session_factory) -> None:
    """A worker that dies mid-job must not strand the work for ever."""
    queue = JobQueue(session_factory=db_session_factory, worker_id="worker-a")
    await queue.enqueue("some.job", {})

    await queue.claim(limit=1, lease_seconds=60)
    # Still leased: nobody else may take it.
    assert await queue.claim(limit=1, lease_seconds=60) == []

    # Simulate the lease ageing out.
    requeued = await queue.requeue_expired_leases()
    assert requeued == 0, "the lease has not expired yet"

    from sqlalchemy import update

    from app.jobs.models import Job

    async with db_session_factory() as session:
        await session.execute(
            update(Job).values(
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
        await session.commit()

    assert await queue.requeue_expired_leases() == 1
    reclaimed = await queue.claim(limit=1, lease_seconds=60)
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt == 2, "a recovery counts as another attempt"


@pytest.mark.asyncio
async def test_claim_respects_run_after(job_queue) -> None:
    """A job scheduled for the future must not run early (backoff honoured)."""
    await job_queue.enqueue(
        "some.job", {}, run_after=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert await job_queue.claim(limit=5) == []


# ---------------------------------------------------------------------------
# Retry with backoff, then dead-letter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_requeues_with_backoff_then_dead_letters(job_queue) -> None:
    """A transient failure is retried; an exhausted budget is dead-lettered.

    Dead-lettering is what stops a poison job from being retried for ever, and
    ``error_history`` is what makes it diagnosable afterwards.
    """
    job = await job_queue.enqueue("some.job", {}, max_attempts=3)
    assert job is not None

    for expected_attempt in (1, 2):
        claimed = await job_queue.claim(limit=1)
        assert len(claimed) == 1
        assert claimed[0].attempt == expected_attempt
        updated = await job_queue.fail(claimed[0].id, f"boom {expected_attempt}")
        assert updated is not None
        assert updated.state == JobState.PENDING.value, "still retryable"
        assert updated.run_after > datetime.now(timezone.utc), "backed off"
        # Make it runnable now instead of waiting out the backoff.
        from sqlalchemy import update

        from app.jobs.models import Job

        async with job_queue._session_factory() as session:
            await session.execute(
                update(Job).values(run_after=datetime.now(timezone.utc))
            )
            await session.commit()

    # Third attempt exhausts the budget.
    claimed = await job_queue.claim(limit=1)
    assert claimed[0].attempt == 3
    final = await job_queue.fail(claimed[0].id, "boom 3")
    assert final is not None
    assert final.state == JobState.DEAD_LETTER.value
    assert final.finished_at is not None
    assert final.error_history and len(final.error_history) == 3
    assert (await job_queue.stats()).get(JobState.DEAD_LETTER.value) == 1
    # And it is no longer claimable.
    assert await job_queue.claim(limit=5) == []


@pytest.mark.asyncio
async def test_completion_clears_the_lease_and_the_error(job_queue) -> None:
    job = await job_queue.enqueue("some.job", {})
    assert job is not None
    claimed = await job_queue.claim(limit=1)
    await job_queue.complete(claimed[0].id)

    stored = await job_queue.get(claimed[0].id)
    assert stored is not None
    assert stored.state == JobState.SUCCEEDED.value
    assert stored.lease_expires_at is None
    assert stored.claimed_by is None
    assert stored.last_error is None


def test_backoff_is_exponential_capped_and_jittered() -> None:
    """Jitter prevents a synchronised retry stampede after an outage."""
    import random

    rng_a = random.Random(1)
    rng_b = random.Random(2)
    base = [
        compute_backoff_seconds(i, base_seconds=1.0, jitter_ratio=0.0)
        for i in range(1, 5)
    ]
    assert base == [1.0, 2.0, 4.0, 8.0], base

    # Capped regardless of attempt count.
    assert compute_backoff_seconds(100) <= 300.0

    # Jitter actually varies the delay for the same attempt.
    jittered = [
        compute_backoff_seconds(4, rng=random.Random(seed)) for seed in range(12)
    ]
    assert len(set(round(v, 4) for v in jittered)) > 1
    # But stays within the +/- jitter band.
    for value in jittered:
        assert 8.0 * 0.75 - 0.01 <= value <= 8.0 * 1.25 + 0.01


# ---------------------------------------------------------------------------
# Worker behaviour: a bad handler must not take down the loop
# ---------------------------------------------------------------------------


def _worker(queue: JobQueue, registry: JobRegistry, **kw) -> JobWorker:
    return JobWorker(
        queue=queue,
        registry=registry,
        poll_interval_seconds=0.01,
        batch_size=5,
        job_timeout_seconds=kw.pop("job_timeout_seconds", 5.0),
        **kw,
    )


@pytest.mark.asyncio
async def test_worker_runs_a_job_to_completion(job_queue) -> None:
    seen: list[uuid.UUID] = []

    async def handler(job) -> None:
        seen.append(job.id)

    registry = JobRegistry()
    registry.register("demo", handler)
    job = await job_queue.enqueue("demo", {"x": 1})
    assert job is not None

    worker = _worker(job_queue, registry)
    assert await worker.run_once() == 1
    assert seen == [job.id]
    stored = await job_queue.get(job.id)
    assert stored is not None and stored.state == JobState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_failing_handler_is_recorded_and_does_not_kill_the_worker(
    job_queue,
) -> None:
    attempts: list[int] = []

    async def flaky(job) -> None:
        attempts.append(job.attempt)
        raise RuntimeError("transient downstream failure")

    async def healthy(job) -> None:
        attempts.append(-1)

    registry = JobRegistry()
    registry.register("flaky", flaky)
    registry.register("healthy", healthy)

    await job_queue.enqueue("flaky", {}, max_attempts=2)
    await job_queue.enqueue("healthy", {})

    worker = _worker(job_queue, registry)
    await worker.run_once()

    # The flaky job was recorded as a failure, and the healthy job still ran.
    assert -1 in attempts, "one failing job must not stop the others"
    flaky_stats = await job_queue.stats()
    assert flaky_stats.get(JobState.PENDING.value) == 1  # requeued, retryable
    assert worker.stats["failed"] == 1


@pytest.mark.asyncio
async def test_unregistered_job_kind_dead_letters_immediately(job_queue) -> None:
    """An unknown kind can never succeed, so retrying it is pure noise."""
    registry = JobRegistry()  # no handlers
    job = await job_queue.enqueue("mystery.job", {})
    assert job is not None

    worker = _worker(job_queue, registry)
    await worker.run_once()

    stored = await job_queue.get(job.id)
    assert stored is not None
    assert stored.state == JobState.DEAD_LETTER.value
    assert "No handler registered" in (stored.last_error or "")


@pytest.mark.asyncio
async def test_permanent_error_dead_letters_without_burning_retries(
    job_queue,
) -> None:
    async def broken(job) -> None:
        raise PermanentJobError("payload references a deleted workflow")

    registry = JobRegistry()
    registry.register("demo", broken)
    job = await job_queue.enqueue("demo", {}, max_attempts=10)
    assert job is not None

    worker = _worker(job_queue, registry)
    await worker.run_once()

    stored = await job_queue.get(job.id)
    assert stored is not None
    assert stored.state == JobState.DEAD_LETTER.value
    assert stored.attempt == 1, "a permanent error must not consume the retry budget"


@pytest.mark.asyncio
async def test_hung_handler_times_out_and_is_retried(job_queue) -> None:
    """A wedged handler must not hold the worker or the queue for ever."""
    import asyncio

    async def hang(job) -> None:
        await asyncio.sleep(5)

    registry = JobRegistry()
    registry.register("demo", hang)
    job = await job_queue.enqueue("demo", {})
    assert job is not None

    worker = _worker(job_queue, registry, job_timeout_seconds=0.05)
    await worker.run_once()

    stored = await job_queue.get(job.id)
    assert stored is not None
    assert stored.state == JobState.PENDING.value, "timed out, so retryable"
    assert "timed out" in (stored.last_error or "")


@pytest.mark.asyncio
async def test_circuit_breaker_pauses_the_worker(job_queue) -> None:
    """When a dependency is clearly down, stop hammering it."""
    breaker = CircuitBreaker(name="downstream", failure_threshold=1, reset_timeout=60)
    breaker.record_failure()
    assert breaker.is_open

    registry = JobRegistry()

    async def handler(job) -> None:
        raise AssertionError("must not run while the breaker is open")

    registry.register("demo", handler)
    await job_queue.enqueue("demo", {})

    worker = _worker(job_queue, registry, circuit_breaker=breaker)
    assert await worker.run_once() == 0, "the worker must pause, not attempt"
    assert (await job_queue.stats()).get(JobState.PENDING.value) == 1


@pytest.mark.asyncio
async def test_graceful_stop_does_not_abandon_work(job_queue) -> None:
    """Shutdown must leave the job retryable rather than half-done."""
    registry = JobRegistry()

    async def handler(job) -> None:
        raise AssertionError("must not run during shutdown")

    registry.register("demo", handler)
    job = await job_queue.enqueue("demo", {})
    assert job is not None

    worker = _worker(job_queue, registry)
    worker._stopping = True
    await worker.run_once()

    stored = await job_queue.get(job.id)
    assert stored is not None
    assert stored.state == JobState.PENDING.value


def test_registry_rejects_duplicate_and_empty_kinds() -> None:
    """An ambiguous registration must fail at startup, not in production."""
    registry = JobRegistry()

    async def handler(job) -> None:
        return None

    registry.register("a", handler)
    with pytest.raises(ValueError):
        registry.register("a", handler)
    with pytest.raises(ValueError):
        registry.register("", handler)


def test_app_registers_handlers_and_starts_the_worker() -> None:
    """Wiring: the worker must actually be started and drained."""
    import app.main as main_module

    src = inspect.getsource(main_module.lifespan)
    assert "JobWorker" in src
    assert "job_worker.start()" in src
    assert "job_worker.stop()" in src
    # Handler registration is explicit, not implicit by convention.
    assert "registry.register(" in src
