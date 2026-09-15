"""Nexus Part 10: Autonomy & Decision Engine — End-to-End Demo.

Demonstrates:
  1. Bounded Autonomous Goal Planning & Step-by-step Evaluation
  2. Safe Execution with Human-in-the-Loop Approval Pause & Resume
  3. Hard Budget Limits (max steps enforced by Decision Engine)
  4. Authoritative Policy Deny (LLM / Plan cannot bypass PolicyService)
  5. Complete Tamper-Evident Durable Audit Trail
  6. Startup Crash Recovery & Reconciliation
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.a2a.service import A2AService
from app.autonomy.models import (
    ActionType,
    ApprovalStatus,
    AutonomyMode,
    DecisionResult,
    RiskLevel,
    RunStatus,
)
from app.autonomy.planner import PlanAction
from app.autonomy.service import AutonomyService
from app.config.settings import get_settings
from app.database.repositories import OwnerRepository
from app.identity.service import IdentityService
from app.memory.embeddings import LocalHashEmbeddingProvider
from app.memory.manager import MemoryManager
from app.policy.service import PolicyService
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService
from app.workflows.service import WorkflowService

# Suppress debug logs for clean demo output
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("nexus.autonomy.demo")


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Initialize supporting services
    embeddings = LocalHashEmbeddingProvider()
    memory_manager = MemoryManager(session_factory=session_factory, embeddings=embeddings)
    policy_service = PolicyService(session_factory=session_factory)

    tool_registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        tool_registry.register(tool)
    tool_service = ToolService(
        registry=tool_registry,
        policy_service=policy_service,
        session_factory=session_factory,
    )

    async with session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        owner_id = owner.id

    identity_service = IdentityService(
        session_factory=session_factory,
        encryption_secret=settings.nexus_identity_key,
        owner_id=owner_id,
    )
    await identity_service.initialize_identity()
    workflow_service = WorkflowService(
        session_factory=session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        memory_manager=memory_manager,
    )

    # Initialize Part 10 AutonomyService
    autonomy_service = AutonomyService(
        session_factory=session_factory,
        workflow_service=workflow_service,
        tool_service=tool_service,
        memory_manager=memory_manager,
        policy_service=policy_service,
    )

    print("\n" + "=" * 75)
    print("  NEXUS PART 10: CONTROLLED AUTONOMY & DECISION ENGINE")
    print("  Safe, Policy-Governed, Human-in-the-Loop Execution")
    print("=" * 75)

    # -------------------------------------------------------------------------
    # Scenario 1: Bounded Autonomous Plan & Approval Pause/Resume
    # -------------------------------------------------------------------------
    print("\n[Scenario 1] Bounded Autonomous Coordination with Approval Pause & Resume")
    print("-" * 75)

    # Configure owner for BOUNDED autonomy mode
    config = await autonomy_service.update_config(
        owner_id,
        mode=AutonomyMode.BOUNDED.value,
        max_steps_per_run=10,
        require_approval_for_external=True,
    )
    print(f"-> Owner autonomy configured: Mode='{config.mode}', MaxSteps={config.max_steps_per_run}")

    # Seed a memory so step 1 can retrieve context
    await memory_manager.store_memory(
        owner_id=owner_id,
        memory_type="semantic",
        content="Prefers lunch meetings between 12:00 and 13:30 on weekdays",
    )

    goal = "Coordinate lunch meeting with Sarah on Thursday"
    custom_plan = [
        {
            "action_type": ActionType.READ_MEMORY.value,
            "purpose": "check_preferences",
            "proposed_action": "Read owner lunch schedule preferences from memory",
            "payload": {"query": "lunch meeting preferences"},
        },
        {
            "action_type": ActionType.READ_CONTEXT.value,
            "purpose": "draft_proposal",
            "proposed_action": "Synthesize lunch proposal for Thursday 12:30 PM",
        },
        {
            "action_type": ActionType.REQUEST_APPROVAL.value,
            "purpose": "send_invitation",
            "proposed_action": "Send lunch invitation to Sarah for Thursday 12:30 PM",
            "payload": {"target_contact": "Sarah", "time": "Thursday 12:30 PM", "venue": "Cafe Roma"},
        },
        {
            "action_type": ActionType.UPDATE_MEMORY.value,
            "purpose": "record_calendar",
            "proposed_action": "Record confirmed meeting in semantic memory",
            "payload": {"content": "Lunch meeting with Sarah scheduled for Thursday 12:30 PM at Cafe Roma"},
        },
    ]

    print(f"-> Goal submitted: '{goal}'")
    print(f"-> Planner created bounded 4-step execution plan.")

    run = await autonomy_service.create_run(
        owner_id,
        goal=goal,
        custom_plan=custom_plan,
        execute_immediately=True,
    )

    print(f"-> Execution paused! Run status: '{run.status.upper()}' at Step {run.current_step}/4")
    assert run.status == RunStatus.WAITING_APPROVAL.value
    assert len(run.approvals) >= 1

    pending_appr = run.approvals[0]
    print(f"\n   [PENDING APPROVAL NOTIFICATION]")
    print(f"   Approval ID   : {pending_appr.approval_id}")
    print(f"   Action        : {pending_appr.requested_action}")
    print(f"   Purpose       : {pending_appr.purpose}")
    print(f"   Risk Level    : {pending_appr.risk_level.upper()}")
    print(f"   Payload       : {pending_appr.required_data}")

    # Owner reviews and grants approval
    print(f"\n-> Owner reviews request and grants approval...")
    resumed_run = await autonomy_service.approve_run(
        owner_id,
        run.id,
        notes="Approved by owner: Thursday 12:30 PM works great.",
    )
    print(f"-> Execution resumed! Final status: '{resumed_run.status.upper()}'")
    print(f"-> Steps executed: {resumed_run.steps_executed}, Completed at: {resumed_run.completed_at}")
    assert resumed_run.status == RunStatus.COMPLETED.value

    # -------------------------------------------------------------------------
    # Scenario 2: Hard Budget Limit STOP Enforcement
    # -------------------------------------------------------------------------
    print("\n[Scenario 2] Hard Budget Limit STOP Enforcement")
    print("-" * 75)

    # Set strict step limit to 2
    await autonomy_service.update_config(owner_id, max_steps_per_run=2)
    print("-> Updated owner policy limit: max_steps_per_run = 2")

    budget_plan = [
        {"action_type": ActionType.READ_CONTEXT.value, "purpose": "step1", "proposed_action": "Read context 1"},
        {"action_type": ActionType.READ_CONTEXT.value, "purpose": "step2", "proposed_action": "Read context 2"},
        {"action_type": ActionType.READ_CONTEXT.value, "purpose": "step3", "proposed_action": "Attempt step 3"},
    ]

    budget_run = await autonomy_service.create_run(
        owner_id,
        goal="Perform 3 context reads under limit of 2",
        custom_plan=budget_plan,
        execute_immediately=True,
    )

    print(f"-> Run status: '{budget_run.status.upper()}'")
    print(f"-> Stop reason: {budget_run.stop_reason}")
    print(f"-> Total steps executed: {budget_run.steps_executed} (Hard capped at 2)")
    assert budget_run.status == RunStatus.STOPPED.value

    # Restore step limit
    await autonomy_service.update_config(owner_id, max_steps_per_run=10)

    # -------------------------------------------------------------------------
    # Scenario 3: Deterministic PolicyService DENY (Security Authority)
    # -------------------------------------------------------------------------
    print("\n[Scenario 3] Deterministic Policy Engine DENY Authority")
    print("-" * 75)

    # Create an explicit DENY policy in PolicyService
    await policy_service.create_policy(
        owner_id=owner_id,
        requester_agent_id="*",
        data_category="confidential",
        action="execute_tool",
        purpose="*",
        decision="DENY",
    )
    print("-> Policy registered in PolicyService: data_category='confidential' -> DENY")

    forbidden_plan = [
        {
            "action_type": ActionType.EXECUTE_TOOL.value,
            "purpose": "data_cleanup",
            "proposed_action": "Process confidential data",
            "tool_name": "database_cleanup",
            "required_data_categories": ["confidential"],
        }
    ]

    deny_run = await autonomy_service.create_run(
        owner_id,
        goal="Attempt unauthorized critical data cleanup",
        custom_plan=forbidden_plan,
        execute_immediately=True,
    )

    print(f"-> Run status: '{deny_run.status.upper()}'")
    print(f"-> Failure reason: '{deny_run.failure_reason}'")
    assert deny_run.status == RunStatus.FAILED.value
    print("-> Safety invariant verified: LLM/Planner cannot bypass PolicyService!")

    # -------------------------------------------------------------------------
    # Scenario 4: Tamper-Evident Durable Audit Trail
    # -------------------------------------------------------------------------
    print("\n[Scenario 4] Tamper-Evident Durable Audit Trail Inspection")
    print("-" * 75)

    decisions = await autonomy_service.list_decisions(owner_id, resumed_run.id)
    audits = await autonomy_service.list_audits(owner_id, resumed_run.id)
    print(f"-> Audit for Run: {resumed_run.id}")
    print(f"   Total decisions recorded: {len(decisions)}")
    print(f"   Total audit triggers recorded: {len(audits)}")

    print("\n   Decisions Log:")
    for idx, dec in enumerate(decisions, start=1):
        print(
            f"   [{idx}] Action: {dec.action_type:<18} | "
            f"Risk: {dec.risk_level:<8} | "
            f"Decision: {dec.decision:<6} | "
            f"Reason: {dec.reason}"
        )

    # -------------------------------------------------------------------------
    # Scenario 5: Crash Recovery & Reconciliation
    # -------------------------------------------------------------------------
    print("\n[Scenario 5] Crash Recovery & Orphaned Run Reconciliation")
    print("-" * 75)

    # Simulate an interrupted run left in RUNNING state
    orphan_run = await autonomy_service.create_run(
        owner_id,
        goal="Interrupted long-running task",
        custom_plan=[{"action_type": ActionType.READ_CONTEXT.value, "purpose": "step1", "proposed_action": "Context"}],
        execute_immediately=False,
    )
    async with session_factory() as sess:
        o_run = await sess.get(type(orphan_run), orphan_run.id)
        if o_run:
            o_run.status = RunStatus.RUNNING.value
            await sess.commit()

    print(f"-> Simulated server abrupt crash while run {orphan_run.id} was in RUNNING status")
    recovered = await autonomy_service.reconcile_on_startup()
    print(f"-> System restart triggered reconcile_on_startup(): {recovered} runs recovered")

    checked_orphan = await autonomy_service.get_run(owner_id, orphan_run.id)
    print(f"-> Recovered run status: '{checked_orphan.status.upper()}' | Reason: '{checked_orphan.stop_reason}'")
    assert checked_orphan.status == RunStatus.STOPPED.value

    print("\n" + "=" * 75)
    print("  PART 10 DEMO COMPLETED SUCCESSFULLY: ALL INVARIANTS VERIFIED!")
    print("=" * 75 + "\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
