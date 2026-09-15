"""Identity API tests: GET /identity and POST /identity/verify.

Requires PostgreSQL; skips when unavailable. Asserts that private key
material NEVER appears in responses.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import pytest_asyncio

from app.identity import crypto
from app.identity.service import IdentityService
from app.main import create_app

pytestmark = pytest.mark.asyncio

TEST_SECRET = "api-test-identity-secret-not-real"


@pytest_asyncio.fixture
async def identity_app(db_session_factory, db_owner_id):
    """App with a real, initialised IdentityService but the fake chat agent."""
    service = IdentityService(
        session_factory=db_session_factory,
        encryption_secret=TEST_SECRET,
        owner_id=db_owner_id,
    )
    await service.initialize_identity()

    from app.agent.agent import NexusAgent
    from app.agent.context import ContextBuilder
    from app.agent.session import InMemorySessionStore
    from tests.conftest import FakeProvider

    application = create_app()
    application.state.agent = NexusAgent(
        provider=FakeProvider(),
        sessions=InMemorySessionStore(),
        context_builder=ContextBuilder(system_prompt="You are Nexus."),
    )
    application.state.engine = None
    application.state.database_ok = True
    application.state.identity_service = service
    application.state.identity_ok = True
    return application


@pytest_asyncio.fixture
async def identity_client(identity_app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=identity_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_identity_returns_public_material_only(identity_client) -> None:
    response = await identity_client.get("/identity")

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"].startswith("nexus:ed25519:")
    assert body["key_algorithm"] == "Ed25519"
    assert len(base64.b64decode(body["public_key"])) == 32
    assert len(body["fingerprint"].split("-")) == 8

    # Private material must never appear.
    raw = response.text
    assert "private" not in raw.lower()
    assert "encrypted" not in raw.lower()
    assert "secret" not in raw.lower()


async def test_verify_endpoint_happy_path(identity_client, identity_app) -> None:
    service: IdentityService = identity_app.state.identity_service
    public = service.get_public_identity()

    message = "Hello from Nexus"
    signature = await service.sign(message.encode("utf-8"))

    response = await identity_client.post(
        "/identity/verify",
        json={
            "agent_id": public.agent_id,
            "public_key": public.public_key,
            "message": message,
            "signature": base64.b64encode(signature).decode("ascii"),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["agent_id_matches"] is True


async def test_verify_endpoint_tampered_message(identity_client, identity_app) -> None:
    service: IdentityService = identity_app.state.identity_service
    public = service.get_public_identity()

    signature = await service.sign(b"Hello from Nexus")
    response = await identity_client.post(
        "/identity/verify",
        json={
            "agent_id": public.agent_id,
            "public_key": public.public_key,
            "message": "Hello from Nexus!",
            "signature": base64.b64encode(signature).decode("ascii"),
        },
    )

    body = response.json()
    assert body["valid"] is False
    assert "signature" in body["reason"]


async def test_verify_endpoint_rejects_mismatched_agent_id(
    identity_client, identity_app
) -> None:
    """A valid signature under a real key but a LIED-about agent_id fails."""
    service: IdentityService = identity_app.state.identity_service
    public = service.get_public_identity()

    message = "Hello from Nexus"
    signature = await service.sign(message.encode("utf-8"))

    response = await identity_client.post(
        "/identity/verify",
        json={
            "agent_id": "nexus:ed25519:00000000000000000000000000000000",
            "public_key": public.public_key,
            "message": message,
            "signature": base64.b64encode(signature).decode("ascii"),
        },
    )

    body = response.json()
    assert body["valid"] is False
    assert body["agent_id_matches"] is False


async def test_verify_endpoint_wrong_key(identity_client, identity_app) -> None:
    service: IdentityService = identity_app.state.identity_service
    message = "Hello from Nexus"
    signature = await service.sign(message.encode("utf-8"))

    other_public = crypto.public_key_bytes(crypto.generate_keypair()[1])
    response = await identity_client.post(
        "/identity/verify",
        json={
            "agent_id": service.get_public_identity().agent_id,
            "public_key": base64.b64encode(other_public).decode("ascii"),
            "message": message,
            "signature": base64.b64encode(signature).decode("ascii"),
        },
    )

    assert response.json()["valid"] is False


async def test_verify_endpoint_invalid_base64(identity_client) -> None:
    response = await identity_client.post(
        "/identity/verify",
        json={
            "agent_id": "nexus:ed25519:abcd",
            "public_key": "!!!not-base64!!!",
            "message": "hi",
            "signature": "also-not-base64!!!",
        },
    )
    assert response.status_code == 422


async def test_verify_endpoint_invalid_public_key_bytes(identity_client) -> None:
    response = await identity_client.post(
        "/identity/verify",
        json={
            "agent_id": "nexus:ed25519:abcd",
            "public_key": base64.b64encode(b"too-short").decode(),
            "message": "hi",
            "signature": base64.b64encode(b"x" * 64).decode(),
        },
    )
    assert response.status_code == 422


async def test_health_reports_identity(identity_client) -> None:
    response = await identity_client.get("/health")
    body = response.json()
    assert body["status"] == "ok"
    assert body["identity"] is True
