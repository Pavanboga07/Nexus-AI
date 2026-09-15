"""Task routes: delegation, approval, negotiation, and lifecycle (Part 8).

Endpoints:
    POST /a2a/tasks                   — Delegate a task to a remote trusted agent
    GET  /a2a/tasks                   — List owner-scoped tasks
    GET  /a2a/tasks/{task_id}         — Get details of a single task
    POST /a2a/tasks/{task_id}/approve — Manually approve a pending_approval task
    POST /a2a/tasks/{task_id}/reject  — Reject a task
    POST /a2a/tasks/{task_id}/cancel  — Cancel an active task
    POST /a2a/tasks/{task_id}/negotiate — Submit next negotiation round / proposal
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.a2a.errors import A2AError
from app.a2a.service import A2AService
from app.agent.agent import NexusAgent
from app.api.dependencies import get_a2a_service, get_agent
from app.schemas.tasks import (
    TaskActionResponse,
    TaskApproveRequest,
    TaskDelegateRequest,
    TaskListResponse,
    TaskNegotiateRequest,
    TaskOut,
    TaskRejectRequest,
)

logger = logging.getLogger("nexus.api.tasks")

router = APIRouter()


async def _owner_id(agent: NexusAgent) -> uuid.UUID:
    return await agent._owner_id()


@router.post(
    "/a2a/tasks",
    response_model=TaskActionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
    summary="Delegate a task to a remote trusted agent",
    responses={
        400: {"description": "Invalid envelope or unsupported task type"},
        401: {"description": "Recipient trust revoked"},
        404: {"description": "Recipient not found in trusted agents"},
        502: {"description": "Transport error or invalid remote response"},
    },
)
async def delegate_task(
    payload: TaskDelegateRequest,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskActionResponse:
    owner_id = await _owner_id(agent)
    try:
        result = await a2a_service.delegate_task(
            owner_id,
            recipient_agent_id=payload.recipient_agent_id,
            task_type=payload.task_type,
            purpose=payload.purpose,
            payload=payload.payload,
            endpoint=payload.endpoint,
        )
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    return TaskActionResponse(
        task_id=result["task_id"],
        status=result["status"],
        response_payload=result.get("payload"),
    )


@router.get(
    "/a2a/tasks",
    response_model=TaskListResponse,
    tags=["tasks"],
    summary="List owner-scoped tasks",
)
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskListResponse:
    owner_id = await _owner_id(agent)
    tasks = await a2a_service.list_tasks(owner_id, status=status, limit=limit)
    items = [TaskOut(**t.to_dict()) for t in tasks]  # type: ignore[arg-type]
    return TaskListResponse(tasks=items, total=len(items))


@router.get(
    "/a2a/tasks/{task_id}",
    response_model=TaskOut,
    tags=["tasks"],
    summary="Get single task details",
    responses={
        404: {"description": "Task not found"},
    },
)
async def get_task(
    task_id: str,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskOut:
    owner_id = await _owner_id(agent)
    task = await a2a_service.get_task(owner_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    return TaskOut(**task.to_dict())  # type: ignore[arg-type]


@router.post(
    "/a2a/tasks/{task_id}/approve",
    response_model=TaskActionResponse,
    tags=["tasks"],
    summary="Manually approve a task pending approval",
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Task is not pending approval"},
        410: {"description": "Task has expired"},
    },
)
async def approve_task(
    task_id: str,
    payload: TaskApproveRequest | None = None,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskActionResponse:
    owner_id = await _owner_id(agent)
    notes = payload.notes if payload else None
    try:
        task = await a2a_service.approve_task(owner_id, task_id, notes=notes)
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    return TaskActionResponse(
        task_id=task.task_id,
        status=task.status,
        response_payload=task.response_payload,
    )


@router.post(
    "/a2a/tasks/{task_id}/reject",
    response_model=TaskActionResponse,
    tags=["tasks"],
    summary="Reject a task",
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Task is in terminal state"},
    },
)
async def reject_task(
    task_id: str,
    payload: TaskRejectRequest | None = None,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskActionResponse:
    owner_id = await _owner_id(agent)
    reason = payload.reason if payload else "Rejected by owner"
    try:
        task = await a2a_service.reject_task(owner_id, task_id, reason=reason)
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    return TaskActionResponse(
        task_id=task.task_id,
        status=task.status,
        detail=task.failure_reason,
    )


@router.post(
    "/a2a/tasks/{task_id}/cancel",
    response_model=TaskActionResponse,
    tags=["tasks"],
    summary="Cancel an active task",
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Task is in terminal state"},
    },
)
async def cancel_task(
    task_id: str,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskActionResponse:
    owner_id = await _owner_id(agent)
    try:
        task = await a2a_service.cancel_task(owner_id, task_id)
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    return TaskActionResponse(
        task_id=task.task_id,
        status=task.status,
        detail="Task cancelled by owner.",
    )


@router.post(
    "/a2a/tasks/{task_id}/negotiate",
    response_model=TaskActionResponse,
    tags=["tasks"],
    summary="Submit next negotiation round / proposal",
    responses={
        400: {"description": "Maximum negotiation rounds reached or invalid proposal"},
        404: {"description": "Task or remote agent not found"},
        409: {"description": "Task is in terminal state"},
        410: {"description": "Task has expired"},
    },
)
async def negotiate_task(
    task_id: str,
    payload: TaskNegotiateRequest,
    agent: NexusAgent = Depends(get_agent),
    a2a_service: A2AService = Depends(get_a2a_service),
) -> TaskActionResponse:
    owner_id = await _owner_id(agent)
    try:
        result = await a2a_service.negotiate_task(
            owner_id,
            task_id=task_id,
            proposal_payload=payload.proposal_payload,
            purpose=payload.purpose,
        )
    except A2AError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    return TaskActionResponse(
        task_id=result["task_id"],
        status=result["status"],
        detail=f"Negotiation round {result.get('negotiation_round', 1)}",
        response_payload=result.get("payload"),
    )


__all__ = ["router"]
