"""Bounded negotiation controls (Part 8).

Negotiation allows personal agents to exchange counter-proposals (e.g.
"What about 7 PM instead?") within strict, non-autonomous boundaries:

- Maximum negotiation rounds (default 3, configurable).
- Maximum task lifetime (expiration check on every round).
- Negotiation NEVER overrides policy: every proposal or requested data
  category is independently authorized by PolicyService.
- No autonomous infinite loops: each round is a discrete message exchange.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.models import A2ATask, TaskStatus


def validate_negotiation_round(current_round: int, max_rounds: int) -> None:
    """Ensure negotiation does not exceed configured bounds."""
    if current_round >= max_rounds:
        raise A2AError(
            A2AErrorCode.NEGOTIATION_LIMIT_EXCEEDED,
            f"Maximum negotiation rounds ({max_rounds}) reached. Negotiation terminated.",
        )


def validate_task_active(task: A2ATask, now: datetime | None = None) -> None:
    """Check that the task is still active and has not expired or terminated."""
    now = now or datetime.now(UTC)

    if task.expires_at is not None and task.expires_at <= now:
        raise A2AError(
            A2AErrorCode.TASK_EXPIRED,
            "Task has expired. Further negotiation or execution is rejected.",
        )

    terminal_statuses = {
        TaskStatus.COMPLETED.value,
        TaskStatus.REJECTED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.EXPIRED.value,
    }
    if task.status in terminal_statuses:
        raise A2AError(
            A2AErrorCode.CONFLICT,
            f"Task is in terminal state '{task.status}'. Cannot continue negotiation.",
        )


__all__ = [
    "validate_negotiation_round",
    "validate_task_active",
]
