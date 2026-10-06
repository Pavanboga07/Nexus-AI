"""Agent Card: a cryptographically signed self-description (Part 7).

Every Nexus agent can publish a card that describes:

    - who it is (agent_id, public_key, display_name)
    - where to reach it (endpoint)
    - what it can do (capabilities, supported_purposes)
    - when this description is valid (issued_at, expires_at)
    - proof of authenticity (Ed25519 signature over canonical JSON)

A card is NOT a message: it has no sender/recipient/task_id.  It is a
self-contained document that anyone can fetch, verify independently,
and use to decide whether to initiate communication.

Verification checklist for consumers:

    1. Schema validation (required fields, types, patterns)
    2. agent_id <-> public_key consistency
    3. Signature verification under the card's own public_key
    4. Time window (issued_at <= now < expires_at)

The card never contains private keys, memory, or policy details.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.a2a.schemas import AGENT_ID_PATTERN, PROTOCOL, PROTOCOL_VERSION

#: Card-specific constants.
CARD_TYPE = "agent-card"
MAX_CAPABILITIES = 50
MAX_PURPOSE_LENGTH = 64
PURPOSE_SLUG_PATTERN = re.compile(r"^[a-z0-9:_\-.]{1,64}$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class AgentCapability:
    """A capability exposed by an agent.

    Backward compatible with the pre-M6 free-text form (``name`` +
    ``description`` + ``data_category``) while carrying the 0.2 contract
    (``id``, ``version``, input/output schema) when the publisher has one. A
    0.1 consumer reads the fields it knows and ignores the rest, which is what
    makes the card format additive rather than breaking.
    """

    name: str
    description: str
    data_category: str
    #: 0.2 contract fields (None for a free-text-only capability).
    id: str | None = None
    version: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "data_category": self.data_category,
        }
        if self.id:
            payload["id"] = self.id
        if self.version:
            payload["version"] = self.version
        if self.input_schema:
            payload["input_schema"] = self.input_schema
        if self.output_schema:
            payload["output_schema"] = self.output_schema
        return payload


def build_card(
    *,
    agent_id: str,
    public_key: str,
    display_name: str,
    endpoint: str,
    capabilities: list[AgentCapability] | None = None,
    supported_purposes: list[str] | None = None,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """Build an unsigned agent card dict, ready for signing.

    Parameters
    ----------
    agent_id:
        The ``nexus:ed25519:<fingerprint>`` identity.
    public_key:
        Base64 raw Ed25519 public key.
    display_name:
        Human-readable name for this agent.
    endpoint:
        The base URL where ``/a2a/messages`` is reachable.
    capabilities:
        Tool/action capabilities to advertise.
    supported_purposes:
        Purpose slugs this agent recognises (e.g. "scheduling").
    ttl_seconds:
        How long the card is valid, in seconds from now.

    Returns
    -------
    dict
        Unsigned card dict.  Pass to ``sign_card()`` to add a signature.
    """
    now = datetime.now(UTC)
    issued_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (now + timedelta(seconds=ttl_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    caps = capabilities or []
    purposes = supported_purposes or []
    return {
        "type": CARD_TYPE,
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "agent_id": agent_id,
        "display_name": display_name,
        "public_key": public_key,
        "endpoint": endpoint,
        "capabilities": [c.to_dict() for c in caps[:MAX_CAPABILITIES]],
        "supported_purposes": purposes,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


def capabilities_from_tools(tools: list[dict[str, Any]]) -> list[AgentCapability]:
    """Convert ToolService.list_tools() output to card capabilities.

    Only name, description, and data_category cross the boundary - never
    inputSchema (implementation detail).
    """
    result: list[AgentCapability] = []
    for tool in tools:
        result.append(
            AgentCapability(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                data_category=tool.get("data_category", "custom"),
            )
        )
    return result


def capabilities_from_specs(specs: list[Any]) -> list[AgentCapability]:
    """Convert CapabilitySpec contracts into card capabilities (M6).

    The schemas are included: a caller cannot construct a valid request without
    them, and they contain no private data (an input schema describes what the
    agent ACCEPTS, not what it knows).

    ``name`` is emitted alongside ``id`` for backward compatibility with 0.1
    consumers that only understood the free-text form, so a peer that has not
    upgraded still sees the capability list.
    """
    result: list[AgentCapability] = []
    for spec in specs:
        result.append(
            AgentCapability(
                # `name` duplicates the id so a 0.1-only consumer still sees a
                # usable capability list.
                name=spec.id,
                description=spec.description,
                data_category=spec.data_category,
                id=spec.id,
                version=spec.version,
                input_schema=dict(spec.input_schema or {}),
                output_schema=dict(spec.output_schema or {}),
            )
        )
    return result


# --- Card validation (consumer side) ----------------------------------------


_REQUIRED_FIELDS = {
    "type", "protocol", "version", "agent_id", "display_name",
    "public_key", "endpoint", "capabilities", "supported_purposes",
    "issued_at", "expires_at",
}


class CardValidationError(ValueError):
    """Raised when a card fails structural validation."""


def validate_card_schema(card: dict[str, Any]) -> None:
    """Validate structural integrity of a card dict.

    Raises CardValidationError on any failure.
    """
    if not isinstance(card, dict):
        raise CardValidationError("Card must be a JSON object.")

    missing = _REQUIRED_FIELDS - set(card.keys())
    if missing:
        raise CardValidationError(
            f"Card is missing required fields: {', '.join(sorted(missing))}"
        )

    if card.get("type") != CARD_TYPE:
        raise CardValidationError(
            f"Card type must be '{CARD_TYPE}', got {card.get('type')!r}."
        )

    if card.get("protocol") != PROTOCOL:
        raise CardValidationError(
            f"Card protocol must be '{PROTOCOL}', got {card.get('protocol')!r}."
        )

    # Accept any protocol version we understand, not just the current one, so a
    # peer that has not upgraded yet can still be discovered. (Exact-equality
    # against PROTOCOL_VERSION would make every 0.1 card undiscoverable the
    # moment we bumped to 0.2.)
    from app.a2a.schemas import SUPPORTED_PROTOCOL_VERSIONS

    card_version = card.get("version")
    if card_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise CardValidationError(
            f"Card version must be one of {SUPPORTED_PROTOCOL_VERSIONS}, "
            f"got {card_version!r}."
        )

    agent_id = card.get("agent_id", "")
    if not isinstance(agent_id, str) or not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise CardValidationError(
            "agent_id must match nexus:ed25519:<32 hex chars>."
        )

    if not isinstance(card.get("public_key"), str) or len(card["public_key"]) < 8:
        raise CardValidationError("public_key must be a non-empty base64 string.")

    if not isinstance(card.get("endpoint"), str) or not card["endpoint"]:
        raise CardValidationError("endpoint must be a non-empty string.")

    if not isinstance(card.get("display_name"), str) or not card["display_name"]:
        raise CardValidationError("display_name must be a non-empty string.")

    for ts_field in ("issued_at", "expires_at"):
        value = card.get(ts_field, "")
        if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
            raise CardValidationError(
                f"{ts_field} must be UTC ISO-8601 (YYYY-MM-DDTHH:MM:SSZ)."
            )

    if not isinstance(card.get("capabilities"), list):
        raise CardValidationError("capabilities must be a list.")

    if not isinstance(card.get("supported_purposes"), list):
        raise CardValidationError("supported_purposes must be a list.")

    # Reject floats anywhere in the card (canonical signing constraint).
    _reject_floats(card)


def validate_card_time_window(
    card: dict[str, Any],
    *,
    max_clock_skew_seconds: float = 30.0,
    now: datetime | None = None,
) -> None:
    """Check that the card's time window is currently valid.

    Raises CardValidationError when the card is expired or issued
    too far in the future.
    """
    now = now or datetime.now(UTC)
    issued_at = datetime.strptime(card["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    expires_at = datetime.strptime(card["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )

    if expires_at <= now:
        raise CardValidationError("Card has expired.")

    if issued_at > now:
        skew = (issued_at - now).total_seconds()
        if skew > max_clock_skew_seconds:
            raise CardValidationError(
                "Card issued_at is too far in the future; are the clocks "
                "synchronised?"
            )

    if expires_at <= issued_at:
        raise CardValidationError("expires_at must be after issued_at.")


def _reject_floats(node: Any) -> None:
    """Reject floats recursively (canonical signing constraint)."""
    if isinstance(node, float):
        raise CardValidationError(
            "Floats are not allowed in agent cards (canonical signing "
            "requires platform-stable representations)."
        )
    if isinstance(node, dict):
        for value in node.values():
            _reject_floats(value)
    if isinstance(node, (list, tuple)):
        for value in node:
            _reject_floats(value)


__all__ = [
    "AgentCapability",
    "CARD_TYPE",
    "CardValidationError",
    "build_card",
    "capabilities_from_tools",
    "validate_card_schema",
    "validate_card_time_window",
]
