"""The single authorization entry point.

Every side effect in Nexus must pass exactly one authorization question through
exactly one object. Before M5 that question was asked in several places with
slightly different shapes:

    ToolService.execute        EvaluationRequest(action="access_tool", ...)
    A2AService.handle_inbound  EvaluationRequest(action=<payload action>, ...)
    WorkflowService            EvaluationRequest(handler.get_evaluation_request())
    AutonomyService            DecisionEngine.evaluate(...) -> PolicyService
    OrchestrationExecutor      DecisionEngine + PolicyService, evaluated twice

Any new call site that forgot the argument order, or used a different action
slug for the same thing, silently got a different answer from the policy
engine - because policy matching is keyed on those exact slugs.

``Authorizer`` fixes the vocabulary and the shape:

    decision = await authorizer.check(ctx, Action.ACCESS_TOOL, category="chat", purpose="...")

It returns the engine's ``EvaluationResult`` unchanged (decision, matched rule,
disclosure scope), so nothing downstream loses information. It does not make
authorization decisions itself - that is the deterministic ``PolicyEngine``'s
job, and it stays the only thing that decides.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.policy.engine import EvaluationRequest, EvaluationResult
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService

logger = logging.getLogger("nexus.authz")


class Action:
    """The controlled action vocabulary.

    These strings are part of the policy contract: a stored rule matches on
    them, so a typo does not fail loudly - it silently matches nothing and the
    request falls through to the secure default (ASK). Centralising them here
    makes that class of bug impossible to introduce by hand.
    """

    READ_MEMORY = "read_memory"
    DISCLOSE_INFORMATION = "disclose_information"
    SEND_MESSAGE = "send_message"
    CREATE_EVENT = "create_event"
    MODIFY_EVENT = "modify_event"
    ACCESS_TOOL = "access_tool"
    EXECUTE_STEP = "execute_step"
    DELEGATE_TASK = "delegate_task"
    RUN_AUTONOMY = "run_autonomy"
    CUSTOM = "custom"

    ALL = frozenset(
        {
            READ_MEMORY,
            DISCLOSE_INFORMATION,
            SEND_MESSAGE,
            CREATE_EVENT,
            MODIFY_EVENT,
            ACCESS_TOOL,
            EXECUTE_STEP,
            DELEGATE_TASK,
            RUN_AUTONOMY,
            CUSTOM,
        }
    )


@dataclass(frozen=True)
class Requester:
    """Who is asking.

    ``agent_id`` is the cryptographically identified caller. ``SELF`` is used
    for actions the owner's own agent initiates without an external requester
    (a local tool call), which is a different trust position from a remote
    agent and therefore a different policy subject.
    """

    #: Sentinel for "the owner's own agent, acting locally".
    SELF = "nexus:self"

    agent_id: str

    @classmethod
    def remote(cls, agent_id: str) -> "Requester":
        return cls(agent_id=agent_id)

    @classmethod
    def local(cls) -> "Requester":
        return cls(agent_id=cls.SELF)


class Authorizer:
    """Wraps PolicyService so authorization has ONE call shape."""

    def __init__(self, policy_service: PolicyService) -> None:
        self._policy = policy_service

    @property
    def policy_service(self) -> PolicyService:
        return self._policy

    async def check(
        self,
        owner_id: uuid.UUID,
        requester: Requester,
        action: str,
        *,
        data_category: str,
        purpose: str,
        resource_id: str | None = None,
    ) -> EvaluationResult:
        """Authorize one action.

        Returns the engine's result. Callers MUST branch on
        ``result.decision``: ALLOW executes, ASK pauses for the owner, DENY
        refuses. There is deliberately no boolean helper that could be
        misread - the three-way decision is the point.
        """
        if action not in Action.ALL:
            # An unknown slug matches no stored rule and would silently degrade
            # to ASK. Failing loudly here turns a silent authorization
            # surprise into a startup/test failure.
            raise ValueError(
                f"Unknown action {action!r}; add it to app.authz.Action so the "
                "policy vocabulary stays closed."
            )
        request = EvaluationRequest(
            requester_agent_id=requester.agent_id,
            data_category=data_category,
            action=action,
            purpose=purpose,
            resource_id=resource_id,
        )
        return await self._policy.evaluate(owner_id, request)

    @staticmethod
    def allowed(result: EvaluationResult) -> bool:
        return result.decision is PolicyDecision.ALLOW

    @staticmethod
    def requires_approval(result: EvaluationResult) -> bool:
        return result.decision is PolicyDecision.ASK

    @staticmethod
    def denied(result: EvaluationResult) -> bool:
        return result.decision is PolicyDecision.DENY


__all__ = ["Action", "Authorizer", "Requester"]
