# tests/test_dbsync.py
"""Tests for dbsync.py — the seam main/process_manager/worker read and write
Postgres through. This task lands before db/lease.py, db/history.py or
db/journal.py exist (see the plan's "Sequencing and the dbsync dependency"
table), so only `enabled` and `upsert_issue` exist yet; a durable write that
cannot reach Postgres is logged and discarded rather than journaled — safe
because GitHub labels stay truth and startup reconciliation (Tasks 10-11)
rebuilds issues.json from scratch on every restart regardless. Task 21
upgrades this from log-and-drop to a real journal without touching any
signature here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbsync import DbSync  # noqa: E402
from db.harness import Harness  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402
from state import IssueRecord  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, msg):
        self.messages.append(("info", msg))

    def warn(self, msg):
        self.messages.append(("warn", msg))

    def error(self, msg):
        self.messages.append(("error", msg))


class FakeDatabase:
    """A Database stand-in whose execute() can be told to fail."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._raises = raises

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self._raises is not None:
            raise self._raises
        return []


def _make_record(**overrides):
    defaults = dict(
        issue_id="repo#1", repo="repo", number=1, title="t", body="",
        labels=[], action="implement", status="queued",
        discovered_at="", updated_at="", issue_updated_at="",
        branch=None, pr_url=None, triage_attempts=0, error=None,
        rework_count=0, continuation_count=0,
    )
    defaults.update(overrides)
    return IssueRecord(**defaults)


HARNESS = Harness(id="h1", hostname="box", pid=1, version="0.2.0")


class TestEnabled:
    def test_false_when_db_is_none(self):
        sync = DbSync(None, HARNESS, FakeLogger())
        assert sync.enabled is False

    def test_true_when_db_is_present(self):
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger())
        assert sync.enabled is True


class TestUpsertIssueNeverRaises:
    def test_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())

        sync.upsert_issue(_make_record(), stage="ac-in-progress")

        assert db.calls, "must have issued a write, not short-circuited"

    def test_db_unavailable_is_logged_and_swallowed_not_raised(self):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert any("dropping" in msg.lower() or "unreachable" in msg.lower()
                   for _lvl, msg in logger.messages)

    def test_a_non_connectivity_error_is_also_logged_and_swallowed(self):
        # A bad payload must not crash the caller any more than a dropped
        # connection does — both are "we could not durably write this".
        db = FakeDatabase(raises=ValueError("value too long"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)

    def test_no_database_at_all_is_a_silent_no_op(self):
        sync = DbSync(None, HARNESS, FakeLogger())
        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise
