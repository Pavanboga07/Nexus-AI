"""Part 4: policy, consent, and audit tables.

Revision ID: 0003_part4_policy
Revises: 0002_part3_identity
Create Date: 2026-09-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003_part4_policy"
down_revision: Union[str, None] = "0002_part3_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("requester_agent_id", sa.String(255), nullable=False),
        sa.Column("data_category", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column(
            "disclosure_scope",
            sa.String(16),
            nullable=False,
            server_default="category",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('ALLOW','ASK','DENY')", name="ck_policies_decision"
        ),
        sa.CheckConstraint(
            "disclosure_scope IN ('none','category','summary','exact')",
            name="ck_policies_disclosure_scope",
        ),
    )
    op.create_index("ix_policies_owner", "policies", ["owner_id"])

    op.create_table(
        "consents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("requester_agent_id", sa.String(255), nullable=False),
        sa.Column("data_category", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column(
            "disclosure_scope",
            sa.String(16),
            nullable=False,
            server_default="category",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "single_use", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('ALLOW','DENY')", name="ck_consents_decision"
        ),
    )
    op.create_index("ix_consents_owner", "consents", ["owner_id"])

    op.create_table(
        "policy_decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("requester_agent_id", sa.String(255), nullable=False),
        sa.Column("data_category", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column(
            "matched_policy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "matched_consent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("consents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_policy_decisions_owner_created",
        "policy_decisions",
        ["owner_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_policy_decisions_owner_created", table_name="policy_decisions"
    )
    op.drop_table("policy_decisions")
    op.drop_index("ix_consents_owner", table_name="consents")
    op.drop_table("consents")
    op.drop_index("ix_policies_owner", table_name="policies")
    op.drop_table("policies")
