"""Tests for main._maybe_heartbeat - the lease heartbeat's cadence.

Guards docs/plans/12-shared-state-in-postgres.md, "main owns the heartbeat":
the poll loop's shutdown-responsive sleep runs in 1-second increments, but a
slow poll/triage pass can spend many seconds without ever reaching that
sleep loop, so heartbeat cadence must be driven by elapsed wall clock and
called from more than one point in main(), not by "did the sleep loop run
N times".

Also guards Fix round 1, Finding 1: `dbsync.heartbeat()` can raise
`DbUnavailable` when Postgres drops out mid-run (unlike being disabled,
which is a silent no-op inside DbSync itself). The poll loop's only
exception handler is `except KeyboardInterrupt`, so an uncaught
`DbUnavailable` here would kill the whole supervisor out from under every
live worker - `_maybe_heartbeat` must catch it, warn, and still advance
`last_at` so a down database does not turn every remaining tick into
another doomed heartbeat attempt.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402


class _FakeLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, msg):
        self.messages.append(("info", msg))

    def warn(self, msg):
        self.messages.append(("warn", msg))

    def error(self, msg):
        self.messages.append(("error", msg))

    def text(self) -> str:
        return " | ".join(m for _lvl, m in self.messages)


class _FakeDbSync:
    def __init__(self):
        self.heartbeats = 0
        self.touches = 0

    def heartbeat(self):
        self.heartbeats += 1

    def touch_harness(self):
        self.touches += 1


class _FakeUnavailableDbSync:
    """Postgres is enabled but unreachable: heartbeat() raises DbUnavailable
    uncaught, exactly as db/lease.py's real heartbeat does through a plain
    DbSync with no handling of its own."""

    def __init__(self):
        self.attempts = 0
        self.touch_attempts = 0

    def heartbeat(self):
        self.attempts += 1
        raise DbUnavailable("could not reach Postgres")

    def touch_harness(self):
        # Never reached while heartbeat() itself raises first - kept here
        # only so a future reordering can't silently AttributeError instead
        # of hitting the DbUnavailable handling this class exists to guard.
        self.touch_attempts += 1
        raise DbUnavailable("could not reach Postgres")


class TestMaybeHeartbeat:
    def test_does_not_fire_before_the_interval_elapses(self):
        dbsync = _FakeDbSync()
        logger = _FakeLogger()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, logger=logger, now=159.0)
        assert dbsync.heartbeats == 0
        assert last == 100.0, "unchanged last_at is how the next call knows nothing fired"

    def test_fires_once_the_interval_has_elapsed(self):
        dbsync = _FakeDbSync()
        logger = _FakeLogger()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, logger=logger, now=161.0)
        assert dbsync.heartbeats == 1
        assert last == 161.0

    def test_a_slow_pass_that_never_reaches_the_sleep_loop_still_heartbeats(self):
        # Simulates step 1 of the poll loop (drain/reap) calling this
        # directly, not just the per-second sleep tick - see main.py's two
        # call sites.
        dbsync = _FakeDbSync()
        logger = _FakeLogger()
        last = 0.0
        for elapsed in (10.0, 200.0):  # one slow pass alone blows past 60s
            last = main._maybe_heartbeat(dbsync, last_at=last, interval=60, logger=logger, now=elapsed)
        assert dbsync.heartbeats == 1

    def test_also_touches_the_harness_row_on_the_same_cadence(self):
        # Final whole-branch review, Finding 7: db/harness.py's `touch` had
        # no caller at all, so `last_seen_at` only ever advanced at startup
        # via `register`'s ON CONFLICT - the column meant to answer "is this
        # harness alive" never actually did once the daemon was running.
        dbsync = _FakeDbSync()
        logger = _FakeLogger()
        main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, logger=logger, now=161.0)
        assert dbsync.touches == 1


class TestMaybeHeartbeatSurvivesDbUnavailable:
    def test_db_unavailable_is_caught_and_warned_not_raised(self):
        dbsync = _FakeUnavailableDbSync()
        logger = _FakeLogger()
        main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, logger=logger, now=161.0)  # must not raise
        assert dbsync.attempts == 1
        assert "unreachable" in logger.text().lower()

    def test_last_at_still_advances_so_the_next_tick_waits_out_the_interval(self):
        # Otherwise a down database turns every remaining loop tick into
        # another doomed heartbeat attempt instead of behaving like a
        # healthy one that waits out `interval`.
        dbsync = _FakeUnavailableDbSync()
        logger = _FakeLogger()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, logger=logger, now=161.0)
        assert last == 161.0
        last = main._maybe_heartbeat(dbsync, last_at=last, interval=60, logger=logger, now=161.5)
        assert dbsync.attempts == 1, "must not retry before the interval elapses again"
