"""Add full-text search and an optional pgvector index."""

from __future__ import annotations

from alembic import op

revision = "0009_semantic_search"
down_revision = "0008_agent_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE threads ADD COLUMN IF NOT EXISTS search_vector TSVECTOR;
        CREATE INDEX IF NOT EXISTS threads_search_vector_idx
            ON threads USING GIN (search_vector);
        UPDATE threads AS thread
        SET search_vector = to_tsvector(
            'simple',
            concat_ws(
                ' ',
                thread.title,
                thread.tags::text,
                (
                    SELECT string_agg(post.body, ' ' ORDER BY post.created_at)
                    FROM posts AS post
                    WHERE post.thread_id = thread.id
                      AND post.moderation_status = 'visible'
                )
            )
        );
        DO $$
        BEGIN
            BEGIN
                CREATE EXTENSION IF NOT EXISTS vector;
            EXCEPTION
                WHEN undefined_file OR feature_not_supported OR insufficient_privilege THEN
                    RETURN;
            END;
            EXECUTE 'ALTER TABLE threads ADD COLUMN IF NOT EXISTS semantic_embedding vector(128)';
            BEGIN
                EXECUTE 'CREATE INDEX IF NOT EXISTS threads_semantic_embedding_idx '
                    'ON threads USING hnsw (semantic_embedding vector_cosine_ops)';
            EXCEPTION
                WHEN undefined_object THEN
                    NULL;
            END;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS threads_semantic_embedding_idx;
        ALTER TABLE threads DROP COLUMN IF EXISTS semantic_embedding;
        DROP INDEX IF EXISTS threads_search_vector_idx;
        ALTER TABLE threads DROP COLUMN IF EXISTS search_vector;
        """
    )
