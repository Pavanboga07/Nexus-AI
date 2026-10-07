"""0.2 <-> 0.3 envelope translation for the Nexus Gateway relay.

Nexus-AI speaks the 0.2 A2A envelope (``app.a2a.schemas``); the Nexus
Gateway relay validates the frozen 0.3 envelope (``relay/envelope.py`` on
the gateway: ``version: Literal["0.3"]``, required ``correlation_id``,
restricted ``message_type`` set, ``extra="forbid"``). Without translation
every gateway-routed message is rejected with INVALID_ENVELOPE, in both
directions — gateway-routed A2A was completely dead.

The translation is LOSSLESS by construction: the complete SIGNED 0.2
envelope (signature included) is nested inside the 0.3 ``payload`` under
``"a2a_v02"``. End-to-end Ed25519 authentication therefore still rests on
the inner 0.2 signature, which the existing inbound pipeline verifies; the
outer 0.3 envelope is additionally signed so 0.3-native verifiers see a
well-formed signed envelope too.

``correlation_id`` is REQUIRED by 0.3 and groups one ask/approve/answer
exchange across hops. It is derived deterministically as
``f"{task_id}_{message_id}"`` — underscore, not colon, because both the
0.2 (``A2A_ID_PATTERN``) and 0.3 (``ID_PATTERN``) identifier patterns
forbid ``:``.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from app.identity import crypto

logger = logging.getLogger("nexus.a2a.gateway_translate")

#: The envelope version the gateway relay validates.
GATEWAY_ENVELOPE_VERSION = "0.3"

#: Payload key under which the full 0.2 envelope rides inside a 0.3 envelope.
A2A_V02_WRAPPER_KEY = "a2a_v02"

#: 0.2 message vocabulary -> 0.3 message vocabulary. 0.3 has no notion of
#: tasks/proposals/progress; those ride as request/response with the full
#: 0.2 envelope nested, so the receiver recovers the original type from the
#: wrapper, not from this mapping.
MESSAGE_TYPE_TO_GATEWAY: dict[str, str] = {
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


class GatewayTranslationError(ValueError):
    """A 0.3 envelope cannot be translated back to a 0.2 envelope."""


def new_correlation_id(task_id: str, message_id: str) -> str:
    """Deterministic 0.3 correlation_id for one 0.2 message.

    Underscore-joined (not colon-joined): both ID patterns forbid ":".
    5+32+1+4+32 = 74 chars, under the 128-char limit.
    """
    return f"{task_id}_{message_id}"


def is_gateway_envelope(data: Any) -> bool:
    """True when ``data`` looks like a 0.3 gateway envelope."""
    return (
        isinstance(data, dict)
        and data.get("protocol") == "nexus-a2a"
        and data.get("version") == GATEWAY_ENVELOPE_VERSION
    )


def to_gateway_envelope(envelope_02: dict[str, Any]) -> dict[str, Any]:
    """Translate a SIGNED 0.2 envelope dict to an UNSIGNED 0.3 envelope dict.

    The caller signs the result with :func:`sign_gateway_envelope`.
    Raises KeyError/ValueError when required 0.2 fields are missing or the
    message type has no 0.3 mapping.
    """
    message_type_02 = envelope_02["message_type"]
    try:
        message_type_03 = MESSAGE_TYPE_TO_GATEWAY[message_type_02]
    except KeyError:
        raise ValueError(
            f"0.2 message_type {message_type_02!r} has no 0.3 mapping"
        ) from None

    task_id = envelope_02["task_id"]
    message_id = envelope_02["message_id"]
    correlation_id = envelope_02.get("correlation_id") or new_correlation_id(
        task_id, message_id
    )

    return {
        "protocol": "nexus-a2a",
        "version": GATEWAY_ENVELOPE_VERSION,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "sender": envelope_02["sender"],
        "recipient": envelope_02["recipient"],
        "timestamp": envelope_02["timestamp"],
        "expires_at": envelope_02["expires_at"],
        "message_type": message_type_03,
        "payload": {A2A_V02_WRAPPER_KEY: envelope_02},
    }


def from_gateway_envelope(envelope_03: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a 0.3 envelope dict back to the nested SIGNED 0.2 envelope dict.

    Raises GatewayTranslationError when this is not a 0.3 envelope or the
    0.2 wrapper is absent — a native 0.3 envelope cannot be faithfully
    reconstructed as 0.2 (no task_id/purpose/task_type fields exist in 0.3),
    so it is a dead-letter, not a guess.
    """
    if not is_gateway_envelope(envelope_03):
        raise GatewayTranslationError(
            f"not a 0.3 gateway envelope (version={envelope_03.get('version')!r})"
        )
    payload = envelope_03.get("payload") or {}
    inner = payload.get(A2A_V02_WRAPPER_KEY)
    if not isinstance(inner, dict):
        raise GatewayTranslationError(
            f"0.3 envelope has no {A2A_V02_WRAPPER_KEY!r} wrapper; "
            "cannot reconstruct a 0.2 envelope"
        )
    return inner


def gateway_canonical_bytes(envelope_03: dict[str, Any]) -> bytes:
    """Canonical bytes of a 0.3 envelope, per the gateway's frozen rules.

    JSON with sorted keys, compact separators, ``ensure_ascii=False``;
    floats rejected anywhere; ``None``-valued keys omitted recursively;
    the ``signature`` field excluded from the bytes it covers. Mirrors
    ``relay/envelope.py::canonical_json_bytes`` exactly so a signature made
    here verifies against the gateway's implementation.
    """
    unsigned = {
        key: value for key, value in envelope_03.items() if key != "signature"
    }
    cleaned = _drop_none(unsigned)
    _reject_floats(cleaned)
    return json.dumps(
        cleaned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


async def sign_gateway_envelope(
    identity_service: Any, envelope_03: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of the 0.3 envelope dict with an Ed25519 signature."""
    signature = await identity_service.sign(
        gateway_canonical_bytes(envelope_03)
    )
    return {
        **envelope_03,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_gateway_signature(
    envelope_03: dict[str, Any], public_key_b64: str
) -> bool:
    """Verify a 0.3 envelope's outer signature. Never raises."""
    signature_b64 = envelope_03.get("signature")
    if not signature_b64 or not public_key_b64:
        return False
    try:
        public_key = crypto.load_public_key(
            base64.b64decode(public_key_b64.encode("ascii"), validate=True)
        )
        signature = base64.b64decode(
            signature_b64.encode("ascii"), validate=True
        )
        canonical = gateway_canonical_bytes(envelope_03)
    except Exception:
        return False
    return crypto.verify_bytes(public_key, canonical, signature)


def _drop_none(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _drop_none(value)
            for key, value in node.items()
            if value is not None
        }
    if isinstance(node, list):
        return [_drop_none(item) for item in node]
    return node


def _reject_floats(node: Any) -> None:
    if isinstance(node, float):
        raise ValueError(
            "floats are not allowed in gateway envelopes "
            "(platform-dependent repr breaks signatures)"
        )
    if isinstance(node, dict):
        for value in node.values():
            _reject_floats(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _reject_floats(value)


__all__ = [
    "A2A_V02_WRAPPER_KEY",
    "GATEWAY_ENVELOPE_VERSION",
    "MESSAGE_TYPE_TO_GATEWAY",
    "GatewayTranslationError",
    "from_gateway_envelope",
    "gateway_canonical_bytes",
    "is_gateway_envelope",
    "new_correlation_id",
    "sign_gateway_envelope",
    "to_gateway_envelope",
    "verify_gateway_signature",
]
