"""Memory subsystem tests: extractor, embeddings, manager, dedup, chat flow.

All LLM calls are scripted via FakeProvider; embeddings are the deterministic
local-hash provider. Requires PostgreSQL (skips if unavailable).
"""

from __future__ import annotations

import json

import pytest

from app.memory.embeddings import (
    EmbeddingError,
    LocalHashEmbeddingProvider,
    build_embedding_provider,
)
from app.memory.extractor import MemoryExtractor
from app.memory.manager import MemoryManager
from tests.conftest import FakeProvider

# --- Embeddings -----------------------------------------------------------------


async def test_local_embeddings_deterministic_and_normalised() -> None:
    embedder = LocalHashEmbeddingProvider()
    a = await embedder.embed("Boss prefers meetings after 6 PM.")
    b = await embedder.embed("Boss prefers meetings after 6 PM.")
    assert a == b
    assert len(a) == embedder.dimensions == 256
    # L2-normalised.
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6


async def test_local_embeddings_differ_for_different_text() -> None:
    embedder = LocalHashEmbeddingProvider()
    a = await embedder.embed("meetings after six")
    b = await embedder.embed("goa beach holiday")
    assert a != b


def test_embedding_factory_local() -> None:
    provider = build_embedding_provider(
        provider="local", api_key=None, model="x", dimensions=1536
    )
    assert isinstance(provider, LocalHashEmbeddingProvider)


async def test_embedding_factory_openai_without_key_is_unconfigured() -> None:
    provider = build_embedding_provider(
        provider="openai", api_key=None, model="text-embedding-3-small", dimensions=1536
    )
    with pytest.raises(EmbeddingError):
        await provider.embed("hi")


# --- Extractor -----------------------------------------------------------------


async def test_extraction_parses_structured_output() -> None:
    provider = FakeProvider()
    provider.reply = json.dumps(
        {
            "memories": [
                {
                    "type": "semantic",
                    "content": "Boss prefers meetings after 6 PM.",
                    "importance": 0.9,
                    "confidence": 0.95,
                }
            ]
        }
    )
    extractor = MemoryExtractor(provider=provider)
    candidates = await extractor.extract("I prefer meetings after 6 PM", "Noted!")

    assert len(candidates) == 1
    assert candidates[0].memory_type == "semantic"
    assert candidates[0].content == "Boss prefers meetings after 6 PM."
    assert candidates[0].importance == 0.9


async def test_extraction_parses_markdown_fenced_json() -> None:
    provider = FakeProvider()
    provider.reply = (
        '```json\n{"memories": [{"type": "relationship", '
        '"content": "Rahul is Boss\'s college project partner.", '
        '"importance": 0.8, "confidence": 0.9}]}\n```'
    )
    extractor = MemoryExtractor(provider=provider)
    candidates = await extractor.extract(
        "Rahul is my college project partner", "Got it."
    )
    assert len(candidates) == 1
    assert candidates[0].memory_type == "relationship"


async def test_extraction_empty_memories() -> None:
    provider = FakeProvider()
    provider.reply = '{"memories": []}'
    extractor = MemoryExtractor(provider=provider)
    assert await extractor.extract("Hi", "Hello!") == []


async def test_extraction_survives_invalid_json() -> None:
    provider = FakeProvider()
    provider.reply = "I am a chatty LLM and refuse to emit JSON."
    extractor = MemoryExtractor(provider=provider)
    assert await extractor.extract("Hi", "Hello") == []


async def test_extraction_survives_provider_error() -> None:
    provider = FakeProvider()
    provider.error = RuntimeError("LLM down")
    extractor = MemoryExtractor(provider=provider)
    assert await extractor.extract("Hi", "Hello") == []


async def test_extraction_clamps_out_of_range_scores() -> None:
    provider = FakeProvider()
    provider.reply = json.dumps(
        {
            "memories": [
                {
                    "type": "semantic",
                    "content": "Boss likes blue.",
                    "importance": 7.5,
                    "confidence": -3,
                }
            ]
        }
    )
    extractor = MemoryExtractor(provider=provider)
    candidates = await extractor.extract("I like blue", "Cool")
    assert candidates[0].importance == 1.0
    assert candidates[0].confidence == 0.0


