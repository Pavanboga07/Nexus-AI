"""Database models for agent identity (Part 3, extended in M4).

The distinction that matters here:

    ``Agent``          an agent as an entity: who it belongs to, its display
                       name, handle, status, and which key is current
    ``AgentKey``       a cryptographic keypair belonging to an agent, with a
                       validity window and an optional revocation

Before M4 there was only ``AgentIdentity`` with
``UniqueConstraint("owner_id")`` - one agent per owner, enforced by the
database. That single constraint is what made "one owner, many agents"
impossible, and it is why the audit's "can one owner have multiple agents?"
question had a hard *no*.

Key rotation is modelled as multiple ``AgentKey`` rows per agent with
``not_before`` / ``not_after`` / ``revoked_at``, so a rotated key can still
verify signatures it produced during its validity window while a revoked key
is rejected everywhere. Deleting key material would destroy the audit trail
and break in-flight signatures.

The private key is stored ONLY as AES-GCM ciphertext (see
``app/identity/crypto.py``); the encryption secret lives in the deployment
environment, never in the database.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models import Base


class AgentStatus(str):
    """Agent lifecycle states.

    ``active``   usable for signing and messaging
    ``paused``   exists, keeps its identity, refuses new outbound sends
    ``revoked``  permanently retired; identity retained for audit only
    """

    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"


ACTIVE = AgentStatus.ACTIVE
PAUSED = AgentStatus.PAUSED
REVOKED = AgentStatus.REVOKED


class Agent(Base):
    """An AI agent owned by an owner (one owner may have MANY agents)."""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agents_agent_id"),
        # A handle is unique per owner, not globally: "@mum" is meaningful
        # inside one owner's address book.
        UniqueConstraint("owner_id", "handle", name="uq_agents_owner_handle"),
        Index("ix_agents_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("owners.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Cryptographic identity: nexus:ed25519:<hex fingerprint of public key>
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Optional short alias used in "@handle" resolution, unique per owner.
    handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Optional explicitly advertised endpoint; falls back to settings.
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ACTIVE, default=ACTIVE
    )
    #: Marks the owner's default agent (used when only one is expected).
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    keys: Mapped[list["AgentKey"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        order_by="AgentKey.created_at",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "owner_id": str(self.owner_id),
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "handle": self.handle,
            "endpoint": self.endpoint,
            "status": self.status,
            "is_primary": self.is_primary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AgentKey(Base):
    """One keypair belonging to an agent.

    Implemented on the historical ``agent_identities`` table so existing
    deployments keep their key material and audit history; M4 adds the
    validity window and the link to the ``agents`` row.
    """

    __tablename__ = "agent_identities"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_identities_agent_id"),
        UniqueConstraint("public_key", name="uq_agent_identities_public_key"),
        Index("ix_agent_identities_agent_row", "agent_row_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: The owning owner (kept for owner-scoped queries and back-compat).
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    #: The owning agent row (many keys per agent over time).
    agent_row_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    #: nexus:ed25519:<hex fingerprint of public key>
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: base64(raw 32-byte Ed25519 public key)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: base64(nonce || ciphertext+tag) - NEVER plaintext
    encrypted_private_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="Ed25519"
    )
    #: Validity window. A key outside it must not be used for new signatures,
    #: but may still verify signatures produced while it was valid.
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )
    not_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    agent: Mapped["Agent | None"] = relationship(back_populates="keys")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_usable_at(self, when: datetime) -> bool:
        """True when this key may sign at ``when``."""
        if self.revoked_at is not None:
            return False
        if self.not_before is not None and when < self.not_before:
            return False
        if self.not_after is not None and when >= self.not_after:
            return False
        return True


#: Back-compat alias: the pre-M4 name for AgentKey.
AgentIdentity = AgentKey

__all__ = [
    "ACTIVE",
    "Agent",
    "AgentIdentity",
    "AgentKey",
    "AgentStatus",
    "PAUSED",
    "REVOKED",
]
