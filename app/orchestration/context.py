"""Conversational context and follow-up reference resolution.

Maintains state across turns so pronouns ("him", "her", "them") and references
("it", "that time", "book it") map to active orchestration targets and proposed slots.
Persists state durably to the database across server restarts.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.orchestration.repository import OrchestrationContextRepository

logger = logging.getLogger("nexus.orchestration.context")


class AwaitableTuple(tuple):
    """A tuple that can also be directly awaited if called in an async context."""

    def __await__(self):
        async def _c():
            return self
        return _c().__await__()


class AwaitableNone:
    """An object evaluating to None in sync contexts that can be awaited in async contexts."""

    def __init__(self, coro=None) -> None:
        self._coro = coro

    def __await__(self):
        if self._coro is not None:
            return self._coro.__await__()

        async def _c():
            return None

        return _c().__await__()


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
    """Manages multi-turn orchestration context across sessions with DB persistence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._contexts: dict[str, SessionOrchestrationContext] = {}
        self._repo = OrchestrationContextRepository()

    async def get_context(
        self, *args: Any, **kwargs: Any
    ) -> SessionOrchestrationContext:
        """Fetch context. Supports (session_id) or (owner_id, session_id)."""
        session_id: str
        owner_id: uuid.UUID | None = None
        if len(args) == 2:
            owner_id, session_id = args
        elif len(args) == 1:
            session_id = args[0]
        else:
            session_id = kwargs.get("session_id", "")
            owner_id = kwargs.get("owner_id")

        if session_id in self._contexts:
            return self._contexts[session_id]

        if self._session_factory is not None:
            try:
                async with self._session_factory() as session:
                    db_ctx = await self._repo.get_by_session_id(session, session_id)
                    if db_ctx is not None:
                        cached = SessionOrchestrationContext(
                            session_id=session_id,
                            active_target=db_ctx.active_target,
                            active_agent_id=db_ctx.active_agent_id,
                            last_proposed_time=db_ctx.last_proposed_time,
                            last_task_id=db_ctx.last_task_id,
                            last_run_id=db_ctx.last_run_id,
                            last_intent_type=db_ctx.last_intent_type,
                            pending_approval=db_ctx.pending_approval,
                            updated_at=db_ctx.updated_at,
                        )
                        self._contexts[session_id] = cached
                        return cached
            except Exception as exc:
                logger.warning("Failed loading orchestration context from DB: %s", exc)

        ctx = SessionOrchestrationContext(session_id=session_id)
        self._contexts[session_id] = ctx
        return ctx

    def get_context_sync(self, session_id: str) -> SessionOrchestrationContext:
        if session_id not in self._contexts:
            self._contexts[session_id] = SessionOrchestrationContext(session_id=session_id)
        return self._contexts[session_id]

    def update_target(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AwaitableNone:
        """Update active target. Supports sync or async caller, and (owner_id, session_id, target_name) or (session_id, target_name)."""
        owner_id: uuid.UUID | None = None
        session_id: str = ""
        target_name: str = ""
        agent_id: str | None = None

        if len(args) >= 3 and isinstance(args[0], uuid.UUID):
            owner_id = args[0]
            session_id = args[1]
            target_name = args[2]
            if len(args) > 3:
                agent_id = args[3]
            else:
                agent_id = kwargs.get("agent_id")
        elif len(args) >= 2:
            session_id = args[0]
            target_name = args[1]
            if len(args) > 2:
                agent_id = args[2]
            else:
                agent_id = kwargs.get("agent_id")
            owner_id = kwargs.get("owner_id")
        else:
            session_id = kwargs.get("session_id", "")
            target_name = kwargs.get("target_name", "")
            agent_id = kwargs.get("agent_id")
            owner_id = kwargs.get("owner_id")

        ctx = self.get_context_sync(session_id)
        ctx.active_target = target_name
        if agent_id:
            ctx.active_agent_id = agent_id
        ctx.updated_at = datetime.now(timezone.utc)

        if self._session_factory is None or owner_id is None:
            return AwaitableNone(None)

        async def _persist():
            try:
                async with self._session_factory() as session:
                    await self._repo.upsert(
                        session,
                        session_id=session_id,
                        owner_id=owner_id,
                        active_target=target_name,
                        active_agent_id=agent_id,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("Failed persisting orchestration context target: %s", exc)

        return AwaitableNone(_persist())

    def update_proposed_time(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AwaitableNone:
        owner_id: uuid.UUID | None = None
        session_id: str = ""
        proposed_time: str = ""
        task_id: str | None = None

        if len(args) >= 3 and isinstance(args[0], uuid.UUID):
            owner_id = args[0]
            session_id = args[1]
            proposed_time = args[2]
            if len(args) > 3:
                task_id = args[3]
            else:
                task_id = kwargs.get("task_id")
        elif len(args) >= 2:
            session_id = args[0]
            proposed_time = args[1]
            if len(args) > 2:
                task_id = args[2]
            else:
                task_id = kwargs.get("task_id")
            owner_id = kwargs.get("owner_id")
        else:
            session_id = kwargs.get("session_id", "")
            proposed_time = kwargs.get("proposed_time", "")
            task_id = kwargs.get("task_id")
            owner_id = kwargs.get("owner_id")

        ctx = self.get_context_sync(session_id)
        ctx.last_proposed_time = proposed_time
        if task_id:
            ctx.last_task_id = task_id
        ctx.updated_at = datetime.now(timezone.utc)

        if self._session_factory is None or owner_id is None:
            return AwaitableNone(None)

        async def _persist():
            try:
                async with self._session_factory() as session:
                    await self._repo.upsert(
                        session,
                        session_id=session_id,
                        owner_id=owner_id,
                        last_proposed_time=proposed_time,
                        last_task_id=task_id,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("Failed persisting orchestration context proposed time: %s", exc)

        return AwaitableNone(_persist())

    def set_pending_approval(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AwaitableNone:
        owner_id: uuid.UUID | None = None
        session_id: str = ""
        approval_data: dict[str, Any] | None = None

        if len(args) >= 3 and isinstance(args[0], uuid.UUID):
            owner_id = args[0]
            session_id = args[1]
            approval_data = args[2]
        elif len(args) >= 2:
            session_id = args[0]
            approval_data = args[1]
            owner_id = kwargs.get("owner_id")
        else:
            session_id = kwargs.get("session_id", "")
            approval_data = kwargs.get("approval_data")
            owner_id = kwargs.get("owner_id")

        ctx = self.get_context_sync(session_id)
        ctx.pending_approval = approval_data
        ctx.updated_at = datetime.now(timezone.utc)

        if self._session_factory is None or owner_id is None:
            return AwaitableNone(None)

        async def _persist():
            try:
                async with self._session_factory() as session:
                    await self._repo.upsert(
                        session,
                        session_id=session_id,
                        owner_id=owner_id,
                        pending_approval=approval_data,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("Failed persisting orchestration context pending approval: %s", exc)

        return AwaitableNone(_persist())

    def resolve_references(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AwaitableTuple:
        """Resolve pronouns ('him', 'her', 'them') in message using active session target.

        Supports both (session_id, message) and (owner_id, session_id, message).
        Returns an AwaitableTuple of (resolved_message, resolved_target_name).
        """
        session_id: str = ""
        message: str = ""

        if len(args) == 3:
            session_id = args[1]
            message = args[2]
        elif len(args) == 2:
            session_id = args[0]
            message = args[1]
        else:
            session_id = kwargs.get("session_id", "")
            message = kwargs.get("message", "")

        ctx = self.get_context_sync(session_id)
        target = ctx.active_target

        if not target:
            return AwaitableTuple((message, None))

        low = message.lower()
        resolved_msg = message

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
            return AwaitableTuple((resolved_msg, target))

        return AwaitableTuple((message, None))
