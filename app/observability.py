"""Observability: correlation IDs, structured logs, and metrics (M11).

The audit finding this closes is `M1`: there was no correlation between a
request, the log lines it produced, and the A2A envelope it caused. Debugging a
cross-agent failure meant guessing which of a hundred interleaved log lines
belonged together.

Four rules shape the design:

1. **One trace id, propagated, never regenerated.** It arrives on a header if a
   caller supplies one, is minted otherwise, is stored in a `ContextVar` so
   *every* log line and metric in the request's call stack picks it up without
   being threaded through as a parameter, and is echoed back on the response so
   the caller can quote it. It is also put on the outbound A2A envelope, which is
   what makes `User -> API -> Gateway -> Agent B` traceable by one value.

2. **Logs are JSON on one line per event.** The previous format was
   `"%(asctime)s %(levelname)s %(name)s %(message)s"`, which is fine for a
   terminal and useless for a log aggregator: nothing is queryable, and a
   multi-line exception breaks the line-per-event contract.

3. **Metrics are dependency-free.** `prometheus_client` is not a dependency of
   this project and adding a metrics stack to satisfy a counter would be the
   tail wagging the dog. This is a small, bounded, thread-safe registry that
   renders the Prometheus text exposition format - so a scraper works, and the
   counters are unit-testable without a server. The tradeoff is explicit: no
   histograms with configurable buckets, no multiprocess mode. If either becomes
   necessary, swap the registry, not the call sites.

4. **Absence of a trace id is a bug, not a state.** `current_trace_id()` returns
   a placeholder rather than `None`, so a log line emitted outside a request
   (a startup line, a background job) is still well-formed JSON.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

#: The header a caller may supply to join its trace to ours, and that we echo.
TRACE_HEADER = "X-Request-ID"

#: Used when there is no request in scope. Deliberately a visible value rather
#: than `None`: a log line with `"trace_id": null` looks like a bug in the
#: logging, which it is not.
NO_TRACE = "-"

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_trace_id", default=NO_TRACE
)


def new_trace_id() -> str:
    """A fresh, unguessable trace id.

    Not a counter and not `uuid1`: a trace id is attacker-influenced (we accept
    one on a header), so sequential ids would let a caller enumerate other
    users' traces in a log search.
    """
    return uuid.uuid4().hex


def current_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(value: str) -> contextvars.Token:
    """Bind a trace id for the current context. Use the token to restore."""
    return _trace_id.set(sanitise_trace_id(value))


def reset_trace_id(token: contextvars.Token) -> None:
    _trace_id.reset(token)


def sanitise_trace_id(value: str | None, *, max_length: int = 64) -> str:
    """Accept a caller's trace id only if it is safe to put in a log line.

    An unvalidated header value is log injection: a newline forges a log entry,
    a very long value floods the index, and arbitrary bytes corrupt the JSON.
    Anything outside ``[A-Za-z0-9._-]`` is rejected outright and replaced, rather
    than escaped - a caller that wants its id honoured can send a sane one.
    """
    if not value:
        return new_trace_id()
    candidate = value.strip()
    if not candidate or len(candidate) > max_length:
        return new_trace_id()
    if not all(c.isalnum() or c in "._-" for c in candidate):
        return new_trace_id()
    return candidate


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

#: Attributes present on every `LogRecord`. Anything else on a record was put
#: there by the caller via `extra=` and is worth emitting as a field.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class JsonLogFormatter(logging.Formatter):
    """One JSON object per log record, always carrying the trace id."""

    def __init__(self, *, service: str = "nexus", environment: str = "development"):
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # Read at format time, not bind time: the trace id is set by
            # middleware and must appear on every line logged during the request,
            # including lines logged by code that never saw the request object.
            "trace_id": current_trace_id(),
            "service": self.service,
            "environment": self.environment,
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # `default=str` is the backstop: a log line must never be the thing that
        # raises, so anything unserialisable degrades to its repr.
        return json.dumps(payload, default=str, ensure_ascii=False)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)


def configure_structured_logging(
    *, level: str = "INFO", service: str = "nexus", environment: str = "development"
) -> None:
    """Install the JSON formatter on the root logger.

    `force=True` so repeated calls (tests, `create_app` in a suite) do not stack
    handlers - stacked handlers emit every line N times, which reads as a
    duplicate-logging bug in the application rather than in the setup.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter(service=service, environment=environment))
    logging.basicConfig(level=level, handlers=[handler], force=True)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class Metric:
    """A counter or a summary, keyed by a bounded label set."""

    __slots__ = ("name", "kind", "help", "labels", "_lock", "_counts", "_values")

    def __init__(self, name: str, kind: str, help_text: str, labels: tuple[str, ...]):
        self.name = name
        self.kind = kind
        self.help = help_text
        self.labels = labels
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, ...], float] = {}
        if kind == "summary":
            #: label values -> [count, sum], for an average.
            self._values: dict[tuple[str, ...], list[float]] = {}
        else:
            self._values = {}

    def _key(self, values: Mapping[str, str]) -> tuple[str, ...]:
        missing = [label for label in self.labels if label not in values]
        if missing:
            raise ValueError(
                f"metric {self.name!r} requires labels {self.labels}; missing {missing}"
            )
        # Unknown labels are dropped rather than stored: an unbounded label
        # (a user id, a URL) is how a metrics endpoint becomes a memory leak.
        return tuple(str(values[label]) for label in self.labels)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if self.kind != "counter":
            raise TypeError(f"{self.name} is a {self.kind}, not a counter")
        key = self._key(labels)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0.0) + amount

    def observe(self, value: float, **labels: str) -> None:
        if self.kind != "summary":
            raise TypeError(f"{self.name} is a {self.kind}, not a summary")
        key = self._key(labels)
        with self._lock:
            entry = self._values.setdefault(key, [0.0, 0.0])
            entry[0] += 1.0
            entry[1] += float(value)

    def value(self, **labels: str) -> float:
        """Current count for a counter, or mean for a summary (tests use this)."""
        key = self._key(labels)
        with self._lock:
            if self.kind == "counter":
                return self._counts.get(key, 0.0)
            entry = self._values.get(key)
            if entry is None or entry[0] == 0:
                return 0.0
            return entry[1] / entry[0]

    def total(self) -> float:
        with self._lock:
            return sum(self._counts.values())

    def reset(self) -> None:
        """Clear all series. Only for tests - never called in production paths."""
        with self._lock:
            self._counts.clear()
            self._values.clear()

    def _mean(self, values: list[float]) -> float:
        return values[1] / values[0] if values[0] else 0.0

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        with self._lock:
            counts = dict(self._counts)
            values = {k: list(v) for k, v in self._values.items()}

        if self.kind == "counter":
            for key, count in sorted(counts.items()):
                lines.append(
                    f"{self.name}{_render_labels(self.labels, key)} {_num(count)}"
                )
            if not counts:
                lines.append(f"{self.name} 0")
            return lines

        for key in sorted(values):
            entry = values[key]
            lines.append(
                f"{self.name}_count{_render_labels(self.labels, key)} "
                f"{_num(entry[0])}"
            )
            lines.append(
                f"{self.name}_sum{_render_labels(self.labels, key)} {_num(entry[1])}"
            )
        if not values:
            lines.append(f"{self.name}_count 0")
            lines.append(f"{self.name}_sum 0")
        return lines


