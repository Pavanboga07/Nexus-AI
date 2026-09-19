"""Guarded page fetch (Phase D, task D2).

``fetch_url`` validates the URL with ``validate_endpoint`` FIRST (SSRF
protection reused verbatim from ``app.a2a.transport``), then reads through
a module-level pooled ``httpx.AsyncClient`` (follows redirects, max 3,
timeout 10s) with a streaming read capped at 8 KB. Boilerplate
(``script``/``style``/``nav``) is stripped with stdlib ``html.parser`` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from app.a2a.errors import A2AError
from app.a2a.transport import validate_endpoint
from app.search import SearchError

MAX_BYTES = 8 * 1024

_client = httpx.AsyncClient(
    follow_redirects=True,
    max_redirects=3,
    timeout=10.0,
)

_WHITESPACE = re.compile(r"\s+")
_SKIP_TAGS = {"script", "style", "nav"}


class FetchError(SearchError):
    """A page fetch failed (blocked URL, transport error, too many redirects)."""


@dataclass
class FetchedPage:
    """Extracted page text."""

    url: str
    title: str
    text: str


class _TextParser(HTMLParser):
    """Collect ``<title>`` + visible text, skipping boilerplate subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title" and self._skip_depth == 0:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._parts.append(data)

    @property
    def title(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self._title_parts)).strip()

    @property
    def text(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self._parts)).strip()


async def fetch_url(url: str, *, allow_local: bool = False) -> FetchedPage:
    """Fetch a page: SSRF-validated, redirect-bounded, size-capped, stripped."""
    try:
        validate_endpoint(url, allow_local=allow_local)
    except A2AError as exc:
        raise FetchError(f"Blocked URL {url!r}: {exc}") from exc

    try:
        async with _client.stream(
            "GET",
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as response:
            if response.status_code >= 400:
                raise FetchError(
                    f"Fetch failed with HTTP {response.status_code} for {url!r}."
                )
            # Content-Length pre-check: a huge (or lying) header never
            # changes the outcome — the streaming loop below caps anyway.
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                remaining = MAX_BYTES - len(raw)
                if remaining <= 0:
                    break
                raw += chunk[:remaining]
                if len(raw) >= MAX_BYTES:
                    break
            final_url = str(response.url)
    except httpx.HTTPError as exc:
        raise FetchError(f"Fetch failed for {url!r} ({type(exc).__name__}).") from exc

    parser = _TextParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    return FetchedPage(url=final_url, title=parser.title, text=parser.text)


__all__ = ["FetchError", "FetchedPage", "MAX_BYTES", "fetch_url"]
