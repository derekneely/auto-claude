"""Tests for worker.py's review-worker label logic and verdict parsing.

Pure functions only — no subprocess, no network, mirroring
tests/test_worker_labels.py's split between "compute add/remove from labels"
(tested here) and "read live labels then call gh" (worker.py, not tested here).

The `TestRunReviewWorkerPrResolution` class is the exception: it drives
`run_review_worker` itself, but with every subprocess/network touchpoint
monkeypatched out, to pin down the PR-lookup fallback (ctx.pr_url/
existing_branch are only populated when this daemon process ran the dev
worker that opened the PR — a restart, a human label, or a PR from the
sibling toolchain leaves them unset).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from logger import WorkerLogger  # noqa: E402
from worker import (  # noqa: E402
    IssueContext,
    RunMetrics,
    StateUpdate,
    _candidate_prs_for_issue,
    _extract_review_feedback,
    _labels_for_review_claim,
    _labels_for_review_crash,
    _labels_for_review_fail,
    _labels_for_review_pass,
    _parse_review_verdict,
    run_review_worker,
)


class TestLabelsForReviewClaim:
    def test_claim_from_dev_review(self):
        add, remove = _labels_for_review_claim(["ac-dev-review", "ac-fix", "ac-pr-created"])
        assert add == ["ac-review-in-progress"]
        assert remove == ["ac-dev-review"]

    def test_kind_label_survives(self):
        add, remove = _labels_for_review_claim(["ac-dev-review", "ac-implement"])
        assert "ac-implement" not in add
        assert "ac-implement" not in remove


class TestLabelsForReviewPass:
    def test_pass_hands_off_to_hitl(self):
        add, remove = _labels_for_review_pass(["ac-review-in-progress", "ac-fix"])
        assert add == ["ac-hitl"]
        assert remove == ["ac-review-in-progress"]

    def test_kind_label_survives(self):
        add, remove = _labels_for_review_pass(["ac-review-in-progress", "ac-implement"])
        assert "ac-implement" not in add
        assert "ac-implement" not in remove

    def test_preserves_existing_attempt_counter(self):
        add, remove = _labels_for_review_pass(["ac-review-in-progress", "ac-attempt-1"])
        assert "ac-attempt-1" not in add
        assert "ac-attempt-1" not in remove


class TestLabelsForReviewFail:
    def test_first_failure_goes_to_dev_ready_with_attempt_1(self):
        add, remove, blocked = _labels_for_review_fail(
            ["ac-review-in-progress", "ac-implement"]
        )
        assert set(add) == {"ac-dev-ready", "ac-attempt-1"}
        assert remove == ["ac-review-in-progress"]
        assert blocked is False

    def test_second_failure_bumps_to_attempt_2_and_drops_attempt_1(self):
        add, remove, blocked = _labels_for_review_fail(
            ["ac-review-in-progress", "ac-attempt-1", "ac-implement"]
        )
        assert set(add) == {"ac-dev-ready", "ac-attempt-2"}
        assert set(remove) == {"ac-review-in-progress", "ac-attempt-1"}
        assert blocked is False

    def test_third_failure_blocks_instead_of_requeueing(self):
        add, remove, blocked = _labels_for_review_fail(
            ["ac-review-in-progress", "ac-attempt-2", "ac-implement"]
        )
        assert set(add) == {"ac-blocked", "ac-attempt-3"}
        assert set(remove) == {"ac-review-in-progress", "ac-attempt-2"}
        assert blocked is True
        assert "ac-dev-ready" not in add

    def test_kind_label_survives_every_failure(self):
        for labels in (
            ["ac-review-in-progress", "ac-fix"],
            ["ac-review-in-progress", "ac-attempt-1", "ac-fix"],
            ["ac-review-in-progress", "ac-attempt-2", "ac-fix"],
        ):
            add, remove, _blocked = _labels_for_review_fail(labels)
            assert "ac-fix" not in add
            assert "ac-fix" not in remove

    def test_unrelated_labels_survive(self):
        add, remove, _blocked = _labels_for_review_fail(
            ["ac-review-in-progress", "bug", "P1"]
        )
        assert "bug" not in add and "bug" not in remove
        assert "P1" not in add and "P1" not in remove


class TestLabelsForReviewCrash:
    def test_releases_lock_back_to_dev_review(self):
        add, remove = _labels_for_review_crash(["ac-review-in-progress", "ac-fix"])
        assert add == ["ac-dev-review"]
        assert remove == ["ac-review-in-progress"]

    def test_does_not_consume_an_attempt(self):
        # A crash is an infra failure, not a review verdict — no ac-attempt-N bump.
        add, remove = _labels_for_review_crash(
            ["ac-review-in-progress", "ac-attempt-1", "ac-fix"]
        )
        assert "ac-attempt-1" not in add
        assert "ac-attempt-1" not in remove
        assert "ac-attempt-2" not in add

    def test_kind_label_survives(self):
        add, remove = _labels_for_review_crash(["ac-review-in-progress", "ac-rework"])
        assert "ac-rework" not in add
        assert "ac-rework" not in remove


class TestParseReviewVerdict:
    def test_pass_is_true(self):
        assert _parse_review_verdict("all good\n\nREVIEW_VERDICT: PASS\n") is True

    def test_fail_is_false(self):
        assert _parse_review_verdict("nope\n\nREVIEW_VERDICT: FAIL\n") is False

    def test_unparseable_output_is_treated_as_fail(self):
        # No marker at all — never approve on a verdict we could not read.
        assert _parse_review_verdict("I looked at the code and it seems okay.") is False

    def test_empty_output_is_treated_as_fail(self):
        assert _parse_review_verdict("") is False

    def test_case_insensitive(self):
        assert _parse_review_verdict("review_verdict: pass") is True

    def test_last_marker_wins_when_prompt_instructions_are_echoed(self):
        text = (
            "Output REVIEW_VERDICT: PASS or REVIEW_VERDICT: FAIL as your final line.\n\n"
            "REVIEW_VERDICT: FAIL\n"
        )
        assert _parse_review_verdict(text) is False

    def test_garbled_verdict_value_is_treated_as_fail(self):
        assert _parse_review_verdict("REVIEW_VERDICT: MAYBE") is False


class TestExtractReviewFeedback:
    def test_extracts_the_feedback_section(self):
        text = (
            "REVIEW_FEEDBACK:\n"
            "1. Fix the null check in foo.py:42\n"
            "REVIEW_VERDICT: FAIL\n"
        )
        feedback = _extract_review_feedback(text)
        assert "null check" in feedback
        assert "REVIEW_VERDICT" not in feedback

    def test_falls_back_to_full_text_without_the_marker(self):
        feedback = _extract_review_feedback("just some prose, no marker here")
        assert "just some prose" in feedback

    def test_empty_text_gives_a_non_empty_fallback(self):
        feedback = _extract_review_feedback("")
        assert feedback  # never post an empty --request-changes body


def test_run_review_worker_is_a_distinct_entry_point():
    # Guards against accidentally aliasing the two workers, which
    # tests/test_wiring.py::TestSpawnRouting also checks from process_manager's side.
    assert run_review_worker is not worker.run_dev_worker
    assert callable(run_review_worker)


class TestCandidatePrsForIssue:
    """Pure — matches loop-review-agent.md's PR discovery, no network."""

    def test_matches_by_branch_prefix(self):
        prs = [{"number": 1, "headRefName": "ac/issue-7-do-a-thing", "body": ""}]
        result = _candidate_prs_for_issue(prs, 7, "ac/issue-7-")
        assert [p["number"] for p in result] == [1]

    def test_matches_by_closing_keyword_in_body(self):
        prs = [{"number": 2, "headRefName": "some-other-branch", "body": "Closes #7"}]
        result = _candidate_prs_for_issue(prs, 7, "ac/issue-7-")
        assert [p["number"] for p in result] == [2]

    def test_no_match_returns_empty(self):
        prs = [{"number": 3, "headRefName": "ac/issue-8-x", "body": "fixes #8"}]
        assert _candidate_prs_for_issue(prs, 7, "ac/issue-7-") == []

    def test_multiple_matches_sorted_most_recent_first(self):
        prs = [
            {"number": 1, "headRefName": "ac/issue-7-old", "body": "",
             "updatedAt": "2026-01-01T00:00:00Z"},
            {"number": 2, "headRefName": "ac/issue-7-new", "body": "",
             "updatedAt": "2026-02-01T00:00:00Z"},
        ]
        result = _candidate_prs_for_issue(prs, 7, "ac/issue-7-")
        assert [p["number"] for p in result] == [2, 1]

    def test_no_prefix_collision_between_issue_7_and_issue_70(self):
        prs = [{"number": 4, "headRefName": "ac/issue-70-thing", "body": ""}]
        assert _candidate_prs_for_issue(prs, 7, "ac/issue-7-") == []


