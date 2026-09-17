"""M4: one owner, MANY agents - the agents entity + key rotation.

Revision ID: 0013_multi_agent
Revises: 0012_auth
Create Date: 2026-09-20

The finding: ``agent_identities`` carried ``UniqueConstraint("owner_id")``,
so the database allowed exactly ONE agent per owner. That constraint is the
structural reason the audit's "can one owner have multiple agents?" question
had a hard *no*, and why "multi-agent" could only ever mean "my one agent
talks to your one agent".

This migration uses EXPAND-CONTRACT rather than a single destructive step:

  EXPAND     create ``agents``; add ``agent_row_id``, ``not_before``,
             ``not_after``, ``revoked_at``, ``revoked_reason`` to
             ``agent_identities``
  BACKFILL   one ``agents`` row per existing identity row (preserving the
             existing agent_id and display name), then link the key to it
  CONTRACT   drop ``uq_agent_identities_owner`` - the constraint that
             forbade many agents per owner

Backfill happens inside the migration so there is never a window in which an
existing deployment has keys with no agent. ``downgrade`` re-adds the
constraint only if it is still satisfiable, so a rollback cannot silently
truncate an owner's agents.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0013_multi_agent"
down_revision: Union[str, None] = "0012_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _unique_constraint_names(inspector, table: str) -> list[tuple[str, list[str]]]:
    return [
        (c["name"], list(c.get("column_names") or []))
        for c in inspector.get_unique_constraints(table)
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # --- EXPAND -----------------------------------------------------------
    if "agents" not in tables:
        op.create_table(
            "agents",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "owner_id",
                UUID(as_uuid=True),
                sa.ForeignKey("owners.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("agent_id", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("handle", sa.String(64), nullable=True),
            sa.Column("endpoint", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="active",
            ),
            sa.Column(
                "is_primary",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
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
            sa.UniqueConstraint("agent_id", name="uq_agents_agent_id"),
            sa.UniqueConstraint("owner_id", "handle", name="uq_agents_owner_handle"),
        )
        op.create_index("ix_agents_owner", "agents", ["owner_id"])

    identity_cols = _columns(inspector, "agent_identities")
    if "agent_row_id" not in identity_cols:
        op.add_column(
            "agent_identities",
            sa.Column(
                "agent_row_id",
                UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_agent_identities_agent_row", "agent_identities", ["agent_row_id"]
        )
    if "not_before" not in identity_cols:
        op.add_column(
            "agent_identities",
            sa.Column(
                "not_before",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
    if "not_after" not in identity_cols:
        op.add_column(
            "agent_identities",
            sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        )
    if "revoked_at" not in identity_cols:
        op.add_column(
            "agent_identities",
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "revoked_reason" not in identity_cols:
        op.add_column(
            "agent_identities",
            sa.Column("revoked_reason", sa.String(255), nullable=True),
        )

    # --- BACKFILL ---------------------------------------------------------
    # One agents row per existing identity, preserving identity and naming.
    # Done in SQL so it is atomic with the schema change and needs no app code.
    op.execute(
        """
        INSERT INTO agents (id, owner_id, agent_id, display_name, handle, endpoint,
                            status, is_primary, created_at, updated_at)
        SELECT gen_random_uuid(),
               ai.owner_id,
               ai.agent_id,
               COALESCE(o.name, 'Nexus Agent'),
               NULL,
               NULL,
               'active',
               true,
               COALESCE(ai.created_at, now()),
               now()
        FROM agent_identities ai
        JOIN owners o ON o.id = ai.owner_id
        WHERE ai.agent_row_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE agent_identities ai
        SET agent_row_id = a.id
        FROM agents a
        WHERE ai.agent_row_id IS NULL
          AND a.agent_id = ai.agent_id
        """
    )

    # --- CONTRACT ---------------------------------------------------------
    # The constraint that forbade many agents per owner.
    for name, cols in _unique_constraint_names(inspector, "agent_identities"):
        if cols == ["owner_id"]:
            op.drop_constraint(name, "agent_identities", type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Only restore one-agent-per-owner if the data actually satisfies it;
    # otherwise the ALTER would fail on a legitimate multi-agent deployment.
    duplicates = bind.execute(
        sa.text(
            """
            SELECT owner_id, COUNT(*) AS n
            FROM agent_identities
            GROUP BY owner_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).fetchone()
    if duplicates is None:
        existing = {name for name, _ in _unique_constraint_names(inspector, "agent_identities")}
        if "uq_agent_identities_owner" not in existing:
            op.create_unique_constraint(
                "uq_agent_identities_owner", "agent_identities", ["owner_id"]
            )

    inspector = sa.inspect(bind)
    cols = _columns(inspector, "agent_identities")
    for col in ("revoked_reason", "revoked_at", "not_after", "not_before"):
        if col in cols:
            op.drop_column("agent_identities", col)
    if "agent_row_id" in cols:
        op.drop_index("ix_agent_identities_agent_row", table_name="agent_identities")
        op.drop_column("agent_identities", "agent_row_id")
    if "agents" in set(inspector.get_table_names()):
        op.drop_index("ix_agents_owner", table_name="agents")
        op.drop_table("agents")
