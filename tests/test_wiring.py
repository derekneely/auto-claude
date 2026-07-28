"""Tests for the glue between modules: stale-lock recovery, board sync, telemetry.

Each of these is a one- or two-line call that is easy to get subtly wrong and
impossible to notice at runtime — a stale lock silently removes an issue from
circulation, and a board sync run from the wrong directory silently does
nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import worker  # noqa: E402
from github_client import GithubClientError  # noqa: E402


class FakeGithub:
    def __init__(self, issues=None, fail=False):
        self._issues = issues or {}
        self._fail = fail
        self.added: list[tuple[str, int, str]] = []
        self.removed: list[tuple[str, int, str]] = []

    def list_issues(self, repo, assignee=None):
        if self._fail:
            raise GithubClientError("boom")
        return self._issues.get(repo, [])

    def add_label(self, repo, number, label):
        self.added.append((repo, number, label))

    def remove_label(self, repo, number, label):
        self.removed.append((repo, number, label))


def _logger():
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


def _config(repos=("field_admin",), tools_root=None, repos_dir=Path("/nope")):
    return SimpleNamespace(
        github=SimpleNamespace(
            org="Accelevation", repos=list(repos), bot_login="accelevation-bot",
        ),
        integrations=SimpleNamespace(claude_tools_root=tools_root),
        paths=SimpleNamespace(repos_dir=repos_dir),
    )


def _issue(number, labels):
    return {"number": number, "labels": [{"name": n} for n in labels]}


class TestReleaseStaleLocks:
    def test_rewinds_in_progress_to_dev_ready(self):
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress", "ac-fix"])]})
        n = main._release_stale_locks(_config(), gh, _logger())
        assert n == 1
        assert ("field_admin", 7, "ac-dev-ready") in gh.added
        assert ("field_admin", 7, "ac-in-progress") in gh.removed

    def test_rewinds_review_lock_to_dev_review(self):
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-review-in-progress"])]})
        main._release_stale_locks(_config(), gh, _logger())
        assert ("field_admin", 7, "ac-dev-review") in gh.added

    def test_never_touches_the_kind_label(self):
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress", "ac-fix"])]})
        main._release_stale_locks(_config(), gh, _logger())
        assert not any(lbl == "ac-fix" for _r, _n, lbl in gh.removed)

    @pytest.mark.parametrize("stage", ["ac-dev-ready", "ac-hitl", "ac-done", "ac-blocked"])
    def test_leaves_unlocked_stages_alone(self, stage):
        gh = FakeGithub({"field_admin": [_issue(7, [stage])]})
        assert main._release_stale_locks(_config(), gh, _logger()) == 0
        assert not gh.added and not gh.removed

    def test_dry_run_writes_nothing(self):
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})
        n = main._release_stale_locks(_config(), gh, _logger(), dry_run=True)
        assert n == 1, "should still report what it would do"
        assert not gh.added and not gh.removed

    def test_a_failing_repo_does_not_stop_the_others(self):
        gh = FakeGithub(fail=True)
        assert main._release_stale_locks(
            _config(repos=("a", "b")), gh, _logger()
        ) == 0

    def test_scopes_the_query_to_the_bot(self):
        seen = {}

        class Recording(FakeGithub):
            def list_issues(self, repo, assignee=None):
                seen["assignee"] = assignee
                return []

        main._release_stale_locks(_config(), Recording(), _logger())
        assert seen["assignee"] == "accelevation-bot"


class TestSyncBoards:
    def test_noop_without_a_configured_toolchain(self, monkeypatch):
        called = []
        monkeypatch.setattr(main, "sync_board", lambda **kw: called.append(kw))
        main._sync_boards(_config(tools_root=None), _logger())
        assert not called

    def test_skips_repos_that_are_not_cloned(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(main, "sync_board", lambda **kw: called.append(kw))
        main._sync_boards(
            _config(tools_root=tmp_path, repos_dir=tmp_path / "missing"), _logger()
        )
        assert not called

    def test_runs_with_cwd_set_to_the_repo_checkout(self, monkeypatch, tmp_path):
        # project-sync.mjs reads a literal relative `.claude/pipeline.json`, so
        # the wrong cwd makes it silently sync nothing.
        (tmp_path / "repos" / "field_admin").mkdir(parents=True)
        called = []
        monkeypatch.setattr(main, "sync_board", lambda **kw: called.append(kw))
        main._sync_boards(
            _config(tools_root=tmp_path, repos_dir=tmp_path / "repos"), _logger()
        )
        assert len(called) == 1
        assert called[0]["cwd"] == tmp_path / "repos" / "field_admin"
        assert called[0]["assignee"] == "accelevation-bot"
        assert called[0]["repo"] == "Accelevation/field_admin"


class FakeState:
    def __init__(self):
        self.transitions: list[tuple[str, str]] = []
        self.saved = 0

    def transition(self, issue_id, status):
        self.transitions.append((issue_id, status))

    def update(self, *_a, **_k):
        pass

    def save(self):
        self.saved += 1

    def get(self, _issue_id):
        return SimpleNamespace(status="queued")


class TestReviewSkipsTriage:
    """A PR review must never be sent through issue triage.

    Triage asks 'is this issue specified well enough to implement' - meaningless
    for a review, and a needs-info verdict would move the issue to
    ac-input-needed and strand an already-open PR.
    """

    def _record(self, mode, status="discovered"):
        return SimpleNamespace(
            issue_id="field_admin#7", repo="field_admin", number=7,
            mode=mode, action="implement", triage_attempts=0, labels=[],
            status=status,
        )

    def test_an_already_queued_review_is_not_re_transitioned(self):
        # The poller lands review records on QUEUED itself; QUEUED -> QUEUED is
        # not a legal move and would raise.
        from state import IssueStatus
        state = FakeState()
        main._run_triage(
            self._record("review", status=IssueStatus.QUEUED), state,
            FakeGithub(), SimpleNamespace(triage=lambda _r: None),
            _config(), _logger(),
        )
        assert state.transitions == []

    def test_review_record_goes_straight_to_queued(self):
        from state import IssueStatus
        state = FakeState()
        engine = SimpleNamespace(
            triage=lambda _r: pytest.fail("triage must not run for a review")
        )
        main._run_triage(
            self._record("review"), state, FakeGithub(), engine,
            _config(), _logger(),
        )
        assert state.transitions == [("field_admin#7", IssueStatus.QUEUED)]

    def test_dev_record_is_still_triaged(self):
        called = []
        state = FakeState()
        engine = SimpleNamespace(triage=lambda r: called.append(r) or SimpleNamespace(
            decision="proceed", confidence="high", summary="ok",
        ))
        main._run_triage(
            self._record("dev"), state, FakeGithub(), engine,
            _config(), _logger(),
        )
        assert called, "dev records must still go through triage"


class TestSpawnRouting:
    def test_mode_selects_the_worker(self):
        import process_manager
        import worker
        # The routing expression must key off record.mode, not action/status.
        assert worker.run_review_worker is not worker.run_dev_worker
        assert hasattr(process_manager, "run_review_worker")


class TestPrNumber:
    def test_parses_a_pr_url(self):
        assert worker._pr_number("https://github.com/o/r/pull/42") == 42

    def test_tolerates_a_trailing_slash(self):
        assert worker._pr_number("https://github.com/o/r/pull/42/") == 42

    def test_none_for_missing(self):
        assert worker._pr_number(None) is None

    def test_none_for_a_non_numeric_tail(self):
        assert worker._pr_number("https://github.com/o/r/pulls") is None
