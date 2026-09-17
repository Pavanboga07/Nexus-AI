"""Protocol conformance suite (M9).

The ultimate architectural test from the audit: can an INDEPENDENT developer
build a Nexus-compatible agent without modifying the core application?

This suite answers it mechanically:

  1. **Independence.** The SDK (`nexus-sdk/`) must import nothing from the
     runtime (`app/`). Enforced by scanning its source, not by good intentions.
  2. **Byte-level agreement.** Both implementations must reproduce the exact
     canonical bytes in `tests/vectors/conformance.json`. Two implementations
     can agree on "canonical JSON" in prose and still disagree on key order,
     whitespace, float formatting, `+00:00` vs `Z`, or absent-vs-null.
  3. **Cross-verification.** A signature produced by one implementation must
     verify in the other. That is the property that makes interop real rather
     than nominal.
  4. **Round trip.** An SDK agent can be handed an envelope produced by the
     runtime and must accept it, and vice versa.

If the runtime's canonicalization changes, the vectors no longer match and this
suite fails - which is how spec/implementation drift becomes a test failure
instead of a production incompatibility.
"""

from __future__ import annotations

import ast
import base64
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_ROOT = REPO_ROOT.parent / "nexus-sdk"
VECTORS_PATH = REPO_ROOT / "tests" / "vectors" / "conformance.json"

# Make the SDK importable without installing it: a third-party developer would
# `pip install nexus-sdk`, and the suite should not require that to pass.
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))


@pytest.fixture(scope="module")
def vectors() -> dict:
    if not VECTORS_PATH.exists():
        pytest.fail(
            f"Conformance vectors missing at {VECTORS_PATH}. "
            "Regenerate with: python scripts/generate_conformance_vectors.py"
        )
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sdk():
    """The SDK as a third-party developer would import it."""
    try:
        import nexus_sdk
    except ImportError as exc:  # pragma: no cover
        pytest.fail(f"The reference SDK is not importable: {exc}")
    return nexus_sdk


# ---------------------------------------------------------------------------
# 1. Independence: no runtime imports
# ---------------------------------------------------------------------------


def test_sdk_imports_nothing_from_the_runtime() -> None:
    """The SDK must not depend on `app` - that is the whole claim.

    Uses the AST rather than a text search so a mention inside a docstring or
    comment does not count, and a genuine import cannot hide.
    """
    assert SDK_ROOT.exists(), f"SDK not found at {SDK_ROOT}"
    offenders: list[str] = []

    for path in SDK_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root == "app":
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".")[0]
                if root == "app":
                    offenders.append(f"{path.name}: from {module} import ...")

    assert not offenders, (
        "The SDK must not import the Nexus runtime (it would not be an "
        "independent implementation):\n" + "\n".join(offenders)
    )


def test_sdk_declares_only_its_own_dependencies() -> None:
    """A third-party agent should not need this project's web framework."""
    requirements = (SDK_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for forbidden in ("fastapi", "sqlalchemy", "alembic", "pydantic-settings"):
        assert forbidden not in requirements, (
            f"the SDK must not depend on {forbidden}"
        )
    for needed in ("cryptography", "httpx"):
        assert needed in requirements


def test_sdk_can_be_used_without_the_runtime_on_the_path() -> None:
    """Import the SDK with `app` made unimportable, simulating a third party."""
    import importlib

    hidden = {}
    # Block `app` so any leaked dependency fails loudly.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            hidden[name] = sys.modules.pop(name)
    sys.modules["app"] = None  # type: ignore[assignment]
    try:
        for name in list(sys.modules):
            if name == "nexus_sdk" or name.startswith("nexus_sdk."):
                sys.modules.pop(name)
        module = importlib.import_module("nexus_sdk")
        identity = module.AgentIdentity.generate()
        assert identity.agent_id.startswith("nexus:ed25519:")
    finally:
        sys.modules.pop("app", None)
        sys.modules.update(hidden)


# ---------------------------------------------------------------------------
# 2. Byte-level canonical agreement
# ---------------------------------------------------------------------------


def test_runtime_and_sdk_agree_on_canonical_bytes(vectors, sdk) -> None:
    """Every canonical vector must be reproduced byte for byte."""
    from app.a2a.serialization import canonical_json_bytes as runtime_canonical

    assert vectors["canonical_json"], "no canonical vectors to check"
    for case in vectors["canonical_json"]:
        sample = case["input"]
        expected = bytes.fromhex(case["canonical_utf8_hex"])

        runtime_bytes = runtime_canonical(sample)
        sdk_bytes = sdk.canonical_json_bytes(sample)

        assert runtime_bytes == expected, (
            f"runtime canonicalization drifted for {sample!r}"
        )
        assert sdk_bytes == expected, (
            f"SDK canonicalization disagrees for {sample!r}:\n"
            f"  expected {expected!r}\n  got      {sdk_bytes!r}"
        )


def test_canonical_form_is_sorted_compact_and_utf8(sdk) -> None:
    """The stated rules, checked directly rather than only via vectors."""
    encoded = sdk.canonical_json_bytes({"b": 1, "a": 2})
    assert encoded == b'{"a":2,"b":1}'
    # No whitespace whatsoever.
    assert b" " not in encoded
    # Non-ASCII is emitted as UTF-8, not escaped.
    assert sdk.canonical_json_bytes({"k": "café"}) == '{"k":"café"}'.encode("utf-8")


def test_floats_are_rejected_by_both_implementations(vectors, sdk) -> None:
    """A float makes signatures non-portable, so both must refuse it."""
    from app.a2a.serialization import canonical_json_bytes as runtime_canonical

    payload = {"amount": 1.5, "nested": [{"x": 0.1}]}
    with pytest.raises(Exception):
        runtime_canonical(payload)
    with pytest.raises(sdk.CanonicalizationError):
        sdk.canonical_json_bytes(payload)
    # Integers are fine.
    sdk.canonical_json_bytes({"amount": 1})


def test_timestamps_are_second_resolution_utc(sdk) -> None:
    """`+00:00` and sub-second precision would break byte-identity."""
    stamp = sdk.utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), stamp


