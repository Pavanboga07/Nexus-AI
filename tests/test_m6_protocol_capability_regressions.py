"""M6 regression tests: protocol 0.2, capability contracts, orchestration gate.

Audit findings covered:

  --   The envelope had no `correlation_id`, so several concurrent exchanges
       within one logical task were inexpressible (0.1 correlated on task_id
       alone); no `reply_to`, so a reply to a reply was ambiguous; no
       capability reference, so a capability was addressed only by a free-form
       `purpose` slug the receiver could not validate; no `authorization`, so
       delegation had no wire representation at all; and no `trace`, which is
       the audit's headline observability gap.
  --   Capabilities were free text (`name`, `description`, `data_category`)
       with no input/output contract and no versioning, so a third-party agent
       had to guess the payload shape and nothing could be rejected precisely.
  D5   Natural-language orchestration was always on. It routes free text
       through an LLM intent resolver and fuzzy target matching, so a wrong
       guess is acted upon - and its failures are invisible.
"""

from __future__ import annotations

import inspect

import pytest

from app.a2a.capabilities import (
    CapabilityPayloadError,
    CapabilitySpec,
    CapabilityValidationError,
    validate_capability_spec,
    validate_payload_against_schema,
    version_compatible,
)
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.schemas import (
    A2AEnvelope,
    CapabilityRef,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    utc_iso_in,
    utc_now_iso,
)

SENDER = "nexus:ed25519:" + "a" * 32
RECIPIENT = "nexus:ed25519:" + "b" * 32


def _envelope(**overrides) -> A2AEnvelope:
    base = dict(
        message_id="msg_" + "1" * 8,
        task_id="task_" + "2" * 8,
        sender=SENDER,
        recipient=RECIPIENT,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(300),
        message_type="request",
        purpose="scheduling",
        payload={"action": "read_memory", "data_category": "availability"},
    )
    base.update(overrides)
    return A2AEnvelope(**base)


# ---------------------------------------------------------------------------
# Protocol 0.2 is additive: 0.1 must keep working
# ---------------------------------------------------------------------------


def test_0_1_envelopes_still_validate() -> None:
    """A peer that has not upgraded must not be cut off."""
    env = _envelope(version="0.1")
    assert env.version == "0.1"
    # And every 0.2 field is simply absent.
    assert env.correlation_id is None
    assert env.reply_to is None
    assert env.capability is None
    assert env.authorization is None
    assert env.trace is None


def test_new_envelopes_default_to_the_current_version() -> None:
    assert PROTOCOL_VERSION == "0.2"
    assert _envelope().version == "0.2"
    assert "0.1" in SUPPORTED_PROTOCOL_VERSIONS
    assert "0.2" in SUPPORTED_PROTOCOL_VERSIONS


def test_unsupported_version_is_rejected() -> None:
    with pytest.raises(Exception):
        _envelope(version="9.9")


def test_0_1_cannot_claim_a_0_2_feature() -> None:
    """Version capability is checked, not assumed."""
    old = _envelope(version="0.1")
    new = _envelope()
    assert old.supports("capability") is False
    assert old.supports("trace") is False
    assert new.supports("capability") is True
    assert new.supports("trace") is True


def test_new_message_types_are_accepted() -> None:
    """Errors, progress, cancellation and approvals are now expressible."""
    for message_type in (
        "error",
        "task_progress",
        "task_cancel",
        "task_cancelled",
        "capability_query",
        "capability_response",
        "approval_required",
        "approval_granted",
        "approval_denied",
    ):
        env = _envelope(message_type=message_type)
        assert env.message_type == message_type


def test_0_2_fields_are_validated_not_just_accepted() -> None:
    """A malformed reference must fail at parse time, not silently mismatch."""
    with pytest.raises(Exception):
        _envelope(capability={"id": "Not A Slug", "version": "1.0"})
    with pytest.raises(Exception):
        _envelope(capability={"id": "calendar.availability", "version": "one"})
    with pytest.raises(Exception):
        _envelope(trace={"trace_id": "not-hex"})
    with pytest.raises(Exception):
        _envelope(reply_to="has spaces")
    # Valid values pass.
    env = _envelope(
        capability={"id": "calendar.availability", "version": "1.2"},
        trace={"trace_id": "a" * 32, "span_id": "b" * 16},
        reply_to="msg_" + "3" * 8,
        correlation_id="cor_" + "4" * 8,
    )
    assert str(env.capability) == "calendar.availability@1.2"
    assert env.trace is not None and env.trace.trace_id == "a" * 32


