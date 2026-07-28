"""Tests for the worker's push guard.

auto-claude must never push to a shared branch. Server-side branch protection
is unavailable (the org is on a free plan with private repos, so rulesets 403)
and `accelevation-bot` holds `write`, so this guard is the only thing standing
between a bad branch value and a rewritten `dev`.

The branch name is not always computed — `_setup_rework_worktree` reads it from
`state/issues.json` and `run_review_worker` reads it from a PR's `headRefName`.
Both are outside auto-claude's control, which is what the guard exists for.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from worker import ProtectedBranchError, assert_pushable  # noqa: E402


class TestRefusesSharedBranches:
    def test_refuses_main(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("main", base_branch="dev")

    def test_refuses_master(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("master", base_branch="dev")

    def test_refuses_dev(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("dev", base_branch="main")

    def test_refuses_the_configured_base_branch(self):
        # derekdev is not in the built-in list, but it is this repo's PR base.
        with pytest.raises(ProtectedBranchError):
            assert_pushable("derekdev", base_branch="derekdev")

    def test_error_names_the_branch(self):
        with pytest.raises(ProtectedBranchError, match="dev"):
            assert_pushable("dev", base_branch="main")


class TestNormalisation:
    """A guard that only matches the exact literal is trivially bypassed."""

    def test_case_insensitive(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("MAIN", base_branch="dev")

    def test_strips_refs_heads_prefix(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("refs/heads/main", base_branch="dev")

    def test_strips_surrounding_whitespace(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("  dev\n", base_branch="main")

    def test_base_branch_comparison_is_also_normalised(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("refs/heads/DerekDev", base_branch="derekdev")


class TestRefusesUnusableValues:
    """An empty branch makes `git push origin ''` fall back to the push default,
    which on a worktree checked out at the base branch pushes the base branch."""

    def test_refuses_empty(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("", base_branch="dev")

    def test_refuses_whitespace_only(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable("   ", base_branch="dev")

    def test_refuses_none(self):
        with pytest.raises(ProtectedBranchError):
            assert_pushable(None, base_branch="dev")

    def test_refuses_hard_head(self):
        # `git push origin HEAD` on a base-branch checkout pushes the base branch.
        with pytest.raises(ProtectedBranchError):
            assert_pushable("HEAD", base_branch="dev")


class TestAllowsWorkBranches:
    def test_allows_the_standard_issue_branch(self):
        assert_pushable("ac/issue-215-job-wizard-progress", base_branch="dev")

    def test_allows_a_versioned_rework_branch(self):
        assert_pushable("ac/issue-215-job-wizard-progress-v2", base_branch="dev")

    def test_allows_a_sibling_toolchain_branch(self):
        assert_pushable("issue-215", base_branch="dev")

    def test_allows_a_branch_that_merely_contains_a_protected_name(self):
        # Substring matching would wrongly refuse this.
        assert_pushable("ac/issue-9-fix-dev-server-crash", base_branch="dev")

    def test_allows_a_branch_prefixed_with_the_base_name(self):
        assert_pushable("dev-tools-refactor", base_branch="dev")

    def test_returns_none_on_success(self):
        assert assert_pushable("ac/issue-1-x", base_branch="dev") is None


# ---------------------------------------------------------------------------
# Wiring — the guard is worthless unless every push path calls it
# ---------------------------------------------------------------------------

class _FakeResult:
    """Stands in for CompletedProcess. Reports a dirty tree so the commit path
    runs, and success on everything, so nothing but the guard can stop a push."""
    returncode = 0
    stdout = " M src/foo.ts\n"
    stderr = ""


def _record_cmds(monkeypatch):
    """Replace worker._run_cmd with a recorder. Returns the command list."""
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
    return calls


def _ctx(tmp_path):
    """A real IssueContext — the fake fields drifted out of sync too easily."""
    return worker.IssueContext(
        issue_id="field_admin#215",
        repo="field_admin",
        number=215,
        title="Job wizard progress",
        body="",
        action="implement",
        org="Accelevation",
        base_branch="dev",
        repos_dir=tmp_path,
        worktrees_dir=tmp_path,
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


def _pushes(calls):
    return [c for c in calls if c[:2] == ["git", "push"]]


class TestPushAndPrIsGuarded:
    def test_refuses_to_push_the_base_branch(self, monkeypatch, tmp_path):
        calls = _record_cmds(monkeypatch)
        with pytest.raises(ProtectedBranchError):
            worker._push_and_pr(_ctx(tmp_path), "dev", tmp_path, "summary", _logger())
        assert _pushes(calls) == [], "guard must fire before any git push"

    def test_still_pushes_a_legitimate_branch(self, monkeypatch, tmp_path):
        calls = _record_cmds(monkeypatch)
        worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert _pushes(calls) == [["git", "push", "-u", "origin", "ac/issue-215-x"]]


class TestPushReworkIsGuarded:
    def test_refuses_to_push_main(self, monkeypatch, tmp_path):
        calls = _record_cmds(monkeypatch)
        with pytest.raises(ProtectedBranchError):
            worker._push_rework(_ctx(tmp_path), "main", tmp_path, "summary", _logger())
        assert _pushes(calls) == [], "guard must fire before any git push"

    def test_still_pushes_a_legitimate_branch(self, monkeypatch, tmp_path):
        calls = _record_cmds(monkeypatch)
        worker._push_rework(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert _pushes(calls) == [["git", "push", "origin", "ac/issue-215-x"]]


def test_no_unguarded_git_push_remains_in_worker():
    """Meta-test: every `git push` in worker.py must be reachable only through
    a function that calls assert_pushable. New push paths fail this until wired."""
    source = (Path(__file__).resolve().parent.parent / "worker.py").read_text(encoding="utf-8")
    push_sites = source.count('"git", "push"')
    guard_calls = source.count("assert_pushable(")
    # -1 for the definition itself.
    assert guard_calls - 1 >= push_sites, (
        f"{push_sites} `git push` site(s) but only {guard_calls - 1} assert_pushable "
        f"call(s) — a push path is unguarded"
    )
