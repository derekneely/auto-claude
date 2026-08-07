# Merge Detection — closing the pipeline loop at `ac-merged`

**Date:** 2026-08-07
**Status:** approved for planning

## Problem

`stages.py` defines a ten-stage vocabulary ending
`ac-hitl → ac-merged → ac-done`, but **nothing in auto-claude ever writes
`ac-merged`**. The stage exists in the label set, in `STAGE_LABELS`, in
`TERMINAL`, and in `reconcile.py`'s status map — and no code path reaches it.

The consequence is visible right now. `field_admin#268` carries
`ac-pr-created` + `ac-dev-review` + `ac-attempt-1`. Its PR (#341) merged to
`dev` at 2026-08-07T12:59:28Z. The issue is still open, still labelled for a
review that will never happen, and its board card is still sitting in
**In Review**. Every issue the pipeline has ever completed ends this way: the
harness does the work, a human merges, and the record silently rots one stage
short of the finish line.

Worse, starting the daemon in this state is actively expensive. `#268` sits at
`ac-dev-review`, which is `REVIEW_TRIGGER` — the next poll would spawn an Opus
review worker, check out a branch whose PR merged hours ago, and burn a full
run producing a review nobody can act on.

## The flow this encodes

The stage vocabulary already matches how the team actually works. This design
does not change the flow; it fills in the one automatic transition that was
missing from it.

```
ac-dev-ready → ac-in-progress → ac-dev-review → ac-review-in-progress → ac-hitl
   (human       (dev agent)       (queued for      (review agent)        (human +
    gate 1)                        review)                               local Claude:
                                                                         stand up, test,
                                                                         tweak, merge)
                                                                              │
                                                                        PR merges to dev
                                                                              ▼
                                                                        ac-merged      ← THIS DESIGN
                                                                     "Pending Release"
                                                                              │
                                                                     (human gate 3:
                                                                      release to prod)
                                                                              ▼
                                                                          ac-done
```

`ac-hitl` is where a human and a local Claude session stand the PR up, test it,
tweak it, and merge. That stage stays entirely human-owned. `ac-done` stays
entirely human-owned — promoting a merged change to *released* is a judgement
the harness has no basis to make.

`ac-merged` is the only new automatic write, and it means exactly one thing:
**the PR is merged; this is pending release.**

## Design

### 1. The merge sweep

**Location:** `Poller._poll_repo`, immediately *before* the
`stages.is_terminal(label_names)` early-`continue` at `poller.py:87`.

Placement is load-bearing. `ac-hitl` is a member of `stages.TERMINAL`, so the
sweep must run before that guard — a check placed after it would never see the
issues that matter most.

**Watch set:**

```python
# stages.py
MERGE_WATCH = frozenset({"ac-dev-review", "ac-hitl"})
```

An issue qualifies when `stage_of(labels) in MERGE_WATCH` **and** its local
record carries a `pr_url`.

- `ac-hitl` is the expected case: agent review passed, human merged.
- `ac-dev-review` is the `#268` case: the human merged before the review worker
  ever picked it up. Auto-advancing is correct — a merged PR cannot be
  usefully reviewed.
- `ac-review-in-progress` is **deliberately excluded.** A review worker holds
  the lease on that issue, and a label write from `main` would race the
  worker's own fenced write at the end of its run. That case is handled inside
  the worker instead — see §2.
- `ac-in-progress` is excluded for the same reason, and because a merged PR
  during the dev run is not a state the pipeline produces.

**On merged:**

1. `add, remove = stages.transition(labels, "ac-merged")`, applied via the
   existing GitHub label client. `CONTROL_LABELS` (`ac-pr-created`,
   `ac-attempt-N`, `ac-reviewed`) survive untouched — `transition` only
   clears other *stage* labels.
2. Refresh the local record's `labels`, which carries `stage = "ac-merged"`
   into Postgres through the `StateStore.on_change` → `DbSync` seam already
   installed in `main`. No new database code.
3. Log at info: `field_admin#268 — PR #341 merged, → ac-merged (Pending Release)`.

**The local status must move too, not just the label.** This is subtle and it
is the one place the design touches the local state machine. Poll step 5
(`main.py:995-1001`) spawns a worker for every record at `QUEUED`. `#268`'s
record is `QUEUED` with `mode="review"` — relabelling it to `ac-merged` while
leaving the status at `QUEUED` would still spawn the review worker on the very
same tick. The label sweep and the local status have to move together.

`VALID_TRANSITIONS` does not currently allow `QUEUED → COMPLETED`; `QUEUED`
may only reach `IN_PROGRESS` or `SKIPPED`. That constraint predates any notion
of an *external* event finishing an issue. This design adds `COMPLETED` to
`QUEUED`'s allowed targets, justified narrowly: a merge is a completion that
can arrive without the issue ever passing through `IN_PROGRESS`. `COMPLETED`
is also what `reconcile.py:35` already derives from `ac-merged`, so choosing
it keeps a restart idempotent — reconciliation lands on the same status the
sweep just set. Routing through `SKIPPED` instead would be a lie the next
restart silently corrects.

**What it explicitly must not do:**

- Not set `ac-done`.
- Not close the GitHub issue.
- Not touch the PR (no comment, no review, no branch deletion).
- Not prune worktrees — post-run cleanup already removed them.

**On closed-without-merge:** log a warning **once per daemon run**, keyed on
`issue_id` in an in-memory set, and leave the issue exactly where it is.
Rewinding an abandoned PR to `ac-dev-ready` would silently restart work a human
chose to stop. The warning surfaces it; the human decides.

**Cost:** one `gh pr view <url> --json state,mergedAt` per watched issue per
poll tick. In practice 0–3 calls per 60s tick, against a 5,000/hour budget.

**Failure handling:** each per-issue lookup is wrapped in
`try/except GithubClientError` → log and continue to the next issue. This
mirrors how `_poll_repo` already treats a repo-level failure at
`poller.py:43-44`. A GitHub hiccup must never stall the poll loop or mark an
issue merged on incomplete information.

### 2. Review-worker short-circuit

`run_review_worker` checks the PR's state as its first act, before checking
out anything or invoking Claude.

- **Merged** → skip the review entirely, transition to `ac-merged`, exit clean.
- **Closed unmerged** → skip the review, log, leave the stage alone (consistent
  with §1).
- **Open** → proceed exactly as today.

This is where the `ac-review-in-progress` case is handled: the worker owns the
lease, so the label write is correctly fenced by the machinery already in
place. It also closes the race where a human merges between the poller's
observation and the worker's spawn.

Without this, the first daemon start after this change spends a full Opus run
reviewing merged PR #341.

### 3. Board sync — no new code

`integrations.sync_board` shells out to `project-sync.mjs` in
`accelevation-claude-tools`, which owns the label→column mapping. field_admin's
board (`.claude/pipeline.json`) already defines the needed columns:

| Stage label | Board column |
|---|---|
| `ac-in-progress` | In Progress |
| `ac-dev-review` / `ac-review-in-progress` | In Review |
| `ac-hitl` | Dev Review |
| `ac-merged` | **Pending Release** |
| `ac-done` | Done |

Setting the label is sufficient. Poll step 6 (`_safe_sync_boards`) moves the
card on the next tick.

### 4. Re-triage — verification only, no new feature

`docs/plans/post-mvp.md` lists "Re-Triage Cycle" as deferred. **It is already
built:**

- `poller.py:150-161` — an issue at `NEEDS_INFO` whose GitHub `updated_at`
  differs from the stored `issue_updated_at` is pushed onto `retriage_issues`.
- `main.py:988-993` — poll step 4 runs `_run_triage` over that list.

One risk to verify rather than assume: the trigger is *any* `updated_at`
change, and auto-claude's own label write bumps `updated_at`. If
`issue_updated_at` is not refreshed at the moment the needs-info label is
applied, the very next poll sees a phantom change and re-triages an issue no
human has answered — burning a triage call per tick until someone responds.

Scope: add a regression test for that specific case, fix it if it reproduces,
and delete the stale `post-mvp.md` entry.

### 5. One-off repair

Hand-set `ac-merged` on `field_admin#268` (removing `ac-dev-review`) so the
backlog is clean before the daemon next runs. This is a manual `gh` command in
the plan, not code.

## Testing

`tests/test_merge_detection.py`, using the existing fake-GitHub-client style.
No network, no database, no subprocess — consistent with the suite-wide rule.

| Case | Expectation |
|---|---|
| `ac-hitl` + merged PR | → `ac-merged` |
| `ac-dev-review` + merged PR | → `ac-merged` |
| `ac-in-progress` + merged PR | untouched |
| `ac-review-in-progress` + merged PR | untouched (worker's job) |
| open PR | untouched |
| closed-unmerged PR | untouched, warned exactly once across two polls |
| watched stage, no `pr_url` | skipped, no `gh` call |
| `gh` raises `GithubClientError` | poll completes, other issues still processed |
| merged PR | `ac-done` never written |
| merged PR | issue never closed |
| merged | `ac-pr-created` / `ac-attempt-N` survive |
| `QUEUED` + `mode="review"` + merged PR | record reaches `COMPLETED`, so poll step 5 does not spawn |
| `QUEUED → COMPLETED` | permitted by `VALID_TRANSITIONS` |

Plus, in `tests/test_review_worker.py`: merged PR → no Claude invocation, stage
`ac-merged`; open PR → unchanged behaviour.

Plus, in the poller tests: the re-triage phantom-trigger regression from §4.

The last two rows of the table are negative assertions that earn their keep —
`ac-done` and issue-closing are precisely the human prerogatives this design
must not quietly absorb.

## Out of scope

- Auto-advancing `ac-merged → ac-done`. Human gate, by design.
- Reopening or rewinding abandoned PRs.
- Detecting merges in repos other than those in `config.github.repos`
  (currently `field_admin` only).
- Backfilling `ac-merged` onto historically completed issues. Only `#268` is
  affected and it is repaired by hand in §5.