# ---------------------------------------------------------------------------
# 3. Cross-implementation signature verification
# ---------------------------------------------------------------------------


def test_sdk_verifies_a_runtime_signed_envelope(vectors, sdk) -> None:
    """A signature made by the runtime must verify in the SDK."""
    for case_name in ("envelope_v01", "envelope_v02"):
        case = vectors[case_name]
        envelope = sdk.Envelope.from_dict(case["envelope"])
        assert sdk.verify_envelope_signature(
            envelope, case["signer_public_key_b64"]
        ), f"{case_name}: the SDK could not verify a runtime signature"

        # And the signing bytes must match exactly, not merely verify.
        assert envelope.signing_bytes().hex() == case["signing_bytes_hex"], (
            f"{case_name}: SDK and runtime disagree on the signed bytes"
        )


def test_runtime_verifies_an_sdk_signed_envelope(vectors, sdk) -> None:
    """And the reverse: a signature made by the SDK must verify in the runtime."""
    from app.a2a.schemas import A2AEnvelope
    from app.a2a.signing import verify_envelope_signature

    case = vectors["envelope_v02"]
    identity = sdk.AgentIdentity.from_seed(
        bytes.fromhex(vectors["test_key"]["private_seed_hex"])
    )
    # Rebuild the same envelope in the SDK and sign it there.
    envelope = sdk.Envelope.from_dict(
        {**case["envelope"], "signature": None}
        if case["envelope"].get("signature") is None
        else {k: v for k, v in case["envelope"].items() if k != "signature"}
    )
    sdk_signed = identity.sign_envelope(envelope)
    assert sdk_signed.signature is not None

    runtime_envelope = A2AEnvelope.model_validate(sdk_signed.to_dict())
    assert verify_envelope_signature(runtime_envelope, identity.public_key_b64), (
        "the runtime could not verify an SDK signature"
    )

    # Tampering must break verification in both directions.
    tampered = sdk.Envelope.from_dict(
        {**sdk_signed.to_dict(), "purpose": "tampered"}
    )
    assert not sdk.verify_envelope_signature(tampered, identity.public_key_b64)
    runtime_tampered = A2AEnvelope.model_validate(tampered.to_dict())
    assert not verify_envelope_signature(
        runtime_tampered, identity.public_key_b64
    )


def test_agent_ids_agree(vectors, sdk) -> None:
    """The agent id derivation is identical, or nothing else can match."""
    from app.identity import crypto

    raw = base64.b64decode(vectors["test_key"]["public_key_b64"])
    assert sdk.agent_id_from_public_key(raw) == vectors["test_key"]["agent_id"]
    assert (
        crypto.agent_id_from_public_key(raw) == vectors["test_key"]["agent_id"]
    )


# ---------------------------------------------------------------------------
# 4. Agent cards and round trips
# ---------------------------------------------------------------------------


def test_sdk_verifies_a_runtime_signed_card(vectors, sdk) -> None:
    """Discovery interoperability: the card is how a peer pins your key."""
    case = vectors["agent_card"]
    ok, reason = sdk.verify_agent_card(case["card"])
    assert ok, f"the SDK rejected a valid runtime card: {reason}"

    # A card whose key was swapped must be rejected: the agent_id would no
    # longer be the fingerprint of the key.
    import copy

    forged = copy.deepcopy(case["card"])
    forged["public_key"] = vectors["peer_key"]["public_key_b64"]
    ok, reason = sdk.verify_agent_card(forged)
    assert not ok and reason is not None


def test_sdk_card_is_verifiable_by_the_runtime(vectors, sdk) -> None:
    """A card built by the SDK must pass the runtime's own verification."""
    from app.a2a.discovery import DiscoveryService

    identity = sdk.AgentIdentity.from_seed(
        bytes.fromhex(vectors["test_key"]["private_seed_hex"])
    )
    card = sdk.build_agent_card(
        identity,
        display_name="Third-party agent",
        endpoint="https://third.example.com",
        capabilities=[
            {
                "id": "calendar.availability",
                "version": "1.0",
                "description": "x",
                "data_category": "availability",
                "input_schema": {"type": "object"},
            }
        ],
    )

    class _NoopA2A:
        async def register_trusted_agent(self, *a, **k):  # pragma: no cover
            raise AssertionError("verification must not need to register")

    service = DiscoveryService(
        identity_service=None,  # type: ignore[arg-type]
        a2a_service=_NoopA2A(),  # type: ignore[arg-type]
    )
    verified = service.verify_card(card, expected_agent_id=identity.agent_id)
    assert verified["agent_id"] == identity.agent_id
    assert verified["capabilities"][0]["id"] == "calendar.availability"


def test_sdk_agent_accepts_a_runtime_envelope_end_to_end(vectors, sdk) -> None:
    """The full inbound path: a runtime-signed request is handled by an SDK agent.

    The runtime side is exercised for real: an ``A2AEnvelope`` is built and
    signed by the RUNTIME, then handed to the SDK agent, which must verify it
    and produce a reply the runtime's verifier would accept.
    """
    from app.a2a.schemas import A2AEnvelope
    from app.a2a.signing import sign_envelope

    class _RuntimeSigner:
        """The runtime identity, signing with the fixed conformance key."""

        def __init__(self) -> None:
            from app.identity import crypto
            from app.identity.service import PublicIdentity

            self._private = crypto.load_private_key(
                bytes.fromhex(vectors["test_key"]["private_seed_hex"])
            )
            raw = crypto.public_key_bytes(self._private.public_key())
            self.agent_id = crypto.agent_id_from_public_key(raw)
            self.public_key_b64 = base64.b64encode(raw).decode("ascii")
            self._public = PublicIdentity(
                agent_id=self.agent_id,
                public_key=self.public_key_b64,
                key_algorithm=crypto.KEY_ALGORITHM,
                fingerprint=crypto.fingerprint_from_public_key(raw),
            )

        def get_public_identity(self):
            return self._public

        async def sign(self, data: bytes) -> bytes:
            from app.identity import crypto

            return crypto.sign_bytes(self._private, data)

    import asyncio

    runtime_signer = _RuntimeSigner()
    sdk_receiver = sdk.AgentIdentity.from_seed(bytes(range(32, 64)))

    runtime_envelope = A2AEnvelope(
        message_id="msg_" + "7" * 16,
        task_id="task_" + "8" * 16,
        sender=runtime_signer.agent_id,
        recipient=sdk_receiver.agent_id,
        timestamp=sdk.utc_now_iso(),
        expires_at=sdk.utc_in(300),
        message_type="request",
        purpose="scheduling",
        payload={"action": "read_memory", "data_category": "availability"},
    )
    signed_by_runtime = asyncio.run(sign_envelope(runtime_signer, runtime_envelope))

    handled: list[str] = []
    agent = sdk.NexusAgent(
        identity=sdk_receiver,
        display_name="SDK agent",
        on_request=lambda env: handled.append(env.message_id) or {
            "status": "completed",
            "echo": env.purpose,
        },
    )
    # A valid signature is not the same as recognition: pin the runtime key.
    agent.trust(runtime_signer.agent_id, runtime_signer.public_key_b64)

    reply = agent.handle_inbound(signed_by_runtime.model_dump(exclude_none=True))
    assert reply is not None, "the SDK agent produced no reply"
    assert handled == [runtime_envelope.message_id]
    assert reply.recipient == runtime_signer.agent_id
    assert reply.task_id == runtime_envelope.task_id
    # 0.2: the reply names the exact message it answers.
    assert reply.reply_to == runtime_envelope.message_id
    assert sdk.verify_envelope_signature(reply, sdk_receiver.public_key_b64)

    # And the runtime's own verifier accepts the SDK's reply.
    from app.a2a.signing import verify_envelope_signature

    runtime_reply = A2AEnvelope.model_validate(reply.to_dict())
    assert verify_envelope_signature(
        runtime_reply, sdk_receiver.public_key_b64
    ), "the runtime could not verify the SDK's reply"


