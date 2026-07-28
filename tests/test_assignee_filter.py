"""Tests for assignee-scoped issue discovery.

Ownership boundary: auto-claude works only issues assigned to its bot account.
The human `/loop` runners already scope themselves the same way
(`gh issue list --assignee @me`), so this makes the protection symmetric -
without it, auto-claude would pick up a correctly-labelled issue belonging to a
human and two agents would work it at once.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_client import GithubClient  # noqa: E402
from poller import Poller  # noqa: E402


class RecordingClient(GithubClient):
    """GithubClient with the network seam replaced."""

    def __init__(self, org="Accelevation", payload=None):
        super().__init__(org)
        self.endpoints: list[str] = []
        self._payload = payload if payload is not None else []

    def _gh_api(self, endpoint, **kwargs):
        self.endpoints.append(endpoint)
        return self._payload


class TestListIssuesAssigneeParam:
    def test_adds_the_assignee_filter(self):
        c = RecordingClient()
        c.list_issues("field_admin", assignee="accelevation-bot")
        assert "assignee=accelevation-bot" in c.endpoints[0]

    def test_omits_the_filter_when_none(self):
        c = RecordingClient()
        c.list_issues("field_admin")
        assert "assignee=" not in c.endpoints[0]

    def test_keeps_existing_query_parameters(self):
        c = RecordingClient()
        c.list_issues("field_admin", state="all", assignee="accelevation-bot")
        ep = c.endpoints[0]
        assert "state=all" in ep
        assert "per_page=100" in ep
        assert "assignee=accelevation-bot" in ep

    def test_url_encodes_the_login(self):
        c = RecordingClient()
        c.list_issues("field_admin", assignee="a b")
        assert "assignee=a%20b" in c.endpoints[0] or "assignee=a+b" in c.endpoints[0]

    def test_still_excludes_pull_requests(self):
        payload = [
            {"number": 1, "title": "issue"},
            {"number": 2, "title": "pr", "pull_request": {"url": "..."}},
        ]
        c = RecordingClient(payload=payload)
        out = c.list_issues("field_admin", assignee="accelevation-bot")
        assert [i["number"] for i in out] == [1]


def make_poller(bot_login, client):
    config = SimpleNamespace(
        github=SimpleNamespace(
            org="Accelevation",
            repos=["field_admin"],
            label_prefix="ac-",
            action_labels=["ac-implement"],
            needs_info_label="ac-needs-info",
            in_progress_label="ac-in-progress",
            pr_created_label="ac-pr-created",
            bot_login=bot_login,
        )
    )
    logger = SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    state = SimpleNamespace(
        is_known=lambda _i: False, get=lambda _i: None,
        add=lambda _r: None, save=lambda: None,
    )
    return Poller(config, client, state, logger)


class TestPollerPassesTheFilter:
    def test_scopes_discovery_to_the_bot(self):
        c = RecordingClient()
        make_poller("accelevation-bot", c).poll()
        assert c.endpoints, "poller made no request"
        assert "assignee=accelevation-bot" in c.endpoints[0]

    def test_no_filter_when_bot_login_unconfigured(self):
        # Back-compat: an operator running without a bot account still works.
        c = RecordingClient()
        make_poller(None, c).poll()
        assert "assignee=" not in c.endpoints[0]

    def test_issue_assigned_to_a_human_is_never_returned(self):
        # The API does the filtering, so the guarantee is that we ASK for it.
        # This asserts the request is scoped, which is what prevents the
        # double-dispatch the loop's own @me filter already prevents.
        c = RecordingClient(payload=[{
            "number": 7, "title": "human work", "body": "",
            "labels": [{"name": "ac-implement"}],
            "assignees": [{"login": "derekneely"}],
            "created_at": "", "updated_at": "",
        }])
        make_poller("accelevation-bot", c).poll()
        assert "assignee=accelevation-bot" in c.endpoints[0]
