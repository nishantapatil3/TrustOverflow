"""Add community ownership, memberships, moderators, and subscriptions."""

from __future__ import annotations

from alembic import op

revision = "0007_community_permissions"
down_revision = "0006_content_quarantine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE communities ADD COLUMN IF NOT EXISTS owner_id UUID;
        UPDATE communities SET owner_id = created_by WHERE owner_id IS NULL;

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'communities_owner_id_fkey'
            ) THEN
                ALTER TABLE communities
                    ADD CONSTRAINT communities_owner_id_fkey
                    FOREIGN KEY (owner_id) REFERENCES agents(id) ON DELETE SET NULL;
            END IF;
        END $$;

        CREATE INDEX IF NOT EXISTS communities_owner_id_idx ON communities (owner_id);

        CREATE TABLE IF NOT EXISTS community_memberships (
            community_id UUID NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'member',
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (community_id, agent_id),
            CONSTRAINT community_membership_role_check
                CHECK (role IN ('owner', 'moderator', 'member'))
        );
        CREATE INDEX IF NOT EXISTS community_memberships_agent_idx
            ON community_memberships (agent_id, created_at);

        CREATE TABLE IF NOT EXISTS community_subscriptions (
            community_id UUID NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (community_id, agent_id)
        );
        CREATE INDEX IF NOT EXISTS community_subscriptions_agent_idx
            ON community_subscriptions (agent_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS community_subscriptions;
        DROP TABLE IF EXISTS community_memberships;
        DROP INDEX IF EXISTS communities_owner_id_idx;
        ALTER TABLE communities DROP CONSTRAINT IF EXISTS communities_owner_id_fkey;
        ALTER TABLE communities DROP COLUMN IF EXISTS owner_id;
        """
    )
