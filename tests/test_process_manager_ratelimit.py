"""Tests for ProcessManager's pool-wide rate-limit backpressure."""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.pool import DbUnavailable  # noqa: E402
from process_manager import ProcessManager  # noqa: E402
from worker import StateUpdate  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, msg):
        self.messages.append(("info", msg))

    def warn(self, msg):
        self.messages.append(("warn", msg))

    def error(self, msg):
        self.messages.append(("error", msg))

    def drain_queue(self, _q):
        pass

    def text(self) -> str:
        return " | ".join(m for _lvl, m in self.messages)


class FakeProc:
    """A worker process that has already exited."""

    def __init__(self, exitcode=1):
        self.exitcode = exitcode
        self.pid = 1234

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class FakeState:
    """Minimal StateStore stand-in."""

    def __init__(self, records=None):
        self._records = records or {}
        self.saved = 0

    def get(self, issue_id):
        return self._records.get(issue_id)

    def transition(self, issue_id, status):
        self._records[issue_id].status = status

    def update(self, issue_id, **kwargs):
        for k, v in kwargs.items():
            setattr(self._records[issue_id], k, v)

    def save(self):
        self.saved += 1


def make_pm(max_parallel=3, records=None, dbsync=None, harness_id=None):
    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=max_parallel, shutdown_grace_seconds=30),
    )
    state = FakeState(records)
    logger = FakeLogger()
    return (
        ProcessManager(
            config=config,
            state=state,
            logger=logger,
            log_queue=queue.Queue(),
            state_queue=queue.Queue(),
            dbsync=dbsync,
            harness_id=harness_id,
        ),
        state,
        logger,
    )


class TestCanSpawn:
    def test_allows_spawning_when_idle(self):
        pm, _, _ = make_pm()
        assert pm.can_spawn()

    def test_blocks_all_spawning_while_rate_limited(self):
        pm, _, _ = make_pm()
        pm._rate_limited_until = time.time() + 300
        assert not pm.can_spawn()

    def test_resumes_once_the_window_passes(self):
        pm, _, _ = make_pm()
        pm._rate_limited_until = time.time() - 1
        assert pm.can_spawn()
        assert pm.rate_limit_remaining == 0.0

    def test_rate_limit_outranks_free_capacity(self):
        # The whole point: idle slots must NOT be filled during a limit window.
        pm, _, _ = make_pm(max_parallel=3)
        pm._rate_limited_until = time.time() + 300
        assert pm.active_count == 0
        assert not pm.can_spawn()


class TestNoteRateLimit:
    def test_records_the_pause_and_warns(self):
        pm, _, logger = make_pm()
        deadline = time.time() + 600
        pm._note_rate_limit(StateUpdate("r#1", "failed", rate_limited_until=deadline))
        assert pm._rate_limited_until == deadline
        assert "Rate limited" in logger.text()

    def test_ignores_updates_with_no_rate_limit(self):
        pm, _, _ = make_pm()
        pm._note_rate_limit(StateUpdate("r#1", "completed"))
        assert pm._rate_limited_until == 0.0

    def test_keeps_the_furthest_deadline(self):
        pm, _, _ = make_pm()
        later = time.time() + 900
        pm._note_rate_limit(StateUpdate("r#1", "failed", rate_limited_until=later))
        pm._note_rate_limit(
            StateUpdate("r#2", "failed", rate_limited_until=time.time() + 60)
        )
        assert pm._rate_limited_until == later

    def test_does_not_re_warn_for_a_shorter_deadline(self):
        pm, _, logger = make_pm()
        pm._note_rate_limit(
            StateUpdate("r#1", "failed", rate_limited_until=time.time() + 900)
        )
        before = len(logger.messages)
        pm._note_rate_limit(
            StateUpdate("r#2", "failed", rate_limited_until=time.time() + 60)
        )
        assert len(logger.messages) == before


