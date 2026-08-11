"""Wrappers around the two accelevation-claude-tools scripts auto-claude calls
so it reports into the same pipeline-metrics DB and the same Projects v2 board
as the human `/loop` runners.

Both scripts already fail safe on their own terms:

- `tooling/pipeline-metrics/scripts/log-event.mjs` always exits 0, printing
  `warn: pipeline metrics write failed: <reason>` to stderr and swallowing
  every error (unreachable DB, missing env var, bad args, missing psql).
- `commands/scripts/project-sync.mjs` exits 0 on success, on "board sync
  disabled" (no `projectBoard` in pipeline.json), and on nothing-to-do; it
  exits 1 only on hard errors (missing `gh`, missing/invalid pipeline.json,
  missing `project` gh scope).

What is NOT safe by default is the Python side of invoking them: `node` may not
be installed, the process may hang, or the toolchain may not be configured at
all. This module's whole job is to make sure none of that ever raises into a
worker - telemetry and board sync are both best-effort side effects, never
load-bearing for whether an issue gets worked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ghauth import build_env, current_token

DEFAULT_TIMEOUT_SECONDS = 30

# project-sync.mjs is not a single request like log-event.mjs: it walks every
# assigned issue in a repo and spends a `gh api graphql` round trip per issue to
# move its board card. On a board with real work on it that alone blows past 30s,
# and the timeout is worse than the delay it prevents - a killed `node` leaves
# its `gh` grandchildren alive on Windows, still holding the scratch cwd open
# (WinError 32 on cleanup) and still mutating the board unsupervised.
BOARD_SYNC_TIMEOUT_SECONDS = 300

# Paths are relative to the claude-tools checkout root (the `claude_tools_root`
# config key), not to auto-claude's own root.
_LOG_EVENT_SCRIPT = Path("tooling") / "pipeline-metrics" / "scripts" / "log-event.mjs"
_PROJECT_SYNC_SCRIPT = Path("commands") / "scripts" / "project-sync.mjs"


@dataclass(frozen=True)
class RunResult:
    """Runner-agnostic stand-in for subprocess.CompletedProcess.

    Kept separate from subprocess.CompletedProcess so tests can construct a
    fake without going through subprocess at all.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""


# (cmd, *, cwd, timeout) -> RunResult. Injected everywhere so tests never touch
# node/gh/the network - see tests/test_integrations.py.
Runner = Callable[..., RunResult]


METRICS_DB_ENV_VAR = "PIPELINE_METRICS_DATABASE_URL"


