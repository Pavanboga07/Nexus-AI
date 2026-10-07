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
from typing import Any

from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.models import A2ATask, TaskStatus


def validate_negotiation_round(current_round: int, max_rounds: int) -> None:
    """Ensure negotiation does not exceed configured bounds."""
    if current_round >= max_rounds:
        raise A2AError(
            A2AErrorCode.NEGOTIATION_LIMIT_EXCEEDED,
            f"Maximum negotiation rounds ({max_rounds}) reached. Negotiation terminated.",
        )


#: Task states after which no inbound update may overwrite the task row.
#: A late or duplicated response (new message_id, passes replay protection)
#: must not resurrect a task that already reached a terminal state.
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED.value,
        TaskStatus.REJECTED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.EXPIRED.value,
    }
)


def map_response_status(
    payload: dict[str, Any] | None, *, default: TaskStatus
) -> TaskStatus:
    """Map a response payload's ``status`` to a TaskStatus, uniformly.

    The fast path (``delegate_task``/``negotiate_task`` awaiting a reply) and
    the late path (``handle_inbound_response``) previously disagreed — e.g. a
    late ``pending_approval`` marked the sender's task COMPLETED. Every path
    now shares this table; only the fallback for an unrecognized status
    differs per call site (``default``), because a delegation expects a final
    answer (COMPLETED) while a negotiation round expects progression
    (ACCEPTED).
    """
    status = (payload or {}).get("status")
    if status in {"rejected", "failed"}:
        return TaskStatus.REJECTED
    if status in {"pending_approval", "approval_required"}:
        return TaskStatus.PENDING_APPROVAL
    if status == "counter_proposal":
        return TaskStatus.ACCEPTED
    if status in {"completed", "accepted"}:
        return TaskStatus.COMPLETED
    return default


def is_terminal_status(status: str | None) -> bool:
    """True when ``status`` is terminal and must not be overwritten."""
    return status in TERMINAL_TASK_STATUSES


def validate_task_active(task: A2ATask, now: datetime | None = None) -> None:
    """Check that the task is still active and has not expired or terminated."""
    now = now or datetime.now(UTC)

    if task.expires_at is not None and task.expires_at <= now:
        raise A2AError(
            A2AErrorCode.TASK_EXPIRED,
            "Task has expired. Further negotiation or execution is rejected.",
        )

    if task.status in TERMINAL_TASK_STATUSES:
        raise A2AError(
            A2AErrorCode.CONFLICT,
            f"Task is in terminal state '{task.status}'. Cannot continue negotiation.",
        )


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "is_terminal_status",
    "map_response_status",
    "validate_negotiation_round",
    "validate_task_active",
]
