"""Tests for db/history.py — run and summary rows must survive journal replay.

Every insert here uses ON CONFLICT (id) DO NOTHING and every update is a
plain last-writer-wins SET. The specific bug this guards: if start_run or
add_summary ever used a naive INSERT, replaying a journaled write a second
time (see db/journal.py) would raise a duplicate-key error and abort the
whole replay batch instead of silently no-op'ing on the row that already
made it to Postgres before the connection dropped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import history  # noqa: E402


class FakeDatabase:
    """Records every execute() call; never touches a real connection."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return []


class TestNewId:
    def test_returns_a_32_char_hex_string(self):
        value = history.new_id()
        assert len(value) == 32
        assert all(c in "0123456789abcdef" for c in value)

    def test_two_calls_never_collide(self):
        assert history.new_id() != history.new_id()


class TestStartRun:
    def test_inserts_with_on_conflict_do_nothing(self):
        db = FakeDatabase()
        history.start_run(
            db, run_id="run1", issue_id="repo#1", harness_id="h1",
            mode="dev", model="claude-sonnet-4-5",
        )
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "INSERT INTO auto_claude.run" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert params == ("run1", "repo#1", "h1", "dev", "claude-sonnet-4-5")

    def test_calling_twice_with_the_same_id_is_a_replay_safe_no_op(self):
        # The fake can't enforce the real primary-key constraint, but it
        # proves the function itself never short-circuits or raises on a
        # repeat call — idempotency is delegated entirely to the SQL text
        # asserted above, which is what actually protects a replayed insert.
        db = FakeDatabase()
        kwargs = dict(
            run_id="run1", issue_id="repo#1", harness_id="h1",
            mode="dev", model="claude-sonnet-4-5",
        )
        history.start_run(db, **kwargs)
        history.start_run(db, **kwargs)
        assert len(db.calls) == 2
        assert db.calls[0] == db.calls[1]


class TestFinishRun:
    def test_updates_ended_at_and_outcome(self):
        db = FakeDatabase()
        history.finish_run(
            db, run_id="run1", outcome="completed", exit_code=0,
            duration_seconds=42, cost_usd=1.2345, turns=7,
            crash_log_path=None,
        )
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "UPDATE auto_claude.run" in sql
        assert "ended_at = now()" in sql
        assert params == ("completed", 0, 42, 1.2345, 7, None, "run1")

    def test_calling_twice_is_last_writer_wins_no_op(self):
        db = FakeDatabase()
        history.finish_run(
            db, run_id="run1", outcome="failed", exit_code=1,
            duration_seconds=10, cost_usd=0.5, turns=3,
            crash_log_path="crash_logs/x.log",
        )
        history.finish_run(
            db, run_id="run1", outcome="failed", exit_code=1,
            duration_seconds=10, cost_usd=0.5, turns=3,
            crash_log_path="crash_logs/x.log",
        )
        assert len(db.calls) == 2
        assert db.calls[0] == db.calls[1]


class TestAddSummary:
    def test_inserts_with_on_conflict_do_nothing(self):
        db = FakeDatabase()
        history.add_summary(
            db, summary_id="sum1", issue_id="repo#1", run_id="run1",
            kind="dev", body="Implemented the thing.",
            comment_url="https://github.com/o/r/issues/1#issuecomment-1",
        )
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "INSERT INTO auto_claude.summary" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert params == (
            "sum1", "repo#1", "run1", "dev", "Implemented the thing.",
            "https://github.com/o/r/issues/1#issuecomment-1",
        )

    def test_run_id_may_be_none_for_runless_summaries(self):
        db = FakeDatabase()
        history.add_summary(
            db, summary_id="sum2", issue_id="repo#1", run_id=None,
            kind="triage", body="Needs more info.", comment_url=None,
        )
        _sql, params = db.calls[0]
        assert params[2] is None

    def test_calling_twice_with_the_same_id_is_a_replay_safe_no_op(self):
        db = FakeDatabase()
        kwargs = dict(
            summary_id="sum1", issue_id="repo#1", run_id="run1",
            kind="dev", body="text", comment_url=None,
        )
        history.add_summary(db, **kwargs)
        history.add_summary(db, **kwargs)
        assert len(db.calls) == 2
        assert db.calls[0] == db.calls[1]
