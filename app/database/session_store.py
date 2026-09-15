"""PostgreSQL-backed session store (Part 2).

Implements the Part 1 ``SessionStore`` interface against the conversations
and messages tables, so conversations survive restarts. ``Session`` objects
are thin snapshots - the database is the source of truth.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.session import Session, SessionNotFoundError, SessionStore
from app.database.models import Message as MessageModel
from app.database.repositories import (
    ConversationRepository,
    OwnerRepository,
)

logger = logging.getLogger("nexus.agent.session.db")


class DatabaseSessionStore(SessionStore):
    """Persistent ``SessionStore`` over PostgreSQL."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        max_messages: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._conversations = ConversationRepository()
        self._owners = OwnerRepository()
        self._max_messages = max_messages
        self._owner_id: uuid.UUID | None = None

    async def _get_owner_id(self) -> uuid.UUID:
        """Resolve the (single) owner lazily; cached for the process."""
        if self._owner_id is None:
            async with self._session_factory() as session:
                owner = await self._owners.get_or_create_default(session)
                await session.commit()
                self._owner_id = owner.id
        return self._owner_id

    async def _to_session(self, conversation, messages: list[MessageModel]) -> Session:
        return Session(
            session_id=str(conversation.id),
            messages=[{"role": m.role, "content": m.content} for m in messages],
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    async def create_session(self) -> Session:
        owner_id = await self._get_owner_id()
        async with self._session_factory() as session:
            conversation = await self._conversations.create(session, owner_id)
            await session.commit()
            logger.info("session_created session_id=%s", conversation.id)
            session_obj = await self._to_session(conversation, [])
        return session_obj

    async def get_session(self, session_id: str) -> Session:
        owner_id = await self._get_owner_id()
        try:
            conversation_uuid = uuid.UUID(session_id)
        except ValueError:
            raise SessionNotFoundError(session_id) from None
        async with self._session_factory() as session:
            conversation = await self._conversations.get(
                session, conversation_uuid, owner_id
            )
            if conversation is None:
                raise SessionNotFoundError(session_id)
            return await self._to_session(conversation, list(conversation.messages))

    async def add_message(self, session_id: str, role: str, content: str):
        owner_id = await self._get_owner_id()
        try:
            conversation_uuid = uuid.UUID(session_id)
        except ValueError:
            raise SessionNotFoundError(session_id) from None
        async with self._session_factory() as session:
            try:
                message = await self._conversations.add_message(
                    session, conversation_uuid, owner_id, role, content
                )
                await session.commit()
            except KeyError:
                raise SessionNotFoundError(session_id) from None
        return {"role": role, "content": content}

    async def clear_session(self, session_id: str) -> Session:
        owner_id = await self._get_owner_id()
        try:
            conversation_uuid = uuid.UUID(session_id)
        except ValueError:
            raise SessionNotFoundError(session_id) from None
        async with self._session_factory() as session:
            conversation = await self._conversations.clear_messages(
                session, conversation_uuid, owner_id
            )
            if conversation is None:
                raise SessionNotFoundError(session_id)
            await session.commit()
            logger.info("session_cleared session_id=%s", session_id)
            return await self._to_session(conversation, [])

    async def delete_session(self, session_id: str) -> None:
        owner_id = await self._get_owner_id()
        try:
            conversation_uuid = uuid.UUID(session_id)
        except ValueError:
            raise SessionNotFoundError(session_id) from None
        async with self._session_factory() as session:
            deleted = await self._conversations.delete(
                session, conversation_uuid, owner_id
            )
            await session.commit()
        if not deleted:
            raise SessionNotFoundError(session_id)
        logger.info("session_deleted session_id=%s", session_id)

    async def list_sessions(self) -> list[str]:
        owner_id = await self._get_owner_id()
        from sqlalchemy import select

        from app.database.models import Conversation

        async with self._session_factory() as session:
            result = await session.execute(
                select(Conversation.id).where(
                    Conversation.owner_id == owner_id
                )
            )
            return [str(row[0]) for row in result.all()]


__all__ = ["DatabaseSessionStore"]
