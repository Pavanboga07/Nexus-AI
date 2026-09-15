"""Part 3: agent identity table.

Revision ID: 0002_part3_identity
Revises: 0001_part2_memory
Create Date: 2026-09-14

Adds the agent_identities table: one Ed25519 identity per owner, private key
stored only as AES-GCM ciphertext. Unique constraints enforce one identity
per owner, one row per agent_id, and one row per public key.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002_part3_identity"
down_revision: Union[str, None] = "0001_part2_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_identities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("encrypted_private_key", sa.Text(), nullable=False),
        sa.Column(
            "key_algorithm", sa.String(32), nullable=False, server_default="Ed25519"
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
        sa.UniqueConstraint("owner_id", name="uq_agent_identities_owner"),
        sa.UniqueConstraint("agent_id", name="uq_agent_identities_agent_id"),
        sa.UniqueConstraint("public_key", name="uq_agent_identities_public_key"),
    )
    op.create_index(
        "ix_agent_identities_owner_id", "agent_identities", ["owner_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_identities_owner_id", table_name="agent_identities")
    op.drop_table("agent_identities")
