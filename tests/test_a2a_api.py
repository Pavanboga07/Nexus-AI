"""A2A API tests: trusted-agent CRUD, inbound messages, outbound send, audit."""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.a2a import signing
from app.a2a.schemas import new_message_id, new_task_id, utc_iso_in, utc_now_iso
from app.schemas.a2a import A2AMessageIn

pytestmark = pytest.mark.asyncio

TEST_ENDPOINT = "http://127.0.0.1:9999/a2a/messages"


@pytest_asyncio.fixture
async def local_agent_id(db_a2a_client) -> str:
    response = await db_a2a_client.get("/identity")
    assert response.status_code == 200
    return response.json()["agent_id"]


@pytest.fixture
def sender(db_a2a_app):
    from tests.test_a2a_service import StubIdentity

    return StubIdentity()


async def _register_sender(client, sender, endpoint=TEST_ENDPOINT) -> dict:
    response = await client.post(
        "/a2a/agents",
        json={
            "agent_id": sender.agent_id,
            "public_key": sender.public_key_b64,
            "display_name": "Remote Agent",
            "endpoint": endpoint,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _allow_sender(db_a2a_app, sender, scope="exact") -> None:
    await db_a2a_app.state.policy_service.create_policy(
        await db_a2a_app.state.agent.owner_id(),
        requester_agent_id=sender.agent_id,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
        decision="ALLOW",
        disclosure_scope=scope,
    )


async def _signed_envelope(sender, recipient_agent_id, **overrides) -> dict:
    from app.a2a.schemas import A2AEnvelope

    payload = {
        "action": "disclose_information",
        "data_category": "availability",
    }
    envelope = A2AEnvelope(
        message_id=new_message_id(),
        task_id=new_task_id(),
        sender=sender.agent_id,
        recipient=recipient_agent_id,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(60),
        message_type="request",
        purpose="scheduling",
        payload=payload,
    )
    signed = await signing.sign_envelope(sender, envelope)
    data = signed.model_dump()
    data.update(overrides)
    return data


# --- Trusted-agent CRUD ------------------------------------------------------------


async def test_register_and_list_agents(db_a2a_client, sender) -> None:
    created = await _register_sender(db_a2a_client, sender)
    assert created["status"] == "active"
    assert created["display_name"] == "Remote Agent"

    listed = await db_a2a_client.get("/a2a/agents")
    body = listed.json()
    assert body["total"] == 1
    assert body["agents"][0]["agent_id"] == sender.agent_id
    # Public metadata only.
    assert "public_key" not in body["agents"][0]


async def test_get_agent(db_a2a_client, sender) -> None:
    await _register_sender(db_a2a_client, sender)
    response = await db_a2a_client.get(f"/a2a/agents/{sender.agent_id}")
    assert response.status_code == 200
    assert response.json()["agent_id"] == sender.agent_id


async def test_get_unknown_agent_404(db_a2a_client, sender) -> None:
    response = await db_a2a_client.get(
        "/a2a/agents/nexus:ed25519:" + "0" * 32
    )
    assert response.status_code == 404


async def test_register_rejects_mismatched_identity(db_a2a_client, sender) -> None:
    from tests.test_a2a_service import StubIdentity

    other = StubIdentity()
    response = await db_a2a_client.post(
        "/a2a/agents",
        json={
            "agent_id": sender.agent_id,
            "public_key": other.public_key_b64,  # wrong key
            "display_name": "Impostor",
            "endpoint": TEST_ENDPOINT,
        },
    )
    assert response.status_code == 401  # IDENTITY_MISMATCH


async def test_register_rejects_bad_endpoint_scheme(db_a2a_client, sender) -> None:
    response = await db_a2a_client.post(
        "/a2a/agents",
        json={
            "agent_id": sender.agent_id,
            "public_key": sender.public_key_b64,
            "display_name": "X",
            "endpoint": "ftp://host/messages",
        },
    )
    assert response.status_code == 422


async def test_revoke_agent(db_a2a_client, sender) -> None:
    await _register_sender(db_a2a_client, sender)
    response = await db_a2a_client.post(f"/a2a/agents/{sender.agent_id}/revoke")
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


async def test_delete_agent(db_a2a_client, sender) -> None:
    await _register_sender(db_a2a_client, sender)
    response = await db_a2a_client.delete(f"/a2a/agents/{sender.agent_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True

    missing = await db_a2a_client.delete(f"/a2a/agents/{sender.agent_id}")
    assert missing.status_code == 404


# --- Inbound messages -----------------------------------------------------------------


async def test_inbound_message_full_flow(
    db_a2a_client, db_a2a_app, sender, local_agent_id, memory_manager
) -> None:
    await _register_sender(db_a2a_client, sender)
    await _allow_sender(db_a2a_app, sender, scope="exact")
    owner_id = await db_a2a_app.state.agent.owner_id()
    await memory_manager.store_memory(
        owner_id, memory_type="semantic",
        content="Boss availability is after 6 PM.",
    )

    envelope = await _signed_envelope(sender, local_agent_id)
    response = await db_a2a_client.post("/a2a/messages", json=envelope)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message_type"] == "response"
    assert body["sender"] == local_agent_id
    assert body["payload"]["status"] == "completed"
    assert "after 6 PM" in str(body["payload"].get("memories"))

    # The response signature verifies under the local identity's key.
    from app.a2a.schemas import A2AEnvelope

    parsed = A2AEnvelope.model_validate(body)
    public_key = db_a2a_app.state.identity_service.get_public_identity().public_key
    assert signing.verify_envelope_signature(parsed, public_key)


async def test_inbound_untrusted_sender_401(
    db_a2a_client, sender, local_agent_id
) -> None:
    envelope = await _signed_envelope(sender, local_agent_id)
    response = await db_a2a_client.post("/a2a/messages", json=envelope)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNTRUSTED_SENDER"


async def test_inbound_tampered_message_401(
    db_a2a_client, sender, local_agent_id
) -> None:
    await _register_sender(db_a2a_client, sender)
    envelope = await _signed_envelope(sender, local_agent_id)
    envelope["payload"]["data_category"] = "financial"  # tampered after signing
    response = await db_a2a_client.post("/a2a/messages", json=envelope)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_SIGNATURE"


async def test_inbound_replay_409(
    db_a2a_client, db_a2a_app, sender, local_agent_id
) -> None:
    await _register_sender(db_a2a_client, sender)
    await _allow_sender(db_a2a_app, sender)

    envelope = await _signed_envelope(sender, local_agent_id)
    first = await db_a2a_client.post("/a2a/messages", json=envelope)
    assert first.status_code == 200

    second = await db_a2a_client.post("/a2a/messages", json=envelope)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "REPLAY"


async def test_inbound_ask_returns_approval_required(
    db_a2a_client, sender, local_agent_id
) -> None:
    await _register_sender(db_a2a_client, sender)
    # No policy -> ASK.
    envelope = await _signed_envelope(sender, local_agent_id)
    response = await db_a2a_client.post("/a2a/messages", json=envelope)
    assert response.status_code == 200
    assert response.json()["payload"]["status"] == "approval_required"


async def test_inbound_revoked_sender_401(
    db_a2a_client, sender, local_agent_id
) -> None:
    await _register_sender(db_a2a_client, sender)
    await db_a2a_client.post(f"/a2a/agents/{sender.agent_id}/revoke")
    envelope = await _signed_envelope(sender, local_agent_id)
    response = await db_a2a_client.post("/a2a/messages", json=envelope)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REVOKED_SENDER"


# --- Outbound send ------------------------------------------------------------------------


async def test_send_via_loopback(
    db_a2a_client, db_a2a_app, sender, local_agent_id
) -> None:
    from app.a2a.schemas import A2AEnvelope
    from tests.test_a2a_service import LoopbackTransport

    await _register_sender(db_a2a_client, sender, endpoint=TEST_ENDPOINT)

    async def handler(envelope: A2AEnvelope) -> A2AEnvelope:
        response = A2AEnvelope(
            message_id=new_message_id(),
            task_id=envelope.task_id,
            sender=sender.agent_id,
            recipient=envelope.sender,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="response",
            purpose=envelope.purpose,
            payload={"status": "completed", "answer": "available after 6 PM"},
        )
        return await signing.sign_envelope(sender, response)

    transport: LoopbackTransport = db_a2a_app.state.a2a_service._transport  # type: ignore[assignment]
    transport.handler = handler

    response = await db_a2a_client.post(
        "/a2a/send",
        json={
            "recipient_agent_id": sender.agent_id,
            "purpose": "scheduling",
            "action": "disclose_information",
            "data_category": "availability",
            "payload": {"requested_time": "2026-09-15T18:00:00+05:30"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["payload"]["answer"] == "available after 6 PM"


async def test_send_untrusted_recipient_404(db_a2a_client, sender) -> None:
    response = await db_a2a_client.post(
        "/a2a/send",
        json={
            "recipient_agent_id": sender.agent_id,
            "purpose": "scheduling",
            "action": "disclose_information",
            "data_category": "availability",
        },
    )
    assert response.status_code == 404


# --- Audit -----------------------------------------------------------------------------------


async def test_a2a_audit_endpoint(
    db_a2a_client, db_a2a_app, sender, local_agent_id
) -> None:
    await _register_sender(db_a2a_client, sender)
    envelope = await _signed_envelope(sender, local_agent_id)
    await db_a2a_client.post("/a2a/messages", json=envelope)

    response = await db_a2a_client.get("/a2a/audit/list")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    entry = body["messages"][0]
    assert entry["status"] == "approval_required"
    assert entry["policy_decision"] == "ASK"
    # Metadata only.
    assert "payload" not in entry
    assert "signature" not in entry


async def test_a2a_service_unavailable_503(db_client) -> None:
    """Plain db_client has no A2A service attached."""
    response = await db_client.get("/a2a/agents")
    assert response.status_code == 503
