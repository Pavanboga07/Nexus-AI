"""Web search providers (Phase D).

``SearchProvider`` is the seam: chat tools and the ``information.search``
capability call it, starting with the keyless DuckDuckGo implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SearchResult:
    """One normalized search hit."""

    title: str
    url: str
    snippet: str


class SearchError(Exception):
    """Base error for search provider failures."""


class SearchTimeoutError(SearchError):
    """The provider did not respond within its timeout (after a retry)."""


class SearchProvider(ABC):
    """Async web search returning normalized results."""

    @abstractmethod
    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        """Search the web; at most ``count`` results."""
        raise NotImplementedError


def get_provider(
    name: str = "duckduckgo", tavily_key: str | None = None
) -> SearchProvider:
    """Return the named provider (default: keyless DuckDuckGo)."""
    if name == "duckduckgo":
        # lazy: avoids circular import app.search.duckduckgo -> app.search
        # (the provider module imports names from this package __init__).
        from app.search.duckduckgo import DuckDuckGoProvider

        return DuckDuckGoProvider()
    if name == "tavily":
        if not tavily_key:
            raise ValueError(
                "Tavily search provider requires an API key: pass "
                "tavily_key=... or set NEXUS_TAVILY_API_KEY."
            )
        raise NotImplementedError("Tavily search provider is not implemented yet.")
    raise ValueError(
        f"Unknown search provider: {name!r} (expected 'duckduckgo' or 'tavily')."
    )


def get_provider_from_settings(settings: Any) -> SearchProvider:
    """Return the search provider selected in app settings.

    Default is keyless DuckDuckGo. Selecting ``tavily`` without
    ``NEXUS_TAVILY_API_KEY`` raises the same clear error as
    :func:`get_provider`.
    """
    name = getattr(settings, "nexus_search_provider", "duckduckgo")
    key = getattr(settings, "nexus_tavily_api_key", None)
    return get_provider(name, tavily_key=key)


__all__ = [
    "SearchError",
    "SearchProvider",
    "SearchResult",
    "SearchTimeoutError",
    "get_provider",
    "get_provider_from_settings",
]
