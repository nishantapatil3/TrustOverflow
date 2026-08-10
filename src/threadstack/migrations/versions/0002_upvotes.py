"""Add agent upvotes for threads and posts."""

from __future__ import annotations

from alembic import op

revision = "0002_upvotes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS upvotes (
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            thread_id UUID REFERENCES threads(id) ON DELETE CASCADE,
            post_id UUID REFERENCES posts(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT upvotes_one_target_check CHECK (num_nonnulls(thread_id, post_id) = 1),
            CONSTRAINT upvotes_agent_thread_unique UNIQUE (agent_id, thread_id),
            CONSTRAINT upvotes_agent_post_unique UNIQUE (agent_id, post_id)
        );

        CREATE INDEX IF NOT EXISTS upvotes_thread_id_idx ON upvotes (thread_id);
        CREATE INDEX IF NOT EXISTS upvotes_post_id_idx ON upvotes (post_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS upvotes")
