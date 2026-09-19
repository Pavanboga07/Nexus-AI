"""Tools API tests: GET /tools, GET /tools/{name}, POST /tools/execute."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _execute_payload(**overrides) -> dict:
    payload = {
        "tool_name": "echo",
        "arguments": {"text": "Hello Nexus"},
        "request_id": "req_api_1",
        "purpose": "testing",
    }
    payload.update(overrides)
    return payload


async def _allow_tools(client) -> None:
    response = await client.post(
        "/policy",
        json={
            "requester_agent_id": "nexus:self",
            "data_category": "custom",
            "action": "access_tool",
            "purpose": "testing",
            "decision": "ALLOW",
        },
    )
    assert response.status_code == 201, response.text


# --- Discovery -------------------------------------------------------------------


async def test_list_tools(db_tools_client) -> None:
    response = await db_tools_client.get("/tools")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    names = {t["name"] for t in body["tools"]}
    assert names == {"echo", "get_current_time", "web_fetch", "web_search"}
    # MCP-style metadata, no implementation details.
    for tool in body["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["inputSchema"]["type"] == "object"


async def test_get_tool_metadata(db_tools_client) -> None:
    response = await db_tools_client.get("/tools/echo")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "echo"
    assert "text" in body["inputSchema"]["properties"]
    assert body["inputSchema"]["additionalProperties"] is False


async def test_get_unknown_tool_404(db_tools_client) -> None:
    response = await db_tools_client.get("/tools/nonexistent")
    assert response.status_code == 404


async def test_metadata_never_exposes_secrets(db_tools_client) -> None:
    """GET /tools must never leak secrets or environment material."""
    import os

    responses = [
        await db_tools_client.get("/tools"),
        await db_tools_client.get("/tools/echo"),
        await db_tools_client.get("/tools/get_current_time"),
    ]
    blob = " ".join(r.text for r in responses)
    assert "NEXUS_IDENTITY_KEY" not in blob
    assert "gsk_" not in blob
    assert "BEGIN PRIVATE KEY" not in blob
    assert "postgresql" not in blob
    identity_key = os.environ.get("NEXUS_IDENTITY_KEY", "")
    if identity_key:
        assert identity_key not in blob


# --- Execution ----------------------------------------------------------------------


async def test_execute_without_policy_asks(db_tools_client) -> None:
    response = await db_tools_client.post(
        "/tools/execute", json=_execute_payload()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["status"] == "approval_required"
    assert body["data"] is None
    assert body["request_id"] == "req_api_1"


async def test_execute_with_policy_succeeds(db_tools_client) -> None:
    await _allow_tools(db_tools_client)
    response = await db_tools_client.post(
        "/tools/execute", json=_execute_payload()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "executed"
    assert body["data"] == {"text": "Hello Nexus"}


async def test_execute_with_deny_blocked(db_tools_client) -> None:
    await db_tools_client.post(
        "/policy",
        json={
            "requester_agent_id": "nexus:self",
            "data_category": "custom",
            "action": "access_tool",
            "purpose": "testing",
            "decision": "DENY",
            "priority": 10,
        },
    )
    response = await db_tools_client.post(
        "/tools/execute", json=_execute_payload()
    )
    body = response.json()
    assert body["status"] == "denied"
    assert body["success"] is False
    assert body["data"] is None


async def test_execute_time_tool(db_tools_client) -> None:
    await _allow_tools(db_tools_client)
    response = await db_tools_client.post(
        "/tools/execute",
        json={
            "tool_name": "get_current_time",
            "arguments": {},
            "purpose": "testing",
            "request_id": "req_time_1",
        },
    )
    body = response.json()
    assert body["success"] is True
    assert body["data"]["utc"].endswith("Z")


async def test_execute_unknown_tool(db_tools_client) -> None:
    response = await db_tools_client.post(
        "/tools/execute", json=_execute_payload(tool_name="no_such_tool")
    )
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "UNKNOWN_TOOL"


async def test_execute_malformed_tool_name_rejected(db_tools_client) -> None:
    response = await db_tools_client.post(
        "/tools/execute", json=_execute_payload(tool_name="../../shell")
    )
    assert response.status_code == 422


async def test_execute_missing_purpose_rejected(db_tools_client) -> None:
    payload = _execute_payload()
    del payload["purpose"]
    response = await db_tools_client.post("/tools/execute", json=payload)
    assert response.status_code == 422


async def test_execute_invalid_arguments(db_tools_client) -> None:
    await _allow_tools(db_tools_client)
    response = await db_tools_client.post(
        "/tools/execute",
        json=_execute_payload(arguments={"text": "x" * 2000}),
    )
    body = response.json()
    assert body["error"]["code"] == "INVALID_ARGUMENTS"


async def test_execute_unknown_arguments(db_tools_client) -> None:
    await _allow_tools(db_tools_client)
    response = await db_tools_client.post(
        "/tools/execute",
        json=_execute_payload(arguments={"text": "hi", "extra": 1}),
    )
    body = response.json()
    assert body["error"]["code"] == "INVALID_ARGUMENTS"


async def test_execute_purpose_mismatch(db_tools_client) -> None:
    await _allow_tools(db_tools_client)
    response = await db_tools_client.post(
        "/tools/execute", json=_execute_payload(purpose="marketing")
    )
    assert response.json()["status"] == "approval_required"


# --- Audit ------------------------------------------------------------------------------


async def test_tool_audit_endpoint(db_tools_client) -> None:
    await db_tools_client.post("/tools/execute", json=_execute_payload(request_id="req_x1"))
    await _allow_tools(db_tools_client)
    await db_tools_client.post("/tools/execute", json=_execute_payload(request_id="req_x2"))

    response = await db_tools_client.get("/tools/audit/list")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    by_request = {e["request_id"]: e for e in body["executions"]}
    assert by_request["req_x1"]["status"] == "approval_required"
    assert by_request["req_x2"]["status"] == "executed"
    # No arguments/output in audit.
    assert "Hello Nexus" not in response.text


async def test_tools_service_unavailable_503(db_client) -> None:
    """Plain db_client has no tool service attached."""
    response = await db_client.get("/tools")
    assert response.status_code == 503
