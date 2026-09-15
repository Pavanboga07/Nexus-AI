"""Shared FastAPI dependencies.

The agent and identity service are created once at application startup and
stored on ``app.state``. Routes retrieve them through these dependencies, so
handlers stay free of construction details and are trivial to override in
tests.
"""

from __future__ import annotations

from fastapi import Request

from app.a2a.discovery import DiscoveryService
from app.a2a.service import A2AService
from app.agent.agent import NexusAgent
from app.identity.service import IdentityService
from app.policy.service import PolicyService
from app.tools.service import ToolService
from app.workflows.service import WorkflowService


class HTTPDependencyError(RuntimeError):
    """Raised when a dependency is missing; mapped to 503 by the handler."""


def get_agent(request: Request) -> NexusAgent:
    """Return the process-wide agent instance."""
    agent: NexusAgent | None = getattr(request.app.state, "agent", None)
    if agent is None:  # pragma: no cover - only if startup failed
        raise RuntimeError("Nexus agent is not initialised.")
    return agent


def get_identity_service(request: Request) -> IdentityService:
    """Return the process-wide identity service (may be uninitialised)."""
    service: IdentityService | None = getattr(
        request.app.state, "identity_service", None
    )
    if service is None:
        raise RuntimeError("Identity service is not initialised.")
    return service


def get_policy_service(request: Request) -> PolicyService:
    """Return the process-wide policy service."""
    service: PolicyService | None = getattr(
        request.app.state, "policy_service", None
    )
    if service is None:
        raise HTTPDependencyError(
            "Policy service is not configured (database required)."
        )
    return service


def get_tool_service(request: Request) -> ToolService:
    """Return the process-wide tool service."""
    service: ToolService | None = getattr(
        request.app.state, "tool_service", None
    )
    if service is None:
        raise HTTPDependencyError(
            "Tool service is not configured (database required)."
        )
    return service


def get_a2a_service(request: Request) -> A2AService:
    """Return the process-wide A2A service."""
    service: A2AService | None = getattr(
        request.app.state, "a2a_service", None
    )
    if service is None:
        raise HTTPDependencyError(
            "A2A service is not configured (database + identity required)."
        )
    return service


def get_discovery_service(request: Request) -> DiscoveryService:
    """Return the process-wide discovery service (Part 7)."""
    service: DiscoveryService | None = getattr(
        request.app.state, "discovery_service", None
    )
    if service is None:
        raise HTTPDependencyError(
            "Discovery service is not configured (identity + A2A required)."
        )
    return service


def get_workflow_service(request: Request) -> WorkflowService:
    """Return the process-wide workflow service (Part 9)."""
    service: WorkflowService | None = getattr(
        request.app.state, "workflow_service", None
    )
    if service is None:
        raise HTTPDependencyError(
            "Workflow service is not configured (database required)."
        )
    return service


def get_autonomy_service(request: Request):
    """Return the process-wide autonomy service (Part 10)."""
    service = getattr(request.app.state, "autonomy_service", None)
    if service is None:
        raise HTTPDependencyError(
            "Autonomy service is not configured (database required)."
        )
    return service


__all__ = [
    "get_a2a_service",
    "get_agent",
    "get_autonomy_service",
    "get_discovery_service",
    "get_identity_service",
    "get_policy_service",
    "get_tool_service",
    "get_workflow_service",
    "HTTPDependencyError",
]

