"""HTTP routes: health, session lifecycle, and chat.

Handlers stay thin - they validate input, delegate to the agent, and translate
domain errors into HTTP responses. No LLM or storage logic lives here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import __version__
from app.agent.agent import NexusAgent
from app.agent.session import Session, SessionNotFoundError
from app.api.dependencies import get_agent
from app.config.settings import Settings, get_settings
from app.llm.base import (
    LLMConfigurationError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    HealthResponse,
    SessionCreateResponse,
    SessionResponse,
)

logger = logging.getLogger("nexus.api")

router = APIRouter()


def _to_session_response(session: Session) -> SessionResponse:
    return SessionResponse(**session.to_dict())  # type: ignore[arg-type]


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Liveness and configuration probe",
)
async def health(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Liveness probe.

    Always returns 200 while the process is alive. ``llm_configured`` reports
    whether chat is actually usable, so a missing API key is visible without
    taking the health check down.
    """
    database_ok: bool = getattr(request.app.state, "database_ok", False)
    identity_ok: bool = getattr(request.app.state, "identity_ok", False)
    tools_ok: bool = getattr(request.app.state, "tools_ok", False)
    a2a_ok: bool = getattr(request.app.state, "a2a_ok", False)
    autonomy_ok: bool = getattr(request.app.state, "autonomy_ok", False)
    gateway_ok: bool = getattr(request.app.state, "gateway_ok", False)
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.nexus_env,
        llm_provider=agent.provider_name,
        llm_configured=settings.llm_configured,
        database=database_ok,
        memory=agent.memory_enabled,
        identity=identity_ok,
        tools=tools_ok,
        a2a=a2a_ok,
        autonomy=autonomy_ok,
        gateway=gateway_ok,
    )


@router.post(
    "/sessions",
    response_model=SessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
    summary="Create a new conversation session",
)
async def create_session(
    agent: NexusAgent = Depends(get_agent),
) -> SessionCreateResponse:
    session = await agent.create_session()
    return SessionCreateResponse(session_id=session.session_id)


@router.get(
    "/sessions",
    response_model=list[str],
    tags=["sessions"],
    summary="List session IDs",
)
async def list_sessions(
    agent: NexusAgent = Depends(get_agent),
) -> list[str]:
    return await agent.list_sessions()


@router.get(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    tags=["sessions"],
    summary="Retrieve a session and its messages",
    responses={404: {"model": ErrorResponse}},
)
async def get_session(
    session_id: str,
    agent: NexusAgent = Depends(get_agent),
) -> SessionResponse:
    try:
        session = await agent.get_session(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return _to_session_response(session)


@router.delete(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    tags=["sessions"],
    summary="Clear a session's conversation history",
    description=(
        "Empties the conversation but keeps the session id valid, so the same "
        "session can continue with a clean history."
    ),
    responses={404: {"model": ErrorResponse}},
)
async def clear_session(
    session_id: str,
    agent: NexusAgent = Depends(get_agent),
) -> SessionResponse:
    try:
        session = await agent.clear_session(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return _to_session_response(session)


@router.post(
    "/chat",
    response_model=ChatResponse,
    tags=["chat"],
    summary="Send a message and receive the assistant's reply",
    responses={
        404: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def chat(
    payload: ChatRequest,
    agent: NexusAgent = Depends(get_agent),
) -> ChatResponse:
    try:
        reply = await agent.process_message(payload.session_id, payload.message)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except LLMConfigurationError as exc:
        # Provider not usable (e.g. missing key). Not the client's fault.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except LLMTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return ChatResponse(session_id=payload.session_id, response=reply)


__all__ = ["router"]
