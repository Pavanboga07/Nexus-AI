"""System/health response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReadinessResponse(BaseModel):
    """The PUBLIC liveness payload - intentionally minimal.

    Subsystem/configuration detail is available at ``/system/status`` behind
    authentication.
    """

    status: Literal["ok"] = "ok"
    version: str


class DependencyCheck(BaseModel):
    """One dependency's verdict.

    ``detail`` is a short, stable reason string - not a traceback. This payload
    is public, so it must say "database unreachable" without saying the host,
    the port, or the driver's error text.
    """

    name: str
    ready: bool
    detail: str | None = None


class ReadyResponse(BaseModel):
    """The PUBLIC readiness payload.

    Deliberately a list of dependency *names* and booleans, not the subsystem
    report: a probe needs to know "can this replica take traffic", and an
    anonymous caller does not need to know which optional features are
    configured. The names are the ones an operator already sees in the compose
    file, so they disclose nothing that is not in the repository.
    """

    status: Literal["ready", "not_ready"]
    version: str
    checks: list[DependencyCheck]
    failed: list[str] = Field(default_factory=list)


__all__ = ["DependencyCheck", "ReadyResponse", "ReadinessResponse"]
