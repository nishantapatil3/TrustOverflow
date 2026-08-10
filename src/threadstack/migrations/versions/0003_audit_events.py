"""Add append-only mutation audit events."""

from __future__ import annotations

from alembic import op

revision = "0003_audit_events"
down_revision = "0002_upvotes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            actor_id UUID REFERENCES agents(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id UUID,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE INDEX IF NOT EXISTS audit_events_actor_created_at_idx
            ON audit_events (actor_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS audit_events_target_created_at_idx
            ON audit_events (target_type, target_id, created_at DESC);

        CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$;

        DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
        CREATE TRIGGER audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
        DROP FUNCTION IF EXISTS reject_audit_event_mutation();
        DROP TABLE IF EXISTS audit_events;
        """
    )
