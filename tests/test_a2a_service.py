"""A2AService integration tests: the full inbound/outbound pipelines.

Uses throwaway Ed25519 identities (no DB identity rows needed - the receiver
only needs the trusted-agent record) and a loopback transport (no network).
Requires PostgreSQL for policy/memory/trusted-agent persistence.
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio

from app.a2a import signing
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.rate_limit import SlidingWindowRateLimiter
from app.a2a.schemas import (
    A2AEnvelope,
    new_message_id,
    new_task_id,
    utc_iso_in,
    utc_now_iso,
)
from app.a2a.service import A2AService
from app.identity import crypto
from app.identity.service import PublicIdentity
from app.policy.service import PolicyService

pytestmark = pytest.mark.asyncio

TEST_ENDPOINT = "http://127.0.0.1:9999/a2a/messages"


class StubIdentity:
    """Signs with a throwaway keypair; satisfies the A2AService interface
    (sign + get_public_identity) without touching the identity DB."""

    def __init__(self) -> None:
        private_key, public_key = crypto.generate_keypair()
        self._private = private_key
        raw = crypto.public_key_bytes(public_key)
        self.public_key_b64 = base64.b64encode(raw).decode("ascii")
        self.agent_id = crypto.agent_id_from_public_key(raw)

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private, data)

    def get_public_identity(self) -> PublicIdentity:
        return PublicIdentity(
            agent_id=self.agent_id,
            public_key=self.public_key_b64,
            key_algorithm="Ed25519",
            fingerprint="STUB",
        )

    @property
    def ready(self) -> bool:
        return True


class LoopbackTransport:
    """Delivers envelopes to an in-process handler; records traffic."""

    def __init__(self) -> None:
        self.handler = None  # async (A2AEnvelope) -> A2AEnvelope
        self.sent: list[dict] = []

    async def send(self, endpoint: str, envelope: dict) -> dict:
        self.sent.append(envelope)
        if self.handler is None:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR, "No loopback handler installed."
            )
        response = await self.handler(A2AEnvelope.model_validate(envelope))
        return response.model_dump()


def _make_service(
    session_factory,
    policy_service: PolicyService,
    memory_manager,
    identity: StubIdentity,
    *,
    rate_limit: int = 60,
    max_message_bytes: int = 65_536,
) -> A2AService:
    return A2AService(
        session_factory=session_factory,
        identity_service=identity,
        policy_service=policy_service,
        memory_manager=memory_manager,
        transport=LoopbackTransport(),
        rate_limiter=SlidingWindowRateLimiter(rate_limit),
        max_message_bytes=max_message_bytes,
        allow_local_endpoints=True,
    )


async def _register(
    service: A2AService, owner_id, identity: StubIdentity, display: str = "Remote"
) -> None:
    await service.register_trusted_agent(
        owner_id,
        agent_id=identity.agent_id,
        public_key=identity.public_key_b64,
        display_name=display,
        endpoint=TEST_ENDPOINT,
    )


async def _signed_request(
    sender: StubIdentity,
    recipient_agent_id: str,
    *,
    purpose: str = "scheduling",
    action: str = "disclose_information",
    data_category: str = "availability",
    payload_extra: dict | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    timestamp: str | None = None,
    expires_at: str | None = None,
) -> A2AEnvelope:
    payload = {"action": action, "data_category": data_category}
    if payload_extra:
        payload.update(payload_extra)
    envelope = A2AEnvelope(
        message_id=message_id or new_message_id(),
        task_id=task_id or new_task_id(),
        sender=sender.agent_id,
        recipient=recipient_agent_id,
        timestamp=timestamp or utc_now_iso(),
        expires_at=expires_at or utc_iso_in(60),
        message_type="request",
        purpose=purpose,
        payload=payload,
    )
    return await signing.sign_envelope(sender, envelope)


@pytest.fixture
def receiver_identity() -> StubIdentity:
    return StubIdentity()


@pytest.fixture
def sender_identity() -> StubIdentity:
    return StubIdentity()


@pytest.fixture
def receiver_service(
    db_session_factory, policy_service, memory_manager, receiver_identity
) -> A2AService:
    return _make_service(
        db_session_factory, policy_service, memory_manager, receiver_identity
    )


async def _allow(
    policy_service: PolicyService,
    owner_id,
    requester_agent_id: str,
    *,
    data_category: str = "availability",
    purpose: str = "scheduling",
    scope: str = "exact",
) -> None:
    await policy_service.create_policy(
        owner_id,
        requester_agent_id=requester_agent_id,
        data_category=data_category,
        action="disclose_information",
        purpose=purpose,
        decision="ALLOW",
        disclosure_scope=scope,
    )


async def _store_memory(memory_manager, owner_id, content: str) -> None:
    await memory_manager.store_memory(
        owner_id, memory_type="semantic", content=content
    )


# --- Trusted-agent registration ---------------------------------------------------


async def test_register_rejects_key_agent_id_mismatch(
    receiver_service, db_owner_id, sender_identity, receiver_identity
) -> None:
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.register_trusted_agent(
            db_owner_id,
            agent_id=sender_identity.agent_id,  # claims sender's id
            public_key=receiver_identity.public_key_b64,  # but receiver's key
            display_name="Liar",
            endpoint=TEST_ENDPOINT,
        )
    assert excinfo.value.code is A2AErrorCode.IDENTITY_MISMATCH


async def test_register_rejects_bad_endpoint(receiver_service, db_owner_id, sender_identity) -> None:
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.register_trusted_agent(
            db_owner_id,
            agent_id=sender_identity.agent_id,
            public_key=sender_identity.public_key_b64,
            display_name="X",
            endpoint="file:///etc/passwd",
        )
    assert excinfo.value.code is A2AErrorCode.INVALID_ENDPOINT


async def test_register_duplicate_rejected(
    receiver_service, db_owner_id, sender_identity
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    with pytest.raises(A2AError) as excinfo:
        await _register(receiver_service, db_owner_id, sender_identity)
    assert excinfo.value.code is A2AErrorCode.CONFLICT


async def test_trusted_agent_owner_isolation(
    receiver_service, owner_ids, sender_identity
) -> None:
    owner_a, owner_b = owner_ids
    await _register(receiver_service, owner_a, sender_identity)
    assert await receiver_service.get_trusted_agent(
        owner_b, sender_identity.agent_id
    ) is None
    assert await receiver_service.list_trusted_agents(owner_b) == []
    assert (
        await receiver_service.revoke_trusted_agent(
            owner_b, sender_identity.agent_id
        )
        is None
    )


# --- Inbound: verification pipeline ----------------------------------------------


async def test_inbound_unknown_sender_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity
) -> None:
    # No registration at all.
    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.UNTRUSTED_SENDER


async def test_inbound_revoked_sender_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await receiver_service.revoke_trusted_agent(
        db_owner_id, sender_identity.agent_id
    )
    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.REVOKED_SENDER


async def test_inbound_wrong_recipient_rejected(
    receiver_service, db_owner_id, sender_identity
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    other = StubIdentity()
    envelope = await _signed_request(sender_identity, other.agent_id)
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.NOT_ADDRESSED_TO_US


async def test_inbound_tampered_payload_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    signed = await _signed_request(sender_identity, receiver_identity.agent_id)
    tampered = signed.model_copy(
        update={
            "payload": {
                "action": "disclose_information",
                "data_category": "financial",  # swapped after signing
            }
        }
    )
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, tampered)
    assert excinfo.value.code is A2AErrorCode.INVALID_SIGNATURE


async def test_inbound_registered_key_mismatch_rejected(
    receiver_service, db_session_factory, db_owner_id,
    sender_identity, receiver_identity,
) -> None:
    """Corrupt the registered key (agent_id no longer matches it)."""
    await _register(receiver_service, db_owner_id, sender_identity)
    # Directly overwrite the stored key with a different valid key.
    from app.a2a.models import TrustedAgent

    async with db_session_factory() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(TrustedAgent).where(
                    TrustedAgent.owner_id == db_owner_id,
                    TrustedAgent.agent_id == sender_identity.agent_id,
                )
            )
        ).scalar_one()
        row.public_key = receiver_identity.public_key_b64
        await session.commit()

    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.IDENTITY_MISMATCH


async def test_inbound_expired_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    envelope = await _signed_request(
        sender_identity,
        receiver_identity.agent_id,
        expires_at=utc_now_iso(),  # already expired
    )
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.EXPIRED


async def test_inbound_future_timestamp_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity
) -> None:
    from datetime import datetime, timedelta, timezone

    await _register(receiver_service, db_owner_id, sender_identity)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    envelope = await _signed_request(
        sender_identity,
        receiver_identity.agent_id,
        timestamp=future.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.CLOCK_SKEW


async def test_inbound_replay_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity, policy_service
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await _allow(policy_service, db_owner_id, sender_identity.agent_id)

    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    first = await receiver_service.handle_inbound(db_owner_id, envelope)
    assert first.message_type == "response"

    # Exact same signed message again -> REPLAY.
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.REPLAY


async def test_inbound_rate_limited(
    db_session_factory, policy_service, memory_manager,
    db_owner_id, sender_identity, receiver_identity,
) -> None:
    service = _make_service(
        db_session_factory, policy_service, memory_manager,
        receiver_identity, rate_limit=1,
    )
    await _register(service, db_owner_id, sender_identity)
    await _allow(policy_service, db_owner_id, sender_identity.agent_id)

    first = await _signed_request(sender_identity, receiver_identity.agent_id)
    await service.handle_inbound(db_owner_id, first)
    second = await _signed_request(sender_identity, receiver_identity.agent_id)
    with pytest.raises(A2AError) as excinfo:
        await service.handle_inbound(db_owner_id, second)
    assert excinfo.value.code is A2AErrorCode.RATE_LIMITED


async def test_oversized_message_rejected(receiver_service) -> None:
    with pytest.raises(A2AError) as excinfo:
        receiver_service.check_size(b"x" * (receiver_service._max_message_bytes + 1))
    assert excinfo.value.code is A2AErrorCode.MESSAGE_TOO_LARGE
    # At the limit is fine.
    receiver_service.check_size(b"x" * min(1024, receiver_service._max_message_bytes))


# --- Inbound: policy outcomes ----------------------------------------------------


async def test_inbound_ask_when_no_policy(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
    memory_manager, db_session_factory,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await _store_memory(
        memory_manager, db_owner_id, "Boss availability is after 6 PM."
    )

    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    response = await receiver_service.handle_inbound(db_owner_id, envelope)

    assert response.payload["status"] == "approval_required"
    assert "memories" not in response.payload
    assert "summary" not in response.payload


async def test_inbound_allow_exact_discloses_bounded_memories(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
    memory_manager, policy_service,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await _allow(
        policy_service, db_owner_id, sender_identity.agent_id, scope="exact"
    )
    await _store_memory(
        memory_manager, db_owner_id, "Boss availability is after 6 PM."
    )

    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    response = await receiver_service.handle_inbound(db_owner_id, envelope)

    assert response.payload["status"] == "completed"
    assert "after 6 PM" in str(response.payload.get("memories"))
    # The response itself is signed by the receiver.
    assert signing.verify_envelope_signature(
        response, receiver_identity.public_key_b64
    )


async def test_inbound_allow_summary_never_leaks_content(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
    memory_manager, policy_service,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await _allow(
        policy_service, db_owner_id, sender_identity.agent_id, scope="summary"
    )
    await _store_memory(
        memory_manager, db_owner_id, "Boss availability is after 6 PM."
    )

    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    response = await receiver_service.handle_inbound(db_owner_id, envelope)

    payload = response.payload
    assert payload["status"] == "completed"
    assert payload["summary"] == ["1 memory regarding availability"]
    assert "6 PM" not in str(payload)
    assert "memories" not in payload


async def test_inbound_deny_returns_no_data(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
    memory_manager, policy_service,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await policy_service.create_policy(
        db_owner_id,
        requester_agent_id=sender_identity.agent_id,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
        decision="DENY",
        priority=10,
    )
    await _store_memory(
        memory_manager, db_owner_id, "Boss availability is after 6 PM."
    )

    envelope = await _signed_request(sender_identity, receiver_identity.agent_id)
    response = await receiver_service.handle_inbound(db_owner_id, envelope)

    assert response.payload["status"] == "rejected"
    assert "memories" not in response.payload
    assert "summary" not in response.payload


async def test_inbound_purpose_mismatch_not_authorized(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
    policy_service,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    await _allow(policy_service, db_owner_id, sender_identity.agent_id)

    envelope = await _signed_request(
        sender_identity, receiver_identity.agent_id, purpose="marketing"
    )
    response = await receiver_service.handle_inbound(db_owner_id, envelope)
    assert response.payload["status"] == "approval_required"


async def test_inbound_sensitive_category_default_denies(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    envelope = await _signed_request(
        sender_identity, receiver_identity.agent_id, data_category="financial"
    )
    response = await receiver_service.handle_inbound(db_owner_id, envelope)
    assert response.payload["status"] == "rejected"


async def test_inbound_invalid_payload_rejected(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
) -> None:
    await _register(receiver_service, db_owner_id, sender_identity)
    envelope = await _signed_request(
        sender_identity,
        receiver_identity.agent_id,
        payload_extra={"action": "not-a-slug!!"},
    )
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.INVALID_ENVELOPE


async def test_audit_trail_metadata_only(
    receiver_service, db_owner_id, sender_identity, receiver_identity,
    policy_service,
) -> None:
    import json

    await _register(receiver_service, db_owner_id, sender_identity)
    await _allow(policy_service, db_owner_id, sender_identity.agent_id)

    envelope = await _signed_request(
        sender_identity,
        receiver_identity.agent_id,
        payload_extra={"requested_time": "2026-09-15T18:00:00+05:30"},
    )
    await receiver_service.handle_inbound(db_owner_id, envelope)

    records = await receiver_service.list_audit(db_owner_id)
    assert len(records) == 1
    record = records[0]
    assert record.status == "accepted"
    assert record.policy_decision == "ALLOW"
    serialized = json.dumps(record.to_dict())
    # Never store payloads / signatures / keys.
    assert "requested_time" not in serialized
    assert "BEGIN PRIVATE KEY" not in serialized
    assert "signature" not in serialized


# --- Outbound: full loopback A <-> B ---------------------------------------------


async def test_full_loopback_exchange(
    db_session_factory, policy_service, memory_manager, owner_ids
) -> None:
    """A sends a signed request -> B verifies -> policy ALLOW -> B discloses
    (bounded) -> A verifies B's signed response."""
    owner_a, owner_b = owner_ids
    identity_a = StubIdentity()
    identity_b = StubIdentity()

    service_a = _make_service(
        db_session_factory, policy_service, memory_manager, identity_a
    )
    service_b = _make_service(
        db_session_factory, policy_service, memory_manager, identity_b
    )

    # Each side registers the other as trusted.
    await _register(service_a, owner_a, identity_b, display="Agent B")
    await _register(service_b, owner_b, identity_a, display="Agent A")

    # B allows A to ask about availability for scheduling, exact scope.
    await _allow(policy_service, owner_b, identity_a.agent_id, scope="exact")
    await _store_memory(
        memory_manager, owner_b, "Boss-B availability is after 6 PM."
    )

    # Loopback: A's transport hands envelopes to B's inbound handler.
    transport_a: LoopbackTransport = service_a._transport  # type: ignore[assignment]
    transport_a.handler = lambda env: service_b.handle_inbound(owner_b, env)

    result = await service_a.send_request(
        owner_a,
        recipient_agent_id=identity_b.agent_id,
        purpose="scheduling",
        action="disclose_information",
        data_category="availability",
        payload={"requested_time": "2026-09-15T18:00:00+05:30"},
    )

    assert result["status"] == "completed"
    assert "after 6 PM" in str(result["payload"].get("memories"))


