"""Load test: sustained request rate against a running Nexus API (M11).

The M11 acceptance criterion is "load test meets target with no unbounded
growth". That cannot be asserted from a unit test, so this is a tool that
produces the evidence - and it deliberately refuses to *invent* the evidence:

* It reports what it measured, and exits non-zero if a target was not met.
* With no ``--rate`` it measures the ceiling; with ``--rate`` it holds that rate.
* It samples the target's ``/metrics`` before and after, so "no unbounded growth"
  is checked against the app's own counters rather than a guess.
* The plan's headline target (1k agents, 100 msg/s of A2A traffic) requires a
  deployment sized for it and a populated database. This script will drive it,
  but it will not claim it: point ``--base-url`` at the target you mean.

Usage::

    # Ceiling on a local dev instance, 20 concurrent clients, 10 seconds
    python -m scripts.load_test --base-url http://127.0.0.1:8000 --concurrency 20 --duration 10

    # Hold 100 requests/second for 60 seconds and assert the latency budget
    python -m scripts.load_test --rate 100 --duration 60 --p95-budget-ms 250

    # Include authenticated endpoints
    python -m scripts.load_test --login you@example.com --password '...'

Exit codes: 0 met the targets, 1 a target was missed, 2 a usage/setup problem.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

#: The mix a person actually generates: mostly reads, some identity/status, and
#: the approval surfaces the Inbox fans out to. Weighted so the common path
#: dominates, because a benchmark that only hits `/health` measures the router.
DEFAULT_MIX: tuple[tuple[str, int], ...] = (
    ("/health", 15),
    ("/system/status", 20),
    ("/identity", 10),
    ("/a2a/tasks", 15),
    ("/workflows", 10),
    ("/autonomy/runs", 10),
    ("/memories", 10),
    ("/tools", 5),
    ("/policy/audit", 5),
)


@dataclass
class Result:
    """Accumulated outcomes. Kept as plain data so it can be printed or asserted."""

    statuses: dict[int, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.statuses.values()) + sum(self.errors.values())

    @property
    def failures(self) -> int:
        return sum(
            count for status, count in self.statuses.items() if status >= 500
        ) + sum(self.errors.values())

    @property
    def elapsed(self) -> float:
        return max(self.finished_at - self.started_at, 1e-9)

    @property
    def rate(self) -> float:
        return self.total / self.elapsed

    def percentile(self, fraction: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(int(len(ordered) * fraction), len(ordered) - 1)
        return ordered[index]

    def report(self) -> str:
        lines = [
            "",
            "=== load test ===",
            f"requests     : {self.total} in {self.elapsed:.1f}s "
            f"({self.rate:.0f} req/s)",
            f"failures     : {self.failures} (5xx or transport)",
            f"latency p50  : {self.percentile(0.50):.1f} ms",
            f"latency p95  : {self.percentile(0.95):.1f} ms",
            f"latency p99  : {self.percentile(0.99):.1f} ms",
        ]
        if self.latencies_ms:
            lines.append(f"latency mean : {statistics.fmean(self.latencies_ms):.1f} ms")
        if self.statuses:
            lines.append(
                "status codes : "
                + ", ".join(
                    f"{code}={count}" for code, count in sorted(self.statuses.items())
                )
            )
        if self.errors:
            lines.append(
                "transport    : "
                + ", ".join(f"{name}={count}" for name, count in sorted(self.errors.items()))
            )
        return "\n".join(lines)


def _parse_mix(raw: str | None) -> tuple[tuple[str, int], ...]:
    if not raw:
        return DEFAULT_MIX
    entries: list[tuple[str, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        path, _, weight = part.rpartition(":")
        if not path:
            raise ValueError(f"bad --mix entry {part!r}; expected '/path:weight'")
        entries.append((path, int(weight)))
    if not entries:
        raise ValueError("--mix contained no usable entries")
    return tuple(entries)


def _expand_mix(mix: tuple[tuple[str, int], ...]) -> list[str]:
    """Expand weights into a flat list, so selection is a plain `random.choice`."""
    return [path for path, weight in mix for _ in range(weight)]


async def _readiness(client):
    """GET /readyz, or None when the target is unreachable.

    Its own function because a transport failure here is the *expected* result
    of pointing the tool at the wrong host, and it must produce the documented
    exit code rather than a traceback.
    """
    try:
        return await client.get("/readyz", timeout=15.0)
    except Exception:  # noqa: BLE001
        return None


async def _fetch_metrics(client, base_url: str) -> dict[str, float]:
    """Counter totals from the target's own exposition endpoint.

    Used as the "unbounded growth" witness: the point is not the absolute number
    but that `nexus_http_requests_total` accounts for roughly the traffic we
    generated, so nothing is silently accumulating without being counted.
    """
    try:
        response = await client.get(f"{base_url}/metrics", timeout=10.0)
    except Exception:  # noqa: BLE001
        return {}
    totals: dict[str, float] = {}
    for line in response.text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name = line.split("{")[0].split(" ")[0]
        try:
            value = float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
        totals[name] = totals.get(name, 0.0) + value
    return totals


async def _worker(
    client,
    paths: list[str],
    result: Result,
    *,
    deadline: float,
    rate_per_worker: float | None,
    lock: asyncio.Lock,
) -> None:
    """One virtual client. Runs until the deadline, optionally paced."""
    import random

    interval = 1.0 / rate_per_worker if rate_per_worker else 0.0
    while time.perf_counter() < deadline:
        path = random.choice(paths)
        started = time.perf_counter()
        try:
            response = await client.get(path, timeout=30.0)
            status = response.status_code
        except Exception as exc:  # noqa: BLE001 - transport failures are data here
            async with lock:
                name = type(exc).__name__
                result.errors[name] = result.errors.get(name, 0) + 1
        else:
            async with lock:
                result.statuses[status] = result.statuses.get(status, 0) + 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        async with lock:
            result.latencies_ms.append(elapsed_ms)

        if interval:
            # Sleep the remainder of this worker's slot; if the request already
            # overran it, do not sleep at all (otherwise the rate silently
            # collapses and the test measures nothing).
            remaining = interval - (time.perf_counter() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)


async def run(args: argparse.Namespace) -> int:
    import httpx

    paths = _expand_mix(_parse_mix(args.mix))
    concurrency = args.concurrency
    # Split the target rate across workers so each is paced independently; a
    # single global pacer serialises the test and measures the pacer.
    per_worker = (args.rate / concurrency) if args.rate else None

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        if args.login:
            response = await client.post(
                "/auth/login",
                json={"email": args.login, "password": args.password},
            )
            if response.status_code >= 400:
                response = await client.post(
                    "/auth/register",
                    json={"email": args.login, "password": args.password},
                )
            if response.status_code >= 400:
                print(
                    f"could not authenticate: {response.status_code} {response.text[:200]}",
                    file=sys.stderr,
                )
                return 2
            print(f"authenticated as {args.login}")

        ready = await _readiness(client)
        if ready is None:
            print(
                f"cannot reach {args.base_url}: no /readyz response.\n"
                "Refusing to load-test a target that is not accepting traffic.",
                file=sys.stderr,
            )
            return 2
        if ready.status_code != 200:
            print(
                f"target is not ready ({ready.status_code}): {ready.text[:200]}\n"
                "Refusing to load-test a replica that is not accepting traffic.",
                file=sys.stderr,
            )
            return 2

        before = await _fetch_metrics(client, args.base_url)

        result = Result()
        lock = asyncio.Lock()
        result.started_at = time.perf_counter()
        deadline = result.started_at + args.duration

        print(
            f"driving {args.base_url} for {args.duration:g}s with "
            f"{concurrency} clients"
            + (f" at ~{args.rate:g} req/s" if args.rate else " (no rate cap: measuring ceiling)")
        )
        await asyncio.gather(
            *(
                _worker(
                    client,
                    paths,
                    result,
                    deadline=deadline,
                    rate_per_worker=per_worker,
                    lock=lock,
                )
                for _ in range(concurrency)
            )
        )
        result.finished_at = time.perf_counter()

        after = await _fetch_metrics(client, args.base_url)

    print(result.report())

    # --- Verdicts ---------------------------------------------------------
    exit_code = 0

    if result.failures:
        print(
            f"\nFAIL: {result.failures} request(s) failed. A load test that tolerates "
            "5xx measures nothing.",
            file=sys.stderr,
        )
        exit_code = 1

    if args.p95_budget_ms and result.percentile(0.95) > args.p95_budget_ms:
        print(
            f"\nFAIL: p95 {result.percentile(0.95):.0f} ms exceeds the "
            f"{args.p95_budget_ms:g} ms budget.",
            file=sys.stderr,
        )
        exit_code = 1

    if args.min_rate and result.rate < args.min_rate:
        print(
            f"\nFAIL: achieved {result.rate:.0f} req/s, below the "
            f"{args.min_rate:g} req/s target.",
            file=sys.stderr,
        )
        exit_code = 1

    # Unbounded-growth witness: the app's own counter must see the traffic we
    # generated. A large shortfall would mean requests are being served without
    # being accounted for - the signature of something accumulating off the
    # metrics path.
    if before and after:
        delta = after.get("nexus_http_requests_total", 0.0) - before.get(
            "nexus_http_requests_total", 0.0
        )
        ratio = (delta / result.total) if result.total else 0.0
        print(
            f"\napp counter  : nexus_http_requests_total +{delta:.0f} "
            f"for {result.total} requests (ratio {ratio:.2f})"
        )
        if ratio < args.min_counted_ratio:
            print(
                f"FAIL: the app counted only {ratio:.2f} of the requests made. "
                "Requests are being served without being accounted for.",
                file=sys.stderr,
            )
            exit_code = 1
    else:
        print(
            "\nnote: /metrics was unavailable, so the app-side accounting check "
            "was skipped.",
            file=sys.stderr,
        )

    if exit_code == 0:
        print("\nOK: all targets met.")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--concurrency", type=int, default=10, help="Concurrent virtual clients."
    )
    parser.add_argument(
        "--duration", type=float, default=10.0, help="Seconds to drive traffic."
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help="Target total requests/second. Omit to measure the ceiling.",
    )
    parser.add_argument(
        "--mix",
        default=None,
        help="Override the endpoint mix, e.g. '/health:1,/a2a/tasks:9'.",
    )
    parser.add_argument("--p95-budget-ms", type=float, default=None)
    parser.add_argument("--min-rate", type=float, default=None)
    parser.add_argument(
        "--min-counted-ratio",
        type=float,
        default=0.9,
        help="Fraction of requests the app's own counter must account for.",
    )
    parser.add_argument("--login", default=None, help="Email to authenticate as.")
    parser.add_argument("--password", default="Load-Test-Password-1")
    args = parser.parse_args(argv)

    if args.concurrency < 1:
        print("--concurrency must be at least 1", file=sys.stderr)
        return 2
    if args.duration <= 0:
        print("--duration must be positive", file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
