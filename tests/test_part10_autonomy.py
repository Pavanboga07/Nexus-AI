"""Comprehensive test suite for Part 10: Nexus Autonomy & Decision Engine.

Validates:
1. Autonomy Configuration & Modes (OFF, ASSISTED, BOUNDED, FULLY_DELEGATED)
2. Deterministic 13-Step Decision Engine & Allowlist Enforcement
3. Deterministic Risk Classification (LOW, MEDIUM, HIGH, CRITICAL)
4. Policy & Consent Enforcement (ALLOW, ASK pauses, DENY halts)
5. Untrusted Agent Rejection & Capability Checks
6. Hard Limits Enforcement (max_steps, max_tool_calls, max_remote_tasks, max_runtime)
7. Approval Flows (ASK -> WAITING_APPROVAL -> approve -> resume / reject -> stop)
8. Owner Interruption & Safe Idempotent Cancellation
9. Crash Recovery & Startup Reconciliation
10. Malicious Plan & LLM Prompt Bypass Resistance
11. Owner Isolation
12. Audit Logging & Decision History
13. HTTP REST API Endpoints
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.a2a.service import A2AService
from app.autonomy.decision_engine import DecisionEngine, DecisionOutcome, DecisionRequest
from app.autonomy.errors import (
    AutonomyActionDeniedError,
    AutonomyConflictError,
    AutonomyDisabledError,
    AutonomyInvalidActionError,
    AutonomyLimitExceededError,
    AutonomyRunNotFoundError,
)
from app.autonomy.limits import classify_risk
from app.autonomy.models import (
    ActionType,
    ApprovalStatus,
    AutonomyConfig,
    AutonomyMode,
    AutonomyRun,
    DecisionResult,
    RiskLevel,
    RunStatus,
)
from app.autonomy.planner import ActionPlan, ActionPlanner, PlanAction
from app.autonomy.service import AutonomyService
from app.database.repositories import OwnerRepository
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService
from app.workflows.service import WorkflowService
from tests.conftest import drain_workflow_jobs


@pytest_asyncio.fixture
async def test_owner(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        return owner.id


@pytest_asyncio.fixture
async def second_owner(db_session_factory) -> uuid.UUID:
    from app.database.models import Owner
    async with db_session_factory() as session:
        owner = Owner(name="Second Owner")
        session.add(owner)
        await session.commit()
        return owner.id


@pytest_asyncio.fixture
async def autonomy_service_instance(
    db_session_factory, policy_service, memory_manager
) -> AutonomyService:
    tool_registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        tool_registry.register(tool)
    tool_service = ToolService(
        registry=tool_registry,
        policy_service=policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=4096,
    )
    workflow_service = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        memory_manager=memory_manager,
        default_ttl_seconds=3600,
        max_step_attempts=3,
    )

    return AutonomyService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        workflow_service=workflow_service,
        memory_manager=memory_manager,
    )


# =============================================================================
# 1. CONFIGURATION & MODES
# =============================================================================


@pytest.mark.asyncio
async def test_autonomy_config_defaults_and_update(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Verify conservative default configuration and update semantics."""
    cfg = await autonomy_service_instance.get_config(test_owner)
    assert cfg.owner_id == test_owner
    assert cfg.mode == AutonomyMode.BOUNDED.value
    assert cfg.enabled is True
    assert cfg.max_steps_per_run == 10
    assert cfg.max_runtime_seconds == 3600
    assert cfg.require_approval_for_external_communication is True
    assert cfg.require_approval_for_sensitive_data is True

    # Update config
    updated = await autonomy_service_instance.update_config(
        test_owner,
        mode=AutonomyMode.ASSISTED.value,
        max_steps_per_run=5,
    )
    assert updated.mode == AutonomyMode.ASSISTED.value
    assert updated.max_steps_per_run == 5


