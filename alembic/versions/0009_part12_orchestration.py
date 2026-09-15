"""Part 12: Natural Language Agent Orchestration.

Revision ID: 0009_part12_orchestration
Revises: 0008_part10_autonomy
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0009_part12_orchestration"
down_revision: Union[str, None] = "0008_part10_autonomy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. contacts
    op.create_table(
        "contacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "aliases",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
    )
    op.create_index("ix_contacts_owner_id", "contacts", ["owner_id"])
    op.create_index("ix_contacts_display_name", "contacts", ["display_name"])

    # 2. orchestration_runs
    op.create_table(
        "orchestration_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("intent_type", sa.String(64), nullable=False),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default="UNDERSTANDING",
        ),
        sa.Column("target_person", sa.String(255), nullable=True),
        sa.Column("target_agent_id", sa.String(255), nullable=True),
        sa.Column(
            "plan",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "result",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("approval_prompt", sa.Text(), nullable=True),
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
    )
    op.create_index("ix_orchestration_runs_owner_id", "orchestration_runs", ["owner_id"])
    op.create_index("ix_orchestration_runs_session_id", "orchestration_runs", ["session_id"])
    op.create_index("ix_orchestration_runs_state", "orchestration_runs", ["state"])


def downgrade() -> None:
    op.drop_index("ix_orchestration_runs_state", table_name="orchestration_runs")
    op.drop_index("ix_orchestration_runs_session_id", table_name="orchestration_runs")
    op.drop_index("ix_orchestration_runs_owner_id", table_name="orchestration_runs")
    op.drop_table("orchestration_runs")

    op.drop_index("ix_contacts_display_name", table_name="contacts")
    op.drop_index("ix_contacts_owner_id", table_name="contacts")
    op.drop_table("contacts")
