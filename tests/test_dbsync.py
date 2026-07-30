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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbsync  # noqa: E402
from dbsync import DbSync  # noqa: E402
from db.harness import Harness  # noqa: E402
from db.journal import Journal  # noqa: E402
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

    def test_db_unavailable_is_logged_and_swallowed_not_raised(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, logger, journal=journal)

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

    def test_no_database_at_all_is_a_silent_no_op(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(None, HARNESS, FakeLogger(), journal=journal)
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

    def test_finish_run_is_logged_and_journaled_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, logger, journal=journal)
        sync.finish_run(run_id="r1", outcome="completed", exit_code=0,
                         duration_seconds=1, cost_usd=0.1, turns=1, crash_log_path=None)
        assert any("unreachable" in msg.lower() for _lvl, msg in logger.messages)


class TestAddSummary:
    def test_returns_a_stable_id_even_when_the_write_is_journaled(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)
        summary_id = sync.add_summary(issue_id="repo#1", run_id=None, kind="triage", body="text")
        assert isinstance(summary_id, str) and len(summary_id) == 32

    def test_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger())
        sync.add_summary(issue_id="repo#1", run_id="r1", kind="dev", body="did it")
        assert db.calls


class TestDurableWritesNowJournalInsteadOfBeingDropped:
    """The Task 8 -> Task 21 upgrade: every durable write already routed
    through `_durable`; only its failure branch changes here, from a log
    line to `self._journal.append(op, payload)`. Nothing above `_durable`
    (upsert_issue, start_run, finish_run, add_summary) changes at all."""

    def test_upsert_issue_journals_and_does_not_raise_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert journal.pending() == 1

    def test_start_run_journals_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")

        assert journal.pending() == 1

    def test_finish_run_journals_on_db_unavailable(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        sync.finish_run(
            run_id="r1", outcome="completed", exit_code=0, duration_seconds=1,
            cost_usd=0.1, turns=1, crash_log_path=None,
        )

        assert journal.pending() == 1

    def test_add_summary_journals_and_still_returns_a_stable_id(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        summary_id = sync.add_summary(
            issue_id="repo#1", run_id=None, kind="triage", body="text",
        )

        assert isinstance(summary_id, str) and len(summary_id) == 32
        assert journal.pending() == 1

    def test_a_non_connectivity_error_is_still_logged_and_dropped_not_journaled(self, tmp_path):
        # A bad payload (e.g. a body too long for the column) must not
        # journal forever against a write that will never succeed — this
        # branch of _durable is untouched by the upgrade.
        db = FakeDatabase(raises=ValueError("value too long"))
        journal = Journal(tmp_path / "j.jsonl")
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger, journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")

        assert journal.pending() == 0
        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)

    def test_no_db_at_all_journals_directly(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(None, HARNESS, FakeLogger(), journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")

        assert journal.pending() == 1


class TestLeaseOperationsStillNeverJournal:
    """Regression guard for the upgrade above: a real Journal is now wired
    in, so it would be easy to accidentally route a lease operation through
    it. `acquire_lease`/`check_lease` must keep failing closed and queuing
    nothing — see the comment on that section of DbSync. "Claims never
    queue" (spec, 12-shared-state-in-postgres.md): a journaled claim would
    silently replay later and double-claim an issue another harness has
    since taken over, which is the exact bug the lease exists to prevent."""

    def test_acquire_lease_propagates_db_unavailable_and_does_not_journal(
        self, tmp_path, monkeypatch
    ):
        # acquire_lease deliberately does not catch DbUnavailable itself (see
        # the comment on DbSync's lease-operations block) — the caller
        # decides what an unreachable database means for it. What this
        # upgrade must not do is quietly journal the claim on the way out.
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger(), journal=journal)
        monkeypatch.setattr(
            dbsync.lease, "acquire",
            lambda *a, **k: (_ for _ in ()).throw(DbUnavailable("down")),
        )

        with pytest.raises(DbUnavailable):
            sync.acquire_lease("repo#1")

        assert journal.pending() == 0, "claims must fail closed, never queue"

    def test_check_lease_delegates_to_lease_check_and_never_journals(
        self, tmp_path, monkeypatch
    ):
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger(), journal=journal)
        monkeypatch.setattr(dbsync.lease, "check", lambda *a, **k: False)

        assert sync.check_lease("repo#1") is False
        assert journal.pending() == 0


class TestReplayPending:
    def test_drains_the_journal_once_the_db_is_back(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.append(
            "history.start_run",
            dict(run_id="r1", issue_id="repo#1", harness_id="h1", mode="dev", model="m"),
        )
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger(), journal=journal)

        applied = sync.replay_pending()

        assert applied == 1
        assert journal.pending() == 0

    def test_returns_zero_with_no_db(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.append(
            "history.start_run",
            dict(run_id="r1", issue_id="repo#1", harness_id="h1", mode="dev", model="m"),
        )
        sync = DbSync(None, HARNESS, FakeLogger(), journal=journal)

        assert sync.replay_pending() == 0
        assert journal.pending() == 1
