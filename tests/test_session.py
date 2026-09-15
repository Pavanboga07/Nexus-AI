"""Tests for session lifecycle: create, get, add message, clear, delete."""

from __future__ import annotations

import httpx
import pytest

from app.agent.session import InMemorySessionStore, SessionNotFoundError


# --- Store-level unit tests -------------------------------------------------


async def test_store_create_returns_unique_ids() -> None:
    store = InMemorySessionStore()
    first = await store.create_session()
    second = await store.create_session()

    assert first.session_id != second.session_id
    assert first.messages == []


async def test_store_add_and_get_message() -> None:
    store = InMemorySessionStore()
    session = await store.create_session()

    await store.add_message(session.session_id, "user", "Hello")
    await store.add_message(session.session_id, "assistant", "Hi there")

    fetched = await store.get_session(session.session_id)
    assert [m["role"] for m in fetched.messages] == ["user", "assistant"]
    assert fetched.messages[0]["content"] == "Hello"


async def test_store_clear_keeps_session_valid() -> None:
    store = InMemorySessionStore()
    session = await store.create_session()
    await store.add_message(session.session_id, "user", "Hello")

    cleared = await store.clear_session(session.session_id)

    assert cleared.messages == []
    # Session id still resolves after clearing.
    assert (await store.get_session(session.session_id)).messages == []


async def test_store_delete_removes_session() -> None:
    store = InMemorySessionStore()
    session = await store.create_session()

    await store.delete_session(session.session_id)

    with pytest.raises(SessionNotFoundError):
        await store.get_session(session.session_id)


async def test_store_unknown_session_raises() -> None:
    store = InMemorySessionStore()
    with pytest.raises(SessionNotFoundError):
        await store.get_session("does-not-exist")


async def test_store_trims_oldest_messages() -> None:
    store = InMemorySessionStore(max_messages=3)
    session = await store.create_session()
    for i in range(5):
        await store.add_message(session.session_id, "user", f"m{i}")

    fetched = await store.get_session(session.session_id)
    assert [m["content"] for m in fetched.messages] == ["m2", "m3", "m4"]


async def test_store_rejects_invalid_role() -> None:
    store = InMemorySessionStore()
    session = await store.create_session()
    with pytest.raises(ValueError):
        await store.add_message(session.session_id, "wizard", "hi")


# --- HTTP-level tests -------------------------------------------------------


async def test_create_session_endpoint(client: httpx.AsyncClient) -> None:
    response = await client.post("/sessions")

    assert response.status_code == 201
    assert response.json()["session_id"]


async def test_get_session_endpoint(client: httpx.AsyncClient) -> None:
    session_id = (await client.post("/sessions")).json()["session_id"]

    response = await client.get(f"/sessions/{session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["messages"] == []


async def test_get_unknown_session_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/sessions/nope")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_clear_session_endpoint(client: httpx.AsyncClient) -> None:
    session_id = (await client.post("/sessions")).json()["session_id"]
    await client.post(
        "/chat", json={"session_id": session_id, "message": "Hello"}
    )

    response = await client.delete(f"/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["messages"] == []


async def test_clear_unknown_session_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.delete("/sessions/nope")
    assert response.status_code == 404
