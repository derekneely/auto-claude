"""auto-claude — monitors GitHub issues and spawns Claude workers to solve them."""

import argparse
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import stages
from config import load_config
from ghauth import (
    TOKEN_ENV_VAR,
    load_dotenv,
    check_access,
    check_ownership_config,
    format_report,
    has_fatal,
    load_token,
    verify_identity,
)
from github_client import GithubClient, GithubClientError
from integrations import METRICS_DB_ENV_VAR, sync_board
from logger import MainLogger, enable_ansi_windows
from pipeline import PIPELINE_JSON_RELATIVE_PATH
from poller import Poller
from process_manager import ProcessManager
from state import IssueStatus, StateStore
from triage import TriageEngine, format_clarifying_comment

BANNER = r"""
   ___       __           _______             __
  / _ |__ __/ /____  ____/ ___/ /__ ___ _____/ /__
 / __ / // / __/ _ \/___/ /__/ / _ `/ // / _  / -_)
/_/ |_\_,_/\__/\___/    \___/_/\_,_/\_,_/\_,_/\__/

  Accelevation Issue Automation
"""

shutdown_requested = False


def handle_signal(signum: int, frame) -> None:
    global shutdown_requested
    shutdown_requested = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor GitHub issues and spawn Claude workers to solve them.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run poll + triage but do not spawn workers or modify GitHub",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help=(
            "Skip the repo-permission and project-board checks. The identity "
            "and ownership checks always run."
        ),
    )
    parser.add_argument(
        "--issue",
        type=str,
        default=None,
        help="Process a single issue and exit (format: repo#number)",
    )
    return parser.parse_args()


def _report(logger: MainLogger, checks) -> None:
    """Log a block of preflight results at the severity each one carries."""
    for line in format_report(checks).splitlines():
        (logger.error if line.startswith("[FAIL]") else
         logger.warn if line.startswith("[WARN]") else logger.info)(line)


def _abort(logger: MainLogger, message: str) -> None:
    logger.error(message)
    logger.close()
    sys.exit(1)


def create_directories(config) -> None:
    """Create runtime directories if they don't exist."""
    config.paths.repos_dir.mkdir(parents=True, exist_ok=True)
    config.paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
    config.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.paths.log_file.parent.mkdir(parents=True, exist_ok=True)


def _ensure_labels(config, github: GithubClient, logger: MainLogger) -> None:
    """Create any missing ac-* labels on the monitored repos.

    Create-only: an existing label is left exactly as it is, so this never
    repaints labels the sibling toolchain owns. Failures are per-label warnings
    rather than fatal - a repo where we lack label scope should still be polled.
    """
    for repo in config.github.repos:
        created = 0
        for name, color, description in stages.LABEL_SPECS:
            try:
                if github.ensure_label_exists(repo, name, color, description):
                    created += 1
            except GithubClientError as exc:
                logger.warn(f"Could not ensure label {name} on {repo}: {exc}")
        if created:
            logger.info(f"Created {created} missing label(s) on {repo}")


def _release_stale_locks(config, github: GithubClient, logger: MainLogger,
                         dry_run: bool = False) -> int:
    """Rewind locks left behind by a crashed or killed run.

    At startup no worker of ours is alive, so any bot-assigned issue still
    carrying `ac-in-progress` is stale by definition — no timeout needed. This
    matters because the sibling toolchain's stale-lock sweep only covers issues
    assigned to the human running it; nothing else will ever free ours, and a
    stuck lock is invisible (the issue simply stops being picked up).
    """
    released = 0
    for repo in config.github.repos:
        try:
            issues = github.list_issues(repo, assignee=config.github.bot_login)
        except GithubClientError as exc:
            logger.warn(f"Could not check {repo} for stale locks: {exc}")
            continue

        for issue in issues:
            labels = [lbl["name"] for lbl in issue.get("labels", [])]
            target = stages.stale_reset_target(labels)
            if target is None:
                continue

            issue_id = f"{repo}#{issue['number']}"
            if dry_run:
                logger.info(f"DRY-RUN: would release stale lock on {issue_id} -> {target}")
                released += 1
                continue

            add, remove = stages.transition(labels, target)
            try:
                for label in add:
                    github.add_label(repo, issue["number"], label)
                for label in remove:
                    github.remove_label(repo, issue["number"], label)
            except GithubClientError as exc:
                logger.warn(f"Could not release stale lock on {issue_id}: {exc}")
                continue

            logger.warn(f"Released stale lock on {issue_id} -> {target}")
            released += 1

    return released


