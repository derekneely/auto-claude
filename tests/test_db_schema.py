"""Tests for db/schema.py — the daemon never migrates; it only checks.

docs/plans/12-shared-state-in-postgres.md, "Migrations": "The daemon never
migrates. It checks at startup whether the schema is current; if it is
behind, it refuses to start and prints the command to run." All of `db.schema`
is tested against a FakeDatabase — no real Postgres, matching every other
db/*.py test in this plan except Task 4's migration round-trip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.pool import DbUnavailable  # noqa: E402
from db.schema import (  # noqa: E402
    EXPECTED_REVISION,
    SchemaOutOfDate,
    check_schema_current,
    current_revision,
)


class FakeDatabase:
    def __init__(self, rows=None, raises=None):
        self._rows = rows
        self._raises = raises

    def execute(self, sql, params=()):
        if self._raises is not None:
            raise self._raises
        return self._rows if self._rows is not None else []


class TestCurrentRevision:
    def test_returns_the_revision_when_the_table_exists(self):
        db = FakeDatabase(rows=[(EXPECTED_REVISION,)])
        assert current_revision(db) == EXPECTED_REVISION

    def test_returns_none_when_the_table_does_not_exist(self):
        db = FakeDatabase(raises=RuntimeError(
            'relation "auto_claude.alembic_version" does not exist'))
        assert current_revision(db) is None

    def test_propagates_db_unavailable_rather_than_treating_it_as_missing(self):
        db = FakeDatabase(raises=DbUnavailable("Postgres is down"))
        with pytest.raises(DbUnavailable):
            current_revision(db)


class TestCheckSchemaCurrent:
    def test_passes_silently_when_current(self):
        db = FakeDatabase(rows=[(EXPECTED_REVISION,)])
        check_schema_current(db)  # must not raise

    def test_raises_schema_out_of_date_with_the_upgrade_command_when_behind(self):
        db = FakeDatabase(rows=[("0000_before",)])
        with pytest.raises(SchemaOutOfDate) as excinfo:
            check_schema_current(db)
        assert "alembic upgrade head" in str(excinfo.value)

    def test_raises_when_schema_is_missing_entirely(self):
        db = FakeDatabase(raises=RuntimeError('relation "..." does not exist'))
        with pytest.raises(SchemaOutOfDate) as excinfo:
            check_schema_current(db)
        assert "None" in str(excinfo.value)
