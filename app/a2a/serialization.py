"""Canonical serialization for A2A messages.

Thin adapter over Part 3's ``canonical_json_bytes`` - there is exactly ONE
canonical serializer in Nexus (Part 6 spec §6). Envelope canonicalization
excludes the signature field: the signature never covers itself.
"""

from __future__ import annotations

from app.a2a.schemas import A2AEnvelope
from app.identity.serialization import CanonicalizationError, canonical_json_bytes


def envelope_canonical_bytes(envelope: A2AEnvelope) -> bytes:
    """Deterministic UTF-8 bytes of the unsigned envelope."""
    try:
        return canonical_json_bytes(envelope.unsigned_dict())
    except CanonicalizationError as exc:
        raise ValueError(f"Envelope is not canonically serializable: {exc}") from exc


__all__ = ["CanonicalizationError", "envelope_canonical_bytes"]
