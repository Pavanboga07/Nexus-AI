"""Part 8: Agent task delegation and negotiation columns.

Revision ID: 0006_part8_tasks
Revises: 0005_part6_a2a
Create Date: 2026-09-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_part8_tasks"
down_revision: Union[str, None] = "0005_part6_a2a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "a2a_tasks",
        sa.Column("task_type", sa.String(64), nullable=True),
    )
    op.add_column(
        "a2a_tasks",
        sa.Column("purpose", sa.String(64), nullable=True),
    )
    op.add_column(
        "a2a_tasks",
        sa.Column("request_payload", sa.JSON(), nullable=True),
    )
    op.add_column(
        "a2a_tasks",
        sa.Column("response_payload", sa.JSON(), nullable=True),
    )
    op.add_column(
        "a2a_tasks",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "a2a_tasks",
        sa.Column("failure_reason", sa.String(255), nullable=True),
    )
    op.add_column(
        "a2a_tasks",
        sa.Column(
            "negotiation_round",
            sa.Integer(),
            nullable=True,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("a2a_tasks", "negotiation_round")
    op.drop_column("a2a_tasks", "failure_reason")
    op.drop_column("a2a_tasks", "completed_at")
    op.drop_column("a2a_tasks", "response_payload")
    op.drop_column("a2a_tasks", "request_payload")
    op.drop_column("a2a_tasks", "purpose")
    op.drop_column("a2a_tasks", "task_type")
