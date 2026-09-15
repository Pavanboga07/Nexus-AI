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

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.agent.agent import NexusAgent
from app.agent.context import ContextBuilder
from app.agent.session import InMemorySessionStore, SessionStore
from app.api.dependencies import HTTPDependencyError
from app.api.routes.a2a import router as a2a_router
from app.api.routes.chat import router as chat_router
from app.api.routes.discovery import router as discovery_router
from app.api.routes.identity import router as identity_router
from app.api.routes.memories import router as memories_router
from app.api.routes.policy import router as policy_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.tools import router as tools_router
from app.api.routes.workflows import router as workflows_router
from app.api.routes.autonomy import router as autonomy_router
from app.a2a.rate_limit import SlidingWindowRateLimiter
from app.a2a.service import A2AService
from app.a2a.transport import HttpA2ATransport
from app.config.settings import Settings, get_settings
from app.database.connection import (
    create_engine,
    create_session_factory,
)
from app.database.session_store import DatabaseSessionStore
from app.identity.service import IdentityCorruptionError, IdentityService
from app.llm import build_provider
from app.memory.embeddings import build_embedding_provider
from app.memory.manager import MemoryManager
from app.policy.service import PolicyService
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService

logger = logging.getLogger("nexus")


def configure_logging(settings: Settings) -> None:
    """Configure root logging once, with a compact structured-ish format."""
    logging.basicConfig(
        level=settings.nexus_log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )


