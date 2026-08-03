"""`--issue` mode has to mirror the board too.

Board sync lived only in step 6 of the daemon's poll loop, so a one-shot run
moved the ac-* labels and left the Projects v2 card where it was. On
`field_admin#268` the issue reached `ac-dev-review` with PR #341 open while its
card still read **Backlog** — the board, which is what anyone actually looks
at, said the work had not started.

The sync runs after the worker finishes, not before: the whole point is to
publish the stage the run ended on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402


def _source_of(func) -> str:
    import inspect
    return inspect.getsource(func)


class TestSingleIssueSyncsTheBoard:
    def test_run_single_issue_calls_sync_boards(self):
        assert "_sync_boards" in _source_of(main_module._run_single_issue), (
            "--issue mode must mirror labels onto the board; without it a "
            "one-shot run leaves the card stale"
        )

    def test_the_sync_happens_after_the_worker_finishes(self):
        """Syncing before the run would publish the stage it started from."""
        body = _source_of(main_module._run_single_issue)
        assert body.index("Final status for") < body.index("_sync_boards("), (
            "board sync must come after the worker completes"
        )

    def test_the_daemon_loop_still_syncs(self):
        """The poll-loop call is the one that already worked — do not lose it."""
        assert "_sync_boards(" in _source_of(main_module.main)


class TestSyncIsNotLoadBearing:
    def test_a_failing_sync_does_not_break_the_run(self, monkeypatch, capsys):
        """Board drift is recoverable; a dead run is not."""
        def boom(*_a, **_k):
            raise RuntimeError("gh project scope missing")

        monkeypatch.setattr(main_module, "_sync_boards", boom)
        # _safe_sync_boards is the wrapper the single-issue path goes through.
        main_module._safe_sync_boards(
            config=None, github=None,
            logger=type("L", (), {"warn": staticmethod(lambda *_a: None)})(),
        )
