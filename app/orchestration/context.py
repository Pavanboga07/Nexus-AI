"""Conversational context and follow-up reference resolution.

Maintains state across turns so pronouns ("him", "her", "them") and references
("it", "that time", "book it") map to active orchestration targets and proposed slots.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("nexus.orchestration.context")


@dataclass
class SessionOrchestrationContext:
    """Conversational orchestration context for a session."""

    session_id: str
    active_target: str | None = None
    active_agent_id: str | None = None
    last_proposed_time: str | None = None
    last_task_id: str | None = None
    last_run_id: str | None = None
    last_intent_type: str | None = None
    pending_approval: dict[str, Any] | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OrchestrationContextManager:
    """Manages multi-turn orchestration context across sessions."""

    def __init__(self) -> None:
        self._contexts: dict[str, SessionOrchestrationContext] = {}

    def get_context(self, session_id: str) -> SessionOrchestrationContext:
        if session_id not in self._contexts:
            self._contexts[session_id] = SessionOrchestrationContext(session_id=session_id)
        return self._contexts[session_id]

    def update_target(
        self,
        session_id: str,
        target_name: str,
        agent_id: str | None = None,
    ) -> None:
        ctx = self.get_context(session_id)
        ctx.active_target = target_name
        if agent_id:
            ctx.active_agent_id = agent_id
        ctx.updated_at = datetime.now(timezone.utc)

    def update_proposed_time(
        self, session_id: str, proposed_time: str, task_id: str | None = None
    ) -> None:
        ctx = self.get_context(session_id)
        ctx.last_proposed_time = proposed_time
        if task_id:
            ctx.last_task_id = task_id
        ctx.updated_at = datetime.now(timezone.utc)

    def set_pending_approval(
        self, session_id: str, approval_data: dict[str, Any] | None
    ) -> None:
        ctx = self.get_context(session_id)
        ctx.pending_approval = approval_data
        ctx.updated_at = datetime.now(timezone.utc)

    def resolve_references(
        self,
        session_id: str,
        message: str,
    ) -> tuple[str, str | None]:
        """Resolve pronouns ('him', 'her', 'them') in message using active session target.

        Returns (resolved_message, resolved_target_name).
        """
        ctx = self.get_context(session_id)
        target = ctx.active_target

        if not target:
            return message, None

        low = message.lower()
        resolved_msg = message

        # Replace pronouns when referring to an action: "ask him", "tell her", "inform them"
        pronoun_patterns = [
            (r"\bask\s+(?:him|her|them)\b", f"ask {target}"),
            (r"\btell\s+(?:him|her|them)\b", f"tell {target}"),
            (r"\bcheck\s+with\s+(?:him|her|them)\b", f"check with {target}"),
            (r"\bsee\s+if\s+(?:he|she|they)\s+can\b", f"see if {target} can"),
            (r"\bis\s+(?:he|she|they)\s+free\b", f"is {target} free"),
        ]

        matched = False
        for pattern, replacement in pronoun_patterns:
            if re.search(pattern, low):
                resolved_msg = re.sub(pattern, replacement, resolved_msg, flags=re.IGNORECASE)
                matched = True

        if matched or any(w in low for w in ["him", "her", "them", "it", "that works"]):
            return resolved_msg, target

        return message, None
