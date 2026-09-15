"""Built-in demonstration tools: safe, local, side-effect free."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import BaseTool
from app.tools.schemas import ToolContext


class _StrictModel(BaseModel):
    """Arguments model base: rejects unknown fields (additionalProperties:
    false in the derived JSON schema)."""

    model_config = ConfigDict(extra="forbid")


# --- get_current_time -----------------------------------------------------------


class CurrentTimeArgs(_StrictModel):
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
        now = datetime.now(timezone.utc)
        return {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# --- echo ------------------------------------------------------------------------


class EchoArgs(_StrictModel):
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


BUILTIN_TOOLS = (CurrentTimeTool(), EchoTool())

__all__ = ["BUILTIN_TOOLS", "CurrentTimeTool", "EchoTool"]
