"""Part 10: Autonomy and Decision Engine.

Revision ID: 0008_part10_autonomy
Revises: 0007_part9_workflows
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008_part10_autonomy"
down_revision: Union[str, None] = "0007_part9_workflows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. autonomy_configs
    op.create_table(
        "autonomy_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False, server_default="bounded"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("max_steps_per_run", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("max_runtime_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("max_remote_tasks", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("require_approval_for_unknown_actions", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("require_approval_for_external_communication", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("require_approval_for_sensitive_data", sa.Boolean(), nullable=False, server_default="true"),
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
        sa.UniqueConstraint("owner_id", name="uq_autonomy_configs_owner_id"),
    )
    op.create_index("ix_autonomy_configs_owner_id", "autonomy_configs", ["owner_id"])

    # 2. autonomy_runs
    op.create_table(
        "autonomy_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column(
            "workflow_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workflows.workflow_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("goal", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_executed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remote_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.Column("context_data", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("stop_reason", sa.String(255), nullable=True),
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
    )
    op.create_index("ix_autonomy_runs_owner_id", "autonomy_runs", ["owner_id"])
    op.create_index("ix_autonomy_runs_status", "autonomy_runs", ["status"])
    op.create_index("ix_autonomy_runs_workflow_id", "autonomy_runs", ["workflow_id"])

    # 3. autonomy_decisions
    op.create_table(
        "autonomy_decisions",
        sa.Column("decision_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("autonomy_runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("trigger", sa.String(64), nullable=False),
        sa.Column("goal", sa.String(500), nullable=False),
        sa.Column("proposed_action", sa.String(128), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("required_capability", sa.String(64), nullable=True),
        sa.Column("required_data_categories", sa.JSON(), nullable=True),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_decision", sa.String(32), nullable=True),
        sa.Column("consent_decision", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_autonomy_decisions_owner_id", "autonomy_decisions", ["owner_id"])
    op.create_index("ix_autonomy_decisions_run_id", "autonomy_decisions", ["run_id"])

    # 4. autonomy_approvals
    op.create_table(
        "autonomy_approvals",
        sa.Column("approval_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("autonomy_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            UUID(as_uuid=True),
            sa.ForeignKey("autonomy_decisions.decision_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("requested_action", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("required_data", sa.JSON(), nullable=True),
        sa.Column("recipient", sa.String(128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_autonomy_approvals_owner_id", "autonomy_approvals", ["owner_id"])
    op.create_index("ix_autonomy_approvals_run_id", "autonomy_approvals", ["run_id"])
    op.create_index("ix_autonomy_approvals_status", "autonomy_approvals", ["status"])

    # 5. autonomy_triggers
    op.create_table(
        "autonomy_triggers",
        sa.Column("trigger_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("autonomy_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_autonomy_triggers_owner_id", "autonomy_triggers", ["owner_id"])
    op.create_index("ix_autonomy_triggers_run_id", "autonomy_triggers", ["run_id"])
    op.create_index("ix_autonomy_triggers_trigger_type", "autonomy_triggers", ["trigger_type"])


def downgrade() -> None:
    op.drop_index("ix_autonomy_triggers_trigger_type", table_name="autonomy_triggers")
    op.drop_index("ix_autonomy_triggers_run_id", table_name="autonomy_triggers")
    op.drop_index("ix_autonomy_triggers_owner_id", table_name="autonomy_triggers")
    op.drop_table("autonomy_triggers")

    op.drop_index("ix_autonomy_approvals_status", table_name="autonomy_approvals")
    op.drop_index("ix_autonomy_approvals_run_id", table_name="autonomy_approvals")
    op.drop_index("ix_autonomy_approvals_owner_id", table_name="autonomy_approvals")
    op.drop_table("autonomy_approvals")

    op.drop_index("ix_autonomy_decisions_run_id", table_name="autonomy_decisions")
    op.drop_index("ix_autonomy_decisions_owner_id", table_name="autonomy_decisions")
    op.drop_table("autonomy_decisions")

    op.drop_index("ix_autonomy_runs_workflow_id", table_name="autonomy_runs")
    op.drop_index("ix_autonomy_runs_status", table_name="autonomy_runs")
    op.drop_index("ix_autonomy_runs_owner_id", table_name="autonomy_runs")
    op.drop_table("autonomy_runs")

    op.drop_index("ix_autonomy_configs_owner_id", table_name="autonomy_configs")
    op.drop_table("autonomy_configs")
