"""Tests for the canonical ac-* stage vocabulary.

This module is the contract auto-claude shares with accelevation-claude-tools.
Every literal here must match `commands/scripts/setup-pipeline-labels.sh` in that
repo - a typo is not a test failure, it is two agents silently disagreeing about
who owns an issue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stages  # noqa: E402


class TestVocabulary:
    def test_trigger_is_dev_ready(self):
        assert stages.TRIGGER == "ac-dev-ready"

    def test_stage_labels_match_the_shared_label_set(self):
        # Mirrors setup-pipeline-labels.sh. Update both or neither.
        assert stages.STAGE_LABELS == (
            "ac-pending-review",
            "ac-dev-ready",
            "ac-in-progress",
            "ac-dev-review",
            "ac-review-in-progress",
            "ac-hitl",
            "ac-merged",
            "ac-done",
            "ac-input-needed",
            "ac-blocked",
        )

    def test_terminal_stages_are_hands_off(self):
        assert stages.TERMINAL == frozenset({
            "ac-hitl", "ac-merged", "ac-done", "ac-blocked",
        })

    def test_every_terminal_stage_is_a_real_stage(self):
        assert stages.TERMINAL <= set(stages.STAGE_LABELS)

    def test_control_labels_are_not_stages(self):
        # ac-reviewed / ac-pr-created / ac-attempt-N coexist with a stage; if
        # they were treated as stages, stage_of() would return the wrong one.
        assert not (set(stages.CONTROL_LABELS) & set(stages.STAGE_LABELS))

    def test_max_attempts_is_three(self):
        assert stages.MAX_ATTEMPTS == 3


class TestLabelSpecs:
    """The create-on-startup table must cover everything we ever write."""

    def test_covers_every_label_in_the_vocabulary(self):
        declared = {name for name, _color, _desc in stages.LABEL_SPECS}
        required = (
            set(stages.STAGE_LABELS)
            | set(stages.CONTROL_LABELS)
            | set(stages.KIND_LABELS)
        )
        assert not (required - declared), (
            f"no colour/description for: {sorted(required - declared)} - "
            f"auto-claude would try to apply a label it never creates"
        )

    def test_declares_nothing_retired(self):
        declared = {name for name, _color, _desc in stages.LABEL_SPECS}
        assert not (declared & stages.RETIRED_LABELS)

    def test_no_duplicates(self):
        names = [name for name, _color, _desc in stages.LABEL_SPECS]
        assert len(names) == len(set(names))

    def test_colours_are_bare_six_digit_hex(self):
        # The GitHub API rejects a leading '#'.
        for name, color, _desc in stages.LABEL_SPECS:
            assert len(color) == 6 and not color.startswith("#"), name
            int(color, 16)

    def test_every_label_has_a_description(self):
        for name, _color, desc in stages.LABEL_SPECS:
            assert desc.strip(), name


class TestStageOf:
    def test_finds_the_stage(self):
        assert stages.stage_of(["ac-fix", "ac-dev-ready"]) == "ac-dev-ready"

    def test_none_when_no_stage_label(self):
        assert stages.stage_of(["ac-fix", "bug", "P1"]) is None

    def test_ignores_control_labels(self):
        assert stages.stage_of(["ac-reviewed", "ac-pr-created"]) is None

    def test_ignores_unrelated_labels(self):
        assert stages.stage_of(["enhancement", "good first issue"]) is None

    def test_returns_the_earliest_stage_when_several_are_present(self):
        # Shouldn't happen, but a half-applied transition must resolve
        # deterministically rather than depending on GitHub's label ordering.
        got = stages.stage_of(["ac-done", "ac-in-progress"])
        assert got == "ac-in-progress"

    def test_is_order_independent(self):
        a = stages.stage_of(["ac-done", "ac-in-progress"])
        b = stages.stage_of(["ac-in-progress", "ac-done"])
        assert a == b

    def test_handles_empty(self):
        assert stages.stage_of([]) is None


class TestIsTerminal:
    @pytest.mark.parametrize("label", ["ac-hitl", "ac-merged", "ac-done", "ac-blocked"])
    def test_terminal_stages(self, label):
        assert stages.is_terminal([label])

    @pytest.mark.parametrize("label", ["ac-dev-ready", "ac-in-progress", "ac-pending-review"])
    def test_active_stages(self, label):
        assert not stages.is_terminal([label])

    def test_no_stage_is_not_terminal(self):
        assert not stages.is_terminal(["ac-fix"])


class TestIsClaimable:
    """The single predicate the poller uses to decide 'is this mine to start'."""

    def test_dev_ready_is_claimable(self):
        assert stages.is_claimable(["ac-dev-ready", "ac-implement"])

    def test_terminal_is_not(self):
        assert not stages.is_claimable(["ac-done"])

    def test_another_runners_lock_is_not(self):
        assert not stages.is_claimable(["ac-in-progress"])

    def test_pending_review_is_not(self):
        # Belongs to triage, not to the dev worker.
        assert not stages.is_claimable(["ac-pending-review"])

    def test_unlabelled_is_not(self):
        assert not stages.is_claimable(["bug"])

    def test_blocked_beats_dev_ready(self):
        # A human marked this blocked; a stale ac-dev-ready must not resurrect it.
        assert not stages.is_claimable(["ac-dev-ready", "ac-blocked"])

    def test_exhausted_attempts_are_not_claimable(self):
        assert not stages.is_claimable(["ac-dev-ready", "ac-attempt-3"])


class TestStaleReset:
    """Crash recovery: a lock with no live worker behind it must rewind."""

    def test_in_progress_rewinds_to_dev_ready(self):
        assert stages.stale_reset_target(["ac-in-progress"]) == "ac-dev-ready"

    def test_review_in_progress_rewinds_to_dev_review(self):
        assert stages.stale_reset_target(["ac-review-in-progress"]) == "ac-dev-review"

    def test_unlocked_stages_do_not_rewind(self):
        assert stages.stale_reset_target(["ac-dev-ready"]) is None
        assert stages.stale_reset_target(["ac-hitl"]) is None

    def test_no_stage_does_not_rewind(self):
        assert stages.stale_reset_target(["ac-fix"]) is None
        assert stages.stale_reset_target([]) is None

    def test_every_locked_stage_has_a_reset_target(self):
        assert set(stages.STALE_RESET) == stages.LOCKED

    def test_reset_targets_are_real_stages(self):
        assert set(stages.STALE_RESET.values()) <= set(stages.STAGE_LABELS)

    def test_reset_never_targets_a_locked_stage(self):
        # Rewinding a lock to another lock would never recover.
        assert not (set(stages.STALE_RESET.values()) & stages.LOCKED)


class TestIsReviewable:
    """The review worker's trigger — mirror of is_claimable."""

    def test_dev_review_is_reviewable(self):
        assert stages.is_reviewable(["ac-dev-review", "ac-pr-created"])

    def test_dev_ready_is_not(self):
        assert not stages.is_reviewable(["ac-dev-ready"])

    def test_another_runners_review_lock_is_not(self):
        assert not stages.is_reviewable(["ac-review-in-progress"])

    def test_terminal_is_not(self):
        assert not stages.is_reviewable(["ac-hitl"])
        assert not stages.is_reviewable(["ac-dev-review", "ac-blocked"])

    def test_exhausted_attempts_are_not_reviewable(self):
        # Nothing a fourth review could say would change the outcome.
        assert not stages.is_reviewable(["ac-dev-review", "ac-attempt-3"])

    def test_unlabelled_is_not(self):
        assert not stages.is_reviewable(["bug"])

    def test_the_two_triggers_are_mutually_exclusive(self):
        # An issue must never queue for both workers at once.
        for labels in (["ac-dev-ready"], ["ac-dev-review"], ["ac-in-progress"], []):
            assert not (stages.is_claimable(labels) and stages.is_reviewable(labels))


