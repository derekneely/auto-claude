"""The merge sweep: a merged PR advances its issue to ac-merged.

Nothing in auto-claude wrote ac-merged before this. field_admin#268 is the
case that motivated it — PR merged, issue left labelled for a review that
could no longer accomplish anything, board card stuck in "In Review".

The two negative tests at the bottom are the point of the whole design:
ac-done and closing the issue are human prerogatives, and a sweep that runs
unattended every 60s must never quietly absorb them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_client import GithubClient, GithubClientError  # noqa: E402
from poller import Poller  # noqa: E402
from state import IssueRecord, IssueStatus, StateStore  # noqa: E402

from test_poller_stages import make_config, make_issue  # noqa: E402


MERGED = {"number": 341, "state": "closed", "merged": True,
          "merged_at": "2026-08-07T12:59:28Z"}
OPEN = {"number": 341, "state": "open", "merged": False, "merged_at": None}
CLOSED = {"number": 341, "state": "closed", "merged": False, "merged_at": None}


class FakeMergeClient(GithubClient):
    """GithubClient double for the merge sweep.

    Subclass-and-override, matching the convention in test_poller_stages.py
    rather than instance-attribute lambda patching. `label_set` is the
    mutable ground truth `add_label`/`remove_label` mutate; `close_issue` and
    `post_comment` are real methods (not absent ones) so the "never called"
    assertions in TestTheHumanPrerogatives mean something.
    """

    def __init__(self, labels, pr_state, label_write_fails):
        super().__init__("Accelevation")
        self.label_set: set[str] = set(labels)
        self._pr_state = pr_state
        self._label_write_fails = label_write_fails
        self.pr_lookups: list[tuple[str, int]] = []
        self.close_calls: list[tuple[str, int]] = []
        self.comment_calls: list[tuple[str, int, str]] = []

    def list_issues(self, repo, state="open", assignee=None):
        return [make_issue(341, sorted(self.label_set))]

    def add_label(self, repo, number, label):
        if self._label_write_fails:
            raise GithubClientError("label write failed")
        self.label_set.add(label)

    def remove_label(self, repo, number, label):
        if self._label_write_fails:
            raise GithubClientError("label write failed")
        self.label_set.discard(label)

    def get_pr_state(self, repo, pr_number):
        self.pr_lookups.append((repo, pr_number))
        if isinstance(self._pr_state, GithubClientError):
            raise self._pr_state
        return self._pr_state

    def close_issue(self, repo, number):
        self.close_calls.append((repo, number))

    def post_comment(self, repo, number, body):
        self.comment_calls.append((repo, number, body))
        return None


class Env:
    """Drives one `Poller.poll()` and exposes the fake's recorded state."""

    def __init__(self, poller: Poller, client: FakeMergeClient,
                 state: StateStore, issue_id: str, warnings: list[str],
                 errors: list[str]) -> None:
        self._poller = poller
        self._client = client
        self._state = state
        self._issue_id = issue_id
        self.warnings = warnings
        self.errors = errors
        self.queued: list[IssueRecord] = []

    def poll(self) -> None:
        new_issues, _retriage = self._poller.poll()
        # What poll step 5 (main.py) would spawn a worker for on this tick.
        self.queued = list(new_issues)

    def labels_now(self) -> set[str]:
        return set(self._client.label_set)

    def record(self) -> IssueRecord:
        return self._state.get(self._issue_id)

    @property
    def pr_lookups(self) -> list[tuple[str, int]]:
        return self._client.pr_lookups

    @property
    def close_calls(self) -> list[tuple[str, int]]:
        return self._client.close_calls

    @property
    def comment_calls(self) -> list[tuple[str, int, str]]:
        return self._client.comment_calls


# Status -> the valid-transition path seed_record-style helpers walk to reach
# it, mirroring tests/test_poller_stages.py's seed_record.
_STATUS_PATH = {
    IssueStatus.DISCOVERED: [],
    IssueStatus.QUEUED: [IssueStatus.TRIAGING, IssueStatus.QUEUED],
    IssueStatus.IN_PROGRESS: [IssueStatus.TRIAGING, IssueStatus.QUEUED,
                               IssueStatus.IN_PROGRESS],
    IssueStatus.COMPLETED: [IssueStatus.TRIAGING, IssueStatus.QUEUED,
                             IssueStatus.IN_PROGRESS, IssueStatus.COMPLETED],
    IssueStatus.FAILED: [IssueStatus.TRIAGING, IssueStatus.QUEUED,
                          IssueStatus.IN_PROGRESS, IssueStatus.FAILED],
}


