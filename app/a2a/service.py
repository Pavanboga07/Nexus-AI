"""A2AService: orchestration for trusted agents, inbound and outbound (Part 6).

Inbound (POST /a2a/messages), in strict order:

    size limit -> schema validation -> recipient check -> rate limit
    -> trusted-agent lookup (unknown/revoked -> reject)
    -> agent_id <-> public key consistency
    -> Ed25519 signature verification
    -> time window (expiry / clock skew)
    -> replay protection (atomic unique insert)
    -> POLICY (Part 4)                      [never skipped]
    -> memory retrieval (only on ALLOW, category-scoped)
    -> minimum disclosure shaping
    -> signed response

Outbound (POST /a2a/send):

    validate recipient is trusted+active -> build task -> canonical message
    -> sign with local identity -> transport (SSRF-guarded) -> verify the
    response (sender, agent_id<->key, signature, task_id, type) -> return.

Authentication ≠ authorization: a perfectly valid signed message from a
trusted agent still gets evaluated by policy, and DENY/ASK return no data.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a import signing
from app.a2a.disclosure import build_disclosure
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.handlers import (
    TaskContext,
    TaskHandlerRegistry,
    create_default_task_registry,
)
from app.a2a.models import (
    A2AMessageRecord,
    A2ATask,
    TaskStatus,
    TrustedAgent,
    TrustStatus,
)
from app.a2a.negotiation import (
    validate_negotiation_round,
    validate_task_active,
)
from app.a2a.rate_limit import RateLimiter
from app.a2a.replay import validate_time_window
from app.a2a.repository import (
    MessageRecordRepository,
    TaskRepository,
    TrustedAgentRepository,
)
from app.a2a.schemas import (
    A2AEnvelope,
    new_message_id,
    new_task_id,
    parse_iso,
    utc_iso_in,
    utc_now_iso,
    validate_request_payload,
)
from app.a2a.transport import A2ATransport, validate_endpoint
from app.identity.service import IdentityService
from app.memory.manager import MemoryManager
from app.policy.engine import EvaluationRequest
from app.policy.models import DisclosureScope, PolicyDecision
from app.policy.service import PolicyService

logger = logging.getLogger("nexus.a2a")


class A2AService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        identity_service: IdentityService,
        policy_service: PolicyService,
        memory_manager: MemoryManager | None,
        transport: A2ATransport,
        rate_limiter: RateLimiter,
        tool_service: Any = None,
        task_registry: TaskHandlerRegistry | None = None,
        max_negotiation_rounds: int = 3,
        task_ttl_seconds: float = 3600.0,
        max_message_bytes: int = 65_536,
        max_clock_skew_seconds: float = 30.0,
        message_ttl_seconds: float = 60.0,
        allow_local_endpoints: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._identity = identity_service
        self._policy = policy_service
        self._memory = memory_manager
        self._transport = transport
        self._rate_limiter = rate_limiter
        self._tool_service = tool_service
        self._task_registry = task_registry or create_default_task_registry()
        self._max_negotiation_rounds = max_negotiation_rounds
        self._task_ttl = task_ttl_seconds
        self._max_message_bytes = max_message_bytes
        self._max_clock_skew = max_clock_skew_seconds
        self._message_ttl = message_ttl_seconds
        self._allow_local = allow_local_endpoints

        self._trusted = TrustedAgentRepository()
        self._tasks = TaskRepository()
        self._records = MessageRecordRepository()
        self._task_completion_callbacks: list[Any] = []

    def register_task_completion_callback(self, callback: Any) -> None:
        """Register an async callback (task_id, payload) invoked upon remote task response."""
        self._task_completion_callbacks.append(callback)

    # ------------------------------------------------------------------ utils

    def check_size(self, raw_body: bytes) -> None:
        if len(raw_body) > self._max_message_bytes:
            raise A2AError(
                A2AErrorCode.MESSAGE_TOO_LARGE,
                f"Message exceeds the {self._max_message_bytes}-byte limit.",
            )

    async def local_agent_id(self) -> str:
        return self._identity.get_public_identity().agent_id

    # ------------------------------------------------- trusted-agent registry

    async def register_trusted_agent(
        self,
        owner_id: uuid.UUID,
        *,
        agent_id: str,
        public_key: str,
        display_name: str,
        endpoint: str,
    ) -> TrustedAgent:
        """Register a remote agent after verifying agent_id <-> public_key.

        Trust = recognition of a cryptographic identity. It is NOT
        authorization: policy still gates every request.
        """
        if not signing.agent_id_matches_key(agent_id, public_key):
            raise A2AError(
                A2AErrorCode.IDENTITY_MISMATCH,
                "agent_id does not match the supplied public key.",
            )
        validate_endpoint(endpoint, allow_local=self._allow_local)

        async with self._session_factory() as session:
            existing = await self._trusted.get(session, owner_id, agent_id)
            if existing is not None:
                raise A2AError(
                    A2AErrorCode.CONFLICT,
                    "Agent is already registered for this owner.",
                )
            agent = TrustedAgent(
                owner_id=owner_id,
                agent_id=agent_id,
                public_key=public_key,
                display_name=display_name,
                endpoint=endpoint,
                status=TrustStatus.ACTIVE.value,
            )
            await self._trusted.add(session, agent)
            await session.commit()
            logger.info(
                "a2a_agent_registered agent_id=%s display_name=%s",
                agent_id,
                display_name,
            )
            return agent

    async def list_trusted_agents(self, owner_id: uuid.UUID) -> list[TrustedAgent]:
        async with self._session_factory() as session:
            return await self._trusted.list_active(session, owner_id)

    async def get_trusted_agent(
        self, owner_id: uuid.UUID, agent_id: str
    ) -> TrustedAgent | None:
        async with self._session_factory() as session:
            return await self._trusted.get(session, owner_id, agent_id)

    async def revoke_trusted_agent(
        self, owner_id: uuid.UUID, agent_id: str
    ) -> TrustedAgent | None:
        """Mark a trusted agent as revoked. Returns the updated record, or
        None when the agent is not registered for this owner."""
        async with self._session_factory() as session:
            agent = await self._trusted.set_status(
                session, owner_id, agent_id, TrustStatus.REVOKED
            )
            await session.commit()
        if agent is not None:
            logger.info("a2a_agent_revoked agent_id=%s", agent_id)
        return agent

    async def delete_trusted_agent(self, owner_id: uuid.UUID, agent_id: str) -> bool:
        async with self._session_factory() as session:
            deleted = await self._trusted.delete(session, owner_id, agent_id)
            await session.commit()
        return deleted

    # ------------------------------------------------------------- INBOUND

    async def handle_inbound_response(
        self, owner_id: uuid.UUID, envelope: A2AEnvelope
    ) -> A2AEnvelope | None:
        """Process and verify an inbound response for a previously sent task or request."""
        local_agent_id = await self.local_agent_id()

        if envelope.recipient != local_agent_id:
            raise A2AError(
                A2AErrorCode.NOT_ADDRESSED_TO_US,
                "Message is not addressed to this agent.",
            )

        # 1. Lookup local task
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, envelope.task_id)
        if task is None:
            logger.warning("Received response for unknown task_id: %s from %s", envelope.task_id, envelope.sender)
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                f"Task {envelope.task_id} not found locally.",
            )

        # 2. Check sender matches task recipient
        if envelope.sender != task.recipient_agent_id:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                f"Response sender {envelope.sender} does not match expected {task.recipient_agent_id}",
            )

        # 3. Verify sender trust and signature
        async with self._session_factory() as session:
            sender_record = await self._trusted.get(session, owner_id, envelope.sender)
        if sender_record is None:
            raise A2AError(
                A2AErrorCode.UNTRUSTED_SENDER,
                "Sender is not a registered trusted agent.",
            )
        if sender_record.status == TrustStatus.REVOKED.value:
            raise A2AError(
                A2AErrorCode.REVOKED_SENDER,
                "Sender's trust has been revoked.",
            )

        if not signing.agent_id_matches_key(envelope.sender, sender_record.public_key):
            raise A2AError(
                A2AErrorCode.IDENTITY_MISMATCH,
                "Registered key no longer matches the sender identity.",
            )
        if not signing.verify_envelope_signature(envelope, sender_record.public_key):
            raise A2AError(
                A2AErrorCode.INVALID_SIGNATURE,
                "Response signature verification failed.",
            )

        # 4. Time window validation
        validate_time_window(envelope, max_clock_skew_seconds=self._max_clock_skew)

        # 5. Replay protection
        async with self._session_factory() as session:
            record = A2AMessageRecord(
                owner_id=owner_id,
                message_id=envelope.message_id,
                task_id=envelope.task_id,
                sender_agent_id=envelope.sender,
                recipient_agent_id=envelope.recipient,
                message_type=envelope.message_type,
                purpose=envelope.purpose,
                status="received",
            )
            is_new = await self._records.try_record(session, record)
            if not is_new:
                logger.info("Duplicate response message %s for task %s (idempotent ignore)", envelope.message_id, envelope.task_id)
                return None
            await session.commit()

        # 6. Update local task status
        resp_status = (envelope.payload or {}).get("status")
        final_status = TaskStatus.COMPLETED
        if resp_status in {"rejected", "failed"}:
            final_status = TaskStatus.REJECTED

        async with self._session_factory() as session:
            task.status = final_status.value
            task.response_payload = envelope.payload
            task.completed_at = datetime.now(timezone.utc)
            await self._tasks.upsert(session, task)
            await session.commit()

        # 7. Notify callbacks (e.g. workflows and orchestration runs)
        for callback in self._task_completion_callbacks:
            try:
                await callback(envelope.task_id, envelope.payload)
            except Exception as exc:
                logger.error("Task completion callback error for %s: %s", envelope.task_id, exc)

        return None

    async def handle_inbound(self, owner_id: uuid.UUID, envelope: A2AEnvelope) -> A2AEnvelope | None:
        """Verify + authorize + process one inbound request; return a SIGNED
        response envelope. Raises A2AError for every rejection."""
        if envelope.message_type in {"response", "task_response"}:
            return await self.handle_inbound_response(owner_id, envelope)

        local_agent_id = await self.local_agent_id()

        # Recipient check: the envelope must be addressed to this agent.
        if envelope.recipient != local_agent_id:
            raise A2AError(
                A2AErrorCode.NOT_ADDRESSED_TO_US,
                "Message is not addressed to this agent.",
            )

        if envelope.message_type not in {"request", "task_request", "task_proposal"}:
            raise A2AError(
                A2AErrorCode.INVALID_ENVELOPE,
                "Only 'request', 'task_request', or 'task_proposal' messages are accepted on this endpoint.",
            )

        # Rate limit per sender.
        if not self._rate_limiter.check(envelope.sender):
            raise A2AError(
                A2AErrorCode.RATE_LIMITED,
                "Too many requests from this agent; slow down.",
            )

        # Trust: unknown -> reject; revoked -> reject.
        async with self._session_factory() as session:
            sender_record = await self._trusted.get(
                session, owner_id, envelope.sender
            )
        if sender_record is None:
            raise A2AError(
                A2AErrorCode.UNTRUSTED_SENDER,
                "Sender is not a registered trusted agent.",
            )
        if sender_record.status == TrustStatus.REVOKED.value:
            raise A2AError(
                A2AErrorCode.REVOKED_SENDER,
                "Sender's trust has been revoked.",
            )

        # Identity: claimed agent_id must match the REGISTERED key, and the
        # signature must verify under that same key.
        if not signing.agent_id_matches_key(envelope.sender, sender_record.public_key):
            raise A2AError(
                A2AErrorCode.IDENTITY_MISMATCH,
                "Registered key no longer matches the sender identity.",
            )
        if not signing.verify_envelope_signature(envelope, sender_record.public_key):
            raise A2AError(
                A2AErrorCode.INVALID_SIGNATURE,
                "Signature verification failed.",
            )

        # Time window + replay.
        validate_time_window(
            envelope, max_clock_skew_seconds=self._max_clock_skew
        )
        async with self._session_factory() as session:
            record = A2AMessageRecord(
                owner_id=owner_id,
                message_id=envelope.message_id,
                task_id=envelope.task_id,
                sender_agent_id=envelope.sender,
                recipient_agent_id=envelope.recipient,
                message_type=envelope.message_type,
                purpose=envelope.purpose,
                status="received",
            )
            if not await self._records.try_record(session, record):
                raise A2AError(
                    A2AErrorCode.REPLAY,
                    "Duplicate message (replay protection).",
                )
            await session.commit()

        # ---- Route between standard Part 6 request and Part 8 task delegation
        if envelope.message_type in {"task_request", "task_proposal"}:
            return await self._handle_inbound_task(owner_id, envelope, local_agent_id)

        # ---- Standard Part 6 request flow
        try:
            action, data_category = validate_request_payload(envelope.payload)
        except ValueError as exc:
            await self._finalize(owner_id, envelope, "failed", None, "INVALID_ENVELOPE")
            raise A2AError(A2AErrorCode.INVALID_ENVELOPE, str(exc)) from None

        policy_result = await self._policy.evaluate(
            owner_id,
            EvaluationRequest(
                requester_agent_id=envelope.sender,
                data_category=data_category,
                action=action,
                purpose=envelope.purpose,
            ),
        )

        if policy_result.decision is PolicyDecision.DENY:
            await self._finalize(
                owner_id, envelope, "rejected", "DENY", None
            )
            return await self._sign_response(
                envelope,
                TaskStatus.REJECTED,
                {"status": "rejected", "reason": "policy_denied"},
            )

        if policy_result.decision is PolicyDecision.ASK:
            await self._finalize(
                owner_id, envelope, "approval_required", "ASK", None
            )
            return await self._sign_response(
                envelope,
                TaskStatus.PENDING,
                {"status": "approval_required"},
            )

        memories: list[str] = []
        if self._memory is not None:
            found = await self._memory.get_relevant_memories(
                owner_id, f"{data_category}", limit=3
            )
            memories = [m.content for m in found]

        disclosure = build_disclosure(
            scope=DisclosureScope(policy_result.disclosure_scope.value)
            if policy_result.disclosure_scope
            else DisclosureScope.SUMMARY,
            data_category=data_category,
            memories=memories,
        )
        await self._finalize(owner_id, envelope, "accepted", "ALLOW", None)
        logger.info(
            "a2a_request_accepted sender=%s task=%s purpose=%s scope=%s",
            envelope.sender,
            envelope.task_id,
            envelope.purpose,
            policy_result.disclosure_scope,
        )
        return await self._sign_response(
            envelope, disclosure.task_status, disclosure.to_payload()
        )

    async def handle_inbound_response(
        self, owner_id: uuid.UUID, envelope_data: dict[str, Any] | A2AEnvelope
    ) -> dict[str, Any] | None:
        """Process an inbound response or task_response from a remote agent."""
        if isinstance(envelope_data, dict):
            try:
                envelope = A2AEnvelope.model_validate(envelope_data)
            except Exception as exc:
                logger.warning("Invalid envelope format for inbound response: %s", exc)
                return None
        else:
            envelope = envelope_data

        local_agent_id = await self.local_agent_id()
        if envelope.recipient != local_agent_id:
            logger.warning("Inbound response not addressed to us: %s", envelope.recipient)
            return None

        # Verify sender
        async with self._session_factory() as session:
            sender_record = await self._trusted.get(session, owner_id, envelope.sender)
        if sender_record is None:
            logger.warning("Inbound response from untrusted sender: %s", envelope.sender)
            return None

        if not signing.agent_id_matches_key(envelope.sender, sender_record.public_key):
            logger.warning("Sender key mismatch for: %s", envelope.sender)
            return None

        if not signing.verify_envelope_signature(envelope, sender_record.public_key):
            logger.warning("Signature verification failed on inbound response from %s", envelope.sender)
            return None

        # Correlate task in database
        task_id = envelope.task_id
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, task_id)
            if task is not None:
                await self._tasks.update_status(
                    session,
                    owner_id,
                    task_id,
                    status=TaskStatus.COMPLETED.value,
                    response_payload=envelope.payload,
                    completed_at=datetime.now(timezone.utc),
                )
                await session.commit()

        # Fire registered callbacks
        for cb in self._task_completion_callbacks:
            try:
                res = cb(task_id, envelope.payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.error("Error in task completion callback: %s", exc)

        return {"status": "completed", "task_id": task_id, "payload": envelope.payload}

    async def _handle_inbound_task(
        self, owner_id: uuid.UUID, envelope: A2AEnvelope, local_agent_id: str
    ) -> A2AEnvelope:
        """Inbound handler for task_request and task_proposal messages (Part 8)."""
        task_type = envelope.task_type or envelope.payload.get("task_type")
        if not task_type:
            await self._finalize(owner_id, envelope, "failed", None, "INVALID_ENVELOPE")
            raise A2AError(
                A2AErrorCode.INVALID_ENVELOPE,
                "Task message requires a 'task_type'.",
            )

        handler = self._task_registry.get(task_type)
        if handler is None:
            await self._finalize(owner_id, envelope, "failed", None, "UNSUPPORTED_TASK_TYPE")
            raise A2AError(
                A2AErrorCode.UNSUPPORTED_TASK_TYPE,
                f"Unsupported task type: {task_type}",
            )

        try:
            handler.validate(envelope.payload)
        except ValueError as exc:
            await self._finalize(owner_id, envelope, "failed", None, "INVALID_ENVELOPE")
            raise A2AError(A2AErrorCode.INVALID_ENVELOPE, str(exc)) from None

        async with self._session_factory() as session:
            existing_task = await self._tasks.get(session, owner_id, envelope.task_id)

        # Idempotency: if already completed and this is a re-sent task_request, return cached response
        if (
            existing_task is not None
            and existing_task.status == TaskStatus.COMPLETED.value
            and envelope.message_type == "task_request"
        ):
            await self._finalize(owner_id, envelope, "accepted", "ALLOW", None)
            return await self._sign_response(
                envelope,
                TaskStatus.COMPLETED,
                existing_task.response_payload or {"status": "completed"},
                message_type="task_response",
            )

        # Negotiation state validation
        if envelope.message_type == "task_proposal":
            if existing_task is None:
                await self._finalize(owner_id, envelope, "failed", None, "NOT_FOUND")
                raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found for proposal.")
            try:
                validate_task_active(existing_task)
                validate_negotiation_round(existing_task.negotiation_round, self._max_negotiation_rounds)
            except A2AError as exc:
                await self._finalize(owner_id, envelope, "failed", None, exc.code.value)
                raise

        # Policy evaluation: handler defaults or payload overrides
        data_category = envelope.payload.get("data_category") or handler.default_data_category
        action = envelope.payload.get("action") or handler.default_action

        policy_result = await self._policy.evaluate(
            owner_id,
            EvaluationRequest(
                requester_agent_id=envelope.sender,
                data_category=data_category,
                action=action,
                purpose=envelope.purpose,
            ),
        )

        round_num = (
            (existing_task.negotiation_round + 1)
            if (existing_task and envelope.message_type == "task_proposal")
            else 0
        )

        if policy_result.decision is PolicyDecision.DENY:
            await self._finalize(owner_id, envelope, "rejected", "DENY", None)
            async with self._session_factory() as session:
                await self._tasks.upsert(
                    session,
                    A2ATask(
                        owner_id=owner_id,
                        task_id=envelope.task_id,
                        sender_agent_id=envelope.sender,
                        recipient_agent_id=local_agent_id,
                        status=TaskStatus.REJECTED.value,
                        task_type=task_type,
                        purpose=envelope.purpose,
                        request_payload=envelope.payload,
                        failure_reason="policy_denied",
                        negotiation_round=round_num,
                    ),
                )
                await session.commit()
            return await self._sign_response(
                envelope,
                TaskStatus.REJECTED,
                {"status": "rejected", "reason": "policy_denied"},
                message_type="task_response",
            )

        if policy_result.decision is PolicyDecision.ASK:
            await self._finalize(owner_id, envelope, "approval_required", "ASK", None)
            async with self._session_factory() as session:
                await self._tasks.upsert(
                    session,
                    A2ATask(
                        owner_id=owner_id,
                        task_id=envelope.task_id,
                        sender_agent_id=envelope.sender,
                        recipient_agent_id=local_agent_id,
                        status=TaskStatus.PENDING_APPROVAL.value,
                        task_type=task_type,
                        purpose=envelope.purpose,
                        request_payload=envelope.payload,
                        expires_at=parse_iso(envelope.expires_at),
                        negotiation_round=round_num,
                    ),
                )
                await session.commit()
            return await self._sign_response(
                envelope,
                TaskStatus.PENDING_APPROVAL,
                {"status": "pending_approval", "task_id": envelope.task_id},
                message_type="task_response",
            )

        # Policy ALLOW: execute handler under restricted TaskContext
        context = TaskContext(
            owner_id=owner_id,
            requester_agent_id=envelope.sender,
            task_id=envelope.task_id,
            purpose=envelope.purpose,
            disclosure_scope=policy_result.disclosure_scope or DisclosureScope.SUMMARY,
            _memory_manager=self._memory,
            _tool_service=self._tool_service,
        )

        handler_result = await handler.execute(context, envelope.payload)

        final_status = TaskStatus.COMPLETED
        if handler_result.get("status") == "counter_proposal":
            final_status = TaskStatus.ACCEPTED
        elif handler_result.get("status") == "rejected":
            final_status = TaskStatus.REJECTED

        response_payload = {
            "status": final_status.value,
            **handler_result,
        }

        async with self._session_factory() as session:
            await self._tasks.upsert(
                session,
                A2ATask(
                    owner_id=owner_id,
                    task_id=envelope.task_id,
                    sender_agent_id=envelope.sender,
                    recipient_agent_id=local_agent_id,
                    status=final_status.value,
                    task_type=task_type,
                    purpose=envelope.purpose,
                    request_payload=envelope.payload,
                    response_payload=response_payload,
                    negotiation_round=round_num,
                    completed_at=datetime.now(timezone.utc) if final_status is TaskStatus.COMPLETED else None,
                ),
            )
            await session.commit()

        await self._finalize(owner_id, envelope, "accepted", "ALLOW", None)
        return await self._sign_response(
            envelope,
            final_status,
            response_payload,
            message_type="task_response",
        )

    async def _finalize(
        self,
        owner_id: uuid.UUID,
        envelope: A2AEnvelope,
        status: str,
        policy_decision: str | None,
        error_code: str | None,
    ) -> None:
        """Update the replay-record row (created during replay protection)
        with the final processing outcome. Metadata only."""
        async with self._session_factory() as session:
            record = await self._records.get_by_message_id(
                session, owner_id, envelope.message_id
            )
            if record is not None:
                await self._records.mark_processed(
                    session, record, status, policy_decision, error_code
                )
                await session.commit()

    async def _sign_response(
        self,
        request: A2AEnvelope,
        task_status: TaskStatus,
        payload: dict[str, Any],
        message_type: str = "response",
    ) -> A2AEnvelope:
        local_agent_id = await self.local_agent_id()
        response = A2AEnvelope(
            message_id=new_message_id(),
            task_id=request.task_id,
            sender=local_agent_id,
            recipient=request.sender,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(self._message_ttl),
            message_type=message_type,
            purpose=request.purpose,
            task_type=request.task_type,
            payload=payload,
        )
        return await signing.sign_envelope(self._identity, response)

    # ------------------------------------------------------------- OUTBOUND

    async def send_request(
        self,
        owner_id: uuid.UUID,
        *,
        recipient_agent_id: str,
        purpose: str,
        action: str,
        data_category: str,
        payload: dict[str, Any] | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Send a signed request to a trusted agent and verify its response.

        The endpoint comes from the trusted-agent record, not from the
        caller, unless explicitly overridden (still validated).
        """
        async with self._session_factory() as session:
            recipient = await self._trusted.get(
                session, owner_id, recipient_agent_id
            )
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
        validate_endpoint(target_endpoint, allow_local=self._allow_local)

        local_agent_id = await self.local_agent_id()
        task_id = new_task_id()
        full_payload: dict[str, Any] = {
            "action": action,
            "data_category": data_category,
            **(payload or {}),
        }

        request = A2AEnvelope(
            message_id=new_message_id(),
            task_id=task_id,
            sender=local_agent_id,
            recipient=recipient_agent_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(self._message_ttl),
            message_type="request",
            purpose=purpose,
            payload=full_payload,
        )
        signed_request = await signing.sign_envelope(self._identity, request)

        # Record the outbound task for correlation.
        async with self._session_factory() as session:
            await self._tasks.upsert(
                session,
                A2ATask(
                    owner_id=owner_id,
                    task_id=task_id,
                    sender_agent_id=local_agent_id,
                    recipient_agent_id=recipient_agent_id,
                    status=TaskStatus.PENDING.value,
                    expires_at=parse_iso(signed_request.expires_at),
                ),
            )
            await session.commit()

        logger.info(
            "a2a_request_sent recipient=%s task=%s purpose=%s",
            recipient_agent_id,
            task_id,
            purpose,
        )
        response_data = await self._transport.send(
            target_endpoint, signed_request.model_dump()
        )

        if isinstance(response_data, dict) and response_data.get("status") == "queued":
            async with self._session_factory() as session:
                await self._tasks.upsert(
                    session,
                    A2ATask(
                        owner_id=owner_id,
                        task_id=task_id,
                        sender_agent_id=local_agent_id,
                        recipient_agent_id=recipient_agent_id,
                        status=TaskStatus.WAITING_REMOTE.value,
                    ),
                )
                await session.commit()
            return {
                "task_id": task_id,
                "recipient": recipient_agent_id,
                "status": "queued",
                "relay_id": response_data.get("relay_id"),
            }

        # Verify the response envelope.
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
        if response.message_type != "response":
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Expected a response message.",
            )
        if not signing.verify_envelope_signature(
            response, recipient.public_key
        ):
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Response signature verification failed.",
            )

        status = TaskStatus.COMPLETED
        response_status = response.payload.get("status")
        if response_status == "rejected":
            status = TaskStatus.REJECTED
        elif response_status == "approval_required":
            status = TaskStatus.PENDING
        async with self._session_factory() as session:
            await self._tasks.upsert(
                session,
                A2ATask(
                    owner_id=owner_id,
                    task_id=task_id,
                    sender_agent_id=local_agent_id,
                    recipient_agent_id=recipient_agent_id,
                    status=status.value,
                ),
            )
            await session.commit()

        return {
            "task_id": task_id,
            "recipient": recipient_agent_id,
            "status": status.value,
            "payload": response.payload,
        }

    # --------------------------------------------------- TASK DELEGATION (Part 8)

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
        handler = self._task_registry.get(task_type)
        if handler is None:
            raise A2AError(
                A2AErrorCode.UNSUPPORTED_TASK_TYPE,
                f"Unsupported task type: {task_type}",
            )
        try:
            handler.validate(payload or {})
        except ValueError as exc:
            raise A2AError(A2AErrorCode.INVALID_ENVELOPE, str(exc)) from exc

        async with self._session_factory() as session:
            recipient = await self._trusted.get(session, owner_id, recipient_agent_id)
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
        validate_endpoint(target_endpoint, allow_local=self._allow_local)

        local_agent_id = await self.local_agent_id()
        task_id = new_task_id()
        expires_at_iso = utc_iso_in(self._task_ttl)

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
        )
        signed_request = await signing.sign_envelope(self._identity, request)

        # Record outbound task
        async with self._session_factory() as session:
            await self._tasks.upsert(
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
        response_data = await self._transport.send(
            target_endpoint, signed_request.model_dump()
        )

        if isinstance(response_data, dict) and response_data.get("status") == "queued":
            async with self._session_factory() as session:
                await self._tasks.upsert(
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

        async with self._session_factory() as session:
            await self._tasks.update_status(
                session,
                owner_id,
                task_id,
                status=final_status.value,
                response_payload=response.payload,
                completed_at=datetime.now(timezone.utc) if final_status is TaskStatus.COMPLETED else None,
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
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        validate_task_active(task)
        validate_negotiation_round(task.negotiation_round, self._max_negotiation_rounds)

        local_agent_id = await self.local_agent_id()
        recipient_id = (
            task.recipient_agent_id
            if task.sender_agent_id == local_agent_id
            else task.sender_agent_id
        )

        async with self._session_factory() as session:
            recipient = await self._trusted.get(session, owner_id, recipient_id)
        if recipient is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Remote agent is not trusted.")
        if recipient.status == TrustStatus.REVOKED.value:
            raise A2AError(A2AErrorCode.REVOKED_SENDER, "Remote agent trust revoked.")

        target_endpoint = recipient.endpoint
        validate_endpoint(target_endpoint, allow_local=self._allow_local)

        round_num = task.negotiation_round + 1
        request = A2AEnvelope(
            message_id=new_message_id(),
            task_id=task.task_id,
            sender=local_agent_id,
            recipient=recipient_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(self._task_ttl),
            message_type="task_proposal",
            task_type=task.task_type,
            purpose=purpose or task.purpose or "negotiation",
            payload=proposal_payload,
        )
        signed_request = await signing.sign_envelope(self._identity, request)

        response_data = await self._transport.send(target_endpoint, signed_request.model_dump())
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

        async with self._session_factory() as session:
            task.status = final_status.value
            task.response_payload = response.payload
            task.negotiation_round = round_num
            if final_status is TaskStatus.COMPLETED:
                task.completed_at = datetime.now(timezone.utc)
            await self._tasks.upsert(session, task)
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
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        if task.status != TaskStatus.PENDING_APPROVAL.value:
            raise A2AError(
                A2AErrorCode.TASK_NOT_PENDING,
                f"Task is in status '{task.status}', expected '{TaskStatus.PENDING_APPROVAL.value}'.",
            )

        validate_task_active(task)

        handler = self._task_registry.get(task.task_type or "")
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
            _memory_manager=self._memory,
            _tool_service=self._tool_service,
        )

        result_payload = await handler.execute(context, task.request_payload or {})

        async with self._session_factory() as session:
            updated = await self._tasks.update_status(
                session,
                owner_id,
                task_id,
                status=TaskStatus.COMPLETED.value,
                response_payload=result_payload,
                completed_at=datetime.now(timezone.utc),
            )
            await session.commit()
            return updated  # type: ignore[return-value]

    async def reject_task(
        self, owner_id: uuid.UUID, task_id: str, reason: str | None = None
    ) -> A2ATask:
        """Reject a task."""
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        async with self._session_factory() as session:
            updated = await self._tasks.update_status(
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
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, task_id)
        if task is None:
            raise A2AError(A2AErrorCode.NOT_FOUND, "Task not found.")

        async with self._session_factory() as session:
            updated = await self._tasks.update_status(
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
        async with self._session_factory() as session:
            return await self._tasks.list_for_owner(session, owner_id, status=status, limit=limit)

    async def get_task(
        self, owner_id: uuid.UUID, task_id: str
    ) -> A2ATask | None:
        async with self._session_factory() as session:
            return await self._tasks.get(session, owner_id, task_id)

    # ------------------------------------------------------------- AUDIT

    async def list_audit(
        self, owner_id: uuid.UUID, *, limit: int = 100
    ) -> list[A2AMessageRecord]:
        async with self._session_factory() as session:
            return await self._records.list_for_owner(session, owner_id, limit=limit)


__all__ = ["A2AService"]
