"""M8 regression tests: memory scoping, dimension enforcement, portability.

Audit findings covered:

  H7   ``memories.embedding`` was a dimensionless ``vector`` with NO index, so
       every retrieval was an exact cosine scan over the owner's memories -
       O(n), fine at thousands and fatal at millions. pgvector cannot build an
       HNSW index on a dimensionless column at all, so pinning the dimension is
       a prerequisite rather than a tuning knob.
  --   Nothing enforced the embedding dimension, so a provider switch could
       write vectors of a different length into the same column, making cosine
       search inconsistent - and the failure would surface at query time as a
       confusing SQL error, long after the bad write.
  --   Memory was owner-scoped only. With many agents per owner (M4) every
       agent could read every other agent's memories.
  --   Extraction runs on every chat turn and nothing ever removed a memory:
       unbounded growth with no retention, no pin, and no delete-all path.
  --   Memory was welded to this database. An agent moved to another host lost
       everything, which contradicts the point of an independently hostable
       agent.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database.models import Memory, MemoryType
from app.database.repositories import DimensionMismatchError, MemoryRepository
from app.memory.manager import MemoryManager

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


# ---------------------------------------------------------------------------
# H7: the vector index exists and the column can be indexed
# ---------------------------------------------------------------------------


def test_index_script_exists_and_refuses_unsafe_builds() -> None:
    """The index must be buildable, and the script must verify before acting.

    pgvector refuses to index a dimensionless column, so a naive
    ``CREATE INDEX`` fails with "column does not have dimensions". The script
    therefore pins the dimension itself, and must refuse when doing so would
    break existing data.
    """
    script = (SCRIPTS_DIR / "build_vector_index.py").read_text(encoding="utf-8")
    # It must pin the dimension (the prerequisite) ...
    assert "ALTER TABLE memories ALTER COLUMN embedding TYPE vector(" in script
    # ... check validity, not just existence (a failed concurrent build leaves
    # an INVALID index the planner silently ignores) ...
    assert "indisvalid" in script
    # ... refuse mixed dimensions ...
    assert "holds vectors of more than one" in script
    # ... and be usable for teardown.
    assert "--drop" in script


def test_index_script_uses_a_real_autocommit_connection() -> None:
    """CREATE INDEX CONCURRENTLY cannot run in a transaction block.

    Setting isolation_level on a pooled connection is not reliable; a previous
    version left an INVALID index behind because of exactly that.
    """
    script = (SCRIPTS_DIR / "build_vector_index.py").read_text(encoding="utf-8")
    assert "driver_connection" in script
    assert "CONCURRENTLY" in script


def test_memory_model_documents_the_dimension_prerequisite() -> None:
    doc = Memory.__doc__ or ""
    assert "dimensionless" in doc
    assert "HNSW" in doc
    # The operational consequence must be stated, not implied.
    assert "scan" in doc.lower()


# ---------------------------------------------------------------------------
# Dimension enforcement at write time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_dimension_is_rejected_at_write_time(db_session_factory, db_owner_id) -> None:
    """A mismatched embedding must fail on WRITE, not on query.

    Otherwise a provider switch silently poisons the column and the error shows
    up later as an opaque SQL failure.
    """
    repo = MemoryRepository(expected_dimensions=256)
    async with db_session_factory() as session:
        with pytest.raises(DimensionMismatchError) as exc:
            await repo.add(
                session,
                owner_id=db_owner_id,
                memory_type=MemoryType.semantic,
                content="wrong size",
                embedding=[0.1] * 128,
                importance=0.5,
                confidence=0.8,
            )
        assert "128" in str(exc.value) and "256" in str(exc.value)

        # The correct dimension is accepted.
        memory = await repo.add(
            session,
            owner_id=db_owner_id,
            memory_type=MemoryType.semantic,
            content="right size",
            embedding=[0.1] * 256,
            importance=0.5,
            confidence=0.8,
        )
        assert memory.id is not None
        await session.commit()


@pytest.mark.asyncio
async def test_search_rejects_a_mismatched_query_vector(db_session_factory, db_owner_id) -> None:
    """A query embedding of the wrong size is also rejected, with a clear error."""
    repo = MemoryRepository(expected_dimensions=256)
    async with db_session_factory() as session:
        with pytest.raises(DimensionMismatchError):
            await repo.search(session, db_owner_id, [0.1] * 512)


@pytest.mark.asyncio
async def test_no_dimension_configured_allows_any_size(db_session_factory, db_owner_id) -> None:
    """Unconfigured (the default) must not start rejecting writes."""
    repo = MemoryRepository()  # no expected_dimensions
    async with db_session_factory() as session:
        memory = await repo.add(
            session,
            owner_id=db_owner_id,
            memory_type=MemoryType.semantic,
            content="anything",
            embedding=[0.1] * 7,
            importance=0.5,
            confidence=0.8,
        )
        assert memory.id is not None
        await session.commit()


def test_manager_reports_its_dimension() -> None:
    """The index must be built with the SAME dimension the app writes."""

    class _StubEmbeddings:
        dimensions = 256

        async def embed(self, text: str) -> list[float]:
            return [0.0] * 256

    manager = MemoryManager(
        session_factory=None,  # type: ignore[arg-type]
        embeddings=_StubEmbeddings(),  # type: ignore[arg-type]
        expected_dimensions=256,
    )
    assert manager.dimensions == 256


# ---------------------------------------------------------------------------
# Agent scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memories_are_scoped_to_an_agent(db_session_factory, owner_ids) -> None:
    """One owner's agents must not read each other's memories."""
    owner_id, _other = owner_ids
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    repo = MemoryRepository()

    async with db_session_factory() as session:
        for content, agent in (("A's secret", agent_a), ("B's secret", agent_b)):
            await repo.add(
                session,
                owner_id=owner_id,
                agent_id=agent,
                memory_type=MemoryType.semantic,
                content=content,
                embedding=[0.1] * 4,
                importance=0.5,
                confidence=0.8,
            )
        await session.commit()

        a_memories = await repo.list_memories(session, owner_id, agent_id=agent_a)
        b_memories = await repo.list_memories(session, owner_id, agent_id=agent_b)
        all_memories = await repo.list_memories(session, owner_id)

    assert [m.content for m in a_memories] == ["A's secret"]
    assert [m.content for m in b_memories] == ["B's secret"]
    assert len(all_memories) == 2


@pytest.mark.asyncio
async def test_scoped_search_includes_owner_level_memories(
    db_session_factory, owner_ids
) -> None:
    """A scoped search sees the agent's own rows PLUS owner-level (NULL) ones.

    NULL means "pre-M8 row" or "explicitly shared", so excluding it would make
    every existing memory invisible to a scoped agent - a silent data-loss
    appearance.
    """
    owner_id, _ = owner_ids
    agent_a = uuid.uuid4()
    repo = MemoryRepository()
    vector = [0.5] * 4

    async with db_session_factory() as session:
        await repo.add(
            session,
            owner_id=owner_id,
            agent_id=agent_a,
            memory_type=MemoryType.semantic,
            content="agent-owned",
            embedding=vector,
            importance=0.5,
            confidence=0.8,
        )
        await repo.add(
            session,
            owner_id=owner_id,
            agent_id=None,  # owner-level
            memory_type=MemoryType.semantic,
            content="owner-level",
            embedding=vector,
            importance=0.5,
            confidence=0.8,
        )
        await repo.add(
            session,
            owner_id=owner_id,
            agent_id=uuid.uuid4(),  # a DIFFERENT agent
            memory_type=MemoryType.semantic,
            content="other agent",
            embedding=vector,
            importance=0.5,
            confidence=0.8,
        )
        await session.commit()

        scoped = await repo.search(session, owner_id, vector, limit=10, agent_id=agent_a)

    contents = {m.content for m, _ in scoped}
    assert contents == {"agent-owned", "owner-level"}, contents


@pytest.mark.asyncio
async def test_agent_id_is_not_a_foreign_key_so_memories_survive_agent_deletion() -> None:
    """Memories are the OWNER's data, not the agent's.

    A foreign key with ON DELETE CASCADE would let deleting an agent destroy
    the owner's memory; RESTRICT would block the deletion. Neither is right.
    """
    column = Memory.__table__.columns["agent_id"]
    assert column.foreign_keys == set(), (
        "agent_id must not be a foreign key; memories outlive an agent"
    )


# ---------------------------------------------------------------------------
# Retention and pinning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_reaps_old_episodic_but_never_pinned(
    db_session_factory, db_owner_id
) -> None:
    """Retention must remove ageing events and never a pinned memory.

    Semantic and relationship memories are durable facts about the owner and
    are never reaped by a retention job.
    """
    repo = MemoryRepository()
    vector = [0.1] * 4
    old = datetime.now(timezone.utc) - timedelta(days=400)

    async with db_session_factory() as session:
        rows = [
            ("old episodic", MemoryType.episodic, False),
            ("pinned old episodic", MemoryType.episodic, True),
            ("old semantic", MemoryType.semantic, False),
        ]
        for content, memory_type, pinned in rows:
            memory = await repo.add(
                session,
                owner_id=db_owner_id,
                memory_type=memory_type,
                content=content,
                embedding=vector,
                importance=0.5,
                confidence=0.8,
            )
            memory.created_at = old
            memory.pinned = pinned
        await session.commit()

        removed = await repo.reap_old(
            session,
            older_than=datetime.now(timezone.utc) - timedelta(days=90),
            memory_types=[MemoryType.episodic],
        )
        await session.commit()
        remaining = await repo.list_memories(session, db_owner_id)

    assert removed == 1
    remaining_contents = {m.content for m in remaining}
    assert remaining_contents == {"pinned old episodic", "old semantic"}


@pytest.mark.asyncio
async def test_manager_retention_is_opt_in(db_session_factory, db_owner_id) -> None:
    """No retention setting means no deletion - a default must not destroy data."""
    class _StubEmbeddings:
        dimensions = 4

        async def embed(self, text: str) -> list[float]:
            return [0.1] * 4

    manager = MemoryManager(
        session_factory=db_session_factory,
        embeddings=_StubEmbeddings(),  # type: ignore[arg-type]
    )
    assert await manager.reap_expired(episodic_retention_days=None) == 0
    assert await manager.reap_expired(episodic_retention_days=0) == 0


# ---------------------------------------------------------------------------
# Portability: export / import
# ---------------------------------------------------------------------------


class _StubEmbeddings:
    """Deterministic stand-in embedder with clean similarity semantics.

    Property that matters here: IDENTICAL text embeds to an identical vector
    (similarity 1.0), and DIFFERENT text embeds to a near-orthogonal one
    (similarity well under the 0.92 dedup threshold).

    A realistic bag-of-words stub is a poor fit for these tests: two short
    strings sharing one word measured 0.926 cosine, which is ABOVE the default
    threshold, so dedup would fire on genuinely different memories and the test
    would be measuring the stub rather than the code. Two earlier stub designs
    were rejected for exactly this reason (4-dim vectors gave 0.974).
    """

    dimensions = 64

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        import hashlib
        import math

        self.calls.append(text)
        normalized = " ".join((text or "").lower().split())
        digest = hashlib.sha256(normalized.encode("utf-8")).digest()
        # Spread the digest across all dimensions so two different texts land
        # near-orthogonal, then L2-normalise as a real embedder would.
        vector = [
            (digest[i % len(digest)] - 127.5) / 127.5 for i in range(self.dimensions)
        ]
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


@pytest.mark.asyncio
async def test_export_then_import_moves_memory_between_databases(
    db_session_factory, owner_ids
) -> None:
    """Memory must be portable, or an agent cannot move hosts.

    Export deliberately omits embeddings: they are a function of the text and a
    specific model, so shipping them would pin the importer to the exporter's
    embedder. The importer re-embeds with its own provider.
    """
    owner_id, other_owner = owner_ids
    embeddings = _StubEmbeddings()
    source = MemoryManager(session_factory=db_session_factory, embeddings=embeddings)  # type: ignore[arg-type]
    await source.store_memory(owner_id, memory_type="semantic", content="Boss prefers mornings")
    await source.store_memory(owner_id, memory_type="semantic", content="Boss lives in Pune")

    document = await source.export_memories(owner_id)
    assert document["format"] == "nexus-memory-export"
    assert document["count"] == 2
    assert all("embedding" not in m for m in document["memories"]), (
        "export must not carry embeddings"
    )
    # The owner id in the document is informational; the importer chooses the
    # destination owner.
    assert document["owner_id"] == str(owner_id)

    target = MemoryManager(session_factory=db_session_factory, embeddings=embeddings)  # type: ignore[arg-type]
    result = await target.import_memories(other_owner, document)
    assert result["stored"] == 2

    moved = await target.list_memories(other_owner)
    assert {m.content for m in moved} == {"Boss prefers mornings", "Boss lives in Pune"}

    # Importing again must not double the memory: dedup applies on import.
    again = await target.import_memories(other_owner, document)
    assert again["stored"] == 0
    assert again["duplicates"] == 2
    assert len(await target.list_memories(other_owner)) == 2


@pytest.mark.asyncio
async def test_import_rejects_a_foreign_document(db_session_factory, db_owner_id) -> None:
    """A document that is not a Nexus export must be refused, not partially read."""
    manager = MemoryManager(
        session_factory=db_session_factory,
        embeddings=_StubEmbeddings(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        await manager.import_memories(db_owner_id, {"notes": ["not an export"]})
    with pytest.raises(ValueError):
        await manager.import_memories(db_owner_id, "not even a dict")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_import_skips_empty_entries_without_failing(
    db_session_factory, db_owner_id
) -> None:
    """A partially valid export imports what it can and reports the rest.

    A blank entry and an entry with an unknown memory type are both skipped: a
    bad row in an export must not abort the whole import, and must not be
    silently counted as imported either.
    """
    manager = MemoryManager(
        session_factory=db_session_factory,
        embeddings=_StubEmbeddings(),  # type: ignore[arg-type]
    )
    document = {
        "format": "nexus-memory-export",
        "version": 1,
        "memories": [
            {"memory_type": "semantic", "content": "good one"},
            {"memory_type": "semantic", "content": "   "},
            {"memory_type": "not-a-type", "content": "bad type"},
        ],
    }
    result = await manager.import_memories(db_owner_id, document)
    assert result["stored"] == 1, result
    assert result["duplicates"] == 0, result
    assert result["skipped"] == 2, result

    stored = await manager.list_memories(db_owner_id)
    assert [m.content for m in stored] == ["good one"]


def test_export_does_not_leak_private_columns() -> None:
    """The export shape is a contract; it must not gain internal fields."""
    import app.memory.manager as manager_module

    src = inspect.getsource(manager_module.MemoryManager.export_memories)
    for forbidden in ("encrypted_private_key", "public_key", "patch", "policy"):
        assert forbidden not in src
    for expected in ("content", "memory_type", "importance", "created_at"):
        assert expected in src
