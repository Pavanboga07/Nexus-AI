"""Identity routes: public identity and signature verification.

Only public material is ever exposed. The private key and its encrypted form
do not appear in any response.
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.a2a.service import A2AService
from app.api.dependencies import get_a2a_service, get_identity_service
from app.identity import crypto
from app.identity.service import IdentityService
from app.schemas.identity import (
    CapabilityListResponse,
    CapabilityOut,
    IdentityResponse,
    IdentityVerifyRequest,
    IdentityVerifyResponse,
)

logger = logging.getLogger("nexus.api.identity")

router = APIRouter()


@router.get(
    "/identity",
    response_model=IdentityResponse,
    tags=["identity"],
    summary="Public agent identity",
)
async def get_identity(
    identity_service: IdentityService = Depends(get_identity_service),
) -> IdentityResponse:
    if not identity_service.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent identity is not initialised.",
        )
    public = identity_service.get_public_identity()
    return IdentityResponse(
        agent_id=public.agent_id,
        public_key=public.public_key,
        key_algorithm=public.key_algorithm,
        fingerprint=public.fingerprint,
    )


@router.post(
    "/identity/verify",
    response_model=IdentityVerifyResponse,
    tags=["identity"],
    summary="Verify a signature against a claimed agent identity",
    description=(
        "Checks both that the signature verifies under the supplied public "
        "key AND that the supplied agent_id matches that public key. "
        "Intended for local development/testing ahead of Part 6 A2A."
    ),
)
async def verify_identity(
    payload: IdentityVerifyRequest,
    identity_service: IdentityService = Depends(get_identity_service),
) -> IdentityVerifyResponse:
    try:
        public_key_raw = base64.b64decode(
            payload.public_key.encode("ascii"), validate=True
        )
        signature = base64.b64decode(
            payload.signature.encode("ascii"), validate=True
        )
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="public_key and signature must be valid base64.",
        ) from None

    try:
        public_key = crypto.load_public_key(public_key_raw)
    except crypto.IdentityCryptoError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="public_key is not a valid Ed25519 public key (expected "
            "base64 of 32 raw bytes).",
        ) from None

    message = payload.message.encode("utf-8")
    signature_valid = crypto.verify_bytes(public_key, message, signature)
    agent_id_matches = (
        crypto.agent_id_from_public_key(public_key_raw) == payload.agent_id
    )

    valid = signature_valid and agent_id_matches
    reason = None
    if not signature_valid:
        reason = "signature does not verify for this message and public key"
    elif not agent_id_matches:
        reason = "agent_id does not match the supplied public key"

    logger.info(
        "identity_verify attempted valid=%s agent_id_matches=%s",
        valid,
        agent_id_matches,
    )
    return IdentityVerifyResponse(
        valid=valid, agent_id_matches=agent_id_matches, reason=reason
    )


@router.get(
    "/identity/capabilities",
    response_model=CapabilityListResponse,
    tags=["identity"],
    summary="Live capability registry for this agent",
    description=(
        "Returns the typed capability contracts (id, version, schemas) the "
        "agent will actually accept on the 0.2 envelope. This is the live "
        "registry - not a static list - so the Agent page's 'What your agent "
        "can do' panel cannot drift from what the receiver enforces."
    ),
)
async def list_own_capabilities(
    a2a_service: A2AService = Depends(get_a2a_service),
) -> CapabilityListResponse:
    specs = sorted(
        a2a_service.capabilities.values(), key=lambda s: s.id
    )
    return CapabilityListResponse(
        capabilities=[CapabilityOut(**s.to_dict()) for s in specs],
        total=len(specs),
    )


__all__ = ["router"]
