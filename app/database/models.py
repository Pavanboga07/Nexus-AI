"""SQLAlchemy models for persistent state (Part 2).

Schema overview:

    owners            one row per Nexus principal (single owner today)
    conversations     one row per session (id == the public session_id)
    messages          chat messages, ordered by created_at
    memories          extracted long-term memories with pgvector embeddings

Every memory and conversation row is scoped by ``owner_id``. Queries must
always filter on it - this is the privacy boundary until the policy layer
arrives in Part 4.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MemoryType(str, enum.Enum):
    semantic = "semantic"
    episodic = "episodic"
    relationship = "relationship"


class Owner(Base):
    """A Nexus principal. Part 2 assumes exactly one (the owner), but the
    schema keeps ``owner_id`` on all dependent rows so multi-user support is
    an application-layer change, not a schema rewrite."""

    __tablename__ = "owners"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    conversations: Mapped[list[Conversation]] = relationship(back_populates="owner")
    memories: Mapped[list[Memory]] = relationship(back_populates="owner")


class Conversation(Base):
    """A persistent conversation. ``id`` is the public session_id from the
    API, so Part 1 session ids remain valid across restarts."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("owners.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    owner: Mapped[Owner] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        order_by="Message.created_at",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": str(self.id),
            "messages": [
                {"role": m.role, "content": m.content} for m in self.messages
            ],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # user | assistant | system
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class Memory(Base):
    """A long-term memory with its pgvector embedding.

    The embedding column is declared dimensionless because the deployment's
    embedder decides the dimension (local-hash: 256, text-embedding-3-small:
    1536). Two consequences, both addressed in M8:

      * **pgvector cannot build an HNSW index on a dimensionless column** - it
        fails with "column does not have dimensions". Pinning the dimension is
        a PREREQUISITE for indexing, not an optimisation.
      * Until the index exists, retrieval is an EXACT cosine scan over the
        owner's memories: correct at thousands, not at millions.

    ``scripts/build_vector_index.py`` verifies the data, pins the dimension and
    builds the index (verified in testing: with 20k rows the planner switches
    to ``Index Scan using ix_memories_embedding_hnsw`` for the repository's
    query shape).

    Dimensions are additionally enforced at WRITE time
    (``MemoryRepository.add``): mixing dimensions in one column cannot be
    searched consistently, and the failure would otherwise appear at query time
    as a confusing SQL error.

    ``agent_id`` is nullable for backward compatibility: rows written before M8
    belong to the owner rather than to a specific agent.
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_owner_type",
            "owner_id",
            "memory_type",
        ),
        # Supports per-agent scoping without a scan.
        Index("ix_memories_owner_agent", "owner_id", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("owners.id"), nullable=False, index=True)
    #: The agent this memory belongs to. NULL = owner-scoped (pre-M8 rows).
    #: Not a FK to agents.id: memories must survive an agent being deleted,
    #: because they are the owner's data, not the agent's.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType, name="memory_type", native_enum=False, length=32),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.8, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Soft pin: a pinned memory is never reaped by retention.
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    owner: Mapped[Owner] = relationship(back_populates="memories")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "memory_type": self.memory_type.value if isinstance(self.memory_type, MemoryType) else self.memory_type,
            "content": self.content,
            "importance": self.importance,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_accessed_at": self.last_accessed_at.isoformat() if self.last_accessed_at else None,
            "metadata": self.metadata_ or {},
        }


__all__ = [
    "Base",
    "Conversation",
    "Memory",
    "MemoryType",
    "Message",
    "Owner",
]
