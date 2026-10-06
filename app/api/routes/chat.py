"""HTTP routes: health, session lifecycle, and chat.

Handlers stay thin - they validate input, delegate to the agent, and translate
domain errors into HTTP responses. No LLM or storage logic lives here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.agent.agent import NexusAgent
from app.agent.session import Session, SessionNotFoundError
from app.api.auth_context import RequestContext, get_request_context
from app.api.dependencies import get_agent
from app.llm.base import (
    LLMConfigurationError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    SessionCreateResponse,
    SessionResponse,
)

logger = logging.getLogger("nexus.api")

router = APIRouter()


def _to_session_response(session: Session) -> SessionResponse:
    return SessionResponse(**session.to_dict())


# NOTE: /health moved to app/api/routes/system.py so it can be mounted WITHOUT
# the authentication dependency. A liveness probe that requires a session
# cannot be used by a load balancer or orchestrator.


@router.post(
    "/sessions",
    response_model=SessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
    summary="Create a new conversation session",
)
async def create_session(
    ctx: RequestContext = Depends(get_request_context),
    agent: NexusAgent = Depends(get_agent),
) -> SessionCreateResponse:
    session = await agent.create_session(ctx.owner_id)
    return SessionCreateResponse(session_id=session.session_id)


@router.get(
    "/sessions",
    response_model=list[str],
    tags=["sessions"],
    summary="List session IDs",
)
async def list_sessions(
    ctx: RequestContext = Depends(get_request_context),
    agent: NexusAgent = Depends(get_agent),
) -> list[str]:
    return await agent.list_sessions(ctx.owner_id)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    tags=["sessions"],
    summary="Retrieve a session and its messages",
    responses={404: {"model": ErrorResponse}},
)
async def get_session(
    session_id: str,
    ctx: RequestContext = Depends(get_request_context),
    agent: NexusAgent = Depends(get_agent),
) -> SessionResponse:
    try:
        session = await agent.get_session(ctx.owner_id, session_id)
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
    ctx: RequestContext = Depends(get_request_context),
    agent: NexusAgent = Depends(get_agent),
) -> SessionResponse:
    try:
        session = await agent.clear_session(ctx.owner_id, session_id)
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
    request: Request,
    payload: ChatRequest,
    ctx: RequestContext = Depends(get_request_context),
    agent: NexusAgent = Depends(get_agent),
) -> ChatResponse:
    owner_id = ctx.owner_id
    # Ensure the session exists first (and belongs to THIS owner).
    try:
        await agent.get_session(owner_id, payload.session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    # Check if this is a natural language agent orchestration request (Part 12)
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is not None:
        try:
            orch_res = await orchestrator.handle_user_message(
                owner_id=owner_id,
                session_id=payload.session_id,
                message=payload.message,
            )
            if orch_res is not None:
                # Orchestrator handled it; record in session history and return.
                # The route records the exchange through the agent's public
                # API rather than reaching into agent._sessions /
                # agent._schedule_extraction (M5).
                await agent.record_exchange(
                    owner_id,
                    payload.session_id,
                    payload.message,
                    orch_res.message,
                )
                return ChatResponse(session_id=payload.session_id, response=orch_res.message)
        except Exception as exc:
            logger.warning("orchestrator_execution_error: %s", exc, exc_info=True)

    try:
        reply = await agent.process_message(owner_id, payload.session_id, payload.message)
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
