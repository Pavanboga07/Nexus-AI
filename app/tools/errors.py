"""Tool subsystem errors.

Expected tool failures raise :class:`ToolError` with a stable code; anything
else is an unexpected programming failure and is logged (never leaked to
API clients).
"""

from __future__ import annotations

import enum


class ToolErrorCode(str, enum.Enum):
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INVALID_TOOL_NAME = "INVALID_TOOL_NAME"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    DENIED = "DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    TIMEOUT = "TIMEOUT"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    EXECUTION_ERROR = "EXECUTION_ERROR"


class ToolError(Exception):
    """An expected, structured tool failure (never carries a stack trace)."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["ToolError", "ToolErrorCode"]
