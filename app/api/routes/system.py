"""System routes: liveness and readiness.

These are mounted WITHOUT the authentication dependency on purpose. A liveness
probe that requires a session cannot be used by a load balancer, a container
orchestrator, or a monitoring system - and it would be a self-inflicted outage
during a login incident.

To avoid trading one problem for another (leaking configuration to anonymous
callers), the public payload is deliberately minimal. The detailed subsystem
report requires a session and lives at ``/system/status``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app import __version__
from app.agent.agent import NexusAgent
from app.api.auth_context import get_request_context
from app.api.dependencies import get_agent
from app.config.settings import Settings, get_settings
from app.schemas.chat import HealthResponse
from app.schemas.system import ReadinessResponse

logger = logging.getLogger("nexus.api.system")

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=ReadinessResponse,
    summary="Liveness probe (public, minimal payload)",
)
async def health(request: Request, settings: Settings = Depends(get_settings)) -> ReadinessResponse:
    """Always 200 while the process is alive.

    Reports only what a probe needs. Deliberately does NOT report which
    subsystems are configured, because that is configuration disclosure to an
    unauthenticated caller.
    """
    return ReadinessResponse(status="ok", version=__version__)


@router.get(
    "/system/status",
    response_model=HealthResponse,
    summary="Detailed subsystem status (requires a session)",
)
async def system_status(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    settings: Settings = Depends(get_settings),
    _ctx=Depends(get_request_context),
) -> HealthResponse:
    """Full configuration/subsystem report for the signed-in operator."""
    database_ok: bool = getattr(request.app.state, "database_ok", False)
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.nexus_env,
        llm_provider=agent.provider_name,
        llm_configured=settings.llm_configured,
        database=database_ok,
        memory=agent.memory_enabled,
        identity=bool(getattr(request.app.state, "identity_ok", False)),
        tools=bool(getattr(request.app.state, "tools_ok", False)),
        a2a=bool(getattr(request.app.state, "a2a_ok", False)),
        autonomy=bool(getattr(request.app.state, "autonomy_ok", False)),
        gateway=bool(getattr(request.app.state, "gateway_ok", False)),
    )


__all__ = ["router"]
