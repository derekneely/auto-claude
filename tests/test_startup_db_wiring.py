"""Tests for main.py's database-layer startup wiring (Task 11): constructing
Database/Harness/DbSync, registering the harness, reconciling issues.json
from GitHub + Postgres (releasing expired leases first), and wiring
on_change into StateStore.

Degraded mode (`[database].enabled = false`, or the URL environment variable
unset) must never stop the daemon from starting: GitHub labels and the PR are
still truth, and Postgres is explicitly an add-on (docs/plans/
12-shared-state-in-postgres.md, "Three stores").

Startup behaviour is deliberately stricter than runtime (ruling, 2026-07-29):
a harness that cannot prove it holds a valid lease must never begin polling,
since it could double-claim an issue another box already owns. So unlike
runtime — where a database outage must never abort a running agent — a
Postgres that is unreachable OR whose schema is stale/missing at startup
must abort the daemon before it starts, via the existing `main._abort`. Only
"database not configured at all" is exempt: that is single-harness mode,
which never took a lease to begin with.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from db.pool import DbUnavailable  # noqa: E402
from db.schema import SchemaOutOfDate  # noqa: E402
from github_client import GithubClientError  # noqa: E402


def _logger():
    msgs = []
    return SimpleNamespace(
        info=lambda m: msgs.append(("info", m)),
        warn=lambda m: msgs.append(("warn", m)),
        error=lambda m: msgs.append(("error", m)),
        close=lambda: None,
        messages=msgs,
    )


def _db_config(enabled=True, url="postgresql://x"):
    return SimpleNamespace(
        enabled=enabled,
        url=lambda: url,
        connect_timeout_seconds=10,
        journal_file=Path("state/journal.jsonl"),
        lease_ttl_seconds=1800,
    )


def _config(db_config):
    return SimpleNamespace(database=db_config)


class TestInitDbLayerDegradedMode:
    def test_disabled_in_config_yields_no_database(self, monkeypatch):
        cfg = _config(_db_config(enabled=False))
        monkeypatch.setattr(main, "Database", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not construct a Database when disabled")))
        db, _journal, harness, dbsync = main._init_db_layer(cfg, _logger())
        assert db is None
        assert dbsync.enabled is False
        assert harness is not None  # a harness identity always exists, DB or not

    def test_url_unset_yields_no_database_even_when_enabled(self, monkeypatch):
        cfg = _config(_db_config(enabled=True, url=None))
        monkeypatch.setattr(main, "Database", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not construct a Database with no URL")))
        db, _journal, _harness, dbsync = main._init_db_layer(cfg, _logger())
        assert db is None
        assert dbsync.enabled is False

    def test_init_db_layer_wires_the_configured_ttl_into_dbsync(self, monkeypatch):
        # Fix for config.database.lease_ttl_seconds otherwise being dead —
        # Task 13's acquire_lease/heartbeat read dbsync._ttl_seconds, so it
        # has to land here even though nothing consumes it yet.
        cfg = _config(_db_config(enabled=False))
        cfg.database.lease_ttl_seconds = 900
        _db, _journal, _harness, dbsync = main._init_db_layer(cfg, _logger())
        assert dbsync._ttl_seconds == 900

    def test_reconcile_at_startup_tolerates_no_database(self, tmp_path):
        state = main.StateStore(tmp_path / "issues.json")
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [])
        config = SimpleNamespace(
            github=SimpleNamespace(repos=["field_admin"], bot_login="bot"),
        )
        harness = SimpleNamespace(id="me")
        dbsync = main.DbSync(None, harness, _logger(), journal=main.Journal(tmp_path / "j.jsonl"))
        # db=None must not raise — reconciliation runs against an empty
        # db_rows dict, identical to Postgres genuinely being empty.
        main._reconcile_at_startup(config, github, state, None, harness, dbsync, _logger())


class TestInitDbLayerStaleSchemaAborts:
    def test_schema_out_of_date_aborts_before_reaching_dbsync(self, monkeypatch):
        cfg = _config(_db_config(enabled=True, url="postgresql://x"))
        monkeypatch.setattr(main, "Database", lambda *a, **k: object())
        monkeypatch.setattr(
            main, "check_schema_current",
            lambda db: (_ for _ in ()).throw(SchemaOutOfDate("run: alembic upgrade head")),
        )
        with pytest.raises(SystemExit) as excinfo:
            main._init_db_layer(cfg, _logger())
        assert excinfo.value.code == 1


class TestInitDbLayerUnreachablePostgresAborts:
    """Ruling, 2026-07-29: unlike a database outage at *runtime* (which must
    never abort a running agent), Postgres being unreachable at *startup*
    must refuse to start — a harness with no confirmed connection cannot
    safely take a lease, and could double-claim an issue another box already
    owns. This is distinct from TestInitDbLayerStaleSchemaAborts, which
    covers a reachable Postgres with a stale/missing schema; here the
    connection attempt itself fails."""

    def test_db_unavailable_aborts_before_reaching_dbsync(self, monkeypatch):
        cfg = _config(_db_config(enabled=True, url="postgresql://x"))
        monkeypatch.setattr(main, "Database", lambda *a, **k: object())
        monkeypatch.setattr(
            main, "check_schema_current",
            lambda db: (_ for _ in ()).throw(DbUnavailable("could not reach Postgres")),
        )
        with pytest.raises(SystemExit) as excinfo:
            main._init_db_layer(cfg, _logger())
        assert excinfo.value.code == 1


class TestReconcileAtStartupReleasesExpiredLeasesFirst:
    """Guards Fix 5: reconcile()'s own leases_released is derived from
    db_rows, which by the time it is read here already reflects any release
    dbsync.release_expired() just performed — so reconcile() alone cannot
    tell "just freed" apart from "never held". The ids release_expired()
    actually returns are substituted into the report afterwards, which is
    what keeps ReconcileReport.leases_released describing a real Postgres
    UPDATE instead of merely echoing db_rows back."""

    def test_release_expired_runs_before_the_fetch_and_its_ids_land_in_the_report(
        self, tmp_path, monkeypatch,
    ):
        order = []
        state = main.StateStore(tmp_path / "issues.json")
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [])
        config = SimpleNamespace(github=SimpleNamespace(repos=[], bot_login="bot"))
        harness = SimpleNamespace(id="me")

        def fake_fetch_all(db):
            order.append("fetch")
            return {}

        monkeypatch.setattr(main.db_issue_state, "fetch_all", fake_fetch_all)

        def fake_release_expired():
            order.append("release")
            return ["field_admin#7"]

        dbsync = SimpleNamespace(release_expired=fake_release_expired)
        report = main._reconcile_at_startup(
            config, github, state, object(), harness, dbsync, _logger(),
        )

        assert order == ["release", "fetch"], "leases must be released before db_rows is fetched"
        assert report.leases_released == ["field_admin#7"]

    def test_no_release_expired_method_yet_is_a_no_op(self, tmp_path):
        # Guards the getattr(dbsync, "release_expired", None) shim in
        # _reconcile_at_startup: it exists so a dbsync stand-in that omits
        # the method (e.g. an older SimpleNamespace fake, or any future
        # caller that hasn't wired one) still no-ops instead of raising.
        # A real DbSync always has release_expired now (Task 13); this test
        # uses db=None, so DbSync.release_expired's own no-op path (Postgres
        # disabled) is what actually returns [] here.
        state = main.StateStore(tmp_path / "issues.json")
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [])
        config = SimpleNamespace(github=SimpleNamespace(repos=[], bot_login="bot"))
        harness = SimpleNamespace(id="me")
        dbsync = main.DbSync(None, harness, _logger(), journal=main.Journal(tmp_path / "j.jsonl"))

        report = main._reconcile_at_startup(
            config, github, state, None, harness, dbsync, _logger(),
        )

        assert report.leases_released == []


class TestCollectGhIssuesForReconcile:
    def test_shapes_one_row_per_issue_keyed_by_issue_id(self):
        github = SimpleNamespace(list_issues=lambda repo, assignee=None: [
            {"number": 215, "title": "t", "body": "b", "updated_at": "u",
             "created_at": "c", "labels": [{"name": "ac-in-progress"}]},
        ])
        config = SimpleNamespace(
            github=SimpleNamespace(repos=["field_admin"], bot_login="bot"),
        )
        result = main._collect_gh_issues_for_reconcile(config, github, _logger())
        assert set(result) == {"field_admin#215"}
        assert result["field_admin#215"]["stage"] == "ac-in-progress"

    def test_a_failing_repo_does_not_stop_the_others(self):
        def list_issues(repo, assignee=None):
            if repo == "boom":
                raise GithubClientError("down")
            return []

        github = SimpleNamespace(list_issues=list_issues)
        config = SimpleNamespace(
            github=SimpleNamespace(repos=["boom", "field_admin"], bot_login="bot"),
        )
        result = main._collect_gh_issues_for_reconcile(config, github, _logger())
        assert result == {}


class TestOnChangeWrapperUpsertsWithDerivedStage:
    def test_calls_dbsync_upsert_issue_with_the_stage_derived_from_labels(self):
        calls = []
        dbsync = SimpleNamespace(upsert_issue=lambda record, stage: calls.append((record, stage)))
        handler = main._make_on_change(dbsync)
        record = SimpleNamespace(labels=["ac-in-progress", "ac-fix"])
        handler(record)
        assert calls == [(record, "ac-in-progress")]


class TestHarnessIdForWorkers:
    """Final whole-branch review, Critical 1: `main()` used to pass
    `harness.id` into `ProcessManager` unconditionally — `_init_db_layer`
    constructs a real `harness` even when `db` is None (disabled/no URL), so
    every worker's `IssueContext.harness_id` was truthy regardless of
    whether Postgres was configured. Combined with
    `PIPELINE_METRICS_DATABASE_URL` routinely being set for the sibling Node
    telemetry, `worker._assert_lease_held` connected to that unrelated
    Postgres, found no lease row for a harness that never registered
    anywhere, and fenced every single write - the documented
    `[database] enabled = false` rollback switch was the thing that broke
    the daemon. This pins the fix directly: the harness id handed to workers
    must be None whenever there is no database to fence against."""

    def test_none_when_db_is_none(self):
        harness = SimpleNamespace(id="h1")
        assert main._harness_id_for_workers(None, harness) is None

    def test_real_id_when_db_is_configured(self):
        harness = SimpleNamespace(id="h1")
        db = SimpleNamespace()  # any non-None stand-in for a real Database
        assert main._harness_id_for_workers(db, harness) == "h1"


class TestInitDbLayerConstructsARealJournal:
    def test_init_db_layer_returns_a_four_tuple_with_a_real_journal(self, tmp_path):
        cfg = _config(_db_config(enabled=False))
        cfg.database.journal_file = tmp_path / "journal.jsonl"
        db, journal, _harness, dbsync = main._init_db_layer(cfg, _logger())
        assert db is None
        assert isinstance(journal, main.Journal)
        assert dbsync._journal is journal

    def test_register_harness_now_journals_instead_of_only_logging(self, tmp_path, monkeypatch):
        # Task 11 could only log-and-drop a failed registration — db/journal.py
        # did not exist yet. Now that it does, a registration that cannot
        # reach Postgres must be queued for replay, not merely logged.
        harness = main.new_harness("0.2.0")
        journal = main.Journal(tmp_path / "journal.jsonl")
        monkeypatch.setattr(
            main.db_harness, "register",
            lambda *a, **k: (_ for _ in ()).throw(main.DbUnavailable("down")),
        )
        main._register_harness(object(), harness, _logger(), journal)
        assert journal.pending() == 1

    def test_a_non_connectivity_error_is_logged_and_dropped_not_journaled(
        self, tmp_path, monkeypatch,
    ):
        # Final whole-branch review, Finding 5: _register_harness used to
        # catch only DbUnavailable, so any other exception from register()
        # (a bad payload, a constraint violation - not a connectivity
        # problem) propagated out of _init_db_layer as a raw traceback
        # instead of the clean, logged outcome dbsync.py's _durable already
        # gives the equivalent case. Journaling it would be wrong too:
        # replaying the identical bad data would fail identically forever.
        harness = main.new_harness("0.2.0")
        journal = main.Journal(tmp_path / "journal.jsonl")
        monkeypatch.setattr(
            main.db_harness, "register",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("value too long")),
        )
        logger = _logger()

        main._register_harness(object(), harness, logger, journal)  # must not raise

        assert journal.pending() == 0
        assert any("failed" in m.lower() for _lvl, m in logger.messages)

    def test_a_journal_write_failure_after_db_unavailable_is_logged_not_raised(
        self, tmp_path, monkeypatch,
    ):
        # Mirrors dbsync.py's _durable: a journal we cannot write to (disk
        # full, a permissions failure) must not turn a best-effort
        # registration into a startup crash either.
        harness = main.new_harness("0.2.0")
        monkeypatch.setattr(
            main.db_harness, "register",
            lambda *a, **k: (_ for _ in ()).throw(main.DbUnavailable("down")),
        )
        broken_journal = SimpleNamespace(
            append=lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )
        logger = _logger()

        main._register_harness(object(), harness, logger, broken_journal)  # must not raise

        assert any("could not journal" in m.lower() for _lvl, m in logger.messages)
