"""Agent routes: registry, key rotation, and revocation.

M4 exists because ``agent_identities`` carried ``UniqueConstraint("owner_id")``
- one agent per owner, enforced by the database. This router is the API surface
for the multi-agent model that replaced it:

    POST   /agents                     create an agent (new keypair)
    GET    /agents                     list this owner's agents
    GET    /agents/{agent_row_id}      one agent
    PATCH  /agents/{agent_row_id}      pause / resume / revoke
    GET    /agents/{agent_row_id}/keys list keys (current + retired history)
    POST   /agents/{agent_row_id}/rotate-key   issue a new key, retire the old
    POST   /agents/{agent_row_id}/revoke-key   revoke one specific key
    GET    /agents/{agent_row_id}/card signed agent card for THIS agent
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.auth_context import request_owner_id
from app.api.dependencies import get_identity_service
from app.config.settings import Settings, get_settings
from app.identity.bound import AgentIdentity
from app.identity.service import (
    AgentNotFoundError,
    AgentNotUsableError,
    IdentityCorruptionError,
    IdentityNotReadyError,
    IdentityService,
)
from app.schemas.identity import (
    AgentCardResponse,
    AgentCreateRequest,
    AgentKeyOut,
    AgentKeyRotateResponse,
    AgentListResponse,
    AgentOut,
    AgentStatusUpdate,
    DeleteResponse,
)
from app.a2a.cards import AgentCapability, build_card, capabilities_from_tools
from app.a2a.signing import sign_card

logger = logging.getLogger("nexus.api.agents")

router = APIRouter(prefix="/agents", tags=["agents"])


def _to_out(summary) -> AgentOut:
    return AgentOut(
        id=str(summary.id),
        agent_id=summary.agent_id,
        display_name=summary.display_name,
        handle=summary.handle,
        endpoint=summary.endpoint,
        status=summary.status,
        is_primary=summary.is_primary,
        created_at=summary.created_at.isoformat() if summary.created_at else None,
    )


def _handle_identity_errors(exc: Exception) -> HTTPException:
    """Map identity-domain failures onto honest HTTP statuses.

    IdentityNotReadyError covers "no such agent" and "agent not usable" (its
    subclasses), so 404 vs 409 is decided by the concrete type.
    """
    if isinstance(exc, AgentNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AgentNotUsableError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, IdentityCorruptionError):
        # A corrupted key is an operator problem, not a client error.
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, IdentityNotReadyError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="Identity operation failed.")


@router.post(
    "/",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent with a new Ed25519 keypair",
)
async def create_agent(
    request: Request,
    payload: AgentCreateRequest,
    identity_service: IdentityService = Depends(get_identity_service),
) -> AgentOut:
    owner_id = request_owner_id(request)
    try:
        summary = await identity_service.create_agent(
            owner_id,
            display_name=payload.display_name,
            handle=payload.handle,
            endpoint=payload.endpoint,
            make_primary=payload.make_primary,
        )
    except Exception as exc:  # noqa: BLE001 - mapped below
        raise _handle_identity_errors(exc) from exc
    return _to_out(summary)


@router.get(
    "/",
    response_model=AgentListResponse,
    summary="List this owner's agents",
)
async def list_agents(
    request: Request,
    identity_service: IdentityService = Depends(get_identity_service),
) -> AgentListResponse:
    owner_id = request_owner_id(request)
    summaries = await identity_service.list_agents(owner_id)
    items = [_to_out(s) for s in summaries]
    return AgentListResponse(agents=items, total=len(items))


@router.get(
    "/{agent_row_id}",
    response_model=AgentOut,
    summary="Get one agent",
    responses={404: {"description": "Agent not found"}},
)
async def get_agent(
    request: Request,
    agent_row_id: uuid.UUID,
    identity_service: IdentityService = Depends(get_identity_service),
) -> AgentOut:
    owner_id = request_owner_id(request)
    try:
        summary = await identity_service.get_agent(owner_id, agent_row_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_identity_errors(exc) from exc
    return _to_out(summary)


@router.patch(
    "/{agent_row_id}",
    response_model=AgentOut,
    summary="Change an agent's status (active | paused | revoked)",
)
async def update_agent_status(
    request: Request,
    agent_row_id: uuid.UUID,
    payload: AgentStatusUpdate,
    identity_service: IdentityService = Depends(get_identity_service),
) -> AgentOut:
    owner_id = request_owner_id(request)
    try:
        summary = await identity_service.set_agent_status(
            owner_id, agent_row_id, payload.status
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _handle_identity_errors(exc) from exc
    return _to_out(summary)


@router.get(
    "/{agent_row_id}/keys",
    response_model=list[AgentKeyOut],
    summary="List an agent's keys (current plus retired history)",
)
async def list_agent_keys(
    request: Request,
    agent_row_id: uuid.UUID,
    identity_service: IdentityService = Depends(get_identity_service),
) -> list[AgentKeyOut]:
    owner_id = request_owner_id(request)
    try:
        keys = await identity_service.list_keys(owner_id, agent_row_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_identity_errors(exc) from exc
    return [AgentKeyOut(**k) for k in keys]


@router.post(
    "/{agent_row_id}/rotate-key",
    response_model=AgentKeyRotateResponse,
    summary="Issue a new key and retire the old one after an overlap window",
)
async def rotate_agent_key(
    request: Request,
    agent_row_id: uuid.UUID,
    identity_service: IdentityService = Depends(get_identity_service),
    settings: Settings = Depends(get_settings),
) -> AgentKeyRotateResponse:
    """Rotate an agent's key.

    The agent_id CHANGES: it is the fingerprint of the public key, so a new key
    is a new cryptographic identity. The previous key is retained and remains
    verifiable for an overlap window, which is what lets in-flight messages and
    peers that have not yet re-pinned continue to work. Peers must re-discover
    the agent afterwards.
    """
    owner_id = request_owner_id(request)
    try:
        summary = await identity_service.rotate_key(owner_id, agent_row_id)
        keys = await identity_service.list_keys(owner_id, agent_row_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_identity_errors(exc) from exc

    logger.warning(
        "agent_key_rotated_via_api agent_row=%s new_agent_id=%s",
        agent_row_id,
        summary.agent_id,
    )
    return AgentKeyRotateResponse(
        agent=_to_out(summary),
        keys=[AgentKeyOut(**k) for k in keys],
        note=(
            "The agent_id changed because it is derived from the public key. "
            "The previous key remains valid for verification during the "
            "overlap window; peers must re-discover this agent to pin the new "
            "identity."
        ),
    )


@router.post(
    "/{agent_row_id}/revoke-key",
    response_model=DeleteResponse,
    summary="Revoke one key immediately",
)
async def revoke_agent_key(
    request: Request,
    agent_row_id: uuid.UUID,
    agent_id: str,
    identity_service: IdentityService = Depends(get_identity_service),
) -> DeleteResponse:
    owner_id = request_owner_id(request)
    try:
        await identity_service.revoke_key(
            owner_id, agent_row_id, agent_id=agent_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle_identity_errors(exc) from exc
    return DeleteResponse(deleted=True, agent_id=agent_id)


@router.get(
    "/{agent_row_id}/card",
    response_model=AgentCardResponse,
    summary="Signed agent card for THIS agent",
)
async def get_agent_card_for(
    request: Request,
    agent_row_id: uuid.UUID,
    identity_service: IdentityService = Depends(get_identity_service),
    settings: Settings = Depends(get_settings),
) -> AgentCardResponse:
    """Build and sign a card for a specific agent.

    Distinct from the public /a2a/card, which serves the deployment's primary
    agent to peers. This one lets an owner inspect any of their agents.
    """
    owner_id = request_owner_id(request)
    try:
        summary = await identity_service.get_agent(owner_id, agent_row_id)
        bound = await AgentIdentity.for_agent(
            identity_service=identity_service, agent_row_id=agent_row_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle_identity_errors(exc) from exc

    tool_service = getattr(request.app.state, "tool_service", None)
    capabilities: list[AgentCapability] = []
    if tool_service is not None:
        capabilities = capabilities_from_tools(tool_service.list_tools())

    endpoint = summary.endpoint or settings.nexus_agent_endpoint or (
        f"http://{settings.nexus_host}:{settings.nexus_port}"
    )
    public = bound.get_public_identity()
    card = build_card(
        agent_id=public.agent_id,
        public_key=public.public_key,
        display_name=summary.display_name,
        endpoint=endpoint,
        capabilities=capabilities,
        supported_purposes=settings.agent_supported_purposes_list,
        ttl_seconds=settings.nexus_discovery_card_ttl_seconds,
    )
    if summary.handle:
        card["handle"] = summary.handle
    signed = await sign_card(bound, card)
    return AgentCardResponse(**signed)


__all__ = ["router"]
