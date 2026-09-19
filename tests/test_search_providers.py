"""Task D1: search provider tests.

No network in CI: the DuckDuckGo HTML response was recorded once to
``tests/fixtures/ddg_response.html`` and is replayed through a loopback
``http.server`` fixture below.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.search import (
    SearchError,
    SearchProvider,
    SearchResult,
    SearchTimeoutError,
    get_provider,
)
from app.search.duckduckgo import DuckDuckGoProvider

FIXTURE = Path(__file__).parent / "fixtures" / "ddg_response.html"


class _ReplayHandler(BaseHTTPRequestHandler):
    body: bytes = b""
    delay: float = 0.0
    paths: list[str] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        if type(self).delay:
            time.sleep(type(self).delay)
        data = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def loopback_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReplayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    _ReplayHandler.body = b""
    _ReplayHandler.delay = 0.0
    _ReplayHandler.paths = []


def _base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_search_result_shape() -> None:
    result = SearchResult(title="t", url="https://example.com", snippet="s")
    assert result.title == "t"
    assert result.url == "https://example.com"
    assert result.snippet == "s"


def test_provider_is_abstract() -> None:
    with pytest.raises(TypeError):
        SearchProvider()  # type: ignore[abstract]


async def test_ddg_replays_recorded_fixture(loopback_server) -> None:
    _ReplayHandler.body = FIXTURE.read_bytes()
    provider = DuckDuckGoProvider(base_url=_base_url(loopback_server))

    results = await provider.search("python programming", count=5)

    assert len(results) >= 3
    first = results[0]
    assert first.title == "Welcome to Python.org"
    assert first.url == "https://www.python.org/"
    assert "Python Software Foundation" in first.snippet
    for result in results:
        assert result.title
        assert result.url.startswith("http")
    # Query is URL-encoded on the request line.
    assert any("q=python+programming" in path for path in _ReplayHandler.paths)


async def test_ddg_count_limits_results(loopback_server) -> None:
    _ReplayHandler.body = FIXTURE.read_bytes()
    provider = DuckDuckGoProvider(base_url=_base_url(loopback_server))

    results = await provider.search("python programming", count=2)
    assert len(results) == 2


async def test_ddg_timeout_surfaces_typed_error(loopback_server) -> None:
    _ReplayHandler.body = b"<html></html>"
    _ReplayHandler.delay = 2.0
    provider = DuckDuckGoProvider(base_url=_base_url(loopback_server), timeout=0.3)

    with pytest.raises(SearchTimeoutError):
        await provider.search("python programming")


def test_timeout_error_is_a_search_error() -> None:
    assert issubclass(SearchTimeoutError, SearchError)


def test_factory_defaults_to_duckduckgo() -> None:
    assert isinstance(get_provider(), DuckDuckGoProvider)
    assert isinstance(get_provider("duckduckgo"), DuckDuckGoProvider)


def test_factory_tavily_without_key_raises() -> None:
    with pytest.raises(ValueError, match="[Aa][Pp][Ii] key"):
        get_provider("tavily")


# --- Task D7: settings wiring -------------------------------------------------


def test_settings_search_provider_defaults_to_duckduckgo(monkeypatch) -> None:
    from app.config.settings import Settings

    monkeypatch.delenv("NEXUS_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_TAVILY_API_KEY", raising=False)
    settings = Settings(_env_file=None)

    assert settings.nexus_search_provider == "duckduckgo"
    assert settings.nexus_tavily_api_key is None


def test_provider_from_settings_uses_configured_default(monkeypatch) -> None:
    from app.config.settings import Settings
    from app.search import get_provider_from_settings

    monkeypatch.delenv("NEXUS_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_TAVILY_API_KEY", raising=False)

    provider = get_provider_from_settings(Settings(_env_file=None))

    assert isinstance(provider, DuckDuckGoProvider)


def test_provider_from_settings_tavily_without_key_raises() -> None:
    from app.config.settings import Settings
    from app.search import get_provider_from_settings

    settings = Settings(
        _env_file=None,
        nexus_search_provider="tavily",
        nexus_tavily_api_key=None,
    )
    with pytest.raises(ValueError, match="[Aa][Pp][Ii] key"):
        get_provider_from_settings(settings)


def test_web_search_tool_default_comes_from_settings(monkeypatch) -> None:
    from app.config.settings import get_settings
    from app.tools.builtin_search import WebSearchTool

    monkeypatch.delenv("NEXUS_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_TAVILY_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert isinstance(WebSearchTool()._provider, DuckDuckGoProvider)
    finally:
        get_settings.cache_clear()
