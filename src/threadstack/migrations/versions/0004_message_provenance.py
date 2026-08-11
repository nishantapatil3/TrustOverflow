"""Add signed message provenance fields."""

from __future__ import annotations

from alembic import op

revision = "0004_message_provenance"
down_revision = "0003_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE threads ADD COLUMN IF NOT EXISTS signature TEXT;
        ALTER TABLE threads ADD COLUMN IF NOT EXISTS payload_hash TEXT;
        ALTER TABLE threads ADD COLUMN IF NOT EXISTS signature_valid BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE posts ADD COLUMN IF NOT EXISTS signature TEXT;
        ALTER TABLE posts ADD COLUMN IF NOT EXISTS payload_hash TEXT;
        ALTER TABLE posts ADD COLUMN IF NOT EXISTS signature_valid BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE INDEX IF NOT EXISTS threads_payload_hash_idx ON threads (payload_hash);
        CREATE INDEX IF NOT EXISTS posts_payload_hash_idx ON posts (payload_hash);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS posts_payload_hash_idx;
        DROP INDEX IF EXISTS threads_payload_hash_idx;
        ALTER TABLE posts DROP COLUMN IF EXISTS signature_valid;
        ALTER TABLE posts DROP COLUMN IF EXISTS payload_hash;
        ALTER TABLE posts DROP COLUMN IF EXISTS signature;
        ALTER TABLE threads DROP COLUMN IF EXISTS signature_valid;
        ALTER TABLE threads DROP COLUMN IF EXISTS payload_hash;
        ALTER TABLE threads DROP COLUMN IF EXISTS signature;
        """
    )
