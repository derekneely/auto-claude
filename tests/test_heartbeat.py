"""Tests for main._maybe_heartbeat - the lease heartbeat's cadence.

Guards docs/plans/12-shared-state-in-postgres.md, "main owns the heartbeat":
the poll loop's shutdown-responsive sleep runs in 1-second increments, but a
slow poll/triage pass can spend many seconds without ever reaching that
sleep loop, so heartbeat cadence must be driven by elapsed wall clock and
called from more than one point in main(), not by "did the sleep loop run
N times".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class _FakeDbSync:
    def __init__(self):
        self.heartbeats = 0

    def heartbeat(self):
        self.heartbeats += 1


class TestMaybeHeartbeat:
    def test_does_not_fire_before_the_interval_elapses(self):
        dbsync = _FakeDbSync()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, now=159.0)
        assert dbsync.heartbeats == 0
        assert last == 100.0, "unchanged last_at is how the next call knows nothing fired"

    def test_fires_once_the_interval_has_elapsed(self):
        dbsync = _FakeDbSync()
        last = main._maybe_heartbeat(dbsync, last_at=100.0, interval=60, now=161.0)
        assert dbsync.heartbeats == 1
        assert last == 161.0

    def test_a_slow_pass_that_never_reaches_the_sleep_loop_still_heartbeats(self):
        # Simulates step 1 of the poll loop (drain/reap) calling this
        # directly, not just the per-second sleep tick - see main.py's two
        # call sites.
        dbsync = _FakeDbSync()
        last = 0.0
        for elapsed in (10.0, 200.0):  # one slow pass alone blows past 60s
            last = main._maybe_heartbeat(dbsync, last_at=last, interval=60, now=elapsed)
        assert dbsync.heartbeats == 1
