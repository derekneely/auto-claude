"""A crash must not erase the branch and PR the run already created.

Regression: field_admin#215, 2026-07-31. The dev worker pushed a branch and
opened PR #334, then threw on the way out. The crash handler's `StateUpdate`
carried `status`, `error`, and the run metrics — but not `branch` or `pr_url`,
unlike every other terminal path in the same function. `issue_state.branch` and
`issue_state.pr_url` stayed NULL with a live PR sitting on GitHub, and the
local record lost the only pointer back to the work.

That matters beyond cosmetics: `poller`'s rework branch requires
`record.branch and record.pr_url` to resurrect a completed issue on its
existing branch. Without them a rework falls through to the retry branch and
starts over on a fresh branch, abandoning the open PR.

The values are known at crash time in every case — `branch` is computed before
the `try` in the dev worker, and the review worker resolves `ctx.pr_url` /
`ctx.existing_branch` as its first act — so there is no reason to drop them.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402


PR_URL = "https://github.com/Accelevation/field_admin/pull/334"


def _ctx(tmp_path, **overrides):
    fields = dict(
        issue_id="field_admin#215",
        repo="field_admin",
        number=215,
        title="P2 job wizard show real loading progress",
        body="",
        action="implement",
        org="Accelevation",
        base_branch="dev",
        repos_dir=tmp_path / "repos",
        worktrees_dir=tmp_path / "worktrees",
        prompts_dir=tmp_path / "prompts",
        dev_model="opus",
        light_model="sonnet",
        permission_mode="acceptEdits",
        max_budget_usd=10.0,
        max_turns=100,
        crash_logs_dir=tmp_path / "crash_logs",
        color_name="blue",
        color_code="\033[34m",
    )
    fields.update(overrides)
    return worker.IssueContext(**fields)


def _ok(cmd, **kwargs):
    # Non-empty stdout so the dev worker's "did Claude change anything?" probe
    # sees uncommitted work rather than raising "No changes produced".
    return subprocess.CompletedProcess(cmd, 0, "M src/app.tsx\n", "")


def _stub_crash_reporting(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_write_crash_log",
                        lambda *_a, **_k: tmp_path / "crash.log")
    monkeypatch.setattr(worker, "_post_crash_comment",
                        lambda *_a, **_k: ("https://gh/comment/1", "crash body"))


def _final_update(state_queue: queue.Queue):
    updates = []
    while not state_queue.empty():
        updates.append(state_queue.get_nowait())
    assert updates, "worker put nothing on the state queue"
    return updates[-1]


def _run_dev(ctx):
    state_queue: queue.Queue = queue.Queue()
    worker.run_dev_worker(ctx, queue.Queue(), state_queue, threading.Event())
    return _final_update(state_queue)


class TestDevWorkerCrashAfterThePrExists:
    """The exact #215 shape: everything succeeded, step [8] threw."""

    def _arrange(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "_claim_labels", lambda *_a, **_k: None)
        monkeypatch.setattr(worker, "_clone_or_fetch",
                            lambda *_a, **_k: tmp_path / "repos" / "field_admin")
        monkeypatch.setattr(worker, "_run_cmd", _ok)
        monkeypatch.setattr(worker, "_get_issue_comments", lambda *_a, **_k: [])
        monkeypatch.setattr(worker, "_build_prompt", lambda *_a, **_k: "prompt")
        monkeypatch.setattr(
            worker, "_run_claude",
            lambda **_k: (0, "done", False, None,
                          worker.RunMetrics(cost_usd=6.72, turns=55,
                                            duration_seconds=2440)),
        )
        monkeypatch.setattr(worker, "_prepare_and_check", lambda *_a, **_k: (True, ""))
        monkeypatch.setattr(worker, "_push_and_pr", lambda *_a, **_k: PR_URL)
        monkeypatch.setattr(worker, "_cleanup_worktree_best_effort",
                            lambda *_a, **_k: None)
        monkeypatch.setattr(worker, "_failure_labels", lambda *_a, **_k: False)
        _stub_crash_reporting(monkeypatch, tmp_path)

        # [8] — the issue report is posted after the PR is live, and it is the
        # last thing that can still throw.
        def explode(*_a, **_k):
            raise RuntimeError("gh issue comment failed")

        monkeypatch.setattr(worker, "_post_issue_report", explode)

    def test_pr_url_survives_the_crash(self, tmp_path, monkeypatch):
        self._arrange(monkeypatch, tmp_path)
        update = _run_dev(_ctx(tmp_path))

        assert update.status == "failed"
        assert update.pr_url == PR_URL

    def test_branch_survives_the_crash(self, tmp_path, monkeypatch):
        self._arrange(monkeypatch, tmp_path)
        ctx = _ctx(tmp_path)
        update = _run_dev(ctx)

        assert update.branch == worker.sanitize_branch_name(ctx.title, ctx.number)


