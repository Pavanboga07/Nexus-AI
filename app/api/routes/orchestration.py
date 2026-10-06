"""REST API endpoints for Natural Language Agent Orchestration (Part 12)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.agent.agent import NexusAgent
from app.api.auth_context import request_owner_id
from app.api.dependencies import get_agent, get_orchestrator
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.repository import ContactRepository, OrchestrationRunRepository
from app.orchestration.schemas import (
    ContactCreateRequest,
    ContactResponse,
    OrchestrationApproveRequest,
    OrchestrationCancelRequest,
    OrchestrationExecuteRequest,
    OrchestrationExecuteResponse,
    OrchestrationRejectRequest,
    OrchestrationRunResponse,
    OrchestrationTrustRequest,
)

logger = logging.getLogger("nexus.api.orchestration")

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


async def _owner_id(request: Request) -> uuid.UUID:
    return request_owner_id(request)


@router.post(
    "/execute",
    response_model=OrchestrationExecuteResponse,
    summary="Execute a natural language agent orchestration request",
)
async def execute_orchestration(
    request: Request,
    payload: OrchestrationExecuteRequest,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationExecuteResponse:
    owner_id = await _owner_id(request)
    session_id = payload.session_id or f"orch_{uuid.uuid4().hex[:12]}"
    result = await orchestrator.handle_user_message(
        owner_id=owner_id,
        session_id=session_id,
        message=payload.message,
    )
    if result is None:
        # User message was not an agent orchestration request
        return OrchestrationExecuteResponse(
            run_id="",
            status="chat",
            message="This appears to be a general conversation request.",
            intent_type="GENERAL_CHAT",
        )
    return result


@router.get(
    "/runs",
    response_model=list[OrchestrationRunResponse],
    summary="List recent orchestration runs for the owner",
)
async def list_runs(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> list[OrchestrationRunResponse]:
    owner_id = await _owner_id(request)
    repo = OrchestrationRunRepository()
    async with orchestrator.session_factory() as session:
        runs = await repo.list_runs(session, owner_id)
    return [
        OrchestrationRunResponse(
            run_id=str(r.id),
            session_id=r.session_id,
            goal=r.goal,
            intent_type=r.intent_type,
            state=r.state,
            target_person=r.target_person,
            target_agent_id=r.target_agent_id,
            requires_approval=r.requires_approval,
            approval_prompt=r.approval_prompt,
            result=r.result,
            error=r.error,
            created_at=r.created_at.isoformat(),
        )
        for r in runs
    ]


@router.get(
    "/runs/{run_id}",
    response_model=OrchestrationRunResponse,
    summary="Get details of a specific orchestration run",
)
async def get_run(
    request: Request,
    run_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationRunResponse:
    owner_id = await _owner_id(request)
    repo = OrchestrationRunRepository()
    async with orchestrator.session_factory() as session:
        r = await repo.get_by_id(session, owner_id, run_id)
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Orchestration run not found."
        )
    return OrchestrationRunResponse(
        run_id=str(r.id),
        session_id=r.session_id,
        goal=r.goal,
        intent_type=r.intent_type,
        state=r.state,
        target_person=r.target_person,
        target_agent_id=r.target_agent_id,
        task_id=r.task_id,
        workflow_id=str(r.workflow_id) if r.workflow_id else None,
        requires_approval=r.requires_approval,
        approval_prompt=r.approval_prompt,
        approval_reason=r.approval_reason,
        requested_action=r.requested_action,
        approval_target=r.approval_target,
        approval_category=r.approval_category,
        approval_purpose=r.approval_purpose,
        owner_decision=r.owner_decision,
        result=r.result,
        error=r.error,
        created_at=r.created_at.isoformat(),
    )


@router.post(
    "/runs/{run_id}/approve",
    response_model=OrchestrationExecuteResponse,
    summary="Approve a pending action in an orchestration run and resume the same run",
)
async def approve_run_endpoint(
    request: Request,
    run_id: uuid.UUID,
    payload: OrchestrationApproveRequest = OrchestrationApproveRequest(),
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationExecuteResponse:
    owner_id = await _owner_id(request)
    return await orchestrator.approve_run(owner_id, run_id, payload)


@router.post(
    "/runs/{run_id}/reject",
    response_model=OrchestrationExecuteResponse,
    summary="Reject a pending action in an orchestration run",
)
async def reject_run_endpoint(
    request: Request,
    run_id: uuid.UUID,
    payload: OrchestrationRejectRequest = OrchestrationRejectRequest(),
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationExecuteResponse:
    owner_id = await _owner_id(request)
    return await orchestrator.reject_run(owner_id, run_id, payload)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=OrchestrationExecuteResponse,
    summary="Cancel an active or waiting orchestration run",
)
async def cancel_run_endpoint(
    request: Request,
    run_id: uuid.UUID,
    payload: OrchestrationCancelRequest = OrchestrationCancelRequest(),
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationExecuteResponse:
    owner_id = await _owner_id(request)
    return await orchestrator.cancel_run(owner_id, run_id, payload)


@router.post(
    "/runs/{run_id}/trust",
    response_model=OrchestrationExecuteResponse,
    summary="Trust a discovered agent and resume the same orchestration run",
)
async def trust_run_endpoint(
    request: Request,
    run_id: uuid.UUID,
    payload: OrchestrationTrustRequest = OrchestrationTrustRequest(),
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationExecuteResponse:
    owner_id = await _owner_id(request)
    return await orchestrator.trust_and_resume_run(owner_id, run_id, payload)


# --- Contact Management ---


@router.post(
    "/contacts",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new person contact with aliases and optional agent mapping",
)
async def create_contact(
    request: Request,
    payload: ContactCreateRequest,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> ContactResponse:
    owner_id = await _owner_id(request)
    repo = ContactRepository()
    async with orchestrator.session_factory() as session:
        c = await repo.create(
            session,
            owner_id=owner_id,
            display_name=payload.display_name,
            aliases=payload.aliases,
            agent_id=payload.agent_id,
            endpoint=payload.endpoint,
            notes=payload.notes,
        )
        await session.commit()
    return ContactResponse(
        id=str(c.id),
        display_name=c.display_name,
        aliases=c.aliases or [],
        agent_id=c.agent_id,
        endpoint=c.endpoint,
        notes=c.notes,
        created_at=c.created_at.isoformat(),
    )


@router.get(
    "/contacts",
    response_model=list[ContactResponse],
    summary="List all contacts for the current owner",
)
async def list_contacts(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> list[ContactResponse]:
    owner_id = await _owner_id(request)
    repo = ContactRepository()
    async with orchestrator.session_factory() as session:
        contacts = await repo.list_all(session, owner_id)
    return [
        ContactResponse(
            id=str(c.id),
            display_name=c.display_name,
            aliases=c.aliases or [],
            agent_id=c.agent_id,
            endpoint=c.endpoint,
            notes=c.notes,
            created_at=c.created_at.isoformat(),
        )
        for c in contacts
    ]