@pytest.mark.asyncio
async def test_autonomy_mode_off_halts_execution(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """When autonomy is OFF, actions are halted immediately."""
    await autonomy_service_instance.update_config(
        test_owner, mode=AutonomyMode.OFF.value
    )

    with pytest.raises(AutonomyDisabledError):
        await autonomy_service_instance.create_run(
            test_owner,
            goal="Calculate quarterly taxes",
            execute_immediately=True,
        )

    # Standalone evaluation also returns STOP
    req = DecisionRequest(
        action_type=ActionType.EXECUTE_TOOL.value,
        proposed_action="Calculate quarterly taxes",
        purpose="tax_calc",
        goal="Calculate quarterly taxes",
        tool_name="calculator",
    )
    outcome = await autonomy_service_instance.evaluate_action(test_owner, req)
    assert outcome.decision == DecisionResult.STOP
    assert "disabled" in outcome.reason.lower()


@pytest.mark.asyncio
async def test_autonomy_mode_assisted_requires_approval(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """In ASSISTED mode, any external/consequential action returns ASK."""
    await autonomy_service_instance.update_config(
        test_owner, mode=AutonomyMode.ASSISTED.value
    )

    req = DecisionRequest(
        action_type=ActionType.CREATE_WORKFLOW.value,
        proposed_action="Initiate multi-step workflow",
        purpose="workflow_coordination",
        goal="Coordinate meeting",
    )
    outcome = await autonomy_service_instance.evaluate_action(test_owner, req)
    assert outcome.decision == DecisionResult.ASK
    assert outcome.requires_approval is True
    assert "assisted mode" in outcome.reason.lower()


@pytest.mark.asyncio
async def test_autonomy_mode_fully_delegated_still_policy_bound(
    autonomy_service_instance: AutonomyService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    """FULL_DELEGATED mode is NEVER unrestricted: policy DENY still halts."""
    await autonomy_service_instance.update_config(
        test_owner,
        mode=AutonomyMode.FULLY_DELEGATED.value,
        require_approval_for_external_communication=False,
    )

    # Set up an explicit policy DENY for confidential data
    await policy_service.create_policy(
        owner_id=test_owner,
        requester_agent_id="*",
        data_category="confidential",
        action="execute_tool",
        purpose="*",
        decision="DENY",
    )

    req = DecisionRequest(
        action_type=ActionType.EXECUTE_TOOL.value,
        proposed_action="Execute tool with confidential data",
        purpose="confidential",
        goal="Secret analysis",
        tool_name="calculator",
        required_data_categories=["confidential"],
    )
    outcome = await autonomy_service_instance.evaluate_action(test_owner, req)
    assert outcome.decision == DecisionResult.DENY
    assert "Policy DENY" in outcome.reason


# =============================================================================
# 2. DETERMINISTIC RISK CLASSIFICATION & ALLOWLIST
# =============================================================================


def test_deterministic_risk_classification():
    """Risk classification is rule-based and independent of any LLM."""
    # 1. Untrusted agent is CRITICAL
    assert (
        classify_risk(
            action_type=ActionType.CONTACT_AGENT.value,
            target_agent_id="nexus:ed25519:unknown",
            is_trusted=False,
        )
        == RiskLevel.CRITICAL
    )

    # 2. Financial categories are CRITICAL
    assert (
        classify_risk(
            action_type=ActionType.READ_MEMORY.value,
            data_categories=["financial"],
        )
        == RiskLevel.CRITICAL
    )

    # 3. Critical tools are CRITICAL
    assert (
        classify_risk(
            action_type=ActionType.EXECUTE_TOOL.value,
            tool_name="transfer_funds",
        )
        == RiskLevel.CRITICAL
    )

    # 4. Personal sensitive data is HIGH
    assert (
        classify_risk(
            action_type=ActionType.READ_MEMORY.value,
            data_categories=["health"],
        )
        == RiskLevel.HIGH
    )

    # 5. Low risk reads/tools are LOW
    assert (
        classify_risk(
            action_type=ActionType.EXECUTE_TOOL.value,
            tool_name="calculator",
        )
        == RiskLevel.LOW
    )
    assert (
        classify_risk(
            action_type=ActionType.READ_MEMORY.value,
            data_categories=["preferences"],
        )
        == RiskLevel.LOW
    )


@pytest.mark.asyncio
async def test_action_allowlist_enforcement(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Arbitrary shell or script commands are strictly forbidden."""
    req = DecisionRequest(
        action_type="run_bash_command",
        proposed_action="rm -rf /",
        purpose="clean",
        goal="Clean system",
    )
    outcome = await autonomy_service_instance.evaluate_action(test_owner, req)
    assert outcome.decision == DecisionResult.DENY
    assert "explicit allowlist" in outcome.reason


# =============================================================================
# 3. HARD LIMITS ENFORCEMENT
# =============================================================================


@pytest.mark.asyncio
async def test_hard_limits_max_steps(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Runs stop when max_steps_per_run limit is reached."""
    await autonomy_service_instance.update_config(
        test_owner,
        mode=AutonomyMode.FULLY_DELEGATED.value,
        max_steps_per_run=2,
        require_approval_for_external_communication=False,
        require_approval_for_sensitive_data=False,
    )

    custom_plan = [
        {"action_type": ActionType.READ_CONTEXT.value, "purpose": "step1", "proposed_action": "Step 1"},
        {"action_type": ActionType.READ_CONTEXT.value, "purpose": "step2", "proposed_action": "Step 2"},
        {"action_type": ActionType.READ_CONTEXT.value, "purpose": "step3", "proposed_action": "Step 3"},
    ]

    run = await autonomy_service_instance.create_run(
        test_owner,
        goal="Run 3 steps",
        custom_plan=custom_plan,
        execute_immediately=True,
    )
    assert run.status == RunStatus.STOPPED.value
    assert "max_steps_exceeded" in (run.stop_reason or "")
    assert run.steps_executed == 2


@pytest.mark.asyncio
async def test_hard_limits_max_tool_calls(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Runs stop when max_tool_calls limit is reached."""
    await autonomy_service_instance.update_config(
        test_owner,
        mode=AutonomyMode.FULLY_DELEGATED.value,
        max_tool_calls=1,
        require_approval_for_external_communication=False,
        require_approval_for_sensitive_data=False,
    )

    custom_plan = [
        {
            "action_type": ActionType.EXECUTE_TOOL.value,
            "purpose": "calc1",
            "proposed_action": "Calc 1",
            "tool_name": "calculator",
            "payload": {"expression": "1 + 1"},
        },
        {
            "action_type": ActionType.EXECUTE_TOOL.value,
            "purpose": "calc2",
            "proposed_action": "Calc 2",
            "tool_name": "calculator",
            "payload": {"expression": "2 + 2"},
        },
    ]

    run = await autonomy_service_instance.create_run(
        test_owner,
        goal="Run tools",
        custom_plan=custom_plan,
        execute_immediately=True,
    )
    assert run.status == RunStatus.STOPPED.value
    assert "max_tool_calls_exceeded" in (run.stop_reason or "")
    assert run.tool_calls == 1


# =============================================================================
# 4. APPROVAL FLOW & RECOVERY
# =============================================================================


@pytest.mark.asyncio
async def test_approval_flow_pause_and_resume(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Actions requiring approval pause in WAITING_APPROVAL, resume upon approval."""
    await autonomy_service_instance.update_config(
        test_owner,
        mode=AutonomyMode.BOUNDED.value,
    )

    custom_plan = [
        {
            "action_type": ActionType.READ_CONTEXT.value,
            "purpose": "eval",
            "proposed_action": "Read context",
        },
        {
            "action_type": ActionType.REQUEST_APPROVAL.value,
            "purpose": "confirm_date",
            "proposed_action": "Confirm meeting date",
            "payload": {"proposed_time": "18:00"},
        },
        {
            "action_type": ActionType.UPDATE_MEMORY.value,
            "purpose": "record_confirmation",
            "proposed_action": "Save confirmed time",
            "payload": {"content": "Meeting confirmed for 18:00"},
        },
    ]

    # Create run -> pauses on step 2 (REQUEST_APPROVAL)
    run = await autonomy_service_instance.create_run(
        test_owner,
        goal="Arrange meeting with approval",
        custom_plan=custom_plan,
        execute_immediately=True,
    )
    assert run.status == RunStatus.WAITING_APPROVAL.value
    assert len(run.approvals) == 1
    assert run.approvals[0].status == ApprovalStatus.PENDING.value

    # Owner approves the run
    resumed = await autonomy_service_instance.approve_run(
        test_owner,
        run.id,
        notes="Looks good, proceed",
    )
    assert resumed.status == RunStatus.COMPLETED.value
    assert resumed.steps_executed == 3


@pytest.mark.asyncio
async def test_approval_flow_reject(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """When owner rejects approval, the run transitions to STOPPED."""
    custom_plan = [
        {
            "action_type": ActionType.REQUEST_APPROVAL.value,
            "purpose": "confirm_purchase",
            "proposed_action": "Confirm high-cost item",
        }
    ]

    run = await autonomy_service_instance.create_run(
        test_owner,
        goal="Purchase approval",
        custom_plan=custom_plan,
        execute_immediately=True,
    )
    assert run.status == RunStatus.WAITING_APPROVAL.value

    rejected = await autonomy_service_instance.reject_run(
        test_owner,
        run.id,
        notes="Too expensive",
    )
    assert rejected.status == RunStatus.STOPPED.value
    assert "Too expensive" in (rejected.stop_reason or "")


@pytest.mark.asyncio
async def test_safe_idempotent_cancellation(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Owner can cancel an active run safely; repeated cancellation is idempotent."""
    custom_plan = [
        {
            "action_type": ActionType.REQUEST_APPROVAL.value,
            "purpose": "awaiting",
            "proposed_action": "Wait",
        }
    ]
    run = await autonomy_service_instance.create_run(
        test_owner,
        goal="To be cancelled",
        custom_plan=custom_plan,
        execute_immediately=True,
    )
    assert run.status == RunStatus.WAITING_APPROVAL.value

    # First cancellation
    cancelled1 = await autonomy_service_instance.cancel_run(test_owner, run.id)
    assert cancelled1.status == RunStatus.CANCELLED.value

    # Second cancellation (idempotent)
    cancelled2 = await autonomy_service_instance.cancel_run(test_owner, run.id)
    assert cancelled2.status == RunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_crash_recovery_on_startup(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    test_owner: uuid.UUID,
):
    """On server restart, interrupted RUNNING runs are reconciled to STOPPED."""
    # Simulate an interrupted run directly in DB
    from app.autonomy.models import AutonomyRun
    async with db_session_factory() as session:
        interrupted_run = AutonomyRun(
            owner_id=test_owner,
            goal="Interrupted operation",
            status=RunStatus.RUNNING.value,
            started_at=datetime.now(timezone.utc),
        )
        session.add(interrupted_run)
        await session.commit()
        run_id = interrupted_run.id

    # Execute startup recovery
    reconciled_count = await autonomy_service_instance.reconcile_on_startup()
    assert reconciled_count >= 1

    # Verify run is now safely marked STOPPED
    recovered = await autonomy_service_instance.get_run(test_owner, run_id)
    assert recovered.status == RunStatus.STOPPED.value
    assert "restart" in (recovered.stop_reason or "").lower()


# =============================================================================
# 5. MALICIOUS PLAN & LLM BYPASS RESISTANCE
# =============================================================================


@pytest.mark.asyncio
async def test_malicious_plan_llm_bypass_resistance(
    autonomy_service_instance: AutonomyService, test_owner: uuid.UUID
):
    """Untrusted plans cannot execute arbitrary code or bypass security."""
    planner = ActionPlanner()

    # If LLM emits malicious action types, planner rejects them
    malicious_actions = [
        {"action_type": "import_os_and_system", "purpose": "hack", "proposed_action": "os.system('sh')"}
    ]
    with pytest.raises(AutonomyInvalidActionError):
        planner.create_plan(
            goal="Hack host",
            custom_actions=malicious_actions,
        )


# =============================================================================
# 6. OWNER ISOLATION
# =============================================================================


@pytest.mark.asyncio
async def test_owner_isolation(
    autonomy_service_instance: AutonomyService,
    test_owner: uuid.UUID,
    second_owner: uuid.UUID,
):
    """Owner A cannot view or manipulate Owner B's autonomous runs."""
    run_a = await autonomy_service_instance.create_run(
        test_owner,
        goal="Owner A private task",
        execute_immediately=False,
    )

    # Owner B cannot access Owner A's run
    with pytest.raises(AutonomyRunNotFoundError):
        await autonomy_service_instance.get_run(second_owner, run_a.id)

    with pytest.raises(AutonomyRunNotFoundError):
        await autonomy_service_instance.cancel_run(second_owner, run_a.id)


# =============================================================================
# 7. HTTP REST API
# =============================================================================


@pytest.mark.asyncio
async def test_autonomy_http_api_endpoints(db_autonomy_client: AsyncClient):
    """Verify HTTP endpoints for autonomy config, run lifecycle, and evaluate."""
    # 1. GET /autonomy/config
    res = await db_autonomy_client.get("/autonomy/config")
    assert res.status_code == 200
    config_data = res.json()
    assert config_data["mode"] == "bounded"

    # 2. POST /autonomy/config
    res = await db_autonomy_client.post("/autonomy/config", json={"max_steps_per_run": 7})
    assert res.status_code == 200
    assert res.json()["max_steps_per_run"] == 7

    # 3. POST /autonomy/evaluate
    eval_payload = {
        "goal": "Test calculation",
        "proposed_action": "Execute calculator",
        "action_type": "execute_tool",
        "purpose": "test_calc",
        "tool_name": "calculator",
    }
    res = await db_autonomy_client.post("/autonomy/evaluate", json=eval_payload)
    assert res.status_code == 200
    assert res.json()["decision"] in {"allow", "ask"}

    # 4. POST /autonomy/run
    run_payload = {
        "goal": "Retrieve general facts",
        "execute_immediately": True,
    }
    res = await db_autonomy_client.post("/autonomy/run", json=run_payload)
    assert res.status_code == 201
    run_id = res.json()["id"]

    # 5. GET /autonomy/runs/{run_id}
    res = await db_autonomy_client.get(f"/autonomy/runs/{run_id}")
    assert res.status_code == 200
    assert res.json()["id"] == run_id

    # 6. GET /autonomy/runs/{run_id}/decisions
    res = await db_autonomy_client.get(f"/autonomy/runs/{run_id}/decisions")
    assert res.status_code == 200
    assert "decisions" in res.json()

    # 7. GET /autonomy/runs/{run_id}/audit
    res = await db_autonomy_client.get(f"/autonomy/runs/{run_id}/audit")
    assert res.status_code == 200
    assert "audits" in res.json()


# =============================================================================
# A1. AUTONOMY -> WORKFLOW CALL ARITY
# =============================================================================


@pytest.mark.asyncio
async def test_a1_cancel_run_cancels_linked_workflow(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    test_owner: uuid.UUID,
):
    """Cancelling a workflow-backed run must cancel the linked workflow (A1)."""
    from app.schemas.workflows import WorkflowStepSpec
    from app.workflows.models import WorkflowStatus

    wf_service: WorkflowService = autonomy_service_instance._workflows
    wf = await wf_service.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="a1_cancel_link",
        steps=[
            WorkflowStepSpec(
                step_type="availability_check",
                input_payload={"date": "tomorrow", "candidate_slots": ["10:00"]},
            ),
        ],
    )

    run = await autonomy_service_instance.create_run(
        test_owner,
        goal="A1 linked cancellation",
        execute_immediately=False,
    )
    async with db_session_factory() as session:
        db_run = await autonomy_service_instance._run_repo.get(
            session, run.id, test_owner
        )
        assert db_run is not None
        db_run.workflow_id = wf.workflow_id
        await session.commit()

    cancelled = await autonomy_service_instance.cancel_run(test_owner, run.id)
    assert cancelled.status == RunStatus.CANCELLED.value

    wf_after = await wf_service.get_workflow(test_owner, wf.workflow_id)
    assert wf_after.status == WorkflowStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_a1_reconcile_workflow_backed_run_terminal(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    test_owner: uuid.UUID,
):
    """reconcile_on_startup must map a terminal linked workflow onto the run (A1)."""
    from app.schemas.workflows import WorkflowStepSpec

    wf_service: WorkflowService = autonomy_service_instance._workflows
    wf = await wf_service.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="a1_reconcile_terminal",
        steps=[
            WorkflowStepSpec(
                step_type="availability_check",
                input_payload={"date": "tomorrow", "candidate_slots": ["10:00"]},
            ),
        ],
    )
    # Drive the linked workflow to a terminal state.
    await wf_service.cancel_workflow(test_owner, wf.workflow_id)

    async with db_session_factory() as session:
        linked_run = AutonomyRun(
            owner_id=test_owner,
            workflow_id=wf.workflow_id,
            goal="A1 reconcile terminal workflow",
            status=RunStatus.RUNNING.value,
            started_at=datetime.now(timezone.utc),
        )
        session.add(linked_run)
        await session.commit()
        run_id = linked_run.id

    reconciled = await autonomy_service_instance.reconcile_on_startup()
    assert reconciled >= 1

    recovered = await autonomy_service_instance.get_run(test_owner, run_id)
    assert recovered.status == RunStatus.FAILED.value
    assert "cancelled" in (recovered.failure_reason or "").lower()


# =============================================================================
# A2. CREATE_WORKFLOW USES REGISTERED STEP TYPES + start_workflow
# =============================================================================


@pytest.mark.asyncio
async def test_a2_create_workflow_advances_with_registered_step_type(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    """CREATE_WORKFLOW must use a registered step type and really start (A2)."""
    from app.autonomy.models import AutonomyRun
    from app.autonomy.planner import PlanAction
    from app.workflows.models import WorkflowStatus

    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="*",
        action="*",
        purpose="tool_test",
        decision="ALLOW",
    )

    executor = autonomy_service_instance._executor
    config = await autonomy_service_instance.get_config(test_owner)
    action = PlanAction(
        step_number=1,
        action_type=ActionType.CREATE_WORKFLOW.value,
        purpose="tool_test",
        proposed_action="Run echo tool via workflow",
        payload={
            "workflow_type": "tool_workflow",
            "tool_name": "echo",
            "arguments": {"text": "hello from autonomy"},
            "purpose": "tool_test",
        },
    )
    shell = await autonomy_service_instance.create_run(
        test_owner,
        goal="A2 workflow start",
        execute_immediately=False,
    )
    async with db_session_factory() as session:
        run = await session.get(AutonomyRun, shell.id)
        assert run is not None
        result = await executor.execute_step(
            session,
            owner_id=test_owner,
            run=run,
            config=config,
            action=action,
            is_pre_approved=True,
        )
        assert run.workflow_id is not None

    wf_service: WorkflowService = autonomy_service_instance._workflows
    wf = await wf_service.get_workflow(test_owner, run.workflow_id)
    assert wf.steps[0].step_type == "tool_execution"
    assert wf.status != WorkflowStatus.PENDING.value
    assert result.status == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_a2_create_workflow_unmappable_payload_fails_closed(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    test_owner: uuid.UUID,
):
    """A CREATE_WORKFLOW payload mapping to no step type must fail closed (A2)."""
    from app.autonomy.models import AutonomyRun
    from app.autonomy.planner import PlanAction

    executor = autonomy_service_instance._executor
    config = await autonomy_service_instance.get_config(test_owner)
    action = PlanAction(
        step_number=1,
        action_type=ActionType.CREATE_WORKFLOW.value,
        purpose="a2_unmappable",
        proposed_action="Unmappable workflow",
        payload={"workflow_type": "mystery", "note": "maps to nothing"},
    )
    shell = await autonomy_service_instance.create_run(
        test_owner,
        goal="A2 unmappable payload",
        execute_immediately=False,
    )
    async with db_session_factory() as session:
        run = await session.get(AutonomyRun, shell.id)
        assert run is not None
        result = await executor.execute_step(
            session,
            owner_id=test_owner,
            run=run,
            config=config,
            action=action,
            is_pre_approved=True,
        )
        assert run.workflow_id is None

    assert result.status == RunStatus.FAILED
    assert "no registered step type" in (result.step_output or {}).get("error", "")


@pytest.mark.asyncio
async def test_a2_create_workflow_propagates_waiting_remote(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    policy_service: PolicyService,
    memory_manager,
    test_owner: uuid.UUID,
):
    """A workflow parked in WAITING_REMOTE must park the run too (A2)."""
    from app.autonomy.executor import AutonomyExecutor
    from app.autonomy.models import AutonomyRun
    from app.autonomy.planner import PlanAction
    from app.tools.builtin import BUILTIN_TOOLS
    from app.tools.registry import ToolRegistry
    from app.tools.service import ToolService
    from app.workflows.models import WorkflowStatus

    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="a2a",
        action="delegate_task",
        purpose="remote_task",
        decision="ALLOW",
    )

    class _PendingA2A:
        async def delegate_task(self, owner_id, **kwargs):
            return {"task_id": "task-pending-1", "status": "pending", "payload": {}}

    tool_registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        tool_registry.register(tool)
    tool_service = ToolService(
        registry=tool_registry,
        policy_service=policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=4096,
    )
    wf_service = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        a2a_service=_PendingA2A(),
        memory_manager=memory_manager,
        default_ttl_seconds=3600,
        max_step_attempts=3,
    )
    executor = AutonomyExecutor(
        decision_engine=autonomy_service_instance._decision_engine,
        workflow_service=wf_service,
        a2a_service=_PendingA2A(),
        tool_service=tool_service,
        memory_manager=memory_manager,
    )
    config = await autonomy_service_instance.get_config(test_owner)
    action = PlanAction(
        step_number=1,
        action_type=ActionType.CREATE_WORKFLOW.value,
        purpose="remote_task",
        proposed_action="Delegate availability check to peer",
        payload={
            "workflow_type": "remote_workflow",
            "target_agent_id": "nexus:ed25519:peer",
            "recipient_agent_id": "nexus:ed25519:peer",
            "task_type": "availability_check",
            "purpose": "remote_task",
        },
    )
    shell = await autonomy_service_instance.create_run(
        test_owner,
        goal="A2 remote propagation",
        execute_immediately=False,
    )
    async with db_session_factory() as session:
        run = await session.get(AutonomyRun, shell.id)
        assert run is not None
        result = await executor.execute_step(
            session,
            owner_id=test_owner,
            run=run,
            config=config,
            action=action,
            is_pre_approved=True,
        )
        assert run.workflow_id is not None
        # A5 timing: start_workflow only enqueues advancement, so the step
        # returns in-progress; the workflow parks once its jobs drain.
        assert result.status == RunStatus.RUNNING
        wf_id = run.workflow_id
        await session.commit()

    await drain_workflow_jobs(db_session_factory, wf_service)
    wf = await wf_service.get_workflow(test_owner, wf_id)
    assert wf.status == WorkflowStatus.WAITING_REMOTE.value
    # A8: linked run must reflect WAITING_REMOTE too (restores A2 intent).
    run_after = await autonomy_service_instance.get_run(test_owner, shell.id)
    assert run_after.status == RunStatus.WAITING_REMOTE.value


# =============================================================================
# A8. SINGLE CONSENT + RUN LINKAGE
# =============================================================================


@pytest.mark.asyncio
async def test_a8_approve_workflow_backed_orchestration_single_consent(
    db_session_factory,
    policy_service: PolicyService,
    memory_manager,
    test_owner: uuid.UUID,
):
    """Workflow-backed orchestration approve must not leave an unconsumed consent (A8)."""
    from unittest.mock import AsyncMock, MagicMock

    from app.orchestration.models import OrchestrationState
    from app.orchestration.orchestrator import AgentOrchestrator
    from app.orchestration.repository import OrchestrationRunRepository
    from app.orchestration.schemas import TargetResolution, TargetResolutionStatus
    from app.schemas.workflows import WorkflowStepSpec
    from app.tools.builtin import BUILTIN_TOOLS
    from app.tools.registry import ToolRegistry
    from app.tools.service import ToolService

    tool_registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        tool_registry.register(tool)
    tool_service = ToolService(
        registry=tool_registry,
        policy_service=policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=4096,
    )
    wf_service = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        memory_manager=memory_manager,
        default_ttl_seconds=3600,
        max_step_attempts=3,
    )
    wf = await wf_service.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="a8_single_consent",
        steps=[
            WorkflowStepSpec(
                step_type="availability_check",
                input_payload={"candidate_slots": ["10:00"]},
            ),
        ],
    )
    started = await wf_service.start_workflow(test_owner, wf.workflow_id)
    assert started.status == "running"
    await drain_workflow_jobs(db_session_factory, wf_service)
    parked = await wf_service.get_workflow(test_owner, wf.workflow_id)
    assert parked.status == "waiting_approval"

    runs_repo = OrchestrationRunRepository()
    async with db_session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="a8-consent",
            goal="A8 single consent",
            intent_type="COORDINATE_MEETING",
            state=OrchestrationState.WAITING_APPROVAL.value,
            target_person="Rahul",
            target_agent_id="nexus:anonymous",
            plan={"goal": "A8", "intent_type": "COORDINATE_MEETING", "steps": []},
            workflow_id=wf.workflow_id,
        )
        await session.commit()
        run_id = run.id

    orig_create = policy_service.create_consent
    mint_calls: list[dict] = []

    async def _mint_spy(owner_id, *args, **kwargs):
        mint_calls.append({"owner_id": owner_id, "args": args, "kwargs": kwargs})
        return await orig_create(owner_id, *args, **kwargs)

    policy_service.create_consent = _mint_spy  # type: ignore[method-assign]

    mock_target = AsyncMock()
    mock_target.resolve.return_value = TargetResolution(
        target_name="Rahul",
        status=TargetResolutionStatus.KNOWN_AGENT,
        agent_id="nexus:anonymous",
        display_name="Rahul",
        is_trusted=True,
    )
    orchestrator = AgentOrchestrator(
        session_factory=db_session_factory,
        a2a_service=MagicMock(),
        policy_service=policy_service,
        intent_resolver=MagicMock(),
        target_resolver=mock_target,
        workflow_service=wf_service,
    )
    await orchestrator.approve_run(test_owner, run_id)
    await drain_workflow_jobs(db_session_factory, wf_service)

    consents = await policy_service.list_consents(test_owner)
    lingering = [
        c
        for c in consents
        if c.single_use and c.decision == "ALLOW" and c.used_at is None
    ]
    assert lingering == []
    assert len(mint_calls) == 1
    step_mint = mint_calls[0]["kwargs"]
    assert step_mint["data_category"] == "calendar"
    assert step_mint["action"] == "read"
    assert step_mint["purpose"] == "a8_single_consent"
    assert step_mint["requester_agent_id"] == "nexus:self"


