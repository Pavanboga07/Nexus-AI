"""Policy repository: the only place policy/consent/audit SQL lives.

Every method is owner-scoped (spec §19). There is deliberately no unscoped
read method anywhere in this class.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.policy.models import Consent, Policy, PolicyDecisionRecord


class PolicyRepository:
    # --- Policies ---------------------------------------------------------

    async def list_policies(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> list[Policy]:
        result = await session.execute(
            select(Policy)
            .where(Policy.owner_id == owner_id)
            .order_by(Policy.created_at.asc())
        )
        return list(result.scalars())

    async def add_policy(self, session: AsyncSession, policy: Policy) -> Policy:
        session.add(policy)
        await session.flush()
        return policy

    async def get_policy(
        self, session: AsyncSession, owner_id: uuid.UUID, policy_id: uuid.UUID
    ) -> Policy | None:
        result = await session.execute(
            select(Policy).where(
                Policy.id == policy_id, Policy.owner_id == owner_id
            )
        )
        return result.scalar_one_or_none()

    async def delete_policy(
        self, session: AsyncSession, owner_id: uuid.UUID, policy_id: uuid.UUID
    ) -> bool:
        result = await session.execute(
            delete(Policy).where(
                Policy.id == policy_id, Policy.owner_id == owner_id
            )
        )
        return bool(result.rowcount)

    # --- Consents -----------------------------------------------------------

    async def list_consents(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> list[Consent]:
        result = await session.execute(
            select(Consent)
            .where(Consent.owner_id == owner_id)
            .order_by(Consent.created_at.asc())
        )
        return list(result.scalars())

    async def add_consent(self, session: AsyncSession, consent: Consent) -> Consent:
        session.add(consent)
        await session.flush()
        return consent

    async def get_consent(
        self, session: AsyncSession, owner_id: uuid.UUID, consent_id: uuid.UUID
    ) -> Consent | None:
        result = await session.execute(
            select(Consent).where(
                Consent.id == consent_id, Consent.owner_id == owner_id
            )
        )
        return result.scalar_one_or_none()

    async def delete_consent(
        self, session: AsyncSession, owner_id: uuid.UUID, consent_id: uuid.UUID
    ) -> bool:
        result = await session.execute(
            delete(Consent).where(
                Consent.id == consent_id, Consent.owner_id == owner_id
            )
        )
        return bool(result.rowcount)

    async def find_matching_consent(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        *,
        requester_agent_id: str,
        data_category: str,
        action: str,
        purpose: str,
        now: datetime | None = None,
    ) -> Consent | None:
        """Most recent valid, unconsumed consent matching the request exactly.

        Consents never use wildcards, so matching is equality on all four
        dimensions. Expired and already-consumed consents never match.
        """
        now = now or datetime.now(UTC)
        result = await session.execute(
            select(Consent)
            .where(
                Consent.owner_id == owner_id,
                Consent.requester_agent_id == requester_agent_id,
                Consent.data_category == data_category,
                Consent.action == action,
                Consent.purpose == purpose,
                Consent.used_at.is_(None),
            )
            .order_by(Consent.created_at.desc())
        )
        for consent in result.scalars():
            if consent.expires_at is not None and consent.expires_at <= now:
                continue
            return consent
        return None

    async def try_consume_consent(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        consent_id: uuid.UUID,
    ) -> bool:
        """Atomically consume a single-use consent (spec §17).

        ``UPDATE ... WHERE used_at IS NULL`` is serialised by PostgreSQL row
        locking: of two concurrent consumers exactly one gets rowcount 1.
        Must be committed by the caller in the same transaction.
        """
        result = await session.execute(
            update(Consent)
            .where(
                Consent.id == consent_id,
                Consent.owner_id == owner_id,
                Consent.used_at.is_(None),
            )
            .values(used_at=datetime.now(UTC))
        )
        return bool(result.rowcount)

    # --- Audit ---------------------------------------------------------------

    async def record_decision(
        self,
        session: AsyncSession,
        record: PolicyDecisionRecord,
    ) -> PolicyDecisionRecord:
        session.add(record)
        await session.flush()
        return record

    async def list_decisions(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> list[PolicyDecisionRecord]:
        result = await session.execute(
            select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.owner_id == owner_id)
            .order_by(PolicyDecisionRecord.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())


__all__ = ["PolicyRepository"]
