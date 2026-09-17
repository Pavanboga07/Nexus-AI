"""Shared test fixtures.

Two stacks:

1. In-memory stack (Part 1 behaviour): ``fake_provider`` -> ``agent`` ->
   ``app`` -> ``client``. No database, no network.
2. Database stack (Part 2): ``db_engine`` -> ``db_session_factory`` ->
   ``db_agent`` (with memory) -> ``db_app`` -> ``db_client``. Requires the
   local PostgreSQL container; skips gracefully when unreachable.

Tests NEVER call the real OpenAI/Groq API: the LLM is a :class:`FakeProvider`
and embeddings use the deterministic local-hash provider.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.agent import NexusAgent
from app.agent.context import ContextBuilder
from app.agent.session import InMemorySessionStore
from app.database.connection import (
    create_engine,
    create_session_factory,
)
from app.database.models import Base, Owner
from app.database.session_store import DatabaseSessionStore
from app.llm.base import (
    LLMConfigurationError,
    LLMProvider,
    LLMProviderError,
    LLMTimeoutError,
    Message,
)
from app.main import create_app
from app.memory.embeddings import LocalHashEmbeddingProvider
from app.memory.manager import MemoryManager

TEST_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5433/nexus_test"


class FakeProvider(LLMProvider):
    """Deterministic in-memory provider for tests.

    ``reply`` is returned by default. If ``script`` (a list of strings) is
    set, each ``generate`` call pops the next scripted reply instead - use
    this to script a chat reply followed by a memory-extraction JSON reply.
    ``calls`` records every context the agent built.
    """

    name = "fake"

    def __init__(self, reply: str = "Hello! How can I help?") -> None:
        self.reply = reply
        self.script: list[str] = []
        self.calls: list[list[Message]] = []
        self.error: Exception | None = None
        self.closed = False

    async def generate(self, messages: list[Message]) -> str:
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        if self.script:
            return self.script.pop(0)
        return self.reply

    async def aclose(self) -> None:
        self.closed = True


# --- Part 1: in-memory stack ------------------------------------------------


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def agent(fake_provider: FakeProvider) -> NexusAgent:
    return NexusAgent(
        provider=fake_provider,
        sessions=InMemorySessionStore(max_messages=100),
        context_builder=ContextBuilder(system_prompt="You are Nexus."),
    )


@pytest.fixture
def app(agent: NexusAgent) -> FastAPI:
    """App with the fake agent installed, bypassing lifespan/network setup."""
    application = create_app()
    application.state.agent = agent
    application.state.engine = None
    application.state.database_ok = False
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


# --- Part 2: database stack -------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine | None]:
    """Engine against a scratch database; skip when PG is down.

    Creates ``nexus_test`` in the running container, builds the schema with
    ``Base.metadata.create_all`` (the Alembic migration is verified against
    the real database separately), and drops everything on session end.
    """
    admin_engine = create_engine(
        "postgresql+asyncpg://nexus:nexus@localhost:5433/nexus"
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text("COMMIT"))
            await conn.execute(
                text("DROP DATABASE IF EXISTS nexus_test WITH (FORCE)")
            )
            await conn.execute(text("CREATE DATABASE nexus_test"))
    except Exception:
        await admin_engine.dispose()
        pytest.skip("PostgreSQL (nexus_postgres container) is not available")
    await admin_engine.dispose()

    # The vector extension is per-database; create_all() does not add it.
    bootstrap_engine = create_engine(TEST_DATABASE_URL)
    async with bootstrap_engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await bootstrap_engine.dispose()

    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(
    db_engine: AsyncEngine,
) -> AsyncIterator:
    """A session factory with FULL table isolation between tests.

    This used to truncate a hardcoded list of four tables
    (``memories, messages, conversations, owners``). That list stopped covering
    the schema many parts ago, so rows in newer tables (jobs, agents, policies,
    workflows, trusted_agent_cards, auth_sessions, ...) leaked between tests -
    which is how a test can pass alone and fail in a suite.

    Truncation is derived from the model metadata, so a new table is covered
    automatically instead of silently reintroducing leakage.
    """
    factory = create_session_factory(db_engine)

    async def _truncate_all() -> None:
        from app.database.models import Base

        tables = ", ".join(
            f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables)
        )
        if not tables:
            return
        async with db_engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))

    # Clean on the way IN as well as out: a previous interrupted run must not
    # poison this one.
    await _truncate_all()
    yield factory
    await _truncate_all()


@pytest_asyncio.fixture
async def db_store(db_session_factory) -> DatabaseSessionStore:
    return DatabaseSessionStore(session_factory=db_session_factory, max_messages=100)


@pytest.fixture
def embeddings() -> LocalHashEmbeddingProvider:
    return LocalHashEmbeddingProvider()


@pytest_asyncio.fixture
async def memory_manager(db_session_factory, embeddings) -> MemoryManager:
    return MemoryManager(
        session_factory=db_session_factory,
        embeddings=embeddings,
        dedup_threshold=0.92,
    )


@pytest_asyncio.fixture
async def policy_service(db_session_factory):
    from app.policy.service import PolicyService

    return PolicyService(session_factory=db_session_factory)


@pytest_asyncio.fixture
async def db_agent(
    fake_provider: FakeProvider, db_store, memory_manager
):
    agent = NexusAgent(
        provider=fake_provider,
        sessions=db_store,
        context_builder=ContextBuilder(system_prompt="You are Nexus."),
        memory_manager=memory_manager,
        memory_top_k=5,
        memory_enabled=True,
    )
    yield agent
    # Drain background extraction tasks so they never race the teardown
    # TRUNCATE of the next test.
    await agent.aclose()


@pytest_asyncio.fixture
async def db_owner_id(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as session:
        owner = Owner(name="test-owner")
        session.add(owner)
        await session.commit()
        return owner.id


@pytest_asyncio.fixture
async def owner_ids(db_session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Two fresh owners for isolation tests."""
    async with db_session_factory() as session:
        a = Owner(name="owner-a")
        b = Owner(name="owner-b")
        session.add_all([a, b])
        await session.commit()
        return a.id, b.id


