"""auto-claude — monitors GitHub issues and spawns Claude workers to solve them."""

import argparse
import dataclasses
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import stages
import version
from config import load_config
from db import harness as db_harness
from db import issue_state as db_issue_state
from db import lease as db_lease
from db.harness import new_harness
from db.journal import Journal
from db.pool import Database, DbUnavailable
from db.schema import SchemaOutOfDate, check_schema_current
from dbsync import DbSync
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
from pipeline import PIPELINE_JSON_RELATIVE_PATH, PipelineConfigError, parse_pipeline_config
from poller import Poller
from process_manager import ProcessManager
from reconcile import reconcile
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


def _check_schema_gate(db, logger: MainLogger) -> None:
    """Refuse to start against a Postgres schema behind EXPECTED_REVISION.
    The daemon never migrates itself — see db/schema.py and docs/plans/
    12-shared-state-in-postgres.md, "Migrations"."""
    try:
        check_schema_current(db)
    except SchemaOutOfDate as exc:
        _abort(logger, str(exc))
    except DbUnavailable as exc:
        _abort(logger, f"Cannot reach Postgres to verify the schema: {exc}")


def _register_harness(db, harness, logger: MainLogger, journal) -> None:
    """Best-effort harness registration. A registration that cannot reach
    Postgres is journaled and replayed the next time Postgres is reachable —
    see Journal.OPS, which lists "harness.register" as a legitimate op."""
    if db is None:
        return
    try:
        db_harness.register(db, harness)
    except DbUnavailable as exc:
        journal.append("harness.register", {
            "id": harness.id, "hostname": harness.hostname,
            "pid": harness.pid, "version": harness.version,
        })
        logger.warn(f"Postgres unreachable while registering the harness — journaled: {exc}")


def _init_db_layer(config, logger: MainLogger):
    """Construct the Postgres-backed layer main() writes through, or a fully
    degraded stand-in when it is disabled/unreachable. Returns
    (db, journal, harness, dbsync) — db is None in degraded mode; journal,
    harness and dbsync always exist so callers never branch on "did this
    construct".

    Startup is deliberately stricter than runtime (ruling, 2026-07-29): a
    harness that cannot confirm it reaches a current-schema Postgres could
    double-claim an issue another box already holds a lease on, so both an
    unreachable Postgres and a stale/missing schema abort the daemon here via
    `_check_schema_gate` -> `_abort`. Only "database not configured at all"
    (disabled, or no URL) is exempt — that is single-harness mode, which
    never takes a lease to begin with, and runs exactly as the daemon does
    today. This is the opposite of runtime behaviour, where a database
    problem must never abort a running agent.
    """
    journal = Journal(config.database.journal_file)
    harness = new_harness(version.__version__)

    db = None
    url = config.database.url() if config.database.enabled else None
    if url:
        db = Database(url, connect_timeout=config.database.connect_timeout_seconds)
        _check_schema_gate(db, logger)
        _register_harness(db, harness, logger, journal)
    else:
        logger.info(
            "Database sync disabled or PIPELINE_METRICS_DATABASE_URL unset — "
            "running on local state only"
        )

    dbsync = DbSync(db, harness, logger, journal=journal,
                     ttl_seconds=config.database.lease_ttl_seconds)
    return db, journal, harness, dbsync


def _collect_gh_issues_for_reconcile(config, github, logger: MainLogger) -> dict:
    """The `gh_issues` shape reconcile.reconcile() expects — see reconcile.py.
    A repo that cannot be read is skipped, not fatal; a poll a few seconds
    later behaves the same way already."""
    gh_issues: dict[str, dict] = {}
    for repo in config.github.repos:
        try:
            issues = github.list_issues(repo, assignee=config.github.bot_login)
        except GithubClientError as exc:
            logger.warn(f"Could not read {repo} for reconciliation: {exc}")
            continue
        for issue in issues:
            label_names = [lbl["name"] for lbl in issue.get("labels", [])]
            issue_id = f"{repo}#{issue['number']}"
            gh_issues[issue_id] = {
                "stage": stages.stage_of(label_names),
                "repo": repo,
                "number": issue["number"],
                "title": issue["title"],
                "body": issue.get("body", "") or "",
                "labels": label_names,
                "action": stages.kind_of(label_names),
                "issue_updated_at": issue.get("updated_at", ""),
                "discovered_at": issue.get("created_at", ""),
            }
    return gh_issues


