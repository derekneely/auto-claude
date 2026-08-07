# Merge Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advance an issue to `ac-merged` ("Pending Release") once its pull request merges, so the pipeline stops rotting one stage short of the finish line.

**Architecture:** A merge sweep in `Poller._poll_repo` checks the PR state of any issue at `ac-dev-review` or `ac-hitl` that carries a `pr_url`; a merged PR triggers a label transition to `ac-merged` plus a local status move to `COMPLETED`. A parallel short-circuit at the top of `run_review_worker` covers the `ac-review-in-progress` case, where a lease holder — not `main` — must own the label write. `ac-hitl` and `ac-done` remain human-owned; nothing here closes an issue.

**Tech Stack:** Python 3.14, `gh` CLI via the existing `GithubClient`/`_run_cmd` wrappers, pytest.

**Spec:** `docs/superpowers/specs/2026-08-07-merge-detection-design.md`. That document is the design and this plan does not repeat its rationale.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **PowerShell is primary.** Run tests as `.venv\Scripts\python -m pytest tests/ -q`. The Bash tool is Git Bash and its cwd drifts — prefer absolute paths.
- **`.venv\Scripts\python -m pytest tests/ -q` must pass after every single task.** The baseline before Task 1 is **847 passed, 3 skipped**. Each task states its own expected running total, derived from the test methods this plan writes out. If you legitimately add a case the plan did not list, the total moves — record the new number in the task rather than deleting a test to hit the stated one.
- **No test touches a real database, network or subprocess.** Every new test uses fakes, in the style already established by `tests/test_poller_stages.py` and `tests/test_review_worker.py`.
- **Every `subprocess.run`/`Popen` with `text=True` MUST also pass `encoding="utf-8", errors="replace"`.** Enforced by `tests/test_subprocess_encoding.py`, which scans source text. This plan adds no new `subprocess` call sites — everything routes through the existing `GithubClient._run_gh` and `worker._run_cmd` — so do not add one.
- **`tests/test_push_guard.py:200-210` counts `'"git", "push"'` against `'assert_pushable('` in `worker.py` source text.** This plan adds no push site. Do not let a refactor disturb that ratio.
- **Never abort a running Claude agent** for a lost lease or a database outage. Unchanged by this plan.
- **`ac-done` is never written by code, and no code path closes a GitHub issue.** This is the design's central boundary. Two tests assert it explicitly (Task 3).
- **Commits:** conventional commit with an `(ai-cc)` suffix in the subject, and **no `Co-Authored-By` trailer**. One commit per task.
- **Do not start the daemon to check something.** The one-off repair in Task 6 is a single `gh` command, not a daemon run.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `stages.py` | Canonical `ac-*` vocabulary | Add `MERGE_WATCH` |
| `state.py` | Local record status machine | Permit `QUEUED → COMPLETED` |
| `github_client.py` | `gh` CLI wrapper | Add `pr_number_from_url()` and `GithubClient.get_pr_state()` |
| `poller.py` | Discovery + label-truth reconciliation | Add the merge sweep |
| `worker.py` | Worker entry points | Add the review-worker short-circuit |
| `docs/plans/post-mvp.md` | Deferred-work list | Delete the stale Re-Triage entry |
| `docs/reviewing-prs.md` | Human PR-review guide | Document the automatic `ac-merged` step |
| `tests/test_merge_detection.py` | **New.** Poller merge sweep | Create |
| `tests/test_review_worker.py` | Review worker | Add short-circuit cases |
| `tests/test_poller_stages.py` | Poller behaviour | Add the re-triage regression |
| `tests/test_state.py` | Status machine | Add the new transition case |

`pr_number_from_url` lands in `github_client.py` rather than being duplicated: `worker._pr_number` already does exactly this, and `poller` must not import `worker` (that would pull the whole worker module, including `multiprocessing` machinery, into the poll path). `github_client` is already imported by both.

---

## Task 1: The vocabulary and the status machine

Two pure, dependency-free changes that everything downstream consumes. No I/O, no GitHub, no database.

**Files:**
- Modify: `stages.py` (after the `LOCKED` definition, ~line 58)
- Modify: `state.py:41-51` (`VALID_TRANSITIONS`)
- Test: `tests/test_stages.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `stages.MERGE_WATCH: frozenset[str]`, and a `VALID_TRANSITIONS` entry permitting `IssueStatus.QUEUED → IssueStatus.COMPLETED`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stages.py`:

```python
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
```

Append to `tests/test_state.py`:

