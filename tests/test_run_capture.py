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
        self.summaries: list[dict] = []

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

    def add_summary(self, *, issue_id, run_id, kind, body, comment_url):
        self.summaries.append(dict(
            issue_id=issue_id, run_id=run_id, kind=kind, body=body, comment_url=comment_url,
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

    def test_reap_dead_closes_a_dangling_run_even_when_the_issue_record_is_gone(self):
        # Task 18 review Finding 2: reap_dead popped the dead worker and its
        # color slot, then hit `if record is None: continue` *before* ever
        # reaching `_close_dangling_run` — so a run left open by a worker
        # whose issue record vanished from the state store (removed between
        # spawn and reap) stayed open forever: nothing else keys off that
        # issue_id once its record is gone. No `record` fixture at all here,
        # by design — the state store has nothing for "r#1".
        pm, dbsync = _make_pm_with_dbsync({})
        pm._active_runs["r#1"] = "run1"
        pm._workers["r#1"] = (FakeProc(exitcode=137), object())

        pm.reap_dead()

        assert dbsync.finished == [dict(
            run_id="run1", outcome="failed", exit_code=137, duration_seconds=None,
            cost_usd=None, turns=None, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs


class TestRunRowClosedOnFencing:
    def test_a_fenced_state_update_closes_the_run_row(self):
        # Companion to tests/test_worker_fencing.py's TestHandleLeaseLost
        # fenced-outcome tests: this confirms the outcome the worker now
        # sends actually reaches DbSync.finish_run through drain_state_queue,
        # the same path every other terminal outcome goes through.
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "failed", error="fenced: lease lost",
            run_id="run1", run_outcome="fenced",
            duration_seconds=20, cost_usd=0.5, turns=3,
        ))
        pm.drain_state_queue()
        assert dbsync.finished == [dict(
            run_id="run1", outcome="fenced", exit_code=None, duration_seconds=20,
            cost_usd=0.5, turns=3, crash_log_path=None,
        )]
        assert "r#1" not in pm._active_runs


class FakeProc:
    """A worker process that has already exited."""

    def __init__(self, exitcode=1):
        self.exitcode = exitcode
        self.pid = 1234

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


# ---------------------------------------------------------------------------
# run_dev_worker end-to-end: the accumulate-then-emit path
#
# Task 18 review Finding 3: every test above builds a StateUpdate by hand —
# nothing actually drove run_dev_worker or run_review_worker, so a regression
# in the wiring itself (e.g. the terminal StateUpdate reading the pre-`try`
# `RunMetrics()` seed instead of the `metrics` variable rebound by
# `_run_claude`/`_accumulate_metrics`) would pass every other test in this
# file. This drives run_dev_worker's real success path — including a forced
# repair round, the one place metrics from two separate `_run_claude` calls
# must be summed rather than either one alone — with every subprocess/
# network touchpoint monkeypatched out, mirroring
# tests/test_review_worker.py's TestRunReviewWorkerPrResolution.
# ---------------------------------------------------------------------------

class _FakeWorkerQueue:
    """multiprocessing.Queue stand-in — records puts, no process boundary."""

    def __init__(self):
        self.items: list = []

    def put(self, item) -> None:
        self.items.append(item)


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeAbortEvent:
    def is_set(self) -> bool:
        return False


def _make_dev_ctx(tmp_path):
    import worker as worker_module
    return worker_module.IssueContext(
        issue_id="repo#1",
        repo="repo",
        number=1,
        title="Add a widget",
        body="b",
        action="implement",
        org="org",
        base_branch="main",
        repos_dir=tmp_path / "repos",
        worktrees_dir=tmp_path / "worktrees",
        prompts_dir=Path(__file__).resolve().parent.parent / "prompts",
        dev_model="model",
        light_model="model",
        permission_mode="bypassPermissions",
        max_budget_usd=1.0,
        max_turns=5,
        crash_logs_dir=tmp_path / "crash",
        color_name="RED",
        color_code="\033[91m",
    )


def _stub_dev_worker_happy_path_with_one_repair_round(monkeypatch):
    """Stub every touchpoint of run_dev_worker's success path, forcing exactly
    one repair round so the accumulate-then-emit path actually runs."""
    import worker as worker_module

    monkeypatch.setattr(worker_module, "_claim_labels", lambda ctx, logger: None)
    monkeypatch.setattr(worker_module, "_clone_or_fetch", lambda ctx, logger: Path("."))
    monkeypatch.setattr(worker_module, "_cleanup_worktree", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "_get_issue_comments", lambda ctx, logger: [])
    monkeypatch.setattr(worker_module, "_build_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(worker_module, "_post_issue_report", lambda *a, **k: ("report body", None))
    monkeypatch.setattr(worker_module, "_write_crash_log", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "_post_crash_comment", lambda *a, **k: (None, "crash body"))
    monkeypatch.setattr(
        worker_module, "_push_and_pr",
        lambda *a, **k: "https://github.com/org/repo/pull/1",
    )

    def fake_run_cmd(cmd, **_kwargs):
        if cmd[:2] == ["git", "status"]:
            return _FakeCompletedProcess(returncode=0, stdout=" M src/foo.py\n")
        if cmd[:2] == ["git", "log"]:
            return _FakeCompletedProcess(returncode=0, stdout="abc123 commit\n")
        return _FakeCompletedProcess(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker_module, "_run_cmd", fake_run_cmd)

    run_claude_calls = {"n": 0}

    def fake_run_claude(**_kwargs):
        run_claude_calls["n"] += 1
        if run_claude_calls["n"] == 1:
            return (0, "primary output", False, None,
                    worker_module.RunMetrics(cost_usd=1.0, turns=5, duration_seconds=30))
        return (0, "repair output", False, None,
                worker_module.RunMetrics(cost_usd=0.25, turns=2, duration_seconds=3))

    monkeypatch.setattr(worker_module, "_run_claude", fake_run_claude)

    check_calls = {"n": 0}

    def fake_prepare_and_check(ctx, worktree_dir, logger):
        check_calls["n"] += 1
        if check_calls["n"] == 1:
            return False, "checks failed"
        return True, "ok"

    monkeypatch.setattr(worker_module, "_prepare_and_check", fake_prepare_and_check)
    return run_claude_calls, check_calls


class TestRunDevWorkerEmitsAccumulatedMetrics:
    def test_a_repair_rounds_metrics_are_folded_into_the_terminal_state_update(
        self, monkeypatch, tmp_path,
    ):
        import worker as worker_module

        ctx = _make_dev_ctx(tmp_path)
        run_claude_calls, check_calls = _stub_dev_worker_happy_path_with_one_repair_round(
            monkeypatch,
        )
        state_queue = _FakeWorkerQueue()

        worker_module.run_dev_worker(ctx, _FakeWorkerQueue(), state_queue, _FakeAbortEvent())

        assert run_claude_calls["n"] == 2, "primary run + exactly one repair round"
        assert check_calls["n"] == 2, "checks fail once, then pass after the repair round"

        opening = state_queue.items[0]
        assert opening.run_mode == "dev"
        assert opening.run_id is not None

        terminal = state_queue.items[-1]
        assert terminal.status == "completed"
        assert terminal.run_outcome == "completed"
        assert terminal.run_id == opening.run_id
        # 1.0 + 0.25, 5 + 2, 30 + 3 — the primary run's metrics plus the
        # repair round's, per _accumulate_metrics. A regression that let the
        # pre-`try` `RunMetrics()` seed reach the terminal StateUpdate
        # instead (rather than the rebound-then-accumulated `metrics`
        # variable) would report all three fields as None here.
        assert terminal.cost_usd == 1.25
        assert terminal.turns == 7
        assert terminal.duration_seconds == 33


class TestPostIssueReportReturnsBodyAndUrl:
    def test_returns_the_exact_body_and_the_comment_url_on_success(self, tmp_path, monkeypatch):
        import worker

        ctx = SimpleNamespace(
            number=7, org="o", repo="r", dev_model="m",
        )
        monkeypatch.setattr(worker, "_get_issue_labels", lambda ctx, logger: [])
        monkeypatch.setattr(
            worker, "_run_cmd",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="https://github.com/o/r/issues/7#issuecomment-1\n",
                stderr="",
            ),
        )
        body, url = worker._post_issue_report(
            ctx, output="", summary="did the thing", branch="ac/issue-7-x",
            pr_url="https://github.com/o/r/pull/7", outcome="success",
            logger=SimpleNamespace(warn=lambda *a: None),
        )
        assert "did the thing" in body
        assert url == "https://github.com/o/r/issues/7#issuecomment-1"

    def test_returns_none_url_when_the_post_fails(self, tmp_path, monkeypatch):
        # Review fix (round 1): the original ctx omitted dev_model, so
        # `_post_issue_report`'s `model=ctx.dev_model` read raised
        # AttributeError inside the outer try — caught by the OUTER except,
        # not the `result.returncode != 0` branch this test is meant to
        # cover. `url is None` passed either way, for the wrong reason, and
        # the second assertion (`"s" in body or body is not None`) was a
        # tautology since `body is not None` is always true for a str. Adding
        # dev_model reaches the intended branch; the replacement assertion
        # actually checks the body the caller would have posted.
        import worker

        ctx = SimpleNamespace(number=7, org="o", repo="r", dev_model="m")
        monkeypatch.setattr(worker, "_get_issue_labels", lambda ctx, logger: [])
        monkeypatch.setattr(
            worker, "_run_cmd",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        body, url = worker._post_issue_report(
            ctx, output="", summary="s", branch="b", pr_url=None,
            outcome="success", logger=SimpleNamespace(warn=lambda *a: None),
        )
        assert url is None
        assert "s" in body, "the built report body must still carry the summary text"


class TestPostCrashCommentReturnsUrlAndBody:
    def test_returns_the_url_and_the_exact_posted_body(self, tmp_path, monkeypatch):
        import worker

        ctx = SimpleNamespace(number=3, org="o", repo="r")

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(
                returncode=0, stdout="https://github.com/o/r/issues/3#issuecomment-9\n",
                stderr="",
            )

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        url, body = worker._post_crash_comment(
            ctx, "boom", None, SimpleNamespace(error=lambda *a: None),
        )
        assert url == "https://github.com/o/r/issues/3#issuecomment-9"
        assert "boom" in body


class TestPostPrReviewReturnsUrlFromApiLookup:
    def test_looks_up_the_review_url_after_a_successful_post(self, monkeypatch):
        import worker

        ctx = SimpleNamespace(
            pr_url="https://github.com/o/r/pull/4", org="o", repo="r", harness_id=None,
        )
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "review"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0, stdout="https://github.com/o/r/pull/4#pullrequestreview-1\n",
                stderr="",
            )

        monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
        url = worker._post_pr_review(
            ctx, SimpleNamespace(warn=lambda *a: None), approve=True, body="looks good",
        )
        assert url == "https://github.com/o/r/pull/4#pullrequestreview-1"
        assert any(c[:2] == ["gh", "api"] for c in calls)

    def test_no_pr_number_returns_none_without_calling_gh(self, monkeypatch):
        import worker

        ctx = SimpleNamespace(pr_url=None, org="o", repo="r")
        monkeypatch.setattr(
            worker, "_run_cmd", lambda *a, **k: pytest.fail("must not call gh with no PR")
        )
        url = worker._post_pr_review(
            ctx, SimpleNamespace(warn=lambda *a: None), approve=True, body="x",
        )
        assert url is None

    def test_a_lookup_timeout_degrades_to_none_without_failing_the_already_posted_review(
        self, monkeypatch,
    ):
        # Review fix (round 1, Finding 1): _run_cmd does not catch
        # subprocess.TimeoutExpired/FileNotFoundError, so a timeout on the
        # follow-up URL lookup used to propagate out of _post_pr_review
        # *after* `gh pr review` had already posted successfully — an
        # approved review recorded as a crashed run. The lookup must degrade
        # to comment_url=None instead.
        import subprocess

        import worker

        ctx = SimpleNamespace(
            pr_url="https://github.com/o/r/pull/4", org="o", repo="r", harness_id=None,
        )

        def fake_run_cmd(cmd, **kwargs):
            if cmd[:3] == ["gh", "pr", "review"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
        url = worker._post_pr_review(
            ctx, SimpleNamespace(warn=lambda *a: None), approve=True, body="looks good",
        )
        assert url is None

    def test_a_multi_page_reviews_response_selects_the_true_last_review(self, monkeypatch):
        # Review fix (round 1, Finding 2): `gh api` without --paginate only
        # fetches page 1 (30 items). On a PR with >30 reviews, the old
        # `.[-1]` selected the 30th-OLDEST review, not the one this run just
        # posted — a confidently wrong URL, worse than NULL per this task's
        # contract. --paginate + `.[].html_url` now prints one URL per line
        # across every page, oldest first; the true last line is the most
        # recent review regardless of how many pages preceded it.
        import worker

        ctx = SimpleNamespace(
            pr_url="https://github.com/o/r/pull/4", org="o", repo="r", harness_id=None,
        )
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "review"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            # Simulate 31 reviews spread across two pages, oldest first —
            # the OLD `.[-1]` on an unpaginated call would have returned
            # review-30 (page 1's last item), not review-31 (the real last).
            urls = [f"https://github.com/o/r/pull/4#pullrequestreview-{n}" for n in range(1, 32)]
            return SimpleNamespace(returncode=0, stdout="\n".join(urls) + "\n", stderr="")

        monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
        url = worker._post_pr_review(
            ctx, SimpleNamespace(warn=lambda *a: None), approve=True, body="looks good",
        )
        assert url == "https://github.com/o/r/pull/4#pullrequestreview-31"
        assert any("--paginate" in c for c in calls)


class TestProcessManagerPostsSummaries:
    def test_drain_state_queue_writes_every_summary_on_the_terminal_update(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "completed", run_id="run1", run_outcome="completed",
            summaries=[
                {"kind": "dev", "body": "did it", "comment_url": "https://x/1"},
            ],
        ))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", None) == [
            dict(issue_id="r#1", run_id="run1", kind="dev", body="did it",
                 comment_url="https://x/1"),
        ]

    def test_no_summaries_field_posts_nothing(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate("r#1", "completed", run_id="run1", run_outcome="completed"))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", []) == []

    def test_dbsync_property_exposes_the_wired_instance(self):
        pm, dbsync = _make_pm_with_dbsync({})
        assert pm.dbsync is dbsync

    def test_a_fenced_error_writes_a_fenced_summary_row_even_with_no_summaries_list(self):
        # Task 14's _handle_lease_lost sends error="fenced: ..." with
        # summaries=None (it never touches GitHub, so there is no comment to
        # report) — that hand-off must still produce a Postgres summary row,
        # or it silently falls through the gap.
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "failed", error="fenced: lease lost", run_id="run1",
            run_outcome="failed",
        ))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", None) == [
            dict(issue_id="r#1", run_id="run1", kind="fenced",
                 body="fenced: lease lost", comment_url=None),
        ]

    def test_a_non_fenced_error_does_not_write_a_fenced_summary(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None, pr_url=None,
                              worker_pid=111, handoff_summary=None)
        pm, dbsync = _make_pm_with_dbsync({"r#1": rec})
        pm._active_runs["r#1"] = "run1"
        pm._state_queue.put(StateUpdate(
            "r#1", "failed", error="budget_exceeded", run_id="run1", run_outcome="failed",
        ))
        pm.drain_state_queue()
        assert getattr(dbsync, "summaries", []) == []


