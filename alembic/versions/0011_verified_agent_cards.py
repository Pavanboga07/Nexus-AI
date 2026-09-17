"""Part 7 / M1: verified agent card cache.

Revision ID: 0011_verified_agent_cards
Revises: 0010_part13_integration
Create Date: 2026-09-20

Adds ``trusted_agent_cards``: a cache of VERIFIED agent cards.

Why this exists
---------------
Capability discovery previously had nowhere to store a remote agent's card.
``trusted_agents`` holds only agent_id/public_key/display_name/endpoint, and
``DiscoveryService.discover_and_register`` verified a card and then discarded
it. As a result:
  * capability discovery was impossible without re-fetching every peer, and
  * ``TargetResolver``'s "known cards" fallback called a ``list_known_cards``
    method that did not exist, so it silently returned nothing.

Only ``DiscoveryService.register_verified_card`` writes to this table, and it
writes only after signature, agent_id<->public_key consistency and the time
window have been verified. That is what lets readers treat a cached card as
attested instead of raw directory metadata.

``card_expires_at`` mirrors the card's own ``expires_at`` so stale cards can be
excluded from lookups with an indexed comparison.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0011_verified_agent_cards"
down_revision: Union[str, None] = "0010_part13_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "trusted_agent_cards" in inspector.get_table_names():
        return  # already present (e.g. created by create_all in tests)

    op.create_table(
        "trusted_agent_cards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("owners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column(
            "card",
            sa.JSON().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("card_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "owner_id", "agent_id", name="uq_trusted_agent_cards_owner_agent"
        ),
    )
    op.create_index(
        "ix_trusted_agent_cards_owner_id",
        "trusted_agent_cards",
        ["owner_id"],
    )
    op.create_index(
        "ix_trusted_agent_cards_expires",
        "trusted_agent_cards",
        ["card_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trusted_agent_cards_expires", table_name="trusted_agent_cards")
    op.drop_index("ix_trusted_agent_cards_owner_id", table_name="trusted_agent_cards")
    op.drop_table("trusted_agent_cards")
