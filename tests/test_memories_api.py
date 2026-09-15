"""API tests for /memories endpoints and Part 1 endpoints over the DB stack."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _seed_memory(db_agent, db_owner_id, content="Boss prefers meetings after 6 PM."):
    return await db_agent._memory.store_memory(
        db_owner_id,
        memory_type="semantic",
        content=content,
        importance=0.9,
        confidence=0.95,
    )


async def test_list_memories_endpoint(db_client, db_agent, db_owner_id) -> None:
    await _seed_memory(db_agent, db_owner_id)
    await _seed_memory(
        db_agent, db_owner_id, "Rahul is Boss's college project partner."
    )

    response = await db_client.get("/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    types = {m["memory_type"] for m in body["memories"]}
    assert types == {"semantic"}


async def test_list_memories_filter_by_type(db_client, db_agent, db_owner_id) -> None:
    await db_agent._memory.store_memory(
        db_owner_id, memory_type="semantic", content="Boss likes tea."
    )
    await db_agent._memory.store_memory(
        db_owner_id,
        memory_type="relationship",
        content="Rahul is Boss's college project partner.",
    )

    response = await db_client.get("/memories", params={"memory_type": "relationship"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["memories"][0]["memory_type"] == "relationship"


async def test_list_memories_invalid_type_rejected(db_client) -> None:
    response = await db_client.get("/memories", params={"memory_type": "banana"})
    assert response.status_code == 422


async def test_search_memories_endpoint(db_client, db_agent, db_owner_id) -> None:
    await _seed_memory(db_agent, db_owner_id)

    response = await db_client.post(
        "/memories/search",
        json={"query": "meeting preference", "limit": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert "6 PM" in body["results"][0]["memory"]["content"]
    assert 0.0 <= body["results"][0]["similarity"] <= 1.0


async def test_search_memories_empty_query_rejected(db_client) -> None:
    response = await db_client.post("/memories/search", json={"query": ""})
    assert response.status_code == 422


async def test_delete_memory_endpoint(db_client, db_agent, db_owner_id) -> None:
    stored = await _seed_memory(db_agent, db_owner_id, "Boss visited Mumbai.")

    response = await db_client.delete(f"/memories/{stored.id}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "id": str(stored.id)}

    # Second delete -> 404.
    response = await db_client.delete(f"/memories/{stored.id}")
    assert response.status_code == 404


async def test_delete_memory_invalid_uuid(db_client) -> None:
    response = await db_client.delete("/memories/not-a-uuid")
    assert response.status_code == 404


# --- Part 1 endpoints over the DB stack -------------------------------------


async def test_part1_endpoints_over_database(db_client) -> None:
    """Sessions CRUD must behave identically on the persistent store."""
    created = await db_client.post("/sessions")
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    chat = await db_client.post(
        "/chat", json={"session_id": session_id, "message": "Hello"}
    )
    assert chat.status_code == 200

    fetched = await db_client.get(f"/sessions/{session_id}")
    assert fetched.status_code == 200
    assert [m["role"] for m in fetched.json()["messages"]] == ["user", "assistant"]

    cleared = await db_client.delete(f"/sessions/{session_id}")
    assert cleared.status_code == 200
    assert cleared.json()["messages"] == []

    missing = await db_client.get("/sessions/does-not-exist")
    assert missing.status_code == 404
