"""Tests for the poller's retarget onto the ac-* stage machine.

The trigger used to be "any ac-fix/ac-implement/ac-test/ac-rework label";
it is now `stages.is_claimable(labels)` — exactly `ac-dev-ready`, not
terminal, attempts not exhausted. Verb labels are demoted to kind hints
(`stages.kind_of`) and no longer trigger anything. Terminal stages
(ac-hitl/ac-merged/ac-done/ac-blocked) are hands-off even when local state
disagrees — the label is the source of truth.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stages  # noqa: E402
from github_client import GithubClient  # noqa: E402
from poller import Poller  # noqa: E402
from state import IssueRecord, IssueStatus, StateStore  # noqa: E402


class FakeClient(GithubClient):
    """GithubClient with the network seam replaced by a canned payload."""

    def __init__(self, payload):
        super().__init__("Accelevation")
        self._payload = payload
        self.endpoints: list[str] = []

    def _gh_api(self, endpoint, **kwargs):
        self.endpoints.append(endpoint)
        return self._payload


def make_issue(number, labels, updated_at="2026-07-28T00:00:00Z", title="issue"):
    return {
        "number": number,
        "title": title,
        "body": "",
        "labels": [{"name": lbl} for lbl in labels],
        "assignees": [{"login": "accelevation-bot"}],
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": updated_at,
    }


def make_config():
    return SimpleNamespace(
        github=SimpleNamespace(
            org="Accelevation",
            repos=["field_admin"],
            label_prefix="ac-",
            action_labels=["ac-fix", "ac-implement", "ac-test", "ac-rework"],
            needs_info_label="ac-needs-info",
            in_progress_label="ac-in-progress",
            pr_created_label="ac-pr-created",
            bot_login="accelevation-bot",
        )
    )


def make_logger():
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


def make_poller(payload, state):
    return Poller(make_config(), FakeClient(payload), state, make_logger())


def seed_record(state: StateStore, issue_id, repo, number, labels, status,
                 action="implement", branch=None, pr_url=None, rework_count=0,
                 mode="dev"):
    record = IssueRecord(
        issue_id=issue_id,
        repo=repo,
        number=number,
        title="issue",
        body="",
        labels=labels,
        action=action,
        status=IssueStatus.DISCOVERED,
        discovered_at="2026-07-01T00:00:00Z",
        updated_at="2026-07-01T00:00:00Z",
        issue_updated_at="2026-07-01T00:00:00Z",
        branch=branch,
        pr_url=pr_url,
        rework_count=rework_count,
        mode=mode,
    )
    state.add(record)
    # Walk the record to the requested status via valid transitions so the
    # StateStore's own invariants stay honest.
    path = {
        IssueStatus.DISCOVERED: [],
        IssueStatus.QUEUED: [IssueStatus.TRIAGING, IssueStatus.QUEUED],
        IssueStatus.IN_PROGRESS: [IssueStatus.TRIAGING, IssueStatus.QUEUED, IssueStatus.IN_PROGRESS],
        IssueStatus.COMPLETED: [IssueStatus.TRIAGING, IssueStatus.QUEUED,
                                 IssueStatus.IN_PROGRESS, IssueStatus.COMPLETED],
        IssueStatus.FAILED: [IssueStatus.TRIAGING, IssueStatus.QUEUED,
                              IssueStatus.IN_PROGRESS, IssueStatus.FAILED],
        IssueStatus.INTERRUPTED: [IssueStatus.TRIAGING, IssueStatus.QUEUED,
                                   IssueStatus.IN_PROGRESS, IssueStatus.INTERRUPTED],
    }[status]
    for step in path:
        state.transition(issue_id, step)
    return state.get(issue_id)


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "issues.json")


class TestDiscovery:
    def test_dev_ready_and_assigned_is_discovered(self, state):
        payload = [make_issue(1, ["ac-dev-ready", "ac-implement"])]
        new, _retriage = make_poller(payload, state).poll()
        assert [r.issue_id for r in new] == ["field_admin#1"]

    def test_verb_label_alone_is_not_discovered(self, state):
        payload = [make_issue(2, ["ac-implement"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#2")

    def test_in_progress_is_not_claimed(self, state):
        # Another runner (or this one, mid-flight) owns it.
        payload = [make_issue(3, ["ac-in-progress", "ac-implement"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#3")

    @pytest.mark.parametrize("terminal_label", ["ac-hitl", "ac-merged", "ac-done", "ac-blocked"])
    def test_terminal_labels_are_never_discovered(self, state, terminal_label):
        payload = [make_issue(4, [terminal_label, "ac-implement"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#4")

    def test_attempts_exhausted_is_not_discovered(self, state):
        payload = [make_issue(5, ["ac-dev-ready", "ac-attempt-3"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#5")

    def test_missing_verb_label_defaults_kind_to_implement(self, state):
        payload = [make_issue(6, ["ac-dev-ready"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new[0].action == "implement"

    def test_rework_label_sets_kind_to_rework(self, state):
        payload = [make_issue(7, ["ac-dev-ready", "ac-rework"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new[0].action == "rework"


class TestTerminalOverridesLocalState:
    """A human's terminal label must stick even if local state is stale."""

    @pytest.mark.parametrize("terminal_label", ["ac-hitl", "ac-merged", "ac-done", "ac-blocked"])
    def test_failed_local_state_is_not_resurrected(self, state, terminal_label):
        issue_id = "field_admin#8"
        seed_record(state, issue_id, "field_admin", 8, [terminal_label],
                    IssueStatus.FAILED)
        state.save()

        payload = [make_issue(8, [terminal_label])]
        new, retriage = make_poller(payload, state).poll()

        assert new == []
        assert retriage == []
        assert state.get(issue_id).status == IssueStatus.FAILED

    def test_blocked_beats_a_fresh_dev_ready(self, state):
        # A stale dev-ready label alongside ac-blocked must not resurrect it.
        issue_id = "field_admin#9"
        seed_record(state, issue_id, "field_admin", 9, ["ac-blocked"],
                    IssueStatus.FAILED)
        state.save()

        payload = [make_issue(9, ["ac-dev-ready", "ac-blocked"])]
        new, retriage = make_poller(payload, state).poll()

        assert new == []
        assert retriage == []
        assert state.get(issue_id).status == IssueStatus.FAILED


