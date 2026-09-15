"""ToolService: the ONLY public path to tool execution (Part 5).

    Invocation
        -> validate tool name (strict pattern)
        -> resolve tool from the registry
        -> validate arguments against the tool's schema
        -> POLICY (Part 4): action=access_tool, category=tool.data_category,
           purpose=invocation.purpose, requester=<the agent itself>
        -> DENY -> rejected | ASK -> approval_required | ALLOW -> execute
        -> executor boundary (timeout, result size, exception isolation)
        -> audit record (every outcome)

The security invariant: there is no method on this class, the registry, or
any tool that executes without the policy check. The registry deliberately
has no execute() - callers cannot accidentally bypass authorization.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.policy.engine import EvaluationRequest
from app.policy.models import PolicyDecision
from app.policy.service import PolicyService
from app.tools.errors import ToolError, ToolErrorCode
from app.tools.executor import run_with_guards, validate_arguments
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.tools.registry import ToolRegistry, validate_tool_name
from app.tools.repository import ToolExecutionRepository
from app.tools.schemas import (
    ToolContext,
    ToolErrorInfo,
    ToolInvocation,
    ToolResult,
)

logger = logging.getLogger("nexus.tools.service")

#: The requester identity for self-initiated tool calls is the Nexus agent
#: itself (Part 6 A2A will introduce external requester identities).
SELF_REQUESTER = "nexus:self"


class ToolService:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy_service: PolicyService,
        session_factory: async_sessionmaker[AsyncSession],
        timeout_seconds: float = 10.0,
        max_result_bytes: int = 65_536,
    ) -> None:
        self._registry = registry
        self._policy = policy_service
        self._session_factory = session_factory
        self._repo = ToolExecutionRepository()
        self._timeout = timeout_seconds
        self._max_result_bytes = max_result_bytes

    # --- Discovery (no policy needed: metadata is public capability info) ---

    def list_tools(self) -> list[dict[str, Any]]:
        return self._registry.list_tools()

    def get_tool(self, name: str) -> dict[str, Any] | None:
        tool = self._registry.get(name)
        return tool.metadata() if tool else None

    # --- Execution (policy-gated) ---------------------------------------------

    async def execute(
        self, owner_id: uuid.UUID, invocation: ToolInvocation
    ) -> ToolResult:
        try:
            validate_tool_name(invocation.tool_name)
        except ValueError as exc:
            return self._failure(
                invocation, ToolErrorCode.INVALID_TOOL_NAME, str(exc)
            )

        tool = self._registry.get(invocation.tool_name)
        if tool is None:
            return self._failure(
                invocation,
                ToolErrorCode.UNKNOWN_TOOL,
                f"Unknown tool {invocation.tool_name!r}.",
            )

        # Validate BEFORE policy so malformed requests never reach
        # authorization (and get audited as invalid, not denied).
        try:
            arguments = validate_arguments(tool, invocation.arguments)
        except ToolError as exc:
            return self._failure(invocation, exc.code, exc.message)

        request_id = invocation.request_id or f"req_{uuid.uuid4().hex[:16]}"

        # --- Policy gate (Part 4). Never skipped. ---
        policy_result = await self._policy.evaluate(
            owner_id,
            EvaluationRequest(
                requester_agent_id=SELF_REQUESTER,
                data_category=tool.data_category,
                action="access_tool",
                purpose=invocation.purpose,
            ),
        )

        if policy_result.decision is PolicyDecision.DENY:
            await self._audit(
                owner_id,
                request_id,
                invocation.tool_name,
                invocation.purpose,
                ToolExecutionStatus.DENIED,
                PolicyDecision.DENY,
                error_code=None,
            )
            return ToolResult(
                success=False,
                status="denied",
                tool_name=invocation.tool_name,
                request_id=request_id,
                error=ToolErrorInfo(
                    code=ToolErrorCode.DENIED.value,
                    message="Policy denied this tool execution.",
                ),
            )

        if policy_result.decision is PolicyDecision.ASK:
            await self._audit(
                owner_id,
                request_id,
                invocation.tool_name,
                invocation.purpose,
                ToolExecutionStatus.APPROVAL_REQUIRED,
                PolicyDecision.ASK,
                error_code=None,
            )
            return ToolResult(
                success=False,
                status="approval_required",
                tool_name=invocation.tool_name,
                request_id=request_id,
                error=ToolErrorInfo(
                    code=ToolErrorCode.APPROVAL_REQUIRED.value,
                    message="No policy allows this tool execution; the owner "
                    "must approve it (create a policy or consent).",
                ),
            )

        # ALLOW: execute inside the guarded boundary, audited.
        context = ToolContext(
            owner_id=owner_id, request_id=request_id, purpose=invocation.purpose
        )
        try:
            data = await run_with_guards(
                tool,
                arguments,
                context,
                timeout_seconds=self._timeout,
                max_result_bytes=self._max_result_bytes,
            )
        except ToolError as exc:
            await self._audit(
                owner_id,
                request_id,
                invocation.tool_name,
                invocation.purpose,
                ToolExecutionStatus.FAILED,
                PolicyDecision.ALLOW,
                error_code=exc.code.value,
            )
            return self._failure_with_id(
                invocation.tool_name, request_id, exc.code, exc.message
            )

        await self._audit(
            owner_id,
            request_id,
            invocation.tool_name,
            invocation.purpose,
            ToolExecutionStatus.EXECUTED,
            PolicyDecision.ALLOW,
            error_code=None,
        )
        return ToolResult(
            success=True,
            status="executed",
            tool_name=invocation.tool_name,
            request_id=request_id,
            data=data,
        )

    # --- Audit -----------------------------------------------------------------

    async def _audit(
        self,
        owner_id: uuid.UUID,
        request_id: str,
        tool_name: str,
        purpose: str,
        status: ToolExecutionStatus,
        policy_decision: PolicyDecision,
        *,
        error_code: str | None,
    ) -> None:
        """Record the execution outcome. Metadata only - never arguments
        or tool output."""
        async with self._session_factory() as session:
            record = ToolExecutionRecord(
                owner_id=owner_id,
                request_id=request_id,
                tool_name=tool_name,
                purpose=purpose,
                status=status.value,
                policy_decision=policy_decision.value,
                error_code=error_code,
            )
            await self._repo.add(session, record)
            if status is ToolExecutionStatus.EXECUTED:
                await self._repo.mark_completed(session, record)
            await session.commit()
        logger.info(
            "tool_execution tool=%s purpose=%s policy=%s status=%s",
            tool_name,
            purpose,
            policy_decision.value,
            status.value,
        )

    async def list_audit(
        self, owner_id: uuid.UUID, *, limit: int = 100
    ) -> list[ToolExecutionRecord]:
        async with self._session_factory() as session:
            return await self._repo.list_for_owner(session, owner_id, limit=limit)

    # --- Helpers ------------------------------------------------------------------

    @staticmethod
    def _failure(
        invocation: ToolInvocation, code: ToolErrorCode, message: str
    ) -> ToolResult:
        request_id = invocation.request_id or f"req_{uuid.uuid4().hex[:16]}"
        return ToolService._failure_with_id(
            invocation.tool_name, request_id, code, message
        )

    @staticmethod
    def _failure_with_id(
        tool_name: str, request_id: str, code: ToolErrorCode, message: str
    ) -> ToolResult:
        return ToolResult(
            success=False,
            status="failed",
            tool_name=tool_name,
            request_id=request_id,
            error=ToolErrorInfo(code=code.value, message=message),
        )


__all__ = ["SELF_REQUESTER", "ToolService"]
