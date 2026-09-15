"""Part 6: A2A trusted agents, tasks, and message records.

Revision ID: 0005_part6_a2a
Revises: 0004_part5_tools
Create Date: 2026-09-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005_part6_a2a"
down_revision: Union[str, None] = "0004_part5_tools"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trusted_agents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
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
        sa.UniqueConstraint("owner_id", "agent_id", name="uq_trusted_agents_owner_agent"),
    )
    op.create_index("ix_trusted_agents_owner_id", "trusted_agents", ["owner_id"])

    op.create_table(
        "a2a_tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("sender_agent_id", sa.String(255), nullable=False),
        sa.Column("recipient_agent_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "task_id", name="uq_a2a_tasks_owner_task"),
    )
    op.create_index("ix_a2a_tasks_owner_id", "a2a_tasks", ["owner_id"])

    op.create_table(
        "a2a_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("sender_agent_id", sa.String(255), nullable=False),
        sa.Column("recipient_agent_id", sa.String(255), nullable=False),
        sa.Column("message_type", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("policy_decision", sa.String(16), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "message_id", name="uq_a2a_messages_owner_message"),
    )
    op.create_index("ix_a2a_messages_owner_id", "a2a_messages", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_a2a_messages_owner_id", table_name="a2a_messages")
    op.drop_table("a2a_messages")
    op.drop_index("ix_a2a_tasks_owner_id", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
    op.drop_index("ix_trusted_agents_owner_id", table_name="trusted_agents")
    op.drop_table("trusted_agents")
