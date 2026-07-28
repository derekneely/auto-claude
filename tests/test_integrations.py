"""Tests for integrations.py - wrappers around accelevation-claude-tools scripts.

Both scripts already fail safe on their own terms (log-event.mjs always exits 0
and warns to stderr; project-sync.mjs exits 1 only on hard errors and 0 on
"disabled"/"nothing to do"). What this module has to get right is the *Python*
side of the call: a missing `node`, a hung subprocess, or an unconfigured
toolchain must never raise into a worker. No test here touches node/gh/network -
every subprocess.run is replaced by an injected fake runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations import (  # noqa: E402
    RunResult,
    TelemetryEvent,
    log_event,
    sync_board,
)

TOOLS_ROOT = Path("/fake/accelevation-claude-tools")
REPO_ROOT = Path("/fake/field_admin")


class FakeRunner:
    """Records every call and returns a canned RunResult."""

    def __init__(self, result: RunResult | None = None):
        self.result = result if result is not None else RunResult(0, "", "")
        self.calls: list[dict] = []

    def __call__(self, cmd, *, cwd=None, timeout=None):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
        return self.result


class RaisingRunner:
    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, timeout=None):
        self.calls.append(list(cmd))
        raise self.exc


class TestTelemetryEventArgv:
    def test_full_event_builds_correct_argv(self):
        event = TelemetryEvent(
            project="field_admin",
            issue=42,
            stage="dev",
            action="pr_opened",
            attempt=2,
            duration_seconds=180,
            pr=99,
            github_login="accelevation-bot",
            detail={"foo": "bar"},
        )
        argv = event.to_argv()
        assert argv == [
            "--project", "field_admin",
            "--issue", "42",
            "--stage", "dev",
            "--action", "pr_opened",
            "--actor", "auto-claude",
            "--attempt", "2",
            "--duration", "180",
            "--pr", "99",
            "--github-login", "accelevation-bot",
            "--detail", '{"foo": "bar"}',
        ]

    def test_optional_flags_omitted_when_none(self):
        event = TelemetryEvent(
            project="field_admin", issue=0, stage="dev", action="picked_up",
        )
        argv = event.to_argv()
        assert "--attempt" not in argv
        assert "--duration" not in argv
        assert "--pr" not in argv
        assert "--github-login" not in argv
        assert "--detail" not in argv

    def test_actor_always_auto_claude(self):
        event = TelemetryEvent(project="p", issue=1, stage="dev", action="blocked")
        argv = event.to_argv()
        i = argv.index("--actor")
        assert argv[i + 1] == "auto-claude"

    def test_actor_is_not_overridable_by_caller_mistake(self):
        # actor defaults to "auto-claude" and every call site should rely on
        # that default rather than pass its own - but even if constructed
        # explicitly, the field is what travels to argv.
        event = TelemetryEvent(
            project="p", issue=1, stage="dev", action="blocked", actor="auto-claude",
        )
        assert event.to_argv().count("--actor") == 1

    def test_sentinel_issue_zero_is_passed_through(self):
        event = TelemetryEvent(project="p", issue=0, stage="dev", action="picked_up")
        argv = event.to_argv()
        assert argv[argv.index("--issue") + 1] == "0"


class TestLogEvent:
    def _event(self, **overrides):
        defaults = dict(project="field_admin", issue=42, stage="dev", action="picked_up")
        defaults.update(overrides)
        return TelemetryEvent(**defaults)

    def test_invokes_node_with_script_path_and_argv(self):
        runner = FakeRunner()
        log_event(self._event(), TOOLS_ROOT, run=runner)
        assert len(runner.calls) == 1
        cmd = runner.calls[0]["cmd"]
        assert cmd[0] == "node"
        assert cmd[1] == str(TOOLS_ROOT / "tooling" / "pipeline-metrics" / "scripts" / "log-event.mjs")
        assert "--actor" in cmd and "auto-claude" in cmd

    def test_noop_when_claude_tools_root_is_none(self):
        runner = FakeRunner()
        log_event(self._event(), None, run=runner)
        assert runner.calls == []

    def test_nonzero_exit_does_not_raise(self):
        runner = FakeRunner(RunResult(1, "", "warn: pipeline metrics write failed: no DB"))
        log_event(self._event(), TOOLS_ROOT, run=runner)  # must not raise

    def test_missing_node_does_not_raise(self):
        runner = RaisingRunner(FileNotFoundError("node not found"))
        log_event(self._event(), TOOLS_ROOT, run=runner)  # must not raise
        assert len(runner.calls) == 1

    def test_timeout_does_not_raise(self):
        runner = RaisingRunner(subprocess.TimeoutExpired(cmd=["node"], timeout=5))
        log_event(self._event(), TOOLS_ROOT, run=runner)  # must not raise

    def test_unexpected_exception_does_not_raise(self):
        runner = RaisingRunner(RuntimeError("something exploded"))
        log_event(self._event(), TOOLS_ROOT, run=runner)  # must not raise

    def test_failure_is_reported_through_log_callback(self):
        messages = []
        runner = RaisingRunner(FileNotFoundError("node not found"))
        log_event(self._event(), TOOLS_ROOT, run=runner, log=messages.append)
        assert messages
        assert "node" in messages[0].lower()

    def test_a_hard_timeout_is_passed_to_the_runner(self):
        runner = FakeRunner()
        log_event(self._event(), TOOLS_ROOT, run=runner, timeout=7)
        assert runner.calls[0]["timeout"] == 7

    def test_success_reports_nothing(self):
        messages = []
        runner = FakeRunner(RunResult(0, "", ""))
        log_event(self._event(), TOOLS_ROOT, run=runner, log=messages.append)
        assert messages == []


class TestSyncBoard:
    def test_runs_with_cwd_set_to_the_consuming_repo(self):
        runner = FakeRunner()
        sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)
        assert len(runner.calls) == 1
        assert runner.calls[0]["cwd"] == REPO_ROOT

    def test_invokes_node_with_the_project_sync_script(self):
        runner = FakeRunner()
        sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)
        cmd = runner.calls[0]["cmd"]
        assert cmd[0] == "node"
        assert cmd[1] == str(TOOLS_ROOT / "commands" / "scripts" / "project-sync.mjs")

    def test_passes_explicit_assignee_instead_of_default_at_me(self):
        runner = FakeRunner()
        sync_board(REPO_ROOT, TOOLS_ROOT, assignee="accelevation-bot", run=runner)
        cmd = runner.calls[0]["cmd"]
        i = cmd.index("--assignee")
        assert cmd[i + 1] == "accelevation-bot"

    def test_omits_optional_flags_when_not_given(self):
        runner = FakeRunner()
        sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)
        cmd = runner.calls[0]["cmd"]
        assert "--repo" not in cmd
        assert "--issue" not in cmd
        assert "--assignee" not in cmd
        assert "--dry-run" not in cmd

    def test_includes_repo_issue_and_dry_run_when_given(self):
        runner = FakeRunner()
        sync_board(
            REPO_ROOT, TOOLS_ROOT,
            repo="Accelevation/field_admin", issue=42, dry_run=True, run=runner,
        )
        cmd = runner.calls[0]["cmd"]
        assert cmd[cmd.index("--repo") + 1] == "Accelevation/field_admin"
        assert cmd[cmd.index("--issue") + 1] == "42"
        assert "--dry-run" in cmd

    def test_noop_when_claude_tools_root_is_none(self):
        runner = FakeRunner()
        result = sync_board(REPO_ROOT, None, run=runner)
        assert runner.calls == []
        assert result is None

    def test_exit_1_does_not_raise_and_is_reported_as_a_warning_not_ok(self):
        runner = FakeRunner(RunResult(1, "", "error: missing gh"))
        result = sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)  # must not raise
        assert result is not None
        assert result.ok is False

    def test_exit_0_is_ok(self):
        runner = FakeRunner(RunResult(0, "", ""))
        result = sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)
        assert result.ok is True

    def test_missing_gh_does_not_raise(self):
        runner = RaisingRunner(FileNotFoundError("gh not found"))
        result = sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)  # must not raise
        assert result is not None and result.ok is False

    def test_timeout_does_not_raise(self):
        runner = RaisingRunner(subprocess.TimeoutExpired(cmd=["node"], timeout=5))
        result = sync_board(REPO_ROOT, TOOLS_ROOT, run=runner)  # must not raise
        assert result is not None and result.ok is False

    def test_failure_is_reported_through_log_callback(self):
        messages = []
        runner = FakeRunner(RunResult(1, "", "error: missing gh scope"))
        sync_board(REPO_ROOT, TOOLS_ROOT, run=runner, log=messages.append)
        assert messages


class TestDefaultRunnerEncoding:
    """The default runner must go through ghauth.build_env like every other
    subprocess call in this repo (see tests/test_subprocess_encoding.py)."""

    def test_default_runner_is_used_when_none_injected(self, monkeypatch):
        captured = {}

        class FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            captured["cmd"] = cmd
            return FakeCompleted()

        monkeypatch.setattr("subprocess.run", fake_run)
        log_event(
            TelemetryEvent(project="p", issue=1, stage="dev", action="picked_up"),
            TOOLS_ROOT,
        )
        assert captured.get("encoding") == "utf-8"
        assert captured.get("errors") == "replace"
        assert captured.get("env") is not None
