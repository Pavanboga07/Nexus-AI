"""Discovery routes: agent card hosting and remote agent discovery (Part 7).

Endpoints:
    GET  /.well-known/nexus-agent.json  — Public signed agent card
    GET  /a2a/card                      — Same card, A2A namespace
    POST /a2a/discover                  — Fetch, verify, and register a remote agent
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.a2a import signing
from app.a2a.cards import AgentCapability, build_card, capabilities_from_tools
from app.a2a.discovery import DiscoveryService
from app.a2a.errors import A2AError
from app.agent.agent import NexusAgent
from app.api.dependencies import (
    get_agent,
    get_discovery_service,
    get_identity_service,
)
from app.config.settings import Settings, get_settings
from app.identity.service import IdentityService
from app.schemas.discovery import (
    AgentCardResponse,
    DiscoverRequest,
    DiscoverResponse,
)

logger = logging.getLogger("nexus.api.discovery")

router = APIRouter()


async def _build_signed_card(
    identity_service: IdentityService,
    settings: Settings,
    tool_service=None,
) -> dict:
    """Build and sign the local agent card on-demand."""
    public = identity_service.get_public_identity()

    # Determine the endpoint URL: configured value or fallback.
    endpoint = settings.nexus_agent_endpoint
    if not endpoint:
        endpoint = f"http://{settings.nexus_host}:{settings.nexus_port}"

    # Build capabilities from the tool registry if available.
    capabilities: list[AgentCapability] = []
    if tool_service is not None:
        tools = tool_service.list_tools()
        capabilities = capabilities_from_tools(tools)

    card = build_card(
        agent_id=public.agent_id,
        public_key=public.public_key,
        display_name=settings.nexus_agent_display_name,
        endpoint=endpoint,
        capabilities=capabilities,
        supported_purposes=settings.agent_supported_purposes_list,
        ttl_seconds=settings.nexus_discovery_card_ttl_seconds,
    )
    return await signing.sign_card(identity_service, card)


@router.get(
    "/.well-known/nexus-agent.json",
    tags=["discovery"],
    summary="Public signed agent card (standard discovery path)",
    response_model=AgentCardResponse,
)
async def well_known_card(
    request: Request,
    identity_service: IdentityService = Depends(get_identity_service),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    if not identity_service.ready:
        raise HTTPException(status_code=503, detail="Agent identity is not initialised.")
    tool_service = getattr(request.app.state, "tool_service", None)
    card = await _build_signed_card(identity_service, settings, tool_service)
    return JSONResponse(content=card)


@router.get(
    "/a2a/card",
    tags=["discovery"],
    summary="Public signed agent card (A2A namespace)",
    response_model=AgentCardResponse,
)
async def a2a_card(
    request: Request,
    identity_service: IdentityService = Depends(get_identity_service),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    if not identity_service.ready:
        raise HTTPException(status_code=503, detail="Agent identity is not initialised.")
    tool_service = getattr(request.app.state, "tool_service", None)
    card = await _build_signed_card(identity_service, settings, tool_service)
    return JSONResponse(content=card)


@router.post(
    "/a2a/discover",
    response_model=DiscoverResponse,
    tags=["discovery"],
    summary="Discover and register a remote agent from its card URL",
    responses={
        400: {"description": "Invalid card"},
        401: {"description": "Card signature invalid"},
        409: {"description": "Agent already registered"},
        410: {"description": "Card expired"},
        502: {"description": "Failed to fetch remote card"},
    },
)
async def discover_agent(
    payload: DiscoverRequest,
    agent: NexusAgent = Depends(get_agent),
    discovery_service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoverResponse:
    owner_id = await agent._owner_id()
    try:
        trusted_agent, card = await discovery_service.discover_and_register(
            owner_id, payload.url, display_name=payload.display_name
        )
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return DiscoverResponse(
        agent_id=trusted_agent.agent_id,
        display_name=trusted_agent.display_name,
        endpoint=trusted_agent.endpoint,
        status=trusted_agent.status,
        card=AgentCardResponse(**card),
    )


__all__ = ["router"]
