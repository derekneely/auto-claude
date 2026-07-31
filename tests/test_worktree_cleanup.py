"""Post-PR worktree cleanup must never fail a run that already succeeded.

Regression: field_admin#215, 2026-07-31. The dev worker finished, pushed, and
opened PR #334, then `git worktree remove --force` exceeded `_run_cmd`'s 120s
timeout on a worktree holding a freshly-installed `node_modules`. The
`TimeoutExpired` propagated out of step [7] into `run_dev_worker`'s
`except Exception`, which posted a crash comment and rolled the issue back from
ac-dev-review to ac-dev-ready + ac-attempt-1 — re-queueing a completed $6.72 /
55-turn run for a full re-implementation on top of the live PR.

Cleanup runs after the PR exists. The work is done and irreversible by then, so
a janitorial rmdir must not be inside the blast radius. A leftover worktree is
harmless: `_cleanup_worktree` removes a stale one at the start of the next run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402


class FakeLogger:
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


class TestCleanupNeverRaises:
    def test_timeout_is_swallowed(self, tmp_path, monkeypatch):
        """The exact failure from #215: remove exceeds its timeout."""
        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

        monkeypatch.setattr(worker, "_run_cmd", boom)
        logger = FakeLogger()

        # Must not raise.
        worker._cleanup_worktree_best_effort(
            tmp_path, tmp_path / "issue-215", logger
        )

    def test_timeout_is_reported_not_silent(self, tmp_path, monkeypatch):
        """Swallowing is not the same as hiding — the operator needs to know a
        worktree was left behind, or disk fills up silently."""
        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

        monkeypatch.setattr(worker, "_run_cmd", boom)
        logger = FakeLogger()

        worker._cleanup_worktree_best_effort(
            tmp_path, tmp_path / "issue-215", logger
        )

        assert any(lvl in ("warn", "error") for lvl, _ in logger.messages), (
            "cleanup failure must be logged"
        )

    def test_arbitrary_exception_is_swallowed_too(self, tmp_path, monkeypatch):
        """Windows throws more than TimeoutExpired at a locked directory."""
        def boom(cmd, **kwargs):
            raise PermissionError("The process cannot access the file")

        monkeypatch.setattr(worker, "_run_cmd", boom)

        worker._cleanup_worktree_best_effort(
            tmp_path, tmp_path / "issue-215", FakeLogger()
        )


class TestCleanupStillCleansUp:
    def test_happy_path_removes_and_prunes(self, tmp_path, monkeypatch):
        """Swallowing failures must not turn cleanup into a no-op."""
        calls: list[list[str]] = []

        def record(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(worker, "_run_cmd", record)

        worker._cleanup_worktree_best_effort(
            tmp_path, tmp_path / "issue-215", FakeLogger()
        )

        joined = [" ".join(c) for c in calls]
        assert any("worktree remove" in c for c in joined), joined
        assert any("worktree prune" in c for c in joined), joined

    def test_prune_still_runs_when_remove_fails(self, tmp_path, monkeypatch):
        """`prune` is what unregisters a worktree whose directory is already
        gone — exactly the #215 state, where the delete completed but the
        command was killed before it exited. Losing prune leaves a phantom
        registration that breaks the next `worktree add`."""
        calls: list[list[str]] = []

        def flaky(cmd, **kwargs):
            calls.append(cmd)
            if "remove" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(worker, "_run_cmd", flaky)

        worker._cleanup_worktree_best_effort(
            tmp_path, tmp_path / "issue-215", FakeLogger()
        )

        joined = [" ".join(c) for c in calls]
        assert any("worktree prune" in c for c in joined), (
            f"prune must still run after remove fails, got: {joined}"
        )


# ---------------------------------------------------------------------------
# Wiring — the helper is worthless if a new cleanup site inlines a raising one
# ---------------------------------------------------------------------------

def test_every_post_work_cleanup_goes_through_the_helper():
    """Meta-test, in the style of test_push_guard's.

    Four sites clean up *after* work that is already committed to the outside
    world, so all four must be best-effort:

      - dev worker  [7]  — PR is open
      - dev worker  [3a] — rate limited, partial work pushed, attempt not billed
      - dev worker  [3b] — budget exhausted, handoff summary produced
      - review worker [5] — verdict decided, not yet posted

    Exactly five raw `git worktree remove` sites may remain, and none of them
    is a post-work cleanup:

      1. `_cleanup_worktree`               — setup; a stale tree that will not
                                             die must fail loudly, before any
                                             model time is spent
      2. merge-conflict fallback           — the removal *is* the operation;
                                             failing it must abort the fallback
      3. inside `_cleanup_worktree_best_effort` itself
      4. dev worker's `except` handler     — already wrapped in try/except
      5. review worker's `except` handler  — already wrapped in try/except
    """
    source = (
        Path(__file__).resolve().parent.parent / "worker.py"
    ).read_text(encoding="utf-8")

    raw_removes = source.count('"git", "worktree", "remove"')
    helper_refs = source.count("_cleanup_worktree_best_effort(")

    assert raw_removes <= 5, (
        f"{raw_removes} raw `git worktree remove` sites (expected <= 5) — a new "
        f"cleanup path is inlined instead of using _cleanup_worktree_best_effort, "
        f"which is how field_admin#215 threw away a finished run"
    )
    # 1 definition + 4 call sites.
    assert helper_refs >= 5, (
        f"only {helper_refs} references to _cleanup_worktree_best_effort "
        f"(expected >= 5: 1 def + 4 post-work call sites) — a post-work cleanup "
        f"site stopped being best-effort"
    )
