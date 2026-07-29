"""Tests for db/pool.py — Database's retry-then-fail and reconnect behavior.

No real Postgres involved anywhere in this file: `connect` is injected, so
retry and reconnect logic is exercised entirely with fakes, per the house
rule ("No test touches a real DB, network or subprocess", CONTRACT.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.pool import Database, DbUnavailable  # noqa: E402


class FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description

    def execute(self, sql, params):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """`fail_times` cursor() calls raise OperationalError before succeeding —
    simulates a connection that answers but whose query keeps resetting."""

    def __init__(self, rows=(), description=(("col",),), fail_times=0):
        self.closed = False
        self._rows = rows
        self._description = description
        self._fail_times = fail_times
        self._calls = 0

    def cursor(self):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise psycopg.OperationalError("connection reset")
        return FakeCursor(self._rows, self._description)

    def close(self):
        self.closed = True


def _connect_returning(conn):
    def _connect(url, **kwargs):
        return conn
    return _connect


class TestDatabaseExecute:
    def test_returns_rows_from_a_select(self):
        conn = FakeConnection(rows=[(1, "a"), (2, "b")])
        db = Database("postgresql://x", connect=_connect_returning(conn))
        assert db.execute("SELECT * FROM t") == [(1, "a"), (2, "b")]

    def test_returns_empty_list_for_a_statement_with_no_result_set(self):
        conn = FakeConnection(rows=[], description=None)
        db = Database("postgresql://x", connect=_connect_returning(conn))
        assert db.execute("UPDATE t SET x = 1") == []


class TestDatabaseRetries:
    def test_retries_on_operational_error_then_succeeds(self):
        conn = FakeConnection(rows=[(1,)], fail_times=1)
        sleeps = []
        db = Database("postgresql://x", connect=_connect_returning(conn),
                       retries=2, sleep=lambda s: sleeps.append(s))
        assert db.execute("SELECT 1") == [(1,)]
        assert sleeps, "must sleep between retries"

    def test_raises_db_unavailable_after_exhausting_retries(self):
        conn = FakeConnection(fail_times=999)
        db = Database("postgresql://x", connect=_connect_returning(conn),
                       retries=2, sleep=lambda s: None)
        with pytest.raises(DbUnavailable):
            db.execute("SELECT 1")


class TestDatabaseReconnects:
    def test_reconnects_after_a_dropped_connection(self):
        conns = [FakeConnection(rows=[(1,)]), FakeConnection(rows=[(2,)])]

        def connect(url, **kwargs):
            return conns.pop(0)

        db = Database("postgresql://x", connect=connect)
        assert db.execute("SELECT 1") == [(1,)]

        # Simulate the pooler recycling the connection between calls —
        # Database must notice `.closed` and open a new one rather than
        # reusing a dead socket.
        db._conn.closed = True

        assert db.execute("SELECT 1") == [(2,)]


class TestDatabaseClose:
    def test_close_before_any_connection_is_a_safe_noop(self):
        db = Database("postgresql://x", connect=_connect_returning(FakeConnection()))
        db.close()
        assert db._conn is None

    def test_close_after_use_marks_the_connection_closed(self):
        conn = FakeConnection(rows=[(1,)])
        db = Database("postgresql://x", connect=_connect_returning(conn))
        db.execute("SELECT 1")
        db.close()
        assert conn.closed
        assert db._conn is None
