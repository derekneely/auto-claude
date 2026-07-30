# dbsync.py
"""dbsync.py — the single seam `main`, `process_manager` and `worker` use to
reach Postgres.

Introduced here, at the first task that needs it, with only the methods
whose backing module already exists (`db/pool.py`, `db/harness.py`,
`db/issue_state.py`). It grows module-by-module as later tasks land — see
the plan's "Sequencing and the `dbsync` dependency" table — without any
earlier caller ever having to change how it constructs or calls this class.

At this task, a durable write that cannot reach Postgres is logged and
dropped, not journaled: `db/journal.py` does not exist until Task 20. That is
safe because GitHub labels remain truth and startup reconciliation (Tasks
10-11) rebuilds `issues.json` from scratch on every restart regardless. Task
21 upgrades this method to journal instead, without changing this
signature — `journal` is already accepted and stored for exactly that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from db import history
from db import issue_state
from db import lease as db_lease
from db.harness import Harness
from db.pool import Database, DbUnavailable
from state import IssueRecord

if TYPE_CHECKING:
    # db/journal.py does not exist until Task 21 — guard the import so this
    # module never fails to import in the meantime. `journal` is typed
    # loosely (see below) so no runtime reference to `Journal` is needed.
    from db.journal import Journal


class DbSync:
    """Postgres access for the harness. At this task, durable writes that
    fail are logged and dropped; Task 21 upgrades that to a real journal."""

    def __init__(self, db: Database | None, harness: Harness, logger, *,
                 journal: "Journal | None" = None, ttl_seconds: int = 1800) -> None:
        self._db = db
        self._harness = harness
        self._logger = logger
        # Accepted now so later tasks never change this signature again:
        # `journal` is wired for real by Task 21 (replacing the log-and-drop
        # sink below with `journal.append(...)`); `ttl_seconds` is read by
        # Task 13's acquire_lease/heartbeat once db/lease.py exists.
        self._journal = journal
        self._ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._db is not None

    # ------------------------------------------------------------------
    # Durable writes — never raise
    # ------------------------------------------------------------------

    def upsert_issue(self, record: IssueRecord, stage: str | None) -> None:
        payload = dict(
            issue_id=record.issue_id, repo=record.repo, number=record.number,
            title=record.title, stage=stage, kind=record.action, mode=record.mode,
            branch=record.branch, pr_url=record.pr_url,
            triage_attempts=record.triage_attempts, rework_count=record.rework_count,
            continuation_count=record.continuation_count, last_error=record.error,
        )
        self._durable("issue_state.upsert", payload, lambda: issue_state.upsert(self._db, **payload))

    def start_run(self, *, run_id: str, issue_id: str, mode: str, model: str | None) -> None:
        payload = dict(
            run_id=run_id, issue_id=issue_id, harness_id=self._harness.id,
            mode=mode, model=model,
        )
        self._durable("history.start_run", payload, lambda: history.start_run(self._db, **payload))

    def finish_run(self, *, run_id: str, outcome: str, exit_code: int | None,
                   duration_seconds: int | None, cost_usd: float | None, turns: int | None,
                   crash_log_path: str | None) -> None:
        payload = dict(
            run_id=run_id, outcome=outcome, exit_code=exit_code,
            duration_seconds=duration_seconds, cost_usd=cost_usd, turns=turns,
            crash_log_path=crash_log_path,
        )
        self._durable("history.finish_run", payload, lambda: history.finish_run(self._db, **payload))

    def _durable(self, op: str, payload: dict, call) -> None:
        """Run a durable write. Never raises — logs and drops on failure.

        Safe because GitHub labels stay truth and startup reconciliation
        rebuilds `issues.json` from scratch every restart. Task 21 replaces
        the two `self._logger.warn(...)` branches below with
        `self._journal.append(op, payload)`, once `db/journal.py` exists —
        `payload` is already the exact dict a journal entry needs.
        """
        if self._db is None:
            self._logger.warn(f"No database configured — dropping {op}")
            return
        try:
            call()
        except DbUnavailable as exc:
            self._logger.warn(f"Postgres unreachable — dropping {op} (not journaled yet): {exc}")
        except Exception as exc:
            self._logger.error(f"Durable write {op} failed (not journaled): {exc}")

    # ------------------------------------------------------------------
    # Lease operations - NEVER journal, NEVER queue (spec: "Claims and fence
    # checks never queue" - do not "helpfully" route these through
    # db/journal.py the way `_durable` above does for issue_state writes).
    # Each is a thin pass-through to db.lease. Two entirely different
    # database states, both handled here, must not be conflated:
    #
    #   * DISABLED (self._db is None, `enabled` is False): permissive on
    #     every method — acquire_lease/check_lease return True,
    #     heartbeat/release_lease are silent no-ops, release_expired
    #     returns []. No shared database means no second harness to
    #     coordinate with, so a lease call must behave as if uncontested,
    #     never as if blocked.
    #
    #   * UNREACHABLE (self._db is set but Postgres cannot be reached — the
    #     opposite case, and it must fail the opposite way): `check_lease`
    #     is the only one of the five that fails closed itself — db.lease's
    #     `check` retries internally and returns False rather than raising,
    #     since the fencing caller cannot tell "partitioned" from "lost the
    #     race" and must refuse the irreversible act either way. The other
    #     four (acquire_lease, heartbeat, release_lease, release_expired)
    #     let `DbUnavailable` propagate uncaught, exactly as db/lease.py's
    #     own functions do — the caller, not this seam, decides what an
    #     unreachable database means for it. As of the fix in this task's
    #     review round, those callers are: `ProcessManager._lease_ok`
    #     (acquire_lease → treat as lease denied, skip this spawn),
    #     `ProcessManager.reap_dead` (release_lease → warn, rely on TTL
    #     expiry), and `main._maybe_heartbeat` (heartbeat → warn, keep
    #     polling) — all fail-closed-but-non-fatal, per "a running Claude
    #     agent is never aborted for a lost lease or a database outage".
    #     `release_expired` is deliberately left to propagate all the way
    #     out of `main._reconcile_at_startup`, because startup is stricter
    #     than runtime by design (see `main._init_db_layer`).
    #
    # `ttl_seconds` (Task 8's constructor, `config.database.lease_ttl_seconds`
    # via Task 11) is threaded through acquire_lease/heartbeat here — this is
    # the only place it is ever read.
    # ------------------------------------------------------------------

    def acquire_lease(self, issue_id: str) -> bool:
        if not self.enabled:
            return True
        return db_lease.acquire(self._db, issue_id, self._harness.id, self._ttl_seconds)

    def heartbeat(self) -> None:
        if not self.enabled:
            return
        db_lease.heartbeat(self._db, self._harness.id, self._ttl_seconds)

    def release_lease(self, issue_id: str) -> None:
        if not self.enabled:
            return
        db_lease.release(self._db, issue_id, self._harness.id)

    def check_lease(self, issue_id: str) -> bool:
        if not self.enabled:
            return True
        return db_lease.check(self._db, issue_id, self._harness.id)

    def release_expired(self) -> list[str]:
        if not self.enabled:
            return []
        return db_lease.release_expired(self._db)
