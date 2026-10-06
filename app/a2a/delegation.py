"""Task delegation and multi-round negotiation (Part 6, M6).

Extracted from ``app.a2a.service.A2AService``: this module owns the
outbound-delegation lifecycle (delegate / negotiate / approve / reject /
cancel) plus task queries. It is constructed with the ``A2AService`` as its
collaborator and reaches the shared infrastructure (session factory,
repositories, identity, transport, policy) through it, so there is exactly
one place that wires those pieces together.

``A2AService`` keeps thin wrappers with the original public signatures, so
callers (routes, orchestration, autonomy, workflows) are unaffected.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.a2a import signing
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.handlers import TaskContext
from app.a2a.models import A2ATask, TaskStatus, TrustStatus
from app.a2a.negotiation import validate_negotiation_round, validate_task_active
from app.a2a.schemas import (
    A2AEnvelope,
    new_message_id,
    new_task_id,
    parse_iso,
    utc_iso_in,
    utc_now_iso,
)
from app.a2a.tracing import trace_context_for_outbound
from app.a2a.transport import validate_endpoint
from app.policy.models import DisclosureScope

if TYPE_CHECKING:
    from app.a2a.service import A2AService

logger = logging.getLogger("nexus.a2a.delegation")


class TaskDelegationService:
    """Owns the A2A task-delegation lifecycle; see module docstring."""

    def __init__(self, a2a: A2AService) -> None:
        self._a2a = a2a

    async def delegate_task(
        self,
        owner_id: uuid.UUID,
        *,
        recipient_agent_id: str,
        task_type: str,
        purpose: str,
        payload: dict[str, Any] | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Delegate a task to a trusted remote agent and await its response."""
        handler = self._a2a._task_registry.get(task_type)
        if handler is None:
            raise A2AError(
                A2AErrorCode.UNSUPPORTED_TASK_TYPE,
                f"Unsupported task type: {task_type}",
            )
        try:
            handler.validate(payload or {})
        except ValueError as exc:
            raise A2AError(A2AErrorCode.INVALID_ENVELOPE, str(exc)) from exc

        async with self._a2a._session_factory() as session:
            recipient = await self._a2a._trusted.get(session, owner_id, recipient_agent_id)
        if recipient is None:
            raise A2AError(
                A2AErrorCode.NOT_FOUND,
                "Recipient is not a registered trusted agent.",
            )
        if recipient.status == TrustStatus.REVOKED.value:
            raise A2AError(
                A2AErrorCode.REVOKED_SENDER,
                "Recipient trust has been revoked.",
            )

        target_endpoint = endpoint or recipient.endpoint
        validate_endpoint(target_endpoint, allow_local=self._a2a._allow_local)

        local_agent_id = await self._a2a.local_agent_id()
        task_id = new_task_id()
        expires_at_iso = utc_iso_in(self._a2a._task_ttl)

        request = A2AEnvelope(
            message_id=new_message_id(),
            task_id=task_id,
            sender=local_agent_id,
            recipient=recipient_agent_id,
            timestamp=utc_now_iso(),
            expires_at=expires_at_iso,
            message_type="task_request",
            task_type=task_type,
            purpose=purpose,
            payload=payload or {},
            trace=trace_context_for_outbound(),
        )
        signed_request = await signing.sign_envelope(self._a2a._identity, request)

        # Record outbound task
        async with self._a2a._session_factory() as session:
            await self._a2a._tasks.upsert(
                session,
                A2ATask(
                    owner_id=owner_id,
                    task_id=task_id,
                    sender_agent_id=local_agent_id,
                    recipient_agent_id=recipient_agent_id,
                    status=TaskStatus.PENDING.value,
                    task_type=task_type,
                    purpose=purpose,
                    request_payload=payload or {},
                    expires_at=parse_iso(expires_at_iso),
                ),
            )
            await session.commit()

        logger.info(
            "a2a_task_delegated recipient=%s task=%s type=%s purpose=%s",
            recipient_agent_id,
            task_id,
            task_type,
            purpose,
        )
        response_data = await self._a2a._transport.send(
            target_endpoint, signed_request.model_dump()
        )

        if isinstance(response_data, dict) and response_data.get("status") == "queued":
            async with self._a2a._session_factory() as session:
                await self._a2a._tasks.upsert(
                    session,
                    A2ATask(
                        owner_id=owner_id,
                        task_id=task_id,
                        sender_agent_id=local_agent_id,
                        recipient_agent_id=recipient_agent_id,
                        status=TaskStatus.WAITING_REMOTE.value,
                        task_type=task_type,
                        purpose=purpose,
                        request_payload=payload or {},
                        expires_at=parse_iso(expires_at_iso),
                    ),
                )
                await session.commit()
            return {
                "task_id": task_id,
                "recipient": recipient_agent_id,
                "status": "queued",
                "relay_id": response_data.get("relay_id"),
            }

        try:
            response = A2AEnvelope.model_validate(response_data)
        except Exception as exc:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Remote response failed schema validation.",
            ) from exc

        if response.sender != recipient_agent_id:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Response came from a different agent than expected.",
            )
        if response.task_id != task_id:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Response task_id does not match the request.",
            )
        if response.message_type not in {"task_response", "response"}:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Expected a task_response message.",
            )
        if not signing.verify_envelope_signature(response, recipient.public_key):
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Response signature verification failed.",
            )

        resp_status = response.payload.get("status")
        final_status = TaskStatus.COMPLETED
        if resp_status == "rejected":
            final_status = TaskStatus.REJECTED
        elif resp_status in {"pending_approval", "approval_required"}:
            final_status = TaskStatus.PENDING_APPROVAL
        elif resp_status == "counter_proposal":
            final_status = TaskStatus.ACCEPTED

        async with self._a2a._session_factory() as session:
            await self._a2a._tasks.update_status(
                session,
                owner_id,
                task_id,
                status=final_status.value,
                response_payload=response.payload,
                completed_at=datetime.now(UTC) if final_status is TaskStatus.COMPLETED else None,
            )
            await session.commit()

        return {
            "task_id": task_id,
            "recipient": recipient_agent_id,
            "status": final_status.value,
            "payload": response.payload,
        }

    async def negotiate_task(
        self,
        owner_id: uuid.UUID,
        *,
        task_id: str,
        proposal_payload: dict[str, Any],
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Submit a counter-proposal / next negotiation round for an existing task."""
        async with self._a2a._session_factory() as session:
            task = await self._a2a._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        validate_task_active(task)
        validate_negotiation_round(task.negotiation_round, self._a2a._max_negotiation_rounds)

        local_agent_id = await self._a2a.local_agent_id()
        recipient_id = (
            task.recipient_agent_id
            if task.sender_agent_id == local_agent_id
            else task.sender_agent_id
        )

        async with self._a2a._session_factory() as session:
            recipient = await self._a2a._trusted.get(session, owner_id, recipient_id)
        if recipient is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Remote agent is not trusted.")
        if recipient.status == TrustStatus.REVOKED.value:
            raise A2AError(A2AErrorCode.REVOKED_SENDER, "Remote agent trust revoked.")

        target_endpoint = recipient.endpoint
        validate_endpoint(target_endpoint, allow_local=self._a2a._allow_local)

        round_num = task.negotiation_round + 1
        request = A2AEnvelope(
            message_id=new_message_id(),
            task_id=task.task_id,
            sender=local_agent_id,
            recipient=recipient_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(self._a2a._task_ttl),
            message_type="task_proposal",
            task_type=task.task_type,
            purpose=purpose or task.purpose or "negotiation",
            payload=proposal_payload,
            trace=trace_context_for_outbound(),
        )
        signed_request = await signing.sign_envelope(self._a2a._identity, request)

        response_data = await self._a2a._transport.send(target_endpoint, signed_request.model_dump())
        try:
            response = A2AEnvelope.model_validate(response_data)
        except Exception as exc:
            raise A2AError(A2AErrorCode.INVALID_RESPONSE, "Invalid response schema.") from exc

        if response.sender != recipient_id or response.task_id != task.task_id:
            raise A2AError(A2AErrorCode.INVALID_RESPONSE, "Mismatched response.")
        if not signing.verify_envelope_signature(response, recipient.public_key):
            raise A2AError(A2AErrorCode.INVALID_RESPONSE, "Response signature invalid.")

        resp_status = response.payload.get("status")
        final_status = TaskStatus.ACCEPTED
        if resp_status in {"completed", "accepted"}:
            final_status = TaskStatus.COMPLETED
        elif resp_status == "rejected":
            final_status = TaskStatus.REJECTED
        elif resp_status in {"pending_approval", "approval_required"}:
            final_status = TaskStatus.PENDING_APPROVAL
        elif resp_status == "counter_proposal":
            final_status = TaskStatus.ACCEPTED

        async with self._a2a._session_factory() as session:
            task.status = final_status.value
            task.response_payload = response.payload
            task.negotiation_round = round_num
            if final_status is TaskStatus.COMPLETED:
                task.completed_at = datetime.now(UTC)
            await self._a2a._tasks.upsert(session, task)
            await session.commit()

        return {
            "task_id": task_id,
            "recipient": recipient_id,
            "status": final_status.value,
            "negotiation_round": round_num,
            "payload": response.payload,
        }

    async def approve_task(
        self, owner_id: uuid.UUID, task_id: str, notes: str | None = None
    ) -> A2ATask:
        """Manually approve a task in PENDING_APPROVAL and execute it."""
        async with self._a2a._session_factory() as session:
            task = await self._a2a._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        if task.status != TaskStatus.PENDING_APPROVAL.value:
            raise A2AError(
                A2AErrorCode.TASK_NOT_PENDING,
                f"Task is in status '{task.status}', expected '{TaskStatus.PENDING_APPROVAL.value}'.",
            )

        validate_task_active(task)

        handler = self._a2a._task_registry.get(task.task_type or "")
        if handler is None:
            raise A2AError(
                A2AErrorCode.UNSUPPORTED_TASK_TYPE,
                f"Unsupported task type: {task.task_type}",
            )

        context = TaskContext(
            owner_id=owner_id,
            requester_agent_id=task.sender_agent_id,
            task_id=task.task_id,
            purpose=task.purpose or "delegation",
            disclosure_scope=DisclosureScope.EXACT,
            _memory_manager=self._a2a._memory,
            _tool_service=self._a2a._tool_service,
        )

        result_payload = await handler.execute(context, task.request_payload or {})

        async with self._a2a._session_factory() as session:
            updated = await self._a2a._tasks.update_status(
                session,
                owner_id,
                task_id,
                status=TaskStatus.COMPLETED.value,
                response_payload=result_payload,
                completed_at=datetime.now(UTC),
            )
            await session.commit()
            return updated  # type: ignore[return-value]

    async def reject_task(
        self, owner_id: uuid.UUID, task_id: str, reason: str | None = None
    ) -> A2ATask:
        """Reject a task."""
        async with self._a2a._session_factory() as session:
            task = await self._a2a._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        async with self._a2a._session_factory() as session:
            updated = await self._a2a._tasks.update_status(
                session,
                owner_id,
                task_id,
                status=TaskStatus.REJECTED.value,
                failure_reason=reason or "Rejected by owner",
            )
            await session.commit()
            return updated  # type: ignore[return-value]

    async def cancel_task(
        self, owner_id: uuid.UUID, task_id: str
    ) -> A2ATask:
        """Cancel an active task."""
        async with self._a2a._session_factory() as session:
            task = await self._a2a._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        async with self._a2a._session_factory() as session:
            updated = await self._a2a._tasks.update_status(
                session,
                owner_id,
                task_id,
                status=TaskStatus.CANCELLED.value,
                failure_reason="Cancelled by owner",
            )
            await session.commit()
            return updated  # type: ignore[return-value]

    async def list_tasks(
        self, owner_id: uuid.UUID, status: str | None = None, limit: int = 100
    ) -> list[A2ATask]:
        async with self._a2a._session_factory() as session:
            return await self._a2a._tasks.list_for_owner(session, owner_id, status=status, limit=limit)

    async def get_task(
        self, owner_id: uuid.UUID, task_id: str
    ) -> A2ATask | None:
        async with self._a2a._session_factory() as session:
            return await self._a2a._tasks.get(session, owner_id, task_id)


__all__ = ["TaskDelegationService"]
