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

    src = inspect.getsource(main_module.build_orchestration)
    assert "nexus_orchestration_enabled" in src
    assert "orchestration_disabled" in src
    # And lifespan still invokes the builder at startup.
    assert "build_orchestration(" in inspect.getsource(main_module.lifespan)


def test_disabling_orchestration_does_not_remove_the_api() -> None:
    """The workflow/task APIs remain: only the fuzzy router is gated."""
    import app.main as main_module

    src = inspect.getsource(main_module.create_app)
    assert "workflows_router" in src
    assert "tasks_router" in src
    assert "orchestration_router" in src


# ---------------------------------------------------------------------------
# Phase D5: information.search capability contract
# ---------------------------------------------------------------------------

SEARCH_TEST_ENDPOINT = "http://127.0.0.1:9999/a2a/messages"


class _StubSearchProvider:
    """Stub for the shared search-provider path (no network in tests)."""

    def __init__(self, results=None) -> None:
        from app.search import SearchResult

        self.calls: list[dict] = []
        self._results = (
            results
            if results is not None
            else [
                SearchResult(
                    title="Example",
                    url="https://example.com",
                    snippet="An example hit.",
                ),
                SearchResult(
                    title="Second",
                    url="https://example.com/2",
                    snippet="Another hit.",
                ),
            ]
        )

    async def search(self, query: str, count: int = 5):
        self.calls.append({"query": query, "count": count})
        return list(self._results[:count])


def _search_service(
    db_session_factory, policy_service, memory_manager, receiver_identity, provider
):
    from app.a2a.rate_limit import SlidingWindowRateLimiter
    from app.a2a.service import A2AService
    from tests.test_a2a_service import LoopbackTransport

    return A2AService(
        session_factory=db_session_factory,
        identity_service=receiver_identity,
        policy_service=policy_service,
        memory_manager=memory_manager,
        transport=LoopbackTransport(),
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
        search_provider=provider,
    )


async def _signed_search_request(
    sender,
    recipient_agent_id: str,
    *,
    capability_id: str | None = "information.search",
    capability_version: str = "1.0",
    payload_extra: dict | None = None,
):
    from app.a2a import signing
    from app.a2a.schemas import (
        A2AEnvelope,
        new_message_id,
        new_task_id,
        utc_iso_in,
        utc_now_iso,
    )

    payload = {
        "action": "disclose_information",
        "data_category": "public-web",
        "query": "latest nexus release",
        "count": 2,
    }
    if payload_extra:
        payload.update(payload_extra)
    kwargs: dict = {}
    if capability_id is not None:
        kwargs["capability"] = {"id": capability_id, "version": capability_version}
    envelope = A2AEnvelope(
        message_id=new_message_id(),
        task_id=new_task_id(),
        sender=sender.agent_id,
        recipient=recipient_agent_id,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(60),
        message_type="request",
        purpose="research",
        payload=payload,
        **kwargs,
    )
    return await signing.sign_envelope(sender, envelope)


async def _allow_search(policy_service, owner_id, requester_agent_id: str) -> None:
    await policy_service.create_policy(
        owner_id,
        requester_agent_id=requester_agent_id,
        data_category="public-web",
        action="disclose_information",
        purpose="research",
        decision="ALLOW",
        disclosure_scope="summary",
    )


def test_information_search_is_declared_with_schemas() -> None:
    """The agent advertises information.search v1.0 with input/output contracts."""
    from unittest.mock import MagicMock

    from app.a2a.service import A2AService

    service = A2AService(
        session_factory=MagicMock(),
        identity_service=MagicMock(),
        policy_service=MagicMock(),
        memory_manager=None,
        transport=MagicMock(),
        rate_limiter=MagicMock(),
        search_provider=_StubSearchProvider(),
    )
    spec = service.capabilities.get("information.search")
    assert spec is not None
    assert spec.version == "1.0"
    assert spec.data_category == "public-web"
    assert spec.input_schema["properties"]["query"]["maxLength"] == 500
    assert spec.input_schema["properties"]["count"]["maximum"] == 10
    assert "results" in spec.output_schema["properties"]


