"""REST API endpoints for Natural Language Agent Orchestration (Part 12)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agent import NexusAgent
from app.api.dependencies import get_agent, get_orchestrator
from app.orchestration.models import OrchestrationRun
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.repository import ContactRepository, OrchestrationRunRepository
from app.orchestration.schemas import (
    ContactCreateRequest,
    ContactResponse,
    OrchestrationExecuteRequest,
    OrchestrationExecuteResponse,
    OrchestrationRunResponse,
)

logger = logging.getLogger("nexus.api.orchestration")

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


async def _owner_id(agent: NexusAgent) -> uuid.UUID:
    return await agent._owner_id()


@router.post(
    "/execute",
    response_model=OrchestrationExecuteResponse,
    summary="Execute a natural language agent orchestration request",
)
async def execute_orchestration(
    payload: OrchestrationExecuteRequest,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationExecuteResponse:
    owner_id = await _owner_id(agent)
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
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> list[OrchestrationRunResponse]:
    owner_id = await _owner_id(agent)
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
    run_id: uuid.UUID,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> OrchestrationRunResponse:
    owner_id = await _owner_id(agent)
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
        requires_approval=r.requires_approval,
        approval_prompt=r.approval_prompt,
        result=r.result,
        error=r.error,
        created_at=r.created_at.isoformat(),
    )


# --- Contact Management ---


@router.post(
    "/contacts",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new person contact with aliases and optional agent mapping",
)
async def create_contact(
    payload: ContactCreateRequest,
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> ContactResponse:
    owner_id = await _owner_id(agent)
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
    agent: NexusAgent = Depends(get_agent),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> list[ContactResponse]:
    owner_id = await _owner_id(agent)
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
