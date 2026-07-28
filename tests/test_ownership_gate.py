"""Tests for the un-skippable ownership gate.

auto-claude must refuse to start unless it knows *who it is*. Two failures let
it run "rogue" - working issues that belong to a human, or working them under
the operator's own identity:

1. No `bot_login` configured. `list_issues` then sends no assignee filter and
   every correctly-labelled issue in the org is fair game, including ones a
   human `/loop` runner has already claimed.
2. No bot token. `gh` falls back to the operator's stored credentials, so
   commits, PRs and comments are attributed to a person who did not write them.

Neither is recoverable at runtime and neither is worth a warning you can scroll
past, so both are fatal - and unlike the repo/projects checks, they are NOT
bypassable with --skip-preflight.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghauth  # noqa: E402
from ghauth import (  # noqa: E402
    check_ownership_config,
    has_fatal,
    verify_identity,
)

FAKE = "ghp_faketokenfortests"


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        for pattern, response in self.responses.items():
            if pattern in " ".join(args):
                return response
        return (1, "", "unexpected call")


class TestOwnershipConfigIsFatal:
    def test_missing_bot_login_is_fatal(self):
        checks = check_ownership_config(token=FAKE, bot_login=None)
        assert has_fatal(checks)

    def test_blank_bot_login_is_fatal(self):
        checks = check_ownership_config(token=FAKE, bot_login="   ")
        assert has_fatal(checks)

    def test_missing_token_is_fatal(self):
        checks = check_ownership_config(token=None, bot_login="accelevation-bot")
        assert has_fatal(checks)

    def test_both_present_passes(self):
        checks = check_ownership_config(token=FAKE, bot_login="accelevation-bot")
        assert not has_fatal(checks)
        assert all(c.ok for c in checks)

    def test_explains_the_consequence_not_just_the_absence(self):
        # An unattended run fails at boot; the message is all the operator gets.
        detail = " ".join(
            c.detail for c in check_ownership_config(token=FAKE, bot_login=None)
            if not c.ok
        ).lower()
        assert "assign" in detail or "bot_login" in detail

    def test_makes_no_network_calls(self, monkeypatch):
        # This gate runs before preflight and must work offline - it is a
        # config-coherence check, not a credential check.
        def explode(*_a, **_k):
            raise AssertionError("ownership gate must not shell out")

        monkeypatch.setattr("subprocess.run", explode)
        check_ownership_config(token=None, bot_login=None)


class TestVerifyIdentity:
    def test_matching_login_passes(self):
        checks = verify_identity(
            token=FAKE, expected_login="accelevation-bot",
            run=FakeRunner({"api user": (0, "accelevation-bot", "")}),
        )
        assert not has_fatal(checks)

    def test_wrong_login_is_fatal_even_without_a_token(self):
        # Previously this was downgraded to a warning when no token was in play,
        # which is exactly the case where running as the operator is worst.
        checks = verify_identity(
            token=None, expected_login="accelevation-bot",
            run=FakeRunner({"api user": (0, "derekneely", "")}),
        )
        assert has_fatal(checks)

    def test_failed_call_is_fatal(self):
        checks = verify_identity(
            token=FAKE, expected_login="accelevation-bot",
            run=FakeRunner({"api user": (1, "", "HTTP 401: Bad credentials")}),
        )
        assert has_fatal(checks)

    def test_names_both_logins_in_the_failure(self):
        checks = verify_identity(
            token=FAKE, expected_login="accelevation-bot",
            run=FakeRunner({"api user": (0, "derekneely", "")}),
        )
        detail = checks[0].detail
        assert "derekneely" in detail and "accelevation-bot" in detail

    def test_issues_exactly_one_network_call(self):
        runner = FakeRunner({"api user": (0, "accelevation-bot", "")})
        verify_identity(token=FAKE, expected_login="accelevation-bot", run=runner)
        assert len(runner.calls) == 1


class TestPreflightStillComposesTheGate:
    """preflight() keeps its all-in-one contract for callers and tests."""

    def test_includes_the_ownership_checks(self):
        checks = ghauth.preflight(
            token=None, org="Accelevation", repos=[],
            expected_login="accelevation-bot",
            run=FakeRunner({"api user": (0, "accelevation-bot", ""),
                            "project list": (0, "", "")}),
        )
        assert has_fatal(checks), "a tokenless preflight must now fail"
