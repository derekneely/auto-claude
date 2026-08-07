"""Poller — discovers new and updated issues across monitored repos."""

from __future__ import annotations

import stages
from config import Config
from github_client import GithubClient, GithubClientError, pr_number_from_url
from logger import MainLogger
from state import InvalidTransitionError, IssueRecord, IssueStatus, StateStore

# Statuses `StateStore.transition()` can reach QUEUED from in a single hop.
# DISCOVERED is one hop further (via TRIAGING) — see the ac-dev-review branch
# of `_poll_repo` below.
_DIRECT_TO_QUEUED = frozenset({
    IssueStatus.TRIAGING,
    IssueStatus.NEEDS_INFO,
    IssueStatus.COMPLETED,
    IssueStatus.FAILED,
    IssueStatus.INTERRUPTED,
})


class Poller:
    """Queries GitHub for issues with ac-* labels and diffs against local state."""

    def __init__(self, config: Config, github: GithubClient, state: StateStore,
                 logger: MainLogger) -> None:
        self._config = config
        self._github = github
        self._state = state
        self._logger = logger

        # Issues whose PR was closed without merging, already warned about.
        # Process-lifetime only: a restart re-warning once is correct, a 60s
        # poll loop re-warning 1,440 times a day is not.
        self._warned_closed: set[str] = set()

    def poll(self) -> tuple[list[IssueRecord], list[IssueRecord]]:
        """Poll all repos and return (new_issues, retriage_issues)."""
        new_issues: list[IssueRecord] = []
        retriage_issues: list[IssueRecord] = []

        for repo in self._config.github.repos:
            try:
                new, retriage = self._poll_repo(repo)
                new_issues.extend(new)
                retriage_issues.extend(retriage)
            except GithubClientError as exc:
                self._logger.error(f"Failed to poll {repo}: {exc}")

        return new_issues, retriage_issues

    def _poll_repo(self, repo: str) -> tuple[list[IssueRecord], list[IssueRecord]]:
        """Query one repo, filter issues, detect new and retriage candidates."""
        new_issues: list[IssueRecord] = []
        retriage_issues: list[IssueRecord] = []

        # Scope to the bot's own issues. The human /loop runners already scope
        # themselves with `--assignee @me`; without the mirror image here,
        # auto-claude would pick up a labelled issue owned by a human and both
        # would work it at once.
        issues = self._github.list_issues(
            repo, assignee=self._config.github.bot_login
        )

        for issue in issues:
            label_names = [lbl["name"] for lbl in issue.get("labels", [])]
            issue_id = f"{repo}#{issue['number']}"

            # Record the live labels on any issue we already track, before the
            # terminal check below can `continue` past it. `record.labels` is
            # what becomes `issue_state.stage` (main._make_on_change), and
            # every real stage transition happens in the worker, out of
            # process, where StateUpdate has no way to carry labels back. This
            # poll is the only place the truth is visible — so an issue that
            # walked on to ac-hitl or ac-blocked while we weren't acting on it
            # would otherwise leave the shared database frozen at whatever
            # stage it held when we last touched it, forever.
            #
            # Guarded on an actual change: without it, every poll re-upserts
            # every issue every cycle.
            if self._state.is_known(issue_id):
                record = self._state.get(issue_id)
                if record.labels != label_names:
                    self._state.update(issue_id, labels=label_names)
                    self._state.save()

            # A merged PR advances the issue to ac-merged ("Pending
            # Release"). This MUST run before the is_terminal guard below —
            # ac-hitl is itself terminal, so a check placed after it would
            # never see the stage where most merges happen.
            #
            # It also claims (without writing anything) an issue whose PR was
            # confirmed closed without merging, so a closed PR at
            # ac-dev-review cannot be re-queued for review on every tick.
            if self._check_merged(repo, issue, label_names, issue_id):
                continue

            # Terminal stages are hands-off, unconditionally. A human who set
            # ac-blocked/ac-hitl/ac-merged/ac-done must have that stick even
            # if local state still thinks the issue is FAILED and retriable —
            # the label, not state/issues.json, is the source of truth here.
            if stages.is_terminal(label_names):
                continue

            # Verb labels are hints only now; default "implement" covers
            # loop-created issues that carry no verb label at all.
            action = stages.kind_of(label_names)

            if not self._state.is_known(issue_id):
                # ac-dev-ready and ac-dev-review are the only two triggers. A
                # verb label alone (or any other stage, e.g. ac-in-progress
                # owned by a runner already working it) does not queue
                # anything.
                if stages.is_claimable(label_names):
                    record = IssueRecord(
                        issue_id=issue_id,
                        repo=repo,
                        number=issue["number"],
                        title=issue["title"],
                        body=issue.get("body", "") or "",
                        labels=label_names,
                        action=action,
                        status=IssueStatus.DISCOVERED,
                        discovered_at=issue.get("created_at", ""),
                        updated_at=issue.get("updated_at", ""),
                        issue_updated_at=issue.get("updated_at", ""),
                        mode="dev",
                    )
                    self._state.add(record)
                    self._state.save()
                    new_issues.append(record)
                    self._logger.info(
                        f"New issue: {issue_id} \"{issue['title']}\" [{stages.TRIGGER}, kind={action}]"
                    )
                elif stages.is_reviewable(label_names):
                    # Review issues skip triage entirely — "is this issue
                    # well-specified" is meaningless for a PR review — so the
                    # record is created directly at QUEUED, the same way the
                    # claimable branch above creates directly at DISCOVERED.
                    record = IssueRecord(
                        issue_id=issue_id,
                        repo=repo,
                        number=issue["number"],
                        title=issue["title"],
                        body=issue.get("body", "") or "",
                        labels=label_names,
                        action=action,
                        status=IssueStatus.QUEUED,
                        discovered_at=issue.get("created_at", ""),
                        updated_at=issue.get("updated_at", ""),
                        issue_updated_at=issue.get("updated_at", ""),
                        mode="review",
                    )
                    self._state.add(record)
                    self._state.save()
                    new_issues.append(record)
                    self._logger.info(
                        f"New reviewable issue: {issue_id} \"{issue['title']}\" "
                        f"[{stages.REVIEW_TRIGGER}]"
                    )
                # else: neither trigger label is present — ignored entirely.
            else:
                record = self._state.get(issue_id)

                # Re-triage: issue is needs_info and GitHub updated_at changed
                if (record.status == IssueStatus.NEEDS_INFO
                        and issue.get("updated_at", "") != record.issue_updated_at):
                    self._state.update(
                        issue_id,
                        issue_updated_at=issue.get("updated_at", ""),
                    )
                    self._state.save()
                    retriage_issues.append(record)
                    self._logger.info(
                        f"Re-triage candidate: {issue_id} (updated since needs_info)"
                    )

                # Rework and retry both resurrect a finished issue. Local
                # status (FAILED/COMPLETED/INTERRUPTED) is execution history,
                # not permission — only the label can authorize resuming, so
                # neither branch below fires without ac-dev-ready present.
                elif stages.is_claimable(label_names):

                    # Rework: reviewer requested changes on a completed
                    # issue's PR. Skip triage — go directly to QUEUED,
                    # preserving branch/pr_url.
                    if (action == "rework"
                          and record.status == IssueStatus.COMPLETED
                          and record.branch and record.pr_url):
                        self._state.transition(issue_id, IssueStatus.QUEUED)
                        self._state.update(
                            issue_id,
                            action="rework",
                            labels=label_names,
                            mode="dev",
                            error=None,
                            rework_count=record.rework_count + 1,
                            worker_pid=None,
                            issue_updated_at=issue.get("updated_at", ""),
                        )
                        self._state.save()
                        self._logger.info(
                            f"Rework: {issue_id} — branch={record.branch}, PR={record.pr_url}"
                        )

                    # Retry: user relabeled a failed/completed/interrupted issue
                    elif record.status in (
                        IssueStatus.FAILED,
                        IssueStatus.COMPLETED,
                        IssueStatus.INTERRUPTED,
                    ):
                        self._state.transition(issue_id, IssueStatus.DISCOVERED)
                        self._state.update(
                            issue_id,
                            action=action,
                            labels=label_names,
                            mode="dev",
                            error=None,
                            branch=None,
                            pr_url=None,
                            worker_pid=None,
                            issue_updated_at=issue.get("updated_at", ""),
                        )
                        self._state.save()
                        updated = self._state.get(issue_id)
                        new_issues.append(updated)
                        self._logger.info(
                            f"Retry: {issue_id} relabeled [{stages.TRIGGER}] — resetting from {record.status}"
                        )

                # An issue that swings around to ac-dev-review — either fresh
                # off a dev worker's PR, or re-labelled by a human — must be
                # (re)marked mode="review" and pushed to QUEUED without
                # triage. Skipped when already staged (mode="review" and
                # QUEUED) so a poll that sees no real change is a no-op.
                elif stages.is_reviewable(label_names) and not (
                    record.mode == "review" and record.status == IssueStatus.QUEUED
                ):
                    current = record.status
                    if current == IssueStatus.DISCOVERED:
                        self._state.transition(issue_id, IssueStatus.TRIAGING)
                        current = IssueStatus.TRIAGING
                    if current in _DIRECT_TO_QUEUED:
                        self._state.transition(issue_id, IssueStatus.QUEUED)
                    elif current != IssueStatus.QUEUED:
                        # IN_PROGRESS (a worker still owns it) or SKIPPED
                        # (terminal) — neither can reach QUEUED from here.
                        # Leave it; a future poll resolves the mismatch once
                        # the owning worker reports back.
                        continue

                    self._state.update(
                        issue_id,
                        action=action,
                        labels=label_names,
                        mode="review",
                        error=None,
                        issue_updated_at=issue.get("updated_at", ""),
                    )
                    self._state.save()
                    updated = self._state.get(issue_id)
                    new_issues.append(updated)
                    self._logger.info(
                        f"Review: {issue_id} -> QUEUED [{stages.REVIEW_TRIGGER}]"
                    )

        return new_issues, retriage_issues

    def _check_merged(
        self,
        repo: str,
        issue: dict,
        label_names: list[str],
        issue_id: str,
    ) -> bool:
        """Advance `issue_id` to ac-merged if its PR has merged.

        Returns True when this poll is done with the issue and the caller
        must skip every remaining branch for it. That covers two cases:

        1. The PR merged and the issue was advanced to ac-merged.
        2. The PR was **confirmed closed without merging**. Nothing is
           written — the label stays exactly where it is, for a human — but
           the issue still has to be claimed for this tick. At ac-hitl
           "leave it where it is" is genuinely inert, but ac-dev-review is
           REVIEW_TRIGGER: falling through would set the record QUEUED and
           poll step 5 would spawn an Opus review worker against a PR a
           human deliberately closed, on every 60s tick, forever.

        Never raises. A GitHub failure, and any payload this cannot read,
        return False — "could not ask" must never be read as either "merged"
        or "closed", so an unreadable answer leaves the ordinary paths
        untouched and simply tries again next tick. Every field read off the
        `get_pr_state` payload uses `.get()` for that reason: a KeyError here
        would escape `poll()`'s `except GithubClientError` and stall the loop.
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

        if not pr.get("merged"):
            if pr.get("state") != "closed":
                # Open, or a payload that does not say. Either way this is
                # not a decision — leave every other branch of the poll to
                # handle the issue exactly as it would have.
                return False

            if issue_id not in self._warned_closed:
                self._warned_closed.add(issue_id)
                self._logger.warn(
                    f"{issue_id}: PR #{pr_number} was closed without merging — "
                    f"leaving it at {stages.stage_of(label_names)} for a human"
                )
            # Claim the issue for this tick without writing anything. A human
            # closing a PR is an explicit decision, so the label stays put and
            # no rewind, no ac-done and no close happens — but the issue must
            # not fall through to the is_reviewable branch and be re-queued
            # for a review of a dead branch on every tick from now on.
            return True

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
