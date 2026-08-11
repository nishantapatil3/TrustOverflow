"""Add one-time password reset tokens."""

from __future__ import annotations

from alembic import op

revision = "0010_password_reset_tokens"
down_revision = "0009_semantic_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token_digest TEXT PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS password_reset_tokens_user_id_idx
            ON password_reset_tokens (user_id);
        CREATE INDEX IF NOT EXISTS password_reset_tokens_expires_at_idx
            ON password_reset_tokens (expires_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS password_reset_tokens")
