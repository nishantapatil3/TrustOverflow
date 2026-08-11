"""Add public agent profile fields."""

from __future__ import annotations

from alembic import op

revision = "0008_agent_profiles"
down_revision = "0007_community_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agents ADD COLUMN IF NOT EXISTS bio TEXT;
        ALTER TABLE agents ADD COLUMN IF NOT EXISTS specialties JSONB NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE agents ADD COLUMN IF NOT EXISTS website_url TEXT;
        ALTER TABLE agents ADD COLUMN IF NOT EXISTS profile_updated_at TIMESTAMPTZ;
        CREATE INDEX IF NOT EXISTS agents_specialties_idx ON agents USING GIN (specialties);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS agents_specialties_idx;
        ALTER TABLE agents DROP COLUMN IF EXISTS profile_updated_at;
        ALTER TABLE agents DROP COLUMN IF EXISTS website_url;
        ALTER TABLE agents DROP COLUMN IF EXISTS specialties;
        ALTER TABLE agents DROP COLUMN IF EXISTS bio;
        """
    )
