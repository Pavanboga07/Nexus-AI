"""Replay protection and time-window validation for inbound A2A messages.

The DB unique constraint on (owner_id, message_id) provides race-safe
duplicate detection; this module adds the time-window checks:

- expired (expires_at <= now)          -> reject
- timestamp too far in the future      -> reject (clock skew)

Production deployments should synchronise clocks (NTP); the skew window
exists to tolerate small drift between honest agents.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.schemas import A2AEnvelope, parse_iso


def validate_time_window(
    envelope: A2AEnvelope,
    *,
    max_clock_skew_seconds: float,
    now: datetime | None = None,
) -> None:
    """Raise A2AError when the message is expired or from the future."""
    now = now or datetime.now(timezone.utc)
    timestamp = parse_iso(envelope.timestamp)
    expires_at = parse_iso(envelope.expires_at)

    if expires_at <= now:
        raise A2AError(A2AErrorCode.EXPIRED, "Message has expired.")

    if timestamp > now:
        skew = (timestamp - now).total_seconds()
        if skew > max_clock_skew_seconds:
            raise A2AError(
                A2AErrorCode.CLOCK_SKEW,
                "Message timestamp is too far in the future; are the "
                "clocks synchronised?",
            )

    if expires_at <= timestamp:
        raise A2AError(
            A2AErrorCode.INVALID_ENVELOPE,
            "expires_at must be after timestamp.",
        )


__all__ = ["validate_time_window"]