@pytest.mark.asyncio
async def test_a8_complete_linked_workflow_syncs_run(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    """Completing a linked workflow must move the parent run out of waiting (A8)."""
    from datetime import datetime, timezone

    from app.autonomy.models import AutonomyRun
    from app.schemas.workflows import WorkflowStepSpec
    from app.workflows.models import WorkflowStatus

    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="a8_link_complete",
        decision="ALLOW",
    )
    wf_service: WorkflowService = autonomy_service_instance._workflows
    wf = await wf_service.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="a8_link_complete",
        steps=[
            WorkflowStepSpec(
                step_type="availability_check",
                input_payload={"candidate_slots": ["10:00"]},
            ),
        ],
    )
    async with db_session_factory() as session:
        linked = AutonomyRun(
            owner_id=test_owner,
            workflow_id=wf.workflow_id,
            goal="A8 linked completion",
            status=RunStatus.WAITING_APPROVAL.value,
            started_at=datetime.now(timezone.utc),
        )
        session.add(linked)
        await session.commit()
        run_id = linked.id

    await wf_service.start_workflow(test_owner, wf.workflow_id)
    await drain_workflow_jobs(db_session_factory, wf_service)
    wf_after = await wf_service.get_workflow(test_owner, wf.workflow_id)
    assert wf_after.status == WorkflowStatus.COMPLETED.value

    run_after = await autonomy_service_instance.get_run(test_owner, run_id)
    assert run_after.status == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_a8_direct_path_orchestration_mints_single_consent(
    db_session_factory,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    """Direct-path orchestration approve must mint exact-scope consent (A8d)."""
    from unittest.mock import AsyncMock, MagicMock

    from app.orchestration.models import OrchestrationState
    from app.orchestration.orchestrator import AgentOrchestrator
    from app.orchestration.repository import OrchestrationRunRepository
    from app.orchestration.schemas import TargetResolution, TargetResolutionStatus

    runs_repo = OrchestrationRunRepository()
    async with db_session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="a8-direct",
            goal="A8 direct path",
            intent_type="COORDINATE_MEETING",
            state=OrchestrationState.WAITING_APPROVAL.value,
            target_person="Rahul",
            target_agent_id="nexus:anonymous",
            plan={
                "goal": "A8 direct",
                "intent_type": "COORDINATE_MEETING",
                "target_person": "Rahul",
                "target_agent_id": "nexus:anonymous",
                "steps": [],
            },
            workflow_id=None,
        )
        await runs_repo.set_approval_request(
            session,
            run.id,
            reason="Direct approval needed",
            requested_action="disclose_information",
            approval_target="Rahul",
            approval_category="availability",
            approval_purpose="direct_path_check",
            approval_step=1,
        )
        await session.commit()
        run_id = run.id

    orig_create = policy_service.create_consent
    calls: list[dict] = []

    async def _spy(owner_id, *args, **kwargs):
        calls.append({"owner_id": owner_id, "args": args, "kwargs": kwargs})
        return await orig_create(owner_id, *args, **kwargs)

    policy_service.create_consent = _spy  # type: ignore[method-assign]

    mock_target = AsyncMock()
    mock_target.resolve.return_value = TargetResolution(
        target_name="Rahul",
        status=TargetResolutionStatus.KNOWN_AGENT,
        agent_id="nexus:anonymous",
        display_name="Rahul",
        is_trusted=True,
    )
    mock_executor = AsyncMock()
    mock_executor.execute_plan.return_value = {
        "status": "completed",
        "message": "direct ok",
    }
    orchestrator = AgentOrchestrator(
        session_factory=db_session_factory,
        a2a_service=MagicMock(),
        policy_service=policy_service,
        intent_resolver=MagicMock(),
        target_resolver=mock_target,
        executor=mock_executor,
        workflow_service=None,
    )
    await orchestrator.approve_run(test_owner, run_id)

    assert len(calls) == 1
    minted = calls[0]["kwargs"]
    assert minted["action"] == "disclose_information"
    assert minted["data_category"] == "availability"
    assert minted["purpose"] == "direct_path_check"
    assert minted["requester_agent_id"] == "nexus:anonymous"
    assert minted["single_use"] is True


