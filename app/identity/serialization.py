"""Canonical serialization for future signed agent messages.

The same logical payload must always produce the same bytes before signing,
or signatures would be unverifiable across machines and Python versions.

Rules for ``canonical_json_bytes``:

- ``json.dumps`` with ``sort_keys=True`` (deterministic key order)
- ``separators=(",", ":")`` (no ambiguous whitespace)
- ``ensure_ascii=False`` + UTF-8 (stable text encoding)
- numbers/booleans/null pass through JSON's canonical forms
- floats are rejected: Ed25519 signs exact bytes, and float repr differs
  across platforms (1.0 vs 1) - callers should encode quantities as strings
  or ints

Non-JSON values (sets, datetimes, ...) raise ``TypeError`` rather than being
silently coerced to something unsignable.
"""

from __future__ import annotations

import json
from typing import Any


class CanonicalizationError(TypeError):
    """Raised when a payload cannot be canonically serialized."""


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize ``payload`` to deterministic UTF-8 JSON bytes."""
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(
            f"Payload is not canonically serializable: {exc}"
        ) from exc

    def _reject_floats(node: Any) -> None:
        if isinstance(node, float):
            raise CanonicalizationError(
                "Floats are not allowed in canonical payloads "
                "(platform-dependent repr); use str or int."
            )
        if isinstance(node, dict):
            for value in node.values():
                _reject_floats(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _reject_floats(value)

    _reject_floats(payload)
    return text.encode("utf-8")


__all__ = ["CanonicalizationError", "canonical_json_bytes"]
