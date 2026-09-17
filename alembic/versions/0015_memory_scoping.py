"""M8: memory agent scoping, retention pin, and the vector index.

Revision ID: 0015_memory_scoping
Revises: 0014_jobs
Create Date: 2026-09-20

Three findings this addresses:

1. **No vector index.** ``memories.embedding`` is a dimensionless ``vector``
   and no index existed, so every retrieval was an EXACT cosine scan over the
   owner's memories - correct at thousands, not at millions. An HNSW index
   requires a pinned dimension, which the migration cannot choose for you
   (it depends on the configured embedder: 256 for the local hash embedder,
   1536 for text-embedding-3-small). ``scripts/build_vector_index.py`` builds it
   with the dimension you choose, and reports what it did.

2. **No agent scoping.** Memory was owner-scoped only. With many agents per
   owner (M4) that means every agent can read every other agent's memories.
   ``agent_id`` is nullable so pre-M8 rows read as owner-scoped rather than
   becoming invisible.

3. **Unbounded growth.** Extraction runs on every chat turn and nothing ever
   removed a memory. ``pinned`` gives the owner an explicit "keep this" signal
   that retention must respect.

Deliberately NOT changed here: the embedding column's type. Altering it to a
fixed typmod would reject existing rows written with a different dimension and
lock a large table; ``scripts/build_vector_index.py`` verifies before it acts.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0015_memory_scoping"
down_revision: Union[str, None] = "0014_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _index_names(inspector, table: str) -> set[str]:
    return {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "memories" not in tables:
        return

    cols = _columns(inspector, "memories")
    if "agent_id" not in cols:
        op.add_column(
            "memories",
            sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
        )
    if "pinned" not in cols:
        op.add_column(
            "memories",
            sa.Column(
                "pinned",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    indexes = _index_names(inspector, "memories")
    if "ix_memories_agent_id" not in indexes:
        op.create_index("ix_memories_agent_id", "memories", ["agent_id"])
    if "ix_memories_owner_agent" not in indexes:
        op.create_index(
            "ix_memories_owner_agent", "memories", ["owner_id", "agent_id"]
        )
    # Supports the retention sweep: old, unpinned rows.
    if "ix_memories_created_pinned" not in indexes:
        op.create_index(
            "ix_memories_created_pinned", "memories", ["created_at", "pinned"]
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "memories" not in set(inspector.get_table_names()):
        return
    indexes = _index_names(inspector, "memories")
    for name in ("ix_memories_created_pinned", "ix_memories_owner_agent",
                 "ix_memories_agent_id"):
        if name in indexes:
            op.drop_index(name, table_name="memories")
    cols = _columns(inspector, "memories")
    if "pinned" in cols:
        op.drop_column("memories", "pinned")
    if "agent_id" in cols:
        op.drop_column("memories", "agent_id")
