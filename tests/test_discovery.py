"""Part 7 tests: Agent Discovery & Agent Cards.

Unit tests (no database):
    - Card building, canonical serialization, signing, verification
    - Card schema validation (missing fields, bad types, floats)
    - Card time window validation (expired, future, malformed)
    - Capability extraction from tool metadata

Integration tests (database, fixtures):
    - GET /.well-known/nexus-agent.json and GET /a2a/card endpoints
    - POST /a2a/discover with valid/invalid/expired/tampered cards
    - Owner isolation
    - SSRF protection
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from app.a2a import signing
from app.a2a.cards import (
    AgentCapability,
    CardValidationError,
    build_card,
    capabilities_from_tools,
    validate_card_schema,
    validate_card_time_window,
)
from app.a2a.errors import A2AError, A2AErrorCode
from app.identity import crypto


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_keypair():
    private_key, public_key = crypto.generate_keypair()
    raw = crypto.public_key_bytes(public_key)
    pub_b64 = base64.b64encode(raw).decode()
    agent_id = crypto.agent_id_from_public_key(raw)
    return private_key, pub_b64, agent_id


class StubCardIdentity:
    """Minimal identity stub for card signing in unit tests."""

    def __init__(self):
        self._private, self.public_key_b64, self.agent_id = _make_keypair()

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private, data)


def _build_unsigned_card(identity: StubCardIdentity, **overrides) -> dict[str, Any]:
    card = build_card(
        agent_id=identity.agent_id,
        public_key=identity.public_key_b64,
        display_name="Test Agent",
        endpoint="https://agent.example.com",
        capabilities=[
            AgentCapability(
                name="echo", description="Echo back", data_category="custom"
            ),
        ],
        supported_purposes=["scheduling", "information"],
        ttl_seconds=3600,
    )
    card.update(overrides)
    return card


async def _build_signed_card(identity: StubCardIdentity, **overrides) -> dict[str, Any]:
    card = _build_unsigned_card(identity, **overrides)
    return await signing.sign_card(identity, card)


def _mock_card_response(svc, card: dict[str, Any]):
    """Set up an in-memory return for svc._http_get_card."""
    original = svc._http_get_card

    async def _mock_fetch(url: str) -> dict[str, Any]:
        return card

    svc._http_get_card = _mock_fetch
    return original


# ========================================================================== #
#  UNIT TESTS — Card building, validation, signing (no database)              #
# ========================================================================== #


class TestCardBuilder:
    """Card construction and capability extraction."""

    def test_build_card_has_required_fields(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        assert card["type"] == "agent-card"
        assert card["protocol"] == "nexus-a2a"
        assert card["version"] == "0.1"
        assert card["agent_id"] == identity.agent_id
        assert card["public_key"] == identity.public_key_b64
        assert card["display_name"] == "Test Agent"
        assert card["endpoint"] == "https://agent.example.com"
        assert len(card["capabilities"]) == 1
        assert card["capabilities"][0]["name"] == "echo"
        assert "scheduling" in card["supported_purposes"]
        assert card["issued_at"] is not None
        assert card["expires_at"] is not None

    def test_build_card_timestamps_are_utc_iso(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        assert pattern.fullmatch(card["issued_at"])
        assert pattern.fullmatch(card["expires_at"])

    def test_capabilities_from_tools(self) -> None:
        tools = [
            {"name": "echo", "description": "Echo back", "data_category": "custom",
             "inputSchema": {"type": "object"}},
            {"name": "get_current_time", "description": "Current time"},
        ]
        caps = capabilities_from_tools(tools)
        assert len(caps) == 2
        assert caps[0].name == "echo"
        assert caps[0].data_category == "custom"
        assert caps[1].data_category == "custom"

    def test_capabilities_never_include_input_schema(self) -> None:
        tools = [{"name": "echo", "description": "Echo", "inputSchema": {"x": 1}}]
        caps = capabilities_from_tools(tools)
        d = caps[0].to_dict()
        assert "inputSchema" not in d
        assert "input_schema" not in d


class TestCardSchemaValidation:
    """Consumer-side card schema validation."""

    def test_valid_card_passes(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        validate_card_schema(card)  # no raise

    def test_missing_field_rejected(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        del card["agent_id"]
        with pytest.raises(CardValidationError, match="missing required fields"):
            validate_card_schema(card)

    def test_wrong_type_rejected(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        card["type"] = "not-a-card"
        with pytest.raises(CardValidationError, match="type must be"):
            validate_card_schema(card)

    def test_wrong_protocol_rejected(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        card["protocol"] = "some-other-protocol"
        with pytest.raises(CardValidationError, match="protocol must be"):
            validate_card_schema(card)

    def test_bad_agent_id_rejected(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        card["agent_id"] = "not-an-agent-id"
        with pytest.raises(CardValidationError, match="agent_id"):
            validate_card_schema(card)

    def test_bad_timestamp_rejected(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        card["issued_at"] = "2026-09-14 10:00:00"
        with pytest.raises(CardValidationError, match="issued_at"):
            validate_card_schema(card)

    def test_floats_in_card_rejected(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        card["capabilities"] = [{"name": "x", "description": "y",
                                  "data_category": "z", "score": 0.5}]
        with pytest.raises(CardValidationError, match="[Ff]loat"):
            validate_card_schema(card)

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(CardValidationError, match="JSON object"):
            validate_card_schema("not a dict")  # type: ignore[arg-type]

    def test_capabilities_must_be_list(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        card["capabilities"] = "not a list"
        with pytest.raises(CardValidationError, match="capabilities must be a list"):
            validate_card_schema(card)


class TestCardTimeWindow:
    """Card time window validation."""

    def test_valid_window_passes(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        validate_card_time_window(card)  # no raise

    def test_expired_card_rejected(self) -> None:
        identity = StubCardIdentity()
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        card = _build_unsigned_card(
            identity,
            issued_at=(past - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=past.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        with pytest.raises(CardValidationError, match="expired"):
            validate_card_time_window(card)

    def test_future_issued_at_beyond_skew_rejected(self) -> None:
        identity = StubCardIdentity()
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        card = _build_unsigned_card(
            identity,
            issued_at=future.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(future + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        with pytest.raises(CardValidationError, match="future"):
            validate_card_time_window(card, max_clock_skew_seconds=30)

    def test_future_issued_at_within_skew_passes(self) -> None:
        identity = StubCardIdentity()
        near_future = datetime.now(timezone.utc) + timedelta(seconds=5)
        card = _build_unsigned_card(
            identity,
            issued_at=near_future.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(near_future + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        validate_card_time_window(card, max_clock_skew_seconds=30)  # no raise

    def test_expires_before_issued_rejected(self) -> None:
        identity = StubCardIdentity()
        now = datetime.now(timezone.utc)
        card = _build_unsigned_card(
            identity,
            issued_at=(now + timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(now + timedelta(seconds=50)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        with pytest.raises(CardValidationError, match="after issued_at"):
            validate_card_time_window(card, max_clock_skew_seconds=200)


class TestCardSigning:
    """Card signing and verification."""

    async def test_sign_and_verify_roundtrip(self) -> None:
        identity = StubCardIdentity()
        signed = await _build_signed_card(identity)
        assert signed["signature"] is not None
        assert signing.verify_card_signature(signed, identity.public_key_b64)

    async def test_tampered_card_fails_verification(self) -> None:
        identity = StubCardIdentity()
        signed = await _build_signed_card(identity)
        signed["display_name"] = "Tampered Name"
        assert not signing.verify_card_signature(signed, identity.public_key_b64)

    async def test_wrong_key_fails_verification(self) -> None:
        identity = StubCardIdentity()
        other = StubCardIdentity()
        signed = await _build_signed_card(identity)
        assert not signing.verify_card_signature(signed, other.public_key_b64)

    async def test_missing_signature_fails(self) -> None:
        identity = StubCardIdentity()
        card = _build_unsigned_card(identity)
        assert not signing.verify_card_signature(card, identity.public_key_b64)

    async def test_malformed_signature_fails(self) -> None:
        identity = StubCardIdentity()
        signed = await _build_signed_card(identity)
        signed["signature"] = "not-valid-base64!!!"
        assert not signing.verify_card_signature(signed, identity.public_key_b64)

    async def test_canonical_bytes_are_deterministic(self) -> None:
        identity = StubCardIdentity()
        card1 = _build_unsigned_card(identity)
        card2 = dict(card1)
        from app.identity.serialization import canonical_json_bytes
        assert canonical_json_bytes(card1) == canonical_json_bytes(card2)

    async def test_agent_id_key_mismatch_detectable(self) -> None:
        id_a = StubCardIdentity()
        id_b = StubCardIdentity()
        assert not signing.agent_id_matches_key(id_a.agent_id, id_b.public_key_b64)


# ========================================================================== #
#  INTEGRATION TESTS — API endpoints (database required)                      #
# ========================================================================== #


@pytest_asyncio.fixture
async def discovery_app(db_a2a_app):
    """db_a2a_app already has identity + a2a + policy + tools.
    We add the discovery service on top."""
    from app.a2a.discovery import DiscoveryService

    discovery_service = DiscoveryService(
        identity_service=db_a2a_app.state.identity_service,
        a2a_service=db_a2a_app.state.a2a_service,
        allow_local_endpoints=True,
        timeout_seconds=5.0,
        max_card_bytes=65_536,
    )
    db_a2a_app.state.discovery_service = discovery_service
    return db_a2a_app


@pytest_asyncio.fixture
async def discovery_client(discovery_app):
    import httpx
    transport = httpx.ASGITransport(app=discovery_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


class TestCardEndpoints:
    """GET /.well-known/nexus-agent.json and GET /a2a/card."""

    async def test_well_known_returns_signed_card(self, discovery_client, discovery_app) -> None:
        response = await discovery_client.get("/.well-known/nexus-agent.json")
        assert response.status_code == 200
        card = response.json()
        assert card["type"] == "agent-card"
        assert card["protocol"] == "nexus-a2a"
        assert card["version"] == "0.1"
        assert card["signature"] is not None

        # Verify agent_id matches the local identity.
        public = discovery_app.state.identity_service.get_public_identity()
        assert card["agent_id"] == public.agent_id
        assert card["public_key"] == public.public_key

        # Verify signature.
        assert signing.verify_card_signature(card, public.public_key)

    async def test_a2a_card_returns_same_card(self, discovery_client) -> None:
        well_known = await discovery_client.get("/.well-known/nexus-agent.json")
        a2a_card = await discovery_client.get("/a2a/card")
        assert well_known.status_code == 200
        assert a2a_card.status_code == 200
        wk = well_known.json()
        ac = a2a_card.json()
        assert wk["agent_id"] == ac["agent_id"]
        assert wk["public_key"] == ac["public_key"]
        assert wk["protocol"] == ac["protocol"]

    async def test_card_includes_tool_capabilities(self, discovery_client) -> None:
        response = await discovery_client.get("/a2a/card")
        card = response.json()
        cap_names = [c["name"] for c in card["capabilities"]]
        assert "echo" in cap_names
        assert "get_current_time" in cap_names

    async def test_card_capabilities_never_expose_input_schema(self, discovery_client) -> None:
        response = await discovery_client.get("/a2a/card")
        card = response.json()
        for cap in card["capabilities"]:
            assert "inputSchema" not in cap
            assert "input_schema" not in cap


class TestDiscoverEndpoint:
    """POST /a2a/discover — fetch, verify, and register a remote agent."""

    async def test_discover_valid_card_registers_agent(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            response = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["agent_id"] == remote.agent_id
            assert body["status"] == "active"
            assert body["card"]["agent_id"] == remote.agent_id
            assert body["card"]["signature"] is not None

            # Verify the agent is now in the trusted list.
            agents = await discovery_client.get("/a2a/agents")
            agent_ids = [a["agent_id"] for a in agents.json()["agents"]]
            assert remote.agent_id in agent_ids
        finally:
            svc._http_get_card = orig

    async def test_discover_tampered_card_rejected(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)
        remote_card["display_name"] = "Tampered"

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            response = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert response.status_code == 401
        finally:
            svc._http_get_card = orig

    async def test_discover_expired_card_rejected(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        remote_card = await _build_signed_card(
            remote,
            issued_at=(past - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=past.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            response = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert response.status_code == 410
        finally:
            svc._http_get_card = orig

    async def test_discover_identity_mismatch_rejected(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        other = StubCardIdentity()
        card = _build_unsigned_card(remote, public_key=other.public_key_b64)
        signed_card = await signing.sign_card(remote, card)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, signed_card)

        try:
            response = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert response.status_code == 400
        finally:
            svc._http_get_card = orig

    async def test_discover_already_registered_conflict(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            first = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert first.status_code == 200

            second = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert second.status_code == 409
        finally:
            svc._http_get_card = orig

    async def test_discover_ssrf_endpoint_rejected(self, discovery_client) -> None:
        response = await discovery_client.post(
            "/a2a/discover",
            json={"url": "file:///etc/passwd"},
        )
        assert response.status_code == 422

    async def test_discover_with_display_name_override(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            response = await discovery_client.post(
                "/a2a/discover",
                json={
                    "url": "https://remote.example.com/.well-known/nexus-agent.json",
                    "display_name": "My Custom Name",
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["display_name"] == "My Custom Name"
        finally:
            svc._http_get_card = orig

    async def test_discover_missing_card_schema_field_rejected(
        self, discovery_client, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)
        del remote_card["capabilities"]

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            response = await discovery_client.post(
                "/a2a/discover",
                json={"url": "https://remote.example.com/.well-known/nexus-agent.json"},
            )
            assert response.status_code == 400
        finally:
            svc._http_get_card = orig


class TestDiscoveryService:
    """Direct DiscoveryService tests (no HTTP routes)."""

    async def test_fetch_card_validates_full_pipeline(
        self, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            card = await svc.fetch_card("https://remote.example.com/card")
            assert card["agent_id"] == remote.agent_id
            assert card["signature"] is not None
        finally:
            svc._http_get_card = orig

    async def test_fetch_card_rejects_invalid_signature(
        self, discovery_app
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)
        remote_card["endpoint"] = "https://attacker.com"

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            with pytest.raises(A2AError) as exc:
                await svc.fetch_card("https://remote.example.com/card")
            assert exc.value.code is A2AErrorCode.CARD_SIGNATURE_INVALID
        finally:
            svc._http_get_card = orig

    async def test_discover_and_register_creates_trusted_agent(
        self, discovery_app, db_owner_id
    ) -> None:
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            agent, card = await svc.discover_and_register(
                db_owner_id, "https://remote.example.com/card"
            )
            assert agent.agent_id == remote.agent_id
            assert agent.status == "active"
            assert card["signature"] is not None
        finally:
            svc._http_get_card = orig

    async def test_discover_does_not_bypass_policy(
        self, discovery_app, db_owner_id
    ) -> None:
        """Discovery registers the agent but does NOT grant any policy
        permissions. A subsequent A2A request should still get ASK/DENY."""
        remote = StubCardIdentity()
        remote_card = await _build_signed_card(remote)

        svc = discovery_app.state.discovery_service
        orig = _mock_card_response(svc, remote_card)

        try:
            await svc.discover_and_register(
                db_owner_id, "https://remote.example.com/card"
            )
        finally:
            svc._http_get_card = orig

        a2a_svc = discovery_app.state.a2a_service
        trusted = await a2a_svc.get_trusted_agent(db_owner_id, remote.agent_id)
        assert trusted is not None
        assert trusted.status == "active"

        from app.a2a.schemas import (
            A2AEnvelope,
            new_message_id,
            new_task_id,
            utc_iso_in,
            utc_now_iso,
        )

        local_id = discovery_app.state.identity_service.get_public_identity().agent_id
        envelope = A2AEnvelope(
            message_id=new_message_id(),
            task_id=new_task_id(),
            sender=remote.agent_id,
            recipient=local_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(60),
            message_type="request",
            purpose="scheduling",
            payload={"action": "disclose_information", "data_category": "availability"},
        )
        signed_env = await signing.sign_envelope(remote, envelope)
        response = await a2a_svc.handle_inbound(db_owner_id, signed_env)
        assert response.payload["status"] == "approval_required"


class TestDiscoveryServiceUnavailable:
    """Discovery service 503 when not configured."""

    async def test_discover_503_without_service(self, db_client) -> None:
        response = await db_client.post(
            "/a2a/discover",
            json={"url": "https://example.com/.well-known/nexus-agent.json"},
        )
        assert response.status_code == 503

    async def test_well_known_503_when_identity_not_ready(
        self, discovery_client, discovery_app
    ) -> None:
        identity_svc = discovery_app.state.identity_service
        saved_key = identity_svc._private_key
        try:
            identity_svc._private_key = None
            response = await discovery_client.get("/.well-known/nexus-agent.json")
            assert response.status_code == 503
            assert response.json()["detail"] == "Agent identity is not initialised."
        finally:
            identity_svc._private_key = saved_key
