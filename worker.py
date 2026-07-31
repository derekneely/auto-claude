"""Worker processes — run Claude CLI to develop solutions or produce plans/reviews."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from multiprocessing import Event, Queue
from pathlib import Path

import stages
from db.harness import Harness
from db.journal import NullJournal
from db.pool import Database
from dbsync import DbSync
from ghauth import apply_git_credentials, build_env, current_token
from integrations import TelemetryEvent, log_event
from logger import WorkerLogger
from pipeline import load_pipeline_config
from ratelimit import (
    RateLimitInfo,
    detect_rate_limit_in_stderr,
    parse_rate_limit_event,
    pause_seconds,
)
from redact import redact
from worktree_setup import RepoSetupConfig, prepare_worktree


# ---------------------------------------------------------------------------
# Push guard
# ---------------------------------------------------------------------------

# Shared branches auto-claude must never push to, on top of whatever the repo
# configures as its PR base. `develop` and `trunk` are not used here today but
# cost nothing to refuse and would be silently unprotected otherwise.
PROTECTED_BRANCHES = frozenset({"main", "master", "dev", "develop", "trunk"})


class ProtectedBranchError(RuntimeError):
    """Raised when a worker is about to push to a shared branch."""


def _normalize_branch(branch: str | None) -> str:
    """Lowercase, trim, and strip a `refs/heads/` prefix for comparison."""
    name = (branch or "").strip()
    if name.lower().startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    return name.lower()


def assert_pushable(branch: str | None, base_branch: str | None) -> None:
    """Refuse to push to a shared branch. Raises `ProtectedBranchError`.

    Called immediately before every `git push`. The branch name is not always
    computed by us — `_setup_rework_worktree` reads it from local state and
    `run_review_worker` reads it from a PR's `headRefName` — so a stale or
    hostile value can otherwise reach `git push origin <branch>`. Server-side
    branch protection is unavailable on the org's plan, which makes this the
    only guard.
    """
    name = _normalize_branch(branch)
    if not name:
        raise ProtectedBranchError(
            "Refusing to push: branch name is empty. `git push origin ''` "
            "falls back to the push default, which on a base-branch checkout "
            "pushes the base branch."
        )
    if name == "head":
        raise ProtectedBranchError(
            "Refusing to push: branch resolved to HEAD, which pushes whatever "
            "the worktree happens to have checked out."
        )
    if name in PROTECTED_BRANCHES or name == _normalize_branch(base_branch):
        raise ProtectedBranchError(
            f"Refusing to push to protected branch '{(branch or '').strip()}'. "
            f"Workers may only push their own ac/issue-* branches."
        )


class LeaseLostError(RuntimeError):
    """Raised when this harness no longer holds the Postgres lease on the
    issue being worked, discovered immediately before an irreversible remote
    act. Mirrors `ProtectedBranchError`: fails LOUD by raising, so a caller
    cannot forget to check a return value.
    """


def _assert_lease_held(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Refuse an irreversible act once another harness owns this issue.

    Called immediately before every `git push`, `gh pr create`, `gh pr
    review`, and inside `_set_labels` - the single chokepoint for every
    `ac-*` label write - mirroring where `assert_pushable` is called. A
    no-op when `ctx.harness_id` is unset OR `ctx.lease_db_url_env` is unset
    OR that env var itself has no value, because all three mean "no shared
    database, no second harness to fence against" - see
    `main._release_stale_locks`'s degraded-path reasoning, applied here
    mid-run instead of at startup. Critically, `ctx.harness_id` being unset
    is *itself* the degraded-mode signal `ProcessManager.spawn` derives from
    whether Postgres is configured (see its own docstring) - the env-var
    check alone is not enough, because `PIPELINE_METRICS_DATABASE_URL` is
    also used by the sibling Node telemetry and is routinely set even when
    `[database] enabled = false` for auto-claude's own lease system. Reading
    `ctx.lease_db_url_env` rather than a hardcoded constant matters for the
    same reason: an operator who points `[database].url_env` at a different
    variable must have this fence check the SAME Postgres `main` does, not
    silently default back to the telemetry variable.

    Builds its own `Database`/`DbSync` from `os.environ` rather than
    receiving one from `main`: the worker is a separate `spawn`ed process,
    so a live connection cannot cross the pickle boundary. Only
    `check_lease` is ever called on the result, which per its docstring
    fails CLOSED (returns False, i.e. "lease lost") if Postgres is
    unreachable after its own internal retries - so this function needs no
    second layer of retry logic.
    """
    if not ctx.harness_id:
        return
    if not ctx.lease_db_url_env:
        return
    url = os.environ.get(ctx.lease_db_url_env)
    if not url:
        return

    db = Database(url)
    try:
        harness = Harness(id=ctx.harness_id, hostname="", pid=0, version="")
        # A NullJournal, not a real Journal: check_lease never journals in
        # the first place (spec: "Claims and fence checks never queue"), and
        # `journal` is only a required DbSync constructor argument (fix
        # round, Finding 2) so this throwaway DbSync can be built at all.
        # Using a real Journal on a hardcoded relative path used to make
        # "this call never journals" a usage convention rather than a
        # structural guarantee - the day a durable write is added to a
        # worker by mistake, it would silently append cross-process into
        # main's real journal.jsonl (see Journal.replay's read-then-
        # truncate race). NullJournal.append raising turns that into an
        # immediate, loud failure instead.
        dbsync = DbSync(db, harness, logger, journal=NullJournal())
        if not dbsync.check_lease(ctx.issue_id):
            raise LeaseLostError(
                f"Lease on {ctx.issue_id} is no longer held by harness "
                f"{ctx.harness_id} — refusing to touch the remote."
            )
    finally:
        db.close()


def _handle_lease_lost(
    ctx: IssueContext,
    logger: WorkerLogger,
    state_queue: Queue,
    exc: LeaseLostError,
    *,
    branch: str | None = None,
    run_id: str | None = None,
    metrics: "RunMetrics | None" = None,
    summaries: list[dict] | None = None,
) -> None:
    """Common fenced-exit path shared by run_dev_worker and run_review_worker.

    Writes a local crash log (disk only, never the remote) and sends a
    StateUpdate whose `error` is prefixed "fenced:" — StateUpdate's shape is
    frozen for Phases A-C, so this is how a later phase's summary capture
    can recognise and record a `summary` row with kind="fenced" without a
    new field. The `run` row itself is closed right here, with
    `run_outcome="fenced"`: a fenced worker's first StateUpdate already
    opened it (see run_dev_worker/run_review_worker's setup), and nothing
    else in this exit path would ever close it otherwise — the caller's
    status goes straight to "failed", not "in_progress", so `reap_dead`'s
    and `_mark_interrupted`'s dangling-run cleanup (both gated on the issue
    still being IN_PROGRESS) never fire for it. Deliberately does NOT call
    `_post_crash_comment` and does NOT clean up the worktree — the branch
    stays local and the issue is retried by whichever harness now holds it.
    No new GitHub comment is posted here because there is nothing useful
    left to say to a ticket this harness no longer owns, not because a
    comment is itself one of the remote acts fencing guards (the enumerated
    fence sites are `git push`, `gh pr create`/`review`, and `ac-*` label
    writes — a duplicate comment from another harness picking up the same
    issue is noise, not corruption, so comments are deliberately unfenced
    everywhere else in this file).

    `summaries` carries any `summary` rows a comment already posted to
    GitHub before the lease was discovered lost (e.g. `_post_issue_report`'s
    failed-checks report) — without threading it through, that comment would
    be the one path where something real lands on GitHub and is then
    forgotten by Postgres, contradicting "the database and GitHub cannot
    disagree". Passed straight to the terminal StateUpdate alongside the
    existing `fenced:`-prefixed summary `_sync_run` synthesizes from `error`
    (see process_manager.py) — the two are independent summary rows, not a
    replacement for each other.
    """
    logger.error(f"Fenced: {exc}")
    log_path = _write_crash_log(ctx, str(exc), logger)
    logger.warn(f"Lease lost — leaving remote state untouched; crash log at {log_path}")
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="failed",
        error=f"fenced: {exc}",
        branch=branch,
        pr_url=ctx.pr_url,
        run_id=run_id,
        run_outcome="fenced",
        duration_seconds=metrics.duration_seconds if metrics else None,
        cost_usd=metrics.cost_usd if metrics else None,
        turns=metrics.turns if metrics else None,
        crash_log_path=str(log_path) if log_path else None,
        summaries=summaries or None,
    ))


# ---------------------------------------------------------------------------
# Dataclasses (picklable — passed from main process to worker)
# ---------------------------------------------------------------------------

@dataclass
class IssueContext:
    """Everything a worker needs to process an issue."""
    issue_id: str           # "{repo}#{number}"
    repo: str
    number: int
    title: str
    body: str
    action: str             # kind hint: "fix", "implement", "test", "rework"
    org: str
    # Per-repo prBaseBranch from .claude/pipeline.json, falling back to the
    # global [github].base_branch when the repo has no pipeline.json.
    base_branch: str
    # Paths
    repos_dir: Path
    worktrees_dir: Path
    prompts_dir: Path
    # Claude settings
    dev_model: str
    light_model: str
    permission_mode: str
    max_budget_usd: float
    max_turns: int
    crash_logs_dir: Path
    # Worker color (for logging)
    color_name: str
    color_code: str
    # Rework fields (None for fresh work)
    existing_branch: str | None = None
    pr_url: str | None = None
    rework_count: int = 0
    handoff_summary: str | None = None
    grace_budget_usd: float = 1.0
    # Shared-telemetry wiring. `pipeline_project` is the metrics discriminator
    # from pipeline.json (defaults to the repo name); both are None/absent when
    # the toolchain is not configured, which disables telemetry silently.
    claude_tools_root: Path | None = None
    pipeline_project: str = ""
    # Worktree-preparation overrides from `[repos.<name>]`. None means
    # auto-detect from what is in the checkout, which is the normal case.
    repo_setup: RepoSetupConfig | None = None
    # Which Postgres harness row spawned this worker. Set by
    # ProcessManager.spawn from its own `harness_id`, which in turn is
    # None whenever `main._init_db_layer` built no `Database` (Postgres
    # disabled, or no URL configured) - see main.py's `_harness_id_for_
    # workers`. `_assert_lease_held` treats None the same as "no shared
    # database".
    harness_id: str | None = None
    # Env-var name `_assert_lease_held` reads to build its own throwaway
    # Database/DbSync - the raw connection URL itself must never cross the
    # pickle boundary (a live credential should never be logged, and this
    # keeps it out of any StateUpdate/crash-log dump too). Set by
    # ProcessManager.spawn from `config.database.url_env`, so a worker
    # fences against the SAME Postgres `main` uses even when an operator has
    # pointed `[database].url_env` away from the default
    # PIPELINE_METRICS_DATABASE_URL telemetry variable.
    lease_db_url_env: str | None = None


