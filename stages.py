"""The canonical ac-* stage vocabulary.

This is the contract auto-claude shares with `accelevation-claude-tools`. Both
runners read and write the same labels on the same issues, so every literal in
this module must match `commands/scripts/setup-pipeline-labels.sh` in that repo.

Two orthogonal axes, which the two systems used to conflate:

**Stage** - exactly one per issue, and it is the state machine:

    ac-pending-review -> ac-dev-ready -> ac-in-progress -> ac-dev-review
                                      -> ac-review-in-progress -> ac-hitl
                                      -> ac-merged -> ac-done

    plus ac-input-needed (waiting on a human) and ac-blocked (failed MAX_ATTEMPTS).

**Kind** - auto-claude's original verbs (`ac-fix`, `ac-implement`, ...), demoted
to hints. They no longer trigger anything; they select a prompt and a model.

The stage is the trigger, the assignee is the owner (see
`docs/plans/11-taxonomy-consolidation.md` §3), and the kind is advice.
"""

from __future__ import annotations

from collections.abc import Iterable

# The label that queues an issue for the dev worker.
TRIGGER = "ac-dev-ready"

# The label that queues an issue for the review worker. auto-claude reviews its
# own PRs because the sibling toolchain's review agent scopes itself with
# `--assignee @me` and so never sees a bot-assigned issue.
REVIEW_TRIGGER = "ac-dev-review"

