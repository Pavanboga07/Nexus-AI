"""PolicyService: the app-facing authorization facade (Part 4).

    Agent / API
        -> PolicyService          (this file)
            -> PolicyEngine       (pure decision)
            -> PolicyRepository   (SQL, owner-scoped)
                -> PostgreSQL

Every evaluation is audited. Single-use consents are consumed atomically in
the same transaction that records the ALLOW.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.policy.engine import (
    EvaluationRequest,
    EvaluationResult,
    PolicyEngine,
)
from app.policy.models import (
    WILDCARD,
    Consent,
    DisclosureScope,
    Policy,
    PolicyDecision,
    PolicyDecisionRecord,
)
from app.policy.repository import PolicyRepository

logger = logging.getLogger("nexus.policy")


class PolicyServiceError(Exception):
    """Invalid policy/consent input (validation failures)."""


class PolicyService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._engine = PolicyEngine()
        self._repo = PolicyRepository()

    # --- Evaluation ------------------------------------------------------------

    async def evaluate(
        self, owner_id: uuid.UUID, request: EvaluationRequest
    ) -> EvaluationResult:
        """Authorize a request: consent/policy lookup, decision, audit,
        and (for single-use consents) atomic consumption - all in one
        transaction."""
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            consents = await self._repo.list_consents(session, owner_id)
            policies = await self._repo.list_policies(session, owner_id)

            result = self._engine.evaluate(
                request, policies=policies, consents=consents, now=now
            )

            # Consume single-use consents only when they actually granted
            # this ALLOW, transactionally with the audit record.
            if (
                result.matched_consent_id is not None
                and result.decision is PolicyDecision.ALLOW
            ):
                consumed = await self._repo.try_consume_consent(
                    session, owner_id, uuid.UUID(result.matched_consent_id)
                )
                if not consumed:
                    # Raced: another request consumed it between our read and
                    # this write. Re-evaluate without it.
                    consents = [
                        c
                        for c in consents
                        if str(c.id) != result.matched_consent_id
                    ]
                    result = self._engine.evaluate(
                        request, policies=policies, consents=consents, now=now
                    )

            await self._repo.record_decision(
                session,
                PolicyDecisionRecord(
                    owner_id=owner_id,
                    requester_agent_id=request.requester_agent_id,
                    data_category=request.data_category,
                    action=request.action,
                    purpose=request.purpose,
                    decision=result.decision.value,
                    matched_policy_id=(
                        uuid.UUID(result.matched_policy_id)
                        if result.matched_policy_id
                        else None
                    ),
                    matched_consent_id=(
                        uuid.UUID(result.matched_consent_id)
                        if result.matched_consent_id
                        else None
                    ),
                    reason=result.reason,
                ),
            )
            await session.commit()

        logger.info(
            "policy_decision requester=%s category=%s action=%s purpose=%s "
            "decision=%s",
            request.requester_agent_id,
            request.data_category,
            request.action,
            request.purpose,
            result.decision.value,
        )
        return result

    # --- Policy CRUD (owner-scoped) ----------------------------------------------

    async def create_policy(
        self,
        owner_id: uuid.UUID,
        *,
        requester_agent_id: str,
        data_category: str,
        action: str,
        purpose: str,
        decision: str,
        disclosure_scope: str = "category",
        priority: int = 0,
        starts_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> Policy:
        self._validate_rule_fields(
            requester_agent_id=requester_agent_id,
            data_category=data_category,
            action=action,
            purpose=purpose,
        )
        if decision not in {d.value for d in PolicyDecision}:
            raise PolicyServiceError(
                f"decision must be one of ALLOW, ASK, DENY (got {decision!r})"
            )
        if disclosure_scope not in {s.value for s in DisclosureScope}:
            raise PolicyServiceError(
                "disclosure_scope must be one of none, category, summary, exact"
            )
        if expires_at is not None and starts_at is not None and expires_at <= starts_at:
            raise PolicyServiceError("expires_at must be after starts_at")

        async with self._session_factory() as session:
            policy = Policy(
                owner_id=owner_id,
                requester_agent_id=requester_agent_id,
                data_category=data_category,
                action=action,
                purpose=purpose,
                decision=decision,
                disclosure_scope=disclosure_scope,
                priority=priority,
                starts_at=starts_at,
                expires_at=expires_at,
            )
            await self._repo.add_policy(session, policy)
            await session.commit()
            logger.info(
                "policy_created id=%s decision=%s category=%s",
                policy.id,
                decision,
                data_category,
            )
            return policy

    async def list_policies(self, owner_id: uuid.UUID) -> list[Policy]:
        async with self._session_factory() as session:
            return await self._repo.list_policies(session, owner_id)

    async def get_policy(
        self, owner_id: uuid.UUID, policy_id: uuid.UUID
    ) -> Policy | None:
        async with self._session_factory() as session:
            return await self._repo.get_policy(session, owner_id, policy_id)

    async def delete_policy(self, owner_id: uuid.UUID, policy_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            deleted = await self._repo.delete_policy(session, owner_id, policy_id)
            await session.commit()
        if deleted:
            logger.info("policy_deleted id=%s", policy_id)
        return deleted

    # --- Consent CRUD (owner-scoped) ------------------------------------------------

    async def create_consent(
        self,
        owner_id: uuid.UUID,
        *,
        requester_agent_id: str,
        data_category: str,
        action: str,
        purpose: str,
        decision: str,
        disclosure_scope: str = "category",
        expires_at: datetime | None = None,
        single_use: bool = False,
    ) -> Consent:
        # Consents record a concrete approval: no wildcards allowed.
        self._validate_rule_fields(
            requester_agent_id=requester_agent_id,
            data_category=data_category,
            action=action,
            purpose=purpose,
            allow_wildcards=False,
        )
        if decision not in {"ALLOW", "DENY"}:
            raise PolicyServiceError(
                "consent decision must be ALLOW or DENY "
                "(ASK is not a recordable answer)"
            )
        if disclosure_scope not in {s.value for s in DisclosureScope}:
            raise PolicyServiceError(
                "disclosure_scope must be one of none, category, summary, exact"
            )
        if single_use and expires_at is not None:
            # Allowed, but unusual; both limits apply (whichever hits first).
            pass

        async with self._session_factory() as session:
            consent = Consent(
                owner_id=owner_id,
                requester_agent_id=requester_agent_id,
                data_category=data_category,
                action=action,
                purpose=purpose,
                decision=decision,
                disclosure_scope=disclosure_scope,
                expires_at=expires_at,
                single_use=single_use,
            )
            await self._repo.add_consent(session, consent)
            await session.commit()
            logger.info(
                "consent_created id=%s decision=%s single_use=%s",
                consent.id,
                decision,
                single_use,
            )
            return consent

    async def list_consents(self, owner_id: uuid.UUID) -> list[Consent]:
        async with self._session_factory() as session:
            return await self._repo.list_consents(session, owner_id)

    async def get_consent(
        self, owner_id: uuid.UUID, consent_id: uuid.UUID
    ) -> Consent | None:
        async with self._session_factory() as session:
            return await self._repo.get_consent(session, owner_id, consent_id)

    async def delete_consent(self, owner_id: uuid.UUID, consent_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            deleted = await self._repo.delete_consent(session, owner_id, consent_id)
            await session.commit()
        if deleted:
            logger.info("consent_deleted id=%s", consent_id)
        return deleted

    # --- Audit ----------------------------------------------------------------------

    async def list_decisions(
        self, owner_id: uuid.UUID, *, limit: int = 100
    ) -> list[PolicyDecisionRecord]:
        async with self._session_factory() as session:
            return await self._repo.list_decisions(session, owner_id, limit=limit)

    # --- Validation --------------------------------------------------------------------

    @staticmethod
    def _validate_rule_fields(
        *,
        requester_agent_id: str,
        data_category: str,
        action: str,
        purpose: str,
        allow_wildcards: bool = True,
    ) -> None:
        """Boundary validation: format slugs, cap lengths, reject injection
        junk. Unknown-but-well-formed slugs are allowed so future categories
        work without code changes (spec §5)."""
        def _check_slug(value: str, field: str, wildcard_ok: bool) -> None:
            if not isinstance(value, str) or not value.strip():
                raise PolicyServiceError(f"{field} must be a non-empty string")
            if len(value) > 64 and value != WILDCARD:
                raise PolicyServiceError(f"{field} exceeds 64 characters")
            if value != value.strip():
                raise PolicyServiceError(f"{field} must not have surrounding whitespace")
            if value == WILDCARD:
                if not wildcard_ok:
                    raise PolicyServiceError(
                        f"{field} may not be the wildcard '*' in a consent"
                    )
                return
            # Slug vocabulary: lowercase letters, digits, hyphen, colon,
            # dot, underscore. Blocks SQL/HTML injection payloads.
            if not re.fullmatch(r"[a-z0-9:_\-.]+", value):
                raise PolicyServiceError(
                    f"{field} contains invalid characters (allowed: "
                    "lowercase letters, digits, - : _ .)"
                )

        _check_slug(requester_agent_id, "requester_agent_id", allow_wildcards)
        _check_slug(data_category, "data_category", allow_wildcards)
        _check_slug(action, "action", allow_wildcards)
        _check_slug(purpose, "purpose", allow_wildcards)


__all__ = ["PolicyService", "PolicyServiceError"]