def test_sdk_rejects_untrusted_unexpired_and_replayed(sdk) -> None:
    """Verification is enforced, not decorative."""
    sender = sdk.AgentIdentity.from_seed(bytes(range(32)))
    receiver = sdk.AgentIdentity.from_seed(bytes(range(32, 64)))
    agent = sdk.NexusAgent(
        identity=receiver, display_name="receiver", on_request=lambda e: {}
    )

    envelope = sdk.Envelope.request(
        sender=sender.agent_id,
        recipient=receiver.agent_id,
        capability_id="calendar.availability",
    )
    signed = sender.sign_envelope(envelope)

    # Untrusted sender: a valid signature is not the same as recognition.
    with pytest.raises(sdk.EnvelopeError, match="UNTRUSTED_SENDER"):
        agent.handle_inbound(signed.to_dict())

    agent.trust(sender.agent_id, sender.public_key_b64)
    assert agent.handle_inbound(signed.to_dict()) is not None

    # Replay of the same message_id must be refused.
    with pytest.raises(sdk.EnvelopeError, match="REPLAY"):
        agent.handle_inbound(signed.to_dict())


def test_sdk_rejects_an_expired_envelope(sdk) -> None:
    sender = sdk.AgentIdentity.from_seed(bytes(range(32)))
    receiver = sdk.AgentIdentity.from_seed(bytes(range(32, 64)))
    agent = sdk.NexusAgent(
        identity=receiver, display_name="receiver", on_request=lambda e: {}
    )
    agent.trust(sender.agent_id, sender.public_key_b64)

    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    envelope = sdk.Envelope(
        sender=sender.agent_id,
        recipient=receiver.agent_id,
        message_type="request",
        purpose="test",
        payload={"action": "read_memory", "data_category": "custom"},
        timestamp=sdk.utc_now_iso(past),
        expires_at=sdk.utc_now_iso(past + timedelta(minutes=1)),
    )
    signed = sender.sign_envelope(envelope)
    with pytest.raises(sdk.EnvelopeError, match="EXPIRED"):
        agent.handle_inbound(signed.to_dict())


def test_sdk_rejects_an_unknown_envelope_field(sdk) -> None:
    """An unknown field inside signed bytes is a substitution risk.

    Ignoring it would mean the signer and the receiver disagree about what the
    message says, which is exactly the ambiguity an attacker wants.
    """
    sender = sdk.AgentIdentity.from_seed(bytes(range(32)))
    receiver = sdk.AgentIdentity.from_seed(bytes(range(32, 64)))
    envelope = sdk.Envelope.request(
        sender=sender.agent_id,
        recipient=receiver.agent_id,
        capability_id="calendar.availability",
    )
    signed = sender.sign_envelope(envelope).to_dict()
    signed["surprise"] = "value"
    with pytest.raises(sdk.EnvelopeError, match="Unknown envelope field"):
        sdk.Envelope.from_dict(signed)


def test_sdk_protocol_versions_match_the_runtime(sdk, vectors) -> None:
    """The advertised versions and message vocabulary must not drift."""
    from app.a2a import schemas as runtime_schemas

    assert sdk.PROTOCOL == runtime_schemas.PROTOCOL
    assert sdk.PROTOCOL_VERSION == runtime_schemas.PROTOCOL_VERSION
    assert set(sdk.SUPPORTED_PROTOCOL_VERSIONS) == set(
        runtime_schemas.SUPPORTED_PROTOCOL_VERSIONS
    )
    assert vectors["protocol_version"] == runtime_schemas.PROTOCOL_VERSION


def test_vectors_are_regenerable_and_current(sdk, vectors) -> None:
    """Stale vectors must fail loudly rather than silently passing.

    Regenerating must reproduce the SAME signatures given the fixed seed, which
    proves the vectors describe the current implementation rather than a past
    one.
    """
    identity = sdk.AgentIdentity.from_seed(
        bytes.fromhex(vectors["test_key"]["private_seed_hex"])
    )
    case = vectors["envelope_v01"]
    envelope = sdk.Envelope.from_dict(
        {k: v for k, v in case["envelope"].items() if k != "signature"}
    )
    re_signed = identity.sign_envelope(envelope)
    assert re_signed.signature == case["signature_b64"], (
        "signing the recorded envelope with the recorded key did not "
        "reproduce the recorded signature: the vectors are stale"
    )
