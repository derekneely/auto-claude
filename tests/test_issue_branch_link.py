"""The issue has to show the branch and the PR.

`Closes #N` in a pull-request body is inert for auto-claude: GitHub only
records a closing reference when the PR targets the repository's *default*
branch, and every auto-claude PR targets `base_branch` (`dev`). Verified
against the real PR #334 — body says `Closes #215`, `closingIssuesReferences`
is empty — so the issue's Development panel showed neither the branch nor the
PR, and there was no way to tell from the issue whether the work had merged.

`createLinkedBranch` is the linkage that does not depend on the base branch.
It creates the ref, so it must run *before* the push.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402


BRANCH = "ac/issue-215-x"
MERGE_BASE = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def _ctx(tmp_path: Path):
    return SimpleNamespace(
        issue_id="field_admin#215",
        org="Accelevation",
        repo="field_admin",
        number=215,
        title="Job wizard progress",
        action="feat",
        base_branch="dev",
        pr_url=None,
        existing_branch=None,
        worktree_dir=tmp_path,
        repo_dir=tmp_path,
        prompts_dir=tmp_path,
        dev_model="opus",
        light_model="sonnet",
        permission_mode="acceptEdits",
        max_budget_usd=10.0,
        max_turns=100,
        crash_logs_dir=tmp_path,
        color_name="blue",
        color_code="\033[34m",
    )


def _logger():
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


def _fake_cmds(monkeypatch, *, stdout_for=None, fail_on=None):
    """Record every `_run_cmd` invocation and answer the ones that are read."""
    calls: list[list[str]] = []

    def fake(cmd, cwd=None, logger=None, timeout=120, env_extra=None, stdin_text=None):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if fail_on and fail_on in joined:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        stdout = ""
        for needle, value in (stdout_for or {}).items():
            if needle in joined:
                stdout = value
                break
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(worker, "_run_cmd", fake)
    monkeypatch.setattr(worker, "_assert_lease_held", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_success_labels", lambda *_a, **_k: None)
    return calls


DEFAULT_STDOUT = {
    "issue view": "I_kwDOissue215",
    "merge-base": MERGE_BASE,
}


def _graphql(calls):
    return [c for c in calls if c[:3] == ["gh", "api", "graphql"]]


class TestLinkBranchToIssue:
    def test_creates_a_linked_branch_at_the_merge_base(self, monkeypatch, tmp_path):
        calls = _fake_cmds(monkeypatch, stdout_for=DEFAULT_STDOUT)

        assert worker._link_branch_to_issue(_ctx(tmp_path), BRANCH, tmp_path, _logger())

        mutation = _graphql(calls)
        assert len(mutation) == 1
        flat = " ".join(mutation[0])
        assert "createLinkedBranch" in flat
        assert "issueId=I_kwDOissue215" in flat
        assert f"oid={MERGE_BASE}" in flat, "must link at the fork point, not dev's tip"
        assert f"name={BRANCH}" in flat

    def test_links_at_an_ancestor_so_the_push_stays_a_fast_forward(
        self, monkeypatch, tmp_path
    ):
        calls = _fake_cmds(monkeypatch, stdout_for=DEFAULT_STDOUT)
        worker._link_branch_to_issue(_ctx(tmp_path), BRANCH, tmp_path, _logger())

        # `git merge-base HEAD origin/dev` — not `rev-parse origin/dev`, which
        # would create the remote branch ahead of what we are about to push.
        assert ["git", "merge-base", "HEAD", "origin/dev"] in calls

    def test_a_failed_mutation_does_not_raise(self, monkeypatch, tmp_path):
        _fake_cmds(monkeypatch, stdout_for=DEFAULT_STDOUT, fail_on="createLinkedBranch")
        assert worker._link_branch_to_issue(_ctx(tmp_path), BRANCH, tmp_path, _logger()) is False

    def test_no_issue_id_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        calls = _fake_cmds(monkeypatch, stdout_for={"merge-base": MERGE_BASE})
        assert worker._link_branch_to_issue(_ctx(tmp_path), BRANCH, tmp_path, _logger()) is False
        assert _graphql(calls) == []

    def test_an_exception_does_not_raise(self, monkeypatch, tmp_path):
        def boom(*_a, **_k):
            raise RuntimeError("network gone")

        monkeypatch.setattr(worker, "_run_cmd", boom)
        assert worker._link_branch_to_issue(_ctx(tmp_path), BRANCH, tmp_path, _logger()) is False


class TestWiring:
    """`createLinkedBranch` creates the ref, so ordering is the whole point."""

    def test_push_and_pr_links_before_it_pushes(self, monkeypatch, tmp_path):
        calls = _fake_cmds(monkeypatch, stdout_for=DEFAULT_STDOUT)
        worker._push_and_pr(_ctx(tmp_path), BRANCH, tmp_path, "summary", _logger())

        flat = [" ".join(c) for c in calls]
        link = next(i for i, c in enumerate(flat) if "createLinkedBranch" in c)
        push = next(i for i, c in enumerate(flat) if c.startswith("git push"))
        assert link < push

    def test_partial_push_links_too(self, monkeypatch, tmp_path):
        """A budget-exceeded run still opens a WIP PR a human has to find."""
        calls = _fake_cmds(
            monkeypatch,
            stdout_for={**DEFAULT_STDOUT, "status --porcelain": "", "log ": "abc123"},
        )
        worker._push_partial_work(_ctx(tmp_path), BRANCH, tmp_path, _logger())
        assert _graphql(calls), "partial work reaches a public branch too"


class TestTrailerScrubRunsBeforePush:
    def test_push_and_pr_scrubs_before_pushing(self, monkeypatch, tmp_path):
        seen: list[str] = []
        monkeypatch.setattr(
            worker, "_scrub_ai_trailers",
            lambda *_a, **_k: seen.append("scrub") or 0,
        )
        calls = _fake_cmds(monkeypatch, stdout_for=DEFAULT_STDOUT)
        worker._push_and_pr(_ctx(tmp_path), BRANCH, tmp_path, "summary", _logger())

        assert seen == ["scrub"]
        assert [c for c in calls if c[:2] == ["git", "push"]], "still pushes"

    def test_push_rework_scrubs_too(self, monkeypatch, tmp_path):
        seen: list[str] = []
        monkeypatch.setattr(
            worker, "_scrub_ai_trailers",
            lambda *_a, **_k: seen.append("scrub") or 0,
        )
        _fake_cmds(monkeypatch, stdout_for=DEFAULT_STDOUT)
        worker._push_rework(_ctx(tmp_path), BRANCH, tmp_path, "summary", _logger())
        assert seen == ["scrub"]
