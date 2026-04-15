"""Worker processes — run Claude CLI to develop solutions or produce plans/reviews."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from multiprocessing import Event, Queue
from pathlib import Path

from logger import WorkerLogger


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
    action: str             # "fix", "implement", "test", "plan", "review"
    org: str
    base_branch: str
    # Paths
    repos_dir: Path
    worktrees_dir: Path
    prompts_dir: Path
    # Claude settings
    dev_model: str
    permission_mode: str
    max_budget_usd: float
    # Worker color (for logging)
    color_name: str
    color_code: str


@dataclass
class StateUpdate:
    """Status update sent from worker back to main process via state_queue."""
    issue_id: str
    status: str             # IssueStatus value
    error: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    worker_pid: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    if env_extra:
        env.update(env_extra)

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


def _build_prompt(ctx: IssueContext, comments: list[dict] | None = None) -> str:
    """Build a prompt from the appropriate template file."""
    # Pick template based on action
    template_map = {
        "fix": "develop.txt",
        "implement": "develop.txt",
        "test": "test.txt",
        "plan": "plan.txt",
        "review": "review.txt",
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

    return template.format(
        number=ctx.number,
        org=ctx.org,
        repo=ctx.repo,
        title=ctx.title,
        body=ctx.body or "(no body)",
        action=ctx.action,
        comments_section=comments_section,
    )


def _run_claude(
    prompt: str,
    cwd: Path,
    ctx: IssueContext,
    logger: WorkerLogger,
    abort_event: Event,
    bypass_permissions: bool = True,
) -> tuple[int, str]:
    """Run Claude CLI via Popen, stream output to logger.

    Returns (returncode, captured_output).
    """
    cmd = [
        "claude",
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--model", ctx.dev_model,
        "--no-session-persistence",
    ]
    if bypass_permissions:
        cmd += ["--permission-mode", ctx.permission_mode]
        cmd += ["--max-budget-usd", str(ctx.max_budget_usd)]
    cmd.append(prompt)

    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"

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
    try:
        for line in proc.stdout:
            line = line.rstrip("\n\r")
            if not line:
                continue

            captured_lines.append(line)

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
                return (-1, "")

        proc.wait()
    except Exception as exc:
        logger.error(f"Error reading Claude output: {exc}")
        proc.kill()
        proc.wait()
        return (-1, "")

    stderr_thread.join(timeout=5)
    stderr_output = "\n".join(stderr_lines).strip()
    if proc.returncode != 0:
        logger.error(f"Claude exited with code {proc.returncode}")
        if stderr_output:
            logger.error(f"Claude stderr: {stderr_output}")
        else:
            logger.warn("No stderr output from Claude")

    return (proc.returncode, "\n".join(captured_lines))


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
    """Extract the IMPLEMENTATION_SUMMARY line from Claude's output."""
    for line in output.split("\n"):
        # Check both raw lines and JSON-embedded text
        if "IMPLEMENTATION_SUMMARY:" in line:
            # Raw text match
            idx = line.index("IMPLEMENTATION_SUMMARY:")
            return line[idx + len("IMPLEMENTATION_SUMMARY:"):].strip()
        # Try parsing as JSON to find embedded summary
        try:
            data = json.loads(line)
            result = data.get("result", "")
            if isinstance(result, str) and "IMPLEMENTATION_SUMMARY:" in result:
                idx = result.index("IMPLEMENTATION_SUMMARY:")
                return result[idx + len("IMPLEMENTATION_SUMMARY:"):].strip()
        except (json.JSONDecodeError, TypeError):
            pass
    return ""


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