class TestDrainStateQueue:
    def test_pause_survives_an_unknown_issue(self):
        # The pause is account-wide; it must not be lost because this particular
        # record is missing from the store.
        pm, _, _ = make_pm(records={})
        deadline = time.time() + 600
        pm._state_queue.put(
            StateUpdate("ghost#99", "failed", rate_limited_until=deadline)
        )
        pm.drain_state_queue()
        assert pm._rate_limited_until == deadline
        assert not pm.can_spawn()

    def test_pause_survives_a_failed_transition(self):
        rec = SimpleNamespace(status="completed", error=None)
        pm, state, _ = make_pm(records={"r#1": rec})

        def boom(*_args, **_kwargs):
            raise ValueError("illegal transition")

        state.transition = boom

        deadline = time.time() + 600
        pm._state_queue.put(StateUpdate("r#1", "failed", rate_limited_until=deadline))
        pm.drain_state_queue()
        assert pm._rate_limited_until == deadline

    def test_rate_limited_worker_is_requeued_without_burning_a_continuation(self):
        rec = SimpleNamespace(
            issue_id="r#1", status="failed", error="rate_limited",
            continuation_count=0, branch="ac/issue-1", number=1, repo="r",
        )
        pm, state, logger = make_pm(records={"r#1": rec})
        pm._rate_limited_until = time.time() + 600
        pm._workers["r#1"] = (FakeProc(), object())

        pm.reap_dead()

        assert rec.status == "queued", "must be retried, not left failed"
        assert rec.error is None, "stale error would poison the next reap"
        assert rec.continuation_count == 0, "rate limiting is not the issue's fault"
        assert "r#1" not in pm._workers

    def test_budget_exhaustion_still_consumes_a_continuation(self):
        # Guard against the new branch swallowing the budget path.
        rec = SimpleNamespace(
            issue_id="r#1", status="failed", error="budget_exceeded",
            continuation_count=0, branch=None, number=1, repo="r",
        )
        pm, _, _ = make_pm(records={"r#1": rec})
        pm._config.workers.max_continuations = 2
        pm._workers["r#1"] = (FakeProc(), object())

        pm.reap_dead()

        assert rec.status == "queued"
        assert rec.continuation_count == 1

    def test_normal_updates_still_apply(self):
        rec = SimpleNamespace(status="in_progress", error=None, branch=None,
                              pr_url=None, worker_pid=None, handoff_summary=None)
        pm, _, _ = make_pm(records={"r#1": rec})
        pm._state_queue.put(StateUpdate("r#1", "completed", branch="ac/issue-1"))
        pm.drain_state_queue()
        assert rec.status == "completed"
        assert rec.branch == "ac/issue-1"
        assert pm._rate_limited_until == 0.0


class FakeDbSync:
    def __init__(self, grant=True):
        self.grant = grant
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire_lease(self, issue_id):
        self.acquired.append(issue_id)
        return self.grant

    def release_lease(self, issue_id):
        self.released.append(issue_id)


class FakeUnavailableDbSync:
    """A dbsync whose Postgres has gone unreachable mid-run: acquire_lease
    and release_lease raise DbUnavailable uncaught, exactly as db/lease.py's
    real functions do when DbSync.enabled is True but the connection is
    down. Distinct from FakeDbSync(grant=False), which models a live,
    reachable Postgres that legitimately denies the lease to another
    harness — the two must produce different behaviour (fail-closed-and-
    retry-next-poll for both, but this one must never propagate the
    exception out of ProcessManager)."""

    def __init__(self):
        self.acquired: list[str] = []
        self.release_attempts: list[str] = []

    def acquire_lease(self, issue_id):
        self.acquired.append(issue_id)
        raise DbUnavailable("could not reach Postgres")

    def release_lease(self, issue_id):
        self.release_attempts.append(issue_id)
        raise DbUnavailable("could not reach Postgres")


class TestLeaseGatesSpawn:
    """spawn() must not start a worker process without first holding the
    Postgres lease. `dbsync=None` (the default) preserves the pre-lease
    behaviour exactly, so every other test in this file is unaffected."""

    def test_no_dbsync_always_ok(self):
        pm, _, _ = make_pm()
        assert pm._lease_ok("r#1") is True

    def test_denied_lease_blocks_and_warns(self):
        dbsync = FakeDbSync(grant=False)
        pm, _, logger = make_pm(dbsync=dbsync)
        assert pm._lease_ok("r#1") is False
        assert dbsync.acquired == ["r#1"]
        assert "No lease" in logger.text()

    def test_granted_lease_allows(self):
        dbsync = FakeDbSync(grant=True)
        pm, _, _ = make_pm(dbsync=dbsync)
        assert pm._lease_ok("r#1") is True

    def test_db_unavailable_blocks_without_raising(self):
        # Fix round 1, Finding 1: an unreachable Postgres must fail closed
        # (skip this spawn, retry next poll) rather than let DbUnavailable
        # escape spawn() and take the whole poll loop down with it.
        dbsync = FakeUnavailableDbSync()
        pm, _, logger = make_pm(dbsync=dbsync)
        assert pm._lease_ok("r#1") is False  # must not raise
        assert dbsync.acquired == ["r#1"]
        assert "unreachable" in logger.text().lower()

    def test_spawn_returns_before_starting_a_process_when_lease_denied(self, monkeypatch):
        pm, _, _ = make_pm()
        pm._lease_ok = lambda issue_id: False
        calls = []
        monkeypatch.setattr("process_manager.Process", lambda **kw: calls.append(kw))
        pm.spawn(SimpleNamespace(
            issue_id="r#1", repo="r", number=1, title="t", body="",
            action="fix", branch=None, pr_url=None, rework_count=0,
            handoff_summary=None,
        ))
        assert calls == [], "must not construct a worker Process without the lease"


