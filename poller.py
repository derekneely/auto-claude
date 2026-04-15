"""Poller — discovers new and updated issues across monitored repos."""

from __future__ import annotations

from config import Config
from github_client import GithubClient, GithubClientError
from logger import MainLogger
from state import IssueRecord, IssueStatus, StateStore


class Poller:
    """Queries GitHub for issues with ac-* labels and diffs against local state."""

    def __init__(self, config: Config, github: GithubClient, state: StateStore,
                 logger: MainLogger) -> None:
        self._config = config
        self._github = github
        self._state = state
        self._logger = logger

        # Status labels that indicate auto-claude is already handling the issue.
        # Issues carrying only these labels (no action label) are excluded.
        gh = config.github
        self._status_labels = {
            gh.needs_info_label,
            gh.in_progress_label,
            gh.pr_created_label,
            gh.plan_posted_label,
            gh.review_posted_label,
        }

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

        issues = self._github.list_issues(repo)
        prefix = self._config.github.label_prefix

        for issue in issues:
            label_names = [lbl["name"] for lbl in issue.get("labels", [])]

            # Find action labels (ac-fix, ac-implement, etc.)
            action_label = self._find_action_label(label_names)

            # Skip issues that have no ac-* action label
            if action_label is None:
                continue

            issue_id = f"{repo}#{issue['number']}"
            action = action_label[len(prefix):]  # strip "ac-" prefix

            if not self._state.is_known(issue_id):
                # New issue — add to state as discovered
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
                )
                self._state.add(record)
                self._state.save()
                new_issues.append(record)
                self._logger.info(
                    f"New issue: {issue_id} \"{issue['title']}\" [{action_label}]"
                )
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

                # Plan-to-action: issue was plan_posted, user added a new action label
                elif record.status == IssueStatus.PLAN_POSTED:
                    self._state.transition(issue_id, IssueStatus.DISCOVERED)
                    self._state.update(
                        issue_id,
                        action=action,
                        labels=label_names,
                        issue_updated_at=issue.get("updated_at", ""),
                    )
                    self._state.save()
                    updated = self._state.get(issue_id)
                    new_issues.append(updated)
                    self._logger.info(
                        f"Plan→action: {issue_id} relabeled [{action_label}]"
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
                        error=None,
                        retry_count=0,
                        branch=None,
                        pr_url=None,
                        worker_pid=None,
                        issue_updated_at=issue.get("updated_at", ""),
                    )
                    self._state.save()
                    updated = self._state.get(issue_id)
                    new_issues.append(updated)
                    self._logger.info(
                        f"Retry: {issue_id} relabeled [{action_label}] — resetting from {record.status}"
                    )

        return new_issues, retriage_issues

    def _find_action_label(self, labels: list[str]) -> str | None:
        """Return the first ac-* action label found, or None."""
        action_labels = set(self._config.github.action_labels)
        for label in labels:
            if label in action_labels:
                return label
        return None
