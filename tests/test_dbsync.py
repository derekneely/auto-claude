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


# ----------------------------------------------------------------------
# Lease pass-through on DbSync.
#
# Guards three things at once: that DbSync.acquire_lease/heartbeat/
# release_lease/check_lease/release_expired actually call db.lease's
# functions (not reimplement the SQL), that every one of them is a safe
# no-op when Postgres is disabled (`db=None`) - a disabled database means
# no shared state, which per docs/plans/12-shared-state-in-postgres.md
# means there cannot be a second harness, so lease operations must not
# block anything - and that a non-default configured `ttl_seconds` (Task
# 8's `DbSync.__init__`, otherwise unused until now) actually reaches
# `db.lease.acquire`/`heartbeat`, which is what makes
# `config.database.lease_ttl_seconds` (Task 2) more than a dead field on
# the config object.
# ----------------------------------------------------------------------


class _FakeLeaseDb:
    """Records every call it receives; DbSync must forward to db.lease, not
    touch this fake's SQL surface directly."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return [("field_admin#1",)]


def _dbsync(db, ttl_seconds=1800):
    harness = Harness(id="harness-a", hostname="box", pid=1, version="0.2.0")
    return DbSync(db, harness, None, ttl_seconds=ttl_seconds)


class TestLeasePassThroughWhenEnabled:
    def test_acquire_lease_calls_db_lease_acquire(self):
        db = _FakeLeaseDb()
        assert _dbsync(db).acquire_lease("field_admin#1") is True
        assert db.calls, "must have issued a query, not short-circuited"

    def test_check_lease_calls_db_lease_check(self):
        db = _FakeLeaseDb()
        assert _dbsync(db).check_lease("field_admin#1") is True

    def test_release_expired_returns_the_freed_ids(self):
        db = _FakeLeaseDb()
        assert _dbsync(db).release_expired() == ["field_admin#1"]


class TestLeasePassThroughWhenDisabled:
    def test_acquire_lease_is_always_granted(self):
        assert _dbsync(None).acquire_lease("field_admin#1") is True

    def test_check_lease_is_always_true(self):
        assert _dbsync(None).check_lease("field_admin#1") is True

    def test_heartbeat_and_release_lease_do_not_raise(self):
        dbsync = _dbsync(None)
        dbsync.heartbeat()
        dbsync.release_lease("field_admin#1")  # must not raise

    def test_release_expired_returns_empty(self):
        assert _dbsync(None).release_expired() == []


class TestConfiguredTtlReachesDbLease:
    def test_acquire_lease_passes_the_configured_ttl_seconds_through(self):
        db = _FakeLeaseDb()
        _dbsync(db, ttl_seconds=900).acquire_lease("field_admin#1")
        _sql, params = db.calls[0]
        assert 900 in params, "the configured ttl_seconds must reach db.lease.acquire"

    def test_heartbeat_passes_the_configured_ttl_seconds_through(self):
        db = _FakeLeaseDb()
        _dbsync(db, ttl_seconds=900).heartbeat()
        _sql, params = db.calls[0]
        assert 900 in params, "the configured ttl_seconds must reach db.lease.heartbeat"


class TestStartAndFinishRun:
    def test_start_run_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())
        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")
        assert db.calls

    def test_finish_run_is_logged_and_dropped_on_db_unavailable(self):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger)
        sync.finish_run(run_id="r1", outcome="completed", exit_code=0,
                         duration_seconds=1, cost_usd=0.1, turns=1, crash_log_path=None)
        assert any("dropping" in msg.lower() for _lvl, msg in logger.messages)


class TestAddSummary:
    def test_returns_a_stable_id_even_when_the_write_is_dropped(self):
        db = FakeDatabase(raises=DbUnavailable("down"))
        sync = DbSync(db, HARNESS, FakeLogger())
        summary_id = sync.add_summary(issue_id="repo#1", run_id=None, kind="triage", body="text")
        assert isinstance(summary_id, str) and len(summary_id) == 32

    def test_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())
        sync.add_summary(issue_id="repo#1", run_id="r1", kind="dev", body="did it")
        assert db.calls