async def test_extraction_rejects_invalid_memory_type() -> None:
    provider = FakeProvider()
    provider.reply = json.dumps(
        {"memories": [{"type": "procedural", "content": "something", "importance": 0.5}]}
    )
    extractor = MemoryExtractor(provider=provider)
    assert await extractor.extract("Hi", "Hello") == []


# --- MemoryManager: store / search / lifecycle ----------------------------------


async def test_store_and_search_memory(memory_manager: MemoryManager, db_owner_id) -> None:
    await memory_manager.store_memory(
        db_owner_id,
        memory_type="semantic",
        content="Boss prefers meetings after 6 PM.",
        importance=0.9,
        confidence=0.95,
    )
    results = await memory_manager.search_memories(
        db_owner_id, "meeting schedule preference", limit=3
    )
    assert len(results) == 1
    assert "6 PM" in results[0].memory.content
    assert results[0].similarity > 0


async def test_search_filters_by_memory_type(memory_manager, db_owner_id) -> None:
    await memory_manager.store_memory(
        db_owner_id, memory_type="semantic", content="Boss prefers tea over coffee."
    )
    await memory_manager.store_memory(
        db_owner_id,
        memory_type="relationship",
        content="Rahul is Boss's college project partner.",
    )
    results = await memory_manager.search_memories(
        db_owner_id, "Rahul project partner", memory_types=["relationship"]
    )
    assert len(results) == 1
    assert results[0].memory.memory_type.value == "relationship"


async def test_delete_memory(memory_manager, db_owner_id) -> None:
    stored = await memory_manager.store_memory(
        db_owner_id, memory_type="episodic", content="Boss visited Mumbai in May."
    )
    assert await memory_manager.delete_memory(db_owner_id, stored.id) is True
    assert await memory_manager.delete_memory(db_owner_id, stored.id) is False
    assert await memory_manager.get_memory(db_owner_id, stored.id) is None


# --- Deduplication ---------------------------------------------------------------


async def test_duplicate_memories_are_merged(memory_manager, db_owner_id) -> None:
    first = await memory_manager.store_candidates(
        db_owner_id,
        _candidates(
            [("semantic", "Boss prefers meetings after 6 PM.", 0.8, 0.9)]
        ),
    )
    assert first == {"stored": 1, "updated": 0, "duplicates": 0}

    # Substantially the same memory phrased differently - but identical here
    # (identical strings -> identical hash vectors -> similarity 1.0).
    second = await memory_manager.store_candidates(
        db_owner_id,
        _candidates(
            [("semantic", "Boss prefers meetings after 6 PM.", 0.9, 0.95)]
        ),
    )
    assert second == {"stored": 0, "updated": 0, "duplicates": 1}

    memories = await memory_manager.list_memories(db_owner_id)
    assert len(memories) == 1
    # Importance was raised to the max of the two (LLM "remember" upgrade).
    assert memories[0].importance == 0.9


async def test_distinct_memories_are_both_stored(memory_manager, db_owner_id) -> None:
    counts = await memory_manager.store_candidates(
        db_owner_id,
        _candidates(
            [
                ("semantic", "Boss prefers meetings after 6 PM.", 0.8, 0.9),
                ("relationship", "Rahul is Boss's college project partner.", 0.8, 0.9),
                ("episodic", "Boss and Rahul discussed the Nexus architecture.", 0.6, 0.85),
            ]
        ),
    )
    assert counts == {"stored": 3, "updated": 0, "duplicates": 0}
    assert len(await memory_manager.list_memories(db_owner_id)) == 3


def _candidates(rows):
    from app.memory.extractor import CandidateMemory

    return [
        CandidateMemory(
            memory_type=t, content=c, importance=i, confidence=conf
        )
        for t, c, i, conf in rows
    ]


# --- Owner isolation (spec §28) ----------------------------------------------------


async def test_memory_isolation_between_owners(memory_manager, db_owner_id, db_session_factory) -> None:
    from app.database.models import Owner

    async with db_session_factory() as session:
        stranger = Owner(name="stranger")
        session.add(stranger)
        await session.commit()
        stranger_id = stranger.id

    await memory_manager.store_memory(
        db_owner_id,
        memory_type="semantic",
        content="Boss prefers meetings after 6 PM.",
    )
    await memory_manager.store_memory(
        stranger_id,
        memory_type="semantic",
        content="Stranger enjoys morning runs.",
    )

    boss_hits = await memory_manager.search_memories(db_owner_id, "meetings", limit=10)
    stranger_hits = await memory_manager.search_memories(stranger_id, "meetings", limit=10)

    assert all("Boss" in r.memory.content for r in boss_hits)
    assert all("Stranger" in r.memory.content for r in stranger_hits)
    assert not any("Stranger" in r.memory.content for r in boss_hits)


# --- Chat flow with memory (agent-level) ---------------------------------------------


async def test_chat_persists_and_injects_memory(
    db_agent, db_owner_id, fake_provider: FakeProvider
) -> None:
    """Full pipeline: chat -> extraction (scripted) -> storage -> retrieval
    into the next turn's context."""
    extraction_json = json.dumps(
        {
            "memories": [
                {
                    "type": "semantic",
                    "content": "Boss prefers meetings after 6 PM.",
                    "importance": 0.9,
                    "confidence": 0.95,
                }
            ]
        }
    )
    # Turn 1: chat reply, then the background extraction pops the JSON.
    fake_provider.script = ["Got it!", extraction_json]

    session_obj = await db_agent.create_session(db_owner_id)
    reply = await db_agent.process_message(
        db_owner_id, session_obj.session_id, "I prefer meetings after 6 PM."
    )
    assert reply == "Got it!"

    # Let the background extraction task finish deterministically.
    await db_agent.aclose()

    # Turn 2 (new session = new context): the memory must be retrieved and
    # injected as a system message before the history.
    session_obj_2 = await db_agent.create_session(db_owner_id)
    fake_provider.script = ["After 6 PM, of course."]
    await db_agent.process_message(
        db_owner_id, session_obj_2.session_id, "When should I schedule my meetings?"
    )

    second_call = fake_provider.calls[-1]
    system_blocks = [m for m in second_call if m["role"] == "system"]
    assert len(system_blocks) == 2  # prompt + memory block
    assert "Boss prefers meetings after 6 PM." in system_blocks[1]["content"]


async def test_chat_memory_survives_agent_restart(
    db_session_factory, memory_manager, fake_provider, owner_ids
) -> None:
    """The spec §24 critical scenario: stop the 'server' (build a new agent
    with a fresh store), and the memory is still retrieved."""
    from app.agent.agent import NexusAgent
    from app.agent.context import ContextBuilder

    owner_a, _owner_b = owner_ids

    extraction_json = json.dumps(
        {
            "memories": [
                {
                    "type": "relationship",
                    "content": "Rahul is Boss's college project partner.",
                    "importance": 0.9,
                    "confidence": 0.9,
                }
            ]
        }
    )
    store = DatabaseSessionStoreFixture(db_session_factory)
    agent_one = NexusAgent(
        provider=fake_provider,
        sessions=store,
        context_builder=ContextBuilder(system_prompt="You are Nexus."),
        memory_manager=memory_manager,
    )
    fake_provider.script = ["Noted.", extraction_json]
    session_obj = await agent_one.create_session(owner_a)
    await agent_one.process_message(
        owner_a, session_obj.session_id, "Rahul is my college project partner."
    )
    await agent_one.aclose()

    # "Restart": brand-new agent + store, same database.
    store_two = DatabaseSessionStoreFixture(db_session_factory)
    agent_two = NexusAgent(
        provider=fake_provider,
        sessions=store_two,
        context_builder=ContextBuilder(system_prompt="You are Nexus."),
        memory_manager=memory_manager,
    )
    fake_provider.calls.clear()
    fake_provider.script = ["Rahul is your college project partner."]
    session_obj_2 = await agent_two.create_session(owner_a)
    reply = await agent_two.process_message(
        owner_a, session_obj_2.session_id, "Who is Rahul?"
    )
    assert "project partner" in reply

    system_blocks = [m for m in fake_provider.calls[-1] if m["role"] == "system"]
    assert any("Rahul" in b["content"] for b in system_blocks)


def DatabaseSessionStoreFixture(factory):
    from app.database.session_store import DatabaseSessionStore

    return DatabaseSessionStore(session_factory=factory)