# ---------------------------------------------------------------------------
# run_review_worker: PR-resolution fallback
# ---------------------------------------------------------------------------

class _FakeQueue:
    """multiprocessing.Queue stand-in — records puts, no process boundary."""

    def __init__(self):
        self.items: list = []

    def put(self, item) -> None:
        self.items.append(item)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeAbortEvent:
    def is_set(self) -> bool:
        return False


def _make_ctx(tmp_path: Path, **overrides) -> IssueContext:
    defaults = dict(
        issue_id="repo#1",
        repo="repo",
        number=1,
        title="t",
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
        existing_branch=None,
        pr_url=None,
    )
    defaults.update(overrides)
    return IssueContext(**defaults)


def _stub_happy_path(monkeypatch, *, verdict: str = "REVIEW_VERDICT: PASS"):
    """Stub every subprocess/network touchpoint after PR resolution.

    Leaves `_find_pr_for_issue` for the caller to set, since that's what each
    test in this section varies.
    """
    monkeypatch.setattr(worker, "_clone_or_fetch", lambda ctx, logger: Path("."))
    monkeypatch.setattr(
        worker, "_setup_review_worktree", lambda ctx, repo_dir, worktree_dir, logger: ctx.existing_branch
    )
    monkeypatch.setattr(worker, "_run_pipeline_checks", lambda ctx, worktree_dir, logger: (True, "ok"))
    monkeypatch.setattr(worker, "_get_issue_comments", lambda ctx, logger: [])
    monkeypatch.setattr(worker, "_build_review_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(worker, "_run_claude", lambda **k: (0, verdict, False, None, RunMetrics()))
    monkeypatch.setattr(worker, "_run_cmd", lambda *a, **k: _FakeCompleted())
    monkeypatch.setattr(worker, "_claim_review_labels", lambda ctx, logger: None)
    monkeypatch.setattr(worker, "_review_pass_labels", lambda ctx, logger: None)
    monkeypatch.setattr(worker, "_review_fail_labels", lambda ctx, logger: False)
    monkeypatch.setattr(worker, "_write_crash_log", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: (None, "crash body"))


class TestRunReviewWorkerPrResolution:
    def test_uses_ctx_pr_url_and_branch_without_a_lookup(self, monkeypatch, tmp_path):
        ctx = _make_ctx(
            tmp_path,
            pr_url="https://github.com/org/repo/pull/5",
            existing_branch="ac/issue-1-x",
        )
        _stub_happy_path(monkeypatch)
        monkeypatch.setattr(
            worker, "_find_pr_for_issue",
            lambda *a, **k: pytest.fail("must not look up a PR when ctx already has one"),
        )
        reviews = []
        monkeypatch.setattr(
            worker, "_post_pr_review",
            lambda ctx, logger, *, approve, body: reviews.append((approve, body)),
        )

        worker.run_review_worker(ctx, _FakeQueue(), _FakeQueue(), _FakeAbortEvent())

        assert reviews == [(True, reviews[0][1])]
        assert ctx.pr_url == "https://github.com/org/repo/pull/5"

    def test_looks_up_the_pr_when_ctx_has_none(self, monkeypatch, tmp_path):
        ctx = _make_ctx(tmp_path, pr_url=None, existing_branch=None)
        match = {
            "number": 9,
            "headRefName": "ac/issue-1-fix-thing",
            "url": "https://github.com/org/repo/pull/9",
        }
        found = []
        monkeypatch.setattr(
            worker, "_find_pr_for_issue",
            lambda c, logger: found.append(1) or match,
        )
        _stub_happy_path(monkeypatch)
        monkeypatch.setattr(worker, "_post_pr_review", lambda *a, **k: None)

        worker.run_review_worker(ctx, _FakeQueue(), _FakeQueue(), _FakeAbortEvent())

        assert found == [1]
        assert ctx.pr_url == match["url"]
        assert ctx.existing_branch == match["headRefName"]

    def test_no_pr_found_does_not_approve_or_crash_and_releases_the_lock(
        self, monkeypatch, tmp_path
    ):
        ctx = _make_ctx(tmp_path, pr_url=None, existing_branch=None)
        monkeypatch.setattr(worker, "_find_pr_for_issue", lambda c, logger: None)
        monkeypatch.setattr(
            worker, "_clone_or_fetch",
            lambda *a, **k: pytest.fail("must not clone/fetch without a PR to review"),
        )
        reviews = []
        monkeypatch.setattr(worker, "_post_pr_review", lambda *a, **k: reviews.append(1))
        released = []
        monkeypatch.setattr(
            worker, "_release_review_lock_after_crash",
            lambda ctx, logger: released.append(ctx.issue_id),
        )
        monkeypatch.setattr(worker, "_claim_review_labels", lambda ctx, logger: None)
        monkeypatch.setattr(worker, "_write_crash_log", lambda *a, **k: None)
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: (None, "crash body"))
        monkeypatch.setattr(worker, "_run_cmd", lambda *a, **k: _FakeCompleted())

        state_queue = _FakeQueue()
        # Must not raise — the caller (process_manager) never sees an exception.
        worker.run_review_worker(ctx, _FakeQueue(), state_queue, _FakeAbortEvent())

        assert reviews == [], "must never approve or request-changes with no PR found"
        assert released == [ctx.issue_id], "the self-lock must be released"
        assert state_queue.items[-1].status == "failed"