def _pipeline_json_text(config, github, repo: str, logger: MainLogger) -> str | None:
    """The repo's current `.claude/pipeline.json`, read from GitHub.

    Same rule as `ProcessManager.pipeline_for`: prefer the configured base
    branch over the repo's default branch, since GitHub's contents API defaults
    to the latter and `main` can trail the active integration branch by a
    release cycle. Returns None when no branch carries the file.
    """
    base_branch = getattr(config.github, "base_branch", None)
    refs: list[str | None] = [base_branch, None] if base_branch else [None]

    for ref in refs:
        try:
            text = github.get_file(repo, PIPELINE_JSON_RELATIVE_PATH, ref=ref)
        except Exception as exc:  # noqa: BLE001 - board drift is never fatal
            logger.warn(
                f"board sync: could not read {repo}/{PIPELINE_JSON_RELATIVE_PATH}"
                f"{f'@{ref}' if ref else ''}: {exc}"
            )
            return None
        if text is not None:
            return text
    return None


def _sync_boards(config, github, logger: MainLogger) -> None:
    """Mirror ac-* labels onto the shared Projects v2 board, per repo.

    `project-sync.mjs` reads a literal relative `.claude/pipeline.json` from its
    cwd, so it needs a directory holding that file. That directory is
    deliberately *not* the repo's local checkout: `repos/<repo>` is shared with
    the workers and sits on whatever branch one of them last left it on.
    field_admin's clone sat on `main` from the day it was cloned, and
    `.claude/pipeline.json` exists only on `dev` — so every sync exited 1 until
    a worker incidentally checked out `dev`. Serving the file from GitHub into a
    scratch directory decouples board sync from checkout state entirely, and
    lets a repo that was never cloned still sync its board.

    cwd is the script's *only* dependency on the filesystem — every `gh` call it
    makes either passes `--repo` (which we always supply) or is `gh api graphql`.
    """
    root = config.integrations.claude_tools_root
    if root is None:
        return

    for repo in config.github.repos:
        text = _pipeline_json_text(config, github, repo, logger)
        if text is None:
            # No pipeline.json anywhere means no board to sync. Silent by
            # design: this runs every tick, and the script itself treats a
            # missing projectBoard block as "disabled", not as an error.
            continue

        with tempfile.TemporaryDirectory(prefix="ac-board-sync-") as scratch:
            pipeline_json = Path(scratch) / PIPELINE_JSON_RELATIVE_PATH
            pipeline_json.parent.mkdir(parents=True, exist_ok=True)
            pipeline_json.write_text(text, encoding="utf-8")
            sync_board(
                cwd=Path(scratch),
                claude_tools_root=root,
                repo=f"{config.github.org}/{repo}",
                assignee=config.github.bot_login,
                log=logger.warn,
            )


