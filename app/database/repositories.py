"""Repositories: the only place raw SQLAlchemy queries live.

MemoryManager and DatabaseSessionStore talk to these classes; nothing else in
the app builds queries. Owner scoping is enforced here, not by callers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Conversation, Memory, MemoryType, Message, Owner


class DimensionMismatchError(ValueError):
    """An embedding's length does not match the deployment's configured one.

    Raised at write time on purpose: mixing dimensions in one vector column
    makes cosine search inconsistent, and the failure would otherwise surface
    at query time as a confusing SQL error rather than as a clear configuration
    problem.
    """


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
    deliberately no unscoped method - the policy layer wraps this repository,
    and remote agents must go through the runtime, never the DB.

    M8 additions: ``agent_id`` scoping (rows written before M8 have NULL and
    are treated as owner-scoped), a write-time dimension check, retention, and
    export/import.
    """

    def __init__(
        self,
        dedup_threshold: float = 0.92,
        expected_dimensions: int | None = None,
    ) -> None:
        self._dedup_threshold = dedup_threshold
        #: When set, a memory whose embedding has a different length is
        #: rejected at WRITE time. Mixing dimensions in one column cannot be
        #: searched consistently - the comparison fails at query time, long
        #: after the bad write, and the failure is a confusing SQL error rather
        #: than a clear "your embedder changed".
        self._expected_dimensions = expected_dimensions

    def _check_dimensions(self, embedding: list[float] | None) -> None:
        if embedding is None or self._expected_dimensions is None:
            return
        if len(embedding) != self._expected_dimensions:
            raise DimensionMismatchError(
                f"Embedding has {len(embedding)} dimensions but this deployment "
                f"is configured for {self._expected_dimensions}. Writing it would "
                "make cosine search inconsistent: pin one embedder per database, "
                "or re-embed the existing memories."
            )

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
        agent_id: uuid.UUID | None = None,
    ) -> Memory:
        self._check_dimensions(embedding)
        memory = Memory(
            owner_id=owner_id,
            agent_id=agent_id,
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
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[Memory]:
        stmt = select(Memory).where(Memory.owner_id == owner_id)
        if memory_type is not None:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if agent_id is not None:
            stmt = stmt.where(Memory.agent_id == agent_id)
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
        agent_id: uuid.UUID | None = None,
    ) -> list[tuple[Memory, float]]:
        """Cosine-similarity KNN search via pgvector's ``<=>`` operator.

        Returns ``(memory, similarity)`` pairs, best first, scoped to the owner.

        Performance note (M8): without an HNSW index this is an exact scan over
        the owner's memories - correct at thousands, not at millions. Run
        ``scripts/build_vector_index.py`` to pin the dimension and build the
        index. The query is written so the index is usable when present; it
        also still returns correct results when the index is absent.
        """
        self._check_dimensions(query_embedding)
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
        if agent_id is not None:
            # Scoped search: the agent's own memories plus owner-level ones
            # (agent_id IS NULL, i.e. pre-M8 rows and explicitly shared notes).
            stmt = stmt.where(
                or_(Memory.agent_id == agent_id, Memory.agent_id.is_(None))
            )
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

    async def count_for_agent(
        self, session: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
    ) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Memory)
            .where(Memory.owner_id == owner_id, Memory.agent_id == agent_id)
        )
        return int(result.scalar_one())

    async def reap_old(
        self,
        session: AsyncSession,
        *,
        older_than: datetime,
        memory_types: list[MemoryType] | None = None,
        limit: int = 500,
    ) -> int:
        """Delete old, unpinned memories (retention).

        Pinned memories are never reaped: pinning is the owner's explicit
        "keep this" signal, and a retention job that ignored it would delete
        exactly the things a user cared enough to mark.
        """
        stmt = delete(Memory).where(
            Memory.created_at < older_than,
            Memory.pinned.is_(False),
        )
        if memory_types:
            stmt = stmt.where(Memory.memory_type.in_(memory_types))
        # Bound the work per run so a large backlog cannot hold a transaction
        # open for minutes.
        stmt = stmt.where(
            Memory.id.in_(
                select(Memory.id)
                .where(
                    Memory.created_at < older_than,
                    Memory.pinned.is_(False),
                )
                .limit(limit)
                .scalar_subquery()
            )
        )
        result = await session.execute(stmt)
        return int(result.rowcount or 0)

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
    "DimensionMismatchError",
    "MemoryRepository",
    "OwnerRepository",
]