@pytest.mark.asyncio
async def test_a8_expiry_via_get_syncs_linked_run(
    autonomy_service_instance: AutonomyService,
    db_session_factory,
    test_owner: uuid.UUID,
):
    """Expiring via get_workflow (no advance) must sync WAITING runs (A8e)."""
    from datetime import datetime, timedelta, timezone

    from app.autonomy.models import AutonomyRun
    from app.schemas.workflows import WorkflowStepSpec
    from app.workflows.models import Workflow, WorkflowStatus

    wf_service: WorkflowService = autonomy_service_instance._workflows
    wf = await wf_service.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="a8_expiry_get",
        steps=[
            WorkflowStepSpec(
                step_type="availability_check",
                input_payload={"candidate_slots": ["10:00"]},
            ),
        ],
    )
    async with db_session_factory() as session:
        db_wf = await session.get(Workflow, wf.workflow_id)
        assert db_wf is not None
        db_wf.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        linked = AutonomyRun(
            owner_id=test_owner,
            workflow_id=wf.workflow_id,
            goal="A8 expiry orphan",
            status=RunStatus.WAITING_APPROVAL.value,
            started_at=datetime.now(timezone.utc),
        )
        session.add(linked)
        await session.commit()
        run_id = linked.id

    expired = await wf_service.get_workflow(test_owner, wf.workflow_id)
    assert expired.status == WorkflowStatus.EXPIRED.value

    run_after = await autonomy_service_instance.get_run(test_owner, run_id)
    assert run_after.status == RunStatus.FAILED.value
    assert run_after.failure_reason is not None
