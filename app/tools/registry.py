"""Tool registry: registration, discovery, and metadata.

The registry is application-scoped (created once at startup, never per
request). It exposes MCP-style metadata only - never tool implementations or
internals. There is deliberately NO method that executes a tool: execution
lives in ToolService, which always passes through the policy engine.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from app.tools.schemas import ToolContext

#: Strict tool-name boundary: lowercase letter first, then lowercase
#: letters/digits/underscore/hyphen, max 64 chars. Blocks path traversal,
#: shell metacharacters, SQL, and spaces.
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ToolNameError(ValueError):
    """Raised for malformed tool names or duplicate registrations."""


def validate_tool_name(name: str) -> str:
    if not isinstance(name, str) or not TOOL_NAME_PATTERN.fullmatch(name):
        raise ToolNameError(
            f"Invalid tool name {name!r}: must be lowercase letters, digits, "
            "underscore or hyphen, start with a letter, max 64 chars."
        )
    return name


def _strip_titles(schema: Any) -> Any:
    """Remove Pydantic's auto-generated ``title`` fields - they leak the
    implementation class name and MCP schemas don't need them."""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(item) for item in schema]
    return schema


class BaseTool(ABC):
    """The tool contract: metadata + a validated-argument executor.

    Subclasses declare ``args_model`` (a Pydantic model with
    ``extra="forbid"``) - it IS the input schema, so metadata and validation
    can never drift apart. ``data_category`` maps the tool onto the Part 4
    policy vocabulary (action is always ``access_tool``).
    """

    name: ClassVar[str]
    description: ClassVar[str]
    data_category: ClassVar[str] = "custom"
    args_model: ClassVar[type[BaseModel]]

    @property
    def input_schema(self) -> dict[str, Any]:
        """MCP-style inputSchema derived from the args model."""
        return _strip_titles(self.args_model.model_json_schema())

    def metadata(self) -> dict[str, Any]:
        """Public metadata - nothing else may leave the registry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    @abstractmethod
    async def execute(
        self, arguments: BaseModel, context: ToolContext
    ) -> dict[str, Any]:
        """Execute with ALREADY-VALIDATED arguments. Return a JSON-able dict."""
        raise NotImplementedError


class ToolRegistry:
    """Application-scoped tool catalogue. Deterministic: listing is sorted
    by name so registration order cannot change observable output."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if not isinstance(tool, BaseTool):
            raise ToolNameError(
                "Only BaseTool implementations can be registered."
            )
        validate_tool_name(tool.name)
        if tool.name in self._tools:
            raise ToolNameError(
                f"Tool {tool.name!r} is already registered."
            )
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[dict[str, Any]]:
        """MCP-style metadata for every registered tool, sorted by name."""
        return [
            self._tools[name].metadata() for name in sorted(self._tools)
        ]


__all__ = [
    "BaseTool",
    "TOOL_NAME_PATTERN",
    "ToolNameError",
    "ToolRegistry",
    "validate_tool_name",
]