# ---------------------------------------------------------------------------
# Task 14's reviewer flagged (and Task 19's dispatch recorded) a carry-forward
# gap: _handle_lease_lost is tested in isolation (tests/test_worker_fencing.py),
# but nothing asserts that `except LeaseLostError` actually precedes
# `except Exception` inside run_dev_worker/run_review_worker themselves. A
# one-line reordering or deletion there would let a fenced exit fall through
# to the generic crash handler and emit error=str(exc) with no "fenced: "
# prefix — silently breaking process_manager's `kind="fenced"` summary (see
# TestProcessManagerPostsSummaries above) with zero other test failures.
# These drive the real entry points end-to-end, mirroring
# TestRunDevWorkerEmitsAccumulatedMetrics's monkeypatch-every-touchpoint style.
# ---------------------------------------------------------------------------

class TestRunDevWorkerFencesOnLeaseLost:
    def test_a_lease_lost_error_produces_a_single_fenced_terminal_update(
        self, monkeypatch, tmp_path,
    ):
        import worker as worker_module

        ctx = _make_dev_ctx(tmp_path)

        def raise_lease_lost(ctx, logger):
            raise worker_module.LeaseLostError("lease lost")

        # First touchpoint inside run_dev_worker's try block — raising here
        # proves the wiring without needing to stub the rest of the pipeline.
        monkeypatch.setattr(worker_module, "_claim_labels", raise_lease_lost)
        state_queue = _FakeWorkerQueue()

        worker_module.run_dev_worker(ctx, _FakeWorkerQueue(), state_queue, _FakeAbortEvent())

        # Exactly two updates: the opening "in_progress" and the fenced exit —
        # if this ever fell through to the generic `except Exception` instead,
        # a crash-comment post would be attempted too (and would blow up here
        # since gh/subprocess are not stubbed).
        assert len(state_queue.items) == 2
        terminal = state_queue.items[-1]
        assert terminal.status == "failed"
        assert terminal.error.startswith("fenced: "), (
            "a LeaseLostError raised inside run_dev_worker must be caught by "
            "its own except clause, not fall through to the generic handler"
        )


