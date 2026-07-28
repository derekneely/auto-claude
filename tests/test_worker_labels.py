"""Tests for worker.py's stage-label transitions.

Pure functions only — no subprocess, no network. `worker.py` reads live labels
from GitHub via `gh` at the point of transition (see `_get_issue_labels`), then
hands them to these pure functions to compute the add/remove sets. That split
is what makes the label logic testable at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import (  # noqa: E402
    _labels_for_claim,
    _labels_for_failure,
    _labels_for_success,
)


class TestLabelsForClaim:
    def test_claim_from_dev_ready(self):
        add, remove = _labels_for_claim(["ac-dev-ready", "ac-fix"])
        assert add == ["ac-in-progress"]
        assert remove == ["ac-dev-ready"]

    def test_verb_label_survives(self):
        add, remove = _labels_for_claim(["ac-dev-ready", "ac-implement"])
        assert "ac-implement" not in add
        assert "ac-implement" not in remove

    def test_self_heals_a_half_applied_transition(self):
        # Both a stale ac-dev-ready and a lingering ac-in-progress present —
        # transition removes every stage label but the target.
        add, remove = _labels_for_claim(["ac-dev-ready", "ac-in-progress", "ac-fix"])
        assert add == []
        assert set(remove) == {"ac-dev-ready"}


class TestLabelsForSuccess:
    def test_success_hands_off_to_dev_review(self):
        add, remove = _labels_for_success(["ac-in-progress", "ac-implement"])
        assert set(add) == {"ac-dev-review", "ac-pr-created"}
        assert remove == ["ac-in-progress"]

    def test_verb_label_survives(self):
        add, remove = _labels_for_success(["ac-in-progress", "ac-fix"])
        assert "ac-fix" not in add
        assert "ac-fix" not in remove

    def test_rework_verb_survives_success_too(self):
        # Regression: the old code hardcoded remove=["ac-rework", "ac-in-progress"]
        # on rework success, stripping the kind label. It must not anymore.
        add, remove = _labels_for_success(["ac-in-progress", "ac-rework"])
        assert "ac-rework" not in remove

    def test_preserves_existing_attempt_counter(self):
        add, remove = _labels_for_success(["ac-in-progress", "ac-attempt-1"])
        assert "ac-attempt-1" not in remove
        assert "ac-attempt-1" not in add


class TestLabelsForFailure:
    def test_first_failure_goes_back_to_dev_ready_with_attempt_1(self):
        add, remove, blocked = _labels_for_failure(["ac-in-progress", "ac-implement"])
        assert set(add) == {"ac-dev-ready", "ac-attempt-1"}
        assert remove == ["ac-in-progress"]
        assert blocked is False

    def test_second_failure_bumps_to_attempt_2(self):
        add, remove, blocked = _labels_for_failure(
            ["ac-in-progress", "ac-attempt-1", "ac-implement"]
        )
        assert set(add) == {"ac-dev-ready", "ac-attempt-2"}
        assert set(remove) == {"ac-in-progress", "ac-attempt-1"}
        assert blocked is False

    def test_third_failure_blocks_instead_of_requeueing(self):
        add, remove, blocked = _labels_for_failure(
            ["ac-in-progress", "ac-attempt-2", "ac-implement"]
        )
        assert set(add) == {"ac-blocked", "ac-attempt-3"}
        assert set(remove) == {"ac-in-progress", "ac-attempt-2"}
        assert blocked is True

    def test_verb_label_survives_every_failure(self):
        for labels in (
            ["ac-in-progress", "ac-fix"],
            ["ac-in-progress", "ac-attempt-1", "ac-fix"],
            ["ac-in-progress", "ac-attempt-2", "ac-fix"],
        ):
            add, remove, _blocked = _labels_for_failure(labels)
            assert "ac-fix" not in add
            assert "ac-fix" not in remove

    def test_unrelated_labels_survive(self):
        add, remove, _blocked = _labels_for_failure(["ac-in-progress", "bug", "P1"])
        assert "bug" not in add and "bug" not in remove
        assert "P1" not in add and "P1" not in remove
