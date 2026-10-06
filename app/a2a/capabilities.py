"""The capability model (M6).

Before this module, an agent's capabilities were free text:

    AgentCapability(name="calendar.check", description="Availability check",
                    data_category="availability")

That is enough to *display* a capability and not enough to *call* one. A
third-party agent had no way to know what to send, and the receiver had no way
to know whether what arrived was valid. Consequences:

  * no input/output contract, so a caller guessed the payload shape,
  * no versioning, so a capability could change meaning under a caller's feet,
  * no way to reject an unsupported or malformed request with a specific error,
  * and nothing to discover by (you could search by name but not by "who can
    book a meeting?").

A capability is now a typed, versioned contract:

    id             stable slug, e.g. "calendar.availability"
    version        semantic-ish string, e.g. "1.0"
    description    human-readable purpose
    data_category  the policy vocabulary category this exposes
    input_schema   JSON Schema (subset) for `payload`
    output_schema  JSON Schema (subset) for the response payload

Deliberate scope limit: this is a *subset* of JSON Schema - the keywords below
are supported and anything else is ignored. Implementing all of JSON Schema is
a project in itself, and a documented subset that both sides can rely on beats
a half-implemented full validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Capability ids are dotted lowercase slugs: "calendar.availability".
CAPABILITY_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
#: Versions are "major" or "major.minor" (we do not need full semver).
CAPABILITY_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")

MAX_CAPABILITY_ID_LENGTH = 96
MAX_CAPABILITY_VERSION_LENGTH = 16
MAX_DESCRIPTION_LENGTH = 500
MAX_SCHEMA_DEPTH = 8

#: JSON Schema keywords this subset understands. Anything else is ignored
#: rather than rejected, so a richer third-party schema still parses.
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "const",
        "additionalProperties",
        "description",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "pattern",
    }
)

_JSON_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


class CapabilityValidationError(ValueError):
    """A capability declaration is malformed."""


class CapabilityPayloadError(ValueError):
    """A payload does not satisfy a capability's declared input schema."""


