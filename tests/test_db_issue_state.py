# tests/test_db_issue_state.py
"""Tests for db/issue_state.py.

The one rule that matters here is structural, not behavioral: `upsert` must
NEVER reference owner_harness_id / lease_expires_at / heartbeat_at, in either
the INSERT column list or the ON CONFLICT SET clause. A state write silently
clearing or stealing a lease is exactly the bug class docs/plans/
12-shared-state-in-postgres.md design decision #2 exists to rule out by
construction — lease columns are exclusively db/lease.py's job.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.issue_state import fetch, fetch_all, upsert  # noqa: E402

_LEASE_COLUMNS = ("owner_harness_id", "lease_expires_at", "heartbeat_at")


class FakeDatabase:
    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self._rows


def _upsert(db, **overrides):
    kwargs = dict(
        issue_id="r#1", repo="r", number=1, title="t", stage="ac-dev-ready",
        kind="fix", mode="dev", branch=None, pr_url=None,
        triage_attempts=0, rework_count=0, continuation_count=0, last_error=None,
    )
    kwargs.update(overrides)
    upsert(db, **kwargs)


class TestUpsertNeverTouchesLeaseColumns:
    def test_lease_columns_are_absent_from_the_insert_column_list(self):
        db = FakeDatabase()
        _upsert(db)
        sql, _params = db.calls[0]
        insert_clause = sql[: sql.index("VALUES")]
        for column in _LEASE_COLUMNS:
            assert column not in insert_clause

    def test_lease_columns_are_absent_from_the_on_conflict_set_clause(self):
        db = FakeDatabase()
        _upsert(db)
        sql, _params = db.calls[0]
        set_clause = sql[sql.index("DO UPDATE"):]
        for column in _LEASE_COLUMNS:
            assert column not in set_clause

    def test_passes_every_non_lease_field_as_a_parameter_in_order(self):
        db = FakeDatabase()
        _upsert(db, issue_id="r#1", repo="repo", number=7, title="Title",
                stage="ac-in-progress", kind="fix", mode="dev", branch="b",
                pr_url="https://x/pull/1", triage_attempts=1, rework_count=2,
                continuation_count=3, last_error="boom")
        _sql, params = db.calls[0]
        assert params == ("r#1", "repo", 7, "Title", "ac-in-progress", "fix",
                           "dev", "b", "https://x/pull/1", 1, 2, 3, "boom")


class TestFetch:
    def test_returns_none_when_no_row(self):
        db = FakeDatabase(rows=[])
        assert fetch(db, "r#1") is None

    def test_zips_columns_onto_the_returned_row(self):
        row = ("r#1", "repo", 1, "t", "ac-dev-ready", "fix", "dev", None, None,
               0, 0, 0, None, "2026-01-01", "2026-01-01", None, None, None)
        db = FakeDatabase(rows=[row])
        result = fetch(db, "r#1")
        assert result["issue_id"] == "r#1"
        assert result["stage"] == "ac-dev-ready"
        assert result["owner_harness_id"] is None


class TestFetchAll:
    def test_keys_the_result_by_issue_id(self):
        row1 = ("r#1", "repo", 1, "t1", None, None, "dev", None, None, 0, 0, 0,
                None, "x", "x", None, None, None)
        row2 = ("r#2", "repo", 2, "t2", None, None, "dev", None, None, 0, 0, 0,
                None, "x", "x", "harness-a", "2099-01-01", "2026-01-01")
        db = FakeDatabase(rows=[row1, row2])
        result = fetch_all(db)
        assert set(result) == {"r#1", "r#2"}
        assert result["r#2"]["owner_harness_id"] == "harness-a"
