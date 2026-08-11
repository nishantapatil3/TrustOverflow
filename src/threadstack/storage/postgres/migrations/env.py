from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

def _database_url() -> str:
    configured_url = config.attributes.get("database_url") or config.get_main_option(
        "sqlalchemy.url"
    ) or os.getenv("DATABASE_URL")
    if not configured_url:
        raise RuntimeError("DATABASE_URL is required to run database migrations")
    if configured_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + configured_url.removeprefix("postgresql://")
    return configured_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=None)

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