class TestRunReviewWorkerFencesOnLeaseLost:
    def test_a_lease_lost_error_produces_a_single_fenced_terminal_update(
        self, monkeypatch, tmp_path,
    ):
        import worker as worker_module

        ctx = _make_ctx_for_review_fencing(tmp_path)

        def raise_lease_lost(ctx, logger):
            raise worker_module.LeaseLostError("lease lost")

        # First touchpoint inside run_review_worker's try block.
        monkeypatch.setattr(worker_module, "_claim_review_labels", raise_lease_lost)
        state_queue = _FakeWorkerQueue()

        worker_module.run_review_worker(ctx, _FakeWorkerQueue(), state_queue, _FakeAbortEvent())

        assert len(state_queue.items) == 2
        terminal = state_queue.items[-1]
        assert terminal.status == "failed"
        assert terminal.error.startswith("fenced: "), (
            "a LeaseLostError raised inside run_review_worker must be caught "
            "by its own except clause, not fall through to the generic handler"
        )


def _make_ctx_for_review_fencing(tmp_path):
    import worker as worker_module
    return worker_module.IssueContext(
        issue_id="repo#1",
        repo="repo",
        number=1,
        title="Add a widget",
        body="b",
        action="fix",
        org="org",
        base_branch="main",
        repos_dir=tmp_path / "repos",
        worktrees_dir=tmp_path / "worktrees",
        prompts_dir=Path(__file__).resolve().parent.parent / "prompts",
        dev_model="model",
        light_model="model",
        permission_mode="bypassPermissions",
        max_budget_usd=1.0,
        max_turns=5,
        crash_logs_dir=tmp_path / "crash",
        color_name="RED",
        color_code="\033[91m",
        existing_branch="ac/issue-1-x",
        pr_url="https://github.com/org/repo/pull/1",
    )