def _run_triage(record, state, github, triage_engine, config, logger,
                dry_run: bool = False) -> None:
    """Triage a single issue and update state accordingly."""
    # Triage answers "is this issue specified well enough to implement". That
    # question is meaningless for a PR review, and a needs-info verdict would
    # bounce the issue to ac-input-needed and strand an open PR. Queue directly.
    if record.mode == "review":
        # The poller usually lands these on QUEUED itself; --issue mode does
        # not. Transitioning QUEUED -> QUEUED is not a legal move, so only
        # move a record that has not arrived yet.
        if record.status != IssueStatus.QUEUED:
            state.transition(record.issue_id, IssueStatus.QUEUED)
            state.save()
        logger.info(f"{record.issue_id} -> queued for review (triage skipped)")
        return

    logger.info(f"Triaging {record.issue_id}...")
    state.transition(record.issue_id, IssueStatus.TRIAGING)
    state.update(record.issue_id, triage_attempts=record.triage_attempts + 1)
    state.save()

    decision = triage_engine.triage(record)
    logger.info(
        f"Triage {decision.decision.upper()} ({decision.confidence}) — {decision.summary}"
    )

    if decision.decision == "proceed":
        state.transition(record.issue_id, IssueStatus.QUEUED)
        state.save()
        logger.info(f"{record.issue_id} -> {state.get(record.issue_id).status}")
    else:
        state.transition(record.issue_id, IssueStatus.NEEDS_INFO)
        state.save()

        if not dry_run:
            comment = format_clarifying_comment(decision, config)
            try:
                github.post_comment(record.repo, record.number, comment)
                # Move the stage backwards off ac-dev-ready: the issue is not
                # ready after all, and leaving the trigger label on would make
                # the next poll pick it straight back up.
                add, remove = stages.transition(record.labels, "ac-input-needed")
                for label in add:
                    github.add_label(record.repo, record.number, label)
                for label in remove:
                    github.remove_label(record.repo, record.number, label)
                # Re-fetch updated_at so the poller doesn't treat our own
                # comment as a user response and immediately re-triage
                try:
                    fresh = github.get_issue(record.repo, record.number)
                    state.update(record.issue_id,
                                 issue_updated_at=fresh.get("updated_at", ""))
                    state.save()
                except Exception:
                    pass
                logger.info(f"Posted clarifying questions on {record.issue_id}")
            except Exception as exc:
                logger.error(f"Failed to post comment on {record.issue_id}: {exc}")
        else:
            logger.info(f"DRY-RUN: would post clarifying questions on {record.issue_id}")


def _run_single_issue(args, config, state, github, triage_engine, logger,
                       process_manager) -> None:
    """Process a single issue (--issue mode) and wait for the worker to finish."""
    issue_str = args.issue
    if "#" not in issue_str:
        logger.error(f"Invalid --issue format: {issue_str!r} (expected repo#number)")
        return

    repo, number_str = issue_str.split("#", 1)
    try:
        number = int(number_str)
    except ValueError:
        logger.error(f"Invalid issue number: {number_str!r}")
        return

    issue_id = f"{repo}#{number}"
    record = state.get(issue_id)

    if record is None:
        # Fetch from GitHub and add to state
        logger.info(f"Fetching issue {issue_id} from GitHub...")
        try:
            issue_data = github.get_issue(repo, number)
        except GithubClientError as exc:
            logger.error(f"Failed to fetch issue: {exc}")
            return

        label_names = [lbl["name"] for lbl in issue_data.get("labels", [])]

        # --issue is a manual override, so it deliberately bypasses the
        # assignee half of the trigger contract - the operator naming an issue
        # explicitly *is* the ownership decision. The stage half still applies:
        # forcing a run on a terminal or already-locked issue would stomp
        # whoever owns it.
        if stages.is_terminal(label_names):
            logger.error(
                f"{issue_id} is in a terminal stage "
                f"({stages.stage_of(label_names)}) — refusing to run"
            )
            return
        if stages.stage_of(label_names) in stages.LOCKED:
            logger.error(
                f"{issue_id} is locked by another runner "
                f"({stages.stage_of(label_names)}) — refusing to run"
            )
            return

        action = stages.kind_of(label_names)
        # Mirror the poller's routing: the stage label decides which worker
        # runs, so `--issue` on an ac-dev-review issue reviews the PR rather
        # than re-triaging an issue that has already been implemented.
        mode = "review" if stages.is_reviewable(label_names) else "dev"
        from state import IssueRecord
        record = IssueRecord(
            issue_id=issue_id,
            repo=repo,
            number=number,
            title=issue_data["title"],
            body=issue_data.get("body", "") or "",
            labels=label_names,
            action=action,
            status=IssueStatus.DISCOVERED,
            discovered_at=issue_data.get("created_at", ""),
            updated_at=issue_data.get("updated_at", ""),
            issue_updated_at=issue_data.get("updated_at", ""),
            mode=mode,
        )
        state.add(record)
        state.save()

    # Triage if needed
    if record.status in (IssueStatus.DISCOVERED,):
        _run_triage(record, state, github, triage_engine, config, logger)
        record = state.get(issue_id)

    if record.status == IssueStatus.QUEUED:
        process_manager.spawn(record)

        # Wait for worker to finish
        logger.info(f"Waiting for worker to complete {issue_id}...")
        while process_manager.active_count > 0 and not shutdown_requested:
            process_manager.drain_state_queue()
            logger.drain_queue(process_manager._log_queue)
            process_manager.reap_dead()
            time.sleep(1)

        # Final drain
        process_manager.drain_state_queue()
        logger.drain_queue(process_manager._log_queue)

        record = state.get(issue_id)
        logger.info(f"Final status for {issue_id}: {record.status}")
    else:
        logger.info(f"Issue {issue_id} is in status {record.status} — nothing to do")