# Ordered earliest-to-latest. The order is load-bearing: `stage_of` resolves a
# half-applied transition (two stage labels present at once) to the earliest,
# so a crash mid-swap is re-run rather than skipped forward.
STAGE_LABELS: tuple[str, ...] = (
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

# Stages where auto-claude hands off and does nothing further. `ac-hitl` is a
# human review gate, not a failure.
TERMINAL = frozenset({"ac-hitl", "ac-merged", "ac-done", "ac-blocked"})

# A stage owned by a runner that is actively working. Not ours to touch.
LOCKED = frozenset({"ac-in-progress", "ac-review-in-progress"})

# Stages where a merged PR should advance the issue to ac-merged, swept by
# `Poller._poll_repo`. Deliberately excludes every member of LOCKED: a lease
# holder owns the label write there, and `main` sweeping it would race the
# worker's own fenced write. The review worker handles its own stage itself.
#
# `ac-dev-review` is watched, not just `ac-hitl`, because a human routinely
# merges before the review worker ever picks the issue up — and a merged PR
# cannot be usefully reviewed.
MERGE_WATCH = frozenset({"ac-dev-review", "ac-hitl"})

# Where a stale lock rewinds to. Mirrors the sibling planner's stale-lock reset
# mapping so a lock left by either runner recovers to the same place.
STALE_RESET: dict[str, str] = {
    "ac-in-progress": "ac-dev-ready",
    "ac-review-in-progress": "ac-dev-review",
}


def stale_reset_target(labels: Iterable[str]) -> str | None:
    """The stage a stuck issue should rewind to, or None if it is not locked."""
    return STALE_RESET.get(stage_of(labels) or "")

ATTEMPT_PREFIX = "ac-attempt-"
MAX_ATTEMPTS = 3

# Non-stage labels that coexist with a stage and must survive transitions.
CONTROL_LABELS: tuple[str, ...] = (
    "ac-reviewed",
    "ac-pr-created",
    *(f"{ATTEMPT_PREFIX}{n}" for n in range(1, MAX_ATTEMPTS + 1)),
)

# Kind hints -> the action name used for prompt and model selection.
KIND_LABELS: dict[str, str] = {
    "ac-fix": "fix",
    "ac-implement": "implement",
    "ac-test": "test",
    "ac-rework": "rework",
}

DEFAULT_KIND = "implement"

# Checked before the rest: an issue relabelled for rework keeps its original
# verb, and rework is the more specific instruction.
_KIND_PRIORITY = ("ac-rework",)

_STAGE_ORDER = {label: i for i, label in enumerate(STAGE_LABELS)}


def stage_of(labels: Iterable[str]) -> str | None:
    """The issue's current stage, or None if it has no stage label."""
    present = [lbl for lbl in labels if lbl in _STAGE_ORDER]
    if not present:
        return None
    return min(present, key=lambda lbl: _STAGE_ORDER[lbl])


def is_terminal(labels: Iterable[str]) -> bool:
    """True when the issue has reached a stage auto-claude does not act on."""
    labels = list(labels)
    # Any terminal label wins, even alongside an earlier stage - a human who
    # marked something ac-blocked outranks a stale ac-dev-ready.
    return bool(TERMINAL & set(labels))


def attempt_of(labels: Iterable[str]) -> int:
    """The highest ac-attempt-N counter present, or 0."""
    best = 0
    for label in labels:
        if not label.startswith(ATTEMPT_PREFIX):
            continue
        suffix = label[len(ATTEMPT_PREFIX):]
        if suffix.isdigit():
            best = max(best, int(suffix))
    return best


def attempt_label(n: int) -> str:
    return f"{ATTEMPT_PREFIX}{n}"


def attempts_exhausted(labels: Iterable[str]) -> bool:
    return attempt_of(labels) >= MAX_ATTEMPTS


def is_claimable(labels: Iterable[str]) -> bool:
    """True when auto-claude may start work on this issue.

    The label half of the trigger contract; the caller is responsible for the
    assignee half (see `poller`/`github_client.list_issues`).
    """
    labels = list(labels)
    if is_terminal(labels):
        return False
    if attempts_exhausted(labels):
        return False
    return stage_of(labels) == TRIGGER


def is_reviewable(labels: Iterable[str]) -> bool:
    """True when auto-claude may review the issue's open PR.

    The mirror of `is_claimable` for the review stage. Attempts-exhausted is
    excluded here too: an issue that has burned all three attempts belongs at
    `ac-blocked`, and reviewing it again cannot change that.
    """
    labels = list(labels)
    if is_terminal(labels):
        return False
    if attempts_exhausted(labels):
        return False
    return stage_of(labels) == REVIEW_TRIGGER


def kind_of(labels: Iterable[str], default: str = DEFAULT_KIND) -> str:
    """The work kind, for prompt and model selection.

    Defaults rather than failing: issues created by the loop carry no verb
    label, and they still have to run.
    """
    labels = list(labels)
    for label in _KIND_PRIORITY:
        if label in labels:
            return KIND_LABELS[label]
    for label in labels:
        if label in KIND_LABELS:
            return KIND_LABELS[label]
    return default


def transition(
    labels: Iterable[str],
    target: str,
) -> tuple[list[str], list[str]]:
    """Compute (add, remove) to move an issue to `target`.

    Removes *every* other stage label, not just the expected predecessor, so a
    half-applied transition self-heals instead of leaving two stages set. Kind
    hints, control labels and unrelated labels are never removed.
    """
    if target not in _STAGE_ORDER:
        raise ValueError(
            f"{target!r} is not a stage label; expected one of {STAGE_LABELS}"
        )

    labels = list(labels)
    stale = sorted(
        (lbl for lbl in set(labels) if lbl in _STAGE_ORDER and lbl != target),
        key=lambda lbl: _STAGE_ORDER[lbl],
    )
    add = [] if target in labels else [target]
    return add, stale


# ---------------------------------------------------------------------------
# Label definitions
# ---------------------------------------------------------------------------

# Colour and description for every label auto-claude may create, mirroring
# `commands/scripts/setup-pipeline-labels.sh` in accelevation-claude-tools so a
# repo stamped by either system looks the same.
#
# auto-claude creates only; it never updates an existing label. The sibling
# script uses `--force`, which *does* overwrite - so where the two disagree, the
# sibling wins and we leave it alone. That is the intended precedence: this file
# is a mirror, not a second source of truth.
#
# The kind hints at the end are auto-claude's own and have no counterpart there.
LABEL_SPECS: tuple[tuple[str, str, str], ...] = (
    # (name, colour, description)
    ("ac-pending-review", "C2E0C6", "Queued - awaiting Claude's spec review + human approval"),
    ("ac-input-needed", "FBCA04", "Triage found gaps; needs human clarification"),
    ("ac-dev-ready", "0E8A16", "Human-approved - loop may pick up (HUMAN gate 1)"),
    ("ac-in-progress", "1D76DB", "Dev agent working (self-lock)"),
    ("ac-dev-review", "5319E7", "PR open - awaiting agent review"),
    ("ac-review-in-progress", "0075CA", "Review agent working (self-lock)"),
    ("ac-hitl", "E4E669", "Passed agent review - human testing PR"),
    ("ac-merged", "006B75", "Human merged the PR to dev - pending release (HUMAN gate 3)"),
    ("ac-done", "BFD4F2", "Pipeline complete (merged to dev, worktree pruned) - awaiting prod release"),
    ("ac-blocked", "B60205", "Failed 3x or hard-blocked - human-only"),
    ("ac-reviewed", "D4C5F9", "Claude reviewed the spec (idempotency guard)"),
    ("ac-attempt-1", "F9D0C4", "Dev attempt 1"),
    ("ac-attempt-2", "F9D0C4", "Dev attempt 2"),
    ("ac-attempt-3", "F9D0C4", "Dev attempt 3"),
    ("ac-pr-created", "C5DEF5", "PR opened by dev agent"),
    # Kind hints - auto-claude only. Not part of the shared taxonomy.
    ("ac-fix", "D93F0B", "Kind hint: bug fix"),
    ("ac-implement", "0052CC", "Kind hint: new feature"),
    ("ac-test", "5319E7", "Kind hint: write or improve tests"),
    ("ac-rework", "FBCA04", "Kind hint: reviewer requested changes"),
)

# Labels auto-claude no longer uses. Left on issues rather than deleted - a
# human may still be filtering on them, and deleting a label is destructive and
# org-wide. The migration (task G) relabels in-flight issues; these just stop
# being written.
RETIRED_LABELS: frozenset[str] = frozenset({
    "ac-needs-info",     # -> ac-input-needed
    "ac-plan-posted",    # -> ac-reviewed
    "ac-review-posted",  # -> ac-reviewed
    "ac-plan",           # plan/review worker retired outright
    "ac-review",
})
