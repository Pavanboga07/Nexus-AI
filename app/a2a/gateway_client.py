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
from collections.abc import Callable, Coroutine
from typing import Any

import websockets

try:
    from websockets.asyncio.client import ClientConnection as WSClient
except ImportError:
    from websockets.client import WebSocketClientProtocol as WSClient

from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.gateway_translate import (
    GatewayTranslationError,
    from_gateway_envelope,
    sign_gateway_envelope,
    to_gateway_envelope,
)
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
        # Gateway error frames carry correlation_id (not relay_id); this maps
        # them back to the in-flight ack future so a rejection fails the send
        # immediately instead of burning the ack timeout.
        self._ack_correlation: dict[str, str] = {}

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

            # Proactive heartbeats: the gateway only refreshes presence on
            # client-initiated heartbeats, so without this loop the presence
            # written at auth goes permanently stale.
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                await self._read_loop(ws)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _heartbeat_loop(self, ws: WSClient) -> None:
        """Send {"type": "heartbeat"} every heartbeat_interval seconds."""
        try:
            while self._connected:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._connected:
                    break
                try:
                    await ws.send(json.dumps({"type": "heartbeat"}))
                except Exception as exc:
                    logger.debug("gateway heartbeat send failed: %s", exc)
                    break
        except asyncio.CancelledError:
            pass

    async def _authenticate(self, ws: WSClient) -> None:
        """Execute cryptographic challenge-response authentication with gateway."""
        # 1. Receive challenge (bounded: a gateway without a server-side auth
        #    timeout must not hang us forever).
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        except TimeoutError:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Timed out waiting for gateway auth_challenge.",
            ) from None
        data = json.loads(raw)
        if data.get("type") != "auth_challenge":
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                f"Expected auth_challenge, got {data.get('type')}",
            )

        challenge_b64 = data["challenge"]
        challenge_bytes = base64.b64decode(challenge_b64.encode("ascii"))

        # 2. Sign challenge with the local identity bound to this agent
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
        try:
            raw_result = await asyncio.wait_for(ws.recv(), timeout=15.0)
        except TimeoutError:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Timed out waiting for gateway auth_result.",
            ) from None
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
            await self._handle_delivery(ws, frame)

        elif frame_type == "delivery_ack":
            relay_id = frame.get("relay_id")
            logger.debug("Gateway delivery_ack received: %s", frame)
            self._ack_correlation.pop(frame.get("correlation_id", ""), None)
            if relay_id and relay_id in self._pending_acks:
                fut = self._pending_acks.pop(relay_id)
                if not fut.done():
                    fut.set_result(frame)

        elif frame_type == "delivery_failed":
            relay_id = frame.get("relay_id")
            logger.warning("Gateway delivery_failed received: %s", frame)
            self._ack_correlation.pop(frame.get("correlation_id", ""), None)
            if relay_id and relay_id in self._pending_acks:
                fut = self._pending_acks.pop(relay_id)
                if not fut.done():
                    fut.set_exception(
                        A2AError(A2AErrorCode.TRANSPORT_ERROR, frame.get("reason", "Delivery failed"))
                    )

        elif frame_type == "error":
            # Gateway rejection (e.g. INVALID_ENVELOPE): surface it with its
            # correlation id instead of silently ignoring it, and fail the
            # in-flight send it answers so the caller learns immediately.
            code = frame.get("code")
            correlation_id = frame.get("correlation_id")
            logger.warning(
                "gateway error frame code=%s correlation_id=%s message=%s",
                code,
                correlation_id,
                frame.get("message"),
            )
            relay_id = self._ack_correlation.pop(correlation_id, None) if correlation_id else None
            if relay_id and relay_id in self._pending_acks:
                fut = self._pending_acks.pop(relay_id)
                if not fut.done():
                    fut.set_exception(
                        A2AError(
                            A2AErrorCode.TRANSPORT_ERROR,
                            f"Gateway rejected envelope: {code}: {frame.get('message')}",
                        )
                    )

        elif frame_type == "heartbeat":
            # Echo pong back
            pong = {"type": "heartbeat"}
            await ws.send(json.dumps(pong))

        elif frame_type == "heartbeat_ack":
            logger.debug("Gateway heartbeat_ack received")

    async def _handle_delivery(self, ws: WSClient, frame: dict[str, Any]) -> None:
        """Process one gateway delivery frame.

        Order matters: unwrap the 0.3 envelope to the nested signed 0.2
        envelope, VALIDATE it, dispatch it, and only then ack. A malformed
        envelope is dead-lettered (acked so the gateway does not redeliver
        poison forever, with message_id/sender in the log line) — never
        acked-then-silently-dropped.
        """
        relay_id = frame.get("relay_id")
        envelope_data = frame.get("envelope", {})

        async def ack_delivery() -> None:
            if relay_id:
                await ws.send(json.dumps({"type": "delivery_ack", "relay_id": relay_id}))

        # 1. Unwrap 0.3 -> 0.2.
        try:
            inner_data = from_gateway_envelope(envelope_data)
        except GatewayTranslationError as exc:
            logger.warning(
                "delivery_untranslatable message_id=%s sender=%s detail=%s; dead-lettering",
                envelope_data.get("message_id"),
                envelope_data.get("sender"),
                exc,
            )
            await ack_delivery()
            return

        # 2. Validate the 0.2 envelope BEFORE acking.
        try:
            envelope = A2AEnvelope.model_validate(inner_data)
        except Exception as exc:
            logger.warning(
                "delivery_malformed_envelope message_id=%s sender=%s detail=%s; dead-lettering",
                inner_data.get("message_id"),
                inner_data.get("sender"),
                exc,
            )
            await ack_delivery()
            return

        # 3. Correlate responses to outbound requests we are awaiting.
        msg_type = envelope.message_type
        task_id = envelope.task_id
        if msg_type in {"response", "task_response"} and task_id in self._pending_responses:
            fut = self._pending_responses.pop(task_id)
            if not fut.done():
                fut.set_result(envelope.model_dump())

        # 4. Dispatch to the local inbound handler (full verification pipeline).
        response_envelope: A2AEnvelope | None = None
        if self._inbound_handler:
            try:
                response_envelope = await self._inbound_handler(envelope)
            except Exception as exc:
                # Validated but not dispatchable: ack anyway, so the gateway
                # does not redeliver a message that fails identically forever.
                logger.error(
                    "inbound handler failure for delivered envelope "
                    "message_id=%s sender=%s: %s",
                    envelope.message_id,
                    envelope.sender,
                    exc,
                )
        if response_envelope is not None and msg_type not in {"response", "task_response"}:
            # Relay the signed 0.2 response back through the gateway as 0.3.
            gateway_envelope = to_gateway_envelope(response_envelope.model_dump())
            signed = await sign_gateway_envelope(self._identity, gateway_envelope)
            response_frame = {
                "type": "relay_envelope",
                "relay_id": f"relay_{uuid.uuid4().hex}",
                "recipient": response_envelope.recipient,
                "correlation_id": signed["correlation_id"],
                "envelope": signed,
            }
            await ws.send(json.dumps(response_frame))

        # 5. Ack only after successful validation + dispatch.
        await ack_delivery()

    async def send_relay_envelope(
        self,
        envelope: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a signed 0.2 envelope through the gateway; await the response.

        The 0.2 envelope is translated to the gateway's 0.3 wire format at
        this boundary (see ``app.a2a.gateway_translate``) and the outer 0.3
        envelope is signed with the local identity.

        Raises A2AError(QUEUED) when the gateway accepts the envelope for an
        offline recipient — no response will arrive on this call, so returning
        a dict would let callers mistake it for a completed delegation.
        """
        if not self.is_connected or self._ws is None:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Gateway client is not connected.",
            )

        task_id = envelope.get("task_id")
        if not task_id:
            # Fail fast: without a task_id no response can ever be correlated
            # back to this call, so awaiting would just burn the timeout.
            raise A2AError(
                A2AErrorCode.INVALID_ENVELOPE,
                "Cannot relay an envelope without a task_id.",
            )

        gateway_envelope = to_gateway_envelope(envelope)
        signed_envelope = await sign_gateway_envelope(
            self._identity, gateway_envelope
        )
        correlation_id = signed_envelope["correlation_id"]

        relay_id = f"relay_{uuid.uuid4().hex}"
        frame = {
            "type": "relay_envelope",
            "relay_id": relay_id,
            "recipient": envelope.get("recipient"),
            "correlation_id": correlation_id,
            "envelope": signed_envelope,
        }

        # Setup response future
        loop = asyncio.get_running_loop()
        resp_fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_responses[task_id] = resp_fut

        ack_fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_acks[relay_id] = ack_fut
        self._ack_correlation[correlation_id] = relay_id

        try:
            await self._ws.send(json.dumps(frame))
        except Exception:
            self._pending_responses.pop(task_id, None)
            self._pending_acks.pop(relay_id, None)
            self._ack_correlation.pop(correlation_id, None)
            raise

        # Wait for delivery ack first; fail fast when it times out instead of
        # burning the full response timeout on a message the gateway never
        # accepted. (An "error" frame from the gateway fails ack_fut via
        # _handle_frame with the gateway's rejection reason.)
        try:
            ack = await asyncio.wait_for(ack_fut, timeout=10.0)
        except TimeoutError:
            self._pending_acks.pop(relay_id, None)
            self._pending_responses.pop(task_id, None)
            self._ack_correlation.pop(correlation_id, None)
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Timed out waiting for gateway delivery_ack.",
            ) from None
        self._ack_correlation.pop(correlation_id, None)
        logger.debug("send_relay_envelope received ack: %s", ack)
        if ack.get("status") == "queued":
            # Recipient is offline; the gateway holds the envelope. Nothing
            # will arrive on resp_fut, so raise rather than return a dict.
            self._pending_responses.pop(task_id, None)
            raise A2AError(
                A2AErrorCode.QUEUED,
                f"Recipient is offline; envelope queued on the gateway "
                f"(relay_id={relay_id}, task_id={task_id}).",
                details={"relay_id": relay_id, "task_id": task_id},
            )

        # Wait for the response envelope from remote agent
        try:
            return await asyncio.wait_for(resp_fut, timeout=timeout)
        except TimeoutError:
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
    """A2ATransport implementation that routes via the Gateway or direct HTTP.

    Selection (decision D2 - gateway-first):

      1. A live Gateway connection exists -> route through the Gateway.
         This is the PREFERRED and default path: it traverses NAT, gives
         durable offline buffering, and gives one transport to reason about.
      2. No live Gateway AND the endpoint is a direct http(s) URL AND direct
         egress is explicitly enabled (``NEXUS_A2A_DIRECT_EGRESS=true``) ->
         direct HTTP.
      3. No live Gateway and direct egress not enabled -> raise a transport
         error naming the reason.

    Direct egress is opt-in on purpose: silently falling back to a direct HTTP
    POST means a peer's NAT reachability decides which delivery semantics you
    get (no offline queue, no ack, different failure modes). An operator should
    choose that, not discover it.

    KNOWN LIMITATION (multi-agent): the GatewayClient authenticates as the
    local PRIMARY agent and ``send_relay_envelope`` ignores the ``endpoint``
    argument, so over the gateway every outbound message is sent as that agent
    regardless of which local agent originated it. Per-agent gateway
    connections are the fix and are tracked with the M9 work.
    """

    def __init__(
        self,
        *,
        http_transport: A2ATransport | None,
        gateway_client: GatewayClient | None = None,
        allow_direct_egress: bool = False,
    ) -> None:
        self._http = http_transport
        self._gateway = gateway_client
        self._allow_direct_egress = allow_direct_egress

    def set_gateway_client(self, client: GatewayClient | None) -> None:
        self._gateway = client

    @property
    def using_gateway(self) -> bool:
        return self._gateway is not None and self._gateway.is_connected

    async def send(
        self, endpoint: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a signed envelope, return the parsed response envelope."""
        # 1. Prefer the Gateway.
        if self._gateway is not None and self._gateway.is_connected:
            return await self._gateway.send_relay_envelope(envelope)

        # 2. Explicit gateway target with no connection is a hard failure - it
        #    must not silently become a direct POST to a ws:// URL.
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

        # 3. Direct HTTP only when the operator has opted in.
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            if not self._allow_direct_egress:
                raise A2AError(
                    A2AErrorCode.TRANSPORT_ERROR,
                    "Gateway is not connected and direct egress is disabled "
                    "(NEXUS_A2A_DIRECT_EGRESS=false). Direct HTTP would lose "
                    "the gateway's offline buffering and delivery acks; enable "
                    "it explicitly if that is intended.",
                )
            if self._http is None:
                raise A2AError(
                    A2AErrorCode.TRANSPORT_ERROR,
                    "No HTTP transport is configured for direct egress.",
                )
            return await self._http.send(endpoint, envelope)

        raise A2AError(
            A2AErrorCode.TRANSPORT_ERROR,
            f"No valid transport available for endpoint: {endpoint}",
        )


__all__ = ["GatewayClient", "GatewayA2ATransport"]
