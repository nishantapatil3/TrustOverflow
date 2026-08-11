"""Add automatic content quarantine state."""

from __future__ import annotations

from alembic import op

revision = "0006_content_quarantine"
down_revision = "0005_session_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE threads ADD COLUMN IF NOT EXISTS moderation_status TEXT NOT NULL
            DEFAULT 'visible';
        ALTER TABLE threads ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;
        ALTER TABLE posts ADD COLUMN IF NOT EXISTS moderation_status TEXT NOT NULL
            DEFAULT 'visible';
        ALTER TABLE posts ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;
        CREATE INDEX IF NOT EXISTS threads_moderation_status_idx
            ON threads (moderation_status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS posts_moderation_status_idx
            ON posts (moderation_status, created_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS posts_moderation_status_idx;
        DROP INDEX IF EXISTS threads_moderation_status_idx;
        ALTER TABLE posts DROP COLUMN IF EXISTS quarantine_reason;
        ALTER TABLE posts DROP COLUMN IF EXISTS moderation_status;
        ALTER TABLE threads DROP COLUMN IF EXISTS quarantine_reason;
        ALTER TABLE threads DROP COLUMN IF EXISTS moderation_status;
        """
    )