def main() -> None:
    global shutdown_requested

    enable_ansi_windows()

    args = parse_args()

    # Secrets and locations from the gitignored .env. This runs *before*
    # load_config because config path values may reference ${VARS} defined
    # here - `env_source = "${ACCELEVATION_ROOT}/field_admin"` resolves at load
    # time, so the environment has to be populated first. Worker processes
    # inherit os.environ, so this reaches them too.
    project_root = Path(args.config).resolve().parent if args.config else Path.cwd()
    for key, value in load_dotenv(project_root).items():
        os.environ[key] = value

    config = load_config(args.config)

    logger = MainLogger(
        log_file=config.paths.log_file,
        colorize=config.logging.colorize,
        log_to_file=config.logging.log_to_file,
        level=config.logging.level,
    )

    print(BANNER)
    logger.info(f"Loaded config for org: {config.github.org}")
    logger.info(f"Monitoring repos: {', '.join(config.github.repos)}")
    if config.github.bot_login:
        logger.info(f"Issue scope: only issues assigned to '{config.github.bot_login}'")

    if args.dry_run:
        logger.info("DRY-RUN mode — no workers will be spawned")
    if args.issue:
        logger.info(f"Single-issue mode: {args.issue}")

    create_directories(config)
    logger.info("Runtime directories ready")

    # Authenticate as the bot account. The token is placed in os.environ under a
    # private name so worker processes inherit it; it only becomes GH_TOKEN
    # inside the subprocess environments ghauth builds. `.env` was already
    # loaded above, before config.
    token = load_token(project_root)
    if token:
        os.environ[TOKEN_ENV_VAR] = token
        logger.info("Loaded bot token")

    if os.environ.get(METRICS_DB_ENV_VAR):
        logger.info("Pipeline metrics DB configured")
    else:
        logger.warn(
            f"No {METRICS_DB_ENV_VAR} - telemetry will be dropped. Set it in "
            f".env to record pipeline events."
        )

    # The ownership gate is not skippable. Without a bot account and a token to
    # act as it, auto-claude either poaches issues belonging to a human /loop
    # runner or does the work under the operator's own name - both silent, both
    # discovered after the fact.
    _report(logger, gate := check_ownership_config(token, config.github.bot_login))
    if has_fatal(gate):
        _abort(logger, "Refusing to start without a bot identity.")

    _report(logger, identity := verify_identity(token, config.github.bot_login))
    if has_fatal(identity):
        _abort(logger, "Refusing to start: not authenticated as the bot account.")

    if not args.skip_preflight:
        logger.info("Running preflight checks...")
        checks = check_access(
            token=token,
            org=config.github.org,
            repos=config.github.repos,
        )
        _report(logger, checks)
        if has_fatal(checks):
            _abort(
                logger,
                "Preflight failed — fix the above, or re-run with "
                "--skip-preflight to proceed anyway.",
            )

    # Register signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, handle_signal)
    else:
        signal.signal(signal.SIGBREAK, handle_signal)

    # Initialize core components
    state = StateStore(config.paths.state_file)
    github = GithubClient(config.github.org)
    poller = Poller(config, github, state, logger)
    triage_engine = TriageEngine(config, github)

    # Create multiprocessing queues
    log_queue = multiprocessing.Queue()
    state_queue = multiprocessing.Queue()

    process_manager = ProcessManager(
        config=config,
        state=state,
        logger=logger,
        log_queue=log_queue,
        state_queue=state_queue,
    )

    # Ensure all labels exist on monitored repos. Skipped under --dry-run:
    # creating labels is a write, and a dry run that mutates GitHub is not one.
    if args.dry_run:
        logger.info("DRY-RUN: skipping label creation")
    else:
        logger.info("Ensuring labels exist on all repos...")
        _ensure_labels(config, github, logger)

    # Recover from a previous crash before polling, so a stuck lock does not
    # silently remove an issue from circulation.
    released = _release_stale_locks(config, github, logger, dry_run=args.dry_run)
    if not released:
        logger.info("No stale locks to release")

    # Single-issue mode
    if args.issue:
        try:
            _run_single_issue(args, config, state, github, triage_engine, logger,
                              process_manager)
        except KeyboardInterrupt:
            shutdown_requested = True
        finally:
            process_manager.shutdown_all()
            logger.info("Done.")
            logger.close()
        return

    # Main polling loop
    logger.info(f"Polling every {config.github.poll_interval_seconds}s — press Ctrl+C to stop")

    try:
        while not shutdown_requested:
            # 1. Drain queues + reap dead workers
            process_manager.drain_state_queue()
            logger.drain_queue(log_queue)
            process_manager.reap_dead()

            # 2. Poll for new/retriage issues
            new_issues, retriage_issues = poller.poll()

            # 3. Triage new issues. Review records arrive already QUEUED —
            #    the poller walks them there directly, since triage answers a
            #    question that does not apply to an open PR.
            for record in new_issues:
                if shutdown_requested:
                    break
                if record.mode == "review":
                    continue
                _run_triage(record, state, github, triage_engine, config, logger,
                            dry_run=args.dry_run)

            # 4. Re-triage updated needs_info issues
            for record in retriage_issues:
                if shutdown_requested:
                    break
                _run_triage(record, state, github, triage_engine, config, logger,
                            dry_run=args.dry_run)

            # 5. Spawn workers for queued issues
            if not args.dry_run:
                for record in state.get_by_status(IssueStatus.QUEUED):
                    if shutdown_requested or not process_manager.can_spawn():
                        break
                    if record.issue_id not in process_manager.active_issue_ids:
                        process_manager.spawn(record)

            # 6. Mirror label state onto the shared Projects v2 board. Runs
            #    once per tick rather than per transition — the script syncs
            #    every open issue it is given, so per-transition calls would be
            #    redundant work. Never fatal; board drift is recoverable.
            if not args.dry_run:
                _sync_boards(config, github, logger)

            # 7. Sleep in small increments so shutdown is responsive
            for _ in range(config.github.poll_interval_seconds):
                if shutdown_requested:
                    break
                # Drain queues during sleep too
                process_manager.drain_state_queue()
                logger.drain_queue(log_queue)
                time.sleep(1)

    except KeyboardInterrupt:
        shutdown_requested = True

    # Graceful shutdown
    logger.info("Shutting down...")
    process_manager.shutdown_all()
    logger.drain_queue(log_queue)
    logger.info("Goodbye.")
    logger.close()


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
