"""auto-claude — monitors GitHub issues and spawns Claude workers to solve them."""

import argparse
import multiprocessing
import signal
import sys
import time
from pathlib import Path

from config import load_config
from github_client import GithubClient, GithubClientError
from logger import MainLogger, enable_ansi_windows
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
        "--issue",
        type=str,
        default=None,
        help="Process a single issue and exit (format: repo#number)",
    )
    return parser.parse_args()


def create_directories(config) -> None:
    """Create runtime directories if they don't exist."""
    config.paths.repos_dir.mkdir(parents=True, exist_ok=True)
    config.paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
    config.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.paths.log_file.parent.mkdir(parents=True, exist_ok=True)


def _ensure_labels(config, github: GithubClient, logger: MainLogger) -> None:
    """Ensure all ac-* labels exist on all monitored repos."""
    all_labels = (
        config.github.action_labels
        + [
            config.github.needs_info_label,
            config.github.in_progress_label,
            config.github.pr_created_label,
            config.github.plan_posted_label,
            config.github.review_posted_label,
        ]
    )
    for repo in config.github.repos:
        for label in all_labels:
            try:
                github.ensure_label_exists(repo, label)
            except GithubClientError as exc:
                logger.warn(f"Could not ensure label {label} on {repo}: {exc}")


def _run_triage(record, state, github, triage_engine, config, logger,
                dry_run: bool = False) -> None:
    """Triage a single issue and update state accordingly."""
    logger.info(f"Triaging {record.issue_id}...")
    state.transition(record.issue_id, IssueStatus.TRIAGING)
    state.update(record.issue_id, triage_attempts=record.triage_attempts + 1)
    state.save()

    decision = triage_engine.triage(record)
    logger.info(
        f"Triage {decision.decision.upper()} ({decision.confidence}) — {decision.summary}"
    )

    if decision.decision == "proceed":
        if record.action in config.github.plan_actions:
            state.transition(record.issue_id, IssueStatus.PLANNING)
        else:
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
                github.add_label(record.repo, record.number,
                                 config.github.needs_info_label)
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
        action_label = None
        for lbl in label_names:
            if lbl in config.github.action_labels:
                action_label = lbl
                break

        if action_label is None:
            logger.error(f"No ac-* action label found on {issue_id}")
            return

        action = action_label[len(config.github.label_prefix):]
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
        )
        state.add(record)
        state.save()

    # Triage if needed
    if record.status in (IssueStatus.DISCOVERED,):
        _run_triage(record, state, github, triage_engine, config, logger)
        record = state.get(issue_id)

    # Spawn worker if queued or planning
    if record.status in (IssueStatus.QUEUED, IssueStatus.PLANNING):
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

    if args.dry_run:
        logger.info("DRY-RUN mode — no workers will be spawned")
    if args.issue:
        logger.info(f"Single-issue mode: {args.issue}")

    create_directories(config)
    logger.info("Runtime directories ready")

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

    # Ensure all labels exist on monitored repos
    logger.info("Ensuring labels exist on all repos...")
    _ensure_labels(config, github, logger)

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

            # 3. Triage new issues
            for record in new_issues:
                if shutdown_requested:
                    break
                _run_triage(record, state, github, triage_engine, config, logger,
                            dry_run=args.dry_run)

            # 4. Re-triage updated needs_info issues
            for record in retriage_issues:
                if shutdown_requested:
                    break
                _run_triage(record, state, github, triage_engine, config, logger,
                            dry_run=args.dry_run)

            # 5. Spawn workers for queued/planning issues
            if not args.dry_run:
                for record in state.get_by_status(IssueStatus.QUEUED):
                    if shutdown_requested or not process_manager.can_spawn():
                        break
                    if record.issue_id not in process_manager.active_issue_ids:
                        process_manager.spawn(record)

                for record in state.get_by_status(IssueStatus.PLANNING):
                    if shutdown_requested or not process_manager.can_spawn():
                        break
                    if record.issue_id not in process_manager.active_issue_ids:
                        process_manager.spawn(record)

            # 6. Sleep in small increments so shutdown is responsive
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