@pytest.fixture
def db_app(db_agent: NexusAgent) -> FastAPI:
    application = create_app()
    application.state.agent = db_agent
    application.state.engine = None
    application.state.database_ok = True
    return application


@pytest.fixture
def db_policy_app(db_app: FastAPI, db_session_factory) -> FastAPI:
    """DB app with the policy service attached (Part 4 routes)."""
    from app.policy.service import PolicyService

    db_app.state.policy_service = PolicyService(session_factory=db_session_factory)
    return db_app


@pytest.fixture
def db_tools_app(db_policy_app: FastAPI, db_session_factory) -> FastAPI:
    """DB app with policy + tool services attached (Part 5 routes)."""
    from app.tools.builtin import BUILTIN_TOOLS
    from app.tools.registry import ToolRegistry
    from app.tools.service import ToolService

    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    db_policy_app.state.tool_service = ToolService(
        registry=registry,
        policy_service=db_policy_app.state.policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=4096,
    )
    return db_policy_app


@pytest_asyncio.fixture
async def db_tools_client(db_tools_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_tools_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def db_a2a_app(
    db_tools_app: FastAPI, db_session_factory, memory_manager
) -> FastAPI:
    """DB app with identity + policy + A2A services attached (Part 6 routes).

    The identity uses the SAME default owner the agent resolves, so policy,
    memory, and identity all share one owner scope.
    """
    from app.a2a.rate_limit import SlidingWindowRateLimiter
    from app.a2a.service import A2AService
    from app.database.repositories import OwnerRepository
    from app.identity.service import IdentityService
    from tests.test_a2a_service import LoopbackTransport

    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        await session.commit()
        owner_id = owner.id

    identity_service = IdentityService(
        session_factory=db_session_factory,
        encryption_secret="a2a-api-test-secret-not-real",
        owner_id=owner_id,
    )
    # M4: identity is per-agent. Initialise the owner's primary agent and bind
    # the adapter the protocol stack consumes (app.state.primary_identity).
    from app.identity.bound import AgentIdentity

    agent_summary = await identity_service.initialize_primary_agent(owner_id)
    primary_identity = await AgentIdentity.for_agent(
        identity_service=identity_service,
        agent_row_id=agent_summary.id,
    )

    a2a_service = A2AService(
        session_factory=db_session_factory,
        identity_service=primary_identity,
        policy_service=db_tools_app.state.policy_service,
        memory_manager=memory_manager,
        transport=LoopbackTransport(),
        rate_limiter=SlidingWindowRateLimiter(60),
        allow_local_endpoints=True,
    )
    db_tools_app.state.identity_service = identity_service
    db_tools_app.state.primary_identity = primary_identity
    db_tools_app.state.identity_ok = True
    db_tools_app.state.a2a_service = a2a_service
    return db_tools_app


@pytest_asyncio.fixture
async def db_a2a_client(db_a2a_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_a2a_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def db_policy_client(db_policy_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_policy_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def db_client(db_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


# --- M3: authentication stack -----------------------------------------------

#: Fixed secret so session cookies are deterministic across test runs.
TEST_SESSION_SECRET = "test-session-secret-not-real-do-not-use"


@pytest.fixture
def authed_app(db_app: FastAPI, db_session_factory) -> FastAPI:
    """App with the auth service attached and auth ENFORCED AND THE REAL
    SERVICES ATTACHED.

    Existing fixtures bypass auth (they set app.state.agent directly and leave
    auth_required=False, which exercises the development fallback). Tests that
    need real authentication use this fixture.

    Identity and policy services are attached so the protected endpoints reach
    their real handlers (rather than 503-ing on a missing dependency); that is
    what makes the 401-vs-200 assertions meaningful rather than vacuous.
    """
    from app.auth.service import AuthService
    from app.database.repositories import OwnerRepository
    from app.identity.service import IdentityService
    from app.policy.service import PolicyService
    from app.database.models import Owner

    # A dedicated owner row for identity binding in this fixture (distinct from
    # the per-registration owners the auth tests create).
    db_app.state.auth_service = AuthService(
        session_factory=db_session_factory,
        session_secret=TEST_SESSION_SECRET,
        session_ttl_seconds=3600,
        allow_registration=True,
    )
    db_app.state.auth_required = True
    db_app.state.registration_open = True
    db_app.state.cookie_secure = False  # plain-HTTP test transport
    db_app.state.session_ttl_seconds = 3600
    db_app.state.adopt_legacy_owner = False  # each test user gets its own owner

    db_app.state.policy_service = PolicyService(session_factory=db_session_factory)
    return db_app


@pytest_asyncio.fixture
async def authed_client(authed_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def auth_service_standalone(db_session_factory):
    """Bare AuthService for unit-level auth tests."""
    from app.auth.service import AuthService

    return AuthService(
        session_factory=db_session_factory,
        session_secret=TEST_SESSION_SECRET,
        session_ttl_seconds=3600,
        allow_registration=True,
    )


@pytest_asyncio.fixture
async def db_workflow_app(
    db_a2a_app: FastAPI, db_session_factory, memory_manager
) -> FastAPI:
    """DB app with identity + policy + tools + A2A + workflow services attached (Part 9 routes)."""
    from app.workflows.service import WorkflowService

    workflow_service = WorkflowService(
        session_factory=db_session_factory,
        policy_service=db_a2a_app.state.policy_service,
        tool_service=db_a2a_app.state.tool_service,
        a2a_service=db_a2a_app.state.a2a_service,
        memory_manager=memory_manager,
        identity_service=db_a2a_app.state.identity_service,
    )
    db_a2a_app.state.workflow_service = workflow_service
    return db_a2a_app


@pytest_asyncio.fixture
async def db_workflow_client(
    db_workflow_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_workflow_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def db_autonomy_app(
    db_workflow_app: FastAPI, db_session_factory, memory_manager
) -> FastAPI:
    """DB app with workflow + autonomy services attached (Part 10 routes)."""
    from app.autonomy.service import AutonomyService

    autonomy_service = AutonomyService(
        session_factory=db_session_factory,
        policy_service=db_workflow_app.state.policy_service,
        tool_service=db_workflow_app.state.tool_service,
        a2a_service=db_workflow_app.state.a2a_service,
        workflow_service=db_workflow_app.state.workflow_service,
        memory_manager=memory_manager,
    )
    db_workflow_app.state.autonomy_service = autonomy_service
    return db_workflow_app


@pytest_asyncio.fixture
async def db_autonomy_client(
    db_autonomy_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_autonomy_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


@pytest.fixture
def db_orchestration_app(
    db_autonomy_app: FastAPI,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """DB app with orchestration service attached (Part 12 routes)."""
    from app.orchestration.intent import IntentResolver
    from app.orchestration.orchestrator import AgentOrchestrator
    from app.orchestration.target_resolver import TargetResolver

    intent_resolver = IntentResolver(llm_provider=db_autonomy_app.state.agent._provider)
    target_resolver = TargetResolver(
        session_factory=db_session_factory,
        trusted_agents=db_autonomy_app.state.a2a_service._trusted,
        discovery_service=getattr(db_autonomy_app.state, "discovery_service", None),
        memory_manager=db_autonomy_app.state.agent._memory,
    )
    decision_engine = getattr(db_autonomy_app.state.autonomy_service, "_engine", None)
    orchestrator = AgentOrchestrator(
        session_factory=db_session_factory,
        a2a_service=db_autonomy_app.state.a2a_service,
        policy_service=db_autonomy_app.state.policy_service,
        intent_resolver=intent_resolver,
        target_resolver=target_resolver,
        decision_engine=decision_engine,
    )
    db_autonomy_app.state.orchestrator = orchestrator
    return db_autonomy_app


@pytest_asyncio.fixture
async def db_orchestration_client(
    db_orchestration_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=db_orchestration_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


__all__ = [
    "FakeProvider",
    "LLMConfigurationError",
    "LLMProviderError",
    "LLMTimeoutError",
    "TEST_DATABASE_URL",
]
