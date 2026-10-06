"""Tool routes: discovery, metadata, and policy-gated execution."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.agent.agent import NexusAgent
from app.api.auth_context import request_owner_id
from app.api.dependencies import get_agent, get_tool_service
from app.schemas.tools import (
    ToolAuditEntryOut,
    ToolAuditResponse,
    ToolExecuteRequest,
    ToolExecuteResponse,
    ToolListResponse,
    ToolMetadataOut,
)
from app.tools.schemas import ToolInvocation
from app.tools.service import ToolService

logger = logging.getLogger("nexus.api.tools")

router = APIRouter()


async def _owner_id(request: Request) -> uuid.UUID:
    """The ACTING owner for this request (authenticated principal).

    Multi-tenancy: the owner comes from the resolved RequestContext, never from
    a process-wide cache. Raising here (rather than defaulting) surfaces a
    route mounted without the auth dependency instead of silently operating on
    the wrong tenant's data.
    """
    return request_owner_id(request)


@router.get(
    "/tools",
    response_model=ToolListResponse,
    tags=["tools"],
    summary="List registered tools (MCP-style metadata)",
)
async def list_tools(
    tool_service: ToolService = Depends(get_tool_service),
) -> ToolListResponse:
    tools = [
        ToolMetadataOut(**meta) for meta in tool_service.list_tools()
    ]
    return ToolListResponse(tools=tools, total=len(tools))


@router.get(
    "/tools/{tool_name}",
    response_model=ToolMetadataOut,
    tags=["tools"],
    summary="Get one tool's metadata",
    responses={404: {"description": "Tool not found"}},
)
async def get_tool(
    tool_name: str,
    tool_service: ToolService = Depends(get_tool_service),
) -> ToolMetadataOut:
    metadata = tool_service.get_tool(tool_name)
    if metadata is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tool {tool_name!r} not found.",
        )
    return ToolMetadataOut(**metadata)


@router.post(
    "/tools/execute",
    response_model=ToolExecuteResponse,
    tags=["tools"],
    summary="Request a tool execution (policy-gated)",
    description=(
        "The tool only executes when the Part 4 policy engine returns "
        "ALLOW. ASK yields status=approval_required; DENY yields "
        "status=denied. Nothing executes without authorization."
    ),
)
async def execute_tool(
    request: Request,
    payload: ToolExecuteRequest,
    agent: NexusAgent = Depends(get_agent),
    tool_service: ToolService = Depends(get_tool_service),
) -> ToolExecuteResponse:
    owner_id = await _owner_id(request)
    result = await tool_service.execute(
        owner_id,
        ToolInvocation(
            tool_name=payload.tool_name,
            arguments=payload.arguments,
            purpose=payload.purpose,
            request_id=payload.request_id,
        ),
    )
    return ToolExecuteResponse(**result.to_dict())


@router.get(
    "/tools/audit/list",
    response_model=ToolAuditResponse,
    tags=["tools"],
    summary="Tool execution audit trail (owner-scoped)",
)
async def list_tool_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    agent: NexusAgent = Depends(get_agent),
    tool_service: ToolService = Depends(get_tool_service),
) -> ToolAuditResponse:
    owner_id = await _owner_id(request)
    records = await tool_service.list_audit(owner_id, limit=limit)
    entries = [ToolAuditEntryOut(**r.to_dict()) for r in records]
    return ToolAuditResponse(executions=entries, total=len(entries))


__all__ = ["router"]