class TestRetryAndReworkAreLabelGated:
    def test_failed_issue_without_dev_ready_is_not_retried(self, state):
        issue_id = "field_admin#10"
        seed_record(state, issue_id, "field_admin", 10, ["ac-implement"],
                    IssueStatus.FAILED)
        state.save()

        payload = [make_issue(10, ["ac-implement"])]
        new, _retriage = make_poller(payload, state).poll()

        assert new == []
        assert state.get(issue_id).status == IssueStatus.FAILED

    def test_failed_issue_with_dev_ready_is_retried(self, state):
        issue_id = "field_admin#11"
        seed_record(state, issue_id, "field_admin", 11, ["ac-implement"],
                    IssueStatus.FAILED)
        state.save()

        payload = [make_issue(11, ["ac-dev-ready", "ac-implement"])]
        new, _retriage = make_poller(payload, state).poll()

        assert [r.issue_id for r in new] == [issue_id]
        assert state.get(issue_id).status == IssueStatus.DISCOVERED

    def test_completed_rework_without_dev_ready_is_not_reworked(self, state):
        issue_id = "field_admin#12"
        seed_record(state, issue_id, "field_admin", 12, ["ac-rework"],
                    IssueStatus.COMPLETED, branch="ac/issue-12", pr_url="https://x/12")
        state.save()

        payload = [make_issue(12, ["ac-rework"])]
        make_poller(payload, state).poll()

        assert state.get(issue_id).status == IssueStatus.COMPLETED

    def test_completed_rework_with_dev_ready_is_reworked(self, state):
        issue_id = "field_admin#13"
        seed_record(state, issue_id, "field_admin", 13, ["ac-rework"],
                    IssueStatus.COMPLETED, branch="ac/issue-13", pr_url="https://x/13")
        state.save()

        payload = [make_issue(13, ["ac-dev-ready", "ac-rework"])]
        make_poller(payload, state).poll()

        record = state.get(issue_id)
        assert record.status == IssueStatus.QUEUED
        assert record.rework_count == 1
        assert record.branch == "ac/issue-13"
        assert record.pr_url == "https://x/13"


