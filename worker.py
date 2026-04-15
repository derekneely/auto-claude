"""Worker processes — run Claude CLI to develop solutions or produce plans/reviews."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from multiprocessing import Event, Queue
from pathlib import Path

from logger import WorkerLogger
from redact import redact


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
    action: str             # "fix", "implement", "test", "plan", "review", "rework"
    org: str
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
) -> None:
    """Post a concise failure comment on the issue referencing the local crash log."""
    log_ref = f"\n\nCrash log: `{log_path}`" if log_path else ""
    body = redact(
        f"**auto-claude** failed while processing this issue.\n\n"
        f"> {error[:200]}{log_ref}\n\n"
        f"_Re-label to retry after investigating._"
    )
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    try:
        subprocess.run(
            [
                "gh", "issue", "comment", str(ctx.number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--body", body,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
    except Exception as exc:
        logger.error(f"Failed to post crash comment: {exc}")


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

    # Build review section (for rework)
    review_section = ""
    if review_comments:
        review_section = _format_review_section(review_comments)

    format_vars = dict(
        number=ctx.number,
        org=ctx.org,
        repo=ctx.repo,
        title=ctx.title,
        body=ctx.body or "(no body)",
        action=ctx.action,
        comments_section=comments_section,
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
) -> tuple[int, str, bool]:
    """Run Claude CLI via Popen, stream output to logger.

    Returns (returncode, captured_output, budget_exceeded).
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
                return (-1, "", False)

        proc.wait()
    except Exception as exc:
        logger.error(f"Error reading Claude output: {exc}")
        proc.kill()
        proc.wait()
        return (-1, "", False)

    stderr_thread.join(timeout=5)
    stderr_output = "\n".join(stderr_lines).strip()
    budget_exceeded = "Exceeded USD budget" in stderr_output
    if proc.returncode != 0:
        logger.error(f"Claude exited with code {proc.returncode}")
        if budget_exceeded:
            logger.warn("Budget limit reached")
        elif stderr_output:
            logger.error(f"Claude stderr: {stderr_output}")
        else:
            logger.warn("No stderr output from Claude")

    return (proc.returncode, "\n".join(captured_lines), budget_exceeded)


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
    returncode, output, _ = _run_claude(
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

    # Swap labels: remove rework + in-progress, add pr-created
    _set_labels(ctx, logger, add=["ac-pr-created"], remove=["ac-rework", "ac-in-progress"])

    logger.info(f"Rework pushed to existing PR: {ctx.pr_url}")
    return ctx.pr_url


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

    # Create PR — redact summary to avoid leaking secrets from Claude output
    pr_body = f"{redact(summary)}\n\nCloses #{ctx.number}" if summary else f"Closes #{ctx.number}"
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
    is_fresh_branch = False  # True if rework fell back to a new versioned branch

    try:
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

        returncode, output, budget_exceeded = _run_claude(
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

        # [3b] Handle budget exhaustion — graceful handoff
        if budget_exceeded:
            logger.warn("Budget exceeded — running handoff summary")
            handoff = _run_handoff_summary(ctx, worktree_dir, output, logger, abort_event)

            # Push any partial work so the next agent can pick it up
            partial_pr = _push_partial_work(ctx, branch, worktree_dir, logger)

            # Cleanup worktree
            _run_cmd(
                ["git", "worktree", "remove", str(worktree_dir), "--force"],
                cwd=repo_dir, logger=logger,
            )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
            _run_cmd(["git", "worktree", "prune"], cwd=repo_dir, logger=logger)

            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="failed",
                error="budget_exceeded",
                branch=branch,
                pr_url=partial_pr or ctx.pr_url,
                handoff_summary=handoff,
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

        logger.info("Changes detected — committing and pushing")

        # [5-6] Push and create PR (or push to existing branch for rework)
        summary = _extract_summary(output)
        if not summary:
            summary = f"Automated {ctx.action} for issue #{ctx.number}"

        if is_rework and not is_fresh_branch:
            # Rework on same branch — push only, PR auto-updates
            pr_url = _push_rework(ctx, branch, worktree_dir, summary, logger)
        else:
            # Fresh work or conflict fallback — create new PR
            pr_url = _push_and_pr(ctx, branch, worktree_dir, summary, logger)

        # [7] Cleanup worktree
        logger.info("Cleaning up worktree...")
        _run_cmd(
            ["git", "worktree", "remove", str(worktree_dir), "--force"],
            cwd=repo_dir,
            logger=logger,
        )
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        _run_cmd(["git", "worktree", "prune"], cwd=repo_dir, logger=logger)

        # Success
        logger.info(f"Completed successfully — PR: {pr_url}")
        state_queue.put(StateUpdate(
            issue_id=ctx.issue_id,
            status="completed",
            branch=branch,
            pr_url=pr_url,
        ))

    except Exception as exc:
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Worker failed: {exc}")

        # Write crash log and post comment
        log_path = _write_crash_log(ctx, error_detail, logger)
        _post_crash_comment(ctx, str(exc), log_path, logger)

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

        returncode, output, _budget_exceeded = _run_claude(
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

        comment_body = f"{heading}\n\n{redact(result_text)}{footer}"

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
        import traceback
        error_detail = f"{exc}\n\n{traceback.format_exc()}"
        logger.error(f"Plan worker failed: {exc}")

        # Write crash log and post comment
        log_path = _write_crash_log(ctx, error_detail, logger)
        _post_crash_comment(ctx, str(exc), log_path, logger)

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
