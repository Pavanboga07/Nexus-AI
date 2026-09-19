"""Task D3: web_search + web_fetch tools via real ToolService (PostgreSQL required)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.policy.service import PolicyService
from app.search import SearchProvider, SearchResult, SearchTimeoutError
from app.search.fetch import FetchError
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolInvocation
from app.tools.service import ToolService
from app.tools.builtin_search import WebFetchTool, WebSearchTool

pytestmark = pytest.mark.asyncio


class StubProvider(SearchProvider):
    """Stubbed search provider: records calls, returns canned results."""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._results = results if results is not None else [
            SearchResult(title="Example", url="https://example.com/", snippet="A snippet."),
        ]

    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        self.calls.append((query, count))
        return self._results[:count]


@pytest.fixture
def stub_provider() -> StubProvider:
    return StubProvider()


@pytest.fixture
def registry(stub_provider: StubProvider) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(WebSearchTool(provider=stub_provider))
    reg.register(WebFetchTool(fetch_fn=_ok_fetch))
    return reg


async def _ok_fetch(url: str):
    from app.search.fetch import FetchedPage

    return FetchedPage(url=url, title="T", text="body")


@pytest_asyncio.fixture
async def policy_service(db_session_factory) -> PolicyService:
    return PolicyService(session_factory=db_session_factory)


@pytest_asyncio.fixture
async def tool_service(db_session_factory, registry, policy_service) -> ToolService:
    return ToolService(
        registry=registry,
        policy_service=policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=65536,
    )


async def _allow_search(policy: PolicyService, owner_id: uuid.UUID, purpose="testing"):
    await policy.create_policy(
        owner_id,
        requester_agent_id="nexus:self",
        data_category="public-web",
        action="access_tool",
        purpose=purpose,
        decision="ALLOW",
    )


async def test_web_search_returns_results(tool_service, policy_service, owner_ids, stub_provider) -> None:
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_search",
            arguments={"query": "nexus ai", "count": 1},
            purpose="testing",
        ),
    )
    assert result.success is True
    assert result.status == "executed"
    assert result.data is not None
    assert result.data["results"][0]["url"] == "https://example.com/"
    assert stub_provider.calls == [("nexus ai", 1)]


async def test_web_search_empty_query_rejected(tool_service, policy_service, owner_ids, stub_provider) -> None:
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_search",
            arguments={"query": ""},
            purpose="testing",
        ),
    )
    assert result.success is False
    assert result.error.code == "INVALID_ARGUMENTS"
    assert stub_provider.calls == []


async def test_web_search_denied_never_touches_provider(
    tool_service, policy_service, owner_ids, stub_provider
) -> None:
    owner_a, _ = owner_ids
    await policy_service.create_policy(
        owner_a,
        requester_agent_id="nexus:self",
        data_category="public-web",
        action="access_tool",
        purpose="testing",
        decision="DENY",
        priority=10,
    )
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_search",
            arguments={"query": "hello"},
            purpose="testing",
        ),
    )
    assert result.status == "denied"
    assert result.data is None
    assert stub_provider.calls == []


async def test_web_fetch_disallowed_url_clean_error(
    tool_service, policy_service, owner_ids, registry
) -> None:
    async def _blocked(url: str):
        raise FetchError(f"Blocked URL {url!r}: private range.")

    registry.unregister("web_fetch")
    registry.register(WebFetchTool(fetch_fn=_blocked))
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    # Must not raise into the caller: a failed ToolResult instead.
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_fetch",
            arguments={"url": "http://127.0.0.1/secret"},
            purpose="testing",
        ),
    )
    assert result.success is False
    assert result.status == "failed"
    assert result.error is not None


async def test_web_fetch_success_shape(tool_service, policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_fetch",
            arguments={"url": "https://example.com/"},
            purpose="testing",
        ),
    )
    assert result.success is True
    assert result.status == "executed"
    assert result.data is not None
    assert set(result.data) >= {"url", "title", "text"}
    assert isinstance(result.data["url"], str)
    assert isinstance(result.data["title"], str)
    assert isinstance(result.data["text"], str)


class _TimeoutProvider(SearchProvider):
    """Provider stub that always times out."""

    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        raise SearchTimeoutError("timed out")


async def test_web_search_timeout_maps_to_timeout_code(
    tool_service, policy_service, owner_ids, registry
) -> None:
    registry.unregister("web_search")
    registry.register(WebSearchTool(provider=_TimeoutProvider()))
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_search",
            arguments={"query": "nexus ai"},
            purpose="testing",
        ),
    )
    assert result.success is False
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


async def test_web_fetch_timeout_maps_to_timeout_code(
    tool_service, policy_service, owner_ids, registry
) -> None:
    async def _timed_out(url: str):
        raise SearchTimeoutError("timed out")

    registry.unregister("web_fetch")
    registry.register(WebFetchTool(fetch_fn=_timed_out))
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="web_fetch",
            arguments={"url": "https://example.com/"},
            purpose="testing",
        ),
    )
    assert result.success is False
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


async def test_web_search_count_out_of_bounds_rejected(
    tool_service, policy_service, owner_ids, stub_provider
) -> None:
    owner_a, _ = owner_ids
    await _allow_search(policy_service, owner_a)
    for count in (0, 99):
        result = await tool_service.execute(
            owner_a,
            ToolInvocation(
                tool_name="web_search",
                arguments={"query": "nexus ai", "count": count},
                purpose="testing",
            ),
        )
        assert result.success is False
        assert result.error is not None
        assert result.error.code == "INVALID_ARGUMENTS"
    assert stub_provider.calls == []


async def test_tools_listing_shows_both(tool_service) -> None:
    reg = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        reg.register(tool)
    listed = {t["name"] for t in reg.list_tools()}
    assert "web_search" in listed
    assert "web_fetch" in listed
    via_service = {t["name"] for t in tool_service.list_tools()}
    assert "web_search" in via_service
    assert "web_fetch" in via_service