def _render_labels(labels: tuple[str, ...], key: tuple[str, ...]) -> str:
    if not labels:
        return ""
    pairs = ",".join(
        f'{label}="{_escape_label_value(value)}"' for label, value in zip(labels, key)
    )
    return "{" + pairs + "}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(value: float) -> str:
    """Render a number the way the exposition format expects.

    `inf`/`nan` are valid there and NOT valid JSON, which is why the fallback is
    spelled out rather than left to `str()`.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(value)


class MetricsRegistry:
    """Holds every metric, so exposition and test reset are one call each."""

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str, labels: Iterable[str] = ()) -> Metric:
        return self._register(name, "counter", help_text, labels)

    def summary(self, name: str, help_text: str, labels: Iterable[str] = ()) -> Metric:
        return self._register(name, "summary", help_text, labels)

    def _register(
        self, name: str, kind: str, help_text: str, labels: Iterable[str]
    ) -> Metric:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                # Returning the existing metric keeps a module imported twice
                # (or a test-created app) from splitting one series into two.
                if existing.kind != kind:
                    raise ValueError(
                        f"metric {name!r} already registered as a {existing.kind}"
                    )
                return existing
            metric = Metric(name, kind, help_text, tuple(labels))
            self._metrics[name] = metric
            return metric

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in metrics:
            metric.reset()


#: The process-wide registry.
METRICS = MetricsRegistry()

# --- The metrics that matter, named once so call sites cannot drift ----------

HTTP_REQUESTS = METRICS.counter(
    "nexus_http_requests_total",
    "HTTP requests by method, route template and status class.",
    ("method", "route", "status"),
)
HTTP_DURATION = METRICS.summary(
    "nexus_http_request_duration_seconds",
    "HTTP request duration by route template.",
    ("route",),
)
POLICY_DECISIONS = METRICS.counter(
    "nexus_policy_decisions_total",
    "Policy engine outcomes. DENY and ASK are the security-relevant series.",
    ("decision",),
)
A2A_OUTCOMES = METRICS.counter(
    "nexus_a2a_messages_total",
    "A2A envelopes by direction and outcome.",
    ("direction", "outcome"),
)
JOB_OUTCOMES = METRICS.counter(
    "nexus_jobs_total",
    "Background jobs by kind and outcome.",
    ("kind", "outcome"),
)
JOB_QUEUE_DEPTH = METRICS.summary(
    "nexus_job_queue_depth",
    "Pending jobs observed at claim time. Reported as a summary, not a gauge: "
    "the value is a sample at each worker tick, and a stale gauge is worse than "
    "a decaying average.",
    ("status",),
)
LLM_LATENCY = METRICS.summary(
    "nexus_llm_request_duration_seconds",
    "LLM call latency by provider and outcome.",
    ("provider", "outcome"),
)


__all__ = [
    "A2A_OUTCOMES",
    "HTTP_DURATION",
    "HTTP_REQUESTS",
    "JOB_OUTCOMES",
    "JOB_QUEUE_DEPTH",
    "JsonLogFormatter",
    "LLM_LATENCY",
    "METRICS",
    "Metric",
    "MetricsRegistry",
    "NO_TRACE",
    "POLICY_DECISIONS",
    "TRACE_HEADER",
    "configure_structured_logging",
    "current_trace_id",
    "new_trace_id",
    "reset_trace_id",
    "sanitise_trace_id",
    "set_trace_id",
]
