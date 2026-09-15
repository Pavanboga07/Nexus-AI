"""Memory routes: list, search, delete - owner-scoped, dev/debug oriented."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.agent.agent import NexusAgent
from app.api.dependencies import get_agent
from app.schemas.memory import (
    MemoryDeleteResponse,
    MemoryListResponse,
    MemoryOut,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySearchResultOut,
)

logger = logging.getLogger("nexus.api.memories")

router = APIRouter()

VALID_MEMORY_TYPES = {"semantic", "episodic", "relationship"}


def _memory_out(memory) -> MemoryOut:
    return MemoryOut(**memory.to_dict())  # type: ignore[arg-type]


async def _owner_id(agent: NexusAgent) -> uuid.UUID:
    return await agent._owner_id()


def _require_memory(agent: NexusAgent):
    if agent._memory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory subsystem is not configured.",
        )
    return agent._memory


@router.get(
    "/memories",
    response_model=MemoryListResponse,
    tags=["memories"],
    summary="List stored memories (owner-scoped)",
)
async def list_memories(
    memory_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    agent: NexusAgent = Depends(get_agent),
) -> MemoryListResponse:
    memory = _require_memory(agent)
    if memory_type is not None and memory_type not in VALID_MEMORY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="memory_type must be semantic, episodic, or relationship",
        )
    owner_id = await _owner_id(agent)
    memories = await memory.list_memories(
        owner_id, memory_type=memory_type, limit=limit
    )
    items = [_memory_out(m) for m in memories]
    return MemoryListResponse(memories=items, total=len(items))


@router.post(
    "/memories/search",
    response_model=MemorySearchResponse,
    tags=["memories"],
    summary="Semantic memory search (owner-scoped)",
)
async def search_memories(
    payload: MemorySearchRequest,
    agent: NexusAgent = Depends(get_agent),
) -> MemorySearchResponse:
    memory = _require_memory(agent)
    owner_id = await _owner_id(agent)
    results = await memory.search_memories(
        owner_id,
        payload.query,
        limit=payload.limit,
        memory_types=payload.memory_types,
    )
    out = [
        MemorySearchResultOut(
            memory=_memory_out(r.memory), similarity=round(r.similarity, 4)
        )
        for r in results
    ]
    return MemorySearchResponse(results=out, total=len(out))


@router.delete(
    "/memories/{memory_id}",
    response_model=MemoryDeleteResponse,
    tags=["memories"],
    summary="Delete a memory",
    responses={404: {"description": "Memory not found"}},
)
async def delete_memory(
    memory_id: str,
    agent: NexusAgent = Depends(get_agent),
) -> MemoryDeleteResponse:
    memory = _require_memory(agent)
    try:
        memory_uuid = uuid.UUID(memory_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found.",
        ) from None
    owner_id = await _owner_id(agent)
    deleted = await memory.delete_memory(owner_id, memory_uuid)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found.",
        )
    return MemoryDeleteResponse(deleted=True, id=memory_id)
