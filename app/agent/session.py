"""In-process session storage.

TEMPORARY BY DESIGN. Part 1 keeps conversations in a plain dict guarded by an
``asyncio.Lock``. This is deliberately not durable: restarting the process
loses every session, and the store is per-worker (it will not work behind
multiple Uvicorn workers).

Part 2 (Persistent Memory) replaces this module with a PostgreSQL + pgvector
backed implementation. To keep that swap cheap, callers depend on the
:class:`SessionStore` interface rather than on the concrete class.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.llm.base import Message

logger = logging.getLogger("nexus.agent.session")

VALID_ROLES = frozenset({"system", "user", "assistant"})


class SessionNotFoundError(KeyError):
    """Raised when a session id does not exist in the store."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id

    def __str__(self) -> str:
        return f"Session '{self.session_id}' not found."


@dataclass
class Session:
    """A single conversation and its message history.

    ``owner_id`` is part of the session, not an ambient property of the
    process. Before multi-tenancy the owner came from a process-wide cache, so
    every request served the same principal and one user could read another's
    conversation by guessing its id.
    """

    session_id: str
    owner_id: uuid.UUID | None = None
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def append(self, role: str, content: str) -> Message:
        """Append a message and return it."""
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role!r}")
        message: Message = {"role": role, "content": content}
        self.messages.append(message)
        self.updated_at = datetime.now(UTC)
        return message

    def trim(self, max_messages: int) -> None:
        """Drop the oldest messages, keeping the most recent ``max_messages``.

        The system prompt is not stored here (it is injected at context-build
        time), so trimming cannot orphan it.
        """
        if max_messages > 0 and len(self.messages) > max_messages:
            del self.messages[: len(self.messages) - max_messages]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "messages": [dict(m) for m in self.messages],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class SessionStore(ABC):
    """Interface every session backend must satisfy.

    Every method takes the acting ``owner_id`` explicitly. This is the
    tenancy boundary: a store must never return another owner's data, and the
    only way to guarantee that is to make ownership a required argument rather
    than something the store resolves for itself.
    """

    @abstractmethod
    async def create_session(self, owner_id: uuid.UUID) -> Session: ...

    @abstractmethod
    async def get_session(self, owner_id: uuid.UUID, session_id: str) -> Session: ...

    @abstractmethod
    async def add_message(
        self, owner_id: uuid.UUID, session_id: str, role: str, content: str
    ) -> Message: ...

    @abstractmethod
    async def clear_session(self, owner_id: uuid.UUID, session_id: str) -> Session: ...

    @abstractmethod
    async def delete_session(self, owner_id: uuid.UUID, session_id: str) -> None: ...

    @abstractmethod
    async def list_sessions(self, owner_id: uuid.UUID) -> list[str]: ...


class InMemorySessionStore(SessionStore):
    """Dict-backed session store for Part 1.

    Safe for concurrent use within a single event loop: every mutation is
    guarded by an ``asyncio.Lock``.

    Sessions are keyed by ``(owner_id, session_id)`` so a session id is not
    globally guessable across owners.
    """

    def __init__(self, *, max_messages: int = 100) -> None:
        self._sessions: dict[tuple[uuid.UUID, str], Session] = {}
        self._lock = asyncio.Lock()
        self._max_messages = max_messages

    async def create_session(self, owner_id: uuid.UUID) -> Session:
        session = Session(session_id=str(uuid.uuid4()), owner_id=owner_id)
        async with self._lock:
            self._sessions[(owner_id, session.session_id)] = session
        logger.info("session_created session_id=%s", session.session_id)
        return session

    async def get_session(self, owner_id: uuid.UUID, session_id: str) -> Session:
        async with self._lock:
            session = self._sessions.get((owner_id, session_id))
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    async def add_message(
        self, owner_id: uuid.UUID, session_id: str, role: str, content: str
    ) -> Message:
        async with self._lock:
            session = self._sessions.get((owner_id, session_id))
            if session is None:
                raise SessionNotFoundError(session_id)
            message = session.append(role, content)
            session.trim(self._max_messages)
        return message

    async def clear_session(self, owner_id: uuid.UUID, session_id: str) -> Session:
        """Empty a session's history but keep the session id valid."""
        async with self._lock:
            session = self._sessions.get((owner_id, session_id))
            if session is None:
                raise SessionNotFoundError(session_id)
            session.messages.clear()
            session.updated_at = datetime.now(UTC)
        logger.info("session_cleared session_id=%s", session_id)
        return session

    async def delete_session(self, owner_id: uuid.UUID, session_id: str) -> None:
        async with self._lock:
            if (owner_id, session_id) not in self._sessions:
                raise SessionNotFoundError(session_id)
            del self._sessions[(owner_id, session_id)]
        logger.info("session_deleted session_id=%s", session_id)

    async def list_sessions(self, owner_id: uuid.UUID) -> list[str]:
        async with self._lock:
            return [sid for (owner, sid) in self._sessions if owner == owner_id]


__all__ = [
    "InMemorySessionStore",
    "Session",
    "SessionNotFoundError",
    "SessionStore",
    "VALID_ROLES",
]
