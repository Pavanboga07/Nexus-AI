"""M7: durable job queue.

Revision ID: 0014_jobs
Revises: 0013_multi_agent
Create Date: 2026-09-20

Before this revision there was NO durable asynchronous work and no retry
machinery anywhere in the codebase:

  * work happened inline in a request, or in a fire-and-forget
    ``asyncio.create_task`` whose failure was logged and lost;
  * a transient failure was terminal - no retry with backoff existed in the A2A
    or LLM paths;
  * a retried request could double-execute side effects because nothing
    carried an idempotency key;
  * and a workflow interrupted mid-run had no mechanism to resume.

Creates ``jobs`` with:

  * a UNIQUE ``idempotency_key`` so enqueueing the same logical work twice is a
    no-op. PostgreSQL treats NULL as distinct, so jobs without a key are never
    deduplicated - correct, since only the caller knows what "the same work"
    means.
  * ``ix_jobs_claimable (state, run_after)`` for the claim query, which uses
    ``FOR UPDATE SKIP LOCKED`` so two workers never take the same job.
  * a lease (``lease_expires_at``) rather than a lock held across the work, so
    a crashed worker's job becomes claimable again instead of sitting RUNNING
    for ever.
  * ``error_history`` so a dead-lettered job can be diagnosed without trawling
    logs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_jobs"
down_revision: Union[str, None] = "0013_multi_agent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "jobs" in set(inspector.get_table_names()):
        return  # already present (e.g. created by create_all in tests)

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column(
            "state", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")
        ),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "error_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("ix_jobs_claimable", "jobs", ["state", "run_after"])
    op.create_index("ix_jobs_kind_state", "jobs", ["kind", "state"])
    op.create_index("ix_jobs_owner", "jobs", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_owner", table_name="jobs")
    op.drop_index("ix_jobs_kind_state", table_name="jobs")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_table("jobs")
