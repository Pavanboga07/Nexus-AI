"""Database model for agent identity (Part 3).

One row per owner's agent. ``agent_id`` and ``public_key`` are unique - a
single public key can only ever identify one agent row. The private key is
stored ONLY as AES-GCM ciphertext (see app/identity/crypto.py); the
encryption secret lives in the deployment environment, never in the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models import Base


class AgentIdentity(Base):
    __tablename__ = "agent_identities"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_agent_identities_owner"),
        UniqueConstraint("agent_id", name="uq_agent_identities_agent_id"),
        UniqueConstraint("public_key", name="uq_agent_identities_public_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False, index=True
    )
    # nexus:ed25519:<hex fingerprint of public key>
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # base64(raw 32-byte Ed25519 public key)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # base64(nonce || ciphertext+tag) - NEVER plaintext
    encrypted_private_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="Ed25519")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner = relationship("Owner", backref="agent_identity")


__all__ = ["AgentIdentity"]