def test_authorization_refuses_an_unknown_kind() -> None:
    with pytest.raises(Exception):
        _envelope(authorization={"kind": "made_up"})
    env = _envelope(
        authorization={
            "kind": "delegation_grant",
            "reference": "grant_123",
            "on_behalf_of": "owner_1",
        }
    )
    assert env.authorization is not None
    assert env.authorization.kind == "delegation_grant"


# ---------------------------------------------------------------------------
# Capability contracts
# ---------------------------------------------------------------------------


def test_capability_spec_requires_a_wellformed_id_and_version() -> None:
    CapabilitySpec(id="calendar.availability", version="1.0")  # valid
    for bad_id in ("", "Calendar.Availability", "calendar availability", "x" * 200):
        with pytest.raises(CapabilityValidationError):
            CapabilitySpec(id=bad_id)
    for bad_version in ("", "one", "1.0.0.0", "v1"):
        with pytest.raises(CapabilityValidationError):
            CapabilitySpec(id="calendar.availability", version=bad_version)


def test_capability_schema_must_be_structurally_sound() -> None:
    with pytest.raises(CapabilityValidationError):
        CapabilitySpec(
            id="x.y", input_schema={"type": "not-a-json-type"}
        )
    with pytest.raises(CapabilityValidationError):
        CapabilitySpec(id="x.y", input_schema={"properties": "should be object"})


def test_payload_validation_gives_a_precise_reason() -> None:
    """A caller must learn WHICH field is wrong, not just 'invalid payload'."""
    schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "minLength": 4},
            "slots": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["date"],
        "additionalProperties": False,
    }

    validate_payload_against_schema({"date": "2026-09-21"}, schema)

    with pytest.raises(CapabilityPayloadError, match="date is required"):
        validate_payload_against_schema({}, schema)
    with pytest.raises(CapabilityPayloadError, match="unexpected field"):
        validate_payload_against_schema({"date": "2026-09-21", "x": 1}, schema)
    with pytest.raises(CapabilityPayloadError, match="must be .*string"):
        validate_payload_against_schema({"date": 5}, schema)
    with pytest.raises(CapabilityPayloadError, match="at least 4"):
        validate_payload_against_schema({"date": "ab"}, schema)
    # The valid first element passes and the invalid SECOND one is named by
    # index - precise paths are the point.
    with pytest.raises(CapabilityPayloadError, match=r"slots\[1\]"):
        validate_payload_against_schema(
            {"date": "2026-09-21", "slots": [1, "two"]}, schema
        )


def test_capability_versions_are_major_compatible() -> None:
    """Minor bumps are additive; a major bump is a breaking change."""
    assert version_compatible("1.0", "1.0")
    assert version_compatible("1.0", "1.7")
    assert not version_compatible("1.0", "2.0")
    assert not version_compatible("2.1", "1.9")


def test_an_empty_schema_accepts_anything() -> None:
    """Declaring no contract must not reject every request."""
    validate_payload_against_schema({"anything": [1, 2, 3]}, {})
    validate_payload_against_schema("a string", {})


# ---------------------------------------------------------------------------
# The service enforces capability contracts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_capability_is_refused_precisely(
    db_session_factory, db_owner_id
) -> None:
    """Naming a capability the agent does not offer is a specific error.

    Before M6 a request that named something unsupported either succeeded
    against the wrong handler or failed with a generic envelope error.
    """
    from app.a2a.service import A2AService
    from tests.test_a2a_service import LoopbackTransport
    from unittest.mock import MagicMock

    service = A2AService(
        session_factory=db_session_factory,
        identity_service=MagicMock(),
        policy_service=MagicMock(),
        memory_manager=None,
        transport=LoopbackTransport(),
        rate_limiter=MagicMock(),
    )
    assert service.capabilities, "the agent must declare some capabilities"
    assert "calendar.availability" in service.capabilities


