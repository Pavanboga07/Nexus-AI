"""Agent Orchestrator: Central coordinator for Natural Language Agent Orchestration.

Orchestrates:
    User Message
        -> Context Reference Resolution (him, it, them)
        -> Intent Resolution
        -> Target Resolution
        -> Trust Verification / Bootstrap
        -> Bounded Planning / Durable Workflows
        -> Authoritative Policy Execution
        -> Human-friendly Natural Language Summary
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a import signing
from app.a2a.models import TrustStatus
from app.a2a.repository import TrustedAgentRepository
from app.a2a.service import A2AService
from app.autonomy.decision_engine import DecisionEngine
from app.orchestration.context import OrchestrationContextManager
from app.orchestration.errors import (
    AmbiguousTargetError,
    OrchestrationError,
    OrchestrationPolicyError,
    TargetResolutionError,
    UntrustedAgentError,
)
from app.orchestration.executor import OrchestrationExecutor
from app.orchestration.intent import IntentResolver
from app.orchestration.models import OrchestrationRun, OrchestrationState
from app.orchestration.planner import OrchestrationPlanner
from app.orchestration.repository import ContactRepository, OrchestrationRunRepository
from app.orchestration.schemas import (
    Intent,
    IntentType,
    OrchestrationApproveRequest,
    OrchestrationCancelRequest,
    OrchestrationExecuteResponse,
    OrchestrationPlan,
    OrchestrationRejectRequest,
    OrchestrationTrustRequest,
    TargetResolution,
    TargetResolutionStatus,
)
from app.policy.service import PolicyService

logger = logging.getLogger("nexus.orchestration.orchestrator")


class AgentOrchestrator:
    """End-to-end natural language agent orchestrator."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        a2a_service: A2AService,
        policy_service: PolicyService,
        intent_resolver: IntentResolver,
        target_resolver: Any,
        decision_engine: DecisionEngine | None = None,
        context_manager: OrchestrationContextManager | None = None,
        planner: OrchestrationPlanner | None = None,
        executor: OrchestrationExecutor | None = None,
        workflow_service: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._a2a = a2a_service
        self._policy = policy_service
        self._intent_resolver = intent_resolver
        self._target_resolver = target_resolver
        self._decision_engine = decision_engine
        self._workflow = workflow_service
        self._context_mgr = context_manager or OrchestrationContextManager(session_factory=session_factory)
        self._planner = planner or OrchestrationPlanner()
        self._executor = executor or OrchestrationExecutor(
            a2a_service=a2a_service,
            policy_service=policy_service,
            decision_engine=decision_engine,
            workflow_service=workflow_service,
        )
        self._runs_repo = OrchestrationRunRepository()
        self._contacts_repo = ContactRepository()
        self._trusted_repo = TrustedAgentRepository()

        # Register callback for asynchronous remote responses
        if hasattr(self._a2a, "register_task_completion_callback"):
            self._a2a.register_task_completion_callback(self.handle_task_completion)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    async def handle_user_message(
        self,
        owner_id: uuid.UUID,
        session_id: str,
        message: str,
    ) -> OrchestrationExecuteResponse | None:
        """Process a user message through the orchestration pipeline.

        Returns an OrchestrationExecuteResponse if the message was handled by
        the orchestration engine, or None if it should fall through to general chat.
        """
        # 1. Resolve pronouns and references against active context
        resolved_msg, active_target = await self._context_mgr.resolve_references(owner_id, session_id, message)

        # 2. Extract structured Intent
        intent = await self._intent_resolver.resolve_intent(
            resolved_msg,
            active_target=active_target,
        )

        # Check if this is a confirmation ("Yes", "Confirm", "Book it") for an active waiting run
        if intent.intent_type == IntentType.CONFIRM_ACTION:
            async with self._session_factory() as session:
                latest_run = await self._runs_repo.get_latest_active_run(session, owner_id, session_id)
            if latest_run is not None:
                if latest_run.state == OrchestrationState.WAITING_FOR_TRUST.value:
                    # User confirmed trust establishment
                    return await self.trust_and_resume_run(owner_id, latest_run.id)
                elif latest_run.state == OrchestrationState.WAITING_APPROVAL.value:
                    # User approved pending action
                    return await self.approve_run(owner_id, latest_run.id)

        # If ordinary conversational chat, fall through to regular LLM chat
        if intent.intent_type == IntentType.GENERAL_CHAT:
            return None

        # 3. Create persistent run record
        run_id = uuid.uuid4()
        async with self._session_factory() as session:
            run = await self._runs_repo.create(
                session,
                owner_id=owner_id,
                session_id=session_id,
                goal=intent.goal,
                intent_type=intent.intent_type.value,
                state=OrchestrationState.UNDERSTANDING.value,
                target_person=intent.target,
            )
            await session.commit()

        try:
            return await self._execute_orchestration(owner_id, session_id, run, intent)
        except OrchestrationError as exc:
            logger.warning("Orchestration error during execution: %s", exc)
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session, run, state=OrchestrationState.FAILED.value, error=str(exc)
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="failed",
                message=exc.user_message,
                intent_type=intent.intent_type.value,
                target=intent.target,
            )
        except Exception as exc:
            logger.exception("Unexpected error in orchestration pipeline: %s", exc)
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session, run, state=OrchestrationState.FAILED.value, error=str(exc)
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="failed",
                message="I encountered an issue processing that request with the remote agent.",
                intent_type=intent.intent_type.value,
                target=intent.target,
            )

    async def _execute_orchestration(
        self,
        owner_id: uuid.UUID,
        session_id: str,
        run: OrchestrationRun,
        intent: Intent,
    ) -> OrchestrationExecuteResponse:
        """Step-by-step state machine execution for an intent."""
        # 1. Target Resolution
        target_name = intent.target
        if not target_name:
            ctx = await self._context_mgr.get_context(owner_id, session_id)
            target_name = ctx.active_target

        if not target_name:
            raise TargetResolutionError(
                "Missing target person",
                user_message="Who would you like me to connect with?",
            )

        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session, run, state=OrchestrationState.RESOLVING_TARGET.value
            )
            await session.commit()

        target_res = await self._target_resolver.resolve(owner_id, target_name)

        if target_res.status == TargetResolutionStatus.AMBIGUOUS_AGENT:
            cand_str = ", ".join(target_res.candidates)
            msg = f"I found multiple contacts matching '{target_name}': {cand_str}. Which one did you mean?"
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session, run, state=OrchestrationState.FAILED.value, error="Ambiguous target"
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="ambiguous",
                message=msg,
                intent_type=intent.intent_type.value,
                target=target_name,
            )

        if target_res.status == TargetResolutionStatus.UNKNOWN_AGENT:
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session, run, state=OrchestrationState.FAILED.value, error="Unknown agent"
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="not_found",
                message=f"I couldn't find a reachable Nexus agent for {target_name}.",
                intent_type=intent.intent_type.value,
                target=target_name,
            )

        if target_res.status == TargetResolutionStatus.DISCOVERED_AGENT or not target_res.is_trusted:
            # Trust Bootstrap flow (Part 13 P0)
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session,
                    run,
                    state=OrchestrationState.WAITING_FOR_TRUST.value,
                    target_agent_id=target_res.agent_id,
                    requires_approval=True,
                    approval_prompt=f"I found {target_name}'s Nexus agent. I haven't connected to it before. Would you like me to connect?",
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="waiting_trust",
                message=f"I found {target_name}'s Nexus agent. I haven't connected to it before. Would you like me to connect?",
                intent_type=intent.intent_type.value,
                target=target_name,
                requires_approval=True,
                approval_prompt=f"Connect with {target_name}'s agent ({target_res.agent_id})?",
            )

        # 2. Multi-agent secondary target resolution (if applicable)
        sec_resolutions: list[TargetResolution] = []
        for sec_name in intent.secondary_targets:
            sec_res = await self._target_resolver.resolve(owner_id, sec_name)
            if sec_res.status == TargetResolutionStatus.KNOWN_AGENT:
                sec_resolutions.append(sec_res)

        # 3. Planning
        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.PLANNING.value,
                target_agent_id=target_res.agent_id,
            )
            await session.commit()

        plan = self._planner.build_plan(intent, target_res, sec_resolutions)

        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.EXECUTING.value,
                plan=plan.model_dump(),
            )
            await session.commit()

        # 4. Authoritative Execution
        result = await self._executor.execute_plan(owner_id, plan, target_res)

        # 5. Update context for follow-up turns
        await self._context_mgr.update_target(
            owner_id, session_id, target_name, agent_id=target_res.agent_id
        )
        if result.get("alternatives"):
            await self._context_mgr.update_proposed_time(
                owner_id, session_id, result["alternatives"][0]
            )

        # 6. Check state - never mark completed if awaiting approval or waiting for remote agent!
        res_status = result.get("status", "completed")
        requires_approval = result.get("requires_approval", False)

        if res_status == "waiting_approval" or requires_approval:
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session,
                    run,
                    state=OrchestrationState.WAITING_APPROVAL.value,
                    requires_approval=True,
                    approval_prompt=result.get("approval_prompt"),
                    approval_reason=result.get("approval_reason"),
                    requested_action=result.get("requested_action"),
                    approval_target=result.get("approval_target") or target_name,
                    approval_category=result.get("approval_category"),
                    approval_purpose=result.get("approval_purpose"),
                    approval_step=result.get("approval_step"),
                    result=result,
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="waiting_approval",
                message=result.get("message", "This action requires your confirmation."),
                intent_type=intent.intent_type.value,
                target=target_name,
                requires_approval=True,
                approval_prompt=result.get("approval_prompt"),
                details=result.get("details"),
            )

        if res_status in {"queued", "waiting_remote"}:
            task_id = result.get("details", {}).get("task_id") or result.get("task_id")
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session,
                    run,
                    state=OrchestrationState.WAITING_REMOTE.value,
                    task_id=task_id,
                    result=result,
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="queued",
                message=result.get("message", f"{target_name}'s agent is offline. I've queued the request and I'll continue when it reconnects."),
                intent_type=intent.intent_type.value,
                target=target_name,
                details=result.get("details"),
            )

        # 7. Completed successfully
        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.COMPLETED.value,
                result=result,
            )
            await session.commit()

        return OrchestrationExecuteResponse(
            run_id=str(run.id),
            status=result.get("status", "completed"),
            message=result.get("message", "Task completed."),
            intent_type=intent.intent_type.value,
            target=target_name,
            details=result.get("details"),
        )

    # ------------------------------------------------------------- RESUMABLE ENDPOINTS (Part 13)

    async def approve_run(
        self,
        owner_id: uuid.UUID,
        run_id: uuid.UUID,
        req: OrchestrationApproveRequest | None = None,
    ) -> OrchestrationExecuteResponse:
        """Approve a pending action in an orchestration run, resuming the SAME run."""
        async with self._session_factory() as session:
            run = await self._runs_repo.get_by_id(session, owner_id, run_id)
        if run is None:
            raise TargetResolutionError(f"Orchestration run {run_id} not found.")

        if run.state != OrchestrationState.WAITING_APPROVAL.value:
            raise OrchestrationError(f"Run {run_id} is not awaiting approval (current state: {run.state})")

        # 1. Authorize exact pending action by creating single-use consent.
        # Skip for workflow-backed runs: the workflow's own approval mints the
        # exact-scope consent, so minting here would leave an unconsumed duplicate.
        is_workflow_backed = run.workflow_id is not None and self._workflow is not None
        if not is_workflow_backed:
            action = run.requested_action or "disclose_information"
            data_cat = run.approval_category or "availability"
            purpose = run.approval_purpose or "collaboration"
            target_agent_id = run.target_agent_id or "nexus:anonymous"

            await self._policy.create_consent(
                owner_id,
                requester_agent_id=target_agent_id,
                data_category=data_cat,
                action=action,
                purpose=purpose,
                decision="ALLOW",
                single_use=True,
            )

        # 2. Update run record to EXECUTING
        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.EXECUTING.value,
                requires_approval=False,
                owner_decision="approved",
            )
            await session.commit()

        # 3. If run was backed by a workflow, resume the workflow
        if run.workflow_id and self._workflow is not None:
            wf = await self._workflow.approve_workflow(owner_id, run.workflow_id)
            if wf.status == "completed":
                final_res = {"status": "completed", "message": "Workflow completed successfully."}
                async with self._session_factory() as session:
                    await self._runs_repo.update_state(session, run, state=OrchestrationState.COMPLETED.value, result=final_res)
                    await session.commit()
                return OrchestrationExecuteResponse(
                    run_id=str(run.id),
                    status="completed",
                    message="Workflow completed successfully.",
                    intent_type=run.intent_type,
                    target=run.target_person,
                )
            elif wf.status in {"waiting_remote", "waiting_approval"}:
                st = OrchestrationState.WAITING_REMOTE.value if wf.status == "waiting_remote" else OrchestrationState.WAITING_APPROVAL.value
                async with self._session_factory() as session:
                    await self._runs_repo.update_state(session, run, state=st)
                    await session.commit()
                return OrchestrationExecuteResponse(
                    run_id=str(run.id),
                    status="waiting_remote" if wf.status == "waiting_remote" else "waiting_approval",
                    message="Waiting for remote response.",
                    intent_type=run.intent_type,
                    target=run.target_person,
                )
            elif wf.status == "running":
                # A5 timing: advancement is deferred to jobs, so the workflow
                # is resuming asynchronously; the run is already EXECUTING.
                return OrchestrationExecuteResponse(
                    run_id=str(run.id),
                    status="executing",
                    message="Workflow resuming.",
                    intent_type=run.intent_type,
                    target=run.target_person,
                )

        # 4. Otherwise re-execute pending plan step directly
        plan_data = run.plan
        target_res = await self._target_resolver.resolve(owner_id, run.target_person or "")
        plan = OrchestrationPlan.model_validate(plan_data)
        result = await self._executor.execute_plan(owner_id, plan, target_res)

        new_state = OrchestrationState.COMPLETED.value
        if result.get("status") == "waiting_approval":
            new_state = OrchestrationState.WAITING_APPROVAL.value
        elif result.get("status") in {"queued", "waiting_remote"}:
            new_state = OrchestrationState.WAITING_REMOTE.value

        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=new_state,
                result=result,
                task_id=result.get("details", {}).get("task_id") or result.get("task_id"),
            )
            await session.commit()

        return OrchestrationExecuteResponse(
            run_id=str(run.id),
            status=result.get("status", "completed"),
            message=result.get("message", "Task completed."),
            intent_type=run.intent_type,
            target=run.target_person,
            details=result.get("details"),
        )

    async def reject_run(
        self,
        owner_id: uuid.UUID,
        run_id: uuid.UUID,
        req: OrchestrationRejectRequest | None = None,
    ) -> OrchestrationExecuteResponse:
        """Reject a pending approval action in an orchestration run."""
        reason = req.reason if req else "User declined"
        async with self._session_factory() as session:
            run = await self._runs_repo.get_by_id(session, owner_id, run_id)
        if run is None:
            raise TargetResolutionError(f"Orchestration run {run_id} not found.")

        if run.workflow_id and self._workflow is not None:
            try:
                await self._workflow.cancel_workflow(owner_id, run.workflow_id, reason=reason)
            except Exception:
                pass

        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.CANCELLED.value,
                requires_approval=False,
                owner_decision="rejected",
                error=reason,
            )
            await session.commit()

        return OrchestrationExecuteResponse(
            run_id=str(run.id),
            status="cancelled",
            message=f"Action rejected: {reason}",
            intent_type=run.intent_type,
            target=run.target_person,
        )

    async def cancel_run(
        self,
        owner_id: uuid.UUID,
        run_id: uuid.UUID,
        req: OrchestrationCancelRequest | None = None,
    ) -> OrchestrationExecuteResponse:
        """Cancel an in-flight or waiting orchestration run."""
        reason = req.reason if req else "User cancelled"
        async with self._session_factory() as session:
            run = await self._runs_repo.get_by_id(session, owner_id, run_id)
        if run is None:
            raise TargetResolutionError(f"Orchestration run {run_id} not found.")

        if run.workflow_id and self._workflow is not None:
            try:
                await self._workflow.cancel_workflow(owner_id, run.workflow_id, reason=reason)
            except Exception:
                pass

        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.CANCELLED.value,
                requires_approval=False,
                error=reason,
            )
            await session.commit()

        return OrchestrationExecuteResponse(
            run_id=str(run.id),
            status="cancelled",
            message=f"Run cancelled: {reason}",
            intent_type=run.intent_type,
            target=run.target_person,
        )

    async def trust_and_resume_run(
        self,
        owner_id: uuid.UUID,
        run_id: uuid.UUID,
        req: OrchestrationTrustRequest | None = None,
    ) -> OrchestrationExecuteResponse:
        """Explicitly trust a discovered agent and resume the SAME orchestration run."""
        async with self._session_factory() as session:
            run = await self._runs_repo.get_by_id(session, owner_id, run_id)
        if run is None:
            raise TargetResolutionError(f"Orchestration run {run_id} not found.")

        if run.state != OrchestrationState.WAITING_FOR_TRUST.value:
            raise OrchestrationError(f"Run {run_id} is not awaiting trust (current state: {run.state})")

        target_agent_id = run.target_agent_id
        target_name = run.target_person or "remote_agent"
        display_name = (req.display_name if req and req.display_name else None) or target_name

        # 1. Retrieve the discovered Agent Card or public metadata
        card = None
        if hasattr(self._target_resolver, "get_discovered_card"):
            card = self._target_resolver.get_discovered_card(target_agent_id)
            if inspect.isawaitable(card):
                card = await card

        public_key = None
        endpoint = "wss://gateway/ws"
        if card and isinstance(card, dict):
            public_key = card.get("public_key") or card.get("agent_id")
            endpoint = card.get("endpoint") or endpoint
            display_name = card.get("display_name") or display_name

        if not public_key:
            # Check contacts
            async with self._session_factory() as session:
                contacts = await self._contacts_repo.find_by_name_or_alias(session, owner_id, target_name)
                for c in contacts:
                    if c.agent_id == target_agent_id and c.endpoint:
                        endpoint = c.endpoint

        if not public_key and target_agent_id:
            # Check trusted_agents to see if already there
            async with self._session_factory() as session:
                existing = await self._trusted_repo.get(session, owner_id, target_agent_id)
                if existing:
                    public_key = existing.public_key
                    endpoint = existing.endpoint

        if not public_key:
            public_key = target_agent_id

        # 2. Persist trusted agent
        await self._a2a.register_trusted_agent(
            owner_id,
            agent_id=target_agent_id,
            public_key=public_key,
            display_name=display_name,
            endpoint=endpoint,
        )

        # 3. Update or create local contact
        async with self._session_factory() as session:
            existing_contacts = await self._contacts_repo.find_by_name_or_alias(session, owner_id, target_name)
            if existing_contacts:
                c = existing_contacts[0]
                c.agent_id = target_agent_id
                c.endpoint = endpoint
            else:
                await self._contacts_repo.create(
                    session,
                    owner_id=owner_id,
                    display_name=display_name,
                    aliases=[target_name],
                    agent_id=target_agent_id,
                    endpoint=endpoint,
                )
            await session.commit()

        # 4. Resume the SAME run from RESOLVING_TARGET state
        async with self._session_factory() as session:
            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.RESOLVING_TARGET.value,
                requires_approval=False,
            )
            await session.commit()

        # Reconstruct Intent and execute on the SAME run
        intent = await self._intent_resolver.resolve_intent(run.goal, active_target=target_name)
        return await self._execute_orchestration(owner_id, run.session_id, run, intent)

    async def handle_task_completion(
        self, task_id: str, response_payload: dict[str, Any]
    ) -> None:
        """Handle incoming response for a task associated with an orchestration run."""
        async with self._session_factory() as session:
            run = await self._runs_repo.find_by_task_id(session, task_id)
            if run is None or run.state != OrchestrationState.WAITING_REMOTE.value:
                return

            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.PROCESSING_RESULT.value,
            )
            await session.commit()

            target_name = run.target_person or "Remote agent"
            plan = OrchestrationPlan.model_validate(run.plan) if run.plan else OrchestrationPlan(goal=run.goal, intent_type=run.intent_type)
            formatted = self._executor._format_result(plan, target_name, [{"payload": response_payload, "status": "completed"}])

            await self._runs_repo.update_state(
                session,
                run,
                state=OrchestrationState.COMPLETED.value,
                result=formatted,
            )
            await session.commit()

        # Update context
        await self._context_mgr.update_target(run.owner_id, run.session_id, target_name, agent_id=run.target_agent_id)
        if formatted.get("alternatives"):
            await self._context_mgr.update_proposed_time(run.owner_id, run.session_id, formatted["alternatives"][0])
