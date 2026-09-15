"""A2A signing and verification.

Signing goes through Part 3's IdentityService - A2A never touches private
keys (spec §7). Verification is a pure function of (public key, message,
signature) using the sender's PUBLISHED key, so the receiving side needs no
private material (Part 3 spec §15).
"""

from __future__ import annotations

import base64

from app.a2a.schemas import A2AEnvelope
from app.identity import crypto
from app.identity.service import IdentityService


async def sign_envelope(
    identity_service: IdentityService, envelope: A2AEnvelope
) -> A2AEnvelope:
    """Return a copy of ``envelope`` signed with the local agent identity."""
    signature = await identity_service.sign(envelope.canonical_bytes())
    return envelope.model_copy(
        update={"signature": base64.b64encode(signature).decode("ascii")}
    )


def verify_envelope_signature(
    envelope: A2AEnvelope, public_key_b64: str
) -> bool:
    """Verify the envelope signature against a base64 raw Ed25519 key.

    Returns False for any malformed input - never raises.
    """
    if not envelope.signature or not public_key_b64:
        return False
    try:
        public_key = crypto.load_public_key(
            base64.b64decode(public_key_b64.encode("ascii"), validate=True)
        )
        signature = base64.b64decode(
            envelope.signature.encode("ascii"), validate=True
        )
    except Exception:
        return False
    try:
        canonical = envelope.canonical_bytes()
    except ValueError:
        return False
    return crypto.verify_bytes(public_key, canonical, signature)


def agent_id_matches_key(agent_id: str, public_key_b64: str) -> bool:
    """True when the agent_id is the fingerprint of this public key."""
    try:
        raw = base64.b64decode(public_key_b64.encode("ascii"), validate=True)
        crypto.load_public_key(raw)  # reject non-key bytes
    except Exception:
        return False
    return crypto.agent_id_from_public_key(raw) == agent_id


# --- Agent Card signing (Part 7) ------------------------------------------


async def sign_card(
    identity_service: IdentityService, card: dict
) -> dict:
    """Return a copy of the card dict with an Ed25519 signature.

    The signature covers the canonical JSON of the card EXCLUDING the
    ``signature`` field, using the same serializer as A2A envelopes.
    """
    from app.identity.serialization import canonical_json_bytes

    unsigned = {k: v for k, v in card.items() if k != "signature"}
    canonical = canonical_json_bytes(unsigned)
    signature = await identity_service.sign(canonical)
    return {
        **unsigned,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_card_signature(card: dict, public_key_b64: str) -> bool:
    """Verify a card's signature against a base64 raw Ed25519 public key.

    Returns False for any malformed input - never raises.
    """
    from app.identity.serialization import canonical_json_bytes

    signature_b64 = card.get("signature")
    if not signature_b64 or not public_key_b64:
        return False
    try:
        public_key = crypto.load_public_key(
            base64.b64decode(public_key_b64.encode("ascii"), validate=True)
        )
        signature = base64.b64decode(
            signature_b64.encode("ascii"), validate=True
        )
    except Exception:
        return False
    try:
        unsigned = {k: v for k, v in card.items() if k != "signature"}
        canonical = canonical_json_bytes(unsigned)
    except (ValueError, TypeError):
        return False
    return crypto.verify_bytes(public_key, canonical, signature)


__all__ = [
    "agent_id_matches_key",
    "sign_card",
    "sign_envelope",
    "verify_card_signature",
    "verify_envelope_signature",
]

