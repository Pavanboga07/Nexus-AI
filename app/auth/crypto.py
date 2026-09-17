"""Authentication primitives: password hashing and session tokens.

Human authentication (as opposed to agent authentication, which is Ed25519
envelope signing in ``app.a2a``). Until this module existed, the API had NO
authentication at all: every endpoint was reachable by anyone who could reach
the port, with full owner authority, and authorization was self-service
(anyone could create a wildcard ALLOW policy).

Design notes
------------
* Passwords are hashed with **Argon2id** (``argon2-cffi``), the Password
  Hashing Competition winner. Parameters are explicit rather than library
  defaults so they are reviewable and stable across upgrades.
* Session tokens are **stateless and signed** (HMAC-SHA256 over a canonical
  payload) so a session survives a process restart without server-side state,
  but the token's *hash* is also recorded in the database. That gives
  server-side revocation (logout, "sign out everywhere", password change)
  without needing a session lookup on every request unless we choose to.
* Comparison uses ``hmac.compare_digest`` to avoid timing leaks.
* The signing key is separate from the identity encryption key. If it is not
  configured we derive from the identity key and log a warning, because
  reusing one secret for two purposes is a (mild) design smell we should not
  normalise.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

logger = logging.getLogger("nexus.auth.crypto")

#: Argon2id parameters. ~64 MiB / 3 iterations is a reasonable 2020s baseline
#: for an interactive login on server hardware.
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

SESSION_TOKEN_BYTES = 32
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days
SESSION_COOKIE_NAME = "nexus_session"


class AuthCryptoError(Exception):
    """Raised when hashing, signing, or verifying fails."""


# --- Passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with Argon2id. Returns the encoded hash string."""
    if not isinstance(password, str) or not password:
        raise AuthCryptoError("Password must be a non-empty string.")
    return _PASSWORD_HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time-ish verification. Returns False for any malformed input.

    Never raises: a corrupt hash or a bad password is simply "not a match".
    """
    if not stored_hash or not password:
        return False
    try:
        return _PASSWORD_HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("password_verify_failed detail=%s", type(exc).__name__)
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now use."""
    try:
        return _PASSWORD_HASHER.check_needs_rehash(stored_hash)
    except Exception:
        return False


# --- Session tokens ---------------------------------------------------------


@dataclass(frozen=True)
class SessionToken:
    """A verified session token payload."""

    user_id: uuid.UUID
    issued_at: int
    expires_at: int
    token_id: str


def generate_session_token() -> str:
    """A high-entropy opaque token; the signed payload embeds only its id.

    URL-safe base64 WITHOUT padding: the token is embedded in a cookie together
    with the signed payload, joined by ``.``, so it must not itself contain a
    ``.`` or trailing ``=`` padding (padding made the cookie value contain two
    dots, which broke parsing).
    """
    return base64.urlsafe_b64encode(os.urandom(SESSION_TOKEN_BYTES)).decode("ascii").rstrip("=")


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible fingerprint of a token, for storage/revocation.

    We store this instead of the token so a database leak does not yield
    usable sessions. SHA-256 is adequate here because the token is already
    256 bits of entropy (no brute-force or rainbow-table concern).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _derive_key(secret: str) -> bytes:
    """HKDF-ish derivation so the signing key differs from the raw secret."""
    return hashlib.sha256(f"nexus-session-v1:{secret}".encode("utf-8")).digest()


def sign_session_payload(payload: dict, *, secret: str) -> str:
    """Produce ``base64url(payload).base64url(hmac)``."""
    if not secret:
        raise AuthCryptoError("Session signing secret is empty.")
    body = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        _derive_key(secret), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64url_encode(signature)}"


def build_session_cookie_value(opaque_token: str, signed_payload: str) -> str:
    """Compose the cookie value: ``<opaque>.<signed payload>``.

    The opaque part is what identifies the session row (by fingerprint); the
    signed part carries the claims and is integrity-protected. Keeping them
    separate means revocation keys on something that never leaves our control,
    while expiry/identity can be checked without a database round trip.
    """
    return f"{opaque_token}.{signed_payload}"


def split_session_cookie_value(value: str) -> tuple[str | None, str | None]:
    """Split a cookie value into ``(opaque, signed_payload)``.

    Splits on the FIRST separator because the opaque part is padding-free
    base64url (no dots), while the signed payload contains a dot of its own.
    Returns ``(None, None)`` for anything malformed.
    """
    if not value or "." not in value:
        return None, None
    opaque, _, signed = value.partition(".")
    if not opaque or not signed:
        return None, None
    return opaque, signed


def session_fingerprint_from_cookie(value: str) -> str | None:
    """Fingerprint of the opaque half of a cookie value.

    ``AuthService`` stores the fingerprint of the opaque token, so this is the
    lookup key for revocation.
    """
    opaque, _ = split_session_cookie_value(value)
    if opaque is None:
        return None
    return token_fingerprint(opaque)


def verify_session_payload(token: str, *, secret: str) -> dict | None:
    """Verify a signed payload. Returns the dict, or None if invalid/expired.

    ``token`` may be either the signed payload alone or a full cookie value
    (``<opaque>.<signed>``); the signed half is extracted automatically.

    Never raises: every failure mode (bad format, bad base64, bad signature,
    wrong key, expired) is "not authenticated".
    """
    if not token or not secret:
        return None
    # Accept a full cookie value by taking the signed half.
    _, signed = split_session_cookie_value(token)
    candidate = signed if signed is not None else token
    if "." not in candidate:
        return None
    body, _, signature_b64 = candidate.rpartition(".")
    if not body or not signature_b64:
        return None
    try:
        expected = hmac.new(
            _derive_key(secret), body.encode("ascii"), hashlib.sha256
        ).digest()
        provided = _b64url_decode(signature_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= int(time.time()):
        return None
    return payload


def build_session_payload(
    user_id: uuid.UUID,
    *,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    now: int | None = None,
) -> dict:
    issued = int(now if now is not None else time.time())
    return {
        "sub": str(user_id),
        "iat": issued,
        "exp": issued + int(ttl_seconds),
        "jti": uuid.uuid4().hex,
    }


__all__ = [
    "AuthCryptoError",
    "DEFAULT_SESSION_TTL_SECONDS",
    "SESSION_COOKIE_NAME",
    "SessionToken",
    "build_session_cookie_value",
    "build_session_payload",
    "generate_session_token",
    "hash_password",
    "password_needs_rehash",
    "session_fingerprint_from_cookie",
    "sign_session_payload",
    "split_session_cookie_value",
    "token_fingerprint",
    "verify_password",
    "verify_session_payload",
]