def _reconcile_at_startup(config, github, state, db, harness, dbsync, logger: MainLogger):
    """Rebuild `state` from GitHub + Postgres before the poll loop starts.
    See reconcile.py and docs/plans/12-shared-state-in-postgres.md, "Startup
    reconciliation". db=None (disabled or unreachable) reconciles against an
    empty db_rows dict — identical to a genuinely empty database on the very
    first run, which the design already treats as the normal case.

    Expired leases are released in Postgres FIRST, before `db_rows` is
    fetched: `dbsync.release_expired()` issues the actual `UPDATE` that
    clears them (db/lease.py, Task 12), so by the time `db_rows` is read
    below those rows already show a free lease, and `reconcile()`'s own
    db_rows-derived `leases_released` can no longer tell "just freed" apart
    from "never held". The ids `release_expired()` actually returned are
    substituted into the report afterwards, which is what keeps
    `ReconcileReport.leases_released` true rather than merely informational.
    `release_expired` now exists on every real `DbSync` (Task 13 wired
    db/lease.py into it); the `getattr(..., None)` guard is kept so a
    stand-in `dbsync` that omits the method (e.g. a bare test double) still
    no-ops instead of raising — that guard is the only thing "safe in every
    configuration" about this call. DbSync.release_expired fails open only
    when Postgres is disabled (returns `[]`, matching single-harness mode);
    if Postgres is unreachable it raises `DbUnavailable` uncaught, same as
    `db/lease.py`'s own `release_expired`. That is deliberate, not a gap:
    this call runs at *startup*, which is stricter than runtime by design
    (see `_init_db_layer`) — a harness that cannot confirm Postgres is
    reachable must not start polling, since a stale reconciliation could
    double-claim an issue another box already holds. `_init_db_layer`'s own
    schema/connectivity gate normally catches an unreachable Postgres
    moments earlier; letting `DbUnavailable` propagate here as well covers
    the narrow race where it drops between that gate and this call, and
    aborts the daemon the same way rather than reconciling against stale data.
    """
    release_expired = getattr(dbsync, "release_expired", None)
    released = release_expired() if release_expired is not None else []

    gh_issues = _collect_gh_issues_for_reconcile(config, github, logger)

    db_rows: dict = {}
    if db is not None:
        try:
            db_rows = db_issue_state.fetch_all(db)
        except DbUnavailable as exc:
            logger.warn(f"Could not fetch issue_state for reconciliation: {exc}")

    report = reconcile(state=state, db_rows=db_rows, gh_issues=gh_issues,
                        harness_id=harness.id, logger=logger)
    if released:
        report = dataclasses.replace(report, leases_released=released)
    return report


def _make_on_change(dbsync):
    """Bridges StateStore's single-record hook to DbSync.upsert_issue, which
    additionally wants the GitHub stage label — not part of IssueRecord
    itself, but derivable from record.labels via stages.stage_of."""
    def _handler(record) -> None:
        dbsync.upsert_issue(record, stages.stage_of(record.labels))
    return _handler


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
                         dry_run: bool = False, db=None) -> int:
    """Rewind locks left behind by a crashed or killed run.

    Expiry-driven, not startup-driven: this can no longer assume no worker
    of ours is alive elsewhere - a second harness may hold a perfectly live
    lease on an issue this box also sees carrying `ac-in-progress`. A label
    is only rewound once its Postgres lease is confirmed free or expired
    (see docs/plans/12-shared-state-in-postgres.md, "The blocker this
    removes").

    `db` is the raw Postgres handle (`db.pool.Database`), not `DbSync` -
    this is a one-time startup sweep across every repo's issues, not a
    per-issue write on the worker hot path, so it goes straight at
    `db.lease`/`db.issue_state` rather than through the single-writer seam
    those modules exist to protect.

    Degraded path: `db is None` means Postgres is disabled, or was
    unreachable when `main` tried to construct it - in which case there
    cannot be a second harness (no shared database to coordinate through)
    and every bot-assigned locked issue is stale by definition, exactly as
    before this task. If Postgres *is* configured but `release_expired`
    fails right here (a transient outage), this fails CLOSED instead of
    falling back to that assumption: nothing is rewound for the rest of
    this call, because a configured-but-currently-unreachable database
    might still have a live second harness we simply cannot see, and
    rewinding blind is the exact bug this function exists to prevent.
    """
    lease_verified = db is not None
    if lease_verified:
        try:
            freed = db_lease.release_expired(db)
            if freed:
                logger.info(f"Postgres: {len(freed)} expired lease(s) released")
        except DbUnavailable as exc:
            logger.warn(
                f"Postgres unreachable at startup — cannot verify which "
                f"locks are genuinely stale, so none will be rewound this "
                f"run: {exc}"
            )
            lease_verified = False

    released = 0
    for repo in config.github.repos:
        if lease_verified:
            _warn_stale_lock_hours(config, github, repo, logger)

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

            if db is None:
                pass  # degraded path: no shared database, no second harness
            elif not lease_verified:
                logger.info(f"{issue_id}: cannot verify lease — leaving it")
                continue
            elif not _lease_is_free(db, issue_id, logger):
                logger.info(f"{issue_id}: lease still held by another harness — leaving it")
                continue

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


