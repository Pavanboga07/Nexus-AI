"""Part 5: tool execution audit table.

Revision ID: 0004_part5_tools
Revises: 0003_part4_policy
Create Date: 2026-09-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004_part5_tools"
down_revision: Union[str, None] = "0003_part4_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("policy_decision", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tool_executions_owner_created",
        "tool_executions",
        ["owner_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_executions_owner_created", table_name="tool_executions"
    )
    op.drop_table("tool_executions")
