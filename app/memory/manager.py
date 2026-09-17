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
from datetime import datetime, timedelta, timezone
from typing import Any

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
        expected_dimensions: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embeddings = embeddings
        self._repo = MemoryRepository(
            dedup_threshold=dedup_threshold,
            expected_dimensions=expected_dimensions,
        )
        self._dedup_threshold = dedup_threshold
        self._expected_dimensions = expected_dimensions

    @property
    def dimensions(self) -> int:
        """The embedding dimension this deployment writes.

        Surfaced because the vector index must be built with the SAME
        dimension, and a mismatch is a silent retrieval failure rather than a
        loud one.
        """
        return self._expected_dimensions or getattr(self._embeddings, "dimensions", 0)

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
        agent_id: uuid.UUID | None = None,
    ) -> Memory:
        """Embed and persist a single memory. Raises on invalid type.

        ``agent_id`` scopes the memory to one of the owner's agents. Omitted
        (None) means owner-scoped, which is also how every pre-M8 row reads.
        """
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
                agent_id=agent_id,
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

    # --- Portability (M8) --------------------------------------------------
    # Memory was welded to this database: an agent moved to another host lost
    # everything, which contradicts the point of an independently hostable
    # agent. Export/import makes memory the owner's data rather than the
    # deployment's.

    async def export_memories(
        self,
        owner_id: uuid.UUID,
        *,
        agent_id: uuid.UUID | None = None,
        limit: int = 10_000,
    ) -> dict[str, Any]:
        """Export memories as a portable document.

        Deliberately does NOT include embeddings: they are a function of the
        text and a specific model, so exporting them would pin the importer to
        the exporter's embedder. The importer re-embeds with its own provider,
        which is what makes a move between deployments work.
        """
        memories = await self.list_memories(owner_id, limit=limit)
        if agent_id is not None:
            memories = [m for m in memories if m.agent_id == agent_id]

        now = datetime.now(timezone.utc)
        return {
            "format": "nexus-memory-export",
            "version": 1,
            "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "owner_id": str(owner_id),
            "agent_id": str(agent_id) if agent_id else None,
            "embedding_dimensions": self.dimensions,
            "count": len(memories),
            "memories": [
                {
                    "memory_type": (
                        m.memory_type.value
                        if hasattr(m.memory_type, "value")
                        else str(m.memory_type)
                    ),
                    "content": m.content,
                    "importance": m.importance,
                    "confidence": m.confidence,
                    "pinned": bool(getattr(m, "pinned", False)),
                    "metadata": m.metadata_ or {},
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in memories
            ],
        }

    async def import_memories(
        self,
        owner_id: uuid.UUID,
        document: dict[str, Any],
        *,
        agent_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        """Import a portable document, re-embedding with THIS deployment's
        provider.

        Deduplication is applied on import, so re-importing the same document
        does not double the memory. Returns counts rather than raising on a
        single bad entry: a partially-valid export should import what it can
        and report the rest.
        """
        if not isinstance(document, dict):
            raise ValueError("Import document must be an object.")
        if document.get("format") != "nexus-memory-export":
            raise ValueError(
                "Not a Nexus memory export: expected format "
                "'nexus-memory-export'."
            )

        stored = 0
        duplicates = 0
        skipped = 0
        for entry in document.get("memories") or []:
            try:
                memory_type = str(entry.get("memory_type") or "semantic")
                content = str(entry.get("content") or "").strip()
                if not content:
                    skipped += 1
                    continue
                embedding = await self._embeddings.embed(content)
                async with self._session_factory() as session:
                    similar = await self._repo.find_similar(
                        session, owner_id, embedding, limit=1
                    )
                    if similar and similar[0][1] >= self._dedup_threshold:
                        duplicates += 1
                        continue
                    await self._repo.add(
                        session,
                        owner_id=owner_id,
                        memory_type=MemoryType(memory_type),
                        content=content,
                        embedding=embedding,
                        importance=float(entry.get("importance", 0.5)),
                        confidence=float(entry.get("confidence", 0.8)),
                        metadata=entry.get("metadata") or {},
                        agent_id=agent_id,
                    )
                    await session.commit()
                    stored += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("memory_import_entry_failed detail=%s", exc)
                skipped += 1

        logger.info(
            "memory_import_complete stored=%d duplicates=%d skipped=%d",
            stored,
            duplicates,
            skipped,
        )
        return {"stored": stored, "duplicates": duplicates, "skipped": skipped}

    async def reap_expired(
        self,
        *,
        episodic_retention_days: int | None = None,
        limit: int = 500,
    ) -> int:
        """Apply retention to old episodic memories.

        Only ``episodic`` memories are reaped: they record events, which age
        out. Semantic and relationship memories are the durable facts about the
        owner and are never removed by a retention job. Pinned memories are
        never removed either.
        """
        if not episodic_retention_days or episodic_retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=episodic_retention_days)
        async with self._session_factory() as session:
            removed = await self._repo.reap_old(
                session,
                older_than=cutoff,
                memory_types=[MemoryType.episodic],
                limit=limit,
            )
            await session.commit()
        if removed:
            logger.info(
                "memory_retention_reaped count=%d older_than_days=%d",
                removed,
                episodic_retention_days,
            )
        return removed


__all__ = ["MemoryManager", "MemorySearchResult"]
