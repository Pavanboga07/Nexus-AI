"""Autonomy routes: configuration, runs, approvals, decisions, and audit (Part 10)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.agent.agent import NexusAgent
from app.api.auth_context import request_owner_id
from app.api.dependencies import get_agent, get_autonomy_service
from app.autonomy.decision_engine import DecisionRequest
from app.autonomy.errors import (
    AutonomyConflictError,
    AutonomyDisabledError,
    AutonomyError,
    AutonomyRunNotFoundError,
)
from app.autonomy.schemas import (
    AutonomyApprovalDecisionRequest,
    AutonomyAuditListResponse,
    AutonomyAuditOut,
    AutonomyConfigOut,
    AutonomyConfigUpdate,
    AutonomyDecisionListResponse,
    AutonomyDecisionOut,
    AutonomyEvaluateRequest,
    AutonomyEvaluateResponse,
    AutonomyRunCreateRequest,
    AutonomyRunListResponse,
    AutonomyRunOut,
)
from app.autonomy.service import AutonomyService

logger = logging.getLogger("nexus.api.autonomy")

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


async def _owner_id(request: Request) -> uuid.UUID:
    return request_owner_id(request)


@router.get(
    "/config",
    response_model=AutonomyConfigOut,
    summary="Get current owner autonomy configuration",
)
async def get_autonomy_config(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyConfigOut:
    owner_id = await _owner_id(request)
    cfg = await autonomy_service.get_config(owner_id)
    return AutonomyConfigOut(**cfg.to_dict())


@router.post(
    "/config",
    response_model=AutonomyConfigOut,
    summary="Update owner autonomy configuration",
)
async def update_autonomy_config(
    request: Request,
    payload: AutonomyConfigUpdate,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyConfigOut:
    owner_id = await _owner_id(request)
    update_data = payload.model_dump(exclude_unset=True)
    if "mode" in update_data and update_data["mode"]:
        update_data["mode"] = update_data["mode"].value
    cfg = await autonomy_service.update_config(owner_id, **update_data)
    return AutonomyConfigOut(**cfg.to_dict())


@router.post(
    "/run",
    response_model=AutonomyRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create and start an autonomous run",
)
async def create_autonomy_run(
    request: Request,
    payload: AutonomyRunCreateRequest,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyRunOut:
    owner_id = await _owner_id(request)
    custom_plan = [p.model_dump() for p in payload.plan] if payload.plan else None
    try:
        run = await autonomy_service.create_run(
            owner_id=owner_id,
            goal=payload.goal,
            context_data=payload.context_data,
            custom_plan=custom_plan,
            execute_immediately=payload.execute_immediately,
        )
    except AutonomyDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except AutonomyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return AutonomyRunOut(**run.to_dict())


@router.get(
    "/runs",
    response_model=AutonomyRunListResponse,
    summary="List owner autonomous runs",
)
async def list_autonomy_runs(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyRunListResponse:
    owner_id = await _owner_id(request)
    runs = await autonomy_service.list_runs(
        owner_id, status=status_filter, limit=limit, offset=offset
    )
    items = [AutonomyRunOut(**r.to_dict()) for r in runs]
    return AutonomyRunListResponse(runs=items, total=len(items))


@router.get(
    "/runs/{run_id}",
    response_model=AutonomyRunOut,
    summary="Get details of an autonomous run",
)
async def get_autonomy_run(
    request: Request,
    run_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyRunOut:
    owner_id = await _owner_id(request)
    try:
        run = await autonomy_service.get_run(owner_id, run_id)
    except AutonomyRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return AutonomyRunOut(**run.to_dict())


@router.post(
    "/runs/{run_id}/approve",
    response_model=AutonomyRunOut,
    summary="Approve a paused run in waiting_approval status",
)
async def approve_autonomy_run(
    request: Request,
    run_id: uuid.UUID,
    payload: AutonomyApprovalDecisionRequest | None = None,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyRunOut:
    owner_id = await _owner_id(request)
    notes = payload.notes if payload else None
    try:
        run = await autonomy_service.approve_run(owner_id, run_id, notes=notes)
    except AutonomyRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AutonomyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return AutonomyRunOut(**run.to_dict())


@router.post(
    "/runs/{run_id}/reject",
    response_model=AutonomyRunOut,
    summary="Reject a paused run in waiting_approval status",
)
async def reject_autonomy_run(
    request: Request,
    run_id: uuid.UUID,
    payload: AutonomyApprovalDecisionRequest | None = None,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyRunOut:
    owner_id = await _owner_id(request)
    notes = payload.notes if payload else None
    try:
        run = await autonomy_service.reject_run(owner_id, run_id, notes=notes)
    except AutonomyRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return AutonomyRunOut(**run.to_dict())


@router.post(
    "/runs/{run_id}/cancel",
    response_model=AutonomyRunOut,
    summary="Cancel an active autonomous run",
)
async def cancel_autonomy_run(
    request: Request,
    run_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyRunOut:
    owner_id = await _owner_id(request)
    try:
        run = await autonomy_service.cancel_run(owner_id, run_id)
    except AutonomyRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return AutonomyRunOut(**run.to_dict())


@router.get(
    "/runs/{run_id}/decisions",
    response_model=AutonomyDecisionListResponse,
    summary="Get all decisions made for a run",
)
async def get_run_decisions(
    request: Request,
    run_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyDecisionListResponse:
    owner_id = await _owner_id(request)
    decisions = await autonomy_service.list_decisions(owner_id, run_id)
    items = [AutonomyDecisionOut(**d.to_dict()) for d in decisions]
    return AutonomyDecisionListResponse(decisions=items, total=len(items))


@router.get(
    "/runs/{run_id}/audit",
    response_model=AutonomyAuditListResponse,
    summary="Get audit trail and trigger history for a run",
)
async def get_run_audit(
    request: Request,
    run_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyAuditListResponse:
    owner_id = await _owner_id(request)
    audits = await autonomy_service.list_audits(owner_id, run_id)
    items = [
        AutonomyAuditOut(
            run_id=a.get("run_id"),
            event=a.get("trigger_type", "event"),
            timestamp=a.get("created_at", ""),
            details=a.get("payload", {}),
        )
        for a in audits
    ]
    return AutonomyAuditListResponse(audits=items, total=len(items))


@router.post(
    "/evaluate",
    response_model=AutonomyEvaluateResponse,
    summary="Evaluate an action against the Decision Engine without executing it",
)
async def evaluate_action(
    request: Request,
    payload: AutonomyEvaluateRequest,
    agent: NexusAgent = Depends(get_agent),
    autonomy_service: AutonomyService = Depends(get_autonomy_service),
) -> AutonomyEvaluateResponse:
    owner_id = await _owner_id(request)
    req = DecisionRequest(
        action_type=payload.action_type,
        proposed_action=payload.proposed_action,
        purpose=payload.purpose,
        goal=payload.goal,
        target_agent_id=payload.target_agent_id,
        tool_name=payload.tool_name,
        required_data_categories=payload.required_data_categories,
        payload=payload.payload,
        trigger=payload.trigger,
    )
    outcome = await autonomy_service.evaluate_action(owner_id, req)
    return AutonomyEvaluateResponse(
        decision=outcome.decision.value,
        reason=outcome.reason,
        risk_level=outcome.risk_level.value if hasattr(outcome.risk_level, "value") else str(outcome.risk_level),
        action_type=outcome.action_type,
        purpose=outcome.purpose,
        policy_decision=outcome.policy_decision,
        consent_decision=outcome.consent_decision,
        requires_approval=outcome.requires_approval,
    )


__all__ = ["router"]
