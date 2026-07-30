# tests/test_dbsync.py
"""Tests for dbsync.py — the seam main/process_manager/worker read and write
Postgres through.

A durable write that cannot reach Postgres (`DbUnavailable`, with a real
`self._db` configured) is journaled to `db/journal.py` and replayed by
`replay_pending()` once Postgres is reachable again. A write attempted while
no database is configured at all (`self._db is None` for the whole process
lifetime) is logged and dropped instead — journaling it would never drain,
since `replay_pending()` is also a no-op in that state — safe because GitHub
labels stay truth and startup reconciliation (Tasks 10-11) rebuilds
issues.json from scratch on every restart regardless. A non-connectivity
error (a bad payload, a constraint violation) is likewise logged and
dropped, never journaled, since replaying it would fail identically forever.
`journal` is a required constructor argument (fix round, Finding 2): every
`DbSync` that can attempt a durable write must have somewhere to queue a
failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbsync  # noqa: E402
from dbsync import DbSync  # noqa: E402
from db.harness import Harness  # noqa: E402
from db.journal import Journal, NullJournal  # noqa: E402
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


def _unused_journal() -> NullJournal:
    """A journal that must never actually be written to. `journal` is a
    required DbSync constructor argument (fix round, Finding 2), but many
    tests below exercise a path that succeeds before ever reaching
    `_durable`'s failure branches — this stands in for those, matching a
    real construction site without adding an irrelevant `tmp_path` fixture
    to every one of them.

    A real `Journal` on a bare relative path used to fill this role — its
    constructor does no filesystem I/O, so it never failed outright — but
    that made "this path must never journal" a fact about which tests
    happen to exercise which branches rather than something enforced: a
    regression that made one of these tests reach `_durable`'s
    DbUnavailable branch would have quietly appended a real line into
    `unused-in-this-test.jsonl` in the repo root and still passed.
    `NullJournal.append` raises instead, so that regression fails loudly."""
    return NullJournal()


class TestEnabled:
    def test_false_when_db_is_none(self):
        sync = DbSync(None, HARNESS, FakeLogger(), journal=_unused_journal())
        assert sync.enabled is False

    def test_true_when_db_is_present(self):
        sync = DbSync(FakeDatabase(), HARNESS, FakeLogger(), journal=_unused_journal())
        assert sync.enabled is True


class TestJournalIsRequired:
    """Fix round, Finding 2: a `DbSync` that can attempt a durable write must
    always have somewhere to queue a failure. `journal` used to default to
    `None`, which `_durable` would then unconditionally dereference on a
    DbUnavailable — an AttributeError deep inside what is supposed to be a
    'never raises' write. Failing loudly at construction time instead is
    much easier to catch in review and in this test than a crash that only
    surfaces once Postgres happens to drop out."""

    def test_constructing_without_a_journal_raises(self):
        with pytest.raises(TypeError):
            DbSync(FakeDatabase(), HARNESS, FakeLogger())


class TestUpsertIssueNeverRaises:
    def test_succeeds_silently_when_postgres_is_reachable(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger(), journal=_unused_journal())

        sync.upsert_issue(_make_record(), stage="ac-in-progress")

        assert db.calls, "must have issued a write, not short-circuited"

    def test_db_unavailable_is_logged_and_swallowed_not_raised(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()
        journal = Journal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, logger, journal=journal)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        # Tightened (fix round): "dropping" can no longer be emitted on this
        # path at all — a real db configured plus DbUnavailable always
        # journals now — so asserting it as an acceptable alternative would
        # let a reworded regression slide silently past this test.
        assert any("unreachable" in msg.lower() for _lvl, msg in logger.messages)
        assert journal.pending() == 1

    def test_a_non_connectivity_error_is_also_logged_and_swallowed(self):
        # A bad payload must not crash the caller any more than a dropped
        # connection does — both are "we could not durably write this".
        db = FakeDatabase(raises=ValueError("value too long"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger, journal=_unused_journal())

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)

    def test_no_database_at_all_is_a_silent_no_op(self, tmp_path):
        # Fix round, Finding 3 (user ruling, 2026-07-30): a permanently
        # disabled/unconfigured database logs and drops, exactly as it did
        # before Task 21 — it must NOT journal, because replay_pending()
        # never drains this case (there is nothing to replay into), so
        # journaling here would grow state/journal.jsonl forever.
        journal = Journal(tmp_path / "j.jsonl")
        logger = FakeLogger()
        sync = DbSync(None, HARNESS, logger, journal=journal)

        sync.upsert_issue(_make_record(), stage="ac-in-progress")  # must not raise

        assert journal.pending() == 0, "a permanently disabled database must never journal"
        assert any("dropping" in msg.lower() for _lvl, msg in logger.messages)


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
    return DbSync(db, harness, None, ttl_seconds=ttl_seconds, journal=_unused_journal())


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
        sync = DbSync(db, HARNESS, FakeLogger(), journal=_unused_journal())
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
        sync = DbSync(db, HARNESS, FakeLogger(), journal=_unused_journal())
        sync.add_summary(issue_id="repo#1", run_id="r1", kind="dev", body="did it")
        assert db.calls


class TestSummaryBodiesAreRedactedAtThisSeam:
    """Postgres is a second destination summary text escapes to, and the six
    callers do not agree about scrubbing it.

    The real gap this guards: `worker._post_pr_review` passes `redact(body)`
    as its `--body` argument but hands the caller back nothing, so the
    request-changes path appends the RAW `request_body` to
    `pending_summaries` — and that body carries `checks_transcript`, raw
    verify/test output, which is exactly where a leaked env var surfaces. The
    scrubbed copy went to GitHub while the unscrubbed one went to Postgres.
    Redacting here rather than in each caller makes it structural: a future
    summary kind cannot forget.
    """

    def _body_written(self, db):
        # FakeDatabase records (sql, params); the body is whichever param
        # carries the marker text, so this does not depend on column order.
        return "\n".join(str(p) for _sql, params in db.calls for p in params)

    def test_a_secret_in_raw_review_feedback_never_reaches_postgres(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger(), journal=_unused_journal())
        sync.add_summary(
            issue_id="repo#1", run_id="r1", kind="review",
            body="Verify/test checks failed:\nAUTH_SECRET=hunter2supersecret\n",
        )
        written = self._body_written(db)
        assert "hunter2supersecret" not in written
        assert "[REDACTED]" in written

    def test_a_github_token_in_a_transcript_never_reaches_postgres(self):
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger(), journal=_unused_journal())
        sync.add_summary(
            issue_id="repo#1", run_id="r1", kind="review",
            body="the call used ghp_" + "A" * 36 + " and failed",
        )
        written = self._body_written(db)
        assert "ghp_" + "A" * 36 not in written
        assert "[REDACTED]" in written

    def test_already_redacted_bodies_are_unchanged_because_redact_is_idempotent(self):
        # dev/crash/budget/triage arrive already scrubbed; double-scrubbing
        # them must not mangle the text a human will read.
        db = FakeDatabase()
        sync = DbSync(db, HARNESS, FakeLogger(), journal=_unused_journal())
        clean = "### Summary\n\nImplemented the thing. No secrets here."
        sync.add_summary(issue_id="repo#1", run_id="r1", kind="dev", body=clean)
        assert clean in self._body_written(db)


class TestDurableWritesNowJournalInsteadOfBeingDropped:
    """The Task 8 -> Task 21 upgrade: every durable write already routed
    through `_durable`; its DbUnavailable failure branch changes here, from
    a log line to `self._journal.append(op, payload)`, but ONLY when a real
    `self._db` is configured — a permanently disabled/unconfigured database
    (`self._db is None`) still logs and drops (see
    TestUpsertIssueNeverRaises::test_no_database_at_all_is_a_silent_no_op;
    fix round, Finding 3). Nothing above `_durable` (upsert_issue, start_run,
    finish_run, add_summary) changes at all."""

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

    # NOTE: the brief's `test_no_db_at_all_journals_directly` was deleted
    # here (fix round, Finding 3 / user ruling, 2026-07-30). A permanently
    # disabled database must log-and-drop, not journal — see
    # TestUpsertIssueNeverRaises::test_no_database_at_all_is_a_silent_no_op,
    # which now asserts the opposite of what that deleted test asserted.


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


class TestReplayPendingSurvivesReplayFailures:
    """Fix round, Finding 1: `Journal.replay` deliberately lets a handler
    failure (an FK/constraint violation on a queued entry, e.g. a stale
    `harness_id` on `history.start_run`) or an OSError from its own file
    read/truncate propagate uncaught, so the journal file stays intact for
    the next attempt — see db/journal.py's docstring. Before this fix,
    `replay_pending` caught only DbUnavailable, so anything else escaped
    into main.py's poll loop, whose only handler is `except
    KeyboardInterrupt` — killing the whole supervisor with live workers
    attached, and wedging forever afterward, since the untouched journal
    re-raises the same entry on every subsequent replay and restart."""

    def test_a_non_db_unavailable_replay_failure_is_logged_and_swallowed(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.append(
            "history.start_run",
            dict(run_id="r1", issue_id="repo#1", harness_id="h1", mode="dev", model="m"),
        )
        db = FakeDatabase(raises=ValueError("FK violation: no such harness_id"))
        logger = FakeLogger()
        sync = DbSync(db, HARNESS, logger, journal=journal)

        applied = sync.replay_pending()  # must not raise

        assert applied == 0
        assert journal.pending() == 1, "a failed replay must leave the journal intact"
        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)

    def test_an_oserror_from_the_journal_itself_is_logged_and_swallowed(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.append(
            "history.start_run",
            dict(run_id="r1", issue_id="repo#1", harness_id="h1", mode="dev", model="m"),
        )

        class _BrokenReplayJournal(Journal):
            def replay(self, db):
                raise OSError("disk full")

        broken_journal = _BrokenReplayJournal(journal._path)
        logger = FakeLogger()
        sync = DbSync(FakeDatabase(), HARNESS, logger, journal=broken_journal)

        applied = sync.replay_pending()  # must not raise

        assert applied == 0
        assert any("failed" in msg.lower() for _lvl, msg in logger.messages)


class TestDurableWriteJournalItselfCanFail:
    """Fix round, Finding 2: `Journal.append` does `mkdir(parents=True)` +
    `open('a')` + `write` (db/journal.py), so a disk-full, permissions, or
    Windows file-lock failure raises OSError. `_durable`'s "never raise"
    contract must survive that too, not just a DbUnavailable from
    Postgres."""

    def test_a_journal_append_failure_is_logged_and_dropped_not_raised(self, tmp_path):
        db = FakeDatabase(raises=DbUnavailable("down"))
        logger = FakeLogger()

        class _BrokenAppendJournal(Journal):
            def append(self, op, payload):
                raise OSError("disk full")

        journal = _BrokenAppendJournal(tmp_path / "j.jsonl")
        sync = DbSync(db, HARNESS, logger, journal=journal)

        sync.start_run(run_id="r1", issue_id="repo#1", mode="dev", model="m")  # must not raise

        assert any("could not journal" in msg.lower() for _lvl, msg in logger.messages)
