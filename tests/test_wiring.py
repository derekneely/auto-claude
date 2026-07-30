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
from db.schema import SchemaOutOfDate  # noqa: E402
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
        close=lambda *_a, **_k: None,
    )


def _config(repos=("field_admin",), tools_root=None, repos_dir=Path("/nope"),
            base_branch="dev"):
    return SimpleNamespace(
        github=SimpleNamespace(
            org="Accelevation", repos=list(repos), bot_login="accelevation-bot",
            base_branch=base_branch,
        ),
        integrations=SimpleNamespace(claude_tools_root=tools_root),
        paths=SimpleNamespace(repos_dir=repos_dir),
    )


# A minimal pipeline.json with a valid projectBoard block, as GitHub would
# return it. Board sync only ever reads `projectBoard`.
BOARD_JSON = (
    '{"project": "field_admin", "prBaseBranch": "dev", "projectBoard": '
    '{"projectId": "PVT_1", "statusFieldId": "F_1", '
    '"columns": {"ac-dev-ready": "OPT_1"}}}'
)


class FakeGithubFiles:
    """GithubClient stand-in exposing just `get_file`, recording every ref asked
    for. `by_ref` maps a ref (None = the repo's default branch) to file text;
    a missing key means "that ref has no such file", which is what
    GithubClient.get_file returns as None."""

    def __init__(self, by_ref=None):
        self.by_ref = {"dev": BOARD_JSON} if by_ref is None else by_ref
        self.asked: list[tuple[str, str, str | None]] = []

    def get_file(self, repo, path, ref=None):
        self.asked.append((repo, path, ref))
        return self.by_ref.get(ref)


def _recording_sync_board(called):
    """Capture what sync_board was handed, including the state of the cwd at
    call time — the cwd is temporary and gone by the time the test asserts."""
    def fake(**kw):
        pj = Path(kw["cwd"]) / ".claude" / "pipeline.json"
        called.append({
            **kw,
            "pipeline_exists": pj.is_file(),
            "pipeline_text": pj.read_text(encoding="utf-8") if pj.is_file() else None,
        })
    return fake


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


class _FakeDbUnavailable(Exception):
    """Stand-in for db.pool.DbUnavailable that does not require importing
    the real db package into this test file."""


class FakeLeaseDb:
    """Stands in for a real db.pool.Database, driven entirely through
    main.db_lease / main.db_issue_state monkeypatches below - this class
    itself is never called by _release_stale_locks directly, it just needs
    to be a non-None sentinel."""


def _patch_lease(monkeypatch, *, rows=None, release_expired_raises=False,
                  fetch_raises_for=()):
    """Monkeypatch main.db_lease.release_expired and main.db_issue_state.fetch.

    `rows`: issue_id -> {"owner_harness_id": str | None}. Missing issue_id
    means fetch() returns None (no Postgres row at all - e.g. a pre-Postgres
    holdover), which must be treated as free.
    """
    rows = rows or {}

    def fake_release_expired(db):
        if release_expired_raises:
            raise _FakeDbUnavailable("release_expired: db down")
        return []

    def fake_fetch(db, issue_id):
        if issue_id in fetch_raises_for:
            raise _FakeDbUnavailable(f"fetch({issue_id}): db down")
        return rows.get(issue_id)

    monkeypatch.setattr(main.db_lease, "release_expired", fake_release_expired)
    monkeypatch.setattr(main.db_issue_state, "fetch", fake_fetch)
    monkeypatch.setattr(main, "DbUnavailable", _FakeDbUnavailable)


class TestReleaseStaleLocksWithLease:
    def test_rewinds_a_lock_whose_lease_is_free(self, monkeypatch):
        _patch_lease(monkeypatch, rows={"field_admin#7": {"owner_harness_id": None}})
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress", "ac-fix"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 1
        assert ("field_admin", 7, "ac-dev-ready") in gh.added
        assert ("field_admin", 7, "ac-in-progress") in gh.removed

    def test_leaves_a_lock_whose_lease_is_held_by_another_harness(self, monkeypatch):
        _patch_lease(monkeypatch, rows={
            "field_admin#7": {"owner_harness_id": "some-other-harness"},
        })
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress", "ac-fix"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 0
        assert not gh.added and not gh.removed

    def test_treats_a_missing_postgres_row_as_free(self, monkeypatch):
        # A pre-Postgres holdover, or a row this harness never got to upsert.
        _patch_lease(monkeypatch, rows={})
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 1

    def test_release_expired_failing_at_startup_rewinds_nothing(self, monkeypatch):
        # Fails closed: cannot verify safety, so touch nothing this run,
        # rather than falling back to "assume everything is stale".
        _patch_lease(monkeypatch, release_expired_raises=True,
                      rows={"field_admin#7": {"owner_harness_id": None}})
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 0
        assert not gh.added and not gh.removed

    def test_a_fetch_failure_mid_sweep_leaves_that_one_issue_alone(self, monkeypatch):
        _patch_lease(monkeypatch, fetch_raises_for={"field_admin#7"},
                      rows={"field_admin#8": {"owner_harness_id": None}})
        gh = FakeGithub({"field_admin": [
            _issue(7, ["ac-in-progress"]),
            _issue(8, ["ac-in-progress"]),
        ]})

        n = main._release_stale_locks(_config(), gh, _logger(), db=FakeLeaseDb())

        assert n == 1
        assert ("field_admin", 8, "ac-dev-ready") in gh.added
        assert not any(num == 7 for _r, num, _l in gh.added)

    def test_no_db_argument_still_behaves_like_before(self):
        # db is None by default - the degraded/no-Postgres path, unchanged.
        gh = FakeGithub({"field_admin": [_issue(7, ["ac-in-progress"])]})
        assert main._release_stale_locks(_config(), gh, _logger()) == 1


