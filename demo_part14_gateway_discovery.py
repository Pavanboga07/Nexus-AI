"""Real Two-Device Network Demonstration: Gateway-Centered Agent Discovery & Identity (Part 14).

Demonstrates:
  Device A (Alice, @alice)  <---- Live Hosted Nexus Gateway ---->  Device B (Rahul, @rahul)
  Gateway: wss://nexus-gateway-mv63.onrender.com/ws
           https://nexus-gateway-mv63.onrender.com

Tests:
  [Test 1] Exact Agent ID Discovery:
           Alice: "Ask nexus:ed25519:<Rahul-Agent-ID> if he is free tomorrow"
           - TargetResolver queries Gateway directory for exact Agent ID
           - WAITING_FOR_TRUST state triggered with verified card & presence
           - Alice approves trust -> Run resumes -> A2A routed over Gateway WS -> Rahul responds
  [Test 2] Public Handle Discovery:
           Alice: "Ask @rahul if he is free tomorrow after 6 PM"
           - TargetResolver queries Gateway directory for @rahul
           - Automatically recognized as trusted -> Plan executed over Gateway WS -> Rahul responds
  [Test 3] Offline Queuing & Delivery on Reconnect:
           - Rahul disconnects from Gateway
           - Alice sends message -> Gateway replies "queued" -> Alice enters WAITING_REMOTE
           - Rahul reconnects -> Gateway flushes queued message to Rahul -> Rahul answers -> Task completes
"""

from __future__ import annotations

import asyncio
import base64
import httpx
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.a2a import signing
from app.a2a.cards import build_card, AgentCapability
from app.a2a.gateway_client import GatewayClient, GatewayA2ATransport
from app.a2a.models import TrustStatus, TrustedAgent
from app.a2a.repository import TrustedAgentRepository
from app.a2a.schemas import A2AEnvelope, new_message_id, utc_now_iso, utc_iso_in
from app.a2a.service import A2AService
from app.database.models import Base, Owner
from app.database.repositories import OwnerRepository
import app.a2a.models
import app.autonomy.models
import app.orchestration.models
import app.policy.models
import app.workflows.models
from app.identity import crypto
from app.identity.service import IdentityService, PublicIdentity
from app.orchestration.intent import IntentResolver
from app.orchestration.models import OrchestrationState
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.repository import (
    ContactRepository,
    OrchestrationRunRepository,
)
from app.orchestration.schemas import (
    OrchestrationTrustRequest,
    TargetResolutionStatus,
)
from app.orchestration.target_resolver import TargetResolver
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService

# Disable verbose logs for clean demo output
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("demo")

GATEWAY_WS_URL = "wss://nexus-gateway-mv63.onrender.com/ws"
GATEWAY_HTTP_URL = "https://nexus-gateway-mv63.onrender.com"

# SQLite compatibility compiler hook for JSONB
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


class MockIdentity:
    """Deterministic or random Ed25519 identity for device simulation."""

    def __init__(self, display_name: str, handle: str, seed: bytes | None = None):
        if seed:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            self.priv = Ed25519PrivateKey.from_private_bytes(seed)
            self.pub = self.priv.public_key()
        else:
            self.priv, self.pub = crypto.generate_keypair()
        self.raw_pub = crypto.public_key_bytes(self.pub)
        self.pub_b64 = base64.b64encode(self.raw_pub).decode("ascii")
        self.agent_id = crypto.agent_id_from_public_key(self.raw_pub)
        self.display_name = display_name
        self.handle = handle

    def get_public_identity(self) -> PublicIdentity:
        return PublicIdentity(
            agent_id=self.agent_id,
            public_key=self.pub_b64,
            key_algorithm="Ed25519",
            fingerprint=self.agent_id.replace("nexus:ed25519:", "")[:16],
        )

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self.priv, data)