# ---------------------------------------------------------------------------
# Merged-PR short-circuit (Task 4)
# ---------------------------------------------------------------------------

@pytest.fixture
def review_env(monkeypatch, tmp_path):
    """Build a fully-stubbed `run_review_worker` environment.

    Follows this file's existing convention (`_make_ctx`, `_FakeQueue`,
    `_FakeCompleted`, `_FakeAbortEvent`, direct `monkeypatch.setattr(worker, ...)`
    on the touchpoints `_stub_happy_path` already stubs) rather than
    introducing a second fake style.

    `_run_cmd` is faked at the same chokepoint the production code calls —
    `_check_pr_already_merged` / `_merged_pr_for_issue` / `_find_pr_for_issue`
    route `gh pr view`, `gh pr list --state merged` and `gh pr list --state
    open` through it for real, so a test asserting on the result is
    exercising the actual implementation, not the fixture. `open_list` feeds
    the open-state query — needed to prove a newer open PR takes priority
    over a stale merged one. `_claim_review_labels` and `_get_issue_labels`
    are also left real (routed through the same `_run_cmd` fake, answering
    `gh api ...` with an empty label set) so the self-lock genuinely runs
    before the short-circuit.
    """

    def _make(*, pr_url=None, pr_view=None, merged_list=None, open_list=None,
              number=1, gh_fails=False):
        ctx = _make_ctx(
            tmp_path,
            issue_id=f"repo#{number}",
            number=number,
            pr_url=pr_url,
            existing_branch=f"ac/issue-{number}-x" if pr_url else None,
        )
        log_q = _FakeQueue()
        state_q = _FakeQueue()
        abort = _FakeAbortEvent()
        logger = WorkerLogger(log_q, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)

        env = SimpleNamespace(
            ctx=ctx,
            logger=logger,
            log_q=log_q,
            state_q=state_q,
            abort=abort,
            claude_invocations=0,
            labels_added=[],
            worktrees_created=[],
        )
        env.final_status = lambda: state_q.items[-1].status if state_q.items else None

        def fake_run_cmd(args, **kwargs):
            if gh_fails:
                return _FakeCompleted(returncode=1, stderr="gh: boom")
            if args[:3] == ["gh", "pr", "view"]:
                return _FakeCompleted(returncode=0, stdout=json.dumps(pr_view or {}))
            if args[:3] == ["gh", "pr", "list"] and "merged" in args:
                return _FakeCompleted(returncode=0, stdout=json.dumps(merged_list or []))
            if args[:3] == ["gh", "pr", "list"]:
                return _FakeCompleted(returncode=0, stdout=json.dumps(open_list or []))
            if args[:2] == ["gh", "api"]:
                return _FakeCompleted(returncode=0, stdout=json.dumps({"labels": []}))
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)

        def fake_set_labels(ctx, logger, add=None, remove=None):
            if add:
                env.labels_added.extend(add)

        monkeypatch.setattr(worker, "_set_labels", fake_set_labels)

        def fake_setup_worktree(ctx, repo_dir, worktree_dir, logger):
            env.worktrees_created.append(worktree_dir)
            return ctx.existing_branch

        monkeypatch.setattr(worker, "_setup_review_worktree", fake_setup_worktree)
        monkeypatch.setattr(worker, "_clone_or_fetch", lambda ctx, logger: Path("."))
        monkeypatch.setattr(
            worker, "_run_pipeline_checks", lambda ctx, worktree_dir, logger: (True, "ok")
        )
        monkeypatch.setattr(worker, "_get_issue_comments", lambda ctx, logger: [])
        monkeypatch.setattr(worker, "_build_review_prompt", lambda *a, **k: "prompt")

        def fake_run_claude(**kwargs):
            env.claude_invocations += 1
            return (0, "REVIEW_VERDICT: PASS", False, None, RunMetrics())

        monkeypatch.setattr(worker, "_run_claude", fake_run_claude)
        monkeypatch.setattr(worker, "_post_pr_review", lambda *a, **k: None)
        monkeypatch.setattr(worker, "_write_crash_log", lambda *a, **k: None)
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: (None, "crash body"))

        return env

    return _make


