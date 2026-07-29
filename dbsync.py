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

from db import issue_state
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