@dataclass(frozen=True)
class CapabilitySpec:
    """A typed, versioned capability contract."""

    id: str
    version: str = "1.0"
    description: str = ""
    data_category: str = "custom"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_capability_spec(self)

    @property
    def reference(self) -> str:
        """The canonical "id@version" reference used on the wire."""
        return f"{self.id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        """Card representation. Schemas are included: a caller cannot build a
        valid request without them, and they contain no private data."""
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "data_category": self.data_category,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CapabilitySpec:
        if not isinstance(raw, dict):
            raise CapabilityValidationError("Capability must be an object.")
        return cls(
            id=str(raw.get("id") or raw.get("name") or ""),
            version=str(raw.get("version") or "1.0"),
            description=str(raw.get("description") or ""),
            data_category=str(raw.get("data_category") or "custom"),
            input_schema=raw.get("input_schema") or {},
            output_schema=raw.get("output_schema") or {},
        )


def validate_capability_spec(spec: CapabilitySpec) -> None:
    """Validate a capability declaration. Raises CapabilityValidationError."""
    if not spec.id or len(spec.id) > MAX_CAPABILITY_ID_LENGTH:
        raise CapabilityValidationError(
            f"Capability id must be 1..{MAX_CAPABILITY_ID_LENGTH} characters."
        )
    if not CAPABILITY_ID_PATTERN.fullmatch(spec.id):
        raise CapabilityValidationError(
            f"Capability id {spec.id!r} must be a dotted lowercase slug "
            "(e.g. 'calendar.availability')."
        )
    if not spec.version or len(spec.version) > MAX_CAPABILITY_VERSION_LENGTH:
        raise CapabilityValidationError(
            f"Capability version must be 1..{MAX_CAPABILITY_VERSION_LENGTH} characters."
        )
    if not CAPABILITY_VERSION_PATTERN.fullmatch(spec.version):
        raise CapabilityValidationError(
            f"Capability version {spec.version!r} must be 'major' or 'major.minor'."
        )
    if len(spec.description) > MAX_DESCRIPTION_LENGTH:
        raise CapabilityValidationError(
            f"Capability description exceeds {MAX_DESCRIPTION_LENGTH} characters."
        )
    _validate_schema(spec.input_schema, path=f"{spec.id}.input_schema", depth=0)
    _validate_schema(spec.output_schema, path=f"{spec.id}.output_schema", depth=0)


def _validate_schema(schema: Any, *, path: str, depth: int) -> None:
    """Shallow structural validation of a schema declaration itself."""
    if depth > MAX_SCHEMA_DEPTH:
        raise CapabilityValidationError(f"{path} nests deeper than {MAX_SCHEMA_DEPTH}.")
    if not isinstance(schema, dict):
        raise CapabilityValidationError(f"{path} must be an object.")
    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        for t in types:
            if t not in _JSON_TYPES:
                raise CapabilityValidationError(
                    f"{path}.type {t!r} is not a JSON type."
                )
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise CapabilityValidationError(f"{path}.properties must be an object.")
        for name, sub in properties.items():
            _validate_schema(sub, path=f"{path}.properties.{name}", depth=depth + 1)
    items = schema.get("items")
    if items is not None:
        _validate_schema(items, path=f"{path}.items", depth=depth + 1)
    required = schema.get("required")
    if required is not None and not isinstance(required, list):
        raise CapabilityValidationError(f"{path}.required must be an array.")


def validate_payload_against_schema(
    payload: Any, schema: dict[str, Any], *, path: str = "payload"
) -> None:
    """Validate a payload against a capability schema (supported subset).

    Raises CapabilityPayloadError with a precise path so a caller can fix the
    request, rather than a generic "invalid payload".
    """
    if not schema:
        return  # no contract declared => anything goes

    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not _matches_any_type(payload, expected_types):
            raise CapabilityPayloadError(
                f"{path} must be {expected_types}, got {type(payload).__name__}."
            )

    if "const" in schema and payload != schema["const"]:
        raise CapabilityPayloadError(f"{path} must equal {schema['const']!r}.")

    if "enum" in schema:
        allowed = schema["enum"]
        if isinstance(allowed, list) and payload not in allowed:
            raise CapabilityPayloadError(
                f"{path} must be one of {allowed!r}."
            )

    if isinstance(payload, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in payload:
                raise CapabilityPayloadError(f"{path}.{name} is required.")
        additional = schema.get("additionalProperties", True)
        if additional is False:
            unexpected = set(payload) - set(properties)
            if unexpected:
                raise CapabilityPayloadError(
                    f"{path} has unexpected field(s): {sorted(unexpected)}."
                )
        for name, sub_schema in properties.items():
            if name in payload:
                validate_payload_against_schema(
                    payload[name], sub_schema, path=f"{path}.{name}"
                )

    if isinstance(payload, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(payload):
            validate_payload_against_schema(
                item, schema["items"], path=f"{path}[{index}]"
            )

    if isinstance(payload, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(payload) < min_length:
            raise CapabilityPayloadError(
                f"{path} must be at least {min_length} characters."
            )
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(payload) > max_length:
            raise CapabilityPayloadError(
                f"{path} must be at most {max_length} characters."
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.fullmatch(pattern, payload) is None:
                    raise CapabilityPayloadError(
                        f"{path} does not match the required pattern."
                    )
            except re.error as exc:
                raise CapabilityPayloadError(
                    f"{path} pattern is invalid: {exc}"
                ) from None

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and payload < minimum:
            raise CapabilityPayloadError(f"{path} must be >= {minimum}.")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and payload > maximum:
            raise CapabilityPayloadError(f"{path} must be <= {maximum}.")


def _matches_any_type(value: Any, expected_types: list[Any]) -> bool:
    for t in expected_types:
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(
            value, bool
        ):
            return True
        if t == "null" and value is None:
            return True
    return False


def version_compatible(declared: str, offered: str) -> bool:
    """Whether a caller asking for ``declared`` can be served by ``offered``.

    Compatible when the MAJOR versions match: a minor bump is additive, a major
    bump may break. This is what makes capability versioning meaningful instead
    of decorative - a 1.0 caller is still served by a 1.2 provider, and is
    refused by a 2.0 provider with UNSUPPORTED_CAPABILITY.
    """
    declared_major = declared.split(".", 1)[0]
    offered_major = offered.split(".", 1)[0]
    return declared_major == offered_major


__all__ = [
    "CAPABILITY_ID_PATTERN",
    "CAPABILITY_VERSION_PATTERN",
    "CapabilityPayloadError",
    "CapabilitySpec",
    "CapabilityValidationError",
    "SUPPORTED_SCHEMA_KEYWORDS",
    "validate_capability_spec",
    "validate_payload_against_schema",
    "version_compatible",
]
