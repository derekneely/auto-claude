"""Triage investigates before it asks.

The regression these pin down: on field_admin#152 triage asked the human three
questions it could have answered by reading the repo, and when the human
replied "go review #389 yourself and report back" it asked again. It had no
tools, no repo checkout, no way to represent a report, and no cap on asking.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import triage as triage_mod
from state import IssueRecord
from triage import (
    TRIAGE_TOOLS,
    TriageDecision,
    TriageEngine,
    format_clarifying_comment,
    format_findings_comment,
    format_stuck_comment,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeGithub:
    def __init__(self, comments=None):
        self._comments = comments or []

    def get_issue_comments(self, repo, number):
        return self._comments


def _record(**overrides) -> IssueRecord:
    base = dict(
        issue_id="field_admin#152",
        repo="field_admin",
        number=152,
        title="Configure ESLint on field_admin root",
        body="ESLint is not configured.",
        labels=["ac-dev-ready"],
        action="implement",
        status="triaging",
        discovered_at="2026-08-10T00:00:00Z",
        updated_at="2026-08-10T00:00:00Z",
        issue_updated_at="2026-08-10T00:00:00Z",
    )
    base.update(overrides)
    return IssueRecord(**base)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A real Config, with prompts/ and repos/ redirected into tmp_path."""
    import config as config_mod

    # config.toml interpolates this from the gitignored .env, which tests
    # deliberately do not load.
    monkeypatch.setenv("ACCELEVATION_ROOT", str(tmp_path / "accelevation"))

    project_root = Path(__file__).resolve().parent.parent
    cfg = config_mod.load_config(project_root / "config.toml")

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "triage.txt").write_text("SYSTEM PROMPT", encoding="utf-8")

    repos = tmp_path / "repos"
    (repos / "field_admin").mkdir(parents=True)

    return replace(
        cfg,
        paths=replace(cfg.paths, prompts_dir=prompts, repos_dir=repos),
    )


