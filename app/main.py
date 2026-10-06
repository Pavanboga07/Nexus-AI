"""FastAPI application factory and process entrypoint.

Wiring happens here and only here:

    settings -> engine -> session_factory ─┬-> DatabaseSessionStore ─┐
                                            └-> MemoryManager ───────┤
    settings -> provider ────────────────────────────────────────────┤
    settings -> embedding provider ───────────────────────────────────┤
                                                                      ▼
                                              ContextBuilder -> NexusAgent
                                                                      |
                                                                app.state.agent

Routes reach the agent through the ``get_agent`` dependency, so the transport
layer never constructs domain objects itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.a2a.rate_limit import SlidingWindowRateLimiter
from app.a2a.service import A2AService
from app.a2a.transport import A2ATransport, HttpA2ATransport
from app.agent.agent import NexusAgent
from app.agent.context import ContextBuilder
from app.agent.session import InMemorySessionStore, SessionStore
from app.api.auth_context import get_request_context
from app.api.dependencies import HTTPDependencyError
from app.api.middleware import MetricsMiddleware, TraceMiddleware
from app.api.readiness import build_system_router, reset_readiness_cache
from app.api.routes.a2a import router as a2a_router
from app.api.routes.agents import router as agents_router
from app.api.routes.auth import router as auth_router
from app.api.routes.autonomy import router as autonomy_router
from app.api.routes.chat import router as chat_router
from app.api.routes.discovery import router as discovery_router
from app.api.routes.identity import router as identity_router
from app.api.routes.memories import router as memories_router
from app.api.routes.orchestration import router as orchestration_router
from app.api.routes.policy import router as policy_router
from app.api.routes.system import router as system_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.tools import router as tools_router
from app.api.routes.workflows import router as workflows_router
from app.auth.service import AuthService
from app.config.settings import Settings, get_settings
from app.database.connection import (
    create_engine,
    create_session_factory,
)
from app.database.session_store import DatabaseSessionStore
from app.errors import (
    CODE_BAD_REQUEST,
    CODE_CONFLICT,
    CODE_DEPENDENCY_UNAVAILABLE,
    CODE_FORBIDDEN,
    CODE_GONE,
    CODE_INTERNAL,
    CODE_METHOD_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_PAYLOAD_TOO_LARGE,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    CODE_UNAUTHENTICATED,
    CODE_UPSTREAM,
    CODE_VALIDATION,
    DependencyUnavailableError,
    NexusError,
    ValidationError,
)
from app.identity.bound import AgentIdentity
from app.identity.service import IdentityCorruptionError, IdentityService
from app.llm import build_provider
from app.memory.embeddings import build_embedding_provider
from app.memory.manager import MemoryManager
from app.observability import configure_structured_logging
from app.policy.service import PolicyService
from app.search.fetch import aclose_fetcher
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService

#: Maps a framework HTTP status onto the shared error-code vocabulary, so a
#: route's `raise HTTPException(404)` produces the same envelope (and the same
#: machine-readable code) as a domain `NotFoundError`.
_HTTP_STATUS_TO_CODE = {
    400: CODE_BAD_REQUEST,
    401: CODE_UNAUTHENTICATED,
    403: CODE_FORBIDDEN,
    404: CODE_NOT_FOUND,
    405: CODE_METHOD_NOT_ALLOWED,
    409: CODE_CONFLICT,
    410: CODE_GONE,
    413: CODE_PAYLOAD_TOO_LARGE,
    422: CODE_VALIDATION,
    429: CODE_RATE_LIMITED,
    500: CODE_INTERNAL,
    502: CODE_UPSTREAM,
    503: CODE_DEPENDENCY_UNAVAILABLE,
    504: CODE_TIMEOUT,
}

logger = logging.getLogger("nexus")


def configure_logging(settings: Settings) -> None:
    """Configure root logging once.

    M11: JSON, one object per line, with the request's ``trace_id`` on every
    record. The previous format was a human-readable timestamp/level/logger
    triple, which no log aggregator can query and which a multi-line traceback
    breaks. Logging is structured because the alternative is grepping.
    """
    level = settings.nexus_log_level
    if settings.nexus_log_format == "text":
        # Retained deliberately: a developer tailing a terminal during local
        # debugging reads plain text faster than escaped JSON, and forcing JSON
        # on them is how people end up piping through `jq` to read a stack trace.
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
            force=True,
        )
        return
    configure_structured_logging(
        level=level, service=settings.nexus_service_name, environment=settings.nexus_env
    )


def build_agent(
    settings: Settings,
    engine: AsyncEngine | None,
    tool_service: ToolService | None = None,
) -> NexusAgent:
    """Construct the agent graph from settings.

    With an engine (normal operation), sessions persist to PostgreSQL and the
    memory manager is active. Without one (tests, degraded start when the DB
    is unreachable), Nexus falls back to the Part 1 in-memory store and runs
    without memory - existing chat behaviour keeps working.
    """
    provider = build_provider(settings)
    context_builder = ContextBuilder(system_prompt=settings.nexus_system_prompt)

    sessions: SessionStore
    memory_manager: MemoryManager | None = None

    if engine is not None:
        session_factory = create_session_factory(engine)
        sessions = DatabaseSessionStore(
            session_factory=session_factory,
            max_messages=settings.nexus_max_session_messages,
        )
        if settings.nexus_memory_enabled:
            embeddings = build_embedding_provider(
                provider=settings.nexus_embedding_provider,
                api_key=settings.llm_api_key,
                model=settings.nexus_embedding_model,
                dimensions=settings.nexus_embedding_dimensions,
                base_url=settings.llm_base_url,
            )
            memory_manager = MemoryManager(
                session_factory=session_factory,
                embeddings=embeddings,
                dedup_threshold=settings.nexus_memory_dedup_threshold,
            )
    else:
        sessions = InMemorySessionStore(
            max_messages=settings.nexus_max_session_messages
        )

    return NexusAgent(
        provider=provider,
        sessions=sessions,
        context_builder=context_builder,
        memory_manager=memory_manager,
        memory_top_k=settings.nexus_memory_top_k,
        memory_enabled=settings.nexus_memory_enabled,
        tool_service=tool_service,
    )


async def _memory_retention_loop(
    memory_manager: MemoryManager, retention_days: int
) -> None:
    """Reap expired episodic memories once a day until cancelled."""
    while True:
        try:
            await asyncio.sleep(24 * 3600)
            removed = await memory_manager.reap_expired(
                episodic_retention_days=retention_days
            )
            logger.info("memory_retention_tick removed=%d", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad tick
            logger.warning("memory_retention_tick_failed detail=%s", exc)


def start_memory_retention(
    settings, memory_manager: MemoryManager | None
) -> asyncio.Task | None:
    """Start the daily episodic-memory retention task; None when disabled."""
    retention_days = settings.nexus_memory_episodic_retention_days
    if not retention_days or memory_manager is None:
        return None
    logger.info("memory_retention_enabled days=%d", retention_days)
    return asyncio.create_task(_memory_retention_loop(memory_manager, retention_days))


async def _try_connect(database_url: str, echo: bool) -> AsyncEngine | None:
    """Create an engine and verify connectivity; None if unreachable.

    Nexus starts even when PostgreSQL is down (health reports it), rather
    than crashing - the chat API still works on the in-memory fallback.
    """
    engine = create_engine(database_url, echo=echo)
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return engine
    except Exception as exc:
        logger.warning(
            "database_unreachable detail=%s falling back to in-memory sessions",
            type(exc).__name__,
        )
        await engine.dispose()
        return None


@dataclasses.dataclass
class _IdentityParts:
    """Everything the identity block produces; threaded into later builders."""

    session_factory: async_sessionmaker | None
    identity_service: IdentityService | None
    primary_identity: AgentIdentity | None
    owner_id: uuid.UUID | None


async def build_identity(
    settings: Settings, engine: AsyncEngine | None
) -> _IdentityParts:
    """Build the session factory + deployment-owner identity (Part 3, M4).

    Identity is per-AGENT, not per-process. Startup ensures the deployment
    owner has at least one agent (idempotent) and binds an AgentIdentity
    adapter for the protocol stack. A corrupted identity (wrong secret,
    mismatched keypair) aborts startup: silently regenerating would break
    all future trust relationships.
    """
    session_factory = None
    identity_service: IdentityService | None = None
    primary_identity: AgentIdentity | None = None
    owner_id: uuid.UUID | None = None
    if engine is not None:
        session_factory = create_session_factory(engine)
        # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
        from app.database.repositories import OwnerRepository

        async with session_factory() as session:
            owner = await OwnerRepository().get_or_create_default(session)
            await session.commit()
            owner_id = owner.id
        identity_service = IdentityService(
            session_factory=session_factory,
            encryption_secret=settings.nexus_identity_key,
            owner_id=owner_id,
        )
        try:
            # Idempotent: creates the owner's primary agent on first run only.
            agent_summary = await identity_service.initialize_primary_agent(
                owner_id, display_name=settings.nexus_agent_display_name
            )
            primary_identity = await AgentIdentity.for_agent(
                identity_service=identity_service,
                agent_row_id=agent_summary.id,
            )
            public_identity = primary_identity.get_public_identity()
            logger.info(
                "identity_ready agent_id=%s fingerprint=%s display_name=%s",
                public_identity.agent_id,
                public_identity.fingerprint,
                agent_summary.display_name,
            )
        except IdentityCorruptionError as exc:
            logger.critical("identity_verification_failure detail=%s", exc)
            await engine.dispose()
            raise RuntimeError(f"Fatal identity error: {exc}") from exc
    return _IdentityParts(
        session_factory=session_factory,
        identity_service=identity_service,
        primary_identity=primary_identity,
        owner_id=owner_id,
    )


def build_auth(
    settings: Settings, session_factory: async_sessionmaker | None
) -> AuthService | None:
    """Build the session authentication service (M3).

    Every request resolves its own principal from a session. Until this
    existed the owner was resolved ONCE here from the first `owners` row and
    cached, which made the whole API single-tenant and left authorization
    with nothing to authorize against.
    """
    if session_factory is None:
        return None
    session_secret = settings.nexus_session_key or settings.nexus_identity_key
    if settings.auth_is_required and not settings.nexus_session_key:
        # Refuse to run "authenticated" with a borrowed/missing secret:
        # sessions would be forgeable by anyone who knows the identity key.
        logger.critical(
            "auth_required_without_session_key: NEXUS_SESSION_KEY must be "
            "set when NEXUS_AUTH_REQUIRED is true."
        )
        raise RuntimeError(
            "NEXUS_SESSION_KEY is required when authentication is enforced."
        )
    if not settings.nexus_session_key:
        logger.warning(
            "auth_session_key_not_set: falling back to NEXUS_IDENTITY_KEY "
            "for session signing; set NEXUS_SESSION_KEY before production."
        )
    return AuthService(
        session_factory=session_factory,
        session_secret=session_secret,
        session_ttl_seconds=settings.nexus_session_ttl_seconds,
        allow_registration=settings.nexus_allow_registration,
    )


def build_policy(session_factory: async_sessionmaker | None) -> PolicyService | None:
    """Build deterministic authorization over policies/consents (Part 4)."""
    if session_factory is None:
        return None
    return PolicyService(session_factory=session_factory)


def build_tools(
    settings: Settings,
    session_factory: async_sessionmaker | None,
    policy_service: PolicyService | None,
) -> ToolService | None:
    """Build the MCP-compatible registry + policy-gated execution (Part 5)."""
    if session_factory is None or policy_service is None:
        return None
    registry = ToolRegistry()
    for builtin in BUILTIN_TOOLS:
        registry.register(builtin)
    return ToolService(
        registry=registry,
        policy_service=policy_service,
        session_factory=session_factory,
        timeout_seconds=settings.nexus_tool_timeout_seconds,
        max_result_bytes=settings.nexus_tool_max_result_bytes,
    )


def build_a2a(
    settings: Settings,
    identity: _IdentityParts,
    policy_service: PolicyService | None,
    agent: NexusAgent,
    tool_service: ToolService | None,
) -> tuple[A2AService | None, Any]:
    """Build secure agent-to-agent communication (Part 6).

    Requires identity (signing), policy (authorization), and the database;
    the memory manager is reused from the agent for policy-gated disclosures.
    Returns (a2a_service, gateway_client); the client is None unless a
    gateway URL is configured. The caller starts the gateway client AFTER
    storing it on app.state, so shutdown can always find it.
    """
    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.a2a.gateway_client import GatewayA2ATransport, GatewayClient

    session_factory = identity.session_factory
    primary_identity = identity.primary_identity
    owner_id = identity.owner_id
    if session_factory is None or primary_identity is None or policy_service is None:
        return None, None

    http_transport = HttpA2ATransport(
        timeout_seconds=settings.nexus_a2a_timeout_seconds,
        max_response_bytes=settings.nexus_a2a_max_message_bytes,
        allow_local=settings.nexus_a2a_allow_local_endpoints,
    )

    gateway_client = None
    transport: A2ATransport = http_transport

    # Late-binding box: the inbound handler is registered before the service
    # exists, but it only ever runs after startup has assigned it.
    box: dict[str, A2AService | None] = {"service": None}

    async def _handle_gateway_inbound(envelope):
        service = box["service"]
        if service is not None and owner_id is not None:
            # The gateway is an untrusted relay: a malformed or
            # hostile frame must never raise into the reader loop.
            return await service.handle_gateway_delivery(owner_id, envelope)
        return None

    if settings.nexus_gateway_url:
        gateway_client = GatewayClient(
            gateway_url=settings.nexus_gateway_url,
            identity_service=primary_identity,
            owner_id=owner_id,
            inbound_handler=_handle_gateway_inbound,
            display_name=settings.nexus_agent_display_name,
            handle=settings.nexus_agent_handle,
        )
        transport = GatewayA2ATransport(
            http_transport=http_transport,
            gateway_client=gateway_client,
            allow_direct_egress=settings.nexus_a2a_direct_egress,
        )

    a2a_service = A2AService(
        session_factory=session_factory,
        identity_service=primary_identity,
        policy_service=policy_service,
        memory_manager=agent.memory,
        transport=transport,
        rate_limiter=SlidingWindowRateLimiter(
            settings.nexus_a2a_rate_limit_per_minute
        ),
        max_message_bytes=settings.nexus_a2a_max_message_bytes,
        max_clock_skew_seconds=settings.nexus_a2a_max_clock_skew_seconds,
        message_ttl_seconds=settings.nexus_a2a_message_ttl_seconds,
        allow_local_endpoints=settings.nexus_a2a_allow_local_endpoints,
        tool_service=tool_service,
        max_negotiation_rounds=settings.nexus_a2a_max_negotiation_rounds,
        task_ttl_seconds=settings.nexus_a2a_task_ttl_seconds,
    )
    box["service"] = a2a_service
    return a2a_service, gateway_client


def build_discovery(
    settings: Settings,
    identity: _IdentityParts,
    a2a_service: A2AService | None,
) -> Any:
    """Build agent discovery and card hosting (Part 7).

    Requires identity (card signing) and A2A (trusted-agent registration).
    Falls back gracefully when unavailable.
    """
    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.a2a.discovery import DiscoveryService

    if a2a_service is None or identity.primary_identity is None:
        return None
    return DiscoveryService(
        identity_service=identity.primary_identity,
        a2a_service=a2a_service,
        allow_local_endpoints=settings.nexus_a2a_allow_local_endpoints,
        timeout_seconds=settings.nexus_discovery_timeout_seconds,
        max_card_bytes=settings.nexus_discovery_max_card_bytes,
        session_factory=identity.session_factory,
    )


async def build_workflows(
    settings: Settings,
    identity: _IdentityParts,
    policy_service: PolicyService | None,
    tool_service: ToolService | None,
    a2a_service: A2AService | None,
    agent: NexusAgent,
) -> Any:
    """Build the workflow engine (Part 9), resume listeners and crash recovery."""
    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.workflows.service import WorkflowService

    session_factory = identity.session_factory
    if session_factory is None or policy_service is None:
        return None
    workflow_service = WorkflowService(
        session_factory=session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        a2a_service=a2a_service,
        memory_manager=agent.memory,
        identity_service=identity.primary_identity,
        default_ttl_seconds=settings.nexus_workflow_default_ttl_seconds,
        max_step_attempts=settings.nexus_workflow_max_step_attempts,
    )

    # Resume path: remote A2A responses arrive on the bus, so the workflow
    # engine must listen for them — otherwise WAITING_REMOTE steps never wake.
    if a2a_service is not None:
        a2a_service.register_task_completion_callback(
            workflow_service.handle_task_completion
        )

    # Crash recovery: workflows paused mid-run (awaiting a remote task or an
    # approval) are left in a non-terminal state by a process restart. Resume
    # them now, mirroring autonomy's reconcile_on_startup(). Without this,
    # interrupted workflows stay wedged forever.
    try:
        resumed = await workflow_service.recover_interrupted_workflows()
        if resumed:
            logger.info("workflow_crash_recovery_completed count=%d", len(resumed))
    except Exception as exc:
        logger.warning("workflow_startup_recovery_warning: %s", exc)
    return workflow_service


async def build_autonomy(
    identity: _IdentityParts,
    policy_service: PolicyService | None,
    tool_service: ToolService | None,
    a2a_service: A2AService | None,
    workflow_service: Any,
    agent: NexusAgent,
) -> Any:
    """Build the autonomy service + decision engine (Part 10)."""
    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.autonomy.service import AutonomyService

    session_factory = identity.session_factory
    if session_factory is None or policy_service is None:
        return None
    autonomy_service = AutonomyService(
        session_factory=session_factory,
        policy_service=policy_service,
        tool_service=tool_service,
        a2a_service=a2a_service,
        workflow_service=workflow_service,
        memory_manager=agent.memory,
    )
    try:
        reconciled = await autonomy_service.reconcile_on_startup()
        if reconciled > 0:
            logger.info("autonomy_crash_recovery_completed count=%d", reconciled)
    except Exception as exc:
        logger.warning("autonomy_startup_reconciliation_warning: %s", exc)
    return autonomy_service


def build_orchestration(
    settings: Settings,
    identity: _IdentityParts,
    a2a_service: A2AService | None,
    policy_service: PolicyService | None,
    discovery_service: Any,
    agent: NexusAgent,
    autonomy_service: Any,
    workflow_service: Any,
) -> Any:
    """Build natural-language orchestration (Part 12).

    Gated behind NEXUS_ORCHESTRATION_ENABLED (decision D5): OFF by default.
    Orchestration converts free text into agent actions through an LLM intent
    resolver plus fuzzy target matching, so a wrong guess is acted upon - and
    silently. Explicit asks and the workflow API work without it, so this
    costs convenience rather than capability.
    """
    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.orchestration.intent import IntentResolver

    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.orchestration.orchestrator import AgentOrchestrator
    from app.orchestration.target_resolver import TargetResolver

    if not settings.nexus_orchestration_enabled:
        logger.info(
            "orchestration_disabled detail=NEXUS_ORCHESTRATION_ENABLED is false; "
            "natural-language orchestration is off (chat and explicit asks are "
            "unaffected)"
        )
        return None
    session_factory = identity.session_factory
    if (
        session_factory is None
        or a2a_service is None
        or policy_service is None
    ):
        return None
    intent_resolver = IntentResolver(llm_provider=agent.provider)
    target_resolver = TargetResolver(
        session_factory=session_factory,
        trusted_agents=a2a_service.trusted_agents,
        discovery_service=discovery_service,
        memory_manager=agent.memory,
        gateway_url=settings.nexus_gateway_url,
    )
    decision_engine = getattr(autonomy_service, "_decision_engine", None)
    return AgentOrchestrator(
        session_factory=session_factory,
        a2a_service=a2a_service,
        policy_service=policy_service,
        intent_resolver=intent_resolver,
        target_resolver=target_resolver,
        decision_engine=decision_engine,
        workflow_service=workflow_service,
    )


async def build_jobs(
    settings: Settings,
    identity: _IdentityParts,
    workflow_service: Any,
) -> tuple[Any, Any, Any]:
    """Build the durable job queue + worker (M7).

    A Postgres-backed queue (decision D6) so retries, backoff, idempotency and
    dead-lettering exist at all. Before this, asynchronous work was either
    inline in a request or a fire-and-forget task whose failure was lost.
    Returns (job_queue, job_worker, job_registry); all None when no database.
    """
    # lazy: deferred to startup; keeps `import app.main` light for tests/tooling.
    from app.jobs import JobQueue, JobRegistry, JobWorker

    session_factory = identity.session_factory
    if session_factory is None:
        return None, None, None
    job_queue = JobQueue(session_factory=session_factory)
    registry = JobRegistry()

    # Handler registration is explicit: a kind is enqueued only if a handler
    # exists, and an unregistered kind dead-letters with a clear reason.
    if workflow_service is not None:
        async def _advance_workflow_job(job) -> None:
            workflow_id = uuid.UUID(str(job.payload.get("workflow_id")))
            await workflow_service.advance_workflow(workflow_id)

        registry.register("workflow.advance", _advance_workflow_job)

    job_worker = JobWorker(
        queue=job_queue,
        registry=registry,
        poll_interval_seconds=settings.nexus_job_poll_interval_seconds,
        batch_size=settings.nexus_job_batch_size,
        lease_seconds=settings.nexus_job_lease_seconds,
        job_timeout_seconds=settings.nexus_job_timeout_seconds,
    )
    await job_worker.start()
    return job_queue, job_worker, registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the agent on startup and release resources on shutdown."""
    settings = get_settings()
    configure_logging(settings)

    engine = await _try_connect(settings.database_url, settings.nexus_db_echo)
    app.state.engine = engine
    app.state.database_ok = engine is not None

    identity = await build_identity(settings, engine)
    app.state.identity_service = identity.identity_service
    #: Back-compat name used by the A2A stack, the gateway client and card
    #: signing. It is the PRIMARY agent's identity adapter, not a singleton
    #: identity.
    app.state.primary_identity = identity.primary_identity
    app.state.identity_ok = identity.primary_identity is not None

    auth_service = build_auth(settings, identity.session_factory)
    app.state.auth_service = auth_service
    app.state.auth_required = settings.auth_is_required
    app.state.registration_open = settings.nexus_allow_registration
    app.state.cookie_secure = settings.cookie_secure_effective
    app.state.session_ttl_seconds = settings.nexus_session_ttl_seconds
    app.state.adopt_legacy_owner = settings.nexus_adopt_legacy_owner

    policy_service = build_policy(identity.session_factory)
    app.state.policy_service = policy_service
    app.state.policy_ok = policy_service is not None

    tool_service = build_tools(settings, identity.session_factory, policy_service)
    app.state.tool_service = tool_service
    app.state.tools_ok = tool_service is not None

    app.state.agent = build_agent(settings, engine, tool_service)

    # --- Memory retention (M8) --------------------------------------------------
    # Wires the documented NEXUS_MEMORY_EPISODIC_RETENTION_DAYS knob: a daily
    # task reaps expired episodic memories. Disabled (0/unset) by default.
    app.state.memory_retention_task = start_memory_retention(
        settings, app.state.agent.memory
    )

    a2a_service, gateway_client = build_a2a(
        settings, identity, policy_service, app.state.agent, tool_service
    )
    # Stored BEFORE the client starts, so shutdown can always find it even
    # if a later builder raises.
    app.state.gateway_client = gateway_client
    if gateway_client is not None:
        await gateway_client.start()
        logger.info("gateway_relay_active url=%s", settings.nexus_gateway_url)
    app.state.a2a_service = a2a_service
    app.state.a2a_ok = a2a_service is not None
    app.state.gateway_ok = gateway_client is not None

    discovery_service = build_discovery(settings, identity, a2a_service)
    app.state.discovery_service = discovery_service
    app.state.discovery_ok = discovery_service is not None

    workflow_service = await build_workflows(
        settings,
        identity,
        policy_service,
        tool_service,
        a2a_service,
        app.state.agent,
    )
    app.state.workflow_service = workflow_service
    app.state.workflows_ok = workflow_service is not None

    autonomy_service = await build_autonomy(
        identity,
        policy_service,
        tool_service,
        a2a_service,
        workflow_service,
        app.state.agent,
    )
    app.state.autonomy_service = autonomy_service
    app.state.autonomy_ok = autonomy_service is not None

    orchestrator = build_orchestration(
        settings,
        identity,
        a2a_service,
        policy_service,
        discovery_service,
        app.state.agent,
        autonomy_service,
        workflow_service,
    )
    app.state.orchestrator = orchestrator
    app.state.orchestration_ok = orchestrator is not None

    job_queue, job_worker, job_registry = await build_jobs(
        settings, identity, workflow_service
    )
    if job_registry is not None:
        app.state.job_registry = job_registry
    app.state.job_queue = job_queue
    app.state.job_worker = job_worker
    app.state.jobs_ok = job_queue is not None

    logger.info(
        "nexus_started env=%s provider=%s model=%s base_url=%s llm_configured=%s "
        "database=%s memory=%s embedding=%s identity=%s tools=%s a2a=%s discovery=%s workflows=%s autonomy=%s orchestration=%s",
        settings.nexus_env,
        app.state.agent.provider_name,
        settings.llm_model,
        settings.llm_base_url or "(default)",
        settings.llm_configured,
        "postgres" if engine is not None else "in-memory-fallback",
        app.state.agent.memory_enabled,
        settings.nexus_embedding_provider,
        "ready" if app.state.identity_ok else "unavailable",
        "ready" if app.state.tools_ok else "unavailable",
        "ready" if app.state.a2a_ok else "unavailable",
        "ready" if app.state.discovery_ok else "unavailable",
        "ready" if app.state.workflows_ok else "unavailable",
        "ready" if app.state.autonomy_ok else "unavailable",
        "ready" if app.state.orchestration_ok else "unavailable",
    )
    if not settings.llm_configured:
        logger.warning(
            "nexus_llm_not_configured detail=NEXUS_LLM_API_KEY is not set; "
            "chat requests will return 503 until it is configured."
        )

    try:
        yield
    finally:
        # Stop claiming new jobs and let the in-flight one finish, so a deploy
        # does not abandon a half-done unit of work.
        if getattr(app.state, "job_worker", None) is not None:
            await app.state.job_worker.stop()
        retention_task = getattr(app.state, "memory_retention_task", None)
        if retention_task is not None:
            retention_task.cancel()
        if getattr(app.state, "gateway_client", None) is not None:
            await app.state.gateway_client.stop()
        await app.state.agent.aclose()
        if app.state.engine is not None:
            await app.state.engine.dispose()
        await aclose_fetcher()
        logger.info("nexus_stopped")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Nexus Runtime",
        description=(
            "Personal AI agent runtime: conversational sessions, long-term "
            "memory, cryptographic identity, policy-gated tools, A2A "
            "communication, workflows, autonomy, and orchestration."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    settings = get_settings()
    cors_origins = settings.cors_origins_list
    # Browsers reject `allow_credentials=True` combined with a wildcard
    # origin; more importantly a wildcard would let any site drive this API
    # with the user's cookies. Keep them mutually exclusive.
    cors_allow_credentials = not settings.cors_allows_any_origin
    if settings.cors_allows_any_origin and not settings.is_development:
        logger.warning(
            "cors_wildcard_origin_configured env=%s: this is unsafe outside "
            "development; set NEXUS_CORS_ORIGINS to an explicit allow-list.",
            settings.nexus_env,
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    # --- Observability (M11) --------------------------------------------------
    # Order matters, and Starlette applies middleware in REVERSE order of
    # `add_middleware`: the last one added is the outermost. Tracing is added
    # last so it wraps metrics - every metric then carries the trace id of the
    # request that produced it, and the trace header is set even on a response
    # the metrics layer short-circuits.
    #
    # TraceMiddleware must be pure ASGI, not BaseHTTPMiddleware: the latter runs
    # the app in a task with a COPY of the context, so a ContextVar bound inside
    # it never reaches the endpoint.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(TraceMiddleware)

    # Auth first, unauthenticated by necessity (you cannot require a session
    # in order to obtain one).
    app.include_router(auth_router)

    # Liveness must never require a session (load balancers, orchestrators,
    # monitoring), so it is mounted without the auth dependency. Its payload is
    # deliberately minimal; the detailed report is /system/status, protected.
    app.include_router(system_router)

    # Readiness and metrics (M11). Also public, for the same reason as /health:
    # a probe and a scraper have no session. /readyz is the one that returns 503
    # when a required dependency is down, so the load balancer drains this
    # replica instead of routing requests that can only fail.
    reset_readiness_cache()
    app.include_router(build_system_router(metrics_enabled=True))

    # Every data router requires a resolved principal. Applied at the router
    # level rather than per-handler, so a new endpoint is protected by default
    # instead of protected only if its author remembered to add a dependency.
    #
    # Deliberately NOT behind this dependency:
    #   /health                        - liveness must be reachable
    #   /auth/*                        - bootstrapping
    #   /.well-known/*, /a2a/card      - public identity cards (no data)
    #   /a2a/messages                  - authenticated by Ed25519 envelope
    #                                    signature, not by a session
    protected = [Depends(get_request_context)]
    app.include_router(chat_router, dependencies=protected)
    app.include_router(memories_router, dependencies=protected)
    app.include_router(identity_router, dependencies=protected)
    app.include_router(agents_router, dependencies=protected)
    app.include_router(policy_router, dependencies=protected)
    app.include_router(tools_router, dependencies=protected)
    app.include_router(a2a_router, dependencies=protected)
    # Discovery is mixed: the public card endpoints (/.well-known/..., /a2a/card)
    # must stay reachable so peers can fetch an identity, while the stateful
    # endpoints carry their own get_request_context dependency. See the module
    # docstring in app/api/routes/discovery.py.
    app.include_router(discovery_router)
    app.include_router(tasks_router, dependencies=protected)
    app.include_router(workflows_router, dependencies=protected)
    app.include_router(autonomy_router, dependencies=protected)
    app.include_router(orchestration_router, dependencies=protected)

    # --- Error handling (M5) --------------------------------------------------
    # ONE envelope for every expected failure. These handlers are registered so
    # a route can simply let a domain error propagate and still get a correct
    # status and a consistent body, instead of translating by hand (which is
    # how the same condition ended up as a different status in different
    # routes).

    @app.exception_handler(NexusError)
    async def _nexus_error_handler(
        request: Request, exc: NexusError
    ) -> JSONResponse:
        """Domain errors: stable code, honest status, safe message."""
        if exc.http_status >= 500:
            logger.error(
                "domain_error path=%s code=%s detail=%s",
                request.url.path,
                exc.code,
                exc.message,
            )
        else:
            logger.info(
                "domain_error path=%s code=%s detail=%s",
                request.url.path,
                exc.code,
                exc.message,
            )
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(HTTPDependencyError)
    async def _dependency_handler(
        request: Request, exc: HTTPDependencyError
    ) -> JSONResponse:
        """A required subsystem is missing: 503, not 500.

        This used to be reachable only as a bare RuntimeError from some
        dependencies, which the catch-all turned into a misleading 500.
        """
        error = DependencyUnavailableError(str(exc))
        return JSONResponse(
            status_code=error.http_status, content=error.to_dict()
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return the SAME envelope as every other error.

        Pydantic v2 error dicts can carry the original exception object in
        ``ctx``; encode defensively so the response stays JSON-safe.
        """
        from fastapi.encoders import jsonable_encoder

        error = ValidationError(
            "Request failed validation.",
            details={"errors": jsonable_encoder(exc.errors())},
        )
        return JSONResponse(status_code=422, content=error.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Convert framework HTTPExceptions into the ONE error envelope.

        Without this there were still TWO response shapes: domain errors went
        out as ``{"error": {...}}`` while a route's ``raise HTTPException`` went
        out as ``{"detail": "..."}``. A client then has to parse both, and the
        frontend's error handling silently degraded to "HTTP 400" for the
        second kind. Routing them through the same envelope is what makes the
        error contract actually single.
        """
        code = _HTTP_STATUS_TO_CODE.get(exc.status_code, "http_error")
        detail = exc.detail
        error = NexusError(
            detail if isinstance(detail, str) else "Request failed.",
            code=code,
            http_status=exc.status_code,
            details=None if isinstance(detail, str) else {"detail": detail},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error.to_dict(),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """Last-resort handler: log the trace, return a generic message.

        The message is deliberately opaque - an unexpected error must never
        leak internals - but the envelope matches every other error so clients
        parse responses uniformly.
        """
        logger.exception("unhandled_error path=%s", request.url.path)
        error = NexusError("An unexpected error occurred.")
        return JSONResponse(
            status_code=error.http_status, content=error.to_dict()
        )

    return app


app = create_app()


__all__ = [
    "app",
    "build_agent",
    "build_a2a",
    "build_auth",
    "build_autonomy",
    "build_discovery",
    "build_identity",
    "build_jobs",
    "build_orchestration",
    "build_policy",
    "build_tools",
    "build_workflows",
    "configure_logging",
    "create_app",
    "lifespan",
    "start_memory_retention",
]