class TestLabelsForReviewMerged:
    def test_it_targets_ac_merged(self):
        add, remove = worker._labels_for_review_merged(
            ["ac-review-in-progress", "ac-pr-created"]
        )
        assert add == ["ac-merged"]
        assert "ac-review-in-progress" in remove

    def test_control_labels_are_preserved(self):
        add, remove = worker._labels_for_review_merged(
            ["ac-review-in-progress", "ac-pr-created", "ac-attempt-2"]
        )
        assert "ac-pr-created" not in remove
        assert "ac-attempt-2" not in remove

    def test_it_never_targets_ac_done(self):
        add, _remove = worker._labels_for_review_merged(["ac-review-in-progress"])
        assert "ac-done" not in add


class TestCheckPrAlreadyMerged:
    def test_a_merged_pr_on_ctx_is_detected(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        assert worker._check_pr_already_merged(env.ctx, env.logger) is not None

    def test_an_open_pr_on_ctx_returns_none(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "OPEN"})
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None

    def test_a_closed_unmerged_pr_returns_none(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "CLOSED"})
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None

    def test_with_no_pr_url_it_searches_merged_prs(self, review_env):
        # _find_pr_for_issue lists only OPEN PRs, so a restart that lost
        # ctx.pr_url could not see a merged PR at all — the exact path that
        # produces the "No open PR found" crash loop.
        env = review_env(pr_url=None, merged_list=[
            {"number": 341, "headRefName": "ac/issue-268-attachments",
             "url": "https://github.com/Org/repo/pull/341", "body": "", "title": ""},
        ], number=268)
        assert worker._check_pr_already_merged(env.ctx, env.logger) is not None

    def test_a_gh_failure_returns_none_rather_than_raising(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         gh_fails=True)
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None

    def test_a_newer_open_pr_takes_priority_over_a_stale_merged_one(self, review_env):
        # Regression for the coordinator-flagged hazard: an issue can carry
        # both a stale merged PR (a re-opened issue, a superseded branch)
        # and a newer open one. With ctx.pr_url unset, the open PR is the
        # one that still needs reviewing — the merged lookup must not steal
        # it just because it runs first internally.
        env = review_env(
            pr_url=None,
            number=268,
            merged_list=[
                {"number": 340, "headRefName": "ac/issue-268-old-attempt",
                 "url": "https://github.com/Org/repo/pull/340", "body": "", "title": ""},
            ],
            open_list=[
                {"number": 341, "headRefName": "ac/issue-268-attachments",
                 "url": "https://github.com/Org/repo/pull/341", "body": "", "title": ""},
            ],
        )
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None