def metrics_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Add the metrics DB URL to a subprocess environment, if we hold one.

    `log-event.mjs` reads `PIPELINE_METRICS_DATABASE_URL` from its environment,
    falling back to a `.env` inside the sibling toolchain. auto-claude supplies
    it directly so the toolchain needs no per-checkout setup - one credential,
    configured in one place, and the sibling repo stays untouched.
    """
    env = dict(os.environ if base is None else base)
    url = (os.environ.get(METRICS_DB_ENV_VAR) or "").strip()
    if url:
        env[METRICS_DB_ENV_VAR] = url
    return env


def _subprocess_runner(cmd: Sequence[str], *, cwd: Path | None = None, timeout: int) -> RunResult:
    """Default runner. Every subprocess.run in this repo must set encoding,
    errors and env like this - see tests/test_subprocess_encoding.py."""
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=metrics_env(build_env(current_token())),
    )
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    """Report a soft failure. Defaults to stderr so it's never silently lost
    even if a call site forgets to wire a logger."""
    if log is not None:
        log(message)
    else:
        print(message, file=sys.stderr)


def _invoke(
    cmd: list[str],
    *,
    cwd: Path | None,
    timeout: int,
    run: Runner,
    log: Callable[[str], None] | None,
    label: str,
) -> RunResult | None:
    """Run `cmd` and swallow every way it can fail to even produce a result.

    Returns None (never raises) on FileNotFoundError (no `node`/`gh`),
    TimeoutExpired, or any other unexpected exception. A RunResult - including
    one with a non-zero returncode - is only returned when the subprocess
    actually ran to completion.
    """
    try:
        return run(cmd, cwd=cwd, timeout=timeout)
    except FileNotFoundError as exc:
        _warn(log, f"{label}: command not found ({exc})")
    except subprocess.TimeoutExpired:
        _warn(log, f"{label}: timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001 - this call must never crash a worker
        _warn(log, f"{label}: unexpected error invoking subprocess: {exc}")
    return None


@dataclass(frozen=True)
class TelemetryEvent:
    """One row for `log-event.mjs`. `actor` defaults to `auto-claude` so both
    runners stay distinguishable in the shared pipeline-metrics table - call
    sites should not need to set it."""

    project: str
    issue: int
    stage: str
    action: str
    actor: str = "auto-claude"
    attempt: int | None = None
    duration_seconds: int | None = None
    pr: int | None = None
    github_login: str | None = None
    detail: dict | None = None

    def to_argv(self) -> list[str]:
        argv = [
            "--project", self.project,
            "--issue", str(self.issue),
            "--stage", self.stage,
            "--action", self.action,
            "--actor", self.actor,
        ]
        if self.attempt is not None:
            argv += ["--attempt", str(self.attempt)]
        if self.duration_seconds is not None:
            argv += ["--duration", str(self.duration_seconds)]
        if self.pr is not None:
            argv += ["--pr", str(self.pr)]
        if self.github_login is not None:
            argv += ["--github-login", self.github_login]
        if self.detail is not None:
            argv += ["--detail", json.dumps(self.detail)]
        return argv


def log_event(
    event: TelemetryEvent,
    claude_tools_root: Path | None,
    *,
    run: Runner = _subprocess_runner,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    log: Callable[[str], None] | None = None,
) -> None:
    """Fire-and-forget a pipeline-metrics event.

    No-ops when `claude_tools_root` is None (toolchain not configured). Never
    raises - log-event.mjs itself always exits 0, but a missing `node`, a hang,
    or any other Python-side failure must be at least as safe.
    """
    if claude_tools_root is None:
        return

    script = Path(claude_tools_root) / _LOG_EVENT_SCRIPT
    cmd = ["node", str(script), *event.to_argv()]

    result = _invoke(
        cmd, cwd=None, timeout=timeout, run=run, log=log, label="pipeline metrics",
    )
    if result is not None and result.returncode != 0:
        # Documented to always exit 0; non-zero means the invocation itself is
        # broken (bad args, etc.), not an ordinary write failure.
        _warn(
            log,
            f"pipeline metrics: log-event.mjs exited {result.returncode}: "
            f"{result.stderr.strip()}",
        )


@dataclass(frozen=True)
class BoardSyncResult:
    """Outcome of one `project-sync.mjs` invocation.

    `ok` is False for exit code 1 (hard error) and for any Python-side failure
    to even run the script - both are board drift, not worker failures, and
    callers should log and continue rather than propagate.
    """

    ok: bool
    returncode: int | None
    stdout: str = ""
    stderr: str = ""


def sync_board(
    cwd: Path,
    claude_tools_root: Path | None,
    *,
    repo: str | None = None,
    issue: int | None = None,
    assignee: str | None = None,
    dry_run: bool = False,
    run: Runner = _subprocess_runner,
    timeout: int = BOARD_SYNC_TIMEOUT_SECONDS,
    log: Callable[[str], None] | None = None,
) -> BoardSyncResult | None:
    """Sync the Projects v2 board after a stage transition.

    `cwd` is required (not optional/defaulted) on purpose: project-sync.mjs
    reads the literal relative path `.claude/pipeline.json`, so it must run
    with cwd set to the *consuming* repo's checkout root, not auto-claude's
    own root. Getting this wrong is the main failure mode.

    Since auto-claude authenticates as a bot account, pass `assignee` explicitly
    (e.g. the configured bot login) rather than relying on the script's `@me`
    default, which resolves to whichever `gh` identity is active.

    Returns None when `claude_tools_root` is not configured (no-op). Exit code
    1 is a warning (`ok=False`), never an exception - board drift is
    recoverable, a dead worker is not.
    """
    if claude_tools_root is None:
        return None

    script = Path(claude_tools_root) / _PROJECT_SYNC_SCRIPT
    argv: list[str] = []
    if repo is not None:
        argv += ["--repo", repo]
    if issue is not None:
        argv += ["--issue", str(issue)]
    if assignee is not None:
        argv += ["--assignee", assignee]
    if dry_run:
        argv.append("--dry-run")
    cmd = ["node", str(script), *argv]

    result = _invoke(
        cmd, cwd=cwd, timeout=timeout, run=run, log=log, label="board sync",
    )
    if result is None:
        return BoardSyncResult(ok=False, returncode=None)

    if result.returncode != 0:
        _warn(
            log,
            f"board sync: project-sync.mjs exited {result.returncode}: "
            f"{result.stderr.strip()}",
        )
    return BoardSyncResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
