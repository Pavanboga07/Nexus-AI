"""M3: authentication - user credentials and revocable sessions.

Revision ID: 0012_auth
Revises: 0011_verified_agent_cards
Create Date: 2026-09-20

Before this revision the API had NO authentication. ``owners`` was a
single implicit principal resolved once at startup, so there were no
credentials to check and no way to distinguish callers.

Adds:
  * ``user_credentials`` - login methods per owner (password now, OIDC later).
    Kept separate from ``owners`` so the core schema stays free of auth
    concerns and a second login method does not require a schema rewrite.
  * ``auth_sessions``    - issued sessions, revocable server-side. Stores only
    a token FINGERPRINT (never the token), so a database leak does not yield
    usable sessions.

The existing ``owners`` row is deliberately left in place: the first
registered user adopts it (see AuthService.register with
``adopt_existing_owner_id``), so pre-auth memory, conversations, identity and
policies are not orphaned.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0012_auth"
down_revision: Union[str, None] = "0011_verified_agent_cards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "user_credentials" not in tables:
        op.create_table(
            "user_credentials",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "owner_id",
                UUID(as_uuid=True),
                sa.ForeignKey("owners.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(16), nullable=False, server_default="password"),
            sa.Column("identifier", sa.String(320), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=True),
            sa.Column("issuer", sa.String(255), nullable=True),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
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
            sa.UniqueConstraint(
                "owner_id", "kind", name="uq_user_credentials_owner_kind"
            ),
        )
        op.create_index(
            "ix_user_credentials_owner_id", "user_credentials", ["owner_id"]
        )
        op.create_index(
            "ix_user_credentials_identifier", "user_credentials", ["identifier"]
        )
        # At most one password credential per identifier, case-insensitively.
        # Enforced in the database so two concurrent registrations cannot both
        # succeed.
        op.execute(
            "CREATE UNIQUE INDEX uq_user_credentials_password_identifier "
            "ON user_credentials (lower(identifier)) WHERE kind = 'password'"
        )

    if "auth_sessions" not in tables:
        op.create_table(
            "auth_sessions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "owner_id",
                UUID(as_uuid=True),
                sa.ForeignKey("owners.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token_fingerprint", sa.String(64), nullable=False),
            sa.Column("token_id", sa.String(64), nullable=False),
            sa.Column("user_agent", sa.String(512), nullable=True),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "token_fingerprint", name="uq_auth_sessions_fingerprint"
            ),
        )
        op.create_index("ix_auth_sessions_owner", "auth_sessions", ["owner_id"])
        op.create_index("ix_auth_sessions_expires", "auth_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_expires", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_owner", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.execute("DROP INDEX IF EXISTS uq_user_credentials_password_identifier")
    op.drop_index("ix_user_credentials_identifier", table_name="user_credentials")
    op.drop_index("ix_user_credentials_owner_id", table_name="user_credentials")
    op.drop_table("user_credentials")
