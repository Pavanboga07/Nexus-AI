"""Test suite for Gateway-Centered Agent Discovery & Identity (Part 14).

Verifies:
1. Exact Agent ID lookup (nexus:ed25519:...) -> KNOWN_AGENT or DISCOVERED_AGENT
2. Public handle lookup (@handle) -> KNOWN_AGENT or DISCOVERED_AGENT
3. Display name lookup (Rahul) -> KNOWN_AGENT, DISCOVERED_AGENT, or AMBIGUOUS_AGENT
4. Cryptographic card verification (valid vs forged / tampered signatures)
5. Candidate ambiguity handling
6. Unknown agent handling
7. Trust bootstrap flow (WAITING_FOR_TRUST -> owner trust -> resume same run)
8. Intent parser preservation of Agent IDs and @handles
9. GatewayClient authentication payload with handle & signed agent card
10. Directory search API endpoint with local trust enrichment
11. SSRF validation on discovery endpoints
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.a2a import signing
from app.a2a.cards import build_card
from app.a2a.discovery import DiscoveryService
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.gateway_client import GatewayClient, GatewayA2ATransport
from app.a2a.models import TrustStatus, TrustedAgent
from app.a2a.repository import TrustedAgentRepository
from app.a2a.service import A2AService
from app.a2a.transport import validate_endpoint
from app.database.models import Base, Owner
from app.database.repositories import OwnerRepository
from app.identity import crypto
from app.identity.service import IdentityService, PublicIdentity
from app.orchestration.intent import IntentResolver
from app.orchestration.models import Contact, OrchestrationRun, OrchestrationState
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.planner import OrchestrationPlanner
from app.orchestration.repository import (
    ContactRepository,
    OrchestrationRunRepository,
)
from app.orchestration.schemas import (
    Intent,
    IntentType,
    OrchestrationExecuteResponse,
    OrchestrationTrustRequest,
    TargetResolution,
    TargetResolutionStatus,
)
from app.orchestration.target_resolver import TargetResolver
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService


# SQLite compatibility hook for JSONB
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


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


def _generate_test_keypair():
    """Generate a real Ed25519 keypair for cryptographic card testing."""
    priv, pub = crypto.generate_keypair()
    raw_pub = crypto.public_key_bytes(pub)
    pub_b64 = base64.b64encode(raw_pub).decode("ascii")
    agent_id = crypto.agent_id_from_public_key(raw_pub)
    return priv, raw_pub, pub_b64, agent_id


def _create_signed_card(priv_key, agent_id: str, pub_b64: str, display_name: str, endpoint: str):
    """Build and sign a valid Agent Card using a private key."""
    from app.identity.serialization import canonical_json_bytes
    card = build_card(
        agent_id=agent_id,
        public_key=pub_b64,
        display_name=display_name,
        endpoint=endpoint,
    )
    canonical = canonical_json_bytes(card)
    raw_sig = crypto.sign_bytes(priv_key, canonical)
    card["signature"] = base64.b64encode(raw_sig).decode("ascii")
    return card


# =============================================================================
# 1. Intent Parser Preserves Agent ID and @handle
# =============================================================================


@pytest.mark.asyncio
async def test_intent_parser_preserves_agent_id_and_handle():
    resolver = IntentResolver(llm_provider=None)

    # Exact Agent ID
    agent_id = "nexus:ed25519:abcdef1234567890abcdef1234567890"
    intent1 = await resolver.resolve_intent(f"Ask {agent_id} if he is free tomorrow")
    assert intent1.intent_type == IntentType.CHECK_AVAILABILITY
    assert intent1.target == agent_id

    # Public Handle
    intent2 = await resolver.resolve_intent("Ask @rahul if he is free tomorrow after 6 PM")
    assert intent2.intent_type == IntentType.CHECK_AVAILABILITY
    assert intent2.target == "@rahul"
    assert intent2.constraints.get("time").lower() == "6 pm"

    # Human Display Name
    intent3 = await resolver.resolve_intent("Ask Rahul if he is free tomorrow")
    assert intent3.intent_type == IntentType.CHECK_AVAILABILITY
    assert intent3.target == "Rahul"


# =============================================================================
# 2. Exact Agent ID Lookup: Known vs Discovered
# =============================================================================


@pytest.mark.asyncio
async def test_exact_agent_id_lookup_known_when_trusted(session_factory, test_owner):
    priv, raw_pub, pub_b64, agent_id = _generate_test_keypair()
    trusted_repo = TrustedAgentRepository()

    # Pre-register as trusted
    async with session_factory() as session:
        await trusted_repo.add(
            session,
            TrustedAgent(
                owner_id=test_owner,
                agent_id=agent_id,
                public_key=pub_b64,
                display_name="Trusted Alice",
                endpoint="wss://gateway/ws",
                status=TrustStatus.ACTIVE.value,
            ),
        )
        await session.commit()

    resolver = TargetResolver(
        session_factory=session_factory,
        trusted_agents=trusted_repo,
        gateway_url="wss://gateway.example.com/ws",
    )

    res = await resolver.resolve(test_owner, agent_id)
    assert res.status == TargetResolutionStatus.KNOWN_AGENT
    assert res.is_trusted is True
    assert res.agent_id == agent_id
    assert res.display_name == "Trusted Alice"


@pytest.mark.asyncio
async def test_exact_agent_id_lookup_discovered_from_gateway(session_factory, test_owner):
    priv, raw_pub, pub_b64, agent_id = _generate_test_keypair()
    valid_card = _create_signed_card(priv, agent_id, pub_b64, "Remote Bob", "wss://gateway/ws")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": agent_id,
                "public_key": pub_b64,
                "display_name": "Remote Bob",
                "handle": "bob",
                "agent_card": valid_card,
                "is_online": True,
            },
            request=httpx.Request("GET", f"https://gateway.example.com/agents/{agent_id}"),
        )
    )

    resolver = TargetResolver(
        session_factory=session_factory,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    res = await resolver.resolve(test_owner, agent_id)
    assert res.status == TargetResolutionStatus.DISCOVERED_AGENT
    assert res.is_trusted is False
    assert res.agent_id == agent_id
    assert res.display_name == "Remote Bob"
    assert res.card is not None
    assert res.card["public_key"] == pub_b64


# =============================================================================
# 3. Forged Agent Card Rejection
# =============================================================================


@pytest.mark.asyncio
async def test_forged_agent_card_signature_rejected(session_factory, test_owner):
    priv, raw_pub, pub_b64, agent_id = _generate_test_keypair()
    card = _create_signed_card(priv, agent_id, pub_b64, "Remote Bob", "wss://gateway/ws")

    # Tamper with signature
    card["signature"] = base64.b64encode(b"invalid_signature_bytes_1234567890abcdef").decode("ascii")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"agent_id": agent_id, "agent_card": card},
            request=httpx.Request("GET", f"https://gateway.example.com/agents/{agent_id}"),
        )
    )

    resolver = TargetResolver(
        session_factory=session_factory,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    res = await resolver.resolve(test_owner, agent_id)
    # Verification failed -> not accepted as discovered agent
    assert res.status == TargetResolutionStatus.UNKNOWN_AGENT


# =============================================================================
# 4. Handle Lookup (@handle)
# =============================================================================


@pytest.mark.asyncio
async def test_handle_lookup_via_gateway(session_factory, test_owner):
    priv, raw_pub, pub_b64, agent_id = _generate_test_keypair()
    valid_card = _create_signed_card(priv, agent_id, pub_b64, "Rahul Sharma", "wss://gateway/ws")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": agent_id,
                "public_key": pub_b64,
                "display_name": "Rahul Sharma",
                "handle": "rahul",
                "agent_card": valid_card,
                "is_online": True,
            },
            request=httpx.Request("GET", "https://gateway.example.com/agents/handle/rahul"),
        )
    )

    resolver = TargetResolver(
        session_factory=session_factory,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    res = await resolver.resolve(test_owner, "@rahul")
    assert res.status == TargetResolutionStatus.DISCOVERED_AGENT
    assert res.is_trusted is False
    assert res.agent_id == agent_id
    assert res.display_name == "Rahul Sharma"


# =============================================================================
# 5. Display Name Search & Ambiguity Handling
# =============================================================================


@pytest.mark.asyncio
async def test_display_name_search_single_match(session_factory, test_owner):
    priv, raw_pub, pub_b64, agent_id = _generate_test_keypair()
    valid_card = _create_signed_card(priv, agent_id, pub_b64, "Rahul", "wss://gateway/ws")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "agent_id": agent_id,
                        "display_name": "Rahul",
                        "handle": "rahul",
                        "public_key": pub_b64,
                        "agent_card": valid_card,
                        "is_online": True,
                    }
                ],
                "total": 1,
            },
            request=httpx.Request("GET", "https://gateway.example.com/agents/search?q=Rahul"),
        )
    )

    resolver = TargetResolver(
        session_factory=session_factory,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    res = await resolver.resolve(test_owner, "Rahul")
    assert res.status == TargetResolutionStatus.DISCOVERED_AGENT
    assert res.is_trusted is False
    assert res.agent_id == agent_id
    assert res.display_name == "Rahul"


@pytest.mark.asyncio
async def test_display_name_search_multiple_ambiguous(session_factory, test_owner):
    _, _, pk1, id1 = _generate_test_keypair()
    _, _, pk2, id2 = _generate_test_keypair()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "agents": [
                    {"agent_id": id1, "display_name": "Rahul Sharma", "handle": "rahul_s"},
                    {"agent_id": id2, "display_name": "Rahul Verma", "handle": "rahul_v"},
                ],
                "total": 2,
            },
            request=httpx.Request("GET", "https://gateway.example.com/agents/search?q=Rahul"),
        )
    )

    resolver = TargetResolver(
        session_factory=session_factory,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    res = await resolver.resolve(test_owner, "Rahul")
    assert res.status == TargetResolutionStatus.AMBIGUOUS_AGENT
    assert len(res.candidates) == 2
    assert "Rahul Sharma" in res.candidates
    assert "Rahul Verma" in res.candidates


@pytest.mark.asyncio
async def test_unknown_agent_returns_unknown(session_factory, test_owner):
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            404,
            json={"detail": "Agent not found"},
            request=httpx.Request("GET", "https://gateway.example.com/agents/handle/nonexistent"),
        )
    )

    resolver = TargetResolver(
        session_factory=session_factory,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    res = await resolver.resolve(test_owner, "@nonexistent")
    assert res.status == TargetResolutionStatus.UNKNOWN_AGENT


# =============================================================================
# 6. Trust Bootstrap Flow (WAITING_FOR_TRUST -> Owner Trust -> Resumes)
# =============================================================================


@pytest.mark.asyncio
async def test_trust_bootstrap_waiting_for_trust_and_resume(session_factory, test_owner):
    priv, raw_pub, pub_b64, agent_id = _generate_test_keypair()
    valid_card = _create_signed_card(priv, agent_id, pub_b64, "Discovered Priya", "wss://gateway/ws")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": agent_id,
                "public_key": pub_b64,
                "display_name": "Discovered Priya",
                "handle": "priya",
                "agent_card": valid_card,
            },
            request=httpx.Request("GET", f"https://gateway.example.com/agents/{agent_id}"),
        )
    )

    trusted_repo = TrustedAgentRepository()
    contacts_repo = ContactRepository()
    runs_repo = OrchestrationRunRepository()

    target_resolver = TargetResolver(
        session_factory=session_factory,
        trusted_agents=trusted_repo,
        contacts=contacts_repo,
        gateway_url="wss://gateway.example.com/ws",
        http_client=mock_client,
    )

    a2a_service = MagicMock(spec=A2AService)
    a2a_service.register_trusted_agent = AsyncMock(
        return_value=TrustedAgent(
            agent_id=agent_id,
            display_name="Discovered Priya",
            public_key=pub_b64,
            endpoint="wss://gateway/ws",
            status=TrustStatus.ACTIVE.value,
        )
    )

    policy_service = MagicMock(spec=PolicyService)
    intent_resolver = IntentResolver(llm_provider=None)

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=a2a_service,
        policy_service=policy_service,
        intent_resolver=intent_resolver,
        target_resolver=target_resolver,
    )

    # 1. Execute run with untrusted discovered agent
    res1 = await orchestrator.handle_user_message(
        test_owner,
        session_id="test-session-1",
        message=f"Ask {agent_id} if he is free tomorrow",
    )

    # Must transition to waiting_trust, NOT auto-trusted!
    assert res1.status == "waiting_trust"
    assert res1.requires_approval is True
    run_id = uuid.UUID(res1.run_id)

    async with session_factory() as session:
        run_record = await runs_repo.get_by_id(session, test_owner, run_id)
        assert run_record.state == OrchestrationState.WAITING_FOR_TRUST.value
        assert run_record.target_agent_id == agent_id

    # 2. Owner explicitly approves trust via trust_and_resume_run
    # Mock executor for post-trust resumption
    orchestrator._executor.execute_plan = AsyncMock(
        return_value={"status": "completed", "message": "Priya confirmed free tomorrow."}
    )

    # Also update mock_client to return known on second resolution
    async with session_factory() as session:
        await trusted_repo.add(
            session,
            TrustedAgent(
                owner_id=test_owner,
                agent_id=agent_id,
                public_key=pub_b64,
                display_name="Discovered Priya",
                endpoint="wss://gateway/ws",
                status=TrustStatus.ACTIVE.value,
            ),
        )
        await session.commit()

    res2 = await orchestrator.trust_and_resume_run(
        test_owner,
        run_id,
        req=OrchestrationTrustRequest(display_name="Discovered Priya"),
    )

    assert res2.run_id == str(run_id)
    assert res2.status == "completed"

    async with session_factory() as session:
        run_after = await runs_repo.get_by_id(session, test_owner, run_id)
        assert run_after.state == OrchestrationState.COMPLETED.value


# =============================================================================
# 7. GatewayClient Auth Response Transmits Handle & Card
# =============================================================================


@pytest.mark.asyncio
async def test_gateway_client_auth_response_includes_card_and_handle():
    ident = MagicMock(spec=IdentityService)
    ident.get_public_identity.return_value = PublicIdentity(
        agent_id="nexus:ed25519:11112222333344445555666677778888",
        public_key="bW9ja19wdWJsaWNfa2V5",
        key_algorithm="Ed25519",
        fingerprint="1111-2222-3333-4444",
    )
    ident.sign = AsyncMock(return_value=b"0" * 64)

    client = GatewayClient(
        gateway_url="wss://gateway.example.com/ws",
        identity_service=ident,
        display_name="Test Agent",
        handle="testagent",
    )

    mock_ws = AsyncMock()
    # Challenge frame from server
    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps({"type": "auth_challenge", "challenge": base64.b64encode(b"random_challenge_bytes_32bytes!!").decode("ascii")}),
            json.dumps({"type": "auth_result", "success": True, "agent_id": "nexus:ed25519:11112222333344445555666677778888"}),
        ]
    )

    await client._authenticate(mock_ws)

    # Check what client sent
    mock_ws.send.assert_called_once()
    sent_frame = json.loads(mock_ws.send.call_args[0][0])

    assert sent_frame["type"] == "auth_response"
    assert sent_frame["agent_id"] == "nexus:ed25519:11112222333344445555666677778888"
    assert sent_frame["display_name"] == "Test Agent"
    assert sent_frame["handle"] == "testagent"
    assert "agent_card" in sent_frame
    assert sent_frame["agent_card"]["agent_id"] == "nexus:ed25519:11112222333344445555666677778888"


# =============================================================================
# 8. Gateway Routing and Offline Queue
# =============================================================================


@pytest.mark.asyncio
async def test_gateway_transport_routing_and_offline_queue():
    client = MagicMock(spec=GatewayClient)
    client.is_connected = True
    client.send_relay_envelope = AsyncMock(return_value={"status": "queued", "relay_id": "relay_123"})

    mock_http = MagicMock()
    transport = GatewayA2ATransport(http_transport=mock_http, gateway_client=client)

    envelope = {
        "protocol": "nexus-a2a",
        "version": "0.1",
        "message_id": "msg-1",
        "task_id": "task-1",
        "sender": "nexus:ed25519:11112222333344445555666677778888",
        "recipient": "nexus:ed25519:99998888777766665555444433332222",
        "message_type": "request",
        "purpose": "availability",
        "payload": {},
    }

    result = await transport.send("wss://gateway.example.com/ws", envelope)
    assert result["status"] == "queued"
    assert result["relay_id"] == "relay_123"
    client.send_relay_envelope.assert_called_once_with(envelope)


# =============================================================================
# 9. SSRF Protection on Discovery Endpoints
# =============================================================================


def test_ssrf_protection_rejects_private_ips():
    # Loopback and private ranges must raise when allow_local=False
    with pytest.raises(A2AError) as exc1:
        validate_endpoint("http://127.0.0.1:8000/a2a/messages", allow_local=False)
    assert exc1.value.code == A2AErrorCode.INVALID_ENDPOINT

    with pytest.raises(A2AError) as exc2:
        validate_endpoint("http://192.168.1.10/a2a/messages", allow_local=False)
    assert exc2.value.code == A2AErrorCode.INVALID_ENDPOINT

    with pytest.raises(A2AError) as exc3:
        validate_endpoint("http://169.254.169.254/latest/meta-data", allow_local=False)
    assert exc3.value.code == A2AErrorCode.INVALID_ENDPOINT

    # Public domain passes
    endpoint = validate_endpoint("https://remote.agent.nexus.example.com/a2a/messages", allow_local=False)
    assert endpoint.startswith("https://")
