"""Add rotating refresh tokens and revocable session families."""

from __future__ import annotations

from alembic import op

revision = "0005_session_hardening"
down_revision = "0004_message_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS refresh_digest CHAR(64);
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS refresh_expires_at TIMESTAMPTZ;
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS family_id UUID;
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
        CREATE UNIQUE INDEX IF NOT EXISTS sessions_refresh_digest_idx
            ON sessions (refresh_digest)
            WHERE refresh_digest IS NOT NULL;
        CREATE INDEX IF NOT EXISTS sessions_family_id_idx ON sessions (family_id);
        CREATE INDEX IF NOT EXISTS sessions_agent_revoked_idx
            ON sessions (agent_id, revoked_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS sessions_agent_revoked_idx;
        DROP INDEX IF EXISTS sessions_family_id_idx;
        DROP INDEX IF EXISTS sessions_refresh_digest_idx;
        ALTER TABLE sessions DROP COLUMN IF EXISTS revoked_at;
        ALTER TABLE sessions DROP COLUMN IF EXISTS family_id;
        ALTER TABLE sessions DROP COLUMN IF EXISTS refresh_expires_at;
        ALTER TABLE sessions DROP COLUMN IF EXISTS refresh_digest;
        """
    )
