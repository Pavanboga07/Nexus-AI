"""AutonomyService: orchestrator for controlled autonomy and decisions (Part 10)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.autonomy.decision_engine import DecisionEngine, DecisionOutcome, DecisionRequest
from app.autonomy.errors import (
    AutonomyConflictError,
    AutonomyDisabledError,
    AutonomyRunNotFoundError,
)
from app.autonomy.executor import AutonomyExecutor
from app.autonomy.models import (
    ApprovalStatus,
    AutonomyConfig,
    AutonomyDecision,
    AutonomyMode,
    AutonomyRun,
    RunStatus,
    TriggerType,
)
from app.autonomy.planner import ActionPlan, ActionPlanner, PlanAction
from app.autonomy.repository import (
    AutonomyApprovalRepository,
    AutonomyConfigRepository,
    AutonomyDecisionRepository,
    AutonomyRunRepository,
)
from app.autonomy.triggers import TriggerService
from app.policy.service import PolicyService

logger = logging.getLogger("nexus.autonomy.service")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AutonomyService:
    """Central orchestration facade for Part 10 Autonomy."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        policy_service: PolicyService,
        tool_service: Any = None,
        a2a_service: Any = None,
        workflow_service: Any = None,
        memory_manager: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._policy = policy_service
        self._tools = tool_service
        self._a2a = a2a_service
        self._workflows = workflow_service
        self._memory = memory_manager

        self._config_repo = AutonomyConfigRepository()
        self._run_repo = AutonomyRunRepository()
        self._decision_repo = AutonomyDecisionRepository()
        self._approval_repo = AutonomyApprovalRepository()
        self._triggers = TriggerService()

        self._decision_engine = DecisionEngine(
            policy_service=self._policy,
            a2a_service=self._a2a,
            tool_service=self._tools,
        )
        self._planner = ActionPlanner()
        self._executor = AutonomyExecutor(
            decision_engine=self._decision_engine,
            workflow_service=self._workflows,
            a2a_service=self._a2a,
            tool_service=self._tools,
            memory_manager=self._memory,
        )

    # --- Config ---------------------------------------------------------------

    async def get_config(self, owner_id: uuid.UUID) -> AutonomyConfig:
        async with self._session_factory() as session:
            config = await self._config_repo.get_or_create_default(session, owner_id)
            await session.commit()
            await session.refresh(config)
            session.expunge(config)
            return config

    async def update_config(self, owner_id: uuid.UUID, **fields) -> AutonomyConfig:
        async with self._session_factory() as session:
            config = await self._config_repo.update(session, owner_id, **fields)
            await session.commit()
            await session.refresh(config)
            session.expunge(config)
            return config

    # --- Standalone Evaluation -----------------------------------------------

    async def evaluate_action(
        self, owner_id: uuid.UUID, request: DecisionRequest
    ) -> DecisionOutcome:
        config = await self.get_config(owner_id)
        return await self._decision_engine.evaluate(owner_id, config, request)

    # --- Run Lifecycle --------------------------------------------------------

    async def create_run(
        self,
        owner_id: uuid.UUID,
        goal: str,
        context_data: dict[str, Any] | None = None,
        custom_plan: Sequence[dict[str, Any]] | None = None,
        execute_immediately: bool = True,
    ) -> AutonomyRun:
        config = await self.get_config(owner_id)
        if not config.enabled or config.mode == AutonomyMode.OFF.value:
            raise AutonomyDisabledError("Cannot create run: autonomy is OFF or disabled")

        plan: ActionPlan = self._planner.create_plan(
            goal=goal,
            context=context_data,
            custom_actions=custom_plan,
        )

        async with self._session_factory() as session:
            run = await self._run_repo.create(
                session,
                owner_id=owner_id,
                goal=goal,
                plan=[a.to_dict() for a in plan.actions],
                context_data=context_data or {},
            )
            # Record initial trigger
            await self._triggers.record_trigger(
                session,
                owner_id=owner_id,
                trigger_type=TriggerType.USER_REQUEST,
                run_id=run.id,
                payload={"goal": goal},
            )
            await session.commit()
            run_id = run.id

        if execute_immediately:
            return await self.execute_run(owner_id, run_id)

        return await self.get_run(owner_id, run_id)

    async def execute_run(self, owner_id: uuid.UUID, run_id: uuid.UUID) -> AutonomyRun:
        config = await self.get_config(owner_id)

        while True:
            async with self._session_factory() as session:
                run = await self._run_repo.get(session, run_id, owner_id)
                if run is None:
                    raise AutonomyRunNotFoundError(str(run_id))

                if run.status not in {RunStatus.PENDING.value, RunStatus.RUNNING.value}:
                    return run

                run.status = RunStatus.RUNNING.value
                plan_actions = [PlanAction(**a) for a in (run.plan or [])]

                if run.current_step >= len(plan_actions):
                    # All steps completed!
                    run.status = RunStatus.COMPLETED.value
                    run.completed_at = _utcnow()
                    await session.commit()
                    return run

                next_action = plan_actions[run.current_step]

                # Execute next step
                res = await self._executor.execute_step(
                    session,
                    owner_id=owner_id,
                    run=run,
                    config=config,
                    action=next_action,
                )
                await session.commit()

            # Check loop exit condition
            if res.status in {
                RunStatus.WAITING_APPROVAL,
                RunStatus.WAITING_REMOTE,
                RunStatus.STOPPED,
                RunStatus.FAILED,
            }:
                return await self.get_run(owner_id, run_id)

    async def approve_run(
        self, owner_id: uuid.UUID, run_id: uuid.UUID, notes: str | None = None
    ) -> AutonomyRun:
        async with self._session_factory() as session:
            run = await self._run_repo.get(session, run_id, owner_id)
            if run is None:
                raise AutonomyRunNotFoundError(str(run_id))

            if run.status != RunStatus.WAITING_APPROVAL.value:
                raise AutonomyConflictError(
                    f"Run {run_id} is in status '{run.status}', not 'waiting_approval'"
                )

            approval = await self._approval_repo.get_pending_by_run(session, run_id)
            if approval:
                approval.status = ApprovalStatus.APPROVED.value
                approval.notes = notes
                approval.resolved_at = _utcnow()

            run.status = RunStatus.RUNNING.value

            # Execute the currently approved step with is_pre_approved=True
            config = await self._config_repo.get_or_create_default(session, owner_id)
            plan_actions = [PlanAction(**a) for a in (run.plan or [])]
            if run.current_step < len(plan_actions):
                cur_action = plan_actions[run.current_step]
                await self._executor.execute_step(
                    session,
                    owner_id=owner_id,
                    run=run,
                    config=config,
                    action=cur_action,
                    is_pre_approved=True,
                )

            await self._triggers.record_trigger(
                session,
                owner_id=owner_id,
                trigger_type=TriggerType.APPROVAL_GRANTED,
                run_id=run_id,
                payload={"notes": notes},
            )
            await session.commit()

        # Continue execution
        return await self.execute_run(owner_id, run_id)

    async def reject_run(
        self, owner_id: uuid.UUID, run_id: uuid.UUID, notes: str | None = None
    ) -> AutonomyRun:
        async with self._session_factory() as session:
            run = await self._run_repo.get(session, run_id, owner_id)
            if run is None:
                raise AutonomyRunNotFoundError(str(run_id))

            approval = await self._approval_repo.get_pending_by_run(session, run_id)
            if approval:
                approval.status = ApprovalStatus.REJECTED.value
                approval.notes = notes
                approval.resolved_at = _utcnow()

            run.status = RunStatus.STOPPED.value
            run.stop_reason = f"Owner rejected approval: {notes or 'No reason specified'}"
            run.completed_at = _utcnow()

            await self._triggers.record_trigger(
                session,
                owner_id=owner_id,
                trigger_type=TriggerType.APPROVAL_REJECTED,
                run_id=run_id,
                payload={"notes": notes},
            )
            await session.commit()
            return run

    async def cancel_run(self, owner_id: uuid.UUID, run_id: uuid.UUID) -> AutonomyRun:
        """Interrupt and cancel a run cleanly and idempotently."""
        async with self._session_factory() as session:
            run = await self._run_repo.get(session, run_id, owner_id)
            if run is None:
                raise AutonomyRunNotFoundError(str(run_id))

            if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
                return run

            run.status = RunStatus.CANCELLED.value
            run.stop_reason = "Cancelled by owner"
            run.completed_at = _utcnow()

            # If linked workflow exists, cancel it
            if run.workflow_id and self._workflows:
                try:
                    await self._workflows.cancel_workflow(run.owner_id, run.workflow_id)
                except Exception as exc:
                    logger.warning("Failed to cancel linked workflow %s: %s", run.workflow_id, exc)

            await session.commit()
            return run

    async def get_run(self, owner_id: uuid.UUID, run_id: uuid.UUID) -> AutonomyRun:
        async with self._session_factory() as session:
            run = await self._run_repo.get(session, run_id, owner_id)
            if run is None:
                raise AutonomyRunNotFoundError(str(run_id))
            return run

    async def list_runs(
        self,
        owner_id: uuid.UUID,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AutonomyRun]:
        async with self._session_factory() as session:
            return await self._run_repo.list(
                session, owner_id, status=status, limit=limit, offset=offset
            )

    async def list_decisions(
        self, owner_id: uuid.UUID, run_id: uuid.UUID
    ) -> Sequence[AutonomyDecision]:
        async with self._session_factory() as session:
            return await self._decision_repo.list_by_run(session, owner_id, run_id)

    async def list_audits(
        self, owner_id: uuid.UUID, run_id: uuid.UUID | None = None
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            triggers = await self._triggers.list_triggers(session, owner_id, run_id=run_id)
            return [t.to_dict() for t in triggers]

    # --- Crash Recovery -------------------------------------------------------

    async def reconcile_on_startup(self) -> int:
        """Find in-flight RUNNING runs and reconcile them safely."""
        reconciled = 0
        now = _utcnow()
        async with self._session_factory() as session:
            running_runs = await self._run_repo.list_running_all(session)
            for r in running_runs:
                # Check if it was waiting on a linked workflow
                if r.workflow_id and self._workflows:
                    try:
                        wf = await self._workflows.get_workflow(r.owner_id, r.workflow_id)
                        if wf.status == "completed":
                            r.status = RunStatus.COMPLETED.value
                            r.completed_at = now
                        elif wf.status in {"failed", "cancelled", "expired"}:
                            r.status = RunStatus.FAILED.value
                            r.failure_reason = f"Linked workflow {wf.status}"[:250]
                            r.completed_at = now
                        elif wf.status == "waiting_approval":
                            r.status = RunStatus.WAITING_APPROVAL.value
                        elif wf.status == "waiting_remote":
                            r.status = RunStatus.WAITING_REMOTE.value
                    except Exception:
                        r.status = RunStatus.STOPPED.value
                        r.stop_reason = "Interrupted by server restart"
                        r.completed_at = now
                else:
                    # Safe restart recovery: don't blindly re-execute external actions; mark STOPPED
                    r.status = RunStatus.STOPPED.value
                    r.stop_reason = "Interrupted by server restart"
                    r.completed_at = now

                reconciled += 1

            if reconciled > 0:
                await session.commit()
                logger.info("autonomy_crash_recovery reconciled_runs=%d", reconciled)

        return reconciled


__all__ = ["AutonomyService"]