class TestReviewWorkerShortCircuit:
    def test_a_merged_pr_skips_claude_entirely(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.claude_invocations == 0

    def test_a_merged_pr_sets_ac_merged(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert "ac-merged" in env.labels_added

    def test_a_merged_pr_never_creates_a_worktree(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.worktrees_created == []

    def test_a_merged_pr_reports_completed_not_failed(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        update = env.state_q.items[-1]
        # `status == "completed"` alone doesn't distinguish this from the
        # ordinary review-pass path, which also ends "completed" — pin the
        # "no run occurred" semantics instead: run_outcome mirrors status,
        # and exit_code is None because no `_run_claude` call ever happened.
        assert update.status == "completed"
        assert update.run_outcome == "completed"
        assert update.exit_code is None

    def test_an_open_pr_still_reviews_as_before(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "OPEN"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.claude_invocations == 1

    def test_a_merged_pr_found_via_issue_lookup_still_skips_claude(self, review_env):
        # End-to-end regression for the crash loop this task exists to fix:
        # ctx.pr_url unset (restart / human label / sibling toolchain), no
        # open PR at all, but a merged one is found by issue-number lookup.
        # Before this task: _find_pr_for_issue (open-only) returns None ->
        # RuntimeError("No open PR found") -> _labels_for_review_crash
        # rewinds to ac-dev-review -> the next poll re-queues the same
        # review, forever.
        env = review_env(
            pr_url=None,
            number=268,
            merged_list=[
                {"number": 341, "headRefName": "ac/issue-268-attachments",
                 "url": "https://github.com/Org/repo/pull/341", "body": "", "title": ""},
            ],
        )
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.claude_invocations == 0
        assert env.final_status() == "completed"

    def test_a_newer_open_pr_is_reviewed_instead_of_a_stale_merged_one(self, review_env):
        # run_review_worker-level counterpart to the TestCheckPrAlreadyMerged
        # priority test — proves the full worker entry point, not just the
        # helper, does not short-circuit a still-open PR.
        env = review_env(
            pr_url=None,
            number=268,
            merged_list=[
                {"number": 340, "headRefName": "ac/issue-268-old-attempt",
                 "url": "https://github.com/Org/repo/pull/340", "body": "", "title": ""},
            ],
            open_list=[
                {"number": 341, "headRefName": "ac/issue-268-attachments",
                 "url": "https://github.com/Org/repo/pull/341", "body": "", "title": ""},
            ],
        )
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.claude_invocations == 1
        assert "ac-merged" not in env.labels_added