def _make_full_pm(tmp_path, dbsync):
    """A ProcessManager with a config complete enough to reach spawn()'s
    Process construction, not just the early lease-check return. Kept
    separate from `make_pm` above (whose config is deliberately minimal)
    because every other test in this file returns before touching most of
    these fields."""
    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=30),
        github=SimpleNamespace(org="Accelevation", base_branch="dev"),
        claude=SimpleNamespace(
            dev_model="claude-opus", light_model="claude-sonnet",
            permission_mode="acceptEdits", max_budget_usd=10.0,
            max_turns_dev=100, grace_budget_usd=1.0, action_models={},
        ),
        paths=SimpleNamespace(
            repos_dir=tmp_path, worktrees_dir=tmp_path, prompts_dir=tmp_path,
            crash_logs_dir=tmp_path,
        ),
        integrations=SimpleNamespace(claude_tools_root=None),
        database=SimpleNamespace(url_env="PIPELINE_METRICS_DATABASE_URL"),
    )
    pm = ProcessManager(
        config=config,
        state=FakeState(),
        logger=FakeLogger(),
        log_queue=queue.Queue(),
        state_queue=queue.Queue(),
        dbsync=dbsync,
        harness_id="h1",
    )
    pm._pipelines["r"] = None  # skip the GitHub pipeline.json lookup entirely
    return pm


def _full_record():
    return SimpleNamespace(
        issue_id="r#1", repo="r", number=1, title="t", body="",
        action="fix", branch=None, pr_url=None, rework_count=0,
        handoff_summary=None, mode="dev",
    )


class TestSpawnFailureReleasesTheLease:
    """Final whole-branch review, Important 2 (second half): `proc.start()`
    can raise (a Windows spawn failure, a pickling failure in `ctx`), and the
    lease `_lease_ok` just acquired above must not then be held and
    heartbeated for the rest of LEASE_TTL_SECONDS - the same as
    `reap_dead`'s own release-on-exit path already guarantees."""

    def test_a_process_construction_failure_releases_the_lease_and_registers_no_worker(
        self, tmp_path, monkeypatch,
    ):
        dbsync = FakeDbSync(grant=True)
        pm = _make_full_pm(tmp_path, dbsync)
        monkeypatch.setattr(
            "process_manager.Process",
            lambda **kw: (_ for _ in ()).throw(OSError("could not spawn worker process")),
        )

        pm.spawn(_full_record())  # must not raise

        assert dbsync.acquired == ["r#1"]
        assert dbsync.released == ["r#1"], (
            "a lease taken for a spawn that then failed must be released, "
            "not left to expire on TTL alone"
        )
        assert "r#1" not in pm._workers

    def test_a_process_construction_failure_with_postgres_unreachable_still_does_not_raise(
        self, tmp_path, monkeypatch,
    ):
        # The release-on-failure path must itself tolerate a database that
        # has gone unreachable in between - same fail-open-but-non-fatal
        # rule as reap_dead's own release_lease handling.
        dbsync = FakeDbSync(grant=True)
        pm = _make_full_pm(tmp_path, dbsync)
        dbsync.release_lease = lambda issue_id: (_ for _ in ()).throw(
            DbUnavailable("could not reach Postgres")
        )
        monkeypatch.setattr(
            "process_manager.Process",
            lambda **kw: (_ for _ in ()).throw(OSError("could not spawn worker process")),
        )

        pm.spawn(_full_record())  # must not raise

        assert "r#1" not in pm._workers


class TestLeaseReleaseOnReap:
    def test_releases_the_lease_when_a_worker_is_reaped(self):
        rec = SimpleNamespace(issue_id="r#1", status="completed", error=None,
                               continuation_count=0, branch=None, number=1, repo="r")
        dbsync = FakeDbSync()
        pm, _, _ = make_pm(records={"r#1": rec}, dbsync=dbsync)
        pm._workers["r#1"] = (FakeProc(exitcode=0), object())

        pm.reap_dead()

        assert dbsync.released == ["r#1"]

    def test_no_dbsync_reap_does_not_error(self):
        rec = SimpleNamespace(issue_id="r#1", status="completed", error=None,
                               continuation_count=0, branch=None, number=1, repo="r")
        pm, _, _ = make_pm(records={"r#1": rec})
        pm._workers["r#1"] = (FakeProc(exitcode=0), object())

        pm.reap_dead()  # must not raise

    def test_db_unavailable_on_release_warns_and_reaping_continues(self):
        # Fix round 1, Finding 1: reap_dead() must not abandon the rest of
        # the batch just because Postgres went unreachable partway through
        # releasing leases. TTL expiry covers the leak (bounded by
        # LEASE_TTL_SECONDS), same as shutdown_all's forced-termination path.
        rec1 = SimpleNamespace(issue_id="r#1", status="completed", error=None,
                                continuation_count=0, branch=None, number=1, repo="r")
        rec2 = SimpleNamespace(issue_id="r#2", status="completed", error=None,
                                continuation_count=0, branch=None, number=2, repo="r")
        dbsync = FakeUnavailableDbSync()
        pm, state, logger = make_pm(records={"r#1": rec1, "r#2": rec2}, dbsync=dbsync)
        pm._workers["r#1"] = (FakeProc(exitcode=0), object())
        pm._workers["r#2"] = (FakeProc(exitcode=0), object())

        pm.reap_dead()  # must not raise

        assert dbsync.release_attempts == ["r#1", "r#2"], \
            "both workers must still be reaped despite the first release failing"
        assert "unreachable" in logger.text().lower()
        assert "r#1" not in pm._workers and "r#2" not in pm._workers
