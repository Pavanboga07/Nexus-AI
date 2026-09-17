"""Generate the protocol conformance vectors.

Why vectors, not prose
----------------------
Two implementations can agree on "canonical JSON" in a document and still
produce different bytes - key ordering, whitespace, float formatting, `+00:00`
vs `Z`, absent-vs-null. A test vector is the only thing that actually pins it.

These vectors are generated from the RUNTIME's implementation and checked
against the SDK (and any future third-party implementation) by the conformance
suite. Regenerating them after a protocol change, and seeing the SDK fail until
it is updated, is exactly the signal we want - it is how spec/implementation
drift becomes a test failure instead of a production incompatibility.

Run:  python scripts/generate_conformance_vectors.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.a2a.capabilities import CapabilitySpec  # noqa: E402
from app.a2a.cards import AgentCapability, build_card  # noqa: E402
from app.a2a.schemas import (  # noqa: E402
    A2AEnvelope,
    PROTOCOL_VERSION,
    CapabilityRef,
)
from app.a2a.signing import sign_card, sign_envelope  # noqa: E402
from app.identity import crypto  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors"
#: A FIXED seed so the vectors are reproducible. This is a test key; it must
#: never be used for anything real, and it is deliberately not a secret.
TEST_SEED = bytes(range(32))


class _FixedSigner:
    """Signs with the fixed test key, exposing the IdentityService surface."""

    def __init__(self) -> None:
        self._private = crypto.load_private_key(TEST_SEED)
        self._public_raw = crypto.public_key_bytes(self._private.public_key())

    def get_public_identity(self):
        from app.identity.service import PublicIdentity

        return PublicIdentity(
            agent_id=crypto.agent_id_from_public_key(self._public_raw),
            public_key=base64.b64encode(self._public_raw).decode("ascii"),
            key_algorithm=crypto.KEY_ALGORITHM,
            fingerprint=crypto.fingerprint_from_public_key(self._public_raw),
        )

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private, data)


async def build() -> dict:
    signer = _FixedSigner()
    ident = signer.get_public_identity()
    other_seed = bytes(range(32, 64))
    other_private = crypto.load_private_key(other_seed)
    other_raw = crypto.public_key_bytes(other_private.public_key())
    other_id = crypto.agent_id_from_public_key(other_raw)

    # --- Canonicalization vectors ----------------------------------------
    canonical_samples = [
        {"b": 1, "a": 2},
        {"nested": {"z": [1, 2, 3], "a": {"k": "v"}}},
        {"unicode": "café ☕", "emoji": "🔐"},
        {"nulls": {"present": None, "number": 0, "empty": ""}},
        {"integers_only": {"big": 2**53 - 1, "negative": -1}},
    ]

    # --- Envelope vectors -------------------------------------------------
    envelope = A2AEnvelope(
        message_id="msg_" + "1" * 16,
        task_id="task_" + "2" * 16,
        sender=ident.agent_id,
        recipient=other_id,
        timestamp="2026-09-20T12:00:00Z",
        expires_at="2026-09-20T12:05:00Z",
        message_type="request",
        purpose="scheduling",
        payload={"action": "read_memory", "data_category": "availability"},
    )
    signed_envelope = await sign_envelope(signer, envelope)

    envelope_v02 = A2AEnvelope(
        message_id="msg_" + "3" * 16,
        task_id="task_" + "4" * 16,
        sender=ident.agent_id,
        recipient=other_id,
        timestamp="2026-09-20T12:00:00Z",
        expires_at="2026-09-20T12:05:00Z",
        message_type="task_progress",
        purpose="scheduling",
        payload={"action": "read_memory", "data_category": "availability"},
        correlation_id="cor_" + "5" * 16,
        reply_to="msg_" + "1" * 16,
        capability=CapabilityRef(id="calendar.availability", version="1.0"),
        authorization={"kind": "delegation_grant", "reference": "grant_1"},
        trace={"trace_id": "a" * 32, "span_id": "b" * 16},
    )
    signed_envelope_v02 = await sign_envelope(signer, envelope_v02)

    # --- Card vector ------------------------------------------------------
    card_cap = AgentCapability(
        name="calendar.availability",
        description="Check availability",
        data_category="availability",
        id="calendar.availability",
        version="1.0",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    card = build_card(
        agent_id=ident.agent_id,
        public_key=ident.public_key,
        display_name="Test Agent",
        endpoint="https://agent.example.com",
        capabilities=[card_cap],
        supported_purposes=["scheduling"],
        # A LONG window, deliberately. An earlier 1-hour TTL kept the vector
        # "verifiable" only until someone ran the suite an hour later, at which
        # point `test_sdk_verifies_a_runtime_signed_card` failed with "Card has
        # expired" - a red suite caused by the clock, not by a regression. A
        # conformance vector is a durable artifact and must not decay.
        #
        # Expiry handling is still covered: the runtime's card verifier is
        # exercised against explicit expired/not-yet-valid cases elsewhere, so
        # this does not reduce coverage - it removes an accidental time bomb.
        ttl_seconds=365 * 24 * 3600,
    )
    signed_card = await sign_card(signer, card)

    # Envelope timestamps are pinned so their signatures are stable across
    # runs; the canonical BYTES do not depend on wall-clock time, only on the
    # strings recorded here.
    return {
        "format": "nexus-conformance-vectors",
        "version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "generated_by": "nexus/scripts/generate_conformance_vectors.py",
        "notes": [
            "Generated from the runtime implementation. A third-party "
            "implementation is conformant when it reproduces every byte and "
            "verifies every signature here.",
            "TEST_SEED is bytes(range(32)) - a test key, never use it for real.",
        ],
        "test_key": {
            "private_seed_hex": TEST_SEED.hex(),
            "agent_id": ident.agent_id,
            "public_key_b64": ident.public_key,
        },
        "peer_key": {
            "private_seed_hex": other_seed.hex(),
            "agent_id": other_id,
            "public_key_b64": base64.b64encode(other_raw).decode("ascii"),
        },
        "canonical_json": [
            {
                "input": sample,
                "canonical_utf8_hex": crypto_canonical_hex(sample),
            }
            for sample in canonical_samples
        ],
        "envelope_v01": {
            "envelope": signed_envelope.model_dump(exclude_none=True),
            "signing_bytes_hex": signed_envelope.canonical_bytes().hex(),
            "signature_b64": signed_envelope.signature,
            "signer_public_key_b64": ident.public_key,
        },
        "envelope_v02": {
            "envelope": signed_envelope_v02.model_dump(exclude_none=True),
            "signing_bytes_hex": signed_envelope_v02.canonical_bytes().hex(),
            "signature_b64": signed_envelope_v02.signature,
            "signer_public_key_b64": ident.public_key,
        },
        "agent_card": {
            "card": signed_card,
            "signing_bytes_hex": canonical_card_hex(signed_card),
            "signature_b64": signed_card["signature"],
        },
        "expected_rejections": [
            {
                "case": "float in signed payload",
                "input": {"value": 0.5},
                "error": "CanonicalizationError",
            },
            {
                "case": "unknown envelope field",
                "input": {"unknown_field": True},
                "error": "validation",
            },
        ],
    }


def crypto_canonical_hex(value) -> str:
    from app.a2a.serialization import canonical_json_bytes

    return canonical_json_bytes(value).hex()


def canonical_card_hex(card: dict) -> str:
    """Canonical bytes of a card minus its signature field."""
    from app.a2a.serialization import canonical_json_bytes

    return canonical_json_bytes(
        {k: v for k, v in card.items() if k != "signature"}
    ).hex()


def main() -> int:
    import asyncio

    OUT.mkdir(parents=True, exist_ok=True)
    vectors = asyncio.run(build())
    target = OUT / "conformance.json"
    target.write_text(
        json.dumps(vectors, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {target}")
    print(f"  protocol   : {vectors['protocol_version']}")
    print(f"  test agent : {vectors['test_key']['agent_id']}")
    print(f"  canonical  : {len(vectors['canonical_json'])} vectors")
    print(f"  envelopes  : 0.1 + 0.2 signed")
    print(f"  card       : signed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