```python
class TestQueuedCanCompleteOnAnExternalMerge:
    def test_queued_may_transition_to_completed(self):
        # A merge is a completion that arrives without the issue ever passing
        # through IN_PROGRESS — the review worker never ran because the human
        # merged first. Without this, poll step 5 would keep spawning a review
        # worker for a record whose label already says ac-merged.
        assert IssueStatus.COMPLETED in VALID_TRANSITIONS[IssueStatus.QUEUED]

    def test_queued_to_completed_is_accepted_by_the_store(self, tmp_path):
        store = StateStore(tmp_path / "issues.json")
        store.add(_record(status=IssueStatus.QUEUED))
        store.transition("repo#1", IssueStatus.COMPLETED)
        assert store.get("repo#1").status == IssueStatus.COMPLETED

    def test_queued_to_needs_info_is_still_rejected(self):
        # The widening is narrow on purpose — only COMPLETED was added.
        assert IssueStatus.NEEDS_INFO not in VALID_TRANSITIONS[IssueStatus.QUEUED]
```

Use whatever `_record(...)` / import style `tests/test_state.py` already establishes; do not introduce a second helper if one exists.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_stages.py::TestMergeWatch tests/test_state.py::TestQueuedCanCompleteOnAnExternalMerge -v`
Expected: FAIL — `AttributeError: module 'stages' has no attribute 'MERGE_WATCH'`, and the `VALID_TRANSITIONS` assertions fail.

- [ ] **Step 3: Write the implementation**

In `stages.py`, immediately after the `LOCKED` definition:

```python
# Stages where a merged PR should advance the issue to ac-merged, swept by
# `Poller._poll_repo`. Deliberately excludes every member of LOCKED: a lease
# holder owns the label write there, and `main` sweeping it would race the
# worker's own fenced write. The review worker handles its own stage itself.
#
# `ac-dev-review` is watched, not just `ac-hitl`, because a human routinely
# merges before the review worker ever picks the issue up — and a merged PR
# cannot be usefully reviewed.
MERGE_WATCH = frozenset({"ac-dev-review", "ac-hitl"})
```

In `state.py`, change the `QUEUED` row of `VALID_TRANSITIONS`:

```python
    # COMPLETED is reachable from QUEUED only via an external event: the PR
    # merged before a worker ever claimed the issue (see stages.MERGE_WATCH).
    # Without it, the merge sweep could relabel an issue to ac-merged while
    # its record stayed QUEUED, and poll step 5 would spawn a review worker
    # against a merged PR on the very same tick.
    IssueStatus.QUEUED:      [IssueStatus.IN_PROGRESS, IssueStatus.COMPLETED, IssueStatus.SKIPPED],
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_stages.py::TestMergeWatch tests/test_state.py::TestQueuedCanCompleteOnAnExternalMerge -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: **854 passed, 3 skipped**

- [ ] **Step 6: Commit**

```bash
git add stages.py state.py tests/test_stages.py tests/test_state.py
git commit -m "feat(stages): add MERGE_WATCH and let a queued issue complete on an external merge (ai-cc)"
```

---

## Task 2: Reading a PR's merge state

**Files:**
- Modify: `github_client.py` (module-level function after `GithubClientError`; method after `get_pr_review_comments`, ~line 256)
- Modify: `worker.py:1833-1838` (`_pr_number` delegates instead of duplicating)
- Test: `tests/test_github_client.py`

