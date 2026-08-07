"""Tests for reconcile.py — rebuilding issues.json from GitHub + Postgres at
startup.

Covers the exact bug from docs/plans/12-shared-state-in-postgres.md and
tests/test_shutdown_recovery.py: field_admin#215 was left at IN_PROGRESS by a
crash on 2026-07-29, with `ac-in-progress` still on the issue and no live
lease. Before reconciliation, nothing on a fresh startup would ever move it
off IN_PROGRESS again — the poller only resurrects from
FAILED/COMPLETED/INTERRUPTED. After reconciliation, GitHub's stage plus a
free lease derives QUEUED directly, on every startup, not as a one-time fix.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconcile import derive_status, reconcile  # noqa: E402
from state import IssueRecord, IssueStatus, StateStore  # noqa: E402

_NOW = datetime.now(timezone.utc)
_FUTURE = _NOW + timedelta(minutes=30)
_PAST = _NOW - timedelta(minutes=5)


def _logger():
    return SimpleNamespace(info=lambda *_a: None, warn=lambda *_a: None,
                            error=lambda *_a: None)


def _gh(stage, repo="field_admin", number=215, **extra):
    return {
        "stage": stage, "repo": repo, "number": number,
        "title": "t", "body": "", "labels": [stage] if stage else [],
        "action": "fix", "issue_updated_at": "2026-07-29T00:00:00+00:00",
        "discovered_at": "2026-07-29T00:00:00+00:00",
        **extra,
    }


def _lease_row(owner, expires_at, **extra):
    return {
        "owner_harness_id": owner, "lease_expires_at": expires_at,
        "heartbeat_at": _NOW, "branch": None, "pr_url": None,
        "triage_attempts": 0, "rework_count": 0, "continuation_count": 0,
        "last_error": None, **extra,
    }


class TestDeriveStatus:
    def test_maps_every_frozen_stage(self):
        assert derive_status("ac-pending-review", lease_held_by_other=False) == IssueStatus.DISCOVERED
        assert derive_status("ac-input-needed", lease_held_by_other=False) == IssueStatus.NEEDS_INFO
        assert derive_status("ac-dev-ready", lease_held_by_other=False) == IssueStatus.QUEUED
        assert derive_status("ac-dev-review", lease_held_by_other=False) == IssueStatus.QUEUED
        assert derive_status("ac-hitl", lease_held_by_other=False) == IssueStatus.COMPLETED
        assert derive_status("ac-merged", lease_held_by_other=False) == IssueStatus.COMPLETED
        assert derive_status("ac-done", lease_held_by_other=False) == IssueStatus.COMPLETED
        assert derive_status("ac-blocked", lease_held_by_other=False) == IssueStatus.SKIPPED
        assert derive_status(None, lease_held_by_other=False) == IssueStatus.SKIPPED

    def test_in_progress_with_a_free_lease_resurrects_to_queued(self):
        assert derive_status("ac-in-progress", lease_held_by_other=False) == IssueStatus.QUEUED

    def test_in_progress_with_a_lease_held_by_another_harness_stays_in_progress(self):
        assert derive_status("ac-in-progress", lease_held_by_other=True) == IssueStatus.IN_PROGRESS

    def test_review_in_progress_mirrors_in_progress(self):
        assert derive_status("ac-review-in-progress", lease_held_by_other=False) == IssueStatus.QUEUED
        assert derive_status("ac-review-in-progress", lease_held_by_other=True) == IssueStatus.IN_PROGRESS


class TestReconcileFirstRunEmptyDatabase:
    def test_populates_state_purely_from_github_when_postgres_is_empty(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        report = reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#31": _gh("ac-done", number=31)},
            harness_id="me", logger=_logger(),
        )
        assert report.rebuilt == 1
        assert state.get("field_admin#31").status == IssueStatus.COMPLETED


class TestStrandedInProgressIsResurrected:
    def test_ac_in_progress_with_no_postgres_row_comes_back_queued(self, tmp_path):
        # The exact field_admin#215 shape: ac-in-progress on GitHub, no row in
        # Postgres at all — the first migration ran after the crash.
        state = StateStore(tmp_path / "issues.json")
        report = reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#215": _gh("ac-in-progress")},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#215").status == IssueStatus.QUEUED
        assert "field_admin#215" in report.resurrected

    def test_ac_in_progress_with_an_expired_lease_is_also_resurrected(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#215": _lease_row("old-harness", _PAST, branch="fix/215")}
        report = reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#215": _gh("ac-in-progress")},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#215").status == IssueStatus.QUEUED
        assert state.get("field_admin#215").branch == "fix/215"
        assert "field_admin#215" in report.resurrected
        assert "field_admin#215" in report.leases_released

    def test_ac_review_in_progress_with_an_expired_lease_is_also_resurrected(self, tmp_path):
        # Regression for a review-fix: derive_status() already treats
        # ac-review-in-progress the same as ac-in-progress (both are in
        # stages.LOCKED), but the resurrected list used to be keyed off a
        # literal "ac-in-progress" string comparison, so a stranded review
        # issue was moved to QUEUED without ever appearing in
        # report.resurrected — silently dropping the logger.warn() and
        # undercounting anything Task 11 keys off that list.
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#216": _lease_row("old-harness", _PAST, branch="fix/216")}
        report = reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#216": _gh("ac-review-in-progress", number=216)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#216").status == IssueStatus.QUEUED
        assert "field_admin#216" in report.resurrected
        assert "field_admin#216" in report.leases_released


class TestNullExpiryLeaseShape:
    def test_owner_set_with_null_expiry_is_treated_as_reclaimable(self, tmp_path):
        # Pins the chosen interpretation of an owner_harness_id set alongside
        # a NULL lease_expires_at — a shape that should be unreachable once
        # db/lease.py (Task 12) always writes both columns together, but is
        # not ruled out by this module's own contract.
        #
        # This encodes "NULL expiry == no live lease == reclaimable", i.e.
        # the *opposite* reading from the design doc's illustrative SQL
        # (`owner_harness_id IS NULL OR lease_expires_at < now()`), where
        # `NULL < now()` is unknown in SQL and so a NULL-expiry row would
        # *not* match that WHERE clause. Task 12 is expected to make this
        # shape unreachable by having acquire/release always write both
        # columns together, rather than reconciling the two readings here.
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#217": _lease_row("old-harness", None, branch="fix/217")}
        report = reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#217": _gh("ac-in-progress", number=217)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#217").status == IssueStatus.QUEUED
        assert "field_admin#217" in report.resurrected
        assert "field_admin#217" in report.leases_released


class TestLeaseHeldByAnotherHarnessIsNotResurrected:
    def test_an_unexpired_lease_owned_by_someone_else_stays_in_progress(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#9": _lease_row("other-harness", _FUTURE, branch="fix/9")}
        report = reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#9": _gh("ac-in-progress", number=9)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#9").status == IssueStatus.IN_PROGRESS
        assert "field_admin#9" not in report.resurrected
        assert "field_admin#9" not in report.leases_released

    def test_a_lease_held_by_this_harness_itself_resurrects_normally(self, tmp_path):
        # We own this lease — a crashed harness picking itself back up, not a
        # takeover — so it resurrects the same as a genuinely free lease.
        state = StateStore(tmp_path / "issues.json")
        db_rows = {"field_admin#9": _lease_row("me", _FUTURE, branch="fix/9")}
        reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#9": _gh("ac-in-progress", number=9)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#9").status == IssueStatus.QUEUED


class TestGithubLabelBeatsStaleLocalStatus:
    def test_local_completed_is_overwritten_when_github_now_shows_dev_ready(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        state.add(IssueRecord(
            issue_id="field_admin#5", repo="field_admin", number=5, title="t",
            body="", labels=[], action="fix", status=IssueStatus.COMPLETED,
            discovered_at="x", updated_at="x", issue_updated_at="x",
        ))
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#5": _gh("ac-dev-ready", number=5)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#5").status == IssueStatus.QUEUED


class TestTerminalLabelMapsToCompleted:
    def test_ac_hitl_maps_to_completed(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#1": _gh("ac-hitl", number=1)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#1").status == IssueStatus.COMPLETED


class TestAbsentDbRowDoesNotBlankLocalState:
    """A Postgres outage (or a merely-empty db_rows dict) must not destroy
    locally-held pr_url/branch/counters on a record reconciliation already
    knows about. Absence of a DB row carries no information — see
    .superpowers/sdd/2026-08-07-merge-detection/task-7-brief.md. Before this
    fix, `counters = db_row or {}` treated a missing row the same as a row
    full of empty/zero values, so one outage blanked every known record."""

    def _known_state(self, tmp_path, **overrides):
        state = StateStore(tmp_path / "issues.json")
        fields = dict(
            issue_id="field_admin#50", repo="field_admin", number=50,
            title="t", body="", labels=["ac-dev-ready"], action="fix",
            status=IssueStatus.QUEUED, discovered_at="x", updated_at="x",
            issue_updated_at="x", branch="fix/50", pr_url="https://pr/50",
            triage_attempts=1, rework_count=2, continuation_count=3,
            error="boom",
        )
        fields.update(overrides)
        state.add(IssueRecord(**fields))
        return state

    def test_known_record_keeps_pr_url_when_db_rows_is_empty(self, tmp_path):
        state = self._known_state(tmp_path)
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").pr_url == "https://pr/50"

    def test_known_record_keeps_branch_when_db_rows_is_empty(self, tmp_path):
        state = self._known_state(tmp_path)
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").branch == "fix/50"

    def test_known_record_keeps_rework_count_when_db_rows_is_empty(self, tmp_path):
        state = self._known_state(tmp_path)
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").rework_count == 2

    def test_known_record_keeps_triage_attempts_when_db_rows_is_empty(self, tmp_path):
        state = self._known_state(tmp_path)
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").triage_attempts == 1

    def test_known_record_keeps_continuation_count_when_db_rows_is_empty(self, tmp_path):
        state = self._known_state(tmp_path)
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").continuation_count == 3

    def test_known_record_keeps_error_when_db_rows_is_empty(self, tmp_path):
        state = self._known_state(tmp_path)
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").error == "boom"

    def test_present_db_row_still_overrides_pr_url(self, tmp_path):
        # The authority rule still holds where a row genuinely exists.
        state = self._known_state(tmp_path)
        db_rows = {"field_admin#50": _lease_row(None, None, pr_url="https://pr/new")}
        reconcile(
            state=state, db_rows=db_rows,
            gh_issues={"field_admin#50": _gh("ac-dev-ready", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").pr_url == "https://pr/new"

    def test_status_is_still_derived_from_github_stage_during_total_outage(self, tmp_path):
        # Pins that this fix did not weaken the anti-stranding guarantee:
        # status must still come from GitHub's stage + lease, never from the
        # stale local status, even when db_rows is entirely empty.
        state = self._known_state(
            tmp_path, status=IssueStatus.IN_PROGRESS,
        )
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#50": _gh("ac-dev-review", number=50)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#50").status == IssueStatus.QUEUED

    def test_unknown_issue_with_empty_db_rows_still_gets_current_defaults(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#60": _gh("ac-dev-ready", number=60)},
            harness_id="me", logger=_logger(),
        )
        record = state.get("field_admin#60")
        assert record is not None
        assert record.pr_url is None
        assert record.branch is None
        assert record.triage_attempts == 0
        assert record.rework_count == 0
        assert record.continuation_count == 0
        assert record.error is None


class TestAcHitlSurvivesDbOutageForMergeDetection:
    """Regression tied to this branch's merge-detection feature
    (docs behind ac-hitl -> ac-merged). Poller._check_merged returns early
    when record.pr_url is empty, and ac-hitl is a terminal stage with no
    worker fallback — so if a Postgres outage at startup blanked pr_url here,
    the issue would rot at ac-hitl forever with nothing left to notice the PR
    ever merged. Placed alongside the other absent-db-row cases in this file
    (not in a poller-specific test file) because the defect and its fix are
    entirely in reconcile.py; the poller behaviour it protects is asserted
    by poller tests elsewhere."""

    def test_ac_hitl_record_keeps_pr_url_through_total_db_outage(self, tmp_path):
        state = StateStore(tmp_path / "issues.json")
        state.add(IssueRecord(
            issue_id="field_admin#70", repo="field_admin", number=70,
            title="t", body="", labels=["ac-hitl"], action="fix",
            status=IssueStatus.COMPLETED, discovered_at="x", updated_at="x",
            issue_updated_at="x", branch="fix/70", pr_url="https://pr/70",
        ))
        reconcile(
            state=state, db_rows={},
            gh_issues={"field_admin#70": _gh("ac-hitl", number=70)},
            harness_id="me", logger=_logger(),
        )
        assert state.get("field_admin#70").pr_url == "https://pr/70"