class TestReviewDiscovery:
    """The mirror of TestDiscovery for `stages.is_reviewable` / REVIEW_TRIGGER."""

    def test_dev_review_and_assigned_is_discovered_as_review(self, state):
        payload = [make_issue(20, ["ac-dev-review"])]
        new, _retriage = make_poller(payload, state).poll()
        assert [r.issue_id for r in new] == ["field_admin#20"]
        assert new[0].mode == "review"

    def test_review_issue_skips_triage_and_lands_on_queued(self, state):
        # Triage is meaningless for a PR review; the record must reach QUEUED
        # without passing through TRIAGING/NEEDS_INFO.
        payload = [make_issue(21, ["ac-dev-review"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new[0].status == IssueStatus.QUEUED

    def test_review_in_progress_is_not_discovered(self, state):
        # Another runner (the review worker) already holds this lock.
        payload = [make_issue(22, ["ac-review-in-progress"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#22")

    @pytest.mark.parametrize("terminal_label", ["ac-hitl", "ac-merged", "ac-done", "ac-blocked"])
    def test_terminal_labels_are_never_discovered_as_review(self, state, terminal_label):
        payload = [make_issue(23, [terminal_label, "ac-dev-review"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#23")

    def test_review_attempts_exhausted_is_not_discovered(self, state):
        payload = [make_issue(24, ["ac-dev-review", "ac-attempt-3"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#24")

    def test_neither_claimable_nor_reviewable_is_ignored(self, state):
        payload = [make_issue(25, ["ac-pending-review"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []
        assert not state.is_known("field_admin#25")


class TestModeFlipsOnStageTransition:
    """`mode` must track which stage owns the issue, not just be set once."""

    def test_dev_ready_discovers_with_dev_mode(self, state):
        payload = [make_issue(26, ["ac-dev-ready"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new[0].mode == "dev"

    def test_ready_to_review_between_polls_flips_mode(self, state):
        # Same issue, same state store, two sequential polls — mirrors a real
        # dev worker landing a PR and relabelling ac-dev-ready -> ac-dev-review
        # while auto-claude wasn't looking.
        issue_id = "field_admin#27"
        new1, _ = make_poller([make_issue(27, ["ac-dev-ready"])], state).poll()
        assert new1[0].mode == "dev"
        assert state.get(issue_id).status == IssueStatus.DISCOVERED

        new2, _ = make_poller([make_issue(27, ["ac-dev-review"])], state).poll()
        assert [r.issue_id for r in new2] == [issue_id]
        record = state.get(issue_id)
        assert record.mode == "review"
        assert record.status == IssueStatus.QUEUED

    def test_rework_after_review_resets_mode_to_dev(self, state):
        # A record left over from a review pass (mode="review") that comes
        # back around at ac-dev-ready via rework must not stay stuck in
        # review mode — that would spawn the wrong worker.
        issue_id = "field_admin#28"
        seed_record(state, issue_id, "field_admin", 28, ["ac-rework"],
                    IssueStatus.COMPLETED, action="rework",
                    branch="ac/issue-28", pr_url="https://x/28", mode="review")
        state.save()

        payload = [make_issue(28, ["ac-dev-ready", "ac-rework"])]
        make_poller(payload, state).poll()

        record = state.get(issue_id)
        assert record.mode == "dev"
        assert record.status == IssueStatus.QUEUED

    def test_already_queued_review_is_not_rediscovered(self, state):
        # Idempotency: once a record is staged mode="review"/QUEUED, a poll
        # that sees the same label again must not re-add it to new_issues
        # (that would risk a duplicate spawn upstream).
        issue_id = "field_admin#29"
        seed_record(state, issue_id, "field_admin", 29, ["ac-dev-review"],
                    IssueStatus.QUEUED, mode="review")
        state.save()

        payload = [make_issue(29, ["ac-dev-review"])]
        new, _retriage = make_poller(payload, state).poll()
        assert new == []


class TestLabelSnapshotFollowsGitHub:
    """`record.labels` is a snapshot, and `main._make_on_change` turns it into
    `issue_state.stage` — so a snapshot that stops moving publishes a stage
    that was true once and is now a lie.

    Regression: field_admin#215, 2026-07-31. Every real stage transition is
    performed by the worker, out of process, and `StateUpdate` carries no
    `labels` field, so nothing propagates back. `_poll_repo` is the only place
    that sees the live labels — but it dropped them on the floor for any issue
    it was not about to act on:

      - terminal stages hit `continue` before any state update, so an issue
        that reached ac-hitl froze the DB at ac-dev-review, and one that burned
        three attempts froze it at **ac-dev-ready** — the DB advertising a
        blocked issue as ready to work
      - the locked stages (ac-in-progress / ac-review-in-progress) match
        neither `is_claimable` nor `is_reviewable`, so the known-issue branch
        never touched them either

    Adding `labels` to `StateUpdate` was considered and rejected: it cannot
    cover the stages a human sets (ac-merged, ac-done). The label on GitHub is
    the truth even when the issue is not ours to act on.
    """

    def test_terminal_label_updates_a_known_record(self, state):
        issue_id = "field_admin#215"
        seed_record(state, issue_id, "field_admin", 215, ["ac-dev-review"],
                    IssueStatus.COMPLETED, mode="review")
        state.save()

        payload = [make_issue(215, ["ac-hitl"])]
        new, retriage = make_poller(payload, state).poll()

        assert new == [] and retriage == []
        assert stages.stage_of(state.get(issue_id).labels) == "ac-hitl"

    def test_blocked_replaces_a_stale_dev_ready(self, state):
        # The worst manifestation: three attempts burned, GitHub says
        # ac-blocked, and the DB still says ac-dev-ready.
        issue_id = "field_admin#216"
        seed_record(state, issue_id, "field_admin", 216, ["ac-dev-ready"],
                    IssueStatus.FAILED)
        state.save()

        payload = [make_issue(216, ["ac-blocked", "ac-attempt-3"])]
        make_poller(payload, state).poll()

        assert stages.stage_of(state.get(issue_id).labels) == "ac-blocked"

    def test_terminal_label_does_not_disturb_local_status(self, state):
        # Recording the label is not the same as acting on it — the record's
        # execution history must survive untouched.
        issue_id = "field_admin#217"
        seed_record(state, issue_id, "field_admin", 217, ["ac-dev-review"],
                    IssueStatus.COMPLETED, mode="review")
        state.save()

        make_poller([make_issue(217, ["ac-hitl"])], state).poll()

        record = state.get(issue_id)
        assert record.status == IssueStatus.COMPLETED
        assert record.mode == "review"

    def test_locked_stage_updates_a_known_record(self, state):
        # ac-review-in-progress matches neither trigger predicate, so nothing
        # in the known-issue branch below ever reaches it.
        issue_id = "field_admin#218"
        seed_record(state, issue_id, "field_admin", 218, ["ac-dev-review"],
                    IssueStatus.IN_PROGRESS, mode="review")
        state.save()

        make_poller([make_issue(218, ["ac-review-in-progress"])], state).poll()

        assert stages.stage_of(state.get(issue_id).labels) == "ac-review-in-progress"

    def test_unknown_terminal_issue_is_still_not_tracked(self, state):
        # Recording labels must not become a back door that starts tracking
        # issues auto-claude never claimed.
        make_poller([make_issue(219, ["ac-hitl"])], state).poll()
        assert not state.is_known("field_admin#219")

    def test_changed_labels_are_published_once(self, tmp_path):
        # The whole point: the on_change hook is what writes issue_state.stage.
        published: list[str | None] = []
        store = StateStore(
            tmp_path / "issues.json",
            on_change=lambda r: published.append(stages.stage_of(r.labels)),
        )
        seed_record(store, "field_admin#220", "field_admin", 220,
                    ["ac-dev-review"], IssueStatus.COMPLETED, mode="review")
        store.save()
        published.clear()

        make_poller([make_issue(220, ["ac-hitl"])], store).poll()

        assert published == ["ac-hitl"]

    def test_unchanged_labels_are_not_republished(self, tmp_path):
        # The change-guard. Without it every poll re-upserts every issue every
        # 60 seconds, forever.
        published: list[str | None] = []
        store = StateStore(
            tmp_path / "issues.json",
            on_change=lambda r: published.append(stages.stage_of(r.labels)),
        )
        seed_record(store, "field_admin#221", "field_admin", 221,
                    ["ac-hitl"], IssueStatus.COMPLETED, mode="review")
        store.save()
        published.clear()

        make_poller([make_issue(221, ["ac-hitl"])], store).poll()

        assert published == []


class TestRetriageDoesNotFireOnOurOwnWrite:
    """Re-triage triggers on any updated_at change, and auto-claude's own
    label write bumps updated_at. `main._run_triage` re-fetches updated_at
    after posting the needs-info comment to close that gap. Without it, every
    60s poll would re-triage an issue no human has answered — a triage call
    per tick until someone responds.
    """

    @staticmethod
    def _seed_needs_info(state, issue_id, repo, number, issue_updated_at):
        # seed_record's status-path table stops at DISCOVERED/QUEUED/etc and
        # has no route to NEEDS_INFO, so this walks the DISCOVERED ->
        # TRIAGING -> NEEDS_INFO path directly and then stamps the
        # GitHub-side timestamp the retriage check compares against.
        record = IssueRecord(
            issue_id=issue_id,
            repo=repo,
            number=number,
            title="issue",
            body="",
            labels=["ac-input-needed"],
            action="implement",
            status=IssueStatus.DISCOVERED,
            discovered_at="2026-07-01T00:00:00Z",
            updated_at="2026-07-01T00:00:00Z",
            issue_updated_at="2026-07-01T00:00:00Z",
            mode="dev",
        )
        state.add(record)
        state.transition(issue_id, IssueStatus.TRIAGING)
        state.transition(issue_id, IssueStatus.NEEDS_INFO)
        state.update(issue_id, issue_updated_at=issue_updated_at)
        state.save()
        return state.get(issue_id)

    def test_a_needs_info_issue_whose_timestamp_matches_is_not_retriaged(self, state):
        issue_id = "field_admin#30"
        self._seed_needs_info(state, issue_id, "field_admin", 30,
                               issue_updated_at="T1")

        payload = [make_issue(30, ["ac-input-needed"], updated_at="T1")]
        _new, retriage = make_poller(payload, state).poll()

        assert retriage == []

    def test_a_genuine_human_response_is_retriaged(self, state):
        issue_id = "field_admin#31"
        self._seed_needs_info(state, issue_id, "field_admin", 31,
                               issue_updated_at="T1")

        payload = [make_issue(31, ["ac-input-needed"], updated_at="T2")]
        _new, retriage = make_poller(payload, state).poll()

        assert [r.issue_id for r in retriage] == [issue_id]

    def test_run_triage_refetches_updated_at_after_posting(self):
        # Source-text assertion: the re-fetch is the only thing standing
        # between needs_info and a per-tick triage loop, and it is easy to
        # drop during an unrelated refactor of the needs-info branch. The
        # brief's proposed spelling was "issue_updated_at=fresh.get" — the
        # real code matches that exactly, so it is asserted verbatim here.
        # The re-fetch now lives in the shared comment poster, which every
        # triage comment path (findings, questions, stuck) routes through.
        import inspect

        import main as main_module

        body = inspect.getsource(main_module._post_triage_comment)
        assert "issue_updated_at=fresh.get" in body, (
            "main._post_triage_comment must re-read updated_at after applying "
            "ac-input-needed, or the poller re-triages its own label write"
        )


class TestRetriageRereadsTheIssue:
    """A re-triage must read the issue as it is now, not as it was when parked.

    The trigger for a re-triage is "updated_at moved", and the most common
    reason it moved is that the human edited the body to answer the question
    triage asked. Re-triaging the stored copy would read the text from before
    the answer and ask the same question again.
    """

    def _parked(self, state, *, body="old body", title="old title"):
        from state import IssueRecord, IssueStatus

        issue_id = "field_admin#40"
        state.add(IssueRecord(
            issue_id=issue_id, repo="field_admin", number=40,
            title=title, body=body, labels=["ac-input-needed"],
            action="implement", status=IssueStatus.DISCOVERED,
            discovered_at="", updated_at="", issue_updated_at="T1",
            mode="dev",
        ))
        state.transition(issue_id, IssueStatus.TRIAGING)
        state.transition(issue_id, IssueStatus.NEEDS_INFO)
        state.update(issue_id, issue_updated_at="T1")
        state.save()
        return issue_id

    def test_the_edited_body_reaches_triage(self, state):
        self._parked(state)
        payload = [make_issue(40, ["ac-input-needed"], updated_at="T2")]
        payload[0]["body"] = "new body answering the question"

        _new, retriage = make_poller(payload, state).poll()

        assert [r.body for r in retriage] == ["new body answering the question"]

    def test_the_edited_title_reaches_triage(self, state):
        self._parked(state)
        payload = [make_issue(40, ["ac-input-needed"], updated_at="T2")]
        payload[0]["title"] = "clearer title"

        _new, retriage = make_poller(payload, state).poll()

        assert [r.title for r in retriage] == ["clearer title"]

    def test_the_refreshed_body_is_persisted_not_just_handed_over(self, state):
        issue_id = self._parked(state)
        payload = [make_issue(40, ["ac-input-needed"], updated_at="T2")]
        payload[0]["body"] = "new body"

        make_poller(payload, state).poll()

        assert state.get(issue_id).body == "new body"

    def test_an_unchanged_timestamp_still_does_not_retrigger(self, state):
        self._parked(state)
        payload = [make_issue(40, ["ac-input-needed"], updated_at="T1")]
        payload[0]["body"] = "edited but timestamp unchanged"

        _new, retriage = make_poller(payload, state).poll()

        assert retriage == []
