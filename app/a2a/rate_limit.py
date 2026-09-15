"""Per-agent inbound rate limiting (Part 6 spec §31).

In-memory sliding window, isolated behind an interface so Redis can replace
it later without touching the A2A service. Single-process only by design;
horizontal deployments will need the Redis implementation.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Protocol


class RateLimiter(Protocol):
    def check(self, key: str) -> bool:
        """True when the request is within the limit."""
        ...


class SlidingWindowRateLimiter:
    """N requests per window per key, sliding window, in memory."""

    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


__all__ = ["RateLimiter", "SlidingWindowRateLimiter"]
