# dbsync.py
"""dbsync.py — the single seam `main`, `process_manager` and `worker` use to
reach Postgres.

Covers issue_state upserts, run/summary history, the harness lease, and
journal replay — see `db/issue_state.py`, `db/history.py`, `db/lease.py` and
`db/journal.py` for the modules each group of methods below is a thin,
never-raising wrapper over.

A durable write that cannot reach Postgres (`DbUnavailable`) is journaled to
`db/journal.py` instead of being dropped (Task 21); `main.py`'s poll loop
calls `replay_pending()` to drain that journal once Postgres is reachable
again. GitHub labels remain truth in the meantime, and startup reconciliation
(Tasks 10-11) rebuilds `issues.json` from scratch on every restart regardless,
so a pending journal is a durability optimisation, never a correctness
requirement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from db import harness as db_harness
from db import history
from db import issue_state
from db import lease
from db.harness import Harness
from db.pool import Database, DbUnavailable
from redact import redact
from state import IssueRecord

if TYPE_CHECKING:
    # Only needed for the type hint below — no runtime reference to
    # `Journal` is required, so this stays lazy even though db/journal.py
    # exists now.
    from db.journal import Journal


class DbSync:
    """Postgres access for the harness. Durable writes that cannot reach
    Postgres are journaled (see `_durable`) and replayed by `replay_pending`
    once it is reachable again; lease operations never journal."""

    def __init__(self, db: Database | None, harness: Harness, logger, *,
                 journal: "Journal", ttl_seconds: int = 1800) -> None:
        self._db = db
        self._harness = harness
        self._logger = logger
        # Required, not optional: `_durable`'s DbUnavailable branch always
        # dereferences `self._journal` (see below). A `journal=None` default
        # used to be accepted here as a placeholder for Task 21's wiring;
        # now that the wiring is real, an omitted journal is a construction
        # bug, not a supported degraded mode — fail at construction time
        # (fix round, Finding 2), not with an AttributeError deep inside a
        # failed write. `ttl_seconds` is read by acquire_lease/heartbeat
        # (db/lease.py).
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

    def add_summary(self, *, issue_id: str, run_id: str | None, kind: str,
                    body: str, comment_url: str | None = None) -> str:
        # Generated up front, not inside _durable: the caller (and the
        # log line, if this ends up dropped) must get back the same id that
        # will eventually land in Postgres, whether that happens now or via
        # a later journal replay once Postgres is reachable again.
        summary_id = history.new_id()
        payload = dict(
            summary_id=summary_id, issue_id=issue_id, run_id=run_id,
            # Redacted HERE rather than trusting each caller, because Postgres
            # is a second destination this content escapes to and the callers
            # do not agree about it. `worker._post_pr_review` redacts only its
            # `--body` argument, so GitHub got the scrubbed copy while the
            # caller kept the raw `request_body` for its summary row — and on
            # the request-changes path that body carries `checks_transcript`,
            # raw verify/test output, which is exactly where a leaked env var
            # surfaces. The other five kinds arrive already redacted (see
            # `_issue_report`, `_post_crash_comment`, `_post_budget_comment`,
            # `triage.format_clarifying_comment`); `redact` is idempotent, so
            # scrubbing them again is a no-op. Doing it at this single seam
            # makes the property structural: a future summary kind cannot
            # forget.
            kind=kind, body=redact(body), comment_url=comment_url,
        )
        self._durable(
            "history.add_summary", payload, lambda: history.add_summary(self._db, **payload)
        )
        return summary_id

    def _durable(self, op: str, payload: dict, call) -> None:
        """Run a durable write. Journal it on DbUnavailable; log and drop it
        on any other error, since journaling a write that will never succeed
        (a bad payload, a constraint violation) would retry it forever
        against the same broken data every time the journal replays.

        A permanently disabled/unconfigured database (`self._db is None` for
        the whole process lifetime) also logs and drops rather than
        journaling (fix round, Finding 3 — reversing the first pass at this
        task, which journaled here too): `replay_pending` is a no-op in that
        same state, since there is nothing to replay into, so a journal
        entry made here would never drain — `state/journal.jsonl` would grow
        forever for the life of any installation that runs in
        local-state-only mode. GitHub labels stay truth and startup
        reconciliation rebuilds `issues.json` from scratch every restart, so
        dropping here is safe, exactly as it was before this task.
        """
        if self._db is None:
            # info, not warn: with no database configured at all this is the
            # expected, supported steady state (single-harness mode), not a
            # transient problem — every state transition would otherwise
            # warn forever for the life of an installation that never
            # configures Postgres.
            self._logger.info(f"No database configured — dropping {op}")
            return
        try:
            call()
        except DbUnavailable as exc:
            self._logger.warn(f"Postgres unreachable — journaling {op}: {exc}")
            try:
                self._journal.append(op, payload)
            except Exception as journal_exc:
                # A journal we cannot write to (disk full, a permissions
                # failure, a Windows file lock) is exactly the log-and-drop
                # case: the write is lost, but `_durable`'s "never raise"
                # contract must survive this too, not just a DbUnavailable
                # from Postgres (fix round, Finding 2).
                self._logger.error(
                    f"Could not journal {op} after Postgres became "
                    f"unreachable (dropped): {journal_exc}"
                )
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
        return lease.acquire(self._db, issue_id, self._harness.id, self._ttl_seconds)

    def heartbeat(self) -> None:
        if not self.enabled:
            return
        lease.heartbeat(self._db, self._harness.id, self._ttl_seconds)

    def release_lease(self, issue_id: str) -> None:
        if not self.enabled:
            return
        lease.release(self._db, issue_id, self._harness.id)

    def check_lease(self, issue_id: str) -> bool:
        if not self.enabled:
            return True
        return lease.check(self._db, issue_id, self._harness.id)

    def release_expired(self) -> list[str]:
        if not self.enabled:
            return []
        return lease.release_expired(self._db)

    def touch_harness(self) -> None:
        """Bump this harness row's `last_seen_at`. Best-effort, called from
        `main._maybe_heartbeat` on the same cadence as the lease heartbeat
        so "is this harness alive" (docs/plans/12-shared-state-in-postgres.md)
        has a real, moving answer instead of only ever advancing at startup
        via `register`'s `ON CONFLICT` — `db/harness.py`'s `touch` had no
        caller at all until this. A no-op when Postgres is disabled, and
        propagates `DbUnavailable` uncaught exactly like `heartbeat` above —
        `_maybe_heartbeat` already catches it there, so this durable write
        cannot abort the poll loop either.
        """
        if not self.enabled:
            return
        db_harness.touch(self._db, self._harness.id)

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay_pending(self) -> int:
        if self._db is None:
            return 0
        try:
            return self._journal.replay(self._db)
        except DbUnavailable as exc:
            self._logger.warn(f"Replay stopped — Postgres unreachable again: {exc}")
            return 0
        except Exception as exc:
            # Journal.replay deliberately lets a handler failure (an FK/
            # constraint violation on a queued entry) or an OSError from its
            # own file read/truncate propagate uncaught, so the journal file
            # stays byte-for-byte intact on that path (see db/journal.py).
            # Before this fix (Finding 1), anything other than DbUnavailable
            # escaped from here into main.py's poll loop, whose only handler
            # is `except KeyboardInterrupt` — killing the whole supervisor
            # with live workers attached, and on every restart thereafter,
            # since the untouched journal re-raises the same entry on the
            # very next replay. Swallowing means a poison entry logs one
            # ERROR per tick instead of draining — strictly better than
            # daemon death. Quarantining bad entries is Journal's job, not
            # this seam's.
            self._logger.error(f"Journal replay failed (journal left intact): {exc}")
            return 0