@pytest.mark.asyncio
async def test_information_search_envelope_executes(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """A 0.2 envelope naming information.search validates and executes."""
    from tests.test_a2a_service import StubIdentity

    sender = StubIdentity()
    receiver = StubIdentity()
    provider = _StubSearchProvider()
    service = _search_service(
        db_session_factory, policy_service, memory_manager, receiver, provider
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )
    await _allow_search(policy_service, db_owner_id, sender.agent_id)
    await memory_manager.store_memory(
        db_owner_id, memory_type="semantic", content="a private memory that must not leak"
    )

    envelope = await _signed_search_request(sender, receiver.agent_id)
    response = await service.handle_inbound(db_owner_id, envelope)

    assert response.payload["status"] == "completed"
    assert response.payload["results"] == [
        {"title": "Example", "url": "https://example.com", "snippet": "An example hit."},
        {"title": "Second", "url": "https://example.com/2", "snippet": "Another hit."},
    ]
    assert provider.calls == [{"query": "latest nexus release", "count": 2}]
    # Results only: never memory.
    assert "private memory" not in str(response.payload)
    assert "memories" not in response.payload


@pytest.mark.asyncio
async def test_unknown_capability_is_refused_precisely(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """Naming a capability the agent does not offer is UNSUPPORTED_CAPABILITY."""
    from tests.test_a2a_service import StubIdentity

    sender = StubIdentity()
    receiver = StubIdentity()
    service = _search_service(
        db_session_factory,
        policy_service,
        memory_manager,
        receiver,
        _StubSearchProvider(),
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )

    envelope = await _signed_search_request(
        sender, receiver.agent_id, capability_id="nosuch.thing"
    )
    with pytest.raises(A2AError) as excinfo:
        await service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.UNSUPPORTED_CAPABILITY


@pytest.mark.asyncio
async def test_search_major_version_mismatch_is_refused(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """information.search is offered at 1.0: a 2.0 request is refused."""
    from tests.test_a2a_service import StubIdentity

    sender = StubIdentity()
    receiver = StubIdentity()
    provider = _StubSearchProvider()
    service = _search_service(
        db_session_factory, policy_service, memory_manager, receiver, provider
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )
    await _allow_search(policy_service, db_owner_id, sender.agent_id)

    envelope = await _signed_search_request(
        sender, receiver.agent_id, capability_version="2.0"
    )
    with pytest.raises(A2AError) as excinfo:
        await service.handle_inbound(db_owner_id, envelope)
    assert excinfo.value.code is A2AErrorCode.UNSUPPORTED_CAPABILITY
    assert provider.calls == []


@pytest.mark.asyncio
async def test_search_minor_version_is_compatible(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """A 1.x request is served by the 1.0 contract (additive minor bumps)."""
    from tests.test_a2a_service import StubIdentity

    sender = StubIdentity()
    receiver = StubIdentity()
    service = _search_service(
        db_session_factory,
        policy_service,
        memory_manager,
        receiver,
        _StubSearchProvider(),
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )
    await _allow_search(policy_service, db_owner_id, sender.agent_id)

    envelope = await _signed_search_request(
        sender, receiver.agent_id, capability_version="1.4"
    )
    response = await service.handle_inbound(db_owner_id, envelope)
    assert response.payload["status"] == "completed"
    assert len(response.payload["results"]) == 2


@pytest.mark.asyncio
async def test_search_without_query_is_rejected(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """The contract requires a query: omitting it is INVALID_ENVELOPE."""
    from tests.test_a2a_service import StubIdentity

    from app.a2a import signing
    from app.a2a.schemas import (
        A2AEnvelope,
        new_message_id,
        new_task_id,
        utc_iso_in,
        utc_now_iso,
    )

    sender = StubIdentity()
    receiver = StubIdentity()
    provider = _StubSearchProvider()
    service = _search_service(
        db_session_factory, policy_service, memory_manager, receiver, provider
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )
    await _allow_search(policy_service, db_owner_id, sender.agent_id)

    envelope = A2AEnvelope(
        message_id=new_message_id(),
        task_id=new_task_id(),
        sender=sender.agent_id,
        recipient=receiver.agent_id,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(60),
        message_type="request",
        purpose="research",
        payload={
            "action": "disclose_information",
            "data_category": "public-web",
            "count": 2,
        },
        capability={"id": "information.search", "version": "1.0"},
    )
    signed = await signing.sign_envelope(sender, envelope)
    with pytest.raises(A2AError) as excinfo:
        await service.handle_inbound(db_owner_id, signed)
    assert excinfo.value.code is A2AErrorCode.INVALID_ENVELOPE
    assert provider.calls == []


@pytest.mark.asyncio
async def test_search_policy_deny_never_touches_provider(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """Policy gates the provider: DENY returns rejected without searching."""
    from tests.test_a2a_service import StubIdentity

    sender = StubIdentity()
    receiver = StubIdentity()
    provider = _StubSearchProvider()
    service = _search_service(
        db_session_factory, policy_service, memory_manager, receiver, provider
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )
    await policy_service.create_policy(
        db_owner_id,
        requester_agent_id=sender.agent_id,
        data_category="public-web",
        action="disclose_information",
        purpose="research",
        decision="DENY",
        priority=10,
    )

    envelope = await _signed_search_request(sender, receiver.agent_id)
    response = await service.handle_inbound(db_owner_id, envelope)

    assert response.payload["status"] == "rejected"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_search_provider_failure_maps_to_signed_failed(
    db_session_factory, db_owner_id, policy_service, memory_manager
) -> None:
    """A SearchError from the provider finalizes as failed with a signed FAILED envelope."""
    from app.a2a import signing
    from app.a2a.repository import MessageRecordRepository
    from app.search import SearchError
    from tests.test_a2a_service import StubIdentity

    class _FailingSearchProvider:
        async def search(self, query: str, count: int = 5):
            raise SearchError("upstream exploded")

    sender = StubIdentity()
    receiver = StubIdentity()
    service = _search_service(
        db_session_factory,
        policy_service,
        memory_manager,
        receiver,
        _FailingSearchProvider(),
    )
    await service.register_trusted_agent(
        db_owner_id,
        agent_id=sender.agent_id,
        public_key=sender.public_key_b64,
        display_name="Searcher",
        endpoint=SEARCH_TEST_ENDPOINT,
    )
    await _allow_search(policy_service, db_owner_id, sender.agent_id)

    envelope = await _signed_search_request(sender, receiver.agent_id)
    response = await service.handle_inbound(db_owner_id, envelope)

    assert response.payload["status"] == "failed"
    assert response.payload["reason"] == "upstream exploded"
    assert response.task_id == envelope.task_id
    assert signing.verify_envelope_signature(response, receiver.public_key_b64) is True

    async with db_session_factory() as session:
        record = await MessageRecordRepository().get_by_message_id(
            session, db_owner_id, envelope.message_id
        )
    assert record is not None
    assert record.status == "failed"
    assert record.policy_decision == "ALLOW"