class TestKindOf:
    def test_reads_the_verb_label(self):
        assert stages.kind_of(["ac-dev-ready", "ac-fix"]) == "fix"

    @pytest.mark.parametrize("label,kind", [
        ("ac-fix", "fix"), ("ac-implement", "implement"),
        ("ac-test", "test"), ("ac-rework", "rework"),
    ])
    def test_every_kind(self, label, kind):
        assert stages.kind_of([label]) == kind

    def test_defaults_when_absent(self):
        # A loop-created issue carries no verb label; it still has to run.
        assert stages.kind_of(["ac-dev-ready"]) == "implement"

    def test_rework_wins_over_other_verbs(self):
        # An issue relabelled for rework keeps its original verb; rework is the
        # more specific instruction and picks the rework prompt.
        assert stages.kind_of(["ac-implement", "ac-rework"]) == "rework"

    def test_stage_labels_are_never_mistaken_for_kinds(self):
        assert stages.kind_of(["ac-dev-ready", "ac-in-progress"]) == "implement"


class TestAttempts:
    def test_no_label_is_attempt_zero(self):
        assert stages.attempt_of(["ac-dev-ready"]) == 0

    @pytest.mark.parametrize("n", [1, 2, 3])
    def test_reads_the_counter(self, n):
        assert stages.attempt_of([f"ac-attempt-{n}"]) == n

    def test_takes_the_highest_when_several_linger(self):
        assert stages.attempt_of(["ac-attempt-1", "ac-attempt-3"]) == 3

    def test_ignores_malformed(self):
        assert stages.attempt_of(["ac-attempt-", "ac-attempt-x"]) == 0

    def test_attempt_label_formats(self):
        assert stages.attempt_label(2) == "ac-attempt-2"

    def test_exhausted_at_max(self):
        assert stages.attempts_exhausted(["ac-attempt-3"])
        assert not stages.attempts_exhausted(["ac-attempt-2"])

    def test_over_max_is_still_exhausted(self):
        assert stages.attempts_exhausted(["ac-attempt-3", "ac-attempt-2"])


