"""reconcile.py — rebuilds issues.json from GitHub (authoritative for stage)
and Postgres auto_claude.issue_state (authoritative for counters and lease),
on every startup.

This makes the 2026-07-29 field_admin#215 failure mode impossible by
construction: a record stranded at IN_PROGRESS with no live worker used to be
invisible to the poller forever (see tests/test_shutdown_recovery.py's
docstring). Reconciliation runs every startup, deriving status from GitHub's
`ac-*` label plus Postgres's lease columns — never from the local status left
over from the previous run — so a stranded record cannot survive a restart.

Records are *constructed* with the derived status, not transition()'d into
it. `StateStore.update()` sets fields with plain setattr and never consults
VALID_TRANSITIONS (only `transition()` does) — which is what makes it safe to
use here for a jump like IN_PROGRESS -> QUEUED that VALID_TRANSITIONS does
not otherwise allow (see state.py's transition table).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from stages import LOCKED
from state import IssueRecord, IssueStatus, StateStore

STAGE_TO_STATUS: dict[str, str] = {
    "ac-pending-review": IssueStatus.DISCOVERED,
    "ac-input-needed": IssueStatus.NEEDS_INFO,
    "ac-dev-ready": IssueStatus.QUEUED,
    "ac-in-progress": IssueStatus.QUEUED,          # overridden below when leased by another
    "ac-dev-review": IssueStatus.QUEUED,
    "ac-review-in-progress": IssueStatus.QUEUED,   # overridden below when leased by another
    "ac-hitl": IssueStatus.COMPLETED,
    "ac-merged": IssueStatus.COMPLETED,
    "ac-done": IssueStatus.COMPLETED,
    "ac-blocked": IssueStatus.SKIPPED,
}

# Both queue the review worker, per poller.py's own routing.
_REVIEW_STAGES = frozenset({"ac-dev-review", "ac-review-in-progress"})


def derive_status(stage: str | None, *, lease_held_by_other: bool) -> str:
    """The IssueStatus a stage + lease state maps to — the table in
    docs/plans/12-shared-state-in-postgres.md / CONTRACT.md."""
    if stage in LOCKED and lease_held_by_other:
        return IssueStatus.IN_PROGRESS
    return STAGE_TO_STATUS.get(stage, IssueStatus.SKIPPED)


@dataclass(frozen=True)
class ReconcileReport:
    rebuilt: int
    resurrected: list[str]
    leases_released: list[str]


def _lease_held_by_other(row: dict | None, harness_id: str, now: datetime) -> bool:
    if row is None:
        return False
    owner = row.get("owner_harness_id")
    expires = row.get("lease_expires_at")
    if not owner or owner == harness_id:
        return False
    return expires is not None and expires > now


def _lease_expired(row: dict | None, now: datetime) -> bool:
    if row is None:
        return False
    owner = row.get("owner_harness_id")
    if not owner:
        return False
    expires = row.get("lease_expires_at")
    return expires is None or expires <= now


def reconcile(*, state: StateStore, db_rows: dict[str, dict],
              gh_issues: dict[str, dict], harness_id: str,
              logger) -> ReconcileReport:
    """Rebuild `state` from `gh_issues` (stage, authoritative) and `db_rows`
    (counters + lease, authoritative) — both keyed by issue_id
    ("{repo}#{number}"). `gh_issues` rows carry: stage, repo, number, title,
    body, labels, action, issue_updated_at, discovered_at (see Task 11's
    `main._collect_gh_issues_for_reconcile`). `db_rows` rows are exactly what
    `db.issue_state.fetch_all` returns."""
    now = datetime.now(timezone.utc)
    rebuilt = 0
    resurrected: list[str] = []
    leases_released: list[str] = []

    for issue_id, gh in gh_issues.items():
        db_row = db_rows.get(issue_id)
        stage = gh.get("stage")
        held_by_other = _lease_held_by_other(db_row, harness_id, now)
        status = derive_status(stage, lease_held_by_other=held_by_other)
        mode = "review" if stage in _REVIEW_STAGES else "dev"

        if stage in LOCKED and not held_by_other:
            resurrected.append(issue_id)
        if _lease_expired(db_row, now):
            leases_released.append(issue_id)

        fields = dict(
            status=status,
            mode=mode,
            labels=gh.get("labels", []),
            action=gh.get("action", "implement"),
            issue_updated_at=gh.get("issue_updated_at", ""),
        )

        existing = state.get(issue_id)
        if db_row is not None:
            # DB row present: it is authoritative for the fields it owns,
            # exactly as before.
            fields.update(
                branch=db_row.get("branch"),
                pr_url=db_row.get("pr_url"),
                triage_attempts=db_row.get("triage_attempts", 0),
                rework_count=db_row.get("rework_count", 0),
                continuation_count=db_row.get("continuation_count", 0),
                error=db_row.get("last_error"),
            )
        elif existing is not None:
            # DB row absent for *this* issue (Postgres outage, or a row that
            # simply hasn't been written yet) but we already have a local
            # record: absence carries no information, so preserve what's
            # there instead of blanking it. Overwriting with None/0 here used
            # to silently disable merge detection at ac-hitl (Poller.
            # _check_merged bails out on an empty pr_url, and ac-hitl is
            # terminal with no worker fallback to ever retry it) and defeat
            # the ac-blocked backstop by resetting exhausted attempt/rework
            # counters back to 0.
            fields.update(
                branch=existing.branch,
                pr_url=existing.pr_url,
                triage_attempts=existing.triage_attempts,
                rework_count=existing.rework_count,
                continuation_count=existing.continuation_count,
                error=existing.error,
            )
        else:
            # Genuinely new record with no DB row yet: nothing local to
            # preserve, so fall back to the current defaults.
            fields.update(
                branch=None, pr_url=None, triage_attempts=0,
                rework_count=0, continuation_count=0, error=None,
            )

        if existing is not None:
            state.update(issue_id, **fields)
        else:
            state.add(IssueRecord(
                issue_id=issue_id,
                repo=gh.get("repo", issue_id.split("#", 1)[0]),
                number=gh.get("number", 0),
                title=gh.get("title", ""),
                body=gh.get("body", ""),
                discovered_at=gh.get("discovered_at", now.isoformat()),
                updated_at=now.isoformat(),
                **fields,
            ))
        rebuilt += 1

    state.save()

    if resurrected:
        logger.warn(
            f"Reconciliation resurrected {len(resurrected)} stranded issue(s) "
            f"back to queued: {', '.join(resurrected)}"
        )
    if leases_released:
        logger.info(
            f"Reconciliation found {len(leases_released)} expired lease(s): "
            f"{', '.join(leases_released)}"
        )

    return ReconcileReport(
        rebuilt=rebuilt, resurrected=resurrected, leases_released=leases_released,
    )
