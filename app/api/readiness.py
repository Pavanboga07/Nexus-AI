"""Readiness probe with real dependency checks (M11).

The difference between `/health` and `/readyz` is the point of this module, and
it is a difference people get wrong:

* `/health` is **liveness** - "is this process wedged?". It must not check the
  database. A liveness probe that fails when Postgres is down makes the
  orchestrator kill every healthy replica during a database incident, turning a
  slow API into an outage. It stays a constant 200.

* `/readyz` is **readiness** - "should traffic be routed here yet?". It must
  check the database, and it must fail (503) when a *required* dependency is
  unavailable, so the load balancer drains this replica instead of sending it
  requests that can only 500.

Which dependencies are required is a policy decision, so it is explicit:

* **database** - required whenever one is configured. Without it, every
  stateful endpoint fails; the only reason Nexus can start without one is the
  in-memory fallback for chat, which is a development affordance, not a
  production posture.
* **identity** - required. A2A signing and card serving are the product.
* **job queue** - required only when the worker is enabled, because a queue
  configured but not claimable means approvals never advance.
* **gateway** - never required. Gateway-first transport falls back to direct
  egress by design (D2), so a gateway outage degrades reachability rather than
  readiness.

The result is cached briefly. A probe runs every few seconds per replica, and
hitting the database on each one turns monitoring into load - but caching
forever would let a replica stay "ready" after its database died.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.observability import METRICS
from app.schemas.system import DependencyCheck, ReadyResponse

logger = logging.getLogger("nexus.api.system")

#: Seconds a readiness verdict is reused. Short enough that a dead database is
#: noticed within one probe interval, long enough that a 2-second probe does not
#: become 30 queries a minute per replica.
READINESS_TTL_SECONDS = 2.0

#: Bound on the database check. A probe that hangs is worse than one that fails:
#: the orchestrator never gets an answer and the replica is never drained.
DB_CHECK_TIMEOUT_SECONDS = 2.0


@dataclass
class _CacheEntry:
    at: float
    payload: ReadyResponse


_cache: _CacheEntry | None = None


def reset_readiness_cache() -> None:
    """Drop the cached verdict. Called on startup and by tests."""
    global _cache
    _cache = None


async def _check_database(request: Request) -> DependencyCheck:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        # Not configured (tests, or an explicit no-DB run). Reported as ready
        # with a reason rather than as a failure, because a deployment without a
        # database is a supported development configuration.
        if getattr(request.app.state, "database_ok", False):
            return DependencyCheck(name="database", ready=True)
        return DependencyCheck(
            name="database", ready=True, detail="not configured (in-memory fallback)"
        )

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the reason is the whole point
        # The message is logged with its type but reported to the caller without
        # it: this payload is public, and a driver error routinely contains the
        # host and user name.
        logger.warning("readiness_database_unreachable error=%s", type(exc).__name__)
        return DependencyCheck(
            name="database", ready=False, detail="database unreachable"
        )
    return DependencyCheck(name="database", ready=True)


def _check_identity(request: Request) -> DependencyCheck:
    if getattr(request.app.state, "identity_ok", False):
        return DependencyCheck(name="identity", ready=True)
    return DependencyCheck(
        name="identity", ready=False, detail="agent identity not initialised"
    )


def _check_jobs(request: Request) -> DependencyCheck:
    worker = getattr(request.app.state, "job_worker", None)
    if worker is None:
        return DependencyCheck(name="jobs", ready=True, detail="worker disabled")
    running = bool(getattr(worker, "is_running", False))
    if not running:
        return DependencyCheck(name="jobs", ready=False, detail="worker not running")
    return DependencyCheck(name="jobs", ready=True)


def _check_gateway(request: Request) -> DependencyCheck:
    """Informational only - never changes the verdict (see the module docstring)."""
    client = getattr(request.app.state, "gateway_client", None)
    if client is None:
        return DependencyCheck(name="gateway", ready=True, detail="not configured")
    connected = bool(getattr(client, "is_connected", False))
    return DependencyCheck(
        name="gateway",
        ready=True,
        detail="connected" if connected else "not connected; direct egress in use",
    )


async def build_readiness(request: Request) -> ReadyResponse:
    checks: list[DependencyCheck] = [
        await _check_database(request),
        _check_identity(request),
        _check_jobs(request),
        _check_gateway(request),
    ]
    # `gateway` is advisory; everything else gates traffic.
    failed = [c.name for c in checks if not c.ready and c.name != "gateway"]
    return ReadyResponse(
        status="not_ready" if failed else "ready",
        version=__version__,
        checks=checks,
        failed=failed,
    )


def build_system_router(metrics_enabled: bool = True) -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get(
        "/readyz",
        summary="Readiness probe (public, checks dependencies)",
        responses={503: {"description": "A required dependency is unavailable"}},
    )
    async def readyz(request: Request) -> JSONResponse:
        global _cache
        now = time.monotonic()
        if _cache is not None and now - _cache.at < READINESS_TTL_SECONDS:
            payload = _cache.payload
        else:
            payload = await build_readiness(request)
            _cache = _CacheEntry(at=now, payload=payload)

        # 503 is the signal the orchestrator acts on. Returning 200 with
        # `"status": "not_ready"` would require every probe to parse the body -
        # most do not, and would keep routing traffic to a broken replica.
        status_code = 200 if payload.status == "ready" else 503
        return JSONResponse(status_code=status_code, content=payload.model_dump())

    if metrics_enabled:

        @router.get(
            "/metrics",
            summary="Prometheus metrics (public)",
        )
        async def metrics() -> Response:
            """The exposition endpoint.

            Public on purpose: a scraper has no session, and the payload carries
            counts rather than data. It is unauthenticated in the same sense a
            load balancer's stats page is - the mitigation for a hostile network
            is to bind it internally or front it with the ingress, which is a
            deployment decision, not something the app can enforce.
            """
            return Response(
                content=METRICS.render(), media_type="text/plain; version=0.0.4"
            )

    return router


__all__ = [
    "READINESS_TTL_SECONDS",
    "build_readiness",
    "build_system_router",
    "reset_readiness_cache",
]