class TestMergeWatch:
    def test_merge_watch_holds_exactly_the_two_human_facing_stages(self):
        assert stages.MERGE_WATCH == frozenset({"ac-dev-review", "ac-hitl"})

    def test_locked_stages_are_never_watched(self):
        # A lease holder owns the label write on a locked stage; main sweeping
        # it would race the worker's own fenced write at the end of its run.
        assert not (stages.MERGE_WATCH & stages.LOCKED)

    def test_merge_watch_never_includes_ac_merged_itself(self):
        # Re-sweeping an already-merged issue would re-write the same label
        # every 60s forever.
        assert "ac-merged" not in stages.MERGE_WATCH

    def test_every_watched_stage_is_a_real_stage_label(self):
        for label in stages.MERGE_WATCH:
            assert label in stages.STAGE_LABELS


class TestTransition:
    """Label add/remove sets. Exactly one stage label must survive."""

    def test_claiming_swaps_dev_ready_for_in_progress(self):
        add, remove = stages.transition(["ac-dev-ready", "ac-fix"], "ac-in-progress")
        assert add == ["ac-in-progress"]
        assert remove == ["ac-dev-ready"]

    def test_never_removes_the_kind_label(self):
        _add, remove = stages.transition(["ac-dev-ready", "ac-fix"], "ac-in-progress")
        assert "ac-fix" not in remove

    def test_never_removes_unrelated_labels(self):
        _add, remove = stages.transition(["ac-dev-ready", "bug", "P1"], "ac-in-progress")
        assert "bug" not in remove and "P1" not in remove

    def test_removes_every_stale_stage_label(self):
        _add, remove = stages.transition(
            ["ac-dev-ready", "ac-in-progress"], "ac-dev-review"
        )
        assert set(remove) == {"ac-dev-ready", "ac-in-progress"}

    def test_no_op_when_already_in_the_target_stage(self):
        add, remove = stages.transition(["ac-in-progress"], "ac-in-progress")
        assert add == [] and remove == []

    def test_rejects_an_unknown_target(self):
        with pytest.raises(ValueError):
            stages.transition(["ac-dev-ready"], "ac-not-a-stage")

    def test_preserves_control_labels(self):
        _add, remove = stages.transition(
            ["ac-in-progress", "ac-pr-created", "ac-attempt-1"], "ac-dev-review"
        )
        assert "ac-pr-created" not in remove
        assert "ac-attempt-1" not in remove
