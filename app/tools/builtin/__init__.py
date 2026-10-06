"""Built-in demonstration tools: safe, local, side-effect free."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from app.tools.builtin_search import WebFetchTool, WebSearchTool
from app.tools.registry import BaseTool
from app.tools.schemas import StrictModel, ToolContext

# --- get_current_time -----------------------------------------------------------


class CurrentTimeArgs(StrictModel):
    # No arguments - the empty strict model renders as
    # {"properties": {}, "additionalProperties": false}
    pass


class CurrentTimeTool(BaseTool):
    name = "get_current_time"
    description = "Returns the current UTC time in ISO-8601 format."
    data_category = "custom"
    args_model = CurrentTimeArgs

    async def execute(
        self, arguments: CurrentTimeArgs, context: ToolContext
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        return {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# --- echo ------------------------------------------------------------------------


class EchoArgs(StrictModel):
    text: str = Field(min_length=1, max_length=1000)


class EchoTool(BaseTool):
    name = "echo"
    description = "Returns the supplied text. Test tool for the tool subsystem."
    data_category = "custom"
    args_model = EchoArgs

    async def execute(
        self, arguments: EchoArgs, context: ToolContext
    ) -> dict[str, str]:
        return {"text": arguments.text}


BUILTIN_TOOLS = (
    CurrentTimeTool(),
    EchoTool(),
    WebSearchTool(),
    WebFetchTool(),
)

__all__ = [
    "BUILTIN_TOOLS",
    "CurrentTimeTool",
    "EchoTool",
    "WebFetchTool",
    "WebSearchTool",
]
