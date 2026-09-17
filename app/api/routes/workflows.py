"""Workflow API routes (Part 9).

Endpoints:
    POST /workflows                   — Create a new workflow
    GET  /workflows                   — List owner-scoped workflows
    GET  /workflows/{workflow_id}     — Get workflow status and steps
    POST /workflows/{workflow_id}/start   — Start workflow execution
    POST /workflows/{workflow_id}/approve — Approve a step in waiting_approval status
    POST /workflows/{workflow_id}/cancel  — Cancel an active workflow
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.agent.agent import NexusAgent
from app.api.auth_context import request_owner_id
from app.api.dependencies import get_agent, get_workflow_service
from app.schemas.workflows import (
    WorkflowActionResponse,
    WorkflowCreateRequest,
    WorkflowListResponse,
    WorkflowOut,
    WorkflowStepOut,
)
from app.workflows.errors import (
    WorkflowConflictError,
    WorkflowExpiredError,
    WorkflowNotFoundError,
)
from app.workflows.models import Workflow
from app.workflows.service import WorkflowService

logger = logging.getLogger("nexus.api.workflows")

router = APIRouter()


class WorkflowApproveRequest(BaseModel):
    step_id: uuid.UUID | None = None


class WorkflowCancelRequest(BaseModel):
    reason: str = Field(default="Cancelled by owner", max_length=255)


async def _owner_id(request: Request) -> uuid.UUID:
    return request_owner_id(request)


def _to_workflow_out(wf: Workflow) -> WorkflowOut:
    return WorkflowOut(
        workflow_id=str(wf.workflow_id),
        owner_id=str(wf.owner_id),
        workflow_type=wf.workflow_type,
        purpose=wf.purpose,
        status=wf.status,
        current_step_number=wf.current_step_number,
        context_data=wf.context_data or {},
        workflow_metadata=wf.workflow_metadata or {},
        failure_reason=wf.failure_reason,
        expires_at=wf.expires_at.isoformat() if wf.expires_at else None,
        completed_at=wf.completed_at.isoformat() if wf.completed_at else None,
        created_at=wf.created_at.isoformat() if wf.created_at else None,
        updated_at=wf.updated_at.isoformat() if wf.updated_at else None,
        steps=[
            WorkflowStepOut(
                step_id=str(s.step_id),
                workflow_id=str(s.workflow_id),
                step_number=s.step_number,
                step_type=s.step_type,
                status=s.status,
                input_payload=s.input_payload or {},
                output_payload=s.output_payload,
                task_id=s.task_id,
                attempt_count=s.attempt_count,
                max_attempts=s.max_attempts,
                failure_reason=s.failure_reason,
                started_at=s.started_at.isoformat() if s.started_at else None,
                completed_at=s.completed_at.isoformat() if s.completed_at else None,
                created_at=s.created_at.isoformat() if s.created_at else None,
                updated_at=s.updated_at.isoformat() if s.updated_at else None,
            )
            for s in (wf.steps or [])
        ],
    )


@router.post(
    "/workflows",
    response_model=WorkflowOut,
    status_code=status.HTTP_201_CREATED,
    tags=["workflows"],
    summary="Create a new multi-step workflow",
)
async def create_workflow(
    request: Request,
    payload: WorkflowCreateRequest,
    agent: NexusAgent = Depends(get_agent),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowOut:
    owner_id = await _owner_id(request)
    try:
        wf = await workflow_service.create_workflow(
            owner_id,
            workflow_type=payload.workflow_type,
            purpose=payload.purpose,
            steps=payload.steps,
            context_data=payload.context_data,
            ttl_seconds=payload.ttl_seconds,
        )
        return _to_workflow_out(wf)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get(
    "/workflows",
    response_model=WorkflowListResponse,
    tags=["workflows"],
    summary="List workflows for the current owner",
)
async def list_workflows(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    agent: NexusAgent = Depends(get_agent),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowListResponse:
    owner_id = await _owner_id(request)
    workflows = await workflow_service.list_workflows(
        owner_id, status=status_filter, limit=limit
    )
    items = [_to_workflow_out(wf) for wf in workflows]
    return WorkflowListResponse(workflows=items, total=len(items))


@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Get workflow details and current step status",
)
async def get_workflow(
    request: Request,
    workflow_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowOut:
    owner_id = await _owner_id(request)
    try:
        wf = await workflow_service.get_workflow(owner_id, workflow_id)
        return _to_workflow_out(wf)
    except WorkflowNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/workflows/{workflow_id}/start",
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Start execution of a pending workflow",
)
async def start_workflow(
    request: Request,
    workflow_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowOut:
    owner_id = await _owner_id(request)
    try:
        wf = await workflow_service.start_workflow(owner_id, workflow_id)
        return _to_workflow_out(wf)
    except WorkflowNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except WorkflowExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(exc),
        ) from exc
    except WorkflowConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/workflows/{workflow_id}/approve",
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Approve a workflow step awaiting authorization",
)
async def approve_workflow(
    request: Request,
    workflow_id: uuid.UUID,
    payload: WorkflowApproveRequest | None = None,
    agent: NexusAgent = Depends(get_agent),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowOut:
    owner_id = await _owner_id(request)
    step_id = payload.step_id if payload else None
    try:
        wf = await workflow_service.approve_workflow(
            owner_id, workflow_id, step_id=step_id
        )
        return _to_workflow_out(wf)
    except WorkflowNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except WorkflowConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/workflows/{workflow_id}/cancel",
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Cancel an active workflow",
)
async def cancel_workflow(
    request: Request,
    workflow_id: uuid.UUID,
    payload: WorkflowCancelRequest | None = None,
    agent: NexusAgent = Depends(get_agent),
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowOut:
    owner_id = await _owner_id(request)
    reason = payload.reason if payload else "Cancelled by owner"
    try:
        wf = await workflow_service.cancel_workflow(
            owner_id, workflow_id, reason=reason
        )
        return _to_workflow_out(wf)
    except WorkflowNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