**Interfaces:**
- Consumes: `stages.MERGE_WATCH` (not directly — Task 1 only needs to have landed).
- Produces:
  - `github_client.pr_number_from_url(pr_url: str | None) -> int | None`
  - `GithubClient.get_pr_state(repo: str, pr_number: int) -> dict` returning
    `{"number": int, "state": str, "merged": bool, "merged_at": str | None}`
    where `state` is `"open"` or `"closed"` (GitHub's REST casing, lowercase).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_github_client.py`, following the existing fake-`_gh_api` style in that file:

```python
class TestPrNumberFromUrl:
    def test_extracts_the_number_from_a_pull_url(self):
        assert pr_number_from_url("https://github.com/Org/repo/pull/341") == 341

    def test_tolerates_a_trailing_slash(self):
        assert pr_number_from_url("https://github.com/Org/repo/pull/341/") == 341

    def test_returns_none_for_none(self):
        assert pr_number_from_url(None) is None

    def test_returns_none_for_a_non_numeric_tail(self):
        # e.g. a .../pull/341/files deep link, which is not a PR identity
        assert pr_number_from_url("https://github.com/Org/repo/pull/341/files") is None

    def test_returns_none_for_empty_string(self):
        assert pr_number_from_url("") is None


class TestGetPrState:
    def _client(self, payload):
        client = GithubClient("Org")
        client._gh_api = lambda endpoint, **kw: payload
        return client

    def test_reports_a_merged_pr(self):
        client = self._client({
            "number": 341, "state": "closed",
            "merged": True, "merged_at": "2026-08-07T12:59:28Z",
        })
        assert client.get_pr_state("repo", 341) == {
            "number": 341, "state": "closed",
            "merged": True, "merged_at": "2026-08-07T12:59:28Z",
        }

    def test_reports_an_open_pr(self):
        client = self._client({
            "number": 341, "state": "open", "merged": False, "merged_at": None,
        })
        assert client.get_pr_state("repo", 341)["merged"] is False

    def test_reports_a_closed_unmerged_pr(self):
        client = self._client({
            "number": 341, "state": "closed", "merged": False, "merged_at": None,
        })
        result = client.get_pr_state("repo", 341)
        assert result["merged"] is False
        assert result["state"] == "closed"

    def test_a_missing_merged_key_is_treated_as_not_merged(self):
        # Never infer a merge from an absent field. Guessing here would
        # advance an issue to ac-merged on incomplete information.
        client = self._client({"number": 341, "state": "closed"})
        assert client.get_pr_state("repo", 341)["merged"] is False

    def test_calls_the_pulls_endpoint_not_the_issues_endpoint(self):
        seen = []
        client = GithubClient("Org")
        client._gh_api = lambda endpoint, **kw: seen.append(endpoint) or {
            "number": 1, "state": "open", "merged": False,
        }
        client.get_pr_state("repo", 341)
        # /issues/N returns a PR too, but without the `merged` boolean.
        assert seen == ["/repos/Org/repo/pulls/341"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_github_client.py::TestPrNumberFromUrl tests/test_github_client.py::TestGetPrState -v`
Expected: FAIL — `ImportError: cannot import name 'pr_number_from_url'`

- [ ] **Step 3: Write the implementation**

In `github_client.py`, at module level after the `GithubClientError` class:

```python
def pr_number_from_url(pr_url: str | None) -> int | None:
    """Extract the PR number from a github.com/.../pull/N URL.

    Lives here rather than in `worker` so `poller` can use it without
    importing the worker module — that import would pull the whole
    multiprocessing worker stack into the poll path.
    """
    if not pr_url:
        return None
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None
```

As a `GithubClient` method, after `get_pr_review_comments`:

```python
    def get_pr_state(self, repo: str, pr_number: int) -> dict:
        """Return a PR's merge state.

        Uses `/pulls/{n}` rather than `/issues/{n}`: the issues endpoint
        serves pull requests too, but omits the `merged` boolean, and this
        caller must never infer a merge from `state == "closed"` — a PR
        closed without merging looks identical there.
        """
        payload = self._gh_api(f"/repos/{self.org}/{repo}/pulls/{pr_number}")
        return {
            "number": payload.get("number", pr_number),
            "state": payload.get("state", ""),
            "merged": bool(payload.get("merged", False)),
            "merged_at": payload.get("merged_at"),
        }
```

In `worker.py`, replace the body of `_pr_number` (keeping the name — it has call sites at lines 1857, 1872 and elsewhere):

```python
def _pr_number(pr_url: str | None) -> int | None:
    """Extract the PR number from a github.com/.../pull/N URL.

    Delegates to `github_client.pr_number_from_url` so the poller and the
    worker cannot drift on what counts as a PR URL.
    """
    return pr_number_from_url(pr_url)
```

Add `pr_number_from_url` to `worker.py`'s existing `from github_client import ...` line. If `worker.py` imports the module rather than names, call it as `github_client.pr_number_from_url` and leave the import untouched — match whatever is already there.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_github_client.py -v`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: **864 passed, 3 skipped**

- [ ] **Step 6: Commit**

```bash
git add github_client.py worker.py tests/test_github_client.py
git commit -m "feat(github): read a PR's merge state, and share PR-number parsing with the poller (ai-cc)"
```

---

## Task 3: The merge sweep

The core of the feature.

**Files:**
- Modify: `poller.py` (imports; `__init__` ~line 26; `_poll_repo` insertion at line 82; new `_check_merged` method)
- Test: `tests/test_merge_detection.py` (create)

**Interfaces:**
- Consumes: `stages.MERGE_WATCH`, `github_client.pr_number_from_url`, `GithubClient.get_pr_state` (Tasks 1–2).
- Produces: `Poller._check_merged(repo: str, issue: dict, label_names: list[str], issue_id: str) -> bool` — `True` means the issue was advanced to `ac-merged` and the caller must `continue` past all further processing for it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_merge_detection.py`. Mirror the fake-client and `make_poller`/`make_issue`/`seed_record` helper style already in `tests/test_poller_stages.py` — import them from there if they are importable, otherwise copy the minimal shapes rather than inventing a different convention.

```python
"""The merge sweep: a merged PR advances its issue to ac-merged.

Nothing in auto-claude wrote ac-merged before this. field_admin#268 is the
case that motivated it — PR merged, issue left labelled for a review that
could no longer accomplish anything, board card stuck in "In Review".

The two negative tests at the bottom are the point of the whole design:
ac-done and closing the issue are human prerogatives, and a sweep that runs
unattended every 60s must never quietly absorb them.
"""

from __future__ import annotations

import pytest

import stages
from github_client import GithubClientError
from state import IssueStatus


MERGED = {"number": 341, "state": "closed", "merged": True,
          "merged_at": "2026-08-07T12:59:28Z"}
OPEN = {"number": 341, "state": "open", "merged": False, "merged_at": None}
CLOSED = {"number": 341, "state": "closed", "merged": False, "merged_at": None}


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


class TestFailureIsNeverFatal:
    def test_a_gh_error_does_not_raise(self, poller_env):
        env = poller_env(stage="ac-hitl", pr_state=GithubClientError("boom"))
        env.poll()  # must not raise
        assert "ac-merged" not in env.labels_now()

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
```

Write a `poller_env` fixture in this file that builds a `Poller` over a fake `GithubClient` and a real `StateStore` on `tmp_path`. The fake must:

- serve one issue from `list_issues` carrying `stage` + `extra_labels`
- record `add_label`/`remove_label` calls into a mutable label set, raising `GithubClientError` when `label_write_fails`
- serve `get_pr_state` from `pr_state`, appending to `pr_lookups`; if `pr_state` is a `GithubClientError` instance, raise it
- expose `close_calls` and `comment_calls` as always-empty lists that the fake would append to if `Poller` ever called a close/comment method — assert-by-absence only works if the fake *would* record such a call, so give it `close_issue` and `post_comment` methods that append

`env.labels_now()` returns the fake's current label set; `env.record()` returns the `StateStore` record; `env.warnings` collects `logger.warn` strings.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_merge_detection.py -v`
Expected: FAIL — every advancing case fails because `ac-merged` is never written.

- [ ] **Step 3: Write the implementation**

In `poller.py`, extend the imports:

```python
from github_client import GithubClient, GithubClientError, pr_number_from_url
```

In `Poller.__init__`, after `self._logger = logger`:

```python
        # Issues whose PR was closed without merging, already warned about.
        # Process-lifetime only: a restart re-warning once is correct, a 60s
        # poll loop re-warning 1,440 times a day is not.
        self._warned_closed: set[str] = set()
```

Insert into `_poll_repo`, between the label-refresh block that ends at line 81 and the `stages.is_terminal` guard at line 87:

```python
            # A merged PR advances the issue to ac-merged ("Pending
            # Release"). This MUST run before the is_terminal guard below —
            # ac-hitl is itself terminal, so a check placed after it would
            # never see the stage where most merges happen.
            if self._check_merged(repo, issue, label_names, issue_id):
                continue
```

Add the method to `Poller`:

```python
    def _check_merged(
        self,
        repo: str,
        issue: dict,
        label_names: list[str],
        issue_id: str,
    ) -> bool:
        """Advance `issue_id` to ac-merged if its PR has merged.

        Returns True when the issue was advanced, meaning the caller should
        skip it for the rest of this poll.

        Never raises: a GitHub failure here must not stall the poll loop, and
        must never advance an issue on incomplete information. Both failure
        paths return False, which simply means "try again next tick".
        """
        if stages.stage_of(label_names) not in stages.MERGE_WATCH:
            return False
        if not self._state.is_known(issue_id):
            return False

        record = self._state.get(issue_id)
        pr_number = pr_number_from_url(record.pr_url)
        if pr_number is None:
            return False

        try:
            pr = self._github.get_pr_state(repo, pr_number)
        except GithubClientError as exc:
            self._logger.warn(f"Could not check PR state for {issue_id}: {exc}")
            return False

        if not pr["merged"]:
            if pr["state"] == "closed" and issue_id not in self._warned_closed:
                self._warned_closed.add(issue_id)
                self._logger.warn(
                    f"{issue_id}: PR #{pr_number} was closed without merging — "
                    f"leaving it at {stages.stage_of(label_names)} for a human"
                )
            return False

        # Labels first. A record moved to COMPLETED whose label still reads
        # ac-hitl would never be swept again, so the durable half must land
        # before the local half.
        add, remove = stages.transition(label_names, "ac-merged")
        try:
            for label in add:
                self._github.add_label(repo, issue["number"], label)
            for label in remove:
                self._github.remove_label(repo, issue["number"], label)
        except GithubClientError as exc:
            self._logger.warn(f"Could not advance {issue_id} to ac-merged: {exc}")
            return False

        if record.status != IssueStatus.COMPLETED:
            try:
                self._state.transition(issue_id, IssueStatus.COMPLETED)
            except InvalidTransitionError:
                # The label is the source of truth and it is already correct;
                # startup reconciliation derives COMPLETED from ac-merged
                # (reconcile.py) and will settle any residual mismatch.
                self._logger.warn(
                    f"{issue_id}: merged, but local status {record.status} "
                    f"could not move to completed"
                )
        self._state.update(
            issue_id,
            labels=[lbl for lbl in label_names if lbl not in remove] + add,
            worker_pid=None,
        )
        self._state.save()
        self._logger.info(
            f"{issue_id} — PR #{pr_number} merged, -> ac-merged (Pending Release)"
        )
        return True
```

Add `InvalidTransitionError` to the `from state import ...` line in `poller.py`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_merge_detection.py -v`
Expected: PASS (19 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: **883 passed, 3 skipped**

- [ ] **Step 6: Commit**

```bash
git add poller.py tests/test_merge_detection.py
git commit -m "feat(poller): advance an issue to ac-merged once its PR lands (ai-cc)"
```

---

## Task 4: Review-worker short-circuit

Covers the stage the sweep deliberately skips. Without it, the first daemon start after Task 3 still burns an Opus run reviewing merged PR #341 — and worse, `_find_pr_for_issue` lists only **open** PRs, so a merged PR with no `ctx.pr_url` raises `"No open PR found"`, which `_labels_for_review_crash` rewinds to `ac-dev-review`, which re-queues it next tick. That is an unbounded crash loop, not a one-off waste.

**Files:**
- Modify: `worker.py` — new `_labels_for_review_merged`, `_review_merged_labels`, `_merged_pr_for_issue`, `_check_pr_already_merged`; call site in `run_review_worker` after `_claim_review_labels` (~line 2966)
- Test: `tests/test_review_worker.py`

**Interfaces:**
- Consumes: `stages.transition`, `worker._run_cmd`, `worker._candidate_prs_for_issue`, `worker._set_labels`, `worker._pr_number`.
- Produces:
  - `_labels_for_review_merged(labels: list[str]) -> tuple[list[str], list[str]]`
  - `_check_pr_already_merged(ctx: IssueContext, logger: WorkerLogger) -> dict | None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_worker.py`, matching its existing fake-`_run_cmd` conventions:

```python
class TestLabelsForReviewMerged:
    def test_it_targets_ac_merged(self):
        add, remove = worker._labels_for_review_merged(
            ["ac-review-in-progress", "ac-pr-created"]
        )
        assert add == ["ac-merged"]
        assert "ac-review-in-progress" in remove

    def test_control_labels_are_preserved(self):
        add, remove = worker._labels_for_review_merged(
            ["ac-review-in-progress", "ac-pr-created", "ac-attempt-2"]
        )
        assert "ac-pr-created" not in remove
        assert "ac-attempt-2" not in remove

    def test_it_never_targets_ac_done(self):
        add, _remove = worker._labels_for_review_merged(["ac-review-in-progress"])
        assert "ac-done" not in add


class TestCheckPrAlreadyMerged:
    def test_a_merged_pr_on_ctx_is_detected(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        assert worker._check_pr_already_merged(env.ctx, env.logger) is not None

    def test_an_open_pr_on_ctx_returns_none(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "OPEN"})
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None

    def test_a_closed_unmerged_pr_returns_none(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "CLOSED"})
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None

    def test_with_no_pr_url_it_searches_merged_prs(self, review_env):
        # _find_pr_for_issue lists only OPEN PRs, so a restart that lost
        # ctx.pr_url could not see a merged PR at all — the exact path that
        # produces the "No open PR found" crash loop.
        env = review_env(pr_url=None, merged_list=[
            {"number": 341, "headRefName": "ac/issue-268-attachments",
             "url": "https://github.com/Org/repo/pull/341", "body": "", "title": ""},
        ], number=268)
        assert worker._check_pr_already_merged(env.ctx, env.logger) is not None

    def test_a_gh_failure_returns_none_rather_than_raising(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         gh_fails=True)
        assert worker._check_pr_already_merged(env.ctx, env.logger) is None


