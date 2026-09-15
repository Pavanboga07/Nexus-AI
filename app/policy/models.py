"""Policy & consent domain models and controlled vocabularies (Part 4).

Security model:

    Identity = Who are you?
    Policy   = What are you allowed to do?
    Consent  = What did the owner explicitly approve?
    Memory   = What does the agent know?
    LLM      = How does the agent reason?

The policy engine is deterministic: structured inputs + database state in,
structured decision out. It never consults the LLM, embeddings, or memory
similarity.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models import Base

#: Wildcard for stored rules ("applies to any"). Evaluation REQUESTS may
#: never use the wildcard - a request must name a concrete requester.
WILDCARD = "*"


class PolicyDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class DisclosureScope(str, enum.Enum):
    """Minimum-disclosure vocabulary (spec §14).

    none     -> no information
    category -> only the high-level category result
    summary  -> summarized information
    exact    -> exact permitted information
    """

    NONE = "none"
    CATEGORY = "category"
    SUMMARY = "summary"
    EXACT = "exact"


#: Controlled data-category vocabulary. Membership is NOT enforced at the
#: boundary beyond slug format - future categories must work without
#: architectural changes (spec §5) - but these are the known ones.
KNOWN_DATA_CATEGORIES = frozenset(
    {
        "availability",
        "contact",
        "location",
        "schedule",
        "relationship",
        "preferences",
        "personal",
        "financial",
        "private",
        "work",
        "custom",
    }
)

#: Controlled action vocabulary (authorization only; actions are not
#: implemented in Part 4).
KNOWN_ACTIONS = frozenset(
    {
        "read_memory",
        "disclose_information",
        "send_message",
        "create_event",
        "modify_event",
        "access_tool",
        "custom",
    }
)

#: High-sensitivity categories that are deny-by-default: wildcard-category
#: policies never satisfy them - the owner must name the category explicitly
#: in a policy or grant a consent (spec §11).
SENSITIVE_DEFAULT_DENY = frozenset({"financial", "private"})


class Policy(Base):
    """A standing owner rule. Applies to a requester/category/action/purpose
    tuple, any of which (except owner) may be the wildcard ``*``."""

    __tablename__ = "policies"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('ALLOW','ASK','DENY')", name="ck_policies_decision"
        ),
        CheckConstraint(
            "disclosure_scope IN ('none','category','summary','exact')",
            name="ck_policies_disclosure_scope",
        ),
        Index("ix_policies_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False
    )
    #: "nexus:ed25519:..." for a specific agent, or "*" for any agent.
    requester_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Data category slug, or "*".
    data_category: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Action slug, or "*".
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Purpose slug, or "*".
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    disclosure_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="category"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Optional validity window. starts_at in the future -> not yet active.
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "requester_agent_id": self.requester_agent_id,
            "data_category": self.data_category,
            "action": self.action,
            "purpose": self.purpose,
            "decision": self.decision,
            "disclosure_scope": self.disclosure_scope,
            "priority": self.priority,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Consent(Base):
    """An explicit owner approval/denial - typically the recorded answer to
    an ASK decision.

    Consents always name concrete values (no wildcards): they record what
    the owner actually approved. One-time consents (``single_use``) are
    consumed atomically on first successful use.
    """

    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('ALLOW','DENY')", name="ck_consents_decision"
        ),
        Index("ix_consents_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False
    )
    requester_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    data_category: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    disclosure_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="category"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    single_use: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "requester_agent_id": self.requester_agent_id,
            "data_category": self.data_category,
            "action": self.action,
            "purpose": self.purpose,
            "decision": self.decision,
            "disclosure_scope": self.disclosure_scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "single_use": self.single_use,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PolicyDecisionRecord(Base):
    """Audit trail. One row per evaluation - ALLOW, ASK, and DENY alike.

    Stores the decision metadata only; never the disclosed content.
    """

    __tablename__ = "policy_decisions"
    __table_args__ = (Index("ix_policy_decisions_owner_created", "owner_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owners.id"), nullable=False
    )
    requester_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    data_category: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    matched_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policies.id", ondelete="SET NULL"), nullable=True
    )
    matched_consent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("consents.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "requester_agent_id": self.requester_agent_id,
            "data_category": self.data_category,
            "action": self.action,
            "purpose": self.purpose,
            "decision": self.decision,
            "matched_policy_id": (
                str(self.matched_policy_id) if self.matched_policy_id else None
            ),
            "matched_consent_id": (
                str(self.matched_consent_id) if self.matched_consent_id else None
            ),
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = [
    "KNOWN_ACTIONS",
    "KNOWN_DATA_CATEGORIES",
    "PolicyDecision",
    "PolicyDecisionRecord",
    "Policy",
    "Consent",
    "DisclosureScope",
    "SENSITIVE_DEFAULT_DENY",
    "WILDCARD",
]
