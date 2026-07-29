"""Structural checks on the Alembic scaffold.

No real database here — that is Step 6 below, run once by hand against the
live Supabase instance, not on every `pytest` run. This file guards the one
trap docs/plans/12-shared-state-in-postgres.md calls out by name:
`version_table_schema="auto_claude"` means Alembic reads its version table out
of a schema that must already exist, so `CREATE SCHEMA IF NOT EXISTS
auto_claude` has to run in migrations/env.py *before* `context.configure(...)`
executes — not inside revision 0001, where it would be too late for
`context.configure` itself.
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
