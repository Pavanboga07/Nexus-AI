"""IdentityService: the app-facing identity API.

Two responsibilities, deliberately separated:

    AgentRegistry   which agents exist for an owner (rows, status, naming)
    IdentityService cryptographic operations for ONE agent

The M4 change
-------------
Before M4 this service bound itself to an ``owner_id`` in the constructor and
cached the single decrypted private key in ``self._private_key``. Combined with
``UniqueConstraint("owner_id")`` that made identity a process-global singleton:
one agent per deployment, resolved at startup.

Now every operation names the agent it applies to. Key material is loaded
**per operation** rather than held in process state, so:

  * many agents can be served by one process,
  * key rotation takes effect immediately (no stale cached key), and
  * a revoked key cannot keep signing because it was cached at startup.

Loading a key costs one indexed query plus an AES-GCM decrypt (~microseconds),
which is negligible next to the network calls a signature accompanies.
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity import crypto
from app.identity.models import (
    ACTIVE,
    PAUSED,
    REVOKED,
    Agent,
    AgentKey,
)
from app.identity.serialization import canonical_json_bytes

logger = logging.getLogger("nexus.identity")


@dataclass(frozen=True)
class PublicIdentity:
    """Safe-to-share identity representation. NEVER contains private material."""

    agent_id: str
    public_key: str  # base64
    key_algorithm: str
    fingerprint: str


class IdentityNotReadyError(RuntimeError):
    """Raised when signing is requested before an identity is loaded."""


class IdentityCorruptionError(RuntimeError):
    """Raised when a stored identity fails to decrypt or self-verify.

    Startup must abort: silently regenerating the keypair would break every
    future trust relationship (spec §12)."""


class AgentNotFoundError(IdentityNotReadyError):
    """Raised when an agent row does not exist for the given owner.

    Subclasses IdentityNotReadyError so "nothing to sign with" is ONE
    catchable condition for callers, whether the cause is an unbound service,
    a missing agent, or a revoked one.
    """


class AgentNotUsableError(IdentityNotReadyError):
    """Raised when an agent exists but may not sign (paused/revoked)."""


@dataclass(frozen=True)
class AgentSummary:
    """Registry view of an agent (no key material)."""

    id: uuid.UUID
    agent_id: str
    display_name: str
    handle: str | None
    endpoint: str | None
    status: str
    is_primary: bool
    created_at: datetime | None


class IdentityService:
    """Cryptographic identity operations, scoped to one agent per call."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        encryption_secret: str | None,
        owner_id: uuid.UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret = encryption_secret
        #: Retained for back-compat with pre-M4 callers that bound the service
        #: to a single owner. New code passes the agent explicitly.
        self._owner_id = owner_id
        #: Identity snapshot for the primary agent, populated by
        #: ``initialize_identity`` / ``initialize_primary_agent``. This exists
        #: ONLY for the synchronous back-compat accessor; per-agent operations
        #: read the current key every time.
        self._primary_public: PublicIdentity | None = None
        self._primary_agent_row_id: uuid.UUID | None = None

    # --- Readiness -----------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True when the service can perform cryptography at all.

        Note this no longer means "an identity is loaded": identities are
        loaded per call. It means a secret is configured.
        """
        return bool(self._secret)

    # --- Agent registry ------------------------------------------------------

    async def list_agents(self, owner_id: uuid.UUID) -> list[AgentSummary]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Agent)
                    .where(Agent.owner_id == owner_id)
                    .order_by(Agent.is_primary.desc(), Agent.created_at)
                )
            ).scalars().all()
            return [self._summarise(a) for a in rows]

    async def get_agent(self, owner_id: uuid.UUID, agent_row_id: uuid.UUID) -> AgentSummary:
        async with self._session_factory() as session:
            row = await self._require_agent(session, owner_id, agent_row_id)
            return self._summarise(row)

    async def get_primary_agent(self, owner_id: uuid.UUID) -> AgentSummary | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Agent)
                    .where(Agent.owner_id == owner_id)
                    .order_by(Agent.is_primary.desc(), Agent.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._summarise(row) if row else None

    async def create_agent(
        self,
        owner_id: uuid.UUID,
        *,
        display_name: str,
        handle: str | None = None,
        endpoint: str | None = None,
        make_primary: bool = False,
    ) -> AgentSummary:
        """Create an agent with a brand-new Ed25519 keypair.

        Raises IdentityCorruptionError when no encryption secret is configured,
        because an unencryptable private key must never be written.
        """
        if not self._secret:
            raise IdentityCorruptionError(
                "Cannot create an agent: NEXUS_IDENTITY_KEY is not set. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )

        clean_handle = (handle or "").strip().lstrip("@").lower() or None

        async with self._session_factory() as session:
            existing_count = len(
                (
                    await session.execute(
                        select(Agent.id).where(Agent.owner_id == owner_id)
                    )
                ).all()
            )
            agent = Agent(
                owner_id=owner_id,
                # Placeholder: replaced below once the key (and therefore the
                # cryptographic id) exists. Kept non-null to satisfy the schema.
                agent_id=f"pending:{uuid.uuid4().hex}",
                display_name=display_name,
                handle=clean_handle,
                endpoint=endpoint,
                status=ACTIVE,
                is_primary=make_primary or existing_count == 0,
            )
            session.add(agent)
            await session.flush()

            key = self._build_key(agent, session_owner_id=owner_id)
            session.add(key)
            await session.flush()

            agent.agent_id = key.agent_id
            if agent.is_primary:
                # Exactly one primary per owner.
                for other in (
                    await session.execute(
                        select(Agent).where(
                            Agent.owner_id == owner_id,
                            Agent.id != agent.id,
                            Agent.is_primary.is_(True),
                        )
                    )
                ).scalars().all():
                    other.is_primary = False

            await session.commit()
            await session.refresh(agent)
            logger.info(
                "agent_created owner_id=%s agent_id=%s display_name=%s",
                owner_id,
                agent.agent_id,
                display_name,
            )
            return self._summarise(agent)

    async def initialize_primary_agent(
        self, owner_id: uuid.UUID, *, display_name: str = "Nexus Agent"
    ) -> AgentSummary:
        """Return the owner's primary agent, creating it if absent.

        Idempotent: repeated calls never generate a second keypair. Also caches
        the identity snapshot used by the synchronous back-compat accessor.
        """
        existing = await self.get_primary_agent(owner_id)
        if existing is not None:
            self._primary_agent_row_id = existing.id
            # A corrupted identity must fail loudly here, at startup, rather
            # than at the first signature.
            self._primary_public = await self.verify_agent_key_integrity(
                existing.id
            )
            return existing
        created = await self.create_agent(
            owner_id, display_name=display_name, make_primary=True
        )
        self._primary_agent_row_id = created.id
        self._primary_public = await self.verify_agent_key_integrity(created.id)
        return created

    async def set_agent_status(
        self, owner_id: uuid.UUID, agent_row_id: uuid.UUID, status: str
    ) -> AgentSummary:
        if status not in {ACTIVE, PAUSED, REVOKED}:
            raise ValueError(f"Unknown agent status {status!r}")
        async with self._session_factory() as session:
            agent = await self._require_agent(session, owner_id, agent_row_id)
            agent.status = status
            if status == REVOKED:
                now = datetime.now(UTC)
                for key in (
                    await session.execute(
                        select(AgentKey).where(AgentKey.agent_row_id == agent.id)
                    )
                ).scalars().all():
                    if key.revoked_at is None:
                        key.revoked_at = now
                        key.revoked_reason = "agent revoked"
            await session.commit()
            await session.refresh(agent)
            logger.warning(
                "agent_status_changed agent_id=%s status=%s", agent.agent_id, status
            )
            return self._summarise(agent)

    # --- Key lifecycle -------------------------------------------------------

    async def list_keys(
        self, owner_id: uuid.UUID, agent_row_id: uuid.UUID
    ) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            agent = await self._require_agent(session, owner_id, agent_row_id)
            keys = (
                await session.execute(
                    select(AgentKey)
                    .where(AgentKey.agent_row_id == agent.id)
                    .order_by(AgentKey.created_at.desc())
                )
            ).scalars().all()
            return [
                {
                    "public_key": k.public_key,
                    "algorithm": k.key_algorithm,
                    "fingerprint": crypto.fingerprint_from_public_key(
                        base64.b64decode(k.public_key.encode("ascii"))
                    ),
                    "not_before": k.not_before.isoformat() if k.not_before else None,
                    "not_after": k.not_after.isoformat() if k.not_after else None,
                    "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
                    "revoked_reason": k.revoked_reason,
                    "is_current": k.agent_id == agent.agent_id,
                }
                for k in keys
            ]

    async def rotate_key(
        self,
        owner_id: uuid.UUID,
        agent_row_id: uuid.UUID,
        *,
        overlap_seconds: int = 86400,
    ) -> AgentSummary:
        """Issue a new key for an agent, retaining the old one for verification.

        The old key is NOT deleted: signatures it produced remain
        verifiable, and the audit trail stays intact. It is retired by setting
        ``not_after`` to the end of an overlap window, so in-flight messages
        signed with it still verify while new signatures use the new key.

        The agent's ``agent_id`` CHANGES, because the agent_id is a fingerprint
        of the public key - that is inherent to a self-certifying identity.
        Peers must re-pin; the old key remains verifiable during the overlap.
        """
        if not self._secret:
            raise IdentityCorruptionError(
                "Cannot rotate a key: NEXUS_IDENTITY_KEY is not set."
            )

        async with self._session_factory() as session:
            agent = await self._require_agent(session, owner_id, agent_row_id)
            if agent.status == REVOKED:
                raise AgentNotUsableError(
                    "Cannot rotate the key of a revoked agent."
                )

            now = datetime.now(UTC)
            cutoff = now + timedelta(seconds=overlap_seconds)

            # Retire the current key at the end of the overlap window.
            current = (
                await session.execute(
                    select(AgentKey).where(AgentKey.agent_id == agent.agent_id)
                )
            ).scalar_one_or_none()
            if current is not None:
                current.not_after = cutoff

            new_key = self._build_key(agent, session_owner_id=owner_id)
            session.add(new_key)
            await session.flush()

            old_agent_id = agent.agent_id
            agent.agent_id = new_key.agent_id
            await session.commit()
            await session.refresh(agent)

            logger.warning(
                "agent_key_rotated agent_row=%s old_agent_id=%s new_agent_id=%s "
                "old_key_valid_until=%s",
                agent.id,
                old_agent_id,
                agent.agent_id,
                cutoff.isoformat(),
            )
            return self._summarise(agent)

    async def revoke_key(
        self,
        owner_id: uuid.UUID,
        agent_row_id: uuid.UUID,
        *,
        agent_id: str,
        reason: str = "revoked by owner",
    ) -> None:
        """Revoke one specific key immediately."""
        async with self._session_factory() as session:
            agent = await self._require_agent(session, owner_id, agent_row_id)
            key = (
                await session.execute(
                    select(AgentKey).where(
                        AgentKey.agent_row_id == agent.id,
                        AgentKey.agent_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            if key is None:
                raise AgentNotFoundError(f"No key {agent_id} for this agent.")
            key.revoked_at = datetime.now(UTC)
            key.revoked_reason = reason
            await session.commit()
            logger.warning("agent_key_revoked agent_id=%s reason=%s", agent_id, reason)

    async def verify_agent_key_integrity(self, agent_row_id: uuid.UUID) -> PublicIdentity:
        """Force-decrypt and self-verify an agent's current key.

        Restores the pre-M4 startup guarantee ("a corrupted identity aborts
        startup: silently regenerating would break every future trust
        relationship"). The M4 refactor made key material resolve lazily per
        call, which is correct for rotation but means nothing touches the key
        at startup unless we do it explicitly here.

        Checks, in order:
          * the key decrypts under the configured secret (catches a wrong or
            missing NEXUS_IDENTITY_KEY),
          * the private key derives exactly the stored public key (catches a
            mismatched pair),
          * the stored agent_id is the fingerprint of that public key (catches
            a tampered identifier),
          * a signature round-trips (catches a broken crypto path).

        Raises IdentityCorruptionError on any failure. Never returns a
        partially-valid identity.
        """
        async with self._session_factory() as session:
            key = await self._current_key_for_row(session, agent_row_id)
            private_key = self._decrypt_key(key)

            test_message = b"nexus-identity-self-verification"
            signature = crypto.sign_bytes(private_key, test_message)
            public_key = crypto.load_public_key(
                base64.b64decode(key.public_key.encode("ascii"))
            )
            if not crypto.verify_bytes(public_key, test_message, signature):
                raise IdentityCorruptionError(
                    "Identity self-verification failed: signature did not verify."
                )
            public = self._public_from_key(key)

        logger.info(
            "identity_verification_success agent_id=%s", public.agent_id
        )
        return public

    # --- Cryptographic operations -------------------------------------------

    async def initialize_identity(self) -> PublicIdentity:
        """Back-compat: ensure the bound owner has a primary agent and return it.

        Retained so pre-M4 call sites and startup wiring keep working; new code
        should use :meth:`get_public_identity_for` with an explicit agent.
        """
        if self._owner_id is None:
            raise IdentityNotReadyError(
                "initialize_identity() requires the service to be bound to an owner."
            )
        summary = await self.initialize_primary_agent(self._owner_id)
        return await self.get_public_identity_for(summary.id)

    async def get_public_identity_for(
        self, agent_row_id: uuid.UUID
    ) -> PublicIdentity:
        async with self._session_factory() as session:
            key = await self._current_key_for_row(session, agent_row_id)
            return self._public_from_key(key)

    async def get_public_identity_by_agent_id(self, agent_id: str) -> PublicIdentity:
        async with self._session_factory() as session:
            key = (
                await session.execute(
                    select(AgentKey).where(AgentKey.agent_id == agent_id)
                )
            ).scalar_one_or_none()
            if key is None:
                raise AgentNotFoundError(f"No key for agent_id {agent_id}")
            return self._public_from_key(key)

    def get_public_identity(self) -> PublicIdentity:
        """Synchronous identity accessor for the primary agent.

        Back-compat for pre-M4 call sites and tests. Returns the snapshot taken
        when the primary agent was initialised, so it MUST NOT be used to make
        security decisions about a rotated key - use
        ``get_public_identity_for(agent_row_id)`` for that.
        """
        if self._primary_public is None:
            raise IdentityNotReadyError(
                "No primary agent loaded. Call initialize_primary_agent() / "
                "initialize_identity() first, or use "
                "get_public_identity_for(agent_row_id)."
            )
        return self._primary_public

    @property
    def primary_agent_row_id(self) -> uuid.UUID | None:
        """Row id of the primary agent, once initialized."""
        return self._primary_agent_row_id

    async def sign(self, data: bytes, *, agent_row_id: uuid.UUID | None = None) -> bytes:
        """Sign with an agent's CURRENT key.

        ``agent_row_id`` selects the agent. When omitted, the service falls
        back to the bound owner's primary agent (pre-M4 behaviour).
        """
        async with self._session_factory() as session:
            key = await self._resolve_signing_key(session, agent_row_id)
            private_key = self._decrypt_key(key)
            return crypto.sign_bytes(private_key, data)

    async def sign_as(self, agent_id: str, data: bytes) -> bytes:
        """Sign with the key identified by ``agent_id`` (must be usable)."""
        async with self._session_factory() as session:
            key = (
                await session.execute(
                    select(AgentKey).where(AgentKey.agent_id == agent_id)
                )
            ).scalar_one_or_none()
            if key is None:
                raise AgentNotFoundError(f"No key for agent_id {agent_id}")
            self._assert_key_usable(key)
            private_key = self._decrypt_key(key)
            return crypto.sign_bytes(private_key, data)

    async def sign_json(
        self, payload: object, *, agent_row_id: uuid.UUID | None = None
    ) -> bytes:
        return await self.sign(canonical_json_bytes(payload), agent_row_id=agent_row_id)

    async def verify(
        self, public_key: bytes, data: bytes, signature: bytes
    ) -> bool:
        """Verify with ANY public key - no private key needed, no agent lookup."""
        try:
            key = crypto.load_public_key(public_key)
        except crypto.IdentityCryptoError:
            return False
        return crypto.verify_bytes(key, data, signature)

    async def is_key_valid_at(
        self, agent_id: str, when: datetime | None = None
    ) -> bool:
        """Whether a key may be used at ``when`` (revocation-aware).

        Receivers use this to reject signatures from revoked keys while still
        accepting signatures produced during a key's validity window.
        """
        when = when or datetime.now(UTC)
        async with self._session_factory() as session:
            key = (
                await session.execute(
                    select(AgentKey).where(AgentKey.agent_id == agent_id)
                )
            ).scalar_one_or_none()
            if key is None:
                return False
            return key.is_usable_at(when)

    # --- Internals ----------------------------------------------------------

    def _summarise(self, agent: Agent) -> AgentSummary:
        return AgentSummary(
            id=agent.id,
            agent_id=agent.agent_id,
            display_name=agent.display_name,
            handle=agent.handle,
            endpoint=agent.endpoint,
            status=agent.status,
            is_primary=agent.is_primary,
            created_at=agent.created_at,
        )

    def _build_key(self, agent: Agent, *, session_owner_id: uuid.UUID) -> AgentKey:
        private_key, public_key = crypto.generate_keypair()
        public_raw = crypto.public_key_bytes(public_key)
        return AgentKey(
            owner_id=session_owner_id,
            agent_row_id=agent.id,
            agent_id=crypto.agent_id_from_public_key(public_raw),
            public_key=base64.b64encode(public_raw).decode("ascii"),
            encrypted_private_key=crypto.encrypt_private_key(
                crypto.private_key_bytes(private_key), self._secret or ""
            ),
            key_algorithm=crypto.KEY_ALGORITHM,
            not_before=datetime.now(UTC),
        )

    async def _require_agent(
        self, session: AsyncSession, owner_id: uuid.UUID, agent_row_id: uuid.UUID
    ) -> Agent:
        agent = await session.get(Agent, agent_row_id)
        if agent is None or agent.owner_id != owner_id:
            # Same error for "missing" and "someone else's" so agent ids are
            # not enumerable across owners.
            raise AgentNotFoundError("Agent not found.")
        return agent

    async def _current_key_for_row(
        self, session: AsyncSession, agent_row_id: uuid.UUID
    ) -> AgentKey:
        agent = await session.get(Agent, agent_row_id)
        if agent is None:
            raise AgentNotFoundError(f"Agent {agent_row_id} not found.")
        key = (
            await session.execute(
                select(AgentKey).where(AgentKey.agent_id == agent.agent_id)
            )
        ).scalar_one_or_none()
        if key is None:
            raise IdentityCorruptionError(
                f"Agent {agent.agent_id} has no key row: identity is corrupted."
            )
        return key

    async def _resolve_signing_key(
        self, session: AsyncSession, agent_row_id: uuid.UUID | None
    ) -> AgentKey:
        if agent_row_id is None:
            if self._owner_id is None:
                raise IdentityNotReadyError(
                    "No agent specified and the service is not bound to an owner."
                )
            agent = (
                await session.execute(
                    select(Agent)
                    .where(Agent.owner_id == self._owner_id)
                    .order_by(Agent.is_primary.desc(), Agent.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if agent is None:
                raise AgentNotFoundError("Owner has no agent to sign with.")
        else:
            agent = await session.get(Agent, agent_row_id)
            if agent is None:
                raise AgentNotFoundError(f"Agent {agent_row_id} not found.")
        if agent.status == REVOKED:
            raise AgentNotUsableError("This agent has been revoked and cannot sign.")
        if agent.status == PAUSED:
            raise AgentNotUsableError("This agent is paused and cannot sign.")
        key = await self._current_key_for_row(session, agent.id)
        # Revoking the CURRENT key must block signing. Without this check,
        # revoking a key only recorded intent while the key kept signing - the
        # revocation was decorative.
        self._assert_key_usable(key)
        return key

    def _assert_key_usable(self, key: AgentKey) -> None:
        if key.revoked_at is not None:
            raise AgentNotUsableError("This key has been revoked and cannot sign.")
        now = datetime.now(UTC)
        if not key.is_usable_at(now):
            raise AgentNotUsableError("This key is outside its validity window.")

    def _decrypt_key(self, key: AgentKey) -> crypto.Ed25519PrivateKey:
        if not self._secret:
            raise IdentityCorruptionError(
                "NEXUS_IDENTITY_KEY is not set; cannot decrypt key material."
            )
        try:
            private_raw = crypto.decrypt_private_key(
                key.encrypted_private_key, self._secret
            )
            private_key = crypto.load_private_key(private_raw)
            public_raw = base64.b64decode(key.public_key.encode("ascii"))
            public_key = crypto.load_public_key(public_raw)
        except crypto.IdentityCryptoError as exc:
            raise IdentityCorruptionError(str(exc)) from exc

        if not crypto.keypair_matches(private_key, public_key):
            raise IdentityCorruptionError(
                "Stored keypair is inconsistent: the private key does not match "
                "the public key. Refusing to sign with a corrupted key."
            )
        if key.agent_id != crypto.agent_id_from_public_key(public_raw):
            raise IdentityCorruptionError(
                "Stored agent_id does not match the stored public key. "
                "Refusing to sign with a corrupted key."
            )
        return private_key

    def _public_from_key(self, key: AgentKey) -> PublicIdentity:
        public_raw = base64.b64decode(key.public_key.encode("ascii"))
        return PublicIdentity(
            agent_id=key.agent_id,
            public_key=key.public_key,
            key_algorithm=key.key_algorithm,
            fingerprint=crypto.fingerprint_from_public_key(public_raw),
        )


__all__ = [
    "AgentNotFoundError",
    "AgentNotUsableError",
    "AgentSummary",
    "IdentityCorruptionError",
    "IdentityNotReadyError",
    "IdentityService",
    "PublicIdentity",
]
