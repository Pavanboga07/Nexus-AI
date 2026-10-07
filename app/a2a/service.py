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

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a import signing
from app.a2a.capabilities import (
    CapabilityPayloadError,
    CapabilitySpec,
    validate_payload_against_schema,
    version_compatible,
)
from app.a2a.delegation import TaskDelegationService
from app.a2a.disclosure import build_disclosure
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.gateway_translate import new_correlation_id
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
    is_terminal_status,
    map_response_status,
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
from app.a2a.tracing import trace_context_for_outbound
from app.a2a.transport import A2ATransport, validate_endpoint
from app.config.settings import get_settings
from app.identity.service import IdentityService
from app.memory.manager import MemoryManager
from app.observability import A2A_OUTCOMES
from app.policy.engine import EvaluationRequest
from app.policy.models import DisclosureScope, PolicyDecision
from app.policy.service import PolicyService
from app.search import SearchError, get_provider_from_settings

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
        search_provider: Any = None,
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
        self._search_provider = search_provider
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
        #: Capability contracts this agent offers, keyed by capability id.
        #: Empty means "no capability contracts declared", in which case a 0.2
        #: request that names one is refused with UNSUPPORTED_CAPABILITY rather
        #: than being accepted unvalidated.
        self._capabilities: dict[str, CapabilitySpec] = {}
        self._register_default_capabilities()
        self._delegation = TaskDelegationService(self)

    def register_task_completion_callback(self, callback: Any) -> None:
        """Register an async callback (task_id, payload) invoked upon remote task response."""
        self._task_completion_callbacks.append(callback)

    # --- Capability contracts (M6) -------------------------------------------

    def register_capability(self, spec: CapabilitySpec) -> None:
        """Declare a capability this agent offers.

        Registration is explicit rather than derived, so what an agent
        advertises and what it will accept cannot diverge silently.
        """
        self._capabilities[spec.id] = spec
        logger.info(
            "capability_registered id=%s version=%s category=%s",
            spec.id,
            spec.version,
            spec.data_category,
        )

    @property
    def capabilities(self) -> dict[str, CapabilitySpec]:
        return dict(self._capabilities)

    def _register_default_capabilities(self) -> None:
        """Register contracts for the capabilities this agent actually serves.

        These mirror the task handlers: every handler that can execute a
        request gets a declared contract, so a caller can build a valid request
        instead of guessing the payload shape.
        """
        from app.a2a.capabilities import CapabilitySpec as _Spec

        defaults = [
            _Spec(
                id="calendar.availability",
                version="1.0",
                description="Check whether the owner is available at a time.",
                data_category="availability",
                input_schema={
                    "type": "object",
                    "properties": {
                        "requested_time": {"type": "string", "maxLength": 128},
                        "date": {"type": "string", "maxLength": 128},
                    },
                    "additionalProperties": True,
                },
                output_schema={"type": "object"},
            ),
            _Spec(
                id="calendar.propose_meeting",
                version="1.0",
                description="Propose a meeting time to the owner.",
                data_category="schedule",
                input_schema={
                    "type": "object",
                    "properties": {
                        "proposed_time": {"type": "string", "maxLength": 128},
                        "duration_minutes": {"type": "integer", "minimum": 5,
                                             "maximum": 480},
                    },
                    "additionalProperties": True,
                },
                output_schema={"type": "object"},
            ),
            _Spec(
                id="information.request",
                version="1.0",
                description="Request a policy-permitted piece of information.",
                data_category="preferences",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 500},
                    },
                    "additionalProperties": True,
                },
                output_schema={"type": "object"},
            ),
            _Spec(
                id="information.search",
                version="1.0",
                description="Search the public web for current or external information.",
                data_category="public-web",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": True,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "results": {"type": "array"},
                    },
                    "additionalProperties": True,
                },
            ),
        ]
        for spec in defaults:
            self._capabilities[spec.id] = spec

    # --- Public collaborators (M5) -------------------------------------------
    # ``TargetResolver`` and the discovery route used to reach through to
    # ``a2a_service._trusted`` and ``a2a_service._session_factory()``. Those are
    # implementation details; exposing them made the dependency invisible and
    # let a refactor break callers silently.

    @property
    def trusted_agents(self) -> TrustedAgentRepository:
        """Repository the service uses for the trusted-agent registry."""
        return self._trusted

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Session factory, for collaborators that must share this service's DB."""
        return self._session_factory

    async def list_trusted_agent_ids(self, owner_id: uuid.UUID) -> set[str]:
        """Active trusted-agent ids for an owner.

        The single supported way to ask "who do I already trust?", used by the
        directory route instead of poking at the repository.
        """
        async with self._session_factory() as session:
            from app.a2a.models import TrustStatus

            agents = await self._trusted.list_active(session, owner_id)
        return {
            a.agent_id for a in agents if a.status == TrustStatus.ACTIVE.value
        }

    # ------------------------------------------------------------------ utils

    def check_size(self, raw_body: bytes) -> None:
        if len(raw_body) > self._max_message_bytes:
            raise A2AError(
                A2AErrorCode.MESSAGE_TOO_LARGE,
                f"Message exceeds the {self._max_message_bytes}-byte limit.",
            )

    async def local_agent_id(self) -> str:
        """The local agent's current cryptographic id.

        The bound identity reports the id captured when the adapter was built;
        a key rotation changes the real agent_id, so this reflects the identity
        actually in use for signing.
        """
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

        # 2. Check sender is a party to the task. Normally this is the
        #    recipient we delegated to; it can also be the original sender
        #    when the "response" is the ack answering a one-way status
        #    notification (approval_granted/denied, task_cancel(led)) we sent.
        #    The signature is still verified against the sender's registered
        #    key below, so this widening grants nothing to a third party.
        if envelope.sender not in {task.sender_agent_id, task.recipient_agent_id}:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                f"Response sender {envelope.sender} is not a party to task {envelope.task_id}",
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

        # 4. Time window validation (expiry + clock skew). Responses are
        #    just as replayable as requests, so they get the same checks.
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

        # 6. Update local task status. Terminal-state guard: a late or
        #    duplicated response (new message_id, so it passes replay
        #    protection) must not resurrect a task that already reached a
        #    terminal state, nor clobber its payload/completed_at.
        if is_terminal_status(task.status):
            logger.info(
                "ignoring response for terminal task %s (status=%s)",
                envelope.task_id,
                task.status,
            )
            return None

        # Shared with delegate_task/negotiate_task (fast path): one mapping
        # table, so a late pending_approval can no longer mark the task
        # COMPLETED here while the fast path says PENDING_APPROVAL.
        final_status = map_response_status(
            envelope.payload, default=TaskStatus.COMPLETED
        )

        async with self._session_factory() as session:
            task.status = final_status.value
            task.response_payload = envelope.payload
            if final_status is TaskStatus.COMPLETED:
                task.completed_at = datetime.now(UTC)
            await self._tasks.upsert(session, task)
            await session.commit()

        # 7. Notify callbacks (e.g. workflows and orchestration runs).
        #    Callbacks may be sync or async; both are supported, and a
        #    failing callback must not corrupt the response handling above.
        await self._fire_completion_callbacks(envelope.task_id, envelope.payload)

        return None

    async def _fire_completion_callbacks(
        self, task_id: str, payload: dict[str, Any] | None
    ) -> None:
        """Wake every waiter registered for a task's terminal outcome."""
        for callback in self._task_completion_callbacks:
            try:
                result = callback(task_id, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error("Task completion callback error for %s: %s", task_id, exc)

    async def handle_inbound(
        self,
        owner_id: uuid.UUID,
        envelope: A2AEnvelope,
        *,
        direction: str = "direct",
    ) -> A2AEnvelope | None:
        """Verify + authorize + process one inbound request; return a SIGNED
        response envelope. Raises A2AError for every rejection.

        Records the outcome (M11). The label is the A2A error code rather than a
        boolean, because "inbound failures spiked" is not actionable while
        "`UNTRUSTED_SENDER` spiked" points straight at the cause. `direction`
        separates direct-HTTP arrivals from gateway relays, which is how a
        transport problem is told apart from a trust problem.
        """
        try:
            result = await self._handle_inbound(owner_id, envelope)
        except A2AError as exc:
            A2A_OUTCOMES.inc(
                1, direction=direction, outcome=f"rejected:{exc.code.value}"
            )
            raise
        except Exception:
            A2A_OUTCOMES.inc(1, direction=direction, outcome="error:unexpected")
            raise
        A2A_OUTCOMES.inc(1, direction=direction, outcome="accepted")
        return result

    async def _handle_inbound(self, owner_id: uuid.UUID, envelope: A2AEnvelope) -> A2AEnvelope | None:
        if envelope.message_type in {"response", "task_response"}:
            return await self.handle_inbound_response(owner_id, envelope)

        local_agent_id = await self.local_agent_id()

        # Recipient check: the envelope must be addressed to this agent.
        if envelope.recipient != local_agent_id:
            raise A2AError(
                A2AErrorCode.NOT_ADDRESSED_TO_US,
                "Message is not addressed to this agent.",
            )

        if envelope.message_type not in {
            "request",
            "task_request",
            "task_proposal",
            # Status updates for tasks we (co-)own: approval and cancel
            # notifications, answered with a signed ack, not a task execution.
            "approval_required",
            "approval_granted",
            "approval_denied",
            "task_cancel",
            "task_cancelled",
            # Informational / discovery vocabulary: accepted, then handled by
            # dedicated branches below (never silently rejected).
            "error",
            "task_progress",
            "capability_query",
            "capability_response",
        }:
            raise A2AError(
                A2AErrorCode.INVALID_ENVELOPE,
                "Only request/task_request/task_proposal, task status updates "
                "(approval_*/task_cancel(led)), and informational "
                "(error/task_progress/capability_*) messages are accepted on "
                "this endpoint.",
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

        # ---- Task status updates (approval / cancel notifications) -----------
        # Verified exactly like requests above (trust, signature, replay all
        # ran); they update the task row and fire completion callbacks, then
        # answer with a signed ack so the notifier's transport send() resolves.
        if envelope.message_type in {
            "approval_required",
            "approval_granted",
            "approval_denied",
            "task_cancel",
            "task_cancelled",
        }:
            return await self._handle_inbound_status_update(
                owner_id, envelope, local_agent_id
            )

        # ---- Capability discovery --------------------------------------------
        if envelope.message_type == "capability_query":
            return await self._handle_capability_query(owner_id, envelope)

        # ---- Informational: log, maybe stash, acknowledge --------------------
        if envelope.message_type == "task_progress":
            async with self._session_factory() as session:
                task = await self._tasks.get(session, owner_id, envelope.task_id)
                if task is not None and not is_terminal_status(task.status):
                    task.response_payload = envelope.payload
                    await session.commit()
            logger.info(
                "task_progress task=%s sender=%s payload_status=%s",
                envelope.task_id,
                envelope.sender,
                (envelope.payload or {}).get("status"),
            )
            return await self._sign_response(
                envelope,
                TaskStatus.COMPLETED,
                {"status": "acknowledged"},
                message_type="task_response",
            )

        if envelope.message_type in {"error", "capability_response"}:
            # Nothing to do locally beyond noting it; the ack keeps the
            # notifier's transport send() from hanging.
            logger.info(
                "inbound_%s task=%s sender=%s payload=%s",
                envelope.message_type,
                envelope.task_id,
                envelope.sender,
                envelope.payload,
            )
            return await self._sign_response(
                envelope,
                TaskStatus.COMPLETED,
                {"status": "acknowledged"},
                message_type="task_response",
            )

        # ---- Standard Part 6 request flow
        try:
            action, data_category = validate_request_payload(envelope.payload)
        except ValueError as exc:
            await self._finalize(owner_id, envelope, "failed", None, "INVALID_ENVELOPE")
            raise A2AError(A2AErrorCode.INVALID_ENVELOPE, str(exc)) from None

        # ---- Capability contract (0.2) --------------------------------------
        # A 0.2 request may name the capability it is invoking. When it does,
        # the declaration must be supported and the payload must satisfy its
        # input schema. Refusing explicitly is the point: before 0.2 there was
        # nothing to validate against, so a malformed request was either
        # accepted and mishandled or failed with a generic error.
        if envelope.capability is not None:
            spec = self._capabilities.get(envelope.capability.id)
            if spec is None:
                await self._finalize(
                    owner_id, envelope, "failed", None, "UNSUPPORTED_CAPABILITY"
                )
                raise A2AError(
                    A2AErrorCode.UNSUPPORTED_CAPABILITY,
                    f"This agent does not offer capability "
                    f"'{envelope.capability.id}'.",
                )
            if not version_compatible(envelope.capability.version, spec.version):
                await self._finalize(
                    owner_id, envelope, "failed", None, "UNSUPPORTED_CAPABILITY"
                )
                raise A2AError(
                    A2AErrorCode.UNSUPPORTED_CAPABILITY,
                    f"Capability '{spec.id}' is offered at version "
                    f"{spec.version}, which is not compatible with the "
                    f"requested {envelope.capability.version}.",
                )
            try:
                validate_payload_against_schema(
                    envelope.payload, spec.input_schema, path="payload"
                )
            except CapabilityPayloadError as exc:
                await self._finalize(
                    owner_id, envelope, "failed", None, "INVALID_ENVELOPE"
                )
                raise A2AError(A2AErrorCode.INVALID_ENVELOPE, str(exc)) from None
            # The capability's declared category is authoritative for policy:
            # a caller must not widen disclosure by naming a different category
            # in the payload than the capability advertises.
            if spec.data_category:
                data_category = spec.data_category

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

        if envelope.capability is not None and envelope.capability.id == "information.search":
            return await self._handle_information_search(owner_id, envelope)

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

    async def _handle_inbound_status_update(
        self, owner_id: uuid.UUID, envelope: A2AEnvelope, local_agent_id: str
    ) -> A2AEnvelope:
        """Apply an approval/cancel notification to our task row.

        Runs after the full verification pipeline (recipient, trust,
        signature, time window, replay). Updates the task, fires completion
        callbacks so waiting orchestrations wake, and returns a signed ack —
        the notifier's ``transport.send()`` awaits a response envelope, so
        this must never return None.
        """
        async with self._session_factory() as session:
            task = await self._tasks.get(session, owner_id, envelope.task_id)

        if task is None:
            logger.warning(
                "status update for unknown task %s from %s (type=%s)",
                envelope.task_id,
                envelope.sender,
                envelope.message_type,
            )
            return await self._sign_response(
                envelope,
                TaskStatus.PENDING,
                {"status": "acknowledged", "task_known": False},
                message_type="task_response",
            )

        if envelope.sender not in {task.sender_agent_id, task.recipient_agent_id}:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                f"Status update sender {envelope.sender} is not a party to task {envelope.task_id}",
            )

        if is_terminal_status(task.status):
            logger.info(
                "ignoring status update for terminal task %s (status=%s type=%s)",
                envelope.task_id,
                task.status,
                envelope.message_type,
            )
            return await self._sign_response(
                envelope,
                TaskStatus.COMPLETED,
                {"status": "acknowledged"},
                message_type="task_response",
            )

        message_type = envelope.message_type
        if message_type == "approval_granted":
            new_status = TaskStatus.COMPLETED
        elif message_type == "approval_denied":
            new_status = TaskStatus.REJECTED
        elif message_type == "approval_required":
            new_status = TaskStatus.PENDING_APPROVAL
        else:  # task_cancel / task_cancelled
            new_status = TaskStatus.CANCELLED

        payload = envelope.payload or {}
        failure_reason = payload.get("reason")
        async with self._session_factory() as session:
            task.status = new_status.value
            task.response_payload = payload
            if new_status in {TaskStatus.REJECTED, TaskStatus.CANCELLED}:
                task.failure_reason = (
                    failure_reason if isinstance(failure_reason, str) else None
                )
            if new_status is TaskStatus.COMPLETED:
                task.completed_at = datetime.now(UTC)
            await self._tasks.upsert(session, task)
            await session.commit()

        logger.info(
            "task status updated via %s task=%s new_status=%s",
            message_type,
            envelope.task_id,
            new_status.value,
        )
        await self._fire_completion_callbacks(envelope.task_id, payload)
        return await self._sign_response(
            envelope,
            TaskStatus.COMPLETED,
            {
                "status": "acknowledged",
                "task_id": envelope.task_id,
                "new_status": new_status.value,
            },
            message_type="task_response",
        )

    async def _handle_capability_query(
        self, owner_id: uuid.UUID, envelope: A2AEnvelope
    ) -> A2AEnvelope:
        """Answer a capability_query with this agent's capability catalogue."""
        catalogue = [
            spec.to_dict() for spec in self._capabilities.values()
        ]
        logger.info(
            "capability_query answered task=%s sender=%s capabilities=%d",
            envelope.task_id,
            envelope.sender,
            len(catalogue),
        )
        return await self._sign_response(
            envelope,
            TaskStatus.COMPLETED,
            {"status": "completed", "capabilities": catalogue},
            message_type="capability_response",
        )

    async def _handle_information_search(
        self, owner_id: uuid.UUID, envelope: A2AEnvelope
    ) -> A2AEnvelope:
        """Execute the ``information.search`` capability via the shared provider.

        Runs only after policy ALLOW, so the provider is never touched on
        DENY/ASK. The response carries results only, never memory.
        """
        payload = envelope.payload or {}
        query = payload.get("query", "")
        count = payload.get("count", 5)
        provider = (
            self._search_provider
            if self._search_provider is not None
            else get_provider_from_settings(get_settings())
        )
        try:
            results = await provider.search(query, count)
        except SearchError as exc:
            await self._finalize(owner_id, envelope, "failed", "ALLOW", None)
            return await self._sign_response(
                envelope,
                TaskStatus.FAILED,
                {"status": "failed", "reason": str(exc) or "search_failed"},
            )
        await self._finalize(owner_id, envelope, "accepted", "ALLOW", None)
        logger.info(
            "a2a_search_completed sender=%s task=%s hits=%d",
            envelope.sender,
            envelope.task_id,
            len(results),
        )
        return await self._sign_response(
            envelope,
            TaskStatus.COMPLETED,
            {
                "status": "completed",
                "results": [
                    {"title": r.title, "url": r.url, "snippet": r.snippet}
                    for r in results
                ],
            },
        )

    async def handle_gateway_delivery(
        self, owner_id: uuid.UUID, envelope_data: dict[str, Any] | A2AEnvelope
    ) -> A2AEnvelope | None:
        """Tolerant entry point for envelopes pushed by the Nexus Gateway.

        The gateway is an untrusted relay: anything on the wire may be
        malformed or hostile. Unlike the strict handlers (which raise
        ``A2AError`` so the HTTP layer can map it to a status code), this
        method never raises - a bad frame must not tear down the gateway
        connection or kill the reader loop.

        Requests are dispatched through :meth:`handle_inbound`, which returns
        the SIGNED response envelope that the gateway client relays back to
        the original sender. Responses are dispatched through
        :meth:`handle_inbound_response` (returning ``None``) and are consumed
        locally. Every verification step still runs exactly once; a rejection
        is logged and the frame is dropped.
        """
        envelope: A2AEnvelope
        if isinstance(envelope_data, dict):
            try:
                envelope = A2AEnvelope.model_validate(envelope_data)
            except Exception as exc:
                logger.warning("gateway_delivery_malformed_envelope detail=%s", exc)
                return None
        else:
            envelope = envelope_data

        try:
            # `direction="gateway"` so a relayed rejection is distinguishable
            # from a direct one: the same error code arriving over the gateway
            # and over HTTP points at different causes.
            return await self.handle_inbound(
                owner_id, envelope, direction="gateway"
            )
        except A2AError as exc:
            logger.warning(
                "gateway_delivery_rejected code=%s message=%s sender=%s",
                exc.code.value,
                exc.message,
                envelope.sender,
            )
            return None

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

        # Idempotency for retried proposals: a (task_id, round) pair already
        # processed returns the cached response without re-executing the
        # handler. The sender stamps the round in the payload (see
        # TaskDelegationService.negotiate_task); without the stamp a retried
        # proposal (new message_id) is indistinguishable from a new round and
        # would desync the round counter.
        if envelope.message_type == "task_proposal" and existing_task is not None:
            proposed_round = (envelope.payload or {}).get("a2a_negotiation_round")
            if (
                isinstance(proposed_round, int)
                and proposed_round <= (existing_task.negotiation_round or 0)
                and existing_task.response_payload
            ):
                logger.info(
                    "duplicate proposal round ignored task=%s round=%s",
                    envelope.task_id,
                    proposed_round,
                )
                await self._finalize(owner_id, envelope, "accepted", "ALLOW", None)
                return await self._sign_response(
                    envelope,
                    TaskStatus(existing_task.status),
                    existing_task.response_payload,
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

        try:
            handler_result = await handler.execute(context, envelope.payload)
        except Exception as exc:
            # Without this the sender hangs until its response timeout: answer
            # promptly with a signed failure instead, and always finalize the
            # message record below.
            logger.exception(
                "a2a handler failed task=%s type=%s", envelope.task_id, task_type
            )
            handler_result = {
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }

        final_status = TaskStatus.COMPLETED
        if handler_result.get("status") == "counter_proposal":
            final_status = TaskStatus.ACCEPTED
        elif handler_result.get("status") == "rejected":
            final_status = TaskStatus.REJECTED
        elif handler_result.get("status") == "failed":
            final_status = TaskStatus.FAILED

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
                    completed_at=datetime.now(UTC) if final_status is TaskStatus.COMPLETED else None,
                ),
            )
            await session.commit()

        await self._finalize(
            owner_id,
            envelope,
            "failed" if final_status is TaskStatus.FAILED else "accepted",
            "ALLOW",
            None,
        )
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
        message_id = new_message_id()
        response = A2AEnvelope(
            message_id=message_id,
            task_id=request.task_id,
            sender=local_agent_id,
            recipient=request.sender,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(self._message_ttl),
            message_type=message_type,  # type: ignore[arg-type]
            purpose=request.purpose,
            task_type=request.task_type,
            payload=payload,
            # Responses join the request's exchange; when the requester never
            # set one (0.1 peers), derive it deterministically so the gateway
            # can still group the exchange.
            correlation_id=request.correlation_id
            or new_correlation_id(request.task_id, message_id),
            reply_to=request.message_id,
            trace=trace_context_for_outbound(),
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
        message_id = new_message_id()
        full_payload: dict[str, Any] = {
            "action": action,
            "data_category": data_category,
            **(payload or {}),
        }

        request = A2AEnvelope(
            message_id=message_id,
            task_id=task_id,
            sender=local_agent_id,
            recipient=recipient_agent_id,
            timestamp=utc_now_iso(),
            expires_at=utc_iso_in(self._message_ttl),
            message_type="request",
            purpose=purpose,
            payload=full_payload,
            correlation_id=new_correlation_id(task_id, message_id),
            trace=trace_context_for_outbound(),
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
        try:
            response_data = await self._transport.send(
                target_endpoint, signed_request.model_dump()
            )
        except A2AError as exc:
            if exc.code is not A2AErrorCode.QUEUED:
                raise
            # Gateway accepted the envelope for an offline recipient.
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
            raise

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

    # --- Task delegation (see app.a2a.delegation) --------------------------------
    # delegate_task / negotiate_task / approve_task / reject_task / cancel_task /
    # list_tasks / get_task live on TaskDelegationService. These thin wrappers
    # keep the public surface stable for existing callers.

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
        return await self._delegation.delegate_task(
            owner_id,
            recipient_agent_id=recipient_agent_id,
            task_type=task_type,
            purpose=purpose,
            payload=payload,
            endpoint=endpoint,
        )

    async def negotiate_task(
        self,
        owner_id: uuid.UUID,
        *,
        task_id: str,
        proposal_payload: dict[str, Any],
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Submit a counter-proposal / next negotiation round for an existing task."""
        return await self._delegation.negotiate_task(
            owner_id,
            task_id=task_id,
            proposal_payload=proposal_payload,
            purpose=purpose,
        )

    async def approve_task(
        self, owner_id: uuid.UUID, task_id: str, notes: str | None = None
    ) -> A2ATask:
        """Manually approve a task in PENDING_APPROVAL and execute it."""
        return await self._delegation.approve_task(owner_id, task_id, notes=notes)

    async def reject_task(
        self, owner_id: uuid.UUID, task_id: str, reason: str | None = None
    ) -> A2ATask:
        """Reject a task."""
        return await self._delegation.reject_task(owner_id, task_id, reason=reason)

    async def cancel_task(self, owner_id: uuid.UUID, task_id: str) -> A2ATask:
        """Cancel an active task."""
        return await self._delegation.cancel_task(owner_id, task_id)

    async def list_tasks(
        self, owner_id: uuid.UUID, status: str | None = None, limit: int = 100
    ) -> list[A2ATask]:
        return await self._delegation.list_tasks(
            owner_id, status=status, limit=limit
        )

    async def get_task(self, owner_id: uuid.UUID, task_id: str) -> A2ATask | None:
        return await self._delegation.get_task(owner_id, task_id)

    # ------------------------------------------------------------- AUDIT

    async def list_audit(
        self, owner_id: uuid.UUID, *, limit: int = 100
    ) -> list[A2AMessageRecord]:
        async with self._session_factory() as session:
            return await self._records.list_for_owner(session, owner_id, limit=limit)


__all__ = ["A2AService"]
