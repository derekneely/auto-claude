"""Process manager — spawns, tracks, and reaps worker processes."""

from __future__ import annotations

import multiprocessing
import os
import time
from multiprocessing import Event, Process, Queue

from config import Config
from ghauth import build_env, current_token
from logger import ColorAssigner, MainLogger
from github_client import GithubClient
from pipeline import PIPELINE_JSON_RELATIVE_PATH, parse_pipeline_config
from state import IssueRecord, IssueStatus, StateStore
from redact import redact
from worker import IssueContext, StateUpdate, run_dev_worker, run_review_worker


class ProcessManager:
    """Manages the pool of worker processes."""

    def __init__(
        self,
        config: Config,
        state: StateStore,
        logger: MainLogger,
        log_queue: Queue,
        state_queue: Queue,
    ) -> None:
        self._config = config
        self._state = state
        self._logger = logger
        self._log_queue = log_queue
        self._state_queue = state_queue
        self._color_assigner = ColorAssigner()
        # Built on first use rather than here: nothing in the constructor needs
        # GitHub, and requiring it would make ProcessManager unconstructible
        # without a fully-populated config.
        self._github: GithubClient | None = None
        # issue_id -> (Process, abort_event)
        self._workers: dict[str, tuple[Process, Event]] = {}
        # Epoch seconds until which no new workers may spawn. Rate limiting is an
        # account-wide condition, so it has to gate the whole pool — retrying a
        # single worker just burns the next slot against the same closed window.
        self._rate_limited_until: float = 0.0
        # repo -> PipelineConfig | None, cached so we do not re-read and
        # re-warn about the same pipeline.json on every spawn.
        self._pipelines: dict[str, object] = {}

    def _repo_pipeline(self, repo: str):
        """The repo's `.claude/pipeline.json`, or None if it has none.

        Read from GitHub, not from the local clone. The clone is only refreshed
        when a worker actually runs, so reading it locally reports "no
        pipeline.json" for a repo that has had one for weeks.

        Read from the **configured base branch** in preference to the repo's
        default branch. GitHub's contents API defaults to the default branch,
        which for `field_admin` is `main` — a release cycle behind the active
        integration branch. That made auto-claude read `prBaseBranch:
        "derekdev"` for days after `dev` had been corrected. The sibling
        toolchain never hits this because it reads the operator's working
        checkout. Falls back to the default branch when the base branch has no
        copy, so repos that keep the file only on their default branch are
        unaffected.
        """
        if repo in self._pipelines:
            return self._pipelines[repo]

        self._pipelines[repo] = None
        if self._github is None:
            self._github = GithubClient(self._config.github.org)

        base_branch = getattr(self._config.github, "base_branch", None)
        refs: list[str | None] = [base_branch, None] if base_branch else [None]

        text = None
        for ref in refs:
            try:
                text = self._github.get_file(repo, PIPELINE_JSON_RELATIVE_PATH, ref=ref)
            except Exception as exc:
                self._logger.warn(
                    f"Could not read {repo}/{PIPELINE_JSON_RELATIVE_PATH}"
                    f"{f'@{ref}' if ref else ''}: {exc}"
                )
                return None
            if text is not None:
                break

        if text is None:
            self._logger.warn(
                f"{repo}: no {PIPELINE_JSON_RELATIVE_PATH} - falling back to "
                f"global [github] config"
            )
            return None

        try:
            self._pipelines[repo] = parse_pipeline_config(
                text, source=f"{repo}/{PIPELINE_JSON_RELATIVE_PATH}"
            )
        except Exception as exc:
            # A malformed pipeline.json must not take the daemon down; the
            # global fallback is a correct, if less specific, answer.
            self._logger.warn(f"Ignoring {repo}/{PIPELINE_JSON_RELATIVE_PATH}: {exc}")

        return self._pipelines[repo]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_spawn(self) -> bool:
        """Return True if we may start another worker right now."""
        if self.rate_limit_remaining > 0:
            return False
        return len(self._workers) < self._config.workers.max_parallel

    @property
    def rate_limit_remaining(self) -> float:
        """Seconds left on the global rate-limit pause (0.0 when not paused)."""
        return max(0.0, self._rate_limited_until - time.time())

    def spawn(self, record: IssueRecord) -> None:
        """Spawn a worker process for the given issue."""
        if not self.can_spawn():
            self._logger.warn(
                f"Cannot spawn worker for {record.issue_id} — at capacity "
                f"({len(self._workers)}/{self._config.workers.max_parallel})"
            )
            return

        if record.issue_id in self._workers:
            self._logger.warn(f"Worker already running for {record.issue_id}")
            return

        # Assign color
        color_name, color_code = self._color_assigner.assign(record.issue_id)

        # Resolve model for this action (action_models override, else dev_model)
        action = record.action
        claude_cfg = self._config.claude
        resolved_model = claude_cfg.action_models.get(action, claude_cfg.dev_model)

        max_turns = claude_cfg.max_turns_dev

        # Per-repo contract wins over the global fallback. A repo with no
        # pipeline.json keeps the old behaviour rather than failing to spawn.
        pipeline = self._repo_pipeline(record.repo)
        base_branch = (
            pipeline.pr_base_branch if pipeline
            else self._config.github.base_branch
        )
        pipeline_project = pipeline.project if pipeline else record.repo

        # Build IssueContext (all picklable)
        ctx = IssueContext(
            issue_id=record.issue_id,
            repo=record.repo,
            number=record.number,
            title=record.title,
            body=record.body,
            action=record.action,
            org=self._config.github.org,
            base_branch=base_branch,
            repos_dir=self._config.paths.repos_dir,
            worktrees_dir=self._config.paths.worktrees_dir,
            repo_setup=getattr(self._config, "repo_setup", {}).get(record.repo),
            prompts_dir=self._config.paths.prompts_dir,
            dev_model=resolved_model,
            light_model=claude_cfg.light_model,
            permission_mode=claude_cfg.permission_mode,
            max_budget_usd=claude_cfg.max_budget_usd,
            max_turns=max_turns,
            crash_logs_dir=self._config.paths.crash_logs_dir,
            color_name=color_name,
            color_code=color_code,
            existing_branch=record.branch,
            pr_url=record.pr_url,
            rework_count=record.rework_count,
            handoff_summary=record.handoff_summary,
            grace_budget_usd=claude_cfg.grace_budget_usd,
            claude_tools_root=self._config.integrations.claude_tools_root,
            pipeline_project=pipeline_project,
        )

        abort_event = multiprocessing.Event()

        # `mode` is set by the poller from the issue's stage label, so routing
        # follows the label rather than any local guess about what is next.
        target = run_review_worker if record.mode == "review" else run_dev_worker

        proc = Process(
            target=target,
            args=(ctx, self._log_queue, self._state_queue, abort_event),
            name=f"worker-{record.issue_id}",
            daemon=True,
        )
        proc.start()

        self._workers[record.issue_id] = (proc, abort_event)
        self._logger.info(
            f"Spawned {record.mode} worker ({record.action}) for "
            f"{record.issue_id} (PID {proc.pid})"
        )

    def reap_dead(self) -> None:
        """Check for dead workers, handle retries."""
        dead: list[str] = []

        for issue_id, (proc, _abort_event) in self._workers.items():
            if not proc.is_alive():
                dead.append(issue_id)

        for issue_id in dead:
            proc, _abort_event = self._workers.pop(issue_id)
            self._color_assigner.release(issue_id)
            proc.join(timeout=5)

            record = self._state.get(issue_id)
            if record is None:
                continue

            exitcode = proc.exitcode
            self._logger.info(
                f"Worker for {issue_id} exited (code={exitcode}, status={record.status})"
            )

            # If the worker crashed without sending a status update, mark failed
            if record.status == IssueStatus.IN_PROGRESS:
                self._state.transition(issue_id, IssueStatus.FAILED)
                self._state.update(issue_id, error=f"Worker crashed (exit code {exitcode})")
                self._state.save()
                record = self._state.get(issue_id)

            # Rate limited: re-queue unconditionally. This is not the issue's
            # fault, so it must not consume a continuation or count as a failure.
            # can_spawn() holds it back until the window resets, so this cannot spin.
            if record.status == IssueStatus.FAILED and record.error == "rate_limited":
                self._state.transition(issue_id, IssueStatus.QUEUED)
                self._state.update(issue_id, error=None)
                self._state.save()
                self._logger.info(
                    f"Rate limited — re-queued {issue_id} "
                    f"(resumes in {self.rate_limit_remaining / 60:.1f} min)"
                )

            # Budget exhaustion: re-queue for continuation if under limit
            elif record.status == IssueStatus.FAILED and record.error == "budget_exceeded":
                new_count = record.continuation_count + 1
                max_cont = self._config.workers.max_continuations
                if new_count <= max_cont:
                    self._state.transition(issue_id, IssueStatus.QUEUED)
                    self._state.update(issue_id, continuation_count=new_count)
                    self._state.save()
                    self._logger.info(
                        f"Budget exceeded — re-queued {issue_id} for continuation "
                        f"({new_count}/{max_cont})"
                    )
                else:
                    self._state.update(issue_id, continuation_count=new_count)
                    self._state.save()
                    self._logger.error(
                        f"{issue_id} exceeded budget across {new_count} runs — giving up"
                    )
                    self._post_budget_comment(record)

            # No automatic retries — crash logs are written by the worker.
            # Re-label the issue manually to retry.
            elif record.status == IssueStatus.FAILED:
                self._state.save()
                self._logger.error(
                    f"{issue_id} failed — check crash_logs for details. "
                    f"Re-label the issue to retry."
                )

    def drain_state_queue(self) -> None:
        """Process all pending StateUpdate messages from workers."""
        while True:
            try:
                update: StateUpdate = self._state_queue.get_nowait()
            except Exception:
                break

            # Rate limiting is account-wide, so honour it before anything that
            # can `continue` — an unknown issue or a rejected transition must
            # not cause the pool-wide pause to be dropped on the floor.
            self._note_rate_limit(update)

            record = self._state.get(update.issue_id)
            if record is None:
                continue

            try:
                self._state.transition(update.issue_id, update.status)
            except Exception as exc:
                self._logger.warn(
                    f"State transition failed for {update.issue_id}: {exc}"
                )
                continue

            # Apply optional fields
            updates = {}
            if update.error is not None:
                updates["error"] = update.error
            if update.branch is not None:
                updates["branch"] = update.branch
            if update.pr_url is not None:
                updates["pr_url"] = update.pr_url
            if update.worker_pid is not None:
                updates["worker_pid"] = update.worker_pid
            if update.handoff_summary is not None:
                updates["handoff_summary"] = update.handoff_summary
            if updates:
                self._state.update(update.issue_id, **updates)

            self._state.save()

    def abort_worker(self, issue_id: str) -> None:
        """Signal a specific worker to abort."""
        if issue_id in self._workers:
            _proc, abort_event = self._workers[issue_id]
            abort_event.set()
            self._logger.info(f"Sent abort signal to worker for {issue_id}")

    def shutdown_all(self, grace_seconds: int | None = None) -> None:
        """Gracefully shut down all workers."""
        if not self._workers:
            return

        if grace_seconds is None:
            grace_seconds = self._config.workers.shutdown_grace_seconds

        self._logger.info(f"Shutting down {len(self._workers)} worker(s)...")

        # Set abort on all workers
        for issue_id, (_proc, abort_event) in self._workers.items():
            abort_event.set()

        # Wait for graceful exit
        deadline = time.monotonic() + grace_seconds
        while self._workers and time.monotonic() < deadline:
            self._drain_and_reap_during_shutdown()
            time.sleep(0.5)

        # Force-terminate any remaining
        for issue_id, (proc, _abort_event) in list(self._workers.items()):
            if proc.is_alive():
                self._logger.warn(f"Force-terminating worker for {issue_id}")
                proc.terminate()
                proc.join(timeout=5)

            self._mark_interrupted(issue_id)

        self._workers.clear()

        # Final drain
        self.drain_state_queue()
        self._logger.drain_queue(self._log_queue)

    @property
    def active_count(self) -> int:
        return len(self._workers)

    @property
    def active_issue_ids(self) -> set[str]:
        return set(self._workers.keys())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _note_rate_limit(self, update: StateUpdate) -> None:
        """Extend the pool-wide spawn pause from a worker's rate-limit report."""
        deadline = update.rate_limited_until
        if deadline is None or deadline <= self._rate_limited_until:
            return

        self._rate_limited_until = deadline
        resume = time.strftime("%H:%M:%S", time.localtime(deadline))
        self._logger.warn(
            f"Rate limited — pausing all worker spawns for "
            f"{self.rate_limit_remaining / 60:.1f} min (resuming ~{resume})"
        )

    def _mark_interrupted(self, issue_id: str) -> None:
        """Leave an aborted worker's issue in a status the poller can resurrect.

        `poller` only re-queues a known issue from FAILED/COMPLETED/INTERRUPTED,
        so a record left at IN_PROGRESS is stranded for good: relabelling it
        ac-dev-ready does nothing, and `_release_stale_locks` only rewinds
        GitHub labels, never the state store. Guarded on IN_PROGRESS so a worker
        that reported a terminal status before the abort landed keeps it —
        callers must drain the state queue first, which both do.
        """
        record = self._state.get(issue_id)
        if record and record.status == IssueStatus.IN_PROGRESS:
            self._state.transition(issue_id, IssueStatus.INTERRUPTED)
            self._state.save()

    def _drain_and_reap_during_shutdown(self) -> None:
        """Drain queues and remove dead workers during shutdown."""
        self.drain_state_queue()
        self._logger.drain_queue(self._log_queue)

        dead = [
            issue_id
            for issue_id, (proc, _) in self._workers.items()
            if not proc.is_alive()
        ]
        for issue_id in dead:
            proc, _ = self._workers.pop(issue_id)
            self._color_assigner.release(issue_id)
            proc.join(timeout=5)
            # A worker that obeys abort and exits inside the grace period is
            # reaped here, which pops it before shutdown_all's force-terminate
            # loop can see it. Without this the "mark interrupted" pass there
            # ran over an empty dict and every clean Ctrl+C stranded its issue.
            self._mark_interrupted(issue_id)

    def _post_budget_comment(self, record: IssueRecord) -> None:
        """Post a comment when budget was exceeded across max continuation runs."""
        try:
            import subprocess
            env = build_env(current_token())
            max_cont = self._config.workers.max_continuations
            budget = self._config.claude.max_budget_usd
            total = budget * (max_cont + 1)
            body = redact(
                f"**auto-claude** exceeded its budget across {record.continuation_count} "
                f"continuation run(s) (${budget}/run, ~${total:.2f} total).\n\n"
                f"The issue may be too large for automated handling at the current budget. "
                f"Partial work has been pushed to branch `{record.branch}`.\n\n"
                f"_Consider breaking this into smaller issues, or increase "
                f"`max_budget_usd` in config._"
            )
            subprocess.run(
                [
                    "gh", "issue", "comment", str(record.number),
                    "--repo", f"{self._config.github.org}/{record.repo}",
                    "--body", body,
                ],
                text=True,
                capture_output=True,
                timeout=30,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            self._logger.error(f"Failed to post budget comment on {record.issue_id}: {exc}")
