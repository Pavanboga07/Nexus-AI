"""A2A routes: trusted-agent registry, inbound messages, outbound send, audit."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app.a2a.errors import A2AError
from app.a2a.schemas import A2AEnvelope
from app.a2a.service import A2AService
from app.agent.agent import NexusAgent
from app.api.auth_context import request_owner_id
from app.api.dependencies import get_agent, get_a2a_service
from app.schemas.a2a import (
    A2AAuditEntryOut,
    A2AAuditResponse,
    A2AMessageIn,
    A2ASendRequest,
    A2ASendResponse,
    DeleteResponse,
    TrustedAgentCreate,
    TrustedAgentListResponse,
    TrustedAgentOut,
)

logger = logging.getLogger("nexus.api.a2a")

router = APIRouter()


async def _owner_id(request: Request) -> uuid.UUID:
    return request_owner_id(request)


@router.post(
    "/a2a/agents",
    response_model=TrustedAgentOut,
    status_code=status.HTTP_201_CREATED,
    tags=["a2a"],
    summary="Register a trusted remote agent",
)
async def register_agent(
    request: Request,
    payload: TrustedAgentCreate,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TrustedAgentOut:
    owner_id = await _owner_id(request)
    try:
        record = await a2a_service.register_trusted_agent(
            owner_id,
            agent_id=payload.agent_id,
            public_key=payload.public_key,
            display_name=payload.display_name,
            endpoint=payload.endpoint,
        )
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return TrustedAgentOut(**record.to_dict())  # type: ignore[arg-type]


@router.get(
    "/a2a/agents",
    response_model=TrustedAgentListResponse,
    tags=["a2a"],
    summary="List trusted agents",
)
async def list_agents(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TrustedAgentListResponse:
    owner_id = await _owner_id(request)
    agents = await a2a_service.list_trusted_agents(owner_id)
    items = [TrustedAgentOut(**a.to_dict()) for a in agents]  # type: ignore[arg-type]
    return TrustedAgentListResponse(agents=items, total=len(items))


@router.get(
    "/a2a/agents/{agent_id}",
    response_model=TrustedAgentOut,
    tags=["a2a"],
    summary="Get one trusted agent",
    responses={404: {"description": "Agent not found"}},
)
async def get_agent_record(
    request: Request,
    agent_id: str,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TrustedAgentOut:
    owner_id = await _owner_id(request)
    record = await a2a_service.get_trusted_agent(owner_id, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return TrustedAgentOut(**record.to_dict())  # type: ignore[arg-type]


@router.delete(
    "/a2a/agents/{agent_id}",
    response_model=DeleteResponse,
    tags=["a2a"],
    summary="Delete a trusted agent",
    responses={404: {"description": "Agent not found"}},
)
async def delete_agent(
    request: Request,
    agent_id: str,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> DeleteResponse:
    owner_id = await _owner_id(request)
    deleted = await a2a_service.delete_trusted_agent(owner_id, agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return DeleteResponse(deleted=True, agent_id=agent_id)


@router.post(
    "/a2a/agents/{agent_id}/revoke",
    response_model=TrustedAgentOut,
    tags=["a2a"],
    summary="Revoke a trusted agent (keeps the record, blocks communication)",
    responses={404: {"description": "Agent not found"}},
)
async def revoke_agent(
    request: Request,
    agent_id: str,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TrustedAgentOut:
    owner_id = await _owner_id(request)
    revoked = await a2a_service.revoke_trusted_agent(owner_id, agent_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return TrustedAgentOut(**revoked.to_dict())  # type: ignore[arg-type]


@router.post(
    "/a2a/messages",
    tags=["a2a"],
    summary="Inbound signed A2A message (remote agents call this)",
    description=(
        "Full verification pipeline: size -> schema -> recipient -> rate "
        "limit -> trust -> identity <-> key -> signature -> time window -> "
        "replay -> POLICY. Only policy-authorized data is returned, in a "
        "signed response envelope."
    ),
)
async def receive_message(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
):
    """Inbound peer message.

    Authenticated by the Ed25519 envelope signature plus the trusted-agent
    registry - NOT by a session. A remote agent has no account on this
    deployment, so requiring a session cookie here would make peer messaging
    impossible.

    NOTE (tracked in M6): the receiving owner is currently inferred from the
    local identity (one agent per deployment). Once the envelope carries the
    recipient's owner/agent in protocol 0.2, this must resolve the owner from
    the envelope instead.
    """
    raw = await request.body()
    try:
        a2a_service.check_size(raw)
        envelope = A2AEnvelope.model_validate_json(raw)
        owner_id = await _owner_id(request)
        response = await a2a_service.handle_inbound(owner_id, envelope)
    except A2AError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
    except Exception:
        logger.exception("a2a_unexpected_error")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL", "message": "Unexpected error."}},
        )
    return JSONResponse(status_code=200, content=response.model_dump())


@router.post(
    "/a2a/send",
    response_model=A2ASendResponse,
    tags=["a2a"],
    summary="Send a signed request to a trusted remote agent",
)
async def send_message(
    request: Request,
    payload: A2ASendRequest,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> A2ASendResponse:
    owner_id = await _owner_id(request)
    try:
        result = await a2a_service.send_request(
            owner_id,
            recipient_agent_id=payload.recipient_agent_id,
            purpose=payload.purpose,
            action=payload.action,
            data_category=payload.data_category,
            payload=payload.payload,
            endpoint=payload.endpoint,
        )
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return A2ASendResponse(**result)


@router.get(
    "/a2a/audit/list",
    response_model=A2AAuditResponse,
    tags=["a2a"],
    summary="A2A message audit trail (owner-scoped, metadata only)",
)
async def list_a2a_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> A2AAuditResponse:
    owner_id = await _owner_id(request)
    records = await a2a_service.list_audit(owner_id, limit=limit)
    entries = [A2AAuditEntryOut(**r.to_dict()) for r in records]  # type: ignore[arg-type]
    return A2AAuditResponse(messages=entries, total=len(entries))


__all__ = ["router"]