@pytest.fixture
def poller_env(tmp_path):
    def _make(
        *,
        stage="ac-hitl",
        pr_state=OPEN,
        extra_labels=None,
        status=IssueStatus.QUEUED,
        mode="dev",
        pr_url="https://github.com/Accelevation/field_admin/pull/341",
        known=True,
        label_write_fails=False,
    ):
        labels = [stage] + list(extra_labels or [])
        issue_id = "field_admin#341"

        client = FakeMergeClient(labels, pr_state, label_write_fails)
        state = StateStore(tmp_path / "issues.json")

        if known:
            record = IssueRecord(
                issue_id=issue_id,
                repo="field_admin",
                number=341,
                title="issue",
                body="",
                labels=labels,
                action="implement",
                status=IssueStatus.DISCOVERED,
                discovered_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:00:00Z",
                issue_updated_at="2026-07-01T00:00:00Z",
                pr_url=pr_url,
                mode=mode,
            )
            state.add(record)
            for step in _STATUS_PATH[status]:
                state.transition(issue_id, step)
            state.save()

        warnings: list[str] = []
        errors: list[str] = []
        logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warn=lambda msg, *a, **k: warnings.append(msg),
            error=lambda msg, *a, **k: errors.append(msg),
        )

        poller = Poller(make_config(), client, state, logger)
        return Env(poller, client, state, issue_id, warnings, errors)

    return _make


class TestAdvancesToMerged:
    def test_hitl_with_a_merged_pr_becomes_ac_merged(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED)
        env.poll()
        assert "ac-merged" in env.labels_now()
        assert "ac-hitl" not in env.labels_now()

    def test_dev_review_with_a_merged_pr_becomes_ac_merged(self, poller_env):
        # The #268 case: merged before the review worker ever ran.
        env = poller_env(stage="ac-dev-review", pr_state=MERGED)
        env.poll()
        assert "ac-merged" in env.labels_now()
        assert "ac-dev-review" not in env.labels_now()

    def test_control_labels_survive_the_transition(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED,
                         extra_labels=["ac-pr-created", "ac-attempt-1"])
        env.poll()
        assert "ac-pr-created" in env.labels_now()
        assert "ac-attempt-1" in env.labels_now()

    def test_the_local_record_reaches_completed(self, poller_env):
        # Otherwise poll step 5 spawns a review worker for a merged PR.
        env = poller_env(stage="ac-dev-review", pr_state=MERGED,
                         status=IssueStatus.QUEUED, mode="review")
        env.poll()
        assert env.record().status == IssueStatus.COMPLETED

    def test_an_already_completed_record_is_not_re_transitioned(self, poller_env):
        # COMPLETED -> COMPLETED is not a legal transition; the sweep must
        # guard rather than raise on the ordinary post-dev-run case.
        env = poller_env(stage="ac-hitl", pr_state=MERGED,
                         status=IssueStatus.COMPLETED)
        env.poll()  # must not raise
        assert env.record().status == IssueStatus.COMPLETED

    def test_the_local_labels_are_refreshed_so_postgres_sees_the_stage(self, poller_env):
        # record.labels is what becomes issue_state.stage via on_change.
        env = poller_env(stage="ac-hitl", pr_state=MERGED)
        env.poll()
        assert "ac-merged" in env.record().labels
        assert "ac-hitl" not in env.record().labels


class TestLeavesEverythingElseAlone:
    def test_an_open_pr_is_untouched(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=OPEN)
        env.poll()
        assert "ac-merged" not in env.labels_now()
        assert "ac-hitl" in env.labels_now()

    def test_in_progress_is_not_watched(self, poller_env):
        env = poller_env(stage="ac-in-progress", pr_state=MERGED)
        env.poll()
        assert "ac-merged" not in env.labels_now()

    def test_review_in_progress_is_not_watched(self, poller_env):
        # A lease holder owns this stage; the review worker handles it.
        env = poller_env(stage="ac-review-in-progress", pr_state=MERGED)
        env.poll()
        assert "ac-merged" not in env.labels_now()

    def test_a_watched_stage_without_a_pr_url_makes_no_gh_call(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED, pr_url=None)
        env.poll()
        assert env.pr_lookups == []
        assert "ac-merged" not in env.labels_now()

    def test_an_untracked_issue_makes_no_gh_call(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED, known=False)
        env.poll()
        assert env.pr_lookups == []


