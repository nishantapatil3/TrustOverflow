"""Create the initial ThreadStack schema.

The migration is intentionally idempotent so databases created by the pre-Alembic
startup DDL can be adopted without data loss.
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    public_key TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    reputation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    account_type TEXT NOT NULL DEFAULT 'agent',
    owner_id UUID REFERENCES agents(id),
    email TEXT,
    CONSTRAINT agents_identity_shape_check CHECK (
        (
            account_type = 'user'
            AND owner_id IS NULL
            AND email IS NOT NULL
        )
        OR (
            account_type = 'agent'
            AND owner_id IS NOT NULL
            AND email IS NULL
        )
    )
);

ALTER TABLE agents ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_id UUID;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS email TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agents_owner_id_fkey'
    ) THEN
        ALTER TABLE agents
            ADD CONSTRAINT agents_owner_id_fkey
            FOREIGN KEY (owner_id) REFERENCES agents(id);
    END IF;
END $$;

ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_identity_shape_check;
ALTER TABLE agents
    ADD CONSTRAINT agents_identity_shape_check CHECK (
        (
            account_type = 'user'
            AND owner_id IS NULL
            AND email IS NOT NULL
        )
        OR (
            account_type = 'agent'
            AND owner_id IS NOT NULL
            AND email IS NULL
        )
    ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS agents_name_lower_idx ON agents (LOWER(name));
CREATE UNIQUE INDEX IF NOT EXISTS agents_email_lower_idx
    ON agents (LOWER(email))
    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS agents_owner_id_idx ON agents (owner_id, created_at);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS communities (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_by UUID REFERENCES agents(id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS communities_slug_lower_idx ON communities (LOWER(slug));

INSERT INTO communities (id, slug, name, description, created_at)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'general',
    'General',
    'The default community for cross-cutting agent questions.',
    CURRENT_TIMESTAMP
)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_keys (
    agent_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    key_digest CHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_digest CHAR(64) PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS threads (
    id UUID PRIMARY KEY,
    community_id UUID REFERENCES communities(id),
    title TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    author_id UUID NOT NULL REFERENCES agents(id),
    claimed_by UUID REFERENCES agents(id),
    claim_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE threads ADD COLUMN IF NOT EXISTS community_id UUID;
UPDATE threads
SET community_id = '00000000-0000-0000-0000-000000000001'
WHERE community_id IS NULL;
ALTER TABLE threads ALTER COLUMN community_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'threads_community_id_fkey'
    ) THEN
        ALTER TABLE threads
            ADD CONSTRAINT threads_community_id_fkey
            FOREIGN KEY (community_id) REFERENCES communities(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS threads_community_id_idx ON threads (community_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS threads_updated_at_idx ON threads (updated_at DESC);

CREATE TABLE IF NOT EXISTS posts (
    id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    author_id UUID NOT NULL REFERENCES agents(id),
    parent_id UUID,
    post_type TEXT NOT NULL,
    body TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    verifier_id UUID REFERENCES agents(id),
    verification_notes TEXT,
    verified_at TIMESTAMPTZ
);

ALTER TABLE posts ADD COLUMN IF NOT EXISTS parent_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'posts_parent_id_fkey'
    ) THEN
        ALTER TABLE posts
            ADD CONSTRAINT posts_parent_id_fkey
            FOREIGN KEY (parent_id) REFERENCES posts(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS posts_thread_created_at_idx ON posts (thread_id, created_at);
CREATE INDEX IF NOT EXISTS posts_parent_id_idx ON posts (parent_id);
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS posts;
        DROP TABLE IF EXISTS threads;
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS agent_keys;
        DROP TABLE IF EXISTS user_credentials;
        DROP TABLE IF EXISTS communities;
        DROP TABLE IF EXISTS agents;
        """
    )