async def test_outbound_untrusted_recipient_rejected(
    receiver_service, db_owner_id, sender_identity
) -> None:
    with pytest.raises(A2AError) as excinfo:
        await receiver_service.send_request(
            db_owner_id,
            recipient_agent_id=sender_identity.agent_id,
            purpose="scheduling",
            action="disclose_information",
            data_category="availability",
        )
    assert excinfo.value.code is A2AErrorCode.NOT_FOUND


async def test_outbound_response_from_wrong_agent_rejected(
    db_session_factory, policy_service, memory_manager, owner_ids
) -> None:
    owner_a, owner_b = owner_ids
    identity_a = StubIdentity()
    identity_b = StubIdentity()
    impostor = StubIdentity()

    service_a = _make_service(
        db_session_factory, policy_service, memory_manager, identity_a
    )
    await _register(service_a, owner_a, identity_b)

    async def impostor_handler(envelope: A2AEnvelope) -> A2AEnvelope:
        # Respond signed by the impostor, not by B.
        response = A2AEnvelope(
            message_id=new_message_id(),
            task_id=envelope.task_id,
            sender=impostor.agent_id,
            recipient=envelope.sender,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="response",
            purpose=envelope.purpose,
            payload={"status": "completed", "memories": ["stolen data"]},
        )
        return await signing.sign_envelope(impostor, response)

    transport_a: LoopbackTransport = service_a._transport  # type: ignore[assignment]
    transport_a.handler = impostor_handler

    with pytest.raises(A2AError) as excinfo:
        await service_a.send_request(
            owner_a,
            recipient_agent_id=identity_b.agent_id,
            purpose="scheduling",
            action="disclose_information",
            data_category="availability",
        )
    assert excinfo.value.code is A2AErrorCode.INVALID_RESPONSE