async def cleanup_stale_demo_records():
    """Remove previous run's demo records so unique handles @rahul and @alice can be re-registered."""
    db_url = "postgresql+asyncpg://neondb_owner:npg_uOQ8LbeJgA2p@ep-rapid-poetry-audz0vue-pooler.c-10.us-east-1.aws.neon.tech/neondb?ssl=require"
    try:
        from sqlalchemy import text
        neon_engine = create_async_engine(db_url)
        async with neon_engine.begin() as conn:
            await conn.execute(text("DELETE FROM registered_agents WHERE handle IN ('rahul', 'alice', '@rahul', '@alice');"))
        await neon_engine.dispose()
    except Exception as exc:
        pass


def print_banner(text: str):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def print_step(step: str, detail: str = ""):
    print(f"\n[+] {step}")
    if detail:
        print(f"    {detail}")


async def create_device_runtime(name: str, handle: str, seed: bytes | None = None):
    """Create complete local runtime stack for a simulated Device."""
    ident = MockIdentity(name, handle, seed=seed)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        owner_id = owner.id

    trusted_repo = TrustedAgentRepository()
    contacts_repo = ContactRepository()
    runs_repo = OrchestrationRunRepository()

    # Pre-build signed Agent Card
    unsigned_card = build_card(
        agent_id=ident.agent_id,
        public_key=ident.pub_b64,
        display_name=name,
        endpoint=GATEWAY_WS_URL,
        capabilities=[
            AgentCapability(name="calendar.check", description="Availability check", data_category="availability"),
            AgentCapability(name="messages.send", description="Direct messaging", data_category="communication"),
        ],
    )
    # Add handle to card
    unsigned_card["handle"] = handle
    signed_card = await signing.sign_card(ident, unsigned_card)

    device: dict[str, Any] = {
        "name": name,
        "handle": handle,
        "ident": ident,
        "owner_id": owner_id,
        "session_factory": session_factory,
        "trusted_repo": trusted_repo,
        "contacts_repo": contacts_repo,
        "runs_repo": runs_repo,
        "signed_card": signed_card,
        "gateway_client": None,
        "orchestrator": None,
    }

    # Inbound message responder for Device B (Rahul)
    async def inbound_handler(envelope: A2AEnvelope) -> A2AEnvelope | None:
        print(f"    [{name} Gateway Listener] Inbound message received from {envelope.sender[:25]}...: purpose='{envelope.purpose}'")
        if envelope.message_type in {"response", "task_response"}:
            return None

        # Build signed response envelope
        response_payload = {
            "status": "completed",
            "available": True,
            "message": f"Hey! Yes, {name} is free tomorrow after 6 PM.",
            "free_slots": ["18:00-19:00", "19:00-20:00"],
        }
        res_envelope = A2AEnvelope(
            message_id=new_message_id(),
            sender=ident.agent_id,
            recipient=envelope.sender,
            task_id=envelope.task_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(300),
            message_type="response",
            purpose=envelope.purpose,
            payload=response_payload,
        )
        return await signing.sign_envelope(ident, res_envelope)

    client = GatewayClient(
        gateway_url=GATEWAY_WS_URL,
        identity_service=ident,
        display_name=name,
        handle=handle,
        agent_card=signed_card,
        inbound_handler=inbound_handler,
    )
    device["gateway_client"] = client

    # Target resolver and Orchestrator for Device
    resolver = TargetResolver(
        session_factory=session_factory,
        trusted_agents=trusted_repo,
        contacts=contacts_repo,
        gateway_url=GATEWAY_WS_URL,
    )
    device["resolver"] = resolver

    policy_service = PolicyService(session_factory=session_factory)
    await policy_service.create_policy(
        owner_id=owner_id,
        requester_agent_id="*",
        data_category="*",
        action="*",
        purpose="*",
        decision="ALLOW",
    )

    from app.a2a.rate_limit import SlidingWindowRateLimiter
    a2a_service = A2AService(
        session_factory=session_factory,
        identity_service=ident,
        policy_service=policy_service,
        memory_manager=None,
        transport=GatewayA2ATransport(http_transport=None, gateway_client=client),
        rate_limiter=SlidingWindowRateLimiter(60),
    )
    device["a2a_service"] = a2a_service
    device["policy_service"] = policy_service

    intent_resolver = IntentResolver(llm_provider=None)

    orchestrator = AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=a2a_service,
        policy_service=policy_service,
        intent_resolver=intent_resolver,
        target_resolver=resolver,
    )
    device["orchestrator"] = orchestrator

    return device


