"""A2A-specific failures with stable codes and HTTP status mapping.

Errors carry structured metadata only - never stack traces, keys, or
credentials.
"""

from __future__ import annotations

import enum
from typing import Any


class A2AErrorCode(str, enum.Enum):
    MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    NOT_ADDRESSED_TO_US = "NOT_ADDRESSED_TO_US"
    UNTRUSTED_SENDER = "UNTRUSTED_SENDER"
    REVOKED_SENDER = "REVOKED_SENDER"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    EXPIRED = "EXPIRED"
    CLOCK_SKEW = "CLOCK_SKEW"
    REPLAY = "REPLAY"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_ENDPOINT = "INVALID_ENDPOINT"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    # Part 7: Agent Discovery & Agent Cards
    INVALID_CARD = "INVALID_CARD"
    CARD_EXPIRED = "CARD_EXPIRED"
    CARD_SIGNATURE_INVALID = "CARD_SIGNATURE_INVALID"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    # Part 8: Agent Task Delegation & Negotiation
    UNSUPPORTED_TASK_TYPE = "UNSUPPORTED_TASK_TYPE"
    NEGOTIATION_LIMIT_EXCEEDED = "NEGOTIATION_LIMIT_EXCEEDED"
    TASK_NOT_PENDING = "TASK_NOT_PENDING"
    TASK_EXPIRED = "TASK_EXPIRED"
    # Gateway offline buffering: the message was accepted and queued by the
    # relay for a currently-offline recipient; no response will arrive on
    # this call. Raised (not returned) so callers cannot mistake it for a
    # completed delegation.
    QUEUED = "QUEUED"
    # M6: capability contracts (0.2)
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"


_HTTP_STATUS = {
    A2AErrorCode.MESSAGE_TOO_LARGE: 400,
    A2AErrorCode.INVALID_ENVELOPE: 400,
    A2AErrorCode.NOT_ADDRESSED_TO_US: 400,
    A2AErrorCode.UNTRUSTED_SENDER: 401,
    A2AErrorCode.REVOKED_SENDER: 401,
    A2AErrorCode.IDENTITY_MISMATCH: 401,
    A2AErrorCode.INVALID_SIGNATURE: 401,
    A2AErrorCode.EXPIRED: 410,
    A2AErrorCode.CLOCK_SKEW: 400,
    A2AErrorCode.REPLAY: 409,
    A2AErrorCode.RATE_LIMITED: 429,
    A2AErrorCode.TRANSPORT_ERROR: 502,
    A2AErrorCode.INVALID_RESPONSE: 502,
    A2AErrorCode.INVALID_ENDPOINT: 422,
    A2AErrorCode.CONFLICT: 409,
    A2AErrorCode.NOT_FOUND: 404,
    # Part 7: Agent Discovery & Agent Cards
    A2AErrorCode.INVALID_CARD: 400,
    A2AErrorCode.CARD_EXPIRED: 410,
    A2AErrorCode.CARD_SIGNATURE_INVALID: 401,
    A2AErrorCode.DISCOVERY_FAILED: 502,
    # Part 8: Agent Task Delegation & Negotiation
    A2AErrorCode.UNSUPPORTED_TASK_TYPE: 400,
    A2AErrorCode.NEGOTIATION_LIMIT_EXCEEDED: 400,
    A2AErrorCode.TASK_NOT_PENDING: 409,
    A2AErrorCode.TASK_EXPIRED: 410,
    A2AErrorCode.QUEUED: 202,
    # M6
    A2AErrorCode.UNSUPPORTED_CAPABILITY: 400,
    A2AErrorCode.UNSUPPORTED_VERSION: 400,
}


class A2AError(Exception):
    """An expected A2A failure with a stable code and HTTP status."""

    def __init__(
        self,
        code: A2AErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: Structured context (e.g. task_id/relay_id on QUEUED) so handlers
        #: can act on it without parsing the human-readable message.
        self.details: dict[str, Any] = details or {}
        self.http_status = _HTTP_STATUS.get(code, 400)

    def to_dict(self) -> dict[str, str]:
        return {"error": {"code": self.code.value, "message": self.message}}


__all__ = ["A2AError", "A2AErrorCode"]
