"""Trace propagation and request metrics middleware (M11).

Two middlewares, deliberately separate in intent:

``TraceMiddleware``
    Binds a correlation id for the whole request and echoes it back. It is a
    *pure ASGI* middleware rather than a `BaseHTTPMiddleware` because the latter
    runs the downstream app in a task with a *copied* context, so a `ContextVar`
    set inside it does not reach the endpoint. Binding the trace id would then
    appear to work (the header is echoed) while every log line inside the request
    reported no trace - the exact failure this milestone exists to remove.

``MetricsMiddleware``
    Counts requests and observes duration. It uses the ASGI `scope["route"]`
    template when available, because labelling by raw path would create one
    series per agent id and turn the exposition endpoint into a memory leak.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    TRACE_HEADER,
    new_trace_id,
    reset_trace_id,
    sanitise_trace_id,
    set_trace_id,
)

logger = logging.getLogger("nexus.http")

#: Never labelled with the raw path - see the module docstring.
_UNMATCHED_ROUTE = "<unmatched>"


def _header(scope: Scope, name: str) -> str | None:
    """Case-insensitive header lookup on a raw ASGI scope."""
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or ():
        if key.lower() == target:
            return value.decode("latin-1", errors="replace")
    return None


class TraceMiddleware:
    """Bind `trace_id` for the request, add it to the response headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = _header(scope, TRACE_HEADER)
        trace_id = sanitise_trace_id(supplied) if supplied else new_trace_id()
        # Exposed on the scope so a handler can quote it without importing the
        # contextvar (useful for putting it on an outbound envelope).
        scope.setdefault("state", {})
        scope["state"]["trace_id"] = trace_id
        token = set_trace_id(trace_id)

        header = TRACE_HEADER.lower().encode("latin-1")
        value = trace_id.encode("latin-1")

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for (k, v) in message.get("headers", [])
                    if k.lower() != header
                ]
                headers.append((header, value))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            # Reset before the task can be reused: a leaked trace id would attach
            # one request's id to the next log line on the same task.
            reset_trace_id(token)


class MetricsMiddleware:
    """Count requests and observe their duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_tracked(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_tracked)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or _UNMATCHED_ROUTE
            method = scope.get("method", "GET")
            status_class = f"{status_holder['status'] // 100}xx"
            HTTP_REQUESTS.inc(1, method=method, route=template, status=status_class)
            HTTP_DURATION.observe(
                time.perf_counter() - started, route=template
            )


__all__ = ["MetricsMiddleware", "TraceMiddleware"]