class TestClosedWithoutMerging:
    def test_the_issue_is_left_where_it_is(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=CLOSED)
        env.poll()
        assert "ac-merged" not in env.labels_now()
        assert "ac-hitl" in env.labels_now()

    def test_it_warns_exactly_once_across_repeated_polls(self, poller_env):
        # A 60s poll loop would otherwise emit this warning 1,440 times a day.
        env = poller_env(stage="ac-hitl", pr_state=CLOSED)
        env.poll()
        env.poll()
        env.poll()
        assert len([w for w in env.warnings if "closed without merging" in w]) == 1

    def test_dev_review_with_a_closed_pr_is_not_queued_for_review(self, poller_env):
        # "Leave it exactly where it is" is a no-op at ac-hitl, but NOT at
        # ac-dev-review: that stage is REVIEW_TRIGGER, so falling through to
        # the is_reviewable branch sets the record QUEUED and poll step 5
        # spawns an Opus review worker against a PR a human deliberately
        # closed — every 60s, forever, because nothing ever changes the
        # label. The sweep must claim the issue for this tick instead.
        env = poller_env(stage="ac-dev-review", pr_state=CLOSED,
                         status=IssueStatus.COMPLETED, mode="dev")
        env.poll()
        assert env.record().status != IssueStatus.QUEUED
        assert env.queued == []

    def test_dev_review_with_a_closed_pr_still_leaves_the_label_alone(self, poller_env):
        # Containment, not a verdict: no rewind, no ac-done, no close. The
        # human who closed the PR decides what happens next.
        env = poller_env(stage="ac-dev-review", pr_state=CLOSED,
                         status=IssueStatus.COMPLETED, mode="dev")
        env.poll()
        assert "ac-dev-review" in env.labels_now()
        assert "ac-merged" not in env.labels_now()
        assert "ac-done" not in env.labels_now()
        assert env.close_calls == []

    def test_it_stays_unqueued_across_repeated_polls(self, poller_env):
        # The failure this guards is a per-tick spawn loop, so one tick is
        # not enough evidence.
        env = poller_env(stage="ac-dev-review", pr_state=CLOSED,
                         status=IssueStatus.COMPLETED, mode="dev")
        env.poll()
        env.poll()
        env.poll()
        assert env.record().status != IssueStatus.QUEUED
        assert env.queued == []

    def test_an_open_pr_at_dev_review_is_still_queued_for_review(self, poller_env):
        # Control: the containment must be specific to a *confirmed* closed
        # PR. An open PR at ac-dev-review is exactly what reviews exist for.
        env = poller_env(stage="ac-dev-review", pr_state=OPEN,
                         status=IssueStatus.COMPLETED, mode="dev")
        env.poll()
        assert env.record().status == IssueStatus.QUEUED

    def test_an_unreadable_pr_at_dev_review_is_still_queued_for_review(self, poller_env):
        # Control: "could not ask" is not "confirmed closed". A gh failure
        # must leave the ordinary review path alone rather than silently
        # suppressing reviews for the duration of a GitHub hiccup.
        env = poller_env(stage="ac-dev-review", pr_state=GithubClientError("boom"),
                         status=IssueStatus.COMPLETED, mode="dev")
        env.poll()
        assert env.record().status == IssueStatus.QUEUED


class TestTheSweepNeverRaises:
    """`_check_merged`'s docstring promises "Never raises", but only
    `GithubClientError` is caught. A malformed `get_pr_state` payload would
    raise KeyError out of `_check_merged`, past `poll()`'s
    `except GithubClientError`, and stall the whole 60s loop.
    """

    def test_a_payload_missing_merged_does_not_raise(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state={"number": 341})
        env.poll()  # must not raise
        assert "ac-merged" not in env.labels_now()

    def test_a_payload_missing_state_does_not_raise(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state={"number": 341, "merged": False})
        env.poll()  # must not raise
        assert "ac-hitl" in env.labels_now()


class TestFailureIsNeverFatal:
    def test_a_gh_error_does_not_raise(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=GithubClientError("boom"))
        env.poll()  # must not raise
        assert "ac-merged" not in env.labels_now()
        # Pins the get_pr_state try/except itself: without it, the exception
        # would propagate up to Poller.poll()'s own try/except (poller.py
        # :48-49), which routes it to logger.error, not logger.warn.
        assert env.errors == []

    def test_a_gh_error_still_warns(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=GithubClientError("boom"))
        env.poll()
        assert any("boom" in w for w in env.warnings)

    def test_a_label_write_failure_leaves_the_record_untouched(self, poller_env):
        # Half-applied state is worse than none: a record marked COMPLETED
        # whose label still says ac-hitl would never be swept again.
        env = poller_env(stage="ac-hitl", pr_state=MERGED,
                         status=IssueStatus.QUEUED, label_write_fails=True)
        env.poll()
        assert env.record().status == IssueStatus.QUEUED
        # Pins the add_label/remove_label try/except itself: without it, the
        # error would propagate to Poller.poll()'s outer try/except instead
        # of being caught and warned about here.
        assert any("Could not advance" in w for w in env.warnings)


class TestTheHumanPrerogatives:
    def test_ac_done_is_never_written(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED)
        env.poll()
        assert "ac-done" not in env.labels_now()

    def test_the_issue_is_never_closed(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED)
        env.poll()
        assert env.close_calls == []

    def test_the_pr_is_never_touched(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=MERGED)
        env.poll()
        assert env.comment_calls == []
