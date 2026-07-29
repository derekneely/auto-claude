"""Tests for StateStore.on_change — the hook Task 11's DbSync wiring hangs
off of.

Wiring it correctly matters because ~20 existing call sites do
`state.add/update/transition(...)` with no idea a database now exists behind
them (docs/plans/12-shared-state-in-postgres.md design decision #6: "Making
it automatic is the point"). The hook must fire on every mutating call, and
an exploding hook must never surface to the caller — a state mutation cannot
be allowed to fail merely because Postgres is down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state import IssueRecord, IssueStatus, StateStore  # noqa: E402


def _record(issue_id="r#1", status=IssueStatus.DISCOVERED):
    return IssueRecord(
        issue_id=issue_id, repo="r", number=1, title="t", body="",
        labels=[], action="fix", status=status,
        discovered_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        issue_updated_at="2026-01-01T00:00:00+00:00",
    )


class TestOnChangeFiresOnEveryMutation:
    def test_add_fires_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        assert len(seen) == 1
        assert seen[0].issue_id == "r#1"

    def test_update_fires_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        seen.clear()
        store.update("r#1", branch="feat/x")
        assert len(seen) == 1
        assert seen[0].branch == "feat/x"

    def test_transition_fires_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        seen.clear()
        store.transition("r#1", IssueStatus.TRIAGING)
        assert len(seen) == 1
        assert seen[0].status == IssueStatus.TRIAGING

    def test_no_hook_configured_is_a_silent_noop(self, tmp_path):
        store = StateStore(tmp_path / "issues.json")
        store.add(_record())
        store.update("r#1", branch="x")
        store.transition("r#1", IssueStatus.TRIAGING)  # none of this may raise


class TestOnChangeNeverRaisesIntoTheCaller:
    def test_an_exploding_hook_does_not_break_add(self, tmp_path):
        store = StateStore(tmp_path / "issues.json",
                            on_change=lambda _r: (_ for _ in ()).throw(RuntimeError("down")))
        store.add(_record())  # must not raise
        assert store.get("r#1") is not None

    def test_an_exploding_hook_does_not_break_update(self, tmp_path):
        store = StateStore(tmp_path / "issues.json",
                            on_change=lambda _r: (_ for _ in ()).throw(RuntimeError("down")))
        store.add(_record())
        store.update("r#1", branch="feat/x")  # must not raise
        assert store.get("r#1").branch == "feat/x"

    def test_an_exploding_hook_does_not_break_transition(self, tmp_path):
        store = StateStore(tmp_path / "issues.json",
                            on_change=lambda _r: (_ for _ in ()).throw(RuntimeError("down")))
        store.add(_record())
        store.transition("r#1", IssueStatus.TRIAGING)  # must not raise
        assert store.get("r#1").status == IssueStatus.TRIAGING


class TestSaveIsUntouched:
    def test_save_does_not_invoke_on_change(self, tmp_path):
        seen = []
        store = StateStore(tmp_path / "issues.json", on_change=seen.append)
        store.add(_record())
        seen.clear()
        store.save()
        assert seen == []
