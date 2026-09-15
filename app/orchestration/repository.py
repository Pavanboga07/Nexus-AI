"""Database repositories for Contacts and Orchestration Runs."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.models import Contact, OrchestrationRun, OrchestrationState


class ContactRepository:
    """Repository for managing Contacts."""

    async def create(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        display_name: str,
        aliases: list[str] | None = None,
        agent_id: str | None = None,
        endpoint: str | None = None,
        notes: str | None = None,
    ) -> Contact:
        contact = Contact(
            owner_id=owner_id,
            display_name=display_name.strip(),
            aliases=[a.strip() for a in (aliases or []) if a.strip()],
            agent_id=agent_id.strip() if agent_id else None,
            endpoint=endpoint.strip() if endpoint else None,
            notes=notes,
        )
        session.add(contact)
        await session.flush()
        return contact

    async def get_by_id(
        self, session: AsyncSession, owner_id: uuid.UUID, contact_id: uuid.UUID
    ) -> Contact | None:
        stmt = select(Contact).where(
            Contact.owner_id == owner_id, Contact.id == contact_id
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> Sequence[Contact]:
        stmt = (
            select(Contact)
            .where(Contact.owner_id == owner_id)
            .order_by(Contact.display_name.asc())
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    async def find_by_name_or_alias(
        self, session: AsyncSession, owner_id: uuid.UUID, query: str
    ) -> list[Contact]:
        """Find contacts matching query by display_name or in aliases array."""
        clean_q = query.strip().lower()
        if not clean_q:
            return []

        # Fetch all contacts for owner and perform case-insensitive match
        all_contacts = await self.list_all(session, owner_id)
        matches: list[Contact] = []
        for c in all_contacts:
            if clean_q == c.display_name.lower():
                matches.append(c)
                continue
            if any(clean_q == a.lower() for a in (c.aliases or [])):
                matches.append(c)
                continue
            # Partial match (e.g. "rahul" in "rahul patil")
            if clean_q in c.display_name.lower() or any(
                clean_q in a.lower() for a in (c.aliases or [])
            ):
                matches.append(c)

        return matches


class OrchestrationRunRepository:
    """Repository for managing OrchestrationRuns."""

    async def create(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        session_id: str,
        goal: str,
        intent_type: str,
        state: str = OrchestrationState.UNDERSTANDING.value,
        target_person: str | None = None,
        target_agent_id: str | None = None,
        plan: dict[str, Any] | None = None,
    ) -> OrchestrationRun:
        run = OrchestrationRun(
            owner_id=owner_id,
            session_id=session_id,
            goal=goal,
            intent_type=intent_type,
            state=state,
            target_person=target_person,
            target_agent_id=target_agent_id,
            plan=plan or {},
        )
        session.add(run)
        await session.flush()
        return run

    async def get_by_id(
        self, session: AsyncSession, owner_id: uuid.UUID, run_id: uuid.UUID
    ) -> OrchestrationRun | None:
        stmt = select(OrchestrationRun).where(
            OrchestrationRun.owner_id == owner_id, OrchestrationRun.id == run_id
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_active_run(
        self, session: AsyncSession, owner_id: uuid.UUID, session_id: str
    ) -> OrchestrationRun | None:
        """Find the most recent active or waiting run for conversational follow-up."""
        active_states = {
            OrchestrationState.UNDERSTANDING.value,
            OrchestrationState.RESOLVING_TARGET.value,
            OrchestrationState.DISCOVERING_AGENT.value,
            OrchestrationState.WAITING_FOR_TRUST.value,
            OrchestrationState.PLANNING.value,
            OrchestrationState.AUTHORIZING.value,
            OrchestrationState.WAITING_APPROVAL.value,
            OrchestrationState.EXECUTING.value,
            OrchestrationState.WAITING_REMOTE.value,
            OrchestrationState.COMPLETED.value,  # recently completed is valid for follow-up!
        }
        stmt = (
            select(OrchestrationRun)
            .where(
                OrchestrationRun.owner_id == owner_id,
                OrchestrationRun.session_id == session_id,
                OrchestrationRun.state.in_(active_states),
            )
            .order_by(desc(OrchestrationRun.created_at))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_state(
        self,
        session: AsyncSession,
        run: OrchestrationRun,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        requires_approval: bool | None = None,
        approval_prompt: str | None = None,
        plan: dict[str, Any] | None = None,
        target_agent_id: str | None = None,
    ) -> OrchestrationRun:
        run.state = state
        if result is not None:
            run.result = result
        if error is not None:
            run.error = error
        if requires_approval is not None:
            run.requires_approval = requires_approval
        if approval_prompt is not None:
            run.approval_prompt = approval_prompt
        if plan is not None:
            run.plan = plan
        if target_agent_id is not None:
            run.target_agent_id = target_agent_id
        run.updated_at = func.now()
        await session.flush()
        return run

    async def list_runs(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        limit: int = 50,
    ) -> Sequence[OrchestrationRun]:
        stmt = (
            select(OrchestrationRun)
            .where(OrchestrationRun.owner_id == owner_id)
            .order_by(desc(OrchestrationRun.created_at))
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()
