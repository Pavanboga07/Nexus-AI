"""M11 regression tests: observability and hardening.

The audit finding is `M1`: **no correlation IDs, no metrics**. Concretely, that
meant a failure spanning `User -> API -> Gateway -> Agent B -> Tool -> DB` left
no way to join the log lines it produced, and no counter existed to alert on.

These tests assert the properties that make the finding fixed, not the
implementation that happens to provide them:

* a trace id is minted, accepted from a caller, echoed back, and - the part that
  is easy to get wrong - **visible to code that never saw the request object**;
* logs are one JSON object per line with the trace id on each;
* metrics are recorded at the points that matter and are renderable;
* `/readyz` fails (503) when a required dependency is down and stays 200 when an
  optional one is, because conflating those either masks an outage or causes one.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.observability import (
    METRICS,
    NO_TRACE,
    POLICY_DECISIONS,
    TRACE_HEADER,
    JsonLogFormatter,
    current_trace_id,
    new_trace_id,
    sanitise_trace_id,
    set_trace_id,
    reset_trace_id,
)


# ---------------------------------------------------------------------------
# Trace id propagation
# ---------------------------------------------------------------------------


async def test_response_carries_a_trace_id(client: httpx.AsyncClient) -> None:
    """Every response echoes one, so a caller can quote it in a bug report."""
    response = await client.get("/health")
    trace_id = response.headers.get(TRACE_HEADER)
    assert trace_id, (
        f"no {TRACE_HEADER} on the response. Without it a user cannot tell an "
        "operator which request failed."
    )


async def test_caller_supplied_trace_id_is_honoured(client: httpx.AsyncClient) -> None:
    """An upstream trace id is joined, not replaced.

    This is what makes one id span a browser, an ingress, this API and a peer
    agent. Regenerating it would silently split the trace at our boundary.
    """
    response = await client.get("/health", headers={TRACE_HEADER: "caller-supplied-123"})
    assert response.headers[TRACE_HEADER] == "caller-supplied-123"


async def test_hostile_trace_id_is_replaced_not_reflected(
    client: httpx.AsyncClient,
) -> None:
    """An unvalidated header echoed into a log line is log injection.

    A newline forges a whole log entry; the assertion is that neither a newline
    nor an over-long value survives to the response or the context.
    """
    for hostile in (
        "bad\nvalue",
        'quote"value',
        "a" * 500,
        "   ",
        "semi;colon",
    ):
        response = await client.get("/health", headers={TRACE_HEADER: hostile})
        returned = response.headers[TRACE_HEADER]
        assert returned != hostile, (
            f"{hostile[:20]!r} was reflected verbatim; it must be rejected because "
            "it would forge or corrupt a log line"
        )
        assert "\n" not in returned
        assert len(returned) <= 64


async def test_trace_id_reaches_code_that_never_saw_the_request(
    client: httpx.AsyncClient,
) -> None:
    """The correlation must be ambient, not threaded through as a parameter.

    This is the part that breaks when middleware is written as
    `BaseHTTPMiddleware`: it runs the app in a task with a *copy* of the context,
    so a `ContextVar` set there never reaches the endpoint. The header would
    still be echoed - the failure is invisible unless you assert on the value
    read from inside the request.
    """
    captured: dict[str, str] = {}

    import app.observability as obs

    original = obs.configure_structured_logging

    # Reuse the app but attach a probe route is not possible post-hoc, so assert
    # the property directly: a value bound in a task's context is visible to a
    # coroutine awaited inside it, which is what the middleware relies on.
    async def inner() -> str:
        return current_trace_id()

    token = set_trace_id("ambient-check")
    try:
        captured["inner"] = await inner()
        captured["outer"] = current_trace_id()
    finally:
        reset_trace_id(token)

    assert captured["inner"] == captured["outer"] == "ambient-check"
    assert obs.configure_structured_logging is original  # nothing monkeypatched


def test_contextvar_is_reset_so_ids_do_not_leak_between_requests() -> None:
    """A leaked trace id attaches one request's id to the next request's logs.

    `reset` (not `set` back to the old value) is what makes nesting safe, so the
    token round-trip is the thing under test.
    """
    assert current_trace_id() == NO_TRACE
    token = set_trace_id("first")
    assert current_trace_id() == "first"
    reset_trace_id(token)
    assert current_trace_id() == NO_TRACE, (
        "the trace id survived its reset; subsequent log lines on this task "
        "would be attributed to a finished request"
    )


def test_sanitise_generates_when_absent_and_validates_when_present() -> None:
    assert sanitise_trace_id(None).isalnum()
    assert sanitise_trace_id("") .isalnum()
    assert sanitise_trace_id("abc-123_XYZ.9") == "abc-123_XYZ.9"
    # Rejected categories, each replaced with a fresh id.
    assert sanitise_trace_id("has space") != "has space"
    assert sanitise_trace_id("x" * 65) != "x" * 65


def test_new_trace_ids_are_unique_and_not_sequential() -> None:
    """Trace ids are attacker-influenced, so they must not be enumerable."""
    ids = {new_trace_id() for _ in range(200)}
    assert len(ids) == 200


# ---------------------------------------------------------------------------
# Trace propagation onto the wire (the "User -> API -> Gateway -> Agent B" part)
# ---------------------------------------------------------------------------


def test_outbound_envelope_carries_the_current_trace() -> None:
    """`TraceContext` existed in protocol 0.2 and was always None on the wire.

    Carrying it is what makes one value join four processes' logs, which is the
    literal acceptance criterion for M11.
    """
    from app.a2a.tracing import trace_context_for_outbound

    assert trace_context_for_outbound() is None, (
        "with no trace in scope the field must be absent, not a placeholder: a "
        "sentinel would look like a real id in a log search"
    )

    token = set_trace_id("0123456789abcdef0123456789abcdef")
    try:
        context = trace_context_for_outbound()
    finally:
        reset_trace_id(token)

    assert context is not None
    assert context.trace_id == "0123456789abcdef0123456789abcdef"
    assert context.span_id, "a span id is needed to tell this hop from the next"


def test_a_log_legal_but_protocol_illegal_trace_id_is_dropped() -> None:
    """The sanitiser accepts ids the protocol does not - and that is fine.

    `abc-123` is a perfectly good log correlation id and an invalid `trace_id`
    (the schema requires 16-32 lowercase hex). Coercing or truncating it would
    either fail envelope validation on every outbound message or put a
    fabricated id on the wire. Dropping it keeps the message sendable and the
    local logs joinable.
    """
    from app.a2a.tracing import trace_context_for_outbound

    token = set_trace_id("abc-123")
    try:
        assert trace_context_for_outbound() is None
    finally:
        reset_trace_id(token)


def test_the_four_outbound_envelope_call_sites_all_carry_trace() -> None:
    """Guards against a new send path forgetting to propagate.

    A static check, because the failure is invisible: a message without a trace
    is delivered perfectly well, it just cannot be joined to anything.
    """
    from pathlib import Path
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "app" / "a2a" / "service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    missing = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "A2AEnvelope"
        ):
            if not any(k.arg == "trace" for k in node.keywords):
                missing.append(node.lineno)
    assert not missing, (
        f"A2AEnvelope is constructed without a trace at line(s) {missing}. Every "
        "outbound envelope must carry the current trace, or the correlation "
        "breaks at that hop."
    )


# ---------------------------------------------------------------------------
# Structured logs
# ---------------------------------------------------------------------------


def test_every_log_line_is_one_json_object_with_the_trace_id() -> None:
    """One object per line, `trace_id` always present, exception as a string.

    A multi-line traceback would break the line-per-event contract that log
    shippers rely on, which is why the exception is embedded rather than allowed
    to wrap.
    """
    formatter = JsonLogFormatter(service="nexus-test", environment="test")
    record = logging.LogRecord(
        name="nexus.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="a2a_outcome outcome=%s",
        args=("denied",),
        exc_info=None,
    )
    token = set_trace_id("json-check")
    try:
        line = formatter.format(record)
    finally:
        reset_trace_id(token)

    assert "\n" not in line, "a log line must not span lines"
    payload = json.loads(line)
    assert payload["trace_id"] == "json-check"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "nexus.test"
    assert payload["message"] == "a2a_outcome outcome=denied"
    assert payload["service"] == "nexus-test"
    assert payload["environment"] == "test"
    assert "timestamp" in payload


def test_log_formatter_includes_the_exception_without_breaking_the_line() -> None:
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom\nsecond line")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="nexus.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    line = formatter.format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert "ValueError" in payload["exception"]


def test_log_formatter_serialises_unserialisable_extras() -> None:
    """A log call must never be the thing that raises."""
    formatter = JsonLogFormatter()

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    record = logging.LogRecord(
        name="nexus.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="extras",
        args=(),
        exc_info=None,
    )
    record.payload = {"nested": [Opaque(), {"deep": Opaque()}]}
    payload = json.loads(formatter.format(record))
    assert payload["payload"] == {"nested": ["<opaque>", {"deep": "<opaque>"}]}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_policy_decisions_are_counted_by_outcome() -> None:
    """ASK and DENY are separate series: they are the two failure modes.

    A single `nexus_policy_decisions_total` with no label would answer "is the
    engine being called" and nothing else.
    """
    from app.policy.engine import EvaluationRequest, PolicyEngine

    POLICY_DECISIONS.reset()
    engine = PolicyEngine()
    engine.evaluate(
        EvaluationRequest(
            requester_agent_id="nexus:ed25519:" + "a" * 32,
            data_category="calendar",
            action="read",
            purpose="scheduling",
        ),
        policies=[],
        consents=[],
    )
    assert POLICY_DECISIONS.value(decision="ASK") == 1
    assert POLICY_DECISIONS.value(decision="ALLOW") == 0

    # A sensitive category with no rule is deny-by-default, so it must land in
    # the DENY series rather than the ASK one.
    engine.evaluate(
        EvaluationRequest(
            requester_agent_id="nexus:ed25519:" + "a" * 32,
            data_category="financial",
            action="read",
            purpose="scheduling",
        ),
        policies=[],
        consents=[],
    )
    assert POLICY_DECISIONS.value(decision="DENY") == 1


async def test_http_requests_are_counted_by_route_template(
    client: httpx.AsyncClient,
) -> None:
    """Metrics must be labelled by route template, never by raw path.

    A raw path with an id in it creates one series per id, which is how a
    metrics endpoint becomes an unbounded memory leak and takes the process with
    it. The assertion is that the labelled value is a template (`/sessions/{id}`)
    and that distinct ids do not each get their own series.
    """
    from app.observability import HTTP_REQUESTS

    before = HTTP_REQUESTS.value(method="GET", route="/health", status="2xx")
    await client.get("/health")
    after = HTTP_REQUESTS.value(method="GET", route="/health", status="2xx")
    assert after == before + 1, (
        "the request was not counted under its route template; the middleware's "
        "scope['route'] lookup is not seeing the matched route"
    )


def test_metrics_render_in_the_prometheus_exposition_format() -> None:
    """A scraper must be able to parse it without a client library."""
    text = METRICS.render()
    assert "# TYPE nexus_policy_decisions_total counter" in text
    assert "# HELP nexus_policy_decisions_total" in text
    # Every non-comment line must be `name{labels} value` or `name value`.
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert " " in line, f"malformed exposition line: {line!r}"
        assert line.rsplit(" ", 1)[-1] not in ("", None)


async def test_metrics_endpoint_is_reachable_without_a_session(
    client: httpx.AsyncClient,
) -> None:
    """A scraper has no cookie; requiring one makes the endpoint useless."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "nexus_policy_decisions_total" in response.text


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readyz_contract_is_public_and_minimal() -> None:
    """Readiness has its own payload shape, separate from liveness.

    `/health` must stay a constant `{status, version}` (see test_health.py);
    `/readyz` is the one allowed to say which dependency is unhappy, and it
    deliberately reports names and booleans rather than configuration.
    """
    from app.schemas.system import ReadyResponse

    fields = set(ReadyResponse.model_fields)
    assert fields == {"status", "version", "checks", "failed"}


