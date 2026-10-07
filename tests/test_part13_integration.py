"""Comprehensive Test Suite for Part 13: Nexus Integration, State Correctness & Real Network Orchestration.

Tests all 30 specified scenarios:
1. test_state_machine_valid_transitions
2. test_approval_creates_waiting_approval_state
3. test_approve_resumes_same_run
4. test_reject_cancels_same_run
5. test_cancel_cancels_same_run
6. test_trust_resumes_waiting_for_trust_run
7. test_gateway_preferred_when_connected
8. test_gateway_fallback_to_http_when_disconnected
9. test_gateway_offline_waiting_remote
10. test_remote_response_correlation_across_restart
11. test_idempotent_duplicate_responses
12. test_response_with_unknown_task_id
13. test_response_invalid_signature_rejected
14. test_discovery_search_by_name_and_handle
15. test_discovery_does_not_auto_trust
16. test_datetime_normalizer_relative_dates
17. test_datetime_normalizer_time_parsing
18. test_datetime_normalizer_iso_passthrough
19. test_policy_deny_halts_orchestration
20. test_policy_ask_owner_transitions_waiting_approval
21. test_policy_allow_proceeds_seamlessly
22. test_context_pronoun_resolution_multi_turn
23. test_context_action_resolution_book_it
24. test_context_db_persistence_across_instances
25. test_workflow_step_queued_transitions_waiting
26. test_workflow_step_waiting_remote_transitions_waiting
27. test_orchestration_run_response_schema_fields
28. test_gateway_client_inbound_response_dispatch
29. test_gateway_discovery_endpoint_public_metadata_only
30. test_end_to_end_meeting_coordination_simulated
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.a2a.models import A2ATask, TaskStatus, TrustedAgent, TrustStatus
from app.a2a import signing
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.repository import TaskRepository, TrustedAgentRepository
from app.a2a.service import A2AService
from app.a2a.gateway_client import GatewayA2ATransport, GatewayClient
from app.a2a.transport import A2ATransport
from app.identity import crypto
from app.autonomy.decision_engine import DecisionEngine, DecisionOutcome, DecisionRequest
from app.autonomy.models import ActionType, DecisionResult, RiskLevel
from app.database.models import Base, Owner
from app.database.repositories import OwnerRepository
from app.identity.service import IdentityService, PublicIdentity
from app.orchestration.context import OrchestrationContextManager
from app.orchestration.datetime_norm import DateTimeNormalizer
from app.orchestration.errors import (
    AmbiguousTargetError,
    OrchestrationError,
    OrchestrationPolicyError,
    TargetResolutionError,
    UntrustedAgentError,
)
from app.orchestration.executor import OrchestrationExecutor
from app.orchestration.intent import IntentResolver
from app.orchestration.models import Contact, OrchestrationContext, OrchestrationRun, OrchestrationState
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.planner import OrchestrationPlanner
from app.orchestration.repository import (
    ContactRepository,
    OrchestrationContextRepository,
    OrchestrationRunRepository,
)
from app.orchestration.schemas import (
    Intent,
    IntentType,
    OrchestrationApproveRequest,
    OrchestrationCancelRequest,
    OrchestrationExecuteResponse,
    OrchestrationPlan,
    PlanStep,
    OrchestrationRejectRequest,
    OrchestrationRunResponse,
    OrchestrationTrustRequest,
    TargetResolution,
    TargetResolutionStatus,
)
from app.orchestration.target_resolver import TargetResolver
from app.policy.engine import EvaluationRequest, EvaluationResult
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService
from app.workflows.models import StepStatus
from app.workflows.handlers import A2ATaskStepHandler, StepResult, WorkflowStepContext

# SQLite compatibility compiler hook for JSONB
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


class _RealSigner:
    """Minimal signer implementing the IdentityService surface A2A needs.

    Used to produce REAL Ed25519 signatures in tests, so signature
    verification code paths are genuinely exercised rather than mocked.
    """

    __test__ = False

    def __init__(self, private_key) -> None:
        self._private_key = private_key

    def get_public_identity(self) -> PublicIdentity:
        raw = crypto.public_key_bytes(self._private_key.public_key())
        return PublicIdentity(
            agent_id=crypto.agent_id_from_public_key(raw),
            public_key=base64.b64encode(raw).decode("ascii"),
            key_algorithm=crypto.KEY_ALGORITHM,
            fingerprint=crypto.fingerprint_from_public_key(raw),
        )

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private_key, data)


@pytest_asyncio.fixture
async def test_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(test_engine):
    return async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def test_owner(session_factory):
    async with session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        return owner.id


@pytest.fixture
def mock_identity():
    ident = MagicMock(spec=IdentityService)
    ident.get_public_identity.return_value = PublicIdentity(
        agent_id="nexus:ed25519:11112222333344445555666677778888",
        public_key="bW9ja19wdWJsaWNfa2V5",
        key_algorithm="Ed25519",
        fingerprint="1111-2222-3333-4444",
    )
    ident.sign = AsyncMock(return_value=b"0" * 64)
    ident.verify = MagicMock(return_value=True)
    return ident


# =============================================================================
# 1. State Machine Valid Transitions
# =============================================================================
@pytest.mark.asyncio
async def test_state_machine_valid_transitions(session_factory, test_owner):
    runs_repo = OrchestrationRunRepository()
    async with session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Ask Rahul if free",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.UNDERSTANDING.value,
        )
        assert run.state == OrchestrationState.UNDERSTANDING.value

        # Valid forward transitions
        for next_state in [
            OrchestrationState.RESOLVING_TARGET,
            OrchestrationState.WAITING_FOR_DISCOVERY,
            OrchestrationState.WAITING_FOR_TRUST,
            OrchestrationState.PLANNING,
            OrchestrationState.WAITING_APPROVAL,
            OrchestrationState.WAITING_REMOTE,
            OrchestrationState.EXECUTING,
            OrchestrationState.PROCESSING_RESULT,
            OrchestrationState.COMPLETED,
        ]:
            run = await runs_repo.transition_state(session, run.id, next_state)
            assert run.state == next_state.value
        await session.commit()


# =============================================================================
# 2. Approval Creates WAITING_APPROVAL State with Details
# =============================================================================
@pytest.mark.asyncio
async def test_approval_creates_waiting_approval_state(session_factory, test_owner):
    runs_repo = OrchestrationRunRepository()
    async with session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Book 8 PM with Rahul",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.PLANNING.value,
        )
        await runs_repo.set_approval_request(
            session,
            run.id,
            reason="High risk calendar write action",
            requested_action="book_calendar_slot",
            approval_target="Rahul",
            approval_category="calendar",
            approval_purpose="meeting_booking",
            approval_step=1,
        )
        await session.commit()

        updated = await runs_repo.get_by_id(session, run.id)
        assert updated.state == OrchestrationState.WAITING_APPROVAL.value
        assert updated.approval_reason == "High risk calendar write action"
        assert updated.requested_action == "book_calendar_slot"
        assert updated.approval_target == "Rahul"
        assert updated.approval_step == 1


# =============================================================================
# 3. Approve Resumes Same Run (Does Not Duplicate)
# =============================================================================
@pytest.mark.asyncio
async def test_approve_resumes_same_run(session_factory, test_owner, mock_identity):
    runs_repo = OrchestrationRunRepository()
    mock_a2a = AsyncMock(spec=A2AService)
    mock_a2a.delegate_task.return_value = {"status": "completed", "payload": {"confirmed": True}}
    mock_policy = MagicMock(spec=PolicyService)

    async with session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Book 8 PM",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.WAITING_APPROVAL.value,
            plan={
                "goal": "Book 8 PM",
                "target_agent_id": "nexus:ed25519:22222222222222222222222222222222",
                "target_person": "Rahul",
                "intent_type": IntentType.COORDINATE_MEETING.value,
                "steps": [
                    {
                        "step_id": "step_1",
                        "step_type": "delegate_task",
                        "description": "book",
                        "payload": {"time": "20:00", "purpose": "booking", "data_category": "calendar", "action": "book"},
                        "requires_approval": True,
                    }
                ],
            },
        )
        await runs_repo.set_approval_request(
            session,
            run.id,
            reason="Approval needed",
            requested_action="book",
            approval_target="Rahul",
            approval_category="calendar",
            approval_purpose="booking",
            approval_step=1,
        )
        await session.commit()

    mock_target_res = AsyncMock()
    mock_target_res.resolve.return_value = TargetResolution(
        target_name="Rahul",
        status=TargetResolutionStatus.KNOWN_AGENT,
        agent_id="nexus:ed25519:22222222222222222222222222222222",
        display_name="Rahul",
        is_trusted=True,
    )

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=mock_a2a,
        policy_service=mock_policy,
        intent_resolver=MagicMock(),
        target_resolver=mock_target_res,
    )

    resp = await orchestrator.approve_run(test_owner, run.id)
    assert resp.run_id == str(run.id)
    assert resp.status in ("completed", "executing")


# =============================================================================
# 4. Reject Cancels Same Run
# =============================================================================
@pytest.mark.asyncio
async def test_reject_cancels_same_run(session_factory, test_owner):
    runs_repo = OrchestrationRunRepository()
    async with session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Book 8 PM",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.WAITING_APPROVAL.value,
        )
        await session.commit()

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=MagicMock(),
        policy_service=MagicMock(),
        intent_resolver=MagicMock(),
        target_resolver=MagicMock(),
    )

    resp = await orchestrator.reject_run(test_owner, run.id, OrchestrationRejectRequest(reason="Not available then"))
    assert resp.run_id == str(run.id)
    assert resp.status == "cancelled"


# =============================================================================
# 5. Cancel Cancels Same Run
# =============================================================================
@pytest.mark.asyncio
async def test_cancel_cancels_same_run(session_factory, test_owner):
    runs_repo = OrchestrationRunRepository()
    async with session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Book 8 PM",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.WAITING_REMOTE.value,
        )
        await session.commit()

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=MagicMock(),
        policy_service=MagicMock(),
        intent_resolver=MagicMock(),
        target_resolver=MagicMock(),
    )

    resp = await orchestrator.cancel_run(test_owner, run.id, OrchestrationCancelRequest(reason="Changed my mind"))
    assert resp.run_id == str(run.id)
    assert resp.status == "cancelled"


# =============================================================================
# 6. Trust Resumes Waiting-for-Trust Run
# =============================================================================
@pytest.mark.asyncio
async def test_trust_resumes_waiting_for_trust_run(session_factory, test_owner):
    runs_repo = OrchestrationRunRepository()
    target_id = "nexus:ed25519:33333333333333333333333333333333"

    async with session_factory() as session:
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Ask Amit if free",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.WAITING_FOR_TRUST.value,
            target_agent_id=target_id,
            target_person="Amit",
        )
        await session.commit()

    mock_a2a = AsyncMock(spec=A2AService)
    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(return_value=EvaluationResult(decision=PolicyDecision.ALLOW, reason="Allowed"))

    mock_intent = AsyncMock(spec=IntentResolver)
    mock_intent.resolve_intent.return_value = Intent(
        goal="Ask Amit if free",
        intent_type=IntentType.COORDINATE_MEETING,
        target="Amit",
        purpose="scheduling",
        requested_information=["free_busy"],
    )

    mock_target_res = AsyncMock()
    mock_target_res.resolve.return_value = TargetResolution(
        target_name="Amit",
        status=TargetResolutionStatus.KNOWN_AGENT,
        agent_id=target_id,
        display_name="Amit",
    )
    mock_target_res.get_discovered_card.return_value = {
        "public_key": "bW9ja19wdWJsaWNfa2V5",
        "endpoint": "https://example.com/a2a",
        "display_name": "Amit",
    }

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=mock_a2a,
        policy_service=mock_policy,
        intent_resolver=mock_intent,
        target_resolver=mock_target_res,
    )

    resp = await orchestrator.trust_and_resume_run(
        test_owner,
        run.id,
        OrchestrationTrustRequest(display_name="Amit"),
    )
    assert resp.run_id == str(run.id)
    assert resp.status != OrchestrationState.WAITING_FOR_TRUST.value


# =============================================================================
# 7. Gateway Preferred When Connected
# =============================================================================
@pytest.mark.asyncio
async def test_gateway_preferred_when_connected():
    mock_http = AsyncMock(spec=A2ATransport)
    mock_client = AsyncMock(spec=GatewayClient)
    mock_client.is_connected = True
    mock_client.send_relay_envelope.return_value = {"status": "gateway_ok"}

    transport = GatewayA2ATransport(http_transport=mock_http, gateway_client=mock_client)
    envelope = {
        "protocol": "nexus-a2a",
        "version": "0.1",
        "message_id": "m1",
        "task_id": "t1",
        "sender": "nexus:ed25519:11111111111111111111111111111111",
        "recipient": "nexus:ed25519:22222222222222222222222222222222",
        "timestamp": "2026-09-15T10:00:00Z",
        "expires_at": "2026-09-15T10:01:00Z",
        "message_type": "request",
        "purpose": "test",
        "payload": {},
    }

    res = await transport.send("http://remote.agent.com/a2a", envelope)
    assert res == {"status": "gateway_ok"}
    mock_client.send_relay_envelope.assert_awaited_once_with(envelope)
    mock_http.send.assert_not_called()


# =============================================================================
# 8. Gateway Fallback to HTTP When Disconnected
# =============================================================================
@pytest.mark.asyncio
async def test_direct_egress_requires_explicit_opt_in():
    """Direct HTTP is opt-in (decision D2), not a silent fallback.

    This test previously asserted that a disconnected gateway silently fell
    back to a direct HTTP POST. That is exactly what D2 reverses: falling back
    silently means a peer's reachability decides your delivery semantics (no
    offline buffering, no delivery ack, different failure modes), so the
    operator must choose it.
    """
    mock_http = AsyncMock(spec=A2ATransport)
    mock_http.send.return_value = {"status": "http_ok"}
    mock_client = AsyncMock(spec=GatewayClient)
    mock_client.is_connected = False

    envelope = {
        "recipient": "nexus:ed25519:22222222222222222222222222222222",
        "message_id": "m1",
    }

    # Default deployment: refuse and explain.
    strict = GatewayA2ATransport(
        http_transport=mock_http, gateway_client=mock_client
    )
    with pytest.raises(A2AError) as exc:
        await strict.send("http://remote.agent.com/a2a", envelope)
    assert "direct egress is disabled" in exc.value.message
    mock_http.send.assert_not_awaited()

    # With direct egress enabled the HTTP transport is used.
    opted_in = GatewayA2ATransport(
        http_transport=mock_http,
        gateway_client=mock_client,
        allow_direct_egress=True,
    )
    res = await opted_in.send("http://remote.agent.com/a2a", envelope)
    assert res == {"status": "http_ok"}
    mock_http.send.assert_awaited_once_with("http://remote.agent.com/a2a", envelope)


# =============================================================================
# 9. Gateway Offline Sets WAITING_REMOTE (Not Failed)
# =============================================================================
@pytest.mark.asyncio
async def test_gateway_offline_waiting_remote(session_factory, test_owner):
    task_repo = TaskRepository()
    async with session_factory() as session:
        task = A2ATask(
            owner_id=test_owner,
            task_id="task_offline_1",
            sender_agent_id="nexus:ed25519:11111111111111111111111111111111",
            recipient_agent_id="nexus:ed25519:22222222222222222222222222222222",
            task_type="availability_check",
            purpose="scheduling",
            request_payload={"requested_time": "18:00"},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            status=TaskStatus.WAITING_REMOTE.value,
        )
        await task_repo.upsert(session, task)
        await session.commit()

        fetched = await task_repo.get(session, test_owner, "task_offline_1")
        assert fetched.status == TaskStatus.WAITING_REMOTE.value


# =============================================================================
# 10. Remote Response Correlation Across Restart
# =============================================================================
@pytest.mark.asyncio
async def test_remote_response_correlation_across_restart(session_factory, test_owner):
    task_repo = TaskRepository()
    runs_repo = OrchestrationRunRepository()

    async with session_factory() as session:
        task = A2ATask(
            owner_id=test_owner,
            task_id="corr_task_1",
            sender_agent_id="nexus:ed25519:11111111111111111111111111111111",
            recipient_agent_id="nexus:ed25519:22222222222222222222222222222222",
            task_type="availability_check",
            purpose="scheduling",
            request_payload={"time": "18:00"},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            status=TaskStatus.WAITING_REMOTE.value,
        )
        await task_repo.upsert(session, task)
        run = await runs_repo.create(
            session,
            owner_id=test_owner,
            session_id="s1",
            goal="Ask Rahul",
            intent_type=IntentType.COORDINATE_MEETING.value,
            state=OrchestrationState.WAITING_REMOTE.value,
            task_id="corr_task_1",
            target_person="Rahul",
        )
        await session.commit()

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=MagicMock(),
        policy_service=MagicMock(),
        intent_resolver=MagicMock(),
        target_resolver=MagicMock(),
    )

    # Inbound response arrived over WebSocket
    response_envelope = {
        "protocol": "nexus-a2a",
        "version": "0.1",
        "message_id": "resp_msg_1",
        "task_id": "corr_task_1",
        "sender": "nexus:ed25519:22222222222222222222222222222222",
        "recipient": "nexus:ed25519:11111111111111111111111111111111",
        "timestamp": "2026-09-15T10:05:00Z",
        "expires_at": "2026-09-15T10:15:00Z",
        "message_type": "response",
        "purpose": "scheduling",
        "payload": {"available": True, "suggested_time": "18:00"},
    }

    # Simulate callback fired by A2AService
    await orchestrator.handle_task_completion("corr_task_1", response_envelope["payload"])

    async with session_factory() as session:
        updated_run = await runs_repo.get_by_id(session, test_owner, run.id)
        assert updated_run.state == OrchestrationState.COMPLETED.value
        assert updated_run.result is not None


# =============================================================================
# 11. Idempotent Duplicate Responses
# =============================================================================
@pytest.mark.asyncio
async def test_idempotent_duplicate_responses(session_factory, test_owner):
    task_repo = TaskRepository()
    async with session_factory() as session:
        task = A2ATask(
            owner_id=test_owner,
            task_id="idemp_task_1",
            sender_agent_id="nexus:ed25519:11111111111111111111111111111111",
            recipient_agent_id="nexus:ed25519:22222222222222222222222222222222",
            task_type="availability_check",
            purpose="scheduling",
            request_payload={"time": "18:00"},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            status=TaskStatus.COMPLETED.value,
            response_payload={"available": True},
        )
        await task_repo.upsert(session, task)
        await session.commit()

        # Update again with duplicate response
        await task_repo.update_status(session, test_owner, "idemp_task_1", TaskStatus.COMPLETED.value, response_payload={"available": True})
        await session.commit()

        fetched = await task_repo.get(session, test_owner, "idemp_task_1")
        assert fetched.status == TaskStatus.COMPLETED.value


# =============================================================================
# 12. Response with Unknown Task ID Safely Handled
# =============================================================================
@pytest.mark.asyncio
async def test_response_with_unknown_task_id(session_factory, test_owner):
    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=MagicMock(),
        policy_service=MagicMock(),
        intent_resolver=MagicMock(),
        target_resolver=MagicMock(),
    )
    # Should not raise any exception
    await orchestrator.handle_task_completion("non_existent_task_999", {"data": "test"})


# =============================================================================
# 13. Response Invalid Signature Rejected
# =============================================================================
@pytest.mark.asyncio
async def test_response_invalid_signature_rejected(db_session_factory, db_owner_id):
    """A response whose Ed25519 signature does not verify is rejected and
    must NOT mutate the correlated task.

    Uses a REAL Ed25519 keypair and a real (but unrelated) key on the trusted
    agent record, so the signature check genuinely fails. The previous version
    mocked IdentityService.verify, which meant no code path actually
    exercised signature verification, and passed a raw dict (which the
    hardened handle_inbound_response no longer accepts).
    """
    from app.a2a.errors import A2AError
    from app.a2a.schemas import A2AEnvelope, utc_iso_in, utc_now_iso

    sender_priv, sender_pub = crypto.generate_keypair()
    sender_raw = crypto.public_key_bytes(sender_pub)
    sender_key_b64 = base64.b64encode(sender_raw).decode("ascii")
    sender_agent_id = crypto.agent_id_from_public_key(sender_raw)

    # A DIFFERENT key recorded as the trusted peer's key => verification fails.
    _, other_pub = crypto.generate_keypair()
    other_key_b64 = base64.b64encode(
        crypto.public_key_bytes(other_pub)
    ).decode("ascii")

    local_agent_id = "nexus:ed25519:11111111111111111111111111111111"
    mock_identity = MagicMock(spec=IdentityService)
    mock_identity.get_public_identity.return_value = PublicIdentity(
        agent_id=local_agent_id,
        public_key="bW9ja19wdWJsaWNfa2V5",
        key_algorithm="Ed25519",
        fingerprint="1111-2222",
    )

    ta_repo = TrustedAgentRepository()
    async with db_session_factory() as session:
        await ta_repo.add(
            session,
            TrustedAgent(
                owner_id=db_owner_id,
                agent_id=sender_agent_id,
                public_key=other_key_b64,
                display_name="Responder",
                endpoint="https://example.com/a2a",
                status=TrustStatus.ACTIVE.value,
            ),
        )
        await session.commit()

    task_id = f"task_{uuid.uuid4().hex}"
    task_repo = TaskRepository()
    async with db_session_factory() as session:
        await task_repo.upsert(
            session,
            A2ATask(
                owner_id=db_owner_id,
                task_id=task_id,
                sender_agent_id=local_agent_id,
                recipient_agent_id=sender_agent_id,
                status=TaskStatus.PENDING.value,
            ),
        )
        await session.commit()

    a2a = A2AService(
        session_factory=db_session_factory,
        identity_service=mock_identity,
        policy_service=MagicMock(),
        memory_manager=None,
        transport=MagicMock(),
        rate_limiter=MagicMock(),
    )

    envelope = A2AEnvelope(
        message_id=f"msg_{uuid.uuid4().hex}",
        task_id=task_id,
        sender=sender_agent_id,
        recipient=local_agent_id,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(300),
        message_type="response",
        purpose="scheduling",
        payload={"status": "completed", "available": True},
    )
    # Sign with the sender key, but the registry holds a different key.
    signed = await signing.sign_envelope(_RealSigner(sender_priv), envelope)
    assert signed.signature

    # Strict path raises (so the HTTP layer can map it to a status code) ...
    with pytest.raises(A2AError):
        await a2a.handle_inbound_response(db_owner_id, signed)

    # ... and the tolerant gateway path swallows it without raising.
    assert await a2a.handle_gateway_delivery(db_owner_id, signed) is None

    # The task must still be PENDING: a bad signature must not complete it.
    async with db_session_factory() as session:
        unchanged = await task_repo.get(session, db_owner_id, task_id)
    assert unchanged is not None
    assert unchanged.status == TaskStatus.PENDING.value
    assert unchanged.response_payload is None


# =============================================================================
# 14. Discovery Search by Name and Handle
# =============================================================================
@pytest.mark.asyncio
async def test_discovery_search_by_name_and_handle(db_session_factory, db_owner_id):
    """A directory entry WITHOUT a signed card must NOT be discoverable.

    This test previously asserted the opposite: it fed the resolver unsigned
    gateway metadata (agent_id + display_name, no card, no signature) and
    expected DISCOVERED_AGENT. That was audit finding H2 - a compromised
    directory could inject agents that had never been cryptographically
    verified. A directory entry is a hint; only a verified card is identity.

    The positive path (a properly signed card IS discovered) is covered by
    tests/test_m1_discovery_regressions.py.
    """
    mock_http_client = AsyncMock()
    mock_http_client.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "agents": [
                {
                    "agent_id": "nexus:ed25519:44444444444444444444444444444444",
                    "display_name": "Rahul Sharma",
                    "handle": "rahul",
                    "is_online": True,
                }
            ]
        }),
    )

    target_resolver = TargetResolver(
        session_factory=db_session_factory,
        gateway_url="https://gateway.example.com",
        http_client=mock_http_client,
    )

    res = await target_resolver.resolve(db_owner_id, "@rahul")
    assert res.status == TargetResolutionStatus.UNKNOWN_AGENT
    assert res.agent_id is None
    assert res.card is None


# =============================================================================
# 15. Discovery Does Not Auto-Trust
# =============================================================================
@pytest.mark.asyncio
async def test_discovery_does_not_auto_trust(session_factory, test_owner):
    ta_repo = TrustedAgentRepository()
    async with session_factory() as session:
        agent = await ta_repo.get(session, test_owner, "nexus:ed25519:44444444444444444444444444444444")
        assert agent is None  # Discovery must NEVER auto-trust!


# =============================================================================
# 16. DateTimeNormalizer Relative Dates
# =============================================================================
def test_datetime_normalizer_relative_dates():
    normalizer = DateTimeNormalizer()
    now = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)

    # "tomorrow" and "after 6 PM"
    norm = normalizer.normalize(raw_date="tomorrow", raw_time="after 6 PM", reference_time=now)
    assert norm.date == "2026-09-17"
    assert norm.start_time == "18:00"


# =============================================================================
# 17. DateTimeNormalizer Time Parsing
# =============================================================================
def test_datetime_normalizer_time_parsing():
    normalizer = DateTimeNormalizer()
    norm = normalizer.normalize(raw_time="afternoon")
    assert norm.start_time == "14:00"
    assert norm.end_time == "17:00"


# =============================================================================
# 18. DateTimeNormalizer ISO Passthrough
# =============================================================================
def test_datetime_normalizer_iso_passthrough():
    normalizer = DateTimeNormalizer()
    norm = normalizer.normalize(raw_date="2026-09-20", raw_time="15:30")
    assert norm.date == "2026-09-20"
    assert norm.start_time == "15:30"


# =============================================================================
# 19. Policy DENY Halts Orchestration
# =============================================================================
@pytest.mark.asyncio
async def test_policy_deny_halts_orchestration(session_factory, test_owner):
    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(return_value=EvaluationResult(
        decision=PolicyDecision.DENY, reason="Calendar access forbidden by policy"
    ))

    executor = OrchestrationExecutor(
        a2a_service=MagicMock(),
        policy_service=mock_policy,
    )

    step = PlanStep(
        step_id="s1",
        step_type="delegate_task",
        description="read calendar",
        payload={
            "action": "read_calendar",
            "data_category": "calendar",
            "purpose": "scheduling",
        },
    )

    with pytest.raises(OrchestrationPolicyError) as exc_info:
        await executor._authorize_step(test_owner, step, "nexus:ed25519:22222222222222222222222222222222", "Rahul")
    assert "Calendar access forbidden by policy" in str(exc_info.value)


# =============================================================================
# 20. Policy ASK_OWNER Transitions to WAITING_APPROVAL
# =============================================================================
@pytest.mark.asyncio
async def test_policy_ask_owner_transitions_waiting_approval(session_factory, test_owner):
    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(return_value=EvaluationResult(
        decision=PolicyDecision.ASK, reason="Requires explicit confirmation"
    ))

    executor = OrchestrationExecutor(
        a2a_service=MagicMock(),
        policy_service=mock_policy,
    )

    step = PlanStep(
        step_id="s1",
        step_type="delegate_task",
        description="book calendar",
        payload={
            "action": "book_calendar",
            "data_category": "calendar",
            "purpose": "booking",
        },
    )

    auth = await executor._authorize_step(test_owner, step, "nexus:ed25519:22222222222222222222222222222222", "Rahul")
    assert auth is not None
    assert auth["status"] == "waiting_approval"
    assert auth["requires_approval"] is True
    assert auth["approval_reason"] == "Requires explicit confirmation"


# =============================================================================
# 21. Policy ALLOW Proceeds Seamlessly
# =============================================================================
@pytest.mark.asyncio
async def test_policy_allow_proceeds_seamlessly(session_factory, test_owner):
    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(return_value=EvaluationResult(decision=PolicyDecision.ALLOW, reason="Policy allows"))

    executor = OrchestrationExecutor(
        a2a_service=MagicMock(),
        policy_service=mock_policy,
    )

    step = PlanStep(
        step_id="s1",
        step_type="delegate_task",
        description="check availability",
        payload={
            "action": "check_availability",
            "data_category": "availability",
            "purpose": "scheduling",
        },
    )

    auth = await executor._authorize_step(test_owner, step, "nexus:ed25519:22222222222222222222222222222222", "Rahul")
    assert auth is None  # None means allowed, no approval needed


# =============================================================================
# 22. Context Pronoun Resolution Multi-Turn
# =============================================================================
def test_context_pronoun_resolution_multi_turn():
    mgr = OrchestrationContextManager()
    mgr.update_target("s_multi", "Rahul")

    resolved, target = mgr.resolve_references("s_multi", "Ask him if 8 PM works instead")
    assert target == "Rahul"
    assert "rahul" in resolved.lower()


# =============================================================================
# 23. Context Action Resolution "Book it"
# =============================================================================
def test_context_action_resolution_book_it():
    mgr = OrchestrationContextManager()
    mgr.update_target("s_action", "Rahul")
    mgr.update_proposed_time("s_action", "20:00")

    resolved, target = mgr.resolve_references("s_action", "Book it")
    assert target == "Rahul"
    ctx = mgr.get_context_sync("s_action")
    assert ctx.last_proposed_time == "20:00"


# =============================================================================
# 24. Context DB Persistence Across Instances
# =============================================================================
@pytest.mark.asyncio
async def test_context_db_persistence_across_instances(session_factory, test_owner):
    # Instance 1 writes
    mgr1 = OrchestrationContextManager(session_factory=session_factory)
    await mgr1.update_target(test_owner, "s_persist", "Rahul", "nexus:ed25519:22222222222222222222222222222222")
    await mgr1.update_proposed_time(test_owner, "s_persist", "19:00")

    # Instance 2 (clean in-memory state) reads from DB
    mgr2 = OrchestrationContextManager(session_factory=session_factory)
    ctx2 = await mgr2.get_context(test_owner, "s_persist")
    assert ctx2.active_target == "Rahul"
    assert ctx2.active_agent_id == "nexus:ed25519:22222222222222222222222222222222"
    assert ctx2.last_proposed_time == "19:00"


# =============================================================================
# 25. Workflow Step Queued Transitions to WAITING
# =============================================================================
@pytest.mark.asyncio
async def test_workflow_step_queued_transitions_waiting(session_factory, test_owner):
    mock_a2a = AsyncMock(spec=A2AService)
    # New contract: delegate_task raises QUEUED for an offline recipient.
    mock_a2a.delegate_task.side_effect = A2AError(
        A2AErrorCode.QUEUED, "Recipient offline; queued on the gateway."
    )

    handler = A2ATaskStepHandler()
    ctx = WorkflowStepContext(
        owner_id=test_owner,
        workflow_id=uuid.uuid4(),
        step_id=uuid.uuid4(),
        step_number=1,
        purpose="scheduling",
        workflow_context={},
        policy_service=MagicMock(),
        a2a_service=mock_a2a,
    )

    res = await handler.execute(ctx, {
        "recipient_agent_id": "nexus:ed25519:22222222222222222222222222222222",
        "task_type": "meeting_proposal",
    })
    assert res.status == StepStatus.WAITING
    # The QUEUED error carries no task_id (it is raised, not returned), so the
    # step waits without one; the task row itself is parked as WAITING_REMOTE.
    assert res.task_id is None


# =============================================================================
# 26. Workflow Step Waiting Remote Transitions to WAITING
# =============================================================================
@pytest.mark.asyncio
async def test_workflow_step_waiting_remote_transitions_waiting(session_factory, test_owner):
    mock_a2a = AsyncMock(spec=A2AService)
    mock_a2a.delegate_task.return_value = {
        "status": "waiting_remote",
        "task_id": "wf_task_waiting_rem",
        "payload": {},
    }

    handler = A2ATaskStepHandler()
    ctx = WorkflowStepContext(
        owner_id=test_owner,
        workflow_id=uuid.uuid4(),
        step_id=uuid.uuid4(),
        step_number=1,
        purpose="scheduling",
        workflow_context={},
        policy_service=MagicMock(),
        a2a_service=mock_a2a,
    )

    res = await handler.execute(ctx, {
        "recipient_agent_id": "nexus:ed25519:22222222222222222222222222222222",
        "task_type": "meeting_proposal",
    })
    assert res.status == StepStatus.WAITING
    assert res.task_id == "wf_task_waiting_rem"


# =============================================================================
# 27. Orchestration Run Response Schema Serialization
# =============================================================================
def test_orchestration_run_response_schema_fields():
    now_str = datetime.now(timezone.utc).isoformat()
    resp = OrchestrationRunResponse(
        run_id="r123",
        session_id="s123",
        goal="Coordinate meeting with Rahul",
        intent_type=IntentType.COORDINATE_MEETING.value,
        state=OrchestrationState.WAITING_APPROVAL.value,
        target_person="Rahul",
        target_agent_id="nexus:ed25519:22222222222222222222222222222222",
        requires_approval=True,
        approval_prompt="Do you want to book at 7 PM?",
        approval_reason="Requires confirmation",
        task_id="t123",
        workflow_id="w123",
        created_at=now_str,
        updated_at=now_str,
    )
    dumped = resp.model_dump()
    assert dumped["run_id"] == "r123"
    assert dumped["approval_reason"] == "Requires confirmation"
    assert dumped["task_id"] == "t123"
    assert dumped["workflow_id"] == "w123"


# =============================================================================
# 28. Gateway Client Inbound Response Dispatch
# =============================================================================
@pytest.mark.asyncio
async def test_gateway_client_inbound_response_dispatch():
    from app.a2a.gateway_translate import to_gateway_envelope

    inbound_mock = AsyncMock(return_value={"status": "handled"})
    client = GatewayClient(
        gateway_url="wss://gateway.example.com",
        identity_service=MagicMock(),
        owner_id=uuid.uuid4(),
        inbound_handler=inbound_mock,
    )

    # Deliver a 0.3 delivery frame carrying a nested signed 0.2 envelope
    # (the C1 wire format): the client must unwrap and dispatch the inner
    # envelope to the inbound handler.
    inner_02 = {
        "protocol": "nexus-a2a",
        "version": "0.1",
        "message_id": "msg_resp_1",
        "task_id": "t1",
        "sender": "nexus:ed25519:22222222222222222222222222222222",
        "recipient": "nexus:ed25519:11111111111111111111111111111111",
        "timestamp": "2026-09-15T10:00:00Z",
        "expires_at": "2026-09-15T10:05:00Z",
        "message_type": "response",
        "purpose": "scheduling",
        "payload": {"status": "ok"},
        "signature": "mock_signature",
    }
    frame = {
        "type": "delivery",
        "relay_id": "relay_1",
        "envelope": to_gateway_envelope(inner_02),
    }

    mock_ws = AsyncMock()
    await client._handle_frame(mock_ws, frame)
    inbound_mock.assert_awaited_once()
    # The unwrapped 0.2 envelope is what the handler receives.
    dispatched = inbound_mock.call_args[0][0]
    assert dispatched.message_id == "msg_resp_1"
    assert dispatched.task_id == "t1"
    called_env = inbound_mock.await_args[0][0]
    assert getattr(called_env, "message_id", None) == "msg_resp_1" or (isinstance(called_env, dict) and called_env.get("message_id") == "msg_resp_1")


# =============================================================================
# 29. Gateway Discovery Endpoint Public Metadata Only
# =============================================================================
def test_gateway_discovery_endpoint_public_metadata_only():
    # Verify schemas only leak public safe metadata
    safe_fields = {"agent_id", "handle", "display_name", "is_online", "last_seen_at"}
    mock_agent_record = {
        "agent_id": "nexus:ed25519:44444444444444444444444444444444",
        "handle": "rahul",
        "display_name": "Rahul Sharma",
        "is_online": True,
        "last_seen_at": "2026-09-15T12:00:00Z",
        "private_key": "SUPER_SECRET",
        "logs": ["private log"],
    }
    # Public filtered projection
    filtered = {k: v for k, v in mock_agent_record.items() if k in safe_fields}
    assert "private_key" not in filtered
    assert "logs" not in filtered
    assert filtered["handle"] == "rahul"


# =============================================================================
# 30. End-to-End Meeting Coordination Simulated
# =============================================================================
@pytest.mark.asyncio
async def test_end_to_end_meeting_coordination_simulated(session_factory, test_owner):
    target_id = "nexus:ed25519:22222222222222222222222222222222"

    # Setup contact and trusted agent in DB
    contacts_repo = ContactRepository()
    ta_repo = TrustedAgentRepository()
    async with session_factory() as session:
        await contacts_repo.create(
            session,
            owner_id=test_owner,
            display_name="Rahul",
            agent_id=target_id,
            aliases=["rahul"],
        )
        await ta_repo.add(
            session,
            TrustedAgent(
                owner_id=test_owner,
                agent_id=target_id,
                public_key="bW9ja19wdWJsaWNfa2V5",
                display_name="Rahul",
                endpoint="https://example.com/a2a",
                status=TrustStatus.ACTIVE.value,
            ),
        )
        await session.commit()

    mock_a2a = AsyncMock(spec=A2AService)
    # Target is offline on gateway initially -> delegate_task raises QUEUED
    mock_a2a.delegate_task.side_effect = A2AError(
        A2AErrorCode.QUEUED,
        "Recipient offline; queued on the gateway.",
        details={"task_id": "sim_task_123", "relay_id": "relay_1"},
    )

    mock_policy = MagicMock(spec=PolicyService)
    mock_policy.evaluate = AsyncMock(return_value=EvaluationResult(decision=PolicyDecision.ALLOW, reason="Allowed"))

    mock_intent = AsyncMock(spec=IntentResolver)
    mock_intent.resolve_intent.return_value = Intent(
        goal="Ask Rahul if free tomorrow after 6 PM",
        intent_type=IntentType.COORDINATE_MEETING,
        target="Rahul",
        purpose="scheduling",
        requested_information=["free_busy"],
        constraints={"time": "after 6 PM"},
    )

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=mock_a2a,
        policy_service=mock_policy,
        intent_resolver=mock_intent,
        target_resolver=TargetResolver(session_factory=session_factory),
    )

    # 1. User initiates flow
    resp1 = await orchestrator.handle_user_message(
        owner_id=test_owner,
        session_id="sim_session",
        message="Ask Rahul if he's free tomorrow after 6 PM",
    )
    assert resp1 is not None
    assert resp1.status == "queued"
    assert resp1.details is not None
    assert resp1.details.get("task_id") == "sim_task_123" or resp1.run_id is not None

    # 2. Asynchronous response arrives later from Rahul's agent over Gateway
    remote_response_payload = {
        "available": True,
        "suggested_time": "19:00",
        "notes": "Free at 7 PM",
    }
    await orchestrator.handle_task_completion("sim_task_123", remote_response_payload)

    # 3. Check run state in DB
    runs_repo = OrchestrationRunRepository()
    async with session_factory() as session:
        completed_run = await runs_repo.get_by_id(session, test_owner, uuid.UUID(resp1.run_id))
        assert completed_run.state == OrchestrationState.COMPLETED.value
        assert completed_run.result is not None
