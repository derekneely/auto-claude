"""Tests for pull-request body construction.

Two defects this covers, both found on the live PR #334 that auto-claude
opened against field_admin:

1. `_extract_summary` checked its raw-text branch BEFORE trying to parse the
   line as JSON. Under `output_format = "stream-json"` the whole result event
   is one physical line whose raw text contains "IMPLEMENTATION_SUMMARY:", so
   the raw branch always won and returned everything from the marker to the
   end of the JSON line — literal \\n escapes, the entire NOTES block, and the
   envelope tail carrying `session_id`, `request_id`, `uuid` and token counts.
   All of that was published into a public pull request.

2. Nothing ever asked the agent how to test the change, so no PR could
   explain it. The body is now an assembled four-section document rather than
   one raw string with "Closes #N" stapled to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from worker import OUTPUT_MARKERS, _extract_summary, _pr_body  # noqa: E402

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

#: The exact agent text behind PR #334 — a single-line summary followed by a
#: multi-line notes block. Reproduces the shape that broke extraction.
AGENT_TEXT = (
    "IMPLEMENTATION_SUMMARY:\n"
    "Job wizard now shows a blocking, step-labeled progress overlay.\n"
    "\n"
    "IMPLEMENTATION_NOTES:\n"
    "- E2E run created a real job in the shared dev environment.\n"
    "- Determinate progress could not be observed in dev.\n"
)


def _stream_json(text: str) -> str:
    """A realistic stream-json capture: init, assistant turn, result envelope.

    `json.dumps` escapes the newlines inside `result`, so the whole event
    lands on one physical line — which is precisely what defeated the
    line-by-line raw-text scan.
    """
    return "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "bd117aac"}),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "result": text,
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 111444},
            "session_id": "bd117aac-4583-4b28-9d1c-2ce4d3a7f9e1",
            "uuid": "6040637d-b060-43ab-8695-f2f6de9e1f8a",
            "request_id": "req_011CdaBYxihHpikyRfDw9ZBL",
        }),
    ])


class TestExtractSummaryUnderStreamJson:
    def test_returns_only_the_summary_sentence(self):
        out = _stream_json(AGENT_TEXT)
        assert _extract_summary(out) == (
            "Job wizard now shows a blocking, step-labeled progress overlay."
        )

    def test_does_not_leak_the_json_envelope(self):
        """The PR #334 body ended with `"usage":{...},"request_id":"req_..."`."""
        summary = _extract_summary(_stream_json(AGENT_TEXT))
        for leaked in ("request_id", "session_id", "usage", "uuid", "input_tokens"):
            assert leaked not in summary, f"{leaked} leaked into the PR body"

    def test_does_not_emit_literal_backslash_n(self):
        """Raw JSON escapes rendered as text instead of real newlines."""
        assert "\\n" not in _extract_summary(_stream_json(AGENT_TEXT))

    def test_does_not_absorb_the_notes_block(self):
        summary = _extract_summary(_stream_json(AGENT_TEXT))
        assert "IMPLEMENTATION_NOTES" not in summary
        assert "E2E run created" not in summary

    def test_still_reads_plain_text_output(self):
        assert _extract_summary(AGENT_TEXT) == (
            "Job wizard now shows a blocking, step-labeled progress overlay."
        )

    def test_missing_marker_returns_empty(self):
        assert _extract_summary(_stream_json("no markers here")) == ""


class TestPrBody:
    def test_renders_every_section_that_has_content(self):
        md = _pr_body(
            summary="Adds a progress overlay.",
            changes="- `page.tsx` — per-step progress state",
            testing="1. Go to /jobs/configure\n2. Click Create Job",
            notes="Overlay persists through router.push.",
            number=215,
        )
        assert "## Summary" in md
        assert "## Changes" in md
        assert "## How to test" in md
        assert "## Notes for the reviewer" in md
        assert "Adds a progress overlay." in md
        assert "1. Go to /jobs/configure" in md

    def test_always_closes_the_issue(self):
        md = _pr_body(summary="s", changes="", testing="", notes="", number=215)
        assert "Closes #215" in md

    def test_omits_empty_sections_rather_than_printing_bare_headers(self):
        md = _pr_body(summary="s", changes="", testing="", notes="", number=7)
        assert "## Changes" not in md
        assert "## How to test" not in md
        assert "## Notes for the reviewer" not in md

    def test_whitespace_only_sections_count_as_empty(self):
        """An agent that emits a marker then a blank line must not produce a
        header with nothing under it."""
        md = _pr_body(
            summary="s", changes="   ", testing="\n\n", notes="\t", number=7,
        )
        assert "## Changes" not in md
        assert "## How to test" not in md
        assert "## Notes for the reviewer" not in md

    def test_redacts_secrets_from_agent_text(self):
        md = _pr_body(
            summary="used ghp_" + "A" * 36 + " to push",
            changes="", testing="", notes="", number=7,
        )
        assert "ghp_" + "A" * 36 not in md

    def test_produces_no_literal_escape_sequences(self):
        md = _pr_body(
            summary="a", changes="b", testing="c", notes="d", number=1,
        )
        assert "\\n" not in md

    def test_empty_everything_still_produces_a_usable_body(self):
        md = _pr_body(summary="", changes="", testing="", notes="", number=9)
        assert md.strip()
        assert "Closes #9" in md


class TestTestingStepsAreRequestedFromTheAgent:
    def test_testing_steps_is_a_parsed_marker(self):
        assert "TESTING_STEPS" in OUTPUT_MARKERS

    def test_changes_is_a_parsed_marker(self):
        assert "CHANGES" in OUTPUT_MARKERS

    def test_the_prompt_asks_for_testing_steps(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8")
        assert "TESTING_STEPS:" in text

    def test_the_prompt_asks_for_changes(self):
        text = (PROMPTS / "_orchestration.txt").read_text(encoding="utf-8")
        assert "CHANGES:" in text

    def test_the_new_markers_reach_the_built_prompt(self, tmp_path):
        ctx = worker.IssueContext(
            issue_id="field_admin#215", repo="field_admin", number=215,
            title="t", body="b", action="implement", org="Accelevation",
            base_branch="dev", repos_dir=tmp_path, worktrees_dir=tmp_path,
            prompts_dir=PROMPTS, dev_model="m", light_model="l",
            permission_mode="bypassPermissions", max_budget_usd=10.0,
            max_turns=50, crash_logs_dir=tmp_path, color_name="blue",
            color_code="\033[34m",
        )
        prompt = worker._build_prompt(ctx)
        assert "TESTING_STEPS:" in prompt
        assert "CHANGES:" in prompt

    def test_extract_block_separates_the_new_markers(self):
        out = (
            "CHANGES:\n- `a.ts` — did a thing\n"
            "TESTING_STEPS:\n1. open the page\n2. click it\n"
            "IMPLEMENTATION_NOTES:\nNone.\n"
        )
        assert worker._extract_block(out, "CHANGES") == "- `a.ts` — did a thing"
        assert worker._extract_block(out, "TESTING_STEPS") == "1. open the page\n2. click it"
        assert worker._extract_block(out, "IMPLEMENTATION_NOTES") == "None."