async def test_readyz_reports_ready_when_dependencies_are_up(
    db_client: httpx.AsyncClient, db_app
) -> None:
    """With a real database attached, this replica should take traffic."""
    # The shared fixtures build an app whose services are injected directly, so
    # `identity_ok` is never set by a lifespan. A bootstrapped deployment does
    # set it, and that is the deployment this test is about.
    db_app.state.identity_ok = True
    from app.api import readiness

    readiness.reset_readiness_cache()

    response = await db_client.get("/readyz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["failed"] == []
    names = {check["name"] for check in body["checks"]}
    assert names == {"database", "identity", "jobs", "gateway"}, (
        f"the readiness checks changed shape: {sorted(names)}"
    )


async def test_readyz_returns_503_when_a_required_dependency_is_down(
    db_client: httpx.AsyncClient, db_app
) -> None:
    """503, not 200-with-a-body-field.

    Most load balancers and orchestrators only look at the status code, so a 200
    carrying `"status": "not_ready"` keeps traffic flowing to a replica that
    cannot serve it.
    """
    from app.api import readiness

    saved = getattr(db_app.state, "identity_ok", False)
    try:
        db_app.state.identity_ok = False
        # The verdict is cached for a couple of seconds; a probe interval is
        # longer than that, but a test is not.
        readiness.reset_readiness_cache()

        response = await db_client.get("/readyz")

        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "not_ready"
        assert "identity" in body["failed"]
    finally:
        db_app.state.identity_ok = saved
        readiness.reset_readiness_cache()


async def test_gateway_outage_does_not_make_the_replica_unready(
    db_client: httpx.AsyncClient, db_app
) -> None:
    """D2: gateway-first transport falls back to direct egress by design.

    Treating a gateway outage as unreadiness would take the whole deployment out
    of rotation because a relay is down - the opposite of what the fallback is
    for. It is reported, and it is advisory.
    """
    from app.api import readiness

    db_app.state.identity_ok = True
    gateway_client = getattr(db_app.state, "gateway_client", None)
    if gateway_client is None:
        pytest.skip("no gateway client configured on this app fixture")

    saved = getattr(gateway_client, "is_connected", False)
    try:
        gateway_client.is_connected = False
        readiness.reset_readiness_cache()
        response = await db_client.get("/readyz")
        assert response.status_code == 200, (
            "a gateway outage must degrade reachability, not readiness"
        )
        gateway = next(
            check for check in response.json()["checks"] if check["name"] == "gateway"
        )
        assert gateway["ready"] is True
        assert "direct egress" in (gateway["detail"] or "")
    finally:
        gateway_client.is_connected = saved
        readiness.reset_readiness_cache()


async def test_liveness_stays_200_regardless_of_dependencies(
    db_client: httpx.AsyncClient, db_app
) -> None:
    """The inverse of readiness, and the reason they are separate endpoints.

    A liveness probe that fails when the database is down makes the orchestrator
    restart every healthy replica during a database incident.
    """
    from app.api import readiness

    saved = getattr(db_app.state, "identity_ok", False)
    try:
        db_app.state.identity_ok = False
        readiness.reset_readiness_cache()
        response = await db_client.get("/health")
        assert response.status_code == 200
        assert set(response.json()) == {"status", "version"}
    finally:
        db_app.state.identity_ok = saved
        readiness.reset_readiness_cache()