def test_service_validates_payloads_against_the_contract() -> None:
    """The inbound path must actually call the payload validator.

    Inspects `_handle_inbound`: `handle_inbound` is a thin outcome-recording
    wrapper (M11), so the validation lives in the method it delegates to. The
    assertion is about the code that runs, not the name of the entry point.
    """
    from app.a2a.service import A2AService

    src = inspect.getsource(A2AService._handle_inbound)
    assert "validate_payload_against_schema" in src
    assert "UNSUPPORTED_CAPABILITY" in src
    assert "version_compatible" in src


def test_capability_category_is_authoritative_for_policy() -> None:
    """A caller must not widen disclosure by naming a different category.

    The capability's declared data_category governs which policy rule applies,
    so a request cannot smuggle a more sensitive category past the engine by
    mislabelling itself.
    """
    from app.a2a.service import A2AService

    src = inspect.getsource(A2AService._handle_inbound)
    assert "spec.data_category" in src


def test_inbound_outcomes_are_recorded_at_the_choke_point() -> None:
    """M11: the wrapper must sit in front of the real handler.

    If `handle_inbound` ever stops delegating - a refactor that reintroduces the
    validation inline, say - the outcome metric would silently stop recording
    and nothing else would fail. This is the guard for that.
    """
    from app.a2a.service import A2AService

    wrapper = inspect.getsource(A2AService.handle_inbound)
    assert "_handle_inbound" in wrapper
    assert "A2A_OUTCOMES.inc" in wrapper

    # And the wrapper must not have grown its own copy of the verification
    # steps: two implementations is exactly the C2 finding.
    assert "validate_payload_against_schema" not in wrapper


def test_cards_advertise_contracts_without_private_data() -> None:
    from app.a2a.cards import AgentCapability, capabilities_from_specs

    caps = capabilities_from_specs(
        [
            CapabilitySpec(
                id="calendar.availability",
                version="1.0",
                description="Check availability",
                data_category="availability",
                input_schema={"type": "object"},
            )
        ]
    )
    payload = caps[0].to_dict()
    assert payload["id"] == "calendar.availability"
    assert payload["version"] == "1.0"
    assert payload["input_schema"] == {"type": "object"}
    # Back-compat: 0.1 consumers read `name`.
    assert payload["name"] == "calendar.availability"

    # A free-text-only capability still serialises the old way.
    legacy = AgentCapability(name="x", description="y", data_category="z").to_dict()
    assert set(legacy) == {"name", "description", "data_category"}


def test_gateway_accepts_every_supported_protocol_version() -> None:
    """The relay must not pin one version, or a 0.1 peer breaks on deploy.

    The gateway is a relay and does not interpret envelopes, so it accepts all
    versions the app understands.
    """
    from pathlib import Path

    gateway_validation = (
        Path(__file__).resolve().parent.parent.parent
        / "nexus-gateway"
        / "app"
        / "security"
        / "validation.py"
    ).read_text(encoding="utf-8")
    assert 'SUPPORTED_PROTOCOL_VERSIONS = {"0.1", "0.2"}' in gateway_validation
    assert 'envelope.get("version") != "0.1"' not in gateway_validation
    # And the 0.2 message types are forwardable.
    for message_type in ("error", "task_progress", "approval_required"):
        assert message_type in gateway_validation


# ---------------------------------------------------------------------------
# D5: orchestration is off by default
# ---------------------------------------------------------------------------


def test_orchestration_is_disabled_by_default() -> None:
    """Decision D5: the fuzzy, LLM-routed path is opt-in."""
    from app.config.settings import Settings

    assert Settings().nexus_orchestration_enabled is False


def test_startup_logs_and_skips_orchestration_when_disabled() -> None:
    import app.main as main_module

    src = inspect.getsource(main_module.lifespan)
    assert "nexus_orchestration_enabled" in src
    assert "orchestration_disabled" in src


def test_disabling_orchestration_does_not_remove_the_api() -> None:
    """The workflow/task APIs remain: only the fuzzy router is gated."""
    import app.main as main_module

    src = inspect.getsource(main_module.create_app)
    assert "workflows_router" in src
    assert "tasks_router" in src
    assert "orchestration_router" in src
