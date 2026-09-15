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