def build_agent(settings: Settings, engine: AsyncEngine | None) -> NexusAgent:
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
    )


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the agent on startup and release resources on shutdown."""
    settings = get_settings()
    configure_logging(settings)

    engine = await _try_connect(settings.database_url, settings.nexus_db_echo)
    app.state.engine = engine
    app.state.database_ok = engine is not None

    # --- Identity (Part 3) -------------------------------------------------
    # Owner-scoped Ed25519 identity. A corrupted identity (wrong secret,
    # mismatched keypair) aborts startup: silently regenerating would break
    # all future trust relationships. Without a database, identity is
    # unavailable and the app still runs (identity endpoints report 503).
    session_factory = None
    identity_service: IdentityService | None = None
    if engine is not None:
        session_factory = create_session_factory(engine)
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
            public_identity = await identity_service.initialize_identity()
            logger.info(
                "identity_ready agent_id=%s fingerprint=%s",
                public_identity.agent_id,
                public_identity.fingerprint,
            )
        except IdentityCorruptionError as exc:
            logger.critical("identity_verification_failure detail=%s", exc)
            await engine.dispose()
            raise RuntimeError(f"Fatal identity error: {exc}") from exc
    app.state.identity_service = identity_service
    app.state.identity_ok = identity_service is not None and identity_service.ready

    # --- Policy & Consent (Part 4) ------------------------------------------
    # Deterministic authorization over policies/consents; requires the DB.
    policy_service: PolicyService | None = None
    if engine is not None:
        policy_service = PolicyService(session_factory=session_factory)
    app.state.policy_service = policy_service
    app.state.policy_ok = policy_service is not None

    # --- Tools (Part 5) --------------------------------------------------------
    # MCP-compatible registry + policy-gated execution service. Application-
    # scoped: one registry per process, built-ins registered at startup.
    tool_service: ToolService | None = None
    if engine is not None and policy_service is not None:
        registry = ToolRegistry()
        for builtin in BUILTIN_TOOLS:
            registry.register(builtin)
        tool_service = ToolService(
            registry=registry,
            policy_service=policy_service,
            session_factory=session_factory,
            timeout_seconds=settings.nexus_tool_timeout_seconds,
            max_result_bytes=settings.nexus_tool_max_result_bytes,
        )
    app.state.tool_service = tool_service
    app.state.tools_ok = tool_service is not None

    app.state.agent = build_agent(settings, engine)

    # --- A2A (Part 6) -----------------------------------------------------------
    # Secure agent-to-agent communication. Requires identity (signing),
    # policy (authorization), and the database; the memory manager is reused
    # from the agent for policy-gated disclosures.
    a2a_service: A2AService | None = None
    if (
        engine is not None
        and identity_service is not None
        and identity_service.ready
        and policy_service is not None
    ):
        a2a_service = A2AService(
            session_factory=session_factory,
            identity_service=identity_service,
            policy_service=policy_service,
            memory_manager=app.state.agent._memory,
            transport=HttpA2ATransport(
                timeout_seconds=settings.nexus_a2a_timeout_seconds,
                max_response_bytes=settings.nexus_a2a_max_message_bytes,
                allow_local=settings.nexus_a2a_allow_local_endpoints,
            ),
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
    app.state.a2a_service = a2a_service
    app.state.a2a_ok = a2a_service is not None

    # --- Discovery (Part 7) ---------------------------------------------------
    # Agent discovery and card hosting. Requires identity (card signing) and
    # A2A (trusted-agent registration). Falls back gracefully when unavailable.
    from app.a2a.discovery import DiscoveryService

    discovery_service: DiscoveryService | None = None
    if a2a_service is not None and identity_service is not None:
        discovery_service = DiscoveryService(
            identity_service=identity_service,
            a2a_service=a2a_service,
            allow_local_endpoints=settings.nexus_a2a_allow_local_endpoints,
            timeout_seconds=settings.nexus_discovery_timeout_seconds,
            max_card_bytes=settings.nexus_discovery_max_card_bytes,
        )
    app.state.discovery_service = discovery_service
    app.state.discovery_ok = discovery_service is not None

    # --- Workflows (Part 9) ---------------------------------------------------
    from app.workflows.service import WorkflowService

    workflow_service: WorkflowService | None = None
    if session_factory is not None and policy_service is not None:
        workflow_service = WorkflowService(
            session_factory=session_factory,
            policy_service=policy_service,
            tool_service=tool_service,
            a2a_service=a2a_service,
            memory_manager=app.state.agent._memory,
            identity_service=identity_service,
            default_ttl_seconds=settings.nexus_workflow_default_ttl_seconds,
            max_step_attempts=settings.nexus_workflow_max_step_attempts,
        )
    app.state.workflow_service = workflow_service
    app.state.workflows_ok = workflow_service is not None

    # --- Autonomy & Decision Engine (Part 10) ---------------------------------
    from app.autonomy.service import AutonomyService

    autonomy_service: AutonomyService | None = None
    if session_factory is not None and policy_service is not None:
        autonomy_service = AutonomyService(
            session_factory=session_factory,
            policy_service=policy_service,
            tool_service=tool_service,
            a2a_service=a2a_service,
            workflow_service=workflow_service,
            memory_manager=app.state.agent._memory,
        )
        try:
            reconciled = await autonomy_service.reconcile_on_startup()
            if reconciled > 0:
                logger.info("autonomy_crash_recovery_completed count=%d", reconciled)
        except Exception as exc:
            logger.warning("autonomy_startup_reconciliation_warning: %s", exc)

    app.state.autonomy_service = autonomy_service
    app.state.autonomy_ok = autonomy_service is not None

    logger.info(
        "nexus_started env=%s provider=%s model=%s base_url=%s llm_configured=%s "
        "database=%s memory=%s embedding=%s identity=%s tools=%s a2a=%s discovery=%s workflows=%s autonomy=%s",
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
    )
    if not settings.llm_configured:
        logger.warning(
            "nexus_llm_not_configured detail=NEXUS_LLM_API_KEY is not set; "
            "chat requests will return 503 until it is configured."
        )

    try:
        yield
    finally:
        await app.state.agent.aclose()
        if app.state.engine is not None:
            await app.state.engine.dispose()
        logger.info("nexus_stopped")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Nexus Runtime",
        description=(
            "Personal AI agent core (Part 2): persistent conversational "
            "sessions and long-term personal memory on PostgreSQL + pgvector."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3001",
            "*",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(chat_router)
    app.include_router(memories_router)
    app.include_router(identity_router)
    app.include_router(policy_router)
    app.include_router(tools_router)
    app.include_router(a2a_router)
    app.include_router(discovery_router)
    app.include_router(tasks_router)
    app.include_router(workflows_router)
    app.include_router(autonomy_router)

    @app.exception_handler(HTTPDependencyError)
    async def _dependency_handler(
        request: Request, exc: HTTPDependencyError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": "service_unavailable", "detail": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return a clean 422 envelope instead of FastAPI's default body.

        Pydantic v2 error dicts can carry the original exception object in
        ``ctx``; encode defensively so the response stays JSON-safe."""
        from fastapi.encoders import jsonable_encoder

        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": "Request body failed validation.",
                "errors": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """Last-resort handler: log the trace, return a generic message."""
        logger.exception("unhandled_error path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": "An unexpected error occurred.",
            },
        )

    return app


app = create_app()


__all__ = ["app", "build_agent", "configure_logging", "create_app"]
