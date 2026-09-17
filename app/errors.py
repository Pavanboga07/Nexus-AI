"""The unified Nexus error model.

Before M5 the codebase had four unrelated error shapes:

    A2AError(code, message)         with its own HTTP mapping table
    ToolError(code, message)        with its own code enum
    PolicyServiceError(message)     no code at all
    HTTPException(...)              raised ad hoc from routes

Each route then translated between them, so the same condition could surface as
a different status and a different body depending on which layer caught it. In
particular ``get_identity_service`` raised a bare ``RuntimeError``, which the
catch-all handler turned into a 500 instead of a 503 - the real cause was
hidden behind "An unexpected error occurred."

This module gives every domain error ONE shape:

    NexusError(code, message, http_status, details)

with:

  * a stable machine-readable ``code`` (the API contract),
  * a ``status_code`` (the transport concern),
  * a ``to_dict()`` used by the exception handler so every error response has
    the same envelope,
  * and a ``user_message`` that is safe to show (never a stack trace, key, or
    internal path).

Existing error types (``A2AError``, ``ToolError``, ``PolicyServiceError``) are
kept as SUBCLASSES so no caller has to change behaviour, but they now inherit
one envelope - which is what makes the responses consistent without a
flag-day refactor.
"""

from __future__ import annotations

import enum
from typing import Any

#: Stable, machine-readable error codes. These are API contract: clients may
#: branch on them, so values must not change casually.
CODE_INTERNAL = "internal_error"
CODE_VALIDATION = "validation_error"
CODE_UNAUTHENTICATED = "unauthenticated"
CODE_FORBIDDEN = "forbidden"
CODE_NOT_FOUND = "not_found"
CODE_CONFLICT = "conflict"
CODE_RATE_LIMITED = "rate_limited"
CODE_DEPENDENCY_UNAVAILABLE = "service_unavailable"
CODE_UPSTREAM = "upstream_error"
CODE_TIMEOUT = "timeout"

#: Transport-level codes with no domain class of their own.
#:
#: These exist because a framework `HTTPException` carries a status and nothing
#: else, so the status-to-code map in `app.main` has to name a code for it. They
#: are declared here, with the rest of the vocabulary, so the map cannot invent a
#: code a client was never told about - which is a bug that only shows up in
#: somebody's `switch` statement.
CODE_BAD_REQUEST = "bad_request"
CODE_METHOD_NOT_ALLOWED = "method_not_allowed"
CODE_GONE = "gone"
CODE_PAYLOAD_TOO_LARGE = "payload_too_large"
#: The last resort, when a status has no more specific code. Deliberately ugly
#: so it is obvious in a log that a case is unmapped.
CODE_HTTP_ERROR = "http_error"


class ErrorKind(str, enum.Enum):
    """Coarse classification, used for metrics and for choosing a status."""

    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    DEPENDENCY = "dependency"
    UPSTREAM = "upstream"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


#: Default HTTP status per kind. Concrete errors may override.
_KIND_STATUS: dict[ErrorKind, int] = {
    ErrorKind.VALIDATION: 400,
    ErrorKind.AUTHENTICATION: 401,
    ErrorKind.AUTHORIZATION: 403,
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.CONFLICT: 409,
    ErrorKind.RATE_LIMIT: 429,
    ErrorKind.DEPENDENCY: 503,
    ErrorKind.UPSTREAM: 502,
    ErrorKind.TIMEOUT: 504,
    ErrorKind.INTERNAL: 500,
}


class NexusError(Exception):
    """Base class for every expected, reportable failure."""

    #: Overridden by subclasses that want a fixed kind.
    kind: ErrorKind = ErrorKind.INTERNAL
    #: Overridden by subclasses that want a fixed code.
    code: str = CODE_INTERNAL

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        kind: ErrorKind | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if kind is not None:
            self.kind = kind
        self.http_status = (
            http_status
            if http_status is not None
            else _KIND_STATUS.get(self.kind, 500)
        )
        #: Structured extras safe to return to a client (never secrets).
        self.details = details or {}

    @property
    def user_message(self) -> str:
        """Message safe to show a user. Overridden where a friendlier text is
        warranted; defaults to the message, which is written to be safe."""
        return self.message

    def to_dict(self) -> dict[str, Any]:
        """The single error envelope.

        Shape is uniform across every domain error:

            {"error": {"code": ..., "message": ..., "kind": ..., "details": {...}}}
        """
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.user_message,
            "kind": self.kind.value,
        }
        if self.details:
            body["details"] = self.details
        return {"error": body}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


# --- Concrete families -------------------------------------------------------


class ValidationError(NexusError):
    kind = ErrorKind.VALIDATION
    code = CODE_VALIDATION


class AuthenticationError(NexusError):
    kind = ErrorKind.AUTHENTICATION
    code = CODE_UNAUTHENTICATED


class AuthorizationError(NexusError):
    kind = ErrorKind.AUTHORIZATION
    code = CODE_FORBIDDEN


class NotFoundError(NexusError):
    kind = ErrorKind.NOT_FOUND
    code = CODE_NOT_FOUND


class ConflictError(NexusError):
    kind = ErrorKind.CONFLICT
    code = CODE_CONFLICT


class RateLimitedError(NexusError):
    kind = ErrorKind.RATE_LIMIT
    code = CODE_RATE_LIMITED


class DependencyUnavailableError(NexusError):
    """A required subsystem is missing (no database, no identity, ...).

    This is the class that replaces the bare ``RuntimeError`` which the
    catch-all handler used to turn into a 500.
    """

    kind = ErrorKind.DEPENDENCY
    code = CODE_DEPENDENCY_UNAVAILABLE


class UpstreamError(NexusError):
    kind = ErrorKind.UPSTREAM
    code = CODE_UPSTREAM


class TimeoutError_(NexusError):
    """Named with a trailing underscore to avoid shadowing the builtin."""

    kind = ErrorKind.TIMEOUT
    code = CODE_TIMEOUT


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "CODE_BAD_REQUEST",
    "CODE_CONFLICT",
    "CODE_DEPENDENCY_UNAVAILABLE",
    "CODE_FORBIDDEN",
    "CODE_GONE",
    "CODE_HTTP_ERROR",
    "CODE_INTERNAL",
    "CODE_METHOD_NOT_ALLOWED",
    "CODE_NOT_FOUND",
    "CODE_PAYLOAD_TOO_LARGE",
    "CODE_RATE_LIMITED",
    "CODE_TIMEOUT",
    "CODE_UNAUTHENTICATED",
    "CODE_UPSTREAM",
    "CODE_VALIDATION",
    "ConflictError",
    "DependencyUnavailableError",
    "ErrorKind",
    "NexusError",
    "NotFoundError",
    "RateLimitedError",
    "TimeoutError_",
    "UpstreamError",
    "ValidationError",
]
