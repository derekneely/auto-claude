"""Tests for github_client.py — GitHub API wrappers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_client import GithubClient, pr_number_from_url  # noqa: E402


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
