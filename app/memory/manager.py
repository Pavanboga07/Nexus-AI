"""MemoryManager: the agent-facing memory API.

Owns the full memory pipeline:

    candidates (from extractor)
        -> deduplication (vector similarity vs existing memories)
        -> embedding
        -> PostgreSQL + pgvector (via MemoryRepository)

and retrieval:

    query text
        -> embedding
        -> pgvector cosine KNN (owner-scoped)
        -> relevant memories for the context builder

The agent NEVER issues SQLAlchemy queries; it talks to this class only. A
future policy layer (Part 4) wraps this same interface.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Memory, MemoryType
from app.database.repositories import MemoryRepository
from app.memory.embeddings import EmbeddingProvider
from app.memory.extractor import CandidateMemory

logger = logging.getLogger("nexus.memory.manager")


@dataclass
class MemorySearchResult:
    memory: Memory
    similarity: float


class MemoryManager:
    """Owner-scoped long-term memory operations."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embeddings: EmbeddingProvider,
        dedup_threshold: float = 0.92,
    ) -> None:
        self._session_factory = session_factory
        self._embeddings = embeddings
        self._repo = MemoryRepository(dedup_threshold=dedup_threshold)
        self._dedup_threshold = dedup_threshold

    # --- Storage -----------------------------------------------------------

    async def store_memory(
        self,
        owner_id: uuid.UUID,
        *,
        memory_type: str,
        content: str,
        importance: float = 0.5,
        confidence: float = 0.8,
        source_message_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> Memory:
        """Embed and persist a single memory. Raises on invalid type."""
        memory_type_enum = MemoryType(memory_type)
        embedding = await self._embeddings.embed(content)
        async with self._session_factory() as session:
            memory = await self._repo.add(
                session,
                owner_id=owner_id,
                memory_type=memory_type_enum,
                content=content,
                embedding=embedding,
                importance=max(0.0, min(1.0, importance)),
                confidence=max(0.0, min(1.0, confidence)),
                source_message_id=source_message_id,
                metadata=metadata,
            )
            await session.commit()
            logger.info(
                "memory_stored id=%s type=%s", memory.id, memory_type
            )
            return memory

    async def store_candidates(
        self,
        owner_id: uuid.UUID,
        candidates: list[CandidateMemory],
        *,
        source_message_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        """Store validated candidates with deduplication.

        Returns counts: {"stored": n, "updated": n, "duplicates": n}.
        - duplicate: similarity >= threshold -> refresh the existing memory's
          content/importance instead of inserting a near-identical row.
        - near-identical content is a full duplicate only when similarity is
          very high; lower similarity falls through to a new memory.
        """
        counts = {"stored": 0, "updated": 0, "duplicates": 0}
        for candidate in candidates:
            embedding = await self._embeddings.embed(candidate.content)
            async with self._session_factory() as session:
                similar = await self._repo.find_similar(
                    session, owner_id, embedding
                )
                if similar:
                    existing, similarity = similar[0]
                    if similarity >= self._dedup_threshold:
                        # Treat as the same memory: keep the row, refresh it.
                        await self._repo.update(
                            session,
                            existing,
                            content=candidate.content,
                            importance=max(
                                existing.importance, candidate.importance
                            ),
                            confidence=max(
                                existing.confidence, candidate.confidence
                            ),
                            embedding=embedding,
                        )
                        await session.commit()
                        counts["duplicates"] += 1
                        logger.info(
                            "memory_duplicate_merged id=%s similarity=%.3f",
                            existing.id,
                            similarity,
                        )
                        continue

                await self._repo.add(
                    session,
                    owner_id=owner_id,
                    memory_type=MemoryType(candidate.memory_type),
                    content=candidate.content,
                    embedding=embedding,
                    importance=candidate.importance,
                    confidence=candidate.confidence,
                    source_message_id=source_message_id,
                )
                await session.commit()
                counts["stored"] += 1
        return counts

    # --- Retrieval ---------------------------------------------------------

    async def search_memories(
        self,
        owner_id: uuid.UUID,
        query: str,
        *,
        limit: int = 5,
        memory_types: list[str] | None = None,
    ) -> list[MemorySearchResult]:
        """Vector similarity search for ``query``, owner-scoped."""
        query_embedding = await self._embeddings.embed(query)
        types: list[MemoryType] | None = None
        if memory_types:
            types = [MemoryType(t) for t in memory_types]
        async with self._session_factory() as session:
            rows = await self._repo.search(
                session,
                owner_id,
                query_embedding,
                limit=limit,
                memory_types=types,
            )
            await session.commit()
        return [MemorySearchResult(memory=m, similarity=s) for m, s in rows]

    async def get_relevant_memories(
        self,
        owner_id: uuid.UUID,
        query: str,
        *,
        limit: int = 5,
    ) -> list[Memory]:
        """Memories to inject into the next LLM context.

        Deliberately untyped (all memory types compete) and similarity-
        threshold-free here; ranking is the repository's job. Part 4's policy
        layer will wrap this method.
        """
        results = await self.search_memories(owner_id, query, limit=limit)
        return [r.memory for r in results]

    async def get_memory(
        self, owner_id: uuid.UUID, memory_id: uuid.UUID
    ) -> Memory | None:
        async with self._session_factory() as session:
            return await self._repo.get(session, memory_id, owner_id)

    async def list_memories(
        self,
        owner_id: uuid.UUID,
        *,
        memory_type: str | None = None,
        limit: int = 100,
    ) -> list[Memory]:
        type_enum = MemoryType(memory_type) if memory_type else None
        async with self._session_factory() as session:
            memories = await self._repo.list_memories(
                session, owner_id, memory_type=type_enum, limit=limit
            )
        return memories

    async def delete_memory(self, owner_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            deleted = await self._repo.delete(session, memory_id, owner_id)
            await session.commit()
        if deleted:
            logger.info("memory_deleted id=%s", memory_id)
        return deleted


__all__ = ["MemoryManager", "MemorySearchResult"]
