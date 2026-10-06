"""Auth routes: register, login, logout, session status, password change."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.auth_context import (
    RequestContext,
    auth_required,
    extract_session_token,
    get_auth_service,
    require_authenticated_context,
)
from app.auth.crypto import SESSION_COOKIE_NAME
from app.auth.service import AuthError, AuthService
from app.schemas.auth import (
    AuthStatusOut,
    AuthUserOut,
    ChangePasswordRequest,
    LoginRequest,
    LogoutOut,
    MessageOut,
    RegisterRequest,
    SessionOut,
)

logger = logging.getLogger("nexus.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _set_session_cookie(
    response: Response, request: Request, token: str, *, max_age: int
) -> None:
    secure = bool(getattr(request.app.state, "cookie_secure", True))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,          # not readable from JavaScript
        secure=secure,          # HTTPS-only outside development
        samesite="lax",         # blocks cross-site POST CSRF
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    secure = bool(getattr(request.app.state, "cookie_secure", True))
    response.delete_cookie(
        key=SESSION_COOKIE_NAME, path="/", httponly=True, secure=secure, samesite="lax"
    )


@router.get(
    "/status",
    response_model=AuthStatusOut,
    summary="Whether auth is enforced and whether this caller is signed in",
)
async def auth_status(request: Request) -> AuthStatusOut:
    """Always 200: the client uses this to decide what to render.

    Never leaks whether a given email exists.
    """
    service: AuthService | None = getattr(request.app.state, "auth_service", None)
    enforced = auth_required(request)
    user_out: AuthUserOut | None = None

    token = extract_session_token(request)
    if service is not None and token:
        try:
            resolved = await service.resolve_session(token)
        except Exception:
            resolved = None
        if resolved is not None:
            user_out = AuthUserOut(
                owner_id=str(resolved.owner_id),
                email=resolved.identifier,
                display_name=None,
            )

    return AuthStatusOut(
        authenticated=user_out is not None,
        auth_required=enforced,
        registration_open=bool(
            getattr(request.app.state, "registration_open", True)
        ),
        user=user_out,
    )


@router.post(
    "/register",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and start a session",
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> SessionOut:
    # If this deployment predates auth, the single existing owner (with all its
    # memory, identity and policies) is adopted by the first registrant rather
    # than orphaned.
    adopt: object = None
    if getattr(request.app.state, "adopt_legacy_owner", False):
        adopt = await service.first_unclaimed_owner_id()

    try:
        issued = await service.register(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            adopt_existing_owner_id=adopt,  # type: ignore[arg-type]
            user_agent=request.headers.get("User-Agent"),
            ip_address=_client_ip(request),
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    _set_session_cookie(
        response,
        request,
        issued.token,
        max_age=int(getattr(request.app.state, "session_ttl_seconds", 1209600)),
    )
    if getattr(request.app.state, "adopt_legacy_owner", False):
        # Only the first registration may adopt; clear the flag.
        request.app.state.adopt_legacy_owner = False

    return SessionOut(
        token=issued.token,
        user=AuthUserOut(
            owner_id=str(issued.owner_id),
            email=payload.email.strip().lower(),
            display_name=payload.display_name,
        ),
        expires_at=issued.expires_at.isoformat(),
    )


@router.post(
    "/login",
    response_model=SessionOut,
    summary="Start a session with email and password",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> SessionOut:
    try:
        issued = await service.login(
            email=payload.email,
            password=payload.password,
            user_agent=request.headers.get("User-Agent"),
            ip_address=_client_ip(request),
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    _set_session_cookie(
        response,
        request,
        issued.token,
        max_age=int(getattr(request.app.state, "session_ttl_seconds", 1209600)),
    )
    resolved = await service.resolve_session(issued.token)
    return SessionOut(
        token=issued.token,
        user=AuthUserOut(
            owner_id=str(issued.owner_id),
            email=(resolved.identifier if resolved else payload.email),
            display_name=None,
        ),
        expires_at=issued.expires_at.isoformat(),
    )


@router.post(
    "/logout",
    response_model=LogoutOut,
    summary="Revoke the current session",
)
async def logout(
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> LogoutOut:
    token = extract_session_token(request)
    revoked = await service.logout(token) if token else False
    _clear_session_cookie(response, request)
    return LogoutOut(logged_out=revoked)


@router.get(
    "/me",
    response_model=AuthUserOut,
    summary="The authenticated account",
)
async def me(ctx: RequestContext = Depends(require_authenticated_context)) -> AuthUserOut:
    return AuthUserOut(
        owner_id=str(ctx.owner_id), email=ctx.identifier, display_name=None
    )


@router.post(
    "/password",
    response_model=MessageOut,
    summary="Change the password and sign out all sessions",
)
async def change_password(
    payload: ChangePasswordRequest,
    ctx: RequestContext = Depends(require_authenticated_context),
    service: AuthService = Depends(get_auth_service),
) -> MessageOut:
    try:
        await service.change_password(
            ctx.owner_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return MessageOut(
        message="Password changed. All sessions have been signed out; sign in again."
    )


__all__ = ["router"]
