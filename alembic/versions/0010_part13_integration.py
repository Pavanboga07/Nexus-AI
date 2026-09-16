"""Part 13: Nexus Integration, State Correctness & Real Network Orchestration.

Revision ID: 0010_part13_integration
Revises: 0009_part12_orchestration
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0010_part13_integration"
down_revision: Union[str, None] = "0009_part12_orchestration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add approval and correlation columns to orchestration_runs
    op.add_column("orchestration_runs", sa.Column("task_id", sa.String(128), nullable=True))
    op.add_column("orchestration_runs", sa.Column("workflow_id", UUID(as_uuid=True), nullable=True))
    op.add_column("orchestration_runs", sa.Column("approval_reason", sa.Text(), nullable=True))
    op.add_column("orchestration_runs", sa.Column("requested_action", sa.String(128), nullable=True))
    op.add_column("orchestration_runs", sa.Column("approval_target", sa.String(255), nullable=True))
    op.add_column("orchestration_runs", sa.Column("approval_category", sa.String(64), nullable=True))
    op.add_column("orchestration_runs", sa.Column("approval_purpose", sa.String(128), nullable=True))
    op.add_column("orchestration_runs", sa.Column("approval_step", sa.Integer(), nullable=True))
    op.add_column("orchestration_runs", sa.Column("owner_decision", sa.String(32), nullable=True))

    # 2. Create orchestration_contexts table for durable session state across restarts
    op.create_table(
        "orchestration_contexts",
        sa.Column("session_id", sa.String(255), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("active_target", sa.String(255), nullable=True),
        sa.Column("active_agent_id", sa.String(255), nullable=True),
        sa.Column("last_proposed_time", sa.String(128), nullable=True),
        sa.Column("last_task_id", sa.String(128), nullable=True),
        sa.Column("last_run_id", sa.String(64), nullable=True),
        sa.Column("last_intent_type", sa.String(64), nullable=True),
        sa.Column(
            "pending_approval",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_orchestration_contexts_owner_id",
        "orchestration_contexts",
        ["owner_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_orchestration_contexts_owner_id", table_name="orchestration_contexts")
    op.drop_table("orchestration_contexts")
    op.drop_column("orchestration_runs", "owner_decision")
    op.drop_column("orchestration_runs", "approval_step")
    op.drop_column("orchestration_runs", "approval_purpose")
    op.drop_column("orchestration_runs", "approval_category")
    op.drop_column("orchestration_runs", "approval_target")
    op.drop_column("orchestration_runs", "requested_action")
    op.drop_column("orchestration_runs", "approval_reason")
    op.drop_column("orchestration_runs", "workflow_id")
    op.drop_column("orchestration_runs", "task_id")
