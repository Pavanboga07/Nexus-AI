"""Authentication domain models.

``Owner`` (in ``app.database.models``) is the principal; credentials live here
so the core schema is not polluted with authentication concerns, and so a user
can later have several login methods (password now, OIDC later) without a
schema rewrite.

    owners           who the principal is
    user_credentials how they prove it (Argon2id password hash)
    auth_sessions    issued sessions, revocable server-side
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base

#: At most one password credential per identifier, case-insensitively -
#: enforced in the database so two concurrent registrations cannot both win.
PASSWORD_IDENTIFIER_INDEX = "uq_user_credentials_password_identifier"


class UserCredential(Base):
    """A login method for an owner (password today, OIDC later)."""

    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", name="uq_user_credentials_owner_kind"),
        Index("ix_user_credentials_identifier", "identifier"),
        Index(
            PASSWORD_IDENTIFIER_INDEX,
            text("lower(identifier)"),
            unique=True,
            postgresql_where=text("kind = 'password'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("owners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: "password" | "oidc"
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="password")
    #: Login identifier. For "password" this is the email; for "oidc" the
    #: provider subject. Stored lowercase for password logins.
    identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Argon2id encoded hash. NULL for credential kinds that do not use one.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: For OIDC: the issuer the subject belongs to.
    issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AuthSession(Base):
    """An issued session.

    The cookie carries a SIGNED payload; this row makes the session revocable
    (logout, password change, "sign out everywhere") and gives an audit trail
    of when and from where a session was created.

    Only the token FINGERPRINT is stored - never the token itself - so a
    database disclosure does not hand over live sessions.
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_fingerprint", name="uq_auth_sessions_fingerprint"),
        Index("ix_auth_sessions_owner", "owner_id"),
        Index("ix_auth_sessions_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("owners.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Matches the `jti` claim, so a signed payload can be tied to a row.
    token_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


__all__ = ["AuthSession", "UserCredential"]
