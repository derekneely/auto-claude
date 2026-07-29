"""auto_claude.issue_state lease: a per-issue distributed lock inlined into
the row it protects, rather than a separate table - so the claim is one
atomic single-row UPDATE with nothing to join (see
docs/plans/12-shared-state-in-postgres.md, "Lease protocol").

`acquire` is the spec's exact statement, parameterised only on the TTL. Zero
rows returned means someone else holds an unexpired lease - that is the
entire locking protocol. This exists because GitHub labels cannot be a lock:
read-labels-then-write-label is a check-then-act race with no compare-and-
swap, so two harnesses would eventually both claim the same issue.

Invariant enforced by every statement below: owner_harness_id and
lease_expires_at are always written together, never one without the other.
`acquire` sets both; `release` and `release_expired` clear both. This is
what makes the (owner set, expiry NULL) row shape unreachable - a shape
that reconcile.py and the design doc's illustrative WHERE clause would
otherwise disagree about (NULL < now() is unknown in SQL, so that clause
alone would treat such a row as still held). See
tests/test_db_lease.py::TestOwnerAndExpiryAreAlwaysWrittenTogether and
tests/test_reconcile.py::TestNullExpiryLeaseShape.
"""

from __future__ import annotations

import time

from db.pool import Database, DbUnavailable

LEASE_TTL_SECONDS: int = 1800          # 30 minutes
HEARTBEAT_INTERVAL_SECONDS: int = 60

# Total wall-clock budget for `check`'s fail-closed retry, spread across
# `retries` attempts after the first. ~5s per the spec ("lease.check retries
# 3x over ~5s").
_CHECK_RETRY_BUDGET_SECONDS = 5.0

_ACQUIRE_SQL = """
UPDATE auto_claude.issue_state
   SET owner_harness_id = %s,
       lease_expires_at = now() + make_interval(secs => %s),
       heartbeat_at     = now()
 WHERE issue_id = %s
   AND (owner_harness_id IS NULL OR lease_expires_at < now())
RETURNING issue_id
"""

_HEARTBEAT_SQL = """
UPDATE auto_claude.issue_state
   SET lease_expires_at = now() + make_interval(secs => %s),
       heartbeat_at     = now()
 WHERE owner_harness_id = %s
   AND lease_expires_at >= now()
RETURNING issue_id
"""

_RELEASE_SQL = """
UPDATE auto_claude.issue_state
   SET owner_harness_id = NULL,
       lease_expires_at = NULL,
       heartbeat_at     = NULL
 WHERE issue_id = %s
   AND owner_harness_id = %s
"""

_CHECK_SQL = """
SELECT 1 FROM auto_claude.issue_state
 WHERE issue_id = %s
   AND owner_harness_id = %s
   AND lease_expires_at >= now()
"""

_RELEASE_EXPIRED_SQL = """
UPDATE auto_claude.issue_state
   SET owner_harness_id = NULL,
       lease_expires_at = NULL,
       heartbeat_at     = NULL
 WHERE owner_harness_id IS NOT NULL
   AND lease_expires_at < now()
RETURNING issue_id
"""


def acquire(db: Database, issue_id: str, harness_id: str,
            ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
    """Claim the lease on `issue_id` for `harness_id`. True iff we now hold it.

    Fails CLOSED: `DbUnavailable` propagates uncaught rather than being
    reported as False. A swallowed exception here would be indistinguishable
    from "someone else holds it", and the two demand different responses
    from a caller - retry shortly, versus move on to the next issue.
    """
    rows = db.execute(_ACQUIRE_SQL, (harness_id, ttl_seconds, issue_id))
    return len(rows) == 1


def heartbeat(db: Database, harness_id: str,
              ttl_seconds: int = LEASE_TTL_SECONDS) -> int:
    """Extend every unexpired lease `harness_id` owns. Returns rows updated.

    Excludes already-expired rows on purpose (`lease_expires_at >= now()`):
    a harness that stalled past the TTL must not resurrect a lease another
    harness may have already reclaimed by heartbeating its way back in - it
    has to `acquire` again like anyone else.
    """
    rows = db.execute(_HEARTBEAT_SQL, (ttl_seconds, harness_id))
    return len(rows)


def release(db: Database, issue_id: str, harness_id: str) -> None:
    """Release the lease, but only if `harness_id` still holds it.

    The `owner_harness_id = %s` guard matters: if our lease already expired
    and someone else re-acquired it, an unconditional release would clear
    *their* lease out from under them. A worker that finishes after losing
    that race simply no-ops here.
    """
    db.execute(_RELEASE_SQL, (issue_id, harness_id))


def check(db: Database, issue_id: str, harness_id: str,
          *, retries: int = 3, sleep=time.sleep) -> bool:
    """True iff `harness_id` still holds an unexpired lease on `issue_id`.

    Fails CLOSED, unlike every other function here: an unreachable database
    is retried `retries` times over ~5s and then reported as False rather
    than re-raised. This is deliberate and is the *only* place in this
    module that swallows `DbUnavailable` - the fencing caller (Task 14's
    `worker._assert_lease_held`) cannot distinguish "this box is
    partitioned" from "someone else legitimately took over", and refusing
    the irreversible act is the safe response to both.
    """
    delay = _CHECK_RETRY_BUDGET_SECONDS / retries if retries else 0
    for attempt in range(retries + 1):
        try:
            rows = db.execute(_CHECK_SQL, (issue_id, harness_id))
            return len(rows) == 1
        except DbUnavailable:
            if attempt >= retries:
                return False
            sleep(delay)
    return False  # pragma: no cover - unreachable, loop always returns above


def release_expired(db: Database) -> list[str]:
    """Clear every lease past its `lease_expires_at`. Returns issue_ids freed."""
    rows = db.execute(_RELEASE_EXPIRED_SQL)
    return [row[0] for row in rows]
