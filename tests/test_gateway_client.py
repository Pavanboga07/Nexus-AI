"""Tests for Nexus-side GatewayClient and GatewayA2ATransport."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

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
async def test_gateway_transport_routing(mock_identity: MagicMock) -> None:
    """GatewayA2ATransport routes ws:// to gateway client and http:// to HTTP transport."""
    mock_http = AsyncMock(spec=A2ATransport)
    mock_http.send.return_value = {"status": "http_ok"}

    mock_client = AsyncMock(spec=GatewayClient)
    mock_client.send_relay_envelope.return_value = {"status": "gateway_ok"}

    transport = GatewayA2ATransport(
        http_transport=mock_http,
        gateway_client=mock_client,
    )

    envelope = {
        "protocol": "nexus-a2a",
        "version": "0.1",
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

    # 1. HTTP endpoint goes to HTTP transport
    res_http = await transport.send("http://remote.agent.com/a2a", envelope)
    assert res_http == {"status": "http_ok"}
    mock_http.send.assert_awaited_once_with("http://remote.agent.com/a2a", envelope)

    # 2. WebSocket endpoint goes to gateway client
    res_gw = await transport.send("ws://gateway.example.com:9000/ws", envelope)
    assert res_gw == {"status": "gateway_ok"}
    mock_client.send_relay_envelope.assert_awaited_once_with(envelope)
