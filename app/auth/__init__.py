"""Authentication subsystem: password hashing, sessions, and resolution."""

from app.auth.crypto import (
    DEFAULT_SESSION_TTL_SECONDS,
    SESSION_COOKIE_NAME,
    AuthCryptoError,
    build_session_payload,
    hash_password,
    sign_session_payload,
    token_fingerprint,
    verify_password,
    verify_session_payload,
)
from app.auth.models import AuthSession, UserCredential
from app.auth.service import (
    AuthenticatedUser,
    AuthError,
    AuthService,
    EmailAlreadyRegisteredError,
    IssuedSession,
    RegistrationError,
)

__all__ = [
    "AuthCryptoError",
    "AuthError",
    "AuthService",
    "AuthSession",
    "AuthenticatedUser",
    "DEFAULT_SESSION_TTL_SECONDS",
    "EmailAlreadyRegisteredError",
    "IssuedSession",
    "RegistrationError",
    "SESSION_COOKIE_NAME",
    "UserCredential",
    "build_session_payload",
    "hash_password",
    "sign_session_payload",
    "token_fingerprint",
    "verify_password",
    "verify_session_payload",
]
