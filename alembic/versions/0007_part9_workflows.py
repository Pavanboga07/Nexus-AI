"""Part 9: Proactive workflows and workflow steps.

Revision ID: 0007_part9_workflows
Revises: 0006_part8_tasks
Create Date: 2026-09-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007_part9_workflows"
down_revision: Union[str, None] = "0006_part8_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("workflow_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("workflow_type", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "current_step_number",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("context_data", sa.JSON(), nullable=True),
        sa.Column("workflow_metadata", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_workflows_owner_id", "workflows", ["owner_id"])
    op.create_index("ix_workflows_status", "workflows", ["status"])

    op.create_table(
        "workflow_steps",
        sa.Column("step_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("input_payload", sa.JSON(), nullable=True),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "workflow_id",
            "step_number",
            name="uq_workflow_steps_workflow_step_number",
        ),
    )
    op.create_index("ix_workflow_steps_workflow_id", "workflow_steps", ["workflow_id"])
    op.create_index("ix_workflow_steps_status", "workflow_steps", ["status"])
    op.create_index("ix_workflow_steps_task_id", "workflow_steps", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_steps_task_id", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_status", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_workflow_id", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_workflows_status", table_name="workflows")
    op.drop_index("ix_workflows_owner_id", table_name="workflows")
    op.drop_table("workflows")
