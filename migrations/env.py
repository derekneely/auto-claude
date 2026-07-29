"""Alembic environment for auto-claude's `auto_claude` Postgres schema.

Two things this file must get right (docs/plans/12-shared-state-in-postgres.md,
"Migrations"):

1. `version_table_schema="auto_claude"` keeps Alembic's own bookkeeping out of
   `public`, where the Node toolchain's `pipeline_events` /
   `pipeline_schema_migrations` already live — the two migration systems must
   never collide.
2. `CREATE SCHEMA IF NOT EXISTS auto_claude` has to run before Alembic's own
   configure step, because that step immediately tries to read the version
   table out of the `auto_claude` schema. Revision 0001 also issues the same
   CREATE SCHEMA, but by the time it runs, configure has already needed the
   schema to exist — so the CREATE here is not redundant, it is what makes
   the very first `alembic upgrade head` possible at all.

Connection string: PIPELINE_METRICS_DATABASE_URL — the same variable
`integrations.py` and `config.DatabaseConfig.url_env`'s default read. Moving
that database moves auto-claude's operational state with it.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    url = os.environ.get("PIPELINE_METRICS_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "PIPELINE_METRICS_DATABASE_URL is not set — Alembic needs it to "
            "reach the database. Set it in .env or the environment."
        )
    return url


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        # Must run before context.configure(): configure() immediately tries
        # to read the version table out of `auto_claude`, which does not
        # exist yet on a virgin database.
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS auto_claude"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="auto_claude",
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        version_table_schema="auto_claude",
        include_schemas=True,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
