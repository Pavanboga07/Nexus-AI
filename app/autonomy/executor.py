"""Execution subsystem for Nexus Autonomy (Part 10).

Executes approved plan actions by dispatching to authoritative existing systems:
- Multi-step execution -> WorkflowService (Part 9)
- Remote agent communication -> A2AService (Part 8)
- Tool calls -> ToolService (Part 5)
- Knowledge storage & recall -> MemoryManager (Part 2)

Autonomy NEVER executes actions directly without prior Decision Engine authorization.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.autonomy.decision_engine import DecisionEngine, DecisionOutcome, DecisionRequest
from app.autonomy.limits import check_limits
from app.autonomy.models import (
    ActionType,
    ApprovalStatus,
    AutonomyApproval,
    AutonomyConfig,
    AutonomyDecision,
    AutonomyRun,
    DecisionResult,
    RunStatus,
)
from app.autonomy.planner import PlanAction
from app.schemas.workflows import WorkflowStepSpec

logger = logging.getLogger("nexus.autonomy.executor")


@dataclass
class ExecutionResult:
    status: RunStatus
    outcome: DecisionOutcome | None = None
    step_output: dict[str, Any] | None = None
    error: str | None = None


class AutonomyExecutor:
    """Executes validated, policy-permitted autonomous actions."""

    def __init__(
        self,
        *,
        decision_engine: DecisionEngine,
        workflow_service: Any = None,
        a2a_service: Any = None,
        tool_service: Any = None,
        memory_manager: Any = None,
    ) -> None:
        self._decision_engine = decision_engine
        self._workflow_service = workflow_service
        self._a2a_service = a2a_service
        self._tool_service = tool_service
        self._memory_manager = memory_manager

    async def execute_step(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        run: AutonomyRun,
        config: AutonomyConfig,
        action: PlanAction,
        is_pre_approved: bool = False,
    ) -> ExecutionResult:
        """Evaluate and conditionally execute a single plan action."""
        now = datetime.now(timezone.utc)
        if run.started_at is None:
            run.started_at = now

        # 1. Enforce hard limits first
        within_limits, limit_violation = check_limits(run, config, now)
        if not within_limits:
            run.status = RunStatus.STOPPED.value
            run.stop_reason = limit_violation
            run.completed_at = now
            return ExecutionResult(status=RunStatus.STOPPED, error=limit_violation)

        # 2. Evaluate through Decision Engine (unless explicitly pre-approved by owner via approval record)
        if is_pre_approved:
            decision_outcome = DecisionOutcome(
                decision=DecisionResult.ALLOW,
                reason="Pre-approved by owner",
                risk_level=action.payload.get("risk_level", "low"),
                action_type=action.action_type,
                purpose=action.purpose,
                policy_decision="ALLOW",
            )
        else:
            req = DecisionRequest(
                action_type=action.action_type,
                proposed_action=action.proposed_action,
                purpose=action.purpose,
                goal=run.goal,
                target_agent_id=action.target_agent_id,
                tool_name=action.tool_name,
                required_data_categories=action.required_data_categories,
                payload=action.payload,
                run=run,
            )
            decision_outcome = await self._decision_engine.evaluate(owner_id, config, req)

            # Durably record the decision
            decision_record = AutonomyDecision(
                owner_id=owner_id,
                run_id=run.id,
                trigger="autonomous_step",
                goal=run.goal,
                proposed_action=action.proposed_action,
                action_type=action.action_type,
                purpose=action.purpose,
                risk_level=decision_outcome.risk_level.value if hasattr(decision_outcome.risk_level, "value") else str(decision_outcome.risk_level),
                required_capability=action.tool_name,
                required_data_categories=action.required_data_categories,
                decision=decision_outcome.decision.value,
                reason=decision_outcome.reason,
                policy_decision=decision_outcome.policy_decision,
                consent_decision=decision_outcome.consent_decision,
            )
            session.add(decision_record)
            await session.flush()

        # 3. Handle Decision outcome
        if decision_outcome.decision is DecisionResult.DENY:
            run.status = RunStatus.FAILED.value
            run.failure_reason = f"Decision DENY: {decision_outcome.reason}"
            run.completed_at = now
            return ExecutionResult(
                status=RunStatus.FAILED,
                outcome=decision_outcome,
                error=run.failure_reason,
            )

        if decision_outcome.decision is DecisionResult.STOP:
            run.status = RunStatus.STOPPED.value
            run.stop_reason = f"Decision STOP: {decision_outcome.reason}"
            run.completed_at = now
            return ExecutionResult(
                status=RunStatus.STOPPED,
                outcome=decision_outcome,
                error=run.stop_reason,
            )

        if decision_outcome.decision is DecisionResult.ASK:
            run.status = RunStatus.WAITING_APPROVAL.value
            approval_rec = AutonomyApproval(
                run_id=run.id,
                owner_id=owner_id,
                status=ApprovalStatus.PENDING.value,
                requested_action=action.proposed_action,
                purpose=action.purpose,
                risk_level=decision_outcome.risk_level.value if hasattr(decision_outcome.risk_level, "value") else str(decision_outcome.risk_level),
                required_data={"categories": action.required_data_categories, "payload": action.payload},
                recipient=action.target_agent_id,
                notes=decision_outcome.reason,
            )
            session.add(approval_rec)
            await session.flush()
            return ExecutionResult(
                status=RunStatus.WAITING_APPROVAL,
                outcome=decision_outcome,
            )

        # 4. Decision ALLOW -> Dispatch execution
        step_output: dict[str, Any] = {}
        act_norm = action.action_type.lower()

        try:
            if act_norm == ActionType.READ_MEMORY.value:
                query = action.payload.get("query", run.goal)
                if self._memory_manager:
                    # Retrieve relevant memories safely
                    mems = await self._memory_manager.search_memories(
                        owner_id=owner_id,
                        query=query,
                        limit=5,
                    )
                    step_output = {
                        "recalled_memories": [
                            {"content": getattr(res.memory, "content", str(res.memory)), "id": str(getattr(res.memory, "id", ""))}
                            for res in mems
                        ]
                    }
                else:
                    step_output = {"recalled_memories": [], "note": "memory_manager_offline"}

            elif act_norm == ActionType.UPDATE_MEMORY.value:
                content = action.payload.get("content", f"Completed: {run.goal}")
                if self._memory_manager:
                    mem = await self._memory_manager.store_memory(
                        owner_id=owner_id,
                        memory_type="semantic",
                        content=content,
                    )
                    step_output = {"memory_id": str(getattr(mem, "id", "")), "status": "stored"}
                else:
                    step_output = {"status": "memory_manager_offline"}

            elif act_norm == ActionType.READ_CONTEXT.value:
                step_output = {"context_snapshot": dict(run.context_data or {})}

            elif act_norm == ActionType.EXECUTE_TOOL.value:
                tool_name = action.tool_name or action.payload.get("tool_name", "calculator")
                params = action.payload.get("parameters", action.payload)
                if self._tool_service:
                    from app.tools.schemas import ToolInvocation
                    inv = ToolInvocation(
                        tool_name=tool_name,
                        arguments=params,
                        purpose=action.purpose,
                    )
                    tool_res = await self._tool_service.execute(owner_id, inv)
                    step_output = tool_res.to_dict() if hasattr(tool_res, "to_dict") else {"result": str(tool_res)}
                else:
                    step_output = {"tool_name": tool_name, "result": "tool_service_offline"}
                run.tool_calls += 1

            elif act_norm in {ActionType.CREATE_TASK.value, ActionType.CONTACT_AGENT.value}:
                target_agent = action.target_agent_id
                task_type = action.payload.get("task_type", "availability_query")
                purpose = action.purpose
                payload = action.payload.get("payload", action.payload)
                if self._a2a_service and target_agent:
                    # A2AService.delegate_task takes keyword-only arguments
                    # after owner_id; passing a request object positionally
                    # raised TypeError on every remote-task step.
                    task_res = await self._a2a_service.delegate_task(
                        owner_id,
                        recipient_agent_id=target_agent,
                        task_type=task_type,
                        purpose=purpose,
                        payload=payload,
                    )
                    step_output = task_res if isinstance(task_res, dict) else {"task_id": str(task_res)}
                else:
                    step_output = {"target_agent_id": target_agent, "task_type": task_type, "status": "simulated"}
                run.remote_tasks += 1

            elif act_norm == ActionType.CREATE_WORKFLOW.value:
                wf_type = action.payload.get("workflow_type", "autonomous_workflow")
                payload = action.payload or {}
                if payload.get("target_agent_id") or payload.get("recipient_agent_id"):
                    step_type: str = "a2a_task"
                elif payload.get("tool_name"):
                    step_type = "tool_execution"
                else:
                    return ExecutionResult(
                        status=RunStatus.FAILED,
                        step_output={"error": "CREATE_WORKFLOW payload maps to no registered step type"},
                    )
                steps_spec = [WorkflowStepSpec(step_type=step_type, input_payload=payload)]
                if self._workflow_service:
                    wf = await self._workflow_service.create_workflow(
                        owner_id,
                        workflow_type=wf_type,
                        purpose=action.purpose,
                        steps=steps_spec,
                    )
                    run.workflow_id = wf.workflow_id
                    # create_workflow() only persists the PENDING record, so the
                    # workflow must be started explicitly to advance the first step.
                    wf_run = await self._workflow_service.start_workflow(
                        owner_id, wf.workflow_id
                    )
                    step_output = {
                        "workflow_id": str(wf.workflow_id),
                        "status": wf_run.status,
                    }
                    if wf_run.status == "waiting_approval":
                        run.status = RunStatus.WAITING_APPROVAL.value
                        return ExecutionResult(
                            status=RunStatus.WAITING_APPROVAL,
                            step_output=step_output,
                        )
                    if wf_run.status == "waiting_remote":
                        run.status = RunStatus.WAITING_REMOTE.value
                        return ExecutionResult(
                            status=RunStatus.WAITING_REMOTE,
                            step_output=step_output,
                        )
                else:
                    step_output = {"workflow_type": wf_type, "status": "simulated"}

            elif act_norm == ActionType.REQUEST_APPROVAL.value:
                if is_pre_approved:
                    step_output = {"approval": "granted", "action": action.proposed_action}
                else:
                    run.status = RunStatus.WAITING_APPROVAL.value
                    approval_rec = AutonomyApproval(
                        run_id=run.id,
                        owner_id=owner_id,
                        status=ApprovalStatus.PENDING.value,
                        requested_action=action.proposed_action,
                        purpose=action.purpose,
                        risk_level=decision_outcome.risk_level.value if hasattr(decision_outcome.risk_level, "value") else str(decision_outcome.risk_level),
                        required_data=action.payload,
                        recipient=action.target_agent_id,
                        notes="Explicit approval step in plan",
                    )
                    session.add(approval_rec)
                    await session.flush()
                    return ExecutionResult(status=RunStatus.WAITING_APPROVAL, outcome=decision_outcome)

            else:
                step_output = {"action": act_norm, "status": "executed"}

        except Exception as exc:
            logger.exception("Error during autonomous action execution: %s", exc)
            run.status = RunStatus.FAILED.value
            run.failure_reason = f"Execution error: {exc}"
            run.completed_at = now
            return ExecutionResult(status=RunStatus.FAILED, error=str(exc))

        # 5. Success updating state
        run.steps_executed += 1
        run.current_step = action.step_number
        ctx = dict(run.context_data or {})
        ctx[f"step_{action.step_number}"] = step_output
        run.context_data = ctx

        return ExecutionResult(
            status=RunStatus.RUNNING,
            outcome=decision_outcome,
            step_output=step_output,
        )


__all__ = [
    "AutonomyExecutor",
    "ExecutionResult",
]
