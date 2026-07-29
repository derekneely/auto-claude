"""Structural checks on the Alembic scaffold.

No real database here — that is Step 6 below, run once by hand against the
live Supabase instance, not on every `pytest` run. This file guards the one
trap docs/plans/12-shared-state-in-postgres.md calls out by name:
`version_table_schema="auto_claude"` means Alembic reads its version table out
of a schema that must already exist, so `CREATE SCHEMA IF NOT EXISTS
auto_claude` has to run in migrations/env.py *before* `context.configure(...)`
executes — not inside revision 0001, where it would be too late for
`context.configure` itself.

Also guards three bugs a controller-run live migration surfaced in review
(structural checks only — still no real database, see module note above):

1. A bare `postgresql://` URL resolves to the psycopg2 dialect in
   SQLAlchemy's dialect registry, but this project pins psycopg 3 only —
   `create_engine()` needs the driver forced to `postgresql+psycopg`.
2. `downgrade()` must not `DROP SCHEMA auto_claude` outright: with
   `version_table_schema="auto_claude"`, Alembic deletes its own
   `alembic_version` row in that schema in the same transaction right after
   `downgrade()` returns, and Postgres would abort that DELETE against a
   schema just dropped in the same transaction. `downgrade()` must drop only
   the four tables it created and leave the (now empty) schema behind.
3. `env.py` must load the gitignored `.env` (via `ghauth.load_dotenv`, same
   as `main.py`) so `alembic upgrade head` — the remedy Task 5's startup gate
   prints on a stale schema — works in a fresh shell, not just one where the
   operator has already exported the URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


class TestEnvCreatesSchemaBeforeConfigure:
    def test_create_schema_appears_before_context_configure(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        create_at = text.index("CREATE SCHEMA IF NOT EXISTS auto_claude")
        configure_at = text.index("context.configure(")
        assert create_at < configure_at, (
            "schema must be created before context.configure() reads the "
            "version table out of it"
        )

    def test_version_table_schema_is_auto_claude(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        assert 'version_table_schema="auto_claude"' in text


class TestRevision0001CreatesTheFrozenSchema:
    def test_creates_all_four_tables(self):
        text = (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        for table in ("harness", "issue_state", "run", "summary"):
            assert f"CREATE TABLE auto_claude.{table}" in text

    def test_lease_columns_have_no_default(self):
        # Guards Task 7's "upsert never touches the lease columns" rule one
        # layer down — a DEFAULT here would make it easy for a future INSERT
        # to accidentally seed a lease value.
        text = (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("owner_harness_id", "lease_expires_at", "heartbeat_at")):
                assert "DEFAULT" not in stripped, f"lease column must have no default: {stripped!r}"

    def test_down_revision_is_none_and_id_matches_filename(self):
        text = (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        assert 'revision: str = "0001_initial"' in text
        assert "down_revision: Union[str, None] = None" in text


class TestDowngradeLeavesTheSchemaForAlembicsOwnBookkeeping:
    """DROP SCHEMA auto_claude CASCADE would abort mid-downgrade: Alembic
    deletes its alembic_version row (itself inside auto_claude, since
    version_table_schema="auto_claude") in the same transaction right after
    downgrade() runs, and Postgres already considers the schema gone by then.
    """

    def _text(self) -> str:
        return (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")

    def test_does_not_drop_the_schema(self):
        # The downgrade() docstring/comment is allowed to *explain* why not
        # (and does) - what must never appear is an actual op.execute(...)
        # call that drops the schema.
        assert 'op.execute("DROP SCHEMA' not in self._text()

    def test_drops_all_four_tables(self):
        text = self._text()
        for table in ("harness", "issue_state", "run", "summary"):
            assert f"DROP TABLE auto_claude.{table}" in text

    def test_drops_children_before_parents(self):
        # summary -> run/issue_state; run -> issue_state/harness;
        # issue_state -> harness. Dropping in this order needs no CASCADE.
        text = self._text()
        order = [
            text.index("DROP TABLE auto_claude.summary"),
            text.index("DROP TABLE auto_claude.run"),
            text.index("DROP TABLE auto_claude.issue_state"),
            text.index("DROP TABLE auto_claude.harness"),
        ]
        assert order == sorted(order), "tables must drop children before parents"


class TestEnvForcesThePsycopg3Dialect:
    """A bare `postgresql://` scheme maps to psycopg2 in SQLAlchemy's dialect
    registry; requirements.txt pins psycopg 3 only. db/pool.py is unaffected
    (raw psycopg3, no dialect lookup) - only SQLAlchemy's create_engine() is.
    """

    def test_forces_postgresql_plus_psycopg_drivername(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "postgresql+psycopg" in text
        assert "drivername" in text

    def test_create_engine_uses_the_forced_url_not_the_raw_one(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "create_engine(_engine_url(_database_url()))" in text


class TestEnvLoadsTheGitignoredDotenv:
    """alembic upgrade head is the remedy Task 5's startup gate prints on a
    stale schema, so it must work in a fresh shell - not just one where the
    operator already exported PIPELINE_METRICS_DATABASE_URL by hand.
    """

    def test_imports_load_dotenv_from_ghauth(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "from ghauth import load_dotenv" in text

    def test_calls_load_dotenv_before_database_url_is_ever_read(self):
        text = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        load_dotenv_at = text.index("load_dotenv(ROOT)")
        first_database_url_call = text.index("_database_url()")
        assert load_dotenv_at < first_database_url_call
