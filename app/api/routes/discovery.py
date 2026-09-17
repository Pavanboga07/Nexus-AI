"""Discovery routes: agent card hosting and remote agent discovery (Part 7).

Endpoints:
    GET  /.well-known/nexus-agent.json  — Public signed agent card
    GET  /a2a/card                      — Same card, A2A namespace
    POST /a2a/discover                  — Fetch, verify, and register a remote agent
    GET  /a2a/directory/search          — Search the gateway directory

Authentication split
---------------------
The two card endpoints are PUBLIC on purpose: they carry no owner data, and a
remote peer must be able to fetch our identity in order to decide whether to
talk to us. Requiring an account to read a public key would make the agent
undiscoverable.

The two stateful endpoints (``/a2a/discover`` writes a trusted-agent record,
``/a2a/directory/search`` proxies outbound) carry an explicit
``get_request_context`` dependency instead of inheriting one from the router,
because those dependencies must not apply to the public card endpoints.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.a2a import signing
from app.a2a.cards import (
    AgentCapability,
    build_card,
    capabilities_from_specs,
    capabilities_from_tools,
)
from app.a2a.discovery import DiscoveryService
from app.a2a.errors import A2AError
from app.a2a.transport import validate_endpoint
from app.agent.agent import NexusAgent
from app.api.auth_context import (
    RequestContext,
    get_request_context,
    request_owner_id,
)
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
    a2a_service=None,
) -> dict:
    """Build and sign the local agent card on-demand.

    Capabilities are advertised from the A2A capability contracts (typed, with
    input/output schemas) and additionally from registered tools, so a calling
    agent can construct a valid request instead of guessing.
    """
    public = identity_service.get_public_identity()

    # Determine the endpoint URL: configured value or fallback.
    endpoint = settings.nexus_agent_endpoint
    if not endpoint:
        endpoint = f"http://{settings.nexus_host}:{settings.nexus_port}"

    capabilities: list[AgentCapability] = []
    # 1. A2A capability contracts (what this agent will accept over the wire).
    if a2a_service is not None:
        specs = list(getattr(a2a_service, "capabilities", {}).values())
        if specs:
            capabilities.extend(capabilities_from_specs(specs))
    # 2. Tool-backed capabilities (local actions), when no contract exists for
    #    the same id - contracts are the richer description.
    if tool_service is not None:
        existing = {c.id or c.name for c in capabilities}
        for cap in capabilities_from_tools(tool_service.list_tools()):
            if cap.name not in existing:
                capabilities.append(cap)

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
    _require_agent_identity(request)
    tool_service = getattr(request.app.state, "tool_service", None)
    card = await _build_signed_card(
        request.app.state.primary_identity,
        settings,
        tool_service,
        getattr(request.app.state, "a2a_service", None),
    )
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
    _require_agent_identity(request)
    tool_service = getattr(request.app.state, "tool_service", None)
    card = await _build_signed_card(
        request.app.state.primary_identity,
        settings,
        tool_service,
        getattr(request.app.state, "a2a_service", None),
    )
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
    request: Request,
    payload: DiscoverRequest,
    agent: NexusAgent = Depends(get_agent),
    discovery_service: DiscoveryService = Depends(get_discovery_service),
    _ctx: RequestContext = Depends(get_request_context),
) -> DiscoverResponse:
    owner_id = request_owner_id(request)
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


def _require_agent_identity(request: Request) -> None:
    """503 unless a primary agent identity is bound.

    M4: identity is per-agent, so readiness is the presence of a bound agent,
    not a cached private key on the service.
    """
    if getattr(request.app.state, "primary_identity", None) is None:
        raise HTTPException(
            status_code=503, detail="Agent identity is not initialised."
        )


def _validate_gateway_url(url: str) -> None:
    """SSRF-validate an outbound gateway directory URL.

    The gateway URL is operator-configured, but user input is appended to it,
    so the composed URL still passes through the same scheme/host checks used
    by every other outbound call. Raises ``A2AError`` on violation.
    """
    validate_endpoint(url, allow_local=True)


@router.get(
    "/a2a/directory/search",
    tags=["discovery"],
    summary="Search the Gateway directory by agent_id, handle, or name",
)
async def directory_search(
    request: Request,
    q: str,
    agent: NexusAgent = Depends(get_agent),
    discovery_service: DiscoveryService = Depends(get_discovery_service),
    settings: Settings = Depends(get_settings),
    _ctx: RequestContext = Depends(get_request_context),
) -> JSONResponse:
    if not settings.nexus_gateway_url:
        raise HTTPException(status_code=503, detail="Nexus Gateway URL is not configured.")

    base_http = (
        settings.nexus_gateway_url.replace("wss://", "https://")
        .replace("ws://", "http://")
        .rstrip("/ws")
        .rstrip("/")
    )

    query = q.strip()
    results: list[dict] = []

    import httpx
    from urllib.parse import quote

    # Every outbound call in this codebase goes through validate_endpoint();
    # this route previously interpolated raw user input into the URL and
    # skipped SSRF validation entirely. Both are fixed here.
    encoded_query = quote(query, safe="")
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        try:
            if query.startswith("nexus:ed25519:"):
                target = f"{base_http}/agents/{quote(query, safe='')}"
                _validate_gateway_url(target)
                resp = await client.get(target)
                if resp.status_code == 200:
                    results = [resp.json()]
            elif query.startswith("@"):
                handle = query.lstrip("@")
                target = f"{base_http}/agents/handle/{quote(handle, safe='')}"
                _validate_gateway_url(target)
                resp = await client.get(target)
                if resp.status_code == 200:
                    results = [resp.json()]
                else:
                    search_target = f"{base_http}/agents/search?q={encoded_query}"
                    _validate_gateway_url(search_target)
                    search_resp = await client.get(search_target)
                    if search_resp.status_code == 200:
                        results = search_resp.json().get("agents", [])
            else:
                search_target = f"{base_http}/agents/search?q={encoded_query}"
                _validate_gateway_url(search_target)
                resp = await client.get(search_target)
                if resp.status_code == 200:
                    results = resp.json().get("agents", [])
        except A2AError as exc:
            logger.warning("Gateway directory endpoint rejected: %s", exc.message)
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except Exception as exc:
            logger.warning("Gateway directory search error: %s", exc)
            raise HTTPException(status_code=502, detail=f"Failed to query Gateway directory: {exc}")

    # Check local trust status and verify cards
    owner_id = request_owner_id(request)
    a2a_service = getattr(request.app.state, "a2a_service", None)
    trusted_agent_ids: set[str] = set()
    if a2a_service is not None:
        # Public accessor rather than a2a_service._trusted / _session_factory (M5).
        trusted_agent_ids = await a2a_service.list_trusted_agent_ids(owner_id)

    enriched = []
    for r in results:
        agent_id = r.get("agent_id")
        card = r.get("agent_card")
        # "Verified" means ONE thing: the agent's signed card passed full
        # verification (schema, agent_id<->key, Ed25519 signature, time
        # window). A matching public_key fingerprint is NOT verification - the
        # directory is an untrusted hint source and could pair any key with any
        # claimed agent_id. Previously this branch set verified=True on a
        # fingerprint match alone.
        verified = False
        card_error: str | None = None
        if card:
            try:
                discovery_service.verify_card(card, expected_agent_id=agent_id)
                verified = True
            except Exception as exc:
                card_error = str(exc)
                verified = False
        else:
            card_error = "no signed agent card published; cannot verify"

        capabilities = []
        if verified and card and isinstance(card.get("capabilities"), list):
            for c in card["capabilities"]:
                capabilities.append(c.get("name") if isinstance(c, dict) else str(c))

        enriched.append({
            "agent_id": agent_id,
            "display_name": (card and card.get("display_name")) or r.get("display_name") or agent_id,
            "handle": r.get("handle"),
            "public_key": (card and card.get("public_key")) or r.get("public_key"),
            "endpoint": (card and card.get("endpoint")) or settings.nexus_gateway_url,
            "capabilities": capabilities,
            "is_online": r.get("is_online", False),
            "verified": verified,
            "verification_error": card_error,
            "is_trusted": agent_id in trusted_agent_ids,
            # Only expose the card itself once it verified, so the UI cannot
            # render unverified metadata as if it were attested.
            "card": card if verified else None,
        })

    return JSONResponse(content={"agents": enriched, "total": len(enriched)})


__all__ = ["router"]