class TestReviewWorkerShortCircuit:
    def test_a_merged_pr_skips_claude_entirely(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.claude_invocations == 0

    def test_a_merged_pr_sets_ac_merged(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert "ac-merged" in env.labels_added

    def test_a_merged_pr_never_creates_a_worktree(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.worktrees_created == []

    def test_a_merged_pr_reports_completed_not_failed(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "MERGED"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.final_status() == "completed"

    def test_an_open_pr_still_reviews_as_before(self, review_env):
        env = review_env(pr_url="https://github.com/Org/repo/pull/341",
                         pr_view={"number": 341, "state": "OPEN"})
        worker.run_review_worker(env.ctx, env.log_q, env.state_q, env.abort)
        assert env.claude_invocations == 1
```

Build the `review_env` fixture on whatever monkeypatching `tests/test_review_worker.py` already uses for `_run_cmd`, `_run_claude`, `_clone_or_fetch`, `_setup_review_worktree` and `_set_labels`. Do not introduce a second fake style in the same file.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_review_worker.py -v -k "Merged or ShortCircuit"`
Expected: FAIL — `AttributeError: module 'worker' has no attribute '_labels_for_review_merged'`

- [ ] **Step 3: Write the implementation**

In `worker.py`, alongside the other `_labels_for_review_*` functions (after `_labels_for_review_crash`, ~line 1831):

```python
def _labels_for_review_merged(labels: list[str]) -> tuple[list[str], list[str]]:
    """Add/remove when the PR under review has already merged.

    Not a review verdict — there is nothing left to review — so this consumes
    no attempt and bumps no counter. It is the same terminal the poller's
    merge sweep writes; the review worker owns the write here because it holds
    the lease on ac-review-in-progress.
    """
    return stages.transition(labels, "ac-merged")
```

Next to `_find_pr_for_issue` (~line 1402):

```python
def _merged_pr_for_issue(ctx: IssueContext, logger: WorkerLogger) -> dict | None:
    """Look up a *merged* PR for this issue.

    `_find_pr_for_issue` lists only open PRs by design — it answers "what can
    I review". This answers "was it already merged", which that query cannot
    see, and whose absence produced an unbounded crash loop: no open PR ->
    RuntimeError -> _labels_for_review_crash rewinds to ac-dev-review -> the
    next poll re-queues the same review.
    """
    result = _run_cmd(
        [
            "gh", "pr", "list",
            "--repo", f"{ctx.org}/{ctx.repo}",
            "--state", "merged",
            "--limit", "50",
            "--json", "number,headRefName,url,body,title,updatedAt",
        ],
        logger=logger,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warn(f"Failed to list merged PRs: {result.stderr.strip()}")
        return None
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warn("Failed to parse `gh pr list --state merged` output")
        return None

    branch_prefix = f"ac/issue-{ctx.number}-"
    candidates = _candidate_prs_for_issue(prs, ctx.number, branch_prefix)
    return candidates[0] if candidates else None


def _check_pr_already_merged(ctx: IssueContext, logger: WorkerLogger) -> dict | None:
    """Return the merged PR for this issue, or None if there is nothing merged.

    Never raises. An unreadable `gh` result returns None, which means the
    review proceeds as normal — the worse failure is skipping a review that
    should have happened, so this errs toward doing the work.
    """
    pr_number = _pr_number(ctx.pr_url)
    if pr_number is not None:
        result = _run_cmd(
            [
                "gh", "pr", "view", str(pr_number),
                "--repo", f"{ctx.org}/{ctx.repo}",
                "--json", "number,state,url,headRefName",
            ],
            logger=logger,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warn(f"Could not read PR #{pr_number} state: {result.stderr.strip()}")
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.warn(f"Could not parse PR #{pr_number} state")
            return None
        # `gh pr view` reports state in caps: OPEN / CLOSED / MERGED.
        return payload if payload.get("state") == "MERGED" else None

    return _merged_pr_for_issue(ctx, logger)


def _review_merged_labels(ctx: IssueContext, logger: WorkerLogger) -> None:
    """Transition the issue to ac-merged when its PR turned out to be merged."""
    labels = _get_issue_labels(ctx, logger)
    add, remove = _labels_for_review_merged(labels)
    _set_labels(ctx, logger, add=add, remove=remove)
    _telemetry(ctx, logger, "merged", labels, stage="review",
               pr=_pr_number(ctx.pr_url))
```

In `run_review_worker`, immediately after the `_claim_review_labels(ctx, logger)` call at line 2966:

```python
        # [1.5] Nothing to review if the PR already merged — a human merged
        # before this review was picked up. Runs after the self-lock so the
        # label write is fenced by the lease this worker holds, and before
        # the PR *resolution* below, which lists only open PRs and would
        # crash-loop on a merged one.
        merged = _check_pr_already_merged(ctx, logger)
        if merged is not None:
            ctx.pr_url = merged.get("url") or ctx.pr_url
            logger.info(
                f"PR #{merged.get('number')} already merged — skipping review, "
                f"-> ac-merged (Pending Release)"
            )
            _review_merged_labels(ctx, logger)
            state_queue.put(StateUpdate(
                issue_id=ctx.issue_id,
                status="completed",
                run_id=run_id,
                pr_url=ctx.pr_url,
            ))
            return
```

Check `StateUpdate`'s field names in `worker.py:287` before writing that call and use the real ones — pass only fields it actually declares.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_review_worker.py -v`
Expected: PASS, including the file's pre-existing tests.

- [ ] **Step 5: Confirm the push-guard meta-test still holds**

Run: `.venv\Scripts\python -m pytest tests/test_push_guard.py tests/test_subprocess_encoding.py -v`
Expected: PASS. This task added `gh` calls through `_run_cmd` and no `git push` site; both meta-tests must be unaffected.

- [ ] **Step 6: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: **896 passed, 3 skipped**

- [ ] **Step 7: Commit**

```bash
git add worker.py tests/test_review_worker.py
git commit -m "fix(review): skip the review and mark ac-merged when the PR already landed (ai-cc)"
```

---

## Task 5: Re-triage verification and documentation

Spec §4 turned out to be already implemented — including the guard against the phantom re-trigger. `main.py:689-697` re-fetches `updated_at` right after posting the needs-info comment, precisely so the poller does not read auto-claude's own write as a human response. This task pins that behaviour with a regression test and corrects the stale documentation.

**Files:**
- Modify: `docs/plans/post-mvp.md` (delete the "Re-Triage Cycle" entry)
- Modify: `docs/reviewing-prs.md` (document the automatic `ac-merged` step)
- Test: `tests/test_poller_stages.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_poller_stages.py`:

```python
class TestRetriageDoesNotFireOnOurOwnWrite:
    """Re-triage triggers on any updated_at change, and auto-claude's own
    label write bumps updated_at. `main._run_triage` re-fetches updated_at
    after posting the needs-info comment to close that gap. Without it, every
    60s poll would re-triage an issue no human has answered — a triage call
    per tick until someone responds.
    """

    def test_a_needs_info_issue_whose_timestamp_matches_is_not_retriaged(self):
        poller = make_poller(issues=[
            make_issue(number=1, labels=["ac-input-needed"], updated_at="T1"),
        ])
        seed_record(poller, "repo#1", status=IssueStatus.NEEDS_INFO,
                    issue_updated_at="T1")
        _new, retriage = poller.poll()
        assert retriage == []

    def test_a_genuine_human_response_is_retriaged(self):
        poller = make_poller(issues=[
            make_issue(number=1, labels=["ac-input-needed"], updated_at="T2"),
        ])
        seed_record(poller, "repo#1", status=IssueStatus.NEEDS_INFO,
                    issue_updated_at="T1")
        _new, retriage = poller.poll()
        assert [r.issue_id for r in retriage] == ["repo#1"]

    def test_run_triage_refetches_updated_at_after_posting(self):
        # Source-text assertion: the re-fetch is the only thing standing
        # between needs_info and a per-tick triage loop, and it is easy to
        # drop during an unrelated refactor of the needs-info branch.
        import inspect

        import main as main_module

        body = inspect.getsource(main_module._run_triage)
        assert "issue_updated_at=fresh.get" in body, (
            "main._run_triage must re-read updated_at after applying "
            "ac-input-needed, or the poller re-triages its own label write"
        )
```

Match `make_poller` / `make_issue` / `seed_record` to the real helper signatures at `tests/test_poller_stages.py:40-79`; adapt the calls above if those helpers take different keywords.

- [ ] **Step 2: Run the test to verify it passes or fails**

Run: `.venv\Scripts\python -m pytest tests/test_poller_stages.py::TestRetriageDoesNotFireOnOurOwnWrite -v`
Expected: PASS on all three. This is a characterization test of existing behaviour, so passing immediately is the correct outcome, not a mistake.

**If any of the three fails**, the phantom re-trigger is real. Stop and fix it in `main._run_triage` before continuing — the fix is to ensure `issue_updated_at` is refreshed from a post-write `github.get_issue` call on every path that applies `ac-input-needed`, then re-run.

- [ ] **Step 3: Delete the stale post-MVP entry**

In `docs/plans/post-mvp.md`, delete the entire `### Re-Triage Cycle` section under "Deferred from MVP". It is implemented at `poller.py:150-161` and `main.py:988-993`, and listing shipped work as deferred sends the next planner to rebuild it.

- [ ] **Step 4: Document the new automatic step**

In `docs/reviewing-prs.md`, update the stage diagram and the surrounding prose so they say what the code now does. The file currently tells the reader that "`ac-merged` and `ac-done` are yours"; from now on `ac-merged` is automatic and only `ac-done` is manual. Specifically:

- At the diagram (~line 25), mark `ac-merged` as set automatically once the PR merges.
- At ~line 63 and ~line 164, replace the instruction to hand-set `ac-merged` with: merge the PR, and auto-claude moves the issue to `ac-merged` / "Pending Release" within one poll interval. Setting `ac-done` at release time stays manual.
- Note the closed-without-merging behaviour: auto-claude warns once and leaves the issue where it is, for a human to decide.

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest tests/ -q`
Expected: **899 passed, 3 skipped**

- [ ] **Step 6: Commit**

```bash
git add docs/plans/post-mvp.md docs/reviewing-prs.md tests/test_poller_stages.py
git commit -m "docs: ac-merged is automatic now; pin the re-triage self-write guard (ai-cc)"
```

---

## Task 6: Repair field_admin#268

A one-off data fix, not code. `#268`'s PR merged on 2026-08-07 while the daemon was stopped, so no sweep will ever observe the transition — `get_pr_state` would report it correctly, but only if the issue is still in a watched stage, which it is. Running the daemon would in fact repair it automatically. This task does it by hand so the backlog is clean and the first daemon run has nothing anomalous in it.

**Files:** none.

- [ ] **Step 1: Confirm the current state**

```bash
gh issue view 268 --repo Accelevation/field_admin --json number,state,labels --jq '{n:.number,s:.state,l:[.labels[].name]}'
gh pr view 341 --repo Accelevation/field_admin --json number,state,mergedAt
```

Expected: issue 268 OPEN with `ac-pr-created`, `ac-dev-review`, `ac-attempt-1`; PR 341 MERGED.

- [ ] **Step 2: Apply the transition**

```bash
gh issue edit 268 --repo Accelevation/field_admin \
  --add-label ac-merged --remove-label ac-dev-review
```

`ac-pr-created` and `ac-attempt-1` stay — they are control labels, not stages. The issue stays **open**: `ac-merged` means pending release, and closing it is the human `ac-done` step.

- [ ] **Step 3: Verify**

```bash
gh issue view 268 --repo Accelevation/field_admin --json state,labels --jq '{s:.state,l:[.labels[].name]}'
```

Expected: `state: OPEN`, labels contain `ac-merged`, `ac-pr-created`, `ac-attempt-1`, and **not** `ac-dev-review`.

Confirm the board card moved to **Pending Release** — that is `project-sync.mjs`'s mapping, and seeing it move is the end-to-end proof that setting the label alone is sufficient.

- [ ] **Step 4: No commit**

Nothing to commit. This task changes GitHub state only.

---

## Verification

After Task 6, the whole feature is verifiable without starting the daemon:

- [ ] `.venv\Scripts\python -m pytest tests/ -q` → **899 passed, 3 skipped**
- [ ] `git log --oneline -5` shows five task commits, each with the `(ai-cc)` suffix and no `Co-Authored-By` trailer
- [ ] `grep -rn "ac-done" poller.py worker.py` returns no line that *writes* the label — only the `stages.py` vocabulary and `TERMINAL` membership reference it
- [ ] field_admin#268 is OPEN, at `ac-merged`, and its board card reads **Pending Release**

The first real daemon run is the end-to-end test, and it is deliberately after this plan rather than during it.
