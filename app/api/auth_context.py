"""Per-request identity resolution.

The single most consequential fix in the project. Before this module the owner
was resolved ONCE at startup from the first row in ``owners`` and cached for
the process lifetime (``OwnerRepository.get_or_create_default``), which made
every endpoint serve exactly one principal:

  * no user could be distinguished from any other,
  * authorization had nothing to authorize *against*, and
  * creating a second owner did not make the running process serve it.

Now every request resolves its own principal from a session cookie (or a
bearer token for non-browser clients). Services receive that owner id as an
argument, so nothing is process-global.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.auth.crypto import SESSION_COOKIE_NAME
from app.auth.service import AuthenticatedUser, AuthService

logger = logging.getLogger("nexus.api.auth")

#: Non-browser clients may send `Authorization: Bearer <session token>`.
BEARER_PREFIX = "bearer "


class AuthenticationRequiredError(HTTPException):
    def __init__(self, detail: str = "Authentication required.") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass(frozen=True)
class RequestContext:
    """Everything downstream needs to know about the caller."""

    owner_id: uuid.UUID
    identifier: str
    session_id: uuid.UUID | None
    #: True when the deployment is running without authentication (development
    #: only). Callers that must never operate unauthenticated check this.
    is_anonymous_dev: bool = False

    @property
    def email(self) -> str:
        return self.identifier


def get_auth_service(request: Request) -> AuthService:
    service: AuthService | None = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is unavailable (database required).",
        )
    return service


def extract_session_token(request: Request) -> str | None:
    """Session cookie first, then a bearer header."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith(BEARER_PREFIX):
        return header[len(BEARER_PREFIX):].strip() or None
    return None


def auth_required(request: Request) -> bool:
    """Whether this deployment enforces authentication."""
    return bool(getattr(request.app.state, "auth_required", False))


async def _resolve_optional(request: Request) -> AuthenticatedUser | None:
    token = extract_session_token(request)
    if not token:
        return None
    service: AuthService | None = getattr(request.app.state, "auth_service", None)
    if service is None or not service.configured:
        return None
    try:
        return await service.resolve_session(token)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("session_resolution_failed detail=%s", type(exc).__name__)
        return None


async def _legacy_dev_owner_id(request: Request) -> uuid.UUID | None:
    """Owner id for unauthenticated development.

    Returns the pre-auth single owner row if one exists. This keeps the
    local/dev experience working (and the existing test suite meaningful)
    without pretending the request was authenticated.
    """
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        return None
    try:
        return await agent.owner_id()
    except Exception:
        return None


async def get_request_context(request: Request) -> RequestContext:
    """Resolve the caller. Raises 401 when auth is enforced and absent.

    Also publishes the resolved context on ``request.state`` so handlers can
    read it without threading another parameter through every signature (see
    :func:`request_owner_id`).
    """
    user = await _resolve_optional(request)

    if user is not None:
        ctx = RequestContext(
            owner_id=user.owner_id,
            identifier=user.identifier or "",
            session_id=user.session_id,
            is_anonymous_dev=False,
        )
        _publish(request, ctx)
        return ctx

    if auth_required(request):
        raise AuthenticationRequiredError()

    # Authentication disabled (development): fall back to the legacy single
    # owner so the app remains usable, but flag it clearly so nothing mistakes
    # this for an authenticated caller.
    owner_id = await _legacy_dev_owner_id(request)
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No owner context available; sign in or configure a database.",
        )
    ctx = RequestContext(
        owner_id=owner_id,
        identifier="",
        session_id=None,
        is_anonymous_dev=True,
    )
    _publish(request, ctx)
    return ctx


#: Where the resolved context is stashed, so handlers and helpers can read the
#: acting owner without an extra Depends parameter in every signature.
_REQUEST_STATE_KEY = "nexus_request_context"


def _publish(request: Request, ctx: RequestContext) -> None:
    try:
        setattr(request.state, _REQUEST_STATE_KEY, ctx)
    except Exception:  # pragma: no cover - state is always settable
        pass


def get_published_context(request: Request) -> RequestContext | None:
    return getattr(request.state, _REQUEST_STATE_KEY, None)


def request_owner_id(request: Request) -> uuid.UUID:
    """The acting owner for this request.

    Raises 401 when no context has been resolved, which happens only if a
    route was mounted without the auth dependency - a bug we want to surface
    loudly rather than paper over with a default owner.
    """
    ctx = get_published_context(request)
    if ctx is None:
        raise AuthenticationRequiredError(
            "No request context: this route is not authenticated."
        )
    return ctx.owner_id


async def require_authenticated_context(request: Request) -> RequestContext:
    """Like :func:`get_request_context` but rejects the anonymous-dev fallback.

    Used by endpoints that must never be driven without a real principal
    (credential management, delegation grants, destructive operations).
    """
    ctx = await get_request_context(request)
    if ctx.is_anonymous_dev:
        raise AuthenticationRequiredError(
            "This endpoint requires an authenticated account."
        )
    return ctx


__all__ = [
    "AuthenticationRequiredError",
    "RequestContext",
    "auth_required",
    "extract_session_token",
    "get_auth_service",
    "get_published_context",
    "get_request_context",
    "request_owner_id",
    "require_authenticated_context",
]