class TestDevWorkerCrashBeforeAnythingWasPushed:
    def test_pr_url_is_none_and_the_handler_does_not_itself_crash(
        self, tmp_path, monkeypatch,
    ):
        """`pr_url` is assigned deep inside the `try`. Referencing it from the
        handler after an early crash must not raise NameError — that would
        replace one lost update with a dead worker."""
        monkeypatch.setattr(worker, "_claim_labels", lambda *_a, **_k: None)
        monkeypatch.setattr(worker, "_run_cmd", _ok)
        monkeypatch.setattr(worker, "_failure_labels", lambda *_a, **_k: False)
        _stub_crash_reporting(monkeypatch, tmp_path)

        def explode(*_a, **_k):
            raise RuntimeError("clone failed")

        monkeypatch.setattr(worker, "_clone_or_fetch", explode)

        ctx = _ctx(tmp_path)
        update = _run_dev(ctx)

        assert update.status == "failed"
        assert update.pr_url is None
        assert update.branch == worker.sanitize_branch_name(ctx.title, ctx.number)

    def test_a_rework_crash_keeps_the_inherited_pr(self, tmp_path, monkeypatch):
        """Rework arrives with the PR it is reworking already on `ctx`. Losing
        it here is what forces the next attempt onto a fresh branch."""
        monkeypatch.setattr(worker, "_claim_labels", lambda *_a, **_k: None)
        monkeypatch.setattr(worker, "_run_cmd", _ok)
        monkeypatch.setattr(worker, "_failure_labels", lambda *_a, **_k: False)
        _stub_crash_reporting(monkeypatch, tmp_path)

        def explode(*_a, **_k):
            raise RuntimeError("clone failed")

        monkeypatch.setattr(worker, "_clone_or_fetch", explode)

        update = _run_dev(_ctx(
            tmp_path,
            action="rework",
            existing_branch="ac/issue-215-p2-job-wizard",
            pr_url=PR_URL,
        ))

        assert update.pr_url == PR_URL


class TestReviewWorkerCrash:
    def test_the_reviewed_pr_survives_the_crash(self, tmp_path, monkeypatch):
        """The review worker's other terminal paths all carry `ctx.pr_url`;
        its crash path was the one that did not."""
        monkeypatch.setattr(worker, "_claim_review_labels", lambda *_a, **_k: None)
        monkeypatch.setattr(worker, "_run_cmd", _ok)
        monkeypatch.setattr(worker, "_release_review_lock_after_crash",
                            lambda *_a, **_k: None)
        _stub_crash_reporting(monkeypatch, tmp_path)

        def explode(*_a, **_k):
            raise RuntimeError("fetch failed")

        monkeypatch.setattr(worker, "_clone_or_fetch", explode)

        ctx = _ctx(tmp_path, action="review", pr_url=PR_URL,
                   existing_branch="ac/issue-215-p2-job-wizard")
        state_queue: queue.Queue = queue.Queue()
        worker.run_review_worker(ctx, queue.Queue(), state_queue, threading.Event())
        update = _final_update(state_queue)

        assert update.status == "failed"
        assert update.pr_url == PR_URL
        assert update.branch == "ac/issue-215-p2-job-wizard"
