"""ToolService + tools API integration tests (PostgreSQL required).

Proves the Part 5 security invariant: no execution without policy ALLOW.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.policy.engine import EvaluationRequest
from app.policy.service import PolicyService
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolInvocation
from app.tools.service import ToolService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        reg.register(tool)
    return reg


@pytest_asyncio.fixture
async def policy_service(db_session_factory) -> PolicyService:
    return PolicyService(session_factory=db_session_factory)


@pytest_asyncio.fixture
async def tool_service(db_session_factory, registry, policy_service) -> ToolService:
    return ToolService(
        registry=registry,
        policy_service=policy_service,
        session_factory=db_session_factory,
        timeout_seconds=2.0,
        max_result_bytes=4096,
    )


async def _allow_tools(policy: PolicyService, owner_id: uuid.UUID, purpose="testing"):
    await policy.create_policy(
        owner_id,
        requester_agent_id="nexus:self",
        data_category="custom",
        action="access_tool",
        purpose=purpose,
        decision="ALLOW",
    )


def _echo_invocation(**overrides) -> ToolInvocation:
    payload = {
        "tool_name": "echo",
        "arguments": {"text": "Hello Nexus"},
        "purpose": "testing",
        "request_id": "req_test_1",
    }
    payload.update(overrides)
    return ToolInvocation(**payload)


# --- The security invariant -------------------------------------------------------


async def test_no_policy_means_ask_not_executed(tool_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    result = await tool_service.execute(owner_a, _echo_invocation())
    assert result.success is False
    assert result.status == "approval_required"
    assert result.data is None  # nothing executed


async def test_allow_executes(tool_service, policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await _allow_tools(policy_service, owner_a)
    result = await tool_service.execute(owner_a, _echo_invocation())
    assert result.success is True
    assert result.status == "executed"
    assert result.data == {"text": "Hello Nexus"}
    assert result.request_id == "req_test_1"


async def test_denied_never_executes(tool_service, policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await policy_service.create_policy(
        owner_a,
        requester_agent_id="nexus:self",
        data_category="custom",
        action="access_tool",
        purpose="testing",
        decision="DENY",
        priority=10,
    )
    result = await tool_service.execute(owner_a, _echo_invocation())
    assert result.status == "denied"
    assert result.data is None


async def test_purpose_mismatch_not_allowed(tool_service, policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await _allow_tools(policy_service, owner_a, purpose="testing")
    result = await tool_service.execute(
        owner_a, _echo_invocation(purpose="marketing")
    )
    assert result.status == "approval_required"


async def test_expired_policy_not_allowed(tool_service, policy_service, owner_ids) -> None:
    from datetime import datetime, timedelta, timezone

    owner_a, _ = owner_ids
    await policy_service.create_policy(
        owner_a,
        requester_agent_id="nexus:self",
        data_category="custom",
        action="access_tool",
        purpose="testing",
        decision="ALLOW",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    result = await tool_service.execute(owner_a, _echo_invocation())
    assert result.status == "approval_required"


async def test_consent_enables_execution(tool_service, policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await policy_service.create_consent(
        owner_a,
        requester_agent_id="nexus:self",
        data_category="custom",
        action="access_tool",
        purpose="testing",
        decision="ALLOW",
    )
    result = await tool_service.execute(owner_a, _echo_invocation())
    assert result.status == "executed"


# --- Execution outcomes --------------------------------------------------------------


async def test_unknown_tool(tool_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    result = await tool_service.execute(
        owner_a, _echo_invocation(tool_name="nonexistent_tool")
    )
    assert result.success is False
    assert result.error.code == "UNKNOWN_TOOL"


async def test_invalid_tool_name(tool_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    result = await tool_service.execute(
        owner_a, _echo_invocation(tool_name="../../shell")
    )
    assert result.error.code == "INVALID_TOOL_NAME"


async def test_invalid_arguments(tool_service, policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await _allow_tools(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a, _echo_invocation(arguments={"text": "x" * 2000})
    )
    assert result.success is False
    assert result.error.code == "INVALID_ARGUMENTS"


async def test_timeout_via_service(tool_service, policy_service, owner_ids, registry) -> None:
    import asyncio

    from pydantic import BaseModel, ConfigDict, Field

    from app.tools.registry import BaseTool

    class SlowArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        seconds: float = Field(default=5)

    class SlowTool(BaseTool):
        name = "slow_tool"
        description = "deliberately slow"
        data_category = "custom"
        args_model = SlowArgs

        async def execute(self, arguments, context):
            await asyncio.sleep(arguments.seconds)
            return {"done": True}

    registry.register(SlowTool())
    owner_a, _ = owner_ids
    await _allow_tools(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="slow_tool",
            arguments={"seconds": 30},
            purpose="testing",
        ),
    )
    assert result.success is False
    assert result.error.code == "TIMEOUT"


async def test_oversized_result_via_service(
    tool_service, policy_service, owner_ids, registry
) -> None:
    from pydantic import BaseModel, ConfigDict

    from app.tools.registry import BaseTool

    class NoArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class HugeTool(BaseTool):
        name = "huge_tool"
        description = "returns too much"
        data_category = "custom"
        args_model = NoArgs

        async def execute(self, arguments, context):
            return {"blob": "x" * 100_000}

    registry.register(HugeTool())
    owner_a, _ = owner_ids
    await _allow_tools(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(
            tool_name="huge_tool", arguments={}, purpose="testing"
        ),
    )
    assert result.success is False
    assert result.error.code == "RESULT_TOO_LARGE"


async def test_broken_tool_isolated(tool_service, policy_service, owner_ids, registry) -> None:
    from pydantic import BaseModel, ConfigDict

    from app.tools.registry import BaseTool

    class NoArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class BrokenTool(BaseTool):
        name = "broken_tool"
        description = "always raises"
        data_category = "custom"
        args_model = NoArgs

        async def execute(self, arguments, context):
            raise RuntimeError("boom: internal secret=xyz")

    registry.register(BrokenTool())
    owner_a, _ = owner_ids
    await _allow_tools(policy_service, owner_a)
    result = await tool_service.execute(
        owner_a,
        ToolInvocation(tool_name="broken_tool", arguments={}, purpose="testing"),
    )
    assert result.success is False
    assert result.error.code == "EXECUTION_ERROR"
    assert "xyz" not in result.error.message  # no internals leaked


# --- Owner isolation + audit -----------------------------------------------------------


async def test_audit_records_all_outcomes(tool_service, policy_service, owner_ids) -> None:
    owner_a, owner_b = owner_ids

    # A: approval_required (no policy)
    await tool_service.execute(owner_a, _echo_invocation(request_id="req_a1"))
    # A: executed (with policy)
    await _allow_tools(policy_service, owner_a)
    await tool_service.execute(owner_a, _echo_invocation(request_id="req_a2"))
    # A: denied (explicit DENY, higher priority)
    await policy_service.create_policy(
        owner_a,
        requester_agent_id="nexus:self",
        data_category="custom",
        action="access_tool",
        purpose="testing",
        decision="DENY",
        priority=10,
    )
    await tool_service.execute(owner_a, _echo_invocation(request_id="req_a3"))
    # B: never touched
    audit_a = await tool_service.list_audit(owner_a)
    audit_b = await tool_service.list_audit(owner_b)

    assert len(audit_a) == 3
    assert len(audit_b) == 0  # owner-scoped audit

    statuses = {r.request_id: r.status for r in audit_a}
    assert statuses["req_a1"] == "approval_required"
    assert statuses["req_a2"] == "executed"
    assert statuses["req_a3"] == "denied"

    executed = next(r for r in audit_a if r.request_id == "req_a2")
    assert executed.policy_decision == "ALLOW"
    assert executed.completed_at is not None

    # Audit rows never store arguments or output.
    import json

    for record in audit_a:
        serialized = json.dumps(record.to_dict())
        assert "Hello Nexus" not in serialized  # arguments
        assert "text" not in serialized  # output field


async def test_owner_a_cannot_execute_as_b(
    tool_service, policy_service, owner_ids
) -> None:
    """A's ALLOW policy must not let B's invocations execute: the execution
    context is the CALLER's owner_id, never an argument."""
    owner_a, owner_b = owner_ids
    await _allow_tools(policy_service, owner_a)

    result_b = await tool_service.execute(owner_b, _echo_invocation())
    assert result_b.status == "approval_required"

    result_a = await tool_service.execute(owner_a, _echo_invocation())
    assert result_a.status == "executed"