@dataclass
class StateUpdate:
    """Status update sent from worker back to main process via state_queue."""
    issue_id: str
    status: str             # IssueStatus value
    error: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    worker_pid: int | None = None
    handoff_summary: str | None = None
    # Epoch seconds until which the supervisor should stop spawning workers.
    # Set when Claude reports a rate limit; see ratelimit.py.
    rate_limited_until: float | None = None
    # Run identity + metrics, threaded from worker back to `main` — the sole
    # Postgres writer — so a `run` row can be opened/closed without the
    # worker touching the database directly. `run_mode`/`run_model` are set
    # on the FIRST update of a run (opens the row); `run_outcome` and the
    # metric fields are set on the LAST update (closes it). A message never
    # carries both halves.
    run_id: str | None = None
    run_mode: str | None = None       # "dev" | "review"
    run_model: str | None = None
    run_outcome: str | None = None    # completed|failed|interrupted|fenced
    exit_code: int | None = None
    duration_seconds: int | None = None
    cost_usd: float | None = None
    turns: int | None = None
    crash_log_path: str | None = None
    # Comments this run posted to GitHub, to be written as `summary` rows
    # alongside the closing update. A list because some failure paths post
    # more than one comment for the same terminal event (e.g. a
    # failed-checks report from `_post_issue_report`, followed by the crash
    # comment from the outer exception handler) — each is its own row. Each
    # dict has keys {"kind", "body", "comment_url"}.
    summaries: list[dict] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_crash_log(
    ctx: IssueContext,
    error: str,
    logger: WorkerLogger,
) -> Path | None:
    """Write a crash log file and return its path."""
    try:
        from datetime import datetime, timezone
        crash_dir = ctx.crash_logs_dir
        crash_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{date_str}-{ctx.repo}-{ctx.number}.log"
        log_path = crash_dir / filename
        log_path.write_text(
            f"Issue: {ctx.issue_id}\n"
            f"Action: {ctx.action}\n"
            f"Model: {ctx.dev_model}\n"
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
            f"{'=' * 60}\n\n"
            f"{error}\n",
            encoding="utf-8",
        )
        logger.info(f"Crash log written to {log_path}")
        return log_path
    except Exception as exc:
        logger.error(f"Failed to write crash log: {exc}")
        return None


