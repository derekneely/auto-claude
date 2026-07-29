"""Lease concurrency - the one guarantee a fake Database cannot honestly
verify: that `lease.acquire`'s single UPDATE ... RETURNING is genuinely
atomic under two simultaneous claims.

Runs against a real Postgres. A Python-level fake would pass even a broken,
non-atomic implementation (e.g. a naive SELECT-then-UPDATE) by construction,
because Python threads serialise around the GIL between I/O waits in a way
that happens to look atomic. Real network round-trips to Postgres do not.
Skipped by default - AUTO_CLAUDE_TEST_DATABASE_URL must point at a
throwaway schema; CI or a developer wires it up deliberately. See
docs/plans/12-shared-state-in-postgres.md, "Testing": "the atomic claim is
the one thing a fake cannot honestly verify."

This file is the ONLY place in the repo permitted to touch a real database
(see docs/plans/13-shared-state-implementation.md). It creates its own
throwaway `issue_state` rows, keyed by a random uuid so it can never collide
with real pipeline data, and removes them in a `finally` regardless of
outcome. It never touches `public.pipeline_events` - the connection URL an
operator supplies here may be the same shared Postgres instance the sibling
Node toolchain (field_admin's pipeline metrics) also uses, and a test run
must leave no residue behind in either schema.

Also proves the second load-bearing invariant this task carries: that
db.issue_state.upsert (Task 7) genuinely cannot clobber a live lease. The
unit-level test in tests/test_db_issue_state.py only inspects the SQL text;
a real behavioural proof needs a real database, which is why it lives here.
"""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import issue_state, lease  # noqa: E402
from db.pool import Database  # noqa: E402

TEST_DB_URL_ENV = "AUTO_CLAUDE_TEST_DATABASE_URL"

pytestmark = pytest.mark.postgres


def _require_test_db_url() -> str:
    url = os.environ.get(TEST_DB_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_URL_ENV} not set - skipping real-Postgres lease test")
    return url


@pytest.fixture
def db():
    database = Database(_require_test_db_url())
    yield database
    database.close()


@pytest.fixture
def issue_id(db):
    """A throwaway issue_state row this test owns end to end."""
    new_id = f"test#{uuid.uuid4().hex}"
    db.execute(
        "INSERT INTO auto_claude.issue_state (issue_id, repo, number, title) "
        "VALUES (%s, %s, %s, %s)",
        (new_id, "test-repo", 1, "lease concurrency test"),
    )
    try:
        yield new_id
    finally:
        db.execute("DELETE FROM auto_claude.issue_state WHERE issue_id = %s", (new_id,))


class TestConcurrentAcquire:
    def test_exactly_one_of_two_simultaneous_claims_wins(self, issue_id):
        """Two harnesses race to claim a fresh issue. Exactly one must win -
        the failure mode this guards is two dev workers both spawning on the
        same issue, which the label-based lock could not prevent."""
        url = _require_test_db_url()
        harness_a, harness_b = uuid.uuid4().hex, uuid.uuid4().hex

        def claim(harness_id: str) -> bool:
            # Each thread opens its own connection - sharing one Database
            # across threads would serialise the two claims through a
            # single session and prove nothing about cross-connection
            # atomicity, which is exactly what is under test.
            database = Database(url)
            try:
                return lease.acquire(database, issue_id, harness_id)
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(claim, harness_a)
            future_b = pool.submit(claim, harness_b)
            result_a = future_a.result()
            result_b = future_b.result()

        assert [result_a, result_b].count(True) == 1, (
            f"expected exactly one winner, got a={result_a} b={result_b}"
        )

    def test_loser_sees_the_winners_harness_id(self, db, issue_id):
        harness_a, harness_b = uuid.uuid4().hex, uuid.uuid4().hex
        assert lease.acquire(db, issue_id, harness_a) is True
        assert lease.acquire(db, issue_id, harness_b) is False

        rows = db.execute(
            "SELECT owner_harness_id FROM auto_claude.issue_state WHERE issue_id = %s",
            (issue_id,),
        )
        assert rows[0][0] == harness_a


class TestUpsertCannotClobberALiveLease:
    """Behavioural proof for db.issue_state.upsert (Task 7): a state write
    that legitimately updates counters/branch/etc must never touch the lease
    columns of a row another harness currently holds. Task 7's own test only
    asserts the SQL text excludes the lease columns from the INSERT/SET
    clauses - that proves the statement can't, in principle, clobber them,
    but not that a real row with a live lease genuinely survives an upsert
    unchanged. This is that proof, and it needs a real database because the
    thing under test is Postgres's own read of the row before and after."""

    def test_upsert_leaves_owner_expiry_and_heartbeat_untouched(self, db, issue_id):
        holder = uuid.uuid4().hex
        assert lease.acquire(db, issue_id, holder) is True

        before = issue_state.fetch(db, issue_id)
        assert before["owner_harness_id"] == holder
        assert before["lease_expires_at"] is not None
        assert before["heartbeat_at"] is not None

        issue_state.upsert(
            db,
            issue_id=issue_id, repo="test-repo", number=1,
            title="lease concurrency test - updated by upsert",
            stage="ac-in-progress", kind="fix", mode="dev",
            branch="fix/whatever", pr_url=None,
            triage_attempts=1, rework_count=0, continuation_count=0,
            last_error=None,
        )

        after = issue_state.fetch(db, issue_id)
        assert after["title"] == "lease concurrency test - updated by upsert", (
            "sanity check that the upsert actually wrote something"
        )
        assert after["owner_harness_id"] == before["owner_harness_id"]
        assert after["lease_expires_at"] == before["lease_expires_at"]
        assert after["heartbeat_at"] == before["heartbeat_at"]
