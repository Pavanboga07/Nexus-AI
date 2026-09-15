"""A2A wire schemas: the signed message envelope.

Envelope format (Part 6 spec §5):

    {
      "protocol": "nexus-a2a", "version": "0.1",
      "message_id": "msg_...", "task_id": "task_...",
      "sender": "nexus:ed25519:...", "recipient": "nexus:ed25519:...",
      "timestamp": "...Z", "expires_at": "...Z",
      "message_type": "request" | "response",
      "purpose": "<slug>",
      "payload": {...}
      -- signature added AFTER canonical serialization --
    }

The signature is NOT part of the signed bytes: canonicalization excludes it.
Timestamps are strict UTC "YYYY-MM-DDTHH:MM:SSZ" strings so both sides
produce byte-identical canonical JSON. Floats are rejected anywhere in the
payload (platform-dependent repr would break signature portability).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

AGENT_ID_PATTERN = re.compile(r"^nexus:ed25519:[0-9a-f]{32}$")
A2A_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
PURPOSE_PATTERN = re.compile(r"^[a-z0-9:_\-.]{1,64}$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

PROTOCOL = "nexus-a2a"
PROTOCOL_VERSION = "0.1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_in(seconds: float) -> str:
    future = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return future.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex}"


def _reject_floats(node: Any) -> None:
    if isinstance(node, float):
        raise ValueError(
            "floats are not allowed in A2A payloads (canonical signing "
            "requires platform-stable representations)"
        )
    if isinstance(node, dict):
        for value in node.values():
            _reject_floats(value)
    if isinstance(node, (list, tuple)):
        for value in node:
            _reject_floats(value)


class A2AEnvelope(BaseModel):
    """The signed A2A message envelope (wire format)."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["nexus-a2a"] = PROTOCOL
    version: Literal["0.1"] = PROTOCOL_VERSION
    message_id: str
    task_id: str
    sender: str
    recipient: str
    timestamp: str
    expires_at: str
    message_type: Literal[
        "request",
        "response",
        "task_request",
        "task_response",
        "task_proposal",
    ]
    purpose: str
    task_type: str | None = None
    payload: dict[str, Any]
    # base64 Ed25519 signature over canonical(unsigned envelope); not part
    # of the signed bytes.
    signature: str | None = None

    @field_validator("sender", "recipient")
    @classmethod
    def _valid_agent_id(cls, value: str, info) -> str:
        if not AGENT_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"{info.field_name} must match nexus:ed25519:<32 hex chars>"
            )
        return value

    @field_validator("message_id", "task_id")
    @classmethod
    def _valid_id(cls, value: str, info) -> str:
        if not A2A_ID_PATTERN.fullmatch(value):
            raise ValueError(f"{info.field_name} has an invalid format")
        return value

    @field_validator("purpose")
    @classmethod
    def _valid_purpose(cls, value: str) -> str:
        if not PURPOSE_PATTERN.fullmatch(value):
            raise ValueError("purpose may contain only lowercase slugs")
        return value

    @field_validator("task_type")
    @classmethod
    def _valid_task_type(cls, value: str | None) -> str | None:
        if value is not None and not PURPOSE_PATTERN.fullmatch(value):
            raise ValueError("task_type may contain only lowercase slugs")
        return value

    @field_validator("timestamp", "expires_at")
    @classmethod
    def _valid_timestamp(cls, value: str, info) -> str:
        if not TIMESTAMP_PATTERN.fullmatch(value):
            raise ValueError(
                f"{info.field_name} must be UTC ISO-8601 "
                "(YYYY-MM-DDTHH:MM:SSZ)"
            )
        parse_iso(value)  # reject impossible dates
        return value

    @field_validator("payload")
    @classmethod
    def _valid_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_floats(value)
        return value

    def unsigned_dict(self) -> dict[str, Any]:
        """Everything except the signature - the bytes covered by it.
        Excludes task_type if None so canonical signature of standard Part 6
        messages is identical."""
        dumped = self.model_dump(exclude={"signature"})
        if dumped.get("task_type") is None:
            dumped.pop("task_type", None)
        return dumped

    def canonical_bytes(self) -> bytes:
        from app.a2a.serialization import envelope_canonical_bytes

        return envelope_canonical_bytes(self)


def validate_request_payload(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract and validate (action, data_category) from a request payload."""
    action = payload.get("action")
    data_category = payload.get("data_category")
    if not isinstance(action, str) or not PURPOSE_PATTERN.fullmatch(action):
        raise ValueError("request payload requires a valid 'action' slug")
    if not isinstance(data_category, str) or not PURPOSE_PATTERN.fullmatch(
        data_category
    ):
        raise ValueError(
            "request payload requires a valid 'data_category' slug"
        )
    return action, data_category


__all__ = [
    "A2AEnvelope",
    "AGENT_ID_PATTERN",
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "new_message_id",
    "new_task_id",
    "parse_iso",
    "utc_iso_in",
    "utc_now_iso",
    "validate_request_payload",
]
