"""Task D2: guarded page-fetch tests.

No network in CI: a loopback ``http.server`` fixture serves a known HTML
page, an oversized body, and redirect chains. The SSRF test asserts the
private-range URL raises via ``validate_endpoint`` BEFORE any socket opens
(the fixture server records zero hits).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.a2a.errors import A2AError
from app.a2a.transport import validate_endpoint
from app.search import SearchError
from app.search.fetch import FetchedPage, FetchError, fetch_url

PAGE_HTML = (
    b"<!DOCTYPE html><html><head><title>Known Page</title>"
    b"<style>body { color: red; }</style>"
    b'<script>alert("boilerplate");</script>'
    b"</head><body>"
    b'<nav><a href="/x">Nav boilerplate link</a></nav>'
    b"<article><h1>Headline</h1>"
    b"<p>The quick brown fox jumps over the lazy dog.</p></article>"
    b"</body></html>"
)

FINAL_HTML = (
    b"<html><head><title>Final</title></head>"
    b"<body><p>Reached the final page.</p></body></html>"
)

BIG_HTML = (
    b"<html><head><title>Big</title></head><body><p>"
    + b"x" * 200_000
    + b"</p></body></html>"
)


class _FetchHandler(BaseHTTPRequestHandler):
    pages: dict[str, bytes] = {}
    redirects: dict[str, str] = {}
    hits: list[str] = []

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        if self.path in type(self).redirects:
            target = type(self).redirects[self.path]
            body = b"redirect"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        data = type(self).pages.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
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
def fetch_server():
    _FetchHandler.pages = {
        "/page": PAGE_HTML,
        "/final": FINAL_HTML,
        "/big": BIG_HTML,
    }
    # Four hops: /r0 -> /r1 -> /r2 -> /r3 -> /final (exceeds the max of 3).
    _FetchHandler.redirects = {
        "/r0": "/r1",
        "/r1": "/r2",
        "/r2": "/r3",
        "/r3": "/final",
        "/short": "/page",
    }
    _FetchHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FetchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    _FetchHandler.pages = {}
    _FetchHandler.redirects = {}
    _FetchHandler.hits = []


def _base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_fetch_error_is_a_search_error() -> None:
    assert issubclass(FetchError, SearchError)


async def test_fetch_returns_title_and_stripped_text(fetch_server) -> None:
    page = await fetch_url(f"{_base_url(fetch_server)}/page", allow_local=True)

    assert isinstance(page, FetchedPage)
    assert page.title == "Known Page"
    assert "The quick brown fox jumps over the lazy dog." in page.text
    # Boilerplate stripped: nav links, scripts, styles.
    assert "Nav boilerplate link" not in page.text
    assert "alert" not in page.text
    assert "color" not in page.text
    assert len(page.text.encode("utf-8")) <= 8 * 1024


async def test_private_url_blocked_before_any_socket(fetch_server) -> None:
    url = f"{_base_url(fetch_server)}/page"
    # validate_endpoint itself refuses the loopback host ...
    with pytest.raises(A2AError):
        validate_endpoint(url, allow_local=False)

    _FetchHandler.hits = []
    # ... and fetch_url raises before any socket opens (zero server hits).
    with pytest.raises(SearchError):
        await fetch_url(url, allow_local=False)
    assert _FetchHandler.hits == []


async def test_oversized_body_truncates_at_cap(fetch_server) -> None:
    page = await fetch_url(f"{_base_url(fetch_server)}/big", allow_local=True)

    assert len(page.text.encode("utf-8")) <= 8 * 1024
    assert page.text.startswith("x")


async def test_redirect_chain_stops_after_three(fetch_server) -> None:
    with pytest.raises(SearchError):
        await fetch_url(f"{_base_url(fetch_server)}/r0", allow_local=True)


async def test_short_redirect_chain_is_followed(fetch_server) -> None:
    page = await fetch_url(f"{_base_url(fetch_server)}/short", allow_local=True)

    assert page.title == "Known Page"
    assert "The quick brown fox" in page.text
