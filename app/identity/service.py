"""IdentityService: the app-facing identity API.

Owns the full identity lifecycle:

    startup: load existing identity (decrypt, self-verify)
             OR first run: generate, encrypt, persist (exactly once)

    signing: sign(bytes) with the resident private key
    verify:  static check of (public_key, message, signature) - no private
             key required, ready for Part 6 A2A

The service is owner-scoped: the identity belongs to one owner row. Nothing
outside this class performs cryptography or touches key material.
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Owner
from app.identity import crypto
from app.identity.models import AgentIdentity

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


class IdentityService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        encryption_secret: str | None,
        owner_id: uuid.UUID,
    ) -> None:
        self._session_factory = session_factory
        self._secret = encryption_secret
        self._owner_id = owner_id
        self._private_key: crypto.Ed25519PrivateKey | None = None
        self._public_identity: PublicIdentity | None = None

    # --- State ----------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._private_key is not None and self._public_identity is not None

    def get_public_identity(self) -> PublicIdentity:
        if self._public_identity is None:
            raise IdentityNotReadyError("Agent identity is not initialised.")
        return self._public_identity

    # --- Lifecycle ----------------------------------------------------------------

    async def initialize_identity(self) -> PublicIdentity:
        """Load the owner's identity, creating it on first run.

        Idempotent per owner: repeated calls never generate a second keypair.
        """
        async with self._session_factory() as session:
            identity = await self._load_row(session)
            if identity is None:
                identity = await self._create_row(session)
                await session.commit()
                logger.info(
                    "identity_initialized agent_id=%s",
                    identity.agent_id,
                )
            else:
                logger.info(
                    "identity_loaded agent_id=%s",
                    identity.agent_id,
                )

        self._activate(identity)
        await self._self_verify()
        return self._public_identity  # type: ignore[return-value]

    async def _load_row(self, session: AsyncSession) -> AgentIdentity | None:
        result = await session.execute(
            select(AgentIdentity).where(
                AgentIdentity.owner_id == self._owner_id
            )
        )
        return result.scalar_one_or_none()

    async def _create_row(self, session: AsyncSession) -> AgentIdentity:
        if not self._secret:
            raise IdentityCorruptionError(
                "Cannot create a new agent identity: NEXUS_IDENTITY_KEY is "
                "not set. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        private_key, public_key = crypto.generate_keypair()
        public_raw = crypto.public_key_bytes(public_key)
        identity = AgentIdentity(
            owner_id=self._owner_id,
            agent_id=crypto.agent_id_from_public_key(public_raw),
            public_key=_b64encode(public_raw),
            encrypted_private_key=crypto.encrypt_private_key(
                crypto.private_key_bytes(private_key), self._secret
            ),
            key_algorithm=crypto.KEY_ALGORITHM,
        )
        session.add(identity)
        await session.flush()
        return identity

    def _activate(self, identity: AgentIdentity) -> None:
        """Decrypt and load the keypair into memory, or fail hard."""
        if not self._secret:
            raise IdentityCorruptionError(
                "An agent identity exists but NEXUS_IDENTITY_KEY is not "
                "set. Provide the same secret used when the identity was "
                "created; refusing to continue with an unreadable identity."
            )
        try:
            private_raw = crypto.decrypt_private_key(
                identity.encrypted_private_key, self._secret
            )
            private_key = crypto.load_private_key(private_raw)
            public_raw = _b64decode(identity.public_key)
            public_key = crypto.load_public_key(public_raw)
        except crypto.IdentityCryptoError as exc:
            raise IdentityCorruptionError(str(exc)) from exc

        if not crypto.keypair_matches(private_key, public_key):
            raise IdentityCorruptionError(
                "Stored keypair is inconsistent: the private key does not "
                "match the public key. Refusing to run with a corrupted "
                "identity."
            )
        if identity.agent_id != crypto.agent_id_from_public_key(public_raw):
            raise IdentityCorruptionError(
                "Stored agent_id does not match the stored public key. "
                "Refusing to run with a corrupted identity."
            )

        self._private_key = private_key
        self._public_identity = PublicIdentity(
            agent_id=identity.agent_id,
            public_key=identity.public_key,
            key_algorithm=identity.key_algorithm,
            fingerprint=crypto.fingerprint_from_public_key(public_raw),
        )

    async def _self_verify(self) -> None:
        """Sign a test message and verify it (spec §13). Failure is fatal."""
        assert self._private_key is not None and self._public_identity is not None
        test_message = b"nexus-identity-self-verification"
        signature = crypto.sign_bytes(self._private_key, test_message)
        public_key = crypto.load_public_key(
            _b64decode(self._public_identity.public_key)
        )
        if not crypto.verify_bytes(public_key, test_message, signature):
            raise IdentityCorruptionError(
                "Identity self-verification failed: signature did not verify."
            )
        logger.info(
            "identity_verification_success agent_id=%s",
            self._public_identity.agent_id,
        )

    # --- Signing / verification ----------------------------------------------

    async def sign(self, data: bytes) -> bytes:
        """Sign arbitrary bytes with the resident private key."""
        if self._private_key is None:
            raise IdentityNotReadyError(
                "Cannot sign: agent identity is not initialised."
            )
        return crypto.sign_bytes(self._private_key, data)

    async def sign_json(self, payload: object) -> bytes:
        """Convenience: canonically serialize then sign (see serialization.py)."""
        from app.identity.serialization import canonical_json_bytes

        return await self.sign(canonical_json_bytes(payload))

    async def verify(
        self, public_key: bytes, data: bytes, signature: bytes
    ) -> bool:
        """Verify a signature with ANY public key - no private key needed.

        Pure function of its arguments; this is the seed of Part 6 A2A
        verification."""
        try:
            key = crypto.load_public_key(public_key)
        except crypto.IdentityCryptoError:
            return False
        return crypto.verify_bytes(key, data, signature)


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


__all__ = [
    "IdentityCorruptionError",
    "IdentityNotReadyError",
    "IdentityService",
    "PublicIdentity",
]
