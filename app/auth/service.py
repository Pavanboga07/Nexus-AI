"""AuthService: registration, login, logout, and session resolution.

This is the ONLY place that turns a browser session into an ``(owner_id)``.
Everything downstream receives that id as an argument, which is what makes
multi-tenancy possible: before this module, the owner was resolved once at
startup from the first row in ``owners`` and cached for the process lifetime,
so the entire API served exactly one principal no matter who called it.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import crypto
from app.auth.models import AuthSession, UserCredential
from app.database.models import Owner

logger = logging.getLogger("nexus.auth.service")

#: Deliberately permissive but structurally meaningful. Full RFC 5322
#: validation is a rabbit hole; require one @, a dot in the domain, no spaces.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
MAX_EMAIL_LENGTH = 320


class AuthError(Exception):
    """Authentication failure with a safe, user-facing message."""

    def __init__(self, message: str, *, http_status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


class RegistrationError(AuthError):
    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=400)


class EmailAlreadyRegisteredError(AuthError):
    def __init__(self, message: str = "That email address is already registered.") -> None:
        super().__init__(message, http_status=409)


@dataclass(frozen=True)
class AuthenticatedUser:
    """The resolved identity for a request."""

    owner_id: uuid.UUID
    identifier: str
    session_id: uuid.UUID | None = None

    @property
    def email(self) -> str:
        return self.identifier


@dataclass(frozen=True)
class IssuedSession:
    """A newly created session: the signed cookie value plus its metadata."""

    token: str
    owner_id: uuid.UUID
    expires_at: datetime


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized or len(normalized) > MAX_EMAIL_LENGTH:
        raise RegistrationError("A valid email address is required.")
    if not _EMAIL_RE.match(normalized):
        raise RegistrationError("That does not look like a valid email address.")
    return normalized


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise RegistrationError("Password must be a string.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise RegistrationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise RegistrationError("Password is too long.")
    return password


class AuthService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        session_secret: str | None,
        session_ttl_seconds: int = crypto.DEFAULT_SESSION_TTL_SECONDS,
        allow_registration: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._secret = session_secret
        self._ttl = session_ttl_seconds
        self._allow_registration = allow_registration

    @property
    def configured(self) -> bool:
        """Auth can only be enforced when a signing secret exists."""
        return bool(self._secret)

    # --- Registration -------------------------------------------------------

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str | None = None,
        adopt_existing_owner_id: uuid.UUID | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        """Create a principal + credential and issue a session.

        ``adopt_existing_owner_id`` attaches the credential to an existing
        owner row that has no credential yet. That is the migration path for a
        pre-auth deployment: the single pre-existing owner (and therefore all
        of its memory, conversations, identity and policies) becomes the first
        registered user's account instead of being orphaned.
        """
        if not self._allow_registration:
            raise RegistrationError("Registration is disabled on this deployment.")
        if not self.configured:
            raise AuthError(
                "Authentication is not configured: set NEXUS_SESSION_KEY.",
                http_status=503,
            )

        clean_email = validate_email(email)
        validate_password(password)

        async with self._session_factory() as session:
            existing = await self._find_credential(session, clean_email)
            if existing is not None:
                raise EmailAlreadyRegisteredError()

            owner_id = adopt_existing_owner_id
            if owner_id is not None:
                claimed = await self._owner_is_unclaimed(session, owner_id)
                if not claimed:
                    # Already has credentials: do not hijack it.
                    owner_id = None

            if owner_id is None:
                owner = Owner(name=display_name or clean_email)
                session.add(owner)
                await session.flush()
                owner_id = owner.id
            elif display_name:
                owner_row = await session.get(Owner, owner_id)
                if owner_row is not None and not owner_row.name:
                    owner_row.name = display_name

            session.add(
                UserCredential(
                    owner_id=owner_id,
                    kind="password",
                    identifier=clean_email,
                    password_hash=crypto.hash_password(password),
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                raise EmailAlreadyRegisteredError() from exc

            issued = await self._issue_session(
                session,
                owner_id=owner_id,
                user_agent=user_agent,
                ip_address=ip_address,
            )
            await session.commit()

        logger.info("user_registered owner_id=%s", owner_id)
        return issued

    # --- Login / logout -----------------------------------------------------

    async def login(
        self,
        *,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        clean_email = normalize_email(email)
        # Validate password length loosely so a None/empty password fails the
        # same way as a wrong one (no user-enumeration signal).
        password = password if isinstance(password, str) else ""

        async with self._session_factory() as session:
            credential = await self._find_credential(session, clean_email)
            if credential is None or not credential.is_active:
                # Spend comparable time to avoid a trivial timing oracle.
                crypto.verify_password(
                    "$argon2id$v=19$m=65536,t=3,p=4$"
                    "AAAAAAAAAAAAAAAAAAAAAA$"
                    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    password,
                )
                raise AuthError("Invalid email or password.")
            if not crypto.verify_password(credential.password_hash or "", password):
                raise AuthError("Invalid email or password.")

            # Opportunistically upgrade the hash if parameters changed.
            if crypto.password_needs_rehash(credential.password_hash or ""):
                credential.password_hash = crypto.hash_password(password)

            issued = await self._issue_session(
                session,
                owner_id=credential.owner_id,
                user_agent=user_agent,
                ip_address=ip_address,
            )
            await session.commit()

        logger.info("user_login owner_id=%s", issued.owner_id)
        return issued

    async def logout(self, token: str) -> bool:
        """Revoke the session identified by ``token``. Idempotent.

        ``token`` is the cookie value (``<opaque>.<signed>``); revocation keys
        on the fingerprint of the opaque half.
        """
        fingerprint = crypto.session_fingerprint_from_cookie(token or "")
        if fingerprint is None:
            return False
        async with self._session_factory() as session:
            result = await session.execute(
                select(AuthSession).where(
                    AuthSession.token_fingerprint == fingerprint,
                    AuthSession.revoked_at.is_(None),
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            row.revoked_at = datetime.now(timezone.utc)
            await session.commit()
        return True

    async def revoke_all_sessions(self, owner_id: uuid.UUID) -> int:
        """Sign the user out everywhere (e.g. after a password change)."""
        async with self._session_factory() as session:
            result = await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.owner_id == owner_id,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(timezone.utc))
            )
            await session.commit()
            return int(result.rowcount or 0)

    # --- Resolution ---------------------------------------------------------

    async def resolve_session(self, token: str) -> AuthenticatedUser | None:
        """Turn a cookie value into an identity, or None.

        Verifies BOTH the signature/expiry and that the session row is still
        live, so logout and revocation take effect immediately.
        """
        if not token or not self._secret:
            return None
        payload = crypto.verify_session_payload(token, secret=self._secret)
        if payload is None:
            return None
        try:
            owner_id = uuid.UUID(str(payload["sub"]))
        except (KeyError, ValueError, TypeError):
            return None

        fingerprint = crypto.session_fingerprint_from_cookie(token)
        if fingerprint is None:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                select(AuthSession).where(
                    AuthSession.token_fingerprint == fingerprint,
                    AuthSession.owner_id == owner_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at <= datetime.now(timezone.utc):
                return None
            row.last_seen_at = datetime.now(timezone.utc)
            await session.commit()

            credential = await self._credential_for_owner(session, owner_id)

        return AuthenticatedUser(
            owner_id=owner_id,
            identifier=credential.identifier if credential else "",
            session_id=row.id,
        )

    async def change_password(
        self, owner_id: uuid.UUID, *, current_password: str, new_password: str
    ) -> None:
        """Change a password and revoke every existing session."""
        validate_password(new_password)
        async with self._session_factory() as session:
            credential = await self._credential_for_owner(session, owner_id)
            if credential is None:
                raise AuthError("No password credential for this account.")
            if not crypto.verify_password(
                credential.password_hash or "", current_password
            ):
                raise AuthError("Current password is incorrect.")
            credential.password_hash = crypto.hash_password(new_password)
            await session.commit()
        await self.revoke_all_sessions(owner_id)

    # --- Internals ----------------------------------------------------------

    async def _issue_session(
        self,
        session: AsyncSession,
        *,
        owner_id: uuid.UUID,
        user_agent: str | None,
        ip_address: str | None,
    ) -> IssuedSession:
        assert self._secret is not None
        opaque_token = crypto.generate_session_token()
        # Revocation keys on the fingerprint of the OPAQUE half so it is
        # independent of the signed claims.
        payload = crypto.build_session_payload(owner_id, ttl_seconds=self._ttl)
        signed = crypto.sign_session_payload(payload, secret=self._secret)
        expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

        session.add(
            AuthSession(
                owner_id=owner_id,
                token_fingerprint=crypto.token_fingerprint(opaque_token),
                token_id=str(payload["jti"]),
                user_agent=(user_agent or "")[:512] or None,
                ip_address=(ip_address or "")[:64] or None,
                expires_at=expires_at,
            )
        )
        # The cookie carries the opaque token (for revocation) joined to the
        # signed payload (for stateless verification) - both halves refer to
        # the same session.
        cookie_value = crypto.build_session_cookie_value(opaque_token, signed)
        await session.flush()
        return IssuedSession(token=cookie_value, owner_id=owner_id, expires_at=expires_at)

    async def _find_credential(
        self, session: AsyncSession, identifier: str
    ) -> UserCredential | None:
        result = await session.execute(
            select(UserCredential).where(
                UserCredential.kind == "password",
                UserCredential.identifier == identifier,
            )
        )
        return result.scalar_one_or_none()

    async def _credential_for_owner(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> UserCredential | None:
        result = await session.execute(
            select(UserCredential).where(
                UserCredential.owner_id == owner_id,
                UserCredential.kind == "password",
            )
        )
        return result.scalar_one_or_none()

    async def _owner_is_unclaimed(
        self, session: AsyncSession, owner_id: uuid.UUID
    ) -> bool:
        """True when the owner exists and has no credential yet."""
        owner = await session.get(Owner, owner_id)
        if owner is None:
            return False
        result = await session.execute(
            select(UserCredential.id).where(UserCredential.owner_id == owner_id)
        )
        return result.scalar_one_or_none() is None

    async def first_unclaimed_owner_id(self) -> uuid.UUID | None:
        """The pre-auth owner row, if the deployment has exactly one and it
        has no credentials. Used to adopt legacy data on first registration."""
        async with self._session_factory() as session:
            owners = (
                await session.execute(select(Owner).order_by(Owner.created_at))
            ).scalars().all()
            if len(owners) != 1:
                return None
            if await self._owner_is_unclaimed(session, owners[0].id):
                return owners[0].id
        return None


__all__ = [
    "AuthError",
    "AuthService",
    "AuthenticatedUser",
    "EmailAlreadyRegisteredError",
    "IssuedSession",
    "MIN_PASSWORD_LENGTH",
    "RegistrationError",
    "normalize_email",
    "validate_email",
    "validate_password",
]
