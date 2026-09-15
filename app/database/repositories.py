"""Repositories: the only place raw SQLAlchemy queries live.

MemoryManager and DatabaseSessionStore talk to these classes; nothing else in
the app builds queries. Owner scoping is enforced here, not by callers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Conversation, Memory, MemoryType, Message, Owner


class OwnerRepository:
    async def get_or_create_default(self, session: AsyncSession) -> Owner:
        """Part 2 has a single implicit owner. The row is created lazily on
        first use; a dedicated ``get_or_create_by_id`` can be added when
        authentication arrives."""
        result = await session.execute(
            select(Owner).order_by(Owner.created_at).limit(1)
        )
        owner = result.scalar_one_or_none()
        if owner is None:
            owner = Owner(name="owner")
            session.add(owner)
            await session.flush()
        return owner


class ConversationRepository:
    async def create(self, session: AsyncSession, owner_id: uuid.UUID) -> Conversation:
        conversation = Conversation(owner_id=owner_id)
        session.add(conversation)
        await session.flush()
        return conversation

    async def get(
        self, session: AsyncSession, conversation_id: uuid.UUID, owner_id: uuid.UUID
    ) -> Conversation | None:
        result = await session.execute(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.owner_id == owner_id,
            )
            .options()  # messages loaded explicitly below to control ordering
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            return None
        # Touch the relationship inside the session so to_dict() sees messages.
        await session.refresh(conversation, ["messages"])
        return conversation

    async def add_message(
        self,
        session: AsyncSession,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        role: str,
        content: str,
    ) -> Message:
        # Ownership check: a conversation that exists but belongs to another
        # owner must behave exactly like a missing one.
        conversation = await self.get(session, conversation_id, owner_id)
        if conversation is None:
            raise KeyError(str(conversation_id))
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
        )
        session.add(message)
        await session.flush()
        return message

    async def list_messages(
        self, session: AsyncSession, conversation_id: uuid.UUID, owner_id: uuid.UUID
    ) -> list[Message]:
        conversation = await self.get(session, conversation_id, owner_id)
        if conversation is None:
            raise KeyError(str(conversation_id))
        return list(conversation.messages)

    async def clear_messages(
        self, session: AsyncSession, conversation_id: uuid.UUID, owner_id: uuid.UUID
    ) -> Conversation | None:
        """DELETE /sessions/{id}: empty the history, keep the id valid."""
        conversation = await self.get(session, conversation_id, owner_id)
        if conversation is None:
            return None
        await session.execute(
            delete(Message).where(Message.conversation_id == conversation_id)
        )
        await session.flush()
        # The bulk DELETE bypasses the ORM relationship cache; reload it so
        # to_dict() on the returned object reflects the empty history.
        await session.refresh(conversation, ["messages"])
        return conversation

    async def delete(
        self, session: AsyncSession, conversation_id: uuid.UUID, owner_id: uuid.UUID
    ) -> bool:
        result = await session.execute(
            delete(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.owner_id == owner_id,
            )
        )
        return bool(result.rowcount)


class MemoryRepository:
    """Owner-scoped access to the memories table.

    Every method takes ``owner_id`` and every query filters on it. There is
    deliberately no unscoped method - Part 4's policy layer will wrap this
    repository, and remote agents must go through the runtime, never the DB.
    """

    def __init__(self, dedup_threshold: float = 0.92) -> None:
        self._dedup_threshold = dedup_threshold

    async def add(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        memory_type: MemoryType,
        content: str,
        embedding: list[float] | None,
        importance: float,
        confidence: float,
        source_message_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> Memory:
        memory = Memory(
            owner_id=owner_id,
            memory_type=memory_type,
            content=content,
            embedding=embedding,
            importance=importance,
            confidence=confidence,
            source_message_id=source_message_id,
            metadata_=metadata or {},
        )
        session.add(memory)
        await session.flush()
        return memory

    async def get(
        self, session: AsyncSession, memory_id: uuid.UUID, owner_id: uuid.UUID
    ) -> Memory | None:
        result = await session.execute(
            select(Memory).where(
                Memory.id == memory_id, Memory.owner_id == owner_id
            )
        )
        return result.scalar_one_or_none()

    async def list_memories(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        *,
        memory_type: MemoryType | None = None,
        limit: int = 100,
    ) -> list[Memory]:
        stmt = select(Memory).where(Memory.owner_id == owner_id)
        if memory_type is not None:
            stmt = stmt.where(Memory.memory_type == memory_type)
        stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars())

    async def update(
        self, session: AsyncSession, memory: Memory, **fields: object
    ) -> Memory:
        for key, value in fields.items():
            setattr(memory, key, value)
        await session.flush()
        return memory

    async def delete(
        self, session: AsyncSession, memory_id: uuid.UUID, owner_id: uuid.UUID
    ) -> bool:
        result = await session.execute(
            delete(Memory).where(
                Memory.id == memory_id, Memory.owner_id == owner_id
            )
        )
        return bool(result.rowcount)

    async def search(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        query_embedding: list[float],
        *,
        limit: int = 5,
        memory_types: list[MemoryType] | None = None,
        similarity_threshold: float = 0.0,
    ) -> list[tuple[Memory, float]]:
        """Cosine-similarity KNN search via pgvector's ``<=>`` operator.

        Returns ``(memory, similarity)`` pairs, best first, scoped to the
        owner. ``similarity_threshold`` (cosine similarity, 0..1) filters out
        weak matches.
        """
        distance = Memory.embedding.cosine_distance(query_embedding)
        similarity = 1.0 - distance
        stmt = (
            select(Memory, similarity.label("similarity"))
            .where(Memory.owner_id == owner_id, Memory.embedding.is_not(None))
            .order_by(distance.asc())
            .limit(limit)
        )
        if memory_types:
            stmt = stmt.where(Memory.memory_type.in_(memory_types))
        if similarity_threshold > 0:
            stmt = stmt.where(similarity >= similarity_threshold)

        result = await session.execute(stmt)
        rows = [(row[0], float(row[1])) for row in result.all()]

        # Lifecycle: record retrieval for future memory management.
        now = datetime.now(timezone.utc)
        for memory, _sim in rows:
            memory.last_accessed_at = now
        await session.flush()
        return rows

    async def find_similar(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        embedding: list[float],
        *,
        similarity_threshold: float | None = None,
        limit: int = 1,
    ) -> list[tuple[Memory, float]]:
        """Deduplication helper: nearest existing memories to ``embedding``."""
        threshold = (
            self._dedup_threshold if similarity_threshold is None else similarity_threshold
        )
        return await self.search(
            session,
            owner_id,
            embedding,
            limit=limit,
            similarity_threshold=threshold,
        )

    async def count(self, session: AsyncSession, owner_id: uuid.UUID) -> int:
        result = await session.execute(
            select(func.count()).select_from(Memory).where(
                Memory.owner_id == owner_id
            )
        )
        return int(result.scalar_one())


__all__ = [
    "ConversationRepository",
    "MemoryRepository",
    "OwnerRepository",
]
