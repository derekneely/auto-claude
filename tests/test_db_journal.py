"""Tests for db/journal.py — the append-only fallback when Postgres is down.

Every op the journal can hold is idempotent by construction (Task 16's
ON CONFLICT DO NOTHING / last-writer-wins), so replaying the same file twice
must be silently harmless. The specific failure this guards against: if
`replay()` ever truncated the journal file *before* confirming every entry
applied, a connection drop on entry 3 of 5 would discard entries 4 and 5
along with the ones that already succeeded — silent data loss with no way to
recover the lost writes, since the only other copy was Postgres itself,
which is exactly what was unreachable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.journal import Journal  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402


class FakeDatabase:
    """Records every execute() call; fails on the Nth call if `fail_at` is set."""

    def __init__(self, fail_at: int | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._fail_at = fail_at

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self._fail_at is not None and len(self.calls) == self._fail_at:
            raise DbUnavailable("connection dropped mid-replay")
        return []


class TestJournalAppendAndReplay:
    def test_replay_applies_every_entry_in_order(self, tmp_path):
        db = FakeDatabase()
        journal = Journal(tmp_path / "journal.jsonl")
        journal.append("history.start_run", dict(
            run_id="run1", issue_id="repo#1", harness_id="h1", mode="dev", model="m",
        ))
        journal.append("history.finish_run", dict(
            run_id="run1", outcome="completed", exit_code=0, duration_seconds=10,
            cost_usd=0.1, turns=2, crash_log_path=None,
        ))

        applied = journal.replay(db)

        assert applied == 2
        assert len(db.calls) == 2
        assert "INSERT INTO auto_claude.run" in db.calls[0][0]
        assert "UPDATE auto_claude.run" in db.calls[1][0]
        assert journal.pending() == 0

    def test_replaying_twice_is_a_no_op(self, tmp_path):
        db = FakeDatabase()
        journal = Journal(tmp_path / "journal.jsonl")
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )

        first = journal.replay(db)
        second = journal.replay(db)

        assert first == 1
        assert second == 0
        assert len(db.calls) == 1, "the second replay must not re-apply anything"

    def test_a_db_failure_mid_replay_leaves_the_journal_intact_and_untruncated(self, tmp_path):
        db = FakeDatabase(fail_at=2)
        journal = Journal(tmp_path / "journal.jsonl")
        for i in range(3):
            journal.append("history.start_run", dict(
                run_id=f"run{i}", issue_id="repo#1", harness_id="h1",
                mode="dev", model="m",
            ))

        with pytest.raises(DbUnavailable):
            journal.replay(db)

        assert journal.pending() == 3, "nothing may be lost when the DB drops mid-replay"

    def test_file_is_created_lazily(self, tmp_path):
        path = tmp_path / "nested" / "journal.jsonl"
        journal = Journal(path)
        assert not path.exists()
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )
        assert path.exists()

    def test_pending_is_zero_before_any_append(self, tmp_path):
        journal = Journal(tmp_path / "journal.jsonl")
        assert journal.pending() == 0

    def test_each_line_is_valid_jsonl(self, tmp_path):
        journal = Journal(tmp_path / "journal.jsonl")
        journal.append("issue_state.upsert", dict(issue_id="repo#1"))
        journal.append("issue_state.upsert", dict(issue_id="repo#2"))

        lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            assert set(entry.keys()) == {"op", "payload"}

    def test_unknown_op_is_rejected_on_append(self, tmp_path):
        journal = Journal(tmp_path / "journal.jsonl")
        with pytest.raises(ValueError):
            journal.append("not.a.real.op", {})


class TestJournalQuarantinesUnreplayableEntries:
    """A line that can never be replayed (torn write, unknown op, a payload
    that doesn't match its handler's signature) must not wedge the journal
    forever. Regression coverage for the Task 20 review finding: raising
    uncaught left the bad line in place permanently, draining nothing behind
    it and losing every write queued after it — the same failure
    confirm-then-truncate exists to prevent, merely deferred instead of
    avoided. The fix quarantines the line to a sibling `.corrupt` file
    (verbatim, so an operator can inspect it) and lets replay continue.
    """

    def test_an_unparseable_json_line_is_quarantined_and_replay_continues(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = Journal(path)
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )
        # Simulate a torn write (crash/power-loss mid-write): malformed JSON,
        # written directly since append() can never itself produce this.
        with path.open("a", encoding="utf-8") as f:
            f.write('{"op": "history.start_run", "payload": {truncated\n')
        journal.append(
            "harness.register", dict(id="h2", hostname="box2", pid=2, version="0.2.0")
        )

        db = FakeDatabase()
        applied = journal.replay(db)

        assert applied == 2, "the two valid entries either side of the bad line still land"
        assert journal.pending() == 0
        corrupt = path.with_suffix(".corrupt")
        assert corrupt.exists()
        lines = corrupt.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert "truncated" in lines[0], "the bad line is preserved verbatim, not summarised"

    def test_an_entry_missing_the_op_or_payload_key_is_quarantined(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = Journal(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"payload": {}}) + "\n")  # no "op" key
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )

        db = FakeDatabase()
        applied = journal.replay(db)

        assert applied == 1
        assert journal.pending() == 0
        assert path.with_suffix(".corrupt").exists()

    def test_an_unrecognised_op_string_is_quarantined_not_raised(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = Journal(path)
        # Hand-written, bypassing append()'s own validation — e.g. a line
        # written by a future version of this daemon with an op this one
        # doesn't know, or a hand-edited file.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"op": "not.a.real.op", "payload": {}}) + "\n")
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )

        db = FakeDatabase()
        applied = journal.replay(db)

        assert applied == 1
        assert journal.pending() == 0
        assert path.with_suffix(".corrupt").exists()

    def test_a_payload_that_mismatches_the_handlers_signature_is_quarantined(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = Journal(path)
        # `history.start_run` requires issue_id/harness_id/mode/model too —
        # this payload TypeErrors against the real function's signature
        # before db.execute is ever reached.
        journal.append("history.start_run", dict(run_id="run1", bogus_field="x"))
        journal.append(
            "harness.register", dict(id="h1", hostname="box", pid=1, version="0.2.0")
        )

        db = FakeDatabase()
        applied = journal.replay(db)

        assert applied == 1
        assert journal.pending() == 0
        assert path.with_suffix(".corrupt").exists()

    def test_transient_db_failure_after_a_bad_line_leaves_everything_intact_and_uncommitted(
        self, tmp_path
    ):
        """A structurally-bad line seen earlier in the same pass must not be
        quarantined if a later entry hits a transient DbUnavailable — nothing
        commits until the whole pass succeeds, so a retry re-evaluates the
        bad line too rather than finding it half-migrated to `.corrupt`."""
        path = tmp_path / "journal.jsonl"
        journal = Journal(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"op": "not.a.real.op", "payload": {}}) + "\n")
        journal.append("history.start_run", dict(
            run_id="run1", issue_id="repo#1", harness_id="h1", mode="dev", model="m",
        ))

        db = FakeDatabase(fail_at=1)  # fails on the first real db.execute call
        with pytest.raises(DbUnavailable):
            journal.replay(db)

        assert journal.pending() == 2, (
            "the bad line and the entry behind it must both survive an aborted replay"
        )
        assert not path.with_suffix(".corrupt").exists(), (
            "nothing is quarantined until the whole pass completes without a transient failure"
        )

    def test_ops_is_derived_from_op_handlers_so_the_two_cannot_drift_apart(self):
        from db.journal import _OP_HANDLERS  # noqa: PLC0415

        assert set(Journal.OPS) == set(_OP_HANDLERS), (
            "OPS must be generated from _OP_HANDLERS, not hand-copied — an op "
            "added to one but not the other silently wedges append() or replay()"
        )
