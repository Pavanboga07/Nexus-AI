"""Agent Orchestrator: Central coordinator for Natural Language Agent Orchestration.

Orchestrates:
    User Message
        -> Context Reference Resolution (him, it, them)
        -> Intent Resolution
        -> Target Resolution
        -> Trust Verification / Bootstrap
        -> Bounded Planning
        -> Authoritative Policy Execution
        -> Human-friendly Natural Language Summary
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from app.orchestration.repository import OrchestrationRunRepository
from app.orchestration.schemas import (
    Intent,
    IntentType,
    OrchestrationExecuteResponse,
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
    ) -> None:
        self._session_factory = session_factory
        self._a2a = a2a_service
        self._policy = policy_service
        self._intent_resolver = intent_resolver
        self._target_resolver = target_resolver
        self._decision_engine = decision_engine
        self._context_mgr = context_manager or OrchestrationContextManager()
        self._planner = planner or OrchestrationPlanner()
        self._executor = executor or OrchestrationExecutor(
            a2a_service=a2a_service,
            policy_service=policy_service,
            decision_engine=decision_engine,
        )
        self._runs_repo = OrchestrationRunRepository()

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
        resolved_msg, active_target = self._context_mgr.resolve_references(session_id, message)

        # 2. Extract structured Intent
        intent = await self._intent_resolver.resolve_intent(
            resolved_msg,
            active_target=active_target,
        )

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
            ctx = self._context_mgr.get_context(session_id)
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

        if target_res.status == TargetResolutionStatus.DISCOVERED_AGENT:
            # Trust Bootstrap flow (Part G)
            async with self._session_factory() as session:
                await self._runs_repo.update_state(
                    session,
                    run,
                    state=OrchestrationState.WAITING_FOR_TRUST.value,
                    target_agent_id=target_res.agent_id,
                )
                await session.commit()
            return OrchestrationExecuteResponse(
                run_id=str(run.id),
                status="waiting_trust",
                message=f"I found {target_name}'s Nexus agent, but you haven't trusted it yet. Would you like to connect with {target_name}'s agent?",
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
        self._context_mgr.update_target(
            session_id, target_name, agent_id=target_res.agent_id
        )
        if result.get("alternatives"):
            # If alternatives were proposed, remember the first slot for "book it"
            self._context_mgr.update_proposed_time(
                session_id, result["alternatives"][0]
            )

        # 6. Mark completed
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
