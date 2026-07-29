"""Tests for the Postgres issue lease - db/lease.py.

Guards the 2026-07-29 finding that GitHub labels cannot be a distributed
lock: `main._release_stale_locks` used to assume "at startup no worker of
ours is alive, so any bot-assigned ac-in-progress issue is stale by
definition" - an assumption a second harness breaks immediately, and
read-labels-then-write-label has no compare-and-swap to make it safe anyway
(GitHub offers none). `db/lease.py` replaces both with one atomic SQL
statement (see docs/plans/12-shared-state-in-postgres.md, "Lease protocol").

These tests exercise the Python side against a fake, in-memory Database. The
one thing a fake cannot honestly verify - that the UPDATE really is atomic
under two simultaneous connections - is tests/test_lease_concurrency.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import lease  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402


class _FakeDb:
    """In-memory stand-in for Database. Recognises lease.py's SQL by
    identity (the exact private constants it exports) so it can simulate
    the atomic claim/heartbeat/release/check/expiry semantics without a real
    connection. This is a fake of the *lease protocol*, not of SQL in
    general - it would not help test anything else in db/."""

    def __init__(self):
        # issue_id -> {"owner": str | None, "expires_at": float | None}
        self.rows: dict[str, dict] = {}

    def seed(self, issue_id, owner=None, expires_in=None):
        self.rows[issue_id] = {
            "owner": owner,
            "expires_at": (time.time() + expires_in) if expires_in is not None else None,
        }

    def _row(self, issue_id):
        return self.rows.setdefault(issue_id, {"owner": None, "expires_at": None})

    def execute(self, sql, params=()):
        now = time.time()
        if sql == lease._ACQUIRE_SQL:
            harness_id, ttl_seconds, issue_id = params
            row = self._row(issue_id)
            free = row["owner"] is None or (
                row["expires_at"] is not None and row["expires_at"] < now
            )
            if not free:
                return []
            row["owner"] = harness_id
            row["expires_at"] = now + ttl_seconds
            return [(issue_id,)]

        if sql == lease._HEARTBEAT_SQL:
            ttl_seconds, harness_id = params
            updated = []
            for issue_id, row in self.rows.items():
                if (row["owner"] == harness_id
                        and row["expires_at"] is not None
                        and row["expires_at"] >= now):
                    row["expires_at"] = now + ttl_seconds
                    updated.append((issue_id,))
            return updated

        if sql == lease._RELEASE_SQL:
            issue_id, harness_id = params
            row = self.rows.get(issue_id)
            if row and row["owner"] == harness_id:
                row["owner"] = None
                row["expires_at"] = None
            return []

        if sql == lease._CHECK_SQL:
            issue_id, harness_id = params
            row = self.rows.get(issue_id)
            if (row and row["owner"] == harness_id
                    and row["expires_at"] is not None and row["expires_at"] >= now):
                return [(1,)]
            return []

        if sql == lease._RELEASE_EXPIRED_SQL:
            freed = []
            for issue_id, row in self.rows.items():
                if (row["owner"] is not None
                        and row["expires_at"] is not None and row["expires_at"] < now):
                    row["owner"] = None
                    row["expires_at"] = None
                    freed.append((issue_id,))
            return freed

        raise AssertionError(f"unrecognised SQL passed to fake db: {sql!r}")


class _AlwaysDownDb:
    def execute(self, sql, params=()):
        raise DbUnavailable("connection refused")


class TestAcquire:
    def test_wins_a_free_issue(self):
        db = _FakeDb()
        assert lease.acquire(db, "r#1", "harness-a") is True
        assert db.rows["r#1"]["owner"] == "harness-a"

    def test_loses_to_an_unexpired_holder(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        assert lease.acquire(db, "r#1", "harness-b") is False
        assert db.rows["r#1"]["owner"] == "harness-a", "loser must not overwrite the winner"

    def test_reclaims_an_expired_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        assert lease.acquire(db, "r#1", "harness-b") is True
        assert db.rows["r#1"]["owner"] == "harness-b"

    def test_propagates_db_unavailable_rather_than_reporting_false(self):
        # A swallowed exception here would be indistinguishable from "someone
        # else holds it" - Task 13's caller needs to tell those apart: one
        # means "try again next tick", the other means "spawn elsewhere".
        with pytest.raises(DbUnavailable):
            lease.acquire(_AlwaysDownDb(), "r#1", "harness-a")


class TestHeartbeat:
    def test_extends_every_lease_this_harness_owns(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=100)
        db.seed("r#2", owner="harness-a", expires_in=100)
        db.seed("r#3", owner="harness-b", expires_in=100)

        updated = lease.heartbeat(db, "harness-a")

        assert updated == 2
        assert db.rows["r#1"]["expires_at"] > time.time() + 1000
        assert db.rows["r#3"]["expires_at"] < time.time() + 1000, \
            "must not touch another harness's lease"

    def test_does_not_resurrect_an_already_expired_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        assert lease.heartbeat(db, "harness-a") == 0


class TestRelease:
    def test_clears_our_own_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        lease.release(db, "r#1", "harness-a")
        assert db.rows["r#1"]["owner"] is None

    def test_does_not_clear_a_lease_we_no_longer_hold(self):
        # We lost the race to expiry and someone else already re-acquired -
        # an unconditional release would clear *their* lease out from under
        # them.
        db = _FakeDb()
        db.seed("r#1", owner="harness-b", expires_in=1800)
        lease.release(db, "r#1", "harness-a")
        assert db.rows["r#1"]["owner"] == "harness-b"


class TestCheck:
    def test_true_when_this_harness_holds_an_unexpired_lease(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        assert lease.check(db, "r#1", "harness-a") is True

    def test_false_when_another_harness_holds_it(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-b", expires_in=1800)
        assert lease.check(db, "r#1", "harness-a") is False

    def test_false_when_expired(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        assert lease.check(db, "r#1", "harness-a") is False

    def test_fails_closed_after_three_retries_when_db_unreachable(self):
        sleeps = []
        result = lease.check(
            _AlwaysDownDb(), "r#1", "harness-a", retries=3, sleep=sleeps.append,
        )
        assert result is False
        assert len(sleeps) == 3, "must retry exactly `retries` times before giving up"

    def test_recovers_if_the_db_comes_back_before_retries_are_exhausted(self):
        calls = {"n": 0}

        class _FlakyDb:
            def execute(self, sql, params=()):
                calls["n"] += 1
                if calls["n"] < 2:
                    raise DbUnavailable("still down")
                return [(1,)]

        held = lease.check(_FlakyDb(), "r#1", "harness-a", retries=3, sleep=lambda s: None)
        assert held is True


class TestReleaseExpired:
    def test_frees_only_leases_past_their_expiry(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)   # expired
        db.seed("r#2", owner="harness-b", expires_in=1800)  # live
        db.seed("r#3", owner=None)                            # never leased

        freed = lease.release_expired(db)

        assert freed == ["r#1"]
        assert db.rows["r#1"]["owner"] is None
        assert db.rows["r#2"]["owner"] == "harness-b", "must not touch a live lease"


class TestAcquireOnUnregisteredHarness:
    """`owner_harness_id` carries a real FK to `auto_claude.harness(id)`
    (`ON DELETE SET NULL`) - discovered when the controller ran
    tests/test_lease_concurrency.py against a real database and every
    lease.acquire call for a bare uuid4 harness id failed with
    `psycopg.errors.ForeignKeyViolation`, because nothing had registered
    that id in auto_claude.harness first.

    That FK is correct and load-bearing, not a bug: db/lease.py itself never
    registers harnesses (db/harness.py does, at startup - Task 11), so an
    unregistered harness id reaching acquire() is a caller error, not a lost
    race. This pins that `acquire` lets such an error propagate rather than
    reporting it as False (indistinguishable from "someone else holds the
    lease") or as DbUnavailable (would trigger a retry that can never
    succeed).

    Note this is documentation, not a regression guard: `acquire` has no
    `except` clause at all, so *any* exception from `db.execute` propagates
    by construction - this test cannot fail short of someone adding a
    swallowing `try/except` to `acquire` itself."""

    def test_an_integrity_error_propagates_rather_than_being_reported_as_false(self):
        class _IntegrityErrorDb:
            def execute(self, sql, params=()):
                raise Exception(
                    "simulated ForeignKeyViolation: harness id not registered"
                )

        with pytest.raises(Exception, match="ForeignKeyViolation"):
            lease.acquire(_IntegrityErrorDb(), "r#1", "unregistered-harness")


class TestOwnerAndExpiryAreAlwaysWrittenTogether:
    """Pins the resolution to the two-readings conflict between reconcile.py
    (treats owner-set/expiry-NULL as reclaimable) and the design doc's
    illustrative SQL (`owner_harness_id IS NULL OR lease_expires_at < now()`,
    where `NULL < now()` is unknown, so that same row would NOT match and
    would be treated as un-reclaimable).

    Resolved here by making the disputed shape unreachable: every write this
    module makes to owner_harness_id sets lease_expires_at in the same
    statement, and clearing one always clears the other. If that holds,
    which reading reconcile.py picks for the shape no longer matters because
    the shape never occurs in a database only db/lease.py has written to.
    """

    def _assert_owner_and_expiry_are_in_lockstep(self, db):
        for issue_id, row in db.rows.items():
            has_owner = row["owner"] is not None
            has_expiry = row["expires_at"] is not None
            assert has_owner == has_expiry, (
                f"{issue_id}: owner={row['owner']!r} expires_at={row['expires_at']!r} "
                "- owner_harness_id and lease_expires_at must never diverge"
            )

    def test_acquire_sets_both_columns_in_the_same_call(self):
        db = _FakeDb()
        lease.acquire(db, "r#1", "harness-a")
        self._assert_owner_and_expiry_are_in_lockstep(db)

    def test_release_clears_both_columns_in_the_same_call(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=1800)
        lease.release(db, "r#1", "harness-a")
        self._assert_owner_and_expiry_are_in_lockstep(db)

    def test_release_expired_clears_both_columns_in_the_same_call(self):
        db = _FakeDb()
        db.seed("r#1", owner="harness-a", expires_in=-10)
        lease.release_expired(db)
        self._assert_owner_and_expiry_are_in_lockstep(db)

    def test_never_produces_an_owner_set_with_a_null_expiry(self):
        # The exact shape reconcile.py's TestNullExpiryLeaseShape documents:
        # if db/lease.py ever wrote it, the two readings would disagree.
        db = _FakeDb()
        lease.acquire(db, "r#1", "harness-a")
        lease.release_expired(db)  # not expired yet - no-op
        lease.release(db, "r#1", "harness-a")
        for row in db.rows.values():
            assert not (row["owner"] is not None and row["expires_at"] is None)


def _set_clause(sql: str) -> str:
    return sql[sql.index("SET"): sql.index("WHERE")]


def _where_clause(sql: str) -> str:
    start = sql.index("WHERE")
    end = sql.index("RETURNING") if "RETURNING" in sql else len(sql)
    return sql[start:end]


class TestLeaseColumnInvariantIsPinnedInSQL:
    """`TestOwnerAndExpiryAreAlwaysWrittenTogether`, above, only proves that
    `_FakeDb`'s hand-written simulation keeps `owner`/`expires_at` in
    lockstep - it dispatches on `sql == lease._ACQUIRE_SQL` by *identity*
    and then runs its own hardcoded logic, completely independent of what
    that SQL text actually sets. A future edit that dropped
    `lease_expires_at = ...` from `_ACQUIRE_SQL`'s SET clause would leave
    every test in that class still green, because the fake never re-parses
    the real statement - exactly the kind of silent regression this
    invariant exists to prevent (review finding, fix round 2).

    These assert directly against the SQL constants' text instead, so
    dropping either column from a SET or WHERE clause trips a hermetic test
    with no database required - the same style of protection
    tests/test_db_issue_state.py already relies on for `issue_state.upsert`.
    """

    def test_acquire_sets_owner_and_expiry_together(self):
        set_clause = _set_clause(lease._ACQUIRE_SQL)
        assert "owner_harness_id" in set_clause
        assert "lease_expires_at" in set_clause

    def test_release_clears_owner_and_expiry_together(self):
        set_clause = _set_clause(lease._RELEASE_SQL)
        assert "owner_harness_id" in set_clause
        assert "lease_expires_at" in set_clause

    def test_release_expired_clears_owner_and_expiry_together(self):
        set_clause = _set_clause(lease._RELEASE_EXPIRED_SQL)
        assert "owner_harness_id" in set_clause
        assert "lease_expires_at" in set_clause

    def test_heartbeat_guards_on_both_owner_and_unexpired_lease(self):
        # heartbeat never writes owner_harness_id - it only extends the
        # expiry of a lease that already has an owner - so its protection
        # against the disputed shape lives in its WHERE guard, not a SET
        # clause: it can only touch a row that already has owner_harness_id
        # set AND an unexpired lease_expires_at, which is what stops it
        # from ever resurrecting (or producing) the owner-set/expiry-NULL
        # shape.
        where_clause = _where_clause(lease._HEARTBEAT_SQL)
        assert "owner_harness_id = %s" in where_clause
        assert "lease_expires_at >= now()" in where_clause
