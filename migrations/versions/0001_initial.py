"""initial auto_claude schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-29

Creates the `auto_claude` schema and its four tables exactly as frozen in
docs/plans/12-shared-state-in-postgres.md and CONTRACT.md. Raw DDL via
`op.execute` — no SQLAlchemy Table objects. Alembic is the only consumer in
this project that needs SQLAlchemy at all; everything else is raw psycopg3.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auto_claude")

    op.execute("""
        CREATE TABLE auto_claude.harness (
            id           text PRIMARY KEY,
            hostname     text NOT NULL,
            pid          integer NOT NULL,
            version      text NOT NULL,
            started_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE auto_claude.issue_state (
            issue_id            text PRIMARY KEY,
            repo                text NOT NULL,
            number              integer NOT NULL,
            title               text NOT NULL DEFAULT '',
            stage               text,
            kind                text,
            mode                text NOT NULL DEFAULT 'dev',
            branch              text,
            pr_url              text,
            triage_attempts     integer NOT NULL DEFAULT 0,
            rework_count        integer NOT NULL DEFAULT 0,
            continuation_count  integer NOT NULL DEFAULT 0,
            last_error          text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            owner_harness_id    text REFERENCES auto_claude.harness(id) ON DELETE SET NULL,
            lease_expires_at    timestamptz,
            heartbeat_at        timestamptz
        )
    """)
    op.execute(
        "CREATE INDEX issue_state_lease_idx "
        "ON auto_claude.issue_state (owner_harness_id, lease_expires_at)"
    )

    op.execute("""
        CREATE TABLE auto_claude.run (
            id               text PRIMARY KEY,
            issue_id         text NOT NULL REFERENCES auto_claude.issue_state(issue_id) ON DELETE CASCADE,
            harness_id       text REFERENCES auto_claude.harness(id) ON DELETE SET NULL,
            mode             text NOT NULL,
            model            text,
            started_at       timestamptz NOT NULL DEFAULT now(),
            ended_at         timestamptz,
            outcome          text,
            exit_code        integer,
            duration_seconds integer,
            cost_usd         numeric(10,4),
            turns            integer,
            crash_log_path   text
        )
    """)
    op.execute(
        "CREATE INDEX run_issue_idx ON auto_claude.run (issue_id, started_at DESC)"
    )

    op.execute("""
        CREATE TABLE auto_claude.summary (
            id          text PRIMARY KEY,
            issue_id    text NOT NULL REFERENCES auto_claude.issue_state(issue_id) ON DELETE CASCADE,
            run_id      text REFERENCES auto_claude.run(id) ON DELETE SET NULL,
            kind        text NOT NULL,
            body        text NOT NULL,
            comment_url text,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX summary_issue_idx ON auto_claude.summary (issue_id, created_at DESC)"
    )


def downgrade() -> None:
    # Deliberately do NOT drop the `auto_claude` schema itself: with
    # version_table_schema="auto_claude", Alembic's own bookkeeping
    # (`auto_claude.alembic_version`) lives inside it too, and after
    # downgrade() returns, Alembic issues a DELETE against that table in the
    # *same* transaction. Postgres makes DDL catalog changes visible to later
    # statements in the same transaction, so a DROP SCHEMA ... CASCADE here
    # would drop alembic_version out from under that DELETE and abort the
    # whole downgrade. Drop only what this revision created, in dependency
    # order (children before parents), and leave the empty schema in place
    # for Alembic to finish its own bookkeeping against.
    op.execute("DROP TABLE auto_claude.summary")
    op.execute("DROP TABLE auto_claude.run")
    op.execute("DROP TABLE auto_claude.issue_state")
    op.execute("DROP TABLE auto_claude.harness")