def _lease_is_free(db, issue_id: str, logger: MainLogger) -> bool:
    """True if `issue_id` has no live Postgres lease.

    Fails closed: an unreachable database counts as "cannot verify", which
    this reports as *not* free so the caller leaves the label alone rather
    than guessing - one transient fetch failure should not blind the whole
    sweep the way a `release_expired` failure does, so this degrades
    per-issue instead of aborting the loop.
    """
    try:
        row = db_issue_state.fetch(db, issue_id)
    except DbUnavailable as exc:
        logger.warn(f"Could not verify lease for {issue_id}: {exc}")
        return False
    return row is None or row.get("owner_harness_id") is None


def _warn_stale_lock_hours(config, github, repo: str, logger: MainLogger) -> None:
    """Log if a repo's `staleLockHours` disagrees with the real Postgres
    lease TTL, in either direction.

    `staleLockHours` (`pipeline.json`, parsed at `pipeline.py:76,157`) used
    to be auto-claude's only staleness signal and was never read. Now
    `lease_expires_at` (`config.database.lease_ttl_seconds`, Task 11 —
    global to the harness, the same for every repo; `DbSync.acquire_lease`'s
    frozen signature takes no per-repo override) is what actually determines
    staleness. Any disagreement between the two is worth surfacing, not just
    a `staleLockHours` that claims a *shorter* window than the TTL: the
    disagreement that actually exists under stock configuration is the
    opposite one — `pipeline.json`'s 2-hour default advertises a far longer
    stale window than a 1800s (30-minute) TTL actually enforces, which is
    exactly the "two sources of truth for when a lease expires" this
    diagnostic exists to surface. A shorter `staleLockHours` makes a live
    worker look stuck to a human for up to the difference; a longer one
    means a lock is already free for up to the difference while
    pipeline.json still calls it in-progress. This changes no behaviour - it
    is a diagnostic only - which is deliberate: it makes the field read and
    acted upon rather than silently discarded, without inventing per-repo
    TTL plumbing the frozen `DbSync` surface does not support. Compares
    against `config.database.lease_ttl_seconds`, not the
    `db.lease.LEASE_TTL_SECONDS` module default, since Task 13 lets an
    operator override that default — this diagnostic must reflect whatever
    is actually running, not the fallback.
    """
    text = _pipeline_json_text(config, github, repo, logger)
    if text is None:
        return
    try:
        pipeline = parse_pipeline_config(text, source=f"{repo}/{PIPELINE_JSON_RELATIVE_PATH}")
    except PipelineConfigError:
        return  # already surfaced elsewhere; not this function's job to repeat it

    ttl_seconds = config.database.lease_ttl_seconds
    configured_seconds = pipeline.stale_lock_hours * 3600
    if configured_seconds == ttl_seconds:
        return

    if configured_seconds < ttl_seconds:
        direction = "shorter than"
        diff_minutes = (ttl_seconds - configured_seconds) / 60
        consequence = (
            f"a legitimate in-progress run may appear stuck for up to "
            f"{diff_minutes:.0f} more minute(s)"
        )
    else:
        direction = "longer than"
        diff_minutes = (configured_seconds - ttl_seconds) / 60
        consequence = (
            f"a lock may already be free for up to {diff_minutes:.0f} "
            f"minute(s) while pipeline.json still calls it in-progress"
        )

    logger.warn(
        f"{repo}: staleLockHours={pipeline.stale_lock_hours}h "
        f"({configured_seconds:.0f}s) is {direction} the harness lease "
        f"TTL ({ttl_seconds}s) — {consequence}"
    )


