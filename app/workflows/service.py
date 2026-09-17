"""WorkflowService: Durable orchestration engine for multi-step workflows (Part 9).

Security & Architectural Invariants:
1. Every step is independently evaluated against PolicyService before execution.
2. Step 1 ALLOW does NOT grant Step 2 ALLOW.
3. No arbitrary code execution; workflows execute registered handlers only.
4. Database is the durable source of truth (no in-memory-only state).
5. Concurrency protection prevents dual-worker step pickup via row-level locks.
6. Transient errors retry up to max_attempts; security failures fail immediately.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.queue import JobQueue
from app.policy.engine import EvaluationRequest
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService
from app.schemas.workflows import WorkflowStepSpec
from app.workflows.errors import (
    WorkflowAccessDeniedError,
    WorkflowConflictError,
    WorkflowExpiredError,
    WorkflowNotFoundError,
)
from app.workflows.handlers import (
    BaseWorkflowStepHandler,
    StepResult,
    WorkflowStepContext,
    WorkflowStepHandlerRegistry,
    _slugify,
    build_default_step_registry,
)
from app.workflows.models import (
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
)
from app.workflows.repository import (
    WorkflowRepository,
    WorkflowStepRepository,
)

logger = logging.getLogger("nexus.workflows.service")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        policy_service: PolicyService,
        registry: WorkflowStepHandlerRegistry | None = None,
        tool_service: Any = None,
        a2a_service: Any = None,
        memory_manager: Any = None,
        identity_service: Any = None,
        default_ttl_seconds: int = 3600,
        max_step_attempts: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._policy = policy_service
        self._registry = registry or build_default_step_registry()
        self._tool_service = tool_service
        self._a2a_service = a2a_service
        self._memory_manager = memory_manager
        self._identity_service = identity_service
        self._default_ttl_seconds = default_ttl_seconds
        self._max_step_attempts = max_step_attempts
        self._repo = WorkflowRepository()
        self._step_repo = WorkflowStepRepository()

    def _audit(
        self,
        event: str,
        *,
        workflow_id: uuid.UUID | str,
        step_id: uuid.UUID | str | None = None,
        task_id: str | None = None,
        owner_id: uuid.UUID | str | None = None,
        status: str | None = None,
        purpose: str | None = None,
        policy_decision: str | None = None,
        detail: str | None = None,
    ) -> None:
        logger.info(
            "workflow_audit event=%s workflow_id=%s step_id=%s task_id=%s "
            "owner_id=%s status=%s purpose=%s policy_decision=%s detail=%s",
            event,
            workflow_id,
            step_id or "-",
            task_id or "-",
            owner_id or "-",
            status or "-",
            purpose or "-",
            policy_decision or "-",
            detail or "-",
        )

    # --- Creation & Retrieval (owner-scoped) -----------------------------------

    async def create_workflow(
        self,
        owner_id: uuid.UUID,
        *,
        workflow_type: str,
        purpose: str,
        steps: list[WorkflowStepSpec],
        context_data: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> Workflow:
        if not steps:
            raise ValueError("A workflow must contain at least one step.")

        ttl = ttl_seconds or self._default_ttl_seconds
        expires_at = _utcnow() + timedelta(seconds=ttl)

        workflow = Workflow(
            workflow_id=uuid.uuid4(),
            owner_id=owner_id,
            workflow_type=workflow_type,
            purpose=purpose,
            status=WorkflowStatus.PENDING.value,
            current_step_number=0,
            context_data=context_data or {},
            workflow_metadata={},
            expires_at=expires_at,
        )

        step_models: list[WorkflowStep] = []
        for idx, step_spec in enumerate(steps, start=1):
            step_models.append(
                WorkflowStep(
                    step_id=uuid.uuid4(),
                    workflow_id=workflow.workflow_id,
                    step_number=idx,
                    step_type=step_spec.step_type,
                    status=StepStatus.PENDING.value,
                    input_payload=step_spec.input_payload or {},
                    attempt_count=0,
                    max_attempts=step_spec.max_attempts or self._max_step_attempts,
                )
            )

        async with self._session_factory() as session:
            await self._repo.create(session, workflow)
            await self._step_repo.create_steps(session, step_models)
            await session.commit()

        self._audit(
            "workflow_created",
            workflow_id=workflow.workflow_id,
            owner_id=owner_id,
            status=workflow.status,
            purpose=purpose,
        )

        async with self._session_factory() as session:
            created = await self._repo.get(session, workflow.workflow_id)
            assert created is not None
            return created

    async def get_workflow(
        self, owner_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> Workflow:
        async with self._session_factory() as session:
            wf = await self._repo.get_for_owner(session, owner_id, workflow_id)
            if wf is None:
                raise WorkflowNotFoundError(f"Workflow {workflow_id} not found.")
            if await self._expire_if_overdue(session, wf):
                return wf
            return wf

    async def list_workflows(
        self,
        owner_id: uuid.UUID,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[Workflow]:
        async with self._session_factory() as session:
            wfs = await self._repo.list_for_owner(
                session, owner_id, status=status, limit=limit
            )
            for wf in wfs:
                await self._expire_if_overdue(session, wf)
            return wfs

    # --- Lifecycle Operations -------------------------------------------------

    async def start_workflow(
        self, owner_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> Workflow:
        async with self._session_factory() as session:
            wf = await self._repo.get_for_owner(session, owner_id, workflow_id)
            if wf is None:
                raise WorkflowNotFoundError(f"Workflow {workflow_id} not found.")

            if wf.status != WorkflowStatus.PENDING.value:
                raise WorkflowConflictError(
                    f"Workflow cannot be started from status: {wf.status}"
                )

            now = _utcnow()
            if wf.expires_at <= now:
                wf.status = WorkflowStatus.EXPIRED.value
                wf.failure_reason = "Workflow expired before start"
                await session.commit()
                self._audit(
                    "workflow_expired",
                    workflow_id=workflow_id,
                    owner_id=owner_id,
                    status=wf.status,
                    purpose=wf.purpose,
                )
                raise WorkflowExpiredError("Workflow has already expired.")

            wf.status = WorkflowStatus.RUNNING.value
            await session.commit()

        self._audit(
            "workflow_started",
            workflow_id=workflow_id,
            owner_id=owner_id,
            status=WorkflowStatus.RUNNING.value,
            purpose=wf.purpose,
        )

        await self._enqueue_advance(workflow_id)
        return await self._get_wf(workflow_id)

    async def _enqueue_advance(self, workflow_id: uuid.UUID) -> None:
        """Defer advancement to the jobs worker instead of running it inline.

        The payload shape matches the ``workflow.advance`` handler registered
        in ``app/main.py`` verbatim. The worker (or the test drain helper)
        runs :meth:`advance_workflow`; a crash between here and the job run
        leaves a RUNNING workflow that ``recover_interrupted_workflows``
        resumes, so advancement survives restarts.
        """
        queue = JobQueue(session_factory=self._session_factory)
        await queue.enqueue("workflow.advance", {"workflow_id": str(workflow_id)})

    async def _expire_if_overdue(self, session: AsyncSession, wf: Workflow) -> bool:
        """Expire the workflow if past its TTL. Returns True when expired.

        Performs check + expire + commit + audit. Callers return early when
        True. Uses the same predicate and audit event as advance_workflow.
        """
        now = _utcnow()
        if wf.expires_at <= now and wf.status in {
            WorkflowStatus.RUNNING.value,
            WorkflowStatus.PENDING.value,
            WorkflowStatus.WAITING_APPROVAL.value,
            WorkflowStatus.WAITING_REMOTE.value,
        }:
            wf.status = WorkflowStatus.EXPIRED.value
            wf.failure_reason = "Workflow expired"
            for s in wf.steps or []:
                if s.status in {StepStatus.PENDING.value, StepStatus.WAITING.value}:
                    s.status = StepStatus.SKIPPED.value
                elif s.status == StepStatus.RUNNING.value:
                    s.status = StepStatus.FAILED.value
                    s.failure_reason = "parent expired while step running"
                    s.completed_at = now
            await session.commit()
            self._audit(
                "workflow_expired",
                workflow_id=wf.workflow_id,
                owner_id=wf.owner_id,
                status=wf.status,
                purpose=wf.purpose,
            )
            return True
        return False

    async def advance_workflow(self, workflow_id: uuid.UUID) -> Workflow:
        """Advance the workflow through pending steps until completion, pause, or failure."""
        while True:
            async with self._session_factory() as session:
                wf = await self._repo.get(session, workflow_id)
                if wf is None:
                    raise WorkflowNotFoundError(f"Workflow {workflow_id} not found.")

                now = _utcnow()
                if await self._expire_if_overdue(session, wf):
                    return wf

                if wf.status != WorkflowStatus.RUNNING.value:
                    return wf

                # Concurrency lock: acquire next pending step using SELECT ... FOR UPDATE
                step = await self._step_repo.acquire_next_pending_step(
                    session, workflow_id
                )

                if step is None:
                    # Check if any step is in WAITING status
                    steps = await self._step_repo.list_for_workflow(session, workflow_id)
                    has_waiting = any(
                        s.status == StepStatus.WAITING.value for s in steps
                    )
                    if has_waiting:
                        # Workflow remains in WAITING state
                        return wf

                    # Check if all steps completed
                    all_done = all(
                        s.status in {StepStatus.COMPLETED.value, StepStatus.SKIPPED.value}
                        for s in steps
                    )
                    if all_done:
                        wf.status = WorkflowStatus.COMPLETED.value
                        wf.completed_at = now
                        await session.commit()
                        self._audit(
                            "workflow_completed",
                            workflow_id=workflow_id,
                            owner_id=wf.owner_id,
                            status=wf.status,
                            purpose=wf.purpose,
                        )
                        return wf

                    # If some step is failed
                    has_failed = any(
                        s.status == StepStatus.FAILED.value for s in steps
                    )
                    if has_failed:
                        wf.status = WorkflowStatus.FAILED.value
                        wf.completed_at = now
                        await session.commit()
                        self._audit(
                            "workflow_completed",
                            workflow_id=workflow_id,
                            owner_id=wf.owner_id,
                            status=wf.status,
                            purpose=wf.purpose,
                        )
                        return wf

                    return wf

                # Sequentiality guard: another advancer may already own a
                # RUNNING step (handler runs outside the transaction). If so,
                # roll back the pending-step lock and return.
                running_steps = (
                    await self._step_repo.find_running_steps_for_workflow(
                        session, workflow_id
                    )
                )
                if running_steps:
                    await session.rollback()
                    return wf

                # Mark step RUNNING durably
                step.status = StepStatus.RUNNING.value
                step.started_at = now
                wf.current_step_number = step.step_number
                await session.commit()

            self._audit(
                "step_started",
                workflow_id=workflow_id,
                step_id=step.step_id,
                owner_id=wf.owner_id,
                purpose=wf.purpose,
            )

            # Execution context
            context = WorkflowStepContext(
                owner_id=wf.owner_id,
                workflow_id=workflow_id,
                step_id=step.step_id,
                step_number=step.step_number,
                purpose=wf.purpose,
                workflow_context=wf.context_data or {},
                memory_manager=self._memory_manager,
                policy_service=self._policy,
                tool_service=self._tool_service,
                a2a_service=self._a2a_service,
                identity_service=self._identity_service,
            )

            handler = self._registry.get(step.step_type)
            if handler is None:
                async with self._session_factory() as session:
                    await self._fail_step_and_workflow(
                        session,
                        workflow_id,
                        step.step_id,
                        f"Unsupported step type: {step.step_type}",
                    )
                    await session.commit()
                return await self._get_wf(workflow_id)

            # --- Independent Policy Check ---
            eval_req = handler.get_evaluation_request(
                context, step.input_payload or {}
            )
            eval_result = await self._policy.evaluate(wf.owner_id, eval_req)

            if eval_result.decision is PolicyDecision.DENY:
                async with self._session_factory() as session:
                    await self._fail_step_and_workflow(
                        session,
                        workflow_id,
                        step.step_id,
                        f"Policy DENY: {eval_result.reason}",
                    )
                    await session.commit()
                self._audit(
                    "step_failed",
                    workflow_id=workflow_id,
                    step_id=step.step_id,
                    owner_id=wf.owner_id,
                    purpose=wf.purpose,
                    policy_decision="DENY",
                    detail=eval_result.reason,
                )
                return await self._get_wf(workflow_id)

            if eval_result.decision is PolicyDecision.ASK:
                async with self._session_factory() as session:
                    step_db = await self._step_repo.get(session, step.step_id)
                    wf_db = await self._repo.get(session, workflow_id)
                    if step_db and wf_db:
                        step_db.status = StepStatus.WAITING.value
                        step_db.failure_reason = "Waiting for owner approval"
                        wf_db.status = WorkflowStatus.WAITING_APPROVAL.value
                        await session.commit()
                self._audit(
                    "workflow_waiting",
                    workflow_id=workflow_id,
                    step_id=step.step_id,
                    owner_id=wf.owner_id,
                    purpose=wf.purpose,
                    policy_decision="ASK",
                    detail="Awaiting owner approval",
                )
                return await self._get_wf(workflow_id)

            # Policy ALLOW: Execute handler
            try:
                res: StepResult = await handler.execute(
                    context, step.input_payload or {}
                )
            except Exception as exc:
                logger.exception("Step handler uncaught exception: %s", exc)
                res = StepResult(
                    status=StepStatus.FAILED,
                    failure_reason=str(exc),
                    is_transient=True,
                )

            # Process handler result
            if res.status is StepStatus.COMPLETED:
                async with self._session_factory() as session:
                    step_db = await self._step_repo.get(session, step.step_id)
                    wf_db = await self._repo.get(session, workflow_id)
                    if step_db and wf_db:
                        if wf_db.status in {
                            WorkflowStatus.COMPLETED.value,
                            WorkflowStatus.FAILED.value,
                            WorkflowStatus.CANCELLED.value,
                            WorkflowStatus.EXPIRED.value,
                        }:
                            return await self._get_wf(workflow_id)
                        step_db.status = StepStatus.COMPLETED.value
                        step_db.completed_at = _utcnow()
                        step_db.output_payload = res.output_payload
                        step_db.task_id = res.task_id
                        ctx = dict(wf_db.context_data or {})
                        # Record under step output key and step_type
                        ctx[f"step_{step.step_number}"] = res.output_payload
                        ctx[step.step_type] = res.output_payload
                        wf_db.context_data = ctx
                        await session.commit()
                self._audit(
                    "step_completed",
                    workflow_id=workflow_id,
                    step_id=step.step_id,
                    task_id=res.task_id,
                    owner_id=wf.owner_id,
                    status=StepStatus.COMPLETED.value,
                    purpose=wf.purpose,
                )
                # Continue loop to next step!
                continue

            elif res.status is StepStatus.WAITING:
                async with self._session_factory() as session:
                    step_db = await self._step_repo.get(session, step.step_id)
                    wf_db = await self._repo.get(session, workflow_id)
                    if step_db and wf_db:
                        step_db.status = StepStatus.WAITING.value
                        step_db.task_id = res.task_id
                        step_db.output_payload = res.output_payload
                        wf_db.status = WorkflowStatus.WAITING_REMOTE.value
                        await session.commit()
                self._audit(
                    "workflow_waiting",
                    workflow_id=workflow_id,
                    step_id=step.step_id,
                    task_id=res.task_id,
                    owner_id=wf.owner_id,
                    status=WorkflowStatus.WAITING_REMOTE.value,
                    purpose=wf.purpose,
                    detail="Waiting for remote response",
                )
                return await self._get_wf(workflow_id)

            else:  # FAILED
                if res.is_transient and (step.attempt_count + 1) < step.max_attempts:
                    async with self._session_factory() as session:
                        step_db = await self._step_repo.get(session, step.step_id)
                        if step_db:
                            step_db.attempt_count += 1
                            step_db.status = StepStatus.PENDING.value
                            step_db.failure_reason = (
                                f"Transient error: {res.failure_reason}"
                            )
                            await session.commit()
                    self._audit(
                        "step_retry",
                        workflow_id=workflow_id,
                        step_id=step.step_id,
                        owner_id=wf.owner_id,
                        status=StepStatus.PENDING.value,
                        purpose=wf.purpose,
                        detail=f"Attempt {step.attempt_count + 1} of {step.max_attempts}",
                    )
                    # Retry next step iteration
                    continue
                else:
                    async with self._session_factory() as session:
                        await self._fail_step_and_workflow(
                            session,
                            workflow_id,
                            step.step_id,
                            res.failure_reason or "Step execution failed",
                        )
                        await session.commit()
                    self._audit(
                        "step_failed",
                        workflow_id=workflow_id,
                        step_id=step.step_id,
                        owner_id=wf.owner_id,
                        status=StepStatus.FAILED.value,
                        purpose=wf.purpose,
                        detail=res.failure_reason,
                    )
                    return await self._get_wf(workflow_id)

    async def _fail_step_and_workflow(
        self,
        session: AsyncSession,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        reason: str,
    ) -> None:
        now = _utcnow()
        truncated = reason[:250] if isinstance(reason, str) else reason
        step = await self._step_repo.get(session, step_id)
        if step:
            step.status = StepStatus.FAILED.value
            step.failure_reason = truncated
            step.completed_at = now
        wf = await self._repo.get(session, workflow_id)
        if wf:
            wf.status = WorkflowStatus.FAILED.value
            wf.failure_reason = truncated
            wf.completed_at = now

    async def _get_wf(self, workflow_id: uuid.UUID) -> Workflow:
        async with self._session_factory() as session:
            wf = await self._repo.get(session, workflow_id)
            assert wf is not None
            return wf

    # --- Approval -------------------------------------------------------------

    async def approve_workflow(
        self,
        owner_id: uuid.UUID,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID | None = None,
    ) -> Workflow:
        async with self._session_factory() as session:
            wf = await self._repo.get_for_owner(session, owner_id, workflow_id)
            if wf is None:
                raise WorkflowNotFoundError(f"Workflow {workflow_id} not found.")

            if await self._expire_if_overdue(session, wf):
                return wf

            if wf.status != WorkflowStatus.WAITING_APPROVAL.value:
                raise WorkflowConflictError(
                    f"Workflow is not awaiting approval (current status: {wf.status})"
                )

            # Find waiting step
            target_step: WorkflowStep | None = None
            if step_id is not None:
                target_step = await self._step_repo.get(session, step_id)
                if not target_step or target_step.workflow_id != workflow_id:
                    raise WorkflowNotFoundError(f"Step {step_id} not found.")
                if target_step.status != StepStatus.WAITING.value:
                    raise WorkflowConflictError(
                        f"Step {step_id} is not awaiting approval (current status: {target_step.status})"
                    )
            else:
                steps = await self._step_repo.list_for_workflow(session, workflow_id)
                for s in steps:
                    if s.status == StepStatus.WAITING.value:
                        target_step = s
                        break

            if target_step is None:
                raise WorkflowConflictError("No waiting step found to approve.")

            handler = self._registry.get(target_step.step_type)
            data_cat = handler.data_category if handler else "workflow"
            act = handler.action if handler else "execute_step"
            requester = (
                (target_step.input_payload or {}).get("requester_agent_id")
                or "nexus:self"
            )

            # Create single-use consent to approve this exact step
            purpose_slug = _slugify((target_step.input_payload or {}).get("purpose") or wf.purpose)
            await self._policy.create_consent(
                owner_id,
                requester_agent_id=requester,
                data_category=data_cat,
                action=act,
                purpose=purpose_slug,
                decision="ALLOW",
                single_use=True,
            )

            # Reset step to PENDING and workflow to RUNNING
            target_step.status = StepStatus.PENDING.value
            target_step.failure_reason = None
            wf.status = WorkflowStatus.RUNNING.value
            await session.commit()

        self._audit(
            "workflow_resumed",
            workflow_id=workflow_id,
            step_id=target_step.step_id,
            owner_id=owner_id,
            status=WorkflowStatus.RUNNING.value,
            purpose=wf.purpose,
            detail="Owner approved step",
        )

        await self._enqueue_advance(workflow_id)
        return await self._get_wf(workflow_id)

    # --- Cancellation ---------------------------------------------------------

    async def cancel_workflow(
        self,
        owner_id: uuid.UUID,
        workflow_id: uuid.UUID,
        reason: str = "Cancelled by owner",
    ) -> Workflow:
        async with self._session_factory() as session:
            wf = await self._repo.get_for_owner(session, owner_id, workflow_id)
            if wf is None:
                raise WorkflowNotFoundError(f"Workflow {workflow_id} not found.")

            if wf.status in {
                WorkflowStatus.COMPLETED.value,
                WorkflowStatus.FAILED.value,
                WorkflowStatus.CANCELLED.value,
                WorkflowStatus.EXPIRED.value,
            }:
                return wf

            wf.status = WorkflowStatus.CANCELLED.value
            wf.failure_reason = reason
            now = _utcnow()
            wf.completed_at = now

            steps = await self._step_repo.list_for_workflow(session, workflow_id)
            for s in steps:
                if s.status in {StepStatus.PENDING.value, StepStatus.WAITING.value}:
                    s.status = StepStatus.SKIPPED.value
                elif s.status == StepStatus.RUNNING.value:
                    s.status = StepStatus.FAILED.value
                    s.failure_reason = "parent cancelled while step running"
                    s.completed_at = now

            await session.commit()

        self._audit(
            "workflow_cancelled",
            workflow_id=workflow_id,
            owner_id=owner_id,
            status=WorkflowStatus.CANCELLED.value,
            purpose=wf.purpose,
            detail=reason,
        )

        return await self._get_wf(workflow_id)

    # --- A2A Resumption -------------------------------------------------------

    async def handle_task_completion(
        self, task_id: str, response_payload: dict[str, Any]
    ) -> Workflow | None:
        """Called when an asynchronous A2A task completes to resume the waiting step."""
        workflow_id: uuid.UUID | None = None
        async with self._session_factory() as session:
            step = await self._step_repo.find_step_by_task_id(session, task_id)
            if not step or step.status != StepStatus.WAITING.value:
                return None

            workflow_id = step.workflow_id
            wf = await self._repo.get(session, workflow_id)
            if not wf:
                return None

            # Re-evaluate the step's own policy before ingesting anything: the
            # authorization that allowed the delegation may have changed while
            # the remote task was in flight. Same call the advance loop uses.
            handler = self._registry.get(step.step_type)
            if handler is not None:
                resume_ctx = WorkflowStepContext(
                    owner_id=wf.owner_id,
                    workflow_id=step.workflow_id,
                    step_id=step.step_id,
                    step_number=step.step_number,
                    purpose=wf.purpose,
                    workflow_context=dict(wf.context_data or {}),
                    memory_manager=self._memory_manager,
                    policy_service=self._policy,
                    tool_service=self._tool_service,
                    a2a_service=self._a2a_service,
                    identity_service=self._identity_service,
                )
                resume_eval = await self._policy.evaluate(
                    wf.owner_id,
                    handler.get_evaluation_request(
                        resume_ctx, step.input_payload or {}
                    ),
                )
                if resume_eval.decision is PolicyDecision.DENY:
                    reason = f"Policy DENY on resume: {resume_eval.reason}"[:250]
                    await self._fail_step_and_workflow(
                        session, workflow_id, step.step_id, reason
                    )
                    await session.commit()
                    self._audit(
                        "step_failed",
                        workflow_id=workflow_id,
                        step_id=step.step_id,
                        task_id=task_id,
                        owner_id=wf.owner_id,
                        purpose=wf.purpose,
                        policy_decision="DENY",
                        detail=resume_eval.reason,
                    )
                    return await self._get_wf(workflow_id)
                if resume_eval.decision is PolicyDecision.ASK:
                    step.status = StepStatus.WAITING.value
                    step.failure_reason = "Waiting for owner approval"
                    wf.status = WorkflowStatus.WAITING_APPROVAL.value
                    await session.commit()
                    self._audit(
                        "workflow_waiting",
                        workflow_id=workflow_id,
                        step_id=step.step_id,
                        task_id=task_id,
                        owner_id=wf.owner_id,
                        purpose=wf.purpose,
                        policy_decision="ASK",
                        detail="Awaiting owner approval",
                    )
                    return await self._get_wf(workflow_id)

            if wf.status in {
                WorkflowStatus.COMPLETED.value,
                WorkflowStatus.FAILED.value,
                WorkflowStatus.CANCELLED.value,
                WorkflowStatus.EXPIRED.value,
            }:
                return await self._get_wf(workflow_id)

            step.status = StepStatus.COMPLETED.value
            step.completed_at = _utcnow()
            step.output_payload = response_payload

            ctx = dict(wf.context_data or {})
            ctx[f"step_{step.step_number}"] = response_payload
            ctx[step.step_type] = response_payload
            wf.context_data = ctx

            if wf.status == WorkflowStatus.WAITING_REMOTE.value:
                wf.status = WorkflowStatus.RUNNING.value

            await session.commit()

        if workflow_id:
            self._audit(
                "workflow_resumed",
                workflow_id=workflow_id,
                step_id=step.step_id,
                task_id=task_id,
                status=WorkflowStatus.RUNNING.value,
                detail="A2A task response arrived",
            )
            await self._enqueue_advance(workflow_id)
            return await self._get_wf(workflow_id)
        return None

    # --- Crash Recovery -------------------------------------------------------

    async def recover_interrupted_workflows(self) -> list[uuid.UUID]:
        """Recovers workflows left in a non-terminal state after a crash."""
        recovered_ids: list[uuid.UUID] = []
        async with self._session_factory() as session:
            running_wfs = await self._repo.find_interrupted_workflows(session)
            now = _utcnow()

            for wf in running_wfs:
                # Check expiration
                if wf.expires_at <= now:
                    wf.status = WorkflowStatus.EXPIRED.value
                    wf.failure_reason = "Expired while interrupted"
                    for s in wf.steps:
                        if s.status in {
                            StepStatus.PENDING.value,
                            StepStatus.WAITING.value,
                        }:
                            s.status = StepStatus.SKIPPED.value
                        elif s.status == StepStatus.RUNNING.value:
                            s.status = StepStatus.FAILED.value
                            s.failure_reason = (
                                "parent expired while step running"
                            )
                            s.completed_at = now
                    self._audit(
                        "workflow_expired",
                        workflow_id=wf.workflow_id,
                        owner_id=wf.owner_id,
                        status=wf.status,
                        purpose=wf.purpose,
                    )
                    continue

                # Find steps left in RUNNING
                crashed_steps = await self._step_repo.find_running_steps_for_workflow(
                    session, wf.workflow_id
                )
                for cs in crashed_steps:
                    cs.attempt_count += 1
                    if cs.attempt_count < cs.max_attempts:
                        cs.status = StepStatus.PENDING.value
                        cs.failure_reason = "Recovered after process restart"
                    else:
                        cs.status = StepStatus.FAILED.value
                        cs.failure_reason = "Max attempts exceeded after restart"
                        wf.status = WorkflowStatus.FAILED.value
                        wf.failure_reason = (
                            f"Step {cs.step_number} failed after crash recovery"
                        )

                if wf.status in {
                    WorkflowStatus.RUNNING.value,
                    WorkflowStatus.WAITING_APPROVAL.value,
                    WorkflowStatus.WAITING_REMOTE.value,
                    WorkflowStatus.PENDING.value,
                }:
                    recovered_ids.append(wf.workflow_id)

            await session.commit()

        # Resume recovered workflows
        for wid in recovered_ids:
            try:
                self._audit(
                    "workflow_resumed",
                    workflow_id=wid,
                    status=WorkflowStatus.RUNNING.value,
                    detail="Crash recovery resumption",
                )
                await self.advance_workflow(wid)
            except Exception as exc:
                logger.error("Failed resuming recovered workflow %s: %s", wid, exc)

        return recovered_ids


__all__ = [
    "WorkflowService",
]
