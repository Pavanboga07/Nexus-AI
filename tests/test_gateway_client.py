"""Tests for Nexus-side GatewayClient and GatewayA2ATransport."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.a2a.errors import A2AError
from app.a2a.gateway_client import GatewayA2ATransport, GatewayClient
from app.a2a.transport import A2ATransport
from app.identity.service import IdentityService, PublicIdentity


@pytest.fixture
def mock_identity() -> MagicMock:
    ident = MagicMock(spec=IdentityService)
    ident.get_public_identity.return_value = PublicIdentity(
        agent_id="nexus:ed25519:0123456789abcdef0123456789abcdef",
        public_key="bW9ja19wdWJsaWNfa2V5",
        key_algorithm="Ed25519",
        fingerprint="0123-4567-89AB-CDEF",
    )
    ident.sign = AsyncMock(return_value=b"0" * 64)
    return ident


@pytest.mark.asyncio
async def test_gateway_transport_prefers_gateway(mock_identity: MagicMock) -> None:
    """A connected gateway wins, even when the endpoint is a plain http URL.

    Gateway-first (decision D2): one transport to reason about, with NAT
    traversal, offline buffering and delivery acks.
    """
    mock_http = AsyncMock(spec=A2ATransport)
    mock_http.send.return_value = {"status": "http_ok"}

    mock_client = AsyncMock(spec=GatewayClient)
    mock_client.send_relay_envelope.return_value = {"status": "gateway_ok"}

    transport = GatewayA2ATransport(
        http_transport=mock_http,
        gateway_client=mock_client,
    )
    envelope = _envelope()

    mock_client.is_connected = True
    res_gw = await transport.send("http://remote.agent.com/a2a", envelope)
    assert res_gw == {"status": "gateway_ok"}
    mock_client.send_relay_envelope.assert_awaited_once_with(envelope)
    # The HTTP path must not have been used at all.
    mock_http.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_egress_is_refused_unless_explicitly_enabled(
    mock_identity: MagicMock,
) -> None:
    """Gateway down + direct egress off => an explanatory error, not a silent POST.

    Silently falling back to direct HTTP would mean a peer's NAT reachability
    decides your delivery semantics: no offline buffering, no delivery ack, and
    different failure modes. An operator must opt in.
    """
    mock_http = AsyncMock(spec=A2ATransport)
    mock_http.send.return_value = {"status": "http_ok"}
    mock_client = AsyncMock(spec=GatewayClient)
    mock_client.is_connected = False
    envelope = _envelope()

    # Default: refuse, and say why.
    strict = GatewayA2ATransport(
        http_transport=mock_http, gateway_client=mock_client
    )
    with pytest.raises(A2AError) as exc:
        await strict.send("http://remote.agent.com/a2a", envelope)
    assert "direct egress is disabled" in exc.value.message
    mock_http.send.assert_not_awaited()

    # Opted in: the direct path is used.
    permissive = GatewayA2ATransport(
        http_transport=mock_http,
        gateway_client=mock_client,
        allow_direct_egress=True,
    )
    res_http = await permissive.send("http://remote.agent.com/a2a", envelope)
    assert res_http == {"status": "http_ok"}
    mock_http.send.assert_awaited_once_with("http://remote.agent.com/a2a", envelope)


@pytest.mark.asyncio
async def test_gateway_target_never_silently_becomes_http(
    mock_identity: MagicMock,
) -> None:
    """A ws:// endpoint with no connection is an error, not a downgrade."""
    mock_http = AsyncMock(spec=A2ATransport)
    mock_client = AsyncMock(spec=GatewayClient)
    mock_client.is_connected = False

    transport = GatewayA2ATransport(
        http_transport=mock_http,
        gateway_client=mock_client,
        allow_direct_egress=True,  # even with egress allowed
    )
    with pytest.raises(A2AError):
        await transport.send("wss://gateway.example/ws", _envelope())
    mock_http.send.assert_not_awaited()


def _envelope() -> dict:
    return {
        "protocol": "nexus-a2a",
        "version": "0.2",
        "message_id": "msg_1",
        "task_id": "task_1",
        "sender": "nexus:ed25519:0123456789abcdef0123456789abcdef",
        "recipient": "nexus:ed25519:fedcba9876543210fedcba9876543210",
        "timestamp": "2026-09-15T10:00:00Z",
        "expires_at": "2026-09-15T10:01:00Z",
        "message_type": "request",
        "purpose": "test",
        "payload": {},
    }