def _set_labels(
    ctx: IssueContext,
    logger: WorkerLogger,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Add and/or remove labels on the issue in a single gh call."""
    args = [
        "gh", "issue", "edit", str(ctx.number),
        "--repo", f"{ctx.org}/{ctx.repo}",
    ]
    if add:
        args += ["--add-label", ",".join(add)]
    if remove:
        args += ["--remove-label", ",".join(remove)]

    if add or remove:
        desc = []
        if remove:
            desc.append(f"-{','.join(remove)}")
        if add:
            desc.append(f"+{','.join(add)}")
        logger.info(f"Labels: {' '.join(desc)}")
        _run_cmd(args, logger=logger, timeout=30)


def _clone_or_fetch(ctx: IssueContext, logger: WorkerLogger) -> Path:
    """Clone repo if missing, otherwise fetch + reset to main."""
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
) -> str:
    """Commit, push, create PR, comment on issue. Returns PR URL."""
    # Stage all changes
    _run_cmd(["git", "add", "-A"], cwd=worktree_dir, logger=logger)

    # Commit
    commit_msg = f"{ctx.action}: {ctx.title} (#{ctx.number})"
    result = _run_cmd(
        ["git", "commit", "-m", commit_msg],
        cwd=worktree_dir,
        logger=logger,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Commit failed: {result.stderr.strip()}")

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

    # Create PR
    pr_body = f"{summary}\n\nCloses #{ctx.number}" if summary else f"Closes #{ctx.number}"
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

    # Swap in-progress for pr-created so the issue shows as done
    _set_labels(ctx, logger, add=["ac-pr-created"], remove=["ac-in-progress"])

    return pr_url


# ---------------------------------------------------------------------------
# Dev Worker (fix / implement / test)
# ---------------------------------------------------------------------------

def run_dev_worker(
    ctx: IssueContext,
    log_queue: Queue,
    state_queue: Queue,
    abort_event: Event,
) -> None:
    """Worker process entry point for dev actions (fix, implement, test).

    Full lifecycle: clone → worktree → Claude dev → check changes → push → PR → cleanup.
    """
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()

    logger.info(f"Dev worker started (PID {pid}) — action={ctx.action}")

    action_label = f"ac-{ctx.action}"

    # Signal that we're in progress + set GitHub label as distributed lock
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="in_progress",
        worker_pid=pid,
    ))
    _set_labels(ctx, logger, add=["ac-in-progress"], remove=[action_label])

    branch = sanitize_branch_name(ctx.title, ctx.number)
    worktree_dir = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"

    try:
        # [1] Clone / fetch
        repo_dir = _clone_or_fetch(ctx, logger)

        if abort_event.is_set():
            logger.warn("Abort — exiting after clone")
            return

        # [2] Create worktree
        logger.info(f"Creating worktree at {worktree_dir}...")
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        # Clean up stale worktree if it exists
        if worktree_dir.exists():
            logger.warn("Worktree already exists — removing stale worktree")
            _run_cmd(
                ["git", "worktree", "remove", str(worktree_dir), "--force"],
                cwd=repo_dir,
                logger=logger,
            )

        # Remove the branch if it already exists locally (from a previous failed run)
        _run_cmd(
            ["git", "branch", "-D", branch],
            cwd=repo_dir,
            logger=logger,
        )

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
        prompt = _build_prompt(ctx, comments)

        returncode, output = _run_claude(
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

        if returncode != 0:
            raise RuntimeError(f"Claude exited with code {returncode}")

        # [4] Check for changes
        status_result = _run_cmd(
            ["git", "status", "--porcelain"],
            cwd=worktree_dir,
            logger=logger,
        )
        if not status_result.stdout.strip():
            raise RuntimeError("No changes produced by Claude")

        logger.info("Changes detected — committing and pushing")

        # [5-6] Push and create PR
        summary = _extract_summary(output)
        if not summary:
            summary = f"Automated {ctx.action} for issue #{ctx.number}"
        pr_url = _push_and_pr(ctx, branch, worktree_dir, summary, logger)

        # [7] Cleanup worktree
        logger.info("Cleaning up worktree...")
        _run_cmd(
            ["git", "worktree", "remove", str(worktree_dir), "--force"],
            cwd=repo_dir,
            logger=logger,
        )

        # Success
        logger.info(f"Completed successfully — PR: {pr_url}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="completed",
            branch=branch,
            pr_url=pr_url,
        ))

    except Exception as exc:
        logger.error(f"Worker failed: {exc}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
        ))

        # Remove in-progress label on failure
        try:
            _set_labels(ctx, logger, remove=["ac-in-progress"])
        except Exception:
            pass

        # Try to clean up worktree on failure
        try:
            repo_dir = ctx.repos_dir / ctx.repo
            if worktree_dir.exists():
                _run_cmd(
                    ["git", "worktree", "remove", str(worktree_dir), "--force"],
                    cwd=repo_dir,
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Plan Worker (plan / review)
# ---------------------------------------------------------------------------

def run_plan_worker(
    ctx: IssueContext,
    log_queue: Queue,
    state_queue: Queue,
    abort_event: Event,
) -> None:
    """Worker process entry point for plan/review actions.

    Read-only: clone → Claude analysis → post comment → swap labels.
    """
    logger = WorkerLogger(log_queue, ctx.issue_id, ctx.color_name, ctx.color_code, ctx.repo)
    pid = os.getpid()

    logger.info(f"Plan worker started (PID {pid}) — action={ctx.action}")

    action_label = f"ac-{ctx.action}"

    # Signal that we're in progress + set GitHub label as distributed lock
    state_queue.put(StateUpdate(
        issue_id=ctx.issue_id,
        status="planning",
        worker_pid=pid,
    ))
    _set_labels(ctx, logger, add=["ac-in-progress"], remove=[action_label])

    try:
        # [1] Clone / fetch (read-only — no worktree)
        repo_dir = _clone_or_fetch(ctx, logger)

        if abort_event.is_set():
            logger.warn("Abort — exiting after clone")
            return

        # [2] Build prompt and run Claude (no bypassPermissions — read-only)
        comments = _get_issue_comments(ctx, logger)
        prompt = _build_prompt(ctx, comments)

        returncode, output = _run_claude(
            prompt=prompt,
            cwd=repo_dir,
            ctx=ctx,
            logger=logger,
            abort_event=abort_event,
            bypass_permissions=False,
        )

        if abort_event.is_set():
            logger.warn("Abort — exiting after Claude")
            return

        if returncode != 0:
            raise RuntimeError(f"Claude exited with code {returncode}")

        # [3] Extract result text and post as comment
        result_text = _extract_result_text(output)
        if not result_text.strip():
            raise RuntimeError("Claude produced no output for plan/review")

        if ctx.action == "plan":
            heading = "## Implementation Plan"
            footer = "\n\n---\n_To proceed, replace `ac-plan` with `ac-fix` or `ac-implement`._"
            posted_label = "ac-plan-posted"
        else:
            heading = "## Code Review"
            footer = ""
            posted_label = "ac-review-posted"

        comment_body = f"{heading}\n\n{result_text}{footer}"

        logger.info("Posting comment on issue...")
        _run_cmd(
            [
                "gh", "issue", "comment", str(ctx.number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--body", comment_body,
            ],
            logger=logger,
            timeout=30,
        )

        # Swap labels: remove in-progress, add posted label
        _set_labels(ctx, logger, add=[posted_label], remove=["ac-in-progress"])

        # Success
        logger.info(f"Plan/review posted successfully")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="plan_posted",
        ))

    except Exception as exc:
        logger.error(f"Plan worker failed: {exc}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="failed",
            error=str(exc),
        ))

        # Remove in-progress label on failure
        try:
            _set_labels(ctx, logger, remove=["ac-in-progress"])
        except Exception:
            pass
