"""Tests for the chat endpoint and agent orchestration.

The LLM is always mocked via ``FakeProvider`` - no real API calls are made.
"""

from __future__ import annotations

import httpx
import pytest

from app.llm.base import (
    LLMConfigurationError,
    LLMProviderError,
    LLMTimeoutError,
)
from tests.conftest import FakeProvider


async def _new_session(client: httpx.AsyncClient) -> str:
    return (await client.post("/sessions")).json()["session_id"]


async def test_chat_returns_assistant_reply(
    client: httpx.AsyncClient, fake_provider: FakeProvider
) -> None:
    session_id = await _new_session(client)

    response = await client.post(
        "/chat", json={"session_id": session_id, "message": "Hello Nexus"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["response"] == fake_provider.reply


async def test_chat_maintains_conversation_context(
    client: httpx.AsyncClient, fake_provider: FakeProvider
) -> None:
    """The second turn must include the first exchange in the LLM context."""
    session_id = await _new_session(client)

    await client.post("/chat", json={"session_id": session_id, "message": "Hi, my name is Boss."})
    await client.post("/chat", json={"session_id": session_id, "message": "What is my name?"})

    # The provider saw two calls; the second carried the full history.
    assert len(fake_provider.calls) == 2
    second_call = fake_provider.calls[1]
    roles = [m["role"] for m in second_call]
    assert roles == ["system", "user", "assistant", "user"]
    assert second_call[1]["content"] == "Hi, my name is Boss."
    assert second_call[3]["content"] == "What is my name?"


async def test_chat_persists_both_turns(client: httpx.AsyncClient) -> None:
    session_id = await _new_session(client)
    await client.post("/chat", json={"session_id": session_id, "message": "Hello"})

    messages = (await client.get(f"/sessions/{session_id}")).json()["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant"]


async def test_chat_unknown_session_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/chat", json={"session_id": "missing", "message": "Hello"}
    )
    assert response.status_code == 404


async def test_chat_empty_message_returns_422(client: httpx.AsyncClient) -> None:
    session_id = await _new_session(client)

    response = await client.post(
        "/chat", json={"session_id": session_id, "message": "   "}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_chat_missing_field_returns_422(client: httpx.AsyncClient) -> None:
    response = await client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (LLMConfigurationError("no key"), 503),
        (LLMTimeoutError("timed out"), 504),
        (LLMProviderError("provider blew up"), 502),
    ],
)
async def test_chat_maps_provider_errors(
    client: httpx.AsyncClient,
    fake_provider: FakeProvider,
    error: Exception,
    expected_status: int,
) -> None:
    session_id = await _new_session(client)
    fake_provider.error = error

    response = await client.post(
        "/chat", json={"session_id": session_id, "message": "Hello"}
    )

    assert response.status_code == expected_status
    # The raw exception message is surfaced as a safe detail, no traceback.
    assert "Traceback" not in response.text


async def test_chat_keeps_user_message_when_provider_fails(
    client: httpx.AsyncClient, fake_provider: FakeProvider
) -> None:
    """A failed turn must not lose the user's message."""
    session_id = await _new_session(client)
    fake_provider.error = LLMProviderError("boom")

    await client.post("/chat", json={"session_id": session_id, "message": "Remember me"})

    messages = (await client.get(f"/sessions/{session_id}")).json()["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == "Remember me"


async def test_list_sessions(client: httpx.AsyncClient) -> None:
    """GET /sessions returns existing session IDs."""
    sid1 = await _new_session(client)
    sid2 = await _new_session(client)
    response = await client.get("/sessions")
    assert response.status_code == 200
    session_ids = response.json()
    assert isinstance(session_ids, list)
    assert sid1 in session_ids
    assert sid2 in session_ids
