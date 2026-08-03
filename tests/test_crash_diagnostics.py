"""A failed run has to leave behind enough to diagnose it, and the work.

The live run on `field_admin#268` exited 1 after 17 minutes of Opus with an
empty stderr, and produced: a 434-byte crash log holding only a Python
traceback, no CLI transcript (`--no-session-persistence`), a branch with zero
commits, and an empty worktree — the `except` block force-removed it. The cause
was undiagnosable and the work was gone.

Three things are covered here:
  * the stream-json `result` event's `subtype` is read, so `error_max_turns`
    stops looking like a generic crash;
  * the crash log carries the transcript;
  * uncommitted work is committed to the branch before the worktree dies.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402


def _result_line(**over) -> str:
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 3.5,
        "num_turns": 42,
        "duration_ms": 900_000,
        "result": "done",
    }
    data.update(over)
    return json.dumps(data)


# ---------------------------------------------------------------------------
# The result event says why the run stopped
# ---------------------------------------------------------------------------

class TestRunMetricsCapturesTheReason:
    def test_reads_subtype_and_is_error(self):
        m = worker._parse_run_metrics(
            _result_line(subtype="error_max_turns", is_error=True)
        )
        assert m.subtype == "error_max_turns"
        assert m.is_error is True

    def test_success_is_not_an_error(self):
        m = worker._parse_run_metrics(_result_line())
        assert m.subtype == "success"
        assert m.is_error is False
        assert m.turns == 42

    def test_no_result_event_leaves_them_unknown(self):
        """A run killed mid-stream stores NULL, not a fabricated verdict."""
        m = worker._parse_run_metrics('{"type":"assistant","message":{}}')
        assert m.subtype is None
        assert m.is_error is None

    def test_last_result_event_wins(self):
        out = "\n".join([
            _result_line(subtype="success"),
            _result_line(subtype="error_during_execution", is_error=True),
        ])
        assert worker._parse_run_metrics(out).subtype == "error_during_execution"


class TestTurnLimitIsExhaustionNotACrash:
    """`error_max_turns` exits 1 with empty stderr — identical to a real crash.

    It has to reach the same graceful path budget exhaustion does (handoff
    summary, partial work pushed) instead of raising and discarding the run.
    """

    def test_turn_limit_counts_as_exhaustion(self):
        m = worker._parse_run_metrics(_result_line(subtype="error_max_turns", is_error=True))
        assert worker._is_exhaustion(budget_exceeded=False, metrics=m) is True

    def test_budget_still_counts(self):
        m = worker._parse_run_metrics(_result_line())
        assert worker._is_exhaustion(budget_exceeded=True, metrics=m) is True

    def test_a_real_error_is_not_exhaustion(self):
        m = worker._parse_run_metrics(
            _result_line(subtype="error_during_execution", is_error=True)
        )
        assert worker._is_exhaustion(budget_exceeded=False, metrics=m) is False

    def test_a_clean_run_is_not_exhaustion(self):
        m = worker._parse_run_metrics(_result_line())
        assert worker._is_exhaustion(budget_exceeded=False, metrics=m) is False

    def test_unknown_reason_is_not_exhaustion(self):
        """No result event means no evidence — do not invent a graceful exit."""
        assert worker._is_exhaustion(budget_exceeded=False, metrics=worker.RunMetrics()) is False


def test_exit_reason_names_the_subtype():
    """`Claude exited with code 1` alone sent us to the transcript we did not have."""
    m = worker._parse_run_metrics(
        _result_line(subtype="error_during_execution", is_error=True)
    )
    assert "error_during_execution" in worker._exit_reason(1, m)
    assert "1" in worker._exit_reason(1, m)


def test_exit_reason_survives_an_unknown_subtype():
    assert "unknown" in worker._exit_reason(1, worker.RunMetrics()).lower()


# ---------------------------------------------------------------------------
# The crash log carries the transcript
# ---------------------------------------------------------------------------

def _ctx(tmp_path: Path):
    return SimpleNamespace(
        issue_id="field_admin#268",
        org="Accelevation",
        repo="field_admin",
        number=268,
        title="Job File Attachments",
        action="implement",
        base_branch="dev",
        dev_model="claude-opus-5",
        crash_logs_dir=tmp_path / "crash_logs",
        repos_dir=tmp_path / "repos",
    )


def _logger():
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


class TestCrashLogHoldsTheTranscript:
    def test_transcript_is_written(self, tmp_path):
        path = worker._write_crash_log(
            _ctx(tmp_path), "RuntimeError: boom", _logger(),
            transcript='{"type":"assistant","message":"the agent said this"}',
        )
        assert path is not None
        body = path.read_text(encoding="utf-8")
        assert "the agent said this" in body
        assert "RuntimeError: boom" in body

    def test_no_transcript_still_writes_a_log(self, tmp_path):
        path = worker._write_crash_log(_ctx(tmp_path), "RuntimeError: boom", _logger())
        assert path is not None
        assert "RuntimeError: boom" in path.read_text(encoding="utf-8")

    def test_transcript_is_redacted(self, tmp_path):
        """A transcript is agent-authored text and can hold anything."""
        secret = "ghp_" + "a" * 36
        path = worker._write_crash_log(
            _ctx(tmp_path), "boom", _logger(), transcript=f"token={secret}",
        )
        assert secret not in path.read_text(encoding="utf-8")

    def test_a_huge_transcript_is_capped(self, tmp_path):
        path = worker._write_crash_log(
            _ctx(tmp_path), "boom", _logger(), transcript="x" * 5_000_000,
        )
        assert path.stat().st_size < 1_000_000

    def test_the_tail_is_kept_not_the_head(self, tmp_path):
        """The failure is at the end of the stream, not the beginning."""
        path = worker._write_crash_log(
            _ctx(tmp_path), "boom", _logger(),
            transcript=("x" * 5_000_000) + "THE_LAST_THING_THAT_HAPPENED",
        )
        assert "THE_LAST_THING_THAT_HAPPENED" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Uncommitted work survives the crash
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "dev")
    _git(r, "config", "user.name", "accelevation-bot")
    _git(r, "config", "user.email", "bot@example.com")
    (r / "README.md").write_text("base\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "chore: base")
    _git(r, "switch", "-c", "ac/issue-268-x")
    return r


class TestPreserveUncommittedWork:
    def test_dirty_tree_is_committed(self, worktree, tmp_path):
        (worktree / "feature.ts").write_text("export const x = 1\n", encoding="utf-8")

        sha = worker._preserve_uncommitted_work(_ctx(tmp_path), worktree, _logger())

        assert sha
        assert _git(worktree, "status", "--porcelain") == ""
        assert "feature.ts" in _git(worktree, "show", "--name-only", "--format=", "HEAD")

    def test_the_commit_says_what_it_is(self, worktree, tmp_path):
        (worktree / "feature.ts").write_text("x\n", encoding="utf-8")
        worker._preserve_uncommitted_work(_ctx(tmp_path), worktree, _logger())
        message = _git(worktree, "log", "-1", "--format=%B")
        assert "268" in message
        assert "Co-Authored-By" not in message

    def test_untracked_files_are_kept_too(self, worktree, tmp_path):
        """The agent's new files are the whole point — 17 minutes of them."""
        (worktree / "brand-new.ts").write_text("x\n", encoding="utf-8")
        worker._preserve_uncommitted_work(_ctx(tmp_path), worktree, _logger())
        assert "brand-new.ts" in _git(worktree, "show", "--name-only", "--format=", "HEAD")

    def test_a_clean_tree_makes_no_commit(self, worktree, tmp_path):
        head = _git(worktree, "rev-parse", "HEAD")
        assert worker._preserve_uncommitted_work(_ctx(tmp_path), worktree, _logger()) is None
        assert _git(worktree, "rev-parse", "HEAD") == head

    def test_a_missing_worktree_is_not_fatal(self, tmp_path):
        """The crash may be the worktree itself. This runs on the crash path."""
        assert worker._preserve_uncommitted_work(
            _ctx(tmp_path), tmp_path / "gone", _logger(),
        ) is None
