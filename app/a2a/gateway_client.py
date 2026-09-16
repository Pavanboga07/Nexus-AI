"""Gateway Client and WebSocket Transport for Nexus A2A.

Enables Nexus instances behind NAT / firewalls to communicate through a
standalone Nexus Gateway relay service.

Architecture:
    1. Nexus connects OUTBOUND to Gateway via WebSocket (WSS)
    2. Nexus authenticates via challenge-response using its local IdentityService
       (private key never leaves this machine)
    3. Gateway forwards signed A2A envelopes to/from remote agents
    4. End-to-end Ed25519 signatures remain fully authoritative between agents
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Callable, Coroutine

import websockets

try:
    from websockets.asyncio.client import ClientConnection as WSClient
except ImportError:
    from websockets.client import WebSocketClientProtocol as WSClient  # type: ignore

from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.schemas import A2AEnvelope
from app.a2a.transport import A2ATransport
from app.identity.service import IdentityService

logger = logging.getLogger("nexus.a2a.gateway")


class GatewayClient:
    """Maintains an outbound WebSocket connection to the Nexus Gateway."""

    def __init__(
        self,
        *,
        gateway_url: str,
        identity_service: IdentityService,
        owner_id: uuid.UUID | None = None,
        inbound_handler: Callable[[A2AEnvelope], Coroutine[Any, Any, A2AEnvelope | None]] | None = None,
        heartbeat_interval: float = 30.0,
        reconnect_delay: float = 5.0,
        display_name: str | None = None,
        handle: str | None = None,
        agent_card: dict[str, Any] | None = None,
    ) -> None:
        self._gateway_url = gateway_url
        self._identity = identity_service
        self._owner_id = owner_id
        self._inbound_handler = inbound_handler
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_delay = reconnect_delay
        self._display_name = display_name
        self._handle = handle
        self._agent_card = agent_card

        self._ws: WSClient | None = None
        self._connected = False
        self._running = False
        self._loop_task: asyncio.Task | None = None

        # Correlation futures: task_id / message_id -> asyncio.Future
        self._pending_responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Pending delivery acks: relay_id -> asyncio.Future
        self._pending_acks: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def start(self) -> None:
        """Start the gateway client background connection loop."""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._connection_supervisor())
        logger.info("Gateway client started (target: %s)", self._gateway_url)

    async def stop(self) -> None:
        """Stop the gateway client and disconnect."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close(code=1000, reason="Normal closure")
            except Exception:
                pass
            self._ws = None
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._connected = False
        logger.info("Gateway client stopped.")

    async def _connection_supervisor(self) -> None:
        """Continuously connect and reconnect with backoff."""
        while self._running:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Gateway connection lost: %s. Reconnecting in %ss...", exc, self._reconnect_delay)
                self._connected = False
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_run(self) -> None:
        """Establish connection, complete auth handshake, and process frames."""
        logger.info("Connecting to gateway at %s...", self._gateway_url)
        async with websockets.connect(self._gateway_url) as ws:
            self._ws = ws
            await self._authenticate(ws)
            self._connected = True
            logger.info("Authenticated and connected to gateway successfully.")

            # Run reader loop until disconnect
            await self._read_loop(ws)

    async def _authenticate(self, ws: WSClient) -> None:
        """Execute cryptographic challenge-response authentication with gateway."""
        # 1. Receive challenge
        raw = await ws.recv()
        data = json.loads(raw)
        if data.get("type") != "auth_challenge":
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                f"Expected auth_challenge, got {data.get('type')}",
            )

        challenge_b64 = data["challenge"]
        challenge_bytes = base64.b64decode(challenge_b64.encode("ascii"))

        # 2. Sign challenge with local IdentityService
        pub_ident = self._identity.get_public_identity()
        raw_sig = await self._identity.sign(challenge_bytes)
        sig_b64 = base64.b64encode(raw_sig).decode("ascii")

        # Build / sign agent card if not provided
        card = self._agent_card
        if card is None:
            try:
                from app.a2a.cards import build_card
                from app.a2a.signing import sign_card
                unsigned = build_card(
                    agent_id=pub_ident.agent_id,
                    public_key=pub_ident.public_key,
                    display_name=self._display_name or "Nexus Primary Agent",
                    endpoint=self._gateway_url,
                )
                card = await sign_card(self._identity, unsigned)
            except Exception as exc:
                logger.warning("Failed to auto-build signed agent card for gateway auth: %s", exc)
                card = None

        # 3. Send auth_response
        auth_response = {
            "type": "auth_response",
            "agent_id": pub_ident.agent_id,
            "public_key": pub_ident.public_key,
            "signature": sig_b64,
            "display_name": self._display_name or "Nexus Primary Agent",
            "handle": self._handle,
            "agent_card": card,
        }
        await ws.send(json.dumps(auth_response))

        # 4. Receive auth_result
        raw_result = await ws.recv()
        result_data = json.loads(raw_result)
        if result_data.get("type") != "auth_result" or not result_data.get("success"):
            error_msg = result_data.get("error", "Unknown auth failure")
            raise A2AError(
                A2AErrorCode.IDENTITY_MISMATCH,
                f"Gateway rejected authentication: {error_msg}",
            )

    async def _read_loop(self, ws: WSClient) -> None:
        """Read incoming frames from gateway and dispatch them."""
        async for raw in ws:
            try:
                frame = json.loads(raw)
                await self._handle_frame(ws, frame)
            except Exception as exc:
                logger.error("Error processing gateway frame: %s", exc)

    async def _handle_frame(self, ws: WSClient, frame: dict[str, Any]) -> None:
        """Dispatch a single parsed frame."""
        frame_type = frame.get("type")

        if frame_type == "delivery":
            relay_id = frame.get("relay_id")
            envelope_data = frame.get("envelope", {})

            # 1. Send delivery_ack back to gateway
            if relay_id:
                ack = {"type": "delivery_ack", "relay_id": relay_id}
                await ws.send(json.dumps(ack))

            # 2. Check if this is a response to an outbound request we sent
            msg_type = envelope_data.get("message_type")
            task_id = envelope_data.get("task_id")
            if msg_type in {"response", "task_response"} and task_id in self._pending_responses:
                fut = self._pending_responses.pop(task_id)
                if not fut.done():
                    fut.set_result(envelope_data)

            # 3. In all cases, dispatch to local inbound handler so DB and workflows are updated
            if self._inbound_handler:
                try:
                    envelope = A2AEnvelope.model_validate(envelope_data)
                    response_envelope = await self._inbound_handler(envelope)
                    if response_envelope and msg_type not in {"response", "task_response"}:
                        # Relay the signed response back to the sender
                        response_frame = {
                            "type": "relay_envelope",
                            "relay_id": f"relay_{uuid.uuid4().hex}",
                            "recipient": response_envelope.recipient,
                            "envelope": response_envelope.model_dump(),
                        }
                        await ws.send(json.dumps(response_frame))
                except Exception as exc:
                    logger.error("Inbound handler failure for delivered envelope: %s", exc)

        elif frame_type == "delivery_ack":
            relay_id = frame.get("relay_id")
            logger.debug("Gateway delivery_ack received: %s", frame)
            if relay_id and relay_id in self._pending_acks:
                fut = self._pending_acks.pop(relay_id)
                if not fut.done():
                    fut.set_result(frame)

        elif frame_type == "delivery_failed":
            relay_id = frame.get("relay_id")
            logger.warning("Gateway delivery_failed received: %s", frame)
            if relay_id and relay_id in self._pending_acks:
                fut = self._pending_acks.pop(relay_id)
                if not fut.done():
                    fut.set_exception(
                        A2AError(A2AErrorCode.TRANSPORT_ERROR, frame.get("reason", "Delivery failed"))
                    )

        elif frame_type == "heartbeat":
            # Echo pong back
            pong = {"type": "heartbeat"}
            await ws.send(json.dumps(pong))

    async def send_relay_envelope(
        self,
        envelope: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a signed envelope through the gateway and await response envelope."""
        if not self.is_connected or self._ws is None:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Gateway client is not connected.",
            )

        relay_id = f"relay_{uuid.uuid4().hex}"
        task_id = envelope.get("task_id", "")
        frame = {
            "type": "relay_envelope",
            "relay_id": relay_id,
            "recipient": envelope.get("recipient"),
            "envelope": envelope,
        }

        # Setup response future
        resp_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        if task_id:
            self._pending_responses[task_id] = resp_fut

        ack_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_acks[relay_id] = ack_fut

        await self._ws.send(json.dumps(frame))

        # Wait for delivery ack first
        try:
            ack = await asyncio.wait_for(ack_fut, timeout=10.0)
            logger.debug("send_relay_envelope received ack: %s", ack)
            if ack.get("status") == "queued":
                # Recipient is offline, envelope queued
                if task_id in self._pending_responses:
                    self._pending_responses.pop(task_id, None)
                return {"status": "queued", "relay_id": relay_id}
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for delivery_ack (relay_id=%s)", relay_id)
            self._pending_acks.pop(relay_id, None)

        # Wait for the response envelope from remote agent
        try:
            return await asyncio.wait_for(resp_fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_responses.pop(task_id, None)
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Timed out waiting for response from remote agent via gateway.",
            ) from None

    async def _disconnect(self) -> None:
        self._connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


class GatewayA2ATransport:
    """A2ATransport implementation that routes via Gateway or direct HTTP."""

    def __init__(
        self,
        *,
        http_transport: A2ATransport,
        gateway_client: GatewayClient | None = None,
    ) -> None:
        self._http = http_transport
        self._gateway = gateway_client

    def set_gateway_client(self, client: GatewayClient | None) -> None:
        self._gateway = client

    async def send(
        self, endpoint: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a signed envelope, return the parsed response envelope.
        
        Selection rules (Part 13):
        1. If a live Gateway connection exists: route through Gateway.
        2. If Gateway is unavailable AND endpoint is a valid direct HTTP(S) URL: use DirectHTTPTransport.
        3. If neither is available: raise transport unavailable error.
        """
        # 1. Prefer Gateway if connected
        if self._gateway is not None and self._gateway.is_connected:
            return await self._gateway.send_relay_envelope(envelope)

        # 2. If endpoint explicitly targets gateway but gateway is disconnected
        is_gateway_target = (
            endpoint.startswith("ws://")
            or endpoint.startswith("wss://")
            or endpoint.startswith("gateway://")
        )
        if is_gateway_target:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Gateway transport is not connected.",
            )

        # 3. Fallback to direct HTTP transport if endpoint is valid http/https
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return await self._http.send(endpoint, envelope)

        raise A2AError(
            A2AErrorCode.TRANSPORT_ERROR,
            f"No valid transport available for endpoint: {endpoint}",
        )


__all__ = ["GatewayClient", "GatewayA2ATransport"]
