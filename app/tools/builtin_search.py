"""Web search + fetch tools (Phase D, task D3).

Follow ``CurrentTimeTool`` exactly: ``_StrictModel`` args (extra=forbid),
``data_category = "public-web"``, ``execute(arguments, context)`` returning
JSON-able dicts. Provider + fetch helper are constructor-injected with sane
defaults (keyless DuckDuckGo + ``fetch_url``) so ``BUILTIN_TOOLS`` stays a
no-arg tuple.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.search import (
    SearchError,
    SearchProvider,
    SearchTimeoutError,
    get_provider_from_settings,
)
from app.search.fetch import FetchedPage, fetch_url
from app.tools.errors import ToolError, ToolErrorCode
from app.tools.registry import BaseTool
from app.tools.schemas import ToolContext


class _StrictModel(BaseModel):
    """Arguments model base: rejects unknown fields.

    Local copy of ``app.tools.builtin._StrictModel`` (importing it here
    would be circular once ``builtin/__init__`` imports this module).
    """

    model_config = ConfigDict(extra="forbid")


class WebSearchArgs(_StrictModel):
    query: str = Field(min_length=1, max_length=500)
    count: int = Field(default=5, ge=1, le=10)


class WebFetchArgs(_StrictModel):
    url: str = Field(min_length=1, max_length=2000)


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Discover current or external information to answer a question; "
        "use before answering from memory. Returns titles, URLs, and snippets."
    )
    data_category = "public-web"
    args_model = WebSearchArgs

    def __init__(self, provider: SearchProvider | None = None) -> None:
        if provider is not None:
            self._provider = provider
        else:
            # No-arg default stays intact for BUILTIN_TOOLS: the provider
            # comes from settings (default: keyless DuckDuckGo).
            from app.config.settings import get_settings

            self._provider = get_provider_from_settings(get_settings())

    async def execute(
        self, arguments: WebSearchArgs, context: ToolContext
    ) -> dict[str, Any]:
        try:
            results = await self._provider.search(arguments.query, arguments.count)
        except SearchTimeoutError as exc:
            raise ToolError(
                ToolErrorCode.TIMEOUT, str(exc) or "Web search timed out."
            ) from exc
        except SearchError as exc:
            raise ToolError(
                ToolErrorCode.EXECUTION_ERROR, str(exc) or "Web search failed."
            ) from exc
        return {
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ]
        }


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = (
        "Read the full content of a URL from search results. "
        "Call only with URLs returned by web_search or supplied by the user."
    )
    data_category = "public-web"
    args_model = WebFetchArgs

    def __init__(
        self,
        fetch_fn: Callable[..., Awaitable[FetchedPage]] | None = None,
    ) -> None:
        self._fetch = fetch_fn if fetch_fn is not None else fetch_url

    async def execute(
        self, arguments: WebFetchArgs, context: ToolContext
    ) -> dict[str, Any]:
        try:
            page = await self._fetch(arguments.url)
        except SearchTimeoutError as exc:
            raise ToolError(
                ToolErrorCode.TIMEOUT, str(exc) or "Web fetch timed out."
            ) from exc
        except SearchError as exc:
            raise ToolError(
                ToolErrorCode.EXECUTION_ERROR, str(exc) or "Web fetch failed."
            ) from exc
        return {"url": page.url, "title": page.title, "text": page.text}


__all__ = [
    "WebFetchArgs",
    "WebFetchTool",
    "WebSearchArgs",
    "WebSearchTool",
]
