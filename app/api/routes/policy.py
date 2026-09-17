"""Policy & consent routes (owner-scoped).

Handlers stay thin: validate via schemas, delegate to PolicyService, map
errors to HTTP. The engine never sees the LLM or memory.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.agent.agent import NexusAgent
from app.api.auth_context import request_owner_id
from app.api.dependencies import get_agent, get_policy_service
from app.policy.engine import EvaluationRequest
from app.policy.service import PolicyService, PolicyServiceError
from app.schemas.policy import (
    AuditListResponse,
    ConsentCreate,
    ConsentListResponse,
    ConsentOut,
    DeletedResponse,
    PolicyCreate,
    PolicyEvaluateRequest,
    PolicyEvaluateResponse,
    PolicyListResponse,
    PolicyOut,
)

logger = logging.getLogger("nexus.api.policy")

router = APIRouter()


async def _owner_id(request: Request) -> uuid.UUID:
    return request_owner_id(request)


def _policy_out(policy) -> PolicyOut:
    data = policy.to_dict()
    return PolicyOut(**data)  # type: ignore[arg-type]


def _consent_out(consent) -> ConsentOut:
    data = consent.to_dict()
    return ConsentOut(**data)  # type: ignore[arg-type]


@router.get(
    "/policy",
    response_model=PolicyListResponse,
    tags=["policy"],
    summary="List the owner's policies",
)
async def list_policies(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> PolicyListResponse:
    owner_id = await _owner_id(request)
    policies = await policy_service.list_policies(owner_id)
    items = [_policy_out(p) for p in policies]
    return PolicyListResponse(policies=items, total=len(items))


@router.post(
    "/policy",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
    tags=["policy"],
    summary="Create a policy rule",
)
async def create_policy(
    request: Request,
    payload: PolicyCreate,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> PolicyOut:
    owner_id = await _owner_id(request)
    try:
        policy = await policy_service.create_policy(
            owner_id,
            requester_agent_id=payload.requester_agent_id,
            data_category=payload.data_category,
            action=payload.action,
            purpose=payload.purpose,
            decision=payload.decision,
            disclosure_scope=payload.disclosure_scope,
            priority=payload.priority,
            starts_at=payload.starts_at,
            expires_at=payload.expires_at,
        )
    except PolicyServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _policy_out(policy)


@router.delete(
    "/policy/{policy_id}",
    response_model=DeletedResponse,
    tags=["policy"],
    summary="Delete a policy rule",
    responses={404: {"description": "Policy not found"}},
)
async def delete_policy(
    request: Request,
    policy_id: str,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> DeletedResponse:
    try:
        policy_uuid = uuid.UUID(policy_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found."
        ) from None
    owner_id = await _owner_id(request)
    deleted = await policy_service.delete_policy(owner_id, policy_uuid)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found."
        )
    return DeletedResponse(deleted=True, id=policy_id)


@router.get(
    "/consent",
    response_model=ConsentListResponse,
    tags=["consent"],
    summary="List the owner's consents",
)
async def list_consents(
    request: Request,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> ConsentListResponse:
    owner_id = await _owner_id(request)
    consents = await policy_service.list_consents(owner_id)
    items = [_consent_out(c) for c in consents]
    return ConsentListResponse(consents=items, total=len(items))


@router.post(
    "/consent",
    response_model=ConsentOut,
    status_code=status.HTTP_201_CREATED,
    tags=["consent"],
    summary="Record an owner consent (approval or denial)",
    description=(
        "Records what the owner explicitly approved/denied - e.g. the "
        "answer to an ASK decision. Duration is explicit: single_use, "
        "expires_at, or persistent. Nothing is implicit."
    ),
)
async def create_consent(
    request: Request,
    payload: ConsentCreate,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> ConsentOut:
    owner_id = await _owner_id(request)
    try:
        consent = await policy_service.create_consent(
            owner_id,
            requester_agent_id=payload.requester_agent_id,
            data_category=payload.data_category,
            action=payload.action,
            purpose=payload.purpose,
            decision=payload.decision,
            disclosure_scope=payload.disclosure_scope,
            expires_at=payload.expires_at,
            single_use=payload.single_use,
        )
    except PolicyServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _consent_out(consent)


@router.delete(
    "/consent/{consent_id}",
    response_model=DeletedResponse,
    tags=["consent"],
    summary="Revoke/delete a consent",
    responses={404: {"description": "Consent not found"}},
)
async def delete_consent(
    request: Request,
    consent_id: str,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> DeletedResponse:
    try:
        consent_uuid = uuid.UUID(consent_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Consent not found."
        ) from None
    owner_id = await _owner_id(request)
    deleted = await policy_service.delete_consent(owner_id, consent_uuid)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Consent not found."
        )
    return DeletedResponse(deleted=True, id=consent_id)


@router.post(
    "/policy/evaluate",
    response_model=PolicyEvaluateResponse,
    tags=["policy"],
    summary="Evaluate an authorization request",
)
async def evaluate_policy(
    request: Request,
    payload: PolicyEvaluateRequest,
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> PolicyEvaluateResponse:
    owner_id = await _owner_id(request)
    result = await policy_service.evaluate(
        owner_id,
        EvaluationRequest(
            requester_agent_id=payload.requester_agent_id,
            data_category=payload.data_category,
            action=payload.action,
            purpose=payload.purpose,
            resource_id=payload.resource_id,
        ),
    )
    return PolicyEvaluateResponse(**result.to_dict())  # type: ignore[arg-type]


@router.get(
    "/policy/audit",
    response_model=AuditListResponse,
    tags=["policy"],
    summary="Audit trail of policy decisions (owner-scoped)",
)
async def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    agent: NexusAgent = Depends(get_agent),
    policy_service: PolicyService = Depends(get_policy_service),
) -> AuditListResponse:
    owner_id = await _owner_id(request)
    decisions = await policy_service.list_decisions(owner_id, limit=limit)
    entries = []
    for record in decisions:
        entry = record.to_dict()
        entries.append(entry)
    return AuditListResponse(decisions=entries, total=len(entries))


__all__ = ["router"]
