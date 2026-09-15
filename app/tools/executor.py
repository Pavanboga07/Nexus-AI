"""The safe execution boundary around individual tool invocations.

Enforces:
- asyncio timeout (a hung tool cannot hang the agent)
- result-size cap (a tool cannot return unlimited data)
- exception isolation (a broken tool returns a structured error, never a
  crash; unexpected programming errors are logged server-side without
  leaking internals)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from app.tools.errors import ToolError, ToolErrorCode
from app.tools.registry import BaseTool
from app.tools.schemas import ToolContext

logger = logging.getLogger("nexus.tools.executor")


def validate_arguments(tool: BaseTool, raw: Any) -> BaseModel:
    """Validate raw arguments against the tool's declared schema.

    Rejects unknown arguments, missing required fields, wrong types, and
    oversized strings BEFORE any policy or execution happens.
    """
    if not isinstance(raw, dict):
        raise ToolError(
            ToolErrorCode.INVALID_ARGUMENTS,
            "arguments must be a JSON object",
        )
    try:
        return tool.args_model.model_validate(raw)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            field = ".".join(str(p) for p in error["loc"]) or "(root)"
            problems.append(f"{field}: {error['msg']}")
        raise ToolError(
            ToolErrorCode.INVALID_ARGUMENTS,
            "; ".join(problems)[:500],
        ) from exc


async def run_with_guards(
    tool: BaseTool,
    arguments: BaseModel,
    context: ToolContext,
    *,
    timeout_seconds: float,
    max_result_bytes: int,
) -> dict[str, Any]:
    """Execute one tool inside the timeout/size/exception boundary."""
    try:
        raw_result = await asyncio.wait_for(
            tool.execute(arguments, context), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        raise ToolError(
            ToolErrorCode.TIMEOUT,
            f"Tool {tool.name} exceeded the {timeout_seconds:g}s limit.",
        ) from None
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - isolate broken tools
        # Unexpected programming failure: log server-side, return a
        # structured error with NO stack trace or internals.
        logger.error(
            "tool_execution_crash tool=%s request_id=%s error=%s",
            tool.name,
            context.request_id,
            type(exc).__name__,
            exc_info=True,
        )
        raise ToolError(
            ToolErrorCode.EXECUTION_ERROR,
            f"Tool {tool.name} failed unexpectedly.",
        ) from exc

    if not isinstance(raw_result, dict):
        raise ToolError(
            ToolErrorCode.EXECUTION_ERROR,
            f"Tool {tool.name} returned a non-object result.",
        )
    try:
        encoded_size = len(json.dumps(raw_result).encode("utf-8"))
    except (TypeError, ValueError):
        raise ToolError(
            ToolErrorCode.EXECUTION_ERROR,
            f"Tool {tool.name} returned non-serializable data.",
        ) from None
    if encoded_size > max_result_bytes:
        # Fail safely rather than truncate: truncating could corrupt or
        # mislead. Documented in README.
        raise ToolError(
            ToolErrorCode.RESULT_TOO_LARGE,
            f"Tool {tool.name} result ({encoded_size} bytes) exceeds the "
            f"{max_result_bytes}-byte limit.",
        )
    return raw_result


__all__ = ["run_with_guards", "validate_arguments"]
