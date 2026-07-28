"""Tests for orchestration framing and issue write-back.

Two gaps this covers:

1. The worker's agent was a flat coder. It now runs as an orchestrator that
   delegates to the `accelevation:*` subagents — but only the advisory ones.
   The pipeline-driving agents (dev-agent, review-agent, triage-agent,
   pipeline-planner, test-pr-agent) must never be delegated to from inside a
   worker: they self-lock ac-* labels, claim their own worktree slots, and open
   their own PRs, all of which auto-claude's Python already owns. Two systems
   driving the same labels on the same issue is the exact collision this whole
   consolidation existed to remove.

2. The issue was read as source of truth but never written back to. The agent
   now emits IMPLEMENTATION_PLAN / _SUMMARY / _NOTES and Python posts them, so
   the ticket carries the plan and the outcome rather than just a PR link.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from worker import _build_prompt, _extract_block, _issue_report  # noqa: E402

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# Agents that drive the pipeline themselves — delegating to any of these from
# inside a worker would double-drive the labels.
FORBIDDEN_AGENTS = [
    "accelevation:dev-agent",
    "accelevation:review-agent",
    "accelevation:triage-agent",
    "accelevation:pipeline-planner",
    "accelevation:test-pr-agent",
]


class TestExtractBlock:
    def test_extracts_a_single_line_section(self):
        out = "noise\nIMPLEMENTATION_SUMMARY: did the thing\nmore noise"
        assert _extract_block(out, "IMPLEMENTATION_SUMMARY") == "did the thing"

    def test_extracts_a_multi_line_section(self):
        out = (
            "IMPLEMENTATION_PLAN:\n"
            "1. read the wizard\n"
            "2. add progress state\n"
            "\n"
            "IMPLEMENTATION_SUMMARY: done\n"
        )
        assert _extract_block(out, "IMPLEMENTATION_PLAN") == (
            "1. read the wizard\n2. add progress state"
        )

    def test_stops_at_the_next_marker(self):
        out = "IMPLEMENTATION_NOTES:\nkept it minimal\nIMPLEMENTATION_SUMMARY: done\n"
        assert _extract_block(out, "IMPLEMENTATION_NOTES") == "kept it minimal"

    def test_runs_to_end_of_output_when_no_further_marker(self):
        out = "IMPLEMENTATION_NOTES:\nline one\nline two"
        assert _extract_block(out, "IMPLEMENTATION_NOTES") == "line one\nline two"

    def test_missing_marker_returns_empty(self):
        assert _extract_block("nothing here", "IMPLEMENTATION_PLAN") == ""

    def test_reads_through_stream_json(self):
        text = "IMPLEMENTATION_PLAN:\nstep one\nstep two"
        out = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "result", "result": text}),
        ])
        assert _extract_block(out, "IMPLEMENTATION_PLAN") == "step one\nstep two"

    def test_last_occurrence_wins(self):
        """The prompt echoes the marker name; only the real emission counts."""
        out = (
            "I will end with IMPLEMENTATION_SUMMARY: <one sentence>\n"
            "...work...\n"
            "IMPLEMENTATION_SUMMARY: added a progress overlay\n"
        )
        assert _extract_block(out, "IMPLEMENTATION_SUMMARY") == "added a progress overlay"

    def test_empty_section_returns_empty(self):
        out = "IMPLEMENTATION_PLAN:\n\nIMPLEMENTATION_SUMMARY: done"
        assert _extract_block(out, "IMPLEMENTATION_PLAN") == ""


class TestIssueReport:
    def test_includes_every_section_that_has_content(self):
        md = _issue_report(
            plan="1. do a thing", summary="did it", notes="watch out for X",
            attempt=1, model="claude-opus-4-6", branch="ac/issue-215-x",
            pr_url="https://github.com/o/r/pull/9", outcome="success",
        )
        assert "1. do a thing" in md
        assert "did it" in md
        assert "watch out for X" in md

    def test_omits_empty_sections_rather_than_printing_empty_headers(self):
        md = _issue_report(
            plan="", summary="did it", notes="",
            attempt=1, model="m", branch="b", pr_url=None, outcome="success",
        )
        assert "Plan" not in md
        assert "Notes" not in md
        assert "did it" in md

    def test_records_attempt_model_and_branch_for_the_audit_trail(self):
        md = _issue_report(
            plan="", summary="s", notes="", attempt=2, model="claude-sonnet-4-6",
            branch="ac/issue-215-x-v2", pr_url=None, outcome="success",
        )
        assert "2" in md and "claude-sonnet-4-6" in md and "ac/issue-215-x-v2" in md

    def test_links_the_pr_when_there_is_one(self):
        md = _issue_report(
            plan="", summary="s", notes="", attempt=1, model="m",
            branch="b", pr_url="https://github.com/o/r/pull/9", outcome="success",
        )
        assert "https://github.com/o/r/pull/9" in md

    def test_failure_outcome_is_stated_plainly(self):
        md = _issue_report(
            plan="", summary="", notes="ran out of turns", attempt=3, model="m",
            branch="b", pr_url=None, outcome="failed",
        )
        assert "ran out of turns" in md
        assert "fail" in md.lower()

    def test_is_attributed_so_humans_know_it_is_the_bot(self):
        md = _issue_report(
            plan="", summary="s", notes="", attempt=1, model="m",
            branch="b", pr_url=None, outcome="success",
        )
        assert "auto-claude" in md

    def test_no_content_at_all_still_produces_a_usable_comment(self):
        md = _issue_report(
            plan="", summary="", notes="", attempt=1, model="m",
            branch="b", pr_url=None, outcome="success",
        )
        assert md.strip()


class TestOrchestrationFragmentExists:
    def test_the_fragment_file_is_present(self):
        assert (PROMPTS / "_orchestration.txt").exists()

    def test_it_frames_the_agent_as_an_orchestrator(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8").lower()
        assert "orchestrator" in text

    def test_it_names_advisory_subagents_to_delegate_to(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8")
        for agent in ["accelevation:docs-scout", "accelevation:security-review-agent"]:
            assert agent in text, f"{agent} not offered to the orchestrator"

    def test_it_forbids_every_pipeline_driving_agent(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8")
        for agent in FORBIDDEN_AGENTS:
            assert agent in text, f"{agent} not named in the forbidden list"

    def test_it_forbids_the_agent_from_touching_labels_or_pushing(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8").lower()
        assert "label" in text
        assert "push" in text

    def test_it_asks_for_all_three_output_markers(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8")
        for marker in ["IMPLEMENTATION_PLAN:", "IMPLEMENTATION_SUMMARY:", "IMPLEMENTATION_NOTES:"]:
            assert marker in text

    def test_it_contains_no_format_placeholders(self):
        """It is substituted into a str.format() template as a value; a stray
        brace would raise KeyError at prompt-build time."""
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8")
        assert "{" not in text and "}" not in text


def _ctx(tmp_path, action="implement", handoff=None, pr_url=None):
    return worker.IssueContext(
        issue_id="field_admin#215",
        repo="field_admin",
        number=215,
        title="Job wizard progress",
        body="Show progress during job create.",
        action=action,
        org="Accelevation",
        base_branch="dev",
        repos_dir=tmp_path,
        worktrees_dir=tmp_path,
        prompts_dir=PROMPTS,
        dev_model="claude-opus-4-6",
        light_model="claude-haiku-4-5",
        permission_mode="bypassPermissions",
        max_budget_usd=10.0,
        max_turns=50,
        crash_logs_dir=tmp_path,
        color_name="blue",
        color_code="\033[34m",
        handoff_summary=handoff,
        pr_url=pr_url,
    )


class TestEveryDevPromptCarriesTheFraming:
    """A prompt that forgets the fragment silently reverts to a flat coder."""

    def test_develop(self, tmp_path):
        assert "orchestrator" in _build_prompt(_ctx(tmp_path, "implement")).lower()

    def test_test_prompt(self, tmp_path):
        assert "orchestrator" in _build_prompt(_ctx(tmp_path, "test")).lower()

    def test_continue(self, tmp_path):
        p = _build_prompt(_ctx(tmp_path, "implement", handoff="did half of it"))
        assert "orchestrator" in p.lower()

    def test_rework(self, tmp_path):
        p = _build_prompt(
            _ctx(tmp_path, "rework", pr_url="https://x/pull/1"),
            review_comments={"reviews": [], "inline": []},
        )
        assert "orchestrator" in p.lower()

    def test_the_forbidden_agents_reach_the_actual_prompt(self, tmp_path):
        p = _build_prompt(_ctx(tmp_path, "implement"))
        for agent in FORBIDDEN_AGENTS:
            assert agent in p