def _capture_run(monkeypatch, stdout: str):
    """Stub subprocess.run inside triage; return the list of recorded calls."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(triage_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(triage_mod, "build_env", lambda token: {})
    monkeypatch.setattr(triage_mod, "current_token", lambda: "token")
    return calls


def _result(payload: dict) -> str:
    return json.dumps({"result": json.dumps(payload)})


PROCEED = {
    "decision": "proceed",
    "confidence": "high",
    "summary": "Already configured by #389.",
    "findings": "Read `eslint.config.mjs` — flat config present.",
    "questions": [],
}


# ---------------------------------------------------------------------------
# Triage runs inside the repo, with tools
# ---------------------------------------------------------------------------

class TestTriageCanInvestigate:
    def test_it_runs_inside_the_issues_repo_checkout(self, config, monkeypatch):
        calls = _capture_run(monkeypatch, _result(PROCEED))

        TriageEngine(config, FakeGithub()).triage(_record())

        expected = config.paths.repos_dir / "field_admin"
        assert calls[0]["cwd"] == str(expected)

    def test_a_missing_checkout_does_not_crash_triage(self, config, monkeypatch):
        calls = _capture_run(monkeypatch, _result(PROCEED))

        decision = TriageEngine(config, FakeGithub()).triage(_record(repo="nope"))

        assert decision.decision == "proceed"
        assert calls[0]["cwd"]  # fell back to something runnable

    def test_it_passes_a_read_only_tool_allowlist(self, config, monkeypatch):
        calls = _capture_run(monkeypatch, _result(PROCEED))

        TriageEngine(config, FakeGithub()).triage(_record())

        cmd = calls[0]["cmd"]
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        assert "Read" in allowed
        assert "Grep" in allowed
        assert "Bash(gh issue view:*)" in allowed
        assert "Bash(gh pr diff:*)" in allowed

    def test_the_allowlist_grants_no_mutating_command(self, config):
        # A prefix rule is a prefix: `gh api` would carry `-X POST` with it.
        forbidden = ("Write", "Edit", "gh issue comment", "gh issue edit",
                     "gh pr merge", "gh pr create", "gh api", "git push",
                     "git commit")
        joined = " ".join(TRIAGE_TOOLS)
        for token in forbidden:
            assert token not in joined, f"{token} must not be triage-allowed"

    def test_the_timeout_leaves_room_to_investigate(self, config, monkeypatch):
        calls = _capture_run(monkeypatch, _result(PROCEED))

        TriageEngine(config, FakeGithub()).triage(_record())

        # 60s was a one-shot classifier's budget; an investigation needs more.
        assert calls[0]["timeout"] >= 120
        assert calls[0]["timeout"] == config.claude.triage_timeout_seconds

    def test_it_bounds_the_investigation_with_max_turns(self, config, monkeypatch):
        calls = _capture_run(monkeypatch, _result(PROCEED))

        TriageEngine(config, FakeGithub()).triage(_record())

        cmd = calls[0]["cmd"]
        assert cmd[cmd.index("--max-turns") + 1] == str(config.claude.triage_max_turns)


# ---------------------------------------------------------------------------
# Human directives
# ---------------------------------------------------------------------------

class TestHumanDirectives:
    def _comments(self):
        return [
            {"user": {"login": "derekneely"}, "created_at": "2026-08-10T17:37:55Z",
             "body": "I believe this was fixed by #389."},
            {"user": {"login": "accelevation-bot"}, "created_at": "2026-08-10T17:39:23Z",
             "body": "auto-claude needs more information..."},
            {"user": {"login": "derekneely"}, "created_at": "2026-08-10T17:41:13Z",
             "body": "I need YOU to review this along with #389 and report back."},
        ]

    def _prompt(self, config, comments):
        engine = TriageEngine(config, FakeGithub(comments))
        return engine._build_prompt(_record(), comments)

    def test_a_reply_after_our_question_is_marked_new(self, config):
        prompt = self._prompt(config, self._comments())

        after = prompt.split("I need YOU to review")[0]
        header = after.rsplit("---", 2)[-2] if "---" in after else after
        assert "[NEW" in header

    def test_a_comment_before_our_question_is_not_marked_new(self, config):
        prompt = self._prompt(config, self._comments())

        first = prompt.split("I believe this was fixed")[0]
        assert first.count("[NEW") == 0

    def test_it_is_told_not_to_re_ask_what_the_human_deferred(self, config):
        prompt = self._prompt(config, self._comments())

        assert "Do not re-ask" in prompt

    def test_no_new_marker_when_the_human_has_not_replied_yet(self, config):
        comments = self._comments()[:2]  # ends on the bot's question

        prompt = self._prompt(config, comments)

        assert "[NEW" not in prompt
        assert "Do not re-ask" not in prompt

    def test_comment_authors_and_timestamps_survive_into_the_prompt(self, config):
        prompt = self._prompt(config, self._comments())

        assert "@derekneely at 2026-08-10T17:41:13Z" in prompt


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

class TestFindings:
    def test_findings_are_parsed_off_the_response(self, config, monkeypatch):
        _capture_run(monkeypatch, _result(PROCEED))

        decision = TriageEngine(config, FakeGithub()).triage(_record())

        assert decision.findings == "Read `eslint.config.mjs` — flat config present."

    def test_a_response_without_findings_still_parses(self, config, monkeypatch):
        payload = {k: v for k, v in PROCEED.items() if k != "findings"}
        _capture_run(monkeypatch, _result(payload))

        decision = TriageEngine(config, FakeGithub()).triage(_record())

        assert decision.findings == ""

    def test_a_proceed_reports_what_it_found(self, config):
        body = format_findings_comment(TriageDecision(
            decision="proceed", confidence="high", summary="Already done by #389.",
            questions=[], findings="`eslint.config.mjs` exists.",
        ), config)

        assert "Already done by #389." in body
        assert "`eslint.config.mjs` exists." in body

    def test_needs_info_shows_its_work_above_the_questions(self, config):
        body = format_clarifying_comment(TriageDecision(
            decision="needs_info", confidence="medium", summary="Scope unclear.",
            questions=["Ship phase 2 now or later?"],
            findings="Phase 1 landed in #389.",
        ), config)

        assert body.index("Phase 1 landed in #389.") < body.index("Ship phase 2")


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailureHandling:
    def _failing_run(self, monkeypatch, stdouts):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            out = stdouts[min(len(calls) - 1, len(stdouts) - 1)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        monkeypatch.setattr(triage_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(triage_mod, "build_env", lambda token: {})
        monkeypatch.setattr(triage_mod, "current_token", lambda: "token")
        return calls

    def test_attempt_two_escalates_the_model(self, config, monkeypatch):
        config = replace(config, claude=replace(
            config.claude, triage_model="haiku-x", triage_escalation_model="sonnet-x",
        ))
        calls = self._failing_run(monkeypatch, ["not json", _result(PROCEED)])

        decision = TriageEngine(config, FakeGithub()).triage(_record())

        assert decision.decision == "proceed"
        assert calls[0]["cmd"][calls[0]["cmd"].index("--model") + 1] == "haiku-x"
        assert calls[1]["cmd"][calls[1]["cmd"].index("--model") + 1] == "sonnet-x"

    def test_attempt_two_gets_a_longer_clock(self, config, monkeypatch):
        calls = self._failing_run(monkeypatch, ["not json", _result(PROCEED)])

        TriageEngine(config, FakeGithub()).triage(_record())

        assert calls[1]["timeout"] > calls[0]["timeout"]

    def test_a_failed_attempt_is_logged_not_swallowed(self, config, monkeypatch):
        self._failing_run(monkeypatch, ["not json", _result(PROCEED)])
        warnings = []

        class Log:
            def warn(self, m): warnings.append(m)
            def error(self, m): pass
            def info(self, m): pass

        TriageEngine(config, FakeGithub(), Log()).triage(_record())

        assert any("attempt 1" in w for w in warnings)

    def test_the_fallback_does_not_invent_a_clarifying_question(self, config, monkeypatch):
        self._failing_run(monkeypatch, ["not json"])

        decision = TriageEngine(config, FakeGithub()).triage(_record())

        assert decision.decision == "needs_info"
        # The old fallback asked "Could you provide more details about the
        # expected behavior?" — a fabricated gap that made the human do work
        # for what was really a crashed subprocess.
        assert "expected behavior" not in " ".join(decision.questions)
        assert "errored out" in " ".join(decision.questions)


# ---------------------------------------------------------------------------
# Stuck-loop escalation
# ---------------------------------------------------------------------------

class TestStuckComment:
    def test_it_names_the_round_count_and_the_handoff(self):
        body = format_stuck_comment(TriageDecision(
            decision="needs_info", confidence="low", summary="Still unclear.",
            questions=["Which phase?"], findings="Checked #389.",
        ), rounds=4)

        assert "4 times" in body
        assert "ac-blocked" in body
        assert "Checked #389." in body
        assert "Which phase?" in body


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestResponseParsing:
    """The model is asked for bare JSON and mostly complies. Mostly is not a
    parser contract — on field_admin#152 it opened with a sentence of
    narration and then fenced the object, both attempts died, and the issue
    got a content-free question."""

    def _parse(self, config, inner: str):
        engine = TriageEngine(config, FakeGithub())
        return engine._parse_response(json.dumps({"result": inner}))

    def test_bare_json(self, config):
        d = self._parse(config, json.dumps(PROCEED))
        assert d.decision == "proceed"

    def test_a_fenced_object(self, config):
        d = self._parse(config, "```json\n" + json.dumps(PROCEED) + "\n```")
        assert d.decision == "proceed"

    def test_prose_then_a_fenced_object(self, config):
        # The exact shape that failed twice on #152.
        inner = (
            "All work is confirmed done and already closed by the human. "
            "Reporting findings.\n\n```json\n" + json.dumps(PROCEED) + "\n```"
        )
        d = self._parse(config, inner)
        assert d.decision == "proceed"
        assert d.findings == PROCEED["findings"]

    def test_prose_before_and_after(self, config):
        inner = "Here you go:\n" + json.dumps(PROCEED) + "\nHope that helps."
        assert self._parse(config, inner).decision == "proceed"

    def test_braces_inside_string_values_do_not_break_the_scan(self, config):
        payload = dict(PROCEED, findings="Config uses `{ ignoreDuringBuilds: true }`")
        inner = "Note:\n```json\n" + json.dumps(payload) + "\n```"
        assert self._parse(config, inner).findings == payload["findings"]

    def test_escaped_quotes_inside_string_values_survive(self, config):
        payload = dict(PROCEED, findings='package.json has \\"lint\\": \\"eslint .\\"')
        inner = "Findings below.\n" + json.dumps(payload)
        assert self._parse(config, inner).findings == payload["findings"]

    def test_a_decoy_object_in_the_prose_is_skipped(self, config):
        inner = (
            "The repo config looks like {\"eslint\": {\"ignoreDuringBuilds\": true}} "
            "today.\n\n```json\n" + json.dumps(PROCEED) + "\n```"
        )
        assert self._parse(config, inner).decision == "proceed"

    def test_a_reply_with_no_object_at_all_still_raises(self, config):
        with pytest.raises(json.JSONDecodeError):
            self._parse(config, "I could not determine an answer.")

    def test_an_empty_reply_still_raises(self, config):
        with pytest.raises(json.JSONDecodeError):
            self._parse(config, "")


class TestRawReplyIsLogged:
    def test_an_unparseable_reply_is_shown_in_the_log(self, config, monkeypatch):
        # Empty stdout and unparseable stdout raise the identical
        # JSONDecodeError message, so the log must carry the reply itself.
        _capture_run(monkeypatch, json.dumps({"result": "no json here at all"}))
        warnings = []

        class Log:
            def warn(self, m): warnings.append(m)
            def error(self, m): pass
            def info(self, m): pass

        TriageEngine(config, FakeGithub(), Log()).triage(_record())

        assert any("no json here at all" in w for w in warnings)
