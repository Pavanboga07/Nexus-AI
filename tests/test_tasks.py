"""Part 8 tests: Agent Task Delegation & Negotiation.

Validates:
- End-to-end task delegation between personal agents
- Strict minimum disclosure (never leaking raw memories, private calendar events, notes)
- Deterministic policy gating (ALLOW, ASK, DENY)
- Approval lifecycle (PENDING_APPROVAL -> approve / reject)
- Bounded negotiation (round 1 -> round 2 -> round 3 -> max rounds limit)
- Policy isolation across negotiation rounds and different tasks
- Replay protection & idempotency on duplicate task requests
- Tampered envelope detection & signature verification
- Expired task rejection
- Owner isolation
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.a2a import signing
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.handlers import (
    AvailabilityCheckHandler,
    MeetingProposalHandler,
    TaskContext,
    TaskHandlerRegistry,
)
from app.a2a.models import A2ATask, TaskStatus, TrustStatus
from app.a2a.negotiation import validate_negotiation_round, validate_task_active
from app.a2a.rate_limit import SlidingWindowRateLimiter
from app.a2a.schemas import (
    A2AEnvelope,
    new_message_id,
    new_task_id,
    parse_iso,
    utc_iso_in,
    utc_now_iso,
)
from app.a2a.service import A2AService
from app.identity import crypto
from app.identity.service import PublicIdentity
from app.policy.models import DisclosureScope, PolicyDecision, WILDCARD


# --------------------------------------------------------------------------- #
#  Helpers & Stubs                                                            #
# --------------------------------------------------------------------------- #


def _make_keypair():
    private_key, public_key = crypto.generate_keypair()
    raw = crypto.public_key_bytes(public_key)
    pub_b64 = base64.b64encode(raw).decode()
    agent_id = crypto.agent_id_from_public_key(raw)
    return private_key, pub_b64, agent_id


class StubIdentity:
    """In-memory Ed25519 identity for test agent pairs."""

    def __init__(self):
        self._private, self.public_key_b64, self.agent_id = _make_keypair()
        self.ready = True

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private, data)

    def get_public_identity(self) -> PublicIdentity:
        return PublicIdentity(
            agent_id=self.agent_id,
            public_key=self.public_key_b64,
            key_algorithm="ed25519",
            fingerprint=self.agent_id.split(":")[-1],
        )


class LoopbackTransport:
    """Delivers outbound envelopes to another A2AService instance in-process."""

    def __init__(self, target_service: A2AService, target_owner_id: uuid.UUID):
        self.target_service = target_service
        self.target_owner_id = target_owner_id

    async def send(self, endpoint: str, envelope_dict: dict) -> dict:
        envelope = A2AEnvelope.model_validate(envelope_dict)
        response_env = await self.target_service.handle_inbound(
            self.target_owner_id, envelope
        )
        return response_env.model_dump()


# --------------------------------------------------------------------------- #
#  Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def two_agent_setup(db_session_factory, owner_ids, memory_manager, policy_service):
    """Sets up Agent A (requester) and Agent B (responder) with mutual trust and memory."""
    owner_a, owner_b = owner_ids

    identity_a = StubIdentity()
    identity_b = StubIdentity()

    # Store sample memories for Agent B's owner (Rahul)
    await memory_manager.store_memory(
        owner_b,
        memory_type="semantic",
        content="Rahul is available after 6 PM tomorrow for meetings.",
    )
    await memory_manager.store_memory(
        owner_b,
        memory_type="semantic",
        content="Secret doctor appointment at 3 PM, confidential notes.",
    )

    # Agent B service (responder)
    service_b = A2AService(
        session_factory=db_session_factory,
        identity_service=identity_b,
        policy_service=policy_service,
        memory_manager=memory_manager,
        transport=None,  # type: ignore[arg-type]
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
        max_negotiation_rounds=3,
        task_ttl_seconds=3600,
    )

    # Transport from A -> B
    transport_a_to_b = LoopbackTransport(service_b, owner_b)

    # Agent A service (requester)
    service_a = A2AService(
        session_factory=db_session_factory,
        identity_service=identity_a,
        policy_service=policy_service,
        memory_manager=memory_manager,
        transport=transport_a_to_b,
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
        max_negotiation_rounds=3,
        task_ttl_seconds=3600,
    )

    # Agent B's transport back to Agent A (for multi-round negotiation)
    transport_b_to_a = LoopbackTransport(service_a, owner_a)
    service_b._transport = transport_b_to_a

    # Register mutual trust
    await service_a.register_trusted_agent(
        owner_a,
        agent_id=identity_b.agent_id,
        public_key=identity_b.public_key_b64,
        display_name="Rahul's AI",
        endpoint="http://agent-b/a2a/messages",
    )
    await service_b.register_trusted_agent(
        owner_b,
        agent_id=identity_a.agent_id,
        public_key=identity_a.public_key_b64,
        display_name="Alice's AI",
        endpoint="http://agent-a/a2a/messages",
    )

    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "service_a": service_a,
        "service_b": service_b,
        "identity_a": identity_a,
        "identity_b": identity_b,
        "policy_service": policy_service,
        "memory_manager": memory_manager,
    }


# ========================================================================== #
#  UNIT TESTS — Handlers & Negotiation                                       #
# ========================================================================== #


class TestTaskHandlers:
    """Tests handler logic and minimum disclosure invariants."""

    def test_availability_check_validation(self) -> None:
        handler = AvailabilityCheckHandler()
        handler.validate({"requested_time": "18:00"})  # pass
        with pytest.raises(ValueError, match="requested_time"):
            handler.validate({})
        with pytest.raises(ValueError, match="non-empty"):
            handler.validate({"requested_time": "   "})

    async def test_availability_minimum_disclosure_free(self) -> None:
        handler = AvailabilityCheckHandler()

        class MockMemory:
            async def get_relevant_memories(self, owner_id, query, limit=3):
                class M:
                    content = "Available after 6 PM."
                return [M()]

        ctx = TaskContext(
            owner_id=uuid.uuid4(),
            requester_agent_id="nexus:test",
            task_id="t1",
            purpose="scheduling",
            disclosure_scope=DisclosureScope.SUMMARY,
            _memory_manager=MockMemory(),
        )
        res = await handler.execute(ctx, {"requested_time": "18:00"})
        # Must return available: True and NO raw memory dump
        assert res == {"available": True}
        assert "Available after 6 PM" not in str(res)

    async def test_availability_minimum_disclosure_busy(self) -> None:
        handler = AvailabilityCheckHandler()

        class MockMemory:
            async def get_relevant_memories(self, owner_id, query, limit=3):
                class M:
                    content = "Busy doctor appointment at 15:00."
                return [M()]

        ctx = TaskContext(
            owner_id=uuid.uuid4(),
            requester_agent_id="nexus:test",
            task_id="t2",
            purpose="scheduling",
            disclosure_scope=DisclosureScope.SUMMARY,
            _memory_manager=MockMemory(),
        )
        res = await handler.execute(ctx, {"requested_time": "15:00"})
        assert res["available"] is False
        assert "alternative_times" in res
        # Private doctor appointment details MUST NOT leak
        assert "doctor" not in str(res).lower()
        assert "appointment" not in str(res).lower()


class TestNegotiationControls:
    """Negotiation round limits and expiration enforcement."""

    def test_negotiation_round_limit(self) -> None:
        validate_negotiation_round(0, max_rounds=3)  # pass
        validate_negotiation_round(2, max_rounds=3)  # pass
        with pytest.raises(A2AError) as exc:
            validate_negotiation_round(3, max_rounds=3)
        assert exc.value.code is A2AErrorCode.NEGOTIATION_LIMIT_EXCEEDED

    def test_task_active_validation(self) -> None:
        active_task = A2ATask(
            task_id="t1",
            sender_agent_id="a",
            recipient_agent_id="b",
            status=TaskStatus.ACCEPTED.value,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        validate_task_active(active_task)  # pass

        # Expired task
        expired_task = A2ATask(
            task_id="t2",
            sender_agent_id="a",
            recipient_agent_id="b",
            status=TaskStatus.ACCEPTED.value,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        with pytest.raises(A2AError) as exc:
            validate_task_active(expired_task)
        assert exc.value.code is A2AErrorCode.TASK_EXPIRED

        # Terminal task
        terminal_task = A2ATask(
            task_id="t3",
            sender_agent_id="a",
            recipient_agent_id="b",
            status=TaskStatus.COMPLETED.value,
        )
        with pytest.raises(A2AError) as exc:
            validate_task_active(terminal_task)
        assert exc.value.code is A2AErrorCode.CONFLICT


# ========================================================================== #
#  INTEGRATION TESTS — Delegation, Policy, Lifecycle, Negotiation             #
# ========================================================================== #


class TestTaskDelegationFlow:
    """End-to-end task delegation and policy enforcement."""

    async def test_delegation_allow_availability_check(self, two_agent_setup) -> None:
        """Demo 2 & 3: User A asks if Rahul is free at 6 PM; policy ALLOW; minimum disclosure."""
        s = two_agent_setup

        # Grant ALLOW policy on Agent B for scheduling availability
        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ALLOW.value,
            disclosure_scope="summary",
        )

        result = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )

        assert result["status"] == "completed"
        payload = result["payload"]
        assert payload["status"] == "completed"
        assert payload["available"] is True
        # Verify NO raw calendar or health memories leaked
        assert "doctor" not in str(payload)
        assert "confidential" not in str(payload)

    async def test_delegation_policy_ask_and_approve(self, two_agent_setup) -> None:
        """Demo 4: Policy ASK creates PENDING_APPROVAL; approve completes task."""
        s = two_agent_setup

        # Grant ASK policy on Agent B
        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ASK.value,
        )

        # Agent A sends task
        result = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )

        # Task in Agent A is pending_approval
        assert result["status"] == "pending_approval"
        task_id = result["task_id"]

        # Check Agent B's task repository: task is in PENDING_APPROVAL
        task_b = await s["service_b"].get_task(s["owner_b"], task_id)
        assert task_b is not None
        assert task_b.status == TaskStatus.PENDING_APPROVAL.value

        # Owner B approves the task
        approved_task = await s["service_b"].approve_task(s["owner_b"], task_id)
        assert approved_task.status == TaskStatus.COMPLETED.value
        assert approved_task.response_payload["available"] is True

    async def test_delegation_policy_ask_and_reject(self, two_agent_setup) -> None:
        """Policy ASK -> owner rejects -> REJECTED."""
        s = two_agent_setup

        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ASK.value,
        )

        result = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )
        task_id = result["task_id"]

        rejected = await s["service_b"].reject_task(
            s["owner_b"], task_id, reason="Not interested"
        )
        assert rejected.status == TaskStatus.REJECTED.value
        assert rejected.failure_reason == "Not interested"

    async def test_delegation_policy_deny(self, two_agent_setup) -> None:
        """Demo 5: Policy DENY rejects task immediately without accessing data."""
        s = two_agent_setup

        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.DENY.value,
        )

        result = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )

        assert result["status"] == "rejected"
        assert result["payload"]["reason"] == "policy_denied"

    async def test_untrusted_agent_delegation_rejected(self, two_agent_setup) -> None:
        """Untrusted agent sending task_request is rejected (401)."""
        s = two_agent_setup
        untrusted = StubIdentity()

        envelope = A2AEnvelope(
            message_id=new_message_id(),
            task_id=new_task_id(),
            sender=untrusted.agent_id,
            recipient=s["identity_b"].agent_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="task_request",
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )
        signed = await signing.sign_envelope(untrusted, envelope)

        with pytest.raises(A2AError) as exc:
            await s["service_b"].handle_inbound(s["owner_b"], signed)
        assert exc.value.code is A2AErrorCode.UNTRUSTED_SENDER

    async def test_tampered_task_request_rejected(self, two_agent_setup) -> None:
        """Demo 8: Tampered payload fails signature verification."""
        s = two_agent_setup

        envelope = A2AEnvelope(
            message_id=new_message_id(),
            task_id=new_task_id(),
            sender=s["identity_a"].agent_id,
            recipient=s["identity_b"].agent_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="task_request",
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )
        signed = await signing.sign_envelope(s["identity_a"], envelope)

        # Tamper payload
        tampered = signed.model_copy(update={"payload": {"requested_time": "02:00"}})

        with pytest.raises(A2AError) as exc:
            await s["service_b"].handle_inbound(s["owner_b"], tampered)
        assert exc.value.code is A2AErrorCode.INVALID_SIGNATURE

    async def test_idempotency_duplicate_task_request(self, two_agent_setup) -> None:
        """Demo 7: Sending duplicate task_request returns cached result without re-executing."""
        s = two_agent_setup

        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ALLOW.value,
        )

        task_id = new_task_id()

        # First message
        env1 = A2AEnvelope(
            message_id=new_message_id(),
            task_id=task_id,
            sender=s["identity_a"].agent_id,
            recipient=s["identity_b"].agent_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="task_request",
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )
        signed1 = await signing.sign_envelope(s["identity_a"], env1)
        resp1 = await s["service_b"].handle_inbound(s["owner_b"], signed1)
        assert resp1.payload["status"] == "completed"

        # Duplicate task with new message_id (e.g. re-sent by network)
        env2 = A2AEnvelope(
            message_id=new_message_id(),
            task_id=task_id,
            sender=s["identity_a"].agent_id,
            recipient=s["identity_b"].agent_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="task_request",
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )
        signed2 = await signing.sign_envelope(s["identity_a"], env2)
        resp2 = await s["service_b"].handle_inbound(s["owner_b"], signed2)

        # Returns identical completed result
        assert resp2.payload["status"] == "completed"
        assert resp2.payload["available"] == resp1.payload["available"]

    async def test_expired_task_request_rejected(self, two_agent_setup) -> None:
        """Demo 9: Expired task request is rejected."""
        s = two_agent_setup

        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        expired_env = A2AEnvelope(
            message_id=new_message_id(),
            task_id=new_task_id(),
            sender=s["identity_a"].agent_id,
            recipient=s["identity_b"].agent_id,
            timestamp=past_time,
            expires_at=past_time,
            message_type="task_request",
            task_type="availability_check",
            purpose="scheduling",
            payload={"requested_time": "18:00"},
        )
        signed = await signing.sign_envelope(s["identity_a"], expired_env)

        with pytest.raises(A2AError) as exc:
            await s["service_b"].handle_inbound(s["owner_b"], signed)
        assert exc.value.code is A2AErrorCode.EXPIRED

    async def test_unsupported_task_type_rejected(self, two_agent_setup) -> None:
        """Unsupported task types are safely rejected."""
        s = two_agent_setup

        with pytest.raises(A2AError) as exc:
            await s["service_a"].delegate_task(
                s["owner_a"],
                recipient_agent_id=s["identity_b"].agent_id,
                task_type="financial_transfer",  # unauthorized / unsupported
                purpose="payment",
                payload={"amount": 100},
            )
        assert exc.value.code is A2AErrorCode.UNSUPPORTED_TASK_TYPE


class TestNegotiationFlow:
    """Demo 6: Multi-round bounded negotiation."""

    async def test_bounded_negotiation_success(self, two_agent_setup) -> None:
        """Agent A: 12:00? Agent B: counter-proposal (unavailable). Agent A: 18:00? Agent B: accepted."""
        s = two_agent_setup

        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ALLOW.value,
        )

        # Round 1: Agent A proposes 12:00 (busy)
        r1 = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="meeting_proposal",
            purpose="scheduling",
            payload={"proposed_time": "12:00", "topic": "Sync"},
        )
        task_id = r1["task_id"]
        assert r1["status"] == "accepted"
        assert r1["payload"]["status"] == "counter_proposal"

        # Round 2: Agent A submits counter-proposal 18:00 (free)
        r2 = await s["service_a"].negotiate_task(
            s["owner_a"],
            task_id=task_id,
            proposal_payload={"proposed_time": "18:00", "topic": "Sync"},
        )
        assert r2["status"] == "completed"
        assert r2["payload"]["status"] == "accepted"
        assert r2["negotiation_round"] == 1

    async def test_negotiation_exceeds_max_rounds_terminated(self, two_agent_setup) -> None:
        """Negotiation halts when maximum configured rounds is reached."""
        s = two_agent_setup
        s["service_a"]._max_negotiation_rounds = 2
        s["service_b"]._max_negotiation_rounds = 2

        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ALLOW.value,
        )

        # Round 1
        r1 = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="meeting_proposal",
            purpose="scheduling",
            payload={"proposed_time": "12:00", "topic": "Sync"},
        )
        task_id = r1["task_id"]

        # Round 2 (reaches max rounds)
        r2 = await s["service_a"].negotiate_task(
            s["owner_a"],
            task_id=task_id,
            proposal_payload={"proposed_time": "13:00", "topic": "Sync"},
        )
        assert r2["negotiation_round"] == 1

        # Force task state to round 2 for testing round 3 limit
        async with s["service_a"]._session_factory() as session:
            t = await s["service_a"]._tasks.get(session, s["owner_a"], task_id)
            t.negotiation_round = 2
            t.status = TaskStatus.ACCEPTED.value
            await s["service_a"]._tasks.upsert(session, t)
            await session.commit()

        # Round 3 attempt must be rejected with NEGOTIATION_LIMIT_EXCEEDED
        with pytest.raises(A2AError) as exc:
            await s["service_a"].negotiate_task(
                s["owner_a"],
                task_id=task_id,
                proposal_payload={"proposed_time": "14:00", "topic": "Sync"},
            )
        assert exc.value.code is A2AErrorCode.NEGOTIATION_LIMIT_EXCEEDED

    async def test_negotiation_policy_isolation(self, two_agent_setup) -> None:
        """Authorization on round 1 does NOT bypass policy for an unauthorized round 2 request."""
        s = two_agent_setup

        # Only authorize "availability" data category, explicitly DENY "location"
        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.ALLOW.value,
        )
        await s["policy_service"].create_policy(
            s["owner_b"],
            requester_agent_id=s["identity_a"].agent_id,
            data_category="location",
            action="disclose_information",
            purpose="scheduling",
            decision=PolicyDecision.DENY.value,
        )

        # Round 1: availability check -> ALLOW
        r1 = await s["service_a"].delegate_task(
            s["owner_a"],
            recipient_agent_id=s["identity_b"].agent_id,
            task_type="meeting_proposal",
            purpose="scheduling",
            payload={"proposed_time": "12:00"},
        )
        task_id = r1["task_id"]

        # Round 2: Agent attempts to request "location" without permission -> REJECTED
        r2 = await s["service_a"].negotiate_task(
            s["owner_a"],
            task_id=task_id,
            proposal_payload={"proposed_time": "18:00", "data_category": "location"},
        )
        # B's policy denies location category -> task rejected
        assert r2["status"] == "rejected"
        assert r2["payload"]["reason"] == "policy_denied"


# ========================================================================== #
#  API & OWNER ISOLATION TESTS                                               #
# ========================================================================== #


class TestTaskAPIRoutes:
    """REST API endpoints for tasks."""

    async def test_tasks_api_lifecycle(self, two_agent_setup) -> None:
        s = two_agent_setup

        from app.main import create_app
        app = create_app()
        app.state.a2a_service = s["service_a"]
        app.state.a2a_ok = True
        app.state.identity_service = s["identity_a"]
        app.state.identity_ok = True

        class MockAgent:
            def __init__(self, owner_id: uuid.UUID):
                self._oid = owner_id
            async def _owner_id(self) -> uuid.UUID:
                return self._oid

        app.state.agent = MockAgent(s["owner_a"])

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. Grant policy on B
            await s["policy_service"].create_policy(
                s["owner_b"],
                requester_agent_id=s["identity_a"].agent_id,
                data_category="availability",
                action="disclose_information",
                purpose="scheduling",
                decision=PolicyDecision.ALLOW.value,
            )

            # 2. Delegate via POST /a2a/tasks
            resp = await client.post(
                "/a2a/tasks",
                json={
                    "recipient_agent_id": s["identity_b"].agent_id,
                    "task_type": "availability_check",
                    "purpose": "scheduling",
                    "payload": {"requested_time": "18:00"},
                },
            )
            assert resp.status_code == 201
            body = resp.json()
            task_id = body["task_id"]
            assert body["status"] == "completed"

            # 3. List via GET /a2a/tasks
            list_resp = await client.get("/a2a/tasks")
            assert list_resp.status_code == 200
            tasks = list_resp.json()["tasks"]
            assert any(t["task_id"] == task_id for t in tasks)

            # 4. Get via GET /a2a/tasks/{task_id}
            get_resp = await client.get(f"/a2a/tasks/{task_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["task_id"] == task_id

    async def test_owner_isolation(self, two_agent_setup) -> None:
        """Owner A cannot see or cancel Owner B's tasks."""
        s = two_agent_setup

        async with s["service_a"]._session_factory() as session:
            await s["service_a"]._tasks.upsert(
                session,
                A2ATask(
                    owner_id=s["owner_b"],
                    task_id="secret_task_b",
                    sender_agent_id="b",
                    recipient_agent_id="c",
                    status=TaskStatus.PENDING.value,
                ),
            )
            await session.commit()

        # Owner A tries to get Owner B's task
        task = await s["service_a"].get_task(s["owner_a"], "secret_task_b")
        assert task is None

        # Owner A's list must not contain Owner B's task
        tasks_a = await s["service_a"].list_tasks(s["owner_a"])
        assert not any(t.task_id == "secret_task_b" for t in tasks_a)