class TestSyncBoards:
    def test_noop_without_a_configured_toolchain(self, monkeypatch):
        called = []
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(_config(tools_root=None), FakeGithubFiles(), _logger())
        assert not called

    def test_runs_from_a_directory_holding_the_current_pipeline_json(
        self, monkeypatch, tmp_path
    ):
        # project-sync.mjs reads a literal relative `.claude/pipeline.json` from
        # its cwd, so the cwd must contain that file or the sync dies exit 1.
        called = []
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(
            _config(tools_root=tmp_path), FakeGithubFiles(), _logger()
        )
        assert len(called) == 1
        assert called[0]["pipeline_exists"], "cwd had no .claude/pipeline.json"
        assert called[0]["pipeline_text"] == BOARD_JSON
        assert called[0]["assignee"] == "accelevation-bot"
        assert called[0]["repo"] == "Accelevation/field_admin"

    def test_syncs_a_repo_whose_local_checkout_is_absent_or_on_a_stale_branch(
        self, monkeypatch, tmp_path
    ):
        # The regression: repos/field_admin sat on `main`, which has no
        # .claude/pipeline.json (it lives only on `dev`), so every sync exited 1
        # until a worker happened to move the checkout. Board sync must not
        # depend on the shared checkout's branch — or on it existing at all.
        called = []
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(
            _config(tools_root=tmp_path, repos_dir=tmp_path / "never-cloned"),
            FakeGithubFiles(),
            _logger(),
        )
        assert len(called) == 1
        assert called[0]["pipeline_exists"]

    def test_prefers_the_base_branch_over_the_default_branch(
        self, monkeypatch, tmp_path
    ):
        # `main` trails `dev` by a release cycle; reading the default branch is
        # how auto-claude once picked up a prBaseBranch that was days stale.
        called = []
        gh = FakeGithubFiles({"dev": BOARD_JSON, None: '{"project": "stale"}'})
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(_config(tools_root=tmp_path), gh, _logger())
        assert gh.asked[0][2] == "dev", "base branch must be tried first"
        assert called[0]["pipeline_text"] == BOARD_JSON

    def test_falls_back_to_the_default_branch(self, monkeypatch, tmp_path):
        called = []
        gh = FakeGithubFiles({None: BOARD_JSON})  # nothing on `dev`
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(_config(tools_root=tmp_path), gh, _logger())
        assert len(called) == 1
        assert called[0]["pipeline_text"] == BOARD_JSON

    def test_skips_a_repo_with_no_pipeline_json_on_any_branch(
        self, monkeypatch, tmp_path
    ):
        called = []
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(
            _config(tools_root=tmp_path), FakeGithubFiles({}), _logger()
        )
        assert not called, "nothing to sync against — must not invoke the script"

    def test_a_repo_that_cannot_be_read_does_not_stop_the_others(
        self, monkeypatch, tmp_path
    ):
        class Exploding(FakeGithubFiles):
            def get_file(self, repo, path, ref=None):
                if repo == "boom":
                    raise GithubClientError("boom")
                return super().get_file(repo, path, ref=ref)

        called = []
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(
            _config(repos=("boom", "field_admin"), tools_root=tmp_path),
            Exploding(),
            _logger(),
        )
        assert [c["repo"] for c in called] == ["Accelevation/field_admin"]

    def test_removes_the_scratch_directory_afterwards(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(main, "sync_board", _recording_sync_board(called))
        main._sync_boards(
            _config(tools_root=tmp_path), FakeGithubFiles(), _logger()
        )
        assert not Path(called[0]["cwd"]).exists(), "scratch dir leaked"


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


class TestSchemaGate:
    def test_aborts_when_schema_is_out_of_date(self, monkeypatch):
        monkeypatch.setattr(
            main, "check_schema_current",
            lambda _db: (_ for _ in ()).throw(SchemaOutOfDate("run: alembic upgrade head")),
        )
        with pytest.raises(SystemExit) as excinfo:
            main._check_schema_gate(object(), _logger())
        assert excinfo.value.code == 1

    def test_does_nothing_when_schema_is_current(self, monkeypatch):
        monkeypatch.setattr(main, "check_schema_current", lambda _db: None)
        main._check_schema_gate(object(), _logger())  # must not raise


class TestPrNumber:
    def test_parses_a_pr_url(self):
        assert worker._pr_number("https://github.com/o/r/pull/42") == 42

    def test_tolerates_a_trailing_slash(self):
        assert worker._pr_number("https://github.com/o/r/pull/42/") == 42

    def test_none_for_missing(self):
        assert worker._pr_number(None) is None

    def test_none_for_a_non_numeric_tail(self):
        assert worker._pr_number("https://github.com/o/r/pulls") is None
