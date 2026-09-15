"""Database layer tests: CRUD against real PostgreSQL (skips if unavailable).

Covers spec §28: create owner / conversation / message / memory, retrieve,
delete, plus persistence across "restarts" (new store instance, same DB).
"""

from __future__ import annotations

import uuid

import pytest

from app.agent.session import SessionNotFoundError
from app.database.models import MemoryType
from app.database.repositories import (
    ConversationRepository,
    MemoryRepository,
    OwnerRepository,
)
from app.database.session_store import DatabaseSessionStore

pytestmark = pytest.mark.asyncio


# --- Owner -------------------------------------------------------------------


async def test_owner_get_or_create_is_idempotent(db_session_factory) -> None:
    repo = OwnerRepository()
    async with db_session_factory() as session:
        first = await repo.get_or_create_default(session)
        await session.commit()
    async with db_session_factory() as session:
        second = await repo.get_or_create_default(session)
        await session.commit()
    assert first.id == second.id


# --- Conversations & messages --------------------------------------------------


async def test_conversation_message_roundtrip(db_session_factory, db_owner_id) -> None:
    conversations = ConversationRepository()
    async with db_session_factory() as session:
        conversation = await conversations.create(session, db_owner_id)
        await session.commit()
        conversation_id = conversation.id

        await conversations.add_message(
            session, conversation_id, db_owner_id, "user", "Hello"
        )
        await conversations.add_message(
            session, conversation_id, db_owner_id, "assistant", "Hi there"
        )
        await session.commit()

    async with db_session_factory() as session:
        fetched = await conversations.get(session, conversation_id, db_owner_id)
        assert fetched is not None
        assert [m["role"] for m in fetched.to_dict()["messages"]] == [
            "user",
            "assistant",
        ]


async def test_message_wrong_owner_is_invisible(db_session_factory) -> None:
    conversations = ConversationRepository()
    owners = OwnerRepository()
    async with db_session_factory() as session:
        owner = await owners.get_or_create_default(session)
        await session.commit()
        owner_id = owner.id

    async with db_session_factory() as session:
        conversation = await conversations.create(session, owner_id)
        await session.commit()
        conversation_id = conversation.id

    stranger = uuid.uuid4()  # different owner
    async with db_session_factory() as session:
        assert (
            await conversations.get(session, conversation_id, stranger) is None
        )
        with pytest.raises(KeyError):
            await conversations.add_message(
                session, conversation_id, stranger, "user", "sneak"
            )


async def test_clear_messages_keeps_conversation(db_session_factory, db_owner_id) -> None:
    conversations = ConversationRepository()
    async with db_session_factory() as session:
        conversation = await conversations.create(session, db_owner_id)
        await session.commit()
        conversation_id = conversation.id
        await conversations.add_message(
            session, conversation_id, db_owner_id, "user", "x"
        )
        await session.commit()

    async with db_session_factory() as session:
        cleared = await conversations.clear_messages(
            session, conversation_id, db_owner_id
        )
        await session.commit()
        assert cleared is not None
        assert cleared.to_dict()["messages"] == []


# --- Memories ------------------------------------------------------------------


async def test_memory_crud(db_session_factory, db_owner_id, embeddings) -> None:
    memories = MemoryRepository()
    content = "Boss prefers meetings after 6 PM."
    vector = await embeddings.embed(content)

    async with db_session_factory() as session:
        stored = await memories.add(
            session,
            owner_id=db_owner_id,
            memory_type=MemoryType.semantic,
            content=content,
            embedding=vector,
            importance=0.9,
            confidence=0.95,
        )
        await session.commit()
        memory_id = stored.id

    async with db_session_factory() as session:
        fetched = await memories.get(session, memory_id, db_owner_id)
        assert fetched is not None
        assert fetched.content == content
        assert fetched.importance == 0.9
        assert fetched.embedding is not None
        assert len(fetched.embedding) == embeddings.dimensions

    async with db_session_factory() as session:
        assert await memories.delete(session, memory_id, db_owner_id)
        await session.commit()
        assert await memories.get(session, memory_id, db_owner_id) is None


async def test_memory_wrong_owner_invisible(db_session_factory, db_owner_id) -> None:
    memories = MemoryRepository()
    async with db_session_factory() as session:
        stored = await memories.add(
            session,
            owner_id=db_owner_id,
            memory_type=MemoryType.semantic,
            content="secret fact",
            embedding=None,
            importance=0.5,
            confidence=0.5,
        )
        await session.commit()

    stranger = uuid.uuid4()
    async with db_session_factory() as session:
        assert await memories.get(session, stored.id, stranger) is None
        assert not await memories.delete(session, stored.id, stranger)
        listed = await memories.list_memories(session, stranger)
        assert listed == []


# --- Vector search ---------------------------------------------------------------


async def test_vector_search_returns_nearest_first(
    db_session_factory, db_owner_id, embeddings
) -> None:
    memories = MemoryRepository()
    rows = [
        (MemoryType.semantic, "Boss prefers meetings after 6 PM."),
        (MemoryType.relationship, "Rahul is Boss's college project partner."),
        (MemoryType.episodic, "Boss visited Goa last summer."),
    ]
    async with db_session_factory() as session:
        for memory_type, content in rows:
            await memories.add(
                session,
                owner_id=db_owner_id,
                memory_type=memory_type,
                content=content,
                embedding=await embeddings.embed(content),
                importance=0.5,
                confidence=0.9,
            )
        await session.commit()

    query = await embeddings.embed("When should Boss schedule meetings?")
    async with db_session_factory() as session:
        results = await memories.search(
            session, db_owner_id, query, limit=2
        )
        await session.commit()

    assert len(results) == 2
    # The hash embedder keys on character n-grams: the "meetings" memory must
    # outrank the Goa trip.
    assert "meetings after 6 PM" in results[0][0].content
    # last_accessed_at updated by the search.
    assert results[0][0].last_accessed_at is not None


async def test_vector_search_owner_isolation(db_session_factory, db_owner_id, embeddings) -> None:
    memories = MemoryRepository()
    stranger = uuid.uuid4()
    async with db_session_factory() as session:
        await memories.add(
            session,
            owner_id=db_owner_id,
            memory_type=MemoryType.semantic,
            content="Boss prefers meetings after 6 PM.",
            embedding=await embeddings.embed("Boss prefers meetings after 6 PM."),
            importance=0.9,
            confidence=0.9,
        )
        await session.commit()

    query = await embeddings.embed("Boss prefers meetings after 6 PM.")
    async with db_session_factory() as session:
        stranger_results = await memories.search(session, stranger, query, limit=5)
    assert stranger_results == []


# --- Session store persistence ----------------------------------------------------


async def test_sessions_survive_store_restart(db_session_factory) -> None:
    """The Part 2 critical property: a NEW store instance (= server restart)
    sees the same conversations."""
    store_a = DatabaseSessionStore(session_factory=db_session_factory)
    session_obj = await store_a.create_session()
    await store_a.add_message(session_obj.session_id, "user", "Hi, my name is Boss.")

    # "Restart": a fresh store over the same database.
    store_b = DatabaseSessionStore(session_factory=db_session_factory)
    fetched = await store_b.get_session(session_obj.session_id)
    assert fetched.messages == [
        {"role": "user", "content": "Hi, my name is Boss."}
    ]


async def test_session_store_unknown_session(db_store) -> None:
    with pytest.raises(SessionNotFoundError):
        await db_store.get_session("not-a-uuid")
    with pytest.raises(SessionNotFoundError):
        await db_store.get_session(str(uuid.uuid4()))
