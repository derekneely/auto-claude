"""Tests for lease fencing - worker.py's `_assert_lease_held` and its wiring
into every irreversible remote act (git push, gh pr create, gh pr review,
ac-* label writes).

Guards docs/plans/12-shared-state-in-postgres.md, "Fencing: never kill a
running agent": if this box's lease expires mid-run, another harness may
legitimately retake the issue while the local agent keeps running. The agent
is never aborted here - it is the *acts after it finishes* that must be
refused, the same way `assert_pushable`/`ProtectedBranchError` already
refuse a push to the wrong branch. Two layers: (1) `_assert_lease_held`'s
own decision logic against a fake DbSync, and (2) that every guarded
function actually calls it before touching the remote, mirroring
tests/test_push_guard.py's wiring tests.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from worker import LeaseLostError  # noqa: E402


def _ctx(tmp_path, harness_id="harness-a", lease_db_url_env="PIPELINE_METRICS_DATABASE_URL"):
    return worker.IssueContext(
        issue_id="field_admin#215",
        repo="field_admin",
        number=215,
        title="Job wizard progress",
        body="",
        action="implement",
        org="Accelevation",
        base_branch="dev",
        repos_dir=tmp_path,
        worktrees_dir=tmp_path,
        prompts_dir=tmp_path,
        dev_model="opus",
        light_model="sonnet",
        permission_mode="acceptEdits",
        max_budget_usd=10.0,
        max_turns=100,
        crash_logs_dir=tmp_path,
        color_name="blue",
        color_code="\033[34m",
        harness_id=harness_id,
        lease_db_url_env=lease_db_url_env,
    )


def _logger():
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


# ---------------------------------------------------------------------------
# Layer 1: _assert_lease_held's own decision logic
# ---------------------------------------------------------------------------

class _FakeDbSync:
    def __init__(self, held: bool):
        self._held = held
        self.checked: list[str] = []

    def check_lease(self, issue_id):
        self.checked.append(issue_id)
        return self._held


class TestAssertLeaseHeld:
    def test_raises_when_the_lease_is_lost(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker, "DbSync", lambda *a, **k: _FakeDbSync(held=False))
        monkeypatch.setattr(worker, "Database", lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        with pytest.raises(LeaseLostError):
            worker._assert_lease_held(_ctx(tmp_path), _logger())

    def test_does_not_raise_when_the_lease_is_held(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker, "DbSync", lambda *a, **k: _FakeDbSync(held=True))
        monkeypatch.setattr(worker, "Database", lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        worker._assert_lease_held(_ctx(tmp_path), _logger())  # must not raise

    def test_no_op_when_no_shared_database_is_configured(self, tmp_path, monkeypatch):
        # No shared database means no second harness to fence against - see
        # main._release_stale_locks's degraded-path reasoning, applied here
        # mid-run instead of at startup.
        monkeypatch.delenv("PIPELINE_METRICS_DATABASE_URL", raising=False)
        worker._assert_lease_held(_ctx(tmp_path), _logger())  # must not raise

    def test_no_op_when_the_context_carries_no_harness_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        worker._assert_lease_held(_ctx(tmp_path, harness_id=None), _logger())

    def test_no_op_when_the_context_carries_no_lease_db_url_env(self, tmp_path, monkeypatch):
        # Distinct from the "no shared database" test above: here the env
        # var itself is present (real telemetry, unrelated to auto-claude's
        # own lease system) but ctx never learned which var name to read -
        # e.g. an older/other caller that never wired ProcessManager.spawn's
        # ctx field. Must be treated exactly like "no shared database".
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        worker._assert_lease_held(_ctx(tmp_path, lease_db_url_env=None), _logger())

    def test_the_production_combination_config_disabled_with_telemetry_var_set(
        self, tmp_path, monkeypatch,
    ):
        # Final whole-branch review, Critical 1: the reason no per-task
        # reviewer caught this bug. The two tests above each vary ONE of
        # "harness_id" / the env var in isolation; this is the actual
        # combination that occurred in production. `main._init_db_layer`
        # always constructs a real `harness` (a harness identity always
        # exists, DB or not) and `PIPELINE_METRICS_DATABASE_URL` is
        # routinely set for the sibling Node telemetry even when auto-claude's
        # own `[database] enabled = false` - so with the pre-fix code
        # (`main.py` handing every `IssueContext` the real `harness.id`
        # unconditionally) this exact combination reached a real `Database`
        # connection attempt and fenced every single write. The fix is that
        # `main._harness_id_for_workers` now yields None whenever Postgres
        # is not configured, so the correctly-wired ctx here has
        # harness_id=None despite the env var being present. Asserts more
        # than "must not raise": `Database` is monkeypatched to explode if
        # called at all, proving zero connection attempts, not a raise that
        # merely happened not to occur.
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgres://fake/db")
        monkeypatch.setattr(
            worker, "Database",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not construct a Database with no harness_id")
            ),
        )
        ctx = _ctx(tmp_path, harness_id=None)  # what a disabled db now produces
        worker._assert_lease_held(ctx, _logger())  # must not raise

    def test_reads_the_configured_env_var_name_not_a_hardcoded_constant(
        self, tmp_path, monkeypatch,
    ):
        # Final whole-branch review, Critical 1 (second reachable form): an
        # operator who sets `[database] url_env = "SOMETHING_ELSE"` must have
        # this fence check the SAME Postgres `main` uses. Proven here by
        # leaving the default PIPELINE_METRICS_DATABASE_URL unset entirely -
        # if this were still reading a hardcoded constant, it would find no
        # URL and silently no-op instead of reaching Database/DbSync at all.
        monkeypatch.delenv("PIPELINE_METRICS_DATABASE_URL", raising=False)
        monkeypatch.setenv("SOMETHING_ELSE", "postgres://fake/db")
        monkeypatch.setattr(worker, "DbSync", lambda *a, **k: _FakeDbSync(held=False))
        monkeypatch.setattr(worker, "Database", lambda *a, **k: SimpleNamespace(close=lambda: None))
        ctx = _ctx(tmp_path, lease_db_url_env="SOMETHING_ELSE")
        with pytest.raises(LeaseLostError):
            worker._assert_lease_held(ctx, _logger())


# ---------------------------------------------------------------------------
# Layer 2: wiring into every irreversible act
# ---------------------------------------------------------------------------

class _FakeResult:
    """Reports a dirty tree so the commit path runs, and success on
    everything else, so nothing but the fence can stop a push/PR/label."""
    returncode = 0
    stdout = " M src/foo.ts\n"
    stderr = ""


def _record_cmds(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **_kwargs):
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(worker, "_run_cmd", fake_run_cmd)
    return calls


def _fence(monkeypatch, *, lost=False, raise_on_call=None):
    """Replace worker._assert_lease_held with a canned verdict. `raise_on_call`
    (1-indexed) makes only that specific call raise, simulating a lease lost
    partway through a function that touches the remote more than once (push,
    then PR create). Returns the call counter."""
    calls = {"n": 0}

    def fake(ctx, logger):
        calls["n"] += 1
        if lost and (raise_on_call is None or calls["n"] == raise_on_call):
            raise LeaseLostError(f"lease lost (test, call {calls['n']})")

    monkeypatch.setattr(worker, "_assert_lease_held", fake)
    return calls


class TestPushAndPrIsFenced:
    def test_lease_lost_before_the_push_blocks_everything(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=1)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert calls == [], "no git/gh call may happen once the fence has fired"

    def test_lease_lost_between_push_and_pr_create_blocks_only_the_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=2)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert any(c[:2] == ["git", "push"] for c in calls), "the push already happened"
        assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)

    def test_lease_held_pushes_and_opens_the_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._push_and_pr(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)
        assert any(c[:3] == ["gh", "pr", "create"] for c in calls)


class TestPushReworkIsFenced:
    def test_lease_lost_blocks_the_push(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_rework(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert calls == []

    def test_lease_held_still_pushes(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._push_rework(_ctx(tmp_path), "ac/issue-215-x", tmp_path, "summary", _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)


class TestPushPartialWorkIsFenced:
    def test_lease_lost_before_the_push_propagates(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=1)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_partial_work(_ctx(tmp_path), "ac/issue-215-x", tmp_path, _logger())
        assert calls == [], "no git/gh call may happen once the fence has fired"

    def test_lease_lost_between_push_and_pr_create_blocks_only_the_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True, raise_on_call=2)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._push_partial_work(_ctx(tmp_path), "ac/issue-215-x", tmp_path, _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)
        assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)

    def test_lease_held_pushes_and_opens_the_wip_pr(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._push_partial_work(_ctx(tmp_path), "ac/issue-215-x", tmp_path, _logger())
        assert any(c[:2] == ["git", "push"] for c in calls)
        assert any(c[:3] == ["gh", "pr", "create"] for c in calls)


class TestSetLabelsIsFenced:
    def test_lease_lost_blocks_every_label_write(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True)
        calls = _record_cmds(monkeypatch)
        with pytest.raises(LeaseLostError):
            worker._set_labels(_ctx(tmp_path), _logger(),
                                add=["ac-dev-review"], remove=["ac-in-progress"])
        assert calls == []

    def test_lease_held_writes_labels(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        worker._set_labels(_ctx(tmp_path), _logger(),
                            add=["ac-dev-review"], remove=["ac-in-progress"])
        assert any(c[:3] == ["gh", "issue", "edit"] for c in calls)

    def test_a_no_op_call_never_checks_the_lease(self, monkeypatch, tmp_path):
        checked = _fence(monkeypatch, lost=False)
        worker._set_labels(_ctx(tmp_path), _logger())  # nothing to add/remove
        assert checked["n"] == 0


class TestPostPrReviewIsFenced:
    def test_lease_lost_blocks_the_review(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=True)
        calls = _record_cmds(monkeypatch)
        ctx = _ctx(tmp_path)
        ctx.pr_url = "https://github.com/Accelevation/field_admin/pull/42"
        with pytest.raises(LeaseLostError):
            worker._post_pr_review(ctx, _logger(), approve=True, body="ok")
        assert calls == []

    def test_lease_held_posts_the_review(self, monkeypatch, tmp_path):
        _fence(monkeypatch, lost=False)
        calls = _record_cmds(monkeypatch)
        ctx = _ctx(tmp_path)
        ctx.pr_url = "https://github.com/Accelevation/field_admin/pull/42"
        worker._post_pr_review(ctx, _logger(), approve=True, body="ok")
        assert any(c[:3] == ["gh", "pr", "review"] for c in calls)


# ---------------------------------------------------------------------------
# Layer 3: the fenced exit path
# ---------------------------------------------------------------------------

class TestHandleLeaseLost:
    def test_sends_a_fenced_state_update_writes_a_crash_log_and_touches_no_remote(
        self, monkeypatch, tmp_path,
    ):
        posted = []
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: posted.append(a))
        remote_calls = _record_cmds(monkeypatch)
        q = queue.Queue()
        ctx = _ctx(tmp_path)
        ctx.pr_url = "https://github.com/Accelevation/field_admin/pull/42"

        worker._handle_lease_lost(
            ctx, _logger(), q, LeaseLostError("lease lost"), branch="ac/issue-215-x",
        )

        update = q.get_nowait()
        assert update.status == "failed"
        assert update.error.startswith("fenced:")
        assert update.branch == "ac/issue-215-x"
        assert update.pr_url == ctx.pr_url
        assert posted == [], "must never post a crash comment - that touches the remote"
        assert remote_calls == []
        assert len(list(tmp_path.glob("*.log"))) == 1, "a local crash log must still be written"

    def test_closes_the_run_row_with_a_fenced_outcome_when_run_identity_is_supplied(
        self, monkeypatch, tmp_path,
    ):
        # Task 18 review Finding 1: a fenced worker's `run` row was never
        # closed — its first StateUpdate opens it (run_mode set), but the
        # fenced exit sent neither run_id nor run_outcome, so
        # ProcessManager._active_runs kept the entry forever (the record's
        # status goes straight to "failed", never back to "in_progress", so
        # neither reap_dead's nor _mark_interrupted's dangling-run cleanup —
        # both gated on IN_PROGRESS — ever fires for it). This pins the fix:
        # _handle_lease_lost now accepts the same run_id/metrics every other
        # exit path threads through, and closes the row itself.
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: None)
        _record_cmds(monkeypatch)
        q = queue.Queue()
        ctx = _ctx(tmp_path)
        metrics = worker.RunMetrics(cost_usd=0.5, turns=3, duration_seconds=20)

        worker._handle_lease_lost(
            ctx, _logger(), q, LeaseLostError("lease lost"),
            run_id="run1", metrics=metrics,
        )

        update = q.get_nowait()
        assert update.error.startswith("fenced:"), "the literal prefix Task 19 depends on"
        assert update.run_id == "run1"
        assert update.run_outcome == "fenced"
        assert update.cost_usd == 0.5
        assert update.turns == 3
        assert update.duration_seconds == 20
        assert update.crash_log_path is not None, "the crash log written above must be reported"

    def test_run_identity_defaults_to_none_when_the_caller_supplies_none(
        self, monkeypatch, tmp_path,
    ):
        # Guards the pre-try crash case: a LeaseLostError raised before
        # run_id/metrics are ever bound must not itself crash this path.
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: None)
        _record_cmds(monkeypatch)
        q = queue.Queue()
        ctx = _ctx(tmp_path)

        worker._handle_lease_lost(ctx, _logger(), q, LeaseLostError("lease lost"))

        update = q.get_nowait()
        assert update.run_id is None
        assert update.run_outcome == "fenced"
        assert update.cost_usd is None
        assert update.turns is None
        assert update.duration_seconds is None

    def test_threads_pending_summaries_from_before_the_lease_was_lost(
        self, monkeypatch, tmp_path,
    ):
        # Final whole-branch review, Finding 3: a comment already posted to
        # GitHub before the lease was discovered lost (e.g.
        # _post_issue_report's failed-checks report) used to be silently
        # dropped here — the only path where something real lands on GitHub
        # and is then forgotten by Postgres, contradicting "the database and
        # GitHub cannot disagree".
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: None)
        _record_cmds(monkeypatch)
        q = queue.Queue()
        ctx = _ctx(tmp_path)
        pending = [{"kind": "dev", "body": "checks failed", "comment_url": "https://x/1"}]

        worker._handle_lease_lost(
            ctx, _logger(), q, LeaseLostError("lease lost"), summaries=pending,
        )

        update = q.get_nowait()
        assert update.summaries == pending

    def test_no_pending_summaries_is_none_not_an_empty_list(
        self, monkeypatch, tmp_path,
    ):
        # Mirrors every other exit path's `summaries=pending_summaries or
        # None` convention (see run_dev_worker/run_review_worker) so
        # ProcessManager._sync_run's `for item in (update.summaries or [])`
        # sees the same shape regardless of which exit path produced it.
        monkeypatch.setattr(worker, "_post_crash_comment", lambda *a, **k: None)
        _record_cmds(monkeypatch)
        q = queue.Queue()
        ctx = _ctx(tmp_path)

        worker._handle_lease_lost(ctx, _logger(), q, LeaseLostError("lease lost"), summaries=[])

        update = q.get_nowait()
        assert update.summaries is None