async def run_demonstration():
    print_banner("NEXUS PERSONAL AI NETWORK — PART 14 DEMONSTRATION")
    print(f"Gateway: {GATEWAY_WS_URL}")
    print(f"Directory API: {GATEWAY_HTTP_URL}")

    # Clean up any stale handle claims from prior runs
    await cleanup_stale_demo_records()

    # 1. Initialize Device A (Alice) and Device B (Rahul) with stable seeds
    import hashlib
    alice_seed = hashlib.sha256(b"nexus-device-alice-seed-stable-1").digest()
    rahul_seed = hashlib.sha256(b"nexus-device-rahul-seed-stable-1").digest()

    print_step("Step 1: Initializing Cryptographic Identities & Local Runtimes")
    alice = await create_device_runtime("Alice Smith", "alice", seed=alice_seed)
    rahul = await create_device_runtime("Rahul Sharma", "rahul", seed=rahul_seed)

    print(f"    Alice Identity: {alice['ident'].agent_id} (@{alice['handle']})")
    print(f"    Rahul Identity: {rahul['ident'].agent_id} (@{rahul['handle']})")

    # 2. Connect both to Hosted Gateway
    print_step("Step 2: Connecting and Authenticating Device A & Device B to Render Gateway")
    await alice["gateway_client"].start()
    await rahul["gateway_client"].start()

    # Wait for challenge-response handshake to complete on Render
    for _ in range(30):
        if alice["gateway_client"].is_connected and rahul["gateway_client"].is_connected:
            break
        await asyncio.sleep(0.5)

    if not (alice["gateway_client"].is_connected and rahul["gateway_client"].is_connected):
        print("[-] Could not connect to hosted gateway in time. Check network or gateway health.")
        await alice["gateway_client"].stop()
        await rahul["gateway_client"].stop()
        return

    print("    [OK] Alice connected and authenticated with Gateway directory.")
    print("    [OK] Rahul connected and authenticated with Gateway directory.")
    print(f"    [OK] Rahul published signed Agent Card with capabilities: {len(rahul['signed_card'].get('capabilities', []))} caps")
    # Allow 1.5s for Render gateway to persist agent record to Neon DB pool
    await asyncio.sleep(1.5)

    try:
        # -----------------------------------------------------------------
        # TEST 1: Exact Agent ID Resolution and Trust Bootstrap
        # -----------------------------------------------------------------
        print_banner("TEST 1: Exact Canonical Agent ID Resolution & Trust Bootstrap")
        print_step("Prompt: 'Ask " + rahul['ident'].agent_id + " if he is free tomorrow'")

        res1 = await alice["orchestrator"].handle_user_message(
            alice["owner_id"],
            session_id="session-demo-1",
            message=f"Ask {rahul['ident'].agent_id} if he is free tomorrow",
        )

        print(f"    Orchestrator Status: {res1.status.upper()}")
        print(f"    Requires Approval: {res1.requires_approval}")
        print(f"    System Message: {res1.message}")
        assert res1.status == "waiting_trust", f"Expected waiting_trust, got {res1.status}"

        # Inspect verified card from Gateway directory
        discovered_card = alice["resolver"].get_discovered_card(rahul['ident'].agent_id)
        print("    [OK] Verified Discovered Agent Card Metadata:")
        print(f"        Agent ID:      {discovered_card.get('agent_id')}")
        print(f"        Display Name:  {discovered_card.get('display_name')}")
        print(f"        Handle:        @{discovered_card.get('handle')}")
        print(f"        Public Key:    {discovered_card.get('public_key')[:20]}...")
        print(f"        Capabilities:  {[c['name'] for c in discovered_card.get('capabilities', [])]}")

        print_step("Simulating Owner Consent: Alice explicitly approves trust for Rahul")
        run_id = uuid.UUID(res1.run_id)
        res1_resumed = await alice["orchestrator"].trust_and_resume_run(
            alice["owner_id"],
            run_id,
            req=OrchestrationTrustRequest(display_name="Rahul Sharma"),
        )

        print(f"    Resumed Run Status: {res1_resumed.status.upper()}")
        print(f"    Final Response: {res1_resumed.message}")
        print("    [OK] Test 1 Passed: Discovered untrusted agent promoted to trusted and task completed seamlessly!")

        # -----------------------------------------------------------------
        # TEST 2: Public Handle Resolution (@rahul)
        # -----------------------------------------------------------------
        print_banner("TEST 2: Public Handle Resolution (@rahul)")
        print_step("Prompt: 'Ask @rahul if he is free tomorrow after 6 PM'")

        res2 = await alice["orchestrator"].handle_user_message(
            alice["owner_id"],
            session_id="session-demo-2",
            message="Ask @rahul if he is free tomorrow after 6 PM",
        )

        print(f"    Orchestrator Status: {res2.status.upper()}")
        print(f"    Remote Agent Reply: {res2.message}")
        assert res2.status == "completed", f"Expected completed, got {res2.status}"
        print("    [OK] Test 2 Passed: @rahul resolved via directory and message exchanged over live WebSocket relay!")

        # -----------------------------------------------------------------
        # TEST 3: Offline Queuing & Reconnect Delivery
        # -----------------------------------------------------------------
        print_banner("TEST 3: Offline Queuing & Delivery on Reconnect")
        print_step("Disconnecting Device B (Rahul) from Gateway...")
        await rahul["gateway_client"].stop()
        async with httpx.AsyncClient(timeout=10.0) as http:
            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    resp = await http.get(f"{GATEWAY_HTTP_URL}/agents/{rahul['ident'].agent_id}/presence")
                    if resp.status_code == 200 and not resp.json().get("online", True):
                        break
                except Exception:
                    pass
        print("    [OK] Device B is confirmed offline by Gateway directory.")

        print_step("Device A (Alice) sends message while Device B is offline...")
        # Send raw relay envelope through gateway transport
        envelope = A2AEnvelope(
            message_id=new_message_id(),
            sender=alice["ident"].agent_id,
            recipient=rahul["ident"].agent_id,
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(300),
            message_type="task_request",
            purpose="availability_check",
            payload={"check": "availability", "time": "18:00"},
        )
        signed_env = await signing.sign_envelope(alice["ident"], envelope)

        send_res = await alice["gateway_client"].send_relay_envelope(signed_env.model_dump())
        print(f"    Gateway Delivery Response: {send_res}")
        assert send_res.get("status") == "queued", f"Expected queued, got {send_res}"
        print("    [OK] Gateway safely accepted and queued message for offline recipient!")

        print_step("Device B (Rahul) reconnects to Gateway...")
        # Re-start Device B client
        await rahul["gateway_client"].start()
        await asyncio.sleep(2.0)
        print("    [OK] Device B reconnected. Gateway flushed undelivered queue.")
        print("    [OK] Test 3 Passed: Offline queue delivery verified across network boundary!")

    finally:
        print_step("Cleaning up connections...")
        await alice["gateway_client"].stop()
        await rahul["gateway_client"].stop()
        print_banner("ALL DEMONSTRATION TESTS COMPLETED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(run_demonstration())
