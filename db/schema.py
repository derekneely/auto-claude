"""db/schema.py — the schema-currency gate.

The daemon never migrates (docs/plans/12-shared-state-in-postgres.md,
"Migrations"): it checks at startup whether `auto_claude.alembic_version`
matches EXPECTED_REVISION and refuses to start otherwise, printing the exact
`alembic upgrade head` command rather than running it itself.
"""

from __future__ import annotations

from db.pool import Database, DbUnavailable

EXPECTED_REVISION = "0001_initial"


class SchemaOutOfDate(RuntimeError):
    """The connected database's auto_claude schema is behind EXPECTED_REVISION."""


def current_revision(db: Database) -> str | None:
    """Reads auto_claude.alembic_version. None if the table does not exist."""
    try:
        rows = db.execute("SELECT version_num FROM auto_claude.alembic_version")
    except DbUnavailable:
        raise
    except Exception:
        # UndefinedTable (or any other "the table isn't there yet") means the
        # very first migration has never run — a legitimate "None", not an
        # unhandled traceback, so a virgin database gets the SchemaOutOfDate
        # message rather than a crash.
        return None
    if not rows:
        return None
    return rows[0][0]


def check_schema_current(db: Database) -> None:
    """Raises SchemaOutOfDate with the exact command to run, if behind."""
    actual = current_revision(db)
    if actual == EXPECTED_REVISION:
        return
    raise SchemaOutOfDate(
        f"auto_claude schema is at {actual!r}, expected {EXPECTED_REVISION!r}. "
        f"Run: .venv\\Scripts\\python -m alembic upgrade head"
    )
