"""Process manager — spawns, tracks, and reaps worker processes."""

from __future__ import annotations

import multiprocessing
import os
import time
from multiprocessing import Event, Process, Queue

from config import Config
from logger import ColorAssigner, MainLogger
from state import IssueRecord, IssueStatus, StateStore
from redact import redact
from worker import IssueContext, StateUpdate, run_dev_worker, run_plan_worker


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
        # issue_id -> (Process, abort_event)
        self._workers: dict[str, tuple[Process, Event]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_spawn(self) -> bool:
        """Return True if we haven't hit the max_parallel limit."""
        return len(self._workers) < self._config.workers.max_parallel

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

        # Build IssueContext (all picklable)
        ctx = IssueContext(
            issue_id=record.issue_id,
            repo=record.repo,
            number=record.number,
            title=record.title,
            body=record.body,
            action=record.action,
            org=self._config.github.org,
            base_branch=self._config.github.base_branch,
            repos_dir=self._config.paths.repos_dir,
            worktrees_dir=self._config.paths.worktrees_dir,
            prompts_dir=self._config.paths.prompts_dir,
            dev_model=self._config.claude.dev_model,
            permission_mode=self._config.claude.permission_mode,
            max_budget_usd=self._config.claude.max_budget_usd,
            color_name=color_name,
            color_code=color_code,
        )

        abort_event = multiprocessing.Event()

        # Route to correct worker function
        if record.action in self._config.github.dev_actions:
            target = run_dev_worker
        else:
            target = run_plan_worker

        proc = Process(
            target=target,
            args=(ctx, self._log_queue, self._state_queue, abort_event),
            name=f"worker-{record.issue_id}",
            daemon=True,
        )
        proc.start()

        self._workers[record.issue_id] = (proc, abort_event)
        self._logger.info(
            f"Spawned {record.action} worker for {record.issue_id} (PID {proc.pid})"
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

            # Retry logic: failed + under retry limit → re-queue
            if record.status == IssueStatus.FAILED:
                new_retry_count = record.retry_count + 1
                if new_retry_count < self._config.workers.retry_max:
                    self._state.transition(issue_id, IssueStatus.QUEUED)
                    self._state.update(issue_id, retry_count=new_retry_count)
                    self._state.save()
                    self._logger.info(
                        f"Re-queued {issue_id} (retry {new_retry_count}/{self._config.workers.retry_max})"
                    )
                else:
                    self._state.update(issue_id, retry_count=new_retry_count)
                    self._state.save()
                    self._logger.error(
                        f"{issue_id} failed after {new_retry_count} attempts — giving up"
                    )
                    # Post failure comment
                    self._post_failure_comment(record)

    def drain_state_queue(self) -> None:
        """Process all pending StateUpdate messages from workers."""
        while True:
            try:
                update: StateUpdate = self._state_queue.get_nowait()
            except Exception:
                break

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

            # Mark interrupted
            record = self._state.get(issue_id)
            if record and record.status == IssueStatus.IN_PROGRESS:
                self._state.transition(issue_id, IssueStatus.INTERRUPTED)
                self._state.save()

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

    def _post_failure_comment(self, record: IssueRecord) -> None:
        """Post a failure comment on the issue via gh CLI."""
        try:
            import subprocess
            env = os.environ.copy()
            env["MSYS_NO_PATHCONV"] = "1"
            body = redact(
                f"**auto-claude** failed after {record.retry_count} attempt(s).\n\n"
                f"> {record.error or 'Unknown error'}\n\n"
                f"_You may re-label the issue to try again._"
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
            )
        except Exception as exc:
            self._logger.error(f"Failed to post failure comment on {record.issue_id}: {exc}")
