# tests/test_db_harness.py
"""Tests for db/harness.py — the identity row every lease, run and summary
foreign-keys to. `register` must be an upsert, not a bare INSERT: the same
process id can legitimately reappear (a restart on the same box reuses a
recycled PID), and register() runs on every startup, not just the first.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.harness import Harness, new_harness, register, touch  # noqa: E402


class FakeDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return []


class TestNewHarness:
    def test_generates_a_fresh_uuid4_hex_id_each_call(self):
        h1 = new_harness("0.2.0")
        h2 = new_harness("0.2.0")
        assert h1.id != h2.id
        assert len(h1.id) == 32  # uuid4().hex has no dashes

    def test_captures_hostname_pid_and_version(self):
        h = new_harness("0.2.0")
        assert h.hostname == socket.gethostname()
        assert h.pid == os.getpid()
        assert h.version == "0.2.0"


class TestRegister:
    def test_inserts_with_all_four_fields(self):
        db = FakeDatabase()
        h = Harness(id="abc123", hostname="box1", pid=42, version="0.2.0")
        register(db, h)
        sql, params = db.calls[0]
        assert "INSERT INTO auto_claude.harness" in sql
        assert params == ("abc123", "box1", 42, "0.2.0")

    def test_upserts_on_conflict_rather_than_failing_on_restart(self):
        db = FakeDatabase()
        register(db, Harness(id="abc123", hostname="box1", pid=42, version="0.2.0"))
        assert "ON CONFLICT (id) DO UPDATE" in db.calls[0][0]


class TestTouch:
    def test_updates_last_seen_at_for_the_given_id(self):
        db = FakeDatabase()
        touch(db, "abc123")
        sql, params = db.calls[0]
        assert "UPDATE auto_claude.harness" in sql
        assert params == ("abc123",)