def _maybe_heartbeat(dbsync, last_at: float, interval: float, logger: MainLogger,
                      now: float | None = None) -> float:
    """Call `dbsync.heartbeat()` if `interval` seconds have passed since `last_at`.

    Returns the timestamp to use as `last_at` on the next call. `now`
    defaults to `time.monotonic()` and exists purely so tests do not have to
    sleep for real — production callers never pass it.

    Called from both the top of the poll loop and its per-second sleep tick
    (see `main`), because heartbeat cadence must not depend on the sleep
    loop running uninterrupted — a slow poll/triage pass that never reaches
    the sleep loop must still keep leases alive.

    `dbsync.heartbeat()` fails open when Postgres is disabled (`DbSync`'s
    own no-op path), but when Postgres is *unreachable* mid-run,
    `db/lease.py`'s `heartbeat` lets `DbUnavailable` propagate uncaught, and
    `DbSync.heartbeat` adds no handling — see Finding 1. The poll loop's
    only handler is `except KeyboardInterrupt`, so an uncaught
    `DbUnavailable` here would unwind straight out of `main()` and kill the
    whole supervisor out from under every live worker. Caught and warned
    here instead, and `last_at` still advances to `now` regardless —
    otherwise a down database would turn every remaining loop tick into
    another doomed heartbeat attempt instead of waiting out `interval` like
    a healthy one would. A running agent is never aborted for a lost lease
    or a database outage; this is that guarantee applied to the heartbeat.
    """
    now = time.monotonic() if now is None else now
    if now - last_at < interval:
        return last_at
    try:
        dbsync.heartbeat()
    except DbUnavailable as exc:
        logger.warn(f"Postgres unreachable — heartbeat skipped: {exc}")
    return now


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
                dry_run: bool = False, dbsync=None) -> None:
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
                comment_url = github.post_comment(record.repo, record.number, comment)
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
                # Runless — triage is an inline call from `main`, not a
                # worker run, so `run_id` is NULL by design.
                if dbsync is not None:
                    dbsync.add_summary(
                        issue_id=record.issue_id, run_id=None, kind="triage",
                        body=comment, comment_url=comment_url,
                    )
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
        _run_triage(record, state, github, triage_engine, config, logger,
                    dbsync=process_manager.dbsync)
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
    github = GithubClient(config.github.org)

    db, journal, harness, dbsync = _init_db_layer(config, logger)

    state = StateStore(config.paths.state_file, on_change=_make_on_change(dbsync))
    _reconcile_at_startup(config, github, state, db, harness, dbsync, logger)

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
        dbsync=dbsync,
        harness_id=harness.id,
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
    released = _release_stale_locks(config, github, logger, dry_run=args.dry_run, db=db)
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

    last_heartbeat = time.monotonic()

    try:
        while not shutdown_requested:
            # 1. Drain queues + reap dead workers
            process_manager.drain_state_queue()
            logger.drain_queue(log_queue)
            process_manager.reap_dead()
            last_heartbeat = _maybe_heartbeat(
                dbsync, last_heartbeat, config.database.heartbeat_interval_seconds, logger
            )
            dbsync.replay_pending()

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
                            dry_run=args.dry_run, dbsync=process_manager.dbsync)

            # 4. Re-triage updated needs_info issues
            for record in retriage_issues:
                if shutdown_requested:
                    break
                _run_triage(record, state, github, triage_engine, config, logger,
                            dry_run=args.dry_run, dbsync=process_manager.dbsync)

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
                # Note: dbsync's journal replay is deliberately NOT attempted
                # here (fix round, Finding 4). _maybe_heartbeat is
                # interval-gated so this per-second tick never blocks on a
                # dead database, but the replay call had no such gate:
                # Database(retries=2, connect_timeout=10) means one failing
                # execute() during replay burns ~33s of backoff, defeating
                # the comment above ("shutdown is responsive") on every tick
                # while entries are queued — exactly when Postgres is down.
                # It still runs once per pass, at the top of the loop below.
                last_heartbeat = _maybe_heartbeat(
                    dbsync, last_heartbeat, config.database.heartbeat_interval_seconds, logger
                )
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
