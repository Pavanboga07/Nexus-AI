"""Tool subsystem unit tests: registry, validation, executor guards.

Pure in-memory tests (no DB, no policy). Policy integration and API tests
live in test_tools_service.py / test_tools_api.py.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.tools.errors import ToolError, ToolErrorCode
from app.tools.executor import run_with_guards, validate_arguments
from app.tools.registry import (
    BaseTool,
    ToolNameError,
    ToolRegistry,
    validate_tool_name,
)
from app.tools.schemas import ToolContext
from app.tools.builtin import BUILTIN_TOOLS, EchoTool, CurrentTimeTool

# --- Tool name validation -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["get_current_time", "calendar_create_event", "send_email", "a", "tool-v2", "x" * 64],
)
def test_valid_tool_names(name: str) -> None:
    assert validate_tool_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "../../shell",
        "execute arbitrary code",
        "DROP TABLE",
        "tool name with spaces",
        "Tool",
        "1tool",
        "tool;rm -rf",
        "tool$()",
        "",
        "tool/name",
        "x" * 65,
    ],
)
def test_invalid_tool_names(name: str) -> None:
    with pytest.raises(ToolNameError):
        validate_tool_name(name)


# --- Registry --------------------------------------------------------------------


def test_register_and_list() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(CurrentTimeTool())

    tools = registry.list_tools()
    names = [t["name"] for t in tools]
    assert names == sorted(names)  # deterministic
    assert names == ["echo", "get_current_time"]

    echo_meta = next(t for t in tools if t["name"] == "echo")
    assert echo_meta["description"]
    schema = echo_meta["inputSchema"]
    assert schema["type"] == "object"
    assert "text" in schema["properties"]
    assert schema["additionalProperties"] is False


def test_duplicate_registration_rejected() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(ToolNameError, match="already registered"):
        registry.register(EchoTool())


def test_unregister_and_has() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    assert registry.has("echo")
    assert registry.unregister("echo") is True
    assert not registry.has("echo")
    assert registry.unregister("echo") is False
    assert registry.get("echo") is None


def test_get_unknown_tool_is_none() -> None:
    assert ToolRegistry().get("nope") is None


def test_metadata_never_exposes_internals() -> None:
    """Metadata must not leak implementation details."""
    registry = ToolRegistry()
    registry.register(EchoTool())
    metadata = registry.list_tools()[0]
    serialized = repr(metadata)
    assert "BaseTool" not in serialized
    assert "args_model" not in serialized
    assert "EchoTool" not in serialized  # class name must not leak


def test_builtin_tools_valid() -> None:
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    assert {t["name"] for t in registry.list_tools()} == {
        "echo",
        "get_current_time",
    }


# --- Argument validation -----------------------------------------------------------


def test_validate_arguments_accepts_valid() -> None:
    tool = EchoTool()
    args = validate_arguments(tool, {"text": "hello"})
    assert args.text == "hello"


def test_validate_arguments_rejects_missing_required() -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_arguments(EchoTool(), {})
    assert excinfo.value.code is ToolErrorCode.INVALID_ARGUMENTS
    assert "text" in excinfo.value.message


def test_validate_arguments_rejects_unknown() -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_arguments(EchoTool(), {"text": "hi", "extra": "nope"})
    assert excinfo.value.code is ToolErrorCode.INVALID_ARGUMENTS


def test_validate_arguments_rejects_wrong_type() -> None:
    with pytest.raises(ToolError):
        validate_arguments(EchoTool(), {"text": 123})


def test_validate_arguments_rejects_oversized() -> None:
    with pytest.raises(ToolError):
        validate_arguments(EchoTool(), {"text": "x" * 1001})


def test_validate_arguments_rejects_non_dict() -> None:
    with pytest.raises(ToolError):
        validate_arguments(EchoTool(), ["text"])


def test_current_time_accepts_empty() -> None:
    args = validate_arguments(CurrentTimeTool(), {})
    assert args is not None


# --- Executor guards ------------------------------------------------------------------


class _SlowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(default=0.5)


class _SlowTool(BaseTool):
    name = "slow_probe"
    description = "sleeps"
    args_model = _SlowArgs

    async def execute(self, arguments, context):
        await asyncio.sleep(arguments.seconds)
        return {"slept": arguments.seconds}


class _BrokenTool(BaseTool):
    name = "broken_probe"
    description = "raises"
    args_model = _SlowArgs

    async def execute(self, arguments, context):
        raise RuntimeError("secret internal detail: db password is hunter2")


class _HugeTool(BaseTool):
    name = "huge_probe"
    description = "big result"
    args_model = _SlowArgs

    async def execute(self, arguments, context):
        return {"blob": "x" * 10_000}


def _context() -> ToolContext:
    return ToolContext(
        owner_id=uuid.uuid4(), request_id="req_test", purpose="testing"
    )


async def test_timeout_guard() -> None:
    tool = _SlowTool()
    args = validate_arguments(tool, {"seconds": 5})
    with pytest.raises(ToolError) as excinfo:
        await run_with_guards(
            tool, args, _context(), timeout_seconds=0.2, max_result_bytes=65536
        )
    assert excinfo.value.code is ToolErrorCode.TIMEOUT


async def test_exception_isolation_hides_internals() -> None:
    tool = _BrokenTool()
    args = validate_arguments(tool, {})
    with pytest.raises(ToolError) as excinfo:
        await run_with_guards(
            tool, args, _context(), timeout_seconds=5, max_result_bytes=65536
        )
    assert excinfo.value.code is ToolErrorCode.EXECUTION_ERROR
    # The exception message must not carry the internal detail.
    assert "hunter2" not in excinfo.value.message
    assert "RuntimeError" not in excinfo.value.message


async def test_result_size_guard_fails_safely() -> None:
    tool = _HugeTool()
    args = validate_arguments(tool, {})
    with pytest.raises(ToolError) as excinfo:
        await run_with_guards(
            tool, args, _context(), timeout_seconds=5, max_result_bytes=1024
        )
    assert excinfo.value.code is ToolErrorCode.RESULT_TOO_LARGE
    assert "exceeds" in excinfo.value.message


async def test_guard_passes_normal_execution() -> None:
    tool = _SlowTool()
    args = validate_arguments(tool, {"seconds": 0.01})
    result = await run_with_guards(
        tool, args, _context(), timeout_seconds=5, max_result_bytes=65536
    )
    assert result == {"slept": 0.01}


async def test_builtin_tools_execute() -> None:
    context = _context()

    time_result = await CurrentTimeTool().execute(
        validate_arguments(CurrentTimeTool(), {}), context
    )
    assert "utc" in time_result
    assert time_result["utc"].endswith("Z")

    echo_result = await EchoTool().execute(
        validate_arguments(EchoTool(), {"text": "Hello Nexus"}), context
    )
    assert echo_result == {"text": "Hello Nexus"}
