"""Tests for worker.py's Claude-run metrics parsing.

Cost, turns and duration are inputs today (`--max-budget-usd`,
`--max-turns`) but nothing in this codebase has ever parsed the CLI's
`stream-json` `result` event, so `auto_claude.run.cost_usd`/`turns`/
`duration_seconds` would be unfillable without this. Guards: a run with no
result event at all (crash mid-stream) must not raise or fabricate zeros: it
must report all-None so a NULL lands in Postgres, not a misleading 0.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import (  # noqa: E402
    RunMetrics,
    StateUpdate,
    _accumulate_metrics,
    _parse_run_metrics,
)

# `SimpleNamespace` and `pytest` are unused by the tests below but are
# imported here because this file is built up incrementally across Tasks
# 16-18, and every later task's appended test classes rely on both being
# available at module scope.

RESULT_LINE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"done","session_id":"s1","uuid":"u1",'
    '"num_turns":12,"duration_ms":45231,"duration_api_ms":40000,'
    '"total_cost_usd":0.8842,"stop_reason":"end_turn",'
    '"terminal_reason":null,"usage":{},"modelUsage":{},'
    '"permission_denials":[],"api_error_status":null,"ttft_ms":900}'
)


class TestParseRunMetrics:
    def test_extracts_cost_turns_and_duration_from_a_normal_result_event(self):
        output = (
            '{"type":"assistant","message":{"content":[]}}\n'
            + RESULT_LINE
        )
        metrics = _parse_run_metrics(output)
        assert metrics.cost_usd == 0.8842
        assert metrics.turns == 12
        assert metrics.duration_seconds == 45  # round(45231 / 1000)

    def test_no_result_event_returns_all_none(self):
        # A crash mid-stream: only assistant/system lines, no terminal result.
        output = '{"type":"system","subtype":"init"}\n{"type":"assistant","message":{}}'
        metrics = _parse_run_metrics(output)
        assert metrics == RunMetrics(cost_usd=None, turns=None, duration_seconds=None)

    def test_empty_output_returns_all_none(self):
        assert _parse_run_metrics("") == RunMetrics()

    def test_malformed_json_lines_are_skipped_not_fatal(self):
        output = "not json at all\n" + RESULT_LINE + "\n{broken\n"
        metrics = _parse_run_metrics(output)
        assert metrics.cost_usd == 0.8842
        assert metrics.turns == 12

    def test_multiple_result_events_the_last_one_wins(self):
        first = RESULT_LINE
        second = RESULT_LINE.replace(
            '"total_cost_usd":0.8842', '"total_cost_usd":1.5'
        ).replace('"num_turns":12', '"num_turns":20')
        output = first + "\n" + second
        metrics = _parse_run_metrics(output)
        assert metrics.cost_usd == 1.5
        assert metrics.turns == 20

    def test_missing_keys_on_the_result_event_are_none_not_fatal(self):
        output = '{"type":"result","subtype":"success"}'
        metrics = _parse_run_metrics(output)
        assert metrics == RunMetrics()


class TestAccumulateMetrics:
    def test_sums_two_complete_readings(self):
        base = RunMetrics(cost_usd=1.0, turns=5, duration_seconds=10)
        extra = RunMetrics(cost_usd=0.25, turns=2, duration_seconds=3)
        total = _accumulate_metrics(base, extra)
        assert total == RunMetrics(cost_usd=1.25, turns=7, duration_seconds=13)

    def test_a_none_reading_does_not_poison_the_other(self):
        base = RunMetrics(cost_usd=1.0, turns=5, duration_seconds=10)
        extra = RunMetrics()  # repair round crashed before its own result event
        total = _accumulate_metrics(base, extra)
        assert total == RunMetrics(cost_usd=1.0, turns=5, duration_seconds=10)

    def test_both_none_stays_none(self):
        assert _accumulate_metrics(RunMetrics(), RunMetrics()) == RunMetrics()


class FakeDbSync:
    """Stand-in for dbsync.DbSync — Task 21 supplies the real one.

    ProcessManager only ever calls methods on this object; it never imports
    the concrete class, so this fake is sufficient to prove the wiring here
    without dbsync.py existing yet.
    """

    def __init__(self):
        self.started: list[dict] = []
        self.finished: list[dict] = []

    def release_lease(self, issue_id):
        # reap_dead (Task 13) unconditionally releases the lease for any
        # dbsync it has, regardless of what this test is exercising — a
        # no-op here, since lease behaviour is out of scope for this file.
        pass

    def start_run(self, *, run_id, issue_id, mode, model):
        self.started.append(dict(run_id=run_id, issue_id=issue_id, mode=mode, model=model))

    def finish_run(self, *, run_id, outcome, exit_code, duration_seconds,
                    cost_usd, turns, crash_log_path):
        self.finished.append(dict(
            run_id=run_id, outcome=outcome, exit_code=exit_code,
            duration_seconds=duration_seconds, cost_usd=cost_usd, turns=turns,
            crash_log_path=crash_log_path,
        ))


class FakePmLogger:
    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass

    def drain_queue(self, _q):
        pass


class FakePmState:
    def __init__(self, records):
        self._records = records

    def get(self, issue_id):
        return self._records.get(issue_id)

    def transition(self, issue_id, status):
        self._records[issue_id].status = status

    def update(self, issue_id, **kwargs):
        for k, v in kwargs.items():
            setattr(self._records[issue_id], k, v)

    def save(self):
        pass


def _make_pm_with_dbsync(records=None):
    import queue
    from process_manager import ProcessManager

    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=30, max_continuations=2),
    )
    dbsync = FakeDbSync()
    pm = ProcessManager(
        config=config,
        state=FakePmState(records or {}),
        logger=FakePmLogger(),
        log_queue=queue.Queue(),
        state_queue=queue.Queue(),
        dbsync=dbsync,
    )
    return pm, dbsync


class TestRunRowLifecycleViaDrainStateQueue:
    def test_the_first_update_of_a_run_opens_a_run_row(self):
        rec = SimpleNamespace(status="queued", error=None, branch=None, pr_url=None,
                              worker_pid=None, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._state_queue.put(StateUpdate(
            "r#1", "in_progress", worker_pid=111,
            run_id="run1", run_mode="dev", run_model="claude-sonnet-4-5",
        ))
        pm.drain_state_queue()
        assert dbsync.started == [dict(
            run_id="run1", issue_id="r#1", mode="dev", model="claude-sonnet-4-5",
        )]
        assert pm._active_runs["r#1"] == "run1"

    def test_the_terminal_update_of_a_run_closes_the_run_row(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "completed", run_id="run1", run_outcome="completed",
            exit_code=0, duration_seconds=90, cost_usd=0.42, turns=6,
        ))
        pm.drain_state_queue()
        assert dbsync.finished == [dict(
            run_id="run1", outcome="completed", exit_code=0, duration_seconds=90,
            cost_usd=0.42, turns=6, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs

    def test_no_dbsync_wired_is_a_silent_no_op(self):
        # Guards the sequencing gap: main/process_manager wiring may not yet
        # pass a dbsync (Phase C not landed, or Postgres disabled) — this
        # must never raise.
        import queue
        from process_manager import ProcessManager
        rec = SimpleNamespace(status="queued", error=None, branch=None, pr_url=None,
                              worker_pid=None, handoff_summary=None)
        config = SimpleNamespace(
            workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=30),
        )
        pm = ProcessManager(
            config=config, state=FakePmState({"r#1": rec}), logger=FakePmLogger(),
            log_queue=queue.Queue(), state_queue=queue.Queue(),
        )
        pm._state_queue.put(StateUpdate(
            "r#1", "in_progress", run_id="run1", run_mode="dev", run_model="m",
        ))
        pm.drain_state_queue()  # must not raise


class TestRunRowClosedOnCrash:
    def test_reap_dead_closes_a_run_left_open_by_a_crash_with_no_update(self):
        rec = SimpleNamespace(status="in_progress", error=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._workers["r#1"] = (FakeProc(exitcode=1), object())

        pm.reap_dead()

        assert dbsync.finished == [dict(
            run_id="run1", outcome="failed", exit_code=1, duration_seconds=None,
            cost_usd=None, turns=None, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs

    def test_mark_interrupted_closes_a_run_left_open_by_shutdown(self):
        rec = SimpleNamespace(status="in_progress", error=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"

        pm._mark_interrupted("r#1", exit_code=-15)

        assert dbsync.finished == [dict(
            run_id="run1", outcome="interrupted", exit_code=-15, duration_seconds=None,
            cost_usd=None, turns=None, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs

    def test_a_run_already_closed_normally_is_not_double_closed(self):
        # No entry in _active_runs means drain_state_queue already handled it.
        rec = SimpleNamespace(status="completed", error=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._mark_interrupted("r#1", exit_code=0)
        assert dbsync.finished == []


class FakeProc:
    """A worker process that has already exited."""

    def __init__(self, exitcode=1):
        self.exitcode = exitcode
        self.pid = 1234

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass
