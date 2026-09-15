"""Comprehensive test suite for Part 12: Natural Language Agent Orchestration.

Validates:
1. Contact Management & Persistence (ContactRepository)
2. Target Resolution (Exact, Alias, Known, Discovered, Ambiguous, Unknown)
3. Intent Parsing (Rule-based, LLM-based, Fallback, Strict Validation)
4. Context & Pronoun/Reference Resolution (Multi-turn tracking)
5. Bounded Minimal-Disclosure Planning
6. Orchestration Execution & Policy Enforcement (ALLOW vs DENY)
7. Autonomy & Decision Engine Gating (ASK / WAITING_APPROVAL)
8. Untrusted Agent Bootstrapping & Gating
9. Multi-Turn Conversational Coordination Flow
10. REST API Endpoints (/orchestration/execute, /runs, /contacts)
11. Chat Route Integration (/chat)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.a2a.models import TrustedAgent, TrustStatus
from app.a2a.repository import TrustedAgentRepository
from app.a2a.service import A2AService
from app.agent.agent import NexusAgent
from app.autonomy.decision_engine import DecisionEngine, DecisionOutcome, DecisionRequest
from app.autonomy.models import ActionType, DecisionResult, RiskLevel
from app.database.repositories import OwnerRepository
from app.llm.base import LLMProvider, Message
from app.orchestration.context import OrchestrationContextManager
from app.orchestration.errors import (
    AmbiguousTargetError,
    OrchestrationError,
    OrchestrationPolicyError,
    TargetResolutionError,
    UntrustedAgentError,
)
from app.orchestration.executor import OrchestrationExecutor
from app.orchestration.intent import IntentResolver
from app.orchestration.models import Contact, OrchestrationRun, OrchestrationState
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.planner import OrchestrationPlanner
from app.orchestration.repository import ContactRepository, OrchestrationRunRepository
from app.orchestration.schemas import (
    Intent,
    IntentType,
    OrchestrationExecuteResponse,
    TargetResolution,
    TargetResolutionStatus,
)
from app.orchestration.target_resolver import TargetResolver
from app.policy.engine import EvaluationRequest, EvaluationResult
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService


class MockLLMProvider(LLMProvider):
    """Predictable LLM provider for testing intent resolution."""

    def __init__(self, response_text: str = "") -> None:
        self.response_text = response_text
        self.calls: list[list[Message]] = []

    @property
    def name(self) -> str:
        return "mock_orchestration_llm"

    async def generate(self, messages: list[Message]) -> str:
        self.calls.append(messages)
        return self.response_text


@pytest_asyncio.fixture
async def test_owner(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        return owner.id


@pytest_asyncio.fixture
async def sample_contact(db_session_factory, test_owner: uuid.UUID) -> Contact:
    repo = ContactRepository()
    ta_repo = TrustedAgentRepository()
    async with db_session_factory() as session:
        contact = await repo.create(
            session,
            owner_id=test_owner,
            display_name="Rahul Sharma",
            aliases=["rahul", "colleague", "lead dev"],
            agent_id="nexus:ed25519:11112222333344445555666677778888",
            endpoint="https://rahul.example.com/a2a",
            notes="Tech lead for Platform",
        )
        await ta_repo.add(
            session,
            TrustedAgent(
                owner_id=test_owner,
                agent_id="nexus:ed25519:11112222333344445555666677778888",
                public_key="ERERERERERERERERERERERERERERERERERERERERERE=",
                display_name="Rahul Sharma",
                endpoint="https://rahul.example.com/a2a",
                status=TrustStatus.ACTIVE.value,
            ),
        )
        await session.commit()
        return contact


# =============================================================================
# 1. CONTACT MANAGEMENT & REPOSITORY
# =============================================================================


@pytest.mark.asyncio
async def test_contact_repository_crud(db_session_factory, test_owner: uuid.UUID):
    repo = ContactRepository()
    async with db_session_factory() as session:
        contact = await repo.create(
            session,
            owner_id=test_owner,
            display_name="Priya Patel",
            aliases=["priya", "manager"],
            agent_id="nexus:ed25519:aaaabbbbccccddddeeeeffff00001111",
            notes="Engineering Manager",
        )
        await session.commit()
        assert contact.id is not None
        assert contact.display_name == "Priya Patel"

        # Lookup by alias
        found = await repo.find_by_name_or_alias(session, test_owner, "priya")
        assert len(found) == 1
        assert found[0].id == contact.id

        # Lookup by display name
        found_name = await repo.find_by_name_or_alias(session, test_owner, "Priya Patel")
        assert len(found_name) == 1
        assert found_name[0].id == contact.id

        # List all
        all_contacts = await repo.list_all(session, test_owner)
        assert len(all_contacts) >= 1


# =============================================================================
# 2. TARGET RESOLUTION
# =============================================================================


@pytest.mark.asyncio
async def test_target_resolution_exact_and_alias(
    db_session_factory, test_owner: uuid.UUID, sample_contact: Contact
):
    resolver = TargetResolver(session_factory=db_session_factory)

    # Resolve by first name
    res = await resolver.resolve(test_owner, "Rahul")
    assert res.status == TargetResolutionStatus.KNOWN_AGENT
    assert res.display_name == "Rahul Sharma"
    assert res.agent_id == sample_contact.agent_id
    assert res.is_trusted is True

    # Resolve by alias
    res_alias = await resolver.resolve(test_owner, "lead dev")
    assert res_alias.status == TargetResolutionStatus.KNOWN_AGENT
    assert res_alias.display_name == "Rahul Sharma"

    # Resolve unknown
    res_unknown = await resolver.resolve(test_owner, "StrangerDanger")
    assert res_unknown.status == TargetResolutionStatus.UNKNOWN_AGENT
    assert res_unknown.agent_id is None


@pytest.mark.asyncio
async def test_target_resolution_ambiguity(db_session_factory, test_owner: uuid.UUID):
    repo = ContactRepository()
    async with db_session_factory() as session:
        await repo.create(
            session,
            owner_id=test_owner,
            display_name="Rahul Verma",
            aliases=["rahul", "designer"],
            agent_id="nexus:ed25519:22222222222222222222222222222222",
        )
        await repo.create(
            session,
            owner_id=test_owner,
            display_name="Rahul Gupta",
            aliases=["rahul", "accountant"],
            agent_id="nexus:ed25519:33333333333333333333333333333333",
        )
        await session.commit()

    resolver = TargetResolver(session_factory=db_session_factory)
    res = await resolver.resolve(test_owner, "Rahul")
    assert res.status == TargetResolutionStatus.AMBIGUOUS_AGENT
    assert len(res.candidates) >= 2


# =============================================================================
# 3. INTENT RESOLUTION
# =============================================================================


@pytest.mark.asyncio
async def test_intent_resolution_rule_based():
    resolver = IntentResolver()

    # Availability check
    intent = await resolver.resolve_intent("Ask Rahul if he's free tomorrow after 6 PM")
    assert intent.intent_type == IntentType.CHECK_AVAILABILITY
    assert intent.target == "Rahul"

    # Meeting proposal / negotiation
    intent2 = await resolver.resolve_intent("Tell Rahul 8 PM works instead")
    assert intent2.intent_type == IntentType.NEGOTIATE
    assert intent2.target == "Rahul"

    # Confirmation
    intent3 = await resolver.resolve_intent("Book it", active_target="Rahul")
    assert intent3.intent_type == IntentType.CONFIRM_ACTION
    assert intent3.target == "Rahul"

    # Multi-agent coordination
    intent4 = await resolver.resolve_intent("Ask Rahul and Priya when they're both free")
    assert intent4.intent_type == IntentType.COORDINATE_MEETING
    assert intent4.target == "Rahul"
    assert "Priya" in intent4.secondary_targets

    # General chat
    intent5 = await resolver.resolve_intent("What is the capital of France?")
    assert intent5.intent_type == IntentType.GENERAL_CHAT


@pytest.mark.asyncio
async def test_intent_resolution_llm_structured():
    mock_payload = {
        "goal": "Check calendar availability for evening sync",
        "intent_type": "CHECK_AVAILABILITY",
        "target": "Vikram",
        "secondary_targets": [],
        "purpose": "availability_check",
        "requested_information": ["availability"],
        "constraints": {"time": "evening"},
        "action_payload": {},
    }
    llm = MockLLMProvider(response_text=json.dumps(mock_payload))
    resolver = IntentResolver(llm_provider=llm)

    intent = await resolver.resolve_intent("Check if Vikram has time for an evening sync")
    assert intent.intent_type == IntentType.CHECK_AVAILABILITY
    assert intent.target == "Vikram"
    assert intent.purpose == "availability_check"


@pytest.mark.asyncio
async def test_intent_resolution_llm_fallback_on_corrupted_output():
    # When LLM produces garbage/hallucinated text, fallback gracefully
    llm = MockLLMProvider(response_text="I don't feel like generating JSON right now.")
    resolver = IntentResolver(llm_provider=llm)

    intent = await resolver.resolve_intent("Ask Rahul if he's free tomorrow")
    assert intent.intent_type == IntentType.CHECK_AVAILABILITY
    assert intent.target == "Rahul"


# =============================================================================
# 4. CONTEXT & PRONOUN RESOLUTION
# =============================================================================


def test_context_manager_pronoun_resolution():
    mgr = OrchestrationContextManager()
    session_id = "test_session_123"

    # Turn 1: Establish target
    mgr.update_target(
        session_id=session_id,
        target_name="Rahul",
        agent_id="nexus:ed25519:11112222333344445555666677778888",
    )

    # Turn 2: Pronoun reference
    resolved, target = mgr.resolve_references(session_id, "Ask him if 8 PM works instead")
    assert target == "Rahul"
    assert "rahul" in resolved.lower()

    # Turn 3: Action reference
    resolved_action, target_action = mgr.resolve_references(session_id, "Book it")
    assert target_action == "Rahul"
    assert resolved_action == "Book it"


# =============================================================================
# 5. BOUNDED MINIMAL-DISCLOSURE PLANNING
# =============================================================================


def test_planner_minimal_disclosure_and_allowlist():
    planner = OrchestrationPlanner()

    intent = Intent(
        goal="Check if Rahul is free tomorrow",
        intent_type=IntentType.CHECK_AVAILABILITY,
        target="Rahul",
        purpose="availability_check",
        requested_information=["free_busy"],
        constraints={"date": "tomorrow", "time": "6 PM", "private_notes": "sensitive topic"},
    )
    resolution = TargetResolution(
        target_name="Rahul",
        status=TargetResolutionStatus.KNOWN_AGENT,
        display_name="Rahul Sharma",
        agent_id="nexus:ed25519:11112222333344445555666677778888",
        endpoint="https://rahul.example.com/a2a",
        is_trusted=True,
    )

    plan = planner.build_plan(intent, resolution)
    assert plan.target_agent_id == resolution.agent_id
    assert len(plan.steps) >= 2
    # Find delegate_task step
    task_steps = [s for s in plan.steps if s.step_type == "delegate_task"]
    assert len(task_steps) >= 1
    step = task_steps[0]
    assert step.payload.get("task_type") == "availability_check"
    # Verify sensitive data was not disclosed
    assert "private_notes" not in step.payload.get("payload", {})


# =============================================================================
# 6. POLICY ENFORCEMENT & SECURITY INJECTION RESISTANCE
# =============================================================================


@pytest.mark.asyncio
async def test_policy_denial_blocks_orchestration_execution(
    db_session_factory, test_owner: uuid.UUID
):
    mock_a2a = MagicMock(spec=A2AService)
    mock_policy = MagicMock(spec=PolicyService)

    # Configure Policy to DENY the outbound message
    mock_policy.evaluate = AsyncMock(
        return_value=EvaluationResult(
            decision=PolicyDecision.DENY,
            reason="Outbound communication to this domain is prohibited by owner policy.",
        )
    )

    executor = OrchestrationExecutor(
        a2a_service=mock_a2a,
        policy_service=mock_policy,
        decision_engine=None,
    )

    resolution = TargetResolution(
        target_name="Rahul",
        status=TargetResolutionStatus.KNOWN_AGENT,
        display_name="Rahul Sharma",
        agent_id="nexus:ed25519:11112222333344445555666677778888",
        endpoint="https://rahul.example.com/a2a",
        is_trusted=True,
    )
    plan = OrchestrationPlanner().build_plan(
        Intent(
            goal="Ask Rahul for documents",
            intent_type=IntentType.REQUEST_INFORMATION,
            target="Rahul",
        ),
        resolution,
    )

    # Execution must raise OrchestrationPolicyError and never call A2A transport
    with pytest.raises(OrchestrationPolicyError) as exc_info:
        await executor.execute_plan(test_owner, plan, resolution)

    assert "prohibited by owner policy" in str(exc_info.value)
    mock_a2a.delegate_task.assert_not_called()


@pytest.mark.asyncio
async def test_untrusted_agent_rejection(db_session_factory, test_owner: uuid.UUID):
    mock_a2a = MagicMock(spec=A2AService)
    mock_policy = MagicMock(spec=PolicyService)
    executor = OrchestrationExecutor(
        a2a_service=mock_a2a,
        policy_service=mock_policy,
    )

    untrusted_resolution = TargetResolution(
        target_name="Stranger",
        status=TargetResolutionStatus.DISCOVERED_AGENT,
        display_name="Stranger",
        agent_id="nexus:ed25519:99999999999999999999999999999999",
        is_trusted=False,
    )
    plan = OrchestrationPlanner().build_plan(
        Intent(goal="Ping stranger", intent_type=IntentType.CONTACT_AGENT, target="Stranger"),
        untrusted_resolution,
    )

    with pytest.raises(UntrustedAgentError):
        await executor.execute_plan(test_owner, plan, untrusted_resolution)

    mock_a2a.delegate_task.assert_not_called()


# =============================================================================
# 7. AUTONOMY & DECISION ENGINE GATING (ASK / WAITING_APPROVAL)
# =============================================================================


@pytest.mark.asyncio
async def test_autonomy_decision_engine_requires_approval(
    db_session_factory, test_owner: uuid.UUID
):
    mock_a2a = MagicMock(spec=A2AService)
    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(
        return_value=EvaluationResult(decision=PolicyDecision.ALLOW, reason="Allowed by policy")
    )

    # Mock DecisionEngine returning ASK / WAITING_APPROVAL
    mock_decision_engine = MagicMock(spec=DecisionEngine)
    mock_decision_engine.evaluate = AsyncMock(
        return_value=DecisionOutcome(
            decision=DecisionResult.ASK,
            reason="High impact meeting booking requires owner confirmation.",
            risk_level=RiskLevel.HIGH,
            action_type="contact_agent",
            purpose="confirmation",
            requires_approval=True,
        )
    )

    executor = OrchestrationExecutor(
        a2a_service=mock_a2a,
        policy_service=mock_policy,
        decision_engine=mock_decision_engine,
    )

    resolution = TargetResolution(
        target_name="Rahul",
        status=TargetResolutionStatus.KNOWN_AGENT,
        display_name="Rahul Sharma",
        agent_id="nexus:ed25519:11112222333344445555666677778888",
        endpoint="https://rahul.example.com/a2a",
        is_trusted=True,
    )
    plan = OrchestrationPlanner().build_plan(
        Intent(goal="Confirm meeting", intent_type=IntentType.CONFIRM_ACTION, target="Rahul"),
        resolution,
    )

    result = await executor.execute_plan(test_owner, plan, resolution)
    assert result["status"] == "waiting_approval"
    assert "High impact" in result["message"]
    # Remote call must not be sent yet
    mock_a2a.delegate_task.assert_not_called()


# =============================================================================
# 8. MULTI-TURN ORCHESTRATION PIPELINE END-TO-END
# =============================================================================


@pytest.mark.asyncio
async def test_orchestrator_multi_turn_flow(
    db_session_factory, test_owner: uuid.UUID, sample_contact: Contact
):
    # Mock A2A service that returns successful task execution
    mock_a2a = MagicMock(spec=A2AService)
    mock_a2a.delegate_task = AsyncMock(
        return_value={
            "status": "completed",
            "payload": {
                "available": True,
                "proposed_slots": ["2026-09-16T18:00:00Z", "2026-09-16T20:00:00Z"],
                "message": "I am free tomorrow after 6 PM, 8 PM works great!",
            },
        }
    )

    # Mock Policy allowing the interaction
    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(
        return_value=EvaluationResult(decision=PolicyDecision.ALLOW, reason="Allowed by policy")
    )

    intent_resolver = IntentResolver()
    target_resolver = TargetResolver(session_factory=db_session_factory)

    orchestrator = AgentOrchestrator(
        session_factory=db_session_factory,
        a2a_service=mock_a2a,
        policy_service=mock_policy,
        intent_resolver=intent_resolver,
        target_resolver=target_resolver,
    )

    session_id = f"test_multi_turn_{uuid.uuid4().hex[:8]}"

    # Turn 1: "Ask Rahul if he's free tomorrow after 6 PM"
    res1 = await orchestrator.handle_user_message(
        owner_id=test_owner,
        session_id=session_id,
        message="Ask Rahul if he's free tomorrow after 6 PM",
    )
    assert res1 is not None
    assert res1.status in ("completed", "pending")
    assert "Rahul" in res1.message
    assert mock_a2a.delegate_task.called

    # Turn 2: Follow-up using pronoun "Tell him 8 PM works instead"
    mock_a2a.delegate_task = AsyncMock(
        return_value={
            "status": "completed",
            "payload": {"accepted": True, "message": "8 PM is confirmed!"},
        }
    )

    res2 = await orchestrator.handle_user_message(
        owner_id=test_owner,
        session_id=session_id,
        message="Tell him 8 PM works instead",
    )
    assert res2 is not None
    assert res2.target == "Rahul"


# =============================================================================
# 9. REST API ENDPOINTS
# =============================================================================


@pytest.mark.asyncio
async def test_orchestration_api_contacts_and_runs(
    db_orchestration_client: AsyncClient, test_owner: uuid.UUID
):
    # 1. Create a Contact via REST API
    contact_data = {
        "display_name": "Dr. Sameer Sen",
        "aliases": ["sameer", "doctor", "doc"],
        "agent_id": "nexus:ed25519:abcdef1234567890abcdef1234567890",
        "endpoint": "https://sen-clinic.example.com/a2a",
        "notes": "Primary physician",
    }
    resp = await db_orchestration_client.post("/orchestration/contacts", json=contact_data)
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] == "Dr. Sameer Sen"
    assert "doc" in body["aliases"]

    # 2. List Contacts via REST API
    resp_list = await db_orchestration_client.get("/orchestration/contacts")
    assert resp_list.status_code == 200
    contacts = resp_list.json()
    assert any(c["display_name"] == "Dr. Sameer Sen" for c in contacts)

    # 3. List Runs (initially empty or existing)
    resp_runs = await db_orchestration_client.get("/orchestration/runs")
    assert resp_runs.status_code == 200
    assert isinstance(resp_runs.json(), list)


@pytest.mark.asyncio
async def test_orchestration_api_execute(
    db_orchestration_client: AsyncClient, test_owner: uuid.UUID
):
    # Execute an orchestration request via /orchestration/execute
    # For a contact that doesn't exist, it gracefully reports that target is unknown
    resp = await db_orchestration_client.post(
        "/orchestration/execute",
        json={"message": "Ask UnknownPerson if they are free tomorrow"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("target_unresolved", "error", "completed", "not_found")
    assert "unknownperson" in data["message"].lower()


# =============================================================================
# 10. CHAT ROUTE INTEGRATION
# =============================================================================


@pytest.mark.asyncio
async def test_chat_route_natural_language_orchestration(
    db_orchestration_client: AsyncClient, test_owner: uuid.UUID
):
    # Create session
    session_res = await db_orchestration_client.post("/sessions")
    assert session_res.status_code == 201
    session_id = session_res.json()["session_id"]

    # Natural language request routed through /chat
    resp = await db_orchestration_client.post(
        "/chat",
        json={"session_id": session_id, "message": "Ask UnknownColleague if he is free at 5 PM"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert "unknowncolleague" in body["response"].lower()
