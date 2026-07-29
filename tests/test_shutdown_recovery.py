"""Tests for what a Ctrl+C leaves behind.

An interrupted issue has to end up in a status the poller can resurrect.
`poller.py` only re-queues a known issue from FAILED/COMPLETED/INTERRUPTED, so a
record left at IN_PROGRESS is invisible forever: relabelling it ac-dev-ready does
nothing, and `_release_stale_locks` only rewinds GitHub labels, never the state
store. That combination stranded field_admin#215 on 2026-07-29.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from process_manager import ProcessManager  # noqa: E402
from state import IssueStatus  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, msg):
        self.messages.append(("info", msg))

    def warn(self, msg):
        self.messages.append(("warn", msg))

    def error(self, msg):
        self.messages.append(("error", msg))

    def drain_queue(self, _q):
        pass


class DeadProc:
    """A worker that already exited — the graceful-abort case."""

    exitcode = 0
    pid = 1234

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class LiveProc:
    """A worker that ignores abort and must be force-terminated."""

    exitcode = None
    pid = 5678

    def __init__(self):
        self.terminated = False

    def is_alive(self):
        return not self.terminated

    def terminate(self):
        self.terminated = True

    def join(self, timeout=None):
        pass


class FakeState:
    def __init__(self, records=None):
        self._records = records or {}
        self.saved = 0

    def get(self, issue_id):
        return self._records.get(issue_id)

    def transition(self, issue_id, status):
        self._records[issue_id].status = status

    def update(self, issue_id, **kwargs):
        for k, v in kwargs.items():
            setattr(self._records[issue_id], k, v)

    def save(self):
        self.saved += 1


def make_pm(records=None):
    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=1),
    )
    state = FakeState(records)
    pm = ProcessManager(
        config=config,
        state=state,
        logger=FakeLogger(),
        log_queue=queue.Queue(),
        state_queue=queue.Queue(),
    )
    return pm, state


def _record(status=IssueStatus.IN_PROGRESS):
    return SimpleNamespace(status=status, error=None, worker_pid=99)


def _abort_event():
    return SimpleNamespace(set=lambda: None)


class TestShutdownLeavesResurrectableState:
    def test_a_worker_that_exits_within_the_grace_period_is_marked_interrupted(self):
        # The regression. The worker obeys abort and dies fast, so the grace
        # loop reaps it and pops it from _workers — and the "mark interrupted"
        # block after the loop then iterates an empty dict. The record stayed
        # IN_PROGRESS, which the poller will not resurrect.
        record = _record()
        pm, state = make_pm({"field_admin#215": record})
        pm._workers["field_admin#215"] = (DeadProc(), _abort_event())

        pm.shutdown_all()

        assert record.status == IssueStatus.INTERRUPTED
        assert state.saved > 0, "the transition must be persisted, not just in memory"

    def test_a_force_terminated_worker_is_marked_interrupted(self):
        record = _record()
        pm, state = make_pm({"field_admin#215": record})
        proc = LiveProc()
        pm._workers["field_admin#215"] = (proc, _abort_event())

        pm.shutdown_all()

        assert proc.terminated
        assert record.status == IssueStatus.INTERRUPTED

    def test_a_worker_that_reported_a_terminal_status_is_left_alone(self):
        # The worker finished and pushed COMPLETED before the abort landed;
        # shutdown must not rewrite real execution history.
        record = _record(status=IssueStatus.COMPLETED)
        pm, _state = make_pm({"field_admin#215": record})
        pm._workers["field_admin#215"] = (DeadProc(), _abort_event())

        pm.shutdown_all()

        assert record.status == IssueStatus.COMPLETED

    def test_every_worker_is_accounted_for(self):
        records = {f"field_admin#{n}": _record() for n in (1, 2, 3)}
        pm, _state = make_pm(dict(records))
        for n in (1, 2, 3):
            pm._workers[f"field_admin#{n}"] = (DeadProc(), _abort_event())

        pm.shutdown_all()

        assert all(r.status == IssueStatus.INTERRUPTED for r in records.values())
        assert not pm._workers