def _post_crash_comment(
    ctx: IssueContext,
    error: str,
    log_path: Path | None,
    logger: WorkerLogger,
) -> tuple[str | None, str]:
    """Post a concise failure comment on the issue referencing the local crash log.

    Returns (comment_url, body) — the exact text posted and the URL of the
    created comment (None if the post failed), for the caller's `summary`
    row.
    """
    log_ref = f"\n\nCrash log: `{log_path}`" if log_path else ""
    body = redact(
        f"**auto-claude** failed while processing this issue.\n\n"
        f"> {error[:200]}{log_ref}\n\n"
        f"_Re-label to retry after investigating._"
    )
    env = build_env(current_token())
    try:
        result = subprocess.run(
            [
                "gh", "issue", "comment", str(ctx.number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--body", body,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return None, body
        url = result.stdout.strip()
        return (url if url.startswith("http") else None), body
    except Exception as exc:
        logger.error(f"Failed to post crash comment: {exc}")
        return None, body


def sanitize_branch_name(title: str, number: int) -> str:
    """Create a safe git branch name from an issue title."""
    # Lowercase, replace non-alphanumeric with hyphens, collapse multiples, trim
    sanitized = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    # Limit length to keep total branch name short (Windows 260-char path limit)
    sanitized = sanitized[:40].rstrip("-")
    return f"ac/issue-{number}-{sanitized}"


def _run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    logger: WorkerLogger | None = None,
    timeout: int = 120,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess command, log it, and return the result."""
    env = build_env(current_token())
    if env_extra:
        env.update(env_extra)

    # Network git commands must not fall through to the system credential
    # helper, which holds the operator's credentials rather than the bot's.
    cmd = apply_git_credentials(cmd)

    if logger:
        logger.info(f"$ {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
        encoding="utf-8",
        errors="replace",
    )
    return result


def _build_prompt(
    ctx: IssueContext,
    comments: list[dict] | None = None,
    review_comments: list[dict] | None = None,
) -> str:
    """Build a prompt from the appropriate template file."""
    # Pick template based on action and context
    if ctx.handoff_summary and ctx.action in ("fix", "implement", "test"):
        # Continuation from a previous budget-exhausted run
        template_file = ctx.prompts_dir / "continue.txt"
    elif ctx.action == "rework" and review_comments:
        template_file = ctx.prompts_dir / "rework.txt"
    else:
        # Kind hints only - `stages.kind_of` can return nothing else, and it
        # defaults to "implement". The dev worker never uses review.txt; the
        # review worker builds its own prompt.
        template_map = {
            "fix": "develop.txt",
            "implement": "develop.txt",
            "test": "test.txt",
            "rework": "rework.txt",
        }
        template_file = ctx.prompts_dir / template_map.get(ctx.action, "develop.txt")

    template = template_file.read_text(encoding="utf-8")

    # Build comments section
    comments_section = ""
    if comments:
        lines = ["Comments:"]
        for c in comments:
            user = c.get("user", {}).get("login", "unknown")
            body = c.get("body", "")
            lines.append(f"  @{user}: {body}")
        comments_section = "\n".join(lines)

    # Build review section (for rework)
    review_section = ""
    if review_comments:
        review_section = _format_review_section(review_comments)

    # Shared orchestration framing. Kept in one file rather than duplicated
    # across four templates: a prompt that drifts out of sync silently reverts
    # its agent to a flat coder, which is invisible until you read the logs.
    try:
        orchestration = (ctx.prompts_dir / "_orchestration.txt").read_text(encoding="utf-8")
    except OSError:
        orchestration = ""

    format_vars = dict(
        number=ctx.number,
        org=ctx.org,
        repo=ctx.repo,
        title=ctx.title,
        body=ctx.body or "(no body)",
        action=ctx.action,
        comments_section=comments_section,
        orchestration=orchestration,
    )
    if ctx.action == "rework":
        format_vars["pr_url"] = ctx.pr_url or ""
        format_vars["review_section"] = review_section
    if ctx.handoff_summary:
        format_vars["handoff_summary"] = ctx.handoff_summary

    return template.format(**format_vars)


def _format_review_section(review_comments: list[dict]) -> str:
    """Format PR review data into a readable section for the prompt."""
    lines = ["PR Review Feedback:"]

    reviews = review_comments.get("reviews", []) if isinstance(review_comments, dict) else []
    inline = review_comments.get("inline", []) if isinstance(review_comments, dict) else []

    # Top-level reviews (approve, changes_requested, etc.)
    for r in reviews:
        user = r.get("user", {}).get("login", "unknown")
        state = r.get("state", "COMMENTED")
        body = r.get("body", "").strip()
        if body:
            lines.append(f"\n  @{user} ({state}):")
            lines.append(f"    {body}")

    # Inline comments (file-specific)
    if inline:
        lines.append("\n  Inline comments:")
        for c in inline:
            user = c.get("user", {}).get("login", "unknown")
            path = c.get("path", "unknown")
            line_num = c.get("line") or c.get("original_line") or "?"
            body = c.get("body", "").strip()
            if body:
                lines.append(f"    {path}:{line_num} — @{user}: {body}")

    if len(lines) == 1:
        return ""  # No actual review content
    return "\n".join(lines)


@dataclass
class RunMetrics:
    """Cost/turns/duration parsed from a Claude CLI `stream-json` result event.

    All fields are None when no result event was seen — a crash mid-run, or
    output truncated before the CLI got to emit one — so a caller writing
    this to Postgres stores NULL rather than a fabricated zero.
    """
    cost_usd: float | None = None
    turns: int | None = None
    duration_seconds: int | None = None


def _parse_run_metrics(output: str) -> RunMetrics:
    """Extract cost/turns/duration from the LAST `"type":"result"` line.

    Mirrors `_extract_result_text`'s line-by-line parse below. Malformed
    lines are skipped rather than fatal, since stream-json output is
    line-buffered from a subprocess and a truncated final line is routine.
    When more than one result event appears, the last one wins — matching
    the equivalent last-write behaviour a caller would get by re-running
    `_extract_result_text` over the same stream.
    """
    metrics = RunMetrics()
    for line in output.split("\n"):
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("type") != "result":
            continue
        cost = data.get("total_cost_usd")
        turns = data.get("num_turns")
        duration_ms = data.get("duration_ms")
        metrics = RunMetrics(
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            turns=int(turns) if isinstance(turns, int) else None,
            duration_seconds=(
                round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None
            ),
        )
    return metrics


def _accumulate_metrics(base: RunMetrics, extra: RunMetrics) -> RunMetrics:
    """Sum two readings from sequential `_run_claude` calls billed to one run.

    Used to fold a same-model repair round into the primary call's totals —
    both bill against the same `run` row. A field stays None only when BOTH
    inputs are None; one usable reading is better than discarding it because
    the other call's result event never arrived.
    """
    def _sum(a: float | int | None, b: float | int | None):
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    return RunMetrics(
        cost_usd=_sum(base.cost_usd, extra.cost_usd),
        turns=_sum(base.turns, extra.turns),
        duration_seconds=_sum(base.duration_seconds, extra.duration_seconds),
    )


def _run_claude(
    prompt: str,
    cwd: Path,
    ctx: IssueContext,
    logger: WorkerLogger,
    abort_event: Event,
    bypass_permissions: bool = True,
    budget_override: float | None = None,
    model_override: str | None = None,
    max_turns_override: int | None = None,
) -> tuple[int, str, bool, RateLimitInfo | None, RunMetrics]:
    """Run Claude CLI via Popen, stream output to logger.

    Returns (returncode, captured_output, budget_exceeded, rate_limit, metrics).

    `rate_limit` is the last limiting rate_limit_event seen, or None. Under
    subscription auth this — not the USD budget — is the constraint that
    actually stops work. `metrics` is parsed from the stream-json result
    event and is all-None if the run crashed before emitting one.
    """
    model = model_override or ctx.dev_model
    max_turns = max_turns_override or ctx.max_turns
    cmd = [
        "claude",
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--model", model,
        "--max-turns", str(max_turns),
        "--no-session-persistence",
    ]
    if bypass_permissions:
        cmd += ["--permission-mode", ctx.permission_mode]
        budget = budget_override if budget_override is not None else ctx.max_budget_usd
        cmd += ["--max-budget-usd", str(budget)]
    cmd.append(prompt)

    # The agent runs `gh` itself (commits, PRs, comments), so it inherits the
    # bot identity too — otherwise its calls would be attributed to the operator.
    env = build_env(current_token())

    logger.info("Starting Claude CLI...")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=env,
        encoding="utf-8",
        errors="replace",
    )

    # Read stderr in a background thread to avoid deadlock
    stderr_lines: list[str] = []
    def _read_stderr():
        for line in proc.stderr:
            stderr_lines.append(line.rstrip("\n\r"))

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    captured_lines: list[str] = []
    rate_limit: RateLimitInfo | None = None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n\r")
            if not line:
                continue

            captured_lines.append(line)

            # Rate-limit events arrive inline on every run, usually "allowed".
            # Keep only the limiting ones; log an overage warning once it starts.
            event = parse_rate_limit_event(line)
            if event is not None:
                if event.is_limited:
                    rate_limit = event
                    logger.warn(
                        f"Rate limited by Claude ({event.limit_type or 'unknown window'}, "
                        f"status={event.status})"
                    )
                elif event.is_using_overage:
                    logger.warn(
                        f"Running on overage quota ({event.limit_type or 'unknown window'})"
                    )

            # Try to parse stream-json and extract text content for logging
            display = _extract_display_text(line)
            if display:
                logger.info(display)

            # Check abort between lines
            if abort_event.is_set():
                logger.warn("Abort requested — terminating Claude")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return (-1, "", False, rate_limit, RunMetrics())

        proc.wait()
    except Exception as exc:
        logger.error(f"Error reading Claude output: {exc}")
        proc.kill()
        proc.wait()
        return (-1, "", False, rate_limit, RunMetrics())

    stderr_thread.join(timeout=5)
    stderr_output = "\n".join(stderr_lines).strip()
    budget_exceeded = "Exceeded USD budget" in stderr_output

    # Fallback: a run can die from rate limiting before emitting a usable event.
    # Budget exhaustion is deliberately excluded — it has its own recovery path.
    if (
        rate_limit is None
        and proc.returncode != 0
        and not budget_exceeded
        and detect_rate_limit_in_stderr(stderr_output)
    ):
        rate_limit = RateLimitInfo(status="rejected")

    if proc.returncode != 0:
        logger.error(f"Claude exited with code {proc.returncode}")
        if budget_exceeded:
            logger.warn("Budget limit reached")
        elif rate_limit is not None:
            logger.warn("Rate limit reached")
        elif stderr_output:
            logger.error(f"Claude stderr: {stderr_output}")
        else:
            logger.warn("No stderr output from Claude")

    captured_output = "\n".join(captured_lines)
    metrics = _parse_run_metrics(captured_output)
    return (proc.returncode, captured_output, budget_exceeded, rate_limit, metrics)


def _rate_limited_until(info: RateLimitInfo) -> float:
    """Absolute epoch time until which the supervisor should stop spawning."""
    return time.time() + pause_seconds(info, now=time.time())


def _extract_display_text(line: str) -> str:
    """Extract human-readable text from a stream-json line."""
    try:
        data = json.loads(line)
        # stream-json emits {"type": "assistant", "message": {...}} with content blocks
        if data.get("type") == "assistant":
            msg = data.get("message", {})
            content = msg.get("content", [])
            texts = []
            for block in content:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
            if texts:
                return " ".join(texts)[:200]
        # Also handle {"type": "result", ...}
        if data.get("type") == "result":
            result_text = data.get("result", "")
            if isinstance(result_text, str) and result_text:
                return f"[result] {result_text[:200]}"
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return ""


def _extract_summary(output: str) -> str:
    """Extract the IMPLEMENTATION_SUMMARY line from Claude's output.

    Delegates to `_extract_block`, which decodes stream-json before looking for
    the marker. The previous line-by-line scan checked its raw-text branch
    first, and under `output_format = "stream-json"` the whole result event is
    one physical line whose raw text contains the marker — so the raw branch
    always won and returned everything after it: literal \\n escapes, the
    trailing NOTES block, and the JSON envelope's session_id/request_id/token
    counts. All of that reached the body of a public pull request (PR #334).
    """
    return _extract_block(output, "IMPLEMENTATION_SUMMARY")


#: Markers the agent emits, in the order they appear. A block runs until the
#: next marker or the end of the text.
OUTPUT_MARKERS = (
    "IMPLEMENTATION_PLAN",
    "IMPLEMENTATION_SUMMARY",
    "CHANGES",
    "TESTING_STEPS",
    "IMPLEMENTATION_NOTES",
)

#: Pull-request body sections, in render order, each paired with the keyword
#: that fills it. A section with no content is dropped rather than rendered as
#: a bare header.
_PR_SECTIONS = (
    ("## Summary", "summary"),
    ("## Changes", "changes"),
    ("## How to test", "testing"),
    ("## Notes for the reviewer", "notes"),
)


def _pr_body(*, summary: str, changes: str, testing: str, notes: str,
             number: int) -> str:
    """Assemble the pull-request body from the agent's emitted sections.

    Every value is agent-authored free text headed for a public pull request,
    so each is redacted individually. The body always closes the issue, even
    when the agent emitted nothing usable.
    """
    values = {
        "summary": summary, "changes": changes,
        "testing": testing, "notes": notes,
    }
    parts: list[str] = []
    for header, key in _PR_SECTIONS:
        text = (values[key] or "").strip()
        if not text:
            continue
        parts.append(f"{header}\n\n{redact(text)}")
    parts.append(f"Closes #{number}")
    return "\n\n".join(parts)

#: Markers the prompt specifies as a single line. Without this, a summary
#: absorbs every trailing line the agent emits after it.
SINGLE_LINE_MARKERS = frozenset({"IMPLEMENTATION_SUMMARY"})


def _extract_block(output: str, marker: str) -> str:
    """Pull one `MARKER:` section out of the agent's output.

    Handles both raw text and stream-json. Takes the **last** occurrence: the
    prompt names each marker when it asks for them, and that echo would
    otherwise win over the real emission.
    """
    text = _extract_result_text(output) or output
    needle = f"{marker}:"

    idx = text.rfind(needle)
    if idx == -1:
        return ""
    body = text[idx + len(needle):]

    # Stop at whichever other marker comes first.
    end = len(body)
    for other in OUTPUT_MARKERS:
        if other == marker:
            continue
        found = body.find(f"{other}:")
        if found != -1:
            end = min(end, found)
    block = body[:end].strip()

    if marker in SINGLE_LINE_MARKERS:
        return block.split("\n", 1)[0].strip()
    return block


def _issue_report(
    *,
    plan: str,
    summary: str,
    notes: str,
    attempt: int | None,
    model: str,
    branch: str,
    pr_url: str | None,
    outcome: str,
) -> str:
    """Build the comment auto-claude posts back to the issue.

    The issue is the pipeline's source of truth for a human reading it later,
    but until now it only ever received "PR created: <url>". Everything the
    agent decided lived in a log file nobody opens. Empty sections are omitted
    rather than printed as bare headers.
    """
    ok = outcome == "success"
    lines = [f"## 🤖 auto-claude — {'implementation complete' if ok else 'attempt failed'}"]

    meta = [f"**Model:** `{model}`", f"**Branch:** `{branch}`"]
    if attempt:
        meta.insert(0, f"**Attempt:** {attempt}")
    if pr_url:
        meta.append(f"**PR:** {pr_url}")
    lines.append(" · ".join(meta))

    for heading, body in (
        ("Plan", plan),
        ("Summary", summary),
        ("Notes", notes),
    ):
        if body and body.strip() and body.strip().lower() != "none.":
            lines.append(f"\n### {heading}\n\n{redact(body.strip())}")

    if not ok:
        lines.append(
            "\n_The daemon has bumped the attempt counter and returned this "
            "issue to `ac-dev-ready`, or moved it to `ac-blocked` on the third "
            "failure._"
        )
    return "\n".join(lines)


def _build_repair_prompt(transcript: str) -> str:
    """Prompt for the one in-session repair round after checks fail.

    A fresh `claude` invocation, so none of the first prompt's boundaries carry
    over — they have to be restated here.
    """
    return (
        "The work you just did is already committed in this worktree, but the "
        "repository's own verify/test commands fail against it.\n\n"
        f"{transcript}\n\n"
        "Fix the cause. Do not start over and do not revert the existing work — "
        "amend it. Keep the fix as small as the failure requires; if the failure "
        "is unrelated to your change, say so rather than papering over it.\n\n"
        "You may edit files and commit locally. Do NOT run git push, do NOT open "
        "or modify a pull request, and do NOT touch any ac-* label — the daemon "
        "owns all of that.\n\n"
        "When you are done, end with IMPLEMENTATION_SUMMARY: followed by one "
        "sentence describing the fix."
    )


def _prepare_and_check(
    ctx: IssueContext,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> tuple[bool, str]:
    """Make the worktree buildable, then run its verify/test commands.

    A fresh worktree has tracked source and nothing else — no `node_modules`,
    no generated Prisma client, no gitignored env. Running checks without this
    fails on a missing toolchain, which is indistinguishable from a real
    failure and would blame the code for it.
    """
    setup = prepare_worktree(worktree_dir, ctx.repo_setup, logger)
    if not setup.ok:
        return False, (
            "Worktree preparation failed — the verify commands were not run.\n\n"
            + setup.transcript
        )
    return _run_pipeline_checks(ctx, worktree_dir, logger)


def _post_issue_report(
    ctx: IssueContext,
    *,
    output: str,
    summary: str,
    branch: str,
    pr_url: str | None,
    outcome: str,
    logger: WorkerLogger,
    notes_override: str | None = None,
) -> tuple[str, str | None]:
    """Post the agent's plan, summary and notes back to the issue.

    Never fatal. A worker that wrote the code, pushed it and opened a PR has
    done its job; failing it over a comment would send a completed issue back
    round the retry loop and produce a second PR.

    Returns (body, comment_url) — the exact text posted and the URL of the
    created comment (None if the post failed or the URL could not be
    determined), for the caller's `summary` row.
    """
    body = ""
    try:
        try:
            attempt = stages.attempt_of(_get_issue_labels(ctx, logger))
        except Exception:
            attempt = None

        body = _issue_report(
            plan=_extract_block(output, "IMPLEMENTATION_PLAN"),
            summary=summary,
            notes=notes_override or _extract_block(output, "IMPLEMENTATION_NOTES"),
            attempt=attempt,
            model=ctx.dev_model,
            branch=branch,
            pr_url=pr_url,
            outcome=outcome,
        )
        result = _run_cmd(
            [
                "gh", "issue", "comment", str(ctx.number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--body", body,
            ],
            logger=logger,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warn(f"Could not post issue report: {result.stderr.strip()}")
            return body, None
        url = result.stdout.strip()
        return body, (url if url.startswith("http") else None)
    except Exception as exc:
        logger.warn(f"Could not post issue report: {exc}")
        return body, None


def _extract_result_text(output: str) -> str:
    """Extract the full result text from stream-json output."""
    texts: list[str] = []
    for line in output.split("\n"):
        try:
            data = json.loads(line)
            if data.get("type") == "result":
                result = data.get("result", "")
                if isinstance(result, str):
                    texts.append(result)
            elif data.get("type") == "assistant":
                msg = data.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
        except (json.JSONDecodeError, TypeError):
            pass
    return "\n".join(texts) if texts else output


# The review prompt (prompts/review.txt) asks Claude to end its response with
# exactly this marker. Matched case-insensitively since models are not
# perfectly consistent about case, and the *last* occurrence wins so an
# echoed copy of the prompt's own instructions doesn't get mistaken for the
# actual verdict.
_VERDICT_RE = re.compile(r"REVIEW_VERDICT:\s*(PASS|FAIL)", re.IGNORECASE)
_FEEDBACK_RE = re.compile(r"REVIEW_FEEDBACK:\s*(.*?)(?=REVIEW_VERDICT:|\Z)", re.IGNORECASE | re.DOTALL)


def _parse_review_verdict(text: str) -> bool:
    """True only for an explicit PASS in the model's review output.

    Anything else — an explicit FAIL, no marker at all, or a garbled value —
    resolves to False. An unparseable review must never be treated as an
    approval; the caller cannot tell "Claude said no" apart from "Claude's
    output was truncated/malformed", so both must fail closed.
    """
    matches = _VERDICT_RE.findall(text)
    if not matches:
        return False
    return matches[-1].upper() == "PASS"


def _extract_review_feedback(text: str) -> str:
    """Pull the REVIEW_FEEDBACK: section out of Claude's review output.

    Falls back to the full (trimmed) text when the model didn't use the
    marker, so a failed review never posts an empty --request-changes body.
    """
    matches = _FEEDBACK_RE.findall(text)
    feedback = matches[-1].strip() if matches else ""
    if feedback:
        return feedback
    fallback = text.strip()
    return fallback[:4000] if fallback else "Review agent did not produce readable feedback."


def _run_handoff_summary(
    ctx: IssueContext,
    cwd: Path,
    captured_output: str,
    logger: WorkerLogger,
    abort_event: Event,
) -> str:
    """Run a grace-budget Claude invocation to produce a handoff summary.

    Called after budget exhaustion so the next agent knows where to pick up.
    Returns the handoff text, or a fallback message if this also fails.
    """
    # Truncate captured output to last ~2000 chars to keep prompt manageable
    output_tail = captured_output[-2000:] if len(captured_output) > 2000 else captured_output

    prompt = (
        "You ran out of budget while working on a GitHub issue. "
        "Review the current state of the code in this directory and the output "
        "from your previous work session below, then produce a handoff summary "
        "for the next agent.\n\n"
        "Previous session output (tail):\n"
        f"{output_tail}\n\n"
        "Please output:\n"
        "1. What files you modified and what changes you made\n"
        "2. What remains to be done to complete the task\n"
        "3. Any important context the next agent should know\n"
        "4. Suggested next steps in priority order\n\n"
        "Be concise but thorough. Start your response with HANDOFF: on the first line."
    )

    logger.info(f"Running grace-budget handoff summary (model={ctx.light_model})...")
    # Metrics discarded: this call bills ctx.light_model, a different model
    # paying for a different, smaller job than the primary run. Folding its
    # cost into the primary run's single `model` column would misattribute
    # spend, so it is dropped here rather than accumulated.
    returncode, output, _, _, _metrics = _run_claude(
        prompt=prompt,
        cwd=cwd,
        ctx=ctx,
        logger=logger,
        abort_event=abort_event,
        bypass_permissions=True,
        budget_override=ctx.grace_budget_usd,
        model_override=ctx.light_model,
        max_turns_override=10,
    )

    if returncode != 0 or not output.strip():
        logger.warn("Handoff summary failed — using fallback")
        return "Previous agent ran out of budget. No detailed handoff available. Check git log and working directory for partial progress."

    # Extract text from stream-json output
    result_text = _extract_result_text(output)
    # Try to find HANDOFF: marker
    if "HANDOFF:" in result_text:
        idx = result_text.index("HANDOFF:")
        return result_text[idx + len("HANDOFF:"):].strip()
    return result_text.strip() if result_text.strip() else "Previous agent ran out of budget. Check git log for partial progress."


def _push_partial_work(
    ctx: IssueContext,
    branch: str,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> str | None:
    """Commit and push partial work after budget exhaustion. Returns pr_url or None."""
    # This path is best-effort by contract — every other failure here returns
    # None rather than raising, so the guard does the same.
    try:
        assert_pushable(branch, ctx.base_branch)
    except ProtectedBranchError as exc:
        logger.error(str(exc))
        return None
    _assert_lease_held(ctx, logger)

    # Check for uncommitted changes
    status_result = _run_cmd(
        ["git", "status", "--porcelain"],
        cwd=worktree_dir,
        logger=logger,
    )
    has_uncommitted = bool(status_result.stdout.strip())

    # Also check if Claude already made commits
    base_ref = f"origin/{ctx.base_branch}"
    log_result = _run_cmd(
        ["git", "log", f"{base_ref}..HEAD", "--oneline"],
        cwd=worktree_dir,
        logger=logger,
    )
    has_commits = bool(log_result.stdout.strip())

    if not has_uncommitted and not has_commits:
        logger.info("No partial changes to commit")
        return None

    if has_uncommitted:
        _run_cmd(["git", "add", "-A"], cwd=worktree_dir, logger=logger)
        commit_msg = f"wip: partial progress on #{ctx.number} (budget exceeded)"
        result = _run_cmd(
            ["git", "commit", "-m", commit_msg],
            cwd=worktree_dir,
            logger=logger,
        )
        if result.returncode != 0:
            logger.warn(f"Partial commit failed: {result.stderr.strip()}")
            if not has_commits:
                return None
    else:
        logger.info("Claude already committed partial work — pushing existing commits")

    logger.info(f"Pushing partial work to branch {branch}...")
    result = _run_cmd(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree_dir,
        logger=logger,
        timeout=60,
    )
    if result.returncode != 0:
        logger.warn(f"Partial push failed: {result.stderr.strip()}")
        return None

    # Create PR if one doesn't exist yet
    if not ctx.pr_url:
        _assert_lease_held(ctx, logger)
        pr_body = f"Work in progress — budget exceeded, continuation pending.\n\nAddresses #{ctx.number}"
        pr_title = f"wip: {ctx.title} (#{ctx.number})"
        logger.info("Creating WIP pull request...")
        result = _run_cmd(
            [
                "gh", "pr", "create",
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--title", pr_title,
                "--body", pr_body,
                "--head", branch,
                "--base", ctx.base_branch,
                "--draft",
            ],
            logger=logger,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()

    return ctx.pr_url


def _get_pr_reviews(ctx: IssueContext, logger: WorkerLogger) -> dict:
    """Fetch PR reviews and inline comments via gh CLI.

    Returns {"reviews": [...], "inline": [...]}.
    """
    if not ctx.pr_url:
        return {"reviews": [], "inline": []}

    # Extract PR number from URL like .../pull/42
    pr_number = ctx.pr_url.rstrip("/").split("/")[-1]

    reviews = []
    result = _run_cmd(
        ["gh", "api", f"/repos/{ctx.org}/{ctx.repo}/pulls/{pr_number}/reviews"],
        logger=logger,
        timeout=30,
    )
    if result.returncode == 0:
        try:
            reviews = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

    inline = []
    result = _run_cmd(
        ["gh", "api", f"/repos/{ctx.org}/{ctx.repo}/pulls/{pr_number}/comments"],
        logger=logger,
        timeout=30,
    )
    if result.returncode == 0:
        try:
            inline = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

    return {"reviews": reviews, "inline": inline}


def _candidate_prs_for_issue(
    prs: list[dict],
    number: int,
    branch_prefix: str,
) -> list[dict]:
    """Open PRs that plausibly belong to this issue, most recently updated first.

    Pure — no network. `IssueContext.pr_url`/`existing_branch` are only
    populated when *this* daemon process ran the dev worker that opened the
    PR; a restart, a human-labelled issue, or a PR opened by the sibling
    toolchain's dev agent all leave them unset, so the review worker must be
    able to relocate the PR from the issue number alone.

    Matches on the `ac/issue-<n>-` branch prefix `sanitize_branch_name` uses
    (the trailing hyphen matters — it stops issue 7 from matching issue 70),
    or a closing keyword referencing `#<n>` in the PR body, since a PR opened
    by the other toolchain won't use our branch naming at all.
    """
    closes_re = re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#{number}\b",
        re.IGNORECASE,
    )
    matches = [
        pr for pr in prs
        if (pr.get("headRefName") or "").startswith(branch_prefix)
        or closes_re.search(pr.get("body") or "")
    ]
    return sorted(matches, key=lambda p: p.get("updatedAt", ""), reverse=True)


def _find_pr_for_issue(ctx: IssueContext, logger: WorkerLogger) -> dict | None:
    """Look up the open PR for this issue via `gh`, when ctx carries none.

    Returns None (not an exception) when no open PR matches — the caller
    treats that as "cannot review yet", not a crash.
    """
    result = _run_cmd(
        [
            "gh", "pr", "list",
            "--repo", f"{ctx.org}/{ctx.repo}",
            "--state", "open",
            "--json", "number,headRefName,url,body,title,updatedAt",
        ],
        logger=logger,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warn(f"Failed to list PRs for review lookup: {result.stderr.strip()}")
        return None
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warn("Failed to parse `gh pr list` output during review lookup")
        return None

    branch_prefix = f"ac/issue-{ctx.number}-"
    candidates = _candidate_prs_for_issue(prs, ctx.number, branch_prefix)
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warn(
            f"Multiple open PRs match issue #{ctx.number} — picking the most "
            f"recently updated (#{candidates[0].get('number')})"
        )
    return candidates[0]


def _cleanup_worktree(
    repo_dir: Path,
    worktree_dir: Path,
    branch: str,
    logger: WorkerLogger,
) -> None:
    """Remove a stale worktree and its local branch."""
    if worktree_dir.exists():
        logger.warn("Worktree already exists — removing stale worktree")
        _run_cmd(
            ["git", "worktree", "remove", str(worktree_dir), "--force"],
            cwd=repo_dir,
            logger=logger,
        )
        if worktree_dir.exists():
            logger.warn("Directory still exists after git worktree remove — deleting manually")
            shutil.rmtree(worktree_dir, ignore_errors=True)

    _run_cmd(["git", "worktree", "prune"], cwd=repo_dir, logger=logger)
    _run_cmd(["git", "branch", "-D", branch], cwd=repo_dir, logger=logger)


# `_run_cmd`'s 120s default is sized for git commands that talk to a remote.
# Deleting a worktree is I/O against tens of thousands of node_modules files on
# a Windows filesystem, and in #215 it needed longer — the delete finished, git
# just had not exited yet. Cleanup runs after the PR is open, so nothing waits
# on it; letting it take the time it needs is strictly better than leaking a
# worktree that the *next* run has to remove under a non-best-effort path.
CLEANUP_TIMEOUT_SECONDS = 600


def _cleanup_worktree_best_effort(
    repo_dir: Path,
    worktree_dir: Path,
    logger: WorkerLogger | None = None,
) -> None:
    """Remove a finished worktree. Never raises.

    Runs *after* the PR exists, so the run's real work is already done and
    cannot be undone. This used to be inlined in `run_dev_worker`, where a
    `git worktree remove` that exceeded `_run_cmd`'s 120s timeout propagated
    into `except Exception` — which posts a crash comment and rolls the issue
    back from ac-dev-review to ac-dev-ready + ac-attempt-1, re-queueing a
    finished run for a full paid re-implementation on top of the live PR
    (field_admin#215: a 40-minute, $6.72, 55-turn run discarded by an rmdir).

    A leftover worktree is the cheap failure by comparison — `_cleanup_worktree`
    removes a stale one at the start of the next run.

    Each step is guarded separately: `prune` is what unregisters a worktree
    whose directory is already gone, which is exactly the state `remove` leaves
    when it finishes deleting but is killed before it exits. Letting a failed
    `remove` skip `prune` would leave a phantom registration that breaks the
    next `worktree add`.
    """
    def _step(label: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            if logger:
                logger.warn(
                    f"Worktree cleanup ({label}) failed, leaving it for the "
                    f"next run: {exc}"
                )

    _step("remove", lambda: _run_cmd(
        ["git", "worktree", "remove", str(worktree_dir), "--force"],
        cwd=repo_dir,
        logger=logger,
        timeout=CLEANUP_TIMEOUT_SECONDS,
    ))
    _step("rmtree", lambda: (
        shutil.rmtree(worktree_dir, ignore_errors=True)
        if worktree_dir.exists() else None
    ))
    _step("prune", lambda: _run_cmd(
        ["git", "worktree", "prune"], cwd=repo_dir, logger=logger
    ))


def _next_branch_version(base_branch: str, repo_dir: Path, logger: WorkerLogger) -> int:
    """Scan remote branches to find the next available -vN suffix."""
    result = _run_cmd(
        ["git", "ls-remote", "--heads", "origin", f"{base_branch}*"],
        cwd=repo_dir,
        logger=logger,
    )
    max_v = 1
    for line in (result.stdout or "").strip().splitlines():
        ref = line.split("\t")[-1]
        m = re.search(r"-v(\d+)$", ref)
        if m:
            max_v = max(max_v, int(m.group(1)))
    return max_v + 1


def _setup_fresh_rework_branch(
    ctx: IssueContext,
    repo_dir: Path,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> tuple[str, bool]:
    """Create a versioned fresh branch (e.g. ac/issue-25-...-v2) from base_branch.

    Returns (branch_name, is_fresh_branch=True).
    """
    base_branch_name = sanitize_branch_name(ctx.title, ctx.number)
    version = _next_branch_version(base_branch_name, repo_dir, logger)
    new_branch = f"{base_branch_name}-v{version}"

    logger.info(f"Creating fresh rework branch: {new_branch}")
    _cleanup_worktree(repo_dir, worktree_dir, new_branch, logger)

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _run_cmd(
        ["git", "worktree", "add", str(worktree_dir), "-b", new_branch],
        cwd=repo_dir,
        logger=logger,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Fresh rework branch creation failed: {result.stderr.strip()}")

    return new_branch, True


def _setup_rework_worktree(
    ctx: IssueContext,
    repo_dir: Path,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> tuple[str, bool]:
    """Set up worktree for a rework cycle by checking out the existing branch.

    Returns (branch_name, is_fresh_branch).
    is_fresh_branch=True means conflict fallback occurred and a new PR will be needed.
    """
    branch = ctx.existing_branch

    # 1. Fetch all remotes (gets reviewer commits too)
    _run_cmd(["git", "fetch", "origin"], cwd=repo_dir, logger=logger)

    # 2. Check if remote branch exists
    result = _run_cmd(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_dir,
        logger=logger,
    )
    if not result.stdout.strip():
        logger.warn(f"Remote branch {branch} not found — creating fresh branch")
        return _setup_fresh_rework_branch(ctx, repo_dir, worktree_dir, logger)

    # 3. Clean up stale worktree / local branch
    _cleanup_worktree(repo_dir, worktree_dir, branch, logger)

    # 4. Create worktree from the remote branch
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _run_cmd(
        ["git", "worktree", "add", str(worktree_dir), "-b", branch,
         f"origin/{branch}"],
        cwd=repo_dir,
        logger=logger,
    )
    if result.returncode != 0:
        logger.warn(f"Worktree creation from remote failed: {result.stderr.strip()}")
        return _setup_fresh_rework_branch(ctx, repo_dir, worktree_dir, logger)

    # 5. Try merging base branch to stay up to date
    result = _run_cmd(
        ["git", "merge", f"origin/{ctx.base_branch}", "--no-edit"],
        cwd=worktree_dir,
        logger=logger,
    )
    if result.returncode != 0:
        # Merge conflict — abort and fall back to fresh versioned branch
        _run_cmd(["git", "merge", "--abort"], cwd=worktree_dir, logger=logger)
        logger.warn("Merge conflict with base — falling back to fresh versioned branch")
        _run_cmd(
            ["git", "worktree", "remove", str(worktree_dir), "--force"],
            cwd=repo_dir,
            logger=logger,
        )
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        _run_cmd(["git", "worktree", "prune"], cwd=repo_dir, logger=logger)
        return _setup_fresh_rework_branch(ctx, repo_dir, worktree_dir, logger)

    logger.info(f"Rework worktree ready on branch {branch}")
    return branch, False


def _push_rework(
    ctx: IssueContext,
    branch: str,
    worktree_dir: Path,
    summary: str,
    logger: WorkerLogger,
) -> str:
    """Commit and push rework changes to the existing branch. Returns pr_url."""
    # `branch` here came from local state or a PR's headRefName — not from us.
    # Fail before staging anything.
    assert_pushable(branch, ctx.base_branch)
    _assert_lease_held(ctx, logger)

    # Stage and commit only if there are uncommitted changes
    # (Claude may have already committed)
    status_result = _run_cmd(
        ["git", "status", "--porcelain"],
        cwd=worktree_dir,
        logger=logger,
    )
    if status_result.stdout.strip():
        _run_cmd(["git", "add", "-A"], cwd=worktree_dir, logger=logger)
        commit_msg = f"rework: address review feedback (#{ctx.number})"
        result = _run_cmd(
            ["git", "commit", "-m", commit_msg],
            cwd=worktree_dir,
            logger=logger,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Commit failed: {result.stderr.strip()}")
    else:
        logger.info("Claude already committed — skipping commit step")

    # Push to existing branch (PR auto-updates)
    logger.info(f"Pushing rework to branch {branch}...")
    result = _run_cmd(
        ["git", "push", "origin", branch],
        cwd=worktree_dir,
        logger=logger,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Push failed: {result.stderr.strip()}")

    # Comment on the issue
    comment = redact(
        f"Rework pushed — addressed review feedback.\n\n"
        f"{summary}\n\n"
        f"PR: {ctx.pr_url}"
    )
    _run_cmd(
        [
            "gh", "issue", "comment", str(ctx.number),
            "--repo", f"{ctx.org}/{ctx.repo}",
            "--body", comment,
        ],
        logger=logger,
        timeout=30,
    )

    # Hand off to review — this must not strip ac-rework, the kind hint.
    _success_labels(ctx, logger)

    logger.info(f"Rework pushed to existing PR: {ctx.pr_url}")
    return ctx.pr_url


def _labels_for_claim(labels: list[str]) -> tuple[list[str], list[str]]:
    """Add/remove to move an issue from ac-dev-ready into ac-in-progress.

    Pure — `stages.transition` already removes every stale stage label and
    leaves kind hints (ac-fix, ac-implement, ...) and control labels alone.
    """
    return stages.transition(labels, "ac-in-progress")


def _labels_for_success(labels: list[str]) -> tuple[list[str], list[str]]:
    """Add/remove for a successful run: hand off to the review agent.

    auto-claude does not review its own work, so success lands on
    ac-dev-review, not ac-done — plus ac-pr-created so the PR is discoverable.
    """
    add, remove = stages.transition(labels, "ac-dev-review")
    return add + ["ac-pr-created"], remove


def _labels_for_failure(labels: list[str]) -> tuple[list[str], list[str], bool]:
    """Add/remove for a failed run, plus whether attempts are now exhausted.

    Bumps ac-attempt-N and returns to ac-dev-ready for a retry, unless the
    bump exhausts MAX_ATTEMPTS, in which case the issue is blocked instead
    (terminal — the caller must not re-queue it).
    """
    labels = list(labels)
    current = stages.attempt_of(labels)
    next_label = stages.attempt_label(current + 1)
    bumped = [lbl for lbl in labels if lbl != stages.attempt_label(current)] + [next_label]
    blocked = stages.attempts_exhausted(bumped)

    target = "ac-blocked" if blocked else "ac-dev-ready"
    add, remove = stages.transition(labels, target)
    add = add + [next_label]
    if current > 0:
        remove = remove + [stages.attempt_label(current)]
    return add, remove, blocked


def _get_issue_labels(ctx: IssueContext, logger: WorkerLogger) -> list[str]:
    """Fetch the issue's current labels from GitHub.

    The worker does not carry live labels in IssueContext — they would go
    stale the moment another actor (a human, the loop) edits the issue — so
    every stage transition re-reads before computing add/remove.
    """
    result = _run_cmd(
        ["gh", "api", f"/repos/{ctx.org}/{ctx.repo}/issues/{ctx.number}"],
        logger=logger,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warn(f"Failed to fetch labels: {result.stderr.strip()}")
        return []
    try:
        data = json.loads(result.stdout)
        return [lbl.get("name", "") for lbl in data.get("labels", [])]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


def _telemetry(
    ctx: IssueContext,
    logger: WorkerLogger,
    action: str,
    labels: list[str] | None = None,
    stage: str = "dev",
    **extra,
) -> None:
    """Report one pipeline event to the shared metrics DB.

    `stage` defaults to "dev" for the dev worker's call sites; the review
    worker passes stage="review" instead of duplicating this function.

    Best-effort by construction: `integrations.log_event` swallows every
    failure, and the underlying script exits 0 even when the DB is unreachable.
    Telemetry must never be able to fail a worker that did real work.
    """
    log_event(
        TelemetryEvent(
            project=ctx.pipeline_project or ctx.repo,
            issue=ctx.number,
            stage=stage,
            action=action,
            attempt=stages.attempt_of(labels) if labels else None,
            **extra,
        ),
        ctx.claude_tools_root,
        log=logger.warn,
    )


def _claim_labels(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Transition the issue to ac-in-progress at worker start."""
    labels = _get_issue_labels(ctx, logger)
    add, remove = _labels_for_claim(labels)
    _set_labels(ctx, logger, add=add, remove=remove)
    _telemetry(ctx, logger, "picked_up", labels)


def _success_labels(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Transition the issue to ac-dev-review + ac-pr-created after a PR lands."""
    labels = _get_issue_labels(ctx, logger)
    add, remove = _labels_for_success(labels)
    _set_labels(ctx, logger, add=add, remove=remove)
    _telemetry(ctx, logger, "pr_opened", labels, pr=_pr_number(ctx.pr_url))


def _failure_labels(ctx: IssueContext, logger: WorkerLogger) -> bool:
    """Transition the issue back to ac-dev-ready (or ac-blocked). Returns blocked."""
    labels = _get_issue_labels(ctx, logger)
    add, remove, blocked = _labels_for_failure(labels)
    _set_labels(ctx, logger, add=add, remove=remove)
    # The attempt counter is bumped by the transition, so report the new value
    # rather than the stale one we read.
    _telemetry(
        ctx, logger,
        "blocked" if blocked else "review_fail",
        add,
    )
    return blocked


def _labels_for_review_claim(labels: list[str]) -> tuple[list[str], list[str]]:
    """Add/remove to move an issue from ac-dev-review into ac-review-in-progress.

    The review worker's self-lock — mirrors `_labels_for_claim` and must run
    before any expensive work so a concurrent runner cannot double-claim.
    """
    return stages.transition(labels, "ac-review-in-progress")


def _labels_for_review_pass(labels: list[str]) -> tuple[list[str], list[str]]:
    """Add/remove for an approving review: hand off to the human HITL gate."""
    return stages.transition(labels, "ac-hitl")


def _labels_for_review_fail(labels: list[str]) -> tuple[list[str], list[str], bool]:
    """Add/remove for a failed review, plus whether attempts are now exhausted.

    The bump-and-target logic (ac-attempt-N, then ac-dev-ready for a dev retry
    or ac-blocked once MAX_ATTEMPTS is exhausted) is identical to a failed dev
    run, and `stages.transition` removes every stage label present regardless
    of which one it is — so this delegates to `_labels_for_failure` rather than
    duplicating it. Named separately because the review worker's callers (and
    tests) reason about it as a distinct outcome, per loop-review-agent.md
    Step 10.
    """
    return _labels_for_failure(labels)


def _labels_for_review_crash(labels: list[str]) -> tuple[list[str], list[str]]:
    """Add/remove to release the self-lock after a crash mid-review.

    A crash is an infra failure, not a review verdict, so it must not consume
    an attempt. This rewinds to ac-dev-review — the same target
    `stages.STALE_RESET["ac-review-in-progress"]` uses for an abandoned lock —
    so a review is simply retried, not counted as a failed attempt.
    """
    return stages.transition(labels, stages.REVIEW_TRIGGER)


def _pr_number(pr_url: str | None) -> int | None:
    """Extract the PR number from a github.com/.../pull/N URL."""
    if not pr_url:
        return None
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _claim_review_labels(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Transition the issue to ac-review-in-progress at review-worker start.

    Called before any expensive work (clone, checkout, verify/test, Claude) so
    a concurrent runner cannot double-claim the same review.
    """
    labels = _get_issue_labels(ctx, logger)
    add, remove = _labels_for_review_claim(labels)
    _set_labels(ctx, logger, add=add, remove=remove)


def _review_pass_labels(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Transition the issue to ac-hitl after an approving review."""
    labels = _get_issue_labels(ctx, logger)
    add, remove = _labels_for_review_pass(labels)
    _set_labels(ctx, logger, add=add, remove=remove)
    _telemetry(ctx, logger, "review_pass", labels, stage="review", pr=_pr_number(ctx.pr_url))


def _review_fail_labels(ctx: IssueContext, logger: WorkerLogger) -> bool:
    """Transition the issue back to ac-dev-ready (or ac-blocked). Returns blocked."""
    labels = _get_issue_labels(ctx, logger)
    add, remove, blocked = _labels_for_review_fail(labels)
    _set_labels(ctx, logger, add=add, remove=remove)
    # The attempt counter is bumped by the transition, so report the new value
    # rather than the stale one we read.
    _telemetry(
        ctx, logger,
        "blocked" if blocked else "review_fail",
        add,
        stage="review",
        pr=_pr_number(ctx.pr_url),
    )
    return blocked


def _release_review_lock_after_crash(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Release the self-lock after a crash, without consuming an attempt."""
    labels = _get_issue_labels(ctx, logger)
    add, remove = _labels_for_review_crash(labels)
    _set_labels(ctx, logger, add=add, remove=remove)


def _set_labels(
    ctx: IssueContext,
    logger: WorkerLogger,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Add and/or remove labels on the issue in a single gh call.

    The chokepoint for every `ac-*` label write - `_assert_lease_held` runs
    immediately before the `gh issue edit` call, not at the top of the
    function, so a no-op call (nothing to add or remove) never pays for a
    lease check it does not need.
    """
    args = [
        "gh", "issue", "edit", str(ctx.number),
        "--repo", f"{ctx.org}/{ctx.repo}",
    ]
    if add:
        args += ["--add-label", ",".join(add)]
    if remove:
        args += ["--remove-label", ",".join(remove)]

    if add or remove:
        _assert_lease_held(ctx, logger)
        desc = []
        if remove:
            desc.append(f"-{','.join(remove)}")
        if add:
            desc.append(f"+{','.join(add)}")
        logger.info(f"Labels: {' '.join(desc)}")
        _run_cmd(args, logger=logger, timeout=30)


def _clone_or_fetch(ctx: IssueContext, logger: WorkerLogger) -> Path:
    """Clone repo if missing, otherwise fetch + reset to base_branch."""
    repo_dir = ctx.repos_dir / ctx.repo

    if not repo_dir.exists():
        logger.info(f"Cloning {ctx.org}/{ctx.repo}...")
        result = _run_cmd(
            ["gh", "repo", "clone", f"{ctx.org}/{ctx.repo}", str(repo_dir)],
            timeout=120,
            logger=logger,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Clone failed: {result.stderr.strip()}")
        _run_cmd(["git", "fetch", "origin"], cwd=repo_dir, logger=logger)
        _run_cmd(["git", "checkout", ctx.base_branch], cwd=repo_dir, logger=logger)
    else:
        logger.info(f"Fetching latest for {ctx.repo}...")
        _run_cmd(["git", "fetch", "origin"], cwd=repo_dir, logger=logger)
        _run_cmd(["git", "checkout", ctx.base_branch], cwd=repo_dir, logger=logger)
        _run_cmd(["git", "pull", "--ff-only"], cwd=repo_dir, logger=logger)

    return repo_dir


def _get_issue_comments(ctx: IssueContext, logger: WorkerLogger) -> list[dict]:
    """Fetch issue comments via gh CLI."""
    result = _run_cmd(
        [
            "gh", "api",
            f"/repos/{ctx.org}/{ctx.repo}/issues/{ctx.number}/comments",
        ],
        logger=logger,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warn(f"Failed to fetch comments: {result.stderr.strip()}")
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _push_and_pr(
    ctx: IssueContext,
    branch: str,
    worktree_dir: Path,
    summary: str,
    logger: WorkerLogger,
    pr_body: str | None = None,
) -> str:
    """Commit, push, create PR, comment on issue. Returns PR URL.

    `pr_body` is the assembled multi-section body from `_pr_body`. It stays
    optional so callers that only have a summary still open a valid PR.
    """
    assert_pushable(branch, ctx.base_branch)
    _assert_lease_held(ctx, logger)

    # Stage and commit only if there are uncommitted changes
    # (Claude may have already committed)
    status_result = _run_cmd(
        ["git", "status", "--porcelain"],
        cwd=worktree_dir,
        logger=logger,
    )
    if status_result.stdout.strip():
        _run_cmd(["git", "add", "-A"], cwd=worktree_dir, logger=logger)
        commit_msg = f"{ctx.action}: {ctx.title} (#{ctx.number})"
        result = _run_cmd(
            ["git", "commit", "-m", commit_msg],
            cwd=worktree_dir,
            logger=logger,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Commit failed: {result.stderr.strip()}")
    else:
        logger.info("Claude already committed — skipping commit step")

    # Push
    logger.info(f"Pushing branch {branch}...")
    result = _run_cmd(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree_dir,
        logger=logger,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Push failed: {result.stderr.strip()}")

    _assert_lease_held(ctx, logger)

    # Create PR. `_pr_body` redacts each section; the summary-only fallback
    # redacts here for the same reason — Claude's output is untrusted text.
    if pr_body is None:
        pr_body = _pr_body(
            summary=summary, changes="", testing="", notes="",
            number=ctx.number,
        )
    pr_title = f"{ctx.action}: {ctx.title} (#{ctx.number})"
    logger.info("Creating pull request...")
    result = _run_cmd(
        [
            "gh", "pr", "create",
            "--repo", f"{ctx.org}/{ctx.repo}",
            "--title", pr_title,
            "--body", pr_body,
            "--head", branch,
            "--base", ctx.base_branch,
        ],
        logger=logger,
        timeout=30,
    )
    if result.returncode != 0:
        # PR may already exist — try to extract URL from error
        logger.warn(f"PR create returned {result.returncode}: {result.stderr.strip()}")
        return result.stderr.strip()

    pr_url = result.stdout.strip()
    logger.info(f"PR created: {pr_url}")

    # Comment on the issue
    _run_cmd(
        [
            "gh", "issue", "comment", str(ctx.number),
            "--repo", f"{ctx.org}/{ctx.repo}",
            "--body", f"PR created: {pr_url}",
        ],
        logger=logger,
        timeout=30,
    )

    # Hand off to the review agent — auto-claude does not review its own work.
    _success_labels(ctx, logger)

    return pr_url


# ---------------------------------------------------------------------------
# Dev Worker (fix / implement / test / rework)
# ---------------------------------------------------------------------------

def run_dev_worker(
    ctx: IssueContext,
    log_queue: Queue,
    state_queue: Queue,
    abort_event: Event,
) -> None:
    """Worker process entry point for dev actions (fix, implement, test, rework).

    Full lifecycle: clone → worktree → Claude dev → check changes → push → PR → cleanup.
    For rework: reuses the existing branch and skips PR creation.
    """
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()

    is_rework = ctx.action == "rework" and ctx.existing_branch
    logger.info(f"Dev worker started (PID {pid}) — action={ctx.action}"
                + (f" [rework #{ctx.rework_count}]" if is_rework else ""))

    # Opened here so it is in scope for every exit path below, including the
    # generic exception handler if a crash happens before Claude ever runs.
    run_id = uuid.uuid4().hex
    metrics = RunMetrics()
    returncode: int | None = None
    pending_summaries: list[dict] = []

    # Signal that we're in progress + claim the stage label as a distributed
    # lock. This never touches the kind hint (ac-fix, ac-implement, ...).
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
        run_id=run_id,
        run_mode="dev",
        run_model=ctx.dev_model,
    ))

    branch = sanitize_branch_name(ctx.title, ctx.number)
    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"
    is_fresh_branch = False  # True if rework fell back to a new versioned branch
    # Seeded here, not at the push, so the exception handler can always report
    # it. Rework arrives with the PR it is reworking already on ctx; fresh work
    # starts at None and picks up a URL in [5-6].
    pr_url = ctx.pr_url

    try:
        # [0] Self-lock. Inside the try so a lease lost between spawn and
        # here is fenced, not an unhandled crash.
        _claim_labels(ctx, logger)

        # [1] Clone / fetch
        repo_dir = _clone_or_fetch(ctx, logger)

        if abort_event.is_set():
            logger.warn("Abort — exiting after clone")
            return

        # [2] Create worktree
        if is_rework:
            logger.info(f"Setting up rework worktree from branch {ctx.existing_branch}...")
            branch, is_fresh_branch = _setup_rework_worktree(
                ctx, repo_dir, worktree_dir, logger,
            )
        else:
            logger.info(f"Creating worktree at {worktree_dir}...")
            _cleanup_worktree(repo_dir, worktree_dir, branch, logger)

            worktree_dir.parent.mkdir(parents=True, exist_ok=True)
            result = _run_cmd(
                ["git", "worktree", "add", str(worktree_dir), "-b", branch],
                cwd=repo_dir,
                logger=logger,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Worktree creation failed: {result.stderr.strip()}")

        if abort_event.is_set():
            logger.warn("Abort — exiting after worktree")
            return

        # [3] Build prompt and run Claude
        comments = _get_issue_comments(ctx, logger)
        review_comments = _get_pr_reviews(ctx, logger) if is_rework else None
        prompt = _build_prompt(ctx, comments, review_comments)

        returncode, output, budget_exceeded, rate_limit, metrics = _run_claude(
            prompt=prompt,
            cwd=worktree_dir,
            ctx=ctx,
            logger=logger,
            abort_event=abort_event,
            bypass_permissions=True,
        )

        if abort_event.is_set():
            logger.warn("Abort — exiting after Claude")
            return

        # [3a] Handle rate limiting — preserve work, re-queue, do NOT bill a
        # continuation. Skip the handoff summary: it calls Claude, which is
        # exactly what is currently refusing to serve us.
        if rate_limit is not None:
            logger.warn("Rate limited — pushing partial work and re-queueing")
            partial_pr = _push_partial_work(ctx, branch, worktree_dir, logger)

            # Best-effort: the partial work is already pushed, and a rate limit
            # must not consume an attempt (see process_manager's re-queue). A
            # raising cleanup here would land in `except Exception` and bill one.
            _cleanup_worktree_best_effort(repo_dir, worktree_dir, logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="failed",
                error="rate_limited",
                branch=branch,
                pr_url=partial_pr or ctx.pr_url,
                rate_limited_until=_rate_limited_until(rate_limit),
                run_id=run_id,
                run_outcome="failed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
            ))
            return

        # [3b] Handle budget exhaustion — graceful handoff
        if budget_exceeded:
            logger.warn("Budget exceeded — running handoff summary")
            handoff = _run_handoff_summary(ctx, worktree_dir, output, logger, abort_event)

            # Push any partial work so the next agent can pick it up
            partial_pr = _push_partial_work(ctx, branch, worktree_dir, logger)

            # Cleanup worktree. Best-effort: the handoff summary and partial
            # push are already done, and a raising cleanup would discard both
            # by routing into `except Exception`.
            _cleanup_worktree_best_effort(repo_dir, worktree_dir, logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="failed",
                error="budget_exceeded",
                branch=branch,
                pr_url=partial_pr or ctx.pr_url,
                handoff_summary=handoff,
                run_id=run_id,
                run_outcome="failed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
            ))
            return

        if returncode != 0:
            raise RuntimeError(f"Claude exited with code {returncode}")

        # [4] Check for changes — either uncommitted or already committed by Claude
        has_uncommitted = False
        status_result = _run_cmd(
            ["git", "status", "--porcelain"],
            cwd=worktree_dir,
            logger=logger,
        )
        if status_result.stdout.strip():
            has_uncommitted = True

        # Also check if Claude made commits on this branch
        has_commits = False
        base_ref = f"origin/{ctx.base_branch}"
        log_result = _run_cmd(
            ["git", "log", f"{base_ref}..HEAD", "--oneline"],
            cwd=worktree_dir,
            logger=logger,
        )
        if log_result.stdout.strip():
            has_commits = True

        if not has_uncommitted and not has_commits:
            raise RuntimeError("No changes produced by Claude")

        logger.info("Changes detected — validating before push")

        # [4b] Gate the push on the repo's own verify/test commands. Without
        # this the first thing that ever compiles the code is the review
        # worker, one full dev cycle and one attempt later — and its failure
        # comment blames the implementation for what a 60-second typecheck
        # would have caught here.
        checks_ok, checks_transcript = _prepare_and_check(ctx, worktree_dir, logger)

        if not checks_ok:
            logger.warn("Checks failed — giving the agent one repair round")
            repair_rc, repair_output, _budget, _rl, repair_metrics = _run_claude(
                prompt=_build_repair_prompt(checks_transcript),
                cwd=worktree_dir,
                ctx=ctx,
                logger=logger,
                abort_event=abort_event,
                bypass_permissions=True,
            )
            # A repair round is the same billed task continuing, not a
            # separate job — fold its cost/turns/duration into the primary
            # run's totals rather than discarding them.
            metrics = _accumulate_metrics(metrics, repair_metrics)
            if repair_rc == 0:
                output += "\n" + repair_output
            checks_ok, checks_transcript = _prepare_and_check(ctx, worktree_dir, logger)

        if not checks_ok:
            # Nothing broken gets pushed. The transcript goes to the issue so
            # the next attempt starts from the actual failure.
            logger.error("Checks still failing after repair — refusing to push")
            report_body, report_url = _post_issue_report(
                ctx,
                output=output,
                summary="",
                branch=branch,
                pr_url=None,
                outcome="failed",
                logger=logger,
                notes_override=(
                    "Verify/test commands failed and a repair round did not fix "
                    f"them. Nothing was pushed.\n\n```\n{checks_transcript[-3000:]}\n```"
                ),
            )
            pending_summaries.append(
                {"kind": "dev", "body": report_body, "comment_url": report_url}
            )
            raise RuntimeError("Verify/test checks failed — not pushing")

        logger.info("Checks passed — committing and pushing")

        # [5-6] Push and create PR (or push to existing branch for rework)
        summary = _extract_summary(output)
        if not summary:
            summary = f"Automated {ctx.action} for issue #{ctx.number}"

        # The PR is what a human reviews, so it carries the agent's own
        # file-by-file changes and hand-testing steps — not just the summary.
        pr_body = _pr_body(
            summary=summary,
            changes=_extract_block(output, "CHANGES"),
            testing=_extract_block(output, "TESTING_STEPS"),
            notes=_extract_block(output, "IMPLEMENTATION_NOTES"),
            number=ctx.number,
        )

        if is_rework and not is_fresh_branch:
            # Rework on same branch — push only, PR auto-updates
            pr_url = _push_rework(ctx, branch, worktree_dir, summary, logger)
        else:
            # Fresh work or conflict fallback — create new PR
            pr_url = _push_and_pr(
                ctx, branch, worktree_dir, summary, logger, pr_body=pr_body,
            )

        # [7] Cleanup worktree. Best-effort by design: the PR is already open,
        # so a failure here must not reach the `except Exception` below and
        # undo a finished run. See the helper's docstring.
        logger.info("Cleaning up worktree...")
        _cleanup_worktree_best_effort(repo_dir, worktree_dir, logger)

        # [8] Write the record back to the issue. The issue is the pipeline's
        # source of truth for anyone reading it later, and it previously
        # received only "PR created: <url>" — everything the agent decided
        # lived in a log nobody opens.
        report_body, report_url = _post_issue_report(
            ctx,
            output=output,
            summary=summary,
            branch=branch,
            pr_url=pr_url,
            outcome="success",
            logger=logger,
        )
        pending_summaries.append(
            {"kind": "dev", "body": report_body, "comment_url": report_url}
        )

        # Success
        logger.info(f"Completed successfully — PR: {pr_url}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="completed",
            branch=branch,
            pr_url=pr_url,
            run_id=run_id,
            run_outcome="completed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            summaries=pending_summaries or None,
        ))

    except LeaseLostError as exc:
        _handle_lease_lost(ctx, logger, state_queue, exc, branch=branch,
                            run_id=run_id, metrics=metrics,
                            summaries=pending_summaries)

    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Worker failed: {exc}")

        # Write crash log and post comment
        log_path = _write_crash_log(ctx, error_detail, logger)
        crash_url, crash_body = _post_crash_comment(ctx, str(exc), log_path, logger)
        pending_summaries.append({"kind": "crash", "body": crash_body, "comment_url": crash_url})

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
            # A crash after [5-6] leaves a live branch and PR behind. Dropping
            # them here strands the work: `poller`'s rework branch needs both
            # to resume on the existing branch, and without them the next
            # attempt starts a fresh one on top of the open PR.
            branch=branch,
            pr_url=pr_url,
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            crash_log_path=str(log_path) if log_path else None,
            summaries=pending_summaries or None,
        ))

        # Bump the attempt counter and return to ac-dev-ready — or, on the
        # third failure, land on the terminal ac-blocked so it is not re-queued.
        try:
            blocked = _failure_labels(ctx, logger)
            if blocked:
                logger.warn("Attempts exhausted — issue moved to ac-blocked")
        except Exception:
            pass

        # Try to clean up worktree on failure
        try:
            repo_dir = ctx.repos_dir / ctx.repo
            _run_cmd(
                ["git", "worktree", "remove", str(worktree_dir), "--force"],
                cwd=repo_dir,
            )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
            _run_cmd(["git", "worktree", "prune"], cwd=repo_dir)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Review Worker (ac-dev-review -> ac-hitl / ac-dev-ready / ac-blocked)
# ---------------------------------------------------------------------------
#
# Mirrors accelevation-claude-tools' agents/loop-review-agent.md. auto-claude
# reviews its own PRs because that agent scopes itself with
# `gh issue list --assignee @me` and so never sees an issue assigned to the
# bot — see stages.REVIEW_TRIGGER's docstring.

def _setup_review_worktree(
    ctx: IssueContext,
    repo_dir: Path,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> str:
    """Check out the PR branch for review into its own worktree.

    Unlike dev's rework flow, review does not merge base_branch in — it
    reviews the PR exactly as pushed, matching loop-review-agent.md Step 6.
    """
    branch = ctx.existing_branch
    if not branch:
        raise RuntimeError("No PR branch to review (existing_branch is unset)")

    _run_cmd(["git", "fetch", "origin", branch], cwd=repo_dir, logger=logger)

    result = _run_cmd(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_dir, logger=logger,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"Remote branch {branch} not found for review")

    _cleanup_worktree(repo_dir, worktree_dir, branch, logger)

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _run_cmd(
        ["git", "worktree", "add", str(worktree_dir), "-b", branch, f"origin/{branch}"],
        cwd=repo_dir, logger=logger,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Worktree creation for review failed: {result.stderr.strip()}")

    logger.info(f"Review worktree ready on branch {branch}")
    return branch


def _run_pipeline_command(
    command: str,
    cwd: Path,
    logger: WorkerLogger,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Run one pipeline.json verify/test command through the shell.

    These are opaque shell strings from `.claude/pipeline.json` (e.g.
    "npm run typecheck"), not argv lists — so this shells out directly rather
    than going through `_run_cmd`'s list-based interface.
    """
    env = build_env(current_token())
    logger.info(f"$ {command}")
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        encoding="utf-8",
        errors="replace",
    )


def _run_pipeline_checks(
    ctx: IssueContext,
    worktree_dir: Path,
    logger: WorkerLogger,
) -> tuple[bool, str]:
    """Run pipeline.json's verify then test commands. Returns (ok, transcript).

    A repo with no `.claude/pipeline.json` — or one with empty verify/test —
    has nothing to run. That is not a failure; the transcript says so
    explicitly so the review prompt and any FAIL feedback are honest about the
    absence of checks rather than silent about it.
    """
    config = load_pipeline_config(worktree_dir, logger=logger)
    commands = list(config.verify) + list(config.test) if config else []

    if not commands:
        logger.info("No pipeline.json verify/test commands — skipping checks")
        return True, "No verify/test commands configured for this repo — none were run."

    ok = True
    sections: list[str] = []
    for command in commands:
        try:
            result = _run_pipeline_command(command, cwd=worktree_dir, logger=logger)
        except subprocess.TimeoutExpired:
            ok = False
            sections.append(f"$ {command}\n[FAIL, timed out]")
            logger.error(f"Pipeline command timed out: {command}")
            continue

        status = "PASS" if result.returncode == 0 else "FAIL"
        if result.returncode != 0:
            ok = False
            logger.warn(f"Pipeline command failed ({result.returncode}): {command}")
        section = f"$ {command}\n[{status}, exit {result.returncode}]"
        tail = ((result.stdout or "") + (result.stderr or "")).strip()
        if tail:
            section += "\n" + tail[-4000:]
        sections.append(section)

    return ok, "\n\n".join(sections)


def _build_review_prompt(
    ctx: IssueContext,
    comments: list[dict],
    checks_ok: bool,
    checks_transcript: str,
) -> str:
    """Build the review prompt from prompts/review.txt."""
    template = (ctx.prompts_dir / "review.txt").read_text(encoding="utf-8")

    comments_section = ""
    if comments:
        lines = ["Comments:"]
        for c in comments:
            user = c.get("user", {}).get("login", "unknown")
            body = c.get("body", "")
            lines.append(f"  @{user}: {body}")
        comments_section = "\n".join(lines)

    checks_status = "PASS" if checks_ok else "FAIL"
    checks_section = f"[{checks_status}]\n{checks_transcript}"

    return template.format(
        number=ctx.number,
        org=ctx.org,
        repo=ctx.repo,
        title=ctx.title,
        body=ctx.body or "(no body)",
        base_branch=ctx.base_branch,
        pr_url=ctx.pr_url or "(unknown)",
        checks_section=checks_section,
        comments_section=comments_section,
    )


def _post_pr_review(
    ctx: IssueContext,
    logger: WorkerLogger,
    *,
    approve: bool,
    body: str,
) -> str | None:
    """Post an approving or changes-requested review on the PR via gh.

    Returns the URL of the created review, or None if there was no PR number
    to review, the review post failed, or the follow-up lookup below did.
    `gh pr review` prints nothing on success, so the URL is fetched with a
    second call against the REST reviews endpoint rather than guessed at.
    """
    pr_number = _pr_number(ctx.pr_url)
    if pr_number is None:
        logger.warn("No PR number to review — skipping gh pr review")
        return None
    _assert_lease_held(ctx, logger)
    args = [
        "gh", "pr", "review", str(pr_number),
        "--repo", f"{ctx.org}/{ctx.repo}",
        "--approve" if approve else "--request-changes",
        "--body", redact(body),
    ]
    result = _run_cmd(args, logger=logger, timeout=30)
    if result.returncode != 0:
        logger.warn(f"Could not post PR review: {result.stderr.strip()}")
        return None

    # The review itself already landed on GitHub by this point — a failure
    # from here on must degrade comment_url to None, never fail the post
    # that already succeeded. `_run_cmd` does not catch
    # TimeoutExpired/FileNotFoundError, so this call is wrapped explicitly
    # rather than trusting the lookup to be as reliable as the post above.
    try:
        # `--paginate` because a PR with more than one page of reviews (30+)
        # would otherwise have `.[-1]` select the last item of PAGE ONE, not
        # the true most recent review — a wrong URL, which this task's
        # contract treats as worse than a NULL one.
        lookup = _run_cmd(
            [
                "gh", "api", "--paginate",
                f"repos/{ctx.org}/{ctx.repo}/pulls/{pr_number}/reviews",
                "--jq", ".[].html_url",
            ],
            logger=logger,
            timeout=30,
        )
    except Exception as exc:
        logger.warn(f"Could not resolve posted review URL: {exc}")
        return None

    if lookup.returncode != 0:
        return None
    # One URL per line across every page, oldest first — the true last line
    # is the most recently submitted review, unlike `.[-1]` on an
    # unpaginated single page. The startswith guard is applied to that one
    # selected line, not to the whole (possibly multi-line) stdout blob,
    # so a malformed earlier line can never make a later check pass by
    # accident.
    lines = [line for line in lookup.stdout.strip().splitlines() if line.strip()]
    if not lines:
        return None
    url = lines[-1].strip()
    return url if url.startswith("http") else None


def run_review_worker(
    ctx: IssueContext,
    log_queue: Queue,
    state_queue: Queue,
    abort_event: Event,
) -> None:
    """Worker process entry point for reviewing a PR (ac-dev-review stage).

    Full lifecycle: self-lock -> checkout PR branch -> verify/test -> Claude
    review (correctness + security) -> pass (ac-hitl) or fail (ac-dev-ready
    retry, or ac-blocked on the third failure).
    """
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()
    logger.info(f"Review worker started (PID {pid}) — PR {ctx.pr_url}")

    run_id = uuid.uuid4().hex
    metrics = RunMetrics()
    returncode: int | None = None
    pending_summaries: list[dict] = []

    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
        run_id=run_id,
        run_mode="review",
        run_model=ctx.dev_model,
    ))

    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}-review"
    repo_dir = ctx.repos_dir / ctx.repo

    try:
        # [1] Self-lock FIRST, before any expensive work, so a concurrent
        # runner cannot double-claim this review. Inside the try so a lease
        # lost between spawn and here is fenced, not an unhandled crash.
        _claim_review_labels(ctx, logger)

        # `ctx.pr_url`/`existing_branch` are only populated when this same
        # daemon process ran the dev worker that opened the PR — a restart, a
        # human-applied ac-dev-review label, or a PR from the sibling
        # toolchain's dev agent all leave them unset. Relocate the PR from the
        # issue number in that case rather than assuming there's nothing to
        # review; prefer the free/exact values already on ctx when present.
        if not ctx.pr_url or not ctx.existing_branch:
            match = _find_pr_for_issue(ctx, logger)
            if match is None:
                raise RuntimeError(
                    f"No open PR found for issue #{ctx.number} — cannot review"
                )
            ctx.pr_url = match.get("url") or ctx.pr_url
            ctx.existing_branch = match.get("headRefName") or ctx.existing_branch
            logger.info(
                f"Resolved PR from issue lookup: #{match.get('number')} "
                f"({ctx.existing_branch})"
            )

        if not ctx.pr_url or not ctx.existing_branch:
            raise RuntimeError("PR lookup returned an incomplete match — cannot review")

        # [2] Clone/fetch, then checkout the PR branch into a worktree
        repo_dir = _clone_or_fetch(ctx, logger)

        if abort_event.is_set():
            logger.warn("Abort — exiting after clone")
            return

        _setup_review_worktree(ctx, repo_dir, worktree_dir, logger)

        if abort_event.is_set():
            logger.warn("Abort — exiting after worktree")
            return

        # [3] Prepare the worktree, then run verify/test from .claude/pipeline.json
        checks_ok, checks_transcript = _prepare_and_check(ctx, worktree_dir, logger)
        if not checks_ok:
            logger.warn("Verify/test checks failed")

        if abort_event.is_set():
            logger.warn("Abort — exiting after checks")
            return

        # [4] Review the diff against the issue requirements, via Claude
        comments = _get_issue_comments(ctx, logger)
        prompt = _build_review_prompt(ctx, comments, checks_ok, checks_transcript)

        returncode, output, budget_exceeded, rate_limit, metrics = _run_claude(
            prompt=prompt,
            cwd=worktree_dir,
            ctx=ctx,
            logger=logger,
            abort_event=abort_event,
            bypass_permissions=True,
        )

        if abort_event.is_set():
            logger.warn("Abort — exiting after Claude")
            return

        if rate_limit is not None:
            raise RuntimeError("Rate limited while reviewing")
        if budget_exceeded:
            raise RuntimeError("Budget exceeded while reviewing")
        if returncode != 0:
            raise RuntimeError(f"Claude exited with code {returncode}")

        result_text = _extract_result_text(output)
        claude_pass = _parse_review_verdict(result_text)
        # A failing verify/test check is an automatic FAIL — never approve a
        # broken build, regardless of what Claude's verdict says.
        review_pass = claude_pass and checks_ok
        if claude_pass and not checks_ok:
            logger.warn(
                "Claude returned PASS but verify/test checks failed — overriding to FAIL"
            )

        # [5] Cleanup worktree before acting on the outcome. Best-effort: the
        # review verdict is already decided, and a raising cleanup here would
        # skip [6] entirely — no approval, no ac-hitl, no request-changes —
        # and report a crash instead of the review that actually completed.
        _cleanup_worktree_best_effort(repo_dir, worktree_dir, logger)

        # [6] Outcome
        if review_pass:
            logger.info("Review pass — approving and handing off to ac-hitl")
            approve_body = (
                "Agent review: build passes, diff satisfies acceptance criteria, "
                "security pass clean. Handing to human for final test (HITL gate)."
            )
            review_url = _post_pr_review(ctx, logger, approve=True, body=approve_body)
            pending_summaries.append(
                {"kind": "review", "body": approve_body, "comment_url": review_url}
            )
            _review_pass_labels(ctx, logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="completed",
                pr_url=ctx.pr_url,
                run_id=run_id,
                run_outcome="completed",
                exit_code=returncode,
                duration_seconds=metrics.duration_seconds,
                cost_usd=metrics.cost_usd,
                turns=metrics.turns,
                summaries=pending_summaries or None,
            ))
            return

        feedback = _extract_review_feedback(result_text)
        if not checks_ok:
            feedback = f"Verify/test checks failed:\n{checks_transcript}\n\n{feedback}"

        blocked = _review_fail_labels(ctx, logger)
        if blocked:
            logger.warn("Attempts exhausted — issue moved to ac-blocked")
            request_body = (
                "Agent review: circuit breaker — this issue has failed review 3 "
                f"or more times and cannot converge automatically.\n\n{feedback}"
            )
        else:
            logger.info("Review fail — requesting changes and returning to ac-dev-ready")
            request_body = f"Agent review: changes needed.\n\n{feedback}"
        review_url = _post_pr_review(ctx, logger, approve=False, body=request_body)
        pending_summaries.append(
            {"kind": "review", "body": request_body, "comment_url": review_url}
        )

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error="blocked" if blocked else "review_fail",
            pr_url=ctx.pr_url,
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            summaries=pending_summaries or None,
        ))

    except LeaseLostError as exc:
        _handle_lease_lost(ctx, logger, state_queue, exc, run_id=run_id, metrics=metrics,
                            summaries=pending_summaries)

    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Review worker failed: {exc}")

        log_path = _write_crash_log(ctx, error_detail, logger)
        crash_url, crash_body = _post_crash_comment(ctx, str(exc), log_path, logger)
        pending_summaries.append({"kind": "crash", "body": crash_body, "comment_url": crash_url})

        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
            # Same reasoning as the dev worker's handler: the PR under review
            # exists regardless of how the review ended, and every other exit
            # from this function reports it.
            branch=ctx.existing_branch,
            pr_url=ctx.pr_url,
            run_id=run_id,
            run_outcome="failed",
            exit_code=returncode,
            duration_seconds=metrics.duration_seconds,
            cost_usd=metrics.cost_usd,
            turns=metrics.turns,
            crash_log_path=str(log_path) if log_path else None,
            summaries=pending_summaries or None,
        ))

        # Release the self-lock so the issue is not stranded on
        # ac-review-in-progress. A crash is not a review verdict, so this must
        # not consume an attempt — see _labels_for_review_crash.
        try:
            _release_review_lock_after_crash(ctx, logger)
        except Exception:
            pass

        # Try to clean up the worktree on failure
        try:
            _run_cmd(
                ["git", "worktree", "remove", str(worktree_dir), "--force"],
                cwd=repo_dir,
            )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
            _run_cmd(["git", "worktree", "prune"], cwd=repo_dir)
        except Exception:
            pass

