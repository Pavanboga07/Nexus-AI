"""Conformance tests for the 0.2 <-> 0.3 gateway translation layer (C1).

Nexus-AI speaks the 0.2 A2A envelope; the Nexus Gateway relay validates the
frozen 0.3 envelope. These tests pin the translation contract: a signed 0.2
task_request/task_response must survive a round trip through the 0.3 wire
format in both directions with the inner signature intact.
"""

from __future__ import annotations

import base64
import re

import pytest

from app.a2a import signing
from app.a2a.gateway_translate import (
    A2A_V02_WRAPPER_KEY,
    GATEWAY_ENVELOPE_VERSION,
    GatewayTranslationError,
    from_gateway_envelope,
    is_gateway_envelope,
    new_correlation_id,
    sign_gateway_envelope,
    to_gateway_envelope,
    verify_gateway_signature,
)
from app.a2a.schemas import (
    A2AEnvelope,
    new_message_id,
    new_task_id,
    utc_iso_in,
    utc_now_iso,
)
from app.identity import crypto

ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _agent() -> tuple[str, str, object]:
    priv, pub = crypto.generate_keypair()
    raw = crypto.public_key_bytes(pub)
    return (
        crypto.agent_id_from_public_key(raw),
        base64.b64encode(raw).decode("ascii"),
        priv,
    )


class _StubIdentity:
    """Minimal async signer backed by a real Ed25519 private key."""

    def __init__(self, private_key) -> None:
        self._private_key = private_key

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private_key, data)


def _signed_02_envelope(
    sender_id: str,
    recipient_id: str,
    private_key,
    message_type: str = "task_request",
    payload: dict | None = None,
) -> dict:
    """Build a 0.2 envelope and sign it exactly like signing.sign_envelope."""
    message_id = new_message_id()
    task_id = new_task_id()
    envelope = A2AEnvelope(
        message_id=message_id,
        task_id=task_id,
        sender=sender_id,
        recipient=recipient_id,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(300),
        message_type=message_type,  # type: ignore[arg-type]
        purpose="conformance",
        task_type="availability_check",
        payload=payload or {"status": "requested"},
        correlation_id=new_correlation_id(task_id, message_id),
    )
    signature = crypto.sign_bytes(private_key, envelope.canonical_bytes())
    signed = envelope.model_copy(
        update={"signature": base64.b64encode(signature).decode("ascii")}
    )
    return signed.model_dump()


def test_correlation_id_is_deterministic_and_valid() -> None:
    cid = new_correlation_id("task_abc123", "msg_def456")
    assert cid == "task_abc123_msg_def456"
    assert cid == new_correlation_id("task_abc123", "msg_def456")
    # No colon: both the 0.2 and 0.3 ID patterns forbid it.
    assert ":" not in cid
    assert ID_PATTERN.fullmatch(cid)


def test_to_gateway_envelope_shape() -> None:
    sender_id, _, sender_priv = _agent()
    recipient_id, _, _ = _agent()
    inner = _signed_02_envelope(sender_id, recipient_id, sender_priv)

    outer = to_gateway_envelope(inner)

    assert outer["protocol"] == "nexus-a2a"
    assert outer["version"] == GATEWAY_ENVELOPE_VERSION == "0.3"
    assert outer["message_id"] == inner["message_id"]
    assert outer["correlation_id"] == inner["correlation_id"]
    assert outer["sender"] == sender_id
    assert outer["recipient"] == recipient_id
    assert outer["message_type"] == "request"  # task_request -> request
    assert outer["payload"][A2A_V02_WRAPPER_KEY] == inner
    assert "signature" not in outer  # unsigned until sign_gateway_envelope
    assert is_gateway_envelope(outer)
    assert not is_gateway_envelope(inner)


def test_message_type_vocabulary_mapping() -> None:
    sender_id, _, sender_priv = _agent()
    recipient_id, _, _ = _agent()
    expected = {
        "request": "request",
        "task_request": "request",
        "task_proposal": "request",
        "task_cancel": "request",
        "capability_query": "request",
        "response": "response",
        "task_response": "response",
        "task_progress": "response",
        "task_cancelled": "response",
        "capability_response": "response",
        "approval_required": "approval_request",
        "approval_granted": "approve",
        "approval_denied": "reject",
        "error": "error",
    }
    for mt_02, mt_03 in expected.items():
        inner = _signed_02_envelope(
            sender_id, recipient_id, sender_priv, message_type=mt_02
        )
        assert to_gateway_envelope(inner)["message_type"] == mt_03, mt_02


@pytest.mark.asyncio
async def test_round_trip_task_request_and_response() -> None:
    """task_request -> 0.3 -> 0.2 and task_response -> 0.3 -> 0.2, both signed."""
    alice_id, alice_pub_b64, alice_priv = _agent()
    bob_id, bob_pub_b64, bob_priv = _agent()
    alice_ident = _StubIdentity(alice_priv)
    bob_ident = _StubIdentity(bob_priv)

    # --- Outbound: Alice's signed 0.2 task_request becomes a signed 0.3 frame.
    request_02 = _signed_02_envelope(alice_id, bob_id, alice_priv)
    outer_request = await sign_gateway_envelope(
        alice_ident, to_gateway_envelope(request_02)
    )
    assert verify_gateway_signature(outer_request, alice_pub_b64)
    assert not verify_gateway_signature(outer_request, bob_pub_b64)

    # --- Inbound (Bob's side): unwrap back to the exact 0.2 envelope.
    unwrapped_request = from_gateway_envelope(outer_request)
    assert unwrapped_request == request_02
    restored = A2AEnvelope.model_validate(unwrapped_request)
    assert signing.verify_envelope_signature(restored, alice_pub_b64)
    assert restored.message_type == "task_request"

    # --- Bob answers with a signed 0.2 task_response; same trip back.
    response_02 = _signed_02_envelope(
        bob_id,
        alice_id,
        bob_priv,
        message_type="task_response",
        payload={"status": "completed", "result": {"ok": True}},
    )
    outer_response = await sign_gateway_envelope(
        bob_ident, to_gateway_envelope(response_02)
    )
    assert outer_response["message_type"] == "response"
    unwrapped_response = from_gateway_envelope(outer_response)
    assert unwrapped_response == response_02
    restored_resp = A2AEnvelope.model_validate(unwrapped_response)
    assert signing.verify_envelope_signature(restored_resp, bob_pub_b64)
    assert restored_resp.message_type == "task_response"


def test_from_gateway_envelope_rejects_native_03() -> None:
    """A native 0.3 envelope (no 0.2 wrapper) is a dead-letter, not a guess."""
    native = {
        "protocol": "nexus-a2a",
        "version": "0.3",
        "message_id": "msg_x",
        "correlation_id": "corr_x",
        "sender": "nexus:ed25519:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "recipient": "nexus:ed25519:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "timestamp": "2026-10-06T00:00:00Z",
        "expires_at": "2026-10-06T00:05:00Z",
        "message_type": "request",
        "payload": {"hello": "world"},
    }
    with pytest.raises(GatewayTranslationError):
        from_gateway_envelope(native)
    with pytest.raises(GatewayTranslationError):
        from_gateway_envelope({"version": "0.2"})