async def test_outbound_response_wrong_task_id_rejected(
    db_session_factory, policy_service, memory_manager, owner_ids
) -> None:
    owner_a, owner_b = owner_ids
    identity_a = StubIdentity()
    identity_b = StubIdentity()

    service_a = _make_service(
        db_session_factory, policy_service, memory_manager, identity_a
    )
    await _register(service_a, owner_a, identity_b)

    async def mismatched_handler(envelope: A2AEnvelope) -> A2AEnvelope:
        response = A2AEnvelope(
            message_id=new_message_id(),
            task_id="task_some_other_task",  # not the request's task
            sender=identity_b.agent_id,
            recipient=envelope.sender,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="response",
            purpose=envelope.purpose,
            payload={"status": "completed"},
        )
        return await signing.sign_envelope(identity_b, response)

    transport_a: LoopbackTransport = service_a._transport  # type: ignore[assignment]
    transport_a.handler = mismatched_handler

    with pytest.raises(A2AError) as excinfo:
        await service_a.send_request(
            owner_a,
            recipient_agent_id=identity_b.agent_id,
            purpose="scheduling",
            action="disclose_information",
            data_category="availability",
        )
    assert excinfo.value.code is A2AErrorCode.INVALID_RESPONSE


async def test_outbound_unsigned_response_rejected(
    db_session_factory, policy_service, memory_manager, owner_ids
) -> None:
    owner_a, _ = owner_ids
    identity_a = StubIdentity()
    identity_b = StubIdentity()

    service_a = _make_service(
        db_session_factory, policy_service, memory_manager, identity_a
    )
    await _register(service_a, owner_a, identity_b)

    async def unsigned_handler(envelope: A2AEnvelope) -> A2AEnvelope:
        return A2AEnvelope(
            message_id=new_message_id(),
            task_id=envelope.task_id,
            sender=identity_b.agent_id,
            recipient=envelope.sender,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="response",
            purpose=envelope.purpose,
            payload={"status": "completed"},
            signature=None,  # unsigned!
        )

    transport_a: LoopbackTransport = service_a._transport  # type: ignore[assignment]
    transport_a.handler = unsigned_handler

    with pytest.raises(A2AError) as excinfo:
        await service_a.send_request(
            owner_a,
            recipient_agent_id=identity_b.agent_id,
            purpose="scheduling",
            action="disclose_information",
            data_category="availability",
        )
    assert excinfo.value.code is A2AErrorCode.INVALID_RESPONSE
