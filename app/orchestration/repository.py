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
        task_id: str | None = None,
        workflow_id: uuid.UUID | None = None,
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
            task_id=task_id,
            workflow_id=workflow_id,
        )
        session.add(run)
        await session.flush()
        return run

    async def get_by_id(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
    ) -> OrchestrationRun | None:
        if run_id is None:
            stmt = select(OrchestrationRun).where(OrchestrationRun.id == owner_id)
        else:
            stmt = select(OrchestrationRun).where(
                OrchestrationRun.owner_id == owner_id, OrchestrationRun.id == run_id
            )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def transition_state(
        self, session: AsyncSession, run_id: uuid.UUID, state: OrchestrationState | str
    ) -> OrchestrationRun | None:
        stmt = select(OrchestrationRun).where(OrchestrationRun.id == run_id)
        result = await session.execute(stmt)
        run = result.scalar_one_or_none()
        if run is None:
            return None
        run.state = state.value if hasattr(state, "value") else str(state)
        run.updated_at = func.now()
        await session.flush()
        return run

    async def set_approval_request(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        *,
        reason: str,
        requested_action: str | None = None,
        approval_target: str | None = None,
        approval_category: str | None = None,
        approval_purpose: str | None = None,
        approval_step: Any = None,
    ) -> OrchestrationRun | None:
        stmt = select(OrchestrationRun).where(OrchestrationRun.id == run_id)
        result = await session.execute(stmt)
        run = result.scalar_one_or_none()
        if run is None:
            return None
        run.state = OrchestrationState.WAITING_APPROVAL.value
        run.requires_approval = True
        run.approval_reason = reason
        run.requested_action = requested_action
        run.approval_target = approval_target
        run.approval_category = approval_category
        run.approval_purpose = approval_purpose
        run.approval_step = approval_step
        run.updated_at = func.now()
        await session.flush()
        return run

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
        approval_reason: str | None = None,
        requested_action: str | None = None,
        approval_target: str | None = None,
        approval_category: str | None = None,
        approval_purpose: str | None = None,
        approval_step: int | None = None,
        owner_decision: str | None = None,
        task_id: str | None = None,
        workflow_id: uuid.UUID | None = None,
        plan: dict[str, Any] | None = None,
        target_agent_id: str | None = None,
    ) -> OrchestrationRun:
        stmt = select(OrchestrationRun).where(OrchestrationRun.id == run.id)
        res = await session.execute(stmt)
        active_run = res.scalar_one_or_none()
        if active_run is not None:
            run = active_run
        else:
            run = await session.merge(run)

        run.state = state
        if result is not None:
            run.result = result
        if error is not None:
            run.error = error
        if requires_approval is not None:
            run.requires_approval = requires_approval
        if approval_prompt is not None:
            run.approval_prompt = approval_prompt
        if approval_reason is not None:
            run.approval_reason = approval_reason
        if requested_action is not None:
            run.requested_action = requested_action
        if approval_target is not None:
            run.approval_target = approval_target
        if approval_category is not None:
            run.approval_category = approval_category
        if approval_purpose is not None:
            run.approval_purpose = approval_purpose
        if approval_step is not None:
            run.approval_step = approval_step
        if owner_decision is not None:
            run.owner_decision = owner_decision
        if task_id is not None:
            run.task_id = task_id
        if workflow_id is not None:
            run.workflow_id = workflow_id
        if plan is not None:
            run.plan = plan
        if target_agent_id is not None:
            run.target_agent_id = target_agent_id
        run.updated_at = func.now()
        await session.flush()
        return run

    async def find_by_task_id(
        self, session: AsyncSession, task_id: str
    ) -> OrchestrationRun | None:
        stmt = (
            select(OrchestrationRun)
            .where(OrchestrationRun.task_id == task_id)
            .order_by(desc(OrchestrationRun.created_at))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

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


class OrchestrationContextRepository:
    """Repository for managing persistent session orchestration context."""

    async def get_by_session_id(
        self, session: AsyncSession, session_id: str
    ) -> Any | None:
        from app.orchestration.models import OrchestrationContext
        stmt = select(OrchestrationContext).where(
            OrchestrationContext.session_id == session_id
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        owner_id: uuid.UUID,
        active_target: str | None = None,
        active_agent_id: str | None = None,
        last_proposed_time: str | None = None,
        last_task_id: str | None = None,
        last_run_id: str | None = None,
        last_intent_type: str | None = None,
        pending_approval: dict[str, Any] | None = None,
    ) -> Any:
        from app.orchestration.models import OrchestrationContext
        ctx = await self.get_by_session_id(session, session_id)
        if ctx is None:
            ctx = OrchestrationContext(
                session_id=session_id,
                owner_id=owner_id,
                active_target=active_target,
                active_agent_id=active_agent_id,
                last_proposed_time=last_proposed_time,
                last_task_id=last_task_id,
                last_run_id=last_run_id,
                last_intent_type=last_intent_type,
                pending_approval=pending_approval,
            )
            session.add(ctx)
        else:
            if active_target is not None:
                ctx.active_target = active_target
            if active_agent_id is not None:
                ctx.active_agent_id = active_agent_id
            if last_proposed_time is not None:
                ctx.last_proposed_time = last_proposed_time
            if last_task_id is not None:
                ctx.last_task_id = last_task_id
            if last_run_id is not None:
                ctx.last_run_id = last_run_id
            if last_intent_type is not None:
                ctx.last_intent_type = last_intent_type
            if pending_approval is not None:
                ctx.pending_approval = pending_approval
            ctx.updated_at = func.now()
        await session.flush()
        return ctx
