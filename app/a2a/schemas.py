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
PROTOCOL_VERSION = "0.2"
#: Versions this implementation understands. 0.1 stays accepted so a peer that
#: has not upgraded keeps working; it simply cannot express 0.2 features.
SUPPORTED_PROTOCOL_VERSIONS = ("0.1", "0.2")

#: Which optional features each protocol version can express. Checked rather
#: than assumed, so a 0.2-only feature is REFUSED on a 0.1 envelope instead of
#: being silently ignored.
_FEATURES_BY_VERSION: dict[str, frozenset[str]] = {
    "0.1": frozenset(),
    "0.2": frozenset(
        {
            "correlation_id",
            "reply_to",
            "capability",
            "authorization",
            "trace",
            "error_messages",
            "progress",
            "cancel",
            "approval",
        }
    ),
}


class CapabilityRef(BaseModel):
    """Reference to a capability contract: which one, and which version.

    Validated here so a malformed reference fails at parse time rather than
    being compared against a registry and silently not matching.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = "1.0"

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        from app.a2a.capabilities import CAPABILITY_ID_PATTERN

        if not value or not CAPABILITY_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "capability.id must be a dotted lowercase slug "
                "(e.g. 'calendar.availability')"
            )
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        from app.a2a.capabilities import CAPABILITY_VERSION_PATTERN

        if not value or not CAPABILITY_VERSION_PATTERN.fullmatch(value):
            raise ValueError("capability.version must be 'major' or 'major.minor'")
        return value

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.id}@{self.version}"


class AuthorizationRef(BaseModel):
    """The authority a message is presented under.

    Present so delegated authority is EXPLICIT on the wire. Before 0.2 there
    was no representation for delegation at all, so a receiver had no way to
    distinguish an agent acting for itself from one acting for someone else.
    """

    model_config = ConfigDict(extra="forbid")

    #: "delegation_grant" | "consent" | "owner"
    kind: Literal["delegation_grant", "consent", "owner"]
    #: Identifier of the grant/consent, when applicable.
    reference: str | None = None
    #: The principal whose authority is being exercised.
    on_behalf_of: str | None = None


class TraceContext(BaseModel):
    """Cross-service correlation metadata (M11 consumes it; M6 carries it).

    The audit's observability finding was that no request could be traced from
    user -> app -> gateway -> agent -> tool -> DB, because no identifier was
    propagated. This is that identifier.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None

    @field_validator("trace_id", "span_id", "parent_span_id")
    @classmethod
    def _valid_hex(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{16,32}", value):
            raise ValueError(f"{info.field_name} must be 16-32 lowercase hex chars")
        return value


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
    """The signed A2A message envelope.

    Protocol 0.2 (M6) is ADDITIVE over 0.1. Every new field is optional with a
    safe default, and a 0.1 envelope still validates, so an existing peer keeps
    working and a mixed-version deployment does not break during rollout.

    0.1 -> 0.2 additions, and what each one fixes:

        correlation_id   groups an exchange. 0.1 correlated on task_id alone,
                         which cannot express several concurrent exchanges
                         inside one logical task.
        reply_to         the message_id being answered. In 0.1 this was implied
                         by task_id, so a reply to a reply was ambiguous.
        capability       {id, version}: WHAT is being invoked. 0.1 addressed a
                         capability only through a free-form `purpose` slug
                         plus payload.action, which a third party could not
                         validate against.
        authorization    a delegation grant / consent reference. There was no
                         way to carry delegated authority at all.
        trace            {trace_id, span_id}: correlation across services. The
                         audit's headline observability gap: no request could
                         be traced from user to gateway to agent to tool.
        protocol_version_major / minor are exposed via `version`.

    Message-type additions: `error`, `task_progress`, `task_cancel`,
    `task_cancelled`, `capability_query`, `capability_response`,
    `approval_required`, `approval_granted`, `approval_denied`. In 0.1 errors
    were HTTP statuses and approvals were server-side state with no wire
    representation, so a peer could not express "I need your owner to approve
    this".
    """

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["nexus-a2a"] = PROTOCOL
    #: "0.1" is still accepted so an older peer is not cut off. New sends use
    #: PROTOCOL_VERSION (0.2) unless a caller pins an older version.
    version: Literal["0.1", "0.2"] = PROTOCOL_VERSION
    message_id: str
    task_id: str
    sender: str
    recipient: str
    timestamp: str
    expires_at: str
    message_type: Literal[
        # 0.1 vocabulary
        "request",
        "response",
        "task_request",
        "task_response",
        "task_proposal",
        # 0.2 vocabulary
        "error",
        "task_progress",
        "task_cancel",
        "task_cancelled",
        "capability_query",
        "capability_response",
        "approval_required",
        "approval_granted",
        "approval_denied",
    ]
    purpose: str
    task_type: str | None = None
    payload: dict[str, Any]
    # --- 0.2 additions (all optional; default to 0.1 behaviour) ------------
    correlation_id: str | None = None
    reply_to: str | None = None
    capability: "CapabilityRef | None" = None
    authorization: "AuthorizationRef | None" = None
    trace: "TraceContext | None" = None
    # base64 Ed25519 signature over canonical(unsigned envelope); not part
    # of the signed bytes.
    signature: str | None = None

    @property
    def protocol_major(self) -> int:
        return int(self.version.split(".", 1)[0])

    def supports(self, feature: str) -> bool:
        """Whether this envelope's protocol version carries ``feature``.

        Used to refuse, rather than silently ignore, a feature a peer's
        version cannot express.
        """
        return feature in _FEATURES_BY_VERSION.get(self.version, frozenset())

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

    @field_validator("correlation_id", "reply_to")
    @classmethod
    def _valid_optional_id(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
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

        Optional fields whose value is None are OMITTED, not serialized as
        null. This is a correctness requirement, not tidiness:

          * The canonical form must be identical between implementations. A
            peer that omits an absent optional field and one that emits
            ``"capability":null`` would produce different signed bytes and
            could never verify each other.
          * It preserves 0.1 compatibility. Before 0.2 existed, envelopes had
            no ``capability``/``trace``/``authorization`` fields at all. If a
            0.2 implementation emitted them as null, every 0.1 signature would
            stop verifying - a silent, total interoperability break.

        An earlier version only dropped ``task_type`` at the TOP level, which
        meant (a) adding the 0.2 fields changed the signed bytes of EVERY
        message, and (b) nested optionals such as ``authorization.on_behalf_of``
        and ``trace.parent_span_id`` still serialized as null. Both were caught
        by the cross-implementation conformance suite (M9), which is exactly
        why that suite exists.
        """
        dumped = self.model_dump(exclude={"signature"})
        return _drop_none(dumped)

    def canonical_bytes(self) -> bytes:
        from app.a2a.serialization import envelope_canonical_bytes

        return envelope_canonical_bytes(self)


def _drop_none(node: Any) -> Any:
    """Recursively omit None-valued keys from dicts.

    A module-level function (not a method) because the rule applies to nested
    structures too: ``{"trace": {"parent_span_id": None}}`` must become
    ``{"trace": {}}``, not ``{"trace": {"parent_span_id": null}}``. A
    single-level filter would leave that disagreement in place.
    """
    if isinstance(node, dict):
        return {
            key: _drop_none(value)
            for key, value in node.items()
            if value is not None
        }
    if isinstance(node, list):
        return [_drop_none(item) for item in node]
    return node


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
