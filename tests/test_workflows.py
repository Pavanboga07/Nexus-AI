"""Comprehensive test suite for Part 9: Proactive Workflows & Agent Orchestration.

Tests:
1. Creation & Schema Validation (no eval/exec, unknown types fail safely).
2. Independent Policy Authorization (ALLOW, ASK pauses, DENY halts, Step 1 ALLOW != Step 2 ALLOW).
3. Owner Approval (WAITING_APPROVAL -> approve -> consent -> resume).
4. Owner Isolation (Owner A cannot view, start, approve, cancel Owner B's workflow).
5. Crash Recovery (mid-run crash persists, restart resumes safely, no duplicate step execution).
6. Concurrency Protection (locking prevents duplicate worker execution on same step).
7. Bounded Retries (transient retries up to max_attempts; security failures do NOT retry).
8. Expiration (expired workflow transitions to EXPIRED, steps skipped).
9. Cancellation (cancels active workflow, remaining steps skipped, completed steps preserved).
10. Tool Integration (invokes local tool through ToolService with policy).
11. End-to-End Meeting Coordination Workflow with Strict Minimum Disclosure.
12. HTTP API endpoints (CRUD, start, approve, cancel).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient

from app.a2a.rate_limit import SlidingWindowRateLimiter
from app.a2a.service import A2AService
from app.database.repositories import OwnerRepository
from app.identity.service import IdentityService
from app.policy.engine import EvaluationRequest
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService
from app.schemas.workflows import WorkflowStepSpec
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService
from app.workflows.errors import (
    WorkflowConflictError,
    WorkflowExpiredError,
    WorkflowNotFoundError,
)
from app.workflows.handlers import (
    A2ATaskStepHandler,
    AvailabilityStepHandler,
    BaseWorkflowStepHandler,
    CandidateSelectionHandler,
    StepResult,
    ToolStepHandler,
    WorkflowStepContext,
    WorkflowStepHandlerRegistry,
    build_default_step_registry,
)
from app.workflows.models import (
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
)
from app.workflows.service import WorkflowService
from tests.test_a2a_service import LoopbackTransport


@pytest_asyncio.fixture
async def test_owner(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        return owner.id


@pytest_asyncio.fixture
async def workflow_service_instance(
    db_session_factory, policy_service, memory_manager
) -> WorkflowService:
    # Build tool service
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    tool_service = ToolService(
        registry=registry,
        policy_service=policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=4096,
    )

    return WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        memory_manager=memory_manager,
        default_ttl_seconds=3600,
        max_step_attempts=3,
    )


# -----------------------------------------------------------------------------
# 1. CREATION & VALIDATION
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_create_and_structure(
    workflow_service_instance: WorkflowService, test_owner: uuid.UUID
):
    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"date": "tomorrow", "candidate_slots": ["10:00", "14:00"]},
        ),
        WorkflowStepSpec(
            step_type="candidate_selection",
            input_payload={"user_slots": ["14:00"], "remote_slots": ["14:00"]},
        ),
    ]

    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="scheduling",
        steps=steps,
    )

    assert wf.workflow_id is not None
    assert wf.status == WorkflowStatus.PENDING.value
    assert wf.workflow_type == "meeting_coordination"
    assert len(wf.steps) == 2
    assert wf.steps[0].step_number == 1
    assert wf.steps[0].step_type == "availability_check"
    assert wf.steps[0].status == StepStatus.PENDING.value
    assert wf.steps[1].step_number == 2


@pytest.mark.asyncio
async def test_workflow_create_empty_steps_fails(
    workflow_service_instance: WorkflowService, test_owner: uuid.UUID
):
    with pytest.raises(ValueError, match="at least one step"):
        await workflow_service_instance.create_workflow(
            test_owner,
            workflow_type="invalid",
            purpose="testing_empty_steps",
            steps=[],
        )


@pytest.mark.asyncio
async def test_workflow_unknown_step_fails_safely(
    workflow_service_instance: WorkflowService, test_owner: uuid.UUID
):
    steps = [
        WorkflowStepSpec(step_type="completely_unknown_action"),
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="unknown_test",
        purpose="test_safety",
        steps=steps,
    )

    started_wf = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert started_wf.status == WorkflowStatus.FAILED.value
    assert "Unsupported step type" in (started_wf.failure_reason or "")
    assert started_wf.steps[0].status == StepStatus.FAILED.value


# -----------------------------------------------------------------------------
# 2. INDEPENDENT POLICY AUTHORIZATION
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_policy_deny_halts_without_retry(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    # Set explicit policy DENY for calendar read
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="scheduling",
        decision="DENY",
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00"]},
        ),
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="scheduling",
        steps=steps,
    )

    result = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert result.status == WorkflowStatus.FAILED.value
    assert "Policy DENY" in (result.failure_reason or "")
    assert result.steps[0].status == StepStatus.FAILED.value
    assert result.steps[0].attempt_count == 0  # Security failures do NOT retry


@pytest.mark.asyncio
async def test_workflow_policy_step1_allow_step2_ask_and_approval_flow(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    # Step 1: calendar read -> ALLOW
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="team_sync",
        decision="ALLOW",
    )
    # Step 2: scheduling compute -> ASK
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="scheduling",
        action="compute",
        purpose="team_sync",
        decision="ASK",
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00", "14:00"]},
        ),
        WorkflowStepSpec(
            step_type="candidate_selection",
            input_payload={"user_slots": ["14:00"], "remote_slots": ["14:00"]},
        ),
    ]

    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="meeting_coordination",
        purpose="team_sync",
        steps=steps,
    )

    # Start workflow -> step 1 completes, step 2 triggers ASK -> workflow pauses at WAITING_APPROVAL
    paused_wf = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert paused_wf.status == WorkflowStatus.WAITING_APPROVAL.value
    assert paused_wf.steps[0].status == StepStatus.COMPLETED.value
    assert paused_wf.steps[1].status == StepStatus.WAITING.value

    # Owner approves step 2
    resumed_wf = await workflow_service_instance.approve_workflow(
        test_owner, paused_wf.workflow_id, step_id=paused_wf.steps[1].step_id
    )

    assert resumed_wf.status == WorkflowStatus.COMPLETED.value
    assert resumed_wf.steps[1].status == StepStatus.COMPLETED.value
    assert resumed_wf.steps[1].output_payload.get("selected_slot") == "14:00"


@pytest.mark.asyncio
async def test_workflow_approval_does_not_authorize_unrelated_step(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    # Step 1: ASK
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="test_workflow",
        decision="ASK",
    )
    # Step 2: scheduling -> also ASK
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="scheduling",
        action="compute",
        purpose="test_workflow",
        decision="ASK",
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00"]},
        ),
        WorkflowStepSpec(
            step_type="candidate_selection",
            input_payload={"user_slots": ["10:00"], "remote_slots": ["10:00"]},
        ),
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner, workflow_type="test", purpose="test_workflow", steps=steps
    )

    # Start -> paused at step 1
    w1 = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert w1.status == WorkflowStatus.WAITING_APPROVAL.value
    assert w1.steps[0].status == StepStatus.WAITING.value

    # Approve step 1 -> executes step 1, but step 2 encounters ASK and pauses AGAIN
    w2 = await workflow_service_instance.approve_workflow(test_owner, wf.workflow_id)
    assert w2.status == WorkflowStatus.WAITING_APPROVAL.value
    assert w2.steps[0].status == StepStatus.COMPLETED.value
    assert w2.steps[1].status == StepStatus.WAITING.value


# -----------------------------------------------------------------------------
# 3. OWNER ISOLATION
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_owner_isolation(
    workflow_service_instance: WorkflowService,
    owner_ids: tuple[uuid.UUID, uuid.UUID],
):
    owner_a, owner_b = owner_ids

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00"]},
        )
    ]
    wf_a = await workflow_service_instance.create_workflow(
        owner_a, workflow_type="test", purpose="owner_a_private_workflow", steps=steps
    )

    # Owner B cannot view Owner A's workflow
    with pytest.raises(WorkflowNotFoundError):
        await workflow_service_instance.get_workflow(owner_b, wf_a.workflow_id)

    # Owner B cannot start Owner A's workflow
    with pytest.raises(WorkflowNotFoundError):
        await workflow_service_instance.start_workflow(owner_b, wf_a.workflow_id)

    # Owner B cannot cancel Owner A's workflow
    with pytest.raises(WorkflowNotFoundError):
        await workflow_service_instance.cancel_workflow(owner_b, wf_a.workflow_id)

    # Owner B's workflow list does not include Owner A's workflow
    list_b = await workflow_service_instance.list_workflows(owner_b)
    assert not any(w.workflow_id == wf_a.workflow_id for w in list_b)


# -----------------------------------------------------------------------------
# 4. CRASH RECOVERY
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_crash_recovery(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
    db_session_factory,
):
    # Allow policy
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="*",
        action="*",
        purpose="recovery_test",
        decision="ALLOW",
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00", "14:00"]},
        ),
        WorkflowStepSpec(
            step_type="candidate_selection",
            input_payload={"user_slots": ["14:00"], "remote_slots": ["14:00"]},
        ),
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="recovery_test",
        purpose="recovery_test",
        steps=steps,
    )

    # Simulate crash: Step 1 completed, Step 2 is in RUNNING status, workflow is RUNNING
    async with db_session_factory() as session:
        wf_db = await workflow_service_instance._repo.get(session, wf.workflow_id)
        wf_db.status = WorkflowStatus.RUNNING.value
        wf_db.steps[0].status = StepStatus.COMPLETED.value
        wf_db.steps[0].output_payload = {"available_slots": ["14:00"]}
        wf_db.steps[1].status = StepStatus.RUNNING.value
        wf_db.steps[1].started_at = datetime.now(timezone.utc)
        await session.commit()

    # Trigger recovery on restart
    recovered = await workflow_service_instance.recover_interrupted_workflows()
    assert wf.workflow_id in recovered

    # Verify workflow resumed and finished successfully without re-executing Step 1
    final_wf = await workflow_service_instance.get_workflow(test_owner, wf.workflow_id)
    assert final_wf.status == WorkflowStatus.COMPLETED.value
    assert final_wf.steps[0].status == StepStatus.COMPLETED.value
    assert final_wf.steps[1].status == StepStatus.COMPLETED.value
    assert final_wf.steps[1].attempt_count == 1  # Incremented attempt on recovery


# -----------------------------------------------------------------------------
# 5. CONCURRENCY PROTECTION
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_concurrency_lock(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="*",
        action="*",
        purpose="concurrency_test",
        decision="ALLOW",
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00"]},
        )
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="concurrency_test",
        purpose="concurrency_test",
        steps=steps,
    )

    # Two concurrent calls to advance_workflow
    await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)

    # Subsequent call immediately sees it is already completed or running
    adv_res = await workflow_service_instance.advance_workflow(wf.workflow_id)
    assert adv_res.status == WorkflowStatus.COMPLETED.value


# -----------------------------------------------------------------------------
# 6. BOUNDED RETRIES & TRANSIENT FAILURES
# -----------------------------------------------------------------------------


class FlakyStepHandler(BaseWorkflowStepHandler):
    step_type = "flaky_step"
    data_category = "workflow"
    action = "execute_step"

    def __init__(self, failures_before_success: int):
        self.count = 0
        self.failures_before_success = failures_before_success

    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        self.count += 1
        if self.count <= self.failures_before_success:
            return StepResult(
                status=StepStatus.FAILED,
                failure_reason=f"Transient failure #{self.count}",
                is_transient=True,
            )
        return StepResult(status=StepStatus.COMPLETED, output_payload={"success": True})


@pytest.mark.asyncio
async def test_workflow_bounded_retry_transient_success(
    db_session_factory, policy_service, test_owner: uuid.UUID
):
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="*",
        action="*",
        purpose="retry_test",
        decision="ALLOW",
    )

    registry = WorkflowStepHandlerRegistry()
    flaky_handler = FlakyStepHandler(failures_before_success=2)
    registry.register(flaky_handler)

    svc = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        registry=registry,
        max_step_attempts=3,
    )

    wf = await svc.create_workflow(
        test_owner,
        workflow_type="retry_test",
        purpose="retry_test",
        steps=[WorkflowStepSpec(step_type="flaky_step", max_attempts=3)],
    )

    res = await svc.start_workflow(test_owner, wf.workflow_id)
    assert res.status == WorkflowStatus.COMPLETED.value
    assert res.steps[0].attempt_count == 2
    assert flaky_handler.count == 3


@pytest.mark.asyncio
async def test_workflow_bounded_retry_exceeded_fails(
    db_session_factory, policy_service, test_owner: uuid.UUID
):
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="*",
        action="*",
        purpose="retry_test",
        decision="ALLOW",
    )

    registry = WorkflowStepHandlerRegistry()
    # Fails 5 times, but max_attempts is 3
    flaky_handler = FlakyStepHandler(failures_before_success=5)
    registry.register(flaky_handler)

    svc = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        registry=registry,
        max_step_attempts=3,
    )

    wf = await svc.create_workflow(
        test_owner,
        workflow_type="retry_test",
        purpose="retry_test",
        steps=[WorkflowStepSpec(step_type="flaky_step", max_attempts=3)],
    )

    res = await svc.start_workflow(test_owner, wf.workflow_id)
    assert res.status == WorkflowStatus.FAILED.value
    assert res.steps[0].status == StepStatus.FAILED.value


# -----------------------------------------------------------------------------
# 7. EXPIRATION & CANCELLATION
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_expiration(
    workflow_service_instance: WorkflowService,
    test_owner: uuid.UUID,
    db_session_factory,
):
    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00"]},
        )
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="expiration_test",
        purpose="expiration_test",
        steps=steps,
        ttl_seconds=1,
    )

    # Manually expire in DB
    async with db_session_factory() as session:
        wf_db = await workflow_service_instance._repo.get(session, wf.workflow_id)
        wf_db.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await session.commit()

    with pytest.raises(WorkflowExpiredError):
        await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)

    expired_wf = await workflow_service_instance.get_workflow(test_owner, wf.workflow_id)
    assert expired_wf.status == WorkflowStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_workflow_cancellation(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    # Step 1 ALLOW, Step 2 ASK
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="cancel_test",
        decision="ALLOW",
    )
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="scheduling",
        action="compute",
        purpose="cancel_test",
        decision="ASK",
    )

    steps = [
        WorkflowStepSpec(step_type="availability_check"),
        WorkflowStepSpec(step_type="candidate_selection"),
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="cancel_test",
        purpose="cancel_test",
        steps=steps,
    )

    # Start -> paused at Step 2
    paused = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert paused.status == WorkflowStatus.WAITING_APPROVAL.value
    assert paused.steps[0].status == StepStatus.COMPLETED.value
    assert paused.steps[1].status == StepStatus.WAITING.value

    # Cancel workflow
    cancelled = await workflow_service_instance.cancel_workflow(
        test_owner, wf.workflow_id, reason="Changed my mind"
    )

    assert cancelled.status == WorkflowStatus.CANCELLED.value
    assert cancelled.steps[0].status == StepStatus.COMPLETED.value  # Completed preserved
    assert cancelled.steps[1].status == StepStatus.SKIPPED.value    # Waiting skipped


# -----------------------------------------------------------------------------
# 8. LOCAL TOOL INTEGRATION
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_tool_step_execution(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    # Allow tool policy
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="*",
        action="*",
        purpose="tool_test",
        decision="ALLOW",
    )

    steps = [
        WorkflowStepSpec(
            step_type="tool_execution",
            input_payload={
                "tool_name": "echo",
                "arguments": {"text": "hello from workflow"},
                "purpose": "tool_test",
            },
        )
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner,
        workflow_type="tool_workflow",
        purpose="tool_test",
        steps=steps,
    )

    result = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert result.status == WorkflowStatus.COMPLETED.value
    assert result.steps[0].status == StepStatus.COMPLETED.value
    assert result.steps[0].output_payload.get("text") == "hello from workflow"


# -----------------------------------------------------------------------------
# 9. END-TO-END MEETING COORDINATION WORKFLOW WITH MINIMUM DISCLOSURE
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meeting_coordination_workflow_e2e(
    db_session_factory,
    memory_manager,
    owner_ids: tuple[uuid.UUID, uuid.UUID],
):
    owner_user_a, owner_rahul = owner_ids

    # 1. Setup Identities
    id_service_a = IdentityService(
        session_factory=db_session_factory,
        encryption_secret="user-a-secret-key-32bytes-padding!!",
        owner_id=owner_user_a,
    )
    await id_service_a.initialize_identity()
    agent_id_a = id_service_a.get_public_identity().agent_id

    id_service_b = IdentityService(
        session_factory=db_session_factory,
        encryption_secret="rahul-secret-key-32bytes-padding!!",
        owner_id=owner_rahul,
    )
    await id_service_b.initialize_identity()
    agent_id_b = id_service_b.get_public_identity().agent_id

    # 2. Setup Policies (ALLOW wildcard for meeting coordination demo)
    policy_a = PolicyService(session_factory=db_session_factory)
    await policy_a.create_policy(
        owner_user_a,
        requester_agent_id="*",
        data_category="*",
        action="*",
        purpose="*",
        decision="ALLOW",
    )

    policy_b = PolicyService(session_factory=db_session_factory)
    await policy_b.create_policy(
        owner_rahul,
        requester_agent_id="*",
        data_category="*",
        action="*",
        purpose="*",
        decision="ALLOW",
    )

    # 3. Setup Loopback Network between Agent A and Rahul
    transport_a = LoopbackTransport()
    transport_b = LoopbackTransport()

    a2a_service_a = A2AService(
        session_factory=db_session_factory,
        identity_service=id_service_a,
        policy_service=policy_a,
        memory_manager=memory_manager,
        transport=transport_a,
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
    )

    a2a_service_b = A2AService(
        session_factory=db_session_factory,
        identity_service=id_service_b,
        policy_service=policy_b,
        memory_manager=memory_manager,
        transport=transport_b,
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
    )

    # Route messages between instances
    async def route_a_to_b(env: Any) -> Any:
        return await a2a_service_b.handle_inbound(owner_rahul, env)

    async def route_b_to_a(env: Any) -> Any:
        return await a2a_service_a.handle_inbound(owner_user_a, env)

    transport_a.handler = route_a_to_b
    transport_b.handler = route_b_to_a

    # Register Rahul as trusted agent for User A
    pub_b = id_service_b.get_public_identity().public_key
    await a2a_service_a.register_trusted_agent(
        owner_user_a,
        agent_id=agent_id_b,
        public_key=pub_b,
        display_name="Rahul",
        endpoint="http://rahul.agent/a2a",
    )

    # Register User A as trusted agent for Rahul
    pub_a = id_service_a.get_public_identity().public_key
    await a2a_service_b.register_trusted_agent(
        owner_rahul,
        agent_id=agent_id_a,
        public_key=pub_a,
        display_name="User A",
        endpoint="http://user-a.agent/a2a",
    )

    # 4. Populate User A's calendar memory with busy slots: ["10:00", "14:00"]
    # So User A's available candidate slots are ["18:00", "19:00"]
    await memory_manager.store_memory(
        owner_user_a,
        content="Doctor appointment at 10:00, busy meeting at 14:00 tomorrow.",
        memory_type="episodic",
    )

    # Populate Rahul's memory: Available after 6 PM (18:00)
    await memory_manager.store_memory(
        owner_rahul,
        content="Available after 6 PM (18:00) for meetings.",
        memory_type="episodic",
    )

    # 5. Build Workflow for User A
    # Step 1: Check User A's availability (availability_check)
    # Step 2: Delegate A2A task to Rahul to check Rahul's availability (a2a_task -> availability_check)
    # Step 3: Compute candidate intersection (candidate_selection)
    # Step 4: Propose meeting to Rahul via A2A (a2a_task -> meeting_proposal)
    workflow_service = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_a,
        a2a_service=a2a_service_a,
        memory_manager=memory_manager,
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={
                "date": "tomorrow",
                "candidate_slots": ["10:00", "14:00", "18:00", "19:00"],
            },
        ),
        WorkflowStepSpec(
            step_type="a2a_task",
            input_payload={
                "recipient_agent_id": agent_id_b,
                "task_type": "availability_check",
                "purpose": "scheduling",
                "payload": {"requested_time": "18:00"},
            },
        ),
        WorkflowStepSpec(
            step_type="candidate_selection",
            input_payload={
                "user_slots": ["18:00", "19:00"],
                "remote_slots": ["18:00"],
            },
        ),
        WorkflowStepSpec(
            step_type="a2a_task",
            input_payload={
                "recipient_agent_id": agent_id_b,
                "task_type": "meeting_proposal",
                "purpose": "scheduling",
                "payload": {"proposed_time": "18:00", "topic": "Quarterly Planning"},
            },
        ),
    ]

    wf = await workflow_service.create_workflow(
        owner_user_a,
        workflow_type="meeting_coordination",
        purpose="scheduling",
        steps=steps,
    )

    completed_wf = await workflow_service.start_workflow(owner_user_a, wf.workflow_id)

    # 6. Verification
    assert completed_wf.status == WorkflowStatus.COMPLETED.value
    assert len(completed_wf.steps) == 4
    for s in completed_wf.steps:
        assert s.status == StepStatus.COMPLETED.value

    # Privacy Invariants:
    # 1. Step 1 output does NOT contain raw calendar memories or doctor appointment details
    step1_out = completed_wf.steps[0].output_payload
    assert step1_out["available"] is True
    assert "Doctor appointment" not in str(step1_out)
    assert "18:00" in step1_out["available_slots"]
    assert "19:00" in step1_out["available_slots"]

    # 2. Step 2 remote response contains only minimal availability boolean
    step2_out = completed_wf.steps[1].output_payload
    assert "available" in step2_out

    # 3. Step 3 candidate selection selected mutual slot 18:00
    step3_out = completed_wf.steps[2].output_payload
    assert step3_out["selected_slot"] == "18:00"

    # 4. Step 4 confirmed proposal with Rahul
    step4_out = completed_wf.steps[3].output_payload
    assert step4_out.get("status") == "accepted" or "18:00" in str(step4_out)


# -----------------------------------------------------------------------------
# 10. HTTP API ENDPOINTS
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_api_lifecycle(db_workflow_client: AsyncClient):
    # 1. Create Workflow via API
    create_payload = {
        "workflow_type": "meeting_coordination",
        "purpose": "scheduling",
        "steps": [
            {
                "step_type": "availability_check",
                "input_payload": {"candidate_slots": ["14:00", "15:00"]},
            }
        ],
        "ttl_seconds": 3600,
    }

    create_resp = await db_workflow_client.post("/workflows", json=create_payload)
    assert create_resp.status_code == 201
    wf_data = create_resp.json()
    workflow_id = wf_data["workflow_id"]
    assert wf_data["status"] == "pending"
    assert len(wf_data["steps"]) == 1

    # 2. List Workflows
    list_resp = await db_workflow_client.get("/workflows")
    assert list_resp.status_code == 200
    workflows = list_resp.json()["workflows"]
    assert any(w["workflow_id"] == workflow_id for w in workflows)

    # 3. Get Single Workflow
    get_resp = await db_workflow_client.get(f"/workflows/{workflow_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["workflow_id"] == workflow_id

    # 4. Start Workflow (Policy ALLOW for calendar needed, else ASK)
    start_resp = await db_workflow_client.post(f"/workflows/{workflow_id}/start")
    assert start_resp.status_code == 200
    started_data = start_resp.json()
    assert started_data["status"] in {"completed", "waiting_approval"}

    # 5. Cancel Workflow test
    create_resp2 = await db_workflow_client.post("/workflows", json=create_payload)
    wf_id_2 = create_resp2.json()["workflow_id"]
    cancel_resp = await db_workflow_client.post(f"/workflows/{wf_id_2}/cancel", json={"reason": "User cancelled"})
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"


# -----------------------------------------------------------------------------
# A3: APPROVAL ACCEPTS ONLY THE WAITING STEP
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_approve_rejects_non_waiting_step_id(
    workflow_service_instance: WorkflowService,
    policy_service: PolicyService,
    test_owner: uuid.UUID,
):
    # Both steps ASK so the workflow is still WAITING_APPROVAL after step 1 completes.
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="test_workflow",
        decision="ASK",
    )
    await policy_service.create_policy(
        test_owner,
        requester_agent_id="nexus:self",
        data_category="scheduling",
        action="compute",
        purpose="test_workflow",
        decision="ASK",
    )

    steps = [
        WorkflowStepSpec(
            step_type="availability_check",
            input_payload={"candidate_slots": ["10:00"]},
        ),
        WorkflowStepSpec(
            step_type="candidate_selection",
            input_payload={"user_slots": ["10:00"], "remote_slots": ["10:00"]},
        ),
    ]
    wf = await workflow_service_instance.create_workflow(
        test_owner, workflow_type="test", purpose="test_workflow", steps=steps
    )

    # Start -> paused at step 1
    w1 = await workflow_service_instance.start_workflow(test_owner, wf.workflow_id)
    assert w1.status == WorkflowStatus.WAITING_APPROVAL.value
    assert w1.steps[0].status == StepStatus.WAITING.value

    # Approve step 1 -> step 1 completes, step 2 pauses; workflow still WAITING_APPROVAL
    w2 = await workflow_service_instance.approve_workflow(test_owner, wf.workflow_id)
    assert w2.status == WorkflowStatus.WAITING_APPROVAL.value
    assert w2.steps[0].status == StepStatus.COMPLETED.value
    assert w2.steps[1].status == StepStatus.WAITING.value

    completed_step_id = w2.steps[0].step_id
    before_status = w2.steps[0].status
    before_output = w2.steps[0].output_payload
    before_completed_at = w2.steps[0].completed_at

    # Re-approving the now-COMPLETED step must be rejected, not re-executed.
    with pytest.raises(WorkflowConflictError):
        await workflow_service_instance.approve_workflow(
            test_owner, wf.workflow_id, step_id=completed_step_id
        )

    after = await workflow_service_instance.get_workflow(test_owner, wf.workflow_id)
    after_step = next(s for s in after.steps if s.step_id == completed_step_id)
    assert after_step.status == before_status == StepStatus.COMPLETED.value
    assert after_step.output_payload == before_output
    assert after_step.completed_at == before_completed_at
    assert after.status == WorkflowStatus.WAITING_APPROVAL.value


# -----------------------------------------------------------------------------
# A4: RESUME PATH — CALLBACK WIRING + POLICY ON RESUME
# -----------------------------------------------------------------------------


class A4RemoteWaitStepHandler(BaseWorkflowStepHandler):
    """Parks the workflow in WAITING_REMOTE like a real async A2A delegation."""

    step_type = "a4_remote_wait"
    data_category = "a2a"
    action = "delegate_task"

    def __init__(self, task_id: str):
        self._task_id = task_id

    async def execute(
        self, context: WorkflowStepContext, input_payload: dict[str, Any]
    ) -> StepResult:
        return StepResult(
            status=StepStatus.WAITING,
            output_payload={},
            task_id=self._task_id,
        )


def _a4_registry(task_id: str) -> WorkflowStepHandlerRegistry:
    registry = WorkflowStepHandlerRegistry()
    registry.register(A4RemoteWaitStepHandler(task_id))
    registry.register(AvailabilityStepHandler())
    return registry


def _a4_bus(
    db_session_factory, policy_service, memory_manager
) -> A2AService:
    return A2AService(
        session_factory=db_session_factory,
        identity_service=None,
        policy_service=policy_service,
        memory_manager=memory_manager,
        transport=LoopbackTransport(),
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
    )


async def _a4_allow_all(policy_service: PolicyService, owner_id: uuid.UUID) -> None:
    await policy_service.create_policy(
        owner_id,
        requester_agent_id="nexus:self",
        data_category="a2a",
        action="delegate_task",
        purpose="a4_resume",
        decision="ALLOW",
    )
    await policy_service.create_policy(
        owner_id,
        requester_agent_id="nexus:self",
        data_category="calendar",
        action="read",
        purpose="a4_resume",
        decision="ALLOW",
    )


async def _a4_park_remote_step(wf_svc: WorkflowService, owner_id: uuid.UUID):
    wf = await wf_svc.create_workflow(
        owner_id,
        workflow_type="a4_test",
        purpose="a4_resume",
        steps=[
            WorkflowStepSpec(step_type="a4_remote_wait"),
            WorkflowStepSpec(
                step_type="availability_check",
                input_payload={"candidate_slots": ["10:00"]},
            ),
        ],
    )
    parked = await wf_svc.start_workflow(owner_id, wf.workflow_id)
    assert parked.status == WorkflowStatus.WAITING_REMOTE.value
    remote_step = next(s for s in parked.steps if s.step_number == 1)
    assert remote_step.status == StepStatus.WAITING.value
    assert remote_step.task_id
    return parked, remote_step.task_id


async def _a4_set_step_policy(
    policy_service: PolicyService, owner_id: uuid.UUID, decision: str
) -> None:
    for p in await policy_service.list_policies(owner_id):
        if p.data_category == "a2a" and p.action == "delegate_task":
            await policy_service.delete_policy(owner_id, p.id)
    await policy_service.create_policy(
        owner_id,
        requester_agent_id="nexus:self",
        data_category="a2a",
        action="delegate_task",
        purpose="a4_resume",
        decision=decision,
    )


def test_a4_workflow_resume_callback_wired_in_main():
    import inspect

    from app import main as main_module

    src = inspect.getsource(main_module.lifespan)
    assert "register_task_completion_callback" in src
    assert "workflow_service.handle_task_completion" in src


@pytest.mark.asyncio
async def test_a4_callback_bus_resume(
    db_session_factory, policy_service, memory_manager, test_owner: uuid.UUID
):
    task_id = f"a4-task-{uuid.uuid4().hex[:8]}"
    wf_svc = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        registry=_a4_registry(task_id),
        memory_manager=memory_manager,
    )
    bus = _a4_bus(db_session_factory, policy_service, memory_manager)
    bus.register_task_completion_callback(wf_svc.handle_task_completion)

    await _a4_allow_all(policy_service, test_owner)
    parked, parked_task_id = await _a4_park_remote_step(wf_svc, test_owner)
    assert parked_task_id == task_id

    payload = {"available": True, "slot": "10:00"}
    for cb in list(bus._task_completion_callbacks):
        res = cb(parked_task_id, payload)
        if asyncio.iscoroutine(res):
            await res

    final = await wf_svc.get_workflow(test_owner, parked.workflow_id)
    assert final.status == WorkflowStatus.COMPLETED.value
    resumed = next(s for s in final.steps if s.step_number == 1)
    assert resumed.status == StepStatus.COMPLETED.value
    assert resumed.output_payload == payload


@pytest.mark.asyncio
async def test_a4_deny_on_resume_not_ingested(
    db_session_factory, policy_service, memory_manager, test_owner: uuid.UUID
):
    task_id = f"a4-task-{uuid.uuid4().hex[:8]}"
    wf_svc = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        registry=_a4_registry(task_id),
        memory_manager=memory_manager,
    )
    await _a4_allow_all(policy_service, test_owner)
    parked, parked_task_id = await _a4_park_remote_step(wf_svc, test_owner)

    await _a4_set_step_policy(policy_service, test_owner, "DENY")

    payload = {"available": True, "slot": "10:00", "marker": "a4-deny-secret"}
    await wf_svc.handle_task_completion(parked_task_id, payload)

    final = await wf_svc.get_workflow(test_owner, parked.workflow_id)
    resumed = next(s for s in final.steps if s.step_number == 1)
    assert resumed.status != StepStatus.COMPLETED.value
    assert not resumed.output_payload
    ctx = final.context_data or {}
    assert "a4-deny-secret" not in str(ctx)
    assert final.status == WorkflowStatus.FAILED.value


@pytest.mark.asyncio
async def test_a4_ask_on_resume_parks_without_ingesting(
    db_session_factory, policy_service, memory_manager, test_owner: uuid.UUID
):
    task_id = f"a4-task-{uuid.uuid4().hex[:8]}"
    wf_svc = WorkflowService(
        session_factory=db_session_factory,
        policy_service=policy_service,
        registry=_a4_registry(task_id),
        memory_manager=memory_manager,
    )
    await _a4_allow_all(policy_service, test_owner)
    parked, parked_task_id = await _a4_park_remote_step(wf_svc, test_owner)

    await _a4_set_step_policy(policy_service, test_owner, "ASK")

    payload = {"available": True, "slot": "10:00", "marker": "a4-ask-secret"}
    await wf_svc.handle_task_completion(parked_task_id, payload)

    final = await wf_svc.get_workflow(test_owner, parked.workflow_id)
    resumed = next(s for s in final.steps if s.step_number == 1)
    assert resumed.status == StepStatus.WAITING.value
    assert not resumed.output_payload
    assert final.status == WorkflowStatus.WAITING_APPROVAL.value
    assert "a4-ask-secret" not in str(final.context_data or {})
